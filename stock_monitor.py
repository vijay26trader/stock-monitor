"""
Stock Momentum + First Reversal Detector
─────────────────────────────────────────
State machine per stock:
  WATCHING   → baseline set, waiting for 20%+ upward momentum
  MOMENTUM   → 20%+ move confirmed, tracking peak, waiting for reversal
  REVERSED   → price pulled back >= REVERSAL_THRESHOLD_PCT from peak → DONE

Only upward momentum (price rises 20%+) is tracked.
Reversal = price drops >= REVERSAL_THRESHOLD_PCT from the peak price.

Example:
  4:00 AM  baseline = $2.00          (WATCHING)
  4:07 AM  price    = $2.50  +25%    (MOMENTUM confirmed, peak=$2.50)
  4:08 AM  price    = $2.55          (still rising, new peak=$2.55)
  4:10 AM  price    = $2.45  -3.9%   (REVERSED if threshold <= 3.9%)
  → Report: symbol, baseline, peak, reversal price, reversal time
"""

import yfinance as yf
import json
import os
from datetime import datetime, timezone, timedelta

# ════════════════════════════════════════════════════════════════
# CONFIG  — edit these values
# ════════════════════════════════════════════════════════════════

WATCHLIST = ["AAPL", "MSFT", "GOOGL", "TSLA", "AMZN", "NVDA", "META", "SPY", "QQQ", "AMD"]

# Monitoring window (ET)
WINDOW_START_HOUR   = 4    # e.g. 4 AM ET
WINDOW_START_MINUTE = 0
WINDOW_END_HOUR     = 5    # e.g. 5 AM ET
WINDOW_END_MINUTE   = 0

# Momentum: upward move % from baseline to qualify
MOMENTUM_THRESHOLD_PCT = 20.0   # e.g. 20%

# Reversal: pullback % from peak to trigger reversal signal
REVERSAL_THRESHOLD_PCT = 2.0    # e.g. 2% drop from peak

# Output (committed to repo, read by GitHub Pages)
OUTPUT_FILE = "data/stocks.json"

# ════════════════════════════════════════════════════════════════
# TIMEZONE
# ════════════════════════════════════════════════════════════════

ET_OFFSET = timezone(timedelta(hours=-4))   # EDT (UTC-4); use -5 for EST winter

def now_et():
    return datetime.now(ET_OFFSET)

def fmt(dt_or_str):
    if isinstance(dt_or_str, str):
        return dt_or_str
    return dt_or_str.strftime("%H:%M ET")

# ════════════════════════════════════════════════════════════════
# WINDOW HELPERS
# ════════════════════════════════════════════════════════════════

def window_bounds(now):
    start = now.replace(hour=WINDOW_START_HOUR, minute=WINDOW_START_MINUTE, second=0, microsecond=0)
    end   = now.replace(hour=WINDOW_END_HOUR,   minute=WINDOW_END_MINUTE,   second=0, microsecond=0)
    return start, end

def in_window(now):
    s, e = window_bounds(now)
    return s <= now <= e

def mins_elapsed(now):
    s, _ = window_bounds(now)
    return max(0, int((now - s).total_seconds() // 60))

# ════════════════════════════════════════════════════════════════
# DATA PERSISTENCE
# ════════════════════════════════════════════════════════════════

EMPTY_STATE = {
    "updated": None,
    "window_active": False,
    "window_config": {},
    "last_scan_date": None,
    # per-symbol state keyed by symbol
    # state fields: status, baseline_price, baseline_time,
    #               peak_price, peak_time,
    #               reversal_price, reversal_time, momentum_pct, reversal_pct
    "tracker": {},
    # final results list (only REVERSED stocks)
    "reversals": [],
    # latest scan snapshot for dashboard table
    "scans": [],
}

def load():
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE) as f:
            return json.load(f)
    return dict(EMPTY_STATE)

def save(data):
    os.makedirs("data", exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(data, f, indent=2)

# ════════════════════════════════════════════════════════════════
# MARKET DATA
# ════════════════════════════════════════════════════════════════

def get_candles(symbol):
    """Return last 90 minutes of 1-min OHLCV candles as list of dicts."""
    try:
        t = yf.Ticker(symbol)
        hist = t.history(period="1d", interval="1m")
        if hist.empty:
            return []
        rows = []
        for ts, row in hist.iterrows():
            rows.append({
                "time": ts.strftime("%H:%M"),
                "open":  round(float(row["Open"]),  4),
                "high":  round(float(row["High"]),  4),
                "low":   round(float(row["Low"]),   4),
                "close": round(float(row["Close"]), 4),
                "volume": int(row["Volume"]),
            })
        return rows
    except Exception as e:
        print(f"  [{symbol}] fetch error: {e}")
        return []

# ════════════════════════════════════════════════════════════════
# STATE MACHINE
# ════════════════════════════════════════════════════════════════
# States:
#   WATCHING   — waiting for 20%+ upward move from baseline
#   MOMENTUM   — 20%+ move seen, tracking rolling peak for reversal
#   REVERSED   — reversal confirmed (terminal)
#   SKIPPED    — window ended without momentum (terminal for today)

def process_symbol(symbol, candles, tracker, now_str, elapsed):
    """
    Update the state machine for one symbol given fresh candles.
    Returns updated tracker entry dict.
    """
    entry = tracker.get(symbol, {"status": "WATCHING"})
    status = entry.get("status", "WATCHING")

    # Terminal states — nothing more to do
    if status in ("REVERSED", "SKIPPED"):
        # Still update latest price for the dashboard table
        if candles:
            entry["latest_price"] = candles[-1]["close"]
            entry["latest_time"]  = candles[-1]["time"]
        return entry

    if not candles:
        return entry

    latest = candles[-1]
    price  = latest["close"]
    entry["latest_price"] = price
    entry["latest_time"]  = latest["time"]

    # ── WATCHING: set baseline on first scan, then watch for 20%+ rise ──
    if status == "WATCHING":
        if "baseline_price" not in entry:
            entry["baseline_price"] = price
            entry["baseline_time"]  = latest["time"]
            print(f"  [{symbol}] baseline set ${price} @ {latest['time']}")

        baseline = entry["baseline_price"]
        pct_from_baseline = (price - baseline) / baseline * 100

        if pct_from_baseline >= MOMENTUM_THRESHOLD_PCT:
            entry["status"]        = "MOMENTUM"
            entry["momentum_pct"]  = round(pct_from_baseline, 2)
            entry["momentum_time"] = latest["time"]
            entry["peak_price"]    = price
            entry["peak_time"]     = latest["time"]
            print(f"  [{symbol}] ✅ MOMENTUM +{pct_from_baseline:.2f}% @ {latest['time']} (peak=${price})")
        else:
            print(f"  [{symbol}] watching — ${price} ({pct_from_baseline:+.2f}% from baseline)")

    # ── MOMENTUM: track rolling peak, detect reversal ────────────────────
    elif status == "MOMENTUM":
        peak = entry.get("peak_price", price)

        # Update rolling peak if price still rising
        if price > peak:
            entry["peak_price"] = price
            entry["peak_time"]  = latest["time"]
            peak = price
            print(f"  [{symbol}] new peak ${peak} @ {latest['time']}")

        # Check reversal: drop from peak >= REVERSAL_THRESHOLD_PCT
        drop_from_peak = (peak - price) / peak * 100

        if drop_from_peak >= REVERSAL_THRESHOLD_PCT:
            entry["status"]         = "REVERSED"
            entry["reversal_price"] = price
            entry["reversal_time"]  = latest["time"]
            entry["reversal_pct"]   = round(drop_from_peak, 2)
            print(
                f"  [{symbol}] 🔻 REVERSAL ${price} @ {latest['time']} "
                f"(down {drop_from_peak:.2f}% from peak ${peak})"
            )
        else:
            print(
                f"  [{symbol}] momentum — peak=${peak}, now=${price}, "
                f"pullback={drop_from_peak:.2f}% (need {REVERSAL_THRESHOLD_PCT}%)"
            )

    return entry

# ════════════════════════════════════════════════════════════════
# MAIN RUN
# ════════════════════════════════════════════════════════════════

def run():
    now    = now_et()
    data   = load()
    active = in_window(now)
    today  = now.strftime("%Y-%m-%d")

    # Reset on new day
    if data.get("last_scan_date") != today:
        data["tracker"]   = {}
        data["reversals"] = []
        data["scans"]     = []
        data["last_scan_date"] = today
        print(f"New day {today} — state reset.")

    data["window_active"] = active
    data["updated"]       = now.strftime("%Y-%m-%d %H:%M:%S ET")
    data["window_config"] = {
        "start":               f"{WINDOW_START_HOUR:02d}:{WINDOW_START_MINUTE:02d} ET",
        "end":                 f"{WINDOW_END_HOUR:02d}:{WINDOW_END_MINUTE:02d} ET",
        "momentum_threshold":  MOMENTUM_THRESHOLD_PCT,
        "reversal_threshold":  REVERSAL_THRESHOLD_PCT,
    }

    if not active:
        # Mark any still-in-progress stocks as SKIPPED when window closes
        for sym, entry in data.get("tracker", {}).items():
            if entry.get("status") not in ("REVERSED", "SKIPPED"):
                entry["status"] = "SKIPPED"
        print(f"[{data['updated']}] Outside window — idle.")
        save(data)
        return

    elapsed  = mins_elapsed(now)
    now_str  = now.strftime("%H:%M")
    tracker  = data.get("tracker", {})
    scan_row = {"time": now_str, "elapsed_mins": elapsed, "results": []}

    print(f"\n[{data['updated']}] Scanning {len(WATCHLIST)} stocks — {elapsed} min into window")

    for symbol in WATCHLIST:
        candles = get_candles(symbol)
        tracker[symbol] = process_symbol(symbol, candles, tracker, now_str, elapsed)

        entry = tracker[symbol]
        scan_row["results"].append({
            "symbol":        symbol,
            "status":        entry.get("status", "WATCHING"),
            "price":         entry.get("latest_price"),
            "baseline":      entry.get("baseline_price"),
            "peak":          entry.get("peak_price"),
            "reversal":      entry.get("reversal_price"),
            "momentum_pct":  entry.get("momentum_pct"),
            "reversal_pct":  entry.get("reversal_pct"),
        })

    # Rebuild reversals list from tracker (single source of truth)
    data["reversals"] = []
    for sym, entry in tracker.items():
        if entry.get("status") == "REVERSED":
            data["reversals"].append({
                "symbol":          sym,
                "baseline_price":  entry["baseline_price"],
                "baseline_time":   entry["baseline_time"],
                "momentum_pct":    entry["momentum_pct"],
                "momentum_time":   entry["momentum_time"],
                "peak_price":      entry["peak_price"],
                "peak_time":       entry["peak_time"],
                "reversal_price":  entry["reversal_price"],
                "reversal_time":   entry["reversal_time"],
                "reversal_pct":    entry["reversal_pct"],
            })

    data["tracker"] = tracker
    data["scans"].append(scan_row)
    data["scans"] = data["scans"][-60:]   # keep last 60 scans

    save(data)
    print(f"  Done. {len(data['reversals'])} reversal(s) detected so far.")

if __name__ == "__main__":
    run()
