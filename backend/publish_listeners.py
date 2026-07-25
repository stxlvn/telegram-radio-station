import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from base64 import b64encode

webroot = os.environ.get("ICECAST_WEBROOT", "/usr/share/icecast2/web")
icecast_config = os.environ.get("ICECAST_CONFIG", "/etc/icecast2/icecast.xml")
base_url = "http://localhost:8000"
poll_interval = 5

# Mounts a real person can actually be listening on. /stream-web.flac is
# the metadata-stripped mount the web player uses and was missing from
# this count entirely -- browser FLAC listeners simply weren't counted,
# while /stream.flac (the ICY mount for hardware players) was.
MP3_MOUNTS = ["/stream"]
FLAC_MOUNTS = ["/stream.flac", "/stream-web.flac"]

# Everything below is us or a robot, not an audience. Without this the
# published figure counted our own plumbing: a relay pull, the HLS
# encoder, and whatever crawlers happened to be holding the mount open
# (measured: 14 "listeners", every one of them infrastructure or a bot).
#
# Deliberately NOT excluded: aiohttp from 127.0.0.1. That is
# icy_flac_proxy.py, which holds exactly one upstream connection per real
# ICY client (verified 1:1 against its own listening socket), so those
# stand in for genuine hardware-player listeners.
INFRA_UA_PREFIXES = (
    "icecast",   # another Icecast pulling us as a relay
    "lavf",      # ffmpeg/Liquidsoap -- our own HLS encoder
)
BOT_UA_PATTERNS = re.compile(
    r"bot|crawler|spider|go-http-client|curl/|wget|python-requests|"
    r"headlesschrome|scrapy|libwww|okhttp",
    re.IGNORECASE,
)


def admin_credentials():
    root = ET.parse(icecast_config).getroot()
    user = root.findtext("./authentication/admin-user") or "admin"
    password = root.findtext("./authentication/admin-password") or ""
    return user, password


def fetch(url, auth=None):
    req = urllib.request.Request(url)
    if auth:
        token = b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=6) as resp:
        return resp.read()


def is_real_listener(user_agent):
    ua = (user_agent or "").strip()
    if not ua:
        # A real player always sends a user agent; an empty one is far
        # more often a scanner.
        return False
    if ua.lower().startswith(INFRA_UA_PREFIXES):
        return False
    if BOT_UA_PATTERNS.search(ua):
        return False
    return True


def count_mount(mount, auth):
    """Genuine listeners on one mount, or None if it isn't reachable."""
    try:
        body = fetch(f"{base_url}/admin/listclients?mount={mount}", auth)
    except Exception:
        return None
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None
    return sum(
        1 for l in root.iter("listener") if is_real_listener(l.findtext("UserAgent"))
    )


def bitrate_mp3():
    try:
        status = json.loads(fetch(f"{base_url}/status-json.xsl").decode("utf-8"))
    except Exception:
        return None
    sources = (status.get("icestats") or {}).get("source") or []
    if isinstance(sources, dict):
        sources = [sources]
    mp3 = next((s for s in sources if s.get("listenurl", "").endswith("/stream")), None)
    return (mp3 or {}).get("bitrate")


def summarize(auth):
    # Only the numbers the public player needs -- Icecast's own
    # status-json.xsl leaks the backend's real IP and admin email, and
    # this site deliberately hides the backend behind relay proxies.
    listeners_mp3 = sum(count_mount(m, auth) or 0 for m in MP3_MOUNTS)
    listeners_flac = sum(count_mount(m, auth) or 0 for m in FLAC_MOUNTS)
    return {
        "listeners": listeners_mp3 + listeners_flac,
        "listeners_mp3": listeners_mp3,
        "listeners_flac": listeners_flac,
        "bitrate_mp3": bitrate_mp3(),
        "updated": int(time.time()),
    }


def main():
    auth = admin_credentials()
    while True:
        try:
            summary = summarize(auth)
            tmp_path = os.path.join(webroot, "listeners.json.tmp")
            with open(tmp_path, "w") as f:
                json.dump(summary, f)
            os.replace(tmp_path, os.path.join(webroot, "listeners.json"))
        except Exception as e:
            print(f"listener publish failed: {e}", file=sys.stderr)
        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
