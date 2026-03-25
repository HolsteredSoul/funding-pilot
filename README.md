# AU-Funding-Arb v1.0

Delta-neutral funding rate arbitrage bot for Australian users. Earns passive income (10-30% APY target) by collecting positive perpetual funding rates while maintaining zero directional exposure.

## Strategy

**Long spot + Short perp (1× leverage) on the same exchange.**

When perpetual futures trade at a premium to spot, longs pay shorts a "funding rate" every 8 hours. This bot:

1. **Scans** all USDT perps on Bybit (or OKX) every 60 minutes
2. **Enters** pairs where funding is consistently profitable after fees
3. **Monitors** position health every 30 seconds via WebSocket
4. **Exits** when funding decays, basis diverges, or stop-loss triggers
5. **Logs** every trade in AUD using official RBA exchange rates

## Quick Start

```bash
# 1. Clone and configure
git clone https://github.com/HolsteredSoul/funding-pilot.git
cd funding-pilot
cp .env.example .env
# Edit .env with your API keys

# 2. Install dependencies
pip install -r requirements.txt

# 3. Dry run (reads live data, places no orders)
ARB_DRY_RUN=true python -m src.main

# 4. Run backtest on sample data
python -m backtest.backtest
```

## Docker Deployment (Recommended)

```bash
# Build and run
docker compose up -d

# View logs
docker compose logs -f

# Graceful shutdown (persists positions, does NOT close hedges)
docker compose down

# systemd (for VPS)
sudo cp systemd/funding-arb.service /etc/systemd/system/
sudo systemctl enable --now funding-arb
```

### Recommended VPS

- **Vultr Sydney** (High Frequency, $6/mo) or **AWS Lightsail Sydney** ($5/mo)
- Sydney region for lowest latency to Bybit/OKX APAC servers
- 1 vCPU / 1GB RAM is sufficient

## Configuration

All settings are in `.env` with `ARB_` prefix. Key parameters:

| Variable | Default | Description |
|---|---|---|
| `ARB_EXCHANGE` | `bybit` | Exchange: `bybit` or `okx` |
| `ARB_DRY_RUN` | `true` | Read-only mode (no orders) |
| `ARB_TESTNET` | `false` | Use exchange sandbox |
| `ARB_POSITION_SIZE_USD` | `1000` | USD per hedge pair |
| `ARB_MAX_CONCURRENT_PAIRS` | `5` | Max simultaneous hedges |
| `ARB_MIN_CURRENT_FUNDING` | `0.0003` | Min 0.03% to enter |
| `ARB_FUNDING_FLOOR` | `0.0` | Exit below 0% (cheaper to sit than churn) |
| `ARB_MIN_HOLD_PERIODS` | `21` | 7 days (21×8h) before floor exit applies |
| `ARB_HARD_STOP_LOSS_PCT` | `-0.02` | -2% stop loss |
| `ARB_CIRCUIT_BREAKER_PCT` | `0.05` | 5% drawdown halts all |

See `.env.example` for the complete list.

## Execution & Fee Strategy

The bot prefers **limit orders** (maker fees) over market orders:

- **Spot buy**: limit at best bid + 1 tick (sits near top of book)
- **Perp short**: limit at best ask - 1 tick
- **Timeout**: if limits don't fill within 30s, falls back to market orders
- **Bybit VIP-0 fees**: spot maker 0.10%, perp maker 0.02%
- **Round-trip cost** (4 legs at maker): ~0.24%

At 0.03% funding per 8h and 8 expected holding periods, gross return is 0.24% — matching the fees. The bot only enters when `(trailing_avg × periods) - fees > 0`.

### Taker Fee Tracking

When limit orders fail and the bot falls back to market (taker) orders, the actual fill type is tracked on each position. Tax logging uses the correct fee rate (maker or taker) so `trades_aud.csv` accurately reflects costs — preventing overstated profit and incorrect ATO tax liability.

### Anti-Churn Protection

Frequent entries and exits ("churning") destroy profitability because round-trip fees (0.24%-0.45%) can exceed funding income on short holds. The bot defends against this with:

1. **Higher entry threshold** (0.03%): only enters when funding justifies the execution drag
2. **Zero exit floor** (0.0%): it's cheaper to sit in a stagnant position than to pay 0.15% to exit and 0.15% to re-enter elsewhere
3. **Minimum hold period** (21 epochs / 7 days): the funding decay exit cannot fire until the position has had enough runway to pay off its own execution costs

Safety exits (hard stop loss, circuit breaker, 24h negative funding streak) are **never gated** — they always fire regardless of hold time.

## Dry Run Mode

`ARB_DRY_RUN=true` (the default) connects to real production APIs, reads live market data, scans funding rates, and logs exactly what it would do — but **never submits orders**. Use this to:

- Verify API connectivity
- Check which pairs the scanner would select
- Test tax logging and Telegram alerts
- Validate the bot before going live

This is distinct from `ARB_TESTNET=true`, which connects to the exchange's sandbox API for integration testing with fake orders.

## SIGTERM / Resume Behaviour

On `SIGTERM` (or `docker compose down`):

1. All open positions are **persisted** to `data/positions.json`
2. Hedges are **NOT closed** — they remain open on the exchange
3. The bot exits cleanly

On next startup:

1. `positions.json` is loaded
2. Each position is **reconciled** against the exchange (verifies both legs still exist)
3. Health monitoring resumes immediately
4. New entry scanning begins after the first scan interval

This means you can safely restart the bot without losing track of positions.

## ATO Tax Compliance

**This is assessable business income per ATO crypto guidance.**

### How It Works

- Every trade, funding payment, and fee is logged to `data/trades_aud.csv`
- USD values are converted to AUD using the **official RBA daily exchange rate**
- The RBA publishes rates at ~4pm AEST daily at [rba.gov.au](https://www.rba.gov.au/statistics/tables/)
- Weekends/holidays use the last available rate (forward-fill)
- Both UTC and AWST timestamps are recorded

### RBA Rate

The bot fetches the RBA F11 statistical table (AUD/USD exchange rate). This is the same rate the ATO accepts for foreign currency conversions. The rate represents how many USD one AUD buys (e.g., 0.6500 means 1 AUD = 0.65 USD).

**Conversion**: `AUD value = USD value ÷ AUD/USD rate`

### Koinly Export

```bash
# Generate Koinly-compatible CSV
python -m src.tax_export --koinly

# Annual tax summary
python -m src.tax_export --summary --year 2026
```

The Koinly CSV maps:
- **Spot buys/sells** → standard buy/sell transactions
- **Perp open/close** → margin trades
- **Funding payments** → "other income"
- **Fees** → cost basis

### Deductible Expenses

Per ATO guidance, you can deduct:
- VPS hosting costs ($5-6/month)
- Electricity costs (proportional)
- Exchange API subscription costs
- Internet costs (proportional)
- Software/tool subscriptions used for the bot

Keep receipts. Consult a tax professional for your specific situation.

## Telegram Alerts

Set `ARB_TELEGRAM_TOKEN` and `ARB_TELEGRAM_CHAT_ID` to receive:

- **7am AWST daily recap**: total PnL, funding collected, active pairs, equity
- **Instant alerts**: hedge open/close, health warnings, circuit breaker
- **Startup/shutdown**: confirmation with position count

To get your chat ID, message [@userinfobot](https://t.me/userinfobot) on Telegram.

## Backtest

```bash
# Default: BTC/ETH/SOL over 30 days
python -m backtest.backtest

# Custom parameters
python -m backtest.backtest --min-funding 0.0003 --holding-periods 6 --position-size 500

# Adjust exit behaviour
python -m backtest.backtest --funding-floor 0.0001 --min-hold-periods 10

# Stress test with taker fees and slippage (realistic worst-case)
python -m backtest.backtest --taker-pct 0.5 --slippage 0.0005

# Custom data directory
python -m backtest.backtest --data-dir path/to/csvs --pairs BTC ETH DOGE
```

| Flag | Default | Description |
|---|---|---|
| `--min-funding` | `0.0003` | Min funding rate to enter (0.03%) |
| `--funding-floor` | `0.0` | Exit when trailing avg falls below this |
| `--min-hold-periods` | `21` | Min 8h periods before floor exit applies |
| `--holding-periods` | `8` | Expected holding periods for entry calc |
| `--position-size` | `1000` | USD per position |
| `--taker-pct` | `0.0` | Fraction of legs assumed to fill as taker (0.0-1.0) |
| `--slippage` | `0.0` | Additional slippage per taker leg |

Sample data in `backtest/data/` contains realistic synthetic 8h funding rates. For real historical data, use [CoinGlass](https://www.coinglass.com/) (note: free tier has rate limits).

### CSV Format

```csv
timestamp,funding_rate
2026-02-19 00:00:00,0.000350
2026-02-19 08:00:00,0.000280
```

## Architecture

Three independent async loops sharing a `PositionManager`:

```
┌──────────────────────┐  ┌──────────────────────┐  ┌──────────────────────┐
│  Funding Scanner     │  │  Health Monitor      │  │  Settlement/Exit     │
│  (every 60 min)      │  │  (every 30s)         │  │  (every 8h aligned)  │
│                      │  │                      │  │                      │
│  Scan → Filter →     │  │  WS streams →        │  │  Eval exits →        │
│  Profitability →     │  │  REST fallback →     │  │  Close hedges →      │
│  Open hedges         │  │  Circuit breaker     │  │  Record funding      │
└──────────┬───────────┘  └──────────┬───────────┘  └──────────┬───────────┘
           │                         │                         │
           └─────────────┬───────────┘─────────────────────────┘
                         │
              ┌──────────▼──────────┐
              │   PositionManager   │
              │   (asyncio.Lock)    │
              └─────────────────────┘
```

## Project Structure

```
funding-pilot/
├── requirements.txt          # Python dependencies
├── .env.example              # Configuration template
├── Dockerfile                # Multi-stage, non-root
├── docker-compose.yml        # Production deployment
├── src/
│   ├── config.py             # Pydantic Settings
│   ├── models.py             # Data models
│   ├── main.py               # Entry point, 3 async loops
│   ├── exchange_client.py    # ccxt + WebSocket
│   ├── arb_engine.py         # Strategy logic
│   ├── order_executor.py     # Atomic hedge execution
│   ├── position_manager.py   # State persistence
│   ├── tax_logger.py         # RBA rates + AUD CSV
│   ├── tax_export.py         # Koinly export CLI
│   └── telegram_bot.py       # Alerts
├── backtest/
│   ├── backtest.py           # Historical simulator
│   └── data/                 # Sample funding CSVs
└── systemd/
    └── funding-arb.service   # systemd unit file
```

## Disclaimer

This software is provided as-is for educational and personal use. Cryptocurrency trading involves significant risk. Past funding rates do not guarantee future returns. The authors are not financial advisors. Consult a qualified tax professional for ATO compliance advice specific to your situation.
