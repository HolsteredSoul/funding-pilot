"""Pydantic Settings — single source of truth for all configuration.

All values load from environment variables prefixed with ARB_.
Example: ARB_API_KEY, ARB_DRY_RUN, ARB_EXCHANGE.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Bot-wide configuration loaded from .env / environment."""

    # ── Exchange ────────────────────────────────────────────────────────
    exchange: Literal["bybit", "okx"] = "bybit"
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""  # OKX only

    # ── Modes ───────────────────────────────────────────────────────────
    dry_run: bool = True  # real APIs, no orders
    testnet: bool = False  # sandbox APIs
    cross_exchange_mode: bool = False  # disabled by default

    # ── Strategy — entry ────────────────────────────────────────────────
    min_current_funding: float = 0.0002  # 0.02 %
    min_profitable_cycles: int = 3  # trailing avg window
    expected_holding_periods: int = 8  # how many settlements we expect to hold
    max_concurrent_pairs: int = 5
    position_size_usd: float = 1000.0
    min_position_usd: float = 500.0
    max_position_usd: float = 2000.0
    max_portfolio_pct: float = 0.02  # 2 % of portfolio per pair

    # ── Strategy — exit ─────────────────────────────────────────────────
    funding_floor: float = 0.0001  # 0.01 %
    basis_divergence_sigma: float = 3.0
    hard_stop_loss_pct: float = -0.02  # -2 %
    funding_negative_hours: int = 24

    # ── Fees (Bybit VIP-0 defaults) ────────────────────────────────────
    spot_maker_fee: float = 0.001
    spot_taker_fee: float = 0.001
    perp_maker_fee: float = 0.0002
    perp_taker_fee: float = 0.00055

    # ── Execution ───────────────────────────────────────────────────────
    urgency_timeout_s: int = 30
    order_poll_interval_s: float = 2.0

    # ── Safety ──────────────────────────────────────────────────────────
    circuit_breaker_pct: float = 0.05  # 5 % portfolio drawdown

    # ── Loop intervals ──────────────────────────────────────────────────
    scan_interval_min: int = 60
    health_interval_s: int = 30
    settlement_interval_h: int = 8

    # ── File paths ──────────────────────────────────────────────────────
    data_dir: str = "data"
    positions_file: str = "positions.json"
    trades_csv: str = "trades_aud.csv"
    rba_cache_file: str = "rba_rates.csv"

    # ── Telegram ────────────────────────────────────────────────────────
    telegram_token: str = ""
    telegram_chat_id: str = ""
    daily_recap_hour_awst: int = 7  # 7 am AWST

    # ── Dashboard ─────────────────────────────────────────────────────
    dashboard_enabled: bool = True
    dashboard_port: int = 8080
    dashboard_host: str = "0.0.0.0"

    # ── Locale ──────────────────────────────────────────────────────────
    timezone: str = "Australia/Perth"  # AWST = UTC+8

    model_config = {"env_file": ".env", "env_prefix": "ARB_", "extra": "ignore"}

    # ── Derived helpers ─────────────────────────────────────────────────

    @property
    def round_trip_maker_fee(self) -> float:
        """Total fee for 4 legs (open+close × spot+perp) at maker rates."""
        return 2 * self.spot_maker_fee + 2 * self.perp_maker_fee

    @property
    def round_trip_taker_fee(self) -> float:
        """Total fee for 4 legs at taker rates (worst case)."""
        return 2 * self.spot_taker_fee + 2 * self.perp_taker_fee
