"""
Upgrade Paper Trading Stage Gate from 2-stage to 3-stage.
Appends to dashboard.py:
  - /api/paper/sell         — manual paper sell
  - /api/paper/activate-ai  — Stage 1→2 (activate AI, no immediate buy)
  - 3-stage UI override (CSS injection + DOM replacement + full JS rewrite via JS)
"""
PATH = 'C:/Users/neo_w/new_world_order/monitor/dashboard.py'

# ── New CSS (injected via JS into <head>) ─────────────────────────────────────
SG3_CSS = """
  /* ── Stage Gate 3-col ─────────────────────────────────────── */
  .sg3-wrap  { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 0; }
  .sg3-col   { display: flex; flex-direction: column; border-right: 1px solid #21262d; min-width: 0; }
  .sg3-col:last-child { border-right: none; }
  .sg3-hd    { padding: 10px 14px; background: #0d1117; border-bottom: 1px solid #21262d;
               display: flex; align-items: center; gap: 8px; }
  .sg3-c1 .sg3-hd  { border-top: 3px solid #8b949e; }
  .sg3-c2 .sg3-hd  { border-top: 3px solid #58a6ff; }
  .sg3-c3 .sg3-hd  { border-top: 3px solid #3fb950; }
  .sg3-title { font-size: 12px; font-weight: 700; }
  .sg3-c1 .sg3-title { color: #8b949e; }
  .sg3-c2 .sg3-title { color: #58a6ff; }
  .sg3-c3 .sg3-title { color: #3fb950; }
  .sg3-sub   { font-size: 10px; color: #8b949e; margin-top: 2px; }
  .sg3-cnt   { margin-left: auto; font-size: 11px; color: #8b949e;
               background: #21262d; padding: 2px 7px; border-radius: 10px; }
  .sg3-zone  { flex: 1; min-height: 80px; padding: 8px;
               display: flex; flex-direction: column; gap: 6px; }
  .sg3-zone.drag-over { background: rgba(88,166,255,0.05);
                        outline: 2px dashed #58a6ff; outline-offset: -3px; border-radius: 4px; }
  .sg3-card  { background: #0d1117; border: 1px solid #21262d; border-radius: 6px;
               padding: 7px 10px; cursor: grab; display: flex; align-items: center;
               gap: 6px; user-select: none; transition: border-color 0.15s; flex-wrap: wrap; }
  .sg3-card:hover   { border-color: #58a6ff; }
  .sg3-card:active  { cursor: grabbing; }
  .sg3-card.dragging { opacity: 0.4; }
  .sg3-c2 .sg3-card { border-left: 3px solid #58a6ff; }
  .sg3-c3 .sg3-card { border-left: 3px solid #3fb950; }
  .sg3-tick  { font-size: 13px; font-weight: 700; min-width: 52px; }
  .sg3-meta  { font-size: 10px; color: #8b949e; flex: 1; min-width: 50px; }
  .sg3-pnl   { font-size: 10px; font-weight: 600; }
  .sg3-sig   { font-size: 10px; font-weight: 600; }
  .sg3-acts  { display: flex; gap: 3px; margin-left: auto; }
  .sg3-btn   { background: none; border: 1px solid #30363d; color: #8b949e;
               cursor: pointer; font-size: 11px; padding: 2px 6px;
               border-radius: 4px; line-height: 1.4; white-space: nowrap; }
  .sg3-btn-buy  { border-color: #3fb950; color: #3fb950; }
  .sg3-btn-buy:hover  { background: rgba(63,185,80,0.12); }
  .sg3-btn-sell { border-color: #f85149; color: #f85149; }
  .sg3-btn-sell:hover { background: rgba(248,81,73,0.12); }
  .sg3-btn-ai   { border-color: #58a6ff; color: #58a6ff; }
  .sg3-btn-ai:hover   { background: rgba(88,166,255,0.12); }
  .sg3-btn-info { border-color: #30363d; color: #58a6ff; }
  .sg3-btn-info:hover { background: rgba(88,166,255,0.08); }
  .sg3-btn-rm   { border-color: transparent; color: #8b949e; }
  .sg3-btn-rm:hover   { color: #f85149; background: rgba(248,81,73,0.08); }
  .sg3-hint  { color: #8b949e; font-size: 11px; text-align: center; padding: 18px 8px;
               border: 2px dashed #21262d; border-radius: 6px; font-style: italic; }
  .sg3-status { font-size: 9px; font-weight: 700; letter-spacing: 0.5px;
                padding: 1px 5px; border-radius: 8px; }
  .sg3-status-bought { background: rgba(63,185,80,0.2); color: #3fb950; }
  .sg3-status-sold   { background: rgba(248,81,73,0.2);  color: #f85149; }
  /* Sell modal */
  .sg3-sell-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.75);
                      display: flex; align-items: center; justify-content: center; z-index: 10001; }
  .sg3-sell-box { background: #161b22; border: 1px solid #30363d; border-radius: 10px;
                  padding: 24px; width: 340px; display: flex; flex-direction: column; gap: 14px; }
  .sg3-sell-box h3 { font-size: 15px; color: #e6edf3; }
  .sg3-sell-pos  { font-size: 12px; color: #8b949e; }
  .sg3-sell-dest { font-size: 11px; color: #8b949e; }
  @media (max-width: 700px) {
    .sg3-wrap { grid-template-columns: 1fr; }
    .sg3-col  { border-right: none; border-bottom: 1px solid #21262d; }
  }
"""

# ── 3-stage HTML section (replaces old 2-col via JS DOM swap) ─────────────────
SG3_HTML = """
<div id="sg3-section" class="section">
  <div class="section-title">&#127760; Stage Gate &mdash; Stock Pipeline</div>
  <div class="sg3-wrap">
    <div class="sg3-col sg3-c1">
      <div class="sg3-hd">
        <div><div class="sg3-title">&#128203; Stage 1 &mdash; Monitoring</div>
             <div class="sg3-sub">Watching only &middot; no trading</div></div>
        <span class="sg3-cnt" id="sg3-cnt-1">0</span>
      </div>
      <div class="sg3-zone" id="sg3-zone-1"
           ondragover="sg3Over(event,'1')" ondragleave="sg3Leave('1')" ondrop="sg3Drop(event,'1')">
        <div class="sg3-hint">Stocks you are watching</div>
      </div>
    </div>
    <div class="sg3-col sg3-c2">
      <div class="sg3-hd">
        <div><div class="sg3-title">&#129302; Stage 2 &mdash; Active AI</div>
             <div class="sg3-sub">AI pipeline &middot; auto-buys on signal</div></div>
        <span class="sg3-cnt" id="sg3-cnt-2">0</span>
      </div>
      <div class="sg3-zone" id="sg3-zone-2"
           ondragover="sg3Over(event,'2')" ondragleave="sg3Leave('2')" ondrop="sg3Drop(event,'2')">
        <div class="sg3-hint">Drag here to activate AI trading</div>
      </div>
    </div>
    <div class="sg3-col sg3-c3">
      <div class="sg3-hd">
        <div><div class="sg3-title">&#128200; Stage 3 &mdash; Open Positions</div>
             <div class="sg3-sub">Live positions &middot; drag left to sell</div></div>
        <span class="sg3-cnt" id="sg3-cnt-3">0</span>
      </div>
      <div class="sg3-zone" id="sg3-zone-3"
           ondragover="sg3Over(event,'3')" ondragleave="sg3Leave('3')" ondrop="sg3Drop(event,'3')">
        <div class="sg3-hint">Positions appear here after a buy</div>
      </div>
    </div>
  </div>
</div>
<!-- Sell modal -->
<div id="sg3-sell-overlay" class="sg3-sell-overlay" style="display:none"
     onclick="if(event.target===this) sg3SellCancel()">
  <div class="sg3-sell-box">
    <h3 id="sg3-sell-title">Sell</h3>
    <p class="sg3-sell-pos" id="sg3-sell-pos"></p>
    <div class="sg-toggle">
      <label><input type="radio" name="sg3sm" id="sg3sm-all" value="all" checked
                    onchange="sg3SellModeChange()"> Sell all</label>
      <label><input type="radio" name="sg3sm" id="sg3sm-part" value="partial"
                    onchange="sg3SellModeChange()"> Partial</label>
    </div>
    <input class="sg-amount-input" id="sg3-sell-qty" type="number" min="1" step="1"
           placeholder="Shares to sell..." style="display:none"
           oninput="sg3UpdateSellHint()" onkeydown="if(event.key==='Enter') sg3SellConfirm()">
    <p class="sg-modal-hint" id="sg3-sell-hint">&nbsp;</p>
    <p class="sg3-sell-dest" id="sg3-sell-dest"></p>
    <div class="sg-modal-btns">
      <button class="sg-cancel-btn" onclick="sg3SellCancel()">Cancel</button>
      <button class="sg-act-btn" id="sg3-sell-btn"
              style="border-color:#f85149;background:rgba(248,81,73,0.15);color:#f85149"
              onclick="sg3SellConfirm()">&#x1f4b8; Sell</button>
    </div>
  </div>
</div>
"""

# ── Full 3-stage JS ───────────────────────────────────────────────────────────
SG3_JS = """
// ════════════════════════════════════════════════════════════════════════════
// Stage Gate 3-Stage (overrides old 2-stage code)
// ════════════════════════════════════════════════════════════════════════════
(function() {

// ── Inject CSS ────────────────────────────────────────────────────────────────
const _sg3style = document.createElement('style');
_sg3style.textContent = """ + repr(SG3_CSS) + """;
document.head.appendChild(_sg3style);

// ── Replace old Stage Gate section with 3-col ─────────────────────────────────
(function injectHtml() {
  // Find old section (has sg-stages or sg3-section)
  const old = document.getElementById('sg3-section')
           || Array.from(document.querySelectorAll('.section'))
                .find(s => s.querySelector('.section-title')
                        && s.querySelector('.section-title').textContent.includes('Stage Gate'));
  if (!old) {
    // Not rendered yet — insert before first .section
    const main = document.querySelector('main');
    if (main) {
      const firstSec = main.querySelector('.section');
      if (firstSec) {
        firstSec.insertAdjacentHTML('beforebegin', """ + repr(SG3_HTML) + """);
      } else {
        main.insertAdjacentHTML('beforeend', """ + repr(SG3_HTML) + """);
      }
    }
  } else if (!document.getElementById('sg3-section')) {
    old.outerHTML = """ + repr(SG3_HTML) + """;
  }
  // Inject sell modal if not present
  if (!document.getElementById('sg3-sell-overlay')) {
    document.body.insertAdjacentHTML('beforeend', """ + repr(SG3_HTML.split('<!-- Sell modal -->')[1] if '<!-- Sell modal -->' in SG3_HTML else '') + """);
  }
})();

// ── State ─────────────────────────────────────────────────────────────────────
let _sg3 = { stage1: [], stage2: [], stage3: [] };
let _sg3sigs = {};
let _sg3pos  = {};   // ticker -> position data {qty, avg_cost, cur_price, pnl, pnl_pct}
let _sg3dragT = null;
let _sg3dragF = null;
let _sg3pendTicker  = null;  // pending for buy modal
let _sg3pendTarget  = null;  // target stage for buy
let _sg3sellTicker  = null;  // pending for sell modal
let _sg3sellTarget  = null;  // target stage after sell
let _sg3recentTrades = {};   // ticker -> 'BOUGHT'|'SOLD' (shown briefly)

// ── Boot ──────────────────────────────────────────────────────────────────────
async function sg3Boot() {
  try {
    const sigs = await fetch('/api/signals').then(r => r.json());
    (sigs || []).forEach(s => { _sg3sigs[s.ticker] = s; });
  } catch(e) {}
  try { _sg3 = await fetch('/api/stagegate').then(r => r.json()); } catch(e) {}
  _sg3.stage1 = _sg3.stage1 || [];
  _sg3.stage2 = _sg3.stage2 || [];
  _sg3.stage3 = _sg3.stage3 || [];
  sg3Render();
}

// Override old sgBoot to be a no-op (sg3Boot takes over)
window.sgBoot = function() {};

// ── Sync positions from account data ─────────────────────────────────────────
function sg3SyncPositions(positions) {
  _sg3pos = {};
  (positions || []).forEach(p => { _sg3pos[p.ticker] = p; });

  // Auto-promote: any open position not in stage3 → move to stage3
  let changed = false;
  Object.keys(_sg3pos).forEach(ticker => {
    if (!_sg3.stage3.includes(ticker)) {
      for (const k of ['stage1', 'stage2']) {
        const i = _sg3[k].indexOf(ticker);
        if (i !== -1) { _sg3[k].splice(i, 1); }
      }
      _sg3.stage3.push(ticker);
      changed = true;
    }
  });
  // Auto-demote: stage3 ticker with no position → back to stage2
  _sg3.stage3 = _sg3.stage3.filter(ticker => {
    if (!_sg3pos[ticker]) {
      if (!_sg3.stage2.includes(ticker) && !_sg3.stage1.includes(ticker)) {
        _sg3.stage2.push(ticker);
      }
      changed = true;
      return false;
    }
    return true;
  });
  if (changed) sg3Save();
  sg3Render();
}

// ── Render ────────────────────────────────────────────────────────────────────
function sg3Render() {
  sg3RenderZone('1', _sg3.stage1);
  sg3RenderZone('2', _sg3.stage2);
  sg3RenderZone('3', _sg3.stage3);
  document.getElementById('sg3-cnt-1').textContent = _sg3.stage1.length;
  document.getElementById('sg3-cnt-2').textContent = _sg3.stage2.length;
  document.getElementById('sg3-cnt-3').textContent = _sg3.stage3.length;
}

function sg3RenderZone(stage, tickers) {
  const zone = document.getElementById('sg3-zone-' + stage);
  if (!zone) return;
  if (!tickers.length) {
    const hints = {
      '1': 'Stocks you are watching',
      '2': 'Drag here to activate AI trading',
      '3': 'Positions appear here after a buy',
    };
    zone.innerHTML = '<div class="sg3-hint">' + hints[stage] + '</div>';
    return;
  }
  zone.innerHTML = tickers.map(t => sg3CardHtml(t, stage)).join('');
}

function sg3CardHtml(ticker, stage) {
  const s    = _sg3sigs[ticker] || {};
  const pos  = _sg3pos[ticker]  || {};
  const sig  = (s.signal || 'HOLD').toUpperCase();
  const sc   = sig === 'BUY' || sig === 'STRONG_BUY'  ? 'sig-bull'
             : sig === 'SELL'|| sig === 'STRONG_SELL' ? 'sig-bear' : '';
  const price = s.current_price ? '$' + s.current_price.toFixed(2) : '';
  const recent = _sg3recentTrades[ticker];

  let meta = price;
  let pnlHtml = '';
  if (stage === '3' && pos.qty) {
    const pnlCls = (pos.pnl || 0) >= 0 ? 'up' : 'dn';
    const pnlStr = ((pos.pnl || 0) >= 0 ? '+' : '') + '$' + Math.abs(pos.pnl || 0).toFixed(0);
    const pctStr = ((pos.pnl_pct || 0) >= 0 ? '+' : '') + (pos.pnl_pct || 0).toFixed(1) + '%';
    meta = price + (price ? ' · ' : '') + pos.qty + ' sh @ $' + (pos.avg_cost || 0).toFixed(2);
    pnlHtml = '<span class="sg3-pnl ' + pnlCls + '">' + pnlStr + ' (' + pctStr + ')</span>';
  }

  let statusHtml = '';
  if (recent) {
    statusHtml = '<span class="sg3-status sg3-status-' + recent.toLowerCase() + '">' + recent + '</span>';
  }

  // Action buttons differ per stage
  let btns = '<div class="sg3-acts">';
  btns += '<button class="sg3-btn sg3-btn-info" data-ticker="' + ticker + '" onclick="sgShowInfo(this.dataset.ticker)" title="AI Analysis">&#9432;</button>';
  if (stage === '1') {
    btns += '<button class="sg3-btn sg3-btn-ai"  data-ticker="' + ticker + '" onclick="sg3ActivateAI(this.dataset.ticker)"  title="Activate AI">AI</button>';
    btns += '<button class="sg3-btn sg3-btn-buy" data-ticker="' + ticker + '" onclick="sg3OpenBuy(this.dataset.ticker,\\'3\\')" title="Buy now">Buy</button>';
  } else if (stage === '2') {
    btns += '<button class="sg3-btn sg3-btn-buy" data-ticker="' + ticker + '" onclick="sg3OpenBuy(this.dataset.ticker,\\'3\\')" title="Buy now">Buy</button>';
  } else if (stage === '3') {
    btns += '<button class="sg3-btn sg3-btn-buy"  data-ticker="' + ticker + '" onclick="sg3OpenBuy(this.dataset.ticker,\\'3\\')"  title="Add to position">Buy+</button>';
    btns += '<button class="sg3-btn sg3-btn-sell" data-ticker="' + ticker + '" onclick="sg3OpenSell(this.dataset.ticker,\\'2\\')" title="Sell">Sell</button>';
  }
  btns += '<button class="sg3-btn sg3-btn-rm" data-ticker="' + ticker + '" onclick="sg3Remove(this.dataset.ticker)" title="Remove">&#x2715;</button>';
  btns += '</div>';

  return '<div class="sg3-card" draggable="true" data-ticker="' + ticker + '" data-stage="' + stage + '" '
    + 'ondragstart="sg3DragStart(event)" ondragend="sg3DragEnd(event)">'
    + '<div class="sg3-tick">' + ticker + '</div>'
    + '<div class="sg3-meta">' + meta + '</div>'
    + pnlHtml
    + statusHtml
    + '<span class="sg3-sig ' + sc + '">' + sig + '</span>'
    + btns
    + '</div>';
}

// ── Drag & drop ───────────────────────────────────────────────────────────────
function sg3DragStart(e) {
  _sg3dragT = e.currentTarget.dataset.ticker;
  _sg3dragF = e.currentTarget.dataset.stage;
  e.currentTarget.classList.add('dragging');
  e.dataTransfer.effectAllowed = 'move';
}
function sg3DragEnd(e) { e.currentTarget.classList.remove('dragging'); }
function sg3Over(e, stage) {
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  const z = document.getElementById('sg3-zone-' + stage);
  if (z) z.classList.add('drag-over');
}
function sg3Leave(stage) {
  const z = document.getElementById('sg3-zone-' + stage);
  if (z) z.classList.remove('drag-over');
}

function sg3Drop(e, toStage) {
  e.preventDefault();
  const z = document.getElementById('sg3-zone-' + toStage);
  if (z) z.classList.remove('drag-over');
  if (!_sg3dragT || _sg3dragF === toStage) return;
  const from = _sg3dragF, ticker = _sg3dragT;

  // Moving to Stage 1 from Stage 2 or 3 → sell popup (if has position)
  if (toStage === '1' && (from === '2' || from === '3')) {
    if (_sg3pos[ticker]) {
      sg3OpenSell(ticker, '1');
    } else {
      sg3MoveLocal(ticker, from, '1');
    }
    return;
  }
  // Moving Stage 3 → Stage 2 → sell popup
  if (toStage === '2' && from === '3') {
    sg3OpenSell(ticker, '2');
    return;
  }
  // Stage 1 → Stage 2: activate for AI (no buy)
  if (toStage === '2' && from === '1') {
    sg3ActivateAI(ticker);
    return;
  }
  // Stage 1/2 → Stage 3: buy popup
  if (toStage === '3') {
    sg3OpenBuy(ticker, '3', from);
    return;
  }
  sg3MoveLocal(ticker, from, toStage);
}

// ── AI activation (Stage 1 → Stage 2, no immediate buy) ──────────────────────
async function sg3ActivateAI(ticker) {
  try {
    await fetch('/api/paper/activate-ai', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ ticker }),
    });
  } catch(e) {}
  sg3MoveLocal(ticker, '1', '2');
}

// ── Buy modal ─────────────────────────────────────────────────────────────────
function sg3OpenBuy(ticker, targetStage, fromStage) {
  _sg3pendTicker = ticker;
  _sg3pendTarget = targetStage || '3';
  _sg3pendFrom   = fromStage || _sg3dragF || null;
  // Reuse existing buy modal (sg-overlay) from the previous embed
  const s = _sg3sigs[ticker] || {};
  document.getElementById('sg-modal-title').textContent = 'Buy ' + ticker;
  document.getElementById('sg-modal-price').textContent =
    s.current_price ? 'Current price: $' + s.current_price.toFixed(2) : 'Price not available';
  document.getElementById('sg-mode-shares').checked = true;
  document.getElementById('sg-amount').value = '';
  document.getElementById('sg-modal-hint').innerHTML = '&nbsp;';
  document.getElementById('sg-overlay').style.display = 'flex';
  setTimeout(() => document.getElementById('sg-amount').focus(), 60);
  // Swap confirm handler
  document.getElementById('sg-act-btn').onclick = sg3BuyConfirm;
  document.getElementById('sg-act-btn').textContent = '\\u25b6 Buy';
}

function sgUpdateHint() {  // keep existing hint updater working
  const mode  = document.querySelector('input[name="sg-mode"]:checked').value;
  const amt   = parseFloat(document.getElementById('sg-amount').value);
  const price = (_sg3sigs[_sg3pendTicker] || {}).current_price;
  const hint  = document.getElementById('sg-modal-hint');
  if (!amt || amt <= 0) { hint.innerHTML = '&nbsp;'; return; }
  if (mode === 'shares') {
    hint.textContent = price
      ? 'Total \\u2248 $' + (amt * price).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2})
      : amt + ' shares';
  } else {
    const sh = price ? Math.floor(amt / price) : null;
    hint.textContent = sh != null ? sh + ' shares @ $' + price.toFixed(2) : '$' + amt;
  }
}

async function sg3BuyConfirm() {
  const ticker = _sg3pendTicker;
  const mode   = document.querySelector('input[name="sg-mode"]:checked').value;
  const amount = parseFloat(document.getElementById('sg-amount').value);
  if (!ticker || !amount || amount <= 0) return;

  const btn = document.getElementById('sg-act-btn');
  btn.disabled = true; btn.textContent = 'Buying\\u2026';

  try {
    const r = await fetch('/api/paper/activate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ ticker, mode, amount }),
    }).then(res => res.json());

    if (r.status === 'ok') {
      if (_sg3pendFrom) sg3RemoveFromStage(_sg3pendTicker, _sg3pendFrom);
      sg3MoveLocal(ticker, null, '3');
      document.getElementById('sg-overlay').style.display = 'none';
      _sg3recentTrades[ticker] = 'BOUGHT';
      setTimeout(() => { delete _sg3recentTrades[ticker]; sg3Render(); }, 8000);
      load();
    } else {
      alert('Buy failed: ' + (r.error || 'unknown'));
    }
  } catch(e) { alert('Error: ' + e.message); }
  finally {
    btn.disabled = false; btn.textContent = '\\u25b6 Start Trading';
    btn.onclick  = sgModalConfirm;  // restore original handler
  }
}

// ── Sell modal ────────────────────────────────────────────────────────────────
function sg3OpenSell(ticker, targetStage) {
  _sg3sellTicker = ticker;
  _sg3sellTarget = targetStage || '1';
  const pos = _sg3pos[ticker] || {};
  const price = (_sg3sigs[ticker] || {}).current_price || pos.cur_price || pos.avg_cost || 0;

  document.getElementById('sg3-sell-title').textContent = 'Sell ' + ticker;
  document.getElementById('sg3-sell-pos').textContent =
    pos.qty
      ? pos.qty + ' shares · avg cost $' + (pos.avg_cost || 0).toFixed(2) + ' · current $' + price.toFixed(2)
      : 'No open position';
  document.getElementById('sg3sm-all').checked = true;
  document.getElementById('sg3-sell-qty').style.display = 'none';
  document.getElementById('sg3-sell-qty').value = '';
  const dest = targetStage === '1' ? 'Stage 1 (Monitoring)' : 'Stage 2 (Active AI)';
  document.getElementById('sg3-sell-dest').textContent = 'After sell: move to ' + dest;
  sg3UpdateSellHint();
  document.getElementById('sg3-sell-overlay').style.display = 'flex';
  if (pos.qty) setTimeout(() => document.getElementById('sg3-sell-overlay').focus?.(), 60);
}

function sg3SellModeChange() {
  const partial = document.getElementById('sg3sm-part').checked;
  document.getElementById('sg3-sell-qty').style.display = partial ? 'block' : 'none';
  if (partial) document.getElementById('sg3-sell-qty').focus();
  sg3UpdateSellHint();
}

function sg3UpdateSellHint() {
  const pos   = _sg3pos[_sg3sellTicker] || {};
  const price = (_sg3sigs[_sg3sellTicker] || {}).current_price || pos.cur_price || 0;
  const hint  = document.getElementById('sg3-sell-hint');
  const mode  = document.querySelector('input[name="sg3sm"]:checked')?.value || 'all';
  const qty   = mode === 'all' ? (pos.qty || 0) : parseFloat(document.getElementById('sg3-sell-qty').value) || 0;
  if (!qty || !price) { hint.innerHTML = '&nbsp;'; return; }
  hint.textContent = 'Proceeds \\u2248 $' + (qty * price).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
}

function sg3SellCancel() {
  document.getElementById('sg3-sell-overlay').style.display = 'none';
  _sg3sellTicker = null;
}

async function sg3SellConfirm() {
  const ticker      = _sg3sellTicker;
  const targetStage = _sg3sellTarget;
  if (!ticker) return;

  const mode = document.querySelector('input[name="sg3sm"]:checked')?.value || 'all';
  const qty  = mode === 'partial' ? parseFloat(document.getElementById('sg3-sell-qty').value) : null;

  const btn = document.getElementById('sg3-sell-btn');
  btn.disabled = true; btn.textContent = 'Selling\\u2026';

  try {
    const r = await fetch('/api/paper/sell', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ ticker, mode, qty, target_stage: targetStage }),
    }).then(res => res.json());

    if (r.status === 'ok') {
      sg3MoveLocal(ticker, '3', targetStage);
      document.getElementById('sg3-sell-overlay').style.display = 'none';
      _sg3sellTicker = null;
      if (!r.no_position) {
        _sg3recentTrades[ticker] = 'SOLD';
        setTimeout(() => { delete _sg3recentTrades[ticker]; sg3Render(); }, 8000);
      }
      load();
    } else {
      alert('Sell failed: ' + (r.error || 'unknown'));
    }
  } catch(e) { alert('Error: ' + e.message); }
  finally { btn.disabled = false; btn.textContent = '\\u1f4b8 Sell'; }
}

// ── Local state helpers ───────────────────────────────────────────────────────
function sg3RemoveFromStage(ticker, stage) {
  const arr = _sg3['stage' + stage];
  if (!arr) return;
  const i = arr.indexOf(ticker);
  if (i !== -1) arr.splice(i, 1);
}

function sg3MoveLocal(ticker, from, to) {
  if (from) sg3RemoveFromStage(ticker, from);
  const toArr = _sg3['stage' + to];
  if (toArr && !toArr.includes(ticker)) toArr.push(ticker);
  sg3Render();
  sg3Save();
}

function sg3Remove(ticker) {
  ['stage1','stage2','stage3'].forEach(k => {
    _sg3[k] = (_sg3[k] || []).filter(t => t !== ticker);
  });
  sg3Render();
  sg3Save();
}

async function sg3Save() {
  try {
    await fetch('/api/stagegate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(_sg3),
    });
  } catch(e) {}
}

// ── Hook into existing load() to sync Stage 3 ────────────────────────────────
const _origLoad = load;
window.load = async function() {
  await _origLoad();
  try {
    const acct = await fetch('/api/paper/account').then(r => r.json());
    sg3SyncPositions(acct.positions || []);
  } catch(e) {}
};

// Escape closes sell modal too
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { sg3SellCancel(); aiClose(); }
});

// Boot
sg3Boot();

})(); // end IIFE
"""

BACKEND = '''

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
'''

# ── Build the append block ─────────────────────────────────────────────────────
APPEND = f"""

# ── 3-Stage UI override (auto-patched) ────────────────────────────────────────
_SG3_JS  = {repr(SG3_JS)}
PAPER_JS = PAPER_JS + _SG3_JS
""" + BACKEND

with open(PATH, 'a', encoding='utf-8') as f:
    f.write(APPEND)

print("Appended OK")

import subprocess, sys
r = subprocess.run([sys.executable, '-m', 'py_compile', PATH], capture_output=True, text=True)
if r.returncode == 0:
    print("Syntax OK")
else:
    print("SYNTAX ERROR:", r.stderr)
