"""Exchange abstraction layer — ccxt async for REST, pybit for WebSocket.

Supports Bybit (default) and OKX unified trading accounts.
WebSocket reconnects automatically with exponential backoff.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import ccxt.async_support as ccxt
import structlog

from src.config import Settings
from src.models import FundingSnapshot

log = structlog.get_logger()


class ExchangeClient:
    """Unified async exchange client wrapping ccxt + WebSocket streams."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._exchange: ccxt.Exchange = self._build_exchange()
        self._ws_task: Optional[asyncio.Task[None]] = None
        self._ws_shutdown = False
        self._last_ws_update: float = 0.0

    # ── Factory ─────────────────────────────────────────────────────────

    def _build_exchange(self) -> ccxt.Exchange:
        """Instantiate the ccxt async exchange with correct options."""
        s = self._settings
        common_opts: dict[str, Any] = {
            "apiKey": s.api_key,
            "secret": s.api_secret,
            "enableRateLimit": True,
        }

        if s.exchange == "bybit":
            exchange = ccxt.bybit(
                {
                    **common_opts,
                    "options": {
                        "defaultType": "swap",
                        "accountType": "unified",
                    },
                }
            )
            if s.testnet:
                exchange.set_sandbox_mode(True)

        elif s.exchange == "okx":
            exchange = ccxt.okx(
                {
                    **common_opts,
                    "password": s.api_passphrase,
                    "options": {
                        "defaultType": "swap",
                        "accountMode": "single",
                    },
                }
            )
            if s.testnet:
                exchange.set_sandbox_mode(True)
        else:
            raise ValueError(f"Unsupported exchange: {s.exchange}")

        return exchange

    # ── Market Data ─────────────────────────────────────────────────────

    async def load_markets(self) -> None:
        """Load/refresh exchange market metadata. Call once on startup."""
        await self._exchange.load_markets()
        log.info(
            "markets_loaded",
            exchange=self._settings.exchange,
            num_markets=len(self._exchange.markets),
        )

    async def fetch_all_funding_rates(self) -> dict[str, dict[str, Any]]:
        """Fetch current funding rates for ALL linear USDT perps.

        Returns:
            Dict keyed by perp symbol → {symbol, fundingRate, fundingTimestamp,
            nextFundingTimestamp, info}.
        """
        try:
            rates: list[dict[str, Any]] = await self._exchange.fetch_funding_rates()
            # Filter to USDT-margined linear perps only
            result = {}
            for sym, data in rates.items():
                market = self._exchange.markets.get(sym)
                if market and market.get("linear") and market.get("active"):
                    result[sym] = data
            log.info("funding_rates_fetched", count=len(result))
            return result
        except Exception:
            log.exception("fetch_funding_rates_failed")
            raise

    async def fetch_funding_rate_history(
        self, symbol: str, limit: int = 24
    ) -> list[dict[str, Any]]:
        """Fetch historical funding rate entries for a single perp.

        Args:
            symbol: Perp symbol, e.g. "BTC/USDT:USDT".
            limit: Number of most-recent settlement periods to fetch.

        Returns:
            List of dicts with keys: timestamp, fundingRate, symbol.
        """
        try:
            history = await self._exchange.fetch_funding_rate_history(
                symbol, limit=limit
            )
            return history
        except Exception:
            log.exception("fetch_funding_history_failed", symbol=symbol)
            return []

    async def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        """Fetch latest ticker (bid/ask/last/mark/index) for a symbol."""
        return await self._exchange.fetch_ticker(symbol)

    async def fetch_orderbook(
        self, symbol: str, limit: int = 5
    ) -> dict[str, Any]:
        """Fetch top-of-book orderbook for a symbol."""
        return await self._exchange.fetch_order_book(symbol, limit=limit)

    async def fetch_mark_price(self, symbol: str) -> float:
        """Fetch the current mark price for a perp."""
        ticker = await self.fetch_ticker(symbol)
        # ccxt stores mark price in info or as a separate field
        mark = ticker.get("mark") or ticker.get("markPrice")
        if mark is None:
            mark = ticker.get("last", 0.0)
        return float(mark)

    # ── Account Data ────────────────────────────────────────────────────

    async def fetch_balance(self) -> dict[str, Any]:
        """Fetch unified account balance (spot + perp margin)."""
        return await self._exchange.fetch_balance()

    async def fetch_positions(self) -> list[dict[str, Any]]:
        """Fetch all open perp positions."""
        positions = await self._exchange.fetch_positions()
        # Filter to positions with non-zero size
        return [p for p in positions if float(p.get("contracts", 0)) != 0]

    async def fetch_total_equity(self) -> float:
        """Return total USD equity in the unified account."""
        balance = await self.fetch_balance()
        # ccxt unified: balance['total']['USDT'] is the primary metric
        total = balance.get("total", {})
        usdt = float(total.get("USDT", 0))
        return usdt

    # ── Order Execution ─────────────────────────────────────────────────

    async def create_limit_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: float,
        params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Place a limit order. Returns ccxt order dict."""
        log.info(
            "place_limit_order",
            symbol=symbol,
            side=side,
            amount=amount,
            price=price,
            dry_run=self._settings.dry_run,
        )
        if self._settings.dry_run:
            return self._dry_run_order(symbol, side, amount, price, "limit")

        return await self._exchange.create_limit_order(
            symbol, side, amount, price, params=params or {}
        )

    async def create_market_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Place a market order. Returns ccxt order dict."""
        log.info(
            "place_market_order",
            symbol=symbol,
            side=side,
            amount=amount,
            dry_run=self._settings.dry_run,
        )
        if self._settings.dry_run:
            return self._dry_run_order(symbol, side, amount, 0.0, "market")

        return await self._exchange.create_market_order(
            symbol, side, amount, params=params or {}
        )

    async def cancel_order(
        self, order_id: str, symbol: str
    ) -> dict[str, Any]:
        """Cancel an open order by ID."""
        if self._settings.dry_run:
            log.info("dry_run_cancel", order_id=order_id, symbol=symbol)
            return {"id": order_id, "status": "canceled"}
        return await self._exchange.cancel_order(order_id, symbol)

    async def fetch_order(
        self, order_id: str, symbol: str
    ) -> dict[str, Any]:
        """Fetch current state of an order."""
        if self._settings.dry_run:
            return {
                "id": order_id,
                "status": "closed",
                "filled": 1.0,
                "remaining": 0.0,
                "average": 0.0,
            }
        return await self._exchange.fetch_order(order_id, symbol)

    # ── Market Info Helpers ─────────────────────────────────────────────

    def get_settlement_interval_h(self, symbol: str) -> int:
        """Read the funding settlement interval (hours) for a perp.

        Bybit uses dynamic intervals (1, 2, 4, 8h). Returns 8 as fallback.
        """
        market = self._exchange.markets.get(symbol, {})
        info = market.get("info", {})
        # Bybit: fundingInterval field (in minutes)
        interval_min = info.get("fundingInterval")
        if interval_min is not None:
            return max(1, int(interval_min) // 60)
        return 8

    def get_contract_size(self, symbol: str) -> float:
        """Return the minimum contract/lot size for a symbol."""
        market = self._exchange.markets.get(symbol, {})
        return float(market.get("contractSize", 1.0))

    def get_min_amount(self, symbol: str) -> float:
        """Return minimum order amount for a symbol."""
        market = self._exchange.markets.get(symbol, {})
        limits = market.get("limits", {}).get("amount", {})
        return float(limits.get("min", 0.001))

    def get_amount_precision(self, symbol: str) -> float:
        """Return the amount step/precision for a symbol."""
        market = self._exchange.markets.get(symbol, {})
        precision = market.get("precision", {})
        return float(precision.get("amount", 0.001))

    def get_price_precision(self, symbol: str) -> float:
        """Return the price tick size for a symbol."""
        market = self._exchange.markets.get(symbol, {})
        precision = market.get("precision", {})
        return float(precision.get("price", 0.01))

    def find_spot_symbol(self, perp_symbol: str) -> Optional[str]:
        """Derive the spot symbol from a perp symbol.

        e.g. "BTC/USDT:USDT" → "BTC/USDT"
        """
        base = perp_symbol.split("/")[0]
        spot = f"{base}/USDT"
        if spot in self._exchange.markets:
            return spot
        return None

    # ── WebSocket (Private Streams) ─────────────────────────────────────

    async def start_private_ws(
        self,
        on_position: Callable[[dict[str, Any]], None],
        on_wallet: Callable[[dict[str, Any]], None],
        on_order: Callable[[dict[str, Any]], None],
    ) -> None:
        """Start WebSocket private streams with auto-reconnect.

        Uses pybit for Bybit; raw aiohttp for OKX.
        Callbacks are invoked on each push message.
        """
        self._ws_shutdown = False
        self._ws_task = asyncio.create_task(
            self._ws_loop(on_position, on_wallet, on_order)
        )

    async def _ws_loop(
        self,
        on_position: Callable[[dict[str, Any]], None],
        on_wallet: Callable[[dict[str, Any]], None],
        on_order: Callable[[dict[str, Any]], None],
    ) -> None:
        """Internal WebSocket loop with exponential backoff reconnection."""
        delay = 1.0

        while not self._ws_shutdown:
            try:
                if self._settings.exchange == "bybit":
                    await self._run_bybit_ws(on_position, on_wallet, on_order)
                else:
                    # OKX WS — placeholder; implement similarly
                    log.warning("okx_ws_not_implemented_falling_back_to_rest")
                    await asyncio.sleep(60)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning(
                    "ws_disconnected",
                    error=str(exc),
                    backoff_s=delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
            else:
                delay = 1.0  # reset on clean exit

    async def _run_bybit_ws(
        self,
        on_position: Callable[[dict[str, Any]], None],
        on_wallet: Callable[[dict[str, Any]], None],
        on_order: Callable[[dict[str, Any]], None],
    ) -> None:
        """Connect to Bybit private WebSocket streams via pybit."""
        from pybit.unified_trading import WebSocket as BybitWS

        testnet = self._settings.testnet
        ws = BybitWS(
            testnet=testnet,
            channel_type="private",
            api_key=self._settings.api_key,
            api_secret=self._settings.api_secret,
        )

        def _handle_position(msg: dict[str, Any]) -> None:
            self._last_ws_update = time.time()
            on_position(msg)

        def _handle_wallet(msg: dict[str, Any]) -> None:
            self._last_ws_update = time.time()
            on_wallet(msg)

        def _handle_order(msg: dict[str, Any]) -> None:
            self._last_ws_update = time.time()
            on_order(msg)

        ws.position_stream(callback=_handle_position)
        ws.wallet_stream(callback=_handle_wallet)
        ws.order_stream(callback=_handle_order)

        log.info("bybit_ws_connected", testnet=testnet)

        # Keep alive until shutdown
        while not self._ws_shutdown:
            await asyncio.sleep(1)

        ws.exit()

    async def stop_private_ws(self) -> None:
        """Signal the WebSocket loop to stop."""
        self._ws_shutdown = True
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass

    @property
    def ws_is_stale(self) -> bool:
        """True if no WS update in the last 60 seconds."""
        if self._last_ws_update == 0:
            return True
        return (time.time() - self._last_ws_update) > 60.0

    # ── Lifecycle ───────────────────────────────────────────────────────

    async def close(self) -> None:
        """Gracefully shut down exchange connections."""
        await self.stop_private_ws()
        await self._exchange.close()
        log.info("exchange_client_closed")

    # ── Internal Helpers ────────────────────────────────────────────────

    @staticmethod
    def _dry_run_order(
        symbol: str,
        side: str,
        amount: float,
        price: float,
        order_type: str,
    ) -> dict[str, Any]:
        """Return a fake order response for dry-run mode."""
        return {
            "id": f"dry_{int(time.time() * 1000)}",
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "amount": amount,
            "price": price,
            "filled": amount,
            "remaining": 0.0,
            "status": "closed",
            "average": price,
            "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
            "datetime": datetime.now(timezone.utc).isoformat(),
        }
