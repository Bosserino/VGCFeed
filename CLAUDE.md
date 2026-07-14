# VGCFeed — bot X → Telegram (media VGC)

Contesto per Claude Code. Leggi questo file prima di lavorare sul progetto.

## Cosa fa
Legge la **media tab** di ~106 giocatori competitivi Pokémon VGC su X, traduce il
testo in inglese (lascia EN/IT) con **Groq + glossario JP→EN** dei nomi Pokémon
(`glossary_ja.json`, rigenerabile con `tools/gen_glossary.py`), **classifica/filtra
severamente** con un LLM (Groq visione → Gemini fallback → regole; passa solo ciò
che è utile a un player: team report, risultati, analisi, annunci rilevanti), e
pubblica **immagini/video** su un **canale Telegram** con link X + XCancel. I
**thread** si accumulano in `pending.json` finché "assestati" (nessun tweet nuovo
da `THREAD_SETTLE_MINUTES`) e poi escono **interi**, un album per tweet, in ordine.
Gira **gratis su GitHub Actions** (cron `*/5` + trigger esterno cron-job.org via
`workflow_dispatch`, perché il cron GitHub da solo parte ogni 1-2h), con lo stato
committato nel repo.

## Deploy & run
- **Deploy = `git push` su `main`.** `.github/workflows/bot.yml` gira su cron `*/5`
  e col bottone "Run workflow". Niente più upload manuali dei file.
- **Run locale (test):** serve **Python 3.10+** (twscrape lo richiede; il Python di
  sistema del Mac è 3.9, usa pyenv/uv o python.org). Poi:
  ```
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
  cp .env.example .env   # e riempi i valori
  .venv/bin/python main.py
  ```
  `main.py` carica `.env` da solo (via python-dotenv) se presente; su GitHub le
  variabili arrivano dai Secrets.

## Flusso (main.py)
1. Carica `accounts.txt` (username), `seen.json` (id già pubblicati, **lista
   ordinata**), `ids.json` (cache username→id + chiave `__cursor__` dello shard).
2. **Sharding:** processa `ACCOUNTS_PER_RUN` (53) account per run, a rotazione col
   cursore → copertura completa ogni ~2 run, e ogni run resta sotto il rate limit di X.
3. Per account: risolve l'id (da cache), `user_media(FETCH_PER_ACCOUNT=12)`. Account
   nuovo o primo giro globale → marca "visto" e **non pubblica** (niente backlog).
4. I tweet nuovi (con media) finiscono nel **buffer `pending.json`** raggruppati per
   `username|conversationId`, **senza** essere marcati visti: un thread aspetta lì
   anche attraverso più run finché è "assestato" (`THREAD_SETTLE_MINUTES`, 45 min
   senza tweet nuovi) → mai più thread pubblicati a pezzi. **Guardia data:** i gruppi
   più vecchi di `MAX_POST_AGE_HOURS` (48h) vengono marcati visti e buttati.
5. **Newest-first globale:** pubblica i `MAX_POSTS_PER_RUN` (25) gruppi assestati più
   recenti; l'eccedenza NON si perde: resta in pending per il giro dopo.
6. `process_group`: scarica i media, **traduce** (Groq + `glossary_ja.json`, fallback
   Google Translate), **classifica una volta sul thread intero** (Groq→Gemini→regole),
   scarta meme/fanart/merch/personal/low-value (mai team report e risultati), poi
   invia **un album per tweet** con link X + XCancel sul primo.
7. Salva `seen.json` + `ids.json` + `pending.json` (anche incrementale dopo ogni
   gruppo pubblicato); il workflow li ri-committa (merge `-X ours` + retry).
   Con `DRY_RUN=1` (env o input del bottone Run workflow): niente invii Telegram e
   niente salvataggio stato — modalità test senza effetti collaterali.

## Vincoli & lezioni imparate (NON ri-scoprirle)
- **X/twscrape:** con **UN solo** account non si leggono 106 profili in <5 min — su
  rate limit twscrape **dorme** fino al reset (15 min). Lo **sharding** è la soluzione.
  Aggiungere altri account usa-e-getta al pool scalerebbe di più. Gli IP datacenter
  (GitHub) sono limitati/bloccati da X più aggressivamente di un IP residenziale.
- **LLM:** `gemini-2.0-flash` è stato **spento il 2026-06-01**. I free tier Gemini
  sono minuscoli (2.5-flash ~250 richieste/giorno). Il **primario è Groq**
  (`qwen/qwen3.6-27b`, visione, ~14k/giorno). **I nomi modello Groq cambiano**: se dà
  404, imposta il secret `GROQ_MODEL` col modello visione attuale da
  console.groq.com/docs/vision. Il bot ripiega comunque su Gemini-lite → regole.
- **Stato nel git:** `seen.json`/`ids.json` sono committati ad ogni run. **Non
  sovrascriverli** con versioni vuote (es. clonando + copiando file). Messaggio di
  commit del bot: `update state`.
- **GitHub cron `*/5`** è ballerino (10-30 min di ritardo) ed è gratis solo su repo
  **pubblico**. I cookie X sono nei **Secrets** (non nel repo) → al sicuro.

## Knob di config (in cima a main.py)
`ACCOUNTS_PER_RUN`, `FETCH_PER_ACCOUNT`, `MAX_POSTS_PER_RUN`, `MAX_POST_AGE_HOURS`,
`THREAD_SETTLE_MINUTES`, `KEEP_LANGUAGES`, `DROP_CATEGORIES`, `DROP_IF_NOT_VGC`,
`DROP_LOW_VALUE`, `NEVER_DROP_BY_VALUE`.

## Secrets / variabili d'ambiente
`TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `X_USERNAME`, `X_COOKIES` (`auth_token=..; ct0=..`),
`GROQ_API_KEY`. Opzionali: `GEMINI_API_KEY` (fallback), `GROQ_MODEL`, `GEMINI_MODEL`.
Nel workflow è impostato `TWS_RAISE_WHEN_NO_ACCOUNT=true`.

## Gotcha
- **Mai committare `.env`** (è in `.gitignore`). I cookie X **scadono** (settimane) →
  aggiornare `X_COOKIES`.
- `accounts.txt`: uno username per riga, senza `@`, righe `#` = commenti.
- Commenti nel codice in **italiano**; tenere le **dipendenze al minimo**.
- Se aggiungi molti account in blocco: non fanno backlog (silent-init per-account).

## File principali
`main.py` · `accounts.txt` · `requirements.txt` · `seen.json` · `ids.json` ·
`pending.json` · `glossary_ja.json` · `tools/gen_glossary.py` ·
`.github/workflows/bot.yml` · `README.md` · `.env.example`
