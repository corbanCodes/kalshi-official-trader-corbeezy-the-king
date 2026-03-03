#!/usr/bin/env python3
"""
15-Minute Trading Strategy - Main Entry Point

Usage:
    python main.py run          # Start live trading
    python main.py paper        # Paper trading mode
    python main.py status       # Show current status
    python main.py setup        # Generate API keys
    python main.py test         # Test connection
    python main.py calc         # Interactive calculator
"""

import sys
import os

# Add src to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src import (
    Trader,
    load_config,
    generate_key_pair,
    KalshiClient,
    MartingaleCalculator,
    MarketScanner,
)


def cmd_run():
    """Start live trading."""
    print("=" * 60)
    print("  15-MINUTE STRATEGY - LIVE TRADING")
    print("=" * 60)
    print()
    print("WARNING: This will place REAL orders with REAL money!")
    print()

    confirm = input("Type 'START' to begin: ")
    if confirm != "START":
        print("Aborted.")
        return

    trader = Trader()
    trader.run_continuous()


def cmd_paper():
    """Paper trading mode."""
    trader = Trader()
    trader.paper_trade(num_trades=20)


def cmd_status():
    """Show current status."""
    trader = Trader()
    trader.show_status()


def cmd_setup():
    """Generate API keys."""
    print("Generating RSA key pair for Kalshi API...")
    print()

    save_path = input("Save keys to directory [.]: ").strip() or "."

    pub, priv = generate_key_pair(save_path)

    print()
    print("=" * 60)
    print("SETUP INSTRUCTIONS")
    print("=" * 60)
    print()
    print("1. Go to https://kalshi.com/account/api-keys")
    print("2. Click 'Create API Key'")
    print("3. Paste this PUBLIC key:")
    print()
    print(pub)
    print()
    print("4. Copy the API Key ID you receive")
    print("5. Create a .env file with:")
    print()
    print("   KALSHI_API_KEY_ID=your_key_id_here")
    print(f"   KALSHI_PRIVATE_KEY_PATH={save_path}/private_key.pem")
    print()
    print("IMPORTANT: Keep private_key.pem secure!")


def cmd_test():
    """Test API connection."""
    print("Testing Kalshi API connection...")

    try:
        config = load_config()
        client = KalshiClient(config.kalshi)

        # Test exchange status
        status = client.get_exchange_status()
        print(f"Exchange status: {status}")

        # Test balance
        balance = client.get_balance_dollars()
        print(f"Account balance: ${balance:.2f}")

        # Test markets
        markets = client.get_markets(limit=5)
        print(f"Found {len(markets.get('markets', []))} markets")

        print("\nConnection successful!")

    except Exception as e:
        print(f"Connection failed: {e}")
        print("\nCheck your .env file and API keys.")


def cmd_calc():
    """Interactive martingale calculator."""
    print("=" * 60)
    print("  MARTINGALE CALCULATOR")
    print("=" * 60)
    print()

    while True:
        try:
            bankroll = float(input("Bankroll ($): ") or "250")
            entry_price = int(input("Entry price (cents, 80-90): ") or "85")
            target = float(input("Target profit per trade ($): ") or "1.0")

            calc = MartingaleCalculator(target_profit=target)
            calc.print_sequence(bankroll, entry_price)

            # Show fee breakdown
            print(f"\nFee at {entry_price}c: ${MarketScanner.calc_fee(entry_price):.2f}")
            print(f"Net profit per contract: ${MarketScanner.calc_net_profit(entry_price):.2f}")
            print(f"Return per win: {MarketScanner.calc_return_pct(entry_price):.1f}%")

            print()
            again = input("Calculate again? [y/N]: ").lower()
            if again != "y":
                break

        except ValueError:
            print("Invalid input, try again.")
        except KeyboardInterrupt:
            break


def cmd_scan():
    """Scan current market opportunities."""
    print("Scanning markets for opportunities...")

    config = load_config()
    client = KalshiClient(config.kalshi)
    scanner = MarketScanner(client)

    opportunities = scanner.scan_all_markets()

    if not opportunities:
        print("No opportunities found in 80-90c range")
        return

    print(f"\nFound {len(opportunities)} opportunities:\n")
    for opp in opportunities[:10]:
        print(f"  {opp}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1].lower()

    commands = {
        "run": cmd_run,
        "paper": cmd_paper,
        "status": cmd_status,
        "setup": cmd_setup,
        "test": cmd_test,
        "calc": cmd_calc,
        "scan": cmd_scan,
    }

    if cmd in commands:
        commands[cmd]()
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
