"""
backtest_runner.py — Signal diagnostic for a historical date.

Loads price history from the DB as-of a given date and runs all
signal indicators to show exactly why trades did or did not trigger.

Usage:
    python backtest_runner.py --date 2026-04-14
    python backtest_runner.py --date 2026-04-14 --ticker TSLA
    python backtest_runner.py --date 2026-04-14 --show-gate-breakdown

What it shows:
  - For each watchlist ticker: the raw indicator readings from that date
  - Old composite score (no momentum) vs new composite score (with momentum)
  - Which signals were strong and which were absent
  - Why a trade would or would not have passed the scoring threshold

Note: This runs entirely from DB data — no live API calls needed.
      Make sure the DB has been populated (run main.py at least once).
"""

import argparse
import sys
import os
from datetime import datetime, date, timedelta
from typing import Optional
from loguru import logger

# ── path setup ────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import config
from models.database import init_db, Company, PriceHistory, Fundamental, NewsItem
from signals.fft_cycles import FFTCycleDetector, FFTResult
from signals.fibonacci import FibonacciAnalyzer, FibResult
from signals.market_microstructure import VWAPCalculator, VolumeProfileAnalyzer, VIXRegimeDetector
from signals.momentum import MomentumAnalyzer, MomentumResult


# Signal weights — must match aggregator.py
NEW_WEIGHTS = {
    "fundamentals": 0.25,
    "momentum":     0.20,
    "insider":      0.20,
    "technical":    0.15,
    "cycle":        0.10,
    "volume":       0.10,
}
OLD_WEIGHTS = {
    "fundamentals": 0.35,
    "insider":      0.25,
    "technical":    0.20,
    "cycle":        0.10,
    "volume":       0.10,
}


# ─────────────────────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────────────────────

def load_price_data(session, ticker: str, as_of: date, lookback: int = 252) -> dict:
    """Load OHLCV history up to and including as_of date."""
    company = session.query(Company).filter_by(ticker=ticker).first()
    if not company:
        return {}

    records = (
        session.query(PriceHistory)
        .filter(PriceHistory.company_id == company.id, PriceHistory.date <= as_of)
        .order_by(PriceHistory.date)
        .all()
    )[-lookback:]

    if not records:
        return {}

    return {
        "closes":  [r.adjusted_close or r.close for r in records if (r.adjusted_close or r.close)],
        "highs":   [r.high for r in records if r.high],
        "lows":    [r.low for r in records if r.low],
        "volumes": [r.volume for r in records if r.volume],
        "dates":   [r.date for r in records],
        "price":   records[-1].adjusted_close or records[-1].close,
    }


def load_fundamentals(session, ticker: str, as_of: date) -> Optional[object]:
    """Most recent annual fundamental row as-of the given date."""
    company = session.query(Company).filter_by(ticker=ticker).first()
    if not company:
        return None
    return (
        session.query(Fundamental)
        .filter(
            Fundamental.company_id == company.id,
            Fundamental.period_end <= as_of,
            Fundamental.fiscal_quarter == 0,
        )
        .order_by(Fundamental.period_end.desc())
        .first()
    )


# ─────────────────────────────────────────────────────────────────────────────
# Score helpers  (mirrors aggregator.py logic)
# ─────────────────────────────────────────────────────────────────────────────

def fundamentals_score(fund) -> float:
    """Replicate aggregator._normalize_fundamental_score() without AnalysisReport."""
    if fund is None:
        return 0.0   # No data = neutral (not a hard block for backtest)

    score = 0.0
    # ROIC vs WACC
    if fund.roic and hasattr(fund, 'wacc') and fund.wacc:
        spread = fund.roic - fund.wacc
        if spread > 0.10:   score += 0.3
        elif spread > 0.05: score += 0.2
        elif spread > 0:    score += 0.1
        else:               score -= 0.2

    # Margin of safety proxy: use price_to_fcf if available
    # (full intrinsic value calc not run here — use a simple heuristic)
    # No margin_of_safety on raw Fundamental row, so just use ROIC as proxy
    if fund.roic:
        if fund.roic > 0.15: score += 0.2
        elif fund.roic < 0:  score -= 0.3

    return max(-1.0, min(1.0, score))


def technical_score(fib: Optional[FibResult], vwap) -> float:
    """Replicate aggregator._normalize_technical_score() (simplified)."""
    score = 0.0
    components = 0

    if fib:
        fib_map = {
            "strong_buy": 1.0, "buy": 0.6, "approaching_support": 0.3,
            "neutral": 0.0, "sell": -0.6, "strong_sell": -1.0,
        }
        fib_score = fib_map.get(fib.entry_signal, 0.0)
        if fib.in_golden_zone: fib_score = min(1.0, fib_score + 0.2)
        score += fib_score
        components += 1

    if vwap:
        vs = 0.0
        if vwap.is_extended_below:   vs += 0.6
        elif vwap.position == "below": vs += 0.3
        elif vwap.position == "at":    vs += 0.1
        elif vwap.is_extended_above:
            if vwap.vwap_slope == "rising" and vwap.institutional_bias == "accumulation":
                vs += 0.2
            else:
                vs -= 0.2
        if vwap.institutional_bias == "accumulation":   vs += 0.2
        elif vwap.institutional_bias == "distribution": vs -= 0.2
        score += vs
        components += 1

    return max(-1.0, min(1.0, score / components)) if components > 0 else 0.0


def cycle_score(fft: Optional[FFTResult]) -> float:
    if fft is None or fft.signal_strength < 0.3:
        return 0.0
    return fft.phase_score * fft.signal_strength


def volume_score(vol) -> float:
    if vol is None:
        return 0.0
    score = 0.0
    current = vol.current_price
    poc_distance = abs(current - vol.point_of_control) / current
    if poc_distance < 0.01:           score += 0.4
    elif vol.price_in_value_area:     score += 0.2
    if vol.nearest_hvn_below:
        hvn_dist = (current - vol.nearest_hvn_below) / current
        if hvn_dist < 0.02:           score += 0.3
    return max(-1.0, min(1.0, score))


def classify(composite: float) -> str:
    if composite >= 0.50:   return "STRONG BUY"
    elif composite >= 0.25: return "BUY"
    elif composite >= -0.15: return "HOLD"
    elif composite >= -0.40: return "SELL"
    else:                   return "STRONG SELL"


# ─────────────────────────────────────────────────────────────────────────────
# Per-ticker diagnostic
# ─────────────────────────────────────────────────────────────────────────────

def run_ticker_diagnostic(session, ticker: str, as_of: date) -> dict:
    fft_det   = FFTCycleDetector()
    fib_an    = FibonacciAnalyzer()
    vwap_calc = VWAPCalculator()
    vol_an    = VolumeProfileAnalyzer()
    mom_an    = MomentumAnalyzer()
    vix_det   = VIXRegimeDetector()

    result = {"ticker": ticker, "as_of": as_of, "error": None}

    price = load_price_data(session, ticker, as_of)
    if not price:
        result["error"] = "No price data in DB for this date (run ingestion first)"
        return result

    fund = load_fundamentals(session, ticker, as_of)

    closes  = price["closes"]
    highs   = price["highs"]
    lows    = price["lows"]
    volumes = price["volumes"]
    current = price["price"]
    result["price"] = current

    # Run signals
    fft_result = vwap_result = fib_result = vol_result = mom_result = None

    try:
        fft_result = fft_det.analyze(ticker=ticker, prices=closes)
    except Exception as e:
        logger.debug(f"{ticker} FFT: {e}")

    try:
        fib_result = fib_an.analyze(ticker=ticker, highs=highs, lows=lows, closes=closes)
    except Exception as e:
        logger.debug(f"{ticker} Fib: {e}")

    try:
        vwap_result = vwap_calc.compute_daily(
            ticker=ticker, highs=highs, lows=lows, closes=closes, volumes=volumes
        )
    except Exception as e:
        logger.debug(f"{ticker} VWAP: {e}")

    try:
        vol_result = vol_an.analyze(
            ticker=ticker, highs=highs, lows=lows, closes=closes, volumes=volumes
        )
    except Exception as e:
        logger.debug(f"{ticker} VolProfile: {e}")

    try:
        mom_result = mom_an.analyze(
            ticker=ticker, closes=closes, highs=highs, lows=lows, volumes=volumes
        )
    except Exception as e:
        logger.debug(f"{ticker} Momentum: {e}")

    # Scores
    f_score = fundamentals_score(fund)
    t_score = technical_score(fib_result, vwap_result)
    c_score = cycle_score(fft_result)
    v_score = volume_score(vol_result)
    m_score = mom_result.composite_momentum_score if mom_result else 0.0

    # Old composite (no momentum)
    old_composite = (
        OLD_WEIGHTS["fundamentals"] * f_score +
        OLD_WEIGHTS["insider"]      * 0.0     +   # No insider data in diagnostic
        OLD_WEIGHTS["technical"]    * t_score +
        OLD_WEIGHTS["cycle"]        * c_score +
        OLD_WEIGHTS["volume"]       * v_score
    )

    # New composite (with momentum, insider stays 0 for diagnostic)
    new_composite = (
        NEW_WEIGHTS["fundamentals"] * f_score +
        NEW_WEIGHTS["momentum"]     * m_score +
        NEW_WEIGHTS["insider"]      * 0.0     +
        NEW_WEIGHTS["technical"]    * t_score +
        NEW_WEIGHTS["cycle"]        * c_score +
        NEW_WEIGHTS["volume"]       * v_score
    )

    result.update({
        "f_score":       f_score,
        "t_score":       t_score,
        "c_score":       c_score,
        "v_score":       v_score,
        "m_score":       m_score,
        "old_composite": old_composite,
        "new_composite": new_composite,
        "old_signal":    classify(old_composite),
        "new_signal":    classify(new_composite),
        "fund":          fund,
        "fft":           fft_result,
        "fib":           fib_result,
        "vwap":          vwap_result,
        "vol":           vol_result,
        "mom":           mom_result,
    })
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Report printer
# ─────────────────────────────────────────────────────────────────────────────

W = 72

def _sig_arrow(sig: str) -> str:
    return "✅" if "BUY" in sig else ("⚠️ " if sig == "HOLD" else "🔴")

def print_report(results: list, as_of: date, show_gates: bool):
    print()
    print("╔" + "═" * W + "╗")
    print(f"║  BACKTEST SIGNAL DIAGNOSTIC — {as_of}" + " " * (W - 35) + "║")
    print(f"║  (data from DB as-of this date; insider score excluded)" + " " * (W - 55) + "║")
    print("╠" + "═" * W + "╣")

    for r in results:
        print()
        ticker = r["ticker"]

        if r.get("error"):
            print(f"  {ticker:<8}  ⚠  {r['error']}")
            continue

        price_str = f"  Price: ${r['price']:.2f}" if r.get("price") else ""
        old_sig = r["old_signal"]
        new_sig = r["new_signal"]

        print(f"  {ticker:<8}{price_str}")
        print(f"  {'─'*68}")
        print(f"  OLD system (no momentum):  {r['old_composite']:+.3f}  →  {_sig_arrow(old_sig)} {old_sig}")
        print(f"  NEW system (w/ momentum):  {r['new_composite']:+.3f}  →  {_sig_arrow(new_sig)} {new_sig}")

        # Score table
        print(f"  {'─'*68}")
        print(f"  SCORE BREAKDOWN:")
        print(f"    {'Signal':<18} {'Raw Score':>10}  {'Old contrib':>12}  {'New contrib':>12}")
        print(f"    {'──────':<18} {'─────────':>10}  {'───────────':>12}  {'───────────':>12}")
        rows = [
            ("Fundamentals",  r["f_score"], OLD_WEIGHTS["fundamentals"], NEW_WEIGHTS["fundamentals"]),
            ("Momentum (NEW)",r["m_score"], 0.0,                          NEW_WEIGHTS["momentum"]),
            ("Insider*",      0.0,          OLD_WEIGHTS["insider"],        NEW_WEIGHTS["insider"]),
            ("Technical",     r["t_score"], OLD_WEIGHTS["technical"],      NEW_WEIGHTS["technical"]),
            ("FFT Cycle",     r["c_score"], OLD_WEIGHTS["cycle"],          NEW_WEIGHTS["cycle"]),
            ("Vol Profile",   r["v_score"], OLD_WEIGHTS["volume"],         NEW_WEIGHTS["volume"]),
        ]
        for name, raw, ow, nw in rows:
            print(f"    {name:<18} {raw:>+10.3f}  {raw*ow:>+12.3f}  {raw*nw:>+12.3f}")
        print(f"    {'TOTAL':<18} {'':>10}  {r['old_composite']:>+12.3f}  {r['new_composite']:>+12.3f}")
        print(f"    * Insider not computed in diagnostic (requires live EDGAR fetch)")

        # Momentum detail
        mom = r.get("mom")
        if mom:
            print(f"  {'─'*68}")
            print(f"  MOMENTUM DETAIL  (composite: {mom.composite_momentum_score:+.3f}  →  {mom.signal.upper()})")
            rvol_flag = "  ⚡ SURGE" if mom.rvol >= 2.0 else ("  ⬇ low" if mom.rvol < 0.8 else "")
            print(f"    RVOL:        {mom.rvol:.2f}×{rvol_flag}")
            print(f"    MACD:        {mom.macd_direction}  (histogram: {mom.macd_hist:+.3f})")
            print(f"    MA Stack:    {mom.ma_stack}")
            print(f"    ROC:         5d={mom.roc_5d:+.1f}%  20d={mom.roc_20d:+.1f}%")
            bk_str = "  🚀 BREAKOUT!" if mom.is_52w_high_breakout else ""
            print(f"    52w High:    breakout={mom.is_52w_high_breakout}{bk_str}")
            if mom.reason:
                print(f"    → {mom.reason}")

        # Technical detail (if show_gates)
        if show_gates:
            fib  = r.get("fib")
            vwap = r.get("vwap")
            fft  = r.get("fft")
            vol  = r.get("vol")
            fund = r.get("fund")

            print(f"  {'─'*68}")
            print(f"  GATE DETAIL:")

            if fund:
                roic_str = f"ROIC={fund.roic:.1%}" if fund.roic else "ROIC=n/a"
                print(f"    Fundamentals: {roic_str}  score={r['f_score']:+.3f}")
            else:
                print(f"    Fundamentals: no data in DB  score={r['f_score']:+.3f}")

            if fib:
                gz = " [GOLDEN ZONE]" if fib.in_golden_zone else ""
                print(f"    Fibonacci:   signal={fib.entry_signal}  conf={fib.confluence_score:.2f}{gz}")
            else:
                print(f"    Fibonacci:   insufficient data")

            if vwap:
                print(f"    VWAP:        ${vwap.vwap:.2f}  price {vwap.position} VWAP  slope={vwap.vwap_slope}  bias={vwap.institutional_bias}")
            else:
                print(f"    VWAP:        insufficient data")

            if fft and fft.signal_strength >= 0.3:
                print(f"    FFT Cycle:   phase={fft.current_cycle_phase}  strength={fft.signal_strength:.2f}  score={r['c_score']:+.3f}")
            else:
                print(f"    FFT Cycle:   weak/noisy signal (strength < 0.3) → neutral")

            if vol:
                print(f"    Vol Profile: POC=${vol.point_of_control:.2f}  RVOL={vol.rvol:.2f}×  in_VA={vol.price_in_value_area}")

        print()

    # Summary
    print("╠" + "═" * W + "╣")
    buys_old = [r for r in results if not r.get("error") and "BUY" in r.get("old_signal","")]
    buys_new = [r for r in results if not r.get("error") and "BUY" in r.get("new_signal","")]
    print(f"║  OLD system:  {len(buys_old):>2} / {len(results)} tickers at BUY or STRONG BUY" + " " * (W - 46) + "║")
    print(f"║  NEW system:  {len(buys_new):>2} / {len(results)} tickers at BUY or STRONG BUY" + " " * (W - 46) + "║")
    print("╚" + "═" * W + "╝")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Signal diagnostic for a historical date")
    parser.add_argument("--date", default=str(date.today() - timedelta(days=1)),
                        help="Date to check (YYYY-MM-DD). Default: yesterday")
    parser.add_argument("--ticker", default=None,
                        help="Single ticker. Default: full config.watchlist")
    parser.add_argument("--show-gate-breakdown", action="store_true",
                        help="Show Fibonacci, VWAP, FFT and fundamentals detail")
    args = parser.parse_args()

    try:
        as_of = datetime.strptime(args.date, "%Y-%m-%d").date()
    except ValueError:
        print(f"ERROR: Invalid date '{args.date}'. Use YYYY-MM-DD.")
        sys.exit(1)

    # Force UTF-8 output on Windows so box-drawing characters render correctly
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    os.makedirs("data", exist_ok=True)
    os.makedirs("logs", exist_ok=True)
    logger.remove()
    logger.add(sys.stderr, level="WARNING", colorize=True,
               format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}")

    _, Session = init_db(config.database.url, echo=False)
    tickers = [args.ticker] if args.ticker else config.watchlist

    print(f"\nDiagnostic: {as_of}  |  tickers: {tickers}")

    results = []
    with Session() as session:
        for t in tickers:
            results.append(run_ticker_diagnostic(session, t, as_of))

    print_report(results, as_of, show_gates=args.show_gate_breakdown)


if __name__ == "__main__":
    main()
