# pavano-trades-bot — Options Trading Bot

Automated options trading on Alpaca paper. One rule above all others:

> **Follow the money. Where smart money flows, we go.**

Forget analyst price targets. Forget technical levels. Forget "expert" opinions.
The only signal that matters is where real money is actually being placed right now.

---

## The Strategy: Flow First

**Smart money flow = vol/OI ratio.**

When a strike has volume >> open interest, new money is flooding in. Someone knows something, or is making a big directional bet. That's where we want to be — not some arbitrary 3% OTM level we calculated ourselves.

### How strike selection works

1. Signal generated for a ticker (bullish = call, bearish = put)
2. Pull the live options chain via yfinance for all expirations in our DTE window
3. Calculate vol/OI ratio for every strike — this tells us where money is actually moving
4. Pick the strike with the **highest flow concentration** (vol/OI ratio × meaningful volume)
5. Fallback: if no meaningful flow data, pick closest to ATM with good DTE
6. Price check: max $2.00/share ask — skip if too expensive

### What we ignore (intentionally)

- Analyst price targets — don't care where "experts" think the stock should go
- Technical levels, support/resistance — not our game
- Moving averages — signals.py stripped these out already
- Sentiment scores alone — only matters if flow confirms it

### What we trust

- **Vol/OI ratio > 2x with volume >= 50 contracts**: real signal
- **Macro tape** (SPY + QQQ direction): suppress calls in bearish tape, puts in bullish tape
- **Gap confirmation**: if stock gaps hard against the signal direction at open, skip
- **DTE**: 14-21 days is the sweet spot — enough time to be right, not too much theta bleed

---

## Architecture

```
signals.py   →  signals_output.json  →  bot.py (open/intraday/close)
                     +                         ↓
              yfinance flow data       Alpaca Options API
              (vol/OI ratios)                  ↓
                                       Telegram notifications
```

## Position Sizing ($500 capital)

- Max $100 per options position
- Max 3 positions open at once ($300 max deployed)
- $200 cash reserve always maintained
- Skip contracts > $2.00/share ask

## Stop / Target

- **Stop loss:** -50% of premium paid (hard floor)
- **Take profit:** +100% of premium paid (double up)
- **Time decay rule:** DTE ≤ 3 + P&L ≤ -30% = cut it, time value is gone
- **Thesis check:** deeply OTM with no price movement toward strike = close
- **Flow contradiction:** down > 15% AND smart money flow is $10+ away from our strike AND our strike vol/OI < 0.5x AND top flow strike has 3x+ ratio = cut early, don't wait for -50%. Flow is telling us we're wrong. Listen.

## Modes

```bash
python3 bot.py --mode open      # Execute trades at market open (9:45am ET entry)
python3 bot.py --mode intraday  # Check SL/TP on open positions
python3 bot.py --mode close     # EOD review — close or hold with reasoning
```

## Alpaca Options API

- Contracts: `GET /v2/options/contracts?underlying_symbols=AAPL&type=call&...`
- Symbol format: OCC standard — `AAPL260315C00220000`
- Live quotes: `GET https://data.alpaca.markets/v1beta1/options/snapshots?symbols=...&feed=indicative`
- Orders: `POST /v2/orders` with `{"symbol": "AAPL260315C00220000", "qty": "1", "side": "buy", "type": "market", "time_in_force": "day"}`
- Multiplier: 100 (1 contract = 100 shares exposure)

## Telegram Notifications

**On BUY:**
```
🟢 OPTIONS BUY — TSLA CALL
📋 Contract: TSLA260325C00400000
💵 1 contract @ $1.65/share ($165.00 total)
🎯 Strike: $400 | Exp: 2026-03-25 (8 DTE)
📊 Signal: 7.5/10 — options, news
📡 EOD flow — gap +0.8% confirms LONG
🛑 Stop: -50% ($0.83/sh) | 🎯 Target: +100% ($3.30/sh)
💵 Cash remaining: $308.00
```

**On SELL:**
```
✅ TARGET HIT — TSLA CALL
📋 TSLA260325C00400000
💵 Sold @ $3.35/share
📊 P&L: +$170.00 (+103.0%)
⏱ Held: 1h 42m
💵 Cash: $478.00 | Portfolio: $478.00
```

---

## Key Lessons (hard-won)

- **Analyst PTs are noise.** Flow is signal. We got burned holding TSLA $420C when all the real volume was at $387-$402.
- **BMNR $30C had 10x the flow of our $24C.** Smart money was right. We were in the wrong strike.
- **Don't fight the flow.** If vol/OI is dead at your strike, nobody believes in that level. Move.
- **3% OTM was arbitrary.** We're not smarter than the market. We just follow it.
