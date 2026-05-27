"""
analysis/data_validator.py — Claude-Powered Pre-Pipeline Data Validator.

═══════════════════════════════════════════════════════════════════════
WHY THIS EXISTS
═══════════════════════════════════════════════════════════════════════

The downstream pipeline (WACC, moat detector, DCF) is only as good as
its inputs. Past silent failures included:

  - CRM gross_margin parsed as 2.65 (265%) because quarterly revenue
    was paired with annual gross_profit during a Jan-31 fiscal year
  - MSFT market_cap = NULL in intraday records → WACC collapsed to
    debt-only cost → intrinsic value calculated as $1,506 vs real ~$213
  - ROIC values > 100% from near-zero denominators

These corrupt numbers flowed silently into Claude's thesis synthesis,
which then narrated them with confidence ("massive upside to $1,506!").

This module sits at the FRONT of the analysis pipeline and uses
Claude Haiku to inspect the input data BEFORE WACC/moat/IV run.

═══════════════════════════════════════════════════════════════════════
TWO-LAYER VALIDATION
═══════════════════════════════════════════════════════════════════════

1. DETERMINISTIC layer (always runs): checks physical impossibilities
     - Gross margin outside [-0.2, 1.0]
     - Market cap NULL when price × shares is computable
     - ROIC > 1.0 or < -1.0
     - Owner earnings flipping sign multiple times
     - Revenue declining >50% YoY (likely fiscal year mismatch)

2. CLAUDE layer (when API available): semantic anomaly detection
     - Inputs flagged as suspicious are sent to Claude Haiku
     - Claude returns {is_valid, anomalies[], suggested_fix}
     - Falls back to deterministic-only if API unavailable
     - Cached: only re-validates when source data hash changes

Output ValidationResult is consumed by analysis_pipeline.run_ticker()
which can: proceed normally, use sanitized fallback values, or skip
the ticker with a logged warning.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger

# Fix Windows SSL cert chain
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
_CACHE_FILE = ROOT / "data" / "validator_cache.json"


@dataclass
class ValidationResult:
    ticker: str
    is_valid: bool                          # OK to proceed with normal pipeline
    confidence: float                       # 0.0-1.0
    anomalies: list = field(default_factory=list)   # Human-readable issues found
    suggested_fix: str = ""                 # e.g. "use 3yr average for gross_margin"
    sanitized_data: dict = field(default_factory=dict)  # Cleaned input if validator could fix
    used_claude: bool = False               # True if Claude API was called
    notes: list = field(default_factory=list)


# ── Deterministic checks (fast, always run) ──────────────────────────────────

def _deterministic_check(ticker: str, fundamentals_by_year: dict,
                         current_price: Optional[float],
                         shares_outstanding: Optional[float],
                         market_cap: Optional[float]) -> tuple[list, dict]:
    """
    Returns (anomalies, sanitized_data).
    Anomalies are human-readable strings describing issues.
    Sanitized_data is the input with obvious bugs corrected when possible.
    """
    anomalies: list = []
    sanitized = {"fundamentals_by_year": dict(fundamentals_by_year)}

    # Market cap derivation (the MSFT bug)
    if market_cap is None and current_price and shares_outstanding:
        derived = current_price * shares_outstanding
        sanitized["market_cap"] = derived
        anomalies.append(
            f"market_cap=NULL — derived as {derived:,.0f} from price×shares"
        )
    else:
        sanitized["market_cap"] = market_cap

    # Fundamentals per-year checks
    bad_margin_years = []
    bad_roic_years   = []
    bad_owner_earnings_years = []
    prev_revenue = None
    revenue_drops = []
    owner_earnings_signs = []

    for year in sorted(fundamentals_by_year.keys()):
        f = fundamentals_by_year[year]
        gm = getattr(f, "gross_margin", None)
        roic = getattr(f, "roic", None)
        oe  = getattr(f, "owner_earnings", None)
        rev = getattr(f, "revenue", None)

        # Gross margin: must be in [-0.2, 1.0]
        if gm is not None and (gm > 1.0 or gm < -0.2):
            bad_margin_years.append((year, gm))

        # ROIC: real businesses don't have ROIC > 100% or < -100%
        if roic is not None and (roic > 1.0 or roic < -1.0):
            bad_roic_years.append((year, roic))

        # Owner earnings sign tracking
        if oe is not None:
            owner_earnings_signs.append((year, 1 if oe > 0 else -1))

        # Revenue drop check
        if prev_revenue and rev and prev_revenue > 0:
            change = (rev - prev_revenue) / prev_revenue
            if change < -0.50:
                revenue_drops.append((year, change))
        if rev:
            prev_revenue = rev

    if bad_margin_years:
        anomalies.append(
            f"impossible gross_margin in {len(bad_margin_years)} year(s): "
            + ", ".join(f"{y}={v:.2f}" for y, v in bad_margin_years[:3])
        )

    if bad_roic_years:
        anomalies.append(
            f"impossible ROIC in {len(bad_roic_years)} year(s): "
            + ", ".join(f"{y}={v:.2f}" for y, v in bad_roic_years[:3])
        )

    # Owner earnings sign flips
    if len(owner_earnings_signs) >= 3:
        flips = sum(1 for i in range(1, len(owner_earnings_signs))
                    if owner_earnings_signs[i][1] != owner_earnings_signs[i-1][1])
        if flips >= 2:
            anomalies.append(
                f"owner_earnings sign flipped {flips}× across {len(owner_earnings_signs)} years "
                f"— DCF assumes stability"
            )

    if revenue_drops:
        anomalies.append(
            f"revenue dropped >50% in {len(revenue_drops)} year(s) — possible fiscal year parsing mismatch"
        )

    return anomalies, sanitized


# ── Claude semantic layer ────────────────────────────────────────────────────

def _claude_validate(ticker: str, fundamentals_summary: dict,
                     anomalies_so_far: list) -> Optional[dict]:
    """
    Call Claude Haiku for semantic validation. Returns parsed JSON or None.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        # Try config
        try:
            from config import config
            api_key = config.brief.anthropic_api_key
        except Exception:
            pass
    if not api_key:
        return None

    try:
        import anthropic
        from utils.ssl_context import make_httpx_client
        http_client = make_httpx_client(timeout=15.0)
        client = anthropic.Anthropic(api_key=api_key, http_client=http_client)

        prompt = (
            f"You are a financial data quality auditor. Review this 5-year "
            f"fundamentals series for {ticker} and identify any values that look "
            f"like data errors (parsing mismatches, NULL-substitution bugs, etc.) "
            f"rather than real business performance.\n\n"
            f"Deterministic checks already flagged:\n"
            + "\n".join(f"  - {a}" for a in anomalies_so_far)
            + "\n\nFundamentals:\n"
            + json.dumps(fundamentals_summary, indent=2, default=str)
            + "\n\nRespond with STRICT JSON only:\n"
            + '{"is_valid": bool, "additional_anomalies": ["..."], "suggested_fix": "..."}'
        )

        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        # Strip code fences if present
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())
    except Exception as exc:
        logger.warning(f"[VALIDATOR] Claude call failed for {ticker}: {exc}")
        return None


# ── Caching ───────────────────────────────────────────────────────────────────

def _hash_inputs(ticker: str, fundamentals_by_year: dict) -> str:
    """Stable hash of inputs so we only re-validate when data changes."""
    snapshot = []
    for y in sorted(fundamentals_by_year.keys()):
        f = fundamentals_by_year[y]
        snapshot.append([
            y,
            getattr(f, "gross_margin", None),
            getattr(f, "roic", None),
            getattr(f, "owner_earnings", None),
            getattr(f, "revenue", None),
            getattr(f, "net_income", None),
        ])
    raw = json.dumps([ticker, snapshot], sort_keys=True, default=str)
    return hashlib.md5(raw.encode()).hexdigest()


def _load_cache() -> dict:
    try:
        if _CACHE_FILE.exists():
            return json.loads(_CACHE_FILE.read_text())
    except Exception:
        pass
    return {}


def _save_cache(cache: dict) -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(cache, indent=2))
    except Exception as exc:
        logger.warning(f"[VALIDATOR] Cache write failed: {exc}")


# ── Public API ────────────────────────────────────────────────────────────────

def validate_fundamentals(ticker: str,
                          fundamentals_by_year: dict,
                          current_price: Optional[float] = None,
                          shares_outstanding: Optional[float] = None,
                          market_cap: Optional[float] = None,
                          use_claude: bool = True) -> ValidationResult:
    """
    Main entry point. Returns ValidationResult.

    The pipeline should:
      - Use result.sanitized_data when fields are corrected
      - Skip ticker if result.is_valid is False AND severe anomalies present
      - Log result.anomalies for observability
    """
    # Deterministic pass — always runs
    anomalies, sanitized = _deterministic_check(
        ticker, fundamentals_by_year, current_price, shares_outstanding, market_cap
    )

    # No anomalies, no need for Claude
    if not anomalies:
        return ValidationResult(
            ticker=ticker,
            is_valid=True,
            confidence=1.0,
            sanitized_data=sanitized,
            notes=["No anomalies detected"],
        )

    # Cache lookup — skip Claude if we've already validated this exact input
    input_hash = _hash_inputs(ticker, fundamentals_by_year)
    cache = _load_cache()
    cached = cache.get(ticker, {})
    if cached.get("hash") == input_hash and cached.get("result"):
        r = cached["result"]
        return ValidationResult(
            ticker=ticker,
            is_valid=r.get("is_valid", True),
            confidence=r.get("confidence", 0.7),
            anomalies=r.get("anomalies", anomalies),
            suggested_fix=r.get("suggested_fix", ""),
            sanitized_data=sanitized,
            used_claude=r.get("used_claude", False),
            notes=["Loaded from validator cache"],
        )

    # Build compact summary for Claude
    summary = {}
    for y in sorted(fundamentals_by_year.keys()):
        f = fundamentals_by_year[y]
        summary[str(y)] = {
            "gross_margin":   getattr(f, "gross_margin", None),
            "roic":           getattr(f, "roic", None),
            "owner_earnings": getattr(f, "owner_earnings", None),
            "revenue":        getattr(f, "revenue", None),
            "net_income":     getattr(f, "net_income", None),
        }

    claude_result = None
    if use_claude:
        claude_result = _claude_validate(ticker, summary, anomalies)

    if claude_result:
        additional = claude_result.get("additional_anomalies", []) or []
        anomalies = list(anomalies) + list(additional)
        is_valid = bool(claude_result.get("is_valid", True))
        suggested_fix = claude_result.get("suggested_fix", "")
        confidence = 0.85
        used_claude = True
    else:
        # Deterministic only — be conservative
        # If we found impossible values, mark as not-fully-valid but salvageable
        severe = any("impossible" in a for a in anomalies)
        is_valid = not severe
        suggested_fix = "use 3yr average to dampen anomalies" if anomalies else ""
        confidence = 0.6
        used_claude = False

    # Persist cache
    cache[ticker] = {
        "hash": input_hash,
        "ts": datetime.utcnow().isoformat(),
        "result": {
            "is_valid": is_valid,
            "confidence": confidence,
            "anomalies": anomalies,
            "suggested_fix": suggested_fix,
            "used_claude": used_claude,
        }
    }
    _save_cache(cache)

    logger.info(
        f"[VALIDATOR] {ticker}: valid={is_valid} conf={confidence:.2f} "
        f"anomalies={len(anomalies)} claude={used_claude}"
    )

    return ValidationResult(
        ticker=ticker,
        is_valid=is_valid,
        confidence=confidence,
        anomalies=anomalies,
        suggested_fix=suggested_fix,
        sanitized_data=sanitized,
        used_claude=used_claude,
    )
