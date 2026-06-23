"""
Bot che legge i feed RSS di account X (Twitter), prende i post con
immagini/video, traduce il testo in inglese (lascia stare EN e IT) e
pubblica tutto su un canale Telegram.

Configurazione tramite variabili d'ambiente:
  TELEGRAM_TOKEN   -> token del bot (da @BotFather)
  TELEGRAM_CHAT_ID -> id/username del canale (es. @miocanale o -100123...)

Le fonti RSS si mettono nel file feeds.txt (una URL per riga).
Lo stato (post gia' inviati) e' salvato in seen.json.
"""

import json
import os
import re
import sys
import html
import time
import tempfile

import requests
import feedparser
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator

try:
    from langdetect import detect as detect_lang
except Exception:  # pragma: no cover
    detect_lang = None

# ------------------------------------------------------------------ config
TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

FEEDS_FILE = "feeds.txt"
SEEN_FILE = "seen.json"

# quanti post al massimo inviare per ogni esecuzione (anti-spam)
MAX_POSTS_PER_RUN = 15
# lingue che NON vanno tradotte (vengono lasciate cosi' come sono)
KEEP_LANGUAGES = {"en", "it"}
# limite caption Telegram
CAPTION_LIMIT = 1000

API = f"https://api.telegram.org/bot{TOKEN}"

# header browser per scaricare i media (alcuni server rifiutano richieste "nude")
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
}


# ------------------------------------------------------------------ stato
def load_seen():
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data.get("ids", []))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen(seen):
    # teniamo solo gli ultimi 4000 id per non far crescere il file all'infinito
    ids = list(seen)[-4000:]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump({"ids": ids}, f, ensure_ascii=False, indent=0)


def load_feeds():
    try:
        with open(FEEDS_FILE, "r", encoding="utf-8") as f:
            lines = [ln.strip() for ln in f]
    except FileNotFoundError:
        return []
    # ignora righe vuote e commenti (#)
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
        return text  # gia' in inglese o italiano: lascio com'e'

    try:
        result = GoogleTranslator(source="auto", target="en").translate(text)
        # se non c'e' nulla da tradurre (es. solo emoji) ritorna None: teniamo l'originale
        return result or text
    except Exception as e:
        print(f"  [warn] traduzione fallita: {e}")
        return text


# ------------------------------------------------------------------ parsing RSS
def clean_text(raw_html):
    """Estrae il testo leggibile da un blocco HTML."""
    soup = BeautifulSoup(raw_html or "", "html.parser")
    text = soup.get_text(separator=" ")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_media(entry):
    """Trova le URL di immagini e video dentro un post del feed."""
    urls = []

    # 1) enclosures / media_content (campi standard RSS)
    for link in entry.get("links", []):
        if link.get("rel") == "enclosure" and link.get("href"):
            urls.append(link["href"])
    for m in entry.get("media_content", []):
        if m.get("url"):
            urls.append(m["url"])
    for m in entry.get("media_thumbnail", []):
        if m.get("url"):
            urls.append(m["url"])

    # 2) tag <img> e <video> dentro la descrizione HTML
    html_block = entry.get("summary", "") or entry.get("description", "")
    soup = BeautifulSoup(html_block, "html.parser")
    for img in soup.find_all("img"):
        if img.get("src"):
            urls.append(img["src"])
    for vid in soup.find_all("video"):
        if vid.get("src"):
            urls.append(vid["src"])
        for src in vid.find_all("source"):
            if src.get("src"):
                urls.append(src["src"])

    # dedup mantenendo l'ordine
    seen, clean = set(), []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            clean.append(u)
    return clean


def media_type(url):
    low = url.lower().split("?")[0]
    if low.endswith((".mp4", ".m4v", ".mov", ".webm")):
        return "video"
    return "photo"


# ------------------------------------------------------------------ download
def download(url):
    """Scarica un media in un file temporaneo. Ritorna (path, None) o (None, errore)."""
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
    r = requests.post(f"{API}/sendMessage", data={
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "false",
    }, timeout=30)
    return r.ok


def tg_send_single(path, kind, caption):
    method = "sendVideo" if kind == "video" else "sendPhoto"
    field = "video" if kind == "video" else "photo"
    with open(path, "rb") as f:
        r = requests.post(f"{API}/{method}", data={
            "chat_id": CHAT_ID,
            "caption": caption,
            "parse_mode": "HTML",
        }, files={field: f}, timeout=120)
    return r.ok, r.text


def tg_send_group(items, caption):
    """items = lista di (path, kind). Manda un album; caption sul primo."""
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
        r = requests.post(f"{API}/sendMediaGroup", data={
            "chat_id": CHAT_ID,
            "media": json.dumps(media),
        }, files=files, timeout=180)
        return r.ok, r.text
    finally:
        for f in files.values():
            f.close()


# ------------------------------------------------------------------ post
def build_caption(source_name, text_en, link):
    parts = []
    if source_name:
        parts.append(f"<b>{html.escape(source_name)}</b>")
    if text_en:
        parts.append(html.escape(text_en))
    caption = "\n\n".join(parts)
    if len(caption) > CAPTION_LIMIT:
        caption = caption[:CAPTION_LIMIT - 1].rstrip() + "…"
    if link:
        caption += f'\n\n<a href="{html.escape(link)}">🔗 Original on X</a>'
    return caption


def handle_entry(entry, source_name):
    media_urls = extract_media(entry)
    if not media_urls:
        return False  # niente immagini/video -> saltiamo (vogliamo solo media)

    raw = entry.get("summary", "") or entry.get("title", "")
    text = clean_text(raw)
    text_en = translate_to_english(text)
    link = entry.get("link", "")
    caption = build_caption(source_name, text_en, link)

    # scarica i media (max 10, limite album Telegram)
    downloaded = []
    for u in media_urls[:10]:
        path, err = download(u)
        if path:
            downloaded.append((path, media_type(u)))
        else:
            print(f"  [warn] download fallito {u}: {err}")

    ok = False
    try:
        if len(downloaded) == 1:
            path, kind = downloaded[0]
            ok, info = tg_send_single(path, kind, caption)
            if not ok:
                print(f"  [warn] invio media fallito: {info}")
        elif len(downloaded) > 1:
            ok, info = tg_send_group(downloaded, caption)
            if not ok:
                print(f"  [warn] invio album fallito: {info}")

        # se non e' partito nessun media, mando almeno il testo+link
        if not ok:
            ok = tg_send_text(caption)
    finally:
        for path, _ in downloaded:
            try:
                os.remove(path)
            except OSError:
                pass

    return ok


# ------------------------------------------------------------------ main
def main():
    if not TOKEN or not CHAT_ID:
        print("ERRORE: mancano TELEGRAM_TOKEN o TELEGRAM_CHAT_ID.")
        sys.exit(1)

    feeds = load_feeds()
    if not feeds:
        print("ERRORE: feeds.txt e' vuoto. Aggiungi almeno una URL RSS.")
        sys.exit(1)

    seen = load_seen()
    first_run = len(seen) == 0
    sent = 0

    for url in feeds:
        print(f"\n>> Feed: {url}")
        parsed = feedparser.parse(url, request_headers=HTTP_HEADERS)
        if parsed.bozo:
            print(f"  [warn] feed illeggibile: {parsed.bozo_exception}")
        source_name = parsed.feed.get("title", "") if parsed.feed else ""

        # i feed danno il piu' recente per primo: invertiamo per ordine cronologico
        for entry in reversed(parsed.entries):
            uid = entry.get("id") or entry.get("link") or entry.get("title", "")
            if not uid or uid in seen:
                continue

            seen.add(uid)

            # PRIMA esecuzione: NON inondiamo il canale con lo storico,
            # marchiamo tutto come "visto" e ripartiamo puliti dal prossimo giro.
            if first_run:
                continue

            if sent >= MAX_POSTS_PER_RUN:
                continue

            print(f"  -> nuovo post: {uid[:60]}")
            if handle_entry(entry, source_name):
                sent += 1
                time.sleep(2)  # piccola pausa anti rate-limit

    save_seen(seen)
    if first_run:
        print("\nPrima esecuzione: storico marcato come 'visto'. "
              "Dal prossimo giro invio solo i post NUOVI.")
    else:
        print(f"\nFatto. Post inviati: {sent}")


if __name__ == "__main__":
    main()
