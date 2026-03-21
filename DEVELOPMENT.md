# AU-Funding-Arb v1.0 — Development Plan

## Phase 1: Foundation & Configuration
- [x] `requirements.txt` — Python dependencies (ccxt, pydantic-settings, structlog, aiohttp, pandas, pybit)
- [x] `src/__init__.py` — Package init
- [x] `src/models.py` — Shared Pydantic models (FundingSnapshot, HedgePosition, TradeRecord, PortfolioState, FundingPayment)
- [x] `src/config.py` — Pydantic Settings with `ARB_` env prefix, all strategy/fee/safety defaults
- [x] `.env.example` — Complete environment variable template with comments

## Phase 2: Exchange Abstraction
- [x] `src/exchange_client.py` — ccxt async wrapper (Bybit/OKX), Unified Trading Account mode
  - [x] REST methods: funding rates, orderbooks, balances, positions, orders
  - [x] Market info helpers: settlement interval, contract size, precision
  - [x] WebSocket private streams via pybit with exponential backoff reconnection
  - [x] Dry-run fake order responses
  - [x] Spot symbol derivation from perp symbol

## Phase 3: State Management
- [x] `src/position_manager.py` — In-memory position store with asyncio.Lock
  - [x] CRUD operations (add/remove/update positions)
  - [x] Funding payment tracking with negative-streak detection
  - [x] Portfolio equity tracking with high-water mark for circuit breaker
  - [x] Persist to `positions.json` on SIGTERM (synchronous for signal safety)
  - [x] Load and reconcile against exchange on startup

## Phase 4: Core Strategy Engine
- [x] `src/arb_engine.py` — Funding scanner, profitability checks, exit logic
  - [x] `scan_funding_opportunities()` — fetch all rates → pre-filter → trailing avg → net profitability → rank
  - [x] `compute_net_profitability()` — `(trailing_avg × adjusted_periods) - 4-leg maker fees`
  - [x] Dynamic settlement frequency awareness (Bybit 1h/2h/4h/8h)
  - [x] `evaluate_exits()` — hard stop-loss, 24h negative funding, basis divergence (3σ), funding decay
  - [x] Rolling basis standard deviation tracking
  - [x] Circuit breaker check (5% drawdown)
  - [x] Position sizing with portfolio-percentage cap

## Phase 5: Order Execution
- [x] `src/order_executor.py` — Atomic hedge execution (safety-critical)
  - [x] `open_hedge()` — concurrent limit orders, poll for fills, unwind on partial
  - [x] `close_hedge()` — same atomic logic in reverse
  - [x] `emergency_unwind()` — market orders both legs (circuit breaker)
  - [x] Limit-to-market fallback after urgency timeout (30s)
  - [x] Quantity rounding to exchange precision

## Phase 6: ATO Tax Module
- [x] `src/tax_logger.py` — RBA rate cache + per-trade AUD CSV logging
  - [x] `RbaRateCache` — download RBA f11 CSV, parse AUD/USD, forward-fill weekends, local cache fallback
  - [x] `TaxLogger` — append to `trades_aud.csv` with UTC + AWST timestamps, RBA rate, AUD conversion
- [x] `src/tax_export.py` — CLI export tool
  - [x] `--koinly` — Koinly Universal CSV format (spot → buy/sell, perps → margin trade, funding → other income)
  - [x] `--summary --year` — Annual tax breakdown with per-pair detail

## Phase 7: Telegram Alerts
- [x] `src/telegram_bot.py` — Outbound-only via aiohttp POST (no framework)
  - [x] Open/close hedge alerts
  - [x] Health warnings
  - [x] Circuit breaker alert
  - [x] Daily 7am AWST recap (equity, PnL, funding, active pairs)
  - [x] Startup/shutdown notifications
  - [x] Silent no-op when credentials not configured

## Phase 8: Main Entry Point
- [x] `src/main.py` — Wires all components, runs 3 async loops
  - [x] Structured logging setup (structlog with ISO timestamps)
  - [x] SIGTERM/SIGINT handler: persist positions, set shutdown event, do NOT close hedges
  - [x] Startup: load markets → refresh RBA → resume positions → reconcile → fetch equity
  - [x] Loop 1: Funding Scanner (every 60 min) — scan, open hedges, tax log, Telegram alert
  - [x] Loop 2: Health Monitor (every 30s) — WS-first, REST fallback, circuit breaker
  - [x] Loop 3: Settlement Evaluator (8h aligned) — evaluate exits, record funding payments
  - [x] Loop 4: Daily Recap (7am AWST)
  - [x] Graceful shutdown with final persist + Telegram notification

## Phase 9: Backtest
- [x] `backtest/backtest.py` — Historical funding rate simulator
  - [x] Mirrors live bot entry/exit logic
  - [x] Per-trade and aggregate results (net PnL, annualised return)
  - [x] CLI with configurable parameters
- [x] `backtest/data/` — Sample 30-day 8h funding CSVs for BTC, ETH, SOL

## Phase 10: Deployment
- [x] `Dockerfile` — python:3.12-slim, non-root user, `/app/data` volume, healthcheck
- [x] `docker-compose.yml` — env_file, volume mount, SIGTERM, log rotation
- [x] `systemd/funding-arb.service` — systemd unit for VPS deployment

## Phase 11: Documentation
- [x] `README.md` — Quick start, Docker deploy, config table, maker-fee strategy, dry-run mode, SIGTERM/resume, ATO tax compliance (RBA rates, Koinly export, deductible expenses), backtest usage, architecture diagram

## Phase 12: Quality & Testing
- [ ] Unit tests for `arb_engine` (profitability math, exit conditions)
- [ ] Unit tests for `order_executor` (atomic logic with mocked exchange)
- [ ] Unit tests for `tax_logger` (RBA CSV parsing, AUD conversion, CSV format)
- [ ] Integration test: dry-run against live Bybit API
- [ ] Type checking with mypy
- [ ] Linting with pylint/ruff

## Phase 13: Hardening
- [ ] OKX WebSocket implementation (currently placeholder)
- [ ] CoinGlass API integration for historical funding data (optional, free-tier rate limits)
- [ ] Basis divergence: fetch 30-day mark/index history on startup (currently builds incrementally)
- [ ] Prometheus/Grafana metrics endpoint
- [ ] Automated alerting on log errors via structured log sinks
