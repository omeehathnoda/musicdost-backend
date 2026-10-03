#!/usr/bin/env python3
"""Download engine for the downloader bot (yt-dlp wrappers).

- yt-dlp standalone binary (pip blocked by PEP 668 here).
- Always --no-check-certificate: egress goes through a MITM proxy.
- YouTube via the android player client to dodge the bot sign-in wall.
- Telegram send cap is 50MB -> keep files <= 48MB.
"""
import base64
import glob
import html
import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request

YTDLP = os.environ.get("YTDLP_BIN") or shutil.which("yt-dlp") or os.path.expanduser("~/.local/bin/yt-dlp")
BASE = [
    "--no-check-certificate",
    "--no-playlist",
    "--extractor-args", "youtube:player_client=android",
    "--no-warnings",
]
MAX_MB = 48


def _run(args, timeout=120):
    return subprocess.run(
        [YTDLP, *BASE, *args],
        capture_output=True, text=True, timeout=timeout,
    )


def clean_url(text):
    m = re.search(r"https?://\S+", text)
    if not m:
        return None
    return m.group(0).rstrip(").,>]'\"")


def yt_search(query, n=5):
    """YouTube search -> [{'id','title','uploader','duration'}]."""
    r = _run([f"ytsearch{n}:{query}", "--flat-playlist",
              "--print", "%(id)s\t%(title)s\t%(uploader)s\t%(duration_string)s"],
             timeout=90)
    out = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].strip():
            out.append({
                "id": parts[0].strip(),
                "title": (parts[1] if len(parts) > 1 else "").strip() or "video",
                "uploader": (parts[2] if len(parts) > 2 else "").strip(),
                "duration": (parts[3] if len(parts) > 3 else "").strip(),
            })
    return out


# --- JioSaavn fast path -------------------------------------------------
# JioSaavn ka public search API + DES-encrypted media URL.
# IMPORTANT: OpenSSL 3 me single-DES nahi hai, isliye Triple-DES
# (des-ede3-ecb) key ko 3 baar repeat karke use karo — ye mathematically
# single-DES ke barabar hai. (2026-10-01 ko verify kiya.)
JS_DES_KEY = b"38346591" * 3  # 24 bytes for Triple DES


def js_decrypt(enc_url):
    """Encrypted media URL ko decrypt karke seedha CDN URL do.
    Returns None on failure."""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        data = base64.b64decode(enc_url.strip())
        cipher = Cipher(algorithms.TripleDES(JS_DES_KEY), modes.ECB())
        dec = cipher.decryptor().update(data) + cipher.decryptor().finalize()
        # PKCS5/PKCS7 padding hatao
        pad = dec[-1]
        url = dec[:-pad].decode("utf-8", errors="ignore").strip()
        return url or None
    except Exception:
        pass
    # fallback: openssl CLI (NOTE: input me trailing newline zaroori hai,
    # bina uske base64 ka aakhri block flush nahi hota -> "bad decrypt")
    try:
        r = subprocess.run(
            ["openssl", "enc", "-d", "-a", "-des-ede3-ecb",
             "-K", JS_DES_KEY.hex()],
            input=(enc_url.strip() + "\n").encode(),
            capture_output=True, timeout=15)
        url = r.stdout.decode("utf-8", errors="ignore").strip().strip("\x00")
        return url or None
    except Exception:
        return None


def jiosaavn_search(query, n=5):
    """JioSaavn search: [{'title','artist','enc'}]. Empty list on failure."""
    try:
        params = urllib.parse.urlencode({
            "__call": "search.getResults", "q": query, "p": "1",
            "n": str(n * 2), "_format": "json", "_marker": "0",
            "api_version": "4", "ctx": "web6dot0"})
        req = urllib.request.Request(
            "https://www.jiosaavn.com/api.php?" + params,
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
    except Exception:
        return []
    out, seen = [], set()
    for r in data.get("results", []):
        title = (r.get("title") or "").strip()
        mi = r.get("more_info") or {}
        enc = (mi.get("encrypted_media_url") or "").strip()
        artist = (mi.get("singers") or "").strip() or \
                 (mi.get("artistMap", {}).get("primary_artists", [{}])[0].get("name", "") if isinstance(mi.get("artistMap"), dict) else "")
        if not title or not enc:
            continue
        k = (title + "|" + artist).lower()
        if k in seen:
            continue
        seen.add(k)
        # HTML entities saaf karo
        title = html.unescape(title)
        img = (r.get("image") or "").strip()
        # chhota cover (150x150) -> bada (500x500)
        img = re.sub(r"150x150", "500x500", img)
        out.append({"title": title, "artist": artist, "enc": enc,
                    "image": img, "duration": _mi_duration(mi)})
        if len(out) >= n:
            break
    return out


def jiosaavn_chart(listid, n=50):
    """JioSaavn editorial chart playlist -> [{'title','artist','enc'}].

    listid: JioSaavn playlist id (e.g. Hindi Top 50 = 1134543272).
    Empty list on failure.
    """
    try:
        params = urllib.parse.urlencode({
            "__call": "playlist.getDetails", "listid": str(listid),
            "_format": "json", "_marker": "0",
            "api_version": "4", "ctx": "web6dot0"})
        req = urllib.request.Request(
            "https://www.jiosaavn.com/api.php?" + params,
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.load(resp)
    except Exception:
        return []
    out = []
    for s in (data.get("list") or [])[:n]:
        title = html.unescape((s.get("title") or "").strip())
        mi = s.get("more_info") or {}
        enc = (mi.get("encrypted_media_url") or "").strip()
        artist = ""
        am = mi.get("artistMap") or {}
        pa = am.get("primary_artists") or []
        if pa and isinstance(pa[0], dict):
            artist = html.unescape((pa[0].get("name") or "").strip())
        if not title:
            continue
        out.append({"title": title, "artist": artist, "enc": enc,
                    "duration": _mi_duration(mi)})
    return out


def itunes_chart(country="us", n=50):
    """iTunes Top Songs RSS -> [{'title','artist'}]. No auth needed."""
    try:
        req = urllib.request.Request(
            "https://itunes.apple.com/%s/rss/topsongs/limit=%d/json"
            % (country, n),
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
    except Exception:
        return []
    out = []
    for e in data.get("feed", {}).get("entry", [])[:n]:
        title = ((e.get("im:name") or {}).get("label") or "").strip()
        artist = ((e.get("im:artist") or {}).get("label") or "").strip()
        if title:
            out.append({"title": html.unescape(title),
                        "artist": html.unescape(artist)})
    return out


def youtube_playlist_songs(pl_id, n=100):
    """YouTube playlist -> [{'title','artist'}].

    Trending me JioSaavn chart ke saath combine karne ke liye (Om 2026-10-02).
    yt-dlp flat playlist se har video ka title+uploader -> _clean_music_title.
    Download ke liye title/artist hi kaafi hai (trdl: normal pipeline se
    JioSaavn search karta hai). Fail par []."""
    try:
        proc = subprocess.run(
            [YTDLP, "--no-check-certificate", "--flat-playlist",
             "--print", "%(title)s\t%(uploader)s",
             "https://www.youtube.com/playlist?list=" + pl_id],
            capture_output=True, text=True, timeout=180)
    except Exception:
        return []
    songs, seen = [], set()
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t", 1)
        vt = (parts[0] if parts else "").strip()
        if not vt or vt == "[Deleted video]" or vt == "[Private video]":
            continue
        uploader = parts[1].strip() if len(parts) > 1 else ""
        cleaned = _clean_music_title(vt, uploader)
        if not cleaned:
            continue
        t, a = cleaned
        key = (_norm_name(t), _norm_name(a))
        if not key[0] or key in seen:
            continue
        seen.add(key)
        songs.append({"title": t, "artist": a})
        if len(songs) >= n:
            break
    return songs


def _norm_name(s):
    return "".join(c for c in s.lower() if c.isalnum() or c.isspace()).strip()


def jiosaavn_artist_match(query, min_ctr=0):
    """Query koi artist hai to uska canonical naam, warna None.

    Strict match (group auto-detect ke liye safe): poora naam match ho
    ya query ka har shabd artist ke naam me ho (ya ulta).
    min_ctr: JioSaavn popularity threshold — group me galat match
    (jaise "Good Morning Everyone" naam ka zero-fan artist) se bachne
    ke liye min_ctr=100 use karo.
    """
    try:
        params = urllib.parse.urlencode({
            "__call": "search.getArtistResults", "q": query, "p": "1",
            "n": "3", "_format": "json", "_marker": "0",
            "api_version": "4", "ctx": "web6dot0"})
        req = urllib.request.Request(
            "https://www.jiosaavn.com/api.php?" + params,
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
    except Exception:
        return None
    res = data.get("results") or []
    if not res:
        return None
    name = ((res[0].get("name") or "").strip())
    if not name:
        return None
    nq, nt = _norm_name(query), _norm_name(name)
    if nq and (nq == nt or nq in nt.split() or nt in nq.split()):
        try:
            ctr = int(res[0].get("ctr") or 0)
        except (ValueError, TypeError):
            ctr = 0
        if ctr >= min_ctr:
            return html.unescape(name)
    return None


def jiosaavn_artist_songs(artist_name, n=30):
    """Artist ke top gaane -> [{'title','artist','enc'}]."""
    songs = jiosaavn_search(artist_name, n=max(10, n))
    an = _norm_name(artist_name)
    ranked = []
    for s in songs:
        t = _norm_name(s["title"] + " " + s.get("artist", ""))
        ranked.append((0 if an in t else 1, s))
    ranked.sort(key=lambda x: x[0])
    return [s for _, s in ranked[:n]]


def sanitize_filename(s, maxlen=90):
    """Music player me saaf naam dikhe: junk chars hatao, lamba na ho."""
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", s or "")
    s = re.sub(r"\s+", " ", s).strip().strip(" .")
    return s[:maxlen] or "song"


def tag_audio_file(path, title, artist, image_url=None):
    """MP3/M4A me title/artist (+cover art) tags likho taaki music player
    me sahi naam, artist aur photo dikhe. Fail ho to silent — file waisa hi
    rehta hai. Returns True/False."""
    if not path or not os.path.exists(path):
        return False
    cover_path = None
    if image_url:
        try:
            req = urllib.request.Request(
                image_url, headers={"User-Agent": "Mozilla/5.0"})
            cover_path = path + ".cover.jpg"
            with urllib.request.urlopen(req, timeout=20) as resp, \
                    open(cover_path, "wb") as f:
                shutil.copyfileobj(resp, f)
            if os.path.getsize(cover_path) < 1024:
                raise ValueError("cover too small")
        except Exception:
            cover_path = None
    # temp output: original extension rakho taaki ffmpeg format pehchane
    base, ext = os.path.splitext(path)
    out = base + ".tagged" + (ext or ".m4a")
    try:
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", path]
        if cover_path:
            cmd += ["-i", cover_path, "-map", "0:a", "-map", "1",
                    "-c:a", "copy", "-c:v", "copy",
                    "-disposition:v:0", "attached_pic"]
        else:
            cmd += ["-c", "copy"]
        if title:
            cmd += ["-metadata", f"title={title}"]
        if artist:
            cmd += ["-metadata", f"artist={artist}"]
        cmd.append(out)
        subprocess.run(cmd, capture_output=True, timeout=90)
        if os.path.exists(out) and os.path.getsize(out) > 1024:
            os.replace(out, path)
            return True
        if os.path.exists(out):
            os.remove(out)
    except Exception:
        pass
    finally:
        for p in (cover_path,):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
    return False


def _mi_duration(mi):
    """JioSaavn more_info se duration (seconds) nikalo, int ya None."""
    try:
        d = (mi or {}).get("duration")
        return int(d) if d else None
    except (TypeError, ValueError):
        return None


# Circuit breaker: JioSaavn CDN hamare IP ko throttle kare (kata hua
# download) to lagatar fail par 30 min ke liye JioSaavn skip — caller
# turant YouTube fallback lega, har gaane par retry me time waste nahi.
# Ek safal download streak reset kar deta hai.
# State FILE me hai (js_throttle.json) taaki alag-alag processes
# (listener, precache, rebuild cron) sab ise dekhein.
_JS_FAILS = 0
_JS_SKIP_UNTIL = 0.0
_JS_THROTTLE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "js_throttle.json")


def _throttle_file_until():
    try:
        return float(json.load(open(_JS_THROTTLE_FILE)).get("skip_until", 0))
    except Exception:
        return 0.0


def jiosaavn_usable():
    """False = abhi JioSaavn mat try karo, seedha fallback lo."""
    now = time.time()
    return now >= _JS_SKIP_UNTIL and now >= _throttle_file_until()


def _js_fail():
    # Fail count FILE me hai taaki alag-alag processes (listener, precache,
    # rebuild cron) shared throttle state dekhein.
    try:
        d = json.load(open(_JS_THROTTLE_FILE))
        if not isinstance(d, dict):
            d = {}
    except Exception:
        d = {}
    n = int(d.get("fails", 0) or 0) + 1
    global _JS_FAILS, _JS_SKIP_UNTIL
    _JS_FAILS = n
    d["fails"] = n
    if n >= 3:
        until = time.time() + 1800  # 30 min cooldown (Om 2026-10-03:
        # pichla tareeka hi theek tha — lambi khamoshi se block jaldi khulta hai)
        _JS_SKIP_UNTIL = until
        d["skip_until"] = until
    try:
        json.dump(d, open(_JS_THROTTLE_FILE, "w"))
    except Exception:
        pass


def _js_ok():
    global _JS_FAILS, _JS_SKIP_UNTIL
    _JS_FAILS = 0
    _JS_SKIP_UNTIL = 0.0
    try:
        if os.path.exists(_JS_THROTTLE_FILE):
            os.remove(_JS_THROTTLE_FILE)
    except Exception:
        pass


def jiosaavn_download(enc, workdir, quality="160", title=None, artist=None,
                      image=None, expected_duration=None):
    """Decrypt karke seedha CDN se download karo (yt-dlp nahi chahiye).
    Returns (path, title) or (None, None). Bahut tez: ~2-5 second.

    expected_duration: JioSaavn API se gaane ki asal lambai (seconds).
    Download kat jaye (CDN throttling) to duration chhoti hogi — aise
    me retry karo. 3 attempts, phir None (caller YouTube fallback lega).
    Lagatar throttling par circuit breaker 30 min ke liye JioSaavn skip
    karta hai (jiosaavn_usable()).
    """
    if not jiosaavn_usable():
        return None, None  # circuit open — turant fallback
    url = js_decrypt(enc)
    if not url:
        return None, None
    # quality badlo: _96 -> _160 (ya _320)
    url = re.sub(r"_\d+\.mp4$", f"_{quality}.mp4", url)
    os.makedirs(workdir, exist_ok=True)
    # saaf filename: music player me "js_17908..." ki jagah gaane ka naam
    if title:
        fname = sanitize_filename(
            f"{title} - {artist}" if artist else title) + ".m4a"
    else:
        fname = f"js_{int(time.time())}.m4a"
    path = os.path.join(workdir, fname)
    truncated = False  # network kata to retry; 404/preview par nahi
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                clen = resp.headers.get("Content-Length")
                total = int(clen) if clen and clen.isdigit() else 0
                with open(path, "wb") as f:
                    shutil.copyfileobj(resp, f)
        except Exception:
            truncated = True
            time.sleep(2 * (attempt + 1))
            continue
        got = os.path.getsize(path)
        # 1. aadhi download pakdo: Content-Length se kam aaya to retry
        if total and got < total * 0.98:
            truncated = True
            time.sleep(2 * (attempt + 1))
            continue
        # 2. 50KB se chhota = error page/404 — retry bekar, fallback do
        if got < 50 * 1024:
            os.remove(path)
            return None, None
        if got > MAX_MB * 1024 * 1024:
            os.remove(path)
            return None, None
        # 3. Jab asal lambai pata na ho to 2 min se chhota = toota hua file.
        #    Lekin JioSaavn khud kahe ki gaana 108s ka hai aur utna hi aaya
        #    to wo POORA hai, toota nahi (Om 2026-10-02: chhote bhajan bhi
        #    library me chahiye). YouTube path me expected nahi hota, wahan
        #    120s floor jaari rahega.
        dur = audio_duration(path)
        if dur and dur < 120 and not (expected_duration and
                                      expected_duration < 120):
            os.remove(path)
            return None, None
        # 4. expected se chhota = kata hua download — retry
        if expected_duration and dur and dur < expected_duration * 0.85:
            truncated = True
            time.sleep(2 * (attempt + 1))
            continue
        _js_ok()  # safal download — throttling nahi hai
        # title/artist/cover tags likho (music player me sahi dikhe)
        tag_audio_file(path, title, artist, image)
        return path, None
    if truncated:
        _js_fail()  # lagatar kata to circuit breaker trip karega
    try:
        os.remove(path)
    except OSError:
        pass
    return None, None


def _norm_words(s):
    """Lowercase words, bracketed suffixes like (Jhankar Beats) hatao."""
    s = (s or "").lower()
    s = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return [w for w in s.split() if w]


def title_matches(query, title, artist=""):
    """Kya ye result query se milta hai?

    iTunes/JioSaavn kabhi-kabhi fuzzy match me bilkul alag gaane de dete
    hain (jaise 'tu mera yaar' par 'Goliya'). Aise irrelevant results ko
    filter karne ke liye: query ke shabd title/artist se milne chahiye.
    """
    qw = set(_norm_words(query))
    tw = set(_norm_words(title))
    aw = set(_norm_words(artist))
    if not qw or (not tw and not aw):
        return False
    need = max(2, len(qw) * 0.5)
    if len(qw & tw) >= need or (tw and tw <= qw):
        return True
    if aw and (len(qw & aw) >= need or aw <= qw):
        return True
    return False


def web_song_search(query, n=5):
    """Clean song names via the iTunes Search API (free, no key needed).

    Returns [{'title','artist'}] with proper song/artist names, much cleaner
    than raw YouTube search (which returns lyric videos, 1-hour loops,
    reaction videos...). Empty list on any failure (caller falls back).
    """
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
        # iTunes ke irrelevant fuzzy results hatao (alag gaane na dikhe)
        if not title_matches(query, title, artist):
            continue
        out.append({"title": title, "artist": artist})
        if len(out) >= n:
            break
    return out


_JUNK = ("live", "reaction", "cover", "1 hour", "1hour", "loop",
         "8d audio", "slowed", "sped up", "ringtone", "status",
         "whatsapp", "dj remix", "mashup")
_GOOD_UPLOADER = ("topic", "vevo", "official", "music", "records",
                  "t-series", "sony", "zee", "tips", "saregama")


def _dur_secs(dur):
    m = re.match(r"(?:(\d+):)?(\d+):(\d+)$", dur or "")
    if not m:
        return 0
    h, mn, s = m.groups()
    return (int(h or 0) * 3600) + int(mn) * 60 + int(s)


def indown_video_url(ig_url):
    """indown.io se Instagram video ka direct (proxy) download URL nikalo.

    Returns (server2_url, server1_url) ya (None, None).
    Server2 = indown proxy (referer-locked, sirf hamare server se chalega).
    Fast hai: koi yt-dlp extraction nahi, seedha file.
    """
    import html as _html
    UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
    try:
        jar = {}
        req = urllib.request.Request("https://indown.io/en4", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=25) as r:
            page = r.read().decode("utf-8", "ignore")
            for c in r.headers.get_all("Set-Cookie") or []:
                jar[c.split(";")[0].split("=")[0]] = c.split(";")[0].split("=", 1)[1]
        token = re.search(r'name="_token" value="([^"]+)"', page)
        if not token:
            return None, None
        data = urllib.parse.urlencode({
            "referer": "https://indown.io/en4", "locale": "en",
            "_token": token.group(1), "link": ig_url,
        }).encode()
        req = urllib.request.Request("https://indown.io/download", data=data, headers={
            "User-Agent": UA, "Referer": "https://indown.io/en4",
            "Cookie": "; ".join(f"{k}={v}" for k, v in jar.items()),
            "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=30) as r:
            out = r.read().decode("utf-8", "ignore")
        s2 = re.search(r'href="(https://d\d\.indown\.io/fetch\?url=[^"]+)"', out)
        s1 = re.search(r'href="(https://scontent[^"]+&dl=1)"', out)
        return (_html.unescape(s2.group(1)) if s2 else None,
                _html.unescape(s1.group(1)) if s1 else None)
    except Exception:
        return None, None


def indown_download(ig_url, outpath):
    """indown.io proxy se Instagram video fast download karo. True/False."""
    url, _ = indown_video_url(ig_url)
    if not url:
        return False
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            "Referer": "https://indown.io/"})
        with urllib.request.urlopen(req, timeout=120) as r, open(outpath, "wb") as f:
            shutil.copyfileobj(r, f, 65536)
        return os.path.getsize(outpath) > 50000
    except Exception:
        return False


def yt_best_pick(query):
    """YouTube search -> best song-match {'url','duration_secs','title'} ya None.

    yt_best_audio_url jaisi scoring; saath me duration bhi deta hai taaki
    caller pehle hi dekh sake file Telegram limit me aayegi ya nahi.
    """
    try:
        results = yt_search(query, 8)
    except Exception:
        return None
    if not results:
        return None
    qwords = [w for w in re.findall(r"[a-z0-9]+", query.lower())
              if len(w) >= 3 and w not in ("song", "the", "from", "with")]

    def score(r):
        s = 0
        tl = r["title"].lower()
        ul = (r.get("uploader") or "").lower()
        if any(j in tl for j in _JUNK):
            s -= 50
        if any(g in ul for g in _GOOD_UPLOADER):
            s += 10
        if "official" in tl:
            s += 5
        for w in qwords:
            if w in tl:
                s += 2
        secs = _dur_secs(r.get("duration"))
        if 120 <= secs <= 480:
            s += 5
        elif secs > 600:
            s -= 20
        return s

    results.sort(key=score, reverse=True)
    best = results[0]
    return {"url": f"https://www.youtube.com/watch?v={best['id']}",
            "duration_secs": _dur_secs(best.get("duration")),
            "title": best.get("title", "")}


def yt_best_audio_url(query):
    """YouTube search -> best song-match watch URL.

    Scores results: official/label/topic channels up, lyric-junk
    (live/reaction/1-hour-loop/8d/slowed...) down, song-length
    duration preferred. Returns the watch URL or None.
    """
    pick = yt_best_pick(query)
    return pick["url"] if pick else None


def _title_of(url):
    try:
        r = _run(["--skip-download", "--print", "%(title)s", url], timeout=90)
        line = (r.stdout or "").strip().splitlines()
        return line[0].strip()[:120] if line else "file"
    except Exception:
        return "file"


def download_audio(url, workdir):
    """Download best audio as 128k mp3. Returns (path, title) or (None, None).

    YouTube throttling intermittent hai — 3 attempts (yt-dlp .part file se
    resume karta hai, dobara zero se nahi).
    """
    os.makedirs(workdir, exist_ok=True)
    title = _title_of(url)
    for attempt in range(3):
        try:
            r = _run(["-x", "--audio-format", "mp3", "--audio-quality", "128K",
                      "--embed-metadata",
                      "-o", os.path.join(workdir, "%(id)s.%(ext)s"), url],
                     timeout=900)
        except Exception:
            r = None
        if r is not None and r.returncode == 0:
            mp3s = sorted(glob.glob(os.path.join(workdir, "*.mp3")),
                          key=os.path.getmtime)
            if mp3s:
                path = mp3s[-1]
                sz = os.path.getsize(path)
                if sz > MAX_MB * 1024 * 1024:
                    os.remove(path)
                    return None, None
                # Tooti/adhuri download pakdo: 50KB se chhoti ya 2 min se
                # chhoti "song" file reject (Om ka rule: koi gaana 2 min se
                # chhota nahi hota). Warna 1-sec/30-sec ka kachra bhej jata
                # hai aur cache me ghus jata hai. Retry ho sakta hai.
                bad = sz < 50 * 1024
                if not bad:
                    dur = audio_duration(path)
                    bad = bool(dur) and dur < 120
                if bad:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                else:
                    return path, title
        time.sleep(3 * (attempt + 1))
    return None, None


def download_video(url, workdir):
    """Download video capped at 48MB, trying 720p -> 480p -> 360p.
    Returns (path, title) or (None, None)."""
    os.makedirs(workdir, exist_ok=True)
    title = _title_of(url)
    for height in (720, 480, 360):
        # wipe previous attempt
        for f in glob.glob(os.path.join(workdir, "*")):
            try:
                os.remove(f)
            except OSError:
                pass
        fmt = (f"bv*[height<={height}]+ba/b[height<={height}]/b")
        try:
            r = _run(["-f", fmt, "--merge-output-format", "mp4",
                      "-o", os.path.join(workdir, "video.%(ext)s"), url],
                     timeout=1200)
        except Exception:
            return None, None
        if r.returncode != 0:
            return None, None
        vids = [f for f in glob.glob(os.path.join(workdir, "video.*"))
                if not f.endswith(".part")]
        if not vids:
            return None, None
        path = vids[0]
        if os.path.getsize(path) <= MAX_MB * 1024 * 1024:
            return path, title
    return None, None


def fresh_workdir(base):
    d = os.path.join(base, f"dl_{int(time.time() * 1000)}")
    os.makedirs(d, exist_ok=True)
    return d


def wipe(d):
    shutil.rmtree(d, ignore_errors=True)


def audio_duration(path):
    """Audio file ki duration (seconds) — Telegram player me 0:00 na dikhe."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30)
        return int(float(r.stdout.strip()))
    except Exception:
        return 0


_SPOTIFY_TOKEN = {"token": None, "exp": 0}


def _spotify_page_info(url):
    """Spotify track link -> (title, artist) from the page's og tags.

    No credentials needed (public page metadata). VK-style bots do the
    same. Returns None on any failure.
    """
    m = re.search(r"open\.spotify\.com/(?:\w+/)?track/([A-Za-z0-9]+)", url)
    if not m:
        return None
    try:
        req = urllib.request.Request(
            f"https://open.spotify.com/track/{m.group(1)}",
            headers={"User-Agent":
                     "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", "replace")
        t = re.search(r'<meta property="og:title" content="([^"]+)"', html)
        d = re.search(r'<meta property="og:description" content="([^"]+)"',
                      html)
        title = t.group(1).strip() if t else ""
        artist = ""
        if d:
            # "Artist, Artist · Title · Song · Year" -> first part = artists
            parts = [p.strip() for p in d.group(1).split("·")]
            if parts and parts[0].lower() != title.lower():
                artist = parts[0]
        return (title, artist) if title else None
    except Exception:
        return None


_ARTIST_JUNK = re.compile(
    r"(official|music|video|audio|songs?|superhit|ghazal|lyrics?|full|hd|4k|"
    r"mp3|dj|remix|slowed|reverb|live|concert|mashup|jukebox|playlist|best|"
    r"top\s*\d*|hits?|collection|evergreen|romantic|sad|party|dance|trending|"
    r"viral|original|version|episode|vol\.?|part\s*\d+|juke\s*box)",
    re.IGNORECASE)


def _looks_like_artist(s):
    """Kya ye text ek artist ka naam lag raha hai (na ki 'Superhit Ghazal Song')?"""
    s = (s or "").strip()
    if not s or len(s) > 40:
        return False
    if _ARTIST_JUNK.search(s):
        return False
    words = s.split()
    return 1 <= len(words) <= 5


def _clean_music_title(title, author):
    """YouTube/oEmbed titles saaf karo -> (title, artist).

    - "Artist - Song (Official Video)" -> artist alag
    - "Song | Artist | Junk" (ghazal/purane uploads) -> beech wala artist
    - "Roman Title देवनागरी Title" -> doosri script ka dohraav hatao
      (Latin shabd bache hon tabhi; sirf Hindi ho to rehne do)
    """
    title = (title or "").strip()
    author = (author or "").strip()
    # "Artist - Song (Official Video)" -> artist alag, title saaf
    if author and title.lower().startswith(author.lower() + " - "):
        title = title[len(author) + 3:].strip()
    # "(Official Video/Audio/...)" jaise junk groups kahin bhi hon, hatao
    title = re.sub(r"\s*\((Official\s+)?(Music\s+)?(Video|Audio)[^)]*\)",
                   "", title, flags=re.IGNORECASE).strip()
    title = re.sub(r"\s*\[(Official\s+)?(Music\s+)?(Video|Audio)[^\]]*\]",
                   "", title, flags=re.IGNORECASE).strip()
    artist = author
    # "Song | Artist | Junk" pattern
    if "|" in title:
        parts = [p.strip() for p in title.split("|") if p.strip()]
        if parts:
            title = parts[0]
            if len(parts) > 1 and _looks_like_artist(parts[1]):
                artist = parts[1]
    # Devanagari dohraav hatao ("Yeh Baatein ... ये बातें ...")
    no_deva = re.sub(r"[\u0900-\u097F]+", " ", title)
    no_deva = re.sub(r"\s+", " ", no_deva).strip()
    no_deva = re.sub(r"\(\s*\)", "", no_deva).strip()
    no_deva = re.sub(r"\[\s*\]", "", no_deva).strip()
    if len(no_deva.split()) >= 2:
        title = no_deva
    title = re.sub(r"\s+", " ", title).strip()
    if not title:
        return None
    return (title, artist)


def ytmusic_track_info(url):
    """music.youtube.com link -> (title, artist) via YouTube oEmbed.

    No credentials, no yt-dlp, ~1 sec. Returns None on any failure,
    tab caller seedha yt-dlp wala rasta le sake.
    """
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", url)
    if not m:
        return None
    vid = m.group(1)
    title, artist = "", ""
    for _ in range(3):  # oEmbed kabhi-kabhi 429 deta hai — retry karo
        try:
            oembed = ("https://www.youtube.com/oembed?url=" +
                      urllib.parse.quote(
                          f"https://www.youtube.com/watch?v={vid}", safe="") +
                      "&format=json")
            req = urllib.request.Request(
                oembed,
                headers={"User-Agent":
                         "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            title = html.unescape(data.get("title", "")).strip()
            artist = html.unescape(data.get("author_name", "")).strip()
            if title:
                break
        except Exception:
            time.sleep(2)
    if not title:
        return None
    return _clean_music_title(title, artist)


def ytmusic_track_info_fallback(url):
    """oEmbed fail ho jaye to yt-dlp se video title/uploader nikalo.

    handle_link me music_platform_info ke None dene par istemal hota hai —
    taaki seedhe dumb download ki jagah smart song pipeline (JioSaavn
    fast path -> YouTube search) chal sake.
    """
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", url)
    if not m:
        return None
    try:
        r = _run(["--skip-download", "--print", "%(title)s\t%(uploader)s",
                  f"https://www.youtube.com/watch?v={m.group(1)}"],
                 timeout=90)
        if r.returncode != 0:
            return None
        lines = (r.stdout or "").strip().splitlines()
        if not lines:
            return None
        parts = lines[0].split("\t")
        title = html.unescape(parts[0]).strip() if parts else ""
        author = html.unescape(parts[1]).strip() if len(parts) > 1 else ""
        if not title:
            return None
        return _clean_music_title(title, author)
    except Exception:
        return None


def jiosaavn_url_info(url):
    """jiosaavn.com song link -> (title, artist).

    URL slug me hi gaane ka naam hota hai:
    /song/kesariya-from-brahmastra/xxx -> "kesariya from brahmastra".
    Song pipeline (search + title_matches) sahi gaana dhundh lega.
    """
    m = re.search(r"(?:jio)?saavn\.com/song/([^/?#]+)", url, re.IGNORECASE)
    if not m:
        return None
    name = m.group(1).replace("-", " ").strip()
    return (name, "") if name else None


def gaana_track_info(url):
    """gaana.com song link -> (title, artist) from <title> tag.

    Title format: "Download Kesariya Song -Brahmastra by Pritam | ..."
    """
    if "gaana.com/song/" not in url.lower():
        return None
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent":
                     "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            html_text = resp.read().decode("utf-8", "replace")
        m = re.search(r"<title>\s*Download\s+(.+?)\s+Song\b", html_text,
                      re.IGNORECASE)
        if not m:
            return None
        title = html.unescape(m.group(1)).strip()
        # title ke baad "by Artist" ho to artist nikalo
        artist = ""
        am = re.search(r"\bby\s+([A-Za-z .,&]+?)(?:\s*\||\s*$)",
                       html_text[m.end():m.end() + 120])
        if am:
            artist = html.unescape(am.group(1)).strip(" |")
        return (title, artist) if title else None
    except Exception:
        return None


def music_platform_info(url):
    """Kisi bhi supported music link -> (title, artist).

    Spotify / YT Music / JioSaavn / Gaana. None = pehchana nahi.
    """
    host = url.lower()
    if "open.spotify.com" in host:
        return spotify_track_info(url)
    if "music.youtube.com" in host:
        return ytmusic_track_info(url)
    if "saavn.com" in host:
        return jiosaavn_url_info(url)
    if "gaana.com" in host:
        return gaana_track_info(url)
    return None


def playlist_info(url):
    """Playlist link -> (label, [{'title','artist','enc','image'}]).

    YouTube / JioSaavn / Spotify playlist. None = samajh nahi aaya.
    JioSaavn items me 'enc' hota hai = fast direct download, search skip.
    """
    low = url.lower()
    try:
        if "youtube.com" in low or "youtu.be" in low:
            return _youtube_playlist_info(url)
        if "saavn.com" in low:
            return _jiosaavn_playlist_info(url)
        if "open.spotify.com" in low:
            return _spotify_playlist_info(url)
    except Exception:
        return None
    return None


last_playlist_error = ""  # playlist_info fail ki wajah (UI message ke liye)


def _youtube_playlist_info(url):
    global last_playlist_error
    last_playlist_error = ""
    m = re.search(r"[?&]list=([A-Za-z0-9_-]+)", url)
    if not m:
        return None
    lid = m.group(1)
    # NOTE: BASE me --no-playlist hai, isliye yahan manual args.
    r = subprocess.run(
        [YTDLP, "--no-check-certificate", "--flat-playlist",
         "--print", "%(playlist_title)s\t%(title)s\t%(uploader)s",
         "--no-warnings",
         "https://www.youtube.com/playlist?list=" + lid],
        capture_output=True, text=True, timeout=180)
    err = (r.stderr or "").lower()
    if "does not exist" in err or "private" in err:
        # Private/delete playlist — YouTube bina login ke nahi dikhata
        last_playlist_error = "private"
        return None
    songs, label = [], "YouTube Playlist"
    for line in (r.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        pl_title, title, uploader = (p.strip() for p in parts[:3])
        if pl_title and label == "YouTube Playlist":
            label = pl_title
        if not title or title in ("[Private video]", "[Deleted video]"):
            continue
        artist = re.sub(r"\s*-\s*Topic$", "", uploader).strip()
        songs.append({"title": title, "artist": artist})
    return (label, songs) if songs else None


def _jiosaavn_playlist_info(url):
    low = url.lower()
    typ = "album" if "/album/" in low else "playlist"
    tok = url.rstrip("/").split("/")[-1].split("?")[0].strip()
    if not tok:
        return None
    songs, label, total, p = [], "JioSaavn Playlist", 0, 1
    while True:
        params = urllib.parse.urlencode({
            "__call": "webapi.get", "token": tok, "type": typ,
            "p": str(p), "n": "50", "includeMetaTags": "0",
            "api_version": "4", "ctx": "web6dot0",
            "_format": "json", "_marker": "0"})
        req = urllib.request.Request(
            "https://www.jiosaavn.com/api.php?" + params,
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.load(resp)
        if p == 1:
            label = data.get("title") or label
            try:
                total = int(str(data.get("list_count") or "0"))
            except ValueError:
                total = 0
        items = data.get("list") or []
        if not items:
            break
        for s in items:
            title = html.unescape((s.get("title") or "").strip())
            if not title:
                continue
            mi = s.get("more_info") or {}
            enc = (mi.get("encrypted_media_url") or "").strip()
            artist = ""
            am = mi.get("artistMap") or {}
            pa = am.get("primary_artists") or []
            if pa and isinstance(pa[0], dict):
                artist = html.unescape((pa[0].get("name") or "").strip())
            if not artist:
                sub = html.unescape((s.get("subtitle") or "").strip())
                artist = sub.split(" - ")[0].strip()
            img = re.sub(r"150x150", "500x500",
                         (s.get("image") or "").strip())
            songs.append({"title": title, "artist": artist,
                          "enc": enc, "image": img})
        if (total and len(songs) >= total) or len(items) < 50 or p >= 20:
            break
        p += 1
        time.sleep(0.3)
    return (label, songs) if songs else None


def _spotify_playlist_info(url):
    m = re.search(r"/playlist/([A-Za-z0-9]{22})", url)
    if not m:
        return None
    req = urllib.request.Request(
        "https://open.spotify.com/embed/playlist/" + m.group(1),
        headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        raw = resp.read().decode("utf-8", "ignore")
    label = "Spotify Playlist"
    try:  # playlist ka naam canonical page ke og:title se
        req2 = urllib.request.Request(
            "https://open.spotify.com/playlist/" + m.group(1),
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req2, timeout=15) as resp2:
            raw2 = resp2.read().decode("utf-8", "ignore")
        lm = re.search(r'<meta property="og:title" content="([^"]{1,120})"',
                       raw2)
        if lm:
            label = html.unescape(lm.group(1))
    except Exception:
        pass
    songs, seen = [], set()
    for t, a in re.findall(
            r'"uri":"spotify:track:[A-Za-z0-9]{22}","uid":"[^"]*",'
            r'"title":"((?:[^"\\]|\\.)*)","subtitle":"((?:[^"\\]|\\.)*)"',
            raw):
        title = html.unescape(
            t.replace('\\"', '"').replace("\\\\", "\\")
             .replace("\\/", "/")).strip()
        artist = html.unescape(
            a.replace('\\"', '"').replace("\\\\", "\\")
             .replace("\\/", "/")).strip()
        if not title:
            continue
        k = (title + "|" + artist).lower()
        if k in seen:
            continue
        seen.add(k)
        songs.append({"title": title, "artist": artist})
    return (label, songs) if songs else None


def spotify_track_info(url):
    """Spotify track link -> (title, artist).

    1) page og tags (no credentials), 2) official Web API if spotify.json
    has client_id/client_secret. Returns None when both fail.
    """
    info = _spotify_page_info(url)
    if info:
        return info
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "spotify.json")
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
        cid, sec = cfg.get("client_id"), cfg.get("client_secret")
        if not cid or not sec:
            return None
    except (OSError, ValueError):
        return None
    m = re.search(r"open\.spotify\.com/(?:\w+/)?track/([A-Za-z0-9]+)", url)
    if not m:
        return None
    tid = m.group(1)
    try:
        now = time.time()
        if _SPOTIFY_TOKEN["token"] is None or now >= _SPOTIFY_TOKEN["exp"]:
            basic = urllib.parse.quote(cid) + ":" + urllib.parse.quote(sec)
            import base64
            req = urllib.request.Request(
                "https://accounts.spotify.com/api/token",
                data=b"grant_type=client_credentials",
                headers={"Authorization": "Basic " + base64.b64encode(
                    f"{cid}:{sec}".encode()).decode()})
            with urllib.request.urlopen(req, timeout=20) as resp:
                tok = json.load(resp)
            _SPOTIFY_TOKEN["token"] = tok["access_token"]
            _SPOTIFY_TOKEN["exp"] = now + tok.get("expires_in", 3600) - 60
        req = urllib.request.Request(
            f"https://api.spotify.com/v1/tracks/{tid}",
            headers={"Authorization": "Bearer " + _SPOTIFY_TOKEN["token"]})
        with urllib.request.urlopen(req, timeout=20) as resp:
            tr = json.load(resp)
        title = tr.get("name", "")
        artists = ", ".join(a.get("name", "") for a in tr.get("artists", []))
        if title:
            return title, artists
        return None
    except Exception:
        return None


def locked_json_update(path, fn):
    """JSON dict ka read-modify-write, exclusive file lock ke saath.

    (Om 2026-10-02, Filter duplicate bug): listener + rebuild/library worker
    ALAG processes me ye file likhte hain. Bina lock ke ek ka save doosre
    ki entries uda deta tha (race) — phir smart duplicate check andha ho
    jata tha aur gaana dobara download ho jata tha.
    """
    import fcntl
    lock_path = path + ".lock"
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            try:
                with open(path, encoding="utf-8") as f:
                    d = json.load(f)
                    if not isinstance(d, dict):
                        d = {}
            except (OSError, ValueError):
                d = {}
            fn(d)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
            os.replace(tmp, path)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
