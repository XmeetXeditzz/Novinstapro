import time, threading, random, json, os, concurrent.futures, secrets, shutil
from pathlib import Path
from flask import Flask, render_template_string, request, jsonify
from instagrapi import Client
from instagrapi.exceptions import ClientError, LoginRequired
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

# Configuration
ALLOWED_EXTENSIONS = {'json'}
SESSION_UPLOAD_FOLDER = Path("uploaded_sessions")
SESSION_UPLOAD_FOLDER.mkdir(exist_ok=True)

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

# ---------- ULTIMATE UI ----------
TEMPLATE = r'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>NovaGram Pro • Multi-Account DM Manager</title>
    <style>
        :root {
            --primary: #667eea;
            --primary-dark: #5a67d8;
            --secondary: #764ba2;
            --success: #51cf66;
            --danger: #ff6b6b;
            --warning: #ffd43b;
            --dark: #2d3748;
            --light: #f8f9fa;
        }
        
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: #333;
            line-height: 1.6;
            min-height: 100vh;
        }
        
        .container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 20px;
        }
        
        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 25px;
            background: rgba(255, 255, 255, 0.95);
            border-radius: 20px;
            box-shadow: 0 10px 40px rgba(0, 0, 0, 0.1);
            margin-bottom: 30px;
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255, 255, 255, 0.2);
        }
        
        .logo {
            font-size: 32px;
            font-weight: 800;
            background: linear-gradient(45deg, #405de6, #5851db, #833ab4, #c13584, #e1306c, #fd1d1d);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            text-shadow: 0 2px 10px rgba(0, 0, 0, 0.1);
        }
        
        .credit {
            font-size: 14px;
            color: #666;
            text-align: right;
            font-weight: 500;
        }
        
        .status-badge {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 12px 20px;
            background: white;
            border-radius: 25px;
            border: 2px solid #e9ecef;
            font-size: 14px;
            font-weight: 600;
        }
        
        .status-dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: #8e8e8e;
            animation: pulse 2s infinite;
        }
        
        @keyframes pulse {
            0% { opacity: 1; }
            50% { opacity: 0.5; }
            100% { opacity: 1; }
        }
        
        .status-running { background: var(--success); }
        .status-error { background: var(--danger); }
        .status-warning { background: var(--warning); }
        
        .main-layout {
            display: grid;
            grid-template-columns: 400px 1fr;
            gap: 30px;
            min-height: 700px;
        }
        
        .sidebar {
            background: rgba(255, 255, 255, 0.95);
            border-radius: 20px;
            padding: 30px;
            box-shadow: 0 10px 40px rgba(0, 0, 0, 0.1);
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255, 255, 255, 0.2);
        }
        
        .section {
            margin-bottom: 35px;
        }
        
        .section-title {
            font-size: 20px;
            font-weight: 700;
            margin-bottom: 25px;
            color: var(--dark);
            border-bottom: 3px solid var(--light);
            padding-bottom: 15px;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        
        .section-title::before {
            content: '';
            width: 4px;
            height: 20px;
            background: var(--primary);
            border-radius: 2px;
        }
        
        .account-card {
            display: flex;
            align-items: center;
            gap: 15px;
            padding: 20px;
            border-radius: 15px;
            cursor: pointer;
            margin-bottom: 12px;
            border: 2px solid transparent;
            transition: all 0.3s ease;
            background: var(--light);
            position: relative;
            overflow: hidden;
        }
        
        .account-card::before {
            content: '';
            position: absolute;
            top: 0;
            left: -100%;
            width: 100%;
            height: 100%;
            background: linear-gradient(90deg, transparent, rgba(255,255,255,0.4), transparent);
            transition: left 0.5s;
        }
        
        .account-card:hover::before {
            left: 100%;
        }
        
        .account-card:hover {
            transform: translateY(-3px);
            box-shadow: 0 8px 25px rgba(102, 126, 234, 0.15);
        }
        
        .account-card.active {
            background: linear-gradient(135deg, var(--primary) 0%, var(--secondary) 100%);
            color: white;
            border-color: var(--primary-dark);
            box-shadow: 0 8px 25px rgba(102, 126, 234, 0.3);
        }
        
        .account-avatar {
            width: 50px;
            height: 50px;
            border-radius: 50%;
            background: linear-gradient(45deg, #405de6, #5851db, #833ab4);
            display: flex;
            align-items: center;
            justify-content: center;
            color: white;
            font-weight: 700;
            font-size: 20px;
            box-shadow: 0 4px 15px rgba(0, 0, 0, 0.1);
        }
        
        .account-info {
            flex: 1;
        }
        
        .account-name {
            font-weight: 700;
            font-size: 16px;
            margin-bottom: 4px;
        }
        
        .account-status {
            font-size: 13px;
            opacity: 0.8;
            display: flex;
            align-items: center;
            gap: 5px;
        }
        
        .status-indicator {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: #8e8e8e;
        }
        
        .status-online { background: var(--success); }
        .status-offline { background: var(--danger); }
        .status-working { background: var(--warning); }
        
        .account-select {
            position: absolute;
            top: 15px;
            right: 15px;
            width: 20px;
            height: 20px;
            border: 2px solid #ddd;
            border-radius: 4px;
            background: white;
            transition: all 0.3s ease;
        }
        
        .account-card.active .account-select {
            background: var(--primary-dark);
            border-color: var(--primary-dark);
        }
        
        .account-card.active .account-select::after {
            content: '✓';
            color: white;
            font-size: 12px;
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
        }
        
        .chat-item {
            display: flex;
            align-items: center;
            gap: 15px;
            padding: 18px;
            border-radius: 15px;
            cursor: pointer;
            margin-bottom: 10px;
            border: 2px solid transparent;
            transition: all 0.3s ease;
            background: var(--light);
        }
        
        .chat-item:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(0, 0, 0, 0.1);
        }
        
        .chat-item.selected {
            background: linear-gradient(135deg, var(--primary) 0%, var(--secondary) 100%);
            color: white;
            border-color: var(--primary-dark);
        }
        
        .chat-avatar {
            width: 45px;
            height: 45px;
            border-radius: 50%;
            background: var(--dark);
            display: flex;
            align-items: center;
            justify-content: center;
            color: white;
            font-weight: 600;
            font-size: 16px;
            box-shadow: 0 3px 10px rgba(0, 0, 0, 0.1);
        }
        
        .chat-name {
            font-weight: 600;
            font-size: 15px;
        }
        
        .btn {
            padding: 16px 28px;
            border: none;
            border-radius: 15px;
            font-weight: 700;
            cursor: pointer;
            font-size: 16px;
            transition: all 0.3s ease;
            width: 100%;
            margin-bottom: 15px;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
            position: relative;
            overflow: hidden;
        }
        
        .btn::before {
            content: '';
            position: absolute;
            top: 0;
            left: -100%;
            width: 100%;
            height: 100%;
            background: linear-gradient(90deg, transparent, rgba(255,255,255,0.2), transparent);
            transition: left 0.5s;
        }
        
        .btn:hover::before {
            left: 100%;
        }
        
        .btn-primary {
            background: linear-gradient(135deg, var(--primary) 0%, var(--secondary) 100%);
            color: white;
        }
        
        .btn-primary:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 30px rgba(102, 126, 234, 0.4);
        }
        
        .btn-danger {
            background: linear-gradient(135deg, var(--danger) 0%, #ee5a24 100%);
            color: white;
        }
        
        .btn-danger:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 30px rgba(255, 107, 107, 0.4);
        }
        
        .btn-secondary {
            background: white;
            color: var(--dark);
            border: 2px solid #e9ecef;
        }
        
        .btn-secondary:hover {
            background: var(--light);
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(0, 0, 0, 0.1);
        }
        
        .btn-success {
            background: linear-gradient(135deg, var(--success) 0%, #40c057 100%);
            color: white;
        }
        
        .btn-success:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 30px rgba(81, 207, 102, 0.4);
        }
        
        .btn:disabled {
            opacity: 0.6;
            cursor: not-allowed;
            transform: none !important;
            box-shadow: none !important;
        }
        
        .content-area {
            background: rgba(255, 255, 255, 0.95);
            border-radius: 20px;
            padding: 30px;
            box-shadow: 0 10px 40px rgba(0, 0, 0, 0.1);
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255, 255, 255, 0.2);
        }
        
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 20px;
            margin-bottom: 30px;
        }
        
        .stat-card {
            background: white;
            padding: 25px;
            border-radius: 15px;
            text-align: center;
            box-shadow: 0 5px 15px rgba(0, 0, 0, 0.08);
            border: 2px solid #f1f3f4;
            transition: transform 0.3s ease;
        }
        
        .stat-card:hover {
            transform: translateY(-5px);
        }
        
        .stat-value {
            font-size: 32px;
            font-weight: 800;
            margin-bottom: 8px;
        }
        
        .stat-sent { color: var(--success); }
        .stat-failed { color: var(--danger); }
        .stat-rate { color: var(--primary); }
        .stat-max { color: var(--secondary); }
        
        .stat-label {
            font-size: 14px;
            color: #666;
            font-weight: 600;
        }
        
        .input-group {
            margin-bottom: 25px;
        }
        
        .input-label {
            display: block;
            margin-bottom: 10px;
            font-weight: 600;
            color: var(--dark);
        }
        
        .textarea, .input {
            width: 100%;
            padding: 18px;
            border: 2px solid #e9ecef;
            border-radius: 15px;
            font-size: 16px;
            transition: all 0.3s ease;
            background: white;
            resize: vertical;
        }
        
        .textarea:focus, .input:focus {
            outline: none;
            border-color: var(--primary);
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
        }
        
        .textarea {
            min-height: 120px;
            font-family: 'Segoe UI', sans-serif;
        }
        
        .logs-container {
            background: var(--dark);
            color: white;
            padding: 20px;
            border-radius: 15px;
            font-family: 'Courier New', monospace;
            font-size: 14px;
            max-height: 300px;
            overflow-y: auto;
            margin-top: 20px;
        }
        
        .log-entry {
            padding: 8px 0;
            border-bottom: 1px solid rgba(255, 255, 255, 0.1);
        }
        
        .log-entry:last-child {
            border-bottom: none;
        }
        
        .login-form {
            background: white;
            padding: 30px;
            border-radius: 20px;
            margin-bottom: 25px;
            box-shadow: 0 5px 20px rgba(0, 0, 0, 0.08);
        }
        
        .form-group {
            margin-bottom: 20px;
        }
        
        .form-input {
            width: 100%;
            padding: 15px;
            border: 2px solid #e9ecef;
            border-radius: 12px;
            font-size: 16px;
            transition: all 0.3s ease;
        }
        
        .form-input:focus {
            outline: none;
            border-color: var(--primary);
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
        }
        
        .control-panel {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 15px;
            margin-top: 25px;
        }
        
        @media (max-width: 768px) {
            .main-layout {
                grid-template-columns: 1fr;
            }
            
            .stats-grid {
                grid-template-columns: repeat(2, 1fr);
            }
            
            .control-panel {
                grid-template-columns: 1fr;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div>
                <div class="logo">NovaGram Pro</div>
                <div class="credit">Multi-Account DM Manager • Powered by Instagrapi</div>
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
                        <button class="btn btn-primary" onclick="loginAccount()">
                            <span>🚀 Login Account</span>
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
                <div class="stats-grid">
                    <div class="stat-card">
                        <div class="stat-value stat-sent" id="sentCount">0</div>
                        <div class="stat-label">Messages Sent</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-value stat-failed" id="failedCount">0</div>
                        <div class="stat-label">Failed</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-value stat-rate" id="rateValue">0/s</div>
                        <div class="stat-label">Current Rate</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-value stat-max" id="maxMessages">100</div>
                        <div class="stat-label">Max Messages</div>
                    </div>
                </div>
                
                <div class="section">
                    <div class="section-title">📝 Configuration</div>
                    
                    <div class="input-group">
                        <label class="input-label">📋 Thread IDs (one per line):</label>
                        <textarea class="textarea" id="threadIds" placeholder="Enter Thread IDs...">123456789
987654321</textarea>
                    </div>
                    
                    <div class="input-group">
                        <label class="input-label">💬 Messages (one per line, random selection):</label>
                        <textarea class="textarea" id="messages" placeholder="Enter your messages...">Hello! 👋
How are you doing? 😊
Check this out! 🚀</textarea>
                    </div>
                    
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px;">
                        <div class="input-group">
                            <label class="input-label">⚡ Messages Per Second:</label>
                            <input type="number" class="input" id="messagesPerSecond" value="1" min="0.1" max="10" step="0.1">
                        </div>
                        <div class="input-group">
                            <label class="input-label">🎯 Max Messages:</label>
                            <input type="number" class="input" id="maxMessagesInput" value="100" min="1" max="1000">
                        </div>
                    </div>
                </div>
                
                <div class="section">
                    <div class="section-title">📊 Live Logs</div>
                    <div class="logs-container" id="logsContainer">
                        <!-- Logs will appear here -->
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script>
        let statusInterval;
        
        function updateStatus() {
            fetch('/api/status')
                .then(r => r.json())
                .then(data => {
                    // Update status
                    document.getElementById('statusText').textContent = data.status;
                    const statusDot = document.getElementById('statusDot');
                    statusDot.className = 'status-dot ' + 
                        (data.running ? 'status-running' : 
                         data.status === 'error' ? 'status-error' : 
                         data.status === 'warning' ? 'status-warning' : '');
                    
                    // Update stats
                    document.getElementById('sentCount').textContent = data.stats.sent;
                    document.getElementById('failedCount').textContent = data.stats.failed;
                    document.getElementById('rateValue').textContent = data.stats.rate + '/s';
                    document.getElementById('maxMessages').textContent = data.stats.max_messages;
                    
                    // Update logs
                    const logsContainer = document.getElementById('logsContainer');
                    logsContainer.innerHTML = data.logs.map(log => 
                        `<div class="log-entry">${log}</div>`
                    ).join('');
                    logsContainer.scrollTop = logsContainer.scrollHeight;
                    
                    // Update button states
                    document.getElementById('startBtn').disabled = data.running;
                    document.getElementById('stopBtn').disabled = !data.running;
                })
                .catch(err => {
                    console.error('Status update error:', err);
                });
        }
        
        function loadAccounts() {
            fetch('/api/accounts')
                .then(r => r.json())
                .then(data => {
                    const container = document.getElementById('accountsList');
                    if (data.accounts && data.accounts.length === 0) {
                        container.innerHTML = '<div style="text-align: center; color: #666; padding: 20px;">No accounts loaded</div>';
                        return;
                    }
                    
                    container.innerHTML = data.accounts.map(acc => `
                        <div class="account-card ${acc.is_active ? 'active' : ''}" onclick="toggleAccount('${acc.username}')">
                            <div class="account-avatar">${acc.username.charAt(0).toUpperCase()}</div>
                            <div class="account-info">
                                <div class="account-name">${acc.username}</div>
                                <div class="account-status">
                                    <div class="status-indicator ${acc.status === 'online' ? 'status-online' : 'status-offline'}"></div>
                                    ${acc.full_name || 'Instagram User'}
                                </div>
                            </div>
                            <div class="account-select"></div>
                        </div>
                    `).join('');
                })
                .catch(err => {
                    console.error('Load accounts error:', err);
                });
        }
        
        function loginAccount() {
            const username = document.getElementById('loginUsername').value;
            const password = document.getElementById('loginPassword').value;
            const verificationCode = document.getElementById('verificationCode').value;
            
            if (!username || !password) {
                alert('Please enter username and password');
                return;
            }
            
            fetch('/api/login', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({username, password, verification_code: verificationCode})
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    alert('Login successful!');
                    document.getElementById('loginUsername').value = '';
                    document.getElementById('loginPassword').value = '';
                    document.getElementById('verificationCode').value = '';
                    document.getElementById('verificationCodeGroup').style.display = 'none';
                    loadAccounts();
                } else {
                    if (data.error === 'verification_required') {
                        document.getElementById('verificationCodeGroup').style.display = 'block';
                        alert('Verification code required. Please check your Instagram app.');
                    } else {
                        alert('Login failed: ' + data.error);
                    }
                }
            })
            .catch(err => {
                alert('Login error: ' + err);
            });
        }
        
        function handleSessionFileUpload(event) {
            const file = event.target.files[0];
            if (!file) return;
            
            if (!file.name.endsWith('.json')) {
                alert('Please select a valid JSON session file');
                return;
            }
            
            const formData = new FormData();
            formData.append('session_file', file);
            
            // Show loading
            const uploadButton = event.target.closest('.form-group').querySelector('button');
            const originalText = uploadButton.querySelector('span').textContent;
            uploadButton.querySelector('span').textContent = 'Uploading...';
            uploadButton.disabled = true;
            
            fetch('/api/upload_session', {
                method: 'POST',
                body: formData
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    alert('✅ Session imported successfully!');
                    loadAccounts();
                } else {
                    alert('❌ Import failed: ' + data.error);
                }
            })
            .catch(err => {
                alert('Upload error: ' + err);
            })
            .finally(() => {
                // Reset button
                uploadButton.querySelector('span').textContent = originalText;
                uploadButton.disabled = false;
                event.target.value = '';
            });
        }
        
        function toggleAccount(username) {
            const accountCard = event.currentTarget;
            const isActive = accountCard.classList.contains('active');
            
            fetch('/api/account/toggle', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({username, activate: !isActive})
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    loadAccounts();
                } else {
                    alert('Error: ' + data.error);
                }
            });
        }
        
        function startSending() {
            const threadIds = document.getElementById('threadIds').value.split('\n').filter(t => t.trim());
            const messages = document.getElementById('messages').value.split('\n').filter(m => m.trim());
            const messagesPerSecond = parseFloat(document.getElementById('messagesPerSecond').value);
            const maxMessages = parseInt(document.getElementById('maxMessagesInput').value);
            
            if (threadIds.length === 0 || messages.length === 0) {
                alert('Please enter at least one thread ID and one message');
                return;
            }
            
            fetch('/api/start', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    thread_ids: threadIds,
                    messages: messages,
                    messages_per_second: messagesPerSecond,
                    max_messages: maxMessages
                })
            })
            .then(r => r.json())
            .then(data => {
                if (!data.success) {
                    alert('Error: ' + data.error);
                }
            })
            .catch(err => {
                alert('Start error: ' + err);
            });
        }
        
        function stopSending() {
            fetch('/api/stop', {
                method: 'POST'
            })
            .then(r => r.json())
            .then(data => {
                // Status will update automatically
            });
        }
        
        function clearLogs() {
            fetch('/api/clear_logs', {
                method: 'POST'
            });
        }
       
        // Initialize
        document.addEventListener('DOMContentLoaded', function() {
            // Add event listener for file input
            const fileInput = document.getElementById('sessionFileInput');
            if (fileInput) {
                fileInput.addEventListener('change', handleSessionFileUpload);
            }
            
            loadAccounts();
            statusInterval = setInterval(updateStatus, 2000);
            updateStatus();
        });
    </script>
</body>
</html>'''

# ---------- PRODUCTION SERVER COMPATIBLE ----------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('DEBUG', 'False').lower() == 'true'
    
    if debug:
        app.run(host='0.0.0.0', port=port, debug=True)
    else:
        from waitress import serve
        print(f"🚀 Starting production server on port {port}...")
        serve(app, host='0.0.0.0', port=port)
else:
    # For Gunicorn and other WSGI servers
    application = app
