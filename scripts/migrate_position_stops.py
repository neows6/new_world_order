"""
scripts/migrate_position_stops.py — re-baseline stop levels on already-open paper positions.

Positions opened before the 2026-09-05 ATR fix carry stops computed from a
deflated ATR: `price_history` stored two rows per trading day, and the duplicate
zero-range bar dragged Wilder ATR down to ~0.6x of true, so stops intended at
2.5x ATR landed at ~1.46x — inside ordinary noise. New entries get correct stops
automatically; these already-open ones do not.

Recomputes each open position's stop as `avg_cost - MIN_STOP_ATR_MULT * ATR`,
using the same SuperTrendAnalyzer ATR the live signal path uses, on deduped bars.

Safety rules — a position is SKIPPED rather than modified when:
  * its stop is already at or above avg_cost (a ratcheted trailing / breakeven
    stop — moving that down would hand back locked-in profit)
  * the recomputed stop would TIGHTEN the existing one (never move a stop up
    under a live position; that can trigger an immediate exit)
  * the recomputed stop sits at or above the current price (would fire at once)
  * ATR is unavailable or non-positive

The -15% floor from signals/aggregator.py is applied so a high-ATR name can't
produce an absurdly wide stop.

Usage:
    python scripts/migrate_position_stops.py              # dry run, prints a table
    python scripts/migrate_position_stops.py --apply      # writes (backs up DBs first)
"""

import argparse
import shutil
import sys
import pathlib
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from loguru import logger

from config import config
from models.database import init_db, Company, PriceHistory
from paper.account import init_paper_db, PaperPosition
from paper.executor import PAPER_MODEL_CONFIGS
from signals.aggregator import MIN_STOP_ATR_MULT
from signals.supertrend import SuperTrendAnalyzer
from utils.price_data import ohlcv_arrays

STOP_FLOOR_PCT = 0.85       # matches the -15% floor in signals/aggregator.py


def _atr_for(Session, st: SuperTrendAnalyzer, ticker: str):
    """SuperTrend ATR on deduped bars — identical to what the live path computes."""
    with Session() as s:
        co = s.query(Company).filter_by(ticker=ticker).first()
        if not co:
            return None, None
        recs = (s.query(PriceHistory)
                .filter_by(company_id=co.id)
                .order_by(PriceHistory.date)
                .all())
    if not recs:
        return None, None
    bars = ohlcv_arrays(recs)
    if len(bars["closes"]) < 20:
        return None, None
    res = st.analyze(ticker, bars["highs"], bars["lows"], bars["closes"])
    atr = res.atr if (res and res.atr and res.atr > 0) else None
    return atr, bars["closes"][-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write the new stops (default is a dry run)")
    args = ap.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    _, Session = init_db(config.database.url, echo=False)
    st = SuperTrendAnalyzer()

    if args.apply:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = pathlib.Path("backups") / f"stopmigration_{stamp}"
        backup.mkdir(parents=True, exist_ok=True)
        for cfg in PAPER_MODEL_CONFIGS.values():
            src = pathlib.Path(cfg["db"])
            if src.exists():
                shutil.copy2(src, backup / src.name)
        print(f"Backed up {len(PAPER_MODEL_CONFIGS)} paper DBs -> {backup}\n")

    hdr = "%-13s %-6s %9s %9s %9s %8s %8s  %s" % (
        "model", "tkr", "entry", "old stop", "new stop", "old%", "new%", "action")
    print(hdr)
    print("-" * len(hdr))

    atr_cache = {}
    changed = skipped = 0
    widened_pct = []

    for name, cfg in PAPER_MODEL_CONFIGS.items():
        _, MSession = init_paper_db(cfg["db"])
        with MSession() as s:
            positions = s.query(PaperPosition).filter(PaperPosition.qty > 0).all()

            for p in positions:
                tk, entry, old = p.ticker, p.avg_cost, p.stop_loss

                if tk not in atr_cache:
                    atr_cache[tk] = _atr_for(Session, st, tk)
                atr, last_close = atr_cache[tk]

                def emit(new, action):
                    print("%-13s %-6s %9.2f %9.2f %9s %7.2f%% %8s  %s" % (
                        name, tk, entry, old or 0,
                        ("%9.2f" % new) if new else "        -",
                        100 * (entry - (old or 0)) / entry,
                        ("%.2f%%" % (100 * (entry - new) / entry)) if new else "-",
                        action))

                if old is None:
                    emit(None, "SKIP no existing stop")
                    skipped += 1
                    continue
                if atr is None:
                    emit(None, "SKIP no ATR available")
                    skipped += 1
                    continue
                if old >= entry:
                    emit(None, "SKIP trailing/breakeven stop preserved")
                    skipped += 1
                    continue

                new = max(entry - MIN_STOP_ATR_MULT * atr, entry * STOP_FLOOR_PCT)
                new = round(new, 2)

                if new >= old:
                    emit(new, "SKIP would tighten — preserved")
                    skipped += 1
                    continue
                if last_close and new >= last_close:
                    emit(new, "SKIP above current price")
                    skipped += 1
                    continue

                emit(new, "WIDEN" + ("" if args.apply else " (dry run)"))
                widened_pct.append(100 * (entry - new) / entry)
                changed += 1
                if args.apply:
                    p.stop_loss = new

            if args.apply:
                s.commit()

    print()
    print("%d widened, %d skipped" % (changed, skipped))
    if widened_pct:
        widened_pct.sort()
        print("new stop depth: median %.2f%%  min %.2f%%  max %.2f%%" % (
            widened_pct[len(widened_pct) // 2], widened_pct[0], widened_pct[-1]))
    print("DRY RUN — nothing written. Re-run with --apply to commit."
          if not args.apply else "Applied.")


if __name__ == "__main__":
    main()
