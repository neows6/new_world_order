"""
analysis/wacc.py — Weighted Average Cost of Capital estimator.

WACC = (E/V * Re) + (D/V * Rd * (1 - Tc))

Where:
  E  = Market value of equity (market cap)
  D  = Market value of debt
  V  = E + D (total capital)
  Re = Cost of equity (CAPM)
  Rd = Cost of debt (interest expense / total debt)
  Tc = Corporate tax rate

Cost of equity via CAPM:
  Re = Rf + Beta * (Rm - Rf)

  Rf   = Risk-free rate (10-yr Treasury yield)
  Beta = Systematic risk relative to S&P 500
  Rm   = Expected market return
"""

import statistics
from dataclasses import dataclass
from typing import Optional

from loguru import logger


# ── Constants ────────────────────────────────────────────────
RISK_FREE_RATE = 0.043          # ~4.3% 10-yr Treasury (update periodically)
MARKET_RISK_PREMIUM = 0.055     # Historical equity risk premium (~5.5%)
CORPORATE_TAX_RATE = 0.21       # US federal corporate tax rate
SP500_ANNUAL_RETURN = 0.10      # Long-run S&P 500 return assumption

# Sector beta defaults — used when we can't compute from price history
# Source: Damodaran NYU sector betas
SECTOR_BETA_DEFAULTS = {
    "Technology":           1.25,
    "Consumer Discretionary": 1.15,
    "Communication Services": 1.10,
    "Financials":           1.05,
    "Industrials":          1.00,
    "Healthcare":           0.85,
    "Materials":            1.00,
    "Energy":               1.10,
    "Consumer Staples":     0.65,
    "Utilities":            0.55,
    "Real Estate":          0.75,
    "default":              1.00,
}


@dataclass
class WACCResult:
    cost_of_equity: float
    cost_of_debt: float
    after_tax_cost_of_debt: float
    equity_weight: float
    debt_weight: float
    wacc: float
    beta: float
    risk_free_rate: float
    market_risk_premium: float
    notes: list


class WACCEstimator:
    """
    Estimates WACC for a company from price history and fundamentals.
    Falls back gracefully to sector defaults when data is sparse.
    """

    def estimate_beta(
        self,
        price_returns: list,        # Company daily returns (decimals)
        market_returns: list,       # S&P 500 daily returns for same period
    ) -> Optional[float]:
        """
        Compute beta via linear regression of stock vs market returns.
        Beta = Cov(stock, market) / Var(market)
        Minimum 60 observations needed for statistical reliability.
        """
        if len(price_returns) < 60 or len(market_returns) < 60:
            return None

        n = min(len(price_returns), len(market_returns))
        stock = price_returns[:n]
        market = market_returns[:n]

        try:
            mean_s = statistics.mean(stock)
            mean_m = statistics.mean(market)

            cov = sum((s - mean_s) * (m - mean_m) for s, m in zip(stock, market)) / (n - 1)
            var_m = statistics.variance(market)

            if var_m == 0:
                return None

            beta = cov / var_m
            # Clip to reasonable range — extreme betas are usually data artifacts
            beta = max(0.1, min(beta, 3.0))
            logger.debug(f"Computed beta: {beta:.3f} from {n} observations")
            return beta

        except statistics.StatisticsError as e:
            logger.warning(f"Beta computation failed: {e}")
            return None

    def estimate_cost_of_debt(
        self,
        interest_expense: Optional[float],
        total_debt: Optional[float],
        sector: Optional[str] = None,
    ) -> float:
        """
        Cost of debt = interest expense / total debt.
        Falls back to sector-based estimate if data unavailable.
        """
        if interest_expense is not None and total_debt is not None and total_debt > 0:
            rd = abs(interest_expense) / total_debt
            # Sanity check — Rd should be 2-15% for investment grade companies
            if 0.02 <= rd <= 0.15:
                return rd

        # Sector fallback estimates
        sector_defaults = {
            "Technology": 0.04,
            "Utilities": 0.045,
            "Financials": 0.035,
            "Healthcare": 0.04,
            "default": 0.05,
        }
        return sector_defaults.get(sector, sector_defaults["default"])

    def compute(
        self,
        market_cap: Optional[float],
        total_debt: Optional[float],
        interest_expense: Optional[float],
        sector: Optional[str] = None,
        beta_computed: Optional[float] = None,
        price_returns: Optional[list] = None,
        market_returns: Optional[list] = None,
    ) -> WACCResult:
        """
        Full WACC computation with graceful fallbacks.
        """
        notes = []

        # ── Beta ─────────────────────────────────────────────
        beta = beta_computed

        if beta is None and price_returns and market_returns:
            beta = self.estimate_beta(price_returns, market_returns)

        if beta is None:
            beta = SECTOR_BETA_DEFAULTS.get(sector, SECTOR_BETA_DEFAULTS["default"])
            notes.append(f"Beta: using sector default ({sector or 'default'}) = {beta}")
        else:
            notes.append(f"Beta: computed from price history = {beta:.3f}")

        # ── Cost of equity (CAPM) ─────────────────────────────
        re = RISK_FREE_RATE + beta * MARKET_RISK_PREMIUM
        notes.append(f"Cost of equity: {RISK_FREE_RATE:.1%} + {beta:.2f} × {MARKET_RISK_PREMIUM:.1%} = {re:.2%}")

        # ── Cost of debt ──────────────────────────────────────
        rd = self.estimate_cost_of_debt(interest_expense, total_debt, sector)
        rd_after_tax = rd * (1 - CORPORATE_TAX_RATE)
        notes.append(f"Cost of debt (after-tax): {rd:.2%} × (1 - {CORPORATE_TAX_RATE:.0%}) = {rd_after_tax:.2%}")

        # ── Capital weights ───────────────────────────────────
        market_cap = market_cap or 0
        total_debt = total_debt or 0
        total_capital = market_cap + total_debt

        if total_capital == 0:
            # Can't compute weights — assume all equity
            equity_weight, debt_weight = 1.0, 0.0
            notes.append("Warning: No capital data — assuming 100% equity financing")
        else:
            equity_weight = market_cap / total_capital
            debt_weight = total_debt / total_capital
            notes.append(f"Capital weights: equity={equity_weight:.1%}, debt={debt_weight:.1%}")

        # ── WACC ─────────────────────────────────────────────
        wacc = (equity_weight * re) + (debt_weight * rd_after_tax)
        notes.append(f"WACC = {wacc:.2%}")

        return WACCResult(
            cost_of_equity=re,
            cost_of_debt=rd,
            after_tax_cost_of_debt=rd_after_tax,
            equity_weight=equity_weight,
            debt_weight=debt_weight,
            wacc=wacc,
            beta=beta,
            risk_free_rate=RISK_FREE_RATE,
            market_risk_premium=MARKET_RISK_PREMIUM,
            notes=notes,
        )
