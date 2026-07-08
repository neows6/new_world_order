"""
diagnose.py — NWO System Health Check + Gate Diagnostic.

Run this any time to understand exactly why signals are (or aren't) firing.

Usage:
    python diagnose.py                # DB health + last signal per ticker
    python diagnose.py --gates TSLA   # Full gate breakdown for one ticker
    python diagnose.py --gates all    # Gate breakdown for every watchlist ticker
"""

import argparse
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from loguru import logger
logger.remove()
logger.add(sys.stderr, level="WARNING")   # Suppress info spam during diagnosis

from config import config
from models.database import init_db, Company, PriceHistory, Fundamental, TradeSignal

# ─────────────────────────────────────────────────────────────────────────────
# DB health check
# ─────────────────────────────────────────────────────────────────────────────

def check_db(Session) -> dict:
    """Return counts and last dates for every table."""
    results = {}
    with Session() as session:
        for ticker in config.watchlist + config.ai_watch_tickers:
            company = session.query(Company).filter_by(ticker=ticker).first()
            if not company:
                results[ticker] = {"company": False, "prices": 0, "fundamentals": 0, "last_price": None}
                continue

            price_count = session.query(PriceHistory).filter_by(company_id=company.id).count()
            fund_count  = session.query(Fundamental).filter_by(company_id=company.id).count()
            last_price  = (
                session.query(PriceHistory.date)
                .filter_by(company_id=company.id)
                .order_by(PriceHistory.date.desc())
                .limit(1)
                .scalar()
            )
            last_signal = (
                session.query(TradeSignal)
                .filter_by(company_id=company.id)
                .order_by(TradeSignal.generated_at.desc())
                .first()
            )

            results[ticker] = {
                "company": True,
                "prices": price_count,
                "fundamentals": fund_count,
                "last_price": last_price,
                "last_signal": last_signal,
            }
    return results


def print_db_health(db_status: dict):
    print("\n" + "═" * 65)
    print("  NWO SYSTEM HEALTH CHECK")
    print("═" * 65)

    any_empty = False
    for ticker, info in db_status.items():
        ai = " ★AI" if ticker in config.ai_watch_tickers else ""
        if not info["company"]:
            print(f"  {ticker:8s}{ai} — ❌ NOT IN DB (run ingestion)")
            any_empty = True
            continue

        prices = info["prices"]
        last   = info["last_price"]
        last_sig = info.get("last_signal")

        price_str = f"{prices} bars (last: {last})" if last else f"{prices} bars (NO DATA)"
        signal_str = (
            f"{last_sig.signal} @ {last_sig.generated_at.strftime('%H:%M')}"
            if last_sig else "no signals yet"
        )

        flag = "✓" if prices >= 60 else "⚠" if prices > 0 else "❌"
        print(f"  {ticker:8s}{ai} — {flag} {price_str:35s} | last signal: {signal_str}")

        if prices == 0:
            any_empty = True

    if any_empty:
        print()
        print("  ACTION REQUIRED: Run ingestion to populate empty tickers:")
        print("    python -m pipeline.ingestion")
        print("    python -m pipeline.ingestion --ticker TSLA --days 365")

    print()


# ─────────────────────────────────────────────────────────────────────────────
# Gate breakdown for a single ticker
# ─────────────────────────────────────────────────────────────────────────────

def run_gate_breakdown(ticker: str, Session) -> None:
    """Run all pipeline stages for ticker and print per-gate pass/fail."""
    from analysis.engine import FirstPrinciplesEngine, AnalysisReport
    from signals.fft_cycles import FFTCycleDetector
    from signals.fibonacci import FibonacciAnalyzer
    from signals.insider_flow import InsiderFlowAnalyzer
    from signals.market_microstructure import VWAPCalculator, VolumeProfileAnalyzer, VIXRegimeDetector
    from signals.momentum import MomentumAnalyzer
    from signals.supertrend import SuperTrendAnalyzer
    from signals.three_green_arrows import ThreeGreenArrowsAnalyzer
    from signals.tipranks_signal import TipRanksAnalyzer
    from signals.aggregator import SignalAggregator, SIGNAL_WEIGHTS, BUY_THRESHOLD
    from fud.filter_engine import FUDFilterEngine
    from decision.engine import DecisionEngine
    from decision.reynolds_turbulence import ReynoldsMarketAnalyzer
    from decision.quantum_kalman import QuantumStateAnalyzer, KalmanPriceFilter
    from decision.ensemble_kelly import EnsembleKellyEngine

    # ── Load price data ───────────────────────────────────────────
    with Session() as session:
        company = session.query(Company).filter_by(ticker=ticker).first()
        if not company:
            print(f"\n  {ticker}: NOT IN DATABASE — run ingestion first\n")
            return

        records = (
            session.query(PriceHistory)
            .filter_by(company_id=company.id)
            .order_by(PriceHistory.date)
            .all()
        )[-252:]

    if len(records) < 20:
        print(f"\n  {ticker}: INSUFFICIENT DATA ({len(records)} bars, need 20+)\n")
        return

    closes  = [r.adjusted_close or r.close for r in records if (r.adjusted_close or r.close)]
    highs   = [r.high   for r in records if r.high]
    lows    = [r.low    for r in records if r.low]
    volumes = [r.volume for r in records if r.volume]
    price   = closes[-1]
    cik     = company.cik or ""

    print(f"\n{'═'*65}")
    print(f"  GATE BREAKDOWN: {ticker}")
    print(f"  {len(closes)} price bars | last close: ${price:.2f} | as of {records[-1].date}")
    print(f"{'═'*65}")

    # ── L2 Analysis ───────────────────────────────────────────────
    l2 = FirstPrinciplesEngine(Session)
    analysis = None
    try:
        analysis = l2.analyze_ticker(ticker)
    except Exception as e:
        print(f"  [L2] ERROR: {e}")

    if analysis is None:
        analysis = AnalysisReport(
            ticker=ticker, company_name=ticker,
            analyzed_at=datetime.utcnow().isoformat(),
            wacc=None, cost_of_equity=None, beta=None,
            moat_strength="none", moat_score=0.0,
            moat_types=[], moat_signals=[], moat_warnings=[],
            latest_year=None, revenue=None, gross_margin=None, roic=None,
            owner_earnings=None, free_cash_flow=None, net_debt=None,
            net_debt_to_ebitda=None, current_price=price,
            intrinsic_value_conservative=None, intrinsic_value_base=None,
            margin_of_safety=None, price_to_intrinsic=None, is_undervalued=False,
            dcf_bear_iv=None, dcf_base_iv=None, dcf_bull_iv=None,
            is_investable=False, investable_reasons=[], analysis_notes=[],
            analysis_warnings=[],
        )

    # ── Compute signals ───────────────────────────────────────────
    fft = None
    try:
        fft = FFTCycleDetector().analyze(ticker, closes)
    except Exception: pass

    fib = None
    try:
        fib = FibonacciAnalyzer().analyze(ticker, highs, lows, closes)
    except Exception: pass

    vwap = None
    try:
        vwap = VWAPCalculator().compute_daily(ticker, highs, lows, closes, volumes)
    except Exception: pass

    vol_prof = None
    try:
        vol_prof = VolumeProfileAnalyzer().analyze(ticker, highs, lows, closes, volumes)
    except Exception: pass

    momentum = None
    try:
        momentum = MomentumAnalyzer().analyze(ticker, closes, highs, lows, volumes)
    except Exception as e:
        print(f"  [MOMENTUM] ERROR: {e}")

    insider = None
    try:
        insider = InsiderFlowAnalyzer().score(ticker, cik, price)
    except Exception: pass

    supertrend = None
    try:
        supertrend = SuperTrendAnalyzer().analyze(ticker, highs, lows, closes, volumes)
    except Exception: pass

    tga = None
    try:
        tga = ThreeGreenArrowsAnalyzer().analyze(ticker, closes, highs, lows, volumes)
    except Exception: pass

    tipranks = None
    try:
        from data_sources.tipranks_client import get_client as _tr_client
        raw_tr = _tr_client().get_stock_data(ticker)
        if raw_tr:
            tipranks = TipRanksAnalyzer().analyze(ticker, raw_tr, current_price=price)
    except Exception: pass

    vix_level = 20.0
    try:
        from broker.market_data import SchwabMarketData
        q = SchwabMarketData().get_quote("$VIX.X")
        if q and q.get("last"):
            vix_level = float(q["last"])
    except Exception: pass
    vix_regime = VIXRegimeDetector().classify(vix_level)

    # ── Aggregate ─────────────────────────────────────────────────
    agg = SignalAggregator()
    is_ai_watch  = ticker in config.ai_watch_tickers
    is_breakout  = (momentum and momentum.signal in ("strong_buy", "buy")
                    and momentum.rvol >= 1.5)
    floor_funds  = is_ai_watch and is_breakout

    agg_signal = agg.aggregate(
        analysis=analysis, fft=fft, fib=fib, insider=insider,
        vwap=vwap, vol_profile=vol_prof, vix_regime=vix_regime,
        current_price=price, momentum=momentum,
        supertrend=supertrend, tipranks=tipranks, tga=tga,
        floor_fundamentals=floor_funds,
    )

    # ── Print score breakdown ─────────────────────────────────────
    ai_note = " ★AI Watch" if is_ai_watch else ""
    brk_note = " [BREAKOUT OVERRIDE active]" if floor_funds else ""
    print(f"\n  Composite: {agg_signal.composite_score:+.3f} → {agg_signal.signal.upper()}{ai_note}{brk_note}")
    print(f"  Confidence: {agg_signal.confidence:.0%} | VIX: {vix_level:.1f} ({agg_signal.vix_regime})")
    if agg_signal.signal == "watch" and agg_signal.watch_reasons:
        print(f"\n  {'WATCH — missing confirmations:':<45}")
        for r in agg_signal.watch_reasons:
            print(f"    [ ] {r}")
    print()
    print(f"  {'SIGNAL BREAKDOWN':─<45}")

    # Compute per-signal scores using the same normalizers as the aggregator,
    # so the breakdown reflects what actually feeds the composite.
    _st_score  = agg._normalize_supertrend_score(supertrend)
    _tr_score  = agg._normalize_tipranks_score(tipranks)
    _tga_score = agg._normalize_tga_score(tga)

    components = [
        ("Fundamentals",  agg_signal.fundamentals_score, SIGNAL_WEIGHTS.get("fundamentals", 0),
         f"moat={analysis.moat_strength} MoS={analysis.margin_of_safety:.0%}" if analysis.margin_of_safety is not None else "no data"),
        ("Momentum",      agg_signal.momentum_score,     SIGNAL_WEIGHTS.get("momentum", 0),
         f"RVOL={momentum.rvol:.1f}x {momentum.macd_direction} {momentum.ma_stack} [{momentum.signal}]" if momentum else "no data"),
        ("Insider",       agg_signal.insider_score,       SIGNAL_WEIGHTS.get("insider", 0),
         f"cluster_buy={agg_signal.insider_cluster_buy}" if insider else "no Form4 data"),
        ("Technical",     agg_signal.technical_score,     SIGNAL_WEIGHTS.get("technical", 0),
         f"fib={agg_signal.fib_confluence_score:.2f} vwap={agg_signal.vwap_position}" if fib or vwap else "no data"),
        ("SuperTrend",    _st_score,                      SIGNAL_WEIGHTS.get("supertrend", 0),
         f"dir={agg_signal.supertrend_direction} fav={agg_signal.supertrend_favourability:.2f}" if supertrend else "no data"),
        ("TipRanks",      _tr_score,                      SIGNAL_WEIGHTS.get("tipranks", 0),
         f"smart={agg_signal.tipranks_smart_score} buy_pct={agg_signal.tipranks_buy_pct}" if tipranks else "no data (cookies?)"),
        ("3 Green Arrows",_tga_score,                     SIGNAL_WEIGHTS.get("tga", 0),
         f"arrows={agg_signal.tga_arrows_count}/3 [{agg_signal.tga_signal}]" if tga else "no data"),
        ("FFT Cycle",     agg_signal.cycle_score,         SIGNAL_WEIGHTS.get("cycle", 0),
         f"phase={agg_signal.fft_phase} strength={agg_signal.fft_signal_strength:.2f}" if fft else "insufficient data"),
        ("Volume",        agg_signal.volume_score,        SIGNAL_WEIGHTS.get("volume", 0),
         f"poc={agg_signal.poc_level:.2f}" if agg_signal.poc_level else "no profile"),
    ]

    for name, score, weight, detail in components:
        contrib = score * weight
        print(f"  {name:14s} {score:+.3f}  ×{weight:.0%}  = {contrib:+.3f}   {detail}")
    print(f"  {'─'*45}")
    print(f"  {'Composite':14s} {agg_signal.composite_score:+.3f}              (buy threshold: {BUY_THRESHOLD:+.2f})")

    # ── L3 FUD gate ───────────────────────────────────────────────
    print(f"\n  {'FUD FILTER (L3)':─<45}")
    try:
        fud_engine = FUDFilterEngine(Session)
        layer3 = fud_engine.analyze_ticker(ticker, agg_signal)
        status = "✓ PASS" if layer3.proceed_to_execution else "✗ BLOCK"
        print(f"  {status} — {layer3.gate_reason}")
        print(f"  Articles: {layer3.fud_analysis.total_articles} | "
              f"avg_fud={layer3.fud_analysis.avg_fud_score:.2f} | "
              f"quality_ratio={layer3.fud_analysis.quality_signal_ratio:.0%}")
        print(f"  Adjusted signal: {layer3.adjusted_signal} | "
              f"Adjusted composite: {layer3.adjusted_composite_score:+.3f}")
    except Exception as e:
        print(f"  ERROR running FUD filter: {e}")
        layer3 = None

    if layer3 is None:
        print("\n  L4/L5 skipped (L3 error)")
        print(f"{'═'*65}\n")
        return

    # ── L4 Decision gates ─────────────────────────────────────────
    print(f"\n  {'DECISION ENGINE (L4)':─<45}")

    reynolds_analyzer = ReynoldsMarketAnalyzer()
    quantum_analyzer  = QuantumStateAnalyzer()
    kalman_filter     = KalmanPriceFilter()
    ensemble_engine   = EnsembleKellyEngine()

    try:
        reynolds = reynolds_analyzer.analyze(ticker=ticker, closes=closes, highs=highs, lows=lows, volumes=volumes)
        re_num = reynolds.reynolds_number
        # AI Watch override: allow extreme turbulence for confirmed breakout signals,
        # same logic as DecisionEngine._run_gates()
        ai_watch_re_override = (
            is_ai_watch
            and not reynolds.allow_entry
            and layer3 is not None
            and layer3.adjusted_signal in ("buy", "strong_buy")
        )
        if ai_watch_re_override:
            reynolds.allow_entry = True
            reynolds.position_multiplier = 0.15
        re_ok = reynolds.allow_entry
        if ai_watch_re_override:
            print(f"  ✓ Reynolds: Re={re_num:.2f} {reynolds.regime.upper()} "
                  f"(AI Watch override — position capped at 15%)")
        else:
            print(f"  {'✓' if re_ok else '✗'} Reynolds: Re={re_num:.2f} {reynolds.regime.upper()} "
                  f"(entry {'allowed' if re_ok else 'BLOCKED — extreme turbulence'})")
    except Exception as e:
        print(f"  ? Reynolds: ERROR {e}")
        reynolds = None

    try:
        quantum = quantum_analyzer.compute(
            fundamental_score=agg_signal.fundamentals_score,
            technical_score=agg_signal.technical_score,
            insider_score=agg_signal.insider_score,
            news_quality_score=layer3.fud_analysis.avg_fud_score,
            cycle_score=agg_signal.cycle_score,
        )
        q_ok = quantum.state_certainty >= DecisionEngine.MIN_QUANTUM_CERTAIN
        print(f"  {'✓' if q_ok else '✗'} Quantum: certainty={quantum.state_certainty:.0%} "
              f"(need ≥{DecisionEngine.MIN_QUANTUM_CERTAIN:.0%}) | {quantum.dominant_state.upper()}")
    except Exception as e:
        print(f"  ? Quantum: ERROR {e}")
        quantum = None

    try:
        kalman = kalman_filter.filter(prices=closes)
    except Exception:
        kalman = None

    try:
        momentum_score = getattr(agg_signal, "momentum_score", 0.0)
        quantum_score  = DecisionEngine._compute_quantum_score(None, quantum) if quantum else 0.0
        kalman_score   = DecisionEngine._compute_kalman_score(None, kalman)   if kalman  else 0.0
        reynolds_mult  = reynolds.position_multiplier if reynolds else 0.75
        reynolds_regime = reynolds.regime if reynolds else "transient"

        ensemble = ensemble_engine.run(
            ticker=ticker,
            fundamental_score=agg_signal.fundamentals_score,
            quantum_score=quantum_score,
            kalman_score=kalman_score,
            reynolds_position_mult=reynolds_mult,
            reynolds_regime=reynolds_regime,
            technical_score=agg_signal.technical_score,
            insider_score=agg_signal.insider_score,
            momentum_score=momentum_score,
            win_probability_estimate=quantum.p_bull if quantum else 0.5,
            risk_reward_ratio=agg_signal.risk_reward_ratio or 2.0,
            current_price=price,
            portfolio_value=None,
        )
        e_ok = ensemble.ensemble_probability_bull >= DecisionEngine.MIN_ENSEMBLE_PROB
        print(f"  {'✓' if e_ok else '✗'} Ensemble: P(bull)={ensemble.ensemble_probability_bull:.0%} "
              f"(need ≥{DecisionEngine.MIN_ENSEMBLE_PROB:.0%}) | spread={ensemble.spread_category}")
    except Exception as e:
        print(f"  ? Ensemble: ERROR {e}")
        ensemble = None

    rr_ok = (agg_signal.risk_reward_ratio or 0) >= DecisionEngine.MIN_RR_RATIO
    print(f"  {'✓' if rr_ok else '✗'} R/R: {agg_signal.risk_reward_ratio or 0:.1f}:1 (need ≥{DecisionEngine.MIN_RR_RATIO:.1f})")

    # Gate 6: Kalman innovation — AI Watch breakout relaxes limit from 2.5σ to 5.0σ
    kalman_ok = True
    if kalman:
        is_ai_watch_breakout = is_ai_watch and layer3 and layer3.adjusted_signal in ("buy", "strong_buy")
        kalman_limit = 5.0 if is_ai_watch_breakout else DecisionEngine.MAX_KALMAN_SURPRISE
        innovation = abs(kalman.innovation_normalized)
        kalman_ok = innovation <= kalman_limit
        ai_note = f" (AI Watch limit {kalman_limit:.0f}σ)" if is_ai_watch_breakout else ""
        print(f"  {'✓' if kalman_ok else '✗'} Kalman: innovation={innovation:.1f}σ "
              f"(need ≤{kalman_limit:.1f}σ){ai_note} trend={kalman.trend_direction}")

    gate7_ok = layer3.adjusted_signal in ("buy", "strong_buy")
    print(f"  {'✓' if gate7_ok else '✗'} Gate 7 (signal): adjusted_signal={layer3.adjusted_signal} "
          f"({'pass' if gate7_ok else 'BLOCKED — need buy or strong_buy'})")

    fud_ok = layer3.proceed_to_execution
    re_pass  = reynolds.allow_entry if reynolds else True
    q_pass   = (quantum.state_certainty >= DecisionEngine.MIN_QUANTUM_CERTAIN) if quantum else False
    e_pass   = (ensemble.ensemble_probability_bull >= DecisionEngine.MIN_ENSEMBLE_PROB) if ensemble else False

    all_pass = fud_ok and re_pass and q_pass and e_pass and rr_ok and kalman_ok and gate7_ok
    print(f"\n  L4 final: {'✅ GO' if all_pass else '❌ NO-GO'}")

    if not all_pass:
        blockers = []
        if not fud_ok:  blockers.append("FUD filter")
        if not re_pass: blockers.append("Reynolds turbulence")
        if not q_pass:   blockers.append("Quantum certainty")
        if not e_pass:   blockers.append("Ensemble P(bull)")
        if not rr_ok:    blockers.append("R/R ratio")
        if not kalman_ok: blockers.append("Kalman innovation")
        if not gate7_ok: blockers.append("Gate 7 (signal level)")
        print(f"  Blocking gates: {', '.join(blockers)}")

    print(f"\n{'═'*65}\n")


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="NWO System Diagnostic")
    parser.add_argument("--gates", metavar="TICKER_OR_ALL",
                        help='Run full gate breakdown: ticker symbol or "all"')
    args = parser.parse_args()

    engine, Session = init_db(config.database.url, echo=False)

    # Always print DB health
    db_status = check_db(Session)
    print_db_health(db_status)

    if args.gates:
        tickers = (config.watchlist + config.ai_watch_tickers) if args.gates.lower() == "all" else [args.gates.upper()]
        # Deduplicate while preserving order
        seen = set()
        tickers = [t for t in tickers if not (t in seen or seen.add(t))]
        for ticker in tickers:
            run_gate_breakdown(ticker, Session)


if __name__ == "__main__":
    main()
