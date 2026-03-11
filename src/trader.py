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
from .kraken import KrakenClient
from .market_scanner import MarketScanner, TradingOpportunity
from .martingale import MartingaleCalculator, MartingaleBet
from .trade_executor import TradeExecutor, TradeRecord, TradeStatus
from .trade_tracker import TradeTracker, TradeRecord as TrackedTrade


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
            max_consecutive_losses=self.config.trading.max_consecutive_losses,
        )
        self.executor = TradeExecutor(
            client=self.client,
            limit_offset=self.config.trading.limit_order_offset,
        )

        # Trade tracker for exact payout calculations with Kalshi official settlement
        self.tracker = TradeTracker(data_dir=self.config.data_dir)

        # State
        self.state_path = self.config.data_dir / "trading_state.json"
        self.state = TradingState.load(self.state_path)

        # Sync martingale state from tracker (it's persisted)
        self._sync_martingale_from_tracker()

        # Always fetch real balance from Kalshi
        self.refresh_bankroll()
        self.state.session_start = datetime.now(timezone.utc).isoformat()

        # Effective bankroll for calculations (can be overridden by apportioned amount)
        self.effective_bankroll = self.config.trading.apportioned_bankroll or self.state.bankroll

        # Logging
        self.log_path = self.config.logs_dir / f"trades_{datetime.now().strftime('%Y%m%d')}.json"

        # Rate limiting for scan logs (don't spam at 300ms polling)
        self._last_scan_log_time = 0
        self._scan_log_interval = 10  # Only log "no opportunities" every 10 seconds

    def log(self, message: str, level: str = "INFO"):
        """Log a message with timestamp."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        prefix = {
            "INFO": "[INFO ]",
            "TRADE": "[TRADE]",
            "WIN": "[WIN  ]",
            "LOSS": "[LOSS ]",
            "ERROR": "[ERROR]",
            "WARN": "[WARN ]",
            "DEBUG": "[DEBUG]",
            "SETTLE": "[SETTL]",
        }
        print(f"{timestamp} {prefix.get(level, '[INFO ]')} {message}")

    def _sync_martingale_from_tracker(self):
        """Sync the MartingaleCalculator state from the TradeTracker's persisted state."""
        tracker_state = self.tracker.martingale
        self.martingale.state.in_recovery = tracker_state.in_recovery
        self.martingale.state.total_loss_dollars = tracker_state.total_loss_cents / 100
        self.martingale.state.base_target_profit_dollars = tracker_state.base_target_profit_cents / 100
        self.martingale.state.consecutive_losses = tracker_state.consecutive_losses
        # consecutive_losses is tracked in self.state, sync it too
        self.state.consecutive_losses = tracker_state.consecutive_losses
        if tracker_state.in_recovery:
            self.log(
                f"RECOVERY MODE ACTIVE: {tracker_state.consecutive_losses} consecutive losses",
                "WARN"
            )
            self.log(
                f"  Total loss to recover: ${tracker_state.total_loss_cents/100:.2f}",
                "WARN"
            )
            self.log(
                f"  Next bet will be recovery #{tracker_state.consecutive_losses + 1}",
                "WARN"
            )

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

        # Check real bankroll (must have enough to execute)
        if self.state.bankroll < 10:
            self.log("Real bankroll too low", "ERROR")
            return False

        # Check effective bankroll (apportioned limit)
        effective = getattr(self, 'effective_bankroll', self.state.bankroll)
        if effective < 10:
            self.log(f"Apportioned bankroll too low (${effective:.2f})", "ERROR")
            return False

        return True

    def calculate_bet(self, opportunity: TradingOpportunity) -> Optional[MartingaleBet]:
        """Calculate the next bet based on current state."""
        # Use effective bankroll (apportioned or full)
        bankroll = getattr(self, 'effective_bankroll', self.state.bankroll)

        bet = self.martingale.calculate_next_bet(
            bankroll=bankroll,
            entry_price_cents=opportunity.entry_price,
        )

        if not bet:
            self.log(f"Cannot calculate bet - insufficient bankroll (${bankroll:.2f}) for recovery", "ERROR")
            return None

        # Verify we can afford with REAL balance (not apportioned)
        if bet.cost_dollars > self.state.bankroll:
            self.log(f"Bet cost ${bet.cost_dollars:.2f} exceeds real bankroll ${self.state.bankroll:.2f}", "ERROR")
            return None

        return bet

    def execute_trade(self, opportunity: TradingOpportunity, bet: MartingaleBet) -> Optional[tuple]:
        """Execute a single trade. Returns (TradeRecord, TrackedTrade) tuple."""
        self.log("=" * 60, "TRADE")
        self.log(f"EXECUTING TRADE #{bet.bet_number}", "TRADE")
        self.log(f"  Ticker: {opportunity.ticker}", "TRADE")
        self.log(f"  Side: {opportunity.side.upper()}", "TRADE")
        self.log(f"  Entry price: {opportunity.entry_price}c", "TRADE")
        self.log(f"  Contracts: {bet.contracts}", "TRADE")
        self.log(f"  Cost: ${bet.cost_dollars:.2f}", "TRADE")
        self.log(f"  Strike: ${opportunity.floor_strike:,.2f}", "TRADE")
        self.log(f"  Close time: {opportunity.close_time.isoformat()}", "TRADE")
        if bet.bet_number > 1:
            self.log(f"  RECOVERY BET: Recovering ${self.martingale.state.total_loss_dollars:.2f} in losses", "WARN")

        result = self.executor.execute_opportunity(opportunity, bet)

        if not result.success:
            self.log(f"ORDER FAILED: {result.error}", "ERROR")
            return None

        trade = result.trade
        self.log(f"Order submitted: {trade.order_id}", "DEBUG")

        # Wait for fill
        self.log(f"Waiting for fill (timeout: 30s)...", "DEBUG")
        trade = self.executor.wait_for_fill(trade.order_id, timeout_seconds=30)

        if trade.status == TradeStatus.UNFILLED:
            self.log(f"ORDER NOT FILLED - canceled after timeout", "WARN")
            return None

        if trade.filled_contracts < bet.contracts:
            self.log(f"PARTIAL FILL: {trade.filled_contracts}/{bet.contracts} contracts", "WARN")

        slippage = trade.actual_fill_price - opportunity.entry_price
        self.log(f"ORDER FILLED:", "TRADE")
        self.log(f"  Fill price: {trade.actual_fill_price}c (slippage: {slippage:+d}c)", "TRADE")
        self.log(f"  Contracts filled: {trade.filled_contracts}", "TRADE")
        self.log(f"  Actual cost: ${trade.filled_contracts * trade.actual_fill_price / 100:.2f}", "TRADE")

        # Create tracked trade record with exact details
        bankroll_cents = int(self.state.bankroll * 100)
        tracked = self.tracker.create_trade(
            ticker=opportunity.ticker,
            side=opportunity.side,
            contracts=trade.filled_contracts,
            intended_price=opportunity.entry_price,
            actual_fill_price=trade.actual_fill_price,
            floor_strike=opportunity.floor_strike,
            close_time=opportunity.close_time.isoformat(),
            bankroll_cents=bankroll_cents,
        )

        self.log(f"Trade recorded: {tracked.trade_id}", "DEBUG")
        self.log(f"  Fee calculated: ${tracked.fee_cents/100:.2f}", "DEBUG")
        self.log(f"  Bet #{tracked.bet_number} | Recovering: ${tracked.recovering_amount_cents/100:.2f}", "DEBUG")
        self.log("Waiting for settlement...", "TRADE")

        return (trade, tracked)

    def get_official_settlement(self, ticker: str, max_wait: int = 180, poll_interval: int = 5) -> Optional[str]:
        """
        Poll Kalshi's settled markets API for the official result.

        This is the SAME method the multi-crypto scraper uses - authoritative source.

        Args:
            ticker: The market ticker to check
            max_wait: Maximum seconds to wait for settlement (default 180 = 3 min)
            poll_interval: Seconds between polls (default 5)

        Returns:
            'yes' or 'no' if settled, None if timeout
        """
        import requests

        KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
        series_ticker = "KXBTC15M"

        start_time = time.time()
        attempts = 0

        while (time.time() - start_time) < max_wait:
            attempts += 1
            try:
                resp = requests.get(
                    f"{KALSHI_API}/markets",
                    params={"series_ticker": series_ticker, "status": "settled", "limit": 20},
                    timeout=10
                )
                resp.raise_for_status()
                data = resp.json()

                for market in data.get('markets', []):
                    if market.get('ticker') == ticker:
                        result = market.get('result')
                        if result:
                            elapsed = time.time() - start_time
                            print(f"[SETTLED] Official result: {result.upper()} (took {elapsed:.1f}s)")
                            self.log(f"Official settlement received after {elapsed:.1f}s ({attempts} polls)", "SETTLE")
                            return result

                # Not settled yet, wait and try again
                elapsed = time.time() - start_time
                if attempts % 6 == 0:  # Log every 30 seconds
                    print(f"[POLLING] Waiting for settlement... {elapsed:.0f}s elapsed")
                    self.log(f"Waiting for official settlement... ({elapsed:.0f}s elapsed)", "SETTLE")
                time.sleep(poll_interval)

            except Exception as e:
                print(f"[POLL ERROR] {e}")
                self.log(f"Settlement poll error: {e}", "WARN")
                time.sleep(poll_interval)

        print(f"[TIMEOUT] No settlement after {max_wait}s!")
        return None

    def process_settlement(self, trade: TradeRecord, tracked: TrackedTrade):
        """Process trade settlement using Kalshi's OFFICIAL result API."""
        self.log("=" * 60, "SETTLE")
        self.log(f"SETTLEMENT PROCESS STARTED for {tracked.ticker}", "SETTLE")
        self.log(f"  Our side: {tracked.side.upper()}", "SETTLE")
        self.log(f"  Strike: ${tracked.floor_strike:,.2f}", "SETTLE")
        self.log(f"  Contracts: {tracked.contracts} @ {tracked.actual_fill_price}c", "SETTLE")
        self.log(f"  Cost: ${tracked.cost_cents/100:.2f} + ${tracked.fee_cents/100:.2f} fee", "SETTLE")

        # Wait for market close time
        close_time = datetime.fromisoformat(tracked.close_time.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        wait_seconds = (close_time - now).total_seconds()

        if wait_seconds > 0:
            print(f"[WAITING] {wait_seconds:.0f}s until market closes...")
            self.log(f"Waiting {wait_seconds:.0f}s for market close...", "SETTLE")
            time.sleep(wait_seconds)

        # Wait for Kalshi to settle (~90-105 seconds typically)
        print("[SETTLEMENT] Waiting for Kalshi official settlement...")
        self.log("Polling Kalshi for official settlement result...", "SETTLE")
        self.log("(Kalshi typically takes 90-105 seconds to settle)", "SETTLE")

        # Poll for official result
        result = self.get_official_settlement(tracked.ticker, max_wait=180, poll_interval=5)

        if not result:
            print("[FALLBACK] Kalshi settlement timeout - checking bankroll change...")
            self.log("CRITICAL: Timeout waiting for Kalshi settlement!", "ERROR")
            self.log("Falling back to bankroll change detection...", "WARN")

            # Fallback: check bankroll change
            old_bankroll = self.state.bankroll
            self.refresh_bankroll()
            delta = self.state.bankroll - old_bankroll

            if delta > 0:
                result = tracked.side  # We won
                print(f"[FALLBACK] Bankroll +${delta:.2f} -> WIN")
                self.log(f"Bankroll increased ${delta:.2f} -> assuming WIN", "WARN")
            else:
                result = "yes" if tracked.side == "no" else "no"  # We lost
                print(f"[FALLBACK] Bankroll ${delta:.2f} -> LOSS")
                self.log(f"Bankroll decreased ${delta:.2f} -> assuming LOSS", "WARN")

        self.log(f"OFFICIAL RESULT from Kalshi: {result.upper()}", "SETTLE")

        we_won = (tracked.side == result)
        self.log(f"RESULT: Market settled {result.upper()}, we bet {tracked.side.upper()} -> {'WIN' if we_won else 'LOSS'}", "SETTLE")

        # Get actual bankroll from Kalshi
        old_bankroll = self.state.bankroll
        self.refresh_bankroll()
        bankroll_after_cents = int(self.state.bankroll * 100)
        self.log(f"Bankroll: ${old_bankroll:.2f} -> ${self.state.bankroll:.2f} (delta: ${self.state.bankroll - old_bankroll:+.2f})", "SETTLE")

        # Record settlement
        self.log("Recording settlement in trade tracker...", "SETTLE")
        self.tracker.settle_trade_with_result(tracked, result, bankroll_after_cents)

        # Log result with exact details
        slippage = tracked.actual_fill_price - tracked.intended_price
        if tracked.won:
            self.log("=" * 60, "WIN")
            self.log(f"TRADE WON: {tracked.ticker}", "WIN")
            self.log(f"  Market result: {result.upper()} | Our side: {tracked.side.upper()}", "WIN")
            self.log(f"  Gross payout: ${tracked.gross_payout_cents/100:.2f} ({tracked.contracts} contracts x $1)", "WIN")
            self.log(f"  Cost: ${tracked.cost_cents/100:.2f} | Fee: ${tracked.fee_cents/100:.2f}", "WIN")
            self.log(f"  Net profit: ${tracked.net_profit_cents/100:+.2f}", "WIN")
            self.log(f"  Slippage: {slippage:+d}c ({tracked.intended_price}c -> {tracked.actual_fill_price}c)", "WIN")
            if tracked.bet_number > 1:
                self.log(f"  Recovery bet #{tracked.bet_number}: Successfully recovered ${tracked.recovering_amount_cents/100:.2f}", "WIN")
            self.state.total_wins += 1
        else:
            self.log("=" * 60, "LOSS")
            self.log(f"TRADE LOST: {tracked.ticker}", "LOSS")
            self.log(f"  Market result: {result.upper()} | Our side: {tracked.side.upper()}", "LOSS")
            self.log(f"  Gross payout: $0.00 (lost)", "LOSS")
            self.log(f"  Cost lost: ${tracked.cost_cents/100:.2f} | Fee lost: ${tracked.fee_cents/100:.2f}", "LOSS")
            self.log(f"  Net loss: ${tracked.net_profit_cents/100:.2f}", "LOSS")
            self.log(f"  Slippage: {slippage:+d}c ({tracked.intended_price}c -> {tracked.actual_fill_price}c)", "LOSS")
            self.state.total_losses += 1

        self.state.total_profit += tracked.net_profit_cents / 100
        self.state.total_trades += 1
        self.state.last_trade_time = datetime.now(timezone.utc).isoformat()

        # Sync martingale state from tracker (it's the source of truth)
        old_recovery_state = self.martingale.state.in_recovery
        old_consecutive_losses = self.martingale.state.consecutive_losses
        self._sync_martingale_from_tracker()

        # Log martingale state change
        self.log("MARTINGALE STATE UPDATE:", "DEBUG")
        self.log(f"  Consecutive losses: {old_consecutive_losses} -> {self.martingale.state.consecutive_losses}", "DEBUG")
        self.log(f"  In recovery: {old_recovery_state} -> {self.martingale.state.in_recovery}", "DEBUG")
        if self.martingale.state.in_recovery:
            self.log(f"  Total loss to recover: ${self.martingale.state.total_loss_dollars:.2f}", "DEBUG")
            self.log(f"  Next bet will be RECOVERY bet #{self.martingale.state.consecutive_losses + 1}", "WARN")
        else:
            self.log(f"  Next bet will be fresh BASE bet", "DEBUG")

        # Print detailed trade summary
        self.tracker.print_trade_summary(tracked)
        self.log("=" * 60, "SETTLE")

        self.state.save(self.state_path)

    def run_once(self) -> bool:
        """
        Run one trading cycle.

        Returns:
            True if a trade was executed
        """
        if not self.can_trade():
            return False

        # Check if we're in recovery mode
        in_recovery = self.tracker.martingale.in_recovery

        # Scan for opportunities (don't log every scan - too spammy at 300ms polling)
        opportunity = self.scanner.find_best_opportunity()

        if not opportunity:
            # Rate-limit "no opportunities" logging to avoid spam
            now = time.time()
            if now - self._last_scan_log_time >= self._scan_log_interval:
                self._last_scan_log_time = now
                if in_recovery:
                    recovery_target = self.tracker.martingale.get_recovery_target_cents() / 100
                    self.log(
                        f"No opportunities in price range | RECOVERY MODE: "
                        f"{self.tracker.martingale.consecutive_losses} losses, "
                        f"need ${recovery_target:.2f} to recover",
                        "WARN"
                    )
                else:
                    self.log("No opportunities in 80-92c range (polling every 300ms)")
            return False

        # RECOVERY MODE: Apply additional filters
        if in_recovery:
            # Filter 1: Price cap at 85c for recovery bets
            recovery_cap = self.config.trading.recovery_price_cap
            if opportunity.entry_price > recovery_cap:
                recovery_target = self.tracker.martingale.get_recovery_target_cents() / 100
                self.log(
                    f"RECOVERY MODE: Skipping {opportunity.entry_price}c (cap is {recovery_cap}c) | "
                    f"Need ${recovery_target:.2f} to recover",
                    "WARN"
                )
                return False

            # Filter 2: BTC distance filter (only if enabled - 0 = disabled)
            min_distance = self.config.trading.min_btc_distance_pct
            if min_distance > 0:
                passes, reason, btc_price = KrakenClient.passes_distance_filter(
                    opportunity.floor_strike,
                    opportunity.side,
                    min_distance
                )

                if not passes:
                    recovery_target = self.tracker.martingale.get_recovery_target_cents() / 100
                    self.log(
                        f"RECOVERY MODE: Distance filter failed - {reason} | "
                        f"Need ${recovery_target:.2f} to recover",
                        "WARN"
                    )
                    return False

                self.log(f"RECOVERY MODE: {reason} | BTC ${btc_price:,.2f}", "INFO")

        # === OPPORTUNITY FOUND - LOG PROMINENTLY ===
        print("=" * 60)
        print(f"[OPPORTUNITY FOUND] {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
        print(f"  Ticker: {opportunity.ticker}")
        print(f"  Side: {opportunity.side.upper()}")
        print(f"  Entry Price: {opportunity.entry_price}c")
        print(f"  Strike: ${opportunity.floor_strike:,.2f}")
        print(f"  Close Time: {opportunity.close_time}")
        print("=" * 60)

        # Calculate bet
        bet = self.calculate_bet(opportunity)
        if not bet:
            return False

        # Show martingale context
        if bet.bet_number > 1:
            self.log(
                f"RECOVERY BET #{bet.bet_number}: Recovering ${self.martingale.state.total_loss_dollars:.2f} + "
                f"${self.martingale.state.base_target_profit_dollars:.2f} target",
                "WARN"
            )

        # === PLACING ORDER ===
        print(f"[PLACING ORDER] {bet.contracts} contracts @ {opportunity.entry_price}c limit")
        print(f"  Estimated cost: ${bet.cost_dollars:.2f}")

        # Execute
        result = self.execute_trade(opportunity, bet)
        if not result:
            return False

        trade, tracked = result

        # Process settlement with Kalshi official API
        self.process_settlement(trade, tracked)

        return True

    def run_continuous(self, poll_interval: float = 5.0):
        """
        Run continuously, scanning for opportunities.

        Args:
            poll_interval: Seconds between scans
        """
        self.log("=" * 60)
        self.log("STARTING 15-MINUTE STRATEGY TRADER")
        self.log("=" * 60)
        self.log("CONFIGURATION:")
        self.log(f"  Real Bankroll: ${self.state.bankroll:.2f}")
        self.log(f"  Effective Bankroll: ${self.effective_bankroll:.2f}")
        self.log(f"  Target per trade: ${self.config.target_profit_per_trade:.2f}")
        self.log(f"  Entry range: {self.config.trading.min_entry_price}-{self.config.trading.max_entry_price}c")
        self.log(f"  Recovery price cap: {self.config.trading.recovery_price_cap}c")
        self.log(f"  Max consecutive losses: {self.config.trading.max_consecutive_losses}")
        self.log("MARTINGALE STATE:")
        self.log(f"  In recovery: {self.martingale.state.in_recovery}")
        self.log(f"  Consecutive losses: {self.martingale.state.consecutive_losses}")
        if self.martingale.state.in_recovery:
            self.log(f"  Loss to recover: ${self.martingale.state.total_loss_dollars:.2f}")
        self.log(f"SESSION STATS:")
        self.log(f"  Total trades: {self.state.total_trades}")
        self.log(f"  Wins/Losses: {self.state.total_wins}/{self.state.total_losses}")
        self.log(f"  Total P&L: ${self.state.total_profit:+.2f}")
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

        # Print detailed trade history with exact payouts
        self.tracker.print_all_trades()

        # Also print executor log for reference
        self.executor.print_trade_log()

    def reset_recovery_mode(self):
        """
        Manually reset the martingale recovery state.
        Use this when a trade was incorrectly recorded as a loss.
        """
        self.log("Resetting martingale recovery state...", "WARN")
        self.tracker.martingale.reset()
        self.tracker.save()
        self._sync_martingale_from_tracker()
        self.log("Recovery state reset. Next bet will be a fresh base bet.", "INFO")

    def trigger_manual_recovery(self, loss_dollars: float, bankroll_dollars: float = None):
        """Manually trigger recovery mode - calls EXACT same record_loss() as normal losses."""
        self.log("=" * 60, "WARN")
        self.log("MANUAL RECOVERY TRIGGERED", "WARN")

        # Call the EXACT same function that normal losses use
        loss_cents = int(loss_dollars * 100)
        self.tracker.martingale.record_loss(loss_cents=loss_cents, base_profit_cents=0)
        self.tracker.save()
        self._sync_martingale_from_tracker()

        if bankroll_dollars is not None:
            self.effective_bankroll = bankroll_dollars

        self.log(f"  Loss: ${loss_dollars:.2f} | Next bet: RECOVERY #2", "WARN")
        self.log("=" * 60, "WARN")

    def show_status(self):
        """Print current status."""
        self.refresh_bankroll()
        self._sync_martingale_from_tracker()

        print("\n" + "=" * 50)
        print("CURRENT STATUS")
        print("=" * 50)
        print(f"Bankroll: ${self.state.bankroll:.2f}")
        print(f"Consecutive losses: {self.tracker.martingale.consecutive_losses}")
        print(f"In recovery mode: {self.tracker.martingale.in_recovery}")
        if self.tracker.martingale.in_recovery:
            recovery_target = self.tracker.martingale.get_recovery_target_cents() / 100
            print(f"Recovery target: ${recovery_target:.2f}")
        print(f"Session trades: {self.state.total_trades}")
        print(f"Session P&L: ${self.state.total_profit:+.2f}")
        print()

        # Show next bet info
        next_bet_info = self.tracker.get_next_bet_info()
        print(f"Next bet: #{next_bet_info['bet_number']}")
        if next_bet_info['in_recovery']:
            print(f"  Recovering: ${next_bet_info['recovering_cents']/100:.2f}")
        print()

        # Show martingale sequence
        self.martingale.print_sequence(self.state.bankroll, 85)

    def show_recent_trades(self, count: int = 10):
        """Print recent trades with exact payout details."""
        self.tracker.print_all_trades()

    def get_trade_history(self) -> list:
        """Get all tracked trades for external display."""
        return self.tracker.trades

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
