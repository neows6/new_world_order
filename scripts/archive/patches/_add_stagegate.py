"""Append Stage Gate page + API routes to dashboard.py."""
import json, os

PATH = 'C:/Users/neo_w/new_world_order/monitor/dashboard.py'
STAGEGATE_FILE = 'data/stagegate.json'

STAGEGATE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Stage Gate \u2014 NWO</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d1117; color: #e6edf3; font-family: 'Segoe UI', monospace; font-size: 14px; min-height: 100vh; }
  header { background: #161b22; padding: 12px 20px; border-bottom: 1px solid #30363d;
           display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  .back-btn { padding: 5px 12px; border-radius: 6px; border: 1px solid #30363d;
              background: #21262d; color: #8b949e; text-decoration: none; font-size: 12px; }
  header h1 { font-size: 17px; letter-spacing: 1px; color: #58a6ff; }
  .header-right { margin-left: auto; display: flex; gap: 8px; align-items: center; }
  .add-form { display: flex; gap: 6px; }
  .add-input { padding: 5px 10px; border-radius: 6px; border: 1px solid #30363d;
               background: #21262d; color: #e6edf3; font-size: 12px; width: 120px; text-transform: uppercase; }
  .add-input::placeholder { color: #8b949e; text-transform: none; }
  .add-btn { padding: 5px 12px; border-radius: 6px; border: 1px solid #58a6ff;
             background: #0d1e36; color: #58a6ff; cursor: pointer; font-size: 12px; }
  .add-btn:hover { background: #1c2e50; }
  .save-badge { font-size: 11px; color: #3fb950; display: none; }
  /* Two-column layout */
  .stages { display: grid; grid-template-columns: 1fr 1fr; gap: 0; height: calc(100vh - 57px); }
  .stage { display: flex; flex-direction: column; border-right: 1px solid #30363d; overflow: hidden; }
  .stage:last-child { border-right: none; }
  .stage-header { padding: 14px 18px; background: #161b22; border-bottom: 1px solid #30363d;
                  display: flex; align-items: center; gap: 10px; flex-shrink: 0; }
  .stage-1 .stage-header { border-top: 3px solid #8b949e; }
  .stage-2 .stage-header { border-top: 3px solid #3fb950; }
  .stage-title { font-size: 14px; font-weight: 700; letter-spacing: 0.5px; }
  .stage-1 .stage-title { color: #8b949e; }
  .stage-2 .stage-title { color: #3fb950; }
  .stage-subtitle { font-size: 11px; color: #8b949e; margin-top: 1px; }
  .stage-count { margin-left: auto; font-size: 11px; color: #8b949e; background: #21262d;
                 padding: 2px 8px; border-radius: 10px; }
  /* Drop zone */
  .drop-zone { flex: 1; overflow-y: auto; padding: 12px;
               display: flex; flex-direction: column; gap: 8px; }
  .drop-zone.drag-over { background: rgba(88,166,255,0.05);
                          outline: 2px dashed #58a6ff; outline-offset: -4px; border-radius: 4px; }
  /* Stock cards */
  .stock-card { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
                padding: 10px 14px; cursor: grab; display: flex; align-items: center;
                gap: 10px; transition: border-color 0.15s, background 0.15s;
                user-select: none; }
  .stock-card:hover { border-color: #58a6ff; background: #1c2128; }
  .stock-card:active { cursor: grabbing; }
  .stock-card.dragging { opacity: 0.4; }
  .stage-2 .stock-card { border-left: 3px solid #3fb950; }
  .card-ticker { font-size: 15px; font-weight: 700; min-width: 60px; }
  .card-meta { font-size: 11px; color: #8b949e; flex: 1; }
  .card-signal { font-size: 11px; font-weight: 600; }
  .sig-bull { color: #3fb950; } .sig-bear { color: #f85149; } .sig-hold { color: #8b949e; }
  .card-remove { background: none; border: none; color: #8b949e; cursor: pointer;
                 font-size: 14px; padding: 2px 4px; border-radius: 4px; line-height: 1; }
  .card-remove:hover { color: #f85149; background: rgba(248,81,73,0.1); }
  .drop-hint { color: #8b949e; font-size: 12px; text-align: center; padding: 30px;
               border: 2px dashed #21262d; border-radius: 8px; font-style: italic; margin-top: 4px; }
  @media (max-width: 700px) {
    .stages { grid-template-columns: 1fr; height: auto; }
    .stage { min-height: 40vh; border-right: none; border-bottom: 1px solid #30363d; }
  }
</style>
</head>
<body>
<header>
  <a href="/" class="back-btn">&#8592; Dashboard</a>
  <h1>&#127760; Stage Gate</h1>
  <div class="header-right">
    <div class="add-form">
      <input class="add-input" id="add-input" type="text" placeholder="Add ticker..." maxlength="10"
             onkeydown="if(event.key==='Enter') addTicker()">
      <button class="add-btn" onclick="addTicker()">+ Add</button>
    </div>
    <span class="save-badge" id="save-badge">&#10003; Saved</span>
  </div>
</header>

<div class="stages">
  <!-- Stage 1 -->
  <div class="stage stage-1">
    <div class="stage-header">
      <div>
        <div class="stage-title">&#128203; Stage 1 &mdash; Monitoring</div>
        <div class="stage-subtitle">Watching only &middot; drag right to activate</div>
      </div>
      <span class="stage-count" id="count-1">0</span>
    </div>
    <div class="drop-zone" id="zone-1"
         ondragover="onDragOver(event,'1')" ondragleave="onDragLeave('1')" ondrop="onDrop(event,'1')">
      <div class="drop-hint">Drag stocks here to monitor (no trading)</div>
    </div>
  </div>

  <!-- Stage 2 -->
  <div class="stage stage-2">
    <div class="stage-header">
      <div>
        <div class="stage-title">&#9654; Stage 2 &mdash; Active Trading</div>
        <div class="stage-subtitle">Full AI pipeline &middot; paper &amp; live execution</div>
      </div>
      <span class="stage-count" id="count-2">0</span>
    </div>
    <div class="drop-zone" id="zone-2"
         ondragover="onDragOver(event,'2')" ondragleave="onDragLeave('2')" ondrop="onDrop(event,'2')">
      <div class="drop-hint">Drag stocks here to activate AI analysis &amp; trading</div>
    </div>
  </div>
</div>

<script src="/stagegate.js"></script>
</body>
</html>"""

STAGEGATE_JS = r"""
// ── State ─────────────────────────────────────────────────────────────────────
let _state = { stage1: [], stage2: [] };
let _signals = {};   // ticker -> signal data from /api/signals
let _dragTicker = null;
let _dragFrom   = null;

// ── Boot ──────────────────────────────────────────────────────────────────────
async function boot() {
  // Load signals for metadata (confidence, signal type, price)
  try {
    const sigs = await fetch('/api/signals').then(r => r.json());
    (sigs || []).forEach(s => { _signals[s.ticker] = s; });
  } catch(e) {}

  // Load stage state
  try {
    _state = await fetch('/api/stagegate').then(r => r.json());
  } catch(e) {}

  render();
}

// ── Render ────────────────────────────────────────────────────────────────────
function render() {
  renderZone('1', _state.stage1);
  renderZone('2', _state.stage2);
  document.getElementById('count-1').textContent = _state.stage1.length;
  document.getElementById('count-2').textContent = _state.stage2.length;
}

function renderZone(stage, tickers) {
  const zone = document.getElementById('zone-' + stage);
  if (!tickers.length) {
    zone.innerHTML = stage === '1'
      ? '<div class="drop-hint">Drag stocks here to monitor (no trading)</div>'
      : '<div class="drop-hint">Drag stocks here to activate AI analysis &amp; trading</div>';
    return;
  }
  zone.innerHTML = tickers.map(ticker => cardHtml(ticker, stage)).join('');
}

function cardHtml(ticker, stage) {
  const s = _signals[ticker] || {};
  const sig = (s.signal || 'HOLD').toUpperCase();
  const sigCls = sig === 'BUY' || sig === 'STRONG_BUY' ? 'sig-bull'
               : sig === 'SELL' || sig === 'STRONG_SELL' ? 'sig-bear' : 'sig-hold';
  const price = s.current_price ? '$' + s.current_price.toFixed(2) : '';
  const conf  = s.confidence ? (s.confidence * 100).toFixed(0) + '% conf' : '';
  const meta  = [price, conf].filter(Boolean).join(' \u00b7 ');
  return '<div class="stock-card" draggable="true" data-ticker="' + ticker + '" data-stage="' + stage + '" '
    + 'ondragstart="onDragStart(event)" ondragend="onDragEnd(event)">'
    + '<div class="card-ticker">' + ticker + '</div>'
    + '<div class="card-meta">' + meta + '</div>'
    + '<span class="card-signal ' + sigCls + '">' + sig + '</span>'
    + '<button class="card-remove" onclick="removeTicker(\'' + ticker + '\')" title="Remove">&#x2715;</button>'
    + '</div>';
}

// ── Drag & Drop ───────────────────────────────────────────────────────────────
function onDragStart(e) {
  _dragTicker = e.currentTarget.dataset.ticker;
  _dragFrom   = e.currentTarget.dataset.stage;
  e.currentTarget.classList.add('dragging');
  e.dataTransfer.effectAllowed = 'move';
}

function onDragEnd(e) {
  e.currentTarget.classList.remove('dragging');
}

function onDragOver(e, stage) {
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  document.getElementById('zone-' + stage).classList.add('drag-over');
}

function onDragLeave(stage) {
  document.getElementById('zone-' + stage).classList.remove('drag-over');
}

function onDrop(e, targetStage) {
  e.preventDefault();
  document.getElementById('zone-' + targetStage).classList.remove('drag-over');
  if (!_dragTicker || _dragFrom === targetStage) return;

  // Move ticker
  const fromArr = _state['stage' + _dragFrom];
  const toArr   = _state['stage' + targetStage];
  const idx = fromArr.indexOf(_dragTicker);
  if (idx !== -1) fromArr.splice(idx, 1);
  if (!toArr.includes(_dragTicker)) toArr.push(_dragTicker);

  render();
  save();
}

// ── Add / Remove ──────────────────────────────────────────────────────────────
function addTicker() {
  const inp = document.getElementById('add-input');
  const ticker = inp.value.trim().toUpperCase();
  inp.value = '';
  if (!ticker) return;
  if (_state.stage1.includes(ticker) || _state.stage2.includes(ticker)) return;
  _state.stage1.push(ticker);
  render();
  save();
}

function removeTicker(ticker) {
  _state.stage1 = _state.stage1.filter(t => t !== ticker);
  _state.stage2 = _state.stage2.filter(t => t !== ticker);
  render();
  save();
}

// ── Persist ───────────────────────────────────────────────────────────────────
async function save() {
  try {
    await fetch('/api/stagegate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(_state),
    });
    const badge = document.getElementById('save-badge');
    badge.style.display = 'inline';
    setTimeout(() => { badge.style.display = 'none'; }, 2000);
  } catch(e) {}
}

boot();
"""

ROUTES = '''

# ── Stage Gate ────────────────────────────────────────────────────────────────

STAGEGATE_HTML = ''' + repr(STAGEGATE_HTML) + '''
STAGEGATE_JS   = ''' + repr(STAGEGATE_JS) + '''

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
    return {"stage1": list(config.watchlist), "stage2": []}


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
    import json
    body = await request.json()
    stage1 = body.get("stage1", [])
    stage2 = body.get("stage2", [])
    _save_stagegate({"stage1": stage1, "stage2": stage2})
    return {"status": "ok", "stage1": len(stage1), "stage2": len(stage2)}
'''

with open(PATH, 'a', encoding='utf-8') as f:
    f.write(ROUTES)

print("Appended Stage Gate routes")

# Verify syntax
import subprocess, sys
r = subprocess.run([sys.executable, '-m', 'py_compile', PATH], capture_output=True, text=True)
if r.returncode == 0:
    print("Syntax OK")
else:
    print("SYNTAX ERROR:", r.stderr)
