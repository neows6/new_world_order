"""
config.py — Central configuration for schwab_trader.
All secrets loaded from .env — never hardcode credentials.
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class SchwabConfig:
    api_key: str = field(default_factory=lambda: os.getenv("SCHWAB_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: os.getenv("SCHWAB_API_SECRET", ""))
    redirect_uri: str = field(default_factory=lambda: os.getenv("SCHWAB_REDIRECT_URI", "https://127.0.0.1"))
    token_path: str = field(default_factory=lambda: os.getenv("SCHWAB_TOKEN_PATH", "tokens/schwab_token.json"))
    account_hash: str = field(default_factory=lambda: os.getenv("SCHWAB_ACCOUNT_HASH", ""))


@dataclass
class DatabaseConfig:
    # SQLite by default — swap to postgres URL for production
    url: str = field(default_factory=lambda: os.getenv(
        "DATABASE_URL", "sqlite:///data/schwab_trader.db"
    ))
    echo_sql: bool = False  # Set True for debugging SQL


@dataclass
class EdgarConfig:
    base_url: str = "https://data.sec.gov"
    user_agent: str = field(default_factory=lambda: os.getenv(
        "EDGAR_USER_AGENT", "YourName your@email.com"  # SEC requires this
    ))
    # SEC rate limit: 10 requests/second — we stay well under
    requests_per_second: float = 5.0
    cache_dir: str = "data/edgar_cache"


@dataclass
class RiskConfig:
    max_position_pct: float = 0.05       # 5% of portfolio per ticker
    max_sector_pct: float = 0.25         # 25% per sector
    daily_loss_halt_pct: float = 0.03    # Halt if down 3% on the day
    min_margin_of_safety: float = 0.15   # 15% discount to intrinsic value
    min_fud_score: float = 0.60          # Signal quality threshold
    max_trade_dollars: float = 500.0     # Hard cap per single trade ($)
    max_daily_trades: int = 5            # Max trades per day
    max_shares_per_trade: int = 50       # Hard share quantity ceiling
    dry_run: bool = True                 # ALWAYS True until you're confident


@dataclass
class BriefConfig:
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    google_api_key: str    = field(default_factory=lambda: os.getenv("GOOGLE_API_KEY", ""))
    # Generation schedule: "07:00" ET on trading days
    generate_time: str = field(default_factory=lambda: os.getenv("BRIEF_GENERATE_TIME", "07:00"))
    cache_file: str = "data/morning_brief.json"
    # How many hours before the brief is considered stale and regenerated
    stale_hours: int = 4


@dataclass
class TipRanksConfig:
    email:    str = field(default_factory=lambda: os.getenv("TIPRANKS_EMAIL",    "Alonzoaceves@gmail.com"))
    password: str = field(default_factory=lambda: os.getenv("TIPRANKS_PASSWORD", "Texas123"))


@dataclass
class AppConfig:
    schwab: SchwabConfig = field(default_factory=SchwabConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    edgar: EdgarConfig = field(default_factory=EdgarConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    brief: BriefConfig = field(default_factory=BriefConfig)
    tipranks: TipRanksConfig = field(default_factory=TipRanksConfig)

    # AI Watch tickers — scanned every 1 minute, breakout override active.
    # When momentum ≥ "momentum" AND RVOL ≥ 1.5×, fundamentals score is floored
    # at 0 so premium valuation doesn't block a confirmed breakout signal.
    ai_watch_tickers: list = field(default_factory=lambda: ["TSLA"])

    # Tickers to watch — extend as needed
    watchlist: list = field(default_factory=lambda: [
        "AAPL", "MSFT", "GOOGL", "AMZN", "META",
        "JPM", "V", "JNJ", "PG", "TSLA",
        "UAVS", "RIVN", "QBTS", "RCAT",
        # Stage Gate tickers — must be in watchlist for price/fundamentals ingestion
        "AEE", "AEP", "AFL", "ADI", "AJG", "ACGL",
    ])

    # How often to refresh fundamentals (hours)
    fundamentals_refresh_hours: int = 24

    # How often to check price signals (minutes)
    price_check_interval_minutes: int = 5

    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    log_file: str = "logs/schwab_trader.log"


# Singleton — import this everywhere
config = AppConfig()
