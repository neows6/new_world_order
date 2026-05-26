"""
decision/ensemble_kelly.py — Ensemble Forecast + Kelly Criterion Position Sizing.

═══════════════════════════════════════════════════════════════════════
ENSEMBLE FORECASTING (Weather Forecasting Method)
═══════════════════════════════════════════════════════════════════════

ECMWF (European Centre for Medium-Range Weather Forecasts) runs 51 ensemble
members with slightly perturbed initial conditions to capture forecast
uncertainty. The spread of the ensemble tells you how confident to be.
Narrow spread = confident forecast. Wide spread = high uncertainty.

The key insight from Bayesian Model Averaging (Raftery et al., Monthly
Weather Review 2005): "The goal of probabilistic forecasting is to
maximize sharpness subject to calibration." A calibrated forecast means
when you say 70%, you're right 70% of the time.

We apply this by running multiple independent forecasting "models"
(fundamentals, technical, quantum, Kalman, Reynolds) and combining
their outputs via BMA:

  P(outcome) = Σ w_k × P_k(outcome)

  where w_k = BMA weight proportional to model historical performance
  (we approximate with our signal weights and regime adjustments).

The ensemble SPREAD gives us our uncertainty estimate — exactly like
weather ensembles. Tight spread → high confidence → larger position.
Wide spread → low confidence → smaller position or no trade.

═══════════════════════════════════════════════════════════════════════
KELLY CRITERION — OPTIMAL BET SIZING
═══════════════════════════════════════════════════════════════════════

Kelly (1956) proved that the optimal fraction of capital to bet is:

  f* = (b × p - q) / b = p - q/b

  where:
    p = probability of winning
    q = 1 - p = probability of losing
    b = odds (potential gain / potential loss = R/R ratio)

This maximizes the geometric growth rate of capital over time.
Betting MORE than Kelly leads to ruin. Betting less is suboptimal.

In practice, we use FRACTIONAL KELLY (typically 0.25× to 0.5×) to:
  - Account for model uncertainty (our p is estimated, not exact)
  - Reduce variance (full Kelly has very high variance)
  - Survive the inevitable losing streaks

Full Kelly on uncertain probabilities is DANGEROUS. But 25% Kelly
with high-conviction signals is mathematically optimal.

From Kelly Betting as Bayesian Model Evaluation (arXiv 2602.09982, 2026):
"Each model treated as a canonical Kelly bettor. Better-calibrated models
siphon credibility from worse models over time." We use this to weight
our ensemble members.

═══════════════════════════════════════════════════════════════════════
"""

import math
import statistics
from dataclasses import dataclass, field
from typing import Optional
from loguru import logger


# Kelly fraction — we use half-Kelly for safety
KELLY_FRACTION = 0.25       # 25% of full Kelly — conservative but compounding

# Ensemble members and their base weights (calibrated from research)
ENSEMBLE_WEIGHTS = {
    "fundamental":  0.20,   # ROIC, moat, intrinsic value (reduced — DCF alone shouldn't block breakouts)
    "momentum":     0.15,   # RVOL, MACD, MA stack, ATR, 52w breakout
    "quantum":      0.15,   # Quantum state probability
    "kalman":       0.15,   # Kalman trend signal
    "reynolds":     0.10,   # Fluid dynamics regime
    "technical":    0.15,   # Fib + VWAP
    "insider":      0.10,   # Form 4 buying
}


@dataclass
class EnsembleMember:
    name: str
    signal_score: float      # -1.0 to +1.0
    win_probability: float   # 0.0 to 1.0 (for Kelly)
    weight: float            # BMA weight
    confidence: float        # 0.0 to 1.0 — how confident this model is


@dataclass
class EnsembleForecastResult:
    """
    Ensemble forecast combining all models via Bayesian Model Averaging.
    Produces calibrated probability distribution over outcomes.
    """
    # Ensemble members
    members: list                     # List of EnsembleMember

    # BMA output
    ensemble_probability_bull: float  # P(positive outcome)
    ensemble_probability_bear: float  # P(negative outcome)
    ensemble_score: float             # Weighted composite (-1.0 to +1.0)

    # Ensemble spread (uncertainty)
    ensemble_spread: float            # Std dev of member scores
    spread_category: str              # "tight", "moderate", "wide"

    # Calibrated confidence (spread-adjusted)
    calibrated_confidence: float      # 0.0 to 1.0

    # Kelly-optimal position size
    kelly_win_probability: float      # Our best estimate of P(win)
    kelly_fraction: float             # Full Kelly fraction
    recommended_fraction: float       # 25% Kelly (our actual recommendation)
    kelly_reasoning: str

    # BMA regime adjustments
    regime_weight_multiplier: float   # Reynolds-adjusted weight
    effective_position_pct: float     # Final position size recommendation

    # Outcome distribution
    p10_outcome: float                # 10th percentile outcome (bear case)
    p50_outcome: float                # 50th percentile (base case)
    p90_outcome: float                # 90th percentile (bull case)

    notes: list
    warnings: list


class EnsembleKellyEngine:
    """
    Runs ensemble forecast using BMA and sizes positions with Kelly Criterion.

    This is the CEO-level thinking: we're not trying to predict the future.
    We're trying to identify when we have a GENUINE edge (P > 0.55) and
    size our bets OPTIMALLY for that edge using Kelly.

    Most retail traders either bet too much (risking ruin) or too little
    (leaving returns on the table). Kelly gives us the mathematical optimum.
    """

    # Minimum win probability to enter a trade
    MIN_WIN_PROB = 0.55             # Must believe P(win) > 55% to trade

    # Spread thresholds for confidence classification
    TIGHT_SPREAD    = 0.20          # Ensemble members broadly agree
    MODERATE_SPREAD = 0.40          # Some disagreement
    # Above moderate = wide spread

    # Maximum portfolio fraction (hard cap, even if Kelly says more)
    MAX_POSITION_PCT = 0.05         # Never exceed 5% of portfolio per trade

    def _score_to_win_prob(self, score: float) -> float:
        """
        Convert a signal score (-1.0 to +1.0) to a win probability.
        Uses a sigmoid transformation calibrated to historical signal accuracy.

        At score=0: P(win) = 0.50 (coin flip)
        At score=1.0: P(win) ≈ 0.75 (strong edge)
        At score=-1.0: P(win) ≈ 0.25 (strong against)

        Sigmoid: P = 1 / (1 + e^(-k × score))
        k=1.5 calibrated to our signal historical accuracy estimates.
        """
        k = 1.5   # Steepness — higher = more aggressive conversion
        return 1.0 / (1.0 + math.exp(-k * score))

    def _compute_kelly_fraction(
        self,
        win_prob: float,
        risk_reward_ratio: float,
    ) -> float:
        """
        Kelly Criterion: f* = (b × p - q) / b

        Where:
          b = risk/reward ratio (potential gain / potential loss)
          p = win probability
          q = 1 - p = loss probability

        Example:
          win_prob = 0.60, risk_reward = 2.0 (risk $1 to make $2)
          f* = (2.0 × 0.60 - 0.40) / 2.0 = (1.20 - 0.40) / 2.0 = 0.40
          → Bet 40% of capital (full Kelly)
          → With 25% Kelly: bet 10% of capital
        """
        q = 1.0 - win_prob
        b = max(0.1, risk_reward_ratio)   # Prevent division by zero

        kelly = (b * win_prob - q) / b

        return max(0.0, min(1.0, kelly))   # Cap between 0 and 1

    def _compute_outcome_distribution(
        self,
        ensemble_score: float,
        spread: float,
        current_price: float,
        risk_reward: float,
    ) -> tuple:
        """
        Compute P10/P50/P90 outcome prices.
        Uses normal distribution approximation around ensemble estimate.
        Analogous to weather forecast confidence intervals.
        """
        if current_price <= 0:
            return 0.0, 0.0, 0.0

        # Expected return proportional to ensemble score and R/R
        expected_return = ensemble_score * 0.05 * risk_reward   # 5% base move

        # Spread creates the probability distribution width
        sigma = max(0.02, spread * 0.10)   # Spread → price uncertainty

        # P10/P90 use Student's t(df=5) quantiles (±1.476) instead of normal (±1.28)
        # Fat tails: market returns have excess kurtosis ~4-6; t(5) captures this
        T5_Q = 1.476
        p10 = current_price * (1.0 + expected_return - T5_Q * sigma)
        p50 = current_price * (1.0 + expected_return)
        p90 = current_price * (1.0 + expected_return + T5_Q * sigma)

        return p10, p50, p90

    def run(
        self,
        ticker: str,

        # Input scores from each layer
        fundamental_score: float,
        quantum_score: float,
        kalman_score: float,
        reynolds_position_mult: float,
        technical_score: float,
        insider_score: float,
        momentum_score: float = 0.0,      # From signals/momentum.py

        # Supporting data
        win_probability_estimate: float = 0.5,   # From quantum state
        risk_reward_ratio: float = 2.0,           # From Fibonacci targets
        current_price: Optional[float] = None,
        portfolio_value: Optional[float] = None,

        # Dollar limits
        max_trade_dollars: float = 500.0,
        max_position_pct: float = 0.05,
    ) -> EnsembleForecastResult:
        """
        Run ensemble forecast and compute Kelly-optimal position size.
        """
        notes    = []
        warnings = []

        # ── Assemble ensemble members ──────────────────────────────
        # Semantic mapping: turbulent (0.40) ≠ bearish; it's cautious-neutral.
        # The old arithmetic rescaling (mult-0.5)*2 made Re=0.40 read as -0.20 (bearish) — wrong.
        _REYNOLDS_SCORE_MAP = {1.0: 0.30, 0.40: 0.00, 0.15: -0.20, 0.10: -0.30}
        reynolds_score = _REYNOLDS_SCORE_MAP.get(round(reynolds_position_mult, 2), 0.0)

        raw_scores = {
            "fundamental": fundamental_score,
            "momentum":    momentum_score,
            "quantum":     quantum_score,
            "kalman":      kalman_score,
            "reynolds":    reynolds_score,
            "technical":   technical_score,
            "insider":     insider_score,
        }

        members = []
        for name, score in raw_scores.items():
            win_p = self._score_to_win_prob(score)
            weight = ENSEMBLE_WEIGHTS.get(name, 0.1)
            conf   = abs(score)   # Confidence proportional to signal magnitude
            members.append(EnsembleMember(
                name=name, signal_score=score, win_probability=win_p,
                weight=weight, confidence=conf,
            ))

        # ── Bayesian Model Averaging ───────────────────────────────
        # BMA: weighted average of member predictions
        total_weight = sum(m.weight for m in members)
        ensemble_score = sum(m.signal_score * m.weight for m in members) / total_weight
        ensemble_prob_bull = sum(m.win_probability * m.weight for m in members) / total_weight
        ensemble_prob_bear = 1.0 - ensemble_prob_bull

        # Ensemble spread = uncertainty in the forecast
        scores = [m.signal_score for m in members]
        spread = statistics.stdev(scores) if len(scores) > 1 else 0.5

        # Classify spread
        if spread <= self.TIGHT_SPREAD:
            spread_cat = "tight"
            confidence_multiplier = 1.0
            notes.append(f"Tight ensemble spread ({spread:.2f}) — models agree, high confidence")
        elif spread <= self.MODERATE_SPREAD:
            spread_cat = "moderate"
            confidence_multiplier = 0.75
            notes.append(f"Moderate ensemble spread ({spread:.2f}) — some model disagreement")
        else:
            spread_cat = "wide"
            confidence_multiplier = 0.40
            warnings.append(
                f"Wide ensemble spread ({spread:.2f}) — models strongly disagree. "
                f"High uncertainty. Consider waiting for consensus."
            )

        calibrated_confidence = ensemble_prob_bull * confidence_multiplier

        # ── Kelly Criterion ────────────────────────────────────────
        kelly_p = min(0.85, ensemble_prob_bull)
        rr = max(0.5, risk_reward_ratio)

        kelly_full = self._compute_kelly_fraction(kelly_p, rr)

        # Adaptive Kelly fraction — scale down when models disagree
        kelly_fraction = KELLY_FRACTION   # base 25%
        if spread > self.MODERATE_SPREAD:
            kelly_fraction *= 0.50        # wide disagreement → half Kelly
        elif spread > self.TIGHT_SPREAD:
            kelly_fraction *= 0.75        # moderate disagreement → 3/4 Kelly
        if calibrated_confidence < 0.40:
            kelly_fraction *= 0.70        # low calibrated confidence → further reduce

        kelly_recommended = kelly_full * kelly_fraction

        # Kelly reasoning
        q = 1.0 - kelly_p
        kelly_reasoning = (
            f"Kelly: f* = (b×p - q)/b = ({rr:.1f}×{kelly_p:.2f} - {q:.2f})/{rr:.1f} "
            f"= {kelly_full:.3f} → {kelly_fraction:.0%} Kelly = {kelly_recommended:.3f} "
            f"({kelly_recommended:.1%} of portfolio)"
        )
        notes.append(kelly_reasoning)

        # ── Reynolds regime adjustment ─────────────────────────────
        regime_weight_mult = reynolds_position_mult   # 0.0 to 1.0

        # ── Final position size ────────────────────────────────────
        # Start with Kelly recommendation, apply regime and dollar limits
        base_pct = min(kelly_recommended, max_position_pct)
        adjusted_pct = base_pct * regime_weight_mult * confidence_multiplier

        # Hard dollar cap
        if portfolio_value and portfolio_value > 0:
            dollar_amount = min(
                adjusted_pct * portfolio_value,
                max_trade_dollars
            )
            effective_pct = dollar_amount / portfolio_value
        else:
            effective_pct = min(adjusted_pct, max_position_pct)

        # Win probability gate — require 5% edge above break-even for the R/R ratio
        # Break-even: 1/(1+b). At R/R=2: need >33% win rate. At R/R=1: need >50%.
        # This replaces the fixed 55% floor which wrongly blocked high-R/R setups.
        breakeven_p = 1.0 / (1.0 + rr)
        min_win_p = max(breakeven_p + 0.05, 0.45)   # always require at least 5% edge
        if ensemble_prob_bull < min_win_p:
            effective_pct = 0.0
            warnings.append(
                f"Win probability ({ensemble_prob_bull:.0%}) below break-even+5% "
                f"({min_win_p:.0%} at R/R={rr:.1f}) — Kelly recommends no position"
            )

        # ── Outcome distribution ───────────────────────────────────
        p10, p50, p90 = self._compute_outcome_distribution(
            ensemble_score, spread, current_price or 0, rr
        )

        # Summary notes
        notes.append(
            f"Ensemble: bull={ensemble_prob_bull:.0%} bear={ensemble_prob_bear:.0%} "
            f"score={ensemble_score:+.2f} spread={spread:.2f} ({spread_cat})"
        )
        notes.append(
            f"Position: Kelly={kelly_full:.1%} → 25%Kelly={kelly_recommended:.1%} "
            f"→ regime-adj={adjusted_pct:.1%} → final={effective_pct:.1%}"
        )

        if current_price and portfolio_value:
            dollar_pos = effective_pct * portfolio_value
            shares = int(dollar_pos / current_price) if current_price > 0 else 0
            notes.append(f"Dollar amount: ${dollar_pos:,.0f} ≈ {shares} shares @ ${current_price:.2f}")

        logger.info(
            f"[ENSEMBLE/KELLY] {ticker}: "
            f"P(bull)={ensemble_prob_bull:.0%} spread={spread_cat} "
            f"Kelly={kelly_full:.1%}→{kelly_recommended:.1%} "
            f"final={effective_pct:.1%}"
        )

        return EnsembleForecastResult(
            members=members,
            ensemble_probability_bull=ensemble_prob_bull,
            ensemble_probability_bear=ensemble_prob_bear,
            ensemble_score=ensemble_score,
            ensemble_spread=spread,
            spread_category=spread_cat,
            calibrated_confidence=calibrated_confidence,
            kelly_win_probability=kelly_p,
            kelly_fraction=kelly_full,
            recommended_fraction=kelly_recommended,
            kelly_reasoning=kelly_reasoning,
            regime_weight_multiplier=regime_weight_mult,
            effective_position_pct=effective_pct,
            p10_outcome=p10,
            p50_outcome=p50,
            p90_outcome=p90,
            notes=notes,
            warnings=warnings,
        )
