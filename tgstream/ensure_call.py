import asyncio
import os
import random
from telethon import TelegramClient
from telethon.tl.functions.phone import CreateGroupCallRequest
from telethon.tl.functions.channels import GetFullChannelRequest

api_id = int(os.environ["TELEGRAM_API_ID"])
api_hash = os.environ["TELEGRAM_API_HASH"]
session_path = os.environ.get("TGSTREAM_SESSION_PATH", "/opt/tgstream/session")
channel_username = os.environ["TELEGRAM_CHANNEL"]


async def main():
    client = TelegramClient(session_path, api_id, api_hash)
    await client.start()
    entity = await client.get_entity(channel_username)
    full = await client(GetFullChannelRequest(entity))
    if full.full_chat.call is not None:
        print("call already active")
        await client.disconnect()
        return
    await client(
        CreateGroupCallRequest(
            peer=entity,
            rtmp_stream=True,
            random_id=random.randint(1, 2**31 - 1),
            title="MusicmaniA Radio",
        )
    )
    print("call created")
    await client.disconnect()


asyncio.run(main())
