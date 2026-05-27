"""
Patch: six paper-dashboard features
  1. Ticker tape on Paper Trading page (uses /api/signals)
  2. Search field + Stage-1/Stage-2 add buttons in header
  3. "Sync Watchlist" button — top-5 bullish I-Tool + all recent signals → Stage 1
  4. Compact single-box swim lanes, moved below Stage Gate
  5. Morning-brief auto-regenerate at 06:00 and 09:00 ET daily
  6. Minor: expose sg3AddTicker globally for search
"""
PATH = 'C:/Users/neo_w/new_world_order/monitor/dashboard.py'

# ─── CSS additions ────────────────────────────────────────────────────────────
EXTRA_CSS = """
  /* ── Ticker tape (paper page) ─────────────────────────────── */
  .p-tape-wrap  { overflow: hidden; background: #0a0f17;
                  border-bottom: 1px solid #1f6feb; height: 26px; flex-shrink: 0; }
  .p-tape-track { display: flex; gap: 24px; white-space: nowrap; will-change: transform;
                  animation: p-tape 100s linear infinite; align-items: center; height: 100%;
                  padding-left: 12px; }
  .p-tape-track:hover { animation-play-state: paused; }
  @keyframes p-tape { 0%{transform:translateX(0)} 100%{transform:translateX(-50%)} }
  .pt-bull { color: #3fb950; font-size: 12px; font-weight: 700; }
  .pt-bear { color: #f85149; font-size: 12px; font-weight: 700; }
  .pt-neu  { color: #8b949e; font-size: 12px; }
  .pt-sep  { color: #30363d; font-size: 10px; }
  /* ── Search + sync header controls ────────────────────────── */
  .p-search-wrap { display: flex; gap: 5px; align-items: center; }
  .p-search-input { padding: 5px 10px; border-radius: 6px; border: 1px solid #30363d;
                    background: #21262d; color: #e6edf3; font-size: 12px; width: 130px;
                    text-transform: uppercase; }
  .p-search-input::placeholder { color: #8b949e; text-transform: none; }
  .p-add-s1 { padding: 5px 9px; border-radius: 6px; border: 1px solid #8b949e;
               background: transparent; color: #8b949e; cursor: pointer; font-size: 11px; }
  .p-add-s1:hover { background: rgba(139,148,158,0.12); }
  .p-add-s2 { padding: 5px 9px; border-radius: 6px; border: 1px solid #58a6ff;
               background: transparent; color: #58a6ff; cursor: pointer; font-size: 11px; }
  .p-add-s2:hover { background: rgba(88,166,255,0.12); }
  .p-sync-btn { padding: 5px 10px; border-radius: 6px; border: 1px solid #d29922;
                background: transparent; color: #d29922; cursor: pointer; font-size: 11px; }
  .p-sync-btn:hover { background: rgba(210,153,34,0.12); }
  /* ── Compact swim lanes ───────────────────────────────────── */
  .swim-compact-wrap { padding: 10px 14px; display: flex; flex-direction: column; gap: 10px; }
  .swim-row  { display: flex; gap: 8px; align-items: flex-start; }
  .swim-row-label { font-size: 10px; font-weight: 700; letter-spacing: 0.5px;
                    text-transform: uppercase; width: 90px; flex-shrink: 0; padding-top: 4px; }
  .swim-row-0 .swim-row-label { color: #58a6ff; }
  .swim-row-1 .swim-row-label { color: #d29922; }
  .swim-row-2 .swim-row-label { color: #3fb950; }
  .swim-chips { display: flex; gap: 5px; flex-wrap: wrap; }
  .swim-chip  { padding: 3px 8px; border-radius: 10px; font-size: 11px; font-weight: 600;
                background: rgba(63,185,80,0.12); color: #3fb950; border: 1px solid rgba(63,185,80,0.3); }
  .swim-none  { font-size: 11px; color: #8b949e; font-style: italic; padding-top: 3px; }
"""

# ─── Header additions (search + sync, injected via JS) ────────────────────────
SEARCH_HTML = """
  <div class="p-search-wrap">
    <input class="p-search-input" id="p-search" type="text" placeholder="Add ticker..."
           maxlength="10" onkeydown="if(event.key==='Enter') pAddTicker('1')">
    <button class="p-add-s1" onclick="pAddTicker('1')" title="Add to Stage 1">+S1</button>
    <button class="p-add-s2" onclick="pAddTicker('2')" title="Add to Stage 2">+S2</button>
  </div>
  <button class="p-sync-btn" onclick="pSyncWatchlist()" title="Sync top signals to Stage 1">&#8635; Sync</button>
"""

# ─── Tape HTML (injected right after </header>) ────────────────────────────────
TAPE_HTML = """<div class="p-tape-wrap"><div class="p-tape-track" id="p-tape">&nbsp;</div></div>"""

# ─── Feature JS ───────────────────────────────────────────────────────────────
FEATURES_JS = """
// ════════════════════════════════════════════════════════════════════════════
// Paper dashboard feature additions
// ════════════════════════════════════════════════════════════════════════════

// ── Inject extra CSS ─────────────────────────────────────────────────────────
(function() {
  const s = document.createElement('style');
  s.textContent = """ + repr(EXTRA_CSS) + """;
  document.head.appendChild(s);
})();

// ── Inject search + sync into header ─────────────────────────────────────────
(function() {
  const hr = document.querySelector('.header-right');
  if (hr) hr.insertAdjacentHTML('afterbegin', """ + repr(SEARCH_HTML) + """);
})();

// ── Inject ticker tape after header ──────────────────────────────────────────
(function() {
  const hdr = document.querySelector('header');
  if (hdr) hdr.insertAdjacentHTML('afterend', """ + repr(TAPE_HTML) + """);
})();

// ── Build ticker tape from signals ───────────────────────────────────────────
function pBuildTape(sigs) {
  const track = document.getElementById('p-tape');
  if (!track) return;
  const items = (sigs || []).filter(s => {
    const sig = (s.signal || '').toUpperCase();
    return sig === 'BUY' || sig === 'STRONG_BUY' || sig === 'SELL' || sig === 'STRONG_SELL';
  });
  if (!items.length) { track.innerHTML = '<span class="pt-neu">No signals</span>'; return; }
  const all = [...items, ...items];
  track.innerHTML = all.map(s => {
    const sig = (s.signal || '').toUpperCase();
    const bull = sig === 'BUY' || sig === 'STRONG_BUY';
    const cls  = bull ? 'pt-bull' : 'pt-bear';
    const arr  = bull ? '&#9650;' : '&#9660;';
    const price = s.current_price ? ' $' + s.current_price.toFixed(2) : '';
    return '<span class="' + cls + '">' + arr + ' ' + s.ticker + price + '</span>'
         + '<span class="pt-sep">|</span>';
  }).join('');
  track.style.animationDuration = Math.max(40, items.length * 0.7) + 's';
}

// ── Compact swim lanes (replace 3-col grid with single box) ──────────────────
function pBuildCompactSwim(sigs) {
  const swimSec = document.getElementById('swim-section');
  if (!swimSec) return;

  const MODELS = [
    { label: 'Standard',     conf: 0.50,  mos: 0.15,   fud: 0.60 },
    { label: 'Relaxed -25%', conf: 0.375, mos: 0.1125, fud: 0.45 },
    { label: 'Relaxed -50%', conf: 0.25,  mos: 0.075,  fud: 0.30 },
  ];

  const rows = MODELS.map((m, i) => {
    const passing = (sigs || []).filter(s => {
      const sig = (s.signal || '').toUpperCase();
      return (s.confidence || 0) >= m.conf
          && (s.margin_of_safety || 0) >= m.mos
          && (s.fud_score || 0) >= m.fud
          && (sig === 'BUY' || sig === 'STRONG_BUY');
    }).sort((a, b) => (b.confidence || 0) - (a.confidence || 0));

    const chips = passing.length
      ? passing.map(s =>
          '<span class="swim-chip" title="Conf ' + ((s.confidence||0)*100).toFixed(0) + '% | MoS '
          + ((s.margin_of_safety||0)*100).toFixed(1) + '%">' + s.ticker + '</span>'
        ).join('')
      : '<span class="swim-none">None clear this bar</span>';

    return '<div class="swim-row swim-row-' + i + '">'
      + '<div class="swim-row-label">' + m.label + '</div>'
      + '<div class="swim-chips">' + chips + '</div>'
      + '</div>';
  });

  swimSec.querySelector('.section-title').textContent = '\\ud83d\\udcca Threshold Models';
  let body = swimSec.querySelector('.swim-compact-wrap');
  if (!body) {
    // Replace old grid with compact wrap
    const old = swimSec.querySelector('.swim-wrap');
    if (old) old.remove();
    body = document.createElement('div');
    body.className = 'swim-compact-wrap';
    swimSec.appendChild(body);
  }
  body.innerHTML = rows.join('');

  // Move swim section below sg3-section
  const sg3Sec = document.getElementById('sg3-section');
  if (sg3Sec && swimSec.parentNode) {
    sg3Sec.insertAdjacentElement('afterend', swimSec);
  }
}

// ── Search: add ticker to stage 1 or 2 ───────────────────────────────────────
function pAddTicker(toStage) {
  const inp = document.getElementById('p-search');
  const ticker = (inp ? inp.value : '').trim().toUpperCase();
  if (!ticker) return;
  inp.value = '';
  sg3AddTicker(ticker, toStage);
}

function sg3AddTicker(ticker, toStage) {
  if (!ticker) return;
  // Remove from all stages first to avoid duplicates
  ['stage1','stage2','stage3'].forEach(k => {
    if (_sg3[k] && _sg3[k].includes(ticker)) return; // already there
  });
  const already = (_sg3.stage1||[]).includes(ticker)
               || (_sg3.stage2||[]).includes(ticker)
               || (_sg3.stage3||[]).includes(ticker);
  if (already) return;
  (_sg3['stage' + toStage] || []).push(ticker);
  sg3Render();
  sg3Save();
}
window.sg3AddTicker = sg3AddTicker;
window.pAddTicker   = pAddTicker;

// ── Sync watchlist: top-5 bullish I-Tool + all recent signals ─────────────────
async function pSyncWatchlist() {
  const btn = document.querySelector('.p-sync-btn');
  if (btn) { btn.disabled = true; btn.textContent = '\\u29d7 Syncing...'; }
  try {
    const [itool, sigs] = await Promise.all([
      fetch('/api/itool').then(r => r.json()).catch(() => ({})),
      fetch('/api/signals').then(r => r.json()).catch(() => []),
    ]);

    const toAdd = new Set();

    // Top 5 bullish from I-Tool
    const itoolResults = (itool.results || [])
      .filter(r => r.signal === 'bullish')
      .slice(0, 5);
    itoolResults.forEach(r => toAdd.add(r.ticker));

    // All bullish from recent signals
    (sigs || []).forEach(s => {
      const sig = (s.signal || '').toUpperCase();
      if (sig === 'BUY' || sig === 'STRONG_BUY') toAdd.add(s.ticker);
    });

    let added = 0;
    toAdd.forEach(ticker => {
      const inAny = (_sg3.stage1||[]).includes(ticker)
                 || (_sg3.stage2||[]).includes(ticker)
                 || (_sg3.stage3||[]).includes(ticker);
      if (!inAny) {
        (_sg3.stage1 = _sg3.stage1 || []).push(ticker);
        added++;
      }
    });

    if (added > 0) {
      sg3Render();
      await sg3Save();
    }
    if (btn) btn.textContent = '\\u2713 Synced +' + added;
  } catch(e) {
    if (btn) btn.textContent = 'Error';
  } finally {
    setTimeout(() => { if (btn) { btn.disabled = false; btn.textContent = '\\u8635 Sync'; } }, 3000);
  }
}
window.pSyncWatchlist = pSyncWatchlist;

// ── Hook into existing loadSwimLanes / signal load to drive tape + compact swim ─
const _origLoadSwimLanes = typeof loadSwimLanes === 'function' ? loadSwimLanes : null;
window.loadSwimLanes = async function() {
  if (_origLoadSwimLanes) await _origLoadSwimLanes();
  try {
    const sigs = await fetch('/api/signals').then(r => r.json());
    pBuildTape(sigs);
    pBuildCompactSwim(sigs);
  } catch(e) {}
};

// Also seed tape immediately from already-loaded _sg3sigs
setTimeout(() => {
  const sigsArr = Object.values(_sg3sigs || {});
  if (sigsArr.length) {
    pBuildTape(sigsArr);
    pBuildCompactSwim(sigsArr);
  }
}, 800);
"""

# ─── Morning brief auto-regen scheduler ──────────────────────────────────────
SCHEDULER_CODE = '''

# ── Morning Brief auto-regeneration at 06:00 and 09:00 ET ────────────────────
@app.on_event("startup")
async def _schedule_morning_brief():
    import threading, time
    from datetime import datetime
    import pytz

    ET = pytz.timezone("America/New_York")

    def _brief_scheduler():
        fired_today = set()
        while True:
            try:
                now = datetime.now(ET)
                key_6  = (now.date(), 6)
                key_9  = (now.date(), 9)
                if now.hour == 6 and now.minute == 0 and key_6 not in fired_today:
                    fired_today.add(key_6)
                    fired_today.discard((now.date(), 9) if False else None)  # cleanup
                    _trigger_brief_generation()
                    logger.info("[BRIEF] Auto-regen triggered at 06:00 ET")
                elif now.hour == 9 and now.minute == 0 and key_9 not in fired_today:
                    fired_today.add(key_9)
                    _trigger_brief_generation()
                    logger.info("[BRIEF] Auto-regen triggered at 09:00 ET")
                # Prune old keys daily
                today = now.date()
                fired_today = {k for k in fired_today if k and k[0] == today}
            except Exception as e:
                logger.warning(f"[BRIEF] Scheduler error: {e}")
            time.sleep(30)

    threading.Thread(target=_brief_scheduler, daemon=True).start()
    logger.info("[BRIEF] Auto-regen scheduler started (06:00 + 09:00 ET daily)")
'''

# ─── Build append block ───────────────────────────────────────────────────────
APPEND = """

# ── Paper dashboard feature additions (auto-patched) ─────────────────────────
_FEAT_JS  = """ + repr(FEATURES_JS) + """
PAPER_JS  = PAPER_JS + _FEAT_JS
""" + SCHEDULER_CODE

with open(PATH, 'a', encoding='utf-8') as f:
    f.write(APPEND)

print("Appended OK")

import subprocess, sys
r = subprocess.run([sys.executable, '-m', 'py_compile', PATH], capture_output=True, text=True)
print("Syntax OK" if r.returncode == 0 else "SYNTAX ERROR:\n" + r.stderr)
