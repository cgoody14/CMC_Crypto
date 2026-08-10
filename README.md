# CMC New Listing Tracker (API version)

Uses the official CoinMarketCap API to detect newly listed coins and
track their price for 24 hours after listing, so you can test whether
new listings tend to pop.

Two endpoints, two jobs:
- **`/cryptocurrency/listings/latest?sort=date_added`** — detection.
  Sorted newest-first, gives the exact `date_added` timestamp plus
  live price/mcap/volume in one call.
- **`/cryptocurrency/quotes/latest?id=...`** — tracking. Batched
  lookup by CMC's numeric ID for coins still inside their 24h window.

No HTML parsing, no scraping, no blocking risk.

## 1. Get a free API key (walkthrough)

1. Go to **pro.coinmarketcap.com** and sign up (Basic tier is free,
   no card required)
2. Once logged in, go to **Account → API Key** — a key is generated
   automatically
3. Copy it — you'll need it in step 3 below

## 2. Setup

```bash
cd cmc_tracker
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## 3. Set your API key

Two options — pick one:

**Option A - .env file (recommended, keeps the key out of your shell history):**
```bash
echo "CMC_API_KEY=your-key-here" > .env
```

**Option B - environment variable:**
```bash
export CMC_API_KEY="your-key-here"
```

Either way, **never commit the `.env` file or hardcode the key in the
script** — add `.env` to `.gitignore` if this ever goes in a repo.

## 4. Test it once, manually

```bash
python3 cmc_tracker.py
```

Check `cmc_tracker.log` for lines like:
```
Fetched 200 listings sorted by date_added
NEW LISTING: Some Coin (SYM) - $0.0012, added 2026-08-03T14:22:00.000Z (2.3h ago)
Detection pass: N new coin(s) inserted
Tracking pass: M snapshot(s) recorded for K active coin(s)
```

## 5. Schedule it (cron, every 15 min)

```bash
crontab -e
```

Add (adjust the path):
```
*/15 * * * * cd /path/to/cmc_tracker && venv/bin/python3 cmc_tracker.py >> cron.log 2>&1
```

Cron doesn't load your shell's environment variables or `.env` file
automatically — if you used Option A above, that's already handled
because the script loads `.env` itself via `python-dotenv`. If you used
Option B (`export`), add `CMC_API_KEY=your-key-here` as its own line
at the top of your crontab instead.

Verify it's registered: `crontab -l`

## 6. Analyze results

Once it's run for at least a day or two:
```bash
python3 analyze.py
python3 analyze.py --csv returns.csv
python3 analyze.py --min-first-price 0.000001
```

Gives mean/median % return and % positive at 15min/30min/1h/3h/6h/12h/24h
after listing — the actual test of your thesis.

## Credit budget (free Basic tier: 15,000/month)

- Detection call: `limit=200` = 1 credit per call (1 credit per 200
  data points). At 15-min polling: 96 calls/day × 30 = 2,880/month.
- Tracking calls: batched by ID, typically well under 100 IDs at once
  (only coins inside their 24h window) = roughly 1 credit per call.
  Same cadence: ~2,880/month more, usually less since batches shrink
  as coins age out.
- **Total: comfortably under 15,000/month** even at 15-min polling.
  If you tighten the interval or track a much higher-volume period,
  check usage at pro.coinmarketcap.com — a `402 Payment Required`
  response means you've hit the monthly cap (the script logs this
  clearly if it happens).

## Data model (cmc_tracker.db, SQLite)

- `coins` — one row per newly detected listing (cmc_id, slug, name,
  symbol, platform/blockchain, exact `added_at`, first_price/mcap/volume)
- `price_snapshots` — one row per poll per tracked coin during its 24h
  window (price, mcap, volume, hours_since_listing)

## Known limitations

- `DETECTION_LIMIT = 200` per call means if CMC lists more than 200
  new coins between two polling cycles, the overflow wouldn't be
  caught. At 15-min polling this is very unlikely, but if you ever
  see gaps, raise the limit (costs more credits per call — see math
  above) or poll more frequently.
- New listings are often extremely illiquid — a "500% pop" on $40 of
  volume isn't a tradeable signal. Use `--min-first-price` in
  `analyze.py`, and consider adding a volume/mcap filter once you're
  looking at real data.
- A large share of what shows up as "new" is tokenized stocks
  (xStock/Robinhood/Reality wrappers) and stablecoins, not organic
  new token launches — these behave very differently. Worth
  segmenting by `platform`/name patterns before trusting aggregate
  stats.
