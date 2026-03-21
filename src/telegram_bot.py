"""Telegram alerting — outbound-only via Bot API HTTP POST.

No framework needed — just aiohttp POST to api.telegram.org.
Silently no-ops if token/chat_id not configured.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

import aiohttp
import structlog

from src.config import Settings
from src.models import FundingSnapshot, HedgePosition, PortfolioState

log = structlog.get_logger()

AWST = ZoneInfo("Australia/Perth")


class TelegramAlerter:
    """Send alerts and daily recaps to a Telegram chat."""

    def __init__(self, settings: Settings) -> None:
        self._token = settings.telegram_token
        self._chat_id = settings.telegram_chat_id
        self._enabled = bool(self._token and self._chat_id)
        if not self._enabled:
            log.info("telegram_disabled_no_credentials")

    # ── Core Send ───────────────────────────────────────────────────────

    async def send_alert(self, message: str) -> None:
        """Send a plain-text message to the configured Telegram chat."""
        if not self._enabled:
            return
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        log.warning(
                            "telegram_send_failed",
                            status=resp.status,
                            body=body[:200],
                        )
        except Exception:
            log.warning("telegram_send_error")

    # ── Structured Alerts ───────────────────────────────────────────────

    async def send_open_alert(
        self, pos: HedgePosition, snapshot: FundingSnapshot
    ) -> None:
        """Alert when a new hedge is opened."""
        msg = (
            f"<b>NEW HEDGE OPENED</b>\n\n"
            f"Pair: <code>{pos.pair_id}</code>\n"
            f"Notional: <code>${pos.notional_usd:,.2f}</code>\n"
            f"Spot buy: <code>{pos.spot_qty:.6f}</code> @ ${pos.spot_entry_price:,.2f}\n"
            f"Perp short: <code>{pos.perp_qty:.6f}</code> @ ${pos.perp_entry_price:,.2f}\n"
            f"Current funding: <code>{snapshot.current_rate:.4%}</code>\n"
            f"Trailing avg: <code>{snapshot.trailing_avg:.4%}</code>\n"
            f"Net profit est: <code>{snapshot.net_profit:.4%}</code>\n"
            f"Settlement: every <code>{snapshot.settlement_interval_h}h</code>"
        )
        await self.send_alert(msg)

    async def send_close_alert(
        self, pos: HedgePosition, reason: str, pnl_usd: float
    ) -> None:
        """Alert when a hedge is closed."""
        emoji = "+" if pnl_usd >= 0 else ""
        hold_time = datetime.utcnow() - pos.entry_time.replace(tzinfo=None)
        hours = hold_time.total_seconds() / 3600

        msg = (
            f"<b>HEDGE CLOSED</b>\n\n"
            f"Pair: <code>{pos.pair_id}</code>\n"
            f"Reason: <code>{reason}</code>\n"
            f"PnL: <code>{emoji}${pnl_usd:,.2f}</code>\n"
            f"Funding collected: <code>${pos.cumulative_funding:,.2f}</code>\n"
            f"Hold time: <code>{hours:.1f}h</code>"
        )
        await self.send_alert(msg)

    async def send_health_alert(self, warning: str) -> None:
        """Alert on position health issues."""
        msg = f"<b>HEALTH WARNING</b>\n\n{warning}"
        await self.send_alert(msg)

    async def send_circuit_breaker_alert(self, portfolio: PortfolioState) -> None:
        """Alert when circuit breaker triggers."""
        drawdown = 0.0
        if portfolio.peak_equity_usd > 0:
            drawdown = (
                (portfolio.peak_equity_usd - portfolio.total_equity_usd)
                / portfolio.peak_equity_usd
            )
        msg = (
            f"<b>CIRCUIT BREAKER TRIGGERED</b>\n\n"
            f"Equity: <code>${portfolio.total_equity_usd:,.2f}</code>\n"
            f"Peak: <code>${portfolio.peak_equity_usd:,.2f}</code>\n"
            f"Drawdown: <code>{drawdown:.2%}</code>\n\n"
            f"All positions are being emergency-unwound."
        )
        await self.send_alert(msg)

    async def send_daily_recap(
        self,
        portfolio: PortfolioState,
        positions: list[HedgePosition],
        daily_funding_usd: float = 0.0,
        daily_pnl_usd: float = 0.0,
    ) -> None:
        """7am AWST daily recap with portfolio stats."""
        now_awst = datetime.now(AWST)

        lines = [
            f"<b>DAILY RECAP — {now_awst.strftime('%d %b %Y')}</b>\n",
            f"Equity: <code>${portfolio.total_equity_usd:,.2f}</code>",
            f"Peak: <code>${portfolio.peak_equity_usd:,.2f}</code>",
            f"Funding today: <code>${daily_funding_usd:,.2f}</code>",
            f"PnL today: <code>${daily_pnl_usd:,.2f}</code>",
            f"Active pairs: <code>{len(positions)}</code>",
        ]

        if positions:
            lines.append("\n<b>Open Positions:</b>")
            for pos in positions:
                hold_h = (
                    datetime.utcnow() - pos.entry_time.replace(tzinfo=None)
                ).total_seconds() / 3600
                lines.append(
                    f"  {pos.pair_id}: funding=${pos.cumulative_funding:,.2f} "
                    f"pnl=${pos.cumulative_pnl_usd:,.2f} "
                    f"hold={hold_h:.0f}h"
                )

        # Compute realised APY if we have enough data
        if portfolio.total_funding_collected_usd > 0 and portfolio.total_equity_usd > 0:
            # Rough annualised: (funding / equity) * (365 / days_active)
            lines.append(
                f"\nTotal funding: <code>${portfolio.total_funding_collected_usd:,.2f}</code>"
            )

        await self.send_alert("\n".join(lines))

    async def send_startup_alert(self, num_resumed: int, equity: float) -> None:
        """Alert on bot startup."""
        msg = (
            f"<b>BOT STARTED</b>\n\n"
            f"Resumed positions: <code>{num_resumed}</code>\n"
            f"Equity: <code>${equity:,.2f}</code>"
        )
        await self.send_alert(msg)

    async def send_shutdown_alert(self, num_positions: int) -> None:
        """Alert on graceful shutdown."""
        msg = (
            f"<b>BOT SHUTTING DOWN</b>\n\n"
            f"Persisted positions: <code>{num_positions}</code>\n"
            f"Hedges are NOT closed — will resume on restart."
        )
        await self.send_alert(msg)
