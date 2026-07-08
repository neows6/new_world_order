"""
analysis/moat_detector.py — Durable Competitive Advantage (Moat) Detector.

A moat is a business's ability to maintain competitive advantages over its
competitors in order to protect its long-term profits and market share.

We detect moats through measurable proxies:
  1. Gross margin stability & level    → Pricing power
  2. ROIC > WACC sustained over time  → Capital efficiency / real advantage
  3. Revenue growth consistency        → Demand durability
  4. FCF conversion rate               → Quality of earnings
  5. Net debt trend                    → Financial fortress
  6. R&D / SGA efficiency              → Intangible asset moats

Moat types (Porter / Morningstar framework):
  - COST_ADVANTAGE    : Structural low-cost producer
  - SWITCHING_COST    : High customer lock-in
  - NETWORK_EFFECT    : Value grows with user base
  - INTANGIBLE_ASSET  : Brand, patents, regulatory license
  - EFFICIENT_SCALE   : Natural monopoly in niche market
  - NONE              : Commodity business, no moat
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from loguru import logger


class MoatType(Enum):
    COST_ADVANTAGE   = "cost_advantage"
    SWITCHING_COST   = "switching_cost"
    NETWORK_EFFECT   = "network_effect"
    INTANGIBLE_ASSET = "intangible_asset"
    EFFICIENT_SCALE  = "efficient_scale"
    NONE             = "none"


class MoatStrength(Enum):
    WIDE    = "wide"    # 20+ year runway — highest conviction
    NARROW  = "narrow"  # 5-10 year runway — still valuable
    NONE    = "none"    # Cyclical or commodity


@dataclass
class MoatResult:
    strength: MoatStrength
    likely_types: list              # List of MoatType
    score: float                    # 0.0 to 1.0 composite
    gross_margin_avg: Optional[float]
    gross_margin_stable: bool
    roic_above_wacc_count: int      # Years where ROIC > WACC
    roic_years_analyzed: int
    revenue_growth_consistent: bool
    fcf_conversion_rate: Optional[float]  # FCF / Net income
    net_debt_trend: str             # "improving", "stable", "deteriorating"
    signals: list                   # Human-readable signal descriptions
    warnings: list                  # Red flags


class MoatDetector:
    """
    Analyzes a company's fundamental history to detect and quantify moat.
    Requires at least 3 years of data for meaningful signals.
    5+ years gives high confidence.
    """

    # Threshold constants
    WIDE_MOAT_GROSS_MARGIN    = 0.50    # 50%+ sustained = pricing power
    NARROW_MOAT_GROSS_MARGIN  = 0.35    # 35-50% = some pricing power
    HIGH_ROIC_THRESHOLD       = 0.15    # 15%+ ROIC = excellent capital allocation
    MIN_ROIC_ABOVE_WACC_YEARS = 3       # Must beat WACC for 3+ years for wide moat
    FCF_CONVERSION_THRESHOLD  = 0.80    # FCF/NI > 80% = high earnings quality
    GROSS_MARGIN_VOLATILITY   = 0.05    # < 5% std deviation = stable margins

    def _analyze_gross_margins(self, margins: list) -> dict:
        """Analyze gross margin level, trend, and stability."""
        if not margins:
            return {"avg": None, "stable": False, "trend": "unknown", "signal": "Insufficient data"}

        # Discard impossible values (>100% or <0%) — caused by EDGAR fiscal-year
        # parsing mismatches where quarterly revenue is divided into annual gross_profit
        valid = [m for m in margins if m is not None and 0.0 < m <= 1.0]
        if len(valid) < 2:
            return {"avg": valid[0] if valid else None, "stable": False, "trend": "unknown", "signal": "Insufficient data"}

        import statistics
        avg = statistics.mean(valid)
        stdev = statistics.stdev(valid) if len(valid) > 1 else 0
        stable = stdev < self.GROSS_MARGIN_VOLATILITY

        # Trend: compare first half vs second half
        mid = len(valid) // 2
        first_half_avg = statistics.mean(valid[:mid]) if mid > 0 else avg
        second_half_avg = statistics.mean(valid[mid:]) if mid < len(valid) else avg
        delta = second_half_avg - first_half_avg

        if delta > 0.02:
            trend = "expanding"
        elif delta < -0.02:
            trend = "contracting"
        else:
            trend = "stable"

        if avg >= self.WIDE_MOAT_GROSS_MARGIN and stable:
            signal = f"Strong pricing power: {avg:.1%} avg gross margin, stable (σ={stdev:.1%})"
        elif avg >= self.NARROW_MOAT_GROSS_MARGIN:
            signal = f"Moderate pricing power: {avg:.1%} avg gross margin, {trend}"
        else:
            signal = f"Weak pricing power: {avg:.1%} avg gross margin — commodity risk"

        return {"avg": avg, "stable": stable, "stdev": stdev, "trend": trend, "signal": signal}

    def _analyze_roic_vs_wacc(self, roics: list, wacc: float) -> dict:
        """Count years where ROIC > WACC — the single most important moat signal."""
        valid_roics = [(yr, r) for yr, r in roics if r is not None]
        if not valid_roics:
            return {"above_count": 0, "total": 0, "avg_spread": None, "signal": "No ROIC data"}

        above = [(yr, r) for yr, r in valid_roics if r > wacc]
        spread = [r - wacc for _, r in valid_roics]

        import statistics
        avg_spread = statistics.mean(spread) if spread else 0

        pct_above = len(above) / len(valid_roics) if valid_roics else 0

        if pct_above >= 0.80 and avg_spread > 0.05:
            signal = f"ROIC consistently {avg_spread:.1%} above WACC — strong value creation"
        elif pct_above >= 0.50:
            signal = f"ROIC above WACC in {pct_above:.0%} of years — moderate value creation"
        else:
            signal = "ROIC below WACC in majority of years — value destruction risk"

        return {
            "above_count": len(above),
            "total": len(valid_roics),
            "pct_above": pct_above,
            "avg_spread": avg_spread,
            "signal": signal,
        }

    def _analyze_revenue_consistency(self, revenues: list) -> dict:
        """
        Revenue should grow consistently — not lumpy or declining.
        Lumpy revenue = no moat (project-based, cyclical).
        """
        valid = [r for r in revenues if r is not None and r > 0]
        if len(valid) < 3:
            return {"consistent": False, "cagr": None, "signal": "Insufficient revenue history"}

        # Year-over-year growth rates
        yoy = [(valid[i] - valid[i-1]) / valid[i-1] for i in range(1, len(valid))]
        negative_years = sum(1 for g in yoy if g < 0)

        # CAGR
        cagr = (valid[-1] / valid[0]) ** (1 / (len(valid) - 1)) - 1 if valid[0] > 0 else None

        # Consistent = no more than 1 down year in 5
        consistent = negative_years <= 1

        if consistent and cagr and cagr > 0.07:
            signal = f"Strong consistent revenue growth: {cagr:.1%} CAGR, {negative_years} down years"
        elif consistent:
            signal = f"Stable revenue: {cagr:.1%} CAGR, {negative_years} down years"
        else:
            signal = f"Inconsistent revenue: {negative_years} down years — cyclical risk"

        return {"consistent": consistent, "cagr": cagr, "negative_years": negative_years, "signal": signal}

    def _analyze_fcf_conversion(self, net_incomes: list, fcfs: list) -> dict:
        """
        FCF / Net Income > 80% means earnings are real cash, not accounting fiction.
        """
        pairs = [(ni, fcf) for ni, fcf in zip(net_incomes, fcfs)
                 if ni is not None and fcf is not None and ni > 0]

        if not pairs:
            return {"rate": None, "signal": "Insufficient FCF data"}

        rates = [fcf / ni for ni, fcf in pairs]
        import statistics
        avg_rate = statistics.mean(rates)

        if avg_rate >= self.FCF_CONVERSION_THRESHOLD:
            signal = f"High earnings quality: FCF/NI = {avg_rate:.0%} (target >80%)"
        elif avg_rate >= 0.60:
            signal = f"Moderate earnings quality: FCF/NI = {avg_rate:.0%}"
        else:
            signal = f"Low earnings quality: FCF/NI = {avg_rate:.0%} — investigate accruals"

        return {"rate": avg_rate, "signal": signal}

    def _analyze_debt_trend(self, net_debts: list) -> str:
        """Classify net debt trend over the analysis period."""
        valid = [d for d in net_debts if d is not None]
        if len(valid) < 2:
            return "unknown"

        # Negative net debt = net cash position (ideal)
        if all(d < 0 for d in valid):
            return "net_cash_fortress"

        delta = valid[-1] - valid[0]
        pct_change = delta / abs(valid[0]) if valid[0] != 0 else 0

        if pct_change < -0.20:
            return "improving"
        elif pct_change > 0.20:
            return "deteriorating"
        else:
            return "stable"

    def _infer_moat_types(
        self,
        gross_margin_avg: Optional[float],
        roic_avg_spread: Optional[float],
        fcf_conversion: Optional[float],
        revenue_cagr: Optional[float],
    ) -> list:
        """
        Infer likely moat type from quantitative signals.
        This is probabilistic — confirms with qualitative knowledge.
        """
        types = []

        # High gross margin = pricing power = intangible asset or switching cost
        if gross_margin_avg is not None and gross_margin_avg > 0.60:
            types.append(MoatType.INTANGIBLE_ASSET)
        if gross_margin_avg is not None and 0.40 <= gross_margin_avg <= 0.70:
            types.append(MoatType.SWITCHING_COST)

        # Very high ROIC spread = efficient scale or network effect
        if roic_avg_spread is not None and roic_avg_spread > 0.10:
            types.append(MoatType.NETWORK_EFFECT)

        # High FCF conversion + modest margins = cost advantage
        if fcf_conversion is not None and fcf_conversion > 0.85 and gross_margin_avg is not None and gross_margin_avg < 0.40:
            types.append(MoatType.COST_ADVANTAGE)

        return list(set(types)) if types else [MoatType.NONE]

    def analyze(
        self,
        fundamentals_by_year: dict,     # {year: Fundamental ORM object}
        wacc: float,
        market_cap: Optional[float] = None,
    ) -> MoatResult:
        """
        Main entry point. Analyzes fundamentals history and returns MoatResult.

        fundamentals_by_year: sorted dict of {fiscal_year: fundamental_record}
        """
        years = sorted(fundamentals_by_year.keys())
        signals = []
        warnings = []

        if len(years) < 3:
            warnings.append(f"Only {len(years)} years of data — moat analysis needs 3+ years")

        # Extract time series
        gross_margins = [fundamentals_by_year[y].gross_margin for y in years]
        roics = [(y, fundamentals_by_year[y].roic) for y in years]
        revenues = [fundamentals_by_year[y].revenue for y in years]
        net_incomes = [fundamentals_by_year[y].net_income for y in years]
        fcfs = [fundamentals_by_year[y].free_cash_flow for y in years]
        net_debts = [fundamentals_by_year[y].net_debt for y in years]

        # Run analyses
        gm = self._analyze_gross_margins(gross_margins)
        roic_analysis = self._analyze_roic_vs_wacc(roics, wacc)
        revenue_analysis = self._analyze_revenue_consistency(revenues)
        fcf_analysis = self._analyze_fcf_conversion(net_incomes, fcfs)
        debt_trend = self._analyze_debt_trend(net_debts)

        signals.extend([
            gm["signal"],
            roic_analysis["signal"],
            revenue_analysis["signal"],
            fcf_analysis["signal"],
        ])

        if debt_trend == "net_cash_fortress":
            signals.append("Balance sheet: Net cash position — financial fortress")
        elif debt_trend == "deteriorating":
            warnings.append("Balance sheet: Debt is increasing — monitor leverage")

        # ── Composite moat score ──────────────────────────────
        score = 0.0
        max_score = 4.0

        # Gross margin component (0-1)
        if gm["avg"] is not None:
            gm_score = min(gm["avg"] / self.WIDE_MOAT_GROSS_MARGIN, 1.0)
            gm_score *= (1.1 if gm["stable"] else 0.9)
            score += gm_score

        # ROIC vs WACC component (0-1)
        if roic_analysis["total"] > 0:
            score += roic_analysis["pct_above"]

        # Revenue consistency component (0-1)
        if revenue_analysis["consistent"]:
            cagr_bonus = min((revenue_analysis.get("cagr") or 0) / 0.10, 1.0)
            score += 0.5 + 0.5 * cagr_bonus
        else:
            score += 0.1

        # FCF quality component (0-1)
        if fcf_analysis["rate"] is not None:
            score += min(fcf_analysis["rate"], 1.0)

        normalized_score = score / max_score

        # ── Moat strength classification ─────────────────────
        roic_above_count = roic_analysis["above_count"]
        gm_avg = gm["avg"]

        if (
            normalized_score >= 0.70
            and roic_above_count >= self.MIN_ROIC_ABOVE_WACC_YEARS
            and gm_avg and gm_avg >= self.WIDE_MOAT_GROSS_MARGIN
            and gm["stable"]
        ):
            strength = MoatStrength.WIDE
        elif normalized_score >= 0.45 or roic_above_count >= 2:
            strength = MoatStrength.NARROW
        else:
            strength = MoatStrength.NONE
            warnings.append("No durable competitive advantage detected — commodity risk")

        # ── Moat type inference ───────────────────────────────
        likely_types = self._infer_moat_types(
            gross_margin_avg=gm_avg,
            roic_avg_spread=roic_analysis.get("avg_spread"),
            fcf_conversion=fcf_analysis.get("rate"),
            revenue_cagr=revenue_analysis.get("cagr"),
        )

        logger.info(
            f"Moat analysis: {strength.value.upper()} | "
            f"Score: {normalized_score:.2f} | "
            f"Types: {[t.value for t in likely_types]}"
        )

        return MoatResult(
            strength=strength,
            likely_types=likely_types,
            score=normalized_score,
            gross_margin_avg=gm_avg,
            gross_margin_stable=gm["stable"],
            roic_above_wacc_count=roic_above_count,
            roic_years_analyzed=roic_analysis["total"],
            revenue_growth_consistent=revenue_analysis["consistent"],
            fcf_conversion_rate=fcf_analysis.get("rate"),
            net_debt_trend=debt_trend,
            signals=signals,
            warnings=warnings,
        )
