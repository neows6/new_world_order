"""Append updated paper trading section to dashboard.py."""

PAPER_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Paper Trading \u2014 NWO</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d1117; color: #e6edf3; font-family: 'Segoe UI', monospace; font-size: 14px; }
  header { background: #161b22; padding: 12px 20px; border-bottom: 1px solid #30363d;
           display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .back-btn { padding: 5px 12px; border-radius: 6px; border: 1px solid #30363d;
              background: #21262d; color: #8b949e; text-decoration: none; font-size: 12px; }
  header h1 { font-size: 17px; letter-spacing: 1px; color: #58a6ff; }
  .header-right { margin-left: auto; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .run-btn   { padding: 6px 16px; border-radius: 6px; border: 1px solid #3fb950;
               background: #1a4731; color: #3fb950; cursor: pointer; font-size: 12px; font-weight: 600; }
  .run-btn:hover { background: #1e5c3a; }
  .run-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .reset-btn { padding: 5px 14px; border-radius: 6px; border: 1px solid #f85149;
               background: transparent; color: #f85149; cursor: pointer; font-size: 12px; }
  .reset-btn:hover { background: rgba(248,81,73,0.1); }
  .refresh-btn { padding: 5px 14px; border-radius: 6px; border: 1px solid #30363d;
                 background: #21262d; color: #8b949e; cursor: pointer; font-size: 12px; }
  #run-status { font-size: 11px; color: #d29922; }
  main { padding: 16px; display: grid; gap: 16px; }
  /* Summary cards */
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px; }
  .card-label { font-size: 10px; color: #8b949e; letter-spacing: 1px; text-transform: uppercase; margin-bottom: 6px; }
  .card-value { font-size: 22px; font-weight: 700; }
  .card-sub { font-size: 11px; color: #8b949e; margin-top: 4px; }
  .up { color: #3fb950; } .dn { color: #f85149; } .neu { color: #8b949e; }
  /* Swim lanes */
  .swim-wrap { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; }
  .lane { background: #161b22; border: 1px solid #30363d; border-radius: 8px; overflow: hidden; min-width: 0; }
  .lane-header { padding: 10px 12px; font-size: 11px; font-weight: 700; letter-spacing: 0.5px;
                 text-transform: uppercase; border-bottom: 1px solid #21262d; }
  .lane-0 .lane-header { color: #58a6ff; border-top: 3px solid #58a6ff; }
  .lane-1 .lane-header { color: #d29922; border-top: 3px solid #d29922; }
  .lane-2 .lane-header { color: #3fb950; border-top: 3px solid #3fb950; }
  .lane-sub { font-size: 10px; color: #8b949e; font-weight: 400; margin-top: 2px; }
  .lane-body { padding: 8px; display: flex; flex-direction: column; gap: 6px; min-height: 80px; }
  .signal-card { background: #0d1117; border: 1px solid #21262d; border-radius: 6px;
                 padding: 8px 10px; font-size: 12px; }
  .sig-ticker { font-weight: 700; font-size: 13px; }
  .sig-bull { color: #3fb950; } .sig-bear { color: #f85149; }
  .sig-meta { font-size: 10px; color: #8b949e; margin-top: 3px; display: flex; gap: 8px; flex-wrap: wrap; }
  .lane-empty { color: #8b949e; font-size: 12px; font-style: italic; padding: 12px; text-align: center; }
  /* Tables */
  .section { background: #161b22; border: 1px solid #30363d; border-radius: 8px; overflow: hidden; }
  .section-title { padding: 10px 14px; font-size: 12px; font-weight: 600; letter-spacing: 1px;
                   color: #8b949e; border-bottom: 1px solid #21262d; text-transform: uppercase; }
  table { width: 100%; border-collapse: collapse; }
  th { padding: 8px 12px; text-align: left; font-size: 11px; color: #8b949e;
       font-weight: 600; letter-spacing: 0.5px; border-bottom: 1px solid #21262d; }
  td { padding: 8px 12px; font-size: 13px; border-bottom: 1px solid #161b22; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: #1c2128; }
  .empty { color: #8b949e; font-style: italic; padding: 20px; text-align: center; display: block; }
  @media (max-width: 800px) {
    .swim-wrap { grid-template-columns: 1fr; }
    .cards { grid-template-columns: 1fr 1fr; }
    th:nth-child(n+5), td:nth-child(n+5) { display: none; }
  }
</style>
</head>
<body>
<header>
  <a href="/" class="back-btn">&#8592; Dashboard</a>
  <h1>&#127918; Paper Trading</h1>
  <div class="header-right">
    <span id="run-status"></span>
    <button class="run-btn" id="run-btn" onclick="runNow()">&#9654; Run Now</button>
    <button class="refresh-btn" onclick="load()">&#8635; Refresh</button>
    <button class="reset-btn" onclick="resetAccount()">&#x21BA; Reset</button>
  </div>
</header>
<main>
  <!-- Summary cards -->
  <div class="cards">
    <div class="card"><div class="card-label">Total Equity</div><div class="card-value" id="c-equity">\u2014</div><div class="card-sub">Starting: $100,000</div></div>
    <div class="card"><div class="card-label">Cash</div><div class="card-value" id="c-cash">\u2014</div><div class="card-sub" id="c-cash-sub">&nbsp;</div></div>
    <div class="card"><div class="card-label">Invested</div><div class="card-value" id="c-invested">\u2014</div><div class="card-sub" id="c-invested-sub">&nbsp;</div></div>
    <div class="card"><div class="card-label">Total P&amp;L</div><div class="card-value" id="c-pnl">\u2014</div><div class="card-sub" id="c-pnl-sub">&nbsp;</div></div>
  </div>
  <!-- Three model swim lanes -->
  <div class="swim-wrap" id="swim-wrap">
    <div class="lane lane-0"><div class="lane-header">&#9899; Standard<div class="lane-sub">Conf &gt;50% &middot; MoS &gt;15% &middot; FUD &gt;0.60</div></div><div class="lane-body" id="lane-0"><span class="lane-empty">Loading...</span></div></div>
    <div class="lane lane-1"><div class="lane-header">&#9898; Relaxed \u221225%<div class="lane-sub">Conf &gt;37.5% &middot; MoS &gt;11.25% &middot; FUD &gt;0.45</div></div><div class="lane-body" id="lane-1"><span class="lane-empty">Loading...</span></div></div>
    <div class="lane lane-2"><div class="lane-header">&#9711; Relaxed \u221250%<div class="lane-sub">Conf &gt;25% &middot; MoS &gt;7.5% &middot; FUD &gt;0.30</div></div><div class="lane-body" id="lane-2"><span class="lane-empty">Loading...</span></div></div>
  </div>
  <!-- Open Positions -->
  <div class="section">
    <div class="section-title">Open Positions</div>
    <div id="positions-wrap"><span class="empty">Loading...</span></div>
  </div>
  <!-- Trade History -->
  <div class="section">
    <div class="section-title">Trade History</div>
    <div id="trades-wrap"><span class="empty">Loading...</span></div>
  </div>
</main>
<script src="/paper.js"></script>
</body>
</html>"""

PAPER_JS = r"""
// ── Model thresholds (mirror main dashboard) ─────────────────────────────────
const MODELS = [
  { name: 'Standard',    conf: 0.50,  mos: 0.15,   fud: 0.60 },
  { name: 'Relaxed -25%', conf: 0.375, mos: 0.1125, fud: 0.45 },
  { name: 'Relaxed -50%', conf: 0.25,  mos: 0.075,  fud: 0.30 },
];

// ── Formatters ────────────────────────────────────────────────────────────────
function fmt(n, d) {
  if (d === undefined) d = 2;
  if (n == null) return '\u2014';
  return '$' + Math.abs(n).toLocaleString('en-US', {minimumFractionDigits: d, maximumFractionDigits: d});
}
function fmtPct(n) { return n == null ? '' : (n >= 0 ? '+' : '') + n.toFixed(2) + '%'; }
function cls(n)    { return n > 0 ? 'up' : n < 0 ? 'dn' : 'neu'; }

// ── Account + trades ──────────────────────────────────────────────────────────
async function load() {
  try {
    const [acct, trades] = await Promise.all([
      fetch('/api/paper/account').then(r => r.json()),
      fetch('/api/paper/trades').then(r => r.json()),
    ]);

    document.getElementById('c-equity').textContent = fmt(acct.total_equity, 0);
    document.getElementById('c-cash').textContent = fmt(acct.cash, 0);
    document.getElementById('c-cash-sub').textContent =
      ((acct.cash / acct.total_equity) * 100).toFixed(1) + '% of portfolio';
    document.getElementById('c-invested').textContent = fmt(acct.positions_value, 0);
    document.getElementById('c-invested-sub').textContent =
      ((acct.positions_value / acct.total_equity) * 100).toFixed(1) + '% of portfolio';

    const pnlEl = document.getElementById('c-pnl');
    pnlEl.textContent = (acct.total_pnl >= 0 ? '+' : '') + fmt(acct.total_pnl, 0);
    pnlEl.className = 'card-value ' + cls(acct.total_pnl);
    document.getElementById('c-pnl-sub').innerHTML =
      '<span class="' + cls(acct.total_pnl_pct) + '">' + fmtPct(acct.total_pnl_pct) + '</span> vs $100k start';

    // Positions
    const pw = document.getElementById('positions-wrap');
    if (!acct.positions || !acct.positions.length) {
      pw.innerHTML = '<span class="empty">No open positions yet.</span>';
    } else {
      let h = '<table><tr><th>Ticker</th><th>Qty</th><th>Avg Cost</th><th>Price</th><th>Mkt Value</th><th>P&amp;L</th><th>%</th></tr>';
      for (const p of acct.positions) {
        h += '<tr><td><strong>' + p.ticker + '</strong></td><td>' + p.qty + '</td><td>' +
          fmt(p.avg_cost) + '</td><td>' + fmt(p.cur_price) + '</td><td>' + fmt(p.mkt_val, 0) +
          '</td><td class="' + cls(p.pnl) + '">' + (p.pnl >= 0 ? '+' : '') + fmt(p.pnl) +
          '</td><td class="' + cls(p.pnl_pct) + '">' + fmtPct(p.pnl_pct) + '</td></tr>';
      }
      pw.innerHTML = h + '</table>';
    }

    // Trades
    const tw = document.getElementById('trades-wrap');
    if (!trades || !trades.length) {
      tw.innerHTML = '<span class="empty">No trades yet \u2014 click Run Now or start python -m paper.runner</span>';
    } else {
      let h = '<table><tr><th>Time</th><th>Ticker</th><th>Action</th><th>Qty</th><th>Price</th><th>Total</th><th>Cash After</th></tr>';
      for (const t of trades) {
        const dt = t.timestamp
          ? new Date(t.timestamp + 'Z').toLocaleString([], {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})
          : '\u2014';
        h += '<tr><td class="neu" style="font-size:11px">' + dt + '</td><td><strong>' + t.ticker +
          '</strong></td><td class="' + (t.action==='BUY'?'up':'dn') + '">' + t.action +
          '</td><td>' + t.qty + '</td><td>' + fmt(t.price) + '</td><td>' + fmt(t.total, 0) +
          '</td><td class="neu">' + fmt(t.cash_after, 0) + '</td></tr>';
      }
      tw.innerHTML = h + '</table>';
    }
  } catch(e) { console.error('Paper load error:', e); }
}

// ── Swim lanes ────────────────────────────────────────────────────────────────
async function loadSwimLanes() {
  try {
    const signals = await fetch('/api/signals').then(r => r.json());
    if (!signals || !signals.length) {
      for (let i = 0; i < 3; i++)
        document.getElementById('lane-' + i).innerHTML =
          '<span class="lane-empty">No signals yet.</span>';
      return;
    }

    MODELS.forEach((m, idx) => {
      // Tickers that pass this model's gates
      const passing = signals.filter(s => {
        const conf = s.confidence || 0;
        const mos  = s.margin_of_safety || 0;
        const fud  = s.fud_score || 0;
        const sig  = (s.signal || '').toUpperCase();
        return conf >= m.conf && mos >= m.mos && fud >= m.fud
               && (sig === 'BUY' || sig === 'STRONG_BUY');
      });

      const el = document.getElementById('lane-' + idx);
      if (!passing.length) {
        el.innerHTML = '<span class="lane-empty">No tickers clear this threshold.</span>';
        return;
      }

      // Sort by confidence desc
      passing.sort((a, b) => (b.confidence || 0) - (a.confidence || 0));

      el.innerHTML = passing.map(s => {
        const conf   = ((s.confidence || 0) * 100).toFixed(0);
        const mos    = ((s.margin_of_safety || 0) * 100).toFixed(1);
        const fud    = (s.fud_score || 0).toFixed(2);
        const price  = s.current_price ? '$' + s.current_price.toFixed(2) : '';
        const chg    = s.change_pct != null
          ? '<span class="' + cls(s.change_pct) + '">' + (s.change_pct >= 0 ? '+' : '') + s.change_pct.toFixed(1) + '%</span>'
          : '';
        return '<div class="signal-card">' +
          '<div class="sig-ticker sig-bull">&#9650; ' + s.ticker +
            (price ? ' <span class="neu" style="font-weight:400">' + price + '</span>' : '') +
            (chg ? ' ' + chg : '') +
          '</div>' +
          '<div class="sig-meta">' +
            '<span>Conf ' + conf + '%</span>' +
            '<span>MoS ' + mos + '%</span>' +
            '<span>FUD ' + fud + '</span>' +
          '</div>' +
        '</div>';
      }).join('');
    });
  } catch(e) { console.error('Swim lanes error:', e); }
}

// ── Run Now ───────────────────────────────────────────────────────────────────
let _runPollTimer = null;

async function runNow() {
  const btn = document.getElementById('run-btn');
  const status = document.getElementById('run-status');
  btn.disabled = true;
  status.textContent = '\u29d7 Cycle running...';

  try {
    const r = await fetch('/api/paper/run', {method: 'POST'}).then(r => r.json());
    if (r.status === 'started') {
      status.textContent = '\u29d7 Running pipeline...';
      // Poll every 5s until done
      _runPollTimer = setInterval(async () => {
        const s = await fetch('/api/paper/run/status').then(r => r.json());
        if (!s.running) {
          clearInterval(_runPollTimer);
          btn.disabled = false;
          status.textContent = '\u2713 Done \u2014 ' + new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
          load();
          loadSwimLanes();
          setTimeout(() => { status.textContent = ''; }, 8000);
        }
      }, 5000);
    } else {
      status.textContent = r.status || 'Already running';
      btn.disabled = false;
    }
  } catch(e) {
    status.textContent = 'Error: ' + e.message;
    btn.disabled = false;
  }
}

async function resetAccount() {
  if (!confirm('Reset paper account to $100,000? This erases all trades and positions.')) return;
  await fetch('/api/paper/reset', {method: 'POST'});
  load();
}

// ── Init ──────────────────────────────────────────────────────────────────────
load();
loadSwimLanes();
setInterval(() => { load(); loadSwimLanes(); }, 30000);
"""

import sys
sys.path.insert(0, 'C:/Users/neo_w/new_world_order')

PAPER_ROUTES = '''

# ── Paper Trading Dashboard ───────────────────────────────────────────────────

PAPER_HTML = """ + repr(PAPER_HTML)[1:-1] + """

PAPER_JS = """ + repr(PAPER_JS)[1:-1] + """


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
    import threading

    def _run():
        global _paper_running
        _paper_running = True
        try:
            from paper.runner import (
                run_paper_cycle, _load_price_data, _get_vix
            )
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
    return {"running": _paper_running}
'''

# Write the code properly using Python string representation to avoid quote issues
code = '\n\n# ── Paper Trading Dashboard ───────────────────────────────────────────────────\n\n'
code += 'PAPER_HTML = ' + repr(PAPER_HTML) + '\n\n'
code += 'PAPER_JS = ' + repr(PAPER_JS) + '\n\n'

code += '''
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
    import threading

    def _run():
        global _paper_running
        _paper_running = True
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
    return {"running": _paper_running}
'''

with open('C:/Users/neo_w/new_world_order/monitor/dashboard.py', 'a', encoding='utf-8') as f:
    f.write(code)

print("Appended. Lines:", len(open('C:/Users/neo_w/new_world_order/monitor/dashboard.py', encoding='utf-8').readlines()))
