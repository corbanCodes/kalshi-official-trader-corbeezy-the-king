"""
Kraken API for BTC price data.
Used for instant settlement determination instead of waiting for Kalshi API.
"""

import requests
from typing import Optional


class KrakenClient:
    """Simple client to fetch BTC price from Kraken."""

    BASE_URL = "https://api.kraken.com/0/public"

    @staticmethod
    def get_btc_price() -> Optional[float]:
        """
        Get current BTC/USD price from Kraken.

        Returns:
            Current BTC price in USD, or None if error
        """
        try:
            response = requests.get(
                f"{KrakenClient.BASE_URL}/Ticker",
                params={"pair": "XBTUSD"},
                timeout=5
            )
            response.raise_for_status()
            data = response.json()

            if data.get("error"):
                print(f"Kraken API error: {data['error']}")
                return None

            result = data.get("result", {})
            ticker = result.get("XXBTZUSD", {})

            # 'c' is the last trade closed [price, lot volume]
            last_price = ticker.get("c", [None])[0]

            if last_price:
                return float(last_price)
            return None

        except Exception as e:
            print(f"Error fetching Kraken price: {e}")
            return None

    @staticmethod
    def determine_settlement(
        floor_strike: float,
        side: str,
    ) -> Optional[bool]:
        """
        Determine if a trade won based on current BTC price.

        Args:
            floor_strike: The strike price (price to beat)
            side: "yes" or "no"

        Returns:
            True if won, False if lost, None if couldn't get price
        """
        btc_price = KrakenClient.get_btc_price()

        if btc_price is None:
            return None

        # YES wins if BTC >= strike
        # NO wins if BTC < strike
        if side == "yes":
            won = btc_price >= floor_strike
        else:
            won = btc_price < floor_strike

        return won
