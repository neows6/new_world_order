"""
models/database.py — SQLAlchemy ORM models and schema.

Tables:
  - companies         : ticker metadata and CIK mapping
  - fundamentals      : quarterly fundamental snapshots
  - price_history     : OHLCV daily price data
  - news_items        : raw news with FUD scores
  - trade_signals     : generated trade signals (pre-execution)
  - trade_log         : executed or dry-run trades with full audit trail
"""

from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, Float, String,
    Boolean, DateTime, Text, ForeignKey, Index, UniqueConstraint
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from loguru import logger

Base = declarative_base()


# ─────────────────────────────────────────
# Company master — maps ticker → SEC CIK
# ─────────────────────────────────────────
class Company(Base):
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True)
    ticker = Column(String(10), unique=True, nullable=False, index=True)
    name = Column(String(255))
    cik = Column(String(20), unique=True)          # SEC Central Index Key
    sic_code = Column(String(10))                   # Industry classification
    sector = Column(String(100))
    exchange = Column(String(20))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    fundamentals = relationship("Fundamental", back_populates="company")
    price_history = relationship("PriceHistory", back_populates="company")
    news_items = relationship("NewsItem", back_populates="company")
    trade_signals = relationship("TradeSignal", back_populates="company")

    def __repr__(self):
        return f"<Company {self.ticker} ({self.cik})>"


# ─────────────────────────────────────────
# Fundamentals — one row per quarter
# ─────────────────────────────────────────
class Fundamental(Base):
    __tablename__ = "fundamentals"
    __table_args__ = (
        UniqueConstraint("company_id", "fiscal_year", "fiscal_quarter"),
    )

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    fiscal_year = Column(Integer, nullable=False)
    fiscal_quarter = Column(Integer, nullable=False)   # 0 = annual, 1-4 = quarterly
    period_end = Column(DateTime)
    filed_at = Column(DateTime)

    # Income statement
    revenue = Column(Float)
    gross_profit = Column(Float)
    gross_margin = Column(Float)           # Computed: gross_profit / revenue
    operating_income = Column(Float)
    net_income = Column(Float)
    eps_diluted = Column(Float)

    # Balance sheet
    total_assets = Column(Float)
    total_liabilities = Column(Float)
    total_equity = Column(Float)
    cash_and_equivalents = Column(Float)
    total_debt = Column(Float)
    net_debt = Column(Float)               # Computed: total_debt - cash

    # Cash flow
    operating_cash_flow = Column(Float)
    capex = Column(Float)
    free_cash_flow = Column(Float)         # Computed: OCF - capex
    depreciation_amortization = Column(Float)

    # First principles metrics (computed on ingest)
    owner_earnings = Column(Float)         # net_income + D&A - capex
    roic = Column(Float)                   # NOPAT / invested_capital
    invested_capital = Column(Float)
    nopat = Column(Float)                  # Net operating profit after tax
    net_debt_to_ebitda = Column(Float)
    ebitda = Column(Float)

    # Source tracking
    source = Column(String(50), default="edgar")
    raw_accession = Column(String(50))     # EDGAR accession number for audit

    created_at = Column(DateTime, default=datetime.utcnow)

    company = relationship("Company", back_populates="fundamentals")

    def __repr__(self):
        return f"<Fundamental {self.company_id} FY{self.fiscal_year}Q{self.fiscal_quarter}>"


# ─────────────────────────────────────────
# Price history — daily OHLCV
# ─────────────────────────────────────────
class PriceHistory(Base):
    __tablename__ = "price_history"
    __table_args__ = (
        UniqueConstraint("company_id", "date"),
        Index("ix_price_company_date", "company_id", "date"),
    )

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    date = Column(DateTime, nullable=False)

    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    volume = Column(Float)
    adjusted_close = Column(Float)

    # Derived metrics (computed on ingest)
    market_cap = Column(Float)
    shares_outstanding = Column(Float)
    pe_ratio = Column(Float)
    price_to_fcf = Column(Float)

    source = Column(String(50), default="schwab")
    created_at = Column(DateTime, default=datetime.utcnow)

    company = relationship("Company", back_populates="price_history")


# ─────────────────────────────────────────
# News items — raw articles with FUD scoring
# ─────────────────────────────────────────
class NewsItem(Base):
    __tablename__ = "news_items"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)

    headline = Column(Text)
    summary = Column(Text)
    url = Column(String(2048))
    source = Column(String(100))          # reuters, sec_filing, twitter, etc.
    published_at = Column(DateTime)

    # FUD scoring (populated by Layer 2)
    fud_score = Column(Float)             # 0.0 = pure FUD, 1.0 = quality signal
    sentiment_score = Column(Float)       # -1.0 to 1.0
    contains_financial_data = Column(Boolean, default=False)
    is_sec_filing = Column(Boolean, default=False)
    filing_type = Column(String(20))      # 10-K, 10-Q, 8-K, Form4, etc.

    created_at = Column(DateTime, default=datetime.utcnow)

    company = relationship("Company", back_populates="news_items")


# ─────────────────────────────────────────
# Trade signals — what the engine recommends
# ─────────────────────────────────────────
class TradeSignal(Base):
    __tablename__ = "trade_signals"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)

    signal = Column(String(10))           # BUY, SELL, HOLD
    confidence = Column(Float)            # 0.0 to 1.0
    generated_at = Column(DateTime, default=datetime.utcnow)

    # What drove the signal
    roic = Column(Float)
    wacc_estimate = Column(Float)
    margin_of_safety = Column(Float)
    fud_score = Column(Float)
    intrinsic_value_estimate = Column(Float)
    current_price = Column(Float)

    # Risk
    suggested_position_pct = Column(Float)
    reasoning = Column(Text)              # JSON blob of full reasoning

    acted_on = Column(Boolean, default=False)

    company = relationship("Company", back_populates="trade_signals")


# ─────────────────────────────────────────
# Trade log — full audit trail
# ─────────────────────────────────────────
class TradeLog(Base):
    __tablename__ = "trade_log"

    id = Column(Integer, primary_key=True)
    signal_id = Column(Integer, ForeignKey("trade_signals.id"))
    ticker = Column(String(10), nullable=False)

    action = Column(String(10))           # BUY, SELL
    quantity = Column(Float)
    price_at_execution = Column(Float)
    total_value = Column(Float)

    dry_run = Column(Boolean, default=True)  # True = paper trade, False = live
    schwab_order_id = Column(String(100))    # Populated only on live trades
    status = Column(String(20))              # PENDING, FILLED, REJECTED, CANCELLED

    executed_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    notes = Column(Text)


# ─────────────────────────────────────────
# Database setup
# ─────────────────────────────────────────
def init_db(database_url: str, echo: bool = False):
    """Initialize database, create all tables, return session factory."""
    import os
    # Ensure data directory exists for SQLite
    if database_url.startswith("sqlite"):
        db_path = database_url.replace("sqlite:///", "")
        os.makedirs(os.path.dirname(db_path) if os.path.dirname(db_path) else ".", exist_ok=True)

    engine = create_engine(database_url, echo=echo)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    logger.info(f"Database initialized: {database_url}")
    return engine, Session
