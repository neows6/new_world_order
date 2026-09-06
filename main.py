"""
main.py — Application entry point.

Full pipeline (6 layers + signals):

  L1  Data Ingestion      pipeline/ingestion.py     EDGAR + Schwab → DB
  L2  First Principles    analysis/engine.py         ROIC, moat, DCF
  SIG Signal Layer        signals/aggregator.py      FFT, Fib, Insider, VWAP, VIX
  L3  FUD Filter          fud/filter_engine.py       News quality + attack detection
  L4  Decision Engine     decision/engine.py         Reynolds, Quantum, Kalman, Kelly
  L5  Risk Manager        risk/manager.py            Behavioral psych + hard limits
  L6  Executor            broker/executor.py         Order placement (dry_run by default)
"""

import os
import sys
import time
from pathlib import Path
from typing import Optional

PAUSE_FLAG = Path("data/paused.flag")

# Install Norton-aware CA bundle into env BEFORE any HTTP clients are imported.
# Modules that use schwab-py / curl_cffi / requests pick this up automatically.
from utils.ssl_context import install_env_ca_bundle as _install_ca
_install_ca()

from loguru import logger

from config import config
from models.database import init_db, Company, PriceHistory
from pipeline.ingestion import IngestionPipeline, setup_scheduler

# L2
from analysis.engine import FirstPrinciplesEngine

# Signals
from signals.fft_cycles import FFTCycleDetector
from signals.fibonacci import FibonacciAnalyzer
from signals.insider_flow import InsiderFlowAnalyzer
from signals.market_microstructure import (
    VWAPCalculator, VolumeProfileAnalyzer, VIXRegimeDetector
)
from signals.aggregator import SignalAggregator
from signals.momentum import MomentumAnalyzer
from signals.supertrend import SuperTrendAnalyzer

# L3
from fud.filter_engine import FUDFilterEngine

# L4
from decision.engine import DecisionEngine

# L5
from risk.manager import RiskManager

# L6
from broker.executor import SchwabExecutor
from broker.market_data import SchwabMarketData


def setup_logging():
    os.makedirs(os.path.dirname(config.log_file), exist_ok=True)
    logger.remove()
    logger.add(sys.stdout, level=config.log_level, colorize=True,
               format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}")
    logger.add(config.log_file, rotation="10 MB", retention="30 days",
               level=config.log_level, compression="zip")


def setup_directories():
    for d in ["data", "data/edgar_cache", "logs", "tokens"]:
        os.makedirs(d, exist_ok=True)


def _load_price_data(Session, ticker: str) -> dict:
    """Load OHLCV lists from DB for a ticker — used by signal modules."""
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

    # Dedupe the doubled daily rows before anything ATR-based reads them.
    from utils.price_data import ohlcv_arrays
    _bars   = ohlcv_arrays(records)
    closes  = _bars["closes"]
    highs   = _bars["highs"]
    lows    = _bars["lows"]
    volumes = _bars["volumes"]

    return {
        "closes": closes, "highs": highs,
        "lows": lows, "volumes": volumes,
        "cik": cik,
        "current_price": closes[-1] if closes else None,
    }


def _get_vix(market_data: SchwabMarketData) -> float:
    """Fetch VIX level from Schwab. Falls back to 20 (normal regime) on failure."""
    try:
        quote = market_data.get_quote("$VIX")
        if quote and quote.get("last_price"):
            return float(quote["last_price"])
    except Exception:
        pass
    logger.warning("Could not fetch VIX — using default 20.0 (normal regime)")
    return 20.0


def run_trading_cycle(
    Session,
    analysis_engine:  FirstPrinciplesEngine,
    fft_detector:     FFTCycleDetector,
    fib_analyzer:     FibonacciAnalyzer,
    insider_analyzer: InsiderFlowAnalyzer,
    vwap_calc:        VWAPCalculator,
    vol_analyzer:     VolumeProfileAnalyzer,
    vix_detector:     VIXRegimeDetector,
    aggregator:       SignalAggregator,
    fud_engine:       FUDFilterEngine,
    decision_engine:  DecisionEngine,
    risk_manager:     RiskManager,
    executor:         SchwabExecutor,
    market_data:      SchwabMarketData,
    momentum_analyzer: MomentumAnalyzer = None,
    st_analyzer: SuperTrendAnalyzer = None,
    tickers_override: list = None,      # None = full watchlist, list = subset (AI Watch)
):
    """
    One full analysis + trade cycle across the watchlist.
    Called every 5 minutes during market hours by APScheduler.
    """
    if PAUSE_FLAG.exists():
        logger.info("Trading cycle skipped — system is PAUSED (remove data/paused.flag to resume)")
        return

    logger.info("─" * 70)
    logger.info("Trading cycle start")

    # Portfolio value for sizing (None = dry_run sizing fallback)
    portfolio_value: Optional[float] = None
    try:
        portfolio_value = market_data.get_account_value()
    except Exception as e:
        logger.warning(f"Could not fetch portfolio value: {e}")

    # VIX regime — applies to all tickers this cycle
    vix_level  = _get_vix(market_data)
    vix_regime = vix_detector.classify(vix_level)
    logger.info(f"VIX {vix_level:.1f} → regime: {vix_regime.regime} | {vix_regime.action}")

    # ── Per-ticker: L2 + Signals + L3 ────────────────────────────────
    layer3_results = {}
    active_tickers = tickers_override if tickers_override else config.watchlist

    for ticker in active_tickers:
        try:
            # L2: First principles analysis
            analysis = analysis_engine.analyze_ticker(ticker)
            if not analysis:
                logger.warning(f"[L2] No analysis for {ticker} — skipping")
                continue

            # Load price data for signal modules
            price = _load_price_data(Session, ticker)
            closes  = price.get("closes", [])
            highs   = price.get("highs", [])
            lows    = price.get("lows", [])
            volumes = price.get("volumes", [])
            current_price = price.get("current_price") or analysis.current_price
            cik     = price.get("cik") or ""

            # Signals (all optional — aggregator handles None gracefully)
            fft     = fft_detector.analyze(ticker, closes) if len(closes) >= 64 else None
            fib     = fib_analyzer.analyze(ticker, highs, lows, closes) if len(closes) >= 30 else None
            insider = insider_analyzer.score(ticker, cik, current_price) if cik else None
            vwap    = vwap_calc.compute_daily(ticker, highs, lows, closes, volumes) if len(closes) >= 5 else None
            vol_profile = vol_analyzer.analyze(ticker, highs, lows, closes, volumes) if len(closes) >= 10 else None

            # Momentum (RVOL, MACD, MA stack, ATR, 52w breakout)
            momentum = None
            if momentum_analyzer and len(closes) >= 26:
                try:
                    momentum = momentum_analyzer.analyze(ticker, closes, highs, lows, volumes)
                except Exception as e:
                    logger.debug(f"[MOMENTUM] {ticker}: {e}")

            # SuperTrend (ATR trend direction + TP favourability)
            supertrend = None
            if st_analyzer and len(closes) >= 20:
                try:
                    supertrend = st_analyzer.analyze(ticker, highs, lows, closes, volumes)
                except Exception as e:
                    logger.debug(f"[ST] {ticker}: {e}")

            # AI Watch breakout override: floor fundamentals at 0 when RVOL≥1.5 + momentum confirmed
            is_ai_watch = ticker in config.ai_watch_tickers
            floor_fundamentals = (
                is_ai_watch
                and momentum is not None
                and momentum.signal in ("strong_buy", "buy")
                and momentum.rvol >= 1.5
            )

            # Signal aggregation
            agg_signal = aggregator.aggregate(
                analysis=analysis,
                fft=fft,
                fib=fib,
                insider=insider,
                vwap=vwap,
                vol_profile=vol_profile,
                vix_regime=vix_regime,
                current_price=current_price,
                momentum=momentum,
                supertrend=supertrend,
                floor_fundamentals=floor_fundamentals,
            )

            # L3: FUD filter
            l3_result = fud_engine.analyze_ticker(ticker, agg_signal)
            layer3_results[ticker] = l3_result

        except Exception as e:
            logger.error(f"L2/Signals/L3 failed for {ticker}: {e}")

    if not layer3_results:
        logger.info("No tickers passed L3 — cycle complete.")
        return

    # ── L4: Decision Engine ───────────────────────────────────────────
    # Fetch live quotes so signals store actual intraday price, not EOD close
    _live_prices: dict = {}
    try:
        from broker.market_data import SchwabMarketData as _SMD
        _lq = _SMD().get_quotes_batch(list(layer3_results.keys()))
        _live_prices = {t: round(float(q["last_price"]), 2)
                        for t, q in _lq.items() if q.get("last_price")}
    except Exception:
        pass

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
        max_trade_dollars=getattr(config.risk, "max_trade_dollars", 500.0),  # fixed: was wrong attr name -> always 500
        gate_overrides=_gate_overrides,
        live_prices=_live_prices,
    )

    # ── L5: Risk Manager ──────────────────────────────────────────────
    market_returns: list = []  # TODO: populate from SPY price history in DB
    risk_assessments = risk_manager.assess_watchlist(
        decisions=decisions,
        portfolio_value=portfolio_value,
        market_returns=market_returns,
    )

    # ── L6: Execute approved trades ───────────────────────────────────
    for ticker, assessment in risk_assessments.items():
        if assessment.approved:
            executor.execute(assessment)

    approved = [t for t, a in risk_assessments.items() if a.approved]
    logger.info(
        f"Cycle complete. {len(approved)} trade(s) {'logged (DRY RUN)' if config.risk.dry_run else 'executed'}"
        + (f": {approved}" if approved else ".")
    )


def main():
    setup_logging()
    setup_directories()

    logger.info("=" * 70)
    logger.info("new_world_order starting up")
    logger.info(f"DRY RUN MODE : {config.risk.dry_run}")
    logger.info(f"Watchlist    : {config.watchlist}")
    logger.info("=" * 70)

    # Database
    _, Session = init_db(config.database.url, echo=config.database.echo_sql)

    # L1
    pipeline = IngestionPipeline(db_session_factory=Session)

    # L2
    analysis_engine = FirstPrinciplesEngine(db_session_factory=Session)

    # Signals
    fft_detector      = FFTCycleDetector()
    fib_analyzer      = FibonacciAnalyzer()
    insider_analyzer  = InsiderFlowAnalyzer()
    vwap_calc         = VWAPCalculator()
    vol_analyzer      = VolumeProfileAnalyzer()
    vix_detector      = VIXRegimeDetector()
    aggregator        = SignalAggregator()
    momentum_analyzer = MomentumAnalyzer()
    st_analyzer       = SuperTrendAnalyzer()

    # L3
    fud_engine = FUDFilterEngine(db_session_factory=Session)

    # L4
    decision_engine = DecisionEngine(db_session_factory=Session)

    # L5
    risk_manager = RiskManager(db_session_factory=Session)

    # L6
    executor    = SchwabExecutor(db_session_factory=Session)
    market_data = SchwabMarketData()

    # L1: Initial ingest on startup
    logger.info("Running initial data ingestion...")
    pipeline.run_full_ingest()

    # Scheduler
    scheduler = setup_scheduler(pipeline)

    # Common kwargs for the trading cycle (avoids repetition)
    _cycle_kwargs = dict(
        Session=Session,
        analysis_engine=analysis_engine,
        fft_detector=fft_detector,
        fib_analyzer=fib_analyzer,
        insider_analyzer=insider_analyzer,
        vwap_calc=vwap_calc,
        vol_analyzer=vol_analyzer,
        vix_detector=vix_detector,
        aggregator=aggregator,
        fud_engine=fud_engine,
        decision_engine=decision_engine,
        risk_manager=risk_manager,
        executor=executor,
        market_data=market_data,
        momentum_analyzer=momentum_analyzer,
        st_analyzer=st_analyzer,
    )

    # Full watchlist trading cycle every 5 min, Mon-Fri market hours
    from apscheduler.triggers.cron import CronTrigger
    scheduler.add_job(
        func=lambda: run_trading_cycle(**_cycle_kwargs),
        trigger=CronTrigger(day_of_week="mon-fri", hour="9-16", minute="*/5"),
        id="trading_cycle",
        name="Full 6-layer analysis + trade cycle",
        replace_existing=True,
    )

    # AI Watch: 1-min fast scan for TSLA and other priority tickers
    # Runs at :00 each minute (offset by 30s from the price refresh at :30)
    scheduler.add_job(
        func=lambda: run_trading_cycle(**_cycle_kwargs, tickers_override=config.ai_watch_tickers),
        trigger=CronTrigger(day_of_week="mon-fri", hour="9-16", minute="*", second="0"),
        id="ai_watch_trading_1min",
        name="1-min AI Watch trading cycle (priority tickers)",
        replace_existing=True,
    )

    # Morning brief — generated at 7am ET Mon-Fri, cached for the day
    def _generate_brief():
        try:
            from monitor.morning_brief import get_brief
            get_brief(force_refresh=True)
            logger.info("[BRIEF] Morning brief generated and cached.")
        except Exception as e:
            logger.warning(f"[BRIEF] Generation failed: {e}")

    scheduler.add_job(
        func=_generate_brief,
        trigger=CronTrigger(day_of_week="mon-fri", hour=7, minute=0),
        id="morning_brief",
        name="Morning market brief generation",
        replace_existing=True,
    )

    # I-Tool scan — every 30 min Mon-Fri during market hours
    def _run_itool_scan():
        try:
            from monitor.itool import get_scan
            get_scan(force_refresh=True)
        except Exception as e:
            logger.warning(f"[ITOOL] Scan failed: {e}")

    scheduler.add_job(
        func=_run_itool_scan,
        trigger=CronTrigger(day_of_week="mon-fri", hour="9-16", minute="*/30"),
        id="itool_scan",
        name="S&P 500 I-Tool technical scan",
        replace_existing=True,
    )

    def _run_alfred():
        try:
            from monitor.alfred import _compute_alfred
            _compute_alfred()
        except Exception as e:
            logger.warning(f"[Alfred] scheduler failed: {e}")

    scheduler.add_job(
        func=_run_alfred,
        trigger=CronTrigger(day_of_week="mon-fri", hour="0-23", minute="*/5"),
        id="alfred_forecast",
        name="Alfred CME futures forecast (Mon-Fri)",
        replace_existing=True,
    )

    scheduler.add_job(
        func=_run_alfred,
        trigger=CronTrigger(day_of_week="sun", hour="18-23", minute="*/5"),
        id="alfred_forecast_sunday",
        name="Alfred CME futures forecast (Sunday evening)",
        replace_existing=True,
    )

    scheduler.add_job(
        func=lambda: __import__('monitor.alfred', fromlist=['_run_backtest_all'])._run_backtest_all(),
        trigger=CronTrigger(day_of_week="sun", hour=3),
        id="alfred_backtest_weekly",
        name="Alfred weekly backtest + calibration",
        replace_existing=True,
    )

    # Schwab token expiry monitor — Telegram alert before the 7-day refresh token lapses
    def _check_schwab_token():
        try:
            import json as _json
            from datetime import datetime as _dt, timedelta as _td
            from pathlib import Path as _P
            tok_path = _P(config.schwab.token_path)
            if not tok_path.exists():
                return
            ct = _json.loads(tok_path.read_text()).get("creation_timestamp")
            if not ct:
                return
            expiry = _dt.fromtimestamp(ct) + _td(days=7)
            hours_left = (expiry - _dt.now()).total_seconds() / 3600.0

            if   hours_left <= 0:  bucket = "expired"
            elif hours_left <= 12: bucket = "12h"
            elif hours_left <= 36: bucket = "36h"
            else:                  return   # not yet in alert range

            # Send each bucket at most once per token (keyed by this expiry)
            state_path = _P("data/token_alert_state.json")
            try:
                state = _json.loads(state_path.read_text()) if state_path.exists() else {}
            except Exception:
                state = {}
            key = expiry.isoformat()
            if bucket in state.get(key, []):
                return

            if bucket == "expired":
                msg = (f"\U0001F534 <b>Schwab token EXPIRED</b>\n"
                       f"7-day refresh token lapsed {expiry:%Y-%m-%d %H:%M}. Price feed is DOWN — "
                       f"run reauth.bat (or python get_token.py) now.")
            else:
                msg = (f"⚠️ <b>Schwab token expires in ~{hours_left:.0f}h</b>\n"
                       f"Refresh token lapses {expiry:%Y-%m-%d %H:%M}. Run reauth.bat before then "
                       f"to avoid a feed outage.")
            try:
                from monitor.telegram_bot import send_alert as _tg
                _tg(msg)
            except Exception as _te:
                logger.warning(f"[TOKEN] telegram alert failed: {_te}")

            state = {key: state.get(key, []) + [bucket]}   # keep only current token's buckets
            try:
                state_path.parent.mkdir(parents=True, exist_ok=True)
                state_path.write_text(_json.dumps(state, indent=2))
            except Exception:
                pass
            logger.info(f"[TOKEN] Schwab token alert sent: {bucket} ({hours_left:.0f}h left)")
        except Exception as e:
            logger.warning(f"[TOKEN] expiry check failed: {e}")

    scheduler.add_job(
        func=_check_schwab_token,
        trigger=CronTrigger(hour="*/6", minute=15),
        id="schwab_token_monitor",
        name="Schwab token expiry monitor + Telegram alert",
        replace_existing=True,
    )

    scheduler.start()
    _check_schwab_token()   # run once at startup so an already-near-expiry token alerts immediately
    logger.info("Scheduler started. Press Ctrl+C to stop.")

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        scheduler.shutdown()


if __name__ == "__main__":
    main()
