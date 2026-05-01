"""
pipeline/analysis_pipeline.py — Live Analysis Pipeline (Layers 2-5).

Orchestrates the complete signal generation and decision process for
live trading. Called after each price ingestion cycle.

Pipeline flow:
  L1 (Ingestion) → this file reads price/fundamentals already in DB
  L2: FirstPrinciplesEngine — WACC, moat, DCF, intrinsic value
  Signals: FFT, Fibonacci, VWAP, VolumeProfile, InsiderFlow, Momentum, VIX
           TradingView technical consensus (optional)
  Aggregator: Combines all signals into AggregatedSignal
  L3: FUDFilterEngine — news quality gate
  L4: DecisionEngine — physics-based GO/NO-GO
  L5: RiskManager — position sizing, hard risk limits
  Output: RiskAssessment → logged to DB, executor-ready
"""

import json
from datetime import datetime
from typing import Optional

from loguru import logger

from config import config
from models.database import Company, PriceHistory, TradeSignal
from analysis.engine import FirstPrinciplesEngine, AnalysisReport
from signals.fft_cycles import FFTCycleDetector
from signals.fibonacci import FibonacciAnalyzer
from signals.insider_flow import InsiderFlowAnalyzer
from signals.market_microstructure import VWAPCalculator, VolumeProfileAnalyzer, VIXRegimeDetector
from signals.momentum import MomentumAnalyzer
from signals.aggregator import SignalAggregator
from fud.filter_engine import FUDFilterEngine
from decision.engine import DecisionEngine
from risk.manager import RiskManager


class AnalysisPipeline:
    """
    Live analysis pipeline — layers 2-5 for all watchlist tickers.
    Reads from the DB (populated by IngestionPipeline / Layer 1).
    Call run_watchlist() after each price ingestion cycle.
    """

    def __init__(self, db_session_factory):
        self.Session = db_session_factory

        # Layer 2
        self.l2_engine = FirstPrinciplesEngine(db_session_factory)

        # Signal modules
        self.fft              = FFTCycleDetector()
        self.fib              = FibonacciAnalyzer()
        self.insider          = InsiderFlowAnalyzer()
        self.vwap_calc        = VWAPCalculator()
        self.vol_profile      = VolumeProfileAnalyzer()
        self.vix_detector     = VIXRegimeDetector()
        self.momentum_calc    = MomentumAnalyzer()
        self.aggregator       = SignalAggregator()

        # Layer 3
        self.fud_filter       = FUDFilterEngine(db_session_factory)

        # Layer 4
        self.decision_engine  = DecisionEngine(db_session_factory)

        # Layer 5
        self.risk_manager     = RiskManager(db_session_factory)

        # TradingView signal fetcher (optional — degrades gracefully if not installed)
        try:
            from signals.tradingview import TradingViewSignalFetcher
            self.tv_fetcher = TradingViewSignalFetcher()
            logger.info("[TV] TradingView signal fetcher initialised")
        except Exception:
            self.tv_fetcher = None
            logger.debug("[TV] tradingview-ta not installed — TV consensus disabled")

    # ─────────────────────────────────────────────────────────────────────────
    # Data loading
    # ─────────────────────────────────────────────────────────────────────────

    def _load_price_data(self, ticker: str, lookback: int = 252) -> dict:
        """Load most recent OHLCV bars from DB for signal computation."""
        with self.Session() as session:
            company = session.query(Company).filter_by(ticker=ticker).first()
            if not company:
                return {}

            records = (
                session.query(PriceHistory)
                .filter_by(company_id=company.id)
                .order_by(PriceHistory.date)
                .all()
            )[-lookback:]

            if not records:
                return {}

            closes  = [r.adjusted_close or r.close for r in records if (r.adjusted_close or r.close)]
            highs   = [r.high   for r in records if r.high]
            lows    = [r.low    for r in records if r.low]
            volumes = [r.volume for r in records if r.volume]

            return {
                "closes":  closes,
                "highs":   highs,
                "lows":    lows,
                "volumes": volumes,
                "dates":   [r.date for r in records],
                "price":   closes[-1] if closes else None,
                "cik":     company.cik,
            }

    def _get_vix(self) -> float:
        """
        Fetch VIX from Schwab. Falls back to tvdatafeed if Schwab not available.
        Returns 20.0 (normal regime) if both fail.
        """
        # Try Schwab first
        try:
            from schwab_api.market_data import SchwabMarketData
            md = SchwabMarketData()
            quote = md.get_quote("$VIX.X")   # Schwab symbol for CBOE VIX
            if quote and quote.get("last"):
                return float(quote["last"])
        except Exception:
            pass

        # Try TradingView data feed
        try:
            from signals.tradingview import TradingViewSignalFetcher
            tv = TradingViewSignalFetcher()
            vix = tv.get_vix()
            if vix:
                return vix
        except Exception:
            pass

        logger.debug("[VIX] Not available — defaulting to 20.0 (normal regime)")
        return 20.0

    # ─────────────────────────────────────────────────────────────────────────
    # DB persistence
    # ─────────────────────────────────────────────────────────────────────────

    def _persist_signal(self, session, ticker: str, risk, agg_signal, analysis):
        """Write completed trade signal to the TradeSignal table."""
        company = session.query(Company).filter_by(ticker=ticker).first()
        if not company:
            return

        reasoning = {
            "action":          risk.action,
            "approved":        risk.approved,
            "composite_score": getattr(agg_signal, "composite_score", None),
            "momentum_score":  getattr(agg_signal, "momentum_score", None),
            "rvol":            getattr(agg_signal, "rvol", None),
            "is_52w_breakout": getattr(agg_signal, "is_52w_breakout", None),
            "macd_direction":  getattr(agg_signal, "macd_direction", None),
            "ma_stack":        getattr(agg_signal, "ma_stack", None),
            "why_buy":         getattr(agg_signal, "why_buy", []),
            "why_wait":        getattr(agg_signal, "why_wait", []),
            "final_narrative": getattr(risk, "final_narrative", ""),
        }

        signal_record = TradeSignal(
            company_id=company.id,
            signal=risk.action,
            confidence=getattr(agg_signal, "confidence", 0.0),
            generated_at=datetime.utcnow(),
            roic=getattr(analysis, "roic", None),
            wacc_estimate=getattr(analysis, "wacc", None),
            margin_of_safety=getattr(analysis, "margin_of_safety", None),
            fud_score=None,  # Populated from Layer 3 if needed
            intrinsic_value_estimate=getattr(analysis, "intrinsic_value_base", None),
            current_price=getattr(analysis, "current_price", None),
            suggested_position_pct=risk.final_position_pct,
            reasoning=json.dumps(reasoning, default=str),
            acted_on=False,
        )
        session.add(signal_record)

    # ─────────────────────────────────────────────────────────────────────────
    # Single-ticker analysis
    # ─────────────────────────────────────────────────────────────────────────

    def run_ticker(
        self,
        ticker: str,
        portfolio_value: Optional[float] = None,
        vix_level: Optional[float] = None,
    ):
        """
        Run full layers 2-5 pipeline for a single ticker.
        Returns RiskAssessment or None on failure.
        """
        logger.info(f"[ANALYSIS] ══════ {ticker} ══════")

        # ── Load price data from DB ───────────────────────────────
        price_data = self._load_price_data(ticker)
        if not price_data or len(price_data.get("closes", [])) < 20:
            logger.warning(f"[ANALYSIS] {ticker}: insufficient price history — need 20+ bars")
            return None

        closes  = price_data["closes"]
        highs   = price_data["highs"]
        lows    = price_data["lows"]
        volumes = price_data["volumes"]
        current_price = price_data["price"]
        cik     = price_data.get("cik") or ""

        # ── Layer 2: First Principles Analysis ───────────────────
        try:
            analysis = self.l2_engine.analyze_ticker(ticker)
        except Exception as e:
            logger.error(f"[L2] {ticker}: {e}")
            analysis = None

        # Stub analysis if unavailable — so pipeline can still run on signals alone
        if analysis is None:
            logger.warning(f"[L2] {ticker}: no analysis — fundamentals score will be neutral")
            analysis = AnalysisReport(
                ticker=ticker, company_name=ticker,
                analyzed_at=datetime.utcnow().isoformat(),
                wacc=None, cost_of_equity=None, beta=None,
                moat_strength="none", moat_score=0.0,
                moat_types=[], moat_signals=[], moat_warnings=[],
                latest_year=None, revenue=None, gross_margin=None, roic=None,
                owner_earnings=None, free_cash_flow=None, net_debt=None,
                net_debt_to_ebitda=None, current_price=current_price,
                intrinsic_value_conservative=None, intrinsic_value_base=None,
                margin_of_safety=None, price_to_intrinsic=None, is_undervalued=False,
                dcf_bear_iv=None, dcf_base_iv=None, dcf_bull_iv=None,
                is_investable=False, investable_reasons=[], analysis_notes=[],
                analysis_warnings=[],
            )

        # ── Signals ───────────────────────────────────────────────

        # FFT cycles
        fft = None
        try:
            fft = self.fft.analyze(ticker, closes)
        except Exception as e:
            logger.debug(f"[FFT] {ticker}: {e}")

        # Fibonacci
        fib = None
        try:
            fib = self.fib.analyze(ticker, highs, lows, closes)
        except Exception as e:
            logger.debug(f"[FIB] {ticker}: {e}")

        # VWAP
        vwap = None
        try:
            vwap = self.vwap_calc.compute_daily(ticker, highs, lows, closes, volumes)
        except Exception as e:
            logger.debug(f"[VWAP] {ticker}: {e}")

        # Volume Profile
        vol_prof = None
        try:
            vol_prof = self.vol_profile.analyze(ticker, highs, lows, closes, volumes)
        except Exception as e:
            logger.debug(f"[VOLPROF] {ticker}: {e}")

        # Momentum (live — was only in backtest before this commit)
        momentum = None
        try:
            momentum = self.momentum_calc.analyze(ticker, closes, highs, lows, volumes)
            logger.info(
                f"[MOMENTUM] {ticker}: score={momentum.composite_momentum_score:+.2f} "
                f"RVOL={momentum.rvol:.1f}x MACD={momentum.macd_direction} "
                f"MA={momentum.ma_stack} signal={momentum.signal}"
            )
        except Exception as e:
            logger.debug(f"[MOMENTUM] {ticker}: {e}")

        # Insider flow (Form 4)
        insider = None
        try:
            insider = self.insider.score(ticker, cik, current_price)
        except Exception as e:
            logger.debug(f"[INSIDER] {ticker}: {e}")

        # VIX regime (fetched once per batch and passed in)
        vix_regime = None
        try:
            if vix_level is None:
                vix_level = self._get_vix()
            vix_regime = self.vix_detector.classify(vix_level)
        except Exception as e:
            logger.debug(f"[VIX] {ticker}: {e}")

        # TradingView technical consensus (optional)
        tv_signal = None
        if self.tv_fetcher:
            try:
                tv_signal = self.tv_fetcher.get_signal(ticker)
                if tv_signal:
                    logger.info(
                        f"[TV] {ticker}: {tv_signal.recommendation} "
                        f"(buy={tv_signal.buy_count} sell={tv_signal.sell_count} "
                        f"neutral={tv_signal.neutral_count})"
                    )
            except Exception as e:
                logger.debug(f"[TV] {ticker}: {e}")

        # ── AI Watch breakout override ────────────────────────────
        # For AI watch tickers on confirmed momentum breakout days,
        # floor the fundamentals score at 0 so premium valuation
        # can't block a legitimate institutional breakout signal.
        is_ai_watch = ticker in config.ai_watch_tickers
        is_breakout  = (
            momentum is not None
            and momentum.signal in ("strong_momentum", "momentum")
            and momentum.rvol >= 1.5
        )
        floor_fundamentals = is_ai_watch and is_breakout

        if floor_fundamentals:
            logger.info(
                f"[AI WATCH] {ticker}: breakout override active — "
                f"RVOL={momentum.rvol:.1f}x signal={momentum.signal} — "
                f"fundamentals floored at 0"
            )

        # ── Aggregate all signals ─────────────────────────────────
        try:
            agg_signal = self.aggregator.aggregate(
                analysis=analysis,
                fft=fft,
                fib=fib,
                insider=insider,
                vwap=vwap,
                vol_profile=vol_prof,
                vix_regime=vix_regime,
                current_price=current_price,
                momentum=momentum,
                tv_signal=tv_signal,
                floor_fundamentals=floor_fundamentals,
            )
            logger.info(
                f"[AGG] {ticker}: composite={agg_signal.composite_score:+.2f} "
                f"signal={agg_signal.signal} confidence={agg_signal.confidence:.0%}"
            )
        except Exception as e:
            logger.error(f"[AGG] {ticker}: aggregation failed — {e}")
            return None

        # ── Layer 3: FUD Filter ───────────────────────────────────
        try:
            layer3 = self.fud_filter.analyze_ticker(ticker, agg_signal)
            status = "PASS" if layer3.proceed_to_execution else "BLOCKED"
            logger.info(f"[L3] {ticker}: {status} — {layer3.gate_reason}")
        except Exception as e:
            logger.error(f"[L3] {ticker}: FUD filter error — {e}")
            return None

        # ── Layer 4: Decision Engine ──────────────────────────────
        try:
            decision = self.decision_engine.decide(
                layer3_result=layer3,
                portfolio_value=portfolio_value,
            )
            result_str = "GO" if decision.go_no_go else "NO-GO"
            logger.info(
                f"[L4] {ticker}: {result_str} — {decision.action} "
                f"confidence={decision.decision_confidence:.0%}"
            )
        except Exception as e:
            logger.error(f"[L4] {ticker}: decision engine error — {e}")
            return None

        # ── Layer 5: Risk Manager ─────────────────────────────────
        try:
            risk = self.risk_manager.assess(
                decision=decision,
                portfolio_value=portfolio_value,
            )
            approved_str = "APPROVED" if risk.approved else "BLOCKED"
            logger.info(
                f"[L5] {ticker}: {approved_str} — {risk.action} "
                f"${risk.final_dollar_amount:,.0f} "
                f"({risk.final_shares} shares @ ${risk.entry_price:.2f})"
                if risk.entry_price else
                f"[L5] {ticker}: {approved_str} — {risk.action}"
            )
        except Exception as e:
            logger.error(f"[L5] {ticker}: risk manager error — {e}")
            return None

        # ── Persist signal to DB ──────────────────────────────────
        try:
            with self.Session() as session:
                self._persist_signal(session, ticker, risk, agg_signal, analysis)
                session.commit()
        except Exception as e:
            logger.warning(f"[DB] {ticker}: signal persist failed — {e}")

        # ── Execution gate ────────────────────────────────────────
        if risk.approved:
            if config.risk.dry_run:
                logger.info(
                    f"[DRY RUN] {ticker}: would execute {risk.action} "
                    f"{risk.final_shares} shares — dry_run=True, skipping"
                )
            else:
                logger.warning(
                    f"[EXEC] {ticker}: LIVE trade approved but executor not yet wired. "
                    f"Set dry_run=True to suppress or implement execution layer."
                )

        return risk

    # ─────────────────────────────────────────────────────────────────────────
    # AI Watch fast scan (runs every 1 minute)
    # ─────────────────────────────────────────────────────────────────────────

    def run_ai_watch(self, portfolio_value: Optional[float] = None) -> dict:
        """
        Fast scan for AI watch tickers only. Called every minute by the scheduler.
        AI watch tickers get the breakout override: fundamentals floored at 0
        when momentum is confirmed (RVOL ≥ 2× + strong/momentum signal).
        """
        tickers = config.ai_watch_tickers
        if not tickers:
            return {}

        logger.info(f"[AI WATCH] 1-min scan: {tickers}")
        vix_level = self._get_vix()
        results = {}

        for ticker in tickers:
            try:
                result = self.run_ticker(ticker, portfolio_value=portfolio_value, vix_level=vix_level)
                results[ticker] = result
                if result and result.approved:
                    logger.info(f"[AI WATCH] ★ {ticker}: BUY SIGNAL APPROVED")
            except Exception as e:
                logger.error(f"[AI WATCH] {ticker}: error — {e}")
                results[ticker] = None

        return results

    # ─────────────────────────────────────────────────────────────────────────
    # Watchlist batch
    # ─────────────────────────────────────────────────────────────────────────

    def run_watchlist(
        self,
        tickers: Optional[list] = None,
        portfolio_value: Optional[float] = None,
    ) -> dict:
        """
        Run full analysis for all watchlist tickers.
        Fetches VIX once for the whole batch to avoid redundant API calls.
        Returns dict of ticker → RiskAssessment (or None on failure).
        """
        tickers = tickers or config.watchlist
        logger.info(f"[ANALYSIS] Starting analysis for {len(tickers)} tickers: {tickers}")

        # VIX once per batch
        vix_level = self._get_vix()
        logger.info(f"[VIX] Level: {vix_level:.1f}")

        results  = {}
        approved = []

        for ticker in tickers:
            try:
                result = self.run_ticker(
                    ticker,
                    portfolio_value=portfolio_value,
                    vix_level=vix_level,
                )
                results[ticker] = result
                if result and result.approved:
                    approved.append(ticker)
            except Exception as e:
                logger.error(f"[ANALYSIS] {ticker}: uncaught error — {e}")
                results[ticker] = None

        logger.info(
            f"[ANALYSIS] Complete — {len(results)} tickers | "
            f"Signals: {approved if approved else 'none'}"
        )
        return results
