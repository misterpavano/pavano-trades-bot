#!/usr/bin/env python3
"""
politicians.py — Congressional trade tracker
Fetches House & Senate stock disclosures and scores tickers with recent large purchases.

Data sources (tried in order):
  1. House Stock Watcher S3 (may 403 from VPS/cloud IPs — works from residential)
  2. Senate Stock Watcher S3 (same)

NOTE: If running from a cloud server, these S3 buckets may return 403.
The script handles this gracefully — check knowledge/politicians/latest.json for results.
"""

import json
import logging
import os
from datetime import datetime, date

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
POLITICIANS_DIR = os.path.join(BASE_DIR, "knowledge", "politicians")
LATEST_FILE = os.path.join(POLITICIANS_DIR, "latest.json")
HISTORY_FILE = os.path.join(POLITICIANS_DIR, "history.json")

WATCHLIST = {"SPY", "QQQ", "AAPL", "TSLA", "NVDA", "AMD", "MSFT", "META", "GME", "AMZN"}
LARGE_PURCHASE_THRESHOLD = 50_000
LOOKBACK_DAYS = 30

# Data sources
HOUSE_URLS = [
    "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json",
]
SENATE_URLS = [
    "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/all_transactions.json",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://housestockwatcher.com/",
}


def parse_amount(amount_str):
    if not amount_str:
        return 0
    amount_str = amount_str.replace(",", "").replace("$", "").strip()
    if "-" in amount_str:
        parts = amount_str.split("-")
        try:
            lo = int(parts[0].strip())
            hi = int(parts[1].strip())
            return (lo + hi) // 2
        except Exception:
            pass
    try:
        return int(amount_str.split()[0])
    except Exception:
        return 0


def score_amount(amount):
    if amount >= 250_000:
        return 3
    elif amount >= 100_000:
        return 2
    elif amount >= 50_000:
        return 1
    return 0


def recency_score(tx_date):
    days_ago = (date.today() - tx_date).days
    if days_ago <= 7:
        return 1.5
    elif days_ago <= 14:
        return 1.0
    elif days_ago <= 30:
        return 0.5
    return 0.0


def make_tx_id(name, ticker, tx_date):
    return f"{name.lower().replace(' ', '_')}_{ticker}_{tx_date}"


def fetch_json(urls, label):
    for url in urls:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            if resp.status_code == 403:
                log.warning(f"{label}: 403 Forbidden from {url} — S3 bucket may block cloud/VPS IPs")
                continue
            resp.raise_for_status()
            data = resp.json()
            log.info(f"{label}: {len(data)} total records from {url}")
            return data
        except Exception as e:
            log.error(f"{label} fetch error ({url}): {e}")
    return []


def normalize_house(tx):
    try:
        tx_type = (tx.get("type") or tx.get("transaction_type") or "").lower()
        if "purchase" not in tx_type and "buy" not in tx_type:
            return None
        ticker = (tx.get("ticker") or "").upper().strip()
        if not ticker or ticker == "--":
            return None
        date_str = tx.get("transaction_date") or tx.get("disclosure_date") or ""
        if not date_str:
            return None
        tx_date = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        if (date.today() - tx_date).days > LOOKBACK_DAYS:
            return None
        name = tx.get("representative") or tx.get("name") or "Unknown"
        amount_str = tx.get("amount") or ""
        amount = parse_amount(amount_str)
        return {
            "name": name, "party": tx.get("party", ""), "chamber": "House",
            "ticker": ticker, "date": str(tx_date),
            "amount_str": amount_str, "amount": amount,
            "tx_id": make_tx_id(name, ticker, str(tx_date))
        }
    except Exception:
        return None


def normalize_senate(tx):
    try:
        tx_type = (tx.get("type") or tx.get("transaction_type") or "").lower()
        if "purchase" not in tx_type and "buy" not in tx_type:
            return None
        ticker = (tx.get("ticker") or "").upper().strip()
        if not ticker or ticker == "--":
            return None
        date_str = tx.get("transaction_date") or tx.get("date") or ""
        if not date_str:
            return None
        tx_date = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        if (date.today() - tx_date).days > LOOKBACK_DAYS:
            return None
        name = tx.get("senator") or tx.get("name") or "Unknown"
        amount_str = tx.get("amount") or ""
        amount = parse_amount(amount_str)
        return {
            "name": name, "party": tx.get("party", ""), "chamber": "Senate",
            "ticker": ticker, "date": str(tx_date),
            "amount_str": amount_str, "amount": amount,
            "tx_id": make_tx_id(name, ticker, str(tx_date))
        }
    except Exception:
        return None


def aggregate_signals(transactions):
    by_ticker = {}
    for tx in transactions:
        ticker = tx["ticker"]
        if ticker in WATCHLIST or tx["amount"] >= LARGE_PURCHASE_THRESHOLD:
            by_ticker.setdefault(ticker, []).append(tx)

    results = []
    for ticker, txs in by_ticker.items():
        total_score = 0.0
        for tx in txs:
            total_score += score_amount(tx["amount"])
            tx_date = datetime.strptime(tx["date"], "%Y-%m-%d").date()
            total_score += recency_score(tx_date)
        if len(txs) > 1:
            total_score += len(txs) - 1
        total_score = min(3.0, total_score)

        results.append({
            "ticker": ticker,
            "score": round(total_score, 2),
            "politicians": [{
                "name": tx["name"], "party": tx["party"], "chamber": tx["chamber"],
                "amount": tx["amount_str"], "date": tx["date"]
            } for tx in sorted(txs, key=lambda x: x["date"], reverse=True)],
            "transaction_count": len(txs),
            "scanned_at": datetime.now().isoformat()
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def update_history(transactions):
    os.makedirs(POLITICIANS_DIR, exist_ok=True)
    existing = []
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE) as f:
                existing = json.load(f)
        except Exception:
            existing = []

    existing_ids = {tx["tx_id"] for tx in existing}
    new_txs = [tx for tx in transactions if tx["tx_id"] not in existing_ids]
    if new_txs:
        log.info(f"Adding {len(new_txs)} new transactions to history")
        existing.extend(new_txs)
        with open(HISTORY_FILE, "w") as f:
            json.dump(existing, f, indent=2)


def main():
    os.makedirs(POLITICIANS_DIR, exist_ok=True)

    house_raw = fetch_json(HOUSE_URLS, "House")
    senate_raw = fetch_json(SENATE_URLS, "Senate")

    all_normalized = []
    for tx in house_raw:
        n = normalize_house(tx)
        if n:
            all_normalized.append(n)
    for tx in senate_raw:
        n = normalize_senate(tx)
        if n:
            all_normalized.append(n)

    log.info(f"Normalized {len(all_normalized)} purchase transactions (last {LOOKBACK_DAYS} days)")

    if not all_normalized:
        log.warning(
            "No transactions fetched. If running from a VPS, the S3 data sources may be "
            "blocking cloud IPs. The cron will retry tomorrow. Signal scoring will use "
            "options+news only until politician data is available."
        )
        signals = []
    else:
        signals = aggregate_signals(all_normalized)
        update_history(all_normalized)

    output = {
        "generated_at": datetime.now().isoformat(),
        "lookback_days": LOOKBACK_DAYS,
        "total_transactions": len(all_normalized),
        "tickers_flagged": len(signals),
        "fetch_note": "S3 sources may block cloud IPs; data from residential IPs only" if not all_normalized else "ok",
        "signals": signals
    }

    with open(LATEST_FILE, "w") as f:
        json.dump(output, f, indent=2)

    log.info(f"Saved {len(signals)} signals to {LATEST_FILE}")
    for s in signals[:10]:
        names = ", ".join(p["name"] for p in s["politicians"])
        log.info(f"  {s['ticker']} score={s['score']} ({s['transaction_count']} buys) — {names}")

    return signals


if __name__ == "__main__":
    main()
