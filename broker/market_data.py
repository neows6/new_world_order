"""
schwab/market_data.py — Schwab API client for price data.

Wraps schwab-py to fetch:
  - Historical OHLCV (daily candles)
  - Real-time quotes
  - Account info (for position sizing in risk layer)

Assumes you already have OAuth tokens set up from your previous work.
Token path configured in config.py via SCHWAB_TOKEN_PATH env var.
"""

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from loguru import logger

from config import config

# Norton AV SSL inspection: schwab-py uses authlib + requests internally, so we
# can't inject our custom SSLContext directly. Pointing REQUESTS_CA_BUNDLE at
# our combined bundle (certifi + Norton root) is the standard way to extend
# requests' trust store without modifying the schwab-py library.
# CURL_CA_BUNDLE covers libcurl-based clients (curl_cffi used by TipRanks).
_BUNDLE = Path(__file__).resolve().parent.parent / "data" / "ca_bundle_with_norton.pem"
if _BUNDLE.exists():
    os.environ.setdefault("REQUESTS_CA_BUNDLE", str(_BUNDLE))
    os.environ.setdefault("SSL_CERT_FILE",      str(_BUNDLE))
    os.environ.setdefault("CURL_CA_BUNDLE",     str(_BUNDLE))


class SchwabMarketData:
    """
    Wraps schwab-py for price and account data.
    Lazy-loads the client to avoid auth errors at import time.
    """

    def __init__(self):
        self._client = None

    def _get_client(self):
        """
        Lazy init — only authenticate when first needed.
        Reuses existing token from token_path if valid.
        """
        if self._client is not None:
            return self._client

        try:
            import certifi
            from authlib.integrations.httpx_client import OAuth2Client as _OA2C
            # Patch __init__ ONCE per process. Guard with a class flag — without it,
            # every call here (each fresh MarketData instance, every retry) re-wraps
            # the previous wrapper on the class, stacking nested calls until
            # OAuth2Client() hits "maximum recursion depth exceeded".
            if not getattr(_OA2C, "_nwo_verify_patched", False):
                _orig_init = _OA2C.__init__
                def _patched_init(self, *a, **kw):
                    kw.setdefault("verify", certifi.where())
                    _orig_init(self, *a, **kw)
                _OA2C.__init__ = _patched_init
                _OA2C._nwo_verify_patched = True

            import schwab
            token_path = Path(config.schwab.token_path)

            if not token_path.exists():
                raise FileNotFoundError(
                    f"Schwab token not found at {token_path}. "
                    "Run the OAuth flow first to generate a token."
                )

            self._client = schwab.auth.client_from_token_file(
                token_path=str(token_path),
                api_key=config.schwab.api_key,
                app_secret=config.schwab.api_secret,
            )
            logger.info("Schwab client initialized from token file")
            return self._client

        except ImportError:
            raise ImportError("schwab-py not installed. Run: pip install schwab-py")
        except Exception as e:
            logger.error(f"Schwab auth failed: {e}")
            raise

    def get_price_history(
        self,
        ticker: str,
        days: int = 365,
        frequency: str = "daily"
    ) -> list:
        """
        Fetch historical OHLCV data for a ticker.
        Returns list of dicts: {date, open, high, low, close, volume}
        """
        import schwab

        client = self._get_client()
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=days)

        try:
            resp = client.get_price_history_every_day(
                symbol=ticker,
                start_datetime=start_dt,
                end_datetime=end_dt,
            )
            resp.raise_for_status()
            data = resp.json()

            candles = data.get("candles", [])
            result = []
            for c in candles:
                result.append({
                    "date": datetime.fromtimestamp(c["datetime"] / 1000),
                    "open": c.get("open"),
                    "high": c.get("high"),
                    "low": c.get("low"),
                    "close": c.get("close"),
                    "volume": c.get("volume"),
                })

            logger.info(f"Fetched {len(result)} daily candles for {ticker}")
            return result

        except Exception as e:
            logger.error(f"Failed to fetch price history for {ticker}: {e}")
            return []

    def get_price_history_intraday(
        self,
        ticker: str,
        freq_minutes: int = 5,
    ) -> list:
        """
        Fetch today's intraday OHLCV candles.
        Returns list of dicts: {date, open, high, low, close, volume}
        freq_minutes: 1 or 5 (default 5)
        """
        client = self._get_client()
        end_dt   = datetime.now()
        start_dt = end_dt.replace(hour=9, minute=30, second=0, microsecond=0)

        try:
            if freq_minutes == 1:
                resp = client.get_price_history_every_minute(
                    symbol=ticker,
                    start_datetime=start_dt,
                    end_datetime=end_dt,
                )
            else:
                resp = client.get_price_history_every_five_minutes(
                    symbol=ticker,
                    start_datetime=start_dt,
                    end_datetime=end_dt,
                )
            resp.raise_for_status()
            candles = resp.json().get("candles", [])
            return [
                {
                    "date":   datetime.fromtimestamp(c["datetime"] / 1000),
                    "open":   c.get("open"),
                    "high":   c.get("high"),
                    "low":    c.get("low"),
                    "close":  c.get("close"),
                    "volume": c.get("volume"),
                }
                for c in candles
            ]
        except Exception as e:
            logger.error(f"Failed to fetch intraday history for {ticker}: {e}")
            return []

    def get_quote(self, ticker: str) -> Optional[dict]:
        """
        Fetch real-time quote for a single ticker.
        Returns dict with current price, bid, ask, volume, market cap.
        """
        client = self._get_client()

        try:
            resp = client.get_quote(ticker)
            resp.raise_for_status()
            data = resp.json()

            quote = data.get(ticker, {}).get("quote", {})
            fundamental = data.get(ticker, {}).get("fundamental", {})

            return {
                "ticker": ticker,
                "last_price": quote.get("lastPrice") or quote.get("mark"),
                "bid": quote.get("bidPrice"),
                "ask": quote.get("askPrice"),
                "volume": quote.get("totalVolume"),
                "high_52w": quote.get("52WeekHigh"),
                "low_52w": quote.get("52WeekLow"),
                "market_cap": fundamental.get("marketCap"),
                "shares_outstanding": fundamental.get("sharesOutstanding"),
                "pe_ratio": fundamental.get("peRatio"),
                "timestamp": datetime.now(),
            }

        except Exception as e:
            logger.error(f"Failed to fetch quote for {ticker}: {e}")
            return None

    def get_quotes_batch(self, tickers: list, chunk_size: int = 200) -> dict:
        """
        Fetch quotes for multiple tickers, chunked to stay within Schwab's
        per-request symbol limit. Returns {ticker: quote_dict}.
        """
        client = self._get_client()
        result = {}

        chunks = [tickers[i:i + chunk_size] for i in range(0, len(tickers), chunk_size)]
        for chunk in chunks:
            try:
                resp = client.get_quotes(chunk)
                resp.raise_for_status()
                data = resp.json()

                for ticker, info in data.items():
                    quote = info.get("quote", {})
                    fundamental = info.get("fundamental", {})
                    last        = quote.get("lastPrice") or quote.get("mark")
                    close_price = quote.get("closePrice") or quote.get("regularMarketLastPrice")
                    net_change  = quote.get("netChange") or quote.get("regularMarketNetChange")
                    net_pct     = quote.get("netPercentChange") or quote.get("regularMarketPercentChange")
                    high_price  = quote.get("highPrice") or quote.get("regularMarketHighPrice")
                    low_price   = quote.get("lowPrice")  or quote.get("regularMarketLowPrice")
                    open_price  = quote.get("openPrice") or quote.get("regularMarketOpenPrice")
                    volume      = quote.get("totalVolume") or quote.get("regularMarketVolume")
                    if net_pct is None and net_change is not None and close_price:
                        try:
                            net_pct = round(net_change / close_price * 100, 2)
                        except Exception:
                            pass
                    result[ticker] = {
                        "ticker":        ticker,
                        "last_price":    last,
                        "open_price":    round(float(open_price), 4)  if open_price  else None,
                        "high_price":    round(float(high_price), 4)  if high_price  else None,
                        "low_price":     round(float(low_price), 4)   if low_price   else None,
                        "volume":        int(volume)                   if volume      else None,
                        "bid":           quote.get("bidPrice"),
                        "ask":           quote.get("askPrice"),
                        "net_change":    round(float(net_change), 4)  if net_change  is not None else None,
                        "net_pct_change":round(float(net_pct), 2)     if net_pct     is not None else None,
                        "prev_close":    round(float(close_price), 2) if close_price else None,
                        "market_cap":    fundamental.get("marketCap"),
                        "shares_outstanding": fundamental.get("sharesOutstanding"),
                        "pe_ratio":      fundamental.get("peRatio"),
                        "timestamp":     datetime.now(),
                    }

            except Exception as e:
                logger.error(f"Batch quote fetch failed for chunk of {len(chunk)}: {e}")

        logger.info(f"Fetched batch quotes for {len(result)} tickers in {len(chunks)} chunk(s)")
        return result

    def get_account_value(self) -> Optional[float]:
        """
        Fetch total account value for position sizing calculations.
        Used by the risk manager layer.
        """
        client = self._get_client()

        try:
            resp = client.get_account(
                account_hash=config.schwab.account_hash,
                fields=[client.Account.Fields.POSITIONS]
            )
            resp.raise_for_status()
            data = resp.json()

            liquidation_value = (
                data.get("securitiesAccount", {})
                    .get("currentBalances", {})
                    .get("liquidationValue")
            )

            if liquidation_value is not None:
                logger.info(f"Account liquidation value: ${liquidation_value:,.2f}")
            return liquidation_value

        except Exception as e:
            logger.error(f"Failed to fetch account value: {e}")
            return None

    def get_positions(self) -> list:
        """
        Fetch current open positions.
        Returns list of {ticker, quantity, market_value, average_price}
        """
        client = self._get_client()

        try:
            resp = client.get_account(
                account_hash=config.schwab.account_hash,
                fields=[client.Account.Fields.POSITIONS]
            )
            resp.raise_for_status()
            data = resp.json()

            raw_positions = (
                data.get("securitiesAccount", {})
                    .get("positions", [])
            )

            positions = []
            for p in raw_positions:
                instrument = p.get("instrument", {})
                positions.append({
                    "ticker": instrument.get("symbol"),
                    "quantity": p.get("longQuantity", 0) - p.get("shortQuantity", 0),
                    "market_value": p.get("marketValue"),
                    "average_price": p.get("averagePrice"),
                    "asset_type": instrument.get("assetType"),
                })

            return positions

        except Exception as e:
            logger.error(f"Failed to fetch positions: {e}")
            return []

    def get_options_chain(self, symbol: str, days_to_expiry: int = 45) -> Optional[dict]:
        """
        Fetch options chain for a symbol from Schwab /marketdata/v1/chains.
        Returns a simplified dict with puts and calls.
        """
        try:
            client = self._get_client()
            from datetime import date, timedelta
            today = date.today()
            to_date = today + timedelta(days=days_to_expiry)

            from schwab.client import Client as _SchwabClient
            resp = client.get_option_chain(
                symbol,
                contract_type=_SchwabClient.Options.ContractType.ALL,
                to_date=to_date,
            )
            try:
                resp.raise_for_status()
            except Exception as _http_err:
                logger.error(f"get_options_chain({symbol}) HTTP {resp.status_code}: {resp.text[:300]}")
                return None
            raw = resp.json()

            underlying_price = raw.get("underlyingPrice", 0)
            volatility = raw.get("volatility", 0)
            iv_rank = raw.get("ivRank", None)

            def _parse_leg(exp_date_str, strike_str, leg_list):
                results = []
                for leg in leg_list:
                    exp_clean = exp_date_str.split(":")[0]
                    dte_part  = exp_date_str.split(":")[-1] if ":" in exp_date_str else "0"
                    try:
                        dte = int(dte_part)
                    except ValueError:
                        dte = 0
                    results.append({
                        "expiry":        exp_clean,
                        "dte":           dte,
                        "strike":        float(strike_str),
                        "delta":         leg.get("delta", 0),
                        "bid":           leg.get("bid", 0),
                        "ask":           leg.get("ask", 0),
                        "mid":           round((leg.get("bid", 0) + leg.get("ask", 0)) / 2, 2),
                        "volume":        leg.get("totalVolume", 0),
                        "openInterest":  leg.get("openInterest", 0),
                        "iv":            round(leg.get("volatility", 0), 2),
                    })
                return results

            puts  = []
            calls = []
            for exp_date_str, strikes in raw.get("putExpDateMap", {}).items():
                for strike_str, leg_list in strikes.items():
                    puts.extend(_parse_leg(exp_date_str, strike_str, leg_list))
            for exp_date_str, strikes in raw.get("callExpDateMap", {}).items():
                for strike_str, leg_list in strikes.items():
                    calls.extend(_parse_leg(exp_date_str, strike_str, leg_list))

            return {
                "symbol":           symbol,
                "underlyingPrice":  underlying_price,
                "volatility":       round(volatility, 2),
                "ivRank":           iv_rank,
                "puts":             puts,
                "calls":            calls,
            }
        except Exception as e:
            logger.error(f"get_options_chain({symbol}) error: {e}")
            return None

    def get_iv_rank(self, symbol: str) -> Optional[float]:
        """
        Approximate IV Rank from current chain IV vs the range seen across
        the nearest expiry strikes. Returns 0-100 or None if unavailable.
        """
        try:
            chain = self.get_options_chain(symbol, days_to_expiry=60)
            if not chain:
                return None
            current_iv = chain.get("volatility", 0)
            if not current_iv:
                return None
            all_ivs = [p["iv"] for p in chain["puts"] if p["iv"] > 0] + \
                      [c["iv"] for c in chain["calls"] if c["iv"] > 0]
            if not all_ivs:
                return None
            iv_min = min(all_ivs)
            iv_max = max(all_ivs)
            if iv_max == iv_min:
                return 50.0
            rank = round((current_iv - iv_min) / (iv_max - iv_min) * 100, 1)
            return max(0.0, min(100.0, rank))
        except Exception as e:
            logger.error(f"get_iv_rank({symbol}) error: {e}")
            return None
