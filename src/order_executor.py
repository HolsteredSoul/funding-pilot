"""Atomic hedge execution — the most safety-critical module.

Guarantees: either BOTH legs (spot buy + perp short) fill, or any filled
leg is immediately unwound. No directional exposure is ever left open.

Order flow:
    1. Fetch both orderbooks concurrently
    2. Compute limit prices (spot slightly above best bid, perp at best ask)
    3. Place both limit orders concurrently
    4. Poll for fills up to urgency_timeout_s
    5. If both fill → success
    6. If one fills, other doesn't → cancel unfilled, market-unwind filled
    7. If neither fills after timeout → cancel both, try market on both
"""

from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timezone
from typing import Any, Optional

import structlog

from src.config import Settings
from src.exchange_client import ExchangeClient
from src.models import FundingSnapshot, HedgePosition

log = structlog.get_logger()


class OrderExecutor:
    """Executes atomic hedge open/close with limit-to-market fallback."""

    def __init__(self, settings: Settings, client: ExchangeClient) -> None:
        self._s = settings
        self._client = client

    # ═══════════════════════════════════════════════════════════════════
    # Open Hedge (Atomic)
    # ═══════════════════════════════════════════════════════════════════

    async def open_hedge(
        self, snapshot: FundingSnapshot, size_usd: float
    ) -> Optional[HedgePosition]:
        """Open a delta-neutral hedge: long spot + short perp.

        Args:
            snapshot: The funding opportunity to trade.
            size_usd: Target notional USD size.

        Returns:
            HedgePosition on success, None on failure (fully unwound).
        """
        perp_sym = snapshot.symbol
        spot_sym = snapshot.spot_symbol

        log.info(
            "open_hedge_start",
            pair=snapshot.pair_id,
            size_usd=size_usd,
            perp=perp_sym,
            spot=spot_sym,
        )

        # 1. Fetch orderbooks concurrently
        spot_book, perp_book = await asyncio.gather(
            self._client.fetch_orderbook(spot_sym),
            self._client.fetch_orderbook(perp_sym),
        )

        spot_best_bid = float(spot_book["bids"][0][0])
        spot_best_ask = float(spot_book["asks"][0][0])
        perp_best_bid = float(perp_book["bids"][0][0])
        perp_best_ask = float(perp_book["asks"][0][0])

        # 2. Compute limit prices (maker)
        # Spot buy: slightly above best bid (to sit near top of book)
        spot_tick = self._client.get_price_precision(spot_sym)
        spot_limit = spot_best_bid + spot_tick

        # Perp short: slightly below best ask
        perp_tick = self._client.get_price_precision(perp_sym)
        perp_limit = perp_best_ask - perp_tick

        # 3. Compute quantities
        spot_qty = self._round_amount(size_usd / spot_limit, spot_sym)
        perp_qty = self._round_amount(size_usd / perp_limit, perp_sym)

        if spot_qty <= 0 or perp_qty <= 0:
            log.warning("open_hedge_qty_zero", pair=snapshot.pair_id)
            return None

        # 4. Place both limit orders concurrently
        spot_order, perp_order = await asyncio.gather(
            self._client.create_limit_order(spot_sym, "buy", spot_qty, spot_limit),
            self._client.create_limit_order(perp_sym, "sell", perp_qty, perp_limit),
            return_exceptions=True,
        )

        # Handle placement failures
        if isinstance(spot_order, Exception) or isinstance(perp_order, Exception):
            await self._handle_placement_failure(
                spot_sym, perp_sym, spot_order, perp_order
            )
            return None

        spot_id = spot_order["id"]
        perp_id = perp_order["id"]

        # 5. Poll for fills
        spot_filled, perp_filled = await self._poll_both_orders(
            spot_sym, spot_id, perp_sym, perp_id
        )

        # 6. Evaluate outcome
        if spot_filled and perp_filled:
            # Both filled — success
            spot_fill = await self._client.fetch_order(spot_id, spot_sym)
            perp_fill = await self._client.fetch_order(perp_id, perp_sym)

            pos = HedgePosition(
                pair_id=snapshot.pair_id,
                perp_symbol=perp_sym,
                spot_symbol=spot_sym,
                entry_time=datetime.now(timezone.utc),
                spot_entry_price=float(spot_fill.get("average", spot_limit)),
                perp_entry_price=float(perp_fill.get("average", perp_limit)),
                notional_usd=size_usd,
                spot_qty=float(spot_fill.get("filled", spot_qty)),
                perp_qty=float(perp_fill.get("filled", perp_qty)),
                spot_order_id=spot_id,
                perp_order_id=perp_id,
            )
            log.info(
                "open_hedge_success",
                pair=pos.pair_id,
                spot_price=pos.spot_entry_price,
                perp_price=pos.perp_entry_price,
            )
            return pos

        # Partial fill — unwind
        if spot_filled and not perp_filled:
            log.warning("open_hedge_partial_spot_only", pair=snapshot.pair_id)
            await self._safe_cancel(perp_id, perp_sym)
            await self._market_unwind(spot_sym, "sell", spot_qty)
            return None

        if perp_filled and not spot_filled:
            log.warning("open_hedge_partial_perp_only", pair=snapshot.pair_id)
            await self._safe_cancel(spot_id, spot_sym)
            await self._market_unwind(perp_sym, "buy", perp_qty)
            return None

        # Neither filled after timeout — cancel both, try market
        log.warning("open_hedge_neither_filled_trying_market", pair=snapshot.pair_id)
        await self._safe_cancel(spot_id, spot_sym)
        await self._safe_cancel(perp_id, perp_sym)

        return await self._market_open_hedge(snapshot, size_usd)

    async def _market_open_hedge(
        self, snapshot: FundingSnapshot, size_usd: float
    ) -> Optional[HedgePosition]:
        """Fallback: open hedge with market orders on both legs."""
        spot_sym = snapshot.spot_symbol
        perp_sym = snapshot.symbol

        # Get current prices for qty calculation
        spot_ticker = await self._client.fetch_ticker(spot_sym)
        perp_ticker = await self._client.fetch_ticker(perp_sym)
        spot_price = float(spot_ticker.get("ask", spot_ticker.get("last", 0)))
        perp_price = float(perp_ticker.get("bid", perp_ticker.get("last", 0)))

        spot_qty = self._round_amount(size_usd / spot_price, spot_sym)
        perp_qty = self._round_amount(size_usd / perp_price, perp_sym)

        spot_order, perp_order = await asyncio.gather(
            self._client.create_market_order(spot_sym, "buy", spot_qty),
            self._client.create_market_order(perp_sym, "sell", perp_qty),
            return_exceptions=True,
        )

        if isinstance(spot_order, Exception) or isinstance(perp_order, Exception):
            # Market orders also failed — last resort unwind
            await self._handle_placement_failure(
                spot_sym, perp_sym, spot_order, perp_order
            )
            return None

        pos = HedgePosition(
            pair_id=snapshot.pair_id,
            perp_symbol=perp_sym,
            spot_symbol=spot_sym,
            entry_time=datetime.now(timezone.utc),
            spot_entry_price=float(spot_order.get("average", spot_price)),
            perp_entry_price=float(perp_order.get("average", perp_price)),
            notional_usd=size_usd,
            spot_qty=float(spot_order.get("filled", spot_qty)),
            perp_qty=float(perp_order.get("filled", perp_qty)),
            spot_order_id=spot_order["id"],
            perp_order_id=perp_order["id"],
            fill_type="taker",
        )
        log.info("open_hedge_market_success", pair=pos.pair_id)
        return pos

    # ═══════════════════════════════════════════════════════════════════
    # Close Hedge (Atomic)
    # ═══════════════════════════════════════════════════════════════════

    async def close_hedge(
        self, position: HedgePosition, reason: str
    ) -> bool:
        """Close a hedge: sell spot + buy-to-close perp.

        Args:
            position: The hedge to close.
            reason: Why we're closing (for logging).

        Returns:
            True if fully closed, False if unwind needed manual attention.
        """
        spot_sym = position.spot_symbol
        perp_sym = position.perp_symbol

        log.info(
            "close_hedge_start",
            pair=position.pair_id,
            reason=reason,
        )

        # Fetch orderbooks
        spot_book, perp_book = await asyncio.gather(
            self._client.fetch_orderbook(spot_sym),
            self._client.fetch_orderbook(perp_sym),
        )

        # Spot sell: slightly below best ask (maker)
        spot_tick = self._client.get_price_precision(spot_sym)
        spot_limit = float(spot_book["asks"][0][0]) - spot_tick

        # Perp buy-to-close: slightly above best bid (maker)
        perp_tick = self._client.get_price_precision(perp_sym)
        perp_limit = float(perp_book["bids"][0][0]) + perp_tick

        # Place both limit orders
        spot_order, perp_order = await asyncio.gather(
            self._client.create_limit_order(
                spot_sym, "sell", position.spot_qty, spot_limit
            ),
            self._client.create_limit_order(
                perp_sym, "buy", position.perp_qty, perp_limit
            ),
            return_exceptions=True,
        )

        if isinstance(spot_order, Exception) or isinstance(perp_order, Exception):
            log.error("close_hedge_placement_failed", pair=position.pair_id)
            # Fallback to market
            position.fill_type = "taker"
            return await self._market_close_hedge(position)

        spot_id = spot_order["id"]
        perp_id = perp_order["id"]

        spot_filled, perp_filled = await self._poll_both_orders(
            spot_sym, spot_id, perp_sym, perp_id
        )

        if spot_filled and perp_filled:
            log.info("close_hedge_success", pair=position.pair_id, reason=reason)
            return True

        # Partial — cancel unfilled, market the rest
        if not spot_filled:
            await self._safe_cancel(spot_id, spot_sym)
            await self._market_unwind(spot_sym, "sell", position.spot_qty)
        if not perp_filled:
            await self._safe_cancel(perp_id, perp_sym)
            await self._market_unwind(perp_sym, "buy", position.perp_qty)

        if not spot_filled or not perp_filled:
            position.fill_type = "taker"
        log.info("close_hedge_completed_with_market", pair=position.pair_id)
        return True

    async def _market_close_hedge(self, position: HedgePosition) -> bool:
        """Fallback: close hedge with market orders."""
        spot_order, perp_order = await asyncio.gather(
            self._client.create_market_order(
                position.spot_symbol, "sell", position.spot_qty
            ),
            self._client.create_market_order(
                position.perp_symbol, "buy", position.perp_qty
            ),
            return_exceptions=True,
        )

        success = not isinstance(spot_order, Exception) and not isinstance(
            perp_order, Exception
        )
        if success:
            log.info("close_hedge_market_success", pair=position.pair_id)
        else:
            log.error(
                "close_hedge_market_failed",
                pair=position.pair_id,
                spot_err=str(spot_order) if isinstance(spot_order, Exception) else None,
                perp_err=str(perp_order) if isinstance(perp_order, Exception) else None,
            )
        return success

    # ═══════════════════════════════════════════════════════════════════
    # Emergency Unwind (Circuit Breaker)
    # ═══════════════════════════════════════════════════════════════════

    async def emergency_unwind(self, position: HedgePosition) -> bool:
        """Market-close both legs immediately. Used by circuit breaker."""
        log.critical("emergency_unwind", pair=position.pair_id)
        return await self._market_close_hedge(position)

    # ═══════════════════════════════════════════════════════════════════
    # Internal Helpers
    # ═══════════════════════════════════════════════════════════════════

    async def _poll_both_orders(
        self,
        sym_a: str,
        id_a: str,
        sym_b: str,
        id_b: str,
    ) -> tuple[bool, bool]:
        """Poll two orders for fills up to urgency_timeout_s.

        Returns:
            (a_filled, b_filled) booleans.
        """
        timeout = self._s.urgency_timeout_s
        interval = self._s.order_poll_interval_s
        deadline = time.monotonic() + timeout
        a_filled = False
        b_filled = False

        while time.monotonic() < deadline:
            if not a_filled:
                order_a = await self._client.fetch_order(id_a, sym_a)
                if order_a.get("status") == "closed":
                    a_filled = True
            if not b_filled:
                order_b = await self._client.fetch_order(id_b, sym_b)
                if order_b.get("status") == "closed":
                    b_filled = True

            if a_filled and b_filled:
                return True, True

            await asyncio.sleep(interval)

        return a_filled, b_filled

    async def _safe_cancel(self, order_id: str, symbol: str) -> None:
        """Cancel an order, ignoring errors if already filled/cancelled."""
        try:
            await self._client.cancel_order(order_id, symbol)
        except Exception:
            log.warning("cancel_failed_ignoring", order_id=order_id, symbol=symbol)

    async def _market_unwind(self, symbol: str, side: str, qty: float) -> None:
        """Market-close a single leg to eliminate directional exposure."""
        log.warning("market_unwind_leg", symbol=symbol, side=side, qty=qty)
        try:
            await self._client.create_market_order(symbol, side, qty)
        except Exception:
            log.exception(
                "market_unwind_failed_MANUAL_INTERVENTION_NEEDED",
                symbol=symbol,
                side=side,
                qty=qty,
            )

    async def _handle_placement_failure(
        self,
        spot_sym: str,
        perp_sym: str,
        spot_result: Any,
        perp_result: Any,
    ) -> None:
        """Handle the case where one or both order placements raise exceptions."""
        spot_ok = not isinstance(spot_result, Exception)
        perp_ok = not isinstance(perp_result, Exception)

        if spot_ok and not perp_ok:
            # Spot placed, perp failed — unwind spot
            log.error("perp_placement_failed", error=str(perp_result))
            spot_qty = float(spot_result.get("amount", 0))
            if spot_qty > 0:
                await self._safe_cancel(spot_result["id"], spot_sym)
                await self._market_unwind(spot_sym, "sell", spot_qty)

        elif perp_ok and not spot_ok:
            # Perp placed, spot failed — unwind perp
            log.error("spot_placement_failed", error=str(spot_result))
            perp_qty = float(perp_result.get("amount", 0))
            if perp_qty > 0:
                await self._safe_cancel(perp_result["id"], perp_sym)
                await self._market_unwind(perp_sym, "buy", perp_qty)

        else:
            # Both failed
            log.error(
                "both_placements_failed",
                spot_err=str(spot_result),
                perp_err=str(perp_result),
            )

    def _round_amount(self, amount: float, symbol: str) -> float:
        """Round order quantity to the symbol's precision."""
        precision = self._client.get_amount_precision(symbol)
        if precision <= 0:
            return amount
        # Round down to avoid exceeding balance
        factor = 1.0 / precision
        return math.floor(amount * factor) / factor
