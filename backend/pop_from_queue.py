import json
import os
import sys

queue_dir = os.environ.get("RADIO_QUEUE_DIR", "/opt/radio/queue")
playing_dir = os.environ.get("RADIO_CACHE_DIR", "/opt/radio/cache")

# NOTE: this script only ever ADDS files to playing_dir. It never deletes
# anything from there, since we cannot know here whether Liquidsoap is still
# reading a previously-served file. Deletion of finished tracks is handled
# exclusively by publish_now_playing.py, which is invoked by Liquidsoap's
# on_track hook and therefore knows for certain that the *previous* track
# has finished playing once a *new* one starts.


def main():
    os.makedirs(playing_dir, exist_ok=True)

    if not os.path.isdir(queue_dir):
        return
    files = [f for f in os.listdir(queue_dir) if f.endswith(".audio")]
    if not files:
        return
    files.sort(key=lambda f: os.path.getmtime(os.path.join(queue_dir, f)))
    chosen = files[0]
    track_id = chosen.replace(".audio", "")

    src_audio = os.path.join(queue_dir, chosen)
    src_meta = os.path.join(queue_dir, f"{track_id}.json")
    dst_audio = os.path.join(playing_dir, chosen)

    meta = {"id": track_id}
    if os.path.exists(src_meta):
        try:
            with open(src_meta) as f:
                meta = json.load(f)
        except Exception:
            pass

    try:
        os.replace(src_audio, dst_audio)
        # os.replace() keeps the original mtime (from whenever it was first
        # downloaded into queue_dir, possibly a long wait ago now that the
        # queue buffer runs deeper) -- admin_app.py and queue_list_writer.py
        # both judge "stale, never actually played" by this file's age, so
        # without resetting it here a track can look already-expired the
        # moment it lands here and get hidden (or actually deleted by
        # queue_list_writer.py) before it ever gets to play.
        os.utime(dst_audio, None)
    except OSError as e:
        print(f"failed to move: {e}", file=sys.stderr)
        return

    if os.path.exists(src_meta):
        try:
            os.remove(src_meta)
        except OSError:
            pass

    dst_meta = os.path.join(playing_dir, f"{track_id}.json")
    try:
        with open(dst_meta, "w") as f:
            json.dump(meta, f)
    except OSError:
        pass

    print(dst_audio)


main()
