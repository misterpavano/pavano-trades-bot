# pavano-trades-bot 🤖📈

A disciplined, self-learning paper trading bot that follows institutional momentum signals — options flow, news catalysts, and congressional stock disclosures.

**Hard cap: $500 total capital. Paper trading only.**

---

## What This Bot Does

Each trading day, the bot:
1. **Pre-market (8:45 AM):** Scans congressional disclosures for recent large stock purchases
2. **Pre-market (9:00 AM):** Scans options flow and news for all watchlist tickers, scores signals 0-10
3. **Market open (9:35 AM):** Executes trades on highest-scoring signals (score ≥ 5)
4. **EOD (3:55 PM):** Closes all positions, generates a Telegram report, runs the learning system

---

## Signal Stack

### 1. Options Flow (0–6 pts)
Looks for unusual OTM call/put volume vs open interest across near-term expirations. Big money moving options before a move is one of the most reliable signals in retail.

### 2. News Catalyst (0–2 pts)
Pulls headlines from SearXNG (local multi-engine search). Scores bullish/bearish keyword density. Confirms or cancels options signal direction.

### 3. Congressional Trades (0–3 pts)
Tracks House & Senate stock purchase disclosures via:
- House Stock Watcher API
- Senate Stock Watcher API

Scores by recency (newer = higher), amount ($50k-$100k = 1pt, $100k-$250k = 2pt, $250k+ = 3pt), and multi-politician consensus (bonus pts).

**Final score = options + news + politician (max 10)**

A ticker needs score ≥ 5 to be tradeable.

---

## Knowledge Base

```
knowledge/
  tickers/          # Per-ticker trade logs (TICKER.md)
  signals/          # Performance data, win rates, daily learnings
  politicians/      # Congressional buy data (latest.json, history.json)
  sectors/          # (future) sector rotation notes
  playbooks/        # (future) strategy notes
```

---

## How It Learns

After each EOD close, `learn.py` runs automatically:
- Records each closed trade's outcome in `knowledge/signals/performance.json`
- Updates each ticker's trade log in `knowledge/tickers/TICKER.md`
- Tracks win rate by signal type in `knowledge/signals/win_rate.json`
- Writes human-readable notes to `knowledge/signals/YYYY-MM-DD-learnings.md`

Over time this tells us: **which signals actually make money.**

---

## Cron Schedule (ET, Weekdays)

| Time | Job | Description |
|------|-----|-------------|
| 8:45 AM | Politician Trade Scan | Fetch congressional buys |
| 9:00 AM | Pre-Market Scan | Options + news signal scan |
| 9:35 AM | Market Open | Execute trades |
| 3:55 PM | EOD Close + Report | Close positions, report, learn |

---

## Philosophy

**Follow the big money. Stay humble. Protect the capital.**

- Congress members outperform the market consistently — their disclosures are public and legal to trade on
- Options flow shows where institutions are positioning before a move
- News confirms or cancels — never trade news alone
- $500 cap is sacred. Never risk more than you can lose without blinking.
- The bot learns from every trade. Give it time.

---

## Setup

```bash
pip install alpaca-trade-api alpaca-py yfinance requests numpy
python3 politicians.py   # Test congressional data fetch
python3 signals.py       # Test signal scan
python3 bot.py --mode open    # Execute trades
python3 bot.py --mode close   # Close positions + learn
python3 eod_report.py         # Send EOD Telegram report
```

## Config

Set in `config.py`:
- `ALPACA_KEY` / `ALPACA_SECRET` — Paper trading API creds
- `STARTING_CAPITAL = 500` — Hard cap
- `MAX_POSITIONS = 3` — Max concurrent trades
- `MIN_SIGNAL_SCORE = 5` — Minimum score to trade

---

*Built by Pavano & Wally. Humble, slow growth. Follow the big money.*
