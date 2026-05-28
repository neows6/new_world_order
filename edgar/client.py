"""
edgar/client.py — SEC EDGAR API client.

Uses the free EDGAR data API:
  https://data.sec.gov/api/xbrl/companyfacts/{CIK}.json

Rate limit: SEC asks for <= 10 req/sec. We use 5 req/sec to be polite.
User-Agent header is required by SEC — set EDGAR_USER_AGENT in .env.
"""

import time
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from loguru import logger

from config import config


# XBRL concept mappings — SEC uses US-GAAP taxonomy names
# These map to the human-readable metrics we care about
CONCEPT_MAP = {
    # Income statement
    "revenue":               ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"],
    "gross_profit":          ["GrossProfit", "GrossProfitLoss"],
    "operating_income":      ["OperatingIncomeLoss"],
    "net_income":            ["NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"],
    "eps_diluted":           ["EarningsPerShareDiluted"],
    "depreciation":          ["DepreciationDepletionAndAmortization", "DepreciationAndAmortization", "Depreciation", "DepreciationAmortizationAndAccretionNet", "AmortizationOfIntangibleAssets"],

    # Balance sheet
    "total_assets":          ["Assets"],
    "total_liabilities":     ["Liabilities"],
    "total_equity":          ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "cash":                  ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsAndShortTermInvestments"],
    "total_debt":            ["LongTermDebt", "LongTermDebtAndCapitalLeaseObligations"],

    # Cash flow
    "operating_cash_flow":   ["NetCashProvidedByUsedInOperatingActivities"],
    "capex":                 ["PaymentsToAcquirePropertyPlantAndEquipment", "PropertyPlantAndEquipmentAdditions", "PurchasesOfPropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets", "PaymentsForCapitalImprovements", "PaymentsToAcquireRealEstateHeldForInvestment"],
    "shares_outstanding":    ["CommonStockSharesOutstanding"],
}


class EdgarClient:
    """
    Fetches and parses EDGAR company facts into structured fundamentals.
    Caches raw JSON locally to avoid hammering SEC servers.
    """

    def __init__(self):
        from utils.ssl_context import make_requests_session
        self.base_url = config.edgar.base_url
        self.session = make_requests_session()
        self.session.headers.update({
            "User-Agent": config.edgar.user_agent,
            "Accept-Encoding": "gzip, deflate",
        })
        self.cache_dir = Path(config.edgar.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._last_request_time = 0.0
        self._min_interval = 1.0 / config.edgar.requests_per_second

    def _rate_limit(self):
        """Enforce rate limit between requests."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_time = time.time()

    def get_cik_for_ticker(self, ticker: str) -> Optional[str]:
        """
        Look up SEC CIK number for a ticker symbol.
        Uses the EDGAR company tickers JSON (updated daily by SEC).
        """
        cache_file = self.cache_dir / "company_tickers.json"

        # Refresh if older than 24 hours
        if not cache_file.exists() or (time.time() - cache_file.stat().st_mtime > 86400):
            self._rate_limit()
            try:
                resp = self.session.get(
                    "https://www.sec.gov/files/company_tickers.json",
                    timeout=30
                )
                resp.raise_for_status()
                cache_file.write_text(resp.text)
                logger.info("Refreshed EDGAR company tickers index")
            except requests.RequestException as e:
                logger.error(f"Failed to fetch ticker index: {e}")
                if not cache_file.exists():
                    return None

        data = json.loads(cache_file.read_text())
        ticker_upper = ticker.upper()

        for entry in data.values():
            if entry.get("ticker", "").upper() == ticker_upper:
                # CIK must be zero-padded to 10 digits for API calls
                return str(entry["cik_str"]).zfill(10)

        logger.warning(f"No CIK found for ticker: {ticker}")
        return None

    def get_company_facts(self, cik: str) -> Optional[dict]:
        """
        Fetch full XBRL company facts from EDGAR.
        Returns raw JSON dict. Cached per CIK per day.
        """
        cache_file = self.cache_dir / f"facts_{cik}.json"

        # Use cache if fresh (< 24 hours)
        if cache_file.exists() and (time.time() - cache_file.stat().st_mtime < 86400):
            logger.debug(f"Using cached facts for CIK {cik}")
            return json.loads(cache_file.read_text())

        url = f"{self.base_url}/api/xbrl/companyfacts/CIK{cik}.json"
        self._rate_limit()

        try:
            resp = self.session.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            cache_file.write_text(json.dumps(data))
            logger.info(f"Fetched EDGAR facts for CIK {cik}")
            return data
        except requests.HTTPError as e:
            if e.response.status_code == 404:
                logger.warning(f"No EDGAR data for CIK {cik} (company may not file with SEC)")
            else:
                logger.error(f"EDGAR HTTP error for CIK {cik}: {e}")
            return None
        except requests.RequestException as e:
            logger.error(f"EDGAR request failed for CIK {cik}: {e}")
            return None

    def _extract_concept(self, facts: dict, concept_names: list) -> list:
        """
        Extract time-series values for a concept from EDGAR facts.
        Tries each concept name in order until one is found.
        Returns list of dicts: {end, val, accn, form, filed}
        """
        us_gaap = facts.get("facts", {}).get("us-gaap", {})

        for concept in concept_names:
            if concept in us_gaap:
                units = us_gaap[concept].get("units", {})
                # Most financial metrics are in USD
                for unit_key in ["USD", "USD/shares", "shares", "pure"]:
                    if unit_key in units:
                        return units[unit_key]
        return []

    def _extract_concept_merged(self, facts: dict, concept_names: list) -> dict:
        """
        Extract and merge annual values from multiple concept names.
        Earlier-listed concepts take priority — fallback concepts only fill missing years.
        Returns {fiscal_year: value} dict.
        """
        us_gaap = facts.get("facts", {}).get("us-gaap", {})
        merged_annual = {}

        for concept in concept_names:
            if concept in us_gaap:
                units = us_gaap[concept].get("units", {})
                for unit_key in ["USD", "USD/shares", "shares", "pure"]:
                    if unit_key in units:
                        concept_annual = self._get_annual_values(units[unit_key])
                        # Only add years not already covered by a higher-priority concept
                        for year, val in concept_annual.items():
                            if year not in merged_annual:
                                merged_annual[year] = val
                        break

        return merged_annual

    def _get_annual_values(self, records: list) -> dict:
        """
        Filter to annual 10-K filings and return {fiscal_year: value} dict.
        Prefers most recently filed value per year to handle amendments.
        """
        annual = {}
        for r in records:
            if r.get("form") not in ("10-K", "10-K/A"):
                continue
            try:
                year = datetime.strptime(r["end"], "%Y-%m-%d").year
                filed = datetime.strptime(r["filed"], "%Y-%m-%d")
            except (KeyError, ValueError):
                continue

            # Keep most recently filed version for each year
            if year not in annual or filed > annual[year]["filed"]:
                annual[year] = {
                    "val": r["val"],
                    "filed": filed,
                    "accn": r.get("accn", ""),
                    "end": r["end"],
                }

        return {yr: v["val"] for yr, v in annual.items()}

    def _get_quarterly_values(self, records: list) -> list:
        """
        Filter to 10-Q filings and return list of quarterly snapshots.
        """
        quarterly = {}
        for r in records:
            if r.get("form") not in ("10-Q", "10-Q/A"):
                continue
            try:
                end_date = datetime.strptime(r["end"], "%Y-%m-%d")
                filed = datetime.strptime(r["filed"], "%Y-%m-%d")
            except (KeyError, ValueError):
                continue

            key = r["end"]
            if key not in quarterly or filed > quarterly[key]["filed"]:
                quarterly[key] = {
                    "val": r["val"],
                    "filed": filed,
                    "accn": r.get("accn", ""),
                    "end": end_date,
                }

        return sorted(quarterly.values(), key=lambda x: x["end"])

    def get_fundamentals(self, ticker: str) -> Optional[dict]:
        """
        High-level method: fetch and parse all fundamentals for a ticker.
        Returns structured dict ready for database insertion.
        """
        cik = self.get_cik_for_ticker(ticker)
        if not cik:
            return None

        facts = self.get_company_facts(cik)
        if not facts:
            return None

        entity_name = facts.get("entityName", ticker)
        logger.info(f"Parsing fundamentals for {ticker} ({entity_name})")

        # Extract all concepts as annual series
        # Use merged extraction for multi-fallback fields (capex, gross_profit)
        MERGED_FIELDS = {"capex", "gross_profit", "depreciation", "revenue", "total_equity", "total_debt", "cash", "net_income", "operating_income"}
        extracted = {}
        for field_name, concept_names in CONCEPT_MAP.items():
            if field_name in MERGED_FIELDS and len(concept_names) > 1:
                extracted[field_name] = self._extract_concept_merged(facts, concept_names)
            else:
                records = self._extract_concept(facts, concept_names)
                extracted[field_name] = self._get_annual_values(records)

        # Find years with at least revenue data
        years = sorted(extracted.get("revenue", {}).keys(), reverse=True)

        annual_records = []
        for year in years:
            def g(field): return extracted.get(field, {}).get(year)

            revenue = g("revenue")
            gross_profit = g("gross_profit")
            net_income = g("net_income")
            ocf = g("operating_cash_flow")
            capex = g("capex")
            dep = g("depreciation")
            cash = g("cash")
            debt = g("total_debt")
            equity = g("total_equity")
            assets = g("total_assets")

            # Derived / first principles metrics
            gross_margin = (gross_profit / revenue) if (revenue is not None and gross_profit is not None and revenue != 0) else None
            # capex falls back to 0 for capital-light sectors (banks, insurers) where
            # EDGAR doesn't carry a PaymentsToAcquirePPE tag, or when the filing year
            # predates the concept being used.  OCF-only FCF is labelled approximate.
            _capex = capex if capex is not None else 0
            free_cash_flow = (ocf - _capex) if ocf is not None else None
            owner_earnings = (net_income + dep - _capex) if (net_income is not None and dep is not None) else None
            net_debt = (debt - cash) if (debt is not None and cash is not None) else None

            # NOPAT = Operating Income * (1 - effective tax rate)
            # We estimate using net income / revenue as a simplification
            # A more precise version requires tax rate from the filing
            op_income = g("operating_income")
            nopat = (op_income * 0.79) if op_income is not None else None  # Assumes ~21% corporate tax

            # Invested capital = total equity + net debt
            invested_capital = (equity + net_debt) if (equity is not None and net_debt is not None) else None
            roic = (nopat / invested_capital) if (nopat is not None and invested_capital is not None and invested_capital != 0) else None

            ebitda = (op_income + dep) if (op_income is not None and dep is not None) else None
            net_debt_to_ebitda = (net_debt / ebitda) if (net_debt is not None and ebitda is not None and ebitda != 0) else None

            annual_records.append({
                "ticker": ticker,
                "cik": cik,
                "entity_name": entity_name,
                "fiscal_year": year,
                "fiscal_quarter": 0,  # 0 = annual
                "revenue": revenue,
                "gross_profit": gross_profit,
                "gross_margin": gross_margin,
                "operating_income": op_income,
                "net_income": net_income,
                "eps_diluted": g("eps_diluted"),
                "total_assets": assets,
                "total_liabilities": g("total_liabilities"),
                "total_equity": equity,
                "cash_and_equivalents": cash,
                "total_debt": debt,
                "net_debt": net_debt,
                "operating_cash_flow": ocf,
                "capex": capex,
                "free_cash_flow": free_cash_flow,
                "depreciation_amortization": dep,
                "owner_earnings": owner_earnings,
                "roic": roic,
                "invested_capital": invested_capital,
                "nopat": nopat,
                "net_debt_to_ebitda": net_debt_to_ebitda,
                "ebitda": ebitda,
            })

        return {
            "ticker": ticker,
            "cik": cik,
            "entity_name": entity_name,
            "annual": annual_records,
        }

    def get_recent_filings(self, cik: str, filing_types: list = None) -> list:
        """
        Fetch recent filing metadata for a CIK.
        Used by the news/signal layer to detect new 10-K, 10-Q, 8-K filings.
        """
        if filing_types is None:
            filing_types = ["10-K", "10-Q", "8-K", "4"]  # Form 4 = insider trading

        url = f"{self.base_url}/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=&dateb=&owner=include&count=40&search_text=&output=atom"
        self._rate_limit()

        try:
            resp = self.session.get(url, timeout=30)
            resp.raise_for_status()
            # Parse Atom feed — basic string scan for filing types
            filings = []
            for line in resp.text.split("\n"):
                for ft in filing_types:
                    if f">{ft}<" in line or f"type={ft}" in line:
                        filings.append({"type": ft, "raw": line.strip()})
            return filings
        except requests.RequestException as e:
            logger.error(f"Failed to fetch filings for CIK {cik}: {e}")
            return []
