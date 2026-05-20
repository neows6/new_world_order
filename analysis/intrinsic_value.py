"""
analysis/intrinsic_value.py — Intrinsic Value Estimator (DCF on Owner Earnings).

Uses Buffett's owner earnings as the cash flow basis — more conservative
than standard FCF DCF because it accounts for maintenance capex explicitly.

Owner Earnings = Net Income + D&A - Maintenance Capex
(We approximate maintenance capex as a % of D&A based on asset intensity)

Three scenarios: Bear / Base / Bull
Each uses different growth assumptions and terminal value multiples.
Margin of safety = (Intrinsic Value - Current Price) / Intrinsic Value

We only buy when margin of safety >= threshold (default 15%).
"""

import statistics
from dataclasses import dataclass
from typing import Optional

from loguru import logger


@dataclass
class DCFScenario:
    name: str                           # "bear", "base", "bull"
    growth_rate_years_1_5: float        # Growth rate for first 5 years
    growth_rate_years_6_10: float       # Growth rate for years 6-10
    terminal_growth_rate: float         # Perpetuity growth rate after year 10
    discount_rate: float                # WACC used for discounting
    intrinsic_value_per_share: Optional[float]
    total_intrinsic_value: Optional[float]


@dataclass
class IntrinsicValueResult:
    ticker: str
    current_price: Optional[float]
    shares_outstanding: Optional[float]

    # Owner earnings baseline
    owner_earnings_ttm: Optional[float]         # Trailing 12-month
    owner_earnings_3yr_avg: Optional[float]     # 3-year average (more stable)
    owner_earnings_used: Optional[float]        # Which one we used

    # Scenarios
    bear: DCFScenario
    base: DCFScenario
    bull: DCFScenario

    # Summary
    intrinsic_value_conservative: Optional[float]   # Average of bear + base
    intrinsic_value_base: Optional[float]
    margin_of_safety: Optional[float]               # Based on conservative estimate
    is_undervalued: bool
    price_to_intrinsic: Optional[float]             # Current price / intrinsic

    notes: list
    warnings: list


class IntrinsicValueEstimator:
    """
    Conservative DCF model using owner earnings.
    Designed for quality businesses with predictable cash flows.
    Does NOT work well for: banks, pre-revenue companies, highly cyclical businesses.
    """

    # Terminal growth should never exceed long-run GDP growth
    MAX_TERMINAL_GROWTH = 0.035     # 3.5% — slightly above long-run GDP

    def _normalize_owner_earnings(self, fundamentals_by_year: dict) -> dict:
        """
        Compute normalized owner earnings from multiple years.
        Returns TTM, 3yr avg, and 5yr avg where data allows.
        """
        years = sorted(fundamentals_by_year.keys(), reverse=True)

        owner_earnings_series = []
        for y in years:
            f = fundamentals_by_year[y]
            if f.owner_earnings is not None:
                owner_earnings_series.append((y, f.owner_earnings))
            elif all([f.net_income, f.depreciation_amortization, f.capex]):
                # Recompute if not stored
                oe = f.net_income + f.depreciation_amortization - f.capex
                owner_earnings_series.append((y, oe))

        if not owner_earnings_series:
            return {"ttm": None, "avg_3yr": None, "avg_5yr": None}

        ttm = owner_earnings_series[0][1] if owner_earnings_series else None
        vals = [v for _, v in owner_earnings_series]
        avg_3yr = statistics.mean(vals[:3]) if len(vals) >= 3 else statistics.mean(vals)
        avg_5yr = statistics.mean(vals[:5]) if len(vals) >= 5 else avg_3yr

        return {"ttm": ttm, "avg_3yr": avg_3yr, "avg_5yr": avg_5yr}

    def _estimate_growth_rates(
        self,
        fundamentals_by_year: dict,
        moat_score: float,
        revenue_cagr: Optional[float],
    ) -> dict:
        """
        Estimate conservative growth rates based on:
        - Historical revenue CAGR (hard evidence)
        - Moat strength (future sustainability)
        - We always shade DOWN from historical for conservatism
        """
        # Historical owner earnings growth
        years = sorted(fundamentals_by_year.keys())
        oe_series = []
        for y in years:
            f = fundamentals_by_year[y]
            if f.owner_earnings and f.owner_earnings > 0:
                oe_series.append(f.owner_earnings)

        if len(oe_series) >= 3:
            historical_oe_cagr = (oe_series[-1] / oe_series[0]) ** (1 / (len(oe_series) - 1)) - 1
        else:
            historical_oe_cagr = revenue_cagr or 0.05  # Fallback to revenue CAGR

        # Cap historical CAGR at reasonable levels for projection
        # Even the best businesses rarely sustain >20% growth for 10 years
        historical_capped = min(max(historical_oe_cagr, 0.0), 0.20)

        # Moat-adjusted growth: wide moat = more confidence in persistence
        moat_multiplier = 0.5 + (0.5 * moat_score)  # 0.5x to 1.0x

        base_growth = historical_capped * moat_multiplier

        return {
            "historical_oe_cagr": historical_oe_cagr,
            "bear": {
                "years_1_5":  max(base_growth * 0.50, 0.0),    # 50% of base
                "years_6_10": max(base_growth * 0.25, 0.0),    # Slower phase 2
                "terminal":   min(0.025, self.MAX_TERMINAL_GROWTH),
            },
            "base": {
                "years_1_5":  base_growth,
                "years_6_10": base_growth * 0.60,
                "terminal":   min(0.030, self.MAX_TERMINAL_GROWTH),
            },
            "bull": {
                "years_1_5":  min(base_growth * 1.30, 0.20),   # Cap at 20%
                "years_6_10": base_growth * 0.80,
                "terminal":   min(0.035, self.MAX_TERMINAL_GROWTH),
            },
        }

    def _run_dcf(
        self,
        owner_earnings: float,
        growth_1_5: float,
        growth_6_10: float,
        terminal_growth: float,
        discount_rate: float,
        shares_outstanding: float,
        scenario_name: str,
    ) -> DCFScenario:
        """
        10-year DCF with terminal value.
        Terminal Value = OE_year10 * (1 + g) / (WACC - g)  [Gordon Growth Model]
        """
        if discount_rate <= terminal_growth:
            # Mathematically invalid — WACC must exceed terminal growth
            logger.warning(f"DCF invalid: discount_rate ({discount_rate:.2%}) <= terminal_growth ({terminal_growth:.2%})")
            return DCFScenario(
                name=scenario_name,
                growth_rate_years_1_5=growth_1_5,
                growth_rate_years_6_10=growth_6_10,
                terminal_growth_rate=terminal_growth,
                discount_rate=discount_rate,
                intrinsic_value_per_share=None,
                total_intrinsic_value=None,
            )

        pv_cash_flows = 0.0
        current_oe = owner_earnings

        for year in range(1, 11):
            growth = growth_1_5 if year <= 5 else growth_6_10
            current_oe *= (1 + growth)
            discount_factor = (1 + discount_rate) ** year
            pv_cash_flows += current_oe / discount_factor

        # Terminal value (Gordon Growth Model on year 10 OE)
        terminal_value = (current_oe * (1 + terminal_growth)) / (discount_rate - terminal_growth)
        pv_terminal = terminal_value / ((1 + discount_rate) ** 10)

        total_intrinsic = pv_cash_flows + pv_terminal
        per_share = total_intrinsic / shares_outstanding if shares_outstanding and shares_outstanding > 0 else None

        logger.debug(
            f"DCF [{scenario_name}]: OE={owner_earnings/1e9:.2f}B, "
            f"g1={growth_1_5:.1%}, g2={growth_6_10:.1%}, "
            f"IV={total_intrinsic/1e9:.2f}B, "
            f"IV/share={f'{per_share:.2f}' if per_share is not None else 'N/A'}"
        )

        return DCFScenario(
            name=scenario_name,
            growth_rate_years_1_5=growth_1_5,
            growth_rate_years_6_10=growth_6_10,
            terminal_growth_rate=terminal_growth,
            discount_rate=discount_rate,
            intrinsic_value_per_share=per_share,
            total_intrinsic_value=total_intrinsic,
        )

    def estimate(
        self,
        ticker: str,
        fundamentals_by_year: dict,
        wacc: float,
        current_price: Optional[float],
        shares_outstanding: Optional[float],
        moat_score: float = 0.5,
        revenue_cagr: Optional[float] = None,
    ) -> IntrinsicValueResult:
        """
        Main entry point. Returns IntrinsicValueResult with all scenarios.
        """
        notes = []
        warnings = []

        # ── Normalize owner earnings ──────────────────────────
        oe_data = self._normalize_owner_earnings(fundamentals_by_year)
        oe_ttm = oe_data["ttm"]
        oe_3yr = oe_data["avg_3yr"]

        # Prefer 3-year average — more stable than single year
        # But use TTM if 3yr avg is wildly different (suggests structural change)
        if oe_3yr is not None and oe_ttm is not None:
            pct_diff = abs(oe_ttm - oe_3yr) / abs(oe_3yr) if oe_3yr != 0 else 0
            if pct_diff > 0.40:
                # TTM diverges significantly — use conservative (lower) of the two
                oe_used = min(oe_ttm, oe_3yr)
                notes.append(f"TTM owner earnings diverged {pct_diff:.0%} from 3yr avg — using lower")
                warnings.append("Large earnings change detected — validate before trading")
            else:
                oe_used = oe_3yr
                notes.append(f"Using 3yr average owner earnings: ${oe_used/1e9:.2f}B")
        elif oe_3yr:
            oe_used = oe_3yr
            notes.append(f"Using 3yr average owner earnings: ${oe_used/1e9:.2f}B")
        elif oe_ttm:
            oe_used = oe_ttm
            notes.append(f"Using TTM owner earnings (limited history): ${oe_used/1e9:.2f}B")
        else:
            warnings.append("Cannot compute owner earnings — missing net income, D&A, or capex")
            # Return a result with nulls
            null_scenario = DCFScenario("null", 0, 0, 0, wacc, None, None)
            return IntrinsicValueResult(
                ticker=ticker, current_price=current_price,
                shares_outstanding=shares_outstanding,
                owner_earnings_ttm=None, owner_earnings_3yr_avg=None,
                owner_earnings_used=None,
                bear=null_scenario, base=null_scenario, bull=null_scenario,
                intrinsic_value_conservative=None, intrinsic_value_base=None,
                margin_of_safety=None, is_undervalued=False,
                price_to_intrinsic=None, notes=notes, warnings=warnings,
            )

        # Reject negative owner earnings for DCF — use earnings power value instead
        if oe_used <= 0:
            warnings.append("Negative owner earnings — DCF not applicable. Company is burning cash.")
            null_scenario = DCFScenario("null", 0, 0, 0, wacc, None, None)
            return IntrinsicValueResult(
                ticker=ticker, current_price=current_price,
                shares_outstanding=shares_outstanding,
                owner_earnings_ttm=oe_ttm, owner_earnings_3yr_avg=oe_3yr,
                owner_earnings_used=oe_used,
                bear=null_scenario, base=null_scenario, bull=null_scenario,
                intrinsic_value_conservative=None, intrinsic_value_base=None,
                margin_of_safety=None, is_undervalued=False,
                price_to_intrinsic=None, notes=notes, warnings=warnings,
            )

        # ── Growth rate estimation ────────────────────────────
        growth_rates = self._estimate_growth_rates(fundamentals_by_year, moat_score, revenue_cagr)
        notes.append(f"Historical OE CAGR: {growth_rates['historical_oe_cagr']:.1%}")

        # ── Run DCF scenarios ─────────────────────────────────
        if not shares_outstanding:
            from loguru import logger as _lu
            _lu.warning(
                f"[IV] {ticker}: shares_outstanding missing — falling back to 1B, DCF unreliable"
            )
            shares = 1e9
            warnings.append("DCF unreliable: shares_outstanding missing, used 1B fallback — per-share IV may be 2–3× overstated for mega-caps")
        else:
            shares = shares_outstanding

        bear = self._run_dcf(
            owner_earnings=oe_used,
            growth_1_5=growth_rates["bear"]["years_1_5"],
            growth_6_10=growth_rates["bear"]["years_6_10"],
            terminal_growth=growth_rates["bear"]["terminal"],
            discount_rate=wacc * 1.02,   # Add 2% margin to discount rate in bear
            shares_outstanding=shares,
            scenario_name="bear",
        )

        base = self._run_dcf(
            owner_earnings=oe_used,
            growth_1_5=growth_rates["base"]["years_1_5"],
            growth_6_10=growth_rates["base"]["years_6_10"],
            terminal_growth=growth_rates["base"]["terminal"],
            discount_rate=wacc,
            shares_outstanding=shares,
            scenario_name="base",
        )

        bull = self._run_dcf(
            owner_earnings=oe_used,
            growth_1_5=growth_rates["bull"]["years_1_5"],
            growth_6_10=growth_rates["bull"]["years_6_10"],
            terminal_growth=growth_rates["bull"]["terminal"],
            discount_rate=wacc * 0.98,   # Slight discount rate reduction in bull
            shares_outstanding=shares,
            scenario_name="bull",
        )

        # ── Conservative intrinsic value = avg of bear + base ─
        iv_conservative = None
        if bear.intrinsic_value_per_share and base.intrinsic_value_per_share:
            iv_conservative = (bear.intrinsic_value_per_share + base.intrinsic_value_per_share) / 2

        iv_base = base.intrinsic_value_per_share

        # ── Margin of safety ──────────────────────────────────
        margin_of_safety = None
        is_undervalued = False
        price_to_intrinsic = None

        if current_price is not None and iv_conservative and iv_conservative > 0:
            margin_of_safety = (iv_conservative - current_price) / iv_conservative
            is_undervalued = margin_of_safety > 0
            price_to_intrinsic = current_price / iv_conservative

            if margin_of_safety >= 0.25:
                notes.append(f"Significant discount to intrinsic value: {margin_of_safety:.1%} margin of safety")
            elif margin_of_safety >= 0.15:
                notes.append(f"Adequate margin of safety: {margin_of_safety:.1%}")
            elif margin_of_safety > 0:
                notes.append(f"Thin margin of safety: {margin_of_safety:.1%} — proceed cautiously")
            else:
                notes.append(f"Trading at {-margin_of_safety:.1%} PREMIUM to intrinsic value")

        logger.info(
            f"[{ticker}] IV (conservative): ${f'{iv_conservative:.2f}' if iv_conservative else 'N/A'} | "
            f"Price: ${f'{current_price:.2f}' if current_price else 'N/A'} | "
            f"MoS: {f'{margin_of_safety:.1%}' if margin_of_safety is not None else 'N/A'}"
        )

        return IntrinsicValueResult(
            ticker=ticker,
            current_price=current_price,
            shares_outstanding=shares_outstanding,
            owner_earnings_ttm=oe_ttm,
            owner_earnings_3yr_avg=oe_3yr,
            owner_earnings_used=oe_used,
            bear=bear,
            base=base,
            bull=bull,
            intrinsic_value_conservative=iv_conservative,
            intrinsic_value_base=iv_base,
            margin_of_safety=margin_of_safety,
            is_undervalued=is_undervalued,
            price_to_intrinsic=price_to_intrinsic,
            notes=notes,
            warnings=warnings,
        )
