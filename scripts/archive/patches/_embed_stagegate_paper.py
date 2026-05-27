"""
Embed Stage Gate into Paper Trading dashboard.
Appends to dashboard.py:
  - PAPER_HTML override: adds Stage Gate CSS + two-column drag section + activation modal
  - PAPER_JS override: adds Stage Gate JS (boot, render, drag+drop, modal, save)
  - /api/paper/activate POST route
"""
PATH = 'C:/Users/neo_w/new_world_order/monitor/dashboard.py'

# ── New CSS (injected before </style>) ───────────────────────────────────────
SG_CSS = """
  /* ── Stage Gate ─────────────────────────────────────────────── */
  .sg-stages { display: grid; grid-template-columns: 1fr 1fr; gap: 0; }
  .sg-stage { display: flex; flex-direction: column; border-right: 1px solid #21262d; min-width: 0; }
  .sg-stage:last-child { border-right: none; }
  .sg-stage-header { padding: 10px 14px; background: #0d1117; border-bottom: 1px solid #21262d;
                     display: flex; align-items: center; gap: 8px; }
  .sg-stage-1 .sg-stage-header { border-top: 3px solid #8b949e; }
  .sg-stage-2 .sg-stage-header { border-top: 3px solid #3fb950; }
  .sg-stage-title { font-size: 12px; font-weight: 700; }
  .sg-stage-1 .sg-stage-title { color: #8b949e; }
  .sg-stage-2 .sg-stage-title { color: #3fb950; }
  .sg-stage-sub { font-size: 10px; color: #8b949e; margin-top: 2px; }
  .sg-count { margin-left: auto; font-size: 11px; color: #8b949e; background: #21262d;
              padding: 2px 7px; border-radius: 10px; }
  .sg-drop-zone { flex: 1; min-height: 80px; padding: 8px;
                  display: flex; flex-direction: column; gap: 6px; }
  .sg-drop-zone.drag-over { background: rgba(88,166,255,0.05);
                             outline: 2px dashed #58a6ff; outline-offset: -3px; border-radius: 4px; }
  .sg-card { background: #0d1117; border: 1px solid #21262d; border-radius: 6px;
             padding: 7px 10px; cursor: grab; display: flex; align-items: center;
             gap: 8px; user-select: none; transition: border-color 0.15s; }
  .sg-card:hover { border-color: #58a6ff; }
  .sg-card:active { cursor: grabbing; }
  .sg-card.dragging { opacity: 0.4; }
  .sg-stage-2 .sg-card { border-left: 3px solid #3fb950; }
  .sg-ticker { font-size: 13px; font-weight: 700; min-width: 55px; }
  .sg-meta { font-size: 10px; color: #8b949e; flex: 1; }
  .sg-signal { font-size: 10px; font-weight: 600; }
  .sg-remove { background: none; border: none; color: #8b949e; cursor: pointer;
               font-size: 13px; padding: 1px 3px; border-radius: 3px; line-height: 1; }
  .sg-remove:hover { color: #f85149; background: rgba(248,81,73,0.1); }
  .sg-hint { color: #8b949e; font-size: 11px; text-align: center; padding: 20px 8px;
             border: 2px dashed #21262d; border-radius: 6px; font-style: italic; }
  /* Activation modal */
  .sg-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.75);
                display: flex; align-items: center; justify-content: center; z-index: 9999; }
  .sg-modal-box { background: #161b22; border: 1px solid #30363d; border-radius: 10px;
                  padding: 24px; width: 320px; display: flex; flex-direction: column; gap: 14px; }
  .sg-modal-box h3 { font-size: 15px; color: #e6edf3; }
  .sg-modal-price { font-size: 12px; color: #8b949e; }
  .sg-toggle { display: flex; gap: 20px; font-size: 13px; }
  .sg-toggle label { display: flex; align-items: center; gap: 6px; cursor: pointer; color: #e6edf3; }
  .sg-amount-input { width: 100%; padding: 9px 12px; border-radius: 6px; border: 1px solid #30363d;
                     background: #21262d; color: #e6edf3; font-size: 15px; }
  .sg-amount-input:focus { outline: none; border-color: #58a6ff; }
  .sg-modal-hint { font-size: 11px; color: #8b949e; min-height: 16px; }
  .sg-modal-btns { display: flex; gap: 8px; justify-content: flex-end; margin-top: 4px; }
  .sg-cancel-btn { padding: 7px 16px; border-radius: 6px; border: 1px solid #30363d;
                   background: transparent; color: #8b949e; cursor: pointer; font-size: 13px; }
  .sg-cancel-btn:hover { background: #21262d; }
  .sg-act-btn { padding: 7px 18px; border-radius: 6px; border: 1px solid #3fb950;
                background: #1a4731; color: #3fb950; cursor: pointer; font-size: 13px; font-weight: 600; }
  .sg-act-btn:hover { background: #1e5c3a; }
  .sg-act-btn:disabled { opacity: 0.5; cursor: not-allowed; }
"""

# ── Stage Gate HTML section + modal (injected before </main>) ─────────────────
SG_HTML = """
  <!-- ── Stage Gate ───────────────────────────────────────────── -->
  <div class="section">
    <div class="section-title">&#127760; Stage Gate &mdash; Stock Activation</div>
    <div class="sg-stages">
      <div class="sg-stage sg-stage-1">
        <div class="sg-stage-header">
          <div>
            <div class="sg-stage-title">&#128203; Stage 1 &mdash; Monitoring</div>
            <div class="sg-stage-sub">Watching only &middot; drag right to activate trading</div>
          </div>
          <span class="sg-count" id="sg-count-1">0</span>
        </div>
        <div class="sg-drop-zone" id="sg-zone-1"
             ondragover="sgDragOver(event,'1')" ondragleave="sgDragLeave('1')" ondrop="sgDrop(event,'1')">
          <div class="sg-hint">Drag stocks here to monitor (no trading)</div>
        </div>
      </div>
      <div class="sg-stage sg-stage-2">
        <div class="sg-stage-header">
          <div>
            <div class="sg-stage-title">&#9654; Stage 2 &mdash; Active Trading</div>
            <div class="sg-stage-sub">AI pipeline &middot; paper execution</div>
          </div>
          <span class="sg-count" id="sg-count-2">0</span>
        </div>
        <div class="sg-drop-zone" id="sg-zone-2"
             ondragover="sgDragOver(event,'2')" ondragleave="sgDragLeave('2')" ondrop="sgDrop(event,'2')">
          <div class="sg-hint">Drag here to activate AI analysis &amp; trading</div>
        </div>
      </div>
    </div>
  </div>

  <!-- Activation modal -->
  <div id="sg-overlay" class="sg-overlay" style="display:none">
    <div class="sg-modal-box">
      <h3 id="sg-modal-title">Activate for Trading</h3>
      <p class="sg-modal-price" id="sg-modal-price"></p>
      <div class="sg-toggle">
        <label><input type="radio" name="sg-mode" id="sg-mode-shares" value="shares" checked onchange="sgUpdateHint()"> Shares</label>
        <label><input type="radio" name="sg-mode" id="sg-mode-dollars" value="dollars" onchange="sgUpdateHint()"> Amount ($)</label>
      </div>
      <input class="sg-amount-input" id="sg-amount" type="number" min="1" step="1"
             placeholder="Enter amount..." oninput="sgUpdateHint()"
             onkeydown="if(event.key==='Enter') sgModalConfirm()">
      <p class="sg-modal-hint" id="sg-modal-hint">&nbsp;</p>
      <div class="sg-modal-btns">
        <button class="sg-cancel-btn" onclick="sgModalCancel()">Cancel</button>
        <button class="sg-act-btn" id="sg-act-btn" onclick="sgModalConfirm()">&#9654; Start Trading</button>
      </div>
    </div>
  </div>
"""

# ── Stage Gate JS (appended to PAPER_JS) ─────────────────────────────────────
# NOTE: Uses data-ticker + dataset.ticker to avoid any onclick quote issues.
SG_JS = """
// ── Stage Gate (embedded in paper dashboard) ─────────────────────────────────
let _sgState   = { stage1: [], stage2: [] };
let _sgSigs    = {};
let _sgDragT   = null;
let _sgDragF   = null;
let _sgPending = null;

async function sgBoot() {
  try {
    const sigs = await fetch('/api/signals').then(r => r.json());
    (sigs || []).forEach(s => { _sgSigs[s.ticker] = s; });
  } catch(e) {}
  try { _sgState = await fetch('/api/stagegate').then(r => r.json()); } catch(e) {}
  sgRender();
}

function sgRender() {
  const s1 = _sgState.stage1 || [], s2 = _sgState.stage2 || [];
  sgRenderZone('1', s1);
  sgRenderZone('2', s2);
  document.getElementById('sg-count-1').textContent = s1.length;
  document.getElementById('sg-count-2').textContent = s2.length;
}

function sgRenderZone(stage, tickers) {
  const zone = document.getElementById('sg-zone-' + stage);
  if (!tickers.length) {
    zone.innerHTML = stage === '1'
      ? '<div class="sg-hint">Drag stocks here to monitor (no trading)</div>'
      : '<div class="sg-hint">Drag here to activate AI analysis &amp; trading</div>';
    return;
  }
  zone.innerHTML = tickers.map(t => sgCardHtml(t, stage)).join('');
}

function sgCardHtml(ticker, stage) {
  const s   = _sgSigs[ticker] || {};
  const sig = (s.signal || 'HOLD').toUpperCase();
  const sc  = sig === 'BUY' || sig === 'STRONG_BUY'   ? 'sig-bull'
            : sig === 'SELL' || sig === 'STRONG_SELL'  ? 'sig-bear' : '';
  const price = s.current_price ? '$' + s.current_price.toFixed(2) : '';
  // Use data-ticker on the remove button — no quote escaping needed in onclick
  return '<div class="sg-card" draggable="true" data-ticker="' + ticker + '" data-stage="' + stage + '" '
    + 'ondragstart="sgDragStart(event)" ondragend="sgDragEnd(event)">'
    + '<div class="sg-ticker">' + ticker + '</div>'
    + '<div class="sg-meta">' + price + '</div>'
    + '<span class="sg-signal ' + sc + '">' + sig + '</span>'
    + '<button class="sg-remove" data-ticker="' + ticker + '" onclick="sgRemove(this.dataset.ticker)" title="Remove">&#x2715;</button>'
    + '</div>';
}

// ── Drag & drop ───────────────────────────────────────────────────────────────
function sgDragStart(e) {
  _sgDragT = e.currentTarget.dataset.ticker;
  _sgDragF = e.currentTarget.dataset.stage;
  e.currentTarget.classList.add('dragging');
  e.dataTransfer.effectAllowed = 'move';
}
function sgDragEnd(e) { e.currentTarget.classList.remove('dragging'); }
function sgDragOver(e, stage) {
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  document.getElementById('sg-zone-' + stage).classList.add('drag-over');
}
function sgDragLeave(stage) {
  document.getElementById('sg-zone-' + stage).classList.remove('drag-over');
}
function sgDrop(e, toStage) {
  e.preventDefault();
  document.getElementById('sg-zone-' + toStage).classList.remove('drag-over');
  if (!_sgDragT || _sgDragF === toStage) return;
  if (toStage === '2') {
    _sgPending = _sgDragT;
    sgShowModal(_sgDragT);
  } else {
    sgMoveLocal(_sgDragT, '2', '1');
  }
}

function sgMoveLocal(ticker, from, to) {
  const fa = _sgState['stage' + from] || [];
  const ta = _sgState['stage' + to]   || [];
  const i  = fa.indexOf(ticker);
  if (i !== -1) fa.splice(i, 1);
  if (!ta.includes(ticker)) ta.push(ticker);
  sgRender();
  sgSave();
}

// ── Modal ─────────────────────────────────────────────────────────────────────
function sgShowModal(ticker) {
  const s = _sgSigs[ticker] || {};
  document.getElementById('sg-modal-title').textContent  = 'Activate ' + ticker + ' for Trading';
  document.getElementById('sg-modal-price').textContent  =
    s.current_price ? 'Current price: $' + s.current_price.toFixed(2) : 'Price not available';
  document.getElementById('sg-mode-shares').checked      = true;
  document.getElementById('sg-amount').value             = '';
  document.getElementById('sg-modal-hint').innerHTML     = '&nbsp;';
  document.getElementById('sg-overlay').style.display   = 'flex';
  setTimeout(() => document.getElementById('sg-amount').focus(), 60);
}

function sgUpdateHint() {
  const mode  = document.querySelector('input[name="sg-mode"]:checked').value;
  const amt   = parseFloat(document.getElementById('sg-amount').value);
  const price = (_sgSigs[_sgPending] || {}).current_price;
  const hint  = document.getElementById('sg-modal-hint');
  if (!amt || amt <= 0) { hint.innerHTML = '&nbsp;'; return; }
  if (mode === 'shares') {
    hint.textContent = price
      ? 'Total cost \u2248 $' + (amt * price).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2})
      : amt + ' shares';
  } else {
    const shares = price ? Math.floor(amt / price) : null;
    hint.textContent = shares != null
      ? shares + ' shares @ $' + price.toFixed(2)
      : '$' + amt + ' allocated';
  }
}

function sgModalCancel() {
  document.getElementById('sg-overlay').style.display = 'none';
  _sgPending = null;
}

async function sgModalConfirm() {
  const ticker = _sgPending;
  const mode   = document.querySelector('input[name="sg-mode"]:checked').value;
  const amount = parseFloat(document.getElementById('sg-amount').value);
  if (!ticker || !amount || amount <= 0) return;

  const btn = document.getElementById('sg-act-btn');
  btn.disabled    = true;
  btn.textContent = 'Activating\u2026';

  try {
    const r = await fetch('/api/paper/activate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ ticker, mode, amount }),
    }).then(res => res.json());

    if (r.status === 'ok') {
      sgMoveLocal(ticker, '1', '2');
      document.getElementById('sg-overlay').style.display = 'none';
      _sgPending = null;
      load();   // refresh account summary
    } else {
      alert('Could not activate: ' + (r.error || 'unknown error'));
    }
  } catch(e) {
    alert('Error: ' + e.message);
  } finally {
    btn.disabled    = false;
    btn.textContent = '\\u25b6 Start Trading';
  }
}

function sgRemove(ticker) {
  _sgState.stage1 = (_sgState.stage1 || []).filter(t => t !== ticker);
  _sgState.stage2 = (_sgState.stage2 || []).filter(t => t !== ticker);
  sgRender();
  sgSave();
}

async function sgSave() {
  try {
    await fetch('/api/stagegate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(_sgState),
    });
  } catch(e) {}
}

sgBoot();
"""

# ── Build the code block to append to dashboard.py ───────────────────────────
APPEND = f"""

# ── Stage Gate embedded in Paper Trading (auto-patched) ───────────────────────
_SG_CSS  = {repr(SG_CSS)}
_SG_HTML = {repr(SG_HTML)}
_SG_JS   = {repr(SG_JS)}

PAPER_HTML = PAPER_HTML.replace('</style>', _SG_CSS + '</style>', 1).replace('</main>', _SG_HTML + '</main>', 1)
PAPER_JS   = PAPER_JS + _SG_JS


@app.post("/api/paper/activate")
async def api_paper_activate(request: Request):
    \"\"\"Execute a manual paper buy when dragging a stock to Stage 2.\"\"\"
    import datetime
    body   = await request.json()
    ticker = str(body.get("ticker", "")).upper().strip()
    mode   = body.get("mode", "shares")   # "shares" or "dollars"
    amount = float(body.get("amount", 0))

    if not ticker or amount <= 0:
        return JSONResponse(status_code=400, content={{"error": "invalid input"}})

    try:
        from paper.executor  import PaperExecutor
        from paper.account   import init_paper_db, PaperAccount, PaperPosition, PaperTrade
        from models.database import init_db as _init_db

        _, MainSession = _init_db(config.database.url, echo=False)
        ex    = PaperExecutor(main_db_session_factory=MainSession)
        price = ex._latest_price(ticker)
        if not price or price <= 0:
            return JSONResponse(status_code=400, content={{"error": f"no price data for {{ticker}}"}})

        qty = int(amount / price) if mode == "dollars" else int(amount)
        if qty <= 0:
            return JSONResponse(status_code=400, content={{"error": "quantity rounds to zero"}})

        _, PaperSession = init_paper_db()
        with PaperSession() as session:
            acct = session.query(PaperAccount).first()
            if not acct:
                return JSONResponse(status_code=503, content={{"error": "no paper account"}})
            total = round(qty * price, 2)
            if acct.cash < total:
                return JSONResponse(status_code=400, content={{
                    "error": f"insufficient cash: have ${{acct.cash:,.2f}}, need ${{total:,.2f}}"
                }})
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

        # Persist stage gate state change
        sg = _load_stagegate()
        sg.setdefault("stage1", [])
        sg.setdefault("stage2", [])
        if ticker in sg["stage1"]:
            sg["stage1"].remove(ticker)
        if ticker not in sg["stage2"]:
            sg["stage2"].append(ticker)
        _save_stagegate(sg)

        return {{"status": "ok", "ticker": ticker, "qty": qty, "price": price, "total": total}}

    except Exception as exc:
        import traceback; traceback.print_exc()
        return JSONResponse(status_code=503, content={{"error": str(exc)}})
"""

with open(PATH, 'a', encoding='utf-8') as f:
    f.write(APPEND)

print("Appended OK")

import subprocess, sys
r = subprocess.run([sys.executable, '-m', 'py_compile', PATH], capture_output=True, text=True)
if r.returncode == 0:
    print("Syntax OK")
else:
    print("SYNTAX ERROR:", r.stderr)
