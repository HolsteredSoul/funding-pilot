"""FastAPI dashboard application — read-only monitoring UI.

Integrates with the running bot via shared ``PositionManager``,
``DashboardState``, and ``EventBus`` references.  No database — all state
lives in memory or on disk (trades_aud.csv).
"""

from __future__ import annotations

import csv
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog
import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse
from zoneinfo import ZoneInfo

from src.config import Settings
from src.dashboard.sse import EventBus
from src.models import DashboardState
from src.position_manager import PositionManager

log = structlog.get_logger()

AWST = ZoneInfo("Australia/Perth")

# ── Paths ─────────────────────────────────────────────────────────────
_THIS_DIR = Path(__file__).resolve().parent
_TEMPLATE_DIR = _THIS_DIR / "templates"
_STATIC_DIR = _THIS_DIR / "static"


def create_app(
    settings: Settings,
    pos_mgr: PositionManager,
    dash_state: DashboardState,
    event_bus: EventBus,
) -> FastAPI:
    """Build and return the FastAPI app wired to live bot state.

    Args:
        settings:   Bot configuration.
        pos_mgr:    Shared position manager (read-only access).
        dash_state: Mutable state bag updated by the async loops.
        event_bus:  SSE fan-out bus for real-time browser updates.

    Returns:
        Configured FastAPI instance.
    """
    app = FastAPI(
        title="AU-Funding-Arb Dashboard",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
    )

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

    # ── Helpers ───────────────────────────────────────────────────────

    def _common_ctx(request: Request, page: str) -> dict[str, Any]:
        """Template context shared by every page."""
        return {
            "request": request,
            "active_page": page,
            "dry_run": settings.dry_run,
            "circuit_breaker": dash_state.circuit_breaker_active,
            "ws_connected": dash_state.ws_connected,
        }

    def _portfolio_ctx() -> dict[str, Any]:
        """Equity card context."""
        p = pos_mgr.portfolio
        dd = pos_mgr.drawdown_pct() * 100
        return {
            "equity": p.total_equity_usd,
            "peak_equity": p.peak_equity_usd,
            "drawdown": dd,
            "open_count": pos_mgr.open_count,
            "max_pairs": settings.max_concurrent_pairs,
            "total_funding": p.total_funding_collected_usd,
            "realised_pnl": p.total_realised_pnl_usd,
        }

    def _positions_ctx() -> list[dict[str, Any]]:
        """Enrich positions with display-friendly fields."""
        now = datetime.now(timezone.utc)
        result: list[dict[str, Any]] = []
        for pos in pos_mgr.positions.values():
            delta = now - pos.entry_time
            hours = int(delta.total_seconds() // 3600)
            mins = int((delta.total_seconds() % 3600) // 60)
            hold = f"{hours}h {mins}m" if hours < 48 else f"{delta.days}d {hours % 24}h"
            entry_awst = pos.entry_time.astimezone(AWST).strftime("%Y-%m-%d %H:%M")
            result.append({
                **pos.model_dump(),
                "entry_time_awst": entry_awst,
                "hold_duration": hold,
                "funding_history": pos.funding_history,
            })
        return result

    def _read_trades_csv(page: int = 1, per_page: int = 50) -> tuple[list[dict[str, str]], int]:
        """Read trades_aud.csv with pagination (newest first)."""
        csv_path = Path(settings.data_dir) / settings.trades_csv
        if not csv_path.exists():
            return [], 0

        rows: list[dict[str, str]] = []
        try:
            with csv_path.open(newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
        except Exception:
            log.warning("trades_csv_read_failed", path=str(csv_path))
            return [], 0

        rows.reverse()  # newest first
        total = len(rows)
        total_pages = max(1, math.ceil(total / per_page))
        start = (page - 1) * per_page
        return rows[start : start + per_page], total_pages

    def _settings_sections() -> dict[str, dict[str, Any]]:
        """Group settings into display sections with secrets masked."""
        s = settings
        return {
            "Exchange": {
                "exchange": s.exchange,
                "api_key": s.api_key[:6] + "****" if s.api_key else "(not set)",
                "api_secret": "****" if s.api_secret else "(not set)",
            },
            "Modes": {
                "dry_run": s.dry_run,
                "testnet": s.testnet,
                "cross_exchange_mode": s.cross_exchange_mode,
            },
            "Strategy — Entry": {
                "min_current_funding": f"{s.min_current_funding:.4%}",
                "min_profitable_cycles": s.min_profitable_cycles,
                "expected_holding_periods": s.expected_holding_periods,
                "max_concurrent_pairs": s.max_concurrent_pairs,
                "position_size_usd": f"${s.position_size_usd:,.0f}",
                "max_portfolio_pct": f"{s.max_portfolio_pct:.0%}",
            },
            "Strategy — Exit": {
                "funding_floor": f"{s.funding_floor:.4%}",
                "basis_divergence_sigma": f"{s.basis_divergence_sigma}σ",
                "hard_stop_loss_pct": f"{s.hard_stop_loss_pct:.0%}",
                "funding_negative_hours": f"{s.funding_negative_hours}h",
            },
            "Fees": {
                "spot_maker": f"{s.spot_maker_fee:.4%}",
                "spot_taker": f"{s.spot_taker_fee:.4%}",
                "perp_maker": f"{s.perp_maker_fee:.4%}",
                "perp_taker": f"{s.perp_taker_fee:.4%}",
                "round_trip_maker": f"{s.round_trip_maker_fee:.4%}",
            },
            "Loop Intervals": {
                "scan_interval_min": f"{s.scan_interval_min} min",
                "health_interval_s": f"{s.health_interval_s}s",
                "settlement_interval_h": f"{s.settlement_interval_h}h",
            },
            "Dashboard": {
                "dashboard_enabled": s.dashboard_enabled,
                "dashboard_port": s.dashboard_port,
            },
        }

    # ── HTML Routes ───────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        """Dashboard home page."""
        ctx = {
            **_common_ctx(request, "index"),
            **_portfolio_ctx(),
            "positions": _positions_ctx(),
            "equity_timestamps": [],  # populated by SSE in JS
            "equity_values": [],
        }
        return templates.TemplateResponse("index.html", ctx)

    @app.get("/positions", response_class=HTMLResponse)
    async def positions_page(request: Request) -> HTMLResponse:
        """Detailed position view."""
        ctx = {
            **_common_ctx(request, "positions"),
            "positions": _positions_ctx(),
        }
        return templates.TemplateResponse("positions.html", ctx)

    @app.get("/trades", response_class=HTMLResponse)
    async def trades_page(request: Request, page: int = Query(1, ge=1)) -> HTMLResponse:
        """Trade history with pagination."""
        trades, total_pages = _read_trades_csv(page)
        ctx = {
            **_common_ctx(request, "trades"),
            "trades": trades,
            "page": page,
            "total_pages": total_pages,
        }
        return templates.TemplateResponse("trades.html", ctx)

    @app.get("/scanner", response_class=HTMLResponse)
    async def scanner_page(request: Request) -> HTMLResponse:
        """Latest scanner results."""
        ctx = {
            **_common_ctx(request, "scanner"),
            "scan_results": dash_state.last_scan_results,
            "last_scan_time": (
                dash_state.last_scan_time.astimezone(AWST).strftime("%Y-%m-%d %H:%M AWST")
                if dash_state.last_scan_time
                else None
            ),
            "min_funding": settings.min_current_funding,
        }
        return templates.TemplateResponse("scanner.html", ctx)

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request) -> HTMLResponse:
        """Read-only config view."""
        ctx = {
            **_common_ctx(request, "settings"),
            "settings_sections": _settings_sections(),
        }
        return templates.TemplateResponse("settings.html", ctx)

    # ── SSE Endpoint ──────────────────────────────────────────────────

    @app.get("/api/sse")
    async def sse_stream(request: Request) -> EventSourceResponse:
        """Server-Sent Events stream for real-time dashboard updates."""
        return EventSourceResponse(event_bus.subscribe())

    # ── JSON API ──────────────────────────────────────────────────────

    @app.get("/api/portfolio")
    async def api_portfolio() -> JSONResponse:
        """Raw portfolio state as JSON."""
        return JSONResponse(_portfolio_ctx())

    @app.get("/api/positions")
    async def api_positions() -> JSONResponse:
        """Raw position list as JSON."""
        return JSONResponse(
            {"positions": [p.model_dump(mode="json") for p in pos_mgr.positions.values()]}
        )

    @app.get("/health")
    async def health() -> JSONResponse:
        """Health check endpoint."""
        uptime = ""
        if dash_state.bot_start_time:
            delta = datetime.now(timezone.utc) - dash_state.bot_start_time
            uptime = f"{delta.days}d {delta.seconds // 3600}h"
        return JSONResponse({
            "status": "ok",
            "uptime": uptime,
            "loops": dash_state.loop_status,
            "open_positions": pos_mgr.open_count,
            "circuit_breaker": dash_state.circuit_breaker_active,
            "dry_run": settings.dry_run,
        })

    return app


async def run_dashboard(
    settings: Settings,
    pos_mgr: PositionManager,
    dash_state: DashboardState,
    event_bus: EventBus,
    shutdown: "asyncio.Event",  # noqa: F821 — forward ref
) -> None:
    """Start uvicorn inside the existing asyncio event loop.

    Runs until *shutdown* is set, then gracefully stops.
    """
    import asyncio

    app = create_app(settings, pos_mgr, dash_state, event_bus)

    config = uvicorn.Config(
        app=app,
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)

    # Prevent uvicorn from installing its own signal handlers (we have ours).
    server.install_signal_handlers = lambda: None  # type: ignore[assignment]

    log.info(
        "dashboard_starting",
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        url=f"http://localhost:{settings.dashboard_port}",
    )

    # Run server in background; stop when shutdown is set.
    serve_task = asyncio.create_task(server.serve())

    await shutdown.wait()
    server.should_exit = True

    try:
        await asyncio.wait_for(serve_task, timeout=5.0)
    except asyncio.TimeoutError:
        serve_task.cancel()
    except Exception:
        pass

    log.info("dashboard_stopped")
