"""
paper/stop_monitor.py — Intra-cycle stop-loss and take-profit monitor.

Runs every 60 seconds during market hours (independent of the 5-min AI cycle).
Fetches live Schwab quotes for all open positions and fires a paper SELL
immediately when price hits a stop-loss or take-profit level.

This means you get exit discipline at 60-second resolution rather than
waiting up to 5 minutes for the next full AI cycle.
"""

from datetime import datetime, timezone
from loguru import logger

from paper.account import PaperAccount, PaperPosition, PaperTrade

_STAGEGATE_FILE = "data/stagegate.json"


def check_stops(paper_session_factory, market_data) -> int:
    """
    Check all open positions against their stop_loss / take_profit_1 levels.
    Executes a paper SELL for any position that has breached a level.
    Returns the number of exits triggered.
    """
    try:
        with paper_session_factory() as s:
            positions = (
                s.query(PaperPosition)
                .filter(PaperPosition.qty > 0)
                .all()
            )
            # Detach from session before closing
            pos_data = [
                {
                    "ticker":       p.ticker,
                    "qty":          p.qty,
                    "avg_cost":     p.avg_cost,
                    "stop_loss":    p.stop_loss,
                    "take_profit_1": p.take_profit_1,
                }
                for p in positions
                if p.stop_loss is not None or p.take_profit_1 is not None
            ]
    except Exception as e:
        logger.warning(f"[STOP MON] Could not read positions: {e}")
        return 0

    if not pos_data:
        return 0

    # Batch-fetch live prices
    tickers = [p["ticker"] for p in pos_data]
    try:
        quotes = market_data.get_quotes_batch(tickers)
    except Exception as e:
        logger.warning(f"[STOP MON] Quote fetch failed: {e}")
        return 0

    exits = 0
    for pos in pos_data:
        ticker = pos["ticker"]
        q = quotes.get(ticker)
        if not q:
            continue
        price = q.get("last_price") or 0
        if not price:
            continue

        sl  = pos["stop_loss"]
        tp1 = pos["take_profit_1"]

        reason = None
        if sl  is not None and price <= sl:
            reason = f"STOP @ ${price:.2f} ≤ stop ${sl:.2f}"
        elif tp1 is not None and price >= tp1:
            reason = f"TARGET @ ${price:.2f} ≥ tp1 ${tp1:.2f}"

        if reason:
            logger.info(f"[STOP MON] {ticker}: {reason} — triggering paper SELL")
            ok = _execute_stop_sell(paper_session_factory, ticker, price, reason)
            if ok:
                exits += 1

    return exits


def _execute_stop_sell(paper_session_factory, ticker: str, price: float, reason: str) -> bool:
    """Write a paper SELL directly to the DB for a stop/target exit."""
    try:
        with paper_session_factory() as s:
            account  = s.query(PaperAccount).first()
            position = s.query(PaperPosition).filter_by(ticker=ticker).first()

            if not position or position.qty <= 0.001:
                return False

            sell_qty = position.qty
            proceeds = round(sell_qty * price, 2)
            account.cash = round(account.cash + proceeds, 2)

            s.add(PaperTrade(
                ticker     = ticker,
                action     = "SELL",
                qty        = sell_qty,
                price      = price,
                total      = proceeds,
                cash_after = account.cash,
                signal     = "STOP_SELL",
                stop_loss  = position.stop_loss,
                notes      = reason[:500],
                timestamp  = datetime.now(timezone.utc),
            ))

            s.delete(position)
            s.commit()

        _sync_stagegate(ticker)
        logger.info(
            f"[STOP MON] SOLD {sell_qty}× {ticker} @ ${price:.2f} "
            f"= ${proceeds:,.2f} | {reason}"
        )
        return True

    except Exception as e:
        logger.error(f"[STOP MON] Stop-sell failed for {ticker}: {e}")
        return False


def _sync_stagegate(ticker: str):
    """Move ticker from stage3 back to stage2 after a stop exit."""
    import json, os
    try:
        if not os.path.exists(_STAGEGATE_FILE):
            return
        with open(_STAGEGATE_FILE, encoding="utf-8") as f:
            sg = json.load(f)
        for k in ("stage1", "stage2", "stage3"):
            sg.setdefault(k, [])
        if ticker in sg["stage3"]:
            sg["stage3"].remove(ticker)
        if ticker not in sg["stage1"] and ticker not in sg["stage2"]:
            sg["stage2"].append(ticker)
        with open(_STAGEGATE_FILE, "w", encoding="utf-8") as f:
            json.dump(sg, f, indent=2)
    except Exception as e:
        logger.warning(f"[STOP MON] stagegate sync failed: {e}")
