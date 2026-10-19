#!/usr/bin/env python3
"""MusicDost backend — Render-ready version.

Code gate -> search -> play/download.
Env vars:
  APP_CODE    - access code (required)
  TG_BOT_TOKEN - Telegram bot token for getFile (optional, for channel cache)
  PORT        - default 8080 (Render sets this)
  HOST        - default 0.0.0.0
  MEDIA_DIR   - where MP3s are cached (ephemeral on Render free tier)
  SECRET_KEY  - Flask session secret

Om ka standing architecture principle (2026-10-19):
  *** Telegram channel PRIMARY source hai — use perfect banana hai. ***
  JioSaavn/YouTube SIRF fetchers hain (Telegram ko populate karne ke liye),
  primary playback source NAHI. Har gaana jo kabhi bajta hai wo background me
  channel me cache hota hai; channel ka har entry CORRECT hona chahiye.
  Playback hamesha pehle Telegram se, fallback hi Saavn/YT.
"""
import hashlib
import json
import os
import re
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dl_engine

from flask import Flask, request, session, jsonify, Response, send_from_directory

APP_CODE = os.environ.get("APP_CODE", "")
# Deploy signal: /api/version (no auth) isse batata hai kaunsa code live hai.
BACKEND_VERSION = "2026-10-19.2-channel-source"
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", "8080"))
HOST = os.environ.get("HOST", "0.0.0.0")
MEDIA_DIR = os.environ.get("MEDIA_DIR", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "media"))
SECRET_KEY = os.environ.get("SECRET_KEY", os.urandom(32).hex())
BASE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE, "web")
DATA_DIR = os.environ.get("DATA_DIR", BASE)
PLAYLISTS_FILE = os.path.join(DATA_DIR, "playlists.json")
LIKED_FILE = os.path.join(DATA_DIR, "liked.json")

# In-memory rate limiting (free-tier protection; fail-open on any error)
_RL = {}


def _rate_ok(key, limit, window=60.0):
    """Sliding-window rate limit. Koi error ho to allow (kabhi block mat karo)."""
    try:
        import time
        now = time.time()
        ts = _RL.get(key)
        if ts is None:
            ts = []
            _RL[key] = ts
        while ts and ts[0] <= now - window:
            ts.pop(0)
        if len(ts) >= limit:
            return False
        ts.append(now)
        return True
    except Exception:
        return True


_app_tokens = {}

# Trending charts (JioSaavn playlist IDs, bot wale hi)
TREND_CHARTS = {
    "hindi": ("Hindi Top 50", "1134543272"),
    "english": ("English Top 50", "itunes:us"),  # iTunes via dl_engine
    "punjabi": ("Punjabi Top 50", "1134543511"),
    "haryanvi": ("Haryanvi Top 50", "1134770917"),
    "bhojpuri": ("Bhojpuri Top 50", "1134768973"),
    "bhakti": ("Bhakti Bhajan", "1296588511"),
}
_trend_cache = {}


def _load_json_file(path, default):
    try:
        with open(path) as f:
            d = json.load(f)
            return d if isinstance(d, type(default)) else default
    except (OSError, ValueError):
        return default


def _save_json_file(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)

os.makedirs(MEDIA_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = SECRET_KEY
# Code ek baar dalo, 90 din tak yaad rahe
from datetime import timedelta
app.permanent_session_lifetime = timedelta(days=90)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")


def safe_key(key):
    s = re.sub(r"\s+", " ", key.strip().lower())
    s = re.sub(r"[^a-z0-9 _\-()\[\]]+", "", s)
    s = re.sub(r"\s+", "_", s).strip("_")
    if not s:
        s = hashlib.md5(key.encode()).hexdigest()
    return s[:120]


TG_CACHE_FILE = os.path.join(BASE, "tg_cache.json")
# GitHub se fresh cache lao (deploy ka wait mat karo)
TG_CACHE_URL = "https://raw.githubusercontent.com/omeehathnoda/musicdost-backend/main/tg_cache.json"
_tg_cache_time = 0

def _refresh_tg_cache():
    """GitHub se latest tg_cache.json lao (1 ghante me ek baar)."""
    global _tg_cache, _tg_cache_time
    import time
    now = time.time()
    if _tg_cache is not None and now - _tg_cache_time < 3600:
        return
    try:
        import urllib.request, json
        req = urllib.request.Request(TG_CACHE_URL,
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
            if isinstance(data, dict) and len(data) > 100:
                _tg_cache = data
                _tg_cache_time = now
                # Disk par bhi save karo
                try:
                    with open(TG_CACHE_FILE, "w") as f:
                        json.dump(data, f)
                except Exception:
                    pass
                return
    except Exception:
        pass
    # GitHub fail ho to local file
    if _tg_cache is None:
        _tg_cache = _load_json_file(TG_CACHE_FILE, {})
        _tg_cache_time = now
_tg_cache = None

# v2.6.2: async report jobs — /api/report turant return karta hai, asli
# kaam (download + channel upload) background thread me hota hai.
# key -> {"status": "processing"|"done"|"failed", "stream_url": ..., "error": ...}
_report_jobs = {}
_report_jobs_lock = None  # lazy threading.Lock


def _report_lock():
    global _report_jobs_lock
    if _report_jobs_lock is None:
        import threading
        _report_jobs_lock = threading.Lock()
    return _report_jobs_lock

def tg_cache_lookup(title, artist):
    """Telegram channel me file_id dhoondo. Returns file_id or None.

    PRIMARY SOURCE (Om 2026-10-19): playback hamesha pehle yahan se —
    JioSaavn/YouTube sirf fallback fetchers hain."""
    global _tg_cache
    _refresh_tg_cache()
    if _tg_cache is None:
        _tg_cache = _load_json_file(TG_CACHE_FILE, {})
    key = (title + "|" + artist).lower()
    if key in _tg_cache:
        v = _tg_cache[key]
        # v2.6.3: naya format {"file_id","message_id"} ho sakta hai
        if isinstance(v, dict):
            return v.get("file_id")
        return v
    # Smart match: title similar ho to bhi chalega
    import re
    def norm(s):
        return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()
    nt, na = norm(title), norm(artist)
    for k, fid in _tg_cache.items():
        kt, ka = k.split("|", 1) if "|" in k else (k, "")
        if norm(kt) == nt and (not na or na in norm(ka) or norm(ka) in na):
            # v2.6.3: dict format ho to file_id nikalo
            if isinstance(fid, dict):
                return fid.get("file_id")
            return fid
    return None


def tg_download_file(file_id, dest_path):
    """Telegram getFile -> download. Needs TG_BOT_TOKEN."""
    if not TG_BOT_TOKEN:
        return False
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getFile"
        data = json.dumps({"file_id": file_id}).encode()
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            info = json.loads(resp.read())
        fpath = (info.get("result") or {}).get("file_path")
        if not fpath:
            return False
        furl = f"https://api.telegram.org/file/bot{TG_BOT_TOKEN}/{fpath}"
        req = urllib.request.Request(furl, headers={"User-Agent": "Mozilla/5.0"})
        tmp = dest_path + ".part"
        with urllib.request.urlopen(req, timeout=120) as resp, \
                open(tmp, "wb") as f:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
        if os.path.getsize(tmp) < 50 * 1024:
            os.remove(tmp)
            return False
        os.replace(tmp, dest_path)
        return True
    except Exception:
        return False


# ---------------- Telegram = PRIMARY source (Om 2026-10-19) ----------------
# Principle: channel ko perfect banana hai. JioSaavn/YouTube sirf fetchers
# hain — jo gaana channel me nahi hai wo jab bhi bajta hai, background me
# channel me upload hokar bind ho jata hai. Agli baar se instant playback.
TG_CACHE_CHANNEL = "-1003960695955"


def _tg_cache_entry(key):
    """Cache entry se (file_id, message_id) nikalo.

    Naya format: {"file_id": ..., "message_id": ...}; purana format sirf
    file_id string tha (backward compatible — message_id None milega).
    """
    global _tg_cache
    _refresh_tg_cache()
    if _tg_cache is None:
        _tg_cache = _load_json_file(TG_CACHE_FILE, {})
    v = _tg_cache.get(key)
    if isinstance(v, dict):
        return v.get("file_id"), v.get("message_id")
    if isinstance(v, str) and v:
        return v, None
    return None, None


def _tg_cache_bind(key, file_id, message_id=None):
    """file_id (+message_id) ko runtime + disk cache me bind karo."""
    if not file_id:
        return
    global _tg_cache
    try:
        if _tg_cache is None:
            _tg_cache = {}
        _tg_cache[key] = {"file_id": file_id, "message_id": message_id}
        d = _load_json_file(TG_CACHE_FILE, {})
        d[key] = {"file_id": file_id, "message_id": message_id}
        with open(TG_CACHE_FILE, "w") as f:
            json.dump(d, f)
    except Exception:
        pass


def _channel_delete_message(message_id):
    """Best effort: channel se purana message delete karo.

    Bot purane migration messages delete NAHI kar sakta (HTTP 400) — sirf
    apne recent uploads. Fail ho to silently ignore (report flow isko
    best-effort manta hai).
    """
    if not message_id or not TG_BOT_TOKEN:
        return False
    try:
        data = json.dumps({"chat_id": TG_CACHE_CHANNEL,
                           "message_id": message_id}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/deleteMessage",
            data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.load(r)
        return bool((resp or {}).get("ok"))
    except Exception:
        return False


_lazy_inflight = set()
_lazy_lock = None


def _lazy_lock_get():
    global _lazy_lock
    if _lazy_lock is None:
        import threading
        _lazy_lock = threading.Lock()
    return _lazy_lock


def _lazy_channel_cache_async(title, artist):
    """Gaana channel me nahi tha aur Saavn/YT se aaya — background me
    channel me daal do (PRIMARY source principle). User wait nahi karta;
    agli baar se gaana channel se instant bajega.
    Worker _fresh_verified_media ka STRICT pipeline use karta hai."""
    if not title or not TG_BOT_TOKEN:
        return
    key = (title + "|" + artist).lower()
    with _lazy_lock_get():
        if key in _lazy_inflight:
            return
        _lazy_inflight.add(key)
    import threading
    t = threading.Thread(target=_lazy_channel_cache_worker,
                         args=(title, artist, key),
                         daemon=True, name=f"lazycache-{safe_key(key)[:16]}")
    t.start()


def _lazy_channel_cache_worker(title, artist, key):
    """STRICT verified fetch -> channel upload.

    Om ka rule (2026-10-19): galat gaana (wrong artist / alag duration)
    KABHI channel par nahi jana chahiye. Isliye loose ensure_media wali
    file upload nahi karte — _fresh_verified_media ka strict pipeline
    (title saare words + artist partial match + 2.5s duration + junk skip)
    se verified file lao, tabhi upload karo. Strict na mile to channel me
    kuch mat daalo (agli baar phir try hoga).
    """
    try:
        # Beech me kisi aur thread ne bind kar diya ho to skip.
        fid, _ = _tg_cache_entry(key)
        if fid:
            return
        vpath, err = _fresh_verified_media(title, artist)
        if not vpath or not os.path.exists(vpath):
            return
        if os.path.getsize(vpath) < 50 * 1024:
            return
        fid, mid = _channel_replace_upload(vpath, title, artist)
        if fid:
            _tg_cache_bind(key, fid, mid)
    except Exception:
        pass
    finally:
        with _lazy_lock_get():
            _lazy_inflight.discard(key)


def _stream_fetch(title, artist, ytid=None, for_download=False):
    """ensure_media + lazy Telegram caching.

    Pehle dekho gaana channel me hai ya nahi; nahi hai aur Saavn/YT se
    aaya to background me channel me daal do. ytid key-space alag hai —
    us par lazy caching skip (title|artist mapping nahi hai).

    PRIORITY (Om 2026-10-19): play/stream aur DOWNLOAD dono isi fast-track
    se jaate hain — turant background upload, slow queue (30-60s) ka wait
    nahi. Download par user wait kar raha hai, isliye uska channel upload
    sabse pehle. Agar upload fail ho to agli baar bajne/download par phir
    try hoga (self-healing — tab tak channel me nahi hoga).
    """
    was_cached = None
    if not ytid:
        try:
            was_cached, _ = _tg_cache_entry((title + "|" + artist).lower())
        except Exception:
            was_cached = None
    path, err = ensure_media(title, artist, ytid=ytid,
                             for_download=for_download)
    if path and not ytid and not was_cached:
        try:
            _lazy_channel_cache_async(title, artist)
        except Exception:
            pass
    return path, err


# v2.6.3: CENTRAL background cache queue — Om ke final caching rules
# (2026-10-19):
#   ✅ Playlist add ho (user ki ya curated) -> saare gaane queue me (yahi
#      feed karta hai — playlist_resolve se)
#   ✅ Gaana bajta hai (play/stream) -> fast-track (_stream_fetch ->
#      _lazy_channel_cache_async, turant upload, queue ka wait nahi)
#   ✅ User DOWNLOAD karta hai -> turant priority se channel me (wahi
#      fast-track; user wait kar raha hai, 30-60s queue me nahi)
#   ✅ Report se sahi gaana -> channel me (report worker, already)
#   ❌ Sirf search/browse me dikhna -> queue me NAHI (faltu gaane waste hain)
# Ek single worker dheere dheere (har gaane ke beech 30-60s gap) queue ke
# gaane channel me daalta hai: Telegram me hai? nahi to Saavn/YT se lao ->
# sendAudio -> file_id bind.
import collections as _collections
_cache_queue = _collections.deque()
_cache_queued = set()  # dedupe: queue me / process ho rahe keys
_cache_lock = None
_cache_worker_on = False


def _cache_lock_get():
    global _cache_lock
    if _cache_lock is None:
        import threading
        _cache_lock = threading.Lock()
    return _cache_lock


def _cache_enqueue(tracks):
    """[{title, artist}...] ko background queue me daalo (dedupe).
    Worker pehli enqueue par start hota hai, uske baad hamesha chalta hai."""
    if not tracks or not TG_BOT_TOKEN:
        return
    global _cache_worker_on
    with _cache_lock_get():
        for tr in tracks:
            try:
                title = (tr.get("title") or "").strip()
                artist = (tr.get("artist") or "").strip()
            except AttributeError:
                continue
            if not title:
                continue
            key = (title + "|" + artist).lower()
            if key in _cache_queued:
                continue
            _cache_queued.add(key)
            _cache_queue.append((title, artist))
        if not _cache_worker_on:
            _cache_worker_on = True
            import threading
            t = threading.Thread(target=_cache_worker_loop, daemon=True,
                                 name="channel-cache-queue")
            t.start()


def _cache_worker_loop():
    """Single worker: queue se ek-ek gaana nikalo -> channel me hai to
    skip, nahi to Saavn/YT se fetch -> channel me sendAudio -> bind.
    Har gaane ke beech 30-60s ka gap (rate limit / overload se bachne)."""
    import random
    import time
    while True:
        tr = None
        with _cache_lock_get():
            if _cache_queue:
                tr = _cache_queue.popleft()
        if tr is None:
            time.sleep(10)
            continue
        title, artist = tr
        try:
            key = (title + "|" + artist).lower()
            fid, _ = _tg_cache_entry(key)
            if not fid:
                # STRICT pipeline (Om 2026-10-19): title + artist partial
                # match + 2.5s duration — galat gaana channel par kabhi nahi.
                vpath, verr = _fresh_verified_media(title, artist)
                if vpath and os.path.exists(vpath):
                    nfid, nmid = _channel_replace_upload(vpath, title,
                                                         artist)
                    if nfid:
                        _tg_cache_bind(key, nfid, nmid)
        except Exception:
            pass
        finally:
            with _cache_lock_get():
                _cache_queued.discard((title + "|" + artist).lower())
        try:
            time.sleep(random.uniform(30, 60))
        except Exception:
            pass


def ensure_media(title, artist, ytid=None, for_download=False):
    """MP3 lao. Returns (path, err).
    for_download=True: ID3 me title/artist likho taaki downloaded file
    doosre music players me bhi sahi naam/artist dikhaye (Om ka order
    2026-10-18 — 'ye kahne ki baat nahi h'). Stream par tag nahi likhte
    (fast path bana rahe).

    ARCHITECTURE (Om 2026-10-19): Telegram channel PRIMARY source hai.
    Neeche ka order isi principle par hai — pehle Telegram, uske baad hi
    fetchers (JioSaavn, YouTube). JioSaavn/YouTube se aaya gaana _stream_fetch
    background me channel me daal deta hai taaki agli baar seedha channel
    se baje.
    """
    key = (title + "|" + artist).lower()
    if ytid:
        key = "yt:" + ytid
    path = os.path.join(MEDIA_DIR, safe_key(key) + ".mp3")
    if os.path.exists(path) and os.path.getsize(path) > 50 * 1024:
        if for_download:
            try:
                dl_engine.tag_audio_file(path, title, artist)
            except Exception:
                pass
        return path, None
    # Har request ke liye alag tmpdir (concurrent downloads na takraye)
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="dl_", dir=MEDIA_DIR)

    # 0. Seedha YouTube video ID mila ho to wahi se
    if ytid:
        try:
            yp, _ = dl_engine.download_audio(
                f"https://www.youtube.com/watch?v={ytid}", tmpdir)
            if yp and os.path.exists(yp):
                if for_download:
                    try:
                        dl_engine.tag_audio_file(yp, title, artist)
                    except Exception:
                        pass
                try:
                    os.replace(yp, path)
                except OSError:
                    import shutil
                    shutil.copy(yp, path)
                return path, None
        except Exception:
            pass
        return None, "not_found"

    # 1. JioSaavn (agar throttle nahi hai)
    jio_throttled = not dl_engine.jiosaavn_usable()
    if not jio_throttled:
        try:
            hits = dl_engine.jiosaavn_search(f"{title} {artist}", n=5)
            hit = None
            for h in hits:
                if dl_engine.title_matches(f"{title} {artist}",
                                           h.get("title", ""), h.get("artist", "")):
                    hit = h
                    break
            if hit:
                jp, _ = dl_engine.jiosaavn_download(
                    hit["enc"], tmpdir, title=title, artist=artist,
                    image=hit.get("image"),
                    expected_duration=hit.get("duration"))
                if jp and os.path.exists(jp):
                    try:
                        os.replace(jp, path)
                    except OSError:
                        import shutil
                        shutil.copy(jp, path)
                    return path, None
        except Exception:
            pass

    # 2. Telegram cache (JioSaavn throttle ho ya fail ho jaye)
    try:
        fid = tg_cache_lookup(title, artist)
        if fid and tg_download_file(fid, path):
            if for_download:
                try:
                    dl_engine.tag_audio_file(path, title, artist)
                except Exception:
                    pass
            return path, None
    except Exception:
        pass

    # 3. YouTube fallback
    try:
        pick = dl_engine.yt_best_pick(f"{title} {artist} song")
        if not pick:
            return None, "not_found"
        if pick["duration_secs"] > 2700:
            return None, "too_long"
        yp, _ = dl_engine.download_audio(pick["url"], tmpdir)
        if yp and os.path.exists(yp):
            dl_engine.tag_audio_file(yp, title, artist)
            try:
                os.replace(yp, path)
            except OSError:
                import shutil
                shutil.copy(yp, path)
            # Temp dir saaf karo
            try:
                import shutil
                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass
            return path, None
    except Exception:
        pass
    # Temp dir saaf karo (fail case me bhi)
    try:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception:
        pass
    return None, "not_found"


# ---------------- Artist routing (bot parity: 596 curated artists) ----------------
# Bot (dl_listen.py) pehle query ko curated artist list se fuzzy-match karta hai
# (threshold > 0.4). dl_listen ko import nahi karte — logic port kiya gaya hai.
ARTISTS_FILE = os.path.join(BASE, "artists.json")
_artists_data = None

_ARTIST_JUNK = {"topic", "official", "music", "vevo", "records", "label",
                "audio", "video", "channel"}


def load_artists():
    """artists.json -> {industry: {category: [names]}}. Memory me cached."""
    global _artists_data
    if _artists_data is not None:
        return _artists_data
    try:
        with open(ARTISTS_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and d:
            _artists_data = d
            return d
    except (OSError, ValueError):
        pass
    _artists_data = {}
    return {}


def _canon_artist_words(a):
    return {w for w in re.findall(r"[a-z]+", (a or "").lower())
            if w not in _ARTIST_JUNK}


def _artist_fuzzy(query, candidates, top_n=5):
    """Spelling mistakes sambhalo: word-overlap + char similarity.
    'Arjit Singh' -> 'Arijit Singh'. Top N [(name, score)]."""
    qw = _canon_artist_words(query)
    if not qw:
        return []
    qn = dl_engine._norm_name(query)
    scored = []
    for name in candidates:
        nw = _canon_artist_words(name)
        if not nw:
            continue
        inter = len(qw & nw)
        union = len(qw | nw)
        jaccard = inter / union if union else 0
        nn = dl_engine._norm_name(name)
        bonus = 0.0
        if qn == nn:
            bonus = 1.0
        elif qn and nn and (qn in nn or nn in qn):
            bonus = 0.5
        seq = 0.0
        if len(qn) >= 3 and len(nn) >= 3:
            import difflib
            seq = difflib.SequenceMatcher(None, qn, nn).ratio()
        score = jaccard * 0.5 + bonus * 0.3 + seq * 0.2
        if score > 0.15:
            scored.append((name, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_n]


def _all_artists_flat():
    """[(name, industry, category)] — poori curated list."""
    d = load_artists()
    out = []
    for ind, cats in d.items():
        if not isinstance(cats, dict):
            continue
        for cat, names in cats.items():
            for n in names or []:
                out.append((n, ind, cat))
    return out


def artist_route(query):
    """Bot jaisa artist routing: _artist_fuzzy top_n=8, score > 0.4 ->
    {"artists": [{"name","industry","category"}], "query": q} — FULL pick list
    (bot ke art:pick button rows jaisa).
    Koi achha match na mile to None (tab song search)."""
    try:
        flat = _all_artists_flat()
        if not flat:
            return None
        meta = {}
        for n, ind, cat in flat:
            meta.setdefault(n, (ind, cat))
        seen = set()
        picks = []
        for n, s in _artist_fuzzy(query, [n for n, _, _ in flat], top_n=8):
            if s > 0.4 and n not in seen:
                seen.add(n)
                ind, cat = meta.get(n, ("", ""))
                picks.append({"name": n, "industry": ind, "category": cat})
        if not picks:
            return None
        return {"artists": picks, "query": query}
    except Exception:
        return None


# ---------------- Bot-parity iTunes song search ----------------
# Bot ka dl_engine (bin/) render wali copy se NAYA hai: strict title_matches
# (TITLE ka match zaroori — artist-only match kabhi nahi; Om 2026-10-06) aur
# trackTimeMillis se "m:ss" duration. Render wali dl_engine copy purani hai
# (kamzor filter -> Sanam band ke covers, duration nahi). Bot code read-only
# hai, isliye bot ka function yahin port kiya gaya hai.
_BOT_TITLE_JUNK = ("karaoke", "instrumental", "backing track", "minus one")


def _bot_norm_words(s):
    """Lowercase words, bracketed suffixes hatao, repeated letters collapse
    (yaara->yara) — bot ke dl_engine._norm_words jaisa."""
    s = (s or "").lower()
    s = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    words = []
    for w in s.split():
        w = re.sub(r"(.)\1+", r"\1", w)
        if w:
            words.append(w)
    return words


def _bot_title_matches(query, title, artist=""):
    """Bot wala iTunes filter: query ke shabd TITLE me milne chahiye.
    'sanam hai' par Sanam band ke covers bahar, sirf 'O Sanam' andar."""
    tl = (title or "").lower()
    if any(j in tl for j in _BOT_TITLE_JUNK):
        return False
    qw = set(_bot_norm_words(query))
    # Parentheses ka hissa extra info hai — core title lo
    core = re.sub(r"\(.*?\)", "", title or "").strip()
    tw = set(_bot_norm_words(core or title))
    if not qw or not tw:
        return False
    # 4+ akshar wale shabd significant hain ("the", "se" ignore)
    tw_sig = {w for w in tw if len(w) >= 4}
    if not tw_sig:
        tw_sig = tw
    # Core title ke saare significant shabd query me hon -> pakka match
    if tw_sig <= qw:
        return True
    # Ya kam se kam 2 significant shabd milen
    if len(tw_sig & qw) >= min(2, len(tw_sig)):
        return True
    return False


def _bot_web_song_search(query, n=5):
    """Bot ke dl_engine.web_song_search ka exact port: iTunes Search API,
    [{'title','artist','duration'('m:ss')}], strict title filter, max n.
    Empty list on any failure (caller falls back)."""
    try:
        params = urllib.parse.urlencode({
            "term": query, "media": "music", "entity": "song",
            "limit": n * 3, "country": "IN"})
        req = urllib.request.Request(
            "https://itunes.apple.com/search?" + params,
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
    except Exception:
        return []
    out, seen = [], set()
    for t in data.get("results", []):
        title = (t.get("trackName") or "").strip()
        artist = (t.get("artistName") or "").strip()
        if not title:
            continue
        key = (title + "|" + artist).lower()
        if key in seen:
            continue
        seen.add(key)
        if not _bot_title_matches(query, title, artist):
            continue
        dur_s = ""
        try:
            ms = int(t.get("trackTimeMillis") or 0)
            if ms > 0:
                dur_s = f"{ms // 60000}:{(ms // 1000) % 60:02d}"
        except (ValueError, TypeError):
            pass
        # iTunes har track ke saath cover bhejta hai (artworkUrl100);
        # search rows ke liye 600x600 upscale (100x100 blurry lagega).
        art = (t.get("artworkUrl100") or "").strip().replace("100x100", "600x600")
        out.append({"title": title, "artist": artist, "duration": dur_s,
                    "image": art})
        if len(out) >= n:
            break
    return out


def _mss_to_secs(dur):
    """'m:ss' / 'h:mm:ss' / int -> int seconds (iTunes + YouTube durations)."""
    if isinstance(dur, (int, float)):
        return int(dur)
    if dur is None:
        return 0
    m = re.match(r"(?:(\d+):)?(\d+):(\d+)$", str(dur).strip())
    if not m:
        try:
            return int(float(dur))
        except (TypeError, ValueError):
            return 0
    h, mn, s = m.groups()
    return int(h or 0) * 3600 + int(mn) * 60 + int(s)


def _is_cached(title, artist):
    """Bot ka ⚡: tg_cache me (exact key ya smart match) ya MEDIA_DIR me file."""
    try:
        if tg_cache_lookup(title, artist) is not None:
            return True
    except Exception:
        pass
    try:
        p = os.path.join(MEDIA_DIR,
                         safe_key((title + "|" + artist).lower()) + ".mp3")
        return os.path.exists(p) and os.path.getsize(p) > 50 * 1024
    except Exception:
        return False


def send_range(path, download_name=None):
    size = os.path.getsize(path)
    rh = request.headers.get("Range")
    start, end, status = 0, size - 1, 200
    if rh:
        m = re.match(r"bytes=(\d*)-(\d*)", rh.strip())
        if m:
            s, e = m.groups()
            if s:
                start = int(s)
            elif e:
                start = max(0, size - int(e))
            if e:
                end = min(int(e), size - 1)
            if start >= size:
                return Response(status=416,
                                headers={"Content-Range": f"bytes */{size}"})
            status = 206
    length = end - start + 1

    def gen():
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(1024 * 1024, left))
                if not chunk:
                    break
                left -= len(chunk)
                yield chunk

    headers = {"Accept-Ranges": "bytes",
               "Content-Length": str(length),
               "Content-Type": "audio/mpeg"}
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    if download_name:
        safe = re.sub(r'[^\w\s\-()]', '', download_name).strip()[:100]
        headers["Content-Disposition"] = (
            f'attachment; filename="{safe or "song"}.mp3"')
    return Response(gen(), status=status, headers=headers,
                    direct_passthrough=True)


@app.before_request
def _gate():
    if request.path.startswith("/api/") and request.path not in ("/api/verify", "/api/version"):
        # Token se bhi auth chalega (native app ke liye)
        tok = request.args.get("token") or request.headers.get("X-App-Token")
        if tok and tok in _app_tokens:
            if not _rate_ok("tok:" + tok, 240):
                return jsonify({"error": "rate_limited"}), 429
            return None
        if not session.get("authed"):
            return jsonify({"error": "unauthorized"}), 401
        if not _rate_ok("ip:" + (request.remote_addr or "?"), 120):
            return jsonify({"error": "rate_limited"}), 429


@app.route("/api/version")
def version():
    return jsonify({"version": BACKEND_VERSION})


@app.route("/api/verify", methods=["POST"])
def verify():
    # Brute-force guard (fail-open: limit check fail ho to allow)
    if not _rate_ok("verify:" + (request.remote_addr or "?"), 10):
        return jsonify({"ok": False, "error": "rate_limited"}), 429
    data = request.get_json(silent=True) or {}
    if data.get("code") == APP_CODE and APP_CODE:
        session["authed"] = True
        session.permanent = True
        # Native app ke liye token bhi do
        import secrets
        token = secrets.token_urlsafe(24)
        _app_tokens[token] = True
        return jsonify({"ok": True, "token": token})
    return jsonify({"ok": False}), 401


def bot_song_search(q):
    """Bot ka song path (dl_listen.song_search ke steps ka exact port):
    1) iTunes (_bot_web_song_search — bot wale strict title filter ke saath,
       max 5, saaf names)
    1b) iTunes khaali ho to JioSaavn (dl_engine.jiosaavn_search, max 5)
    2) dono khaali hon to YouTube (dl_engine.yt_search, max 5)
    Pehla non-empty source wins — kabhi merge nahi, koi scoring nahi.
    /api/search aur /api/botsearch dono use karte hain."""
    # 1) iTunes
    itunes = []
    try:
        itunes = _bot_web_song_search(q, 5)
    except Exception:
        itunes = []
    if itunes:
        return [{
            "key": (h["title"] + "|" + h["artist"]).lower(),
            "title": h["title"], "artist": h["artist"],
            "duration": _mss_to_secs(h.get("duration")),
            "image": h.get("image") or "",  # iTunes artworkUrl100 (600x600)
            "cached": _is_cached(h["title"], h["artist"]),
        } for h in itunes[:5]]

    # 1b) JioSaavn fallback
    js = []
    try:
        js = dl_engine.jiosaavn_search(q, 5) or []
    except Exception:
        js = []
    if js:
        out = []
        for h in js[:5]:
            t = (h.get("title") or "").strip()
            a = (h.get("artist") or "").strip()
            if not t:
                continue
            try:
                dur = int(h.get("duration")) if h.get("duration") else 0
            except (TypeError, ValueError):
                dur = 0
            out.append({
                "key": (t + "|" + a).lower(),
                "title": t, "artist": a,
                "duration": dur,
                "image": h.get("image") or "",
                "cached": _is_cached(t, a),
            })
        if out:
            return out

    # 2) YouTube fallback
    yt = []
    try:
        yt = dl_engine.yt_search(q, 5) or []
    except Exception:
        yt = []
    out = []
    for v in yt[:5]:
        vid = (v.get("id") or "").strip()
        if not vid:
            continue
        t = (v.get("title") or "").strip() or "video"
        a = (v.get("uploader") or "").strip()
        out.append({
            "key": ("yt:" + vid).lower(),
            "title": t, "artist": a,
            "duration": _mss_to_secs(v.get("duration")),
            "image": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            "ytid": vid,
            "cached": _is_cached(t, a),
        })
    return out


@app.route("/api/search")
def search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"results": []})
    return jsonify({"results": bot_song_search(q)})


@app.route("/api/botsearch")
def botsearch():
    """Bot jaisa search: pehle 596 curated artists me fuzzy match (top_n=8,
    score > 0.4). Artist mile to FULL pick list
    {"type":"artist","artists":[{"name","industry","category"}...],"query":q},
    warna bot wale song path ke results."""
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"type": "songs", "results": []})
    art = artist_route(q)
    if art:
        return jsonify({"type": "artist", **art})
    return jsonify({"type": "songs", "results": bot_song_search(q)})


ARTIST_SONGS_V3_FILE = os.path.join(BASE, "artist_songs_bot_v3.json")
_artist_songs_v3 = None


def _load_artist_songs_v3():
    """Bot ki official artist song lists (artist_songs_bot_v3.json)."""
    global _artist_songs_v3
    if _artist_songs_v3 is None:
        _artist_songs_v3 = _load_json_file(ARTIST_SONGS_V3_FILE, {})
    return _artist_songs_v3


ARTIST_COVERS_FILE = os.path.join(BASE, "artist_covers.json")
_artist_covers = None


def _load_artist_covers():
    """Batch backfill se bani covers map: 'artist|title' (lower) -> image URL."""
    global _artist_covers
    if _artist_covers is None:
        _artist_covers = _load_json_file(ARTIST_COVERS_FILE, {})
    return _artist_covers


def _accurate_artist_songs(artist_name):
    """Bot ke _get_accurate_artist_songs ka port: official lists se
    artist ke SAARE gaane [{title, artist}]."""
    data = _load_artist_songs_v3()
    if not data:
        return None
    qn = (artist_name or "").strip().lower()
    for key, songs in data.items():
        parts = key.split("|")
        aname = parts[2] if len(parts) > 2 else key
        if aname.strip().lower() == qn:
            return [{"title": (s.get("title") or "").strip(), "artist": artist_name}
                    for s in songs if (s.get("title") or "").strip()]
    return None


@app.route("/api/artist_songs")
def artist_songs():
    """Artist ke SAARE gaane — bot ke Artist Browser jaisa:
    pehle accurate official list (100+ gaane), nahi mili to
    dl_engine.jiosaavn_artist_songs(artist, 30),
    fallback dl_engine.jiosaavn_search(artist, 10).
    Har gaane par cached flag (bot ka ⚡).
    Auth + rate limit baaki /api/* routes jaisa (before_request _gate)."""
    name = (request.args.get("artist") or "").strip()
    if not name:
        return jsonify({"error": "artist chahiye"}), 400
    out = []
    acc = _accurate_artist_songs(name)
    if acc:
        # Durations + covers: JioSaavn artist top-30 se match karke bharo (1 extra call).
        dur_map = {}
        img_map = {}
        try:
            for r in dl_engine.jiosaavn_artist_songs(name, 30) or []:
                rt = (r.get("title") or "").strip().lower()
                if not rt:
                    continue
                rd = r.get("duration")
                if rd:
                    try:
                        dur_map[rt] = int(rd)
                    except (TypeError, ValueError):
                        pass
                ri = (r.get("image") or "").strip()
                if ri:
                    img_map[rt] = ri
        except Exception:
            pass
        covers = _load_artist_covers()
        an = name.strip().lower()
        for s in acc:
            t = s["title"]
            tl = t.strip().lower()
            out.append({"title": t, "artist": name,
                        "duration": dur_map.get(tl, 0),
                        # pehle JioSaavn top-30 ka fresh cover, nahi to batch backfill
                        "image": img_map.get(tl) or covers.get(an + "|" + tl, ""),
                        "cached": _is_cached(t, name)})
        return jsonify({"artist": name, "songs": out})
    songs = []
    try:
        songs = dl_engine.jiosaavn_artist_songs(name, 30) or []
    except Exception:
        songs = []
    if not songs:
        # Artist page khaali ho to naam se song search (bot jaisa)
        try:
            for r in dl_engine.jiosaavn_search(name, 10) or []:
                t = (r.get("title") or "").strip()
                a = (r.get("artist") or "").strip()
                if t:
                    songs.append({
                        "title": t, "artist": a, "enc": r.get("enc", ""),
                        "image": r.get("image", ""),
                        "duration": r.get("duration")})
        except Exception:
            songs = []
    out = []
    for s in songs:
        t = (s.get("title") or "").strip()
        if not t:
            continue
        try:
            dur = int(s.get("duration")) if s.get("duration") else 0
        except (TypeError, ValueError):
            dur = 0
        out.append({"title": t, "artist": (s.get("artist") or "").strip(),
                    "duration": dur, "image": s.get("image") or ""})
    return jsonify({"artist": name, "songs": out})


@app.route("/api/stream")
def stream():
    title = (request.args.get("title") or "").strip()
    artist = (request.args.get("artist") or "").strip()
    ytid = (request.args.get("ytid") or "").strip() or None
    if not title:
        return jsonify({"error": "title chahiye"}), 400
    path, err = _stream_fetch(title, artist, ytid=ytid)
    if not path:
        return jsonify({"error": err}), 404
    return send_range(path)


@app.route("/api/download")
def download():
    title = (request.args.get("title") or "").strip()
    artist = (request.args.get("artist") or "").strip()
    ytid = (request.args.get("ytid") or "").strip() or None
    if not title:
        return jsonify({"error": "title chahiye"}), 400
    path, err = _stream_fetch(title, artist, ytid=ytid, for_download=True)
    if not path:
        return jsonify({"error": err}), 404
    return send_range(path, download_name=f"{title} - {artist}")


@app.route("/api/report", methods=["POST"])
def report():
    """Wrong-song report (Om ka order 2026-10-17): Now Playing me report
    button -> {"reason": "wrong_song", "title": ..., "artist": ...}.

    v2.6.2: ASYNC — turant {"ok": true, "status": "processing"} return karta
    hai. Asli kaam (fresh verified download + channel replace upload)
    background thread me hota hai. App /api/report_status?key=... poll
    karke completion dekhta hai. Isse app ki screen kabhi block nahi hoti.

    Flow (background): poisoned Telegram-cache entry evict karo -> fresh
    VERIFIED download (Telegram cache SKIP — wahi galat hai; JioSaavn-first,
    title+artist match, 2.5s duration rule — Om ke universal rules) ->
    sahi file channel me sendAudio (replace) -> runtime cache update.
    """
    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or "").strip()
    title = (data.get("title") or "").strip()
    artist = (data.get("artist") or "").strip()
    if reason != "wrong_song" or not title:
        return jsonify({"ok": False, "error": "reason/title chahiye"}), 400
    key = (title + "|" + artist).lower()

    # Pehle se chal raha ho to dobara start mat karo.
    with _report_lock():
        existing = _report_jobs.get(key)
        if existing and existing.get("status") == "processing":
            return jsonify({"ok": True, "status": "processing",
                            "key": key})
        _report_jobs[key] = {"status": "processing", "stream_url": "",
                             "error": ""}

    import threading
    t = threading.Thread(target=_report_worker, args=(title, artist, key),
                         daemon=True, name=f"report-{safe_key(key)[:16]}")
    t.start()
    return jsonify({"ok": True, "status": "processing", "key": key})


def _report_worker(title, artist, key):
    """Background me report process karo: evict -> fresh download ->
    channel upload -> cache update. Status _report_jobs me update hota hai.
    """
    try:
        # 1. Evict poisoned entries (runtime cache + disk cache + local mp3).
        # Pehle purane message_id ko pakad lo taaki naya upload hone ke baad
        # galat wale message ko channel se delete kar sakein (best effort).
        old_mid = None
        try:
            _, old_mid = _tg_cache_entry(key)
        except Exception:
            old_mid = None
        global _tg_cache
        try:
            if _tg_cache is not None:
                _tg_cache.pop(key, None)
        except Exception:
            pass
        try:
            d = _load_json_file(TG_CACHE_FILE, {})
            if key in d:
                del d[key]
                with open(TG_CACHE_FILE, "w") as f:
                    json.dump(d, f)
        except Exception:
            pass
        try:
            lp = os.path.join(MEDIA_DIR, safe_key(key) + ".mp3")
            if os.path.exists(lp):
                os.remove(lp)
        except Exception:
            pass

        # 2. Fresh VERIFIED download (Telegram cache skip)
        path, err = _fresh_verified_media(title, artist)
        if not path:
            with _report_lock():
                _report_jobs[key] = {"status": "failed",
                                     "error": err or "sahi gaana nahi mila",
                                     "stream_url": ""}
            return

        # 3. Channel me replace (sahi file upload)
        fid, mid = _channel_replace_upload(path, title, artist)

        # 3b. Purana GALAT message delete karo (best effort). Bot purane
        # migration messages delete nahi kar sakta (HTTP 400) — recent
        # uploads delete ho jayenge, baaki ignore.
        if fid and old_mid:
            try:
                _channel_delete_message(old_mid)
            except Exception:
                pass

        # 4. Runtime cache update (agla /api/stream turant sahi file dega).
        # Naya format: {file_id, message_id} taaki agli report par delete
        # ho sake.
        if fid:
            try:
                _tg_cache_bind(key, fid, mid)
            except Exception:
                pass

        import urllib.parse
        stream_url = ("/api/stream?title=" + urllib.parse.quote(title) +
                      "&artist=" + urllib.parse.quote(artist))
        with _report_lock():
            _report_jobs[key] = {"status": "done", "stream_url": stream_url,
                                 "file_id": fid or "", "message_id": mid or 0,
                                 "error": ""}
    except Exception as e:
        with _report_lock():
            _report_jobs[key] = {"status": "failed",
                                 "error": str(e)[:200], "stream_url": ""}


@app.route("/api/report_status", methods=["GET"])
def report_status():
    """v2.6.2: report job ka status. App har 15s poll karta hai.
    -> {"ok": true, "status": "processing"|"done"|"failed",
        "stream_url": ..., "error": ...}
    """
    key = (request.args.get("key") or "").strip().lower()
    if not key:
        return jsonify({"ok": False, "error": "key chahiye"}), 400
    with _report_lock():
        job = _report_jobs.get(key)
    if not job:
        return jsonify({"ok": False, "error": "koi report nahi mili"}), 404
    out = {"ok": True, "status": job.get("status", "processing"),
           "stream_url": job.get("stream_url", ""),
           "error": job.get("error", "")}
    # Purane completed jobs saaf karo taaki memory na bhare.
    if out["status"] in ("done", "failed"):
        with _report_lock():
            _report_jobs.pop(key, None)
    return jsonify(out)


@app.route("/api/playlist_resolve", methods=["GET", "POST"])
def playlist_resolve():
    """Playlist URL resolver (Om ka order 2026-10-18): Spotify/YouTube/
    JioSaavn playlist link -> {ok, name, tracks: [{title, artist}]}.

    Bot me ye pehle se hai (dl_engine.playlist_info), app ke backend me
    nahi tha — v2.0 gap. App SearchScreen se playlist link paste par
    ye endpoint call hota hai.
    """
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or request.args.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "url chahiye"}), 400
    try:
        result = dl_engine.playlist_info(url)
    except Exception as e:
        return jsonify({"ok": False,
                        "error": "resolve fail: %s" % str(e)[:80]}), 500
    if not result:
        err = getattr(dl_engine, "last_playlist_error", "") or ""
        msg = ("private/deleted playlist" if err == "private"
               else "playlist samajh nahi aayi")
        return jsonify({"ok": False, "error": msg}), 404
    label, songs = result
    tracks = [{"title": (s.get("title") or "").strip(),
               "artist": (s.get("artist") or "").strip()}
              for s in (songs or [])]
    tracks = [t for t in tracks if t["title"]]
    if not tracks:
        return jsonify({"ok": False, "error": "koi gaana nahi mila"}), 404
    # Playlist cover art (Spotify og:image; YouTube/JioSaavn ke liye khaali)
    image = ""
    try:
        low = url.lower()
        if "open.spotify.com" in low:
            m = re.search(r"/playlist/([A-Za-z0-9]{22})", url)
            if m:
                req = urllib.request.Request(
                    "https://open.spotify.com/playlist/" + m.group(1),
                    headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    raw = resp.read().decode("utf-8", "ignore")
                im = re.search(
                    r'<meta property="og:image" content="([^"]+)"', raw)
                if im:
                    image = im.group(1)
    except Exception:
        pass
    # v2.6.3: playlist ke saare gaane background cache queue me — dheere
    # dheere poori playlist Telegram par aa jayegi (PRIMARY source).
    # Search results yahan NAHI aate (Om: faltu gaane waste hain).
    try:
        _cache_enqueue(tracks)
    except Exception:
        pass
    return jsonify({"ok": True, "name": label or "Playlist",
                    "image": image, "tracks": tracks})


_REPORT_JUNK = ("cover", "remix", "karaoke", "instrumental", "lofi",
                "slowed", "sped", "amapiano", "acoustic", "unplugged",
                "reprise", "tribute", "mashup", "dj ", "version", "8d",
                "1 hour", "ringtone", "whatsapp status")


def _report_words(s):
    return [w for w in re.findall(r"[a-z0-9]+", (s or "").lower())
            if len(w) > 1 and w != "the"]


def _report_strict_match(title, artist, hit):
    """Om ke universal rules (2026-10-15): sirf sahi artist ka official
    version — cover/remix/karaoke/instrumental/lofi/slowed waghera auto-skip.
    title_matches() bahut dheela hai (sirf title dekhta hai), isliye report
    flow me ye strict check lagta hai."""
    ht = (hit.get("title") or "").lower()
    ha = (hit.get("artist") or "").lower()
    if any(j in ht for j in _REPORT_JUNK):
        return False
    tw = _report_words(title)
    hw = set(_report_words(ht))
    if not tw or not all(w in hw for w in tw):
        return False
    aw = _report_words(artist)
    if aw and not any(w in set(_report_words(ha)) for w in aw):
        return False
    return True


def _report_yt_artist_ok(title, artist, pick):
    """v2.6.2: YouTube pick ka strict artist check.

    pick ke title ya uploader me artist ke significant words hone chahiye.
    yt_best_pick sirf rerank karta hai (koi result kabhi khaali nahi hota),
    isliye yahan hard reject lagta hai — galat artist ka version kabhi
    aage na badhe.
    """
    aw = _report_words(artist)
    if not aw:
        return True
    pt = (pick.get("title") or "").lower()
    ul = (pick.get("uploader") or "").lower()
    combined = pt + " " + ul
    # Kam se kam ek significant artist word title/uploader me hona chahiye.
    return any(w in combined for w in aw)


def _report_expected_duration(title, artist):
    """iTunes se official duration (2.5s rule ke liye expected)."""
    try:
        import urllib.request
        import urllib.parse
        params = urllib.parse.urlencode({
            "term": f"{title} {artist}", "media": "music",
            "entity": "song", "limit": 5, "country": "IN"})
        req = urllib.request.Request(
            "https://itunes.apple.com/search?" + params,
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
        for t in (data.get("results") or []):
            tn = (t.get("trackName") or "").lower()
            an = (t.get("artistName") or "").lower()
            if not _report_words(title) or not all(
                    w in tn for w in _report_words(title)):
                continue
            aw = _report_words(artist)
            if aw and not any(w in an for w in aw):
                continue
            ms = t.get("trackTimeMillis")
            if ms:
                return float(ms) / 1000.0
    except Exception:
        pass
    return None


def _fresh_verified_media(title, artist):
    """STRICT verified MP3 lao — SHARED fetcher (report flow + background
    channel caching dono isi ko use karte hain).

    Telegram cache SKIP karke fresh download. Returns (path, err).
    Om ke universal rules (2026-10-15) + song-matching rule (2026-10-19):
      1. Duration: expected (iTunes) se ±2.5s ke andar hona chahiye
      2. Artist: kam se kam partial match (koi significant word)
      3. Title: saare words match + junk (cover/remix/karaoke/...) auto-skip
    Galat gaana mile to (None, err) — channel par kuch nahi jata.
    """
    import tempfile
    import shutil
    tmpdir = tempfile.mkdtemp(prefix="report_", dir=MEDIA_DIR)
    try:
        # 1. JioSaavn (throttle nahi to)
        if dl_engine.jiosaavn_usable():
            try:
                hits = dl_engine.jiosaavn_search(f"{title} {artist}", n=5)
                for h in hits:
                    if not _report_strict_match(title, artist, h):
                        continue
                    exp = h.get("duration")
                    jp, _ = dl_engine.jiosaavn_download(
                        h["enc"], tmpdir, title=title, artist=artist,
                        image=h.get("image"), expected_duration=exp)
                    if jp and os.path.exists(jp) and _report_duration_ok(
                            jp, exp):
                        return _report_finalize(jp, title, artist,
                                               tmpdir), None
            except Exception:
                pass
        # 2. YouTube fallback (artist-check wala yt_best_pick +
        #    strict verification: junk-skip, 2.5s rule). Kai query
        #    variants try karo — pehla query aksar lyrics video deta hai.
        try:
            exp = _report_expected_duration(title, artist)
            for q in (f"{title} {artist} song",
                      f"{artist} {title} official audio",
                      f"{title} {artist} official"):
                pick = None
                try:
                    # v2.6.2: artist pass karo taaki yt_best_pick sahi
                    # artist wale results ko upar rank kare (Om 2026-10-19:
                    # dobara galat gaana aaya tha — YouTube path par artist
                    # check lagta hi nahi tha).
                    pick = dl_engine.yt_best_pick(q, artist=artist)
                except Exception:
                    continue
                if not pick or pick.get("duration_secs", 0) > 2700:
                    continue
                pt = (pick.get("title") or "").lower()
                if any(j in pt for j in _REPORT_JUNK):
                    continue
                # v2.6.2: strict artist verify — title ya uploader me
                # artist ka naam hona chahiye, warna galat artist ka
                # version hai.
                if not _report_yt_artist_ok(title, artist, pick):
                    continue
                psecs = pick.get("duration_secs") or 0
                if exp and abs(psecs - exp) > 2.5:
                    continue
                yp, _ = dl_engine.download_audio(pick["url"], tmpdir)
                if (yp and os.path.exists(yp)
                        and _report_duration_ok(yp, exp)):
                    dl_engine.tag_audio_file(yp, title, artist)
                    return _report_finalize(yp, title, artist,
                                           tmpdir), None
        except Exception:
            pass
    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass
    return None, "sahi gaana nahi mila"


def _report_duration_ok(path, expected):
    """2.5s rule: downloaded file expected duration ke ±2.5s me honi chahiye."""
    try:
        if not expected:
            return True
        got = dl_engine.audio_duration(path)
        return got and abs(got - float(expected)) <= 2.5
    except Exception:
        return False


def _report_finalize(src, title, artist, tmpdir):
    """Verified file ko MEDIA_DIR me rakho (key-based naam)."""
    import shutil
    key = (title + "|" + artist).lower()
    dst = os.path.join(MEDIA_DIR, safe_key(key) + ".mp3")
    try:
        os.replace(src, dst)
    except OSError:
        shutil.copy(src, dst)
    return dst


def _channel_replace_upload(path, title, artist):
    """Sahi MP3 channel me bhejo (replace). Returns (file_id, message_id)."""
    if not TG_BOT_TOKEN:
        return None, None
    try:
        import urllib.request
        import uuid
        boundary = uuid.uuid4().hex
        fields = {
            "chat_id": TG_CACHE_CHANNEL,
            "title": title[:100],
            "performer": artist[:100],
        }
        body = bytearray()
        for k, v in fields.items():
            body += f"--{boundary}\r\n".encode()
            body += (f'Content-Disposition: form-data; name="{k}"\r\n\r\n'
                     ).encode()
            body += f"{v}\r\n".encode()
        with open(path, "rb") as f:
            fdata = f.read()
        body += f"--{boundary}\r\n".encode()
        body += (b'Content-Disposition: form-data; name="audio"; '
                 b'filename="song.mp3"\r\n'
                 b"Content-Type: audio/mpeg\r\n\r\n")
        body += fdata + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendAudio",
            data=bytes(body),
            headers={"Content-Type":
                     f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.load(r)
        res = (resp or {}).get("result") or {}
        audio = res.get("audio") or {}
        return audio.get("file_id"), res.get("message_id")
    except Exception:
        return None, None


@app.route("/api/prepare")
def prepare():
    """File cache me taiyaar karo (DownloadManager se pehle)."""
    title = (request.args.get("title") or "").strip()
    artist = (request.args.get("artist") or "").strip()
    ytid = (request.args.get("ytid") or "").strip() or None
    if not title:
        return jsonify({"error": "title chahiye"}), 400
    path, err = _stream_fetch(title, artist, ytid=ytid)
    if not path:
        return jsonify({"error": err or "taiyaar nahi hua"}), 404
    return jsonify({"ok": True})


@app.route("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.route("/<path:p>")
def web_static(p):
    if ".." in p or p.startswith("/"):
        return jsonify({"error": "not found"}), 404
    return send_from_directory(WEB_DIR, p)


# ---------------- Trending ----------------
@app.route("/api/trending")
def trending():
    lang = (request.args.get("lang") or "hindi").lower()
    if lang not in TREND_CHARTS:
        return jsonify({"error": "galat language"}), 400
    import time
    now = time.time()
    c = _trend_cache.get(lang)
    if c and now - c["at"] < 6 * 3600:
        return jsonify({"songs": c["songs"], "name": TREND_CHARTS[lang][0]})
    name, cid = TREND_CHARTS[lang]
    try:
        if cid.startswith("itunes:"):
            songs = dl_engine.itunes_chart(cid.split(":")[1])
            # iTunes RSS me duration nahi hota — ek batched lookup se
            # duration + 600px artwork bharo (2026-10-17.4).
            if songs:
                meta = dl_engine.itunes_track_meta([s.get("tid", "") for s in songs])
                for s in songs:
                    m = meta.get(str(s.get("tid", ""))) or {}
                    if m.get("duration"):
                        s["duration"] = m["duration"]
                    if m.get("image"):
                        s["image"] = m["image"]
            # iTunes fail ho to JioSaavn English chart try karo
            if not songs and lang == "english":
                songs = dl_engine.jiosaavn_chart("1134543273", n=50)
        else:
            songs = dl_engine.jiosaavn_chart(cid, n=50)
    except Exception:
        songs = []
    out = [{"title": s.get("title", ""), "artist": s.get("artist", ""),
            "duration": s.get("duration", 0) or 0,
            "image": s.get("image", "")} for s in (songs or [])[:50]]
    if out:
        _trend_cache[lang] = {"at": now, "songs": out}
    elif c:
        return jsonify({"songs": c["songs"], "name": name})
    return jsonify({"songs": out, "name": name})


# ---------------- Playlists ----------------
def _get_playlists():
    return _load_json_file(PLAYLISTS_FILE, {})


@app.route("/api/playlists")
def playlists():
    pls = _get_playlists()
    return jsonify({"playlists": [
        {"name": n, "count": len(s)} for n, s in pls.items()]})


@app.route("/api/playlists", methods=["POST"])
def playlist_create():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()[:50]
    if not name:
        return jsonify({"error": "naam do"}), 400
    pls = _get_playlists()
    if name not in pls:
        pls[name] = []
        _save_json_file(PLAYLISTS_FILE, pls)
    return jsonify({"ok": True})


@app.route("/api/playlist")
def playlist_get():
    name = request.args.get("name", "")
    pls = _get_playlists()
    return jsonify({"songs": pls.get(name, [])})


@app.route("/api/playlist/add", methods=["POST"])
def playlist_add():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    title = (data.get("title") or "").strip()
    artist = (data.get("artist") or "").strip()
    if not name or not title:
        return jsonify({"error": "naam/title chahiye"}), 400
    pls = _get_playlists()
    lst = pls.setdefault(name, [])
    key = (title + "|" + artist).lower()
    if not any((s.get("title", "") + "|" + s.get("artist", "")).lower() == key
               for s in lst):
        lst.append({"title": title, "artist": artist})
        _save_json_file(PLAYLISTS_FILE, pls)
    return jsonify({"ok": True})


@app.route("/api/playlist/remove", methods=["POST"])
def playlist_remove():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    title = (data.get("title") or "").strip()
    artist = (data.get("artist") or "").strip()
    pls = _get_playlists()
    if name in pls:
        key = (title + "|" + artist).lower()
        pls[name] = [s for s in pls[name]
                     if (s.get("title", "") + "|" + s.get("artist", "")).lower() != key]
        _save_json_file(PLAYLISTS_FILE, pls)
    return jsonify({"ok": True})


@app.route("/api/playlist/delete", methods=["POST"])
def playlist_delete():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    pls = _get_playlists()
    if name in pls:
        del pls[name]
        _save_json_file(PLAYLISTS_FILE, pls)
    return jsonify({"ok": True})


# ---------------- Liked ----------------
@app.route("/api/liked")
def liked_get():
    return jsonify({"songs": _load_json_file(LIKED_FILE, [])})


@app.route("/api/liked", methods=["POST"])
def liked_toggle():
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    artist = (data.get("artist") or "").strip()
    like = bool(data.get("liked", True))
    if not title:
        return jsonify({"error": "title chahiye"}), 400
    lst = _load_json_file(LIKED_FILE, [])
    key = (title + "|" + artist).lower()
    lst = [s for s in lst
           if (s.get("title", "") + "|" + s.get("artist", "")).lower() != key]
    if like:
        lst.insert(0, {"title": title, "artist": artist})
    _save_json_file(LIKED_FILE, lst)
    return jsonify({"ok": True, "liked": like})


# ---------------- Link download ----------------
@app.route("/api/link", methods=["POST"])
def link_download():
    """URL paste karo -> info + download (bot jaisa)."""
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url or not url.startswith("http"):
        return jsonify({"error": "valid link do"}), 400
    try:
        info = dl_engine._title_of(url)
        title = info.strip() or "song"
        artist = ""
    except Exception:
        title, artist = "song", ""
    return jsonify({
        "ok": True, "title": title, "artist": artist,
        "stream": "/api/stream?title=" + urllib.parse.quote(title) +
                  "&artist=" + urllib.parse.quote(artist),
        "dl": "/api/download?title=" + urllib.parse.quote(title) +
              "&artist=" + urllib.parse.quote(artist),
        "filename": f"{title} - {artist}".strip(" -") + ".mp3",
    })


# ---------------- Playlist sharing ----------------
SHARED_FILE = os.path.join(DATA_DIR, "shared_playlists.json")

@app.route("/api/share", methods=["POST"])
def share_playlist():
    import secrets, time
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "naam do"}), 400
    pls = _get_playlists()
    songs = pls.get(name, [])
    if not songs:
        return jsonify({"error": "playlist empty hai"}), 400
    shared = _load_json_file(SHARED_FILE, {})
    token = secrets.token_urlsafe(12)
    shared[token] = {"name": name, "songs": songs, "at": time.time()}
    _save_json_file(SHARED_FILE, shared)
    return jsonify({"ok": True, "token": token})


@app.route("/api/shared/<token>")
def shared_get(token):
    shared = _load_json_file(SHARED_FILE, {})
    s = shared.get(token)
    if not s:
        return jsonify({"error": "link kaam nahi kar raha"}), 404
    return jsonify({"name": s["name"], "songs": s["songs"]})


@app.route("/api/share/import", methods=["POST"])
def share_import():
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    if not token:
        return jsonify({"error": "token do"}), 400
    shared = _load_json_file(SHARED_FILE, {})
    s = shared.get(token)
    if not s:
        return jsonify({"error": "link kaam nahi kar raha"}), 404
    pls = _get_playlists()
    name = s["name"]
    base, i = name, 2
    while name in pls:
        name = f"{base} ({i})"
        i += 1
    pls[name] = s["songs"]
    _save_json_file(PLAYLISTS_FILE, pls)
    return jsonify({"ok": True, "name": name, "count": len(s["songs"])})


if __name__ == "__main__":
    app.run(host=HOST, port=PORT, threaded=True)
