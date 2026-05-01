"""
signals/tipranks_signal.py — TipRanks signal normalizer.

Converts raw TipRanks getData payload into a normalized -1.0 to +1.0 score
using Smart Score (50%), analyst consensus (30%), and PT upside (20%).

Confirmed field paths (from live API response):
  tipranksStockScore.score           → Smart Score 1-10
  portfolioHoldingData.analystConsensus.distribution → {buy, hold, sell}
  ptConsensus[0].priceTarget         → mean price target (period=3, bench=1)
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class TipRanksResult:
    ticker:            str
    smart_score:       Optional[int]   = None   # 1–10
    buy_pct:           Optional[float] = None   # 0–100
    hold_pct:          Optional[float] = None
    sell_pct:          Optional[float] = None
    price_target_mean: Optional[float] = None
    price_target_high: Optional[float] = None
    price_target_low:  Optional[float] = None
    analyst_count:     int             = 0
    hedge_fund_trend:  Optional[str]   = None   # "positive" | "negative" | None
    news_bullish_pct:  Optional[float] = None
    composite_score:   float           = 0.0    # -1.0 to +1.0


class TipRanksAnalyzer:
    """Parses TipRanks getData JSON and computes a composite signal score."""

    def analyze(
        self,
        ticker: str,
        raw: dict,
        current_price: Optional[float] = None,
    ) -> TipRanksResult:
        r = TipRanksResult(ticker=ticker)

        # ── Smart Score (tipranksStockScore.score, 1–10) ──────────────────────
        try:
            ss = (raw.get("tipranksStockScore") or {}).get("score")
            if ss is not None:
                r.smart_score = int(ss)
        except Exception:
            pass

        # ── Analyst consensus (portfolioHoldingData.analystConsensus) ─────────
        try:
            phd  = raw.get("portfolioHoldingData") or {}
            dist = (phd.get("analystConsensus") or {}).get("distribution") or {}
            buys  = float(dist.get("buy",  0))
            holds = float(dist.get("hold", 0))
            sells = float(dist.get("sell", 0))
            total = buys + holds + sells
            if total > 0:
                r.buy_pct      = buys  / total * 100.0
                r.hold_pct     = holds / total * 100.0
                r.sell_pct     = sells / total * 100.0
                r.analyst_count = int(total)
        except Exception:
            pass

        # Fallback: consensuses list
        if r.buy_pct is None:
            try:
                for c in (raw.get("consensuses") or []):
                    if c.get("isLatest") == 1:
                        nb = float(c.get("nB", 0))
                        nh = float(c.get("nH", 0))
                        ns = float(c.get("nS", 0))
                        total = nb + nh + ns
                        if total > 0:
                            r.buy_pct  = nb / total * 100.0
                            r.hold_pct = nh / total * 100.0
                            r.sell_pct = ns / total * 100.0
                            r.analyst_count = int(total)
                        break
            except Exception:
                pass

        # ── Price target (ptConsensus[0] where period=3, bench=1) ─────────────
        try:
            for pt in (raw.get("ptConsensus") or []):
                if pt.get("period") == 3 and pt.get("bench") == 1:
                    r.price_target_mean = float(pt["priceTarget"]) if pt.get("priceTarget") else None
                    r.price_target_high = float(pt["high"])        if pt.get("high")        else None
                    r.price_target_low  = float(pt["low"])         if pt.get("low")         else None
                    break
        except Exception:
            pass

        # ── Hedge fund trend ──────────────────────────────────────────────────
        try:
            hf = raw.get("hedgeFundData") or {}
            trend = hf.get("trendAction")  # "Increased" | "Decreased" | None
            if trend:
                r.hedge_fund_trend = "positive" if "increas" in str(trend).lower() else "negative"
        except Exception:
            pass

        # ── Composite score ───────────────────────────────────────────────────
        parts = []   # (score, weight)

        if r.smart_score is not None:
            # 1 → -1.0,  5.5 → 0.0,  10 → +1.0
            ss_norm = (r.smart_score - 5.5) / 4.5
            parts.append((max(-1.0, min(1.0, ss_norm)), 0.50))

        if r.buy_pct is not None and r.sell_pct is not None:
            cons_norm = (r.buy_pct - r.sell_pct) / 100.0
            parts.append((max(-1.0, min(1.0, cons_norm)), 0.30))

        if r.price_target_mean and current_price and current_price > 0:
            upside = (r.price_target_mean - current_price) / current_price
            pt_norm = max(-1.0, min(1.0, upside * 2.0))
            parts.append((pt_norm, 0.20))

        if parts:
            total_w = sum(w for _, w in parts)
            r.composite_score = round(sum(s * w for s, w in parts) / total_w, 4)

        return r
