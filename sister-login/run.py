import asyncio
import base64
import os
import sys

from fastapi import HTTPException
from pydantic import BaseModel

sys.path.insert(0, "/app")

# --- Patch del metodo di login PRIMA di importare main ---

auth_type = os.getenv("AUTH_TYPE", "spid").lower()

import main as _main  # noqa: E402 (importazione ritardata necessaria per il patch)

if auth_type == "sister":
    from sister_auth import login as _sister_login
    _main.login = _sister_login
    print("[RUN] Modalità autenticazione: SISTER (credenziali dirette)")
else:
    print("[RUN] Modalità autenticazione: SPID (Sielte ID)")


# --- Patch di VisuraService.initialize: salta il login automatico ---
# Le credenziali arrivano via POST /login da Laravel, non dall'env.

_original_initialize = _main.VisuraService.initialize


async def _initialize_senza_autologin(self):
    await self.browser_manager.initialize()
    asyncio.create_task(self._process_requests())
    print("[RUN] Browser pronto. In attesa di POST /login per autenticazione.")


_main.VisuraService.initialize = _initialize_senza_autologin


# --- Patch BrowserManager per Sister auth ---
# _check_session_validity e _perform_session_refresh navigano a SceltaServizio.do
# con networkidle, causando falsi negativi subito dopo il login (province non ancora
# caricate) e doppi login. Per Sister ci fidiamo del flag authenticated.

import logging as _logging
_run_logger = _logging.getLogger("run")

# EasyOCR reader istanziato una sola volta all'avvio
_easyocr_reader = None

def _get_ocr_reader():
    global _easyocr_reader
    if _easyocr_reader is None:
        import easyocr
        _easyocr_reader = easyocr.Reader(['en'], gpu=False, verbose=False)
        print("[OCR] EasyOCR reader inizializzato")
    return _easyocr_reader


def _leggi_captcha(img_bytes: bytes) -> str:
    """Tenta di leggere il captcha con EasyOCR e 4 strategie di preprocessing."""
    from PIL import Image, ImageOps, ImageEnhance
    import numpy as np
    import io as _io

    reader = _get_ocr_reader()
    img = Image.open(_io.BytesIO(img_bytes))
    arr = np.array(img.convert('RGB'))

    def read(data):
        try:
            r = reader.readtext(data, detail=0, allowlist='abcdefghijklmnopqrstuvwxyz0123456789')
            return ''.join(r).lower().replace(' ', '')
        except Exception:
            return ''

    results = []

    # Strategia 1: gray + autocontrast + 3x  (ottima per stile rosso/verde)
    ig = ImageOps.autocontrast(img.convert('L'))
    ig3 = ig.resize((ig.width * 3, ig.height * 3), Image.LANCZOS)
    buf = _io.BytesIO(); ig3.save(buf, 'PNG')
    results.append(('gray+3x', read(buf.getvalue())))

    # Strategia 2: sharpen + gray + 3x  (ottima per stile bianco/blu)
    ig_sharp = ImageEnhance.Sharpness(ig).enhance(3.0)
    ig3s = ig_sharp.resize((ig.width * 3, ig.height * 3), Image.LANCZOS)
    buf = _io.BytesIO(); ig3s.save(buf, 'PNG')
    results.append(('sharp+gray+3x', read(buf.getvalue())))

    # Strategia 3: canale colore più contrastato + 3x  (ottima per stile blu glow)
    best_ch = int(np.argmax([arr[:, :, c].std() for c in range(3)]))
    ich = ImageOps.autocontrast(Image.fromarray(arr[:, :, best_ch]))
    ich3 = ich.resize((ich.width * 3, ich.height * 3), Image.LANCZOS)
    buf = _io.BytesIO(); ich3.save(buf, 'PNG')
    results.append((f'ch{best_ch}+3x', read(buf.getvalue())))

    # Strategia 4: colore originale 3x
    img3 = img.resize((img.width * 3, img.height * 3), Image.LANCZOS)
    buf = _io.BytesIO(); img3.save(buf, 'PNG')
    results.append(('color+3x', read(buf.getvalue())))

    # Strategie 5-9: separazione canali colore (efficace per captcha colorati)
    # R-G: testo rosso/giallo su sfondo scuro; B-R/B-G: testo blu su qualsiasi sfondo
    w, h = img.width * 3, img.height * 3
    for name, ch_a, ch_b in [('R-G', 0, 1), ('R-B', 0, 2), ('G-B', 1, 2), ('B-R', 2, 0), ('B-G', 2, 1)]:
        diff = np.clip(arr[:, :, ch_a].astype(int) - arr[:, :, ch_b].astype(int), 0, 255).astype(np.uint8)
        diff_img = ImageOps.autocontrast(Image.fromarray(diff)).resize((w, h), Image.LANCZOS)
        buf = _io.BytesIO(); diff_img.save(buf, 'PNG')
        results.append((name + '+3x', read(buf.getvalue())))

    print(f"[OCR] Strategie: { {k: v for k, v in results} }")

    from collections import Counter

    results_dict = {k: v for k, v in results}

    def pick(key):
        t = results_dict.get(key, '')
        return t if 5 <= len(t) <= 10 else ''

    # Rilevamento stile basato su pixel dominanti
    rg_diff = np.clip(arr[:, :, 0].astype(int) - arr[:, :, 1].astype(int), 0, 255)
    br_diff = np.clip(arr[:, :, 2].astype(int) - arr[:, :, 0].astype(int), 0, 255)
    red_frac  = float((rg_diff > 80).mean())   # testo rosso su verde
    blue_frac = float((br_diff > 80).mean())   # testo blu su sfondo chiaro
    print(f"[OCR] Style detect: red_frac={red_frac:.3f} blue_frac={blue_frac:.3f}")

    # Testo blu su qualsiasi sfondo (incluso rosso): B-R isola il testo blu
    # Priorità PRIMA di rosso: captcha blu-su-rosso ha red_frac alto (bg rosso)
    # ma deve usare B-R, non R-G.
    if blue_frac > 0.15:
        for key in ('B-R+3x', 'B-G+3x'):
            t = pick(key)
            if t:
                print(f"[OCR] Stile BLU → {key}: '{t}'")
                return t

    # Testo rosso/giallo su sfondo scuro/verde: R-G isola il testo
    if red_frac > 0.1:
        t = pick('R-G+3x')
        if t:
            print(f"[OCR] Stile ROSSO/VERDE → R-G: '{t}'")
            return t

    # Tutti gli altri stili: maggioranza
    validi = [t for _, t in results if 5 <= len(t) <= 10]
    if validi:
        counter = Counter(validi)
        best_t, count = counter.most_common(1)[0]
        print(f"[OCR] Maggioranza: '{best_t}' ({count} voti)")
        return best_t

    # Fallback: il risultato più lungo
    fallback = max(results, key=lambda x: len(x[1]))
    print(f"[OCR] Fallback '{fallback[1]}' da strategia {fallback[0]}")
    return fallback[1]


async def _ensure_authenticated_sister(self):
    if self.authenticated:
        return
    _run_logger.info("[SISTER] Sessione non autenticata, eseguo re-login Sister...")
    try:
        await self.login()
        await self.start_keep_alive()
    except Exception as e:
        _run_logger.error(f"[SISTER] Re-autenticazione fallita: {e}")
        raise _main.AuthenticationError(f"Re-authentication failed: {e}") from e


async def _perform_session_refresh_sister(self):
    # Per Sister il keep-alive leggero (mouse movement) è sufficiente.
    # Non navighiamo a SceltaServizio per non interferire con le visure in corso.
    return True


if auth_type == "sister":
    _main.BrowserManager._ensure_authenticated = _ensure_authenticated_sister
    _main.BrowserManager._perform_session_refresh = _perform_session_refresh_sister
    print("[RUN] Patch _ensure_authenticated e _perform_session_refresh per Sister.")


# --- Patch esegui_visura: aggiunge PDF al termine del flusso originale ---
# Dopo che il flusso standard estrae i dati, clicca "Visura Per Immobile"
# (bottone già presente sulla pagina risultati con radio pre-selezionato)
# e genera il PDF del documento ufficiale.

async def _risolvi_captcha_e_inoltra(page, max_tentativi: int = 12) -> bool:
    """Imposta i parametri del form, risolve il captcha se presente e invia."""
    for tentativo in range(max_tentativi):
        captcha_el = page.locator('#imgCaptcha')
        ha_captcha = await captcha_el.count() > 0

        if ha_captcha:
            # Attende che l'immagine captcha sia effettivamente caricata
            await page.wait_for_function(
                "document.querySelector('#imgCaptcha') && document.querySelector('#imgCaptcha').complete && document.querySelector('#imgCaptcha').naturalWidth > 0",
                timeout=10000,
            )
            await asyncio.sleep(0.3)
            img_bytes = await captcha_el.screenshot()

            import os
            debug_dir = "/app/logs/captcha_debug"
            os.makedirs(debug_dir, exist_ok=True)
            with open(f"{debug_dir}/captcha_{tentativo+1}_raw.png", "wb") as f:
                f.write(img_bytes)

            testo = _leggi_captcha(img_bytes)

            if len(testo) < 4:
                print(f"[PDF] OCR insufficiente ('{testo}'), ricarico captcha...")
                await page.locator('a[onclick*="reloadImg"]').click()
                await asyncio.sleep(1.5)
                continue

            await page.locator('#inCaptchaChars').fill(testo)

        # Imposta i parametri e invia — sempre, con o senza captcha
        await page.locator('input[type="radio"][name="intestati"][value="1"]').check()
        await page.locator('input[type="radio"][name="tipoDocFornitura"][value="PDF"]').check()
        await page.locator('input[name="inoltra"]').click()
        await page.wait_for_load_state("domcontentloaded", timeout=30000)
        print(f"[PDF] Dopo inoltra (tentativo {tentativo+1}): {page.url}")

        content = await page.content()
        if "inCaptchaChars" in content or "Codice di sicurezza" in content:
            print("[PDF] Captcha errato, ricarico e riprovo...")
            await page.locator('a[onclick*="reloadImg"]').click()
            await asyncio.sleep(1)
            continue

        return True

    print("[PDF] Form non inviato dopo tutti i tentativi.")
    return False


_orig_esegui_visura = _main.BrowserManager.esegui_visura


async def _esegui_visura_con_pdf(self, request):
    response = await _orig_esegui_visura(self, request)
    if not response.success:
        # Sessione Sister potrebbe essere scaduta: reset flag così il prossimo
        # POST /login da Laravel ri-autentica invece di rispondere "già autenticato".
        self.authenticated = False
        return response
    if response.success:
        try:
            page = self.auth_page
            btn = page.locator('input[name="visuraImm"]')
            if await btn.count() > 0:
                print("[PDF] Clicco 'Visura Per Immobile'...")
                await btn.click()
                await page.wait_for_load_state("domcontentloaded", timeout=30000)
                print(f"[PDF] Pagina scelta visura: {page.url}")

                ok = await _risolvi_captcha_e_inoltra(page)
                if ok:
                    # Attende che la pagina aggiorni da sola (meta-refresh o JS del portale)
                    print("[PDF] Attendo elaborazione documento da parte del portale...")
                    try:
                        await page.wait_for_function(
                            "!document.body.innerText.toLowerCase().includes('attendere elaborazione in corso')",
                            timeout=90000,
                        )
                    except Exception:
                        print("[PDF] Timeout: elaborazione non completata entro 90s.")

                    content = await page.content()
                    if "attendere elaborazione in corso" in content.lower():
                        print("[PDF] Documento non pronto, salto generazione PDF.")
                    elif "documento" in content.lower() and "pronto" in content.lower():
                        salva_btn = page.locator('input[value="Salva"], button:has-text("Salva"), a:has-text("Salva")')
                        if await salva_btn.count() > 0:
                            print("[PDF] Clicco 'Salva' per scaricare il documento ufficiale...")
                            async with page.expect_download(timeout=60000) as download_info:
                                await salva_btn.first.click()
                            download = await download_info.value
                            path = await download.path()
                            with open(path, 'rb') as f:
                                pdf_bytes = f.read()
                            print(f"[PDF] Download completato: {download.suggested_filename} ({len(pdf_bytes)} bytes)")

                            if response.data is None:
                                response.data = {}
                            response.data["pdf_base64"] = base64.b64encode(pdf_bytes).decode()
                            print("[PDF] PDF salvato con successo.")
                        else:
                            print("[PDF] Bottone 'Salva' non trovato sulla pagina.")
        except Exception as e:
            print(f"[PDF] Errore generazione PDF: {e}")
    return response


_main.BrowserManager.esegui_visura = _esegui_visura_con_pdf
print("[RUN] Patch esegui_visura per generazione PDF documento ufficiale.")


# --- Endpoint /login aggiunto all'app FastAPI esistente ---

class LoginPayload(BaseModel):
    username: str
    password: str


@_main.app.post("/login", tags=["auth"])
async def do_login(payload: LoginPayload):
    """Autentica il servizio con le credenziali Sister passate da Laravel."""
    service = _main.visura_service
    if service is None:
        raise HTTPException(status_code=503, detail="Servizio non ancora inizializzato")

    if service.browser_manager.authenticated:
        return {"status": "ok", "authenticated": True, "message": "già autenticato"}

    os.environ["ADE_USERNAME"] = payload.username
    os.environ["ADE_PASSWORD"] = payload.password

    await service.browser_manager.login()
    await service.browser_manager.start_keep_alive()

    return {
        "status": "ok",
        "authenticated": service.browser_manager.authenticated,
    }


# --- Avvio uvicorn ---

import uvicorn  # noqa: E402

uvicorn.run(
    "main:app",
    host="0.0.0.0",
    port=8000,
    workers=1,
    log_level=os.getenv("LOG_LEVEL", "info").lower(),
)
