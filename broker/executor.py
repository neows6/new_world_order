"""
schwab/executor.py — Layer 6: Schwab API Order Executor.

This is the final layer — it places real orders (or logs them in dry_run mode).
Only called after the risk manager has approved a trade.

IMPORTANT: dry_run=True is the default and must be explicitly disabled in config.
Every live trade is logged to the trade_log table with full audit trail.

Usage:
    Called by the orchestrator — not meant for direct CLI use.
    To test: python -m schwab.executor --ticker AAPL --signal BUY --quantity 5 --price 175.00
"""

from datetime import datetime
from typing import Optional

from loguru import logger

from config import config
from models.database import init_db, TradeLog, TradeSignal, Company
from risk.manager import RiskAssessment
from monitor.telegram_bot import send_alert


class SchwabExecutor:
    """
    Layer 6: Places orders via schwab-py or logs them as paper trades.
    Always writes to trade_log for full audit trail regardless of dry_run.
    """

    def __init__(self, db_session_factory):
        self.Session = db_session_factory
        self._schwab_client = None

    def _get_client(self):
        """Lazy-load Schwab client — same pattern as market_data.py."""
        if self._schwab_client is not None:
            return self._schwab_client

        from pathlib import Path
        import schwab

        token_path = Path(config.schwab.token_path)
        if not token_path.exists():
            raise FileNotFoundError(
                f"Schwab token not found at {token_path}. Run OAuth flow first."
            )

        self._schwab_client = schwab.auth.client_from_token_file(
            token_path=str(token_path),
            api_key=config.schwab.api_key,
            app_secret=config.schwab.api_secret,
        )
        return self._schwab_client

    def _place_market_order(self, ticker: str, quantity: int, action: str) -> dict:
        """
        Place a market order via Schwab API.
        Returns dict with order_id and status.
        action: "BUY" or "SELL"
        """
        import schwab
        from schwab.orders.equities import equity_buy_market, equity_sell_market

        client = self._get_client()

        try:
            if action == "BUY":
                order = equity_buy_market(ticker, quantity)
            else:
                order = equity_sell_market(ticker, quantity)

            resp = client.place_order(config.schwab.account_hash, order)
            resp.raise_for_status()

            # Schwab returns order ID in Location header
            order_id = resp.headers.get("Location", "").split("/")[-1]
            logger.info(f"Order placed: {action} {quantity} {ticker} — order_id={order_id}")

            return {"order_id": order_id, "status": "PENDING", "error": None}

        except Exception as e:
            logger.error(f"Order placement failed for {ticker}: {e}")
            return {"order_id": None, "status": "REJECTED", "error": str(e)}

    def execute(
        self,
        assessment: RiskAssessment,
        signal_id: Optional[int] = None,
    ) -> Optional[TradeLog]:
        """
        Execute (or paper-trade) an L5 RiskAssessment.

        Args:
            assessment: Approved RiskAssessment from risk manager
            signal_id: FK to trade_signals table (for audit trail)

        Returns:
            TradeLog record (persisted to DB)
        """
        if not assessment.approved:
            logger.warning(f"Executor called with non-approved assessment for {assessment.ticker}")
            return None

        ticker      = assessment.ticker
        quantity    = assessment.final_shares
        price       = assessment.entry_price or 0.0
        action      = assessment.action
        total_value = quantity * price
        notes_str   = "\n".join(assessment.adjustment_notes + assessment.risk_warnings)

        # ── Dry run (paper trade) ─────────────────────────────
        if config.risk.dry_run:
            logger.info(
                f"[DRY RUN] {action} {quantity} × {ticker} @ ${price:.2f} "
                f"= ${total_value:,.2f} | stop=${f'{assessment.stop_loss:.2f}' if assessment.stop_loss else 'N/A'}"
            )
            trade = self._log_trade(
                ticker=ticker,
                action=action,
                quantity=quantity,
                price=price,
                total_value=total_value,
                dry_run=True,
                order_id=None,
                status="DRY_RUN",
                signal_id=signal_id,
                notes=notes_str,
            )
            return trade

        # ── Live trade ────────────────────────────────────────
        logger.warning(
            f"LIVE TRADE: {action} {quantity} × {ticker} @ ~${price:.2f} "
            f"(≈${total_value:,.2f}) | stop=${f'{assessment.stop_loss:.2f}' if assessment.stop_loss else 'N/A'}"
        )

        result = self._place_market_order(ticker, quantity, action)

        trade = self._log_trade(
            ticker=ticker,
            action=action,
            quantity=quantity,
            price=price,
            total_value=total_value,
            dry_run=False,
            order_id=result.get("order_id"),
            status=result.get("status", "UNKNOWN"),
            signal_id=signal_id,
            notes=notes_str + ("\n" + result["error"] if result.get("error") else ""),
        )

        return trade

    def _log_trade(
        self,
        ticker: str,
        action: str,
        quantity: int,
        price: float,
        total_value: float,
        dry_run: bool,
        order_id: Optional[str],
        status: str,
        signal_id: Optional[int],
        notes: str = "",
    ) -> TradeLog:
        """Persist trade to trade_log table."""
        with self.Session() as session:
            trade = TradeLog(
                signal_id=signal_id,
                ticker=ticker,
                action=action,
                quantity=quantity,
                price_at_execution=price,
                total_value=total_value,
                dry_run=dry_run,
                schwab_order_id=order_id,
                status=status,
                executed_at=datetime.utcnow(),
                notes=notes,
            )
            session.add(trade)

            # Mark signal as acted on
            if signal_id:
                sig = session.query(TradeSignal).filter_by(id=signal_id).first()
                if sig:
                    sig.acted_on = True

            session.commit()
            session.refresh(trade)

            mode = "DRY RUN" if dry_run else "LIVE"
            logger.info(
                f"[TRADE LOG] [{mode}] {action} {quantity} {ticker} "
                f"@ ${price:.2f} | status={status} | id={trade.id}"
            )
            emoji = "📋" if dry_run else "🔴"
            send_alert(
                f"{emoji} <b>[{mode}]</b> {action} {quantity}× {ticker} "
                f"@ ${price:.2f} = ${total_value:,.2f}\n"
                f"Status: {status}"
            )
            return trade


# ── CLI (for manual testing only) ────────────────────────────

if __name__ == "__main__":
    import argparse, sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    parser = argparse.ArgumentParser(description="Layer 6: Executor (test mode)")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--signal", default="BUY", choices=["BUY", "SELL"])
    parser.add_argument("--quantity", type=int, required=True)
    parser.add_argument("--price", type=float, required=True)
    args = parser.parse_args()

    if not config.risk.dry_run:
        print("WARNING: dry_run is OFF. This will place a real order.")
        confirm = input("Type 'CONFIRM' to proceed: ")
        if confirm != "CONFIRM":
            print("Aborted.")
            sys.exit(0)

    mock_decision = RiskAssessment(
        approved=True,
        ticker=args.ticker.upper(),
        signal=args.signal,
        requested_value=args.quantity * args.price,
        approved_value=args.quantity * args.price,
        approved_quantity=args.quantity,
        price=args.price,
        rejection_reason=None,
        notes=["Manual test execution"],
    )

    _, Session = init_db(config.database.url)
    executor = SchwabExecutor(db_session_factory=Session)
    trade = executor.execute(mock_decision)

    if trade:
        print(f"\nTrade logged: id={trade.id}, status={trade.status}")
