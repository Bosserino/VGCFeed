"""
Bot che legge la MEDIA TAB di account X (Twitter) tramite twscrape,
prende i post con immagini/video, traduce il testo in inglese (lascia
stare EN e IT) e pubblica tutto su un canale Telegram.

Configurazione tramite variabili d'ambiente (GitHub Secrets):
  TELEGRAM_TOKEN    -> token del bot (da @BotFather)
  TELEGRAM_CHAT_ID  -> id/username del canale (es. @miocanale o -100123...)
  X_USERNAME        -> handle dell'account X usa-e-getta (solo etichetta)
  X_COOKIES         -> "auth_token=XXXX; ct0=YYYY" presi dal browser

Gli account X da seguire si mettono nel file accounts.txt (uno username
per riga, senza @). Lo stato (post gia' inviati) e' in seen.json.
"""

import os
import re
import sys
import json
import html
import time
import asyncio
import tempfile

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

ACCOUNTS_FILE = "accounts.txt"
SEEN_FILE = "seen.json"

# quanti post leggere dalla media tab di ogni account ad ogni giro
FETCH_PER_ACCOUNT = 20
# quanti post al massimo inviare per esecuzione (anti-spam)
MAX_POSTS_PER_RUN = 15
# lingue che NON vanno tradotte
KEEP_LANGUAGES = {"en", "it"}
# limite caption Telegram
CAPTION_LIMIT = 1000

API_URL = f"https://api.telegram.org/bot{TOKEN}"

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
}


# ------------------------------------------------------------------ stato
def load_seen():
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f).get("ids", []))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen(seen):
    ids = list(seen)[-4000:]  # evita crescita infinita del file
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump({"ids": ids}, f, ensure_ascii=False, indent=0)


def load_accounts():
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8-sig") as f:
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
        return text  # gia' in inglese o italiano

    try:
        result = GoogleTranslator(source="auto", target="en").translate(text)
        return result or text  # None (es. solo emoji) -> tieni originale
    except Exception as e:
        print(f"  [warn] traduzione fallita: {e}")
        return text


# ------------------------------------------------------------------ media
def clean_tweet_text(raw):
    """Rimuove i link t.co finali (di solito puntano al media stesso)."""
    text = re.sub(r"https?://t\.co/\S+", "", raw or "")
    return re.sub(r"\s+", " ", text).strip()


def best_video_url(video):
    """Sceglie la variante mp4 col bitrate piu' alto."""
    mp4 = [v for v in video.variants
           if getattr(v, "contentType", "") == "video/mp4" and getattr(v, "bitrate", None)]
    if mp4:
        return max(mp4, key=lambda v: v.bitrate or 0).url
    for v in video.variants:  # fallback
        if getattr(v, "url", None):
            return v.url
    return None


def collect_media(tweet):
    """Ritorna lista di (url, kind) da un Tweet di twscrape."""
    out = []
    media = getattr(tweet, "media", None)
    if not media:
        return out
    for photo in getattr(media, "photos", []) or []:
        url = getattr(photo, "url", None)
        if url:
            # chiede la versione grande dell'immagine
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


# ------------------------------------------------------------------ download
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
                if size > 45 * 1024 * 1024:  # limite Telegram ~50MB
                    f.close()
                    os.remove(path)
                    return None, "file troppo grande"
                f.write(chunk)
        return path, None
    except Exception as e:
        return None, str(e)


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


def tg_send_group(items, caption):
    media, files = [], {}
    for i, (path, kind) in enumerate(items):
        key = f"file{i}"
        entry = {"type": kind, "media": f"attach://{key}"}
        if i == 0:
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


def build_caption(username, text_en, link):
    parts = [f"<b>@{html.escape(username)}</b>"]
    if text_en:
        parts.append(html.escape(text_en))
    caption = "\n\n".join(parts)
    if len(caption) > CAPTION_LIMIT:
        caption = caption[:CAPTION_LIMIT - 1].rstrip() + "…"
    if link:
        caption += f'\n\n<a href="{html.escape(link)}">🔗 Original on X</a>'
    return caption


def send_tweet(tweet, username):
    media = collect_media(tweet)
    if not media:
        return False  # vogliamo solo post con immagini/video

    text_en = translate_to_english(clean_tweet_text(getattr(tweet, "rawContent", "")))
    caption = build_caption(username, text_en, getattr(tweet, "url", ""))

    downloaded = []
    for url, kind in media[:10]:  # album Telegram max 10
        path, err = download(url)
        if path:
            downloaded.append((path, kind))
        else:
            print(f"  [warn] download fallito {url}: {err}")

    ok = False
    try:
        if len(downloaded) == 1:
            ok, info = tg_send_single(*downloaded[0], caption)
        elif len(downloaded) > 1:
            ok, info = tg_send_group(downloaded, caption)
        else:
            info = "nessun media scaricato"
        if not ok:
            print(f"  [warn] invio fallito: {info}")
            ok = tg_send_text(caption)  # almeno testo + link
    finally:
        for path, _ in downloaded:
            try:
                os.remove(path)
            except OSError:
                pass
    return ok


# ------------------------------------------------------------------ main
async def run():
    if not (TOKEN and CHAT_ID and X_USERNAME and X_COOKIES):
        print("ERRORE: mancano TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, X_USERNAME o X_COOKIES.")
        sys.exit(1)

    accounts = load_accounts()
    if not accounts:
        print("ERRORE: accounts.txt e' vuoto. Aggiungi almeno uno username X.")
        sys.exit(1)

    api = API()  # usa accounts.db (ricreato ad ogni run su GitHub)
    try:
        await api.pool.add_account_cookies(X_USERNAME, X_COOKIES)
    except Exception as e:
        print(f"  [info] add_account_cookies: {e}")  # gia' presente: ok

    seen = load_seen()
    first_run = len(seen) == 0
    sent = 0

    for username in accounts:
        print(f"\n>> @{username}")
        try:
            user = await api.user_by_login(username)
        except Exception as e:
            print(f"  [warn] impossibile risolvere @{username}: {e}")
            continue
        if not user:
            print(f"  [warn] @{username} non trovato (o cookie scaduti).")
            continue

        try:
            tweets = await gather(api.user_media(user.id, limit=FETCH_PER_ACCOUNT))
        except Exception as e:
            print(f"  [warn] errore lettura media di @{username}: {e}")
            continue

        # dal piu' vecchio al piu' recente, cosi' arrivano in ordine
        for tweet in reversed(tweets):
            uid = str(getattr(tweet, "id", "")) or getattr(tweet, "url", "")
            if not uid or uid in seen:
                continue
            seen.add(uid)

            if first_run:
                continue  # prima esecuzione: marca lo storico, non spamma
            if sent >= MAX_POSTS_PER_RUN:
                continue

            print(f"  -> nuovo: {getattr(tweet, 'url', uid)}")
            if send_tweet(tweet, username):
                sent += 1
                time.sleep(2)

    save_seen(seen)
    if first_run:
        print("\nPrima esecuzione: storico marcato come 'visto'. "
              "Dal prossimo giro invio solo i post NUOVI.")
    else:
        print(f"\nFatto. Post inviati: {sent}")


if __name__ == "__main__":
    asyncio.run(run())
