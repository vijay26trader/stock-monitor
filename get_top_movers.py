"""
get_top_movers.py
──────────────────
Fetches top momentum stocks from Alpaca Screener API:
  - Top gainers (% change from prev close)
  - Most actives by volume
Combines and deduplicates into a single watchlist.

Used by backtest.py and stock_monitor.py when no watchlist is provided.

Endpoints used (free tier):
  GET /v1beta1/screener/stocks/movers      — top gainers/losers
  GET /v1beta1/screener/stocks/most-actives — top by volume

Note: These endpoints reflect the PREVIOUS trading day before market open.
For 4–5 AM scanning, this is exactly what you want — yesterday's hot stocks
tend to have pre-market continuation moves.
"""

import requests
import json
import os
import pytz
from datetime import datetime

ALPACA_API_KEY    = os.environ.get("ALPACA_API_KEY",    "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_SCREENER   = "https://data.alpaca.markets/v1beta1/screener/stocks"

# How many top stocks to fetch from each endpoint
TOP_N         = int(os.environ.get("TOP_N",      "50"))
PRICE_MIN     = float(os.environ.get("PRICE_MIN", "1"))
PRICE_MAX     = float(os.environ.get("PRICE_MAX", "20"))

ET_TZ         = pytz.timezone("America/New_York")
OUTPUT_FILE   = "docs/data/watchlist.json"

def alpaca_headers():
    return {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }

def get_top_gainers(top=50):
    """
    Fetch top gainers by % change from previous close.
    Returns list of { symbol, price, change_pct }
    """
    url    = f"{ALPACA_SCREENER}/movers"
    params = {"top": top, "market_type": "stocks"}

    try:
        resp = requests.get(url, headers=alpaca_headers(), params=params, timeout=15)
        if resp.status_code == 403:
            print("  Movers: 403 — endpoint may require higher Alpaca tier, skipping")
            return []
        if resp.status_code != 200:
            print(f"  Movers: HTTP {resp.status_code} — {resp.text[:120]}")
            return []

        data    = resp.json()
        gainers = data.get("gainers", [])
        print(f"  Movers API: {len(gainers)} gainers returned")
        return [
            {
                "symbol":     g["symbol"],
                "price":      round(float(g.get("price", 0)), 2),
                "change_pct": round(float(g.get("percent_change", 0)), 2),
                "source":     "top_gainer",
            }
            for g in gainers
            if g.get("price")
        ]
    except Exception as e:
        print(f"  Movers fetch error: {e}")
        return []

def get_most_actives(top=50):
    """
    Fetch most active stocks by volume.
    Returns list of { symbol, volume }
    """
    url    = f"{ALPACA_SCREENER}/most-actives"
    params = {"top": top, "by": "volume"}

    try:
        resp = requests.get(url, headers=alpaca_headers(), params=params, timeout=15)
        if resp.status_code == 403:
            print("  Most actives: 403 — endpoint may require higher tier, skipping")
            return []
        if resp.status_code != 200:
            print(f"  Most actives: HTTP {resp.status_code} — {resp.text[:120]}")
            return []

        data    = resp.json()
        actives = data.get("most_actives", [])
        print(f"  Most actives API: {len(actives)} stocks returned")
        return [
            {
                "symbol": a["symbol"],
                "volume": int(a.get("volume", 0)),
                "source": "most_active",
            }
            for a in actives
            if a.get("symbol")
        ]
    except Exception as e:
        print(f"  Most actives fetch error: {e}")
        return []

def build_top_movers_watchlist():
    """
    Combine gainers + most actives, apply price filter, deduplicate.
    Returns list of symbols.
    """
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        print("ERROR: ALPACA_API_KEY / ALPACA_SECRET_KEY not set")
        return []

    print(f"Fetching top {TOP_N} momentum stocks from Alpaca Screener...")
    print(f"Price filter: ${PRICE_MIN}–${PRICE_MAX}")
    print("=" * 60)

    gainers = get_top_gainers(TOP_N)
    actives = get_most_actives(TOP_N)

    if not gainers and not actives:
        print("ERROR: Both screener endpoints failed — check API keys or Alpaca tier")
        return []

    # Merge into a dict keyed by symbol
    merged = {}
    for g in gainers:
        sym = g["symbol"]
        merged[sym] = {
            "symbol":     sym,
            "price":      g.get("price", 0),
            "change_pct": g.get("change_pct", 0),
            "volume":     0,
            "sources":    ["top_gainer"],
        }
    for a in actives:
        sym = a["symbol"]
        if sym in merged:
            merged[sym]["volume"]  = a.get("volume", 0)
            merged[sym]["sources"].append("most_active")
        else:
            merged[sym] = {
                "symbol":     sym,
                "price":      0,    # price not available from actives endpoint
                "change_pct": 0,
                "volume":     a.get("volume", 0),
                "sources":    ["most_active"],
            }

    # Fetch latest price for symbols that came only from most_actives (price=0)
    unknown_price_syms = [sym for sym, d in merged.items() if d["price"] == 0]
    if unknown_price_syms:
        print(f"  Looking up prices for {len(unknown_price_syms)} most-actives symbols...")
        from datetime import date, timedelta
        ref = date.today()
        for _ in range(7):
            if ref.weekday() < 5:
                break
            ref -= timedelta(days=1)
        start = (ref - timedelta(days=5)).strftime("%Y-%m-%d")
        end   = ref.strftime("%Y-%m-%d")

        batch_size = 200
        for i in range(0, len(unknown_price_syms), batch_size):
            batch  = unknown_price_syms[i:i+batch_size]
            url    = f"https://data.alpaca.markets/v2/stocks/bars"
            params = {
                "symbols":   ",".join(batch),
                "timeframe": "1Day",
                "start":     start,
                "end":       end,
                "feed":      "sip",
                "limit":     5,
            }
            try:
                resp = requests.get(url, headers=alpaca_headers(), params=params, timeout=30)
                if resp.status_code != 200:
                    params["feed"] = "iex"
                    resp = requests.get(url, headers=alpaca_headers(), params=params, timeout=30)
                if resp.status_code == 200:
                    bars_data = resp.json().get("bars", {}) or {}
                    for sym, bars in bars_data.items():
                        if bars and sym in merged:
                            merged[sym]["price"] = round(float(bars[-1]["c"]), 2)
            except Exception as e:
                print(f"  Price lookup error: {e}")

    # Apply price filter — now all symbols have a known price (or 0 if truly unavailable)
    filtered = []
    skipped_price = 0
    for sym, d in merged.items():
        price = d["price"]
        if price == 0:
            # Still unknown after lookup — skip to enforce price filter strictly
            skipped_price += 1
        elif PRICE_MIN <= price <= PRICE_MAX:
            filtered.append(d)
        else:
            skipped_price += 1

    # Sort: stocks appearing in both lists first, then by change_pct
    filtered.sort(key=lambda x: (len(x["sources"]) == 2, x["change_pct"]), reverse=True)

    symbols = [d["symbol"] for d in filtered]

    print(f"\nResults:")
    print(f"  Total unique symbols  : {len(merged)}")
    print(f"  After price filter    : {len(filtered)}  (skipped {skipped_price} outside ${PRICE_MIN}–${PRICE_MAX})")
    print(f"  In both lists         : {sum(1 for d in filtered if len(d['sources'])==2)}")
    if symbols:
        print(f"  Top 10               : {', '.join(symbols[:10])}")

    # Save watchlist.json
    os.makedirs("docs/data", exist_ok=True)
    output = {
        "generated_at": datetime.now(ET_TZ).strftime("%Y-%m-%d %H:%M ET"),
        "method":       "top_movers_screener",
        "filters": {
            "price_min": PRICE_MIN,
            "price_max": PRICE_MAX,
            "top_n":     TOP_N,
        },
        "total":   len(filtered),
        "symbols": symbols,
        "details": filtered,
    }
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)

    print(f"  Saved to {OUTPUT_FILE}")
    return symbols

if __name__ == "__main__":
    syms = build_top_movers_watchlist()
    print(f"\nFinal watchlist: {syms}")
