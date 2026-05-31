"""
build_watchlist.py
──────────────────
Fetches all active US stocks from Alpaca and filters to:
  - Price between $1 and $20 (previous close)
  - Average daily volume > 100,000

Writes filtered list to docs/data/watchlist.json
Called by both monitor.yml and backtest.yml before scanning.
"""

import requests
import json
import os
import pytz
from datetime import datetime, timedelta

ALPACA_API_KEY    = os.environ.get("ALPACA_API_KEY",    "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_BASE       = "https://data.alpaca.markets/v2"
ALPACA_BROKER     = "https://paper-api.alpaca.markets/v2"   # assets endpoint

PRICE_MIN    = float(os.environ.get("PRICE_MIN",    "1"))
PRICE_MAX    = float(os.environ.get("PRICE_MAX",    "20"))
MIN_VOLUME   = float(os.environ.get("MIN_VOLUME",   "100000"))
OUTPUT_FILE  = "docs/data/watchlist.json"

ET_TZ = pytz.timezone("America/New_York")

def alpaca_headers():
    return {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }

def get_all_assets():
    """Fetch all active tradeable US equity assets from Alpaca."""
    url    = f"{ALPACA_BROKER}/assets"
    params = {"status": "active", "asset_class": "us_equity"}
    try:
        resp = requests.get(url, headers=alpaca_headers(), params=params, timeout=30)
        if resp.status_code != 200:
            print(f"Assets fetch failed: HTTP {resp.status_code}")
            return []
        assets = resp.json()
        # Only tradeable, non-OTC symbols without special characters
        filtered = [
            a["symbol"] for a in assets
            if a.get("tradable")
            and a.get("status") == "active"
            and "/" not in a["symbol"]   # exclude crypto pairs
            and "." not in a["symbol"]   # exclude preferred shares / warrants
        ]
        print(f"Total active tradeable US equities: {len(filtered)}")
        return filtered
    except Exception as e:
        print(f"Assets fetch error: {e}")
        return []

def get_snapshots_batch(symbols):
    """
    Fetch previous day's close price and volume for each symbol
    using Alpaca's /v2/stocks/bars endpoint (latest bar per symbol).
    Falls back gracefully per batch — 403 means no access to that feed.
    Returns dict: { symbol: {price, volume} }
    """
    results = {}
    batch_size = 200   # smaller batches — bars endpoint is more stable

    from datetime import date, timedelta
    # Use last weekday as the reference date for daily bars
    ref = date.today()
    for _ in range(7):
        if ref.weekday() < 5:
            break
        ref -= timedelta(days=1)
    start = (ref - timedelta(days=5)).strftime("%Y-%m-%d")
    end   = ref.strftime("%Y-%m-%d")

    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i+batch_size]
        url   = f"{ALPACA_BASE}/stocks/bars"
        params = {
            "symbols":   ",".join(batch),
            "timeframe": "1Day",
            "start":     start,
            "end":       end,
            "feed":      "sip",
            "limit":     5,    # just last few daily bars per symbol
        }
        try:
            resp = requests.get(url, headers=alpaca_headers(), params=params, timeout=60)
            if resp.status_code == 403:
                # Try iex feed as fallback
                params["feed"] = "iex"
                resp = requests.get(url, headers=alpaca_headers(), params=params, timeout=60)
            if resp.status_code != 200:
                print(f"  Batch {i//batch_size+1} failed: HTTP {resp.status_code}")
                continue

            data = resp.json().get("bars", {}) or {}
            for sym, bars in data.items():
                if not bars:
                    continue
                # Use the most recent bar
                latest = bars[-1]
                price  = float(latest.get("c", 0))   # close price
                volume = float(latest.get("v", 0))   # volume
                if price > 0:
                    results[sym] = {"price": round(price, 2), "volume": int(volume)}

            print(f"  Batch {i//batch_size+1}/{(len(symbols)-1)//batch_size+1}: {len(data)} symbols returned")

        except Exception as e:
            print(f"  Batch {i//batch_size+1} error: {e}")
            continue

    return results

def build():
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        print("ERROR: ALPACA_API_KEY / ALPACA_SECRET_KEY not set")
        exit(1)

    print(f"Building watchlist: price ${PRICE_MIN}–${PRICE_MAX}, volume >{MIN_VOLUME:,.0f}/day")
    print("=" * 60)

    # Step 1 — get all assets
    print("\nStep 1: Fetching all active US equities from Alpaca...")
    all_symbols = get_all_assets()
    if not all_symbols:
        print("ERROR: No assets returned")
        exit(1)

    # Step 2 — get snapshots in batches
    print(f"\nStep 2: Fetching snapshots for {len(all_symbols)} symbols...")
    snapshots = get_snapshots_batch(all_symbols)
    print(f"Snapshots received: {len(snapshots)}")

    # Step 3 — apply filters
    print(f"\nStep 3: Applying filters (${PRICE_MIN}–${PRICE_MAX}, vol>{MIN_VOLUME:,.0f})...")
    watchlist = []
    for sym, data in snapshots.items():
        price  = data["price"]
        volume = data["volume"]
        if PRICE_MIN <= price <= PRICE_MAX and volume >= MIN_VOLUME:
            watchlist.append({
                "symbol": sym,
                "price":  round(price, 2),
                "volume": int(volume),
            })

    # Sort by volume descending (most active first)
    watchlist.sort(key=lambda x: x["volume"], reverse=True)

    symbols_only = [s["symbol"] for s in watchlist]

    print(f"Stocks matching criteria: {len(watchlist)}")
    if watchlist:
        print(f"Top 10 by volume: {', '.join(symbols_only[:10])}")

    # Save
    os.makedirs("docs/data", exist_ok=True)
    output = {
        "generated_at":  datetime.now(ET_TZ).strftime("%Y-%m-%d %H:%M ET"),
        "filters": {
            "price_min":  PRICE_MIN,
            "price_max":  PRICE_MAX,
            "min_volume": MIN_VOLUME,
        },
        "total":    len(watchlist),
        "symbols":  symbols_only,
        "details":  watchlist,
    }
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nWatchlist saved to {OUTPUT_FILE} ({len(watchlist)} stocks)")
    return symbols_only

if __name__ == "__main__":
    build()
