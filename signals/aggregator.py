"""
signals/aggregator.py — Signal Aggregator.

Combines all Layer 2 + signal outputs into a single scored recommendation.
This is the input to the FUD filter (Layer 3) and decision engine (Layer 4).

Signal weighting (research-backed priorities):
  1. Fundamentals (ROIC > WACC, moat, margin of safety) — 35% weight
     Reason: Long-term alpha. Avoids value traps.
  2. Insider buying (Form 4 cluster buys)               — 25% weight
     Reason: Strongest near-term signal. Insiders know their business.
  3. Fibonacci + VWAP confluence                        — 20% weight
     Reason: Entry precision. Same company, better entry = bigger profit.
  4. FFT cycle phase                                    — 10% weight
     Reason: Confirmation only. Cycles are real but noisy.
  5. Volume profile (POC / Value Area)                  — 10% weight
     Reason: Institutional footprint confirmation.

VIX regime is a GATE not a weight — it scales the final position size.
"""

from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Optional
import json

from loguru import logger

from config import config
from analysis.engine import AnalysisReport
from signals.fft_cycles import FFTResult
from signals.fibonacci import FibResult
from signals.insider_flow import InsiderSignalResult
from signals.market_microstructure import VWAPResult, VolumeProfileResult, VIXRegime
from signals.momentum import MomentumAnalyzer, MomentumResult
from signals.supertrend import SuperTrendAnalyzer, SuperTrendResult
from signals.tipranks_signal import TipRanksResult
from signals.three_green_arrows import ThreeGreenArrowsResult


@dataclass
class AggregatedSignal:
    ticker: str
    timestamp: str

    # Component scores (normalized -1.0 to +1.0)
    fundamentals_score: float        # From Layer 2 analysis
    insider_score: float             # From Form 4 scorer
    technical_score: float           # Fib + VWAP composite
    cycle_score: float               # FFT phase
    volume_score: float              # Volume profile

    # Weighted composite
    composite_score: float           # -1.0 to +1.0
    signal: str                      # "strong_buy", "buy", "hold", "sell", "strong_sell"
    confidence: float                # 0.0 to 1.0 — agreement between signals

    # VIX regime
    vix_regime: str
    position_size_multiplier: float
    recommended_position_pct: float  # Final position size after VIX adjustment

    # Entry details
    entry_price: Optional[float]
    stop_loss: Optional[float]
    take_profit_1: Optional[float]
    take_profit_2: Optional[float]
    risk_reward_ratio: Optional[float]

    # Supporting context
    why_buy: list       # Reasons supporting the buy
    why_wait: list      # Reasons for caution
    key_risks: list     # Risk factors to monitor

    # Momentum fields (from signals/momentum.py)
    momentum_score: float = 0.0
    rvol: float = 1.0
    is_52w_breakout: bool = False
    macd_signal_direction: str = "neutral"

    # Raw component results (for audit trail)
    investable: bool = False
    moat_strength: str = "none"
    margin_of_safety:             Optional[float] = None
    intrinsic_value_conservative: Optional[float] = None
    fib_confluence_score: float = 0.0
    fib_in_golden_zone: bool = False
    vwap_position: str = "unknown"
    vwap_institutional_bias: str = "unknown"
    poc_level: Optional[float] = None
    fft_phase: str = "unknown"
    fft_signal_strength: float = 0.0
    insider_cluster_buy: bool = False
    insider_buy_value_90d: float = 0.0

    # SuperTrend fields
    supertrend_direction: str = "unknown"
    supertrend_value: float = 0.0
    supertrend_atr: float = 0.0
    supertrend_favourability: float = 0.5

    # TipRanks fields
    tipranks_score: float = 0.0
    tipranks_smart_score: Optional[int] = None
    tipranks_buy_pct: Optional[float] = None

    # 3 Green Arrows fields (ThinkorSwim emulation)
    tga_arrows_count: int = 0         # 0-3 primary arrows active
    tga_signal: str = "neutral"       # "buy" | "watch" | "neutral"
    tga_sma_arrow: bool = False
    tga_macd_arrow: bool = False
    tga_stoch_arrow: bool = False
    tga_volume_spike: bool = False
    tga_reason: str = ""

    # ── WATCH state ────────────────────────────────────────────────
    # When the conviction floor or orphaned-signal guard would normally
    # downgrade a BUY composite to HOLD, we emit "watch" instead so the
    # operator can see *why* the trade is parked and what would unblock it.
    # watch_reasons lists the specific missing confirmations; e.g.
    #   ["MACD momentum (currently falling)", "Insider cluster buy (0 in 90d)"]
    watch_reasons: list = field(default_factory=list)


# Signal weights (must sum to 1.0)
# Rebalanced to add momentum + SuperTrend + TipRanks + 3GA
SIGNAL_WEIGHTS = {
    "fundamentals": 0.22,
    "momentum":     0.20,   # RVOL, MACD, MA stack, 52w breakout
    "insider":      0.17,   # Form 4 cluster buys
    "technical":    0.13,   # Fib + VWAP composite
    "supertrend":   0.10,   # ATR trend direction + TP favourability
    "tipranks":     0.10,   # Smart Score + analyst consensus
    "tga":          0.05,   # 3 Green Arrows (SMA cross, MACD, Stochastic)
    "cycle":        0.02,   # FFT cycles (confirmation only)
    "volume":       0.01,   # Volume profile POC/Value Area
}

# Buy signal threshold — must match L3 reclassification threshold in filter_engine.py
BUY_THRESHOLD = 0.10


class SignalAggregator:
    """
    Aggregates all signal components into a final trade recommendation.
    """

    def _normalize_fundamental_score(self, analysis: AnalysisReport) -> float:
        """Convert fundamental analysis to -1.0 to +1.0 score."""
        if not analysis.is_investable:
            return -0.5   # Not investable = negative but not strongly sell

        score = 0.0

        # ROIC vs WACC spread
        if analysis.roic and analysis.wacc:
            spread = analysis.roic - analysis.wacc
            if spread > 0.10:      score += 0.3
            elif spread > 0.05:    score += 0.2
            elif spread > 0:       score += 0.1
            else:                  score -= 0.2

        # Moat
        moat_scores = {"wide": 0.3, "narrow": 0.15, "none": -0.1}
        score += moat_scores.get(analysis.moat_strength, 0)

        # Margin of safety
        # IV > 3x current price (MoS > 200%) almost always means bad DCF inputs
        # (missing shares_outstanding, stale earnings, wrong capex) — treat as unreliable
        # and give zero MoS contribution rather than rewarding a model error with +0.3.
        if analysis.margin_of_safety is not None:
            if analysis.margin_of_safety > 2.0:
                pass   # extreme discount = suspect data; no score contribution
            elif analysis.margin_of_safety > 0.30:   score += 0.3
            elif analysis.margin_of_safety > 0.15:   score += 0.2
            elif analysis.margin_of_safety > 0:      score += 0.1
            else:                                     score -= 0.2

        return max(-1.0, min(1.0, score))

    def _normalize_insider_score(self, insider: Optional[InsiderSignalResult]) -> float:
        """Convert insider signal to -1.0 to +1.0."""
        if insider is None:
            return 0.0   # No data = neutral

        signal_map = {
            "strong_buy":  1.0,
            "buy":         0.6,
            "neutral":     0.0,
            "sell":       -0.5,
            "strong_sell":-1.0,
        }
        base = signal_map.get(insider.signal, 0.0)

        # Cluster buy bonus
        if insider.cluster_buy_detected:
            base = min(1.0, base + 0.2)

        return base

    def _normalize_itool_score(self, itool_signal: Optional[str]) -> Optional[float]:
        """Convert I-Tool bullish/bearish signal to -1.0 to +1.0. Returns None if no signal."""
        if itool_signal == "bullish":
            return 0.8
        if itool_signal == "bearish":
            return -0.8
        return None

    def _normalize_technical_score(
        self,
        fib: Optional[FibResult],
        vwap: Optional[VWAPResult],
        itool_signal: Optional[str] = None,
    ) -> float:
        """Combine Fibonacci, VWAP, and I-Tool technical signal into single technical score."""
        score = 0.0
        components = 0

        if fib:
            fib_map = {
                "strong_buy":        1.0,
                "buy":               0.6,
                "approaching_support": 0.3,
                "neutral":           0.0,
                "sell":             -0.6,
                "strong_sell":      -1.0,
            }
            fib_score = fib_map.get(fib.entry_signal, 0.0)
            if fib.in_golden_zone:     fib_score = min(1.0, fib_score + 0.2)
            if fib.rsi_divergence:     fib_score = min(1.0, fib_score + 0.15)
            if fib.rsi_oversold:       fib_score = min(1.0, fib_score + 0.1)
            if fib.trend_direction == "ranging":  fib_score *= 0.5   # Less reliable
            score += fib_score
            components += 1

        if vwap:
            vwap_score = 0.0
            if vwap.is_extended_below:             vwap_score += 0.6
            elif vwap.position == "below":         vwap_score += 0.3
            elif vwap.position == "at":            vwap_score += 0.1
            elif vwap.is_extended_above:           vwap_score -= 0.4
            if vwap.institutional_bias == "accumulation":   vwap_score += 0.2
            elif vwap.institutional_bias == "distribution": vwap_score -= 0.2
            score += vwap_score
            components += 1

        fib_vwap = max(-1.0, min(1.0, score / components)) if components > 0 else 0.0

        # Blend in I-Tool signal (RSI/MACD/Stochastic confluence) at 40% weight
        itool = self._normalize_itool_score(itool_signal)
        if itool is not None:
            return max(-1.0, min(1.0, 0.60 * fib_vwap + 0.40 * itool))
        return fib_vwap

    def _normalize_cycle_score(self, fft: Optional[FFTResult]) -> float:
        """Convert FFT phase score to -1.0 to +1.0."""
        if fft is None:
            return 0.0
        if fft.signal_strength < 0.3:
            return 0.0   # Cycle too noisy — return neutral
        # FFT phase_score is already -1.0 (peak) to +1.0 (trough)
        return fft.phase_score * fft.signal_strength  # Weight by reliability

    def _normalize_tipranks_score(self, tr: Optional[TipRanksResult]) -> float:
        """TipRanks composite score is already -1.0 to +1.0."""
        if tr is None:
            return 0.0
        return max(-1.0, min(1.0, tr.composite_score))

    def _normalize_tga_score(self, tga: Optional[ThreeGreenArrowsResult]) -> float:
        """3 Green Arrows: arrows_count 0-3 → -0.2 to +1.0."""
        if tga is None:
            return 0.0
        return {3: 1.0, 2: 0.4, 1: 0.0, 0: -0.2}.get(tga.arrows_count, 0.0)

    def _normalize_supertrend_score(self, st: Optional[SuperTrendResult]) -> float:
        """SuperTrend direction + TP favourability → -1.0 to +1.0."""
        if st is None:
            return 0.0
        base = 0.7 if st.direction == "uptrend" else -0.7
        # Favourability nudges the score: 0.5=neutral, 1.0=+0.3, 0.0=-0.3
        tp_adj = (st.tp_favourability - 0.5) * 0.6
        return max(-1.0, min(1.0, base + tp_adj))

    def _normalize_volume_score(
        self,
        vol_profile: Optional[VolumeProfileResult],
        vwap: Optional[VWAPResult],
    ) -> float:
        """Volume profile confirmation score."""
        score = 0.0

        if vol_profile:
            # At POC = strong support/resistance
            current = vol_profile.current_price
            poc_distance = abs(current - vol_profile.point_of_control) / current

            if poc_distance < 0.01:           # Within 1% of POC
                score += 0.4
            elif vol_profile.price_in_value_area:
                score += 0.2

            # Nearest HVN support close by
            if vol_profile.nearest_hvn_below:
                hvn_distance = (current - vol_profile.nearest_hvn_below) / current
                if hvn_distance < 0.02:       # Within 2%
                    score += 0.3

        return max(-1.0, min(1.0, score))

    def _compute_confidence(self, scores: dict) -> float:
        """
        Confidence = how much the signals agree with each other.
        High confidence = most signals point the same way.
        """
        values = list(scores.values())
        if not values:
            return 0.0

        # Count signals in same direction as composite
        _w = getattr(type(self), 'SIGNAL_WEIGHTS', SIGNAL_WEIGHTS)
        composite = sum(_w.get(k, 0) * v for k, v in scores.items())
        same_direction = sum(1 for v in values if (v > 0) == (composite > 0) and abs(v) > 0.1)

        agreement_pct = same_direction / len(values)
        magnitude = abs(composite)

        return min(1.0, agreement_pct * magnitude * 1.5)

    def _build_narrative(
        self,
        analysis: Optional[AnalysisReport],
        fib: Optional[FibResult],
        vwap: Optional[VWAPResult],
        insider: Optional[InsiderSignalResult],
        fft: Optional[FFTResult],
        vol_profile: Optional[VolumeProfileResult],
    ) -> tuple:
        """Build human-readable why_buy / why_wait / risks lists."""
        why_buy  = []
        why_wait = []
        risks    = []

        if analysis:
            if analysis.margin_of_safety and analysis.margin_of_safety > 0.15:
                why_buy.append(f"Trading at {analysis.margin_of_safety:.0%} discount to intrinsic value")
            if analysis.moat_strength in ("wide", "narrow"):
                why_buy.append(f"{analysis.moat_strength.capitalize()} moat: {', '.join(analysis.moat_types)}")
            if analysis.roic and analysis.wacc and analysis.roic > analysis.wacc:
                spread = analysis.roic - analysis.wacc
                why_buy.append(f"ROIC {spread:.0%} above cost of capital — value creation confirmed")
            if not analysis.is_investable:
                why_wait.append("Does not meet minimum investability criteria")
            if analysis.margin_of_safety and analysis.margin_of_safety < 0:
                why_wait.append(f"Trading {-analysis.margin_of_safety:.0%} ABOVE intrinsic value")

        if insider:
            if insider.cluster_buy_detected:
                why_buy.append(f"CLUSTER BUY: {insider.unique_buyers_90d} insiders bought ${insider.cluster_buy_value:,.0f}")
            elif insider.signal == "buy":
                why_buy.append(f"Insider buying detected: ${insider.total_buy_value_90d:,.0f} last 90 days")
            if insider.signal in ("sell", "strong_sell"):
                why_wait.append(f"Insiders net sellers: ${insider.total_sell_value_90d:,.0f} sold last 90 days")

        if fib:
            if fib.in_golden_zone:
                why_buy.append(f"Price at Golden Zone (61.8% Fibonacci) — highest probability reversal")
            if fib.rsi_divergence:
                why_buy.append("RSI bullish divergence at Fibonacci support — timing confirmation")
            if fib.trend_direction == "ranging":
                why_wait.append("Market ranging — Fibonacci signals less reliable")

        if vwap:
            if vwap.is_extended_below:
                why_buy.append(f"Price {abs(vwap.price_vs_vwap_pct):.1%} below VWAP — institutional buy zone")
            if vwap.institutional_bias == "accumulation":
                why_buy.append("VWAP slope rising — institutional accumulation detected")
            if vwap.is_extended_above:
                why_wait.append(f"Price {vwap.price_vs_vwap_pct:.1%} above VWAP — extended, wait for pullback")

        if fft and fft.signal_strength >= 0.3:
            if fft.current_cycle_phase in ("trough_recovering",):
                why_buy.append(f"FFT: At cycle trough in {fft.nearest_known_cycle}-day cycle — timing favorable")
            elif fft.current_cycle_phase in ("peak_forming", "falling"):
                why_wait.append(f"FFT: Near cycle peak — wait for pullback before entering")

        # Standard risks
        risks.append("Always verify with your own research before live trading")
        risks.append("Past signal performance does not guarantee future results")
        if analysis and analysis.net_debt_to_ebitda and analysis.net_debt_to_ebitda > 3:
            risks.append(f"High leverage: {analysis.net_debt_to_ebitda:.1f}x Net Debt/EBITDA")

        return why_buy, why_wait, risks

    def aggregate(
        self,
        analysis: AnalysisReport,
        fft: Optional[FFTResult] = None,
        fib: Optional[FibResult] = None,
        insider: Optional[InsiderSignalResult] = None,
        vwap: Optional[VWAPResult] = None,
        vol_profile: Optional[VolumeProfileResult] = None,
        vix_regime: Optional[VIXRegime] = None,
        current_price: Optional[float] = None,
        itool_signal: Optional[str] = None,  # "bullish" | "bearish" | None
        momentum: Optional[MomentumResult] = None,
        supertrend: Optional[SuperTrendResult] = None,  # ATR trend + TP favourability
        tipranks: Optional[TipRanksResult] = None,      # Smart Score + analyst consensus
        tga: Optional[ThreeGreenArrowsResult] = None,   # 3 Green Arrows (TOS emulation)
        floor_fundamentals: bool = False,   # AI Watch override: floor f_score at 0
        buy_threshold_override: float = None,  # Per-model threshold (None = use BUY_THRESHOLD)
    ) -> AggregatedSignal:
        """
        Combine all signals into final AggregatedSignal.
        """

        # ── Normalize all component scores ────────────────────────
        f_score = self._normalize_fundamental_score(analysis)
        i_score = self._normalize_insider_score(insider)
        t_score = self._normalize_technical_score(fib, vwap, itool_signal=itool_signal)
        c_score = self._normalize_cycle_score(fft)
        v_score = self._normalize_volume_score(vol_profile, vwap)

        # Momentum score from momentum module
        m_score = 0.0
        if momentum is not None:
            momentum_map = {
                "strong_buy": 1.0, "buy": 0.6,
                "hold": 0.0, "sell": -0.3, "strong_sell": -0.7,
            }
            m_score = momentum_map.get(momentum.signal, 0.0)
            # VWAP extended-above is context-aware: only penalise if momentum is not confirming
            if vwap and vwap.is_extended_above and momentum.signal in ("strong_buy", "buy"):
                # Breakout day — extended-above is a good sign, not a penalty
                t_score = max(t_score, 0.1)

        # SuperTrend score
        st_score = self._normalize_supertrend_score(supertrend)

        # TipRanks score
        tr_score = self._normalize_tipranks_score(tipranks)

        # 3 Green Arrows score
        tga_score = self._normalize_tga_score(tga)

        # Momentum-aware fundamental floor: for non-investable tickers with strong
        # momentum signals, soften the -0.5 penalty to 0.0 so momentum/breakout
        # plays aren't permanently blocked by pure value criteria.
        if not analysis.is_investable and (floor_fundamentals or m_score >= 0.35):
            f_score = max(0.0, f_score)
        elif floor_fundamentals:
            f_score = max(0.0, f_score)

        scores = {
            "fundamentals": f_score,
            "momentum":     m_score,
            "insider":      i_score,
            "technical":    t_score,
            "supertrend":   st_score,
            "tipranks":     tr_score,
            "tga":          tga_score,
            "cycle":        c_score,
            "volume":       v_score,
        }

        # ── Weighted composite ─────────────────────────────────────
        _w = getattr(type(self), 'SIGNAL_WEIGHTS', SIGNAL_WEIGHTS)
        composite = sum(_w.get(k, 0) * v for k, v in scores.items())
        confidence = self._compute_confidence(scores)

        # ── Signal classification ──────────────────────────────────
        _bt = buy_threshold_override if buy_threshold_override is not None else BUY_THRESHOLD
        if composite >= 0.50:   signal = "strong_buy"
        elif composite >= _bt:  signal = "buy"
        elif composite >= -0.15: signal = "hold"
        elif composite >= -0.40: signal = "sell"
        else:                    signal = "strong_sell"

        # ── VIX regime ────────────────────────────────────────────
        vix_str   = vix_regime.regime if vix_regime else "unknown"
        psm       = vix_regime.position_size_multiplier if vix_regime else 0.85
        base_pct  = config.risk.max_position_pct * composite if composite > 0 else 0
        final_pct = base_pct * psm

        # Block signal if VIX says no
        if vix_regime and not vix_regime.allow_new_longs and signal in ("buy", "strong_buy"):
            signal = "hold"
            final_pct = 0.0

        # ── Entry / stop / target ─────────────────────────────────
        stop_loss = None
        tp1 = tp2 = None
        rr_ratio = None

        # ATR available for both paths — compute once here.
        _atr = supertrend.atr if (supertrend and supertrend.atr > 0) else None

        if fib and current_price and fib.stop_loss_level:
            stop_loss = fib.stop_loss_level
            tp1 = fib.take_profit_1
            tp2 = fib.take_profit_2

            # ATR floor: Fibonacci stops can be as tight as 1–3%.
            # During normal volatility a 2× ATR stop keeps the trade alive through
            # daily fluctuations without eating into the signal.
            if _atr and stop_loss:
                min_stop_dist = 2.0 * _atr
                if (current_price - stop_loss) < min_stop_dist:
                    stop_loss = round(current_price - min_stop_dist, 2)

            if stop_loss and tp1 and current_price:
                risk   = current_price - stop_loss
                reward = tp1 - current_price
                if risk > 0:
                    rr_ratio = round(reward / risk, 2)

        elif current_price and analysis.current_price:
            # ATR-scaled stop: 2.5× ATR gives each stock room proportional to its volatility.
            # Fallback to 5% when SuperTrend ATR isn't available. Floor at -15%.
            _stop_dist = (2.5 * _atr) if _atr else (current_price * 0.05)
            stop_loss = round(max(current_price - _stop_dist, current_price * 0.85), 2)
            tp1       = current_price * 1.09
            tp2       = current_price * 1.15
            rr_ratio  = round((tp1 - current_price) / _stop_dist, 2) if _stop_dist > 0 else 3.0

        # ── Narrative ────────────────────────────────────────────
        why_buy, why_wait, risks = self._build_narrative(analysis, fib, vwap, insider, fft, vol_profile)

        # ── Conviction floor → WATCH ──────────────────────────────
        # A BUY signal below composite 0.50 must be confirmed by at least one of:
        # positive momentum (price action / institutional follow-through) or positive
        # insider activity (management skin-in-the-game). Without either, the signal
        # is a lone fundamental thesis that the market has not yet validated — the
        # exact pattern behind value traps and DCF model errors.
        #
        # Rather than downgrading silently to HOLD, we surface the setup as WATCH
        # with the specific blockers attached so operators can see what's missing.
        watch_reasons: list = []
        if signal in ("buy", "strong_buy") and composite < 0.50 and m_score <= 0.0 and i_score <= 0.0:
            if momentum is None:
                watch_reasons.append("Momentum confirmation (no momentum data)")
            else:
                _mdir = getattr(momentum, "macd_direction", "flat")
                watch_reasons.append(
                    f"Momentum confirmation (m={m_score:.2f}, MACD {_mdir}, "
                    f"signal={momentum.signal})"
                )
            _ins_buys = getattr(insider, "total_buys_90d", 0) if insider else 0
            watch_reasons.append(
                f"Insider accumulation (i={i_score:.2f}, "
                f"{_ins_buys} open-market buys in 90d)"
            )
            signal = "watch"
            why_wait.append(
                f"Conviction void: composite={composite:.2f} below 0.50 with no momentum "
                f"(m={m_score:.2f}) or insider (i={i_score:.2f}) confirmation — "
                f"WATCH until at least one confirmation arrives"
            )
            logger.info(
                f"[AGG] {analysis.ticker}: conviction floor → WATCH "
                f"composite={composite:.2f} m={m_score:.2f} i={i_score:.2f} "
                f"reasons={watch_reasons}"
            )

        # Orphaned signal guard: weak composite AND low signal agreement.
        # Composite < 0.40 with confidence < 0.20 means fewer than ~2 of 9 sub-signals
        # align — not enough directional consensus to trust the buy classification.
        if signal in ("buy", "strong_buy") and confidence < 0.20 and composite < 0.40:
            watch_reasons.append(
                f"Sub-signal agreement (only {confidence:.0%} of components align)"
            )
            signal = "watch"
            why_wait.append(
                f"Orphaned signal: low sub-signal agreement (conf={confidence:.0%}, "
                f"composite={composite:.2f}) — WATCH for additional confirmations"
            )
            logger.warning(
                f"[AGG] {analysis.ticker}: confidence-floor → WATCH "
                f"conf={confidence:.0%} composite={composite:.2f}"
            )

        logger.info(
            f"[AGG] {analysis.ticker}: signal={signal}, composite={composite:.2f}, "
            f"confidence={confidence:.2f}, VIX={vix_str}, pos_size={final_pct:.1%}"
        )
        logger.info(
            f"[AGG] {analysis.ticker} scores: "
            f"F={f_score:.2f} M={m_score:.2f} I={i_score:.2f} T={t_score:.2f} "
            f"ST={st_score:.2f} TR={tr_score:.2f} TGA={tga_score:.2f}({tga.arrows_count if tga else 0}/3) "
            f"C={c_score:.2f} V={v_score:.2f} | I-Tool={itool_signal or 'n/a'}"
        )

        rvol_val = vol_profile.rvol if vol_profile else (momentum.volume_ratio if momentum else 1.0)

        return AggregatedSignal(
            ticker=analysis.ticker,
            timestamp=datetime.utcnow().isoformat(),
            fundamentals_score=f_score,
            insider_score=i_score,
            technical_score=t_score,
            cycle_score=c_score,
            volume_score=v_score,
            composite_score=composite,
            signal=signal,
            confidence=confidence,
            vix_regime=vix_str,
            position_size_multiplier=psm,
            recommended_position_pct=final_pct,
            entry_price=current_price,
            stop_loss=stop_loss,
            take_profit_1=tp1,
            take_profit_2=tp2,
            risk_reward_ratio=rr_ratio,
            why_buy=why_buy,
            why_wait=why_wait,
            key_risks=risks,
            momentum_score=m_score,
            rvol=rvol_val,
            is_52w_breakout=momentum.is_52w_high_breakout if momentum else False,
            macd_signal_direction=momentum.macd_direction if momentum else "neutral",
            investable=analysis.is_investable,
            moat_strength=analysis.moat_strength,
            margin_of_safety=analysis.margin_of_safety,
            intrinsic_value_conservative=analysis.intrinsic_value_conservative,
            fib_confluence_score=fib.confluence_score if fib else 0.0,
            fib_in_golden_zone=fib.in_golden_zone if fib else False,
            vwap_position=vwap.position if vwap else "unknown",
            vwap_institutional_bias=vwap.institutional_bias if vwap else "unknown",
            poc_level=vol_profile.point_of_control if vol_profile else None,
            fft_phase=fft.current_cycle_phase if fft else "unknown",
            fft_signal_strength=fft.signal_strength if fft else 0.0,
            insider_cluster_buy=insider.cluster_buy_detected if insider else False,
            insider_buy_value_90d=insider.total_buy_value_90d if insider else 0.0,
            supertrend_direction=supertrend.direction if supertrend else "unknown",
            supertrend_value=supertrend.supertrend_value if supertrend else 0.0,
            supertrend_atr=supertrend.atr if supertrend else 0.0,
            supertrend_favourability=supertrend.tp_favourability if supertrend else 0.5,
            tipranks_score=tr_score,
            tipranks_smart_score=tipranks.smart_score if tipranks else None,
            tipranks_buy_pct=tipranks.buy_pct if tipranks else None,
            tga_arrows_count=tga.arrows_count if tga else 0,
            tga_signal=tga.signal if tga else "neutral",
            tga_sma_arrow=tga.sma_arrow if tga else False,
            tga_macd_arrow=tga.macd_arrow if tga else False,
            tga_stoch_arrow=tga.stoch_arrow if tga else False,
            tga_volume_spike=tga.volume_spike if tga else False,
            tga_reason=tga.reason if tga else "",
            watch_reasons=watch_reasons,
        )

class ClaudeMomentumAggregator(SignalAggregator):
    """
    Claude's momentum-regime model.

    Philosophy: trend is truth. SuperTrend + momentum dominate; fundamentals
    are noise for high-beta stocks. VIX hard gate — cash in fear regimes.

    Weights: ST=0.35, Mom=0.30, Insider=0.20, Tech=0.10, Fund=0.05
    Fundamentals floor at -0.10 (non-investable barely penalised).
    VIX gate: score penalty when VIX > 25; block all buys when VIX > 30.
    """

    SIGNAL_WEIGHTS = {
        "fundamentals": 0.05,
        "momentum":     0.30,
        "insider":      0.20,
        "technical":    0.08,
        "supertrend":   0.35,
        "tipranks":     0.00,
        "tga":          0.02,
        "cycle":        0.00,
        "volume":       0.00,
    }

    def _normalize_fundamental_score(self, analysis: AnalysisReport) -> float:
        raw = super()._normalize_fundamental_score(analysis)
        return max(-0.10, raw)

    def aggregate(self, analysis, fft=None, fib=None, insider=None, vwap=None,
                  vol_profile=None, vix_regime=None, current_price=None,
                  itool_signal=None, momentum=None, supertrend=None,
                  tipranks=None, tga=None, floor_fundamentals=True, buy_threshold_override=None):
        result = super().aggregate(
            analysis=analysis, fft=fft, fib=fib, insider=insider,
            vwap=vwap, vol_profile=vol_profile, vix_regime=vix_regime,
            current_price=current_price, itool_signal=itool_signal,
            momentum=momentum, supertrend=supertrend, tipranks=tipranks, tga=tga,
            floor_fundamentals=True,
            buy_threshold_override=buy_threshold_override,
        )
        # VIX hard gate on top of base VIX logic
        if vix_regime:
            vix_val = getattr(vix_regime, "vix_level", 20.0)
            if vix_val > 30:
                result.signal = "hold"
                result.recommended_position_pct = 0.0
                result.composite_score = min(result.composite_score, 0.0)
            elif vix_val > 25:
                result.composite_score = round(result.composite_score * 0.65, 4)
                _bt = buy_threshold_override if buy_threshold_override is not None else 0.08
                if result.signal in ("buy", "strong_buy") and result.composite_score < _bt:
                    result.signal = "hold"
        return result


def summary(self, sig: AggregatedSignal) -> str:
        """Human-readable signal summary."""
        lines = [
            f"{'═'*55}",
            f"  {sig.ticker} — {sig.signal.upper().replace('_', ' ')}",
            f"  Composite: {sig.composite_score:+.2f} | Confidence: {sig.confidence:.0%}",
            f"  VIX Regime: {sig.vix_regime} (size ×{sig.position_size_multiplier:.0%})",
            f"{'═'*55}",
            f"",
            f"SIGNAL BREAKDOWN:",
            f"  Fundamentals: {sig.fundamentals_score:+.2f} (35%)",
            f"  Insider:      {sig.insider_score:+.2f} (25%)",
            f"  Technical:    {sig.technical_score:+.2f} (20%)",
            f"  FFT Cycle:    {sig.cycle_score:+.2f} (10%)",
            f"  Vol Profile:  {sig.volume_score:+.2f} (10%)",
            f"",
            f"TRADE PLAN:",
            f"  Entry:     ${sig.entry_price:.2f}" if sig.entry_price else "  Entry: N/A",
            f"  Stop Loss: ${sig.stop_loss:.2f}" if sig.stop_loss else "  Stop Loss: N/A",
            f"  Target 1:  ${sig.take_profit_1:.2f}" if sig.take_profit_1 else "  Target 1: N/A",
            f"  Target 2:  ${sig.take_profit_2:.2f}" if sig.take_profit_2 else "  Target 2: N/A",
            f"  R/R Ratio: {sig.risk_reward_ratio:.1f}:1" if sig.risk_reward_ratio else "  R/R: N/A",
            f"  Position:  {sig.recommended_position_pct:.1%} of portfolio",
            f"",
            f"WHY BUY:",
        ]
        for r in (sig.why_buy or ["No strong buy reasons"]):
            lines.append(f"  ✓ {r}")

        lines.append(f"\nCAUTION:")
        for r in (sig.why_wait or ["None noted"]):
            lines.append(f"  ⚠ {r}")

        lines.append(f"\nRISKS:")
        for r in sig.key_risks:
            lines.append(f"  ⚡ {r}")

        return "\n".join(lines)
