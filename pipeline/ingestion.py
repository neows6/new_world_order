"""
pipeline/ingestion.py — Layer 1: Data Ingestion Orchestrator.

Coordinates EDGAR fundamentals + Schwab price data into the database.
Designed to run on a schedule (APScheduler) or be called manually.

Usage:
    python -m pipeline.ingestion              # Run full ingestion now
    python -m pipeline.ingestion --ticker AAPL  # Single ticker
"""

import argparse
from datetime import datetime
from typing import Optional

from loguru import logger
from sqlalchemy.orm import Session

from config import config
from models.database import init_db, Company, Fundamental, PriceHistory
from edgar.client import EdgarClient
from broker.market_data import SchwabMarketData


class IngestionPipeline:
    """
    Layer 1: Pulls fundamentals from EDGAR and prices from Schwab,
    normalizes everything, and writes to the local database.
    """

    def __init__(self, db_session_factory):
        self.Session = db_session_factory
        self.edgar = EdgarClient()
        self.schwab = SchwabMarketData()

    # ─────────────────────────────────────────
    # Company resolution
    # ─────────────────────────────────────────

    def _get_or_create_company(self, session: Session, ticker: str, entity_name: str, cik: str) -> Company:
        """Get existing company record or create it."""
        company = session.query(Company).filter_by(ticker=ticker).first()

        if not company:
            company = Company(
                ticker=ticker,
                name=entity_name,
                cik=cik,
            )
            session.add(company)
            session.flush()  # Get the ID
            logger.info(f"Created company record: {ticker} (CIK: {cik})")

        elif company.name != entity_name or company.cik != cik:
            company.name = entity_name
            company.cik = cik
            company.updated_at = datetime.utcnow()

        return company

    # ─────────────────────────────────────────
    # Fundamentals ingestion
    # ─────────────────────────────────────────

    def ingest_fundamentals(self, ticker: str) -> bool:
        """
        Fetch EDGAR fundamentals for a ticker and upsert into database.
        Returns True on success.
        """
        logger.info(f"[FUNDAMENTALS] Ingesting {ticker}...")

        data = self.edgar.get_fundamentals(ticker)
        if not data:
            logger.warning(f"No EDGAR fundamentals returned for {ticker}")
            return False

        with self.Session() as session:
            company = self._get_or_create_company(
                session,
                ticker=data["ticker"],
                entity_name=data["entity_name"],
                cik=data["cik"],
            )

            records_written = 0
            for annual in data["annual"]:
                # Upsert: update if exists, insert if not
                existing = session.query(Fundamental).filter_by(
                    company_id=company.id,
                    fiscal_year=annual["fiscal_year"],
                    fiscal_quarter=0,
                ).first()

                if existing:
                    # Update all numeric fields
                    for field in [
                        "revenue", "gross_profit", "gross_margin", "operating_income",
                        "net_income", "eps_diluted", "total_assets", "total_liabilities",
                        "total_equity", "cash_and_equivalents", "total_debt", "net_debt",
                        "operating_cash_flow", "capex", "free_cash_flow",
                        "depreciation_amortization", "owner_earnings", "roic",
                        "invested_capital", "nopat", "net_debt_to_ebitda", "ebitda",
                    ]:
                        val = annual.get(field)
                        if val is not None:
                            setattr(existing, field, val)
                else:
                    fund = Fundamental(
                        company_id=company.id,
                        fiscal_year=annual["fiscal_year"],
                        fiscal_quarter=0,
                        revenue=annual.get("revenue"),
                        gross_profit=annual.get("gross_profit"),
                        gross_margin=annual.get("gross_margin"),
                        operating_income=annual.get("operating_income"),
                        net_income=annual.get("net_income"),
                        eps_diluted=annual.get("eps_diluted"),
                        total_assets=annual.get("total_assets"),
                        total_liabilities=annual.get("total_liabilities"),
                        total_equity=annual.get("total_equity"),
                        cash_and_equivalents=annual.get("cash_and_equivalents"),
                        total_debt=annual.get("total_debt"),
                        net_debt=annual.get("net_debt"),
                        operating_cash_flow=annual.get("operating_cash_flow"),
                        capex=annual.get("capex"),
                        free_cash_flow=annual.get("free_cash_flow"),
                        depreciation_amortization=annual.get("depreciation_amortization"),
                        owner_earnings=annual.get("owner_earnings"),
                        roic=annual.get("roic"),
                        invested_capital=annual.get("invested_capital"),
                        nopat=annual.get("nopat"),
                        net_debt_to_ebitda=annual.get("net_debt_to_ebitda"),
                        ebitda=annual.get("ebitda"),
                        source="edgar",
                    )
                    session.add(fund)
                    records_written += 1

            session.commit()
            logger.info(f"[FUNDAMENTALS] {ticker}: {records_written} new records, "
                        f"{len(data['annual']) - records_written} updated")

        return True

    # ─────────────────────────────────────────
    # Price history ingestion
    # ─────────────────────────────────────────

    def ingest_price_history(self, ticker: str, days: int = 365) -> bool:
        """
        Fetch Schwab price history and upsert into database.
        """
        logger.info(f"[PRICES] Ingesting {ticker} ({days} days)...")

        candles = self.schwab.get_price_history(ticker, days=days)
        if not candles:
            logger.warning(f"No price data returned for {ticker}")
            return False

        # Fetch current quote for market cap / shares
        quote = self.schwab.get_quote(ticker)

        with self.Session() as session:
            company = session.query(Company).filter_by(ticker=ticker).first()
            if not company:
                company = Company(ticker=ticker, name=ticker)
                session.add(company)
                session.flush()

            records_written = 0
            for c in candles:
                existing = session.query(PriceHistory).filter_by(
                    company_id=company.id,
                    date=c["date"],
                ).first()

                if existing:
                    continue  # Don't overwrite historical data

                price_record = PriceHistory(
                    company_id=company.id,
                    date=c["date"],
                    open=c.get("open"),
                    high=c.get("high"),
                    low=c.get("low"),
                    close=c.get("close"),
                    volume=c.get("volume"),
                    market_cap=quote.get("market_cap") if quote else None,
                    shares_outstanding=quote.get("shares_outstanding") if quote else None,
                    source="schwab",
                )
                session.add(price_record)
                records_written += 1

            session.commit()
            logger.info(f"[PRICES] {ticker}: {records_written} new candles written")

        return True

    # ─────────────────────────────────────────
    # Full pipeline run
    # ─────────────────────────────────────────

    def run_full_ingest(self, tickers: Optional[list] = None):
        """
        Run complete ingestion for all tickers in watchlist (or subset).
        """
        tickers = tickers or config.watchlist
        logger.info(f"Starting full ingestion for {len(tickers)} tickers: {tickers}")

        results = {"success": [], "failed": []}

        for ticker in tickers:
            try:
                ok_fundamentals = self.ingest_fundamentals(ticker)
                ok_prices = self.ingest_price_history(ticker)

                if ok_fundamentals or ok_prices:
                    results["success"].append(ticker)
                else:
                    results["failed"].append(ticker)

            except Exception as e:
                logger.error(f"Ingestion failed for {ticker}: {e}")
                results["failed"].append(ticker)

        logger.info(
            f"Ingestion complete. "
            f"Success: {results['success']} | "
            f"Failed: {results['failed']}"
        )
        return results

    def run_prices_only(self, tickers: Optional[list] = None):
        """
        Lightweight refresh — prices only, skip EDGAR (good for intraday runs).
        """
        tickers = tickers or config.watchlist
        for ticker in tickers:
            try:
                self.ingest_price_history(ticker, days=7)
            except Exception as e:
                logger.error(f"Price refresh failed for {ticker}: {e}")


# ─────────────────────────────────────────
# Scheduler setup (call from main.py)
# ─────────────────────────────────────────

def setup_scheduler(pipeline: IngestionPipeline, analysis_pipeline=None):
    """
    Configure APScheduler for automated ingestion.
    - Fundamentals: once per day at 6am ET
    - Prices: every 5 minutes Mon-Fri during market hours
    - AI Watch: every 1 minute for priority tickers (offset by 30s)

    analysis_pipeline: optional AnalysisPipeline — if provided, analysis runs
                       after each price refresh (layers 2-5).
    """
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = BackgroundScheduler()

    # Daily fundamentals refresh at 6am ET
    scheduler.add_job(
        func=pipeline.run_full_ingest,
        trigger=CronTrigger(hour=6, minute=0),
        id="fundamentals_daily",
        name="Daily EDGAR fundamentals refresh",
        replace_existing=True,
    )

    # Price refresh every 5 minutes, Mon-Fri market hours
    scheduler.add_job(
        func=pipeline.run_prices_only,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour="9-16",
            minute="*/5",
        ),
        id="prices_intraday",
        name="Intraday price refresh",
        replace_existing=True,
    )

    # AI Watch: 1-min price refresh for priority tickers
    # Offset by 30s so it doesn't collide with the 5-min job
    def _ai_watch_prices():
        ai_tickers = config.ai_watch_tickers
        if ai_tickers:
            pipeline.run_prices_only(ai_tickers)

    scheduler.add_job(
        func=_ai_watch_prices,
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour="9-16",
            minute="*",
            second="30",
        ),
        id="ai_watch_prices_1min",
        name="1-min AI Watch price refresh (priority tickers)",
        replace_existing=True,
    )

    return scheduler


# ─────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from loguru import logger
    logger.add(config.log_file, rotation="10 MB", retention="30 days", level=config.log_level)

    parser = argparse.ArgumentParser(description="Layer 1: Data Ingestion Pipeline")
    parser.add_argument("--ticker", type=str, help="Single ticker to ingest (default: all watchlist)")
    parser.add_argument("--prices-only", action="store_true", help="Skip EDGAR, refresh prices only")
    parser.add_argument("--days", type=int, default=365, help="Days of price history to fetch")
    args = parser.parse_args()

    engine, Session = init_db(config.database.url, echo=config.database.echo_sql)
    pipeline = IngestionPipeline(db_session_factory=Session)

    tickers = [args.ticker.upper()] if args.ticker else None

    if args.prices_only:
        pipeline.run_prices_only(tickers)
    else:
        pipeline.run_full_ingest(tickers)
