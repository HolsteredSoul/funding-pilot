"""Historical funding rate backtest simulator.

Simulates the delta-neutral funding arb strategy on historical data
to estimate APY, drawdown, and trade frequency.

Usage:
    python -m backtest.backtest
    python -m backtest.backtest --data-dir backtest/data --pairs BTC ETH SOL
    python -m backtest.backtest --min-funding 0.0003 --holding-periods 6

The sample CSVs in backtest/data/ contain 30 days of 8h funding rates
for BTC, ETH, and SOL perps (realistic synthetic data based on typical
Bybit funding patterns).
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd


@dataclass
class BacktestConfig:
    """Backtest parameters mirroring the live bot's Settings."""

    min_current_funding: float = 0.0002  # 0.02%
    min_profitable_cycles: int = 3
    expected_holding_periods: int = 8
    funding_floor: float = 0.0001  # 0.01%
    hard_stop_loss_pct: float = -0.02
    spot_maker_fee: float = 0.001
    perp_maker_fee: float = 0.0002
    position_size_usd: float = 1000.0
    max_concurrent_pairs: int = 5

    @property
    def round_trip_fee(self) -> float:
        return 2 * self.spot_maker_fee + 2 * self.perp_maker_fee


@dataclass
class BacktestTrade:
    """A single simulated hedge trade."""

    pair: str
    entry_time: datetime
    exit_time: Optional[datetime] = None
    notional_usd: float = 0.0
    cumulative_funding: float = 0.0
    total_fees: float = 0.0
    exit_reason: str = ""
    periods_held: int = 0


@dataclass
class BacktestResult:
    """Aggregate results from a backtest run."""

    trades: list[BacktestTrade] = field(default_factory=list)
    total_funding: float = 0.0
    total_fees: float = 0.0
    total_pnl: float = 0.0
    capital_deployed: float = 0.0
    days: int = 30

    @property
    def net_pnl(self) -> float:
        return self.total_funding - self.total_fees

    @property
    def annualised_return(self) -> float:
        if self.capital_deployed <= 0 or self.days <= 0:
            return 0.0
        period_return = self.net_pnl / self.capital_deployed
        return period_return * (365 / self.days)


def load_funding_data(data_dir: str, pair: str) -> pd.DataFrame:
    """Load a funding rate CSV for a given pair.

    Expected CSV format:
        timestamp, funding_rate
        2026-02-19 00:00:00, 0.000350
    """
    path = Path(data_dir) / f"{pair.lower()}_funding_30d.csv"
    if not path.exists():
        raise FileNotFoundError(f"Funding data not found: {path}")
    df = pd.read_csv(path, parse_dates=["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def run_backtest(
    config: BacktestConfig,
    data_dir: str = "backtest/data",
    pairs: list[str] | None = None,
) -> BacktestResult:
    """Run the funding arb backtest on historical data.

    Simulates:
        - Scanning at each 8h interval
        - Entering when funding is profitable (trailing avg check)
        - Exiting on funding decay or floor breach
        - Tracking fees, funding income, and net PnL
    """
    if pairs is None:
        pairs = ["BTC", "ETH", "SOL"]

    # Load all pair data
    pair_data: dict[str, pd.DataFrame] = {}
    for pair in pairs:
        try:
            pair_data[pair] = load_funding_data(data_dir, pair)
        except FileNotFoundError as e:
            print(f"Warning: {e}")

    if not pair_data:
        print("No funding data available.")
        return BacktestResult()

    result = BacktestResult()
    open_trades: dict[str, BacktestTrade] = {}

    # Determine simulation range from data
    all_timestamps: list[datetime] = []
    for df in pair_data.values():
        all_timestamps.extend(df["timestamp"].tolist())
    if not all_timestamps:
        return result

    start = min(all_timestamps)
    end = max(all_timestamps)
    result.days = (end - start).days or 1

    # Iterate through each 8h settlement
    for pair_name, df in pair_data.items():
        trailing_window: list[float] = []

        for i, row in df.iterrows():
            ts = row["timestamp"]
            rate = row["funding_rate"]
            trailing_window.append(rate)

            # Keep trailing window sized to min_profitable_cycles
            if len(trailing_window) > config.min_profitable_cycles:
                trailing_window = trailing_window[-config.min_profitable_cycles :]

            # Check if we have an open trade for this pair
            if pair_name in open_trades:
                trade = open_trades[pair_name]
                # Record funding payment
                funding_payment = trade.notional_usd * rate
                trade.cumulative_funding += funding_payment
                trade.periods_held += 1

                # Check exit conditions
                should_exit = False
                reason = ""

                # Funding decay
                if len(trailing_window) >= 3:
                    trailing_avg = sum(trailing_window[-3:]) / 3
                    if trailing_avg < config.funding_floor:
                        should_exit = True
                        reason = f"funding_decay (avg={trailing_avg:.6f})"

                # Single negative rate (simplified — live bot tracks 24h streak)
                if rate < 0:
                    trade._neg_count = getattr(trade, "_neg_count", 0) + 1
                    if trade._neg_count >= 3:  # 3 × 8h = 24h
                        should_exit = True
                        reason = "funding_negative_24h"
                else:
                    trade._neg_count = 0

                if should_exit:
                    trade.exit_time = ts
                    trade.exit_reason = reason
                    # Close fees (2 legs × maker)
                    trade.total_fees += trade.notional_usd * (
                        config.spot_maker_fee + config.perp_maker_fee
                    )
                    result.trades.append(trade)
                    result.total_funding += trade.cumulative_funding
                    result.total_fees += trade.total_fees
                    del open_trades[pair_name]

            else:
                # Check entry conditions
                if rate < config.min_current_funding:
                    continue
                if len(trailing_window) < config.min_profitable_cycles:
                    continue

                trailing_avg = sum(trailing_window) / len(trailing_window)
                net = (
                    trailing_avg * config.expected_holding_periods
                ) - config.round_trip_fee

                if net > 0 and len(open_trades) < config.max_concurrent_pairs:
                    # Enter trade
                    open_fee = config.position_size_usd * (
                        config.spot_maker_fee + config.perp_maker_fee
                    )
                    trade = BacktestTrade(
                        pair=pair_name,
                        entry_time=ts,
                        notional_usd=config.position_size_usd,
                        total_fees=open_fee,
                    )
                    open_trades[pair_name] = trade
                    result.capital_deployed += config.position_size_usd

    # Close any remaining open trades at end
    for pair_name, trade in open_trades.items():
        trade.exit_time = end
        trade.exit_reason = "backtest_end"
        trade.total_fees += trade.notional_usd * (
            config.spot_maker_fee + config.perp_maker_fee
        )
        result.trades.append(trade)
        result.total_funding += trade.cumulative_funding
        result.total_fees += trade.total_fees

    result.total_pnl = result.net_pnl
    return result


def print_results(result: BacktestResult) -> None:
    """Pretty-print backtest results."""
    print(f"\n{'=' * 60}")
    print(f"  AU-Funding-Arb — Backtest Results ({result.days} days)")
    print(f"{'=' * 60}")
    print(f"  Total trades:            {len(result.trades)}")
    print(f"  Capital deployed:        ${result.capital_deployed:>12,.2f}")
    print(f"  Gross funding income:    ${result.total_funding:>12,.4f}")
    print(f"  Total fees:              ${result.total_fees:>12,.4f}")
    print(f"  Net PnL:                 ${result.net_pnl:>12,.4f}")
    print(f"  Annualised return:       {result.annualised_return:>12.2%}")
    print(f"{'=' * 60}")

    if result.trades:
        print(f"\n  Per-trade breakdown:")
        print(f"  {'Pair':<8} {'Entry':<20} {'Periods':<8} {'Funding':>10} {'Fees':>10} {'Net':>10} {'Reason'}")
        print(f"  {'-'*90}")
        for t in result.trades:
            net = t.cumulative_funding - t.total_fees
            print(
                f"  {t.pair:<8} {t.entry_time.strftime('%Y-%m-%d %H:%M'):<20} "
                f"{t.periods_held:<8} "
                f"${t.cumulative_funding:>9.4f} "
                f"${t.total_fees:>9.4f} "
                f"${net:>9.4f} "
                f"{t.exit_reason}"
            )
    print()


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="AU-Funding-Arb — Historical Funding Rate Backtest"
    )
    parser.add_argument(
        "--data-dir",
        default="backtest/data",
        help="Directory containing funding CSV files",
    )
    parser.add_argument(
        "--pairs",
        nargs="+",
        default=["BTC", "ETH", "SOL"],
        help="Pairs to backtest (e.g. BTC ETH SOL)",
    )
    parser.add_argument(
        "--min-funding",
        type=float,
        default=0.0002,
        help="Minimum current funding rate (default 0.0002 = 0.02%%)",
    )
    parser.add_argument(
        "--holding-periods",
        type=int,
        default=8,
        help="Expected holding periods (default 8)",
    )
    parser.add_argument(
        "--position-size",
        type=float,
        default=1000.0,
        help="Position size USD (default 1000)",
    )
    args = parser.parse_args()

    config = BacktestConfig(
        min_current_funding=args.min_funding,
        expected_holding_periods=args.holding_periods,
        position_size_usd=args.position_size,
    )

    result = run_backtest(config, args.data_dir, args.pairs)
    print_results(result)


if __name__ == "__main__":
    main()
