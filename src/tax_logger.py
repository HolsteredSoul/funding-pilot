"""ATO-compliant tax logging — RBA daily rate + per-trade AUD CSV.

Every trade, funding payment, and fee is logged with:
    - UTC and AWST timestamps
    - USD values converted to AUD using the official RBA daily exchange rate
    - Format ready for Koinly / CryptoTaxCalculator import

RBA rate source: https://www.rba.gov.au/statistics/tables/csv/f11-data.csv
The CSV contains AUD/USD (how many USD per 1 AUD). To convert:
    aud_value = usd_value / aud_usd_rate
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp
import pandas as pd
import structlog

from src.config import Settings
from src.models import TradeRecord

log = structlog.get_logger()

RBA_CSV_URL = "https://www.rba.gov.au/statistics/tables/csv/f11-data.csv"

# Column in the RBA CSV that contains the AUD/USD rate
RBA_USD_SERIES_ID = "FXRUSD"

AWST = ZoneInfo("Australia/Perth")

# CSV header for trades_aud.csv
TRADES_CSV_HEADER = [
    "timestamp_utc",
    "timestamp_awst",
    "pair",
    "side",
    "qty",
    "price_usd",
    "amount_usd",
    "fee_usd",
    "rba_aud_rate",
    "aud_value",
    "fee_aud",
    "exchange",
    "tx_type",
    "pnl_usd",
    "pnl_aud",
]


class RbaRateCache:
    """Fetches and caches the RBA daily AUD/USD exchange rate.

    Strategy:
        - On startup, download the full RBA CSV (~200KB, years of data).
        - Parse into a date → rate mapping; forward-fill weekends/holidays.
        - Refresh once per day at ~17:00 AWST (RBA publishes ~16:00 AEST).
        - On fetch failure, fall back to locally cached rba_rates.csv.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._data_dir = Path(settings.data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._cache_path = self._data_dir / settings.rba_cache_file
        self._rates: dict[date, float] = {}
        self._last_refresh: Optional[date] = None

    async def refresh(self) -> None:
        """Download and parse the RBA f11 CSV. Falls back to local cache."""
        try:
            raw_csv = await self._download_rba_csv()
            self._rates = self._parse_rba_csv(raw_csv)
            # Save to local cache for offline fallback
            self._cache_path.write_text(raw_csv)
            self._last_refresh = date.today()
            log.info("rba_rates_refreshed", num_dates=len(self._rates))
        except Exception:
            log.warning("rba_download_failed_using_cache")
            self._load_from_cache()

    def _load_from_cache(self) -> None:
        """Load rates from locally cached CSV file."""
        if not self._cache_path.exists():
            log.error("rba_no_cache_available")
            return
        try:
            raw = self._cache_path.read_text()
            self._rates = self._parse_rba_csv(raw)
            log.info("rba_rates_loaded_from_cache", num_dates=len(self._rates))
        except Exception:
            log.exception("rba_cache_parse_failed")

    async def _download_rba_csv(self) -> str:
        """Download the RBA f11 exchange rates CSV."""
        async with aiohttp.ClientSession() as session:
            async with session.get(RBA_CSV_URL, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                resp.raise_for_status()
                return await resp.text()

    def _parse_rba_csv(self, raw_csv: str) -> dict[date, float]:
        """Parse the RBA CSV and extract AUD/USD rates.

        The CSV has a multi-row header. The first column is the date,
        and we need to find the column with Series ID = FXRUSD.
        """
        lines = raw_csv.strip().split("\n")

        # Find the header row containing "Series ID"
        series_id_row_idx: Optional[int] = None
        data_start_idx: Optional[int] = None
        usd_col_idx: Optional[int] = None

        for i, line in enumerate(lines):
            if "Series ID" in line:
                series_id_row_idx = i
                # Parse the series IDs to find FXRUSD column
                parts = line.split(",")
                for j, part in enumerate(parts):
                    if RBA_USD_SERIES_ID in part.strip():
                        usd_col_idx = j
                        break
            # Data rows start after the header block (usually row with "Units")
            if series_id_row_idx is not None and i > series_id_row_idx + 2:
                # First row that looks like a date
                if line and line[0].isdigit():
                    data_start_idx = i
                    break

        if usd_col_idx is None or data_start_idx is None:
            log.error("rba_csv_parse_structure_error")
            return {}

        rates: dict[date, float] = {}
        for line in lines[data_start_idx:]:
            parts = line.split(",")
            if len(parts) <= usd_col_idx:
                continue
            try:
                dt = self._parse_rba_date(parts[0].strip())
                rate_str = parts[usd_col_idx].strip()
                if rate_str:
                    rates[dt] = float(rate_str)
            except (ValueError, IndexError):
                continue

        # Forward-fill: create entries for weekends/holidays
        if rates:
            rates = self._forward_fill(rates)

        return rates

    @staticmethod
    def _parse_rba_date(date_str: str) -> date:
        """Parse RBA date format: '02-Jan-2025' or '2025-01-02'."""
        for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(date_str, fmt).date()
            except ValueError:
                continue
        raise ValueError(f"Cannot parse RBA date: {date_str}")

    @staticmethod
    def _forward_fill(rates: dict[date, float]) -> dict[date, float]:
        """Fill gaps (weekends, holidays) by carrying the last known rate."""
        if not rates:
            return rates
        sorted_dates = sorted(rates.keys())
        filled: dict[date, float] = {}
        current = sorted_dates[0]
        end = date.today()
        last_rate = rates[current]

        from datetime import timedelta

        while current <= end:
            if current in rates:
                last_rate = rates[current]
            filled[current] = last_rate
            current += timedelta(days=1)

        return filled

    async def get_rate(self, dt: datetime) -> float:
        """Get the AUD/USD rate for a given datetime.

        Uses the date portion (in AWST) to look up the rate.
        For dates before the daily rate is published, uses yesterday's rate.

        Args:
            dt: A timezone-aware datetime.

        Returns:
            AUD/USD rate (e.g. 0.6500 means 1 AUD = 0.65 USD).
        """
        # Refresh if we haven't today
        today = date.today()
        if self._last_refresh != today:
            await self.refresh()

        # Convert to AWST date for lookup
        awst_dt = dt.astimezone(AWST)
        lookup_date = awst_dt.date()

        # Try today, then yesterday (in case rate not yet published)
        from datetime import timedelta

        for offset in range(0, 7):  # look back up to a week
            d = lookup_date - timedelta(days=offset)
            if d in self._rates:
                return self._rates[d]

        log.warning("rba_rate_not_found", date=str(lookup_date))
        # Last resort: return most recent known rate
        if self._rates:
            latest = max(self._rates.keys())
            return self._rates[latest]
        return 0.6500  # fallback default


class TaxLogger:
    """Appends trade records to trades_aud.csv with AUD conversion."""

    def __init__(self, settings: Settings, rba_cache: RbaRateCache) -> None:
        self._settings = settings
        self._rba = rba_cache
        self._data_dir = Path(settings.data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._csv_path = self._data_dir / settings.trades_csv
        self._ensure_csv_header()

    def _ensure_csv_header(self) -> None:
        """Create the CSV file with header if it doesn't exist."""
        if not self._csv_path.exists():
            with open(self._csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(TRADES_CSV_HEADER)

    async def log_trade(
        self,
        pair: str,
        side: str,
        qty: float,
        price_usd: float,
        fee_usd: float,
        tx_type: str,
        pnl_usd: Optional[float] = None,
    ) -> TradeRecord:
        """Log a single trade event with AUD conversion.

        Args:
            pair: Pair identifier, e.g. "BTCUSDT".
            side: "buy" or "sell".
            qty: Quantity traded.
            price_usd: Execution price in USD.
            fee_usd: Fee paid in USD.
            tx_type: "open", "close", "funding", or "fee".
            pnl_usd: Realised PnL in USD (for close trades).

        Returns:
            The TradeRecord that was logged.
        """
        now_utc = datetime.now(timezone.utc)
        now_awst = now_utc.astimezone(AWST)
        amount_usd = qty * price_usd

        # Get RBA rate
        aud_rate = await self._rba.get_rate(now_utc)

        # Convert USD to AUD: aud = usd / aud_usd_rate
        aud_value = amount_usd / aud_rate if aud_rate > 0 else 0.0
        fee_aud = fee_usd / aud_rate if aud_rate > 0 else 0.0
        pnl_aud = pnl_usd / aud_rate if pnl_usd is not None and aud_rate > 0 else None

        record = TradeRecord(
            timestamp_utc=now_utc,
            timestamp_awst=now_awst,
            pair=pair,
            side=side,
            qty=qty,
            price_usd=price_usd,
            amount_usd=amount_usd,
            fee_usd=fee_usd,
            rba_aud_rate=aud_rate,
            aud_value=aud_value,
            fee_aud=fee_aud,
            exchange=self._settings.exchange,
            tx_type=tx_type,
            pnl_usd=pnl_usd,
            pnl_aud=pnl_aud,
        )

        self._append_to_csv(record)
        log.info(
            "trade_logged",
            pair=pair,
            tx_type=tx_type,
            amount_usd=f"{amount_usd:.2f}",
            aud_value=f"{aud_value:.2f}",
        )
        return record

    def _append_to_csv(self, record: TradeRecord) -> None:
        """Append a single record to the CSV file."""
        with open(self._csv_path, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    record.timestamp_utc.isoformat(),
                    record.timestamp_awst.strftime("%Y-%m-%d %H:%M:%S %Z"),
                    record.pair,
                    record.side,
                    f"{record.qty:.8f}",
                    f"{record.price_usd:.4f}",
                    f"{record.amount_usd:.4f}",
                    f"{record.fee_usd:.6f}",
                    f"{record.rba_aud_rate:.6f}",
                    f"{record.aud_value:.4f}",
                    f"{record.fee_aud:.6f}",
                    record.exchange,
                    record.tx_type,
                    f"{record.pnl_usd:.4f}" if record.pnl_usd is not None else "",
                    f"{record.pnl_aud:.4f}" if record.pnl_aud is not None else "",
                ]
            )

    @property
    def csv_path(self) -> Path:
        """Return the path to the trades CSV."""
        return self._csv_path
