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
    """Legge il captcha con EasyOCR e preprocessing adattivo per 4 stili Sister."""
    from PIL import Image, ImageOps, ImageEnhance
    import numpy as np
    import io as _io
    from collections import Counter

    reader = _get_ocr_reader()
    img = Image.open(_io.BytesIO(img_bytes))
    arr = np.array(img.convert('RGB'))

    def read(data):
        try:
            r = reader.readtext(data, detail=0, allowlist='abcdefghijklmnopqrstuvwxyz0123456789')
            return ''.join(r).lower().replace(' ', '')
        except Exception:
            return ''

    def to_png(pil_img):
        buf = _io.BytesIO()
        pil_img.save(buf, 'PNG')
        return buf.getvalue()

    def scale(pil_img, factor=4):
        return pil_img.resize((pil_img.width * factor, pil_img.height * factor), Image.LANCZOS)

    results = []

    # --- Rilevamento stile ---
    brightness = float(arr.mean())
    rg_diff = np.clip(arr[:, :, 0].astype(int) - arr[:, :, 1].astype(int), 0, 255)
    br_diff = np.clip(arr[:, :, 2].astype(int) - arr[:, :, 0].astype(int), 0, 255)
    rb_diff = np.clip(arr[:, :, 0].astype(int) - arr[:, :, 2].astype(int), 0, 255)
    red_frac  = float((rg_diff > 80).mean())
    blue_frac = float((br_diff > 80).mean())
    dark_bg   = brightness < 80  # sfondo nero (testo giallo/chiaro su nero)
    print(f"[OCR] Style detect: brightness={brightness:.1f} red_frac={red_frac:.3f} blue_frac={blue_frac:.3f} dark_bg={dark_bg}")

    # --- Stile 1: sfondo scuro (testo chiaro su nero) → inverto e processo ---
    if dark_bg:
        arr_inv = 255 - arr
        img_inv = Image.fromarray(arr_inv.astype(np.uint8))
        ig_inv = ImageOps.autocontrast(img_inv.convert('L'))
        results.append(('inv+gray+4x', read(to_png(scale(ig_inv)))))
        results.append(('inv+color+4x', read(to_png(scale(img_inv)))))
        ig_sharp = ImageEnhance.Sharpness(ig_inv).enhance(3.0)
        results.append(('inv+sharp+4x', read(to_png(scale(ig_sharp)))))
        # Canale G dopo inversione: testo giallo → inv_G basso, bg nero → inv_G alto
        g_inv = ImageOps.autocontrast(Image.fromarray((255 - arr[:, :, 1]).astype(np.uint8)))
        results.append(('inv+G+4x', read(to_png(scale(g_inv)))))
        print(f"[OCR] Strategie (dark_bg): { {k: v for k, v in results} }")

    # --- Stile 2: testo blu su sfondo chiaro uniforme (blu su azzurro/bianco) ---
    # Isola il testo con canale R invertito: testo blu ha R basso, sfondo chiaro ha R alto
    if blue_frac > 0.05 and not dark_bg:
        r_ch = arr[:, :, 0].astype(np.uint8)
        r_inv = ImageOps.autocontrast(Image.fromarray(255 - r_ch))
        results.append(('inv_R+4x', read(to_png(scale(r_inv)))))
        # Anche G invertito: testo blu ha G basso
        g_ch = arr[:, :, 1].astype(np.uint8)
        g_inv = ImageOps.autocontrast(Image.fromarray(255 - g_ch))
        results.append(('inv_G+4x', read(to_png(scale(g_inv)))))
        # Saturazione HSV: testo colorato puro ha alta saturazione, sfondo uniforme bassa
        try:
            rgb_f = arr / 255.0
            cmax = rgb_f.max(axis=2)
            cmin = rgb_f.min(axis=2)
            sat_arr = np.where(cmax > 0, (cmax - cmin) / cmax, 0)
            sat_ch = ImageOps.autocontrast(Image.fromarray((sat_arr * 255).astype(np.uint8)))
            results.append(('sat+4x', read(to_png(scale(sat_ch)))))
        except Exception:
            pass

    # --- Strategie universali (tutte le immagini) ---
    ig = ImageOps.autocontrast(img.convert('L'))
    results.append(('gray+4x', read(to_png(scale(ig)))))
    ig_sharp = ImageEnhance.Sharpness(ig).enhance(3.0)
    results.append(('sharp+4x', read(to_png(scale(ig_sharp)))))
    results.append(('color+4x', read(to_png(scale(img)))))

    # Canale con la maggiore deviazione standard (massimo contrasto testo/bg)
    best_ch = int(np.argmax([arr[:, :, c].std() for c in range(3)]))
    ich = ImageOps.autocontrast(Image.fromarray(arr[:, :, best_ch]))
    results.append((f'ch{best_ch}+4x', read(to_png(scale(ich)))))

    # Separazione canali differenza: utile per testo colorato su sfondo colorato
    w, h = img.width * 4, img.height * 4
    for name, ch_a, ch_b in [('R-G', 0, 1), ('R-B', 0, 2), ('G-B', 1, 2), ('B-R', 2, 0), ('B-G', 2, 1)]:
        diff = np.clip(arr[:, :, ch_a].astype(int) - arr[:, :, ch_b].astype(int), 0, 255).astype(np.uint8)
        diff_img = ImageOps.autocontrast(Image.fromarray(diff)).resize((w, h), Image.LANCZOS)
        results.append((name + '+4x', read(to_png(diff_img))))

    print(f"[OCR] Strategie (all): { {k: v for k, v in results} }")

    results_dict = {k: v for k, v in results}

    def pick(key):
        t = results_dict.get(key, '')
        return t if 5 <= len(t) <= 10 else ''

    # --- Selezione risultato per stile ---

    # Sfondo scuro: priorità alle strategie di inversione
    if dark_bg:
        for key in ('inv+gray+4x', 'inv+sharp+4x', 'inv+G+4x', 'inv+color+4x'):
            t = pick(key)
            if t:
                print(f"[OCR] Stile SCURO → {key}: '{t}'")
                return t

    # Testo blu su sfondo chiaro uniforme
    if blue_frac > 0.05 and not dark_bg:
        for key in ('inv_R+4x', 'inv_G+4x', 'sat+4x', 'B-R+4x', 'B-G+4x'):
            t = pick(key)
            if t:
                print(f"[OCR] Stile BLU-UNIFORME → {key}: '{t}'")
                return t

    # Testo blu su sfondo qualsiasi
    if blue_frac > 0.15:
        for key in ('B-R+4x', 'B-G+4x'):
            t = pick(key)
            if t:
                print(f"[OCR] Stile BLU → {key}: '{t}'")
                return t

    # Testo rosso/arancione su sfondo giallo/verde
    if red_frac > 0.1:
        t = pick('R-G+4x')
        if t:
            print(f"[OCR] Stile ROSSO → R-G: '{t}'")
            return t

    # Maggioranza su tutti i risultati validi
    validi = [t for _, t in results if 5 <= len(t) <= 10]
    if validi:
        counter = Counter(validi)
        best_t, count = counter.most_common(1)[0]
        print(f"[OCR] Maggioranza: '{best_t}' ({count} voti)")
        return best_t

    # Fallback: risultato più lungo
    if results:
        fallback = max(results, key=lambda x: len(x[1]))
        print(f"[OCR] Fallback '{fallback[1]}' da '{fallback[0]}'")
        return fallback[1]

    return ''


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


# --- Patch esegui_visura_soggetto: aggiunge PDF al termine del flusso soggetto ---
# utils.py setta pdf_form_ready=True quando naviga al form captcha/PDF di Sister
# (cliccando "Visura per Soggetto" nel form omonimi). Se invece siamo ancora sulla
# lista HTML (caso nazionale senza bottone PDF), proviamo a trovare il bottone
# "Visura per Soggetto" sulla pagina risultati.

async def _risolvi_captcha_e_inoltra_soggetto(page, max_tentativi: int = 20) -> bool:
    """Come _risolvi_captcha_e_inoltra ma per il form visura soggetto.

    Il form soggetto non ha il radio ``intestati``: setta solo ``tipoDocFornitura=PDF``
    se presente, poi clicca ``inoltra``.
    """
    for tentativo in range(max_tentativi):
        captcha_el = page.locator('#imgCaptcha')
        ha_captcha = await captcha_el.count() > 0

        if ha_captcha:
            await page.wait_for_function(
                "document.querySelector('#imgCaptcha') && document.querySelector('#imgCaptcha').complete && document.querySelector('#imgCaptcha').naturalWidth > 0",
                timeout=10000,
            )
            await asyncio.sleep(0.3)
            img_bytes = await captcha_el.screenshot()

            import os as _os
            debug_dir = "/app/logs/captcha_debug"
            _os.makedirs(debug_dir, exist_ok=True)
            with open(f"{debug_dir}/captcha_soggetto_{tentativo+1}_raw.png", "wb") as f:
                f.write(img_bytes)

            testo = _leggi_captcha(img_bytes)
            if len(testo) < 4:
                print(f"[PDF_SOG] OCR insufficiente ('{testo}'), ricarico captcha...")
                await page.locator('a[onclick*="reloadImg"]').click()
                await asyncio.sleep(1.5)
                continue
            await page.locator('#inCaptchaChars').fill(testo)

        # tipoDocFornitura=PDF se presente (opzionale per soggetto)
        try:
            tipo_loc = page.locator('input[type="radio"][name="tipoDocFornitura"][value="PDF"]')
            if await tipo_loc.count() > 0:
                await tipo_loc.check(timeout=2000)
        except Exception:
            pass

        await page.locator('input[name="inoltra"]').click()
        await page.wait_for_load_state("domcontentloaded", timeout=30000)
        print(f"[PDF_SOG] Dopo inoltra (tentativo {tentativo+1}): {page.url}")

        content = await page.content()
        if "inCaptchaChars" in content or "Codice di sicurezza" in content:
            print("[PDF_SOG] Captcha errato, ricarico e riprovo...")
            await page.locator('a[onclick*="reloadImg"]').click()
            await asyncio.sleep(1)
            continue

        return True

    print("[PDF_SOG] Form non inviato dopo tutti i tentativi.")
    return False


_orig_esegui_visura_soggetto = _main.BrowserManager.esegui_visura_soggetto


async def _esegui_visura_soggetto_con_pdf(self, request):
    response = await _orig_esegui_visura_soggetto(self, request)
    if not response.success:
        self.authenticated = False
        return response

    # Omonimi non ancora selezionati: niente PDF ora
    if response.data and response.data.get('omonimi_required'):
        return response

    try:
        page = self.auth_page
        data = response.data or {}
        already_on_form = data.get('pdf_form_ready', False)

        if not already_on_form:
            # Pagina risultati HTML: cerca bottone "Visura per Soggetto"
            btn = page.locator('input[value="Visura per Soggetto"]')
            if await btn.count() > 0:
                print("[PDF_SOG] Clicco 'Visura per Soggetto' dalla pagina risultati...")
                await btn.first.click()
                await page.wait_for_load_state("domcontentloaded", timeout=30000)
                already_on_form = True

        if already_on_form:
            ok = await _risolvi_captcha_e_inoltra_soggetto(page)
            if ok:
                print("[PDF_SOG] Attendo elaborazione documento...")
                try:
                    await page.wait_for_function(
                        "!document.body.innerText.toLowerCase().includes('attendere elaborazione in corso')",
                        timeout=90000,
                    )
                except Exception:
                    print("[PDF_SOG] Timeout elaborazione.")

                content = await page.content()
                if "attendere elaborazione in corso" in content.lower():
                    print("[PDF_SOG] Documento non pronto, salto PDF.")
                elif "documento" in content.lower() and "pronto" in content.lower():
                    salva_btn = page.locator('input[value="Salva"], button:has-text("Salva"), a:has-text("Salva")')
                    if await salva_btn.count() > 0:
                        print("[PDF_SOG] Clicco 'Salva' per scaricare il documento...")
                        async with page.expect_download(timeout=60000) as download_info:
                            await salva_btn.first.click()
                        download = await download_info.value
                        path = await download.path()
                        with open(path, 'rb') as f:
                            pdf_bytes = f.read()
                        print(f"[PDF_SOG] Download completato: {download.suggested_filename} ({len(pdf_bytes)} bytes)")
                        if response.data is None:
                            response.data = {}
                        response.data["pdf_base64"] = base64.b64encode(pdf_bytes).decode()
                        response.data.pop('pdf_form_ready', None)
                        print("[PDF_SOG] PDF soggetto salvato.")
                    else:
                        print("[PDF_SOG] Bottone 'Salva' non trovato.")
        else:
            print("[PDF_SOG] Nessun bottone PDF trovato sulla pagina risultati.")
    except Exception as e:
        print(f"[PDF_SOG] Errore generazione PDF soggetto: {e}")

    return response


_main.BrowserManager.esegui_visura_soggetto = _esegui_visura_soggetto_con_pdf
print("[RUN] Patch esegui_visura_soggetto per generazione PDF documento ufficiale.")


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
