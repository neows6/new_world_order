"""
signals/market_microstructure.py — VWAP, VIX Regime, and Volume Profile.

Three institutional signals in one module:

1. VWAP (Volume Weighted Average Price)
   The #1 benchmark used by institutional algorithms for execution.
   Institutions buy below VWAP, sell above it. When price is consistently
   above VWAP on rising volume = institutional accumulation (bullish).
   Key signals: price vs VWAP, VWAP slope, distance from VWAP.

2. VIX Regime Detector
   VIX < 15  = Low fear, favorable for long positions, full position sizing
   VIX 15-25 = Normal volatility, standard position sizing
   VIX 25-35 = Elevated fear, reduce position sizing by 50%
   VIX > 35  = Crisis, defensive mode, no new longs, hold cash

3. Volume Profile (simplified)
   Identifies price levels with highest trading volume (Point of Control).
   High volume nodes = strong support/resistance.
   Low volume nodes = price moves quickly through them.
   Used to: find logical entry/exit zones and validate Fibonacci levels.
"""

import statistics
from dataclasses import dataclass
from typing import Optional
from loguru import logger


# VIX Regime thresholds
VIX_CALM        = 15.0   # Bull market, low fear
VIX_NORMAL      = 25.0   # Normal volatility
VIX_STRESSED    = 35.0   # Stressed market, reduce risk
VIX_CRISIS      = 45.0   # Crisis, maximum caution


# ─────────────────────────────────────────────────────────────
# VIX Regime Detector
# ─────────────────────────────────────────────────────────────

@dataclass
class VIXRegime:
    vix_level: float
    regime: str              # "calm", "normal", "stressed", "crisis"
    position_size_multiplier: float   # 0.0 to 1.0 — scale all trades by this
    allow_new_longs: bool
    allow_new_shorts: bool
    description: str
    action: str              # What to do in this regime


class VIXRegimeDetector:
    """
    Determines market regime from VIX level.
    All trade sizes should be multiplied by position_size_multiplier
    before execution. This is a portfolio-level gate, not a stock-level one.

    VIX data: fetch via Schwab API or free CBOE data.
    Symbol: ^VIX (Yahoo Finance) or VIX (CBOE streaming)
    """

    def classify(self, vix_level: float) -> VIXRegime:
        """Classify VIX level into a trading regime."""

        if vix_level < VIX_CALM:
            return VIXRegime(
                vix_level=vix_level,
                regime="calm",
                position_size_multiplier=1.0,
                allow_new_longs=True,
                allow_new_shorts=True,
                description=f"VIX {vix_level:.1f} — Low fear, favorable conditions",
                action="Full position sizing. All strategies active.",
            )
        elif vix_level < VIX_NORMAL:
            return VIXRegime(
                vix_level=vix_level,
                regime="normal",
                position_size_multiplier=0.85,
                allow_new_longs=True,
                allow_new_shorts=True,
                description=f"VIX {vix_level:.1f} — Normal volatility",
                action="Standard position sizing. Proceed with normal signals.",
            )
        elif vix_level < VIX_STRESSED:
            return VIXRegime(
                vix_level=vix_level,
                regime="stressed",
                position_size_multiplier=0.50,
                allow_new_longs=True,
                allow_new_shorts=True,
                description=f"VIX {vix_level:.1f} — Elevated fear, increased volatility",
                action="Reduce all position sizes by 50%. Widen stops. Higher quality bar.",
            )
        elif vix_level < VIX_CRISIS:
            return VIXRegime(
                vix_level=vix_level,
                regime="crisis",
                position_size_multiplier=0.20,
                allow_new_longs=False,
                allow_new_shorts=True,
                description=f"VIX {vix_level:.1f} — Crisis conditions",
                action="DEFENSIVE MODE. No new longs. Protect capital. Reduce to 20% sizing.",
            )
        else:
            return VIXRegime(
                vix_level=vix_level,
                regime="extreme_crisis",
                position_size_multiplier=0.0,
                allow_new_longs=False,
                allow_new_shorts=False,
                description=f"VIX {vix_level:.1f} — Extreme fear / market dislocation",
                action="HALT all trading. Hold cash. Wait for VIX to fall below 35.",
            )

    def get_vix_from_schwab(self, schwab_client) -> Optional[float]:
        """
        Fetch current VIX level from Schwab market data.
        VIX trades as $VIX.X on Schwab.
        """
        try:
            resp = schwab_client.get_quote("$VIX.X")
            resp.raise_for_status()
            data = resp.json()
            vix = data.get("$VIX.X", {}).get("quote", {}).get("lastPrice")
            if vix:
                logger.info(f"VIX: {vix:.2f} — regime: {self.classify(vix).regime}")
            return vix
        except Exception as e:
            logger.warning(f"Could not fetch VIX: {e} — using neutral regime")
            return None   # Caller should use VIX_NORMAL as fallback

    def get_regime_for_position_sizing(
        self, vix: Optional[float], base_position_pct: float
    ) -> float:
        """
        Apply VIX regime multiplier to a base position size.
        base_position_pct: e.g. 0.05 for 5% of portfolio
        Returns: adjusted position size (e.g. 0.025 in stressed regime)
        """
        if vix is None:
            return base_position_pct * 0.85   # Conservative default
        regime = self.classify(vix)
        adjusted = base_position_pct * regime.position_size_multiplier
        logger.debug(
            f"Position size adjusted: {base_position_pct:.1%} → {adjusted:.1%} "
            f"(VIX {vix:.1f}, regime={regime.regime})"
        )
        return adjusted


# ─────────────────────────────────────────────────────────────
# VWAP Calculator
# ─────────────────────────────────────────────────────────────

@dataclass
class VWAPResult:
    ticker: str
    vwap: float              # Current VWAP
    current_price: float
    price_vs_vwap_pct: float   # How far price is from VWAP (signed)
    position: str            # "above", "below", "at"

    # Standard deviation bands (like Bollinger Bands around VWAP)
    vwap_std: float
    upper_band_1: float      # VWAP + 1 std
    lower_band_1: float      # VWAP - 1 std
    upper_band_2: float      # VWAP + 2 std
    lower_band_2: float      # VWAP - 2 std

    # Institutional signals
    is_extended_above: bool  # Price > VWAP + 2std = extended, potential sell
    is_extended_below: bool  # Price < VWAP - 2std = oversold, potential buy
    vwap_slope: str          # "rising", "flat", "falling"
    institutional_bias: str  # "accumulation", "distribution", "neutral"

    # Anchored VWAP (from recent significant event)
    anchored_vwap: Optional[float]      # VWAP since significant swing low/high
    price_vs_anchored_pct: Optional[float]

    notes: list


class VWAPCalculator:
    """
    Computes VWAP and standard deviation bands from intraday or daily OHLCV.
    
    Intraday VWAP: resets each day, computed from intraday bars
    Daily VWAP: uses daily OHLCV, shows multi-day fair value
    Anchored VWAP: starts from a specific event (earnings, breakout)
    """

    def _typical_price(self, high: float, low: float, close: float) -> float:
        """Typical price = (High + Low + Close) / 3 — standard VWAP input."""
        return (high + low + close) / 3.0

    def compute_daily(
        self,
        ticker: str,
        highs: list,
        lows: list,
        closes: list,
        volumes: list,
        anchor_idx: Optional[int] = None,   # Compute anchored VWAP from this index
    ) -> VWAPResult:
        """
        Compute VWAP using daily OHLCV bars.
        Useful for swing trades — shows multi-day institutional average cost.
        anchor_idx: index in the list to start anchored VWAP from (e.g., recent earnings)
        """
        n = len(closes)
        if n == 0:
            raise ValueError("Empty price data")

        typical_prices = [self._typical_price(highs[i], lows[i], closes[i]) for i in range(n)]

        # Standard cumulative VWAP
        cumulative_tpv = 0.0  # typical price * volume
        cumulative_vol = 0.0

        # For std calculation
        sq_sum = 0.0

        for i in range(n):
            tpv = typical_prices[i] * volumes[i]
            cumulative_tpv += tpv
            cumulative_vol += volumes[i]
            sq_sum += (typical_prices[i] ** 2) * volumes[i]

        if cumulative_vol == 0:
            vwap = closes[-1]
            vwap_std = 0.0
        else:
            vwap = cumulative_tpv / cumulative_vol
            # Volume-weighted variance
            variance = (sq_sum / cumulative_vol) - (vwap ** 2)
            vwap_std = max(0.0, variance) ** 0.5

        # Anchored VWAP
        anchored_vwap = None
        price_vs_anchored = None
        if anchor_idx is not None and 0 <= anchor_idx < n:
            anc_tpv = sum(typical_prices[i] * volumes[i] for i in range(anchor_idx, n))
            anc_vol = sum(volumes[i] for i in range(anchor_idx, n))
            if anc_vol > 0:
                anchored_vwap = anc_tpv / anc_vol
                price_vs_anchored = (closes[-1] - anchored_vwap) / anchored_vwap

        current = closes[-1]

        # VWAP bands
        upper_1 = vwap + vwap_std
        lower_1 = vwap - vwap_std
        upper_2 = vwap + 2 * vwap_std
        lower_2 = vwap - 2 * vwap_std

        # Price position
        price_vs_vwap = (current - vwap) / vwap if vwap > 0 else 0
        if abs(price_vs_vwap) < 0.005:
            position = "at"
        elif current > vwap:
            position = "above"
        else:
            position = "below"

        # Extended detection
        is_extended_above = current > upper_2
        is_extended_below = current < lower_2

        # Slope: compare current VWAP to VWAP 5 days ago
        vwap_slope = "flat"
        if n >= 10:
            # Recompute VWAP for n-5 period
            past_tpv = sum(typical_prices[i] * volumes[i] for i in range(n - 5))
            past_vol = sum(volumes[i] for i in range(n - 5))
            if past_vol > 0:
                past_vwap = past_tpv / past_vol
                slope_pct = (vwap - past_vwap) / past_vwap
                if slope_pct > 0.01:
                    vwap_slope = "rising"
                elif slope_pct < -0.01:
                    vwap_slope = "falling"

        # Institutional bias
        if current > vwap and vwap_slope == "rising":
            institutional_bias = "accumulation"
        elif current < vwap and vwap_slope == "falling":
            institutional_bias = "distribution"
        else:
            institutional_bias = "neutral"

        notes = []
        if is_extended_above:
            notes.append(f"Price {price_vs_vwap:.1%} above VWAP — extended, avoid chasing")
        elif is_extended_below:
            notes.append(f"Price {abs(price_vs_vwap):.1%} below VWAP — potential mean reversion buy")
        if anchored_vwap:
            notes.append(f"Anchored VWAP: ${anchored_vwap:.2f} (price {price_vs_anchored:.1%} from it)")

        logger.debug(
            f"[VWAP] {ticker}: ${vwap:.2f}, price={current:.2f}, "
            f"pos={position}, slope={vwap_slope}, bias={institutional_bias}"
        )

        return VWAPResult(
            ticker=ticker,
            vwap=vwap,
            current_price=current,
            price_vs_vwap_pct=price_vs_vwap,
            position=position,
            vwap_std=vwap_std,
            upper_band_1=upper_1,
            lower_band_1=lower_1,
            upper_band_2=upper_2,
            lower_band_2=lower_2,
            is_extended_above=is_extended_above,
            is_extended_below=is_extended_below,
            vwap_slope=vwap_slope,
            institutional_bias=institutional_bias,
            anchored_vwap=anchored_vwap,
            price_vs_anchored_pct=price_vs_anchored,
            notes=notes,
        )


# ─────────────────────────────────────────────────────────────
# Volume Profile
# ─────────────────────────────────────────────────────────────

@dataclass
class VolumeProfileLevel:
    price: float
    volume: float
    pct_of_total: float
    is_high_volume_node: bool    # HVN — strong support/resistance
    is_low_volume_node: bool     # LVN — price moves quickly through


@dataclass
class VolumeProfileResult:
    ticker: str
    point_of_control: float      # Price with highest volume (POC)
    value_area_high: float       # Top of 70% volume zone
    value_area_low: float        # Bottom of 70% volume zone
    current_price: float
    price_in_value_area: bool    # True = price accepted in fair value zone
    levels: list                 # VolumeProfileLevel objects
    nearest_hvn_below: Optional[float]   # Support
    nearest_hvn_above: Optional[float]   # Resistance
    rvol: float = 1.0                    # Relative volume vs 20-day avg
    notes: list = None


class VolumeProfileAnalyzer:
    """
    Computes simplified Volume Profile from daily OHLCV.
    Identifies Point of Control (POC) and Value Area (VA).

    Professional traders use volume profile to:
    - Find strong support (HVN below current price)
    - Find resistance (HVN above current price)
    - Identify LVNs where price will move quickly (no support)
    - Combine with Fibonacci: Fib level + HVN = very high probability zone
    """

    N_BINS = 50    # Price buckets for profile

    def analyze(
        self,
        ticker: str,
        highs: list,
        lows: list,
        closes: list,
        volumes: list,
        lookback: int = 63,    # 63 trading days = 1 quarter
    ) -> VolumeProfileResult:
        """
        Build volume profile for the lookback period.
        Returns POC, Value Area, and HVN/LVN levels.
        """
        n = min(len(closes), lookback)
        recent_highs   = highs[-n:]
        recent_lows    = lows[-n:]
        recent_volumes = volumes[-n:]

        # Price range for the period
        price_min = min(recent_lows)
        price_max = max(recent_highs)
        current   = closes[-1]

        if price_max == price_min:
            return VolumeProfileResult(
                ticker=ticker, point_of_control=current,
                value_area_high=current * 1.02, value_area_low=current * 0.98,
                current_price=current, price_in_value_area=True,
                levels=[], nearest_hvn_below=None, nearest_hvn_above=None,
                rvol=1.0, notes=["Insufficient price range for volume profile"],
            )

        # Create price bins
        bin_size  = (price_max - price_min) / self.N_BINS
        bins      = [0.0] * self.N_BINS
        bin_prices = [price_min + (i + 0.5) * bin_size for i in range(self.N_BINS)]

        # Distribute each day's volume across the price range it covered
        for i in range(n):
            day_low    = recent_lows[i]
            day_high   = recent_highs[i]
            day_volume = recent_volumes[i]
            day_range  = day_high - day_low

            if day_range == 0:
                # All in one bin
                bin_idx = int((day_low - price_min) / bin_size)
                bin_idx = max(0, min(self.N_BINS - 1, bin_idx))
                bins[bin_idx] += day_volume
            else:
                # Distribute volume proportionally across bins covered
                for j in range(self.N_BINS):
                    bin_low  = price_min + j * bin_size
                    bin_high = bin_low + bin_size
                    overlap_low  = max(bin_low, day_low)
                    overlap_high = min(bin_high, day_high)
                    if overlap_high > overlap_low:
                        overlap_pct = (overlap_high - overlap_low) / day_range
                        bins[j] += day_volume * overlap_pct

        total_volume = sum(bins)
        if total_volume == 0:
            return VolumeProfileResult(
                ticker=ticker, point_of_control=current,
                value_area_high=current, value_area_low=current,
                current_price=current, price_in_value_area=True,
                levels=[], nearest_hvn_below=None, nearest_hvn_above=None,
                rvol=1.0, notes=["No volume data"],
            )

        # Point of control = price bin with most volume
        poc_idx = bins.index(max(bins))
        poc     = bin_prices[poc_idx]

        # Value Area: price range containing 70% of total volume
        # Start from POC and expand outward
        va_volume_target = total_volume * 0.70
        va_bins = {poc_idx}
        va_volume = bins[poc_idx]
        low_ptr  = poc_idx - 1
        high_ptr = poc_idx + 1

        while va_volume < va_volume_target and (low_ptr >= 0 or high_ptr < self.N_BINS):
            add_low  = bins[low_ptr]  if low_ptr >= 0          else 0
            add_high = bins[high_ptr] if high_ptr < self.N_BINS else 0

            if add_high >= add_low and high_ptr < self.N_BINS:
                va_bins.add(high_ptr)
                va_volume += add_high
                high_ptr += 1
            elif low_ptr >= 0:
                va_bins.add(low_ptr)
                va_volume += add_low
                low_ptr -= 1
            else:
                break

        va_high = max(bin_prices[i] for i in va_bins) if va_bins else poc
        va_low  = min(bin_prices[i] for i in va_bins) if va_bins else poc

        # Identify HVN and LVN
        mean_vol = statistics.mean(bins)
        std_vol  = statistics.stdev(bins) if len(bins) > 1 else 0

        levels = []
        for i in range(self.N_BINS):
            pct = bins[i] / total_volume
            is_hvn = bins[i] > mean_vol + 0.5 * std_vol
            is_lvn = bins[i] < mean_vol - 0.5 * std_vol
            levels.append(VolumeProfileLevel(
                price=bin_prices[i],
                volume=bins[i],
                pct_of_total=pct,
                is_high_volume_node=is_hvn,
                is_low_volume_node=is_lvn,
            ))

        # Nearest HVN above and below current price
        hvns_below = [l.price for l in levels if l.is_high_volume_node and l.price < current]
        hvns_above = [l.price for l in levels if l.is_high_volume_node and l.price > current]

        nearest_hvn_below = max(hvns_below) if hvns_below else None
        nearest_hvn_above = min(hvns_above) if hvns_above else None

        price_in_va = va_low <= current <= va_high

        # Relative volume: today's volume vs 20-day average
        rvol = 1.0
        if len(volumes) >= 2:
            avg_vol_20 = sum(volumes[-21:-1]) / max(1, len(volumes[-21:-1]))
            if avg_vol_20 > 0:
                rvol = volumes[-1] / avg_vol_20

        notes = []
        notes.append(f"POC: ${poc:.2f} — highest volume at this price level")
        notes.append(f"Value Area: ${va_low:.2f} - ${va_high:.2f} (70% of volume)")
        if nearest_hvn_below:
            notes.append(f"Nearest HVN support: ${nearest_hvn_below:.2f}")
        if nearest_hvn_above:
            notes.append(f"Nearest HVN resistance: ${nearest_hvn_above:.2f}")
        if price_in_va:
            notes.append("Price inside Value Area — fair value zone, expect mean reversion")
        else:
            notes.append("Price outside Value Area — extended, watch for return to POC")

        logger.debug(
            f"[VOL PROFILE] {ticker}: POC=${poc:.2f}, VA=${va_low:.2f}-${va_high:.2f}, "
            f"current=${current:.2f}, in_VA={price_in_va}"
        )

        return VolumeProfileResult(
            ticker=ticker,
            point_of_control=poc,
            value_area_high=va_high,
            value_area_low=va_low,
            current_price=current,
            price_in_value_area=price_in_va,
            levels=levels,
            nearest_hvn_below=nearest_hvn_below,
            nearest_hvn_above=nearest_hvn_above,
            rvol=rvol,
            notes=notes,
        )
