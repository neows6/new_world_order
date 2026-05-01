"""
decision/quantum_kalman.py — Quantum Probability State Vector + Kalman Filter.

═══════════════════════════════════════════════════════════════════════
QUANTUM PROBABILITY STATE VECTOR
═══════════════════════════════════════════════════════════════════════

From peer-reviewed research (Li Lin, arXiv 2401.05823, 2024):
  "Quantum probability is a mathematical extension of classical probability
   to complex numbers. The complex phase captures transitions between long
   and short decisions while considering information interaction among traders."

From Zhang & Huang quantum stock model (2010) and MDPI Mathematics 2024:
  |ψ⟩ = α|Bull⟩ + β|Bear⟩ + γ|Sideways⟩

  The PROBABILITY of each state = |amplitude|²:
    P(Bull)     = |α|²
    P(Bear)     = |β|²
    P(Sideways) = |γ|²

  Key insight: quantum amplitudes can INTERFERE. When bull and bear signals
  both exist simultaneously (as they always do in real markets), quantum
  interference can either:
    - CONSTRUCTIVELY boost one state (signals align)
    - DESTRUCTIVELY cancel (signals conflict — high uncertainty)

  This is qualitatively different from classical weighted averaging,
  which always gives a smooth interpolation and misses the interference
  patterns that create market anomalies.

  We compute interference term:
    I = 2 × Re(α* × β) = cross-term between bull and bear amplitudes
    I > 0: constructive (amplifies uncertainty — dangerous)
    I < 0: destructive (signals cancel — high conviction in one direction)

═══════════════════════════════════════════════════════════════════════
KALMAN FILTER PRICE STATE ESTIMATOR
═══════════════════════════════════════════════════════════════════════

From Apollo navigation computer (Schmidt, 1960) to Wall Street:
  The Kalman Filter is the mathematically optimal state estimator for
  linear systems with Gaussian noise — used on the Apollo moon missions,
  spacecraft trajectory analysis, and quantitative finance.

Two-step recursive algorithm:
  PREDICT: x̂(k|k-1) = F × x̂(k-1|k-1)     [project state forward]
           P(k|k-1) = F × P(k-1|k-1) × Fᵀ + Q  [project uncertainty]

  UPDATE:  K = P(k|k-1) × Hᵀ × (H × P(k|k-1) × Hᵀ + R)⁻¹  [Kalman gain]
           x̂(k|k) = x̂(k|k-1) + K × (z(k) - H × x̂(k|k-1))  [update]
           P(k|k)  = (I - K × H) × P(k|k-1)                  [update uncertainty]

  Where:
    x̂ = state estimate (our best guess of "true" price and trend)
    P  = error covariance matrix (uncertainty in our estimate)
    K  = Kalman gain (how much to trust new measurement vs. prediction)
    Q  = process noise (how much we expect the true state to jump)
    R  = measurement noise (how noisy our price observations are)

  Output: smoothed "true" price estimate + uncertainty bounds.
  Use for: clean trend detection, de-noised entry/exit signals.
═══════════════════════════════════════════════════════════════════════
"""

import math
import statistics
from dataclasses import dataclass
from typing import Optional
from loguru import logger


# ─────────────────────────────────────────────────────────────────────
# QUANTUM PROBABILITY STATE VECTOR
# ─────────────────────────────────────────────────────────────────────

@dataclass
class QuantumStateResult:
    """
    Market state as a quantum superposition of Bull/Bear/Sideways.

    Classical probability: P(Bull) + P(Bear) + P(Sideways) = 1.0
    Quantum probability: |α|² + |β|² + |γ|² = 1.0, but amplitudes
    can be complex and INTERFERE with each other.
    """
    # Probability amplitudes (complex numbers simplified to magnitude + phase)
    bull_amplitude:     float   # |α| — magnitude of bull state
    bear_amplitude:     float   # |β| — magnitude of bear state
    sideways_amplitude: float   # |γ| — magnitude of sideways state

    # Classical probabilities (|amplitude|²)
    p_bull:     float   # P(Bull) = |α|²
    p_bear:     float   # P(Bear) = |β|²
    p_sideways: float   # P(Sideways) = |γ|²

    # Quantum interference term
    interference: float         # Constructive(>0) or destructive(<0)
    interference_type: str      # "constructive", "destructive", "neutral"

    # Dominant state
    dominant_state: str         # "bull", "bear", "sideways"
    state_certainty: float      # 0.0 = maximally uncertain, 1.0 = certain

    # Uncertainty principle analog
    # (In QM: ΔxΔp ≥ ℏ/2 — you can't know position AND momentum precisely)
    # (In markets: precise price level AND precise timing cannot both be known)
    price_timing_uncertainty: float   # 0=low uncertainty, 1=high uncertainty

    # Wave function collapse trigger
    # (Observation collapses quantum state — trade execution collapses market state)
    collapse_triggered: bool    # True when state is certain enough to act

    signal: str                 # "strong_bull", "bull", "neutral", "bear", "strong_bear"
    notes: list


class QuantumStateAnalyzer:
    """
    Models market state as quantum superposition using amplitude formalism.

    This is NOT claiming actual quantum mechanics operates in markets.
    Rather, quantum PROBABILITY THEORY (Li Lin 2024) provides a richer
    mathematical framework that captures interference, entanglement, and
    uncertainty in ways classical probability cannot.

    Input signals are treated as "measurements" that partially collapse
    the wave function. Strong agreement across multiple signals = collapse
    to a definite state. Conflicting signals = sustained superposition.
    """

    # Uncertainty threshold — below this, state is "definite" enough to act
    COLLAPSE_CERTAINTY_THRESHOLD = 0.65

    def _normalize_to_amplitude(self, score: float) -> tuple:
        """
        Convert a score (-1.0 to +1.0) to quantum amplitude components.
        Positive score → bull amplitude
        Negative score → bear amplitude
        Near zero → sideways amplitude

        The "phase" represents the conviction direction. We use a simplified
        real-valued amplitude model (not full complex QM) which is appropriate
        for our application (Li Lin 2024 framework).
        """
        bull_amp     = max(0.0, score)
        bear_amp     = max(0.0, -score)
        sideways_amp = 1.0 - abs(score)
        return bull_amp, bear_amp, sideways_amp

    def _compute_interference(
        self,
        fundamental_score: float,
        technical_score: float,
        insider_score: float,
        news_score: float,
    ) -> float:
        """
        Compute quantum interference between signal pairs.

        Constructive interference (I > 0): signals amplify each other
          → higher uncertainty (both bull AND bear forces active)

        Destructive interference (I < 0): signals cancel each other
          → higher conviction (one direction dominates)

        Formula adapted from quantum amplitude cross-term:
          I = 2 × Σ(sign_i × sign_j × |amplitude_i| × |amplitude_j|)
              where i ≠ j

        In trading: signals in the SAME direction have negative interference
        (they reinforce, reducing uncertainty). Conflicting signals have
        positive interference (they fight, increasing uncertainty).
        """
        scores = [fundamental_score, technical_score, insider_score, news_score]

        interference = 0.0
        for i in range(len(scores)):
            for j in range(i+1, len(scores)):
                # Same sign = signals agree = DESTRUCTIVE interference (reduces uncertainty)
                # Opposite sign = signals disagree = CONSTRUCTIVE interference (increases uncertainty)
                if scores[i] * scores[j] > 0:
                    interference -= abs(scores[i]) * abs(scores[j]) * 0.5
                elif scores[i] * scores[j] < 0:
                    interference += abs(scores[i]) * abs(scores[j]) * 0.5

        return interference

    def compute(
        self,
        fundamental_score: float,    # From Layer 2
        technical_score: float,      # Fib + VWAP
        insider_score: float,        # Form 4
        news_quality_score: float,   # FUD filter output
        cycle_score: float,          # FFT phase
    ) -> QuantumStateResult:
        """
        Compute quantum superposition state from all input signals.

        Each input signal is a "partial measurement" of the market state.
        Strong, aligned signals collapse the wave function toward one state.
        Weak, conflicting signals sustain the superposition.
        """
        notes = []

        # Convert each signal to amplitude components
        signals = {
            "fundamental": fundamental_score,
            "technical":   technical_score,
            "insider":     insider_score,
            "news":        news_quality_score * 2 - 1,  # Convert 0-1 to -1 to +1
            "cycle":       cycle_score,
        }

        # Weighted amplitude aggregation
        weights = {
            "fundamental": 0.35,
            "technical":   0.20,
            "insider":     0.25,
            "news":        0.10,
            "cycle":       0.10,
        }

        # Aggregate amplitude components
        total_bull = sum(max(0, s) * weights[k] for k, s in signals.items())
        total_bear = sum(max(0, -s) * weights[k] for k, s in signals.items())
        total_side = sum((1 - abs(s)) * weights[k] for k, s in signals.items())

        # Normalize so |amplitudes|² sum to 1
        total_sq = total_bull**2 + total_bear**2 + total_side**2
        if total_sq == 0:
            total_sq = 1.0
        norm = math.sqrt(total_sq)

        bull_amp = total_bull / norm if norm > 0 else 0
        bear_amp = total_bear / norm if norm > 0 else 0
        side_amp = total_side / norm if norm > 0 else 0

        # Classical probabilities from Born rule: P = |amplitude|²
        p_bull = bull_amp ** 2
        p_bear = bear_amp ** 2
        p_side = side_amp ** 2

        # Re-normalize probabilities
        p_total = p_bull + p_bear + p_side
        if p_total > 0:
            p_bull /= p_total
            p_bear /= p_total
            p_side /= p_total

        # Quantum interference
        interference = self._compute_interference(
            fundamental_score, technical_score, insider_score,
            news_quality_score * 2 - 1
        )

        if interference > 0.1:
            int_type = "constructive"
            notes.append(
                f"Constructive interference ({interference:.2f}): signals conflict — "
                f"HIGH uncertainty, wait for resolution"
            )
        elif interference < -0.1:
            int_type = "destructive"
            notes.append(
                f"Destructive interference ({interference:.2f}): signals align — "
                f"state collapsing toward conviction"
            )
        else:
            int_type = "neutral"

        # Dominant state
        probs = {"bull": p_bull, "bear": p_bear, "sideways": p_side}
        dominant = max(probs, key=probs.get)
        dominant_prob = probs[dominant]

        # State certainty: how concentrated the probability is
        # Maximum entropy = log(3) ≈ 1.099, minimum entropy = 0
        entropy = -sum(p * math.log(p + 1e-10) for p in [p_bull, p_bear, p_side])
        max_entropy = math.log(3)
        state_certainty = 1.0 - (entropy / max_entropy)

        # Uncertainty principle analog
        # High certainty about state → high uncertainty about timing (and vice versa)
        price_timing_uncertainty = 1.0 - state_certainty

        # Wave function collapse
        collapse = state_certainty >= self.COLLAPSE_CERTAINTY_THRESHOLD

        # Signal from quantum state
        if dominant == "bull" and dominant_prob > 0.60:
            signal = "strong_bull"
        elif dominant == "bull" and dominant_prob > 0.45:
            signal = "bull"
        elif dominant == "bear" and dominant_prob > 0.60:
            signal = "strong_bear"
        elif dominant == "bear" and dominant_prob > 0.45:
            signal = "bear"
        else:
            signal = "neutral"

        notes.append(
            f"State probabilities: Bull={p_bull:.0%} Bear={p_bear:.0%} Side={p_side:.0%}"
        )
        notes.append(f"State certainty: {state_certainty:.0%} | Wave function collapsed: {collapse}")
        if not collapse:
            notes.append("Superposition active — market state ambiguous. Consider smaller position or wait.")

        logger.debug(
            f"[QUANTUM] {dominant}={dominant_prob:.0%} certainty={state_certainty:.0%} "
            f"interference={int_type} signal={signal}"
        )

        return QuantumStateResult(
            bull_amplitude=bull_amp, bear_amplitude=bear_amp, sideways_amplitude=side_amp,
            p_bull=p_bull, p_bear=p_bear, p_sideways=p_side,
            interference=interference, interference_type=int_type,
            dominant_state=dominant, state_certainty=state_certainty,
            price_timing_uncertainty=price_timing_uncertainty,
            collapse_triggered=collapse,
            signal=signal,
            notes=notes,
        )


# ─────────────────────────────────────────────────────────────────────
# KALMAN FILTER PRICE STATE ESTIMATOR
# ─────────────────────────────────────────────────────────────────────

@dataclass
class KalmanResult:
    """
    Kalman Filter output — optimal state estimate from noisy price observations.
    """
    # State estimates
    filtered_price: float       # Kalman-estimated "true" price
    filtered_trend:  float      # Kalman-estimated trend (price velocity)

    # Uncertainty
    price_uncertainty: float    # ±1 sigma confidence interval on price
    trend_uncertainty: float    # ±1 sigma confidence interval on trend

    # Innovation (prediction error)
    innovation: float           # Actual - predicted (how surprised the filter was)
    innovation_normalized: float  # Innovation / std_dev — how unusual is this?

    # Kalman gain (how much the filter trusted the new observation)
    kalman_gain: float          # 0=trust prediction, 1=trust measurement

    # Trend signals
    trend_direction: str        # "up", "down", "flat"
    is_accelerating: bool       # Is the trend getting stronger?
    is_decelerating: bool       # Is the trend weakening?

    # Signal
    signal_strength: float      # 0.0 to 1.0
    signal: str                 # "bullish", "bearish", "neutral"
    notes: list


class KalmanPriceFilter:
    """
    Kalman Filter for optimal price state estimation.

    The Kalman filter treats price as a noisy measurement of an
    underlying "true" state (trend + level). It recursively updates
    its estimate as new prices arrive, giving optimal weight to both
    prior estimates and new observations based on their relative noise levels.

    Process model: price follows a linear trend with Gaussian noise
      x(k) = [price, trend]ᵀ
      x(k) = F × x(k-1) + w   where w ~ N(0, Q)

    Observation model: we observe price with noise
      z(k) = H × x(k) + v     where v ~ N(0, R)

    This is the same algorithm used in:
      - Apollo moon mission navigation
      - GPS systems
      - Autonomous vehicle tracking
      - Wall Street quantitative trading desks
    """

    def __init__(self):
        # State transition matrix F — assumes price evolves as: price += trend × dt
        # F = [[1, 1], [0, 1]]  (constant trend model)
        self.F = [[1.0, 1.0], [0.0, 1.0]]

        # Observation matrix H — we observe price only (not trend directly)
        # H = [1, 0]
        self.H = [1.0, 0.0]

        # Process noise Q — how much the true state can jump each step
        # Higher Q = faster adaptation to new trends, less smooth
        self.Q = [[0.0001, 0.0], [0.0, 0.00001]]

        # Measurement noise R — how noisy our price observations are
        # Higher R = trust the model more, trust measurements less
        self.R = 0.01

    def _mat_mult_2x2_2x2(self, A, B):
        """2×2 matrix multiplication."""
        return [
            [A[0][0]*B[0][0] + A[0][1]*B[1][0], A[0][0]*B[0][1] + A[0][1]*B[1][1]],
            [A[1][0]*B[0][0] + A[1][1]*B[1][0], A[1][0]*B[0][1] + A[1][1]*B[1][1]],
        ]

    def _mat_add_2x2(self, A, B):
        return [[A[i][j]+B[i][j] for j in range(2)] for i in range(2)]

    def _mat_scale_2x2(self, A, s):
        return [[A[i][j]*s for j in range(2)] for i in range(2)]

    def filter(self, prices: list, noise_scale: Optional[float] = None) -> KalmanResult:
        """
        Run Kalman filter over price series.
        Returns final state estimate with uncertainty bounds.
        """
        notes = []
        n = len(prices)

        if n < 5:
            p = prices[-1] if prices else 0.0
            return KalmanResult(
                filtered_price=p, filtered_trend=0.0,
                price_uncertainty=p * 0.05, trend_uncertainty=0.01,
                innovation=0.0, innovation_normalized=0.0, kalman_gain=0.5,
                trend_direction="flat", is_accelerating=False, is_decelerating=False,
                signal_strength=0.0, signal="neutral",
                notes=["Insufficient data for Kalman filtering"],
            )

        # Adjust R based on market noise
        if noise_scale is not None:
            R = max(0.001, self.R * noise_scale)
        else:
            # Estimate measurement noise from price variance
            returns = [abs(prices[i]-prices[i-1])/prices[i-1] for i in range(1, min(20, n)) if prices[i-1] > 0]
            vol = statistics.stdev(returns) if len(returns) > 1 else 0.01
            R = max(0.001, vol ** 2)

        # Initialize state: [first_price, initial_trend=0]
        x = [prices[0], 0.0]  # State vector [price, trend]
        P = [[1.0, 0.0], [0.0, 1.0]]  # Error covariance matrix

        innovations = []
        gains = []
        filtered_prices = []
        filtered_trends  = []

        for price in prices:
            # ── PREDICT step ──────────────────────────────
            # x̂(k|k-1) = F × x̂(k-1|k-1)
            x_pred = [
                self.F[0][0] * x[0] + self.F[0][1] * x[1],
                self.F[1][0] * x[0] + self.F[1][1] * x[1],
            ]

            # P(k|k-1) = F × P × Fᵀ + Q
            FP = self._mat_mult_2x2_2x2(self.F, P)
            Ft = [[self.F[0][0], self.F[1][0]], [self.F[0][1], self.F[1][1]]]
            P_pred = self._mat_add_2x2(self._mat_mult_2x2_2x2(FP, Ft), self.Q)

            # ── UPDATE step ───────────────────────────────
            # Innovation: y = z - H × x̂(k|k-1)
            y = price - (self.H[0] * x_pred[0] + self.H[1] * x_pred[1])
            innovations.append(y)

            # Innovation covariance: S = H × P(k|k-1) × Hᵀ + R
            HP = [self.H[0]*P_pred[0][0] + self.H[1]*P_pred[1][0],
                  self.H[0]*P_pred[0][1] + self.H[1]*P_pred[1][1]]
            S = HP[0] * self.H[0] + HP[1] * self.H[1] + R

            # Kalman gain: K = P(k|k-1) × Hᵀ / S
            K = [P_pred[0][0] * self.H[0] / S + P_pred[0][1] * self.H[1] / S,
                 P_pred[1][0] * self.H[0] / S + P_pred[1][1] * self.H[1] / S]
            gains.append(K[0])

            # State update: x̂(k|k) = x̂(k|k-1) + K × y
            x = [x_pred[0] + K[0] * y,
                 x_pred[1] + K[1] * y]

            # Covariance update: P(k|k) = (I - K × H) × P(k|k-1)
            KH = [[K[0]*self.H[0], K[0]*self.H[1]],
                  [K[1]*self.H[0], K[1]*self.H[1]]]
            I_KH = [[1-KH[0][0], -KH[0][1]], [-KH[1][0], 1-KH[1][1]]]
            P = self._mat_mult_2x2_2x2(I_KH, P_pred)

            filtered_prices.append(x[0])
            filtered_trends.append(x[1])

        # Final state
        final_price = filtered_prices[-1]
        final_trend = filtered_trends[-1]
        price_uncertainty = math.sqrt(max(0, P[0][0]))
        trend_uncertainty = math.sqrt(max(0, P[1][1]))

        # Innovation analysis
        final_innovation = innovations[-1]
        if len(innovations) > 5:
            inn_std = statistics.stdev(innovations[-20:]) if len(innovations) >= 20 else statistics.stdev(innovations)
            innovation_normalized = final_innovation / inn_std if inn_std > 0 else 0
        else:
            innovation_normalized = 0.0

        # Trend analysis
        if len(filtered_trends) >= 5:
            recent_trend   = statistics.mean(filtered_trends[-3:])
            earlier_trend  = statistics.mean(filtered_trends[-8:-3]) if len(filtered_trends) >= 8 else filtered_trends[0]
            is_accel = recent_trend > earlier_trend * 1.1
            is_decel = recent_trend < earlier_trend * 0.9
        else:
            is_accel = is_decel = False

        # Trend direction
        trend_pct = final_trend / final_price if final_price > 0 else 0
        if trend_pct > 0.001:
            trend_dir = "up"
        elif trend_pct < -0.001:
            trend_dir = "down"
        else:
            trend_dir = "flat"

        # Signal
        avg_gain = statistics.mean(gains[-10:]) if len(gains) >= 10 else 0.5
        sig_strength = min(1.0, abs(trend_pct) / 0.01)  # Normalize to daily 1% move

        if trend_dir == "up" and not is_decel:
            signal = "bullish"
        elif trend_dir == "down" and not is_decel:
            signal = "bearish"
        else:
            signal = "neutral"

        notes.append(
            f"Kalman state: price=${final_price:.2f}±{price_uncertainty:.2f}, "
            f"trend={trend_pct:.3%}/day"
        )
        notes.append(f"Innovation: {final_innovation:.3f} ({innovation_normalized:.1f}σ) | Gain: {avg_gain:.2f}")
        if abs(innovation_normalized) > 2.0:
            notes.append(f"⚠ Large innovation ({innovation_normalized:.1f}σ) — unusual price move detected")

        logger.debug(
            f"[KALMAN] price={final_price:.2f} trend={trend_pct:.3%} "
            f"dir={trend_dir} signal={signal}"
        )

        return KalmanResult(
            filtered_price=final_price,
            filtered_trend=final_trend,
            price_uncertainty=price_uncertainty,
            trend_uncertainty=trend_uncertainty,
            innovation=final_innovation,
            innovation_normalized=innovation_normalized,
            kalman_gain=avg_gain,
            trend_direction=trend_dir,
            is_accelerating=is_accel,
            is_decelerating=is_decel,
            signal_strength=sig_strength,
            signal=signal,
            notes=notes,
        )
