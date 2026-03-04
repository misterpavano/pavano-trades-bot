#!/usr/bin/env python3
"""
backtest.py — Backtest signal logic using optopsy

Usage:
  python3 backtest.py --ticker AAPL --direction LONG
  python3 backtest.py --ticker MSFT --direction SHORT

Tests entry/exit logic (2% OTM, 14-21 DTE, -50% stop / +100% target)
against current live options data as a sanity check.
"""

import argparse
import logging
import sys
import os
from datetime import date, datetime

import yfinance as yf

sys.path.insert(0, os.path.dirname(__file__))
from config import STOP_LOSS_PCT, TAKE_PROFIT_PCT, OTM_PCT, OPTION_DTE_MIN, OPTION_DTE_MAX, MAX_CONTRACT_ASK

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


def find_contract(ticker: str, direction: str):
    tk = yf.Ticker(ticker)
    expirations = tk.options
    if not expirations:
        return None

    today = date.today()
    option_type = "call" if direction == "LONG" else "put"

    hist = tk.history(period="2d")
    if hist.empty:
        return None
    stock_price = hist["Close"].iloc[-1]
    target_strike = stock_price * (1 + OTM_PCT) if direction == "LONG" else stock_price * (1 - OTM_PCT)

    best, best_score = None, float("inf")
    for exp in expirations:
        exp_dt = datetime.strptime(exp, "%Y-%m-%d").date()
        dte = (exp_dt - today).days
        if not (OPTION_DTE_MIN <= dte <= OPTION_DTE_MAX):
            continue
        try:
            chain = tk.option_chain(exp)
            contracts = chain.calls if option_type == "call" else chain.puts
            for _, row in contracts.iterrows():
                strike = float(row.get("strike", 0))
                ask = float(row.get("ask") or row.get("lastPrice") or 0)
                if ask <= 0 or ask > MAX_CONTRACT_ASK:
                    continue
                score = abs(strike - target_strike) / stock_price + abs(dte - 17) / 30
                if score < best_score:
                    best_score = score
                    best = {"ticker": ticker, "direction": direction, "option_type": option_type,
                            "strike": strike, "expiration": exp, "dte": dte, "ask": ask,
                            "stock_price": stock_price}
        except Exception as e:
            log.warning(f"Chain error {exp}: {e}")
    return best


def simulate(contract: dict) -> dict:
    tk = yf.Ticker(contract["ticker"])
    entry = contract["ask"]
    stop = entry * (1 + STOP_LOSS_PCT)
    target = entry * (1 + TAKE_PROFIT_PCT)
    try:
        chain = tk.option_chain(contract["expiration"])
        contracts = chain.calls if contract["option_type"] == "call" else chain.puts
        row = contracts[abs(contracts["strike"] - contract["strike"]) < 0.50]
        current = float(row.iloc[0].get("lastPrice") or row.iloc[0].get("ask") or entry) if not row.empty else entry
    except:
        current = entry
    pnl_pct = (current - entry) / entry * 100
    outcome = "stop_loss" if current <= stop else "take_profit" if current >= target else "open"
    return {**contract, "entry": entry, "current": current, "stop": round(stop,2),
            "target": round(target,2), "pnl_pct": round(pnl_pct,1),
            "pnl_dollar": round((current - entry) * 100, 2), "outcome": outcome}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", default="AAPL")
    parser.add_argument("--direction", choices=["LONG","SHORT"], default="LONG")
    args = parser.parse_args()

    contract = find_contract(args.ticker, args.direction)
    if not contract:
        print(f"No valid contract found for {args.ticker} {args.direction}")
        sys.exit(1)

    r = simulate(contract)
    print(f"\n{'='*55}")
    print(f"BACKTEST: {r['ticker']} {r['direction']} ({r['option_type'].upper()})")
    print(f"{'='*55}")
    print(f"Stock price:   ${r['stock_price']:.2f}")
    print(f"Strike:        ${r['strike']:.2f}  |  {r['dte']} DTE  |  exp {r['expiration']}")
    print(f"Entry:         ${r['entry']:.2f}/sh")
    print(f"Stop (-50%):   ${r['stop']:.2f}/sh")
    print(f"Target (+100%): ${r['target']:.2f}/sh")
    print(f"Current:       ${r['current']:.2f}/sh")
    print(f"P&L:           {r['pnl_pct']:+.1f}%  (${r['pnl_dollar']:+.2f}/contract)")
    print(f"Outcome:       {r['outcome'].upper()}")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
