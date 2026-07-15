import asyncio
import json
import os
import random
import sys
import fcntl

api_id = int(os.environ["TELEGRAM_API_ID"])
api_hash = os.environ["TELEGRAM_API_HASH"]
session_path = os.environ.get("TELEGRAM_SESSION_PATH", "/opt/radio/session")
index_path = os.environ.get("TRACK_INDEX_PATH", "/opt/radio/track_index.json")
cache_dir = os.environ.get("RADIO_CACHE_DIR", "/opt/radio/cache")
lock_path = os.environ.get("TELEGRAM_SESSION_LOCK_PATH", "/opt/radio/session.lock")

from telethon import TelegramClient

def cleanup_old_files(keep=3):
    files = [os.path.join(cache_dir, f) for f in os.listdir(cache_dir)]
    files = [f for f in files if os.path.isfile(f)]
    files.sort(key=os.path.getmtime)
    for f in files[:-keep] if len(files) > keep else []:
        try:
            os.remove(f)
        except OSError:
            pass

async def main():
    os.makedirs(cache_dir, exist_ok=True)
    cleanup_old_files(keep=3)
    with open(index_path) as f:
        data = json.load(f)
    ids = data["ids"]
    channel = data["channel"]

    lock_fd = open(lock_path, "w")
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        client = TelegramClient(session_path, api_id, api_hash)
        await client.start()
        entity = await client.get_entity(channel)

        max_size = 20 * 1024 * 1024  # 20MB cap to avoid multi-minute downloads stalling playback
        msg = None
        for _ in range(15):
            candidate_id = random.choice(ids)
            candidate = await client.get_messages(entity, ids=candidate_id)
            if candidate is None or not (candidate.audio or candidate.voice):
                continue
            size = candidate.file.size if candidate.file else None
            if size is not None and size > max_size:
                continue
            msg = candidate
            msg_id = candidate_id
            break
        if msg is None:
            print("SKIP", file=sys.stderr)
            return
        dest = os.path.join(cache_dir, f"{msg_id}.audio")
        path = await client.download_media(msg, file=dest)
        if path:
            print(path)
        await client.disconnect()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)

asyncio.run(main())
