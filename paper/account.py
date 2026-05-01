"""
paper/account.py — Paper trading account model.

Persists to data/paper_trading.db (separate from the main trading DB).
Tracks cash, positions, trade history, and daily equity snapshots for P&L charts.
"""

from datetime import datetime, timezone, date
from typing import Optional

from sqlalchemy import (
    create_engine, Column, Integer, Float, String,
    DateTime, Date, UniqueConstraint, text,
)
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()

PAPER_DB_URL     = "sqlite:///data/paper_trading.db"
STARTING_BALANCE = 100_000.0   # $100k paper money

# Model-specific DB paths
PAPER_DB_PATHS = {
    "standard":    "data/paper_trading.db",
    "relaxed":     "data/paper_relaxed.db",
    "very_relaxed": "data/paper_very_relaxed.db",
}


class PaperAccount(Base):
    """Single-row account — cash balance + metadata."""
    __tablename__ = "paper_account"

    id               = Column(Integer, primary_key=True)
    cash             = Column(Float,   nullable=False, default=STARTING_BALANCE)
    starting_balance = Column(Float,   nullable=False, default=STARTING_BALANCE)
    created_at       = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    reset_at         = Column(DateTime, nullable=True)


class PaperPosition(Base):
    """One row per open ticker position."""
    __tablename__ = "paper_positions"

    id           = Column(Integer, primary_key=True)
    ticker       = Column(String,  nullable=False, unique=True)
    qty          = Column(Float,   nullable=False, default=0.0)
    avg_cost     = Column(Float,   nullable=False, default=0.0)
    stop_loss    = Column(Float,   nullable=True)   # auto-exit if price falls to this
    take_profit_1 = Column(Float,  nullable=True)   # first target (full exit)
    take_profit_2 = Column(Float,  nullable=True)   # stretch target
    opened_at    = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at   = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class PaperTrade(Base):
    """Immutable log of every paper trade executed."""
    __tablename__ = "paper_trades"

    id          = Column(Integer, primary_key=True)
    ticker      = Column(String,  nullable=False)
    action      = Column(String,  nullable=False)   # BUY / SELL
    qty         = Column(Float,   nullable=False)
    price       = Column(Float,   nullable=False)
    total       = Column(Float,   nullable=False)
    cash_after  = Column(Float,   nullable=True)    # cash balance after trade
    signal      = Column(String,  nullable=True)
    stop_loss   = Column(Float,   nullable=True)
    notes       = Column(String,  nullable=True)
    timestamp   = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class PaperEquitySnapshot(Base):
    """Daily total-equity snapshot for P&L chart (cash + market value of positions)."""
    __tablename__ = "paper_equity_snapshots"
    __table_args__ = (UniqueConstraint("snap_date"),)

    id          = Column(Integer, primary_key=True)
    snap_date   = Column(Date,  nullable=False)
    total_equity = Column(Float, nullable=False)
    cash        = Column(Float,  nullable=False)
    positions_value = Column(Float, nullable=False)


def _migrate_positions(engine):
    """Add stop/target columns to paper_positions if the table pre-dates them."""
    new_cols = [
        ("stop_loss",     "FLOAT"),
        ("take_profit_1", "FLOAT"),
        ("take_profit_2", "FLOAT"),
    ]
    with engine.connect() as conn:
        for col, typ in new_cols:
            try:
                conn.execute(text(f"ALTER TABLE paper_positions ADD COLUMN {col} {typ}"))
                conn.commit()
            except Exception:
                pass  # column already exists — ignore


def init_paper_db(db_path: str = None):
    """Create DB, tables, and seed account if first run. Returns (engine, Session).

    Args:
        db_path: Optional path to SQLite file (e.g. "data/paper_relaxed.db").
                 Defaults to data/paper_trading.db.
    """
    url    = f"sqlite:///{db_path}" if db_path else PAPER_DB_URL
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    _migrate_positions(engine)
    Session = sessionmaker(bind=engine)

    with Session() as s:
        if not s.query(PaperAccount).first():
            s.add(PaperAccount(cash=STARTING_BALANCE, starting_balance=STARTING_BALANCE))
            s.commit()

    return engine, Session
