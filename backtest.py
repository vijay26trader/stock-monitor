"""
Backtest — Momentum Reversal Strategy
──────────────────────────────────────
Fetches historical 1-min candles (up to 30 days back via yfinance)
and replays the exact same state machine as stock_monitor.py
for each day in the date range, within the configured time window.

Results written to docs/data/backtest.json for the dashboard.

Usage (via GitHub Actions inputs):
  START_DATE  e.g. 2025-05-01
  END_DATE    e.g. 2025-05-20
"""

import yfinance as yf
import json
import os
import sys
from datetime import datetime, timedelta, timezone

# ════════════════════════════════════════════════════════════════
# CONFIG — keep in sync with stock_monitor.py
# ════════════════════════════════════════════════════════════════

WATCHLIST = ["AAPL", "MSFT", "GOOGL", "TSLA", "AMZN", "NVDA", "META", "SPY", "QQQ", "AMD"]

WINDOW_START_HOUR   = 4
WINDOW_START_MINUTE = 0
WINDOW_END_HOUR     = 5
WINDOW_END_MINUTE   = 0

MOMENTUM_THRESHOLD_PCT = 20.0
REVERSAL_THRESHOLD_PCT = 2.0

OUTPUT_FILE = "docs/data/backtest.json"

# ════════════════════════════════════════════════════════════════
# DATE RANGE  (from env or defaults)
# ════════════════════════════════════════════════════════════════

def parse_date(s):
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()

START_DATE_STR = os.environ.get("START_DATE", "")
END_DATE_STR   = os.environ.get("END_DATE",   "")

today = datetime.utcnow().date()

if START_DATE_STR:
    START_DATE = parse_date(START_DATE_STR)
else:
    START_DATE = today - timedelta(days=7)

if END_DATE_STR:
    END_DATE = parse_date(END_DATE_STR)
else:
    END_DATE = today - timedelta(days=1)

# yfinance 1-min data is only available for the last 30 days
EARLIEST = today - timedelta(days=29)
if START_DATE < EARLIEST:
    print(f"⚠️  yfinance only provides 1-min data for the last 30 days. Clamping start to {EARLIEST}.")
    START_DATE = EARLIEST

print(f"Backtesting {START_DATE} → {END_DATE}  |  window {WINDOW_START_HOUR:02d}:{WINDOW_START_MINUTE:02d}–{WINDOW_END_HOUR:02d}:{WINDOW_END_MINUTE:02d} ET")
print(f"Stocks: {WATCHLIST}\n")

# ════════════════════════════════════════════════════════════════
# FETCH 1-MIN CANDLES FOR A SYMBOL OVER THE DATE RANGE
# ════════════════════════════════════════════════════════════════

ET_OFFSET = timezone(timedelta(hours=-4))   # EDT; change to -5 for EST

def fetch_candles(symbol):
    """
    Returns dict: { 'YYYY-MM-DD': [ {time, open, high, low, close, volume}, ... ] }
    Only candles within the configured window are included.
    """
    try:
        t = yf.Ticker(symbol)
        # fetch max available 1-min history
        hist = t.history(
            start=START_DATE.strftime("%Y-%m-%d"),
            end=(END_DATE + timedelta(days=1)).strftime("%Y-%m-%d"),
            interval="1m",
        )
        if hist.empty:
            return {}

        # Convert index to ET
        hist.index = hist.index.tz_convert(ET_OFFSET)

        by_day = {}
        for ts, row in hist.iterrows():
            # filter to window
            h, m = ts.hour, ts.minute
            win_start = h * 60 + m >= WINDOW_START_HOUR * 60 + WINDOW_START_MINUTE
            win_end   = h * 60 + m <= WINDOW_END_HOUR   * 60 + WINDOW_END_MINUTE
            if not (win_start and win_end):
                continue

            day = ts.strftime("%Y-%m-%d")
            if day not in by_day:
                by_day[day] = []
            by_day[day].append({
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
    Replay one day's 1-min candles through the state machine.
    Returns result dict or None if no reversal.
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
                }

    # Window ended — return partial info if momentum was seen
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
            "note":           "momentum hit but no reversal within window",
        }

    return None   # no momentum hit

# ════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════

def trading_days(start, end):
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:   # Mon–Fri
            days.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return days

def main():
    all_days    = trading_days(START_DATE, END_DATE)
    results     = []   # one entry per (day, symbol) that had momentum or reversal
    summary     = {
        "total_days":      len(all_days),
        "total_reversals": 0,
        "total_momentum":  0,
        "by_symbol":       {},
    }

    # Fetch all candles upfront (one API call per symbol)
    print("Fetching 1-min candle data…")
    all_candles = {}
    for sym in WATCHLIST:
        print(f"  {sym}…")
        all_candles[sym] = fetch_candles(sym)

    print(f"\nReplaying {len(all_days)} trading day(s)…\n")

    for day in all_days:
        print(f"── {day} ──")
        day_hits = 0
        for sym in WATCHLIST:
            candles = all_candles[sym].get(day, [])
            if not candles:
                print(f"  [{sym}] no data")
                continue

            result = run_day(sym, candles)
            if result:
                result["date"] = day
                results.append(result)
                day_hits += 1

                if result["reversal_price"] is not None:
                    summary["total_reversals"] += 1
                    print(
                        f"  [{sym}] ✅ REVERSAL  baseline=${result['baseline_price']} "
                        f"→ peak=${result['peak_price']} ({result['momentum_pct']:+.2f}%) "
                        f"→ reversal=${result['reversal_price']} (-{result['reversal_pct']:.2f}%)"
                        f"  [{result['baseline_time']} → {result['peak_time']} → {result['reversal_time']}]"
                    )
                else:
                    summary["total_momentum"] += 1
                    print(f"  [{sym}] ⚡ momentum only (no reversal in window)")

                # per-symbol stats
                s = summary["by_symbol"].setdefault(sym, {"reversals": 0, "momentum_only": 0, "days": []})
                s["days"].append(day)
                if result["reversal_price"] is not None:
                    s["reversals"] += 1
                else:
                    s["momentum_only"] += 1
            else:
                print(f"  [{sym}] no signal")

        if day_hits == 0:
            print("  (no signals today)")

    # Save
    output = {
        "generated_at":   datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "start_date":     START_DATE.strftime("%Y-%m-%d"),
        "end_date":       END_DATE.strftime("%Y-%m-%d"),
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

    print(f"\n{'═'*50}")
    print(f"Backtest complete.")
    print(f"  Days scanned  : {len(all_days)}")
    print(f"  Reversals     : {summary['total_reversals']}")
    print(f"  Momentum only : {summary['total_momentum']}")
    print(f"  Results saved : {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
