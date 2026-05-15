"""
fud/classifier.py — FUD vs Quality Signal Classifier.

Scores each news item on a spectrum:
  0.0 = Pure FUD (emotional, no data, bearish bias, low-credibility source)
  1.0 = High quality signal (primary source, financial data, verifiable)

The FUD score is NOT just sentiment. A negative article can be high quality
(e.g., a Reuters report on an earnings miss with actual numbers).
A positive article can be low quality (Reddit pump post).

Scoring dimensions:
  1. Source credibility        (40% weight)
  2. Financial data presence   (25% weight)
  3. Sentiment × credibility   (15% weight)
  4. FUD/quality keyword match (10% weight)
  5. Recency of fundamentals   (10% weight)
     — Article published near earnings = more likely to be quality

Output:
  fud_score: float             0.0 to 1.0
  is_quality_signal: bool      fud_score > 0.60
  sentiment: float             -1.0 to 1.0
  reasoning: list[str]         Why it scored this way
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from loguru import logger

from fud.sources import (
    get_source_profile, classify_source_from_url,
    FUD_KEYWORDS, QUALITY_KEYWORDS, SourceProfile
)


# ── Sentiment word banks ──────────────────────────────────────
# Simplified lexicon — production would use FinBERT or similar

POSITIVE_WORDS = {
    "beat", "exceeded", "raised", "growth", "record", "strong",
    "profitable", "improved", "outperformed", "acceleration",
    "momentum", "expansion", "upgrade", "bullish", "opportunity",
    "innovative", "leading", "dominant", "advantage", "milestone",
    "approval", "partnership", "acquisition", "dividend",
}

NEGATIVE_WORDS = {
    "miss", "missed", "below", "declined", "loss", "weak",
    "disappointing", "lowered", "reduced", "cut", "downgrade",
    "concern", "risk", "investigation", "lawsuit", "recall",
    "delay", "shortfall", "headwind", "pressure", "uncertainty",
    "warning", "caution", "bearish", "debt", "layoff",
}

# Intensifiers that amplify sentiment (positive or negative)
INTENSIFIERS = {
    "massive", "huge", "enormous", "record", "unprecedented",
    "shocking", "catastrophic", "explosive", "dramatic",
    "significant", "major", "critical",
}


@dataclass
class ArticleScore:
    # Input
    headline: str
    source: str
    url: Optional[str]
    published_at: Optional[datetime]
    ticker: str

    # Scores
    fud_score: float            # 0.0 (FUD) to 1.0 (quality)
    sentiment: float            # -1.0 to 1.0
    source_credibility: float   # Raw source credibility
    has_financial_data: bool
    is_primary_source: bool

    # Classification
    is_quality_signal: bool     # fud_score > 0.60
    is_fud: bool                # fud_score < 0.35
    is_neutral: bool            # 0.35 <= fud_score <= 0.60

    # Detail
    fud_keywords_found: list
    quality_keywords_found: list
    reasoning: list             # Why it scored this way

    # Metadata
    source_profile: SourceProfile
    scored_at: datetime
    filing_type: Optional[str] = None


class FUDClassifier:
    """
    Scores individual news articles on the FUD vs Quality spectrum.
    Designed to run on article headlines + summaries.
    Does NOT require full article text — works on metadata alone.
    """

    QUALITY_THRESHOLD = 0.60    # Above this = quality signal
    FUD_THRESHOLD     = 0.35    # Below this = FUD

    # Weight of each scoring dimension
    WEIGHTS = {
        "source_credibility":    0.40,
        "financial_data":        0.25,
        "sentiment_credibility": 0.15,
        "keyword_match":         0.10,
        "recency_context":       0.10,
    }

    def _extract_keywords(self, text: str) -> tuple:
        """Find FUD and quality keywords in text."""
        text_lower = text.lower()
        words = set(re.findall(r'\b\w+\b', text_lower))
        bigrams = set()
        word_list = text_lower.split()
        for i in range(len(word_list) - 1):
            bigrams.add(f"{word_list[i]} {word_list[i+1]}")

        all_tokens = words | bigrams

        fud_found     = [k for k in FUD_KEYWORDS     if k in all_tokens or k in text_lower]
        quality_found = [k for k in QUALITY_KEYWORDS if k in all_tokens or k in text_lower]

        return fud_found, quality_found

    def _compute_sentiment(self, text: str) -> float:
        """
        Simple lexicon-based sentiment score.
        Returns -1.0 (very negative) to +1.0 (very positive).
        Production upgrade: replace with FinBERT for 90%+ accuracy.
        """
        text_lower = text.lower()
        words = re.findall(r'\b\w+\b', text_lower)

        pos_count = sum(1 for w in words if w in POSITIVE_WORDS)
        neg_count = sum(1 for w in words if w in NEGATIVE_WORDS)
        int_count = sum(1 for w in words if w in INTENSIFIERS)

        total = pos_count + neg_count
        if total == 0:
            return 0.0

        raw = (pos_count - neg_count) / total

        # Intensifiers amplify sentiment (up to 30% boost)
        intensity_boost = min(0.30, int_count * 0.05)
        if raw > 0:
            return min(1.0, raw + intensity_boost)
        elif raw < 0:
            return max(-1.0, raw - intensity_boost)
        return 0.0

    def _score_source(self, source_key: str, url: Optional[str]) -> tuple:
        """Get source credibility. Try URL classification if key is unknown."""
        profile = get_source_profile(source_key)

        if profile.credibility == 0.25 and url:  # Got the default "unknown"
            url_key = classify_source_from_url(url)
            if url_key != "unknown":
                profile = get_source_profile(url_key)

        return profile.credibility, profile

    def _score_financial_data(self, text: str, quality_keywords: list) -> float:
        """
        Score 0.0-1.0 based on how much verifiable financial data is present.
        Numbers + financial terms = higher score.
        """
        # Look for numerical financial data (%, $, basis points, etc.)
        has_pct    = bool(re.search(r'\d+\.?\d*\s*%', text))
        has_dollar = bool(re.search(r'\$[\d,]+', text))
        has_eps    = bool(re.search(r'eps|earnings per share|\$\d+\.\d{2}', text.lower()))
        has_nums   = bool(re.search(r'\b\d{1,3}[,\d]*(?:\.\d+)?\s*(?:billion|million|thousand|b|m)\b', text.lower()))

        data_score = 0.0
        if has_pct:    data_score += 0.25
        if has_dollar: data_score += 0.25
        if has_eps:    data_score += 0.25
        if has_nums:   data_score += 0.15

        # Quality keywords also indicate data presence
        data_score += min(0.20, len(quality_keywords) * 0.04)

        return min(1.0, data_score)

    def _score_recency_context(self, published_at: Optional[datetime], ticker: str) -> float:
        """
        Score based on when the article was published relative to earnings.
        Articles published right after earnings are more likely to be quality.
        Simplified — full version would check actual earnings calendar.
        """
        if published_at is None:
            return 0.5    # No date = neutral

        now = datetime.now()
        days_old = (now - published_at).days

        # Fresh articles are more likely to be responding to real news
        if days_old <= 1:
            return 0.75    # Very fresh — likely event-driven
        elif days_old <= 7:
            return 0.65
        elif days_old <= 30:
            return 0.50
        else:
            return 0.30    # Old article being recycled = lower quality

    def score(
        self,
        headline: str,
        source: str,
        ticker: str,
        url: Optional[str] = None,
        summary: Optional[str] = None,
        published_at: Optional[datetime] = None,
        is_sec_filing: bool = False,
        filing_type: Optional[str] = None,
    ) -> ArticleScore:
        """
        Score a single news article.
        headline + source are required. Others improve accuracy.
        """
        reasoning = []
        full_text = f"{headline} {summary or ''}"

        # ── Instant overrides ──────────────────────────────────
        # SEC filings are always quality — no need to score further
        if is_sec_filing or source in ("sec_filing", "sec_edgar", "form4", "13f"):
            filing_label = filing_type or "SEC filing"
            reasoning.append(f"Primary SEC source ({filing_label}) — automatic quality signal")
            credibility, profile = self._score_source(source, url)
            return ArticleScore(
                headline=headline, source=source, url=url,
                published_at=published_at, ticker=ticker,
                fud_score=0.70, sentiment=0.0,
                source_credibility=credibility,
                has_financial_data=True, is_primary_source=True,
                is_quality_signal=True, is_fud=False, is_neutral=False,
                fud_keywords_found=[], quality_keywords_found=[],
                reasoning=reasoning, source_profile=profile,
                scored_at=datetime.now(),
            )

        # ── Keyword extraction ─────────────────────────────────
        fud_found, quality_found = self._extract_keywords(full_text)
        sentiment = self._compute_sentiment(full_text)

        # ── Dimension scores ───────────────────────────────────
        source_cred, profile = self._score_source(source, url)
        data_score    = self._score_financial_data(full_text, quality_found)
        recency_score = self._score_recency_context(published_at, ticker)

        # Keyword score: quality keywords boost, FUD keywords penalize
        fud_penalty     = min(0.40, len(fud_found) * 0.08)
        quality_bonus   = min(0.40, len(quality_found) * 0.05)
        keyword_score   = max(0.0, min(1.0, 0.50 + quality_bonus - fud_penalty))

        # Sentiment × credibility: high-credibility negative article is still quality
        # Low-credibility extreme sentiment = likely FUD
        sentiment_extremity = abs(sentiment)
        if source_cred < 0.40 and sentiment_extremity > 0.60:
            sent_cred_score = 0.20   # Extreme emotion from low-cred source = FUD
            reasoning.append(f"Extreme sentiment ({sentiment:+.2f}) from low-credibility source → FUD flag")
        elif source_cred >= 0.70 and data_score >= 0.50:
            sent_cred_score = 0.80   # High-cred + data = quality regardless of sentiment
        else:
            sent_cred_score = 0.50 + (source_cred - 0.50) * 0.60

        # ── Weighted composite ─────────────────────────────────
        fud_score = (
            self.WEIGHTS["source_credibility"]    * source_cred    +
            self.WEIGHTS["financial_data"]        * data_score     +
            self.WEIGHTS["sentiment_credibility"] * sent_cred_score +
            self.WEIGHTS["keyword_match"]         * keyword_score  +
            self.WEIGHTS["recency_context"]       * recency_score
        )

        # ── Special case adjustments ───────────────────────────
        # Short seller reports: credibility varies but always flag bias
        if profile.bias_direction == "bearish" and source_cred < 0.65:
            fud_score *= 0.80
            reasoning.append("Bear-biased source — applying credibility discount")

        # Cluster FUD keywords = strong penalization
        if len(fud_found) >= 3:
            fud_score = min(fud_score, 0.40)
            reasoning.append(f"Multiple FUD keywords found: {fud_found[:3]}")

        # Primary source boost
        if profile.is_primary_source:
            fud_score = max(fud_score, 0.75)
            reasoning.append("Primary source detected — minimum quality floor applied")

        fud_score = max(0.0, min(1.0, fud_score))

        # ── Classification ─────────────────────────────────────
        is_quality = fud_score >= self.QUALITY_THRESHOLD
        is_fud     = fud_score <  self.FUD_THRESHOLD
        is_neutral = not is_quality and not is_fud

        # ── Build reasoning ────────────────────────────────────
        reasoning.append(f"Source '{profile.name}': credibility={source_cred:.2f}")
        if data_score >= 0.50:
            reasoning.append(f"Financial data detected (score={data_score:.2f})")
        else:
            reasoning.append(f"Low financial data content (score={data_score:.2f})")
        if quality_found:
            reasoning.append(f"Quality keywords: {quality_found[:4]}")
        if fud_found:
            reasoning.append(f"FUD keywords: {fud_found[:4]}")
        reasoning.append(f"Sentiment: {sentiment:+.2f} | Final FUD score: {fud_score:.2f}")

        if is_fud:
            reasoning.append("→ CLASSIFIED AS FUD — exclude from signal consideration")
        elif is_quality:
            reasoning.append("→ CLASSIFIED AS QUALITY SIGNAL — include in analysis")
        else:
            reasoning.append("→ NEUTRAL — low weight in signal scoring")

        logger.debug(
            f"[FUD] {ticker} | {profile.name} | "
            f"score={fud_score:.2f} | quality={is_quality} | sentiment={sentiment:+.2f} | "
            f"headline='{headline[:60]}...'"
        )

        return ArticleScore(
            headline=headline,
            source=source,
            url=url,
            published_at=published_at,
            ticker=ticker,
            fud_score=fud_score,
            sentiment=sentiment,
            source_credibility=source_cred,
            has_financial_data=data_score >= 0.40,
            is_primary_source=profile.is_primary_source,
            is_quality_signal=is_quality,
            is_fud=is_fud,
            is_neutral=is_neutral,
            fud_keywords_found=fud_found,
            quality_keywords_found=quality_found,
            reasoning=reasoning,
            source_profile=profile,
            scored_at=datetime.now(),
        )
