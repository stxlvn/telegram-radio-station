import asyncio
import json
import os

from telethon import TelegramClient
from telethon.tl.functions.channels import GetFullChannelRequest

api_id = int(os.environ["TELEGRAM_API_ID"])
api_hash = os.environ["TELEGRAM_API_HASH"]
session_path = os.environ.get("TGSTREAM_SESSION_PATH", "/opt/tgstream/session")
channel_username = os.environ["TELEGRAM_CHANNEL"]
webroot = os.environ.get("ICECAST_WEBROOT", "/usr/share/icecast2/web")
out_path = os.path.join(webroot, "subscribers.json")
poll_interval = 600  # 10 minutes -- this is a cosmetic counter, not real-time data


async def main():
    client = TelegramClient(session_path, api_id, api_hash)
    await client.start()
    entity = await client.get_entity(channel_username)

    while True:
        try:
            full = await client(GetFullChannelRequest(entity))
            count = full.full_chat.participants_count
            tmp = out_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"count": count}, f)
            os.replace(tmp, out_path)
            print(f"subscribers: {count}")
        except Exception as e:
            print(f"update failed: {e}")
        await asyncio.sleep(poll_interval)


asyncio.run(main())
