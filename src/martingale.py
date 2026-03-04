"""
True Martingale System for 15-Minute BTC Strategy.

The strategy:
- Bet a % of bankroll that allows surviving 2 consecutive losses
- After loss, bet enough to recover ALL losses + original profit target
- Max 2 recovery attempts (3 total bets before bust)
- On win, recalculate base bet from new bankroll (compounding)
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
    total_risk_dollars: float  # cumulative across all bets
    entry_price_cents: int
    net_profit_if_win: float


@dataclass
class MartingaleState:
    """Current state of martingale sequence."""
    consecutive_losses: int = 0
    total_loss_dollars: float = 0.0
    base_bet_dollars: float = 0.0  # The original base bet cost
    base_target_profit_dollars: float = 0.0  # The original profit target (FIXED - must be stored!)
    in_recovery: bool = False


class MartingaleCalculator:
    """
    True Martingale Calculator.

    Given a bankroll and entry price, calculates:
    1. Max safe base bet that survives 2 consecutive losses
    2. Recovery bets that recoup all losses + original profit
    """

    def __init__(self, max_consecutive_losses: int = 2):
        self.max_consecutive_losses = max_consecutive_losses
        self.state = MartingaleState()

    def reset(self):
        """Reset after a win."""
        self.state = MartingaleState()

    def record_loss(self, bet_cost: float, target_profit: float = 0.0):
        """
        Record a loss.

        Args:
            bet_cost: Cost of the bet that just lost
            target_profit: The profit we WOULD have made if we won (only needed for first loss)
        """
        self.state.consecutive_losses += 1
        self.state.total_loss_dollars += bet_cost
        if not self.state.in_recovery:
            self.state.base_bet_dollars = bet_cost
            self.state.base_target_profit_dollars = target_profit  # Store the ORIGINAL target!
        self.state.in_recovery = True

    def record_win(self):
        """Record a win and reset."""
        self.reset()

    @property
    def is_bust(self) -> bool:
        """Check if we've exceeded max losses."""
        return self.state.consecutive_losses > self.max_consecutive_losses

    @property
    def current_bet_number(self) -> int:
        """Get current bet number (1 = base, 2+ = recovery)."""
        return self.state.consecutive_losses + 1

    def get_return_multiplier(self, entry_price_cents: int) -> float:
        """
        Get the return multiplier for a given entry price.
        At 85c, you pay $0.85 to win $1.00, so profit = $0.15, return = 15/85 = 17.6%
        """
        net_profit = MarketScanner.calc_net_profit(entry_price_cents)
        price = entry_price_cents / 100
        return net_profit / price

    def calculate_recovery_multiplier(self, entry_price_cents: int) -> float:
        """
        Calculate how much bigger each recovery bet needs to be.

        If return is 15%, you need to bet ~7.6x to recover previous loss + profit.
        Formula: (1 + 1/return_rate)
        """
        return_mult = self.get_return_multiplier(entry_price_cents)
        if return_mult <= 0:
            return float('inf')
        return 1 + (1 / return_mult)

    def calculate_max_base_bet_for_price(self, bankroll: float, entry_price_cents: int) -> float:
        """
        Calculate the maximum base bet for a SPECIFIC price.

        Total risk for 3 bets = base * (1 + R + R²) where R is recovery multiplier
        So base = bankroll / (1 + R + R²)
        """
        R = self.calculate_recovery_multiplier(entry_price_cents)
        total_multiplier = 1 + R + (R * R)
        return bankroll / total_multiplier

    def calculate_max_base_bet(self, bankroll: float, min_price: int = 80, max_price: int = 90) -> float:
        """
        Calculate the maximum base bet that survives 2 losses at ANY price in the range.

        This finds the WORST CASE price and sizes the bet conservatively.
        """
        worst_case_base = float('inf')

        for price in range(min_price, max_price + 1):
            base = self.calculate_max_base_bet_for_price(bankroll, price)
            if base < worst_case_base:
                worst_case_base = base

        return worst_case_base

    def find_max_safe_contracts(self, bankroll: float, min_price: int = 80, max_price: int = 90) -> int:
        """
        Find the maximum base contract count that survives 2 losses at ANY price in range.
        """
        for contracts in range(100, 0, -1):
            all_safe = True
            for price in range(min_price, max_price + 1):
                total_risk = self._calc_total_risk_for_contracts(contracts, price)
                if total_risk > bankroll:
                    all_safe = False
                    break
            if all_safe:
                return contracts
        return 1

    def _calc_total_risk_for_contracts(self, base_contracts: int, price_cents: int) -> float:
        """Calculate total risk for a given base contract count at a specific price."""
        price_dollars = price_cents / 100
        net_profit_per = MarketScanner.calc_net_profit(price_cents)

        base_cost = base_contracts * price_dollars
        base_profit = base_contracts * net_profit_per

        # Recovery 1
        r1_needed = base_cost + base_profit
        r1_contracts = math.ceil(r1_needed / net_profit_per) if net_profit_per > 0 else 1
        r1_cost = r1_contracts * price_dollars

        # Recovery 2
        r2_needed = base_cost + r1_cost + base_profit
        r2_contracts = math.ceil(r2_needed / net_profit_per) if net_profit_per > 0 else 1
        r2_cost = r2_contracts * price_dollars

        return base_cost + r1_cost + r2_cost

    def calculate_base_bet(
        self,
        bankroll: float,
        entry_price_cents: int,
    ) -> MartingaleBet:
        """
        Calculate base bet - uses consistent contract count that survives 2 losses at ANY price 80-90c.
        """
        # Find max safe contracts across entire range
        contracts = self.find_max_safe_contracts(bankroll, min_price=80, max_price=90)

        price_dollars = entry_price_cents / 100
        cost = contracts * price_dollars
        net_profit_per_contract = MarketScanner.calc_net_profit(entry_price_cents)
        profit_if_win = contracts * net_profit_per_contract

        return MartingaleBet(
            bet_number=1,
            contracts=contracts,
            cost_dollars=cost,
            total_risk_dollars=cost,
            entry_price_cents=entry_price_cents,
            net_profit_if_win=profit_if_win,
        )

    def calculate_recovery_bet(
        self,
        entry_price_cents: int,
    ) -> MartingaleBet:
        """
        Calculate recovery bet to recoup ALL losses + original expected profit.

        TRUE MARTINGALE: Recovery = cumulative losses + ORIGINAL profit target
        The profit target is stored when first loss occurs, NOT recalculated!
        """
        net_profit_per_contract = MarketScanner.calc_net_profit(entry_price_cents)
        price_dollars = entry_price_cents / 100

        # Need to recover: all cumulative losses + ORIGINAL profit target (stored, not recalculated!)
        needed_profit = self.state.total_loss_dollars + self.state.base_target_profit_dollars

        # Calculate contracts needed at CURRENT price
        if net_profit_per_contract <= 0:
            contracts = 1
        else:
            contracts = math.ceil(needed_profit / net_profit_per_contract)

        cost = contracts * price_dollars
        total_risk = self.state.total_loss_dollars + cost
        profit_if_win = contracts * net_profit_per_contract

        return MartingaleBet(
            bet_number=self.current_bet_number,
            contracts=contracts,
            cost_dollars=cost,
            total_risk_dollars=total_risk,
            entry_price_cents=entry_price_cents,
            net_profit_if_win=profit_if_win,
        )

    def can_survive_full_range(self, bankroll: float, min_price: int = 80, max_price: int = 90) -> bool:
        """Check if bankroll can survive 2 losses at ANY price in the range."""
        for price in range(min_price, max_price + 1):
            sequence = self.calculate_full_sequence(bankroll, price)
            if sequence[-1].total_risk_dollars > bankroll:
                return False
        return True

    def calculate_next_bet(
        self,
        bankroll: float,
        entry_price_cents: int,
    ) -> Optional[MartingaleBet]:
        """
        Calculate the next bet based on current state.
        Base bet is sized conservatively for worst-case across 80-90c range.
        """
        if self.is_bust:
            return None

        if self.state.in_recovery:
            bet = self.calculate_recovery_bet(entry_price_cents)
        else:
            bet = self.calculate_base_bet(bankroll, entry_price_cents)

        # Verify we have enough bankroll for this specific bet
        if bet.cost_dollars > bankroll:
            return None

        return bet

    def calculate_full_sequence(
        self,
        bankroll: float,
        entry_price_cents: int,
    ) -> list[MartingaleBet]:
        """
        Calculate the full martingale sequence for planning/display.
        Shows base + 2 recovery bets.
        """
        sequence = []
        net_profit_per_contract = MarketScanner.calc_net_profit(entry_price_cents)
        price_dollars = entry_price_cents / 100
        return_mult = self.get_return_multiplier(entry_price_cents)

        # Calculate base bet
        base_dollars = self.calculate_max_base_bet(bankroll, entry_price_cents)
        base_contracts = max(1, int(base_dollars / price_dollars))
        base_cost = base_contracts * price_dollars
        base_profit = base_contracts * net_profit_per_contract

        total_risk = base_cost

        sequence.append(MartingaleBet(
            bet_number=1,
            contracts=base_contracts,
            cost_dollars=base_cost,
            total_risk_dollars=total_risk,
            entry_price_cents=entry_price_cents,
            net_profit_if_win=base_profit,
        ))

        # Calculate recovery bets
        cumulative_loss = base_cost
        original_expected_profit = base_profit

        for bet_num in range(2, self.max_consecutive_losses + 2):
            # Need to recover: cumulative losses + original expected profit
            needed_profit = cumulative_loss + original_expected_profit

            if net_profit_per_contract <= 0:
                contracts = 1
            else:
                contracts = math.ceil(needed_profit / net_profit_per_contract)

            cost = contracts * price_dollars
            total_risk += cost
            profit_if_win = contracts * net_profit_per_contract

            sequence.append(MartingaleBet(
                bet_number=bet_num,
                contracts=contracts,
                cost_dollars=cost,
                total_risk_dollars=total_risk,
                entry_price_cents=entry_price_cents,
                net_profit_if_win=profit_if_win,
            ))

            cumulative_loss += cost

        return sequence

    def print_sequence(self, bankroll: float, entry_price_cents: int):
        """Print the full martingale sequence."""
        sequence = self.calculate_full_sequence(bankroll, entry_price_cents)
        return_pct = self.get_return_multiplier(entry_price_cents) * 100

        print(f"\n{'='*70}")
        print(f"MARTINGALE SEQUENCE @ {entry_price_cents}c ({return_pct:.1f}% return)")
        print(f"Bankroll: ${bankroll:.2f}")
        print(f"{'='*70}")
        print(f"{'Bet':<20} {'Contracts':<12} {'Cost':<12} {'Total Risk':<12} {'If Win':<12}")
        print("-" * 70)

        for bet in sequence:
            status = "OK" if bet.total_risk_dollars <= bankroll else "BUST"
            label = "Base" if bet.bet_number == 1 else f"Recovery {bet.bet_number - 1}"
            print(
                f"{label:<20} "
                f"{bet.contracts:<12} "
                f"${bet.cost_dollars:<11.2f} "
                f"${bet.total_risk_dollars:<11.2f} "
                f"+${bet.net_profit_if_win:<10.2f} [{status}]"
            )

        print("-" * 70)
        can_survive = sequence[-1].total_risk_dollars <= bankroll
        print(f"Can survive 2 losses: {'YES' if can_survive else 'NO'}")
        print(f"Base bet is {sequence[0].cost_dollars / bankroll * 100:.1f}% of bankroll")

    def print_survival_analysis(self, bankroll: float, min_price: int = 80, max_price: int = 90):
        """Print survival analysis for ALL prices in the range."""
        print(f"\n{'='*80}")
        print(f"TRUE MARTINGALE SURVIVAL ANALYSIS - ${bankroll:.2f} bankroll")
        print(f"{'='*80}")
        print(f"{'Price':<8} {'Return%':<10} {'Base':<12} {'R1':<12} {'R2':<12} {'Total Risk':<14} {'Status':<10}")
        print("-" * 80)

        safe_contracts = self.find_max_safe_contracts(bankroll, min_price, max_price)
        worst_case_price = None
        max_total_risk = 0

        for price in range(min_price, max_price + 1):
            net_profit_per = MarketScanner.calc_net_profit(price)
            return_pct = (net_profit_per / (price / 100)) * 100
            price_dollars = price / 100

            # Calculate sequence at this price with safe_contracts
            base_cost = safe_contracts * price_dollars
            base_profit = safe_contracts * net_profit_per

            # Recovery 1
            r1_needed = base_cost + base_profit
            r1_contracts = math.ceil(r1_needed / net_profit_per) if net_profit_per > 0 else 1
            r1_cost = r1_contracts * price_dollars

            # Recovery 2
            r2_needed = base_cost + r1_cost + base_profit
            r2_contracts = math.ceil(r2_needed / net_profit_per) if net_profit_per > 0 else 1
            r2_cost = r2_contracts * price_dollars

            total_risk = base_cost + r1_cost + r2_cost
            status = "OK" if total_risk <= bankroll else "BUST!"

            if total_risk > max_total_risk:
                max_total_risk = total_risk
                worst_case_price = price

            print(
                f"{price}c      "
                f"{return_pct:<10.1f} "
                f"{safe_contracts}@${base_cost:<7.2f} "
                f"{r1_contracts}@${r1_cost:<7.2f} "
                f"{r2_contracts}@${r2_cost:<7.2f} "
                f"${total_risk:<13.2f} "
                f"{status}"
            )

        print("-" * 80)
        print(f"SAFE BASE CONTRACTS: {safe_contracts}")
        print(f"WORST CASE: {worst_case_price}c with ${max_total_risk:.2f} total risk")
        print(f"BUFFER: ${bankroll - max_total_risk:.2f} remaining")
        print(f"{'='*80}")

    def verify_true_martingale(self, base_contracts: int = 3, min_price: int = 80, max_price: int = 90):
        """
        PROOF that TRUE martingale recovery works for ALL price combinations.

        Tests every combination of:
        - Loss at price X (80-90c)
        - Recovery at price Y (80-90c)

        Verifies that recovery profit >= loss + original target profit
        """
        print(f"\n{'='*90}")
        print("TRUE MARTINGALE VERIFICATION - PROVING RECOVERY MATH")
        print(f"{'='*90}")
        print(f"Base contracts: {base_contracts}")
        print()

        all_pass = True
        failures = []

        for loss_price in range(min_price, max_price + 1):
            price_dollars = loss_price / 100
            net_profit_per = MarketScanner.calc_net_profit(loss_price)

            # Calculate what we lose and what we WOULD have profited
            loss_cost = base_contracts * price_dollars
            fee = MarketScanner.calc_fee(loss_price) * base_contracts
            total_loss = loss_cost + fee
            would_have_profit = base_contracts * net_profit_per  # This is the ORIGINAL target

            print(f"LOSS @ {loss_price}c: {base_contracts} contracts")
            print(f"  Cost: ${loss_cost:.2f} + ${fee:.2f} fee = ${total_loss:.2f} total loss")
            print(f"  Would have profited: ${would_have_profit:.2f} (THIS IS THE TARGET TO RECOVER)")
            print(f"  MUST RECOVER: ${total_loss:.2f} + ${would_have_profit:.2f} = ${total_loss + would_have_profit:.2f}")
            print()

            # Now test recovery at every price
            print(f"  {'Recovery@':<12} {'Contracts':<12} {'Cost':<10} {'Profit':<12} {'Needed':<12} {'Status'}")
            print(f"  {'-'*70}")

            for recovery_price in range(min_price, max_price + 1):
                r_price_dollars = recovery_price / 100
                r_net_profit_per = MarketScanner.calc_net_profit(recovery_price)

                # TRUE MARTINGALE: need to recover loss + ORIGINAL target profit
                needed_profit = total_loss + would_have_profit

                # Calculate contracts needed
                r_contracts = math.ceil(needed_profit / r_net_profit_per) if r_net_profit_per > 0 else 999
                r_cost = r_contracts * r_price_dollars
                r_actual_profit = r_contracts * r_net_profit_per

                # Does it actually recover enough?
                recovered = r_actual_profit >= needed_profit - 0.01  # Small float tolerance
                status = "✓ PASS" if recovered else "✗ FAIL"

                if not recovered:
                    all_pass = False
                    failures.append((loss_price, recovery_price, needed_profit, r_actual_profit))

                print(f"  {recovery_price}c          {r_contracts:<12} ${r_cost:<9.2f} ${r_actual_profit:<11.2f} ${needed_profit:<11.2f} {status}")

            print()

        print(f"{'='*90}")
        if all_pass:
            print("✓ ALL RECOVERY SCENARIOS PASS - TRUE MARTINGALE VERIFIED!")
            print("  Every loss at 80-90c can be recovered at any price 80-90c")
        else:
            print(f"✗ {len(failures)} FAILURES:")
            for loss_p, rec_p, needed, got in failures:
                print(f"  Loss@{loss_p}c -> Recovery@{rec_p}c: needed ${needed:.2f}, got ${got:.2f}")
        print(f"{'='*90}")

        return all_pass

    def verify_recovery_sequence(self, bankroll: float = 443.0, base_contracts: int = 3):
        """
        Simulate a FULL loss sequence and verify recovery at each step.

        Shows exactly what happens when:
        1. Base bet loses
        2. Recovery 1 loses
        3. Recovery 2 wins (or busts if insufficient bankroll)
        """
        print(f"\n{'='*90}")
        print("FULL LOSS SEQUENCE SIMULATION")
        print(f"{'='*90}")
        print(f"Starting bankroll: ${bankroll:.2f}")
        print(f"Base contracts: {base_contracts}")
        print()

        # Test worst case: all bets at highest cost (90c)
        test_price = 90
        price_dollars = test_price / 100
        net_profit_per = MarketScanner.calc_net_profit(test_price)

        print(f"Testing at WORST CASE price: {test_price}c")
        print(f"Net profit per contract: ${net_profit_per:.4f}")
        print()

        # Simulate the sequence
        cumulative_loss = 0
        original_target = 0
        remaining_bankroll = bankroll

        for bet_num in range(1, 4):  # Base + 2 recoveries
            if bet_num == 1:
                # Base bet
                contracts = base_contracts
                cost = contracts * price_dollars
                fee = MarketScanner.calc_fee(test_price) * contracts
                total_cost = cost + fee
                profit_if_win = contracts * net_profit_per
                original_target = profit_if_win  # Store original target

                print(f"BET {bet_num} (BASE):")
                print(f"  Contracts: {contracts}")
                print(f"  Cost: ${cost:.2f} + ${fee:.2f} fee = ${total_cost:.2f}")
                print(f"  If WIN: +${profit_if_win:.2f}")
                print(f"  -> LOSES")

                cumulative_loss += total_cost
                remaining_bankroll -= total_cost

            else:
                # Recovery bet
                needed_profit = cumulative_loss + original_target
                contracts = math.ceil(needed_profit / net_profit_per)
                cost = contracts * price_dollars
                fee = MarketScanner.calc_fee(test_price) * contracts
                total_cost = cost + fee
                profit_if_win = contracts * net_profit_per

                print(f"\nBET {bet_num} (RECOVERY {bet_num - 1}):")
                print(f"  Must recover: ${cumulative_loss:.2f} loss + ${original_target:.2f} target = ${needed_profit:.2f}")
                print(f"  Contracts needed: {contracts}")
                print(f"  Cost: ${cost:.2f} + ${fee:.2f} fee = ${total_cost:.2f}")
                print(f"  If WIN: +${profit_if_win:.2f}")

                if cost > remaining_bankroll:
                    print(f"  -> CANNOT AFFORD (need ${cost:.2f}, have ${remaining_bankroll:.2f})")
                    print(f"\n✗ BUST AT BET {bet_num}")
                    return False

                if bet_num == 3:
                    # Final bet - assume it wins
                    print(f"  -> WINS!")
                    net_outcome = profit_if_win - cumulative_loss
                    print(f"\nFINAL RESULT:")
                    print(f"  Total wagered across all bets: ${bankroll - remaining_bankroll + cost:.2f}")
                    print(f"  Recovery profit: ${profit_if_win:.2f}")
                    print(f"  Minus cumulative losses: -${cumulative_loss:.2f}")
                    print(f"  Net outcome: ${net_outcome:+.2f}")

                    if net_outcome >= original_target - 0.01:
                        print(f"\n✓ TRUE MARTINGALE WORKS: Net ${net_outcome:.2f} >= Original target ${original_target:.2f}")
                        return True
                    else:
                        print(f"\n✗ FAILED: Net ${net_outcome:.2f} < Original target ${original_target:.2f}")
                        return False
                else:
                    print(f"  -> LOSES")
                    cumulative_loss += total_cost
                    remaining_bankroll -= total_cost

        print(f"\nCumulative loss after all bets: ${cumulative_loss:.2f}")
        print(f"Remaining bankroll: ${remaining_bankroll:.2f}")
        print(f"{'='*90}")
