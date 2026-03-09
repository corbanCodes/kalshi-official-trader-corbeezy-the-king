#!/usr/bin/env python3
"""
15-Minute Trading Strategy - Main Entry Point

Runs trading bot with web dashboard on PORT (default 8080)
"""

import sys
import os
import json
import threading
import time
import base64
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from urllib.parse import parse_qs

# Dashboard authentication
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS", "")

# Add src to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src import (
    Trader,
    load_config,
    KalshiClient,
    MartingaleCalculator,
    MarketScanner,
)

# Global trader reference for API access
GLOBAL_TRADER = None

# Global state for web dashboard
DASHBOARD_STATE = {
    "status": "stopped",
    "trading_enabled": False,
    "bankroll": 0,
    "starting_bankroll": None,  # User-set starting amount
    "apportioned_bankroll": None,  # Legacy - use starting_bankroll
    "effective_bankroll": 0,  # Current bankroll for calculations (grows with wins)
    "auto_compound": True,  # Auto-increase bankroll after wins
    "today_profit": 0,
    "total_trades": 0,
    "wins": 0,
    "losses": 0,
    "consecutive_losses": 0,
    "in_recovery": False,
    "recovery_target": 0,
    "recovery_stage": 0,  # 0 = not in recovery, 1-2 = recovery stage
    "last_trade": None,
    "last_update": None,
    "recent_trades": [],
    "error": None,
    "activity_log": [],
    "current_market": None,
    "market_prices": {},
    "pending_trade": None,  # Shows trade waiting for settlement
}

def log_activity(msg):
    """Add message to activity log."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    DASHBOARD_STATE["activity_log"].append(f"{timestamp} {msg}")
    DASHBOARD_STATE["activity_log"] = DASHBOARD_STATE["activity_log"][-20:]  # Keep last 20
    print(f"{timestamp} {msg}")


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP handler for the monitoring dashboard."""

    def log_message(self, format, *args):
        pass  # Suppress logging

    def check_auth(self):
        """Check if request is authenticated. Returns True if OK, False if denied."""
        if not DASHBOARD_PASS:
            return True  # No password set, allow access

        auth_header = self.headers.get("Authorization", "")
        if not auth_header.startswith("Basic "):
            return False

        try:
            encoded = auth_header[6:]
            decoded = base64.b64decode(encoded).decode("utf-8")
            username, password = decoded.split(":", 1)
            return password == DASHBOARD_PASS
        except:
            return False

    def send_auth_required(self):
        """Send 401 auth required response."""
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Dashboard"')
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<h1>Authentication Required</h1>")

    def do_GET(self):
        # Health check doesn't need auth
        if self.path == "/health":
            self.send_json({"status": "ok"})
            return

        # Check auth for everything else
        if not self.check_auth():
            self.send_auth_required()
            return

        if self.path == "/" or self.path == "/dashboard":
            self.send_dashboard()
        elif self.path == "/api/status":
            self.send_json(DASHBOARD_STATE)
        elif self.path == "/api/logs":
            self.send_logs_download()
        elif self.path == "/strategy":
            self.send_strategy_explanation()
        else:
            self.send_error(404)

    def send_logs_download(self):
        """Send trade logs as downloadable JSON."""
        import glob
        logs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

        all_logs = {
            "activity_log": DASHBOARD_STATE.get("activity_log", []),
            "recent_trades": DASHBOARD_STATE.get("recent_trades", []),
            "dashboard_state": {
                "bankroll": DASHBOARD_STATE.get("bankroll"),
                "consecutive_losses": DASHBOARD_STATE.get("consecutive_losses"),
                "total_trades": DASHBOARD_STATE.get("total_trades"),
                "wins": DASHBOARD_STATE.get("wins"),
                "losses": DASHBOARD_STATE.get("losses"),
            },
        }

        # Try to load trade history
        trade_history_file = os.path.join(logs_dir, "trade_history.json")
        if os.path.exists(trade_history_file):
            try:
                with open(trade_history_file) as f:
                    all_logs["trade_history"] = json.load(f)
            except:
                pass

        # Try to load martingale state
        martingale_file = os.path.join(logs_dir, "martingale_state.json")
        if os.path.exists(martingale_file):
            try:
                with open(martingale_file) as f:
                    all_logs["martingale_state"] = json.load(f)
            except:
                pass

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Disposition", f"attachment; filename=trading_logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        self.end_headers()
        self.wfile.write(json.dumps(all_logs, indent=2).encode())

    def send_strategy_explanation(self):
        """Send strategy explanation page."""
        html = """<!DOCTYPE html>
<html>
<head>
    <title>Strategy Explanation</title>
    <style>
        body { font-family: monospace; background: #0a0a0a; color: #e0e0e0; padding: 40px; max-width: 800px; margin: 0 auto; }
        h1, h2, h3 { color: #6bcb77; }
        h1 { border-bottom: 1px solid #333; padding-bottom: 10px; }
        .section { background: #1a1a1a; border: 1px solid #333; border-radius: 8px; padding: 20px; margin: 20px 0; }
        code { background: #2a2a2a; padding: 2px 6px; border-radius: 4px; color: #ffd93d; }
        .warning { background: rgba(255,107,107,0.1); border-left: 3px solid #ff6b6b; padding: 10px; margin: 10px 0; }
        a { color: #6bcb77; }
    </style>
</head>
<body>
    <h1>15-Minute Strategy - How It Works</h1>

    <div class="section">
        <h2>The Basic Idea</h2>
        <p>We bet on 15-minute BTC price windows when the odds are heavily in our favor
        (80-92% implied probability) with only 5 minutes remaining.</p>
    </div>

    <div class="section">
        <h2>Entry Criteria (Base Strategy)</h2>
        <ol>
            <li>Wait 10 minutes into the 15-minute window (5 min remaining)</li>
            <li>Look for YES or NO priced at <code>80-92 cents</code></li>
            <li>Place a limit order 1 cent above ask</li>
        </ol>
    </div>

    <div class="section">
        <h2>The Recovery System (Altered Martingale)</h2>
        <p>If we lose, we bet enough to recover <strong>just the loss</strong> (not extra profit).</p>
        <ul>
            <li><strong>Max 2 recovery attempts</strong> (3 bets total)</li>
            <li><strong>Recovery entries capped at 85 cents</strong> (more conservative)</li>
            <li><strong>Distance filter:</strong> BTC must be 0.15% away from strike in our direction</li>
            <li>If both recovery attempts fail, we stop (accept the loss)</li>
        </ul>
        <h3>Recovery Formula</h3>
        <p><code>contracts_needed = total_loss / (1 - contract_price)</code></p>
        <p>Example at 85c: $0.85 loss / 0.15 = 6 contracts needed</p>
    </div>

    <div class="section">
        <h2>The Bet (Core Thesis)</h2>
        <p style="font-size: 1.2rem; color: #6bcb77;">
            <strong>It will NEVER lose 3 times in a row.</strong>
        </p>
        <p>From backtest data (150+ hours):</p>
        <ul>
            <li>1 consecutive loss: Common (expected)</li>
            <li>2 consecutive losses: Rare (seen 3 times)</li>
            <li>3 consecutive losses: <strong>ZERO times</strong></li>
        </ul>
    </div>

    <div class="section">
        <h2>Risk Management</h2>
        <ul>
            <li>Base bet sized so bankroll survives 2 consecutive losses</li>
            <li>~47x multiplier means <code>$47 bankroll per $1 base bet</code></li>
            <li>Recovery bets only taken when BTC distance filter passes</li>
        </ul>
    </div>

    <div class="warning">
        <strong>WARNING:</strong> This is a gambling strategy. Even with 90%+ win rate,
        3 consecutive losses WILL eventually happen. Only use money you can afford to lose completely.
    </div>

    <p><a href="/">Back to Dashboard</a></p>
</body>
</html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())

    def do_POST(self):
        if not self.check_auth():
            self.send_auth_required()
            return
        if self.path == "/api/start":
            DASHBOARD_STATE["trading_enabled"] = True
            DASHBOARD_STATE["status"] = "running"
            DASHBOARD_STATE["error"] = None
            self.send_json({"success": True, "trading": True})
        elif self.path == "/api/stop":
            DASHBOARD_STATE["trading_enabled"] = False
            DASHBOARD_STATE["status"] = "stopped"
            self.send_json({"success": True, "trading": False})
        elif self.path == "/api/set-apportioned":
            self.handle_set_apportioned()
        elif self.path == "/api/reset-recovery":
            self.handle_reset_recovery()
        else:
            self.send_error(404)

    def handle_set_apportioned(self):
        """Handle setting the apportioned bankroll with auto-compound."""
        import math

        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body) if body else {}

            amount = data.get('amount')
            auto_compound = data.get('auto_compound', True)

            # Save to dashboard state
            DASHBOARD_STATE["starting_bankroll"] = amount
            DASHBOARD_STATE["auto_compound"] = auto_compound

            # When setting starting bankroll, ALWAYS use that amount (reset effective)
            if amount:
                DASHBOARD_STATE["effective_bankroll"] = amount

            # Calculate safe contracts based on the STARTING amount (not full balance)
            effective_bankroll = amount if amount else DASHBOARD_STATE.get("bankroll", 0)

            if effective_bankroll <= 0:
                self.send_json({"success": False, "error": "Invalid bankroll amount"})
                return

            # Calculate using loss-only formula at 85c
            price = 0.85
            profit_per_dollar = 0.15

            safe_contracts = 0
            max_risk = 0

            for contracts in range(100, 0, -1):
                base_cost = contracts * price
                cum = base_cost

                r1 = math.ceil(cum / profit_per_dollar)
                cum += r1 * price

                r2 = math.ceil(cum / profit_per_dollar)
                cum += r2 * price

                if cum <= effective_bankroll:
                    safe_contracts = contracts
                    max_risk = cum
                    break

            # Profit per win (rough estimate with fees)
            profit_per_win = safe_contracts * 0.15 * 0.93  # ~7% fee reduction

            self.send_json({
                "success": True,
                "starting_bankroll": amount,
                "effective_bankroll": effective_bankroll,
                "auto_compound": auto_compound,
                "safe_contracts": safe_contracts,
                "max_risk": max_risk,
                "profit_per_win": profit_per_win,
            })

        except Exception as e:
            self.send_json({"success": False, "error": str(e)})

    def handle_reset_recovery(self):
        """Handle resetting the martingale recovery state."""
        global GLOBAL_TRADER
        try:
            if GLOBAL_TRADER:
                GLOBAL_TRADER.reset_recovery_mode()
                DASHBOARD_STATE["consecutive_losses"] = 0
                DASHBOARD_STATE["in_recovery"] = False
                DASHBOARD_STATE["recovery_target"] = 0
                DASHBOARD_STATE["recovery_stage"] = 0
                log_activity("Recovery state reset manually")
                self.send_json({"success": True, "message": "Recovery state reset"})
            else:
                self.send_json({"success": False, "error": "Trader not initialized"})
        except Exception as e:
            self.send_json({"success": False, "error": str(e)})

    def send_json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def send_dashboard(self):
        html = """<!DOCTYPE html>
<html>
<head>
    <title>Trading Bot Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: monospace; background: #0a0a0a; color: #e0e0e0; padding: 40px; }
        h1 { color: #6bcb77; margin-bottom: 20px; }
        .controls { margin-bottom: 20px; display: flex; gap: 10px; align-items: center; }
        .btn { padding: 12px 30px; border: none; border-radius: 8px; font-size: 1rem; cursor: pointer; font-family: monospace; font-weight: bold; transition: all 0.2s; }
        .btn-start { background: #6bcb77; color: #000; }
        .btn-start:hover { background: #5ab868; }
        .btn-stop { background: #ff6b6b; color: #fff; }
        .btn-stop:hover { background: #e55555; }
        .btn:disabled { opacity: 0.5; cursor: not-allowed; }
        .grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 20px; margin: 20px 0; }
        .card { background: #1a1a1a; border: 1px solid #333; border-radius: 10px; padding: 20px; text-align: center; transition: all 0.3s; }
        .card .value { font-size: 2rem; font-weight: bold; color: #6bcb77; transition: all 0.3s; }
        .card .label { color: #888; margin-top: 5px; }
        .card.warn .value { color: #ffd93d; }
        .card.danger .value { color: #ff6b6b; }
        .status { padding: 10px 20px; border-radius: 20px; display: inline-block; transition: all 0.3s; }
        .status.running { background: rgba(107,203,119,0.2); color: #6bcb77; }
        .status.stopped { background: rgba(136,136,136,0.2); color: #888; }
        .status.paused { background: rgba(255,217,61,0.2); color: #ffd93d; }
        .status.error { background: rgba(255,107,107,0.2); color: #ff6b6b; }
        .trades { background: #1a1a1a; border-radius: 10px; padding: 20px; margin-top: 20px; }
        .trade { padding: 10px; border-bottom: 1px solid #333; display: flex; justify-content: space-between; }
        .trade.win { border-left: 3px solid #6bcb77; }
        .trade.loss { border-left: 3px solid #ff6b6b; }
        .updated { color: #666; margin-top: 20px; font-size: 0.8rem; }
        .pulse { animation: pulse 0.5s ease-in-out; }
        @keyframes pulse { 0% { transform: scale(1); } 50% { transform: scale(1.05); } 100% { transform: scale(1); } }
        .market-row { padding: 8px 0; border-bottom: 1px solid #333; display: grid; grid-template-columns: 2fr 0.5fr 1fr 1fr; gap: 10px; align-items: center; }
        .market-row .ticker { color: #6bcb77; font-weight: bold; }
        .market-row .yes { color: #4ecdc4; }
        .market-row .no { color: #ff6b6b; }
        .live-indicator { display: inline-block; width: 8px; height: 8px; background: #6bcb77; border-radius: 50%; margin-right: 8px; animation: blink 1s infinite; }
        @keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
    </style>
</head>
<body>
    <h1><span class="live-indicator"></span>15-MINUTE STRATEGY BOT</h1>

    <div class="controls">
        <button class="btn btn-start" id="startBtn" onclick="startTrading()">START TRADING</button>
        <button class="btn btn-stop" id="stopBtn" onclick="stopTrading()" disabled>STOP</button>
        <span class="status stopped" id="statusBadge">STOPPED</span>
        <a href="/api/logs" class="btn" style="background:#444;color:#fff;text-decoration:none;margin-left:20px;">Download Logs</a>
        <a href="/strategy" class="btn" style="background:#333;color:#6bcb77;text-decoration:none;border:1px solid #6bcb77;" target="_blank">Strategy Info</a>
    </div>

    <div class="grid">
        <div class="card" id="bankrollCard">
            <div class="value" id="bankroll">$0.00</div>
            <div class="label">Bankroll</div>
        </div>
        <div class="card" id="profitCard">
            <div class="value" id="profit">$+0.00</div>
            <div class="label">Today P&L</div>
        </div>
        <div class="card">
            <div class="value" id="winloss">0/0</div>
            <div class="label">W/L (<span id="winrate">0.0</span>%)</div>
        </div>
        <div class="card" id="consecCard">
            <div class="value" id="consec">0</div>
            <div class="label">Consecutive Losses</div>
        </div>
    </div>

    <div id="recoveryBanner" style="display:none; background:rgba(255,107,107,0.15); border:1px solid #ff6b6b; border-radius:8px; padding:15px; margin-bottom:20px;">
        <strong style="color:#ff6b6b;">RECOVERY MODE ACTIVE</strong>
        <span id="recoveryInfo" style="margin-left:20px;">Stage 1 - Need to recover $0.00</span>
        <button onclick="resetRecovery()" class="btn" style="background:#ff6b6b; color:#fff; margin-left:20px; padding:8px 15px; font-size:0.85rem;">Reset Recovery</button>
    </div>

    <div class="trades" style="margin-bottom:20px;">
        <h3 style="margin-bottom:15px; color:#888;">Bankroll Management</h3>
        <div style="display:grid; grid-template-columns: 1fr 1fr 1fr; gap:20px; align-items:end;">
            <div>
                <label style="color:#888; font-size:0.85rem;">Starting Bankroll ($)</label>
                <input type="number" id="apportionedInput" placeholder="Use full balance"
                    style="width:100%; padding:10px; background:#0a0a0a; border:1px solid #333; border-radius:5px; color:#e0e0e0; font-family:monospace; margin-top:5px;">
                <div style="margin-top:8px;">
                    <label style="cursor:pointer;">
                        <input type="checkbox" id="autoCompoundCheck" checked style="margin-right:8px;">
                        <span style="color:#6bcb77; font-size:0.85rem;">Auto-compound wins</span>
                    </label>
                </div>
            </div>
            <div>
                <button onclick="saveApportioned()" class="btn" style="background:#6bcb77; color:#000; width:100%;">Save & Calculate</button>
            </div>
            <div id="safetyInfo" style="padding:10px; background:#1a1a1a; border-radius:5px; font-size:0.9rem;">
                <div>Safe contracts: <span id="safeContracts" style="color:#6bcb77;">--</span></div>
                <div>Max risk: <span id="maxRisk" style="color:#ffd93d;">--</span></div>
                <div>Profit/win: <span id="profitPerWin" style="color:#6bcb77;">--</span></div>
            </div>
        </div>
        <div id="compoundStatus" style="margin-top:10px; padding:10px; background:#1a2a1a; border-radius:5px; display:none;">
            <span style="color:#6bcb77;">Starting: $<span id="startingBankroll">0</span></span>
            <span style="margin-left:20px;">Current: $<span id="currentBankroll" style="color:#ffd93d;">0</span></span>
            <span style="margin-left:20px;">Growth: <span id="growthPct" style="color:#6bcb77;">+0%</span></span>
        </div>
        <div id="apportionedStatus" style="margin-top:10px; font-size:0.85rem; color:#888;"></div>
    </div>

    <div id="pendingTradeBanner" style="display:none; background:rgba(255,217,61,0.15); border:1px solid #ffd93d; border-radius:8px; padding:15px; margin-bottom:20px;">
        <strong style="color:#ffd93d;">⏳ PENDING TRADE</strong>
        <span id="pendingTradeInfo" style="margin-left:20px;">Waiting for settlement...</span>
    </div>

    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-top: 20px;">
        <div class="trades">
            <h3 style="margin-bottom: 15px; color: #888;">Recent Trades</h3>
            <div id="tradesContainer"><div class="trade">No trades yet</div></div>
        </div>
        <div class="trades">
            <h3 style="margin-bottom: 15px; color: #888;">Activity Log</h3>
            <div id="activityLog" style="font-size: 0.85rem; max-height: 200px; overflow-y: auto;">Loading...</div>
        </div>
    </div>

    <div class="trades" style="margin-top: 20px;">
        <h3 style="margin-bottom: 15px; color: #888;">Market Prices (Live)</h3>
        <div id="marketPrices" style="font-size: 0.9rem;">Loading markets...</div>
    </div>

    <p class="updated">Last updated: <span id="lastUpdate">never</span> | Dashboard: 500ms | Scanner: 300ms</p>

    <script>
        let lastBankroll = 0;
        let lastProfit = 0;

        function startTrading() {
            document.getElementById('startBtn').disabled = true;
            fetch('/api/start', {method: 'POST'})
                .then(r => r.json())
                .then(d => {
                    if(d.success) {
                        document.getElementById('startBtn').disabled = true;
                        document.getElementById('stopBtn').disabled = false;
                        updateStatus();
                    }
                });
        }

        function stopTrading() {
            document.getElementById('stopBtn').disabled = true;
            fetch('/api/stop', {method: 'POST'})
                .then(r => r.json())
                .then(d => {
                    if(d.success) {
                        document.getElementById('startBtn').disabled = false;
                        document.getElementById('stopBtn').disabled = true;
                        updateStatus();
                    }
                });
        }

        function resetRecovery() {
            if (!confirm('Reset recovery state? This clears consecutive losses and exits recovery mode.')) {
                return;
            }
            fetch('/api/reset-recovery', {method: 'POST'})
                .then(r => r.json())
                .then(d => {
                    if (d.success) {
                        document.getElementById('recoveryBanner').style.display = 'none';
                        updateStatus();
                        alert('Recovery state reset successfully!');
                    } else {
                        alert('Error: ' + d.error);
                    }
                })
                .catch(err => alert('Error: ' + err));
        }

        function updateStatus() {
            fetch('/api/status')
                .then(r => r.json())
                .then(data => {
                    // Update bankroll with pulse animation if changed
                    const bankroll = data.bankroll || 0;
                    const bankrollEl = document.getElementById('bankroll');
                    if (bankroll !== lastBankroll) {
                        bankrollEl.classList.add('pulse');
                        setTimeout(() => bankrollEl.classList.remove('pulse'), 500);
                        lastBankroll = bankroll;
                    }
                    bankrollEl.textContent = '$' + bankroll.toFixed(2);

                    // Update profit
                    const profit = data.today_profit || 0;
                    const profitEl = document.getElementById('profit');
                    profitEl.textContent = '$' + (profit >= 0 ? '+' : '') + profit.toFixed(2);
                    profitEl.style.color = profit >= 0 ? '#6bcb77' : '#ff6b6b';
                    if (profit !== lastProfit) {
                        profitEl.classList.add('pulse');
                        setTimeout(() => profitEl.classList.remove('pulse'), 500);
                        lastProfit = profit;
                    }

                    // Win/Loss
                    const wins = data.wins || 0;
                    const losses = data.losses || 0;
                    document.getElementById('winloss').textContent = wins + '/' + losses;
                    const winrate = (wins + losses) > 0 ? (wins / (wins + losses) * 100) : 0;
                    document.getElementById('winrate').textContent = winrate.toFixed(1);

                    // Consecutive losses
                    const consec = data.consecutive_losses || 0;
                    document.getElementById('consec').textContent = consec;
                    const consecCard = document.getElementById('consecCard');
                    consecCard.className = 'card' + (consec >= 2 ? ' danger' : (consec === 1 ? ' warn' : ''));

                    // Recovery mode banner
                    const inRecovery = data.in_recovery || false;
                    const recoveryBanner = document.getElementById('recoveryBanner');
                    if (inRecovery) {
                        recoveryBanner.style.display = 'block';
                        const stage = data.recovery_stage || 1;
                        const target = (data.recovery_target || 0).toFixed(2);
                        document.getElementById('recoveryInfo').textContent =
                            'Stage ' + stage + ' of 2 - Need to recover $' + target +
                            ' | Using 85c cap + 0.15% distance filter';
                    } else {
                        recoveryBanner.style.display = 'none';
                    }

                    // Update compound status if active
                    const starting = data.starting_bankroll;
                    const effective = data.effective_bankroll;
                    if (starting && effective) {
                        document.getElementById('compoundStatus').style.display = 'block';
                        document.getElementById('startingBankroll').textContent = starting.toFixed(2);
                        document.getElementById('currentBankroll').textContent = effective.toFixed(2);
                        const growth = ((effective - starting) / starting * 100);
                        const growthEl = document.getElementById('growthPct');
                        growthEl.textContent = (growth >= 0 ? '+' : '') + growth.toFixed(1) + '%';
                        growthEl.style.color = growth >= 0 ? '#6bcb77' : '#ff6b6b';

                        // Update safety info with current effective bankroll
                        calculateSafety(effective);
                    }

                    // Status badge
                    const status = data.status || 'stopped';
                    const statusBadge = document.getElementById('statusBadge');
                    statusBadge.className = 'status ' + status;
                    let statusText = status.toUpperCase();
                    if (data.error) statusText += ': ' + data.error;
                    statusBadge.textContent = statusText;

                    // Buttons
                    document.getElementById('startBtn').disabled = data.trading_enabled;
                    document.getElementById('stopBtn').disabled = !data.trading_enabled;

                    // Activity log
                    const logs = data.activity_log || [];
                    document.getElementById('activityLog').innerHTML = logs.slice(-10).join('<br>') || 'No activity yet';

                    // Pending trade banner
                    const pending = data.pending_trade;
                    const pendingBanner = document.getElementById('pendingTradeBanner');
                    if (pending) {
                        pendingBanner.style.display = 'block';
                        document.getElementById('pendingTradeInfo').innerHTML =
                            '<strong>' + pending.side.toUpperCase() + '</strong> ' +
                            pending.contracts + ' contract @ ' + pending.fill_price + 'c | ' +
                            'Cost: $' + pending.cost.toFixed(2) + ' | Waiting for settlement...';
                    } else {
                        pendingBanner.style.display = 'none';
                    }

                    // Recent trades with details
                    const trades = data.recent_trades || [];
                    let tradesHtml = '';
                    trades.slice(-10).reverse().forEach(t => {
                        const cls = (t.profit || 0) > 0 ? 'win' : 'loss';
                        const side = t.side ? t.side.toUpperCase() : '';
                        const slip = t.slippage !== undefined ? (t.slippage > 0 ? '+' + t.slippage : t.slippage) + 'c' : '';
                        const details = t.ticker ?
                            '<div style="font-size:0.75rem;color:#666;margin-top:2px;">' + side + ' @ ' + (t.fill_price || '?') + 'c | slip: ' + slip + '</div>' : '';
                        tradesHtml += '<div class="trade ' + cls + '" style="flex-direction:column;align-items:flex-start;padding:8px 10px;">' +
                            '<div style="display:flex;justify-content:space-between;width:100%;">' +
                            '<span>' + (t.time || '') + '</span>' +
                            '<span style="color:' + (t.profit >= 0 ? '#6bcb77' : '#ff6b6b') + ';font-weight:bold;">$' + (t.profit >= 0 ? '+' : '') + (t.profit || 0).toFixed(2) + '</span></div>' +
                            details + '</div>';
                    });
                    document.getElementById('tradesContainer').innerHTML = tradesHtml || '<div class="trade">No trades yet</div>';

                    // Market prices - 15 minute crypto markets
                    const markets = data.market_prices || {};
                    let marketHtml = '';
                    const entries = Object.entries(markets);

                    // Sort by time remaining
                    entries.sort((a, b) => {
                        const minsA = parseFloat(a[1].mins_remaining) || 999;
                        const minsB = parseFloat(b[1].mins_remaining) || 999;
                        return minsA - minsB;
                    });

                    for (const [ticker, prices] of entries) {
                        const yesBid = prices.yes_bid || 0;
                        const yesAsk = prices.yes_ask || 0;
                        const noBid = prices.no_bid || 0;
                        const noAsk = prices.no_ask || 0;
                        const mins = prices.mins_remaining || 'N/A';

                        // Highlight if in trading range (80-92c for base, 80-85c for recovery)
                        const yesInRange = yesAsk >= 80 && yesAsk <= 92;
                        const noInRange = noAsk >= 80 && noAsk <= 92;
                        const highlight = yesInRange || noInRange ? ' style=\"background:#1a2a1a;\"' : '';

                        marketHtml += '<div class=\"market-row\"' + highlight + '>';
                        marketHtml += '<span class=\"ticker\">' + ticker + '</span>';
                        marketHtml += '<span style=\"color:#ffd93d;\">' + mins + '</span>';
                        marketHtml += '<span class=\"yes\">YES ' + yesBid + '/' + yesAsk + 'c' + (yesInRange ? ' *' : '') + '</span>';
                        marketHtml += '<span class=\"no\">NO ' + noBid + '/' + noAsk + 'c' + (noInRange ? ' *' : '') + '</span>';
                        marketHtml += '</div>';
                    }
                    document.getElementById('marketPrices').innerHTML = marketHtml || 'No 15-min crypto markets found';

                    // Last update
                    document.getElementById('lastUpdate').textContent = data.last_update || 'never';
                })
                .catch(err => {
                    console.error('Update failed:', err);
                });
        }

        function saveApportioned() {
            const value = document.getElementById('apportionedInput').value;
            const amount = value ? parseFloat(value) : null;
            const autoCompound = document.getElementById('autoCompoundCheck').checked;

            fetch('/api/set-apportioned', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({amount: amount, auto_compound: autoCompound})
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    document.getElementById('safeContracts').textContent = data.safe_contracts;
                    document.getElementById('maxRisk').textContent = '$' + data.max_risk.toFixed(2);
                    document.getElementById('profitPerWin').textContent = '$' + data.profit_per_win.toFixed(2);
                    document.getElementById('apportionedStatus').innerHTML =
                        '<span style="color:#6bcb77;">Saved! ' + (autoCompound ? 'Auto-compound ON' : 'Fixed bankroll') + '</span>';

                    // Show compound status
                    if (amount) {
                        document.getElementById('compoundStatus').style.display = 'block';
                        document.getElementById('startingBankroll').textContent = amount.toFixed(2);
                        document.getElementById('currentBankroll').textContent = data.effective_bankroll.toFixed(2);
                        const growth = ((data.effective_bankroll - amount) / amount * 100);
                        document.getElementById('growthPct').textContent = (growth >= 0 ? '+' : '') + growth.toFixed(1) + '%';
                    }
                } else {
                    document.getElementById('apportionedStatus').innerHTML =
                        '<span style="color:#ff6b6b;">Error: ' + data.error + '</span>';
                }
            });
        }

        function loadApportioned() {
            fetch('/api/status')
            .then(r => r.json())
            .then(data => {
                if (data.starting_bankroll) {
                    document.getElementById('apportionedInput').value = data.starting_bankroll;
                }
                if (data.auto_compound !== undefined) {
                    document.getElementById('autoCompoundCheck').checked = data.auto_compound;
                }

                // Show compound status if active
                if (data.starting_bankroll && data.effective_bankroll) {
                    document.getElementById('compoundStatus').style.display = 'block';
                    document.getElementById('startingBankroll').textContent = data.starting_bankroll.toFixed(2);
                    document.getElementById('currentBankroll').textContent = data.effective_bankroll.toFixed(2);
                    const growth = ((data.effective_bankroll - data.starting_bankroll) / data.starting_bankroll * 100);
                    document.getElementById('growthPct').textContent = (growth >= 0 ? '+' : '') + growth.toFixed(1) + '%';
                    calculateSafety(data.effective_bankroll);
                } else {
                    const bankroll = data.bankroll || 0;
                    if (bankroll > 0) {
                        calculateSafety(bankroll);
                    }
                }
            });
        }

        function calculateSafety(bankroll) {
            // Quick client-side calculation for display
            const price = 0.85;
            const profitPerDollar = 0.15;

            for (let contracts = 100; contracts > 0; contracts--) {
                const baseCost = contracts * price;
                let cum = baseCost;

                const r1 = Math.ceil(cum / profitPerDollar);
                cum += r1 * price;

                const r2 = Math.ceil(cum / profitPerDollar);
                cum += r2 * price;

                if (cum <= bankroll) {
                    document.getElementById('safeContracts').textContent = contracts;
                    document.getElementById('maxRisk').textContent = '$' + cum.toFixed(2);
                    document.getElementById('profitPerWin').textContent = '$' + (contracts * 0.15 * 0.93).toFixed(2);
                    break;
                }
            }
        }

        // Update every 500ms via AJAX (no page reload)
        setInterval(updateStatus, 500);

        // Initial load
        updateStatus();
        loadApportioned();
    </script>
</body>
</html>"""

        # HTML is now fully dynamic via AJAX - no server-side templating needed
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())


def run_web_server(port):
    """Run the monitoring web server."""
    server = HTTPServer(("", port), DashboardHandler)
    print(f"Dashboard running on port {port}")
    server.serve_forever()


def update_dashboard(trader):
    """Update dashboard state from trader."""
    # Get recovery state from tracker
    in_recovery = trader.tracker.martingale.in_recovery
    recovery_target = trader.tracker.martingale.get_recovery_target_cents() / 100 if in_recovery else 0
    recovery_stage = trader.tracker.martingale.consecutive_losses if in_recovery else 0

    # Get starting bankroll from dashboard state
    starting = DASHBOARD_STATE.get("starting_bankroll")

    # Calculate effective bankroll for trading
    # If we have a starting bankroll set, use effective_bankroll (which compounds from starting)
    # Otherwise use full Kalshi balance
    if starting:
        # Use effective_bankroll which starts at 'starting' and compounds with wins
        effective_bankroll = DASHBOARD_STATE.get("effective_bankroll") or starting
    else:
        # No starting set = use full Kalshi balance
        effective_bankroll = trader.state.bankroll

    # DON'T overwrite effective_bankroll in the update - it's managed separately
    DASHBOARD_STATE.update({
        "bankroll": trader.state.bankroll,
        "today_profit": trader.state.total_profit,
        "total_trades": trader.state.total_trades,
        "wins": trader.state.total_wins,
        "losses": trader.state.total_losses,
        "consecutive_losses": trader.state.consecutive_losses,
        "in_recovery": in_recovery,
        "recovery_target": recovery_target,
        "recovery_stage": recovery_stage,
        "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })

    # Only set effective_bankroll if not using starting bankroll
    if not starting:
        DASHBOARD_STATE["effective_bankroll"] = effective_bankroll

    # Update trader's effective bankroll for calculations
    trader.effective_bankroll = DASHBOARD_STATE.get("effective_bankroll", trader.state.bankroll)


def cmd_run():
    """Start live trading with web dashboard."""
    port = int(os.environ.get("PORT", 8080))

    print("=" * 60)
    print("  15-MINUTE STRATEGY - LIVE TRADING")
    print(f"  Dashboard: http://localhost:{port}")
    print("=" * 60)

    # Start web server in background
    web_thread = threading.Thread(target=run_web_server, args=(port,), daemon=True)
    web_thread.start()

    # Give server time to start
    time.sleep(1)

    try:
        global GLOBAL_TRADER
        trader = Trader()
        GLOBAL_TRADER = trader
        DASHBOARD_STATE["bankroll"] = trader.state.bankroll
        DASHBOARD_STATE["status"] = "stopped"
        DASHBOARD_STATE["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        print(f"Bankroll: ${trader.state.bankroll:.2f}")
        print("Waiting for START command from dashboard...")

        last_balance_check = 0

        # Main loop
        while True:
            # Refresh balance every second
            if time.time() - last_balance_check > 1:
                trader.refresh_bankroll()
                last_balance_check = time.time()

            update_dashboard(trader)

            # Always scan 15-minute crypto markets (even when stopped)
            try:
                crypto_markets = trader.scanner.get_all_crypto_markets()
                DASHBOARD_STATE["market_prices"] = {}  # Clear old data

                from datetime import timezone
                now = datetime.now(timezone.utc)

                for m in crypto_markets:
                    # Calculate minutes remaining
                    try:
                        close_time = trader.scanner.parse_close_time(m.close_time)
                        mins_remaining = (close_time - now).total_seconds() / 60
                        mins_str = f"{mins_remaining:.1f}m"
                    except:
                        mins_str = "N/A"

                    DASHBOARD_STATE["market_prices"][m.ticker] = {
                        "yes_bid": m.yes_bid,
                        "yes_ask": m.yes_ask,
                        "no_bid": m.no_bid,
                        "no_ask": m.no_ask,
                        "mins_remaining": mins_str,
                    }
                # Don't spam activity log with scan messages - only log occasionally
                pass  # Scans visible in market prices section
            except Exception as e:
                log_activity(f"Scan error: {e}")

            # Check if trading is enabled
            if not DASHBOARD_STATE["trading_enabled"]:
                DASHBOARD_STATE["status"] = "stopped"
                time.sleep(0.5)  # 500ms polling for faster updates
                continue

            # Check if can trade
            if not trader.can_trade():
                DASHBOARD_STATE["status"] = "paused"
                DASHBOARD_STATE["error"] = f"Bankroll too low (${trader.state.bankroll:.2f})"
                time.sleep(5)
                trader.refresh_bankroll()
                continue

            DASHBOARD_STATE["status"] = "running"
            DASHBOARD_STATE["error"] = None

            # Track profit before trade to calculate individual trade P&L
            profit_before = trader.state.total_profit

            # Check for pending trade from tracker
            if trader.tracker.trades:
                last_trade = trader.tracker.trades[-1]
                if last_trade.won is None:  # Not yet settled
                    DASHBOARD_STATE["pending_trade"] = {
                        "ticker": last_trade.ticker,
                        "side": last_trade.side,
                        "contracts": last_trade.contracts,
                        "fill_price": last_trade.actual_fill_price,
                        "cost": last_trade.cost_cents / 100,
                        "time": last_trade.timestamp,
                    }
                    # Log pending state for Railway visibility
                    print("=" * 60)
                    print(f"[TRADE PENDING] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                    print(f"  Ticker: {last_trade.ticker}")
                    print(f"  Side: {last_trade.side.upper()}")
                    print(f"  Contracts: {last_trade.contracts} @ {last_trade.actual_fill_price}c")
                    print(f"  Cost: ${last_trade.cost_cents/100:.2f}")
                    print(f"  Waiting for market settlement...")
                    print("=" * 60)
                else:
                    DASHBOARD_STATE["pending_trade"] = None

            traded = trader.run_once()

            if traded:
                # Calculate this trade's profit (not cumulative)
                trade_profit = trader.state.total_profit - profit_before

                # Get the last trade details from tracker
                last_trade = trader.tracker.trades[-1] if trader.tracker.trades else None

                # === EXPLICIT LOGGING FOR RAILWAY ===
                print("=" * 60)
                print(f"[TRADE COMPLETE] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                if last_trade:
                    print(f"  Ticker: {last_trade.ticker}")
                    print(f"  Side: {last_trade.side.upper()}")
                    print(f"  Contracts: {last_trade.contracts}")
                    print(f"  Intended: {last_trade.intended_price}c -> Fill: {last_trade.actual_fill_price}c (slip: {last_trade.actual_fill_price - last_trade.intended_price:+d}c)")
                    print(f"  Cost: ${last_trade.cost_cents/100:.2f} | Fee: ${last_trade.fee_cents/100:.2f}")
                    print(f"  Result: {'WIN' if last_trade.won else 'LOSS'}")
                    print(f"  P&L: ${trade_profit:+.2f}")
                print("=" * 60)

                trade_details = {
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "profit": trade_profit,
                }
                if last_trade:
                    trade_details.update({
                        "ticker": last_trade.ticker,
                        "side": last_trade.side,
                        "contracts": last_trade.contracts,
                        "intended_price": last_trade.intended_price,
                        "fill_price": last_trade.actual_fill_price,
                        "slippage": last_trade.actual_fill_price - last_trade.intended_price,
                        "cost": last_trade.cost_cents / 100,
                        "fee": last_trade.fee_cents / 100,
                        "won": last_trade.won,
                        "bet_number": last_trade.bet_number,
                    })

                log_activity(f"TRADE: {last_trade.side.upper() if last_trade else '?'} @ {last_trade.actual_fill_price if last_trade else '?'}c -> ${trade_profit:+.2f}")
                DASHBOARD_STATE["recent_trades"].append(trade_details)
                DASHBOARD_STATE["pending_trade"] = None  # Clear pending

                # Auto-compound: increase effective bankroll after wins
                if trade_profit > 0 and DASHBOARD_STATE.get("auto_compound", True):
                    current_effective = DASHBOARD_STATE.get("effective_bankroll", 0)
                    if current_effective > 0:
                        DASHBOARD_STATE["effective_bankroll"] = current_effective + trade_profit
                        trader.effective_bankroll = DASHBOARD_STATE["effective_bankroll"]
                        log_activity(f"Auto-compound: bankroll now ${DASHBOARD_STATE['effective_bankroll']:.2f}")

                update_dashboard(trader)
                time.sleep(1)  # 1s pause after trade to let settlement process
            else:
                # NO opportunities found - poll faster to catch them
                # Don't log every scan - too spammy at 300ms polling
                time.sleep(0.3)  # 300ms polling to catch fast-moving opportunities

    except Exception as e:
        DASHBOARD_STATE["status"] = "error"
        DASHBOARD_STATE["error"] = str(e)
        print(f"Error: {e}")
        while True:
            time.sleep(60)


def cmd_test():
    """Test API connection."""
    print("Testing Kalshi API connection...")

    try:
        config = load_config()
        client = KalshiClient(config.kalshi)

        status = client.get_exchange_status()
        print(f"Exchange status: {status}")

        balance = client.get_balance_dollars()
        print(f"Account balance: ${balance:.2f}")

        markets = client.get_markets(limit=5)
        print(f"Found {len(markets.get('markets', []))} markets")

        print("\nConnection successful!")

    except Exception as e:
        print(f"Connection failed: {e}")


def cmd_reset_recovery():
    """Reset martingale recovery state."""
    print("Resetting martingale recovery state...")

    try:
        trader = Trader()
        trader.reset_recovery_mode()
        print("Recovery state reset successfully!")
        print(f"Consecutive losses: {trader.tracker.martingale.consecutive_losses}")
        print(f"In recovery: {trader.tracker.martingale.in_recovery}")
    except Exception as e:
        print(f"Error: {e}")


def main():
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else "run"

    if cmd == "run":
        cmd_run()
    elif cmd == "test":
        cmd_test()
    elif cmd == "reset-recovery":
        cmd_reset_recovery()
    else:
        print(f"Unknown command: {cmd}")
        print("Usage: python main.py [run|test|reset-recovery]")


if __name__ == "__main__":
    main()
