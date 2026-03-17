#!/usr/bin/env python3
"""
retro.py — Daily close retro. Run at 4:15pm ET every trading day.

Reviews what the bot did today, what it missed, what's broken,
and what needs fixing. Sends to Telegram. No fluff — just the truth.
"""

import json
import logging
import os
import sys
import requests
import yfinance as yf
from datetime import date, datetime, timedelta
import glob

sys.path.insert(0, os.path.dirname(__file__))
from config import (
    ALPACA_KEY, ALPACA_SECRET, TRADES_DIR,
    STARTING_CAPITAL, TELEGRAM_GROUP, TELEGRAM_BOT_TOKEN,
    SIGNALS_FILE, MIN_SIGNAL_SCORE
)
from alpaca.trading.client import TradingClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

TODAY = date.today().isoformat()
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"


def send_telegram(text: str):
    try:
        requests.post(
            TELEGRAM_API,
            json={"chat_id": TELEGRAM_GROUP, "text": text, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        log.warning(f"Telegram failed: {e}")


def get_portfolio():
    try:
        client = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=True)
        acct = client.get_account()
        positions = client.get_all_positions()
        return {
            "equity": float(acct.equity),
            "cash": float(acct.cash),
            "positions": [
                {
                    "symbol": p.symbol,
                    "qty": float(p.qty),
                    "current_price": float(p.current_price),
                    "unrealized_pl": float(p.unrealized_pl),
                    "unrealized_plpc": float(p.unrealized_plpc),
                }
                for p in positions
            ]
        }
    except Exception as e:
        log.warning(f"Portfolio fetch failed: {e}")
        return {"equity": 0, "cash": 0, "positions": []}


def load_today_trades():
    log_file = os.path.join(TRADES_DIR, f"{TODAY}.json")
    if not os.path.exists(log_file):
        return []
    try:
        with open(log_file) as f:
            return json.load(f).get("trades", [])
    except Exception:
        return []


def load_signals():
    if not os.path.exists(SIGNALS_FILE):
        return []
    try:
        with open(SIGNALS_FILE) as f:
            return json.load(f).get("signals", [])
    except Exception:
        return []


def get_market_movers():
    """Pull today's top movers from yfinance for missed-opportunity detection."""
    movers = []
    # Check a broad list for what actually moved today
    universe = [
        "SPY", "QQQ", "TSLA", "NVDA", "AAPL", "META", "AMZN", "MSFT",
        "DAL", "AAL", "UAL", "LUV", "COIN", "MSTR", "PLTR", "SOFI",
        "AMD", "INTC", "BMNR", "GME", "HOOD", "SOUN", "RKLB",
    ]
    for ticker in universe:
        try:
            info = yf.Ticker(ticker).fast_info
            current = info.last_price
            prev = info.previous_close
            if current and prev and prev > 0:
                pct = (current - prev) / prev * 100
                movers.append({"ticker": ticker, "pct": round(pct, 2), "price": round(current, 2)})
        except Exception:
            pass
    movers.sort(key=lambda x: abs(x["pct"]), reverse=True)
    return movers[:10]


def all_time_stats():
    """Pull win rate and total PnL across all trade logs."""
    wins, losses, total_pnl = 0, 0, 0.0
    log_files = sorted(glob.glob(os.path.join(TRADES_DIR, "????-??-??.json")))
    for lf in log_files:
        try:
            with open(lf) as f:
                data = json.load(f)
            for t in data.get("trades", []):
                if t.get("closed"):
                    pnl = t.get("pnl") or 0
                    total_pnl += pnl
                    if pnl >= 0:
                        wins += 1
                    else:
                        losses += 1
        except Exception:
            pass
    total = wins + losses
    win_rate = round(wins / total * 100) if total > 0 else 0
    return wins, losses, total_pnl, win_rate


def build_retro(portfolio, trades, signals, movers):
    equity = portfolio["equity"]
    cash = portfolio["cash"]
    positions = portfolio["positions"]
    pct_vs_start = (equity - STARTING_CAPITAL) / STARTING_CAPITAL * 100
    sign = "+" if pct_vs_start >= 0 else ""

    wins, losses, total_pnl, win_rate = all_time_stats()

    # ── Trades today ─────────────────────────────────────────────────────────
    closed_today = [t for t in trades if t.get("closed")]
    opened_today = [t for t in trades if not t.get("closed")]

    trade_lines = []
    for t in closed_today:
        pnl_pct = t.get("pnl_pct", 0) or 0
        pnl = t.get("pnl", 0) or 0
        emoji = "✅" if pnl >= 0 else "❌"
        reason = t.get("close_reason", "eod")
        trade_lines.append(
            f"{emoji} {t.get('contract_symbol', t.get('symbol', '?'))} "
            f"{'+' if pnl >= 0 else ''}{pnl_pct:.1f}% (${pnl:+.2f}) — {reason}"
        )
    for t in opened_today:
        trade_lines.append(
            f"📂 {t.get('contract_symbol', '?')} opened @ ${t.get('ask_at_entry', 0):.2f}"
        )
    if not trade_lines:
        trade_lines = ["No trades today"]

    # ── Open positions ────────────────────────────────────────────────────────
    pos_lines = []
    for p in positions:
        plpc = p["unrealized_plpc"] * 100
        pos_lines.append(
            f"  {p['symbol']} {'+' if plpc >= 0 else ''}{plpc:.1f}% "
            f"(${p['unrealized_pl']:+.2f})"
        )
    if not pos_lines:
        pos_lines = ["  Flat — no open positions"]

    # ── Signal quality check ──────────────────────────────────────────────────
    tradeable = [s for s in signals if s.get("tradeable")]
    top_signals = sorted(signals, key=lambda x: x.get("score", 0), reverse=True)[:5]
    sig_lines = []
    for s in top_signals:
        tradeable_tag = "✅ FIRED" if s.get("tradeable") else f"❌ score={s['score']} (need {MIN_SIGNAL_SCORE})"
        sig_lines.append(
            f"  {s['ticker']} {s.get('direction','?')} — {tradeable_tag}"
        )

    # ── Missed moves ──────────────────────────────────────────────────────────
    traded_tickers = {t.get("underlying_ticker") for t in trades}
    scanned_tickers = {s["ticker"] for s in signals}
    missed_lines = []
    for m in movers[:6]:
        ticker = m["ticker"]
        direction = "📈" if m["pct"] > 0 else "📉"
        in_scan = ticker in scanned_tickers
        was_traded = ticker in traded_tickers
        if was_traded:
            tag = "✅ traded"
        elif in_scan:
            score = next((s["score"] for s in signals if s["ticker"] == ticker), 0)
            tag = f"in scan (score={score}, needed {MIN_SIGNAL_SCORE})"
        else:
            tag = "NOT in scan universe"
        missed_lines.append(f"  {direction} {ticker} {m['pct']:+.1f}% — {tag}")

    # ── Diagnose: why did nothing fire? ──────────────────────────────────────
    diagnosis_lines = []
    if not tradeable:
        max_score = max((s.get("score", 0) for s in signals), default=0)
        diagnosis_lines.append(f"No signals cleared threshold (max score today: {max_score}, need {MIN_SIGNAL_SCORE})")
    else:
        diagnosis_lines.append(f"{len(tradeable)} signal(s) fired today")

    # Check if we fell back to stale EOD signals
    eod_file = os.path.join(os.path.dirname(__file__), "signals_eod.json")
    if os.path.exists(eod_file):
        try:
            with open(eod_file) as f:
                eod = json.load(f)
            eod_date = eod.get("scanned_at", "")[:10]
            if eod_date != TODAY:
                diagnosis_lines.append(f"⚠️ Bot used stale EOD signals from {eod_date} (not today)")
        except Exception:
            pass

    report = f"""📊 *DAILY RETRO — {TODAY}*

💰 Portfolio: ${equity:.2f} ({sign}{pct_vs_start:.1f}% vs ${STARTING_CAPITAL:.0f})
📈 All-time: {wins}W / {losses}L ({win_rate}% WR) | Total PnL: ${total_pnl:+.2f}
💵 Cash: ${cash:.2f} | Open: {len(positions)} position(s)

*Trades Today:*
{chr(10).join(trade_lines)}

*Open Positions:*
{chr(10).join(pos_lines)}

*Signal Check (top 5):*
{chr(10).join(sig_lines) if sig_lines else '  No signals scanned'}

*Market Movers vs Bot:*
{chr(10).join(missed_lines)}

*Diagnosis:*
{chr(10).join('  ' + d for d in diagnosis_lines)}"""

    return report


def main():
    log.info("Running daily retro...")
    portfolio = get_portfolio()
    trades = load_today_trades()
    signals = load_signals()
    movers = get_market_movers()
    report = build_retro(portfolio, trades, signals, movers)
    log.info(f"\n{report}")
    send_telegram(report)
    log.info("Retro sent ✅")


if __name__ == "__main__":
    main()
