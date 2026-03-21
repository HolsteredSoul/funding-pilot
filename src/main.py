"""AU-Funding-Arb v1.0 — Entry point.

Starts the three async loops, handles startup resume and SIGTERM persistence.

Loops:
    1. Funding Scanner     — every 60 min, discover & open new hedges
    2. Position Health     — every 30s, monitor all open hedges via WS/REST
    3. Settlement Evaluator — every 8h (aligned), evaluate exits & record funding

SIGTERM behaviour:
    - Persist all open positions to positions.json
    - Do NOT close any hedges
    - Exit cleanly

Startup behaviour:
    - Load positions.json if it exists
    - Reconcile against exchange state
    - Resume health monitoring before scanning for new entries
"""

from __future__ import annotations

import asyncio
import signal
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from src.arb_engine import ArbEngine
from src.config import Settings
from src.dashboard.app import run_dashboard
from src.dashboard.sse import EventBus
from src.exchange_client import ExchangeClient
from src.models import DashboardState, HedgePosition
from src.order_executor import OrderExecutor
from src.position_manager import PositionManager
from src.tax_logger import RbaRateCache, TaxLogger
from src.telegram_bot import TelegramAlerter

# ── Structured Logging Setup ───────────────────────────────────────────
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(0),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)

log = structlog.get_logger()

AWST = ZoneInfo("Australia/Perth")


# ═══════════════════════════════════════════════════════════════════════
# Loop 1 — Funding Scanner
# ═══════════════════════════════════════════════════════════════════════

async def funding_scanner_loop(
    engine: ArbEngine,
    executor: OrderExecutor,
    pos_mgr: PositionManager,
    tax: TaxLogger,
    telegram: TelegramAlerter,
    settings: Settings,
    shutdown: asyncio.Event,
    dash_state: DashboardState | None = None,
    event_bus: EventBus | None = None,
) -> None:
    """Scan for funding opportunities and open new hedges."""
    log.info("loop_started", loop="funding_scanner", interval_min=settings.scan_interval_min)

    while not shutdown.is_set():
        try:
            # Circuit breaker check
            if await engine.should_circuit_break():
                await telegram.send_circuit_breaker_alert(pos_mgr.portfolio)
                # Emergency unwind all positions
                for pos in list(pos_mgr.positions.values()):
                    if not settings.dry_run:
                        await executor.emergency_unwind(pos)
                    await pos_mgr.remove_position(pos.pair_id)
                # Pause 5 min after circuit break
                try:
                    await asyncio.wait_for(shutdown.wait(), timeout=300)
                except asyncio.TimeoutError:
                    pass
                continue

            # Scan for opportunities
            opportunities = await engine.scan_funding_opportunities()

            # Publish scan results to dashboard
            if dash_state is not None:
                dash_state.last_scan_results = opportunities
                dash_state.last_scan_time = datetime.now(timezone.utc)
                dash_state.loop_status["scanner"] = "ok"
            if event_bus is not None:
                await event_bus.publish("portfolio_update", _build_portfolio_event(pos_mgr, settings, dash_state))

            for opp in opportunities:
                if shutdown.is_set():
                    break
                if pos_mgr.open_count >= settings.max_concurrent_pairs:
                    break

                size = engine.compute_position_size()

                if settings.dry_run:
                    log.info(
                        "dry_run_would_open",
                        pair=opp.pair_id,
                        size_usd=size,
                        funding_rate=f"{opp.current_rate:.4%}",
                        net_profit=f"{opp.net_profit:.4%}",
                    )
                    # Log to tax CSV even in dry run for testing
                    await tax.log_trade(
                        pair=opp.pair_id,
                        side="buy",
                        qty=size / opp.mark_price if opp.mark_price > 0 else 0,
                        price_usd=opp.mark_price,
                        fee_usd=0,
                        tx_type="open",
                    )
                    continue

                # Open the hedge
                pos = await executor.open_hedge(opp, size)
                if pos is not None:
                    await pos_mgr.add_position(pos)

                    # Log both legs to tax CSV
                    spot_fee = pos.spot_qty * pos.spot_entry_price * settings.spot_maker_fee
                    perp_fee = pos.perp_qty * pos.perp_entry_price * settings.perp_maker_fee

                    await tax.log_trade(
                        pair=pos.pair_id,
                        side="buy",
                        qty=pos.spot_qty,
                        price_usd=pos.spot_entry_price,
                        fee_usd=spot_fee,
                        tx_type="open",
                    )
                    await tax.log_trade(
                        pair=pos.pair_id,
                        side="sell",
                        qty=pos.perp_qty,
                        price_usd=pos.perp_entry_price,
                        fee_usd=perp_fee,
                        tx_type="open",
                    )
                    await telegram.send_open_alert(pos, opp)

        except Exception:
            log.exception("scanner_loop_error")

        # Sleep until next scan, but wake on shutdown
        try:
            await asyncio.wait_for(
                shutdown.wait(), timeout=settings.scan_interval_min * 60
            )
        except asyncio.TimeoutError:
            pass


# ═══════════════════════════════════════════════════════════════════════
# Loop 2 — Position Health Monitor
# ═══════════════════════════════════════════════════════════════════════

async def health_monitor_loop(
    client: ExchangeClient,
    pos_mgr: PositionManager,
    engine: ArbEngine,
    executor: OrderExecutor,
    telegram: TelegramAlerter,
    settings: Settings,
    shutdown: asyncio.Event,
    dash_state: DashboardState | None = None,
    event_bus: EventBus | None = None,
) -> None:
    """Monitor position health via WebSocket (REST fallback)."""
    log.info("loop_started", loop="health_monitor", interval_s=settings.health_interval_s)

    # WebSocket state (updated by callbacks)
    ws_positions: dict[str, Any] = {}
    ws_wallet: dict[str, Any] = {}

    def on_position(msg: dict[str, Any]) -> None:
        data = msg.get("data", [])
        for item in data if isinstance(data, list) else [data]:
            sym = item.get("symbol", "")
            ws_positions[sym] = item

    def on_wallet(msg: dict[str, Any]) -> None:
        ws_wallet.update(msg)

    def on_order(msg: dict[str, Any]) -> None:
        pass  # Order updates handled by executor's polling

    # Start WebSocket in background
    try:
        await client.start_private_ws(on_position, on_wallet, on_order)
    except Exception:
        log.warning("ws_start_failed_using_rest_only")

    while not shutdown.is_set():
        try:
            if not pos_mgr.positions:
                # No positions — just update equity
                try:
                    equity = await client.fetch_total_equity()
                    await pos_mgr.update_portfolio_equity(equity)
                except Exception:
                    pass
            else:
                # Prefer WS data, fall back to REST if stale
                if client.ws_is_stale:
                    try:
                        exchange_positions = await client.fetch_positions()
                        balance = await client.fetch_balance()
                        equity = float(balance.get("total", {}).get("USDT", 0))
                    except Exception:
                        log.warning("health_rest_fallback_failed")
                        exchange_positions = []
                        equity = pos_mgr.portfolio.total_equity_usd
                else:
                    exchange_positions = []  # Use WS data
                    equity = pos_mgr.portfolio.total_equity_usd

                await pos_mgr.update_portfolio_equity(equity)

                # Check each position
                for pair_id, pos in list(pos_mgr.positions.items()):
                    # Verify perp leg still exists on exchange
                    if exchange_positions:
                        perp_exists = any(
                            p["symbol"] == pos.perp_symbol for p in exchange_positions
                        )
                        if not perp_exists:
                            warning = (
                                f"Perp leg missing for {pair_id} — "
                                f"may have been liquidated or manually closed"
                            )
                            log.critical("leg_missing", pair=pair_id)
                            await telegram.send_health_alert(warning)

                # Circuit breaker
                if await engine.should_circuit_break():
                    if dash_state is not None:
                        dash_state.circuit_breaker_active = True
                    await telegram.send_circuit_breaker_alert(pos_mgr.portfolio)
                    if not settings.dry_run:
                        for pos in list(pos_mgr.positions.values()):
                            await executor.emergency_unwind(pos)
                            await pos_mgr.remove_position(pos.pair_id)

            # Publish health update to dashboard
            if dash_state is not None:
                dash_state.loop_status["health"] = "ok"
                dash_state.ws_connected = not client.ws_is_stale
            if event_bus is not None:
                await event_bus.publish("portfolio_update", _build_portfolio_event(pos_mgr, settings, dash_state))
                await event_bus.publish("positions_update", {
                    "html": True,  # HTMX swap trigger
                })

        except Exception:
            log.exception("health_monitor_error")
            if dash_state is not None:
                dash_state.loop_status["health"] = "error"

        try:
            await asyncio.wait_for(
                shutdown.wait(), timeout=settings.health_interval_s
            )
        except asyncio.TimeoutError:
            pass


# ═══════════════════════════════════════════════════════════════════════
# Loop 3 — Settlement / Exit Evaluator
# ═══════════════════════════════════════════════════════════════════════

async def settlement_evaluator_loop(
    engine: ArbEngine,
    executor: OrderExecutor,
    pos_mgr: PositionManager,
    tax: TaxLogger,
    telegram: TelegramAlerter,
    settings: Settings,
    shutdown: asyncio.Event,
) -> None:
    """Evaluate exits and record funding payments at settlement times."""
    log.info("loop_started", loop="settlement_evaluator")

    # Align to next 8h settlement (00:00, 08:00, 16:00 UTC)
    await _sleep_until_next_settlement(shutdown)

    while not shutdown.is_set():
        try:
            positions = list(pos_mgr.positions.values())

            if positions:
                # Evaluate exit conditions
                exits = await engine.evaluate_exits(positions)

                for pos, reason in exits:
                    if settings.dry_run:
                        log.info(
                            "dry_run_would_close",
                            pair=pos.pair_id,
                            reason=reason,
                        )
                        continue

                    success = await executor.close_hedge(pos, reason)
                    if success:
                        # Log close trades
                        spot_fee = pos.spot_qty * pos.spot_entry_price * settings.spot_maker_fee
                        perp_fee = pos.perp_qty * pos.perp_entry_price * settings.perp_maker_fee

                        await tax.log_trade(
                            pair=pos.pair_id,
                            side="sell",
                            qty=pos.spot_qty,
                            price_usd=pos.spot_entry_price,
                            fee_usd=spot_fee,
                            tx_type="close",
                            pnl_usd=pos.cumulative_pnl_usd,
                        )
                        await tax.log_trade(
                            pair=pos.pair_id,
                            side="buy",
                            qty=pos.perp_qty,
                            price_usd=pos.perp_entry_price,
                            fee_usd=perp_fee,
                            tx_type="close",
                        )
                        await telegram.send_close_alert(
                            pos, reason, pos.cumulative_pnl_usd
                        )
                        await pos_mgr.remove_position(pos.pair_id)

                # Record funding payments for surviving positions
                for pos in list(pos_mgr.positions.values()):
                    try:
                        from src.exchange_client import ExchangeClient

                        # Fetch latest funding for this symbol
                        # (We access the client through the engine)
                        history = await engine._client.fetch_funding_rate_history(
                            pos.perp_symbol, limit=1
                        )
                        if history:
                            latest = history[0]
                            rate = float(latest.get("fundingRate", 0))
                            # Funding amount = position_size × rate
                            amount = pos.perp_qty * pos.perp_entry_price * rate
                            await pos_mgr.update_funding(pos.pair_id, amount, rate)

                            # Log funding payment to tax CSV
                            await tax.log_trade(
                                pair=pos.pair_id,
                                side="buy" if amount >= 0 else "sell",
                                qty=abs(amount),
                                price_usd=1.0,
                                fee_usd=0,
                                tx_type="funding",
                            )
                    except Exception:
                        log.warning("funding_record_failed", pair=pos.pair_id)

        except Exception:
            log.exception("settlement_evaluator_error")

        # Sleep until next settlement
        await _sleep_until_next_settlement(shutdown)


def _build_portfolio_event(
    pos_mgr: PositionManager,
    settings: Settings,
    dash_state: DashboardState | None,
) -> dict[str, Any]:
    """Build the SSE payload dict for a portfolio_update event."""
    p = pos_mgr.portfolio
    dd = pos_mgr.drawdown_pct() * 100
    return {
        "equity": p.total_equity_usd,
        "peak_equity": p.peak_equity_usd,
        "drawdown": round(dd, 2),
        "open_count": pos_mgr.open_count,
        "max_pairs": settings.max_concurrent_pairs,
        "total_funding": p.total_funding_collected_usd,
        "realised_pnl": p.total_realised_pnl_usd,
        "dry_run": settings.dry_run,
        "circuit_breaker": dash_state.circuit_breaker_active if dash_state else False,
        "ws_connected": dash_state.ws_connected if dash_state else False,
    }


async def _sleep_until_next_settlement(shutdown: asyncio.Event) -> None:
    """Sleep until the next 8h funding settlement (00:00, 08:00, 16:00 UTC)."""
    now = datetime.now(timezone.utc)
    current_hour = now.hour
    # Next settlement hour
    settlement_hours = [0, 8, 16]
    next_hour = None
    for h in settlement_hours:
        if h > current_hour:
            next_hour = h
            break
    if next_hour is None:
        next_hour = settlement_hours[0]  # wrap to tomorrow 00:00

    target = now.replace(hour=next_hour, minute=1, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)

    delay = (target - now).total_seconds()
    log.info("settlement_sleep", next_settlement=target.isoformat(), delay_s=int(delay))

    try:
        await asyncio.wait_for(shutdown.wait(), timeout=delay)
    except asyncio.TimeoutError:
        pass


# ═══════════════════════════════════════════════════════════════════════
# Daily Recap Loop
# ═══════════════════════════════════════════════════════════════════════

async def daily_recap_loop(
    telegram: TelegramAlerter,
    pos_mgr: PositionManager,
    settings: Settings,
    shutdown: asyncio.Event,
) -> None:
    """Send daily recap at 7am AWST."""
    log.info("loop_started", loop="daily_recap", hour_awst=settings.daily_recap_hour_awst)

    while not shutdown.is_set():
        now_awst = datetime.now(AWST)
        target = now_awst.replace(
            hour=settings.daily_recap_hour_awst,
            minute=0,
            second=0,
            microsecond=0,
        )
        if target <= now_awst:
            target += timedelta(days=1)

        delay = (target - now_awst).total_seconds()
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

        if shutdown.is_set():
            break

        # Send recap
        try:
            await telegram.send_daily_recap(
                pos_mgr.portfolio,
                list(pos_mgr.positions.values()),
            )
        except Exception:
            log.exception("daily_recap_error")


# ═══════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════

async def run() -> None:
    """Wire all components and start the bot."""
    settings = Settings()

    log.info(
        "bot_starting",
        exchange=settings.exchange,
        dry_run=settings.dry_run,
        testnet=settings.testnet,
        max_pairs=settings.max_concurrent_pairs,
        position_size=settings.position_size_usd,
    )

    # Initialise components
    client = ExchangeClient(settings)
    pos_mgr = PositionManager(settings)
    rba = RbaRateCache(settings)
    tax = TaxLogger(settings, rba)
    telegram = TelegramAlerter(settings)
    engine = ArbEngine(settings, client, pos_mgr)
    executor = OrderExecutor(settings, client)

    # Dashboard state (shared with web UI)
    dash_state = DashboardState(bot_start_time=datetime.now(timezone.utc))
    event_bus = EventBus()

    shutdown = asyncio.Event()

    # ── SIGTERM / SIGINT Handler ────────────────────────────────────
    def _shutdown_handler(sig: int, frame: Any) -> None:
        sig_name = signal.Signals(sig).name
        log.info("shutdown_signal_received", signal=sig_name)
        pos_mgr.save_to_file()
        shutdown.set()

    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

    try:
        # ── Startup ─────────────────────────────────────────────────
        await client.load_markets()
        await rba.refresh()

        # Resume persisted positions
        resumed = pos_mgr.load_from_file()
        if resumed:
            log.info("resuming_positions", count=len(resumed))
            await pos_mgr.reconcile_with_exchange(client)

        # Fetch initial equity
        try:
            equity = await client.fetch_total_equity()
            await pos_mgr.update_portfolio_equity(equity)
        except Exception:
            log.warning("initial_equity_fetch_failed")

        await telegram.send_startup_alert(
            len(resumed), pos_mgr.portfolio.total_equity_usd
        )

        # ── Build task list ───────────────────────────────────────
        tasks = [
            funding_scanner_loop(
                engine, executor, pos_mgr, tax, telegram, settings, shutdown,
                dash_state=dash_state, event_bus=event_bus,
            ),
            health_monitor_loop(
                client, pos_mgr, engine, executor, telegram, settings, shutdown,
                dash_state=dash_state, event_bus=event_bus,
            ),
            settlement_evaluator_loop(
                engine, executor, pos_mgr, tax, telegram, settings, shutdown
            ),
            daily_recap_loop(telegram, pos_mgr, settings, shutdown),
        ]

        # Dashboard (optional — runs uvicorn inside the event loop)
        if settings.dashboard_enabled:
            tasks.append(
                run_dashboard(settings, pos_mgr, dash_state, event_bus, shutdown)
            )

        # ── Run all loops concurrently ──────────────────────────────
        await asyncio.gather(*tasks, return_exceptions=True)

    finally:
        # ── Graceful Shutdown ───────────────────────────────────────
        log.info("shutting_down")
        pos_mgr.save_to_file()
        await telegram.send_shutdown_alert(pos_mgr.open_count)
        await client.close()
        log.info("shutdown_complete")


def main() -> None:
    """Sync entry point."""
    asyncio.run(run())


if __name__ == "__main__":
    main()
