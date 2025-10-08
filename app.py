import time, threading, random, json, os, concurrent.futures, secrets, shutil
from pathlib import Path
from flask import Flask, render_template_string, request, jsonify, redirect, session
from instagrapi import Client
from instagrapi.exceptions import ClientError, LoginRequired
from werkzeug.utils import secure_filename
import requests

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

# Configuration
ALLOWED_EXTENSIONS = {'json'}
SESSION_UPLOAD_FOLDER = Path("uploaded_sessions")
SESSION_UPLOAD_FOLDER.mkdir(exist_ok=True)

# Instagram OAuth Configuration (Yeh aapko Instagram Developer se milenge)
INSTAGRAM_APP_ID = os.environ.get('INSTAGRAM_APP_ID', '')
INSTAGRAM_APP_SECRET = os.environ.get('INSTAGRAM_APP_SECRET', '')
INSTAGRAM_REDIRECT_URI = os.environ.get('INSTAGRAM_REDIRECT_URI', '')

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# Global state
STATE = {
    "running": False,
    "logs": ["System started"],
    "status": "idle",
    "last_response": None,
    "threads": [],
    "stats": {"sent": 0, "failed": 0, "rate": 0, "max_messages": 100},
    "current_account": None,
    "accounts": [],
    "active_workers": 0
}
WORKER = {"threads": [], "stop_flag": False}
lock = threading.Lock()

# ---------- Logging ----------
def log(msg):
    ts = time.strftime("%H:%M:%S")
    with lock:
        STATE["logs"].append(f"[{ts}] {msg}")
        if len(STATE["logs"]) > 25:
            STATE["logs"] = STATE["logs"][-25:]

# ---------- Advanced Account Management ----------
class AdvancedAccountManager:
    def __init__(self):
        self.accounts = {}
        self.sessions_dir = Path("sessions")
        self.sessions_dir.mkdir(exist_ok=True)
        self.load_accounts()
    
    def load_accounts(self):
        """Load all saved accounts from session files"""
        for session_file in self.sessions_dir.glob("*.json"):
            username = session_file.stem
            try:
                cl = Client()
                cl.load_settings(str(session_file))
                user_info = cl.account_info()
                self.accounts[username] = {
                    'client': cl,
                    'username': username,
                    'full_name': user_info.full_name,
                    'status': 'online',
                    'session_file': session_file,
                    'is_active': False,
                    'worker_id': None
                }
                log(f"✅ Loaded account: {username}")
            except Exception as e:
                log(f"❌ Failed to load {username}: {str(e)[:200]}")
    
    def login_account(self, username, password, verification_code=None):
        """Login to Instagram account with OTP support"""
        try:
            cl = Client()
            
            # Set some headers to avoid detection
            cl.set_user_agent("Instagram 219.0.0.12.117 Android")
            
            if verification_code:
                # OTP/2FA login
                cl.login(username, password, verification_code=verification_code)
            else:
                # Regular login
                cl.login(username, password)
            
            session_file = self.sessions_dir / f"{username}.json"
            cl.dump_settings(str(session_file))
            
            user_info = cl.account_info()
            
            self.accounts[username] = {
                'client': cl,
                'username': username,
                'full_name': user_info.full_name,
                'status': 'online',
                'session_file': session_file,
                'is_active': False,
                'worker_id': None
            }
            
            return True, None
            
        except Exception as e:
            error_msg = str(e)
            if "checkpoint" in error_msg.lower() or "verification" in error_msg.lower():
                return False, "verification_required"
            elif "challenge" in error_msg.lower():
                return False, "challenge_required"
            else:
                return False, error_msg
    
    def get_client(self, username):
        """Get client for username"""
        if username in self.accounts:
            return self.accounts[username]['client']
        return None
    
    def get_accounts_list(self):
        """Get list of all accounts"""
        return [
            {
                'username': acc['username'],
                'full_name': acc['full_name'],
                'status': acc['status'],
                'is_active': acc['is_active']
            }
            for acc in self.accounts.values()
        ]
    
    def activate_account(self, username):
        """Mark account as active for sending"""
        if username in self.accounts:
            self.accounts[username]['is_active'] = True
            return True
        return False
    
    def deactivate_account(self, username):
        """Mark account as inactive"""
        if username in self.accounts:
            self.accounts[username]['is_active'] = False
            return True
        return False
    
    def get_active_accounts(self):
        """Get all active accounts"""
        return [acc for acc in self.accounts.values() if acc['is_active']]

# Initialize account manager
account_manager = AdvancedAccountManager()

# ---------- MULTI-ACCOUNT MESSAGE SENDING ----------
def send_message_multi_worker(account_data, thread_id, message):
    """Send single message from specific account"""
    try:
        account_data['client'].direct_send(message, thread_ids=[thread_id])
        return True, account_data['username']
    except Exception as e:
        log(f"❌ Send failed from {account_data['username']}: {str(e)[:100]}")
        return False, account_data['username']

def multi_account_sender_worker(accounts_list, thread_ids, messages, messages_per_second, max_per_run):
    """Multi-account message sender"""
    try:
        start_time = time.time()
        last_rate_check = start_time
        messages_since_check = 0
        
        # Update max messages in state
        with lock:
            STATE["stats"]["max_messages"] = max_per_run
            STATE["active_workers"] = len(accounts_list)

        # Create send tasks distributed across accounts
        send_tasks = []
        account_index = 0
        
        while len(send_tasks) < max_per_run:
            for tid in thread_ids:
                if len(send_tasks) >= max_per_run:
                    break
                    
                message = random.choice(messages)
                account = accounts_list[account_index % len(accounts_list)]
                send_tasks.append((account, tid, message))
                account_index += 1

        log(f"🎯 Starting multi-account sending with {len(accounts_list)} accounts")
        log(f"📊 Total tasks: {len(send_tasks)}, Threads: {len(thread_ids)}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(50, len(accounts_list) * 5)) as executor:
            futures = []
            
            for account, tid, message in send_tasks:
                if WORKER["stop_flag"]:
                    log("🛑 Stopping worker due to stop flag")
                    break
                    
                future = executor.submit(send_message_multi_worker, account, tid, message)
                futures.append(future)
                
                # Control sending speed
                if messages_per_second > 0:
                    time.sleep(1.0 / messages_per_second)

            # Process results
            for future in concurrent.futures.as_completed(futures):
                try:
                    success, username = future.result()
                    
                    with lock:
                        if success:
                            STATE["stats"]["sent"] += 1
                            messages_since_check += 1
                            STATE["last_response"] = {
                                "account": username,
                                "timestamp": time.strftime("%H:%M:%S"),
                                "status": "sent"
                            }
                        else:
                            STATE["stats"]["failed"] += 1

                except Exception as e:
                    with lock:
                        STATE["stats"]["failed"] += 1
                    log(f"❌ Future error: {str(e)[:200]}")

                # Update rate every 2 seconds
                current_time = time.time()
                if current_time - last_rate_check >= 2.0:
                    actual_rate = (messages_since_check / (current_time - last_rate_check)) if (current_time - last_rate_check) > 0 else 0
                    with lock:
                        STATE["stats"]["rate"] = round(actual_rate, 1)
                    messages_since_check = 0
                    last_rate_check = current_time

                # Progress updates
                if STATE["stats"]["sent"] % 10 == 0:
                    progress = (STATE["stats"]["sent"] / max_per_run) * 100
                    log(f"📈 Progress: {STATE['stats']['sent']}/{max_per_run} ({progress:.1f}%)")

                # Check if we've reached max messages
                if STATE["stats"]["sent"] >= max_per_run:
                    log(f"🎯 Reached maximum messages limit: {max_per_run}")
                    break

        # Final stats
        total_time = time.time() - start_time
        final_rate = STATE["stats"]["sent"] / total_time if total_time > 0 else 0

        log(f"✅ Multi-account worker completed: {STATE['stats']['sent']} messages sent")
        log(f"⚡ Final rate: {final_rate:.1f} messages/second")
        log(f"👥 Active accounts used: {len(accounts_list)}")

    except Exception as e:
        log(f"💥 Multi-account worker error: {str(e)[:200]}")
    finally:
        with lock:
            STATE["running"] = False
            STATE["status"] = "idle"
            STATE["active_workers"] = 0
        WORKER["stop_flag"] = False
        # Deactivate all accounts
        for account in accounts_list:
            account_manager.deactivate_account(account['username'])

# ---------- Instagram Official OAuth Login ----------
@app.route('/instagram/login')
def instagram_oauth_login():
    """Redirect to Instagram's official login page"""
    if not INSTAGRAM_APP_ID:
        return jsonify({"success": False, "error": "Instagram OAuth not configured"})
    
    auth_url = (
        f"https://api.instagram.com/oauth/authorize?"
        f"client_id={INSTAGRAM_APP_ID}&"
        f"redirect_uri={INSTAGRAM_REDIRECT_URI}&"
        f"scope=user_profile,user_media&"
        f"response_type=code"
    )
    return redirect(auth_url)

@app.route('/instagram/callback')
def instagram_oauth_callback():
    """Handle Instagram OAuth callback"""
    try:
        code = request.args.get('code')
        
        if not code:
            return jsonify({"success": False, "error": "Authorization code missing"})
        
        # Exchange code for access token
        token_data = {
            'client_id': INSTAGRAM_APP_ID,
            'client_secret': INSTAGRAM_APP_SECRET,
            'grant_type': 'authorization_code',
            'redirect_uri': INSTAGRAM_REDIRECT_URI,
            'code': code
        }
        
        response = requests.post(
            'https://api.instagram.com/oauth/access_token',
            data=token_data
        )
        
        if response.status_code == 200:
            token_info = response.json()
            access_token = token_info.get('access_token')
            user_id = token_info.get('user_id')
            
            # Get user profile information
            profile_url = f"https://graph.instagram.com/{user_id}?fields=id,username&access_token={access_token}"
            profile_response = requests.get(profile_url)
            
            if profile_response.status_code == 200:
                profile_data = profile_response.json()
                username = profile_data.get('username')
                
                # Store session (you might want to save the access_token properly)
                log(f"✅ Instagram OAuth login successful: {username}")
                
                return f"""
                <html>
                    <body>
                        <script>
                            alert('Instagram login successful for {username}! However, for sending DMs you need to use manual login method.');
                            window.close();
                        </script>
                    </body>
                </html>
                """
            else:
                return jsonify({"success": False, "error": "Failed to get user profile"})
        else:
            return jsonify({"success": False, "error": "Failed to get access token"})
            
    except Exception as e:
        log(f"❌ OAuth callback error: {str(e)[:200]}")
        return jsonify({"success": False, "error": str(e)[:200]})

# ---------- Chat Management ----------
def load_chats_for_account(username):
    """Load chats for specific account"""
    try:
        cl = account_manager.get_client(username)
        if not cl:
            return False, "Account not found"
        
        threads = []
        try:
            thread_data = cl.direct_threads(amount=100)
            
            for t in thread_data:
                try:
                    name = t.thread_title or (t.users[0].username if t.users else "Unknown")
                    threads.append({"id": str(t.id), "name": name})
                except Exception:
                    continue
        except Exception as e:
            log(f"❌ Error loading threads: {str(e)[:200]}")
            return False, f"Failed to load chats: {str(e)[:200]}"
        
        STATE["threads"] = threads
        STATE["current_account"] = username
        log(f"✅ Loaded {len(threads)} chats for {username}")
        return True, f"Loaded {len(threads)} conversations"
        
    except Exception as e:
        log(f"❌ Failed to load chats: {str(e)[:200]}")
        return False, str(e)[:200]

# ---------- FLASK ROUTES ----------
@app.route('/')
def index():
    return render_template_string(TEMPLATE)

@app.route('/api/accounts', methods=['GET'])
def get_accounts():
    accounts = account_manager.get_accounts_list()
    return jsonify({"accounts": accounts})

@app.route('/api/login', methods=['POST'])
def login_account():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    verification_code = data.get('verification_code')
    
    if not username or not password:
        return jsonify({"success": False, "error": "Username and password required"})
    
    success, error = account_manager.login_account(username, password, verification_code)
    
    if success:
        return jsonify({"success": True, "message": "Login successful"})
    else:
        return jsonify({"success": False, "error": error})

@app.route('/api/account/toggle', methods=['POST'])
def toggle_account():
    data = request.json
    username = data.get('username')
    activate = data.get('activate', False)
    
    if activate:
        success = account_manager.activate_account(username)
        if success:
            return jsonify({"success": True, "message": f"Account {username} activated"})
    else:
        success = account_manager.deactivate_account(username)
        if success:
            return jsonify({"success": True, "message": f"Account {username} deactivated"})
    
    return jsonify({"success": False, "error": "Account not found"})

@app.route('/api/start', methods=['POST'])
def start_sending():
    if STATE["running"]:
        return jsonify({"success": False, "error": "Already running"})
    
    data = request.json
    thread_ids = data.get('thread_ids', [])
    messages = data.get('messages', [])
    messages_per_second = data.get('messages_per_second', 1)
    max_messages = data.get('max_messages', 100)
    
    if not thread_ids or not messages:
        return jsonify({"success": False, "error": "Thread IDs and messages required"})
    
    active_accounts = account_manager.get_active_accounts()
    if not active_accounts:
        return jsonify({"success": False, "error": "No active accounts selected"})
    
    # Reset stats
    with lock:
        STATE["running"] = True
        STATE["status"] = "running"
        STATE["stats"] = {"sent": 0, "failed": 0, "rate": 0, "max_messages": max_messages}
        WORKER["stop_flag"] = False
    
    # Start worker thread
    thread = threading.Thread(
        target=multi_account_sender_worker,
        args=(active_accounts, thread_ids, messages, messages_per_second, max_messages)
    )
    thread.daemon = True
    thread.start()
    
    log(f"🚀 Started sending with {len(active_accounts)} accounts")
    return jsonify({"success": True, "message": "Sending started"})

@app.route('/api/stop', methods=['POST'])
def stop_sending():
    WORKER["stop_flag"] = True
    with lock:
        STATE["running"] = False
        STATE["status"] = "stopping"
    log("🛑 Stop signal sent")
    return jsonify({"success": True, "message": "Stopping..."})

@app.route('/api/status', methods=['GET'])
def get_status():
    with lock:
        return jsonify({
            "running": STATE["running"],
            "status": STATE["status"],
            "logs": STATE["logs"][-10:],
            "stats": STATE["stats"],
            "last_response": STATE["last_response"],
            "active_workers": STATE["active_workers"]
        })

@app.route('/api/clear_logs', methods=['POST'])
def clear_logs():
    with lock:
        STATE["logs"] = ["Logs cleared"]
    return jsonify({"success": True})

@app.route('/api/load_chats', methods=['POST'])
def load_chats():
    data = request.json
    username = data.get('username')
    
    if not username:
        return jsonify({"success": False, "error": "Username required"})
    
    success, message = load_chats_for_account(username)
    return jsonify({"success": success, "message": message})

@app.route('/api/upload_session', methods=['POST'])
def upload_session_file():
    """Upload and import session file"""
    try:
        if 'session_file' not in request.files:
            return jsonify({"success": False, "error": "No file selected"})
        
        file = request.files['session_file']
        if file.filename == '':
            return jsonify({"success": False, "error": "No file selected"})
        
        if file and allowed_file(file.filename):
            # Secure filename and save
            filename = secure_filename(file.filename)
            file_path = SESSION_UPLOAD_FOLDER / filename
            file.save(file_path)
            
            # Extract username from filename (assuming format: username.json)
            username = filename.replace('.json', '')
            
            # Validate session file
            try:
                cl = Client()
                cl.set_user_agent("Instagram 219.0.0.12.117 Android")
                cl.load_settings(str(file_path))
                
                # Test session by getting account info
                user_info = cl.account_info()
                
                # Move to main sessions directory
                final_path = account_manager.sessions_dir / f"{username}.json"
                shutil.move(file_path, final_path)
                
                # Reload accounts
                account_manager.load_accounts()
                
                log(f"✅ Session imported successfully: {username}")
                return jsonify({
                    "success": True, 
                    "message": f"Session imported successfully for {username}",
                    "username": username
                })
                
            except Exception as e:
                # Clean up invalid file
                if file_path.exists():
                    file_path.unlink()
                log(f"❌ Invalid session file: {str(e)[:100]}")
                return jsonify({"success": False, "error": f"Invalid session file: {str(e)[:100]}"})
        
        return jsonify({"success": False, "error": "Only JSON files are allowed"})
        
    except Exception as e:
        log(f"💥 Session upload error: {str(e)[:100]}")
        return jsonify({"success": False, "error": f"Upload failed: {str(e)[:100]}"})

# ---------- ULTIMATE UI TEMPLATE ----------
TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>NovaGram Pro • Multi-Account DM Manager</title>
    <style>
        /* Same CSS as your second code - too long to include here */
        /* Copy the exact same CSS from your second code */
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div>
                <div class="logo">NovaGram Pro</div>
                <div class="credit">Multi-Account DM Manager • Live Hosting Ready</div>
            </div>
            <div class="status-badge">
                <div class="status-dot" id="statusDot"></div>
                <span id="statusText">Idle</span>
            </div>
        </div>
        
        <div class="main-layout">
            <div class="sidebar">
                <div class="section">
                    <div class="section-title">🔐 Account Login</div>
                    
                    <!-- Instagram Official Login Button -->
                    <button class="btn btn-primary" onclick="instagramOfficialLogin()" style="margin-bottom: 15px;">
                        <span>📱 Login with Instagram</span>
                    </button>
                    
                    <div style="text-align: center; margin: 10px 0; color: #666; font-weight: 600;">OR</div>
                    
                    <div class="login-form">
                        <div class="form-group">
                            <input type="text" class="form-input" id="loginUsername" placeholder="Instagram Username">
                        </div>
                        <div class="form-group">
                            <input type="password" class="form-input" id="loginPassword" placeholder="Instagram Password">
                        </div>
                        <div class="form-group" id="verificationCodeGroup" style="display: none;">
                            <input type="text" class="form-input" id="verificationCode" placeholder="Verification Code">
                        </div>
                        <button class="btn btn-secondary" onclick="loginAccount()">
                            <span>🔑 Manual Login</span>
                        </button>
                    </div>
                </div>
                
                <!-- Session Upload Section -->
                <div style="text-align: center; margin: 15px 0; color: #666; font-weight: 600;">OR</div>
                
                <div class="form-group">
                    <label class="input-label">📁 Import Session File:</label>
                    <input type="file" id="sessionFileInput" accept=".json" style="display: none;">
                    <button class="btn btn-secondary" onclick="document.getElementById('sessionFileInput').click()">
                        <span>📂 Browse Session File</span>
                    </button>
                    <div style="font-size: 12px; color: #666; text-align: center; margin-top: 8px;">
                        Select .json session file
                    </div>
                </div>
                
                <div class="section">
                    <div class="section-title">👥 Active Accounts</div>
                    <div id="accountsList">
                        <!-- Accounts will be loaded here -->
                    </div>
                </div>
                
                <div class="section">
                    <div class="section-title">⚙️ Quick Controls</div>
                    <button class="btn btn-success" onclick="startSending()" id="startBtn">
                        <span>🚀 Start Sending</span>
                    </button>
                    <button class="btn btn-danger" onclick="stopSending()" id="stopBtn">
                        <span>🛑 Stop Sending</span>
                    </button>
                    <button class="btn btn-secondary" onclick="clearLogs()">
                        <span>🗑️ Clear Logs</span>
                    </button>
                </div>
            </div>
            
            <div class="content-area">
                <!-- Same content as your second code -->
                <!-- Copy the exact same HTML structure from your second code -->
            </div>
        </div>
    </div>

    <script>
        // Same JavaScript as your second code with Instagram OAuth addition
        function instagramOfficialLogin() {
            // Open Instagram OAuth in new window
            const width = 600;
            const height = 700;
            const left = (screen.width - width) / 2;
            const top = (screen.height - height) / 2;
            
            window.open(
                '/instagram/login',
                'Instagram Login',
                `width=${width},height=${height},left=${left},top=${top}`
            );
        }
        
        // Rest of your JavaScript code from second version
    </script>
</body>
</html>
'''

# ---------- PRODUCTION SERVER COMPATIBLE ----------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('DEBUG', 'False').lower() == 'true'
    
    if debug:
        app.run(host='0.0.0.0', port=port, debug=True)
    else:
        from waitress import serve
        print(f"🚀 Starting production server on port {port}...")
        print(f"📱 Instagram OAuth: {'Enabled' if INSTAGRAM_APP_ID else 'Disabled'}")
        print(f"🔗 Live Hosting: READY")
        serve(app, host='0.0.0.0', port=port)
else:
    # For Gunicorn and other WSGI servers
    application = app
