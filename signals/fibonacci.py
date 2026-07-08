"""
signals/fibonacci.py — Fibonacci Confluence Scorer.

What makes Fibonacci work (and why):
  Fibonacci levels (23.6%, 38.2%, 50%, 61.8%, 78.6%) work primarily because
  institutional algorithms are programmed to execute at them — creating a
  self-fulfilling prophecy. The Golden Zone (61.8%-65%) is the highest-
  probability reversal zone because it's the deepest "healthy" pullback
  before a trend is considered broken.

  Research (IG Group, 2025): Fib + RSI divergence increases win rates 15-20%.
  Fibonacci alone: ~60-65% success rate in trending markets.
  Fibonacci in ranging markets: unreliable — avoid.

Our approach:
  1. Detect significant swing highs/lows automatically (no manual drawing)
  2. Compute Fibonacci retracement levels from multiple swings
  3. Score confluence zones where multiple Fib levels cluster
  4. Add RSI divergence as timing confirmation
  5. Return a confidence-weighted entry signal

Key levels:
  0.236 — Shallow pullback (weight 1.0)
  0.382 — Healthy correction, first reaction zone (weight 1.5)
  0.500 — Equilibrium, psychological (weight 2.0)
  0.618 — GOLDEN RATIO — most powerful (weight 2.5)
  0.650 — Golden Zone upper bound (weight 2.3)
  0.786 — Deep retracement before reversal (weight 1.8)

Extension levels (profit targets):
  1.272, 1.618 — Most common institutional targets
  2.000, 2.618 — Extended targets in strong trends
"""

import statistics
from dataclasses import dataclass
from typing import Optional
from loguru import logger


# Fibonacci ratios and their institutional weights
FIB_LEVELS = {
    0.236: 1.0,
    0.382: 1.5,
    0.500: 2.0,
    0.618: 2.5,   # Golden ratio — highest weight
    0.650: 2.3,   # Golden zone upper
    0.786: 1.8,
}

FIB_EXTENSIONS = [1.272, 1.618, 2.000, 2.618]

# Golden Zone: 61.8% to 65% retracement — highest probability reversal
GOLDEN_ZONE_LOW  = 0.618
GOLDEN_ZONE_HIGH = 0.650

# Confluence tolerance: levels within this % of each other are "confluent"
CONFLUENCE_TOLERANCE = 0.005   # 0.5% of price


@dataclass
class FibLevel:
    ratio: float
    price: float
    weight: float
    swing_from: str    # "primary", "secondary", "tertiary"


@dataclass
class FibZone:
    price_low: float
    price_high: float
    confluence_score: float   # Sum of weights of levels in this zone
    levels: list              # List of FibLevel objects
    in_golden_zone: bool
    is_support: bool          # True = support (uptrend retracement), False = resistance


@dataclass
class FibResult:
    ticker: str
    current_price: float
    trend_direction: str         # "uptrend", "downtrend", "ranging"

    # Swing points used
    primary_swing_high: float
    primary_swing_low: float

    # Current price position
    current_retracement_ratio: Optional[float]  # Where price is in the primary swing
    in_golden_zone: bool
    nearest_fib_level: Optional[float]
    nearest_fib_ratio: Optional[float]

    # Confluence zones (sorted by score)
    confluence_zones: list       # List of FibZone

    # Entry signal
    entry_signal: str            # "strong_buy", "buy", "neutral", "sell", "strong_sell"
    confluence_score: float      # 0.0 to 5.0 — how many weighted levels align
    stop_loss_level: float       # Just below the confluence zone
    take_profit_1: float         # First extension target
    take_profit_2: float         # Second extension target

    # RSI confirmation
    rsi_value: Optional[float]
    rsi_divergence: bool
    rsi_oversold: bool           # < 30
    rsi_overbought: bool         # > 70

    notes: list
    warnings: list


class FibonacciAnalyzer:
    """
    Automatically detects swing points and computes Fibonacci confluence zones.
    Works on daily price data (OHLCV).

    Best used for:
      - Swing trades (3-10 day holding period)
      - Entries when price pulls back to key Fib levels in trending stocks
      - Setting logical profit targets and stop losses

    NOT suitable for:
      - Ranging / choppy markets (no clear swing to anchor from)
      - Very short timeframes (1-5 min) — noise dominates
    """

    # Swing detection: how many bars on each side must be lower/higher
    SWING_LOOKBACK = 10       # 10-bar pivot detection

    # Minimum swing size to be meaningful (% of price)
    MIN_SWING_PCT = 0.05      # 5% minimum swing to use

    # RSI period
    RSI_PERIOD = 14

    def _compute_rsi(self, closes: list) -> Optional[float]:
        """Compute RSI from closing prices."""
        if len(closes) < self.RSI_PERIOD + 1:
            return None
        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        gains  = [max(d, 0) for d in deltas]
        losses = [abs(min(d, 0)) for d in deltas]

        avg_gain = statistics.mean(gains[-self.RSI_PERIOD:])
        avg_loss = statistics.mean(losses[-self.RSI_PERIOD:])

        if avg_loss == 0:
            return 100.0
        rs  = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi

    def _detect_rsi_divergence(self, closes: list, rsi_series: list) -> bool:
        """
        Bullish divergence: price makes lower low, RSI makes higher low.
        Most reliable when at a Fibonacci support level.
        """
        if len(closes) < 20 or len(rsi_series) < 20:
            return False
        # Compare last 5 bars to prior 5 bars
        price_recent = closes[-5:]
        price_prior  = closes[-10:-5]
        rsi_recent   = rsi_series[-5:]
        rsi_prior    = rsi_series[-10:-5]

        price_lower_low = min(price_recent) < min(price_prior)
        rsi_higher_low  = min(rsi_recent) > min(rsi_prior)

        return price_lower_low and rsi_higher_low  # Bullish divergence

    def _find_swing_points(self, highs: list, lows: list) -> dict:
        """
        Detect significant swing highs and lows using pivot detection.
        Returns primary, secondary, and tertiary swings for multi-swing analysis.
        """
        n = len(highs)
        lb = self.SWING_LOOKBACK

        swing_highs = []
        swing_lows  = []

        for i in range(lb, n - lb):
            # Swing high: bar i is highest in window
            if highs[i] == max(highs[i-lb:i+lb+1]):
                swing_highs.append((i, highs[i]))
            # Swing low: bar i is lowest in window
            if lows[i] == min(lows[i-lb:i+lb+1]):
                swing_lows.append((i, lows[i]))

        return {
            "highs": sorted(swing_highs, key=lambda x: x[1], reverse=True),
            "lows":  sorted(swing_lows, key=lambda x: x[1]),
        }

    def _determine_trend(self, closes: list, swing_highs: list, swing_lows: list) -> str:
        """Simple trend detection using swing structure."""
        if len(closes) < 40:
            return "unknown"

        # Higher highs + higher lows = uptrend
        # Lower highs + lower lows = downtrend
        recent_closes = closes[-20:]
        prior_closes  = closes[-40:-20]

        recent_avg = statistics.mean(recent_closes)
        prior_avg  = statistics.mean(prior_closes)

        diff_pct = (recent_avg - prior_avg) / prior_avg if prior_avg > 0 else 0

        if diff_pct > 0.03:
            return "uptrend"
        elif diff_pct < -0.03:
            return "downtrend"
        else:
            return "ranging"

    def _compute_fib_levels(
        self, swing_high: float, swing_low: float, label: str, is_uptrend: bool
    ) -> list:
        """
        Compute Fibonacci retracement price levels from a swing.
        In uptrend: retracement from high back toward low (support levels).
        In downtrend: retracement from low back toward high (resistance levels).
        """
        swing_range = swing_high - swing_low
        if swing_range <= 0:
            return []

        levels = []
        for ratio, weight in FIB_LEVELS.items():
            if is_uptrend:
                # Price retracing down from high toward low
                price = swing_high - (ratio * swing_range)
            else:
                # Price retracing up from low toward high
                price = swing_low + (ratio * swing_range)

            levels.append(FibLevel(
                ratio=ratio, price=price, weight=weight, swing_from=label
            ))

        return levels

    def _find_confluence_zones(self, all_levels: list, current_price: float) -> list:
        """
        Group nearby Fib levels into confluence zones.
        A zone with 3+ levels (especially from multiple swings) is very powerful.
        """
        if not all_levels:
            return []

        sorted_levels = sorted(all_levels, key=lambda x: x.price)
        zones = []
        i = 0

        while i < len(sorted_levels):
            zone_levels = [sorted_levels[i]]
            base_price = sorted_levels[i].price

            # Group levels within tolerance of each other
            j = i + 1
            while j < len(sorted_levels):
                if abs(sorted_levels[j].price - base_price) / base_price < CONFLUENCE_TOLERANCE * 3:
                    zone_levels.append(sorted_levels[j])
                    j += 1
                else:
                    break

            price_low  = min(l.price for l in zone_levels) * (1 - CONFLUENCE_TOLERANCE)
            price_high = max(l.price for l in zone_levels) * (1 + CONFLUENCE_TOLERANCE)
            score = sum(l.weight for l in zone_levels)

            # Golden zone bonus
            in_golden = False
            if zone_levels:
                ratios = [l.ratio for l in zone_levels]
                in_golden = any(GOLDEN_ZONE_LOW <= r <= GOLDEN_ZONE_HIGH for r in ratios)
                if in_golden:
                    score += 1.5    # Golden zone confluence bonus

            zones.append(FibZone(
                price_low=price_low,
                price_high=price_high,
                confluence_score=score,
                levels=zone_levels,
                in_golden_zone=in_golden,
                is_support=zone_levels[0].price < current_price,
            ))

            i = j

        # Sort by confluence score descending
        return sorted(zones, key=lambda z: z.confluence_score, reverse=True)

    def _compute_entry_signal(
        self,
        current_price: float,
        confluence_zones: list,
        trend: str,
        in_golden_zone: bool,
        rsi: Optional[float],
        rsi_divergence: bool,
    ) -> tuple:
        """
        Generate entry signal based on price position relative to zones.
        Returns (signal, score, stop_loss, tp1, tp2)
        """
        if not confluence_zones:
            return "neutral", 0.0, current_price * 0.97, current_price * 1.05, current_price * 1.10

        # Find nearest support zone below current price
        support_zones = [z for z in confluence_zones if z.price_high < current_price]

        # Is price IN a confluence zone right now?
        at_zone = [z for z in confluence_zones
                   if z.price_low <= current_price <= z.price_high]

        score = 0.0
        signal = "neutral"

        if at_zone and trend == "uptrend":
            best_zone = max(at_zone, key=lambda z: z.confluence_score)
            score = best_zone.confluence_score

            # Base signal on zone strength
            if score >= 4.0 and in_golden_zone:
                signal = "strong_buy"
            elif score >= 3.0:
                signal = "buy"
            elif score >= 2.0:
                signal = "weak_buy"
            else:
                signal = "neutral"

            # RSI confirmation bonus
            if rsi and rsi < 35:
                score += 1.0
                if signal in ("buy", "weak_buy"):
                    signal = "strong_buy" if score >= 4.0 else "buy"
            if rsi_divergence:
                score += 1.0

            # Stop loss below the zone
            stop_loss = best_zone.price_low * 0.99   # 1% below zone

        elif at_zone and trend == "downtrend":
            best_zone = max(at_zone, key=lambda z: z.confluence_score)
            score = best_zone.confluence_score
            if score >= 3.0:
                signal = "sell"
            stop_loss = best_zone.price_high * 1.01
        else:
            # Not at a zone — compute distance to nearest support
            if support_zones:
                nearest_support = max(support_zones, key=lambda z: z.price_high)
                distance_pct = (current_price - nearest_support.price_high) / current_price
                if distance_pct < 0.02:    # Within 2% of support
                    signal = "approaching_support"
                    score = nearest_support.confluence_score * 0.5
            stop_loss = current_price * 0.97

        # Profit targets using Fibonacci extensions
        swing_range = max(z.price_high for z in confluence_zones) - min(z.price_low for z in confluence_zones)
        tp1 = current_price + swing_range * 0.618
        tp2 = current_price + swing_range * 1.272

        return signal, min(score, 5.0), stop_loss, tp1, tp2

    def analyze(
        self,
        ticker: str,
        highs: list,
        lows: list,
        closes: list,
    ) -> FibResult:
        """
        Main entry point.
        Expects aligned lists of daily high, low, close prices (oldest first).
        """
        notes    = []
        warnings = []
        n = len(closes)

        if n < 30:
            warnings.append("Need at least 30 bars for Fibonacci analysis")
            cp = closes[-1] if closes else 0
            return FibResult(
                ticker=ticker, current_price=cp, trend_direction="unknown",
                primary_swing_high=cp, primary_swing_low=cp,
                current_retracement_ratio=None, in_golden_zone=False,
                nearest_fib_level=None, nearest_fib_ratio=None,
                confluence_zones=[], entry_signal="neutral",
                confluence_score=0.0, stop_loss_level=cp * 0.97,
                take_profit_1=cp * 1.05, take_profit_2=cp * 1.10,
                rsi_value=None, rsi_divergence=False,
                rsi_oversold=False, rsi_overbought=False,
                notes=notes, warnings=warnings,
            )

        current_price = closes[-1]

        # ── Swing detection ─────────────────────────────────────
        swings = self._find_swing_points(highs, lows)
        trend  = self._determine_trend(closes, swings["highs"], swings["lows"])

        if trend == "ranging":
            warnings.append("Market is ranging — Fibonacci is less reliable in chop")

        # Primary swing: most recent significant high and low
        if not swings["highs"] or not swings["lows"]:
            warnings.append("Could not detect clear swing points")
            return FibResult(
                ticker=ticker, current_price=current_price,
                trend_direction=trend, primary_swing_high=max(highs),
                primary_swing_low=min(lows), current_retracement_ratio=None,
                in_golden_zone=False, nearest_fib_level=None,
                nearest_fib_ratio=None, confluence_zones=[],
                entry_signal="neutral", confluence_score=0.0,
                stop_loss_level=current_price * 0.97,
                take_profit_1=current_price * 1.05,
                take_profit_2=current_price * 1.10,
                rsi_value=None, rsi_divergence=False,
                rsi_oversold=False, rsi_overbought=False,
                notes=notes, warnings=warnings,
            )

        # Use most recent (by index) significant swings
        primary_high_idx, primary_high = max(swings["highs"][:3], key=lambda x: x[0])
        primary_low_idx,  primary_low  = max(swings["lows"][:3],  key=lambda x: x[0])

        swing_size_pct = (primary_high - primary_low) / primary_low if primary_low > 0 else 0
        if swing_size_pct < self.MIN_SWING_PCT:
            warnings.append(f"Primary swing only {swing_size_pct:.1%} — too small for reliable Fib")

        notes.append(f"Primary swing: ${primary_low:.2f} → ${primary_high:.2f} ({swing_size_pct:.1%})")

        # ── Compute Fib levels from primary and secondary swings ─
        is_uptrend = trend == "uptrend"
        all_levels = self._compute_fib_levels(primary_high, primary_low, "primary", is_uptrend)

        # Secondary swing if available
        if len(swings["highs"]) >= 2 and len(swings["lows"]) >= 2:
            sec_high = swings["highs"][1][1]
            sec_low  = swings["lows"][1][1]
            all_levels += self._compute_fib_levels(sec_high, sec_low, "secondary", is_uptrend)
            notes.append(f"Secondary swing: ${sec_low:.2f} → ${sec_high:.2f} (multi-swing confluence active)")

        # ── Current retracement ratio ────────────────────────────
        swing_range = primary_high - primary_low
        if swing_range > 0 and is_uptrend:
            current_retracement = (primary_high - current_price) / swing_range
        elif swing_range > 0:
            current_retracement = (current_price - primary_low) / swing_range
        else:
            current_retracement = None

        in_golden = (current_retracement is not None and
                     GOLDEN_ZONE_LOW <= current_retracement <= GOLDEN_ZONE_HIGH)

        if in_golden:
            notes.append("⭐ Price is in the Golden Zone (61.8%-65%) — highest probability reversal zone")

        # Nearest Fib level
        nearest_level = None
        nearest_ratio = None
        if all_levels:
            nearest = min(all_levels, key=lambda l: abs(l.price - current_price))
            nearest_level = nearest.price
            nearest_ratio = nearest.ratio
            distance_pct = abs(nearest.price - current_price) / current_price
            notes.append(f"Nearest Fib: {nearest.ratio:.3f} (${nearest.price:.2f}, {distance_pct:.1%} away)")

        # ── Confluence zones ─────────────────────────────────────
        zones = self._find_confluence_zones(all_levels, current_price)

        # ── RSI ──────────────────────────────────────────────────
        rsi = self._compute_rsi(closes)
        rsi_series = [self._compute_rsi(closes[:i]) for i in range(self.RSI_PERIOD + 1, n + 1)]
        rsi_series = [r for r in rsi_series if r is not None]
        rsi_div = self._detect_rsi_divergence(closes, rsi_series)

        if rsi:
            if rsi < 30:
                notes.append(f"RSI oversold ({rsi:.0f}) — timing confirmation for Fib bounce")
            elif rsi > 70:
                notes.append(f"RSI overbought ({rsi:.0f}) — caution, extended")
        if rsi_div:
            notes.append("RSI bullish divergence detected — high-probability reversal signal")

        # ── Entry signal ─────────────────────────────────────────
        signal, score, stop_loss, tp1, tp2 = self._compute_entry_signal(
            current_price, zones, trend, in_golden, rsi, rsi_div
        )

        logger.debug(
            f"[FIB] {ticker}: trend={trend}, signal={signal}, score={score:.1f}, "
            f"golden_zone={in_golden}, rsi={f'{rsi:.0f}' if rsi else 'N/A'}"
        )

        return FibResult(
            ticker=ticker,
            current_price=current_price,
            trend_direction=trend,
            primary_swing_high=primary_high,
            primary_swing_low=primary_low,
            current_retracement_ratio=current_retracement,
            in_golden_zone=in_golden,
            nearest_fib_level=nearest_level,
            nearest_fib_ratio=nearest_ratio,
            confluence_zones=zones[:5],   # Top 5 zones
            entry_signal=signal,
            confluence_score=score,
            stop_loss_level=stop_loss,
            take_profit_1=tp1,
            take_profit_2=tp2,
            rsi_value=rsi,
            rsi_divergence=rsi_div,
            rsi_oversold=bool(rsi and rsi < 30),
            rsi_overbought=bool(rsi and rsi > 70),
            notes=notes,
            warnings=warnings,
        )
