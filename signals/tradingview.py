"""
signals/tradingview.py — TradingView Signal Integration.

Three integration modes (in order of complexity):

  1. TECHNICAL CONSENSUS  (tradingview-ta library)
     Scrapes TradingView's aggregated reading of 26 technical indicators
     for any symbol. No auth required for basic functionality.
     Install: pip install tradingview-ta
     Use:     TradingViewSignalFetcher.get_signal(ticker)

  2. LIVE PRICE DATA  (tvdatafeed library)
     Logs into TradingView and fetches OHLCV bars at any interval.
     Useful for: VIX data, pre/post market bars, extended watchlist.
     Credentials from env: TV_USERNAME, TV_PASSWORD
     Install: pip install tvdatafeed
     Use:     TradingViewSignalFetcher.get_ohlcv(ticker, interval)

  3. WEBHOOK ALERTS  (see pipeline/tv_webhook.py)
     Pine Script alerts POST JSON payloads to a local webhook server.
     Allows complex pattern detection (Wyckoff, harmonic, order blocks)
     written in Pine Script → injected back into the NWO pipeline.
     Use: python pipeline/tv_webhook.py --port 5050
          Then create alerts in TradingView pointing to http://localhost:5050/alert

TVSignalResult plugs into SignalAggregator.aggregate(tv_signal=...) at 5% weight.

Symbol mapping:
  Schwab tickers map directly to TradingView for US equities (NASDAQ:AAPL etc.)
  VIX: TradingView uses "CBOE:VIX"
"""

import os
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger


# ─────────────────────────────────────────────────────────────────────────────
# Result dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TVSignalResult:
    """
    TradingView technical consensus for a single ticker.
    Based on TradingView's aggregation of 26 technical indicators.
    """
    ticker: str

    # TradingView's recommendation
    recommendation: str    # "STRONG_BUY", "BUY", "NEUTRAL", "SELL", "STRONG_SELL"

    # Indicator vote counts (out of 26 total)
    buy_count:     int     # Indicators voting buy
    sell_count:    int     # Indicators voting sell
    neutral_count: int     # Indicators neutral

    # Oscillators sub-group (RSI, MACD, Stoch, CCI, etc.)
    oscillators_recommendation: str
    oscillators_buy:     int
    oscillators_sell:    int
    oscillators_neutral: int

    # Moving averages sub-group (EMA, SMA, Ichimoku, VWMA, etc.)
    ma_recommendation: str
    ma_buy:     int
    ma_sell:    int
    ma_neutral: int

    # Selected raw indicator values (from TradingView)
    rsi: Optional[float]     = None   # 14-period RSI
    macd_line: Optional[float] = None
    macd_signal: Optional[float] = None

    # Normalized score for SignalAggregator
    score: float = 0.0   # -1.0 to +1.0

    notes: list = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Signal fetcher
# ─────────────────────────────────────────────────────────────────────────────

# Map Schwab ticker format to TradingView exchange:symbol format
_TV_EXCHANGE_MAP = {
    # Most US equities are on NASDAQ or NYSE
    "AAPL": "NASDAQ:AAPL",
    "MSFT": "NASDAQ:MSFT",
    "GOOGL": "NASDAQ:GOOGL",
    "AMZN": "NASDAQ:AMZN",
    "META": "NASDAQ:META",
    "TSLA": "NASDAQ:TSLA",
    "NVDA": "NASDAQ:NVDA",
    "BRK.B": "NYSE:BRK.B",
    "JPM": "NYSE:JPM",
    "V": "NYSE:V",
    "JNJ": "NYSE:JNJ",
    "PG": "NYSE:PG",
}


def _schwab_to_tv(ticker: str) -> str:
    """Convert Schwab ticker to TradingView exchange:symbol format."""
    if ticker in _TV_EXCHANGE_MAP:
        return _TV_EXCHANGE_MAP[ticker]
    # Default: try NASDAQ first (works for most liquid US tech names)
    return f"NASDAQ:{ticker}"


def _recommendation_to_score(rec: str) -> float:
    """Convert TradingView text recommendation to -1.0 to +1.0 score."""
    return {
        "STRONG_BUY":  1.0,
        "BUY":         0.6,
        "NEUTRAL":     0.0,
        "SELL":        -0.6,
        "STRONG_SELL": -1.0,
    }.get(rec, 0.0)


class TradingViewSignalFetcher:
    """
    Fetches TradingView technical analysis and price data.

    Integration 1 — Technical Consensus (tradingview-ta):
      Aggregates 26 built-in TradingView indicators into a single
      BUY / SELL / NEUTRAL recommendation. Free, no auth required.

    Integration 2 — Live Price Data (tvdatafeed):
      Fetches OHLCV bars from TradingView at any interval.
      Requires TradingView account credentials in env vars.
    """

    def __init__(self):
        self._tv_data_client = None   # Lazy-init tvdatafeed
        self._tv_username = os.getenv("TV_USERNAME", "")
        self._tv_password = os.getenv("TV_PASSWORD", "")

    # ── Integration 1: Technical Consensus ───────────────────────────────────

    def get_signal(self, ticker: str, interval: str = "1d") -> Optional[TVSignalResult]:
        """
        Fetch TradingView's technical analysis consensus for a ticker.

        Args:
            ticker:   Schwab-format ticker (e.g. "AAPL")
            interval: "1d" (daily), "1W" (weekly), "4h", "1h", "15m", "5m", "1m"

        Returns:
            TVSignalResult or None if tradingview-ta not installed / network error.
        """
        try:
            from tradingview_ta import TA_Handler, Interval, Exchange
        except ImportError:
            logger.debug("[TV] tradingview-ta not installed — run: pip install tradingview-ta")
            return None

        # Map interval string to tradingview_ta Interval enum
        interval_map = {
            "1m":  Interval.INTERVAL_1_MINUTE,
            "5m":  Interval.INTERVAL_5_MINUTES,
            "15m": Interval.INTERVAL_15_MINUTES,
            "1h":  Interval.INTERVAL_1_HOUR,
            "4h":  Interval.INTERVAL_4_HOURS,
            "1d":  Interval.INTERVAL_1_DAY,
            "1W":  Interval.INTERVAL_1_WEEK,
        }
        tv_interval = interval_map.get(interval, Interval.INTERVAL_1_DAY)

        tv_symbol = _schwab_to_tv(ticker)
        # Split "EXCHANGE:SYMBOL" → exchange, symbol for TA_Handler
        if ":" in tv_symbol:
            exchange, symbol = tv_symbol.split(":", 1)
        else:
            exchange, symbol = "NASDAQ", tv_symbol

        try:
            handler = TA_Handler(
                symbol=symbol,
                exchange=exchange,
                screener="america",
                interval=tv_interval,
            )
            analysis = handler.get_analysis()
        except Exception as e:
            logger.debug(f"[TV] {ticker}: fetch failed — {e}")
            return None

        summary    = analysis.summary
        oscill     = analysis.oscillators
        moving_avg = analysis.moving_averages
        indicators = analysis.indicators

        rec = summary.get("RECOMMENDATION", "NEUTRAL")
        score = _recommendation_to_score(rec)

        return TVSignalResult(
            ticker=ticker,
            recommendation=rec,
            buy_count     = summary.get("BUY",     0),
            sell_count    = summary.get("SELL",    0),
            neutral_count = summary.get("NEUTRAL", 0),
            oscillators_recommendation = oscill.get("RECOMMENDATION", "NEUTRAL"),
            oscillators_buy     = oscill.get("BUY",     0),
            oscillators_sell    = oscill.get("SELL",    0),
            oscillators_neutral = oscill.get("NEUTRAL", 0),
            ma_recommendation   = moving_avg.get("RECOMMENDATION", "NEUTRAL"),
            ma_buy     = moving_avg.get("BUY",     0),
            ma_sell    = moving_avg.get("SELL",    0),
            ma_neutral = moving_avg.get("NEUTRAL", 0),
            rsi        = indicators.get("RSI"),
            macd_line  = indicators.get("MACD.macd"),
            macd_signal= indicators.get("MACD.signal"),
            score=score,
            notes=[f"TradingView {interval} consensus: {rec} ({summary.get('BUY',0)}B/{summary.get('SELL',0)}S)"],
        )

    # ── Integration 2: Live Price Data via tvdatafeed ─────────────────────────

    def _get_tv_client(self):
        """Lazy-init tvdatafeed client with account credentials from env."""
        if self._tv_data_client:
            return self._tv_data_client

        try:
            from tvdatafeed import TvDatafeed
        except ImportError:
            raise ImportError("tvdatafeed not installed — run: pip install tvdatafeed")

        if self._tv_username and self._tv_password:
            logger.debug(f"[TV] Logging in as {self._tv_username}")
            self._tv_data_client = TvDatafeed(
                username=self._tv_username,
                password=self._tv_password,
            )
        else:
            # Unauthenticated — limited to 1-day data on major symbols
            logger.debug("[TV] No credentials — using unauthenticated tvdatafeed")
            self._tv_data_client = TvDatafeed()

        return self._tv_data_client

    def get_ohlcv(
        self,
        ticker: str,
        interval: str = "1D",
        n_bars: int = 252,
        exchange: str = "NASDAQ",
    ) -> Optional[object]:
        """
        Fetch OHLCV bars from TradingView via tvdatafeed.

        Returns a pandas DataFrame with columns: open, high, low, close, volume
        or None on failure.

        Args:
            ticker:   Schwab-format ticker
            interval: "1D", "1W", "240" (4h), "60" (1h), "15", "5", "1"
            n_bars:   Number of bars to fetch (max ~5000)
            exchange: TradingView exchange name
        """
        try:
            from tvdatafeed import Interval as TVInterval
        except ImportError:
            logger.debug("[TV] tvdatafeed not installed — run: pip install tvdatafeed")
            return None

        interval_map = {
            "1":   TVInterval.in_1_minute,
            "5":   TVInterval.in_5_minute,
            "15":  TVInterval.in_15_minute,
            "60":  TVInterval.in_1_hour,
            "240": TVInterval.in_4_hour,
            "1D":  TVInterval.in_daily,
            "1W":  TVInterval.in_weekly,
            "1M":  TVInterval.in_monthly,
        }
        tv_interval = interval_map.get(interval, TVInterval.in_daily)

        try:
            client = self._get_tv_client()
            # Handle Schwab dot notation (BRK.B → BRK.B on NYSE)
            symbol = ticker.replace(".", "").upper() if "." not in ticker else ticker
            df = client.get_hist(
                symbol=symbol,
                exchange=exchange,
                interval=tv_interval,
                n_bars=n_bars,
            )
            return df
        except Exception as e:
            logger.debug(f"[TV] get_ohlcv {ticker}: {e}")
            return None

    def get_vix(self) -> Optional[float]:
        """
        Fetch current VIX level from TradingView.
        Used as fallback when Schwab VIX quote is unavailable.
        """
        df = self.get_ohlcv("VIX", interval="1D", n_bars=3, exchange="CBOE")
        if df is not None and len(df) > 0:
            return float(df["close"].iloc[-1])
        return None


# ─────────────────────────────────────────────────────────────────────────────
# TradingView Webhook approach (documentation)
# ─────────────────────────────────────────────────────────────────────────────
#
# For complex Pine Script patterns (institutional order blocks, Wyckoff,
# harmonic patterns, multi-timeframe confluence) that our model doesn't compute:
#
# 1. Write the pattern detection in Pine Script on TradingView
# 2. Create a TradingView alert that fires when pattern triggers
# 3. Set alert webhook URL to: http://YOUR_SERVER/tv-alert
# 4. Start the webhook server: python pipeline/tv_webhook.py --port 5050
#    (use ngrok for local dev: ngrok http 5050)
#
# Alert JSON payload format (set in TradingView alert message):
#   {"ticker": "{{ticker}}", "action": "{{strategy.order.action}}",
#    "price": {{close}}, "pattern": "wyckoff_accumulation",
#    "timeframe": "{{interval}}", "notes": "{{strategy.order.comment}}"}
#
# The webhook server writes the alert to the DB (TradingViewAlert table)
# and the next analysis cycle picks it up as a pre-filter boost signal.
#
# See pipeline/tv_webhook.py for the Flask implementation.
