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
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from urllib.parse import parse_qs

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
    "today_profit": 0,
    "total_trades": 0,
    "wins": 0,
    "losses": 0,
    "consecutive_losses": 0,
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

    def do_GET(self):
        if self.path == "/" or self.path == "/dashboard":
            self.send_dashboard()
        elif self.path == "/api/status":
            self.send_json(DASHBOARD_STATE)
        elif self.path == "/health":
            self.send_json({"status": "ok"})
        else:
            self.send_error(404)

    def do_POST(self):
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
        .btn { padding: 12px 30px; border: none; border-radius: 8px; font-size: 1rem; cursor: pointer; font-family: monospace; font-weight: bold; }
        .btn-start { background: #6bcb77; color: #000; }
        .btn-start:hover { background: #5ab868; }
        .btn-stop { background: #ff6b6b; color: #fff; }
        .btn-stop:hover { background: #e55555; }
        .btn:disabled { opacity: 0.5; cursor: not-allowed; }
        .grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 20px; margin: 20px 0; }
        .card { background: #1a1a1a; border: 1px solid #333; border-radius: 10px; padding: 20px; text-align: center; }
        .card .value { font-size: 2rem; font-weight: bold; color: #6bcb77; }
        .card .label { color: #888; margin-top: 5px; }
        .card.warn .value { color: #ffd93d; }
        .card.danger .value { color: #ff6b6b; }
        .status { padding: 10px 20px; border-radius: 20px; display: inline-block; }
        .status.running { background: rgba(107,203,119,0.2); color: #6bcb77; }
        .status.stopped { background: rgba(136,136,136,0.2); color: #888; }
        .status.error { background: rgba(255,107,107,0.2); color: #ff6b6b; }
        .trades { background: #1a1a1a; border-radius: 10px; padding: 20px; margin-top: 20px; }
        .trade { padding: 10px; border-bottom: 1px solid #333; display: flex; justify-content: space-between; }
        .trade.win { border-left: 3px solid #6bcb77; }
        .trade.loss { border-left: 3px solid #ff6b6b; }
        .updated { color: #666; margin-top: 20px; font-size: 0.8rem; }
    </style>
</head>
<body>
    <h1>15-MINUTE STRATEGY BOT</h1>

    <div class="controls">
        <button class="btn btn-start" id="startBtn" onclick="startTrading()">START TRADING</button>
        <button class="btn btn-stop" id="stopBtn" onclick="stopTrading()" disabled>STOP</button>
        <span class="status STATUS_CLASS" id="statusBadge">STATUS_TEXT</span>
    </div>

    <div class="grid">
        <div class="card">
            <div class="value">$BANKROLL</div>
            <div class="label">Bankroll</div>
        </div>
        <div class="card">
            <div class="value">$TODAY_PROFIT</div>
            <div class="label">Today P&L</div>
        </div>
        <div class="card">
            <div class="value">WINS/LOSSES</div>
            <div class="label">W/L (WIN_RATE%)</div>
        </div>
        <div class="card CONSEC_CLASS">
            <div class="value">CONSECUTIVE</div>
            <div class="label">Consecutive Losses</div>
        </div>
    </div>

    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-top: 20px;">
        <div class="trades">
            <h3 style="margin-bottom: 15px; color: #888;">Recent Trades</h3>
            TRADES_HTML
        </div>
        <div class="trades">
            <h3 style="margin-bottom: 15px; color: #888;">Activity Log</h3>
            <div style="font-size: 0.85rem; max-height: 200px; overflow-y: auto;">ACTIVITY_LOG</div>
        </div>
    </div>

    <div class="trades" style="margin-top: 20px;">
        <h3 style="margin-bottom: 15px; color: #888;">Market Prices</h3>
        <div style="font-size: 0.9rem;">MARKET_PRICES</div>
    </div>

    <p class="updated">Last updated: LAST_UPDATE</p>

    <script>
        function startTrading() {
            fetch('/api/start', {method: 'POST'})
                .then(r => r.json())
                .then(d => { if(d.success) location.reload(); });
        }
        function stopTrading() {
            fetch('/api/stop', {method: 'POST'})
                .then(r => r.json())
                .then(d => { if(d.success) location.reload(); });
        }
        // Auto-refresh every 2 seconds
        setTimeout(() => location.reload(), 2000);
    </script>
</body>
</html>"""

        state = DASHBOARD_STATE
        win_rate = (state["wins"] / (state["wins"] + state["losses"]) * 100) if (state["wins"] + state["losses"]) > 0 else 0

        trades_html = ""
        for t in state.get("recent_trades", [])[-10:]:
            cls = "win" if t.get("profit", 0) > 0 else "loss"
            trades_html += f'<div class="trade {cls}"><span>{t.get("time", "")}</span><span>${t.get("profit", 0):+.2f}</span></div>'

        if not trades_html:
            trades_html = '<div class="trade">No trades yet</div>'

        if state["status"] == "running":
            status_class = "running"
        elif state.get("error"):
            status_class = "error"
        else:
            status_class = "stopped"

        status_text = state["status"].upper()
        if state.get("error"):
            status_text += f": {state['error']}"

        # Button states based on trading status
        if state["trading_enabled"]:
            html = html.replace('id="startBtn"', 'id="startBtn" disabled')
            html = html.replace('id="stopBtn" disabled', 'id="stopBtn"')

        consec_class = "danger" if state["consecutive_losses"] >= 2 else ("warn" if state["consecutive_losses"] == 1 else "")

        html = html.replace("STATUS_CLASS", status_class)
        html = html.replace("STATUS_TEXT", status_text)
        html = html.replace("BANKROLL", f"{state['bankroll']:.2f}")
        html = html.replace("TODAY_PROFIT", f"{state['today_profit']:+.2f}")
        html = html.replace("WINS/LOSSES", f"{state['wins']}/{state['losses']}")
        html = html.replace("WIN_RATE", f"{win_rate:.1f}")
        html = html.replace("CONSECUTIVE", str(state["consecutive_losses"]))
        html = html.replace("CONSEC_CLASS", consec_class)
        html = html.replace("TRADES_HTML", trades_html)

        # Activity log
        activity_html = "<br>".join(state.get("activity_log", [])[-10:]) or "No activity yet"
        html = html.replace("ACTIVITY_LOG", activity_html)

        # Market prices
        market_html = ""
        for ticker, prices in state.get("market_prices", {}).items():
            market_html += f'<div style="padding:5px 0; border-bottom:1px solid #333;">'
            market_html += f'<strong>{ticker}</strong>: '
            market_html += f'YES {prices["yes_bid"]}/{prices["yes_ask"]}c | '
            market_html += f'NO {prices["no_bid"]}/{prices["no_ask"]}c</div>'
        html = html.replace("MARKET_PRICES", market_html or "No market data")

        html = html.replace("LAST_UPDATE", state.get("last_update", "never"))

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
    DASHBOARD_STATE.update({
        "bankroll": trader.state.bankroll,
        "today_profit": trader.state.total_profit,
        "total_trades": trader.state.total_trades,
        "wins": trader.state.total_wins,
        "losses": trader.state.total_losses,
        "consecutive_losses": trader.state.consecutive_losses,
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

            # Always scan markets (even when stopped)
            try:
                markets = trader.client.get_markets(limit=10)
                for m in markets.get("markets", [])[:5]:
                    ticker = m.get("ticker", "")
                    yes_bid = m.get("yes_bid", 0)
                    yes_ask = m.get("yes_ask", 0)
                    DASHBOARD_STATE["market_prices"][ticker] = {
                        "yes_bid": yes_bid,
                        "yes_ask": yes_ask,
                        "no_bid": 100 - yes_ask if yes_ask else 0,
                        "no_ask": 100 - yes_bid if yes_bid else 0,
                    }
                log_activity(f"Scanned {len(markets.get('markets', []))} markets")
            except Exception as e:
                log_activity(f"Scan error: {e}")

            # Check if trading is enabled
            if not DASHBOARD_STATE["trading_enabled"]:
                DASHBOARD_STATE["status"] = "stopped"
                time.sleep(1)
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

            traded = trader.run_once()

            if traded:
                log_activity(f"TRADE EXECUTED! P&L: ${trader.state.total_profit:+.2f}")
                DASHBOARD_STATE["recent_trades"].append({
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "profit": trader.state.total_profit,
                })
                update_dashboard(trader)
                time.sleep(2)
            else:
                log_activity("No opportunities in 80-90c range")
                time.sleep(5)

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
