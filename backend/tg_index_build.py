import asyncio
import json
import os
import time
from telethon import TelegramClient
from telethon.tl.types import InputMessagesFilterMusic

api_id = int(os.environ["TELEGRAM_API_ID"])
api_hash = os.environ["TELEGRAM_API_HASH"]
session_path = os.environ.get("TELEGRAM_SESSION_PATH", "/opt/radio/session")
channel = os.environ["TELEGRAM_CHANNEL"]
out_path = os.environ.get("TRACK_INDEX_PATH", "/opt/radio/track_index.json")

async def main():
    client = TelegramClient(session_path, api_id, api_hash)
    await client.start()
    entity = await client.get_entity(channel)
    start = time.time()
    ids = []
    async for msg in client.iter_messages(entity, filter=InputMessagesFilterMusic):
        ids.append(msg.id)
        if len(ids) % 1000 == 0:
            print(f"count={len(ids)} elapsed={time.time()-start:.1f}s", flush=True)
            with open(out_path, "w") as f:
                json.dump({"channel": channel, "ids": ids}, f)
    with open(out_path, "w") as f:
        json.dump({"channel": channel, "ids": ids}, f)
    print(f"DONE total={len(ids)} elapsed={time.time()-start:.1f}s", flush=True)
    await client.disconnect()

asyncio.run(main())
