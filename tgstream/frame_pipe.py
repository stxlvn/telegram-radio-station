import json
import os
import sys
import time

from render_frame import load_now_playing, load_queue, render, webroot

TICK = 0.4
HEARTBEAT_PATH = os.path.join(webroot, "tgstream_heartbeat.json")


def write_heartbeat():
    try:
        tmp = HEARTBEAT_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"ts": time.time()}, f)
        os.replace(tmp, HEARTBEAT_PATH)
    except Exception:
        pass


def main():
    out = sys.stdout.buffer
    while True:
        start = time.monotonic()
        try:
            write_heartbeat()
            artist, title, cover_local, started_at, duration = load_now_playing()
            queue = load_queue()
            img = render(artist, title, cover_local, queue, started_at, duration)
            out.write(img.convert("RGB").tobytes())
            out.flush()
        except BrokenPipeError:
            return
        except Exception as e:
            print(f"frame_pipe error: {e}", file=sys.stderr)
        elapsed = time.monotonic() - start
        time.sleep(max(0.0, TICK - elapsed))


if __name__ == "__main__":
    main()
