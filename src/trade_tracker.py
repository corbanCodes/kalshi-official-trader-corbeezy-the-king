"""
Trade tracking with exact payout calculations.
Uses Kraken for instant settlement determination.
"""

import json
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List

from .kraken import KrakenClient


@dataclass
class TradeRecord:
    """Complete record of a single trade with exact payouts."""

    # Identity
    trade_id: str
    timestamp: str
    ticker: str

    # Position
    side: str  # "yes" or "no"
    contracts: int
    intended_price: int  # cents - what we wanted
    actual_fill_price: int  # cents - what we got

    # Market data
    floor_strike: float  # BTC price to beat
    close_time: str  # When market closes

    # Financials (all in cents for precision)
    cost_cents: int  # contracts * fill_price
    fee_cents: int  # calculated fee
    bankroll_before_cents: int
    bankroll_after_cents: int = 0

    # Settlement
    settlement_btc_price: float = 0.0
    won: Optional[bool] = None
    gross_payout_cents: int = 0  # 100 * contracts if won, else 0
    net_profit_cents: int = 0

    # Martingale context
    bet_number: int = 1  # 1 = base, 2 = recovery 1, 3 = recovery 2
    recovering_amount_cents: int = 0  # if recovery bet, what we're recovering

    @staticmethod
    def calculate_fee_cents(price_cents: int, contracts: int) -> int:
        """
        Calculate Kalshi fee.
        Formula: ceil(0.07 * price * (1 - price)) per contract, minimum 1 cent
        """
        price = price_cents / 100
        fee_per = 0.07 * price * (1 - price)
        fee_per_cents = max(1, math.ceil(fee_per * 100))
        return fee_per_cents * contracts

    def calculate_settlement(self, btc_price: float) -> None:
        """Calculate settlement based on BTC price."""
        self.settlement_btc_price = btc_price

        # Determine win/loss
        if self.side == "yes":
            self.won = btc_price >= self.floor_strike
        else:
            self.won = btc_price < self.floor_strike

        # Calculate payout
        if self.won:
            self.gross_payout_cents = self.contracts * 100  # $1 per contract
        else:
            self.gross_payout_cents = 0

        # Net profit = payout - cost - fee
        self.net_profit_cents = self.gross_payout_cents - self.cost_cents - self.fee_cents

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TradeRecord":
        """Create from dictionary."""
        return cls(**data)


@dataclass
class MartingaleState:
    """Current martingale sequence state."""
    consecutive_losses: int = 0
    total_loss_cents: int = 0  # Total lost in current sequence
    base_target_profit_cents: int = 0  # Original profit target
    in_recovery: bool = False

    def reset(self):
        """Reset after a win."""
        self.consecutive_losses = 0
        self.total_loss_cents = 0
        self.base_target_profit_cents = 0
        self.in_recovery = False

    def record_loss(self, loss_cents: int, base_profit_cents: int = 0):
        """Record a loss."""
        self.consecutive_losses += 1
        self.total_loss_cents += abs(loss_cents)
        if not self.in_recovery:
            self.base_target_profit_cents = base_profit_cents
        self.in_recovery = True

    def record_win(self):
        """Record a win and reset."""
        self.reset()

    def get_recovery_target_cents(self) -> int:
        """
        Get how much we need to recover.

        LOSS-ONLY RECOVERY: Just need to recover the total loss amount.
        (Changed from loss + original profit target to save money)
        """
        return self.total_loss_cents

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "MartingaleState":
        return cls(**data)


class TradeTracker:
    """
    Tracks all trades with exact payouts and martingale state.
    """

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.trades: List[TradeRecord] = []
        self.martingale = MartingaleState()

        # Ensure data directory exists
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # File paths
        self.trades_file = data_dir / "trade_history.json"
        self.state_file = data_dir / "martingale_state.json"

        # Load existing data
        self._load()

    def _load(self):
        """Load trades and state from disk."""
        if self.trades_file.exists():
            try:
                with open(self.trades_file) as f:
                    data = json.load(f)
                    self.trades = [TradeRecord.from_dict(t) for t in data]
            except Exception as e:
                print(f"Error loading trades: {e}")

        if self.state_file.exists():
            try:
                with open(self.state_file) as f:
                    data = json.load(f)
                    self.martingale = MartingaleState.from_dict(data)
            except Exception as e:
                print(f"Error loading martingale state: {e}")

    def save(self):
        """Save trades and state to disk."""
        with open(self.trades_file, "w") as f:
            json.dump([t.to_dict() for t in self.trades], f, indent=2)

        with open(self.state_file, "w") as f:
            json.dump(self.martingale.to_dict(), f, indent=2)

    def create_trade(
        self,
        ticker: str,
        side: str,
        contracts: int,
        intended_price: int,
        actual_fill_price: int,
        floor_strike: float,
        close_time: str,
        bankroll_cents: int,
    ) -> TradeRecord:
        """Create a new trade record."""
        trade_id = f"{datetime.now().strftime('%H%M%S')}_{ticker[-5:]}"

        cost_cents = contracts * actual_fill_price
        fee_cents = TradeRecord.calculate_fee_cents(actual_fill_price, contracts)

        # Martingale context
        bet_number = self.martingale.consecutive_losses + 1
        recovering = self.martingale.get_recovery_target_cents() if self.martingale.in_recovery else 0

        trade = TradeRecord(
            trade_id=trade_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            ticker=ticker,
            side=side,
            contracts=contracts,
            intended_price=intended_price,
            actual_fill_price=actual_fill_price,
            floor_strike=floor_strike,
            close_time=close_time,
            cost_cents=cost_cents,
            fee_cents=fee_cents,
            bankroll_before_cents=bankroll_cents,
            bet_number=bet_number,
            recovering_amount_cents=recovering,
        )

        self.trades.append(trade)
        return trade

    def settle_trade(self, trade: TradeRecord, btc_price: float, bankroll_after_cents: int) -> None:
        """
        Settle a trade using the BTC price.

        Args:
            trade: The trade to settle
            btc_price: BTC price at settlement time (from Kraken)
            bankroll_after_cents: Actual bankroll after settlement
        """
        # Calculate settlement
        trade.calculate_settlement(btc_price)
        trade.bankroll_after_cents = bankroll_after_cents

        # Update martingale state
        if trade.won:
            self.martingale.record_win()
        else:
            # Calculate what we WOULD have profited if we won (for TRUE martingale recovery)
            # Gross payout if won = contracts * 100 cents
            # Net profit if won = gross_payout - cost - fee
            would_have_won_gross = trade.contracts * 100
            would_have_profited = would_have_won_gross - trade.cost_cents - trade.fee_cents
            base_profit = would_have_profited if trade.bet_number == 1 else 0

            self.martingale.record_loss(
                loss_cents=trade.cost_cents + trade.fee_cents,
                base_profit_cents=base_profit
            )

        self.save()

    def settle_trade_with_kraken(self, trade: TradeRecord, bankroll_after_cents: int) -> bool:
        """
        Settle a trade by fetching current BTC price from Kraken.

        Returns:
            True if settlement was successful
        """
        btc_price = KrakenClient.get_btc_price()

        if btc_price is None:
            print("Could not get BTC price from Kraken")
            return False

        self.settle_trade(trade, btc_price, bankroll_after_cents)
        return True

    def get_next_bet_info(self) -> dict:
        """Get info for the next bet based on martingale state."""
        return {
            "bet_number": self.martingale.consecutive_losses + 1,
            "in_recovery": self.martingale.in_recovery,
            "recovering_cents": self.martingale.get_recovery_target_cents(),
            "consecutive_losses": self.martingale.consecutive_losses,
        }

    def get_recent_trades(self, count: int = 10) -> List[TradeRecord]:
        """Get most recent trades."""
        return self.trades[-count:]

    def print_trade_summary(self, trade: TradeRecord):
        """Print a single trade summary."""
        status = "WIN" if trade.won else "LOSS" if trade.won is False else "PENDING"
        slippage = trade.actual_fill_price - trade.intended_price

        print(f"\n{'='*60}")
        print(f"TRADE: {trade.ticker}")
        print(f"{'='*60}")
        print(f"  Side: {trade.side.upper()}")
        print(f"  Contracts: {trade.contracts}")
        print(f"  Entry: {trade.intended_price}c intended -> {trade.actual_fill_price}c actual ({slippage:+d}c slippage)")
        print(f"  Strike: ${trade.floor_strike:,.2f}")
        print(f"  Cost: ${trade.cost_cents/100:.2f} + ${trade.fee_cents/100:.2f} fee")
        print(f"  Bankroll: ${trade.bankroll_before_cents/100:.2f} -> ${trade.bankroll_after_cents/100:.2f}")

        if trade.won is not None:
            print(f"  Settlement BTC: ${trade.settlement_btc_price:,.2f}")
            print(f"  Result: {status}")
            print(f"  Payout: ${trade.gross_payout_cents/100:.2f}")
            print(f"  Net P&L: ${trade.net_profit_cents/100:+.2f}")

        if trade.bet_number > 1:
            print(f"  [Recovery bet #{trade.bet_number}, recovering ${trade.recovering_amount_cents/100:.2f}]")
        print()

    def print_all_trades(self):
        """Print all trades."""
        print(f"\n{'='*70}")
        print("COMPLETE TRADE HISTORY")
        print(f"{'='*70}")

        total_profit = 0
        wins = 0
        losses = 0

        for trade in self.trades:
            if trade.won is True:
                wins += 1
            elif trade.won is False:
                losses += 1
            total_profit += trade.net_profit_cents

            self.print_trade_summary(trade)

        print(f"{'='*70}")
        print(f"SUMMARY: {wins}W / {losses}L | Net P&L: ${total_profit/100:+.2f}")
        print(f"{'='*70}")
