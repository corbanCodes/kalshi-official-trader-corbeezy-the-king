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

# Global state for web dashboard
DASHBOARD_STATE = {
    "status": "stopped",
    "trading_enabled": False,
    "bankroll": 0,
    "apportioned_bankroll": None,  # For multi-bot setup
    "today_profit": 0,
    "total_trades": 0,
    "wins": 0,
    "losses": 0,
    "consecutive_losses": 0,
    "in_recovery": False,
    "recovery_target": 0,
    "recovery_stage": 0,  # 0 = not in recovery, 1-3 = recovery stage
    "last_trade": None,
    "last_update": None,
    "recent_trades": [],
    "error": None,
    "activity_log": [],
    "current_market": None,
    "market_prices": {},
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
        else:
            self.send_error(404)

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

    <p class="updated">Last updated: <span id="lastUpdate">never</span> | Updates every 500ms</p>

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

                    // Recent trades
                    const trades = data.recent_trades || [];
                    let tradesHtml = '';
                    trades.slice(-10).forEach(t => {
                        const cls = (t.profit || 0) > 0 ? 'win' : 'loss';
                        tradesHtml += '<div class="trade ' + cls + '"><span>' + (t.time || '') + '</span><span>$' + (t.profit >= 0 ? '+' : '') + (t.profit || 0).toFixed(2) + '</span></div>';
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

        // Update every 500ms via AJAX (no page reload)
        setInterval(updateStatus, 500);

        // Initial load
        updateStatus();
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

    # Get apportioned bankroll from config if set
    apportioned = trader.config.trading.apportioned_bankroll

    DASHBOARD_STATE.update({
        "bankroll": trader.state.bankroll,
        "apportioned_bankroll": apportioned,
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
        trader = Trader()
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
                log_activity(f"Scanned {len(crypto_markets)} 15-min crypto markets")
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
            traded = trader.run_once()

            if traded:
                # Calculate this trade's profit (not cumulative)
                trade_profit = trader.state.total_profit - profit_before
                log_activity(f"TRADE EXECUTED! P&L: ${trade_profit:+.2f} (Total: ${trader.state.total_profit:+.2f})")
                DASHBOARD_STATE["recent_trades"].append({
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "profit": trade_profit,
                })
                update_dashboard(trader)
                time.sleep(2)
            else:
                log_activity("No opportunities in 80-92c range")
                time.sleep(1)  # Fast 1-second polling to catch quick opportunities

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


def main():
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else "run"

    if cmd == "run":
        cmd_run()
    elif cmd == "test":
        cmd_test()
    else:
        print(f"Unknown command: {cmd}")
        print("Usage: python main.py [run|test]")


if __name__ == "__main__":
    main()
