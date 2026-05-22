"""
monitor/strat.py - STRAT: Historical Threshold Breach Analysis

For each symbol and each closing date from Feb 2020 to Feb 2026:
  Downside: Did price fall ≥15% at any point within 90 calendar days?
            (stocks: first breach day; indices: last trading day only)
  Upside:   Did price rise ≥10% at any point within 30 calendar days?
            (stocks: first breach day; indices: last trading day only)

Routes:
  GET  /strat                       - Dashboard page
  GET  /api/strat/data              - Full cached analysis (optionally ?symbol=X)
  GET  /api/strat/status            - Compute progress
  POST /api/strat/refresh           - Trigger background recompute
  POST /api/strat/add_symbol        - Add custom symbol  {"symbol": "AMD"}
  DELETE /api/strat/remove_symbol/{symbol} - Remove custom symbol
"""

import json
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import yfinance as yf
from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse
from loguru import logger

strat_router = APIRouter()

ROOT = Path(__file__).resolve().parent.parent
_CACHE_FILE   = ROOT / "data" / "strat_cache.json"
_SYMBOLS_FILE = ROOT / "data" / "strat_symbols.json"

# ── Symbol configuration ──────────────────────────────────────────────────────

STRAT_SYMBOLS: dict[str, dict[str, str]] = {
    "Mega Cap Tech": {"GOOG": "GOOG", "AMZN": "AMZN", "AAPL": "AAPL", "META": "META", "MSFT": "MSFT"},
    "AI & Semis":    {"NVDA": "NVDA", "AVGO": "AVGO"},
    "Growth":        {"TSLA": "TSLA", "LLY": "LLY"},
    "Indices":       {"SPX": "^GSPC", "RUT": "^RUT", "NDX": "^NDX"},
}

INDEX_SYMBOLS: set[str] = {"SPX", "RUT", "NDX"}  # last-trading-day-only breach rule

ANALYSIS_START = date(2020, 2, 1)
ANALYSIS_END   = date(2026, 2, 28)
FETCH_START    = "2020-01-15"   # extra buffer for the first window
FETCH_END      = "2026-05-31"   # covers 90-day window past Feb 2026

DOWNSIDE_THRESHOLD = 0.15
DOWNSIDE_WINDOW    = 90   # calendar days

UPSIDE_THRESHOLD   = 0.10
UPSIDE_WINDOW      = 30   # calendar days

PERIOD_CUTOFFS = {
    "1yr": date(2025, 2, 1),
    "3yr": date(2023, 2, 1),
    "6yr": date(2020, 2, 1),
}

# ── State ─────────────────────────────────────────────────────────────────────

_cache_lock    = threading.Lock()
_cache: dict   = {}
_computing     = False
_compute_pct   = 0
_computed_at   = ""

# ── Custom symbol persistence ─────────────────────────────────────────────────

def _load_custom_symbols() -> dict[str, str]:
    try:
        if _SYMBOLS_FILE.exists():
            return json.loads(_SYMBOLS_FILE.read_text())
    except Exception:
        pass
    return {}

def _save_custom_symbols(custom: dict) -> None:
    _SYMBOLS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SYMBOLS_FILE.write_text(json.dumps(custom, indent=2))

def _all_symbols() -> dict[str, str]:
    """Flat symbol → yf_ticker mapping including custom."""
    result = {}
    for group in STRAT_SYMBOLS.values():
        result.update(group)
    result.update(_load_custom_symbols())
    return result

# ── Computation engine ────────────────────────────────────────────────────────

def _fetch_prices(yf_ticker: str) -> Optional[object]:
    """Return pd.Series of daily closing prices, or None on failure."""
    try:
        df = yf.download(
            yf_ticker,
            start=FETCH_START,
            end=FETCH_END,
            interval="1d",
            auto_adjust=True,
            progress=False,
        )
        if df.empty:
            return None
        # Flatten MultiIndex columns (newer yfinance versions)
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        series = df["Close"].dropna()
        # Strip timezone if present
        if hasattr(series.index, "tz") and series.index.tz is not None:
            series.index = series.index.tz_localize(None)
        return series
    except Exception as exc:
        logger.warning(f"[STRAT] yfinance fetch failed for {yf_ticker}: {exc}")
        return None


def _compute_symbol(sym: str, yf_ticker: str) -> dict:
    """Compute downside and upside breach analysis for one symbol."""
    prices = _fetch_prices(yf_ticker)
    if prices is None or len(prices) < 5:
        return {"error": "no_data", "downside": {"occurrences": []}, "upside": {"occurrences": []}}

    import pandas as pd

    prices_idx  = prices.index.date   # numpy array of date objects
    is_index    = sym in INDEX_SYMBOLS

    # Earliest available date - used to determine which period labels are valid
    earliest = prices_idx[0] if len(prices_idx) > 0 else ANALYSIS_START

    downside_occ = []
    upside_occ   = []

    # Iterate start dates within analysis window
    all_dates = [(d, p) for d, p in zip(prices_idx, prices.values)
                 if ANALYSIS_START <= d <= ANALYSIS_END]

    for start_date, start_price in all_dates:
        if start_price <= 0:
            continue

        # ── Downside ─────────────────────────────────────────────────────────
        win_end = start_date + timedelta(days=DOWNSIDE_WINDOW)
        mask    = (prices_idx > start_date) & (prices_idx <= win_end)
        win_dates  = prices_idx[mask]
        win_prices = prices.values[mask]

        if len(win_prices) == 0:
            pass
        else:
            threshold_dn = start_price * (1 - DOWNSIDE_THRESHOLD)
            breach_date  = None
            breach_price = None

            if is_index:
                # Check only last trading day of window
                bp = win_prices[-1]
                if bp <= threshold_dn:
                    breach_date  = win_dates[-1]
                    breach_price = float(bp)
            else:
                for bd, bp in zip(win_dates, win_prices):
                    if bp <= threshold_dn:
                        breach_date  = bd
                        breach_price = float(bp)
                        break

            if breach_date is not None:
                pct = (breach_price - start_price) / start_price * 100
                # Build window price series as % change from start
                win_pts = [
                    {"d": str(start_date), "p": round(0.0, 4)}
                ] + [
                    {"d": str(wd), "p": round((float(wp) - start_price) / start_price * 100, 4)}
                    for wd, wp in zip(win_dates, win_prices)
                ]
                downside_occ.append({
                    "start_date":   str(start_date),
                    "start_price":  round(float(start_price), 2),
                    "breach_date":  str(breach_date),
                    "breach_price": round(breach_price, 2),
                    "pct_change":   round(pct, 2),
                    "window_prices": win_pts,
                })

        # ── Upside ───────────────────────────────────────────────────────────
        win_end_up = start_date + timedelta(days=UPSIDE_WINDOW)
        mask_up    = (prices_idx > start_date) & (prices_idx <= win_end_up)
        win_dates_up  = prices_idx[mask_up]
        win_prices_up = prices.values[mask_up]

        if len(win_prices_up) == 0:
            pass
        else:
            threshold_up = start_price * (1 + UPSIDE_THRESHOLD)
            breach_date_u  = None
            breach_price_u = None

            if is_index:
                bp = win_prices_up[-1]
                if bp >= threshold_up:
                    breach_date_u  = win_dates_up[-1]
                    breach_price_u = float(bp)
            else:
                for bd, bp in zip(win_dates_up, win_prices_up):
                    if bp >= threshold_up:
                        breach_date_u  = bd
                        breach_price_u = float(bp)
                        break

            if breach_date_u is not None:
                pct_u = (breach_price_u - start_price) / start_price * 100
                win_pts_u = [
                    {"d": str(start_date), "p": round(0.0, 4)}
                ] + [
                    {"d": str(wd), "p": round((float(wp) - start_price) / start_price * 100, 4)}
                    for wd, wp in zip(win_dates_up, win_prices_up)
                ]
                upside_occ.append({
                    "start_date":   str(start_date),
                    "start_price":  round(float(start_price), 2),
                    "breach_date":  str(breach_date_u),
                    "breach_price": round(breach_price_u, 2),
                    "pct_change":   round(pct_u, 2),
                    "window_prices": win_pts_u,
                })

    return {
        "earliest_date": str(earliest),
        "downside": {"threshold": DOWNSIDE_THRESHOLD, "window_days": DOWNSIDE_WINDOW, "occurrences": downside_occ},
        "upside":   {"threshold": UPSIDE_THRESHOLD,   "window_days": UPSIDE_WINDOW,   "occurrences": upside_occ},
    }


def _run_compute(symbols_override: Optional[dict] = None) -> None:
    global _computing, _compute_pct, _computed_at, _cache

    with _cache_lock:
        if _computing:
            return
        _computing = True
        _compute_pct = 0

    try:
        syms = symbols_override if symbols_override is not None else _all_symbols()
        total = len(syms)
        result = {}

        for i, (sym, yf_ticker) in enumerate(syms.items()):
            logger.info(f"[STRAT] Computing {sym} ({yf_ticker}) [{i+1}/{total}]")
            result[sym] = _compute_symbol(sym, yf_ticker)
            with _cache_lock:
                _compute_pct = int((i + 1) / total * 100)

        ts = datetime.utcnow().isoformat()
        full = {"computed_at": ts, "data": result}

        # Merge with existing cache for custom symbol additions
        with _cache_lock:
            existing = dict(_cache.get("data", {}))
            existing.update(result)
            full["data"] = existing
            _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _CACHE_FILE.write_text(json.dumps(full))
            _cache = full
            _computed_at = ts
            _compute_pct = 100

        logger.info(f"[STRAT] Compute complete - {total} symbols")

    except Exception as exc:
        logger.error(f"[STRAT] Compute error: {exc}")
    finally:
        with _cache_lock:
            _computing = False


def _load_cache() -> bool:
    global _cache, _computed_at
    try:
        if _CACHE_FILE.exists():
            data = json.loads(_CACHE_FILE.read_text())
            age_h = (datetime.utcnow() - datetime.fromisoformat(data.get("computed_at", "2000-01-01"))).total_seconds() / 3600
            if age_h < 24:
                with _cache_lock:
                    _cache = data
                    _computed_at = data.get("computed_at", "")
                logger.info(f"[STRAT] Loaded cache ({age_h:.1f}h old, {len(data.get('data', {}))} symbols)")
                return True
    except Exception as exc:
        logger.warning(f"[STRAT] Cache load failed: {exc}")
    return False


def _ensure_computed() -> None:
    if not _load_cache():
        logger.info("[STRAT] Cache stale/missing - computing in background")
        threading.Thread(target=_run_compute, daemon=True, name="strat-compute").start()


# ── Startup - runs when module is imported by dashboard.py ───────────────────
# APIRouter doesn't support on_event; the main app's startup fires after import,
# so we kick off cache loading/computation here at import time instead.
threading.Thread(target=_ensure_computed, daemon=True, name="strat-init").start()

# ── API endpoints ─────────────────────────────────────────────────────────────

@strat_router.get("/api/strat/status")
def api_strat_status():
    with _cache_lock:
        return JSONResponse({
            "computing":   _computing,
            "progress":    _compute_pct,
            "computed_at": _computed_at,
            "symbols":     list(_cache.get("data", {}).keys()),
        })


@strat_router.get("/api/strat/data")
def api_strat_data(symbol: Optional[str] = None):
    with _cache_lock:
        data = _cache.get("data", {})

    if symbol:
        sym = symbol.upper()
        if sym not in data:
            return JSONResponse({"error": "symbol_not_found"}, status_code=404)
        return JSONResponse(data[sym])
    return JSONResponse(data)


@strat_router.post("/api/strat/refresh")
def api_strat_refresh():
    if _computing:
        return JSONResponse({"status": "already_running"})
    threading.Thread(target=_run_compute, daemon=True, name="strat-refresh").start()
    return JSONResponse({"status": "started"})


@strat_router.post("/api/strat/add_symbol")
def api_strat_add_symbol(body: dict):
    sym = (body.get("symbol") or "").strip().upper()
    if not sym:
        return JSONResponse({"error": "symbol_required"}, status_code=400)

    # Check it's not already a built-in
    existing = _all_symbols()
    if sym in existing:
        return JSONResponse({"status": "already_exists"})

    # Validate with yfinance - quick 5d fetch
    try:
        t = yf.Ticker(sym)
        hist = t.history(period="5d")
        if hist.empty:
            return JSONResponse({"error": "symbol_not_found_in_yfinance"}, status_code=400)
    except Exception:
        return JSONResponse({"error": "validation_failed"}, status_code=400)

    custom = _load_custom_symbols()
    custom[sym] = sym   # yf ticker same as symbol for equities
    _save_custom_symbols(custom)

    # Compute just this symbol in background
    threading.Thread(
        target=_run_compute,
        args=({sym: sym},),
        daemon=True,
        name=f"strat-add-{sym}"
    ).start()

    return JSONResponse({"status": "added", "symbol": sym})


@strat_router.delete("/api/strat/remove_symbol/{symbol}")
def api_strat_remove_symbol(symbol: str):
    sym = symbol.upper()
    custom = _load_custom_symbols()
    if sym not in custom:
        return JSONResponse({"error": "not_a_custom_symbol"}, status_code=400)
    del custom[sym]
    _save_custom_symbols(custom)

    with _cache_lock:
        _cache.get("data", {}).pop(sym, None)
        try:
            _CACHE_FILE.write_text(json.dumps(_cache))
        except Exception:
            pass

    return JSONResponse({"status": "removed", "symbol": sym})


# ── HTML page ─────────────────────────────────────────────────────────────────

_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>STRAT - NWO</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d1117;color:#e6edf3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',monospace;font-size:13px;min-height:100vh}
a{color:#58a6ff;text-decoration:none}
header{background:#161b22;border-bottom:1px solid #30363d;padding:10px 18px;display:flex;align-items:center;justify-content:space-between;gap:12px}
.logo{font-weight:700;font-size:15px;color:#e6edf3;letter-spacing:.5px}
.nav{display:flex;gap:6px;flex-wrap:wrap}
.nav a{padding:4px 10px;border-radius:6px;border:1px solid #30363d;background:transparent;color:#8b949e;font-size:11px;font-weight:600;transition:all .15s}
.nav a:hover,.nav a.active{background:#1f6feb22;border-color:#1f6feb;color:#58a6ff}
.container{padding:16px 20px;max-width:1600px;margin:0 auto}
h1{font-size:17px;font-weight:700;color:#e6edf3;margin-bottom:4px}
.subtitle{color:#8b949e;font-size:11px;margin-bottom:16px}

/* Symbol picker */
.picker-section{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px 16px;margin-bottom:16px}
.picker-group{margin-bottom:10px;display:flex;align-items:flex-start;gap:10px}
.picker-group:last-child{margin-bottom:0}
.group-label{font-size:10px;font-weight:700;color:#8b949e;text-transform:uppercase;letter-spacing:.8px;min-width:90px;padding-top:5px;flex-shrink:0}
.chips{display:flex;flex-wrap:wrap;gap:5px;flex:1}
.chip{padding:4px 11px;border-radius:12px;border:1px solid #30363d;background:transparent;color:#8b949e;cursor:pointer;font-size:11px;font-weight:600;transition:all .15s}
.chip:hover{border-color:#58a6ff44;color:#c9d1d9}
.chip.active{background:#1f6feb22;border-color:#1f6feb;color:#58a6ff}
.chip-custom{border-color:#3fb95044;color:#3fb950;display:flex;align-items:center;gap:5px}
.chip-custom.active{background:#3fb95022;border-color:#3fb950}
.chip-del{background:none;border:none;color:#f8514988;cursor:pointer;font-size:13px;line-height:1;padding:0}
.chip-del:hover{color:#f85149}
.add-sym-wrap{display:flex;gap:6px;align-items:center;margin-top:6px}
.add-sym-input{padding:4px 10px;border-radius:6px;border:1px solid #30363d;background:#0d1117;color:#e6edf3;font-size:12px;width:110px;text-transform:uppercase}
.add-sym-input::placeholder{text-transform:none;color:#8b949e}
.add-sym-btn{padding:4px 10px;border-radius:6px;border:1px solid #3fb950;background:transparent;color:#3fb950;cursor:pointer;font-size:11px;font-weight:600}
.add-sym-btn:hover{background:#3fb95011}
.add-sym-status{font-size:11px;color:#8b949e;margin-left:4px}

/* Status bar */
.status-bar{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:8px 14px;margin-bottom:14px;display:flex;align-items:center;gap:10px;font-size:11px;color:#8b949e}
.status-dot{width:7px;height:7px;border-radius:50%;background:#3fb950;flex-shrink:0}
.status-dot.computing{background:#d29922;animation:pulse 1s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.progress-bar{flex:1;height:4px;background:#21262d;border-radius:2px;overflow:hidden}
.progress-fill{height:100%;background:#1f6feb;border-radius:2px;transition:width .3s}
.refresh-btn{padding:3px 9px;border-radius:5px;border:1px solid #30363d;background:transparent;color:#8b949e;cursor:pointer;font-size:10px}
.refresh-btn:hover{border-color:#58a6ff;color:#58a6ff}

/* Symbol header */
.sym-header{display:flex;align-items:center;gap:10px;margin-bottom:14px}
.sym-title{font-size:20px;font-weight:700;color:#e6edf3}
.sym-meta{font-size:11px;color:#8b949e;flex:1}
.csv-btn{padding:4px 11px;border-radius:6px;border:1px solid #30363d;background:transparent;color:#8b949e;cursor:pointer;font-size:11px;font-weight:600;white-space:nowrap;flex-shrink:0}
.csv-btn:hover{border-color:#58a6ff;color:#58a6ff}

/* Analysis panels */
.panels{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:900px){.panels{grid-template-columns:1fr}}
.panel{background:#161b22;border:1px solid #30363d;border-radius:8px;overflow:hidden}
.panel-header{padding:10px 14px;border-bottom:1px solid #30363d;display:flex;align-items:center;justify-content:space-between}
.panel-title{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.6px}
.panel-dn .panel-title{color:#f85149}
.panel-up .panel-title{color:#3fb950}
.panel-sub{font-size:10px;color:#8b949e;margin-top:1px}
.counts-table{width:100%;border-collapse:collapse}
.counts-table th{font-size:10px;text-transform:uppercase;letter-spacing:.5px;color:#8b949e;padding:7px 14px;text-align:left;border-bottom:1px solid #21262d}
.counts-table td{padding:6px 14px;border-bottom:1px solid #21262d;font-size:12px}
.counts-table tr:last-child td{border-bottom:none}
.count-num{font-weight:700;font-size:15px;color:#e6edf3}
.count-na{color:#30363d;font-size:11px}
.period-filter{display:flex;gap:4px;padding:8px 14px;border-bottom:1px solid #30363d}
.pf-btn{padding:3px 10px;border-radius:10px;border:1px solid #30363d;background:transparent;color:#8b949e;font-size:10px;font-weight:600;cursor:pointer;transition:all .15s}
.pf-btn.active-dn{border-color:#f8514966;color:#f85149;background:#f8514911}
.pf-btn.active-up{border-color:#3fb95066;color:#3fb950;background:#3fb95011}
.occ-list{max-height:420px;overflow-y:auto}
.occ-row{display:grid;grid-template-columns:90px 70px 90px 70px 60px 32px;align-items:center;padding:6px 14px;border-bottom:1px solid #21262d;gap:4px;font-size:11px}
.occ-row:last-child{border-bottom:none}
.occ-row:hover{background:#21262d44}
.occ-lbl{font-size:9px;text-transform:uppercase;color:#8b949e;letter-spacing:.4px;padding:6px 14px 2px;background:#161b22}
.occ-date{color:#8b949e}
.occ-price{color:#e6edf3;font-weight:600}
.occ-pct-dn{color:#f85149;font-weight:700}
.occ-pct-up{color:#3fb950;font-weight:700}
.chart-btn{width:26px;height:26px;border-radius:5px;border:1px solid #30363d;background:transparent;color:#58a6ff;cursor:pointer;font-size:13px;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.chart-btn:hover{background:#1f6feb22;border-color:#1f6feb}
.empty-msg{padding:24px;text-align:center;color:#8b949e;font-size:11px}
.select-msg{padding:40px;text-align:center;color:#8b949e;font-size:13px}

/* Chart modal */
.modal-overlay{display:none;position:fixed;inset:0;background:#000000bb;z-index:1000;align-items:center;justify-content:center}
.modal-overlay.open{display:flex}
.modal{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:20px;width:min(680px,95vw);max-height:85vh;overflow:auto}
.modal-header{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:14px}
.modal-title{font-size:13px;font-weight:700;color:#e6edf3}
.modal-sub{font-size:11px;color:#8b949e;margin-top:2px}
.modal-close{background:none;border:none;color:#8b949e;cursor:pointer;font-size:18px;line-height:1;padding:0 2px}
.modal-close:hover{color:#e6edf3}
.chart-wrap{position:relative;height:300px}
.breach-label{font-size:11px;font-weight:700;text-align:center;margin-top:8px;padding:4px 10px;border-radius:4px;display:inline-block}
.breach-label-dn{background:#f8514922;color:#f85149;border:1px solid #f8514944}
.breach-label-up{background:#3fb95022;color:#3fb950;border:1px solid #3fb95044}
</style>
</head>
<body>
<header>
  <span class="logo">&#9711; NWO</span>
  <nav class="nav">
    <a href="/">&#128202; Signals</a>
    <a href="/morning-brief">&#128202; Morning Brief</a>
    <a href="/i-tool">&#128225; I-Tool</a>
    <a href="/paper/compare">&#127918; Paper</a>
    <a href="/wheel">&#127905; Wheel</a>
    <a href="/signals">&#128200; Signal Monitor</a>
    <a href="/alfred">&#128270; Alfred</a>
    <a href="/strat" class="active">&#128202; STRAT</a>
  </nav>
</header>

<div class="container">
  <h1>Strategy Threshold Analysis</h1>
  <p class="subtitle">Historical breach analysis &mdash; downside &#8805;15% within 90 days &bull; upside &#8805;10% within 30 days &bull; Feb 2020&ndash;Feb 2026</p>

  <!-- Status bar -->
  <div class="status-bar" id="statusBar">
    <div class="status-dot" id="statusDot"></div>
    <span id="statusText">Loading&hellip;</span>
    <div class="progress-bar" id="progressWrap" style="display:none">
      <div class="progress-fill" id="progressFill" style="width:0%"></div>
    </div>
    <button class="refresh-btn" onclick="triggerRefresh()">&#8635; Refresh</button>
  </div>

  <!-- Symbol picker -->
  <div class="picker-section" id="pickerSection" style="display:none">
    <div id="groupsContainer"></div>
    <div class="picker-group" style="margin-top:8px">
      <span class="group-label">Custom</span>
      <div class="chips">
        <div id="customChips"></div>
        <div class="add-sym-wrap">
          <input class="add-sym-input" id="addSymInput" type="text" placeholder="Add symbol&hellip;"
                 maxlength="10" onkeydown="if(event.key==='Enter')addSymbol()">
          <button class="add-sym-btn" onclick="addSymbol()">+ Add</button>
          <span class="add-sym-status" id="addSymStatus"></span>
        </div>
      </div>
    </div>
  </div>

  <!-- Main content -->
  <div id="mainContent">
    <div class="select-msg">Select a symbol above to view threshold breach analysis.</div>
  </div>
</div>

<!-- Chart modal -->
<div class="modal-overlay" id="chartModal" onclick="closeModal(event)">
  <div class="modal">
    <div class="modal-header">
      <div>
        <div class="modal-title" id="modalTitle"></div>
        <div class="modal-sub" id="modalSub"></div>
      </div>
      <button class="modal-close" onclick="closeModal()">&#10005;</button>
    </div>
    <div class="chart-wrap">
      <canvas id="chartCanvas"></canvas>
    </div>
    <div style="text-align:center;margin-top:8px">
      <span class="breach-label" id="breachLabel"></span>
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<script>
// -- State --
const BUILTIN_GROUPS = BUILTIN_GROUPS_PLACEHOLDER;
const INDEX_SYMS     = new Set(INDEX_SYMS_PLACEHOLDER);

let _data     = {};   // sym -> {downside: {occurrences:[]}, upside: {occurrences:[]}}
let _custom   = [];   // custom symbol names
let _selSym   = null;
let _selPer   = {dn: '6yr', up: '6yr'};
let _chart    = null;
let _polling  = null;

// -- Init --
async function init() {
  await pollStatus();
}

async function pollStatus() {
  try {
    const s = await fetch('/api/strat/status').then(r => r.json());
    const dot  = document.getElementById('statusDot');
    const txt  = document.getElementById('statusText');
    const prog = document.getElementById('progressWrap');
    const fill = document.getElementById('progressFill');

    if (s.computing) {
      dot.className = 'status-dot computing';
      txt.textContent = `Computing... ${s.progress}%`;
      prog.style.display = 'block';
      fill.style.width   = s.progress + '%';
      if (!_polling) _polling = setInterval(pollStatus, 2000);
    } else {
      dot.className = 'status-dot';
      const ts = s.computed_at ? new Date(s.computed_at + 'Z').toLocaleString() : '--';
      txt.textContent = `Ready - computed ${ts}`;
      prog.style.display = 'none';
      if (_polling) { clearInterval(_polling); _polling = null; }
      renderPicker(s.symbols);
      document.getElementById('pickerSection').style.display = 'block';
    }
  } catch(e) {
    document.getElementById('statusText').textContent = 'Error connecting to server';
  }
}

async function triggerRefresh() {
  await fetch('/api/strat/refresh', {method:'POST'});
  document.getElementById('statusText').textContent = 'Refreshing...';
  setTimeout(pollStatus, 500);
}

// -- Symbol picker --
function renderPicker(availableSyms) {
  const avail = new Set(availableSyms || []);
  const gc = document.getElementById('groupsContainer');
  gc.innerHTML = '';

  BUILTIN_GROUPS.forEach(([label, syms]) => {
    const row = document.createElement('div');
    row.className = 'picker-group';
    row.innerHTML = `<span class="group-label">${label}</span><div class="chips" id="grp-${label.replace(/\\s/g,'_')}"></div>`;
    gc.appendChild(row);
    const chips = row.querySelector('.chips');
    syms.forEach(sym => {
      const c = document.createElement('button');
      c.className = 'chip' + (_selSym === sym ? ' active' : '');
      c.id = 'chip-' + sym;
      c.textContent = sym;
      c.onclick = () => selectSymbol(sym);
      if (!avail.has(sym)) c.style.opacity = '0.4';
      chips.appendChild(c);
    });
  });

  // Custom chips
  _custom = availableSyms.filter(s => !BUILTIN_GROUPS.flatMap(([,ss])=>ss).includes(s));
  renderCustomChips();
}

function renderCustomChips() {
  const cc = document.getElementById('customChips');
  cc.innerHTML = '';
  _custom.forEach(sym => {
    const c = document.createElement('button');
    c.className = 'chip chip-custom' + (_selSym === sym ? ' active' : '');
    c.id = 'chip-' + sym;
    c.innerHTML = `${sym} <button class="chip-del" onclick="removeSymbol('${sym}',event)" title="Remove">&times;</button>`;
    c.onclick = (e) => { if(e.target.classList.contains('chip-del')) return; selectSymbol(sym); };
    cc.appendChild(c);
  });
}

async function selectSymbol(sym) {
  _selSym = sym;
  document.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
  const chip = document.getElementById('chip-' + sym);
  if (chip) chip.classList.add('active');

  document.getElementById('mainContent').innerHTML = '<div class="select-msg">Loading...</div>';

  if (!_data[sym]) {
    try {
      const r = await fetch('/api/strat/data?symbol=' + sym);
      if (!r.ok) { document.getElementById('mainContent').innerHTML = '<div class="select-msg">Data not available for ' + sym + '</div>'; return; }
      _data[sym] = await r.json();
    } catch(e) {
      document.getElementById('mainContent').innerHTML = '<div class="select-msg">Failed to load data.</div>';
      return;
    }
  }
  renderSymbol(sym);
}

// -- Render analysis --
function renderSymbol(sym) {
  const d = _data[sym];
  if (!d || d.error) {
    document.getElementById('mainContent').innerHTML = `<div class="select-msg">No data available for ${sym}.</div>`;
    return;
  }

  const earliest = d.earliest_date ? new Date(d.earliest_date) : null;

  const html = `
    <div class="sym-header">
      <span class="sym-title">${sym}</span>
      <span class="sym-meta">${INDEX_SYMS.has(sym) ? 'Index &mdash; last-day breach rule' : 'Equity &mdash; first-breach rule'} &bull; Earliest data: ${d.earliest_date || '--'}</span>
      <button class="csv-btn" onclick="downloadCSV('${sym}')" title="Download all occurrences as CSV">&#11015; CSV</button>
    </div>
    <div class="panels">
      ${renderPanel(sym, 'dn', d.downside, earliest)}
      ${renderPanel(sym, 'up', d.upside, earliest)}
    </div>`;
  document.getElementById('mainContent').innerHTML = html;
}

function periodCutoff(p) {
  return {'1yr': '2025-02-01', '3yr': '2023-02-01', '6yr': '2020-02-01'}[p];
}

function hasPeriod(earliest, period) {
  if (!earliest) return true;
  return earliest <= new Date(periodCutoff(period));
}

function filterByPeriod(occs, period) {
  const cutoff = periodCutoff(period);
  return occs.filter(o => o.start_date >= cutoff);
}

function renderPanel(sym, dir, analysis, earliest) {
  if (!analysis) return '<div class="panel">No data</div>';
  const isDown = dir === 'dn';
  const occs   = analysis.occurrences || [];
  const thr    = isDown ? '&ge;15% drop' : '&ge;10% rise';
  const days   = isDown ? '90 cal. days' : '30 cal. days';
  const sel    = _selPer[dir];

  const counts = ['1yr','3yr','6yr'].map(p => {
    const valid = hasPeriod(earliest, p);
    const cnt   = valid ? filterByPeriod(occs, p).length : null;
    return `<tr>
      <td>${p === '1yr' ? '1 Year' : p === '3yr' ? '3 Years' : '6 Years'}</td>
      <td><span class="${valid ? 'count-num' : 'count-na'}">${valid ? cnt : 'N/A'}</span></td>
      <td style="color:#8b949e;font-size:10px">${valid ? (p==='1yr'?'Feb 25-Feb 26':p==='3yr'?'Feb 23-Feb 26':'Feb 20-Feb 26') : 'insufficient history'}</td>
    </tr>`;
  }).join('');

  const filtered = filterByPeriod(occs, sel);
  const rows = filtered.length === 0
    ? `<div class="empty-msg">No breaches in ${sel} period</div>`
    : `<div class="occ-lbl">
         <span>Start Date</span> &nbsp;&nbsp;
         <span>Start $</span> &nbsp;&nbsp;&nbsp;
         <span>Breach Date</span> &nbsp;
         <span>Breach $</span> &nbsp;&nbsp;
         <span>%</span>
       </div>` +
      filtered.map((o, i) => {
        const pctFmt = (isDown ? '' : '+') + o.pct_change.toFixed(1) + '%';
        const idxInAll = occs.indexOf(o);
        return `<div class="occ-row">
          <span class="occ-date">${o.start_date}</span>
          <span class="occ-price">$${o.start_price.toFixed(2)}</span>
          <span class="occ-date">${o.breach_date}</span>
          <span class="occ-price">$${o.breach_price.toFixed(2)}</span>
          <span class="${isDown ? 'occ-pct-dn' : 'occ-pct-up'}">${pctFmt}</span>
          <button class="chart-btn" onclick="openChart('${sym}','${dir}',${idxInAll})" title="View chart">&#128200;</button>
        </div>`;
      }).join('');

  const pfBtns = ['1yr','3yr','6yr'].map(p =>
    `<button class="pf-btn ${sel===p ? (isDown?'active-dn':'active-up') : ''}"
             onclick="setPeriod('${dir}','${p}','${sym}')">${p}</button>`
  ).join('');

  return `<div class="panel panel-${dir}">
    <div class="panel-header">
      <div>
        <div class="panel-title">${isDown ? '&#8595; Downside Threshold Breach' : '&#8593; Upside Threshold Breach'}</div>
        <div class="panel-sub">${thr} within ${days} from each closing date</div>
      </div>
    </div>
    <table class="counts-table">
      <tr><th>Period</th><th>Breaches</th><th>Range</th></tr>
      ${counts}
    </table>
    <div class="period-filter">${pfBtns}</div>
    <div class="occ-list">${rows}</div>
  </div>`;
}

function setPeriod(dir, period, sym) {
  _selPer[dir] = period;
  renderSymbol(sym);
}

// -- Custom symbols --
async function addSymbol() {
  const inp = document.getElementById('addSymInput');
  const st  = document.getElementById('addSymStatus');
  const sym = inp.value.trim().toUpperCase();
  if (!sym) return;
  inp.value = '';
  st.textContent = 'Validating...';
  st.style.color = '#8b949e';
  try {
    const r = await fetch('/api/strat/add_symbol', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({symbol: sym})
    });
    const j = await r.json();
    if (j.error) {
      st.textContent = j.error === 'symbol_not_found_in_yfinance' ? sym + ' not found' : j.error;
      st.style.color = '#f85149';
    } else if (j.status === 'already_exists') {
      st.textContent = sym + ' already tracked';
      st.style.color = '#d29922';
    } else {
      st.textContent = sym + ' added - computing...';
      st.style.color = '#3fb950';
      _custom.push(sym);
      renderCustomChips();
      if (!_polling) _polling = setInterval(pollStatus, 3000);
    }
  } catch(e) {
    st.textContent = 'Error'; st.style.color = '#f85149';
  }
  setTimeout(() => st.textContent = '', 4000);
}

async function removeSymbol(sym, evt) {
  evt.stopPropagation();
  const r = await fetch('/api/strat/remove_symbol/' + sym, {method: 'DELETE'});
  const j = await r.json();
  if (j.status === 'removed') {
    _custom = _custom.filter(s => s !== sym);
    delete _data[sym];
    renderCustomChips();
    if (_selSym === sym) {
      _selSym = null;
      document.getElementById('mainContent').innerHTML = '<div class="select-msg">Select a symbol above to view threshold breach analysis.</div>';
    }
  }
}

// -- CSV export --
function downloadCSV(sym) {
  const d = _data[sym];
  if (!d) return;

  const rows = [
    ['Symbol','Direction','Threshold','Window (cal days)','Start Date','Start Price ($)','Breach Date','Breach Price ($)','% Change']
  ];

  (d.downside?.occurrences || []).forEach(o => {
    rows.push([sym, 'Downside', '15%', 90,
      o.start_date, o.start_price.toFixed(2),
      o.breach_date, o.breach_price.toFixed(2),
      o.pct_change.toFixed(2)]);
  });

  (d.upside?.occurrences || []).forEach(o => {
    rows.push([sym, 'Upside', '10%', 30,
      o.start_date, o.start_price.toFixed(2),
      o.breach_date, o.breach_price.toFixed(2),
      '+' + o.pct_change.toFixed(2)]);
  });

  const csv = rows.map(r => r.map(v => `"${v}"`).join(',')).join('\\n');
  const blob = new Blob([csv], {type: 'text/csv'});
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href     = url;
  a.download = `STRAT_${sym}_${new Date().toISOString().slice(0,10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

// -- Chart modal --

// Register vertical-line plugin once at load time (Chart.js 4 throws if re-registered)
const _vertLinePlugin = {
  id: 'vertLine',
  beforeDraw(chart, _, opts) {
    if (opts == null || opts.xLabel == null) return;
    const {ctx, chartArea, scales} = chart;
    // getPixelForValue is the correct Chart.js 4 API for category scales
    const x = scales.x.getPixelForValue(opts.xLabel);
    if (x == null || isNaN(x)) return;
    ctx.save();
    ctx.strokeStyle = opts.color || '#8b949e';
    ctx.lineWidth = 1.5;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(x, chartArea.top);
    ctx.lineTo(x, chartArea.bottom);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.restore();
  }
};
// Guard against double-registration if page hot-reloads
if (!Chart.registry.plugins.get('vertLine')) Chart.register(_vertLinePlugin);

function openChart(sym, dir, idx) {
  const occ = (_data[sym]?.[dir === 'dn' ? 'downside' : 'upside']?.occurrences || [])[idx];
  if (!occ) return;

  const isDown = dir === 'dn';
  const thr    = isDown ? -15 : 10;
  const thrClr = isDown ? '#f85149' : '#3fb950';
  const window_days = isDown ? 90 : 30;

  document.getElementById('modalTitle').textContent =
    `${sym} - ${isDown ? 'Downside' : 'Upside'} Threshold Breach`;
  document.getElementById('modalSub').textContent =
    `Start: ${occ.start_date} ($${occ.start_price.toFixed(2)}) -> ${window_days}-day window | Breach: ${occ.breach_date} ($${occ.breach_price.toFixed(2)}, ${occ.pct_change > 0 ? '+' : ''}${occ.pct_change.toFixed(1)}%)`;

  const lbl = document.getElementById('breachLabel');
  lbl.className = 'breach-label ' + (isDown ? 'breach-label-dn' : 'breach-label-up');
  lbl.textContent = isDown
    ? `[!] Downside Threshold Breach - ${occ.pct_change.toFixed(1)}% on ${occ.breach_date}`
    : `[+] Upside Threshold Breach - +${occ.pct_change.toFixed(1)}% on ${occ.breach_date}`;

  const pts = occ.window_prices || [];
  const labels    = pts.map(p => p.d);
  const values    = pts.map(p => p.p);
  const thrLine   = pts.map(() => thr);
  const breachIdx = pts.findIndex(p => p.d === occ.breach_date);
  const breachPoints = pts.map((p, i) => i === breachIdx ? p.p : null);

  document.getElementById('chartModal').classList.add('open');

  if (_chart) { _chart.destroy(); _chart = null; }

  const ctx = document.getElementById('chartCanvas').getContext('2d');
  _chart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [
        {
          label: `${sym} % change`,
          data: values,
          borderColor: '#58a6ff',
          backgroundColor: 'transparent',
          borderWidth: 1.5,
          pointRadius: 0,
          tension: 0.1,
          order: 1,
        },
        {
          label: `${isDown ? '-15%' : '+10%'} threshold`,
          data: thrLine,
          borderColor: thrClr,
          backgroundColor: 'transparent',
          borderWidth: 1,
          borderDash: [5, 4],
          pointRadius: 0,
          order: 2,
        },
        {
          label: 'Breach point',
          data: breachPoints,
          borderColor: thrClr,
          backgroundColor: thrClr,
          pointRadius: pts.map((_, i) => i === breachIdx ? 6 : 0),
          pointHoverRadius: 8,
          showLine: false,
          order: 0,
        }
      ]
    },
    options: {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {labels: {color: '#8b949e', boxWidth: 12, font: {size: 10}}},
        tooltip: {
          callbacks: {
            label: ctx => `${ctx.dataset.label}: ${ctx.parsed.y != null ? ctx.parsed.y.toFixed(2) + '%' : ''}`
          }
        },
        vertLine: {xLabel: breachIdx >= 0 ? labels[breachIdx] : null, color: thrClr},
      },
      scales: {
        x: {
          ticks: {color: '#8b949e', maxTicksLimit: 8, font: {size: 9}},
          grid:  {color: '#21262d'},
        },
        y: {
          ticks: {
            color: '#8b949e',
            font:  {size: 10},
            callback: v => v.toFixed(1) + '%'
          },
          grid: {color: '#21262d'},
        }
      }
    }
  });
}

function closeModal(e) {
  if (e && e.target !== document.getElementById('chartModal')) return;
  document.getElementById('chartModal').classList.remove('open');
  if (_chart) { _chart.destroy(); _chart = null; }
}

// -- Boot --
init();
</script>
</body>
</html>"""

# Inject JS constants
_BUILTIN_GROUPS_JS = json.dumps([
    [label, list(syms.keys())]
    for label, syms in STRAT_SYMBOLS.items()
])
_INDEX_SYMS_JS = json.dumps(list(INDEX_SYMBOLS))

_PAGE = _PAGE.replace("BUILTIN_GROUPS_PLACEHOLDER", _BUILTIN_GROUPS_JS)
_PAGE = _PAGE.replace("INDEX_SYMS_PLACEHOLDER",     _INDEX_SYMS_JS)


@strat_router.get("/strat", response_class=HTMLResponse)
def strat_page():
    return HTMLResponse(_PAGE, media_type="text/html; charset=utf-8")
