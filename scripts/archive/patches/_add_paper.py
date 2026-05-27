"""Append paper trading routes to dashboard.py — run once then delete."""
import sys
sys.path.insert(0, "C:/Users/neo_w/new_world_order")

PAPER_CODE = '''

# ── Paper Trading Dashboard ───────────────────────────────────────────────────

PAPER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Paper Trading \u2014 NWO</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d1117; color: #e6edf3; font-family: \\'Segoe UI\\', monospace; font-size: 14px; }
  header { background: #161b22; padding: 12px 20px; border-bottom: 1px solid #30363d;
           display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
  .back-btn { padding: 5px 12px; border-radius: 6px; border: 1px solid #30363d;
              background: #21262d; color: #8b949e; text-decoration: none; font-size: 12px; }
  header h1 { font-size: 17px; letter-spacing: 1px; color: #58a6ff; }
  .header-right { margin-left: auto; display: flex; gap: 8px; align-items: center; }
  .reset-btn { padding: 5px 14px; border-radius: 6px; border: 1px solid #f85149;
               background: transparent; color: #f85149; cursor: pointer; font-size: 12px; }
  .reset-btn:hover { background: rgba(248,81,73,0.1); }
  .refresh-btn { padding: 5px 14px; border-radius: 6px; border: 1px solid #30363d;
                 background: #21262d; color: #8b949e; cursor: pointer; font-size: 12px; }
  main { padding: 16px; display: grid; gap: 16px; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px; }
  .card-label { font-size: 10px; color: #8b949e; letter-spacing: 1px; text-transform: uppercase; margin-bottom: 6px; }
  .card-value { font-size: 22px; font-weight: 700; }
  .card-sub { font-size: 11px; color: #8b949e; margin-top: 4px; }
  .up { color: #3fb950; } .dn { color: #f85149; } .neu { color: #8b949e; }
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
  @media (max-width: 700px) {
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
    <button class="refresh-btn" onclick="load()">&#8635; Refresh</button>
    <button class="reset-btn" onclick="resetAccount()">&#x21BA; Reset Account</button>
  </div>
</header>
<main>
  <div class="cards">
    <div class="card"><div class="card-label">Total Equity</div><div class="card-value" id="c-equity">\u2014</div><div class="card-sub">Starting: $100,000</div></div>
    <div class="card"><div class="card-label">Cash</div><div class="card-value" id="c-cash">\u2014</div><div class="card-sub" id="c-cash-sub">&nbsp;</div></div>
    <div class="card"><div class="card-label">Invested</div><div class="card-value" id="c-invested">\u2014</div><div class="card-sub" id="c-invested-sub">&nbsp;</div></div>
    <div class="card"><div class="card-label">Total P&amp;L</div><div class="card-value" id="c-pnl">\u2014</div><div class="card-sub" id="c-pnl-sub">&nbsp;</div></div>
  </div>
  <div class="section">
    <div class="section-title">Open Positions</div>
    <div id="positions-wrap"><span class="empty">Loading...</span></div>
  </div>
  <div class="section">
    <div class="section-title">Trade History</div>
    <div id="trades-wrap"><span class="empty">Loading...</span></div>
  </div>
</main>
<script src="/paper.js"></script>
</body>
</html>"""


PAPER_JS = """
function fmt(n, decimals) {
  if (decimals === undefined) decimals = 2;
  if (n == null) return '\\u2014';
  return '$' + Math.abs(n).toLocaleString('en-US', {minimumFractionDigits: decimals, maximumFractionDigits: decimals});
}
function fmtPct(n) {
  if (n == null) return '';
  return (n >= 0 ? '+' : '') + n.toFixed(2) + '%';
}
function cls(n) { return n > 0 ? 'up' : n < 0 ? 'dn' : 'neu'; }

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

    const pw = document.getElementById('positions-wrap');
    if (!acct.positions || !acct.positions.length) {
      pw.innerHTML = '<span class="empty">No open positions yet.</span>';
    } else {
      let h = '<table><tr><th>Ticker</th><th>Qty</th><th>Avg Cost</th><th>Price</th><th>Mkt Value</th><th>P&amp;L</th><th>P&amp;L %</th></tr>';
      for (const p of acct.positions) {
        h += '<tr>' +
          '<td><strong>' + p.ticker + '</strong></td>' +
          '<td>' + p.qty + '</td>' +
          '<td>' + fmt(p.avg_cost) + '</td>' +
          '<td>' + fmt(p.cur_price) + '</td>' +
          '<td>' + fmt(p.mkt_val, 0) + '</td>' +
          '<td class="' + cls(p.pnl) + '">' + (p.pnl >= 0 ? '+' : '') + fmt(p.pnl) + '</td>' +
          '<td class="' + cls(p.pnl_pct) + '">' + fmtPct(p.pnl_pct) + '</td>' +
          '</tr>';
      }
      pw.innerHTML = h + '</table>';
    }

    const tw = document.getElementById('trades-wrap');
    if (!trades || !trades.length) {
      tw.innerHTML = '<span class="empty">No trades yet \u2014 run: python -m paper.runner</span>';
    } else {
      let h = '<table><tr><th>Time</th><th>Ticker</th><th>Action</th><th>Qty</th><th>Price</th><th>Total</th><th>Cash After</th></tr>';
      for (const t of trades) {
        const dt = t.timestamp
          ? new Date(t.timestamp + 'Z').toLocaleString([], {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})
          : '\\u2014';
        const acls = t.action === 'BUY' ? 'up' : 'dn';
        h += '<tr>' +
          '<td class="neu" style="font-size:11px">' + dt + '</td>' +
          '<td><strong>' + t.ticker + '</strong></td>' +
          '<td class="' + acls + '">' + t.action + '</td>' +
          '<td>' + t.qty + '</td>' +
          '<td>' + fmt(t.price) + '</td>' +
          '<td>' + fmt(t.total, 0) + '</td>' +
          '<td class="neu">' + fmt(t.cash_after, 0) + '</td>' +
          '</tr>';
      }
      tw.innerHTML = h + '</table>';
    }
  } catch(e) { console.error('Paper load error:', e); }
}

async function resetAccount() {
  if (!confirm('Reset paper account to $100,000? This erases all trades and positions.')) return;
  await fetch('/api/paper/reset', {method: 'POST'});
  load();
}

load();
setInterval(load, 30000);
"""


@app.get("/paper", response_class=HTMLResponse)
def paper_page():
    from fastapi.responses import HTMLResponse as HR
    return HR(content=PAPER_HTML, headers={"Cache-Control": "no-store"})


@app.get("/paper.js")
def paper_js():
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
'''

with open('C:/Users/neo_w/new_world_order/monitor/dashboard.py', 'a', encoding='utf-8') as f:
    f.write(PAPER_CODE)
print("Appended OK, file size:", len(open('C:/Users/neo_w/new_world_order/monitor/dashboard.py', encoding='utf-8').read()))
