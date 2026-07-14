"""
Bot che legge la MEDIA TAB di account X (via twscrape), accumula i THREAD in
un buffer (pending.json) finche' non sono 'assestati' (nessun tweet nuovo da
THREAD_SETTLE_MINUTES, cosi' non escono mai spezzati), classifica/filtra i
contenuti (Groq visione -> Gemini -> regole), traduce in inglese con Groq +
glossario JP->EN dei nomi Pokemon (glossary_ja.json) e pubblica su un canale
Telegram un album per tweet, in ordine, con link X e XCancel.

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
from datetime import datetime, timezone, timedelta

import requests
from deep_translator import GoogleTranslator
from twscrape import API, gather

try:
    from langdetect import detect as detect_lang
except Exception:  # pragma: no cover
    detect_lang = None

# In locale carica le variabili da un file .env (se presente). Su GitHub non
# esiste .env e le variabili arrivano dai Secrets: questo blocco e' un no-op.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # pragma: no cover
    pass

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
PENDING_FILE = "pending.json"     # buffer thread in attesa di 'assestamento'
GLOSSARY_FILE = "glossary_ja.json"  # nomi Pokemon/mosse/item JP -> EN

FETCH_PER_ACCOUNT = 12          # post letti per account ad ogni giro
MAX_POSTS_PER_RUN = 25          # tetto post (album) inviati per esecuzione
ACCOUNTS_PER_RUN = 53           # account processati per run (sharding a rotazione)
CURSOR_KEY = "__cursor__"       # chiave riservata in ids.json per il cursore shard
MAX_POST_AGE_HOURS = 48         # pubblica solo tweet piu' recenti di X ore (anti-vecchi)
THREAD_SETTLE_MINUTES = 45      # pubblica un thread solo se fermo da X minuti
KEEP_LANGUAGES = {"en", "it"}   # lingue da NON tradurre
CAPTION_LIMIT = 1024            # limite Telegram per didascalia

# DRY_RUN=1: fa tutto (scrape, raggruppa, classifica, traduce) ma NON invia a
# Telegram e NON salva lo stato. Per testare senza effetti collaterali.
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in {"1", "true", "yes"}

# --- filtri contenuto ---
FILTER_ENABLED = True
DROP_IF_NOT_VGC = True          # scarta i post non legati al VGC
DROP_CATEGORIES = {"meme", "fanart", "merch", "personal"}  # scartate sempre
DROP_LOW_VALUE = True           # scarta i post "value=low"
NEVER_DROP_BY_VALUE = {"team_report", "tournament_result"}  # mai scartati per value

# etichette mostrate per categoria
CATEGORY_TAGS = {
    "team_report": "📋 Team Report",
    "video": "🎥 Video",
    "tournament_result": "🏆 Result",
    "analysis": "🧠 Analysis",
    "announcement": "📣 Announcement",
    "discussion": "💬 Discussion",
    "meme": "😂 Meme",
    "fanart": "",
    "merch": "",
    "personal": "",
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


def load_pending():
    """Buffer dei thread in attesa: {chiave: {"username": ..., "tweets": [...]}}.
    La chiave e' "username|conversationId" (due autori nello stesso thread non
    devono finire nello stesso album)."""
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_pending(pending):
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(pending, f, ensure_ascii=False, indent=0)


def load_accounts():
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            lines = [ln.strip().lstrip("@") for ln in f]
    except FileNotFoundError:
        return []
    return [ln for ln in lines if ln and not ln.startswith("#")]


# ------------------------------------------------------------------ traduzione
def _load_glossary():
    """Glossario JP->EN (specie, mosse, abilita', item, nature) generato da
    tools/gen_glossary.py. Le chiavi sono gia' ordinate per lunghezza
    decrescente nel file; riordina comunque per sicurezza (longest-first)."""
    try:
        with open(GLOSSARY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # via le chiavi pure-hiragana corte (es. あわ "Bubble"): sono anche
        # parole comuni e, senza spazi in giapponese, corromperebbero il testo
        hira_corta = re.compile(r"^[぀-ゟ]{1,3}$")
        items = [(k, v) for k, v in data.items()
                 if len(k) > 1 and not hira_corta.match(k)]
        return sorted(items, key=lambda kv: (-len(kv[0]), kv[0]))
    except (FileNotFoundError, json.JSONDecodeError):
        print(f"  [warn] glossario {GLOSSARY_FILE} mancante: nomi JP non corretti")
        return []


GLOSSARY = _load_glossary()


def apply_glossary(text):
    """Sostituisce i termini giapponesi col nome inglese ufficiale PRIMA della
    traduzione: i traduttori storpiano i nomi (カイリュー -> 'Kairyu' invece di
    Dragonite). Longest-first per non spezzare i nomi composti."""
    for jp, en in GLOSSARY:
        if jp in text:
            text = text.replace(jp, en)
    return text


def groq_translate(text):
    """Traduzione con Groq: molto piu' naturale di Google Translate e conosce
    il gergo VGC. Solleva eccezione se fallisce (-> fallback Google)."""
    prompt = (
        "Translate this social media post by a competitive Pokemon VGC player "
        "into natural, fluent English. Keep Pokemon names, move/item/ability "
        "names and VGC jargon exactly as-is when already in English. Preserve "
        "line breaks and emoji. Do not add commentary.\n"
        'Return ONLY JSON: {"translation": "..."}\n'
        f"POST: {text[:2000]!r}"
    )
    payload = {"model": GROQ_MODEL,
               "messages": [{"role": "user", "content": prompt}],
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
        out = (json.loads(raw).get("translation") or "").strip()
        if out:
            return out
        raise RuntimeError("traduzione vuota")
    raise RuntimeError("Groq 429 ripetuto dopo i retry")


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
    text = apply_glossary(text)
    if GROQ_API_KEY:
        try:
            return groq_translate(text)
        except Exception as e:
            print(f"  [warn] traduzione Groq fallita: {e}")
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
        "You curate a feed for competitive Pokemon VGC players. They want only "
        "USEFUL competitive information; jokes and fluff are noise. Look at the "
        "image and the text.\n"
        "Return ONLY JSON with keys:\n"
        '  "vgc_related": true/false (is it about competitive Pokemon VGC?),\n'
        '  "category": one of ["team_report","tournament_result","analysis",'
        '"video","announcement","discussion","meme","fanart","merch","personal","other"],\n'
        '  "value": "high"|"medium"|"low",\n'
        '  "reason": short string.\n'
        "Category guide:\n"
        "- team_report: teams, PokePaste, rental codes, EV spreads, damage calcs.\n"
        "- tournament_result: placements, win/loss records, standings, day 2, top cut.\n"
        "- analysis: meta/matchup discussion, usage stats, tech explanations.\n"
        "- announcement: competitively relevant news only (events, rules, formats).\n"
        "- meme: jokes, reaction images, shitposts (even if Pokemon-themed).\n"
        "- fanart: drawings/illustrations. merch: merchandise, plushes, cards for "
        "collecting. personal: food, travel, selfies, life updates.\n"
        'Value guide: "low" = a competitive player learns nothing actionable '
        "from it; \"high\" = teams, results, tech they can use.\n"
        "If torn between a competitive category and a non-competitive one, "
        "choose the competitive one (better to keep than to lose a team report).\n"
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
    # 'low value' non tocca MAI team report e risultati: meglio un falso
    # positivo in canale che perdere un team.
    if (DROP_LOW_VALUE and c.get("value") == "low"
            and c["category"] not in NEVER_DROP_BY_VALUE):
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
    if DRY_RUN:
        print("    [dry-run] album NON inviato:")
        for path, kind, caption in items:
            preview = (caption or "").replace("\n", " ⏎ ")[:200]
            print(f"      - {kind}: {os.path.basename(path)}"
                  + (f" | {preview}" if preview else ""))
        return True
    ok_any = False
    for start in range(0, len(items), 10):
        ok, info = _send_chunk(items[start:start + 10])
        if not ok:
            print(f"  [warn] invio album fallito: {info}")
        ok_any = ok_any or ok
        time.sleep(1)
    return ok_any


def _send_chunk(chunk):
    """Invia un blocco (1..10 media) senza MAI sollevare eccezioni: gestisce
    errori di rete e il flood control di Telegram (429 con retry_after)."""
    for attempt in range(3):
        try:
            if len(chunk) == 1:
                ok, info = tg_send_single(*chunk[0])
            else:
                ok, info = tg_send_group(chunk)
        except requests.RequestException as e:
            ok, info = False, str(e)
            time.sleep(3 * (attempt + 1))
            continue
        if ok:
            return True, info
        retry_after = None
        try:  # Telegram sul 429 dice quanto aspettare
            retry_after = json.loads(info).get("parameters", {}).get("retry_after")
        except Exception:
            pass
        if retry_after:
            time.sleep(min(int(retry_after) + 1, 90))
            continue
        return False, info
    return False, "retry esauriti"


def tg_len(text):
    """Telegram conta didascalie in UTF-16 code unit (emoji/kanji rari = 2)."""
    return len(text.encode("utf-16-le")) // 2


def escape_truncated(text, room):
    """Tronca il testo GREZZO e poi fa l'escape HTML: cosi' non si spezzano
    mai le entita' (&amp;...) e il limite e' contato come lo conta Telegram."""
    text = (text or "").strip()
    if not text or room <= 0:
        return ""
    if tg_len(html.escape(text)) <= room:
        return html.escape(text)
    while text and tg_len(html.escape(text)) > room - 1:
        text = text[:-10]
    return html.escape(text.rstrip()) + "…"


def xcancel_link(link):
    """Stesso tweet su xcancel.com: leggibile senza account/login X."""
    return re.sub(r"^https?://(?:www\.)?(?:x|twitter)\.com/",
                  "https://xcancel.com/", link or "")


def first_caption(username, category, is_thread, lead_text, link):
    header = f"<b>@{html.escape(username)}</b>"
    tag = CATEGORY_TAGS.get(category, "")
    if tag:
        header += f"  {tag}"
    if is_thread:
        header += "  🧵"
    link_html = ""
    if link:
        link_html = (f'\n\n<a href="{html.escape(link)}">🔗 X</a> · '
                     f'<a href="{html.escape(xcancel_link(link))}">🔓 XCancel</a>')
    # tronca SOLO il corpo del testo, preservando header e link
    room = CAPTION_LIMIT - tg_len(header) - tg_len(link_html) - 2
    body = escape_truncated(lead_text, room)
    cap = header + ("\n\n" + body if body else "") + link_html
    return cap


# ------------------------------------------------------------------ gruppi/thread
def tweet_to_dict(t):
    """Serializza di un tweet il minimo che serve per pending.json: cosi' un
    thread puo' aspettare in coda tra un run e l'altro."""
    d = getattr(t, "date", None)
    if d is not None and d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return {
        "id": int(getattr(t, "id", 0) or 0),
        "conv": str(getattr(t, "conversationId", "") or getattr(t, "id", "")),
        "text": clean_tweet_text(getattr(t, "rawContent", "")),
        "date": d.astimezone(timezone.utc).isoformat() if d else None,
        "url": getattr(t, "url", "") or "",
        "media": [[url, kind] for url, kind in collect_media(t)],
    }


def _parse_date(iso):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


def newest_date(tweets):
    dates = [d for d in (_parse_date(t.get("date")) for t in tweets) if d]
    return max(dates) if dates else None


def too_old(tweets):
    """True se anche il tweet piu' recente del gruppo supera MAX_POST_AGE_HOURS:
    il gruppo va scartato (marcato visto), non pubblicato."""
    newest = newest_date(tweets)
    if newest is None:
        return False  # nessuna data: non buttare
    return (datetime.now(timezone.utc) - newest) > timedelta(hours=MAX_POST_AGE_HOURS)


def settled(entry):
    """True se il thread e' 'assestato': l'account e' stato RILETTO (last_fetch)
    almeno THREAD_SETTLE_MINUTES dopo l'ultimo tweet, senza trovare parti nuove.
    Contare dall'orologio non basta: con lo sharding un account viene letto un
    run si' e uno no, e il thread sembrerebbe 'fermo' solo perche' non lo
    stavamo guardando (-> uscirebbe ancora spezzato)."""
    newest = newest_date(entry["tweets"])
    if newest is None:
        return True
    ref = _parse_date(entry.get("last_fetch")) or datetime.now(timezone.utc)
    return (ref - newest) >= timedelta(minutes=THREAD_SETTLE_MINUTES)


def process_group(group, username):
    """group = lista di dict (vedi tweet_to_dict) di UN thread 'assestato'.
    Scarica i media, traduce, classifica UNA volta sul thread intero, e
    pubblica UN ALBUM PER TWEET in ordine cronologico. Cosi' le immagini e il
    testo di ogni tweet restano insieme e nell'ordine giusto.
    Ritorna: "sent" (pubblicato), "dropped" (scartato dal filtro),
    "failed" (invio fallito) o "empty" (nessun media scaricabile);
    su failed/empty il chiamante lo lascia in pending e ritenta."""
    group = sorted(group, key=lambda t: t["id"])  # vecchio -> nuovo
    is_thread = len(group) > 1

    # per ogni tweet del gruppo: scarica i suoi media e traduci il suo testo
    per_tweet = []     # [(downloaded=[(path, kind), ...], ttext), ...]
    all_paths = []
    for t in group:
        dl = []
        for url, kind in t["media"]:
            path, err = download(url)
            if path:
                dl.append((path, kind))
                all_paths.append(path)
            else:
                print(f"  [warn] download fallito {url}: {err}")
        if dl:  # traduci solo se c'e' qualcosa da pubblicare
            per_tweet.append((dl, translate_to_english(t["text"])))
    if not per_tweet:
        return "empty"

    def cleanup():
        for p in all_paths:
            try:
                os.remove(p)
            except OSError:
                pass

    # classifica UNA volta, sul testo unito del thread + la prima immagine:
    # mai piu' frammenti giudicati (e scartati) fuori contesto
    combined_text = " \n".join(dict.fromkeys(tt for _, tt in per_tweet if tt)) or \
        translate_to_english(group[0]["text"])
    first_image = next((p for dl, _ in per_tweet for p, k in dl if k == "photo"), None)
    c = classify(combined_text, first_image)
    drop, why = should_drop(c)
    if drop:
        print(f"  -> SCARTATO ({why}; cat={c['category']}; motivo LLM: {c.get('reason', '')!r})")
        cleanup()
        return "dropped"

    lead_link = group[0]["url"]
    print(f"  -> PUBBLICO ({c['category']}, {len(per_tweet)} tweet, "
          f"{'thread' if is_thread else 'singolo'})")
    ok_any = False
    try:
        for i, (dl, ttext) in enumerate(per_tweet):
            if i == 0:  # primo tweet: header + categoria + link
                cap = first_caption(username, c["category"], is_thread, ttext, lead_link)
            else:
                cap = escape_truncated(ttext, CAPTION_LIMIT)
            # la didascalia va sulla PRIMA immagine del tweet = didascalia dell'album
            items = [(path, kind, cap if j == 0 else "")
                     for j, (path, kind) in enumerate(dl)]
            if send_album(items):
                ok_any = True
            time.sleep(1)
        return "sent" if ok_any else "failed"
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
    if DRY_RUN:
        print("*** DRY RUN: niente invii Telegram, niente stato salvato ***")

    api = API()
    try:
        await api.pool.add_account_cookies(X_USERNAME, X_COOKIES)
    except Exception as e:
        print(f"  [info] add_account_cookies: {e}")

    seen_list = load_seen()
    seen = set(seen_list)
    ids = load_ids()
    pending = load_pending()   # thread in attesa, sopravvive tra i run
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

    def mark(tid):
        sid = str(tid)
        if sid and sid != "0" and sid not in seen:
            seen.add(sid)
            seen_list.append(sid)

    # id gia' in coda pending: non vanno ri-aggiunti ai giri successivi
    buffered = {str(t["id"]) for e in pending.values() for t in e["tweets"]}

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

        # letto ORA con successo (anche se senza novita'): aggiorna last_fetch
        # delle entry pending dell'account — e' il segnale per settled() che
        # abbiamo ricontrollato e il thread non e' cresciuto
        now_iso = datetime.now(timezone.utc).isoformat()
        for e in pending.values():
            if e["username"].lower() == username.lower():
                e["last_fetch"] = now_iso

        own = [t for t in tweets
               if str(getattr(t.user, "id", "")) == str(uid)]
        fresh = [t for t in own
                 if str(getattr(t, "id", "")) not in seen
                 and str(getattr(t, "id", "")) not in buffered]
        if not fresh:
            continue

        # Primo giro globale o account nuovo: storico marcato, niente coda.
        if first_run or new_account:
            for t in fresh:
                mark(getattr(t, "id", ""))
            if new_account and not first_run:
                print("  (nuovo account: storico marcato, non pubblicato)")
            continue

        # In coda (pending), NON ancora marcati visti: un thread resta in
        # attesa finche' non e' completo, anche attraverso piu' run.
        added = 0
        for t in fresh:
            td = tweet_to_dict(t)
            if str(td["id"]) in buffered:   # doppione nello stesso fetch
                continue
            if not td["media"]:   # senza media non e' pubblicabile: marca e via
                mark(td["id"])
                continue
            key2 = f"{username.lower()}|{td['conv']}"
            entry = pending.setdefault(key2, {"username": username, "tweets": []})
            entry["tweets"].append(td)
            buffered.add(str(td["id"]))
            added += 1
        if added:
            print(f"  +{added} tweet in coda (pending)")

    # --- pubblica SOLO i gruppi 'assestati'; scarta i troppo vecchi ---
    ready, waiting = [], 0
    for key2, entry in list(pending.items()):
        tws = entry["tweets"]
        if too_old(tws):
            for t in tws:
                mark(t["id"])
            del pending[key2]
            continue
        if not settled(entry):
            waiting += 1
            continue
        ready.append((max(t["id"] for t in tws), key2))

    # i piu' RECENTI prima fino al tetto; l'eccedenza NON si perde piu':
    # resta in pending per il giro dopo (la guardia 48h fa da valvola)
    ready.sort(reverse=True)
    to_post = ready[:MAX_POSTS_PER_RUN]
    left_over = len(ready) - len(to_post)

    sent = 0
    # invio in ordine cronologico (vecchio->nuovo): il piu' recente resta in fondo
    for _, key2 in sorted(to_post):
        entry = pending[key2]
        outcome = process_group(entry["tweets"], entry["username"])
        if outcome in ("failed", "empty"):
            # NON marcato visto e ancora in pending: si ritenta al giro dopo
            # (la guardia 48h evita retry infiniti)
            print(f"  [warn] gruppo non inviato ({outcome}): resta in coda")
            continue
        del pending[key2]           # 'sent' o 'dropped': chiuso
        for t in entry["tweets"]:
            mark(t["id"])
        if outcome == "sent":
            sent += 1
            time.sleep(2)
        if not DRY_RUN:  # salvataggio incrementale: un crash non ripubblica
            save_seen(seen_list)
            save_pending(pending)

    if not DRY_RUN:
        save_seen(seen_list)
        save_ids(ids)
        save_pending(pending)
    if first_run:
        print("\nPrima esecuzione: storico marcato. Dal prossimo giro solo i NUOVI.")
    else:
        msg = f"\nFatto. Post pubblicati: {sent}"
        if waiting:
            msg += f" | thread in attesa di assestamento: {waiting}"
        if left_over:
            msg += f" | oltre il tetto, restano in coda: {left_over}"
        if DRY_RUN:
            msg += "  [dry-run: nessun invio, stato NON salvato]"
        print(msg)


if __name__ == "__main__":
    asyncio.run(run())
