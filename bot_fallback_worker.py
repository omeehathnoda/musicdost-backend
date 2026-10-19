#!/usr/bin/env python3
"""Bot Fallback Worker (Om 2026-10-19).

Render par _fresh_verified_media fail ho to request pending_downloads.json
me jati hai (GitHub par). Ye worker VM par chalta hai (jahan YouTube chalta
hai), har pending ko download karke channel me dalta hai.

Flow:
1. GitHub se pending_downloads.json lao
2. Har pending ke liye:
   - dl_engine.yt_best_pick + download_audio (VM par YouTube works)
   - Strict check: title/artist/duration (same logic as backend)
   - Channel me sendAudio (via dtg.api_multipart)
   - GitHub tg_cache.json me file_id add karo
   - pending_downloads.json se hatao
3. Dono files GitHub par push karo

Cron: har 5 minute me chalao (musicdost-bot-fallback).
"""

import json
import os
import re
import sys
import tempfile
import time
import urllib.request

# Paths
BASE = os.path.dirname(os.path.abspath(__file__))
DL_BIN = os.path.expanduser("~/workspace/custom-connectors/custom.legacy-downloader/bin")
GH_BIN = os.path.expanduser("~/workspace/skills/github/bin")
TG_CACHE_LOCAL = os.path.join(BASE, "tg_cache.json")

PENDING_URL = "https://raw.githubusercontent.com/omeehathnoda/musicdost-backend/main/pending_downloads.json"
TG_CACHE_URL = "https://raw.githubusercontent.com/omeehathnoda/musicdost-backend/main/tg_cache.json"

CACHE_CHANNEL = "-1003960695955"  # Songs library (same as backend)

# Strict matching (backend ke _report_* functions ka copy)
_REPORT_JUNK = ("cover", "remix", "karaoke", "instrumental", "lofi",
                "slowed", "sped", "amapiano", "acoustic", "unplugged",
                "reprise", "tribute", "mashup", "dj ", "version", "8d",
                "1 hour", "ringtone", "whatsapp status")


def _report_words(s):
    return [w for w in re.findall(r"[a-z0-9]+", (s or "").lower())
            if len(w) > 1 and w != "the"]


def _report_yt_artist_ok(title, artist, pick):
    """YouTube pick ka strict artist check."""
    aw = _report_words(artist)
    if not aw:
        return True
    pt = (pick.get("title") or "").lower()
    ul = (pick.get("uploader") or "").lower()
    combined = pt + " " + ul
    return any(w in combined for w in aw)


def _report_expected_duration(title, artist):
    """iTunes se official duration."""
    try:
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


def fetch_json(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except Exception as e:
        print(f"fetch fail {url}: {e}")
        return None


def gh_upload(local_path, remote_name):
    """GitHub par file push karo."""
    try:
        import subprocess
        r = subprocess.run(
            ["python3", "gh.py", "upload", "omeehathnoda",
             "musicdost-backend", local_path, remote_name],
            cwd=GH_BIN, capture_output=True, text=True, timeout=90)
        return r.returncode == 0
    except Exception as e:
        print(f"gh upload fail: {e}")
        return False


def process_one(dl_engine, dtg, title, artist, key):
    """Ek pending song ko download karke channel me dalo. Returns (file_id, message_id) ya (None, None)."""
    print(f"Processing: {title} - {artist}")
    
    # 1. YouTube se dhoondo (VM par chalta hai)
    exp = _report_expected_duration(title, artist)
    pick = None
    for q in (f"{title} {artist} song",
              f"{artist} {title} official audio",
              f"{title} {artist} official"):
        try:
            pick = dl_engine.yt_best_pick(q, artist=artist)
        except Exception:
            continue
        if not pick or pick.get("duration_secs", 0) > 2700:
            pick = None
            continue
        pt = (pick.get("title") or "").lower()
        if any(j in pt for j in _REPORT_JUNK):
            pick = None
            continue
        if not _report_yt_artist_ok(title, artist, pick):
            pick = None
            continue
        psecs = pick.get("duration_secs") or 0
        if exp and abs(psecs - exp) > 2.5:
            pick = None
            continue
        break  # mil gaya
    
    if not pick:
        print(f"  No suitable YouTube pick found")
        return None, None
    
    print(f"  Pick: {pick.get('title')} ({pick.get('duration_secs')}s)")
    
    # 2. Download karo
    tmpdir = tempfile.mkdtemp(prefix="botfb_")
    try:
        yp, _ = dl_engine.download_audio(pick["url"], tmpdir)
        if not yp or not os.path.exists(yp):
            print(f"  Download failed")
            return None, None
        
        # Duration verify karo
        if exp:
            try:
                got = dl_engine.audio_duration(yp)
                if got and abs(got - float(exp)) > 2.5:
                    print(f"  Duration mismatch: got {got}, exp {exp}")
                    return None, None
            except Exception:
                pass
        
        # Tag karo
        try:
            dl_engine.tag_audio_file(yp, title, artist)
        except Exception:
            pass
        
        # 3. Channel me upload karo
        try:
            result = dtg.api_multipart(
                'sendAudio',
                {'chat_id': CACHE_CHANNEL, 'title': title, 'performer': artist},
                'audio', yp)
            d = json.loads(result) if isinstance(result, str) else result
            if d.get("ok"):
                msg = d["result"]
                aud = msg.get("audio", {})
                fid = aud.get("file_id")
                mid = msg.get("message_id")
                print(f"  Uploaded! mid={mid}")
                return fid, mid
            else:
                print(f"  Upload failed: {str(d)[:100]}")
                return None, None
        except Exception as e:
            print(f"  Upload error: {e}")
            return None, None
    finally:
        try:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


def main():
    print(f"Bot Fallback Worker starting at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    # dl_engine aur dtg import karo
    sys.path.insert(0, DL_BIN)
    try:
        import dl_engine
        import dtg
    except Exception as e:
        print(f"Import fail: {e}")
        return
    
    # Pending list lao
    pending = fetch_json(PENDING_URL)
    if not pending:
        print("No pending downloads (or fetch failed)")
        return
    if not isinstance(pending, list):
        print(f"Invalid pending format: {type(pending)}")
        return
    
    print(f"Found {len(pending)} pending downloads")
    if not pending:
        return
    
    # tg_cache lao (local copy update karenge)
    try:
        with open(TG_CACHE_LOCAL) as f:
            tg_cache = json.load(f)
    except Exception:
        tg_cache = fetch_json(TG_CACHE_URL) or {}
    
    remaining = []
    updated_cache = False
    
    for entry in pending:
        if not isinstance(entry, dict):
            continue
        title = entry.get("title", "")
        artist = entry.get("artist", "")
        key = entry.get("key", "")
        if not title or not key:
            continue
        
        # Pehle se cache me hai to skip (kisi aur ne kar diya)
        if key in tg_cache:
            print(f"Already cached, skipping: {key}")
            continue
        
        try:
            fid, mid = process_one(dl_engine, dtg, title, artist, key)
            if fid:
                # Cache me add karo
                tg_cache[key] = {"file_id": fid, "message_id": mid or 0}
                updated_cache = True
                print(f"  Cached: {key}")
            else:
                # Fail hua to rehne do (agli baar phir try hoga)
                # Par 3 se zyada fail ho to hatao (infinite loop se bacho)
                fails = entry.get("fails", 0) + 1
                if fails >= 3:
                    print(f"  Giving up after 3 fails: {key}")
                else:
                    entry["fails"] = fails
                    remaining.append(entry)
        except Exception as e:
            print(f"  Error processing {key}: {e}")
            entry["fails"] = entry.get("fails", 0) + 1
            if entry["fails"] < 3:
                remaining.append(entry)
        
        # Har gaane ke baad thoda gap (rate limit se bacho)
        time.sleep(5)
    
    # Files update karo aur push karo
    if updated_cache:
        try:
            with open(TG_CACHE_LOCAL, "w") as f:
                json.dump(tg_cache, f)
            if gh_upload(TG_CACHE_LOCAL, "tg_cache.json"):
                print("tg_cache.json pushed to GitHub")
            else:
                print("tg_cache.json push FAILED")
        except Exception as e:
            print(f"Cache save fail: {e}")
    
    # Pending list update karo
    try:
        with open(os.path.join(BASE, "pending_downloads.json"), "w") as f:
            json.dump(remaining, f)
        if gh_upload(os.path.join(BASE, "pending_downloads.json"), "pending_downloads.json"):
            print(f"pending_downloads.json pushed ({len(remaining)} remaining)")
        else:
            print("pending_downloads.json push FAILED")
    except Exception as e:
        print(f"Pending save fail: {e}")
    
    print("Done")


if __name__ == "__main__":
    main()
