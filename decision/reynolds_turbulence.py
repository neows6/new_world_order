"""
decision/reynolds_turbulence.py — Reynolds Number Market Turbulence Detector.

═══════════════════════════════════════════════════════════════════════
THE PHYSICS:
  In fluid dynamics, the Reynolds Number (Re) determines whether flow is:
    - LAMINAR  (Re < 2300): Smooth, predictable, ordered flow
    - TRANSIENT (2300-4000): Unstable, transitioning
    - TURBULENT (Re > 4000): Chaotic, unpredictable, eddies and vortices

  Re = (ρ × v × L) / μ
    ρ = fluid density
    v = flow velocity
    L = characteristic length scale
    μ = dynamic viscosity (resistance to flow)

THE MARKET ANALOGY (from peer-reviewed econophysics, Zeng & Dong 2024,
and the supply/demand collision framework of Jakimowicz & Juzwiszyn 2014):

  ρ (density)   → Volume / Average Volume  (market participation density)
  v (velocity)  → |Price Return| / ATR     (price momentum magnitude)
  L (length)    → Lookback window in days  (characteristic time scale)
  μ (viscosity) → Bid-Ask Spread / Price   (market friction / resistance)

  HIGH Re (turbulent market):
    High volume + fast moves + low spread = chaotic, momentum-driven
    Trend strategies fail. Mean reversion risky. REDUCE position size.

  LOW Re (laminar market):
    Low volume + slow moves + wide spread = calm, trending
    Fundamental strategies work. Fibonacci entries reliable.
    NORMAL position sizing.

PRACTICAL IMPLICATION:
  We only execute trades in laminar or transitional regimes.
  In turbulent markets, even the best fundamental pick can lose 10%
  in a day from market-wide panic — not because the business changed,
  but because the flow is turbulent.

═══════════════════════════════════════════════════════════════════════
"""

import math
import statistics
from dataclasses import dataclass
from typing import Optional
from loguru import logger


# Reynolds Number regime boundaries (adapted from econophysics literature)
RE_LAMINAR_MAX     = 1.0    # Normalized: calm, trending market
RE_TRANSIENT_MAX   = 2.5    # Transitional: caution
RE_TURBULENT_MAX   = 5.0    # Turbulent: high volatility
# Above RE_TURBULENT_MAX = extreme turbulence / crisis

# Normal-market daily volatility baseline (stdev of |daily returns| over ~2 weeks).
# Used to scale realized volatility into an O(1) turbulence factor so the
# normalized Reynolds index lands ~0.5-1.5 in quiet markets and 5-10+ in crises.
BASELINE_VOLATILITY = 0.012   # ~1.2%/day — typical large-cap

# Normal-market bid-ask spread baseline (as % of price). A wider spread is
# liquidity friction that damps turbulent flow, so it divides the Reynolds index.
BASELINE_SPREAD = 0.001       # ~0.1% — typical large-cap spread


@dataclass
class ReynoldsResult:
    """
    Market Reynolds Number analysis result.
    Analogous to fluid flow characterization before engineering a pipeline.
    """
    # Raw components
    density:     float      # Volume participation (ρ)
    velocity:    float      # Price momentum magnitude (v)
    length:      float      # Lookback window (L) — fixed at 20 days
    viscosity:   float      # Market friction proxy (μ)

    # Computed Reynolds Number
    reynolds_number: float

    # Regime classification
    regime: str             # "laminar", "transient", "turbulent", "extreme"
    regime_confidence: float  # 0.0 to 1.0 — how clearly in a regime

    # Trading implications
    position_multiplier: float   # Scale all positions by this
    allow_entry: bool            # False in extreme turbulence
    flow_direction: str          # "upward", "downward", "oscillating"
    energy_cascade: str          # "building", "dissipating", "stable"
    # (In turbulence theory: energy cascades from large scales to small
    #  In markets: large institutional moves cascade to retail volatility)

    # Kolmogorov microscale analog
    # (Smallest turbulent eddy — in markets: shortest meaningful signal period)
    minimum_signal_period_days: int

    notes: list
    warnings: list


class ReynoldsMarketAnalyzer:
    """
    Computes the financial Reynolds Number to characterize market flow regime.

    Why this matters:
      A quality company at fair value is a great fundamental setup.
      But if the market is in turbulent flow, price action is dominated
      by momentum cascades and panic, not fundamentals.
      We only deploy capital in laminar or early transient flow.
    """

    LOOKBACK_DAYS = 20          # Characteristic length scale L
    MIN_DATA_POINTS = 25        # Minimum required
    VOLUME_MA_PERIOD = 20       # Volume moving average for density
    ATR_PERIOD = 14             # ATR period for velocity normalization

    def _compute_atr(self, highs: list, lows: list, closes: list) -> float:
        """
        Average True Range — measures price velocity scale.
        True Range = max(H-L, |H-Prev_C|, |L-Prev_C|)
        """
        n = min(len(highs), self.ATR_PERIOD + 1)
        true_ranges = []
        for i in range(1, n):
            hl   = highs[i] - lows[i]
            hc   = abs(highs[i] - closes[i-1])
            lc   = abs(lows[i] - closes[i-1])
            true_ranges.append(max(hl, hc, lc))
        return statistics.mean(true_ranges) if true_ranges else closes[-1] * 0.01

    def _compute_density(self, volumes: list) -> float:
        """
        Market density = current volume / average volume.
        High density = heavy participation = dense flow.
        """
        if not volumes or len(volumes) < 5:
            return 1.0
        avg_volume = statistics.mean(volumes[-self.VOLUME_MA_PERIOD:]) if len(volumes) >= self.VOLUME_MA_PERIOD else statistics.mean(volumes)
        current_vol = statistics.mean(volumes[-3:])  # 3-day recent average
        if avg_volume == 0:
            return 1.0
        return current_vol / avg_volume

    def _compute_velocity(self, closes: list, atr: float) -> float:
        """
        Market velocity = recent absolute price movement / ATR.
        Normalized so 1.0 = one ATR of movement per day (typical).
        Higher = faster moving = higher momentum.
        """
        if len(closes) < 5 or atr == 0:
            return 1.0
        recent_return = abs(closes[-1] - closes[-5]) / closes[-5] if closes[-5] > 0 else 0
        atr_normalized = atr / closes[-1] if closes[-1] > 0 else 0.01
        if atr_normalized == 0:
            return 1.0
        return recent_return / (atr_normalized * 5)  # 5-day normalized

    def _compute_viscosity(self, closes: list) -> float:
        """
        Realized volatility = stdev of |daily returns| over the ATR window.

        This is the primary turbulence DRIVER, not a damper: high volatility
        means chaotic, fast flow (high Reynolds), low volatility means calm
        laminar flow. It is scaled by BASELINE_VOLATILITY in analyze() so it
        contributes to the Reynolds index in the numerator.

        NOTE: the field is named `viscosity` for backward compatibility, but it
        holds realized volatility. (Prior versions wrongly placed it in the Re
        denominator, which inverted the regime — calm blue chips read as
        "extreme" while volatile microcaps read "laminar". Fixed 2026-06-02.)
        Bid-ask spread is NOT blended in here — it acts as liquidity friction
        and is applied as a damping divisor in analyze().
        """
        if len(closes) < 10:
            return 0.01

        # Use the most-recent ATR_PERIOD bars. NOTE: slice first, then index the
        # slice — the prior code iterated range(len(closes[-ATR_PERIOD:])) but
        # indexed closes[i], which read the OLDEST bars instead of the recent window.
        window = closes[-self.ATR_PERIOD:]
        returns = [abs(window[i] - window[i-1]) / window[i-1]
                   for i in range(1, len(window))
                   if window[i-1] > 0]

        if not returns:
            return 0.01

        vol = statistics.stdev(returns) if len(returns) > 1 else returns[0]

        # Ensure non-zero (no market is perfectly frictionless)
        return max(0.001, vol)

    def _compute_flow_direction(self, closes: list) -> str:
        """Characterize overall direction of the price flow."""
        if len(closes) < 20:
            return "unknown"
        short_avg = statistics.mean(closes[-5:])
        long_avg  = statistics.mean(closes[-20:])
        delta_pct = (short_avg - long_avg) / long_avg if long_avg > 0 else 0

        if delta_pct > 0.02:
            return "upward"
        elif delta_pct < -0.02:
            return "downward"
        else:
            return "oscillating"

    def _compute_energy_cascade(self, closes: list, volumes: list) -> str:
        """
        Detect whether turbulent energy is building or dissipating.

        In turbulence theory: energy cascades from large scales (institutional)
        to small scales (retail). Building cascade = approaching crisis.
        Dissipating cascade = normalizing.

        Proxy: Is volatility accelerating or decelerating?
        """
        if len(closes) < 20:
            return "stable"

        # Recent volatility vs prior volatility
        recent_vol = statistics.stdev([abs(closes[i]-closes[i-1])/closes[i-1]
                                       for i in range(len(closes)-5, len(closes))
                                       if closes[i-1] > 0]) if len(closes) > 6 else 0

        prior_vol  = statistics.stdev([abs(closes[i]-closes[i-1])/closes[i-1]
                                       for i in range(len(closes)-15, len(closes)-5)
                                       if closes[i-1] > 0]) if len(closes) > 16 else recent_vol

        if prior_vol == 0:
            return "stable"

        ratio = recent_vol / prior_vol
        if ratio > 1.30:
            return "building"    # Volatility accelerating = energy cascade building
        elif ratio < 0.70:
            return "dissipating" # Volatility decelerating = normalizing
        else:
            return "stable"

    def _kolmogorov_minimum_period(self, reynolds_number: float) -> int:
        """
        Kolmogorov microscale analog for markets.

        In turbulence: η = (ν³/ε)^(1/4) — smallest turbulent eddy scale.
        In markets: minimum signal period below which price is pure noise.

        Higher Re → smaller Kolmogorov scale → noise dominates shorter periods.
        Practical: don't trade signals shorter than this period in bars (days).
        """
        if reynolds_number < 1.0:
            return 2    # Laminar: signals reliable at 2-day minimum
        elif reynolds_number < 2.5:
            return 5    # Transitional: need 1-week signals
        elif reynolds_number < 5.0:
            return 10   # Turbulent: need 2-week signals
        else:
            return 20   # Extreme: only monthly signals reliable

    def analyze(
        self,
        ticker: str,
        closes: list,
        highs: list,
        lows: list,
        volumes: list,
        bid_ask_spread_pct: Optional[float] = None,
    ) -> ReynoldsResult:
        """
        Compute the financial Reynolds Number for current market conditions.

        Parameters:
            closes, highs, lows: Daily OHLC price series (oldest first)
            volumes: Daily volume series
            bid_ask_spread_pct: Current spread as % of price (optional, improves viscosity)

        Returns:
            ReynoldsResult with regime classification and position guidance.
        """
        notes    = []
        warnings = []
        n = len(closes)

        if n < self.MIN_DATA_POINTS:
            warnings.append(f"Only {n} data points — Reynolds analysis needs {self.MIN_DATA_POINTS}+")
            return ReynoldsResult(
                density=1.0, velocity=1.0, length=self.LOOKBACK_DAYS, viscosity=0.01,
                reynolds_number=1.0, regime="laminar", regime_confidence=0.3,
                position_multiplier=0.8, allow_entry=True,
                flow_direction="unknown", energy_cascade="stable",
                minimum_signal_period_days=5,
                notes=["Insufficient data for Reynolds analysis"],
                warnings=warnings,
            )

        # ── Compute Reynolds components ────────────────────────────
        atr       = self._compute_atr(highs[-self.ATR_PERIOD-1:], lows[-self.ATR_PERIOD-1:], closes[-self.ATR_PERIOD-1:])
        density   = self._compute_density(volumes)
        velocity  = self._compute_velocity(closes, atr)
        viscosity = self._compute_viscosity(closes)
        length    = float(self.LOOKBACK_DAYS)

        # ── Reynolds Number (market turbulence index) ──────────────
        # Re ∝ density × velocity × volatility
        #
        # FIX (2026-06-02): the prior formula was Re = (ρ·v·L)/μ with μ = realized
        # volatility in the DENOMINATOR. That inverted the economics — the calmest
        # blue chips (lowest vol) scored as the MOST turbulent, so quality setups
        # like CRM/NVDA/MSFT were blocked as "extreme" while a jumpy microcap read
        # "laminar". Volatility drives turbulence, so it belongs in the numerator,
        # expressed relative to a normal-market baseline to keep the index O(1):
        # quiet markets ≈ 0.5-1.5, genuine crises ≈ 5-10+.
        vol_factor = viscosity / BASELINE_VOLATILITY   # `viscosity` holds realized vol
        reynolds_normalized = density * velocity * vol_factor

        # Liquidity friction: a wide bid-ask spread resists flow and damps
        # turbulence (friction ≥ 1.0, so it can only lower the index).
        if bid_ask_spread_pct:
            reynolds_normalized /= (1.0 + bid_ask_spread_pct / BASELINE_SPREAD)

        notes.append(
            f"Re components: density={density:.2f}, velocity={velocity:.2f}, "
            f"vol={viscosity:.4f} (factor {vol_factor:.2f}× baseline)"
        )
        notes.append(f"Reynolds Number (normalized): {reynolds_normalized:.2f}")

        # ── Regime classification ──────────────────────────────────
        if reynolds_normalized < RE_LAMINAR_MAX:
            regime = "laminar"
            position_multiplier = 1.0
            allow_entry = True
            confidence = min(1.0, (RE_LAMINAR_MAX - reynolds_normalized) / RE_LAMINAR_MAX + 0.5)
            notes.append("LAMINAR flow: Market is calm and trending. Optimal entry conditions.")

        elif reynolds_normalized < RE_TRANSIENT_MAX:
            regime = "transient"
            position_multiplier = 0.75
            allow_entry = True
            confidence = 0.60
            notes.append("TRANSIENT flow: Market transitioning. Reduce position sizes by 25%.")

        elif reynolds_normalized < RE_TURBULENT_MAX:
            regime = "turbulent"
            position_multiplier = 0.40
            allow_entry = True  # Allow but with heavy reduction
            confidence = 0.70
            warnings.append("TURBULENT flow: High volatility regime. Fundamentals may be overwhelmed by price action.")
            notes.append("Position size reduced 60%. Widen stops. Prefer quality + strong insider signals only.")

        else:
            regime = "extreme"
            position_multiplier = 0.0
            allow_entry = False
            confidence = 0.85
            warnings.append("EXTREME TURBULENCE: Market in crisis-like conditions. NO new entries.")
            notes.append("Energy cascade building. Await return to turbulent or transient regime.")

        # ── Flow characterization ──────────────────────────────────
        flow_direction = self._compute_flow_direction(closes)
        energy_cascade = self._compute_energy_cascade(closes, volumes)
        min_period     = self._kolmogorov_minimum_period(reynolds_normalized)

        if energy_cascade == "building":
            warnings.append("Energy cascade BUILDING — volatility accelerating. Consider reducing exposure further.")
        elif energy_cascade == "dissipating":
            notes.append("Energy cascade dissipating — volatility normalizing. Regime may improve soon.")

        notes.append(f"Kolmogorov minimum signal period: {min_period} days — ignore signals shorter than this")
        notes.append(f"Flow direction: {flow_direction} | Energy: {energy_cascade}")

        logger.info(
            f"[REYNOLDS] {ticker}: Re={reynolds_normalized:.2f} | "
            f"regime={regime} | pos_mult={position_multiplier:.0%} | "
            f"flow={flow_direction} | cascade={energy_cascade}"
        )

        return ReynoldsResult(
            density=density,
            velocity=velocity,
            length=length,
            viscosity=viscosity,
            reynolds_number=reynolds_normalized,
            regime=regime,
            regime_confidence=confidence,
            position_multiplier=position_multiplier,
            allow_entry=allow_entry,
            flow_direction=flow_direction,
            energy_cascade=energy_cascade,
            minimum_signal_period_days=min_period,
            notes=notes,
            warnings=warnings,
        )
