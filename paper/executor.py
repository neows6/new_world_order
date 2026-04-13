"""
paper/executor.py — PaperExecutor: drop-in replacement for SchwabExecutor.

Accepts the same RiskAssessment interface but writes trades to the paper
account DB instead of placing real Schwab orders.

Position sizing: uses assessment.final_shares and assessment.entry_price.
If entry_price is None, falls back to the latest close from the main DB.
"""

from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from sqlalchemy.orm import Session as SASession

from risk.manager import RiskAssessment
from paper.account import init_paper_db, PaperAccount, PaperPosition, PaperTrade

_STAGEGATE_FILE = "data/stagegate.json"


class PaperExecutor:
    """Layer 6 paper-trade executor. Mirrors SchwabExecutor.execute() signature."""

    def __init__(self, main_db_session_factory=None):
        """
        Args:
            main_db_session_factory: optional factory for the main DB — used to
                look up the latest price when assessment.entry_price is None.
        """
        _, self.Session = init_paper_db()
        self.main_Session = main_db_session_factory

    # ── Public interface ──────────────────────────────────────────────────────

    def execute(self, assessment: RiskAssessment, signal_id: Optional[int] = None):
        """
        Paper-trade an approved RiskAssessment.
        Returns a dict summary (not a TradeLog ORM object like SchwabExecutor).
        """
        if not assessment.approved:
            return None

        ticker   = assessment.ticker
        action   = assessment.action       # BUY or SELL
        qty      = assessment.final_shares or 1
        price    = assessment.entry_price or self._latest_price(ticker) or 0.0

        if price <= 0:
            logger.warning(f"[PAPER] No price for {ticker} — skipping")
            return None

        total = round(qty * price, 2)

        with self.Session() as s:
            account  = s.query(PaperAccount).first()
            position = s.query(PaperPosition).filter_by(ticker=ticker).first()

            if action == "BUY":
                if account.cash < total:
                    logger.warning(
                        f"[PAPER] Insufficient cash for {ticker}: "
                        f"need ${total:,.2f}, have ${account.cash:,.2f}"
                    )
                    return None

                account.cash -= total

                sl  = assessment.stop_loss
                tp1 = getattr(assessment, "take_profit_1", None)
                tp2 = getattr(assessment, "take_profit_2", None)

                if position:
                    new_qty           = position.qty + qty
                    position.avg_cost = round(
                        (position.avg_cost * position.qty + total) / new_qty, 4
                    )
                    position.qty      = new_qty
                    position.updated_at = datetime.now(timezone.utc)
                    # Update risk levels — always take the freshest assessment values
                    if sl  is not None: position.stop_loss    = sl
                    if tp1 is not None: position.take_profit_1 = tp1
                    if tp2 is not None: position.take_profit_2 = tp2
                else:
                    s.add(PaperPosition(
                        ticker=ticker, qty=qty, avg_cost=price,
                        stop_loss=sl, take_profit_1=tp1, take_profit_2=tp2,
                    ))

            elif action == "SELL":
                sell_qty = min(qty, position.qty if position else 0)
                if sell_qty <= 0:
                    logger.warning(f"[PAPER] No position to sell for {ticker}")
                    return None

                account.cash += round(sell_qty * price, 2)
                position.qty -= sell_qty
                position.updated_at = datetime.now(timezone.utc)
                if position.qty <= 0.001:
                    s.delete(position)
                qty = sell_qty   # record actual qty sold

            else:
                return None   # HOLD / WAIT — nothing to do

            notes = "\n".join(
                (assessment.adjustment_notes or []) + (assessment.risk_warnings or [])
            )

            trade = PaperTrade(
                ticker     = ticker,
                action     = action,
                qty        = qty,
                price      = price,
                total      = total,
                cash_after = round(account.cash, 2),
                signal     = getattr(assessment, "signal", None),
                stop_loss  = assessment.stop_loss,
                notes      = notes[:500] if notes else None,
                timestamp  = datetime.now(timezone.utc),
            )
            s.add(trade)
            s.commit()
            self._update_stagegate(ticker, action)

            logger.info(
                f"[PAPER] {action} {qty} × {ticker} @ ${price:.2f} = ${total:,.2f} "
                f"| cash remaining: ${account.cash:,.2f}"
            )
            return {"ticker": ticker, "action": action, "qty": qty, "price": price, "total": total}

    def get_account_summary(self, price_lookup: Optional[dict] = None) -> dict:
        """
        Return a dict with cash, positions (with current prices), and P&L.
        price_lookup: {ticker: current_price} — if None, uses latest DB close.
        """
        with self.Session() as s:
            account   = s.query(PaperAccount).first()
            positions = s.query(PaperPosition).all()

            pos_list      = []
            total_mkt_val = 0.0

            for p in positions:
                if p.qty <= 0:
                    continue
                cur_price = (price_lookup or {}).get(p.ticker) or self._latest_price(p.ticker) or p.avg_cost
                mkt_val   = round(p.qty * cur_price, 2)
                cost_basis = round(p.qty * p.avg_cost, 2)
                pnl       = round(mkt_val - cost_basis, 2)
                pnl_pct   = round((pnl / cost_basis * 100) if cost_basis else 0, 2)
                total_mkt_val += mkt_val

                pos_list.append({
                    "ticker":     p.ticker,
                    "qty":        p.qty,
                    "avg_cost":   round(p.avg_cost, 2),
                    "cur_price":  round(cur_price, 2),
                    "mkt_val":    mkt_val,
                    "cost_basis": cost_basis,
                    "pnl":        pnl,
                    "pnl_pct":    pnl_pct,
                })

            pos_list.sort(key=lambda x: x["pnl"], reverse=True)

            cash          = round(account.cash, 2)
            total_equity  = round(cash + total_mkt_val, 2)
            start_bal     = round(account.starting_balance, 2)
            total_pnl     = round(total_equity - start_bal, 2)
            total_pnl_pct = round((total_pnl / start_bal * 100) if start_bal else 0, 2)

            return {
                "cash":           cash,
                "positions_value": round(total_mkt_val, 2),
                "total_equity":   total_equity,
                "starting_balance": start_bal,
                "total_pnl":      total_pnl,
                "total_pnl_pct":  total_pnl_pct,
                "positions":      pos_list,
                "created_at":     account.created_at.isoformat() if account.created_at else None,
            }

    def get_recent_trades(self, limit: int = 50) -> list:
        with self.Session() as s:
            trades = (
                s.query(PaperTrade)
                .order_by(PaperTrade.timestamp.desc())
                .limit(limit)
                .all()
            )
            return [
                {
                    "ticker":    t.ticker,
                    "action":    t.action,
                    "qty":       t.qty,
                    "price":     t.price,
                    "total":     t.total,
                    "cash_after": t.cash_after,
                    "timestamp": t.timestamp.isoformat() if t.timestamp else None,
                }
                for t in trades
            ]

    def reset_account(self):
        """Wipe all trades and positions, restore starting balance."""
        with self.Session() as s:
            s.query(PaperTrade).delete()
            s.query(PaperPosition).delete()
            account = s.query(PaperAccount).first()
            account.cash     = account.starting_balance
            account.reset_at = datetime.now(timezone.utc)
            s.commit()
        logger.info("[PAPER] Account reset to starting balance.")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _update_stagegate(self, ticker: str, action: str):
        """Sync stagegate.json after a trade: BUY → stage3, SELL → stage2."""
        import json, os
        try:
            if not os.path.exists(_STAGEGATE_FILE):
                return
            with open(_STAGEGATE_FILE, encoding="utf-8") as f:
                sg = json.load(f)
            for k in ("stage1", "stage2", "stage3"):
                sg.setdefault(k, [])
            if action == "BUY":
                for k in ("stage1", "stage2"):
                    if ticker in sg[k]:
                        sg[k].remove(ticker)
                if ticker not in sg["stage3"]:
                    sg["stage3"].append(ticker)
            elif action == "SELL":
                if ticker in sg["stage3"]:
                    sg["stage3"].remove(ticker)
                # Return to stage2 (still AI-watched) if not already placed
                if ticker not in sg["stage1"] and ticker not in sg["stage2"]:
                    sg["stage2"].append(ticker)
            with open(_STAGEGATE_FILE, "w", encoding="utf-8") as f:
                json.dump(sg, f, indent=2)
        except Exception as e:
            logger.warning(f"[PAPER] stagegate sync failed: {e}")

    def _latest_price(self, ticker: str) -> Optional[float]:
        if not self.main_Session:
            return None
        try:
            from models.database import Company, PriceHistory
            with self.main_Session() as s:
                company = s.query(Company).filter_by(ticker=ticker).first()
                if not company:
                    return None
                row = (
                    s.query(PriceHistory)
                    .filter_by(company_id=company.id)
                    .order_by(PriceHistory.date.desc())
                    .first()
                )
                if not row:
                    return None
                return float(row.adjusted_close or row.close or 0)
        except Exception:
            return None
