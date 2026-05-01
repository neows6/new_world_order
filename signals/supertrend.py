"""
signals/supertrend.py — SuperTrend + Take-Profit Favourability signal.

Python port of the AlgoAlpha "SuperTrend Take-Profit Dimensions" Pine Script
(daily-candle adaptation — time-of-day axis omitted; vol percentile + range
position axes retained).

ATR and SuperTrend follow TradingView's standard Wilder-smoothed method.
TP favourability measures how closely the current bar's context (volume
percentile, price position in range) matches the historical peak-density
exit context — i.e., where past pivots occurred in confirmed trend runs.
"""

from dataclasses import dataclass, field
from typing import Optional
import math

from loguru import logger


@dataclass
class SuperTrendResult:
    ticker: str
    supertrend_value: float        # Current ST stop line
    direction: str                 # "uptrend" | "downtrend"
    flipped: bool                  # True if direction changed on last bar
    atr: float                     # Latest ATR value
    days_in_trend: int             # Bars since last flip
    tp_favourability: float        # 0.0–1.0 (peak-density context match)
    signal: str                    # "buy" | "sell" | "neutral"
    confidence: float              # 0.0–1.0
    notes: list = field(default_factory=list)


class SuperTrendAnalyzer:
    """
    Computes SuperTrend direction, ATR, and a take-profit favourability score.

    The favourability score answers: "Given the current volume percentile and
    price-range position, how closely does this match conditions at historical
    pivot exits in the same trend direction?"

    Args:
        atr_period:  ATR smoothing period (default 10, same as Pine default)
        factor:      SuperTrend multiplier (default 3.0)
        pivot_n:     Bars each side for swing-high/low pivot detection (default 3)
        bin_count:   Histogram bins for each context axis (default 5)
        max_history: Max pivot samples to keep per pool (default 100)
    """

    def __init__(
        self,
        atr_period: int = 10,
        factor: float = 3.0,
        pivot_n: int = 3,
        bin_count: int = 5,
        max_history: int = 100,
    ):
        self.atr_period  = atr_period
        self.factor      = factor
        self.pivot_n     = pivot_n
        self.bin_count   = bin_count
        self.max_history = max_history

    # ── Public API ──────────────────────────────────────────────────────────

    def analyze(
        self,
        ticker: str,
        highs: list,
        lows: list,
        closes: list,
        volumes: Optional[list] = None,
    ) -> Optional["SuperTrendResult"]:
        """
        Compute SuperTrend + favourability for a price series.
        Requires at least atr_period + pivot_n + 1 bars.
        """
        n = len(closes)
        min_bars = self.atr_period + self.pivot_n + 5
        if n < min_bars or len(highs) < n or len(lows) < n:
            return None

        try:
            highs  = list(highs)
            lows   = list(lows)
            closes = list(closes)
            vols   = list(volumes) if volumes and len(volumes) >= n else [1.0] * n

            atr_series   = self._wilder_atr(highs, lows, closes)
            st_series, dir_series = self._supertrend(highs, lows, closes, atr_series)

            direction   = "uptrend" if dir_series[-1] < 0 else "downtrend"
            prev_dir    = "uptrend" if dir_series[-2] < 0 else "downtrend"
            flipped     = direction != prev_dir
            atr_val     = atr_series[-1]
            st_val      = st_series[-1]
            days_in_trend = self._days_in_trend(dir_series)

            # ── Context axes (current bar) ──────────────────────────────────
            rvol_pct   = self._vol_percentile(vols, idx=n - 1, lookback=50)
            range_pos  = self._range_position(highs, lows, closes, idx=n - 1, lookback=20)

            # ── Build pivot pools ───────────────────────────────────────────
            bull_pool, bear_pool = self._build_pivot_pools(
                highs, lows, closes, vols, dir_series, n
            )

            # ── Favourability score ─────────────────────────────────────────
            pool = bull_pool if direction == "uptrend" else bear_pool
            fav  = self._favourability(rvol_pct, range_pos, pool)

            # ── Signal classification ───────────────────────────────────────
            if flipped:
                signal     = "buy" if direction == "uptrend" else "sell"
                confidence = 0.80
            elif direction == "uptrend":
                signal     = "buy"
                confidence = 0.55 + fav * 0.30
            elif direction == "downtrend":
                signal     = "sell"
                confidence = 0.55 + fav * 0.30
            else:
                signal     = "neutral"
                confidence = 0.0

            notes = []
            if flipped:
                notes.append(f"SuperTrend flipped to {direction} this bar")
            notes.append(
                f"Vol%ile={rvol_pct:.0f} RangePos={range_pos:.0f} "
                f"Fav={fav:.2f} ATR={atr_val:.2f}"
            )

            logger.debug(
                f"[ST] {ticker}: {direction} ST={st_val:.2f} ATR={atr_val:.2f} "
                f"fav={fav:.2f} signal={signal}"
            )

            return SuperTrendResult(
                ticker=ticker,
                supertrend_value=round(st_val, 4),
                direction=direction,
                flipped=flipped,
                atr=round(atr_val, 4),
                days_in_trend=days_in_trend,
                tp_favourability=round(fav, 4),
                signal=signal,
                confidence=round(confidence, 4),
                notes=notes,
            )

        except Exception as e:
            logger.warning(f"[ST] {ticker} SuperTrend failed: {e}")
            return None

    # ── ATR ─────────────────────────────────────────────────────────────────

    def _wilder_atr(self, highs, lows, closes):
        """Wilder's smoothed ATR — matches TradingView ta.atr()."""
        n = len(closes)
        p = self.atr_period
        tr = [0.0] * n
        tr[0] = highs[0] - lows[0]
        for i in range(1, n):
            tr[i] = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i]  - closes[i - 1]),
            )

        atr = [0.0] * n
        # Seed with simple average over first period
        atr[p - 1] = sum(tr[:p]) / p
        for i in range(p, n):
            atr[i] = (atr[i - 1] * (p - 1) + tr[i]) / p
        # Backfill
        for i in range(p - 1):
            atr[i] = atr[p - 1]

        return atr

    # ── SuperTrend ───────────────────────────────────────────────────────────

    def _supertrend(self, highs, lows, closes, atr_series):
        """
        Standard SuperTrend with persistence bands.
        Returns (st_series, direction_series) where direction < 0 = uptrend.
        """
        n = len(closes)
        f = self.factor

        upper = [(highs[i] + lows[i]) / 2 + f * atr_series[i] for i in range(n)]
        lower = [(highs[i] + lows[i]) / 2 - f * atr_series[i] for i in range(n)]

        final_upper = list(upper)
        final_lower = list(lower)

        for i in range(1, n):
            if upper[i] < final_upper[i - 1] or closes[i - 1] > final_upper[i - 1]:
                final_upper[i] = upper[i]
            else:
                final_upper[i] = final_upper[i - 1]

            if lower[i] > final_lower[i - 1] or closes[i - 1] < final_lower[i - 1]:
                final_lower[i] = lower[i]
            else:
                final_lower[i] = final_lower[i - 1]

        st      = [0.0] * n
        dir_ser = [1]  # start as downtrend (positive = down, negative = up)
        st[0]   = final_upper[0]

        for i in range(1, n):
            if st[i - 1] == final_upper[i - 1]:
                # Was in downtrend
                if closes[i] > final_upper[i]:
                    st[i]  = final_lower[i]
                    dir_ser.append(-1)
                else:
                    st[i]  = final_upper[i]
                    dir_ser.append(1)
            else:
                # Was in uptrend
                if closes[i] < final_lower[i]:
                    st[i]  = final_upper[i]
                    dir_ser.append(1)
                else:
                    st[i]  = final_lower[i]
                    dir_ser.append(-1)

        return st, dir_ser

    # ── Pivot detection ──────────────────────────────────────────────────────

    def _find_pivots(self, highs, lows, n):
        """
        Returns (pivot_highs, pivot_lows) as lists of confirmed bar indices.
        A pivot high at index i means high[i] is the highest in [i-n, i+n].
        Only pivots with enough lookforward bars are included (i <= len-n-1).
        """
        pn = self.pivot_n
        ph_idx = []
        pl_idx = []
        for i in range(pn, len(highs) - pn):
            window_h = highs[i - pn: i + pn + 1]
            window_l = lows[i  - pn: i + pn + 1]
            if highs[i] == max(window_h):
                ph_idx.append(i)
            if lows[i] == min(window_l):
                pl_idx.append(i)
        return ph_idx, pl_idx

    # ── Pool building ────────────────────────────────────────────────────────

    def _build_pivot_pools(self, highs, lows, closes, vols, dir_series, n):
        """
        bull_pool: context at pivot HIGHS while direction was uptrend
        bear_pool: context at pivot LOWs  while direction was downtrend
        Each pool is a list of (vol_percentile, range_position) tuples.
        """
        ph_idx, pl_idx = self._find_pivots(highs, lows, n)
        bull_pool = []
        bear_pool = []

        for i in ph_idx:
            if i < 1 or i >= len(dir_series):
                continue
            if dir_series[i] < 0:  # uptrend at pivot
                vp = self._vol_percentile(vols, idx=i, lookback=50)
                rp = self._range_position(highs, lows, closes, idx=i, lookback=20)
                bull_pool.append((vp, rp))

        for i in pl_idx:
            if i < 1 or i >= len(dir_series):
                continue
            if dir_series[i] > 0:  # downtrend at pivot
                vp = self._vol_percentile(vols, idx=i, lookback=50)
                rp = self._range_position(highs, lows, closes, idx=i, lookback=20)
                bear_pool.append((vp, rp))

        # Keep only most recent samples
        bull_pool = bull_pool[-self.max_history:]
        bear_pool = bear_pool[-self.max_history:]

        return bull_pool, bear_pool

    # ── Context axes ─────────────────────────────────────────────────────────

    def _vol_percentile(self, vols, idx, lookback=50):
        """Rank current volume in last `lookback` bars → 0–100."""
        start = max(0, idx - lookback + 1)
        window = vols[start: idx + 1]
        if len(window) < 2:
            return 50.0
        cur = vols[idx]
        rank = sum(1 for v in window if v <= cur)
        return min(100.0, rank / len(window) * 100.0)

    def _range_position(self, highs, lows, closes, idx, lookback=20):
        """Price position within recent high-low range → 0–100 (0=bottom, 100=top)."""
        start = max(0, idx - lookback + 1)
        hh = max(highs[start: idx + 1])
        ll = min(lows[start: idx + 1])
        rng = hh - ll
        if rng < 1e-8:
            return 50.0
        return min(100.0, max(0.0, (closes[idx] - ll) / rng * 100.0))

    # ── Favourability score ──────────────────────────────────────────────────

    def _score_axis(self, pool_vals, current_val):
        """
        Given a list of historical values (0–100) and current value,
        bin into `bin_count` buckets and return current_bin_count / peak_bin_count.
        Returns 0.5 (neutral) when pool is empty.
        """
        if not pool_vals:
            return 0.5

        bc = self.bin_count
        bw = 100.0 / bc
        bins = [0] * bc
        for v in pool_vals:
            b = min(bc - 1, int(v / bw))
            bins[b] += 1

        cur_b = min(bc - 1, int(current_val / bw))
        peak  = max(bins)
        if peak == 0:
            return 0.5
        return bins[cur_b] / peak

    def _favourability(self, rvol_pct, range_pos, pool):
        """
        Average per-axis favourability score (0.0–1.0).
        0.0 = current conditions are at the least-common exit context
        1.0 = current conditions are at the peak-density exit context
        """
        if not pool:
            return 0.5

        vol_vals   = [p[0] for p in pool]
        range_vals = [p[1] for p in pool]

        s_vol   = self._score_axis(vol_vals,   rvol_pct)
        s_range = self._score_axis(range_vals, range_pos)

        return (s_vol + s_range) / 2.0

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _days_in_trend(self, dir_series):
        """Count consecutive bars with same direction as the last bar."""
        if not dir_series:
            return 0
        cur = dir_series[-1]
        count = 0
        for d in reversed(dir_series):
            if d == cur:
                count += 1
            else:
                break
        return count
