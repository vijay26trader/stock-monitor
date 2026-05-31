"""
Backtest — Momentum Reversal Strategy
──────────────────────────────────────
Uses Alpaca Markets API for 1-min pre-market data (4 AM ET onwards).
yfinance is NOT used — it only returns regular market hours (9:30 AM+).

Free Alpaca account: https://app.alpaca.markets/signup
API keys go in GitHub Secrets:
  ALPACA_API_KEY
  ALPACA_SECRET_KEY

GitHub Actions inputs (all passed as env vars):
  START_DATE          e.g. 2026-05-22
  END_DATE            e.g. 2026-05-22
  WATCHLIST           e.g. AAPL,TSLA,PCLA
  MOMENTUM_THRESHOLD  e.g. 20.0
  REVERSAL_THRESHOLD  e.g. 2.0
"""

import json
import os
import requests
import pytz
from datetime import datetime, timedelta, date
import sys
sys.path.insert(0, os.path.dirname(__file__))

# ════════════════════════════════════════════════════════════════
# WINDOW SETTINGS  — read from env (set by workflow input)
# ════════════════════════════════════════════════════════════════

def _parse_hhmm(s, default_hour, default_minute):
    """Parse HH:MM string into (hour, minute). Falls back to defaults."""
    try:
        h, m = s.strip().split(":")
        return int(h), int(m)
    except Exception:
        return default_hour, default_minute

_ws = os.environ.get("WINDOW_START", "04:00")
_we = os.environ.get("WINDOW_END",   "05:00")

WINDOW_START_HOUR, WINDOW_START_MINUTE = _parse_hhmm(_ws, 4, 0)
WINDOW_END_HOUR,   WINDOW_END_MINUTE   = _parse_hhmm(_we, 5, 0)

OUTPUT_FILE = "docs/data/backtest.json"
ET_TZ       = pytz.timezone("America/New_York")

# ════════════════════════════════════════════════════════════════
# ALPACA CONFIG
# ════════════════════════════════════════════════════════════════

ALPACA_API_KEY    = os.environ.get("ALPACA_API_KEY",    "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")

# Alpaca data base URL (free tier uses iex feed; paid uses sip)
ALPACA_BASE = "https://data.alpaca.markets/v2"

def alpaca_headers():
    return {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }

# ════════════════════════════════════════════════════════════════
# READ INPUTS FROM ENV
# ════════════════════════════════════════════════════════════════

def parse_date(s):
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()

def parse_watchlist(s):
    return [t.strip().upper() for t in s.split(",") if t.strip()]

def parse_float(s, default):
    try:
        return float(s.strip())
    except Exception:
        print(f"  Could not parse '{s}' — using default {default}")
        return default

today = datetime.utcnow().date()

_start    = os.environ.get("START_DATE",         "")
_end      = os.environ.get("END_DATE",           "")
_watch    = os.environ.get("WATCHLIST",          "")
_momentum = os.environ.get("MOMENTUM_THRESHOLD", "")
_reversal = os.environ.get("REVERSAL_THRESHOLD", "")

START_DATE = parse_date(_start) if _start else today - timedelta(days=7)
END_DATE   = parse_date(_end)   if _end   else today - timedelta(days=1)

WATCHLIST_DETAILS = {}   # symbol -> {price, volume} from snapshot

# WATCHLIST_MODE controls how the watchlist is built when no symbols are passed:
#   top_movers  — Alpaca screener: top gainers + most actives (recommended)
#   price_range — All US stocks filtered by price $1-$20 and volume >100K
_mode = os.environ.get("WATCHLIST_MODE", "top_movers").strip().lower()

if _watch:
    WATCHLIST = parse_watchlist(_watch)
    print(f"Using provided watchlist: {WATCHLIST}")
elif _mode == "price_range":
    print("No watchlist provided — building from price range filter...")
    from build_watchlist import build
    WATCHLIST = build()
    if not WATCHLIST:
        print("ERROR: Dynamic watchlist is empty — check filters or API keys")
        exit(1)
    wl_file = "docs/data/watchlist.json"
    if os.path.exists(wl_file):
        with open(wl_file) as f:
            wl_data = json.load(f)
        WATCHLIST_DETAILS = {d["symbol"]: d for d in wl_data.get("details", [])}
    print(f"Price-range watchlist: {len(WATCHLIST)} stocks\n")
else:
    # Default: top movers from Alpaca screener
    print("No watchlist provided — fetching top momentum stocks from Alpaca Screener...")
    from get_top_movers import build_top_movers_watchlist
    WATCHLIST = build_top_movers_watchlist()
    if not WATCHLIST:
        print("ERROR: Top movers watchlist is empty — check API keys or Alpaca tier")
        exit(1)
    wl_file = "docs/data/watchlist.json"
    if os.path.exists(wl_file):
        with open(wl_file) as f:
            wl_data = json.load(f)
        WATCHLIST_DETAILS = {d["symbol"]: d for d in wl_data.get("details", [])}
    print(f"Top movers watchlist: {len(WATCHLIST)} stocks\n")

MOMENTUM_THRESHOLD_PCT = parse_float(_momentum, 20.0) if _momentum else 20.0
REVERSAL_THRESHOLD_PCT = parse_float(_reversal,  2.0) if _reversal else  2.0

print("=" * 60)
print(f"  Backtest  : {START_DATE} → {END_DATE}")
print(f"  Window    : {WINDOW_START_HOUR:02d}:{WINDOW_START_MINUTE:02d} – {WINDOW_END_HOUR:02d}:{WINDOW_END_MINUTE:02d} ET")
print(f"  Stocks    : {', '.join(WATCHLIST)}")
print(f"  Momentum  : >= +{MOMENTUM_THRESHOLD_PCT}%")
print(f"  Reversal  : >= -{REVERSAL_THRESHOLD_PCT}% from peak")
print(f"  Data src  : Alpaca Markets (pre-market capable)")
print("=" * 60)

if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
    print("\nERROR: ALPACA_API_KEY and ALPACA_SECRET_KEY must be set as GitHub Secrets.")
    print("  1. Sign up free at https://app.alpaca.markets/signup")
    print("  2. Go to repo Settings → Secrets → Actions → New secret")
    print("  3. Add ALPACA_API_KEY and ALPACA_SECRET_KEY")
    exit(1)

# ════════════════════════════════════════════════════════════════
# FETCH 1-MIN CANDLES FROM ALPACA
# ════════════════════════════════════════════════════════════════

def fetch_candles(symbol):
    """
    Returns dict keyed by ET date string:
      { 'YYYY-MM-DD': [ {time, open, high, low, close, volume}, ... ] }
    Includes pre-market candles (4 AM ET onwards).
    """
    # Alpaca needs RFC3339 UTC timestamps
    # Start = window start on START_DATE in ET → convert to UTC
    start_et = ET_TZ.localize(datetime(
        START_DATE.year, START_DATE.month, START_DATE.day,
        WINDOW_START_HOUR, WINDOW_START_MINUTE, 0
    ))
    # End = window end on END_DATE in ET → convert to UTC
    end_et = ET_TZ.localize(datetime(
        END_DATE.year, END_DATE.month, END_DATE.day,
        WINDOW_END_HOUR, WINDOW_END_MINUTE, 0
    ))

    start_utc = start_et.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_utc   = end_et.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    url    = f"{ALPACA_BASE}/stocks/{symbol}/bars"

    # Try SIP feed first (most complete, includes pre-market for all stocks)
    # Fall back to IEX if SIP returns nothing (IEX is subset of stocks only)
    by_day = {}
    for feed in ["sip", "iex"]:
        params = {
            "timeframe": "1Min",
            "start":     start_utc,
            "end":       end_utc,
            "feed":      feed,
            "limit":     10000,
        }

        next_token = None
        feed_bars  = 0


        while True:
            if next_token:
                params["page_token"] = next_token

            try:
                resp = requests.get(url, headers=alpaca_headers(), params=params, timeout=30)
            except Exception as e:
                print(f"  [{symbol}] request error: {e}")
                break


            if resp.status_code == 403:
                print(f"  [{symbol}] 403 — check ALPACA_API_KEY / ALPACA_SECRET_KEY in GitHub Secrets")
                return {}
            if resp.status_code == 422:
                print(f"  [{symbol}] 422 — ticker not found on {feed} feed, trying next feed")
                break
            if resp.status_code != 200:
                print(f"  [{symbol}] HTTP {resp.status_code}: {resp.text[:200]}")
                break

            data_json = resp.json()
            bars      = data_json.get("bars", []) or []
            feed_bars += len(bars)


            for bar in bars:
                ts_utc = datetime.strptime(bar["t"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.utc)
                ts_et  = ts_utc.astimezone(ET_TZ)

                mins      = ts_et.hour * 60 + ts_et.minute
                win_start = WINDOW_START_HOUR * 60 + WINDOW_START_MINUTE
                win_end   = WINDOW_END_HOUR   * 60 + WINDOW_END_MINUTE
                if not (win_start <= mins <= win_end):
                    continue

                day = ts_et.strftime("%Y-%m-%d")
                by_day.setdefault(day, []).append({
                    "time":   ts_et.strftime("%H:%M"),
                    "open":   round(float(bar["o"]), 4),
                    "high":   round(float(bar["h"]), 4),
                    "low":    round(float(bar["l"]), 4),
                    "close":  round(float(bar["c"]), 4),
                    "volume": int(bar["v"]),
                })

            next_token = data_json.get("next_page_token")
            if not next_token:
                break

        kept = sum(len(v) for v in by_day.values())

        if kept > 0:
            print(f"  [{symbol}] using feed={feed}")
            break   # got data, no need to try next feed
        else:
            by_day = {}   # reset for next feed attempt

        for bar in bars:
            # bar['t'] is RFC3339 UTC e.g. "2026-05-22T08:00:00Z"
            ts_utc = datetime.strptime(bar["t"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.utc)
            ts_et  = ts_utc.astimezone(ET_TZ)

            # Filter to window
            mins      = ts_et.hour * 60 + ts_et.minute
            win_start = WINDOW_START_HOUR * 60 + WINDOW_START_MINUTE
            win_end   = WINDOW_END_HOUR   * 60 + WINDOW_END_MINUTE
            if not (win_start <= mins <= win_end):
                continue

            day = ts_et.strftime("%Y-%m-%d")
            by_day.setdefault(day, []).append({
                "time":   ts_et.strftime("%H:%M"),
                "open":   round(float(bar["o"]), 4),
                "high":   round(float(bar["h"]), 4),
                "low":    round(float(bar["l"]), 4),
                "close":  round(float(bar["c"]), 4),
                "volume": int(bar["v"]),
            })

    kept = sum(len(v) for v in by_day.values())
    print(f"  [{symbol}] {kept} candle(s) in window across {len(by_day)} day(s)")
    return by_day

# ════════════════════════════════════════════════════════════════
# STATE MACHINE  (identical logic to stock_monitor.py)
# ════════════════════════════════════════════════════════════════

def run_day(symbol, candles):
    if not candles:
        return None

    state          = "WATCHING"
    baseline_price = None
    baseline_time  = None
    peak_price     = None
    peak_time      = None
    momentum_pct   = None
    momentum_time  = None

    for c in candles:
        price = c["close"]
        t     = c["time"]

        if state == "WATCHING":
            if baseline_price is None:
                baseline_price = c["open"]   # use open of first candle, not close
                baseline_time  = t

            pct = (price - baseline_price) / baseline_price * 100
            if pct >= MOMENTUM_THRESHOLD_PCT:
                state         = "MOMENTUM"
                momentum_pct  = round(pct, 2)
                momentum_time = t
                peak_price    = price
                peak_time     = t

        elif state == "MOMENTUM":
            if price > peak_price:
                peak_price = price
                peak_time  = t

            drop = (peak_price - price) / peak_price * 100
            if drop >= REVERSAL_THRESHOLD_PCT:
                return {
                    "symbol":         symbol,
                    "avg_volume":     int(WATCHLIST_DETAILS.get(symbol, {}).get("volume", 0)) or None,
                    "baseline_price": baseline_price,
                    "baseline_time":  baseline_time,
                    "momentum_pct":   momentum_pct,
                    "momentum_time":  momentum_time,
                    "peak_price":     round(peak_price, 4),
                    "peak_time":      peak_time,
                    "reversal_price": round(price, 4),
                    "reversal_time":  t,
                    "reversal_pct":   round(drop, 2),
                    "status":         "REVERSED",
                }

    if state == "MOMENTUM":
        return {
            "symbol":         symbol,
            "baseline_price": baseline_price,
            "baseline_time":  baseline_time,
            "momentum_pct":   momentum_pct,
            "momentum_time":  momentum_time,
            "avg_volume":     int(WATCHLIST_DETAILS.get(symbol, {}).get("volume", 0)) or None,
            "peak_price":     round(peak_price, 4),
            "peak_time":      peak_time,
            "reversal_price": None,
            "reversal_time":  None,
            "reversal_pct":   None,
            "status":         "MOMENTUM",
            "note":           "Momentum hit but no reversal within window",
        }

    return None

# ════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════

def trading_days(start, end):
    days, d = [], start
    while d <= end:
        if d.weekday() < 5:
            days.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return days

def main():
    all_days = trading_days(START_DATE, END_DATE)
    results  = []
    summary  = {
        "total_days":      len(all_days),
        "total_reversals": 0,
        "total_momentum":  0,
        "by_symbol":       {},
    }

    print(f"\nFetching pre-market 1-min data for {len(WATCHLIST)} stock(s)...")
    all_candles = {}
    for sym in WATCHLIST:
        print(f"  {sym}...")
        all_candles[sym] = fetch_candles(sym)

    print(f"\nReplaying {len(all_days)} trading day(s)...\n")

    for day in all_days:
        print(f"── {day} ──")
        day_hits = 0

        for sym in WATCHLIST:
            candles = all_candles[sym].get(day, [])
            if not candles:
                print(f"  [{sym}] no data for this day/window")
                continue

            result = run_day(sym, candles)
            if result:
                result["date"] = day
                results.append(result)
                day_hits += 1

                if result["status"] == "REVERSED":
                    summary["total_reversals"] += 1
                    print(
                        f"  [{sym}] REVERSAL  "
                        f"baseline=${result['baseline_price']} ({result['baseline_time']}) "
                        f"-> peak=${result['peak_price']} +{result['momentum_pct']}% ({result['peak_time']}) "
                        f"-> reversal=${result['reversal_price']} -{result['reversal_pct']}% ({result['reversal_time']})"
                    )
                else:
                    summary["total_momentum"] += 1
                    print(f"  [{sym}] MOMENTUM only — +{result['momentum_pct']}% (no reversal in window)")

                s = summary["by_symbol"].setdefault(sym, {"reversals": 0, "momentum_only": 0, "days": []})
                s["days"].append(day)
                if result["status"] == "REVERSED":
                    s["reversals"] += 1
                else:
                    s["momentum_only"] += 1
            else:
                print(f"  [{sym}] no signal")

        if day_hits == 0:
            print("  (no signals today)")

    output = {
        "generated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "start_date":   START_DATE.strftime("%Y-%m-%d"),
        "end_date":     END_DATE.strftime("%Y-%m-%d"),
        "data_source":  "Alpaca Markets (SIP/IEX feed)",
        "window_config": {
            "start":              f"{WINDOW_START_HOUR:02d}:{WINDOW_START_MINUTE:02d} ET",
            "end":                f"{WINDOW_END_HOUR:02d}:{WINDOW_END_MINUTE:02d} ET",
            "momentum_threshold": MOMENTUM_THRESHOLD_PCT,
            "reversal_threshold": REVERSAL_THRESHOLD_PCT,
        },
        "watchlist": WATCHLIST,
        "summary":   summary,
        "results":   sorted(results, key=lambda r: (r["date"], r["symbol"])),
    }

    print(f"\n  DEBUG: results list has {len(results)} item(s) before save")
    print(f"  DEBUG: writing to {OUTPUT_FILE}")

    os.makedirs("docs/data", exist_ok=True)
    try:
        with open(OUTPUT_FILE, "w") as f:
            json.dump(output, f, indent=2)
        print(f"  DEBUG: file written successfully")
        # Verify by reading back
        with open(OUTPUT_FILE) as f:
            written = json.load(f)
        print(f"  DEBUG: verified — results in file: {len(written.get('results', []))}")
    except Exception as e:
        print(f"  ERROR writing JSON: {e}")
        import traceback; traceback.print_exc()
        raise

    print(f"\n{'=' * 60}")
    print(f"  Backtest complete")
    print(f"  Days scanned  : {len(all_days)}")
    print(f"  Stocks        : {', '.join(WATCHLIST)}")
    print(f"  Momentum %    : {MOMENTUM_THRESHOLD_PCT}%")
    print(f"  Reversal %    : {REVERSAL_THRESHOLD_PCT}%")
    print(f"  Reversals     : {summary['total_reversals']}")
    print(f"  Momentum only : {summary['total_momentum']}")
    print(f"  Output        : {OUTPUT_FILE}")
    print(f"{'=' * 60}")

if __name__ == "__main__":
    main()
