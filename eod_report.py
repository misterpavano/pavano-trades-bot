#!/usr/bin/env python3
"""
eod_report.py — Generate EOD summary and send to Telegram.
"""

import json
import logging
import os
import sys
import requests
from datetime import date

sys.path.insert(0, os.path.dirname(__file__))
from config import (
    ALPACA_KEY, ALPACA_SECRET, TRADES_DIR,
    STARTING_CAPITAL, TELEGRAM_GROUP
)

from alpaca.trading.client import TradingClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TODAY = date.today().isoformat()
LOG_FILE = os.path.join(TRADES_DIR, f"{TODAY}.json")
POLITICIANS_LATEST = os.path.join(BASE_DIR, "knowledge", "politicians", "latest.json")
WIN_RATE_FILE = os.path.join(BASE_DIR, "knowledge", "signals", "win_rate.json")


def get_account_info():
    client = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=True)
    account = client.get_account()
    positions = client.get_all_positions()

    open_positions = []
    for p in positions:
        open_positions.append({
            "symbol": p.symbol,
            "qty": float(p.qty),
            "current_price": float(p.current_price),
            "market_value": float(p.market_value),
            "unrealized_pl": float(p.unrealized_pl),
            "unrealized_plpc": float(p.unrealized_plpc)
        })

    return {
        "equity": float(account.equity),
        "cash": float(account.cash),
        "open_positions": open_positions
    }


def load_politician_signals():
    """Load top politician signals for today's report."""
    if not os.path.exists(POLITICIANS_LATEST):
        return []
    try:
        with open(POLITICIANS_LATEST) as f:
            data = json.load(f)
        return data.get("signals", [])[:3]
    except Exception:
        return []


def load_win_rates():
    if not os.path.exists(WIN_RATE_FILE):
        return {}
    try:
        with open(WIN_RATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def build_report(account: dict, daily_log: dict) -> str:
    equity = account["equity"]
    cash = account["cash"]
    open_positions = account["open_positions"]
    pct_change = ((equity - STARTING_CAPITAL) / STARTING_CAPITAL) * 100
    sign = "+" if pct_change >= 0 else ""

    trades = daily_log.get("trades", [])
    signals_used = daily_log.get("signals_used", [])

    # Build trades summary
    trade_lines = []
    winning_signals = {}
    for t in trades:
        symbol = t["symbol"]
        entry = t.get("ask_at_entry") or t.get("entry_price") or 0  # field is ask_at_entry in trade records
        if t.get("closed"):
            exit_p = t.get("exit_price", 0)
            pnl_pct = t.get("pnl_pct", 0)
            sign_t = "+" if pnl_pct >= 0 else ""
            emoji = "✅" if pnl_pct >= 0 else "❌"
            trade_lines.append(f"• BUY {symbol} @ ${entry:.2f} → SOLD @ ${exit_p:.2f} ({sign_t}{pnl_pct:.1f}%) {emoji}")
            if pnl_pct >= 0:
                sig_type = []
                if t.get("options_score", 0) > 0: sig_type.append("options")
                if t.get("news_score", 0) > 0: sig_type.append("news")
                if t.get("politician_score", 0) > 0: sig_type.append("politician")
                for st in sig_type:
                    winning_signals[st] = winning_signals.get(st, 0) + 1
        else:
            current = next((p["current_price"] for p in open_positions if p["symbol"] == symbol), entry)
            pnl_pct = ((current - entry) / entry * 100) if entry > 0 else 0
            sign_t = "+" if pnl_pct >= 0 else ""
            trade_lines.append(f"• BUY {symbol} @ ${entry:.2f} → HELD (current: ${current:.2f}, {sign_t}{pnl_pct:.1f}%)")

    if not trade_lines:
        trade_lines = ["• No trades placed today"]

    # Top signal info
    top_signal = "No tradeable signals"
    if signals_used:
        top = signals_used[0]
        pol_note = f" 🏛️ {top.get('politician_note', '')}" if top.get("politician_note") else ""
        top_signal = f"{top['ticker']} (score={top['score']}, {top.get('top_headline', 'no headline')[:70]}){pol_note}"

    trades_text = "\n".join(trade_lines)
    open_count = len(open_positions)

    # Politician signals section
    pol_signals = load_politician_signals()
    pol_lines = []
    for ps in pol_signals[:3]:
        names = ", ".join(p["name"] for p in ps["politicians"][:2])
        pol_lines.append(f"• {ps['ticker']} (score={ps['score']}) — {names}")
    pol_section = "\n".join(pol_lines) if pol_lines else "• No recent congressional buys"

    # Win rates section
    win_rates = load_win_rates()
    wr_lines = []
    for sig_type, stats in win_rates.items():
        if sig_type != "all" and stats["total"] > 0:
            wr_lines.append(f"• {sig_type}: {stats['win_rate']}% ({stats['total']} trades)")
    if win_rates.get("all", {}).get("total", 0) > 0:
        all_wr = win_rates["all"]
        wr_lines.append(f"• Overall: {all_wr['win_rate']}% ({all_wr['total']} trades)")
    wr_section = "\n".join(wr_lines) if wr_lines else "• No trade history yet"

    # Winning signals today
    if winning_signals:
        top_winner = max(winning_signals, key=winning_signals.get)
        winner_note = f"Today's best signal: {top_winner}"
    else:
        winner_note = "No winning signals tracked today"

    report = f"""📊 TRADES EOD — {TODAY}

💰 Portfolio: ${equity:.2f} ({sign}{pct_change:.2f}% vs ${STARTING_CAPITAL:.0f} start)

📋 Today's Trades:
{trades_text}

🎯 Top Signal: {top_signal}

🏛️ Congressional Buys (active):
{pol_section}

📚 Running Win Rates:
{wr_section}
💡 {winner_note}

📈 Open Positions: {open_count} | 💵 Cash: ${cash:.2f}"""

    return report


TELEGRAM_BOT_TOKEN = "8787606784:AAFkKAr2oI4uMlTa5FbyE5J_l550w4e1VI0"
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

def send_telegram(message: str):
    # Escape underscores outside of markdown bold/italic to prevent parse errors
    safe_message = message.replace("_", "\\_")
    payload = {"chat_id": TELEGRAM_GROUP, "text": safe_message, "parse_mode": "Markdown"}
    try:
        resp = requests.post(TELEGRAM_API, json=payload, timeout=10)
        if resp.ok:
            log.info("EOD report sent to Telegram ✅")
        else:
            log.error(f"Telegram send failed: {resp.status_code} {resp.text}")
    except Exception as e:
        log.error(f"Telegram send error: {e}")


def main():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            daily_log = json.load(f)
    else:
        log.warning(f"No trade log for today ({LOG_FILE}). Generating report from account state only.")
        daily_log = {"date": TODAY, "trades": [], "signals_used": []}

    try:
        account = get_account_info()
    except Exception as e:
        log.error(f"Failed to get account info: {e}")
        account = {"equity": STARTING_CAPITAL, "cash": STARTING_CAPITAL, "open_positions": []}

    report = build_report(account, daily_log)
    log.info(f"\n{report}")
    send_telegram(report)


if __name__ == "__main__":
    main()
