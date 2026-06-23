# Bot X → Telegram (aggrega + traduce immagini/video)

Legge i feed di account X, prende i post con **immagini/video**, traduce il testo
in **inglese** (lascia stare i post già in inglese o italiano) e li pubblica su un
**canale Telegram**. Gira gratis su **GitHub Actions**, ogni 5 minuti, senza PC acceso.

---

## Guida passo-passo (parti da zero)

### 1. Crea il bot Telegram
1. Su Telegram apri la chat con **@BotFather**.
2. Scrivi `/newbot`, segui le istruzioni (nome + username che finisce con `bot`).
3. Alla fine ti dà un **TOKEN** tipo `123456:ABC-DEF...`. **Copialo e tienilo da parte.**

### 2. Crea il canale e aggiungi il bot
1. Telegram → nuovo **Canale** (può essere privato).
2. Apri le impostazioni del canale → **Amministratori** → aggiungi il tuo bot come admin
   (deve poter **pubblicare messaggi**).
3. Ti serve il **CHAT ID** del canale:
   - Se il canale è **pubblico** con username: usa `@iltuocanale`.
   - Se è **privato**: posta un messaggio qualsiasi nel canale, poi inoltralo al bot
     **@username_to_id_bot** (o simili) che ti dice l'id; sarà tipo `-1001234567890`.

### 3. Metti i progetti su GitHub
1. Crea un account gratuito su https://github.com
2. Crea un nuovo repository (es. `x-telegram-bot`). **Pubblico** = minuti illimitati gratis.
3. Carica tutti i file di questa cartella nel repo
   (puoi trascinarli nella pagina "Add file → Upload files", oppure usare GitHub Desktop).

### 4. Inserisci i segreti (token e chat id)
Nel repo: **Settings → Secrets and variables → Actions → New repository secret**.
Crea questi due:
- `TELEGRAM_TOKEN` → il token del passo 1
- `TELEGRAM_CHAT_ID` → il chat id del passo 2

> I segreti NON finiscono nel codice: restano nascosti e cifrati su GitHub.

### 5. Scegli gli account X da seguire
Apri **`feeds.txt`** e metti una URL RSS per riga. Formato (vedi sotto come trovarle):
```
https://rsshub.app/twitter/user/nasa
```
Salva.

### 6. Accendi il bot
1. Nel repo vai su **Actions** → se chiede, clicca "I understand... enable workflows".
2. Apri il workflow **x-telegram-bot** → **Run workflow** (parte subito una volta).
   - La **prima** esecuzione NON pubblica nulla: segna lo storico come "già visto",
     così poi pubblica solo i post **nuovi**.
3. Da lì in poi gira da solo ogni 5 minuti. 🎉

---

## Dove trovo le URL RSS degli account X?

⚠️ **Questa è la parte fragile.** X ha chiuso l'accesso gratuito, quindi si usano
servizi-ponte che a volte cambiano o si fermano. Opzioni:

- **RSSHub** — `https://rsshub.app/twitter/user/NOMEACCOUNT`
  (l'istanza pubblica a volte limita; in caso, cerca "RSSHub public instances".)
- **Nitter** — `https://ISTANZA-NITTER/NOMEACCOUNT/rss`
  Cerca "nitter instances list" per trovarne una **attiva** (es. la lista su
  status.d420.de o github.com/zedeus/nitter/wiki/Instances). Sostituisci `ISTANZA-NITTER`.

**Se un feed smette di pubblicare:** apri la URL nel browser. Se non carica più,
cambiala con un'altra istanza attiva e ricarica il file su GitHub. Fine.

---

## Modifiche facili

- **Quanto spesso controlla:** in `.github/workflows/bot.yml`, riga `cron: "*/5 * * * *"`
  (es. `*/15` = ogni 15 min). 5 min è circa il minimo affidabile su GitHub Actions.
- **Max post per giro:** in `main.py`, `MAX_POSTS_PER_RUN`.
- **Lingue da NON tradurre:** in `main.py`, `KEEP_LANGUAGES` (default: inglese e italiano).

## Note
- GitHub disattiva i cron dopo 60 giorni di **inattività** del repo: il bot ricommitta
  `seen.json` ad ogni post, quindi resta attivo da solo finché pubblica.
- Tutto gratis: nessun server, nessuna carta di credito.
