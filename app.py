import time, threading, random, json, os, concurrent.futures, secrets
from pathlib import Path
from flask import Flask, render_template_string, request, jsonify, session
from instagrapi import Client
from instagrapi.exceptions import ClientError, LoginRequired
import hashlib

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)

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
    STATE["logs"].append(f"[{ts}] {msg}")
    if len(STATE["logs"]) > 25:
        STATE["logs"] = STATE["logs"][-25:]

# ---------- Advanced Account Management ----------
class AdvancedAccountManager:
    def __init__(self):
        self.accounts = {}
        self.sessions_dir = Path("sessions")
        self.sessions_dir.mkdir(exist_ok=True)
        self.pending_verification = {}  # Store pending logins needing OTP
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
                log(f"🔄 Attempting login with OTP for {username}")
                cl.login(username, password, verification_code=verification_code)
                
                # Clear pending verification
                if username in self.pending_verification:
                    del self.pending_verification[username]
                    
            else:
                # Regular login
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
            
            log(f"✅ Successfully logged in: {username}")
            return True, None
            
        except Exception as e:
            error_msg = str(e)
            log(f"❌ Login error for {username}: {error_msg[:200]}")
            
            if "checkpoint" in error_msg.lower() or "verification" in error_msg.lower():
                # Store login credentials for OTP verification
                log(f"🔐 OTP required for {username}")
                self.pending_verification[username] = {
                    'password': password,
                    'client': Client(),
                    'timestamp': time.time()
                }
                self.pending_verification[username]['client'].set_user_agent("Instagram 219.0.0.12.117 Android")
                return False, "verification_required"
            elif "challenge" in error_msg.lower():
                log(f"🔐 Challenge required for {username}")
                self.pending_verification[username] = {
                    'password': password,
                    'client': Client(),
                    'timestamp': time.time()
                }
                self.pending_verification[username]['client'].set_user_agent("Instagram 219.0.0.12.117 Android")
                return False, "challenge_required"
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
            
            log(f"🔄 Completing verification for {username} with OTP")
            
            # Complete login with verification code
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
            
            # Clear pending verification
            del self.pending_verification[username]
            
            log(f"✅ OTP verification successful for {username}")
            return True, None
            
        except Exception as e:
            error_msg = str(e)
            log(f"❌ OTP verification failed for {username}: {error_msg[:200]}")
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
        
        .main-content {
            background: rgba(255, 255, 255, 0.95);
            border-radius: 20px;
            padding: 35px;
            box-shadow: 0 10px 40px rgba(0, 0, 0, 0.1);
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255, 255, 255, 0.2);
        }
        
        .login-section {
            text-align: center;
            padding: 60px 40px;
        }
        
        .login-title {
            font-size: 36px;
            font-weight: 800;
            margin-bottom: 50px;
            background: linear-gradient(45deg, #405de6, #5851db, #833ab4);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            text-shadow: 0 2px 10px rgba(0, 0, 0, 0.1);
        }
        
        .login-form {
            max-width: 450px;
            margin: 0 auto;
            display: flex;
            flex-direction: column;
            gap: 20px;
        }
        
        .form-group {
            text-align: left;
        }
        
        .form-label {
            display: block;
            margin-bottom: 8px;
            font-weight: 600;
            color: var(--dark);
        }
        
        .form-input {
            padding: 18px 25px;
            border: 2px solid #e9ecef;
            border-radius: 12px;
            font-size: 16px;
            background: white;
            transition: all 0.3s ease;
            width: 100%;
        }
        
        .form-input:focus {
            outline: none;
            border-color: var(--primary);
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
            transform: translateY(-1px);
        }
        
        .control-section {
            display: flex;
            flex-direction: column;
            gap: 30px;
        }
        
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 25px;
            margin-bottom: 30px;
        }
        
        .stat-card {
            background: linear-gradient(135deg, var(--primary) 0%, var(--secondary) 100%);
            padding: 30px;
            border-radius: 20px;
            text-align: center;
            color: white;
            box-shadow: 0 10px 30px rgba(102, 126, 234, 0.3);
            position: relative;
            overflow: hidden;
        }
        
        .stat-card::before {
            content: '';
            position: absolute;
            top: -50%;
            left: -50%;
            width: 200%;
            height: 200%;
            background: linear-gradient(45deg, transparent, rgba(255,255,255,0.1), transparent);
            transform: rotate(45deg);
            animation: shine 3s infinite;
        }
        
        @keyframes shine {
            0% { transform: translateX(-100%) translateY(-100%) rotate(45deg); }
            100% { transform: translateX(100%) translateY(100%) rotate(45deg); }
        }
        
        .stat-number {
            font-size: 42px;
            font-weight: 800;
            margin-bottom: 10px;
            text-shadow: 0 2px 10px rgba(0, 0, 0, 0.2);
        }
        
        .stat-label {
            font-size: 14px;
            opacity: 0.9;
            text-transform: uppercase;
            letter-spacing: 1.5px;
            font-weight: 600;
        }
        
        .control-panel {
            background: var(--light);
            padding: 30px;
            border-radius: 20px;
            margin: 20px 0;
        }
        
        .control-group {
            margin-bottom: 25px;
        }
        
        .control-label {
            display: block;
            margin-bottom: 12px;
            font-weight: 700;
            color: var(--dark);
            font-size: 16px;
        }
        
        .slider-container {
            display: flex;
            align-items: center;
            gap: 25px;
            margin-top: 15px;
        }
        
        .slider {
            flex: 1;
            height: 8px;
            background: #e9ecef;
            border-radius: 4px;
            outline: none;
            -webkit-appearance: none;
        }
        
        .slider::-webkit-slider-thumb {
            -webkit-appearance: none;
            width: 24px;
            height: 24px;
            background: var(--primary);
            border-radius: 50%;
            cursor: pointer;
            box-shadow: 0 4px 15px rgba(102, 126, 234, 0.3);
            border: 3px solid white;
        }
        
        .value-display {
            min-width: 70px;
            text-align: center;
            font-weight: 800;
            font-size: 20px;
            color: var(--primary);
            background: white;
            padding: 8px 15px;
            border-radius: 10px;
            box-shadow: 0 3px 10px rgba(0, 0, 0, 0.1);
        }
        
        .message-inputs {
            margin: 25px 0;
        }
        
        .message-input-container {
            display: flex;
            align-items: center;
            gap: 15px;
            margin-bottom: 15px;
        }
        
        .message-input {
            flex: 1;
            padding: 18px 25px;
            border: 2px solid #e9ecef;
            border-radius: 12px;
            font-size: 16px;
            background: white;
            transition: all 0.3s ease;
        }
        
        .message-input:focus {
            outline: none;
            border-color: var(--primary);
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
        }
        
        .remove-message-btn {
            background: var(--danger);
            color: white;
            border: none;
            border-radius: 50%;
            width: 40px;
            height: 40px;
            cursor: pointer;
            font-size: 20px;
            font-weight: bold;
            display: flex;
            align-items: center;
            justify-content: center;
            flex-shrink: 0;
            transition: all 0.3s ease;
        }
        
        .remove-message-btn:hover {
            background: #ee5a24;
            transform: scale(1.1) rotate(90deg);
        }
        
        .add-message-btn {
            background: var(--primary);
            color: white;
            border: none;
            border-radius: 50%;
            width: 50px;
            height: 50px;
            cursor: pointer;
            font-size: 24px;
            font-weight: bold;
            display: flex;
            align-items: center;
            justify-content: center;
            margin: 20px auto;
            transition: all 0.3s ease;
            box-shadow: 0 5px 15px rgba(102, 126, 234, 0.3);
        }
        
        .add-message-btn:hover {
            background: var(--primary-dark);
            transform: scale(1.1) rotate(90deg);
        }
        
        .checkbox-group {
            display: flex;
            align-items: center;
            gap: 15px;
            margin: 25px 0;
            padding: 20px;
            background: white;
            border-radius: 15px;
            box-shadow: 0 3px 15px rgba(0, 0, 0, 0.1);
        }
        
        .progress-container {
            margin: 25px 0;
        }
        
        .progress-header {
            display: flex;
            justify-content: between;
            align-items: center;
            margin-bottom: 15px;
        }
        
        .progress-bar {
            width: 100%;
            height: 12px;
            background: #e9ecef;
            border-radius: 6px;
            overflow: hidden;
            margin: 15px 0;
            box-shadow: inset 0 2px 5px rgba(0, 0, 0, 0.1);
        }
        
        .progress-fill {
            height: 100%;
            background: linear-gradient(135deg, var(--primary) 0%, var(--secondary) 100%);
            transition: width 0.5s ease;
            border-radius: 6px;
            position: relative;
            overflow: hidden;
        }
        
        .progress-fill::after {
            content: '';
            position: absolute;
            top: 0;
            left: -100%;
            width: 100%;
            height: 100%;
            background: linear-gradient(90deg, transparent, rgba(255,255,255,0.4), transparent);
            animation: progressShine 2s infinite;
        }
        
        @keyframes progressShine {
            0% { left: -100%; }
            100% { left: 100%; }
        }
        
        .action-buttons {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
            margin: 30px 0;
        }
        
        .logs-panel {
            background: #1a1a1a;
            border: 2px solid #333;
            border-radius: 15px;
            padding: 25px;
            height: 250px;
            overflow-y: auto;
            font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
            font-size: 14px;
            margin-top: 30px;
            color: #00ff00;
            box-shadow: inset 0 0 20px rgba(0, 0, 0, 0.5);
        }
        
        .log-entry {
            padding: 8px 0;
            border-bottom: 1px solid rgba(255, 255, 255, 0.1);
            display: flex;
            align-items: center;
            gap: 10px;
        }
        
        .log-time {
            color: #888;
            margin-right: 10px;
            min-width: 70px;
        }
        
        .hidden {
            display: none !important;
        }
        
        .multi-account-info {
            background: linear-gradient(135deg, var(--warning) 0%, #fcc419 100%);
            color: var(--dark);
            padding: 20px;
            border-radius: 15px;
            margin: 20px 0;
            text-align: center;
            font-weight: 600;
        }
        
        .accounts-counter {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
            font-size: 18px;
            margin-top: 10px;
        }
        
        /* Instagram-like OTP Modal */
        .modal-overlay {
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.7);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 1000;
            backdrop-filter: blur(5px);
        }
        
        .otp-modal {
            background: white;
            border-radius: 20px;
            padding: 40px;
            max-width: 450px;
            width: 90%;
            text-align: center;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
            animation: modalSlideIn 0.3s ease-out;
        }
        
        @keyframes modalSlideIn {
            from {
                opacity: 0;
                transform: translateY(-50px) scale(0.9);
            }
            to {
                opacity: 1;
                transform: translateY(0) scale(1);
            }
        }
        
        .otp-icon {
            font-size: 64px;
            margin-bottom: 20px;
        }
        
        .otp-title {
            font-size: 28px;
            font-weight: 700;
            margin-bottom: 15px;
            color: var(--dark);
        }
        
        .otp-subtitle {
            color: #666;
            margin-bottom: 30px;
            line-height: 1.5;
        }
        
        .otp-input-group {
            margin: 30px 0;
        }
        
        .otp-input {
            width: 100%;
            padding: 20px;
            font-size: 18px;
            text-align: center;
            border: 2px solid #e9ecef;
            border-radius: 12px;
            background: #f8f9fa;
            transition: all 0.3s ease;
            font-weight: 600;
            letter-spacing: 2px;
        }
        
        .otp-input:focus {
            outline: none;
            border-color: var(--primary);
            background: white;
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
            transform: translateY(-2px);
        }
        
        .otp-actions {
            display: flex;
            gap: 15px;
            margin-top: 25px;
        }
        
        .otp-btn {
            flex: 1;
            padding: 16px;
            border: none;
            border-radius: 12px;
            font-weight: 700;
            cursor: pointer;
            transition: all 0.3s ease;
        }
        
        .otp-btn-primary {
            background: linear-gradient(135deg, var(--primary) 0%, var(--secondary) 100%);
            color: white;
        }
        
        .otp-btn-primary:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 25px rgba(102, 126, 234, 0.4);
        }
        
        .otp-btn-secondary {
            background: #f8f9fa;
            color: var(--dark);
            border: 2px solid #e9ecef;
        }
        
        .otp-btn-secondary:hover {
            background: white;
            transform: translateY(-2px);
        }
        
        .security-notice {
            background: #fff3cd;
            border: 1px solid #ffeaa7;
            border-radius: 10px;
            padding: 15px;
            margin: 20px 0;
            text-align: left;
            font-size: 14px;
            color: #856404;
        }
        
        .security-notice strong {
            display: block;
            margin-bottom: 5px;
        }
    </style>
    <style>
        /* Mobile responsive styles */
        @media (max-width: 768px) {
            .container {
                padding: 10px;
                max-width: 100%;
            }
            .header {
                flex-direction: column;
                align-items: flex-start;
                gap: 15px;
                padding: 20px;
            }
            .main-layout {
                display: block;
                min-height: auto;
            }
            .sidebar {
                width: 100%;
                border-radius: 15px;
                padding: 25px;
                margin-bottom: 20px;
            }
            .main-content {
                width: 100%;
                border-radius: 15px;
                padding: 25px;
            }
            .stats-grid {
                grid-template-columns: repeat(2, 1fr);
                gap: 15px;
            }
            .account-card, .chat-item {
                padding: 15px;
                font-size: 14px;
            }
            .btn {
                font-size: 16px;
                padding: 18px 20px;
            }
            .form-input, .message-input {
                font-size: 16px;
                padding: 16px;
            }
            .message-box {
                font-size: 16px;
                min-height: 150px;
            }
            .stat-number {
                font-size: 32px;
            }
            .stat-label {
                font-size: 12px;
            }
            .slider-container {
                gap: 15px;
            }
            .value-display {
                font-size: 18px;
                min-width: 60px;
            }
            .logs-panel {
                height: 200px;
                font-size: 12px;
            }
            .action-buttons {
                grid-template-columns: 1fr;
                gap: 15px;
            }
            .otp-modal {
                padding: 30px 25px;
                margin: 20px;
            }
            .otp-actions {
                flex-direction: column;
            }
        }
        
        @media (max-width: 480px) {
            .stats-grid {
                grid-template-columns: 1fr;
            }
            .login-section {
                padding: 40px 20px;
            }
            .login-title {
                font-size: 28px;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div>
                <div class="logo">NovaGram Pro</div>
                <div class="credit">Multi-Account DM Manager • By Nova (@novaflexed)</div>
            </div>
            <div class="status-badge">
                <div class="status-dot" id="status_dot"></div>
                <span id="status_text">Ready</span>
                <span id="active_workers">• 0 Workers</span>
            </div>
        </div>
        
        <div class="main-layout">
            <!-- Sidebar -->
            <div class="sidebar">
                <div class="section">
                    <div class="section-title">👥 Accounts Manager</div>
                    <div id="accounts_list">
                        <!-- Accounts will be loaded here -->
                    </div>
                    <button class="btn btn-secondary" onclick="showLogin()">
                        <span>➕</span> Add New Account
                    </button>
                </div>
                
                <div class="section">
                    <div class="section-title">💬 Conversations</div>
                    <div id="chats_list">
                        <!-- Chats will be loaded here -->
                    </div>
                    <button class="btn btn-secondary" onclick="loadChats()" id="refresh_chats_btn">
                        <span>🔄</span> Refresh Chats
                    </button>
                </div>
            </div>
            
            <!-- Main Content -->
            <div class="main-content">
                <!-- Login Section -->
                <div id="login_section" class="login-section">
                    <div class="login-title">Add Instagram Account</div>
                    
                    <div class="login-form">
                        <div class="form-group">
                            <label class="form-label">Username</label>
                            <input type="text" id="username" class="form-input" placeholder="Enter Instagram username" autocomplete="username">
                        </div>
                        
                        <div class="form-group">
                            <label class="form-label">Password</label>
                            <input type="password" id="password" class="form-input" placeholder="Enter password" autocomplete="current-password">
                        </div>
                        
                        <button class="btn btn-primary" onclick="loginAccount()" id="login_btn">
                            <span>🚀</span> Login to Instagram
                        </button>
                        <div id="login_status" style="font-size: 14px; margin-top: 20px; padding: 15px; border-radius: 10px; display: none;"></div>
                    </div>
                </div>
                
                <!-- Control Section -->
                <div id="control_section" class="control-section hidden">
                    <!-- Multi-Account Info -->
                    <div class="multi-account-info" id="multi_account_info" style="display: none;">
                        <div>🎯 Multi-Account Mode Active</div>
                        <div class="accounts-counter">
                            <span id="active_accounts_count">0</span> accounts selected for sending
                        </div>
                    </div>
                    
                    <div class="stats-grid">
                        <div class="stat-card">
                            <div class="stat-number" id="sent_count">0</div>
                            <div class="stat-label">Messages Sent</div>
                        </div>
                        <div class="stat-card">
                            <div class="stat-number" id="failed_count">0</div>
                            <div class="stat-label">Failed</div>
                        </div>
                        <div class="stat-card">
                            <div class="stat-number" id="rate_display">0</div>
                            <div class="stat-label">Per Second</div>
                        </div>
                        <div class="stat-card">
                            <div class="stat-number" id="max_messages_display">100</div>
                            <div class="stat-label">Max Messages</div>
                        </div>
                    </div>
                    
                    <div class="control-panel">
                        <div class="control-group">
                            <label class="control-label">⚡ Messages Per Second</label>
                            <div class="slider-container">
                                <input type="range" min="1" max="20" value="5" class="slider" id="speed_slider">
                                <div class="value-display" id="speed_value">5/s</div>
                            </div>
                        </div>

                        <div class="control-group">
                            <label class="control-label">🎯 Maximum Messages to Send</label>
                            <input type="number" min="1" max="10000" value="100" class="form-input" id="max_messages_input">
                        </div>
                    </div>
                    
                    <div class="message-inputs">
                        <label class="control-label">💌 Messages (Randomly Selected)</label>
                        <div id="message_inputs">
                            <div class="message-input-container">
                                <input type="text" class="message-input" placeholder="Enter message" value="Hello! 👋">
                                <button class="remove-message-btn" onclick="removeMessage(this)">-</button>
                            </div>
                            <div class="message-input-container">
                                <input type="text" class="message-input" placeholder="Enter message" value="This is NovaGram Pro Multi-Account DM Manager! 🚀">
                                <button class="remove-message-btn" onclick="removeMessage(this)">-</button>
                            </div>
                            <div class="message-input-container">
                                <input type="text" class="message-input" placeholder="Enter message" value="Hope you're having an amazing day! ✨">
                                <button class="remove-message-btn" onclick="removeMessage(this)">-</button>
                            </div>
                        </div>
                        <button class="add-message-btn" onclick="addMessage()">+</button>
                    </div>
                    
                    <div class="checkbox-group">
                        <input type="checkbox" id="opt_in_confirm">
                        <label for="opt_in_confirm">✅ I confirm recipients have opted in to receive messages</label>
                    </div>
                    
                    <div class="progress-container">
                        <div class="progress-header">
                            <span>📊 Progress</span>
                            <span id="progress_text">0%</span>
                        </div>
                        <div class="progress-bar">
                            <div class="progress-fill" id="progress_fill" style="width: 0%"></div>
                        </div>
                    </div>
                    
                    <div class="action-buttons">
                        <button class="btn btn-success" onclick="startSending()" id="start_btn">
                            <span>🚀</span> Start Multi-Account Sending
                        </button>
                        <button class="btn btn-danger" onclick="stopSending()" id="stop_btn">
                            <span>⏹️</span> Stop All Workers
                        </button>
                    </div>
                    
                    <div class="logs-panel" id="logs_container">
                        <!-- Logs will be loaded here -->
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- OTP Verification Modal -->
    <div id="otp_modal" class="modal-overlay hidden">
        <div class="otp-modal">
            <div class="otp-icon">🔐</div>
            <div class="otp-title">Security Check Required</div>
            <div class="otp-subtitle">
                For your security, Instagram needs to verify it's you. 
                Please enter the 6-digit code sent to your email or phone.
            </div>
            
            <div class="security-notice">
                <strong>🔒 Security Notice</strong>
                This verification helps keep your account secure. The code will expire shortly.
            </div>
            
            <div class="otp-input-group">
                <input type="text" id="otp_code" class="otp-input" placeholder="Enter 6-digit code" maxlength="6" 
                       pattern="[0-9]{6}" inputmode="numeric" autocomplete="one-time-code">
            </div>
            
            <div class="otp-actions">
                <button class="otp-btn otp-btn-secondary" onclick="cancelVerification()">
                    Cancel
                </button>
                <button class="otp-btn otp-btn-primary" onclick="submitVerification()">
                    Verify & Continue
                </button>
            </div>
        </div>
    </div>

    <script>
        let currentAccount = null;
        let selectedChats = new Set();
        let selectedAccounts = new Set();
        let showingLogin = false;
        let pendingUsername = null;
        
        // Initialize speed slider
        const speedSlider = document.getElementById('speed_slider');
        const speedValue = document.getElementById('speed_value');
        speedSlider.addEventListener('input', function() {
            speedValue.textContent = this.value + '/s';
        });

        // Initialize max messages input
        const maxMessagesInput = document.getElementById('max_messages_input');
        maxMessagesInput.addEventListener('input', function() {
            document.getElementById('max_messages_display').textContent = this.value;
        });

        // OTP input formatting
        const otpInput = document.getElementById('otp_code');
        otpInput.addEventListener('input', function(e) {
            // Only allow numbers
            this.value = this.value.replace(/[^0-9]/g, '');
            
            // Auto-submit when 6 digits entered
            if (this.value.length === 6) {
                submitVerification();
            }
        });
        
        function updateUI(state) {
            // Update status
            document.getElementById('status_text').textContent = state.status;
            document.getElementById('status_dot').className = 'status-dot status-' + state.status;
            document.getElementById('active_workers').textContent = `• ${state.active_workers || 0} Workers`;
            
            // Update stats
            document.getElementById('sent_count').textContent = state.stats.sent;
            document.getElementById('failed_count').textContent = state.stats.failed;
            document.getElementById('rate_display').textContent = state.stats.rate;
            document.getElementById('max_messages_display').textContent = state.stats.max_messages || 100;
            
            // Update progress
            const maxMessages = state.stats.max_messages || 100;
            const progress = Math.min(100, (state.stats.sent / maxMessages) * 100);
            document.getElementById('progress_fill').style.width = progress + '%';
            document.getElementById('progress_text').textContent = Math.round(progress) + '%';
            
            // Update accounts
            updateAccountsList(state.accounts || []);
            
            // Update chats
            updateChatsList(state.threads || []);
            
            // Update multi-account info
            updateMultiAccountInfo();
            
            // Update logs
            const logsContainer = document.getElementById('logs_container');
            logsContainer.innerHTML = state.logs.map(log => {
                const parts = log.split(']');
                const time = parts[0] + ']';
                const message = parts.slice(1).join(']');
                return `<div class="log-entry"><span class="log-time">${time}</span>${message}</div>`;
            }).join('');
            logsContainer.scrollTop = logsContainer.scrollHeight;
            
            // Show/hide sections
            if (state.accounts && state.accounts.length > 0 && !showingLogin) {
                document.getElementById('login_section').classList.add('hidden');
                document.getElementById('control_section').classList.remove('hidden');
                document.getElementById('refresh_chats_btn').classList.remove('hidden');
            } else {
                document.getElementById('login_section').classList.remove('hidden');
                document.getElementById('control_section').classList.add('hidden');
                document.getElementById('refresh_chats_btn').classList.add('hidden');
            }
        }
        
        function updateAccountsList(accounts) {
            const container = document.getElementById('accounts_list');
            container.innerHTML = '';
            
            accounts.forEach(account => {
                const div = document.createElement('div');
                div.className = `account-card ${selectedAccounts.has(account.username) ? 'active' : ''}`;
                div.onclick = () => toggleAccountSelection(account.username);
                
                div.innerHTML = `
                    <div class="account-avatar">${account.username.charAt(0).toUpperCase()}</div>
                    <div class="account-info">
                        <div class="account-name">${account.username}</div>
                        <div class="account-status">
                            <div class="status-indicator status-${account.status}"></div>
                            ${account.status} ${account.is_active ? '• 🟢 Active' : '🔴 Inactive'}
                        </div>
                    </div>
                    <div class="account-select"></div>
                `;
                
                container.appendChild(div);
            });
        }
        
        function updateChatsList(threads) {
            const container = document.getElementById('chats_list');
            container.innerHTML = '';
            
            threads.forEach(thread => {
                const div = document.createElement('div');
                div.className = `chat-item ${selectedChats.has(thread.id) ? 'selected' : ''}`;
                div.onclick = () => toggleChat(thread.id);
                
                div.innerHTML = `
                    <div class="chat-avatar">${thread.name.charAt(0).toUpperCase()}</div>
                    <div class="chat-name">${thread.name}</div>
                `;
                
                container.appendChild(div);
            });
        }
        
        function updateMultiAccountInfo() {
            const infoDiv = document.getElementById('multi_account_info');
            const countSpan = document.getElementById('active_accounts_count');
            
            if (selectedAccounts.size > 0) {
                infoDiv.style.display = 'block';
                countSpan.textContent = selectedAccounts.size;
            } else {
                infoDiv.style.display = 'none';
            }
        }
        
        function toggleAccountSelection(username) {
            if (selectedAccounts.has(username)) {
                selectedAccounts.delete(username);
                // Note: We can't call account_manager directly from frontend
                // This will be handled in backend when sending starts
            } else {
                selectedAccounts.add(username);
            }
            updateMultiAccountInfo();
            fetchState();
        }
        
        function toggleChat(chatId) {
            if (selectedChats.has(chatId)) {
                selectedChats.delete(chatId);
            } else {
                selectedChats.add(chatId);
            }
            fetchState();
        }

        function switchAccount(username) {
            currentAccount = username;
            fetch('/switch_account', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({username: username})
            }).then(() => {
                loadChats();
                fetchState();
            });
        }
        
        function showLogin() {
            document.getElementById('username').value = '';
            document.getElementById('password').value = '';
            document.getElementById('login_status').style.display = 'none';
            document.getElementById('login_section').classList.remove('hidden');
            document.getElementById('control_section').classList.add('hidden');
            showingLogin = true;
        }
        
        function loginAccount() {
            const username = document.getElementById('username').value;
            const password = document.getElementById('password').value;
            
            if (!username || !password) {
                showLoginStatus('Please enter username and password', 'error');
                return;
            }
            
            const btn = document.getElementById('login_btn');
            btn.disabled = true;
            btn.innerHTML = '<span>⏳</span> Logging in...';
            
            fetch('/login', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    username: username,
                    password: password
                })
            })
            .then(r => r.json())
            .then(result => {
                console.log('Login response:', result); // Debug log
                if (result.ok) {
                    if (result.requires_verification) {
                        // Show OTP modal
                        pendingUsername = username;
                        showOtpModal();
                        showLoginStatus('🔐 Security verification required', 'warning');
                    } else {
                        currentAccount = username;
                        showingLogin = false;
                        showLoginStatus('✅ Login successful!', 'success');
                        setTimeout(() => {
                            fetchState();
                        }, 1500);
                    }
                } else {
                    showLoginStatus('❌ ' + result.message, 'error');
                }
            })
            .catch(err => {
                console.error('Login error:', err);
                showLoginStatus('❌ Network error: ' + err.message, 'error');
            })
            .finally(() => {
                btn.disabled = false;
                btn.innerHTML = '<span>🚀</span> Login to Instagram';
            });
        }
        
        function showOtpModal() {
            const modal = document.getElementById('otp_modal');
            modal.classList.remove('hidden');
            document.getElementById('otp_code').value = '';
            document.getElementById('otp_code').focus();
        }
        
        function hideOtpModal() {
            const modal = document.getElementById('otp_modal');
            modal.classList.add('hidden');
        }
        
        function submitVerification() {
            const verificationCode = document.getElementById('otp_code').value;
            
            if (!verificationCode || verificationCode.length !== 6) {
                alert('Please enter a valid 6-digit code');
                return;
            }
            
            if (!pendingUsername) {
                alert('No pending verification found');
                return;
            }
            
            fetch('/verify_otp', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    username: pendingUsername,
                    verification_code: verificationCode
                })
            })
            .then(r => r.json())
            .then(result => {
                console.log('OTP verification response:', result); // Debug log
                if (result.ok) {
                    hideOtpModal();
                    currentAccount = pendingUsername;
                    showingLogin = false;
                    showLoginStatus('✅ Verification successful! Account added.', 'success');
                    pendingUsername = null;
                    setTimeout(() => {
                        fetchState();
                    }, 1500);
                } else {
                    showLoginStatus('❌ ' + result.message, 'error');
                    document.getElementById('otp_code').value = '';
                    document.getElementById('otp_code').focus();
                }
            })
            .catch(err => {
                console.error('OTP verification error:', err);
                showLoginStatus('❌ Network error: ' + err.message, 'error');
            });
        }
        
        function cancelVerification() {
            hideOtpModal();
            pendingUsername = null;
            showLoginStatus('Verification cancelled', 'warning');
        }
        
        function showLoginStatus(message, type) {
            const statusDiv = document.getElementById('login_status');
            statusDiv.textContent = message;
            statusDiv.style.display = 'block';
            statusDiv.style.background = type === 'error' ? '#ff6b6b' : 
                                       type === 'warning' ? '#ffd43b' : '#51cf66';
            statusDiv.style.color = type === 'warning' ? '#333' : 'white';
        }
        
        function loadChats() {
            if (!currentAccount && selectedAccounts.size === 0) {
                alert('Please select at least one account');
                return;
            }
            
            const accountToUse = currentAccount || Array.from(selectedAccounts)[0];
            
            fetch('/load_chats', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({username: accountToUse})
            })
            .then(r => r.json())
            .then(result => {
                if (!result.ok) {
                    alert('Error: ' + result.message);
                }
                fetchState();
            });
        }
        
        function startSending() {
            if (selectedChats.size === 0) {
                alert('Please select at least one conversation');
                return;
            }
            
            if (selectedAccounts.size === 0) {
                alert('Please select at least one account');
                return;
            }
            
            // Collect messages
            const messageInputs = document.querySelectorAll('.message-input');
            const messagesArray = [];
            messageInputs.forEach(input => {
                if (input.value.trim()) {
                    messagesArray.push(input.value.trim());
                }
            });
            if (messagesArray.length === 0) {
                alert('Please enter messages');
                return;
            }
            
            if (!document.getElementById('opt_in_confirm').checked) {
                alert('Please confirm opt-in');
                return;
            }
            
            const payload = {
                accounts: Array.from(selectedAccounts),
                thread_ids: Array.from(selectedChats),
                messages: messagesArray,
                messages_per_second: parseInt(speedSlider.value),
                max_per_run: parseInt(maxMessagesInput.value) || 100
            };
            
            fetch('/start_multi', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            })
            .then(r => r.json())
            .then(result => {
                if (result.ok) {
                    alert('🚀 Multi-account sending started with ' + selectedAccounts.size + ' accounts!');
                } else {
                    alert('Error: ' + result.message);
                }
                fetchState();
            })
            .catch(err => {
                console.error('Start sending error:', err);
                alert('Failed to start sending messages.');
            });
        }
        
        function stopSending() {
            fetch('/stop', {method: 'POST'})
            .then(r => r.json())
            .then(result => {
                alert(result.message);
                fetchState();
            })
            .catch(err => {
                console.error('Stop sending error:', err);
                alert('Failed to stop sending messages.');
            });
        }
        
        function fetchState() {
            fetch('/state')
            .then(r => r.json())
            .then(state => {
                updateUI(state);
            })
            .catch(err => {
                console.error('Fetch state error:', err);
            });
        }
        
        function addMessage() {
            const container = document.getElementById('message_inputs');
            const addBtn = container.querySelector('.add-message-btn');

            const newContainer = document.createElement('div');
            newContainer.className = 'message-input-container';

            newContainer.innerHTML = `
                <input type="text" class="message-input" placeholder="Enter message">
                <button class="remove-message-btn" onclick="removeMessage(this)">-</button>
            `;

            container.insertBefore(newContainer, addBtn);
        }

        function removeMessage(btn) {
            const containers = document.querySelectorAll('.message-input-container');
            if (containers.length > 1) {
                btn.closest('.message-input-container').remove();
            }
        }

        // Auto-refresh
        setInterval(fetchState, 2000);
        fetchState();
    </script>
</body>
</html>'''

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

# ---------- Routes ----------
@app.route("/")
def index():
    return render_template_string(TEMPLATE)

@app.route("/state")
def get_state():
    STATE["accounts"] = account_manager.get_accounts_list()
    return jsonify(STATE)

@app.route("/login", methods=["POST"])
def login():
    payload = request.get_json(force=True)
    username = payload.get("username")
    password = payload.get("password")
    verification_code = payload.get("verification_code")
    
    if not username or not password:
        return jsonify({"ok": False, "message": "Username and password required"})
    
    success, error_type = account_manager.login_account(username, password, verification_code)
    
    if success:
        log(f"✅ Account added: {username}")
        return jsonify({"ok": True, "message": "Login successful"})
    else:
        if error_type == "verification_required":
            return jsonify({"ok": False, "requires_verification": True, "message": "Verification code required"})
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
        STATE["current_account"] = username
        STATE["threads"] = []
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
    if STATE["running"]:
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

    STATE["stats"] = {
        "sent": 0,
        "failed": 0,
        "rate": 0,
        "max_messages": max_per_run
    }

    WORKER["threads"] = []
    WORKER["stop_flag"] = False
    STATE["running"] = True
    STATE["status"] = "running"

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
    STATE["running"] = False
    STATE["status"] = "idle"
    STATE["active_workers"] = 0
    
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
    app.run(host='0.0.0.0', port=5000, debug=False)
