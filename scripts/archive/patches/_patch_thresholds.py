"""
Patch: Replace 3-box swim lanes with a thin AI threshold control bar.
  1. Hide swim-wrap via CSS (avoid fragile string matching)
  2. Inject threshold bar (H/M/L toggle + chips) via JS after Stage Gate
  3. Add gate toggles (Re, Ens, QSt, R/R, Kal) to AI modal
  4. Backend: /api/paper/set-thresh and /api/paper/set-gate endpoints
  NOTE: api_paper_run is patched separately via direct Edit to call _apply_thresh_override()
"""
PATH = 'C:/Users/neo_w/new_world_order/monitor/dashboard.py'

THRESH_CSS = """
  /* ── Hide old swim-wrap ─────────────────────────────────────── */
  #swim-wrap, .swim-wrap { display: none !important; }
  /* ── AI Threshold control bar ──────────────────────────────── */
  .thresh-bar { display: flex; align-items: center; gap: 10px; padding: 8px 14px;
                background: #161b22; border: 1px solid #30363d; border-radius: 8px;
                flex-wrap: wrap; }
  .thresh-label { font-size: 10px; font-weight: 700; color: #8b949e;
                  letter-spacing: 0.5px; text-transform: uppercase; white-space: nowrap; }
  .thresh-grp { display: flex; gap: 3px; }
  .thresh-btn  { padding: 3px 11px; border-radius: 20px; border: 1px solid #30363d;
                 background: transparent; color: #8b949e; font-size: 11px; cursor: pointer; }
  .thresh-btn.t-high { border-color: #f85149; color: #f85149; background: rgba(248,81,73,0.1); }
  .thresh-btn.t-med  { border-color: #d29922; color: #d29922; background: rgba(210,153,34,0.1); }
  .thresh-btn.t-low  { border-color: #3fb950; color: #3fb950; background: rgba(63,185,80,0.1); }
  .thresh-desc { font-size: 10px; color: #8b949e; white-space: nowrap; }
  .thresh-chips { display: flex; gap: 5px; flex-wrap: wrap; margin-left: auto; }
  .thresh-chip  { padding: 2px 7px; border-radius: 10px; font-size: 11px; font-weight: 600;
                  background: rgba(63,185,80,0.12); color: #3fb950;
                  border: 1px solid rgba(63,185,80,0.3); }
  .thresh-none  { font-size: 11px; color: #8b949e; font-style: italic; }
  /* ── Gate toggles in AI modal ───────────────────────────────── */
  .ai-gate-sect { margin-top: 12px; padding-top: 10px; border-top: 1px solid #21262d; }
  .ai-gate-title { font-size: 10px; color: #8b949e; font-weight: 700;
                   letter-spacing: 0.5px; text-transform: uppercase; margin-bottom: 8px; }
  .gate-row { display: flex; gap: 8px; flex-wrap: wrap; }
  .gate-tog { display: flex; align-items: center; gap: 5px; cursor: pointer;
              padding: 4px 9px; border-radius: 6px; border: 1px solid #30363d;
              background: #0d1117; user-select: none; }
  .gate-tog:hover { border-color: #58a6ff; }
  .gate-tog.bypassed { border-color: #d29922; background: rgba(210,153,34,0.08); }
  .gate-name { font-size: 11px; font-weight: 700; color: #e6edf3; }
  .gate-tog.bypassed .gate-name { color: #d29922; }
  .gate-hint { font-size: 10px; color: #8b949e; }
  .gate-sw { width: 28px; height: 14px; border-radius: 7px; background: #30363d;
             position: relative; flex-shrink: 0; }
  .gate-tog.bypassed .gate-sw { background: #d29922; }
  .gate-sw::after { content: ''; position: absolute; top: 2px; left: 2px;
                    width: 10px; height: 10px; border-radius: 50%; background: #8b949e; }
  .gate-tog.bypassed .gate-sw::after { left: 16px; background: #fff; }
"""

GATE_MODAL_HTML = """
    <div class="ai-gate-sect" id="ai-gate-sect">
      <div class="ai-gate-title">&#9881; Gate Overrides &mdash; bypass for this ticker</div>
      <div class="gate-row" id="gate-row"></div>
    </div>
"""

THRESH_IIFE_JS = """
// ── AI Threshold control ──────────────────────────────────────────────────────
const THRESH_CFG = {
  high: { label:'High', re:5.0,  ens:0.550, qst:0.450, rr:1.50, kal:2.50 },
  med:  { label:'Med',  re:5.75, ens:0.468, qst:0.383, rr:1.28, kal:2.88 },
  low:  { label:'Low',  re:6.50, ens:0.385, qst:0.315, rr:1.05, kal:3.25 },
};
let _thresh = localStorage.getItem('sg3_thresh') || 'high';
let _gateOv  = JSON.parse(localStorage.getItem('sg3_gate_ov') || '{}');
let _lastSigs = [];

function _threshDesc(t) {
  const c = THRESH_CFG[t];
  return 'Re<' + c.re + ' \xb7 Ens>' + Math.round(c.ens*100) + '% \xb7 QSt>'
       + Math.round(c.qst*100) + '% \xb7 R/R>' + c.rr + ' \xb7 Kal<' + c.kal + '\u03c3';
}

function setThreshLevel(lv) {
  _thresh = lv;
  localStorage.setItem('sg3_thresh', lv);
  fetch('/api/paper/set-thresh', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({level:lv})}).catch(()=>{});
  renderThreshBar();
}
window.setThreshLevel = setThreshLevel;
window._threshGet = () => THRESH_CFG[_thresh];

function renderThreshBar() {
  const bar = document.getElementById('thresh-bar');
  if (!bar) return;
  const c = THRESH_CFG[_thresh];
  const passing = _lastSigs.filter(s => {
    const sig = (s.signal||'').toUpperCase();
    return (s.confidence||0) >= c.ens && (sig==='BUY'||sig==='STRONG_BUY');
  }).sort((a,b) => (b.confidence||0)-(a.confidence||0));
  const chips = passing.length
    ? passing.map(s => '<span class="thresh-chip" title="Conf '
        + Math.round((s.confidence||0)*100) + '%">' + s.ticker + '</span>').join('')
    : '<span class="thresh-none">No signals at this threshold</span>';
  bar.innerHTML =
    '<span class="thresh-label">\u26a1 AI Gates:</span>' +
    '<div class="thresh-grp">' +
    ['high','med','low'].map(lv => {
      const act = _thresh===lv;
      return '<button class="thresh-btn' + (act?' t-'+lv:'') + '" onclick="setThreshLevel(\\'' + lv + '\\')">'
           + (act?'\u25cf ':'\u25cb ') + THRESH_CFG[lv].label + '</button>';
    }).join('') + '</div>' +
    '<span class="thresh-desc">' + _threshDesc(_thresh) + '</span>' +
    '<div class="thresh-chips">' + chips + '</div>';
}
window.renderThreshBar = function(sigs) { if(sigs) _lastSigs=sigs; renderThreshBar(); };

// Inject thresh bar after sg3-section once DOM is ready
setTimeout(function() {
  const sg3 = document.getElementById('sg3-section');
  if (sg3 && !document.getElementById('thresh-bar')) {
    const el = document.createElement('div');
    el.id = 'thresh-bar'; el.className = 'thresh-bar';
    sg3.insertAdjacentElement('afterend', el);
    renderThreshBar();
  }
}, 600);

// ── Per-ticker gate overrides ─────────────────────────────────────────────────
const GATE_DEFS = [
  {key:'reynolds', abbr:'Re',  hint:'Reynolds turbulence'},
  {key:'ensemble', abbr:'Ens', hint:'Ensemble probability'},
  {key:'quantum',  abbr:'QSt', hint:'Quantum state'},
  {key:'rr',       abbr:'R/R', hint:'Risk/reward ratio'},
  {key:'kalman',   abbr:'Kal', hint:'Kalman filter'},
];

function sgRenderGateToggles(ticker) {
  const row = document.getElementById('gate-row');
  if (!row) return;
  const tov = _gateOv[ticker] || [];
  row.innerHTML = GATE_DEFS.map(g => {
    const by = tov.includes(g.key);
    return '<div class="gate-tog' + (by?' bypassed':'') + '" '
      + 'onclick="sgToggleGate(\\'' + ticker + '\\',\\'' + g.key + '\\')" '
      + 'title="' + (by?'BYPASSED':'Active') + '">'
      + '<div class="gate-sw"></div>'
      + '<span class="gate-name">' + g.abbr + '</span>'
      + '<span class="gate-hint">' + g.hint + '</span>'
      + '</div>';
  }).join('');
}
window.sgRenderGateToggles = sgRenderGateToggles;

function sgToggleGate(ticker, key) {
  if (!_gateOv[ticker]) _gateOv[ticker] = [];
  const i = _gateOv[ticker].indexOf(key);
  if (i===-1) _gateOv[ticker].push(key); else _gateOv[ticker].splice(i,1);
  if (!_gateOv[ticker].length) delete _gateOv[ticker];
  localStorage.setItem('sg3_gate_ov', JSON.stringify(_gateOv));
  fetch('/api/paper/set-gate', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify(_gateOv)}).catch(()=>{});
  sgRenderGateToggles(ticker);
}
window.sgToggleGate = sgToggleGate;
"""

BACKEND_ENDPOINTS = '''

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
'''

APPEND = """

# ── Threshold patch ───────────────────────────────────────────────────────────
# 1. Hide swim-wrap + inject threshold bar CSS
PAPER_HTML = PAPER_HTML.replace('</style>', """ + repr(THRESH_CSS) + """ + '</style>', 1)

# 2. Inject gate toggles into AI modal (before modal footer)
PAPER_HTML = PAPER_HTML.replace(
    '<div class="ai-modal-foot">',
    """ + repr(GATE_MODAL_HTML) + """ + '<div class="ai-modal-foot">',
    1
)

# 3. Inject threshold + gate JS inside IIFE
_THRESH_IIFE_JS = """ + repr(THRESH_IIFE_JS) + """
PAPER_JS = PAPER_JS.replace(
    'sg3Boot();\\n\\n})(); // end IIFE',
    _THRESH_IIFE_JS + 'sg3Boot();\\n\\n})(); // end IIFE'
)

# 4. Hook renderThreshBar into signal loads
PAPER_JS = PAPER_JS.replace(
    'pBuildTape(sigs);\\n    pBuildCompactSwim(sigs);',
    'pBuildTape(sigs);\\n    pBuildCompactSwim(sigs);\\n    if(window.renderThreshBar) renderThreshBar(sigs);'
)
PAPER_JS = PAPER_JS.replace(
    'pBuildTape(sigsArr);\\n    pBuildCompactSwim(sigsArr);',
    'pBuildTape(sigsArr);\\n    pBuildCompactSwim(sigsArr);\\n    if(window.renderThreshBar) renderThreshBar(sigsArr);'
)

# 5. Hook gate toggles into AI modal open
PAPER_JS = PAPER_JS.replace(
    "document.getElementById('ai-overlay').style.display = 'flex';",
    "document.getElementById('ai-overlay').style.display = 'flex';\\n"
    "  if(window.sgRenderGateToggles) { var _t=document.getElementById('ai-ticker'); if(_t) sgRenderGateToggles(_t.textContent.trim()); }"
)
""" + BACKEND_ENDPOINTS

with open(PATH, 'a', encoding='utf-8') as f:
    f.write(APPEND)

print("Appended OK")

import subprocess, sys
r = subprocess.run([sys.executable, '-m', 'py_compile', PATH], capture_output=True, text=True)
print("Syntax OK" if r.returncode == 0 else "SYNTAX ERROR:\n" + r.stderr)
