import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.parse

webroot = os.environ.get("ICECAST_WEBROOT", "/usr/share/icecast2/web")
channel_username = os.environ["TELEGRAM_CHANNEL"]
history_path = os.environ.get("PLAYING_HISTORY_PATH", "/opt/radio/cache/.history.json")
history_keep = 2  # current + 1 previous, to safely cover crossfade overlap


def update_history_and_cleanup(current_path):
    history = []
    if os.path.exists(history_path):
        try:
            with open(history_path) as f:
                history = json.load(f)
        except Exception:
            history = []
    if current_path in history:
        history.remove(current_path)
    history.append(current_path)
    while len(history) > history_keep:
        old_path = history.pop(0)
        if old_path == current_path:
            continue
        for candidate in (old_path, old_path.rsplit(".", 1)[0] + ".json"):
            try:
                os.remove(candidate)
            except OSError:
                pass
    tmp = history_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(history, f)
    os.replace(tmp, history_path)


discogs_token = os.environ.get("DISCOGS_TOKEN", "")


def try_discogs_lookup(artist, title, fallback_query=""):
    if not discogs_token:
        return None
    query_text = f"{artist} {title}".strip() or fallback_query.strip()
    if not query_text:
        return None
    query = urllib.parse.quote(query_text)
    url = f"https://api.discogs.com/database/search?q={query}&type=release&per_page=1"
    # Unauthenticated search requests silently return "" for cover_image/thumb
    # on every result (confirmed against several well-known releases) -- the
    # search itself and its text fields work fine either way, but a token is
    # required to actually get image URLs back.
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "MusicmaniaRadio/1.0",
            "Authorization": f"Discogs token={discogs_token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        results = data.get("results") or []
        if not results:
            return None
        result = results[0]

        found_artist, found_title = "", ""
        release_title = result.get("title") or ""
        if " - " in release_title:
            found_artist, found_title = release_title.split(" - ", 1)
        else:
            found_title = release_title

        cover_bytes = None
        img_url = result.get("cover_image") or result.get("thumb")
        if img_url:
            try:
                img_req = urllib.request.Request(img_url, headers={"User-Agent": "MusicmaniaRadio/1.0"})
                with urllib.request.urlopen(img_req, timeout=6) as img_resp:
                    cover_bytes = img_resp.read()
            except Exception as e:
                print(f"discogs cover fetch failed: {e}", file=sys.stderr)

        return {
            "artist": found_artist.strip(),
            "title": found_title.strip(),
            "cover": cover_bytes,
        }
    except Exception as e:
        print(f"discogs lookup failed: {e}", file=sys.stderr)
        return None


def main():
    if len(sys.argv) < 2:
        return
    path = sys.argv[1]
    if not os.path.exists(path):
        print(f"file gone, skipping publish: {path}", file=sys.stderr)
        return
    update_history_and_cleanup(path)
    track_id = os.path.splitext(os.path.basename(path))[0]
    artist = ""
    title = ""
    duration = None
    cover_written = False
    data = None
    try:
        import mutagen
        from mutagen.flac import FLAC
        from mutagen.id3 import ID3
        from mutagen.mp4 import MP4
        from mutagen.easyid3 import EasyID3

        f = None
        try:
            f = mutagen.File(path)
        except Exception:
            f = None
        if f is None:
            # Some FLAC rips have a bogus/garbled ID3v2 tag glued onto the
            # front (from old ripping tools), which trips up mutagen's
            # generic format auto-detection even though the real, correct
            # tags -- and cover art -- are sitting right there in the
            # native FLAC block.
            try:
                f = FLAC(path)
            except Exception:
                f = None
        if f is not None and getattr(f, "info", None) is not None:
            duration = getattr(f.info, "length", None)
        if isinstance(f, FLAC) and f.pictures:
            data = f.pictures[0].data
        if f is not None and f.tags:
            try:
                artist = (f.tags.get("artist") or [""])[0]
                title = (f.tags.get("title") or [""])[0]
            except Exception:
                pass
        if f is not None and not artist and not title:
            try:
                easy = EasyID3(path)
                artist = artist or (easy.get("artist") or [""])[0]
                title = title or (easy.get("title") or [""])[0]
            except Exception:
                pass
        if f is not None:
            try:
                tags = ID3(path)
                apics = tags.getall("APIC")
                if apics and not data:
                    data = apics[0].data
            except Exception:
                pass
            if data is None and isinstance(f, MP4):
                covr = f.tags.get("covr") if f.tags else None
                if covr:
                    data = bytes(covr[0])
                if f.tags:
                    artist = artist or (f.tags.get("\xa9ART") or [""])[0]
                    title = title or (f.tags.get("\xa9nam") or [""])[0]
    except Exception as e:
        print(f"metadata extraction failed: {e}", file=sys.stderr)

    # queue_filler.py already captured whatever Telegram itself knew about
    # this file (its own title/performer fields) at download time, as a
    # fallback for exactly this situation -- our own tag extraction failing.
    # It's sitting right next to the audio file; use it before reaching for
    # an external API.
    sidecar_query = ""
    sidecar_path = os.path.splitext(path)[0] + ".json"
    if not artist or not title:
        try:
            with open(sidecar_path) as sf:
                sidecar = json.load(sf)
            artist = artist or (sidecar.get("artist") or "")
            title = title or (sidecar.get("title") or "")
            sidecar_query = f"{sidecar.get('artist', '')} {sidecar.get('title', '')}".strip()
        except Exception:
            pass

    if not data or not artist or not title:
        discogs = try_discogs_lookup(artist, title, fallback_query=sidecar_query)
        if discogs:
            artist = artist or discogs["artist"]
            title = title or discogs["title"]
            if not data:
                data = discogs["cover"]

    if data:
        tmp_cover = os.path.join(webroot, "cover.jpg.tmp")
        with open(tmp_cover, "wb") as out:
            out.write(data)
        os.replace(tmp_cover, os.path.join(webroot, "cover.jpg"))
        cover_written = True

    now = int(time.time())
    info = {
        "artist": artist or "",
        "title": title or "",
        "cover": f"/radio/cover.jpg?t={now}" if cover_written else None,
        "link": f"https://t.me/{channel_username}/{track_id}" if track_id.isdigit() else None,
        "duration": round(duration) if duration else None,
        "started_at": now,
        "updated": now,
    }
    tmp_path = os.path.join(webroot, "nowplaying.json.tmp")
    with open(tmp_path, "w") as f:
        json.dump(info, f)
    os.replace(tmp_path, os.path.join(webroot, "nowplaying.json"))

main()
