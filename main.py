"""
Bot che legge la MEDIA TAB di account X (via twscrape), raggruppa i THREAD,
classifica/filtra i contenuti (AI Gemini gratuita con fallback a regole),
traduce il testo in inglese (lascia stare EN e IT) e pubblica su un canale
Telegram come album unico, con UNA didascalia per ogni immagine (il testo
del SUO tweet nel thread).

Variabili d'ambiente (GitHub Secrets):
  TELEGRAM_TOKEN    -> token del bot (@BotFather)
  TELEGRAM_CHAT_ID  -> canale (@nome o -100123...)
  X_USERNAME        -> handle account X usa-e-getta (etichetta)
  X_COOKIES         -> "auth_token=XXXX; ct0=YYYY"
  GEMINI_API_KEY    -> (opzionale) chiave Google AI Studio per il filtro AI
  GEMINI_MODEL      -> (opzionale) modello, default "gemini-2.0-flash"

Account da seguire in accounts.txt (uno username per riga). Stato in seen.json.
"""

import os
import re
import sys
import json
import html
import time
import base64
import asyncio
import tempfile
from collections import defaultdict

import requests
from deep_translator import GoogleTranslator
from twscrape import API, gather

try:
    from langdetect import detect as detect_lang
except Exception:  # pragma: no cover
    detect_lang = None

# ------------------------------------------------------------------ config
TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
X_USERNAME = os.environ.get("X_USERNAME", "").strip()
X_COOKIES = os.environ.get("X_COOKIES", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "").strip() or "gemini-2.5-flash-lite"
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.environ.get("GROQ_MODEL", "").strip() or "qwen/qwen3.6-27b"

ACCOUNTS_FILE = "accounts.txt"
SEEN_FILE = "seen.json"
IDS_FILE = "ids.json"   # cache username -> id (evita di risolvere ogni giro)

FETCH_PER_ACCOUNT = 12          # post letti per account ad ogni giro
MAX_POSTS_PER_RUN = 25          # tetto post (album) inviati per esecuzione
ACCOUNTS_PER_RUN = 53           # account processati per run (sharding a rotazione)
CURSOR_KEY = "__cursor__"       # chiave riservata in ids.json per il cursore shard
KEEP_LANGUAGES = {"en", "it"}   # lingue da NON tradurre
CAPTION_LIMIT = 1024            # limite Telegram per didascalia

# --- filtri contenuto ---
FILTER_ENABLED = True
DROP_IF_NOT_VGC = True          # scarta i post non legati al VGC
DROP_CATEGORIES = {"meme"}      # categorie da scartare sempre
DROP_LOW_VALUE = False          # se True, scarta anche i post "value=low"

# etichette mostrate per categoria
CATEGORY_TAGS = {
    "team_report": "📋 Team Report",
    "video": "🎥 Video",
    "tournament_result": "🏆 Result",
    "announcement": "📣 Announcement",
    "discussion": "💬 Discussion",
    "meme": "😂 Meme",
    "other": "",
}

API_URL = f"https://api.telegram.org/bot{TOKEN}"
GEMINI_URL = (f"https://generativelanguage.googleapis.com/v1beta/models/"
              f"{GEMINI_MODEL}:generateContent")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
}


# ------------------------------------------------------------------ stato
def load_seen():
    """Ritorna la lista ORDINATA (vecchio->nuovo) degli id gia' visti."""
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return list(json.load(f).get("ids", []))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_seen(seen_list):
    # mantiene l'ORDINE e tiene gli ultimi (piu' recenti): niente tagli casuali
    ids = seen_list[-8000:]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump({"ids": ids}, f, ensure_ascii=False, indent=0)


def load_ids():
    try:
        with open(IDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_ids(ids):
    with open(IDS_FILE, "w", encoding="utf-8") as f:
        json.dump(ids, f, ensure_ascii=False, indent=0)


def load_accounts():
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            lines = [ln.strip().lstrip("@") for ln in f]
    except FileNotFoundError:
        return []
    return [ln for ln in lines if ln and not ln.startswith("#")]


# ------------------------------------------------------------------ traduzione
def translate_to_english(text):
    text = (text or "").strip()
    if not text:
        return text
    lang = None
    if detect_lang:
        try:
            lang = detect_lang(text)
        except Exception:
            lang = None
    if lang in KEEP_LANGUAGES:
        return text
    try:
        result = GoogleTranslator(source="auto", target="en").translate(text)
        return result or text
    except Exception as e:
        print(f"  [warn] traduzione fallita: {e}")
        return text


def clean_tweet_text(raw):
    text = re.sub(r"https?://t\.co/\S+", "", raw or "")
    return re.sub(r"[ \t]+", " ", text).strip()


# ------------------------------------------------------------------ media
def best_video_url(video):
    mp4 = [v for v in video.variants
           if getattr(v, "contentType", "") == "video/mp4" and getattr(v, "bitrate", None)]
    if mp4:
        return max(mp4, key=lambda v: v.bitrate or 0).url
    for v in video.variants:
        if getattr(v, "url", None):
            return v.url
    return None


def collect_media(tweet):
    out = []
    media = getattr(tweet, "media", None)
    if not media:
        return out
    for photo in getattr(media, "photos", []) or []:
        url = getattr(photo, "url", None)
        if url:
            if "pbs.twimg.com" in url and "name=" not in url:
                url += ("&" if "?" in url else "?") + "name=large"
            out.append((url, "photo"))
    for video in getattr(media, "videos", []) or []:
        url = best_video_url(video)
        if url:
            out.append((url, "video"))
    for gif in getattr(media, "animated", []) or []:
        url = getattr(gif, "videoUrl", None)
        if url:
            out.append((url, "video"))
    return out


def download(url):
    try:
        r = requests.get(url, headers=HTTP_HEADERS, timeout=30, stream=True)
        r.raise_for_status()
        suffix = os.path.splitext(url.split("?")[0])[1] or ".bin"
        fd, path = tempfile.mkstemp(suffix=suffix)
        size = 0
        with os.fdopen(fd, "wb") as f:
            for chunk in r.iter_content(8192):
                size += len(chunk)
                if size > 45 * 1024 * 1024:
                    f.close()
                    os.remove(path)
                    return None, "file troppo grande"
                f.write(chunk)
        return path, None
    except Exception as e:
        return None, str(e)


# ------------------------------------------------------------------ classificazione
VALID_CATEGORIES = set(CATEGORY_TAGS) | {"other"}


def keyword_classify(text):
    """Fallback senza AI: assegna una categoria SOLO con segnali forti e poco
    ambigui; in tutti gli altri casi 'other' (nessun tag mostrato). Non sa
    riconoscere i meme visivi, quindi non filtra: tiene (account gia' curati)."""
    t = (text or "").lower()
    # VIDEO: link a piattaforme o annuncio esplicito di un video
    if re.search(r"youtu\.be|youtube\.com|twitch\.tv|/video/", t) or any(
            k in t for k in ("new video", "video is out", "video out now",
                             "just uploaded", "new vod", "watch my")):
        cat = "video"
    # TEAM REPORT: paste o frasi molto specifiche
    elif "pokepast" in t or any(k in t for k in (
            "team report", "rental code", "rental team", "import this team")):
        cat = "team_report"
    # RISULTATO: solo frasi forti (placement espliciti / vittorie), non parole vaghe
    elif re.search(
            r"top\s?(?:cut|4|8|16|32)\b|"
            r"\bday\s?(?:2|two)\b|"
            r"\b\d+(?:st|nd|rd|th)\s+place\b|"
            r"won (?:the|my|regionals|a regional|the regional|nats|worlds)|"
            r"\bchampion\b|\bfinalist\b|\brunner-?up\b", t):
        cat = "tournament_result"
    # ANNUNCIO: parole specifiche
    elif any(k in t for k in ("announcing", "announce", "now available",
                              "coming soon", "preorder", "pre-order")):
        cat = "announcement"
    else:
        cat = "other"  # incerto -> nessun tag
    return {"vgc_related": True, "category": cat, "value": "medium",
            "reason": "keyword-fallback"}


def _classify_prompt(text):
    return (
        "You classify a social media post from a competitive Pokemon VGC "
        "(Video Game Championships) player. Look at the image and the text.\n"
        "Return ONLY JSON with keys:\n"
        '  "vgc_related": true/false (is it about competitive Pokemon VGC?),\n'
        '  "category": one of '
        '["team_report","video","tournament_result","announcement","discussion","meme","other"],\n'
        '  "value": "high"|"medium"|"low" (added value for a VGC fan),\n'
        '  "reason": short string.\n'
        f"POST TEXT: {text[:1500]!r}"
    )


def _read_image_b64(image_path, max_bytes=3_500_000):
    """Ritorna (base64, mime) o None se assente/troppo grande (limite API immagini)."""
    if not image_path:
        return None
    try:
        if os.path.getsize(image_path) > max_bytes:
            return None
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
    except OSError:
        return None
    mime = "image/png" if image_path.lower().endswith(".png") else "image/jpeg"
    return b64, mime


def _parse_classification(data):
    cat = data.get("category", "other")
    if cat not in VALID_CATEGORIES:
        cat = "other"
    return {"vgc_related": bool(data.get("vgc_related", True)),
            "category": cat,
            "value": data.get("value", "medium"),
            "reason": data.get("reason", "")}


# --- Groq (primario): free tier ampio (~14k/giorno) e con visione ---
GROQ_MIN_INTERVAL = 2.0
_LAST_GROQ = [0.0]


def groq_classify(text, image_path):
    """Classifica con Groq (OpenAI-compatible). Throttle + retry sul 429."""
    content = [{"type": "text", "text": _classify_prompt(text)}]
    img = _read_image_b64(image_path)
    if img:
        b64, mime = img
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"}})
    payload = {"model": GROQ_MODEL,
               "messages": [{"role": "user", "content": content}],
               "temperature": 0,
               "response_format": {"type": "json_object"}}
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    for attempt in range(3):
        wait = GROQ_MIN_INTERVAL - (time.monotonic() - _LAST_GROQ[0])
        if wait > 0:
            time.sleep(wait)
        r = requests.post(GROQ_URL, headers=headers, json=payload, timeout=60)
        _LAST_GROQ[0] = time.monotonic()
        if r.status_code == 429:
            time.sleep(5 * (attempt + 1))
            continue
        r.raise_for_status()
        raw = r.json()["choices"][0]["message"]["content"]
        return _parse_classification(json.loads(raw))
    raise RuntimeError("Groq 429 ripetuto dopo i retry")


# throttle per rispettare il limite/minuto del piano gratis Gemini (~15 req/min)
GEMINI_MIN_INTERVAL = 4.5   # secondi minimi tra due chiamate
_LAST_GEMINI = [0.0]


def gemini_classify(text, image_path):
    """Classifica con Google Gemini (immagine + testo), con throttle e retry sul
    429. Solleva un'eccezione se non riesce (-> il chiamante usa le regole)."""
    parts = [{"text": _classify_prompt(text)}]
    img = _read_image_b64(image_path)
    if img:
        b64, mime = img
        parts.append({"inline_data": {"mime_type": mime, "data": b64}})

    payload = {"contents": [{"parts": parts}],
               "generationConfig": {"response_mime_type": "application/json",
                                    "temperature": 0}}
    for attempt in range(3):
        wait = GEMINI_MIN_INTERVAL - (time.monotonic() - _LAST_GEMINI[0])
        if wait > 0:
            time.sleep(wait)
        r = requests.post(GEMINI_URL, params={"key": GEMINI_API_KEY},
                          json=payload, timeout=60)
        _LAST_GEMINI[0] = time.monotonic()
        if r.status_code == 429:          # rate limit: aspetta e riprova
            time.sleep(12 * (attempt + 1))
            continue
        r.raise_for_status()
        raw = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        return _parse_classification(json.loads(raw))
    raise RuntimeError("Gemini 429 ripetuto dopo i retry")


def classify(text, image_path):
    """Groq (primario) -> Gemini (fallback) -> regole (ultima spiaggia)."""
    if not FILTER_ENABLED:
        return {"vgc_related": True, "category": "other", "value": "high",
                "reason": "filter-off"}
    if GROQ_API_KEY:
        try:
            return groq_classify(text, image_path)
        except Exception as e:
            print(f"  [warn] Groq non disponibile: {e}")
    if GEMINI_API_KEY:
        try:
            return gemini_classify(text, image_path)
        except Exception as e:
            print(f"  [warn] Gemini non disponibile: {e}")
    return keyword_classify(text)


def should_drop(c):
    if DROP_IF_NOT_VGC and not c["vgc_related"]:
        return True, "non-VGC"
    if c["category"] in DROP_CATEGORIES:
        return True, f"categoria {c['category']}"
    if DROP_LOW_VALUE and c.get("value") == "low":
        return True, "low value"
    return False, ""


# ------------------------------------------------------------------ Telegram
def tg_send_text(text):
    r = requests.post(f"{API_URL}/sendMessage", data={
        "chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
    }, timeout=30)
    return r.ok


def tg_send_single(path, kind, caption):
    method = "sendVideo" if kind == "video" else "sendPhoto"
    field = "video" if kind == "video" else "photo"
    with open(path, "rb") as f:
        r = requests.post(f"{API_URL}/{method}", data={
            "chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML",
        }, files={field: f}, timeout=120)
    return r.ok, r.text


def tg_send_group(chunk):
    """chunk = lista di (path, kind, caption). Album con didascalia per item."""
    media, files = [], {}
    for i, (path, kind, caption) in enumerate(chunk):
        key = f"file{i}"
        entry = {"type": kind, "media": f"attach://{key}"}
        if caption:
            entry["caption"] = caption
            entry["parse_mode"] = "HTML"
        media.append(entry)
        files[key] = open(path, "rb")
    try:
        r = requests.post(f"{API_URL}/sendMediaGroup", data={
            "chat_id": CHAT_ID, "media": json.dumps(media),
        }, files=files, timeout=180)
        return r.ok, r.text
    finally:
        for f in files.values():
            f.close()


def send_album(items):
    """items = lista di (path, kind, caption). Spezza in blocchi da 10."""
    ok_any = False
    for start in range(0, len(items), 10):
        chunk = items[start:start + 10]
        if len(chunk) == 1:
            path, kind, caption = chunk[0]
            ok, info = tg_send_single(path, kind, caption)
        else:
            ok, info = tg_send_group(chunk)
        if not ok:
            print(f"  [warn] invio album fallito: {info}")
        ok_any = ok_any or ok
        time.sleep(1)
    return ok_any


def truncate(text, limit=CAPTION_LIMIT):
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def first_caption(username, category, is_thread, lead_text, link):
    header = f"<b>@{html.escape(username)}</b>"
    tag = CATEGORY_TAGS.get(category, "")
    if tag:
        header += f"  {tag}"
    if is_thread:
        header += "  🧵"
    link_html = (f'\n\n<a href="{html.escape(link)}">🔗 Original on X</a>'
                 if link else "")
    # tronca SOLO il corpo del testo, preservando header e link
    body = html.escape(lead_text) if lead_text else ""
    room = CAPTION_LIMIT - len(header) - len(link_html) - 2
    if len(body) > room:
        body = body[:max(0, room - 1)].rstrip() + "…"
    cap = header + ("\n\n" + body if body else "") + link_html
    return cap


# ------------------------------------------------------------------ gruppi/thread
def build_groups(tweets, user_id):
    """Raggruppa i tweet (gia' filtrati a 'non visti') per thread.
    Ritorna lista di liste di tweet, ognuna ordinata dal piu' vecchio al piu' nuovo,
    e le liste ordinate cronologicamente."""
    own = [t for t in tweets if str(getattr(t.user, "id", "")) == str(user_id)]
    by_conv = defaultdict(list)
    for t in own:
        by_conv[str(getattr(t, "conversationId", getattr(t, "id", "")))].append(t)
    groups = []
    for conv_tweets in by_conv.values():
        conv_tweets.sort(key=lambda t: getattr(t, "id", 0))  # vecchio -> nuovo
        groups.append(conv_tweets)
    groups.sort(key=lambda g: getattr(g[0], "id", 0))
    return groups


def process_group(group, username):
    """Scarica i media, classifica una volta, e pubblica UN ALBUM PER TWEET in
    ordine cronologico. Cosi' le immagini e il testo di ogni tweet restano
    insieme e nell'ordine giusto, senza la confusione delle didascalie sparse
    in un unico album. Ritorna True se ha pubblicato qualcosa."""
    is_thread = len(group) > 1

    # per ogni tweet del gruppo: scarica i suoi media e tieni il suo testo
    per_tweet = []     # [(downloaded=[(path, kind), ...], ttext), ...]
    all_paths = []
    for t in group:
        ttext = translate_to_english(clean_tweet_text(getattr(t, "rawContent", "")))
        dl = []
        for url, kind in collect_media(t):
            path, err = download(url)
            if path:
                dl.append((path, kind))
                all_paths.append(path)
            else:
                print(f"  [warn] download fallito {url}: {err}")
        if dl:
            per_tweet.append((dl, ttext))
    if not per_tweet:
        return False

    def cleanup():
        for p in all_paths:
            try:
                os.remove(p)
            except OSError:
                pass

    # classifica UNA volta, sul testo unito del thread + la prima immagine
    combined_text = " \n".join(dict.fromkeys(tt for _, tt in per_tweet if tt)) or \
        translate_to_english(clean_tweet_text(getattr(group[0], "rawContent", "")))
    first_image = next((p for dl, _ in per_tweet for p, k in dl if k == "photo"), None)
    c = classify(combined_text, first_image)
    drop, why = should_drop(c)
    if drop:
        print(f"  -> SCARTATO ({why}; cat={c['category']})")
        cleanup()
        return False

    lead_link = getattr(group[0], "url", "")
    print(f"  -> PUBBLICO ({c['category']}, {len(per_tweet)} tweet, "
          f"{'thread' if is_thread else 'singolo'})")
    ok_any = False
    try:
        for i, (dl, ttext) in enumerate(per_tweet):
            if i == 0:  # primo tweet: header + categoria + link
                cap = first_caption(username, c["category"], is_thread, ttext, lead_link)
            else:
                cap = truncate(html.escape(ttext)) if ttext else ""
            # la didascalia va sulla PRIMA immagine del tweet = didascalia dell'album
            items = [(path, kind, cap if j == 0 else "")
                     for j, (path, kind) in enumerate(dl)]
            if send_album(items):
                ok_any = True
            time.sleep(1)
        return ok_any
    finally:
        cleanup()


# ------------------------------------------------------------------ main
async def run():
    if not (TOKEN and CHAT_ID and X_USERNAME and X_COOKIES):
        print("ERRORE: mancano TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, X_USERNAME o X_COOKIES.")
        sys.exit(1)

    accounts = load_accounts()
    if not accounts:
        print("ERRORE: accounts.txt e' vuoto.")
        sys.exit(1)

    if GROQ_API_KEY:
        prov = f"Groq ({GROQ_MODEL})"
    elif GEMINI_API_KEY:
        prov = f"Gemini ({GEMINI_MODEL})"
    else:
        prov = "OFF -> regole"
    print(f"Filtro AI: {prov}")

    api = API()
    try:
        await api.pool.add_account_cookies(X_USERNAME, X_COOKIES)
    except Exception as e:
        print(f"  [info] add_account_cookies: {e}")

    seen_list = load_seen()
    seen = set(seen_list)
    ids = load_ids()
    first_run = len(seen) == 0

    # --- sharding: ogni run processa solo una fetta di account, a rotazione,
    # con un cursore persistito. Cosi' nessun run satura il rate limit di X,
    # i run restano veloci e la copertura completa avviene ogni pochi giri. ---
    cursor = int(ids.get(CURSOR_KEY, 0) or 0)
    n = len(accounts)
    if n > ACCOUNTS_PER_RUN:
        start = (cursor * ACCOUNTS_PER_RUN) % n
        shard = [accounts[(start + i) % n] for i in range(ACCOUNTS_PER_RUN)]
    else:
        shard = list(accounts)
    ids[CURSOR_KEY] = cursor + 1
    print(f"Account totali: {n} | letti in questo giro: {len(shard)} (cursor {cursor})")

    def mark(t):
        sid = str(getattr(t, "id", ""))
        if sid and sid not in seen:
            seen.add(sid)
            seen_list.append(sid)

    pending = []  # (id piu' recente del gruppo, group, username)

    for username in shard:
        print(f"\n>> @{username}")
        key = username.lower()
        uid = ids.get(key)
        new_account = uid is None
        if new_account:  # mai visto: risolvi l'id e memorizzalo in cache
            try:
                user = await api.user_by_login(username)
            except Exception as e:
                print(f"  [warn] impossibile risolvere @{username}: {e}")
                continue
            if not user:
                print(f"  [warn] @{username} non trovato (o cookie scaduti).")
                continue
            uid = user.id
            ids[key] = uid

        try:
            tweets = await gather(api.user_media(uid, limit=FETCH_PER_ACCOUNT))
        except Exception as e:
            print(f"  [warn] errore lettura media di @{username}: {e}")
            continue

        fresh = [t for t in tweets if str(getattr(t, "id", "")) not in seen]
        if not fresh:
            continue
        for t in fresh:        # marca SEMPRE i nuovi (anche quelli che non pubblico)
            mark(t)

        # Primo giro globale o account nuovo: storico marcato, niente pubblicazione.
        if first_run or new_account:
            if new_account and not first_run:
                print("  (nuovo account: storico marcato, non pubblicato)")
            continue

        for group in build_groups(fresh, uid):
            newest = max(int(getattr(t, "id", 0) or 0) for t in group)
            pending.append((newest, group, username))

    # Pubblica solo i piu' RECENTI fino al tetto; il resto (piu' vecchio) e' gia'
    # marcato visto -> il canale resta attuale e niente ondate di backlog.
    pending.sort(key=lambda x: x[0], reverse=True)
    to_post = pending[:MAX_POSTS_PER_RUN]
    skipped = len(pending) - len(to_post)

    sent = 0
    # invio in ordine cronologico (vecchio->nuovo) cosi' il piu' recente resta in fondo
    for _, group, username in sorted(to_post, key=lambda x: x[0]):
        if process_group(group, username):
            sent += 1
            time.sleep(2)

    save_seen(seen_list)
    save_ids(ids)
    if first_run:
        print("\nPrima esecuzione: storico marcato. Dal prossimo giro solo i NUOVI.")
    else:
        msg = f"\nFatto. Post pubblicati: {sent}"
        if skipped:
            msg += f" | piu' vecchi saltati (gia' marcati visti): {skipped}"
        print(msg)


if __name__ == "__main__":
    asyncio.run(run())
