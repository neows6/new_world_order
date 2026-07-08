"""
risk/manager.py — Layer 5: Risk Manager.

═══════════════════════════════════════════════════════════════════════
ROLE OF THE RISK MANAGER

The Risk Manager is the last line of defense before capital is deployed.
It receives the Layer 4 decision (GO signal with position size) and asks:

  "Even if everything says GO — is this the RIGHT SIZE at the RIGHT TIME?"

It combines:
  1. Hard risk rules (absolute dollar limits, portfolio concentration)
  2. Behavioral psychology (human nature headwinds/tailwinds)
  3. Position sizing refinement (Kelly + Reynolds already applied in L4)
  4. Stop loss and take profit placement (using psychology levels)
  5. Portfolio-level risk (total exposure, correlation, drawdown tracking)
  6. Final go/no-go override authority

The Risk Manager CAN override Layer 4's GO decision.
It CANNOT override Layer 4's NO-GO decision.

═══════════════════════════════════════════════════════════════════════
"""

import json
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional

from loguru import logger

from config import config
from models.database import Company, PriceHistory, TradeLog
from decision.engine import DecisionResult
from risk.behavioral_psychology import BehavioralPsychologyEngine, BehavioralPsychologyResult


@dataclass
class RiskAssessment:
    """
    Complete risk assessment — output of the Risk Manager.
    This is what Layer 6 (Executor) reads.
    """
    ticker: str
    timestamp: str

    # Final position specification (post-risk-manager adjustments)
    approved: bool                    # Final approval to execute
    action: str                       # BUY / SELL / HOLD / WAIT

    # Position
    final_shares: int
    final_position_pct: float
    final_dollar_amount: float

    # Entry spec
    entry_price: Optional[float]
    stop_loss: Optional[float]        # Psychology-adjusted stop
    take_profit_1: Optional[float]    # First target
    take_profit_2: Optional[float]    # Second target
    take_profit_3: Optional[float]    # Stretch target (at next round number)

    # Risk metrics
    dollar_risk: Optional[float]      # (entry - stop) × shares
    pct_risk: Optional[float]         # dollar_risk / portfolio_value
    risk_reward_ratio: Optional[float]

    # Behavioral psychology overlay
    behavioral_score: float
    dominant_human_bias: str
    human_edge: str
    entry_timing: str                 # "good", "early", "late", "avoid"

    # Portfolio-level risk
    current_portfolio_exposure_pct: float   # How much is already deployed
    new_total_exposure_pct: float           # After this trade
    exposure_within_limits: bool

    # Behavioral adjustments made
    position_behavioral_adjustment: float   # How much psychology changed size
    stop_adjusted_for_psychology: bool      # Did we widen/tighten stop?
    targets_adjusted_for_psychology: bool

    # Limit checks
    within_dollar_limit: bool
    within_position_pct_limit: bool
    within_daily_trade_limit: bool

    # Warnings and reasoning
    risk_warnings: list
    adjustment_notes: list
    final_narrative: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)

    def summary(self) -> str:
        status = "✅ APPROVED" if self.approved else "❌ BLOCKED"
        lines = [
            f"{'╔' + '═'*62 + '╗'}",
            f"║  LAYER 5 RISK MANAGER — {self.ticker:<35}║",
            f"{'╚' + '═'*62 + '╝'}",
            "",
            f"  {status} — {self.action}",
            "",
            "FINAL POSITION:",
            f"  Shares:    {self.final_shares}",
            f"  Amount:    ${self.final_dollar_amount:,.2f} ({self.final_position_pct:.1%})",
            f"  Entry:     ${self.entry_price:.2f}" if self.entry_price else "  Entry: N/A",
            f"  Stop:      ${self.stop_loss:.2f}" if self.stop_loss else "  Stop: N/A",
            f"  Target 1:  ${self.take_profit_1:.2f}" if self.take_profit_1 else "  T1: N/A",
            f"  Target 2:  ${self.take_profit_2:.2f}" if self.take_profit_2 else "  T2: N/A",
            f"  Target 3:  ${self.take_profit_3:.2f}" if self.take_profit_3 else "  T3: N/A",
            f"  R/R:       {self.risk_reward_ratio:.1f}:1" if self.risk_reward_ratio else "  R/R: N/A",
            f"  Max Risk:  ${self.dollar_risk:.2f}" if self.dollar_risk else "  Max Risk: N/A",
            "",
            "HUMAN NATURE ANALYSIS:",
            f"  Bias:      {self.dominant_human_bias.replace('_', ' ')}",
            f"  Timing:    {self.entry_timing.upper()}",
            f"  Score:     {self.behavioral_score:+.2f}",
            f"  Edge:      {self.human_edge[:80]}",
            "",
            "PORTFOLIO EXPOSURE:",
            f"  Current:   {self.current_portfolio_exposure_pct:.1%}",
            f"  After trade: {self.new_total_exposure_pct:.1%}",
            f"  Within limits: {'✓' if self.exposure_within_limits else '✗'}",
            "",
        ]

        if self.risk_warnings:
            lines.append("WARNINGS:")
            for w in self.risk_warnings:
                lines.append(f"  ⚠ {w}")
            lines.append("")

        if self.adjustment_notes:
            lines.append("ADJUSTMENTS MADE:")
            for n in self.adjustment_notes:
                lines.append(f"  → {n}")
            lines.append("")

        lines.append(f"NARRATIVE:\n  {self.final_narrative}")
        return "\n".join(lines)


class RiskManager:
    """
    Layer 5 — Risk Manager.
    Applies behavioral psychology overlay, hard limits, and portfolio-level
    risk controls to the Layer 4 decision.
    """

    # Hard limits (override all other signals)
    MAX_PORTFOLIO_EXPOSURE   = 0.40   # Never deploy more than 40% total
    MAX_SINGLE_POSITION_PCT  = 0.10   # 10% max per stock
    MIN_RR_RATIO             = 1.5    # Must have 1.5:1 R/R minimum
    # Per-trade hard caps — sourced from config so they can be tuned without code changes
    # MAX_DOLLAR_PER_TRADE  → config.risk.max_trade_dollars
    # MAX_DAILY_TRADES      → config.risk.max_daily_trades
    # MAX_SHARES_PER_TRADE  → config.risk.max_shares_per_trade

    # Behavioral psychology impact limits
    MAX_BEHAVIORAL_BOOST     = 0.25   # Max 25% position increase from psych
    MAX_BEHAVIORAL_REDUCTION = 0.50   # Max 50% position reduction from psych

    def __init__(self, db_session_factory):
        self.Session = db_session_factory
        self.behavioral = BehavioralPsychologyEngine()

    def _get_current_exposure(self, session) -> float:
        """Get current open position exposure as % of portfolio."""
        try:
            # Count recent live (non-dry-run) trades that haven't been exited
            open_trades = (
                session.query(TradeLog)
                .filter(
                    TradeLog.dry_run == False,
                    TradeLog.action == "BUY",
                    TradeLog.status == "FILLED",
                )
                .all()
            )
            return min(0.40, len(open_trades) * 0.025)   # Estimate 2.5% per trade
        except Exception:
            return 0.10   # Conservative default

    def _get_todays_trade_count(self, session) -> int:
        """Count trades executed today."""
        try:
            today_start = datetime.now().replace(hour=0, minute=0, second=0)
            count = (
                session.query(TradeLog)
                .filter(TradeLog.created_at >= today_start)
                .count()
            )
            return count
        except Exception:
            return 0

    def _load_price_context(self, session, ticker: str) -> dict:
        """Load price data for behavioral analysis."""
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

        closes = [r.adjusted_close or r.close for r in records if (r.adjusted_close or r.close)]
        highs  = [r.high for r in records if r.high]
        lows   = [r.low for r in records if r.low]
        vols   = [r.volume for r in records if r.volume]

        high_52w = max(highs[-252:]) if len(highs) >= 252 else (max(highs) if highs else closes[-1])
        low_52w  = min(lows[-252:]) if len(lows) >= 252 else (min(lows) if lows else closes[-1])

        # Days since 52-week high
        try:
            high_52w_idx = highs[-252:].index(high_52w) if len(highs) >= 252 else highs.index(high_52w)
            days_since = len(highs[-252:]) - high_52w_idx - 1 if len(highs) >= 252 else len(highs) - high_52w_idx - 1
        except (ValueError, IndexError):
            days_since = None

        return {
            "closes": closes,
            "highs": highs,
            "lows": lows,
            "volumes": vols,
            "high_52w": high_52w,
            "low_52w": low_52w,
            "days_since_52w_high": days_since,
        }

    def _adjust_stops_for_psychology(
        self,
        entry_price: float,
        raw_stop: float,
        behavioral: BehavioralPsychologyResult,
    ) -> float:
        """
        Adjust stop loss based on psychological levels.
        
        Key rule: Place stops just BELOW round number support or just BELOW
        the previous session low — NOT at round numbers themselves.
        (Everyone else puts stops AT round numbers, so they get swept.)
        """
        round_below = behavioral.round_number.nearest_round_below

        # If the raw stop is within 1% of a round number, move it just below
        # to avoid the stop-sweep zone
        if round_below > 0 and abs(raw_stop - round_below) / round_below < 0.01:
            adjusted = round_below * 0.985   # 1.5% below the round number
            logger.debug(f"Stop adjusted below round number ${round_below}: ${raw_stop:.2f} → ${adjusted:.2f}")
            return adjusted

        # In PANIC herding: widen stop (panic may go further before reversing)
        if behavioral.herding.market_psychology == "panic":
            return raw_stop * 0.97   # Widen by 3%

        # Near 52-week high: tighter stop (if it fails, fail fast)
        if behavioral.fifty_two_week.momentum_category == "near_high":
            return raw_stop * 1.01   # Slightly tighter

        return raw_stop

    def _compute_psychology_adjusted_targets(
        self,
        entry_price: float,
        tp1: float,
        tp2: float,
        behavioral: BehavioralPsychologyResult,
    ) -> tuple:
        """
        Adjust take-profit targets based on psychology.
        Set TP3 at the next significant round number above current price.
        """
        tp3 = None

        # TP3: next major round number above price (stretch target)
        next_round = behavioral.round_number.nearest_round_above
        if next_round > tp2:
            # Set TP3 just below the round number (before resistance cluster)
            tp3 = next_round * 0.99
        elif next_round > tp1:
            # Round number between TP1 and TP2 — adjust TP2 to just below it
            tp2 = min(tp2, next_round * 0.99)

        # Panic herding = contrarian buy — first target is quick reversal
        if behavioral.herding.market_psychology == "panic":
            # In panic, first target = move back to VWAP or recent consolidation
            quick_tp = entry_price * 1.03   # 3% quick bounce target
            if quick_tp < tp1:
                tp1 = quick_tp

        # Breakout above 52-week high = extend targets (momentum)
        if behavioral.fifty_two_week.momentum_category == "breakout":
            tp1 = max(tp1, entry_price * 1.05)
            tp2 = max(tp2, entry_price * 1.10)
            if not tp3:
                tp3 = entry_price * 1.15

        return tp1, tp2, tp3

    def assess(
        self,
        decision: DecisionResult,
        portfolio_value: Optional[float] = None,
        market_returns: Optional[list] = None,
    ) -> RiskAssessment:
        """
        Run full risk assessment on a Layer 4 decision.
        Returns RiskAssessment with final position specification.
        """
        ticker = decision.ticker
        logger.info(f"[L5] Risk assessment for {ticker}...")

        warnings = []
        adj_notes = []

        with self.Session() as session:
            current_exposure  = self._get_current_exposure(session)
            today_trade_count = self._get_todays_trade_count(session)
            price_ctx         = self._load_price_context(session, ticker)

        # ── Behavioral psychology overlay ──────────────────────────
        closes = price_ctx.get("closes", [decision.entry_price or 100])
        behavioral_result = self.behavioral.analyze(
            ticker=ticker,
            current_price=decision.entry_price or (closes[-1] if closes else 100),
            price_history=closes,
            high_52w=price_ctx.get("high_52w", (decision.entry_price or 100) * 1.15),
            low_52w=price_ctx.get("low_52w", (decision.entry_price or 100) * 0.85),
            market_returns=market_returns or [],
            reference_price=None,
            all_time_high=None,
            days_since_52w_high=price_ctx.get("days_since_52w_high"),
        )

        # ── Apply behavioral adjustment to position ────────────────
        behav_adj = behavioral_result.position_adjustment
        # Clamp behavioral adjustment
        behav_adj = max(1 - self.MAX_BEHAVIORAL_REDUCTION,
                        min(1 + self.MAX_BEHAVIORAL_BOOST, behav_adj))

        base_shares   = decision.recommended_shares
        base_pct      = decision.recommended_position_pct
        base_dollars  = (base_pct * portfolio_value) if portfolio_value else (base_shares * (decision.entry_price or 100))

        # Apply behavioral adjustment
        adjusted_dollars = base_dollars * behav_adj
        if behav_adj != 1.0:
            adj_notes.append(
                f"Behavioral adjustment: {behav_adj:.0%} of base position "
                f"(bias: {behavioral_result.dominant_bias.replace('_', ' ')})"
            )

        # ── Hard dollar limit ──────────────────────────────────────
        max_dollars = min(config.risk.max_trade_dollars, config.risk.max_position_pct * (portfolio_value or 10000))
        if adjusted_dollars > max_dollars:
            adj_notes.append(f"Dollar cap applied: ${adjusted_dollars:.0f} → ${max_dollars:.0f}")
            adjusted_dollars = max_dollars

        # ── Portfolio exposure check ───────────────────────────────
        new_exposure_pct = current_exposure + (adjusted_dollars / portfolio_value if portfolio_value else 0.025)
        within_exposure = new_exposure_pct <= self.MAX_PORTFOLIO_EXPOSURE

        if not within_exposure:
            warnings.append(
                f"Portfolio exposure {new_exposure_pct:.0%} would exceed "
                f"{self.MAX_PORTFOLIO_EXPOSURE:.0%} limit"
            )
            # Scale down to fit within limit
            headroom = max(0, self.MAX_PORTFOLIO_EXPOSURE - current_exposure)
            adjusted_dollars = headroom * (portfolio_value or 10000)
            adj_notes.append(f"Scaled down for exposure limit: ${adjusted_dollars:.0f}")

        # ── Daily trade limit ──────────────────────────────────────
        within_daily = today_trade_count < config.risk.max_daily_trades
        if not within_daily:
            warnings.append(f"Daily trade limit ({config.risk.max_daily_trades}) reached")

        # ── Compute final shares ───────────────────────────────────
        entry_price = decision.entry_price or (closes[-1] if closes else 100)
        final_shares = max(1, int(adjusted_dollars / entry_price)) if entry_price > 0 else 0

        # ── Hard share quantity ceiling ────────────────────────────
        if final_shares > config.risk.max_shares_per_trade:
            adj_notes.append(
                f"Share cap applied: {final_shares} → {config.risk.max_shares_per_trade} shares"
            )
            final_shares = config.risk.max_shares_per_trade

        final_dollars = final_shares * entry_price
        final_pct = final_dollars / portfolio_value if portfolio_value and portfolio_value > 0 else 0

        # ── Psychology-adjusted stops and targets ──────────────────
        raw_stop = decision.stop_loss or (entry_price * 0.97)
        adjusted_stop = self._adjust_stops_for_psychology(entry_price, raw_stop, behavioral_result)
        stop_adjusted = adjusted_stop != raw_stop
        if stop_adjusted:
            adj_notes.append(
                f"Stop adjusted for psychology: ${raw_stop:.2f} → ${adjusted_stop:.2f} "
                f"(avoiding round number sweep zone)"
            )

        tp1, tp2, tp3 = self._compute_psychology_adjusted_targets(
            entry_price,
            decision.take_profit_1 or entry_price * 1.05,
            decision.take_profit_2 or entry_price * 1.10,
            behavioral_result,
        )
        targets_adjusted = (tp1 != decision.take_profit_1 or tp2 != decision.take_profit_2 or tp3 is not None)
        if targets_adjusted:
            adj_notes.append("Targets adjusted for psychological levels and momentum category")

        # ── Risk metrics ───────────────────────────────────────────
        dollar_risk = (entry_price - adjusted_stop) * final_shares if adjusted_stop else None
        pct_risk = dollar_risk / portfolio_value if dollar_risk is not None and portfolio_value else None
        rr = (tp1 - entry_price) / (entry_price - adjusted_stop) if (tp1 and adjusted_stop and entry_price > adjusted_stop) else None

        # R/R check
        within_rr = (rr or 0) >= self.MIN_RR_RATIO
        if not within_rr and rr is not None:
            warnings.append(f"R/R ratio {rr:.1f}:1 below minimum {self.MIN_RR_RATIO}:1")

        # ── Final approval ─────────────────────────────────────────
        # Start from Layer 4's decision
        approved = decision.go_no_go

        # Override if hard limits violated
        if not within_daily:
            approved = False
        if behavioral_result.entry_timing == "avoid":
            approved = False
            warnings.append("Behavioral timing = AVOID — strong human nature headwinds")
        if not within_rr and rr is not None:
            approved = False   # Never enter with bad R/R

        # ── Final narrative ────────────────────────────────────────
        narrative = behavioral_result.human_nature_narrative
        if not approved and decision.go_no_go:
            narrative += (
                f" HOWEVER, risk management overrides the GO signal: "
                f"{'; '.join(warnings) if warnings else 'position sizing concerns'}."
            )
        elif approved:
            narrative += (
                f" Risk manager APPROVES: {final_shares} shares at ${entry_price:.2f}, "
                f"stop ${adjusted_stop:.2f}, T1 ${tp1:.2f}."
            )

        logger.info(
            f"[L5] {ticker}: approved={approved} | "
            f"shares={final_shares} | ${final_dollars:.0f} | "
            f"behavioral={behavioral_result.behavioral_score:+.2f} ({behavioral_result.dominant_bias}) | "
            f"timing={behavioral_result.entry_timing}"
        )

        return RiskAssessment(
            ticker=ticker,
            timestamp=datetime.utcnow().isoformat(),
            approved=approved,
            action=decision.action if approved else "HOLD",
            final_shares=final_shares,
            final_position_pct=final_pct,
            final_dollar_amount=final_dollars,
            entry_price=entry_price,
            stop_loss=adjusted_stop,
            take_profit_1=tp1,
            take_profit_2=tp2,
            take_profit_3=tp3,
            dollar_risk=dollar_risk,
            pct_risk=pct_risk,
            risk_reward_ratio=rr,
            behavioral_score=behavioral_result.behavioral_score,
            dominant_human_bias=behavioral_result.dominant_bias,
            human_edge=behavioral_result.exploitable_edge,
            entry_timing=behavioral_result.entry_timing,
            current_portfolio_exposure_pct=current_exposure,
            new_total_exposure_pct=new_exposure_pct,
            exposure_within_limits=within_exposure,
            position_behavioral_adjustment=behav_adj,
            stop_adjusted_for_psychology=stop_adjusted,
            targets_adjusted_for_psychology=targets_adjusted,
            within_dollar_limit=final_dollars <= config.risk.max_trade_dollars,
            within_position_pct_limit=final_pct <= self.MAX_SINGLE_POSITION_PCT,
            within_daily_trade_limit=within_daily,
            risk_warnings=warnings,
            adjustment_notes=adj_notes,
            final_narrative=narrative,
        )

    def assess_watchlist(
        self,
        decisions: dict,             # {ticker: DecisionResult}
        portfolio_value: Optional[float] = None,
        market_returns: Optional[list] = None,
    ) -> dict:
        """Run Layer 5 risk assessment for all tickers."""
        results = {}
        for ticker, decision in decisions.items():
            try:
                result = self.assess(decision, portfolio_value, market_returns)
                results[ticker] = result
            except Exception as e:
                logger.error(f"[L5] Risk assessment failed for {ticker}: {e}")

        # CEO summary
        logger.info("\n" + "╔" + "═"*75 + "╗")
        logger.info("║  LAYER 5 RISK MANAGER — FINAL DECISIONS" + " "*35 + "║")
        logger.info("╠" + "═"*75 + "╣")
        logger.info(
            f"║  {'Ticker':<8} {'Action':<8} {'Shares':>7} {'$Amount':>9} "
            f"{'Stop':>9} {'T1':>9} {'Bias':<22} {'OK':>4}  ║"
        )
        logger.info("╠" + "═"*75 + "╣")
        for ticker, r in sorted(results.items()):
            logger.info(
                f"║  {ticker:<8} {r.action:<8} {r.final_shares:>7} "
                f"${r.final_dollar_amount:>8,.0f} "
                f"${r.stop_loss:>8.2f}" if r.stop_loss else "║  " + ticker.ljust(8) + r.action.ljust(8) + str(r.final_shares).rjust(7) + f" ${r.final_dollar_amount:>8,.0f}  " + " "*9,
            )
        logger.info("╚" + "═"*75 + "╝")

        return results
