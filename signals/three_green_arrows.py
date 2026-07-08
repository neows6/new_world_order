"""
signals/three_green_arrows.py — ThinkorSwim "3 Green Arrows" emulation.

Reverse-engineered from workspace.DJ_Analysis.xml (studySetName="3greenarrows").

Study 1 (main chart): SMA(30) cross — price crosses above 30-period SMA
Study 2 (Subgraph1):  MACD(8,17,9,EMA) histogram — crosses above zero
Study 3 (Subgraph2):  Stochastic Full(14,5,SMA) — FullD crosses above 25 (exits oversold)
                       OR "phantom" reversal (turning up in 25–75 mid-range)
Study 4 (Subgraph3):  Volume spike — volume ≥ 1.2× 30-bar average (confirmation)

All 3 primary arrows green simultaneously = high-confidence buy signal.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class ThreeGreenArrowsResult:
    ticker: str
    sma_arrow: bool       # close just crossed above SMA(30)
    macd_arrow: bool      # MACD histogram just crossed above 0
    stoch_arrow: bool     # Stoch FullD crossed above 25, or phantom reversal
    volume_spike: bool    # volume >= 1.2× 30-bar avg (bonus confirmation)
    arrows_count: int     # 0-3 primary arrows active
    signal: str           # "buy" | "watch" | "neutral"
    sma_value: float
    macd_hist: float
    stoch_fulld: float
    volume_ratio: float
    reason: str


def _ema_series(values: list, period: int) -> list:
    if not values:
        return []
    k = 2.0 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def _stoch_fulld_at(closes, highs, lows, k_period=14, d_period=5) -> Optional[float]:
    """Compute Stochastic Full %D (slowing_period=1) at the last bar."""
    n = min(len(closes), len(highs), len(lows))
    if n < k_period + d_period:
        return None

    c = closes[-n:]
    h = highs[-n:]
    lo = lows[-n:]

    fast_k = []
    for i in range(k_period - 1, n):
        lo_k = min(lo[i - k_period + 1 : i + 1])
        hi_k = max(h[i - k_period + 1 : i + 1])
        denom = hi_k - lo_k
        fast_k.append((c[i] - lo_k) / denom * 100.0 if denom else 50.0)

    # slowing_period=1 → full_k = fast_k (no extra smoothing)
    if len(fast_k) < d_period:
        return None

    # %D = SMA(full_k, d_period) — compute only the last value
    return sum(fast_k[-d_period:]) / d_period


class ThreeGreenArrowsAnalyzer:
    """
    Emulates the ThinkorSwim DJ_Analysis '3greenarrows' study set.
    Pass the same closes/highs/lows/volumes that the rest of the pipeline uses.
    """

    MIN_BARS = 35  # enough for SMA(30) + 1 prev bar + stochastic warmup

    def analyze(
        self,
        ticker: str,
        closes: list,
        highs: list,
        lows: list,
        volumes: list,
    ) -> Optional[ThreeGreenArrowsResult]:
        if (
            not closes or not highs or not lows
            or len(closes) < self.MIN_BARS
            or len(highs) < self.MIN_BARS
            or len(lows) < self.MIN_BARS
        ):
            return None

        try:
            price = closes[-1]

            # ── Study 1: SMA(30) cross ─────────────────────────────────────
            sma_now  = sum(closes[-30:]) / 30
            sma_prev = sum(closes[-31:-1]) / 30 if len(closes) >= 31 else sma_now
            prev_close = closes[-2]

            sma_arrow = (prev_close <= sma_prev) and (price > sma_now)
            sma_above = price > sma_now

            # ── Study 2: MACD(8,17,9,EMA) histogram cross ─────────────────
            ema8  = _ema_series(closes, 8)
            ema17 = _ema_series(closes, 17)
            n_macd = min(len(ema8), len(ema17))
            macd_line = [ema8[i] - ema17[i] for i in range(n_macd)]
            sig_line  = _ema_series(macd_line, 9)
            hist_arr  = [macd_line[i] - sig_line[i] for i in range(len(sig_line))]

            macd_hist      = hist_arr[-1] if hist_arr else 0.0
            prev_macd_hist = hist_arr[-2] if len(hist_arr) >= 2 else 0.0

            macd_arrow    = (prev_macd_hist <= 0) and (macd_hist > 0)
            macd_positive = macd_hist > 0

            # ── Study 3: Stochastic Full(14,5,SMA) ────────────────────────
            fulld_now  = _stoch_fulld_at(closes, highs, lows, 14, 5)
            fulld_prev = _stoch_fulld_at(closes[:-1], highs[:-1], lows[:-1], 14, 5)
            fulld_prev2 = (
                _stoch_fulld_at(closes[:-2], highs[:-2], lows[:-2], 14, 5)
                if len(closes) >= self.MIN_BARS + 2
                else None
            )

            stoch_val = fulld_now if fulld_now is not None else 50.0

            # Main arrow: FullD crosses above 25
            stoch_cross = (
                fulld_prev is not None and fulld_prev <= 25.0
                and fulld_now is not None and fulld_now > 25.0
            )

            # Phantom: FullD in 25–75, was falling, now turning up
            phantom = bool(
                fulld_now  is not None and 25.0 < fulld_now  < 75.0
                and fulld_prev  is not None and fulld_prev2 is not None
                and fulld_prev < fulld_prev2      # was declining
                and fulld_prev2 > 25.0            # above oversold at the turn
                and fulld_now > fulld_prev         # now rising
            )

            stoch_arrow = stoch_cross or phantom

            # ── Study 4: Volume spike (confirmation) ───────────────────────
            vol_spike = False
            vol_ratio = 1.0
            if volumes and len(volumes) >= 31:
                avg_vol_30 = sum(volumes[-31:-1]) / 30
                if avg_vol_30 > 0:
                    vol_ratio = round(volumes[-1] / avg_vol_30, 2)
                    vol_spike = volumes[-1] >= 1.2 * avg_vol_30

            # ── Composite ──────────────────────────────────────────────────
            arrows = int(sma_arrow) + int(macd_arrow) + int(stoch_arrow)

            if arrows == 3:
                sig = "buy"
            elif arrows == 2:
                sig = "watch"
            else:
                sig = "neutral"

            parts = []
            if sma_arrow:
                parts.append(f"SMA(30)^ ({price:.2f}>{sma_now:.2f})")
            elif sma_above:
                parts.append("above SMA(30)")
            if macd_arrow:
                parts.append(f"MACD hist^0 ({macd_hist:+.3f})")
            elif macd_positive:
                parts.append(f"MACD+ ({macd_hist:+.3f})")
            if stoch_arrow:
                tag = "^25 exit" if stoch_cross else "phantom^"
                parts.append(f"Stoch {tag} ({stoch_val:.1f})")
            if vol_spike:
                parts.append(f"vol spike ({vol_ratio:.1f}×)")

            return ThreeGreenArrowsResult(
                ticker=ticker,
                sma_arrow=sma_arrow,
                macd_arrow=macd_arrow,
                stoch_arrow=stoch_arrow,
                volume_spike=vol_spike,
                arrows_count=arrows,
                signal=sig,
                sma_value=round(sma_now, 2),
                macd_hist=round(macd_hist, 4),
                stoch_fulld=round(stoch_val, 1),
                volume_ratio=vol_ratio,
                reason=", ".join(parts) if parts else "no arrows",
            )

        except Exception:
            return None

    def analyze_series(
        self,
        dates: list,
        closes: list,
        highs: list,
        lows: list,
        volumes: list,
    ) -> dict:
        """
        Compute per-bar indicator values for chart overlay.
        Returns TradingView-compatible [{time, value, ...}] lists.
        """
        n = min(len(dates), len(closes), len(highs), len(lows), len(volumes))
        empty = {"sma30": [], "macd_hist": [], "stoch_fulld": [],
                 "volume_spike_markers": [], "sma_cross_markers": []}
        if n < self.MIN_BARS:
            return empty

        dates   = list(dates[:n])
        closes  = list(closes[:n])
        highs   = list(highs[:n])
        lows    = list(lows[:n])
        volumes = list(volumes[:n])

        # ── MACD(8,17,9,EMA) full series ──────────────────────────
        ema8_s  = _ema_series(closes, 8)
        ema17_s = _ema_series(closes, 17)
        macd_line = [ema8_s[i] - ema17_s[i] for i in range(n)]
        sig_line  = _ema_series(macd_line, 9)
        hist_line = [macd_line[i] - sig_line[i] for i in range(len(sig_line))]
        while len(hist_line) < n:
            hist_line.append(0.0)

        # ── SMA(30) rolling ────────────────────────────────────────
        sma30 = [None] * n
        for i in range(29, n):
            sma30[i] = sum(closes[i - 29: i + 1]) / 30

        # ── Stochastic Full(14,5,SMA) rolling ─────────────────────
        fast_k = [None] * n
        for i in range(13, n):
            lo_k   = min(lows[i - 13: i + 1])
            hi_k   = max(highs[i - 13: i + 1])
            denom  = hi_k - lo_k
            fast_k[i] = (closes[i] - lo_k) / denom * 100 if denom else 50.0

        stoch_d = [None] * n
        for i in range(17, n):   # 13 + 4 = first valid index for 5-period SMA
            window = [fast_k[j] for j in range(i - 4, i + 1) if fast_k[j] is not None]
            if len(window) == 5:
                stoch_d[i] = sum(window) / 5.0

        # ── Volume 30-bar average ──────────────────────────────────
        vol_avg = [None] * n
        for i in range(30, n):
            vol_avg[i] = sum(volumes[i - 30: i]) / 30

        # ── Assemble output ────────────────────────────────────────
        sma30_out, macd_out, stoch_out, spike_out, cross_out = [], [], [], [], []

        for i in range(n):
            t = dates[i]

            if sma30[i] is not None:
                sma30_out.append({"time": t, "value": round(sma30[i], 4)})
                # SMA cross marker
                if i > 0 and sma30[i - 1] is not None:
                    was_above = closes[i - 1] > sma30[i - 1]
                    now_above = closes[i]     > sma30[i]
                    if not was_above and now_above:
                        cross_out.append({"time": t, "dir": "above"})
                    elif was_above and not now_above:
                        cross_out.append({"time": t, "dir": "below"})

            # MACD histogram — TOS color scheme
            h      = hist_line[i]
            prev_h = hist_line[i - 1] if i > 0 else h
            if h >= 0:
                color = "#3fb950" if h >= prev_h else "#1a4731"
            else:
                color = "#f85149" if h <= prev_h else "#7d2f2d"
            macd_out.append({"time": t, "value": round(h, 6), "color": color})

            if stoch_d[i] is not None:
                stoch_out.append({"time": t, "value": round(stoch_d[i], 2)})

            if vol_avg[i] is not None and vol_avg[i] > 0 and volumes[i] >= 1.2 * vol_avg[i]:
                spike_out.append({"time": t, "ratio": round(volumes[i] / vol_avg[i], 2)})

        return {
            "sma30":               sma30_out,
            "macd_hist":           macd_out,
            "stoch_fulld":         stoch_out,
            "volume_spike_markers": spike_out,
            "sma_cross_markers":   cross_out,
        }
