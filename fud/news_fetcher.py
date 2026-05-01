"""
fud/news_fetcher.py — News Article Fetcher.

Sources (all free, no API key required for basics):
  1. SEC EDGAR RSS    — 8-K filings, earnings, material events (primary)
  2. SEC EDGAR search — 10-K, 10-Q, Form 4 filings
  3. Benzinga free    — Market news headlines (limited)
  4. Fallback         — Headline construction from existing DB data

For production upgrade, add:
  - NewsAPI ($50/month) — broad news coverage
  - Polygon.io news endpoint ($29/month) — financial news with ticker filtering
  - FinancialModelingPrep ($30/month) — includes insider + news + earnings

SEC EDGAR XBRL RSS feeds are completely free and provide
real-time 8-K filings which are the highest-quality news signals.
"""

import time
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import requests
from loguru import logger

from config import config
from fud.sources import classify_source_from_url


@dataclass
class RawArticle:
    headline: str
    summary: str
    url: Optional[str]
    source: str
    ticker: str
    published_at: Optional[datetime]
    is_sec_filing: bool
    filing_type: Optional[str]
    raw_data: dict


class NewsFetcher:
    """
    Fetches news and SEC filings for a ticker.
    Prioritizes primary SEC sources, supplements with financial news.
    """

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": config.edgar.user_agent,
            "Accept": "application/json, text/html, application/rss+xml",
        })
        self._last_request = 0.0
        self._min_interval = 0.20   # 5 req/sec max for SEC

    def _rate_limit(self):
        elapsed = time.time() - self._last_request
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request = time.time()

    # ── SEC EDGAR: 8-K RSS Feed ───────────────────────────────

    def fetch_sec_8k_rss(self, cik: str, days_back: int = 30) -> list:
        """
        Fetch recent 8-K filings from SEC EDGAR RSS.
        8-K = material company events: earnings, guidance, M&A, executive changes.
        These are the highest quality signals — primary source, verifiable.
        """
        self._rate_limit()
        cik_padded = str(cik).zfill(10)

        # EDGAR ATOM feed for company filings
        url = (
            f"https://www.sec.gov/cgi-bin/browse-edgar"
            f"?action=getcompany&CIK={cik_padded}"
            f"&type=8-K&dateb=&owner=include&count=20&search_text=&output=atom"
        )

        try:
            resp = self.session.get(url, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.warning(f"SEC 8-K RSS fetch failed for CIK {cik}: {e}")
            return []

        return self._parse_edgar_atom(resp.text, cik, "8-K", days_back)

    def fetch_sec_filings_rss(self, cik: str, filing_types: list = None, days_back: int = 90) -> list:
        """
        Fetch multiple filing types from EDGAR.
        Covers 10-K (annual), 10-Q (quarterly), 8-K (events), Form 4 (insider).
        """
        if filing_types is None:
            filing_types = ["8-K", "10-Q", "10-K"]

        all_articles = []
        for ft in filing_types:
            self._rate_limit()
            url = (
                f"https://www.sec.gov/cgi-bin/browse-edgar"
                f"?action=getcompany&CIK={str(cik).zfill(10)}"
                f"&type={ft}&dateb=&owner=include&count=10&search_text=&output=atom"
            )
            try:
                resp = self.session.get(url, timeout=20)
                if resp.status_code == 200:
                    articles = self._parse_edgar_atom(resp.text, cik, ft, days_back)
                    all_articles.extend(articles)
            except requests.RequestException as e:
                logger.debug(f"Filing type {ft} fetch failed for CIK {cik}: {e}")

        return all_articles

    def _parse_edgar_atom(self, xml_text: str, cik: str, filing_type: str, days_back: int) -> list:
        """Parse SEC EDGAR ATOM feed into RawArticle objects."""
        articles = []
        cutoff = datetime.now() - timedelta(days=days_back)

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            logger.warning(f"Failed to parse EDGAR ATOM feed: {e}")
            return []

        ns = {"atom": "http://www.w3.org/2005/Atom"}

        for entry in root.findall("atom:entry", ns):
            try:
                title   = entry.findtext("atom:title", "", ns).strip()
                updated = entry.findtext("atom:updated", "", ns)
                link    = entry.find("atom:link", ns)
                url     = link.get("href") if link is not None else None
                summary = entry.findtext("atom:summary", "", ns).strip()

                # Parse date
                pub_dt = None
                if updated:
                    try:
                        pub_dt = datetime.fromisoformat(updated.replace("Z", "+00:00")).replace(tzinfo=None)
                    except ValueError:
                        pass

                if pub_dt and pub_dt < cutoff:
                    continue

                articles.append(RawArticle(
                    headline=f"{filing_type} Filing: {title}",
                    summary=summary[:500] if summary else f"SEC {filing_type} filing",
                    url=url,
                    source="sec_filing",
                    ticker="",        # Caller fills this in
                    published_at=pub_dt,
                    is_sec_filing=True,
                    filing_type=filing_type,
                    raw_data={"cik": cik, "type": filing_type},
                ))

            except Exception as e:
                logger.debug(f"Failed to parse EDGAR entry: {e}")
                continue

        logger.info(f"Fetched {len(articles)} {filing_type} filings from EDGAR for CIK {cik}")
        return articles

    # ── Benzinga free news endpoint ───────────────────────────

    def fetch_benzinga_headlines(self, ticker: str, days_back: int = 7) -> list:
        """
        Fetch recent news headlines from Benzinga free endpoint.
        Limited to public free tier — no API key needed for basic headlines.
        """
        self._rate_limit()
        url = f"https://api.benzinga.com/api/v2/news?token=&tickers={ticker}&pageSize=20"

        try:
            resp = self.session.get(url, timeout=15)
            if resp.status_code != 200:
                return []
            data = resp.json()
        except (requests.RequestException, json.JSONDecodeError):
            return []

        articles = []
        cutoff = datetime.now() - timedelta(days=days_back)

        for item in data if isinstance(data, list) else []:
            try:
                pub_str = item.get("created", "")
                pub_dt = None
                if pub_str:
                    try:
                        pub_dt = datetime.fromisoformat(pub_str[:19])
                    except ValueError:
                        pass

                if pub_dt and pub_dt < cutoff:
                    continue

                articles.append(RawArticle(
                    headline=item.get("title", ""),
                    summary=item.get("teaser", "")[:300],
                    url=item.get("url"),
                    source="benzinga",
                    ticker=ticker,
                    published_at=pub_dt,
                    is_sec_filing=False,
                    filing_type=None,
                    raw_data=item,
                ))
            except Exception:
                continue

        logger.info(f"Fetched {len(articles)} Benzinga headlines for {ticker}")
        return articles

    # ── SEC full-text search ──────────────────────────────────

    def fetch_edgar_full_text(self, ticker: str, days_back: int = 30) -> list:
        """
        Search EDGAR full-text for recent filings mentioning the ticker.
        Uses EDGAR full-text search API (EFTS).
        """
        self._rate_limit()
        cutoff_str = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
        url = (
            f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22"
            f"&dateRange=custom&startdt={cutoff_str}&forms=8-K,10-Q,10-K&hits.hits._source=period_of_report,file_date,form_type,display_names,entity_name"
        )

        try:
            resp = self.session.get(url, timeout=15)
            if resp.status_code != 200:
                return []
            data = resp.json()
        except (requests.RequestException, json.JSONDecodeError):
            return []

        articles = []
        hits = data.get("hits", {}).get("hits", [])

        for hit in hits[:10]:
            src = hit.get("_source", {})
            form_type = src.get("form_type", "SEC")
            entity    = src.get("entity_name", ticker)
            file_date = src.get("file_date", "")

            pub_dt = None
            if file_date:
                try:
                    pub_dt = datetime.strptime(file_date, "%Y-%m-%d")
                except ValueError:
                    pass

            articles.append(RawArticle(
                headline=f"{form_type}: {entity}",
                summary=f"SEC {form_type} filing for {entity}",
                url=f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company={ticker}&type={form_type}",
                source="sec_edgar",
                ticker=ticker,
                published_at=pub_dt,
                is_sec_filing=True,
                filing_type=form_type,
                raw_data=src,
            ))

        return articles

    # ── Synthetic from DB data ────────────────────────────────

    def synthesize_from_fundamentals(self, ticker: str, fundamental_record) -> list:
        """
        Create synthetic news articles from fundamental DB records.
        Ensures we always have some signal even when news APIs are dry.
        These score as quality signals because they're based on SEC data.
        """
        articles = []
        f = fundamental_record

        if f is None:
            return []

        # Revenue growth signal
        if f.revenue is not None:
            rev_b = f.revenue / 1e9
            articles.append(RawArticle(
                headline=f"{ticker} FY{f.fiscal_year} Revenue: ${rev_b:.2f}B",
                summary=(
                    f"Annual revenue {f.revenue/1e9:.2f}B. "
                    f"Gross margin: {f'{f.gross_margin:.1%}' if f.gross_margin else 'N/A'}. "
                    f"FCF: ${f.free_cash_flow/1e9:.2f}B" if f.free_cash_flow else ""
                ),
                url=None,
                source="sec_filing",
                ticker=ticker,
                published_at=f.created_at,
                is_sec_filing=True,
                filing_type="10-K",
                raw_data={"fiscal_year": f.fiscal_year, "source": "db_fundamental"},
            ))

        # ROIC signal
        if f.roic is not None:
            articles.append(RawArticle(
                headline=f"{ticker} ROIC {f.roic:.1%} FY{f.fiscal_year}",
                summary=f"Return on invested capital: {f.roic:.1%}. Net debt/EBITDA: {f.net_debt_to_ebitda:.2f}x" if f.net_debt_to_ebitda else "",
                url=None,
                source="sec_filing",
                ticker=ticker,
                published_at=f.created_at,
                is_sec_filing=True,
                filing_type="computed",
                raw_data={"fiscal_year": f.fiscal_year},
            ))

        return articles

    # ── Master fetch ──────────────────────────────────────────

    def fetch_all(
        self,
        ticker: str,
        cik: Optional[str] = None,
        days_back: int = 30,
        fundamental_record=None,
    ) -> list:
        """
        Fetch all available news for a ticker from all sources.
        Returns combined, deduplicated list of RawArticle objects.
        """
        all_articles = []

        # Primary: SEC EDGAR (free, highest quality)
        if cik:
            sec_articles = self.fetch_sec_filings_rss(
                cik, filing_types=["8-K", "10-Q", "10-K"], days_back=days_back
            )
            for a in sec_articles:
                a.ticker = ticker
            all_articles.extend(sec_articles)

        # Secondary: Benzinga headlines
        benzinga_articles = self.fetch_benzinga_headlines(ticker, days_back=min(days_back, 7))
        all_articles.extend(benzinga_articles)

        # Tertiary: EDGAR full-text search
        if cik:
            fts_articles = self.fetch_edgar_full_text(ticker, days_back=days_back)
            all_articles.extend(fts_articles)

        # Fallback: Synthesize from DB fundamentals
        if fundamental_record:
            synth_articles = self.synthesize_from_fundamentals(ticker, fundamental_record)
            all_articles.extend(synth_articles)

        # Deduplicate by headline similarity
        seen_headlines = set()
        unique = []
        for a in all_articles:
            key = a.headline[:50].lower().strip()
            if key not in seen_headlines:
                seen_headlines.add(key)
                unique.append(a)

        logger.info(f"[NEWS] {ticker}: {len(unique)} unique articles fetched "
                    f"({len(all_articles)} total before dedup)")
        return unique
