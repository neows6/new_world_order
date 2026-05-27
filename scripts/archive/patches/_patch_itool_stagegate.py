"""
Patch dashboard.py:
  1. Add search box + sortable columns to I-Tool (ITOOL_HTML + ITOOL_JS changes)
  2. Add Stage Gate page + routes
  3. Add Stage Gate button to main dashboard header
"""
import re

PATH = 'C:/Users/neo_w/new_world_order/monitor/dashboard.py'

with open(PATH, 'r', encoding='utf-8') as f:
    src = f.read()

# ─────────────────────────────────────────────────────────────────────────────
# 1. ITOOL_HTML: add search input + sortable-th CSS, add search box to tabs row
# ─────────────────────────────────────────────────────────────────────────────

# Add CSS for search + sortable headers after .count-label rule
old_css = '  .count-label { font-size: 11px; color: #8b949e; margin-left: 6px; }'
new_css = (old_css + '\n'
    '  .search-box { padding: 5px 10px; border-radius: 6px; border: 1px solid #30363d;\n'
    '                background: #21262d; color: #e6edf3; font-size: 12px; width: 160px; margin-left: auto; }\n'
    '  .search-box::placeholder { color: #8b949e; }\n'
    '  th.sortable { cursor: pointer; user-select: none; }\n'
    '  th.sortable:hover { color: #e6edf3; }\n'
    '  th .sort-arrow { font-size: 10px; margin-left: 3px; opacity: 0.5; }\n'
    '  th.sort-asc .sort-arrow, th.sort-desc .sort-arrow { opacity: 1; color: #58a6ff; }')
src = src.replace(old_css, new_css, 1)

# Add search input to tabs row (after count-label span)
old_tabs = '    <span class="count-label" id="count-label"></span>\n  </div>'
new_tabs = ('    <span class="count-label" id="count-label"></span>\n'
            '    <input class="search-box" id="search-box" type="text" placeholder="&#128269; Search ticker..." '
            'oninput="applyFilter()">\n  </div>')
src = src.replace(old_tabs, new_tabs, 1)

# ─────────────────────────────────────────────────────────────────────────────
# 2. ITOOL_JS: add _sortCol/_sortDir/_searchTerm globals, upgrade applyFilter + renderTable
# ─────────────────────────────────────────────────────────────────────────────

# Add sort/search globals after existing let declarations
old_globals = 'let _pollTimer  = null;'
new_globals = ('let _pollTimer  = null;\n'
               'let _sortCol    = null;   // column key being sorted\n'
               'let _sortDir    = 1;      // 1 = asc, -1 = desc')
src = src.replace(old_globals, new_globals, 1)

# Replace applyFilter to include search
old_apply = (
    'function applyFilter() {\n'
    '  const results = _scanData.results || [];\n'
    '  const filtered = _filter === \'all\' ? results : results.filter(r => r.signal === _filter);\n'
    '  document.getElementById(\'count-label\').textContent =\n'
    '    filtered.length + \' of \' + results.length + \' stocks\';\n'
    '  renderTable(filtered);\n'
    '}'
)
new_apply = (
    'function applyFilter() {\n'
    '  const results = _scanData.results || [];\n'
    '  const term = (document.getElementById(\'search-box\') || {}).value || \'\';\n'
    '  const q = term.trim().toUpperCase();\n'
    '  let filtered = _filter === \'all\' ? results : results.filter(r => r.signal === _filter);\n'
    '  if (q) filtered = filtered.filter(r => r.ticker.toUpperCase().includes(q));\n'
    '  // Sort\n'
    '  if (_sortCol) {\n'
    '    filtered = [...filtered].sort((a, b) => {\n'
    '      let av = a[_sortCol], bv = b[_sortCol];\n'
    '      if (av == null) av = _sortDir > 0 ? Infinity : -Infinity;\n'
    '      if (bv == null) bv = _sortDir > 0 ? Infinity : -Infinity;\n'
    '      return (av < bv ? -1 : av > bv ? 1 : 0) * _sortDir;\n'
    '    });\n'
    '  }\n'
    '  document.getElementById(\'count-label\').textContent =\n'
    '    filtered.length + \' of \' + results.length + \' stocks\';\n'
    '  renderTable(filtered);\n'
    '}'
)
src = src.replace(old_apply, new_apply, 1)

# Replace renderTable to add sortable headers
old_render_header = (
    "  let html = '<table><tr>' +\n"
    "    '<th>Ticker</th><th>Price</th><th>SMA30</th><th>SMA50</th>' +\n"
    "    '<th>MACD Hist</th><th>Stoch %K</th><th>Signal</th><th>Chart</th>' +\n"
    "    '</tr>';"
)
new_render_header = (
    "  function sortTh(col, label) {\n"
    "    const active = _sortCol === col;\n"
    "    const dir = active && _sortDir === 1 ? 'desc' : active && _sortDir === -1 ? '' : 'asc';\n"
    "    const arrow = active ? (_sortDir === 1 ? ' &#9650;' : ' &#9660;') : ' &#8693;';\n"
    "    const cls = active ? (_sortDir === 1 ? 'sort-asc' : 'sort-desc') : '';\n"
    "    return '<th class=\"sortable ' + cls + '\" onclick=\"setSort(\\'' + col + '\\')\">' + label + '<span class=\"sort-arrow\">' + arrow + '</span></th>';\n"
    "  }\n"
    "  let html = '<table><tr>' +\n"
    "    '<th>Ticker</th>' +\n"
    "    sortTh('price','Price') +\n"
    "    sortTh('sma30','SMA30') +\n"
    "    sortTh('sma50','SMA50') +\n"
    "    sortTh('macd_hist','MACD Hist') +\n"
    "    sortTh('stoch_k','Stoch %K') +\n"
    "    '<th>Signal</th><th>Chart</th>' +\n"
    "    '</tr>';"
)
src = src.replace(old_render_header, new_render_header, 1)

# Add setSort function before applyFilter
old_setfilter = 'function applyFilter() {'
new_setfilter = (
    'function setSort(col) {\n'
    '  if (_sortCol === col) {\n'
    '    _sortDir = _sortDir === 1 ? -1 : (_sortDir === -1 ? 1 : 1);\n'
    '  } else {\n'
    '    _sortCol = col; _sortDir = -1;  // default: largest first\n'
    '  }\n'
    '  if (_scanData) applyFilter();\n'
    '}\n\n'
    'function applyFilter() {'
)
src = src.replace(old_setfilter, new_setfilter, 1)

# ─────────────────────────────────────────────────────────────────────────────
# 3. Main dashboard header: add Stage Gate button after Paper Trade button
# ─────────────────────────────────────────────────────────────────────────────
old_paper_btn = (
    '  <a href="/paper" target="_blank" class="brief-btn" id="paper-btn">\n'
    '    <span class="brief-btn-title">&#127918; Paper Trade</span>\n'
    '    <span class="brief-btn-preview" id="paper-preview">$100k Faux Account &middot; Loading...</span>\n'
    '  </a>'
)
new_paper_btn = (old_paper_btn + '\n'
    '  <a href="/stagegate" target="_blank" class="brief-btn" id="sg-btn">\n'
    '    <span class="brief-btn-title">&#127760; Stage Gate</span>\n'
    '    <span class="brief-btn-preview" id="sg-preview">Drag stocks into active trading</span>\n'
    '  </a>')
src = src.replace(old_paper_btn, new_paper_btn, 1)

with open(PATH, 'w', encoding='utf-8') as f:
    f.write(src)
print("Phase 1 done, lines:", src.count('\n'))
