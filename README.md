# Bot X → Telegram (aggrega + traduce immagini/video dalla *media tab*)

Legge la **media tab** di account X (libreria `twscrape`), prende i post con
**immagini/video**, traduce il testo in **inglese** (lascia stare i post già in
inglese o italiano) e li pubblica su un **canale Telegram**. Gira gratis su
**GitHub Actions**, ogni 5 minuti, senza PC acceso.

> ⚠️ X ha chiuso ogni accesso gratuito anonimo (le istanze Nitter pubbliche sono
> morte o bloccate). Quindi il bot legge X **autenticandosi con un account
> X usa-e-getta**, tramite i suoi cookie. È l'unica via gratuita rimasta.

---

## Guida passo-passo (parti da zero)

### 1. Crea il bot Telegram
1. Su Telegram apri **@BotFather** → scrivi `/newbot` → segui le istruzioni.
2. Ti dà un **TOKEN** tipo `123456:ABC-DEF...`. Copialo da parte.

### 2. Crea il canale e aggiungi il bot
1. Telegram → nuovo **Canale** (anche privato).
2. Impostazioni canale → **Amministratori** → aggiungi il tuo bot (deve poter pubblicare).
3. Trova il **CHAT ID**:
   - canale **pubblico** con username → usa `@iltuocanale`;
   - canale **privato** → posta un messaggio nel canale, inoltralo a **@username_to_id_bot**,
     ti dà un id tipo `-1001234567890`.

### 3. Crea l'account X usa-e-getta e prendi i cookie  ⭐ (la parte nuova)
> **NON usare il tuo account X vero**: c'è il rischio che venga limitato/sospeso.
> Crea un account nuovo apposta.

1. Vai su https://x.com e **registra un nuovo account** (basta una email).
   Consiglio: usalo "normalmente" per qualche minuto (segui qualcuno, ecc.) così
   sembra reale e viene flaggato di meno.
2. Restando **loggato** su x.com nel browser **Chrome**, premi **F12**
   (o tasto destro → *Ispeziona*).
3. In alto scegli la scheda **Application** (o *Applicazione*).
4. Nel menu a sinistra: **Cookies** → clicca su **https://x.com**.
5. Nella tabella cerca due righe e copia il loro **Value**:
   - `auth_token`  → un lungo codice
   - `ct0`         → un altro lungo codice
6. Componi questa stringa (sostituendo i due valori):
   ```
   auth_token=IL_VALORE_DI_auth_token; ct0=IL_VALORE_DI_ct0
   ```
   Questo è il tuo **X_COOKIES**. Segnati anche lo **username** dell'account (senza @).

### 4. Metti i file su GitHub
1. Account gratuito su https://github.com → nuovo repository (**Pubblico** = minuti illimitati).
2. Carica tutti i file di questa cartella ("Add file → Upload files").
   - NON caricare la cartella `.venv` né `accounts.db` (servono solo in locale).

### 5. Inserisci i segreti
Repo → **Settings → Secrets and variables → Actions → New repository secret**. Crea:
- `TELEGRAM_TOKEN`   → token del passo 1
- `TELEGRAM_CHAT_ID` → chat id del passo 2
- `X_USERNAME`       → username dell'account usa-e-getta (senza @)
- `X_COOKIES`        → la stringa `auth_token=...; ct0=...` del passo 3
- `GEMINI_API_KEY`   → **(opzionale)** chiave per il filtro AI (vedi sotto). Se non
  la metti, il bot usa automaticamente le regole a parole chiave.

> I segreti restano cifrati su GitHub: non finiscono mai nel codice.

#### (Opzionale) Chiave Gemini per il filtro AI
1. Vai su https://aistudio.google.com/apikey → **Create API key** (serve un account Google, è gratis).
2. Copia la chiave e mettila nel segreto `GEMINI_API_KEY`.
3. Il bot la userà per **guardare immagine + testo** di ogni post e capire se è VGC,
   che tipo è (team report / video / risultato / annuncio / meme) e scartare i meme
   e i contenuti non-VGC. Se la chiave manca o finisce i limiti gratuiti, ripiega
   da solo sulle regole a parole chiave (più grezze).

### 6. Scegli gli account X da seguire
Apri **`accounts.txt`** e metti **uno username per riga, senza @**. Esempio:
```
UB_SLOW
nasa
```

### 7. Accendi il bot
1. Repo → **Actions** → se richiesto, abilita i workflow.
2. Workflow **x-telegram-bot** → **Run workflow** (parte subito).
   - La **prima** esecuzione NON pubblica nulla: segna lo storico come "già visto",
     poi pubblica solo i post **nuovi**.
3. Da lì gira da solo ogni 5 minuti. 🎉

---

## Manutenzione (cosa aspettarsi)

Costi: **zero per sempre**. Ma trattandosi di un account X "scrapato", ogni tanto serve un ritocco:

- **I cookie scadono** (di solito dopo settimane/mesi). Quando il bot smette di
  pubblicare e nei log vedi *"non trovato (o cookie scaduti)"*, rifai il **passo 3**
  e aggiorna il segreto **X_COOKIES**. 1 minuto.
- **L'account può essere sospeso/limitato.** Se succede, crea un nuovo account
  usa-e-getta e aggiorna `X_USERNAME` + `X_COOKIES`.
- **IP di GitHub:** X a volte blocca gli IP dei datacenter. Se noti che fallisce
  spesso solo su GitHub, la soluzione di riserva è far girare lo stesso bot **sul tuo
  Mac** (IP di casa, bloccato molto meno) — chiedimi e ti preparo lo script.

## Come pubblica (thread e filtri)
- **Thread** = più tweet dello stesso autore in fila: vengono uniti in **un solo album**.
  Ogni immagine porta in didascalia **il testo del suo tweet** (apri la 3ª foto → vedi
  il testo del 3° tweet). Se i media sono più di 10, l'album viene spezzato.
- **Filtro/categoria**: ogni post viene classificato e taggato (`📋 Team Report`,
  `🎥 Video`, `🏆 Result`, `📣 Announcement`, `😂 Meme`…). I meme e i contenuti
  non-VGC vengono scartati.

## Modifiche facili (in `main.py`)
- `FETCH_PER_ACCOUNT` → quanti post leggere per account ad ogni giro
- `MAX_POSTS_PER_RUN` → tetto di post inviati per esecuzione
- `KEEP_LANGUAGES`    → lingue da NON tradurre (default: `en`, `it`)
- `DROP_CATEGORIES`   → categorie da scartare sempre (default: `{"meme"}`)
- `DROP_IF_NOT_VGC`   → scarta i post non-VGC (default: `True`)
- `DROP_LOW_VALUE`    → scarta anche i post a basso valore (default: `False`)
- `DROP_LOW_VALUE` + `NEVER_DROP_BY_VALUE` → scarto dei post a basso valore
  (team report e risultati non vengono MAI scartati per valore)
- `THREAD_SETTLE_MINUTES` → minuti di quiete prima di pubblicare un thread intero
- Frequenza: in `.github/workflows/bot.yml`, riga `cron: "*/5 * * * *"`.

## Trigger esterno (cron-job.org)
Il cron di GitHub su `*/5` è inaffidabile: nella pratica parte ogni 1-2 ore.
Per run davvero frequenti si usa un cron esterno gratuito che chiama
`workflow_dispatch` via API:

1. Su GitHub: *Settings → Developer settings → Fine-grained tokens → Generate
   new token*. Repository: solo questo repo. Permessi: **Actions → Read and
   write**. Copia il token (`github_pat_…`).
2. Su [cron-job.org](https://cron-job.org) (account gratuito): crea un cronjob
   ogni 10 minuti con:
   - URL: `https://api.github.com/repos/<owner>/<repo>/actions/workflows/bot.yml/dispatches`
   - Metodo: `POST`, body: `{"ref":"main"}`
   - Header: `Authorization: Bearer <token>`, `Accept: application/vnd.github+json`,
     `User-Agent: cronjob`
3. Il cron `*/5` nel workflow resta come rete di sicurezza; la `concurrency`
   evita run sovrapposti.

## Test senza effetti collaterali
Dal bottone **Run workflow** su GitHub, spunta **Dry run**: il bot fa tutto
(scrape, thread, classificazione, traduzione) ma non invia nulla a Telegram e
non salva lo stato. In locale: `DRY_RUN=1 python main.py`.

## Nota
GitHub disattiva i cron dopo 60 giorni di **inattività** del repo: il bot
ricommitta `seen.json` ad ogni post, quindi resta attivo finché pubblica.
