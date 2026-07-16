import asyncio
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request

api_id = int(os.environ["TELEGRAM_API_ID"])
api_hash = os.environ["TELEGRAM_API_HASH"]
session_path = os.environ.get("TELEGRAM_SESSION_PATH", "/opt/radio/session")
index_path = os.environ.get("TRACK_INDEX_PATH", "/opt/radio/track_index.json")
excluded_path = os.environ.get("EXCLUDED_IDS_PATH", "/opt/radio/excluded_ids.json")
play_history_path = os.environ.get("PLAY_HISTORY_PATH", "/opt/radio/play_history.json")
queue_dir = os.environ.get("RADIO_QUEUE_DIR", "/opt/radio/queue")
playing_dir = os.environ.get("RADIO_CACHE_DIR", "/opt/radio/cache")
webroot = os.environ.get("ICECAST_WEBROOT", "/usr/share/icecast2/web")
target_queue_size = 5
max_size = 20 * 1024 * 1024  # 20MB cap to keep downloads well under playback time
min_duration = 60  # seconds; shorter clips (stingers, jingles) are skipped
max_duration = 600  # seconds; the channel has occasional hour-long medley/compilation
                     # uploads that would otherwise hog the stream for ages if randomly picked
repeat_cooldown = 48 * 3600  # seconds; a track that's been queued recently is skipped,
                              # not permanently excluded -- it becomes eligible again after this
channel_username = os.environ["TELEGRAM_CHANNEL"]

from telethon import TelegramClient


def ready_files():
    files = [f for f in os.listdir(queue_dir) if f.endswith(".audio")]
    files.sort(key=lambda f: os.path.getmtime(os.path.join(queue_dir, f)))
    return files


def current_track_id():
    try:
        with open(os.path.join(webroot, "nowplaying.json")) as f:
            np = json.load(f)
        link = np.get("link") or ""
        return link.rstrip("/").rsplit("/", 1)[-1]
    except Exception:
        return None


def recently_played_ids():
    # Files linger in playing_dir for up to 2 generations after they finish
    # (see update_history_and_cleanup in publish_now_playing.py) so they can
    # still be present here well after they've stopped playing.
    ids = set()
    history_path = os.path.join(playing_dir, ".history.json")
    try:
        with open(history_path) as f:
            for path in json.load(f):
                name = os.path.basename(path)
                ids.add(name.replace(".audio", ""))
    except Exception:
        pass
    return ids


MAX_PREFETCH_AGE = 600  # seconds; a genuinely-upcoming prefetched file is never this old


def write_queue_list():
    current_id = current_track_id()
    played_ids = recently_played_ids()
    items = []

    # Tracks already pulled into playing_dir (prefetched, not yet current)
    # play sooner than anything still sitting in queue_dir, so they come first.
    if os.path.isdir(playing_dir):
        cache_files = [f for f in os.listdir(playing_dir) if f.endswith(".audio")]
        cache_files.sort(key=lambda f: os.path.getmtime(os.path.join(playing_dir, f)))
        now = time.time()
        for f in cache_files:
            track_id = f.replace(".audio", "")
            audio_path = os.path.join(playing_dir, f)
            if track_id == current_id:
                continue
            if track_id in played_ids:
                continue
            # update_history_and_cleanup() sometimes fails to delete a file
            # (silently swallowed OSError) once it rotates out of the tracked
            # history, leaving an orphan that already played hours ago but
            # still looks like "upcoming" here. Anything this old that isn't
            # the current track or in recent history was never cleaned up --
            # sweep it now instead of showing it as still-to-play.
            #
            # Admin-uploaded tracks (admin_app.py, id prefix "admin-") are
            # exempt: a human deliberately picked them, unlike the random
            # auto-fill picks this sweep is really meant for, and losing one
            # silently is a much bigger deal than losing a random pick that
            # queue_filler.py can just draw another one of.
            try:
                age = now - os.path.getmtime(audio_path)
            except OSError:
                age = 0
            if age > MAX_PREFETCH_AGE and not track_id.startswith("admin-"):
                for candidate in (audio_path, os.path.join(playing_dir, f"{track_id}.json")):
                    try:
                        os.remove(candidate)
                    except OSError:
                        pass
                continue
            meta_path = os.path.join(playing_dir, f"{track_id}.json")
            if os.path.exists(meta_path):
                try:
                    with open(meta_path) as mf:
                        meta = json.load(mf)
                    if meta.get("artist") or meta.get("title"):
                        items.append(meta)
                except Exception:
                    pass

    for f in ready_files():
        meta_path = os.path.join(queue_dir, f.replace(".audio", ".json"))
        if os.path.exists(meta_path):
            try:
                with open(meta_path) as mf:
                    items.append(json.load(mf))
            except Exception:
                pass

    tmp_path = os.path.join(webroot, "queue.json.tmp")
    with open(tmp_path, "w") as out:
        json.dump({"queue": items, "updated": int(time.time())}, out)
    os.replace(tmp_path, os.path.join(webroot, "queue.json"))


def extract_tags(path):
    artist, title, genre = "", "", ""
    try:
        import mutagen
        from mutagen.easyid3 import EasyID3
        from mutagen.flac import FLAC
        from mutagen.mp4 import MP4

        f = None
        try:
            f = mutagen.File(path)
        except Exception:
            f = None
        if f is None:
            # Some FLAC rips have a bogus/garbled ID3v2 tag glued onto the
            # front (from old ripping tools), which trips up mutagen's
            # generic format auto-detection even though the real, correct
            # tags are sitting right there in the native FLAC block.
            try:
                f = FLAC(path)
            except Exception:
                f = None
        if isinstance(f, MP4) and f.tags:
            # MP4/M4A stores tags under iTunes-style atom keys, not the
            # plain "artist"/"title" keys ID3/Vorbis comments use.
            artist = (f.tags.get("\xa9ART") or [""])[0]
            title = (f.tags.get("\xa9nam") or [""])[0]
            genre = (f.tags.get("\xa9gen") or [""])[0]
        elif f is not None and f.tags:
            try:
                artist = (f.tags.get("artist") or [""])[0]
                title = (f.tags.get("title") or [""])[0]
                genre = (f.tags.get("genre") or [""])[0]
            except Exception:
                pass
        if not artist and not title:
            try:
                easy = EasyID3(path)
                artist = (easy.get("artist") or [""])[0]
                title = (easy.get("title") or [""])[0]
                genre = genre or (easy.get("genre") or [""])[0]
            except Exception:
                pass
    except Exception as e:
        print(f"tag extraction failed: {e}", file=sys.stderr)
    return artist, title, genre


def is_intro(*texts):
    combined = " ".join(t for t in texts if t).lower()
    return "intro" in combined


# Background/score cues meant to sit under a film scene (tension-building,
# atmospheric, no real song structure) rather than stand on their own --
# excluded outright now, not just thinned out, per explicit request.
# "soundtrack" is deliberately NOT one of these signals: plenty of real
# songs (with vocals, normal structure) get released as soundtrack singles
# too, and those should just play like any other track. Only the more
# specific score/instrumental/cinematic-style signals actually mean "this
# is background scoring, not a song".
AMBIENT_KEYWORDS = ("ambient", "suite", "theme", "score", "cue", "underscore", "reprise")
# The channel's own hashtag convention (e.g. "#disco #eurodisco #rnb #pop")
# on the description post right before a batch of tracks -- a much more
# precise signal than fuzzy keyword matching when it's there.
AMBIENT_HASHTAGS = {"ambient", "score", "instrumental", "cinematic", "underscore"}
SHORT_CUE_DURATION = 120  # seconds

HASHTAG_RE = re.compile(r"#(\w+)")


def looks_ambient(duration=None, *texts):
    combined = " ".join(t for t in texts if t).lower()
    hashtags = set(HASHTAG_RE.findall(combined))
    if hashtags & AMBIENT_HASHTAGS:
        return True
    if any(kw in combined for kw in AMBIENT_KEYWORDS):
        return True
    if duration is not None and duration < SHORT_CUE_DURATION:
        return True
    return False


# Channel convention: the description post's track line looks like
# "🎵 Boney M. — Love For Sale" -- but that second part is the ALBUM being
# described, not the individual track title (confirmed: a Powerwolf post
# named its album there while the actual track underneath was "Dancing
# With The Dead", a different song entirely). Only the artist half is
# reliably reusable from this line; the album half is a search hint for
# Discogs, never a title.
CAPTION_TRACK_RE = re.compile(r"🎵\s*(.+)")
CAPTION_DASH_SPLIT_RE = re.compile(r"\s[-–—]\s")


def parse_caption_artist_album(caption):
    if not caption:
        return None, None
    for line in caption.splitlines():
        m = CAPTION_TRACK_RE.search(line)
        if not m:
            continue
        parts = CAPTION_DASH_SPLIT_RE.split(m.group(1).strip(), maxsplit=1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            return parts[0].strip(), parts[1].strip()
    return None, None


# Same token as publish_now_playing.py's Discogs fallback, but text-only --
# a queued-but-not-yet-playing track doesn't need cover art fetched
# (publish_now_playing.py does that itself once the track actually starts).
discogs_token = os.environ.get("DISCOGS_TOKEN", "")


def _discogs_request(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "MusicmaniaRadio/1.0",
            "Authorization": f"Discogs token={discogs_token}",
        },
    )
    with urllib.request.urlopen(req, timeout=6) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _parse_track_duration(duration_str):
    parts = (duration_str or "").split(":")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        return None


# A release search's own "title" field is the ALBUM name, same trap as the
# caption line above -- using it as a track title would be just as wrong.
# The only way to get an actual track title out of Discogs is to open the
# matched release and read its tracklist, then pick the one entry whose own
# duration lines up with this file's -- if nothing lines up closely enough,
# returning nothing is better than confidently attaching the wrong track's
# name (or the album's) to this file.
DISCOGS_DURATION_TOLERANCE = 5  # seconds


def try_discogs_track_lookup(artist, album_hint, duration, fallback_query=""):
    query_text = f"{artist} {album_hint}".strip() or fallback_query.strip()
    if not query_text or duration is None:
        return None, None
    query = urllib.parse.quote(query_text)
    search_url = f"https://api.discogs.com/database/search?q={query}&type=release&per_page=3"
    try:
        data = _discogs_request(search_url)
    except Exception as e:
        print(f"discogs search failed: {e}", file=sys.stderr)
        return None, None

    for result in (data.get("results") or [])[:3]:
        release_id = result.get("id")
        release_title = result.get("title") or ""
        found_artist = release_title.split(" - ", 1)[0].strip() if " - " in release_title else artist
        if not release_id:
            continue
        try:
            release = _discogs_request(f"https://api.discogs.com/releases/{release_id}")
        except Exception as e:
            print(f"discogs release fetch failed: {e}", file=sys.stderr)
            continue
        for track in release.get("tracklist") or []:
            track_seconds = _parse_track_duration(track.get("duration"))
            track_title = (track.get("title") or "").strip()
            if track_seconds is None or not track_title:
                continue
            if abs(track_seconds - duration) <= DISCOGS_DURATION_TOLERANCE:
                return found_artist, track_title
    return None, None


# Discogs tags every release with genre/style fields, right there in the
# search response -- no extra per-release fetch needed. Used as a tie-
# breaker specifically for tracks that carry a soft "soundtrack"-ish local
# signal but nothing strong enough on their own to exclude outright (see
# looks_like_soundtrack_hint below): a real song happens to share a release
# with score cues often enough that "soundtrack" alone isn't reliable, but
# Discogs' own style tag on the matched release is a genuinely independent
# signal.
DISCOGS_AMBIENT_STYLES = {"ambient", "score", "soundtrack", "non-music", "field recording", "musique concrete"}


def check_ambient_via_discogs(artist, title):
    if not discogs_token or not artist or not title:
        return None
    query = urllib.parse.quote(f"{artist} {title}")
    url = f"https://api.discogs.com/database/search?q={query}&type=release&per_page=3"
    try:
        data = _discogs_request(url)
    except Exception as e:
        print(f"discogs ambient check failed: {e}", file=sys.stderr)
        return None
    for result in (data.get("results") or [])[:3]:
        tags = set()
        for field in ("style", "genre"):
            for v in result.get(field) or []:
                tags.add(v.lower())
        if tags & DISCOGS_AMBIENT_STYLES:
            return True
    return False


SOUNDTRACK_HINT_RE = re.compile(r"\b(soundtrack|ost)\b", re.IGNORECASE)


def looks_like_soundtrack_hint(*texts):
    combined = " ".join(t for t in texts if t).lower()
    hashtags = set(HASHTAG_RE.findall(combined))
    return "soundtrack" in hashtags or bool(SOUNDTRACK_HINT_RE.search(combined))


CAPTION_LOOKBACK = 5  # how many prior messages to check for a description post


async def get_preceding_caption(client, entity, candidate_id):
    # Channel convention: a text post describing an album/soundtrack (with
    # genre hashtags at the top), then the individual tracks follow as
    # separate audio messages right after it. The tracks' own tags/filenames
    # usually don't repeat those hashtags, so it's worth checking on top of
    # the track's own metadata.
    for offset in range(1, CAPTION_LOOKBACK + 1):
        prev_id = candidate_id - offset
        if prev_id < 1:
            break
        try:
            msg = await client.get_messages(entity, ids=prev_id)
        except Exception:
            continue
        if msg is None:
            continue
        if msg.audio or msg.voice:
            # ran into another track first -- this candidate isn't part of
            # a batch that was just described, stop looking further back
            break
        text = (msg.raw_text or "").strip()
        if text:
            return text
    return ""


def load_excluded():
    try:
        with open(excluded_path) as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_excluded(excluded):
    tmp = excluded_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sorted(excluded), f)
    os.replace(tmp, excluded_path)


def load_play_history():
    try:
        with open(play_history_path) as f:
            return json.load(f)
    except Exception:
        return {}


def save_play_history(history):
    # Prune anything past the cooldown window so this doesn't grow forever.
    now = time.time()
    pruned = {tid: ts for tid, ts in history.items() if now - ts < repeat_cooldown}
    tmp = play_history_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(pruned, f)
    os.replace(tmp, play_history_path)
    return pruned


def recently_queued(history, track_id):
    ts = history.get(str(track_id))
    return ts is not None and time.time() - ts < repeat_cooldown


async def fill_once(client, entity, ids, excluded, play_history):
    os.makedirs(queue_dir, exist_ok=True)
    existing_ids = {f.split(".")[0] for f in ready_files()}
    current_count = len(existing_ids)
    if current_count >= target_queue_size:
        return False

    for _ in range(15):
        candidate_id = random.choice(ids)
        sid = str(candidate_id)
        if sid in existing_ids or sid in excluded:
            continue
        if recently_queued(play_history, sid):
            continue

        candidate = await client.get_messages(entity, ids=candidate_id)
        if candidate is None or not (candidate.audio or candidate.voice):
            excluded.add(sid)
            save_excluded(excluded)
            continue

        duration = getattr(candidate.file, "duration", None)
        file_title = getattr(candidate.file, "title", None) or ""
        file_performer = getattr(candidate.file, "performer", None) or ""
        file_name = getattr(candidate.file, "name", None) or ""
        if duration is not None and duration < min_duration:
            excluded.add(sid)
            save_excluded(excluded)
            continue
        if duration is not None and duration > max_duration:
            excluded.add(sid)
            save_excluded(excluded)
            continue
        if is_intro(file_title, file_name, candidate.raw_text or ""):
            excluded.add(sid)
            save_excluded(excluded)
            continue

        preceding_caption = ""
        if not looks_ambient(duration, file_title, file_name):
            preceding_caption = await get_preceding_caption(client, entity, candidate_id)
        if looks_ambient(duration, file_title, file_name, preceding_caption):
            excluded.add(sid)
            save_excluded(excluded)
            continue

        size = candidate.file.size if candidate.file else None
        if size is not None and size > max_size:
            excluded.add(sid)
            save_excluded(excluded)
            continue

        tmp_dest = os.path.join(queue_dir, f"{candidate_id}.audio.part")
        final_dest = os.path.join(queue_dir, f"{candidate_id}.audio")
        path = await client.download_media(candidate, file=tmp_dest)
        if not path:
            continue
        artist, title, genre = extract_tags(path)
        if not artist:
            artist = file_performer
        # Was gated on "not artist" too, so a known artist (from tags or
        # Telegram's own performer field) silently blocked ever recovering
        # a still-missing title from file_title/file_name -- e.g. Powerwolf
        # tracks with performer set but no title ended up stuck as "" here,
        # then shown as "Неизвестный трек" in the queue despite the artist
        # being known the whole time.
        if not title and (file_title or file_name):
            title = file_title or file_name
        # Free, already-fetched source before reaching for an API: the
        # description post's own "🎵 Artist — Album" line. Artist only --
        # the second half is the album being described, not this track's
        # title (see try_discogs_track_lookup below for why that distinction
        # matters).
        caption_album_hint = ""
        if (not artist or not title) and preceding_caption:
            cap_artist, cap_album = parse_caption_artist_album(preceding_caption)
            if not artist and cap_artist:
                artist = cap_artist
            caption_album_hint = cap_album or ""
        # Last resort: ask Discogs for the release (artist + album hint from
        # the caption, or whatever text is available), then match this
        # file's own duration against that release's tracklist to find the
        # actual track title -- never just the release/album title.
        if not title:
            disc_artist, disc_title = try_discogs_track_lookup(
                artist, caption_album_hint, duration, fallback_query=preceding_caption[:150]
            )
            if not artist and disc_artist:
                artist = disc_artist
            if disc_title:
                title = disc_title
        if is_intro(artist, title):
            os.remove(path)
            excluded.add(sid)
            save_excluded(excluded)
            continue
        if looks_ambient(duration, genre, artist, title, preceding_caption):
            os.remove(path)
            excluded.add(sid)
            save_excluded(excluded)
            continue
        # No strong local signal either way, but something nearby smells
        # like a soundtrack -- ask Discogs whether this specific release is
        # actually tagged as score/ambient before letting it straight
        # through, instead of trusting "soundtrack" alone (too many real
        # songs share that tag) or excluding it outright (too many aren't).
        if looks_like_soundtrack_hint(genre, preceding_caption) and check_ambient_via_discogs(artist, title):
            os.remove(path)
            excluded.add(sid)
            save_excluded(excluded)
            continue
        meta = {
            "id": candidate_id,
            "artist": artist,
            "title": title,
            "link": f"https://t.me/{channel_username}/{candidate_id}",
        }
        # Write the metadata sidecar *before* the audio file becomes visible
        # under its final name -- otherwise pop_from_queue.py can grab a
        # freshly-renamed .audio file in the split-second window before its
        # .json sidecar exists, and the track plays with no tags forever.
        with open(os.path.join(queue_dir, f"{candidate_id}.json"), "w") as mf:
            json.dump(meta, mf)
        os.replace(path, final_dest)
        play_history[sid] = time.time()
        save_play_history(play_history)
        print(f"queued {final_dest}", file=sys.stderr)
        return True
    return False


async def main():
    os.makedirs(queue_dir, exist_ok=True)
    with open(index_path) as f:
        data = json.load(f)
    ids = data["ids"]
    channel = data["channel"]
    excluded = load_excluded()
    play_history = load_play_history()

    client = TelegramClient(session_path, api_id, api_hash)
    await client.start()
    entity = await client.get_entity(channel)

    while True:
        try:
            added = await fill_once(client, entity, ids, excluded, play_history)
            write_queue_list()
            if not added:
                await asyncio.sleep(3)
        except Exception as e:
            print(f"fill error: {e}", file=sys.stderr)
            await asyncio.sleep(5)


asyncio.run(main())
