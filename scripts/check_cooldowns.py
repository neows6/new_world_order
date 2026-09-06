"""
scripts/check_cooldowns.py — show which tickers are benched by the post-stop
re-entry cooldown, per paper model.

The cooldown is enforced at execute time in paper/runner.py, AFTER the L4
decision engine has run. That means the dashboard's trade-diagnostic modal will
happily report a clean GO for a ticker that the cooldown then declines to buy.
This script is the way to answer "why didn't it buy X?" in that case.

Usage:
    python scripts/check_cooldowns.py
"""

import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from loguru import logger

logger.remove()      # keep the report clean

from paper.executor import PaperExecutor, PAPER_MODEL_CONFIGS
from paper.runner import _stop_cooldown_block, STOP_REENTRY_COOLDOWN_DAYS

MAIN_DB = "data/schwab_trader.db"


def _last_closes(tickers):
    """Latest EOD close per ticker — stands in for a live quote out of hours."""
    out = {}
    conn = sqlite3.connect(MAIN_DB)
    try:
        for t in tickers:
            row = conn.execute(
                """SELECT ph.close FROM price_history ph
                   JOIN companies co ON co.id = ph.company_id
                   WHERE co.ticker = ? ORDER BY ph.date DESC LIMIT 1""", (t,)
            ).fetchone()
            out[t] = row[0] if row else None
    finally:
        conn.close()
    return out


def main():
    print("post-stop re-entry cooldown = %d days\n" % STOP_REENTRY_COOLDOWN_DAYS)
    total = 0
    for name, cfg in PAPER_MODEL_CONFIGS.items():
        ex = PaperExecutor(db_path=cfg["db"], stagegate_file=cfg["stagegate"])
        conn = sqlite3.connect(cfg["db"])
        try:
            tickers = [r[0] for r in conn.execute(
                "SELECT DISTINCT ticker FROM paper_trades ORDER BY ticker")]
        finally:
            conn.close()

        prices = _last_closes(tickers)
        benched = [(t, r) for t in tickers
                   if (r := _stop_cooldown_block(ex, t, prices.get(t)))]
        total += len(benched)

        print("%-13s %d of %d traded tickers benched" % (name, len(benched), len(tickers)))
        for t, reason in benched:
            print("    %-6s %s" % (t, reason))
    print("\n%d benched across all models." % total)


if __name__ == "__main__":
    main()
