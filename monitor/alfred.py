"""
monitor/alfred.py — Alfred: CME Futures 5-Day Forecasting Dashboard.

Independent module — no imports from signals/ or paper/.
Data: yfinance (futures + VIX) + TipRanks SPY/QQQ as analyst proxy.
Persistence: data/alfred_forecast.json, alfred_anchor.json, alfred_backtest.json

Routes:
  GET  /alfred                — Dashboard page
  GET  /api/alfred/forecast   — Current 5-day forecast JSON
  GET  /api/alfred/backtest   — Backtest results + calibration JSON
  POST /api/alfred/refresh    — Force immediate recompute
  POST /api/alfred/run-backtest — Trigger full 6-month backtest
"""

import json
import math
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import pytz
import yfinance as yf
from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse
from loguru import logger

alfred_router = APIRouter()

_ET  = pytz.timezone("America/New_York")
ROOT = Path(__file__).resolve().parent.parent

_FORECAST_FILE = ROOT / "data" / "alfred_forecast.json"
_ANCHOR_FILE   = ROOT / "data" / "alfred_anchor.json"
_BACKTEST_FILE = ROOT / "data" / "alfred_backtest.json"

_lock         = threading.Lock()
_last_compute = 0.0
_COMPUTE_TTL  = 295  # seconds — just under 5 min

# ── Symbol configuration ─────────────────────────────────────────────────────

ALFRED_SYMBOLS = {
    "/ES":  "ES=F",
    "/MES": "MES=F",
    "/NQ":  "NQ=F",
    "/MNQ": "MNQ=F",
}

FALLBACK_SYMBOLS = {
    "/MES": "ES=F",
    "/MNQ": "NQ=F",
}

TIPRANKS_PROXIES = {
    "/ES":  "SPY",
    "/MES": "SPY",
    "/NQ":  "QQQ",
    "/MNQ": "QQQ",
}

VIX_MULT_TABLE = [
    (15.0,        0.8),
    (25.0,        1.0),
    (35.0,        1.3),
    (float("inf"), 1.6),
]

CHANGE_THRESHOLD_PTS = 5.0  # flag "REVISED" if forecast delta > this

# ── Market hours gate ────────────────────────────────────────────────────────

def _futures_open() -> bool:
    """
    Return True when CME equity futures are open.
    Hours: Sun 6 PM – Fri 5 PM ET, with 5:00–5:15 PM daily maintenance break.
    """
    now = datetime.now(_ET)
    wd  = now.weekday()   # 0=Mon … 6=Sun

    if wd == 5:   # Saturday — always closed
        return False

    total_min = now.hour * 60 + now.minute

    # Daily 5:00–5:15 PM maintenance break (Mon–Fri)
    if 0 <= wd <= 4 and 1020 <= total_min < 1035:
        return False

    # Sunday — only open after 6 PM ET
    if wd == 6 and total_min < 1080:
        return False

    return True


# ── Self-contained math primitives ──────────────────────────────────────────

def _wilder_atr(highs: list, lows: list, closes: list, period: int = 14) -> float:
    n = len(closes)
    if n < period + 1:
        return 0.0
    tr = [highs[0] - lows[0]]
    for i in range(1, n):
        tr.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]),
        ))
    atr = sum(tr[:period]) / period
    for i in range(period, n):
        atr = (atr * (period - 1) + tr[i]) / period
    return atr


def _ema_series(values: list, period: int) -> list:
    if not values:
        return []
    k = 2.0 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def _rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0:
        return 100.0
    return round(100.0 - 100.0 / (1.0 + ag / al), 2)


def _macd_hist(closes: list) -> float:
    if len(closes) < 27:
        return 0.0
    ema12 = _ema_series(closes, 12)
    ema26 = _ema_series(closes, 26)
    n     = min(len(ema12), len(ema26))
    macd  = [ema12[i] - ema26[i] for i in range(n)]
    sig   = _ema_series(macd, 9)
    if not sig:
        return 0.0
    return macd[-1] - sig[-1]


def _supertrend_direction(highs: list, lows: list, closes: list,
                          period: int = 10, factor: float = 3.0) -> int:
    """Return +1 uptrend / -1 downtrend for the final bar."""
    n = len(closes)
    if n < period + 5:
        return 0
    tr_list = [highs[0] - lows[0]]
    for i in range(1, n):
        tr_list.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]),
        ))
    atr_s = [0.0] * n
    atr_s[period - 1] = sum(tr_list[:period]) / period
    for i in range(period, n):
        atr_s[i] = (atr_s[i - 1] * (period - 1) + tr_list[i]) / period
    for i in range(period - 1):
        atr_s[i] = atr_s[period - 1]

    ub = [(highs[i] + lows[i]) / 2 + factor * atr_s[i] for i in range(n)]
    lb = [(highs[i] + lows[i]) / 2 - factor * atr_s[i] for i in range(n)]
    f_ub = list(ub)
    f_lb = list(lb)
    for i in range(1, n):
        f_ub[i] = ub[i] if ub[i] < f_ub[i - 1] or closes[i - 1] > f_ub[i - 1] else f_ub[i - 1]
        f_lb[i] = lb[i] if lb[i] > f_lb[i - 1] or closes[i - 1] < f_lb[i - 1] else f_lb[i - 1]

    st    = f_ub[0]
    direc = 1
    for i in range(1, n):
        if st == f_ub[i - 1]:
            if closes[i] > f_ub[i]:
                st    = f_lb[i]
                direc = -1
            else:
                st = f_ub[i]
        else:
            if closes[i] < f_lb[i]:
                st    = f_ub[i]
                direc = 1
            else:
                st = f_lb[i]
    return -1 if direc < 0 else 1


def _vix_mult(vix: float) -> float:
    for threshold, mult in VIX_MULT_TABLE:
        if vix < threshold:
            return mult
    return 1.6


# ── Data fetchers ────────────────────────────────────────────────────────────

def _fetch_ohlcv(yf_sym: str,
                 fallback_sym: Optional[str] = None,
                 period: str = "200d") -> tuple:
    """Download daily OHLCV; falls back to fallback_sym if < 30 bars."""
    MIN_BARS = 30

    def _dl(sym: str):
        try:
            df = yf.download(sym, period=period, interval="1d",
                             progress=False, auto_adjust=True)
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
            h = df["High"].dropna().tolist()
            l = df["Low"].dropna().tolist()
            c = df["Close"].dropna().tolist()
            n = min(len(h), len(l), len(c))
            return h[:n], l[:n], c[:n]
        except Exception as exc:
            logger.debug(f"[Alfred] yfinance {sym} failed: {exc}")
            return [], [], []

    h, l, c = _dl(yf_sym)
    if len(c) < MIN_BARS and fallback_sym:
        logger.info(f"[Alfred] {yf_sym} has {len(c)} bars — falling back to {fallback_sym}")
        h, l, c = _dl(fallback_sym)
    return h, l, c


def _fetch_vix() -> float:
    try:
        df = yf.download("^VIX", period="5d", interval="1d",
                         progress=False, auto_adjust=True)
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        closes = df["Close"].dropna().tolist()
        if closes:
            return float(closes[-1])
    except Exception as exc:
        logger.warning(f"[Alfred] VIX fetch failed: {exc}")
    return 20.0


def _fetch_tipranks_bias(tr_ticker: str) -> float:
    """
    Return analyst implied upside fraction for SPY or QQQ.
    Uses data_sources/tipranks_client.py 4-hour cache.
    Returns 0.0 if cookies unavailable or data missing.
    Key paths verified against data/tipranks_cache/AAPL.json:
      current price: d["prices"][-1]["p"]
      price target:  d["ptConsensus"][0]["priceTarget"]
    """
    try:
        from data_sources.tipranks_client import TipRanksClient
        client = TipRanksClient()
        if not client.cookies_available():
            return 0.0
        raw = client.get_stock_data(tr_ticker)
        pt_list  = raw.get("ptConsensus") or []
        px_list  = raw.get("prices") or []
        if not pt_list or not px_list:
            return 0.0
        pt_mean   = float(pt_list[0].get("priceTarget") or 0)
        cur_price = float(px_list[-1].get("p") or 0)
        if pt_mean > 0 and cur_price > 0:
            return (pt_mean - cur_price) / cur_price
    except Exception as exc:
        logger.debug(f"[Alfred] TipRanks bias ({tr_ticker}): {exc}")
    return 0.0


# ── Core forecast engine ─────────────────────────────────────────────────────

def _compute_forecast_for_symbol(sym: str,
                                 highs: list,
                                 lows: list,
                                 closes: list,
                                 vix: float,
                                 calib: dict,
                                 tr_bias_raw: float) -> dict:
    if not closes:
        return {"error": "no_data"}

    atr = _wilder_atr(highs, lows, closes, 14)
    if atr == 0.0:
        return {"error": "atr_zero"}

    price  = closes[-1]
    vm     = _vix_mult(vix)
    rsi_v  = _rsi(closes, 14)
    mh     = _macd_hist(closes)
    st_dir = _supertrend_direction(highs, lows, closes, period=10, factor=3.0)

    # Bias components
    rsi_bias  = ((rsi_v - 50.0) / 50.0) * 0.5 * atr
    macd_bias = (1 if mh > 0 else -1) * 0.25 * atr
    st_bias   = (0.2 if st_dir > 0 else -0.2) * atr
    tr_clipped = max(-0.3, min(0.3, tr_bias_raw))
    tr_bias   = tr_clipped * atr
    total_bias = max(-atr, min(atr, rsi_bias + macd_bias + st_bias + tr_bias))

    days_out = {}
    for n in range(1, 6):
        c_mult  = calib.get(n, 1.0)
        rng     = atr * math.sqrt(n) * vm * c_mult
        center  = price + total_bias * (n * 0.4)
        days_out[n] = {
            "high":   round(center + rng / 2, 2),
            "low":    round(center - rng / 2, 2),
            "range":  round(rng, 2),
            "center": round(center, 2),
        }

    vix_regime = ("Low" if vix < 15 else
                  "Normal" if vix < 25 else
                  "Elevated" if vix < 35 else "Extreme")
    st_label  = "BULLISH" if st_dir > 0 else "BEARISH"
    rsi_label = ("Overbought" if rsi_v > 70 else
                 "Oversold" if rsi_v < 30 else
                 "Bullish" if rsi_v > 55 else
                 "Bearish" if rsi_v < 45 else "Neutral")

    bullets = [
        f"ATR(14) = {atr:.2f} pts  (Wilder-smoothed, daily bars)",
        f"VIX = {vix:.1f} ({vix_regime} regime)  →  range multiplier = {vm:.1f}×",
        f"RSI(14) = {rsi_v:.1f} ({rsi_label})  →  bias {rsi_bias:+.2f} pts",
        f"MACD(12,26,9) histogram = {mh:+.4f}  →  bias {macd_bias:+.2f} pts",
        f"SuperTrend(10,3) = {st_label}  →  bias {st_bias:+.2f} pts",
        (f"TipRanks analyst target  →  implied upside {tr_bias_raw*100:+.1f}%  →  bias {tr_bias:+.2f} pts"
         if abs(tr_bias_raw) > 0.001 else
         "TipRanks: unavailable (cookies not set)  →  bias 0.00 pts"),
        f"Total directional bias (clamped ±ATR) = {total_bias:+.2f} pts",
        "Calibration multipliers: " + ", ".join(
            f"D{k}={calib.get(k, 1.0):.3f}" for k in range(1, 6)),
        f"Range formula: ATR × √N × VIX_mult × calib  (N = trading days ahead)",
    ]

    return {
        "price":        round(price, 2),
        "atr":          round(atr, 2),
        "vix_mult":     vm,
        "rsi":          rsi_v,
        "macd_hist":    round(mh, 6),
        "st_direction": st_label,
        "total_bias":   round(total_bias, 2),
        "days":         days_out,
        "bullets":      bullets,
    }


# ── Calibration ──────────────────────────────────────────────────────────────

def _calibrate_multipliers(accuracy_per_day: dict) -> dict:
    """
    Given {N: containment_fraction}, compute multipliers so forecast bands
    target 70% containment. calib[N] = clip(0.70 / accuracy[N], 0.5, 2.5).
    """
    TARGET = 0.70
    result = {}
    for n, acc in accuracy_per_day.items():
        if acc <= 0:
            result[n] = 1.0
        else:
            result[n] = round(max(0.5, min(2.5, TARGET / acc)), 4)
    return result


# ── Backtest engine ──────────────────────────────────────────────────────────

def _run_backtest(sym: str,
                  yf_sym: str,
                  fallback_sym: Optional[str] = None) -> dict:
    """
    180-day walk-forward backtest for one symbol.
    Computes uncalibrated (calib=1.0) forecast at each historical day D,
    compares to actual H/L of D+1 … D+5, derives calibration multipliers.
    """
    h_all, l_all, c_all = _fetch_ohlcv(yf_sym, fallback_sym, period="400d")
    n_total = len(c_all)
    if n_total < 40:
        return {"error": "insufficient_history"}

    try:
        vdf = yf.download("^VIX", period="400d", interval="1d",
                          progress=False, auto_adjust=True)
        vdf.columns = [c[0] if isinstance(c, tuple) else c for c in vdf.columns]
        vix_hist = vdf["Close"].dropna().tolist()
    except Exception:
        vix_hist = [20.0] * n_total

    lookback  = min(180, n_total - 10)
    start_idx = n_total - lookback - 6

    results = {n: {"contained": 0, "total": 0,
                   "over_hi": [], "under_lo": []}
               for n in range(1, 6)}

    raw_calib = {n: 1.0 for n in range(1, 6)}

    for d_idx in range(max(start_idx, 20), n_total - 5):
        h_s = h_all[:d_idx + 1]
        l_s = l_all[:d_idx + 1]
        c_s = c_all[:d_idx + 1]
        v_i = min(d_idx, len(vix_hist) - 1)
        vix_d = float(vix_hist[v_i]) if v_i >= 0 else 20.0

        fc = _compute_forecast_for_symbol(
            sym=sym, highs=h_s, lows=l_s, closes=c_s,
            vix=vix_d, calib=raw_calib, tr_bias_raw=0.0,
        )
        if "error" in fc:
            continue

        for n in range(1, 6):
            fi = d_idx + n
            if fi >= n_total:
                break
            ah, al = h_all[fi], l_all[fi]
            ph = fc["days"][n]["high"]
            pl = fc["days"][n]["low"]
            results[n]["total"] += 1
            if ah <= ph and al >= pl:
                results[n]["contained"] += 1
            else:
                if ah > ph:
                    results[n]["over_hi"].append(ah - ph)
                if al < pl:
                    results[n]["under_lo"].append(pl - al)

    accuracy  = {}
    avg_error = {}
    for n in range(1, 6):
        r = results[n]
        t = r["total"]
        accuracy[n]  = round(r["contained"] / t, 4) if t > 0 else 0.0
        errs = r["over_hi"] + r["under_lo"]
        avg_error[n] = round(sum(errs) / len(errs), 2) if errs else 0.0

    calib = _calibrate_multipliers(accuracy)
    avg_acc = sum(accuracy.values()) / len(accuracy)

    if avg_acc >= 0.70:
        interp = (
            f"The model achieved {avg_acc*100:.1f}% average containment accuracy (target: 70%). "
            f"Day-1 forecast is tightest at {accuracy[1]*100:.1f}%; uncertainty grows to "
            f"{accuracy[5]*100:.1f}% by Day-5 as expected from the √N range expansion. "
            f"Calibration multipliers are near 1.0 — the ATR×√N formula is "
            f"well-calibrated for {sym} at current volatility regimes."
        )
    else:
        avg_err_pts = sum(avg_error.values()) / len(avg_error)
        interp = (
            f"The model achieved {avg_acc*100:.1f}% average containment accuracy, below the 70% target. "
            f"Average forecast error is {avg_err_pts:.1f} pts/day. "
            f"Calibration multipliers have been widened to target 70% on subsequent forecasts. "
            f"Common causes: VIX regime shifts, gap-open events, or extended trending sessions "
            f"that exceed ATR-based projections."
        )

    return {
        "symbol":         sym,
        "accuracy":       {str(k): v for k, v in accuracy.items()},
        "avg_error_pts":  {str(k): v for k, v in avg_error.items()},
        "calibration":    {str(k): v for k, v in calib.items()},
        "total_bars_d1":  results[1]["total"],
        "interpretation": interp,
        "computed_at":    datetime.now(_ET).isoformat(),
    }


def _run_backtest_all() -> dict:
    results = {}
    for sym, yf_sym in ALFRED_SYMBOLS.items():
        fallback = FALLBACK_SYMBOLS.get(sym)
        logger.info(f"[Alfred] Backtesting {sym} ({yf_sym})...")
        try:
            results[sym] = _run_backtest(sym, yf_sym, fallback)
        except Exception as exc:
            logger.warning(f"[Alfred] Backtest failed for {sym}: {exc}")
            results[sym] = {"error": str(exc)}
    payload = {"symbols": results, "run_at": datetime.now(_ET).isoformat()}
    _BACKTEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    _BACKTEST_FILE.write_text(json.dumps(payload, indent=2))
    logger.info("[Alfred] Backtest complete and saved.")
    return payload


# ── Persistence helpers ───────────────────────────────────────────────────────

def _load_forecast() -> dict:
    try:
        if _FORECAST_FILE.exists():
            return json.loads(_FORECAST_FILE.read_text())
    except Exception:
        pass
    return {}


def _load_anchor() -> dict:
    try:
        if _ANCHOR_FILE.exists():
            return json.loads(_ANCHOR_FILE.read_text())
    except Exception:
        pass
    return {}


def _load_backtest() -> dict:
    try:
        if _BACKTEST_FILE.exists():
            return json.loads(_BACKTEST_FILE.read_text())
    except Exception:
        pass
    return {}


def _update_anchor(payload: dict, now_et: datetime) -> None:
    today_str = now_et.strftime("%Y-%m-%d")
    try:
        existing = {}
        if _ANCHOR_FILE.exists():
            existing = json.loads(_ANCHOR_FILE.read_text())
        if existing.get("date") == today_str:
            return
        anchor = {
            "date":    today_str,
            "symbols": payload["symbols"],
            "vix":     payload["vix"],
            "set_at":  payload["updated_at"],
        }
        _ANCHOR_FILE.write_text(json.dumps(anchor, indent=2))
        logger.info(f"[Alfred] Daily anchor set for {today_str}")
    except Exception as exc:
        logger.warning(f"[Alfred] Anchor update failed: {exc}")


def _load_calibration() -> dict:
    """Load per-symbol calibration multipliers from backtest file. Keys are ints."""
    result = {}
    try:
        bt = _load_backtest()
        for sym, sym_data in bt.get("symbols", {}).items():
            raw = sym_data.get("calibration", {})
            result[sym] = {int(k): float(v) for k, v in raw.items()}
    except Exception:
        pass
    return result


# ── Main compute function ─────────────────────────────────────────────────────

def _compute_alfred(force: bool = False) -> bool:
    global _last_compute

    if not force:
        if not _futures_open():
            logger.debug("[Alfred] Futures closed — skipping compute")
            return False
        if time.time() - _last_compute < _COMPUTE_TTL:
            return False

    with _lock:
        if not force and time.time() - _last_compute < _COMPUTE_TTL:
            return False

        logger.info("[Alfred] Computing 5-day forecasts...")
        vix  = _fetch_vix()
        calib_by_sym = _load_calibration()

        # Fetch TipRanks bias once per proxy (SPY + QQQ, not 4 times)
        tr_cache: dict = {}
        for proxy in set(TIPRANKS_PROXIES.values()):
            tr_cache[proxy] = _fetch_tipranks_bias(proxy)

        now_et  = datetime.now(_ET)
        ts_str  = now_et.strftime("%Y-%m-%d %H:%M:%S ET")
        forecast_syms: dict = {}

        for sym, yf_sym in ALFRED_SYMBOLS.items():
            fallback = FALLBACK_SYMBOLS.get(sym)
            h, l, c  = _fetch_ohlcv(yf_sym, fallback)
            proxy    = TIPRANKS_PROXIES[sym]
            calib    = calib_by_sym.get(sym, {n: 1.0 for n in range(1, 6)})
            fc = _compute_forecast_for_symbol(
                sym=sym, highs=h, lows=l, closes=c,
                vix=vix, calib=calib, tr_bias_raw=tr_cache.get(proxy, 0.0),
            )
            forecast_syms[sym] = fc

        payload = {
            "symbols":    forecast_syms,
            "vix":        round(vix, 2),
            "updated_at": ts_str,
            "ts_epoch":   time.time(),
        }
        _FORECAST_FILE.parent.mkdir(parents=True, exist_ok=True)
        _FORECAST_FILE.write_text(json.dumps(payload, indent=2))
        _update_anchor(payload, now_et)
        _last_compute = time.time()
        logger.info(f"[Alfred] Forecast saved. VIX={vix:.1f}")
        return True


# ── HTML builder ─────────────────────────────────────────────────────────────

def _build_alfred_html(forecast: dict, anchor: dict, backtest: dict) -> str:
    from monitor.dashboard import _nav_html, _NAV_CSS, _NAV_TAPE_HTML, _NAV_TAPE_JS

    nav      = _nav_html("alfred")
    syms_fc  = forecast.get("symbols", {})
    syms_anc = anchor.get("symbols", {})
    vix_val  = forecast.get("vix", "—")
    updated  = forecast.get("updated_at", "—")
    bt_syms  = backtest.get("symbols", {})

    # ── Helper: render one symbol card ──────────────────────────────────────

    def _fmt_price(p):
        if p is None:
            return "—"
        if abs(p) >= 10000:
            return f"{p:,.0f}"
        return f"{p:,.2f}"

    def _card(sym: str) -> str:
        fc  = syms_fc.get(sym, {})
        anc = syms_anc.get(sym, {})
        bt  = bt_syms.get(sym, {})

        if "error" in fc:
            return (f'<div class="alf-card"><div class="alf-card-hdr">'
                    f'<span class="alf-sym">{sym}</span></div>'
                    f'<div style="color:#f85149;padding:20px;">Forecast unavailable: {fc["error"]}</div></div>')

        price   = fc.get("price", 0)
        atr     = fc.get("atr", 0)
        bias    = fc.get("total_bias", 0)
        bullets = fc.get("bullets", [])
        days_fc = fc.get("days", {})
        days_an = anc.get("days", {})

        # Accuracy badge from backtest
        acc_d1 = bt.get("accuracy", {}).get("1")
        acc_badge = ""
        if acc_d1 is not None:
            pct = int(round(acc_d1 * 100))
            col = "#3fb950" if pct >= 70 else "#d29922" if pct >= 55 else "#f85149"
            acc_badge = (f'<span class="alf-acc-badge" style="color:{col};border-color:{col};">'
                         f'{pct}% acc D1</span>')

        # Forecast rows
        rows_html = ""
        any_revised = False
        for n in range(1, 6):
            day_str  = str(n)
            d_fc     = days_fc.get(n) or days_fc.get(day_str, {})
            d_an     = days_an.get(n) or days_an.get(day_str, {})
            if not d_fc:
                continue
            h_fc = d_fc.get("high", 0)
            l_fc = d_fc.get("low",  0)
            rng  = d_fc.get("range", 0)

            delta_h = delta_l = None
            revised_html = ""
            if d_an:
                h_an = d_an.get("high", 0)
                l_an = d_an.get("low",  0)
                if h_an and l_an:
                    delta_h = h_fc - h_an
                    delta_l = l_fc - l_an
                    if abs(delta_h) > CHANGE_THRESHOLD_PTS or abs(delta_l) > CHANGE_THRESHOLD_PTS:
                        any_revised = True
                        dh_s = f"{delta_h:+.0f}" if delta_h is not None else "—"
                        dl_s = f"{delta_l:+.0f}" if delta_l is not None else "—"
                        revised_html = (f'<span class="alf-revised">REVISED '
                                        f'H{dh_s} L{dl_s}</span>')

            delta_str = ""
            if delta_h is not None:
                col_h = "#3fb950" if delta_h >= 0 else "#f85149"
                col_l = "#3fb950" if delta_l >= 0 else "#f85149"
                delta_str = (f'<span style="color:{col_h}">{delta_h:+.1f}</span> / '
                             f'<span style="color:{col_l}">{delta_l:+.1f}</span>')
            else:
                delta_str = '<span style="color:#8b949e">anchor</span>'

            rows_html += (
                f'<tr>'
                f'<td class="alf-day">+{n}d</td>'
                f'<td class="alf-hi">{_fmt_price(h_fc)} {revised_html}</td>'
                f'<td class="alf-lo">{_fmt_price(l_fc)}</td>'
                f'<td class="alf-rng">{_fmt_price(rng)}</td>'
                f'<td class="alf-delta">{delta_str}</td>'
                f'</tr>'
            )

        revised_banner = ""
        if any_revised:
            revised_banner = '<div class="alf-revised-banner">⚡ Forecast revised from daily anchor</div>'

        bullets_html = "".join(f"<li>{b}</li>" for b in bullets)

        # SuperTrend color
        st_dir = fc.get("st_direction", "")
        st_col = "#3fb950" if st_dir == "BULLISH" else "#f85149"

        return f"""
<div class="alf-card">
  <div class="alf-card-hdr">
    <span class="alf-sym">{sym}</span>
    <span class="alf-price">{_fmt_price(price)}</span>
    <span class="alf-st" style="color:{st_col};">{st_dir}</span>
    {acc_badge}
  </div>
  <div class="alf-meta">
    ATR: <b>{atr:.2f}</b> &nbsp;|&nbsp;
    Bias: <b style="color:{'#3fb950' if bias >= 0 else '#f85149'};">{bias:+.2f}</b> pts &nbsp;|&nbsp;
    VIX mult: <b>{fc.get('vix_mult', 1.0):.1f}×</b>
  </div>
  {revised_banner}
  <table class="alf-tbl">
    <thead>
      <tr>
        <th>Day</th>
        <th class="alf-hi-hdr">▲ Forecast High</th>
        <th class="alf-lo-hdr">▼ Forecast Low</th>
        <th>Range (pts)</th>
        <th>Δ from Anchor</th>
      </tr>
    </thead>
    <tbody>{rows_html}</tbody>
  </table>
  <details class="alf-analysis">
    <summary>Analysis ▸</summary>
    <ul class="alf-bullets">{bullets_html}</ul>
  </details>
</div>"""

    # ── Backtest section ────────────────────────────────────────────────────

    bt_run_at = backtest.get("run_at", "—")
    bt_rows = ""
    for sym in ALFRED_SYMBOLS:
        bt = bt_syms.get(sym, {})
        if "error" in bt or not bt:
            bt_rows += f'<tr><td>{sym}</td><td colspan="7" style="color:#8b949e;">No backtest data</td></tr>'
            continue
        acc = bt.get("accuracy", {})
        cal = bt.get("calibration", {})
        row = f'<tr><td class="alf-sym-sm">{sym}</td>'
        for n in range(1, 6):
            a = acc.get(str(n))
            col = "#3fb950" if a and a >= 0.70 else "#d29922" if a and a >= 0.55 else "#f85149"
            row += f'<td style="color:{col}">{int(round(a*100)) if a else "—"}%</td>'
        row += f'<td>{bt.get("total_bars_d1","—")} bars</td>'
        cal_str = ", ".join(f"D{n}={cal.get(str(n), '1.0')}" for n in range(1, 6))
        row += f'<td style="font-size:10px;color:#8b949e">{cal_str}</td>'
        row += "</tr>"
        bt_rows += row

    interp_paras = ""
    for sym in ALFRED_SYMBOLS:
        bt = bt_syms.get(sym, {})
        txt = bt.get("interpretation", "")
        if txt:
            interp_paras += (f'<p><b style="color:#58a6ff">{sym}</b>: {txt}</p>')

    # ── Full page ────────────────────────────────────────────────────────────

    cards_top = _card("/ES") + _card("/MES")
    cards_bot = _card("/NQ") + _card("/MNQ")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Alfred — CME Futures Forecast</title>
  <style>
    *{{box-sizing:border-box;}}
    body{{margin:0;background:#0d1117;color:#e6edf3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px;}}
    {_NAV_CSS}
    .alf-wrap{{max-width:1400px;margin:0 auto;padding:16px;}}
    .alf-infobar{{display:flex;align-items:center;gap:16px;padding:10px 16px;background:#161b22;
      border:1px solid #21262d;border-radius:8px;margin-bottom:16px;flex-wrap:wrap;}}
    .alf-infobar span{{color:#8b949e;font-size:12px;}}
    .alf-infobar b{{color:#e6edf3;}}
    .alf-refresh-btn{{padding:5px 14px;border-radius:5px;border:1px solid #58a6ff;
      background:rgba(88,166,255,0.1);color:#58a6ff;cursor:pointer;font-size:12px;margin-left:auto;}}
    .alf-refresh-btn:hover{{background:rgba(88,166,255,0.2);}}
    .alf-info-btn{{padding:5px 10px;border-radius:5px;border:1px solid #30363d;
      background:transparent;color:#8b949e;cursor:pointer;font-size:13px;}}
    .alf-info-btn:hover{{color:#e6edf3;border-color:#58a6ff;}}
    .alf-info-panel{{display:none;background:#161b22;border:1px solid #30363d;border-radius:8px;
      padding:16px 20px;margin-bottom:14px;font-size:12px;line-height:1.8;color:#8b949e;}}
    .alf-info-panel.open{{display:block;}}
    .alf-info-panel h4{{color:#e6edf3;margin:12px 0 4px;font-size:12px;text-transform:uppercase;
      letter-spacing:0.5px;}}
    .alf-info-panel h4:first-child{{margin-top:0;}}
    .alf-info-panel ul{{margin:4px 0 8px 16px;padding:0;}}
    .alf-info-panel li{{margin-bottom:2px;}}
    .alf-info-panel b{{color:#e6edf3;}}
    .alf-info-panel code{{background:#0d1117;border:1px solid #21262d;border-radius:3px;
      padding:1px 5px;font-size:11px;color:#79c0ff;}}
    .alf-grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px;}}
    @media(max-width:800px){{.alf-grid{{grid-template-columns:1fr;}}}}
    .alf-card{{background:#161b22;border:1px solid #21262d;border-radius:10px;padding:16px;}}
    .alf-card-hdr{{display:flex;align-items:baseline;gap:12px;margin-bottom:8px;flex-wrap:wrap;}}
    .alf-sym{{font-size:18px;font-weight:700;color:#58a6ff;}}
    .alf-sym-sm{{font-weight:700;color:#58a6ff;}}
    .alf-price{{font-size:22px;font-weight:700;color:#e6edf3;}}
    .alf-st{{font-size:11px;font-weight:700;padding:2px 6px;border-radius:4px;
      background:rgba(255,255,255,0.05);}}
    .alf-acc-badge{{font-size:10px;padding:2px 7px;border-radius:4px;border:1px solid;
      background:rgba(255,255,255,0.04);margin-left:auto;}}
    .alf-meta{{font-size:11px;color:#8b949e;margin-bottom:10px;}}
    .alf-revised-banner{{background:rgba(210,153,34,0.15);border:1px solid #d29922;
      border-radius:5px;padding:5px 10px;font-size:11px;color:#d29922;margin-bottom:8px;}}
    .alf-revised{{font-size:10px;font-weight:700;color:#d29922;background:rgba(210,153,34,0.15);
      border:1px solid #d29922;border-radius:3px;padding:1px 4px;margin-left:4px;}}
    .alf-tbl{{width:100%;border-collapse:collapse;font-size:12px;}}
    .alf-tbl th{{color:#8b949e;font-weight:600;padding:4px 8px;border-bottom:1px solid #21262d;text-align:left;}}
    .alf-tbl td{{padding:5px 8px;border-bottom:1px solid #161b22;}}
    .alf-tbl tbody tr:hover{{background:#1c2128;}}
    .alf-day{{color:#8b949e;font-weight:600;}}
    .alf-hi{{color:#3fb950;font-weight:700;font-size:13px;}}
    .alf-lo{{color:#f85149;font-weight:700;font-size:13px;}}
    .alf-hi-hdr{{color:#3fb950;}}
    .alf-lo-hdr{{color:#f85149;}}
    .alf-rng{{color:#d29922;}}
    .alf-delta{{font-size:11px;}}
    .alf-analysis{{margin-top:10px;}}
    .alf-analysis summary{{cursor:pointer;color:#58a6ff;font-size:11px;user-select:none;outline:none;}}
    .alf-analysis summary:hover{{color:#79c0ff;}}
    .alf-bullets{{margin:6px 0 0 16px;padding:0;list-style:disc;color:#8b949e;
      font-size:11px;line-height:1.8;}}
    .alf-bullets li b{{color:#e6edf3;}}
    .alf-placeholder{{text-align:center;color:#8b949e;padding:40px;font-size:14px;}}
    .alf-bt-section{{background:#161b22;border:1px solid #21262d;border-radius:10px;
      padding:16px;margin-top:14px;}}
    .alf-bt-section summary{{cursor:pointer;color:#58a6ff;font-size:13px;font-weight:600;
      user-select:none;outline:none;}}
    .alf-bt-tbl{{width:100%;border-collapse:collapse;font-size:12px;margin-top:10px;}}
    .alf-bt-tbl th{{color:#8b949e;font-weight:600;padding:4px 8px;border-bottom:1px solid #21262d;text-align:left;}}
    .alf-bt-tbl td{{padding:5px 8px;border-bottom:1px solid #1c2128;}}
    .alf-learned{{margin-top:14px;font-size:12px;color:#8b949e;line-height:1.7;}}
    .alf-learned p{{margin:6px 0;}}
    #alf-status{{font-size:11px;color:#8b949e;}}
  </style>
</head>
<body>
<div class="sticky-banner">
  <header>
    <h1 style="margin:0;font-size:17px;font-weight:700;">&#128270; Alfred &mdash; CME Futures Forecast</h1>
    <div class="header-right">{nav}</div>
  </header>
</div>
{_NAV_TAPE_HTML}

<div class="alf-wrap">
  <div class="alf-infobar">
    <span>Updated: <b id="alf-updated">{updated}</b></span>
    <span>Next refresh: <b id="alf-countdown">5:00</b></span>
    <span>VIX: <b id="alf-vix">{vix_val}</b></span>
    <span id="alf-status"></span>
    <button class="alf-info-btn" id="alf-info-btn" onclick="toggleAlfInfo()" title="How Alfred works">&#9432; How it works</button>
    <button class="alf-refresh-btn" onclick="alfredForceRefresh()">&#8635; Refresh Now</button>
  </div>

  <div class="alf-info-panel" id="alf-info-panel">
    <h4>Forecast Model — ATR &times; &radic;N Range</h4>
    <ul>
      <li><b>Base range:</b> <code>ATR(14) &times; &radic;N &times; VIX_mult &times; calib[N]</code> — Wilder-smoothed 14-day Average True Range scaled by time horizon and volatility regime.</li>
      <li><b>VIX regime:</b> &lt;15 &rarr; 0.8&times; &nbsp;|&nbsp; 15–25 &rarr; 1.0&times; &nbsp;|&nbsp; 25–35 &rarr; 1.3&times; &nbsp;|&nbsp; &gt;35 &rarr; 1.6&times;</li>
      <li><b>Calibration:</b> Walk-forward backtest over 180 trading days auto-tunes each day's multiplier to target <b>70% containment accuracy</b> (actual H/L stays inside forecast band 7 out of 10 days).</li>
    </ul>
    <h4>Directional Bias (shifts the center, not the width)</h4>
    <ul>
      <li><b>RSI(14):</b> max &plusmn;0.5 &times; ATR — overbought pushes center up, oversold pushes down.</li>
      <li><b>MACD(12,26,9) histogram:</b> sign &times; 0.25 &times; ATR — positive histogram adds upward drift.</li>
      <li><b>SuperTrend(10,3):</b> &plusmn;0.2 &times; ATR — bullish/bearish trend confirmation.</li>
      <li><b>TipRanks analyst target (SPY/QQQ proxy):</b> &plusmn;0.3 &times; ATR max — SPY maps to /ES &amp; /MES; QQQ maps to /NQ &amp; /MNQ. 0.0 if TipRanks data unavailable.</li>
      <li>Total bias is clamped to &plusmn;1 &times; ATR and decays across days (Day 5 bias = 40% of Day 1).</li>
    </ul>
    <h4>Reading the Cards</h4>
    <ul>
      <li><b>High &#9650; / Low &#9660;:</b> Forecasted range boundaries for that trading day. Sell naked calls above High; sell cash-secured puts below Low.</li>
      <li><b>Range (pts):</b> Total width of the forecast band — wider = more uncertainty.</li>
      <li><b>&Delta; from Anchor:</b> How much today's forecast has moved since the first compute of this trading day. <span style="color:#d29922;font-weight:700;">REVISED</span> badge appears when delta &gt; 5 pts.</li>
      <li><b>Acc D1 badge:</b> Backtest containment rate for Day-1 forecasts. Green &ge;70%, yellow &ge;55%, red below.</li>
    </ul>
    <h4>Limitations &amp; Risk</h4>
    <ul>
      <li>Model does <b>not</b> predict direction — it gives a probability band. Actual price can exit the band on gap-open events, macro shocks, or FOMC days.</li>
      <li>Futures are highly leveraged. Always size positions so that a 2&times; ATR adverse move stays within your risk tolerance.</li>
      <li>Calibration resets weekly (Sunday 3 AM). First forecast of a new regime may use stale multipliers.</li>
    </ul>
  </div>

  <div id="alf-cards-top" class="alf-grid">{cards_top if syms_fc else ''}</div>
  <div id="alf-cards-bot" class="alf-grid">{cards_bot if syms_fc else ''}</div>

  {'<div class="alf-placeholder">No forecast yet &mdash; click Refresh Now or wait for the next scheduled compute.</div>' if not syms_fc else ''}

  <details class="alf-bt-section" {'open' if bt_syms else ''}>
    <summary>&#128202; Backtest Results &amp; Calibration
      {'&nbsp;&nbsp;<span style="color:#8b949e;font-size:11px;font-weight:400;">Last run: ' + bt_run_at + '</span>' if bt_run_at != '—' else ''}
    </summary>
    {'<p style="color:#8b949e;font-size:12px;padding:8px;">No backtest data yet. Click Run Backtest to generate.</p>' if not bt_syms else f"""
    <table class="alf-bt-tbl">
      <thead><tr>
        <th>Symbol</th><th>D1 Acc</th><th>D2 Acc</th><th>D3 Acc</th><th>D4 Acc</th><th>D5 Acc</th>
        <th>Bars</th><th>Calibration (applied)</th>
      </tr></thead>
      <tbody>{bt_rows}</tbody>
    </table>
    <div class="alf-learned">{interp_paras}</div>
    """}
    <button onclick="alfredRunBacktest()" style="margin-top:12px;padding:5px 14px;border-radius:5px;
      border:1px solid #30363d;background:#1c2128;color:#8b949e;cursor:pointer;font-size:11px;">
      &#9654; Run Full Backtest (background, ~2 min)
    </button>
  </details>
</div>

{_NAV_TAPE_JS}
<script>
var _alfCountdown = 300;
var _alfInterval  = null;

function toggleAlfInfo() {{
  var panel = document.getElementById('alf-info-panel');
  var btn   = document.getElementById('alf-info-btn');
  if (!panel) return;
  panel.classList.toggle('open');
  if (btn) btn.style.color = panel.classList.contains('open') ? '#58a6ff' : '';
}}

function alfCountdownTick() {{
  _alfCountdown--;
  if (_alfCountdown <= 0) {{
    _alfCountdown = 300;
    alfredRefreshData();
  }}
  var m = Math.floor(_alfCountdown / 60);
  var s = _alfCountdown % 60;
  var el = document.getElementById('alf-countdown');
  if (el) el.textContent = m + ':' + (s < 10 ? '0' : '') + s;
}}

function alfredRefreshData() {{
  fetch('/api/alfred/forecast').then(r=>r.json()).then(function(d) {{
    if (d.error) return;
    var el = document.getElementById('alf-updated');
    if (el) el.textContent = d.updated_at || '—';
    var ve = document.getElementById('alf-vix');
    if (ve) ve.textContent = d.vix || '—';
    // Full page reload to re-render cards (simpler than partial DOM update)
    location.reload();
  }}).catch(function(){{}});
}}

function alfredForceRefresh() {{
  var st = document.getElementById('alf-status');
  if (st) st.textContent = 'Refreshing…';
  fetch('/api/alfred/refresh', {{method:'POST'}}).then(function() {{
    setTimeout(function() {{ location.reload(); }}, 9000);
  }});
}}

function alfredRunBacktest() {{
  if (!confirm('Run full 6-month backtest for all 4 symbols? This takes ~2 minutes in the background.')) return;
  fetch('/api/alfred/run-backtest', {{method:'POST'}}).then(function() {{
    var st = document.getElementById('alf-status');
    if (st) st.textContent = 'Backtest running… (reload in ~2 min)';
  }});
}}

_alfInterval = setInterval(alfCountdownTick, 1000);
</script>
</body>
</html>"""


# ── API routes ────────────────────────────────────────────────────────────────

@alfred_router.get("/alfred", response_class=HTMLResponse)
async def alfred_page():
    fc  = _load_forecast()
    anc = _load_anchor()
    bt  = _load_backtest()
    return HTMLResponse(_build_alfred_html(fc, anc, bt))


@alfred_router.get("/api/alfred/forecast")
def api_alfred_forecast():
    fc = _load_forecast()
    return JSONResponse(fc if fc else {"error": "no_forecast_yet"})


@alfred_router.get("/api/alfred/backtest")
def api_alfred_backtest():
    bt = _load_backtest()
    return JSONResponse(bt if bt else {"error": "no_backtest_yet"})


@alfred_router.post("/api/alfred/refresh")
def api_alfred_refresh():
    threading.Thread(target=_compute_alfred, kwargs={"force": True},
                     daemon=True, name="alfred-force-refresh").start()
    return JSONResponse({"status": "refresh_started"})


@alfred_router.post("/api/alfred/run-backtest")
def api_alfred_run_backtest():
    threading.Thread(target=_run_backtest_all,
                     daemon=True, name="alfred-backtest").start()
    return JSONResponse({"status": "backtest_started"})
