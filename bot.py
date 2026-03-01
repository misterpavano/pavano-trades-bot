#!/usr/bin/env python3
"""
bot.py — Main trading bot
Usage:
  python3 bot.py --mode open     # Execute trades at market open
  python3 bot.py --mode monitor  # Check SL/TP on open positions
  python3 bot.py --mode close    # Close all positions (EOD)
"""

import argparse
import json
import logging
import os
import sys
import subprocess
import time
from datetime import datetime, date

sys.path.insert(0, os.path.dirname(__file__))
from config import (
    ALPACA_KEY, ALPACA_SECRET, ALPACA_BASE_URL,
    STARTING_CAPITAL, MAX_POSITIONS, POSITION_SIZE_MIN, POSITION_SIZE_MAX,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT, TRADES_DIR, SIGNALS_FILE, MIN_SIGNAL_SCORE
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


def get_client():
    return TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=True)


def get_data_client():
    return StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)


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


def get_account_info(client):
    account = client.get_account()
    return {
        "equity": float(account.equity),
        "cash": float(account.cash),
        "buying_power": float(account.buying_power),
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


def get_current_price(data_client, symbol: str) -> float:
    try:
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        quote = data_client.get_stock_latest_quote(req)
        q = quote[symbol]
        return (q.ask_price + q.bid_price) / 2
    except Exception as e:
        log.warning(f"Could not get quote for {symbol}: {e}")
        return None


def submit_market_order(client, symbol: str, qty: float, side: str) -> dict:
    order_side = OrderSide.BUY if side == "BUY" else OrderSide.SELL
    req = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=order_side,
        time_in_force=TimeInForce.DAY
    )
    order = client.submit_order(req)
    log.info(f"Order submitted: {side} {qty} {symbol} — ID={order.id}")
    return {
        "order_id": str(order.id),
        "symbol": symbol,
        "qty": qty,
        "side": side,
        "submitted_at": datetime.now().isoformat(),
        "status": str(order.status)
    }


def mode_open():
    """Load signals and execute trades at market open."""
    log.info("=== MODE: OPEN ===")
    client = get_client()
    data_client = get_data_client()

    # Load signals
    if not os.path.exists(SIGNALS_FILE):
        log.error(f"No signals file found at {SIGNALS_FILE}. Run signals.py first.")
        sys.exit(1)

    with open(SIGNALS_FILE) as f:
        signal_data = json.load(f)

    tradeable = signal_data.get("tradeable", [])
    if not tradeable:
        log.info("No tradeable signals today. No trades placed.")
        daily_log = load_today_log()
        daily_log["notes"] = "No tradeable signals"
        save_log(daily_log)
        return

    account = get_account_info(client)
    log.info(f"Account: equity=${account['equity']:.2f} cash=${account['cash']:.2f}")

    # Hard cap: never exceed $500 total exposure
    max_exposure = min(STARTING_CAPITAL, account["equity"])
    available_cash = min(account["cash"], max_exposure)

    existing_positions = get_positions(client)
    open_count = len(existing_positions)

    if open_count >= MAX_POSITIONS:
        log.info(f"Already at max positions ({MAX_POSITIONS}). No new trades.")
        return

    slots_available = MAX_POSITIONS - open_count
    signals_to_trade = [s for s in tradeable if s["score"] >= MIN_SIGNAL_SCORE][:slots_available]

    daily_log = load_today_log()
    daily_log["signals_used"] = signals_to_trade

    for signal in signals_to_trade:
        symbol = signal["ticker"]
        direction = signal["direction"]

        if direction != "LONG":
            log.info(f"Skipping {symbol} — SHORT signals not supported (paper account, no shorting for now)")
            continue

        # Check if already holding this symbol
        already_held = any(p["symbol"] == symbol for p in existing_positions)
        if already_held:
            log.info(f"Already holding {symbol}, skipping")
            continue

        # Calculate position size (10-25% of available cash)
        position_pct = POSITION_SIZE_MIN + (POSITION_SIZE_MAX - POSITION_SIZE_MIN) * (signal["score"] - 5) / 5
        position_pct = min(POSITION_SIZE_MAX, max(POSITION_SIZE_MIN, position_pct))
        position_dollars = available_cash * position_pct

        # Don't exceed remaining headroom under $500 cap
        current_exposure = sum(p["market_value"] for p in existing_positions)
        max_this_trade = min(position_dollars, STARTING_CAPITAL - current_exposure)

        if max_this_trade < 10:
            log.info(f"Insufficient headroom for {symbol} (${max_this_trade:.2f})")
            continue

        # Get current price
        price = get_current_price(data_client, symbol)
        if not price or price <= 0:
            price = signal.get("current_price")
        if not price or price <= 0:
            log.warning(f"No price for {symbol}, skipping")
            continue

        qty = int(max_this_trade / price)
        if qty < 1:
            log.info(f"Position too small for {symbol} (${max_this_trade:.2f} @ ${price:.2f})")
            continue

        actual_cost = qty * price
        log.info(f"Buying {qty} {symbol} @ ~${price:.2f} = ${actual_cost:.2f} ({position_pct*100:.0f}% of cash)")

        try:
            order = submit_market_order(client, symbol, qty, "BUY")
            trade_record = {
                **order,
                "signal_score": signal["score"],
                "signal_direction": direction,
                "entry_price": price,
                "planned_qty": qty,
                "planned_cost": actual_cost,
                "stop_loss_price": round(price * (1 + STOP_LOSS_PCT), 2),
                "take_profit_price": round(price * (1 + TAKE_PROFIT_PCT), 2),
                "top_headline": signal.get("top_headline", ""),
                "closed": False,
                "exit_price": None,
                "pnl": None,
                "pnl_pct": None
            }
            daily_log["trades"].append(trade_record)
            available_cash -= actual_cost
            existing_positions.append({"symbol": symbol, "market_value": actual_cost})
            time.sleep(0.5)
        except Exception as e:
            log.error(f"Order failed for {symbol}: {e}")

    save_log(daily_log)
    log.info(f"Open mode complete. {len(daily_log['trades'])} trades placed.")


def mode_monitor():
    """Check stop loss and take profit on open positions."""
    log.info("=== MODE: MONITOR ===")
    client = get_client()
    data_client = get_data_client()

    daily_log = load_today_log()
    positions = get_positions(client)

    if not positions:
        log.info("No open positions to monitor.")
        return

    for pos in positions:
        symbol = pos["symbol"]
        plpc = pos["unrealized_plpc"]
        current_price = pos["current_price"]

        log.info(f"{symbol}: current=${current_price:.2f} P&L%={plpc*100:.2f}%")

        should_close = False
        reason = ""

        if plpc <= STOP_LOSS_PCT:
            should_close = True
            reason = f"stop loss hit ({plpc*100:.2f}%)"
        elif plpc >= TAKE_PROFIT_PCT:
            should_close = True
            reason = f"take profit hit ({plpc*100:.2f}%)"

        if should_close:
            log.info(f"Closing {symbol}: {reason}")
            try:
                qty = abs(pos["qty"])
                order = submit_market_order(client, symbol, qty, "SELL")

                # Update trade log
                for trade in daily_log["trades"]:
                    if trade["symbol"] == symbol and not trade["closed"]:
                        trade["closed"] = True
                        trade["exit_price"] = current_price
                        trade["close_reason"] = reason
                        trade["pnl"] = pos["unrealized_pl"]
                        trade["pnl_pct"] = plpc * 100
                        trade["closed_at"] = datetime.now().isoformat()
                        break
            except Exception as e:
                log.error(f"Failed to close {symbol}: {e}")

    save_log(daily_log)


def mode_close():
    """Close ALL open positions (EOD)."""
    log.info("=== MODE: CLOSE ===")
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

        log.info(f"Closing {symbol}: {qty} shares @ ${current_price:.2f} ({plpc*100:.2f}%)")
        try:
            order = submit_market_order(client, symbol, qty, "SELL")

            for trade in daily_log["trades"]:
                if trade["symbol"] == symbol and not trade["closed"]:
                    trade["closed"] = True
                    trade["exit_price"] = current_price
                    trade["close_reason"] = "EOD close"
                    trade["pnl"] = pos["unrealized_pl"]
                    trade["pnl_pct"] = plpc * 100
                    trade["closed_at"] = datetime.now().isoformat()
                    break
            time.sleep(0.5)
        except Exception as e:
            log.error(f"Failed to close {symbol}: {e}")

    save_log(daily_log)
    log.info("All positions closed.")

    # Run learning system to record outcomes
    log.info("Running learn.py to record today's learnings...")
    try:
        learn_script = os.path.join(os.path.dirname(__file__), "learn.py")
        result = subprocess.run([sys.executable, learn_script], capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            log.info("learn.py completed successfully")
        else:
            log.warning(f"learn.py returned non-zero: {result.stderr[:200]}")
    except Exception as e:
        log.error(f"Failed to run learn.py: {e}")


def main():
    parser = argparse.ArgumentParser(description="Alpaca Paper Trading Bot")
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
