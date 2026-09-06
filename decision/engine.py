"""
decision/engine.py — Layer 4: The Decision Engine.
                     CEO-Level Orchestrator.

═══════════════════════════════════════════════════════════════════════
ARCHITECTURE PHILOSOPHY
═══════════════════════════════════════════════════════════════════════

As CEO of this system, the decision framework answers one question:
"Given everything we know, is this trade worth making RIGHT NOW?"

Not: "Is this a good company?" (Layer 2 answered that)
Not: "Is the news clean?" (Layer 3 answered that)
But: "Is the probability distribution of outcomes favorable enough,
     at this exact moment in the market's fluid dynamics cycle,
     to deploy capital at the Kelly-optimal fraction?"

The five physics frameworks we integrate:

  1. REYNOLDS NUMBER (Fluid Dynamics)
     Are we in laminar or turbulent market flow?
     Only deploy in laminar/transient regimes.
     → Regime gate and position scaling

  2. QUANTUM SUPERPOSITION (Quantum Probability)
     What is the probability amplitude of each market state?
     Has the wave function collapsed to a definite direction?
     → Confidence weighting and interference detection

  3. ENSEMBLE FORECAST (Weather Forecasting BMA)
     What does the ensemble of models say?
     How wide is the spread? Is this forecast "sharp"?
     → Calibrated probability and uncertainty quantification

  4. KALMAN FILTER (Apollo Navigation)
     What is the optimal estimate of the true price trend,
     filtering out market microstructure noise?
     → De-noised trend signal and innovation surprise detection

  5. KELLY CRITERION (Information Theory)
     Given our edge and the odds, what is the mathematically
     optimal fraction of capital to deploy?
     → Final position sizing

Final output: A DecisionResult with:
  - GO / NO-GO binary decision
  - Exact shares to buy
  - Exact entry price, stop loss, take profit
  - Full mathematical audit trail

═══════════════════════════════════════════════════════════════════════
"""

import json
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional

from loguru import logger

from config import config
from models.database import Company, PriceHistory, TradeSignal
from fud.filter_engine import Layer3Result
from signals.aggregator import AggregatedSignal
from decision.reynolds_turbulence import ReynoldsMarketAnalyzer, ReynoldsResult
from decision.quantum_kalman import QuantumStateAnalyzer, KalmanPriceFilter, QuantumStateResult, KalmanResult
from decision.ensemble_kelly import EnsembleKellyEngine, EnsembleForecastResult


def _ema_value(values: list, period: int) -> Optional[float]:
    """Wilder/exponential moving average of the last `period` bars. Returns None if insufficient data."""
    if not values or len(values) < period:
        return None
    k = 2.0 / (period + 1)
    ema = float(values[0])
    for v in values[1:]:
        ema = float(v) * k + ema * (1 - k)
    return ema


@dataclass
class DecisionResult:
    """
    Final Layer 4 output — the executive decision.
    Everything downstream (Layer 5 Risk, Layer 6 Executor) reads from this.
    """
    ticker: str
    timestamp: str

    # THE DECISION
    action: str                   # "BUY", "SELL", "HOLD", "WAIT"
    decision_confidence: float    # 0.0 to 1.0
    go_no_go: bool                # True = execute, False = do not trade

    # ENTRY SPECIFICATION
    recommended_shares: int
    recommended_position_pct: float
    entry_price: Optional[float]
    stop_loss: Optional[float]
    take_profit_1: Optional[float]
    take_profit_2: Optional[float]
    risk_reward_ratio: Optional[float]
    max_dollar_risk: Optional[float]    # (entry - stop) × shares

    # PHYSICS MODEL OUTPUTS
    reynolds_regime: str              # laminar/transient/turbulent/extreme
    reynolds_number: float
    reynolds_position_mult: float
    quantum_dominant_state: str       # bull/bear/sideways
    quantum_certainty: float
    quantum_interference: str         # constructive/destructive/neutral
    kalman_trend: str                 # up/down/flat
    kalman_price: float               # De-noised price estimate
    kalman_innovation_sigma: float    # How surprising was last price move?
    ensemble_probability_bull: float
    ensemble_spread_category: str
    kelly_fraction: float
    kelly_recommended_fraction: float

    # OUTCOME DISTRIBUTION (P10/P50/P90)
    price_target_bear: float          # P10 — bear case
    price_target_base: float          # P50 — base case
    price_target_bull: float          # P90 — bull case

    # GATE SUMMARY
    gates_passed: list                # Which gates approved
    gates_failed: list                # Which gates blocked
    blocking_reason: Optional[str]    # Primary reason if blocked

    # FULL REASONING
    decision_narrative: str           # Human-readable explanation
    physics_notes: list               # Technical notes from each model
    warnings: list                    # Risk warnings

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)

    def summary(self) -> str:
        lines = [
            f"{'╔' + '═'*60 + '╗'}",
            f"║  LAYER 4 DECISION ENGINE — {self.ticker:<30}║",
            f"{'╚' + '═'*60 + '╝'}",
            "",
            f"  {'✅ GO' if self.go_no_go else '❌ NO-GO'} — {self.action} | Confidence: {self.decision_confidence:.0%}",
            "",
            "PHYSICS MODEL READINGS:",
            f"  Reynolds:  {self.reynolds_regime.upper()} (Re={self.reynolds_number:.2f}) | Scale: {self.reynolds_position_mult:.0%}",
            f"  Quantum:   {self.quantum_dominant_state.upper()} ({self.quantum_certainty:.0%} certain | {self.quantum_interference} interference)",
            f"  Kalman:    Price=${self.kalman_price:.2f} Trend={self.kalman_trend} Innovation={self.kalman_innovation_sigma:.1f}σ",
            f"  Ensemble:  P(Bull)={self.ensemble_probability_bull:.0%} Spread={self.ensemble_spread_category} Kelly={self.kelly_fraction:.1%}→{self.kelly_recommended_fraction:.1%}",
            "",
            "TRADE SPECIFICATION:",
            f"  Entry:       ${self.entry_price:.2f}" if self.entry_price else "  Entry: N/A",
            f"  Stop Loss:   ${self.stop_loss:.2f}" if self.stop_loss else "  Stop Loss: N/A",
            f"  Target 1:    ${self.take_profit_1:.2f}" if self.take_profit_1 else "  Target 1: N/A",
            f"  Target 2:    ${self.take_profit_2:.2f}" if self.take_profit_2 else "  Target 2: N/A",
            f"  R/R Ratio:   {self.risk_reward_ratio:.1f}:1" if self.risk_reward_ratio else "  R/R: N/A",
            f"  Shares:      {self.recommended_shares}",
            f"  Position:    {self.recommended_position_pct:.1%} of portfolio",
            f"  Max Risk:    ${self.max_dollar_risk:.2f}" if self.max_dollar_risk else "  Max Risk: N/A",
            "",
            "OUTCOME DISTRIBUTION:",
            f"  P10 (Bear):  ${self.price_target_bear:.2f}" if self.price_target_bear else "  P10: N/A",
            f"  P50 (Base):  ${self.price_target_base:.2f}" if self.price_target_base else "  P50: N/A",
            f"  P90 (Bull):  ${self.price_target_bull:.2f}" if self.price_target_bull else "  P90: N/A",
            "",
            f"GATES: {len(self.gates_passed)} passed, {len(self.gates_failed)} failed",
        ]
        for g in self.gates_passed:
            lines.append(f"  ✓ {g}")
        for g in self.gates_failed:
            lines.append(f"  ✗ {g}")

        if self.blocking_reason:
            lines.append(f"\n  BLOCKED: {self.blocking_reason}")

        lines.append(f"\nNARRATIVE:\n  {self.decision_narrative}")

        return "\n".join(lines)


class DecisionEngine:
    """
    Layer 4 — The CEO Decision Engine.
    Combines physics-based models into final GO/NO-GO decisions.
    """

    # Decision gates — all must pass for GO
    MIN_ENSEMBLE_PROB   = 0.52   # P(bull) must exceed this (was 0.45 — 52% = meaningful edge over coin flip)
    MIN_QUANTUM_CERTAIN = 0.60   # Wave function must be substantially collapsed (was 0.45 — near coin flip)
    MAX_REYNOLDS        = 10.0   # Reject in extreme turbulence (raised from 5.0 — Re 5-10 is turbulent but tradeable)
    MIN_RR_RATIO        = 1.5    # Minimum risk/reward ratio
    MAX_KALMAN_SURPRISE = 2.5    # Reject if Kalman innovation > 2.5σ (unusual move)
    TGA_STRICT_COMPOSITE = 0.50  # Below this composite, Three Green Arrows gate needs 2/3 (positive MOS counts as one); at/above, 1/3

    def __init__(self, db_session_factory, threshold_multiplier: float = 1.0):
        self.Session = db_session_factory
        self.reynolds   = ReynoldsMarketAnalyzer()
        self.quantum    = QuantumStateAnalyzer()
        self.kalman_flt = KalmanPriceFilter()
        self.ensemble   = EnsembleKellyEngine()
        # For relaxed/very_relaxed paper models, set instance-level thresholds
        # (standard model keeps class-level so thresh_override still applies)
        if threshold_multiplier != 1.0:
            self.MIN_ENSEMBLE_PROB   = round(self.__class__.MIN_ENSEMBLE_PROB   * threshold_multiplier, 4)
            self.MIN_QUANTUM_CERTAIN = round(self.__class__.MIN_QUANTUM_CERTAIN * threshold_multiplier, 4)
            self.MIN_RR_RATIO        = round(self.__class__.MIN_RR_RATIO        * threshold_multiplier, 4)
            self.MAX_REYNOLDS        = round(self.__class__.MAX_REYNOLDS        / threshold_multiplier, 4)
            self.MAX_KALMAN_SURPRISE = round(self.__class__.MAX_KALMAN_SURPRISE / threshold_multiplier, 4)
            # Lower the strict-TGA cutoff for relaxed models so fewer composites hit the
            # 2/3 requirement — keeps relaxed/very_relaxed genuinely more permissive.
            self.TGA_STRICT_COMPOSITE = round(self.__class__.TGA_STRICT_COMPOSITE * threshold_multiplier, 4)

    def _load_price_data(self, session, ticker: str) -> dict:
        """Load OHLCV price data for a ticker from DB."""
        company = session.query(Company).filter_by(ticker=ticker).first()
        if not company:
            return {}

        records = (
            session.query(PriceHistory)
            .filter_by(company_id=company.id)
            .order_by(PriceHistory.date)
            .all()
        )

        if not records:
            return {}

        # Dedupe the doubled daily rows before anything ATR-based reads them.
        from utils.price_data import ohlcv_arrays
        bars = ohlcv_arrays(records)
        if not bars["bars"]:
            return {}

        return {
            "closes":  bars["closes"],
            "highs":   bars["highs"],
            "lows":    bars["lows"],
            "volumes": bars["volumes"],
            "market_cap": bars["bars"][-1].market_cap,
            "shares_outstanding": bars["bars"][-1].shares_outstanding,
        }

    def _compute_quantum_score(self, q_result: QuantumStateResult) -> float:
        """Convert quantum state to -1.0 to +1.0 score."""
        if q_result.dominant_state == "bull":
            return q_result.state_certainty * q_result.p_bull
        elif q_result.dominant_state == "bear":
            return -q_result.state_certainty * q_result.p_bear
        else:
            return 0.0

    def _compute_kalman_score(self, k_result: KalmanResult) -> float:
        """Convert Kalman result to -1.0 to +1.0 score."""
        base = k_result.signal_strength
        if k_result.signal == "bullish":
            return base * (1.1 if k_result.is_accelerating else 0.9 if k_result.is_decelerating else 1.0)
        elif k_result.signal == "bearish":
            return -base
        return 0.0

    def _run_gates(
        self,
        layer3: Layer3Result,
        reynolds: ReynoldsResult,
        quantum: QuantumStateResult,
        kalman: KalmanResult,
        ensemble: EnsembleForecastResult,
        rr_ratio: float,
        bypass_gates: set = None,
        closes: list = None,
        current_price: float = None,
    ) -> tuple:
        """
        Run all decision gates.
        Returns (gates_passed, gates_failed, blocking_reason, go_no_go)
        """
        passed  = []
        failed  = []
        blocker = None

        _bypass = bypass_gates or set()

        # Gate 1: FUD Filter passed
        if 'fud' in _bypass:
            passed.append("FUD filter: BYPASSED — user override")
        elif layer3.proceed_to_execution:
            passed.append("FUD filter: news quality acceptable")
        else:
            failed.append(f"FUD filter BLOCKED: {layer3.gate_reason}")
            blocker = layer3.gate_reason

        # Gate 2: Reynolds regime
        if 'reynolds' in _bypass:
            passed.append(f"Reynolds: BYPASSED (Re={reynolds.reynolds_number:.2f}) — user override")
        elif reynolds.allow_entry:
            if reynolds.regime == "laminar":
                passed.append(f"Reynolds: LAMINAR flow (Re={reynolds.reynolds_number:.2f}) — optimal entry conditions")
            elif reynolds.regime == "transient":
                passed.append(f"Reynolds: TRANSIENT flow (Re={reynolds.reynolds_number:.2f}) — acceptable, normal sizing")
            else:
                passed.append(f"Reynolds: TURBULENT (Re={reynolds.reynolds_number:.2f}) — passing but position size reduced to 40%")
        elif reynolds.reynolds_number <= self.MAX_REYNOLDS:
            # Re > 5.0 but within this model's extended threshold — allow with reduced sizing
            reynolds.allow_entry = True
            reynolds.position_multiplier = 0.10
            passed.append(f"Reynolds: EXTENDED (Re={reynolds.reynolds_number:.2f} ≤ {self.MAX_REYNOLDS:.1f} model max) — size capped at 10%")
        else:
            failed.append(f"Reynolds: EXTREME TURBULENCE (Re={reynolds.reynolds_number:.2f}) — market too chaotic, no entry (bypass 'Re' to override)")
            blocker = blocker or f"Market in extreme turbulence (Re={reynolds.reynolds_number:.2f}) — wait for calmer conditions"

        # Gate 3: Quantum certainty
        if 'quantum' in _bypass:
            passed.append(f"Quantum: BYPASSED ({quantum.state_certainty:.0%} certain) — user override")
        elif quantum.state_certainty >= self.MIN_QUANTUM_CERTAIN:
            passed.append(
                f"Quantum: {quantum.dominant_state.upper()} state ({quantum.state_certainty:.0%} certain, "
                f"{quantum.interference_type} interference)"
            )
        else:
            failed.append(
                f"Quantum: Market direction unresolved ({quantum.state_certainty:.0%} certainty < "
                f"{self.MIN_QUANTUM_CERTAIN:.0%} required) — {quantum.dominant_state} but not decisive (bypass 'QSt' to override)"
            )
            blocker = blocker or f"Market direction ambiguous — {quantum.dominant_state} state only {quantum.state_certainty:.0%} certain, need {self.MIN_QUANTUM_CERTAIN:.0%}"

        # Gate 4: Ensemble probability
        if 'ensemble' in _bypass:
            passed.append(f"Ensemble: BYPASSED (P(Bull)={ensemble.ensemble_probability_bull:.0%}) — user override")
        elif ensemble.ensemble_probability_bull >= self.MIN_ENSEMBLE_PROB:
            passed.append(
                f"Ensemble: P(Bull)={ensemble.ensemble_probability_bull:.0%} ≥ {self.MIN_ENSEMBLE_PROB:.0%}"
            )
        else:
            failed.append(
                f"Ensemble: P(Bull)={ensemble.ensemble_probability_bull:.0%} below {self.MIN_ENSEMBLE_PROB:.0%} threshold — insufficient bullish consensus (bypass 'Ens' to override)"
            )
            blocker = blocker or f"Bull probability {ensemble.ensemble_probability_bull:.0%} below {self.MIN_ENSEMBLE_PROB:.0%} minimum — market not sufficiently bullish"

        # Gate 5: R/R ratio
        if 'rr' in _bypass:
            passed.append(f"Risk/Reward: BYPASSED ({rr_ratio:.1f}:1) — user override")
        elif rr_ratio >= self.MIN_RR_RATIO:
            passed.append(f"Risk/Reward: {rr_ratio:.1f}:1 ≥ {self.MIN_RR_RATIO:.1f}:1 minimum")
        else:
            failed.append(f"Risk/Reward: {rr_ratio:.1f}:1 below {self.MIN_RR_RATIO:.1f}:1 minimum — stop too close or target too near (bypass 'R/R' to override)")
            blocker = blocker or f"Risk/reward ratio {rr_ratio:.1f}:1 insufficient — entry has unfavourable stop-loss or take-profit geometry"

        # Gate 6: Kalman surprise check
        # AI Watch breakouts are EXPECTED to have high innovation (that's what a breakout is).
        # Relax the limit to 5.0σ for confirmed AI Watch breakout signals.
        is_ai_watch_breakout = (
            layer3.ticker in config.ai_watch_tickers
            and layer3.adjusted_signal in ("buy", "strong_buy")
        )
        kalman_limit = 5.0 if is_ai_watch_breakout else self.MAX_KALMAN_SURPRISE
        if 'kalman' in _bypass:
            passed.append(f"Kalman: BYPASSED ({kalman.innovation_normalized:.1f}σ) — user override")
        elif abs(kalman.innovation_normalized) <= kalman_limit:
            passed.append(
                f"Kalman: Innovation {kalman.innovation_normalized:.1f}σ — normal, "
                f"trend={kalman.trend_direction}"
            )
        else:
            failed.append(
                f"Kalman: Unusual price move detected ({kalman.innovation_normalized:.1f}σ, limit {kalman_limit:.1f}σ) — "
                f"price deviating abnormally from trend model, wait for stabilization (bypass 'Kal' to override)"
            )
            blocker = blocker or f"Abnormal price move ({kalman.innovation_normalized:.1f}σ from Kalman trend) — possible gap, news spike, or data error"

        # Gate 7: Composite signal must be BUY or STRONG_BUY — no bypass available
        if layer3.adjusted_signal in ("buy", "strong_buy"):
            passed.append(f"Composite signal: {layer3.adjusted_signal.upper()} — buy confirmed")
        else:
            failed.append(
                f"Composite signal: {layer3.adjusted_signal.upper()} — composite score below buy threshold "
                f"(gate overrides cannot fix this — signal strength must improve)"
            )
            blocker = blocker or (
                f"Composite signal is {layer3.adjusted_signal.upper()} — "
                f"overall momentum/fundamental score is below the buy threshold. "
                f"Gate overrides do not affect this gate. Wait for a stronger signal."
            )

        # Gate 8: EMA(10) trend filter — price must be at or above the 10-day EMA.
        # Prevents buying mid-plunge when short-term momentum is negative.
        # Bypass key: 'ema'
        ema10 = _ema_value(closes, 10) if closes and len(closes) >= 10 else None
        if 'ema' in _bypass:
            passed.append("Trend filter: BYPASSED (EMA10 gate) — user override")
        elif ema10 is None or current_price is None:
            passed.append("Trend filter: skipped — insufficient price history")
        elif current_price >= ema10 * 0.995:
            passed.append(
                f"Trend filter: ${current_price:.2f} >= EMA(10) ${ema10:.2f} — entry with trend"
            )
        else:
            failed.append(
                f"Trend filter: ${current_price:.2f} < EMA(10) ${ema10:.2f} "
                f"({(current_price/ema10 - 1)*100:+.1f}%) — buying into downtrend "
                f"(bypass 'ema' to override)"
            )
            blocker = blocker or (
                f"Price ${current_price:.2f} is below its 10-day EMA ${ema10:.2f} — "
                f"short-term trend is down; wait for price to reclaim EMA or use 'ema' bypass"
            )

        # Gate 9: Margin of Safety hard floor (-50%)
        # Rejects signals where price is >50% above intrinsic value.
        # TSLA/CAT-style DCF failures show -8000%+ MOS — this eliminates them cleanly.
        agg = layer3.incoming_signal if hasattr(layer3, "incoming_signal") else None
        mos = getattr(agg, "margin_of_safety", None)
        if "mos" in _bypass:
            passed.append(f"MOS: BYPASSED ({mos:.0%} vs -50% floor) — user override")
        elif mos is None:
            passed.append("MOS: skipped — no intrinsic value data")
        elif mos >= -0.50:
            passed.append(f"MOS: {mos:.0%} ≥ -50% floor ✓")
        else:
            failed.append(
                f"MOS: {mos:.0%} < -50% floor — price too far above intrinsic value "
                f"(bypass 'mos' to override)"
            )
            blocker = blocker or f"Margin of safety {mos:.0%} below -50% hard floor"

        # Gate 10: Three Green Arrows — momentum/technical confirmation.
        # Require ≥1/3 arrows for a STRONG_BUY-tier composite; ≥2/3 when the composite is
        # weak (< TGA_STRICT_COMPOSITE), because sub-strong_buy buys lean on valuation alone
        # — the pattern the EOD analyzer flagged as "front-running mean reversion." A positive
        # margin of safety counts as ONE confirmation (fundamental buffer): below the cutoff,
        # MOS + one real arrow passes, but MOS + zero arrows no longer does.
        tga_count = getattr(agg, "tga_arrows_count", None)
        composite = getattr(agg, "composite_score", None)
        if "tga" in _bypass:
            passed.append(f"TGA: BYPASSED ({tga_count}/3 arrows) — user override")
        elif tga_count is None:
            passed.append("TGA: skipped — no TGA data")
        else:
            strict     = composite is not None and composite < self.TGA_STRICT_COMPOSITE
            req        = 2 if strict else 1
            mos_credit = 1 if (mos is not None and mos >= 0.0) else 0
            effective  = tga_count + mos_credit
            arrow_parts = []
            if getattr(agg, "tga_sma_arrow",   False): arrow_parts.append("SMA")
            if getattr(agg, "tga_macd_arrow",  False): arrow_parts.append("MACD")
            if getattr(agg, "tga_stoch_arrow", False): arrow_parts.append("Stoch")
            _arrows = ", ".join(arrow_parts) or "active"
            _cstr   = f"{composite:.2f}" if composite is not None else "n/a"

            if tga_count >= req:
                passed.append(
                    f"TGA: {tga_count}/3 arrows ({_arrows}) — momentum confirmed"
                    + (f" [≥2/3 required: weak composite {_cstr}]" if strict else "")
                )
            elif effective >= req:
                # Cleared on the MOS fundamental buffer, which counts as one confirmation.
                passed.append(
                    f"TGA: {tga_count}/3 arrows + MOS {mos:.0%} buffer = {effective}/{req} "
                    f"confirmation"
                    + (f" (weak composite {_cstr} → ≥2/3)" if strict else "")
                    + " ⚠"
                )
            else:
                failed.append(
                    f"TGA: {tga_count}/3 arrows"
                    + (f" + MOS {mos:.0%} buffer" if mos_credit else "")
                    + f" = {effective}/{req} confirmation < {req} required"
                    + (f" — weak composite {_cstr} needs ≥2/3" if strict
                       else " — no momentum/technical confirmation")
                    + " (bypass 'tga' to override)"
                )
                blocker = blocker or (
                    f"Three Green Arrows: {effective}/{req} confirmation — needs ≥{req}/3"
                    + (f" (weak composite {_cstr}; arrows + MOS buffer)" if strict
                       else " momentum confirmation")
                )

        go_no_go = len(failed) == 0 and blocker is None
        return passed, failed, blocker, go_no_go

    def _build_narrative(
        self,
        ticker: str,
        go_no_go: bool,
        reynolds: ReynoldsResult,
        quantum: QuantumStateResult,
        kalman: KalmanResult,
        ensemble: EnsembleForecastResult,
        blocker: Optional[str],
    ) -> str:
        """Generate the CEO-level decision narrative."""
        if go_no_go:
            narrative = (
                f"{ticker} passes all seven decision gates. "
                f"The market is in {reynolds.regime} flow (Re={reynolds.reynolds_number:.2f}), "
                f"indicating {'predictable, trend-following' if reynolds.regime == 'laminar' else 'acceptable'} conditions. "
                f"The quantum state has collapsed to {quantum.dominant_state} with {quantum.state_certainty:.0%} certainty "
                f"and {quantum.interference_type} interference among signals. "
                f"The Kalman filter estimates a {kalman.trend_direction}ward trend at "
                f"${kalman.filtered_price:.2f} with {abs(kalman.innovation_normalized):.1f}σ innovation. "
                f"The ensemble of {len(ensemble.members)} models gives P(Bull)={ensemble.ensemble_probability_bull:.0%} "
                f"with {ensemble.spread_category} spread. "
                f"Kelly Criterion recommends {ensemble.kelly_fraction:.1%} full Kelly → "
                f"{ensemble.recommended_fraction:.1%} at 25% Kelly."
            )
        else:
            narrative = (
                f"{ticker} blocked from execution. "
                f"Primary reason: {blocker or 'Multiple gate failures'}. "
                f"Current market regime: {reynolds.regime} (Re={reynolds.reynolds_number:.2f}). "
                f"Quantum state: {quantum.dominant_state} at {quantum.state_certainty:.0%} certainty. "
                f"Ensemble P(Bull): {ensemble.ensemble_probability_bull:.0%}. "
                f"No capital will be deployed until conditions improve."
            )
        return narrative

    def decide(
        self,
        layer3_result: Layer3Result,
        portfolio_value: Optional[float] = None,
        max_trade_dollars: float = 500.0,
        bypass_gates: set = None,
        live_price: Optional[float] = None,
    ) -> DecisionResult:
        """
        Run all physics models and produce final GO/NO-GO decision.
        live_price: intraday quote to use as current_price (overrides closes[-1]).
        """
        ticker = layer3_result.ticker
        agg_signal: AggregatedSignal = layer3_result.incoming_signal
        logger.info(f"[L4] Running decision engine for {ticker}...")

        all_notes  = []
        all_warnings = list(layer3_result.fud_analysis.warnings)

        with self.Session() as session:
            price_data = self._load_price_data(session, ticker)

        closes  = price_data.get("closes", [])
        highs   = price_data.get("highs", [])
        lows    = price_data.get("lows", [])
        volumes = price_data.get("volumes", [])

        # Prefer live intraday price; fall back to last close or aggregated entry price
        current_price = live_price or (closes[-1] if closes else None) or agg_signal.entry_price
        shares_outstanding = price_data.get("shares_outstanding")  # noqa: F841  # kept for future use

        # ── 1. Reynolds Number ─────────────────────────────────────
        reynolds_result = self.reynolds.analyze(
            ticker=ticker,
            closes=closes, highs=highs, lows=lows, volumes=volumes,
        )
        all_notes.extend(reynolds_result.notes)
        all_warnings.extend(reynolds_result.warnings)

        # ── AI Watch: Reynolds override before ensemble ────────────
        # When TSLA (or any ai_watch_ticker) has a confirmed L3 buy signal,
        # extreme turbulence should not hard-block — cap position at 15% instead.
        is_ai_watch = ticker in config.ai_watch_tickers
        is_confirmed_breakout = layer3_result.adjusted_signal in ("buy", "strong_buy")
        if is_ai_watch and is_confirmed_breakout and not reynolds_result.allow_entry:
            reynolds_result.allow_entry = True
            reynolds_result.position_multiplier = 0.15
            logger.info(
                f"[L4] {ticker}: AI Watch Reynolds override — "
                f"extreme turbulence bypassed, position capped at 15%"
            )

        # ── 2. Quantum State ───────────────────────────────────────
        quantum_result = self.quantum.compute(
            fundamental_score=agg_signal.fundamentals_score,
            technical_score=agg_signal.technical_score,
            insider_score=agg_signal.insider_score,
            news_quality_score=layer3_result.fud_analysis.avg_fud_score,
            cycle_score=agg_signal.cycle_score,
        )
        all_notes.extend(quantum_result.notes)

        # ── 3. Kalman Filter ───────────────────────────────────────
        kalman_result = self.kalman_flt.filter(prices=closes)
        all_notes.extend(kalman_result.notes)

        # ── 4. Ensemble + Kelly ────────────────────────────────────
        quantum_score = self._compute_quantum_score(quantum_result)
        kalman_score  = self._compute_kalman_score(kalman_result)

        rr_ratio = agg_signal.risk_reward_ratio or 2.0
        stop_loss = agg_signal.stop_loss
        tp1 = agg_signal.take_profit_1
        tp2 = agg_signal.take_profit_2

        ensemble_result = self.ensemble.run(
            ticker=ticker,
            fundamental_score=agg_signal.fundamentals_score,
            quantum_score=quantum_score,
            kalman_score=kalman_score,
            reynolds_position_mult=reynolds_result.position_multiplier,
            reynolds_regime=reynolds_result.regime,
            technical_score=agg_signal.technical_score,
            insider_score=agg_signal.insider_score,
            momentum_score=getattr(agg_signal, "momentum_score", 0.0),
            win_probability_estimate=quantum_result.p_bull,
            risk_reward_ratio=rr_ratio,
            current_price=current_price,
            portfolio_value=portfolio_value,
            max_trade_dollars=max_trade_dollars,
            max_position_pct=config.risk.max_position_pct,
        )
        all_notes.extend(ensemble_result.notes)
        all_warnings.extend(ensemble_result.warnings)

        # ── 5. Run Gates ───────────────────────────────────────────
        passed, failed, blocker, go_no_go = self._run_gates(
            layer3_result, reynolds_result, quantum_result,
            kalman_result, ensemble_result, rr_ratio,
            bypass_gates=bypass_gates,
            closes=closes,
            current_price=current_price,
        )

        # ── 6. Position specification ──────────────────────────────
        effective_pct = ensemble_result.effective_position_pct if go_no_go else 0.0
        shares = 0
        max_dollar_risk = None

        if go_no_go and current_price and current_price > 0:
            if portfolio_value:
                dollar_amount = min(effective_pct * portfolio_value, max_trade_dollars)
            else:
                dollar_amount = max_trade_dollars * effective_pct / config.risk.max_position_pct
            shares = max(1, int(dollar_amount / current_price))
            if stop_loss and current_price:
                max_dollar_risk = (current_price - stop_loss) * shares

        # ── 7. Action classification ───────────────────────────────
        if go_no_go and layer3_result.adjusted_signal == "strong_buy":
            action = "BUY"
        elif go_no_go and layer3_result.adjusted_signal == "buy":
            action = "BUY"
        elif layer3_result.adjusted_signal in ("sell", "strong_sell"):
            action = "SELL"
        elif blocker and "turbulence" in blocker.lower():
            action = "WAIT"
        else:
            action = "HOLD"

        # Combined confidence
        decision_confidence = (
            quantum_result.state_certainty *
            ensemble_result.calibrated_confidence *
            (1.0 - ensemble_result.ensemble_spread / 2.0)
        )

        narrative = self._build_narrative(
            ticker, go_no_go, reynolds_result, quantum_result,
            kalman_result, ensemble_result, blocker
        )

        logger.info(
            f"[L4] {ticker}: {action} | go={go_no_go} | "
            f"Re={reynolds_result.reynolds_number:.2f} ({reynolds_result.regime}) | "
            f"Q={quantum_result.dominant_state} ({quantum_result.state_certainty:.0%}) | "
            f"K_trend={kalman_result.trend_direction} | "
            f"P(bull)={ensemble_result.ensemble_probability_bull:.0%} | "
            f"shares={shares} | pos={effective_pct:.1%}"
        )

        result = DecisionResult(
            ticker=ticker,
            timestamp=datetime.utcnow().isoformat(),
            action=action,
            decision_confidence=decision_confidence,
            go_no_go=go_no_go,
            recommended_shares=shares,
            recommended_position_pct=effective_pct,
            entry_price=current_price,
            stop_loss=stop_loss,
            take_profit_1=tp1,
            take_profit_2=tp2,
            risk_reward_ratio=rr_ratio,
            max_dollar_risk=max_dollar_risk,
            reynolds_regime=reynolds_result.regime,
            reynolds_number=reynolds_result.reynolds_number,
            reynolds_position_mult=reynolds_result.position_multiplier,
            quantum_dominant_state=quantum_result.dominant_state,
            quantum_certainty=quantum_result.state_certainty,
            quantum_interference=quantum_result.interference_type,
            kalman_trend=kalman_result.trend_direction,
            kalman_price=kalman_result.filtered_price,
            kalman_innovation_sigma=kalman_result.innovation_normalized,
            ensemble_probability_bull=ensemble_result.ensemble_probability_bull,
            ensemble_spread_category=ensemble_result.spread_category,
            kelly_fraction=ensemble_result.kelly_fraction,
            kelly_recommended_fraction=ensemble_result.recommended_fraction,
            price_target_bear=ensemble_result.p10_outcome,
            price_target_base=ensemble_result.p50_outcome,
            price_target_bull=ensemble_result.p90_outcome,
            gates_passed=passed,
            gates_failed=failed,
            blocking_reason=blocker,
            decision_narrative=narrative,
            physics_notes=all_notes,
            warnings=all_warnings,
        )

        self._write_signal(result, layer3_result)
        return result

    def _write_signal(self, result: "DecisionResult", layer3_result: Layer3Result) -> None:
        """Persist signal to trade_signals table for dashboard and audit trail."""
        try:
            agg = layer3_result.incoming_signal
            with self.Session() as session:
                company = session.query(Company).filter_by(ticker=result.ticker).first()
                if not company:
                    return
                signal = TradeSignal(
                    company_id=company.id,
                    signal=result.action,
                    confidence=result.decision_confidence,
                    margin_of_safety=agg.margin_of_safety,
                    intrinsic_value_estimate=agg.intrinsic_value_conservative,
                    fud_score=layer3_result.fud_analysis.avg_fud_score,
                    current_price=result.entry_price,
                    suggested_position_pct=result.recommended_position_pct,
                    reasoning=json.dumps({
                        "narrative": result.decision_narrative,
                        "composite_score": agg.composite_score,
                        "momentum_score": getattr(agg, "momentum_score", 0.0),
                        "rvol": getattr(agg, "rvol", 1.0),
                        "is_52w_breakout": getattr(agg, "is_52w_breakout", False),
                        "macd_signal_direction": getattr(agg, "macd_signal_direction", "neutral"),
                        "tga_arrows": getattr(agg, "tga_arrows_count", 0),
                        "tga_signal": getattr(agg, "tga_signal", "neutral"),
                        "tga_sma": getattr(agg, "tga_sma_arrow", False),
                        "tga_macd": getattr(agg, "tga_macd_arrow", False),
                        "tga_stoch": getattr(agg, "tga_stoch_arrow", False),
                        "tga_vol": getattr(agg, "tga_volume_spike", False),
                        "tga_reason": getattr(agg, "tga_reason", ""),
                        "reynolds_regime": result.reynolds_regime,
                        "quantum_state": result.quantum_dominant_state,
                        "p_bull": result.ensemble_probability_bull,
                        "kelly": result.kelly_fraction,
                        "approved": result.go_no_go,
                        "action": result.action,
                        "why_buy": " | ".join(agg.why_buy) if agg.why_buy else "",
                        "why_wait": " | ".join(agg.why_wait) if agg.why_wait else "",
                        "gates_passed": result.gates_passed,
                        "gates_failed": result.gates_failed,
                        "blocking_reason": result.blocking_reason,
                    }),
                    acted_on=False,
                )
                session.add(signal)
                session.commit()
        except Exception as e:
            logger.warning(f"[L4] Could not write signal for {result.ticker}: {e}")

    def run_watchlist(
        self,
        layer3_results: dict,        # {ticker: Layer3Result}
        portfolio_value: Optional[float] = None,
        max_trade_dollars: float = 500.0,
        gate_overrides: dict = None,  # {ticker: set of gate names to bypass}
        live_prices: dict = None,    # {ticker: float} — intraday quotes for current_price
    ) -> dict:
        """Run Layer 4 for all tickers. Returns {ticker: DecisionResult}."""
        results = {}
        for ticker, l3 in layer3_results.items():
            try:
                _go = gate_overrides or {}
                bypasses = set(_go.get(ticker, [])) | set(_go.get("*", []))
                live_price = (live_prices or {}).get(ticker)
                result = self.decide(l3, portfolio_value, max_trade_dollars,
                                     bypass_gates=bypasses, live_price=live_price)
                results[ticker] = result
            except Exception as e:
                logger.error(f"[L4] Decision failed for {ticker}: {e}")

        # CEO summary table
        logger.info("\n" + "╔" + "═"*80 + "╗")
        logger.info("║  LAYER 4 DECISION ENGINE — WATCHLIST SUMMARY" + " "*35 + "║")
        logger.info("╠" + "═"*80 + "╣")
        logger.info(
            f"║  {'Ticker':<8} {'Action':<8} {'Re Regime':<14} {'Quantum':<12} "
            f"{'P(Bull)':>8} {'Kelly':>7} {'Shares':>7} {'GO':>4}  ║"
        )
        logger.info("╠" + "═"*80 + "╣")
        for ticker, r in sorted(results.items()):
            logger.info(
                f"║  {ticker:<8} {r.action:<8} {r.reynolds_regime:<14} "
                f"{r.quantum_dominant_state:<12} "
                f"{r.ensemble_probability_bull:>8.0%} "
                f"{r.kelly_fraction:>7.1%} "
                f"{r.recommended_shares:>7} "
                f"{'✅' if r.go_no_go else '❌':>4}  ║"
            )
        logger.info("╚" + "═"*80 + "╝")

        return results
