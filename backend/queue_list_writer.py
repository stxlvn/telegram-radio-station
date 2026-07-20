import json
import os
import subprocess
import sys
import time

queue_dir = os.environ.get("RADIO_QUEUE_DIR", "/opt/radio/queue")
playing_dir = os.environ.get("RADIO_CACHE_DIR", "/opt/radio/cache")
webroot = os.environ.get("ICECAST_WEBROOT", "/usr/share/icecast2/web")
prefetch_dir = os.environ.get("PREFETCH_DIR", "/opt/radio/prefetch_enrich")
publish_now_playing_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "publish_now_playing.py")

MAX_PREFETCH_AGE = 600  # seconds; a genuinely-upcoming prefetched file is never this old

# queue_filler.py now prefetches further ahead (target_queue_size=10) than
# what's actually worth showing listeners as "up next" -- the site's queue
# display stays at 5 regardless of how deep the internal prefetch buffer is.
DISPLAY_QUEUE_SIZE = 5

# Index into the ordered upcoming-tracks list, not a count -- 0 is the very
# next track, 1 is "the one after that" (i.e. two tracks from now counting
# the current one still playing). That's the track publish_now_playing.py's
# prefetch_enrich() gets a head start on, so its Discogs/Wikipedia/YouTube
# answers are already sitting there by the time it's actually live.
PREFETCH_AHEAD_INDEX = 1


def _trigger_prefetch(item):
    track_id = item.get("id")
    if track_id is None:
        return
    json_path = os.path.join(prefetch_dir, f"{track_id}.json")
    if os.path.exists(json_path):
        return
    subprocess.Popen(
        [
            sys.executable, publish_now_playing_path, "--prefetch",
            str(track_id), item.get("artist") or "", item.get("title") or "",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


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

    if len(items) > PREFETCH_AHEAD_INDEX:
        _trigger_prefetch(items[PREFETCH_AHEAD_INDEX])

    items = items[:DISPLAY_QUEUE_SIZE]

    tmp_path = os.path.join(webroot, "queue.json.tmp")
    with open(tmp_path, "w") as out:
        json.dump({"queue": items, "updated": int(time.time())}, out)
    os.replace(tmp_path, os.path.join(webroot, "queue.json"))


if __name__ == "__main__":
    write_queue_list()
