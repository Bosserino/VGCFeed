# VGCFeed — bot X → Telegram (media VGC)

Contesto per Claude Code. Leggi questo file prima di lavorare sul progetto.

## Cosa fa
Legge la **media tab** di ~106 giocatori competitivi Pokémon VGC su X, traduce il
testo in inglese (lascia EN/IT), **classifica/filtra** con un LLM (Groq visione →
Gemini fallback → regole a parole chiave), e pubblica **immagini/video** su un
**canale Telegram**. I **thread** vengono pubblicati come **un album per tweet**
(immagini + testo del tweet), in ordine. Gira **gratis su GitHub Actions** (cron
`*/5`), con lo stato committato nel repo.

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
4. Raggruppa i tweet in **thread** per `conversationId`. **Guardia data:** solo i
   tweet ≤ `MAX_POST_AGE_HOURS` (48h) sono pubblicabili; i più vecchi vengono marcati
   visti e saltati (evita che vecchi tweet mai marcati sembrino "nuovi").
5. **Newest-first globale:** pubblica i `MAX_POSTS_PER_RUN` (25) gruppi più recenti;
   l'eccedenza più vecchia viene marcata vista (il canale resta attuale).
6. `process_group`: scarica i media, **classifica una volta** (Groq→Gemini→regole),
   scarta non-VGC/meme, poi invia **un album per tweet** (thread splittati per tweet
   per avere ordine e didascalie corretti).
7. Salva `seen.json` + `ids.json`; il workflow li ri-committa (merge `-X ours` + retry).

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
`KEEP_LANGUAGES`, `DROP_CATEGORIES`, `DROP_IF_NOT_VGC`, `DROP_LOW_VALUE`.

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
`.github/workflows/bot.yml` · `README.md` · `.env.example`
