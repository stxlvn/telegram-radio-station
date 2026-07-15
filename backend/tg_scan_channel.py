import asyncio
import os
from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeAudio

api_id = int(os.environ["TELEGRAM_API_ID"])
api_hash = os.environ["TELEGRAM_API_HASH"]
session_path = os.environ.get("TELEGRAM_SESSION_PATH", "/opt/radio/session")
channel = os.environ["TELEGRAM_CHANNEL"]

async def main():
    client = TelegramClient(session_path, api_id, api_hash)
    await client.start()
    entity = await client.get_entity(channel)
    print("Channel:", entity.title)
    count = 0
    async for msg in client.iter_messages(entity):
        is_audio = False
        if msg.audio:
            is_audio = True
        elif msg.voice:
            is_audio = True
        if is_audio:
            count += 1
        if count >= 5 and is_audio:
            pass
    print("total audio messages:", count)
    await client.disconnect()

asyncio.run(main())
