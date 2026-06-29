import asyncio
import os

from playwright.async_api import Page

from utils import PageLogger

_CLOSE_SESSIONS_URL = "https://sister3.agenziaentrate.gov.it/Servizi/CloseSessionsSis"
_LOGIN_URL = "https://iampe.agenziaentrate.gov.it/sam/UI/Login?realm=/agenziaentrate"


async def _chiudi_tutte_sessioni(page: Page, max_tentativi: int = 15) -> None:
    """Naviga ripetutamente a CloseSessionsSis finché Sister non ha sessioni attive."""
    for i in range(max_tentativi):
        try:
            await page.goto(_CLOSE_SESSIONS_URL, timeout=20000)
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
            content = await page.content()
            if "Utente gia' in sessione" not in content:
                print(f"[LOGIN-SISTER] Sessioni pulite dopo {i+1} chiusura/e.")
                return
            print(f"[LOGIN-SISTER] Sessione ancora attiva, chiudo ancora (tentativo {i+1}/{max_tentativi})...")
            await asyncio.sleep(3)
        except Exception as e:
            print(f"[LOGIN-SISTER] Errore chiusura sessione {i+1}: {e}")
            await asyncio.sleep(3)
    print(f"[LOGIN-SISTER] Sessioni non pulite dopo {max_tentativi} tentativi.")


async def login(page: Page):
    """Login diretto via tab Sister su iampe.agenziaentrate.gov.it.

    Sostituisce solo il flusso di autenticazione (tab Sister invece di SPID).
    La navigazione post-login dentro Sister è identica all'originale utils.login().
    Chiude eventuali sessioni attive prima di procedere e riprova fino a 5 volte.
    """
    username = os.getenv("ADE_USERNAME")
    password = os.getenv("ADE_PASSWORD")

    if not username or not password:
        raise ValueError("ADE_USERNAME e ADE_PASSWORD devono essere impostati nel .env")

    # Chiudi proattivamente qualsiasi sessione residua prima di tentare il login.
    print("[LOGIN-SISTER] Chiudo eventuali sessioni residue...")
    await _chiudi_tutte_sessioni(page)
    await page.context.clear_cookies()
    await asyncio.sleep(3)

    for tentativo in range(5):
        try:
            await _esegui_login(page, username, password)
            return
        except _SessoneBloccataError:
            print(f"[LOGIN-SISTER] Sessione bloccata (tentativo {tentativo+1}/5) — chiudo tutte le sessioni...")
            await _chiudi_tutte_sessioni(page)
            print("[LOGIN-SISTER] Attendo 30s prima di riprovare...")
            await asyncio.sleep(30)
            # Cancella i cookie SSO: senza questo iampe auto-autentica bypassando il form
            # e crea una nuova sessione Sister che si blocca immediatamente.
            await page.context.clear_cookies()

    raise Exception("Utente già in sessione su un'altra postazione (dopo chiusura)")


class _SessoneBloccataError(Exception):
    pass


async def _esegui_login(page: Page, username: str, password: str):
    logger = PageLogger("login")
    step = "init"

    try:
        # --- Autenticazione Sister (parte unica di questo modulo) ---

        step = "goto_login"
        print("[LOGIN-SISTER] Navigo alla pagina di login...")
        await page.goto(_LOGIN_URL)
        await logger.log(page, "goto_login")

        step = "click_tab_sister"
        print("[LOGIN-SISTER] Clicco tab 'Sister'...")
        await page.locator('a[href="#tab-5"]').click()
        await logger.log(page, "click_tab_sister")

        # Scoped a #tab-5 per evitare strict mode violation
        tab = page.locator('#tab-5')

        step = "username"
        print("[LOGIN-SISTER] Inserisco username...")
        await tab.locator('#username-sister').fill(username)
        await logger.log(page, "username")

        step = "password"
        print("[LOGIN-SISTER] Inserisco password...")
        await tab.locator('#password-fo-sist').fill(password)

        step = "submit"
        print("[LOGIN-SISTER] Clicco 'Accedi'...")
        await tab.locator('button.btn-primary').click()
        # Aspetta che la chain iampe → portale → sister3 sia completata
        await page.wait_for_url(
            lambda url: "sister3.agenziaentrate.gov.it" in url,
            timeout=60000,
        )
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
        await logger.log(page, "submit")

        step = "controllo_sessione"
        print("[LOGIN-SISTER] Controllo blocco sessione...")
        content = await page.content()
        url = page.url
        if "Utente gia' in sessione" in content or "error_locked.jsp" in url:
            await logger.log(page, "sessione_bloccata")
            raise _SessoneBloccataError()

        # --- Navigazione dentro Sister via sidebar ---
        # Il goto diretto a Informativa.do viene rediretto a index.jsp: usa i click.

        step = "conferma"
        print("[LOGIN-SISTER] Clicco 'Conferma'...")
        await page.get_by_role("button", name="Conferma").click()
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
        await logger.log(page, "conferma")

        step = "consultazioni"
        print("[LOGIN-SISTER] Clicco 'Consultazioni e Certificazioni'...")
        await page.get_by_role("link", name="Consultazioni e Certificazioni").click()
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
        await logger.log(page, "consultazioni")

        step = "visure_catastali"
        print("[LOGIN-SISTER] Clicco 'Visure catastali'...")
        await page.get_by_role("link", name="Visure catastali").click()
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
        await logger.log(page, "visure_catastali")

        step = "conferma_lettura"
        print("[LOGIN-SISTER] Clicco 'Conferma Lettura'...")
        await page.get_by_role("link", name="Conferma Lettura").click()
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
        await logger.log(page, "conferma_lettura")

        step = "selezione_convenzione"
        print("[LOGIN-SISTER] Verifico selezione convenzione...")
        conv_radio = page.locator("form[name='SelConv'] input[type='radio']")
        if await conv_radio.count() > 0:
            print("[LOGIN-SISTER] Selezione convenzione trovata, seleziono la prima opzione...")
            await conv_radio.first.click()
            await page.locator("input[type='submit'][value='Avanti']").click()
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
            print("[LOGIN-SISTER] Convenzione selezionata.")
        await logger.log(page, "selezione_convenzione")

        print("[LOGIN-SISTER] Login completato.")

    except _SessoneBloccataError:
        raise
    except Exception:
        await logger.log(page, f"ERRORE_{step}")
        raise
