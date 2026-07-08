"""
signals/insider_flow.py — Form 4 Insider Buying Signal Scorer.

Why insider buying works:
  Harvard Business School (2022): stocks with significant insider buying
  outperformed the market by 6% annually over 3 years.
  The key is filtering REAL conviction buys vs. routine transactions.

Critical filters (most insider buying signals are noise):
  ✓ Open-market purchases ONLY — not option exercises, RSU vests, or gifts
  ✓ Dollar value > $50,000 — small buys are noise
  ✓ CEO/CFO/Director weight > other insiders — they know the most
  ✓ Cluster buying — multiple insiders buying within 30 days = very strong
  ✓ Buying AFTER a price decline — insiders buying weakness, not chasing

Red flags (insider SELLS are usually noise):
  ✗ Sales for "personal financial planning" — not informative
  ✗ 10b5-1 plans — pre-scheduled, not informative
  ✗ Tiny amounts relative to their total holdings

Data source: SEC EDGAR Form 4 filings (free, public, 2-business-day lag)
API: https://data.sec.gov/api/xbrl/ (same EDGAR client from Layer 1)
"""

import json
import time
import requests
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from loguru import logger

from config import config
from utils.ssl_context import make_requests_session


# Insider role weights — CEO/CFO/Director carry more conviction
INSIDER_ROLE_WEIGHTS = {
    "CEO":                  3.0,
    "Chief Executive":      3.0,
    "CFO":                  2.8,
    "Chief Financial":      2.8,
    "COO":                  2.5,
    "Chief Operating":      2.5,
    "President":            2.5,
    "Director":             2.0,
    "Chairman":             2.5,
    "10%":                  1.8,   # 10% beneficial owner
    "VP":                   1.5,
    "SVP":                  1.5,
    "EVP":                  1.5,
    "default":              1.0,
}

# Transaction type codes from SEC Form 4
OPEN_MARKET_PURCHASE_CODES = {"P"}       # "P" = open-market purchase
OPEN_MARKET_SALE_CODES     = {"S"}       # "S" = open-market sale
EXCLUDED_CODES             = {           # Exclude these — not conviction signals
    "A",  # Grant/award
    "F",  # Tax withholding
    "M",  # Option exercise
    "C",  # Conversion
    "J",  # Other
    "G",  # Gift
    "V",  # Voluntary
    "W",  # Will/inheritance
    "D",  # Disposition to trust
}


@dataclass
class InsiderTransaction:
    insider_name: str
    insider_title: str
    transaction_type: str    # "buy" or "sell"
    transaction_code: str    # SEC code (P, S, M, etc.)
    shares: float
    price_per_share: float
    total_value: float
    filed_date: datetime
    transaction_date: datetime
    is_open_market: bool
    role_weight: float


@dataclass
class InsiderSignalResult:
    ticker: str
    cik: str
    signal: str              # "strong_buy", "buy", "neutral", "sell", "strong_sell"
    score: float             # -5.0 to +5.0

    # Summary stats (last 90 days)
    total_buys_90d: int
    total_sells_90d: int
    total_buy_value_90d: float
    total_sell_value_90d: float
    buy_sell_ratio: float    # > 1.0 = more buying than selling
    unique_buyers_90d: int   # Cluster metric

    # Cluster detection
    cluster_buy_detected: bool     # 3+ insiders buying within 30 days
    cluster_buy_value: float       # Total value of cluster buy

    # Best individual buy
    largest_buy_value: float
    largest_buyer_title: str

    # Context
    days_since_last_buy: Optional[int]
    price_change_since_buy: Optional[float]  # How much stock moved after buy

    transactions: list       # Raw InsiderTransaction objects (last 90 days)
    notes: list
    warnings: list


class InsiderFlowAnalyzer:
    """
    Fetches and scores Form 4 insider buying from SEC EDGAR.
    Uses the same EDGAR HTTP session as Layer 1 (EdgarClient).
    """

    # Minimum buy value to count as meaningful signal
    MIN_BUY_VALUE = 25_000      # $25k minimum

    # Strong signal threshold
    STRONG_BUY_VALUE = 250_000  # $250k+ = high conviction

    # Cluster: how many days window for multi-insider buy
    CLUSTER_WINDOW_DAYS = 30
    CLUSTER_MIN_BUYERS  = 2     # 2+ insiders = cluster

    def __init__(self):
        self.session = make_requests_session()
        self.session.headers.update({
            "User-Agent": config.edgar.user_agent,
            "Accept-Encoding": "gzip, deflate",
        })
        self._last_request = 0.0
        self._min_interval = 0.2  # 5 req/sec max

    def _rate_limit(self):
        elapsed = time.time() - self._last_request
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request = time.time()

    def _get_role_weight(self, title: str) -> float:
        """Map insider title to conviction weight."""
        if not title:
            return INSIDER_ROLE_WEIGHTS["default"]
        title_upper = title.upper()
        for key, weight in INSIDER_ROLE_WEIGHTS.items():
            if key.upper() in title_upper:
                return weight
        return INSIDER_ROLE_WEIGHTS["default"]

    def fetch_form4_transactions(self, cik: str, days_back: int = 90) -> list:
        """
        Fetch recent Form 4 filings for a company from EDGAR.
        Returns list of InsiderTransaction objects.

        EDGAR submissions endpoint returns filing history including Form 4.
        """
        self._rate_limit()
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"

        try:
            resp = self.session.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            logger.error(f"Failed to fetch Form 4 data for CIK {cik}: {e}")
            return []

        # Parse recent filings
        filings = data.get("filings", {}).get("recent", {})
        if not filings:
            return []

        forms        = filings.get("form", [])
        filed_dates  = filings.get("filedAt", [])
        accessions   = filings.get("accessionNumber", [])

        cutoff = datetime.now() - timedelta(days=days_back)
        form4_accessions = []

        for form, filed, acc in zip(forms, filed_dates, accessions):
            if form not in ("4", "4/A"):
                continue
            try:
                filed_dt = datetime.fromisoformat(filed[:10])
            except (ValueError, TypeError):
                continue
            if filed_dt < cutoff:
                break   # EDGAR returns newest first, so we can stop early
            form4_accessions.append((acc, filed_dt))

        if not form4_accessions:
            logger.debug(f"No Form 4 filings in last {days_back} days for CIK {cik}")
            return []

        transactions = []
        for acc, filed_dt in form4_accessions[:20]:   # Cap at 20 filings per run
            txns = self._parse_form4(cik, acc, filed_dt)
            transactions.extend(txns)

        return transactions

    def _parse_form4(self, cik: str, accession: str, filed_dt: datetime) -> list:
        """
        Parse a single Form 4 filing.
        EDGAR provides XML for each filing — we use the index to get it.
        Simplified parser — extracts key transaction fields.
        """
        self._rate_limit()

        # Format accession number for URL
        acc_clean = accession.replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/{accession}-index.json"

        try:
            resp = self.session.get(url, timeout=20)
            if resp.status_code != 200:
                return []
            index = resp.json()
        except (requests.RequestException, json.JSONDecodeError):
            return []

        # Find the XML file in the filing
        xml_url = None
        for item in index.get("directory", {}).get("item", []):
            if item.get("name", "").endswith(".xml") and "form4" in item.get("name", "").lower():
                xml_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/{item['name']}"
                break

        if not xml_url:
            return []

        self._rate_limit()
        try:
            xml_resp = self.session.get(xml_url, timeout=20)
            xml_text = xml_resp.text
        except requests.RequestException:
            return []

        return self._parse_form4_xml(xml_text, filed_dt)

    def _parse_form4_xml(self, xml_text: str, filed_dt: datetime) -> list:
        """
        Lightweight XML parser for Form 4.
        Extracts: insider name, title, transaction code, shares, price, date.
        """
        transactions = []

        def extract(tag: str, text: str) -> Optional[str]:
            start = text.find(f"<{tag}>")
            end   = text.find(f"</{tag}>")
            if start == -1 or end == -1:
                return None
            return text[start + len(tag) + 2:end].strip()

        # Reporting person info
        insider_name  = extract("rptOwnerName",  xml_text) or "Unknown"
        insider_title = extract("officerTitle",  xml_text) or extract("relationship", xml_text) or ""

        # Parse each non-derivative transaction
        txn_start = 0
        while True:
            block_start = xml_text.find("<nonDerivativeTransaction>", txn_start)
            if block_start == -1:
                break
            block_end = xml_text.find("</nonDerivativeTransaction>", block_start)
            if block_end == -1:
                break
            block = xml_text[block_start:block_end]
            txn_start = block_end + 1

            try:
                code        = extract("transactionCode", block)
                shares_str  = extract("transactionShares", block)
                price_str   = extract("transactionPricePerShare", block)
                date_str    = extract("transactionDate", block)

                if not all([code, shares_str, price_str, date_str]):
                    continue

                shares = float(shares_str)
                price  = float(price_str)

                if shares <= 0 or price <= 0:
                    continue

                txn_dt = datetime.strptime(date_str, "%Y-%m-%d")
                total_value = shares * price
                is_buy = code in OPEN_MARKET_PURCHASE_CODES
                is_sale = code in OPEN_MARKET_SALE_CODES
                is_open_market = is_buy or is_sale

                if code in EXCLUDED_CODES:
                    continue   # Skip non-conviction transactions

                transactions.append(InsiderTransaction(
                    insider_name=insider_name,
                    insider_title=insider_title,
                    transaction_type="buy" if is_buy else "sell",
                    transaction_code=code,
                    shares=shares,
                    price_per_share=price,
                    total_value=total_value,
                    filed_date=filed_dt,
                    transaction_date=txn_dt,
                    is_open_market=is_open_market,
                    role_weight=self._get_role_weight(insider_title),
                ))

            except (ValueError, TypeError):
                continue

        return transactions

    def score(self, ticker: str, cik: str, current_price: Optional[float] = None) -> InsiderSignalResult:
        """
        Fetch and score all insider Form 4 activity for a ticker.
        Returns InsiderSignalResult with signal and supporting data.
        """
        notes    = []
        warnings = []

        transactions = self.fetch_form4_transactions(cik, days_back=90)

        # Filter to open-market only
        open_market = [t for t in transactions if t.is_open_market]
        buys  = [t for t in open_market if t.transaction_type == "buy"]
        sells = [t for t in open_market if t.transaction_type == "sell"]

        # Aggregate stats
        total_buy_value  = sum(t.total_value for t in buys)
        total_sell_value = sum(t.total_value for t in sells)
        unique_buyers    = len(set(t.insider_name for t in buys))

        buy_sell_ratio = (total_buy_value / total_sell_value
                          if total_sell_value > 0 else float(total_buy_value > 0))

        # Cluster detection — 2+ insiders buying within 30 days
        cluster_buy_detected = False
        cluster_buy_value    = 0.0
        if len(buys) >= self.CLUSTER_MIN_BUYERS:
            buy_dates = [t.transaction_date for t in buys]
            for i, d in enumerate(buy_dates):
                window = [b for b in buys
                          if abs((b.transaction_date - d).days) <= self.CLUSTER_WINDOW_DAYS]
                unique_in_window = set(b.insider_name for b in window)
                if len(unique_in_window) >= self.CLUSTER_MIN_BUYERS:
                    cluster_buy_detected = True
                    cluster_buy_value = sum(b.total_value for b in window)
                    notes.append(
                        f"🔥 CLUSTER BUY: {len(unique_in_window)} insiders bought "
                        f"${cluster_buy_value:,.0f} total within 30 days"
                    )
                    break

        # Largest single buy
        largest_buy = max(buys, key=lambda t: t.total_value) if buys else None
        largest_buy_value = largest_buy.total_value if largest_buy else 0.0
        largest_buyer_title = largest_buy.insider_title if largest_buy else ""

        if largest_buy_value >= self.STRONG_BUY_VALUE:
            notes.append(f"Large buy: {largest_buyer_title} purchased ${largest_buy_value:,.0f}")

        # Days since last buy
        days_since_buy = None
        if buys:
            most_recent_buy = max(buys, key=lambda t: t.transaction_date)
            days_since_buy = (datetime.now() - most_recent_buy.transaction_date).days

        # Price change since last buy (if we have current price)
        price_change_since_buy = None
        if buys and current_price and largest_buy:
            price_change_since_buy = (current_price - largest_buy.price_per_share) / largest_buy.price_per_share

        # ── Composite scoring ─────────────────────────────────────
        score = 0.0

        def _recency_weight(txn: "InsiderTransaction") -> float:
            """Form 4 signal edge decays after ~30 days."""
            days_ago = (datetime.now() - txn.transaction_date).days
            if days_ago <= 30:  return 1.00
            if days_ago <= 60:  return 0.65
            if days_ago <= 90:  return 0.35
            return 0.10

        # Cluster buy = strongest signal (apply recency to most recent transaction in cluster)
        if cluster_buy_detected:
            most_recent_cluster = max(buys, key=lambda t: t.transaction_date)
            score += 3.0 * _recency_weight(most_recent_cluster)
        elif unique_buyers >= 2:
            most_recent_multi = max(buys, key=lambda t: t.transaction_date)
            score += 1.5 * _recency_weight(most_recent_multi)

        # Large buy dollar value (weighted by role and recency)
        for buy in buys:
            rw = buy.role_weight * _recency_weight(buy)
            if buy.total_value >= self.STRONG_BUY_VALUE:
                score += 1.0 * rw
            elif buy.total_value >= self.MIN_BUY_VALUE:
                score += 0.3 * rw

        # Penalize heavy selling
        if total_sell_value > total_buy_value * 2:
            score -= 2.0
            warnings.append("Heavy insider selling outweighs buying — caution")

        # Buy/sell ratio bonus
        if buy_sell_ratio > 3.0:
            score += 1.0

        # Cap score
        score = max(-5.0, min(5.0, score))

        # ── Signal classification ─────────────────────────────────
        if score >= 3.5:
            signal = "strong_buy"
        elif score >= 2.0:
            signal = "buy"
        elif score >= -1.0:
            signal = "neutral"
        elif score >= -2.5:
            signal = "sell"
        else:
            signal = "strong_sell"

        if not buys:
            notes.append("No open-market insider purchases in last 90 days")
        if not sells:
            notes.append("No open-market insider sales in last 90 days")

        logger.info(
            f"[INSIDER] {ticker}: signal={signal}, score={score:.1f}, "
            f"buys={len(buys)}, unique_buyers={unique_buyers}, "
            f"cluster={cluster_buy_detected}"
        )

        return InsiderSignalResult(
            ticker=ticker,
            cik=cik,
            signal=signal,
            score=score,
            total_buys_90d=len(buys),
            total_sells_90d=len(sells),
            total_buy_value_90d=total_buy_value,
            total_sell_value_90d=total_sell_value,
            buy_sell_ratio=buy_sell_ratio,
            unique_buyers_90d=unique_buyers,
            cluster_buy_detected=cluster_buy_detected,
            cluster_buy_value=cluster_buy_value,
            largest_buy_value=largest_buy_value,
            largest_buyer_title=largest_buyer_title,
            days_since_last_buy=days_since_buy,
            price_change_since_buy=price_change_since_buy,
            transactions=open_market,
            notes=notes,
            warnings=warnings,
        )
