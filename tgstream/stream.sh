#!/bin/bash
set -e

RTMP_URL="${TGSTREAM_RTMP_URL:?Set TGSTREAM_RTMP_URL in the environment (see systemd/tgstream.service)}"
W=1280
H=720
OUT_FPS=15

cd /opt/tgstream
/opt/radio/venv/bin/python /opt/tgstream/ensure_call.py || true

# thread_queue_size raised on both inputs -- confirmed directly via `ss`
# on the running process: the default (8 packets) let the audio demux
# thread's internal queue fill up and block every time the x264-encode/mux
# side fell even briefly behind real time, which stopped it from draining
# the OS socket at all -- the kernel's TCP receive buffer for the /stream
# connection was found sitting at ~700KB unread (and still slowly
# growing), ~44s of real audio stuck waiting to be processed while the
# video frames (piped in separately, not gated by this) stayed live. That
# 44s of audio lag behind an essentially-live video overlay is exactly the
# "рассинхрон, играло предыдущее" symptom on the Telegram broadcast. A much
# larger internal queue gives the demux thread enough slack to ride out
# those momentary stalls without blocking, instead of silently
# accumulating backlog for the life of the process.
exec /opt/radio/venv/bin/python /opt/tgstream/frame_pipe.py | ffmpeg -re \
  -thread_queue_size 4096 -f rawvideo -pix_fmt rgb24 -s ${W}x${H} -r 2.5 -i - \
  -thread_queue_size 4096 -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 \
  -i http://127.0.0.1:8000/stream \
  -c:v libx264 -preset veryfast -tune stillimage -pix_fmt yuv420p -r ${OUT_FPS} -g $((OUT_FPS)) \
  -c:a aac -b:a 128k -ar 44100 -ac 2 \
  -map 0:v -map 1:a \
  -f flv "$RTMP_URL"
