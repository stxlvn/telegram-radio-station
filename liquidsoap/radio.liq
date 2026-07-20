#!/usr/bin/liquidsoap

settings.init.allow_root.set(true)
settings.log.stdout.set(true)
settings.log.level.set(3)

# Set via the systemd unit's EnvironmentFile (see systemd/radio.service) --
# never hardcode this in the script itself.
icecast_password = getenv("ICECAST_PASSWORD")

# Unix socket only (no telnet/TCP), so it's only reachable by something
# already running on this box -- the admin panel's "skip current track"
# button talks to it directly, no network exposure at all.
settings.server.socket.set(true)
settings.server.socket.path.set("/run/liquidsoap-radio.sock")
settings.server.socket.permissions.set(0o660)

def next_request() =
  path = process.read("/opt/radio/venv/bin/python /opt/radio/pop_from_queue.py")
  path = string.trim(path)
  if path != "" then
    (request.create(path) : request?)
  else
    (null() : request?)
  end
end

def on_new_track(m) =
  filename = list.assoc(default="", "filename", m)
  if filename != "" then
    result = process.read(timeout=20., "/opt/radio/venv/bin/python /opt/radio/publish_now_playing.py #{filename} 2>&1")
    log.important(label="now_playing", "publish for #{filename}: #{result}")
  end
end

# timeout is how long a prefetched-but-not-yet-playing request is allowed to
# sit prepared before Liquidsoap gives up on it and silently fetches a
# replacement instead. 180s is shorter than a huge fraction of real tracks
# (confirmed: a 194s track ahead of it was enough to time out and silently
# drop an admin-uploaded track that never got a chance to play). With
# prefetch=2 a request can end up waiting through up to two tracks' worth of
# playback, so give it generous headroom well above max_duration (600s, see
# queue_filler.py) for the worst case of two long tracks back to back.
radio = request.dynamic(timeout=1800., prefetch=2, next_request)
radio.on_track(synchronous=true, on_new_track)
radio = blank.skip(max_blank=15., min_noise=0.5, radio)
radio = crossfade(duration=1.5, radio)
# Explicit id so the admin panel can address it over the socket as
# "radio_out.skip" to skip whatever's currently playing.
radio = mksafe(id="radio_out", radio)

output.icecast(
  %mp3(bitrate=128),
  host="127.0.0.1",
  port=8000,
  password=icecast_password,
  mount="stream",
  name="Your Radio",
  description="Hi-Fi, Lossless Audio, Музыка в высоком качестве",
  genre="Various",
  radio
)

# Opt-in lossless mount alongside the default MP3 stream -- same source,
# duplicated by Liquidsoap into a second encoder. Costs ~6-8x the bandwidth
# per listener, so it's deliberately not the default /stream mount.
#
# Ogg-wrapped, not raw FLAC: raw FLAC only carries its STREAMINFO header
# once, at the very start of the encoder's lifetime. Icecast has no
# FLAC-aware logic to replay that header to listeners who connect later
# (unlike MP3, where any frame is a valid sync point), so every listener who
# wasn't connected at the exact moment Liquidsoap started never got a
# decodable stream -- confirmed directly: readyState stuck at 0 forever in
# Chrome. Ogg containers get real header-page resending from Icecast's
# native Ogg handling, so a listener joining at any time gets a working
# stream (confirmed: readyState reaches 3 within ~1s).
output.icecast(
  %ogg(%flac),
  host="127.0.0.1",
  port=8000,
  password=icecast_password,
  mount="stream.flac",
  name="Your Radio (Lossless)",
  description="Lossless FLAC, Музыка в высоком качестве",
  genre="Various",
  radio
)

# HLS mirror of the same source, purely so the web player can seek backward
# a bit and jump back to the live edge -- the plain /stream mount above is a
# raw chunked HTTP stream with no Content-Length/Accept-Ranges, so browsers
# can't seek it at all. HLS splits into small immutable segments instead, so
# there's no byte-offset staleness problem when old segments roll off.
# Written straight into the icecast webroot so it's served (with the same
# wildcard CORS header) by icecast's built-in static file server, no extra
# process needed.
#
# AAC-in-MPEG-TS, not raw MP3: MP3 frames share a bit reservoir across frame
# boundaries, so a segment cut mid-stream isn't independently decodable --
# every segment splice produced an audible click. AAC packets in a TS
# container don't have that cross-frame dependency, so segments splice
# cleanly (verified: each segment decodes standalone with no errors).
output.file.hls(
  segment_duration=6.,
  segments=100,
  playlist="live.m3u8",
  "/usr/share/icecast2/web/hls",
  [("live", %ffmpeg(format="mpegts", %audio(codec="aac", b="128k")))],
  radio
)

# Same idea, for the FLAC quality tier. FLAC frames don't share a bit
# reservoir the way MP3 frames do, so raw FLAC segments splice cleanly
# without needing the AAC/TS workaround above (verified: each segment
# decodes standalone with no errors). Liquidsoap's HLS output needs
# bandwidth/codecs given explicitly for FLAC since it can't infer them the
# way it does for MP3/AAC.
output.file.hls(
  segment_duration=6.,
  segments=100,
  playlist="live.m3u8",
  "/usr/share/icecast2/web/hls-flac",
  [("live", (%flac).{codecs = "fLaC", bandwidth = 900000})],
  radio
)
