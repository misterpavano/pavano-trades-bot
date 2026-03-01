# pavano-trades-bot — Options Trading Bot

An automated options trading bot using Alpaca paper trading. Buys OTM call/put options based on signals from unusual options flow, news sentiment, and politician trades.

## Architecture

```
signals.py   →  signals_output.json  →  bot.py (open/monitor/close)
                                              ↓
                                      Alpaca Options API
                                              ↓
                                      Telegram notifications
```

## Options-Only Strategy

This bot trades **options contracts only** — no equity shares.

### Contract Selection Logic
1. Signal generated for a ticker (bullish = call, bearish = put)
2. Fetch options chain: 7-30 DTE, active, tradable
3. Target strike: 3% OTM (calls above spot, puts below spot)
4. Select contract closest to target strike + 14-21 DTE sweet spot
5. Price check: max $2.00/share ask ($200/contract) — skip if too expensive
6. Buy 1-2 contracts depending on budget

### Position Sizing ($500 capital)
- Max $100 per options position
- Max 3 positions open at once ($300 max deployed)
- $200 cash reserve always maintained
- Skip contracts > $2.00/share ask

### Stop / Target
- **Stop loss:** -50% of premium paid (options move fast)
- **Take profit:** +100% of premium paid (double up)
- **EOD:** close all positions regardless

## Modes

```bash
python3 bot.py --mode open     # Execute options trades (run at market open)
python3 bot.py --mode monitor  # Check SL/TP on open positions (run every 30min)
python3 bot.py --mode close    # Close all positions EOD (run at 3:45 PM)
```

## Setup

```bash
pip install alpaca-py requests
```

Requires Alpaca paper account with **options trading enabled (Level 2+)**.  
Current account: **Level 3** ✅

## Alpaca Options API Notes

- Contracts endpoint: `GET /v2/options/contracts?underlying_symbols=AAPL&type=call&expiration_date_gte=...`
- Symbol format: OCC standard — `AAPL260315C00220000` (AAPL, Mar 15 2026, Call, $220 strike)
- Live quotes: `GET https://data.alpaca.markets/v1beta1/options/snapshots?symbols=...&feed=indicative`
- Orders: same as equities — `POST /v2/orders` with `{"symbol": "AAPL260315C00220000", "qty": "1", "side": "buy", "type": "market", "time_in_force": "day"}`
- Multiplier: 100 (1 contract = 100 shares exposure)

### Enabling Options on Alpaca Paper Account
Account must have options_trading_level ≥ 2. To enable:
1. Log in to alpaca.markets → Paper Trading dashboard
2. Account → Trading → Options Trading
3. Complete options agreement and select level
4. Paper account reflects same level as live account settings

## Telegram Notifications

**On BUY:**
```
🟢 OPTIONS BUY — AAPL CALL
📋 Contract: AAPL260315C00220000
💵 1 contract @ $1.45/share ($145.00 total)
🎯 Strike: $220 | Exp: 2026-03-15 (14 DTE)
📊 Signal: 7.5/10 — options, news
🛑 Stop: -50% ($0.73/sh) | 🎯 Target: +100% ($2.90/sh)
💵 Cash remaining: $355.00
```

**On SELL:**
```
✅ OPTIONS SOLD — AAPL CALL [TARGET HIT]
📋 AAPL260315C00220000
💵 Sold @ $2.95/share
📊 P&L: +$150.00 (+103.4%)
⏱ Held: 2h 15m
💵 Cash: $505.00 | Portfolio: $505.00
```
