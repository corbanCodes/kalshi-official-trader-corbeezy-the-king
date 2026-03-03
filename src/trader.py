"""
Main trading loop for the 15-minute strategy.
Orchestrates scanning, betting, and recovery.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional

from .config import AppConfig, load_config
from .kalshi_client import KalshiClient
from .market_scanner import MarketScanner, TradingOpportunity
from .martingale import MartingaleCalculator, MartingaleBet
from .trade_executor import TradeExecutor, TradeRecord, TradeStatus


@dataclass
class TradingState:
    """Persistent trading state."""
    bankroll: float
    consecutive_losses: int = 0
    total_trades: int = 0
    total_wins: int = 0
    total_losses: int = 0
    total_profit: float = 0.0
    session_start: str = ""
    last_trade_time: str = ""

    def save(self, path: Path):
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2)

    @classmethod
    def load(cls, path: Path) -> "TradingState":
        if path.exists():
            with open(path) as f:
                return cls(**json.load(f))
        return cls(bankroll=0)


class Trader:
    """
    Main trading orchestrator.

    Implements the complete strategy:
    1. Wait 10 minutes into 15-minute window
    2. Find 80-90c opportunities
    3. Place limit orders at 1c above ask
    4. Track fills and settlements
    5. Martingale recovery on losses
    6. Exponential bankroll growth
    """

    def __init__(self, config: AppConfig = None):
        self.config = config or load_config()

        # Initialize components
        self.client = KalshiClient(self.config.kalshi)
        self.scanner = MarketScanner(
            client=self.client,
            min_price=self.config.trading.min_entry_price,
            max_price=self.config.trading.max_entry_price,
            data_dir=self.config.data_dir,
        )
        self.martingale = MartingaleCalculator(
            target_profit=self.config.target_profit_per_trade,
            max_consecutive_losses=self.config.trading.max_consecutive_losses,
        )
        self.executor = TradeExecutor(
            client=self.client,
            limit_offset=self.config.trading.limit_order_offset,
        )

        # State
        self.state_path = self.config.data_dir / "trading_state.json"
        self.state = TradingState.load(self.state_path)

        if self.state.bankroll == 0:
            self.state.bankroll = self.config.starting_bankroll
            self.state.session_start = datetime.now(timezone.utc).isoformat()

        # Logging
        self.log_path = self.config.logs_dir / f"trades_{datetime.now().strftime('%Y%m%d')}.json"

    def log(self, message: str, level: str = "INFO"):
        """Log a message with timestamp."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        prefix = {"INFO": "[*]", "TRADE": "[$]", "WIN": "[+]", "LOSS": "[-]", "ERROR": "[!]", "WARN": "[?]"}
        print(f"{timestamp} {prefix.get(level, '[*]')} {message}")

    def refresh_bankroll(self):
        """Refresh bankroll from Kalshi account."""
        try:
            balance = self.client.get_balance_dollars()
            self.state.bankroll = balance
            self.log(f"Bankroll: ${balance:.2f}")
        except Exception as e:
            self.log(f"Could not refresh bankroll: {e}", "WARN")

    def can_trade(self) -> bool:
        """Check if we can place another trade."""
        # Check if we're bust
        if self.martingale.is_bust:
            self.log("BUST - Max consecutive losses exceeded", "ERROR")
            return False

        # Check bankroll
        if self.state.bankroll < 10:  # Minimum viable bankroll
            self.log("Bankroll too low", "ERROR")
            return False

        return True

    def calculate_bet(self, opportunity: TradingOpportunity) -> Optional[MartingaleBet]:
        """Calculate the next bet based on current state."""
        bet = self.martingale.calculate_next_bet(
            bankroll=self.state.bankroll,
            entry_price_cents=opportunity.entry_price,
        )

        if not bet:
            self.log("Cannot calculate bet - insufficient bankroll for recovery", "ERROR")
            return None

        # Verify we can afford
        if bet.cost_dollars > self.state.bankroll:
            self.log(f"Bet cost ${bet.cost_dollars:.2f} exceeds bankroll ${self.state.bankroll:.2f}", "ERROR")
            return None

        return bet

    def execute_trade(self, opportunity: TradingOpportunity, bet: MartingaleBet) -> Optional[TradeRecord]:
        """Execute a single trade."""
        self.log(
            f"EXECUTING: {opportunity.side.upper()} {opportunity.ticker} @ {opportunity.entry_price}c | "
            f"{bet.contracts} contracts | ${bet.cost_dollars:.2f}",
            "TRADE"
        )

        result = self.executor.execute_opportunity(opportunity, bet)

        if not result.success:
            self.log(f"Order failed: {result.error}", "ERROR")
            return None

        trade = result.trade

        # Wait for fill
        self.log(f"Waiting for fill (order {trade.order_id})...")
        trade = self.executor.wait_for_fill(trade.order_id, timeout_seconds=30)

        if trade.status == TradeStatus.UNFILLED:
            self.log(f"Order not filled - canceled", "WARN")
            return None

        if trade.filled_contracts < bet.contracts:
            self.log(f"Partial fill: {trade.filled_contracts}/{bet.contracts}", "WARN")

        self.log(f"Filled @ {trade.actual_fill_price}c | Waiting for settlement...")

        return trade

    def process_settlement(self, trade: TradeRecord):
        """Process trade settlement and update state."""
        # Wait for settlement
        trade = self.executor.wait_for_settlement(trade, timeout_seconds=600)

        if trade.status == TradeStatus.SETTLED_WIN:
            self.log(
                f"WIN: {trade.ticker} settled {trade.settlement_result.upper()} | "
                f"+${trade.net_profit_dollars:.2f}",
                "WIN"
            )
            self.state.total_wins += 1
            self.state.consecutive_losses = 0
            self.state.total_profit += trade.net_profit_dollars
            self.state.bankroll += trade.net_profit_dollars
            self.martingale.record_win()

        elif trade.status == TradeStatus.SETTLED_LOSS:
            self.log(
                f"LOSS: {trade.ticker} settled {trade.settlement_result.upper()} | "
                f"${trade.net_profit_dollars:.2f}",
                "LOSS"
            )
            self.state.total_losses += 1
            self.state.consecutive_losses += 1
            self.state.total_profit += trade.net_profit_dollars
            self.state.bankroll += trade.net_profit_dollars  # negative
            self.martingale.record_loss(trade.cost_dollars)

        else:
            self.log(f"Settlement unclear for {trade.ticker}", "WARN")

        self.state.total_trades += 1
        self.state.last_trade_time = datetime.now(timezone.utc).isoformat()
        self.state.save(self.state_path)

    def run_once(self) -> bool:
        """
        Run one trading cycle.

        Returns:
            True if a trade was executed
        """
        if not self.can_trade():
            return False

        # Scan for opportunities
        self.log("Scanning markets...")
        opportunity = self.scanner.find_best_opportunity()

        if not opportunity:
            self.log("No opportunities in 80-90c range")
            return False

        self.log(f"Found: {opportunity}")

        # Calculate bet
        bet = self.calculate_bet(opportunity)
        if not bet:
            return False

        # Show martingale context
        if bet.bet_number > 1:
            self.log(
                f"RECOVERY BET #{bet.bet_number}: Recovering ${self.martingale.state.total_loss_dollars:.2f} + "
                f"${self.martingale.target_profit:.2f} target",
                "WARN"
            )

        # Execute
        trade = self.execute_trade(opportunity, bet)
        if not trade:
            return False

        # Process settlement
        self.process_settlement(trade)

        return True

    def run_continuous(self, poll_interval: float = 5.0):
        """
        Run continuously, scanning for opportunities.

        Args:
            poll_interval: Seconds between scans
        """
        self.log("=" * 60)
        self.log("STARTING 15-MINUTE STRATEGY TRADER")
        self.log(f"Bankroll: ${self.state.bankroll:.2f}")
        self.log(f"Target per trade: ${self.config.target_profit_per_trade:.2f}")
        self.log(f"Entry range: {self.config.trading.min_entry_price}-{self.config.trading.max_entry_price}c")
        self.log("=" * 60)

        try:
            while self.can_trade():
                traded = self.run_once()

                if not traded:
                    time.sleep(poll_interval)
                else:
                    # Brief pause after trade
                    time.sleep(2)

                # Refresh bankroll periodically
                if self.state.total_trades % 10 == 0:
                    self.refresh_bankroll()

        except KeyboardInterrupt:
            self.log("Shutting down...")

        finally:
            self.shutdown()

    def shutdown(self):
        """Clean shutdown."""
        self.log("=" * 60)
        self.log("SESSION SUMMARY")
        self.log(f"Total trades: {self.state.total_trades}")
        self.log(f"Wins/Losses: {self.state.total_wins}/{self.state.total_losses}")
        if self.state.total_wins + self.state.total_losses > 0:
            wr = self.state.total_wins / (self.state.total_wins + self.state.total_losses) * 100
            self.log(f"Win rate: {wr:.1f}%")
        self.log(f"Total P&L: ${self.state.total_profit:+.2f}")
        self.log(f"Final bankroll: ${self.state.bankroll:.2f}")
        self.log("=" * 60)

        # Save state
        self.state.save(self.state_path)

        # Save order book log
        self.scanner.save_order_book_log()

        # Print trade log
        self.executor.print_trade_log()

    def show_status(self):
        """Print current status."""
        self.refresh_bankroll()

        print("\n" + "=" * 50)
        print("CURRENT STATUS")
        print("=" * 50)
        print(f"Bankroll: ${self.state.bankroll:.2f}")
        print(f"Consecutive losses: {self.state.consecutive_losses}")
        print(f"In recovery mode: {self.martingale.state.in_recovery}")
        print(f"Session trades: {self.state.total_trades}")
        print(f"Session P&L: ${self.state.total_profit:+.2f}")
        print()

        # Show martingale sequence
        self.martingale.print_sequence(self.state.bankroll, 85)

    def paper_trade(self, num_trades: int = 10):
        """
        Paper trading mode - simulates without real orders.
        """
        self.log("=" * 60)
        self.log("PAPER TRADING MODE")
        self.log("=" * 60)

        for i in range(num_trades):
            self.log(f"\n--- Trade {i+1}/{num_trades} ---")

            opportunity = self.scanner.find_best_opportunity()
            if not opportunity:
                self.log("No opportunity found, waiting...")
                time.sleep(5)
                continue

            self.log(f"Would trade: {opportunity}")

            bet = self.calculate_bet(opportunity)
            if bet:
                self.log(f"Bet: {bet.contracts} contracts @ ${bet.cost_dollars:.2f}")
                self.log(f"If win: +${bet.net_profit_if_win:.2f}")

            time.sleep(5)
