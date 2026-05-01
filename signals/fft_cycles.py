"""
signals/fft_cycles.py — Fast Fourier Transform Cycle Detector.

What FFT does in trading:
  Decomposes a price series into constituent frequency components,
  revealing dominant market cycles (e.g. 20-day, 40-day, 63-day cycles).
  We use inverse FFT to reconstruct a de-noised price signal and detect
  when that signal is at a cycle trough (buy zone) or cycle peak (sell zone).

Research-honest note:
  A 2026 peer-reviewed study (MDPI Algorithms) found that even sophisticated
  wavelet-transformer models failed to outperform naive baselines at 1-day
  prediction. FFT is most useful as a NOISE FILTER and CYCLE DETECTOR,
  NOT as a pure price predictor. We use it to:
    1. Identify dominant cycle lengths (where to watch for turns)
    2. De-noise price for cleaner crossover signals
    3. Flag when we're near a cycle turning point as a CONFIRMATION signal
       — never as a standalone buy/sell trigger.

Best cycles found in academic research (Stádník et al.):
  20-day, 40-day, 63-day (quarter), 126-day (half-year) cycles
  are most persistent across US stocks.
"""

import numpy as np
from dataclasses import dataclass
from typing import Optional
from loguru import logger


# Academically validated dominant cycle periods (days)
# Source: Stádník et al. FFT US stocks backtesting
KNOWN_MARKET_CYCLES = {
    "weekly":      5,
    "monthly":    20,
    "bimonthly":  40,
    "quarterly":  63,
    "semiannual": 126,
    "annual":     252,
}


@dataclass
class FFTResult:
    ticker: str
    dominant_cycles: list           # List of (period_days, power) sorted by power
    denoised_prices: list           # Inverse FFT reconstructed smooth price
    current_cycle_phase: str        # "trough", "rising", "peak", "falling"
    phase_score: float              # -1.0 (peak/sell) to +1.0 (trough/buy)
    nearest_known_cycle: int        # Which known cycle dominates (days)
    days_to_next_turn: Optional[int]  # Estimated days to next cycle inflection
    signal_strength: float          # 0.0 to 1.0 — how clear the cycle is
    n_observations: int
    warnings: list


class FFTCycleDetector:
    """
    Detects dominant price cycles using Fast Fourier Transform.
    Used as a CONFIRMATION signal — tells you WHERE in the cycle you are,
    not WHETHER to trade. Combined with fundamentals + FUD filter for entries.
    """

    # Top N frequency components to keep when de-noising
    # More components = less smoothing. 10 keeps major cycles, kills noise.
    N_COMPONENTS_KEEP = 10

    # Minimum observations for reliable FFT
    MIN_OBSERVATIONS = 60           # 3 months of daily data minimum
    IDEAL_OBSERVATIONS = 252        # 1 year is ideal

    # Phase score thresholds
    TROUGH_THRESHOLD  = -0.6        # De-noised price near cycle low
    PEAK_THRESHOLD    =  0.6        # De-noised price near cycle high

    def _compute_fft(self, prices: list) -> tuple:
        """
        Core FFT computation.
        Returns (fft_result, frequencies, magnitudes, periods)
        """
        n = len(prices)
        prices_arr = np.array(prices, dtype=float)

        # De-trend before FFT — removes the upward drift that would dominate
        # the frequency spectrum and mask the cycles we care about
        trend = np.linspace(prices_arr[0], prices_arr[-1], n)
        detrended = prices_arr - trend

        # Apply FFT
        fft_result = np.fft.fft(detrended)
        frequencies = np.fft.fftfreq(n, d=1)   # d=1 day
        magnitudes = np.abs(fft_result)

        # Convert frequencies to periods (days per cycle)
        # Only look at positive frequencies (meaningful half of spectrum)
        pos_mask = frequencies > 0
        pos_freq = frequencies[pos_mask]
        pos_mag  = magnitudes[pos_mask]
        periods  = 1.0 / pos_freq     # Days per cycle

        return fft_result, frequencies, pos_mag, periods, detrended, trend

    def _find_dominant_cycles(self, periods: np.ndarray, magnitudes: np.ndarray, n: int) -> list:
        """
        Find top N dominant cycles by magnitude (spectral power).
        Filters to reasonable period range: 5-252 days.
        """
        # Filter to meaningful period range
        mask = (periods >= 5) & (periods <= n // 2)
        filtered_periods = periods[mask]
        filtered_mags    = magnitudes[mask]

        if len(filtered_periods) == 0:
            return []

        # Sort by magnitude descending
        sort_idx = np.argsort(filtered_mags)[::-1]
        top_periods = filtered_periods[sort_idx][:10]
        top_mags    = filtered_mags[sort_idx][:10]

        # Normalize power to 0-1
        max_mag = top_mags[0] if top_mags[0] > 0 else 1.0
        result = [(float(top_periods[i]), float(top_mags[i] / max_mag))
                  for i in range(len(top_periods))]

        return result

    def _denoise_prices(
        self,
        fft_result: np.ndarray,
        magnitudes: np.ndarray,
        n_keep: int,
        trend: np.ndarray,
        n: int,
    ) -> np.ndarray:
        """
        Reconstruct price series keeping only top N frequency components.
        This removes high-frequency noise while preserving dominant cycles.
        """
        # Zero out all but top N components by magnitude
        threshold = np.sort(magnitudes)[::-1][min(n_keep, len(magnitudes) - 1)]
        filtered = fft_result.copy()
        filtered[magnitudes < threshold] = 0

        # Inverse FFT to reconstruct de-noised, de-trended signal
        denoised_detrended = np.real(np.fft.ifft(filtered))

        # Add trend back
        return denoised_detrended + trend

    def _compute_phase(self, denoised: np.ndarray) -> tuple:
        """
        Determine where we are in the dominant cycle.
        Uses the normalized position of current price relative to
        recent cycle high and low.

        Returns (phase_label, phase_score, days_to_turn)
        """
        if len(denoised) < 20:
            return "unknown", 0.0, None

        # Use last 126 days (half-year) to define cycle range
        lookback = min(126, len(denoised))
        recent = denoised[-lookback:]

        cycle_min = np.min(recent)
        cycle_max = np.max(recent)
        current   = denoised[-1]

        if cycle_max == cycle_min:
            return "flat", 0.0, None

        # Normalized position: -1 = at cycle min, +1 = at cycle max
        # We INVERT this so that -1 = bad (peak/sell), +1 = good (trough/buy)
        raw_position = (current - cycle_min) / (cycle_max - cycle_min)
        phase_score  = 1.0 - (2.0 * raw_position)  # Maps [0,1] → [1,-1]

        # Detect momentum direction in de-noised signal
        if len(denoised) >= 5:
            recent_slope = np.mean(np.diff(denoised[-5:]))
        else:
            recent_slope = 0.0

        # Phase classification
        if phase_score >= self.TROUGH_THRESHOLD and recent_slope > 0:
            phase = "trough_recovering"    # Best buy zone
        elif phase_score >= 0 and recent_slope > 0:
            phase = "rising"               # Upswing, still OK to buy
        elif phase_score >= 0 and recent_slope <= 0:
            phase = "peak_forming"         # Getting toppy
        elif phase_score < 0 and recent_slope <= 0:
            phase = "falling"              # Downswing, avoid
        elif phase_score < self.PEAK_THRESHOLD and recent_slope > 0:
            phase = "peak_recovering"      # Bounce from extended sell
        else:
            phase = "neutral"

        # Estimate days to next inflection (zero-crossing of slope)
        days_to_turn = None
        if len(denoised) >= 10:
            slopes = np.diff(denoised[-10:])
            # Count consecutive same-direction bars to estimate turn
            same_dir = 0
            last_slope_sign = np.sign(slopes[-1])
            for s in reversed(slopes):
                if np.sign(s) == last_slope_sign:
                    same_dir += 1
                else:
                    break
            # Rough estimate: momentum exhausts around the dominant cycle length
            days_to_turn = max(1, 10 - same_dir)   # Simplistic but usable

        return phase, phase_score, days_to_turn

    def _find_nearest_cycle(self, dominant_period: float) -> int:
        """Map the top dominant period to nearest known market cycle."""
        known = list(KNOWN_MARKET_CYCLES.values())
        nearest = min(known, key=lambda x: abs(x - dominant_period))
        return nearest

    def _compute_signal_strength(self, magnitudes: np.ndarray, n: int) -> float:
        """
        Signal strength = how much of the total spectral power is concentrated
        in the top 3 components. High concentration = cleaner cycles.
        """
        if len(magnitudes) < 3:
            return 0.0
        sorted_mags = np.sort(magnitudes)[::-1]
        top3_power  = np.sum(sorted_mags[:3] ** 2)
        total_power = np.sum(sorted_mags ** 2)
        if total_power == 0:
            return 0.0
        return float(np.sqrt(top3_power / total_power))   # 0.0 to 1.0

    def analyze(self, ticker: str, prices: list) -> FFTResult:
        """
        Main entry point. Takes list of closing prices (oldest first).
        Returns FFTResult with cycle analysis and phase score.
        """
        warnings = []
        n = len(prices)

        if n < self.MIN_OBSERVATIONS:
            warnings.append(f"Only {n} observations — FFT needs {self.MIN_OBSERVATIONS}+ for reliability")
            return FFTResult(
                ticker=ticker, dominant_cycles=[], denoised_prices=prices,
                current_cycle_phase="insufficient_data", phase_score=0.0,
                nearest_known_cycle=20, days_to_next_turn=None,
                signal_strength=0.0, n_observations=n, warnings=warnings,
            )

        if n < self.IDEAL_OBSERVATIONS:
            warnings.append(f"Only {n} days — 252 days gives highest cycle reliability")

        # Core computation
        fft_result, frequencies, magnitudes, periods, detrended, trend = self._compute_fft(prices)

        dominant_cycles   = self._find_dominant_cycles(periods, magnitudes, n)
        denoised          = self._denoise_prices(fft_result, np.abs(fft_result), self.N_COMPONENTS_KEEP, trend, n)
        phase, phase_score, days_to_turn = self._compute_phase(denoised)
        signal_strength   = self._compute_signal_strength(magnitudes, n)

        top_period = dominant_cycles[0][0] if dominant_cycles else 20.0
        nearest_cycle = self._find_nearest_cycle(top_period)

        logger.debug(
            f"[FFT] {ticker}: phase={phase}, score={phase_score:.2f}, "
            f"dominant_cycle={top_period:.0f}d, strength={signal_strength:.2f}"
        )

        return FFTResult(
            ticker=ticker,
            dominant_cycles=dominant_cycles[:5],   # Top 5 cycles
            denoised_prices=list(denoised),
            current_cycle_phase=phase,
            phase_score=phase_score,
            nearest_known_cycle=nearest_cycle,
            days_to_next_turn=days_to_turn,
            signal_strength=signal_strength,
            n_observations=n,
            warnings=warnings,
        )

    def is_buy_zone(self, result: FFTResult, min_strength: float = 0.3) -> bool:
        """
        True when FFT suggests we're near a cycle trough.
        Only use as CONFIRMATION — not standalone signal.
        """
        if result.signal_strength < min_strength:
            return False   # Cycle too noisy to trust
        return (
            result.phase_score >= 0.3 and
            result.current_cycle_phase in ("trough_recovering", "rising")
        )

    def is_sell_zone(self, result: FFTResult, min_strength: float = 0.3) -> bool:
        """True when FFT suggests we're near a cycle peak."""
        if result.signal_strength < min_strength:
            return False
        return (
            result.phase_score <= -0.3 and
            result.current_cycle_phase in ("peak_forming", "falling")
        )
