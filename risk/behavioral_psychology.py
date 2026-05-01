"""
risk/behavioral_psychology.py — Human Nature Signal Engine.

═══════════════════════════════════════════════════════════════════════
THE HARDEST PREDICTION: HUMAN NATURE

Traditional finance assumes rational actors. Behavioral finance proves
they are predictably irrational. The key insight: irrationality is NOT
random — it is SYSTEMATIC and therefore MEASURABLE.

Seven quantifiable human psychological forces we detect:

1. ROUND NUMBER ANCHORING (Kahneman & Tversky, 1974)
   People anchor to round numbers as mental reference points.
   Orders cluster at $50, $100, $150, $200 creating real support/resistance.
   Self-fulfilling prophecy: enough people believe in it, it becomes true.

   Measurable: Distance from nearest round number / price volatility
   Signal: Near round number → expect resistance or support cluster

2. 52-WEEK HIGH EFFECT (George & Hwang, 2004 — Journal of Finance)
   THIS IS THE MOST POWERFUL HUMAN PSYCHOLOGY SIGNAL IN ACADEMIC FINANCE.
   "Nearness to the 52-week high dominates and improves upon the forecasting
   power of past returns. Future returns do not reverse in the long run."

   Why it works: Traders use the 52-week high as an ANCHOR. When a stock
   approaches its 52-week high, even with good news, investors UNDERREACT
   — they're anchored to the high as a "ceiling." This underreaction creates
   persistent momentum.
   
   Conversely, nearness to the ALL-TIME HIGH negatively predicts returns
   (overreaction at extremes).

   Measurable: current_price / 52_week_high (ratio, 0 to 1+)
   Signal: Ratio 0.90-0.99 = approaching high, expect resistance then breakout
           Ratio > 1.00 = just broke out = strong buy signal (momentum)
           Ratio < 0.75 = far from high = mean reversion opportunity

3. LOSS AVERSION ASYMMETRY (Kahneman & Tversky Prospect Theory, 1979)
   "The pain of losing $100 is felt ~2.25× more strongly than the pleasure
   of gaining $100." This creates the DISPOSITION EFFECT:
   - Investors sell winners too early (lock in gains = pleasure)  
   - Investors hold losers too long (avoid realizing pain)

   Measurable as DER (Disposition Effect Ratio):
     DER = (% losers sold) / (% winners sold)
     DER > 1 = panic (selling losers faster) = crisis signal
     DER < 0.5 = complacency (holding losers) = crowded losers signal

4. HERDING / CSAD (Cross-Sectional Absolute Deviation)
   Herd behavior = investors abandoning individual analysis to follow crowd.
   Measurable: when all stocks move together, CSAD drops.
   "Strong evidence of herding during periods of mid to large negative price
   movements, but weak/no evidence during positive movements." (2014)

   CSAD = (1/n) Σ |R_i - R_m|
   Low CSAD = herding = dangerous (crowded trade)
   High CSAD = dispersion = normal healthy market

5. FEAR OF MISSING OUT (FOMO)
   Volume surges near 52-week highs or after price gaps up = FOMO buying.
   Usually unsustainable — but can persist longer than expected.
   Measurable: relative volume × nearness to 52-week high × recency

6. PSYCHOLOGICAL PRICE BARRIERS (round numbers)
   "$50, $100, $150, $200 — round numbers create battlegrounds."
   Breaking through a psychological round number = momentum acceleration.
   Failing to break = resistance → reversal.

7. RECENCY BIAS GRADIENT
   "Stocks that attained the 52-week high price recently outperform
   those that attained it in the distant past by 0.70%/month."
   Recency ratio = 1 / days_since_52wk_high
   High recency ratio = more predictive momentum

═══════════════════════════════════════════════════════════════════════
"""

import statistics
import math
from dataclasses import dataclass, field
from typing import Optional
from loguru import logger


# ── Psychological price levels ─────────────────────────────────────
# Round numbers that humans fixate on — causing real order clustering
PSYCHOLOGICAL_ROUND_LEVELS = [
    # Whole dollar rounds (strongest)
    1, 2, 3, 4, 5, 7, 10, 12, 15, 20, 25, 30, 40, 50,
    60, 70, 75, 80, 100, 120, 125, 150, 175, 200, 250,
    300, 400, 500, 750, 1000, 1500, 2000, 2500, 5000,
]

# Comfort number zones — human "it just feels right" prices
# Based on common psychological price preferences
COMFORT_NUMBER_ENDINGS = [0, 5, 25, 50, 75, 99]

# Loss aversion coefficient (Kahneman & Tversky) — losses feel 2.25× gains
LOSS_AVERSION_LAMBDA = 2.25


@dataclass
class RoundNumberAnalysis:
    current_price: float
    nearest_round_below: float
    nearest_round_above: float
    distance_to_below_pct: float      # % from nearest round below
    distance_to_above_pct: float      # % from nearest round above
    at_psychological_level: bool      # Within 0.5% of a round number
    approaching_resistance: bool      # Within 2% below a round number
    just_broke_resistance: bool       # Within 1% above a round number (breakout)
    nearest_round: float
    nearest_round_distance_pct: float
    signal: str                       # "strong_support", "approaching_resistance",
                                      # "breakout", "in_no_mans_land"
    zone_strength: float              # 0.0 to 1.0 — how significant the level is


@dataclass
class FiftyTwoWeekResult:
    current_price: float
    high_52w: float
    low_52w: float
    all_time_high: Optional[float]

    # George & Hwang nearness ratio (0 = at low, 1 = at high, >1 = above)
    nearness_to_52w_high: float       # current / 52w_high
    nearness_to_52w_low: float        # current / 52w_low
    nearness_to_all_time_high: Optional[float]

    # Recency (days since 52-week high was set)
    days_since_52w_high: Optional[int]
    recency_ratio: float              # Higher = more recent = stronger momentum

    # Classification (George & Hwang 2004)
    momentum_category: str            # "breakout", "near_high", "mid_range",
                                      # "near_low", "in_recovery"

    # Predicted signal (from academic research)
    gh_signal: str                    # "strong_buy", "buy", "hold", "sell"
    gh_signal_strength: float         # 0.0 to 1.0

    # Human psychology interpretation
    investor_psychology: str          # What investors are feeling
    resistance_level: Optional[float] # Where sellers are anchored
    support_level: Optional[float]    # Where buyers are anchored
    notes: list


@dataclass
class ProspectTheoryResult:
    """Loss aversion and disposition effect analysis."""

    # Reference point for this investment
    reference_price: float            # Entry price or recent high
    current_price: float

    # Gain/loss from reference
    gain_loss_pct: float              # + for gain, - for loss
    is_in_loss: bool

    # Prospect theory value function
    # V(x) = x^α if gain, -λ(-x)^β if loss
    # α = β = 0.88, λ = 2.25 (Kahneman & Tversky 1992)
    prospect_value: float             # Psychological value of current position

    # Disposition effect prediction
    # Holders in loss: anchored to reference, reluctant to sell
    # Holders in gain: eager to sell and lock in profit
    selling_pressure_score: float     # 0.0 to 1.0 — how much pressure to sell
    buying_pressure_score: float      # 0.0 to 1.0 — how much pressure to buy

    # Market-wide disposition
    overhead_supply: float            # How many holders are above water (selling)
    trapped_longs_pct: float          # % of last year's volume that bought higher

    signal: str
    notes: list


@dataclass
class HerdingResult:
    """CSAD-based herding detector."""

    # CSAD = Cross-Sectional Absolute Deviation
    csad: float                       # Current CSAD
    csad_historical_avg: float        # Historical average CSAD
    csad_normalized: float            # csad / historical_avg (1.0 = normal)

    herding_detected: bool
    herding_intensity: float          # 0.0 to 1.0

    # Herding direction
    herding_direction: str            # "downward" (panic) or "upward" (mania)

    # Disposition Effect Ratio proxy
    der_estimate: float               # DER > 1 = panic selling

    market_psychology: str            # "panic", "mania", "fomo", "normal", "apathy"
    signal: str                       # "avoid", "caution", "neutral", "contrarian_buy"
    notes: list


@dataclass
class BehavioralPsychologyResult:
    """Combined behavioral psychology output."""
    ticker: str

    # Component results
    round_number: RoundNumberAnalysis
    fifty_two_week: FiftyTwoWeekResult
    prospect_theory: ProspectTheoryResult
    herding: HerdingResult

    # Composite behavioral score (-1.0 to +1.0)
    behavioral_score: float

    # Human nature summary
    dominant_bias: str                # What human bias is most active right now
    bias_direction: str               # Does it push price up or down?
    exploitable_edge: str             # How to profit from this bias

    # Position guidance
    position_adjustment: float        # Multiply position by this (0.5 to 1.25)
    entry_timing: str                 # "good", "early", "late", "avoid"

    human_nature_narrative: str       # Plain English explanation
    notes: list
    warnings: list


class BehavioralPsychologyEngine:
    """
    Detects and quantifies human psychological biases in price data.
    Converts irrational human behavior into measurable trading signals.

    "Humans are predictably irrational. Irrationality IS the edge."
    """

    # Round number proximity thresholds
    AT_LEVEL_PCT      = 0.005   # Within 0.5% = at the level
    APPROACHING_PCT   = 0.020   # Within 2% below = approaching resistance
    BREAKOUT_PCT      = 0.010   # Within 1% above = fresh breakout

    # 52-week high zones
    NEAR_HIGH_ZONE    = 0.95    # >95% of 52-week high = near high
    MID_ZONE          = 0.75    # 75-95% = mid range
    NEAR_LOW_ZONE     = 0.60    # <75% = near low zone

    # CSAD herding threshold
    HERDING_THRESHOLD = 0.65    # CSAD / historical_avg < this = herding

    def _find_nearest_round_numbers(self, price: float) -> tuple:
        """Find the nearest psychological round numbers above and below current price."""
        below = max([r for r in PSYCHOLOGICAL_ROUND_LEVELS if r <= price], default=price * 0.95)
        above = min([r for r in PSYCHOLOGICAL_ROUND_LEVELS if r > price], default=price * 1.05)

        # Also check comfort number endings
        for multiplier in [1, 10, 100, 1000]:
            for ending in COMFORT_NUMBER_ENDINGS:
                candidate = (int(price / multiplier) * multiplier) + (ending * multiplier / 100)
                if candidate <= price and candidate > below:
                    below = candidate
                elif candidate > price and candidate < above:
                    above = candidate

        return below, above

    def analyze_round_numbers(self, price: float) -> RoundNumberAnalysis:
        """Detect proximity to psychological round number levels."""
        below, above = self._find_nearest_round_numbers(price)

        dist_below_pct = (price - below) / price if price > 0 else 0
        dist_above_pct = (above - price) / price if price > 0 else 0

        at_level       = min(dist_below_pct, dist_above_pct) < self.AT_LEVEL_PCT
        approaching_r  = dist_above_pct < self.APPROACHING_PCT and not at_level
        just_broke     = dist_below_pct < self.BREAKOUT_PCT and dist_below_pct > 0.001

        # Which round number is nearest
        nearest = below if dist_below_pct < dist_above_pct else above
        nearest_dist = min(dist_below_pct, dist_above_pct)

        # Zone strength: rounder numbers are stronger psychological levels
        # $100 > $50 > $25 > $10 > $5 > $1
        zone_strength = 0.3   # Default for minor levels
        if nearest in [100, 200, 500, 1000, 2000, 5000]:
            zone_strength = 1.0   # Major round hundreds/thousands
        elif nearest in [50, 150, 250, 750]:
            zone_strength = 0.85  # Half-centuries
        elif nearest in [25, 75, 125, 175]:
            zone_strength = 0.70  # Quarter-centuries
        elif nearest in [10, 20, 30, 40, 60, 70, 80]:
            zone_strength = 0.55  # Tens
        elif nearest in [5, 15]:
            zone_strength = 0.40  # Fives

        # Classify signal
        if at_level:
            signal = "at_psychological_level"
        elif just_broke:
            signal = "breakout"       # Just cleared resistance — momentum
        elif approaching_r:
            signal = "approaching_resistance"   # Slowdown likely
        else:
            signal = "in_no_mans_land"          # No major level nearby

        logger.debug(
            f"[PSYCHOLOGY] Round levels: below=${below:.2f} above=${above:.2f} "
            f"signal={signal} strength={zone_strength:.2f}"
        )

        return RoundNumberAnalysis(
            current_price=price,
            nearest_round_below=below,
            nearest_round_above=above,
            distance_to_below_pct=dist_below_pct,
            distance_to_above_pct=dist_above_pct,
            at_psychological_level=at_level,
            approaching_resistance=approaching_r,
            just_broke_resistance=just_broke,
            nearest_round=nearest,
            nearest_round_distance_pct=nearest_dist,
            signal=signal,
            zone_strength=zone_strength,
        )

    def analyze_52w_effect(
        self,
        current_price: float,
        high_52w: float,
        low_52w: float,
        all_time_high: Optional[float] = None,
        days_since_52w_high: Optional[int] = None,
    ) -> FiftyTwoWeekResult:
        """
        Apply George & Hwang (2004) 52-week high effect.
        This is the single most academically validated human psychology
        signal in finance — dominates traditional momentum factors.
        """
        notes = []

        # Core ratios
        nearness_high = current_price / high_52w if high_52w > 0 else 1.0
        nearness_low  = current_price / low_52w  if low_52w > 0 else 1.0
        nearness_ath  = (current_price / all_time_high) if all_time_high and all_time_high > 0 else None

        # Recency ratio (higher = hit 52-week high more recently = stronger momentum)
        if days_since_52w_high is not None and days_since_52w_high > 0:
            recency = 1.0 / days_since_52w_high
        elif days_since_52w_high == 0:
            recency = 1.0   # Just hit today
        else:
            recency = 0.01  # Unknown — assume distant

        # Classify momentum category
        if nearness_high > 1.01:
            category = "breakout"           # Above 52-week high — strong buy signal
            notes.append("Price above 52-week high — investors underreacted, strong momentum expected")
            investor_psych = "FOMO kicking in — momentum chasers entering, anchored sellers capitulating"
            resistance = None
            support = high_52w   # Old resistance becomes support
        elif nearness_high >= self.NEAR_HIGH_ZONE:
            category = "near_high"          # 95-100% of high
            notes.append("Approaching 52-week high — anchored sellers creating friction, expect underreaction")
            investor_psych = "Anchored sellers reluctant to bid higher — but news catalyst could break through"
            resistance = high_52w
            support = high_52w * 0.95
        elif nearness_high >= self.MID_ZONE:
            category = "mid_range"          # 75-95% of high
            notes.append("Mid-range — neutral momentum territory")
            investor_psych = "Neither anchored to high nor near low — fundamental factors dominate"
            resistance = high_52w
            support = low_52w
        elif nearness_high >= self.NEAR_LOW_ZONE:
            category = "near_low"           # 60-75% of high
            notes.append("Near 52-week low — loss aversion keeping holders trapped, value buyers emerging")
            investor_psych = "Loss-averse holders trapped, some capitulation, value buyers providing floor"
            resistance = high_52w * 0.75
            support = low_52w
        else:
            category = "in_recovery"        # Below 60% of high
            notes.append("Deep below 52-week high — maximum anchoring, investor capitulation zone")
            investor_psych = "Maximum pain, forced selling possible, but contrarian value opportunity"
            resistance = low_52w * 1.20
            support = low_52w

        # George & Hwang signal mapping
        # Based on: top nearness_high decile = strong buy, bottom = avoid
        if nearness_high > 1.01:
            gh_signal = "strong_buy"
            gh_strength = min(1.0, 0.80 + recency * 10)
            notes.append("George & Hwang: BREAKOUT above 52-wk high — historically strongest momentum signal")
        elif nearness_high >= 0.95:
            gh_signal = "buy"
            gh_strength = 0.65 * nearness_high
            notes.append(f"George & Hwang: Near 52-wk high ({nearness_high:.0%}) — underreaction momentum building")
        elif nearness_high >= 0.75:
            gh_signal = "hold"
            gh_strength = 0.50
        elif nearness_high >= 0.60:
            gh_signal = "sell"
            gh_strength = 0.35
            notes.append("George & Hwang: Far from 52-wk high — anchoring suppressing recovery")
        else:
            gh_signal = "strong_sell"
            gh_strength = 0.20

        # ATH effect: near ATH negatively predicts returns (overreaction at extremes)
        if nearness_ath and nearness_ath >= 0.98:
            notes.append("Warning: Near ALL-TIME HIGH — historical analysis shows negative return prediction at extremes")

        # Recency amplifier: recent 52-wk high = 2× stronger signal
        if days_since_52w_high is not None and days_since_52w_high <= 30:
            gh_strength = min(1.0, gh_strength * 1.50)
            notes.append(f"Recent 52-week high ({days_since_52w_high} days ago) — recency bias amplifies momentum")

        logger.debug(
            f"[52WK] nearness={nearness_high:.2f} category={category} "
            f"gh_signal={gh_signal} strength={gh_strength:.2f}"
        )

        return FiftyTwoWeekResult(
            current_price=current_price,
            high_52w=high_52w, low_52w=low_52w,
            all_time_high=all_time_high,
            nearness_to_52w_high=nearness_high,
            nearness_to_52w_low=nearness_low,
            nearness_to_all_time_high=nearness_ath,
            days_since_52w_high=days_since_52w_high,
            recency_ratio=recency,
            momentum_category=category,
            gh_signal=gh_signal,
            gh_signal_strength=gh_strength,
            investor_psychology=investor_psych,
            resistance_level=resistance,
            support_level=support,
            notes=notes,
        )

    def analyze_prospect_theory(
        self,
        current_price: float,
        reference_price: float,      # Entry price or recent significant level
        price_history: list,         # Historical prices for overhead supply
        volume_history: Optional[list] = None,
    ) -> ProspectTheoryResult:
        """
        Apply Kahneman & Tversky Prospect Theory to assess investor psychology.

        The prospect theory value function:
          V(x) = x^0.88           if x >= 0 (gains, concave — diminishing sensitivity)
          V(x) = -2.25 × (-x)^0.88  if x < 0 (losses, steeper — loss aversion)

        This creates ASYMMETRIC behavior: investors feel losses ~2.25× more
        intensely than equivalent gains.
        """
        notes = []
        alpha = beta = 0.88   # Prospect theory curvature parameters
        lam = LOSS_AVERSION_LAMBDA   # 2.25

        gain_loss_pct = (current_price - reference_price) / reference_price if reference_price > 0 else 0
        is_loss = gain_loss_pct < 0

        # Prospect theory value function
        if is_loss:
            x = abs(gain_loss_pct)
            prospect_value = -lam * (x ** alpha)
            notes.append(
                f"Prospect Theory: Position at {gain_loss_pct:.1%} loss → "
                f"psychological pain = {prospect_value:.2f} (loss feels {lam}× more intense)"
            )
            # Loss aversion: holders RELUCTANT to sell (admit mistake)
            # BUT if losses get large, panic selling threshold hit
            if abs(gain_loss_pct) < 0.05:
                selling_pressure = 0.2   # Small loss — denial phase, holding
                buying_pressure  = 0.3
                notes.append("Small loss: Disposition effect — holders in denial, reluctant to sell")
            elif abs(gain_loss_pct) < 0.15:
                selling_pressure = 0.15  # Medium loss — paralysis
                buying_pressure  = 0.4
                notes.append("Medium loss: Holder paralysis — anchored to purchase price")
            else:
                selling_pressure = 0.7   # Large loss — forced selling, capitulation
                buying_pressure  = 0.6   # But also value buyers
                notes.append("Large loss: Approaching capitulation zone — forced selling + value buyers")
        else:
            x = gain_loss_pct
            prospect_value = x ** alpha
            notes.append(
                f"Prospect Theory: Position at {gain_loss_pct:.1%} gain → "
                f"psychological value = {prospect_value:.2f} (risk aversion in gains)"
            )
            # In gains: disposition effect — EAGER to sell and lock in profit
            if gain_loss_pct < 0.10:
                selling_pressure = 0.4   # Moderate pressure to take profits
                buying_pressure  = 0.5
            elif gain_loss_pct < 0.25:
                selling_pressure = 0.65  # Strong pressure to take profits
                buying_pressure  = 0.35
                notes.append("Significant gain: Disposition effect — holders eager to realize profits")
            else:
                selling_pressure = 0.80  # Very strong profit-taking pressure
                buying_pressure  = 0.25
                notes.append("Large gain: Strong profit-taking pressure — expect distribution")

        # Overhead supply: what % of the past year's volume is now underwater?
        # (bought at higher prices = trapped longs = selling pressure when price rises)
        overhead_supply = 0.0
        trapped_pct = 0.0
        if price_history and len(price_history) >= 20:
            higher_prices = [p for p in price_history if p > current_price]
            trapped_pct = len(higher_prices) / len(price_history)
            overhead_supply = trapped_pct
            if trapped_pct > 0.40:
                notes.append(
                    f"Overhead supply: {trapped_pct:.0%} of recent prices are above current — "
                    f"trapped longs will sell into any rally"
                )
            elif trapped_pct < 0.20:
                notes.append(
                    f"Low overhead supply ({trapped_pct:.0%}) — most holders in profit, "
                    f"fewer trapped longs to create selling pressure"
                )

        # Signal
        if is_loss and abs(gain_loss_pct) > 0.20:
            signal = "capitulation_opportunity"   # Contrarian buy near capitulation
        elif is_loss:
            signal = "holder_paralysis"           # Don't expect quick recovery
        elif gain_loss_pct > 0.20:
            signal = "profit_taking_risk"         # Distribution likely
        else:
            signal = "normal"

        return ProspectTheoryResult(
            reference_price=reference_price,
            current_price=current_price,
            gain_loss_pct=gain_loss_pct,
            is_in_loss=is_loss,
            prospect_value=prospect_value,
            selling_pressure_score=selling_pressure,
            buying_pressure_score=buying_pressure,
            overhead_supply=overhead_supply,
            trapped_longs_pct=trapped_pct,
            signal=signal,
            notes=notes,
        )

    def detect_herding(
        self,
        ticker_returns: list,         # Daily returns for target ticker
        market_returns: list,         # Daily S&P 500 returns (same period)
        sector_returns: Optional[list] = None,  # Sector returns (optional)
    ) -> HerdingResult:
        """
        Detect herding behavior using CSAD (Cross-Sectional Absolute Deviation).

        CSAD = mean absolute deviation of stock returns from market return.
        Low CSAD = all stocks moving together = herding.
        High CSAD = stocks moving independently = healthy dispersion.

        In a crisis: CSAD drops sharply as panic herding sets in.
        In a mania: CSAD also drops as FOMO herding sets in (upward herding).
        """
        notes = []
        n = min(len(ticker_returns), len(market_returns), 20)

        if n < 5:
            return HerdingResult(
                csad=0.02, csad_historical_avg=0.02, csad_normalized=1.0,
                herding_detected=False, herding_intensity=0.0,
                herding_direction="unknown",
                der_estimate=1.0,
                market_psychology="unknown",
                signal="neutral",
                notes=["Insufficient data for herding analysis"],
            )

        ticker_r = ticker_returns[-n:]
        market_r = market_returns[-n:]

        # CSAD: mean absolute deviation of ticker from market
        csad_values = [abs(t - m) for t, m in zip(ticker_r, market_r)]
        csad = statistics.mean(csad_values)

        # Historical average CSAD (if we had multiple stocks, we'd compute cross-sectional)
        # Approximation: use VIX-implied average or assume 2% historical
        # In production: compute across all watchlist stocks
        historical_avg = 0.015   # ~1.5% daily dispersion = normal
        if len(csad_values) > 10:
            # Use the older half as the historical baseline
            historical_avg = statistics.mean(csad_values[:n//2])

        csad_normalized = csad / historical_avg if historical_avg > 0 else 1.0

        # Herding detection
        herding = csad_normalized < self.HERDING_THRESHOLD
        herding_intensity = max(0.0, 1.0 - csad_normalized) if herding else 0.0

        # Herding direction
        avg_market_return = statistics.mean(market_r[-5:]) if len(market_r) >= 5 else 0
        if herding:
            if avg_market_return < -0.005:
                direction = "downward"   # Panic
            elif avg_market_return > 0.005:
                direction = "upward"     # Mania/FOMO
            else:
                direction = "sideways"
        else:
            direction = "dispersed"

        # DER proxy: if market is down and ticker is more down → excess selling
        # Simplified: abs(ticker_return - market_return) / ticker_return std
        der_estimate = 1.0
        if ticker_r and market_r:
            recent_t = statistics.mean(ticker_r[-3:])
            recent_m = statistics.mean(market_r[-3:])
            if recent_t < 0 and recent_m < 0:
                der_estimate = abs(recent_t) / max(0.001, abs(recent_m))

        # Market psychology classification
        if herding and direction == "downward" and der_estimate > 1.5:
            psych = "panic"
            signal = "contrarian_buy"   # Panic often = buying opportunity
            notes.append(f"PANIC HERDING detected — CSAD={csad_normalized:.2f}×normal, DER={der_estimate:.2f}")
            notes.append("Contrarian signal: panic bottoms are buying opportunities for quality stocks")
        elif herding and direction == "upward":
            psych = "mania"
            signal = "caution"
            notes.append(f"MANIA HERDING detected — CSAD={csad_normalized:.2f}×normal")
            notes.append("FOMO-driven rally — unsustainable without fundamental support")
        elif not herding and csad_normalized > 1.5:
            psych = "dispersion"
            signal = "neutral"
            notes.append("High dispersion — stock-specific factors dominating, healthy market")
        elif der_estimate > 2.0:
            psych = "apathy"
            signal = "avoid"
            notes.append("Excessive selling pressure on this ticker vs market — avoid")
        else:
            psych = "normal"
            signal = "neutral"

        logger.debug(
            f"[HERDING] CSAD={csad:.4f} norm={csad_normalized:.2f} "
            f"herding={herding} psych={psych}"
        )

        return HerdingResult(
            csad=csad,
            csad_historical_avg=historical_avg,
            csad_normalized=csad_normalized,
            herding_detected=herding,
            herding_intensity=herding_intensity,
            herding_direction=direction,
            der_estimate=der_estimate,
            market_psychology=psych,
            signal=signal,
            notes=notes,
        )

    def analyze(
        self,
        ticker: str,
        current_price: float,
        price_history: list,          # Daily closes, oldest first
        high_52w: float,
        low_52w: float,
        market_returns: list,         # S&P 500 daily returns
        reference_price: Optional[float] = None,    # Entry price if holding
        all_time_high: Optional[float] = None,
        days_since_52w_high: Optional[int] = None,
        volume_history: Optional[list] = None,
    ) -> BehavioralPsychologyResult:
        """
        Run full behavioral psychology analysis.
        Returns BehavioralPsychologyResult with composite signal.
        """
        notes    = []
        warnings = []

        # Daily returns for herding analysis
        ticker_returns = []
        if len(price_history) >= 2:
            ticker_returns = [
                (price_history[i] - price_history[i-1]) / price_history[i-1]
                for i in range(1, len(price_history))
                if price_history[i-1] > 0
            ]

        # Run all analyses
        round_num = self.analyze_round_numbers(current_price)
        fifty_two = self.analyze_52w_effect(
            current_price, high_52w, low_52w,
            all_time_high=all_time_high,
            days_since_52w_high=days_since_52w_high,
        )
        prospect = self.analyze_prospect_theory(
            current_price=current_price,
            reference_price=reference_price or current_price * 0.95,
            price_history=price_history[-252:] if len(price_history) >= 252 else price_history,
            volume_history=volume_history,
        )
        herding = self.detect_herding(
            ticker_returns=ticker_returns[-20:],
            market_returns=market_returns[-20:] if market_returns else [],
        )

        notes.extend(round_num.signal and [f"Round levels: {round_num.signal}"] or [])
        notes.extend(fifty_two.notes)
        notes.extend(prospect.notes)
        notes.extend(herding.notes)

        # ── Composite behavioral score ─────────────────────────────
        score = 0.0

        # 52-week high effect (strongest single behavioral signal)
        gh_map = {"strong_buy": 1.0, "buy": 0.6, "hold": 0.0, "sell": -0.5, "strong_sell": -1.0}
        score += gh_map.get(fifty_two.gh_signal, 0) * 0.45 * fifty_two.gh_signal_strength

        # Round number effect
        rn_map = {"breakout": 0.5, "in_no_mans_land": 0.0, "at_psychological_level": -0.1, "approaching_resistance": -0.3}
        score += rn_map.get(round_num.signal, 0) * 0.20 * round_num.zone_strength

        # Prospect theory (selling vs buying pressure)
        score += (prospect.buying_pressure_score - prospect.selling_pressure_score) * 0.20

        # Herding
        herd_map = {"contrarian_buy": 0.4, "neutral": 0.0, "caution": -0.2, "avoid": -0.5}
        score += herd_map.get(herding.signal, 0) * 0.15

        score = max(-1.0, min(1.0, score))

        # ── Dominant bias ──────────────────────────────────────────
        # What human bias is most active right now?
        if herding.market_psychology == "panic":
            dominant_bias = "loss_aversion_panic"
            bias_direction = "downward_overshooting"
            edge = "Buy quality stocks in panic — loss aversion creates below-intrinsic-value prices"
        elif herding.market_psychology == "mania":
            dominant_bias = "fomo_herding"
            bias_direction = "upward_overshooting"
            edge = "Reduce position / take profits — FOMO rallies revert to fundamentals"
        elif fifty_two.momentum_category == "breakout":
            dominant_bias = "anchoring_breakout"
            bias_direction = "upward_momentum"
            edge = "Ride the breakout — anchored sellers capitulating creates momentum"
        elif fifty_two.momentum_category == "near_high":
            dominant_bias = "anchoring_resistance"
            bias_direction = "resistance_friction"
            edge = "Position for breakthrough — underreaction creates delayed momentum if fundamentals strong"
        elif prospect.overhead_supply > 0.50:
            dominant_bias = "overhead_supply"
            bias_direction = "selling_pressure"
            edge = "Wait for overhead supply to clear — trapped longs create ceiling"
        elif round_num.approaching_resistance:
            dominant_bias = "round_number_anchoring"
            bias_direction = "resistance_friction"
            edge = f"Resistance at ${round_num.nearest_round_above:.2f} — position size for potential rejection"
        else:
            dominant_bias = "diffuse"
            bias_direction = "neutral"
            edge = "No dominant bias detected — fundamentals and technical signals dominate"

        # ── Position and timing guidance ───────────────────────────
        if score >= 0.40:
            pos_adj = 1.10    # Behavioral tailwind — slight size increase
            timing = "good"
        elif score >= 0.10:
            pos_adj = 1.00
            timing = "good"
        elif score >= -0.10:
            pos_adj = 0.90
            timing = "neutral"
        elif score >= -0.30:
            pos_adj = 0.70
            timing = "early"   # Behavioral headwinds but not blocking
        else:
            pos_adj = 0.50
            timing = "avoid"   # Strong behavioral headwinds

        # ── Human nature narrative ─────────────────────────────────
        narrative = (
            f"Behavioral analysis for {ticker}: "
            f"The dominant human bias is {dominant_bias.replace('_', ' ')} "
            f"pushing prices {bias_direction.replace('_', ' ')}. "
            f"The 52-week high effect (George & Hwang) gives a {fifty_two.gh_signal} signal "
            f"at {fifty_two.nearness_to_52w_high:.0%} of the annual high. "
            f"Round number proximity: {round_num.signal.replace('_', ' ')} "
            f"(nearest ${round_num.nearest_round:.2f}, {round_num.nearest_round_distance_pct:.1%} away). "
            f"Investor psychology: {fifty_two.investor_psychology}. "
            f"Your edge: {edge}"
        )

        logger.info(
            f"[BEHAVIORAL] {ticker}: bias={dominant_bias} "
            f"score={score:.2f} timing={timing} "
            f"52wk={fifty_two.gh_signal} rn={round_num.signal} "
            f"herding={herding.market_psychology}"
        )

        return BehavioralPsychologyResult(
            ticker=ticker,
            round_number=round_num,
            fifty_two_week=fifty_two,
            prospect_theory=prospect,
            herding=herding,
            behavioral_score=score,
            dominant_bias=dominant_bias,
            bias_direction=bias_direction,
            exploitable_edge=edge,
            position_adjustment=pos_adj,
            entry_timing=timing,
            human_nature_narrative=narrative,
            notes=notes,
            warnings=warnings,
        )
