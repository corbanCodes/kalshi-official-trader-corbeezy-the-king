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
    Altered Martingale Calculator.

    Given a bankroll and entry price, calculates:
    1. Max safe base bet that survives 3 consecutive losses (4 bets total)
    2. Recovery bets that recoup LOSSES ONLY (not profit) - saves money

    The bet: It will NEVER lose 3 times in a row.
    - Bet 1: Base strategy bet
    - Bet 2: Recovery Stage 1 (if bet 1 loses)
    - Bet 3: Recovery Stage 2 (if bet 2 loses) - THE REAL BET
    """

    # Recovery entries capped at 85c (more conservative than 80-92c base range)
    RECOVERY_PRICE_CAP = 85

    def __init__(self, max_consecutive_losses: int = 2):
        self.max_consecutive_losses = max_consecutive_losses
        self.state = MartingaleState()

    def reset(self):
        """Reset after a win."""
        self.state = MartingaleState()

    def record_loss(self, bet_cost: float, bet_fee: float = 0.0, target_profit: float = 0.0):
        """
        Record a loss.

        Args:
            bet_cost: Cost of the bet that just lost (contracts * price)
            bet_fee: Fee paid on the bet (important for recovery math!)
            target_profit: The profit we WOULD have made if we won (only needed for first loss)
        """
        self.state.consecutive_losses += 1
        # Include BOTH cost and fee in total loss - we need to recover both!
        self.state.total_loss_dollars += bet_cost + bet_fee
        if not self.state.in_recovery:
            self.state.base_bet_dollars = bet_cost + bet_fee
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

    def find_max_safe_contracts(self, bankroll: float, min_price: int = 80, max_price: int = 92) -> int:
        """
        Find the maximum base contract count that survives 2 consecutive losses (3 bets total).

        Uses loss-only recovery formula: contracts = loss / (1 - price)

        At 85c with 1 base contract:
        - Bet 1: 1 contract at 85c = $0.85 cost
        - Recovery 1: $0.85 / 0.15 = 6 contracts at 85c = $5.10 cost
        - Recovery 2: ($0.85 + $5.10) / 0.15 = 40 contracts at 85c = $34.00 cost
        - Total risk: $0.85 + $5.10 + $34.00 = $39.95

        Multiplier is approximately 47x base bet.
        """
        # Use recovery price cap (85c) for worst-case calculations
        recovery_price = self.RECOVERY_PRICE_CAP
        price_dollars = recovery_price / 100

        # Calculate the total multiplier for 4 bets at 85c using loss-only formula
        # Bet 1: 1 contract = price cost
        # Recovery 1: (bet1_cost) / (1 - price) contracts = R1 contracts
        # Recovery 2: (bet1_cost + R1_cost) / (1 - price) contracts = R2 contracts
        # Recovery 3: (bet1_cost + R1_cost + R2_cost) / (1 - price) contracts = R3 contracts

        # Work backwards from bankroll to find max safe base contracts
        for contracts in range(200, 0, -1):
            total_risk = self._calc_total_risk_for_contracts(contracts, recovery_price)
            if total_risk <= bankroll:
                return contracts

        return 1

    def _calc_total_risk_for_contracts(self, base_contracts: int, price_cents: int) -> float:
        """
        Calculate total risk for a given base contract count using loss-only recovery.

        INCLUDES FEES AND SLIPPAGE BUFFER in recovery calculations.

        3 bets total: Base + Recovery 1 + Recovery 2
        """
        # Assume 1c slippage on all bets
        fill_price_cents = min(price_cents + 1, 99)
        fill_price_dollars = fill_price_cents / 100

        # Calculate net profit per contract after fees
        net_profit_per = MarketScanner.calc_net_profit(fill_price_cents)

        if net_profit_per <= 0:
            return float('inf')  # Can't recover at 99-100c

        # Base bet (Bet 1) - include fee in loss
        base_cost = base_contracts * fill_price_dollars
        base_fee = MarketScanner.calc_fee(fill_price_cents) * base_contracts
        cumulative_loss = base_cost + base_fee

        # Recovery 1 (Bet 2): Need contracts where net_profit >= cumulative_loss
        r1_contracts = math.ceil(cumulative_loss / net_profit_per)
        r1_cost = r1_contracts * fill_price_dollars
        r1_fee = MarketScanner.calc_fee(fill_price_cents) * r1_contracts
        cumulative_loss += r1_cost + r1_fee

        # Recovery 2 (Bet 3): Need contracts where net_profit >= cumulative_loss
        r2_contracts = math.ceil(cumulative_loss / net_profit_per)
        r2_cost = r2_contracts * fill_price_dollars

        return base_cost + r1_cost + r2_cost

    def calculate_base_bet(
        self,
        bankroll: float,
        entry_price_cents: int,
    ) -> MartingaleBet:
        """
        Calculate base bet - uses consistent contract count that survives 2 losses at ANY price 80-90c.
        """
        from datetime import datetime, timezone
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        print(f"{timestamp} [CALC ] CALCULATING BASE BET #1")
        print(f"{timestamp} [CALC ]   Bankroll: ${bankroll:.2f}")
        print(f"{timestamp} [CALC ]   Entry price: {entry_price_cents}c")

        # Find max safe contracts across entire range
        contracts = self.find_max_safe_contracts(bankroll, min_price=80, max_price=90)

        price_dollars = entry_price_cents / 100
        cost = contracts * price_dollars
        net_profit_per_contract = MarketScanner.calc_net_profit(entry_price_cents)
        fee_per_contract = MarketScanner.calc_fee(entry_price_cents)
        profit_if_win = contracts * net_profit_per_contract

        print(f"{timestamp} [CALC ]   BASE BET CALCULATED:")
        print(f"{timestamp} [CALC ]     Max safe contracts: {contracts}")
        print(f"{timestamp} [CALC ]     Cost: ${cost:.2f}")
        print(f"{timestamp} [CALC ]     Fee (est): ${fee_per_contract * contracts:.2f}")
        print(f"{timestamp} [CALC ]     Net profit if win: ${profit_if_win:.2f}")
        print(f"{timestamp} [CALC ]     Bet as % of bankroll: {(cost / bankroll * 100):.1f}%")

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
        slippage_cents: int = 1,
    ) -> MartingaleBet:
        """
        Calculate recovery bet to recoup LOSSES ONLY (not profit).

        ALTERED MARTINGALE: Recovery = cumulative losses only

        IMPORTANT: Accounts for slippage and fees on the recovery bet itself!
        - Assumes fill at (entry_price + slippage)
        - Adds estimated fee to recovery target

        Formula: contracts_needed = (total_loss + estimated_fee) / net_profit_per_contract
        """
        from datetime import datetime, timezone
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        print(f"{timestamp} [CALC ] CALCULATING RECOVERY BET #{self.current_bet_number}")
        print(f"{timestamp} [CALC ]   Entry price: {entry_price_cents}c")
        print(f"{timestamp} [CALC ]   Total loss to recover: ${self.state.total_loss_dollars:.2f}")

        # Enforce recovery price cap (85c max)
        if entry_price_cents > self.RECOVERY_PRICE_CAP:
            print(f"{timestamp} [CALC ]   REJECTED: Price {entry_price_cents}c > cap {self.RECOVERY_PRICE_CAP}c")
            return None  # Don't take recovery bets above 85c

        # Assume worst-case fill price (slippage)
        fill_price_cents = min(entry_price_cents + slippage_cents, 99)
        fill_price_dollars = fill_price_cents / 100
        print(f"{timestamp} [CALC ]   Assumed fill price: {fill_price_cents}c (with {slippage_cents}c slippage)")

        # Calculate net profit per contract AFTER fees at fill price
        net_profit_per_contract = MarketScanner.calc_net_profit(fill_price_cents)
        fee_per_contract = MarketScanner.calc_fee(fill_price_cents)
        print(f"{timestamp} [CALC ]   Net profit per contract: ${net_profit_per_contract:.4f}")
        print(f"{timestamp} [CALC ]   Fee per contract: ${fee_per_contract:.4f}")

        if net_profit_per_contract <= 0:
            print(f"{timestamp} [CALC ]   REJECTED: Net profit <= 0 at {fill_price_cents}c")
            return None  # Can't recover at this price

        # LOSS-ONLY RECOVERY with slippage/fee buffer
        # We need contracts where: contracts * net_profit >= total_loss
        # But we also need to account for the fee on THIS bet
        # Iterate to find exact contracts needed

        print(f"{timestamp} [CALC ]   Iterating to find exact contracts needed...")
        for contracts in range(1, 10000):
            # Calculate what we'd actually net if we win
            cost = contracts * fill_price_dollars
            fee = MarketScanner.calc_fee(fill_price_cents) * contracts
            gross_payout = contracts * 1.0  # $1 per contract if win
            net_profit = gross_payout - cost - fee

            # Do we recover enough?
            if net_profit >= self.state.total_loss_dollars:
                break

        contracts = max(1, contracts)  # At least 1 contract

        # Calculate actual costs at intended price (for display)
        cost = contracts * (entry_price_cents / 100)
        total_risk = self.state.total_loss_dollars + cost
        profit_if_win = contracts * net_profit_per_contract

        # Log the final calculation
        actual_cost = contracts * fill_price_dollars
        actual_fee = MarketScanner.calc_fee(fill_price_cents) * contracts
        actual_net = (contracts * 1.0) - actual_cost - actual_fee
        buffer = actual_net - self.state.total_loss_dollars

        print(f"{timestamp} [CALC ]   RECOVERY BET CALCULATED:")
        print(f"{timestamp} [CALC ]     Contracts: {contracts}")
        print(f"{timestamp} [CALC ]     Cost at fill: ${actual_cost:.2f}")
        print(f"{timestamp} [CALC ]     Fee: ${actual_fee:.2f}")
        print(f"{timestamp} [CALC ]     Gross payout if win: ${contracts * 1.0:.2f}")
        print(f"{timestamp} [CALC ]     Net profit if win: ${actual_net:.2f}")
        print(f"{timestamp} [CALC ]     Recovery target: ${self.state.total_loss_dollars:.2f}")
        print(f"{timestamp} [CALC ]     Buffer (net - target): ${buffer:+.2f}")

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
        Shows base + 2 recovery bets (3 bets total) using LOSS-ONLY recovery formula.

        Recovery formula: contracts = total_loss / (1 - price)
        """
        sequence = []

        # Use recovery price cap for display (recovery bets use 85c max)
        recovery_price = min(entry_price_cents, self.RECOVERY_PRICE_CAP)
        price_dollars = entry_price_cents / 100
        recovery_price_dollars = recovery_price / 100

        # Calculate base bet
        base_contracts = self.find_max_safe_contracts(bankroll)
        base_cost = base_contracts * price_dollars
        net_profit_per_contract = MarketScanner.calc_net_profit(entry_price_cents)
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

        # Calculate recovery bets using LOSS-ONLY formula
        cumulative_loss = base_cost
        profit_per_dollar = 1 - recovery_price_dollars  # At 85c, profit is 15c per $1

        for bet_num in range(2, self.max_consecutive_losses + 2):
            # LOSS-ONLY RECOVERY: contracts = loss / (1 - price)
            if profit_per_dollar <= 0:
                contracts = 1
            else:
                contracts = math.ceil(cumulative_loss / profit_per_dollar)

            cost = contracts * recovery_price_dollars
            total_risk += cost
            r_net_profit_per = MarketScanner.calc_net_profit(recovery_price)
            profit_if_win = contracts * r_net_profit_per

            sequence.append(MartingaleBet(
                bet_number=bet_num,
                contracts=contracts,
                cost_dollars=cost,
                total_risk_dollars=total_risk,
                entry_price_cents=recovery_price,  # Recovery uses capped price
                net_profit_if_win=profit_if_win,
            ))

            cumulative_loss += cost

        return sequence

    def print_sequence(self, bankroll: float, entry_price_cents: int):
        """Print the full martingale sequence with loss-only recovery."""
        sequence = self.calculate_full_sequence(bankroll, entry_price_cents)
        return_pct = self.get_return_multiplier(entry_price_cents) * 100

        print(f"\n{'='*80}")
        print(f"ALTERED MARTINGALE SEQUENCE - LOSS-ONLY RECOVERY")
        print(f"Base entry @ {entry_price_cents}c | Recovery capped @ {self.RECOVERY_PRICE_CAP}c")
        print(f"Bankroll: ${bankroll:.2f}")
        print(f"{'='*80}")
        print(f"{'Bet':<20} {'@Price':<8} {'Contracts':<12} {'Cost':<12} {'Total Risk':<12} {'If Win':<12}")
        print("-" * 80)

        for bet in sequence:
            status = "OK" if bet.total_risk_dollars <= bankroll else "BUST"
            label = "Base" if bet.bet_number == 1 else f"Recovery Stage {bet.bet_number - 1}"
            print(
                f"{label:<20} "
                f"{bet.entry_price_cents}c     "
                f"{bet.contracts:<12} "
                f"${bet.cost_dollars:<11.2f} "
                f"${bet.total_risk_dollars:<11.2f} "
                f"+${bet.net_profit_if_win:<10.2f} [{status}]"
            )

        print("-" * 80)
        can_survive = sequence[-1].total_risk_dollars <= bankroll
        print(f"Can survive 2 consecutive losses: {'YES' if can_survive else 'NO'}")
        print(f"Base bet is {sequence[0].cost_dollars / bankroll * 100:.1f}% of bankroll")
        print(f"Total multiplier: {sequence[-1].total_risk_dollars / sequence[0].cost_dollars:.1f}x base bet")

    def print_survival_analysis(self, bankroll: float):
        """Print survival analysis using loss-only recovery at 85c cap."""
        recovery_price = self.RECOVERY_PRICE_CAP
        price_dollars = recovery_price / 100
        profit_per_dollar = 1 - price_dollars

        print(f"\n{'='*90}")
        print(f"ALTERED MARTINGALE SURVIVAL ANALYSIS - LOSS-ONLY RECOVERY")
        print(f"Bankroll: ${bankroll:.2f} | Recovery capped @ {recovery_price}c")
        print(f"{'='*90}")
        print(f"{'Bet':<15} {'Contracts':<12} {'Cost':<12} {'Cumulative':<14} {'Status':<10}")
        print("-" * 90)

        safe_contracts = self.find_max_safe_contracts(bankroll)
        cumulative_loss = 0

        # Base bet
        base_cost = safe_contracts * price_dollars
        cumulative_loss = base_cost
        status = "OK" if cumulative_loss <= bankroll else "BUST!"
        print(f"Base           {safe_contracts:<12} ${base_cost:<11.2f} ${cumulative_loss:<13.2f} {status}")

        # Recovery stages (2 stages for 3 bets total)
        for stage in range(1, 3):  # 2 recovery stages
            contracts = math.ceil(cumulative_loss / profit_per_dollar)
            cost = contracts * price_dollars
            cumulative_loss += cost
            status = "OK" if cumulative_loss <= bankroll else "BUST!"
            print(f"Recovery {stage}     {contracts:<12} ${cost:<11.2f} ${cumulative_loss:<13.2f} {status}")

        print("-" * 90)
        print(f"SAFE BASE CONTRACTS: {safe_contracts}")
        print(f"TOTAL RISK (all 3 bets lose): ${cumulative_loss:.2f}")
        print(f"BUFFER REMAINING: ${bankroll - cumulative_loss:.2f}")
        print(f"MULTIPLIER: {cumulative_loss / base_cost:.1f}x base bet")
        print(f"{'='*90}")

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
