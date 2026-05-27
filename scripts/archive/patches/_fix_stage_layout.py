"""
Patch: Stage gate UX improvements
  1. Move AI Gates bar above Stage Gate
  2. Sankey-style pipeline flow header (counts + flowing arrows)
  3. Sell validation — no position = just move, no sell modal
  4. Buy-more guard — warn if already have a position
"""
PATH = 'C:/Users/neo_w/new_world_order/monitor/dashboard.py'

PIPELINE_CSS = """
  /* ── Pipeline flow header (Sankey-style) ───────────────────── */
  .sg3-pipeline { display: flex; align-items: stretch; margin-bottom: 10px; gap: 0; }
  .sg3-pnode { flex: 1; background: #0d1117; border: 1px solid #21262d; border-radius: 8px;
               padding: 8px 12px; text-align: center; position: relative; }
  .sg3-pnode-s1 { border-color: #58a6ff; }
  .sg3-pnode-s2 { border-color: #d29922; }
  .sg3-pnode-s3 { border-color: #3fb950; }
  .sg3-pcount { font-size: 24px; font-weight: 700; line-height: 1; }
  .sg3-pnode-s1 .sg3-pcount { color: #58a6ff; }
  .sg3-pnode-s2 .sg3-pcount { color: #d29922; }
  .sg3-pnode-s3 .sg3-pcount { color: #3fb950; }
  .sg3-pname  { font-size: 10px; color: #8b949e; font-weight: 600; letter-spacing: 0.5px;
                text-transform: uppercase; margin-top: 3px; }
  .sg3-pflow  { display: flex; align-items: center; justify-content: center;
                padding: 0 4px; flex-shrink: 0; position: relative; }
  .sg3-pflow-line { flex: 1; height: 2px; background: linear-gradient(90deg, #30363d 0%, #30363d 100%);
                    position: relative; min-width: 28px; }
  .sg3-pflow-line::after { content: '\\25b6'; position: absolute; right: -6px; top: 50%;
                            transform: translateY(-50%); color: #30363d; font-size: 10px; }
  .sg3-pflow-label { position: absolute; top: -16px; left: 50%; transform: translateX(-50%);
                     font-size: 9px; color: #484f58; white-space: nowrap; font-weight: 600;
                     letter-spacing: 0.3px; text-transform: uppercase; }
"""

STAGE_FIXES_IIFE_JS = """
// ── Pipeline flow header ──────────────────────────────────────────────────────
function sg3BuildPipeline() {
  const sg3Sec = document.getElementById('sg3-section');
  if (!sg3Sec || document.getElementById('sg3-pipeline')) return;
  const div = document.createElement('div');
  div.className = 'sg3-pipeline'; div.id = 'sg3-pipeline';
  div.innerHTML =
    '<div class="sg3-pnode sg3-pnode-s1">'
      + '<div class="sg3-pcount" id="sg3-pc-1">0</div>'
      + '<div class="sg3-pname">Monitoring</div>'
    + '</div>'
    + '<div class="sg3-pflow"><div class="sg3-pflow-line"></div>'
      + '<span class="sg3-pflow-label">Promote AI</span></div>'
    + '<div class="sg3-pnode sg3-pnode-s2">'
      + '<div class="sg3-pcount" id="sg3-pc-2">0</div>'
      + '<div class="sg3-pname">Active AI</div>'
    + '</div>'
    + '<div class="sg3-pflow"><div class="sg3-pflow-line"></div>'
      + '<span class="sg3-pflow-label">Execute Buy</span></div>'
    + '<div class="sg3-pnode sg3-pnode-s3">'
      + '<div class="sg3-pcount" id="sg3-pc-3">0</div>'
      + '<div class="sg3-pname">Positions</div>'
    + '</div>';
  // Insert pipeline header at top of section (after title)
  const title = sg3Sec.querySelector('.section-title');
  if (title) title.insertAdjacentElement('afterend', div);
  else sg3Sec.prepend(div);
}
window.sg3BuildPipeline = sg3BuildPipeline;

function sg3UpdatePipelineCounts() {
  const c1 = document.getElementById('sg3-pc-1');
  const c2 = document.getElementById('sg3-pc-2');
  const c3 = document.getElementById('sg3-pc-3');
  if (c1) c1.textContent = (_sg3.stage1||[]).length;
  if (c2) c2.textContent = (_sg3.stage2||[]).length;
  if (c3) c3.textContent = (_sg3.stage3||[]).length;
}
window.sg3UpdatePipelineCounts = sg3UpdatePipelineCounts;

// Override sg3Render to also update pipeline counts
const _origSg3Render = typeof sg3Render === 'function' ? sg3Render : null;
function sg3Render() {
  if (_origSg3Render) _origSg3Render();
  sg3UpdatePipelineCounts();
}
window.sg3Render = sg3Render;

// ── Sell validation: only open sell modal if position exists ──────────────────
function sg3OpenSell(ticker, targetStage) {
  _sg3sellTicker = ticker;
  _sg3sellTarget = targetStage || '1';
  const pos = _sg3pos[ticker] || {};

  // No open position — just move the card, no sell needed
  if (!pos.qty || pos.qty <= 0) {
    const from = Object.keys({'1':_sg3.stage1,'2':_sg3.stage2,'3':_sg3.stage3})
      .find(k => (_sg3['stage'+k]||[]).includes(ticker));
    if (from) sg3MoveLocal(ticker, from, _sg3sellTarget);
    return;
  }

  document.getElementById('sg3-sell-title').textContent = 'Sell ' + ticker;
  const price = (_sg3sigs[ticker] || {}).current_price || pos.cur_price || pos.avg_cost || 0;
  document.getElementById('sg3-sell-pos').textContent =
    pos.qty + ' shares \\u00b7 avg $' + (pos.avg_cost||0).toFixed(2) + ' \\u00b7 now $' + price.toFixed(2);
  document.getElementById('sg3sm-all').checked = true;
  document.getElementById('sg3-sell-qty').style.display = 'none';
  document.getElementById('sg3-sell-qty').value = '';
  const dest = targetStage === '1' ? 'Stage 1 (Monitoring)' : 'Stage 2 (Active AI)';
  document.getElementById('sg3-sell-dest').textContent = 'After sell: move to ' + dest;
  sg3UpdateSellHint();
  document.getElementById('sg3-sell-overlay').style.display = 'flex';
}
window.sg3OpenSell = sg3OpenSell;

// ── Buy-more guard: warn if already have a position ───────────────────────────
const _origSg3BuyConfirm = typeof sg3BuyConfirm === 'function' ? sg3BuyConfirm : null;

function sg3OpenBuy(ticker, targetStage, fromStage) {
  _sg3pendTicker = ticker;
  _sg3pendTarget = targetStage || '3';
  _sg3pendFrom   = fromStage || _sg3dragF || null;

  const existing = _sg3pos[ticker];
  if (existing && existing.qty > 0) {
    // Already have a position — ask to confirm "buy more"
    const price = (_sg3sigs[ticker] || {}).current_price || existing.cur_price || 0;
    const ok = confirm(
      ticker + ': you already hold ' + existing.qty + ' shares'
      + (existing.avg_cost ? ' (avg $' + existing.avg_cost.toFixed(2) + ')' : '')
      + '.\\n\\nBuy MORE shares now? Click OK to continue or Cancel to skip.'
    );
    if (!ok) return;
  }

  const s = _sg3sigs[ticker] || {};
  document.getElementById('sg3-modal-title').textContent = 'Buy ' + ticker;
  document.getElementById('sg3-modal-price').textContent =
    s.current_price ? 'Current price: $' + s.current_price.toFixed(2) : 'Price not available';
  document.getElementById('sg3-mode-shares').checked = true;
  document.getElementById('sg3-amount').value = '';
  document.getElementById('sg3-modal-hint').innerHTML = '&nbsp;';
  document.getElementById('sg3-overlay').style.display = 'flex';
  setTimeout(() => document.getElementById('sg3-amount').focus(), 60);
  document.getElementById('sg3-act-btn').onclick = sg3BuyConfirm;
  document.getElementById('sg3-act-btn').textContent = '\\u25b6 Buy' + (existing && existing.qty > 0 ? ' More' : '');
}
window.sg3OpenBuy = sg3OpenBuy;

// ── Move thresh-bar before sg3-section instead of after ──────────────────────
const _origSg3Boot2 = typeof sg3Boot === 'function' ? sg3Boot : null;
function sg3Boot() {
  if (_origSg3Boot2) _origSg3Boot2();
  sg3BuildPipeline();
  sg3UpdatePipelineCounts();
  // Relocate thresh-bar to above sg3-section once it exists
  setTimeout(() => {
    const bar = document.getElementById('thresh-bar');
    const sg3 = document.getElementById('sg3-section');
    if (bar && sg3 && sg3.parentNode) {
      sg3.parentNode.insertBefore(bar, sg3);
    }
  }, 700);
}
window.sg3Boot = sg3Boot;
"""

APPEND = """

# ── Stage layout fixes ────────────────────────────────────────────────────────
PAPER_HTML = PAPER_HTML.replace('</style>', """ + repr(PIPELINE_CSS) + """ + '</style>', 1)

_STAGE_FIXES_JS = """ + repr(STAGE_FIXES_IIFE_JS) + """
PAPER_JS = PAPER_JS.replace(
    'sg3Boot();\\n\\n})(); // end IIFE',
    _STAGE_FIXES_JS + 'sg3Boot();\\n\\n})(); // end IIFE'
)
"""

with open(PATH, 'a', encoding='utf-8') as f:
    f.write(APPEND)

print("Appended OK")

import subprocess, sys
r = subprocess.run([sys.executable, '-m', 'py_compile', PATH], capture_output=True, text=True)
print("Syntax OK" if r.returncode == 0 else "SYNTAX ERROR:\n" + r.stderr)
