"""Koinly / CryptoTaxCalculator CSV export from trades_aud.csv.

Usage:
    python -m src.tax_export --koinly
    python -m src.tax_export --koinly --output koinly_import.csv
    python -m src.tax_export --summary --year 2026
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

from src.config import Settings


def load_trades(settings: Settings) -> pd.DataFrame:
    """Load trades_aud.csv into a DataFrame."""
    path = Path(settings.data_dir) / settings.trades_csv
    if not path.exists():
        print(f"ERROR: Trades file not found at {path}")
        sys.exit(1)
    return pd.read_csv(path, parse_dates=["timestamp_utc", "timestamp_awst"])


def export_koinly(settings: Settings, output_path: str = "koinly_import.csv") -> None:
    """Convert trades_aud.csv to Koinly Universal CSV format.

    Koinly columns:
        Date, Sent Amount, Sent Currency, Received Amount, Received Currency,
        Fee Amount, Fee Currency, Net Worth Amount, Net Worth Currency,
        Label, Description, TxHash
    """
    df = load_trades(settings)
    rows: list[dict[str, str]] = []

    for _, trade in df.iterrows():
        tx_type = trade["tx_type"]
        pair = str(trade["pair"])
        # Extract base currency from pair (e.g. "BTCUSDT" → "BTC")
        base = pair.replace("USDT", "").replace("BUSD", "")

        koinly_row: dict[str, str] = {
            "Date": pd.Timestamp(trade["timestamp_utc"]).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "Sent Amount": "",
            "Sent Currency": "",
            "Received Amount": "",
            "Received Currency": "",
            "Fee Amount": f"{trade['fee_aud']:.6f}" if trade["fee_aud"] else "",
            "Fee Currency": "AUD",
            "Net Worth Amount": f"{trade['aud_value']:.4f}",
            "Net Worth Currency": "AUD",
            "Label": "",
            "Description": f"Funding arb {tx_type} — {pair} on {trade['exchange']}",
            "TxHash": "",
        }

        if tx_type == "funding":
            # Funding payments are income
            koinly_row["Received Amount"] = f"{trade['qty']:.8f}"
            koinly_row["Received Currency"] = "USDT"
            koinly_row["Label"] = "other income"
            koinly_row["Description"] = f"Funding rate payment — {pair}"

        elif tx_type == "open" and trade["side"] == "buy":
            # Buying spot = send USDT, receive crypto
            koinly_row["Sent Amount"] = f"{trade['amount_usd']:.4f}"
            koinly_row["Sent Currency"] = "USDT"
            koinly_row["Received Amount"] = f"{trade['qty']:.8f}"
            koinly_row["Received Currency"] = base

        elif tx_type == "open" and trade["side"] == "sell":
            # Opening perp short (margin trade)
            koinly_row["Label"] = "margin trade"
            koinly_row["Sent Amount"] = f"{trade['qty']:.8f}"
            koinly_row["Sent Currency"] = base
            koinly_row["Description"] = f"Open perp short — {pair}"

        elif tx_type == "close" and trade["side"] == "sell":
            # Selling spot
            koinly_row["Sent Amount"] = f"{trade['qty']:.8f}"
            koinly_row["Sent Currency"] = base
            koinly_row["Received Amount"] = f"{trade['amount_usd']:.4f}"
            koinly_row["Received Currency"] = "USDT"

        elif tx_type == "close" and trade["side"] == "buy":
            # Closing perp short
            koinly_row["Label"] = "margin trade"
            koinly_row["Received Amount"] = f"{trade['qty']:.8f}"
            koinly_row["Received Currency"] = base
            koinly_row["Description"] = f"Close perp short — {pair}"

        elif tx_type == "fee":
            koinly_row["Sent Amount"] = f"{trade['fee_usd']:.6f}"
            koinly_row["Sent Currency"] = "USDT"
            koinly_row["Label"] = "cost"

        rows.append(koinly_row)

    # Write Koinly CSV
    koinly_headers = [
        "Date",
        "Sent Amount",
        "Sent Currency",
        "Received Amount",
        "Received Currency",
        "Fee Amount",
        "Fee Currency",
        "Net Worth Amount",
        "Net Worth Currency",
        "Label",
        "Description",
        "TxHash",
    ]
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=koinly_headers)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Koinly CSV exported: {output_path} ({len(rows)} rows)")


def export_summary(settings: Settings, year: int) -> None:
    """Print an annual tax summary for a given financial year."""
    df = load_trades(settings)
    df["year"] = pd.to_datetime(df["timestamp_utc"]).dt.year

    # Australian financial year: 1 Jul (year-1) to 30 Jun (year)
    # For simplicity, filter by calendar year
    yr_df = df[df["year"] == year]

    if yr_df.empty:
        print(f"No trades found for {year}.")
        return

    funding = yr_df[yr_df["tx_type"] == "funding"]
    opens = yr_df[yr_df["tx_type"] == "open"]
    closes = yr_df[yr_df["tx_type"] == "close"]

    total_funding_aud = funding["aud_value"].sum() if not funding.empty else 0
    total_fees_aud = yr_df["fee_aud"].sum()
    total_pnl_aud = closes["pnl_aud"].dropna().sum() if not closes.empty else 0
    num_trades = len(yr_df)

    print(f"\n{'=' * 50}")
    print(f"  AU-Funding-Arb — Tax Summary {year}")
    print(f"{'=' * 50}")
    print(f"  Total trades:              {num_trades}")
    print(f"  Funding income (AUD):      ${total_funding_aud:>12,.2f}")
    print(f"  Realised PnL (AUD):        ${total_pnl_aud:>12,.2f}")
    print(f"  Total fees paid (AUD):     ${total_fees_aud:>12,.2f}")
    print(f"  Net income (AUD):          ${total_funding_aud + total_pnl_aud - total_fees_aud:>12,.2f}")
    print(f"{'=' * 50}")
    print()
    print("  NOTE: This is assessable business income per ATO crypto guidance.")
    print("  Deductible expenses include: VPS hosting, electricity, API costs.")
    print("  Use RBA daily rates for consistent, ATO-friendly AUD conversion.")
    print()

    # Per-pair breakdown
    if not closes.empty:
        print("  Per-pair breakdown:")
        for pair in yr_df["pair"].unique():
            pair_df = yr_df[yr_df["pair"] == pair]
            pair_funding = pair_df[pair_df["tx_type"] == "funding"]["aud_value"].sum()
            pair_fees = pair_df["fee_aud"].sum()
            pair_pnl = pair_df[pair_df["tx_type"] == "close"]["pnl_aud"].dropna().sum()
            print(
                f"    {pair:<12} funding=${pair_funding:>8,.2f}  "
                f"pnl=${pair_pnl:>8,.2f}  fees=${pair_fees:>8,.2f}"
            )


def main() -> None:
    """CLI entry point for tax export."""
    parser = argparse.ArgumentParser(
        description="AU-Funding-Arb — ATO Tax Export Tool"
    )
    parser.add_argument(
        "--koinly",
        action="store_true",
        help="Export trades in Koinly Universal CSV format",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print annual tax summary",
    )
    parser.add_argument(
        "--year",
        type=int,
        default=datetime.now().year,
        help="Tax year for summary (default: current year)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="koinly_import.csv",
        help="Output file path for Koinly export",
    )
    args = parser.parse_args()

    settings = Settings()

    if args.koinly:
        export_koinly(settings, args.output)
    elif args.summary:
        export_summary(settings, args.year)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
