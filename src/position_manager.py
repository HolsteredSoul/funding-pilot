"""Position state management — persistence, resume, reconciliation.

Holds all open HedgePositions in memory with an asyncio.Lock for safe
concurrent access across the three async loops. Persists to positions.json
on SIGTERM and reloads on startup.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import structlog

from src.config import Settings
from src.models import HedgePosition, PortfolioState

log = structlog.get_logger()


class PositionManager:
    """Thread-safe (asyncio) manager for open hedge positions."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = asyncio.Lock()
        self.positions: dict[str, HedgePosition] = {}
        self.portfolio = PortfolioState()
        self._data_dir = Path(settings.data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)

    # ── Position CRUD ───────────────────────────────────────────────────

    async def add_position(self, pos: HedgePosition) -> None:
        """Register a newly opened hedge."""
        async with self._lock:
            self.positions[pos.pair_id] = pos
            self.portfolio.positions = list(self.positions.values())
            log.info("position_added", pair=pos.pair_id, notional=pos.notional_usd)

    async def remove_position(self, pair_id: str) -> Optional[HedgePosition]:
        """Remove a closed hedge. Returns the removed position or None."""
        async with self._lock:
            pos = self.positions.pop(pair_id, None)
            self.portfolio.positions = list(self.positions.values())
            if pos:
                log.info("position_removed", pair=pair_id)
            return pos

    def has_hedge(self, pair_id: str) -> bool:
        """Check if a hedge already exists for this pair."""
        return pair_id in self.positions

    @property
    def open_count(self) -> int:
        """Number of currently open hedges."""
        return len(self.positions)

    # ── Position Updates ────────────────────────────────────────────────

    async def update_funding(self, pair_id: str, amount_usd: float, rate: float) -> None:
        """Record a funding payment for a position."""
        async with self._lock:
            pos = self.positions.get(pair_id)
            if pos is None:
                return
            pos.cumulative_funding += amount_usd
            pos.cumulative_pnl_usd += amount_usd
            from src.models import FundingPayment

            pos.funding_history.append(
                FundingPayment(
                    timestamp=datetime.now(timezone.utc),
                    rate=rate,
                    amount_usd=amount_usd,
                )
            )
            # Track negative funding streaks
            if rate < 0:
                if pos.negative_funding_since is None:
                    pos.negative_funding_since = datetime.now(timezone.utc)
            else:
                pos.negative_funding_since = None

    async def update_unrealised_pnl(self, pair_id: str, pnl_usd: float) -> None:
        """Update the unrealised PnL for health monitoring."""
        async with self._lock:
            pos = self.positions.get(pair_id)
            if pos is None:
                return
            pos.cumulative_pnl_usd = pos.cumulative_funding + pnl_usd

    # ── Portfolio State ─────────────────────────────────────────────────

    async def update_portfolio_equity(self, total_equity_usd: float) -> None:
        """Update portfolio equity and track high-water mark."""
        async with self._lock:
            self.portfolio.total_equity_usd = total_equity_usd
            self.portfolio.peak_equity_usd = max(
                self.portfolio.peak_equity_usd, total_equity_usd
            )
            self.portfolio.last_updated = datetime.now(timezone.utc)

    def drawdown_pct(self) -> float:
        """Current drawdown from peak as a positive fraction."""
        if self.portfolio.peak_equity_usd <= 0:
            return 0.0
        return (
            self.portfolio.peak_equity_usd - self.portfolio.total_equity_usd
        ) / self.portfolio.peak_equity_usd

    # ── Persistence ─────────────────────────────────────────────────────

    def save_to_file(self) -> None:
        """Serialize all open positions to positions.json (SIGTERM handler).

        This is synchronous so it can safely run in a signal handler context.
        """
        path = self._data_dir / self._settings.positions_file
        data = {
            pair_id: pos.model_dump(mode="json")
            for pair_id, pos in self.positions.items()
        }
        try:
            path.write_text(json.dumps(data, indent=2, default=str))
            log.info("positions_persisted", path=str(path), count=len(data))
        except Exception:
            log.exception("positions_persist_failed", path=str(path))

    def load_from_file(self) -> list[HedgePosition]:
        """Load positions.json on startup. Returns list of loaded positions."""
        path = self._data_dir / self._settings.positions_file
        if not path.exists():
            log.info("no_positions_file", path=str(path))
            return []

        try:
            raw = json.loads(path.read_text())
            loaded: list[HedgePosition] = []
            for pair_id, data in raw.items():
                pos = HedgePosition.model_validate(data)
                self.positions[pair_id] = pos
                loaded.append(pos)
            self.portfolio.positions = list(self.positions.values())
            log.info("positions_loaded", path=str(path), count=len(loaded))
            return loaded
        except Exception:
            log.exception("positions_load_failed", path=str(path))
            return []

    async def reconcile_with_exchange(self, client: Any) -> None:
        """Compare local state against actual exchange positions.

        Logs discrepancies but does NOT auto-close — manual review required.

        Args:
            client: ExchangeClient instance.
        """
        if not self.positions:
            log.info("reconcile_skip_no_local_positions")
            return

        try:
            exchange_positions = await client.fetch_positions()
            exchange_symbols = {p["symbol"] for p in exchange_positions}

            stale_pairs: list[str] = []
            for pair_id, local_pos in list(self.positions.items()):
                if local_pos.perp_symbol not in exchange_symbols:
                    log.warning(
                        "reconcile_removed_stale",
                        pair=pair_id,
                        perp=local_pos.perp_symbol,
                        msg="Position in file but not on exchange — removing stale entry",
                    )
                    stale_pairs.append(pair_id)

            for pair_id in stale_pairs:
                del self.positions[pair_id]
            if stale_pairs:
                self.portfolio.positions = list(self.positions.values())

            for ex_pos in exchange_positions:
                sym = ex_pos["symbol"]
                # Check if we track this position
                matching = [
                    p for p in self.positions.values() if p.perp_symbol == sym
                ]
                if not matching:
                    log.warning(
                        "reconcile_mismatch_exchange_only",
                        symbol=sym,
                        contracts=ex_pos.get("contracts"),
                        msg="Position on exchange but not in local file — orphaned?",
                    )

            log.info(
                "reconcile_complete",
                local_count=len(self.positions),
                exchange_count=len(exchange_positions),
            )
        except Exception:
            log.exception("reconcile_failed")
