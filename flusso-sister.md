# Flusso Sister — Login, Visura e PDF

Questo documento descrive il flusso completo dall'autenticazione su Sister fino alla restituzione del PDF ufficiale della visura catastale.

---

## Architettura generale

Il servizio gira come container Docker con un browser Chromium headless gestito da Playwright. Il punto di ingresso è `run.py`, che applica una serie di patch a `main.py` prima di avviarlo, personalizzando il comportamento per la modalità Sister.

Le credenziali **non** vengono lette dall'env all'avvio: arrivano via `POST /login` da Laravel a runtime, così il container non contiene credenziali hardcoded.

---

## 1. Avvio e patch

`run.py` si esegue come `CMD` del container. Prima di importare `main`, applica le seguenti patch:

| Patch | Cosa fa |
|---|---|
| `_main.login` | Sostituisce la funzione di login SPID con quella Sister (`sister_auth.login`) |
| `VisuraService.initialize` | Salta il login automatico all'avvio; attende `POST /login` |
| `BrowserManager._ensure_authenticated` | Usa il flag `authenticated` invece di navigare a `SceltaServizio.do` (che causava falsi negativi) |
| `BrowserManager._perform_session_refresh` | Il keep-alive leggero (mouse movement) è sufficiente; non naviga per non interferire con visure in corso |
| `BrowserManager.esegui_visura` | Dopo la visura standard, aggiunge il flusso di richiesta e download del PDF ufficiale |

---

## 2. Login Sister (`sister_auth.py`)

Il login avviene su `iampe.agenziaentrate.gov.it` tramite la tab "Sister" (credenziali dirette, non SPID).

### Sequenza

1. **Chiusura sessioni residue** — naviga a `CloseSessions` per invalidare eventuali sessioni aperte da crash o riavvii precedenti.
2. **Login form** — naviga a `iampe.agenziaentrate.gov.it/sam/UI/Login`, clicca il tab `#tab-5` (Sister), inserisce username e password, clicca "Accedi".
3. **Attesa redirect** — aspetta che l'URL diventi `sister3.agenziaentrate.gov.it`.
4. **Controllo sessione bloccata** — se la pagina contiene `"Utente gia' in sessione"`, lancia `_SessoneBloccataError`.
5. **Navigazione interna Sister** — segue la sidebar: Conferma → Consultazioni e Certificazioni → Visure catastali → Conferma Lettura → eventuale selezione convenzione.

### Gestione sessione bloccata

Sister consente una sola sessione attiva per utente. Se il login fallisce con "Utente già in sessione":
- Clicca `CloseSessions` fino a 3 volte
- Cancella i cookie SSO (`page.context.clear_cookies()`) — senza questo, iampe ri-autentica silenziosamente creando subito un'altra sessione bloccata
- Attende 45 secondi (Sister necessita tempo per invalidare lato server)
- Riprova, fino a 5 tentativi totali

### Endpoint `/login`

```
POST /login
{ "username": "...", "password": "..." }
```

Laravel chiama questo endpoint all'avvio della sessione. Il servizio salva le credenziali nelle variabili d'ambiente, esegue il login e avvia il keep-alive.

---

## 3. Richiesta visura (`main.py` → `esegui_visura`)

Una volta autenticati, Laravel invia:

```
POST /visura
{ "provincia": "Viterbo", "comune": "VITERBO", "foglio": "249", "particella": "273", "tipo_catasto": "F" }
```

Il browser naviga a `SceltaServizio.do`, seleziona provincia/comune, tipo catasto, foglio e particella, clicca Ricerca. Il risultato (tabella immobili) viene estratto e restituito come JSON.

---

## 4. Flusso PDF del documento ufficiale (`run.py`)

Questo flusso viene eseguito **dopo** la visura standard, sulla stessa pagina dei risultati.

### 4.1 Richiesta documento

- Clicca il bottone `input[name="visuraImm"]` ("Visura Per Immobile") presente nella pagina risultati.
- Il portale naviga a `SceltaVisuraImmSoggIMM.do`.

### 4.2 Compilazione form e captcha

La funzione `_risolvi_captcha_e_inoltra` gestisce il form con un loop fino a 12 tentativi.

**Se il captcha è presente (`#imgCaptcha`):**

1. Attende che l'immagine sia completamente caricata (controlla `.complete` e `.naturalWidth > 0`).
2. Fa uno screenshot dell'elemento captcha e lo salva in `/app/logs/captcha_debug/` per debug.
3. Chiama `_leggi_captcha()` (vedi sezione 5).
4. Se il testo OCR è < 4 caratteri, ricarica il captcha e riprova senza consumare un tentativo di submit.
5. Inserisce il testo nel campo `#inCaptchaChars`.

**Sempre (con o senza captcha):**

6. Seleziona i radio button: `intestati=1` e `tipoDocFornitura=PDF`.
7. Clicca `input[name="inoltra"]`.
8. Se la risposta contiene ancora "Codice di sicurezza" → captcha sbagliato, ricarica e riprova.
9. Se la risposta non contiene errori captcha → form inviato, ritorna `True`.

### 4.3 Attesa elaborazione

Il portale elabora la richiesta in modo asincrono. Il codice attende (fino a 90 secondi) che la pagina non contenga più il testo `"Attendere elaborazione in corso..."` usando `page.wait_for_function`.

### 4.4 Download PDF

Quando la pagina contiene `"documento"` e `"pronto"` (pagina `CheckRichiesta`):

- Cerca il bottone `"Salva"`.
- Usa `page.expect_download()` per intercettare il download avviato dal click.
- Legge i bytes del file dal path temporaneo di Playwright.
- Codifica in base64 e aggiunge a `response.data["pdf_base64"]`.

Il PDF scaricato è il documento ufficiale (`DOC_XXXXXXXXXX.pdf`) generato dall'Agenzia delle Entrate.

---

## 5. Risoluzione captcha (`_leggi_captcha`)

Il portale Sister usa captcha di testo con almeno 4 stili grafici diversi, tutti 200×101 pixel.

| Stile | Caratteristiche | Strategia efficace |
|---|---|---|
| Rosso su verde | Testo rosso, sfondo verde con rumore | R-G (sottrazione canali) |
| Blu su giallo | Testo blu/ciano, sfondo giallo | B-R (sottrazione canali) |
| Bianco su blu | Testo bianco outline, sfondo blu pieno | sharp+gray+3x |
| Testo scuro su giallo/chiaro | Font gotico/decorativo | maggioranza strategie |

### Pipeline OCR

Per ogni captcha vengono generate **7 varianti** dell'immagine, ognuna passata a EasyOCR:

1. `gray+3x` — scala di grigi + autocontrast + upscale 3×
2. `sharp+gray+3x` — stessa cosa ma con sharpening (fattore 3.0) applicato prima
3. `ch_best+3x` — canale RGB con varianza più alta + autocontrast + upscale 3×
4. `color+3x` — immagine originale a colori + upscale 3×
5. `R-G+3x` — sottrazione R−G, clip [0,255] + autocontrast + upscale 3×
6. `R-B+3x` — sottrazione R−B
7. `G-B+3x` — sottrazione G−B

EasyOCR è inizializzato **una sola volta** all'avvio del processo (`_easyocr_reader` globale) per evitare il caricamento dei modelli ad ogni captcha.

### Rilevamento stile e selezione risultato

Dopo aver raccolto i 7 risultati, il codice identifica lo stile del captcha misurando la frazione di pixel "dominanti":

```
red_frac  = frazione pixel con R−G > 80   →  testo rosso su verde
blue_frac = frazione pixel con B−R > 80   →  testo blu su sfondo chiaro
```

**Logica di selezione:**

1. Se `red_frac > 0.1` → usa il risultato di `R-G+3x` (accuratezza ~100% su questo stile)
2. Se `blue_frac > 0.15` → usa `B-R+3x`, poi `B-G+3x`
3. Altrimenti → sceglie il risultato più **frequente** tra le 7 strategie (maggioranza)
4. Fallback finale → risultato più lungo disponibile

Se nessuna strategia produce un risultato di lunghezza valida (5–10 caratteri), il captcha viene ricaricato immediatamente senza sprecare un tentativo di submit.

Con 12 tentativi massimi e la rotazione casuale degli stili, la probabilità statistica di incontrare almeno un captcha stile rosso/verde (risolto al 100%) è ~97%.

---

## 6. Risposta finale a Laravel

La risposta JSON della `POST /visura` include, in caso di successo:

```json
{
  "success": true,
  "data": {
    "immobili": [ ... ],
    "pdf_base64": "<PDF del documento ufficiale in base64>"
  }
}
```

`pdf_base64` contiene il PDF ufficiale (`DOC_XXXXXXXXXX.pdf`) scaricato direttamente dal portale Sister tramite il bottone "Salva". Se il download del PDF fallisce (es. tutti i tentativi captcha esauriti), il campo non è presente ma la visura JSON è comunque restituita.
