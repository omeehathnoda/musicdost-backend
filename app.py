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

def tg_cache_lookup(title, artist):
    """Telegram channel me file_id dhoondo. Returns file_id or None."""
    global _tg_cache
    _refresh_tg_cache()
    if _tg_cache is None:
        _tg_cache = _load_json_file(TG_CACHE_FILE, {})
    key = (title + "|" + artist).lower()
    if key in _tg_cache:
        return _tg_cache[key]
    # Smart match: title similar ho to bhi chalega
    import re
    def norm(s):
        return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()
    nt, na = norm(title), norm(artist)
    for k, fid in _tg_cache.items():
        kt, ka = k.split("|", 1) if "|" in k else (k, "")
        if norm(kt) == nt and (not na or na in norm(ka) or norm(ka) in na):
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


def ensure_media(title, artist, ytid=None):
    """MP3 lao. Returns (path, err)."""
    key = (title + "|" + artist).lower()
    if ytid:
        key = "yt:" + ytid
    path = os.path.join(MEDIA_DIR, safe_key(key) + ".mp3")
    if os.path.exists(path) and os.path.getsize(path) > 50 * 1024:
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
        out.append({"title": title, "artist": artist, "duration": dur_s})
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
    if request.path.startswith("/api/") and request.path != "/api/verify":
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
            "image": "",  # iTunes ke paas cover URL nahi
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


@app.route("/api/artist_songs")
def artist_songs():
    """Artist ke top gaane — bot ke show_artist ka exact port:
    dl_engine.jiosaavn_artist_songs(artist, 30),
    fallback dl_engine.jiosaavn_search(artist, 10).
    Auth + rate limit baaki /api/* routes jaisa (before_request _gate)."""
    name = (request.args.get("artist") or "").strip()
    if not name:
        return jsonify({"error": "artist chahiye"}), 400
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
    path, err = ensure_media(title, artist, ytid=ytid)
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
    path, err = ensure_media(title, artist, ytid=ytid)
    if not path:
        return jsonify({"error": err}), 404
    return send_range(path, download_name=f"{title} - {artist}")


@app.route("/api/prepare")
def prepare():
    """File cache me taiyaar karo (DownloadManager se pehle)."""
    title = (request.args.get("title") or "").strip()
    artist = (request.args.get("artist") or "").strip()
    ytid = (request.args.get("ytid") or "").strip() or None
    if not title:
        return jsonify({"error": "title chahiye"}), 400
    path, err = ensure_media(title, artist, ytid=ytid)
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
            # iTunes fail ho to JioSaavn English chart try karo
            if not songs and lang == "english":
                songs = dl_engine.jiosaavn_chart("1134543273", n=50)
        else:
            songs = dl_engine.jiosaavn_chart(cid, n=50)
    except Exception:
        songs = []
    out = [{"title": s.get("title", ""), "artist": s.get("artist", ""),
            "duration": 0, "image": s.get("image", "")} for s in (songs or [])[:50]]
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
