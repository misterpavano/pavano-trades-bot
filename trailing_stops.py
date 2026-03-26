"""
trailing_stops.py — Trailing stop tracker for options positions.
Persists high-water marks to disk so they survive between intraday checks.
"""
import json
import os
from datetime import date

TRAILING_FILE = os.path.join(os.path.dirname(__file__), "trades", f"trailing_{date.today().isoformat()}.json")

def _load() -> dict:
    if os.path.exists(TRAILING_FILE):
        with open(TRAILING_FILE) as f:
            return json.load(f)
    return {}

def _save(data: dict):
    os.makedirs(os.path.dirname(TRAILING_FILE), exist_ok=True)
    with open(TRAILING_FILE, "w") as f:
        json.dump(data, f, indent=2)

def update_high_water(symbol: str, current_pnl_pct: float) -> dict:
    """
    Update high water mark for a position. Returns:
    {
        "high_water": float,   # highest P&L % seen
        "trailing_triggered": bool,  # whether trailing stop should fire
        "trail_level": float   # the P&L % that would trigger a close
    }
    """
    from config import TRAILING_STOP_ACTIVATE, TRAILING_STOP_PCT
    
    data = _load()
    
    prev_high = data.get(symbol, {}).get("high_water", current_pnl_pct)
    new_high = max(prev_high, current_pnl_pct)
    
    data[symbol] = {
        "high_water": new_high,
        "last_pnl": current_pnl_pct,
        "updated": date.today().isoformat()
    }
    _save(data)
    
    # Trailing stop only activates once we've hit the activation threshold
    if new_high >= TRAILING_STOP_ACTIVATE:
        trail_level = new_high - TRAILING_STOP_PCT
        triggered = current_pnl_pct <= trail_level
        return {
            "high_water": new_high,
            "trailing_triggered": triggered,
            "trail_level": trail_level
        }
    
    return {
        "high_water": new_high,
        "trailing_triggered": False,
        "trail_level": None
    }

def clear_position(symbol: str):
    """Remove a position from trailing tracker (after close)."""
    data = _load()
    data.pop(symbol, None)
    _save(data)
