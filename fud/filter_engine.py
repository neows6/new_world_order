"""
fud/filter_engine.py — Layer 3: FUD Filter Engine.

This is the Layer 3 orchestrator. It:
  1. Fetches news for each ticker (SEC filings + financial media)
  2. Scores every article on the FUD vs Quality spectrum
  3. Computes a ticker-level news quality score
  4. Detects adversarial patterns (coordinated FUD, short attacks)
  5. Merges the news quality score with the Layer 2 + Signals output
  6. Produces a FUDFilterResult that feeds into Layer 4 (Decision Engine)

Key insight:
  We don't just filter BAD news. A quality negative article (Reuters
  earnings miss report with actual numbers) is VALUABLE information —
  it may confirm our fundamental analysis or trigger a re-evaluation.

  What we filter is NOISE masquerading as signal:
  - Social media FUD without data
  - Short seller reports we can't verify
  - Macro fear applied indiscriminately
  - Coordinated negative campaigns

FUD attack detection:
  - Volume spike in negative articles from low-credibility sources
  - Negative articles clustering on same day without SEC filing
  - Social media sentiment diverging massively from institutional action
  - Short seller report + no insider selling = suspicious (insiders would know)
"""

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from loguru import logger

from config import config
from models.database import Company, Fundamental, NewsItem
from fud.sources import get_source_profile
from fud.classifier import FUDClassifier, ArticleScore
from fud.news_fetcher import NewsFetcher, RawArticle
from signals.aggregator import AggregatedSignal


@dataclass
class FUDAnalysis:
    """News quality analysis for a single ticker."""
    ticker: str
    analyzed_at: str

    # Article counts
    total_articles: int
    quality_articles: int     # fud_score >= 0.60
    fud_articles: int         # fud_score < 0.35
    neutral_articles: int

    # Quality metrics
    avg_fud_score: float           # 0.0 to 1.0 — higher = more quality news
    quality_signal_ratio: float    # quality_articles / total_articles
    weighted_sentiment: float      # Credibility-weighted sentiment -1.0 to 1.0

    # Primary source signals
    has_recent_10k: bool
    has_recent_10q: bool
    has_recent_8k: bool
    recent_8k_headline: Optional[str]   # What was the most recent 8-K about?

    # Adversarial detection
    fud_attack_detected: bool
    fud_attack_reason: Optional[str]
    coordinated_fud_score: float    # 0.0 to 1.0 — how coordinated/suspicious

    # Pass/fail gate
    passes_fud_filter: bool         # True = OK to proceed to decision engine
    filter_reason: str              # Why it passed or failed

    # Best and worst articles
    top_quality_articles: list      # Top 3 ArticleScore objects
    top_fud_articles: list          # Top 3 FUD ArticleScore objects (for review)

    # Adjustment to signal
    signal_adjustment: float        # -0.30 to +0.20 — how to adjust composite score
    adjustment_reason: str

    notes: list
    warnings: list


@dataclass
class Layer3Result:
    """
    Full Layer 3 output — combines FUD analysis with the incoming signal.
    This feeds directly into Layer 4 (Decision Engine).
    """
    ticker: str
    timestamp: str

    # Input signals (from Layer 2 + signals layer)
    incoming_signal: AggregatedSignal

    # FUD analysis
    fud_analysis: FUDAnalysis

    # Adjusted signal (after FUD filter)
    adjusted_composite_score: float
    adjusted_signal: str            # May differ from incoming if FUD detected
    final_confidence: float         # Confidence after news quality discount

    # Gate decision
    proceed_to_execution: bool
    gate_reason: str

    def summary(self) -> str:
        """Printable Layer 3 summary."""
        lines = [
            f"{'═'*58}",
            f"  LAYER 3 — FUD FILTER: {self.ticker}",
            f"{'═'*58}",
            f"",
            f"INCOMING SIGNAL: {self.incoming_signal.signal.upper()} "
            f"(composite={self.incoming_signal.composite_score:+.2f})",
            f"",
            f"NEWS QUALITY ANALYSIS:",
            f"  Articles analyzed:  {self.fud_analysis.total_articles}",
            f"  Quality signals:    {self.fud_analysis.quality_articles} "
            f"({self.fud_analysis.quality_signal_ratio:.0%})",
            f"  FUD articles:       {self.fud_analysis.fud_articles}",
            f"  Avg quality score:  {self.fud_analysis.avg_fud_score:.2f}",
            f"  Weighted sentiment: {self.fud_analysis.weighted_sentiment:+.2f}",
            f"",
            f"PRIMARY SOURCE CHECK:",
            f"  Recent 10-K: {'✓' if self.fud_analysis.has_recent_10k else '✗'}",
            f"  Recent 10-Q: {'✓' if self.fud_analysis.has_recent_10q else '✗'}",
            f"  Recent 8-K:  {'✓' if self.fud_analysis.has_recent_8k else '✗'}",
        ]

        if self.fud_analysis.recent_8k_headline:
            lines.append(f"  8-K:         {self.fud_analysis.recent_8k_headline[:60]}")

        if self.fud_analysis.fud_attack_detected:
            lines += [
                f"",
                f"  ⚠️  FUD ATTACK DETECTED: {self.fud_analysis.fud_attack_reason}",
            ]

        lines += [
            f"",
            f"SIGNAL ADJUSTMENT: {self.fud_analysis.signal_adjustment:+.2f}",
            f"  Reason: {self.fud_analysis.adjustment_reason}",
            f"",
            f"FINAL SIGNAL: {self.adjusted_signal.upper()} "
            f"(composite={self.adjusted_composite_score:+.2f}, "
            f"confidence={self.final_confidence:.0%})",
            f"",
            f"GATE: {'✅ PROCEED' if self.proceed_to_execution else '❌ BLOCKED'}",
            f"  {self.gate_reason}",
        ]

        if self.fud_analysis.top_quality_articles:
            lines.append(f"\nTOP QUALITY SIGNALS:")
            for a in self.fud_analysis.top_quality_articles[:2]:
                lines.append(f"  [{a.source_profile.name}] {a.headline[:70]}")

        if self.fud_analysis.top_fud_articles:
            lines.append(f"\nFUD DETECTED (excluded):")
            for a in self.fud_analysis.top_fud_articles[:2]:
                lines.append(f"  [{a.source_profile.name}] {a.headline[:70]}")

        return "\n".join(lines)


class FUDFilterEngine:
    """
    Layer 3 — FUD Filter Engine.
    Orchestrates news fetching, classification, and signal adjustment.
    """

    # Minimum quality ratio to pass the filter
    MIN_QUALITY_RATIO       = 0.30   # At least 30% of articles must be quality
    FUD_ATTACK_THRESHOLD    = 0.60   # If 60%+ articles are FUD = attack signal
    MIN_FUD_SCORE_TO_PROCEED = 0.40  # Minimum avg fud score to allow trade

    def __init__(self, db_session_factory):
        self.Session = db_session_factory
        self.fetcher    = NewsFetcher()
        self.classifier = FUDClassifier()

    def _score_articles(self, raw_articles: list, ticker: str) -> list:
        """Run all raw articles through the FUD classifier."""
        scored = []
        for art in raw_articles:
            try:
                score = self.classifier.score(
                    headline=art.headline,
                    source=art.source,
                    ticker=ticker,
                    url=art.url,
                    summary=art.summary,
                    published_at=art.published_at,
                    is_sec_filing=art.is_sec_filing,
                    filing_type=art.filing_type,
                )
                scored.append(score)
            except Exception as e:
                logger.warning(f"Failed to score article '{art.headline[:40]}': {e}")
        return scored

    def _detect_fud_attack(self, scored: list) -> tuple:
        """
        Detect coordinated FUD patterns.
        Returns (attack_detected, reason, coordination_score)
        """
        if len(scored) < 3:
            return False, None, 0.0

        fud_articles = [a for a in scored if a.is_fud]
        if not fud_articles:
            return False, None, 0.0

        fud_ratio = len(fud_articles) / len(scored)

        # Pattern 1: High volume of FUD from social media with no SEC backing
        social_fud = [a for a in fud_articles
                      if a.source_credibility < 0.30]
        has_recent_sec = any(a.is_primary_source for a in scored)

        if len(social_fud) >= 3 and not has_recent_sec:
            score = min(1.0, len(social_fud) / 5.0)
            return True, f"Social FUD spike ({len(social_fud)} low-credibility negative articles, no SEC data)", score

        # Pattern 2: Extreme negative sentiment cluster in short time
        recent_24h = [a for a in fud_articles
                      if a.published_at and
                      (datetime.now() - a.published_at).days < 1]
        if len(recent_24h) >= 4:
            score = min(1.0, len(recent_24h) / 6.0)
            return True, f"Rapid FUD cluster: {len(recent_24h)} negative articles in 24 hours", score

        # Pattern 3: Short seller report with no insider selling corroboration
        short_reports = [a for a in fud_articles
                         if a.source in ("short_seller_report", "citron", "hindenburg")]
        if short_reports:
            return True, "Short seller report detected — verify independently before acting", 0.6

        # Pattern 4: Overall FUD ratio very high
        if fud_ratio >= self.FUD_ATTACK_THRESHOLD:
            score = fud_ratio
            return True, f"Unusually high FUD ratio: {fud_ratio:.0%} of articles are low-quality", score

        return False, None, fud_ratio * 0.3   # Low coordination score if no attack

    def _compute_signal_adjustment(
        self,
        fud_analysis: FUDAnalysis,
        incoming_signal: AggregatedSignal,
    ) -> tuple:
        """
        Compute how much to adjust the composite signal score based on news quality.
        Returns (adjustment: float, reason: str)

        Logic:
          - High quality news confirming buy = small positive boost (+0.10)
          - High quality negative news (earnings miss) = penalize signal (-0.20)
          - FUD attack detected = reduce signal (-0.25) but don't flip
          - Silence (no news) = small neutral discount (-0.05)
          - Primary source (SEC filing) = preserve signal
        """
        adj = 0.0
        reasons = []

        avg_score   = fud_analysis.avg_fud_score
        sentiment   = fud_analysis.weighted_sentiment
        quality_ratio = fud_analysis.quality_signal_ratio

        # FUD attack: reduce confidence, don't flip signal
        if fud_analysis.fud_attack_detected:
            adj -= 0.25
            reasons.append("FUD attack detected — signal confidence reduced")

        # High quality news with POSITIVE sentiment + buy signal = boost
        if avg_score >= 0.70 and sentiment > 0.20 and incoming_signal.composite_score > 0:
            adj += 0.10
            reasons.append("Quality positive news aligns with buy signal")

        # High quality news with NEGATIVE sentiment = serious warning
        elif avg_score >= 0.70 and sentiment < -0.20:
            adj -= 0.20
            reasons.append("Quality negative news detected — signal penalized")

        # Low quality news environment overall
        elif avg_score < 0.40 and quality_ratio < 0.25:
            adj -= 0.10
            reasons.append("Low news quality environment — reduced confidence")

        # Primary SEC filing present = trust fundamentals
        if fud_analysis.has_recent_10k or fud_analysis.has_recent_10q:
            adj += 0.05
            reasons.append("Recent SEC filing confirms data freshness")

        # Recent 8-K might be material event
        if fud_analysis.has_recent_8k:
            # 8-K is neutral until we know what it contains
            reasons.append("Recent 8-K detected — review content before trading")

        # No news = slightly lower confidence
        if fud_analysis.total_articles == 0:
            adj -= 0.05
            reasons.append("No recent news — cannot assess news environment")

        reason_str = " | ".join(reasons) if reasons else "No significant news adjustment"
        return max(-0.30, min(0.20, adj)), reason_str

    def _load_fundamentals_for_ticker(self, session, ticker: str):
        """Load most recent fundamental record for a ticker."""
        company = session.query(Company).filter_by(ticker=ticker).first()
        if not company:
            return None, None
        latest = (
            session.query(Fundamental)
            .filter_by(company_id=company.id, fiscal_quarter=0)
            .order_by(Fundamental.fiscal_year.desc())
            .first()
        )
        return company, latest

    def _persist_news_to_db(self, session, company_id: int, scored_articles: list):
        """Save scored news items to the database for audit trail."""
        for art in scored_articles:
            try:
                existing = (
                    session.query(NewsItem)
                    .filter_by(headline=art.headline[:500], company_id=company_id)
                    .first()
                )
                if existing:
                    existing.fud_score = art.fud_score
                    existing.sentiment_score = art.sentiment
                    existing.contains_financial_data = art.has_financial_data
                    existing.is_sec_filing = art.is_primary_source
                else:
                    news_item = NewsItem(
                        company_id=company_id,
                        headline=art.headline[:500],
                        url=art.url,
                        source=art.source,
                        published_at=art.published_at,
                        fud_score=art.fud_score,
                        sentiment_score=art.sentiment,
                        contains_financial_data=art.has_financial_data,
                        is_sec_filing=art.is_primary_source,
                        filing_type=art.filing_type,
                    )
                    session.add(news_item)
            except Exception as e:
                logger.debug(f"Could not persist news item: {e}")

        try:
            session.commit()
        except Exception as e:
            logger.warning(f"News persist commit failed: {e}")
            session.rollback()

    def analyze_ticker(
        self,
        ticker: str,
        incoming_signal: AggregatedSignal,
    ) -> Layer3Result:
        """
        Run full Layer 3 FUD filter for one ticker.
        Connects the signals aggregator output to the decision engine input.
        """
        logger.info(f"[L3] Running FUD filter for {ticker}...")
        notes    = []
        warnings = []

        with self.Session() as session:
            company, latest_fundamental = self._load_fundamentals_for_ticker(session, ticker)
            cik = company.cik if company else None

            # ── Fetch news ─────────────────────────────────────
            raw_articles = self.fetcher.fetch_all(
                ticker=ticker,
                cik=cik,
                days_back=30,
                fundamental_record=latest_fundamental,
            )

            # ── Score articles ─────────────────────────────────
            scored_articles = self._score_articles(raw_articles, ticker)

            # ── Persist to DB ──────────────────────────────────
            if company:
                self._persist_news_to_db(session, company.id, scored_articles)

            # ── Aggregate news metrics ─────────────────────────
            total = len(scored_articles)
            quality_arts = [a for a in scored_articles if a.is_quality_signal]
            fud_arts     = [a for a in scored_articles if a.is_fud]
            neutral_arts = [a for a in scored_articles if a.is_neutral]

            # Exclude SEC filings (is_primary_source) from the quality average.
            # Filings are a binary structural signal tracked via has_10k/10q/8k.
            # Mixing them into the average pins every large-cap at the filing
            # auto-score (0.70) regardless of actual news coverage quality.
            news_only = [a for a in scored_articles if not a.is_primary_source]
            avg_fud_score = statistics.mean(a.fud_score for a in news_only) if news_only else 0.50
            quality_ratio = len(quality_arts) / total if total > 0 else 0.50

            # Credibility-weighted sentiment
            if scored_articles:
                weighted_sent = sum(a.sentiment * a.source_credibility for a in scored_articles)
                total_weight  = sum(a.source_credibility for a in scored_articles)
                weighted_sentiment = weighted_sent / total_weight if total_weight > 0 else 0.0
            else:
                weighted_sentiment = 0.0

            # Filing checks
            has_10k = any(a.filing_type == "10-K" for a in scored_articles if a.is_primary_source)
            has_10q = any(a.filing_type == "10-Q" for a in scored_articles if a.is_primary_source)
            has_8k  = any(a.filing_type == "8-K"  for a in scored_articles if a.is_primary_source)
            recent_8k_headline = next(
                (a.headline for a in scored_articles if a.filing_type == "8-K"), None
            )

            # ── FUD attack detection ───────────────────────────
            attack_detected, attack_reason, coord_score = self._detect_fud_attack(scored_articles)

            if attack_detected:
                warnings.append(f"FUD attack: {attack_reason}")

            # ── Pass/fail gate ─────────────────────────────────
            passes_filter = True
            filter_reason = "Passed all FUD filter checks"

            if attack_detected and coord_score >= self.FUD_ATTACK_THRESHOLD:  # fix: was hardcoded 0.80
                passes_filter = False
                filter_reason = f"BLOCKED: High-confidence FUD attack — {attack_reason}"
            elif total >= 1 and avg_fud_score < self.MIN_FUD_SCORE_TO_PROCEED:  # fix: was gated on total >= 5
                passes_filter = False
                filter_reason = (
                    f"BLOCKED: News quality too low (avg={avg_fud_score:.2f} < "
                    f"{self.MIN_FUD_SCORE_TO_PROCEED:.2f} threshold)"
                )
            elif total == 0:
                filter_reason = "No news available — proceeding with fundamentals only"
                notes.append("No recent news — relying entirely on fundamental and signal analysis")

            # Top articles for display
            top_quality = sorted(quality_arts, key=lambda a: a.fud_score, reverse=True)[:3]
            top_fud     = sorted(fud_arts,     key=lambda a: a.fud_score)[:3]

            # ── Assemble FUD analysis ──────────────────────────
            fud_analysis = FUDAnalysis(
                ticker=ticker,
                analyzed_at=datetime.utcnow().isoformat(),
                total_articles=total,
                quality_articles=len(quality_arts),
                fud_articles=len(fud_arts),
                neutral_articles=len(neutral_arts),
                avg_fud_score=avg_fud_score,
                quality_signal_ratio=quality_ratio,
                weighted_sentiment=weighted_sentiment,
                has_recent_10k=has_10k,
                has_recent_10q=has_10q,
                has_recent_8k=has_8k,
                recent_8k_headline=recent_8k_headline,
                fud_attack_detected=attack_detected,
                fud_attack_reason=attack_reason,
                coordinated_fud_score=coord_score,
                passes_fud_filter=passes_filter,
                filter_reason=filter_reason,
                top_quality_articles=top_quality,
                top_fud_articles=top_fud,
                signal_adjustment=0.0,     # Filled below
                adjustment_reason="",
                notes=notes,
                warnings=warnings,
            )

            # ── Signal adjustment ──────────────────────────────
            adjustment, adj_reason = self._compute_signal_adjustment(fud_analysis, incoming_signal)
            fud_analysis.signal_adjustment   = adjustment
            fud_analysis.adjustment_reason   = adj_reason

            # ── Compute adjusted signal ────────────────────────
            adj_composite = incoming_signal.composite_score + adjustment
            adj_composite = max(-1.0, min(1.0, adj_composite))

            # Reclassify signal after adjustment
            if adj_composite >= 0.50:      adj_signal = "strong_buy"
            elif adj_composite >= 0.15:    adj_signal = "buy"  # matches aggregator threshold
            elif adj_composite >= -0.15:   adj_signal = "hold"
            elif adj_composite >= -0.40:   adj_signal = "sell"
            else:                          adj_signal = "strong_sell"

            # Respect the aggregator's WATCH downgrade — the conviction floor or
            # orphaned-signal guard already decided this setup is missing
            # confirmation. L3 must not silently undo that with a pure-composite
            # reclassification.
            if incoming_signal.signal == "watch":
                adj_signal = "watch"

            # If filter blocked, force to hold
            if not passes_filter:
                adj_signal   = "hold"
                adj_composite = 0.0

            # Confidence: reduce if FUD present, boost if high quality
            base_confidence = incoming_signal.confidence
            if attack_detected:
                final_confidence = base_confidence * 0.60
            elif avg_fud_score >= 0.70:
                final_confidence = min(1.0, base_confidence * 1.15)
            else:
                final_confidence = base_confidence * (0.80 + 0.20 * quality_ratio)

            logger.info(
                f"[L3] {ticker}: "
                f"incoming={incoming_signal.signal} → adjusted={adj_signal} | "
                f"fud_score={avg_fud_score:.2f} | "
                f"attack={attack_detected} | "
                f"gate={'PASS' if passes_filter else 'BLOCK'}"
            )

            return Layer3Result(
                ticker=ticker,
                timestamp=datetime.utcnow().isoformat(),
                incoming_signal=incoming_signal,
                fud_analysis=fud_analysis,
                adjusted_composite_score=adj_composite,
                adjusted_signal=adj_signal,
                final_confidence=final_confidence,
                proceed_to_execution=passes_filter,
                gate_reason=filter_reason,
            )

    def analyze_watchlist(
        self,
        signals: dict,   # {ticker: AggregatedSignal}
    ) -> dict:
        """
        Run Layer 3 FUD filter across entire watchlist.
        signals: output from the signal aggregator.
        Returns {ticker: Layer3Result}
        """
        results = {}
        for ticker, signal in signals.items():
            try:
                result = self.analyze_ticker(ticker, signal)
                results[ticker] = result
            except Exception as e:
                logger.error(f"[L3] Failed for {ticker}: {e}")

        # Summary table
        logger.info("\n" + "═" * 72)
        logger.info(f"{'Ticker':<8} {'In Signal':<14} {'FUD Score':>9} {'Out Signal':<14} {'Gate':>6}")
        logger.info("─" * 72)
        for ticker, r in sorted(results.items()):
            logger.info(
                f"{ticker:<8} {r.incoming_signal.signal:<14} "
                f"{r.fud_analysis.avg_fud_score:>9.2f} "
                f"{r.adjusted_signal:<14} "
                f"{'✅' if r.proceed_to_execution else '❌':>6}"
            )
        logger.info("═" * 72)

        return results


# ── CLI entry point ───────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    import argparse
    from loguru import logger
    from models.database import init_db

    parser = argparse.ArgumentParser(description="Layer 3: FUD Filter Engine")
    parser.add_argument("--ticker", required=True, help="Ticker to analyze")
    args = parser.parse_args()

    logger.add(config.log_file, rotation="10 MB", level=config.log_level)

    _, Session = init_db(config.database.url)

    # For standalone testing, create a dummy signal
    from signals.aggregator import AggregatedSignal
    dummy_signal = AggregatedSignal(
        ticker=args.ticker.upper(),
        timestamp=datetime.now().isoformat(),
        fundamentals_score=0.4, insider_score=0.0, technical_score=0.2,
        cycle_score=0.1, volume_score=0.1, composite_score=0.3,
        signal="buy", confidence=0.6,
        vix_regime="normal", position_size_multiplier=0.85,
        recommended_position_pct=0.025, entry_price=None,
        stop_loss=None, take_profit_1=None, take_profit_2=None,
        risk_reward_ratio=None, why_buy=[], why_wait=[], key_risks=[],
        investable=True, moat_strength="narrow", margin_of_safety=0.18,
        fib_confluence_score=0.0, fib_in_golden_zone=False,
        vwap_position="below", vwap_institutional_bias="neutral",
        poc_level=None, fft_phase="rising", fft_signal_strength=0.4,
        insider_cluster_buy=False, insider_buy_value_90d=0.0,
    )

    engine = FUDFilterEngine(db_session_factory=Session)
    result = engine.analyze_ticker(args.ticker.upper(), dummy_signal)
    print(result.summary())
