"""
Fix: Remove infinite-recursion sg3Render + sg3Boot overrides injected by _fix_stage_layout.py.

Root cause: function declarations are hoisted in JS — so
  const _origSg3Render = typeof sg3Render === 'function' ? sg3Render : null;
  function sg3Render() { if (_origSg3Render) _origSg3Render(); ... }
captures the NEW sg3Render (itself) in _origSg3Render → infinite recursion → stack overflow.

Fix: strip those two bad function declarations from PAPER_JS, then add safe window.*
wrappers AFTER the IIFE using variable assignment (no hoisting issue).
"""
PATH = 'C:/Users/neo_w/new_world_order/monitor/dashboard.py'

# ─── 1. Remove broken sg3Render override (causes infinite recursion) ──────────
_BAD_RENDER = (
    "// Override sg3Render to also update pipeline counts\n"
    "const _origSg3Render = typeof sg3Render === 'function' ? sg3Render : null;\n"
    "function sg3Render() {\n"
    "  if (_origSg3Render) _origSg3Render();\n"
    "  sg3UpdatePipelineCounts();\n"
    "}\n"
    "window.sg3Render = sg3Render;\n"
)

_SAFE_RENDER_PLACEHOLDER = (
    "// sg3Render pipeline-count hook: applied post-IIFE (see window override below)\n"
)

# ─── 2. Remove broken sg3Boot override (causes infinite recursion) ────────────
_BAD_BOOT = (
    "// ── Move thresh-bar before sg3-section instead of after ──────────────────\n"
    "const _origSg3Boot2 = typeof sg3Boot === 'function' ? sg3Boot : null;\n"
    "function sg3Boot() {\n"
    "  if (_origSg3Boot2) _origSg3Boot2();\n"
    "  sg3BuildPipeline();\n"
    "  sg3UpdatePipelineCounts();\n"
    "  // Relocate thresh-bar to above sg3-section once it exists\n"
    "  setTimeout(() => {\n"
    "    const bar = document.getElementById('thresh-bar');\n"
    "    const sg3 = document.getElementById('sg3-section');\n"
    "    if (bar && sg3 && sg3.parentNode) {\n"
    "      sg3.parentNode.insertBefore(bar, sg3);\n"
    "    }\n"
    "  }, 700);\n"
    "}\n"
    "window.sg3Boot = sg3Boot;\n"
)

_SAFE_BOOT_PLACEHOLDER = (
    "// sg3Boot pipeline + thresh-bar hook: applied post-IIFE (see window override below)\n"
)

# ─── 3. Post-IIFE safe wrappers ────────────────────────────────────────────────
# These run AFTER the IIFE closes, use window.* references — no hoisting issue.
_POST_IIFE_JS = """
// ── Post-IIFE: safe sg3Boot + sg3Render wrappers ──────────────────────────────
(function() {
  // Wrap sg3Boot: run original (fetches data + renders), then add pipeline UI
  var _origBoot = window.sg3Boot;
  window.sg3Boot = function() {
    var r = _origBoot && _origBoot();
    // After original async boot resolves, build pipeline header + move thresh-bar
    var after = function() {
      if (window.sg3BuildPipeline) window.sg3BuildPipeline();
      if (window.sg3UpdatePipelineCounts) window.sg3UpdatePipelineCounts();
      setTimeout(function() {
        var bar = document.getElementById('thresh-bar');
        var sg3e = document.getElementById('sg3-section');
        if (bar && sg3e && sg3e.parentNode) sg3e.parentNode.insertBefore(bar, sg3e);
      }, 800);
    };
    if (r && typeof r.then === 'function') r.then(after); else after();
  };

  // Wrap sg3Render: run original, then update pipeline counts
  var _origRender = window.sg3Render;
  window.sg3Render = function() {
    if (_origRender) _origRender();
    if (window.sg3UpdatePipelineCounts) window.sg3UpdatePipelineCounts();
  };
})();
"""

APPEND = """

# ── Fix: remove infinite-recursion sg3Render/sg3Boot overrides ───────────────
_BAD_RENDER = (
    "// Override sg3Render to also update pipeline counts\\n"
    "const _origSg3Render = typeof sg3Render === 'function' ? sg3Render : null;\\n"
    "function sg3Render() {\\n"
    "  if (_origSg3Render) _origSg3Render();\\n"
    "  sg3UpdatePipelineCounts();\\n"
    "}\\n"
    "window.sg3Render = sg3Render;\\n"
)
PAPER_JS = PAPER_JS.replace(_BAD_RENDER,
    "// sg3Render pipeline-count hook: applied post-IIFE (see window override below)\\n")

_BAD_BOOT = (
    "// ── Move thresh-bar before sg3-section instead of after ──────────────────\\n"
    "const _origSg3Boot2 = typeof sg3Boot === 'function' ? sg3Boot : null;\\n"
    "function sg3Boot() {\\n"
    "  if (_origSg3Boot2) _origSg3Boot2();\\n"
    "  sg3BuildPipeline();\\n"
    "  sg3UpdatePipelineCounts();\\n"
    "  // Relocate thresh-bar to above sg3-section once it exists\\n"
    "  setTimeout(() => {\\n"
    "    const bar = document.getElementById('thresh-bar');\\n"
    "    const sg3 = document.getElementById('sg3-section');\\n"
    "    if (bar && sg3 && sg3.parentNode) {\\n"
    "      sg3.parentNode.insertBefore(bar, sg3);\\n"
    "    }\\n"
    "  }, 700);\\n"
    "}\\n"
    "window.sg3Boot = sg3Boot;\\n"
)
PAPER_JS = PAPER_JS.replace(_BAD_BOOT,
    "// sg3Boot pipeline + thresh-bar hook: applied post-IIFE (see window override below)\\n")

# Add safe post-IIFE wrappers after the IIFE closes
_POST_IIFE_JS = """ + repr(_POST_IIFE_JS) + """
PAPER_JS = PAPER_JS.replace(
    '})(); // end IIFE',
    '})(); // end IIFE\\n' + _POST_IIFE_JS
)
"""

with open(PATH, 'a', encoding='utf-8') as f:
    f.write(APPEND)

print("Appended OK")

import subprocess, sys
r = subprocess.run([sys.executable, '-m', 'py_compile', PATH], capture_output=True, text=True)
print("Syntax OK" if r.returncode == 0 else "SYNTAX ERROR:\n" + r.stderr)
