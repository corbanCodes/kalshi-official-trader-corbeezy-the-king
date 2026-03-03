# 15-Minute Trading Strategy - Official Trader

Automated trader for the 80-90c market sentiment strategy on Kalshi.

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Generate API keys
python main.py setup

# 3. Create .env file (copy from .env.example)
cp .env.example .env
# Edit .env with your API key ID

# 4. Test connection
python main.py test

# 5. Start trading
python main.py run
```

## Commands

| Command | Description |
|---------|-------------|
| `python main.py run` | Start live trading |
| `python main.py paper` | Paper trading mode (no real orders) |
| `python main.py status` | Show current bankroll and state |
| `python main.py setup` | Generate RSA keys for Kalshi API |
| `python main.py test` | Test API connection |
| `python main.py calc` | Interactive martingale calculator |
| `python main.py scan` | Scan markets for current opportunities |

## The Strategy

1. **Wait 10 minutes** into a 15-minute window
2. **Find opportunities** where YES or NO is priced at 80-90 cents
3. **Place limit order** at 1c above ask to ensure fill
4. **Track settlement** and update bankroll
5. **Martingale recovery** on losses (up to 2 consecutive)
6. **Exponential growth** - bet percentage of bankroll

## Configuration

Edit `.env`:

```env
KALSHI_API_KEY_ID=your_key_id
KALSHI_PRIVATE_KEY_PATH=./private_key.pem

STARTING_BANKROLL=250
TARGET_PROFIT_PER_TRADE=1.00
MIN_ENTRY_PRICE=80
MAX_ENTRY_PRICE=90
```

## Risk Warning

- **3 consecutive losses = BUST**
- This has never happened in testing (91% win rate, max 2 in a row)
- Only trade what you can afford to lose
- The bot will stop automatically if bust condition is reached

## Deploy to Railway

```bash
# 1. Install Railway CLI
npm install -g @railway/cli

# 2. Login
railway login

# 3. Create project
railway init

# 4. Add environment variables (in Railway dashboard)
#    KALSHI_API_KEY_ID=your_key_id
#    KALSHI_PRIVATE_KEY_BASE64=<base64 encoded private key>
#    STARTING_BANKROLL=250
#    TARGET_PROFIT_PER_TRADE=1.00

# 5. Deploy
railway up
```

**Getting your base64 private key:**
```bash
# Generate key locally
python main.py setup

# Convert to base64 for Railway
cat private_key.pem | base64
```

Paste the output as `KALSHI_PRIVATE_KEY_BASE64` in Railway's environment variables.

## File Structure

```
official-trader/
├── main.py              # Entry point
├── Procfile             # Railway process definition
├── railway.json         # Railway config
├── nixpacks.toml        # Build config
├── requirements.txt     # Dependencies
├── .env.example         # Config template
├── src/
│   ├── config.py        # Configuration
│   ├── auth.py          # RSA-PSS authentication
│   ├── kalshi_client.py # API client
│   ├── market_scanner.py # Market opportunity scanner
│   ├── martingale.py    # Recovery bet calculator
│   ├── trade_executor.py # Order execution
│   └── trader.py        # Main trading loop
├── data/                # Order book logs, state
└── logs/                # Trade logs
```
