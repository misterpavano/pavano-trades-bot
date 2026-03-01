#!/usr/bin/env python3
"""
signals.py — Options flow + news + politician signal scanner
Final score = options_score (0-6) + news_score (0-2) + politician_score (0-3) — max 10
"""

import json
import time
import logging
import requests
import yfinance as yf
from datetime import datetime, timedelta, date
import numpy as np
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from config import (
    WATCHLIST, SEARXNG_URL, SIGNALS_FILE,
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


def get_options_signal(ticker: str) -> dict:
    """Pull options chain and score unusual flow."""
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
                valid_expiries.append(exp)

        if not valid_expiries:
            return {"ticker": ticker, "options_score": 0, "direction": None, "detail": "no near-term expiry"}

        current_price = None
        try:
            info = tk.fast_info
            current_price = info.last_price
        except Exception:
            pass

        total_unusual_calls = 0
        total_unusual_puts = 0
        call_volume = 0
        put_volume = 0
        unusual_details = []

        for exp in valid_expiries[:3]:
            try:
                chain = tk.option_chain(exp)
                calls = chain.calls
                puts = chain.puts

                if current_price:
                    otm_calls = calls[calls["strike"] > current_price * 1.01]
                    otm_puts = puts[puts["strike"] < current_price * 0.99]
                else:
                    otm_calls = calls
                    otm_puts = puts

                for _, row in otm_calls.iterrows():
                    vol = row.get("volume", 0) or 0
                    oi = row.get("openInterest", 0) or 0
                    if oi > 0 and vol > oi * UNUSUAL_VOLUME_MULTIPLIER and vol > 100:
                        total_unusual_calls += 1
                        call_volume += vol
                        unusual_details.append(f"CALL {row['strike']} exp={exp} vol={vol} oi={oi}")

                for _, row in otm_puts.iterrows():
                    vol = row.get("volume", 0) or 0
                    oi = row.get("openInterest", 0) or 0
                    if oi > 0 and vol > oi * UNUSUAL_VOLUME_MULTIPLIER and vol > 100:
                        total_unusual_puts += 1
                        put_volume += vol
                        unusual_details.append(f"PUT {row['strike']} exp={exp} vol={vol} oi={oi}")

            except Exception as e:
                log.warning(f"{ticker} chain error for {exp}: {e}")
                continue

        options_score = 0
        direction = None

        if total_unusual_calls > 0 or total_unusual_puts > 0:
            net_bullish = total_unusual_calls - total_unusual_puts
            if net_bullish > 0:
                direction = "LONG"
                options_score = min(6, 2 + total_unusual_calls + (1 if call_volume > 10000 else 0))
            elif net_bullish < 0:
                direction = "SHORT"
                options_score = min(6, 2 + total_unusual_puts + (1 if put_volume > 10000 else 0))

        return {
            "ticker": ticker,
            "options_score": options_score,
            "direction": direction,
            "unusual_calls": total_unusual_calls,
            "unusual_puts": total_unusual_puts,
            "call_volume": call_volume,
            "put_volume": put_volume,
            "current_price": current_price,
            "detail": "; ".join(unusual_details[:3]) if unusual_details else "no unusual flow"
        }

    except Exception as e:
        log.error(f"Error scanning {ticker}: {e}")
        return {"ticker": ticker, "options_score": 0, "direction": None, "detail": str(e)}


def get_news_score(ticker: str):
    """Fetch news from SearXNG and score catalyst strength."""
    try:
        company_names = {
            "SPY": "S&P 500 ETF market",
            "QQQ": "Nasdaq QQQ ETF market",
            "AAPL": "Apple stock",
            "TSLA": "Tesla stock",
            "NVDA": "Nvidia stock",
            "AMD": "AMD stock",
            "MSFT": "Microsoft stock",
            "META": "Meta stock",
            "GME": "GameStop stock",
            "AMZN": "Amazon stock"
        }
        query = company_names.get(ticker, f"{ticker} stock")
        url = f"{SEARXNG_URL}?q={requests.utils.quote(query)}&format=json&time_range=day&categories=news"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        results = data.get("results", [])

        if not results:
            return 0, "no news"

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
        top_headline = headlines[0] if headlines else "no headline"

        if bull_hits > bear_hits and len(results) >= 3:
            news_score = min(2, 1 + bull_hits // 3)
        elif bear_hits > bull_hits and len(results) >= 3:
            news_score = min(2, 1 + bear_hits // 3)
        elif len(results) >= 5:
            news_score = 1

        return news_score, top_headline

    except Exception as e:
        log.warning(f"News fetch error for {ticker}: {e}")
        return 0, "news error"


def scan_all() -> list[dict]:
    """Scan all watchlist tickers and return scored signals."""
    log.info(f"Starting scan of {len(WATCHLIST)} tickers...")
    politician_scores = load_politician_scores()
    signals = []

    for ticker in WATCHLIST:
        log.info(f"Scanning {ticker}...")
        opt_signal = get_options_signal(ticker)
        time.sleep(0.5)

        news_score, top_headline = get_news_score(ticker)

        # Politician score (capped at 3)
        pol_data = politician_scores.get(ticker, {})
        politician_score = pol_data.get("score", 0)
        politician_note = ""
        if politician_score > 0:
            count = pol_data.get("transaction_count", 0)
            names = ", ".join(p["name"] for p in pol_data.get("politicians", [])[:2])
            politician_note = f"{count} congressional buy(s): {names}"

        # Final score: options (0-6) + news (0-2) + politician (0-3) = max 10
        options_score = opt_signal["options_score"]
        total_score = options_score + news_score + politician_score
        total_score = min(10, total_score)

        final_direction = opt_signal["direction"]
        # If only politician signal, assume LONG
        if not final_direction and politician_score > 0:
            final_direction = "LONG"

        signal = {
            "ticker": ticker,
            "score": round(total_score, 2),
            "options_score": options_score,
            "news_score": news_score,
            "politician_score": round(politician_score, 2),
            "politician_note": politician_note,
            "direction": final_direction,
            "current_price": opt_signal.get("current_price"),
            "unusual_calls": opt_signal.get("unusual_calls", 0),
            "unusual_puts": opt_signal.get("unusual_puts", 0),
            "top_headline": top_headline,
            "options_detail": opt_signal.get("detail", ""),
            "tradeable": total_score >= MIN_SIGNAL_SCORE and final_direction is not None,
            "scanned_at": datetime.now().isoformat()
        }
        signals.append(signal)
        pol_info = f" 🏛️ pol={politician_score}" if politician_score > 0 else ""
        log.info(f"  {ticker}: score={total_score} opt={options_score} news={news_score}{pol_info} dir={final_direction}")

    signals.sort(key=lambda x: x["score"], reverse=True)
    return signals


def main():
    signals = scan_all()

    tradeable = [s for s in signals if s["tradeable"]]
    log.info(f"\n{'='*50}")
    log.info(f"Scan complete. {len(tradeable)}/{len(signals)} tickers tradeable (score >= {MIN_SIGNAL_SCORE})")
    for s in tradeable:
        pol = f" 🏛️ {s['politician_note']}" if s.get("politician_note") else ""
        log.info(f"  ✅ {s['ticker']} score={s['score']} dir={s['direction']} price=${s['current_price']}{pol}")

    output = {
        "scanned_at": datetime.now().isoformat(),
        "signals": signals,
        "tradeable": tradeable
    }
    with open(SIGNALS_FILE, "w") as f:
        json.dump(output, f, indent=2)
    log.info(f"Signals saved to {SIGNALS_FILE}")


if __name__ == "__main__":
    main()
