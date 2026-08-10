#!/usr/bin/env python3
"""
CMC New Listing Tracker (API version)
--------------------------------------
Uses the official CoinMarketCap API instead of scraping. Two endpoints:

  1. /v1/cryptocurrency/listings/latest?sort=date_added
     -> DETECTION. Sorted newest-first, gives exact `date_added` timestamps
        plus live price/mcap/volume in one call. Run every cycle.

  2. /v2/cryptocurrency/quotes/latest?id=...
     -> TRACKING. Batched lookup by CMC numeric ID for coins still inside
        their 24h post-listing window. Run every cycle, only for coins
        we're actively tracking (cheap - a handful of IDs per call).

Requires a free CMC API key: https://pro.coinmarketcap.com -> Account -> API Key
Set it as an environment variable before running:
    export CMC_API_KEY="your-key-here"
(see README.md for a .env option instead)

Credit cost stays well inside the free Basic tier's 15,000/month even at
15-min polling - see README for the math.
"""

import os
import sqlite3
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH = Path(__file__).parent / "cmc_tracker.db"
LOG_PATH = Path(__file__).parent / "cmc_tracker.log"

API_BASE = "https://pro-api.coinmarketcap.com"
LISTINGS_ENDPOINT = f"{API_BASE}/v1/cryptocurrency/listings/latest"
QUOTES_ENDPOINT = f"{API_BASE}/v2/cryptocurrency/quotes/latest"

DETECTION_LIMIT = 200        # coins per detection call (1 credit per 200 data points)
TRACK_WINDOW_HOURS = 24      # how long to keep polling a coin after listing
REQUEST_TIMEOUT = 15
QUOTES_BATCH_SIZE = 100      # CMC allows batching many IDs in one quotes call

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler()],
)
log = logging.getLogger("cmc_tracker")


def get_api_key() -> str:
    # optional: load a .env file if python-dotenv is installed and present
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    key = os.environ.get("CMC_API_KEY")
    if not key:
        raise SystemExit(
            "CMC_API_KEY environment variable not set. Get a free key at "
            "pro.coinmarketcap.com (Account -> API Key), then either:\n"
            "  export CMC_API_KEY='your-key-here'\n"
            "or add CMC_API_KEY=your-key-here to a .env file in this folder."
        )
    return key


API_KEY = None  # set in main() after get_api_key()


def api_get(url: str, params: dict) -> dict | None:
    headers = {"Accepts": "application/json", "X-CMC_PRO_API_KEY": API_KEY}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        log.warning("Request failed for %s: %s", url, e)
        return None

    if resp.status_code == 200:
        return resp.json()

    if resp.status_code == 401:
        log.error("401 Unauthorized - check CMC_API_KEY is correct and active.")
    elif resp.status_code == 429:
        log.warning("429 rate limited - backing off 10s")
        time.sleep(10)
    elif resp.status_code == 402:
        log.error(
            "402 Payment Required - you've likely exhausted your monthly "
            "credit allowance. Check usage at pro.coinmarketcap.com."
        )
    else:
        log.warning("Non-200 (%s) for %s: %s", resp.status_code, url, resp.text[:300])
    return None


# ---------------------------------------------------------------------------
# DB setup (same shape as the scraper version, plus cmc_id)
# ---------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS coins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cmc_id INTEGER UNIQUE NOT NULL,
            slug TEXT,
            name TEXT,
            symbol TEXT,
            platform TEXT,           -- e.g. 'Solana', 'Ethereum', NULL if own chain
            added_at TEXT,           -- exact date_added from the API (UTC ISO)
            first_seen_at TEXT,      -- when our tracker first saw it (UTC ISO)
            first_price REAL,
            first_mcap REAL,
            first_volume REAL,
            tracking_complete INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS price_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coin_id INTEGER NOT NULL REFERENCES coins(id),
            snapshot_at TEXT NOT NULL,
            price REAL,
            pct_change_1h REAL,
            pct_change_24h REAL,
            mcap REAL,
            volume REAL,
            hours_since_listing REAL
        );

        CREATE INDEX IF NOT EXISTS idx_snapshots_coin ON price_snapshots(coin_id);
        """
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Detection: listings/latest sorted by date_added
# ---------------------------------------------------------------------------

def fetch_newest_listings() -> list[dict]:
    params = {
        "start": 1,
        "limit": DETECTION_LIMIT,
        "sort": "date_added",
        "sort_dir": "desc",
        "cryptocurrency_type": "all",
        "convert": "USD",
        "aux": "platform,date_added",
    }
    data = api_get(LISTINGS_ENDPOINT, params)
    if not data or "data" not in data:
        return []
    return data["data"]


def parse_listing_row(item: dict) -> dict:
    quote = item.get("quote", {}).get("USD", {})
    platform = item.get("platform")
    return {
        "cmc_id": item["id"],
        "slug": item.get("slug"),
        "name": item.get("name"),
        "symbol": item.get("symbol"),
        "platform": platform.get("name") if platform else None,
        "added_at": item.get("date_added"),  # ISO 8601 string, exact
        "price": quote.get("price"),
        "pct_change_1h": quote.get("percent_change_1h"),
        "pct_change_24h": quote.get("percent_change_24h"),
        "mcap": quote.get("market_cap"),
        "volume": quote.get("volume_24h"),
    }


# ---------------------------------------------------------------------------
# Tracking: quotes/latest by ID, batched
# ---------------------------------------------------------------------------

def fetch_quotes_for_ids(ids: list[int]) -> dict[int, dict]:
    """Return {cmc_id: quote_dict} for the given IDs, batched."""
    out = {}
    for i in range(0, len(ids), QUOTES_BATCH_SIZE):
        batch = ids[i : i + QUOTES_BATCH_SIZE]
        params = {"id": ",".join(str(x) for x in batch), "convert": "USD"}
        data = api_get(QUOTES_ENDPOINT, params)
        if not data or "data" not in data:
            continue
        for cmc_id_str, item in data["data"].items():
            # v2 quotes/latest returns a list per id in rare merged-coin cases
            entry = item[0] if isinstance(item, list) else item
            quote = entry.get("quote", {}).get("USD", {})
            out[int(cmc_id_str)] = {
                "price": quote.get("price"),
                "pct_change_1h": quote.get("percent_change_1h"),
                "pct_change_24h": quote.get("percent_change_24h"),
                "mcap": quote.get("market_cap"),
                "volume": quote.get("volume_24h"),
            }
    return out


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def parse_iso(ts: str) -> datetime:
    # CMC returns e.g. "2026-08-03T14:22:00.000Z"
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def ingest_new_listings(rows: list[dict], now: datetime):
    conn = get_conn()
    cur = conn.cursor()

    existing_ids = {
        r[0] for r in cur.execute("SELECT cmc_id FROM coins")
    }

    new_count = 0
    for row in rows:
        if row["cmc_id"] in existing_ids:
            continue
        added_at = parse_iso(row["added_at"]) if row["added_at"] else now
        hours_since = (now - added_at).total_seconds() / 3600

        cur.execute(
            """INSERT INTO coins
               (cmc_id, slug, name, symbol, platform, added_at, first_seen_at,
                first_price, first_mcap, first_volume)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row["cmc_id"], row["slug"], row["name"], row["symbol"],
                row["platform"], added_at.isoformat(), now.isoformat(),
                row["price"], row["mcap"], row["volume"],
            ),
        )
        new_count += 1
        log.info(
            "NEW LISTING: %s (%s) - $%.8g, added %s (%.1fh ago)",
            row["name"], row["symbol"], row["price"] or 0,
            row["added_at"], hours_since,
        )

        # this row also counts as its first snapshot
        coin_id = cur.lastrowid
        cur.execute(
            """INSERT INTO price_snapshots
               (coin_id, snapshot_at, price, pct_change_1h, pct_change_24h,
                mcap, volume, hours_since_listing)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                coin_id, now.isoformat(), row["price"], row["pct_change_1h"],
                row["pct_change_24h"], row["mcap"], row["volume"], hours_since,
            ),
        )

    conn.commit()
    conn.close()
    log.info("Detection pass: %d new coin(s) inserted", new_count)


def track_active_coins(now: datetime):
    conn = get_conn()
    cur = conn.cursor()

    active = cur.execute(
        "SELECT id, cmc_id, added_at FROM coins WHERE tracking_complete = 0"
    ).fetchall()

    if not active:
        conn.close()
        log.info("No coins currently inside their 24h tracking window")
        return

    still_active = []
    for coin_id, cmc_id, added_at_str in active:
        added_at = parse_iso(added_at_str)
        hours_since = (now - added_at).total_seconds() / 3600
        if hours_since <= TRACK_WINDOW_HOURS:
            still_active.append((coin_id, cmc_id, hours_since))
        else:
            cur.execute(
                "UPDATE coins SET tracking_complete = 1 WHERE id = ?", (coin_id,)
            )

    conn.commit()

    if not still_active:
        conn.close()
        log.info("All previously-tracked coins have completed their 24h window")
        return

    ids = [cmc_id for _, cmc_id, _ in still_active]
    quotes = fetch_quotes_for_ids(ids)

    snap_count = 0
    for coin_id, cmc_id, hours_since in still_active:
        q = quotes.get(cmc_id)
        if not q:
            continue
        cur.execute(
            """INSERT INTO price_snapshots
               (coin_id, snapshot_at, price, pct_change_1h, pct_change_24h,
                mcap, volume, hours_since_listing)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                coin_id, now.isoformat(), q["price"], q["pct_change_1h"],
                q["pct_change_24h"], q["mcap"], q["volume"], hours_since,
            ),
        )
        snap_count += 1

    conn.commit()
    conn.close()
    log.info(
        "Tracking pass: %d snapshot(s) recorded for %d active coin(s)",
        snap_count, len(still_active),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    global API_KEY
    API_KEY = get_api_key()

    init_db()
    now = datetime.now(timezone.utc)
    log.info("=== Run started %s ===", now.isoformat())

    raw_rows = fetch_newest_listings()
    if not raw_rows:
        log.error("No data returned from listings/latest - check API key/credits/network.")
        return
    log.info("Fetched %d listings sorted by date_added", len(raw_rows))

    parsed_rows = [parse_listing_row(r) for r in raw_rows]
    ingest_new_listings(parsed_rows, now)
    track_active_coins(now)

    log.info("=== Run finished ===")


if __name__ == "__main__":
    main()
