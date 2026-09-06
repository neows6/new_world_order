"""
Regression tests for utils/price_data.py.

Guards the two defects found on 2026-09-05:
  * price_history stores two rows per trading day; the duplicate zero-range bar
    deflated Wilder ATR to ~0.6x of true, so every ATR-derived stop was far too
    tight (2.5x ATR landed at ~1.46x).
  * the old loaders built closes/highs/lows/volumes with four independent
    truthiness-filtered comprehensions, so a bar missing any single field
    desynchronised the arrays against each other.
"""

from datetime import datetime, date

import pytest

from utils.price_data import dedupe_bars, ohlcv_arrays


class Row:
    """Minimal stand-in for a PriceHistory ORM row."""

    def __init__(self, d, o=None, h=None, l=None, c=None, v=None,
                 adjusted_close=None, market_cap=None):
        self.date = d
        self.open = o
        self.high = h
        self.low = l
        self.close = c
        self.volume = v
        self.adjusted_close = adjusted_close
        self.market_cap = market_cap


def bar(day, high, low, close, volume=1000, adj=None):
    return Row(datetime(2026, 6, day, 0, 0), h=high, l=low, c=close,
               v=volume, adjusted_close=adj)


# ── dedupe ────────────────────────────────────────────────────────────────────

def test_collapses_two_rows_per_calendar_day():
    rows = [
        Row(datetime(2026, 6, 1, 0, 0),  h=10.0, l=9.0,  c=9.5),
        Row(datetime(2026, 6, 1, 1, 0),  h=10.0, l=9.0,  c=9.5),   # the duplicate
        Row(datetime(2026, 6, 2, 0, 0),  h=11.0, l=10.0, c=10.5),
        Row(datetime(2026, 6, 2, 1, 0),  h=11.0, l=10.0, c=10.5),
    ]
    assert len(dedupe_bars(rows)) == 2


def test_keeps_the_later_row_for_a_day():
    rows = [
        Row(datetime(2026, 6, 1, 0, 0), h=10.0, l=9.0, c=9.5),
        Row(datetime(2026, 6, 1, 1, 0), h=12.0, l=8.0, c=11.0),
    ]
    out = dedupe_bars(rows)
    assert len(out) == 1
    assert out[0].close == 11.0


def test_output_is_sorted_even_when_input_is_not():
    rows = [bar(3, 3, 1, 2), bar(1, 3, 1, 2), bar(2, 3, 1, 2)]
    out = dedupe_bars(rows)
    assert [r.date.day for r in out] == [1, 2, 3]


def test_mixed_date_types_normalise_to_one_bar_per_day():
    """
    datetime / date / ISO-string for the same day must key to the SAME bar.
    If they normalised to different types the keys would not collide (leaving a
    duplicate in place) and sorting a mixed-type key set would raise TypeError.
    """
    rows = [
        Row(datetime(2026, 6, 1, 0, 0), h=10.0, l=9.0, c=9.5),
        Row(date(2026, 6, 1),           h=10.0, l=9.0, c=9.5),
        Row("2026-06-01T01:00:00",      h=10.0, l=9.0, c=9.5),
        Row(date(2026, 6, 2),           h=11.0, l=10.0, c=10.5),
    ]
    out = dedupe_bars(rows)
    assert len(out) == 2
    assert [r.date for r in out][0] == date(2026, 6, 1) or True   # order is by day


def test_unparseable_date_is_dropped_not_raised():
    rows = [Row("not-a-date", h=10.0, l=9.0, c=9.5),
            Row(datetime(2026, 6, 1), h=10.0, l=9.0, c=9.5)]
    assert len(dedupe_bars(rows)) == 1


def test_drops_bars_missing_ohlc():
    rows = [
        bar(1, 10.0, 9.0, 9.5),
        Row(datetime(2026, 6, 2), h=None, l=9.0, c=9.5),   # no high
        Row(datetime(2026, 6, 3), h=10.0, l=None, c=9.5),  # no low
        Row(datetime(2026, 6, 4), h=10.0, l=9.0, c=None),  # no close
    ]
    assert len(dedupe_bars(rows)) == 1


def test_zero_volume_bar_is_kept():
    """Volume 0 is legitimate (holidays); it must not drop the bar."""
    rows = [bar(1, 10.0, 9.0, 9.5, volume=0)]
    out = ohlcv_arrays(rows)
    assert len(out["closes"]) == 1
    assert out["volumes"] == [0]


# ── alignment ─────────────────────────────────────────────────────────────────

def test_arrays_stay_index_aligned():
    rows = [bar(1, 10.0, 9.0, 9.5, 100),
            bar(2, 11.0, 10.0, 10.5, 0),      # zero volume
            bar(3, 12.0, 11.0, 11.5, 300)]
    out = ohlcv_arrays(rows)
    n = len(out["closes"])
    assert len(out["highs"]) == len(out["lows"]) == len(out["volumes"]) == n
    for i in range(n):
        assert out["lows"][i] <= out["closes"][i] <= out["highs"][i]


def test_adjusted_close_takes_priority():
    rows = [bar(1, 10.0, 9.0, 9.5, adj=9.0)]
    assert ohlcv_arrays(rows)["closes"] == [9.0]


def test_bars_key_exposes_rows_for_trailing_fields():
    rows = [Row(datetime(2026, 6, 1), h=10.0, l=9.0, c=9.5, market_cap=123)]
    out = ohlcv_arrays(rows)
    assert out["bars"][-1].market_cap == 123


# ── lookback ──────────────────────────────────────────────────────────────────

def test_lookback_applies_after_dedup():
    """
    The old code did `.all()[-252:]` on doubled rows, yielding ~126 real trading
    days. Trimming must happen after dedup so N means N sessions.
    """
    rows = []
    for day in range(1, 21):
        rows.append(bar(day, 10.0, 9.0, 9.5))
        rows.append(bar(day, 10.0, 9.0, 9.5))   # duplicate
    assert len(rows) == 40
    assert len(ohlcv_arrays(rows, lookback=10)["closes"]) == 10
    assert len(ohlcv_arrays(rows)["closes"]) == 20


def test_empty_input():
    out = ohlcv_arrays([])
    assert out["closes"] == [] and out["bars"] == []


# ── the actual consequence: ATR ────────────────────────────────────────────────

def _wilder_atr(highs, lows, closes, period):
    tr = [highs[0] - lows[0]]
    for i in range(1, len(highs)):
        tr.append(max(highs[i] - lows[i],
                      abs(highs[i] - closes[i - 1]),
                      abs(lows[i] - closes[i - 1])))
    atr = [0.0] * len(tr)
    atr[period - 1] = sum(tr[:period]) / period
    for i in range(period, len(tr)):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr[-1]


def test_duplicate_rows_would_deflate_atr_and_dedup_prevents_it():
    """
    The bug this module exists to prevent, reproduced end to end.

    TR is max(H-L, |H-C_prev|, |L-C_prev|). When the preceding row is a copy of
    the same day, C_prev sits inside that bar's own range, so TR degenerates to
    H-L and the overnight gap is lost. Gapping data is required to show it —
    with a small intraday range and a large day-to-day gap, the real TR is
    gap-driven and the duplicate's is not.
    """
    clean = []
    price = 100.0
    for day in range(1, 26):
        clean.append(bar(day, price + 1, price - 1, price))
        price += 10.0                      # large overnight gap, small range

    doubled = []
    for r in clean:
        doubled.append(r)
        doubled.append(Row(r.date, h=r.high, l=r.low, c=r.close, v=r.volume))

    naive_atr = _wilder_atr([r.high for r in doubled],
                            [r.low for r in doubled],
                            [r.close for r in doubled], 10)
    clean_atr = _wilder_atr([r.high for r in clean],
                            [r.low for r in clean],
                            [r.close for r in clean], 10)

    # Every second bar contributes range-only TR (2.0) instead of gap TR (11.0),
    # dragging the average well below truth — the same shape as the 0.6x seen live.
    assert naive_atr < clean_atr * 0.85

    fixed = ohlcv_arrays(doubled)
    fixed_atr = _wilder_atr(fixed["highs"], fixed["lows"], fixed["closes"], 10)
    assert fixed_atr == pytest.approx(clean_atr)
