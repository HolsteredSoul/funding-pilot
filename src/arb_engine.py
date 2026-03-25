"""Core arbitrage strategy — funding scanner, profitability checks, exit logic.

This module produces *decisions* (which pairs to enter/exit and why).
It does NOT execute orders — that responsibility belongs to order_executor.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from src.config import Settings
from src.exchange_client import ExchangeClient
from src.models import FundingSnapshot, HedgePosition, PortfolioState
from src.position_manager import PositionManager

log = structlog.get_logger()


class ArbEngine:
    """Funding-rate arbitrage strategy engine."""

    def __init__(
        self,
        settings: Settings,
        client: ExchangeClient,
        position_mgr: PositionManager,
    ) -> None:
        self._s = settings
        self._client = client
        self._pos_mgr = position_mgr
        # Rolling basis stats cache: symbol → list of basis values
        self._basis_history: dict[str, list[float]] = {}

    # ═══════════════════════════════════════════════════════════════════
    # LOOP 1 — Funding Scanner (every 60 min)
    # ═══════════════════════════════════════════════════════════════════

    async def scan_funding_opportunities(self) -> list[FundingSnapshot]:
        """Scan all perps, pre-filter, compute profitability, rank.

        Returns:
            Sorted list of FundingSnapshot objects that pass all checks,
            capped at (max_concurrent_pairs - currently_open) entries.
        """
        # 1. Fetch current funding rates for all perps
        all_rates = await self._client.fetch_all_funding_rates()
        log.info("scanner_raw_pairs", total=len(all_rates))

        # 2. Pre-filter: current rate >= min_current_funding
        candidates: list[tuple[str, dict[str, Any]]] = []
        for sym, data in all_rates.items():
            rate = float(data.get("fundingRate", 0))
            if rate >= self._s.min_current_funding:
                candidates.append((sym, data))

        log.info(
            "scanner_pre_filtered",
            passed=len(candidates),
            threshold=self._s.min_current_funding,
        )

        # 3. Skip pairs we already have a hedge on
        candidates = [
            (sym, data)
            for sym, data in candidates
            if not self._pos_mgr.has_hedge(self._normalise_pair_id(sym))
        ]

        # 4. For each candidate, fetch trailing history & compute profitability
        snapshots: list[FundingSnapshot] = []
        slots_available = self._s.max_concurrent_pairs - self._pos_mgr.open_count

        if slots_available <= 0:
            log.info("scanner_no_slots_available")
            return []

        for sym, data in candidates:
            snapshot = await self._build_snapshot(sym, data)
            if snapshot is None:
                continue
            if snapshot.net_profit > 0:
                snapshots.append(snapshot)

        # 5. Rank by net profitability descending
        snapshots.sort(key=lambda s: s.net_profit, reverse=True)

        # 6. Cap at available slots
        result = snapshots[:slots_available]
        log.info(
            "scanner_opportunities",
            profitable=len(snapshots),
            selected=len(result),
            top_pairs=[s.pair_id for s in result],
        )
        return result

    async def _build_snapshot(
        self, symbol: str, rate_data: dict[str, Any]
    ) -> FundingSnapshot | None:
        """Build a FundingSnapshot with trailing avg and net profitability.

        Returns None if insufficient history or spot symbol not found.
        """
        spot_symbol = self._client.find_spot_symbol(symbol)
        if spot_symbol is None:
            return None

        pair_id = self._normalise_pair_id(symbol)
        current_rate = float(rate_data.get("fundingRate", 0))
        settlement_h = self._client.get_settlement_interval_h(symbol)

        # Fetch trailing funding history
        history = await self._client.fetch_funding_rate_history(
            symbol, limit=self._s.min_profitable_cycles + 2
        )
        if len(history) < self._s.min_profitable_cycles:
            return None

        # Compute trailing average over min_profitable_cycles periods
        recent_rates = [
            float(h.get("fundingRate", 0))
            for h in history[: self._s.min_profitable_cycles]
        ]
        trailing_avg = statistics.mean(recent_rates)

        # Adjust expected_holding_periods for non-8h settlement intervals
        # More frequent settlements → more funding collections per day
        scale_factor = 8.0 / settlement_h
        adjusted_periods = self._s.expected_holding_periods * scale_factor

        # Net profitability check
        net_profit = self.compute_net_profitability(trailing_avg, adjusted_periods)

        # Next settlement timestamp
        next_ts = rate_data.get("fundingDatetime") or rate_data.get(
            "nextFundingDatetime"
        )
        if isinstance(next_ts, str):
            next_settlement = datetime.fromisoformat(
                next_ts.replace("Z", "+00:00")
            )
        else:
            next_settlement = datetime.now(timezone.utc)

        # Mark & index prices
        try:
            ticker = await self._client.fetch_ticker(symbol)
            mark_price = float(ticker.get("mark", 0) or ticker.get("last", 0))
            index_price = float(ticker.get("index", 0) or mark_price)
        except Exception:
            mark_price = 0.0
            index_price = 0.0

        return FundingSnapshot(
            symbol=symbol,
            spot_symbol=spot_symbol,
            pair_id=pair_id,
            current_rate=current_rate,
            trailing_avg=trailing_avg,
            net_profit=net_profit,
            next_settlement=next_settlement,
            settlement_interval_h=settlement_h,
            mark_price=mark_price,
            index_price=index_price,
        )

    def compute_net_profitability(
        self, trailing_avg: float, adjusted_periods: float
    ) -> float:
        """Net expected return after round-trip fees.

        Formula:
            net = (trailing_avg × adjusted_periods) - round_trip_fees

        Where round_trip_fees = 2×spot_maker + 2×perp_maker (4 legs, maker).
        """
        gross = trailing_avg * adjusted_periods
        fees = self._s.round_trip_maker_fee
        return gross - fees

    # ═══════════════════════════════════════════════════════════════════
    # LOOP 3 — Settlement / Exit Evaluator (every 8h aligned)
    # ═══════════════════════════════════════════════════════════════════

    async def evaluate_exits(
        self, positions: list[HedgePosition]
    ) -> list[tuple[HedgePosition, str]]:
        """Check all exit conditions for each open position.

        Returns:
            List of (position, reason_string) for positions that should close.
        """
        exits: list[tuple[HedgePosition, str]] = []

        for pos in positions:
            reason = await self._check_exit_conditions(pos)
            if reason:
                exits.append((pos, reason))

        if exits:
            log.info(
                "exit_evaluator_results",
                exits=[(p.pair_id, r) for p, r in exits],
            )
        return exits

    async def _check_exit_conditions(self, pos: HedgePosition) -> str | None:
        """Check exit conditions in priority order. Returns reason or None."""

        # 1. Hard stop-loss: cumulative PnL < -2% of notional
        if pos.notional_usd > 0:
            pnl_pct = pos.cumulative_pnl_usd / pos.notional_usd
            if pnl_pct <= self._s.hard_stop_loss_pct:
                return f"hard_stop_loss ({pnl_pct:.4%})"

        # 2. Funding negative for 24h straight
        if pos.negative_funding_since is not None:
            neg_duration = datetime.now(timezone.utc) - pos.negative_funding_since
            if neg_duration >= timedelta(hours=self._s.funding_negative_hours):
                return f"funding_negative_{self._s.funding_negative_hours}h"

        # 3. Basis divergence: |mark - index| > 3σ of 30-day rolling std
        try:
            basis, basis_std = await self._compute_basis_stats(pos.perp_symbol)
            if basis_std > 0 and abs(basis) > self._s.basis_divergence_sigma * basis_std:
                return (
                    f"basis_divergence (basis={basis:.6f}, "
                    f"threshold={self._s.basis_divergence_sigma}σ={self._s.basis_divergence_sigma * basis_std:.6f})"
                )
        except Exception:
            log.warning("basis_check_failed", pair=pos.pair_id)

        # 4. Funding decay: trailing 3-period avg < floor (only after min hold)
        if len(pos.funding_history) >= max(3, self._s.min_hold_periods):
            recent_rates = [f.rate for f in pos.funding_history[-3:]]
            trailing_avg = statistics.mean(recent_rates)
            if trailing_avg < self._s.funding_floor:
                return f"funding_decay (trailing_avg={trailing_avg:.6f} < floor={self._s.funding_floor:.6f})"

        return None

    async def _compute_basis_stats(self, symbol: str) -> tuple[float, float]:
        """Compute current basis and 30-day rolling standard deviation.

        Basis = (mark_price - index_price) / index_price

        Returns:
            (current_basis, rolling_std_dev)
        """
        ticker = await self._client.fetch_ticker(symbol)
        mark = float(ticker.get("mark", 0) or ticker.get("last", 0))
        index = float(ticker.get("index", 0) or mark)

        if index == 0:
            return 0.0, 0.0

        current_basis = (mark - index) / index

        # Update rolling history
        if symbol not in self._basis_history:
            self._basis_history[symbol] = []
        self._basis_history[symbol].append(current_basis)

        # Keep ~30 days of data (assuming 8h checks = 90 data points)
        max_points = 90
        if len(self._basis_history[symbol]) > max_points:
            self._basis_history[symbol] = self._basis_history[symbol][-max_points:]

        history = self._basis_history[symbol]
        if len(history) < 10:
            # Not enough data for meaningful std dev
            return current_basis, 0.0

        std_dev = statistics.stdev(history)
        return current_basis, std_dev

    # ═══════════════════════════════════════════════════════════════════
    # Circuit Breaker
    # ═══════════════════════════════════════════════════════════════════

    async def should_circuit_break(self) -> bool:
        """True if portfolio drawdown exceeds the configured threshold."""
        drawdown = self._pos_mgr.drawdown_pct()
        if drawdown >= self._s.circuit_breaker_pct:
            log.critical(
                "circuit_breaker_triggered",
                drawdown_pct=f"{drawdown:.2%}",
                threshold=f"{self._s.circuit_breaker_pct:.2%}",
            )
            return True
        return False

    # ═══════════════════════════════════════════════════════════════════
    # Position Sizing
    # ═══════════════════════════════════════════════════════════════════

    def compute_position_size(self) -> float:
        """Determine USD size for the next hedge, respecting all limits.

        Rules:
            - Base: position_size_usd (default $1000)
            - Clamped to [min_position_usd, max_position_usd]
            - Never exceed max_portfolio_pct (2%) of total equity
        """
        equity = self._pos_mgr.portfolio.total_equity_usd
        size = self._s.position_size_usd

        if equity > 0:
            max_by_portfolio = equity * self._s.max_portfolio_pct
            size = min(size, max_by_portfolio)

        size = max(self._s.min_position_usd, min(size, self._s.max_position_usd))
        return size

    # ═══════════════════════════════════════════════════════════════════
    # Helpers
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def _normalise_pair_id(symbol: str) -> str:
        """Convert ccxt symbol to a simple pair ID.

        "BTC/USDT:USDT" → "BTCUSDT"
        "BTC/USDT" → "BTCUSDT"
        """
        return symbol.split(":")[0].replace("/", "")
