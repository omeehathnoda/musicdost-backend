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
            return None
        if not session.get("authed"):
            return jsonify({"error": "unauthorized"}), 401


@app.route("/api/verify", methods=["POST"])
def verify():
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


_app_tokens = {}


@app.route("/api/search")
def search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"results": []})

    # 0. iTunes se sahi gaane nikalo (bot jaisa) — sabse upar dikhenge
    itunes_hits = []
    try:
        itunes_hits = dl_engine.web_song_search(q, n=5)
    except Exception:
        pass

    def relevance(title, artist):
        """Query se kitna milta hai — zyada score = upar dikhega."""
        import re
        qt = re.sub(r"[^a-z0-9 ]", "", q.lower())
        tt = re.sub(r"[^a-z0-9 ]", "", (title or "").lower())
        qw = [w for w in qt.split() if len(w) > 1]
        if not qw:
            return 0
        score = 0
        # exact title match sabse upar
        if qt == tt:
            score += 100
        # title query se shuru ho
        if tt.startswith(qt):
            score += 50
        # kitne query words title me hain
        hits = sum(1 for w in qw if w in tt)
        score += hits * 10
        # saare words mile to bonus
        if hits == len(qw):
            score += 20
        return score

    out = []
    for h in dl_engine.jiosaavn_search(q, n=10):
        t, a = h.get("title", ""), h.get("artist", "")
        dur = h.get("duration")
        try:
            dur = int(dur) if dur else 0
        except (TypeError, ValueError):
            dur = 0
        # local cache check
        key = (t + "|" + a).lower()
        cpath = os.path.join(MEDIA_DIR, safe_key(key) + ".mp3")
        cached = os.path.exists(cpath) and os.path.getsize(cpath) > 50 * 1024
        out.append({
            "key": key, "title": t, "artist": a,
            "duration": dur, "cached": cached,
            "image": h.get("image", ""),
            "_score": relevance(t, a),
        })
    out.sort(key=lambda x: x["_score"], reverse=True)
    # YouTube results hamesha lao (bot wali smart scoring ke saath)
    _JUNK = ("live", "reaction", "cover", "1 hour", "1hour", "loop",
             "8d audio", "slowed", "sped up", "ringtone", "status",
             "whatsapp", "dj remix", "mashup")
    _GOOD = ("topic", "vevo", "official", "music", "records",
             "t-series", "sony", "zee", "tips", "saregama")
    try:
        import re as _re
        qwords = [w for w in _re.findall(r"[a-z0-9]+", q.lower())
                  if len(w) >= 3 and w not in ("song", "the", "from", "with")]
        for v in dl_engine.yt_search(q + " song", n=8):
            vid = v.get("id", "")
            t = v.get("title", "") or "video"
            a = v.get("uploader", "")
            tl, ul = t.lower(), (a or "").lower()
            # Bot wali scoring
            ys = 0
            if any(j in tl for j in _JUNK):
                ys -= 50
            if any(g in ul for g in _GOOD):
                ys += 10
            if "official" in tl:
                ys += 5
            for w in qwords:
                if w in tl:
                    ys += 2
            dur = 0
            try:
                parts = (v.get("duration") or "").split(":")
                for p in parts:
                    dur = dur * 60 + int(p)
            except (ValueError, TypeError):
                dur = 0
            if dur > 2700:
                continue
            if 120 <= dur <= 480:
                ys += 5
            elif dur > 600:
                ys -= 20
            # Base score + smart scoring
            score = 15 + ys
            if score < 0:
                continue
            key = ("yt:" + vid).lower()
            # Duplicate na ho
            if any(x.get("ytid") == vid for x in out):
                continue
            out.append({
                "key": key, "title": t, "artist": a,
                "duration": dur, "cached": False,
                "image": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                "ytid": vid, "_score": score,
            })
    except Exception:
        pass
    # iTunes ke sahi gaane sabse upar (bot jaisa)
    for h in itunes_hits:
        t, a = h.get("title", ""), h.get("artist", "")
        if not t:
            continue
        # Duplicate na ho
        if any(x.get("title", "").lower() == t.lower() for x in out):
            continue
        key = (t + "|" + a).lower()
        cpath = os.path.join(MEDIA_DIR, safe_key(key) + ".mp3")
        cached = os.path.exists(cpath) and os.path.getsize(cpath) > 50 * 1024
        out.append({
            "key": key, "title": t, "artist": a,
            "duration": 0, "cached": cached,
            "image": "", "_score": 200,
        })
    out.sort(key=lambda x: x["_score"], reverse=True)
    for x in out:
        del x["_score"]
    return jsonify({"results": out})


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
