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
    TRADES_DIR, SIGNALS_FILE, SIGNALS_EOD_FILE,
    TELEGRAM_BOT_TOKEN
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
# TELEGRAM_BOT_TOKEN imported from config (reads from openclaw.json)
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


def _calc_dte(symbol: str):
    """Parse DTE from OCC option symbol (e.g. PLTR260320P00144000 → 4 days)."""
    try:
        import re
        m = re.search(r'(\d{6})[CP]', symbol)
        if not m:
            return None
        d = m.group(1)
        exp = date(2000 + int(d[0:2]), int(d[2:4]), int(d[4:6]))
        return (exp - date.today()).days
    except Exception:
        return None


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


def get_smart_money_flow(ticker: str, option_type: str, dte_min: int, dte_max: int) -> dict:
    """
    Pull options chain from yfinance and return a map of:
        strike → vol_oi_ratio
    for all expirations within the DTE window.

    vol/OI ratio >> 1 = new money piling in. That's where smart money is.
    This is the PRIMARY signal for strike selection — not OTM%, not technicals.
    """
    flow_map = {}
    try:
        tk = yf.Ticker(ticker)
        expirations = tk.options
        today_dt = date.today()

        for exp_str in expirations:
            exp_dt = datetime.strptime(exp_str, "%Y-%m-%d").date()
            dte = (exp_dt - today_dt).days
            if not (dte_min <= dte <= dte_max):
                continue

            try:
                chain = tk.option_chain(exp_str)
                df = chain.calls if option_type == "call" else chain.puts
                for _, row in df.iterrows():
                    strike = float(row["strike"])
                    vol = float(row["volume"]) if not (row["volume"] != row["volume"]) else 0
                    oi = float(row["openInterest"]) if not (row["openInterest"] != row["openInterest"]) else 0
                    if vol > 0 and oi >= 0:
                        ratio = vol / (oi + 1)
                        key = (strike, exp_str)
                        # Keep highest ratio if same strike appears multiple expirations
                        if key not in flow_map or ratio > flow_map[key]["vol_oi_ratio"]:
                            flow_map[key] = {
                                "strike": strike,
                                "expiration": exp_str,
                                "dte": dte,
                                "volume": int(vol),
                                "open_interest": int(oi),
                                "vol_oi_ratio": round(ratio, 2),
                                "last_price": float(row.get("lastPrice", 0) or 0),
                            }
            except Exception as e:
                log.debug(f"Flow fetch failed for {ticker} {exp_str}: {e}")

    except Exception as e:
        log.warning(f"Smart money flow fetch failed for {ticker}: {e}")

    return flow_map


def select_option_contract(ticker: str, direction: str, stock_price: float) -> dict | None:
    """
    Find the best options contract for a given ticker and direction.

    STRATEGY: Follow the money. We pick the strike where real volume is flooding
    in relative to open interest (vol/OI ratio). That's where smart money is
    positioned — not some arbitrary % OTM based on technicals.

    Flow score = vol/OI ratio. Higher = more new money piling into that strike.
    We pick the highest-flow strike that's affordable and within DTE window.

    Fallback: if no flow data, pick closest affordable strike to ATM.

    direction: "LONG" → call, "SHORT" → put
    Returns a dict with contract info or None if nothing suitable found.
    """
    option_type = "call" if direction == "LONG" else "put"
    today_dt = date.today()
    dte_min_dt = today_dt + timedelta(days=OPTION_DTE_MIN)
    dte_max_dt = today_dt + timedelta(days=OPTION_DTE_MAX)

    # ── Step 1: Get all tradable contracts from Alpaca ────────────────────────
    params = {
        "underlying_symbols": ticker,
        "type": option_type,
        "expiration_date_gte": dte_min_dt.isoformat(),
        "expiration_date_lte": dte_max_dt.isoformat(),
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
            log.info(f"No options contracts found for {ticker} ({option_type}, {dte_min_dt} to {dte_max_dt})")
            return None

    except Exception as e:
        log.error(f"Failed to fetch options chain for {ticker}: {e}")
        return None

    tradable = [c for c in contracts if c.get("tradable")]
    if not tradable:
        log.info(f"No tradable contracts for {ticker}")
        return None

    # Build a lookup: (strike, exp_date) → contract
    contract_lookup = {}
    for c in tradable:
        key = (float(c["strike_price"]), c["expiration_date"])
        contract_lookup[key] = c

    # ── Step 2: Pull smart money flow data from yfinance ─────────────────────
    flow_map = get_smart_money_flow(ticker, option_type, OPTION_DTE_MIN, OPTION_DTE_MAX)
    log.info(f"Flow data for {ticker}: {len(flow_map)} strikes with vol/OI data")

    # ── Step 3: Get live ask prices for all contracts ─────────────────────────
    symbols = [c["symbol"] for c in tradable[:100]]  # cap at 100 for API
    ask_prices = get_option_ask_prices(symbols)

    # ── Step 4: Score every contract by flow, filter by price ─────────────────
    candidates = []

    for c in tradable:
        strike = float(c["strike_price"])
        exp_date = c["expiration_date"]
        sym = c["symbol"]
        exp_dt = datetime.strptime(exp_date, "%Y-%m-%d").date()
        dte = (exp_dt - today_dt).days

        # Get price
        ask = ask_prices.get(sym)
        if ask is None:
            cp = c.get("close_price")
            ask = float(cp) if cp else None
        if ask is None or ask > MAX_CONTRACT_ASK:
            continue

        # Get flow score for this strike/expiry
        flow_key = (strike, exp_date)
        flow_data = flow_map.get(flow_key, {})
        vol_oi_ratio = flow_data.get("vol_oi_ratio", 0.0)
        flow_volume = flow_data.get("volume", 0)

        # Only count flow as signal if there's meaningful volume (>= 50 contracts)
        flow_score = vol_oi_ratio if flow_volume >= 50 else 0.0

        # DTE score: prefer 14-21 DTE window (sweet spot for theta/gamma balance)
        dte_score = 1.0 - abs(dte - 17) / 30.0

        candidates.append({
            "symbol": sym,
            "strike": strike,
            "expiration_date": exp_date,
            "dte": dte,
            "ask": ask,
            "type": option_type,
            "name": c.get("name", sym),
            "vol_oi_ratio": vol_oi_ratio,
            "flow_volume": flow_volume,
            "flow_score": flow_score,
            "dte_score": dte_score,
        })

    if not candidates:
        log.info(f"No affordable contracts for {ticker} ({option_type}), ask ≤ ${MAX_CONTRACT_ASK}")
        return None

    # ── Step 5: Pick the strike. Flow wins. ───────────────────────────────────
    # If we have real flow data, sort by flow score (vol/OI) descending.
    # Flow is the primary signal. DTE is tiebreaker.
    # If no meaningful flow found anywhere, fall back to ATM proximity.

    has_flow = any(c["flow_score"] > 0 for c in candidates)

    if has_flow:
        # Flow-first: weight 80% flow, 20% DTE
        def flow_first_score(c):
            return c["flow_score"] * 0.8 + c["dte_score"] * 0.2
        candidates.sort(key=flow_first_score, reverse=True)
        best = candidates[0]
        log.info(
            f"[FLOW] Selected {best['symbol']}: strike=${best['strike']}, "
            f"DTE={best['dte']}, vol/OI={best['vol_oi_ratio']:.1f}x "
            f"(vol={best['flow_volume']}), ask=${best['ask']}"
        )
    else:
        # Fallback: no flow data — pick closest to ATM with good DTE
        def atm_score(c):
            strike_diff = abs(c["strike"] - stock_price) / stock_price
            return strike_diff + (1.0 - c["dte_score"])
        candidates.sort(key=atm_score)
        best = candidates[0]
        log.info(
            f"[FALLBACK/ATM] Selected {best['symbol']}: strike=${best['strike']}, "
            f"DTE={best['dte']}, no meaningful flow data, ask=${best['ask']}"
        )

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

def mode_intraday():
    """
    Intraday check (runs at 12pm and 2pm ET): load all open positions from Alpaca,
    close any that have hit the -50% stop loss or +100% take profit target.
    """
    log.info("=== MODE: INTRADAY (stop/target check) ===")
    client = get_client()

    positions = get_positions(client)
    if not positions:
        log.info("Intraday check: no open positions.")
        return

    log.info(f"Intraday check: {len(positions)} open position(s)")
    daily_log = load_today_log()
    closed_count = 0

    for pos in positions:
        symbol = pos["symbol"]
        plpc = pos["unrealized_plpc"]
        current_price = pos["current_price"]
        unrealized_pl = pos["unrealized_pl"]
        qty = abs(pos["qty"])

        log.info(f"  {symbol}: P&L={plpc*100:+.1f}% current=${current_price:.2f}")

        # Determine option type and underlying from symbol
        option_type_label = "CALL" if "C" in symbol else "PUT"
        underlying = symbol[:4] if len(symbol) > 4 else symbol

        # Try to find entry info from today's log first
        entry_trade = None
        for trade in daily_log.get("trades", []):
            if trade.get("contract_symbol") == symbol and not trade.get("closed"):
                entry_trade = trade
                underlying = trade.get("underlying_ticker", underlying)
                option_type_label = trade.get("option_type", option_type_label)
                break

        # If not found today, scan recent logs (position may have been opened on prior day)
        if not entry_trade:
            import glob as _glob
            log_files = sorted(_glob.glob(os.path.join(TRADES_DIR, "????-??-??.json")), reverse=True)
            for lf in log_files[:5]:
                if os.path.basename(lf) == f"{TODAY}.json":
                    continue
                try:
                    with open(lf) as f:
                        old_log = json.load(f)
                    for trade in old_log.get("trades", []):
                        if trade.get("contract_symbol") == symbol and not trade.get("closed"):
                            entry_trade = trade
                            underlying = trade.get("underlying_ticker", underlying)
                            option_type_label = trade.get("option_type", option_type_label)
                            break
                    if entry_trade:
                        break
                except Exception:
                    pass

        # Evaluate stop / target
        should_close = False
        close_type = ""
        reason = ""

        if plpc <= STOP_LOSS_PCT:
            should_close = True
            close_type = "stop_loss"
            reason = f"STOP -50% hit ({plpc*100:.1f}%)"
        elif plpc >= TAKE_PROFIT_PCT:
            should_close = True
            close_type = "take_profit"
            reason = f"TARGET +100% hit ({plpc*100:.1f}%)"
        else:
            # Time-decay rule: DTE ≤ 3 and P&L ≤ -30% → cut it, time value is gone
            dte = _calc_dte(symbol)
            if dte is not None and dte <= 3 and plpc <= -0.30:
                should_close = True
                close_type = "stop_loss"
                reason = f"TIME DECAY: {dte} DTE + {plpc*100:.1f}% — cutting losses"
                log.info(f"  Time-decay rule triggered: {dte} DTE, {plpc*100:.1f}%")

        if not should_close:
            continue

        log.info(f"Closing {symbol}: {reason}")
        try:
            if DRY_RUN:
                log.info(f"DRY RUN: Would sell {qty}x {symbol} @ ${current_price:.2f}")
                order = {"order_id": "DRY_RUN", "symbol": symbol, "qty": int(qty), "side": "SELL"}
            else:
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

            try:
                acct = get_account_info(client)
                cash = acct["cash"]
                portfolio = acct["portfolio_value"]
            except Exception:
                cash = portfolio = 0

            emoji = "✅" if close_type == "take_profit" else "🔴"
            reason_label = "TARGET HIT" if close_type == "take_profit" else "STOP HIT"
            pnl_sign = "+" if unrealized_pl >= 0 else ""

            msg = (
                f"{emoji} INTRADAY CLOSE — {underlying} {option_type_label} [{reason_label}]\n"
                f"📋 {symbol}\n"
                f"💵 Sold @ ${current_price:.2f}/share\n"
                f"📊 P&L: {pnl_sign}${unrealized_pl:.2f} ({plpc*100:+.1f}%)\n"
                f"⏱ Held: {held}\n"
                f"💵 Cash: ${cash:.2f} | Portfolio: ${portfolio:.2f}"
            )
            send_telegram(msg)
            fire_trade_hook(f"CLOSE ({close_type.upper()})", f"{option_type_label} {symbol} pnl={pnl_sign}${unrealized_pl:.2f} ({plpc*100:+.1f}%)")
            append_trade_event({
                "type": f"intraday_{close_type}",
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
                "order_id": order["order_id"],
                "reason": reason
            })

            closed_count += 1
            time.sleep(0.5)

        except Exception as e:
            log.error(f"Failed to close {symbol}: {e}")

    save_log(daily_log)

    if closed_count == 0:
        log.info("Intraday check complete — no positions hit stop/target.")
    else:
        log.info(f"Intraday check complete — closed {closed_count} position(s).")





# ─── Macro Filter ──────────────────────────────────────────────────────────────

def get_macro_bias(data_client=None) -> str:
    """
    Check live SPY + QQQ direction vs previous close at execution time.
    If both are down on the day → bearish (suppress LONGs).
    If both are up on the day → bullish (suppress SHORTs).
    Returns: 'bearish', 'bullish', or 'neutral'
    """
    try:
        results = {}
        for ticker in ["SPY", "QQQ"]:
            tk = yf.Ticker(ticker)
            info = tk.fast_info
            current = info.last_price
            prev = info.previous_close
            if current and prev and prev > 0:
                pct = (current - prev) / prev
                results[ticker] = pct
                log.info(f"Macro check {ticker}: {pct*100:+.2f}% vs prev close")
        if len(results) < 2:
            return "neutral"
        if all(v < -0.005 for v in results.values()):  # both down >0.5%
            return "bearish"
        if all(v > 0.005 for v in results.values()):   # both up >0.5%
            return "bullish"
        return "neutral"
    except Exception as e:
        log.warning(f"Macro bias check failed: {e}")
        return "neutral"

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

    # ── FIX 1: Wait for opening noise to settle ───────────────────────────────
    # Markets open 9:30am ET. First 15 min = algos fighting each other.
    # Wait until 9:45am so we enter on actual price action, not the noise.
    from config import OPEN_ENTRY_DELAY_MINUTES
    try:
        import pytz
        ET = pytz.timezone("America/New_York")
    except ImportError:
        import datetime as _dt
        ET = _dt.timezone(_dt.timedelta(hours=-4))  # EDT fallback
    et_now = datetime.now(ET)
    market_open = et_now.replace(hour=9, minute=30, second=0, microsecond=0)
    entry_window = market_open + timedelta(minutes=OPEN_ENTRY_DELAY_MINUTES)
    if et_now < entry_window:
        wait_secs = max(0, (entry_window - et_now).total_seconds())
        log.info(f"⏳ Waiting {wait_secs:.0f}s for opening noise to settle (entry: 9:{30+OPEN_ENTRY_DELAY_MINUTES:02d}am ET)")
        time.sleep(wait_secs)

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

    # ── FIX 2: Macro regime filter ────────────────────────────────────────────
    # SPY + QQQ both down >0.5% = bearish tape → suppress LONG (call) signals
    # SPY + QQQ both up >0.5%   = bullish tape → suppress SHORT (put) signals
    # Neutral = no filter applied
    macro_bias = get_macro_bias(data_client)
    log.info(f"Macro regime: {macro_bias.upper()}")
    if macro_bias == "bearish":
        before = [s["ticker"] for s in tradeable]
        tradeable = [s for s in tradeable if s.get("direction") != "LONG"]
        suppressed = [t for t in before if t not in [s["ticker"] for s in tradeable]]
        if suppressed:
            log.info(f"Macro filter (BEARISH): suppressed LONG signals → {', '.join(suppressed)}")
            send_telegram(f"📉 Macro BEARISH — suppressed {len(suppressed)} LONG signal(s): {', '.join(suppressed)}")
    elif macro_bias == "bullish":
        before = [s["ticker"] for s in tradeable]
        tradeable = [s for s in tradeable if s.get("direction") != "SHORT"]
        suppressed = [t for t in before if t not in [s["ticker"] for s in tradeable]]
        if suppressed:
            log.info(f"Macro filter (BULLISH): suppressed SHORT signals → {', '.join(suppressed)}")
            send_telegram(f"📈 Macro BULLISH — suppressed {len(suppressed)} SHORT signal(s): {', '.join(suppressed)}")

    if not tradeable:
        # Gap check rejected everything — do NOT bypass. No trade is the right trade.
        all_sigs = []
        if eod_signals: all_sigs += eod_signals.get("tradeable", [])
        if open_signals: all_sigs += open_signals.get("tradeable", [])
        if not all_sigs:
            if eod_signals: all_sigs += eod_signals.get("signals", [])
            if open_signals: all_sigs += open_signals.get("signals", [])

        watching = ", ".join(f"{s['ticker']} ({s.get('score',0):.1f})" for s in all_sigs[:5]) if all_sigs else "none"
        msg = (
            f"📊 No trades today — gap check rejected all signals.\n"
            f"Watching: {watching}\n"
            f"💵 Options BP: ${account['options_buying_power']:.2f}"
        )
        log.info("Gap check rejected all signals — no trades (bypass removed)")
        send_telegram(msg)
        daily_log = load_today_log()
        daily_log["notes"] = "Gap check rejected all signals — no trades"
        save_log(daily_log)
        return

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
        conviction_flag = signal.get("conviction_flag")

        # conviction_flag is the strongest signal — if it contradicts direction, override
        if conviction_flag == "HIGH_CONVICTION_SHORT" and direction != "SHORT":
            log.warning(f"{signal['ticker']}: conviction_flag=HIGH_CONVICTION_SHORT overrides direction={direction} → SHORT")
            direction = "SHORT"
        elif conviction_flag == "HIGH_CONVICTION_LONG" and direction != "LONG":
            log.warning(f"{signal['ticker']}: conviction_flag=HIGH_CONVICTION_LONG overrides direction={direction} → LONG")
            direction = "LONG"

        score_val = signal.get("score", 0)
        signals_fired = _signal_labels(signal)
        option_type_label = "CALL" if direction == "LONG" else "PUT"

        # ── Premium sanity gate ───────────────────────────────────────────────
        # If we're going LONG (calls) but put premium >> call premium,
        # smart money is actually buying protection/downside. Don't fight it.
        # Same logic inverted for SHORT (puts) when calls dominate.
        call_prem = signal.get("call_premium_est", 0) or 0
        put_prem = signal.get("put_premium_est", 0) or 0
        if direction == "LONG" and put_prem > call_prem and (call_prem + put_prem) > 50_000:
            log.info(f"SKIPPED {ticker}: LONG signal but put premium (${put_prem:,.0f}) > call premium (${call_prem:,.0f}) — flow contradiction")
            send_telegram(f"⚠️ Skipped {ticker} — LONG signal but put $ (${put_prem/1e6:.1f}M) beats call $ (${call_prem/1e6:.1f}M). Flow says no.")
            continue
        if direction == "SHORT" and call_prem > put_prem and (call_prem + put_prem) > 50_000:
            log.info(f"SKIPPED {ticker}: SHORT signal but call premium (${call_prem:,.0f}) > put premium (${put_prem:,.0f}) — flow contradiction")
            send_telegram(f"⚠️ Skipped {ticker} — SHORT signal but call $ (${call_prem/1e6:.1f}M) beats put $ (${put_prem/1e6:.1f}M). Flow says no.")
            continue

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
            time.sleep(2)  # wait for Alpaca to settle before cash refresh

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
            fire_trade_hook("BUY", f"{option_type_label} {ticker} strike={strike} exp={exp_date} qty={qty} @${ask_per_share:.2f}/sh")
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


def _parse_option_symbol(symbol: str) -> dict:
    """
    Parse an OCC option symbol like NVDA260318P00175000.
    Returns dict with: underlying, expiry (date), option_type, strike (float)
    """
    import re
    m = re.match(r"^([A-Z]+)(\d{6})([CP])(\d{8})$", symbol)
    if not m:
        return {}
    underlying, exp_str, opt_type, strike_str = m.groups()
    expiry = date(2000 + int(exp_str[:2]), int(exp_str[2:4]), int(exp_str[4:6]))
    strike = int(strike_str) / 1000.0
    return {
        "underlying": underlying,
        "expiry": expiry,
        "option_type": "CALL" if opt_type == "C" else "PUT",
        "strike": strike,
        "dte": (expiry - date.today()).days,
    }


def _get_underlying_price(ticker: str) -> float | None:
    """Fetch current (or last close) price for an underlying via yfinance."""
    try:
        info = yf.Ticker(ticker).fast_info
        price = getattr(info, "last_price", None) or getattr(info, "previous_close", None)
        return float(price) if price else None
    except Exception:
        return None


def _find_trade_record(symbol: str, daily_log: dict) -> tuple[dict | None, dict | None, str | None]:
    """
    Search today's log and up to 5 prior-day logs for a trade record matching symbol.
    Returns (trade_record, log_dict, log_filepath).
    log_filepath is None for today's log.
    """
    import glob as _glob
    for trade in daily_log["trades"]:
        if trade.get("contract_symbol") == symbol and not trade.get("closed"):
            return trade, daily_log, None

    log_files = sorted(
        _glob.glob(os.path.join(TRADES_DIR, "????-??-??.json")), reverse=True
    )
    for lf in log_files[:5]:
        if os.path.basename(lf) == f"{TODAY}.json":
            continue
        try:
            with open(lf) as f:
                prior_log = json.load(f)
            for trade in prior_log.get("trades", []):
                if trade.get("contract_symbol") == symbol and not trade.get("closed"):
                    return trade, prior_log, lf
        except Exception:
            pass
    return None, None, None


def evaluate_eod_position(pos: dict, trade_record: dict | None) -> tuple[bool, str, str]:
    """
    Decide whether to close or hold a position at EOD.

    Returns:
        (should_close: bool, close_type: str, reasoning: str)

    close_type values: "stop_loss" | "take_profit" | "dte_risk" |
                       "thesis_broken" | "eod_forced" | "hold"
    """
    symbol = pos["symbol"]
    plpc = pos["unrealized_plpc"]        # e.g. -0.33 = -33%
    unrealized_pl = pos["unrealized_pl"]
    current_price = pos["current_price"]

    parsed = _parse_option_symbol(symbol)
    option_type = parsed.get("option_type", "CALL" if "C" in symbol else "PUT")
    dte = parsed.get("dte", 0)
    underlying_ticker = parsed.get("underlying", symbol[:4])
    strike = parsed.get("strike", 0)

    # Pull extra context from trade record if available
    signal_score = trade_record.get("signal_score", "?") if trade_record else "?"
    signal_direction = trade_record.get("signal_direction", "") if trade_record else ""
    top_headline = trade_record.get("top_headline", "") if trade_record else ""
    dte_at_entry = trade_record.get("dte_at_entry", dte) if trade_record else dte

    # Fetch current underlying price
    stock_price = _get_underlying_price(underlying_ticker)

    # --- HARD CLOSES (non-negotiable) ---

    if plpc <= -0.50:
        reasoning = (
            f"Stop-loss triggered at {plpc*100:.1f}%. Premium cut in half — "
            f"the market has spoken against this thesis. Signal score was {signal_score}/10."
        )
        if top_headline:
            reasoning += f" Original catalyst: \"{top_headline}\""
        return True, "stop_loss", reasoning

    if plpc >= 1.00:
        reasoning = (
            f"100% profit target hit at +{plpc*100:.1f}%. "
            f"Taking the double as planned — that's the game."
        )
        return True, "take_profit", reasoning

    if dte <= 2:
        reasoning = (
            f"Only {dte} DTE remaining. Theta decay is brutal inside 2 days — "
            f"the contract loses time value faster than the stock can move in our favor. "
            f"Closing to recover whatever premium is left (${current_price:.2f}/share)."
        )
        return True, "dte_risk", reasoning

    # --- FLOW CONTRADICTION CHECK ---
    # If smart money flow is pointing the opposite direction from our position,
    # we don't wait for -50%. Flow is the primary signal — contradicting flow
    # means the market is telling us we're wrong. Cut early, redeploy smarter.
    #
    # Rule: if the top flow strike is more than $10 away from our strike
    # AND vol/OI at our strike is < 0.5x (nobody buying our level),
    # AND we're already down > 15%, that's a flow contradiction — exit.
    if plpc <= -0.15 and dte >= 3:
        try:
            flow_map = get_smart_money_flow(underlying_ticker, option_type.lower(), 1, 45)
            real_flow = [f for f in flow_map.values() if f["volume"] >= 50]
            if real_flow:
                real_flow.sort(key=lambda x: x["vol_oi_ratio"], reverse=True)
                top_flow_strike = real_flow[0]["strike"]
                top_flow_ratio = real_flow[0]["vol_oi_ratio"]

                # Check vol/OI at our exact strike
                our_flow = flow_map.get((strike, parsed.get("expiry", exp_date if isinstance(exp_date := parsed.get("expiry"), str) else parsed.get("expiry", date.today()).isoformat())), {})
                our_ratio = our_flow.get("vol_oi_ratio", 0.0)

                strike_gap = abs(top_flow_strike - strike)

                if strike_gap >= 10 and our_ratio < 0.5 and top_flow_ratio >= 3.0:
                    reasoning = (
                        f"Flow contradiction: smart money is at ${top_flow_strike:.0f} "
                        f"({top_flow_ratio:.1f}x vol/OI) — our ${strike:.0f} strike has "
                        f"{our_ratio:.2f}x vol/OI (nobody's buying our level). "
                        f"Gap of ${strike_gap:.0f} between flow and our strike. "
                        f"Down {plpc*100:.1f}% — cutting before the market takes more. "
                        f"Flow is the signal. We're on the wrong side of it."
                    )
                    return True, "flow_contradiction", reasoning
        except Exception as e:
            log.warning(f"Flow contradiction check failed for {symbol}: {e}")

    # --- THESIS CHECK (requires underlying price) ---

    if stock_price:
        otm_pct = (stock_price - strike) / stock_price  # positive = stock above strike

        if option_type == "CALL":
            # Thesis: stock goes UP. Bearish if stock is meaningfully below entry strike area.
            if otm_pct < -0.05:
                # Stock is more than 5% below strike — deeply OTM, thesis challenged
                reasoning = (
                    f"{underlying_ticker} is at ${stock_price:.2f}, which is "
                    f"{abs(otm_pct)*100:.1f}% below our ${strike:.0f} strike. "
                    f"The call is deeply OTM with {dte} DTE. "
                    f"Bullish thesis not supported by current price action — closing."
                )
                return True, "thesis_broken", reasoning
        else:
            # PUT — thesis: stock goes DOWN. Challenged if stock is well above strike.
            if otm_pct > 0.10:
                # Stock is more than 10% above strike — deeply OTM, thesis challenged
                reasoning = (
                    f"{underlying_ticker} is at ${stock_price:.2f}, which is "
                    f"{otm_pct*100:.1f}% above our ${strike:.0f} put strike. "
                    f"Bearish thesis not supported — stock is moving against the position. Closing."
                )
                return True, "thesis_broken", reasoning

    # --- HOLD DECISION ---

    days_remaining = dte
    days_held = dte_at_entry - dte

    if stock_price and option_type == "CALL":
        price_context = (
            f"{underlying_ticker} at ${stock_price:.2f} vs ${strike:.0f} strike "
            f"({'ITM' if stock_price > strike else f'{abs((stock_price-strike)/stock_price)*100:.1f}% OTM'})."
        )
    elif stock_price and option_type == "PUT":
        price_context = (
            f"{underlying_ticker} at ${stock_price:.2f} vs ${strike:.0f} strike "
            f"({'ITM' if stock_price < strike else f'{abs((stock_price-strike)/stock_price)*100:.1f}% OTM'})."
        )
    else:
        price_context = f"Strike: ${strike:.0f}."

    reasoning = (
        f"Holding. {price_context} "
        f"{days_remaining} DTE remaining (held {days_held} day{'s' if days_held != 1 else ''}). "
        f"P&L: {plpc*100:+.1f}% — within acceptable range, no stop triggered. "
        f"Signal score was {signal_score}/10"
    )
    if top_headline:
        reasoning += f". Original catalyst still valid: \"{top_headline}\""
    reasoning += ". Letting the thesis run."

    return False, "hold", reasoning


def mode_close():
    """
    EOD position review — close positions where thesis is broken or risk rules
    are triggered; hold positions where the signal remains intact.
    """
    log.info("=== MODE: CLOSE (options EOD review) ===")
    client = get_client()

    positions = get_positions(client)
    if not positions:
        log.info("No open positions to close.")
        return

    daily_log = load_today_log()
    import glob as _glob

    held_positions = []
    closed_count = 0

    for pos in positions:
        symbol = pos["symbol"]
        qty = abs(pos["qty"])
        current_price = pos["current_price"]
        plpc = pos["unrealized_plpc"]
        unrealized_pl = pos["unrealized_pl"]

        parsed = _parse_option_symbol(symbol)
        option_type_label = parsed.get("option_type", "CALL" if "C" in symbol else "PUT")
        underlying = parsed.get("underlying", symbol[:4])

        # Find trade record across logs
        trade_record, trade_log, trade_lf = _find_trade_record(symbol, daily_log)

        # Evaluate: close or hold?
        should_close, close_type, reasoning = evaluate_eod_position(pos, trade_record)

        log.info(f"EOD eval {symbol}: {'CLOSE' if should_close else 'HOLD'} ({close_type}) — {reasoning[:80]}...")

        if not should_close:
            held_positions.append({
                "symbol": symbol,
                "underlying": underlying,
                "option_type": option_type_label,
                "plpc": plpc,
                "reasoning": reasoning,
            })
            # Log the hold decision
            if trade_record is not None:
                trade_record["eod_hold_reason"] = reasoning
                trade_record["eod_hold_date"] = TODAY
                if trade_lf:
                    try:
                        with open(trade_lf, "w") as f:
                            json.dump(trade_log, f, indent=2)
                    except Exception as e:
                        log.warning(f"Could not update prior log for hold: {e}")
            save_log(daily_log)
            continue

        # Execute the close
        try:
            order = sell_option_position(client, symbol, int(qty))

            # Update trade record
            if trade_record is not None:
                trade_record["closed"] = True
                trade_record["exit_price"] = current_price
                trade_record["close_reason"] = close_type
                trade_record["close_reasoning"] = reasoning
                trade_record["pnl"] = unrealized_pl
                trade_record["pnl_pct"] = plpc * 100
                trade_record["closed_at"] = datetime.now().isoformat()
                if trade_lf:
                    try:
                        with open(trade_lf, "w") as f:
                            json.dump(trade_log, f, indent=2)
                        log.info(f"Updated prior-day log {os.path.basename(trade_lf)} for {symbol}")
                    except Exception as e:
                        log.warning(f"Could not update prior log {trade_lf}: {e}")
            else:
                log.warning(f"No trade record found for {symbol} in today's or recent logs")

            try:
                acct = get_account_info(client)
                cash = acct["cash"]
                portfolio = acct["portfolio_value"]
            except Exception:
                cash = portfolio = 0

            pnl_sign = "+" if unrealized_pl >= 0 else "-"
            pnl_abs = abs(unrealized_pl)
            pnl_pct_abs = abs(plpc * 100)

            # Map close type to emoji + label
            close_labels = {
                "stop_loss":         ("🔴", "STOP HIT"),
                "take_profit":       ("✅", "TARGET HIT"),
                "dte_risk":          ("⏰", "DTE RISK"),
                "thesis_broken":     ("❌", "THESIS BROKEN"),
                "flow_contradiction":("🌊", "FLOW CONTRADICTION"),
                "eod_forced":        ("🔔", "EOD CLOSE"),
            }
            emoji, label = close_labels.get(close_type, ("🔔", "EOD CLOSE"))

            msg = (
                f"{emoji} {label} — {underlying} {option_type_label}\n"
                f"📋 {symbol}\n"
                f"💵 Sold @ ${current_price:.2f}/share\n"
                f"📊 P&L: {pnl_sign}${pnl_abs:.2f} ({pnl_sign}{pnl_pct_abs:.1f}%)\n"
                f"💡 Why: {reasoning}\n"
                f"💵 Cash: ${cash:.2f} | Portfolio: ${portfolio:.2f}"
            )
            send_telegram(msg)
            fire_trade_hook(label, f"{option_type_label} {symbol} pnl={pnl_sign}${pnl_abs:.2f} ({pnl_sign}{pnl_pct_abs:.1f}%) reason={close_type}")
            append_trade_event({
                "type": "eod_close",
                "close_type": close_type,
                "reasoning": reasoning,
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

            closed_count += 1
            time.sleep(0.5)

        except Exception as e:
            log.error(f"Failed to close {symbol}: {e}")

    save_log(daily_log)

    # Send held-positions summary if any
    if held_positions:
        held_lines = []
        for h in held_positions:
            held_lines.append(
                f"  {h['underlying']} {h['option_type']} ({h['plpc']*100:+.1f}%): {h['reasoning']}"
            )
        hold_msg = (
            f"📌 HOLDING {len(held_positions)} position(s) overnight — thesis intact:\n"
            + "\n".join(held_lines)
        )
        send_telegram(hold_msg)

    if closed_count == 0 and not held_positions:
        log.info("No open positions found.")
    else:
        log.info(f"EOD review complete — closed {closed_count}, held {len(held_positions)}.")

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


# --- Pavano Hook Integration ---
import urllib.request as _urllib_req
import urllib.parse as _urllib_parse
import json as _json

def fire_trade_hook(event_type: str, details: str):
    """Fire an isolated agent run via OpenClaw webhook ingress on trade events."""
    try:
        msg = f"[Trade Event] {event_type}: {details}\n\nLog this trade event to the Pavano Trades Telegram group (-5191423233) with appropriate emoji. Keep it brief — one line max."
        payload = {
            "message": msg,
            "name": "TradeFill",
            "wakeMode": "now",
            "deliver": True,
            "channel": "telegram",
            "to": "-5191423233",
            "model": "anthropic/claude-haiku-4-5"
        }
        data = _json.dumps(payload).encode()
        req = _urllib_req.Request(
            "http://127.0.0.1:18789/hooks/agent",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer pavano-hook-token-2026"
            }
        )
        _urllib_req.urlopen(req, timeout=5)
        log.info(f"fire_trade_hook: sent {event_type}")
    except Exception as e:
        log.warning(f"fire_trade_hook failed (non-critical): {e}")


def main():
    parser = argparse.ArgumentParser(description="Alpaca Options Trading Bot")
    parser.add_argument("--mode", choices=["open", "close", "intraday"], required=True)
    args = parser.parse_args()

    if args.mode == "open":
        mode_open()
    elif args.mode == "close":
        mode_close()
    elif args.mode == "intraday":
        mode_intraday()


if __name__ == "__main__":
    main()
