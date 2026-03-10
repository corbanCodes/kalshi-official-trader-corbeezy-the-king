"""
Configuration management for the 15-minute trading strategy.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


@dataclass
class TradingConfig:
    """Trading strategy configuration."""

    # Entry criteria - Base strategy
    min_entry_price: int = 80  # cents
    max_entry_price: int = 92  # cents (changed from 90)
    optimal_min_price: int = 82  # sweet spot
    optimal_max_price: int = 88  # sweet spot

    # Entry criteria - Recovery mode
    recovery_price_cap: int = 87  # cents (allows more opportunities while staying conservative)
    min_btc_distance_pct: float = 0.0  # BTC distance filter disabled for now

    # Timing
    wait_minutes: int = 10  # wait 10 minutes into 15-min window
    window_duration: int = 15  # 15-minute windows

    # Risk management
    max_consecutive_losses: int = 2  # 2 recovery attempts (3 bets total)
    bankroll_bet_percentage: float = 0.03  # 3% of bankroll per base bet

    # Order execution
    limit_order_offset: int = 1  # place limit 1c above ask

    # Martingale
    enable_martingale: bool = True

    # Bankroll management (for multi-bot setup)
    apportioned_bankroll: float = None  # None = use full Kalshi balance
    starting_contracts: int = None  # None = calculate dynamically
    max_base_bet_dollars: float = None  # None = no cap


@dataclass
class KalshiConfig:
    """Kalshi API configuration."""

    api_key_id: str = ""
    private_key_path: str = ""
    private_key_base64: str = ""

    # API endpoints
    base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    demo_url: str = "https://demo-api.kalshi.co/trade-api/v2"

    environment: str = "production"  # or "demo"

    @property
    def api_url(self) -> str:
        if self.environment == "demo":
            return self.demo_url
        return self.base_url


@dataclass
class AppConfig:
    """Main application configuration."""

    trading: TradingConfig
    kalshi: KalshiConfig

    # Paths
    base_dir: Path = Path(__file__).parent.parent
    logs_dir: Path = None
    data_dir: Path = None

    # State
    starting_bankroll: float = 250.0
    target_profit_per_trade: float = 1.0

    def __post_init__(self):
        self.logs_dir = self.base_dir / "logs"
        self.data_dir = self.base_dir / "data"
        self.logs_dir.mkdir(exist_ok=True)
        self.data_dir.mkdir(exist_ok=True)


def load_config() -> AppConfig:
    """Load configuration from environment variables."""

    trading = TradingConfig(
        min_entry_price=int(os.getenv("MIN_ENTRY_PRICE", "80")),
        max_entry_price=int(os.getenv("MAX_ENTRY_PRICE", "92")),
        max_consecutive_losses=int(os.getenv("MAX_CONSECUTIVE_LOSSES", "2")),
        recovery_price_cap=int(os.getenv("RECOVERY_PRICE_CAP", "87")),
        min_btc_distance_pct=float(os.getenv("MIN_BTC_DISTANCE_PCT", "0.0")),
        apportioned_bankroll=float(os.getenv("APPORTIONED_BANKROLL")) if os.getenv("APPORTIONED_BANKROLL") else None,
        starting_contracts=int(os.getenv("STARTING_CONTRACTS")) if os.getenv("STARTING_CONTRACTS") else None,
        max_base_bet_dollars=float(os.getenv("MAX_BASE_BET_DOLLARS")) if os.getenv("MAX_BASE_BET_DOLLARS") else None,
    )

    kalshi = KalshiConfig(
        api_key_id=os.getenv("KALSHI_API_KEY_ID", ""),
        private_key_path=os.getenv("KALSHI_PRIVATE_KEY_PATH", ""),
        private_key_base64=os.getenv("KALSHI_PRIVATE_KEY_BASE64", ""),
        environment=os.getenv("KALSHI_ENV", "production"),
    )

    return AppConfig(
        trading=trading,
        kalshi=kalshi,
        starting_bankroll=float(os.getenv("STARTING_BANKROLL", "250")),
        target_profit_per_trade=float(os.getenv("TARGET_PROFIT_PER_TRADE", "1.0")),
    )
