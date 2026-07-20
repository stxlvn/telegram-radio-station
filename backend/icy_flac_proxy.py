import asyncio
import json
import os
import time

from aiohttp import web, ClientSession, ClientTimeout

# Icecast serves the plain Ogg-FLAC mount fine on its own, but -- unlike
# MP3/AAC -- it has no built-in ICY-metaint support for Ogg content at all
# (confirmed against Icecast's own docs: mp3-metadata-interval is explicitly
# scoped to "shoutcast compatible streams" only). Most players (foobar2000,
# mpv, some VLC builds) instead pick up per-track tags from the Ogg
# "chained stream" restart Icecast/Liquidsoap already do correctly on every
# track change -- but plenty of real-world hardware streamers (WiiM Home
# confirmed via their own forum) only ever look at ICY metadata and never
# re-parse a live Ogg stream's headers. Radio Paradise runs exactly this
# same workaround for their own "-m" FLAC mount (confirmed by diffing its
# response headers against their plain FLAC mount): a thin proxy in front
# of Icecast that injects ICY-style metadata blocks into the Ogg byte
# stream for any client that asks for them, and passes bytes through
# untouched for anyone who doesn't.
ICECAST_UPSTREAM = os.environ.get("ICY_PROXY_UPSTREAM", "http://127.0.0.1:8000/stream.flac")
NOWPLAYING_PATH = os.environ.get(
    "ICY_PROXY_NOWPLAYING",
    os.path.join(os.environ.get("ICECAST_WEBROOT", "/usr/share/icecast2/web"), "nowplaying.json"),
)
# Set on relay nodes that don't run the backend pipeline themselves (e.g.
# the Netherlands backup, which only relays Icecast's audio mounts, not
# nowplaying.json) -- fetched over HTTP from the origin instead of read off
# local disk. Cached with a short TTL so N concurrent listeners here don't
# turn into N origin requests per metadata interval.
NOWPLAYING_URL = os.environ.get("ICY_PROXY_NOWPLAYING_URL", "")
NOWPLAYING_CACHE_TTL = 3.0
LISTEN_PORT = int(os.environ.get("ICY_PROXY_PORT", "8010"))
METAINT = 65536

_nowplaying_cache = {"text": "MusicmaniA Radio", "fetched_at": 0.0}
_nowplaying_lock = asyncio.Lock()


def _title_from_data(data):
    artist = (data.get("artist") or "").strip()
    title = (data.get("title") or "").strip()
    return f"{artist} - {title}".strip(" -") or "MusicmaniA Radio"


def current_stream_title_local():
    try:
        with open(NOWPLAYING_PATH) as f:
            return _title_from_data(json.load(f))
    except Exception:
        return "MusicmaniA Radio"


async def current_stream_title_remote(session):
    now = time.monotonic()
    if now - _nowplaying_cache["fetched_at"] < NOWPLAYING_CACHE_TTL:
        return _nowplaying_cache["text"]
    async with _nowplaying_lock:
        now = time.monotonic()
        if now - _nowplaying_cache["fetched_at"] < NOWPLAYING_CACHE_TTL:
            return _nowplaying_cache["text"]
        try:
            async with session.get(NOWPLAYING_URL, timeout=ClientTimeout(total=4)) as resp:
                data = json.loads(await resp.text())
            _nowplaying_cache["text"] = _title_from_data(data)
        except Exception:
            pass
        _nowplaying_cache["fetched_at"] = now
        return _nowplaying_cache["text"]


async def current_stream_title(session):
    if NOWPLAYING_URL:
        return await current_stream_title_remote(session)
    return current_stream_title_local()


def build_metadata_block(title_text):
    payload = f"StreamTitle='{title_text}';".encode("utf-8", errors="replace")
    # ICY metadata blocks are always a multiple of 16 bytes, preceded by a
    # single length byte counting those 16-byte units.
    pad_len = (-len(payload)) % 16
    payload += b"\x00" * pad_len
    length_byte = bytes([len(payload) // 16])
    return length_byte + payload


async def handle_stream(request):
    wants_icy = request.headers.get("Icy-MetaData") == "1"
    headers = {
        "Content-Type": "application/ogg",
        "icy-name": "MusicmaniA Radio (Lossless)",
        "icy-genre": "Various",
        "icy-description": "Lossless FLAC, Музыка в высоком качестве",
        "icy-pub": "1",
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
        "Access-Control-Allow-Origin": "*",
    }
    if wants_icy:
        headers["icy-metaint"] = str(METAINT)

    resp = web.StreamResponse(status=200, headers=headers)
    await resp.prepare(request)

    timeout = ClientTimeout(total=None, sock_connect=10, sock_read=30)
    try:
        async with ClientSession(timeout=timeout) as session:
            async with session.get(ICECAST_UPSTREAM) as upstream:
                bytes_since_meta = 0
                async for chunk in upstream.content.iter_any():
                    if not wants_icy:
                        await resp.write(chunk)
                        continue
                    pos = 0
                    while pos < len(chunk):
                        take = min(METAINT - bytes_since_meta, len(chunk) - pos)
                        piece = chunk[pos:pos + take]
                        await resp.write(piece)
                        pos += take
                        bytes_since_meta += len(piece)
                        if bytes_since_meta >= METAINT:
                            title_text = await current_stream_title(session)
                            await resp.write(build_metadata_block(title_text))
                            bytes_since_meta = 0
    except (ConnectionResetError, ConnectionError, asyncio.CancelledError, asyncio.TimeoutError):
        # Listener disconnected or upstream hiccupped mid-stream -- not an
        # error worth logging, every live radio proxy sees this constantly.
        pass
    return resp


app = web.Application()
app.router.add_get("/stream.flac", handle_stream)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=LISTEN_PORT)
