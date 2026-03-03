"""
Martingale recovery system.
After a loss, calculate bet size to recover losses + target profit.
"""

import math
from dataclasses import dataclass, field
from typing import Optional

from .market_scanner import MarketScanner


@dataclass
class MartingaleBet:
    """A calculated martingale bet."""
    bet_number: int  # 1 = base, 2 = first recovery, 3 = second recovery
    contracts: int
    cost_dollars: float
    total_risk_dollars: float  # cumulative
    target_profit_dollars: float
    entry_price_cents: int
    net_profit_if_win: float


@dataclass
class MartingaleState:
    """Current state of martingale sequence."""
    consecutive_losses: int = 0
    total_loss_dollars: float = 0.0
    last_bet_cost: float = 0.0
    in_recovery: bool = False
    recovery_bets: list[MartingaleBet] = field(default_factory=list)


class MartingaleCalculator:
    """
    Calculates martingale bet sizes for recovery.

    The strategy:
    - Base bet targets a specific profit (e.g., $1)
    - After loss, next bet recovers all losses + original profit target
    - Max 2 recovery attempts (3 total bets before bust)
    """

    def __init__(
        self,
        target_profit: float = 1.0,
        max_consecutive_losses: int = 2,
        bankroll_bet_percentage: float = 0.03,
    ):
        self.target_profit = target_profit
        self.max_consecutive_losses = max_consecutive_losses
        self.bankroll_bet_percentage = bankroll_bet_percentage
        self.state = MartingaleState()

    def reset(self):
        """Reset martingale state after a win."""
        self.state = MartingaleState()

    def record_loss(self, bet_cost: float):
        """Record a loss and update state."""
        self.state.consecutive_losses += 1
        self.state.total_loss_dollars += bet_cost
        self.state.last_bet_cost = bet_cost
        self.state.in_recovery = True

    def record_win(self):
        """Record a win and reset state."""
        self.reset()

    @property
    def is_bust(self) -> bool:
        """Check if we've exceeded max losses."""
        return self.state.consecutive_losses > self.max_consecutive_losses

    @property
    def current_bet_number(self) -> int:
        """Get current bet number (1 = base, 2+ = recovery)."""
        return self.state.consecutive_losses + 1

    def calculate_base_bet(
        self,
        bankroll: float,
        entry_price_cents: int,
    ) -> MartingaleBet:
        """
        Calculate base bet size (no losses).

        Uses percentage of bankroll for exponential growth.
        """
        net_profit_per_contract = MarketScanner.calc_net_profit(entry_price_cents)
        price_dollars = entry_price_cents / 100

        # Calculate contracts needed for target profit
        contracts_for_target = math.ceil(self.target_profit / net_profit_per_contract)

        # Also consider bankroll percentage
        bankroll_bet = bankroll * self.bankroll_bet_percentage
        contracts_from_bankroll = int(bankroll_bet / price_dollars)

        # Use the smaller of the two (conservative)
        contracts = min(contracts_for_target, max(1, contracts_from_bankroll))

        cost = contracts * price_dollars
        profit_if_win = contracts * net_profit_per_contract

        return MartingaleBet(
            bet_number=1,
            contracts=contracts,
            cost_dollars=cost,
            total_risk_dollars=cost,
            target_profit_dollars=self.target_profit,
            entry_price_cents=entry_price_cents,
            net_profit_if_win=profit_if_win,
        )

    def calculate_recovery_bet(
        self,
        entry_price_cents: int,
        total_loss: float = None,
    ) -> MartingaleBet:
        """
        Calculate recovery bet to recoup losses + target profit.
        """
        if total_loss is None:
            total_loss = self.state.total_loss_dollars

        net_profit_per_contract = MarketScanner.calc_net_profit(entry_price_cents)
        price_dollars = entry_price_cents / 100

        # Need to recover: total_loss + original target profit
        needed_profit = total_loss + self.target_profit

        # Calculate contracts needed
        contracts = math.ceil(needed_profit / net_profit_per_contract)

        cost = contracts * price_dollars
        total_risk = self.state.total_loss_dollars + cost
        profit_if_win = contracts * net_profit_per_contract

        return MartingaleBet(
            bet_number=self.current_bet_number,
            contracts=contracts,
            cost_dollars=cost,
            total_risk_dollars=total_risk,
            target_profit_dollars=self.target_profit,
            entry_price_cents=entry_price_cents,
            net_profit_if_win=profit_if_win,
        )

    def calculate_next_bet(
        self,
        bankroll: float,
        entry_price_cents: int,
    ) -> Optional[MartingaleBet]:
        """
        Calculate the next bet based on current state.

        Returns:
            MartingaleBet or None if bust
        """
        if self.is_bust:
            return None

        if self.state.in_recovery:
            bet = self.calculate_recovery_bet(entry_price_cents)
        else:
            bet = self.calculate_base_bet(bankroll, entry_price_cents)

        # Verify we have enough bankroll
        if bet.cost_dollars > bankroll:
            return None

        return bet

    def calculate_full_sequence(
        self,
        bankroll: float,
        entry_price_cents: int,
    ) -> list[MartingaleBet]:
        """
        Calculate the full martingale sequence for planning.
        Shows all 3 bets (base + 2 recovery).
        """
        sequence = []
        total_risk = 0

        for bet_num in range(1, self.max_consecutive_losses + 2):
            net_profit_per_contract = MarketScanner.calc_net_profit(entry_price_cents)
            price_dollars = entry_price_cents / 100

            if bet_num == 1:
                # Base bet
                needed_profit = self.target_profit
            else:
                # Recovery - need to recover all previous losses + profit
                needed_profit = total_risk + self.target_profit

            contracts = math.ceil(needed_profit / net_profit_per_contract)
            cost = contracts * price_dollars
            total_risk += cost
            profit_if_win = contracts * net_profit_per_contract

            sequence.append(MartingaleBet(
                bet_number=bet_num,
                contracts=contracts,
                cost_dollars=cost,
                total_risk_dollars=total_risk,
                target_profit_dollars=self.target_profit,
                entry_price_cents=entry_price_cents,
                net_profit_if_win=profit_if_win,
            ))

        return sequence

    def calculate_min_bankroll(
        self,
        entry_price_cents: int,
        safety_margin: float = 1.2,  # 20% buffer
    ) -> float:
        """
        Calculate minimum bankroll needed to survive max losses.

        Args:
            entry_price_cents: Expected entry price
            safety_margin: Multiplier for safety buffer

        Returns:
            Minimum bankroll needed in dollars
        """
        # Calculate full sequence at a reasonable bankroll
        sequence = self.calculate_full_sequence(10000, entry_price_cents)

        # Total risk is the cumulative cost of all bets
        total_risk = sequence[-1].total_risk_dollars

        return total_risk * safety_margin

    def can_afford_recovery(self, bankroll: float, entry_price_cents: int) -> bool:
        """Check if we can afford the full recovery sequence."""
        sequence = self.calculate_full_sequence(bankroll, entry_price_cents)
        return sequence[-1].total_risk_dollars <= bankroll

    def print_sequence(self, bankroll: float, entry_price_cents: int):
        """Print the full martingale sequence for visualization."""
        sequence = self.calculate_full_sequence(bankroll, entry_price_cents)

        print(f"\nMartingale Sequence @ {entry_price_cents}c entry:")
        print(f"{'Bet':<15} {'Contracts':<12} {'Cost':<12} {'Total Risk':<12} {'If Win':<12}")
        print("-" * 63)

        for bet in sequence:
            status = "SAFE" if bet.total_risk_dollars <= bankroll else "BUST"
            print(
                f"Bet {bet.bet_number} {'(base)' if bet.bet_number == 1 else '(recovery)':<8} "
                f"{bet.contracts:<12} "
                f"${bet.cost_dollars:<11.2f} "
                f"${bet.total_risk_dollars:<11.2f} "
                f"+${bet.net_profit_if_win:<10.2f} [{status}]"
            )

        print(f"\nBankroll: ${bankroll:.2f}")
        print(f"Can survive 2 losses: {'YES' if self.can_afford_recovery(bankroll, entry_price_cents) else 'NO'}")
