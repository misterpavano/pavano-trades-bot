#!/usr/bin/env python3
"""
learn.py — Post-trade learning system
Reads the daily trade log, updates performance records, ticker logs, win rates, and daily learnings.
"""

import json
import logging
import os
from datetime import date, datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRADES_DIR = os.path.join(BASE_DIR, "trades")
KNOWLEDGE_DIR = os.path.join(BASE_DIR, "knowledge")
SIGNALS_DIR = os.path.join(KNOWLEDGE_DIR, "signals")
TICKERS_DIR = os.path.join(KNOWLEDGE_DIR, "tickers")
PERFORMANCE_FILE = os.path.join(SIGNALS_DIR, "performance.json")
WIN_RATE_FILE = os.path.join(SIGNALS_DIR, "win_rate.json")

TODAY = date.today().isoformat()
LOG_FILE = os.path.join(TRADES_DIR, f"{TODAY}.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


def determine_signal_type(trade):
    opts = trade.get("options_score", 0) or 0
    news = trade.get("news_score", 0) or 0
    pol = trade.get("politician_score", 0) or 0
    types = []
    if opts > 0:
        types.append("options")
    if news > 0:
        types.append("news")
    if pol > 0:
        types.append("politician")
    if not types:
        types = ["unknown"]
    return "+".join(types)


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def update_ticker_md(ticker, trade):
    os.makedirs(TICKERS_DIR, exist_ok=True)
    md_path = os.path.join(TICKERS_DIR, f"{ticker}.md")
    entry = trade.get("entry_price", 0)
    exit_p = trade.get("exit_price", 0)
    pnl_pct = trade.get("pnl_pct", 0) or 0
    signal_type = determine_signal_type(trade)
    outcome = "✅" if pnl_pct >= 0 else "❌"
    sign = "+" if pnl_pct >= 0 else ""

    row = f"| {TODAY} | ${entry:.2f} | ${exit_p:.2f} | {sign}{pnl_pct:.2f}% | {signal_type} | {outcome} |"

    if os.path.exists(md_path):
        with open(md_path) as f:
            content = f.read()
    else:
        content = f"## {ticker} Trade Log\n\n| Date | Entry | Exit | P&L | Signal | Outcome |\n|------|-------|------|-----|--------|---------|\n"

    content += row + "\n"
    with open(md_path, "w") as f:
        f.write(content)
    log.info(f"Updated {md_path}")


def update_win_rate(signal_type, won):
    data = load_json(WIN_RATE_FILE, {})
    key = signal_type
    if key not in data:
        data[key] = {"wins": 0, "losses": 0, "total": 0, "win_rate": 0.0}

    data[key]["total"] += 1
    if won:
        data[key]["wins"] += 1
    else:
        data[key]["losses"] += 1
    total = data[key]["total"]
    data[key]["win_rate"] = round(data[key]["wins"] / total * 100, 1) if total > 0 else 0.0

    # Also track combined
    if "all" not in data:
        data["all"] = {"wins": 0, "losses": 0, "total": 0, "win_rate": 0.0}
    data["all"]["total"] += 1
    if won:
        data["all"]["wins"] += 1
    else:
        data["all"]["losses"] += 1
    data["all"]["win_rate"] = round(data["all"]["wins"] / data["all"]["total"] * 100, 1)

    save_json(WIN_RATE_FILE, data)


def write_learnings(closed_trades, win_rate_data):
    os.makedirs(SIGNALS_DIR, exist_ok=True)
    learnings_path = os.path.join(SIGNALS_DIR, f"{TODAY}-learnings.md")
    lines = [f"# Trading Learnings — {TODAY}\n"]

    if not closed_trades:
        lines.append("No closed trades today.\n")
    else:
        lines.append(f"## Closed Trades ({len(closed_trades)})\n")
        for t in closed_trades:
            ticker = t.get("symbol", "?")
            pnl_pct = t.get("pnl_pct", 0) or 0
            signal_type = determine_signal_type(t)
            outcome = "WIN" if pnl_pct >= 0 else "LOSS"
            lines.append(f"- **{ticker}**: {'+' if pnl_pct >= 0 else ''}{pnl_pct:.2f}% — {signal_type} signal — {outcome}")

    lines.append("\n## Win Rates by Signal Type\n")
    for sig_type, stats in win_rate_data.items():
        lines.append(f"- **{sig_type}**: {stats['win_rate']}% ({stats['wins']}W/{stats['losses']}L, {stats['total']} trades)")

    with open(learnings_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    log.info(f"Learnings written to {learnings_path}")


def main():
    if not os.path.exists(LOG_FILE):
        log.warning(f"No trade log for today: {LOG_FILE}")
        return

    with open(LOG_FILE) as f:
        daily_log = json.load(f)

    trades = daily_log.get("trades", [])
    closed = [t for t in trades if t.get("closed")]

    if not closed:
        log.info("No closed trades to learn from today.")
        # Still write an empty learnings file
        win_rate = load_json(WIN_RATE_FILE, {})
        write_learnings([], win_rate)
        return

    log.info(f"Processing {len(closed)} closed trades...")

    performance = load_json(PERFORMANCE_FILE, [])

    for trade in closed:
        ticker = trade.get("symbol", "?")
        entry = trade.get("entry_price", 0)
        exit_p = trade.get("exit_price", 0)
        pnl_pct = trade.get("pnl_pct", 0) or 0
        pnl = trade.get("pnl", 0) or 0
        won = pnl_pct >= 0
        signal_type = determine_signal_type(trade)

        record = {
            "date": TODAY,
            "ticker": ticker,
            "entry": entry,
            "exit": exit_p,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "signal_score": trade.get("signal_score", 0),
            "signal_type": signal_type,
            "options_score": trade.get("options_score", 0),
            "news_score": trade.get("news_score", 0),
            "politician_score": trade.get("politician_score", 0),
            "won": won,
            "close_reason": trade.get("close_reason", "eod")
        }
        performance.append(record)

        update_ticker_md(ticker, trade)
        update_win_rate(signal_type, won)

        log.info(f"  {ticker}: {'WIN' if won else 'LOSS'} {'+' if pnl_pct >= 0 else ''}{pnl_pct:.2f}% via {signal_type}")

    save_json(PERFORMANCE_FILE, performance)
    log.info(f"Performance log updated: {len(performance)} total records")

    win_rate = load_json(WIN_RATE_FILE, {})
    write_learnings(closed, win_rate)
    log.info("Learning run complete ✅")


if __name__ == "__main__":
    main()
