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

os.makedirs(MEDIA_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")


def safe_key(key):
    s = re.sub(r"\s+", " ", key.strip().lower())
    s = re.sub(r"[^a-z0-9 _\-()\[\]]+", "", s)
    s = re.sub(r"\s+", "_", s).strip("_")
    if not s:
        s = hashlib.md5(key.encode()).hexdigest()
    return s[:120]


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


def ensure_media(title, artist):
    """MP3 lao. Returns (path, err)."""
    key = (title + "|" + artist).lower()
    path = os.path.join(MEDIA_DIR, safe_key(key) + ".mp3")
    if os.path.exists(path) and os.path.getsize(path) > 50 * 1024:
        return path, None
    tmpdir = os.path.join(MEDIA_DIR, ".work")
    os.makedirs(tmpdir, exist_ok=True)

    # 1. JioSaavn
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

    # 2. YouTube fallback
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
            return path, None
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
        if not session.get("authed"):
            return jsonify({"error": "unauthorized"}), 401


@app.route("/api/verify", methods=["POST"])
def verify():
    data = request.get_json(silent=True) or {}
    if data.get("code") == APP_CODE and APP_CODE:
        session["authed"] = True
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 401


@app.route("/api/search")
def search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"results": []})
    out = []
    for h in dl_engine.jiosaavn_search(q, n=8):
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
        })
    return jsonify({"results": out})


@app.route("/api/stream")
def stream():
    title = (request.args.get("title") or "").strip()
    artist = (request.args.get("artist") or "").strip()
    if not title:
        return jsonify({"error": "title chahiye"}), 400
    path, err = ensure_media(title, artist)
    if not path:
        return jsonify({"error": err}), 404
    return send_range(path)


@app.route("/api/download")
def download():
    title = (request.args.get("title") or "").strip()
    artist = (request.args.get("artist") or "").strip()
    if not title:
        return jsonify({"error": "title chahiye"}), 400
    path, err = ensure_media(title, artist)
    if not path:
        return jsonify({"error": err}), 404
    return send_range(path, download_name=f"{title} - {artist}")


@app.route("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.route("/<path:p>")
def web_static(p):
    if ".." in p or p.startswith("/"):
        return jsonify({"error": "not found"}), 404
    return send_from_directory(WEB_DIR, p)


if __name__ == "__main__":
    app.run(host=HOST, port=PORT, threaded=True)
