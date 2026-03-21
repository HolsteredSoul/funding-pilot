"""Shared Pydantic data models used across all modules."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class FundingSnapshot(BaseModel):
    """A ranked funding-rate opportunity for a single perpetual pair."""

    symbol: str  # perp symbol, e.g. "BTC/USDT:USDT"
    spot_symbol: str  # e.g. "BTC/USDT"
    pair_id: str  # normalised key, e.g. "BTCUSDT"
    current_rate: float  # current 8h funding rate (decimal, e.g. 0.0003)
    trailing_avg: float  # trailing avg over min_profitable_cycles periods
    net_profit: float  # expected net profit after fees (decimal)
    next_settlement: datetime
    settlement_interval_h: int = 8  # dynamic: 1, 2, 4, or 8
    mark_price: float = 0.0
    index_price: float = 0.0


class HedgePosition(BaseModel):
    """Represents one active long-spot / short-perp hedge pair."""

    pair_id: str  # "BTCUSDT" normalised key
    perp_symbol: str
    spot_symbol: str
    side: str = "long_spot_short_perp"
    entry_time: datetime
    spot_entry_price: float
    perp_entry_price: float
    notional_usd: float
    spot_qty: float
    perp_qty: float
    spot_order_id: str = ""
    perp_order_id: str = ""
    cumulative_funding: float = 0.0
    cumulative_pnl_usd: float = 0.0
    funding_history: list[FundingPayment] = Field(default_factory=list)
    negative_funding_since: Optional[datetime] = None  # tracks 24h negative streak
    status: str = "open"  # open | closing | closed


class FundingPayment(BaseModel):
    """A single funding payment received/paid on a position."""

    timestamp: datetime
    rate: float
    amount_usd: float


class TradeRecord(BaseModel):
    """A single trade event logged for ATO tax compliance."""

    timestamp_utc: datetime
    timestamp_awst: datetime
    pair: str
    side: str  # "buy" | "sell"
    qty: float
    price_usd: float
    amount_usd: float
    fee_usd: float
    rba_aud_rate: float  # AUD/USD rate from RBA
    aud_value: float
    fee_aud: float
    exchange: str
    tx_type: str  # "open" | "close" | "funding" | "fee"
    pnl_usd: Optional[float] = None
    pnl_aud: Optional[float] = None


class PortfolioState(BaseModel):
    """Aggregate portfolio snapshot for circuit-breaker and reporting."""

    positions: list[HedgePosition] = Field(default_factory=list)
    total_equity_usd: float = 0.0
    peak_equity_usd: float = 0.0
    total_realised_pnl_usd: float = 0.0
    total_funding_collected_usd: float = 0.0
    last_updated: datetime = Field(default_factory=datetime.utcnow)


class DashboardState(BaseModel):
    """Mutable state bag shared between async loops and the web dashboard."""

    last_scan_results: list[FundingSnapshot] = Field(default_factory=list)
    last_scan_time: Optional[datetime] = None
    loop_status: dict[str, str] = Field(default_factory=lambda: {
        "scanner": "idle",
        "health": "idle",
        "settlement": "idle",
    })
    circuit_breaker_active: bool = False
    ws_connected: bool = False
    bot_start_time: Optional[datetime] = None
