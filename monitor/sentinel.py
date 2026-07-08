"""
monitor/sentinel.py — Claude Live Sentinel.

═══════════════════════════════════════════════════════════════════════
THE UPSTREAM CLAUDE LOOP
═══════════════════════════════════════════════════════════════════════

Until now Claude has only narrated AFTER the pipeline ran. This module
inverts that: Claude reads novel-math events (entanglement breaks) plus
the live tape, and produces PREDICTIVE judgments in real time.

The loop:

  Every 30 seconds during market hours
  ↓
  Drain new events from EntanglementEngine
  ↓
  If accumulated_events >= 1 → trigger Claude
  ↓
  Claude reads structured event packet + live prices + current signals
  ↓
  Returns {alert_level, primary_ticker, action, reasoning, time_horizon}
  ↓
  Alert posted to in-memory ring buffer + persisted
  ↓
  /sentinel dashboard tab renders the alert stream

The math modules detect WHAT happened.
Claude interprets WHAT IT MEANS for the user's specific watchlist.

Routes:
  GET    /sentinel                Alert-stream HTML page
  GET    /api/sentinel/alerts     Latest alerts (JSON)
  GET    /api/sentinel/status     Queue, budget, last call timestamp
  POST   /api/sentinel/pause      Toggle pause flag
  POST   /api/sentinel/trigger    Force a Claude judgment now (manual test)
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse
from loguru import logger

ROOT = Path(__file__).resolve().parent.parent
_ALERTS_FILE = ROOT / "data" / "sentinel_alerts.json"
_PAUSE_FILE  = ROOT / "data" / "sentinel_paused.flag"
_BUDGET_FILE = ROOT / "data" / "sentinel_budget.json"

sentinel_router = APIRouter()

# Cost estimates for budget tracking (Haiku 4.5 pricing per million tokens)
_INPUT_PRICE_PER_M  = 1.00
_OUTPUT_PRICE_PER_M = 5.00


@dataclass
class SentinelAlert:
    timestamp: str
    alert_level: str          # "info" | "warn" | "high"
    primary_ticker: str
    action: str               # e.g. "watch_for_entry", "exit", "wait"
    reasoning: str
    time_horizon_minutes: int
    confidence: float
    triggered_by: list        # event type identifiers
    api_cost_usd: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


# ── State ─────────────────────────────────────────────────────────────────────

_alerts: deque = deque(maxlen=200)
_state_lock = threading.Lock()
_last_claude_call: Optional[str] = None
_last_event_seen_idx = 0
_runner_started = False


def _is_paused() -> bool:
    return _PAUSE_FILE.exists()


def _load_budget() -> dict:
    try:
        if _BUDGET_FILE.exists():
            data = json.loads(_BUDGET_FILE.read_text())
            # Reset if it's a new UTC day
            if data.get("date") != datetime.utcnow().strftime("%Y-%m-%d"):
                return {"date": datetime.utcnow().strftime("%Y-%m-%d"),
                        "calls": 0, "cost_usd": 0.0}
            return data
    except Exception:
        pass
    return {"date": datetime.utcnow().strftime("%Y-%m-%d"),
            "calls": 0, "cost_usd": 0.0}


def _save_budget(b: dict) -> None:
    try:
        _BUDGET_FILE.parent.mkdir(parents=True, exist_ok=True)
        _BUDGET_FILE.write_text(json.dumps(b, indent=2))
    except Exception:
        pass


def _persist_alerts() -> None:
    try:
        _ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _state_lock:
            payload = {
                "updated_at": datetime.utcnow().isoformat(),
                "alerts": [a.to_dict() for a in list(_alerts)],
            }
        _ALERTS_FILE.write_text(json.dumps(payload, indent=2))
    except Exception as exc:
        logger.warning(f"[SENTINEL] Alert persist failed: {exc}")


def _load_existing_alerts() -> None:
    """Hydrate ring buffer from disk on startup."""
    try:
        if _ALERTS_FILE.exists():
            data = json.loads(_ALERTS_FILE.read_text())
            for a in data.get("alerts", []):
                _alerts.append(SentinelAlert(**a))
            logger.info(f"[SENTINEL] Loaded {len(_alerts)} existing alerts from disk")
    except Exception as exc:
        logger.warning(f"[SENTINEL] Alert load failed: {exc}")


# ── Claude invocation ─────────────────────────────────────────────────────────

def _claude_judge(events: list[dict], live_prices: dict,
                  current_signals: dict) -> Optional[SentinelAlert]:
    """
    Send events + live tape to Claude, parse alert response.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        try:
            from config import config
            api_key = config.brief.anthropic_api_key
        except Exception:
            pass
    if not api_key:
        logger.debug("[SENTINEL] No Anthropic API key — skipping Claude judgment")
        return None

    try:
        import anthropic
        from utils.ssl_context import make_httpx_client
        # Custom httpx client handles Norton-AV SSL inspection on this machine
        http_client = make_httpx_client(timeout=30.0)
        client = anthropic.Anthropic(api_key=api_key, http_client=http_client)

        prompt = (
            "You are a real-time market sentinel for a quant trading system. "
            "Cross-ticker correlation breaks (decoherence events) have been detected. "
            "These often precede a 'catch-up' move in the lagging ticker.\n\n"
            "Review the events below and produce ONE actionable alert for the "
            "most interesting setup. Be terse, specific, and honest about confidence.\n\n"
            f"## Events ({len(events)} total)\n"
            + json.dumps(events[:10], indent=2)
            + "\n\n## Live Prices\n"
            + json.dumps(live_prices, indent=2)
            + "\n\n## Current Pipeline Signals\n"
            + json.dumps(current_signals, indent=2)
            + "\n\nReturn STRICT JSON only:\n"
            '{\n'
            '  "alert_level": "info" | "warn" | "high",\n'
            '  "primary_ticker": "<the most actionable ticker>",\n'
            '  "action": "watch_for_entry" | "watch_for_exit" | "wait" | "investigate",\n'
            '  "reasoning": "<one sentence, max 30 words, why this setup matters>",\n'
            '  "time_horizon_minutes": <integer, how long this signal is valid>,\n'
            '  "confidence": <0.0-1.0>\n'
            '}\n'
        )

        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        data = json.loads(text.strip())

        # Track budget
        usage = getattr(msg, "usage", None)
        in_tok = getattr(usage, "input_tokens", 0) if usage else 0
        out_tok = getattr(usage, "output_tokens", 0) if usage else 0
        cost = (in_tok / 1e6) * _INPUT_PRICE_PER_M + (out_tok / 1e6) * _OUTPUT_PRICE_PER_M
        budget = _load_budget()
        budget["calls"] = budget.get("calls", 0) + 1
        budget["cost_usd"] = round(budget.get("cost_usd", 0.0) + cost, 5)
        _save_budget(budget)

        return SentinelAlert(
            timestamp=datetime.utcnow().isoformat(),
            alert_level=str(data.get("alert_level", "info")),
            primary_ticker=str(data.get("primary_ticker", "?")),
            action=str(data.get("action", "wait")),
            reasoning=str(data.get("reasoning", "")),
            time_horizon_minutes=int(data.get("time_horizon_minutes", 30)),
            confidence=float(data.get("confidence", 0.5)),
            triggered_by=[e.get("type", "unknown") for e in events[:5]],
            api_cost_usd=round(cost, 5),
        )

    except Exception as exc:
        logger.warning(f"[SENTINEL] Claude judgment failed: {exc}")
        return None


# ── Background runner ─────────────────────────────────────────────────────────

def _gather_live_context(event_tickers: set[str]) -> tuple[dict, dict]:
    """Pull current prices and signals for tickers referenced in events."""
    live_prices: dict = {}
    current_signals: dict = {}
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from models.database import Company, PriceHistory, TradeSignal
        from config import config

        eng = create_engine(config.database.url)
        Session = sessionmaker(bind=eng)
        with Session() as session:
            for t in event_tickers:
                company = session.query(Company).filter(Company.ticker == t).first()
                if not company:
                    continue
                latest_price = (session.query(PriceHistory)
                                .filter(PriceHistory.company_id == company.id)
                                .order_by(PriceHistory.date.desc())
                                .first())
                if latest_price:
                    live_prices[t] = round(latest_price.adjusted_close or latest_price.close or 0.0, 2)

                latest_sig = (session.query(TradeSignal)
                              .filter(TradeSignal.ticker == t)
                              .order_by(TradeSignal.generated_at.desc())
                              .first())
                if latest_sig:
                    current_signals[t] = {
                        "signal": latest_sig.signal,
                        "composite": round(latest_sig.composite_score or 0.0, 3),
                        "confidence": round(latest_sig.confidence or 0.0, 3),
                    }
    except Exception as exc:
        logger.warning(f"[SENTINEL] Context gather failed: {exc}")
    return live_prices, current_signals


def _runner_loop(interval_seconds: int = 30):
    """
    Background thread: drains entanglement events, calls Claude when there's
    something to judge.
    """
    global _last_event_seen_idx, _last_claude_call
    logger.info(f"[SENTINEL] Background runner started (poll={interval_seconds}s)")
    try:
        from signals.entanglement import get_engine
    except Exception as exc:
        logger.error(f"[SENTINEL] Cannot import entanglement engine: {exc}")
        return

    while True:
        try:
            if _is_paused():
                time.sleep(interval_seconds)
                continue

            # Market-hours gate (approx 13:30-20:00 UTC = 9:30-16:00 ET)
            now = datetime.utcnow()
            if now.weekday() >= 5 or not (13 <= now.hour <= 20):
                time.sleep(120)
                continue

            engine = get_engine()
            all_events = engine.recent_events(limit=200)
            new_events = all_events[_last_event_seen_idx:]
            _last_event_seen_idx = len(all_events)

            if not new_events:
                time.sleep(interval_seconds)
                continue

            # Gather live context for tickers in events
            tickers = set()
            for ev in new_events:
                tickers.add(ev.get("lead_ticker", ""))
                tickers.add(ev.get("lag_ticker", ""))
            tickers.discard("")

            live_prices, current_signals = _gather_live_context(tickers)

            # Wrap events with type tag so Claude knows what they are
            tagged_events = [{**ev, "type": "entanglement_break"} for ev in new_events]

            alert = _claude_judge(tagged_events, live_prices, current_signals)
            if alert:
                with _state_lock:
                    _alerts.append(alert)
                _persist_alerts()
                _last_claude_call = datetime.utcnow().isoformat()
                logger.info(
                    f"[SENTINEL] Alert [{alert.alert_level}] {alert.primary_ticker}: "
                    f"{alert.action} — {alert.reasoning[:80]}"
                )

        except Exception as exc:
            logger.error(f"[SENTINEL] Runner loop error: {exc}")

        time.sleep(interval_seconds)


def start_background_runner(interval_seconds: int = 30) -> None:
    global _runner_started
    if _runner_started:
        return
    _runner_started = True
    _load_existing_alerts()
    t = threading.Thread(target=_runner_loop, args=(interval_seconds,),
                         daemon=True, name="sentinel-runner")
    t.start()
    logger.info("[SENTINEL] Background runner thread launched")


# ── API endpoints ─────────────────────────────────────────────────────────────

@sentinel_router.get("/api/sentinel/alerts")
def api_sentinel_alerts(limit: int = 50):
    with _state_lock:
        recent = list(_alerts)[-limit:]
    return JSONResponse({"alerts": [a.to_dict() for a in reversed(recent)]})


@sentinel_router.get("/api/sentinel/status")
def api_sentinel_status():
    budget = _load_budget()
    try:
        from signals.entanglement import get_engine
        eng_state = get_engine().state()
    except Exception:
        eng_state = {}
    with _state_lock:
        alert_count = len(_alerts)
    return JSONResponse({
        "paused":            _is_paused(),
        "last_claude_call":  _last_claude_call,
        "alert_count":       alert_count,
        "engine_state":      eng_state,
        "budget":            budget,
    })


@sentinel_router.post("/api/sentinel/pause")
def api_sentinel_pause():
    if _PAUSE_FILE.exists():
        _PAUSE_FILE.unlink()
        return JSONResponse({"status": "resumed"})
    _PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PAUSE_FILE.write_text(datetime.utcnow().isoformat())
    return JSONResponse({"status": "paused"})


@sentinel_router.post("/api/sentinel/trigger")
def api_sentinel_trigger():
    """Manual trigger — useful for testing without waiting for events."""
    try:
        from signals.entanglement import get_engine, run_scan_once
        # First scan to populate events
        run_scan_once()
        engine = get_engine()
        events = engine.recent_events(limit=10)
        if not events:
            return JSONResponse({"status": "no_events_to_judge"})
        tickers = set()
        for ev in events:
            tickers.add(ev.get("lead_ticker", ""))
            tickers.add(ev.get("lag_ticker", ""))
        tickers.discard("")
        live_prices, current_signals = _gather_live_context(tickers)
        tagged = [{**ev, "type": "entanglement_break"} for ev in events]
        alert = _claude_judge(tagged, live_prices, current_signals)
        if alert:
            with _state_lock:
                _alerts.append(alert)
            _persist_alerts()
            return JSONResponse({"status": "alert_generated", "alert": alert.to_dict()})
        return JSONResponse({"status": "claude_returned_no_alert"})
    except Exception as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=500)


# ── HTML page ─────────────────────────────────────────────────────────────────

_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Sentinel — NWO</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d1117;color:#e6edf3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',monospace;font-size:13px;min-height:100vh}
a{color:#58a6ff;text-decoration:none}
header{background:#161b22;border-bottom:1px solid #30363d;padding:10px 18px;display:flex;align-items:center;justify-content:space-between;gap:12px}
.logo{font-weight:700;font-size:15px;color:#e6edf3;letter-spacing:.5px}
.nav{display:flex;gap:6px;flex-wrap:wrap}
.nav a{padding:4px 10px;border-radius:6px;border:1px solid #30363d;background:transparent;color:#8b949e;font-size:11px;font-weight:600;transition:all .15s}
.nav a:hover,.nav a.active{background:#1f6feb22;border-color:#1f6feb;color:#58a6ff}
.container{padding:16px 20px;max-width:1400px;margin:0 auto}
h1{font-size:17px;font-weight:700;color:#e6edf3;margin-bottom:4px;display:flex;align-items:center;gap:8px}
.subtitle{color:#8b949e;font-size:11px;margin-bottom:16px}

.status-strip{display:flex;gap:14px;background:#161b22;border:1px solid #30363d;border-radius:6px;padding:10px 14px;margin-bottom:16px;font-size:11px}
.status-strip > div{display:flex;flex-direction:column;gap:2px}
.status-strip .lbl{font-size:9px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px}
.status-strip .val{font-size:13px;color:#e6edf3;font-weight:600}
.status-strip .val.ok{color:#3fb950}
.status-strip .val.warn{color:#d29922}
.status-strip .val.err{color:#f85149}

.controls{display:flex;gap:8px;margin-bottom:16px}
.btn{padding:5px 11px;border-radius:5px;border:1px solid #30363d;background:transparent;color:#8b949e;cursor:pointer;font-size:11px;font-weight:600}
.btn:hover{border-color:#58a6ff;color:#58a6ff}
.btn-primary{border-color:#1f6feb;color:#58a6ff}
.btn-warn{border-color:#d29922;color:#d29922}

.alerts{display:flex;flex-direction:column;gap:10px}
.alert{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px;border-left:4px solid #30363d}
.alert.info{border-left-color:#58a6ff}
.alert.warn{border-left-color:#d29922}
.alert.high{border-left-color:#f85149}
.alert-hdr{display:flex;align-items:center;gap:10px;margin-bottom:6px;font-size:11px}
.alert-ticker{font-size:16px;font-weight:700;color:#e6edf3}
.alert-action{padding:2px 8px;border-radius:10px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.4px}
.alert-action.watch_for_entry{background:#3fb95022;color:#3fb950;border:1px solid #3fb95044}
.alert-action.watch_for_exit{background:#f8514922;color:#f85149;border:1px solid #f8514944}
.alert-action.wait{background:#8b949e22;color:#8b949e;border:1px solid #8b949e44}
.alert-action.investigate{background:#d2992222;color:#d29922;border:1px solid #d2992244}
.alert-level{padding:2px 7px;border-radius:8px;font-size:9px;font-weight:700;text-transform:uppercase}
.alert-level.info{background:#58a6ff22;color:#58a6ff}
.alert-level.warn{background:#d2992222;color:#d29922}
.alert-level.high{background:#f8514922;color:#f85149}
.alert-time{color:#8b949e;font-size:10px;margin-left:auto}
.alert-reason{color:#c9d1d9;font-size:12px;line-height:1.5;margin:4px 0}
.alert-meta{font-size:10px;color:#8b949e;margin-top:6px;display:flex;gap:14px;flex-wrap:wrap}
.alert-conf-bar{flex:0 0 100px;height:4px;background:#21262d;border-radius:2px;overflow:hidden;display:inline-block;vertical-align:middle;margin:0 6px}
.alert-conf-fill{height:100%;background:#3fb950}
.empty{padding:36px;text-align:center;color:#8b949e}

/* Info icon + modal */
.info-btn{background:none;border:1px solid #30363d;border-radius:4px;color:#8b949e;cursor:pointer;font-size:14px;padding:2px 8px;line-height:1;transition:all .15s}
.info-btn:hover{border-color:#58a6ff;color:#58a6ff}
.info-overlay{display:none;position:fixed;inset:0;background:#000000cc;z-index:1000;align-items:center;justify-content:center;padding:20px}
.info-overlay.open{display:flex}
.info-modal{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:24px;width:min(720px,95vw);max-height:88vh;overflow-y:auto}
.info-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;padding-bottom:10px;border-bottom:1px solid #30363d}
.info-title{font-size:16px;font-weight:700;color:#e6edf3;display:flex;align-items:center;gap:8px}
.info-close{background:none;border:none;color:#8b949e;cursor:pointer;font-size:20px;line-height:1;padding:0 4px}
.info-close:hover{color:#e6edf3}
.info-modal h3{font-size:13px;color:#58a6ff;margin:18px 0 8px;font-weight:700;text-transform:uppercase;letter-spacing:.5px}
.info-modal h3:first-of-type{margin-top:0}
.info-modal p{color:#c9d1d9;font-size:13px;line-height:1.7;margin-bottom:10px}
.info-modal code{background:#0d1117;border:1px solid #30363d;border-radius:3px;padding:1px 6px;font-size:12px;color:#79c0ff}
.info-modal .info-table{width:100%;font-size:12px;margin:8px 0 14px;border-collapse:collapse}
.info-modal .info-table td{padding:6px 10px;border-bottom:1px solid #21262d;vertical-align:top;color:#c9d1d9}
.info-modal .info-table td:first-child{font-weight:600;color:#e6edf3;width:130px;white-space:nowrap}
.info-modal .info-pill{display:inline-block;padding:1px 8px;border-radius:8px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.3px;margin-right:4px}
.info-modal .info-pill.info{background:#58a6ff22;color:#58a6ff}
.info-modal .info-pill.warn{background:#d2992222;color:#d29922}
.info-modal .info-pill.high{background:#f8514922;color:#f85149}
.info-callout{background:#0d1117;border-left:3px solid #58a6ff;padding:10px 14px;margin:10px 0;font-size:12px;color:#c9d1d9;border-radius:0 4px 4px 0}
.info-callout.warn{border-left-color:#d29922}
</style>
</head>
<body>
<header>
  <span class="logo">&#9711; NWO</span>
  <nav class="nav">
    <a href="/">&#128202; Signals</a>
    <a href="/morning-brief">&#128202; Morning Brief</a>
    <a href="/i-tool">&#128225; I-Tool</a>
    <a href="/paper/compare">&#127918; Paper</a>
    <a href="/wheel">&#127905; Wheel</a>
    <a href="/signals">&#128200; Signal Monitor</a>
    <a href="/alfred">&#128270; Alfred</a>
    <a href="/strat">&#128202; STRAT</a>
    <a href="/sentinel" class="active">&#128737; Sentinel</a>
  </nav>
</header>

<div class="container">
  <h1>
    &#128737; Live Sentinel
    <button class="info-btn" onclick="document.getElementById('infoOverlay').classList.add('open')" title="How the Sentinel works">&#9432;</button>
  </h1>
  <p class="subtitle">Claude-judged real-time alerts on cross-ticker correlation breaks (entanglement decoherence) &mdash; predictive lead-lag signals from the watchlist universe.</p>

  <div class="status-strip" id="statusStrip">
    <div><div class="lbl">Status</div><div class="val" id="stStatus">&hellip;</div></div>
    <div><div class="lbl">Last Claude Call</div><div class="val" id="stLast">&mdash;</div></div>
    <div><div class="lbl">Total Alerts</div><div class="val" id="stCount">0</div></div>
    <div><div class="lbl">Today's Calls</div><div class="val" id="stCalls">0</div></div>
    <div><div class="lbl">Today's Cost</div><div class="val" id="stCost">$0.00</div></div>
    <div><div class="lbl">Engine</div><div class="val" id="stEngine">&mdash;</div></div>
  </div>

  <div class="controls">
    <button class="btn btn-primary" onclick="triggerNow()">&#9889; Trigger Judgment Now</button>
    <button class="btn btn-warn"    onclick="togglePause()" id="pauseBtn">&#9208; Pause</button>
    <button class="btn"             onclick="refresh()">&#8635; Refresh</button>
  </div>

  <div class="alerts" id="alerts">
    <div class="empty">Loading alerts&hellip;</div>
  </div>
</div>

<!-- Info modal -->
<div class="info-overlay" id="infoOverlay" onclick="if(event.target===this)this.classList.remove('open')">
  <div class="info-modal">
    <div class="info-hdr">
      <div class="info-title">&#128737; About the Live Sentinel</div>
      <button class="info-close" onclick="document.getElementById('infoOverlay').classList.remove('open')">&times;</button>
    </div>

    <h3>What is the Sentinel?</h3>
    <p>The Sentinel is a real-time monitoring system that watches the market for unusual patterns and asks Claude (Anthropic&rsquo;s AI) to interpret them. Unlike the rest of the NWO pipeline &mdash; which analyzes each ticker independently &mdash; the Sentinel looks at how watchlist tickers move <em>together</em>, and flags moments when their normal relationships break down.</p>

    <h3>The Physics Analog: Entanglement &amp; Decoherence</h3>
    <p>In quantum mechanics, &ldquo;entangled&rdquo; particles maintain correlations &mdash; observing one tells you about the other. In markets, certain ticker pairs are similarly entangled via shared exposures: <code>NVDA</code> and <code>AMD</code> both ride AI/GPU demand, <code>AAPL</code> and <code>MSFT</code> share mega-cap tech beta, <code>JPM</code> and <code>BAC</code> move together on large-bank sentiment.</p>
    <p>When a pair&rsquo;s correlation suddenly breaks (&ldquo;decoherence&rdquo;), one ticker has received new information the other hasn&rsquo;t propagated yet. The lagging ticker is statistically likely to <strong>catch up</strong> &mdash; this is a predictive lead-lag signal.</p>

    <h3>How Detection Works</h3>
    <table class="info-table">
      <tr><td>Sampling</td><td>30-day rolling Pearson correlation across all watchlist pairs</td></tr>
      <tr><td>Cadence</td><td>Every 60 seconds during market hours (9:30&ndash;16:00 ET)</td></tr>
      <tr><td>Threshold</td><td><code>|&Delta;&rho;|</code> &gt; 2.5&sigma; across all pairs AND historical <code>|&rho;|</code> &gt; 0.50</td></tr>
      <tr><td>Lead/Lag</td><td>The ticker with the larger recent move is the lead; the other is the catch-up candidate</td></tr>
      <tr><td>Confidence</td><td>Scaled by historical correlation strength &times; z-score magnitude</td></tr>
    </table>

    <h3>Reading an Alert</h3>
    <p>When events accumulate, Claude is sent the structured event + live prices + current pipeline signals and returns a single actionable alert:</p>
    <table class="info-table">
      <tr><td>Level</td><td>
        <span class="info-pill info">INFO</span>noteworthy but routine &middot;
        <span class="info-pill warn">WARN</span>setup forming, monitor &middot;
        <span class="info-pill high">HIGH</span>high-conviction divergence
      </td></tr>
      <tr><td>Action</td><td><code>watch_for_entry</code>, <code>watch_for_exit</code>, <code>wait</code>, <code>investigate</code></td></tr>
      <tr><td>Reasoning</td><td>One sentence explaining the setup in plain English</td></tr>
      <tr><td>Horizon</td><td>How long the signal is expected to remain valid (minutes)</td></tr>
      <tr><td>Confidence</td><td>0&ndash;100% &mdash; Claude&rsquo;s own assessment of how strong the setup is</td></tr>
    </table>

    <div class="info-callout">
      <strong>This is an attention-getter, not a trade trigger.</strong> Alerts highlight unusual patterns worth a human review. They are not auto-executed; the regular L1&ndash;L5 pipeline still has to approve any actual trade.
    </div>

    <h3>Why the Engine Stays Quiet Most Days</h3>
    <p>The Sentinel is intentionally <em>selective</em>. Normal market behavior produces zero events. The engine only fires when something genuinely abnormal happens &mdash; sudden divergences, sector-specific news propagating unevenly, single-stock catalysts. If you see no alerts for a few hours, that&rsquo;s the system working correctly, not silence from a failure.</p>

    <h3>Daily API Budget</h3>
    <p>Each Claude judgment costs about <code>$0.001&ndash;0.005</code> (Haiku model). Typical days produce 10&ndash;30 events &rarr; <code>~$0.05&ndash;$0.30/day</code>. Today&rsquo;s usage is shown in the status strip above. The <strong>Pause</strong> button halts new Claude calls without affecting the underlying entanglement detection.</p>

    <h3>Manual Trigger</h3>
    <p>The <strong>Trigger Judgment Now</strong> button forces a fresh scan + Claude judgment immediately, even if no new events have accumulated. Useful for verifying the system is reachable and for testing during quiet markets.</p>

    <div class="info-callout warn">
      <strong>Privacy &amp; security note:</strong> Claude sees only ticker symbols, current prices, and signal scores &mdash; never account balances, positions, or P&amp;L. The API call is HTTPS-encrypted; we route through a Norton-aware SSL context because this machine has SSL inspection enabled.
    </div>
  </div>
</div>

<script>
async function refresh() {
  try {
    const [alertsR, statusR] = await Promise.all([
      fetch('/api/sentinel/alerts?limit=50').then(r => r.json()),
      fetch('/api/sentinel/status').then(r => r.json()),
    ]);

    // Status
    const st = statusR;
    document.getElementById('stStatus').textContent  = st.paused ? 'Paused' : 'Running';
    document.getElementById('stStatus').className    = 'val ' + (st.paused ? 'warn' : 'ok');
    document.getElementById('stLast').textContent    = st.last_claude_call
      ? new Date(st.last_claude_call + 'Z').toLocaleTimeString() : 'never';
    document.getElementById('stCount').textContent   = st.alert_count;
    document.getElementById('stCalls').textContent   = (st.budget && st.budget.calls) || 0;
    document.getElementById('stCost').textContent    = '$' + (((st.budget && st.budget.cost_usd) || 0)).toFixed(3);
    document.getElementById('stEngine').textContent  = st.engine_state && st.engine_state.events_found != null
      ? `${st.engine_state.events_found} events / ${st.engine_state.pairs_analyzed} pairs` : 'idle';
    document.getElementById('pauseBtn').textContent  = st.paused ? '▶ Resume' : '⏸ Pause';

    // Alerts
    const list = alertsR.alerts || [];
    const box = document.getElementById('alerts');
    if (list.length === 0) {
      box.innerHTML = '<div class="empty">No alerts yet. Waiting for entanglement events&hellip;</div>';
      return;
    }
    box.innerHTML = list.map(a => {
      const ts = new Date(a.timestamp + 'Z').toLocaleString();
      const conf = Math.round((a.confidence || 0) * 100);
      const triggers = (a.triggered_by || []).slice(0, 3).join(', ');
      return `<div class="alert ${a.alert_level}">
        <div class="alert-hdr">
          <span class="alert-ticker">${a.primary_ticker}</span>
          <span class="alert-action ${a.action}">${(a.action || '').replace(/_/g,' ')}</span>
          <span class="alert-level ${a.alert_level}">${a.alert_level}</span>
          <span class="alert-time">${ts}</span>
        </div>
        <div class="alert-reason">${a.reasoning}</div>
        <div class="alert-meta">
          <span>Confidence: ${conf}%
            <span class="alert-conf-bar"><span class="alert-conf-fill" style="width:${conf}%"></span></span>
          </span>
          <span>Horizon: ${a.time_horizon_minutes} min</span>
          <span>Triggered by: ${triggers}</span>
          <span>Cost: $${(a.api_cost_usd || 0).toFixed(4)}</span>
        </div>
      </div>`;
    }).join('');
  } catch(e) {
    console.error('Sentinel refresh failed', e);
  }
}

async function triggerNow() {
  const btn = event.target;
  btn.disabled = true;
  btn.textContent = 'Calling Claude…';
  try {
    const r = await fetch('/api/sentinel/trigger', {method: 'POST'}).then(r => r.json());
    await refresh();
    btn.textContent = r.status === 'alert_generated' ? '✓ Alert created' : r.status;
    setTimeout(() => { btn.disabled = false; btn.textContent = '⚡ Trigger Judgment Now'; }, 2000);
  } catch(e) {
    btn.textContent = 'Error';
    setTimeout(() => { btn.disabled = false; btn.textContent = '⚡ Trigger Judgment Now'; }, 2000);
  }
}

async function togglePause() {
  await fetch('/api/sentinel/pause', {method: 'POST'});
  await refresh();
}

refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>"""


@sentinel_router.get("/sentinel", response_class=HTMLResponse)
def sentinel_page():
    return HTMLResponse(_PAGE, media_type="text/html; charset=utf-8")
