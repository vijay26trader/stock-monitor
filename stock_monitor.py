"""
Stock Momentum + First Reversal Detector
─────────────────────────────────────────
State machine per stock:
  WATCHING   → baseline set, waiting for 20%+ upward momentum
  MOMENTUM   → 20%+ move confirmed, tracking peak, waiting for reversal
  REVERSED   → price pulled back >= REVERSAL_THRESHOLD_PCT from peak → DONE

Uses Alpaca Markets API for 1-min pre-market data (4 AM ET onwards).
yfinance does NOT provide pre-market data — Alpaca is required.

Alpaca API keys must be set as GitHub Secrets:
  ALPACA_API_KEY
  ALPACA_SECRET_KEY
"""

import requests
import json
import os
import pytz
from datetime import datetime, timedelta

# ════════════════════════════════════════════════════════════════
# CONFIG  — edit these values
# ════════════════════════════════════════════════════════════════

# Watchlist is built dynamically at runtime by build_watchlist.py
# Filters: price $1–$20, avg volume >100K/day
# Override by setting WATCHLIST env var: "AAPL,TSLA,PCLA"
WATCHLIST = []   # populated in run()

# Monitoring window (ET) — overridden by WINDOW_START / WINDOW_END env vars
def _parse_hhmm(s, default_hour, default_minute):
    """Parse HH:MM string into (hour, minute). Falls back to defaults."""
    try:
        h, m = s.strip().split(":")
        return int(h), int(m)
    except Exception:
        return default_hour, default_minute

import os as _os
_ws = _os.environ.get("WINDOW_START", "04:00")
_we = _os.environ.get("WINDOW_END",   "05:00")

WINDOW_START_HOUR, WINDOW_START_MINUTE = _parse_hhmm(_ws, 4, 0)
WINDOW_END_HOUR,   WINDOW_END_MINUTE   = _parse_hhmm(_we, 5, 0)

# Momentum: upward move % from baseline to qualify
MOMENTUM_THRESHOLD_PCT = 20.0

# Reversal: pullback % from peak to trigger reversal signal
REVERSAL_THRESHOLD_PCT = 2.0

# Output (committed to repo, read by GitHub Pages)
OUTPUT_FILE = "docs/data/stocks.json"

# ════════════════════════════════════════════════════════════════
# ALPACA CONFIG
# ════════════════════════════════════════════════════════════════

ALPACA_API_KEY    = os.environ.get("ALPACA_API_KEY",    "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_BASE       = "https://data.alpaca.markets/v2"

def alpaca_headers():
    return {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }

# ════════════════════════════════════════════════════════════════
# TIMEZONE
# ════════════════════════════════════════════════════════════════

ET_TZ = pytz.timezone("America/New_York")  # auto-handles EDT/EST

def now_et():
    return datetime.now(ET_TZ)

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
    "updated":        None,
    "window_active":  False,
    "window_config":  {},
    "last_scan_date": None,
    "tracker":        {},
    "reversals":      [],
    "scans":          [],
}

def load():
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE) as f:
            return json.load(f)
    return dict(EMPTY_STATE)

def save(data):
    os.makedirs("docs/data", exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(data, f, indent=2)

# ════════════════════════════════════════════════════════════════
# MARKET DATA — Alpaca (supports pre-market from 4 AM ET)
# ════════════════════════════════════════════════════════════════

def get_candles(symbol):
    """
    Fetch today's 1-min bars from Alpaca for the configured window.
    Returns list of candle dicts with ET timestamps.
    """
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        print(f"  [{symbol}] ERROR: ALPACA_API_KEY / ALPACA_SECRET_KEY not set in GitHub Secrets")
        return []

    now    = now_et()
    today  = now.date()

    # Build UTC start/end covering the window for today
    start_et  = ET_TZ.localize(datetime(today.year, today.month, today.day,
                                        WINDOW_START_HOUR, WINDOW_START_MINUTE, 0))
    end_et    = ET_TZ.localize(datetime(today.year, today.month, today.day,
                                        WINDOW_END_HOUR, WINDOW_END_MINUTE, 0))

    start_utc = start_et.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_utc   = end_et.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    url  = f"{ALPACA_BASE}/stocks/{symbol}/bars"
    rows = []

    # Try SIP first (full tape, all stocks, pre-market), fall back to IEX
    for feed in ["sip", "iex"]:
        params = {
            "timeframe": "1Min",
            "start":     start_utc,
            "end":       end_utc,
            "feed":      feed,
            "limit":     200,
        }
        try:
            resp = requests.get(url, headers=alpaca_headers(), params=params, timeout=15)
        except Exception as e:
            print(f"  [{symbol}] request error: {e}")
            return []

        if resp.status_code == 403:
            print(f"  [{symbol}] Alpaca 403 — check API keys in GitHub Secrets")
            return []
        if resp.status_code == 422:
            continue   # ticker not on this feed, try next
        if resp.status_code != 200:
            print(f"  [{symbol}] Alpaca HTTP {resp.status_code}: {resp.text[:120]}")
            continue

        bars = resp.json().get("bars", []) or []
        for bar in bars:
            ts_utc = datetime.strptime(bar["t"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.utc)
            ts_et  = ts_utc.astimezone(ET_TZ)

            mins      = ts_et.hour * 60 + ts_et.minute
            win_start = WINDOW_START_HOUR * 60 + WINDOW_START_MINUTE
            win_end   = WINDOW_END_HOUR   * 60 + WINDOW_END_MINUTE
            if not (win_start <= mins <= win_end):
                continue

            rows.append({
                "time":   ts_et.strftime("%H:%M"),
                "open":   round(float(bar["o"]), 4),
                "high":   round(float(bar["h"]), 4),
                "low":    round(float(bar["l"]), 4),
                "close":  round(float(bar["c"]), 4),
                "volume": int(bar["v"]),
            })

        if rows:
            break   # got data, no need to try next feed

    return rows

# ════════════════════════════════════════════════════════════════
# STATE MACHINE
# ════════════════════════════════════════════════════════════════

def process_symbol(symbol, candles, tracker, now_str, elapsed):
    """
    Update the state machine for one symbol given fresh candles.
    Returns updated tracker entry dict.
    """
    entry  = tracker.get(symbol, {"status": "WATCHING"})
    status = entry.get("status", "WATCHING")

    # Terminal states — nothing more to do
    if status in ("REVERSED", "SKIPPED"):
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

    # ── WATCHING ────────────────────────────────────────────────
    if status == "WATCHING":
        if "baseline_price" not in entry:
            entry["baseline_price"] = latest["open"]   # use open of first candle, not close
            entry["baseline_time"]  = latest["time"]
            print(f"  [{symbol}] baseline set ${latest['open']} (open) @ {latest['time']}")

        baseline          = entry["baseline_price"]
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

    # ── MOMENTUM ────────────────────────────────────────────────
    elif status == "MOMENTUM":
        peak = entry.get("peak_price", price)

        if price > peak:
            entry["peak_price"] = price
            entry["peak_time"]  = latest["time"]
            peak = price
            print(f"  [{symbol}] new peak ${peak} @ {latest['time']}")

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
    global WATCHLIST
    now    = now_et()
    data   = load()
    active = in_window(now)
    today  = now.strftime("%Y-%m-%d")

    # Build or reload watchlist once per day
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    _watch_env = os.environ.get("WATCHLIST", "")
    _mode      = os.environ.get("WATCHLIST_MODE", "top_movers").strip().lower()

    if _watch_env:
        WATCHLIST = [s.strip().upper() for s in _watch_env.split(",") if s.strip()]
        print(f"Using env watchlist: {len(WATCHLIST)} stocks")
    elif not WATCHLIST or data.get("last_scan_date") != today:
        if _mode == "price_range":
            print("Building watchlist from price range ($1-$20, vol >100K)...")
            from build_watchlist import build
            WATCHLIST = build()
        else:
            print("Fetching top momentum stocks from Alpaca Screener...")
            from get_top_movers import build_top_movers_watchlist
            WATCHLIST = build_top_movers_watchlist()
        if not WATCHLIST:
            print("ERROR: Watchlist is empty — check filters or API keys")
            return

    # Reset on new day
    if data.get("last_scan_date") != today:
        data["tracker"]        = {}
        data["reversals"]      = []
        data["scans"]          = []
        data["last_scan_date"] = today
        print(f"New day {today} — state reset.")

    data["window_active"] = active
    data["updated"]       = now.strftime("%Y-%m-%d %H:%M:%S ET")
    data["window_config"] = {
        "start":              f"{WINDOW_START_HOUR:02d}:{WINDOW_START_MINUTE:02d} ET",
        "end":                f"{WINDOW_END_HOUR:02d}:{WINDOW_END_MINUTE:02d} ET",
        "momentum_threshold": MOMENTUM_THRESHOLD_PCT,
        "reversal_threshold": REVERSAL_THRESHOLD_PCT,
    }

    if not active:
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
            "symbol":       symbol,
            "status":       entry.get("status", "WATCHING"),
            "price":        entry.get("latest_price"),
            "baseline":     entry.get("baseline_price"),
            "peak":         entry.get("peak_price"),
            "reversal":     entry.get("reversal_price"),
            "momentum_pct": entry.get("momentum_pct"),
            "reversal_pct": entry.get("reversal_pct"),
        })

    # Rebuild reversals from tracker
    data["reversals"] = []
    for sym, entry in tracker.items():
        if entry.get("status") == "REVERSED":
            data["reversals"].append({
                "symbol":         sym,
                "baseline_price": entry["baseline_price"],
                "baseline_time":  entry["baseline_time"],
                "momentum_pct":   entry["momentum_pct"],
                "momentum_time":  entry["momentum_time"],
                "peak_price":     entry["peak_price"],
                "peak_time":      entry["peak_time"],
                "reversal_price": entry["reversal_price"],
                "reversal_time":  entry["reversal_time"],
                "reversal_pct":   entry["reversal_pct"],
            })

    data["tracker"] = tracker
    data["scans"].append(scan_row)
    data["scans"] = data["scans"][-60:]

    save(data)
    print(f"  Done. {len(data['reversals'])} reversal(s) detected so far.")

if __name__ == "__main__":
    run()
