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
from datetime import date, datetime, timedelta
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

_FORECAST_FILE     = ROOT / "data" / "alfred_forecast.json"
_ANCHOR_FILE       = ROOT / "data" / "alfred_anchor.json"
_BACKTEST_FILE     = ROOT / "data" / "alfred_backtest.json"
_PRO_FORECAST_FILE = ROOT / "data" / "alfred_pro_forecast.json"
_PRO_AI_NOTES_FILE = ROOT / "data" / "alfred_pro_ai_notes.json"

_lock             = threading.Lock()
_last_compute     = 0.0
_COMPUTE_TTL      = 295  # seconds — just under 5 min
_pro_lock         = threading.Lock()
_pro_last_compute = 0.0

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

# ── Options model constants ───────────────────────────────────────────────────

CONTRACT_MULTIPLIERS = {"/ES": 50,  "/MES": 5,  "/NQ": 20,  "/MNQ": 2}
STRIKE_INTERVALS     = {"/ES": 5,   "/MES": 5,  "/NQ": 25,  "/MNQ": 25}

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


# ── Black-76 options model (pure stdlib — no scipy) ───────────────────────────

def _norm_cdf(x: float) -> float:
    """Standard normal CDF via math.erf."""
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def _norm_ppf_approx(p: float) -> float:
    """Rational approximation of the standard normal quantile (Beasley-Springer-Moro)."""
    if p <= 0.0:
        return -8.0
    if p >= 1.0:
        return 8.0
    flip = p < 0.5
    q = p if flip else 1.0 - p
    t = math.sqrt(-2.0 * math.log(q))
    c = (2.515517, 0.802853, 0.010328)
    d = (1.432788, 0.189269, 0.001308)
    approx = t - (c[0] + c[1]*t + c[2]*t*t) / (1.0 + d[0]*t + d[1]*t*t + d[2]*t*t*t)
    return -approx if flip else approx


def _black76_price(F: float, K: float, sigma: float, T: float, opt_type: str) -> float:
    """Black-76 theoretical price for a European futures option (r=0)."""
    if T <= 0 or sigma <= 0 or F <= 0 or K <= 0:
        return 0.0
    sqrtT = math.sqrt(T)
    try:
        d1 = (math.log(F / K) + 0.5 * sigma**2 * T) / (sigma * sqrtT)
    except (ValueError, ZeroDivisionError):
        return 0.0
    d2 = d1 - sigma * sqrtT
    if opt_type == "call":
        return max(0.0, F * _norm_cdf(d1) - K * _norm_cdf(d2))
    return max(0.0, K * _norm_cdf(-d2) - F * _norm_cdf(-d1))


def _prob_worthless(F: float, K: float, sigma: float, T: float, opt_type: str) -> float:
    """Risk-neutral probability that the option expires OTM (worthless to its buyer)."""
    if T <= 0 or sigma <= 0 or F <= 0 or K <= 0:
        return 0.0
    sqrtT = math.sqrt(T)
    try:
        d2 = (math.log(F / K) - 0.5 * sigma**2 * T) / (sigma * sqrtT)
    except (ValueError, ZeroDivisionError):
        return 0.0
    return _norm_cdf(-d2) if opt_type == "call" else _norm_cdf(d2)


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


def _fetch_nq_vol(vix_fallback: float) -> float:
    """
    Annualized implied vol for /NQ and /MNQ via ^VXN (Nasdaq-100 volatility index).
    Falls back to VIX × 1.25 if unavailable.
    """
    try:
        df = yf.download("^VXN", period="5d", interval="1d",
                         progress=False, auto_adjust=True)
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        closes = df["Close"].dropna().tolist()
        if closes:
            val = float(closes[-1]) / 100.0
            logger.debug(f"[Alfred] VXN = {val*100:.1f}%")
            return val
    except Exception as exc:
        logger.debug(f"[Alfred] VXN fetch failed: {exc}")
    return vix_fallback * 1.25


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


# ── Options strike recommendation ────────────────────────────────────────────

def _compute_options_strikes(sym: str, price: float, sigma_annual: float,
                              sigma_source: str = "VIX") -> dict:
    """
    Black-76 strike recommendations for selling OTM calls and puts on CME futures.
    Finds strikes with ≥75% probability of expiring worthless for each of 3 days.
    Returns dollar premium and meets_150 flag using CONTRACT_MULTIPLIERS.
    """
    mult     = CONTRACT_MULTIPLIERS.get(sym, 50)
    interval = STRIKE_INTERVALS.get(sym, 5)
    z75      = _norm_ppf_approx(0.75)   # ≈ 0.6745

    # Compute next 3 trading-day expiry labels (skip weekends)
    today = datetime.now(_ET).date()
    expiry_dates: dict = {}
    d = today
    count = 0
    while count < 3:
        d += timedelta(days=1)
        if d.weekday() < 5:
            count += 1
            expiry_dates[count] = d

    result: dict = {
        "sigma_annual": round(sigma_annual, 4),
        "sigma_source": sigma_source,
    }

    for n in range(1, 4):
        T     = n / 252.0
        sqrtT = math.sqrt(T)
        shift = z75 * sigma_annual * sqrtT

        # Raw 75%-probability strikes
        k_call_raw = price * math.exp(+shift)
        k_put_raw  = price * math.exp(-shift)

        # Round to nearest valid CME strike interval
        k_call = round(round(k_call_raw / interval) * interval, 2)
        k_put  = round(round(k_put_raw  / interval) * interval, 2)

        # Actual probability and premium at rounded strikes
        prob_call     = _prob_worthless(price, k_call, sigma_annual, T, "call")
        prob_put      = _prob_worthless(price, k_put,  sigma_annual, T, "put")
        prem_call_pts = _black76_price(price, k_call, sigma_annual, T, "call")
        prem_put_pts  = _black76_price(price, k_put,  sigma_annual, T, "put")
        prem_call_usd = int(round(prem_call_pts * mult))
        prem_put_usd  = int(round(prem_put_pts  * mult))

        # Cross-platform date label (Windows doesn't support %-m/%-d)
        exp_d = expiry_dates.get(n)
        exp_label = (exp_d.strftime("%a") + f" {exp_d.month}/{exp_d.day}") if exp_d else f"+{n}d"

        result[str(n)] = {
            "expiry_label": exp_label,
            "call": {
                "strike":      k_call,
                "premium_pts": round(prem_call_pts, 2),
                "premium_$":   prem_call_usd,
                "prob":        round(prob_call, 4),
                "meets_150":   prem_call_usd >= 150,
            },
            "put": {
                "strike":      k_put,
                "premium_pts": round(prem_put_pts, 2),
                "premium_$":   prem_put_usd,
                "prob":        round(prob_put, 4),
                "meets_150":   prem_put_usd >= 150,
            },
        }

    return result


# ── Alfred Pro — signal helpers ───────────────────────────────────────────────

def _wilder_atr_series(highs: list, lows: list, closes: list, period: int = 14) -> list:
    """Return Wilder-smoothed ATR at each bar from index `period` onward."""
    n = len(closes)
    if n < period + 1:
        return []
    tr = [highs[0] - lows[0]]
    for i in range(1, n):
        tr.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]),
        ))
    atr0 = sum(tr[:period]) / period
    series = [atr0]
    for i in range(period, n):
        series.append((series[-1] * (period - 1) + tr[i]) / period)
    return series


def _fetch_vix_term_structure() -> tuple:
    """Fetch VIX9D and VIX3M; return (vix9d, vix3m). Returns (0, 0) on failure."""
    results = [0.0, 0.0]
    for idx, sym in enumerate(("^VIX9D", "^VIX3M")):
        try:
            df = yf.download(sym, period="5d", interval="1d",
                             progress=False, auto_adjust=True)
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
            closes = df["Close"].dropna().tolist()
            if closes:
                results[idx] = float(closes[-1])
        except Exception as exc:
            logger.debug(f"[AlfredPro] {sym} fetch failed: {exc}")
    return tuple(results)


_iv_rank_cache:  dict = {}   # (ticker, date_str) -> result dict
_vix10yr_cache:  dict = {}   # date_str -> float


def _fetch_iv_rank(iv_ticker: str) -> dict:
    """
    IV Rank (0–100) over the 52-week range of a vol index.
    IVR = (current − 52w_low) / (52w_high − 52w_low) × 100
    Returns {"current", "ivr", "high", "low"}.
    Low IVR (<30) = cheap premium (buy); high IVR (>70) = expensive (sell).
    """
    today_str = datetime.now(_ET).strftime("%Y-%m-%d")
    cache_key = (iv_ticker, today_str)
    if cache_key in _iv_rank_cache:
        return _iv_rank_cache[cache_key]
    fallback = {"current": 0.0, "ivr": 50.0, "high": 0.0, "low": 0.0}
    try:
        df = yf.download(iv_ticker, period="1y", interval="1d",
                         progress=False, auto_adjust=True)
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        closes = df["Close"].dropna().tolist()
        if len(closes) < 10:
            return fallback
        current  = float(closes[-1])
        high_52w = float(max(closes))
        low_52w  = float(min(closes))
        ivr = 0.0
        if high_52w > low_52w:
            ivr = round((current - low_52w) / (high_52w - low_52w) * 100, 1)
        result = {"current": round(current, 2), "ivr": ivr,
                  "high": round(high_52w, 2), "low": round(low_52w, 2)}
        _iv_rank_cache[cache_key] = result
        return result
    except Exception as exc:
        logger.debug(f"[AlfredPro] IV rank fetch failed for {iv_ticker}: {exc}")
        return fallback


def _fetch_vix_10yr_pct() -> float:
    """VIX current level as a percentile within its 10-year daily close history (0–100)."""
    today_str = datetime.now(_ET).strftime("%Y-%m-%d")
    if today_str in _vix10yr_cache:
        return _vix10yr_cache[today_str]
    try:
        df = yf.download("^VIX", period="10y", interval="1d",
                         progress=False, auto_adjust=True)
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        closes = df["Close"].dropna().tolist()
        if len(closes) < 50:
            return 50.0
        current = float(closes[-1])
        pct = round(sum(1 for v in closes if v <= current) / len(closes) * 100, 1)
        _vix10yr_cache[today_str] = pct
        return pct
    except Exception as exc:
        logger.debug(f"[AlfredPro] VIX 10yr pct fetch failed: {exc}")
        return 50.0


def _compute_rv_iv(closes: list, vix: float) -> float:
    """20-day annualized realized vol divided by VIX. < 1.0 = IV rich = good to sell."""
    if len(closes) < 22 or vix <= 0:
        return 1.0
    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(len(closes) - 20, len(closes))]
    mean = sum(rets) / len(rets)
    var  = sum((r - mean) ** 2 for r in rets) / len(rets)
    rv20 = math.sqrt(var * 252)
    return round(rv20 / (vix / 100.0), 3)


def _atr_percentile_from_ohlcv(highs: list, lows: list, closes: list,
                                current_atr: float, lookback: int = 30) -> float:
    """Percentile rank of current_atr within the last `lookback` ATR values (0–100)."""
    series = _wilder_atr_series(highs, lows, closes, 14)
    if not series:
        return 50.0
    recent = series[-lookback:] if len(series) >= lookback else series
    below  = sum(1 for v in recent if v < current_atr)
    return round(100.0 * below / len(recent), 1)


def _compute_signal_alignment(rsi: float, macd_hist: float,
                               st_direction: str, total_bias: float) -> float:
    """
    Consensus of 4 directional signals, normalized to [-1.0, +1.0].
    +1 = all bullish (favors put side), -1 = all bearish (favors call side).
    """
    bull_count = sum([
        rsi > 55,
        macd_hist > 0,
        st_direction == "BULLISH",
        total_bias > 0,
    ])
    return round((bull_count - 2) / 2.0, 3)


def _compute_pro_strikes(sym: str, price: float, atr: float,
                          days: dict, signals: dict,
                          sigma_annual: float) -> dict:
    """
    Alfred-range-based strike recommendations.
    Strike = Alfred forecast extreme ± ATR buffer (not a lognormal quantile).
    Confidence is a multi-signal score, not N(-d2).
    """
    mult     = CONTRACT_MULTIPLIERS.get(sym, 50)
    interval = STRIKE_INTERVALS.get(sym, 5)

    rv_iv    = signals.get("rv_iv", 1.0)
    atr_pct  = signals.get("atr_pct", 50.0)
    align    = signals.get("alignment", 0.0)
    contango = signals.get("contango", True)

    # Base confidence from signal environment
    base_conf = 0.60
    if rv_iv < 0.85:       base_conf += 0.08
    if contango:           base_conf += 0.06
    if atr_pct < 35:       base_conf += 0.05
    if abs(align) > 0.50:  base_conf += 0.06
    base_conf = min(base_conf, 0.90)

    # Buffer beyond Alfred's forecast edge; grows when vol is elevated
    buf_factor = 0.25
    if rv_iv > 1.10:  buf_factor += 0.15
    if atr_pct > 65:  buf_factor += 0.10
    buffer_pts = round(atr * buf_factor, 1)

    today = datetime.now(_ET).date()
    expiry_dates: dict = {}
    d = today
    count = 0
    while count < 3:
        d += timedelta(days=1)
        if d.weekday() < 5:
            count += 1
            expiry_dates[count] = d

    result = {}
    for n in range(1, 4):
        day_str = str(n)
        d_fc    = days.get(n) or days.get(day_str, {})
        if not d_fc:
            continue

        alfred_high = d_fc["high"]
        alfred_low  = d_fc["low"]
        T = n / 252.0

        raw_call = alfred_high + buffer_pts
        raw_put  = alfred_low  - buffer_pts
        k_call   = round(round(raw_call / interval) * interval, 2)
        k_put    = round(round(raw_put  / interval) * interval, 2)

        prem_call_pts = _black76_price(price, k_call, sigma_annual, T, "call")
        prem_put_pts  = _black76_price(price, k_put,  sigma_annual, T, "put")
        prem_call_usd = int(round(prem_call_pts * mult))
        prem_put_usd  = int(round(prem_put_pts  * mult))

        call_conf = round(min(base_conf + align * 0.05, 0.92), 3)
        put_conf  = round(min(base_conf - align * 0.05, 0.92), 3)

        em_1sd = round(sigma_annual * price * math.sqrt(T), 1)
        em_2sd = round(em_1sd * 2, 1)
        em_3sd = round(em_1sd * 3, 1)

        exp_d     = expiry_dates.get(n)
        exp_label = (exp_d.strftime("%a") + f" {exp_d.month}/{exp_d.day}") if exp_d else f"+{n}d"

        result[day_str] = {
            "expiry_label": exp_label,
            "alfred_high":  alfred_high,
            "alfred_low":   alfred_low,
            "buffer_pts":   buffer_pts,
            "call": {
                "strike":      k_call,
                "premium_pts": round(prem_call_pts, 2),
                "premium_$":   prem_call_usd,
                "conf":        call_conf,
            },
            "put": {
                "strike":      k_put,
                "premium_pts": round(prem_put_pts, 2),
                "premium_$":   prem_put_usd,
                "conf":        put_conf,
            },
            "sd1": em_1sd, "sd1_high": round(price + em_1sd, 1), "sd1_low": round(price - em_1sd, 1),
            "sd2": em_2sd, "sd2_high": round(price + em_2sd, 1), "sd2_low": round(price - em_2sd, 1),
            "sd3": em_3sd, "sd3_high": round(price + em_3sd, 1), "sd3_low": round(price - em_3sd, 1),
        }
    return result


def _call_claude_edge_note(sym: str, price: float, atr: float,
                            vix: float, signals: dict, strikes: dict,
                            env_rating: str) -> str:
    """3-sentence AI edge assessment via Claude Haiku; cached 4 h per symbol per day."""
    today_str = datetime.now(_ET).strftime("%Y-%m-%d")
    cache_key = f"{sym}_{today_str}"
    try:
        cache = json.loads(_PRO_AI_NOTES_FILE.read_text()) if _PRO_AI_NOTES_FILE.exists() else {}
        entry = cache.get(cache_key, {})
        if (entry.get("note") and entry.get("_prompt_v", 1) >= 2
                and (time.time() - entry.get("ts", 0)) < 4 * 3600):
            return entry["note"]
    except Exception:
        cache = {}

    try:
        from config import config as _cfg
        api_key = _cfg.brief.anthropic_api_key
    except Exception:
        api_key = ""
    if not api_key:
        return ""

    rv_iv        = signals.get("rv_iv", 1.0)
    atr_pct      = signals.get("atr_pct", 50.0)
    align        = signals.get("alignment", 0.0)
    ivr          = signals.get("ivr", 50.0)
    ivr_high     = signals.get("ivr_high", 0.0)
    ivr_low      = signals.get("ivr_low", 0.0)
    vix_10yr_pct = signals.get("vix_10yr_pct", 50.0)

    d1 = strikes.get("1", {})
    ivr_label = (
        "LOW (near year low — cheap premium)" if ivr < 30 else
        "HIGH (near year high — expensive premium)" if ivr > 70 else
        f"MID-RANGE ({ivr:.0f}/100)"
    )
    hist_label = "low" if vix_10yr_pct < 35 else "high" if vix_10yr_pct > 65 else "average"

    prompt = (
        f"You are a futures options trading advisor. Analyze {sym} options.\n\n"
        f"Data:\n"
        f"- Price: {price:.2f}  ATR(14): {atr:.1f} pts\n"
        f"- Current VIX: {vix:.1f}\n"
        f"- IV Rank (52-week, 0-100): {ivr:.1f} — {ivr_label}\n"
        f"  52w IV range: {ivr_low:.1f} – {ivr_high:.1f}\n"
        f"- VIX 10-year percentile: {vix_10yr_pct:.0f}th (historically {hist_label} vol)\n"
        f"- RV/IV (20-day realized / current IV): {rv_iv:.2f}\n"
        f"- ATR percentile (30-day lookback): {atr_pct:.0f}th\n"
        f"- Signal alignment: {align:+.2f}\n"
        f"- Overall environment: {env_rating}\n"
        f"- Day-1 Alfred range: {d1.get('alfred_high',0):.0f} high / "
        f"{d1.get('alfred_low',0):.0f} low\n"
        f"- Day-1 strikes: Call {d1.get('call',{}).get('strike',0):.0f} / "
        f"Put {d1.get('put',{}).get('strike',0):.0f}\n"
        f"- Day-1 expected moves: 1SD ±{d1.get('sd1',0):.1f} | "
        f"2SD ±{d1.get('sd2',0):.1f} | 3SD ±{d1.get('sd3',0):.1f}\n\n"
        f"Reply in exactly 3 sentences:\n"
        f"1. Based on the IV Rank and VIX 10-year percentile, should the trader BUY or SELL "
        f"premium right now, and how strong is the conviction?\n"
        f"2. Which side (call or put) has better edge given the directional alignment, "
        f"and which SD strike level offers the best risk/reward?\n"
        f"3. What is the single most important risk to these specific trades today?\n"
        f"Be specific with numbers. No disclaimers. No bullet points."
    )
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg    = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=280,
            messages=[{"role": "user", "content": prompt}],
        )
        note = msg.content[0].text.strip()
        cache[cache_key] = {"note": note, "ts": time.time(), "_prompt_v": 2}
        _PRO_AI_NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PRO_AI_NOTES_FILE.write_text(json.dumps(cache, indent=2))
        logger.info(f"[AlfredPro] Claude edge note cached for {sym}")
        return note
    except Exception as exc:
        logger.warning(f"[AlfredPro] Claude edge note failed for {sym}: {exc}")
        return ""


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

        # Vol for options model — VIX for /ES,/MES; ^VXN for /NQ,/MNQ
        vix_dec  = vix / 100.0
        nq_sigma = _fetch_nq_vol(vix_dec)

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
            if "error" not in fc:
                sigma    = nq_sigma if sym in ("/NQ", "/MNQ") else vix_dec
                sig_src  = "VXN" if sym in ("/NQ", "/MNQ") else "VIX"
                fc["options"] = _compute_options_strikes(sym, fc["price"], sigma, sig_src)
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


def _load_pro_forecast() -> dict:
    try:
        if _PRO_FORECAST_FILE.exists():
            return json.loads(_PRO_FORECAST_FILE.read_text())
    except Exception:
        pass
    return {}


def _compute_alfred_pro(force: bool = False) -> bool:
    """
    Alfred Pro compute: augments the standard Alfred forecast with RV/IV,
    VIX term structure, ATR percentile, signal alignment, Alfred-range-based
    strikes, and a Claude AI edge note.
    """
    global _pro_last_compute

    if not force and (time.time() - _pro_last_compute < _COMPUTE_TTL):
        return False

    with _pro_lock:
        if not force and (time.time() - _pro_last_compute < _COMPUTE_TTL):
            return False

        logger.info("[AlfredPro] Computing Alfred Pro forecast...")

        # Ensure base forecast is fresh
        fc = _load_forecast()
        if not fc or "symbols" not in fc:
            _compute_alfred(force=True)
            fc = _load_forecast()

        vix      = fc.get("vix", 20.0)
        vix9d, vix3m = _fetch_vix_term_structure()
        contango = (vix < vix3m) if vix3m > 0 else True

        vix_dec  = vix / 100.0
        nq_sigma = _fetch_nq_vol(vix_dec)

        vix_10yr_pct = _fetch_vix_10yr_pct()
        _ivr_map = {
            "^VIX": _fetch_iv_rank("^VIX"),
            "^VXN": _fetch_iv_rank("^VXN"),
        }

        pro_syms: dict = {}
        for sym, yf_sym in ALFRED_SYMBOLS.items():
            sym_fc = fc.get("symbols", {}).get(sym, {})
            if "error" in sym_fc or not sym_fc:
                pro_syms[sym] = {"error": sym_fc.get("error", "no_data")}
                continue

            fallback = FALLBACK_SYMBOLS.get(sym)
            h, l, c  = _fetch_ohlcv(yf_sym, fallback)

            price      = sym_fc["price"]
            atr        = sym_fc["atr"]
            rsi        = sym_fc["rsi"]
            macd_h     = sym_fc["macd_hist"]
            st_dir     = sym_fc["st_direction"]
            total_bias = sym_fc["total_bias"]
            days       = sym_fc["days"]

            rv_iv   = _compute_rv_iv(c, vix) if c else 1.0
            atr_pct = _atr_percentile_from_ohlcv(h, l, c, atr) if c else 50.0
            align   = _compute_signal_alignment(rsi, macd_h, st_dir, total_bias)
            sigma   = nq_sigma if sym in ("/NQ", "/MNQ") else vix_dec

            iv_ticker = "^VXN" if sym in ("/NQ", "/MNQ") else "^VIX"
            iv_data   = _ivr_map[iv_ticker]
            ivr       = iv_data["ivr"]

            signals = {
                "rv_iv":         rv_iv,
                "contango":      contango,
                "atr_pct":       atr_pct,
                "alignment":     align,
                "vix9d":         round(vix9d, 2),
                "vix3m":         round(vix3m, 2),
                "ivr":           ivr,
                "ivr_high":      iv_data["high"],
                "ivr_low":       iv_data["low"],
                "vix_10yr_pct":  vix_10yr_pct,
            }

            strikes = _compute_pro_strikes(sym, price, atr, days, signals, sigma)

            if ivr < 30:
                env_rating = "BUY PREMIUM"
            elif ivr > 70:
                env_rating = "SELL PREMIUM"
            else:
                secondary_score = sum([
                    rv_iv < 0.90,
                    contango,
                    atr_pct < 40,
                    abs(align) >= 0.25,
                ])
                env_rating = "SELL PREMIUM" if secondary_score >= 3 else "NEUTRAL"

            ai_note = _call_claude_edge_note(sym, price, atr, vix, signals, strikes, env_rating)

            alfred_days_3 = {}
            for n in range(1, 4):
                d_fc = days.get(n) or days.get(str(n), {})
                if d_fc:
                    alfred_days_3[str(n)] = {
                        "high":  d_fc["high"],
                        "low":   d_fc["low"],
                        "range": d_fc["range"],
                    }

            pro_syms[sym] = {
                "price":       price,
                "atr":         atr,
                "rsi":         rsi,
                "st_direction": st_dir,
                "total_bias":  total_bias,
                "alfred_days": alfred_days_3,
                "signals":     signals,
                "strikes":     strikes,
                "env_rating":  env_rating,
                "ai_note":     ai_note,
            }

        now_et = datetime.now(_ET)
        payload = {
            "symbols":    pro_syms,
            "vix":        round(vix, 2),
            "vix9d":      round(vix9d, 2),
            "vix3m":      round(vix3m, 2),
            "updated_at": now_et.strftime("%Y-%m-%d %H:%M:%S ET"),
            "ts_epoch":   time.time(),
        }
        _PRO_FORECAST_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PRO_FORECAST_FILE.write_text(json.dumps(payload, indent=2))
        _pro_last_compute = time.time()
        logger.info("[AlfredPro] Pro forecast saved.")
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

        # ── Options strikes section ──────────────────────────────────────────
        opts = fc.get("options", {})
        opts_html = ""
        if opts:
            mult_val = CONTRACT_MULTIPLIERS.get(sym, 50)
            sigma_pct = round(opts.get("sigma_annual", 0) * 100, 1)
            sig_src   = opts.get("sigma_source", "VIX")
            below_150_syms = []

            def _prem_cell(info: dict, side: str) -> str:
                usd  = info.get("premium_$", 0)
                pts  = info.get("premium_pts", 0)
                if usd >= 150:
                    col   = "#3fb950"
                    badge = " &#10003;"
                elif usd >= 75:
                    col   = "#d29922"
                    badge = ""
                else:
                    col   = "#f85149"
                    n_ct  = max(2, math.ceil(150 / usd)) if usd > 0 else "?"
                    badge = f" <span style='font-size:9px;'>({n_ct}&times;)</span>"
                txt_col = "#58a6ff" if side == "call" else "#f85149"
                return (f'<td style="color:{col};font-weight:700;">'
                        f'${usd:,}{badge}'
                        f'<br><span style="color:#8b949e;font-weight:400;font-size:10px;">{pts:.1f}pts</span>'
                        f'</td>')

            opts_rows = ""
            any_below = False
            for n in range(1, 4):
                nd = opts.get(str(n), {})
                if not nd:
                    continue
                ci    = nd.get("call", {})
                pi    = nd.get("put",  {})
                exp_l = nd.get("expiry_label", f"+{n}d")
                c_str = _fmt_price(ci.get("strike"))
                p_str = _fmt_price(pi.get("strike"))
                c_prob = ci.get("prob", 0) * 100
                p_prob = pi.get("prob", 0) * 100
                if not ci.get("meets_150") or not pi.get("meets_150"):
                    any_below = True
                opts_rows += (
                    f'<tr>'
                    f'<td class="alf-day">{exp_l}</td>'
                    f'<td class="alf-hi" style="font-size:13px;">{c_str}</td>'
                    f'{_prem_cell(ci, "call")}'
                    f'<td style="color:#8b949e;font-size:11px;">{c_prob:.1f}%</td>'
                    f'<td class="alf-opts-divider alf-lo" style="font-size:13px;">{p_str}</td>'
                    f'{_prem_cell(pi, "put")}'
                    f'<td style="color:#8b949e;font-size:11px;">{p_prob:.1f}%</td>'
                    f'</tr>'
                )

            micro_note = ""
            if any_below:
                # compute minimum contracts needed for $150 from Day 1 call premium
                d1_prem = opts.get("1", {}).get("call", {}).get("premium_$", 0)
                n_needed = max(2, math.ceil(150 / d1_prem)) if d1_prem > 0 else "?"
                micro_note = (f'<div class="alf-opts-note">&#9432; Micro contract: '
                              f'~{n_needed} contracts needed per side to collect $150. '
                              f'Consider the full-size equivalent for single-contract trades.</div>')

            opts_html = f"""
  <div class="alf-opts-wrap">
    <div class="alf-opts-hdr">
      <span>OPTIONS STRIKES &mdash; SELL FOR PREMIUM</span>
      <span class="alf-opts-sigma">&sigma;&nbsp;=&nbsp;{sigma_pct}% ({sig_src}) &nbsp;&bull;&nbsp; ${mult_val}/pt &nbsp;&bull;&nbsp; 75%+ P(worthless)</span>
    </div>
    <table class="alf-opts-tbl">
      <thead>
        <tr>
          <th>Expiry</th>
          <th style="color:#3fb950;">CALL Strike &#9650;</th>
          <th style="color:#3fb950;">Premium</th>
          <th>P(OTM)</th>
          <th style="color:#f85149;padding-left:8px;">PUT Strike &#9660;</th>
          <th style="color:#f85149;">Premium</th>
          <th>P(OTM)</th>
        </tr>
      </thead>
      <tbody>{opts_rows}</tbody>
    </table>
    {micro_note}
  </div>"""

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
  {opts_html}
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
    .alf-opts-wrap{{margin-top:12px;border-top:1px solid #21262d;padding-top:10px;}}
    .alf-opts-hdr{{display:flex;justify-content:space-between;align-items:center;
      margin-bottom:6px;flex-wrap:wrap;gap:6px;}}
    .alf-opts-hdr span:first-child{{font-size:11px;font-weight:700;color:#e6edf3;
      text-transform:uppercase;letter-spacing:0.4px;}}
    .alf-opts-sigma{{font-size:10px;color:#8b949e;}}
    .alf-opts-tbl{{width:100%;border-collapse:collapse;font-size:12px;}}
    .alf-opts-tbl th{{color:#8b949e;font-weight:600;padding:3px 6px;
      border-bottom:1px solid #21262d;text-align:left;font-size:10px;}}
    .alf-opts-tbl td{{padding:5px 6px;border-bottom:1px solid #161b22;vertical-align:middle;}}
    .alf-opts-tbl tbody tr:hover{{background:#1c2128;}}
    .alf-opts-divider{{border-left:2px solid #21262d;padding-left:8px;}}
    .alf-opts-note{{font-size:10px;color:#8b949e;margin-top:5px;padding:3px 0;}}
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
    <div class="header-right">
      <a href="/alfred-pro" style="color:#d29922;font-size:11px;font-weight:700;text-decoration:none;
        border:1px solid #d29922;border-radius:4px;padding:3px 8px;margin-right:8px;
        background:rgba(210,153,34,0.1);">&#9733; Alfred Pro</a>
      {nav}
    </div>
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
    <h4>Options Strikes — Black-76 Model</h4>
    <ul>
      <li><b>Model:</b> Black-76 (Black-Scholes adapted for futures; risk-free rate = 0). Gives theoretical fair value and risk-neutral probability for each strike.</li>
      <li><b>Volatility source:</b> <code>&sigma;</code> = VIX&nbsp;&divide;&nbsp;100 for /ES &amp; /MES; <code>&sigma;</code> = VXN&nbsp;&divide;&nbsp;100 (^VXN, fallback VIX&times;1.25) for /NQ &amp; /MNQ.</li>
      <li><b>Strike selection:</b> Finds the strike where <code>P(expire worthless) &ge; 75%</code>, then rounds to the nearest CME interval (5 pts for /ES,/MES; 25 pts for /NQ,/MNQ). Actual probability is recalculated at the rounded strike.</li>
      <li><b>Premium:</b> Black-76 theoretical value &times; contract multiplier ($50/pt /ES, $20/pt /NQ, $5/pt /MES, $2/pt /MNQ). <span style="color:#3fb950;">Green &#10003;</span> = meets $150/contract target. <span style="color:#f85149;">Red</span> = below target (number in parentheses = contracts needed).</li>
      <li><b>Micro contracts:</b> /MES and /MNQ rarely collect $150 from a single contract at 75%+ probability. The note below the table shows how many contracts are needed.</li>
      <li><b>Note:</b> These are theoretical model premiums based on implied vol. Actual bid/ask spreads and liquidity at these strikes may differ. Always verify live quotes on CME/ThinkorSwim before entering.</li>
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


# ── Alfred Pro HTML builder ───────────────────────────────────────────────────

def _build_alfred_pro_html(data: dict) -> str:
    from monitor.dashboard import _nav_html, _NAV_CSS, _NAV_TAPE_HTML, _NAV_TAPE_JS

    nav     = _nav_html("alfred")
    syms    = data.get("symbols", {})
    vix_val = data.get("vix", "—")
    vix9d   = data.get("vix9d", 0)
    vix3m   = data.get("vix3m", 0)
    updated = data.get("updated_at", "—")

    contango_global = (float(vix_val) < vix3m) if (vix3m > 0 and vix_val != "—") else True
    term_label  = "Contango &#9660;" if contango_global else "Backwardation &#9650;"
    term_color  = "#3fb950" if contango_global else "#f85149"
    regime_vix  = float(vix_val) if vix_val != "—" else 20.0
    regime_label = "CALM" if regime_vix < 15 else "NORMAL" if regime_vix < 25 else "ELEVATED" if regime_vix < 35 else "EXTREME"
    regime_color = "#3fb950" if regime_vix < 15 else "#d29922" if regime_vix < 25 else "#f85149"

    def _fmt(p):
        if p is None:
            return "—"
        if abs(p) >= 10000:
            return f"{p:,.0f}"
        return f"{p:,.2f}"

    def _conf_badge(conf: float) -> str:
        pct = int(round(conf * 100))
        col = "#3fb950" if pct >= 70 else "#d29922" if pct >= 60 else "#f85149"
        return f'<span style="color:{col};font-weight:700;">{pct}%</span>'

    def _sig_pill(label: str, value, good: bool, fmt: str = "") -> str:
        col = "#3fb950" if good else "#f85149"
        v   = fmt.format(value) if fmt else str(value)
        return (f'<span class="pro-pill" style="border-color:{col};">'
                f'<span style="color:#8b949e;">{label}:</span> '
                f'<span style="color:{col};font-weight:700;">{v}</span></span>')

    def _card(sym: str) -> str:
        fc = syms.get(sym, {})
        if "error" in fc or not fc:
            err = fc.get("error", "no_data") if fc else "no_data"
            return (f'<div class="alf-card pro-card">'
                    f'<div class="alf-card-hdr"><span class="alf-sym">{sym}</span></div>'
                    f'<div style="color:#f85149;padding:20px;">Unavailable: {err}</div></div>')

        price     = fc.get("price", 0)
        atr       = fc.get("atr", 0)
        rsi       = fc.get("rsi", 50)
        st_dir    = fc.get("st_direction", "—")
        env       = fc.get("env_rating", "NEUTRAL")
        ai_note   = fc.get("ai_note", "")
        signals   = fc.get("signals", {})
        strikes   = fc.get("strikes", {})
        alf_days  = fc.get("alfred_days", {})

        rv_iv        = signals.get("rv_iv", 1.0)
        a_pct        = signals.get("atr_pct", 50.0)
        align        = signals.get("alignment", 0.0)
        s_cont       = signals.get("contango", True)
        s_vix9d      = signals.get("vix9d", 0.0)
        s_vix3m      = signals.get("vix3m", 0.0)
        ivr_val      = signals.get("ivr", 50.0)
        ivr_h        = signals.get("ivr_high", 0.0)
        ivr_l        = signals.get("ivr_low", 0.0)
        v10_pct      = signals.get("vix_10yr_pct", 50.0)

        env_col = "#3fb950" if env == "BUY PREMIUM" else "#f85149" if env == "SELL PREMIUM" else "#d29922"
        st_col  = "#3fb950" if st_dir == "BULLISH" else "#f85149"

        # ── Range bars ──────────────────────────────────────────────────────
        all_highs = [d["high"] for d in alf_days.values() if d.get("high")]
        all_lows  = [d["low"]  for d in alf_days.values() if d.get("low")]
        if all_highs and all_lows:
            scale_min = min(all_lows)  - atr * 0.3
            scale_max = max(all_highs) + atr * 0.3
            scale_w   = scale_max - scale_min
        else:
            scale_min, scale_w = price - atr * 2, atr * 4

        range_rows = ""
        for n in range(1, 4):
            d = alf_days.get(str(n), {})
            if not d:
                continue
            h    = d["high"]
            l    = d["low"]
            rng  = d["range"]
            s_nd = strikes.get(str(n), {})
            exp  = s_nd.get("expiry_label", f"+{n}d")
            left_pct  = 100.0 * (l - scale_min) / scale_w if scale_w else 0
            width_pct = 100.0 * (h - l) / scale_w if scale_w else 20
            range_rows += f"""
  <div class="pro-range-row">
    <div class="pro-range-label">{exp}</div>
    <div class="pro-range-track">
      <div class="pro-range-bar" style="left:{left_pct:.1f}%;width:{width_pct:.1f}%;"></div>
      <div class="pro-range-price pro-range-lo" style="left:{max(0.0, left_pct-0.5):.1f}%">{_fmt(l)}</div>
      <div class="pro-range-price pro-range-hi" style="left:{min(98.0, left_pct+width_pct):.1f}%">{_fmt(h)}</div>
    </div>
    <div class="pro-range-pts">{rng:.0f} pts</div>
  </div>"""

        # ── Signal pills ────────────────────────────────────────────────────
        ivr_col = "#3fb950" if ivr_val < 30 else "#f85149" if ivr_val > 70 else "#8b949e"
        ivr_lbl = "BUY PREM" if ivr_val < 30 else "SELL PREM" if ivr_val > 70 else "NEUTRAL"
        v10_col = "#3fb950" if v10_pct < 35 else "#f85149" if v10_pct > 65 else "#8b949e"
        ivr_pill = (
            f'<span class="pro-pill" style="color:{ivr_col};border-color:{ivr_col}40;" '
            f'title="IV Rank: {ivr_val:.0f}/100 (52w range {ivr_l:.1f}–{ivr_h:.1f})">'
            f'IVR {ivr_val:.0f} · {ivr_lbl}</span>'
        )
        v10_pill = (
            f'<span class="pro-pill" style="color:{v10_col};border-color:{v10_col}40;" '
            f'title="VIX sits at the {v10_pct:.0f}th percentile of its 10-year range">'
            f'VIX {v10_pct:.0f}th%ile (10yr)</span>'
        )
        pills = " ".join([
            _sig_pill("RV/IV", rv_iv, rv_iv < 0.90, "{:.2f}"),
            _sig_pill("Term", ("Contango" if s_cont else "Backwdn"), s_cont),
            _sig_pill("ATR%", int(a_pct), a_pct < 40, "{}th"),
            _sig_pill("Align", align, abs(align) >= 0.25, "{:+.2f}"),
            ivr_pill,
            v10_pill,
        ])
        tech_line = (f'RSI {rsi:.0f} &nbsp;|&nbsp; '
                     f'<span style="color:{st_col};">{st_dir}</span> &nbsp;|&nbsp; '
                     f'VIX9D {s_vix9d:.1f} &nbsp;/&nbsp; VIX3M {s_vix3m:.1f}')

        # ── Strike table ────────────────────────────────────────────────────
        strike_rows = ""
        for n in range(1, 4):
            s   = strikes.get(str(n), {})
            if not s:
                continue
            exp = s.get("expiry_label", f"+{n}d")
            ci  = s.get("call", {})
            pi  = s.get("put",  {})
            buf = s.get("buffer_pts", 0)
            ah  = s.get("alfred_high", 0)
            al  = s.get("alfred_low",  0)

            def _prem_usd(info):
                usd = info.get("premium_$", 0)
                col = "#3fb950" if usd >= 150 else "#d29922" if usd >= 75 else "#f85149"
                chk = " &#10003;" if usd >= 150 else ""
                return f'<span style="color:{col};font-weight:700;">${usd:,}{chk}</span>'

            strike_rows += (
                f'<tr>'
                f'<td class="alf-day">{exp}</td>'
                f'<td style="color:#8b949e;font-size:10px;">{_fmt(ah)}+{buf:.0f}</td>'
                f'<td class="alf-hi" style="font-size:13px;">{_fmt(ci.get("strike"))}</td>'
                f'<td>{_prem_usd(ci)}</td>'
                f'<td>{_conf_badge(ci.get("conf",0))}</td>'
                f'<td style="color:#8b949e;font-size:10px;">{_fmt(al)}-{buf:.0f}</td>'
                f'<td class="alf-lo" style="font-size:13px;">{_fmt(pi.get("strike"))}</td>'
                f'<td>{_prem_usd(pi)}</td>'
                f'<td>{_conf_badge(pi.get("conf",0))}</td>'
                f'</tr>'
            )

        # ── SD expected moves table ─────────────────────────────────────────
        sd_rows = ""
        for n in range(1, 4):
            s = strikes.get(str(n), {})
            if not s:
                continue
            exp = s.get("expiry_label", f"+{n}d")
            sd_rows += (
                f'<tr>'
                f'<td class="alf-day">{exp}</td>'
                f'<td style="color:#3fb950;font-weight:700;">&#177;{s.get("sd1",0):.1f}</td>'
                f'<td style="color:#8b949e;">{_fmt(s.get("sd1_low",0))} – {_fmt(s.get("sd1_high",0))}</td>'
                f'<td style="color:#d29922;font-weight:700;">&#177;{s.get("sd2",0):.1f}</td>'
                f'<td style="color:#8b949e;">{_fmt(s.get("sd2_low",0))} – {_fmt(s.get("sd2_high",0))}</td>'
                f'<td style="color:#f85149;font-weight:700;">&#177;{s.get("sd3",0):.1f}</td>'
                f'<td style="color:#8b949e;">{_fmt(s.get("sd3_low",0))} – {_fmt(s.get("sd3_high",0))}</td>'
                f'</tr>'
            )
        sd_html = f"""
  <div class="pro-section-hdr" style="margin-top:12px;">EXPECTED MOVES (DTE-MATCHED) &nbsp;<span style="color:#8b949e;font-weight:400;font-size:10px;">&#963; × price × √(DTE/365)</span></div>
  <table class="alf-opts-tbl">
    <thead>
      <tr>
        <th>Expiry</th>
        <th style="color:#3fb950;">1SD &#177;</th><th style="color:#8b949e;font-size:9px;">1SD Range (68%)</th>
        <th style="color:#d29922;">2SD &#177;</th><th style="color:#8b949e;font-size:9px;">2SD Range (95%)</th>
        <th style="color:#f85149;">3SD &#177;</th><th style="color:#8b949e;font-size:9px;">3SD Range (99.7%)</th>
      </tr>
    </thead>
    <tbody>{sd_rows}</tbody>
  </table>"""

        # ── AI note ─────────────────────────────────────────────────────────
        ai_html = ""
        if ai_note:
            note_col = "#3fb950" if env == "BUY PREMIUM" else "#f85149" if env == "SELL PREMIUM" else "#d29922"
            ai_html  = f"""
  <div class="pro-ai-box" style="border-color:{note_col}20;background:{note_col}08;">
    <div class="pro-ai-hdr" style="color:{note_col};">&#9670; AI Edge Assessment</div>
    <div class="pro-ai-text">{ai_note}</div>
  </div>"""
        else:
            ai_html = ('<div class="pro-ai-box" style="border-color:#30363d;">'
                       '<div class="pro-ai-hdr" style="color:#8b949e;">&#9670; AI Edge Assessment</div>'
                       '<div class="pro-ai-text" style="color:#8b949e;font-style:italic;">'
                       'Set ANTHROPIC_API_KEY to enable AI synthesis.</div></div>')

        return f"""
<div class="alf-card pro-card">
  <div class="alf-card-hdr">
    <span class="alf-sym">{sym}</span>
    <span class="alf-price">{_fmt(price)}</span>
    <span class="alf-st" style="color:{st_col};">{st_dir}</span>
    <span class="pro-env-badge" style="color:{env_col};border-color:{env_col};">{env}</span>
  </div>

  <div class="pro-section-hdr">ALFRED FORECAST RANGE &nbsp;<span style="color:#8b949e;font-weight:400;font-size:10px;">Alfred range: strike selection basis</span></div>
  <div class="pro-range-wrap">{range_rows}</div>

  <div class="pro-section-hdr" style="margin-top:12px;">SIGNAL STACK</div>
  <div class="pro-pills">{pills}</div>
  <div class="alf-meta" style="margin-top:5px;">{tech_line}</div>

  <div class="pro-section-hdr" style="margin-top:12px;">STRIKE RECOMMENDATIONS &nbsp;<span style="color:#8b949e;font-weight:400;font-size:10px;">from Alfred range &plusmn; ATR buffer</span></div>
  <table class="alf-opts-tbl">
    <thead>
      <tr>
        <th>Expiry</th>
        <th style="color:#8b949e;font-size:9px;">Alfred H+buf</th>
        <th style="color:#3fb950;">Call Strike &#9650;</th>
        <th style="color:#3fb950;">Premium</th>
        <th>Conf</th>
        <th style="color:#8b949e;font-size:9px;">Alfred L-buf</th>
        <th style="color:#f85149;">Put Strike &#9660;</th>
        <th style="color:#f85149;">Premium</th>
        <th>Conf</th>
      </tr>
    </thead>
    <tbody>{strike_rows}</tbody>
  </table>
  {sd_html}
  {ai_html}
</div>"""

    cards_top = _card("/ES") + _card("/MES")
    cards_bot = _card("/NQ") + _card("/MNQ")
    no_data   = not syms

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Alfred Pro &mdash; AI-First Options</title>
  <style>
    *{{box-sizing:border-box;}}
    body{{margin:0;background:#0d1117;color:#e6edf3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px;}}
    {_NAV_CSS}
    .alf-wrap{{max-width:1500px;margin:0 auto;padding:16px;}}
    .alf-infobar{{display:flex;align-items:center;gap:16px;padding:10px 16px;background:#161b22;
      border:1px solid #21262d;border-radius:8px;margin-bottom:16px;flex-wrap:wrap;}}
    .alf-infobar span{{color:#8b949e;font-size:12px;}}
    .alf-infobar b{{color:#e6edf3;}}
    .alf-refresh-btn{{padding:5px 14px;border-radius:5px;border:1px solid #d29922;
      background:rgba(210,153,34,0.1);color:#d29922;cursor:pointer;font-size:12px;margin-left:auto;}}
    .alf-refresh-btn:hover{{background:rgba(210,153,34,0.2);}}
    .alf-grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px;}}
    @media(max-width:900px){{.alf-grid{{grid-template-columns:1fr;}}}}
    .alf-card{{background:#161b22;border:1px solid #21262d;border-radius:10px;padding:16px;}}
    .pro-card{{border-color:#d2992230;}}
    .alf-card-hdr{{display:flex;align-items:baseline;gap:12px;margin-bottom:8px;flex-wrap:wrap;}}
    .alf-sym{{font-size:18px;font-weight:700;color:#58a6ff;}}
    .alf-price{{font-size:22px;font-weight:700;color:#e6edf3;}}
    .alf-st{{font-size:11px;font-weight:700;padding:2px 6px;border-radius:4px;background:rgba(255,255,255,0.05);}}
    .alf-meta{{font-size:11px;color:#8b949e;}}
    .pro-env-badge{{font-size:10px;font-weight:700;padding:2px 8px;border-radius:4px;border:1px solid;
      background:rgba(255,255,255,0.04);margin-left:auto;letter-spacing:0.5px;}}
    .pro-section-hdr{{font-size:10px;font-weight:700;color:#8b949e;text-transform:uppercase;
      letter-spacing:0.6px;margin-bottom:8px;}}
    .pro-range-wrap{{padding:4px 0;}}
    .pro-range-row{{display:flex;align-items:center;gap:8px;margin-bottom:8px;}}
    .pro-range-label{{width:64px;font-size:11px;color:#8b949e;flex-shrink:0;}}
    .pro-range-track{{flex:1;height:20px;background:#21262d;border-radius:3px;position:relative;overflow:visible;}}
    .pro-range-bar{{position:absolute;top:0;height:100%;background:linear-gradient(90deg,#f8514940,#3fb95040);
      border:1px solid #d2992260;border-radius:2px;}}
    .pro-range-price{{position:absolute;top:-14px;font-size:10px;font-weight:700;white-space:nowrap;}}
    .pro-range-lo{{color:#f85149;transform:translateX(-50%);}}
    .pro-range-hi{{color:#3fb950;transform:translateX(-50%);}}
    .pro-range-pts{{width:50px;font-size:11px;color:#d29922;text-align:right;flex-shrink:0;}}
    .pro-pills{{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:4px;}}
    .pro-pill{{font-size:11px;padding:3px 8px;border-radius:4px;border:1px solid;background:rgba(255,255,255,0.03);}}
    .pro-ai-box{{margin-top:12px;border:1px solid;border-radius:6px;padding:10px 12px;}}
    .pro-ai-hdr{{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px;}}
    .pro-ai-text{{font-size:12px;color:#c9d1d9;line-height:1.7;}}
    .alf-opts-tbl{{width:100%;border-collapse:collapse;font-size:12px;margin-top:4px;}}
    .alf-opts-tbl th{{color:#8b949e;font-weight:600;padding:3px 6px;border-bottom:1px solid #21262d;
      text-align:left;font-size:10px;}}
    .alf-opts-tbl td{{padding:5px 6px;border-bottom:1px solid #161b22;vertical-align:middle;}}
    .alf-opts-tbl tbody tr:hover{{background:#1c2128;}}
    .alf-day{{color:#8b949e;font-weight:600;}}
    .alf-hi{{color:#3fb950;font-weight:700;}}
    .alf-lo{{color:#f85149;font-weight:700;}}
    .alf-placeholder{{text-align:center;color:#8b949e;padding:40px;font-size:14px;}}
    .pro-legend{{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:14px 18px;
      margin-bottom:14px;font-size:11px;color:#8b949e;line-height:1.8;}}
    .pro-legend b{{color:#e6edf3;}}
    .pro-legend-title{{font-size:12px;font-weight:700;color:#d29922;margin-bottom:6px;}}
    #pro-status{{font-size:11px;color:#8b949e;}}
    .pro-info-btn{{background:none;border:1px solid #30363d;border-radius:4px;color:#8b949e;
      cursor:pointer;font-size:12px;padding:3px 8px;margin-right:8px;}}
    .pro-info-btn:hover{{border-color:#58a6ff;color:#58a6ff;}}
    .pro-info-overlay{{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:900;
      align-items:flex-start;justify-content:center;padding-top:40px;overflow-y:auto;}}
    .pro-info-overlay.open{{display:flex;}}
    .pro-info-modal{{background:#161b22;border:1px solid #30363d;border-radius:10px;
      max-width:720px;width:95%;padding:24px 28px;position:relative;}}
    .pro-info-close{{position:absolute;top:14px;right:16px;background:none;border:none;
      color:#8b949e;font-size:18px;cursor:pointer;line-height:1;}}
    .pro-info-close:hover{{color:#e6edf3;}}
    .pro-info-title{{font-size:15px;font-weight:700;color:#e6edf3;margin-bottom:16px;}}
    .pro-info-section{{margin-bottom:14px;}}
    .pro-info-section h3{{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;
      color:#d29922;margin:0 0 6px;}}
    .pro-info-section p,.pro-info-section li{{font-size:12px;color:#8b949e;line-height:1.7;margin:0 0 4px;}}
    .pro-info-section ul{{margin:4px 0 0 16px;padding:0;}}
    .pro-info-section b{{color:#e6edf3;}}
    .pro-info-section .green{{color:#3fb950;font-weight:700;}}
    .pro-info-section .red{{color:#f85149;font-weight:700;}}
    .pro-info-section .amber{{color:#d29922;font-weight:700;}}
    .pro-info-divider{{border:none;border-top:1px solid #21262d;margin:14px 0;}}
  </style>
</head>
<body>
<div class="sticky-banner">
  <header>
    <h1 style="margin:0;font-size:17px;font-weight:700;">&#9733; Alfred Pro &mdash; AI-First Options</h1>
    <div class="header-right">
      <a href="/alfred" style="color:#58a6ff;font-size:11px;text-decoration:none;
        border:1px solid #30363d;border-radius:4px;padding:3px 8px;margin-right:8px;">
        &#9664; Alfred Standard</a>
      <button class="pro-info-btn" onclick="proShowInfo()">&#9432; How It Works</button>
      {nav}
    </div>
  </header>
</div>
{_NAV_TAPE_HTML}

<div class="alf-wrap">
  <div class="alf-infobar">
    <span>Updated: <b id="pro-updated">{updated}</b></span>
    <span>Next refresh: <b id="pro-countdown">5:00</b></span>
    <span>VIX: <b>{vix_val}</b></span>
    <span>VIX9D: <b>{vix9d:.1f}</b></span>
    <span>VIX3M: <b>{vix3m:.1f}</b></span>
    <span>Term: <b style="color:{term_color};">{term_label}</b></span>
    <span>Regime: <b style="color:{regime_color};">{regime_label}</b></span>
    <span id="pro-status"></span>
    <button class="alf-refresh-btn" onclick="proForceRefresh()">&#8635; Refresh Now</button>
  </div>

  <div class="pro-legend">
    <div class="pro-legend-title">&#9670; How Alfred Pro differs from standard Alfred</div>
    <b>Strike selection:</b> Based on Alfred's multi-factor forecast range (ATR &times; &radic;N &times; VIX_mult + RSI/MACD/SuperTrend bias), not a Black-Scholes lognormal quantile. &nbsp;|&nbsp;
    <b>Confidence:</b> Multi-signal score from RV/IV ratio, VIX term structure, ATR percentile, and directional consensus — not N(-d2). &nbsp;|&nbsp;
    <b>RV/IV &lt; 1.0:</b> Realized vol cheaper than implied → premium sellers have edge. &nbsp;|&nbsp;
    <b>Contango (VIX &lt; VIX3M):</b> Calm, stable regime → favorable for selling. &nbsp;|&nbsp;
    <b>ATR% &lt; 40th:</b> Below-average volatility expansion → tighter ranges.
    <br><span style="color:#f85149;font-weight:700;">&#9888;</span>
    Premium figures are Black-76 theoretical values at Alfred-range-derived strikes. Always verify live quotes before entering.
  </div>

  {'<div class="alf-placeholder">No Pro forecast yet &mdash; click Refresh Now.</div>' if no_data else f'''
  <div class="alf-grid">{cards_top}</div>
  <div class="alf-grid">{cards_bot}</div>
  '''}
</div>

{_NAV_TAPE_JS}
<script>
var _proCountdown = 300;
var _proInterval  = null;

function proCountdownTick() {{
  _proCountdown--;
  if (_proCountdown <= 0) {{
    _proCountdown = 300;
    location.reload();
  }}
  var m = Math.floor(_proCountdown / 60);
  var s = _proCountdown % 60;
  var el = document.getElementById('pro-countdown');
  if (el) el.textContent = m + ':' + (s < 10 ? '0' : '') + s;
}}

function proForceRefresh() {{
  var st = document.getElementById('pro-status');
  if (st) st.textContent = 'Refreshing… (~30s)';
  fetch('/api/alfred/pro/refresh', {{method:'POST'}}).then(function() {{
    setTimeout(function() {{ location.reload(); }}, 35000);
  }});
}}

_proInterval = setInterval(proCountdownTick, 1000);

function proShowInfo() {{
  document.getElementById('pro-info-overlay').classList.add('open');
}}
function proHideInfo() {{
  document.getElementById('pro-info-overlay').classList.remove('open');
}}
document.addEventListener('keydown', function(e) {{
  if (e.key === 'Escape') proHideInfo();
}});
</script>

<div class="pro-info-overlay" id="pro-info-overlay" onclick="if(event.target===this)proHideInfo()">
  <div class="pro-info-modal">
    <button class="pro-info-close" onclick="proHideInfo()">&#10005;</button>
    <div class="pro-info-title">&#9432; Alfred Pro &mdash; How It Works</div>

    <div class="pro-info-section">
      <h3>Primary Signal &mdash; IV Rank (IVR)</h3>
      <p>IVR measures where current implied volatility sits within its 52-week range (0–100).</p>
      <ul>
        <li><b class="green">IVR &lt; 30</b> &rarr; <span class="green">BUY PREMIUM</span> &mdash; IV is near its year low. Options are cheap; buying premium has edge.</li>
        <li><b class="red">IVR &gt; 70</b> &rarr; <span class="red">SELL PREMIUM</span> &mdash; IV is near its year high. Options are expensive; selling premium has edge.</li>
        <li><b class="amber">IVR 30–70</b> &rarr; <span class="amber">NEUTRAL</span> &mdash; mid-range; secondary signals decide (RV/IV, term structure, ATR, alignment).</li>
      </ul>
      <p>Formula: <b>IVR = (current &minus; 52w&nbsp;low) &divide; (52w&nbsp;high &minus; 52w&nbsp;low) &times; 100</b><br>
      Source: ^VIX for ES/MES &nbsp;|&nbsp; ^VXN for NQ/MNQ. Cached once per trading day.</p>
    </div>

    <hr class="pro-info-divider">

    <div class="pro-info-section">
      <h3>Secondary Context &mdash; VIX 10-Year Percentile</h3>
      <p>Shows where today's VIX sits within its full 10-year daily close history.</p>
      <ul>
        <li><span class="green">&lt; 35th %ile</span> &mdash; historically low vol environment (premium is cheap).</li>
        <li><span class="red">&gt; 65th %ile</span> &mdash; historically elevated vol (premium is expensive).</li>
      </ul>
      <p>Used by the AI Edge Assessment for conviction framing. Does not override the IVR-based env_rating.</p>
    </div>

    <hr class="pro-info-divider">

    <div class="pro-info-section">
      <h3>Signal Stack Pills</h3>
      <ul>
        <li><b>RV/IV</b> &mdash; 20-day realized vol &divide; current IV. &lt; 1.0 = IV rich = sellers have edge.</li>
        <li><b>Term</b> &mdash; VIX term structure. Contango (VIX &lt; VIX3M) = calm regime. Backwardation = stress.</li>
        <li><b>ATR%</b> &mdash; Percentile rank of today's ATR(14) within its 30-day history. &lt; 40th = low expansion.</li>
        <li><b>Align</b> &mdash; Consensus of RSI, MACD, SuperTrend, and TipRanks bias. &plusmn;1.0 scale; |align| &ge; 0.25 is directional.</li>
        <li><b>IVR</b> &mdash; IV Rank (primary signal, see above).</li>
        <li><b>VIX %ile (10yr)</b> &mdash; Macro vol context (see above).</li>
      </ul>
    </div>

    <hr class="pro-info-divider">

    <div class="pro-info-section">
      <h3>Strike Selection</h3>
      <p>Strikes are derived from Alfred's multi-factor forecast range, <b>not</b> a Black-Scholes lognormal quantile.</p>
      <ul>
        <li><b>Call strike</b> = Alfred forecast high + ATR buffer</li>
        <li><b>Put strike</b> = Alfred forecast low &minus; ATR buffer</li>
        <li>Buffer grows when RV/IV &gt; 1.10 or ATR% &gt; 65th (elevated vol).</li>
      </ul>
      <p>Alfred range = <b>ATR &times; &radic;N &times; VIX_mult &times; calib[N]</b>, centered on price + directional bias.</p>
    </div>

    <hr class="pro-info-divider">

    <div class="pro-info-section">
      <h3>Expected Moves (DTE-Matched)</h3>
      <p>Standard-deviation price ranges scaled to each expiry's exact days to expiration:</p>
      <p><b>Expected Move = &sigma;<sub>annual</sub> &times; Price &times; &radic;(DTE &divide; 365)</b></p>
      <ul>
        <li><span class="green">1SD</span> &mdash; 68% probability of price staying within range.</li>
        <li><span class="amber">2SD</span> &mdash; 95% probability.</li>
        <li><span class="red">3SD</span> &mdash; 99.7% probability (tail risk boundary).</li>
      </ul>
    </div>

    <hr class="pro-info-divider">

    <div class="pro-info-section">
      <h3>Confidence Score</h3>
      <p>Multi-signal composite (not N(&minus;d2)). Base = 60%. Bonuses: RV/IV &lt; 0.85 (+8%), contango (+6%), ATR% &lt; 35th (+5%), |align| &gt; 0.50 (+6%). Capped at 90%. Call/put adjusted &plusmn;5% by directional alignment.</p>
    </div>

    <hr class="pro-info-divider">

    <div class="pro-info-section">
      <h3>AI Edge Assessment</h3>
      <p>Claude Haiku answers 3 specific questions each refresh cycle (cached 4 hours):</p>
      <ul>
        <li>Should the trader BUY or SELL premium, and how strong is the conviction?</li>
        <li>Which side (call or put) has better edge, and which SD level offers best risk/reward?</li>
        <li>What is the single most important risk to these trades today?</li>
      </ul>
      <p>Border color matches the environment: <span class="green">green = BUY PREMIUM</span> &nbsp;|&nbsp; <span class="red">red = SELL PREMIUM</span> &nbsp;|&nbsp; <span class="amber">orange = NEUTRAL</span>.</p>
    </div>

    <hr class="pro-info-divider">

    <div class="pro-info-section">
      <h3>&#9888; Important Disclaimer</h3>
      <p>Premium figures are <b>Black-76 theoretical values</b> at Alfred-range-derived strikes. Always verify live bid/ask quotes before entering any position. Alfred Pro is a decision support tool, not a trade execution system.</p>
    </div>
  </div>
</div>

</body>
</html>"""


# ── Alfred Pro routes ─────────────────────────────────────────────────────────

@alfred_router.get("/alfred-pro", response_class=HTMLResponse)
async def alfred_pro_page():
    _compute_alfred_pro()
    data = _load_pro_forecast()
    return HTMLResponse(_build_alfred_pro_html(data))


@alfred_router.get("/api/alfred/pro/forecast")
def api_alfred_pro_forecast():
    data = _load_pro_forecast()
    return JSONResponse(data if data else {"error": "no_pro_forecast_yet"})


@alfred_router.post("/api/alfred/pro/refresh")
def api_alfred_pro_refresh():
    threading.Thread(target=_compute_alfred_pro, kwargs={"force": True},
                     daemon=True, name="alfred-pro-refresh").start()
    return JSONResponse({"status": "pro_refresh_started"})
