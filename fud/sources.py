"""
fud/sources.py — News Source Credibility Registry.

Every news source gets a credibility score (0.0 to 1.0) based on:
  - Primary source reliability (SEC filing = 1.0, Twitter = 0.1)
  - Historical accuracy
  - Incentive alignment (short sellers have skin in the game, but bias too)
  - Data vs opinion ratio

FUD patterns to detect:
  - High emotional language + no financial data = FUD
  - Source with financial interest in stock going down
  - Coordinated social media volume spike without institutional confirmation
  - Macro fear headlines applied indiscriminately to quality companies

Quality signal patterns:
  - SEC primary filings (10-K, 10-Q, 8-K, Form 4)
  - Earnings guidance upgrades
  - Insider buying concurrent with negative news = strong quality signal
  - Institutional 13-F additions
  - Revenue/earnings beats with raised guidance
"""

from dataclasses import dataclass


@dataclass
class SourceProfile:
    name: str
    credibility: float          # 0.0 to 1.0
    is_primary_source: bool     # SEC, company IR = True
    has_financial_data: bool    # Contains verifiable numbers
    bias_direction: str         # "bullish", "bearish", "neutral"
    notes: str


# ── Source credibility registry ───────────────────────────────

SOURCE_REGISTRY = {
    # Primary sources — highest credibility
    "sec_filing":           SourceProfile("SEC Filing",           1.00, True,  True,  "neutral",  "Direct regulatory filing"),
    "sec_edgar":            SourceProfile("SEC EDGAR",            1.00, True,  True,  "neutral",  "Primary regulatory data"),
    "earnings_transcript":  SourceProfile("Earnings Transcript",  0.95, True,  True,  "neutral",  "Direct management speech"),
    "company_ir":           SourceProfile("Company IR",           0.90, True,  True,  "bullish",  "Company-issued, bullish bias"),
    "form4":                SourceProfile("Form 4 (Insider)",     0.95, True,  True,  "neutral",  "Legal insider disclosure"),
    "13f":                  SourceProfile("13-F Institutional",   0.90, True,  True,  "neutral",  "Institutional position disclosure"),
    "proxy_filing":         SourceProfile("DEF 14A Proxy",        0.90, True,  True,  "neutral",  "Shareholder governance filing"),

    # Tier 1 financial media — high credibility
    "reuters":              SourceProfile("Reuters",              0.82, False, True,  "neutral",  "Wire service, fact-focused"),
    "bloomberg":            SourceProfile("Bloomberg",            0.80, False, True,  "neutral",  "Professional financial media"),
    "wsj":                  SourceProfile("Wall Street Journal",  0.80, False, True,  "neutral",  "Professional financial media"),
    "financial_times":      SourceProfile("Financial Times",      0.80, False, True,  "neutral",  "Professional financial media"),
    "ap":                   SourceProfile("Associated Press",     0.78, False, True,  "neutral",  "Wire service"),
    "barrons":              SourceProfile("Barron's",             0.75, False, True,  "neutral",  "Professional investment media"),

    # Tier 2 financial media — moderate credibility
    "cnbc":                 SourceProfile("CNBC",                 0.65, False, False, "bullish",  "TV media, sometimes sensational"),
    "marketwatch":          SourceProfile("MarketWatch",          0.65, False, True,  "neutral",  "Moderate quality"),
    "yahoo_finance":        SourceProfile("Yahoo Finance",        0.60, False, True,  "neutral",  "Aggregator, variable quality"),
    "benzinga":             SourceProfile("Benzinga",             0.55, False, False, "neutral",  "Mixed quality, some clickbait"),
    "motley_fool":          SourceProfile("Motley Fool",          0.50, False, False, "bullish",  "Retail-focused, sponsored content"),
    "thestreet":            SourceProfile("The Street",           0.50, False, False, "neutral",  "Variable quality"),

    # Analyst research — moderate-high, but watch for conflicts
    "analyst_upgrade":      SourceProfile("Analyst Upgrade",     0.70, False, True,  "bullish",  "Lagging indicator, conflict risk"),
    "analyst_downgrade":    SourceProfile("Analyst Downgrade",   0.70, False, True,  "bearish",  "Lagging indicator, conflict risk"),
    "analyst_initiation":   SourceProfile("Analyst Initiation",  0.65, False, True,  "neutral",  "New coverage, watch conflict"),

    # Short seller research — high conviction but strong bear bias
    "short_seller_report":  SourceProfile("Short Seller Report",  0.55, False, True,  "bearish",  "Financially motivated, verify independently"),
    "citron":               SourceProfile("Citron Research",      0.50, False, True,  "bearish",  "Known short-seller, verify all claims"),
    "hindenburg":           SourceProfile("Hindenburg Research",  0.55, False, True,  "bearish",  "Short-seller, some accurate, some not"),

    # Low credibility — social/retail
    "seeking_alpha":        SourceProfile("Seeking Alpha",        0.40, False, False, "neutral",  "User-generated, variable quality"),
    "reddit_wsb":           SourceProfile("Reddit WallStreetBets",0.10, False, False, "bullish",  "Retail speculation, momentum-driven"),
    "reddit":               SourceProfile("Reddit",               0.15, False, False, "neutral",  "Unverified user content"),
    "twitter":              SourceProfile("Twitter/X",            0.10, False, False, "neutral",  "Unverified, high noise"),
    "stocktwits":           SourceProfile("StockTwits",           0.10, False, False, "neutral",  "Retail sentiment only"),
    "youtube":              SourceProfile("YouTube",              0.10, False, False, "neutral",  "Entertainment, not analysis"),
    "tiktok":               SourceProfile("TikTok",               0.05, False, False, "bullish",  "Entertainment only, pump risk"),

    # Default for unknown sources
    "unknown":              SourceProfile("Unknown",              0.25, False, False, "neutral",  "Unclassified source"),
}


# ── FUD keyword patterns ──────────────────────────────────────
# High emotional charge, low data content = FUD signal

FUD_KEYWORDS = {
    # Catastrophizing without data
    "catastrophic", "collapse", "implode", "crater", "disaster",
    "bankruptcy", "fraud", "scam", "ponzi", "manipulation",
    "bubble", "crash", "meltdown", "death spiral", "terminal",
    "worthless", "zero", "bankrupt",

    # Macro fear applied broadly
    "recession incoming", "market crash", "everything bubble",
    "hyperinflation", "dollar collapse", "systemic risk",

    # Short seller language
    "massive fraud", "going to zero", "criminal", "SEC investigation",
    "accounting irregularities", "channel stuffing",

    # Social media pump/dump
    "to the moon", "100x", "life changing", "buy now before",
    "squeeze incoming", "short squeeze", "gamma squeeze",
    "diamond hands", "paper hands", "apes together",
}

# Quality signal keywords — article likely contains real information
QUALITY_KEYWORDS = {
    # Financial metrics
    "revenue", "earnings", "guidance", "margin", "ebitda",
    "free cash flow", "return on", "operating income",
    "gross profit", "net income", "eps", "diluted",

    # Management actions
    "acquisition", "partnership", "contract", "regulatory approval",
    "fda approval", "patent", "buyback", "dividend increase",

    # Institutional activity
    "institutional", "13f", "position", "stake", "holding",
    "berkshire", "blackrock", "vanguard",

    # SEC language
    "10-k", "10-q", "8-k", "proxy", "form 4", "sec filing",
    "annual report", "quarterly results",

    # Analyst data
    "consensus estimate", "eps estimate", "price target",
    "raised guidance", "beat expectations", "above consensus",
}


def get_source_profile(source_key: str) -> SourceProfile:
    """Look up source profile, default to 'unknown' if not found."""
    key = source_key.lower().replace(" ", "_").replace("-", "_")
    return SOURCE_REGISTRY.get(key, SOURCE_REGISTRY["unknown"])


def classify_source_from_url(url: str) -> str:
    """
    Attempt to classify a source from its URL domain.
    Used when we have a URL but no explicit source label.
    """
    if not url:
        return "unknown"

    url_lower = url.lower()
    domain_map = {
        "sec.gov":          "sec_filing",
        "reuters.com":      "reuters",
        "bloomberg.com":    "bloomberg",
        "wsj.com":          "wsj",
        "ft.com":           "financial_times",
        "cnbc.com":         "cnbc",
        "marketwatch.com":  "marketwatch",
        "barrons.com":      "barrons",
        "yahoo.com":        "yahoo_finance",
        "finance.yahoo":    "yahoo_finance",
        "benzinga.com":     "benzinga",
        "fool.com":         "motley_fool",
        "thestreet.com":    "thestreet",
        "seekingalpha.com": "seeking_alpha",
        "reddit.com/r/wallstreetbets": "reddit_wsb",
        "reddit.com":       "reddit",
        "twitter.com":      "twitter",
        "x.com":            "twitter",
        "stocktwits.com":   "stocktwits",
        "youtube.com":      "youtube",
        "tiktok.com":       "tiktok",
        "hindenburgresearch": "hindenburg",
        "citronresearch":   "citron",
    }

    for domain, key in domain_map.items():
        if domain in url_lower:
            return key

    return "unknown"
