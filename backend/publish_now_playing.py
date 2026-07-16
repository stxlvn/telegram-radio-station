import json
import os
import re
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
youtube_api_key = os.environ.get("YOUTUBE_API_KEY", "")

# Same filtering as the site used to do client-side, moved here so the
# lookup happens once per track instead of once per listener's browser --
# with search.list costing 100 quota units per call and tracks changing
# every few minutes, doing this per-listener blew through the free daily
# quota (10,000 units) within hours; per-track is the only way this fits
# even loosely within budget.
YOUTUBE_AUDIO_ONLY_HINT_RE = re.compile(
    r"\b(audio|аудио|lyric|lyrics|lyric video|текст песни|static|cover art|provided to youtube)\b",
    re.IGNORECASE,
)
YOUTUBE_TOPIC_CHANNEL_RE = re.compile(r"- Topic$", re.IGNORECASE)


def _find_youtube_video_id_via_api(artist, title):
    if not youtube_api_key:
        return None
    query = urllib.parse.quote(f"{artist} {title} official video")
    url = f"https://www.googleapis.com/youtube/v3/search?part=snippet&type=video&maxResults=5&q={query}&key={youtube_api_key}"
    try:
        with urllib.request.urlopen(url, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        # Notably: search.list's free quota (10,000 units/day, 100 units per
        # call) runs out partway through most days at this track-change
        # rate -- every subsequent call fails with HTTP 429 until the
        # quota resets, which is exactly what the yt-dlp fallback below is
        # for.
        print(f"youtube api search failed: {e}", file=sys.stderr)
        return None
    items = [item for item in (data.get("items") or []) if item.get("id", {}).get("videoId")]
    for item in items:
        snippet = item.get("snippet") or {}
        video_title = snippet.get("title") or ""
        channel = snippet.get("channelTitle") or ""
        if not YOUTUBE_AUDIO_ONLY_HINT_RE.search(video_title) and not YOUTUBE_TOPIC_CHANNEL_RE.search(channel):
            return item["id"]["videoId"]
    return items[0]["id"]["videoId"] if items else None


YTDLP_BIN = os.environ.get("YTDLP_BIN", "yt-dlp")


def _find_youtube_video_id_via_ytdlp(artist, title):
    query = f"ytsearch5:{artist} {title} official video"
    try:
        result = subprocess.run(
            [YTDLP_BIN, query, "--flat-playlist", "--dump-json", "--no-warnings"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception as e:
        print(f"yt-dlp search failed: {e}", file=sys.stderr)
        return None
    if result.returncode != 0:
        print(f"yt-dlp search error: {result.stderr[:300]}", file=sys.stderr)
        return None
    items = []
    for line in result.stdout.splitlines():
        try:
            item = json.loads(line)
        except Exception:
            continue
        if item.get("id"):
            items.append(item)
    for item in items:
        video_title = item.get("title") or ""
        channel = item.get("channel") or item.get("uploader") or ""
        if not YOUTUBE_AUDIO_ONLY_HINT_RE.search(video_title) and not YOUTUBE_TOPIC_CHANNEL_RE.search(channel):
            return item["id"]
    return items[0]["id"] if items else None


def find_youtube_video_id(artist, title):
    if not artist or not title:
        return None
    # Official API first (sanctioned, quota-limited); yt-dlp only as a
    # fallback once that quota is exhausted for the day, not the primary
    # path -- yt-dlp works by reverse-engineering YouTube's internal,
    # unofficial endpoints rather than their public API, which is less
    # stable and sits in more of a grey area against their terms, but it's
    # still only ever used for search/metadata here, never to fetch or
    # store the actual video/audio content itself.
    return _find_youtube_video_id_via_api(artist, title) or _find_youtube_video_id_via_ytdlp(artist, title)


def try_discogs_lookup(artist, title, fallback_query=""):
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

    video_id = find_youtube_video_id(artist, title) if (artist and title) else None

    now = int(time.time())
    info = {
        "artist": artist or "",
        "title": title or "",
        "cover": f"/radio/cover.jpg?t={now}" if cover_written else None,
        "link": f"https://t.me/{channel_username}/{track_id}" if track_id.isdigit() else None,
        "duration": round(duration) if duration else None,
        "video_id": video_id,
        "started_at": now,
        "updated": now,
    }
    tmp_path = os.path.join(webroot, "nowplaying.json.tmp")
    with open(tmp_path, "w") as f:
        json.dump(info, f)
    os.replace(tmp_path, os.path.join(webroot, "nowplaying.json"))

main()
