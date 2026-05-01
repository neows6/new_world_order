"""
monitor/tipranks_scanner.py — Batch TipRanks scanner.

Fires twice daily (09:15 AM and 4:05 PM ET) on weekdays.
Fetches Smart Score + analyst consensus for all watchlist + stagegate +
R2000 tickers. Results cached to data/tipranks_scan.json.

Usage:
    from monitor.tipranks_scanner import get_cached_signal, get_status
    signal = get_cached_signal("AAPL")   # → {"smart_score": 8, "composite": 0.41, ...}
"""

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

import pytz
from loguru import logger

ET = pytz.timezone("America/New_York")

_SCAN_SLOTS_ET = [(9, 15), (16, 5)]   # 09:15 AM and 4:05 PM ET
_SCAN_FILE     = Path("data/tipranks_scan.json")

_lock            = threading.Lock()
_last_scan_date: Optional[str] = None
_fired_slots:    set            = set()
_scan_running:   bool           = False
_last_result:    dict           = {}


def should_fire_now() -> Optional[str]:
    """Return slot label e.g. '09:15' if it's time to fire, else None."""
    global _last_scan_date, _fired_slots
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return None
    today = now.strftime("%Y-%m-%d")
    with _lock:
        if _last_scan_date != today:
            _last_scan_date = today
            _fired_slots    = set()
        for h, m in _SCAN_SLOTS_ET:
            slot   = f"{h:02d}:{m:02d}"
            slot_m = h * 60 + m
            now_m  = now.hour * 60 + now.minute
            if slot not in _fired_slots and abs(now_m - slot_m) <= 2:
                _fired_slots.add(slot)
                return slot
    return None


def get_status() -> dict:
    return {
        "scan_running": _scan_running,
        "last_result":  _last_result,
        "last_date":    _last_scan_date,
        "fired_slots":  list(_fired_slots),
    }


def get_cached_signal(ticker: str) -> Optional[dict]:
    """
    Read tipranks_scan.json for a single ticker.
    Returns dict with keys: smart_score, buy_pct, hold_pct, sell_pct,
    price_target, composite, analyst_count — or None if not cached.
    """
    try:
        data = json.loads(_SCAN_FILE.read_text(encoding="utf-8"))
        return data.get("results", {}).get(ticker)
    except Exception:
        return None


def run_scan(tickers: list) -> dict:
    """
    Fetch TipRanks data for all tickers and write tipranks_scan.json.
    Returns summary dict.
    """
    global _scan_running, _last_result

    with _lock:
        if _scan_running:
            logger.info("[TR SCAN] Already running — skipping")
            return {"status": "already_running"}
        _scan_running = True

    try:
        return _do_scan(tickers)
    finally:
        _scan_running = False


def _do_scan(tickers: list) -> dict:
    from data_sources.tipranks_client import get_client, TipRanksClient
    from signals.tipranks_signal import TipRanksAnalyzer

    if not tickers:
        return {"scanned": 0, "failed": 0}

    if not TipRanksClient.cookies_available():
        logger.warning(
            "[TR SCAN] Skipped — data/tipranks_cookies.json not found. "
            "Export browser cookies from tipranks.com and save to that path."
        )
        return {"scanned": 0, "failed": 0, "error": "no_cookies"}

    logger.info(f"[TR SCAN] Starting — {len(tickers)} tickers")
    client   = get_client()
    analyzer = TipRanksAnalyzer()

    # Load existing cache so unscanned tickers keep their old data
    existing: dict = {}
    try:
        if _SCAN_FILE.exists():
            existing = json.loads(_SCAN_FILE.read_text(encoding="utf-8")).get("results", {})
    except Exception:
        pass

    raw_batch = client.get_batch(tickers, delay=0.4)

    scanned = 0
    failed  = len(tickers) - len(raw_batch)
    out     = dict(existing)   # start from existing, overwrite with fresh data

    for ticker, raw in raw_batch.items():
        try:
            res = analyzer.analyze(ticker, raw)
            out[ticker] = {
                "smart_score":   res.smart_score,
                "buy_pct":       res.buy_pct,
                "hold_pct":      res.hold_pct,
                "sell_pct":      res.sell_pct,
                "price_target":  res.price_target_mean,
                "composite":     res.composite_score,
                "analyst_count": res.analyst_count,
            }
            scanned += 1
        except Exception as e:
            logger.debug(f"[TR SCAN] normalize error {ticker}: {e}")
            failed += 1

    payload = {
        "results":     out,
        "_updated_at": datetime.now().isoformat(),
        "_count":      scanned,
    }
    _SCAN_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    result = {
        "scanned":  scanned,
        "failed":   failed,
        "total":    len(tickers),
        "updated_at": payload["_updated_at"],
    }
    global _last_result
    _last_result = result
    logger.info(f"[TR SCAN] Done — scanned {scanned}, failed {failed}/{len(tickers)}")
    return result
