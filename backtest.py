"""
Backtest — Momentum Reversal Strategy
──────────────────────────────────────
Fetches historical 1-min candles (up to 30 days back via yfinance)
and replays the exact same state machine as stock_monitor.py
for each day in the date range, within the configured time window.

Results written to docs/data/backtest.json for the dashboard.

GitHub Actions inputs (all passed as env vars):
  START_DATE          e.g. 2025-05-01
  END_DATE            e.g. 2025-05-20
  WATCHLIST           e.g. AAPL,TSLA,INTC
  MOMENTUM_THRESHOLD  e.g. 20.0
  REVERSAL_THRESHOLD  e.g. 2.0
"""

import yfinance as yf
import json
import os
import pytz
from datetime import datetime, timedelta, timezone

# ════════════════════════════════════════════════════════════════
# FIXED WINDOW SETTINGS  (edit here if needed)
# ════════════════════════════════════════════════════════════════

WINDOW_START_HOUR   = 4
WINDOW_START_MINUTE = 0
WINDOW_END_HOUR     = 5
WINDOW_END_MINUTE   = 0

OUTPUT_FILE = "docs/data/backtest.json"

# ════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════

def parse_date(s):
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()

def parse_watchlist(s):
    """Comma-separated tickers → clean uppercase list."""
    return [t.strip().upper() for t in s.split(",") if t.strip()]

def parse_float(s, default):
    try:
        return float(s.strip())
    except Exception:
        print(f"  Could not parse '{s}' as a number — using default {default}")
        return default

# ════════════════════════════════════════════════════════════════
# READ ALL INPUTS FROM ENV  (set by GitHub Actions workflow)
# ════════════════════════════════════════════════════════════════

today = datetime.utcnow().date()

_start    = os.environ.get("START_DATE",         "")
_end      = os.environ.get("END_DATE",           "")
_watch    = os.environ.get("WATCHLIST",          "")
_momentum = os.environ.get("MOMENTUM_THRESHOLD", "")
_reversal = os.environ.get("REVERSAL_THRESHOLD", "")

START_DATE = parse_date(_start) if _start else today - timedelta(days=7)
END_DATE   = parse_date(_end)   if _end   else today - timedelta(days=1)

WATCHLIST = (
    parse_watchlist(_watch) if _watch
    else ["AAPL", "MSFT", "GOOGL", "TSLA", "AMZN", "NVDA", "META", "SPY", "QQQ", "AMD"]
)

MOMENTUM_THRESHOLD_PCT = parse_float(_momentum, 20.0) if _momentum else 20.0
REVERSAL_THRESHOLD_PCT = parse_float(_reversal,  2.0) if _reversal else  2.0

# yfinance caps 1-min history at 30 days
EARLIEST = today - timedelta(days=29)
if START_DATE < EARLIEST:
    print(f"  yfinance only provides 1-min data for the last 30 days. Clamping start to {EARLIEST}.")
    START_DATE = EARLIEST

print("=" * 60)
print(f"  Backtest  : {START_DATE} → {END_DATE}")
print(f"  Window    : {WINDOW_START_HOUR:02d}:{WINDOW_START_MINUTE:02d} – {WINDOW_END_HOUR:02d}:{WINDOW_END_MINUTE:02d} ET")
print(f"  Stocks    : {', '.join(WATCHLIST)}")
print(f"  Momentum  : >= +{MOMENTUM_THRESHOLD_PCT}%")
print(f"  Reversal  : >= -{REVERSAL_THRESHOLD_PCT}% from peak")
print("=" * 60)

# ════════════════════════════════════════════════════════════════
# TIMEZONE
# ════════════════════════════════════════════════════════════════

ET_TZ = pytz.timezone("America/New_York")  # auto-handles EDT/EST

# ════════════════════════════════════════════════════════════════
# FETCH 1-MIN CANDLES
# ════════════════════════════════════════════════════════════════

def fetch_candles(symbol):
    """
    Returns dict keyed by date string:
      { 'YYYY-MM-DD': [ {time, open, high, low, close, volume}, ... ] }
    Only candles within the configured time window are included.
    """
    try:
        t = yf.Ticker(symbol)
        hist = t.history(
            start=START_DATE.strftime("%Y-%m-%d"),
            end=(END_DATE + timedelta(days=1)).strftime("%Y-%m-%d"),
            interval="1m",
        )
        if hist.empty:
            return {}

        hist.index = hist.index.tz_convert(ET_TZ)
        by_day = {}

        for ts, row in hist.iterrows():
            h, m = ts.hour, ts.minute
            mins = h * 60 + m
            win_start = WINDOW_START_HOUR * 60 + WINDOW_START_MINUTE
            win_end   = WINDOW_END_HOUR   * 60 + WINDOW_END_MINUTE
            if not (win_start <= mins <= win_end):
                continue

            day = ts.strftime("%Y-%m-%d")
            by_day.setdefault(day, []).append({
                "time":   ts.strftime("%H:%M"),
                "open":   round(float(row["Open"]),  4),
                "high":   round(float(row["High"]),  4),
                "low":    round(float(row["Low"]),   4),
                "close":  round(float(row["Close"]), 4),
                "volume": int(row["Volume"]),
            })
        return by_day

    except Exception as e:
        print(f"  [{symbol}] fetch error: {e}")
        return {}

# ════════════════════════════════════════════════════════════════
# STATE MACHINE  (identical logic to stock_monitor.py)
# ════════════════════════════════════════════════════════════════

def run_day(symbol, candles):
    """
    Replay one day's candles through the momentum → reversal state machine.
    Returns a result dict, or None if no momentum was ever reached.
    """
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
                baseline_price = price
                baseline_time  = t

            pct = (price - baseline_price) / baseline_price * 100
            if pct >= MOMENTUM_THRESHOLD_PCT:
                state         = "MOMENTUM"
                momentum_pct  = round(pct, 2)
                momentum_time = t
                peak_price    = price
                peak_time     = t

        elif state == "MOMENTUM":
            # Update rolling peak
            if price > peak_price:
                peak_price = price
                peak_time  = t

            drop = (peak_price - price) / peak_price * 100
            if drop >= REVERSAL_THRESHOLD_PCT:
                return {
                    "symbol":         symbol,
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

    # Window ended — momentum hit but no reversal confirmed
    if state == "MOMENTUM":
        return {
            "symbol":         symbol,
            "baseline_price": baseline_price,
            "baseline_time":  baseline_time,
            "momentum_pct":   momentum_pct,
            "momentum_time":  momentum_time,
            "peak_price":     round(peak_price, 4),
            "peak_time":      peak_time,
            "reversal_price": None,
            "reversal_time":  None,
            "reversal_pct":   None,
            "status":         "MOMENTUM",
            "note":           "Momentum hit but no reversal within window",
        }

    return None   # never reached momentum threshold

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

    print(f"\nFetching 1-min candle data for {len(WATCHLIST)} stock(s)...")
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

    os.makedirs("docs/data", exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)

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
