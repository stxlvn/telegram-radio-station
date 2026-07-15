#!/bin/bash
set -e

RTMP_URL="${TGSTREAM_RTMP_URL:?Set TGSTREAM_RTMP_URL in the environment (see systemd/tgstream.service)}"
W=1280
H=720
OUT_FPS=15

cd /opt/tgstream
/opt/radio/venv/bin/python /opt/tgstream/ensure_call.py || true

exec /opt/radio/venv/bin/python /opt/tgstream/frame_pipe.py | ffmpeg -re \
  -f rawvideo -pix_fmt rgb24 -s ${W}x${H} -r 2.5 -i - \
  -i http://127.0.0.1:8000/stream \
  -c:v libx264 -preset veryfast -tune stillimage -pix_fmt yuv420p -r ${OUT_FPS} -g $((OUT_FPS)) \
  -c:a aac -b:a 128k -ar 44100 -ac 2 \
  -map 0:v -map 1:a \
  -f flv "$RTMP_URL"
