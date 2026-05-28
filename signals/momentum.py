"""
signals/momentum.py — Momentum & Trend Signal Analyzer.

Captures short-to-medium term price momentum that fundamental analysis misses.
Indicators:
  - RSI(14): momentum strength / overbought-oversold
  - MACD(12,26,9): trend direction and crossovers
  - ROC(5d / 20d): rate of change
  - EMA(20) / EMA(50): trend structure
  - Volume ratio: surge confirmation
"""

from dataclasses import dataclass
from typing import Optional
from loguru import logger


@dataclass
class MomentumResult:
    ticker: str
    rsi_14: float           # 0–100
    macd_hist: float        # MACD histogram (positive = bullish)
    macd_direction: str     # "rising" | "falling" | "flat"
    roc_5d: float           # 5-day % change
    roc_20d: float          # 20-day % change
    volume_ratio: float     # latest volume / 20d avg volume
    price_above_ema20: bool
    price_above_ema50: bool
    ema_aligned: bool       # ema20 > ema50
    momentum_score: float   # –1.0 to +1.0
    signal: str             # "strong_buy" | "buy" | "hold" | "sell" | "strong_sell"
    reason: str             # human-readable summary
    is_52w_high_breakout: bool = False  # price within 2% of 52-week high

    # Compatibility aliases used by pipeline / diagnose
    @property
    def rvol(self) -> float:
        return self.volume_ratio

    @property
    def composite_momentum_score(self) -> float:
        return self.momentum_score

    @property
    def ma_stack(self) -> str:
        if self.ema_aligned and self.price_above_ema20:
            return "bullish"
        if not self.ema_aligned and not self.price_above_ema20:
            return "bearish"
        return "mixed"


def _ema(closes: list, period: int) -> float:
    """Exponential moving average of the last `period` values (or all if shorter)."""
    if not closes:
        return 0.0
    k = 2.0 / (period + 1)
    val = closes[0]
    for price in closes[1:]:
        val = price * k + val * (1 - k)
    return val


def _ema_series(closes: list, period: int) -> list:
    """Full EMA series for MACD computation."""
    if not closes:
        return []
    k = 2.0 / (period + 1)
    result = [closes[0]]
    for price in closes[1:]:
        result.append(price * k + result[-1] * (1 - k))
    return result


def _rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0  # Not enough data — neutral
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    # Initial averages
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    # Wilder smoothing
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def _detect_divergence(closes: list, macd_hist: list) -> float:
    """
    Bearish divergence: price makes higher high while MACD histogram makes lower high.
    Bullish divergence: price makes lower low while MACD makes higher low.
    Both are well-documented reversal warnings in technical analysis.
    Returns a score adjustment: negative for bearish, positive for bullish, 0 for none.
    """
    if len(closes) < 20 or len(macd_hist) < 20:
        return 0.0
    recent_price_high = max(closes[-10:])
    prior_price_high  = max(closes[-20:-10])
    recent_macd_high  = max(macd_hist[-10:])
    prior_macd_high   = max(macd_hist[-20:-10])
    if recent_price_high > prior_price_high * 1.005 and recent_macd_high < prior_macd_high * 0.995:
        return -0.25  # bearish divergence
    recent_price_low = min(closes[-10:])
    prior_price_low  = min(closes[-20:-10])
    recent_macd_low  = min(macd_hist[-10:])
    prior_macd_low   = min(macd_hist[-20:-10])
    if recent_price_low < prior_price_low * 0.995 and recent_macd_low > prior_macd_low * 1.005:
        return +0.20  # bullish divergence
    return 0.0


class MomentumAnalyzer:
    """
    Computes momentum signals from price + volume history.
    Designed to complement the fundamentals-heavy existing pipeline
    by capturing trend moves that ROIC/VWAP/Fibonacci miss.
    """

    MIN_BARS = 30  # Need at least 30 bars for reliable signals

    def analyze(
        self,
        ticker: str,
        closes: list,
        highs: Optional[list] = None,
        lows: Optional[list] = None,
        volumes: Optional[list] = None,
    ) -> Optional[MomentumResult]:
        """
        Returns MomentumResult or None if insufficient data.
        closes: list of closing prices, oldest first.
        highs/lows: daily OHLC high/low arrays (optional, not used currently).
        volumes: list of daily volumes, same length as closes (optional).
        """
        if not closes or len(closes) < self.MIN_BARS:
            return None

        try:
            price = closes[-1]

            # ── RSI ────────────────────────────────────────────────
            rsi = _rsi(closes, 14)

            # ── MACD ───────────────────────────────────────────────
            ema12_series = _ema_series(closes, 12)
            ema26_series = _ema_series(closes, 26)
            min_len = min(len(ema12_series), len(ema26_series))
            macd_line = [ema12_series[i] - ema26_series[i] for i in range(min_len)]
            signal_line = _ema_series(macd_line, 9)
            hist_series = [macd_line[i] - signal_line[i] for i in range(len(signal_line))]

            macd_hist = hist_series[-1] if hist_series else 0.0
            prev_hist = hist_series[-2] if len(hist_series) >= 2 else macd_hist
            if macd_hist > prev_hist + abs(prev_hist) * 0.05:
                macd_dir = "rising"
            elif macd_hist < prev_hist - abs(prev_hist) * 0.05:
                macd_dir = "falling"
            else:
                macd_dir = "flat"

            # ── ROC ────────────────────────────────────────────────
            roc_5d  = ((closes[-1] / closes[-6])  - 1) * 100 if len(closes) >= 6  else 0.0
            roc_20d = ((closes[-1] / closes[-21]) - 1) * 100 if len(closes) >= 21 else 0.0

            # ── EMA structure ──────────────────────────────────────
            ema20 = _ema(closes[-40:], 20)   # Feed enough history
            ema50 = _ema(closes[-80:], 50)
            price_above_ema20 = price > ema20
            price_above_ema50 = price > ema50
            ema_aligned = ema20 > ema50      # Uptrend structure

            # ── 52-week high breakout ──────────────────────────────
            lookback = closes[-252:] if len(closes) >= 252 else closes
            w52_high = max(lookback)
            is_52w_high_breakout = w52_high > 0 and (price >= w52_high * 0.98)

            # ── Volume surge ───────────────────────────────────────
            volume_ratio = 1.0
            if volumes and len(volumes) >= 21:
                avg_vol = sum(volumes[-21:-1]) / 20  # 20-day avg excluding today
                if avg_vol > 0:
                    volume_ratio = round(volumes[-1] / avg_vol, 2)

            # ── Composite score ────────────────────────────────────
            score = 0.0

            # RSI component (30% weight)
            if rsi < 30:         rsi_c =  0.50   # Deeply oversold → strong reversal signal
            elif rsi < 40:       rsi_c =  0.25   # Oversold / recovering
            elif rsi < 50:       rsi_c =  0.05   # Mild positive bias
            elif rsi < 60:       rsi_c =  0.15   # Healthy momentum range
            elif rsi < 70:       rsi_c =  0.05   # Approaching overbought
            else:                rsi_c = -0.35   # Overbought → avoid

            # MACD component (30% weight)
            if macd_hist > 0 and macd_dir == "rising":
                macd_c = 0.50    # Bullish and accelerating
            elif macd_hist > 0:
                macd_c = 0.20    # Bullish but slowing
            elif macd_hist < 0 and macd_dir == "falling":
                macd_c = -0.50   # Bearish and accelerating
            else:
                macd_c = -0.15   # Slightly bearish

            # ROC (5-day) component — clamped (20% weight)
            roc_c = max(-0.50, min(0.50, roc_5d / 12.0))

            # EMA structure (15% weight)
            ema_c = 0.0
            if price_above_ema20: ema_c += 0.35
            if price_above_ema50: ema_c += 0.35
            if ema_aligned:       ema_c += 0.30
            ema_c = ema_c - 0.35  # center: fully above = +0.65, fully below = -0.35

            # Volume confirmation (5% weight — only boosts on moves)
            vol_c = 0.0
            if volume_ratio > 2.0 and roc_5d > 0:   vol_c =  0.60
            elif volume_ratio > 1.5 and roc_5d > 0:  vol_c =  0.30
            elif volume_ratio > 2.0 and roc_5d < 0:  vol_c = -0.40  # High vol down day

            score = (
                0.30 * rsi_c
              + 0.30 * macd_c
              + 0.20 * roc_c
              + 0.15 * ema_c
              + 0.05 * vol_c
            )

            # MACD/price divergence adjustment
            div_adj = _detect_divergence(closes, hist_series)
            score = round(max(-1.0, min(1.0, score + div_adj)), 4)

            # ── Signal classification ──────────────────────────────
            if score >= 0.40:      sig = "strong_buy"
            elif score >= 0.20:    sig = "buy"
            elif score >= -0.15:   sig = "hold"
            elif score >= -0.35:   sig = "sell"
            else:                  sig = "strong_sell"

            # ── Reason string ──────────────────────────────────────
            parts = []
            parts.append(f"RSI={rsi:.0f}")
            parts.append(f"MACD={macd_hist:+.3f}({macd_dir})")
            parts.append(f"ROC5d={roc_5d:+.1f}%")
            if price_above_ema20 and price_above_ema50:
                parts.append("above EMA20+50")
            elif price_above_ema20:
                parts.append("above EMA20")
            else:
                parts.append("below EMA20")
            if volume_ratio > 1.5:
                parts.append(f"vol\xd7{volume_ratio:.1f}")
            if div_adj < 0:
                parts.append("bearish MACD divergence")
            elif div_adj > 0:
                parts.append("bullish MACD divergence")
            reason = " | ".join(parts)

            logger.debug(f"[MOM] {ticker}: score={score:+.3f} ({sig}) — {reason}")

            return MomentumResult(
                ticker=ticker,
                rsi_14=rsi,
                macd_hist=round(macd_hist, 4),
                macd_direction=macd_dir,
                roc_5d=round(roc_5d, 2),
                roc_20d=round(roc_20d, 2),
                volume_ratio=volume_ratio,
                price_above_ema20=price_above_ema20,
                price_above_ema50=price_above_ema50,
                ema_aligned=ema_aligned,
                momentum_score=score,
                signal=sig,
                reason=reason,
                is_52w_high_breakout=is_52w_high_breakout,
            )

        except Exception as e:
            logger.warning(f"[MOM] {ticker} momentum analysis failed: {e}")
            return None
