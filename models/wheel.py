"""
monitor/wheel.py
────────────────────────────────────────────────────────────────────────────
Wheel Strategy Module for NWO Monitor
Served as a standalone FastAPI HTMLResponse page at /wheel

To wire into dashboard.py:
  1.  from monitor.wheel import wheel_router
  2.  app.include_router(wheel_router)
  3.  Add nav button in dashboard.py HTML:
        <a href="/wheel" class="brief-btn" id="wheel-btn">🎡 Wheel</a>
────────────────────────────────────────────────────────────────────────────
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse
import time, threading
from typing import Optional

# ── lazy import so module loads even if broker isn't on path yet ──────────
try:
    from broker.market_data import SchwabMarketData
    _md = SchwabMarketData()
except Exception:
    _md = None

wheel_router = APIRouter()

# ─────────────────────────────────────────────────────────────────────────────
# S&P 500 CANDIDATE UNIVERSE  (top 100 by liquidity/name recognition)
# Replace or expand as desired — full 500 list can be loaded from a CSV
# ─────────────────────────────────────────────────────────────────────────────
SP500_UNIVERSE = [
    "AAPL","MSFT","AMZN","GOOGL","META","NVDA","TSLA","BRK.B","JPM","V",
    "UNH","XOM","LLY","JNJ","MA","PG","HD","MRK","AVGO","CVX",
    "ABBV","COST","PEP","KO","WMT","BAC","CRM","TMO","ACN","MCD",
    "ABT","CSCO","NKE","DHR","ADBE","TXN","NEE","PM","LIN","RTX",
    "BMY","AMGN","QCOM","HON","UPS","IBM","GE","CAT","SBUX","GS",
    "MS","BLK","SPGI","AXP","ISRG","PLD","DE","AMD","NOW","INTC",
    "GILD","ZTS","SYK","CI","MDLZ","ADI","REGN","MO","MMC","TJX",
    "EOG","SLB","DUK","SO","D","EXC","PCG","AEP","F","GM",
    "WFC","USB","PNC","TFC","COF","DIS","NFLX","CMCSA","T","VZ",
    "COP","OXY","MPC","VLO","PSX","FCX","NEM","AA","CLF","X",
]

# ─────────────────────────────────────────────────────────────────────────────
# IN-MEMORY STATE
# ─────────────────────────────────────────────────────────────────────────────
_scan_results   = []          # list of screened candidate dicts
_wheel_positions = []         # active wheel positions
_completed_cycles = []        # completed wheel cycles
_scan_running   = False
_scan_ts        = None

# ─────────────────────────────────────────────────────────────────────────────
# SCREENING LOGIC
# ─────────────────────────────────────────────────────────────────────────────

def _screen_candidate(symbol: str) -> Optional[dict]:
    """
    Returns candidate dict if symbol passes wheel criteria, else None.
    Criteria:
      - IV Rank > 50%
      - Has liquid puts near 30 delta, 30-45 DTE
      - Bid/ask spread on target put < 5% of mid (liquidity gate)
    """
    if _md is None:
        return None
    try:
        iv_rank = _md.get_iv_rank(symbol)
        if iv_rank is None or iv_rank < 50:
            return None

        chain = _md.get_options_chain(symbol, days_to_expiry=45)
        if not chain:
            return None

        underlying = chain["underlyingPrice"]
        if underlying <= 0:
            return None

        # Find best put: 30-45 DTE, delta closest to -0.30
        candidates = [
            p for p in chain["puts"]
            if 25 <= p["dte"] <= 50
            and p["bid"] > 0
            and p["ask"] > 0
            and abs(p["delta"]) <= 0.40   # allow up to 40 delta
        ]
        if not candidates:
            return None

        # Score by closeness to -0.30 delta
        target = sorted(candidates, key=lambda p: abs(abs(p["delta"]) - 0.30))
        best = target[0]

        # Liquidity gate: spread < 5% of mid
        spread_pct = (best["ask"] - best["bid"]) / best["mid"] if best["mid"] > 0 else 99
        if spread_pct > 0.05:
            return None

        # Annualised premium yield estimate
        premium_yield = round((best["mid"] / underlying) * (365 / best["dte"]) * 100, 1)

        return {
            "symbol":        symbol,
            "price":         underlying,
            "ivRank":        iv_rank,
            "iv":            chain["volatility"],
            "strike":        best["strike"],
            "expiry":        best["expiry"],
            "dte":           best["dte"],
            "delta":         round(best["delta"], 2),
            "bid":           best["bid"],
            "ask":           best["ask"],
            "mid":           best["mid"],
            "spreadPct":     round(spread_pct * 100, 1),
            "premiumYield":  premium_yield,
            "oi":            best["openInterest"],
            "scannedAt":     time.strftime("%H:%M:%S"),
        }
    except Exception as e:
        print(f"[wheel] screen_candidate({symbol}) error: {e}")
        return None


def _run_scan():
    global _scan_results, _scan_running, _scan_ts
    _scan_running = True
    results = []
    for sym in SP500_UNIVERSE:
        c = _screen_candidate(sym)
        if c:
            results.append(c)
        time.sleep(0.15)   # gentle rate limiting
    # Sort by IV rank descending
    _scan_results = sorted(results, key=lambda x: x["ivRank"], reverse=True)
    _scan_running = False
    _scan_ts = time.strftime("%H:%M:%S")


# ─────────────────────────────────────────────────────────────────────────────
# PAPER POSITION HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _open_position(data: dict):
    pos = {
        "id":           int(time.time() * 1000),
        "symbol":       data["symbol"],
        "phase":        "CSP",           # Cash Secured Put
        "strike":       data["strike"],
        "expiry":       data["expiry"],
        "dte":          data["dte"],
        "contracts":    data.get("contracts", 1),
        "premium":      round(data["mid"] * 100 * data.get("contracts", 1), 2),
        "costBasis":    None,            # set when assigned
        "openedAt":     time.strftime("%Y-%m-%d %H:%M"),
        "closedAt":     None,
        "status":       "open",
        "notes":        "",
        "pnl":          None,
    }
    _wheel_positions.append(pos)
    return pos


def _assign_position(pos_id: int):
    for p in _wheel_positions:
        if p["id"] == pos_id and p["phase"] == "CSP":
            p["phase"]    = "SHARES"
            p["costBasis"] = round(p["strike"] - p["premium"] / 100 / p["contracts"], 2)
            p["notes"]    = f"Assigned at {p['strike']}. Net cost basis: {p['costBasis']}"
            return p
    return None


def _sell_covered_call(pos_id: int, data: dict):
    for p in _wheel_positions:
        if p["id"] == pos_id and p["phase"] == "SHARES":
            p["phase"]   = "CC"
            p["strike"]  = data["strike"]
            p["expiry"]  = data["expiry"]
            p["dte"]     = data["dte"]
            call_premium = round(data["mid"] * 100 * p["contracts"], 2)
            p["premium"] += call_premium
            p["notes"]   = f"CC sold at {data['strike']} exp {data['expiry']}. Total premium: {p['premium']}"
            return p
    return None


def _close_position(pos_id: int, called_away: bool = True):
    for i, p in enumerate(_wheel_positions):
        if p["id"] == pos_id:
            p["status"]   = "closed"
            p["closedAt"] = time.strftime("%Y-%m-%d %H:%M")
            if called_away and p["costBasis"]:
                p["pnl"] = round((p["strike"] - p["costBasis"]) * 100 * p["contracts"] + p["premium"], 2)
            else:
                p["pnl"] = round(p["premium"], 2)
            _completed_cycles.append(p)
            _wheel_positions.pop(i)
            return p
    return None


# ─────────────────────────────────────────────────────────────────────────────
# API ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@wheel_router.get("/wheel", response_class=HTMLResponse)
async def wheel_page():
    return HTMLResponse(content=WHEEL_HTML + "<script>" + WHEEL_JS + "</script>")


@wheel_router.post("/api/wheel/scan")
async def start_scan():
    global _scan_running
    if _scan_running:
        return JSONResponse({"status": "already_running"})
    t = threading.Thread(target=_run_scan, daemon=True)
    t.start()
    return JSONResponse({"status": "started"})


@wheel_router.get("/api/wheel/scan-results")
async def get_scan_results():
    return JSONResponse({
        "running":   _scan_running,
        "scannedAt": _scan_ts,
        "results":   _scan_results,
    })


@wheel_router.post("/api/wheel/open")
async def open_position(data: dict):
    pos = _open_position(data)
    return JSONResponse({"status": "ok", "position": pos})


@wheel_router.post("/api/wheel/assign/{pos_id}")
async def assign_position(pos_id: int):
    p = _assign_position(pos_id)
    return JSONResponse({"status": "ok" if p else "not_found", "position": p})


@wheel_router.post("/api/wheel/sell-cc/{pos_id}")
async def sell_cc(pos_id: int, data: dict):
    p = _sell_covered_call(pos_id, data)
    return JSONResponse({"status": "ok" if p else "not_found", "position": p})


@wheel_router.post("/api/wheel/close/{pos_id}")
async def close_position(pos_id: int, called_away: bool = True):
    p = _close_position(pos_id, called_away)
    return JSONResponse({"status": "ok" if p else "not_found", "position": p})


@wheel_router.get("/api/wheel/positions")
async def get_positions():
    total_premium = sum(p["premium"] for p in _wheel_positions) + \
                    sum(p["premium"] for p in _completed_cycles)
    realized_pnl  = sum(p["pnl"] for p in _completed_cycles if p["pnl"] is not None)
    return JSONResponse({
        "active":        _wheel_positions,
        "completed":     _completed_cycles,
        "totalPremium":  round(total_premium, 2),
        "realizedPnl":   round(realized_pnl, 2),
    })


# ─────────────────────────────────────────────────────────────────────────────
# HTML  (dark theme matching NWO Monitor)
# ─────────────────────────────────────────────────────────────────────────────

WHEEL_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Wheel Strategy — NWO</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d1117; color: #e6edf3; font-family: 'Segoe UI', monospace; font-size: 14px; }

  /* ── Header ── */
  header { background: #161b22; padding: 12px 20px; border-bottom: 1px solid #30363d;
           display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .back-btn { padding: 5px 12px; border-radius: 6px; border: 1px solid #30363d;
              background: #21262d; color: #8b949e; text-decoration: none; font-size: 12px; }
  header h1 { font-size: 17px; letter-spacing: 1px; color: #f0883e; }
  .header-right { margin-left: auto; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }

  /* ── Buttons ── */
  .scan-btn  { padding: 6px 16px; border-radius: 6px; border: 1px solid #3fb950;
               background: #1a4731; color: #3fb950; cursor: pointer; font-size: 12px; font-weight: 600; }
  .scan-btn:hover  { background: #1e5c3a; }
  .scan-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .action-btn { padding: 4px 10px; border-radius: 5px; border: 1px solid #58a6ff;
                background: transparent; color: #58a6ff; cursor: pointer; font-size: 11px; }
  .action-btn:hover { background: rgba(88,166,255,0.1); }
  .danger-btn { border-color: #f85149; color: #f85149; }
  .danger-btn:hover { background: rgba(248,81,73,0.1); }

  /* ── Summary cards ── */
  main { padding: 16px; display: grid; gap: 16px; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px; }
  .card-label { font-size: 10px; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 6px; }
  .card-value { font-size: 22px; font-weight: 700; color: #e6edf3; }
  .card-value.green { color: #3fb950; }
  .card-value.orange { color: #f0883e; }

  /* ── Panels ── */
  .panel { background: #161b22; border: 1px solid #30363d; border-radius: 8px; overflow: hidden; }
  .panel-header { padding: 10px 16px; border-bottom: 1px solid #30363d;
                  display: flex; align-items: center; justify-content: space-between; }
  .panel-title { font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; font-weight: 600; }
  .panel-badge { font-size: 10px; background: #21262d; border: 1px solid #30363d;
                 border-radius: 10px; padding: 2px 8px; color: #8b949e; }

  /* ── Tables ── */
  .tbl-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th { padding: 8px 12px; text-align: left; color: #8b949e; font-weight: 500;
       border-bottom: 1px solid #30363d; white-space: nowrap; }
  td { padding: 8px 12px; border-bottom: 1px solid #21262d; white-space: nowrap; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: #1c2128; }
  .sym { font-weight: 700; color: #e6edf3; }
  .green { color: #3fb950; }
  .red   { color: #f85149; }
  .orange { color: #f0883e; }
  .blue  { color: #58a6ff; }
  .muted { color: #8b949e; }
  .phase-csp    { background: #1a3a5c; color: #58a6ff; border-radius: 4px; padding: 2px 6px; font-size: 10px; }
  .phase-shares { background: #2d1f00; color: #f0883e; border-radius: 4px; padding: 2px 6px; font-size: 10px; }
  .phase-cc     { background: #1a4731; color: #3fb950; border-radius: 4px; padding: 2px 6px; font-size: 10px; }

  /* ── Scan status ── */
  #scan-status { font-size: 11px; color: #d29922; }
  .spinner { display: inline-block; animation: spin 1s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* ── Empty state ── */
  .empty { padding: 32px; text-align: center; color: #8b949e; font-size: 12px; }
</style>
</head>
<body>
<header>
  <a href="/" class="back-btn">← Back</a>
  <h1>🎡 WHEEL STRATEGY</h1>
  <div class="header-right">
    <span id="scan-status"></span>
    <button class="scan-btn" id="scan-btn" onclick="startScan()">🔍 Scan S&amp;P 500</button>
  </div>
</header>

<main>
  <!-- Summary Cards -->
  <div class="cards">
    <div class="card">
      <div class="card-label">Active Positions</div>
      <div class="card-value orange" id="card-active">0</div>
    </div>
    <div class="card">
      <div class="card-label">Total Premium Collected</div>
      <div class="card-value green" id="card-premium">$0</div>
    </div>
    <div class="card">
      <div class="card-label">Realized P&amp;L</div>
      <div class="card-value green" id="card-pnl">$0</div>
    </div>
    <div class="card">
      <div class="card-label">Completed Cycles</div>
      <div class="card-value blue" id="card-cycles">0</div>
    </div>
    <div class="card">
      <div class="card-label">Candidates Found</div>
      <div class="card-value" id="card-candidates">—</div>
    </div>
  </div>

  <!-- Candidate Scanner Results -->
  <div class="panel">
    <div class="panel-header">
      <span class="panel-title">📡 Screened Candidates  <span class="muted" style="font-size:10px;">(IV Rank &gt;50% · 30Δ CSP · 30-45 DTE)</span></span>
      <span class="panel-badge" id="scan-badge">Not scanned</span>
    </div>
    <div class="tbl-wrap">
      <table id="candidates-table">
        <thead>
          <tr>
            <th>Ticker</th><th>Price</th><th>IV Rank</th><th>IV %</th>
            <th>Strike</th><th>Expiry</th><th>DTE</th><th>Delta</th>
            <th>Bid</th><th>Ask</th><th>Mid</th><th>Spread</th>
            <th>Ann.Yield</th><th>OI</th><th>Action</th>
          </tr>
        </thead>
        <tbody id="candidates-body">
          <tr><td colspan="15" class="empty">Run a scan to find wheel candidates</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- Active Wheel Positions -->
  <div class="panel">
    <div class="panel-header">
      <span class="panel-title">🔄 Active Wheel Positions</span>
      <span class="panel-badge" id="positions-badge">0 open</span>
    </div>
    <div class="tbl-wrap">
      <table>
        <thead>
          <tr>
            <th>Ticker</th><th>Phase</th><th>Strike</th><th>Expiry</th>
            <th>DTE</th><th>Contracts</th><th>Premium</th><th>Cost Basis</th>
            <th>Opened</th><th>Notes</th><th>Actions</th>
          </tr>
        </thead>
        <tbody id="positions-body">
          <tr><td colspan="11" class="empty">No active positions</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- Completed Cycles -->
  <div class="panel">
    <div class="panel-header">
      <span class="panel-title">✅ Completed Cycles</span>
      <span class="panel-badge" id="cycles-badge">0 completed</span>
    </div>
    <div class="tbl-wrap">
      <table>
        <thead>
          <tr>
            <th>Ticker</th><th>Strike</th><th>Opened</th><th>Closed</th>
            <th>Contracts</th><th>Premium</th><th>Realized P&amp;L</th>
          </tr>
        </thead>
        <tbody id="cycles-body">
          <tr><td colspan="7" class="empty">No completed cycles yet</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</main>
</body>
'''

# ─────────────────────────────────────────────────────────────────────────────
# JAVASCRIPT
# ─────────────────────────────────────────────────────────────────────────────

WHEEL_JS = r'''
// ── Poll state ───────────────────────────────────────────────────────────────
let scanPollTimer = null;

function fmt$(n){ return n == null ? '—' : '$' + Number(n).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}); }
function fmtPct(n){ return n == null ? '—' : n.toFixed(1) + '%'; }
function fmtD(n){ return n == null ? '—' : n.toFixed(2); }

// ── Scan ─────────────────────────────────────────────────────────────────────
async function startScan(){
  const btn = document.getElementById('scan-btn');
  btn.disabled = true;
  document.getElementById('scan-status').innerHTML = '<span class="spinner">⟳</span> Scanning S&P 500…';
  await fetch('/api/wheel/scan', {method:'POST'});
  scanPollTimer = setInterval(pollScan, 3000);
}

async function pollScan(){
  const r = await fetch('/api/wheel/scan-results');
  const d = await r.json();
  renderCandidates(d.results);
  document.getElementById('card-candidates').textContent = d.results.length;
  if(!d.running){
    clearInterval(scanPollTimer);
    document.getElementById('scan-btn').disabled = false;
    document.getElementById('scan-status').textContent = d.scannedAt ? '✓ Scanned at ' + d.scannedAt : '';
    document.getElementById('scan-badge').textContent = d.results.length + ' candidates';
  }
}

function renderCandidates(rows){
  const tb = document.getElementById('candidates-body');
  if(!rows || !rows.length){
    tb.innerHTML = '<tr><td colspan="15" class="empty">No candidates passed the filter</td></tr>';
    return;
  }
  tb.innerHTML = rows.map(c => `
    <tr>
      <td class="sym">${c.symbol}</td>
      <td>${fmt$(c.price)}</td>
      <td class="${c.ivRank>=70?'orange':c.ivRank>=50?'green':''}">${fmtPct(c.ivRank)}</td>
      <td>${fmtPct(c.iv)}</td>
      <td class="blue">${fmt$(c.strike)}</td>
      <td>${c.expiry}</td>
      <td>${c.dte}</td>
      <td>${fmtD(c.delta)}</td>
      <td>${fmt$(c.bid)}</td>
      <td>${fmt$(c.ask)}</td>
      <td class="green">${fmt$(c.mid)}</td>
      <td class="${c.spreadPct>3?'red':'muted'}">${c.spreadPct}%</td>
      <td class="${c.premiumYield>=20?'orange':c.premiumYield>=10?'green':''}">${fmtPct(c.premiumYield)}</td>
      <td class="muted">${c.oi.toLocaleString()}</td>
      <td><button class="action-btn" onclick="openPosition(${JSON.stringify(c).replace(/"/g,'&quot;')})">+ Open CSP</button></td>
    </tr>
  `).join('');
}

// ── Positions ─────────────────────────────────────────────────────────────────
async function loadPositions(){
  const r = await fetch('/api/wheel/positions');
  const d = await r.json();
  document.getElementById('card-active').textContent   = d.active.length;
  document.getElementById('card-premium').textContent  = fmt$(d.totalPremium);
  document.getElementById('card-pnl').textContent      = fmt$(d.realizedPnl);
  document.getElementById('card-cycles').textContent   = d.completed.length;
  document.getElementById('positions-badge').textContent = d.active.length + ' open';
  document.getElementById('cycles-badge').textContent    = d.completed.length + ' completed';
  renderPositions(d.active);
  renderCycles(d.completed);
}

function renderPositions(rows){
  const tb = document.getElementById('positions-body');
  if(!rows.length){
    tb.innerHTML = '<tr><td colspan="11" class="empty">No active positions</td></tr>';
    return;
  }
  tb.innerHTML = rows.map(p => {
    const phaseTag = p.phase==='CSP'
      ? `<span class="phase-csp">CSP</span>`
      : p.phase==='SHARES'
      ? `<span class="phase-shares">SHARES</span>`
      : `<span class="phase-cc">CC</span>`;

    const actions = p.phase==='CSP'
      ? `<button class="action-btn" onclick="assignPos(${p.id})">Assign</button>
         <button class="action-btn danger-btn" onclick="closePos(${p.id},false)">Expire</button>`
      : p.phase==='SHARES'
      ? `<button class="action-btn" onclick="sellCC(${p.id})">Sell CC</button>`
      : `<button class="action-btn" onclick="closePos(${p.id},true)">Called Away</button>
         <button class="action-btn danger-btn" onclick="closePos(${p.id},false)">Expire</button>`;

    return `<tr>
      <td class="sym">${p.symbol}</td>
      <td>${phaseTag}</td>
      <td class="blue">${fmt$(p.strike)}</td>
      <td>${p.expiry}</td>
      <td>${p.dte}</td>
      <td>${p.contracts}</td>
      <td class="green">${fmt$(p.premium)}</td>
      <td class="${p.costBasis?'orange':'muted'}">${p.costBasis?fmt$(p.costBasis):'—'}</td>
      <td class="muted">${p.openedAt}</td>
      <td class="muted" style="font-size:10px;max-width:200px;overflow:hidden;text-overflow:ellipsis">${p.notes||''}</td>
      <td style="display:flex;gap:4px;flex-wrap:wrap">${actions}</td>
    </tr>`;
  }).join('');
}

function renderCycles(rows){
  const tb = document.getElementById('cycles-body');
  if(!rows.length){
    tb.innerHTML = '<tr><td colspan="7" class="empty">No completed cycles yet</td></tr>';
    return;
  }
  tb.innerHTML = rows.map(p => `
    <tr>
      <td class="sym">${p.symbol}</td>
      <td class="blue">${fmt$(p.strike)}</td>
      <td class="muted">${p.openedAt}</td>
      <td class="muted">${p.closedAt||'—'}</td>
      <td>${p.contracts}</td>
      <td class="green">${fmt$(p.premium)}</td>
      <td class="${p.pnl>=0?'green':'red'}">${fmt$(p.pnl)}</td>
    </tr>
  `).join('');
}

// ── Position actions ──────────────────────────────────────────────────────────
async function openPosition(candidate){
  const contracts = parseInt(prompt(`Open CSP for ${candidate.symbol}\nStrike: $${candidate.strike}  Exp: ${candidate.expiry}\nMid: $${candidate.mid}\n\nHow many contracts?`, '1'));
  if(isNaN(contracts) || contracts < 1) return;
  candidate.contracts = contracts;
  await fetch('/api/wheel/open', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(candidate)});
  loadPositions();
}

async function assignPos(id){
  if(!confirm('Mark this CSP as assigned (shares taken)?')) return;
  await fetch('/api/wheel/assign/'+id, {method:'POST'});
  loadPositions();
}

async function sellCC(id){
  const strike  = parseFloat(prompt('Covered Call strike price?'));
  const expiry  = prompt('Expiry date (YYYY-MM-DD)?');
  const dte     = parseInt(prompt('DTE?'));
  const mid     = parseFloat(prompt('Premium mid price?'));
  if(isNaN(strike)||!expiry||isNaN(dte)||isNaN(mid)) return;
  await fetch('/api/wheel/sell-cc/'+id, {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({strike, expiry, dte, mid})
  });
  loadPositions();
}

async function closePos(id, calledAway){
  const msg = calledAway ? 'Mark as called away (shares sold at strike)?' : 'Mark as expired worthless?';
  if(!confirm(msg)) return;
  await fetch(`/api/wheel/close/${id}?called_away=${calledAway}`, {method:'POST'});
  loadPositions();
}

// ── Init ──────────────────────────────────────────────────────────────────────
loadPositions();
setInterval(loadPositions, 30000);
'''
