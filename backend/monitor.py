import json
import subprocess
import time
import urllib.request

SERVICES = [
    "icecast2.service",
    "radio.service",
    "tgstream.service",
    "queue-filler.service",
    "admin-app.service",
]
CHECK_INTERVAL = 60  # seconds
MAX_STALE_NOWPLAYING = 15 * 60  # nowplaying.json must advance at least this often


def log(msg):
    print(f"[monitor] {msg}", flush=True)


def is_active(service):
    r = subprocess.run(["systemctl", "is-active", service], capture_output=True, text=True)
    return r.stdout.strip() == "active"


def restart(service):
    log(f"restarting {service}")
    subprocess.run(["systemctl", "restart", service])


def restart_radio_and_broadcast():
    # tgstream's ffmpeg pulls audio straight from Icecast and doesn't
    # reconnect on its own if that connection drops mid-restart, so any
    # radio.service restart needs an immediate tgstream.service restart
    # right behind it -- learned the hard way earlier this session.
    restart("radio.service")
    time.sleep(6)
    restart("tgstream.service")


def check_services():
    for svc in SERVICES:
        if not is_active(svc):
            log(f"{svc} is NOT active")
            if svc == "radio.service":
                restart_radio_and_broadcast()
            else:
                restart(svc)


def check_nowplaying_freshness():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/nowplaying.json", timeout=5) as resp:
            data = json.loads(resp.read())
        age = time.time() - data.get("updated", 0)
        if age > MAX_STALE_NOWPLAYING:
            log(f"nowplaying.json stale for {age:.0f}s -- playback looks stuck")
            restart_radio_and_broadcast()
    except Exception as e:
        log(f"nowplaying.json check failed: {e}")


def check_stream_reachable():
    for path in ("/stream", "/stream.flac"):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:8000{path}", timeout=5) as resp:
                if resp.status != 200:
                    log(f"{path} returned unexpected status {resp.status}")
        except Exception as e:
            log(f"{path} unreachable: {e}")


def check_admin_app():
    try:
        with urllib.request.urlopen("http://127.0.0.1:5055/", timeout=5) as resp:
            if resp.status != 200:
                log(f"admin-app returned unexpected status {resp.status}")
    except Exception as e:
        log(f"admin-app unreachable ({e})")
        restart("admin-app.service")


def check_tgstream_audio_health():
    # `systemctl is-active tgstream.service` in check_services() above
    # stays "active" even when its ffmpeg has silently dropped the audio
    # input -- the process and video branch keep running fine, so nothing
    # else in this file ever notices (confirmed directly: happened twice,
    # broadcast ran video-only for hours both times with no restart).
    # ffmpeg now has -reconnect flags on that input (see stream.sh) so it
    # should usually recover on its own; this is the safety net for when
    # it doesn't -- if the exact demuxing-error signature shows up in the
    # last couple of check cycles, force a restart.
    try:
        r = subprocess.run(
            ["journalctl", "-u", "tgstream.service", "--since", "-90 seconds", "--no-pager"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:
        log(f"tgstream audio health check failed: {e}")
        return
    if "Error during demuxing" in r.stdout or "Input/output error" in r.stdout:
        log("tgstream audio input hit a demuxing error -- restarting to recover")
        restart("tgstream.service")


def main():
    log(f"starting, checking every {CHECK_INTERVAL}s")
    while True:
        try:
            check_services()
            check_nowplaying_freshness()
            check_stream_reachable()
            check_admin_app()
            check_tgstream_audio_health()
        except Exception as e:
            log(f"monitor loop error: {e}")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
