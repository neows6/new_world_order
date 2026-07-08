"""
paper/r2000_scanner.py — 3x/day lightweight scan of the R2000 universe.

Fires at 08:30, 12:00, and 15:00 ET on weekdays.
For each ticker in stagegate_russell2000.json stage1 that is not already
in any model's stage2 or stage3, runs a technical + fundamental screen.
Tickers that produce a BUY signal are promoted to stage2 of all 4 models
so the regular 5-min pipeline picks them up immediately.
"""

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

import pytz
from loguru import logger

ET = pytz.timezone("America/New_York")

_SCAN_SLOTS_ET = [(9, 0), (12, 0), (15, 30)]   # (hour, minute) — 9am, noon, 3:30pm ET

_lock           = threading.Lock()
_last_scan_date: Optional[str] = None
_fired_slots:    set            = set()
_scan_running:   bool           = False
_last_result:    dict           = {}


def should_fire_now() -> Optional[str]:
    """Return slot label '08:30'|'12:00'|'15:00' if it's time to fire, else None."""
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


def promote_to_stage2(ticker: str) -> list:
    """Add ticker to stage2 of all 4 models. Returns list of model names."""
    from paper.executor import PAPER_MODEL_CONFIGS
    promoted = []
    for model, cfg in PAPER_MODEL_CONFIGS.items():
        try:
            sg_path = Path(cfg["stagegate"])
            if not sg_path.exists():
                continue
            sg = json.loads(sg_path.read_text(encoding="utf-8"))
            for k in ("stage1", "stage2", "stage3"):
                sg.setdefault(k, [])
            if ticker not in sg["stage2"] and ticker not in sg["stage3"]:
                sg["stage2"].append(ticker)
                sg_path.write_text(json.dumps(sg, indent=2), encoding="utf-8")
                promoted.append(model)
        except Exception as e:
            logger.warning(f"[R2K] promote_to_stage2 failed for {ticker}/{model}: {e}")
    return promoted


def run_scan(Session, engines: dict) -> dict:
    """
    Full R2000 scan.

    engines keys: fft, fib, vwap, vol, vix, aggregator, st, analysis, market_data

    Returns summary dict.
    """
    global _scan_running, _last_result

    with _lock:
        if _scan_running:
            logger.info("[R2K SCAN] Already running — skipping")
            return {"status": "already_running"}
        _scan_running = True

    try:
        return _do_scan(Session, engines)
    finally:
        _scan_running = False


def _do_scan(Session, engines: dict) -> dict:
    from paper.executor import PAPER_MODEL_CONFIGS
    from paper.runner import _load_price_data, _fetch_live_quotes

    _BASE_BUY_THRESHOLD = 0.06   # slightly relaxed vs main pipeline's 0.08

    r2k_path = Path("data/stagegate_russell2000.json")
    if not r2k_path.exists():
        logger.warning("[R2K SCAN] stagegate_russell2000.json not found")
        return {"scanned": 0, "promoted": [], "skipped_no_data": 0}

    universe = json.loads(r2k_path.read_text(encoding="utf-8")).get("stage1", [])
    if not universe:
        return {"scanned": 0, "promoted": [], "skipped_no_data": 0}

    # Collect tickers already active/held in any model
    already_active: set = set()
    for model, cfg in PAPER_MODEL_CONFIGS.items():
        try:
            sg = json.loads(Path(cfg["stagegate"]).read_text(encoding="utf-8"))
            already_active.update(sg.get("stage2", []))
            already_active.update(sg.get("stage3", []))
        except Exception:
            pass

    candidates = [t for t in universe if t not in already_active]
    if not candidates:
        logger.info("[R2K SCAN] No candidates — all already active/held")
        return {"scanned": 0, "promoted": [], "skipped_no_data": 0}

    logger.info(f"[R2K SCAN] Starting — {len(candidates)} candidates from {len(universe)}-ticker universe")

    # Batch fetch live quotes
    live_quotes: dict = {}
    try:
        live_quotes = _fetch_live_quotes(candidates)
    except Exception:
        pass

    # VIX regime (shared)
    vix_regime = None
    try:
        vq = engines["market_data"].get_quote("$VIX")
        vix_level = float(vq["last_price"]) if vq and vq.get("last_price") else 20.0
        vix_regime = engines["vix"].classify(vix_level)
    except Exception:
        pass

    # I-Tool cache
    itool_signals: dict = {}
    try:
        cache = Path("data/itool_scan.json")
        if cache.exists():
            for r in json.loads(cache.read_text(encoding="utf-8")).get("results", []):
                if r.get("ticker") and r.get("signal"):
                    itool_signals[r["ticker"]] = r["signal"]
    except Exception:
        pass

    # TipRanks cache
    from monitor.tipranks_scanner import get_cached_signal as _tr_get
    from signals.tipranks_signal import TipRanksResult as _TRResult

    scanned         = 0
    promoted_list   = []
    skipped_no_data = 0

    for ticker in candidates:
        try:
            pd      = _load_price_data(Session, ticker, live_quotes=live_quotes)
            closes  = pd.get("closes", [])
            highs   = pd.get("highs", [])
            lows    = pd.get("lows", [])
            volumes = pd.get("volumes", [])
            cur_p   = pd.get("current_price")

            if len(closes) < 20:
                skipped_no_data += 1
                continue

            # Fundamental analysis (L1/L2)
            analysis = None
            try:
                analysis = engines["analysis"].analyze_ticker(ticker)
            except Exception:
                pass

            if analysis is None:
                skipped_no_data += 1
                continue

            # Technical signals
            fft  = None
            fib  = None
            vwap = None
            vol_p = None
            st   = None
            try:
                if len(closes) >= 64:
                    fft = engines["fft"].analyze(ticker, closes)
            except Exception:
                pass
            try:
                if len(closes) >= 30:
                    fib = engines["fib"].analyze(ticker, highs, lows, closes)
            except Exception:
                pass
            try:
                if len(closes) >= 5:
                    vwap = engines["vwap"].compute_daily(ticker, highs, lows, closes, volumes)
            except Exception:
                pass
            try:
                if len(closes) >= 10:
                    vol_p = engines["vol"].analyze(ticker, highs, lows, closes, volumes)
            except Exception:
                pass
            try:
                if len(closes) >= 20:
                    st = engines["st"].analyze(ticker, highs, lows, closes, volumes)
            except Exception:
                pass

            _tr_raw = _tr_get(ticker)
            _tr_result = None
            if _tr_raw:
                _tr_result = _TRResult(
                    ticker=ticker,
                    smart_score=_tr_raw.get("smart_score"),
                    buy_pct=_tr_raw.get("buy_pct"),
                    hold_pct=_tr_raw.get("hold_pct"),
                    sell_pct=_tr_raw.get("sell_pct"),
                    price_target_mean=_tr_raw.get("price_target"),
                    composite_score=_tr_raw.get("composite", 0.0),
                    analyst_count=_tr_raw.get("analyst_count", 0),
                )

            agg = engines["aggregator"].aggregate(
                analysis=analysis,
                fft=fft, fib=fib, insider=None,
                vwap=vwap, vol_profile=vol_p,
                vix_regime=vix_regime,
                current_price=cur_p,
                itool_signal=itool_signals.get(ticker),
                supertrend=st,
                tipranks=_tr_result,
                buy_threshold_override=_BASE_BUY_THRESHOLD,
            )

            scanned += 1

            if getattr(agg, "signal", None) == "BUY":
                models_to = promote_to_stage2(ticker)
                if models_to:
                    promoted_list.append(ticker)
                    logger.info(f"[R2K SCAN] \u25b2 {ticker} promoted to stage2 of {models_to}")

        except Exception as e:
            logger.debug(f"[R2K SCAN] {ticker}: {e}")

    result = {
        "scanned":          scanned,
        "promoted":         promoted_list,
        "skipped_no_data":  skipped_no_data,
        "total_candidates": len(candidates),
        "universe_size":    len(universe),
    }
    global _last_result
    _last_result = result
    logger.info(
        f"[R2K SCAN] Done — scanned {scanned}, promoted {len(promoted_list)}, "
        f"no_data_skipped {skipped_no_data}/{len(candidates)}"
    )
    return result
