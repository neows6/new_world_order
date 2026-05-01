"""
monitor/itool.py — I-Tool: S&P 500 Technical Scanner

Scans all S&P 500 stocks for bullish/bearish signals using:
  - Price above SMA(30) or SMA(50) or EMA(30) or EMA(50)  [trend filter]
  - MACD histogram (8, 17, 9) crossed above zero in last 5 trading days [momentum]
  - Stochastic (14, 5, 5) %K rose from below 25 in last 5 days [oversold recovery]

All three conditions must be met simultaneously for a BULLISH signal.
Bearish: price below all MAs + MACD crossed below 0 + Stoch fell from above 75.

Results cached to data/itool_scan.json, stale after 35 minutes.
Schedule: every 30 min Mon-Fri 9am–4pm via APScheduler in main.py.
"""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
import yfinance as yf
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = ROOT / "data" / "itool_scan.json"
SP500_CACHE = ROOT / "data" / "sp500_tickers.json"
STALE_MINUTES = 35


# ── Indicator helpers (pure lists — JSON-serialisable) ────────────────────────

def _sma(prices: list, period: int) -> list:
    out = []
    for i in range(len(prices)):
        if i < period - 1:
            out.append(None)
        else:
            out.append(sum(prices[i - period + 1 : i + 1]) / period)
    return out


def _ema(prices: list, period: int) -> list:
    """Exponential moving average seeded with the first SMA."""
    if len(prices) < period:
        return [None] * len(prices)
    k = 2.0 / (period + 1)
    out = [None] * (period - 1)
    seed = sum(prices[:period]) / period
    out.append(seed)
    for p in prices[period:]:
        out.append(out[-1] * (1 - k) + p * k)
    return out


def _macd_histogram(prices: list, fast: int = 8, slow: int = 17, signal: int = 9) -> list:
    """Returns histogram aligned to the full prices length (None-padded at start)."""
    ema_fast = _ema(prices, fast)
    ema_slow = _ema(prices, slow)

    # MACD line — aligned to slow EMA (first non-None index)
    slow_start = slow - 1
    fast_tail  = ema_fast[slow_start:]
    slow_tail  = ema_slow[slow_start:]
    macd_line  = [f - s for f, s in zip(fast_tail, slow_tail)]

    sig_line   = _ema(macd_line, signal)
    sig_start  = signal - 1  # first non-None index inside sig_line

    hist_raw = [m - s for m, s in zip(macd_line[sig_start:], sig_line[sig_start:])]

    # Pad to full length
    pad = len(prices) - len(hist_raw)
    return [None] * pad + hist_raw


def _stochastic(highs: list, lows: list, closes: list,
                k_period: int = 14, k_smooth: int = 5, d_period: int = 5):
    """Slow Stochastic — returns (k_pct, d_pct) both aligned to closes length."""
    n = len(closes)
    raw_k = []
    for i in range(n):
        if i < k_period - 1:
            raw_k.append(None)
            continue
        lo  = min(lows[i - k_period + 1 : i + 1])
        hi  = max(highs[i - k_period + 1 : i + 1])
        rng = hi - lo
        raw_k.append(100.0 * (closes[i] - lo) / rng if rng else 50.0)

    # Slow %K = SMA(k_smooth) of raw_k
    valid_raw = [v for v in raw_k if v is not None]
    if len(valid_raw) < k_smooth:
        return [None] * n, [None] * n

    k_raw_start = next(i for i, v in enumerate(raw_k) if v is not None)
    k_sma = _sma(valid_raw, k_smooth)
    k_pad = [None] * (k_raw_start + k_smooth - 1) + [v for v in k_sma if v is not None]
    k_pct = k_pad[:n] + [None] * max(0, n - len(k_pad))

    valid_k = [v for v in k_pct if v is not None]
    if len(valid_k) < d_period:
        return k_pct, [None] * n

    k_start = next(i for i, v in enumerate(k_pct) if v is not None)
    d_sma = _sma(valid_k, d_period)
    d_pad = [None] * (k_start + d_period - 1) + [v for v in d_sma if v is not None]
    d_pct = d_pad[:n] + [None] * max(0, n - len(d_pad))

    return k_pct, d_pct


# ── S&P 500 ticker list ───────────────────────────────────────────────────────

_FALLBACK_TICKERS = [
    "AAPL","MSFT","AMZN","NVDA","GOOGL","GOOG","META","TSLA","BRK-B","UNH",
    "LLY","JPM","XOM","JNJ","V","PG","MA","AVGO","HD","CVX","MRK","ABBV",
    "COST","PEP","KO","ADBE","WMT","BAC","CRM","MCD","CSCO","ACN","TMO","ABT",
    "NFLX","NKE","AMD","DHR","LIN","TXN","PM","ORCL","NEE","UPS","AMGN","QCOM",
    "MS","INTU","LOW","CAT","HON","IBM","SPGI","MDT","GS","AMAT","AXP","DE","BLK",
    "ISRG","SYK","ELV","ADI","VRTX","REGN","CI","ZTS","MO","GILD","MDLZ","PLD",
    "SCHW","TJX","MMC","EOG","USB","PNC","BSX","ITW","SO","DUK","WM","NOC","GE",
    "RTX","LMT","FDX","NSC","EMR","CSX","AIG","MMM","D","SHW","ECL","APD","F",
    "GM","INTC","T","VZ","CMCSA","CHTR","TMUS","WBA","CVS","HUM","AET","ANTM",
    "SPG","EQR","AMT","CCI","O","PSA","WELL","DRE","ARE","MAA",
]


def _fetch_sp500_tickers() -> list:
    """Fetch current S&P 500 constituents from Wikipedia. Caches to disk."""
    try:
        resp = requests.get(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers={"User-Agent": "NWO-Trading-Bot/1.0"},
            timeout=15,
        )
        from html.parser import HTMLParser

        class _TableParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.in_table = False
                self.in_td = False
                self.first_td = False
                self.tickers = []
                self._row_count = 0

            def handle_starttag(self, tag, attrs):
                attrs_dict = dict(attrs)
                if tag == "table" and attrs_dict.get("id") == "constituents":
                    self.in_table = True
                if self.in_table and tag == "tr":
                    self._row_count += 1
                    self.first_td = True
                if self.in_table and self.first_td and tag == "td":
                    self.in_td = True

            def handle_endtag(self, tag):
                if tag == "td" and self.in_td:
                    self.in_td = False
                    self.first_td = False

            def handle_data(self, data):
                if self.in_td and self._row_count > 1:
                    ticker = data.strip().replace(".", "-")
                    if ticker:
                        self.tickers.append(ticker)

        parser = _TableParser()
        parser.feed(resp.text)
        tickers = parser.tickers[:505]  # trim to ~500

        if len(tickers) >= 400:
            SP500_CACHE.parent.mkdir(parents=True, exist_ok=True)
            SP500_CACHE.write_text(json.dumps(tickers))
            logger.info(f"[ITOOL] Fetched {len(tickers)} S&P 500 tickers from Wikipedia")
            return tickers
    except Exception as e:
        logger.warning(f"[ITOOL] Wikipedia fetch failed: {e}")

    # Try disk cache
    if SP500_CACHE.exists():
        try:
            tickers = json.loads(SP500_CACHE.read_text())
            logger.info(f"[ITOOL] Using cached S&P 500 list ({len(tickers)} tickers)")
            return tickers
        except Exception:
            pass

    logger.warning(f"[ITOOL] Using fallback ticker list ({len(_FALLBACK_TICKERS)} tickers)")
    return _FALLBACK_TICKERS


# ── Per-ticker analysis ───────────────────────────────────────────────────────

def _analyze_ticker(ticker: str, dates: list, closes: list,
                    highs: list, lows: list) -> Optional[dict]:
    """
    Compute indicators and check signal criteria.
    Returns a result dict if bullish or bearish, else None.
    """
    n = len(closes)
    if n < 55:  # need 50-day SMA + buffer
        return None

    LOOKBACK = 10  # trading days to look back for crossovers

    sma30_s = _sma(closes, 30)
    sma50_s = _sma(closes, 50)
    ema30_s = _ema(closes, 30)
    ema50_s = _ema(closes, 50)
    macd_h  = _macd_histogram(closes, fast=8, slow=17, signal=9)
    k_pct, d_pct = _stochastic(highs, lows, closes, k_period=14, k_smooth=5, d_period=5)

    price = closes[-1]

    # Current MA values (last non-None)
    def last_val(series):
        for v in reversed(series):
            if v is not None:
                return v
        return None

    sma30_v = last_val(sma30_s)
    sma50_v = last_val(sma50_s)
    ema30_v = last_val(ema30_s)
    ema50_v = last_val(ema50_s)

    # ── Trend: price above/below key MAs ─────────────────────
    # Bullish: price above SMA30 or SMA50
    above_ma = (sma30_v is not None and price > sma30_v) or \
               (sma50_v is not None and price > sma50_v)
    # Bearish: price below SMA30 (enough — avoids requiring all 4)
    below_ma = sma30_v is not None and price < sma30_v

    # ── MACD histogram crossovers in last LOOKBACK days ─────
    # Get last LOOKBACK+1 non-None histogram values with their original indices
    hist_window = [(i, v) for i, v in enumerate(macd_h) if v is not None]
    hist_window = hist_window[-(LOOKBACK + 1):]

    macd_bull_cross = False
    macd_bear_cross = False
    bull_macd_idx = []
    bear_macd_idx = []

    for j in range(1, len(hist_window)):
        prev_i, prev_v = hist_window[j - 1]
        curr_i, curr_v = hist_window[j]
        if prev_v <= 0 and curr_v > 0:
            macd_bull_cross = True
            bull_macd_idx.append(curr_i)
        elif prev_v >= 0 and curr_v < 0:
            macd_bear_cross = True
            bear_macd_idx.append(curr_i)

    # ── Stochastic: %K rose from below 25 / fell from above 75 ──
    k_window = [(i, v) for i, v in enumerate(k_pct) if v is not None]
    k_window = k_window[-(LOOKBACK + 3):]  # extra buffer for prev values

    stoch_bull = False
    stoch_bear = False
    bull_stoch_idx = []
    bear_stoch_idx = []

    for j in range(1, len(k_window)):
        prev_i, prev_v = k_window[j - 1]
        curr_i, curr_v = k_window[j]
        if prev_v < 25 and curr_v > prev_v:
            stoch_bull = True
            bull_stoch_idx.append(curr_i)
        if prev_v > 75 and curr_v < prev_v:
            stoch_bear = True
            bear_stoch_idx.append(curr_i)

    # ── Determine overall signal ──────────────────────────────
    bullish = above_ma and macd_bull_cross and stoch_bull
    bearish = below_ma and macd_bear_cross and stoch_bear

    if not bullish and not bearish:
        return None

    # ── Build chart series (last 90 days, all aligned) ───────
    chart_len = min(90, n)
    tail = slice(-chart_len, None)

    def trim(series):
        return [round(v, 4) if v is not None else None for v in series[tail]]

    # Crossover markers — convert absolute index to chart-relative index
    offset = n - chart_len

    def to_chart_idx(indices):
        return [i - offset for i in indices if i >= offset]

    return {
        "ticker":     ticker,
        "signal":     "bullish" if bullish else "bearish",
        "price":      round(price, 2),
        "sma30":      round(sma30_v, 2) if sma30_v else None,
        "sma50":      round(sma50_v, 2) if sma50_v else None,
        "ema30":      round(ema30_v, 2) if ema30_v else None,
        "ema50":      round(ema50_v, 2) if ema50_v else None,
        "macd_hist":  round(last_val(macd_h) or 0, 4),
        "stoch_k":    round(last_val(k_pct) or 0, 2),
        "stoch_d":    round(last_val(d_pct) or 0, 2),
        # Chart series
        "dates":          dates[-chart_len:],
        "closes":         trim(closes),
        "sma30_series":   trim(sma30_s),
        "sma50_series":   trim(sma50_s),
        "macd_hist_series": trim(macd_h),
        "stoch_k_series": trim(k_pct),
        "stoch_d_series": trim(d_pct),
        # Crossover marker positions within the chart window
        "bull_price_idx":  to_chart_idx(bull_macd_idx + bull_stoch_idx),
        "bear_price_idx":  to_chart_idx(bear_macd_idx + bear_stoch_idx),
        "bull_macd_idx":   to_chart_idx(bull_macd_idx),
        "bear_macd_idx":   to_chart_idx(bear_macd_idx),
        "bull_stoch_idx":  to_chart_idx(bull_stoch_idx),
        "bear_stoch_idx":  to_chart_idx(bear_stoch_idx),
    }


# ── Scanner class ─────────────────────────────────────────────────────────────

class IToolScanner:

    def __init__(self):
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)

    def is_stale(self) -> bool:
        if not CACHE_PATH.exists():
            return True
        try:
            cached = json.loads(CACHE_PATH.read_text())
            gen = datetime.fromisoformat(cached.get("generated_at", "2000-01-01"))
            age_min = (datetime.utcnow() - gen).total_seconds() / 60
            return age_min >= STALE_MINUTES
        except Exception:
            return True

    def load_cached(self) -> Optional[dict]:
        try:
            if CACHE_PATH.exists():
                return json.loads(CACHE_PATH.read_text())
        except Exception:
            pass
        return None

    def _save(self, result: dict):
        CACHE_PATH.write_text(json.dumps(result, default=str))

    def _scan_batch(self, tickers: list) -> list:
        """Download one batch of up to 100 tickers and analyse each."""
        results = []

        # yfinance single-ticker edge case: pad to at least 2 so we always get MultiIndex
        fetch_tickers = tickers if len(tickers) > 1 else tickers + ["SPY"]

        try:
            raw = yf.download(
                fetch_tickers,
                period="6mo",      # ~126 trading days — enough for SMA50 + MACD warmup
                interval="1d",
                group_by="ticker",
                auto_adjust=True,
                progress=False,
                threads=True,
            )
        except Exception as e:
            logger.warning(f"[ITOOL] yfinance batch download failed: {e}")
            return results

        for ticker in tickers:
            try:
                if ticker not in raw.columns.get_level_values(0):
                    continue
                df = raw[ticker].dropna(how="all")
                if df.empty or len(df) < 55:
                    continue

                dates  = [d.strftime("%Y-%m-%d") for d in df.index]
                closes = [float(v) for v in df["Close"].ffill()]
                highs  = [float(v) for v in df["High"].ffill()]
                lows   = [float(v) for v in df["Low"].ffill()]

                result = _analyze_ticker(ticker, dates, closes, highs, lows)
                if result:
                    results.append(result)
            except Exception as e:
                logger.debug(f"[ITOOL] {ticker}: {e}")

        return results

    def scan(self) -> dict:
        """Full S&P 500 scan. Caches and returns result dict."""
        logger.info("[ITOOL] Starting S&P 500 technical scan...")
        t0 = time.time()

        sp500 = _fetch_sp500_tickers()
        batch_size = 100
        all_results = []

        for i in range(0, len(sp500), batch_size):
            batch = sp500[i : i + batch_size]
            batch_num = i // batch_size + 1
            total_batches = (len(sp500) + batch_size - 1) // batch_size
            logger.info(f"[ITOOL] Batch {batch_num}/{total_batches} ({len(batch)} tickers)...")
            try:
                all_results.extend(self._scan_batch(batch))
            except Exception as e:
                logger.warning(f"[ITOOL] Batch {batch_num} failed: {e}")
            time.sleep(0.5)  # polite pacing between batches

        # Sort alphabetically
        all_results.sort(key=lambda x: x["ticker"])

        bullish = [r for r in all_results if r["signal"] == "bullish"]
        bearish = [r for r in all_results if r["signal"] == "bearish"]

        result = {
            "generated_at":  datetime.utcnow().isoformat(),
            "total_scanned": len(sp500),
            "results":       all_results,
            "counts": {
                "total":   len(all_results),
                "bullish": len(bullish),
                "bearish": len(bearish),
            },
        }

        self._save(result)
        elapsed = time.time() - t0
        logger.info(
            f"[ITOOL] Scan complete in {elapsed:.0f}s — "
            f"{len(bullish)} bullish, {len(bearish)} bearish of {len(sp500)} scanned"
        )

        _send_telegram_alert(bullish, bearish, len(sp500))
        return result


def _send_telegram_alert(bullish: list, bearish: list, total: int):
    """Send Telegram alert when scan finds new signals."""
    if not bullish and not bearish:
        return
    try:
        from monitor.telegram_bot import send_alert
        lines = [f"<b>📡 I-Tool Scan</b> ({total} stocks scanned)"]
        if bullish:
            syms = " ".join(r["ticker"] for r in bullish[:25])
            lines.append(f"<b>▲ Bullish ({len(bullish)}):</b> {syms}")
        if bearish:
            syms = " ".join(r["ticker"] for r in bearish[:25])
            lines.append(f"<b>▼ Bearish ({len(bearish)}):</b> {syms}")
        send_alert("\n".join(lines))
    except Exception as e:
        logger.debug(f"[ITOOL] Telegram alert failed: {e}")


# ── Public entry point ────────────────────────────────────────────────────────

def get_scan(force_refresh: bool = False) -> dict:
    """Return cached scan or run a fresh one."""
    scanner = IToolScanner()
    if force_refresh or scanner.is_stale():
        return scanner.scan()
    return scanner.load_cached() or scanner.scan()
