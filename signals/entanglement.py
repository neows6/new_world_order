"""
signals/entanglement.py — Cross-Ticker Quantum Entanglement Detector.

═══════════════════════════════════════════════════════════════════════
THE PHYSICS ANALOG
═══════════════════════════════════════════════════════════════════════

In quantum mechanics, entangled particles maintain correlations regardless
of distance. When you observe one, you instantly "know" the state of the
other. This isn't faster-than-light communication — it's information that
was always shared.

In markets, certain ticker pairs are "entangled" via shared exposures:
  - NVDA ↔ AMD (AI/GPU demand)
  - AAPL ↔ MSFT (mega-cap tech beta)
  - XLK ↔ QQQ (broad tech)
  - JPM ↔ BAC (large-cap banks)

These entanglements appear as high rolling correlations (ρ > 0.50).

When the entanglement BREAKS suddenly (decoherence event), one ticker
has received new information the other hasn't propagated yet. The
lagging ticker is statistically likely to "catch up" — this is a
LEADING INDICATOR.

═══════════════════════════════════════════════════════════════════════
DETECTION METHOD
═══════════════════════════════════════════════════════════════════════

1. Compute 30-day rolling Pearson correlation ρ_ij between all watchlist pairs
2. Compute Δρ_ij = ρ_today - ρ_yesterday, and its z-score across all pairs
3. Decoherence event when:
     |Δρ_ij| > 2.5σ        (the correlation broke abruptly)
   AND
     |ρ_30d_ago| > 0.50     (they used to be strongly entangled)
4. The LEAD is whoever had the larger recent return magnitude
5. The LAG is the catch-up trade candidate

Decoherence events are persisted to data/entanglement_events.json and
exposed via API for the Claude Live Sentinel to act on.
"""

from __future__ import annotations

import json
import math
import statistics
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
_EVENTS_FILE = ROOT / "data" / "entanglement_events.json"
_STATE_FILE  = ROOT / "data" / "entanglement_state.json"

# Detection thresholds
MIN_HISTORICAL_CORR = 0.50    # Pair must have been entangled to count
DECOHERENCE_Z       = 2.5     # Z-score threshold for break detection
MIN_BARS            = 25      # Need at least 25 daily bars for stable correlation


@dataclass
class EntanglementEvent:
    timestamp: str
    lead_ticker: str
    lag_ticker: str
    historical_corr: float       # Correlation 30 days ago
    current_corr: float          # Correlation now
    delta_corr: float            # Change
    z_score: float               # How abnormal the break is
    lead_move_pct: float         # Lead ticker's recent (5-day) return
    lag_move_pct: float          # Lag ticker's recent (5-day) return
    predicted_lag_move_pct: float # If lag catches up to lead's behavior
    confidence: float            # 0.0-1.0 — bounded by z-score and historical strength

    def to_dict(self) -> dict:
        return asdict(self)


# ── Pure math helpers ─────────────────────────────────────────────────────────

def _pearson(x: list[float], y: list[float]) -> Optional[float]:
    """Pearson correlation coefficient. Returns None on degenerate input."""
    n = len(x)
    if n != len(y) or n < 3:
        return None
    mx = sum(x) / n
    my = sum(y) / n
    sx2 = sum((xi - mx) ** 2 for xi in x)
    sy2 = sum((yi - my) ** 2 for yi in y)
    if sx2 <= 0 or sy2 <= 0:
        return None
    sxy = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    return sxy / math.sqrt(sx2 * sy2)


def _returns(closes: list[float]) -> list[float]:
    """Daily returns from closing price series."""
    if len(closes) < 2:
        return []
    return [(closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes))
            if closes[i - 1] > 0]


# ── Core engine ───────────────────────────────────────────────────────────────

class EntanglementEngine:
    """
    Maintains rolling correlation matrix across a ticker universe.
    Detects decoherence events — sudden correlation breaks between
    historically-entangled pairs.

    Usage:
        engine = EntanglementEngine()
        events = engine.scan(price_series_by_ticker)
        for ev in events:
            print(f"{ev.lead_ticker} broke from {ev.lag_ticker}")
    """

    def __init__(self,
                 window: int = 30,
                 lookback: int = 60,
                 min_corr: float = MIN_HISTORICAL_CORR,
                 z_threshold: float = DECOHERENCE_Z):
        self.window      = window          # rolling correlation window (days)
        self.lookback    = lookback        # how far back to compare (days)
        self.min_corr    = min_corr
        self.z_threshold = z_threshold

        # In-memory event ring buffer
        self._events: deque = deque(maxlen=200)
        self._last_state: dict = {}
        self._lock = threading.Lock()

    # ── Public API ─────────────────────────────────────────────

    def scan(self, prices_by_ticker: dict[str, list[float]]) -> list[EntanglementEvent]:
        """
        Compute pairwise correlations and detect decoherence events.
        prices_by_ticker: {ticker: list of daily closes, oldest first}
        Requires at least (window + lookback) bars per ticker.
        """
        # Filter to tickers with sufficient data
        usable = {t: p for t, p in prices_by_ticker.items()
                  if p and len(p) >= self.window + self.lookback}
        if len(usable) < 2:
            logger.debug(f"[ENTANGLE] Need 2+ tickers with {self.window + self.lookback}+ bars")
            return []

        # Compute returns once per ticker
        returns_by_ticker = {t: _returns(p) for t, p in usable.items()}

        # Recent correlation window (last `window` returns)
        # Historical correlation window (the `window` returns ending `lookback` days ago)
        all_pairs = []
        deltas    = []
        pair_data = []

        tickers = sorted(usable.keys())
        for i, a in enumerate(tickers):
            for b in tickers[i + 1:]:
                ra = returns_by_ticker[a]
                rb = returns_by_ticker[b]
                # Align lengths
                n = min(len(ra), len(rb))
                if n < self.window + self.lookback:
                    continue
                ra = ra[-(self.window + self.lookback):]
                rb = rb[-(self.window + self.lookback):]

                hist_a = ra[:self.window]
                hist_b = rb[:self.window]
                curr_a = ra[-self.window:]
                curr_b = rb[-self.window:]

                hist_corr = _pearson(hist_a, hist_b)
                curr_corr = _pearson(curr_a, curr_b)
                if hist_corr is None or curr_corr is None:
                    continue

                delta = curr_corr - hist_corr
                deltas.append(delta)
                pair_data.append({
                    "a": a, "b": b,
                    "hist": hist_corr, "curr": curr_corr, "delta": delta,
                    "ra": ra, "rb": rb,
                })

        if len(deltas) < 5:
            return []

        # Z-score of deltas — what's an "abnormal" correlation break right now
        mu_d = statistics.mean(deltas)
        sd_d = statistics.stdev(deltas) if len(deltas) > 1 else 0.0
        if sd_d <= 0:
            return []

        events: list[EntanglementEvent] = []
        ts = datetime.utcnow().isoformat()

        for pd in pair_data:
            z = (pd["delta"] - mu_d) / sd_d
            # Decoherence: large NEGATIVE z (correlation dropped a lot) AND was strongly entangled
            if z < -self.z_threshold and abs(pd["hist"]) >= self.min_corr:
                # Determine lead vs lag — the one with the larger recent move is the lead
                recent_a = pd["ra"][-5:]   # last 5 daily returns
                recent_b = pd["rb"][-5:]
                move_a = (1.0 + sum(recent_a) / len(recent_a)) ** 5 - 1.0 if recent_a else 0.0
                move_b = (1.0 + sum(recent_b) / len(recent_b)) ** 5 - 1.0 if recent_b else 0.0

                if abs(move_a) >= abs(move_b):
                    lead, lag = pd["a"], pd["b"]
                    lead_move, lag_move = move_a, move_b
                else:
                    lead, lag = pd["b"], pd["a"]
                    lead_move, lag_move = move_b, move_a

                # Predicted catch-up: lag should move toward lead × historical correlation
                # If they're 0.78 correlated and lead moved +2%, lag "should" be at +1.56%
                predicted_lag_move = lead_move * pd["hist"]
                catch_up_required = predicted_lag_move - lag_move

                # Confidence: stronger historical entanglement + larger z = higher confidence
                conf = min(1.0, abs(pd["hist"]) * min(abs(z) / 5.0, 1.0))

                ev = EntanglementEvent(
                    timestamp=ts,
                    lead_ticker=lead,
                    lag_ticker=lag,
                    historical_corr=round(pd["hist"], 4),
                    current_corr=round(pd["curr"], 4),
                    delta_corr=round(pd["delta"], 4),
                    z_score=round(z, 3),
                    lead_move_pct=round(lead_move * 100, 3),
                    lag_move_pct=round(lag_move * 100, 3),
                    predicted_lag_move_pct=round(catch_up_required * 100, 3),
                    confidence=round(conf, 3),
                )
                events.append(ev)

        with self._lock:
            for ev in events:
                self._events.append(ev)
            self._last_state = {
                "computed_at": ts,
                "pairs_analyzed": len(pair_data),
                "events_found": len(events),
                "mean_delta": round(mu_d, 4),
                "sd_delta": round(sd_d, 4),
            }

        if events:
            logger.info(f"[ENTANGLE] Detected {len(events)} decoherence event(s): "
                        + ", ".join(f"{e.lead_ticker}→{e.lag_ticker}(z={e.z_score:.1f})"
                                    for e in events[:5]))

        return events

    def recent_events(self, limit: int = 50) -> list[dict]:
        """Most recent N events."""
        with self._lock:
            return [e.to_dict() for e in list(self._events)[-limit:]]

    def state(self) -> dict:
        """Latest scan diagnostic state."""
        with self._lock:
            return dict(self._last_state)

    def persist(self) -> None:
        """Write events + state to disk."""
        with self._lock:
            payload = {
                "state":  dict(self._last_state),
                "events": [e.to_dict() for e in list(self._events)],
            }
        try:
            _EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
            _EVENTS_FILE.write_text(json.dumps(payload, indent=2))
        except Exception as exc:
            logger.warning(f"[ENTANGLE] Persist failed: {exc}")


# ── Singleton + background runner ─────────────────────────────────────────────

_engine_singleton: Optional[EntanglementEngine] = None
_runner_started = False


def get_engine() -> EntanglementEngine:
    global _engine_singleton
    if _engine_singleton is None:
        _engine_singleton = EntanglementEngine()
    return _engine_singleton


def _load_tickers_from_stagegate() -> list[str]:
    """Pull active ticker universe from stagegate.json (S1 ∪ S2 ∪ S3)."""
    try:
        sg = json.loads((ROOT / "data" / "stagegate.json").read_text())
        return sorted(set(sg.get("stage1", []) + sg.get("stage2", []) + sg.get("stage3", [])))
    except Exception:
        return []


def _fetch_price_series(tickers: list[str], days: int = 90) -> dict[str, list[float]]:
    """
    Pull daily closes from DB price_history for each ticker.
    Returns {ticker: [closes oldest-first]}.
    """
    from sqlalchemy import create_engine, and_
    from sqlalchemy.orm import sessionmaker
    from models.database import Company, PriceHistory
    from config import config

    eng = create_engine(config.database.url)
    Session = sessionmaker(bind=eng)
    out: dict[str, list[float]] = {}

    with Session() as session:
        cutoff = datetime.utcnow() - timedelta(days=days * 2)  # buffer for non-trading days
        for t in tickers:
            company = session.query(Company).filter(Company.ticker == t).first()
            if not company:
                continue
            records = (session.query(PriceHistory)
                       .filter(and_(PriceHistory.company_id == company.id,
                                    PriceHistory.date >= cutoff))
                       .order_by(PriceHistory.date.asc())
                       .all())
            closes = [r.adjusted_close or r.close for r in records
                      if (r.adjusted_close or r.close)]
            if closes:
                out[t] = closes
    return out


def run_scan_once() -> list[dict]:
    """Pull current universe and run one scan. Returns event dicts."""
    tickers = _load_tickers_from_stagegate()
    if len(tickers) < 2:
        logger.debug(f"[ENTANGLE] Stagegate has <2 tickers ({tickers}) — skipping scan")
        return []

    prices = _fetch_price_series(tickers, days=90)
    if len(prices) < 2:
        logger.debug(f"[ENTANGLE] Only {len(prices)} tickers have usable price history")
        return []

    engine = get_engine()
    events = engine.scan(prices)
    engine.persist()
    return [e.to_dict() for e in events]


def _runner_loop(interval_seconds: int = 60):
    """Background thread: scan every `interval_seconds` during market hours."""
    logger.info(f"[ENTANGLE] Background runner started (interval={interval_seconds}s)")
    while True:
        try:
            now = datetime.now()
            # Market hours only: Mon-Fri, 9:30-16:00 ET (approximated as 13:30-20:00 UTC)
            is_weekday = now.weekday() < 5
            hour_utc   = datetime.utcnow().hour
            if is_weekday and 13 <= hour_utc <= 20:
                run_scan_once()
            else:
                # Outside market hours — slow polling
                time.sleep(300)
                continue
        except Exception as exc:
            logger.error(f"[ENTANGLE] Scan loop error: {exc}")
        time.sleep(interval_seconds)


def start_background_runner(interval_seconds: int = 60) -> None:
    """Spawn background scan thread. Idempotent."""
    global _runner_started
    if _runner_started:
        return
    _runner_started = True
    t = threading.Thread(target=_runner_loop, args=(interval_seconds,),
                         daemon=True, name="entanglement-runner")
    t.start()
    logger.info("[ENTANGLE] Background runner thread launched")
