"""
utils/price_data.py — shared OHLCV loading helpers for the signal pipeline.

Two long-standing data defects lived in the four copies of `_load_price_data`
(main.py, decision/engine.py, pipeline/analysis_pipeline.py, paper/runner.py):

1. **Duplicate calendar dates.** `price_history` stores two rows per trading day
   (a midnight and a ~01:00 UTC ingest). True Range for a bar is
   `max(H-L, |H-C_prev|, |L-C_prev|)`. When the previous row is a copy of the
   same day, `C_prev` is that day's own close, so the two gap terms collapse
   inside the bar's own range and TR degenerates to just `H-L` — the overnight
   gap component disappears. Half the series therefore contributes range-only
   TR, and Wilder's ATR deflates toward the mean intraday range: measured at
   0.57-0.70x of true across WFC/MSFT/V/NVDA/BLK/GOOGL/COST on 2026-09-05.
   Every ATR-derived level inherits the error: SuperTrend bands, the 2.5x ATR
   stop in signals/aggregator.py, and the R/R gate that divides by stop distance.
   The chart endpoints in monitor/dashboard.py were deduped in May 2026; the
   signal pipeline was not.

2. **Per-field filtering desynchronised the arrays.** The old loaders built
   `closes`/`highs`/`lows`/`volumes` with four independent comprehensions, each
   with its own truthiness filter. A single bar missing one field (or a zero
   volume on a holiday) shifted that array relative to the others, so indicators
   silently combined `highs[i]` with a different day's `closes[i]`.

Both are fixed here, once, for all callers.
"""

from datetime import date, datetime
from typing import Any, Dict, List


def _bar_date(row) -> Any:
    """
    Normalise a PriceHistory.date to a `datetime.date` for dedup keying.

    Always returns the same type so keys sort cleanly and so a string date and a
    date object for the SAME day collapse to one bar instead of two. (datetime is
    a subclass of date, so it must be tested first.)
    """
    d = getattr(row, "date", None)
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    if isinstance(d, str):
        try:
            return datetime.strptime(d[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def dedupe_bars(records: List[Any]) -> List[Any]:
    """
    Collapse `price_history` rows to one bar per calendar date.

    Keeps the LAST row for each date (the later ingest carries the more complete
    session data) and drops any bar missing a high, low, or close so the arrays
    built from the result stay index-aligned. Input need not be sorted; output is
    ascending by date.
    """
    by_date: Dict[Any, Any] = {}
    for r in records:
        key = _bar_date(r)
        if key is None:
            continue
        by_date[key] = r          # later row for the same day wins

    out = []
    for key in sorted(by_date):
        r = by_date[key]
        close = r.adjusted_close or r.close
        if close is None or r.high is None or r.low is None:
            continue
        out.append(r)
    return out


def ohlcv_arrays(records: List[Any], lookback: int | None = None) -> Dict[str, Any]:
    """
    Build index-aligned OHLCV arrays from raw `price_history` rows.

    Returns `{closes, highs, lows, volumes, dates, bars}`. `bars` is the deduped
    row list so callers can still reach fields like `market_cap` off `bars[-1]`.
    `lookback` trims to the most recent N bars AFTER dedup — trimming before it
    would leave roughly half as many distinct trading days as intended.
    """
    bars = dedupe_bars(records)
    if lookback:
        bars = bars[-lookback:]

    closes, highs, lows, volumes, dates = [], [], [], [], []
    for r in bars:
        closes.append(r.adjusted_close or r.close)
        highs.append(r.high)
        lows.append(r.low)
        volumes.append(r.volume or 0)
        dates.append(r.date)

    return {
        "closes":  closes,
        "highs":   highs,
        "lows":    lows,
        "volumes": volumes,
        "dates":   dates,
        "bars":    bars,
    }
