# Options Trading Reference

> This file is read by the trading bot before signal evaluation.
> Sources: quant-trading (je-suis-tm), volatility-trading (jasonstrimpel),
>          Options-Trading-Strategies-in-Python (PyPatel), optopsy (michaelchu)

---

## The Greeks — What They Mean for Flow Trading

**Delta (0 to ±1)**
Measures how much the option price moves per $1 move in the underlying.
- A delta of 0.30 means the option gains $0.30 for every $1 the stock rises.
- For directional flow plays: look for 0.25–0.45 delta (OTM but not lottery tickets).
- High delta (>0.60) = deep ITM, expensive, lower leverage.
- Watch: if you see huge sweeps on 0.20-delta calls, big money is betting on a quick move.

**Gamma**
Rate of change of delta. Highest near ATM and near expiration.
- High gamma = your delta can shift fast. Good if you're right, brutal if you're wrong.
- Short-dated ATM options have explosive gamma — why 7-14 DTE plays move so fast.
- Optopsy's per-leg delta targeting lets you select entries by delta range (target, min, max) — use this to avoid inadvertently buying high-gamma lotto tickets.

**Theta (time decay)**
Options lose value every day. For buyers: theta is your enemy.
- At-the-money options decay fastest in the final 30 days. This is why we cap DTE_MAX at 30.
- A 14-21 DTE option loses ~$0.05-0.10/day in premium on a $2 option — your position must move to overcome this.
- quant-trading's straddle backtest explicitly chose entry timing based on event proximity to minimize theta bleed before the catalyst hits.

**Vega**
Sensitivity to implied volatility changes. Every 1-point IV move = vega dollars gained/lost.
- If you buy options when IV is elevated (e.g., before earnings), vega works against you after the event even if you're directionally right.
- High IV = expensive options. The volatility-trading repo (jasonstrimpel) implements 8 historical volatility estimators (Garman-Klass, Yang-Zhang, Parkinson, Rogers-Satchell, etc.) to compare realized vol against implied vol — when IV >> HV, options are overpriced.

**Rho**
Sensitivity to interest rate changes. Mostly irrelevant for short-dated options (<30 DTE).
- Don't trade around it. Not a factor in our flow-based system.

---

## How to Read Options Flow

**Sweep vs Block**
- Sweep: A large order broken into smaller pieces and executed across multiple exchanges simultaneously to fill fast. Signals urgency — someone needs this position NOW. Strong directional conviction.
- Block: A single large negotiated trade executed off-exchange (dark pool). Often a hedge rather than a directional bet. Less urgent, sometimes a contra-indicator.
- Our SWEEP_VOL_MULTIPLIER = 5.0 — when volume exceeds 5x open interest, treat as sweep signal. This is the single strongest options signal in our scoring system (adds +2 to base score).

**OTM Calls with High Volume**
- When OTM calls (1-10% above current price) see volume 2x+ the open interest, big money is making a directional bet, not hedging.
- Near-OTM threshold: NEAR_OTM_MAX_PCT = 0.10 (within 10% of price). Beyond 20% OTM = noise per MAX_OTM_PCT = 0.20.
- The deeper OTM the strike, the more explosive the move required — but also the bigger the payoff if right.

**Put/Call Ratio (PCR)**
- PCR > 1.0: More puts than calls. Bearish signal or heavy hedging.
- PCR < 0.7: Call-heavy. Bullish sentiment or complacency (can be contrarian warning).
- PyPatel's PCR strategy uses Bollinger Bands on rolling PCR: when PCR crosses the upper BB -> buy signal (market over-hedged, likely to bounce). When PCR crosses lower BB -> sell signal.
- For our bot: extreme PCR readings reinforce or contradict our options flow score.

**IV Spike Before Earnings**
- IV inflates as earnings approach — market makers widen spreads to account for unknown risk.
- Buying calls/puts right before earnings = paying a massive IV premium.
- After the number drops, IV collapses 30-60% in minutes regardless of direction. This is IV crush.
- quant-trading's straddle backtest found that long straddles only work if the actual move exceeds the combined premium paid for both legs. Most earnings moves don't clear this bar.

---

## Volatility

**IV vs HV: What the Spread Tells You**
The volatility-trading repo implements 8 HV estimators. Key insight:
- Yang-Zhang estimator: Best all-around HV measure, accounts for overnight gaps and intraday range.
- Parkinson estimator: Uses high-low range only, good for liquid stocks.
- When IV > HV significantly (IV/HV ratio > 1.3): options are expensive. You're paying for fear. Avoid buying options.
- When IV < HV (IV/HV ratio < 0.8): options are cheap relative to actual movement. Good time to buy.
- The repo's cones() method plots volatility across windows [30, 60, 90, 120 days] to see if current IV is at historical extremes.

**When High IV = Avoid Buying**
- Earnings week: IV rank (IVR) typically spikes to 60-90th percentile. You're overpaying.
- Macro event days (Fed, CPI): same problem.
- Optopsy's IV Rank signal filter (part of its 80+ entry signals) specifically filters out entries when IVR is above threshold.
- Rule: If a stock's IV is in the top 30% of its 52-week range, add 1 to the required minimum score before trading.

**VIX and SPY Options Pricing**
- VIX is the 30-day implied volatility of SPX options (CBOE white paper formula, verified in quant-trading's VIX Calculator.py).
- VIX 15-20 = normal. VIX 20-30 = elevated fear. VIX 30+ = crisis pricing.
- PyPatel's VIX Strategy: buys S&P 500 futures when VIX >= 22 (fear extreme), exits at +5% or -5% with absolute stop loss of $25.
- For our options bot: VIX > 25 means SPY options are expensive. On individual names, watch their own IV rank, not just VIX.
- VIX and SPY move inversely ~80% of the time. Rising VIX + falling SPY = good time to buy SPY puts if not already in.

---

## Strike & Expiry Selection

**Why 14-21 DTE is the Sweet Spot for Directional Plays**
- Enough time for the move to play out without paying for excessive time value.
- Our dte_score_multiplier() in signals.py weights 14-DTE at 1.3x and 21-DTE at 1.15x — shorter-dated flow shows more urgency.
- Optopsy's backtesting confirms 45-DTE works for income strategies (iron condors) but 14-21 DTE optimizes for directional plays.
- At 7 DTE or less: high gamma is exciting but theta decay is brutal. Reserve for very high-conviction plays only.

**OTM vs ATM: Risk/Reward Tradeoff**
- ATM (delta ~0.50): Costs more, moves with the stock, less leverage. Better if uncertain about timing.
- OTM (delta 0.25-0.40): Cheaper premium, explosive if right, total loss if wrong. Requires strong signal.
- quant-trading's straddle backtest emphasizes finding strikes where call and put prices are nearly equal — avoiding asymmetric pricing that signals market bias.
- Our OTM_PCT = 0.03: target strikes 3% OTM. This is in the sweet zone of near-OTM without being lottery tickets.

**How to Pick a Strike Based on Signal Strength**
- Score 8-10 (very high conviction): Go 3-5% OTM, 14-21 DTE. Maximum leverage.
- Score 6-7 (high conviction): ATM or 1-3% OTM, 21-30 DTE. Balanced.
- Score 5 (minimum threshold): ATM only, 21-30 DTE. Closer to the money reduces risk of being wrong.
- Never go >10% OTM unless a sweep with extraordinary volume confirms the target.

---

## Common Traps to Avoid

**IV Crush After Earnings**
The most common options trading mistake. Even if the stock moves in your direction, if IV collapses more than the stock gains, your option loses value.
- quant-trading straddle backtest: "do not arrogantly believe you outsmart the rest of the players — all information may already be priced in."
- Solution: Never buy options within 5 days of earnings unless it's an intentional earnings play AND you've confirmed the IV/HV spread makes it worthwhile.

**Buying Options on Low-Volume Tickers**
- Wide bid-ask spreads = instant loss on entry. A $0.10 option with a $0.05 spread has 50% immediate slippage.
- Optopsy explicitly implements _remove_min_bid_ask() to filter options where bid or ask fall below minimum thresholds.
- Our MIN_OPTION_VOLUME = 250: Only trade strikes with at least 250 contracts of volume.

**Chasing a Move That Already Happened**
- If NVDA already ran 5% on the news, the options have repriced. You're buying the top.
- signals.py MA trend filter (get_ma_trend()): Only score bullish options on MA5 > MA10 > MA20 uptrend — confirms trend is intact, not extended.
- From quant-trading: "finding a good pair at the right price is tough — if conditions aren't met, don't trade."

**Not Sizing Correctly on High vs Low Conviction Signals**
- Our config enforces hard limits: MAX_POSITION_COST = 150, MAX_POSITIONS = 3, CASH_RESERVE = 200.
- Optopsy's simulate() function tracks capital, position limits, and equity curves — the lesson: position sizing consistency matters more than any individual trade.
- High conviction (score 9-10): Full $150 position is fine.
- Lower conviction (score 5-6): Consider half-sizing ($75-100 max). Nothing forces you to max out every position.

---

## Politician Trade Signals

**Why They Matter (Information Asymmetry)**
- Politicians and their family members trading on knowledge from committee briefings is legal under current law. The STOCK Act requires disclosure but doesn't prohibit the trades.
- Empirical studies show congressional trading outperforms the market by 5-10% annually — suggesting informational advantage, particularly from committee members.
- Our bot weighs these signals (0-3 points) alongside options flow and news.

**How to Interpret Timing (45-Day Disclosure Lag)**
- Under the STOCK Act, politicians have up to 45 days to disclose trades. A disclosed trade could have been made 6 weeks ago.
- However: if the disclosure comes during or after a relevant news cycle, the position may still be actionable if the thesis is intact.
- Fresh disclosures (0-10 days old) are stronger signals than stale ones (30-45 days old).
- Our politicians.py weights by recency — factor this into how much score the politician signal adds.

**Which Committees to Weight Higher**
- Armed Services / Intelligence: Defense contractors (LMT, RTX, NOC), semiconductors (NVDA for AI/defense crossover), cybersecurity.
- Finance / Banking: Financials (JPM, GS), fintech, crypto policy plays.
- Health / HELP: Pharma (PFE, MRK, ABBV), biotech, healthcare REITs.
- Energy / Environment: Oil majors (XOM, CVX), clean energy, utilities.
- Score +3 when the committee has direct jurisdiction over the ticker's primary business. Score +1 for indirect exposure. Score +0 for no relevant jurisdiction.

---

## Signal Scoring Guide (Our System)

### Options Flow Score: 0-6 points

| Points | Condition |
|--------|-----------|
| +0 | Volume < 250 contracts OR premium < $50K aggregate |
| +1 | Volume 250-499, premium $50K-$100K, no sweep signal |
| +2 | Volume 500+, decent premium, moderate OI ratio |
| +3 | Near-OTM (within 10%), volume 2x+ OI, premium > $100K |
| +4 | Near-OTM + sweep signal (vol > 5x OI) |
| +5 | Near-OTM + sweep + short DTE (<=14 days, urgency premium) |
| +6 | Near-OTM + sweep + short DTE + bullish MA alignment (MA5 > MA10 > MA20) |

DTE multiplier (dte_score_multiplier() in signals.py):
- <=7 DTE: 1.4x | <=14 DTE: 1.3x | <=21 DTE: 1.15x | <=30 DTE: 1.0x | >30 DTE: 0.85x

### News Score: 0-2 points

| Points | Condition |
|--------|-----------|
| +0 | No relevant news OR news is stale (>48h) |
| +1 | Relevant news within 24-48h (earnings beat, partnership, FDA decision) |
| +2 | Breaking news within 24h, high relevance to ticker's core business |

### Politician Score: 0-3 points

| Points | Condition |
|--------|-----------|
| +0 | No politician trades, or committee has zero jurisdiction |
| +1 | Politician trade exists, indirect committee relevance |
| +2 | Multiple politicians OR direct committee jurisdiction |
| +3 | Multiple politicians + direct committee + fresh disclosure (<10 days) |

### Total Score Interpretation

| Score | Conviction | Action |
|-------|------------|--------|
| 5 | Minimum threshold | Trade but size conservatively ($75-100 max) |
| 6-7 | Moderate | Standard position ($100-150), ATM or slight OTM |
| 8-9 | High | Full position ($150), OTM strike, 14-21 DTE |
| 10 | Exceptional | Max position, consider 2 contracts if liquidity allows |

MIN_SIGNAL_SCORE = 5 (configured in config.py). Do not lower this threshold — the noise below 5 produces losing trades.

---

## Quick-Reference: Before Every Trade

1. Is IV elevated vs HV? If IV/HV > 1.3 — consider passing or sizing down.
2. Is there an earnings event within 5 days? If yes — avoid unless intentional earnings play.
3. Is the flow a sweep (vol > 5x OI) or just elevated volume? Sweep = stronger signal.
4. What's the MA trend? Only go long calls on bullish alignment (MA5 > MA10 > MA20).
5. Is the strike within 10% OTM? If >20% OTM — skip unless score is 9-10.
6. DTE 7-30? Under 7 is too hot to touch unless conviction is maximum.
7. Does this position respect CASH_RESERVE=$200 and MAX_POSITIONS=3?

If all 7 boxes check out and score >= 5 — execute.
