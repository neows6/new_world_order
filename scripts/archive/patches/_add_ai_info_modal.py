"""
Append AI Decision info modal to Paper Trading dashboard.
- Adds info button (ⓘ) to Stage Gate stock cards
- Adds a detailed modal showing narrative, gates passed/failed, and key metrics
"""
PATH = 'C:/Users/neo_w/new_world_order/monitor/dashboard.py'

AI_CSS = """
  /* ── AI Decision modal ──────────────────────────────────────── */
  .sg-info { background: none; border: 1px solid #30363d; color: #58a6ff; cursor: pointer;
             font-size: 12px; padding: 1px 5px; border-radius: 4px; line-height: 1.4; }
  .sg-info:hover { background: rgba(88,166,255,0.1); }
  .ai-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.80);
                display: flex; align-items: flex-start; justify-content: center;
                z-index: 10000; overflow-y: auto; padding: 40px 16px; }
  .ai-modal { background: #161b22; border: 1px solid #30363d; border-radius: 12px;
              width: 100%; max-width: 580px; display: flex; flex-direction: column; gap: 0; }
  .ai-modal-head { padding: 18px 20px 14px; border-bottom: 1px solid #21262d;
                   display: flex; align-items: center; gap: 12px; }
  .ai-modal-ticker { font-size: 20px; font-weight: 700; color: #e6edf3; }
  .ai-modal-price  { font-size: 13px; color: #8b949e; }
  .ai-sig-badge { padding: 3px 10px; border-radius: 12px; font-size: 11px; font-weight: 700;
                  letter-spacing: 0.5px; margin-left: auto; }
  .ai-sig-bull { background: rgba(63,185,80,0.15); color: #3fb950; border: 1px solid #3fb950; }
  .ai-sig-bear { background: rgba(248,81,73,0.15);  color: #f85149; border: 1px solid #f85149; }
  .ai-sig-hold { background: rgba(139,148,158,0.15); color: #8b949e; border: 1px solid #8b949e; }
  .ai-modal-body { padding: 16px 20px; display: flex; flex-direction: column; gap: 14px; }
  .ai-narrative { font-size: 13px; color: #c9d1d9; line-height: 1.6;
                  background: #0d1117; border: 1px solid #21262d; border-radius: 8px;
                  padding: 12px 14px; }
  .ai-blocking { font-size: 12px; color: #f85149; background: rgba(248,81,73,0.08);
                 border: 1px solid rgba(248,81,73,0.25); border-radius: 6px;
                 padding: 8px 12px; }
  .ai-gates { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .ai-gate-col { display: flex; flex-direction: column; gap: 4px; }
  .ai-gate-title { font-size: 10px; font-weight: 700; letter-spacing: 1px;
                   text-transform: uppercase; margin-bottom: 2px; }
  .ai-gate-pass .ai-gate-title { color: #3fb950; }
  .ai-gate-fail .ai-gate-title { color: #f85149; }
  .ai-gate-item { font-size: 11px; color: #c9d1d9; padding: 4px 8px;
                  border-radius: 4px; display: flex; gap: 6px; align-items: flex-start; }
  .ai-gate-pass .ai-gate-item { background: rgba(63,185,80,0.06); }
  .ai-gate-fail .ai-gate-item { background: rgba(248,81,73,0.06); }
  .ai-gate-icon { flex-shrink: 0; margin-top: 1px; }
  .ai-metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(90px, 1fr)); gap: 8px; }
  .ai-metric { background: #0d1117; border: 1px solid #21262d; border-radius: 6px;
               padding: 8px 10px; text-align: center; }
  .ai-metric-label { font-size: 9px; color: #8b949e; letter-spacing: 0.8px;
                     text-transform: uppercase; margin-bottom: 4px; }
  .ai-metric-value { font-size: 14px; font-weight: 700; color: #e6edf3; }
  .ai-modal-foot { padding: 12px 20px; border-top: 1px solid #21262d;
                   display: flex; justify-content: space-between; align-items: center; }
  .ai-gen-time { font-size: 10px; color: #8b949e; }
  .ai-close-btn { padding: 6px 18px; border-radius: 6px; border: 1px solid #30363d;
                  background: #21262d; color: #8b949e; cursor: pointer; font-size: 13px; }
  .ai-close-btn:hover { background: #30363d; }
"""

AI_MODAL_HTML = """
  <!-- AI Decision modal -->
  <div id="ai-overlay" class="ai-overlay" style="display:none" onclick="if(event.target===this) aiClose()">
    <div class="ai-modal">
      <div class="ai-modal-head">
        <div class="ai-modal-ticker" id="ai-ticker"></div>
        <div class="ai-modal-price" id="ai-price"></div>
        <span class="ai-sig-badge" id="ai-sig-badge"></span>
      </div>
      <div class="ai-modal-body">
        <div class="ai-narrative" id="ai-narrative"></div>
        <div class="ai-blocking" id="ai-blocking" style="display:none"></div>
        <div class="ai-gates" id="ai-gates"></div>
        <div class="ai-metrics" id="ai-metrics"></div>
      </div>
      <div class="ai-modal-foot">
        <span class="ai-gen-time" id="ai-gen-time"></span>
        <button class="ai-close-btn" onclick="aiClose()">Close</button>
      </div>
    </div>
  </div>
"""

AI_JS = """
// ── AI Decision info modal ────────────────────────────────────────────────────

// Override sgCardHtml to add the info (ⓘ) button
function sgCardHtml(ticker, stage) {
  const s   = _sgSigs[ticker] || {};
  const sig = (s.signal || 'HOLD').toUpperCase();
  const sc  = sig === 'BUY' || sig === 'STRONG_BUY'   ? 'sig-bull'
            : sig === 'SELL' || sig === 'STRONG_SELL'  ? 'sig-bear' : '';
  const price = s.current_price ? '$' + s.current_price.toFixed(2) : '';
  return '<div class="sg-card" draggable="true" data-ticker="' + ticker + '" data-stage="' + stage + '" '
    + 'ondragstart="sgDragStart(event)" ondragend="sgDragEnd(event)">'
    + '<div class="sg-ticker">' + ticker + '</div>'
    + '<div class="sg-meta">' + price + '</div>'
    + '<span class="sg-signal ' + sc + '">' + sig + '</span>'
    + '<button class="sg-info"  data-ticker="' + ticker + '" onclick="sgShowInfo(this.dataset.ticker)" title="AI Decision">&#9432;</button>'
    + '<button class="sg-remove" data-ticker="' + ticker + '" onclick="sgRemove(this.dataset.ticker)" title="Remove">&#x2715;</button>'
    + '</div>';
}

function sgShowInfo(ticker) {
  const s = _sgSigs[ticker] || {};
  let reason = {};
  try { reason = JSON.parse(s.reasoning || '{}'); } catch(e) {}

  // Header
  document.getElementById('ai-ticker').textContent = ticker;
  const price = s.current_price;
  document.getElementById('ai-price').textContent = price ? '$' + price.toFixed(2) : '';

  const sig = (s.signal || 'HOLD').toUpperCase();
  const badge = document.getElementById('ai-sig-badge');
  badge.textContent = sig;
  badge.className = 'ai-sig-badge ' +
    (sig === 'BUY' || sig === 'STRONG_BUY'   ? 'ai-sig-bull' :
     sig === 'SELL' || sig === 'STRONG_SELL' ? 'ai-sig-bear' : 'ai-sig-hold');

  // Narrative
  document.getElementById('ai-narrative').textContent =
    reason.narrative || 'No AI narrative available yet.';

  // Blocking reason
  const blockEl = document.getElementById('ai-blocking');
  if (reason.blocking_reason) {
    blockEl.textContent = '\\u26d4 Blocked: ' + reason.blocking_reason;
    blockEl.style.display = 'block';
  } else {
    blockEl.style.display = 'none';
  }

  // Gates
  const passed = reason.gates_passed || [];
  const failed = reason.gates_failed || [];
  let gatesHtml = '';
  if (passed.length) {
    gatesHtml += '<div class="ai-gate-col ai-gate-pass">'
      + '<div class="ai-gate-title">&#10003; Gates Passed</div>'
      + passed.map(g => '<div class="ai-gate-item"><span class="ai-gate-icon">&#9679;</span><span>' + g + '</span></div>').join('')
      + '</div>';
  }
  if (failed.length) {
    gatesHtml += '<div class="ai-gate-col ai-gate-fail">'
      + '<div class="ai-gate-title">&#10007; Gates Failed</div>'
      + failed.map(g => '<div class="ai-gate-item"><span class="ai-gate-icon">&#9679;</span><span>' + g + '</span></div>').join('')
      + '</div>';
  }
  document.getElementById('ai-gates').innerHTML = gatesHtml;

  // Metrics
  function pct(v)  { return v != null ? (v * 100).toFixed(1) + '%' : '—'; }
  function f2(v)   { return v != null ? v.toFixed(2) : '—'; }
  const metrics = [
    { label: 'Signal',    value: sig },
    { label: 'Conf',      value: pct(s.confidence) },
    { label: 'P(Bull)',   value: pct(reason.p_bull) },
    { label: 'Kelly',     value: f2(reason.kelly) },
    { label: 'MoS',       value: pct(s.margin_of_safety) },
    { label: 'FUD',       value: f2(s.fud_score) },
    { label: 'Regime',    value: (reason.reynolds_regime || '—').toUpperCase() },
    { label: 'Quantum',   value: (reason.quantum_state  || '—').toUpperCase() },
  ];
  document.getElementById('ai-metrics').innerHTML = metrics.map(m =>
    '<div class="ai-metric"><div class="ai-metric-label">' + m.label + '</div>'
    + '<div class="ai-metric-value">' + m.value + '</div></div>'
  ).join('');

  // Footer timestamp
  document.getElementById('ai-gen-time').textContent =
    s.generated_at ? 'Generated: ' + s.generated_at : '';

  document.getElementById('ai-overlay').style.display = 'flex';
}

function aiClose() {
  document.getElementById('ai-overlay').style.display = 'none';
}

// Close on Escape
document.addEventListener('keydown', e => { if (e.key === 'Escape') aiClose(); });
"""

APPEND = f"""

# ── AI Decision info modal (auto-patched) ─────────────────────────────────────
_AI_CSS        = {repr(AI_CSS)}
_AI_MODAL_HTML = {repr(AI_MODAL_HTML)}
_AI_JS         = {repr(AI_JS)}

PAPER_HTML = PAPER_HTML.replace('</style>', _AI_CSS + '</style>', 1).replace('</main>', _AI_MODAL_HTML + '</main>', 1)
PAPER_JS   = PAPER_JS + _AI_JS
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
