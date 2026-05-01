"""
analysis/decision_engine.py — Layer 4: Decision Engine.

Only generates a trade signal when ALL three conditions align:
  1. Fundamentals pass — ROIC > WACC, moat detected, positive owner earnings
  2. News quality passes — FUD score >= threshold (default 0.60)
  3. Price is attractive — margin of safety >= threshold (default 15%)

This is the final intelligence layer before risk management and execution.

Usage:
    python -m analysis.decision_engine --ticker AAPL
    python -m analysis.decision_engine            # Full watchlist
"""

import json
import argparse
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional

from loguru import logger

from config import config
from models.database import init_db, Company, TradeSignal
from analysis.engine import FirstPrinciplesEngine, AnalysisReport
from analysis.fud_filter import FUDFilter


@dataclass
class TradeDecision:
    ticker: str
    signal: str                     # "BUY", "SELL", "HOLD"
    confidence: float               # 0.0 to 1.0
    generated_at: str

    # What drove the decision
    roic: Optional[float]
    wacc: Optional[float]
    roic_beats_wacc: bool
    moat_strength: str
    moat_score: float
    margin_of_safety: Optional[float]
    intrinsic_value: Optional[float]
    current_price: Optional[float]
    fud_score: float
    n_news_articles: int
    n_sec_filings: int
    n_insider_buys: int

    # Gate results
    fundamentals_pass: bool
    fud_pass: bool
    valuation_pass: bool

    reasoning: str                  # Human-readable summary

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)

    def summary(self) -> str:
        gate_str = (
            f"  Fundamentals : {'✅' if self.fundamentals_pass else '❌'}\n"
            f"  News Quality : {'✅' if self.fud_pass else '❌'} (score={self.fud_score:.2f})\n"
            f"  Valuation    : {'✅' if self.valuation_pass else '❌'} "
            f"(MoS={self.margin_of_safety:.1%})" if self.margin_of_safety is not None
            else f"  Valuation    : {'✅' if self.valuation_pass else '❌'} (MoS=N/A)"
        )
        return (
            f"{'═'*50}\n"
            f"  {self.ticker}  →  {self.signal}  (confidence: {self.confidence:.0%})\n"
            f"{'─'*50}\n"
            f"{gate_str}\n"
            f"  ROIC {(self.roic or 0):.1%} vs WACC {(self.wacc or 0):.1%} | "
            f"Moat: {self.moat_strength}\n"
            f"  {self.reasoning}\n"
            f"{'═'*50}"
        )


class DecisionEngine:
    """
    Layer 4: Combines Layer 2 (fundamentals) + Layer 3 (FUD) into a trade signal.
    Writes TradeSignal records to the database for audit trail.
    """

    def __init__(self, db_session_factory):
        self.Session = db_session_factory
        self.analysis_engine = FirstPrinciplesEngine(db_session_factory)
        self.fud_filter = FUDFilter(db_session_factory)

    def _fundamentals_gate(self, report: AnalysisReport) -> tuple:
        """
        Returns (passes: bool, reasons: list).
        Hard gates: ROIC > WACC, moat exists, positive owner earnings.
        """
        reasons = []
        passes = True

        # Gate: ROIC > WACC
        if report.roic is not None and report.wacc is not None:
            if report.roic > report.wacc:
                reasons.append(f"ROIC {report.roic:.1%} > WACC {report.wacc:.1%}")
            else:
                reasons.append(f"ROIC {report.roic:.1%} ≤ WACC {report.wacc:.1%} — value destruction")
                passes = False
        else:
            reasons.append("ROIC or WACC unavailable")
            passes = False

        # Gate: moat
        if report.moat_strength == "none":
            reasons.append("No moat detected — commodity risk")
            passes = False
        else:
            reasons.append(f"{report.moat_strength.capitalize()} moat detected")

        # Gate: positive owner earnings
        if report.owner_earnings is not None:
            if report.owner_earnings > 0:
                reasons.append(f"Positive owner earnings: ${report.owner_earnings/1e9:.2f}B")
            else:
                reasons.append("Negative owner earnings — cash burn")
                passes = False
        else:
            reasons.append("Owner earnings unavailable")
            passes = False

        return passes, reasons

    def evaluate(self, ticker: str, fud_lookback_days: int = 7) -> Optional[TradeDecision]:
        """
        Run full decision logic for one ticker.
        Returns TradeDecision or None if analysis data is unavailable.
        """
        logger.info(f"[L4] Evaluating {ticker}...")

        # ── Layer 2: Fundamentals ─────────────────────────────
        report = self.analysis_engine.analyze_ticker(ticker)
        if not report:
            logger.warning(f"[L4] No analysis report for {ticker}")
            return None

        # ── Layer 3: FUD score ────────────────────────────────
        fud_result = self.fud_filter.score_ticker(ticker, days=fud_lookback_days)
        fud_score = fud_result["score"]

        # ── Gate 1: Fundamentals ──────────────────────────────
        fundamentals_pass, fund_reasons = self._fundamentals_gate(report)

        # ── Gate 2: News quality ──────────────────────────────
        fud_pass = fud_score >= config.risk.min_fud_score
        fud_reason = (
            f"News quality {fud_score:.2f} ≥ {config.risk.min_fud_score:.2f}"
            if fud_pass else
            f"News quality {fud_score:.2f} < {config.risk.min_fud_score:.2f} — FUD environment"
        )

        # ── Gate 3: Valuation / margin of safety ─────────────
        mos = report.margin_of_safety
        valuation_pass = mos is not None and mos >= config.risk.min_margin_of_safety
        val_reason = (
            f"Margin of safety {mos:.1%} ≥ {config.risk.min_margin_of_safety:.1%}"
            if valuation_pass else
            f"Margin of safety {f'{mos:.1%}' if mos is not None else 'N/A'} "
            f"< {config.risk.min_margin_of_safety:.1%} minimum"
        )

        # ── Signal determination ──────────────────────────────
        all_pass = fundamentals_pass and fud_pass and valuation_pass

        if all_pass:
            signal = "BUY"
        elif not fundamentals_pass:
            signal = "HOLD"  # Fundamentals broken — don't sell on FUD alone
        else:
            signal = "HOLD"

        # ── Confidence: min of the two continuous scores ──────
        fud_confidence = fud_score
        val_confidence = min(1.0, (mos / 0.30)) if mos and mos > 0 else 0.0
        fund_confidence = report.moat_score if fundamentals_pass else 0.0
        confidence = min(fud_confidence, val_confidence, fund_confidence) if all_pass else 0.0

        reasoning = " | ".join(fund_reasons + [fud_reason, val_reason])

        decision = TradeDecision(
            ticker=ticker,
            signal=signal,
            confidence=confidence,
            generated_at=datetime.utcnow().isoformat(),

            roic=report.roic,
            wacc=report.wacc,
            roic_beats_wacc=(report.roic or 0) > (report.wacc or 0),
            moat_strength=report.moat_strength,
            moat_score=report.moat_score,
            margin_of_safety=mos,
            intrinsic_value=report.intrinsic_value_conservative,
            current_price=report.current_price,
            fud_score=fud_score,
            n_news_articles=fud_result["n_articles"],
            n_sec_filings=fud_result["n_sec_filings"],
            n_insider_buys=fud_result["n_insider_buys"],

            fundamentals_pass=fundamentals_pass,
            fud_pass=fud_pass,
            valuation_pass=valuation_pass,

            reasoning=reasoning,
        )

        # ── Persist signal to DB ──────────────────────────────
        self._persist_signal(decision, report)

        logger.info(
            f"[L4] {ticker}: {signal} | conf={confidence:.2f} | "
            f"Gates: fund={'✅' if fundamentals_pass else '❌'} "
            f"fud={'✅' if fud_pass else '❌'} "
            f"val={'✅' if valuation_pass else '❌'}"
        )

        return decision

    def _persist_signal(self, decision: TradeDecision, report: AnalysisReport):
        """Write TradeSignal record to database for audit trail."""
        with self.Session() as session:
            company = session.query(Company).filter_by(ticker=decision.ticker).first()
            if not company:
                return

            signal_record = TradeSignal(
                company_id=company.id,
                signal=decision.signal,
                confidence=decision.confidence,
                generated_at=datetime.utcnow(),
                roic=decision.roic,
                wacc_estimate=decision.wacc,
                margin_of_safety=decision.margin_of_safety,
                fud_score=decision.fud_score,
                intrinsic_value_estimate=decision.intrinsic_value,
                current_price=decision.current_price,
                suggested_position_pct=config.risk.max_position_pct * decision.confidence,
                reasoning=decision.to_json(),
                acted_on=False,
            )
            session.add(signal_record)
            session.commit()

    def evaluate_watchlist(self, tickers: Optional[list] = None) -> dict:
        """Evaluate all tickers. Returns {ticker: TradeDecision}."""
        tickers = tickers or config.watchlist
        decisions = {}

        for ticker in tickers:
            try:
                d = self.evaluate(ticker)
                if d:
                    decisions[ticker] = d
            except Exception as e:
                logger.error(f"[L4] Decision failed for {ticker}: {e}")

        # Summary table
        logger.info("\n" + "═" * 65)
        logger.info(f"{'Ticker':<8} {'Signal':<6} {'Conf':>5} {'MoS':>7} {'FUD':>5} {'Gates'}")
        logger.info("─" * 65)
        for ticker, d in sorted(decisions.items()):
            mos = f"{d.margin_of_safety:.1%}" if d.margin_of_safety is not None else "N/A"
            gates = (
                f"{'✅' if d.fundamentals_pass else '❌'}"
                f"{'✅' if d.fud_pass else '❌'}"
                f"{'✅' if d.valuation_pass else '❌'}"
            )
            logger.info(
                f"{ticker:<8} {d.signal:<6} {d.confidence:>4.0%} "
                f"{mos:>7} {d.fud_score:>4.2f} {gates}"
            )
        logger.info("═" * 65)

        buy_signals = [t for t, d in decisions.items() if d.signal == "BUY"]
        if buy_signals:
            logger.info(f"BUY signals: {buy_signals}")
        else:
            logger.info("No BUY signals generated this run.")

        return decisions


# ── CLI ───────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    parser = argparse.ArgumentParser(description="Layer 4: Decision Engine")
    parser.add_argument("--ticker", type=str, help="Single ticker (default: watchlist)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    _, Session = init_db(config.database.url)
    engine = DecisionEngine(db_session_factory=Session)

    if args.ticker:
        decision = engine.evaluate(args.ticker.upper())
        if decision:
            print(decision.to_json() if args.json else decision.summary())
    else:
        decisions = engine.evaluate_watchlist()
        if args.json:
            print(json.dumps({t: json.loads(d.to_json()) for t, d in decisions.items()}, indent=2))
