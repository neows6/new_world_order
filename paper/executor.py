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

try:
    from monitor.telegram_bot import send_alert as _tg
except Exception:
    def _tg(msg): return False

_STAGEGATE_FILE = "data/stagegate.json"

# Model threshold multipliers (applied on top of standard thresholds)
PAPER_MODEL_CONFIGS = {
    "standard":     {"multiplier": 1.00, "db": "data/paper_trading.db",           "stagegate": "data/stagegate.json"},
    "relaxed":      {"multiplier": 0.75, "db": "data/paper_relaxed.db",           "stagegate": "data/stagegate_relaxed.json"},
    "very_relaxed": {"multiplier": 0.50, "db": "data/paper_very_relaxed.db",      "stagegate": "data/stagegate_very_relaxed.json"},
    "claude":       {"multiplier": 0.85, "db": "data/paper_claude.db",            "stagegate": "data/stagegate_claude.json"},
}


class PaperExecutor:
    """Layer 6 paper-trade executor. Mirrors SchwabExecutor.execute() signature."""

    def __init__(self, main_db_session_factory=None, db_path: str = None,
                 stagegate_file: str = None):
        """
        Args:
            main_db_session_factory: optional factory for the main DB — used to
                look up the latest price when assessment.entry_price is None.
            db_path: SQLite file path (defaults to data/paper_trading.db).
            stagegate_file: JSON file path for stage gate state (defaults to data/stagegate.json).
        """
        _, self.Session = init_paper_db(db_path)
        self.main_Session    = main_db_session_factory
        self._stagegate_file = stagegate_file or _STAGEGATE_FILE

    def _model_label(self) -> str:
        sf = str(self._stagegate_file)
        if "claude"       in sf: return "Claude"
        if "very_relaxed" in sf: return "Very Relaxed"
        if "relaxed"      in sf: return "Relaxed"
        return "Standard"

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
                if position and position.qty > 0:
                    logger.info(f"[PAPER] {ticker} already held ({position.qty} shares) — skipping duplicate BUY")
                    return None
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
            _model = self._model_label()
            _emoji = "🟢" if action == "BUY" else "🔴"
            _tg(
                f"{_emoji} <b>PAPER {action}</b> [{_model}]\n"
                f"<b>{ticker}</b>  {qty} shares @ ${price:.2f}\n"
                f"Total: ${total:,.2f}  |  Cash left: ${account.cash:,.2f}"
            )
            return {"ticker": ticker, "action": action, "qty": qty, "price": price, "total": total}

    def get_account_summary(self, price_lookup: Optional[dict] = None) -> dict:
        """
        Return a dict with cash, positions (with current prices), and P&L.
        price_lookup: {ticker: current_price} — if None, batch-fetches live Schwab quotes.
        """
        with self.Session() as s:
            account   = s.query(PaperAccount).first()
            positions = s.query(PaperPosition).all()

            # Batch-fetch live prices for all open positions if not provided
            if price_lookup is None:
                open_tickers = [p.ticker for p in positions if p.qty > 0]
                if open_tickers:
                    try:
                        from broker.market_data import SchwabMarketData
                        quotes = SchwabMarketData().get_quotes_batch(open_tickers)
                        price_lookup = {
                            t: round(float(q["last_price"]), 2)
                            for t, q in quotes.items()
                            if q.get("last_price")
                        }
                    except Exception:
                        price_lookup = {}

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
            total_cost    = sum(p["cost_basis"] for p in pos_list)
            total_pnl     = round(total_mkt_val - total_cost, 2)
            total_pnl_pct = round((total_pnl / total_cost * 100) if total_cost else 0, 2)

            # Daily P&L: current equity minus prior-day snapshot (or estimated from trade history)
            daily_pnl = 0.0
            daily_pnl_pct = 0.0
            try:
                from paper.account import PaperEquitySnapshot
                from datetime import date as _date, datetime as _dt
                today = _date.today()
                snap = (
                    s.query(PaperEquitySnapshot)
                    .filter(PaperEquitySnapshot.snap_date < today)
                    .order_by(PaperEquitySnapshot.snap_date.desc())
                    .first()
                )
                if snap:
                    daily_pnl = round(total_equity - snap.total_equity, 2)
                    daily_pnl_pct = round((daily_pnl / snap.total_equity * 100) if snap.total_equity else 0, 2)
                else:
                    # No prior snapshot — only reconstruct if trades happened today.
                    # Without today's trades the formula degenerates to (mkt_val - cost_basis)
                    # which is identical to total_pnl, making the card misleading.
                    today_start = _dt.combine(today, _dt.min.time())
                    today_buy_total = sum(
                        t.total for t in s.query(PaperTrade)
                        .filter(PaperTrade.action == "BUY",
                                PaperTrade.timestamp >= today_start)
                        .all()
                    )
                    if today_buy_total > 0:
                        last_prior = (
                            s.query(PaperTrade)
                            .filter(PaperTrade.timestamp < today_start,
                                    PaperTrade.cash_after.isnot(None))
                            .order_by(PaperTrade.timestamp.desc())
                            .first()
                        )
                        if last_prior:
                            start_cash = last_prior.cash_after
                            start_pos_value = max(0.0, total_cost - today_buy_total)
                            start_equity = start_cash + start_pos_value
                            if start_equity > 0:
                                daily_pnl = round(total_equity - start_equity, 2)
                                daily_pnl_pct = round(daily_pnl / start_equity * 100, 2)

                # Always save today's snapshot (idempotent) so tomorrow has a baseline
                try:
                    if not s.query(PaperEquitySnapshot).filter_by(snap_date=today).first():
                        s.add(PaperEquitySnapshot(
                            snap_date=today,
                            total_equity=total_equity,
                            cash=cash,
                            positions_value=total_mkt_val,
                        ))
                        s.commit()
                except Exception:
                    pass
            except Exception:
                pass

            return {
                "cash":             cash,
                "positions_value":  round(total_mkt_val, 2),
                "total_invested":   round(total_cost, 2),
                "total_equity":     total_equity,
                "starting_balance": start_bal,
                "total_pnl":        total_pnl,
                "total_pnl_pct":    total_pnl_pct,
                "lifetime_pnl":     round(total_equity - start_bal, 2),
                "lifetime_pnl_pct": round((total_equity - start_bal) / start_bal * 100 if start_bal else 0, 2),
                "daily_pnl":        daily_pnl,
                "daily_pnl_pct":    daily_pnl_pct,
                "positions":        pos_list,
                "created_at":       account.created_at.isoformat() if account.created_at else None,
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
                    "signal":    t.signal,
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
        """Sync stagegate file after a trade: BUY → stage3, SELL → stage2."""
        import json, os
        try:
            sg_file = self._stagegate_file
            if not os.path.exists(sg_file):
                return
            with open(sg_file, encoding="utf-8") as f:
                sg = json.load(f)
            for k in ("stage1", "stage2", "stage3"):
                sg.setdefault(k, [])
            if action == "BUY":
                # Always remove from stage1
                if ticker in sg["stage1"]:
                    sg["stage1"].remove(ticker)
                # First buy: move from stage2 → stage3
                # Pyramid buy (already in stage3 from a split): keep stage2 presence as-is
                if ticker not in sg["stage3"]:
                    if ticker in sg["stage2"]:
                        sg["stage2"].remove(ticker)
                    sg["stage3"].append(ticker)
                # else: pyramid — stage2 presence unchanged
            elif action == "SELL":
                if ticker in sg["stage3"]:
                    sg["stage3"].remove(ticker)
                # Return to stage2 (still AI-watched) if not already placed
                if ticker not in sg["stage1"] and ticker not in sg["stage2"]:
                    sg["stage2"].append(ticker)
            with open(sg_file, "w", encoding="utf-8") as f:
                json.dump(sg, f, indent=2)

            # Auto-enable AI exit toggle on AI BUY so the AI monitors for exits
            if action == "BUY":
                sg_path = str(sg_file)
                if "very_relaxed" in sg_path:
                    ai_exits_path = "data/ai_exits_very_relaxed.json"
                elif "relaxed" in sg_path:
                    ai_exits_path = "data/ai_exits_relaxed.json"
                elif "claude" in sg_path:
                    ai_exits_path = "data/ai_exits_claude.json"
                else:
                    ai_exits_path = "data/ai_exits.json"
                try:
                    exits = json.loads(open(ai_exits_path, encoding="utf-8").read()) if os.path.exists(ai_exits_path) else {}
                    exits[ticker] = True
                    with open(ai_exits_path, "w", encoding="utf-8") as f:
                        json.dump(exits, f, indent=2)
                except Exception as e2:
                    logger.warning(f"[PAPER] ai_exits auto-enable failed for {ticker}: {e2}")

        except Exception as e:
            logger.warning(f"[PAPER] stagegate sync failed: {e}")

    def execute_sell(self, ticker: str, price: float, reason: str = "AI_SELL") -> bool:
        """Fire a paper SELL for all shares of ticker at the given price (AI-driven exit)."""
        try:
            with self.Session() as s:
                account  = s.query(PaperAccount).first()
                position = s.query(PaperPosition).filter_by(ticker=ticker).first()
                if not position or position.qty <= 0.001:
                    return False
                qty      = position.qty
                proceeds = round(qty * price, 2)
                account.cash = round(account.cash + proceeds, 2)
                s.add(PaperTrade(
                    ticker     = ticker,
                    action     = "SELL",
                    qty        = qty,
                    price      = price,
                    total      = proceeds,
                    cash_after = account.cash,
                    signal     = "AI_SELL",
                    notes      = reason[:500],
                    timestamp  = datetime.now(timezone.utc),
                ))
                s.delete(position)
                s.commit()
            self._update_stagegate(ticker, "SELL")
            logger.info(
                f"[AI EXIT] SOLD {qty}× {ticker} @ ${price:.2f} = ${proceeds:,.2f} | {reason}"
            )
            _model = self._model_label()
            _tg(
                f"🔴 <b>PAPER SELL (AI Exit)</b> [{_model}]\n"
                f"<b>{ticker}</b>  {qty:.2f} shares @ ${price:.2f}\n"
                f"Proceeds: ${proceeds:,.2f}  |  Reason: {reason}"
            )
            return True
        except Exception as e:
            logger.error(f"[AI EXIT] execute_sell failed for {ticker}: {e}")
            return False

    def _latest_price(self, ticker: str) -> Optional[float]:
        # Try live Schwab quote first
        try:
            from broker.market_data import SchwabMarketData
            q = SchwabMarketData().get_quotes_batch([ticker]).get(ticker, {})
            lp = q.get("last_price")
            if lp:
                return round(float(lp), 2)
        except Exception:
            pass
        # EOD fallback
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
