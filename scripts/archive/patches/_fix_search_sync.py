"""
Fix: sg3AddTicker and pSyncWatchlist reference _sg3/sg3Render/sg3Save
which live inside the IIFE — not accessible from outer scope.

Solution:
  1. Inject sg3AddTicker + _sg3Ref getter INSIDE the IIFE
  2. Remove the broken outer sg3AddTicker definition
  3. Fix pSyncWatchlist to use window.sg3AddTicker / window._sg3Ref
"""
PATH = 'C:/Users/neo_w/new_world_order/monitor/dashboard.py'

# These strings are what appear in the actual JS (will be repr()'d for Python)
ADD_INSIDE_JS = """
// sg3AddTicker — inside IIFE: has access to _sg3, sg3Render, sg3Save
function sg3AddTicker(ticker, toStage) {
  if (!ticker) return;
  const already = (_sg3.stage1||[]).includes(ticker)
               || (_sg3.stage2||[]).includes(ticker)
               || (_sg3.stage3||[]).includes(ticker);
  if (already) return;
  (_sg3['stage' + toStage] = _sg3['stage' + toStage] || []).push(ticker);
  sg3Render();
  sg3Save();
}
window.sg3AddTicker = sg3AddTicker;
window._sg3Ref = () => _sg3;

"""

SYNC_NEW_JS = """    let added = 0;
    toAdd.forEach(ticker => {
      const sg3 = window._sg3Ref ? window._sg3Ref() : {};
      const inAny = (sg3.stage1||[]).includes(ticker)
                 || (sg3.stage2||[]).includes(ticker)
                 || (sg3.stage3||[]).includes(ticker);
      window.sg3AddTicker(ticker, '1');
      if (!inAny) added++;
    });

"""

APPEND = """

# ── Fix: search/sync use IIFE-scoped vars — must inject inside IIFE ──────────
_ADD_INSIDE_JS = """ + repr(ADD_INSIDE_JS) + """
_SYNC_NEW_JS   = """ + repr(SYNC_NEW_JS) + """

# 1. Inject sg3AddTicker + _sg3Ref inside IIFE (before sg3Boot)
PAPER_JS = PAPER_JS.replace(
    'sg3Boot();\\n\\n})(); // end IIFE',
    _ADD_INSIDE_JS + 'sg3Boot();\\n\\n})(); // end IIFE'
)

# 2. Remove broken outer sg3AddTicker (references _sg3 which is IIFE-scoped)
_BAD = (
    'function sg3AddTicker(ticker, toStage) {\\n'
    '  if (!ticker) return;\\n'
    '  // Remove from all stages first to avoid duplicates\\n'
    "  ['stage1','stage2','stage3'].forEach(k => {\\n"
    "    if (_sg3[k] && _sg3[k].includes(ticker)) return; // already there\\n"
    '  });\\n'
    '  const already = (_sg3.stage1||[]).includes(ticker)\\n'
    '               || (_sg3.stage2||[]).includes(ticker)\\n'
    "               || (_sg3.stage3||[]).includes(ticker);\\n"
    '  if (already) return;\\n'
    "  (_sg3['stage' + toStage] || []).push(ticker);\\n"
    '  sg3Render();\\n'
    '  sg3Save();\\n'
    '}\\n'
    'window.sg3AddTicker = sg3AddTicker;\\n'
)
PAPER_JS = PAPER_JS.replace(_BAD, '// sg3AddTicker exposed via window from inside IIFE\\n')

# 3. Fix pSyncWatchlist: replace direct _sg3 block with window.sg3AddTicker calls
_SYNC_OLD = (
    '    let added = 0;\\n'
    '    toAdd.forEach(ticker => {\\n'
    '      const inAny = (_sg3.stage1||[]).includes(ticker)\\n'
    '                 || (_sg3.stage2||[]).includes(ticker)\\n'
    "               || (_sg3.stage3||[]).includes(ticker);\\n"
    '      if (!inAny) {\\n'
    "        (_sg3.stage1 = _sg3.stage1 || []).push(ticker);\\n"
    '        added++;\\n'
    '      }\\n'
    '    });\\n'
    '\\n'
    '    if (added > 0) {\\n'
    '      sg3Render();\\n'
    '      await sg3Save();\\n'
    '    }\\n'
)
PAPER_JS = PAPER_JS.replace(_SYNC_OLD, _SYNC_NEW_JS)
"""

with open(PATH, 'a', encoding='utf-8') as f:
    f.write(APPEND)

print("Appended OK")

import subprocess, sys
r = subprocess.run([sys.executable, '-m', 'py_compile', PATH], capture_output=True, text=True)
print("Syntax OK" if r.returncode == 0 else "SYNTAX ERROR:\n" + r.stderr)
