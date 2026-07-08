"""
analysis/fud_filter.py — Layer 3: FUD Filter.

Scores news items from 0.0 (pure FUD) to 1.0 (quality signal).

Quality signals (lean in):
  - SEC filings: 10-K, 10-Q, 8-K, Form 4
  - Earnings transcripts with specific guidance
  - Insider buying (Form 4)
  - Institutional 13-F changes

FUD signals (filter out):
  - High-velocity negative news with no new fundamental data
  - High emotion, no data (sentiment_magnitude > 0.8, no financial data)
  - Short-seller reports without verifiable sourcing
  - Macro fear headlines hitting quality companies indiscriminately

Usage:
    python -m analysis.fud_filter --ticker AAPL
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from loguru import logger

from config import config
from models.database import init_db, NewsItem, Company


# Source credibility scores — 0.0 to 1.0
SOURCE_CREDIBILITY = {
    "sec_filing":           1.00,   # Primary source — verifiable
    "earnings_transcript":  0.90,   # Management on record
    "form_4":               0.90,   # Insider buys = skin in game
    "13f":                  0.85,   # Institutional repositioning
    "reuters":              0.75,
    "bloomberg":            0.75,
    "wsj":                  0.70,
    "ft":                   0.70,
    "barrons":              0.65,
    "benzinga":             0.55,
    "seeking_alpha":        0.40,   # Mixed quality, often promotional
    "motley_fool":          0.35,
    "reddit":               0.10,
    "twitter":              0.10,
    "stocktwits":           0.05,
    "unknown":              0.25,   # Default for unrecognized sources
}

# Keywords that signal macro FUD (penalize when no fundamental data attached)
FUD_KEYWORDS = [
    "recession", "crash", "collapse", "catastrophic", "bubble", "crisis",
    "rate hike", "inflation fear", "fed fears", "market selloff", "panic",
    "worst since", "black swan", "meltdown", "contagion", "death cross",
]

# Keywords that indicate quality fundamental information
QUALITY_KEYWORDS = [
    "earnings", "revenue", "guidance", "ebitda", "free cash flow", "margin",
    "10-k", "10-q", "8-k", "form 4", "insider", "buyback", "dividend",
    "acquisition", "contract", "partnership", "fda approval", "patent",
]


@dataclass
class FUDScore:
    ticker: str
    source: str
    raw_score: float            # 0.0 to 1.0
    credibility: float
    has_financial_data: bool
    is_sec_filing: bool
    sentiment_magnitude: float
    fud_keywords_found: list
    quality_keywords_found: list
    reasoning: str


def score_article(
    source: str,
    headline: str,
    summary: str = "",
    sentiment_score: float = 0.0,
    is_sec_filing: bool = False,
    filing_type: str = None,
    days_since_filing: int = 99,
) -> float:
    """
    Score a single article from 0.0 (pure FUD) to 1.0 (quality signal).

    Args:
        source: Source identifier (key in SOURCE_CREDIBILITY)
        headline: Article headline
        summary: Article body/summary
        sentiment_score: Sentiment from -1.0 (very negative) to 1.0 (very positive)
        is_sec_filing: True if sourced directly from SEC EDGAR
        filing_type: "10-K", "10-Q", "8-K", "4" (Form 4), etc.
        days_since_filing: Days since last SEC filing — recency boosts quality score

    Returns:
        float: 0.0 to 1.0 quality score
    """
    text = (headline + " " + summary).lower()

    credibility = SOURCE_CREDIBILITY.get(source.lower(), SOURCE_CREDIBILITY["unknown"])

    # SEC filings are always quality — return immediately
    if is_sec_filing or source == "sec_filing":
        return min(1.0, credibility + 0.05)

    # Detect financial data content
    quality_hits = [kw for kw in QUALITY_KEYWORDS if kw in text]
    fud_hits = [kw for kw in FUD_KEYWORDS if kw in text]
    has_financial_data = len(quality_hits) >= 2

    sentiment_magnitude = abs(sentiment_score)

    # Base score from credibility
    score = credibility

    # Boost: article contains financial data
    if has_financial_data:
        score += 0.20

    # Boost: recent SEC filing context (e.g. post-earnings article)
    if days_since_filing <= 3:
        score += 0.10

    # Penalty: high emotion with no data = FUD
    if sentiment_magnitude > 0.75 and not has_financial_data:
        score *= 0.25

    # Penalty: FUD keywords without quality data
    if fud_hits and not quality_hits:
        penalty = min(0.30, len(fud_hits) * 0.08)
        score -= penalty

    return max(0.0, min(1.0, score))


def aggregate_fud_score(
    db_session,
    company_id: int,
    days: int = 7,
) -> dict:
    """
    Aggregate FUD scores for a company over the last N days.

    Returns dict with:
        score: float — weighted average quality score
        n_articles: int
        n_sec_filings: int
        n_insider_buys: int
        breakdown: list of individual FUDScore objects
    """
    cutoff = datetime.utcnow() - timedelta(days=days)

    items = (
        db_session.query(NewsItem)
        .filter(
            NewsItem.company_id == company_id,
            NewsItem.published_at >= cutoff,
        )
        .order_by(NewsItem.published_at.desc())
        .all()
    )

    if not items:
        # No news = neutral — don't penalize silence
        return {
            "score": 0.65,  # Slight positive default — absence of FUD is okay
            "n_articles": 0,
            "n_sec_filings": 0,
            "n_insider_buys": 0,
            "breakdown": [],
        }

    scores = []
    weights = []
    n_sec = 0
    n_insider = 0

    for item in items:
        s = item.fud_score
        if s is None:
            # Recompute if not pre-scored
            s = score_article(
                source=item.source or "unknown",
                headline=item.headline or "",
                summary=item.summary or "",
                sentiment_score=item.sentiment_score or 0.0,
                is_sec_filing=item.is_sec_filing or False,
                filing_type=item.filing_type,
            )

        if item.is_sec_filing:
            n_sec += 1
        if item.filing_type == "4":  # Form 4 = insider trade
            n_insider += 1

        # More recent articles weighted higher
        age_days = (datetime.utcnow() - item.published_at).days if item.published_at else days
        recency_weight = max(0.1, 1.0 - (age_days / days) * 0.5)

        scores.append(s)
        weights.append(recency_weight)

    total_weight = sum(weights)
    weighted_avg = sum(s * w for s, w in zip(scores, weights)) / total_weight if total_weight > 0 else 0.5

    # Insider buying is a strong quality signal — boost the aggregate
    if n_insider > 0:
        weighted_avg = min(1.0, weighted_avg + 0.05 * n_insider)

    logger.debug(
        f"FUD aggregate: {len(items)} articles, score={weighted_avg:.2f}, "
        f"SEC filings={n_sec}, insider={n_insider}"
    )

    return {
        "score": weighted_avg,
        "n_articles": len(items),
        "n_sec_filings": n_sec,
        "n_insider_buys": n_insider,
        "breakdown": scores,
    }


class FUDFilter:
    """
    Layer 3 orchestrator. Scores and persists FUD analysis for all watchlist tickers.
    Also provides the interface used by the decision engine.
    """

    def __init__(self, db_session_factory):
        self.Session = db_session_factory

    def score_ticker(self, ticker: str, days: int = 7) -> dict:
        """
        Get aggregated news quality score for a ticker.
        This is the primary interface for the decision engine.
        """
        with self.Session() as session:
            company = session.query(Company).filter_by(ticker=ticker).first()
            if not company:
                logger.warning(f"No company record for {ticker}")
                return {"score": 0.5, "n_articles": 0, "n_sec_filings": 0, "n_insider_buys": 0}

            result = aggregate_fud_score(session, company.id, days=days)
            logger.info(
                f"[L3] {ticker}: news quality score={result['score']:.2f} "
                f"({result['n_articles']} articles, {result['n_sec_filings']} SEC filings)"
            )
            return result

    def score_and_persist(self, ticker: str) -> int:
        """
        Re-score all unscored news items for a ticker and persist to DB.
        Returns number of items scored.
        """
        with self.Session() as session:
            company = session.query(Company).filter_by(ticker=ticker).first()
            if not company:
                return 0

            unscored = (
                session.query(NewsItem)
                .filter_by(company_id=company.id, fud_score=None)
                .all()
            )

            for item in unscored:
                item.fud_score = score_article(
                    source=item.source or "unknown",
                    headline=item.headline or "",
                    summary=item.summary or "",
                    sentiment_score=item.sentiment_score or 0.0,
                    is_sec_filing=item.is_sec_filing or False,
                    filing_type=item.filing_type,
                )

            session.commit()
            logger.info(f"[L3] Scored {len(unscored)} unscored news items for {ticker}")
            return len(unscored)


# ── CLI ───────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    parser = argparse.ArgumentParser(description="Layer 3: FUD Filter")
    parser.add_argument("--ticker", type=str, required=True)
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()

    _, Session = init_db(config.database.url)
    f = FUDFilter(db_session_factory=Session)
    result = f.score_ticker(args.ticker.upper(), days=args.days)
    print(f"\n{args.ticker.upper()} news quality score: {result['score']:.2f}")
    print(f"  Articles analysed : {result['n_articles']}")
    print(f"  SEC filings       : {result['n_sec_filings']}")
    print(f"  Insider buys      : {result['n_insider_buys']}")
