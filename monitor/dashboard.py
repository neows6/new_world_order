"""
monitor/dashboard.py — FastAPI web dashboard for NWO trading system.

Endpoints:
  GET  /                    HTML dashboard (auto-refreshes every 15s)
  GET  /api/status          System status JSON
  GET  /api/signals         Recent trade signals JSON
  GET  /api/trades          Recent trade log JSON
  GET  /api/logs            Last N log lines JSON
  POST /api/pause           Toggle pause flag
  GET  /api/diagnostics     Per-ticker data health check
  POST /api/backfill        Trigger price/fundamentals backfill in background
  GET  /api/backfill/status Backfill progress
"""

import os
import sys
import threading
from datetime import datetime
from pathlib import Path

# Resolve project root so this works whether called directly or via -m
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from config import config
from models.database import init_db, Company, TradeSignal, TradeLog, Fundamental, PriceHistory
from broker.market_data import SchwabMarketData

app = FastAPI(title="NWO Monitor", docs_url=None, redoc_url=None)

from monitor.wheel import wheel_router
app.include_router(wheel_router)

PAUSE_FLAG = ROOT / "data" / "paused.flag"
LOG_FILE   = ROOT / config.log_file

_, Session = init_db(config.database.url, echo=False)


# ── helpers ──────────────────────────────────────────────────────────────────

def _is_paused() -> bool:
    return PAUSE_FLAG.exists()


def _price_change(session, company_id: int) -> dict:
    """Return latest price and day-over-day % change from price_history."""
    candles = (
        session.query(PriceHistory)
        .filter_by(company_id=company_id)
        .order_by(PriceHistory.date.desc())
        .limit(2)
        .all()
    )
    if not candles:
        return {"price": None, "change_pct": None, "prev_close": None}
    latest = candles[0]
    prev   = candles[1] if len(candles) > 1 else None
    price  = latest.close
    change_pct = None
    if prev and prev.close:
        change_pct = round((price - prev.close) / prev.close * 100, 2)
    return {"price": round(price, 2) if price else None,
            "change_pct": change_pct,
            "prev_close": round(prev.close, 2) if prev and prev.close else None}


def _recent_signals(limit: int = 50) -> list:
    with Session() as session:
        rows = (
            session.query(TradeSignal, Company)
            .join(Company, Company.id == TradeSignal.company_id)
            .order_by(TradeSignal.generated_at.desc())
            .limit(limit)
            .all()
        )
        result = []
        for s, company in rows:
            pc = _price_change(session, company.id)
            result.append({
                "ticker":          company.ticker,
                "signal":          s.signal,
                "confidence":      round(s.confidence or 0, 3),
                "margin_of_safety": round(s.margin_of_safety or 0, 3),
                "fud_score":       round(s.fud_score or 0, 3),
                "current_price":   pc["price"] or s.current_price,
                "change_pct":      pc["change_pct"],
                "prev_close":      pc["prev_close"],
                "intrinsic_value": s.intrinsic_value_estimate,
                "generated_at":    s.generated_at.strftime("%Y-%m-%d %H:%M:%S") if s.generated_at else "",
                "acted_on":        s.acted_on,
                "reasoning":       s.reasoning or "{}",
            })
        return result


def _recent_trades(limit: int = 30) -> list:
    with Session() as session:
        rows = (
            session.query(TradeLog)
            .order_by(TradeLog.created_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "ticker":    t.ticker,
                "action":    t.action,
                "quantity":  t.quantity,
                "price":     t.price_at_execution,
                "total":     t.total_value,
                "dry_run":   t.dry_run,
                "status":    t.status,
                "executed_at": t.executed_at.strftime("%Y-%m-%d %H:%M:%S") if t.executed_at else "",
                "notes":     t.notes or "",
            }
            for t in rows
        ]


def _tail_log(lines: int = 80) -> list[str]:
    if not LOG_FILE.exists():
        return ["Log file not found yet — start main.py first."]
    try:
        text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
        return text.splitlines()[-lines:]
    except Exception as e:
        return [f"Could not read log: {e}"]


# ── API routes ────────────────────────────────────────────────────────────────

@app.get("/api/status")
def api_status():
    return {
        "paused":   _is_paused(),
        "dry_run":  config.risk.dry_run,
        "watchlist": config.watchlist,
        "timestamp": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
    }


@app.get("/api/signals")
def api_signals():
    return _recent_signals()


@app.get("/api/trades")
def api_trades():
    return _recent_trades()


@app.get("/api/logs")
def api_logs():
    return {"lines": _tail_log()}


@app.get("/api/positions")
def api_positions():
    """Live positions from Schwab — shares held, avg cost, market value, P&L."""
    try:
        md = SchwabMarketData()
        positions = md.get_positions()
        # Filter to equity only, skip zero-quantity rows
        return [p for p in positions if p.get("quantity") and p.get("asset_type") in (None, "EQUITY", "ETF")]
    except Exception as e:
        return JSONResponse(status_code=503, content={"error": str(e)})


@app.post("/api/pause")
def api_pause():
    if _is_paused():
        PAUSE_FLAG.unlink(missing_ok=True)
        return {"paused": False, "message": "System resumed"}
    else:
        PAUSE_FLAG.parent.mkdir(parents=True, exist_ok=True)
        PAUSE_FLAG.touch()
        return {"paused": True, "message": "System paused"}


# ── Diagnostics ───────────────────────────────────────────────────────────────

@app.get("/api/diagnostics")
def api_diagnostics():
    results = []
    with Session() as session:
        for ticker in config.watchlist:
            company = session.query(Company).filter_by(ticker=ticker).first()
            if not company:
                results.append({
                    "ticker": ticker, "status": "missing",
                    "price_days": 0, "latest_price": None,
                    "fundamental_years": 0, "latest_fundamental": None,
                    "signal_count": 0,
                    "message": "No company record — run main.py to ingest",
                })
                continue

            price_days = session.query(PriceHistory).filter_by(company_id=company.id).count()
            latest_ph  = (
                session.query(PriceHistory)
                .filter_by(company_id=company.id)
                .order_by(PriceHistory.date.desc())
                .first()
            )
            fund_years = (
                session.query(Fundamental.fiscal_year)
                .filter_by(company_id=company.id, fiscal_quarter=0)
                .distinct().count()
            )
            latest_fund = (
                session.query(Fundamental.fiscal_year)
                .filter_by(company_id=company.id, fiscal_quarter=0)
                .order_by(Fundamental.fiscal_year.desc())
                .limit(1)
                .scalar()
            )
            signal_count = session.query(TradeSignal).filter_by(company_id=company.id).count()

            if price_days == 0:
                status = "no_prices"
                message = "No price history — needs backfill"
            elif price_days < 64:
                status = "insufficient"
                message = f"Only {price_days} days — need 64+ for FFT signals"
            elif fund_years == 0:
                status = "no_fundamentals"
                message = "Prices OK — no EDGAR fundamentals (pre-revenue or not SEC filer)"
            else:
                status = "ready"
                message = f"{price_days} days prices | {fund_years} yrs fundamentals | {signal_count} signals"

            results.append({
                "ticker":            ticker,
                "status":            status,
                "price_days":        price_days,
                "latest_price":      latest_ph.date.strftime("%Y-%m-%d") if latest_ph and latest_ph.date else None,
                "fundamental_years": fund_years,
                "latest_fundamental": latest_fund,
                "signal_count":      signal_count,
                "message":           message,
            })
    return results


# ── Backfill ──────────────────────────────────────────────────────────────────

_backfill_state: dict = {"running": False, "log": [], "done": False}


def _run_backfill(days: int):
    _backfill_state["running"] = True
    _backfill_state["done"] = False
    _backfill_state["log"] = [f"Starting {days}-day backfill for {len(config.watchlist)} tickers..."]

    try:
        from pipeline.ingestion import IngestionPipeline
        pipeline = IngestionPipeline(db_session_factory=Session)

        for i, ticker in enumerate(config.watchlist, 1):
            _backfill_state["log"].append(f"[{i}/{len(config.watchlist)}] {ticker} — fetching prices...")
            try:
                ok_p = pipeline.ingest_price_history(ticker, days=days)
                _backfill_state["log"].append(
                    f"  Prices: {'✓' if ok_p else '✗ (no data returned)'}"
                )
                ok_f = pipeline.ingest_fundamentals(ticker)
                _backfill_state["log"].append(
                    f"  Fundamentals: {'✓' if ok_f else '✗ (not an SEC filer or pre-revenue)'}"
                )
            except Exception as e:
                _backfill_state["log"].append(f"  Error: {e}")

        _backfill_state["log"].append("Backfill complete.")
    except Exception as e:
        _backfill_state["log"].append(f"Backfill failed: {e}")
    finally:
        _backfill_state["running"] = False
        _backfill_state["done"] = True


@app.post("/api/backfill")
def api_backfill(days: int = 365):
    if _backfill_state["running"]:
        return {"started": False, "message": "Backfill already in progress"}
    _backfill_state["log"] = []
    _backfill_state["done"] = False
    thread = threading.Thread(target=_run_backfill, args=(days,), daemon=True)
    thread.start()
    return {"started": True, "message": f"Backfill started ({days} days)"}


@app.get("/api/backfill/status")
def api_backfill_status():
    return {
        "running": _backfill_state["running"],
        "done":    _backfill_state["done"],
        "log":     _backfill_state["log"],
    }


# ── Dashboard HTML ────────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NWO Monitor</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d1117; color: #e6edf3; font-family: 'Segoe UI', monospace; font-size: 14px; }
  header { background: #161b22; padding: 12px 20px; border-bottom: 1px solid #30363d;
           display: flex; align-items: center; gap: 16px; }
  header h1 { font-size: 18px; letter-spacing: 2px; color: #58a6ff; white-space: nowrap; }
  .brief-btn { display: flex; flex-direction: column; gap: 2px; padding: 6px 14px;
               border-radius: 8px; border: 1px solid #1f6feb; background: #0d1e36;
               color: #58a6ff; text-decoration: none; line-height: 1.3;
               transition: background 0.15s; }
  .brief-btn:hover { background: #1c2e50; }
  .brief-btn-title { font-size: 12px; font-weight: 700; letter-spacing: 0.5px; }
  .brief-btn-preview { font-size: 10px; color: #8b949e; white-space: nowrap; }
  .brief-btn-preview .up  { color: #3fb950; }
  .brief-btn-preview .dn  { color: #f85149; }
  .brief-btn-preview .neu { color: #8b949e; }
  .header-spacer { flex: 1; }
  .badges { display: flex; gap: 10px; align-items: center; }
  .badges { display: flex; gap: 10px; align-items: center; }
  .badge { padding: 4px 10px; border-radius: 12px; font-size: 12px; font-weight: 600; }
  .badge-green  { background: #1a4731; color: #3fb950; }
  .badge-yellow { background: #3d2b00; color: #d29922; }
  .badge-red    { background: #4a1519; color: #f85149; }
  .badge-blue   { background: #1c2e50; color: #58a6ff; }
  #pause-btn { padding: 6px 16px; border-radius: 6px; border: 1px solid #30363d;
               cursor: pointer; font-size: 13px; background: #21262d; color: #e6edf3; }
  #pause-btn:hover { background: #30363d; }
  #pause-btn.paused { background: #4a1519; color: #f85149; border-color: #f85149; }
  main { padding: 16px; display: grid; gap: 16px;
         grid-template-columns: 3fr 1fr; grid-template-rows: auto auto auto; }
  section { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px; }
  section.full-width { grid-column: 1 / -1; }
  h2 { font-size: 13px; color: #8b949e; text-transform: uppercase; letter-spacing: 1px;
       margin-bottom: 10px; border-bottom: 1px solid #30363d; padding-bottom: 6px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; color: #8b949e; font-weight: 500; padding: 4px 8px; }
  td { padding: 5px 8px; border-top: 1px solid #21262d; }
  tr:hover td { background: #1c2128; }
  .signal-buy  { color: #3fb950; font-weight: 700; }
  .signal-sell { color: #f85149; font-weight: 700; }
  .signal-hold { color: #d29922; font-weight: 700; }
  .conf-bar  { height: 8px; border-radius: 4px; background: #21262d; width: 80px;
               display: block; margin-bottom: 2px; }
  .conf-fill { height: 8px; border-radius: 4px; background: #3fb950;
               display: block; min-width: 2px; }
  .conf-fill.medium { background: #d29922; }
  .conf-fill.low    { background: #f85149; }
  .gate-pip { display: inline-block; width: 18px; height: 18px; line-height: 18px;
              text-align: center; border-radius: 3px; font-size: 10px; font-weight: 700;
              margin-right: 2px; }
  .gate-pass { background: #1a3a22; color: #3fb950; }
  .gate-fail { background: #3a1a1a; color: #f85149; }
  .score-bar { height: 6px; border-radius: 3px; background: #21262d; width: 60px;
               display: block; margin-top: 3px; }
  .score-fill { height: 6px; border-radius: 3px; display: block; }
  #log-box { background: #0d1117; border: 1px solid #21262d; border-radius: 4px;
             padding: 10px; height: 300px; overflow-y: auto; font-family: monospace;
             font-size: 12px; line-height: 1.5; }
  .log-error   { color: #f85149; }
  .log-warning { color: #d29922; }
  .log-info    { color: #e6edf3; }
  .dry-run-tag { background: #1c2e50; color: #58a6ff; font-size: 11px;
                 padding: 1px 6px; border-radius: 10px; margin-left: 6px; }
  #refresh-ts { font-size: 11px; color: #8b949e; }
  .empty { color: #8b949e; font-style: italic; padding: 8px; }
  th[title] { cursor: help; border-bottom: 1px dashed #8b949e; }
  .mos-good { color: #3fb950; font-weight: 600; }
  .mos-warn { color: #d29922; font-weight: 600; }
  .mos-bad  { color: #f85149; font-weight: 600; }
  .threshold-legend { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; font-size: 11px; }
  .leg-title { color: #8b949e; padding: 3px 0; }
  .leg-item  { padding: 2px 8px; border-radius: 10px; }
  .leg-buy   { background: #1a4731; color: #3fb950; }
  .leg-sell  { background: #4a1519; color: #f85149; }
  .leg-hold  { background: #3d2b00; color: #d29922; }
  .action-btn { padding: 3px 10px; border-radius: 5px; border: 1px solid #30363d;
                cursor: pointer; font-size: 11px; background: #21262d; color: #e6edf3; }
  .action-btn:hover { background: #30363d; }
  .model-bar { display: flex; align-items: center; gap: 8px; margin-bottom: 10px;
               padding: 8px 10px; background: #0d1117; border-radius: 6px; border: 1px solid #30363d; }
  .model-label { font-size: 11px; color: #8b949e; }
  .model-btn { padding: 4px 12px; border-radius: 5px; border: 1px solid #30363d; cursor: pointer;
               font-size: 11px; font-weight: 600; background: #21262d; color: #8b949e;
               transition: all 0.15s; }
  .model-btn:hover { border-color: #58a6ff; color: #e6edf3; }
  .model-btn.active-0 { background: #1a3a22; border-color: #3fb950; color: #3fb950; }
  .model-btn.active-1 { background: #3d2b00; border-color: #d29922; color: #d29922; }
  .model-btn.active-2 { background: #4a1519; border-color: #f85149; color: #f85149; }
  .model-cycle { padding: 4px 12px; border-radius: 5px; border: 1px solid #30363d; cursor: pointer;
                 font-size: 11px; background: #21262d; color: #8b949e; margin-left: 6px; }
  .model-cycle.cycling { border-color: #58a6ff; color: #58a6ff; background: #1c2e50; }
  .model-indicator { font-size: 11px; color: #8b949e; margin-left: auto; }
  .would-buy { color: #3fb950; font-size: 10px; font-weight: 700; }
  .status-ready  { color: #3fb950; }
  .status-warn   { color: #d29922; }
  .status-error  { color: #f85149; }
  #backfill-log { background: #0d1117; border: 1px solid #21262d; border-radius: 4px;
                  padding: 10px; max-height: 200px; overflow-y: auto;
                  font-family: monospace; font-size: 12px; line-height: 1.6; color: #8b949e; }
  @media (max-width: 900px) {
    main { grid-template-columns: 1fr; }
    section.full-width { grid-column: 1; }
    header { flex-wrap: wrap; gap: 8px; }
    .brief-btn-preview { display: none; }
    table { font-size: 11px; }
    th, td { padding: 4px 5px; }
    .conf-bar { width: 50px; }
    .score-bar { width: 40px; }
    .model-bar { flex-wrap: wrap; gap: 6px; }
    .model-indicator { display: none; }
  }
</style>
</head>
<body>
<header>
  <h1>NWO MONITOR</h1>
  <a href="/morning-brief" class="brief-btn" id="brief-btn">
    <span class="brief-btn-title">📊 Morning Brief</span>
    <span class="brief-btn-preview" id="brief-preview">Markets · Futures · WSB · Congress · Crypto</span>
  </a>
  <a href="/i-tool" class="brief-btn" id="itool-btn">
    <span class="brief-btn-title">📡 I-Tool</span>
    <span class="brief-btn-preview" id="itool-preview">S&amp;P 500 Technical Scanner</span>
  </a>
  <a href="/paper" class="brief-btn" id="paper-btn">
    <span class="brief-btn-title">🎮 Paper Trade</span>
    <span class="brief-btn-preview" id="paper-preview">$100k Faux Account · Loading...</span>
  </a>
  <a href="/wheel" class="brief-btn" id="wheel-btn">
    <span class="brief-btn-title">🎡 Wheel</span>
    <span class="brief-btn-preview">CSP · Covered Call · Premium</span>
  </a>
  <div class="header-spacer"></div>
  <div class="badges">
    <span id="status-badge" class="badge badge-blue">Loading...</span>
    <span id="mode-badge"   class="badge badge-yellow">DRY RUN</span>
    <button id="pause-btn" onclick="togglePause()">Pause</button>
    <span id="refresh-ts"></span>
  </div>
</header>
<main>
  <section>
    <h2>Recent Signals</h2>
    <div class="model-bar">
      <span class="model-label">Threshold model:</span>
      <button class="model-btn active-0" id="mbtn-0" onclick="setModel(0)">Standard (100%)</button>
      <button class="model-btn"          id="mbtn-1" onclick="setModel(1)">Relaxed −25%</button>
      <button class="model-btn"          id="mbtn-2" onclick="setModel(2)">Relaxed −50%</button>
      <button class="model-cycle" id="cycle-btn" onclick="toggleCycle()">&#9654; Cycle</button>
      <span class="model-indicator" id="model-indicator">Conf &gt;50% · MoS &gt;15% · FUD &gt;0.60</span>
    </div>
    <div class="threshold-legend" id="threshold-legend">
      <span class="leg-title">Action thresholds:</span>
      <span class="leg-item leg-buy" id="leg-buy">BUY: MoS &gt; 15% + Confidence &gt; 50% + FUD &gt; 0.60 + go/no-go gates pass</span>
      <span class="leg-item leg-sell">SELL: signal = sell/strong_sell from aggregator</span>
      <span class="leg-item leg-hold">HOLD / WAIT: gates blocked (VIX stress, turbulence, low confidence)</span>
    </div>
    <div id="signals-table"><p class="empty">Loading...</p></div>
  </section>
  <section>
    <h2>Positions (Live)</h2>
    <div id="positions-table"><p class="empty">Loading...</p></div>
  </section>
  <section>
    <h2>Trade Log</h2>
    <div id="trades-table"><p class="empty">Loading...</p></div>
  </section>
  <section class="full-width">
    <h2>Diagnostics
      <button class="action-btn" onclick="runDiagnostics()" style="margin-left:10px">Run diagnostics</button>
      <button class="action-btn" onclick="runBackfill(365)" style="margin-left:6px">Backfill 1 year</button>
      <button class="action-btn" onclick="runBackfill(90)"  style="margin-left:6px">Backfill 90 days</button>
    </h2>
    <div id="diag-table"><p class="empty">Tap "Run diagnostics" to check data health.</p></div>
    <div id="backfill-log" style="display:none;margin-top:10px"></div>
  </section>
  <section class="full-width">
    <h2>Live Logs</h2>
    <div id="log-box"></div>
  </section>
</main>
<script>
let paused = false;

// ── Morning Brief preview in header ──────────────────────────
async function fetchBriefPreview() {
  try {
    const d = await fetch('/api/morning-brief').then(r => r.json());
    if (d.error) return;

    const fmt = (asset, data, prefix='') => {
      if (!data) return null;
      const pct = data.pct || 0;
      const cls = pct > 0 ? 'up' : pct < 0 ? 'dn' : 'neu';
      const sign = pct > 0 ? '+' : '';
      return `${asset} <span class="${cls}">${prefix}${pct >= 0 ? sign : ''}${pct.toFixed(1)}%</span>`;
    };

    const parts = [];
    const idx = d.indices || {};
    const com = d.commodities || {};
    const cry = d.crypto || {};
    const rat = d.rates || {};
    const fut = d.futures || {};

    if (fut['ES (S&P)']) parts.push(fmt('ES', fut['ES (S&P)']));
    else if (idx['S&P 500']) parts.push(fmt('S&P', idx['S&P 500']));
    if (idx['VIX'])     parts.push(`VIX <span class="${(idx['VIX'].price||0)>25?'dn':(idx['VIX'].price||0)>18?'neu':'up'}">${(idx['VIX'].price||0).toFixed(1)}</span>`);
    if (com['Oil WTI']) parts.push(fmt('Oil', com['Oil WTI'], '$'));
    if (com['Gold'])    parts.push(fmt('Gold', com['Gold'], '$'));
    if (cry['Bitcoin']) parts.push(fmt('BTC', cry['Bitcoin']));
    if (rat['10yr Yield']) parts.push(`10yr <span class="neu">${(rat['10yr Yield'].price||0).toFixed(2)}%</span>`);

    const wsb = d.wsb || [];
    if (wsb.length) parts.push(`WSB: <span class="up">${wsb[0].ticker}</span>`);

    if (parts.length) {
      document.getElementById('brief-preview').innerHTML = parts.filter(Boolean).join(' &nbsp;·&nbsp; ');
    }
  } catch(e) { /* silent — preview is best-effort */ }
}

// ── Threshold models ─────────────────────────────────────────
const MODELS = [
  { name: 'Standard (100%)', conf: 0.50, mos: 0.15, fud: 0.60, cls: 0 },
  { name: 'Relaxed −25%',    conf: 0.375, mos: 0.1125, fud: 0.45, cls: 1 },
  { name: 'Relaxed −50%',    conf: 0.25,  mos: 0.075,  fud: 0.30, cls: 2 },
];
let _activeModel = 0;
let _cachedSignals = [];
let _cycleTimer = null;

function setModel(idx) {
  _activeModel = idx;
  const m = MODELS[idx];
  // Update button styles
  MODELS.forEach((_, i) => {
    const btn = document.getElementById('mbtn-' + i);
    btn.className = 'model-btn' + (i === idx ? ' active-' + i : '');
  });
  // Update indicator and legend
  document.getElementById('model-indicator').textContent =
    `Conf >${(m.conf*100).toFixed(1)}% · MoS >${(m.mos*100).toFixed(2)}% · FUD >${m.fud.toFixed(2)}`;
  document.getElementById('leg-buy').textContent =
    `BUY: MoS > ${(m.mos*100).toFixed(2)}% + Confidence > ${(m.conf*100).toFixed(1)}% + FUD > ${m.fud.toFixed(2)} + go/no-go gates pass`;
  // Re-render cached signals with new thresholds
  if (_cachedSignals.length) renderSignals(_cachedSignals);
}

function toggleCycle() {
  const btn = document.getElementById('cycle-btn');
  if (_cycleTimer) {
    clearInterval(_cycleTimer);
    _cycleTimer = null;
    btn.textContent = '▶ Cycle';
    btn.classList.remove('cycling');
  } else {
    btn.textContent = '⏹ Stop';
    btn.classList.add('cycling');
    _cycleTimer = setInterval(() => {
      setModel((_activeModel + 1) % MODELS.length);
    }, 4000);
  }
}

async function fetchStatus() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    paused = d.paused;
    const btn = document.getElementById('pause-btn');
    const badge = document.getElementById('status-badge');
    const modeBadge = document.getElementById('mode-badge');
    btn.textContent = paused ? 'Resume' : 'Pause';
    btn.className = paused ? 'paused' : '';
    badge.textContent = paused ? 'PAUSED' : 'RUNNING';
    badge.className = 'badge ' + (paused ? 'badge-red' : 'badge-green');
    modeBadge.textContent = d.dry_run ? 'DRY RUN' : 'LIVE';
    modeBadge.className = 'badge ' + (d.dry_run ? 'badge-yellow' : 'badge-red');
  } catch(e) {
    const badge = document.getElementById('status-badge');
    if (badge) { badge.textContent = 'Offline'; badge.className = 'badge badge-red'; }
  }
}

function renderSignals(signals) {
  const m   = MODELS[_activeModel];
  const el  = document.getElementById('signals-table');
  if (!signals.length) { el.innerHTML = '<p class="empty">No signals yet — waiting for market hours.</p>'; return; }

  const modelTag = _activeModel > 0
    ? `<span style="font-size:10px;padding:1px 6px;border-radius:8px;margin-left:6px;background:${_activeModel===1?'#3d2b00':'#4a1519'};color:${_activeModel===1?'#d29922':'#f85149'}">${MODELS[_activeModel].name}</span>`
    : '';

  let html = `<table><tr>
    <th>Ticker</th>
    <th>Signal${modelTag}</th>
    <th title="Latest close price and day-over-day % change vs previous close">Price / Day</th>
    <th title="Model confidence (ensemble agreement). Need >${(m.conf*100).toFixed(1)}% under current model.">Conf ?</th>
    <th title="Margin of Safety vs intrinsic value. Need >${(m.mos*100).toFixed(2)}% under current model.">MoS ?</th>
    <th title="News quality score. Need >${m.fud.toFixed(2)} under current model.">FUD ?</th>
    <th title="Gate score under current model (${MODELS[_activeModel].name}). C=Confidence M=MoS F=FUD. Bar = composite proximity to BUY.">Score (${MODELS[_activeModel].name}) ?</th>
    <th title="Reynolds fluid dynamics regime: laminar=calm, transient=ok, turbulent=blocked">Regime ?</th>
    <th title="Quantum probability state and P(Bull). Need P(Bull)>55% to pass ensemble gate.">Quantum ?</th>
    <th title="What blocked the trade (if anything)">Blocker</th>
    <th>Time (local)</th>
  </tr>`;

  for (const s of signals) {
    const origCls = s.signal === 'BUY' ? 'signal-buy' : s.signal === 'SELL' ? 'signal-sell' : 'signal-hold';
    const conf   = s.confidence || 0;
    const mosVal = s.margin_of_safety;
    const fudVal = s.fud_score || 0;
    const acted  = s.acted_on ? ' <span style="color:#3fb950">&#10003;</span>' : '';

    // ── Gates under active model thresholds ──────────────────
    const gateConf = conf   >= m.conf;
    const gateMos  = mosVal != null && mosVal >= m.mos;
    const gateFud  = fudVal >= m.fud;
    const gatesN   = [gateConf, gateMos, gateFud].filter(Boolean).length;

    // Would this be a BUY under this model? (all 3 quant gates + original signal not SELL)
    const wouldBuy = gatesN === 3 && s.signal !== 'SELL';
    const dispSignal = wouldBuy && _activeModel > 0 && s.signal !== 'BUY'
      ? `${s.signal} <span class="would-buy">→BUY*</span>`
      : s.signal;

    // Composite proximity toward this model's thresholds
    const confScore = Math.min(1, conf   / m.conf);
    const mosScore  = mosVal != null ? Math.min(1, Math.max(0, mosVal) / m.mos) : 0;
    const fudScore  = Math.min(1, fudVal / m.fud);
    const composite = (confScore + mosScore + fudScore) / 3;
    const compositeColor    = gatesN === 3 ? '#3fb950' : gatesN === 2 ? '#d29922' : '#f85149';
    const compositeBarColor = composite >= 0.9 ? '#3fb950' : composite >= 0.6 ? '#d29922' : '#f85149';

    // Gap tooltip against active model
    const confGap = ((conf   - m.conf) * 100).toFixed(1);
    const mosGap  = mosVal != null ? ((mosVal - m.mos) * 100).toFixed(1) : 'N/A';
    const fudGap  = (fudVal - m.fud).toFixed(2);
    const gapSign = v => parseFloat(v) >= 0 ? '+' : '';
    const gapTip  = `[${MODELS[_activeModel].name}]&#10;` +
                    `Conf: ${(conf*100).toFixed(1)}% · need ${(m.conf*100).toFixed(1)}% · gap ${gapSign(confGap)}${confGap}pp&#10;` +
                    `MoS:  ${mosVal!=null?(mosVal*100).toFixed(1):'N/A'}% · need ${(m.mos*100).toFixed(2)}% · gap ${mosGap!=='N/A'?gapSign(mosGap):''}${mosGap}pp&#10;` +
                    `FUD:  ${fudVal.toFixed(2)} · need ${m.fud.toFixed(2)} · gap ${gapSign(fudGap)}${fudGap}`;

    // Confidence bar (raw %, width relative to model threshold for context)
    const confBarPct = Math.min(100, Math.round(conf * 100));
    const barCls = gateConf ? '' : conf >= m.conf * 0.5 ? 'medium' : 'low';

    // MoS display
    const mosPct  = mosVal != null ? (mosVal * 100).toFixed(1) + '%' : '—';
    const mosCls  = mosVal == null ? '' : mosVal >= m.mos ? 'mos-good' : mosVal >= 0 ? 'mos-warn' : 'mos-bad';

    // FUD display
    const fudDisp = s.fud_score != null ? fudVal.toFixed(2) : '—';
    const fudCls  = fudVal >= m.fud ? 'mos-good' : 'mos-bad';

    // ── Impact helpers ────────────────────────────────────────
    // Each returns a small colored line showing delta to the gate threshold.
    // Green arrow = contributing positively (above threshold)
    // Red arrow   = dragging down (below threshold)
    function impactLine(actual, threshold, unit, decimals) {
      const delta = actual - threshold;
      const sign  = delta >= 0 ? '+' : '';
      const col   = delta >= 0 ? '#3fb950' : '#f85149';
      const arrow = delta >= 0 ? '▲' : '▼';
      const label = unit === 'pp' ? `${sign}${(delta*100).toFixed(decimals)}pp` : `${sign}${delta.toFixed(decimals)}`;
      return `<small style="color:${col};display:block;font-size:10px">${arrow} ${label} to gate</small>`;
    }

    // Reasoning
    let regime = '—', regimeImpact = '', quantum = '—', quantumImpact = '', blocker = '—';
    try {
      const rsn = JSON.parse(s.reasoning || '{}');
      const regRaw   = rsn.reynolds_regime || '—';
      const regPasses = regRaw === 'laminar' || regRaw === 'transient';
      const regCls    = regRaw === 'laminar' ? 'mos-good' : regRaw === 'transient' ? 'mos-warn' : 'mos-bad';
      const regImpStr = regRaw === 'laminar'   ? '<small style="color:#3fb950;display:block;font-size:10px">▲ unlocks execution</small>'
                      : regRaw === 'transient' ? '<small style="color:#d29922;display:block;font-size:10px">~ reduces sizing</small>'
                      :                          '<small style="color:#f85149;display:block;font-size:10px">▼ blocks execution</small>';
      regime = `<span class="${regCls}">${regRaw}</span>${regImpStr}`;

      const pbullNum  = rsn.p_bull || 0;
      const pbull     = rsn.p_bull != null ? (pbullNum*100).toFixed(0)+'%' : '—';
      const pbullCls  = pbullNum >= 0.55 ? 'mos-good' : pbullNum >= 0.45 ? 'mos-warn' : 'mos-bad';
      const pbullDelta = pbullNum - 0.55;
      const pbullSign  = pbullDelta >= 0 ? '+' : '';
      const pbullImpCl = pbullDelta >= 0 ? '#3fb950' : '#f85149';
      const pbullArr   = pbullDelta >= 0 ? '▲' : '▼';
      const pbullImp   = rsn.p_bull != null
        ? `<small style="color:${pbullImpCl};display:block;font-size:10px">${pbullArr} ${pbullSign}${(pbullDelta*100).toFixed(0)}pp to 55% gate</small>`
        : '';
      quantum = `<span class="${pbullCls}">${rsn.quantum_state||'—'} ${pbull}</span>${pbullImp}`;

      blocker = rsn.blocking_reason
        ? `<span style="color:#f85149;font-size:11px">${rsn.blocking_reason.substring(0,45)}</span>`
        : `<span style="color:#3fb950;font-size:11px">—</span>`;
    } catch(e) {}

    // Price
    const price  = s.current_price != null ? '$' + s.current_price.toFixed(2) : '—';
    const chg    = s.change_pct;
    const chgStr = chg != null ? (chg >= 0 ? '+' : '') + chg.toFixed(2) + '%' : '—';
    const chgCls = chg == null ? '' : chg > 0 ? 'mos-good' : chg < 0 ? 'mos-bad' : '';
    // Price impact on confidence: large drop = bearish pressure
    const priceImpact = chg != null
      ? `<small style="color:${Math.abs(chg)>3?(chg>0?'#3fb950':'#f85149'):'#555'};display:block;font-size:10px">${Math.abs(chg)>3?(chg>0?'▲ bullish signal':'▼ bearish pressure'):'~ neutral move'}</small>`
      : '';

    // UTC → local
    const utcStr   = s.generated_at.replace(' ', 'T') + 'Z';
    const localTime = new Date(utcStr).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'});

    html += `<tr>
      <td><strong>${s.ticker}</strong>${acted}</td>
      <td class="${origCls}">${dispSignal}</td>
      <td>
        <span style="font-weight:600">${price}</span>
        <small class="${chgCls}" style="display:block">${chgStr}</small>
        ${priceImpact}
      </td>
      <td>
        <div class="conf-bar"><div class="conf-fill ${barCls}" style="width:${confBarPct}%"></div></div>
        <small style="color:#8b949e">${(conf*100).toFixed(0)}% <span style="color:#555">(>${(m.conf*100).toFixed(0)}%)</span></small>
        ${impactLine(conf, m.conf, 'pp', 1)}
      </td>
      <td class="${mosCls}">
        ${mosPct}
        ${mosVal != null ? impactLine(mosVal, m.mos, 'pp', 1) : ''}
      </td>
      <td class="${fudCls}">
        ${fudDisp}
        ${impactLine(fudVal, m.fud, 'raw', 2)}
      </td>
      <td title="${gapTip}">
        <span class="gate-pip ${gateConf?'gate-pass':'gate-fail'}" title="Conf: ${(conf*100).toFixed(0)}% vs ${(m.conf*100).toFixed(0)}% threshold">C</span>
        <span class="gate-pip ${gateMos ?'gate-pass':'gate-fail'}" title="MoS: ${mosPct} vs ${(m.mos*100).toFixed(2)}% threshold">M</span>
        <span class="gate-pip ${gateFud ?'gate-pass':'gate-fail'}" title="FUD: ${fudVal.toFixed(2)} vs ${m.fud.toFixed(2)} threshold">F</span>
        <span style="color:${compositeColor};font-weight:700;margin-left:4px">${gatesN}/3</span>
        <div class="score-bar"><div class="score-fill" style="width:${Math.round(composite*100)}%;background:${compositeBarColor}"></div></div>
      </td>
      <td>${regime}</td>
      <td>${quantum}</td>
      <td>${blocker}</td>
      <td style="color:#8b949e;font-size:12px">${localTime}</td>
    </tr>`;
  }
  el.innerHTML = html + '</table>';
}

async function fetchSignals() {
  try {
    const r = await fetch('/api/signals');
    _cachedSignals = await r.json();
    renderSignals(_cachedSignals);
  } catch(e) {
    const el = document.getElementById('signals-table');
    if (el && !_cachedSignals.length) el.innerHTML = '<p class="empty">Could not reach server.</p>';
  }
}

async function fetchTrades() {
  let trades;
  try {
    const r = await fetch('/api/trades');
    trades = await r.json();
  } catch(e) {
    return;
  }
  const el = document.getElementById('trades-table');
  if (!trades.length) { el.innerHTML = '<p class="empty">No trades logged yet.</p>'; return; }
  let html = '<table><tr><th>Ticker</th><th>Action</th><th>Qty</th><th>Price</th><th>Total</th><th>Status</th></tr>';
  for (const t of trades) {
    const cls = t.action === 'BUY' ? 'signal-buy' : 'signal-sell';
    const tag = t.dry_run ? '<span class="dry-run-tag">DRY</span>' : '';
    const total = t.total ? '$' + t.total.toFixed(2) : '—';
    const price = t.price ? '$' + t.price.toFixed(2) : '—';
    html += `<tr>
      <td><strong>${t.ticker}</strong>${tag}</td>
      <td class="${cls}">${t.action}</td>
      <td>${t.quantity || '—'}</td>
      <td>${price}</td><td>${total}</td>
      <td style="color:#8b949e">${t.status || '—'}</td>
    </tr>`;
  }
  el.innerHTML = html + '</table>';
}

async function fetchLogs() {
  let d;
  try {
    const r = await fetch('/api/logs');
    d = await r.json();
  } catch(e) { return; }

  const box = document.getElementById('log-box');
  const wasAtBottom = box.scrollHeight - box.clientHeight <= box.scrollTop + 20;
  box.innerHTML = d.lines.map(line => {
    const cls = line.includes('ERROR') ? 'log-error' : line.includes('WARNING') ? 'log-warning' : 'log-info';
    return `<div class="${cls}">${line.replace(/</g,'&lt;').replace(/>/g,'&gt;')}</div>`;
  }).join('');
  if (wasAtBottom) box.scrollTop = box.scrollHeight;
}

async function togglePause() {
  await fetch('/api/pause', { method: 'POST' });
  await fetchStatus();
}

async function runDiagnostics() {
  const el = document.getElementById('diag-table');
  el.innerHTML = '<p class="empty">Running...</p>';
  const r = await fetch('/api/diagnostics');
  const rows = await r.json();
  let html = '<table><tr><th>Ticker</th><th>Status</th><th>Price Days</th><th>Latest Price</th><th>Fund. Years</th><th>Signals</th><th>Notes</th></tr>';
  for (const row of rows) {
    const cls = row.status === 'ready' ? 'status-ready' : row.status === 'no_fundamentals' ? 'status-warn' : 'status-error';
    const icon = row.status === 'ready' ? '✓' : row.status === 'no_fundamentals' ? '⚠' : '✗';
    html += `<tr>
      <td><strong>${row.ticker}</strong></td>
      <td class="${cls}">${icon} ${row.status}</td>
      <td>${row.price_days}</td>
      <td style="color:#8b949e;font-size:12px">${row.latest_price || '—'}</td>
      <td>${row.fundamental_years || '—'}</td>
      <td>${row.signal_count}</td>
      <td style="color:#8b949e;font-size:12px">${row.message}</td>
    </tr>`;
  }
  el.innerHTML = html + '</table>';
}

let _backfillPoll = null;
async function runBackfill(days) {
  const logEl = document.getElementById('backfill-log');
  logEl.style.display = 'block';
  logEl.innerHTML = 'Starting backfill...';
  await fetch('/api/backfill?days=' + days, { method: 'POST' });
  if (_backfillPoll) clearInterval(_backfillPoll);
  _backfillPoll = setInterval(async () => {
    const r = await fetch('/api/backfill/status');
    const d = await r.json();
    logEl.innerHTML = d.log.map(l => `<div>${l}</div>`).join('');
    logEl.scrollTop = logEl.scrollHeight;
    if (d.done) {
      clearInterval(_backfillPoll);
      _backfillPoll = null;
      await runDiagnostics();
    }
  }, 2000);
}

async function fetchPositions() {
  const el = document.getElementById('positions-table');
  try {
    const resp = await fetch('/api/positions');
    if (!resp.ok) { el.innerHTML = '<p class="empty">Schwab offline or no positions.</p>'; return; }
    const positions = await resp.json();
    if (positions.error) { el.innerHTML = `<p class="empty">${positions.error}</p>`; return; }
    if (!positions.length) { el.innerHTML = '<p class="empty">No open positions.</p>'; return; }
    let html = `<table><tr>
      <th>Ticker</th>
      <th title="Shares currently held">Shares</th>
      <th title="Average cost per share">Avg Cost</th>
      <th title="Current market value">Mkt Value</th>
      <th title="Unrealised P&amp;L (market value − cost basis)">P&amp;L</th>
    </tr>`;
    let totalValue = 0, totalPnl = 0;
    for (const p of positions) {
      const qty   = p.quantity || 0;
      const avg   = p.average_price;
      const mktV  = p.market_value;
      const cost  = avg != null ? avg * qty : null;
      const pnl   = (mktV != null && cost != null) ? mktV - cost : null;
      const pnlCls = pnl == null ? '' : pnl >= 0 ? 'mos-good' : 'mos-bad';
      const pnlStr = pnl != null ? (pnl >= 0 ? '+' : '') + '$' + pnl.toFixed(2) : '—';
      totalValue += mktV || 0;
      totalPnl   += pnl  || 0;
      html += `<tr>
        <td><strong>${p.ticker}</strong></td>
        <td>${qty}</td>
        <td>${avg != null ? '$' + avg.toFixed(2) : '—'}</td>
        <td>${mktV != null ? '$' + mktV.toFixed(2) : '—'}</td>
        <td class="${pnlCls}">${pnlStr}</td>
      </tr>`;
    }
    // Totals row
    const totPnlCls = totalPnl >= 0 ? 'mos-good' : 'mos-bad';
    html += `<tr style="border-top:1px solid #30363d;font-weight:600">
      <td colspan="3" style="color:#8b949e">Total (${positions.length} positions)</td>
      <td>$${totalValue.toFixed(2)}</td>
      <td class="${totPnlCls}">${totalPnl >= 0 ? '+' : ''}$${totalPnl.toFixed(2)}</td>
    </tr>`;
    el.innerHTML = html + '</table>';
  } catch(e) {
    el.innerHTML = '<p class="empty">Could not load positions.</p>';
  }
}

async function refresh() {
  await Promise.allSettled([fetchStatus(), fetchSignals(), fetchTrades(), fetchLogs(), fetchPositions()]);
  document.getElementById('refresh-ts').textContent = 'Updated ' + new Date().toLocaleTimeString();
}

async function fetchIToolPreview() {
  try {
    const d = await fetch('/api/itool').then(r => r.json());
    if (d.counts && (d.counts.bullish || d.counts.bearish)) {
      const el = document.getElementById('itool-preview');
      if (el) el.innerHTML =
        `<span class="up">&#9650;${d.counts.bullish||0} Bullish</span> &nbsp;&#xB7;&nbsp; <span class="dn">&#9660;${d.counts.bearish||0} Bearish</span>`;
    }
  } catch(e) {}
}

async function fetchPaperPreview() {
  try {
    const d = await fetch('/api/paper/account').then(r => r.json());
    const el = document.getElementById('paper-preview');
    if (!el || d.error) return;
    const pnl = d.total_pnl || 0;
    const cls = pnl >= 0 ? 'up' : 'dn';
    const sign = pnl >= 0 ? '+' : '';
    el.innerHTML = `$${(d.total_equity||0).toLocaleString('en-US',{maximumFractionDigits:0})} &nbsp;·&nbsp; <span class="${cls}">${sign}$${Math.abs(pnl).toLocaleString('en-US',{maximumFractionDigits:0})} P&amp;L</span>`;
  } catch(e) {}
}

// Brief, I-Tool, and Paper previews load once on startup (fast, uses cache)
fetchBriefPreview();
fetchIToolPreview();
fetchPaperPreview();

refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD_HTML


# ── Read-only embed view (Google Sites / public) ──────────────────────────────

MORNING_BRIEF_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NWO Morning Brief</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0d1117; color: #e6edf3; font-size: 13px; line-height: 1.6; }
  header { background: #161b22; border-bottom: 1px solid #30363d; padding: 12px 20px;
           display: flex; align-items: center; gap: 12px; position: sticky; top: 0; z-index: 10; }
  header h1 { font-size: 16px; font-weight: 700; }
  .back-btn { padding: 4px 12px; border-radius: 5px; border: 1px solid #30363d; cursor: pointer;
              font-size: 12px; background: #21262d; color: #e6edf3; text-decoration: none; }
  .back-btn:hover { background: #30363d; }
  .refresh-btn { padding: 4px 12px; border-radius: 5px; border: 1px solid #1f6feb; cursor: pointer;
                 font-size: 12px; background: #1c2e50; color: #58a6ff; margin-left: auto; }
  .refresh-btn:hover { background: #2d4a80; }
  .gen-time { font-size: 11px; color: #8b949e; }
  main { max-width: 1200px; margin: 0 auto; padding: 20px; display: grid;
         grid-template-columns: 2fr 1fr; gap: 16px; }
  .full { grid-column: 1 / -1; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }
  h2 { font-size: 12px; color: #8b949e; text-transform: uppercase; letter-spacing: 1px;
       border-bottom: 1px solid #21262d; padding-bottom: 6px; margin-bottom: 10px; }
  /* Narrative HTML from Claude */
  #narrative h3 { font-size: 14px; color: #e6edf3; margin: 16px 0 8px; }
  #narrative h3:first-child { margin-top: 0; }
  #narrative ul { padding-left: 18px; margin: 6px 0; }
  #narrative li { margin-bottom: 4px; color: #c9d1d9; }
  #narrative p  { color: #c9d1d9; margin-bottom: 8px; }
  #narrative table { width: 100%; border-collapse: collapse; margin: 8px 0; font-size: 12px; }
  #narrative th { color: #8b949e; text-align: left; padding: 3px 8px; border-bottom: 1px solid #21262d; }
  #narrative td { padding: 4px 8px; border-top: 1px solid #161b22; }
  .up   { color: #3fb950; font-weight: 600; }
  .down { color: #f85149; font-weight: 600; }
  /* Data tables */
  table.data { width: 100%; border-collapse: collapse; font-size: 12px; }
  table.data th { color: #8b949e; text-align: left; padding: 3px 6px;
                  font-weight: 500; border-bottom: 1px solid #21262d; font-size: 11px; }
  table.data td { padding: 4px 6px; border-top: 1px solid #161b22; }
  table.data tr:hover td { background: #1c2128; }
  .pos { color: #3fb950; } .neg { color: #f85149; } .neu { color: #8b949e; }
  /* WSB pips */
  .wsb-row { display: flex; align-items: center; gap: 6px; padding: 4px 0;
             border-top: 1px solid #161b22; }
  .wsb-rank { color: #8b949e; font-size: 11px; width: 18px; }
  .wsb-ticker { font-weight: 700; font-size: 13px; min-width: 50px; }
  .wsb-bar-wrap { flex: 1; height: 6px; background: #21262d; border-radius: 3px; }
  .wsb-bar { height: 6px; background: #58a6ff; border-radius: 3px; }
  .wsb-mentions { font-size: 11px; color: #8b949e; min-width: 40px; text-align: right; }
  .wsb-delta { font-size: 10px; min-width: 36px; text-align: right; }
  /* News list */
  .news-item { padding: 6px 0; border-top: 1px solid #161b22; }
  .news-item:first-child { border-top: none; }
  .news-source { font-size: 10px; color: #58a6ff; margin-bottom: 2px; }
  .news-title { color: #e6edf3; font-size: 12px; line-height: 1.4; }
  .news-title a { color: #e6edf3; text-decoration: none; }
  .news-title a:hover { color: #58a6ff; }
  /* Congress trades */
  .ct-buy  { color: #3fb950; font-size: 10px; font-weight: 700; }
  .ct-sell { color: #f85149; font-size: 10px; font-weight: 700; }
  .loading { color: #8b949e; font-style: italic; padding: 20px; text-align: center; }
  .error   { color: #f85149; padding: 12px; background: #1a0a0a; border-radius: 6px; }
  #spinner { display: none; color: #8b949e; font-size: 12px; }
</style>
</head>
<body>
<header>
  <a class="back-btn" href="/">← Dashboard</a>
  <h1>📊 Morning Market Brief</h1>
  <span class="gen-time" id="gen-time">Loading...</span>
  <button class="refresh-btn" id="refresh-btn" onclick="loadBrief(true)">&#8635; Regenerate</button>
</header>

<main id="main-grid" style="display:none">
  <!-- Left: AI narrative (full width if no API key, else 2/3) -->
  <div class="card" id="narrative-card">
    <h2>Market Narrative <span id="spinner">⟳ Generating...</span></h2>
    <div id="narrative"><p class="loading">Loading brief...</p></div>
  </div>

  <!-- Right column: raw data panels -->
  <div id="right-col">
    <div class="card" style="margin-bottom:16px">
      <h2>Pre-Market Futures</h2>
      <div id="futures-table"></div>
    </div>
    <div class="card" style="margin-bottom:16px">
      <h2>US Indices (prior close)</h2>
      <div id="indices-table"></div>
    </div>
    <div class="card" style="margin-bottom:16px">
      <h2>Global Markets</h2>
      <div id="global-table"></div>
    </div>
    <div class="card" style="margin-bottom:16px">
      <h2>Rates &amp; FX</h2>
      <div id="rates-table"></div>
    </div>
  </div>

  <!-- Commodities + Crypto side by side -->
  <div class="card">
    <h2>Commodities</h2>
    <div id="commodities-table"></div>
  </div>
  <div class="card">
    <h2>Crypto</h2>
    <div id="crypto-table"></div>
  </div>

  <!-- WSB + Congress side by side -->
  <div class="card">
    <h2>WSB Trending</h2>
    <div id="wsb-panel"></div>
  </div>
  <div class="card">
    <h2>Capitol Hill Trades</h2>
    <div id="congress-panel"></div>
  </div>

  <!-- Headlines full width -->
  <div class="card full">
    <h2>Top Headlines</h2>
    <div id="headlines-panel" style="columns:2;column-gap:20px"></div>
  </div>
</main>
<div id="loading-screen" style="padding:40px;text-align:center;color:#8b949e">
  ⟳ Fetching market data and generating brief...
</div>

<script>
function priceRow(name, d) {
  if (!d) return '';
  const pct = d.pct || 0;
  const cls = pct > 0 ? 'pos' : pct < 0 ? 'neg' : 'neu';
  const sign = pct > 0 ? '+' : '';
  const price = d.price > 1000 ? d.price.toLocaleString('en-US',{maximumFractionDigits:2})
                                : d.price.toFixed(2);
  return `<tr><td>${name}</td><td>${price}</td>
    <td class="${cls}">${sign}${pct.toFixed(2)}%</td></tr>`;
}

function buildTable(data) {
  if (!data || !Object.keys(data).length) return '<p class="neu" style="font-size:11px">No data</p>';
  return `<table class="data"><tr><th>Asset</th><th>Price</th><th>Change</th></tr>` +
    Object.entries(data).map(([k,v]) => priceRow(k, v)).join('') + '</table>';
}

function buildWSB(items) {
  if (!items || !items.length) return '<p class="neu" style="font-size:11px">No data</p>';
  const maxM = Math.max(...items.map(t => t.mentions));
  return items.map(t => {
    const barW = maxM > 0 ? Math.round(t.mentions / maxM * 100) : 0;
    const delta = t.mentions - (t.mentions_24h_ago || 0);
    const dCls = delta > 0 ? 'pos' : delta < 0 ? 'neg' : 'neu';
    const dStr = delta > 0 ? '+'+delta : delta;
    return `<div class="wsb-row">
      <span class="wsb-rank">#${t.rank}</span>
      <span class="wsb-ticker">${t.ticker}</span>
      <div class="wsb-bar-wrap"><div class="wsb-bar" style="width:${barW}%"></div></div>
      <span class="wsb-mentions">${t.mentions}</span>
      <span class="wsb-delta ${dCls}">${dStr}</span>
    </div>`;
  }).join('');
}

function buildCongress(trades) {
  if (!trades || !trades.length) return '<p class="neu" style="font-size:11px">No recent disclosures</p>';
  return `<table class="data"><tr><th>Who</th><th>Ticker</th><th>Action</th><th>Amount</th><th>Date</th></tr>` +
    trades.map(t => {
      const isBuy = (t.action||'').toLowerCase().includes('purchase');
      const cls = isBuy ? 'ct-buy' : 'ct-sell';
      return `<tr>
        <td><span style="font-size:10px">${t.politician||'—'} <span style="color:#8b949e">(${t.party||'?'})</span></span></td>
        <td><strong>${t.ticker||'—'}</strong></td>
        <td class="${cls}">${t.action||'—'}</td>
        <td style="font-size:11px;color:#8b949e">${t.amount||'—'}</td>
        <td style="font-size:11px;color:#8b949e">${t.date||'—'}</td>
      </tr>`;
    }).join('') + '</table>';
}

function buildHeadlines(headlines) {
  if (!headlines || !headlines.length) return '<p class="neu">No headlines fetched.</p>';
  return headlines.slice(0, 18).map(h =>
    `<div class="news-item">
      <div class="news-source">${h.source}</div>
      <div class="news-title"><a href="${h.link}" target="_blank">${h.title}</a></div>
    </div>`
  ).join('');
}

let _pollTimer = null;
let _lastGenTime = null;

function renderBrief(d) {
  const gt = d.generated_at ? new Date(d.generated_at + 'Z').toLocaleString() : '—';
  document.getElementById('gen-time').textContent = 'Generated: ' + gt;

  document.getElementById('futures-table').innerHTML     = buildTable(d.futures);
  document.getElementById('indices-table').innerHTML     = buildTable(d.indices);
  document.getElementById('global-table').innerHTML      = buildTable(d.global_markets);
  document.getElementById('rates-table').innerHTML       = buildTable(d.rates);
  document.getElementById('commodities-table').innerHTML = buildTable(d.commodities);
  document.getElementById('crypto-table').innerHTML      = buildTable(d.crypto);
  document.getElementById('wsb-panel').innerHTML         = buildWSB(d.wsb);
  document.getElementById('congress-panel').innerHTML    = buildCongress(d.congress_trades);
  document.getElementById('headlines-panel').innerHTML   = buildHeadlines(d.headlines);

  if (d.narrative_html) {
    document.getElementById('narrative').innerHTML = d.narrative_html;
    document.getElementById('narrative-card').style.display = '';
  } else {
    document.getElementById('narrative-card').style.display = 'none';
  }

  document.getElementById('loading-screen').style.display = 'none';
  document.getElementById('main-grid').style.display = 'grid';
  document.getElementById('spinner').style.display = 'none';
  document.getElementById('refresh-btn').disabled = false;
  document.getElementById('refresh-btn').textContent = '\u21BB Regenerate';
}

function setGenerating(dots) {
  const d = '.'.repeat((dots % 3) + 1);
  document.getElementById('loading-screen').style.display = 'block';
  document.getElementById('main-grid').style.display = 'none';
  document.getElementById('loading-screen').innerHTML =
    `<div style="color:#58a6ff;font-size:14px">
       Generating brief with Gemini AI${d}<br>
       <small style="color:#8b949e">Usually takes 10-20 seconds — page will update automatically</small>
     </div>`;
}

async function pollForBrief(attempt) {
  try {
    const d = await fetch('/api/morning-brief').then(r => r.json());
    if (d.error) {
      document.getElementById('loading-screen').innerHTML = `<div class="error">${d.error}</div>`;
      return;
    }
    // Still generating — keep polling
    if (d._generating || !d.generated_at || d.generated_at === _lastGenTime) {
      setGenerating(attempt);
      _pollTimer = setTimeout(() => pollForBrief(attempt + 1), 2000);
      return;
    }
    // New data ready
    _lastGenTime = d.generated_at;
    renderBrief(d);
  } catch(e) {
    document.getElementById('loading-screen').innerHTML =
      `<div class="error">Connection error: ${e.message}<br><button onclick="loadBrief()" style="margin-top:8px;padding:6px 14px;background:#21262d;border:1px solid #30363d;color:#e6edf3;border-radius:5px;cursor:pointer">Retry</button></div>`;
  }
}

async function loadBrief(forceRefresh = false) {
  if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }
  document.getElementById('refresh-btn').disabled = true;
  document.getElementById('refresh-btn').textContent = 'Generating...';

  try {
    if (forceRefresh) {
      // Fire background job, then start polling
      _lastGenTime = _lastGenTime || null;
      await fetch('/api/morning-brief/refresh', { method: 'POST' });
      setGenerating(0);
      _pollTimer = setTimeout(() => pollForBrief(1), 2000);
    } else {
      // Just load cached — instant
      const d = await fetch('/api/morning-brief').then(r => r.json());
      if (d.error) {
        document.getElementById('loading-screen').innerHTML = `<div class="error">${d.error}</div>`;
        return;
      }
      if (d._generating || !d.generated_at) {
        // Server already generating (e.g. 7am job) — poll for it
        setGenerating(0);
        _pollTimer = setTimeout(() => pollForBrief(1), 2000);
        return;
      }
      _lastGenTime = d.generated_at;
      renderBrief(d);
    }
  } catch(e) {
    document.getElementById('loading-screen').innerHTML =
      `<div class="error">Failed to connect: ${e.message}<br><button onclick="loadBrief()" style="margin-top:8px;padding:6px 14px;background:#21262d;border:1px solid #30363d;color:#e6edf3;border-radius:5px;cursor:pointer">Retry</button></div>`;
  }
}

loadBrief();
</script>
</body>
</html>
"""


EMBED_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NWO Activity Feed</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0d1117; color: #e6edf3; font-size: 13px; padding: 12px; }
  h2 { font-size: 12px; color: #8b949e; text-transform: uppercase; letter-spacing: 1px;
       border-bottom: 1px solid #21262d; padding-bottom: 6px; margin: 14px 0 8px; }
  h2:first-child { margin-top: 0; }
  /* Status bar */
  .status-bar { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; align-items: center; }
  .pill { padding: 3px 10px; border-radius: 12px; font-size: 11px; font-weight: 600; }
  .pill-green  { background: #1a4731; color: #3fb950; border: 1px solid #238636; }
  .pill-red    { background: #4a1519; color: #f85149; border: 1px solid #da3633; }
  .pill-yellow { background: #3d2b00; color: #d29922; border: 1px solid #9e6a03; }
  .pill-blue   { background: #1c2e50; color: #58a6ff; border: 1px solid #1f6feb; }
  .pill-grey   { background: #21262d; color: #8b949e; border: 1px solid #30363d; }
  .ts { font-size: 11px; color: #8b949e; margin-left: auto; }
  /* Tables */
  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; color: #8b949e; font-weight: 500; padding: 4px 6px;
       border-bottom: 1px solid #21262d; font-size: 11px; }
  td { padding: 5px 6px; border-top: 1px solid #161b22; vertical-align: top; }
  tr:hover td { background: #161b22; }
  .buy  { color: #3fb950; font-weight: 700; }
  .sell { color: #f85149; font-weight: 700; }
  .hold { color: #d29922; font-weight: 700; }
  .good { color: #3fb950; }
  .bad  { color: #f85149; }
  .warn { color: #d29922; }
  .dim  { color: #8b949e; font-size: 11px; }
  /* Gate pips */
  .pip { display: inline-block; width: 16px; height: 16px; line-height: 16px;
         text-align: center; border-radius: 3px; font-size: 9px; font-weight: 700; margin-right: 1px; }
  .pip-pass { background: #1a3a22; color: #3fb950; }
  .pip-fail { background: #3a1a1a; color: #f85149; }
  /* Conf bar */
  .bar-wrap { height: 6px; border-radius: 3px; background: #21262d; width: 60px; display: block; margin-bottom: 2px; }
  .bar-fill { height: 6px; border-radius: 3px; display: block; min-width: 2px; }
  /* Impact label */
  .impact { font-size: 10px; display: block; margin-top: 1px; }
  /* Section card */
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
          padding: 12px; margin-bottom: 12px; }
  .empty { color: #8b949e; font-style: italic; padding: 6px 0; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 5px; }
  .dot-green  { background: #3fb950; }
  .dot-red    { background: #f85149; }
  .dot-yellow { background: #d29922; }
</style>
</head>
<body>

<div class="card">
  <div class="status-bar" id="status-bar">
    <span class="pill pill-grey">Loading...</span>
  </div>
</div>

<div class="card">
  <h2>Positions (Live)</h2>
  <div id="positions"></div>
</div>

<div class="card">
  <h2>Recent Signals</h2>
  <div id="signals"></div>
</div>

<div class="card">
  <h2>Recent Trades</h2>
  <div id="trades"></div>
</div>

<script>
const API = '';  // same origin

async function loadStatus() {
  try {
    const d = await fetch(API + '/api/status').then(r => r.json());
    const paused  = d.paused;
    const dryRun  = d.dry_run;
    const vix     = d.vix_level != null ? d.vix_level.toFixed(1) : '—';
    const regime  = d.market_regime || '—';
    const lastCyc = d.last_cycle ? new Date(d.last_cycle.replace(' ','T')+'Z').toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}) : '—';
    document.getElementById('status-bar').innerHTML =
      `<span class="dot ${paused?'dot-red':'dot-green'}"></span>`+
      `<span class="pill ${paused?'pill-red':'pill-green'}">${paused?'PAUSED':'LIVE'}</span>`+
      `<span class="pill ${dryRun?'pill-yellow':'pill-red'}">${dryRun?'DRY RUN':'REAL MONEY'}</span>`+
      `<span class="pill pill-blue">VIX ${vix} · ${regime}</span>`+
      `<span class="ts">Last cycle: ${lastCyc} &nbsp;·&nbsp; auto-refresh 30s</span>`;
  } catch(e) {
    document.getElementById('status-bar').innerHTML = '<span class="pill pill-grey">Offline</span>';
  }
}

async function loadSignals() {
  const el = document.getElementById('signals');
  try {
    const signals = await fetch(API + '/api/signals').then(r => r.json());
    if (!signals.length) { el.innerHTML = '<p class="empty">No signals yet.</p>'; return; }
    let html = `<table><tr>
      <th>Ticker</th><th>Signal</th><th>Price / Day</th>
      <th>Conf</th><th>MoS</th><th>FUD</th><th>Gates</th><th>Time</th>
    </tr>`;
    for (const s of signals) {
      const sigCls = s.signal==='BUY'?'buy':s.signal==='SELL'?'sell':'hold';
      const conf   = s.confidence || 0;
      const mosVal = s.margin_of_safety;
      const fudVal = s.fud_score || 0;

      // Gate evaluation (standard thresholds — display only)
      const gC = conf   >= 0.50;
      const gM = mosVal != null && mosVal >= 0.15;
      const gF = fudVal >= 0.60;
      const gN = [gC,gM,gF].filter(Boolean).length;
      const gCol = gN===3?'#3fb950':gN===2?'#d29922':'#f85149';

      // Conf bar + impact
      const barPct = Math.min(100, Math.round(conf*100));
      const barCol = conf>=0.50?'#3fb950':conf>=0.25?'#d29922':'#f85149';
      const confDelta = ((conf-0.50)*100).toFixed(1);
      const confImpCl = conf>=0.50?'good':'bad';

      // MoS
      const mosPct = mosVal!=null?(mosVal*100).toFixed(1)+'%':'—';
      const mosCls = mosVal==null?'dim':mosVal>=0.15?'good':mosVal>=0?'warn':'bad';
      const mosDelta = mosVal!=null?((mosVal-0.15)*100).toFixed(1):null;
      const mosImpCl = mosVal!=null&&mosVal>=0.15?'good':'bad';

      // FUD
      const fudDelta = (fudVal-0.60).toFixed(2);
      const fudCls   = fudVal>=0.60?'good':'bad';

      // Price
      const price  = s.current_price!=null?'$'+s.current_price.toFixed(2):'—';
      const chg    = s.change_pct;
      const chgStr = chg!=null?(chg>=0?'+':'')+chg.toFixed(2)+'%':'—';
      const chgCls = chg==null?'dim':chg>0?'good':'bad';

      // Time
      const utcStr = s.generated_at.replace(' ','T')+'Z';
      const t = new Date(utcStr).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});

      html += `<tr>
        <td><strong>${s.ticker}</strong></td>
        <td class="${sigCls}">${s.signal}</td>
        <td>
          <span style="font-weight:600">${price}</span>
          <span class="${chgCls} dim" style="display:block">${chgStr}</span>
        </td>
        <td>
          <div class="bar-wrap"><div class="bar-fill" style="width:${barPct}%;background:${barCol}"></div></div>
          <span class="dim">${(conf*100).toFixed(0)}%</span>
          <span class="impact ${confImpCl}">${conf>=0.50?'▲':' ▼'} ${confDelta>=0?'+':''}${confDelta}pp</span>
        </td>
        <td class="${mosCls}">
          ${mosPct}
          ${mosDelta!=null?`<span class="impact ${mosImpCl}">${parseFloat(mosDelta)>=0?'▲ +':'▼ '}${mosDelta}pp</span>`:''}
        </td>
        <td class="${fudCls}">
          ${fudVal.toFixed(2)}
          <span class="impact ${fudVal>=0.60?'good':'bad'}">${fudVal>=0.60?'▲':' ▼'} ${parseFloat(fudDelta)>=0?'+':''}${fudDelta}</span>
        </td>
        <td>
          <span class="pip ${gC?'pip-pass':'pip-fail'}">C</span>
          <span class="pip ${gM?'pip-pass':'pip-fail'}">M</span>
          <span class="pip ${gF?'pip-pass':'pip-fail'}">F</span>
          <span style="color:${gCol};font-weight:700;margin-left:2px">${gN}/3</span>
        </td>
        <td class="dim">${t}</td>
      </tr>`;
    }
    el.innerHTML = html + '</table>';
  } catch(e) { el.innerHTML = '<p class="empty">Could not load signals.</p>'; }
}

async function loadTrades() {
  const el = document.getElementById('trades');
  try {
    const trades = await fetch(API + '/api/trades').then(r => r.json());
    if (!trades.length) { el.innerHTML = '<p class="empty">No trades logged yet.</p>'; return; }
    let html = '<table><tr><th>Ticker</th><th>Action</th><th>Qty</th><th>Price</th><th>Total</th><th>Mode</th></tr>';
    for (const t of trades) {
      const cls = t.action==='BUY'?'buy':'sell';
      html += `<tr>
        <td><strong>${t.ticker}</strong></td>
        <td class="${cls}">${t.action}</td>
        <td>${t.quantity||'—'}</td>
        <td>${t.price?'$'+t.price.toFixed(2):'—'}</td>
        <td>${t.total?'$'+t.total.toFixed(2):'—'}</td>
        <td class="dim">${t.dry_run?'DRY RUN':'LIVE'}</td>
      </tr>`;
    }
    el.innerHTML = html + '</table>';
  } catch(e) { el.innerHTML = '<p class="empty">Could not load trades.</p>'; }
}

async function loadPositions() {
  const el = document.getElementById('positions');
  try {
    const resp = await fetch(API + '/api/positions');
    if (!resp.ok) { el.innerHTML = '<p class="empty">Schwab offline or no positions.</p>'; return; }
    const positions = await resp.json();
    if (!positions.length || positions.error) { el.innerHTML = '<p class="empty">No open positions.</p>'; return; }
    let html = '<table><tr><th>Ticker</th><th>Shares</th><th>Avg Cost</th><th>Mkt Value</th><th>P&amp;L</th></tr>';
    let totV = 0, totP = 0;
    for (const p of positions) {
      const qty  = p.quantity || 0;
      const avg  = p.average_price;
      const mktV = p.market_value;
      const cost = avg != null ? avg * qty : null;
      const pnl  = mktV != null && cost != null ? mktV - cost : null;
      const pCls = pnl == null ? 'dim' : pnl >= 0 ? 'good' : 'bad';
      totV += mktV || 0; totP += pnl || 0;
      html += `<tr>
        <td><strong>${p.ticker}</strong></td>
        <td>${qty}</td>
        <td>${avg!=null?'$'+avg.toFixed(2):'—'}</td>
        <td>${mktV!=null?'$'+mktV.toFixed(2):'—'}</td>
        <td class="${pCls}">${pnl!=null?(pnl>=0?'+':'')+'$'+pnl.toFixed(2):'—'}</td>
      </tr>`;
    }
    const tCls = totP >= 0 ? 'good' : 'bad';
    html += `<tr style="font-weight:600;border-top:1px solid #21262d">
      <td colspan="3" class="dim">Total (${positions.length})</td>
      <td>$${totV.toFixed(2)}</td>
      <td class="${tCls}">${totP>=0?'+':''}$${totP.toFixed(2)}</td>
    </tr>`;
    el.innerHTML = html + '</table>';
  } catch(e) { el.innerHTML = '<p class="empty">Could not load positions.</p>'; }
}

async function refresh() {
  await Promise.all([loadStatus(), loadSignals(), loadTrades(), loadPositions()]);
}

refresh();
setInterval(refresh, 30000);
</script>
</body>
</html>
"""


@app.get("/embed", response_class=HTMLResponse)
def embed_view():
    """Read-only activity feed for embedding in Google Sites or any iframe."""
    return EMBED_HTML


# ── Morning Brief ─────────────────────────────────────────────────────────────

_brief_generating = False   # simple flag — only one generation at a time


@app.get("/api/morning-brief")
def api_morning_brief():
    """Return cached brief immediately. Never blocks."""
    try:
        from monitor.morning_brief import MorningBriefGenerator
        gen = MorningBriefGenerator()
        cached = gen.load_cached()
        if cached:
            cached["_generating"] = _brief_generating
            return cached
        # Nothing cached yet — trigger background generation and return status
        _trigger_brief_generation()
        return {"_generating": True, "narrative_html": "", "generated_at": None}
    except Exception as e:
        return JSONResponse(status_code=503, content={"error": str(e)})


@app.post("/api/morning-brief/refresh")
def api_morning_brief_refresh():
    """Kick off a background regeneration. Returns immediately."""
    if _brief_generating:
        return {"status": "already_generating"}
    _trigger_brief_generation()
    return {"status": "started"}


def _trigger_brief_generation():
    global _brief_generating
    if _brief_generating:
        return
    def _run():
        global _brief_generating
        _brief_generating = True
        try:
            from monitor.morning_brief import get_brief
            get_brief(force_refresh=True)
        except Exception as e:
            logger.warning(f"[BRIEF] Background generation failed: {e}")
        finally:
            _brief_generating = False
    threading.Thread(target=_run, daemon=True).start()


@app.get("/morning-brief", response_class=HTMLResponse)
def morning_brief_page():
    return MORNING_BRIEF_HTML


@app.get("/jstest", response_class=HTMLResponse)
def jstest():
    from fastapi.responses import HTMLResponse as HR
    return HR(content="""<!DOCTYPE html>
<html><body>
<div id="out" style="font-size:24px;padding:20px;background:#0d1117;color:red">JS NOT RUNNING</div>
<script>
document.getElementById('out').textContent = 'JS WORKS';
document.getElementById('out').style.color = 'lime';
fetch('/api/itool').then(r=>r.json()).then(d=>{
  document.getElementById('out').textContent = 'FETCH OK: ' + JSON.stringify(d.counts);
}).catch(e=>{
  document.getElementById('out').textContent = 'FETCH ERROR: ' + e;
  document.getElementById('out').style.color = 'orange';
});
</script>
</body></html>""", headers={"Cache-Control":"no-store"})


# ── PWA Launcher ──────────────────────────────────────────────────────────────
# Served at /launcher — install to home screen, auto-routes LAN vs Tailscale.

LAUNCHER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="NWO">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0d1117">
<link rel="manifest" href="/manifest.json">
<title>NWO Monitor</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d1117; color: #e6edf3; font-family: 'Segoe UI', system-ui, sans-serif;
         display: flex; flex-direction: column; align-items: center; justify-content: center;
         min-height: 100vh; gap: 12px; padding: 24px; }
  .icon { font-size: 64px; line-height: 1; }
  h1 { font-size: 22px; letter-spacing: 3px; color: #58a6ff; }
  .sub { font-size: 13px; color: #8b949e; }
  .status-row { display: flex; align-items: center; gap: 8px; margin-top: 8px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: #d29922;
         animation: blink 1.2s ease-in-out infinite; flex-shrink: 0; }
  .dot.green { background: #3fb950; animation: none; }
  .dot.red   { background: #f85149; animation: none; }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.2} }
  #msg { font-size: 13px; color: #8b949e; }
  .route-pill { padding: 3px 10px; border-radius: 10px; font-size: 11px; font-weight: 600;
                background: #1a4731; color: #3fb950; display: none; }
  .route-pill.vpn { background: #1c2e50; color: #58a6ff; }
  .route-pill.err { background: #4a1519; color: #f85149; }
  button { margin-top: 16px; padding: 12px 28px; background: #161b22; color: #58a6ff;
           border: 1px solid #1f6feb; border-radius: 8px; font-size: 15px; font-weight: 600;
           cursor: pointer; display: none; width: 100%; max-width: 280px; }
  button:hover { background: #1c2e50; }
  .install-hint { font-size: 11px; color: #484f58; margin-top: 24px; text-align: center;
                  line-height: 1.6; max-width: 280px; }
</style>
</head>
<body>
<div class="icon">&#x1F4C8;</div>
<h1>NWO MONITOR</h1>
<p class="sub">Automated Trading Dashboard</p>
<div class="status-row">
  <div class="dot" id="dot"></div>
  <span id="msg">Detecting network...</span>
</div>
<span class="route-pill" id="pill"></span>
<button id="open-btn">Open Dashboard &#x2192;</button>
<button id="retry-btn" onclick="autoRoute()">Retry</button>
<p class="install-hint" id="hint"></p>

<script>
const LAN = 'http://192.168.1.184:8765';
const VPN = 'http://100.119.78.45:8765';
const TIMEOUT_MS = 2500;

function setStatus(dotCls, msg, pillText, pillCls) {
  const dot  = document.getElementById('dot');
  const msgEl = document.getElementById('msg');
  const pill = document.getElementById('pill');
  dot.className  = 'dot ' + (dotCls || '');
  msgEl.textContent = msg;
  if (pillText) {
    pill.textContent = pillText;
    pill.className = 'route-pill ' + (pillCls || '');
    pill.style.display = 'inline';
  } else {
    pill.style.display = 'none';
  }
}

async function tryUrl(url) {
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
    const r = await fetch(url + '/api/status', {
      signal: ctrl.signal,
      cache: 'no-store',
    });
    clearTimeout(t);
    return r.ok;
  } catch {
    return false;
  }
}

// true when running as installed PWA (standalone), false in browser
const isInstalled = window.matchMedia('(display-mode: standalone)').matches
                 || window.navigator.standalone === true;

let _targetUrl = null;

async function autoRoute() {
  document.getElementById('retry-btn').style.display = 'none';
  document.getElementById('open-btn').style.display  = 'none';
  setStatus('', 'Trying local network...', '', '');
  if (await tryUrl(LAN)) {
    _targetUrl = LAN;
    setStatus('green', 'Connected via local WiFi', 'LAN', '');
    if (isInstalled) {
      setTimeout(() => { window.location.replace(LAN); }, 600);
    } else {
      showOpenButton(LAN);
    }
    return;
  }
  setStatus('', 'Trying Tailscale VPN...', '', '');
  if (await tryUrl(VPN)) {
    _targetUrl = VPN;
    setStatus('green', 'Connected via Tailscale', 'Tailscale', 'vpn');
    if (isInstalled) {
      setTimeout(() => { window.location.replace(VPN); }, 600);
    } else {
      showOpenButton(VPN);
    }
    return;
  }
  setStatus('red', 'Dashboard unreachable', 'Offline', 'err');
  document.getElementById('retry-btn').style.display = 'inline-block';
}

function showOpenButton(url) {
  const btn = document.getElementById('open-btn');
  btn.style.display = 'inline-block';
  btn.onclick = () => { window.location.replace(url); };
}

// Show platform-specific install instructions when in browser
const ua = navigator.userAgent;
const hint = document.getElementById('hint');
if (!isInstalled) {
  if (/iPhone|iPad/.test(ua)) {
    hint.innerHTML = '&#8595; Install: tap <strong>Share</strong> then <strong>"Add to Home Screen"</strong>';
  } else if (/Android/.test(ua)) {
    hint.innerHTML = '&#8595; Install: tap <strong>&#8942;</strong> then <strong>"Add to Home Screen"</strong>';
  }
}

// Register service worker for offline launcher caching
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}

autoRoute();
</script>
</body>
</html>"""


MANIFEST_JSON = """{
  "name": "NWO Monitor",
  "short_name": "NWO",
  "description": "New World Order Automated Trading Dashboard",
  "start_url": "/launcher",
  "display": "standalone",
  "background_color": "#0d1117",
  "theme_color": "#0d1117",
  "orientation": "portrait",
  "icons": [
    {
      "src": "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Crect width='100' height='100' rx='20' fill='%230d1117'/%3E%3Ctext y='.9em' font-size='80' x='10'%3E%F0%9F%93%88%3C/text%3E%3C/svg%3E",
      "sizes": "any",
      "type": "image/svg+xml",
      "purpose": "any maskable"
    }
  ]
}"""


SW_JS = """// NWO Monitor service worker — caches the launcher for offline use
const CACHE = 'nwo-launcher-v1';
const LAUNCHER = '/launcher';

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.add(LAUNCHER)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  // Only intercept the launcher page itself — all API calls go straight to network
  if (new URL(e.request.url).pathname === LAUNCHER) {
    e.respondWith(
      fetch(e.request).then(r => {
        // Update cache with fresh copy
        const copy = r.clone();
        caches.open(CACHE).then(c => c.put(e.request, copy));
        return r;
      }).catch(() => caches.match(LAUNCHER))
    );
  }
});
"""


@app.get("/launcher", response_class=HTMLResponse)
def launcher():
    return LAUNCHER_HTML


@app.get("/manifest.json")
def manifest_route():
    from fastapi.responses import Response
    return Response(content=MANIFEST_JSON, media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    from fastapi.responses import Response
    return Response(
        content=SW_JS,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


# ── I-Tool: S&P 500 Technical Scanner ────────────────────────────────────────

_itool_scanning = False

ITOOL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>I-Tool — S&P 500 Scanner</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d1117; color: #e6edf3; font-family: 'Segoe UI', monospace; font-size: 14px; }
  /* Ticker tape */
  .tape-wrap { overflow: hidden; background: #0a0f17; border-bottom: 1px solid #1f6feb;
               height: 34px; display: flex; align-items: center; position: relative; }
  .tape-track { display: flex; gap: 28px; white-space: nowrap; will-change: transform;
                animation: tape-scroll 100s linear infinite; align-items: center; }
  .tape-track:hover { animation-play-state: paused; }
  @keyframes tape-scroll { 0%{transform:translateX(0)} 100%{transform:translateX(-50%)} }
  .tape-bull { color: #3fb950; font-size: 12px; font-weight: 700; }
  .tape-bear { color: #f85149; font-size: 12px; font-weight: 700; }
  .tape-sep  { color: #30363d; font-size: 10px; }
  /* Header */
  header { background: #161b22; padding: 12px 20px; border-bottom: 1px solid #30363d;
           display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
  .back-btn { padding: 5px 12px; border-radius: 6px; border: 1px solid #30363d;
              background: #21262d; color: #8b949e; text-decoration: none; font-size: 12px; }
  .back-btn:hover { background: #30363d; }
  header h1 { font-size: 17px; letter-spacing: 1.5px; color: #58a6ff; }
  .scan-info { font-size: 11px; color: #8b949e; margin-left: auto; }
  .rescan-btn { padding: 6px 16px; border-radius: 6px; border: 1px solid #1f6feb;
                background: #0d1e36; color: #58a6ff; cursor: pointer; font-size: 12px; }
  .rescan-btn:hover { background: #1c2e50; }
  .rescan-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  /* Status bar */
  .status-bar { padding: 8px 20px; font-size: 12px; color: #8b949e;
                border-bottom: 1px solid #21262d; background: #161b22;
                display: flex; gap: 16px; align-items: center; flex-wrap: wrap; }
  .cnt-bull { color: #3fb950; font-weight: 600; }
  .cnt-bear { color: #f85149; font-weight: 600; }
  .scanning-pulse { color: #d29922; animation: pulse 1.2s ease-in-out infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
  /* Main */
  main { padding: 16px; }
  /* Filter tabs */
  .tabs { display: flex; gap: 6px; margin-bottom: 14px; align-items: center; }
  .tab-btn { padding: 5px 16px; border-radius: 6px; border: 1px solid #30363d;
             cursor: pointer; font-size: 12px; font-weight: 600;
             background: #21262d; color: #8b949e; transition: all 0.15s; }
  .tab-btn:hover { border-color: #58a6ff; color: #e6edf3; }
  .tab-btn.active { background: #1c2e50; color: #58a6ff; border-color: #58a6ff; }
  .tab-btn.t-bull.active { background: #1a4731; color: #3fb950; border-color: #3fb950; }
  .tab-btn.t-bear.active { background: #4a1519; color: #f85149; border-color: #f85149; }
  .count-label { font-size: 11px; color: #8b949e; margin-left: 6px; }
  .search-box { padding: 5px 10px; border-radius: 6px; border: 1px solid #30363d;
                background: #21262d; color: #e6edf3; font-size: 12px; width: 160px; margin-left: auto; }
  .search-box::placeholder { color: #8b949e; }
  th.sortable { cursor: pointer; user-select: none; }
  th.sortable:hover { color: #e6edf3; }
  th .sort-arrow { font-size: 10px; margin-left: 3px; opacity: 0.5; }
  th.sort-asc .sort-arrow, th.sort-desc .sort-arrow { opacity: 1; color: #58a6ff; }
  /* Table */
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; color: #8b949e; font-weight: 500; padding: 5px 8px;
       border-bottom: 1px solid #30363d; white-space: nowrap; }
  td { padding: 6px 8px; border-top: 1px solid #21262d; }
  tr:hover td { background: #1c2128; cursor: pointer; }
  .sig-bull { color: #3fb950; font-weight: 700; }
  .sig-bear { color: #f85149; font-weight: 700; }
  .pos { color: #3fb950; }
  .neg { color: #f85149; }
  .neu { color: #8b949e; }
  .chart-btn { padding: 2px 10px; border-radius: 5px; border: 1px solid #30363d;
               cursor: pointer; font-size: 11px; background: #21262d; color: #e6edf3; }
  .chart-btn:hover { background: #30363d; }
  /* Chart modal */
  .modal-bg { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.82);
              z-index: 200; align-items: center; justify-content: center; }
  .modal-bg.open { display: flex; }
  .modal-box { background: #161b22; border: 1px solid #30363d; border-radius: 10px;
               padding: 18px; width: min(860px, 96vw); max-height: 92vh; overflow-y: auto; }
  .modal-header { display: flex; justify-content: space-between; align-items: center;
                  margin-bottom: 14px; }
  .modal-title { font-size: 16px; font-weight: 700; color: #e6edf3; }
  .modal-meta  { font-size: 12px; color: #8b949e; margin-top: 2px; }
  .close-btn { background: none; border: none; color: #8b949e; cursor: pointer;
               font-size: 20px; padding: 0 4px; line-height: 1; }
  .chart-section { margin-bottom: 10px; }
  .chart-label { font-size: 11px; color: #8b949e; margin-bottom: 4px; letter-spacing: 0.5px; }
  canvas { display: block; }
  .empty { color: #8b949e; font-style: italic; padding: 20px; text-align: center; }
  @media (max-width: 700px) {
    th:nth-child(3), td:nth-child(3),
    th:nth-child(4), td:nth-child(4) { display: none; }
    table { font-size: 11px; }
    th, td { padding: 4px 5px; }
  }
</style>
</head>
<body>

<!-- Ticker tape -->
<div class="tape-wrap"><div class="tape-track" id="tape-track">&nbsp;</div></div>

<header>
  <a href="/" class="back-btn">&#8592; Dashboard</a>
  <h1>&#x1F4E1; I-Tool &mdash; S&amp;P 500 Scanner <small style="font-size:11px;color:#8b949e">[v4]</small></h1>
  <span class="scan-info" id="scan-info">Loading...</span>
  <button class="rescan-btn" id="rescan-btn" onclick="triggerScan()">&#8635; Rescan</button>
</header>

<div class="status-bar" id="status-bar">
  <span id="js-check" style="color:#f85149">JS not loaded [v4]</span>
  <noscript><span style="color:orange;font-weight:700"> — JavaScript DISABLED in browser settings</span></noscript>
</div>

<main>
  <div class="tabs">
    <button class="tab-btn active"  id="tab-all"     onclick="setFilter('all')">All Signals</button>
    <button class="tab-btn t-bull"  id="tab-bullish"  onclick="setFilter('bullish')">&#9650; Bullish</button>
    <button class="tab-btn t-bear"  id="tab-bearish"  onclick="setFilter('bearish')">&#9660; Bearish</button>
    <span class="count-label" id="count-label"></span>
    <input class="search-box" id="search-box" type="text" placeholder="&#128269; Search ticker..." oninput="applyFilter()">
  </div>
  <div id="results-wrap"></div>
</main>

<!-- Chart modal -->
<div class="modal-bg" id="chart-modal" onclick="modalBgClick(event)">
  <div class="modal-box" id="modal-box">
    <div class="modal-header">
      <div>
        <div class="modal-title" id="modal-title"></div>
        <div class="modal-meta"  id="modal-meta"></div>
      </div>
      <button class="close-btn" onclick="closeModal()">&#x2715;</button>
    </div>
    <div class="chart-section">
      <div class="chart-label">PRICE &amp; MOVING AVERAGES</div>
      <div style="position:relative;height:180px"><canvas id="chart-price"></canvas></div>
    </div>
    <div class="chart-section">
      <div class="chart-label">MACD HISTOGRAM (8, 17, 9)</div>
      <div style="position:relative;height:100px"><canvas id="chart-macd"></canvas></div>
    </div>
    <div class="chart-section">
      <div class="chart-label">STOCHASTIC %K / %D (14, 5, 5)</div>
      <div style="position:relative;height:100px"><canvas id="chart-stoch"></canvas></div>
    </div>
  </div>
</div>

<script>
// Inline check — runs immediately, no external file needed
document.getElementById('js-check').textContent = 'Inline JS OK, loading itool.js... [v4]';
document.getElementById('js-check').style.color = '#d29922';
</script>
<script src="/itool.js"
  onerror="document.getElementById('js-check').textContent='ERROR: /itool.js failed to load [v4]'; document.getElementById('js-check').style.color='orange';">
</script>
<!-- Chart.js loads async — only needed when a chart modal is opened -->
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js" async></script>
</body>
</html>"""

ITOOL_JS = """
// Debug: confirm script is executing
document.getElementById('js-check').textContent = 'JS loaded, fetching...';
document.getElementById('js-check').style.color = '#d29922';

let _scanData   = null;
let _filter     = 'all';
let _charts     = {};
let _pollTimer  = null;
let _sortCol    = null;   // column key being sorted
let _sortDir    = 1;      // 1 = asc, -1 = desc

// ── Data loading ─────────────────────────────────────────────────────────────

async function loadScan() {
  try {
    const d = await fetch('/api/itool').then(r => r.json());
    if (d.error) { showError(d.error); return; }

    if (d._scanning || !d.generated_at) {
      showScanning();
      if (!_pollTimer) _pollTimer = setInterval(loadScan, 5000);
      return;
    }

    clearInterval(_pollTimer); _pollTimer = null;
    _scanData = d;

    const gt = new Date(d.generated_at + 'Z');
    document.getElementById('scan-info').textContent =
      'Scanned ' + gt.toLocaleDateString() + ' ' + gt.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});

    const c = d.counts || {};
    document.getElementById('status-bar').innerHTML =
      '<span>' + (d.total_scanned||0) + ' stocks scanned</span>' +
      '<span class="cnt-bull">&#9650; ' + (c.bullish||0) + ' Bullish</span>' +
      '<span class="cnt-bear">&#9660; ' + (c.bearish||0) + ' Bearish</span>' +
      '<span class="neu">' + ((c.total||0) - (c.bullish||0) - (c.bearish||0)) + ' Neutral shown</span>';

    buildTape(d.results || []);
    applyFilter();
    document.getElementById('rescan-btn').disabled = false;
  } catch(e) {
    showError('Could not reach server.');
  }
}

function showScanning() {
  document.getElementById('status-bar').innerHTML =
    '<span class="scanning-pulse">&#8635; Scanning ~500 stocks... this takes ~2 minutes. Page auto-refreshes.</span>';
  document.getElementById('rescan-btn').disabled = true;
}

function showError(msg) {
  document.getElementById('status-bar').innerHTML = '<span style="color:#f85149">' + msg + '</span>';
}

async function triggerScan() {
  document.getElementById('rescan-btn').disabled = true;
  showScanning();
  await fetch('/api/itool/refresh', {method: 'POST'});
  if (!_pollTimer) _pollTimer = setInterval(loadScan, 5000);
}

// ── Ticker tape ───────────────────────────────────────────────────────────────

function buildTape(results) {
  const signals = results.filter(r => r.signal !== 'neutral');
  if (!signals.length) {
    document.getElementById('tape-track').innerHTML = '<span class="neu">No signals detected</span>';
    return;
  }
  // Duplicate for seamless loop
  const all = [...signals, ...signals];
  const track = document.getElementById('tape-track');
  track.innerHTML = all.map((r, i) =>
    '<span class="tape-' + r.signal.replace('bullish','bull').replace('bearish','bear') + '">' +
    (r.signal === 'bullish' ? '&#9650;' : '&#9660;') + ' ' +
    r.ticker + ' $' + r.price.toFixed(2) + '</span>' +
    '<span class="tape-sep">|</span>'
  ).join('');
  const dur = Math.max(50, signals.length * 0.625);
  track.style.animationDuration = dur + 's';
}

// ── Filter & table ────────────────────────────────────────────────────────────

function setFilter(f) {
  _filter = f;
  ['all','bullish','bearish'].forEach(id => {
    document.getElementById('tab-' + id).classList.toggle('active', id === f);
  });
  if (_scanData) applyFilter();
}

function setSort(col) {
  if (_sortCol === col) {
    _sortDir = _sortDir === 1 ? -1 : (_sortDir === -1 ? 1 : 1);
  } else {
    _sortCol = col; _sortDir = -1;  // default: largest first
  }
  if (_scanData) applyFilter();
}

function applyFilter() {
  const results = _scanData.results || [];
  const term = (document.getElementById('search-box') || {}).value || '';
  const q = term.trim().toUpperCase();
  let filtered = _filter === 'all' ? results : results.filter(r => r.signal === _filter);
  if (q) filtered = filtered.filter(r => r.ticker.toUpperCase().includes(q));
  // Sort
  if (_sortCol) {
    filtered = [...filtered].sort((a, b) => {
      let av = a[_sortCol], bv = b[_sortCol];
      if (av == null) av = _sortDir > 0 ? Infinity : -Infinity;
      if (bv == null) bv = _sortDir > 0 ? Infinity : -Infinity;
      return (av < bv ? -1 : av > bv ? 1 : 0) * _sortDir;
    });
  }
  document.getElementById('count-label').textContent =
    filtered.length + ' of ' + results.length + ' stocks';
  renderTable(filtered);
}

function renderTable(results) {
  const wrap = document.getElementById('results-wrap');
  if (!results.length) {
    wrap.innerHTML = '<p class="empty">No stocks match the current filter.</p>';
    return;
  }
  function sortTh(col, label) {
    const active = _sortCol === col;
    const dir = active && _sortDir === 1 ? 'desc' : active && _sortDir === -1 ? '' : 'asc';
    const arrow = active ? (_sortDir === 1 ? ' &#9650;' : ' &#9660;') : ' &#8693;';
    const cls = active ? (_sortDir === 1 ? 'sort-asc' : 'sort-desc') : '';
    return '<th class="sortable ' + cls + '" onclick="setSort(\\'' + col + '\\')">' + label + '<span class="sort-arrow">' + arrow + '</span></th>';
  }
  let html = '<table><tr>' +
    '<th>Ticker</th>' +
    sortTh('price','Price') +
    sortTh('sma30','SMA30') +
    sortTh('sma50','SMA50') +
    sortTh('macd_hist','MACD Hist') +
    sortTh('stoch_k','Stoch %K') +
    '<th>Signal</th><th>Chart</th>' +
    '</tr>';
  for (const r of results) {
    const sigCls  = r.signal === 'bullish' ? 'sig-bull' : 'sig-bear';
    const sigIcon = r.signal === 'bullish' ? '&#9650;' : '&#9660;';
    const mCls = r.macd_hist > 0 ? 'pos' : r.macd_hist < 0 ? 'neg' : 'neu';
    const kCls = r.stoch_k < 25 ? 'pos' : r.stoch_k > 75 ? 'neg' : 'neu';
    html += '<tr onclick="openChart(\\'' + r.ticker + '\\')">' +
      '<td><strong>' + r.ticker + '</strong></td>' +
      '<td>$' + r.price.toFixed(2) + '</td>' +
      '<td>' + (r.sma30 ? '$' + r.sma30.toFixed(2) : '&#8212;') + '</td>' +
      '<td>' + (r.sma50 ? '$' + r.sma50.toFixed(2) : '&#8212;') + '</td>' +
      '<td class="' + mCls + '">' + r.macd_hist.toFixed(4) + '</td>' +
      '<td class="' + kCls + '">' + r.stoch_k.toFixed(1) + '</td>' +
      '<td class="' + sigCls + '">' + sigIcon + ' ' + r.signal.toUpperCase() + '</td>' +
      '<td><button class="chart-btn" onclick="event.stopPropagation();openChart(\\'' +
        r.ticker + '\\')">Chart</button></td>' +
      '</tr>';
  }
  wrap.innerHTML = html + '</table>';
}

// ── Chart modal ───────────────────────────────────────────────────────────────

function openChart(ticker) {
  if (typeof Chart === 'undefined') {
    document.getElementById('status-bar').innerHTML +=
      ' &nbsp;<span style="color:#d29922">Chart library loading, try again in a moment...</span>';
    return;
  }
  const r = (_scanData?.results || []).find(x => x.ticker === ticker);
  if (!r) return;

  document.getElementById('modal-title').textContent = ticker + ' — Technical Analysis';
  document.getElementById('modal-meta').innerHTML =
    '<span class="' + (r.signal==='bullish'?'sig-bull':'sig-bear') + '">' +
    (r.signal==='bullish'?'&#9650;':'&#9660;') + ' ' + r.signal.toUpperCase() + '</span>' +
    ' &nbsp;|&nbsp; Price: $' + r.price.toFixed(2) +
    ' &nbsp;|&nbsp; MACD: <span class="' + (r.macd_hist>0?'pos':'neg') + '">' +
      r.macd_hist.toFixed(4) + '</span>' +
    ' &nbsp;|&nbsp; Stoch %K: ' + r.stoch_k.toFixed(1);

  document.getElementById('chart-modal').classList.add('open');

  // Destroy previous instances
  ['chart-price','chart-macd','chart-stoch'].forEach(id => {
    if (_charts[id]) { _charts[id].destroy(); delete _charts[id]; }
  });

  const labels = r.dates;
  const dark   = { grid: { color: '#21262d' }, ticks: { color: '#8b949e', font: {size:10} } };

  // ── Panel 1: Price + SMA30 + SMA50 + crossover markers ──
  const bullPts = (r.bull_price_idx || []).map(i => ({x: labels[i], y: r.closes[i]}));
  const bearPts = (r.bear_price_idx || []).map(i => ({x: labels[i], y: r.closes[i]}));

  _charts['chart-price'] = new Chart(
    document.getElementById('chart-price').getContext('2d'), {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: 'Price', data: r.closes, borderColor: '#e6edf3', borderWidth: 1.5,
          pointRadius: 0, tension: 0.1, fill: false, order: 3 },
        { label: 'SMA30', data: r.sma30_series, borderColor: '#58a6ff',
          borderWidth: 1.2, pointRadius: 0, tension: 0, fill: false, order: 2 },
        { label: 'SMA50', data: r.sma50_series, borderColor: '#d29922',
          borderWidth: 1.2, pointRadius: 0, tension: 0, fill: false, order: 2 },
        { type: 'scatter', label: 'Bullish', data: bullPts,
          pointStyle: 'triangle', pointRadius: 9, rotation: 0,
          backgroundColor: '#3fb950', borderColor: '#3fb950', order: 1 },
        { type: 'scatter', label: 'Bearish', data: bearPts,
          pointStyle: 'triangle', pointRadius: 9, rotation: 180,
          backgroundColor: '#f85149', borderColor: '#f85149', order: 1 },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#8b949e', font: {size:11},
        filter: i => i.text !== 'Bullish' && i.text !== 'Bearish' } } },
      scales: {
        x: { ...dark, x: {}, ticks: { ...dark.ticks, maxTicksLimit: 8 } },
        y: { ...dark }
      }
    }
  });

  // ── Panel 2: MACD Histogram ──────────────────────────────
  const histColors = (r.macd_hist_series || []).map(v =>
    v === null ? 'transparent' : v >= 0 ? 'rgba(63,185,80,0.7)' : 'rgba(248,81,73,0.7)');
  const histBorders = (r.macd_hist_series || []).map(v =>
    v === null ? 'transparent' : v >= 0 ? '#3fb950' : '#f85149');

  // Mark MACD crossover bars with bright borders
  (r.bull_macd_idx || []).forEach(i => { histBorders[i] = '#58a6ff'; });
  (r.bear_macd_idx || []).forEach(i => { histBorders[i] = '#ff9900'; });

  _charts['chart-macd'] = new Chart(
    document.getElementById('chart-macd').getContext('2d'), {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        label: 'MACD Hist',
        data: r.macd_hist_series,
        backgroundColor: histColors,
        borderColor: histBorders,
        borderWidth: 1,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#8b949e', font: {size:11} } } },
      scales: {
        x: { ...dark, ticks: { ...dark.ticks, maxTicksLimit: 8 } },
        y: { ...dark }
      }
    }
  });

  // ── Panel 3: Stochastic ──────────────────────────────────
  _charts['chart-stoch'] = new Chart(
    document.getElementById('chart-stoch').getContext('2d'), {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: '%K', data: r.stoch_k_series, borderColor: '#58a6ff',
          borderWidth: 1.5, pointRadius: 0, tension: 0.1, fill: false },
        { label: '%D', data: r.stoch_d_series, borderColor: '#d29922',
          borderWidth: 1, pointRadius: 0, tension: 0.1, fill: false,
          borderDash: [4, 3] },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#8b949e', font: {size:11} } } },
      scales: {
        x: { ...dark, ticks: { ...dark.ticks, maxTicksLimit: 8 } },
        y: {
          ...dark, min: 0, max: 100,
          ticks: { ...dark.ticks,
            callback: v => [0, 25, 50, 75, 100].includes(v) ? v : '' },
          grid: {
            color: ctx => {
              if (ctx.tick.value === 25 || ctx.tick.value === 75)
                return 'rgba(248,81,73,0.25)';
              return '#21262d';
            }
          }
        }
      }
    }
  });
}

function closeModal() {
  document.getElementById('chart-modal').classList.remove('open');
}

function modalBgClick(e) {
  if (e.target.id === 'chart-modal') closeModal();
}

// ── Init ──────────────────────────────────────────────────────────────────────
loadScan();
setInterval(loadScan, 30000);
"""


@app.get("/itool.js")
def itool_js():
    from fastapi.responses import Response
    return Response(
        content=ITOOL_JS,
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )




_itool_scanning = False


def _trigger_itool_scan():
    global _itool_scanning
    if _itool_scanning:
        return
    def _run():
        global _itool_scanning
        _itool_scanning = True
        try:
            from monitor.itool import get_scan
            get_scan(force_refresh=True)
        except Exception as e:
            logger.warning(f"[ITOOL] Background scan failed: {e}")
        finally:
            _itool_scanning = False
    threading.Thread(target=_run, daemon=True).start()


@app.get("/api/itool")
def api_itool():
    """Return cached I-Tool scan immediately — never blocks."""
    try:
        from monitor.itool import IToolScanner
        scanner = IToolScanner()
        cached = scanner.load_cached()
        if cached:
            cached["_scanning"] = _itool_scanning
            return cached
        _trigger_itool_scan()
        return {"_scanning": True, "results": [], "counts": {}, "generated_at": None}
    except Exception as e:
        return JSONResponse(status_code=503, content={"error": str(e)})


@app.post("/api/itool/refresh")
def api_itool_refresh():
    if _itool_scanning:
        return {"status": "already_scanning"}
    _trigger_itool_scan()
    return {"status": "started"}


@app.get("/i-tool", response_class=HTMLResponse)
def itool_page():
    from fastapi.responses import HTMLResponse as HR
    return HR(content=ITOOL_HTML, headers={"Cache-Control": "no-store"})



# ── Paper Trading Dashboard ───────────────────────────────────────────────────



# ── Paper Trading Dashboard ───────────────────────────────────────────────────

PAPER_HTML = '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="UTF-8">\n<meta name="viewport" content="width=device-width, initial-scale=1.0">\n<title>Paper Trading \\u2014 NWO</title>\n<style>\n  * { box-sizing: border-box; margin: 0; padding: 0; }\n  body { background: #0d1117; color: #e6edf3; font-family: \'Segoe UI\', monospace; font-size: 14px; }\n  header { background: #161b22; padding: 12px 20px; border-bottom: 1px solid #30363d;\n           display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }\n  .back-btn { padding: 5px 12px; border-radius: 6px; border: 1px solid #30363d;\n              background: #21262d; color: #8b949e; text-decoration: none; font-size: 12px; }\n  header h1 { font-size: 17px; letter-spacing: 1px; color: #58a6ff; }\n  .header-right { margin-left: auto; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }\n  .run-btn   { padding: 6px 16px; border-radius: 6px; border: 1px solid #3fb950;\n               background: #1a4731; color: #3fb950; cursor: pointer; font-size: 12px; font-weight: 600; }\n  .run-btn:hover { background: #1e5c3a; }\n  .run-btn:disabled { opacity: 0.5; cursor: not-allowed; }\n  .reset-btn { padding: 5px 14px; border-radius: 6px; border: 1px solid #f85149;\n               background: transparent; color: #f85149; cursor: pointer; font-size: 12px; }\n  .reset-btn:hover { background: rgba(248,81,73,0.1); }\n  .refresh-btn { padding: 5px 14px; border-radius: 6px; border: 1px solid #30363d;\n                 background: #21262d; color: #8b949e; cursor: pointer; font-size: 12px; }\n  #run-status { font-size: 11px; color: #d29922; }\n  main { padding: 16px; display: grid; gap: 16px; }\n  /* Summary cards */\n  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }\n  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px; }\n  .card-label { font-size: 10px; color: #8b949e; letter-spacing: 1px; text-transform: uppercase; margin-bottom: 6px; }\n  .card-value { font-size: 22px; font-weight: 700; }\n  .card-sub { font-size: 11px; color: #8b949e; margin-top: 4px; }\n  .up { color: #3fb950; } .dn { color: #f85149; } .neu { color: #8b949e; }\n  /* Swim lanes */\n  .swim-wrap { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; }\n  .lane { background: #161b22; border: 1px solid #30363d; border-radius: 8px; overflow: hidden; min-width: 0; }\n  .lane-header { padding: 10px 12px; font-size: 11px; font-weight: 700; letter-spacing: 0.5px;\n                 text-transform: uppercase; border-bottom: 1px solid #21262d; }\n  .lane-0 .lane-header { color: #58a6ff; border-top: 3px solid #58a6ff; }\n  .lane-1 .lane-header { color: #d29922; border-top: 3px solid #d29922; }\n  .lane-2 .lane-header { color: #3fb950; border-top: 3px solid #3fb950; }\n  .lane-sub { font-size: 10px; color: #8b949e; font-weight: 400; margin-top: 2px; }\n  .lane-body { padding: 8px; display: flex; flex-direction: column; gap: 6px; min-height: 80px; }\n  .signal-card { background: #0d1117; border: 1px solid #21262d; border-radius: 6px;\n                 padding: 8px 10px; font-size: 12px; }\n  .sig-ticker { font-weight: 700; font-size: 13px; }\n  .sig-bull { color: #3fb950; } .sig-bear { color: #f85149; }\n  .sig-meta { font-size: 10px; color: #8b949e; margin-top: 3px; display: flex; gap: 8px; flex-wrap: wrap; }\n  .lane-empty { color: #8b949e; font-size: 12px; font-style: italic; padding: 12px; text-align: center; }\n  /* Tables */\n  .section { background: #161b22; border: 1px solid #30363d; border-radius: 8px; overflow: hidden; }\n  .section-title { padding: 10px 14px; font-size: 12px; font-weight: 600; letter-spacing: 1px;\n                   color: #8b949e; border-bottom: 1px solid #21262d; text-transform: uppercase; }\n  table { width: 100%; border-collapse: collapse; }\n  th { padding: 8px 12px; text-align: left; font-size: 11px; color: #8b949e;\n       font-weight: 600; letter-spacing: 0.5px; border-bottom: 1px solid #21262d; }\n  td { padding: 8px 12px; font-size: 13px; border-bottom: 1px solid #161b22; }\n  tr:last-child td { border-bottom: none; }\n  tr:hover td { background: #1c2128; }\n  .empty { color: #8b949e; font-style: italic; padding: 20px; text-align: center; display: block; }\n  @media (max-width: 800px) {\n    .swim-wrap { grid-template-columns: 1fr; }\n    .cards { grid-template-columns: 1fr 1fr; }\n    th:nth-child(n+5), td:nth-child(n+5) { display: none; }\n  }\n</style>\n</head>\n<body>\n<header>\n  <a href="/" class="back-btn">&#8592; Dashboard</a>\n  <h1>&#127918; Paper Trading</h1>\n  <div class="header-right">\n    <span id="run-status"></span>\n    <button class="run-btn" id="run-btn" onclick="runNow()">&#9654; Run Now</button>\n    <button class="refresh-btn" onclick="load()">&#8635; Refresh</button>\n    <button class="reset-btn" onclick="resetAccount()">&#x21BA; Reset</button>\n  </div>\n</header>\n<div id="sched-bar" style="background:#0d1117;border-bottom:1px solid #21262d;padding:4px 20px;font-size:11px;color:#8b949e;display:flex;gap:16px;flex-wrap:wrap;">\n  <span id="sched-mode">&#9711; Auto: loading...</span>\n  <span id="sched-last"></span>\n  <span id="sched-next"></span>\n  <span id="sched-stops"></span>\n  <button id="sched-toggle" onclick="toggleScheduler()" style="margin-left:auto;background:none;border:1px solid #30363d;color:#8b949e;font-size:10px;padding:2px 8px;border-radius:4px;cursor:pointer">Pause</button>\n</div>\n<main>\n  <!-- Summary cards -->\n  <div class="cards">\n    <div class="card"><div class="card-label">Total Equity</div><div class="card-value" id="c-equity">\\u2014</div><div class="card-sub">Starting: $100,000</div></div>\n    <div class="card"><div class="card-label">Cash</div><div class="card-value" id="c-cash">\\u2014</div><div class="card-sub" id="c-cash-sub">&nbsp;</div></div>\n    <div class="card"><div class="card-label">Invested</div><div class="card-value" id="c-invested">\\u2014</div><div class="card-sub" id="c-invested-sub">&nbsp;</div></div>\n    <div class="card"><div class="card-label">Total P&amp;L</div><div class="card-value" id="c-pnl">\\u2014</div><div class="card-sub" id="c-pnl-sub">&nbsp;</div></div>\n  </div>\n  <!-- Three model swim lanes -->\n  <div class="swim-wrap" id="swim-wrap">\n    <div class="lane lane-0"><div class="lane-header">&#9899; Standard<div class="lane-sub">Conf &gt;50% &middot; MoS &gt;15% &middot; FUD &gt;0.60</div></div><div class="lane-body" id="lane-0"><span class="lane-empty">Loading...</span></div></div>\n    <div class="lane lane-1"><div class="lane-header">&#9898; Relaxed \\u221225%<div class="lane-sub">Conf &gt;37.5% &middot; MoS &gt;11.25% &middot; FUD &gt;0.45</div></div><div class="lane-body" id="lane-1"><span class="lane-empty">Loading...</span></div></div>\n    <div class="lane lane-2"><div class="lane-header">&#9711; Relaxed \\u221250%<div class="lane-sub">Conf &gt;25% &middot; MoS &gt;7.5% &middot; FUD &gt;0.30</div></div><div class="lane-body" id="lane-2"><span class="lane-empty">Loading...</span></div></div>\n  </div>\n  <!-- Open Positions -->\n  <div class="section">\n    <div class="section-title">Open Positions</div>\n    <div id="positions-wrap"><span class="empty">Loading...</span></div>\n  </div>\n  <!-- Trade History -->\n  <div class="section">\n    <div class="section-title">Trade History</div>\n    <div id="trades-wrap"><span class="empty">Loading...</span></div>\n  </div>\n</main>\n<script src="/paper.js"></script>\n</body>\n</html>'

PAPER_JS = '\n// ── Model thresholds (mirror main dashboard) ─────────────────────────────────\nconst MODELS = [\n  { name: \'Standard\',    conf: 0.50,  mos: 0.15,   fud: 0.60 },\n  { name: \'Relaxed -25%\', conf: 0.375, mos: 0.1125, fud: 0.45 },\n  { name: \'Relaxed -50%\', conf: 0.25,  mos: 0.075,  fud: 0.30 },\n];\n\n// ── Formatters ────────────────────────────────────────────────────────────────\nfunction fmt(n, d) {\n  if (d === undefined) d = 2;\n  if (n == null) return \'\\u2014\';\n  return \'$\' + Math.abs(n).toLocaleString(\'en-US\', {minimumFractionDigits: d, maximumFractionDigits: d});\n}\nfunction fmtPct(n) { return n == null ? \'\' : (n >= 0 ? \'+\' : \'\') + n.toFixed(2) + \'%\'; }\nfunction cls(n)    { return n > 0 ? \'up\' : n < 0 ? \'dn\' : \'neu\'; }\n\n// ── Account + trades ──────────────────────────────────────────────────────────\nasync function load() {\n  try {\n    const [acct, trades] = await Promise.all([\n      fetch(\'/api/paper/account\').then(r => r.json()),\n      fetch(\'/api/paper/trades\').then(r => r.json()),\n    ]);\n\n    document.getElementById(\'c-equity\').textContent = fmt(acct.total_equity, 0);\n    document.getElementById(\'c-cash\').textContent = fmt(acct.cash, 0);\n    document.getElementById(\'c-cash-sub\').textContent =\n      ((acct.cash / acct.total_equity) * 100).toFixed(1) + \'% of portfolio\';\n    document.getElementById(\'c-invested\').textContent = fmt(acct.positions_value, 0);\n    document.getElementById(\'c-invested-sub\').textContent =\n      ((acct.positions_value / acct.total_equity) * 100).toFixed(1) + \'% of portfolio\';\n\n    const pnlEl = document.getElementById(\'c-pnl\');\n    pnlEl.textContent = (acct.total_pnl >= 0 ? \'+\' : \'\') + fmt(acct.total_pnl, 0);\n    pnlEl.className = \'card-value \' + cls(acct.total_pnl);\n    document.getElementById(\'c-pnl-sub\').innerHTML =\n      \'<span class="\' + cls(acct.total_pnl_pct) + \'">\' + fmtPct(acct.total_pnl_pct) + \'</span> vs $100k start\';\n\n    // Positions\n    const pw = document.getElementById(\'positions-wrap\');\n    if (!acct.positions || !acct.positions.length) {\n      pw.innerHTML = \'<span class="empty">No open positions yet.</span>\';\n    } else {\n      let h = \'<table><tr><th>Ticker</th><th>Qty</th><th>Avg Cost</th><th>Price</th><th>Mkt Value</th><th>P&amp;L</th><th>%</th></tr>\';\n      for (const p of acct.positions) {\n        h += \'<tr><td><strong>\' + p.ticker + \'</strong></td><td>\' + p.qty + \'</td><td>\' +\n          fmt(p.avg_cost) + \'</td><td>\' + fmt(p.cur_price) + \'</td><td>\' + fmt(p.mkt_val, 0) +\n          \'</td><td class="\' + cls(p.pnl) + \'">\' + (p.pnl >= 0 ? \'+\' : \'\') + fmt(p.pnl) +\n          \'</td><td class="\' + cls(p.pnl_pct) + \'">\' + fmtPct(p.pnl_pct) + \'</td></tr>\';\n      }\n      pw.innerHTML = h + \'</table>\';\n    }\n\n    // Trades\n    const tw = document.getElementById(\'trades-wrap\');\n    if (!trades || !trades.length) {\n      tw.innerHTML = \'<span class="empty">No trades yet \\u2014 click Run Now or start python -m paper.runner</span>\';\n    } else {\n      let h = \'<table><tr><th>Time</th><th>Ticker</th><th>Action</th><th>Qty</th><th>Price</th><th>Total</th><th>Cash After</th></tr>\';\n      for (const t of trades) {\n        const dt = t.timestamp\n          ? new Date(t.timestamp + \'Z\').toLocaleString([], {month:\'2-digit\',day:\'2-digit\',hour:\'2-digit\',minute:\'2-digit\'})\n          : \'\\u2014\';\n        h += \'<tr><td class="neu" style="font-size:11px">\' + dt + \'</td><td><strong>\' + t.ticker +\n          \'</strong></td><td class="\' + (t.action===\'BUY\'?\'up\':\'dn\') + \'">\' + t.action +\n          \'</td><td>\' + t.qty + \'</td><td>\' + fmt(t.price) + \'</td><td>\' + fmt(t.total, 0) +\n          \'</td><td class="neu">\' + fmt(t.cash_after, 0) + \'</td></tr>\';\n      }\n      tw.innerHTML = h + \'</table>\';\n    }\n  } catch(e) { console.error(\'Paper load error:\', e); }\n}\n\n// ── Swim lanes ────────────────────────────────────────────────────────────────\nasync function loadSwimLanes() {\n  try {\n    const signals = await fetch(\'/api/signals\').then(r => r.json());\n    if (!signals || !signals.length) {\n      for (let i = 0; i < 3; i++)\n        document.getElementById(\'lane-\' + i).innerHTML =\n          \'<span class="lane-empty">No signals yet.</span>\';\n      return;\n    }\n\n    MODELS.forEach((m, idx) => {\n      // Tickers that pass this model\'s gates\n      const passing = signals.filter(s => {\n        const conf = s.confidence || 0;\n        const mos  = s.margin_of_safety || 0;\n        const fud  = s.fud_score || 0;\n        const sig  = (s.signal || \'\').toUpperCase();\n        return conf >= m.conf && mos >= m.mos && fud >= m.fud\n               && (sig === \'BUY\' || sig === \'STRONG_BUY\');\n      });\n\n      const el = document.getElementById(\'lane-\' + idx);\n      if (!passing.length) {\n        el.innerHTML = \'<span class="lane-empty">No tickers clear this threshold.</span>\';\n        return;\n      }\n\n      // Sort by confidence desc\n      passing.sort((a, b) => (b.confidence || 0) - (a.confidence || 0));\n\n      el.innerHTML = passing.map(s => {\n        const conf   = ((s.confidence || 0) * 100).toFixed(0);\n        const mos    = ((s.margin_of_safety || 0) * 100).toFixed(1);\n        const fud    = (s.fud_score || 0).toFixed(2);\n        const price  = s.current_price ? \'$\' + s.current_price.toFixed(2) : \'\';\n        const chg    = s.change_pct != null\n          ? \'<span class="\' + cls(s.change_pct) + \'">\' + (s.change_pct >= 0 ? \'+\' : \'\') + s.change_pct.toFixed(1) + \'%</span>\'\n          : \'\';\n        return \'<div class="signal-card">\' +\n          \'<div class="sig-ticker sig-bull">&#9650; \' + s.ticker +\n            (price ? \' <span class="neu" style="font-weight:400">\' + price + \'</span>\' : \'\') +\n            (chg ? \' \' + chg : \'\') +\n          \'</div>\' +\n          \'<div class="sig-meta">\' +\n            \'<span>Conf \' + conf + \'%</span>\' +\n            \'<span>MoS \' + mos + \'%</span>\' +\n            \'<span>FUD \' + fud + \'</span>\' +\n          \'</div>\' +\n        \'</div>\';\n      }).join(\'\');\n    });\n  } catch(e) { console.error(\'Swim lanes error:\', e); }\n}\n\n// ── Run Now ───────────────────────────────────────────────────────────────────\nlet _runPollTimer = null;\n\nasync function runNow() {\n  const btn = document.getElementById(\'run-btn\');\n  const status = document.getElementById(\'run-status\');\n  btn.disabled = true;\n  status.textContent = \'\\u29d7 Cycle running...\';\n\n  try {\n    const r = await fetch(\'/api/paper/run\', {method: \'POST\'}).then(r => r.json());\n    if (r.status === \'started\') {\n      status.textContent = \'\\u29d7 Running pipeline...\';\n      // Poll every 5s until done\n      _runPollTimer = setInterval(async () => {\n        const s = await fetch(\'/api/paper/run/status\').then(r => r.json());\n        if (!s.running) {\n          clearInterval(_runPollTimer);\n          btn.disabled = false;\n          status.textContent = \'\\u2713 Done \\u2014 \' + new Date().toLocaleTimeString([], {hour:\'2-digit\',minute:\'2-digit\'});\n          load();\n          loadSwimLanes();\n          if (typeof sg3Boot === \'function\') sg3Boot();\n          setTimeout(() => { status.textContent = \'\'; }, 8000);\n        }\n      }, 5000);\n    } else {\n      status.textContent = r.status || \'Already running\';\n      btn.disabled = false;\n    }\n  } catch(e) {\n    status.textContent = \'Error: \' + e.message;\n    btn.disabled = false;\n  }\n}\n\nasync function resetAccount() {\n  if (!confirm(\'Reset paper account to $100,000? This erases all trades and positions.\')) return;\n  await fetch(\'/api/paper/reset\', {method: \'POST\'});\n  load();\n}\n\n// ── Init ──────────────────────────────────────────────────────────────────────\nload();\nloadSwimLanes();\nsetInterval(() => { load(); loadSwimLanes(); }, 30000);\n\n// ── Auto-scheduler status bar ────────────────────────────────────────────────\nlet _schedPaused = false;\n\nasync function loadSchedStatus() {\n  try {\n    const s = await fetch(\'/api/paper/scheduler\').then(r => r.json());\n    _schedPaused = s.paused;\n    const modeEl   = document.getElementById(\'sched-mode\');\n    const lastEl   = document.getElementById(\'sched-last\');\n    const nextEl   = document.getElementById(\'sched-next\');\n    const stopsEl  = document.getElementById(\'sched-stops\');\n    const toggleEl = document.getElementById(\'sched-toggle\');\n    if (!modeEl) return;\n\n    if (s.paused) {\n      modeEl.innerHTML = \'&#9899; Auto: <span style="color:#f85149">PAUSED</span>\';\n      if (toggleEl) { toggleEl.textContent = \'Resume\'; toggleEl.style.color = \'#3fb950\'; }\n    } else if (!s.market_hours) {\n      modeEl.innerHTML = \'&#9711; Auto: market closed (runs 9:30\u20134pm ET Mon\u2013Fri)\';\n      if (toggleEl) { toggleEl.textContent = \'Pause\'; toggleEl.style.color = \'#8b949e\'; }\n    } else if (s.running) {\n      modeEl.innerHTML = \'&#9899; Auto: <span style="color:#d29922">cycle running\u2026</span>\';\n    } else {\n      modeEl.innerHTML = \'&#9898; Auto: <span style="color:#3fb950">active</span> \u00b7 every 5 min\';\n      if (toggleEl) { toggleEl.textContent = \'Pause\'; toggleEl.style.color = \'#8b949e\'; }\n    }\n    lastEl.textContent  = s.last_cycle  ? \'Last: \' + s.last_cycle  : \'\';\n    nextEl.textContent  = s.next_cycle  ? \'Next: \' + s.next_cycle  : \'\';\n    stopsEl.textContent = s.stop_exits  ? \'Stop exits: \' + s.stop_exits : \'\';\n  } catch(e) {}\n}\n\nasync function toggleScheduler() {\n  const action = _schedPaused ? \'resume\' : \'pause\';\n  await fetch(\'/api/paper/scheduler/\' + action, {method: \'POST\'});\n  loadSchedStatus();\n}\n\nloadSchedStatus();\nsetInterval(loadSchedStatus, 15000);\n'


@app.get("/paper", response_class=HTMLResponse)
def paper_page():
    from fastapi.responses import HTMLResponse as HR
    return HR(content=PAPER_HTML, headers={"Cache-Control": "no-store"})


@app.get("/paper.js")
def paper_js_route():
    from fastapi.responses import Response
    return Response(content=PAPER_JS, media_type="application/javascript; charset=utf-8",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/paper/account")
def api_paper_account():
    try:
        from paper.executor import PaperExecutor
        from models.database import init_db as _init_db
        _, _Session = _init_db(config.database.url, echo=False)
        ex = PaperExecutor(main_db_session_factory=_Session)
        return ex.get_account_summary()
    except Exception as e:
        return JSONResponse(status_code=503, content={"error": str(e)})


@app.get("/api/paper/trades")
def api_paper_trades():
    try:
        from paper.executor import PaperExecutor
        ex = PaperExecutor()
        return ex.get_recent_trades(limit=100)
    except Exception as e:
        return JSONResponse(status_code=503, content={"error": str(e)})


@app.post("/api/paper/reset")
def api_paper_reset():
    try:
        from paper.executor import PaperExecutor
        ex = PaperExecutor()
        ex.reset_account()
        return {"status": "reset"}
    except Exception as e:
        return JSONResponse(status_code=503, content={"error": str(e)})


_paper_running = False


@app.post("/api/paper/run")
def api_paper_run():
    global _paper_running
    if _paper_running:
        return {"status": "already_running"}
    try:
        from paper.auto_scheduler import get_scheduler
        sched = get_scheduler()
        if sched:
            if sched._cycle_running:
                return {"status": "already_running"}
            # Reuse the persistent engine pool — no cold-start overhead
            sched.trigger_cycle()
            return {"status": "started"}
    except Exception:
        pass

    # Fallback: scheduler not ready yet — spin up a one-shot thread
    import threading

    def _run():
        global _paper_running
        try:
            from models.database import init_db as _init_db
            _, _Session_pre = _init_db(config.database.url, echo=False)
            try:
                from pipeline.ingestion import IngestionPipeline
                price_pipeline = IngestionPipeline(db_session_factory=_Session_pre)
                price_pipeline.run_prices_only()
            except Exception as _pe:
                logger.warning(f"[PAPER] Price refresh failed (using cached): {_pe}")
        except Exception:
            pass
        try:
            from paper.runner import run_paper_cycle
            from analysis.engine import FirstPrinciplesEngine
            from signals.fft_cycles import FFTCycleDetector
            from signals.fibonacci import FibonacciAnalyzer
            from signals.insider_flow import InsiderFlowAnalyzer
            from signals.market_microstructure import (
                VWAPCalculator, VolumeProfileAnalyzer, VIXRegimeDetector
            )
            from signals.aggregator import SignalAggregator
            from fud.filter_engine import FUDFilterEngine
            from decision.engine import DecisionEngine
            from risk.manager import RiskManager
            from broker.market_data import SchwabMarketData
            from paper.executor import PaperExecutor
            from models.database import init_db as _init_db
            _apply_thresh_override()
            _, _Session = _init_db(config.database.url, echo=False)
            run_paper_cycle(
                Session=_Session,
                analysis_engine=FirstPrinciplesEngine(db_session_factory=_Session),
                fft_detector=FFTCycleDetector(),
                fib_analyzer=FibonacciAnalyzer(),
                insider_analyzer=InsiderFlowAnalyzer(),
                vwap_calc=VWAPCalculator(),
                vol_analyzer=VolumeProfileAnalyzer(),
                vix_detector=VIXRegimeDetector(),
                aggregator=SignalAggregator(),
                fud_engine=FUDFilterEngine(db_session_factory=_Session),
                decision_engine=DecisionEngine(db_session_factory=_Session),
                risk_manager=RiskManager(db_session_factory=_Session),
                executor=PaperExecutor(main_db_session_factory=_Session),
                market_data=SchwabMarketData(),
            )
        except Exception as e:
            logger.warning(f"[PAPER] Run-now cycle failed: {e}")
        finally:
            _paper_running = False

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "started"}


@app.get("/api/paper/run/status")
def api_paper_run_status():
    running = _paper_running
    if not running:
        try:
            from paper.auto_scheduler import get_scheduler as _gs
            _s = _gs()
            if _s:
                running = _s._cycle_running
        except Exception:
            pass
    return {"running": running}


# ── Stage Gate ────────────────────────────────────────────────────────────────

STAGEGATE_HTML = '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="UTF-8">\n<meta name="viewport" content="width=device-width, initial-scale=1.0">\n<title>Stage Gate \\u2014 NWO</title>\n<style>\n  * { box-sizing: border-box; margin: 0; padding: 0; }\n  body { background: #0d1117; color: #e6edf3; font-family: \'Segoe UI\', monospace; font-size: 14px; min-height: 100vh; }\n  header { background: #161b22; padding: 12px 20px; border-bottom: 1px solid #30363d;\n           display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }\n  .back-btn { padding: 5px 12px; border-radius: 6px; border: 1px solid #30363d;\n              background: #21262d; color: #8b949e; text-decoration: none; font-size: 12px; }\n  header h1 { font-size: 17px; letter-spacing: 1px; color: #58a6ff; }\n  .header-right { margin-left: auto; display: flex; gap: 8px; align-items: center; }\n  .add-form { display: flex; gap: 6px; }\n  .add-input { padding: 5px 10px; border-radius: 6px; border: 1px solid #30363d;\n               background: #21262d; color: #e6edf3; font-size: 12px; width: 120px; text-transform: uppercase; }\n  .add-input::placeholder { color: #8b949e; text-transform: none; }\n  .add-btn { padding: 5px 12px; border-radius: 6px; border: 1px solid #58a6ff;\n             background: #0d1e36; color: #58a6ff; cursor: pointer; font-size: 12px; }\n  .add-btn:hover { background: #1c2e50; }\n  .save-badge { font-size: 11px; color: #3fb950; display: none; }\n  /* Two-column layout */\n  .stages { display: grid; grid-template-columns: 1fr 1fr; gap: 0; height: calc(100vh - 57px); }\n  .stage { display: flex; flex-direction: column; border-right: 1px solid #30363d; overflow: hidden; }\n  .stage:last-child { border-right: none; }\n  .stage-header { padding: 14px 18px; background: #161b22; border-bottom: 1px solid #30363d;\n                  display: flex; align-items: center; gap: 10px; flex-shrink: 0; }\n  .stage-1 .stage-header { border-top: 3px solid #8b949e; }\n  .stage-2 .stage-header { border-top: 3px solid #3fb950; }\n  .stage-title { font-size: 14px; font-weight: 700; letter-spacing: 0.5px; }\n  .stage-1 .stage-title { color: #8b949e; }\n  .stage-2 .stage-title { color: #3fb950; }\n  .stage-subtitle { font-size: 11px; color: #8b949e; margin-top: 1px; }\n  .stage-count { margin-left: auto; font-size: 11px; color: #8b949e; background: #21262d;\n                 padding: 2px 8px; border-radius: 10px; }\n  /* Drop zone */\n  .drop-zone { flex: 1; overflow-y: auto; padding: 12px;\n               display: flex; flex-direction: column; gap: 8px; }\n  .drop-zone.drag-over { background: rgba(88,166,255,0.05);\n                          outline: 2px dashed #58a6ff; outline-offset: -4px; border-radius: 4px; }\n  /* Stock cards */\n  .stock-card { background: #161b22; border: 1px solid #30363d; border-radius: 8px;\n                padding: 10px 14px; cursor: grab; display: flex; align-items: center;\n                gap: 10px; transition: border-color 0.15s, background 0.15s;\n                user-select: none; }\n  .stock-card:hover { border-color: #58a6ff; background: #1c2128; }\n  .stock-card:active { cursor: grabbing; }\n  .stock-card.dragging { opacity: 0.4; }\n  .stage-2 .stock-card { border-left: 3px solid #3fb950; }\n  .card-ticker { font-size: 15px; font-weight: 700; min-width: 60px; }\n  .card-meta { font-size: 11px; color: #8b949e; flex: 1; }\n  .card-signal { font-size: 11px; font-weight: 600; }\n  .sig-bull { color: #3fb950; } .sig-bear { color: #f85149; } .sig-hold { color: #8b949e; }\n  .card-remove { background: none; border: none; color: #8b949e; cursor: pointer;\n                 font-size: 14px; padding: 2px 4px; border-radius: 4px; line-height: 1; }\n  .card-remove:hover { color: #f85149; background: rgba(248,81,73,0.1); }\n  .drop-hint { color: #8b949e; font-size: 12px; text-align: center; padding: 30px;\n               border: 2px dashed #21262d; border-radius: 8px; font-style: italic; margin-top: 4px; }\n  @media (max-width: 700px) {\n    .stages { grid-template-columns: 1fr; height: auto; }\n    .stage { min-height: 40vh; border-right: none; border-bottom: 1px solid #30363d; }\n  }\n</style>\n</head>\n<body>\n<header>\n  <a href="/" class="back-btn">&#8592; Dashboard</a>\n  <h1>&#127760; Stage Gate</h1>\n  <div class="header-right">\n    <div class="add-form">\n      <input class="add-input" id="add-input" type="text" placeholder="Add ticker..." maxlength="10"\n             onkeydown="if(event.key===\'Enter\') addTicker()">\n      <button class="add-btn" onclick="addTicker()">+ Add</button>\n    </div>\n    <span class="save-badge" id="save-badge">&#10003; Saved</span>\n  </div>\n</header>\n\n<div class="stages">\n  <!-- Stage 1 -->\n  <div class="stage stage-1">\n    <div class="stage-header">\n      <div>\n        <div class="stage-title">&#128203; Stage 1 &mdash; Monitoring</div>\n        <div class="stage-subtitle">Watching only &middot; drag right to activate</div>\n      </div>\n      <span class="stage-count" id="count-1">0</span>\n    </div>\n    <div class="drop-zone" id="zone-1"\n         ondragover="onDragOver(event,\'1\')" ondragleave="onDragLeave(\'1\')" ondrop="onDrop(event,\'1\')">\n      <div class="drop-hint">Drag stocks here to monitor (no trading)</div>\n    </div>\n  </div>\n\n  <!-- Stage 2 -->\n  <div class="stage stage-2">\n    <div class="stage-header">\n      <div>\n        <div class="stage-title">&#9654; Stage 2 &mdash; Active Trading</div>\n        <div class="stage-subtitle">Full AI pipeline &middot; paper &amp; live execution</div>\n      </div>\n      <span class="stage-count" id="count-2">0</span>\n    </div>\n    <div class="drop-zone" id="zone-2"\n         ondragover="onDragOver(event,\'2\')" ondragleave="onDragLeave(\'2\')" ondrop="onDrop(event,\'2\')">\n      <div class="drop-hint">Drag stocks here to activate AI analysis &amp; trading</div>\n    </div>\n  </div>\n</div>\n\n<script src="/stagegate.js"></script>\n</body>\n</html>'
STAGEGATE_JS   = '\n// ── State ─────────────────────────────────────────────────────────────────────\nlet _state = { stage1: [], stage2: [] };\nlet _signals = {};   // ticker -> signal data from /api/signals\nlet _dragTicker = null;\nlet _dragFrom   = null;\n\n// ── Boot ──────────────────────────────────────────────────────────────────────\nasync function boot() {\n  // Load signals for metadata (confidence, signal type, price)\n  try {\n    const sigs = await fetch(\'/api/signals\').then(r => r.json());\n    (sigs || []).forEach(s => { _signals[s.ticker] = s; });\n  } catch(e) {}\n\n  // Load stage state\n  try {\n    _state = await fetch(\'/api/stagegate\').then(r => r.json());\n  } catch(e) {}\n\n  render();\n}\n\n// ── Render ────────────────────────────────────────────────────────────────────\nfunction render() {\n  renderZone(\'1\', _state.stage1);\n  renderZone(\'2\', _state.stage2);\n  document.getElementById(\'count-1\').textContent = _state.stage1.length;\n  document.getElementById(\'count-2\').textContent = _state.stage2.length;\n}\n\nfunction renderZone(stage, tickers) {\n  const zone = document.getElementById(\'zone-\' + stage);\n  if (!tickers.length) {\n    zone.innerHTML = stage === \'1\'\n      ? \'<div class="drop-hint">Drag stocks here to monitor (no trading)</div>\'\n      : \'<div class="drop-hint">Drag stocks here to activate AI analysis &amp; trading</div>\';\n    return;\n  }\n  zone.innerHTML = tickers.map(ticker => cardHtml(ticker, stage)).join(\'\');\n}\n\nfunction cardHtml(ticker, stage) {\n  const s = _signals[ticker] || {};\n  const sig = (s.signal || \'HOLD\').toUpperCase();\n  const sigCls = sig === \'BUY\' || sig === \'STRONG_BUY\' ? \'sig-bull\'\n               : sig === \'SELL\' || sig === \'STRONG_SELL\' ? \'sig-bear\' : \'sig-hold\';\n  const price = s.current_price ? \'$\' + s.current_price.toFixed(2) : \'\';\n  const conf  = s.confidence ? (s.confidence * 100).toFixed(0) + \'% conf\' : \'\';\n  const meta  = [price, conf].filter(Boolean).join(\' \\u00b7 \');\n  return \'<div class="stock-card" draggable="true" data-ticker="\' + ticker + \'" data-stage="\' + stage + \'" \'\n    + \'ondragstart="onDragStart(event)" ondragend="onDragEnd(event)">\'\n    + \'<div class="card-ticker">\' + ticker + \'</div>\'\n    + \'<div class="card-meta">\' + meta + \'</div>\'\n    + \'<span class="card-signal \' + sigCls + \'">\' + sig + \'</span>\'\n    + \'<button class="card-remove" onclick="removeTicker(\\\'\' + ticker + \'\\\')" title="Remove">&#x2715;</button>\'\n    + \'</div>\';\n}\n\n// ── Drag & Drop ───────────────────────────────────────────────────────────────\nfunction onDragStart(e) {\n  _dragTicker = e.currentTarget.dataset.ticker;\n  _dragFrom   = e.currentTarget.dataset.stage;\n  e.currentTarget.classList.add(\'dragging\');\n  e.dataTransfer.effectAllowed = \'move\';\n}\n\nfunction onDragEnd(e) {\n  e.currentTarget.classList.remove(\'dragging\');\n}\n\nfunction onDragOver(e, stage) {\n  e.preventDefault();\n  e.dataTransfer.dropEffect = \'move\';\n  document.getElementById(\'zone-\' + stage).classList.add(\'drag-over\');\n}\n\nfunction onDragLeave(stage) {\n  document.getElementById(\'zone-\' + stage).classList.remove(\'drag-over\');\n}\n\nfunction onDrop(e, targetStage) {\n  e.preventDefault();\n  document.getElementById(\'zone-\' + targetStage).classList.remove(\'drag-over\');\n  if (!_dragTicker || _dragFrom === targetStage) return;\n\n  // Move ticker\n  const fromArr = _state[\'stage\' + _dragFrom];\n  const toArr   = _state[\'stage\' + targetStage];\n  const idx = fromArr.indexOf(_dragTicker);\n  if (idx !== -1) fromArr.splice(idx, 1);\n  if (!toArr.includes(_dragTicker)) toArr.push(_dragTicker);\n\n  render();\n  save();\n}\n\n// ── Add / Remove ──────────────────────────────────────────────────────────────\nfunction addTicker() {\n  const inp = document.getElementById(\'add-input\');\n  const ticker = inp.value.trim().toUpperCase();\n  inp.value = \'\';\n  if (!ticker) return;\n  if (_state.stage1.includes(ticker) || _state.stage2.includes(ticker)) return;\n  _state.stage1.push(ticker);\n  render();\n  save();\n}\n\nfunction removeTicker(ticker) {\n  _state.stage1 = _state.stage1.filter(t => t !== ticker);\n  _state.stage2 = _state.stage2.filter(t => t !== ticker);\n  render();\n  save();\n}\n\n// ── Persist ───────────────────────────────────────────────────────────────────\nasync function save() {\n  try {\n    await fetch(\'/api/stagegate\', {\n      method: \'POST\',\n      headers: {\'Content-Type\': \'application/json\'},\n      body: JSON.stringify(_state),\n    });\n    const badge = document.getElementById(\'save-badge\');\n    badge.style.display = \'inline\';\n    setTimeout(() => { badge.style.display = \'none\'; }, 2000);\n  } catch(e) {}\n}\n\nboot();\n'

_STAGEGATE_FILE = "data/stagegate.json"


def _load_stagegate() -> dict:
    """Load stage gate state; seed Stage 1 from signals if first run."""
    import json, os
    from pathlib import Path
    if Path(_STAGEGATE_FILE).exists():
        try:
            with open(_STAGEGATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    # First run — seed Stage 1 from config watchlist
    return {"stage1": list(config.watchlist), "stage2": [], "stage3": []}


def _save_stagegate(data: dict):
    import json, os
    os.makedirs("data", exist_ok=True)
    with open(_STAGEGATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


@app.get("/stagegate", response_class=HTMLResponse)
def stagegate_page():
    from fastapi.responses import HTMLResponse as HR
    return HR(content=STAGEGATE_HTML, headers={"Cache-Control": "no-store"})


@app.get("/stagegate.js")
def stagegate_js():
    from fastapi.responses import Response
    return Response(content=STAGEGATE_JS,
                    media_type="application/javascript; charset=utf-8",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/stagegate")
def api_stagegate_get():
    return _load_stagegate()


@app.post("/api/stagegate")
async def api_stagegate_save(request: Request):
    import json, threading
    body = await request.json()
    stage1 = body.get("stage1", [])
    stage2 = body.get("stage2", [])
    stage3 = body.get("stage3", [])

    # Detect tickers that are genuinely new (not in the previous stagegate)
    existing = _load_stagegate()
    existing_all = set(existing.get("stage1", []) + existing.get("stage2", []) + existing.get("stage3", []))
    incoming_all = set(stage1 + stage2 + stage3)
    new_tickers  = [t for t in incoming_all - existing_all if t]

    _save_stagegate({"stage1": stage1, "stage2": stage2, "stage3": stage3})

    # Auto-ingest any new tickers in the background (prices + fundamentals)
    if new_tickers:
        import logging as _log
        _log.getLogger("stagegate").info(f"[STAGEGATE] New tickers detected: {new_tickers} — auto-ingesting")

        def _ingest_new():
            try:
                from models.database import init_db as _idb
                from pipeline.ingestion import IngestionPipeline
                _, _S = _idb(config.database.url, echo=False)
                IngestionPipeline(db_session_factory=_S).run_full_ingest(tickers=new_tickers)
            except Exception as e:
                import logging as _log2
                _log2.getLogger("stagegate").warning(f"[STAGEGATE] Auto-ingest failed: {e}")

        threading.Thread(target=_ingest_new, daemon=True, name="sg-ingest").start()

    return {"status": "ok", "stage1": len(stage1), "stage2": len(stage2),
            "ingesting": new_tickers}


# ── Stage Gate embedded in Paper Trading (auto-patched) ───────────────────────
_SG_CSS  = '\n  /* ── Stage Gate ─────────────────────────────────────────────── */\n  .sg-stages { display: grid; grid-template-columns: 1fr 1fr; gap: 0; }\n  .sg-stage { display: flex; flex-direction: column; border-right: 1px solid #21262d; min-width: 0; }\n  .sg-stage:last-child { border-right: none; }\n  .sg-stage-header { padding: 10px 14px; background: #0d1117; border-bottom: 1px solid #21262d;\n                     display: flex; align-items: center; gap: 8px; }\n  .sg-stage-1 .sg-stage-header { border-top: 3px solid #8b949e; }\n  .sg-stage-2 .sg-stage-header { border-top: 3px solid #3fb950; }\n  .sg-stage-title { font-size: 12px; font-weight: 700; }\n  .sg-stage-1 .sg-stage-title { color: #8b949e; }\n  .sg-stage-2 .sg-stage-title { color: #3fb950; }\n  .sg-stage-sub { font-size: 10px; color: #8b949e; margin-top: 2px; }\n  .sg-count { margin-left: auto; font-size: 11px; color: #8b949e; background: #21262d;\n              padding: 2px 7px; border-radius: 10px; }\n  .sg-drop-zone { flex: 1; min-height: 80px; padding: 8px;\n                  display: flex; flex-direction: column; gap: 6px; }\n  .sg-drop-zone.drag-over { background: rgba(88,166,255,0.05);\n                             outline: 2px dashed #58a6ff; outline-offset: -3px; border-radius: 4px; }\n  .sg-card { background: #0d1117; border: 1px solid #21262d; border-radius: 6px;\n             padding: 7px 10px; cursor: grab; display: flex; align-items: center;\n             gap: 8px; user-select: none; transition: border-color 0.15s; }\n  .sg-card:hover { border-color: #58a6ff; }\n  .sg-card:active { cursor: grabbing; }\n  .sg-card.dragging { opacity: 0.4; }\n  .sg-stage-2 .sg-card { border-left: 3px solid #3fb950; }\n  .sg-ticker { font-size: 13px; font-weight: 700; min-width: 55px; }\n  .sg-meta { font-size: 10px; color: #8b949e; flex: 1; }\n  .sg-signal { font-size: 10px; font-weight: 600; }\n  .sg-remove { background: none; border: none; color: #8b949e; cursor: pointer;\n               font-size: 13px; padding: 1px 3px; border-radius: 3px; line-height: 1; }\n  .sg-remove:hover { color: #f85149; background: rgba(248,81,73,0.1); }\n  .sg-hint { color: #8b949e; font-size: 11px; text-align: center; padding: 20px 8px;\n             border: 2px dashed #21262d; border-radius: 6px; font-style: italic; }\n  /* Activation modal */\n  .sg-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.75);\n                display: flex; align-items: center; justify-content: center; z-index: 9999; }\n  .sg-modal-box { background: #161b22; border: 1px solid #30363d; border-radius: 10px;\n                  padding: 24px; width: 320px; display: flex; flex-direction: column; gap: 14px; }\n  .sg-modal-box h3 { font-size: 15px; color: #e6edf3; }\n  .sg-modal-price { font-size: 12px; color: #8b949e; }\n  .sg-toggle { display: flex; gap: 20px; font-size: 13px; }\n  .sg-toggle label { display: flex; align-items: center; gap: 6px; cursor: pointer; color: #e6edf3; }\n  .sg-amount-input { width: 100%; padding: 9px 12px; border-radius: 6px; border: 1px solid #30363d;\n                     background: #21262d; color: #e6edf3; font-size: 15px; }\n  .sg-amount-input:focus { outline: none; border-color: #58a6ff; }\n  .sg-modal-hint { font-size: 11px; color: #8b949e; min-height: 16px; }\n  .sg-modal-btns { display: flex; gap: 8px; justify-content: flex-end; margin-top: 4px; }\n  .sg-cancel-btn { padding: 7px 16px; border-radius: 6px; border: 1px solid #30363d;\n                   background: transparent; color: #8b949e; cursor: pointer; font-size: 13px; }\n  .sg-cancel-btn:hover { background: #21262d; }\n  .sg-act-btn { padding: 7px 18px; border-radius: 6px; border: 1px solid #3fb950;\n                background: #1a4731; color: #3fb950; cursor: pointer; font-size: 13px; font-weight: 600; }\n  .sg-act-btn:hover { background: #1e5c3a; }\n  .sg-act-btn:disabled { opacity: 0.5; cursor: not-allowed; }\n'
_SG_HTML = '\n  <!-- ── Stage Gate ───────────────────────────────────────────── -->\n  <div class="section">\n    <div class="section-title">&#127760; Stage Gate &mdash; Stock Activation</div>\n    <div class="sg-stages">\n      <div class="sg-stage sg-stage-1">\n        <div class="sg-stage-header">\n          <div>\n            <div class="sg-stage-title">&#128203; Stage 1 &mdash; Monitoring</div>\n            <div class="sg-stage-sub">Watching only &middot; drag right to activate trading</div>\n          </div>\n          <span class="sg-count" id="sg-count-1">0</span>\n        </div>\n        <div class="sg-drop-zone" id="sg-zone-1"\n             ondragover="sgDragOver(event,\'1\')" ondragleave="sgDragLeave(\'1\')" ondrop="sgDrop(event,\'1\')">\n          <div class="sg-hint">Drag stocks here to monitor (no trading)</div>\n        </div>\n      </div>\n      <div class="sg-stage sg-stage-2">\n        <div class="sg-stage-header">\n          <div>\n            <div class="sg-stage-title">&#9654; Stage 2 &mdash; Active Trading</div>\n            <div class="sg-stage-sub">AI pipeline &middot; paper execution</div>\n          </div>\n          <span class="sg-count" id="sg-count-2">0</span>\n        </div>\n        <div class="sg-drop-zone" id="sg-zone-2"\n             ondragover="sgDragOver(event,\'2\')" ondragleave="sgDragLeave(\'2\')" ondrop="sgDrop(event,\'2\')">\n          <div class="sg-hint">Drag here to activate AI analysis &amp; trading</div>\n        </div>\n      </div>\n    </div>\n  </div>\n\n  <!-- Activation modal -->\n  <div id="sg-overlay" class="sg-overlay" style="display:none">\n    <div class="sg-modal-box">\n      <h3 id="sg-modal-title">Activate for Trading</h3>\n      <p class="sg-modal-price" id="sg-modal-price"></p>\n      <div class="sg-toggle">\n        <label><input type="radio" name="sg-mode" id="sg-mode-shares" value="shares" checked onchange="sgUpdateHint()"> Shares</label>\n        <label><input type="radio" name="sg-mode" id="sg-mode-dollars" value="dollars" onchange="sgUpdateHint()"> Amount ($)</label>\n      </div>\n      <input class="sg-amount-input" id="sg-amount" type="number" min="1" step="1"\n             placeholder="Enter amount..." oninput="sgUpdateHint()"\n             onkeydown="if(event.key===\'Enter\') sgModalConfirm()">\n      <p class="sg-modal-hint" id="sg-modal-hint">&nbsp;</p>\n      <div class="sg-modal-btns">\n        <button class="sg-cancel-btn" onclick="sgModalCancel()">Cancel</button>\n        <button class="sg-act-btn" id="sg-act-btn" onclick="sgModalConfirm()">&#9654; Start Trading</button>\n      </div>\n    </div>\n  </div>\n'
_SG_JS   = '\n// ── Stage Gate (embedded in paper dashboard) ─────────────────────────────────\nlet _sgState   = { stage1: [], stage2: [] };\nlet _sgSigs    = {};\nlet _sgDragT   = null;\nlet _sgDragF   = null;\nlet _sgPending = null;\n\nasync function sgBoot() {\n  try {\n    const sigs = await fetch(\'/api/signals\').then(r => r.json());\n    (sigs || []).forEach(s => { _sgSigs[s.ticker] = s; });\n  } catch(e) {}\n  try { _sgState = await fetch(\'/api/stagegate\').then(r => r.json()); } catch(e) {}\n  sgRender();\n}\n\nfunction sgRender() {\n  const s1 = _sgState.stage1 || [], s2 = _sgState.stage2 || [];\n  sgRenderZone(\'1\', s1);\n  sgRenderZone(\'2\', s2);\n  document.getElementById(\'sg-count-1\').textContent = s1.length;\n  document.getElementById(\'sg-count-2\').textContent = s2.length;\n}\n\nfunction sgRenderZone(stage, tickers) {\n  const zone = document.getElementById(\'sg-zone-\' + stage);\n  if (!tickers.length) {\n    zone.innerHTML = stage === \'1\'\n      ? \'<div class="sg-hint">Drag stocks here to monitor (no trading)</div>\'\n      : \'<div class="sg-hint">Drag here to activate AI analysis &amp; trading</div>\';\n    return;\n  }\n  zone.innerHTML = tickers.map(t => sgCardHtml(t, stage)).join(\'\');\n}\n\nfunction sgCardHtml(ticker, stage) {\n  const s   = _sgSigs[ticker] || {};\n  const sig = (s.signal || \'HOLD\').toUpperCase();\n  const sc  = sig === \'BUY\' || sig === \'STRONG_BUY\'   ? \'sig-bull\'\n            : sig === \'SELL\' || sig === \'STRONG_SELL\'  ? \'sig-bear\' : \'\';\n  const price = s.current_price ? \'$\' + s.current_price.toFixed(2) : \'\';\n  // Use data-ticker on the remove button — no quote escaping needed in onclick\n  return \'<div class="sg-card" draggable="true" data-ticker="\' + ticker + \'" data-stage="\' + stage + \'" \'\n    + \'ondragstart="sgDragStart(event)" ondragend="sgDragEnd(event)">\'\n    + \'<div class="sg-ticker">\' + ticker + \'</div>\'\n    + \'<div class="sg-meta">\' + price + \'</div>\'\n    + \'<span class="sg-signal \' + sc + \'">\' + sig + \'</span>\'\n    + \'<button class="sg-remove" data-ticker="\' + ticker + \'" onclick="sgRemove(this.dataset.ticker)" title="Remove">&#x2715;</button>\'\n    + \'</div>\';\n}\n\n// ── Drag & drop ───────────────────────────────────────────────────────────────\nfunction sgDragStart(e) {\n  _sgDragT = e.currentTarget.dataset.ticker;\n  _sgDragF = e.currentTarget.dataset.stage;\n  e.currentTarget.classList.add(\'dragging\');\n  e.dataTransfer.effectAllowed = \'move\';\n}\nfunction sgDragEnd(e) { e.currentTarget.classList.remove(\'dragging\'); }\nfunction sgDragOver(e, stage) {\n  e.preventDefault();\n  e.dataTransfer.dropEffect = \'move\';\n  document.getElementById(\'sg-zone-\' + stage).classList.add(\'drag-over\');\n}\nfunction sgDragLeave(stage) {\n  document.getElementById(\'sg-zone-\' + stage).classList.remove(\'drag-over\');\n}\nfunction sgDrop(e, toStage) {\n  e.preventDefault();\n  document.getElementById(\'sg-zone-\' + toStage).classList.remove(\'drag-over\');\n  if (!_sgDragT || _sgDragF === toStage) return;\n  if (toStage === \'2\') {\n    _sgPending = _sgDragT;\n    sgShowModal(_sgDragT);\n  } else {\n    sgMoveLocal(_sgDragT, \'2\', \'1\');\n  }\n}\n\nfunction sgMoveLocal(ticker, from, to) {\n  const fa = _sgState[\'stage\' + from] || [];\n  const ta = _sgState[\'stage\' + to]   || [];\n  const i  = fa.indexOf(ticker);\n  if (i !== -1) fa.splice(i, 1);\n  if (!ta.includes(ticker)) ta.push(ticker);\n  sgRender();\n  sgSave();\n}\n\n// ── Modal ─────────────────────────────────────────────────────────────────────\nfunction sgShowModal(ticker) {\n  const s = _sgSigs[ticker] || {};\n  document.getElementById(\'sg-modal-title\').textContent  = \'Activate \' + ticker + \' for Trading\';\n  document.getElementById(\'sg-modal-price\').textContent  =\n    s.current_price ? \'Current price: $\' + s.current_price.toFixed(2) : \'Price not available\';\n  document.getElementById(\'sg-mode-shares\').checked      = true;\n  document.getElementById(\'sg-amount\').value             = \'\';\n  document.getElementById(\'sg-modal-hint\').innerHTML     = \'&nbsp;\';\n  document.getElementById(\'sg-overlay\').style.display   = \'flex\';\n  setTimeout(() => document.getElementById(\'sg-amount\').focus(), 60);\n}\n\nfunction sgUpdateHint() {\n  const mode  = document.querySelector(\'input[name="sg-mode"]:checked\').value;\n  const amt   = parseFloat(document.getElementById(\'sg-amount\').value);\n  const price = (_sgSigs[_sgPending] || {}).current_price;\n  const hint  = document.getElementById(\'sg-modal-hint\');\n  if (!amt || amt <= 0) { hint.innerHTML = \'&nbsp;\'; return; }\n  if (mode === \'shares\') {\n    hint.textContent = price\n      ? \'Total cost ≈ $\' + (amt * price).toLocaleString(\'en-US\', {minimumFractionDigits:2, maximumFractionDigits:2})\n      : amt + \' shares\';\n  } else {\n    const shares = price ? Math.floor(amt / price) : null;\n    hint.textContent = shares != null\n      ? shares + \' shares @ $\' + price.toFixed(2)\n      : \'$\' + amt + \' allocated\';\n  }\n}\n\nfunction sgModalCancel() {\n  document.getElementById(\'sg-overlay\').style.display = \'none\';\n  _sgPending = null;\n}\n\nasync function sgModalConfirm() {\n  const ticker = _sgPending;\n  const mode   = document.querySelector(\'input[name="sg-mode"]:checked\').value;\n  const amount = parseFloat(document.getElementById(\'sg-amount\').value);\n  if (!ticker || !amount || amount <= 0) return;\n\n  const btn = document.getElementById(\'sg-act-btn\');\n  btn.disabled    = true;\n  btn.textContent = \'Activating…\';\n\n  try {\n    const r = await fetch(\'/api/paper/activate\', {\n      method: \'POST\',\n      headers: {\'Content-Type\': \'application/json\'},\n      body: JSON.stringify({ ticker, mode, amount }),\n    }).then(res => res.json());\n\n    if (r.status === \'ok\') {\n      sgMoveLocal(ticker, \'1\', \'2\');\n      document.getElementById(\'sg-overlay\').style.display = \'none\';\n      _sgPending = null;\n      load();   // refresh account summary\n    } else {\n      alert(\'Could not activate: \' + (r.error || \'unknown error\'));\n    }\n  } catch(e) {\n    alert(\'Error: \' + e.message);\n  } finally {\n    btn.disabled    = false;\n    btn.textContent = \'\\u25b6 Start Trading\';\n  }\n}\n\nfunction sgRemove(ticker) {\n  _sgState.stage1 = (_sgState.stage1 || []).filter(t => t !== ticker);\n  _sgState.stage2 = (_sgState.stage2 || []).filter(t => t !== ticker);\n  sgRender();\n  sgSave();\n}\n\nasync function sgSave() {\n  try {\n    await fetch(\'/api/stagegate\', {\n      method: \'POST\',\n      headers: {\'Content-Type\': \'application/json\'},\n      body: JSON.stringify(_sgState),\n    });\n  } catch(e) {}\n}\n\nsgBoot();\n'

PAPER_HTML = PAPER_HTML.replace('</style>', _SG_CSS + '</style>', 1).replace('</main>', _SG_HTML + '</main>', 1)
PAPER_JS   = PAPER_JS + _SG_JS


@app.post("/api/paper/activate")
async def api_paper_activate(request: Request):
    """Execute a manual paper buy when dragging a stock to Stage 2."""
    import datetime
    body   = await request.json()
    ticker = str(body.get("ticker", "")).upper().strip()
    mode   = body.get("mode", "shares")   # "shares" or "dollars"
    amount = float(body.get("amount", 0))

    if not ticker or amount <= 0:
        return JSONResponse(status_code=400, content={"error": "invalid input"})

    try:
        from paper.executor  import PaperExecutor
        from paper.account   import init_paper_db, PaperAccount, PaperPosition, PaperTrade
        from models.database import init_db as _init_db

        _, MainSession = _init_db(config.database.url, echo=False)
        ex    = PaperExecutor(main_db_session_factory=MainSession)
        price = ex._latest_price(ticker)
        if not price or price <= 0:
            return JSONResponse(status_code=400, content={"error": f"no price data for {ticker}"})

        qty = int(amount / price) if mode == "dollars" else int(amount)
        if qty <= 0:
            return JSONResponse(status_code=400, content={"error": "quantity rounds to zero"})

        _, PaperSession = init_paper_db()
        with PaperSession() as session:
            acct = session.query(PaperAccount).first()
            if not acct:
                return JSONResponse(status_code=503, content={"error": "no paper account"})
            total = round(qty * price, 2)
            if acct.cash < total:
                return JSONResponse(status_code=400, content={
                    "error": f"insufficient cash: have ${acct.cash:,.2f}, need ${total:,.2f}"
                })
            pos = session.query(PaperPosition).filter_by(ticker=ticker).first()
            if pos:
                new_qty      = pos.qty + qty
                pos.avg_cost = (pos.avg_cost * pos.qty + price * qty) / new_qty
                pos.qty      = new_qty
                pos.updated_at = datetime.datetime.utcnow()
            else:
                pos = PaperPosition(ticker=ticker, qty=qty, avg_cost=price)
                session.add(pos)
            acct.cash -= total
            session.add(PaperTrade(
                ticker=ticker, action="BUY", qty=qty, price=price,
                total=total, cash_after=round(acct.cash, 2),
                signal="MANUAL", notes="Stage Gate activation",
            ))
            session.commit()

        # Persist stage gate state change — manual buy goes to Stage 3
        sg = _load_stagegate()
        for k in ("stage1", "stage2", "stage3"):
            sg.setdefault(k, [])
            if ticker in sg[k]:
                sg[k].remove(ticker)
        sg["stage3"].append(ticker)
        _save_stagegate(sg)

        return {"status": "ok", "ticker": ticker, "qty": qty, "price": price, "total": total}

    except Exception as exc:
        import traceback; traceback.print_exc()
        return JSONResponse(status_code=503, content={"error": str(exc)})


# ── AI Decision info modal (auto-patched) ─────────────────────────────────────
_AI_CSS        = '\n  /* ── AI Decision modal ──────────────────────────────────────── */\n  .sg-info { background: none; border: 1px solid #30363d; color: #58a6ff; cursor: pointer;\n             font-size: 12px; padding: 1px 5px; border-radius: 4px; line-height: 1.4; }\n  .sg-info:hover { background: rgba(88,166,255,0.1); }\n  .ai-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.80);\n                display: flex; align-items: flex-start; justify-content: center;\n                z-index: 10000; overflow-y: auto; padding: 40px 16px; }\n  .ai-modal { background: #161b22; border: 1px solid #30363d; border-radius: 12px;\n              width: 100%; max-width: 580px; display: flex; flex-direction: column; gap: 0; }\n  .ai-modal-head { padding: 18px 20px 14px; border-bottom: 1px solid #21262d;\n                   display: flex; align-items: center; gap: 12px; }\n  .ai-modal-ticker { font-size: 20px; font-weight: 700; color: #e6edf3; }\n  .ai-modal-price  { font-size: 13px; color: #8b949e; }\n  .ai-sig-badge { padding: 3px 10px; border-radius: 12px; font-size: 11px; font-weight: 700;\n                  letter-spacing: 0.5px; margin-left: auto; }\n  .ai-sig-bull { background: rgba(63,185,80,0.15); color: #3fb950; border: 1px solid #3fb950; }\n  .ai-sig-bear { background: rgba(248,81,73,0.15);  color: #f85149; border: 1px solid #f85149; }\n  .ai-sig-hold { background: rgba(139,148,158,0.15); color: #8b949e; border: 1px solid #8b949e; }\n  .ai-modal-body { padding: 16px 20px; display: flex; flex-direction: column; gap: 14px; }\n  .ai-narrative { font-size: 13px; color: #c9d1d9; line-height: 1.6;\n                  background: #0d1117; border: 1px solid #21262d; border-radius: 8px;\n                  padding: 12px 14px; }\n  .ai-blocking { font-size: 12px; color: #f85149; background: rgba(248,81,73,0.08);\n                 border: 1px solid rgba(248,81,73,0.25); border-radius: 6px;\n                 padding: 8px 12px; }\n  .ai-gates { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }\n  .ai-gate-col { display: flex; flex-direction: column; gap: 4px; }\n  .ai-gate-title { font-size: 10px; font-weight: 700; letter-spacing: 1px;\n                   text-transform: uppercase; margin-bottom: 2px; }\n  .ai-gate-pass .ai-gate-title { color: #3fb950; }\n  .ai-gate-fail .ai-gate-title { color: #f85149; }\n  .ai-gate-item { font-size: 11px; color: #c9d1d9; padding: 4px 8px;\n                  border-radius: 4px; display: flex; gap: 6px; align-items: flex-start; }\n  .ai-gate-pass .ai-gate-item { background: rgba(63,185,80,0.06); }\n  .ai-gate-fail .ai-gate-item { background: rgba(248,81,73,0.06); }\n  .ai-gate-icon { flex-shrink: 0; margin-top: 1px; }\n  .ai-metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(90px, 1fr)); gap: 8px; }\n  .ai-metric { background: #0d1117; border: 1px solid #21262d; border-radius: 6px;\n               padding: 8px 10px; text-align: center; }\n  .ai-metric-label { font-size: 9px; color: #8b949e; letter-spacing: 0.8px;\n                     text-transform: uppercase; margin-bottom: 4px; }\n  .ai-metric-value { font-size: 14px; font-weight: 700; color: #e6edf3; }\n  .ai-modal-foot { padding: 12px 20px; border-top: 1px solid #21262d;\n                   display: flex; justify-content: space-between; align-items: center; }\n  .ai-gen-time { font-size: 10px; color: #8b949e; }\n  .ai-close-btn { padding: 6px 18px; border-radius: 6px; border: 1px solid #30363d;\n                  background: #21262d; color: #8b949e; cursor: pointer; font-size: 13px; }\n  .ai-close-btn:hover { background: #30363d; }\n'
_AI_MODAL_HTML = '\n  <!-- AI Decision modal -->\n  <div id="ai-overlay" class="ai-overlay" style="display:none" onclick="if(event.target===this) aiClose()">\n    <div class="ai-modal">\n      <div class="ai-modal-head">\n        <div class="ai-modal-ticker" id="ai-ticker"></div>\n        <div class="ai-modal-price" id="ai-price"></div>\n        <span class="ai-sig-badge" id="ai-sig-badge"></span>\n      </div>\n      <div class="ai-modal-body">\n        <div class="ai-narrative" id="ai-narrative"></div>\n        <div class="ai-blocking" id="ai-blocking" style="display:none"></div>\n        <div class="ai-gates" id="ai-gates"></div>\n        <div class="ai-metrics" id="ai-metrics"></div>\n      </div>\n      <div class="ai-modal-foot">\n        <span class="ai-gen-time" id="ai-gen-time"></span>\n        <button class="ai-close-btn" onclick="aiClose()">Close</button>\n      </div>\n    </div>\n  </div>\n'
_AI_JS         = '\n// ── AI Decision info modal ────────────────────────────────────────────────────\n\n// Override sgCardHtml to add the info (ⓘ) button\nfunction sgCardHtml(ticker, stage) {\n  const s   = _sgSigs[ticker] || {};\n  const sig = (s.signal || \'HOLD\').toUpperCase();\n  const sc  = sig === \'BUY\' || sig === \'STRONG_BUY\'   ? \'sig-bull\'\n            : sig === \'SELL\' || sig === \'STRONG_SELL\'  ? \'sig-bear\' : \'\';\n  const price = s.current_price ? \'$\' + s.current_price.toFixed(2) : \'\';\n  return \'<div class="sg-card" draggable="true" data-ticker="\' + ticker + \'" data-stage="\' + stage + \'" \'\n    + \'ondragstart="sgDragStart(event)" ondragend="sgDragEnd(event)">\'\n    + \'<div class="sg-ticker">\' + ticker + \'</div>\'\n    + \'<div class="sg-meta">\' + price + \'</div>\'\n    + \'<span class="sg-signal \' + sc + \'">\' + sig + \'</span>\'\n    + \'<button class="sg-info"  data-ticker="\' + ticker + \'" onclick="sgShowInfo(this.dataset.ticker)" title="AI Decision">&#9432;</button>\'\n    + \'<button class="sg-remove" data-ticker="\' + ticker + \'" onclick="sgRemove(this.dataset.ticker)" title="Remove">&#x2715;</button>\'\n    + \'</div>\';\n}\n\nfunction sgShowInfo(ticker) {\n  const s = _sgSigs[ticker] || {};\n  let reason = {};\n  try { reason = JSON.parse(s.reasoning || \'{}\'); } catch(e) {}\n\n  // Header\n  document.getElementById(\'ai-ticker\').textContent = ticker;\n  const price = s.current_price;\n  document.getElementById(\'ai-price\').textContent = price ? \'$\' + price.toFixed(2) : \'\';\n\n  const sig = (s.signal || \'HOLD\').toUpperCase();\n  const badge = document.getElementById(\'ai-sig-badge\');\n  badge.textContent = sig;\n  badge.className = \'ai-sig-badge \' +\n    (sig === \'BUY\' || sig === \'STRONG_BUY\'   ? \'ai-sig-bull\' :\n     sig === \'SELL\' || sig === \'STRONG_SELL\' ? \'ai-sig-bear\' : \'ai-sig-hold\');\n\n  // Narrative\n  document.getElementById(\'ai-narrative\').textContent =\n    reason.narrative || \'No AI narrative available yet.\';\n\n  // Blocking reason\n  const blockEl = document.getElementById(\'ai-blocking\');\n  if (reason.blocking_reason) {\n    blockEl.textContent = \'\\u26d4 Blocked: \' + reason.blocking_reason;\n    blockEl.style.display = \'block\';\n  } else {\n    blockEl.style.display = \'none\';\n  }\n\n  // Gates\n  const passed = reason.gates_passed || [];\n  const failed = reason.gates_failed || [];\n  let gatesHtml = \'\';\n  if (passed.length) {\n    gatesHtml += \'<div class="ai-gate-col ai-gate-pass">\'\n      + \'<div class="ai-gate-title">&#10003; Gates Passed</div>\'\n      + passed.map(g => \'<div class="ai-gate-item"><span class="ai-gate-icon">&#9679;</span><span>\' + g + \'</span></div>\').join(\'\')\n      + \'</div>\';\n  }\n  if (failed.length) {\n    gatesHtml += \'<div class="ai-gate-col ai-gate-fail">\'\n      + \'<div class="ai-gate-title">&#10007; Gates Failed</div>\'\n      + failed.map(g => \'<div class="ai-gate-item"><span class="ai-gate-icon">&#9679;</span><span>\' + g + \'</span></div>\').join(\'\')\n      + \'</div>\';\n  }\n  document.getElementById(\'ai-gates\').innerHTML = gatesHtml;\n\n  // Metrics\n  function pct(v)  { return v != null ? (v * 100).toFixed(1) + \'%\' : \'—\'; }\n  function f2(v)   { return v != null ? v.toFixed(2) : \'—\'; }\n  const metrics = [\n    { label: \'Signal\',    value: sig },\n    { label: \'Conf\',      value: pct(s.confidence) },\n    { label: \'P(Bull)\',   value: pct(reason.p_bull) },\n    { label: \'Kelly\',     value: f2(reason.kelly) },\n    { label: \'MoS\',       value: pct(s.margin_of_safety) },\n    { label: \'FUD\',       value: f2(s.fud_score) },\n    { label: \'Regime\',    value: (reason.reynolds_regime || \'—\').toUpperCase() },\n    { label: \'Quantum\',   value: (reason.quantum_state  || \'—\').toUpperCase() },\n  ];\n  document.getElementById(\'ai-metrics\').innerHTML = metrics.map(m =>\n    \'<div class="ai-metric"><div class="ai-metric-label">\' + m.label + \'</div>\'\n    + \'<div class="ai-metric-value">\' + m.value + \'</div></div>\'\n  ).join(\'\');\n\n  // Footer timestamp\n  document.getElementById(\'ai-gen-time\').textContent =\n    s.generated_at ? \'Generated: \' + s.generated_at : \'\';\n\n  document.getElementById(\'ai-overlay\').style.display = \'flex\';\n}\n\nfunction aiClose() {\n  document.getElementById(\'ai-overlay\').style.display = \'none\';\n}\n\n// Close on Escape\ndocument.addEventListener(\'keydown\', e => { if (e.key === \'Escape\') aiClose(); });\n'

PAPER_HTML = PAPER_HTML.replace('</style>', _AI_CSS + '</style>', 1).replace('</main>', _AI_MODAL_HTML + '</main>', 1)
PAPER_JS   = PAPER_JS + _AI_JS


# ── 3-Stage UI override (auto-patched) ────────────────────────────────────────
_SG3_JS  = '\n// ════════════════════════════════════════════════════════════════════════════\n// Stage Gate 3-Stage (overrides old 2-stage code)\n// ════════════════════════════════════════════════════════════════════════════\n(function() {\n\n// ── Inject CSS ────────────────────────────────────────────────────────────────\nconst _sg3style = document.createElement(\'style\');\n_sg3style.textContent = \'\\n  /* ── Stage Gate 3-col ─────────────────────────────────────── */\\n  .sg3-wrap  { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 0; }\\n  .sg3-col   { display: flex; flex-direction: column; border-right: 1px solid #21262d; min-width: 0; }\\n  .sg3-col:last-child { border-right: none; }\\n  .sg3-hd    { padding: 10px 14px; background: #0d1117; border-bottom: 1px solid #21262d;\\n               display: flex; align-items: center; gap: 8px; }\\n  .sg3-c1 .sg3-hd  { border-top: 3px solid #8b949e; }\\n  .sg3-c2 .sg3-hd  { border-top: 3px solid #58a6ff; }\\n  .sg3-c3 .sg3-hd  { border-top: 3px solid #3fb950; }\\n  .sg3-title { font-size: 12px; font-weight: 700; }\\n  .sg3-c1 .sg3-title { color: #8b949e; }\\n  .sg3-c2 .sg3-title { color: #58a6ff; }\\n  .sg3-c3 .sg3-title { color: #3fb950; }\\n  .sg3-sub   { font-size: 10px; color: #8b949e; margin-top: 2px; }\\n  .sg3-cnt   { margin-left: auto; font-size: 11px; color: #8b949e;\\n               background: #21262d; padding: 2px 7px; border-radius: 10px; }\\n  .sg3-zone  { flex: 1; min-height: 80px; padding: 8px;\\n               display: flex; flex-direction: column; gap: 6px; }\\n  .sg3-zone.drag-over { background: rgba(88,166,255,0.05);\\n                        outline: 2px dashed #58a6ff; outline-offset: -3px; border-radius: 4px; }\\n  .sg3-card  { background: #0d1117; border: 1px solid #21262d; border-radius: 6px;\\n               padding: 7px 10px; cursor: grab; display: flex; align-items: center;\\n               gap: 6px; user-select: none; transition: border-color 0.15s; flex-wrap: wrap; }\\n  .sg3-card:hover   { border-color: #58a6ff; }\\n  .sg3-card:active  { cursor: grabbing; }\\n  .sg3-card.dragging { opacity: 0.4; }\\n  .sg3-c2 .sg3-card { border-left: 3px solid #58a6ff; }\\n  .sg3-c3 .sg3-card { border-left: 3px solid #3fb950; }\\n  .sg3-tick  { font-size: 13px; font-weight: 700; min-width: 52px; }\\n  .sg3-meta  { font-size: 10px; color: #8b949e; flex: 1; min-width: 50px; }\\n  .sg3-pnl   { font-size: 10px; font-weight: 600; }\\n  .sg3-sig   { font-size: 10px; font-weight: 600; }\\n  .sg3-acts  { display: flex; gap: 3px; margin-left: auto; }\\n  .sg3-btn   { background: none; border: 1px solid #30363d; color: #8b949e;\\n               cursor: pointer; font-size: 11px; padding: 2px 6px;\\n               border-radius: 4px; line-height: 1.4; white-space: nowrap; }\\n  .sg3-btn-buy  { border-color: #3fb950; color: #3fb950; }\\n  .sg3-btn-buy:hover  { background: rgba(63,185,80,0.12); }\\n  .sg3-btn-sell { border-color: #f85149; color: #f85149; }\\n  .sg3-btn-sell:hover { background: rgba(248,81,73,0.12); }\\n  .sg3-btn-ai   { border-color: #58a6ff; color: #58a6ff; }\\n  .sg3-btn-ai:hover   { background: rgba(88,166,255,0.12); }\\n  .sg3-btn-info { border-color: #30363d; color: #58a6ff; }\\n  .sg3-btn-info:hover { background: rgba(88,166,255,0.08); }\\n  .sg3-btn-rm   { border-color: transparent; color: #8b949e; }\\n  .sg3-btn-rm:hover   { color: #f85149; background: rgba(248,81,73,0.08); }\\n  .sg3-hint  { color: #8b949e; font-size: 11px; text-align: center; padding: 18px 8px;\\n               border: 2px dashed #21262d; border-radius: 6px; font-style: italic; }\\n  .sg3-status { font-size: 9px; font-weight: 700; letter-spacing: 0.5px;\\n                padding: 1px 5px; border-radius: 8px; }\\n  .sg3-status-bought { background: rgba(63,185,80,0.2); color: #3fb950; }\\n  .sg3-status-sold   { background: rgba(248,81,73,0.2);  color: #f85149; }\\n  /* Sell modal */\\n  .sg3-sell-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.75);\\n                      display: flex; align-items: center; justify-content: center; z-index: 10001; }\\n  .sg3-sell-box { background: #161b22; border: 1px solid #30363d; border-radius: 10px;\\n                  padding: 24px; width: 340px; display: flex; flex-direction: column; gap: 14px; }\\n  .sg3-sell-box h3 { font-size: 15px; color: #e6edf3; }\\n  .sg3-sell-pos  { font-size: 12px; color: #8b949e; }\\n  .sg3-sell-dest { font-size: 11px; color: #8b949e; }\\n  @media (max-width: 700px) {\\n    .sg3-wrap { grid-template-columns: 1fr; }\\n    .sg3-col  { border-right: none; border-bottom: 1px solid #21262d; }\\n  }\\n\';\ndocument.head.appendChild(_sg3style);\n\n// ── Replace old Stage Gate section with 3-col ─────────────────────────────────\n(function injectHtml() {\n  // Find old section (has sg-stages or sg3-section)\n  const old = document.getElementById(\'sg3-section\')\n           || Array.from(document.querySelectorAll(\'.section\'))\n                .find(s => s.querySelector(\'.section-title\')\n                        && s.querySelector(\'.section-title\').textContent.includes(\'Stage Gate\'));\n  if (!old) {\n    // Not rendered yet — insert before first .section\n    const main = document.querySelector(\'main\');\n    if (main) {\n      const firstSec = main.querySelector(\'.section\');\n      if (firstSec) {\n        firstSec.insertAdjacentHTML(\'beforebegin\', \'\\n<div id="sg3-section" class="section">\\n  <div class="section-title">&#127760; Stage Gate &mdash; Stock Pipeline</div>\\n  <div class="sg3-wrap">\\n    <div class="sg3-col sg3-c1">\\n      <div class="sg3-hd">\\n        <div><div class="sg3-title">&#128203; Stage 1 &mdash; Monitoring</div>\\n             <div class="sg3-sub">Watching only &middot; no trading</div></div>\\n        <span class="sg3-cnt" id="sg3-cnt-1">0</span>\\n      </div>\\n      <div class="sg3-zone" id="sg3-zone-1"\\n           ondragover="sg3Over(event,\\\'1\\\')" ondragleave="sg3Leave(\\\'1\\\')" ondrop="sg3Drop(event,\\\'1\\\')">\\n        <div class="sg3-hint">Stocks you are watching</div>\\n      </div>\\n    </div>\\n    <div class="sg3-col sg3-c2">\\n      <div class="sg3-hd">\\n        <div><div class="sg3-title">&#129302; Stage 2 &mdash; Active AI</div>\\n             <div class="sg3-sub">AI pipeline &middot; auto-buys on signal</div></div>\\n        <span class="sg3-cnt" id="sg3-cnt-2">0</span>\\n      </div>\\n      <div class="sg3-zone" id="sg3-zone-2"\\n           ondragover="sg3Over(event,\\\'2\\\')" ondragleave="sg3Leave(\\\'2\\\')" ondrop="sg3Drop(event,\\\'2\\\')">\\n        <div class="sg3-hint">Drag here to activate AI trading</div>\\n      </div>\\n    </div>\\n    <div class="sg3-col sg3-c3">\\n      <div class="sg3-hd">\\n        <div><div class="sg3-title">&#128200; Stage 3 &mdash; Open Positions</div>\\n             <div class="sg3-sub">Live positions &middot; drag left to sell</div></div>\\n        <span class="sg3-cnt" id="sg3-cnt-3">0</span>\\n      </div>\\n      <div class="sg3-zone" id="sg3-zone-3"\\n           ondragover="sg3Over(event,\\\'3\\\')" ondragleave="sg3Leave(\\\'3\\\')" ondrop="sg3Drop(event,\\\'3\\\')">\\n        <div class="sg3-hint">Positions appear here after a buy</div>\\n      </div>\\n    </div>\\n  </div>\\n</div>\\n<!-- Sell modal -->\\n<div id="sg3-sell-overlay" class="sg3-sell-overlay" style="display:none"\\n     onclick="if(event.target===this) sg3SellCancel()">\\n  <div class="sg3-sell-box">\\n    <h3 id="sg3-sell-title">Sell</h3>\\n    <p class="sg3-sell-pos" id="sg3-sell-pos"></p>\\n    <div class="sg-toggle">\\n      <label><input type="radio" name="sg3sm" id="sg3sm-all" value="all" checked\\n                    onchange="sg3SellModeChange()"> Sell all</label>\\n      <label><input type="radio" name="sg3sm" id="sg3sm-part" value="partial"\\n                    onchange="sg3SellModeChange()"> Partial</label>\\n    </div>\\n    <input class="sg-amount-input" id="sg3-sell-qty" type="number" min="1" step="1"\\n           placeholder="Shares to sell..." style="display:none"\\n           oninput="sg3UpdateSellHint()" onkeydown="if(event.key===\\\'Enter\\\') sg3SellConfirm()">\\n    <p class="sg-modal-hint" id="sg3-sell-hint">&nbsp;</p>\\n    <p class="sg3-sell-dest" id="sg3-sell-dest"></p>\\n    <div class="sg-modal-btns">\\n      <button class="sg-cancel-btn" onclick="sg3SellCancel()">Cancel</button>\\n      <button class="sg-act-btn" id="sg3-sell-btn"\\n              style="border-color:#f85149;background:rgba(248,81,73,0.15);color:#f85149"\\n              onclick="sg3SellConfirm()">&#x1f4b8; Sell</button>\\n    </div>\\n  </div>\\n</div>\\n\');\n      } else {\n        main.insertAdjacentHTML(\'beforeend\', \'\\n<div id="sg3-section" class="section">\\n  <div class="section-title">&#127760; Stage Gate &mdash; Stock Pipeline</div>\\n  <div class="sg3-wrap">\\n    <div class="sg3-col sg3-c1">\\n      <div class="sg3-hd">\\n        <div><div class="sg3-title">&#128203; Stage 1 &mdash; Monitoring</div>\\n             <div class="sg3-sub">Watching only &middot; no trading</div></div>\\n        <span class="sg3-cnt" id="sg3-cnt-1">0</span>\\n      </div>\\n      <div class="sg3-zone" id="sg3-zone-1"\\n           ondragover="sg3Over(event,\\\'1\\\')" ondragleave="sg3Leave(\\\'1\\\')" ondrop="sg3Drop(event,\\\'1\\\')">\\n        <div class="sg3-hint">Stocks you are watching</div>\\n      </div>\\n    </div>\\n    <div class="sg3-col sg3-c2">\\n      <div class="sg3-hd">\\n        <div><div class="sg3-title">&#129302; Stage 2 &mdash; Active AI</div>\\n             <div class="sg3-sub">AI pipeline &middot; auto-buys on signal</div></div>\\n        <span class="sg3-cnt" id="sg3-cnt-2">0</span>\\n      </div>\\n      <div class="sg3-zone" id="sg3-zone-2"\\n           ondragover="sg3Over(event,\\\'2\\\')" ondragleave="sg3Leave(\\\'2\\\')" ondrop="sg3Drop(event,\\\'2\\\')">\\n        <div class="sg3-hint">Drag here to activate AI trading</div>\\n      </div>\\n    </div>\\n    <div class="sg3-col sg3-c3">\\n      <div class="sg3-hd">\\n        <div><div class="sg3-title">&#128200; Stage 3 &mdash; Open Positions</div>\\n             <div class="sg3-sub">Live positions &middot; drag left to sell</div></div>\\n        <span class="sg3-cnt" id="sg3-cnt-3">0</span>\\n      </div>\\n      <div class="sg3-zone" id="sg3-zone-3"\\n           ondragover="sg3Over(event,\\\'3\\\')" ondragleave="sg3Leave(\\\'3\\\')" ondrop="sg3Drop(event,\\\'3\\\')">\\n        <div class="sg3-hint">Positions appear here after a buy</div>\\n      </div>\\n    </div>\\n  </div>\\n</div>\\n<!-- Sell modal -->\\n<div id="sg3-sell-overlay" class="sg3-sell-overlay" style="display:none"\\n     onclick="if(event.target===this) sg3SellCancel()">\\n  <div class="sg3-sell-box">\\n    <h3 id="sg3-sell-title">Sell</h3>\\n    <p class="sg3-sell-pos" id="sg3-sell-pos"></p>\\n    <div class="sg-toggle">\\n      <label><input type="radio" name="sg3sm" id="sg3sm-all" value="all" checked\\n                    onchange="sg3SellModeChange()"> Sell all</label>\\n      <label><input type="radio" name="sg3sm" id="sg3sm-part" value="partial"\\n                    onchange="sg3SellModeChange()"> Partial</label>\\n    </div>\\n    <input class="sg-amount-input" id="sg3-sell-qty" type="number" min="1" step="1"\\n           placeholder="Shares to sell..." style="display:none"\\n           oninput="sg3UpdateSellHint()" onkeydown="if(event.key===\\\'Enter\\\') sg3SellConfirm()">\\n    <p class="sg-modal-hint" id="sg3-sell-hint">&nbsp;</p>\\n    <p class="sg3-sell-dest" id="sg3-sell-dest"></p>\\n    <div class="sg-modal-btns">\\n      <button class="sg-cancel-btn" onclick="sg3SellCancel()">Cancel</button>\\n      <button class="sg-act-btn" id="sg3-sell-btn"\\n              style="border-color:#f85149;background:rgba(248,81,73,0.15);color:#f85149"\\n              onclick="sg3SellConfirm()">&#x1f4b8; Sell</button>\\n    </div>\\n  </div>\\n</div>\\n\');\n      }\n    }\n  } else if (!document.getElementById(\'sg3-section\')) {\n    old.outerHTML = \'\\n<div id="sg3-section" class="section">\\n  <div class="section-title">&#127760; Stage Gate &mdash; Stock Pipeline</div>\\n  <div class="sg3-wrap">\\n    <div class="sg3-col sg3-c1">\\n      <div class="sg3-hd">\\n        <div><div class="sg3-title">&#128203; Stage 1 &mdash; Monitoring</div>\\n             <div class="sg3-sub">Watching only &middot; no trading</div></div>\\n        <span class="sg3-cnt" id="sg3-cnt-1">0</span>\\n      </div>\\n      <div class="sg3-zone" id="sg3-zone-1"\\n           ondragover="sg3Over(event,\\\'1\\\')" ondragleave="sg3Leave(\\\'1\\\')" ondrop="sg3Drop(event,\\\'1\\\')">\\n        <div class="sg3-hint">Stocks you are watching</div>\\n      </div>\\n    </div>\\n    <div class="sg3-col sg3-c2">\\n      <div class="sg3-hd">\\n        <div><div class="sg3-title">&#129302; Stage 2 &mdash; Active AI</div>\\n             <div class="sg3-sub">AI pipeline &middot; auto-buys on signal</div></div>\\n        <span class="sg3-cnt" id="sg3-cnt-2">0</span>\\n      </div>\\n      <div class="sg3-zone" id="sg3-zone-2"\\n           ondragover="sg3Over(event,\\\'2\\\')" ondragleave="sg3Leave(\\\'2\\\')" ondrop="sg3Drop(event,\\\'2\\\')">\\n        <div class="sg3-hint">Drag here to activate AI trading</div>\\n      </div>\\n    </div>\\n    <div class="sg3-col sg3-c3">\\n      <div class="sg3-hd">\\n        <div><div class="sg3-title">&#128200; Stage 3 &mdash; Open Positions</div>\\n             <div class="sg3-sub">Live positions &middot; drag left to sell</div></div>\\n        <span class="sg3-cnt" id="sg3-cnt-3">0</span>\\n      </div>\\n      <div class="sg3-zone" id="sg3-zone-3"\\n           ondragover="sg3Over(event,\\\'3\\\')" ondragleave="sg3Leave(\\\'3\\\')" ondrop="sg3Drop(event,\\\'3\\\')">\\n        <div class="sg3-hint">Positions appear here after a buy</div>\\n      </div>\\n    </div>\\n  </div>\\n</div>\\n<!-- Sell modal -->\\n<div id="sg3-sell-overlay" class="sg3-sell-overlay" style="display:none"\\n     onclick="if(event.target===this) sg3SellCancel()">\\n  <div class="sg3-sell-box">\\n    <h3 id="sg3-sell-title">Sell</h3>\\n    <p class="sg3-sell-pos" id="sg3-sell-pos"></p>\\n    <div class="sg-toggle">\\n      <label><input type="radio" name="sg3sm" id="sg3sm-all" value="all" checked\\n                    onchange="sg3SellModeChange()"> Sell all</label>\\n      <label><input type="radio" name="sg3sm" id="sg3sm-part" value="partial"\\n                    onchange="sg3SellModeChange()"> Partial</label>\\n    </div>\\n    <input class="sg-amount-input" id="sg3-sell-qty" type="number" min="1" step="1"\\n           placeholder="Shares to sell..." style="display:none"\\n           oninput="sg3UpdateSellHint()" onkeydown="if(event.key===\\\'Enter\\\') sg3SellConfirm()">\\n    <p class="sg-modal-hint" id="sg3-sell-hint">&nbsp;</p>\\n    <p class="sg3-sell-dest" id="sg3-sell-dest"></p>\\n    <div class="sg-modal-btns">\\n      <button class="sg-cancel-btn" onclick="sg3SellCancel()">Cancel</button>\\n      <button class="sg-act-btn" id="sg3-sell-btn"\\n              style="border-color:#f85149;background:rgba(248,81,73,0.15);color:#f85149"\\n              onclick="sg3SellConfirm()">&#x1f4b8; Sell</button>\\n    </div>\\n  </div>\\n</div>\\n\';\n  }\n  // Inject sell modal if not present\n  if (!document.getElementById(\'sg3-sell-overlay\')) {\n    document.body.insertAdjacentHTML(\'beforeend\', \'\\n<div id="sg3-sell-overlay" class="sg3-sell-overlay" style="display:none"\\n     onclick="if(event.target===this) sg3SellCancel()">\\n  <div class="sg3-sell-box">\\n    <h3 id="sg3-sell-title">Sell</h3>\\n    <p class="sg3-sell-pos" id="sg3-sell-pos"></p>\\n    <div class="sg-toggle">\\n      <label><input type="radio" name="sg3sm" id="sg3sm-all" value="all" checked\\n                    onchange="sg3SellModeChange()"> Sell all</label>\\n      <label><input type="radio" name="sg3sm" id="sg3sm-part" value="partial"\\n                    onchange="sg3SellModeChange()"> Partial</label>\\n    </div>\\n    <input class="sg-amount-input" id="sg3-sell-qty" type="number" min="1" step="1"\\n           placeholder="Shares to sell..." style="display:none"\\n           oninput="sg3UpdateSellHint()" onkeydown="if(event.key===\\\'Enter\\\') sg3SellConfirm()">\\n    <p class="sg-modal-hint" id="sg3-sell-hint">&nbsp;</p>\\n    <p class="sg3-sell-dest" id="sg3-sell-dest"></p>\\n    <div class="sg-modal-btns">\\n      <button class="sg-cancel-btn" onclick="sg3SellCancel()">Cancel</button>\\n      <button class="sg-act-btn" id="sg3-sell-btn"\\n              style="border-color:#f85149;background:rgba(248,81,73,0.15);color:#f85149"\\n              onclick="sg3SellConfirm()">&#x1f4b8; Sell</button>\\n    </div>\\n  </div>\\n</div>\\n\');\n  }\n})();\n\n// ── State ─────────────────────────────────────────────────────────────────────\nlet _sg3 = { stage1: [], stage2: [], stage3: [] };\nlet _sg3sigs = {};\nlet _sg3pos  = {};   // ticker -> position data {qty, avg_cost, cur_price, pnl, pnl_pct}\nlet _sg3dragT = null;\nlet _sg3dragF = null;\nlet _sg3pendTicker  = null;  // pending for buy modal\nlet _sg3pendTarget  = null;  // target stage for buy\nlet _sg3sellTicker  = null;  // pending for sell modal\nlet _sg3sellTarget  = null;  // target stage after sell\nlet _sg3recentTrades = {};   // ticker -> \'BOUGHT\'|\'SOLD\' (shown briefly)\n\n// ── Boot ──────────────────────────────────────────────────────────────────────\nasync function sg3Boot() {\n  try {\n    const sigs = await fetch(\'/api/signals\').then(r => r.json());\n    (sigs || []).forEach(s => { _sg3sigs[s.ticker] = s; });\n  } catch(e) {}\n  try { _sg3 = await fetch(\'/api/stagegate\').then(r => r.json()); } catch(e) {}\n  _sg3.stage1 = _sg3.stage1 || [];\n  _sg3.stage2 = _sg3.stage2 || [];\n  _sg3.stage3 = _sg3.stage3 || [];\n  sg3Render();\n}\n\n// Override old sgBoot to be a no-op (sg3Boot takes over)\nwindow.sgBoot = function() {};\n\n// ── Sync positions from account data ─────────────────────────────────────────\nfunction sg3SyncPositions(positions) {\n  _sg3pos = {};\n  (positions || []).forEach(p => { _sg3pos[p.ticker] = p; });\n\n  // Auto-promote: any open position not in stage3 → move to stage3\n  let changed = false;\n  Object.keys(_sg3pos).forEach(ticker => {\n    if (!_sg3.stage3.includes(ticker)) {\n      for (const k of [\'stage1\', \'stage2\']) {\n        const i = _sg3[k].indexOf(ticker);\n        if (i !== -1) { _sg3[k].splice(i, 1); }\n      }\n      _sg3.stage3.push(ticker);\n      changed = true;\n    }\n  });\n  // Auto-demote: stage3 ticker with no position → back to stage2\n  _sg3.stage3 = _sg3.stage3.filter(ticker => {\n    if (!_sg3pos[ticker]) {\n      if (!_sg3.stage2.includes(ticker) && !_sg3.stage1.includes(ticker)) {\n        _sg3.stage2.push(ticker);\n      }\n      changed = true;\n      return false;\n    }\n    return true;\n  });\n  if (changed) sg3Save();\n  sg3Render();\n}\n\n// ── Render ────────────────────────────────────────────────────────────────────\nfunction sg3Render() {\n  sg3RenderZone(\'1\', _sg3.stage1);\n  sg3RenderZone(\'2\', _sg3.stage2);\n  sg3RenderZone(\'3\', _sg3.stage3);\n  document.getElementById(\'sg3-cnt-1\').textContent = _sg3.stage1.length;\n  document.getElementById(\'sg3-cnt-2\').textContent = _sg3.stage2.length;\n  document.getElementById(\'sg3-cnt-3\').textContent = _sg3.stage3.length;\n}\n\nfunction sg3RenderZone(stage, tickers) {\n  const zone = document.getElementById(\'sg3-zone-\' + stage);\n  if (!zone) return;\n  if (!tickers.length) {\n    const hints = {\n      \'1\': \'Stocks you are watching\',\n      \'2\': \'Drag here to activate AI trading\',\n      \'3\': \'Positions appear here after a buy\',\n    };\n    zone.innerHTML = \'<div class="sg3-hint">\' + hints[stage] + \'</div>\';\n    return;\n  }\n  zone.innerHTML = tickers.map(t => sg3CardHtml(t, stage)).join(\'\');\n}\n\nfunction sg3CardHtml(ticker, stage) {\n  const s    = _sg3sigs[ticker] || {};\n  const pos  = _sg3pos[ticker]  || {};\n  const sig  = (s.signal || \'HOLD\').toUpperCase();\n  const sc   = sig === \'BUY\' || sig === \'STRONG_BUY\'  ? \'sig-bull\'\n             : sig === \'SELL\'|| sig === \'STRONG_SELL\' ? \'sig-bear\' : \'\';\n  const price = s.current_price ? \'$\' + s.current_price.toFixed(2) : \'\';\n  const recent = _sg3recentTrades[ticker];\n\n  let meta = price;\n  let pnlHtml = \'\';\n  if (stage === \'3\' && pos.qty) {\n    const pnlCls = (pos.pnl || 0) >= 0 ? \'up\' : \'dn\';\n    const pnlStr = ((pos.pnl || 0) >= 0 ? \'+\' : \'\') + \'$\' + Math.abs(pos.pnl || 0).toFixed(0);\n    const pctStr = ((pos.pnl_pct || 0) >= 0 ? \'+\' : \'\') + (pos.pnl_pct || 0).toFixed(1) + \'%\';\n    meta = price + (price ? \' · \' : \'\') + pos.qty + \' sh @ $\' + (pos.avg_cost || 0).toFixed(2);\n    pnlHtml = \'<span class="sg3-pnl \' + pnlCls + \'">\' + pnlStr + \' (\' + pctStr + \')</span>\';\n  }\n\n  let statusHtml = \'\';\n  if (recent) {\n    statusHtml = \'<span class="sg3-status sg3-status-\' + recent.toLowerCase() + \'">\' + recent + \'</span>\';\n  }\n\n  // Action buttons differ per stage\n  let btns = \'<div class="sg3-acts">\';\n  btns += \'<button class="sg3-btn sg3-btn-info" data-ticker="\' + ticker + \'" onclick="sgShowInfo(this.dataset.ticker)" title="AI Analysis">&#9432;</button>\';\n  if (stage === \'1\') {\n    btns += \'<button class="sg3-btn sg3-btn-ai"  data-ticker="\' + ticker + \'" onclick="sg3ActivateAI(this.dataset.ticker)"  title="Activate AI">AI</button>\';\n    btns += \'<button class="sg3-btn sg3-btn-buy" data-ticker="\' + ticker + \'" onclick="sg3OpenBuy(this.dataset.ticker,\\\'3\\\')" title="Buy now">Buy</button>\';\n  } else if (stage === \'2\') {\n    btns += \'<button class="sg3-btn sg3-btn-buy" data-ticker="\' + ticker + \'" onclick="sg3OpenBuy(this.dataset.ticker,\\\'3\\\')" title="Buy now">Buy</button>\';\n  } else if (stage === \'3\') {\n    btns += \'<button class="sg3-btn sg3-btn-buy"  data-ticker="\' + ticker + \'" onclick="sg3OpenBuy(this.dataset.ticker,\\\'3\\\')"  title="Add to position">Buy+</button>\';\n    btns += \'<button class="sg3-btn sg3-btn-sell" data-ticker="\' + ticker + \'" onclick="sg3OpenSell(this.dataset.ticker,\\\'2\\\')" title="Sell">Sell</button>\';\n  }\n  btns += \'<button class="sg3-btn sg3-btn-rm" data-ticker="\' + ticker + \'" onclick="sg3Remove(this.dataset.ticker)" title="Remove">&#x2715;</button>\';\n  btns += \'</div>\';\n\n  return \'<div class="sg3-card" draggable="true" data-ticker="\' + ticker + \'" data-stage="\' + stage + \'" \'\n    + \'ondragstart="sg3DragStart(event)" ondragend="sg3DragEnd(event)">\'\n    + \'<div class="sg3-tick">\' + ticker + \'</div>\'\n    + \'<div class="sg3-meta">\' + meta + \'</div>\'\n    + pnlHtml\n    + statusHtml\n    + \'<span class="sg3-sig \' + sc + \'">\' + sig + \'</span>\'\n    + btns\n    + \'</div>\';\n}\n\n// ── Drag & drop ───────────────────────────────────────────────────────────────\nfunction sg3DragStart(e) {\n  _sg3dragT = e.currentTarget.dataset.ticker;\n  _sg3dragF = e.currentTarget.dataset.stage;\n  e.currentTarget.classList.add(\'dragging\');\n  e.dataTransfer.effectAllowed = \'move\';\n}\nfunction sg3DragEnd(e) { e.currentTarget.classList.remove(\'dragging\'); }\nfunction sg3Over(e, stage) {\n  e.preventDefault();\n  e.dataTransfer.dropEffect = \'move\';\n  const z = document.getElementById(\'sg3-zone-\' + stage);\n  if (z) z.classList.add(\'drag-over\');\n}\nfunction sg3Leave(stage) {\n  const z = document.getElementById(\'sg3-zone-\' + stage);\n  if (z) z.classList.remove(\'drag-over\');\n}\n\nfunction sg3Drop(e, toStage) {\n  e.preventDefault();\n  const z = document.getElementById(\'sg3-zone-\' + toStage);\n  if (z) z.classList.remove(\'drag-over\');\n  if (!_sg3dragT || _sg3dragF === toStage) return;\n  const from = _sg3dragF, ticker = _sg3dragT;\n\n  // Moving to Stage 1 from Stage 2 or 3 → sell popup (if has position)\n  if (toStage === \'1\' && (from === \'2\' || from === \'3\')) {\n    if (_sg3pos[ticker]) {\n      sg3OpenSell(ticker, \'1\');\n    } else {\n      sg3MoveLocal(ticker, from, \'1\');\n    }\n    return;\n  }\n  // Moving Stage 3 → Stage 2 → sell popup\n  if (toStage === \'2\' && from === \'3\') {\n    sg3OpenSell(ticker, \'2\');\n    return;\n  }\n  // Stage 1 → Stage 2: activate for AI (no buy)\n  if (toStage === \'2\' && from === \'1\') {\n    sg3ActivateAI(ticker);\n    return;\n  }\n  // Stage 1/2 → Stage 3: buy popup\n  if (toStage === \'3\') {\n    sg3OpenBuy(ticker, \'3\', from);\n    return;\n  }\n  sg3MoveLocal(ticker, from, toStage);\n}\n\n// ── AI activation (Stage 1 → Stage 2, no immediate buy) ──────────────────────\nasync function sg3ActivateAI(ticker) {\n  try {\n    await fetch(\'/api/paper/activate-ai\', {\n      method: \'POST\',\n      headers: {\'Content-Type\': \'application/json\'},\n      body: JSON.stringify({ ticker }),\n    });\n  } catch(e) {}\n  sg3MoveLocal(ticker, \'1\', \'2\');\n}\n\n// ── Buy modal ─────────────────────────────────────────────────────────────────\nfunction sg3OpenBuy(ticker, targetStage, fromStage) {\n  _sg3pendTicker = ticker;\n  _sg3pendTarget = targetStage || \'3\';\n  _sg3pendFrom   = fromStage || _sg3dragF || null;\n  // Reuse existing buy modal (sg-overlay) from the previous embed\n  const s = _sg3sigs[ticker] || {};\n  document.getElementById(\'sg-modal-title\').textContent = \'Buy \' + ticker;\n  document.getElementById(\'sg-modal-price\').textContent =\n    s.current_price ? \'Current price: $\' + s.current_price.toFixed(2) : \'Price not available\';\n  document.getElementById(\'sg-mode-shares\').checked = true;\n  document.getElementById(\'sg-amount\').value = \'\';\n  document.getElementById(\'sg-modal-hint\').innerHTML = \'&nbsp;\';\n  document.getElementById(\'sg-overlay\').style.display = \'flex\';\n  setTimeout(() => document.getElementById(\'sg-amount\').focus(), 60);\n  // Swap confirm handler\n  document.getElementById(\'sg-act-btn\').onclick = sg3BuyConfirm;\n  document.getElementById(\'sg-act-btn\').textContent = \'\\u25b6 Buy\';\n}\n\nfunction sgUpdateHint() {  // keep existing hint updater working\n  const mode  = document.querySelector(\'input[name="sg-mode"]:checked\').value;\n  const amt   = parseFloat(document.getElementById(\'sg-amount\').value);\n  const price = (_sg3sigs[_sg3pendTicker] || {}).current_price;\n  const hint  = document.getElementById(\'sg-modal-hint\');\n  if (!amt || amt <= 0) { hint.innerHTML = \'&nbsp;\'; return; }\n  if (mode === \'shares\') {\n    hint.textContent = price\n      ? \'Total \\u2248 $\' + (amt * price).toLocaleString(\'en-US\', {minimumFractionDigits:2, maximumFractionDigits:2})\n      : amt + \' shares\';\n  } else {\n    const sh = price ? Math.floor(amt / price) : null;\n    hint.textContent = sh != null ? sh + \' shares @ $\' + price.toFixed(2) : \'$\' + amt;\n  }\n}\n\nasync function sg3BuyConfirm() {\n  const ticker = _sg3pendTicker;\n  const mode   = document.querySelector(\'input[name="sg-mode"]:checked\').value;\n  const amount = parseFloat(document.getElementById(\'sg-amount\').value);\n  if (!ticker || !amount || amount <= 0) return;\n\n  const btn = document.getElementById(\'sg-act-btn\');\n  btn.disabled = true; btn.textContent = \'Buying\\u2026\';\n\n  try {\n    const r = await fetch(\'/api/paper/activate\', {\n      method: \'POST\',\n      headers: {\'Content-Type\': \'application/json\'},\n      body: JSON.stringify({ ticker, mode, amount }),\n    }).then(res => res.json());\n\n    if (r.status === \'ok\') {\n      if (_sg3pendFrom) sg3RemoveFromStage(_sg3pendTicker, _sg3pendFrom);\n      sg3MoveLocal(ticker, null, \'3\');\n      document.getElementById(\'sg-overlay\').style.display = \'none\';\n      _sg3recentTrades[ticker] = \'BOUGHT\';\n      setTimeout(() => { delete _sg3recentTrades[ticker]; sg3Render(); }, 8000);\n      load();\n    } else {\n      alert(\'Buy failed: \' + (r.error || \'unknown\'));\n    }\n  } catch(e) { alert(\'Error: \' + e.message); }\n  finally {\n    btn.disabled = false; btn.textContent = \'\\u25b6 Start Trading\';\n    btn.onclick  = sgModalConfirm;  // restore original handler\n  }\n}\n\n// ── Sell modal ────────────────────────────────────────────────────────────────\nfunction sg3OpenSell(ticker, targetStage) {\n  _sg3sellTicker = ticker;\n  _sg3sellTarget = targetStage || \'1\';\n  const pos = _sg3pos[ticker] || {};\n  const price = (_sg3sigs[ticker] || {}).current_price || pos.cur_price || pos.avg_cost || 0;\n\n  document.getElementById(\'sg3-sell-title\').textContent = \'Sell \' + ticker;\n  document.getElementById(\'sg3-sell-pos\').textContent =\n    pos.qty\n      ? pos.qty + \' shares · avg cost $\' + (pos.avg_cost || 0).toFixed(2) + \' · current $\' + price.toFixed(2)\n      : \'No open position\';\n  document.getElementById(\'sg3sm-all\').checked = true;\n  document.getElementById(\'sg3-sell-qty\').style.display = \'none\';\n  document.getElementById(\'sg3-sell-qty\').value = \'\';\n  const dest = targetStage === \'1\' ? \'Stage 1 (Monitoring)\' : \'Stage 2 (Active AI)\';\n  document.getElementById(\'sg3-sell-dest\').textContent = \'After sell: move to \' + dest;\n  sg3UpdateSellHint();\n  document.getElementById(\'sg3-sell-overlay\').style.display = \'flex\';\n  if (pos.qty) setTimeout(() => document.getElementById(\'sg3-sell-overlay\').focus?.(), 60);\n}\n\nfunction sg3SellModeChange() {\n  const partial = document.getElementById(\'sg3sm-part\').checked;\n  document.getElementById(\'sg3-sell-qty\').style.display = partial ? \'block\' : \'none\';\n  if (partial) document.getElementById(\'sg3-sell-qty\').focus();\n  sg3UpdateSellHint();\n}\n\nfunction sg3UpdateSellHint() {\n  const pos   = _sg3pos[_sg3sellTicker] || {};\n  const price = (_sg3sigs[_sg3sellTicker] || {}).current_price || pos.cur_price || 0;\n  const hint  = document.getElementById(\'sg3-sell-hint\');\n  const mode  = document.querySelector(\'input[name="sg3sm"]:checked\')?.value || \'all\';\n  const qty   = mode === \'all\' ? (pos.qty || 0) : parseFloat(document.getElementById(\'sg3-sell-qty\').value) || 0;\n  if (!qty || !price) { hint.innerHTML = \'&nbsp;\'; return; }\n  hint.textContent = \'Proceeds \\u2248 $\' + (qty * price).toLocaleString(\'en-US\', {minimumFractionDigits:2, maximumFractionDigits:2});\n}\n\nfunction sg3SellCancel() {\n  document.getElementById(\'sg3-sell-overlay\').style.display = \'none\';\n  _sg3sellTicker = null;\n}\n\nasync function sg3SellConfirm() {\n  const ticker      = _sg3sellTicker;\n  const targetStage = _sg3sellTarget;\n  if (!ticker) return;\n\n  const mode = document.querySelector(\'input[name="sg3sm"]:checked\')?.value || \'all\';\n  const qty  = mode === \'partial\' ? parseFloat(document.getElementById(\'sg3-sell-qty\').value) : null;\n\n  const btn = document.getElementById(\'sg3-sell-btn\');\n  btn.disabled = true; btn.textContent = \'Selling\\u2026\';\n\n  try {\n    const r = await fetch(\'/api/paper/sell\', {\n      method: \'POST\',\n      headers: {\'Content-Type\': \'application/json\'},\n      body: JSON.stringify({ ticker, mode, qty, target_stage: targetStage }),\n    }).then(res => res.json());\n\n    if (r.status === \'ok\') {\n      sg3MoveLocal(ticker, \'3\', targetStage);\n      document.getElementById(\'sg3-sell-overlay\').style.display = \'none\';\n      _sg3sellTicker = null;\n      if (!r.no_position) {\n        _sg3recentTrades[ticker] = \'SOLD\';\n        setTimeout(() => { delete _sg3recentTrades[ticker]; sg3Render(); }, 8000);\n      }\n      load();\n    } else {\n      alert(\'Sell failed: \' + (r.error || \'unknown\'));\n    }\n  } catch(e) { alert(\'Error: \' + e.message); }\n  finally { btn.disabled = false; btn.textContent = \'\\u1f4b8 Sell\'; }\n}\n\n// ── Local state helpers ───────────────────────────────────────────────────────\nfunction sg3RemoveFromStage(ticker, stage) {\n  const arr = _sg3[\'stage\' + stage];\n  if (!arr) return;\n  const i = arr.indexOf(ticker);\n  if (i !== -1) arr.splice(i, 1);\n}\n\nfunction sg3MoveLocal(ticker, from, to) {\n  if (from) sg3RemoveFromStage(ticker, from);\n  const toArr = _sg3[\'stage\' + to];\n  if (toArr && !toArr.includes(ticker)) toArr.push(ticker);\n  sg3Render();\n  sg3Save();\n}\n\nfunction sg3Remove(ticker) {\n  [\'stage1\',\'stage2\',\'stage3\'].forEach(k => {\n    _sg3[k] = (_sg3[k] || []).filter(t => t !== ticker);\n  });\n  sg3Render();\n  sg3Save();\n}\n\nasync function sg3Save() {\n  try {\n    await fetch(\'/api/stagegate\', {\n      method: \'POST\',\n      headers: {\'Content-Type\': \'application/json\'},\n      body: JSON.stringify(_sg3),\n    });\n  } catch(e) {}\n}\n\n// ── Hook into existing load() to sync Stage 3 ────────────────────────────────\nconst _origLoad = load;\nwindow.load = async function() {\n  await _origLoad();\n  try {\n    const acct = await fetch(\'/api/paper/account\').then(r => r.json());\n    sg3SyncPositions(acct.positions || []);\n  } catch(e) {}\n};\n\n// Escape closes sell modal too\ndocument.addEventListener(\'keydown\', e => {\n  if (e.key === \'Escape\') { sg3SellCancel(); aiClose(); }\n});\n\n// Boot\nsg3Boot();\n\n})(); // end IIFE\n'
PAPER_JS = PAPER_JS + _SG3_JS


@app.post("/api/paper/activate-ai")
async def api_paper_activate_ai(request: Request):
    """Stage 1 → Stage 2: activate stock for AI pipeline, no immediate buy."""
    body   = await request.json()
    ticker = str(body.get("ticker", "")).upper().strip()
    if not ticker:
        return JSONResponse(status_code=400, content={"error": "invalid ticker"})
    sg = _load_stagegate()
    for k in ("stage1", "stage2", "stage3"):
        sg.setdefault(k, [])
    if ticker in sg["stage1"]:
        sg["stage1"].remove(ticker)
    if ticker not in sg["stage2"] and ticker not in sg["stage3"]:
        sg["stage2"].append(ticker)
    _save_stagegate(sg)
    return {"status": "ok", "ticker": ticker}


@app.post("/api/paper/sell")
async def api_paper_sell(request: Request):
    """Manual paper sell; moves ticker to target_stage in stagegate."""
    import datetime
    body         = await request.json()
    ticker       = str(body.get("ticker", "")).upper().strip()
    mode         = body.get("mode", "all")      # "all" or "partial"
    qty_req      = body.get("qty", None)
    target_stage = str(body.get("target_stage", "1"))

    if not ticker:
        return JSONResponse(status_code=400, content={"error": "invalid ticker"})

    try:
        from paper.account   import init_paper_db, PaperAccount, PaperPosition, PaperTrade
        from paper.executor  import PaperExecutor
        from models.database import init_db as _init_db

        _, MainSession = _init_db(config.database.url, echo=False)
        ex    = PaperExecutor(main_db_session_factory=MainSession)
        price = ex._latest_price(ticker)

        _, PaperSession = init_paper_db()
        with PaperSession() as session:
            acct = session.query(PaperAccount).first()
            pos  = session.query(PaperPosition).filter_by(ticker=ticker).first()

            if not pos or pos.qty <= 0:
                sg = _load_stagegate()
                for k in ("stage1","stage2","stage3"):
                    sg.setdefault(k, [])
                    if ticker in sg[k]: sg[k].remove(ticker)
                sg.setdefault(f"stage{target_stage}", [])
                sg[f"stage{target_stage}"].append(ticker)
                _save_stagegate(sg)
                return {"status": "ok", "ticker": ticker, "qty": 0, "no_position": True}

            if not price or price <= 0:
                price = pos.avg_cost

            if mode == "all":
                qty = pos.qty
            else:
                qty = min(float(qty_req or pos.qty), pos.qty)
                qty = max(1, int(qty))

            proceeds = round(qty * price, 2)
            acct.cash += proceeds
            pos.qty   -= qty
            pos.updated_at = datetime.datetime.utcnow()
            if pos.qty <= 0.001:
                session.delete(pos)

            session.add(PaperTrade(
                ticker=ticker, action="SELL", qty=qty, price=price,
                total=proceeds, cash_after=round(acct.cash, 2),
                signal="MANUAL", notes="Manual sell via Stage Gate",
            ))
            session.commit()

        sg = _load_stagegate()
        for k in ("stage1","stage2","stage3"):
            sg.setdefault(k, [])
            if ticker in sg[k]: sg[k].remove(ticker)
        sg[f"stage{target_stage}"].append(ticker)
        _save_stagegate(sg)

        return {"status": "ok", "ticker": ticker, "qty": qty, "price": price, "proceeds": proceeds}

    except Exception as exc:
        import traceback; traceback.print_exc()
        return JSONResponse(status_code=503, content={"error": str(exc)})

# ── Fix: expose sg3 functions globally (IIFE scope fix) ──────────────────────
_EXPOSE_JS = '\n// Expose sg3 functions globally for HTML ondragover/ondrop/etc. attributes\nwindow.sg3Over           = sg3Over;\nwindow.sg3Leave          = sg3Leave;\nwindow.sg3Drop           = sg3Drop;\nwindow.sg3DragStart      = sg3DragStart;\nwindow.sg3DragEnd        = sg3DragEnd;\nwindow.sg3ActivateAI     = sg3ActivateAI;\nwindow.sg3OpenBuy        = sg3OpenBuy;\nwindow.sg3OpenSell       = sg3OpenSell;\nwindow.sg3SellModeChange = sg3SellModeChange;\nwindow.sg3UpdateSellHint = sg3UpdateSellHint;\nwindow.sg3SellCancel     = sg3SellCancel;\nwindow.sg3SellConfirm    = sg3SellConfirm;\nwindow.sg3Remove         = sg3Remove;\nwindow.sg3Boot           = sg3Boot;\n\n'
PAPER_JS   = PAPER_JS.replace('sg3Boot();\n\n})(); // end IIFE',
                               _EXPOSE_JS + 'sg3Boot();\n\n})(); // end IIFE')


# ── Paper dashboard feature additions (auto-patched) ─────────────────────────
_FEAT_JS  = '\n// ════════════════════════════════════════════════════════════════════════════\n// Paper dashboard feature additions\n// ════════════════════════════════════════════════════════════════════════════\n\n// ── Inject extra CSS ─────────────────────────────────────────────────────────\n(function() {\n  const s = document.createElement(\'style\');\n  s.textContent = \'\\n  /* ── Ticker tape (paper page) ─────────────────────────────── */\\n  .p-tape-wrap  { overflow: hidden; background: #0a0f17;\\n                  border-bottom: 1px solid #1f6feb; height: 26px; flex-shrink: 0; }\\n  .p-tape-track { display: flex; gap: 24px; white-space: nowrap; will-change: transform;\\n                  animation: p-tape 100s linear infinite; align-items: center; height: 100%;\\n                  padding-left: 12px; }\\n  .p-tape-track:hover { animation-play-state: paused; }\\n  @keyframes p-tape { 0%{transform:translateX(0)} 100%{transform:translateX(-50%)} }\\n  .pt-bull { color: #3fb950; font-size: 12px; font-weight: 700; }\\n  .pt-bear { color: #f85149; font-size: 12px; font-weight: 700; }\\n  .pt-neu  { color: #8b949e; font-size: 12px; }\\n  .pt-sep  { color: #30363d; font-size: 10px; }\\n  /* ── Search + sync header controls ────────────────────────── */\\n  .p-search-wrap { display: flex; gap: 5px; align-items: center; }\\n  .p-search-input { padding: 5px 10px; border-radius: 6px; border: 1px solid #30363d;\\n                    background: #21262d; color: #e6edf3; font-size: 12px; width: 130px;\\n                    text-transform: uppercase; }\\n  .p-search-input::placeholder { color: #8b949e; text-transform: none; }\\n  .p-add-s1 { padding: 5px 9px; border-radius: 6px; border: 1px solid #8b949e;\\n               background: transparent; color: #8b949e; cursor: pointer; font-size: 11px; }\\n  .p-add-s1:hover { background: rgba(139,148,158,0.12); }\\n  .p-add-s2 { padding: 5px 9px; border-radius: 6px; border: 1px solid #58a6ff;\\n               background: transparent; color: #58a6ff; cursor: pointer; font-size: 11px; }\\n  .p-add-s2:hover { background: rgba(88,166,255,0.12); }\\n  .p-sync-btn { padding: 5px 10px; border-radius: 6px; border: 1px solid #d29922;\\n                background: transparent; color: #d29922; cursor: pointer; font-size: 11px; }\\n  .p-sync-btn:hover { background: rgba(210,153,34,0.12); }\\n  /* ── Compact swim lanes ───────────────────────────────────── */\\n  .swim-compact-wrap { padding: 10px 14px; display: flex; flex-direction: column; gap: 10px; }\\n  .swim-row  { display: flex; gap: 8px; align-items: flex-start; }\\n  .swim-row-label { font-size: 10px; font-weight: 700; letter-spacing: 0.5px;\\n                    text-transform: uppercase; width: 90px; flex-shrink: 0; padding-top: 4px; }\\n  .swim-row-0 .swim-row-label { color: #58a6ff; }\\n  .swim-row-1 .swim-row-label { color: #d29922; }\\n  .swim-row-2 .swim-row-label { color: #3fb950; }\\n  .swim-chips { display: flex; gap: 5px; flex-wrap: wrap; }\\n  .swim-chip  { padding: 3px 8px; border-radius: 10px; font-size: 11px; font-weight: 600;\\n                background: rgba(63,185,80,0.12); color: #3fb950; border: 1px solid rgba(63,185,80,0.3); }\\n  .swim-none  { font-size: 11px; color: #8b949e; font-style: italic; padding-top: 3px; }\\n\';\n  document.head.appendChild(s);\n})();\n\n// ── Inject search + sync into header ─────────────────────────────────────────\n(function() {\n  const hr = document.querySelector(\'.header-right\');\n  if (hr) hr.insertAdjacentHTML(\'afterbegin\', \'\\n  <div class="p-search-wrap">\\n    <input class="p-search-input" id="p-search" type="text" placeholder="Add ticker..."\\n           maxlength="10" onkeydown="if(event.key===\\\'Enter\\\') pAddTicker(\\\'1\\\')">\\n    <button class="p-add-s1" onclick="pAddTicker(\\\'1\\\')" title="Add to Stage 1">+S1</button>\\n    <button class="p-add-s2" onclick="pAddTicker(\\\'2\\\')" title="Add to Stage 2">+S2</button>\\n  </div>\\n  <button class="p-sync-btn" onclick="pSyncWatchlist()" title="Sync top signals to Stage 1">&#8635; Sync</button>\\n\');\n})();\n\n// ── Inject ticker tape after header ──────────────────────────────────────────\n(function() {\n  const hdr = document.querySelector(\'header\');\n  if (hdr) hdr.insertAdjacentHTML(\'afterend\', \'<div class="p-tape-wrap"><div class="p-tape-track" id="p-tape">&nbsp;</div></div>\');\n})();\n\n// ── Build ticker tape from signals ───────────────────────────────────────────\nfunction pBuildTape(sigs) {\n  const track = document.getElementById(\'p-tape\');\n  if (!track) return;\n  const items = (sigs || []).filter(s => {\n    const sig = (s.signal || \'\').toUpperCase();\n    return sig === \'BUY\' || sig === \'STRONG_BUY\' || sig === \'SELL\' || sig === \'STRONG_SELL\';\n  });\n  if (!items.length) { track.innerHTML = \'<span class="pt-neu">No signals</span>\'; return; }\n  const all = [...items, ...items];\n  track.innerHTML = all.map(s => {\n    const sig = (s.signal || \'\').toUpperCase();\n    const bull = sig === \'BUY\' || sig === \'STRONG_BUY\';\n    const cls  = bull ? \'pt-bull\' : \'pt-bear\';\n    const arr  = bull ? \'&#9650;\' : \'&#9660;\';\n    const price = s.current_price ? \' $\' + s.current_price.toFixed(2) : \'\';\n    return \'<span class="\' + cls + \'">\' + arr + \' \' + s.ticker + price + \'</span>\'\n         + \'<span class="pt-sep">|</span>\';\n  }).join(\'\');\n  track.style.animationDuration = Math.max(40, items.length * 0.7) + \'s\';\n}\n\n// ── Compact swim lanes (replace 3-col grid with single box) ──────────────────\nfunction pBuildCompactSwim(sigs) {\n  const swimSec = document.getElementById(\'swim-section\');\n  if (!swimSec) return;\n\n  const MODELS = [\n    { label: \'Standard\',     conf: 0.50,  mos: 0.15,   fud: 0.60 },\n    { label: \'Relaxed -25%\', conf: 0.375, mos: 0.1125, fud: 0.45 },\n    { label: \'Relaxed -50%\', conf: 0.25,  mos: 0.075,  fud: 0.30 },\n  ];\n\n  const rows = MODELS.map((m, i) => {\n    const passing = (sigs || []).filter(s => {\n      const sig = (s.signal || \'\').toUpperCase();\n      return (s.confidence || 0) >= m.conf\n          && (s.margin_of_safety || 0) >= m.mos\n          && (s.fud_score || 0) >= m.fud\n          && (sig === \'BUY\' || sig === \'STRONG_BUY\');\n    }).sort((a, b) => (b.confidence || 0) - (a.confidence || 0));\n\n    const chips = passing.length\n      ? passing.map(s =>\n          \'<span class="swim-chip" title="Conf \' + ((s.confidence||0)*100).toFixed(0) + \'% | MoS \'\n          + ((s.margin_of_safety||0)*100).toFixed(1) + \'%">\' + s.ticker + \'</span>\'\n        ).join(\'\')\n      : \'<span class="swim-none">None clear this bar</span>\';\n\n    return \'<div class="swim-row swim-row-\' + i + \'">\'\n      + \'<div class="swim-row-label">\' + m.label + \'</div>\'\n      + \'<div class="swim-chips">\' + chips + \'</div>\'\n      + \'</div>\';\n  });\n\n  swimSec.querySelector(\'.section-title\').textContent = \'\\ud83d\\udcca Threshold Models\';\n  let body = swimSec.querySelector(\'.swim-compact-wrap\');\n  if (!body) {\n    // Replace old grid with compact wrap\n    const old = swimSec.querySelector(\'.swim-wrap\');\n    if (old) old.remove();\n    body = document.createElement(\'div\');\n    body.className = \'swim-compact-wrap\';\n    swimSec.appendChild(body);\n  }\n  body.innerHTML = rows.join(\'\');\n\n  // Move swim section below sg3-section\n  const sg3Sec = document.getElementById(\'sg3-section\');\n  if (sg3Sec && swimSec.parentNode) {\n    sg3Sec.insertAdjacentElement(\'afterend\', swimSec);\n  }\n}\n\n// ── Search: add ticker to stage 1 or 2 ───────────────────────────────────────\nfunction pAddTicker(toStage) {\n  const inp = document.getElementById(\'p-search\');\n  const ticker = (inp ? inp.value : \'\').trim().toUpperCase();\n  if (!ticker) return;\n  inp.value = \'\';\n  sg3AddTicker(ticker, toStage);\n}\n\nfunction sg3AddTicker(ticker, toStage) {\n  if (!ticker) return;\n  // Remove from all stages first to avoid duplicates\n  [\'stage1\',\'stage2\',\'stage3\'].forEach(k => {\n    if (_sg3[k] && _sg3[k].includes(ticker)) return; // already there\n  });\n  const already = (_sg3.stage1||[]).includes(ticker)\n               || (_sg3.stage2||[]).includes(ticker)\n               || (_sg3.stage3||[]).includes(ticker);\n  if (already) return;\n  (_sg3[\'stage\' + toStage] || []).push(ticker);\n  sg3Render();\n  sg3Save();\n}\nwindow.sg3AddTicker = sg3AddTicker;\nwindow.pAddTicker   = pAddTicker;\n\n// ── Sync watchlist: top-5 bullish I-Tool + all recent signals ─────────────────\nasync function pSyncWatchlist() {\n  const btn = document.querySelector(\'.p-sync-btn\');\n  if (btn) { btn.disabled = true; btn.textContent = \'\\u29d7 Syncing...\'; }\n  try {\n    const [itool, sigs] = await Promise.all([\n      fetch(\'/api/itool\').then(r => r.json()).catch(() => ({})),\n      fetch(\'/api/signals\').then(r => r.json()).catch(() => []),\n    ]);\n\n    const toAdd = new Set();\n\n    // Top 5 bullish from I-Tool\n    const itoolResults = (itool.results || [])\n      .filter(r => r.signal === \'bullish\')\n      .slice(0, 5);\n    itoolResults.forEach(r => toAdd.add(r.ticker));\n\n    // All bullish from recent signals\n    (sigs || []).forEach(s => {\n      const sig = (s.signal || \'\').toUpperCase();\n      if (sig === \'BUY\' || sig === \'STRONG_BUY\') toAdd.add(s.ticker);\n    });\n\n    let added = 0;\n    toAdd.forEach(ticker => {\n      const inAny = (_sg3.stage1||[]).includes(ticker)\n                 || (_sg3.stage2||[]).includes(ticker)\n                 || (_sg3.stage3||[]).includes(ticker);\n      if (!inAny) {\n        (_sg3.stage1 = _sg3.stage1 || []).push(ticker);\n        added++;\n      }\n    });\n\n    if (added > 0) {\n      sg3Render();\n      await sg3Save();\n    }\n    if (btn) btn.textContent = \'\\u2713 Synced +\' + added;\n  } catch(e) {\n    if (btn) btn.textContent = \'Error\';\n  } finally {\n    setTimeout(() => { if (btn) { btn.disabled = false; btn.textContent = \'\\u8635 Sync\'; } }, 3000);\n  }\n}\nwindow.pSyncWatchlist = pSyncWatchlist;\n\n// ── Hook into existing loadSwimLanes / signal load to drive tape + compact swim ─\nconst _origLoadSwimLanes = typeof loadSwimLanes === \'function\' ? loadSwimLanes : null;\nwindow.loadSwimLanes = async function() {\n  if (_origLoadSwimLanes) await _origLoadSwimLanes();\n  try {\n    const sigs = await fetch(\'/api/signals\').then(r => r.json());\n    pBuildTape(sigs);\n    pBuildCompactSwim(sigs);\n  } catch(e) {}\n};\n\n// Also seed tape immediately from already-loaded _sg3sigs\nsetTimeout(() => {\n  const sigsArr = Object.values(_sg3sigs || {});\n  if (sigsArr.length) {\n    pBuildTape(sigsArr);\n    pBuildCompactSwim(sigsArr);\n  }\n}, 800);\n'
PAPER_JS  = PAPER_JS + _FEAT_JS


# ── Morning Brief auto-regeneration at 06:00 and 09:00 ET ────────────────────
@app.on_event("startup")
async def _schedule_morning_brief():
    import threading, time, logging as _log
    from datetime import datetime
    import pytz

    _slog = _log.getLogger("brief_scheduler")
    ET = pytz.timezone("America/New_York")

    def _brief_scheduler():
        fired_today = set()
        while True:
            try:
                now = datetime.now(ET)
                key_6  = (now.date(), 6)
                key_9  = (now.date(), 9)
                if now.hour == 6 and now.minute == 0 and key_6 not in fired_today:
                    fired_today.add(key_6)
                    _trigger_brief_generation()
                    _slog.info("[BRIEF] Auto-regen triggered at 06:00 ET")
                elif now.hour == 9 and now.minute == 0 and key_9 not in fired_today:
                    fired_today.add(key_9)
                    _trigger_brief_generation()
                    _slog.info("[BRIEF] Auto-regen triggered at 09:00 ET")
                # Prune old keys daily
                today = now.date()
                fired_today = {k for k in fired_today if k and k[0] == today}
            except Exception as e:
                _slog.warning(f"[BRIEF] Scheduler error: {e}")
            time.sleep(30)

    threading.Thread(target=_brief_scheduler, daemon=True).start()
    _slog.info("[BRIEF] Auto-regen scheduler started (06:00 + 09:00 ET daily)")


# ── Paper trading auto-scheduler startup ──────────────────────────────────────
@app.on_event("startup")
async def _start_paper_scheduler():
    import logging as _logging
    _log = _logging.getLogger("paper_scheduler")
    try:
        from paper.auto_scheduler import init_scheduler
        init_scheduler(main_db_url=config.database.url)
        _log.info("[AUTO] Paper scheduler initialised")
    except Exception as e:
        _log.warning(f"[AUTO] Paper scheduler startup failed (will use manual Run Now): {e}")


@app.get("/api/paper/scheduler")
def api_paper_scheduler_status():
    try:
        from paper.auto_scheduler import get_scheduler
        sched = get_scheduler()
        if sched:
            return sched.status()
    except Exception:
        pass
    return {"running": False, "paused": False, "market_hours": False,
            "last_cycle": None, "next_cycle": None, "cycle_count": 0,
            "stop_exits": 0, "engines_ready": False}


@app.post("/api/paper/scheduler/pause")
def api_paper_scheduler_pause():
    try:
        from paper.auto_scheduler import get_scheduler
        sched = get_scheduler()
        if sched:
            sched.pause()
            return {"status": "paused"}
    except Exception:
        pass
    return {"status": "error"}


@app.post("/api/paper/scheduler/resume")
def api_paper_scheduler_resume():
    try:
        from paper.auto_scheduler import get_scheduler
        sched = get_scheduler()
        if sched:
            sched.resume()
            return {"status": "resumed"}
    except Exception:
        pass
    return {"status": "error"}


# ── Fix: search/sync use IIFE-scoped vars — must inject inside IIFE ──────────
_ADD_INSIDE_JS = "\n// sg3AddTicker — inside IIFE: has access to _sg3, sg3Render, sg3Save\nfunction sg3AddTicker(ticker, toStage) {\n  if (!ticker) return;\n  const already = (_sg3.stage1||[]).includes(ticker)\n               || (_sg3.stage2||[]).includes(ticker)\n               || (_sg3.stage3||[]).includes(ticker);\n  if (already) return;\n  (_sg3['stage' + toStage] = _sg3['stage' + toStage] || []).push(ticker);\n  sg3Render();\n  sg3Save();\n}\nwindow.sg3AddTicker = sg3AddTicker;\nwindow._sg3Ref = () => _sg3;\n\n"
_SYNC_NEW_JS   = "    let added = 0;\n    toAdd.forEach(ticker => {\n      const sg3 = window._sg3Ref ? window._sg3Ref() : {};\n      const inAny = (sg3.stage1||[]).includes(ticker)\n                 || (sg3.stage2||[]).includes(ticker)\n                 || (sg3.stage3||[]).includes(ticker);\n      window.sg3AddTicker(ticker, '1');\n      if (!inAny) added++;\n    });\n\n"

# 1. Inject sg3AddTicker + _sg3Ref inside IIFE (before sg3Boot)
PAPER_JS = PAPER_JS.replace(
    'sg3Boot();\n\n})(); // end IIFE',
    _ADD_INSIDE_JS + 'sg3Boot();\n\n})(); // end IIFE'
)

# 2. Remove broken outer sg3AddTicker (references _sg3 which is IIFE-scoped)
_BAD = (
    'function sg3AddTicker(ticker, toStage) {\n'
    '  if (!ticker) return;\n'
    '  // Remove from all stages first to avoid duplicates\n'
    "  ['stage1','stage2','stage3'].forEach(k => {\n"
    "    if (_sg3[k] && _sg3[k].includes(ticker)) return; // already there\n"
    '  });\n'
    '  const already = (_sg3.stage1||[]).includes(ticker)\n'
    '               || (_sg3.stage2||[]).includes(ticker)\n'
    "               || (_sg3.stage3||[]).includes(ticker);\n"
    '  if (already) return;\n'
    "  (_sg3['stage' + toStage] || []).push(ticker);\n"
    '  sg3Render();\n'
    '  sg3Save();\n'
    '}\n'
    'window.sg3AddTicker = sg3AddTicker;\n'
)
PAPER_JS = PAPER_JS.replace(_BAD, '// sg3AddTicker exposed via window from inside IIFE\n')

# 3. Fix pSyncWatchlist: replace direct _sg3 block with window.sg3AddTicker calls
_SYNC_OLD = (
    '    let added = 0;\n'
    '    toAdd.forEach(ticker => {\n'
    '      const inAny = (_sg3.stage1||[]).includes(ticker)\n'
    '                 || (_sg3.stage2||[]).includes(ticker)\n'
    '                 || (_sg3.stage3||[]).includes(ticker);\n'
    '      if (!inAny) {\n'
    "        (_sg3.stage1 = _sg3.stage1 || []).push(ticker);\n"
    '        added++;\n'
    '      }\n'
    '    });\n'
    '\n'
    '    if (added > 0) {\n'
    '      sg3Render();\n'
    '      await sg3Save();\n'
    '    }\n'
)
PAPER_JS = PAPER_JS.replace(_SYNC_OLD, _SYNC_NEW_JS)


# ── Threshold patch ───────────────────────────────────────────────────────────
# 1. Hide swim-wrap + inject threshold bar CSS
PAPER_HTML = PAPER_HTML.replace('</style>', "\n  /* ── Hide old swim-wrap ─────────────────────────────────────── */\n  #swim-wrap, .swim-wrap { display: none !important; }\n  /* ── AI Threshold control bar ──────────────────────────────── */\n  .thresh-bar { display: flex; align-items: center; gap: 10px; padding: 8px 14px;\n                background: #161b22; border: 1px solid #30363d; border-radius: 8px;\n                flex-wrap: wrap; }\n  .thresh-label { font-size: 10px; font-weight: 700; color: #8b949e;\n                  letter-spacing: 0.5px; text-transform: uppercase; white-space: nowrap; }\n  .thresh-grp { display: flex; gap: 3px; }\n  .thresh-btn  { padding: 3px 11px; border-radius: 20px; border: 1px solid #30363d;\n                 background: transparent; color: #8b949e; font-size: 11px; cursor: pointer; }\n  .thresh-btn.t-high { border-color: #f85149; color: #f85149; background: rgba(248,81,73,0.1); }\n  .thresh-btn.t-med  { border-color: #d29922; color: #d29922; background: rgba(210,153,34,0.1); }\n  .thresh-btn.t-low  { border-color: #3fb950; color: #3fb950; background: rgba(63,185,80,0.1); }\n  .thresh-desc { font-size: 10px; color: #8b949e; white-space: nowrap; }\n  .thresh-chips { display: flex; gap: 5px; flex-wrap: wrap; margin-left: auto; }\n  .thresh-chip  { padding: 2px 7px; border-radius: 10px; font-size: 11px; font-weight: 600;\n                  background: rgba(63,185,80,0.12); color: #3fb950;\n                  border: 1px solid rgba(63,185,80,0.3); }\n  .thresh-none  { font-size: 11px; color: #8b949e; font-style: italic; }\n  /* ── Gate toggles in AI modal ───────────────────────────────── */\n  .ai-gate-sect { margin-top: 12px; padding-top: 10px; border-top: 1px solid #21262d; }\n  .ai-gate-title { font-size: 10px; color: #8b949e; font-weight: 700;\n                   letter-spacing: 0.5px; text-transform: uppercase; margin-bottom: 8px; }\n  .gate-row { display: flex; gap: 8px; flex-wrap: wrap; }\n  .gate-tog { display: flex; align-items: center; gap: 5px; cursor: pointer;\n              padding: 4px 9px; border-radius: 6px; border: 1px solid #30363d;\n              background: #0d1117; user-select: none; }\n  .gate-tog:hover { border-color: #58a6ff; }\n  .gate-tog.bypassed { border-color: #d29922; background: rgba(210,153,34,0.08); }\n  .gate-name { font-size: 11px; font-weight: 700; color: #e6edf3; }\n  .gate-tog.bypassed .gate-name { color: #d29922; }\n  .gate-hint { font-size: 10px; color: #8b949e; }\n  .gate-sw { width: 28px; height: 14px; border-radius: 7px; background: #30363d;\n             position: relative; flex-shrink: 0; }\n  .gate-tog.bypassed .gate-sw { background: #d29922; }\n  .gate-sw::after { content: ''; position: absolute; top: 2px; left: 2px;\n                    width: 10px; height: 10px; border-radius: 50%; background: #8b949e; }\n  .gate-tog.bypassed .gate-sw::after { left: 16px; background: #fff; }\n" + '</style>', 1)

# 2. Inject gate toggles into AI modal (before modal footer)
PAPER_HTML = PAPER_HTML.replace(
    '<div class="ai-modal-foot">',
    '\n    <div class="ai-gate-sect" id="ai-gate-sect">\n      <div class="ai-gate-title">&#9881; Gate Overrides &mdash; bypass for this ticker</div>\n      <div class="gate-row" id="gate-row"></div>\n    </div>\n' + '<div class="ai-modal-foot">',
    1
)

# 3. Inject threshold + gate JS inside IIFE
_THRESH_IIFE_JS = '\n// ── AI Threshold control ──────────────────────────────────────────────────────\nconst THRESH_CFG = {\n  high: { label:\'High\', re:5.0,  ens:0.550, qst:0.450, rr:1.50, kal:2.50 },\n  med:  { label:\'Med\',  re:5.75, ens:0.468, qst:0.383, rr:1.28, kal:2.88 },\n  low:  { label:\'Low\',  re:6.50, ens:0.385, qst:0.315, rr:1.05, kal:3.25 },\n};\nlet _thresh = localStorage.getItem(\'sg3_thresh\') || \'high\';\nlet _gateOv  = JSON.parse(localStorage.getItem(\'sg3_gate_ov\') || \'{}\');\nlet _lastSigs = [];\n\nfunction _threshDesc(t) {\n  const c = THRESH_CFG[t];\n  return \'Re<\' + c.re + \' · Ens>\' + Math.round(c.ens*100) + \'% · QSt>\'\n       + Math.round(c.qst*100) + \'% · R/R>\' + c.rr + \' · Kal<\' + c.kal + \'σ\';\n}\n\nfunction setThreshLevel(lv) {\n  _thresh = lv;\n  localStorage.setItem(\'sg3_thresh\', lv);\n  fetch(\'/api/paper/set-thresh\', {method:\'POST\',\n    headers:{\'Content-Type\':\'application/json\'}, body:JSON.stringify({level:lv})}).catch(()=>{});\n  renderThreshBar();\n}\nwindow.setThreshLevel = setThreshLevel;\nwindow._threshGet = () => THRESH_CFG[_thresh];\n\nfunction renderThreshBar() {\n  const bar = document.getElementById(\'thresh-bar\');\n  if (!bar) return;\n  const c = THRESH_CFG[_thresh];\n  const passing = _lastSigs.filter(s => {\n    const sig = (s.signal||\'\').toUpperCase();\n    return (s.confidence||0) >= c.ens && (sig===\'BUY\'||sig===\'STRONG_BUY\');\n  }).sort((a,b) => (b.confidence||0)-(a.confidence||0));\n  const chips = passing.length\n    ? passing.map(s => \'<span class="thresh-chip" title="Conf \'\n        + Math.round((s.confidence||0)*100) + \'%">\' + s.ticker + \'</span>\').join(\'\')\n    : \'<span class="thresh-none">No signals at this threshold</span>\';\n  bar.innerHTML =\n    \'<span class="thresh-label">⚡ AI Gates:</span>\' +\n    \'<div class="thresh-grp">\' +\n    [\'high\',\'med\',\'low\'].map(lv => {\n      const act = _thresh===lv;\n      return \'<button class="thresh-btn\' + (act?\' t-\'+lv:\'\') + \'" onclick="setThreshLevel(\\\'\' + lv + \'\\\')">\'\n           + (act?\'● \':\'○ \') + THRESH_CFG[lv].label + \'</button>\';\n    }).join(\'\') + \'</div>\' +\n    \'<span class="thresh-desc">\' + _threshDesc(_thresh) + \'</span>\' +\n    \'<div class="thresh-chips">\' + chips + \'</div>\';\n}\nwindow.renderThreshBar = function(sigs) { if(sigs) _lastSigs=sigs; renderThreshBar(); };\n\n// Inject thresh bar after sg3-section once DOM is ready\nsetTimeout(function() {\n  const sg3 = document.getElementById(\'sg3-section\');\n  if (sg3 && !document.getElementById(\'thresh-bar\')) {\n    const el = document.createElement(\'div\');\n    el.id = \'thresh-bar\'; el.className = \'thresh-bar\';\n    sg3.insertAdjacentElement(\'afterend\', el);\n    renderThreshBar();\n  }\n}, 600);\n\n// ── Per-ticker gate overrides ─────────────────────────────────────────────────\nconst GATE_DEFS = [\n  {key:\'reynolds\', abbr:\'Re\',  hint:\'Reynolds turbulence\'},\n  {key:\'ensemble\', abbr:\'Ens\', hint:\'Ensemble probability\'},\n  {key:\'quantum\',  abbr:\'QSt\', hint:\'Quantum state\'},\n  {key:\'rr\',       abbr:\'R/R\', hint:\'Risk/reward ratio\'},\n  {key:\'kalman\',   abbr:\'Kal\', hint:\'Kalman filter\'},\n];\n\nfunction sgRenderGateToggles(ticker) {\n  const row = document.getElementById(\'gate-row\');\n  if (!row) return;\n  const tov = _gateOv[ticker] || [];\n  row.innerHTML = GATE_DEFS.map(g => {\n    const by = tov.includes(g.key);\n    return \'<div class="gate-tog\' + (by?\' bypassed\':\'\') + \'" \'\n      + \'onclick="sgToggleGate(\\\'\' + ticker + \'\\\',\\\'\' + g.key + \'\\\')" \'\n      + \'title="\' + (by?\'BYPASSED\':\'Active\') + \'">\'\n      + \'<div class="gate-sw"></div>\'\n      + \'<span class="gate-name">\' + g.abbr + \'</span>\'\n      + \'<span class="gate-hint">\' + g.hint + \'</span>\'\n      + \'</div>\';\n  }).join(\'\');\n}\nwindow.sgRenderGateToggles = sgRenderGateToggles;\n\nfunction sgToggleGate(ticker, key) {\n  if (!_gateOv[ticker]) _gateOv[ticker] = [];\n  const i = _gateOv[ticker].indexOf(key);\n  if (i===-1) _gateOv[ticker].push(key); else _gateOv[ticker].splice(i,1);\n  if (!_gateOv[ticker].length) delete _gateOv[ticker];\n  localStorage.setItem(\'sg3_gate_ov\', JSON.stringify(_gateOv));\n  fetch(\'/api/paper/set-gate\', {method:\'POST\',\n    headers:{\'Content-Type\':\'application/json\'}, body:JSON.stringify(_gateOv)}).catch(()=>{});\n  sgRenderGateToggles(ticker);\n}\nwindow.sgToggleGate = sgToggleGate;\n'
PAPER_JS = PAPER_JS.replace(
    'sg3Boot();\n\n})(); // end IIFE',
    _THRESH_IIFE_JS + 'sg3Boot();\n\n})(); // end IIFE'
)

# 4. Hook renderThreshBar into signal loads
PAPER_JS = PAPER_JS.replace(
    'pBuildTape(sigs);\n    pBuildCompactSwim(sigs);',
    'pBuildTape(sigs);\n    pBuildCompactSwim(sigs);\n    if(window.renderThreshBar) renderThreshBar(sigs);'
)
PAPER_JS = PAPER_JS.replace(
    'pBuildTape(sigsArr);\n    pBuildCompactSwim(sigsArr);',
    'pBuildTape(sigsArr);\n    pBuildCompactSwim(sigsArr);\n    if(window.renderThreshBar) renderThreshBar(sigsArr);'
)

# 5. Hook gate toggles into AI modal open
PAPER_JS = PAPER_JS.replace(
    "document.getElementById('ai-overlay').style.display = 'flex';",
    "document.getElementById('ai-overlay').style.display = 'flex';\n"
    "  if(window.sgRenderGateToggles) { var _t=document.getElementById('ai-ticker'); if(_t) sgRenderGateToggles(_t.textContent.trim()); }"
)


# ── Threshold & gate override endpoints ──────────────────────────────────────
@app.post("/api/paper/set-thresh")
async def api_set_thresh(request: Request):
    import json as _j
    data = await request.json()
    level = data.get("level", "high")
    if level not in ("high", "med", "low"):
        return JSONResponse(status_code=400, content={"error": "invalid level"})
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "thresh_override.json").write_text(
        _j.dumps({"level": level}), encoding="utf-8"
    )
    return {"ok": True, "level": level}


@app.post("/api/paper/set-gate")
async def api_set_gate(request: Request):
    import json as _j
    overrides = await request.json()
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "gate_overrides.json").write_text(
        _j.dumps(overrides), encoding="utf-8"
    )
    return {"ok": True}


def _apply_thresh_override():
    """Apply thresh_override.json settings to DecisionEngine class constants."""
    import json as _j
    MULT = {"high": 1.0, "med": 0.85, "low": 0.70}
    try:
        f = ROOT / "data" / "thresh_override.json"
        lv = _j.loads(f.read_text()).get("level", "high") if f.exists() else "high"
        m = MULT.get(lv, 1.0)
        from decision.engine import DecisionEngine as _DE
        _DE.MIN_ENSEMBLE_PROB   = round(0.55 * m, 4)
        _DE.MIN_QUANTUM_CERTAIN = round(0.45 * m, 4)
        _DE.MAX_REYNOLDS        = round(5.0  / m, 4) if m > 0 else 5.0
        _DE.MIN_RR_RATIO        = round(1.5  * m, 4)
        _DE.MAX_KALMAN_SURPRISE = round(2.5  / m, 4) if m > 0 else 2.5
    except Exception:
        pass


# ── Stage layout fixes ────────────────────────────────────────────────────────
PAPER_HTML = PAPER_HTML.replace('</style>', "\n  /* ── Pipeline flow header (Sankey-style) ───────────────────── */\n  .sg3-pipeline { display: flex; align-items: stretch; margin-bottom: 10px; gap: 0; }\n  .sg3-pnode { flex: 1; background: #0d1117; border: 1px solid #21262d; border-radius: 8px;\n               padding: 8px 12px; text-align: center; position: relative; }\n  .sg3-pnode-s1 { border-color: #58a6ff; }\n  .sg3-pnode-s2 { border-color: #d29922; }\n  .sg3-pnode-s3 { border-color: #3fb950; }\n  .sg3-pcount { font-size: 24px; font-weight: 700; line-height: 1; }\n  .sg3-pnode-s1 .sg3-pcount { color: #58a6ff; }\n  .sg3-pnode-s2 .sg3-pcount { color: #d29922; }\n  .sg3-pnode-s3 .sg3-pcount { color: #3fb950; }\n  .sg3-pname  { font-size: 10px; color: #8b949e; font-weight: 600; letter-spacing: 0.5px;\n                text-transform: uppercase; margin-top: 3px; }\n  .sg3-pflow  { display: flex; align-items: center; justify-content: center;\n                padding: 0 4px; flex-shrink: 0; position: relative; }\n  .sg3-pflow-line { flex: 1; height: 2px; background: linear-gradient(90deg, #30363d 0%, #30363d 100%);\n                    position: relative; min-width: 28px; }\n  .sg3-pflow-line::after { content: '\\25b6'; position: absolute; right: -6px; top: 50%;\n                            transform: translateY(-50%); color: #30363d; font-size: 10px; }\n  .sg3-pflow-label { position: absolute; top: -16px; left: 50%; transform: translateX(-50%);\n                     font-size: 9px; color: #484f58; white-space: nowrap; font-weight: 600;\n                     letter-spacing: 0.3px; text-transform: uppercase; }\n" + '</style>', 1)

_STAGE_FIXES_JS = '\n// ── Pipeline flow header ──────────────────────────────────────────────────────\nfunction sg3BuildPipeline() {\n  const sg3Sec = document.getElementById(\'sg3-section\');\n  if (!sg3Sec || document.getElementById(\'sg3-pipeline\')) return;\n  const div = document.createElement(\'div\');\n  div.className = \'sg3-pipeline\'; div.id = \'sg3-pipeline\';\n  div.innerHTML =\n    \'<div class="sg3-pnode sg3-pnode-s1">\'\n      + \'<div class="sg3-pcount" id="sg3-pc-1">0</div>\'\n      + \'<div class="sg3-pname">Monitoring</div>\'\n    + \'</div>\'\n    + \'<div class="sg3-pflow"><div class="sg3-pflow-line"></div>\'\n      + \'<span class="sg3-pflow-label">Promote AI</span></div>\'\n    + \'<div class="sg3-pnode sg3-pnode-s2">\'\n      + \'<div class="sg3-pcount" id="sg3-pc-2">0</div>\'\n      + \'<div class="sg3-pname">Active AI</div>\'\n    + \'</div>\'\n    + \'<div class="sg3-pflow"><div class="sg3-pflow-line"></div>\'\n      + \'<span class="sg3-pflow-label">Execute Buy</span></div>\'\n    + \'<div class="sg3-pnode sg3-pnode-s3">\'\n      + \'<div class="sg3-pcount" id="sg3-pc-3">0</div>\'\n      + \'<div class="sg3-pname">Positions</div>\'\n    + \'</div>\';\n  // Insert pipeline header at top of section (after title)\n  const title = sg3Sec.querySelector(\'.section-title\');\n  if (title) title.insertAdjacentElement(\'afterend\', div);\n  else sg3Sec.prepend(div);\n}\nwindow.sg3BuildPipeline = sg3BuildPipeline;\n\nfunction sg3UpdatePipelineCounts() {\n  const c1 = document.getElementById(\'sg3-pc-1\');\n  const c2 = document.getElementById(\'sg3-pc-2\');\n  const c3 = document.getElementById(\'sg3-pc-3\');\n  if (c1) c1.textContent = (_sg3.stage1||[]).length;\n  if (c2) c2.textContent = (_sg3.stage2||[]).length;\n  if (c3) c3.textContent = (_sg3.stage3||[]).length;\n}\nwindow.sg3UpdatePipelineCounts = sg3UpdatePipelineCounts;\n\n// Override sg3Render to also update pipeline counts\nconst _origSg3Render = typeof sg3Render === \'function\' ? sg3Render : null;\nfunction sg3Render() {\n  if (_origSg3Render) _origSg3Render();\n  sg3UpdatePipelineCounts();\n}\nwindow.sg3Render = sg3Render;\n\n// ── Sell validation: only open sell modal if position exists ──────────────────\nfunction sg3OpenSell(ticker, targetStage) {\n  _sg3sellTicker = ticker;\n  _sg3sellTarget = targetStage || \'1\';\n  const pos = _sg3pos[ticker] || {};\n\n  // No open position — just move the card, no sell needed\n  if (!pos.qty || pos.qty <= 0) {\n    const from = Object.keys({\'1\':_sg3.stage1,\'2\':_sg3.stage2,\'3\':_sg3.stage3})\n      .find(k => (_sg3[\'stage\'+k]||[]).includes(ticker));\n    if (from) sg3MoveLocal(ticker, from, _sg3sellTarget);\n    return;\n  }\n\n  document.getElementById(\'sg3-sell-title\').textContent = \'Sell \' + ticker;\n  const price = (_sg3sigs[ticker] || {}).current_price || pos.cur_price || pos.avg_cost || 0;\n  document.getElementById(\'sg3-sell-pos\').textContent =\n    pos.qty + \' shares \\u00b7 avg $\' + (pos.avg_cost||0).toFixed(2) + \' \\u00b7 now $\' + price.toFixed(2);\n  document.getElementById(\'sg3sm-all\').checked = true;\n  document.getElementById(\'sg3-sell-qty\').style.display = \'none\';\n  document.getElementById(\'sg3-sell-qty\').value = \'\';\n  const dest = targetStage === \'1\' ? \'Stage 1 (Monitoring)\' : \'Stage 2 (Active AI)\';\n  document.getElementById(\'sg3-sell-dest\').textContent = \'After sell: move to \' + dest;\n  sg3UpdateSellHint();\n  document.getElementById(\'sg3-sell-overlay\').style.display = \'flex\';\n}\nwindow.sg3OpenSell = sg3OpenSell;\n\n// ── Buy-more guard: warn if already have a position ───────────────────────────\nconst _origSg3BuyConfirm = typeof sg3BuyConfirm === \'function\' ? sg3BuyConfirm : null;\n\nfunction sg3OpenBuy(ticker, targetStage, fromStage) {\n  _sg3pendTicker = ticker;\n  _sg3pendTarget = targetStage || \'3\';\n  _sg3pendFrom   = fromStage || _sg3dragF || null;\n\n  const existing = _sg3pos[ticker];\n  if (existing && existing.qty > 0) {\n    // Already have a position — ask to confirm "buy more"\n    const price = (_sg3sigs[ticker] || {}).current_price || existing.cur_price || 0;\n    const ok = confirm(\n      ticker + \': you already hold \' + existing.qty + \' shares\'\n      + (existing.avg_cost ? \' (avg $\' + existing.avg_cost.toFixed(2) + \')\' : \'\')\n      + \'.\\n\\nBuy MORE shares now? Click OK to continue or Cancel to skip.\'\n    );\n    if (!ok) return;\n  }\n\n  const s = _sg3sigs[ticker] || {};\n  document.getElementById(\'sg3-modal-title\').textContent = \'Buy \' + ticker;\n  document.getElementById(\'sg3-modal-price\').textContent =\n    s.current_price ? \'Current price: $\' + s.current_price.toFixed(2) : \'Price not available\';\n  document.getElementById(\'sg3-mode-shares\').checked = true;\n  document.getElementById(\'sg3-amount\').value = \'\';\n  document.getElementById(\'sg3-modal-hint\').innerHTML = \'&nbsp;\';\n  document.getElementById(\'sg3-overlay\').style.display = \'flex\';\n  setTimeout(() => document.getElementById(\'sg3-amount\').focus(), 60);\n  document.getElementById(\'sg3-act-btn\').onclick = sg3BuyConfirm;\n  document.getElementById(\'sg3-act-btn\').textContent = \'\\u25b6 Buy\' + (existing && existing.qty > 0 ? \' More\' : \'\');\n}\nwindow.sg3OpenBuy = sg3OpenBuy;\n\n// ── Move thresh-bar before sg3-section instead of after ──────────────────────\nconst _origSg3Boot2 = typeof sg3Boot === \'function\' ? sg3Boot : null;\nfunction sg3Boot() {\n  if (_origSg3Boot2) _origSg3Boot2();\n  sg3BuildPipeline();\n  sg3UpdatePipelineCounts();\n  // Relocate thresh-bar to above sg3-section once it exists\n  setTimeout(() => {\n    const bar = document.getElementById(\'thresh-bar\');\n    const sg3 = document.getElementById(\'sg3-section\');\n    if (bar && sg3 && sg3.parentNode) {\n      sg3.parentNode.insertBefore(bar, sg3);\n    }\n  }, 700);\n}\nwindow.sg3Boot = sg3Boot;\n'
PAPER_JS = PAPER_JS.replace(
    'sg3Boot();\n\n})(); // end IIFE',
    _STAGE_FIXES_JS + 'sg3Boot();\n\n})(); // end IIFE'
)


# ── Fix: remove infinite-recursion sg3Render/sg3Boot overrides ───────────────
_BAD_RENDER = (
    "// Override sg3Render to also update pipeline counts\n"
    "const _origSg3Render = typeof sg3Render === 'function' ? sg3Render : null;\n"
    "function sg3Render() {\n"
    "  if (_origSg3Render) _origSg3Render();\n"
    "  sg3UpdatePipelineCounts();\n"
    "}\n"
    "window.sg3Render = sg3Render;\n"
)
PAPER_JS = PAPER_JS.replace(_BAD_RENDER,
    "// sg3Render pipeline-count hook: applied post-IIFE (see window override below)\n")

_BAD_BOOT = (
    "// ── Move thresh-bar before sg3-section instead of after ──────────────────\n"
    "const _origSg3Boot2 = typeof sg3Boot === 'function' ? sg3Boot : null;\n"
    "function sg3Boot() {\n"
    "  if (_origSg3Boot2) _origSg3Boot2();\n"
    "  sg3BuildPipeline();\n"
    "  sg3UpdatePipelineCounts();\n"
    "  // Relocate thresh-bar to above sg3-section once it exists\n"
    "  setTimeout(() => {\n"
    "    const bar = document.getElementById('thresh-bar');\n"
    "    const sg3 = document.getElementById('sg3-section');\n"
    "    if (bar && sg3 && sg3.parentNode) {\n"
    "      sg3.parentNode.insertBefore(bar, sg3);\n"
    "    }\n"
    "  }, 700);\n"
    "}\n"
    "window.sg3Boot = sg3Boot;\n"
)
PAPER_JS = PAPER_JS.replace(_BAD_BOOT,
    "// sg3Boot pipeline + thresh-bar hook: applied post-IIFE (see window override below)\n")

# Add safe post-IIFE wrappers after the IIFE closes
_POST_IIFE_JS = "\n// ── Post-IIFE: safe sg3Boot + sg3Render wrappers ──────────────────────────────\n(function() {\n  // Wrap sg3Boot: run original (fetches data + renders), then add pipeline UI\n  var _origBoot = window.sg3Boot;\n  window.sg3Boot = function() {\n    var r = _origBoot && _origBoot();\n    // After original async boot resolves, build pipeline header + move thresh-bar\n    var after = function() {\n      if (window.sg3BuildPipeline) window.sg3BuildPipeline();\n      if (window.sg3UpdatePipelineCounts) window.sg3UpdatePipelineCounts();\n      setTimeout(function() {\n        var bar = document.getElementById('thresh-bar');\n        var sg3e = document.getElementById('sg3-section');\n        if (bar && sg3e && sg3e.parentNode) sg3e.parentNode.insertBefore(bar, sg3e);\n      }, 800);\n    };\n    if (r && typeof r.then === 'function') r.then(after); else after();\n  };\n\n  // Wrap sg3Render: run original, then update pipeline counts\n  var _origRender = window.sg3Render;\n  window.sg3Render = function() {\n    if (_origRender) _origRender();\n    if (window.sg3UpdatePipelineCounts) window.sg3UpdatePipelineCounts();\n  };\n})();\n"
PAPER_JS = PAPER_JS.replace(
    '})(); // end IIFE',
    '})(); // end IIFE\n' + _POST_IIFE_JS
)

# ── Fix sg3Boot override (comment-less match to avoid Unicode dash issues) ────
_BAD_BOOT2 = (
    "const _origSg3Boot2 = typeof sg3Boot === 'function' ? sg3Boot : null;\n"
    "function sg3Boot() {\n"
    "  if (_origSg3Boot2) _origSg3Boot2();\n"
    "  sg3BuildPipeline();\n"
    "  sg3UpdatePipelineCounts();\n"
    "  // Relocate thresh-bar to above sg3-section once it exists\n"
    "  setTimeout(() => {\n"
    "    const bar = document.getElementById('thresh-bar');\n"
    "    const sg3 = document.getElementById('sg3-section');\n"
    "    if (bar && sg3 && sg3.parentNode) {\n"
    "      sg3.parentNode.insertBefore(bar, sg3);\n"
    "    }\n"
    "  }, 700);\n"
    "}\n"
    "window.sg3Boot = sg3Boot;\n"
)
PAPER_JS = PAPER_JS.replace(_BAD_BOOT2,
    "// sg3Boot override removed — safe wrap applied post-IIFE\n")

# ── Fix sg3OpenBuy: override uses wrong sg3- element IDs, correct to sg- ──────
_BAD_BUY = (
    "  const s = _sg3sigs[ticker] || {};\n"
    "  document.getElementById('sg3-modal-title').textContent = 'Buy ' + ticker;\n"
    "  document.getElementById('sg3-modal-price').textContent =\n"
    "    s.current_price ? 'Current price: $' + s.current_price.toFixed(2) : 'Price not available';\n"
    "  document.getElementById('sg3-mode-shares').checked = true;\n"
    "  document.getElementById('sg3-amount').value = '';\n"
    "  document.getElementById('sg3-modal-hint').innerHTML = '&nbsp;';\n"
    "  document.getElementById('sg3-overlay').style.display = 'flex';\n"
    "  setTimeout(() => document.getElementById('sg3-amount').focus(), 60);\n"
    "  document.getElementById('sg3-act-btn').onclick = sg3BuyConfirm;\n"
    "  document.getElementById('sg3-act-btn').textContent = '\u25b6 Buy' + (existing && existing.qty > 0 ? ' More' : '');\n"
)
_GOOD_BUY = (
    "  const s = _sg3sigs[ticker] || {};\n"
    "  document.getElementById('sg-modal-title').textContent = 'Buy ' + ticker;\n"
    "  document.getElementById('sg-modal-price').textContent =\n"
    "    s.current_price ? 'Current price: $' + s.current_price.toFixed(2) : 'Price not available';\n"
    "  document.getElementById('sg-mode-shares').checked = true;\n"
    "  document.getElementById('sg-amount').value = '';\n"
    "  document.getElementById('sg-modal-hint').innerHTML = '&nbsp;';\n"
    "  document.getElementById('sg-overlay').style.display = 'flex';\n"
    "  setTimeout(() => document.getElementById('sg-amount').focus(), 60);\n"
    "  document.getElementById('sg-act-btn').onclick = sg3BuyConfirm;\n"
    "  document.getElementById('sg-act-btn').textContent = '\u25b6 Buy' + (existing && existing.qty > 0 ? ' More' : '');\n"
)
PAPER_JS = PAPER_JS.replace(_BAD_BUY, _GOOD_BUY)

# ── Fix sg3OpenBuy: wrong sg3- element ID prefixes (single-line replacements) ─
# The _fix_stage_layout override used sg3- prefix but modal HTML uses sg- prefix
for _old, _new in [
    ("getElementById('sg3-modal-title')", "getElementById('sg-modal-title')"),
    ("getElementById('sg3-modal-price')", "getElementById('sg-modal-price')"),
    ("getElementById('sg3-mode-shares')", "getElementById('sg-mode-shares')"),
    ("getElementById('sg3-amount')",      "getElementById('sg-amount')"),
    ("getElementById('sg3-modal-hint')",  "getElementById('sg-modal-hint')"),
    ("getElementById('sg3-overlay')",     "getElementById('sg-overlay')"),
    ("getElementById('sg3-act-btn')",     "getElementById('sg-act-btn')"),
]:
    PAPER_JS = PAPER_JS.replace(_old, _new)
