"""
Fix: sg3 functions are inside an IIFE so HTML event attributes can't call them.
Expose them on window by replacing the IIFE closing with window assignments first.
"""
PATH = 'C:/Users/neo_w/new_world_order/monitor/dashboard.py'

EXPOSE = """
// Expose sg3 functions globally for HTML ondragover/ondrop/etc. attributes
window.sg3Over           = sg3Over;
window.sg3Leave          = sg3Leave;
window.sg3Drop           = sg3Drop;
window.sg3DragStart      = sg3DragStart;
window.sg3DragEnd        = sg3DragEnd;
window.sg3ActivateAI     = sg3ActivateAI;
window.sg3OpenBuy        = sg3OpenBuy;
window.sg3OpenSell       = sg3OpenSell;
window.sg3SellModeChange = sg3SellModeChange;
window.sg3UpdateSellHint = sg3UpdateSellHint;
window.sg3SellCancel     = sg3SellCancel;
window.sg3SellConfirm    = sg3SellConfirm;
window.sg3Remove         = sg3Remove;
window.sg3Boot           = sg3Boot;

"""

APPEND = """
# ── Fix: expose sg3 functions globally (IIFE scope fix) ──────────────────────
_EXPOSE_JS = """ + repr(EXPOSE) + """
PAPER_JS   = PAPER_JS.replace('sg3Boot();\\n\\n})(); // end IIFE',
                               _EXPOSE_JS + 'sg3Boot();\\n\\n})(); // end IIFE')
"""

with open(PATH, 'a', encoding='utf-8') as f:
    f.write(APPEND)

print("Appended OK")

import subprocess, sys
r = subprocess.run([sys.executable, '-m', 'py_compile', PATH], capture_output=True, text=True)
print("Syntax OK" if r.returncode == 0 else "SYNTAX ERROR: " + r.stderr)
