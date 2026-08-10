#!/usr/bin/env python3
"""
Analyze collected cmc_tracker.db data to test the thesis:
"New CoinMarketCap listings see a price pop shortly after being added."

Usage:
    python3 analyze.py                       # summary stats, everything
    python3 analyze.py --exclude-tokenized    # organic new launches only
    python3 analyze.py --only-tokenized       # tokenized stocks/ETFs only
    python3 analyze.py --csv out.csv          # also dump per-coin returns
"""

import argparse
import csv
import re
import sqlite3
import statistics
from pathlib import Path

DB_PATH = Path(__file__).parent / "cmc_tracker.db"

# Time buckets (hours since listing) to measure return at.
BUCKETS = [0.25, 0.5, 1, 3, 6, 12, 24]
BUCKET_TOLERANCE = 0.25  # hours - closest snapshot within this window counts

# Patterns that flag a listing as a tokenized/wrapped real-world asset
# rather than an organic new token launch (tokenized stocks, ETFs,
# commodities, and their "bStock"/"xStock" variants). These behave very
# differently from genuine new listings - they track an underlying
# real-world price, not speculative demand for a brand-new token.
TOKENIZED_PATTERNS = re.compile(
    r"tokenized|derivatives|xstock|bstock|\bstock\b|\betf\b",
    re.IGNORECASE,
)


def is_tokenized(name: str) -> bool:
    return bool(name and TOKENIZED_PATTERNS.search(name))


def get_conn():
    return sqlite3.connect(DB_PATH)


def closest_snapshot(snapshots, target_hours):
    """snapshots: list of (hours_since_listing, price). Return price of the
    snapshot closest to target_hours, or None if nothing within tolerance."""
    best = None
    best_diff = None
    for hrs, price in snapshots:
        if price is None:
            continue
        diff = abs(hrs - target_hours)
        if diff <= BUCKET_TOLERANCE and (best_diff is None or diff < best_diff):
            best = price
            best_diff = diff
    return best


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", help="optional path to dump per-coin returns")
    parser.add_argument(
        "--min-first-price",
        type=float,
        default=0.0,
        help="filter out coins with first_price below this (junk/zero filter)",
    )
    parser.add_argument(
        "--exclude-tokenized",
        action="store_true",
        help="exclude tokenized stocks/ETFs/commodities (Robinhood/xStock/"
             "bStock wrappers) - keeps only organic new token launches",
    )
    parser.add_argument(
        "--only-tokenized",
        action="store_true",
        help="inverse of --exclude-tokenized: analyze ONLY the tokenized "
             "wrapper listings, for comparison",
    )
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"No database found at {DB_PATH}. Run cmc_tracker.py first.")
        return

    conn = get_conn()
    coins = conn.execute(
        """SELECT id, slug, name, symbol, first_price, added_at
           FROM coins
           WHERE first_price IS NOT NULL AND first_price >= ?
           ORDER BY added_at""",
        (args.min_first_price,),
    ).fetchall()

    if not coins:
        print("No coins with price data yet.")
        return

    if args.exclude_tokenized and args.only_tokenized:
        print("Use only one of --exclude-tokenized / --only-tokenized, not both.")
        return

    if args.exclude_tokenized:
        before = len(coins)
        coins = [c for c in coins if not is_tokenized(c[2])]  # c[2] = name
        print(f"Excluded {before - len(coins)} tokenized stock/ETF/commodity "
              f"listing(s), {len(coins)} organic listing(s) remain")
    elif args.only_tokenized:
        before = len(coins)
        coins = [c for c in coins if is_tokenized(c[2])]
        print(f"Kept {len(coins)} tokenized listing(s) out of {before} total")

    if not coins:
        print("Nothing left to analyze after filtering.")
        return

    bucket_returns = {b: [] for b in BUCKETS}
    per_coin_rows = []

    for coin_id, slug, name, symbol, first_price, added_at in coins:
        snaps = conn.execute(
            """SELECT hours_since_listing, price FROM price_snapshots
               WHERE coin_id = ? ORDER BY hours_since_listing""",
            (coin_id,),
        ).fetchall()
        if not snaps:
            continue

        row = {"slug": slug, "name": name, "symbol": symbol,
               "first_price": first_price, "added_at": added_at}

        for b in BUCKETS:
            price_at_b = closest_snapshot(snaps, b)
            if price_at_b is not None and first_price:
                pct_return = (price_at_b - first_price) / first_price * 100
                bucket_returns[b].append(pct_return)
                row[f"return_{b}h_pct"] = round(pct_return, 2)
            else:
                row[f"return_{b}h_pct"] = None

        per_coin_rows.append(row)

    conn.close()

    print(f"\nAnalyzed {len(per_coin_rows)} coin(s) with snapshot data\n")
    print(f"{'Hours':>8} | {'N':>5} | {'Mean %':>8} | {'Median %':>9} | {'% Positive':>10}")
    print("-" * 52)
    for b in BUCKETS:
        vals = bucket_returns[b]
        if not vals:
            print(f"{b:>8} | {'0':>5} | {'--':>8} | {'--':>9} | {'--':>10}")
            continue
        mean = statistics.mean(vals)
        median = statistics.median(vals)
        pct_pos = sum(1 for v in vals if v > 0) / len(vals) * 100
        print(f"{b:>8} | {len(vals):>5} | {mean:>7.2f}% | {median:>8.2f}% | {pct_pos:>9.1f}%")

    print(
        "\nNote: small/illiquid new listings can show huge swings on tiny "
        "volume - treat mean/median as directional, not tradeable, until "
        "you've filtered for liquidity (volume/mcap) and sample size grows."
    )

    if args.csv:
        fieldnames = ["slug", "name", "symbol", "first_price", "added_at"] + [
            f"return_{b}h_pct" for b in BUCKETS
        ]
        with open(args.csv, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(per_coin_rows)
        print(f"\nPer-coin returns written to {args.csv}")


if __name__ == "__main__":
    main()
