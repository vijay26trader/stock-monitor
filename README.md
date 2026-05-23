# Stock Momentum Breakout Monitor

Detects stocks with 20%+ momentum moves within 5–10 minutes of a configurable time window. Runs every minute via GitHub Actions and displays results on a live GitHub Pages dashboard.

## How it works

1. At the start of the time window, a **baseline price** is recorded for each stock
2. Every minute, the script checks if any stock has moved **≥20% from its baseline** within 10 minutes
3. Matches are saved to `data/stocks.json` and committed back to the repo
4. The GitHub Pages dashboard reads this JSON and displays results live

## Setup

### 1. Fork / clone this repo

### 2. Enable GitHub Pages
Settings → Pages → Source: **Deploy from branch** → Branch: `main`, Folder: `/docs`

### 3. Enable Actions write permission
Settings → Actions → General → Workflow permissions → **Read and write permissions**

### 4. Configure your watchlist and time window
Edit `stock_monitor.py`:

```python
WATCHLIST = ["AAPL", "MSFT", "GOOGL", "TSLA", "AMZN", "NVDA", "META", "SPY", "QQQ", "AMD"]

WINDOW_START_HOUR   = 10   # 10:00 AM ET
WINDOW_START_MINUTE = 0
WINDOW_END_HOUR     = 11   # 11:00 AM ET
WINDOW_END_MINUTE   = 0

MOMENTUM_THRESHOLD_PCT = 20.0   # % move to detect
MOMENTUM_WINDOW_MINS   = 10     # within how many minutes
```

### 5. Test manually
Go to Actions → Stock Momentum Monitor → Run workflow

### 6. View your dashboard
`https://YOUR-USERNAME.github.io/stock-monitor/`

## Files

| File | Purpose |
|------|---------|
| `stock_monitor.py` | Main scanner script |
| `.github/workflows/monitor.yml` | GitHub Actions schedule |
| `data/stocks.json` | Output data (auto-updated) |
| `docs/index.html` | Live dashboard (GitHub Pages) |

## Notes

- GitHub Actions cron minimum is 1 minute
- The script checks if the current time is within your window — runs outside the window exit immediately
- Baselines reset at the start of each new trading day
- Once a stock matches, it won't match again the same session
- Uses Yahoo Finance via `yfinance` — free, no API key needed
- EDT = UTC-4 (summer), EST = UTC-5 (winter) — update `ET_OFFSET` in the script seasonally
