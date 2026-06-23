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

### 5. Inserisci i 4 segreti
Repo → **Settings → Secrets and variables → Actions → New repository secret**. Crea:
- `TELEGRAM_TOKEN`   → token del passo 1
- `TELEGRAM_CHAT_ID` → chat id del passo 2
- `X_USERNAME`       → username dell'account usa-e-getta (senza @)
- `X_COOKIES`        → la stringa `auth_token=...; ct0=...` del passo 3

> I segreti restano cifrati su GitHub: non finiscono mai nel codice.

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

## Modifiche facili (in `main.py`)
- `FETCH_PER_ACCOUNT` → quanti post leggere per account ad ogni giro
- `MAX_POSTS_PER_RUN` → tetto di post inviati per esecuzione
- `KEEP_LANGUAGES`    → lingue da NON tradurre (default: `en`, `it`)
- Frequenza: in `.github/workflows/bot.yml`, riga `cron: "*/5 * * * *"`.

## Nota
GitHub disattiva i cron dopo 60 giorni di **inattività** del repo: il bot
ricommitta `seen.json` ad ogni post, quindi resta attivo finché pubblica.
