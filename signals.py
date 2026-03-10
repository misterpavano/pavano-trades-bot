#!/usr/bin/env python3
"""
signals.py — Options flow + news + politician signal scanner

Reference: knowledge/education/REFERENCE.md — consult before adjusting thresholds
Final score = options_score (0-6) + news_score (0-2) + politician_score (0-3) — max 10

Options scoring improvements (from community research):
  - DTE-based weight: shorter DTE = stronger signal (urgency premium)
  - OTM distance filter: deep OTM (>20%) = noise, near-OTM (1-10%) = high signal
  - Min premium threshold: aggregate call premium must be meaningful
  - Sweep detection: vol >> OI with large total volume = sweep signal
  - MA trend filter: REMOVED — flow is the signal, TA is noise
"""

import json
import time
import logging
import requests
import yfinance as yf
from datetime import datetime, timedelta, date
import numpy as np
import math

def safe_int(val, default=0):
    """Safe int conversion that handles NaN/None from yfinance options data."""
    try:
        import math
        if val is None or (isinstance(val, float) and math.isnan(val)):
            return default
        return int(val)
    except Exception:
        return default


def safe_float(val, default=0.0):
    """Safe float conversion."""
    try:
        import math
        if val is None or (isinstance(val, float) and math.isnan(val)):
            return default
        return float(val)
    except Exception:
        return default

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from config import (
    WATCHLIST, SEARXNG_URL, SIGNALS_FILE, SIGNALS_EOD_FILE,
    UNUSUAL_VOLUME_MULTIPLIER, MIN_EXPIRY_DAYS, MAX_EXPIRY_DAYS,
    MIN_SIGNAL_SCORE
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
POLITICIANS_LATEST = os.path.join(BASE_DIR, "knowledge", "politicians", "latest.json")

# ── Options signal thresholds ────────────────────────────────────────────────
MIN_OPTION_VOLUME = 500          # Raised from 250 — tighter noise filter
MIN_AGGREGATE_PREMIUM = 200_000  # Raised from $50K — $200K+ signals institutional intent
MAX_OTM_PCT = 0.20               # Ignore options more than 20% OTM (lottery tickets)
NEAR_OTM_MAX_PCT = 0.10          # Near-OTM: within 10% of current price
SWEEP_VOL_MULTIPLIER = 5.0       # Vol > 5x OI = likely sweep (aggressive buyer)


def load_politician_scores():
    """Load politician scores from the latest politician scan."""
    scores = {}
    if not os.path.exists(POLITICIANS_LATEST):
        log.info("No politician data found (run politicians.py first)")
        return scores
    try:
        with open(POLITICIANS_LATEST) as f:
            data = json.load(f)
        for sig in data.get("signals", []):
            scores[sig["ticker"]] = {
                "score": min(3, sig["score"]),
                "politicians": sig["politicians"],
                "transaction_count": sig.get("transaction_count", 0)
            }
        log.info(f"Loaded politician signals for {len(scores)} tickers")
    except Exception as e:
        log.warning(f"Could not load politician data: {e}")
    return scores


def dte_score_multiplier(days_to_expiry):
    """
    DTE-based scoring weight (inspired by stock_option_strategy).
    Shorter DTE = more aggressive/urgent bet = stronger signal.
    """
    if days_to_expiry <= 7:
        return 1.4   # Extremely urgent
    elif days_to_expiry <= 14:
        return 1.3
    elif days_to_expiry <= 21:
        return 1.15
    elif days_to_expiry <= 30:
        return 1.0
    else:
        return 0.85  # Far-dated options = weaker signal


def otm_quality_score(strike, current_price, is_call):
    """
    Score the OTM quality of an option.
    Near-OTM (1-10% OTM) = highest quality signal.
    Deep OTM (>20%) = noise, skip.
    Returns: score multiplier (0 = skip, 0.5-1.5 = use)
    """
    if current_price <= 0:
        return 1.0
    if is_call:
        otm_pct = (strike - current_price) / current_price
    else:
        otm_pct = (current_price - strike) / current_price

    if otm_pct < 0:
        return 0.7   # ITM — less signal value for flow detection
    elif otm_pct > MAX_OTM_PCT:
        return 0.0   # Deep OTM = lottery ticket, skip
    elif otm_pct <= 0.05:
        return 1.5   # Near-the-money = highest conviction
    elif otm_pct <= NEAR_OTM_MAX_PCT:
        return 1.2   # Moderate OTM = good signal
    else:
        return 0.8   # Further OTM = weaker signal


def is_sweep(vol, oi):
    """
    Sweep detection: volume >> open interest means aggressive/directional buyer.
    A sweep fills across multiple exchanges, leaving a large vol/OI footprint.
    """
    if oi == 0:
        return vol > 1000  # No OI means fresh position — high vol = likely sweep
    return vol > oi * SWEEP_VOL_MULTIPLIER


def get_options_signal(ticker: str) -> dict:
    """Pull options chain and score unusual flow with improved logic."""
    try:
        tk = yf.Ticker(ticker)
        expirations = tk.options
        if not expirations:
            return {"ticker": ticker, "options_score": 0, "direction": None, "detail": "no options data"}

        today = date.today()
        valid_expiries = []
        for exp in expirations:
            exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
            days_out = (exp_date - today).days
            if MIN_EXPIRY_DAYS <= days_out <= MAX_EXPIRY_DAYS:
                valid_expiries.append((exp, days_out))

        if not valid_expiries:
            return {"ticker": ticker, "options_score": 0, "direction": None, "detail": "no near-term expiry"}

        current_price = None
        try:
            info = tk.fast_info
            current_price = info.last_price
        except Exception:
            pass

        total_call_score = 0.0
        total_put_score = 0.0
        total_call_premium = 0
        total_put_premium = 0
        sweep_calls = 0
        sweep_puts = 0
        unusual_details = []

        for exp, days_out in valid_expiries[:3]:
            dte_mult = dte_score_multiplier(days_out)
            try:
                chain = tk.option_chain(exp)
                calls = chain.calls
                puts = chain.puts

                # ── Score calls ──────────────────────────────────────────────
                for _, row in calls.iterrows():
                    vol = safe_int(row.get("volume"))
                    oi = safe_int(row.get("openInterest"))
                    ask = safe_float(row.get("ask", 0))
                    strike = safe_float(row.get("strike", 0))

                    if vol < MIN_OPTION_VOLUME:
                        continue

                    # OTM quality filter
                    if current_price:
                        otm_mult = otm_quality_score(strike, current_price, is_call=True)
                        if otm_mult == 0:
                            continue  # Skip deep OTM lottery tickets
                    else:
                        otm_mult = 1.0

                    # Volume vs OI check
                    unusual = (oi > 0 and vol > oi * UNUSUAL_VOLUME_MULTIPLIER) or \
                              (oi == 0 and vol > 500)
                    if not unusual:
                        continue

                    # Aggregate premium (vol * ask * 100 = notional value)
                    notional = vol * ask * 100
                    total_call_premium += notional

                    # Base score: 1 + OTM quality + DTE weight
                    row_score = 1.0 * otm_mult * dte_mult

                    # Sweep bonus
                    if is_sweep(vol, oi):
                        sweep_calls += 1
                        row_score *= 1.3
                        unusual_details.append(f"SWEEP CALL {strike} exp={exp} vol={vol} oi={oi}")
                    else:
                        unusual_details.append(f"CALL {strike} exp={exp} vol={vol} oi={oi}")

                    total_call_score += row_score

                # ── Score puts ───────────────────────────────────────────────
                for _, row in puts.iterrows():
                    vol = safe_int(row.get("volume"))
                    oi = safe_int(row.get("openInterest"))
                    ask = safe_float(row.get("ask", 0))
                    strike = safe_float(row.get("strike", 0))

                    if vol < MIN_OPTION_VOLUME:
                        continue

                    if current_price:
                        otm_mult = otm_quality_score(strike, current_price, is_call=False)
                        if otm_mult == 0:
                            continue
                    else:
                        otm_mult = 1.0

                    unusual = (oi > 0 and vol > oi * UNUSUAL_VOLUME_MULTIPLIER) or \
                              (oi == 0 and vol > 500)
                    if not unusual:
                        continue

                    notional = vol * ask * 100
                    total_put_premium += notional

                    row_score = 1.0 * otm_mult * dte_mult
                    if is_sweep(vol, oi):
                        sweep_puts += 1
                        row_score *= 1.3
                        unusual_details.append(f"SWEEP PUT {strike} exp={exp} vol={vol} oi={oi}")
                    else:
                        unusual_details.append(f"PUT {strike} exp={exp} vol={vol} oi={oi}")

                    total_put_score += row_score

            except Exception as e:
                log.warning(f"{ticker} chain error for {exp}: {e}")
                continue

        # ── Compute final options score ─────────────────────────────────────
        options_score = 0
        direction = None

        net_call_score = total_call_score
        net_put_score = total_put_score

        # Minimum premium check — filter out low-notional noise
        if net_call_score > net_put_score and total_call_premium >= MIN_AGGREGATE_PREMIUM:
            direction = "LONG"
            base_score = 2 + min(3, net_call_score)
            sweep_bonus = min(1, sweep_calls * 0.5)
            options_score = min(6, int(base_score + sweep_bonus))
        elif net_put_score > net_call_score and total_put_premium >= MIN_AGGREGATE_PREMIUM:
            direction = "SHORT"
            base_score = 2 + min(3, net_put_score)
            sweep_bonus = min(1, sweep_puts * 0.5)
            options_score = min(6, int(base_score + sweep_bonus))

        # ── Put/call premium ratio — standalone conviction factor ────────────
        conviction_flag = None
        if total_put_premium / (total_call_premium + 1) > 10:
            options_score = min(6, options_score + 2)
            direction = "SHORT"
            conviction_flag = "HIGH_CONVICTION_SHORT"
            log.info(f"{ticker}: HIGH_CONVICTION_SHORT — put premium {total_put_premium:.0f} >> call premium {total_call_premium:.0f}")
        elif total_call_premium / (total_put_premium + 1) > 10:
            options_score = min(6, options_score + 2)
            conviction_flag = "HIGH_CONVICTION_LONG"
            log.info(f"{ticker}: HIGH_CONVICTION_LONG — call premium {total_call_premium:.0f} >> put premium {total_put_premium:.0f}")

        return {
            "ticker": ticker,
            "options_score": options_score,
            "direction": direction,
            "conviction_flag": conviction_flag,
            "call_score": round(net_call_score, 2),
            "put_score": round(net_put_score, 2),
            "call_premium_est": int(total_call_premium),
            "put_premium_est": int(total_put_premium),
            "sweep_calls": sweep_calls,
            "sweep_puts": sweep_puts,
            "current_price": current_price,
            "detail": "; ".join(unusual_details[:3]) if unusual_details else "no unusual flow"
        }

    except Exception as e:
        log.error(f"Error scanning {ticker}: {e}")
        return {"ticker": ticker, "options_score": 0, "direction": None, "detail": str(e)}


def get_news_score(ticker: str):
    """Fetch news from SearXNG and score catalyst strength."""
    try:
        # Known mappings for better search quality — falls back to "{TICKER} stock"
        company_names = {
            "SPY": "S&P 500 ETF market",
            "QQQ": "Nasdaq QQQ ETF market",
            "IWM": "Russell 2000 ETF market",
            "AAPL": "Apple stock",
            "TSLA": "Tesla stock",
            "NVDA": "Nvidia stock",
            "AMD": "AMD stock",
            "MSFT": "Microsoft stock",
            "META": "Meta stock",
            "GME": "GameStop stock",
            "AMZN": "Amazon stock",
            "GOOG": "Google Alphabet stock",
            "GOOGL": "Google Alphabet stock",
            "NFLX": "Netflix stock",
            "BABA": "Alibaba stock",
            "COIN": "Coinbase stock",
            "PLTR": "Palantir stock",
            "ARM": "ARM Holdings stock",
        }
        query = company_names.get(ticker, f"{ticker} stock")
        url = f"{SEARXNG_URL}?q={requests.utils.quote(query)}&format=json&time_range=day&categories=news"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        results = data.get("results", [])

        if not results:
            return 0, "no news", None

        bullish_kw = ["surge", "beat", "rally", "upgrade", "buy", "bullish", "record", "gain", "profit", "strong"]
        bearish_kw = ["crash", "drop", "miss", "downgrade", "sell", "bearish", "loss", "weak", "decline", "cut"]

        bull_hits = 0
        bear_hits = 0
        headlines = []

        for r in results[:10]:
            title = (r.get("title", "") + " " + r.get("content", "")).lower()
            headlines.append(r.get("title", ""))
            for kw in bullish_kw:
                if kw in title:
                    bull_hits += 1
            for kw in bearish_kw:
                if kw in title:
                    bear_hits += 1

        news_score = 0
        news_direction = None
        top_headline = headlines[0] if headlines else "no headline"

        if bull_hits > bear_hits and len(results) >= 3:
            news_score = 1  # capped at 1 — tie-breaker only, not a primary signal
            news_direction = "LONG"
        elif bear_hits > bull_hits and len(results) >= 3:
            news_score = 1  # capped at 1 — tie-breaker only
            news_direction = "SHORT"
        # neutral/mixed news = 0 (removed noise point)

        return news_score, top_headline, news_direction

    except Exception as e:
        log.warning(f"News fetch error for {ticker}: {e}")
        return 0, "news error", None



def get_earnings_data(ticker: str) -> dict:
    """
    Check upcoming earnings date and historical surprise pattern.

    Returns:
      days_to_earnings: int or None
      earnings_risk: "DANGER" (<3 days) | "CAUTION" (3-7 days) | "CLEAR" | "UNKNOWN"
      earnings_surprise_score: 0-2 (bonus for consistent beat history)
      earnings_note: human-readable summary
    """
    try:
        tk = yf.Ticker(ticker)

        # ── Next earnings date ───────────────────────────────────────────────
        days_to_earnings = None
        earnings_risk = "UNKNOWN"
        earnings_note = ""

        try:
            cal = tk.calendar
            if cal is not None:
                # yfinance returns a dict: {"Earnings Date": [date, ...], ...}
                raw = None
                if isinstance(cal, dict):
                    earn_list = cal.get("Earnings Date")
                    if earn_list:
                        raw = earn_list[0] if isinstance(earn_list, list) else earn_list
                elif hasattr(cal, "loc") and "Earnings Date" in cal.index:
                    raw = cal.loc["Earnings Date"].iloc[0]
                elif hasattr(cal, "columns") and "Earnings Date" in cal.columns:
                    raw = cal["Earnings Date"].iloc[0]

                if raw is not None:
                    if isinstance(raw, date) and not isinstance(raw, datetime):
                        earn_date = raw
                    elif hasattr(raw, "date"):
                        earn_date = raw.date()
                    else:
                        earn_date = datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
                    days_to_earnings = (earn_date - date.today()).days
                    if days_to_earnings < 0:
                        days_to_earnings = None  # Already passed
        except Exception as e:
            log.debug(f"{ticker} calendar error: {e}")

        if days_to_earnings is not None:
            if days_to_earnings <= 2:
                earnings_risk = "DANGER"
                earnings_note = f"⚠️ EARNINGS IN {days_to_earnings}d — IV crush risk, skip"
            elif days_to_earnings <= 7:
                earnings_risk = "CAUTION"
                earnings_note = f"⚡ Earnings in {days_to_earnings}d — elevated IV, use caution"
            else:
                earnings_risk = "CLEAR"
                earnings_note = f"Earnings in {days_to_earnings}d"
        else:
            earnings_risk = "UNKNOWN"

        # ── Historical earnings surprise pattern ─────────────────────────────
        earnings_surprise_score = 0
        beat_streak = 0
        try:
            hist = tk.earnings_history
            if hist is not None and not hist.empty and "surprisePercent" in hist.columns:
                # Most recent 4 quarters
                recent = hist.dropna(subset=["surprisePercent"]).tail(4)
                beats = (recent["surprisePercent"] > 0).sum()
                total = len(recent)
                if total >= 3:
                    beat_streak = beats
                    if beats == total:
                        earnings_surprise_score = 2  # Perfect beat record
                        earnings_note += f" | Beat {beats}/{total} qtrs ✓✓"
                    elif beats >= total - 1:
                        earnings_surprise_score = 1  # Strong beat record
                        earnings_note += f" | Beat {beats}/{total} qtrs ✓"
                    elif beats <= 1:
                        earnings_surprise_score = -1  # Consistent misser — penalty
                        earnings_note += f" | Misser {total - beats}/{total} qtrs ✗"
        except Exception as e:
            log.debug(f"{ticker} earnings history error: {e}")

        return {
            "days_to_earnings": int(days_to_earnings) if days_to_earnings is not None else None,
            "earnings_risk": earnings_risk,
            "earnings_surprise_score": int(earnings_surprise_score),
            "beat_streak": int(beat_streak),
            "earnings_note": earnings_note.strip(" |"),
        }

    except Exception as e:
        log.warning(f"Earnings check failed for {ticker}: {e}")
        return {
            "days_to_earnings": None,
            "earnings_risk": "UNKNOWN",
            "earnings_surprise_score": 0,
            "beat_streak": 0,
            "earnings_note": "",
        }


def check_consecutive_losses(ticker: str, trades_dir: str) -> bool:
    """
    Returns True if the ticker had ANY loss in the last 2 trading day logs.
    One loss = 1-day cooldown. Two losses = still on cooldown (2 days looked back).
    Does NOT require closed=True — checks pnl field directly so Bug 2 doesn't cascade here.
    """
    import glob
    from datetime import date as _date

    today_str = _date.today().isoformat()
    pattern = os.path.join(trades_dir, "????-??-??.json")
    files = sorted(f for f in glob.glob(pattern)
                   if os.path.basename(f).replace(".json", "") != today_str)

    # Look at the last 2 trading day logs
    recent_files = files[-2:] if len(files) >= 2 else files
    if not recent_files:
        return False

    for fpath in recent_files:
        try:
            with open(fpath) as f:
                day_log = json.load(f)
            day_trades = day_log.get("trades", [])
            ticker_trades = [
                t for t in day_trades
                if t.get("underlying_ticker") == ticker
            ]
            for t in ticker_trades:
                pnl = t.get("pnl")
                # A trade counts as a loss if: pnl is set and negative,
                # OR it was closed with a negative pnl_pct
                pnl_pct = t.get("pnl_pct")
                if (pnl is not None and pnl < 0) or (pnl_pct is not None and pnl_pct < 0):
                    log.info(f"{ticker}: COOLDOWN triggered — loss found in {os.path.basename(fpath)} (pnl={pnl}, pnl_pct={pnl_pct})")
                    return True
        except Exception:
            pass

    return False

def get_dynamic_universe(n: int = 30) -> list:
    """
    Pull top tickers by options volume from Yahoo Finance screener.
    Falls back to WATCHLIST if the screener is unavailable.
    Returns a deduped list of tickers.
    """
    tickers = []
    try:
        url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
        headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
        params = {"scrIds": "most_actives", "count": n, "formatted": "false"}
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        data = resp.json()
        quotes = data["finance"]["result"][0]["quotes"]
        tickers = [q["symbol"] for q in quotes if q.get("symbol")]
        log.info(f"Dynamic universe: {len(tickers)} tickers from Yahoo options screener")
    except Exception as e:
        log.warning(f"Options screener failed ({e}), falling back to WATCHLIST")

    # Always include watchlist tickers (for politician tracking + baseline)
    combined = list(dict.fromkeys(tickers + WATCHLIST))[:n]
    log.info(f"Final scan universe ({len(combined)}): {combined}")
    return combined


def scan_all() -> list:
    """Scan dynamic options-active universe and return scored signals."""
    universe = get_dynamic_universe(n=30)
    log.info(f"Starting scan of {len(universe)} tickers...")
    politician_scores = load_politician_scores()
    signals = []

    for ticker in universe:
        log.info(f"Scanning {ticker}...")
        opt_signal = get_options_signal(ticker)
        time.sleep(0.5)

        news_score, top_headline, news_direction = get_news_score(ticker)

        earnings = get_earnings_data(ticker)
        earnings_surprise_score = earnings["earnings_surprise_score"]
        earnings_risk = earnings["earnings_risk"]
        earnings_note = earnings["earnings_note"]
        days_to_earnings = earnings["days_to_earnings"]

        pol_data = politician_scores.get(ticker, {})
        politician_score = pol_data.get("score", 0)
        politician_note = ""
        if politician_score > 0:
            count = pol_data.get("transaction_count", 0)
            names = ", ".join(p["name"] for p in pol_data.get("politicians", [])[:2])
            politician_note = f"{count} congressional buy(s): {names}"

        options_score = opt_signal["options_score"]
        total_score = options_score + news_score + politician_score + earnings_surprise_score
        total_score = min(10, max(0, total_score))

        # ── Earnings danger block ────────────────────────────────────────────
        earnings_blocked = earnings_risk == "DANGER"
        if earnings_blocked:
            log.info(f"  {ticker}: EARNINGS DANGER — {earnings_note}")

        # ── Consecutive-loss cooldown ────────────────────────────────────────
        cooldown_triggered = check_consecutive_losses(ticker, BASE_DIR + "/trades")
        if cooldown_triggered:
            log.info(f"  {ticker}: COOLDOWN — 2 consecutive losses on last 2 trading days")

        final_direction = opt_signal["direction"]
        if not final_direction and politician_score > 0:
            final_direction = "LONG"
        # If options gave no direction, use news direction as tiebreaker
        if not final_direction and news_direction:
            final_direction = news_direction
        # If options and news conflict, reduce news score contribution
        if final_direction and news_direction and final_direction != news_direction:
            news_score = max(0, news_score - 1)
            log.info(f"  {ticker}: news direction ({news_direction}) conflicts with options ({final_direction}) — news score penalized")

        sweep_note = ""
        sc = opt_signal.get("sweep_calls", 0)
        sp = opt_signal.get("sweep_puts", 0)
        if sc > 0 or sp > 0:
            sweep_note = f" 🌊 sweeps: {sc}C/{sp}P"

        signal = {
            "ticker": ticker,
            "score": round(total_score, 2),
            "options_score": options_score,
            "news_score": news_score,
            "politician_score": round(politician_score, 2),
            "politician_note": politician_note,
            "direction": final_direction,
            "current_price": opt_signal.get("current_price"),
            "call_score": opt_signal.get("call_score", 0),
            "put_score": opt_signal.get("put_score", 0),
            "call_premium_est": opt_signal.get("call_premium_est", 0),
            "put_premium_est": opt_signal.get("put_premium_est", 0),
            "sweep_calls": opt_signal.get("sweep_calls", 0),
            "sweep_puts": opt_signal.get("sweep_puts", 0),
            "top_headline": top_headline,
            "options_detail": opt_signal.get("detail", ""),
            "conviction_flag": opt_signal.get("conviction_flag"),
            "earnings_risk": earnings_risk,
            "earnings_surprise_score": earnings_surprise_score,
            "days_to_earnings": days_to_earnings,
            "earnings_note": earnings_note,
            "tradeable": total_score >= MIN_SIGNAL_SCORE and final_direction is not None and not cooldown_triggered and not earnings_blocked,
            "cooldown": cooldown_triggered,
            "cooldown_note": "COOLDOWN: 2 consecutive losses" if cooldown_triggered else "",
            "scanned_at": datetime.now().isoformat()
        }
        signals.append(signal)
        pol_info = f" 🏛️ pol={politician_score}" if politician_score > 0 else ""
        earn_info = f" 📅 earn={earnings_surprise_score:+d}({earnings_risk})" if earnings_risk != "UNKNOWN" else ""
        log.info(
            f"  {ticker}: score={total_score} opt={options_score} news={news_score}"
            f"{pol_info}{earn_info} dir={final_direction}{sweep_note}"
        )

    signals.sort(key=lambda x: x["score"], reverse=True)
    return signals


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Signal scanner")
    parser.add_argument("--source", choices=["eod", "open"], default="open",
                        help="eod = end-of-day scan (saves to signals_eod.json), open = intraday scan")
    args = parser.parse_args()

    signals = scan_all()

    # Tag each signal with source
    for s in signals:
        s["source"] = args.source

    tradeable = [s for s in signals if s["tradeable"]]
    log.info(f"\n{'='*50}")
    log.info(f"Scan complete [{args.source}]. {len(tradeable)}/{len(signals)} tickers tradeable (score >= {MIN_SIGNAL_SCORE})")
    for s in tradeable:
        pol = f" 🏛️ {s['politician_note']}" if s.get("politician_note") else ""
        sweep = f" 🌊 {s['sweep_calls']}C/{s['sweep_puts']}P sweeps" if (s.get('sweep_calls') or s.get('sweep_puts')) else ""
        log.info(
            f"  ✅ {s['ticker']} score={s['score']} dir={s['direction']} "
            f"price=${s['current_price']}{pol}{sweep}"
        )

    output = {
        "scanned_at": datetime.now().isoformat(),
        "source": args.source,
        "signals": signals,
        "tradeable": tradeable
    }

    out_file = SIGNALS_EOD_FILE if args.source == "eod" else SIGNALS_FILE
    with open(out_file, "w") as f:
        json.dump(output, f, indent=2)
    log.info(f"Signals saved to {out_file} (source={args.source})")


if __name__ == "__main__":
    main()
