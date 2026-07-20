import json
import os
import sys
import time
import urllib.request

webroot = os.environ.get("ICECAST_WEBROOT", "/usr/share/icecast2/web")
status_url = "http://localhost:8000/status-json.xsl"
poll_interval = 5


def fetch_status():
    with urllib.request.urlopen(status_url, timeout=6) as resp:
        return json.loads(resp.read().decode("utf-8"))


def summarize(status):
    # Icecast's own status-json.xsl leaks the backend's real IP and admin
    # email (this site deliberately hides the backend behind relay
    # proxies) -- this republishes only the two numbers the public player
    # actually needs, nothing else from that payload.
    sources = (status.get("icestats") or {}).get("source") or []
    if isinstance(sources, dict):
        sources = [sources]

    mp3 = next((s for s in sources if s.get("listenurl", "").endswith("/stream")), None)
    flac = next((s for s in sources if s.get("listenurl", "").endswith("/stream.flac")), None)

    listeners_mp3 = (mp3 or {}).get("listeners", 0)
    listeners_flac = (flac or {}).get("listeners", 0)

    return {
        "listeners": listeners_mp3 + listeners_flac,
        "listeners_mp3": listeners_mp3,
        "listeners_flac": listeners_flac,
        "bitrate_mp3": (mp3 or {}).get("bitrate"),
        "updated": int(time.time()),
    }


def main():
    while True:
        try:
            summary = summarize(fetch_status())
            tmp_path = os.path.join(webroot, "listeners.json.tmp")
            with open(tmp_path, "w") as f:
                json.dump(summary, f)
            os.replace(tmp_path, os.path.join(webroot, "listeners.json"))
        except Exception as e:
            print(f"listener publish failed: {e}", file=sys.stderr)
        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
