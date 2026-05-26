"""
analysis/engine.py — Layer 2: First Principles Analysis Engine.

Orchestrates all analysis components for a ticker:
  1. Load fundamentals from DB (Layer 1 output)
  2. Compute WACC
  3. Analyze moat
  4. Estimate intrinsic value (DCF)
  5. Produce a structured AnalysisReport

This is the output that feeds into Layer 3 (FUD filter) and
Layer 4 (decision engine).

Usage:
    python -m analysis.engine --ticker AAPL
    python -m analysis.engine  # Runs full watchlist
"""

import json
import argparse
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional

from loguru import logger

from config import config
from models.database import init_db, Company, Fundamental, PriceHistory
from analysis.wacc import WACCEstimator, WACCResult
from analysis.moat_detector import MoatDetector, MoatResult, MoatStrength
from analysis.intrinsic_value import IntrinsicValueEstimator, IntrinsicValueResult


@dataclass
class AnalysisReport:
    """
    Complete first-principles analysis output for a single ticker.
    This is the canonical input to the FUD filter and decision engine.
    """
    ticker: str
    company_name: str
    analyzed_at: str

    # Capital structure
    wacc: Optional[float]
    cost_of_equity: Optional[float]
    beta: Optional[float]

    # Moat
    moat_strength: str              # "wide", "narrow", "none"
    moat_score: float               # 0.0 to 1.0
    moat_types: list
    moat_signals: list
    moat_warnings: list

    # Most recent fundamentals snapshot
    latest_year: Optional[int]
    revenue: Optional[float]
    gross_margin: Optional[float]
    roic: Optional[float]
    owner_earnings: Optional[float]
    free_cash_flow: Optional[float]
    net_debt: Optional[float]
    net_debt_to_ebitda: Optional[float]

    # Valuation
    current_price: Optional[float]
    intrinsic_value_conservative: Optional[float]
    intrinsic_value_base: Optional[float]
    margin_of_safety: Optional[float]
    price_to_intrinsic: Optional[float]
    is_undervalued: bool

    # DCF scenarios
    dcf_bear_iv: Optional[float]
    dcf_base_iv: Optional[float]
    dcf_bull_iv: Optional[float]

    # Qualitative flags
    is_investable: bool             # Passes minimum quality bar
    investable_reasons: list        # Why yes / why no
    analysis_notes: list
    analysis_warnings: list

    # DCF reliability — False for REITs (SIC 65xx) and financial sector (SIC 60-64xx)
    # where discounted cash flow systematically overstates intrinsic value.
    dcf_reliable: bool = True

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)

    def summary(self) -> str:
        """Human-readable one-page summary."""
        lines = [
            f"═══════════════════════════════════════════════",
            f"  {self.ticker} — {self.company_name}",
            f"  Analyzed: {self.analyzed_at}",
            f"═══════════════════════════════════════════════",
            f"",
            f"MOAT: {self.moat_strength.upper()} (score: {self.moat_score:.2f})",
            f"  Types: {', '.join(self.moat_types) or 'none detected'}",
        ]
        for s in self.moat_signals:
            lines.append(f"  ✓ {s}")
        for w in self.moat_warnings:
            lines.append(f"  ⚠ {w}")

        lines += [
            f"",
            f"FUNDAMENTALS (FY{self.latest_year}):",
            f"  Revenue:          ${(self.revenue or 0)/1e9:.2f}B",
            f"  Gross Margin:     {(self.gross_margin or 0):.1%}",
            f"  ROIC:             {(self.roic or 0):.1%}  vs  WACC: {(self.wacc or 0):.1%}",
            f"  ROIC Spread:      {((self.roic or 0) - (self.wacc or 0)):.1%}",
            f"  Owner Earnings:   ${(self.owner_earnings or 0)/1e9:.2f}B",
            f"  FCF:              ${(self.free_cash_flow or 0)/1e9:.2f}B",
            f"  Net Debt/EBITDA:  {self.net_debt_to_ebitda:.2f}x" if self.net_debt_to_ebitda else "  Net Debt/EBITDA:  N/A",
            f"",
            f"VALUATION:",
            f"  Current Price:    ${self.current_price:.2f}" if self.current_price else "  Current Price:    N/A",
            f"  IV (Conservative): ${self.intrinsic_value_conservative:.2f}" if self.intrinsic_value_conservative else "  IV (Conservative): N/A",
            f"  IV (Base):         ${self.intrinsic_value_base:.2f}" if self.intrinsic_value_base else "  IV (Base):         N/A",
            f"  Margin of Safety:  {self.margin_of_safety:.1%}" if self.margin_of_safety is not None else "  Margin of Safety:  N/A",
            f"",
            f"  DCF Bear: ${self.dcf_bear_iv:.2f}  |  Base: ${self.dcf_base_iv:.2f}  |  Bull: ${self.dcf_bull_iv:.2f}" if all([self.dcf_bear_iv, self.dcf_base_iv, self.dcf_bull_iv]) else "",
            f"",
            f"VERDICT: {'✅ INVESTABLE' if self.is_investable else '❌ NOT INVESTABLE'}",
        ]
        for r in self.investable_reasons:
            lines.append(f"  {'✓' if self.is_investable else '✗'} {r}")

        return "\n".join(lines)


class FirstPrinciplesEngine:
    """
    Layer 2 orchestrator. Loads data from DB, runs all analysis,
    produces AnalysisReport per ticker.
    """

    # Minimum quality bar for "investable" classification
    MIN_GROSS_MARGIN           = 0.25    # 25% gross margin floor
    MIN_ROIC                   = 0.08    # 8% ROIC floor (above risk-free rate)
    MAX_NET_DEBT_TO_EBITDA     = 4.0     # Leverage ceiling
    MIN_YEARS_DATA             = 3       # Need at least 3 years of history

    def __init__(self, db_session_factory):
        self.Session = db_session_factory
        self.wacc_estimator = WACCEstimator()
        self.moat_detector = MoatDetector()
        self.iv_estimator = IntrinsicValueEstimator()

    def _load_fundamentals(self, session, company_id: int) -> dict:
        """Load all annual fundamentals for a company, keyed by fiscal year."""
        records = (
            session.query(Fundamental)
            .filter_by(company_id=company_id, fiscal_quarter=0)
            .order_by(Fundamental.fiscal_year)
            .all()
        )
        return {r.fiscal_year: r for r in records}

    def _load_price_history(self, session, company_id: int) -> list:
        """Load price history for beta computation."""
        records = (
            session.query(PriceHistory)
            .filter_by(company_id=company_id)
            .order_by(PriceHistory.date)
            .all()
        )
        return records

    def _compute_price_returns(self, price_records: list) -> list:
        """Convert price history to daily return series."""
        closes = [p.adjusted_close or p.close for p in price_records if (p.adjusted_close or p.close)]
        if len(closes) < 2:
            return []
        return [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, len(closes))]

    def _get_sp500_returns(self, n_days: int) -> list:
        """
        Real S&P 500 daily returns via yfinance, cached in data/spy_returns_cache.json.
        Falls back to synthetic returns if yfinance is unavailable.
        Cache is refreshed if > 7 days old.
        """
        import json as _json
        from pathlib import Path as _Path
        from datetime import datetime as _dt, timedelta as _td

        cache_path = _Path(__file__).resolve().parent.parent / "data" / "spy_returns_cache.json"

        def _load_cache():
            try:
                if cache_path.exists():
                    raw = _json.loads(cache_path.read_text())
                    age_days = (_dt.utcnow() - _dt.fromisoformat(raw["ts"])).days
                    if age_days <= 7:
                        return raw["returns"]
            except Exception:
                pass
            return None

        def _fetch_and_cache():
            try:
                import yfinance as yf
                end = _dt.utcnow().strftime("%Y-%m-%d")
                start = (_dt.utcnow() - _td(days=1260)).strftime("%Y-%m-%d")  # ~5 years
                spy = yf.download("SPY", start=start, end=end, interval="1d",
                                  auto_adjust=True, progress=False)["Close"]
                if spy.empty or len(spy) < 60:
                    return None
                spy_vals = spy.values.tolist()
                returns = [(spy_vals[i] - spy_vals[i-1]) / spy_vals[i-1]
                           for i in range(1, len(spy_vals))]
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(_json.dumps({"ts": _dt.utcnow().isoformat(), "returns": returns}))
                logger.info(f"[ENGINE] SPY beta returns fetched: {len(returns)} days")
                return returns
            except Exception as exc:
                logger.warning(f"[ENGINE] SPY fetch failed, using synthetic returns: {exc}")
                return None

        returns = _load_cache() or _fetch_and_cache()
        if returns:
            return returns[-n_days:] if len(returns) >= n_days else returns
        # Fallback: synthetic
        return [0.10 / 252] * n_days

    def _assess_investability(
        self,
        fundamentals_by_year: dict,
        wacc_result: WACCResult,
        moat_result: MoatResult,
        iv_result: IntrinsicValueResult,
    ) -> tuple:
        """
        Apply minimum quality gates.
        Returns (is_investable: bool, reasons: list)
        """
        reasons = []
        passes = []

        if not fundamentals_by_year:
            return False, ["No fundamental data available"]

        latest = fundamentals_by_year[max(fundamentals_by_year.keys())]

        # Gate 1: Sufficient data history
        if len(fundamentals_by_year) < self.MIN_YEARS_DATA:
            reasons.append(f"Only {len(fundamentals_by_year)} years of data (need {self.MIN_YEARS_DATA}+)")
            passes.append(False)
        else:
            reasons.append(f"{len(fundamentals_by_year)} years of data ✓")
            passes.append(True)

        # Gate 2: Gross margin floor
        if latest.gross_margin and latest.gross_margin >= self.MIN_GROSS_MARGIN:
            reasons.append(f"Gross margin {latest.gross_margin:.1%} above {self.MIN_GROSS_MARGIN:.0%} floor ✓")
            passes.append(True)
        elif latest.gross_margin:
            reasons.append(f"Gross margin {latest.gross_margin:.1%} below {self.MIN_GROSS_MARGIN:.0%} floor")
            passes.append(False)
        else:
            reasons.append("Gross margin data unavailable — gate skipped")
            # Don't append False: missing data is not a disqualifier

        # Gate 3: ROIC floor
        if latest.roic and latest.roic >= self.MIN_ROIC:
            reasons.append(f"ROIC {latest.roic:.1%} above {self.MIN_ROIC:.0%} floor ✓")
            passes.append(True)
        elif latest.roic:
            reasons.append(f"ROIC {latest.roic:.1%} below {self.MIN_ROIC:.0%} floor")
            passes.append(False)
        else:
            reasons.append("ROIC data unavailable — gate skipped")
            # Don't append False: missing data is not a disqualifier

        # Gate 4: Leverage check
        if latest.net_debt_to_ebitda is not None:
            if latest.net_debt_to_ebitda <= self.MAX_NET_DEBT_TO_EBITDA:
                reasons.append(f"Leverage {latest.net_debt_to_ebitda:.1f}x below {self.MAX_NET_DEBT_TO_EBITDA:.0f}x ceiling ✓")
                passes.append(True)
            else:
                reasons.append(f"Leverage {latest.net_debt_to_ebitda:.1f}x exceeds {self.MAX_NET_DEBT_TO_EBITDA:.0f}x ceiling")
                passes.append(False)

        # Gate 5: Moat (not a hard gate, but noted)
        if moat_result.strength == MoatStrength.NONE:
            reasons.append("No moat detected — higher risk profile")
        else:
            reasons.append(f"{moat_result.strength.value.capitalize()} moat detected ✓")

        # Gate 6: Positive owner earnings
        # Skip for financial sector companies (banks, insurers) where D&A-based OE is not applicable
        if iv_result.owner_earnings_used is not None:
            if iv_result.owner_earnings_used > 0:
                reasons.append("Positive owner earnings ✓")
                passes.append(True)
            else:
                reasons.append("Negative or zero owner earnings — cash burn")
                passes.append(False)
        else:
            reasons.append("Owner earnings N/A (financial sector or missing D&A) — gate skipped")

        # All hard gates must pass
        is_investable = all(passes)
        return is_investable, reasons

    def analyze_ticker(self, ticker: str) -> Optional[AnalysisReport]:
        """
        Run full first-principles analysis for one ticker.
        Returns AnalysisReport or None if insufficient data.
        """
        logger.info(f"[L2] Analyzing {ticker}...")

        with self.Session() as session:
            company = session.query(Company).filter_by(ticker=ticker).first()
            if not company:
                logger.warning(f"No company record for {ticker} — run ingestion first")
                return None

            fundamentals_by_year = self._load_fundamentals(session, company.id)
            price_records = self._load_price_history(session, company.id)

            if not fundamentals_by_year:
                logger.warning(f"No fundamentals for {ticker}")
                return None

            latest_year = max(fundamentals_by_year.keys())
            latest = fundamentals_by_year[latest_year]

            # Latest price
            current_price = None
            shares_outstanding = None
            market_cap = None
            if price_records:
                last_price_rec = price_records[-1]
                current_price = last_price_rec.adjusted_close or last_price_rec.close
                shares_outstanding = last_price_rec.shares_outstanding
                market_cap = last_price_rec.market_cap
                # market_cap is often NULL in intraday records — derive it so WACC
                # doesn't collapse to debt-only cost and produce a 60x terminal multiplier
                if market_cap is None and current_price and shares_outstanding:
                    market_cap = current_price * shares_outstanding

            # ── WACC ───────────────────────────────────────────
            price_returns = self._compute_price_returns(price_records)
            market_returns = self._get_sp500_returns(len(price_returns))

            wacc_result = self.wacc_estimator.compute(
                market_cap=market_cap,
                total_debt=latest.total_debt,
                interest_expense=None,  # TODO: add to schema if needed
                sector=company.sector,
                price_returns=price_returns if len(price_returns) >= 60 else None,
                market_returns=market_returns if len(market_returns) >= 60 else None,
            )

            # ── Moat ───────────────────────────────────────────
            moat_result = self.moat_detector.analyze(
                fundamentals_by_year=fundamentals_by_year,
                wacc=wacc_result.wacc,
                market_cap=market_cap,
            )

            # ── Intrinsic Value ────────────────────────────────
            iv_result = self.iv_estimator.estimate(
                ticker=ticker,
                fundamentals_by_year=fundamentals_by_year,
                wacc=wacc_result.wacc,
                current_price=current_price,
                shares_outstanding=shares_outstanding,
                moat_score=moat_result.score,
                revenue_cagr=None,  # Populated by moat detector internally
            )

            # ── DCF reliability check ──────────────────────────
            # REITs (SIC 65xx) and financials (SIC 60-64xx) are structurally
            # mispriced by DCF — they should use FFO/cap-rate models instead.
            # Cap displayed MOS at 200% so phantom 400%+ gaps don't dominate
            # the fundamental score and mislead the human reviewer.
            _sic = (company.sic_code or "")
            dcf_reliable = not _sic.startswith(("60", "62", "63", "64", "65"))
            _mos = iv_result.margin_of_safety
            _iv_con = iv_result.intrinsic_value_conservative
            _iv_base = iv_result.intrinsic_value_base

            # ── Investability assessment ───────────────────────
            is_investable, investable_reasons = self._assess_investability(
                fundamentals_by_year, wacc_result, moat_result, iv_result
            )

            # ── Assemble report ────────────────────────────────
            all_notes = (
                wacc_result.notes +
                iv_result.notes +
                moat_result.signals
            )
            all_warnings = iv_result.warnings + moat_result.warnings
            if not dcf_reliable:
                dcf_warning = (
                    f"DCF unreliable for SIC {_sic} sector — "
                    f"intrinsic value overstated; use FFO/cap-rate for REITs, P/Book for banks"
                )
                all_warnings.append(dcf_warning)
                if _mos is not None and _mos > 2.0:
                    _mos = 2.0   # cap at 200% so f_score isn't inflated by phantom gaps

            report = AnalysisReport(
                ticker=ticker,
                company_name=company.name or ticker,
                analyzed_at=datetime.utcnow().isoformat(),

                wacc=wacc_result.wacc,
                cost_of_equity=wacc_result.cost_of_equity,
                beta=wacc_result.beta,

                moat_strength=moat_result.strength.value,
                moat_score=moat_result.score,
                moat_types=[t.value for t in moat_result.likely_types],
                moat_signals=moat_result.signals,
                moat_warnings=moat_result.warnings,

                latest_year=latest_year,
                revenue=latest.revenue,
                gross_margin=latest.gross_margin,
                roic=latest.roic,
                owner_earnings=latest.owner_earnings,
                free_cash_flow=latest.free_cash_flow,
                net_debt=latest.net_debt,
                net_debt_to_ebitda=latest.net_debt_to_ebitda,

                current_price=current_price,
                intrinsic_value_conservative=_iv_con,
                intrinsic_value_base=_iv_base,
                margin_of_safety=_mos,
                price_to_intrinsic=iv_result.price_to_intrinsic,
                is_undervalued=iv_result.is_undervalued,

                dcf_bear_iv=iv_result.bear.intrinsic_value_per_share,
                dcf_base_iv=iv_result.base.intrinsic_value_per_share,
                dcf_bull_iv=iv_result.bull.intrinsic_value_per_share,

                is_investable=is_investable,
                investable_reasons=investable_reasons,
                analysis_notes=all_notes,
                analysis_warnings=all_warnings,
                dcf_reliable=dcf_reliable,
            )

            logger.info(
                f"[L2] {ticker} complete | "
                f"Moat: {report.moat_strength} | "
                f"ROIC: {(report.roic or 0):.1%} vs WACC: {(report.wacc or 0):.1%} | "
                f"MoS: {f'{report.margin_of_safety:.1%}' if report.margin_of_safety else 'N/A'} | "
                f"Investable: {report.is_investable}"
            )

            return report

    def analyze_watchlist(self, tickers: Optional[list] = None) -> dict:
        """Run analysis on all watchlist tickers. Returns {ticker: AnalysisReport}."""
        tickers = tickers or config.watchlist
        reports = {}

        for ticker in tickers:
            try:
                report = self.analyze_ticker(ticker)
                if report:
                    reports[ticker] = report
            except Exception as e:
                logger.error(f"Analysis failed for {ticker}: {e}")

        # Summary table
        logger.info("\n" + "═" * 70)
        logger.info(f"{'Ticker':<8} {'Moat':<10} {'ROIC':>6} {'WACC':>6} {'MoS':>8} {'IV?':>5}")
        logger.info("─" * 70)
        for ticker, r in sorted(reports.items()):
            mos = f"{r.margin_of_safety:.1%}" if r.margin_of_safety is not None else "N/A"
            logger.info(
                f"{ticker:<8} {r.moat_strength:<10} "
                f"{(r.roic or 0):>5.1%} {(r.wacc or 0):>5.1%} "
                f"{mos:>8} {'✅' if r.is_investable else '❌':>5}"
            )
        logger.info("═" * 70)

        return reports


# ── CLI entry point ───────────────────────────────────────────

if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from loguru import logger
    logger.add(config.log_file, rotation="10 MB", retention="30 days", level=config.log_level)

    parser = argparse.ArgumentParser(description="Layer 2: First Principles Analysis Engine")
    parser.add_argument("--ticker", type=str, help="Single ticker to analyze")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--save", type=str, help="Save report JSON to file")
    args = parser.parse_args()

    _, Session = init_db(config.database.url)
    engine = FirstPrinciplesEngine(db_session_factory=Session)

    if args.ticker:
        report = engine.analyze_ticker(args.ticker.upper())
        if report:
            if args.json:
                print(report.to_json())
            else:
                print(report.summary())
            if args.save:
                with open(args.save, "w") as f:
                    f.write(report.to_json())
                print(f"\nReport saved to {args.save}")
    else:
        reports = engine.analyze_watchlist()
        if args.save:
            all_json = {t: json.loads(r.to_json()) for t, r in reports.items()}
            with open(args.save, "w") as f:
                json.dump(all_json, f, indent=2)
            print(f"All reports saved to {args.save}")
