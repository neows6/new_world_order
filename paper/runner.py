"""
paper/runner.py — Standalone paper trading runner.

Runs the full 6-layer AI pipeline on the same watchlist / schedule as main.py
but routes all trades through PaperExecutor against a fake $100k account.

Usage:
    python -m paper.runner

Runs every 5 minutes Mon-Fri 9-4pm ET, same schedule as the live system.
Can run alongside main.py (uses a separate DB and port-less — no web server).
"""

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PAUSE_FLAG = Path("data/paused.flag")

from loguru import logger

from config import config
from models.database import init_db, Company, PriceHistory

from analysis.engine import FirstPrinciplesEngine
from signals.fft_cycles import FFTCycleDetector
from signals.fibonacci import FibonacciAnalyzer
from signals.insider_flow import InsiderFlowAnalyzer
from signals.market_microstructure import VWAPCalculator, VolumeProfileAnalyzer, VIXRegimeDetector
from signals.aggregator import SignalAggregator
from fud.filter_engine import FUDFilterEngine
from decision.engine import DecisionEngine
from risk.manager import RiskManager
from broker.market_data import SchwabMarketData

from paper.account import init_paper_db
from paper.executor import PaperExecutor


def setup_logging():
    os.makedirs("logs", exist_ok=True)
    logger.remove()
    logger.add(
        sys.stdout, level="INFO", colorize=True,
        format="<green>{time:HH:mm:ss}</green> | <cyan>PAPER</cyan> | <level>{level: <8}</level> | {message}"
    )
    logger.add(
        "logs/paper_trading.log", rotation="10 MB", retention="30 days",
        level="INFO", compression="zip"
    )


def _load_price_data(Session, ticker: str) -> dict:
    with Session() as session:
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
        cik = company.cik

    closes  = [r.adjusted_close or r.close for r in records if (r.adjusted_close or r.close)]
    highs   = [r.high   for r in records if r.high]
    lows    = [r.low    for r in records if r.low]
    volumes = [r.volume for r in records if r.volume]

    return {
        "closes": closes, "highs": highs,
        "lows": lows, "volumes": volumes,
        "cik": cik,
        "current_price": closes[-1] if closes else None,
    }


def _get_stage2_tickers() -> list:
    """Return Stage 2 tickers from stagegate.json (AI-active stocks)."""
    import json
    from pathlib import Path
    sg_file = Path("data/stagegate.json")
    if sg_file.exists():
        try:
            sg = json.loads(sg_file.read_text(encoding="utf-8"))
            stage2 = sg.get("stage2", [])
            if stage2:
                logger.info(f"Stage Gate: running AI on {len(stage2)} Stage 2 tickers: {stage2}")
                return stage2
        except Exception:
            pass
    logger.info("Stage Gate: no Stage 2 tickers — falling back to full watchlist")
    return list(config.watchlist)


def _get_ai_exit_tickers() -> list:
    """Return Stage 3 tickers that have the AI exit toggle enabled."""
    import json
    from pathlib import Path
    ai_exits_file = Path("data/ai_exits.json")
    if not ai_exits_file.exists():
        return []
    try:
        overrides = json.loads(ai_exits_file.read_text(encoding="utf-8"))
        sg_file = Path("data/stagegate.json")
        if not sg_file.exists():
            return []
        sg = json.loads(sg_file.read_text(encoding="utf-8"))
        stage3 = sg.get("stage3", [])
        return [t for t in stage3 if overrides.get(t, False)]
    except Exception:
        return []


def _get_vix(market_data: SchwabMarketData) -> float:
    try:
        quote = market_data.get_quote("$VIX")
        if quote and quote.get("last_price"):
            return float(quote["last_price"])
    except Exception:
        pass
    return 20.0


def run_paper_cycle(
    Session,
    analysis_engine, fft_detector, fib_analyzer,
    insider_analyzer, vwap_calc, vol_analyzer,
    vix_detector, aggregator, fud_engine,
    decision_engine, risk_manager,
    executor: PaperExecutor,
    market_data: SchwabMarketData,
):
    if PAUSE_FLAG.exists():
        logger.info("Paper cycle skipped — system PAUSED")
        return

    logger.info("─" * 60)
    logger.info("Paper trading cycle start")

    portfolio_value = None
    try:
        summary = executor.get_account_summary()
        portfolio_value = summary["total_equity"]
        logger.info(
            f"Paper account: ${portfolio_value:,.2f} total "
            f"(cash ${summary['cash']:,.2f} | "
            f"P&L {summary['total_pnl_pct']:+.2f}%)"
        )
    except Exception as e:
        logger.warning(f"Could not fetch paper account value: {e}")

    vix_level  = _get_vix(market_data)
    vix_regime = vix_detector.classify(vix_level)
    logger.info(f"VIX {vix_level:.1f} → {vix_regime.regime} | {vix_regime.action}")

    # Load I-Tool cache for technical signal enrichment
    _itool_signals: dict = {}
    try:
        import json as _json
        _itool_cache = ROOT / "data" / "itool_scan.json"
        if _itool_cache.exists():
            _itool_data = _json.loads(_itool_cache.read_text(encoding="utf-8"))
            for r in _itool_data.get("results", []):
                if r.get("ticker") and r.get("signal"):
                    _itool_signals[r["ticker"]] = r["signal"]  # "bullish" | "bearish"
            logger.info(f"[ITOOL] Loaded {len(_itool_signals)} technical signals from cache")
    except Exception as e:
        logger.warning(f"[ITOOL] Could not load I-Tool cache: {e}")

    layer3_results = {}

    for ticker in _get_stage2_tickers():
        try:
            analysis = analysis_engine.analyze_ticker(ticker)
            if not analysis:
                continue

            price = _load_price_data(Session, ticker)
            closes        = price.get("closes", [])
            highs         = price.get("highs", [])
            lows          = price.get("lows", [])
            volumes       = price.get("volumes", [])
            current_price = price.get("current_price") or analysis.current_price
            cik           = price.get("cik") or ""

            fft     = fft_detector.analyze(ticker, closes)        if len(closes) >= 64  else None
            fib     = fib_analyzer.analyze(ticker, highs, lows, closes) if len(closes) >= 30 else None
            insider = insider_analyzer.score(ticker, cik, current_price) if cik else None
            vwap    = vwap_calc.compute_daily(ticker, highs, lows, closes, volumes) if len(closes) >= 5 else None
            vol_profile = vol_analyzer.analyze(ticker, highs, lows, closes, volumes) if len(closes) >= 10 else None

            agg_signal = aggregator.aggregate(
                analysis=analysis, fft=fft, fib=fib, insider=insider,
                vwap=vwap, vol_profile=vol_profile,
                vix_regime=vix_regime, current_price=current_price,
                itool_signal=_itool_signals.get(ticker),
            )

            l3_result = fud_engine.analyze_ticker(ticker, agg_signal)
            layer3_results[ticker] = l3_result

        except Exception as e:
            logger.error(f"Paper L2/Signals/L3 failed for {ticker}: {e}")

    if not layer3_results:
        logger.info("No tickers passed L3 — paper cycle complete.")
        return

    # Load per-ticker gate overrides from UI settings
    _gate_overrides = {}
    try:
        import json as _json
        _go_file = Path("data/gate_overrides.json")
        if _go_file.exists():
            _gate_overrides = _json.loads(_go_file.read_text(encoding="utf-8"))
    except Exception:
        pass

    decisions = decision_engine.run_watchlist(
        layer3_results=layer3_results,
        portfolio_value=portfolio_value,
        max_trade_dollars=config.risk.max_dollar_per_trade if hasattr(config.risk, "max_dollar_per_trade") else 500.0,
        gate_overrides=_gate_overrides,
    )

    risk_assessments = risk_manager.assess_watchlist(
        decisions=decisions,
        portfolio_value=portfolio_value,
        market_returns=[],
    )

    approved = []
    for ticker, assessment in risk_assessments.items():
        if assessment.approved:
            result = executor.execute(assessment)
            if result:
                approved.append(ticker)

    logger.info(f"Paper cycle complete. {len(approved)} paper trade(s) executed" +
                (f": {approved}" if approved else "."))

    # ── AI-managed exits for Stage 3 tickers with toggle ON ───────────────────
    ai_exit_tickers = [t for t in _get_ai_exit_tickers() if t not in layer3_results]
    if ai_exit_tickers:
        logger.info(f"[AI EXIT] Checking {len(ai_exit_tickers)} Stage 3 ticker(s) for exit signals")
        for ticker in ai_exit_tickers:
            try:
                analysis = analysis_engine.analyze_ticker(ticker)
                if not analysis:
                    continue
                price_data    = _load_price_data(Session, ticker)
                closes        = price_data.get("closes", [])
                highs         = price_data.get("highs", [])
                lows          = price_data.get("lows", [])
                volumes       = price_data.get("volumes", [])
                current_price = price_data.get("current_price") or analysis.current_price
                cik           = price_data.get("cik") or ""

                fft         = fft_detector.analyze(ticker, closes)                      if len(closes) >= 64 else None
                fib         = fib_analyzer.analyze(ticker, highs, lows, closes)         if len(closes) >= 30 else None
                insider     = insider_analyzer.score(ticker, cik, current_price)        if cik else None
                vwap        = vwap_calc.compute_daily(ticker, highs, lows, closes, volumes) if len(closes) >= 5 else None
                vol_profile = vol_analyzer.analyze(ticker, highs, lows, closes, volumes) if len(closes) >= 10 else None

                agg = aggregator.aggregate(
                    analysis=analysis, fft=fft, fib=fib, insider=insider,
                    vwap=vwap, vol_profile=vol_profile,
                    vix_regime=vix_regime, current_price=current_price,
                    itool_signal=_itool_signals.get(ticker),
                )
                l3 = fud_engine.analyze_ticker(ticker, agg)
                dec_map = decision_engine.run_watchlist(
                    layer3_results={ticker: l3},
                    portfolio_value=portfolio_value,
                    max_trade_dollars=config.risk.max_dollar_per_trade if hasattr(config.risk, "max_dollar_per_trade") else 500.0,
                    gate_overrides=_gate_overrides,
                )
                dec = dec_map.get(ticker)
                if dec:
                    sig = (dec.signal or "").upper()
                    if sig in ("SELL", "STRONG_SELL"):
                        logger.info(f"[AI EXIT] {ticker}: {sig} signal — triggering paper SELL")
                        executor.execute_sell(ticker, current_price or 0.0, reason=f"AI EXIT: {sig}")
            except Exception as e:
                logger.error(f"[AI EXIT] Failed for {ticker}: {e}")


def main():
    setup_logging()
    os.makedirs("data", exist_ok=True)

    logger.info("=" * 60)
    logger.info("NWO Paper Trading Runner starting")
    logger.info(f"Watchlist: {config.watchlist}")
    logger.info("=" * 60)

    init_paper_db()

    _, Session = init_db(config.database.url, echo=False)

    analysis_engine  = FirstPrinciplesEngine(db_session_factory=Session)
    fft_detector     = FFTCycleDetector()
    fib_analyzer     = FibonacciAnalyzer()
    insider_analyzer = InsiderFlowAnalyzer()
    vwap_calc        = VWAPCalculator()
    vol_analyzer     = VolumeProfileAnalyzer()
    vix_detector     = VIXRegimeDetector()
    aggregator       = SignalAggregator()
    fud_engine       = FUDFilterEngine(db_session_factory=Session)
    decision_engine  = DecisionEngine(db_session_factory=Session)
    risk_manager     = RiskManager(db_session_factory=Session)
    executor         = PaperExecutor(main_db_session_factory=Session)
    market_data      = SchwabMarketData()

    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = BackgroundScheduler(timezone="America/New_York")

    scheduler.add_job(
        func=lambda: run_paper_cycle(
            Session, analysis_engine,
            fft_detector, fib_analyzer, insider_analyzer,
            vwap_calc, vol_analyzer, vix_detector, aggregator,
            fud_engine, decision_engine, risk_manager,
            executor, market_data,
        ),
        trigger=CronTrigger(day_of_week="mon-fri", hour="9-16", minute="*/5"),
        id="paper_trading_cycle",
        name="Paper trading — full 6-layer cycle",
        replace_existing=True,
    )

    scheduler.start()
    logger.info("Paper trading scheduler started. Press Ctrl+C to stop.")

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Paper runner shutting down...")
        scheduler.shutdown()


if __name__ == "__main__":
    main()
