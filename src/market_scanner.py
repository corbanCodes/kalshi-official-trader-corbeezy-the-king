"""
Real-time market scanner for 15-minute trading windows.
Identifies opportunities in the 80-90 cent range.
"""

import json
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .kalshi_client import KalshiClient, MarketData


@dataclass
class TradingOpportunity:
    """A trading opportunity that meets our criteria."""
    ticker: str
    side: str  # "yes" or "no"
    entry_price: int  # cents
    close_time: datetime
    minutes_remaining: float
    net_profit_per_contract: float  # dollars
    return_percentage: float
    floor_strike: float  # BTC price to beat for settlement

    def __str__(self):
        return (
            f"{self.ticker} | {self.side.upper()} @ {self.entry_price}c | "
            f"{self.minutes_remaining:.1f}m left | "
            f"+{self.net_profit_per_contract*100:.0f}c ({self.return_percentage:.1f}%)"
        )


@dataclass
class OrderBookSnapshot:
    """Snapshot of order book for analysis."""
    ticker: str
    timestamp: datetime
    yes_bid: int
    yes_ask: int
    no_bid: int
    no_ask: int
    spread_yes: int
    spread_no: int
    intended_side: Optional[str] = None
    intended_price: Optional[int] = None
    actual_fill_price: Optional[int] = None


class MarketScanner:
    """
    Scans markets for 15-minute trading opportunities.

    Rules:
    1. Wait 10 minutes into window (5 minutes remaining)
    2. Find YES or NO priced at 80-90 cents
    3. If neither at 80-90, watch for whichever hits 80 first
    """

    # 15-minute BTC market series only
    CRYPTO_15M_SERIES = ["KXBTC15M"]

    def __init__(
        self,
        client: KalshiClient,
        min_price: int = 80,
        max_price: int = 90,
        wait_minutes: int = 10,
        data_dir: Path = None,
    ):
        self.client = client
        self.min_price = min_price
        self.max_price = max_price
        self.wait_minutes = wait_minutes
        self.data_dir = data_dir or Path("./data")
        self.order_book_log: list[OrderBookSnapshot] = []

    @staticmethod
    def calc_fee(price_cents: int) -> float:
        """
        Calculate Kalshi fee per contract.
        Formula: ceil(0.07 × price × (1 - price))
        """
        price = price_cents / 100
        fee = 0.07 * price * (1 - price)
        # Round up to nearest cent
        return max(0.01, round(fee + 0.005, 2))

    @staticmethod
    def calc_net_profit(entry_price_cents: int) -> float:
        """Calculate net profit per contract after fees (in dollars)."""
        price = entry_price_cents / 100
        gross_profit = 1.0 - price
        fee = MarketScanner.calc_fee(entry_price_cents)
        return gross_profit - fee

    @staticmethod
    def calc_return_pct(entry_price_cents: int) -> float:
        """Calculate return percentage after fees."""
        net_profit = MarketScanner.calc_net_profit(entry_price_cents)
        return (net_profit / (entry_price_cents / 100)) * 100

    def is_valid_entry(self, price: int) -> bool:
        """Check if price is in valid entry range (80-90c)."""
        return self.min_price <= price <= self.max_price

    def parse_close_time(self, close_time_str: str) -> datetime:
        """Parse ISO format close time."""
        # Handle various formats
        if close_time_str.endswith("Z"):
            close_time_str = close_time_str[:-1] + "+00:00"
        return datetime.fromisoformat(close_time_str)

    def get_minutes_remaining(self, close_time: datetime) -> float:
        """Get minutes remaining until market closes."""
        now = datetime.now(timezone.utc)
        delta = close_time - now
        return delta.total_seconds() / 60

    def scan_market(self, market: MarketData) -> Optional[TradingOpportunity]:
        """
        Analyze a single market for trading opportunity.

        Returns:
            TradingOpportunity if valid entry found, None otherwise
        """
        if market.status not in ("open", "active"):
            return None

        close_time = self.parse_close_time(market.close_time)
        minutes_remaining = self.get_minutes_remaining(close_time)

        # Rule 1: Must be within last 5 minutes (waited 10 minutes)
        if minutes_remaining > 5 or minutes_remaining < 0.5:
            return None

        # Record order book snapshot
        snapshot = OrderBookSnapshot(
            ticker=market.ticker,
            timestamp=datetime.now(timezone.utc),
            yes_bid=market.yes_bid,
            yes_ask=market.yes_ask,
            no_bid=market.no_bid,
            no_ask=market.no_ask,
            spread_yes=market.yes_ask - market.yes_bid,
            spread_no=market.no_ask - market.no_bid,
        )

        # Check YES side
        if self.is_valid_entry(market.yes_ask):
            snapshot.intended_side = "yes"
            snapshot.intended_price = market.yes_ask
            self.order_book_log.append(snapshot)

            return TradingOpportunity(
                ticker=market.ticker,
                side="yes",
                entry_price=market.yes_ask,
                close_time=close_time,
                minutes_remaining=minutes_remaining,
                net_profit_per_contract=self.calc_net_profit(market.yes_ask),
                return_percentage=self.calc_return_pct(market.yes_ask),
                floor_strike=market.floor_strike,
            )

        # Check NO side
        if self.is_valid_entry(market.no_ask):
            snapshot.intended_side = "no"
            snapshot.intended_price = market.no_ask
            self.order_book_log.append(snapshot)

            return TradingOpportunity(
                ticker=market.ticker,
                side="no",
                entry_price=market.no_ask,
                close_time=close_time,
                minutes_remaining=minutes_remaining,
                net_profit_per_contract=self.calc_net_profit(market.no_ask),
                return_percentage=self.calc_return_pct(market.no_ask),
                floor_strike=market.floor_strike,
            )

        # Neither in range - log for monitoring
        self.order_book_log.append(snapshot)
        return None

    def scan_all_markets(self) -> list[TradingOpportunity]:
        """
        Scan 15-minute crypto markets for opportunities.

        Returns:
            List of valid trading opportunities
        """
        opportunities = []

        # Scan each 15-minute crypto series (BTC, ETH, SOL)
        for series in self.CRYPTO_15M_SERIES:
            try:
                response = self.client.get_markets(
                    status="open",
                    series_ticker=series,
                    limit=50
                )
                markets = response.get("markets", [])

                for market_data in markets:
                    market = MarketData.from_api(market_data)
                    opp = self.scan_market(market)
                    if opp:
                        opportunities.append(opp)

            except Exception as e:
                print(f"Error scanning {series}: {e}")

        return opportunities

    def get_all_crypto_markets(self) -> list[MarketData]:
        """
        Get all 15-minute crypto markets (for dashboard display).

        Returns:
            List of MarketData for all 15M crypto markets
        """
        all_markets = []

        for series in self.CRYPTO_15M_SERIES:
            try:
                response = self.client.get_markets(
                    status="open",
                    series_ticker=series,
                    limit=200  # Get all available windows
                )
                for market_data in response.get("markets", []):
                    all_markets.append(MarketData.from_api(market_data))
            except Exception as e:
                print(f"Error fetching {series}: {e}")

        return all_markets

    def find_best_opportunity(self) -> Optional[TradingOpportunity]:
        """
        Find the best current opportunity.
        Prefers: optimal price range (82-88c) > higher return > more time
        """
        opportunities = self.scan_all_markets()

        if not opportunities:
            return None

        # Sort by: optimal range first, then by return percentage
        def score(opp: TradingOpportunity) -> tuple:
            in_optimal = 82 <= opp.entry_price <= 88
            return (in_optimal, opp.return_percentage, opp.minutes_remaining)

        opportunities.sort(key=score, reverse=True)
        return opportunities[0]

    def watch_for_entry(
        self,
        tickers: list[str] = None,
        poll_interval: float = 1.0,
        timeout_seconds: float = 300,
    ) -> Optional[TradingOpportunity]:
        """
        Watch specific markets until one hits our entry criteria.

        This is for Rule 3: if no 80-90c opportunities exist,
        watch for whichever reaches 80 first.

        Args:
            tickers: Specific tickers to watch (or all if None)
            poll_interval: Seconds between checks
            timeout_seconds: Max time to watch

        Returns:
            First opportunity that meets criteria
        """
        start_time = time.time()

        while time.time() - start_time < timeout_seconds:
            if tickers:
                for ticker in tickers:
                    try:
                        market = self.client.get_market(ticker)
                        opp = self.scan_market(market)
                        if opp:
                            return opp
                    except Exception as e:
                        print(f"Error checking {ticker}: {e}")
            else:
                opp = self.find_best_opportunity()
                if opp:
                    return opp

            time.sleep(poll_interval)

        return None

    def save_order_book_log(self, filename: str = None):
        """Save order book snapshots to JSON for analysis."""
        if not filename:
            filename = f"orderbook_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

        filepath = self.data_dir / filename
        data = [
            {
                "ticker": s.ticker,
                "timestamp": s.timestamp.isoformat(),
                "yes_bid": s.yes_bid,
                "yes_ask": s.yes_ask,
                "no_bid": s.no_bid,
                "no_ask": s.no_ask,
                "spread_yes": s.spread_yes,
                "spread_no": s.spread_no,
                "intended_side": s.intended_side,
                "intended_price": s.intended_price,
                "actual_fill_price": s.actual_fill_price,
            }
            for s in self.order_book_log
        ]

        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)

        return filepath
