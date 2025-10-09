import time, threading, random, json, os, concurrent.futures, secrets
from pathlib import Path
from flask import Flask, render_template_string, request, jsonify, session
from instagrapi import Client
from instagrapi.exceptions import ClientError, LoginRequired
import hashlib

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

# Session configuration for Railway
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=1800  # 30 minutes
)

# ---------- Persistent State Management ----------
class StateManager:
    def __init__(self):
        self.state_file = Path("app_state.json")
        self._ensure_state_file()
    
    def _ensure_state_file(self):
        if not self.state_file.exists():
            initial_state = {
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
            self.state_file.write_text(json.dumps(initial_state))
    
    def get_state(self):
        try:
            return json.loads(self.state_file.read_text())
        except:
            self._ensure_state_file()
            return self.get_state()
    
    def set_state(self, state):
        self.state_file.write_text(json.dumps(state, indent=2))
    
    def update_state(self, updates):
        state = self.get_state()
        state.update(updates)
        self.set_state(state)
    
    def add_log(self, message):
        state = self.get_state()
        ts = time.strftime("%H:%M:%S")
        state["logs"].append(f"[{ts}] {message}")
        if len(state["logs"]) > 25:
            state["logs"] = state["logs"][-25:]
        self.set_state(state)

# Initialize state manager
state_manager = StateManager()

WORKER = {"threads": [], "stop_flag": False}
lock = threading.Lock()

# ---------- Logging ----------
def log(msg):
    ts = time.strftime("%H:%M:%S")
    state_manager.add_log(msg)
    print(f"[{ts}] {msg}")

# ---------- Advanced Account Management ----------
class AdvancedAccountManager:
    def __init__(self):
        self.accounts = {}
        self.sessions_dir = Path("sessions")
        self.sessions_dir.mkdir(exist_ok=True)
        self.pending_verification = {}
        self.accounts_file = Path("accounts.json")  # ✅ Added accounts persistence
        self.load_accounts()
    
    def save_accounts(self):
        """Save accounts to persistent file"""
        accounts_data = {}
        for username, acc in self.accounts.items():
            accounts_data[username] = {
                'username': acc['username'],
                'full_name': acc['full_name'],
                'status': acc['status'],
                'is_active': acc['is_active'],
                'session_file': str(acc['session_file'])
            }
        self.accounts_file.write_text(json.dumps(accounts_data, indent=2))
    
    def load_accounts(self):
        """Load accounts from persistent file"""
        # Load from accounts.json if exists
        if self.accounts_file.exists():
            try:
                accounts_data = json.loads(self.accounts_file.read_text())
                for username, acc_data in accounts_data.items():
                    session_file = Path(acc_data['session_file'])
                    if session_file.exists():
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
                                'is_active': acc_data.get('is_active', False),
                                'worker_id': None
                            }
                            log(f"✅ Loaded account: {username}")
                        except Exception as e:
                            log(f"❌ Failed to load {username}: {str(e)[:200]}")
            except Exception as e:
                log(f"❌ Error loading accounts file: {e}")
        
        # Also load from session files as backup
        for session_file in self.sessions_dir.glob("*.json"):
            username = session_file.stem
            if username not in self.accounts:
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
                    log(f"✅ Loaded account from session: {username}")
                except Exception as e:
                    log(f"❌ Failed to load {username}: {str(e)[:200]}")
        
        # Save the final state
        self.save_accounts()
    
    def login_account(self, username, password, verification_code=None):
        """Login to Instagram account with OTP support"""
        try:
            cl = Client()
            cl.set_user_agent("Instagram 219.0.0.12.117 Android")
            
            if verification_code:
                log(f"🔄 Attempting login with OTP for {username}")
                cl.login(username, password, verification_code=verification_code)
                
                if username in self.pending_verification:
                    del self.pending_verification[username]
                    
            else:
                log(f"🔄 Attempting regular login for {username}")
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
            
            # ✅ Save to persistent file
            self.save_accounts()
            
            log(f"✅ Successfully logged in: {username}")
            return True, None
            
        except Exception as e:
            error_msg = str(e)
            log(f"❌ Login error for {username}: {error_msg}")
            
            if any(keyword in error_msg.lower() for keyword in ["checkpoint", "verification", "challenge", "2fa", "two-factor"]):
                log(f"🔐 OTP REQUIRED DETECTED for {username}")
                self.pending_verification[username] = {
                    'password': password,
                    'client': Client(),
                    'timestamp': time.time()
                }
                self.pending_verification[username]['client'].set_user_agent("Instagram 219.0.0.12.117 Android")
                return False, "verification_required"
            else:
                return False, error_msg
    
    def complete_verification(self, username, verification_code):
        """Complete login with verification code"""
        if username not in self.pending_verification:
            log(f"❌ No pending verification found for {username}")
            return False, "No pending verification found"
        
        try:
            pending_data = self.pending_verification[username]
            cl = pending_data['client']
            password = pending_data['password']
            
            log(f"🔄 Completing verification for {username} with OTP: {verification_code}")
            cl.login(username, password, verification_code=verification_code)
            
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
            
            del self.pending_verification[username]
            
            # ✅ Save to persistent file
            self.save_accounts()
            
            log(f"✅ OTP verification successful for {username}")
            return True, None
            
        except Exception as e:
            error_msg = str(e)
            log(f"❌ OTP verification failed for {username}: {error_msg}")
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
            self.save_accounts()  # ✅ Save state
            return True
        return False
    
    def deactivate_account(self, username):
        """Mark account as inactive"""
        if username in self.accounts:
            self.accounts[username]['is_active'] = False
            self.save_accounts()  # ✅ Save state
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
        return False, account_data['username']

def multi_account_sender_worker(accounts_list, thread_ids, messages, messages_per_second, max_per_run):
    """Multi-account message sender"""
    try:
        start_time = time.time()
        last_rate_check = start_time
        messages_since_check = 0
        
        # Update max messages in state
        state_manager.update_state({
            "stats": {"max_messages": max_per_run},
            "active_workers": len(accounts_list)
        })

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
                
                if messages_per_second > 0:
                    time.sleep(1.0 / messages_per_second)

            # Process results
            for future in concurrent.futures.as_completed(futures):
                try:
                    success, username = future.result()
                    
                    with lock:
                        state = state_manager.get_state()
                        if success:
                            state["stats"]["sent"] += 1
                            messages_since_check += 1
                            state["last_response"] = {
                                "account": username,
                                "timestamp": time.strftime("%H:%M:%S"),
                                "status": "sent"
                            }
                        else:
                            state["stats"]["failed"] += 1
                        state_manager.set_state(state)

                except Exception as e:
                    with lock:
                        state = state_manager.get_state()
                        state["stats"]["failed"] += 1
                        state_manager.set_state(state)
                    log(f"❌ Future error: {str(e)[:200]}")

                # Update rate every 2 seconds
                current_time = time.time()
                if current_time - last_rate_check >= 2.0:
                    actual_rate = (messages_since_check / (current_time - last_rate_check)) if (current_time - last_rate_check) > 0 else 0
                    state_manager.update_state({"stats": {"rate": round(actual_rate, 1)}})
                    messages_since_check = 0
                    last_rate_check = current_time

                # Progress updates
                state = state_manager.get_state()
                if state["stats"]["sent"] % 10 == 0:
                    progress = (state["stats"]["sent"] / max_per_run) * 100
                    log(f"📈 Progress: {state['stats']['sent']}/{max_per_run} ({progress:.1f}%)")

                if state["stats"]["sent"] >= max_per_run:
                    log(f"🎯 Reached maximum messages limit: {max_per_run}")
                    break

        # Final stats
        total_time = time.time() - start_time
        state = state_manager.get_state()
        final_rate = state["stats"]["sent"] / total_time if total_time > 0 else 0

        log(f"✅ Multi-account worker completed: {state['stats']['sent']} messages sent")
        log(f"⚡ Final rate: {final_rate:.1f} messages/second")
        log(f"👥 Active accounts used: {len(accounts_list)}")

    except Exception as e:
        log(f"💥 Multi-account worker error: {str(e)[:200]}")
    finally:
        state_manager.update_state({
            "running": False,
            "status": "idle",
            "active_workers": 0
        })
        WORKER["stop_flag"] = False
        # Deactivate all accounts
        for account in accounts_list:
            account_manager.deactivate_account(account['username'])

# ---------- ULTIMATE UI ----------
# [SAME TEMPLATE AS BEFORE - TOO LONG TO REPEAT]
# Copy the exact same TEMPLATE variable from your previous code

TEMPLATE = r'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>NovaGram Pro • Multi-Account DM Manager</title>
    <!-- COPY THE EXACT SAME STYLES AND HTML FROM YOUR PREVIOUS CODE -->
</head>
<body>
    <!-- COPY THE EXACT SAME HTML FROM YOUR PREVIOUS CODE -->
</body>
</html>'''

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
        
        state_manager.update_state({
            "threads": threads,
            "current_account": username
        })
        log(f"✅ Loaded {len(threads)} chats for {username}")
        return True, f"Loaded {len(threads)} conversations"
        
    except Exception as e:
        log(f"❌ Failed to load chats: {str(e)[:200]}")
        return False, str(e)[:200]

# ---------- Routes ----------
@app.route("/")
def index():
    return render_template_string(TEMPLATE)

@app.route("/state")
def get_state():
    state = state_manager.get_state()
    state["accounts"] = account_manager.get_accounts_list()
    return jsonify(state)
    
@app.route("/login", methods=["POST"])
def login():
    payload = request.get_json(force=True)
    username = payload.get("username")
    password = payload.get("password")
    
    if not username or not password:
        return jsonify({"ok": False, "message": "Username and password required"})
    
    success, error_type = account_manager.login_account(username, password)
    
    if success:
        log(f"✅ Account added: {username}")
        return jsonify({"ok": True, "message": "Login successful"})
    else:
        if error_type == "verification_required":
            log(f"🔐 OTP required for {username}")
            return jsonify({
                "ok": False, 
                "requires_verification": True, 
                "message": "Verification code required"
            })
        else:
            return jsonify({"ok": False, "message": f"Login failed: {error_type}"})

@app.route("/verify_otp", methods=["POST"])
def verify_otp():
    """Complete login with OTP verification"""
    payload = request.get_json(force=True)
    username = payload.get("username")
    verification_code = payload.get("verification_code")
    
    if not username or not verification_code:
        return jsonify({"ok": False, "message": "Username and verification code required"})
    
    success, error_msg = account_manager.complete_verification(username, verification_code)
    
    if success:
        log(f"✅ Account verified and added: {username}")
        return jsonify({"ok": True, "message": "Verification successful"})
    else:
        log(f"❌ OTP verification failed for {username}: {error_msg}")
        return jsonify({"ok": False, "message": f"Verification failed: {error_msg}"})

@app.route("/switch_account", methods=["POST"])
def switch_account():
    payload = request.get_json(force=True)
    username = payload.get("username")
    
    if username in account_manager.accounts:
        state_manager.update_state({
            "current_account": username,
            "threads": []
        })
        log(f"🔄 Switched to account: {username}")
        return jsonify({"ok": True, "message": f"Switched to {username}"})
    else:
        return jsonify({"ok": False, "message": "Account not found"})

@app.route("/load_chats", methods=["POST"])
def load_chats():
    payload = request.get_json(force=True)
    username = payload.get("username")
    
    success, message = load_chats_for_account(username)
    return jsonify({"ok": success, "message": message})

@app.route("/start_multi", methods=["POST"])
def start_multi_sending():
    state = state_manager.get_state()
    if state["running"]:
        return jsonify({"ok": False, "message": "Already running"})

    payload = request.get_json(force=True)
    accounts = payload.get("accounts", [])
    thread_ids = payload.get("thread_ids", [])
    messages = payload.get("messages", [])

    if not thread_ids:
        return jsonify({"ok": False, "message": "Select conversations"})

    if not messages:
        return jsonify({"ok": False, "message": "Enter messages"})

    if not accounts:
        return jsonify({"ok": False, "message": "Select accounts"})

    try:
        messages_per_second = int(payload.get("messages_per_second", 5))
    except Exception:
        messages_per_second = 5

    try:
        max_per_run = int(payload.get("max_per_run", 100))
    except Exception:
        max_per_run = 100

    # Get active accounts data
    active_accounts = []
    for username in accounts:
        account = account_manager.accounts.get(username)
        if account:
            active_accounts.append(account)
            account_manager.activate_account(username)

    if not active_accounts:
        return jsonify({"ok": False, "message": "No valid accounts selected"})

    state_manager.update_state({
        "running": True,
        "status": "running",
        "stats": {
            "sent": 0,
            "failed": 0,
            "rate": 0,
            "max_messages": max_per_run
        }
    })

    WORKER["threads"] = []
    WORKER["stop_flag"] = False

    t = threading.Thread(
        target=multi_account_sender_worker,
        args=(active_accounts, thread_ids, messages, messages_per_second, max_per_run),
        daemon=True
    )
    WORKER["threads"].append(t)
    t.start()

    log(f"🚀 Started multi-account sending with {len(active_accounts)} accounts")
    log(f"🎯 Target: {max_per_run} messages at {messages_per_second} msg/s")
    return jsonify({"ok": True, "message": f"Started multi-account sending with {len(active_accounts)} accounts"})

@app.route("/stop", methods=["POST"])
def stop_sending():
    WORKER["stop_flag"] = True
    for t in WORKER["threads"]:
        t.join(timeout=5)
    WORKER["threads"] = []
    
    state_manager.update_state({
        "running": False,
        "status": "idle", 
        "active_workers": 0
    })
    
    # Deactivate all accounts
    for account in account_manager.accounts.values():
        account['is_active'] = False
        
    log("🛑 All sending stopped")
    return jsonify({"ok": True, "message": "Stopped all sending operations"})

# ---------- Main ----------
if __name__ == "__main__":
    print("🚀 NovaGram Pro - Multi-Account Instagram DM Manager")
    print("📍 http://127.0.0.1:5000")
    print("👤 Owner: Nova • Telegram: @novaflexed")
    print("👥 MULTI-ACCOUNT SUPPORT: Multiple accounts simultaneously")
    print("🔐 AUTO OTP HANDLING: Automatic Instagram verification")
    print("💬 Smart conversation loading")
    print("⚡ Speed control: 1-20 messages/second")
    print("🎯 Custom max messages: 1-10,000")
    print("🔥 REAL-TIME MULTI-ACCOUNT WORKING")
    print("\n🔧 DEBUG MODE: OTP detection enabled")
    
    # Railway configuration
    port = int(os.environ.get("PORT", 5000))
    app.run(
        host='0.0.0.0', 
        port=port, 
        debug=False,
        threaded=False,
        processes=1
    )
