"""
monitor/signals_dashboard.py — Live signal monitor dashboard for NWO trading pipeline.

Separate from the main dashboard.py — this focuses on real-time per-ticker
signal status, gate breakdowns, momentum, RVOL, and AI Watch overrides.

Usage:
    python -m uvicorn monitor.signals_dashboard:app --host 0.0.0.0 --port 8765 --reload

Endpoints:
    GET /            — HTML signal monitor (auto-refreshes every 30s)
    GET /api/signals — JSON: latest signal per ticker
"""

import json
import sys
import os
from datetime import datetime, timezone
from typing import Optional
import pytz

_ET = pytz.timezone("America/New_York")

def _to_et_str(dt, fmt="%Y-%m-%d %H:%M:%S ET"):
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_ET).strftime(fmt)

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

# Make sure project root is on sys.path when running as a package
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from config import config
from models.database import init_db, TradeSignal, Company, PriceHistory, TradeLog

# ── DB session ────────────────────────────────────────────────────────────────
_engine, _Session = init_db(config.database.url)


def _get_db():
    return _Session()


# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(title="NWO Signal Monitor", version="1.0.0")


# ── Data helpers ──────────────────────────────────────────────────────────────

def _latest_signals() -> list[dict]:
    """Return one row per watchlist ticker: latest TradeSignal + latest price."""
    db = _get_db()
    try:
        rows = []
        for ticker in config.watchlist:
            company = db.query(Company).filter(Company.ticker == ticker).first()
            signal_row: Optional[TradeSignal] = None
            latest_price: Optional[float] = None
            price_date: Optional[str] = None

            if company:
                signal_row = (
                    db.query(TradeSignal)
                    .filter(TradeSignal.company_id == company.id)
                    .order_by(TradeSignal.generated_at.desc())
                    .first()
                )
                price_row = (
                    db.query(PriceHistory)
                    .filter(PriceHistory.company_id == company.id)
                    .order_by(PriceHistory.date.desc())
                    .first()
                )
                if price_row:
                    latest_price = price_row.close
                    price_date = price_row.date.strftime("%H:%M") if price_row.date else None

            # Parse reasoning JSON
            reasoning: dict = {}
            if signal_row and signal_row.reasoning:
                try:
                    reasoning = json.loads(signal_row.reasoning)
                except (json.JSONDecodeError, TypeError):
                    reasoning = {}

            # Determine signal label
            if signal_row:
                sig = (signal_row.signal or "WAIT").upper()
                confidence = signal_row.confidence or 0.0
                generated_str = _to_et_str(signal_row.generated_at, "%H:%M:%S ET") if signal_row.generated_at else "—"
                price = signal_row.current_price or latest_price or 0.0
                composite = reasoning.get("composite_score", 0.0)
                momentum_score = reasoning.get("momentum_score", 0.0)
                rvol = reasoning.get("rvol", 0.0)
                is_breakout = reasoning.get("is_52w_breakout", False)
                macd_dir = reasoning.get("macd_signal_direction", "—")
                approved = reasoning.get("approved", False)
                why_buy = reasoning.get("why_buy", "")
                why_wait = reasoning.get("why_wait", "")
                action = reasoning.get("action", "")
            else:
                sig = "NO DATA"
                confidence = 0.0
                generated_str = "—"
                price = latest_price or 0.0
                composite = 0.0
                momentum_score = 0.0
                rvol = 0.0
                is_breakout = False
                macd_dir = "—"
                approved = False
                why_buy = ""
                why_wait = ""
                action = ""

            rows.append({
                "ticker": ticker,
                "is_ai_watch": ticker in config.ai_watch_tickers,
                "signal": sig,
                "confidence": round(confidence * 100, 1),
                "price": price,
                "price_time": price_date or "—",
                "composite": round(composite, 3),
                "momentum_score": round(momentum_score, 3),
                "rvol": round(rvol, 2),
                "is_52w_breakout": is_breakout,
                "macd_direction": macd_dir,
                "approved": approved,
                "generated_at": generated_str,
                "why_buy": why_buy[:120] if why_buy else "",
                "why_wait": why_wait[:120] if why_wait else "",
                "action": action,
            })
        return rows
    finally:
        db.close()


def _trade_log_recent(limit: int = 20) -> list[dict]:
    """Return recent trade log entries."""
    db = _get_db()
    try:
        entries = (
            db.query(TradeLog)
            .order_by(TradeLog.created_at.desc())
            .limit(limit)
            .all()
        )
        result = []
        for e in entries:
            result.append({
                "ticker": e.ticker,
                "action": e.action,
                "quantity": e.quantity,
                "price": e.price_at_execution,
                "total": e.total_value,
                "dry_run": e.dry_run,
                "status": e.status,
                "executed_at": _to_et_str(e.executed_at, "%Y-%m-%d %H:%M:%S ET") if e.executed_at else "—",
            })
        return result
    finally:
        db.close()


# ── API endpoints ─────────────────────────────────────────────────────────────

@app.get("/api/signals")
def api_signals():
    return JSONResponse(content={"signals": _latest_signals(), "dry_run": config.risk.dry_run})


@app.get("/api/trades")
def api_trades():
    return JSONResponse(content={"trades": _trade_log_recent()})


# ── HTML dashboard ────────────────────────────────────────────────────────────

_SIGNAL_COLOR = {
    "BUY": "#00e676",
    "STRONG_BUY": "#00e676",
    "SELL": "#ff5252",
    "HOLD": "#ffd740",
    "WAIT": "#78909c",
    "NO DATA": "#455a64",
}

_SIGNAL_BG = {
    "BUY": "#00251a",
    "STRONG_BUY": "#00251a",
    "SELL": "#2d0000",
    "HOLD": "#2d2600",
    "WAIT": "#1a1f23",
    "NO DATA": "#1a1f23",
}


def _signal_badge(sig: str) -> str:
    color = _SIGNAL_COLOR.get(sig, "#90a4ae")
    return (
        f'<span style="background:{color};color:#000;font-weight:700;'
        f'padding:3px 10px;border-radius:4px;font-size:0.85em;">{sig}</span>'
    )


def _composite_bar(val: float) -> str:
    """Render a small inline bar for composite score (-1 to 1)."""
    pct = min(max((val + 1) / 2 * 100, 0), 100)
    color = "#00e676" if val >= 0.15 else ("#ffd740" if val >= 0 else "#ff5252")
    return (
        f'<div style="display:flex;align-items:center;gap:6px;">'
        f'<div style="width:80px;background:#263238;border-radius:3px;height:8px;">'
        f'<div style="width:{pct:.0f}%;background:{color};height:8px;border-radius:3px;"></div></div>'
        f'<span style="font-size:0.8em;color:{color};">{val:+.3f}</span>'
        f'</div>'
    )


def _build_html(rows: list[dict]) -> str:
    now = datetime.now(_ET).strftime("%Y-%m-%d %H:%M:%S ET")
    dry_run_badge = (
        '<span style="background:#ff6d00;color:#fff;padding:3px 10px;border-radius:4px;'
        'font-size:0.8em;font-weight:700;">DRY RUN</span>'
        if config.risk.dry_run else
        '<span style="background:#d50000;color:#fff;padding:3px 10px;border-radius:4px;'
        'font-size:0.8em;font-weight:700;animation:pulse 1s infinite;">LIVE TRADING</span>'
    )

    # Build signal rows
    signal_rows_html = ""
    for r in rows:
        sig = r["signal"]
        row_bg = _SIGNAL_BG.get(sig, "#1a1f23")
        ai_badge = (
            ' <span style="background:#7c4dff;color:#fff;font-size:0.7em;'
            'padding:2px 6px;border-radius:3px;vertical-align:middle;">AI WATCH</span>'
            if r["is_ai_watch"] else ""
        )
        breakout_badge = (
            ' <span style="background:#ff6d00;color:#fff;font-size:0.7em;'
            'padding:2px 5px;border-radius:3px;">52W↑</span>'
            if r["is_52w_breakout"] else ""
        )
        approved_badge = (
            ' <span style="background:#00c853;color:#000;font-size:0.7em;'
            'padding:2px 5px;border-radius:3px;font-weight:700;">APPROVED</span>'
            if r["approved"] else ""
        )
        price_str = f"${r['price']:.2f}" if r["price"] else "—"
        rvol_color = "#00e676" if r["rvol"] >= 1.5 else ("#ffd740" if r["rvol"] >= 1.0 else "#90a4ae")
        macd_color = "#00e676" if r["macd_direction"] == "bullish" else ("#ff5252" if r["macd_direction"] == "bearish" else "#90a4ae")
        tooltip = r["why_buy"] or r["why_wait"] or r["action"] or "No signal data"

        signal_rows_html += f"""
        <tr style="background:{row_bg};border-bottom:1px solid #263238;" title="{tooltip}">
          <td style="padding:10px 14px;font-weight:700;font-size:1em;">{r['ticker']}{ai_badge}</td>
          <td style="padding:10px 14px;">{_signal_badge(sig)}{approved_badge}</td>
          <td style="padding:10px 14px;color:#cfd8dc;">{r['confidence']:.1f}%</td>
          <td style="padding:10px 14px;color:#eceff1;">{price_str} <span style="color:#546e7a;font-size:0.75em;">{r['price_time']}</span></td>
          <td style="padding:10px 14px;">{_composite_bar(r['composite'])}</td>
          <td style="padding:10px 14px;color:#b0bec5;font-size:0.85em;">{r['momentum_score']:+.3f}</td>
          <td style="padding:10px 14px;color:{rvol_color};font-size:0.9em;font-weight:600;">{r['rvol']:.2f}×{breakout_badge}</td>
          <td style="padding:10px 14px;color:{macd_color};font-size:0.85em;">{r['macd_direction']}</td>
          <td style="padding:10px 14px;color:#546e7a;font-size:0.8em;">{r['generated_at']}</td>
          <td style="padding:6px 10px;"><a href="/charts?ticker={r['ticker']}" target="_blank" style="font-size:11px;padding:2px 6px;border-radius:4px;border:1px solid #30363d;background:#0d1117;color:#8b949e;text-decoration:none;" title="Open chart">&#128200;</a></td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>NWO Signal Monitor</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: #0d1117; color: #cfd8dc; font-family: 'Segoe UI', monospace; }}
    header {{ background: #161b22; padding: 14px 24px; border-bottom: 1px solid #263238;
              display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }}
    .back-btn {{ padding: 5px 12px; border-radius: 6px; border: 1px solid #30363d;
                 background: #21262d; color: #8b949e; text-decoration: none; font-size: 12px; white-space: nowrap; }}
    .back-btn:hover {{ background: #30363d; }}
    h1 {{ font-size: 1.3em; color: #eceff1; letter-spacing: 2px; }}
    .info-btn {{ padding: 4px 10px; border-radius: 6px; border: 1px solid #30363d;
                 background: transparent; color: #58a6ff; cursor: pointer; font-size: 13px; }}
    .info-btn:hover {{ background: rgba(88,166,255,0.1); }}
    .meta {{ margin-left: auto; color: #546e7a; font-size: 0.8em; }}
    .container {{ padding: 24px; }}
    .section-title {{ color: #78909c; font-size: 0.75em; letter-spacing: 2px;
                      text-transform: uppercase; margin-bottom: 10px; margin-top: 24px; }}
    table {{ width: 100%; border-collapse: collapse; background: #161b22;
             border-radius: 8px; overflow: hidden; }}
    th {{ padding: 10px 14px; text-align: left; color: #546e7a; font-size: 0.75em;
          letter-spacing: 1px; text-transform: uppercase; background: #1c2430;
          border-bottom: 2px solid #263238; }}
    tr:hover {{ filter: brightness(1.15); cursor: default; }}
    .refresh-bar {{ color: #546e7a; font-size: 0.75em; margin-top: 6px; }}
    #countdown {{ color: #78909c; }}
    @keyframes pulse {{ 0%,100% {{ opacity:1; }} 50% {{ opacity:0.5; }} }}
    /* Info modal */
    .info-overlay {{ position: fixed; inset: 0; background: rgba(0,0,0,0.8);
                     display: none; align-items: center; justify-content: center; z-index: 9999; }}
    .info-overlay.open {{ display: flex; }}
    .info-modal {{ background: #161b22; border: 1px solid #30363d; border-radius: 10px;
                   padding: 24px; max-width: 560px; width: 90%; max-height: 85vh; overflow-y: auto; }}
    .info-modal h2 {{ font-size: 15px; color: #58a6ff; margin-bottom: 16px; }}
    .info-row {{ display: flex; gap: 10px; margin-bottom: 12px; align-items: flex-start; }}
    .info-label {{ font-size: 11px; font-weight: 700; color: #e6edf3; min-width: 120px;
                   background: #21262d; padding: 3px 8px; border-radius: 4px; flex-shrink: 0; }}
    .info-desc {{ font-size: 12px; color: #8b949e; line-height: 1.5; }}
    .info-close {{ float: right; background: none; border: 1px solid #30363d;
                   color: #8b949e; cursor: pointer; padding: 4px 10px; border-radius: 4px; font-size: 12px; }}
    .info-close:hover {{ color: #f85149; }}
    /* ── Shared nav buttons ── */
    .brief-btn {{ display: flex; flex-direction: column; gap: 2px; padding: 6px 14px;
                 border-radius: 6px; border: 1px solid #30363d; background: #161b22;
                 text-decoration: none; color: #e6edf3; transition: background 0.15s; }}
    .brief-btn:hover {{ background: #1c2e50; }}
    .brief-btn-title {{ font-size: 12px; font-weight: 700; letter-spacing: 0.5px; }}
    .brief-btn-preview {{ font-size: 10px; color: #8b949e; white-space: nowrap; }}
    /* ── Sticky banner ── */
    .sticky-banner {{ position: sticky; top: 0; z-index: 200; background: #0d1117; }}
    /* ── Shared ticker tape ── */
    .sh-tape-wrap  {{ overflow: hidden; background: #0a0f17; border-bottom: 1px solid #1f6feb; height: 26px; }}
    .sh-tape-track {{ display: flex; gap: 24px; white-space: nowrap; will-change: transform;
                      animation: sh-tape 80s linear infinite; align-items: center; height: 100%; padding-left: 12px; }}
    .sh-tape-track:hover {{ animation-play-state: paused; }}
    @keyframes sh-tape {{ 0%{{transform:translateX(0)}} 100%{{transform:translateX(-50%)}} }}
    .sht-bull    {{ color: #3fb950; font-size: 12px; font-weight: 700; }}
    .sht-bear    {{ color: #f85149; font-size: 12px; font-weight: 700; }}
    .sht-neu     {{ color: #8b949e; font-size: 12px; }}
    .sht-chg-up  {{ color: rgba(63,185,80,0.75); font-size: 12px; }}
    .sht-chg-dn  {{ color: rgba(248,81,73,0.75); font-size: 12px; }}
    .sht-sep     {{ color: #30363d; font-size: 10px; }}
    /* ── Page info bar ── */
    .page-info-bar {{ border-bottom: 1px solid #21262d; background: #0a0f17; }}
    .page-info-toggle {{ width:100%;text-align:left;padding:4px 16px;background:none;border:none;
                         color:#8b949e;cursor:pointer;font-size:11px;display:flex;align-items:center;gap:6px; }}
    .page-info-toggle:hover {{ background:rgba(88,166,255,0.05); }}
    .page-info-content {{ padding:6px 16px 10px;font-size:12px;color:#8b949e;line-height:1.75; }}
    .page-info-bar.pib-collapsed .page-info-content {{ display:none; }}
    .pib-arrow {{ display:inline-block;transition:transform 0.2s;font-size:10px;margin-left:auto; }}
    .page-info-bar.pib-collapsed .pib-arrow {{ transform:rotate(-90deg); }}
  </style>
</head>
<body>
  <!-- Info modal -->
  <div class="info-overlay" id="info-overlay" onclick="if(event.target===this) closeInfo()">
    <div class="info-modal">
      <button class="info-close" onclick="closeInfo()">&#x2715; Close</button>
      <h2>&#9432; Signal Monitor — Column Guide</h2>
      <div class="info-row">
        <span class="info-label">Signal</span>
        <span class="info-desc">Aggregated decision after all 7 gates: BUY / STRONG_BUY / HOLD / WAIT / SELL. Derives from L3 FUD filter output.</span>
      </div>
      <div class="info-row">
        <span class="info-label">Confidence</span>
        <span class="info-desc">Ensemble model probability (0–100%). Gate threshold: &gt;50% required for a BUY to proceed to execution.</span>
      </div>
      <div class="info-row">
        <span class="info-label">Composite</span>
        <span class="info-desc">Weighted signal score (–1 to +1). Weights: Fundamentals 25% · Momentum 20% · Insider 20% · Technical 15% · Cycle 10% · Volume 10%. Threshold for BUY: ≥+0.15.</span>
      </div>
      <div class="info-row">
        <span class="info-label">Momentum</span>
        <span class="info-desc">Momentum sub-score from MACD crossover, MA stack (20/50/200), ATR expansion, and RVOL. Feeds 20% of composite.</span>
      </div>
      <div class="info-row">
        <span class="info-label">RVOL</span>
        <span class="info-desc">Relative Volume vs 20-day average. Green ≥1.5× = unusual buying activity. AI Watch breakout override triggers at ≥1.5×.</span>
      </div>
      <div class="info-row">
        <span class="info-label">MACD</span>
        <span class="info-desc">MACD signal line crossover direction: bullish = MACD crossed above signal line; bearish = crossed below.</span>
      </div>
      <div class="info-row">
        <span class="info-label">52W ↑</span>
        <span class="info-desc">Price is within 2% of its 52-week high — potential breakout setup.</span>
      </div>
      <div class="info-row">
        <span class="info-label">APPROVED badge</span>
        <span class="info-desc">All 7 decision gates passed: Reynolds turbulence, Ensemble probability, Quantum state, Kalman filter, Risk/reward, FUD quality, Signal threshold. Ready for execution.</span>
      </div>
      <div class="info-row">
        <span class="info-label">AI WATCH badge</span>
        <span class="info-desc">Priority ticker (e.g. TSLA) scanned every 1 minute. On confirmed breakout (RVOL ≥1.5×), fundamentals floored at 0 and Reynolds + Kalman gates relaxed.</span>
      </div>
    </div>
  </div>

<div class="sticky-banner">
  <header>
    <a href="/" class="brief-btn" style="flex-direction:row;align-items:center;gap:4px;padding:5px 10px">&#8592; NWO</a>
    <a href="/morning-brief" class="brief-btn"><span class="brief-btn-title">&#128202; Morning Brief</span><span class="brief-btn-preview">Markets &middot; Futures</span></a>
    <a href="/paper"         class="brief-btn"><span class="brief-btn-title">&#127918; Paper Trade</span><span class="brief-btn-preview">$100k &middot; 3-Stage AI</span></a>
    <a href="/live-trading"  class="brief-btn"><span class="brief-btn-title">&#128185; Live Trading</span><span class="brief-btn-preview">Coming Soon</span></a>
    <a href="/wheel"         class="brief-btn"><span class="brief-btn-title">&#127905; Wheel</span><span class="brief-btn-preview">CSP &middot; Covered Call</span></a>
    <a href="/i-tool"        class="brief-btn"><span class="brief-btn-title">&#128225; I-Tool</span><span class="brief-btn-preview">S&amp;P 500 Scanner</span></a>
    <a href="/charts"        class="brief-btn"><span class="brief-btn-title">&#128200; Charts</span><span class="brief-btn-preview">Candles &middot; EMA &middot; Fibonacci</span></a>
    <a href="/signals"       class="brief-btn" id="signals-btn" style="border-color:#58a6ff;background:rgba(88,166,255,0.1)"><span class="brief-btn-title">&#128200; Signal Monitor</span><span class="brief-btn-preview" style="color:#58a6ff">Active page</span></a>
    <h1 style="margin-left:8px">&#128200; NWO SIGNAL MONITOR</h1>
    {dry_run_badge}
    <button class="info-btn" onclick="document.getElementById('info-overlay').classList.add('open')" title="Column guide">&#9432; How to read this</button>
    <div class="meta">Last updated: {now}</div>
  </header>
  <div class="sh-tape-wrap"><div class="sh-tape-track" id="sh-tape"><span class="sht-neu">Loading signals&#8230;</span></div></div>
</div>
<div class="page-info-bar pib-collapsed" id="page-info-bar">
  <button class="page-info-toggle" onclick="var b=this.closest('.page-info-bar');b.classList.toggle('pib-collapsed')">ℹ️ About this page<span class="pib-arrow">&#9660;</span></button>
  <div class="page-info-content"><b>Signal Monitor</b> — Live per-ticker AI gate breakdown: Reynolds, Quantum, Ensemble, R/R, Kalman, momentum, RVOL. One row per ticker — current state only. Distinct from <i>AI Signal History</i> on the main page (that is a chronological event log; this shows current state).</div>
</div>

  <div class="container">
    <div class="section-title">Signal Monitor — {len(rows)} tickers</div>
    <table>
      <thead>
        <tr>
          <th>Ticker</th>
          <th>Signal</th>
          <th>Confidence</th>
          <th>Price</th>
          <th>Composite &#9432;</th>
          <th>Momentum</th>
          <th>RVOL</th>
          <th>MACD</th>
          <th>Updated</th>
          <th>Chart</th>
        </tr>
      </thead>
      <tbody>
        {signal_rows_html}
      </tbody>
    </table>
    <div class="refresh-bar">Auto-refresh in <span id="countdown">30</span>s &nbsp;|&nbsp;
      <a href="/api/signals" style="color:#546e7a;">JSON API</a>
    </div>
  </div>

  <script>
    function closeInfo() {{
      document.getElementById('info-overlay').classList.remove('open');
    }}
    document.addEventListener('keydown', e => {{ if (e.key === 'Escape') closeInfo(); }});

    let t = 30;
    const el = document.getElementById('countdown');
    setInterval(() => {{
      t--;
      el.textContent = t;
      if (t <= 0) {{ window.location.reload(); }}
    }}, 1000);

    // Shared ticker tape with dedup + change_pct arrows
    (async function() {{
      const track = document.getElementById('sh-tape');
      if (!track) return;
      try {{
        const sigs = await fetch('/api/signals').then(r => r.json());
        const byT = new Map();
        (sigs || []).forEach(s => {{ if (!byT.has(s.ticker)) byT.set(s.ticker, s); }});
        const items = [...byT.values()];
        if (!items.length) {{ track.innerHTML = '<span class="sht-neu">No signals</span>'; return; }}
        const all = [...items, ...items];
        track.innerHTML = all.map(s => {{
          const sig = (s.signal || '').toUpperCase();
          const bull = sig === 'BUY' || sig === 'STRONG_BUY';
          const bear = sig === 'SELL' || sig === 'STRONG_SELL';
          const chg  = s.change_pct;
          const up   = chg != null ? chg > 0 : null;
          let cls, arr;
          if (bull)             {{ cls = 'sht-bull';   arr = '&#9650;'; }}
          else if (bear)        {{ cls = 'sht-bear';   arr = '&#9660;'; }}
          else if (up === true) {{ cls = 'sht-chg-up'; arr = '&#9650;'; }}
          else if (up === false){{ cls = 'sht-chg-dn'; arr = '&#9660;'; }}
          else                  {{ cls = 'sht-neu';    arr = '&#8212;'; }}
          const p = s.current_price;
          return '<span class="' + cls + '">' + arr + ' ' + s.ticker + (p ? ' $' + Number(p).toFixed(2) : '') + '</span>'
               + '<span class="sht-sep">|</span>';
        }}).join('');
        track.style.animationDuration = Math.max(40, items.length * 0.8) + 's';
      }} catch(e) {{}}
    }})();
  </script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def dashboard():
    rows = _latest_signals()
    return HTMLResponse(content=_build_html(rows))
