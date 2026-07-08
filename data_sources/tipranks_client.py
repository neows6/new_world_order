"""
data_sources/tipranks_client.py — TipRanks internal API client.

Uses curl_cffi to impersonate Chrome's TLS fingerprint (required to pass
Cloudflare) plus browser session cookies exported from tipranks.com.

Cookie setup (one-time, repeat when you get 403 errors):
  1. Log in to tipranks.com in Chrome/Edge
  2. Open Cookie-Editor extension → Export → Export as JSON
  3. Paste / save to:  data/tipranks_cookies.json
"""

import json
import threading
import time
from pathlib import Path
from typing import Optional

from loguru import logger

_CACHE_DIR   = Path("data/tipranks_cache")
_COOKIE_FILE = Path("data/tipranks_cookies.json")
_CACHE_TTL   = 4 * 3600   # 4 hours

_DATA_URL  = "https://www.tipranks.com/api/stocks/getData/?name={ticker}&benchmark=1&period=3&break={ts}"
_NEWS_URL  = "https://www.tipranks.com/api/stocks/getNewsSentiments/?ticker={ticker}"
_CONS_URL  = "https://www.tipranks.com/api/stocks/getAnalystConsensus/?ticker={ticker}"

_CHROME_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer":         "https://www.tipranks.com/",
    "Origin":          "https://www.tipranks.com",
    "sec-ch-ua":       '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile":   "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest":     "empty",
    "sec-fetch-mode":     "cors",
    "sec-fetch-site":     "same-origin",
}


def _load_cookies_from_file() -> dict:
    """Load Cookie-Editor JSON export → {name: value} dict."""
    if not _COOKIE_FILE.exists():
        raise FileNotFoundError(
            f"TipRanks cookie file not found: {_COOKIE_FILE}\n"
            "Export cookies from tipranks.com with Cookie-Editor and save to that path."
        )
    raw = json.loads(_COOKIE_FILE.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return {c["name"]: c["value"] for c in raw if "name" in c and "value" in c}
    if isinstance(raw, dict):
        return raw
    raise ValueError(f"Unrecognised cookie format in {_COOKIE_FILE}")


def _make_session():
    """Create a curl_cffi session impersonating Chrome 124."""
    from curl_cffi import requests as cf
    s = cf.Session(impersonate="chrome124")
    s.headers.update(_CHROME_HEADERS)
    return s


class TipRanksClient:
    """Thread-safe TipRanks client using curl_cffi + browser cookies."""

    def __init__(self):
        self._session    = None
        self._lock       = threading.Lock()
        self._cookies_ok = False
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _ensure_session(self):
        if self._session is None:
            self._session = _make_session()
        if not self._cookies_ok:
            cookies = _load_cookies_from_file()
            self._session.cookies.clear()
            for k, v in cookies.items():
                self._session.cookies.set(k, v, domain=".tipranks.com")
            self._cookies_ok = True
            logger.info(f"[TipRanks] Loaded {len(cookies)} cookies")

    def reload_cookies(self):
        """Force re-read of cookie file after re-exporting from browser."""
        self._cookies_ok = False
        self._session    = None
        self._ensure_session()

    def _try_cloudflare_refresh(self) -> bool:
        """
        Attempt to renew the short-lived __cf_bm Cloudflare cookie by hitting
        the TipRanks homepage. curl_cffi's Chrome TLS impersonation usually
        passes Cloudflare's fingerprint check and receives a fresh cookie
        without needing real browser JavaScript.
        Returns True if a new __cf_bm was received and the session is usable again.
        """
        try:
            for cookie_name in ("__cf_bm", "TiPMix", "x-ms-routing-name"):
                try:
                    self._session.cookies.delete(cookie_name)
                except Exception:
                    pass
            resp = self._session.get("https://www.tipranks.com/", timeout=15)
            if resp.status_code == 200:
                new_cfbm = self._session.cookies.get("__cf_bm")
                if new_cfbm:
                    logger.info("[TipRanks] Cloudflare cookie auto-refreshed successfully")
                    return True
                # Even without __cf_bm, a 200 means we're through — try proceeding
                logger.info("[TipRanks] Cloudflare warm-up returned 200 (no new cf_bm cookie, but proceeding)")
                return True
            logger.debug(f"[TipRanks] Cloudflare refresh got HTTP {resp.status_code}")
            return False
        except Exception as e:
            logger.debug(f"[TipRanks] Cloudflare refresh attempt failed: {e}")
            return False

    # ── Fetchers ──────────────────────────────────────────────────────────────

    def _get(self, url: str) -> dict:
        """GET url, raise on non-200, return parsed JSON. Auto-retries once on 403."""
        resp = self._session.get(url, timeout=20)
        if resp.status_code in (401, 403):
            logger.debug(f"[TipRanks] HTTP {resp.status_code} — attempting Cloudflare auto-refresh")
            if self._try_cloudflare_refresh():
                resp = self._session.get(url, timeout=20)
                if resp.status_code == 200:
                    return resp.json()
            raise PermissionError(
                f"HTTP {resp.status_code} — cookies may be expired. "
                "Re-export from browser and call reload_cookies()."
            )
        resp.raise_for_status()
        return resp.json()

    def get_stock_data(self, ticker: str) -> dict:
        """Return getData payload (cached 4 h)."""
        cache_path = _CACHE_DIR / f"{ticker}.json"
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if time.time() - cached.get("_ts", 0) < _CACHE_TTL:
                    return cached
            except Exception:
                pass

        with self._lock:
            self._ensure_session()
            ts  = int(time.time() * 1000)
            url = _DATA_URL.format(ticker=ticker, ts=ts)
            data = self._get(url)
            data["_ts"] = time.time()
            cache_path.write_text(json.dumps(data), encoding="utf-8")
            return data

    def get_consensus(self, ticker: str) -> dict:
        """Return analyst consensus payload."""
        with self._lock:
            self._ensure_session()
            return self._get(_CONS_URL.format(ticker=ticker))

    def get_news_sentiment(self, ticker: str) -> dict:
        """Return news sentiment payload."""
        with self._lock:
            self._ensure_session()
            return self._get(_NEWS_URL.format(ticker=ticker))

    def _auth_cookies_valid(self) -> bool:
        """Return True if the long-lived auth cookies (token, tr-uid) are still unexpired."""
        try:
            cookies = json.loads(_COOKIE_FILE.read_text(encoding="utf-8-sig"))
            now = time.time()
            for c in cookies:
                if c.get("name") in ("token", "tr-uid") and not c.get("session"):
                    exp = c.get("expirationDate") or c.get("expires") or c.get("expiry")
                    if exp and float(exp) > now:
                        return True
            return False
        except Exception:
            return False

    def get_batch(self, tickers: list, delay: float = 0.5) -> dict:
        """Fetch getData for all tickers. Returns {ticker: data}. Skips errors."""
        results = {}
        for ticker in tickers:
            try:
                results[ticker] = self.get_stock_data(ticker)
                time.sleep(delay)
            except PermissionError:
                logger.warning(f"[TipRanks] 403 on {ticker} — waiting 60s then retrying once")
                time.sleep(60)
                try:
                    results[ticker] = self.get_stock_data(ticker)
                    logger.info(f"[TipRanks] Retry succeeded for {ticker} after backoff")
                    time.sleep(delay)
                    continue
                except PermissionError:
                    pass
                except Exception:
                    pass
                # Still failing — check if it's a real auth expiry or just rate-limiting
                if self._auth_cookies_valid():
                    logger.warning("[TipRanks] Auth cookies still valid — 403 is rate-limiting, not expiry. Skipping batch.")
                else:
                    logger.warning("[TipRanks] Auth cookies appear expired — alerting via Telegram")
                    try:
                        from monitor.telegram_bot import send_alert as _tg
                        _tg(
                            "⚠️ <b>TipRanks cookies expired</b>\n"
                            "Re-export cookies from tipranks.com "
                            "and save to <code>data/tipranks_cookies.json</code>, then restart the server."
                        )
                    except Exception:
                        pass
                break
            except Exception as e:
                logger.debug(f"[TipRanks] skip {ticker}: {e}")
        return results

    @staticmethod
    def cookies_available() -> bool:
        return _COOKIE_FILE.exists()


# ── Module-level singleton ─────────────────────────────────────────────────────

_client: Optional[TipRanksClient] = None
_client_lock = threading.Lock()


def get_client() -> TipRanksClient:
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            _client = TipRanksClient()
    return _client
