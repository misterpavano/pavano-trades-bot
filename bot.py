#!/usr/bin/env python3
"""
bot.py — OPTIONS-ONLY trading bot
Buys OTM call/put options based on signals from signals.py.

Usage:
  python3 bot.py --mode open     # Execute trades at market open
  python3 bot.py --mode monitor  # Check SL/TP on open positions
  python3 bot.py --mode close    # Close all positions (EOD)

Contract selection:
  - Bullish signal → OTM call, strike ~3% above current price
  - Bearish signal → OTM put, strike ~3% below current price
  - DTE: 7-30 days, prefer 14-21 day range
  - Max ask: $2.00/share ($200/contract), max $100 per position
"""

import argparse
import json
import logging
import os
import sys
import subprocess
import time
import requests
import yfinance as yf
from datetime import datetime, date, timedelta

sys.path.insert(0, os.path.dirname(__file__))
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

from config import (
    ALPACA_KEY, ALPACA_SECRET, ALPACA_BASE_URL,
    STARTING_CAPITAL, MAX_POSITIONS, MIN_SIGNAL_SCORE,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT,
    MAX_POSITION_COST, CASH_RESERVE, MAX_CONTRACT_ASK,
    OPTION_DTE_MIN, OPTION_DTE_MAX, OTM_PCT,
    TRADES_DIR, SIGNALS_FILE, SIGNALS_EOD_FILE
)

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

TODAY = date.today().isoformat()
LOG_FILE = os.path.join(TRADES_DIR, f"{TODAY}.json")

TELEGRAM_CHAT_ID = "-5191423233"
TELEGRAM_BOT_TOKEN = "8787606784:AAFkKAr2oI4uMlTa5FbyE5J_l550w4e1VI0"
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

ALPACA_HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
}


# ─── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(text: str):
    try:
        resp = requests.post(
            TELEGRAM_API,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=5
        )
        log.info(f"Telegram sent (status={resp.status_code})")
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")


def _held_duration(entry_time_iso: str) -> str:
    try:
        entry_dt = datetime.fromisoformat(entry_time_iso)
        delta = datetime.now() - entry_dt
        total_minutes = int(delta.total_seconds() / 60)
        if total_minutes < 60:
            return f"{total_minutes}m"
        hours = total_minutes // 60
        mins = total_minutes % 60
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    except Exception:
        return "?"


def _signal_labels(signal: dict) -> str:
    parts = []
    if signal.get("options_score", 0) > 0:
        parts.append("options")
    if signal.get("news_score", 0) > 0:
        parts.append("news")
    if signal.get("politician_score", 0) > 0:
        parts.append("politician")
    return ", ".join(parts) if parts else "mixed"


# ─── Trade Log ─────────────────────────────────────────────────────────────────

def append_trade_event(event: dict):
    os.makedirs(TRADES_DIR, exist_ok=True)
    event["logged_at"] = datetime.now().isoformat()
    events_file = os.path.join(TRADES_DIR, f"{TODAY}-events.json")
    events = []
    if os.path.exists(events_file):
        try:
            with open(events_file) as f:
                events = json.load(f)
        except Exception:
            events = []
    events.append(event)
    with open(events_file, "w") as f:
        json.dump(events, f, indent=2)


def load_today_log() -> dict:
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            return json.load(f)
    return {
        "date": TODAY,
        "starting_capital": STARTING_CAPITAL,
        "trades": [],
        "signals_used": [],
        "created_at": datetime.now().isoformat()
    }


def save_log(data: dict):
    os.makedirs(TRADES_DIR, exist_ok=True)
    with open(LOG_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ─── Alpaca Clients ────────────────────────────────────────────────────────────

def get_client():
    return TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=True)


def get_data_client():
    return StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)


def get_account_info(client):
    account = client.get_account()
    return {
        "equity": float(account.equity),
        "cash": float(account.cash),
        "buying_power": float(account.buying_power),
        "options_buying_power": float(getattr(account, "options_buying_power", account.buying_power)),
        "portfolio_value": float(account.portfolio_value)
    }


def get_positions(client) -> list:
    positions = client.get_all_positions()
    result = []
    for p in positions:
        result.append({
            "symbol": p.symbol,
            "qty": float(p.qty),
            "side": p.side.value,
            "avg_entry": float(p.avg_entry_price),
            "current_price": float(p.current_price),
            "market_value": float(p.market_value),
            "unrealized_pl": float(p.unrealized_pl),
            "unrealized_plpc": float(p.unrealized_plpc)
        })
    return result


def get_stock_price(data_client, symbol: str) -> float:
    """Get current mid-price for an underlying stock."""
    try:
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        quote = data_client.get_stock_latest_quote(req)
        q = quote[symbol]
        mid = (q.ask_price + q.bid_price) / 2
        if mid > 0:
            return mid
        return float(q.ask_price or q.bid_price or 0)
    except Exception as e:
        log.warning(f"Could not get stock quote for {symbol}: {e}")
        return None


# ─── Options Contract Selection ────────────────────────────────────────────────

def get_option_ask_prices(symbols: list) -> dict:
    """Fetch live ask prices for a list of option symbols via Alpaca data API."""
    if not symbols:
        return {}
    # Batch in chunks of 100
    result = {}
    chunk_size = 50
    for i in range(0, len(symbols), chunk_size):
        chunk = symbols[i:i+chunk_size]
        try:
            resp = requests.get(
                "https://data.alpaca.markets/v1beta1/options/snapshots",
                headers=ALPACA_HEADERS,
                params={"symbols": ",".join(chunk), "feed": "indicative"},
                timeout=10
            )
            if resp.status_code == 200:
                data = resp.json().get("snapshots", {})
                for sym, snap in data.items():
                    q = snap.get("latestQuote", {})
                    ask = q.get("ap")
                    if ask:
                        result[sym] = float(ask)
        except Exception as e:
            log.warning(f"Option snapshot fetch failed: {e}")
    return result


def select_option_contract(ticker: str, direction: str, stock_price: float) -> dict | None:
    """
    Find the best options contract for a given ticker and direction.

    direction: "LONG" → call, "SHORT" → put
    Returns a dict with contract info or None if nothing suitable found.
    """
    option_type = "call" if direction == "LONG" else "put"
    today_dt = date.today()
    dte_min = today_dt + timedelta(days=OPTION_DTE_MIN)
    dte_max = today_dt + timedelta(days=OPTION_DTE_MAX)

    # Target strike: 3% OTM
    if option_type == "call":
        target_strike = stock_price * (1 + OTM_PCT)
    else:
        target_strike = stock_price * (1 - OTM_PCT)

    params = {
        "underlying_symbols": ticker,
        "type": option_type,
        "expiration_date_gte": dte_min.isoformat(),
        "expiration_date_lte": dte_max.isoformat(),
        "status": "active",
        "limit": 200
    }

    try:
        resp = requests.get(
            f"{ALPACA_BASE_URL}/v2/options/contracts",
            headers=ALPACA_HEADERS,
            params=params,
            timeout=10
        )
        if resp.status_code != 200:
            log.warning(f"Options API error for {ticker}: {resp.status_code} {resp.text[:200]}")
            return None

        data = resp.json()
        contracts = data.get("option_contracts", [])
        if not contracts:
            log.info(f"No options contracts found for {ticker} ({option_type}, {dte_min} to {dte_max})")
            return None

    except Exception as e:
        log.error(f"Failed to fetch options chain for {ticker}: {e}")
        return None

    # Filter tradable contracts near target strike (within 10%)
    tradable = [c for c in contracts if c.get("tradable")]
    if not tradable:
        log.info(f"No tradable contracts for {ticker}")
        return None

    # Only fetch quotes for contracts near the target strike (reduce API calls)
    near_strike = [
        c for c in tradable
        if abs(float(c["strike_price"]) - target_strike) / stock_price < 0.10
    ]
    if not near_strike:
        near_strike = tradable[:50]  # fallback: take first 50

    symbols = [c["symbol"] for c in near_strike]
    ask_prices = get_option_ask_prices(symbols)

    candidates = []
    for c in near_strike:
        sym = c["symbol"]
        # Use live ask, fallback to close_price
        ask = ask_prices.get(sym)
        if ask is None:
            cp = c.get("close_price")
            if cp:
                ask = float(cp)
            else:
                continue  # no price data

        if ask > MAX_CONTRACT_ASK:
            continue  # too expensive per share

        strike = float(c["strike_price"])
        exp_date = datetime.strptime(c["expiration_date"], "%Y-%m-%d").date()
        dte = (exp_date - today_dt).days

        candidates.append({
            "symbol": sym,
            "strike": strike,
            "expiration_date": c["expiration_date"],
            "dte": dte,
            "ask": ask,
            "type": option_type,
            "name": c.get("name", sym)
        })

    if not candidates:
        log.info(f"No affordable contracts for {ticker} ({option_type}), ask ≤ ${MAX_CONTRACT_ASK}")
        return None

    # Score candidates: prefer closest to target strike + 14-21 DTE sweet spot
    def score_contract(c):
        strike_diff = abs(c["strike"] - target_strike) / stock_price  # normalized
        dte_diff = abs(c["dte"] - 17) / 30  # prefer ~17 DTE
        return strike_diff + dte_diff  # lower = better

    candidates.sort(key=score_contract)
    best = candidates[0]
    log.info(f"Selected {best['symbol']}: strike=${best['strike']}, DTE={best['dte']}, ask=${best['ask']}")
    return best


# ─── Order Execution ───────────────────────────────────────────────────────────

def buy_option_contract(client, contract_symbol: str, qty: int) -> dict:
    """Submit a market buy order for an options contract."""
    req = MarketOrderRequest(
        symbol=contract_symbol,
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY
    )
    order = client.submit_order(req)
    log.info(f"Options order submitted: BUY {qty}x {contract_symbol} — ID={order.id}")
    return {
        "order_id": str(order.id),
        "symbol": contract_symbol,
        "qty": qty,
        "side": "BUY",
        "submitted_at": datetime.now().isoformat(),
        "status": str(order.status)
    }


def sell_option_position(client, contract_symbol: str, qty: int) -> dict:
    """Submit a market sell order for an options position."""
    req = MarketOrderRequest(
        symbol=contract_symbol,
        qty=qty,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY
    )
    order = client.submit_order(req)
    log.info(f"Options order submitted: SELL {qty}x {contract_symbol} — ID={order.id}")
    return {
        "order_id": str(order.id),
        "symbol": contract_symbol,
        "qty": qty,
        "side": "SELL",
        "submitted_at": datetime.now().isoformat(),
        "status": str(order.status)
    }


# ─── Modes ─────────────────────────────────────────────────────────────────────


# ─── Two-Shot Signal Loading ───────────────────────────────────────────────────

def check_open_confirmation(ticker: str, signal_direction: str) -> tuple:
    """
    Check if current price confirms the EOD signal direction.
    Returns (confirmed: bool, reason: str)
    Gap > 2% against signal direction = skip.
    """
    try:
        tk = yf.Ticker(ticker)
        info = tk.fast_info
        current = info.last_price
        prev_close = info.previous_close
        if not current or not prev_close or prev_close == 0:
            return True, "no gap data, proceeding"
        gap_pct = (current - prev_close) / prev_close
        if signal_direction == "LONG":
            if gap_pct < -0.03:
                return False, f"gapped down {gap_pct*100:.1f}% against LONG signal (>3%)"
            return True, f"gap {gap_pct*100:+.1f}% confirms LONG"
        else:  # SHORT
            if gap_pct > 0.03:
                return False, f"gapped up {gap_pct*100:.1f}% against SHORT signal (>3%)"
            return True, f"gap {gap_pct*100:+.1f}% confirms SHORT"
    except Exception as e:
        log.warning(f"Gap check failed for {ticker}: {e}")
        return True, "gap check error, proceeding"


def load_signals():
    """
    Two-shot loader: prefer today's or yesterday's EOD signals, merge with open scan.
    Returns (eod_data, open_data) — either can be None.
    """
    today_str = date.today().isoformat()
    yesterday_str = (date.today() - timedelta(days=1)).isoformat()
    eod_data = None
    open_data = None

    if os.path.exists(SIGNALS_EOD_FILE):
        try:
            with open(SIGNALS_EOD_FILE) as f:
                data = json.load(f)
            scan_date = data.get("scanned_at", "")[:10]
            if scan_date in [today_str, yesterday_str]:
                eod_data = data
                log.info(f"Loaded EOD signals from {scan_date} ({len(data.get('tradeable', []))} tradeable)")
        except Exception as e:
            log.warning(f"Could not load EOD signals: {e}")

    if os.path.exists(SIGNALS_FILE):
        try:
            with open(SIGNALS_FILE) as f:
                data = json.load(f)
            scan_date = data.get("scanned_at", "")[:10]
            if scan_date == today_str:
                open_data = data
                log.info(f"Loaded open scan signals from today ({len(data.get('tradeable', []))} tradeable)")
        except Exception as e:
            log.warning(f"Could not load open signals: {e}")

    return eod_data, open_data


def mode_open():
    """Load signals and buy option contracts at market open."""
    log.info("=== MODE: OPEN (options) ===")
    client = get_client()
    data_client = get_data_client()

    eod_signals, open_signals = load_signals()

    account = get_account_info(client)
    log.info(f"Account: equity=${account['equity']:.2f} cash=${account['cash']:.2f} options_bp=${account['options_buying_power']:.2f}")

    # Build tradeable list: EOD-confirmed first, then open scan additions
    tradeable = []
    covered_tickers = set()

    if eod_signals:
        for s in eod_signals.get("tradeable", []):
            ticker = s["ticker"]
            direction = s.get("direction", "LONG")
            confirmed, reason = check_open_confirmation(ticker, direction)
            if confirmed:
                s["signal_source"] = "EOD flow"
                s["confirmation_note"] = reason
                tradeable.append(s)
                covered_tickers.add(ticker)
                log.info(f"EOD signal confirmed: {ticker} — {reason}")
            else:
                log.info(f"EOD signal skipped (gap check): {ticker} — {reason}")

    if open_signals:
        for s in open_signals.get("tradeable", []):
            if s["ticker"] not in covered_tickers:
                s["signal_source"] = "open scan"
                s["confirmation_note"] = ""
                tradeable.append(s)
                log.info(f"Open scan signal added: {s['ticker']}")

    if not tradeable:
        # Gap check rejected everything — fall back to top scoring signals ignoring gap filter
        all_sigs = []
        if eod_signals: all_sigs += eod_signals.get("tradeable", [])
        if open_signals: all_sigs += open_signals.get("tradeable", [])
        if not all_sigs:
            if eod_signals: all_sigs += eod_signals.get("signals", [])
            if open_signals: all_sigs += open_signals.get("signals", [])
        if not all_sigs:
            log.error("No signals found at all. Run signals.py first.")
            sys.exit(1)
        # Use top signals by score, gap check bypassed
        seen = set()
        for s in sorted(all_sigs, key=lambda x: x.get("score", 0), reverse=True):
            if s["ticker"] not in seen and s.get("score", 0) >= MIN_SIGNAL_SCORE:
                s["signal_source"] = "EOD flow (gap bypass)"
                s["confirmation_note"] = "gap check bypassed — all signals gap-rejected"
                tradeable.append(s)
                seen.add(s["ticker"])
            if len(tradeable) >= 3:
                break
        if not tradeable:
            watching = ", ".join(f"{s['ticker']} ({s.get('score',0):.1f})" for s in all_sigs[:5]) if all_sigs else "none"
            msg = (
                f"📊 No trades today — signals below minimum score.\n"
                f"Watching: {watching}\n"
                f"💵 Options BP: ${account['options_buying_power']:.2f}"
            )
            send_telegram(msg)
            daily_log = load_today_log()
            daily_log["notes"] = "No signals met minimum score"
            save_log(daily_log)
            return
        log.info(f"Gap bypass: using {len(tradeable)} signals after gap check rejected all")

    existing_positions = get_positions(client)
    open_count = len(existing_positions)

    if open_count >= MAX_POSITIONS:
        log.info(f"Already at max positions ({MAX_POSITIONS}). No new trades.")
        return

    slots_available = MAX_POSITIONS - open_count
    signals_to_trade = [s for s in tradeable if s.get("score", 0) >= MIN_SIGNAL_SCORE][:slots_available]

    # Check cash reserve
    available_cash = account["cash"]
    if available_cash <= CASH_RESERVE:
        msg = f"⚠️ Insufficient cash (${available_cash:.2f}) to trade — need >${CASH_RESERVE} reserve."
        log.warning(msg)
        send_telegram(msg)
        return

    spendable = available_cash - CASH_RESERVE
    log.info(f"Spendable (after ${CASH_RESERVE} reserve): ${spendable:.2f}")

    daily_log = load_today_log()
    daily_log["signals_used"] = signals_to_trade

    for signal in signals_to_trade:
        ticker = signal["ticker"]
        direction = signal.get("direction", "LONG")
        score_val = signal.get("score", 0)
        signals_fired = _signal_labels(signal)
        option_type_label = "CALL" if direction == "LONG" else "PUT"

        # Check if already have a position on this underlying
        already_held = any(ticker in p["symbol"] for p in existing_positions)
        if already_held:
            log.info(f"Already have a {ticker} options position, skipping")
            continue

        if spendable < 10:
            log.info("No more spendable cash")
            break

        # Get stock price
        stock_price = get_stock_price(data_client, ticker)
        if not stock_price:
            stock_price = signal.get("current_price")
        if not stock_price or stock_price <= 0:
            log.warning(f"No price for {ticker}, skipping")
            continue

        # Select best contract
        contract = select_option_contract(ticker, direction, stock_price)
        if not contract:
            log.warning(f"SKIPPED {ticker}: no suitable option contract found (direction={direction}, price=${stock_price:.2f}, DTE={OPTION_DTE_MIN}-{OPTION_DTE_MAX}, maxAsk=${MAX_CONTRACT_ASK})")
            continue

        # Position sizing: max $100 per position, but limited by spendable
        # Each contract = ask_per_share * 100
        cost_per_contract = contract["ask"] * 100
        if cost_per_contract < 1:
            log.info(f"Contract cost suspiciously low (${cost_per_contract:.2f}), skipping")
            continue

        max_spend = min(MAX_POSITION_COST, spendable)
        qty = max(1, int(max_spend / cost_per_contract))
        # Never spend more than MAX_POSITION_COST
        while qty > 0 and qty * cost_per_contract > MAX_POSITION_COST:
            qty -= 1

        if qty < 1:
            log.info(f"Cannot afford even 1 contract of {contract['symbol']} (${cost_per_contract:.2f}/contract)")
            continue

        total_cost = qty * cost_per_contract
        exp_date = contract["expiration_date"]
        dte = contract["dte"]
        strike = contract["strike"]
        ask_per_share = contract["ask"]

        log.info(f"Buying {qty}x {contract['symbol']} @ ${ask_per_share:.2f}/share = ${total_cost:.2f}")

        try:
            if DRY_RUN:
                log.info(f"DRY RUN: Would buy {qty}x {contract['symbol']} @ ${ask_per_share:.2f} = ${total_cost:.2f}")
                order = {"order_id": "DRY_RUN", "symbol": contract["symbol"], "qty": qty, "side": "BUY", "submitted_at": datetime.now().isoformat(), "status": "dry_run"}
            else:
                order = buy_option_contract(client, contract["symbol"], qty)

            stop_premium = round(ask_per_share * (1 + STOP_LOSS_PCT), 2)
            target_premium = round(ask_per_share * (1 + TAKE_PROFIT_PCT), 2)

            trade_record = {
                **order,
                "underlying_ticker": ticker,
                "option_type": option_type_label,
                "contract_symbol": contract["symbol"],
                "strike": strike,
                "expiration_date": exp_date,
                "dte_at_entry": dte,
                "ask_at_entry": ask_per_share,
                "qty_contracts": qty,
                "total_cost": total_cost,
                "stop_premium": stop_premium,
                "target_premium": target_premium,
                "signal_score": score_val,
                "signal_direction": direction,
                "signal_source": signal.get("signal_source", "scan"),
                "signals_fired": signals_fired,
                "top_headline": signal.get("top_headline", ""),
                "closed": False,
                "exit_price": None,
                "pnl": None,
                "pnl_pct": None
            }
            daily_log["trades"].append(trade_record)
            spendable -= total_cost
            existing_positions.append({"symbol": contract["symbol"], "market_value": total_cost})

            try:
                acct_refresh = get_account_info(client)
                cash_remaining = acct_refresh["cash"]
            except Exception:
                cash_remaining = available_cash - total_cost

            source_label = signal.get("signal_source", "scan")
            confirm_note = signal.get("confirmation_note", "")
            source_line = f"📡 {source_label}" + (f" — {confirm_note}" if confirm_note else "")
            msg = (
                f"🟢 OPTIONS BUY — {ticker} {option_type_label}\n"
                f"📋 Contract: {contract['symbol']}\n"
                f"💵 {qty} contract{'s' if qty > 1 else ''} @ ${ask_per_share:.2f}/share (${total_cost:.2f} total)\n"
                f"🎯 Strike: ${strike:.0f} | Exp: {exp_date} ({dte} DTE)\n"
                f"📊 Signal: {score_val}/10 — {signals_fired}\n"
                f"{source_line}\n"
                f"🛑 Stop: -50% (${stop_premium:.2f}/sh) | 🎯 Target: +100% (${target_premium:.2f}/sh)\n"
                f"💵 Cash remaining: ${cash_remaining:.2f}"
            )
            send_telegram(msg)
            append_trade_event({
                "type": "options_buy",
                "underlying": ticker,
                "option_type": option_type_label,
                "contract_symbol": contract["symbol"],
                "strike": strike,
                "expiration_date": exp_date,
                "dte": dte,
                "ask_per_share": ask_per_share,
                "qty_contracts": qty,
                "total_cost": total_cost,
                "stop_premium": stop_premium,
                "target_premium": target_premium,
                "score": score_val,
                "signals_fired": signals_fired,
                "cash_remaining": cash_remaining,
                "order_id": order["order_id"]
            })

            time.sleep(0.5)

        except Exception as e:
            log.error(f"Options order failed for {ticker} ({contract['symbol']}): {e}")

    save_log(daily_log)
    log.info(f"Open mode complete. {len(daily_log['trades'])} option trades placed.")


def mode_monitor():
    """Check stop loss and take profit on open options positions."""
    log.info("=== MODE: MONITOR (options) ===")
    client = get_client()

    daily_log = load_today_log()
    positions = get_positions(client)

    if not positions:
        log.info("No open positions to monitor.")
        return

    for pos in positions:
        symbol = pos["symbol"]
        plpc = pos["unrealized_plpc"]
        current_price = pos["current_price"]  # per-share option price
        unrealized_pl = pos["unrealized_pl"]
        qty = abs(pos["qty"])

        log.info(f"{symbol}: current=${current_price:.2f}/sh P&L%={plpc*100:.2f}%")

        # Find entry info from log
        entry_trade = None
        for trade in daily_log["trades"]:
            if trade.get("contract_symbol") == symbol and not trade.get("closed"):
                entry_trade = trade
                break

        # Determine option type label
        option_type_label = "CALL" if "C" in symbol else "PUT"
        # Extract underlying from symbol or trade log
        underlying = entry_trade["underlying_ticker"] if entry_trade else symbol[:4]

        should_close = False
        reason = ""
        close_type = ""

        if plpc <= STOP_LOSS_PCT:
            should_close = True
            reason = f"stop loss hit ({plpc*100:.1f}%)"
            close_type = "stop_loss"
        elif plpc >= TAKE_PROFIT_PCT:
            should_close = True
            reason = f"take profit hit ({plpc*100:.1f}%)"
            close_type = "take_profit"

        if should_close:
            log.info(f"Closing {symbol}: {reason}")
            try:
                order = sell_option_position(client, symbol, int(qty))

                entry_time = None
                if entry_trade:
                    entry_time = entry_trade.get("submitted_at")
                    entry_trade["closed"] = True
                    entry_trade["exit_price"] = current_price
                    entry_trade["close_reason"] = reason
                    entry_trade["pnl"] = unrealized_pl
                    entry_trade["pnl_pct"] = plpc * 100
                    entry_trade["closed_at"] = datetime.now().isoformat()

                held = _held_duration(entry_time) if entry_time else "?"
                pnl_sign = "+" if unrealized_pl >= 0 else "-"
                pnl_pct_sign = "+" if plpc >= 0 else "-"
                pnl_abs = abs(unrealized_pl)
                pnl_pct_abs = abs(plpc * 100)

                try:
                    acct = get_account_info(client)
                    cash = acct["cash"]
                    portfolio = acct["portfolio_value"]
                except Exception:
                    cash = portfolio = 0

                if close_type == "take_profit":
                    emoji = "✅"
                    reason_label = "TARGET HIT"
                else:
                    emoji = "🔴"
                    reason_label = "STOP HIT"

                msg = (
                    f"{emoji} OPTIONS SOLD — {underlying} {option_type_label} [{reason_label}]\n"
                    f"📋 {symbol}\n"
                    f"💵 Sold @ ${current_price:.2f}/share\n"
                    f"📊 P&L: {pnl_sign}${pnl_abs:.2f} ({pnl_pct_sign}{pnl_pct_abs:.1f}%)\n"
                    f"⏱ Held: {held}\n"
                    f"💵 Cash: ${cash:.2f} | Portfolio: ${portfolio:.2f}"
                )
                send_telegram(msg)
                append_trade_event({
                    "type": close_type,
                    "symbol": symbol,
                    "underlying": underlying,
                    "option_type": option_type_label,
                    "qty": int(qty),
                    "price": current_price,
                    "pnl": unrealized_pl,
                    "pnl_pct": plpc * 100,
                    "held": held,
                    "cash": cash,
                    "portfolio": portfolio,
                    "order_id": order["order_id"]
                })

            except Exception as e:
                log.error(f"Failed to close {symbol}: {e}")

    save_log(daily_log)


def mode_close():
    """Close ALL open options positions (EOD)."""
    log.info("=== MODE: CLOSE (options EOD) ===")
    client = get_client()

    positions = get_positions(client)
    if not positions:
        log.info("No open positions to close.")
        return

    daily_log = load_today_log()

    for pos in positions:
        symbol = pos["symbol"]
        qty = abs(pos["qty"])
        current_price = pos["current_price"]
        plpc = pos["unrealized_plpc"]
        unrealized_pl = pos["unrealized_pl"]

        # Determine option type from symbol
        option_type_label = "CALL" if "C" in symbol else "PUT"
        underlying = symbol[:4] if len(symbol) > 4 else symbol

        log.info(f"EOD closing {symbol}: {qty}x @ ${current_price:.2f} ({plpc*100:.1f}%)")

        try:
            order = sell_option_position(client, symbol, int(qty))

            for trade in daily_log["trades"]:
                if trade.get("contract_symbol") == symbol and not trade.get("closed"):
                    underlying = trade.get("underlying_ticker", underlying)
                    option_type_label = trade.get("option_type", option_type_label)
                    trade["closed"] = True
                    trade["exit_price"] = current_price
                    trade["close_reason"] = "EOD close"
                    trade["pnl"] = unrealized_pl
                    trade["pnl_pct"] = plpc * 100
                    trade["closed_at"] = datetime.now().isoformat()
                    break

            try:
                acct = get_account_info(client)
                cash = acct["cash"]
                portfolio = acct["portfolio_value"]
            except Exception:
                cash = portfolio = 0

            pnl_sign = "+" if unrealized_pl >= 0 else "-"
            pnl_pct_sign = "+" if plpc >= 0 else "-"
            pnl_abs = abs(unrealized_pl)
            pnl_pct_abs = abs(plpc * 100)

            msg = (
                f"🔔 EOD CLOSE — {underlying} {option_type_label}\n"
                f"📋 {symbol}\n"
                f"💵 Sold @ ${current_price:.2f}/share\n"
                f"📊 P&L: {pnl_sign}${pnl_abs:.2f} ({pnl_pct_sign}{pnl_pct_abs:.1f}%)\n"
                f"💵 Cash: ${cash:.2f} | Portfolio: ${portfolio:.2f}"
            )
            send_telegram(msg)
            append_trade_event({
                "type": "eod_close",
                "symbol": symbol,
                "underlying": underlying,
                "option_type": option_type_label,
                "qty": int(qty),
                "price": current_price,
                "pnl": unrealized_pl,
                "pnl_pct": plpc * 100,
                "cash": cash,
                "portfolio": portfolio,
                "order_id": order["order_id"]
            })

            time.sleep(0.5)

        except Exception as e:
            log.error(f"Failed to close {symbol}: {e}")

    save_log(daily_log)
    log.info("All options positions closed.")

    # Run learning system
    log.info("Running learn.py...")
    try:
        learn_script = os.path.join(os.path.dirname(__file__), "learn.py")
        result = subprocess.run([sys.executable, learn_script], capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            log.info("learn.py completed")
        else:
            log.warning(f"learn.py error: {result.stderr[:200]}")
    except Exception as e:
        log.error(f"Failed to run learn.py: {e}")


def main():
    parser = argparse.ArgumentParser(description="Alpaca Options Trading Bot")
    parser.add_argument("--mode", choices=["open", "monitor", "close"], required=True)
    args = parser.parse_args()

    if args.mode == "open":
        mode_open()
    elif args.mode == "monitor":
        mode_monitor()
    elif args.mode == "close":
        mode_close()


if __name__ == "__main__":
    main()
