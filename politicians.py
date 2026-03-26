#!/usr/bin/env python3
"""
politicians.py — Congressional trade tracker
Fetches congressional trades from Quiver Quant (no API key needed, works from VPS).

Data sources (tried in order):
  1. Quiver Quant /beta/live/congresstrading (primary — works from VPS, returning 200 as of 2026-03-11)
  2. House Clerk PTR PDFs (secondary — 500ing since 2026-03-09, still down as of 2026-03-11)
  3. House Stock Watcher S3 (fallback — 403 from VPS since ~2026-01, residential IPs only)
  4. Senate Stock Watcher S3 (fallback — 403 from VPS since ~2026-01, residential IPs only)

IMPORTANT: Quiver Quant is the ONLY working data source from VPS as of 2026-03-11.
If it fails, politician_score degrades to 0 silently. Caching + alerting added to mitigate.

Scoring enhancements:
  - Multi-politician convergence: multiple members buying same ticker = boost
  - Amount-based scoring with whale tier
  - Recency decay (7d > 14d > 30d)
"""

import json
import logging
import os
import re
import io
import time
import glob
import zipfile
from datetime import datetime, date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from config import TELEGRAM_BOT_TOKEN

# Telegram alerting (for Quiver Quant failures)
TELEGRAM_CHAT_ID = "-5063061110"  # Pavano Maintenance
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

# Caching for Quiver Quant (resilience against transient failures)
QUIVER_CACHE_DIR = "/home/pavano/scripts/cache"
QUIVER_CACHE_MAX_AGE_DAYS = 3

# Degraded signal logging
SIGNAL_DEGRADED_LOG = "/home/pavano/scripts/logs/signal-degraded.log"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
POLITICIANS_DIR = os.path.join(BASE_DIR, "knowledge", "politicians")
LATEST_FILE = os.path.join(POLITICIANS_DIR, "latest.json")
HISTORY_FILE = os.path.join(POLITICIANS_DIR, "history.json")
PDF_CACHE_DIR = os.path.join(POLITICIANS_DIR, "pdf_cache")

WATCHLIST = {"SPY", "QQQ", "AAPL", "TSLA", "NVDA", "AMD", "MSFT", "META", "GME", "AMZN"}
LARGE_PURCHASE_THRESHOLD = 50_000
LOOKBACK_DAYS = 30

# Data sources
QUIVER_QUANT_URL = "https://api.quiverquant.com/beta/live/congresstrading"
HOUSE_CLERK_SEARCH_URL = "https://disclosures-clerk.house.gov/FinancialDisclosure/ViewMemberSearchResult"
HOUSE_PTR_BASE_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs"
HOUSE_S3_URLS = [
    "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json",
]
SENATE_S3_URLS = [
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
    """Tiered scoring: small/medium/large/whale."""
    if amount >= 1_000_000:
        return 4
    elif amount >= 250_000:
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


def convergence_bonus(num_politicians):
    """Multiple politicians buying same ticker = insider convergence signal."""
    if num_politicians >= 5:
        return 2.0
    elif num_politicians >= 3:
        return 1.5
    elif num_politicians >= 2:
        return 1.0
    return 0.0


def make_tx_id(name, ticker, tx_date):
    return f"{name.lower().replace(' ', '_')}_{ticker}_{tx_date}"


# ---------------------------------------------------------------------------
# Alerting, caching, and degraded signal logging (Sprint 5 hardening)
# ---------------------------------------------------------------------------

def send_quiver_alert(message: str):
    """Send Telegram alert for Quiver Quant failures."""
    try:
        resp = requests.post(
            TELEGRAM_API,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
            timeout=5
        )
        log.info(f"Quiver alert sent (status={resp.status_code})")
    except Exception as e:
        log.warning(f"Failed to send Quiver alert: {e}")


def get_quiver_cache_path(ticker: str = "all") -> str:
    """Get cache file path for Quiver Quant data."""
    today = date.today().isoformat()
    return os.path.join(QUIVER_CACHE_DIR, f"politician-{ticker}-{today}.json")


def save_quiver_cache(transactions: list, ticker: str = "all"):
    """Cache successful Quiver Quant results."""
    os.makedirs(QUIVER_CACHE_DIR, exist_ok=True)
    cache_path = get_quiver_cache_path(ticker)
    try:
        cache_data = {
            "cached_at": datetime.now().isoformat(),
            "transactions": transactions
        }
        with open(cache_path, "w") as f:
            json.dump(cache_data, f)
        log.info(f"Cached {len(transactions)} Quiver transactions to {cache_path}")
    except Exception as e:
        log.warning(f"Failed to cache Quiver data: {e}")


def load_quiver_cache(ticker: str = "all") -> tuple:
    """
    Load cached Quiver data from last 3 days.
    Returns (transactions, staleness_note) or (None, None) if no valid cache.
    """
    os.makedirs(QUIVER_CACHE_DIR, exist_ok=True)
    
    # Look for cache files from last N days
    for days_ago in range(QUIVER_CACHE_MAX_AGE_DAYS + 1):
        check_date = (date.today() - timedelta(days=days_ago)).isoformat()
        cache_path = os.path.join(QUIVER_CACHE_DIR, f"politician-{ticker}-{check_date}.json")
        
        if os.path.exists(cache_path):
            try:
                with open(cache_path) as f:
                    cache_data = json.load(f)
                transactions = cache_data.get("transactions", [])
                cached_at = cache_data.get("cached_at", check_date)
                staleness = f"STALE ({days_ago}d old, cached {cached_at})" if days_ago > 0 else "fresh"
                log.info(f"Loaded {len(transactions)} transactions from cache ({staleness})")
                return transactions, staleness if days_ago > 0 else None
            except Exception as e:
                log.warning(f"Failed to load cache {cache_path}: {e}")
                continue
    
    return None, None


def log_degraded_signal(ticker: str, reason: str):
    """Log when politician_score is 0 due to data failure (not legitimately 0)."""
    os.makedirs(os.path.dirname(SIGNAL_DEGRADED_LOG), exist_ok=True)
    timestamp = datetime.now().isoformat()
    log_line = f"{timestamp} | ticker={ticker} | reason={reason}\n"
    try:
        with open(SIGNAL_DEGRADED_LOG, "a") as f:
            f.write(log_line)
        log.info(f"Logged degraded signal: {ticker} - {reason}")
    except Exception as e:
        log.warning(f"Failed to log degraded signal: {e}")


# ---------------------------------------------------------------------------
# House Clerk PTR PDF parsing (primary source — works from VPS)
# ---------------------------------------------------------------------------

def get_house_ptr_doc_ids(year=None):
    """Fetch PTR filing DocIDs from House Clerk search form."""
    if year is None:
        year = date.today().year
    try:
        resp = requests.post(
            HOUSE_CLERK_SEARCH_URL,
            data={
                "LastName": "",
                "FirstName": "",
                "FilingYear": str(year),
                "ReportType": "P",
                "State": "",
                "District": "",
            },
            timeout=30,
            headers={"User-Agent": HEADERS["User-Agent"]},
        )
        resp.raise_for_status()
        anchors = re.findall(
            r'href="(public_disc/ptr-pdfs/\d+/(\d+)\.pdf)"[^>]*>([^<]+)',
            resp.text
        )
        filings = []
        for path, doc_id, name in anchors:
            filings.append({
                "doc_id": doc_id,
                "url": f"https://disclosures-clerk.house.gov/{path}",
                "name": name.strip(),
                "year": year,
            })
        log.info(f"House Clerk: found {len(filings)} PTR filings for {year}")
        return filings
    except Exception as e:
        log.error(f"House Clerk search error: {e}")
        return []


def parse_ptr_pdf(pdf_bytes, member_name):
    """Parse a House PTR PDF and extract purchase transactions."""
    try:
        import pdfplumber
    except ImportError:
        log.warning("pdfplumber not installed — skipping PDF parsing")
        return []

    transactions = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                lines = text.split("\n")
                for line in lines:
                    # Look for stock ticker: (AAPL) [ST]
                    ticker_match = re.search(r'\(([A-Z]{1,5})\)\s*\[ST\]', line)
                    if not ticker_match:
                        continue
                    ticker = ticker_match.group(1)
                    # P = Purchase, S = Sale
                    tx_type_match = re.search(r'\[ST\]\s+([PS])\s+', line)
                    if not tx_type_match or tx_type_match.group(1) != "P":
                        continue
                    # Extract dates (MM/DD/YYYY)
                    dates = re.findall(r'(\d{2}/\d{2}/\d{4})', line)
                    if not dates:
                        continue
                    try:
                        tx_date = datetime.strptime(dates[0], "%m/%d/%Y").date()
                    except Exception:
                        continue
                    if (date.today() - tx_date).days > LOOKBACK_DAYS:
                        continue
                    # Extract amount range like "$15,001 - $50,000"
                    amount_match = re.search(r'\$[\d,]+\s*-\s*\$[\d,]+', line)
                    amount_str = amount_match.group(0) if amount_match else ""
                    amount_clean = amount_str.replace("$", "").replace(",", "").strip()
                    amount = parse_amount(amount_clean)

                    transactions.append({
                        "name": member_name,
                        "party": "",
                        "chamber": "House",
                        "ticker": ticker,
                        "date": str(tx_date),
                        "amount_str": amount_str,
                        "amount": amount,
                        "tx_id": make_tx_id(member_name, ticker, str(tx_date)),
                    })
    except Exception as e:
        log.warning(f"PDF parse error for {member_name}: {e}")
    return transactions


def fetch_and_parse_ptr(filing, cache_dir):
    """Download (or load cached) PTR PDF and parse it."""
    os.makedirs(cache_dir, exist_ok=True)
    cache_file = os.path.join(cache_dir, f"{filing['doc_id']}.json")
    if os.path.exists(cache_file):
        try:
            with open(cache_file) as f:
                return json.load(f)
        except Exception:
            pass
    try:
        resp = requests.get(filing["url"], timeout=30, headers={"User-Agent": HEADERS["User-Agent"]})
        if resp.status_code != 200:
            return []
        txs = parse_ptr_pdf(resp.content, filing["name"])
        with open(cache_file, "w") as f:
            json.dump(txs, f)
        return txs
    except Exception as e:
        log.warning(f"Error fetching PTR {filing['doc_id']}: {e}")
        return []


def fetch_house_clerk(year=None):
    """Fetch and parse all House PTR filings for this year."""
    filings = get_house_ptr_doc_ids(year)
    if not filings:
        return []

    all_transactions = []
    log.info(f"Parsing {len(filings)} House PTR PDFs (using cache where available)...")

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_and_parse_ptr, f, PDF_CACHE_DIR): f for f in filings}
        for future in as_completed(futures):
            txs = future.result()
            all_transactions.extend(txs)
            time.sleep(0.05)

    log.info(f"House Clerk (PDF): {len(all_transactions)} purchase transactions found")
    return all_transactions


# ---------------------------------------------------------------------------
# S3 fallback sources (residential IPs only)
# ---------------------------------------------------------------------------

def fetch_json(urls, label):
    for url in urls:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            if resp.status_code == 403:
                log.warning(f"{label}: 403 Forbidden — S3 blocks cloud/VPS IPs")
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


# ---------------------------------------------------------------------------
# Quiver Quant (primary — works from VPS, no auth required)
# ---------------------------------------------------------------------------

def fetch_quiver_quant(ticker_filter: str = None):
    """
    Fetch congressional trades from Quiver Quant API (free, no key needed).
    
    On success: caches results for resilience.
    On failure: sends Telegram alert and attempts to load from cache.
    """
    cache_key = ticker_filter or "all"
    
    try:
        resp = requests.get(
            QUIVER_QUANT_URL,
            headers={"User-Agent": HEADERS["User-Agent"], "Accept": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        log.info(f"Quiver Quant: {len(data)} total records")
    except Exception as e:
        error_msg = str(e)
        log.error(f"Quiver Quant fetch error: {error_msg}")
        
        # ALERT: Quiver Quant is the only working source - this is critical
        alert_text = (
            f"⚠️ *Quiver Quant fetch failed*\n\n"
            f"Error: `{error_msg[:200]}`\n\n"
            f"Politician score will be 0 unless cache is available.\n"
            f"This is the ONLY working data source from VPS."
        )
        send_quiver_alert(alert_text)
        
        # Try to load from cache
        cached_txs, staleness = load_quiver_cache(cache_key)
        if cached_txs is not None:
            log.warning(f"Using cached Quiver data ({staleness})")
            # Log as degraded if using stale cache
            if staleness:
                log_degraded_signal(cache_key, f"Quiver API failed, using {staleness} cache")
            return cached_txs
        
        # No cache available - this will result in score 0
        log_degraded_signal(cache_key, f"Quiver API failed, no cache available: {error_msg}")
        return []

    transactions = []
    for tx in data:
        try:
            tx_type = (tx.get("Transaction") or "").lower()
            if "purchase" not in tx_type:
                continue
            ticker = (tx.get("Ticker") or "").upper().strip()
            if not ticker or ticker == "--":
                continue
            # Apply ticker filter if specified
            if ticker_filter and ticker != ticker_filter.upper():
                continue
            date_str = tx.get("TransactionDate") or tx.get("ReportDate") or ""
            if not date_str:
                continue
            tx_date = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
            if (date.today() - tx_date).days > LOOKBACK_DAYS:
                continue
            name = tx.get("Representative") or "Unknown"
            chamber = tx.get("House") or "Unknown"
            party = tx.get("Party") or ""
            amount_str = tx.get("Range") or ""
            amount = parse_amount(tx.get("Amount") or amount_str)
            transactions.append({
                "name": name,
                "party": party,
                "chamber": chamber,
                "ticker": ticker,
                "date": str(tx_date),
                "amount_str": amount_str,
                "amount": amount,
                "tx_id": make_tx_id(name, ticker, str(tx_date)),
            })
        except Exception:
            continue

    log.info(f"Quiver Quant: {len(transactions)} purchase transactions in last {LOOKBACK_DAYS} days")
    
    # Cache successful results
    if transactions:
        save_quiver_cache(transactions, cache_key)
    
    return transactions


# ---------------------------------------------------------------------------
# Signal aggregation
# ---------------------------------------------------------------------------

def aggregate_signals(transactions):
    by_ticker = {}
    for tx in transactions:
        ticker = tx["ticker"]
        if ticker in WATCHLIST or tx["amount"] >= LARGE_PURCHASE_THRESHOLD:
            by_ticker.setdefault(ticker, []).append(tx)

    results = []
    for ticker, txs in by_ticker.items():
        total_score = 0.0
        unique_politicians = set()
        for tx in txs:
            total_score += score_amount(tx["amount"])
            tx_date = datetime.strptime(tx["date"], "%Y-%m-%d").date()
            total_score += recency_score(tx_date)
            unique_politicians.add(tx["name"])

        # Convergence bonus: multiple members buying same ticker
        total_score += convergence_bonus(len(unique_politicians))
        total_score = min(3.0, total_score)

        results.append({
            "ticker": ticker,
            "score": round(total_score, 2),
            "politicians": [{
                "name": tx["name"], "party": tx["party"], "chamber": tx["chamber"],
                "amount": tx["amount_str"], "date": tx["date"]
            } for tx in sorted(txs, key=lambda x: x["date"], reverse=True)],
            "transaction_count": len(txs),
            "unique_politicians": len(unique_politicians),
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

    all_normalized = []
    source_used = "none"

    # Primary: Quiver Quant (works from VPS, no auth required)
    qq_txs = fetch_quiver_quant()
    if qq_txs:
        all_normalized.extend(qq_txs)
        source_used = "quiver_quant"
        log.info(f"Primary source: {len(qq_txs)} purchase transactions from Quiver Quant")

    # Secondary: House Clerk PTR PDFs (may 500 on gov servers)
    if not all_normalized:
        log.info("Quiver Quant empty — trying House Clerk PTR PDFs...")
        house_clerk_txs = fetch_house_clerk()
        if house_clerk_txs:
            all_normalized.extend(house_clerk_txs)
            source_used = "house_clerk_pdf"
            log.info(f"Secondary source: {len(house_clerk_txs)} House transactions from PTR PDFs")

    # Fallback: S3 sources (residential only — 403 from VPS/cloud)
    if not all_normalized:
        log.info("All primary sources empty — trying S3 fallback (likely blocked)...")
        house_raw = fetch_json(HOUSE_S3_URLS, "House S3")
        senate_raw = fetch_json(SENATE_S3_URLS, "Senate S3")
        for tx in house_raw:
            n = normalize_house(tx)
            if n:
                all_normalized.append(n)
        for tx in senate_raw:
            n = normalize_senate(tx)
            if n:
                all_normalized.append(n)
        if all_normalized:
            source_used = "s3_fallback"

    log.info(f"Total: {len(all_normalized)} purchase transactions (last {LOOKBACK_DAYS} days)")

    if not all_normalized:
        log.warning("No transactions available. Options+news signals only for this run.")
        # Log this as a degraded signal condition - all sources failed
        log_degraded_signal("ALL", "All data sources failed or returned empty - politician_score will be 0 for all tickers")
        signals = []
    else:
        signals = aggregate_signals(all_normalized)
        update_history(all_normalized)

    output = {
        "generated_at": datetime.now().isoformat(),
        "lookback_days": LOOKBACK_DAYS,
        "total_transactions": len(all_normalized),
        "tickers_flagged": len(signals),
        "source_used": source_used,
        "signals": signals
    }

    with open(LATEST_FILE, "w") as f:
        json.dump(output, f, indent=2)

    log.info(f"Saved {len(signals)} signals to {LATEST_FILE}")
    for s in signals[:10]:
        names = ", ".join(p["name"] for p in s["politicians"])
        log.info(
            f"  {s['ticker']} score={s['score']} "
            f"({s['transaction_count']} buys, {s['unique_politicians']} members) — {names}"
        )

    return signals


if __name__ == "__main__":
    main()
