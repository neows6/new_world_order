"""
monitor/dashboard.py — FastAPI web dashboard for NWO trading system.

Endpoints:
  GET  /                    HTML dashboard (auto-refreshes every 15s)
  GET  /api/status          System status JSON
  GET  /api/signals         Recent trade signals JSON
  GET  /api/trades          Recent trade log JSON
  GET  /api/logs            Last N log lines JSON
  POST /api/pause           Toggle pause flag
  GET  /api/diagnostics     Per-ticker data health check
  POST /api/backfill        Trigger price/fundamentals backfill in background
  GET  /api/backfill/status Backfill progress
"""

import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
import pytz

# Resolve project root so this works whether called directly or via -m
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from config import config
from models.database import init_db, Company, TradeSignal, TradeLog, Fundamental, PriceHistory
from broker.market_data import SchwabMarketData

app = FastAPI(title="NWO Monitor", docs_url=None, redoc_url=None)

from monitor.wheel import wheel_router
app.include_router(wheel_router)

from monitor.alfred import alfred_router
app.include_router(alfred_router)

# ── Mount signals dashboard under /signals ────────────────────────────────────
from monitor.signals_dashboard import app as _signals_app, _latest_signals as _get_live_signals
app.mount("/signals", _signals_app)

PAUSE_FLAG  = ROOT / "data" / "paused.flag"
LOG_FILE    = ROOT / config.log_file
LIVE_LOG    = ROOT / "logs" / "nwo_live.log"

# Wire loguru to write live server activity to a tailable file.
# All modules in this process (paper scheduler, thesis analysis, etc.) share
# this same loguru instance, so their output lands here automatically.
try:
    from loguru import logger as _logger
    LIVE_LOG.parent.mkdir(parents=True, exist_ok=True)
    _logger.add(
        str(LIVE_LOG),
        rotation="10 MB",
        retention="7 days",
        compression="zip",
        level="INFO",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function} - {message}",
        enqueue=True,
    )
except Exception:
    pass

_, Session = init_db(config.database.url, echo=False)

_ET = pytz.timezone("America/New_York")

def _to_et_str(dt, fmt="%Y-%m-%d %H:%M:%S ET"):
    """Convert a UTC datetime (naive or aware) to an ET-formatted string."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_ET).strftime(fmt)


# ── Live price cache — Schwab quotes, refreshed every 5 min always ────────────
import time as _time

_live_price_cache: dict = {}   # ticker -> {price, change_pct, prev_close, ts}
_live_cache_lock  = threading.Lock()
_live_cache_ts: float = 0.0
_LIVE_TTL = 300  # 5 minutes

def _refresh_live_prices(force: bool = False) -> None:
    """Fetch Schwab quotes for all stage gate + watchlist tickers. No market-hours gate —
    Schwab returns lastPrice/mark 24/7 (after-hours, pre-market, extended hours)."""
    global _live_cache_ts
    if not force and (_time.time() - _live_cache_ts) < _LIVE_TTL:
        return
    try:
        # Use a fresh SchwabMarketData — avoids waiting for scheduler init
        from broker.market_data import SchwabMarketData as _SMD
        md = _SMD()
        import json as _json
        _sgf = ROOT / "data" / "stagegate.json"
        sg = _json.loads(_sgf.read_text()) if _sgf.exists() else {}
        tickers = list({
            t for stage in sg.values() for t in stage
        } | set(config.watchlist))
        if not tickers:
            return
        quotes = md.get_quotes_batch(tickers)
        if not quotes:
            return
        with _live_cache_lock:
            for ticker, q in quotes.items():
                lp = q.get("last_price")
                if lp:
                    _live_price_cache[ticker] = {
                        "price":      round(float(lp), 2),
                        "change_pct": q.get("net_pct_change"),
                        "net_change": q.get("net_change"),
                        "prev_close": q.get("prev_close"),
                        "ts":         _time.time(),
                    }
        _live_cache_ts = _time.time()
    except Exception:
        pass

def _live_price(ticker: str) -> dict | None:
    """Return live price dict for ticker. Triggers refresh if cache is stale."""
    _refresh_live_prices()
    with _live_cache_lock:
        return _live_price_cache.get(ticker)

def _start_live_price_refresher() -> None:
    """Background thread: refresh every 5 min, always."""
    def _loop():
        _time.sleep(10)  # brief delay so server startup completes first
        while True:
            try:
                _refresh_live_prices()
            except Exception:
                pass
            _time.sleep(60)
    t = threading.Thread(target=_loop, daemon=True, name="live-price-refresher")
    t.start()

_start_live_price_refresher()


# ── helpers ──────────────────────────────────────────────────────────────────

def _is_paused() -> bool:
    return PAUSE_FLAG.exists()


def _price_change(session, company_id: int, ticker: str = "") -> dict:
    """Return latest price and day-over-day % change.
    Prefers live Schwab intraday quote; falls back to EOD PriceHistory."""
    if ticker:
        live = _live_price(ticker)
        if live:
            return {
                "price":      live["price"],
                "change_pct": live.get("change_pct"),
                "prev_close": live.get("prev_close"),
                "live":       True,
            }
    candles = (
        session.query(PriceHistory)
        .filter_by(company_id=company_id)
        .order_by(PriceHistory.date.desc())
        .limit(2)
        .all()
    )
    if not candles:
        return {"price": None, "change_pct": None, "prev_close": None}
    latest = candles[0]
    prev   = candles[1] if len(candles) > 1 else None
    price  = latest.close
    change_pct = None
    if prev and prev.close:
        change_pct = round((price - prev.close) / prev.close * 100, 2)
    return {"price": round(price, 2) if price else None,
            "change_pct": change_pct,
            "prev_close": round(prev.close, 2) if prev and prev.close else None}


_SYNTH_CACHE_PATH     = ROOT / "data" / "signal_syntheses.json"
_THESIS_LOG_PATH      = ROOT / "data" / "thesis_log.json"
_THESIS_ANALYSIS_PATH = ROOT / "data" / "thesis_analysis.json"
_THESIS_LOG_LOCK      = threading.Lock()


def _load_synth_cache() -> dict:
    try:
        if _SYNTH_CACHE_PATH.exists():
            import json as _j
            return _j.loads(_SYNTH_CACHE_PATH.read_text())
    except Exception:
        pass
    return {}


def _save_synth_cache(cache: dict) -> None:
    try:
        import json as _j
        _SYNTH_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SYNTH_CACHE_PATH.write_text(_j.dumps(cache, indent=2))
    except Exception:
        pass


def _append_thesis_log(entry: dict) -> None:
    """Thread-safe append of one synthesis entry to thesis_log.json."""
    import json as _j
    with _THESIS_LOG_LOCK:
        try:
            _THESIS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            existing = []
            if _THESIS_LOG_PATH.exists():
                try:
                    existing = _j.loads(_THESIS_LOG_PATH.read_text())
                except Exception:
                    existing = []
            existing.append(entry)
            _THESIS_LOG_PATH.write_text(_j.dumps(existing, indent=2))
        except Exception as exc:
            import logging as _lg
            _lg.getLogger(__name__).warning(f"[ThesisLog] append failed: {exc}")


def _load_thesis_analysis() -> dict | None:
    """Return the most-recent daily analysis entry, or None."""
    import json as _j
    try:
        if _THESIS_ANALYSIS_PATH.exists():
            data = _j.loads(_THESIS_ANALYSIS_PATH.read_text())
            if data:
                return data[0]  # newest first
    except Exception:
        pass
    return None


def _save_thesis_analysis(entry: dict) -> None:
    """Prepend entry to thesis_analysis.json (newest first, keeps full history)."""
    import json as _j
    try:
        _THESIS_ANALYSIS_PATH.parent.mkdir(parents=True, exist_ok=True)
        existing = []
        if _THESIS_ANALYSIS_PATH.exists():
            try:
                existing = _j.loads(_THESIS_ANALYSIS_PATH.read_text())
            except Exception:
                existing = []
        existing.insert(0, entry)
        _THESIS_ANALYSIS_PATH.write_text(_j.dumps(existing, indent=2))
    except Exception as exc:
        import logging as _lg
        _lg.getLogger(__name__).warning(f"[ThesisAnalysis] save failed: {exc}")


def _generate_signal_synthesis(sig: dict, api_key: str) -> str:
    import json as _j
    r = sig.get("reasoning", {})
    if isinstance(r, str):
        try:
            r = _j.loads(r)
        except Exception:
            r = {}
    why_buy  = r.get("why_buy",  [])
    why_wait = r.get("why_wait", [])
    prompt = (
        f"Stock signal for {sig['ticker']} — {sig['signal']} ({sig.get('confidence', 0):.0%} confidence)\n"
        f"Scores: composite={r.get('composite_score', 0):.2f}, momentum={r.get('momentum_score', 0):.2f}, "
        f"news_quality={sig.get('fud_score', 0):.2f}, MOS={sig.get('margin_of_safety', 0):.0%}\n"
        f"Price: ${(sig.get('current_price') or 0):.2f} vs intrinsic ${(sig.get('intrinsic_value') or 0):.2f}\n"
        f"TGA: {r.get('tga_arrows', 0)}/3 | "
        f"Insider: {'CLUSTER BUY' if r.get('insider_cluster_buy') else 'none'} | "
        f"VIX: {r.get('vix_regime', 'normal')} | MACD: {r.get('macd_direction', r.get('macd_signal_direction', '?'))}\n"
        f"System reasons: {'; '.join(str(x) for x in why_buy[:3])}\n"
        f"Cautions: {'; '.join(str(x) for x in why_wait[:2]) if why_wait else 'none'}\n\n"
        f"Write exactly 2 plain sentences (no markdown, no headers, no bullets):\n"
        f"Sentence 1: The narrative connecting these signals into a coherent trade thesis.\n"
        f"Sentence 2: One non-obvious risk or timing factor the trader should watch.\n"
        f"Be specific and quantitative. No disclaimers. Start directly with sentence 1."
    )
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg    = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as exc:
        import logging as _lg
        _lg.getLogger(__name__).warning(f"[Synthesis] Claude call failed for {sig.get('ticker')}: {exc}")
        return ""


def _recent_signals(limit: int = 50) -> list:
    with Session() as session:
        rows = (
            session.query(TradeSignal, Company)
            .join(Company, Company.id == TradeSignal.company_id)
            .order_by(TradeSignal.generated_at.desc())
            .limit(limit)
            .all()
        )
        _synth = _load_synth_cache()
        result = []
        for s, company in rows:
            pc = _price_change(session, company.id, ticker=company.ticker)
            # Parse reasoning JSON to surface new signal fields for the dashboard
            rsn = {}
            try:
                import json as _json
                rsn = _json.loads(s.reasoning or "{}")
            except Exception:
                pass

            # Resolve price at signal generation time + source label for UI:
            #   "exact"  — stored in TradeSignal.current_price at generation time (live intraday)
            #   "eod"    — best available: EOD close for the signal date (daily candle, not intraday)
            #   None     — today's signal with no EOD yet → JS uses live_price with "now" badge
            from datetime import timedelta as _td, datetime as _dt
            hist_price   = None
            price_source = None

            if s.generated_at:
                try:
                    et_dt    = s.generated_at - _td(hours=4)
                    et_date  = et_dt.date()
                    today_et = (_dt.utcnow() - _td(hours=4)).date()

                    if et_date < today_et:
                        # Past-date signal: always use EOD close for that trading day
                        ph = (
                            session.query(PriceHistory)
                            .filter(
                                PriceHistory.company_id == company.id,
                                PriceHistory.date <= et_date,
                            )
                            .order_by(PriceHistory.date.desc())
                            .first()
                        )
                        if ph:
                            hist_price   = float(ph.adjusted_close or ph.close or 0) or None
                            price_source = "eod"
                        else:
                            hist_price   = s.current_price
                            price_source = "stored" if hist_price else None
                    else:
                        # Today's signal — no EOD yet; JS will show live_price with "now" badge
                        # Use stored live price if available (set by live_price param in decision engine)
                        if s.current_price:
                            hist_price   = s.current_price
                            price_source = "exact"
                except Exception:
                    pass

            result.append({
                "id":              s.id,
                "ticker":          company.ticker,
                "signal":          s.signal,
                "confidence":      round(s.confidence or 0, 3),
                "margin_of_safety": round(s.margin_of_safety or 0, 3),
                "fud_score":       round(s.fud_score or 0, 3),
                "current_price":   hist_price,                # best historical price for this signal row
                "price_source":    price_source,              # "exact" | "eod" | None
                "live_price":      pc["price"],               # current live price for tape/card/buy modal
                "change_pct":      pc["change_pct"],
                "prev_close":      pc["prev_close"],
                "intrinsic_value": s.intrinsic_value_estimate,
                "generated_at":    s.generated_at.strftime("%Y-%m-%dT%H:%M:%S") if s.generated_at else "",
                "acted_on":        s.acted_on,
                "reasoning":       s.reasoning or "{}",
                # New signal fields (populated by updated decision/engine.py)
                "composite_score":      rsn.get("composite_score", 0.0),
                "momentum_score":       rsn.get("momentum_score", 0.0),
                "rvol":                 rsn.get("rvol", 1.0),
                "is_52w_breakout":      rsn.get("is_52w_breakout", False),
                "macd_direction":       rsn.get("macd_signal_direction", "neutral"),
                "tga_arrows":           rsn.get("tga_arrows", 0),
                "tga_signal":           rsn.get("tga_signal", "neutral"),
                "tga_sma":              rsn.get("tga_sma", False),
                "tga_macd":             rsn.get("tga_macd", False),
                "tga_stoch":            rsn.get("tga_stoch", False),
                "tga_vol":              rsn.get("tga_vol", False),
                "tga_reason":           rsn.get("tga_reason", ""),
                # If signal IS a buy, it passed all gates — treat as approved regardless of stale JSON
                "approved":             (s.signal or "").upper() in ("BUY", "STRONG_BUY") or rsn.get("approved", False),
                "why_buy":              rsn.get("why_buy", ""),
                "why_wait":             rsn.get("why_wait", ""),
                "ai_synthesis":         _synth.get(str(s.id), ""),
            })
        return result


_PAGE_INFO = {
    'brief':   ('<b>Morning Brief</b> — AI-generated daily market summary, regenerated at 06:00 and 09:00 ET. '
                'Covers S&amp;P 500 futures, VIX, oil, gold, BTC, 10yr yield, Nasdaq &amp; Dow. '
                'Includes economic calendar events, top WSB tickers, congressional trades, and crypto sentiment.'),
    'paper':   ('<b>Paper Trade</b> — $100k virtual account running the full 6-layer AI pipeline. '
                'Stage&nbsp;1: Monitoring (no trades). Stage&nbsp;2: Active AI (auto-buys every 5&nbsp;min during market hours). '
                'Stage&nbsp;3: Open positions with stop-loss &amp; take-profit monitoring every 60s. '
                'AI exit toggle per position. Threshold model swim lanes.'),
    'live':    ('<b>Live Trading</b> — <span style="color:#d29922">&#9888; Coming Soon.</span> '
                'Will execute real orders through the Schwab Trader API using the same 6-layer pipeline as Paper Trade. '
                'Dry-run mode remains ON until explicitly enabled by the account owner. All risk controls enforced: '
                'max 5% position, 25% sector cap, 3% daily loss halt.'),
    'wheel':   ('<b>Wheel Strategy</b> — Options income scanner targeting top-100 S&amp;P 500 names. '
                'Screens for IV&nbsp;Rank&nbsp;&gt;50%, 30&Delta; cash-secured put at 30&ndash;45&nbsp;DTE, '
                'bid/ask spread &lt;5% of mid. Phase progression: CSP &rarr; Shares (assigned) &rarr; Covered Call &rarr; Closed.'),
    'itool':   ('<b>I-Tool</b> — S&amp;P 500 technical scanner using Fibonacci retracements, VWAP deviation, '
                'RSI, MACD, and Bollinger Bands. Results sortable by any column. '
                'Bullish/bearish rows link to Stage Gate via +S1/+S2 buttons. '
                'Scan cache persists between visits. Does not run the AI fundamental pipeline.'),
    'signals': ('<b>Signal Monitor</b> — Live per-ticker AI composite view showing current gate breakdown: '
                'Reynolds filter, Quantum score, Ensemble, Risk/Reward, Kalman trend, momentum, RVOL. '
                'One row per watchlist ticker — current state only. '
                'Distinct from <i>AI Signal History</i> on the main page (that is a chronological event log; this shows now).'),
    'alfred':  ('<b>Alfred</b> — CME futures 5-day high/low forecasting dashboard for /ES, /MES, /NQ, /MNQ. '
                'ATR&times;&radic;N range model with VIX-regime scaling, RSI/MACD/SuperTrend bias, and TipRanks analyst targets (SPY/QQQ proxy). '
                'Refreshes every 5&nbsp;min during futures hours. Daily anchor tracks forecast revisions. '
                'Walk-forward backtest calibrates multipliers to 70% containment. Designed for selling naked calls and cash-secured puts.'),
}


def _page_info_html(key: str) -> str:
    text = _PAGE_INFO.get(key, '')
    if not text:
        return ''
    return (
        '<div class="page-info-bar pib-collapsed" id="page-info-bar">'
        '<button class="page-info-toggle" '
        r'onclick="var b=this.closest(\'.page-info-bar\');b.classList.toggle(\'pib-collapsed\')">'
        'ℹ️ About this page'
        '<span class="pib-arrow">&#9660;</span>'
        '</button>'
        f'<div class="page-info-content">{text}</div>'
        '</div>'
    )


def _nav_html(active: str = '') -> str:
    """Shared nav bar. active = 'brief'|'paper'|'r2000'|'live'|'wheel'|'itool'|'signals'"""
    back = ('<a href="/" class="nav-back-link" style="padding:5px 12px;border-radius:6px;border:1px solid #30363d;'
            'background:#21262d;color:#8b949e;text-decoration:none;font-size:12px;'
            'white-space:nowrap;align-self:center;">&#8592; NWO Monitor</a>')
    def btn(key, href, emoji, label, preview_text):
        hi = ' style="border-color:#58a6ff!important;background:rgba(88,166,255,0.15)!important;"' if key == active else ''
        return (f'<a href="{href}" class="brief-btn" id="{key}-btn"{hi}>'
                f'<span class="brief-btn-title">{emoji} {label}</span>'
                f'<span class="brief-btn-preview" id="{key}-preview">{preview_text}</span></a>')
    burger = (
        '<button class="nav-burger" aria-label="Menu"'
        ' onclick="(function(b){var h=b.closest(\'header\');if(h)h.classList.toggle(\'nav-open\');})(this)"'
        '>&#9776;</button>'
    )
    links = (
        '<div class="nav-links">'
        + back
        + btn('brief',   '/morning-brief',    '&#128202;', 'Morning Brief', 'Markets &middot; Futures &middot; WSB &middot; Crypto')
        + btn('paper',   '/paper/compare',     '&#127918;', 'Paper Trade',   '$100k Virtual &middot; 3-Stage AI Gate')
        + btn('r2000',   '/paper/russell2000','&#128202;', 'Russell 2000',  'Curated 49-stock small-cap watchlist')
        + btn('live',    '/live-trading',     '&#128185;', 'Live Trading',  'Schwab API &middot; Coming Soon')
        + btn('wheel',   '/wheel',            '&#127905;', 'Wheel',         'CSP &middot; Covered Call &middot; IV Rank')
        + btn('itool',   '/i-tool',           '&#128225;', 'I-Tool',        'S&amp;P 500 Technical Scanner')
        + btn('charts',  '/charts',           '&#128200;', 'Charts',        'Candles &middot; EMA &middot; TGA Panel')
        + btn('signals', '/signals',          '&#128200;', 'Signal Monitor','Gates &middot; Momentum &middot; RVOL &middot; Kalman')
        + btn('alfred',  '/alfred',           '&#128270;', 'Alfred',        'CME Futures &middot; 5-Day Forecast &middot; Backtest')
        + btn('pipeline','/pipeline',         '&#128301;', 'Pipeline',      'Health &middot; Decision Trace &middot; Restore Points')
        + '<span id="nwo-fetch-err" title="A background fetch failed — click to dismiss"'
        + ' onclick="this.style.display=\'none\'">&#9888; fetch error</span>'
        + '</div>'
    )
    err_js = (
        '<script>'
        'if(!window._nwoErr){'
        'var _nwoErrT;'
        'window._nwoErr=function(e){'
        'console.error("[NWO]",e);'
        'var el=document.getElementById("nwo-fetch-err");'
        'if(!el)return;'
        'el.style.display="inline-flex";'
        'clearTimeout(_nwoErrT);'
        '_nwoErrT=setTimeout(function(){el.style.display="none";},15000);'
        '};'
        '}'
        '</script>'
    )
    return burger + links + err_js


_NAV_CSS = """
  /* ── Sticky top banner ──────────────────────────────────────── */
  .sticky-banner { position: sticky; top: 0; z-index: 200; background: #0d1117; }
  /* ── Shared nav buttons ─────────────────────────────────────── */
  .brief-btn { display: flex; flex-direction: column; gap: 2px; padding: 6px 14px;
               border-radius: 6px; border: 1px solid #30363d; background: #161b22;
               text-decoration: none; color: #e6edf3; transition: background 0.15s; }
  .brief-btn:hover { background: #1c2e50; }
  .brief-btn-title { font-size: 12px; font-weight: 700; letter-spacing: 0.5px; }
  .brief-btn-preview { font-size: 10px; color: #8b949e; white-space: nowrap; }
  /* ── Shared ticker tape ─────────────────────────────────────── */
  .sh-tape-wrap  { overflow: hidden; background: #0a0f17; border-bottom: 1px solid #1f6feb; height: 26px; }
  .sh-tape-track { display: flex; gap: 24px; white-space: nowrap; will-change: transform;
                   animation: sh-tape 80s linear infinite; align-items: center; height: 100%;
                   padding-left: 12px; }
  .sh-tape-track:hover { animation-play-state: paused; }
  @keyframes sh-tape { 0%{transform:translateX(0)} 100%{transform:translateX(-50%)} }
  .sht-bull    { color: #3fb950; font-size: 12px; font-weight: 700; }
  .sht-bear    { color: #f85149; font-size: 12px; font-weight: 700; }
  .sht-neu     { color: #8b949e; font-size: 12px; }
  .sht-chg-up  { color: rgba(63,185,80,0.75);  font-size: 12px; }
  .sht-chg-dn  { color: rgba(248,81,73,0.75);  font-size: 12px; }
  .sht-sep     { color: #30363d; font-size: 10px; }
  /* ── Paper tape change classes ──────────────────────────────── */
  .pt-chg-up { color: rgba(63,185,80,0.75);  font-size: 12px; }
  .pt-chg-dn { color: rgba(248,81,73,0.75);  font-size: 12px; }
  /* ── Page info bar ──────────────────────────────────────────── */
  .page-info-bar { border-bottom: 1px solid #21262d; background: #0a0f17; }
  .page-info-toggle { width: 100%; text-align: left; padding: 4px 16px;
                      background: none; border: none; color: #8b949e; cursor: pointer;
                      font-size: 11px; display: flex; align-items: center; gap: 6px; }
  .page-info-toggle:hover { background: rgba(88,166,255,0.05); }
  .page-info-content { padding: 6px 16px 10px; font-size: 12px; color: #8b949e; line-height: 1.75; }
  .page-info-bar.pib-collapsed .page-info-content { display: none; }
  .pib-arrow { display: inline-block; transition: transform 0.2s; font-size: 10px; margin-left: auto; }
  .page-info-bar.pib-collapsed .pib-arrow { transform: rotate(-90deg); }
  /* ── Nav: desktop layout ─────────────────────────────────────── */
  .nav-links { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .nav-burger { display: none; align-items: center; justify-content: center;
                min-width: 34px; height: 30px; padding: 0 10px;
                background: #21262d; border: 1px solid #30363d; border-radius: 6px;
                color: #e6edf3; font-size: 18px; cursor: pointer; flex-shrink: 0; }
  /* ── Mobile nav ──────────────────────────────────────────────── */
  @media (max-width: 640px) {
    .nav-burger { display: flex; }
    .nav-links { display: none; flex-direction: column; gap: 4px; order: 99;
                 width: 100%; padding-top: 8px; margin-top: 4px;
                 border-top: 1px solid #21262d; }
    header.nav-open .nav-links { display: flex; }
    .brief-btn { width: 100%; }
    .brief-btn-preview { white-space: normal; }
    #model-nav { overflow-x: auto; flex-wrap: nowrap !important;
                 margin-left: 0 !important; width: 100%; padding-bottom: 2px;
                 scrollbar-width: none; }
    #model-nav::-webkit-scrollbar { display: none; }
    header { padding: 8px 12px !important; }
    header h1 { font-size: 15px !important; }
  }
  /* ── Fetch-error amber badge ────────────────────────────────────── */
  #nwo-fetch-err { display:none; align-items:center; gap:4px; padding:3px 8px;
    border-radius:6px; border:1px solid #d29922;
    background:rgba(210,153,34,0.15); color:#d29922;
    font-size:11px; white-space:nowrap; cursor:pointer; }
  #nwo-fetch-err:hover { background:rgba(210,153,34,0.25); }
"""

_NAV_TAPE_HTML = (
    '<div class="sh-tape-wrap">'
    '<div class="sh-tape-track" id="sh-tape"><span class="sht-neu">Loading signals…</span></div>'
    '</div>'
)

_NAV_TAPE_JS = """<script>
// ── Sticky banner wrapper ─────────────────────────────────────────────────────
(function() {
  var h = document.querySelector('header');
  if (!h || h.closest('.sticky-banner')) return;
  var t = h.nextElementSibling;
  var isTape = t && t.className && (t.className.indexOf('tape') >= 0);
  var w = document.createElement('div'); w.className = 'sticky-banner';
  h.parentNode.insertBefore(w, h); w.appendChild(h);
  if (isTape) w.appendChild(t);
})();
// ── Page-info bar (per-page static description, collapsed by default) ─────────
(function() {
  var INFO = {
    'brief-btn':   '<b>Morning Brief</b> \u2014 AI-generated daily market summary at 06:00 &amp; 09:00 ET. Covers S&amp;P futures, VIX, oil, gold, BTC, 10yr, Nasdaq, Dow; economic calendar; top WSB tickers; congressional trades; crypto sentiment.',
    'paper-btn':   '<b>Paper Trade</b> \u2014 $100k virtual account. Stage\u00a01: Monitoring. Stage\u00a02: Active AI auto-buys every 5&nbsp;min (market hours). Stage\u00a03: Open positions with stop-loss &amp; take-profit every 60s. AI exit toggle per position.',
    'live-btn':    '<b>Live Trading</b> \u2014 <span style="color:#d29922">&#9888; Coming Soon.</span> Real Schwab Trader API execution using the identical 6-layer pipeline. Dry-run stays ON until the account owner explicitly enables it. All risk controls enforced.',
    'wheel-btn':   '<b>Wheel Strategy</b> \u2014 Options income scanner. Screens top-100 S&amp;P 500 for IV\u00a0Rank\u00a0&gt;50%, 30\u0394 CSP at 30\u201345\u00a0DTE, spread\u00a0&lt;5% of mid. Tracks CSP \u2192 Shares \u2192 Covered Call \u2192 Closed.',
    'itool-btn':   '<b>I-Tool</b> \u2014 S&amp;P 500 technical scanner: Fibonacci, VWAP, RSI, MACD, Bollinger Bands. Sortable columns. +S1/+S2 buttons push directly to Stage Gate. Scan cache persists between visits. Does not run the AI fundamental pipeline.',
    'signals-btn': '<b>Signal Monitor</b> \u2014 Live per-ticker AI gate breakdown: Reynolds, Quantum, Ensemble, R/R, Kalman, momentum, RVOL. One row per ticker \u2014 current state only. Distinct from AI Signal History on the main page (that is a chronological event log).',
    'alfred-btn':  '<b>Alfred</b> \u2014 CME futures 5-day high/low forecast for /ES, /MES, /NQ, /MNQ. ATR\u00d7\u221aN model with VIX-regime scaling, RSI/MACD/SuperTrend bias &amp; TipRanks analyst targets (SPY/QQQ proxy). Updated every 5\u00a0min. Daily anchor tracks revisions. Backtest-calibrated to 70% containment.'
  };
  var activeKey = '';
  Object.keys(INFO).forEach(function(k) {
    var el = document.getElementById(k);
    if (el && (el.style.cssText || '').indexOf('58a6ff') >= 0) activeKey = k;
  });
  var text = INFO[activeKey];
  if (!text) return;
  var sb = document.querySelector('.sticky-banner') || document.querySelector('header');
  if (!sb) return;
  var bar = document.createElement('div');
  bar.className = 'page-info-bar pib-collapsed';
  bar.id = 'page-info-bar';
  bar.innerHTML = '<button class="page-info-toggle" onclick="var b=this.closest(\\'.page-info-bar\\');b.classList.toggle(\\'pib-collapsed\\')">&#8505;&#65039; About this page<span class="pib-arrow">&#9660;</span></button>'
    + '<div class="page-info-content">' + text + '</div>';
  sb.insertAdjacentElement('afterend', bar);
})();
// ── Ticker tape with dedup + change_pct-based arrows ─────────────────────────
(async function() {
  const track = document.getElementById('sh-tape');
  if (!track) return;
  try {
    const sigs = await fetch('/api/signals').then(r => r.json());
    const byT = new Map();
    (sigs || []).forEach(s => { if (!byT.has(s.ticker)) byT.set(s.ticker, s); });
    const items = [...byT.values()];
    if (!items.length) { track.innerHTML = '<span class="sht-neu">No signals</span>'; return; }
    const all = [...items, ...items];
    track.innerHTML = all.map(s => {
      const sig = (s.signal || '').toUpperCase();
      const bull = sig === 'BUY' || sig === 'STRONG_BUY';
      const bear = sig === 'SELL' || sig === 'STRONG_SELL';
      const chg  = s.change_pct;
      const up   = chg != null ? chg > 0 : null;
      let cls, arr;
      if (bull)            { cls = 'sht-bull';   arr = '&#9650;'; }
      else if (bear)       { cls = 'sht-bear';   arr = '&#9660;'; }
      else if (up === true){ cls = 'sht-chg-up'; arr = '&#9650;'; }
      else if (up===false) { cls = 'sht-chg-dn'; arr = '&#9660;'; }
      else                 { cls = 'sht-neu';    arr = '&#8212;'; }
      const p = s.current_price;
      return '<span class="' + cls + '">' + arr + ' ' + s.ticker + (p ? ' $' + Number(p).toFixed(2) : '') + '</span>'
           + '<span class="sht-sep">|</span>';
    }).join('');
    track.style.animationDuration = Math.max(40, items.length * 0.8) + 's';
  } catch(e) { console.warn('[NWO] tape',e); }
})();
</script>"""


def _recent_trades(limit: int = 30) -> list:
    with Session() as session:
        rows = (
            session.query(TradeLog)
            .order_by(TradeLog.created_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "ticker":    t.ticker,
                "action":    t.action,
                "quantity":  t.quantity,
                "price":     t.price_at_execution,
                "total":     t.total_value,
                "dry_run":   t.dry_run,
                "status":    t.status,
                "executed_at": _to_et_str(t.executed_at, "%Y-%m-%d %H:%M:%S ET") if t.executed_at else "",
                "notes":     t.notes or "",
            }
            for t in rows
        ]


def _tail_log(lines: int = 80) -> list[str]:
    # Prefer the live server log (written by this process); fall back to legacy main.py log
    target = LIVE_LOG if LIVE_LOG.exists() else LOG_FILE
    if not target.exists():
        return ["No log file found yet — restart the server to begin live logging."]
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
        return text.splitlines()[-lines:]
    except Exception as e:
        return [f"Could not read log: {e}"]


# ── API routes ────────────────────────────────────────────────────────────────

@app.get("/api/status")
def api_status():
    return {
        "paused":           _is_paused(),
        "dry_run":          config.risk.dry_run,
        "watchlist":        config.watchlist,
        "ai_watch_tickers": getattr(config, "ai_watch_tickers", []),
        "timestamp":        datetime.now(_ET).strftime("%Y-%m-%d %H:%M:%S ET"),
    }


@app.get("/api/signals")
def api_signals():
    return _recent_signals()


@app.post("/api/signals/synthesize")
async def api_signals_synthesize(request: Request):
    """Accepts {"ids": [1,2,3]} (max 5). Returns {"1": "synthesis text", ...}. Caches permanently."""
    import json as _j
    api_key = config.brief.anthropic_api_key
    if not api_key:
        return JSONResponse({"error": "no_api_key"})
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({})
    ids = [int(i) for i in (payload.get("ids") or [])[:5]]
    if not ids:
        return JSONResponse({})

    cache   = _load_synth_cache()
    result  = {}
    changed = False

    uncached = [i for i in ids if str(i) not in cache]
    if uncached:
        with Session() as session:
            sigs = {
                s.id: s for s in
                session.query(TradeSignal).filter(TradeSignal.id.in_(uncached)).all()
            }
            ticker_map = {
                c.id: c.ticker for c in
                session.query(Company).filter(Company.id.in_([s.company_id for s in sigs.values()])).all()
            }
        for sid in uncached:
            s = sigs.get(sid)
            if not s:
                continue
            sig_dict = {
                "ticker":           ticker_map.get(s.company_id, "?"),
                "signal":           s.signal,
                "confidence":       s.confidence or 0,
                "fud_score":        s.fud_score or 0,
                "margin_of_safety": s.margin_of_safety or 0,
                "current_price":    s.current_price or 0,
                "intrinsic_value":  s.intrinsic_value_estimate or 0,
                "reasoning":        s.reasoning or "{}",
            }
            note = _generate_signal_synthesis(sig_dict, api_key)
            if note:
                cache[str(sid)] = note
                changed = True
                try:
                    import json as _jl
                    _rsn = sig_dict.get("reasoning", {})
                    if isinstance(_rsn, str):
                        try: _rsn = _jl.loads(_rsn)
                        except Exception: _rsn = {}
                    _append_thesis_log({
                        "signal_id":        sid,
                        "ticker":           sig_dict["ticker"],
                        "signal":           sig_dict["signal"],
                        "composite_score":  _rsn.get("composite_score", 0.0),
                        "momentum_score":   _rsn.get("momentum_score",  0.0),
                        "fud_score":        sig_dict.get("fud_score", 0.0),
                        "margin_of_safety": sig_dict.get("margin_of_safety", 0.0),
                        "confidence":       sig_dict.get("confidence", 0.0),
                        "thesis_text":      note,
                        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                    })
                except Exception:
                    pass  # log append must never crash the synthesis response
            result[str(sid)] = note

    for i in ids:
        if str(i) in cache and str(i) not in result:
            result[str(i)] = cache[str(i)]

    if changed:
        _save_synth_cache(cache)

    return JSONResponse(result)


@app.get("/api/trades")
def api_trades():
    return _recent_trades()


@app.get("/api/logs")
def api_logs():
    return {"lines": _tail_log()}


@app.get("/api/positions")
def api_positions():
    """Live positions from Schwab — shares held, avg cost, market value, P&L."""
    try:
        md = SchwabMarketData()
        positions = md.get_positions()
        # Filter to equity only, skip zero-quantity rows
        return [p for p in positions if p.get("quantity") and p.get("asset_type") in (None, "EQUITY", "ETF")]
    except Exception as e:
        return JSONResponse(status_code=503, content={"error": str(e)})


@app.post("/api/pause")
def api_pause():
    if _is_paused():
        PAUSE_FLAG.unlink(missing_ok=True)
        return {"paused": False, "message": "System resumed"}
    else:
        PAUSE_FLAG.parent.mkdir(parents=True, exist_ok=True)
        PAUSE_FLAG.touch()
        return {"paused": True, "message": "System paused"}


# ── Diagnostics ───────────────────────────────────────────────────────────────

@app.get("/api/diagnostics")
def api_diagnostics():
    results = []
    with Session() as session:
        for ticker in config.watchlist:
            company = session.query(Company).filter_by(ticker=ticker).first()
            if not company:
                results.append({
                    "ticker": ticker, "status": "missing",
                    "price_days": 0, "latest_price": None,
                    "fundamental_years": 0, "latest_fundamental": None,
                    "signal_count": 0,
                    "message": "No company record — run main.py to ingest",
                })
                continue

            price_days = session.query(PriceHistory).filter_by(company_id=company.id).count()
            latest_ph  = (
                session.query(PriceHistory)
                .filter_by(company_id=company.id)
                .order_by(PriceHistory.date.desc())
                .first()
            )
            fund_years = (
                session.query(Fundamental.fiscal_year)
                .filter_by(company_id=company.id, fiscal_quarter=0)
                .distinct().count()
            )
            latest_fund = (
                session.query(Fundamental.fiscal_year)
                .filter_by(company_id=company.id, fiscal_quarter=0)
                .order_by(Fundamental.fiscal_year.desc())
                .limit(1)
                .scalar()
            )
            signal_count = session.query(TradeSignal).filter_by(company_id=company.id).count()

            if price_days == 0:
                status = "no_prices"
                message = "No price history — needs backfill"
            elif price_days < 64:
                status = "insufficient"
                message = f"Only {price_days} days — need 64+ for FFT signals"
            elif fund_years == 0:
                status = "no_fundamentals"
                message = "Prices OK — no EDGAR fundamentals (pre-revenue or not SEC filer)"
            else:
                status = "ready"
                message = f"{price_days} days prices | {fund_years} yrs fundamentals | {signal_count} signals"

            results.append({
                "ticker":            ticker,
                "status":            status,
                "price_days":        price_days,
                "latest_price":      latest_ph.date.strftime("%Y-%m-%d") if latest_ph and latest_ph.date else None,
                "fundamental_years": fund_years,
                "latest_fundamental": latest_fund,
                "signal_count":      signal_count,
                "message":           message,
            })
    return results


# ── Backfill ──────────────────────────────────────────────────────────────────

_backfill_state: dict = {"running": False, "log": [], "done": False}


def _run_backfill(days: int):
    _backfill_state["running"] = True
    _backfill_state["done"] = False
    _backfill_state["log"] = [f"Starting {days}-day backfill for {len(config.watchlist)} tickers..."]

    try:
        from pipeline.ingestion import IngestionPipeline
        pipeline = IngestionPipeline(db_session_factory=Session)

        for i, ticker in enumerate(config.watchlist, 1):
            _backfill_state["log"].append(f"[{i}/{len(config.watchlist)}] {ticker} — fetching prices...")
            try:
                ok_p = pipeline.ingest_price_history(ticker, days=days)
                _backfill_state["log"].append(
                    f"  Prices: {'✓' if ok_p else '✗ (no data returned)'}"
                )
                ok_f = pipeline.ingest_fundamentals(ticker)
                _backfill_state["log"].append(
                    f"  Fundamentals: {'✓' if ok_f else '✗ (not an SEC filer or pre-revenue)'}"
                )
            except Exception as e:
                _backfill_state["log"].append(f"  Error: {e}")

        _backfill_state["log"].append("Backfill complete.")
    except Exception as e:
        _backfill_state["log"].append(f"Backfill failed: {e}")
    finally:
        _backfill_state["running"] = False
        _backfill_state["done"] = True


@app.post("/api/backfill")
def api_backfill(days: int = 365):
    if _backfill_state["running"]:
        return {"started": False, "message": "Backfill already in progress"}
    _backfill_state["log"] = []
    _backfill_state["done"] = False
    thread = threading.Thread(target=_run_backfill, args=(days,), daemon=True)
    thread.start()
    return {"started": True, "message": f"Backfill started ({days} days)"}


@app.get("/api/backfill/status")
def api_backfill_status():
    return {
        "running": _backfill_state["running"],
        "done":    _backfill_state["done"],
        "log":     _backfill_state["log"],
    }


# ── R2000 backfill ────────────────────────────────────────────────────────────

_r2k_backfill_state: dict = {"running": False, "done": False, "log": [], "total": 0, "completed": 0}


def _run_r2000_backfill(days: int):
    import json
    from pathlib import Path
    from pipeline.ingestion import IngestionPipeline
    from models.database import init_db
    from config import config as _cfg

    sg_path = Path("data/stagegate_russell2000.json")
    if not sg_path.exists():
        _r2k_backfill_state["log"].append("stagegate_russell2000.json not found")
        _r2k_backfill_state["running"] = False
        _r2k_backfill_state["done"] = True
        return

    universe = json.loads(sg_path.read_text(encoding="utf-8")).get("stage1", [])
    _r2k_backfill_state["total"] = len(universe)
    _r2k_backfill_state["completed"] = 0
    _r2k_backfill_state["log"] = [f"Starting R2000 backfill: {len(universe)} tickers × {days} days"]

    try:
        _, Session = init_db(_cfg.database.url, echo=False)
        pipeline = IngestionPipeline(db_session_factory=Session)
        for i, ticker in enumerate(universe, 1):
            try:
                ok = pipeline.ingest_price_history(ticker, days=days)
                _r2k_backfill_state["log"].append(f"[{i}/{len(universe)}] {ticker}: {'✓' if ok else '✗'}")
            except Exception as e:
                _r2k_backfill_state["log"].append(f"[{i}/{len(universe)}] {ticker}: error — {e}")
            _r2k_backfill_state["completed"] = i
        _r2k_backfill_state["log"].append("R2000 backfill complete.")
    except Exception as e:
        _r2k_backfill_state["log"].append(f"R2000 backfill failed: {e}")
    finally:
        _r2k_backfill_state["running"] = False
        _r2k_backfill_state["done"] = True


@app.post("/api/backfill/r2000")
def api_backfill_r2000(days: int = 365):
    if _r2k_backfill_state["running"]:
        return {"started": False, "message": "R2000 backfill already in progress"}
    _r2k_backfill_state.update({"running": True, "done": False, "log": [], "completed": 0})
    threading.Thread(target=_run_r2000_backfill, args=(days,), daemon=True).start()
    return {"started": True, "message": f"R2000 backfill started ({days} days)"}


@app.get("/api/backfill/r2000/status")
def api_backfill_r2000_status():
    return {k: _r2k_backfill_state[k] for k in ("running", "done", "total", "completed", "log")}


# ── TipRanks API endpoints ────────────────────────────────────────────────────

@app.get("/api/tipranks/all")
def api_tipranks_all():
    """Return entire tipranks_scan.json results dict for bulk JS use."""
    import json
    from pathlib import Path
    try:
        data = json.loads(Path("data/tipranks_scan.json").read_text(encoding="utf-8"))
        return data.get("results", {})
    except Exception:
        return {}


@app.get("/api/tipranks/ticker/{ticker}")
def api_tipranks_ticker(ticker: str):
    """Return cached TipRanks data for a single ticker."""
    from monitor.tipranks_scanner import get_cached_signal
    d = get_cached_signal(ticker.upper())
    return d if d else {"error": "not_cached"}


@app.get("/api/tipranks/status")
def api_tipranks_status():
    """Return TipRanks scanner status + cookie availability."""
    from monitor.tipranks_scanner import get_status
    from data_sources.tipranks_client import TipRanksClient
    d = get_status()
    d["cookies_ok"] = TipRanksClient.cookies_available()
    return d


@app.post("/api/tipranks/reload-cookies")
def api_tipranks_reload_cookies():
    """Re-read cookie file after user exports fresh cookies from browser."""
    from data_sources.tipranks_client import get_client, TipRanksClient
    if not TipRanksClient.cookies_available():
        return {"ok": False, "error": "data/tipranks_cookies.json not found"}
    try:
        get_client().reload_cookies()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/tipranks/scan")
def api_tipranks_scan_now(background_tasks=None):
    """Trigger an immediate TipRanks scan for all watchlist + stagegate tickers."""
    import threading, json
    from pathlib import Path as _Path
    from config import config as _cfg
    from paper.executor import PAPER_MODEL_CONFIGS
    from monitor.tipranks_scanner import run_scan

    tickers: set = set(_cfg.watchlist)
    for model, cfg in PAPER_MODEL_CONFIGS.items():
        try:
            sg = json.loads(_Path(cfg["stagegate"]).read_text(encoding="utf-8"))
            for k in ("stage1", "stage2", "stage3"):
                tickers.update(sg.get(k, []))
        except Exception:
            pass
    r2k = _Path("data/stagegate_russell2000.json")
    if r2k.exists():
        tickers.update(json.loads(r2k.read_text(encoding="utf-8")).get("stage1", []))

    t = threading.Thread(target=run_scan, args=(list(tickers),), daemon=True, name="tipranks-manual")
    t.start()
    return {"status": "started", "tickers": len(tickers)}


# ── Dashboard HTML ────────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NWO Monitor</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d1117; color: #e6edf3; font-family: 'Segoe UI', monospace; font-size: 14px; }
  header { background: #161b22; padding: 10px 16px 0; border-bottom: 1px solid #30363d;
           display: flex; flex-direction: column; gap: 0; }
  .header-top { display: flex; align-items: center; gap: 12px; padding-bottom: 8px; flex-wrap: wrap; }
  header h1 { font-size: 16px; letter-spacing: 2px; color: #58a6ff; white-space: nowrap; }
  .header-nav { display: flex; gap: 6px; flex-wrap: wrap; padding-bottom: 8px; }
  .brief-btn { display: flex; align-items: center; gap: 5px; padding: 5px 10px;
               border-radius: 6px; border: 1px solid #1f6feb; background: #0d1e36;
               color: #58a6ff; text-decoration: none; font-size: 12px; font-weight: 600;
               white-space: nowrap; transition: background 0.15s; }
  .brief-btn:hover { background: #1c2e50; }
  .brief-btn-title { font-size: 12px; font-weight: 700; }
  .brief-btn-preview { display: none; }
  .header-spacer { flex: 1; }
  .badges { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .badge { padding: 4px 10px; border-radius: 12px; font-size: 12px; font-weight: 600; }
  .badge-green  { background: #1a4731; color: #3fb950; }
  .badge-yellow { background: #3d2b00; color: #d29922; }
  .badge-red    { background: #4a1519; color: #f85149; }
  .badge-blue   { background: #1c2e50; color: #58a6ff; }
  #pause-btn { padding: 6px 16px; border-radius: 6px; border: 1px solid #30363d;
               cursor: pointer; font-size: 13px; background: #21262d; color: #e6edf3; }
  #pause-btn:hover { background: #30363d; }
  #pause-btn.paused { background: #4a1519; color: #f85149; border-color: #f85149; }
  main { padding: 16px; display: grid; gap: 16px;
         grid-template-columns: 1fr; }
  section { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px; }
  section.full-width { grid-column: 1 / -1; }
  h2 { font-size: 13px; color: #8b949e; text-transform: uppercase; letter-spacing: 1px;
       margin-bottom: 10px; border-bottom: 1px solid #30363d; padding-bottom: 6px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; color: #8b949e; font-weight: 500; padding: 4px 8px; }
  td { padding: 5px 8px; border-top: 1px solid #21262d; }
  tr:hover td { background: #1c2128; }
  .signal-buy  { color: #3fb950; font-weight: 700; }
  .signal-sell { color: #f85149; font-weight: 700; }
  .signal-hold { color: #d29922; font-weight: 700; }
  .conf-bar  { height: 8px; border-radius: 4px; background: #21262d; width: 80px;
               display: block; margin-bottom: 2px; }
  .conf-fill { height: 8px; border-radius: 4px; background: #3fb950;
               display: block; min-width: 2px; }
  .conf-fill.medium { background: #d29922; }
  .conf-fill.low    { background: #f85149; }
  .gate-pip { display: inline-block; width: 18px; height: 18px; line-height: 18px;
              text-align: center; border-radius: 3px; font-size: 10px; font-weight: 700;
              margin-right: 2px; }
  .gate-pass { background: #1a3a22; color: #3fb950; }
  .gate-fail { background: #3a1a1a; color: #f85149; }
  .score-bar { height: 6px; border-radius: 3px; background: #21262d; width: 60px;
               display: block; margin-top: 3px; }
  .score-fill { height: 6px; border-radius: 3px; display: block; }
  #log-box { background: #0d1117; border: 1px solid #21262d; border-radius: 4px;
             padding: 10px; height: 300px; overflow-y: auto; font-family: monospace;
             font-size: 12px; line-height: 1.5; }
  .log-error   { color: #f85149; }
  .log-warning { color: #d29922; }
  .log-info    { color: #e6edf3; }
  .dry-run-tag { background: #1c2e50; color: #58a6ff; font-size: 11px;
                 padding: 1px 6px; border-radius: 10px; margin-left: 6px; }
  #refresh-ts { font-size: 11px; color: #8b949e; }
  .empty { color: #8b949e; font-style: italic; padding: 8px; }
  th[title] { cursor: help; border-bottom: 1px dashed #8b949e; }
  .mos-good { color: #3fb950; font-weight: 600; }
  .mos-warn { color: #d29922; font-weight: 600; }
  .mos-bad  { color: #f85149; font-weight: 600; }
  .threshold-legend { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; font-size: 11px; }
  .leg-title { color: #8b949e; padding: 3px 0; }
  .leg-item  { padding: 2px 8px; border-radius: 10px; }
  .leg-buy   { background: #1a4731; color: #3fb950; }
  .leg-sell  { background: #4a1519; color: #f85149; }
  .leg-hold  { background: #3d2b00; color: #d29922; }
  .action-btn { padding: 3px 10px; border-radius: 5px; border: 1px solid #30363d;
                cursor: pointer; font-size: 11px; background: #21262d; color: #e6edf3; }
  .action-btn:hover { background: #30363d; }
  .model-bar { display: flex; align-items: center; gap: 8px; margin-bottom: 10px;
               padding: 8px 10px; background: #0d1117; border-radius: 6px; border: 1px solid #30363d; }
  .model-label { font-size: 11px; color: #8b949e; }
  .model-btn { padding: 4px 12px; border-radius: 5px; border: 1px solid #30363d; cursor: pointer;
               font-size: 11px; font-weight: 600; background: #21262d; color: #8b949e;
               transition: all 0.15s; }
  .model-btn:hover { border-color: #58a6ff; color: #e6edf3; }
  .model-btn.active-0 { background: #1a3a22; border-color: #3fb950; color: #3fb950; }
  .model-btn.active-1 { background: #3d2b00; border-color: #d29922; color: #d29922; }
  .model-btn.active-2 { background: #4a1519; border-color: #f85149; color: #f85149; }
  .model-cycle { padding: 4px 12px; border-radius: 5px; border: 1px solid #30363d; cursor: pointer;
                 font-size: 11px; background: #21262d; color: #8b949e; margin-left: 6px; }
  .model-cycle.cycling { border-color: #58a6ff; color: #58a6ff; background: #1c2e50; }
  .model-indicator { font-size: 11px; color: #8b949e; margin-left: auto; }
  .would-buy { color: #3fb950; font-size: 10px; font-weight: 700; }
  .status-ready  { color: #3fb950; }
  .status-warn   { color: #d29922; }
  .status-error  { color: #f85149; }
  #backfill-log { background: #0d1117; border: 1px solid #21262d; border-radius: 4px;
                  padding: 10px; max-height: 200px; overflow-y: auto;
                  font-family: monospace; font-size: 12px; line-height: 1.6; color: #8b949e; }
  /* ── Sticky banner ───────────────────────────────────────────────── */
  .sticky-banner { position: sticky; top: 0; z-index: 200; background: #0d1117; }
  /* ── Ticker tape (main dashboard) ──────────────────────────────── */
  .tape-wrap  { overflow: hidden; background: #0a0f17;
                border-bottom: 1px solid #1f6feb; height: 26px; flex-shrink: 0; }
  .tape-track { display: flex; gap: 24px; white-space: nowrap; will-change: transform;
                animation: main-tape 80s linear infinite; align-items: center; height: 100%;
                padding-left: 12px; }
  .tape-track:hover { animation-play-state: paused; }
  @keyframes main-tape { 0%{transform:translateX(0)} 100%{transform:translateX(-50%)} }
  .mt-bull   { color: #3fb950; font-size: 12px; font-weight: 700; }   /* BUY signal */
  .mt-bear   { color: #f85149; font-size: 12px; font-weight: 700; }   /* SELL signal */
  .mt-chg-up { color: #7ee8a2; font-size: 12px; }                     /* price up, neutral signal */
  .mt-chg-dn { color: #ff8a80; font-size: 12px; }                     /* price down, neutral signal */
  .mt-neu    { color: #8b949e; font-size: 12px; }
  .mt-sep    { color: #30363d; font-size: 10px; }
  /* ── Info modal (main page) ─────────────────────────────────────── */
  .nwo-info-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.8);
                      display: none; align-items: center; justify-content: center; z-index: 9999; }
  .nwo-info-overlay.open { display: flex; }
  .nwo-info-modal { background: #161b22; border: 1px solid #30363d; border-radius: 10px;
                    padding: 24px; max-width: 600px; width: 92%; max-height: 88vh; overflow-y: auto; }
  .nwo-info-modal h2 { font-size: 15px; color: #58a6ff; margin-bottom: 4px; }
  .nwo-info-modal h3 { font-size: 11px; color: #8b949e; text-transform: uppercase;
                       letter-spacing: 1px; margin: 14px 0 8px; border-bottom: 1px solid #21262d; padding-bottom: 4px; }
  .nwo-info-row { display: flex; gap: 10px; margin-bottom: 9px; align-items: flex-start; }
  .nwo-info-tag { font-size: 10px; font-weight: 700; color: #e6edf3; min-width: 110px;
                  background: #21262d; padding: 2px 7px; border-radius: 4px; flex-shrink: 0; }
  .nwo-info-desc { font-size: 12px; color: #8b949e; line-height: 1.5; }
  .nwo-info-close { float: right; background: none; border: 1px solid #30363d;
                    color: #8b949e; cursor: pointer; padding: 4px 10px; border-radius: 4px; font-size: 12px; }
  .nwo-info-close:hover { color: #f85149; }
  /* ── Hero grid (landing page) ────────────────────────────────── */
  .hero-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 14px; margin-bottom: 16px; }
  .hero-card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px; }
  .hero-card-title { font-size: 10px; color: #8b949e; text-transform: uppercase; letter-spacing: 1px;
                     display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
  .hero-card-snap  { font-size: 11px; color: #8b949e; margin-bottom: 8px; }
  .hero-card-text  { font-size: 12px; color: #c9d1d9; line-height: 1.5; }
  .hero-pos-row, .hero-trade-row { display: flex; justify-content: space-between;
                                   padding: 4px 0; border-bottom: 1px solid #21262d; font-size: 11px; }
  .hero-trade-row  { gap: 8px; }
  @media (max-width: 900px) {
    main { grid-template-columns: 1fr; }
    section.full-width { grid-column: 1; }
    table { font-size: 11px; }
    th, td { padding: 4px 5px; }
    .conf-bar { width: 50px; }
    .score-bar { width: 40px; }
    .model-bar { flex-wrap: wrap; gap: 6px; }
    .model-indicator { display: none; }
    .hero-grid { grid-template-columns: 1fr; }
    .badges { gap: 4px; }
  }
  /* ── Thesis Pattern Analysis panel ──────────────────────────────── */
  #thesis-analysis-panel { display: none; }
  #thesis-analysis-panel.tap-visible {
    display: block; background: #161b22;
    border: 1px solid #d29922; border-left: 3px solid #d29922;
    border-radius: 8px; margin-bottom: 16px;
  }
  #thesis-analysis-panel details { padding: 0; }
  #thesis-analysis-panel summary {
    list-style: none; padding: 10px 14px; cursor: pointer;
    display: flex; align-items: center; gap: 10px; user-select: none;
  }
  #thesis-analysis-panel summary::-webkit-details-marker { display: none; }
  .tap-title { font-size: 12px; font-weight: 700; color: #d29922;
               text-transform: uppercase; letter-spacing: 1px; }
  .tap-badge { font-size: 10px; padding: 2px 8px; border-radius: 10px;
               background: rgba(210,153,34,0.15); color: #d29922;
               border: 1px solid rgba(210,153,34,0.3); white-space: nowrap; }
  .tap-ts    { font-size: 10px; color: #8b949e; margin-left: auto; }
  .tap-arrow { font-size: 10px; color: #8b949e; transition: transform 0.2s; }
  #thesis-analysis-panel details[open] .tap-arrow { transform: rotate(90deg); }
  .tap-body  { padding: 10px 14px 12px; font-size: 12px; color: #c9d1d9;
               line-height: 1.65; border-top: 1px solid #21262d; }
</style>
</head>
<body>
<!-- NWO info modal -->
<div class="nwo-info-overlay" id="nwo-info-overlay" onclick="if(event.target===this)document.getElementById('nwo-info-overlay').classList.remove('open')">
  <div class="nwo-info-modal">
    <button class="nwo-info-close" onclick="document.getElementById('nwo-info-overlay').classList.remove('open')">&#x2715; Close</button>
    <h2>&#9432; How NWO AI Works</h2>
    <h3>6-Layer Pipeline</h3>
    <div class="nwo-info-row"><span class="nwo-info-tag">L1 Ingestion</span><span class="nwo-info-desc">EDGAR fundamentals + Schwab price history → SQLite DB. Runs daily at 6am ET + intraday every 5 min.</span></div>
    <div class="nwo-info-row"><span class="nwo-info-tag">L2 Analysis</span><span class="nwo-info-desc">First-principles valuation: ROIC, moat score, DCF intrinsic value, owner earnings.</span></div>
    <div class="nwo-info-row"><span class="nwo-info-tag">Signals</span><span class="nwo-info-desc">FFT cycle detection · Fibonacci levels · Insider flow · VWAP · Volume profile · VIX regime · Momentum (RVOL/MACD/MA stack/ATR/52W breakout).</span></div>
    <div class="nwo-info-row"><span class="nwo-info-tag">L3 FUD Filter</span><span class="nwo-info-desc">News quality scoring + FUD attack detection. Articles scored for relevance, credibility, and manipulation signals. Blocks trades on coordinated FUD.</span></div>
    <div class="nwo-info-row"><span class="nwo-info-tag">L4 Decision</span><span class="nwo-info-desc">7-gate engine: Reynolds turbulence · Ensemble BMA (P(bull) &gt;50%) · Quantum state · Kalman innovation · Risk/reward · FUD gate · Signal threshold.</span></div>
    <div class="nwo-info-row"><span class="nwo-info-tag">L5 Risk</span><span class="nwo-info-desc">Behavioral psychology filters · Kelly criterion position sizing · Max $500/trade · Max 5% position · Max 5 trades/day.</span></div>
    <div class="nwo-info-row"><span class="nwo-info-tag">L6 Executor</span><span class="nwo-info-desc">Order placement via Schwab API. DRY RUN = True by default — trades are logged but not sent.</span></div>
    <h3>Signal Weights (Composite Score)</h3>
    <div class="nwo-info-row"><span class="nwo-info-tag">Fundamentals</span><span class="nwo-info-desc">25% — ROIC, moat, DCF valuation</span></div>
    <div class="nwo-info-row"><span class="nwo-info-tag">Momentum</span><span class="nwo-info-desc">20% — RVOL, MACD, MA stack, ATR, 52W breakout</span></div>
    <div class="nwo-info-row"><span class="nwo-info-tag">Insider Flow</span><span class="nwo-info-desc">20% — SEC Form 4 insider buy/sell activity</span></div>
    <div class="nwo-info-row"><span class="nwo-info-tag">Technical</span><span class="nwo-info-desc">15% — VWAP position, Fibonacci levels</span></div>
    <div class="nwo-info-row"><span class="nwo-info-tag">Cycle</span><span class="nwo-info-desc">10% — FFT cycle phase</span></div>
    <div class="nwo-info-row"><span class="nwo-info-tag">Volume</span><span class="nwo-info-desc">10% — Volume profile, RVOL confirmation</span></div>
    <h3>AI Watch (Priority Tickers)</h3>
    <div class="nwo-info-row"><span class="nwo-info-tag">Scan frequency</span><span class="nwo-info-desc">Every 1 minute during market hours (vs 5 min for standard watchlist)</span></div>
    <div class="nwo-info-row"><span class="nwo-info-tag">Breakout override</span><span class="nwo-info-desc">When RVOL ≥1.5× + momentum confirmed: fundamentals score floored at 0 (premium valuation can't block a breakout), Reynolds + Kalman gates relaxed.</span></div>
  </div>
</div>
<header>
  <div class="header-top">
    <h1>NWO MONITOR</h1>
    <div class="header-spacer"></div>
    <div class="badges">
      <span id="hdr-daily-pnl" style="padding:3px 8px;border-radius:6px;border:1px solid #30363d;background:#0d1117;font-size:11px;color:#8b949e;white-space:nowrap" title="Paper account daily P&amp;L">Daily: —</span>
      <span id="hdr-total-pnl" style="padding:3px 8px;border-radius:6px;border:1px solid #30363d;background:#0d1117;font-size:11px;color:#8b949e;white-space:nowrap" title="Paper account total P&amp;L">Total: —</span>
      <span id="status-badge" class="badge badge-blue">Loading...</span>
      <span id="mode-badge"   class="badge badge-yellow">DRY RUN</span>
      <button id="pause-btn" onclick="togglePause()">Pause</button>
      <button style="padding:4px 8px;border-radius:6px;border:1px solid #30363d;background:transparent;color:#58a6ff;cursor:pointer;font-size:11px;" onclick="document.getElementById('nwo-info-overlay').classList.add('open')" title="How NWO AI works">&#9432; AI Logic</button>
      <span id="nwo-fetch-err" title="A background fetch failed — click to dismiss" onclick="this.style.display='none'" style="display:none;align-items:center;gap:4px;padding:3px 8px;border-radius:6px;border:1px solid #d29922;background:rgba(210,153,34,0.15);color:#d29922;font-size:11px;white-space:nowrap;cursor:pointer;">&#9888; fetch error</span>
      <span id="refresh-ts"></span>
    </div>
  </div>
  <div class="header-nav">
    <a href="/morning-brief" class="brief-btn" id="brief-btn">&#128202; Morning Brief</a>
    <a href="/i-tool"        class="brief-btn" id="itool-btn">&#128225; I-Tool</a>
    <a href="/paper/compare" class="brief-btn" id="paper-btn">&#127918; Paper Trade</a>
    <a href="/paper/russell2000" class="brief-btn" id="r2000-btn">&#128202; Russell 2000</a>
    <a href="/wheel"         class="brief-btn" id="wheel-btn">&#127905; Wheel</a>
    <a href="/charts"        class="brief-btn" id="charts-btn">&#128200; Charts</a>
    <a href="/signals"       class="brief-btn" id="signals-btn">&#128200; Signal Monitor</a>
    <a href="/alfred"        class="brief-btn" id="alfred-btn">&#128270; Alfred</a>
  </div>
</header>
<div class="tape-wrap"><div class="tape-track" id="main-tape"><span class="mt-neu">Loading signals...</span></div></div>
<script>
(function(){var h=document.querySelector('header');if(!h||h.closest('.sticky-banner'))return;
var t=h.nextElementSibling;var isTape=t&&t.className&&t.className.indexOf('tape')>=0;
var w=document.createElement('div');w.className='sticky-banner';
h.parentNode.insertBefore(w,h);w.appendChild(h);if(isTape)w.appendChild(t);})();
</script>
<script>
if(!window._nwoErr){var _nwoErrT;window._nwoErr=function(e){console.error('[NWO]',e);var el=document.getElementById('nwo-fetch-err');if(!el)return;el.style.display='inline-flex';clearTimeout(_nwoErrT);_nwoErrT=setTimeout(function(){el.style.display='none';},15000);};}
</script>
<main>
  <!-- ── Hero dashboard cards ──────────────────────────────────────── -->
  <div class="hero-grid">
    <div class="hero-card">
      <div class="hero-card-title">
        <span>&#128202; Morning Brief</span>
        <div style="display:flex;gap:8px;align-items:center">
          <a href="/morning-brief" style="color:#58a6ff;font-size:10px;text-decoration:none;">View Full &#8594;</a>
          <button onclick="loadHeroBrief()" style="background:none;border:none;color:#8b949e;cursor:pointer;font-size:14px;padding:0;line-height:1" title="Refresh">&#8635;</button>
        </div>
      </div>
      <div id="hc-brief-markets" style="display:grid;grid-template-columns:1fr 1fr;gap:2px 12px;margin-bottom:8px;font-size:11px;"></div>
      <div id="hc-brief-text" class="hero-card-text" style="margin-bottom:8px;">Loading brief...</div>
      <div id="hc-brief-wsb" style="font-size:10px;color:#8b949e;"></div>
    </div>
    <div class="hero-card">
      <div class="hero-card-title">
        <span>&#128200; Live Positions</span>
        <a href="/paper" style="color:#58a6ff;font-size:10px;text-decoration:none;">Paper Trade &#8594;</a>
      </div>
      <div style="margin-bottom:8px">
        <div style="font-size:10px;color:#8b949e;margin-bottom:3px;text-transform:uppercase;letter-spacing:0.5px">Paper Account</div>
        <div id="hc-paper-equity" style="font-size:20px;font-weight:700">&#8212;</div>
        <div id="hc-paper-pnl" style="font-size:11px;color:#8b949e;margin-top:2px">&#8212; Total P&amp;L</div>
        <div id="hc-paper-daily" style="font-size:11px;color:#8b949e;margin-top:1px">&#8212; Daily P&amp;L</div>
      </div>
      <div id="hc-pos-list"></div>
    </div>
    <div class="hero-card">
      <div class="hero-card-title">
        <span>&#128221; Trade Log</span>
        <span style="font-size:10px;color:#8b949e">[P] Paper &middot; [L] Live</span>
      </div>
      <div id="hc-trade-list"><span style="color:#8b949e;font-size:11px">Loading...</span></div>
    </div>
  </div>

  <!-- ── Pattern Analysis notification panel ───────────────────────── -->
  <div id="thesis-analysis-panel">
    <details>
      <summary>
        <span class="tap-title">&#128202; Pattern Analysis</span>
        <span class="tap-badge" id="tap-badge">0 signals</span>
        <span class="tap-ts"   id="tap-ts"></span>
        <span class="tap-arrow">&#9654;</span>
      </summary>
      <div class="tap-body" id="tap-body"></div>
    </details>
  </div>

  <section>
    <h2>AI Signal History <span style="font-size:11px;font-weight:400;color:#8b949e;margin-left:10px;">Chronological event log &mdash; for live per-ticker status &#8594; <a href="/signals" style="color:#58a6ff;text-decoration:none;">Signal Monitor</a></span></h2>
    <div class="model-bar">
      <span class="model-label">Threshold model:</span>
      <button class="model-btn active-0" id="mbtn-0" onclick="setModel(0)">Standard (100%)</button>
      <button class="model-btn"          id="mbtn-1" onclick="setModel(1)">Relaxed −25%</button>
      <button class="model-btn"          id="mbtn-2" onclick="setModel(2)">Relaxed −50%</button>
      <button class="model-cycle" id="cycle-btn" onclick="toggleCycle()">&#9654; Cycle</button>
      <span class="model-indicator" id="model-indicator">Conf &gt;50% · MoS &gt;15% · FUD &gt;0.60</span>
    </div>
    <div class="threshold-legend" id="threshold-legend">
      <span class="leg-title">Action thresholds:</span>
      <span class="leg-item leg-buy" id="leg-buy">BUY: MoS &gt; 15% + Confidence &gt; 50% + FUD &gt; 0.60 + go/no-go gates pass</span>
      <span class="leg-item leg-sell">SELL: signal = sell/strong_sell from aggregator</span>
      <span class="leg-item leg-hold">HOLD / WAIT: gates blocked (VIX stress, turbulence, low confidence)</span>
    </div>
    <div id="signals-table"><p class="empty">Loading...</p></div>
  </section>
  <section>
    <h2>Positions (Live)</h2>
    <div id="positions-table"><p class="empty">Loading...</p></div>
  </section>
  <section>
    <h2>Trade Log</h2>
    <div id="trades-table"><p class="empty">Loading...</p></div>
  </section>
  <section class="full-width">
    <h2>Diagnostics
      <button class="action-btn" onclick="runDiagnostics()" style="margin-left:10px">Run diagnostics</button>
      <button class="action-btn" onclick="runBackfill(365)" style="margin-left:6px">Backfill 1 year</button>
      <button class="action-btn" onclick="runBackfill(90)"  style="margin-left:6px">Backfill 90 days</button>
    </h2>
    <div id="diag-table"><p class="empty">Tap "Run diagnostics" to check data health.</p></div>
    <div id="backfill-log" style="display:none;margin-top:10px"></div>
  </section>
  <section class="full-width">
    <h2>Live Logs</h2>
    <div id="log-box"></div>
  </section>
</main>
<script>
let paused = false;
let _aiWatchTickers = [];

// ── Thesis Pattern Analysis ───────────────────────────────────────────────────
async function loadThesisAnalysis() {
  try {
    const d = await fetch('/api/thesis/analysis').then(r => r.json());
    const panel = document.getElementById('thesis-analysis-panel');
    if (!d || !panel) return;
    const fmt_ts = d.timestamp_utc
      ? new Date(d.timestamp_utc).toLocaleString('en-US', {
          timeZone: 'America/New_York', month: 'short', day: 'numeric',
          hour: '2-digit', minute: '2-digit'
        }) + ' ET'
      : '';
    document.getElementById('tap-badge').textContent =
      (d.signal_count || 0) + ' signal' + (d.signal_count === 1 ? '' : 's') +
      ' \xb7 ' + (d.ticker_count || 0) + ' tickers';
    document.getElementById('tap-ts').textContent   = fmt_ts;
    document.getElementById('tap-body').textContent = d.analysis || '';
    const titleEl = panel.querySelector('.tap-title');
    if (titleEl && d.date)
      titleEl.innerHTML = '&#128202; Pattern Analysis &mdash; ' + d.date;
    panel.classList.add('tap-visible');
  } catch(e) { /* silent — panel stays hidden if API fails */ }
}
loadThesisAnalysis();
setInterval(loadThesisAnalysis, 900000);  // re-check every 15 min (changes once/day)

// ── Morning Brief preview in header ──────────────────────────
async function fetchBriefPreview() {
  try {
    const d = await fetch('/api/morning-brief').then(r => r.json());
    if (d.error) return;

    const fmt = (asset, data, prefix='') => {
      if (!data) return null;
      const pct = data.pct || 0;
      const cls = pct > 0 ? 'up' : pct < 0 ? 'dn' : 'neu';
      const sign = pct > 0 ? '+' : '';
      return `${asset} <span class="${cls}">${prefix}${pct >= 0 ? sign : ''}${pct.toFixed(1)}%</span>`;
    };

    const parts = [];
    const idx = d.indices || {};
    const com = d.commodities || {};
    const cry = d.crypto || {};
    const rat = d.rates || {};
    const fut = d.futures || {};

    if (fut['ES (S&P)']) parts.push(fmt('ES', fut['ES (S&P)']));
    else if (idx['S&P 500']) parts.push(fmt('S&P', idx['S&P 500']));
    if (idx['VIX'])     parts.push(`VIX <span class="${(idx['VIX'].price||0)>25?'dn':(idx['VIX'].price||0)>18?'neu':'up'}">${(idx['VIX'].price||0).toFixed(1)}</span>`);
    if (com['Oil WTI']) parts.push(fmt('Oil', com['Oil WTI'], '$'));
    if (com['Gold'])    parts.push(fmt('Gold', com['Gold'], '$'));
    if (cry['Bitcoin']) parts.push(fmt('BTC', cry['Bitcoin']));
    if (rat['10yr Yield']) parts.push(`10yr <span class="neu">${(rat['10yr Yield'].price||0).toFixed(2)}%</span>`);

    const wsb = d.wsb || [];
    if (wsb.length) parts.push(`WSB: <span class="up">${wsb[0].ticker}</span>`);

    if (parts.length) {
      document.getElementById('brief-preview').innerHTML = parts.filter(Boolean).join(' &nbsp;·&nbsp; ');
    }
  } catch(e) { /* silent — preview is best-effort */ }
}

// ── Threshold models ─────────────────────────────────────────
const MODELS = [
  { name: 'Standard (100%)', conf: 0.50, mos: 0.15, fud: 0.60, cls: 0 },
  { name: 'Relaxed −25%',    conf: 0.375, mos: 0.1125, fud: 0.45, cls: 1 },
  { name: 'Relaxed −50%',    conf: 0.25,  mos: 0.075,  fud: 0.30, cls: 2 },
];
let _activeModel = 0;
let _cachedSignals = [];
let _cycleTimer = null;
let _tipranksData = {};   // ticker -> {smart_score, buy_pct, composite, ...}

async function _loadTipranksData() {
  try {
    const d = await fetch('/api/tipranks/all').then(r => r.json());
    if (d && typeof d === 'object') _tipranksData = d;
  } catch(e) { if(window._nwoErr)_nwoErr(e);else console.error('[NWO]',e); }
}

function setModel(idx) {
  _activeModel = idx;
  const m = MODELS[idx];
  // Update button styles
  MODELS.forEach((_, i) => {
    const btn = document.getElementById('mbtn-' + i);
    btn.className = 'model-btn' + (i === idx ? ' active-' + i : '');
  });
  // Update indicator and legend
  document.getElementById('model-indicator').textContent =
    `Conf >${(m.conf*100).toFixed(1)}% · MoS >${(m.mos*100).toFixed(2)}% · FUD >${m.fud.toFixed(2)}`;
  document.getElementById('leg-buy').textContent =
    `BUY: MoS > ${(m.mos*100).toFixed(2)}% + Confidence > ${(m.conf*100).toFixed(1)}% + FUD > ${m.fud.toFixed(2)} + go/no-go gates pass`;
  // Re-render cached signals with new thresholds
  if (_cachedSignals.length) renderSignals(_cachedSignals);
}

function toggleCycle() {
  const btn = document.getElementById('cycle-btn');
  if (_cycleTimer) {
    clearInterval(_cycleTimer);
    _cycleTimer = null;
    btn.textContent = '▶ Cycle';
    btn.classList.remove('cycling');
  } else {
    btn.textContent = '⏹ Stop';
    btn.classList.add('cycling');
    _cycleTimer = setInterval(() => {
      setModel((_activeModel + 1) % MODELS.length);
    }, 4000);
  }
}

async function fetchStatus() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    paused = d.paused;
    _aiWatchTickers = d.ai_watch_tickers || [];
    const btn = document.getElementById('pause-btn');
    const badge = document.getElementById('status-badge');
    const modeBadge = document.getElementById('mode-badge');
    btn.textContent = paused ? 'Resume' : 'Pause';
    btn.className = paused ? 'paused' : '';
    badge.textContent = paused ? 'PAUSED' : 'RUNNING';
    badge.className = 'badge ' + (paused ? 'badge-red' : 'badge-green');
    modeBadge.textContent = d.dry_run ? 'DRY RUN' : 'LIVE';
    modeBadge.className = 'badge ' + (d.dry_run ? 'badge-yellow' : 'badge-red');
  } catch(e) {
    const badge = document.getElementById('status-badge');
    if (badge) { badge.textContent = 'Offline'; badge.className = 'badge badge-red'; }
  }
}

function renderSignals(signals) {
  const m   = MODELS[_activeModel];
  const el  = document.getElementById('signals-table');
  if (!signals.length) { el.innerHTML = '<p class="empty">No signals yet — waiting for market hours.</p>'; return; }

  const modelTag = _activeModel > 0
    ? `<span style="font-size:10px;padding:1px 6px;border-radius:8px;margin-left:6px;background:${_activeModel===1?'#3d2b00':'#4a1519'};color:${_activeModel===1?'#d29922':'#f85149'}">${MODELS[_activeModel].name}</span>`
    : '';

  let html = `<table><tr>
    <th>Ticker</th>
    <th>Signal${modelTag}</th>
    <th title="Weighted composite score from all signals (-1 to +1). ≥+0.15 = BUY threshold.">Composite ?</th>
    <th title="Momentum score + Relative Volume. RVOL ≥1.5× green = institutional breakout volume.">Momentum / RVOL ?</th>
    <th title="Latest close price and day-over-day % change vs previous close">Price / Day</th>
    <th title="Model confidence (ensemble agreement). Need >${(m.conf*100).toFixed(1)}% under current model.">Conf ?</th>
    <th title="Margin of Safety vs intrinsic value. Need >${(m.mos*100).toFixed(2)}% under current model.">MoS ?</th>
    <th title="News quality score. Need >${m.fud.toFixed(2)} under current model.">FUD ?</th>
    <th title="Gate score under current model (${MODELS[_activeModel].name}). C=Confidence M=MoS F=FUD. Bar = composite proximity to BUY.">Score (${MODELS[_activeModel].name}) ?</th>
    <th title="Reynolds fluid dynamics regime: laminar=calm, transient=ok, turbulent=blocked">Regime ?</th>
    <th title="Quantum probability state and P(Bull). Need P(Bull)>50% to pass ensemble gate.">Quantum ?</th>
    <th title="What blocked the trade (if anything), or APPROVED if all gates passed">Blocker</th>
    <th>Time (local)</th>
    <th title="Claude AI synthesis — trade narrative + non-obvious risk (BUY signals only)">AI Thesis</th>
  </tr>`;

  // Group by ticker — one primary row per ticker, hidden sub-rows for history
  const _grp = {};
  (signals || []).forEach(s => { if (!_grp[s.ticker]) _grp[s.ticker] = []; _grp[s.ticker].push(s); });
  Object.values(_grp).forEach(g => g.sort((a, b) => new Date(b.generated_at||0) - new Date(a.generated_at||0)));
  const _sigRank = {'STRONG_BUY':0,'BUY':1,'HOLD':2,'STRONG_SELL':3,'SELL':4};
  const _tkOrd = Object.keys(_grp).sort((a, b) =>
    (_sigRank[_grp[a][0].signal]??9) - (_sigRank[_grp[b][0].signal]??9));
  for (const _tk of _tkOrd) { for (const [_si, s] of _grp[_tk].entries()) {
  const _isPrimary = _si === 0; const _hasHistory = _grp[_tk].length > 1;

    const origCls = s.signal === 'BUY' ? 'signal-buy' : s.signal === 'SELL' ? 'signal-sell' : 'signal-hold';
    const conf   = s.confidence || 0;
    const mosVal = s.margin_of_safety;
    const fudVal = s.fud_score || 0;
    const acted  = s.acted_on ? ' <span style="color:#3fb950">&#10003;</span>' : '';

    // ── Gates under active model thresholds ──────────────────
    const gateConf = conf   >= m.conf;
    const gateMos  = mosVal != null && mosVal >= m.mos;
    const gateFud  = fudVal >= m.fud;
    const gatesN   = [gateConf, gateMos, gateFud].filter(Boolean).length;

    // Would this be a BUY under this model? (all 3 quant gates + original signal not SELL)
    const wouldBuy = gatesN === 3 && s.signal !== 'SELL';
    const dispSignal = wouldBuy && _activeModel > 0 && s.signal !== 'BUY'
      ? `${s.signal} <span class="would-buy">→BUY*</span>`
      : s.signal;

    // Composite proximity toward this model's thresholds
    const confScore = Math.min(1, conf   / m.conf);
    const mosScore  = mosVal != null ? Math.min(1, Math.max(0, mosVal) / m.mos) : 0;
    const fudScore  = Math.min(1, fudVal / m.fud);
    const composite = (confScore + mosScore + fudScore) / 3;
    const compositeColor    = gatesN === 3 ? '#3fb950' : gatesN === 2 ? '#d29922' : '#f85149';
    const compositeBarColor = composite >= 0.9 ? '#3fb950' : composite >= 0.6 ? '#d29922' : '#f85149';

    // Gap tooltip against active model
    const confGap = ((conf   - m.conf) * 100).toFixed(1);
    const mosGap  = mosVal != null ? ((mosVal - m.mos) * 100).toFixed(1) : 'N/A';
    const fudGap  = (fudVal - m.fud).toFixed(2);
    const gapSign = v => parseFloat(v) >= 0 ? '+' : '';
    const gapTip  = `[${MODELS[_activeModel].name}]&#10;` +
                    `Conf: ${(conf*100).toFixed(1)}% · need ${(m.conf*100).toFixed(1)}% · gap ${gapSign(confGap)}${confGap}pp&#10;` +
                    `MoS:  ${mosVal!=null?(mosVal*100).toFixed(1):'N/A'}% · need ${(m.mos*100).toFixed(2)}% · gap ${mosGap!=='N/A'?gapSign(mosGap):''}${mosGap}pp&#10;` +
                    `FUD:  ${fudVal.toFixed(2)} · need ${m.fud.toFixed(2)} · gap ${gapSign(fudGap)}${fudGap}`;

    // Confidence bar (raw %, width relative to model threshold for context)
    const confBarPct = Math.min(100, Math.round(conf * 100));
    const barCls = gateConf ? '' : conf >= m.conf * 0.5 ? 'medium' : 'low';

    // MoS display
    const mosPct  = mosVal != null ? (mosVal * 100).toFixed(1) + '%' : '—';
    const mosCls  = mosVal == null ? '' : mosVal >= m.mos ? 'mos-good' : mosVal >= 0 ? 'mos-warn' : 'mos-bad';

    // FUD display
    const fudDisp = s.fud_score != null ? fudVal.toFixed(2) : '—';
    const fudCls  = fudVal >= m.fud ? 'mos-good' : 'mos-bad';

    // ── Impact helpers ────────────────────────────────────────
    // Each returns a small colored line showing delta to the gate threshold.
    // Green arrow = contributing positively (above threshold)
    // Red arrow   = dragging down (below threshold)
    function impactLine(actual, threshold, unit, decimals) {
      const delta = actual - threshold;
      const sign  = delta >= 0 ? '+' : '';
      const col   = delta >= 0 ? '#3fb950' : '#f85149';
      const arrow = delta >= 0 ? '▲' : '▼';
      const label = unit === 'pp' ? `${sign}${(delta*100).toFixed(decimals)}pp` : `${sign}${delta.toFixed(decimals)}`;
      return `<small style="color:${col};display:block;font-size:10px">${arrow} ${label} to gate</small>`;
    }

    // Reasoning — parse both raw JSON and pre-extracted fields from server
    let regime = '—', quantum = '—', blocker = '—';
    let compositeCell = '—', momentumCell = '—';
    try {
      const rsn = JSON.parse(s.reasoning || '{}');
      const regRaw   = rsn.reynolds_regime || '—';
      const regCls    = regRaw === 'laminar' ? 'mos-good' : regRaw === 'transient' ? 'mos-warn' : 'mos-bad';
      const regImpStr = regRaw === 'laminar'   ? '<small style="color:#3fb950;display:block;font-size:10px">▲ unlocks execution</small>'
                      : regRaw === 'transient' ? '<small style="color:#d29922;display:block;font-size:10px">~ reduces sizing</small>'
                      :                          '<small style="color:#f85149;display:block;font-size:10px">▼ blocks execution</small>';
      regime = `<span class="${regCls}">${regRaw}</span>${regImpStr}`;

      const pbullNum   = rsn.p_bull || 0;
      const pbull      = rsn.p_bull != null ? (pbullNum*100).toFixed(0)+'%' : '—';
      const pbullCls   = pbullNum >= 0.50 ? 'mos-good' : pbullNum >= 0.42 ? 'mos-warn' : 'mos-bad';
      const pbullDelta = pbullNum - 0.50;   // gate is now 50%, down from 55%
      const pbullSign  = pbullDelta >= 0 ? '+' : '';
      const pbullImpCl = pbullDelta >= 0 ? '#3fb950' : '#f85149';
      const pbullArr   = pbullDelta >= 0 ? '▲' : '▼';
      const pbullImp   = rsn.p_bull != null
        ? `<small style="color:${pbullImpCl};display:block;font-size:10px">${pbullArr} ${pbullSign}${(pbullDelta*100).toFixed(0)}pp to 50% gate</small>`
        : '';
      quantum = `<span class="${pbullCls}">${rsn.quantum_state||'—'} ${pbull}</span>${pbullImp}`;

      // Approved badge or blocker text
      const isApproved = s.approved || rsn.approved;
      blocker = isApproved
        ? `<span style="background:#00c853;color:#000;font-size:10px;font-weight:700;padding:2px 6px;border-radius:3px">✓ APPROVED</span>`
        : (rsn.blocking_reason
            ? `<span style="color:#f85149;font-size:11px">${rsn.blocking_reason.substring(0,45)}</span>`
            : `<span style="color:#3fb950;font-size:11px">—</span>`);

      // Composite score bar
      const comp     = typeof s.composite_score === 'number' ? s.composite_score : (rsn.composite_score || 0);
      const compPct  = Math.min(100, Math.max(0, ((comp + 1) / 2) * 100));
      const compCol  = comp >= 0.15 ? '#3fb950' : comp >= 0 ? '#d29922' : '#f85149';
      const compSign = comp >= 0 ? '+' : '';
      compositeCell  = `<div style="display:flex;align-items:center;gap:5px">
        <div style="width:60px;background:#21262d;border-radius:3px;height:6px">
          <div style="width:${compPct.toFixed(0)}%;background:${compCol};height:6px;border-radius:3px"></div>
        </div>
        <span style="font-size:11px;color:${compCol};font-weight:600">${compSign}${comp.toFixed(3)}</span>
      </div>`;

      // Momentum + RVOL
      const mom     = typeof s.momentum_score === 'number' ? s.momentum_score : (rsn.momentum_score || 0);
      const rvol    = typeof s.rvol === 'number' ? s.rvol : (rsn.rvol || 1.0);
      const macdDir = s.macd_direction || rsn.macd_signal_direction || 'neutral';
      const brk52w  = s.is_52w_breakout || rsn.is_52w_breakout;
      const rvolCol = rvol >= 1.5 ? '#3fb950' : rvol >= 1.0 ? '#d29922' : '#8b949e';
      const momSign = mom >= 0 ? '+' : '';
      const macdCol = macdDir === 'bullish' ? '#3fb950' : macdDir === 'bearish' ? '#f85149' : '#8b949e';
      const brkBadge = brk52w ? ' <span style="background:#e65100;color:#fff;font-size:9px;padding:1px 4px;border-radius:2px">52W↑</span>' : '';
      momentumCell  = `<span style="color:${momSign==='-'?'#f85149':'#3fb950'};font-size:11px;font-weight:600">${momSign}${mom.toFixed(2)}</span>
        <small style="color:${rvolCol};display:block;font-size:10px">RVOL ${rvol.toFixed(2)}×${brkBadge}</small>
        <small style="color:${macdCol};display:block;font-size:10px">${macdDir}</small>`;
    } catch(e) { console.warn('[NWO] signal card',e); }

    // Price — Signal Monitor shows live price (current state view)
    const _dispPrice = s.live_price != null ? s.live_price : s.current_price;
    const price  = _dispPrice != null ? '$' + Number(_dispPrice).toFixed(2) : '—';
    const chg    = s.change_pct;
    const chgStr = chg != null ? (chg >= 0 ? '+' : '') + chg.toFixed(2) + '%' : '—';
    const chgCls = chg == null ? '' : chg > 0 ? 'mos-good' : chg < 0 ? 'mos-bad' : '';
    // Price impact on confidence: large drop = bearish pressure
    const priceImpact = chg != null
      ? `<small style="color:${Math.abs(chg)>3?(chg>0?'#3fb950':'#f85149'):'#555'};display:block;font-size:10px">${Math.abs(chg)>3?(chg>0?'▲ bullish signal':'▼ bearish pressure'):'~ neutral move'}</small>`
      : '';

    // UTC → ET (handles both "2026-05-06 14:30:22 ET" and "2026-05-06T20:04:06" formats, plus null)
    const _ga = s.generated_at || '';
    const _gaClean = _ga.replace(/ ET$/, '').replace(' ', 'T');
    const utcStr   = _gaClean ? _gaClean + 'Z' : '';
    const localTime = utcStr ? new Date(utcStr).toLocaleTimeString('en-US', {timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '—';

    const isAiWatch = _aiWatchTickers.includes(s.ticker);
    const aiWatchBadge = isAiWatch ? ' <span style="background:#7c4dff;color:#fff;font-size:9px;padding:1px 5px;border-radius:2px;vertical-align:middle">AI</span>' : '';

    const _tr = _tipranksData[s.ticker] || {};
    const _ss  = _tr.smart_score;
    const _ssBg = _ss >= 8 ? '#1a4731' : _ss >= 4 ? '#3d2b00' : _ss != null ? '#4a1519' : null;
    const _ssCol = _ss >= 8 ? '#3fb950' : _ss >= 4 ? '#d29922' : _ss != null ? '#f85149' : null;
    const _ssTip = _tr.buy_pct != null ? `Smart Score ${_ss}/10 | Buy ${_tr.buy_pct.toFixed(0)}% Sell ${(_tr.sell_pct||0).toFixed(0)}%` : `Smart Score ${_ss}/10`;
    const trBadge = _ss != null
      ? ` <span style="background:${_ssBg};color:${_ssCol};border:1px solid ${_ssCol};font-size:9px;padding:1px 5px;border-radius:3px;vertical-align:middle;font-weight:700" title="${_ssTip}">&#9733;${_ss}</span>`
      : '';

    // 3 Green Arrows badge
    const _tgaN = s.tga_arrows || 0;
    const _tgaCol = _tgaN === 3 ? '#3fb950' : _tgaN === 2 ? '#d29922' : '#30363d';
    const _tgaTip = `3GA: SMA${s.tga_sma?'✓':'✗'} MACD${s.tga_macd?'✓':'✗'} Stoch${s.tga_stoch?'✓':'✗'}` + (s.tga_reason ? ` | ${s.tga_reason}` : '');
    const tgaBadge = _tgaN > 0
      ? ` <span style="color:${_tgaCol};font-size:10px;letter-spacing:-1px;vertical-align:middle" title="${_tgaTip}">${'▲'.repeat(_tgaN)}${'▽'.repeat(3-_tgaN)}</span>`
      : '';

    const _isBuySignal = s.signal === 'BUY' || s.signal === 'STRONG_BUY';
    const _aiNote = s.ai_synthesis || '';
    let _aiCell = '';
    if (_isBuySignal && _isPrimary) {
      if (_aiNote) {
        _aiCell = `<details><summary style="color:#d29922;cursor:pointer;font-size:10px">&#9672; AI Thesis</summary><div style="color:#c9d1d9;font-size:11px;padding:4px 0;max-width:280px;line-height:1.4">${_aiNote}</div></details>`;
      } else {
        _aiCell = `<span class="sig-ai-note" data-sid="${s.id}" style="color:#8b949e;font-size:10px">&#9672; loading...</span>`;
      }
    }

    html += `<tr${!_isPrimary ? ` class="sig-hist-row" data-ticker="${s.ticker}" style="display:none;opacity:0.75"` : ''}${_isBuySignal && _isPrimary ? ` data-signal-id="${s.id}"` : ''}>
      <td><strong>${s.ticker}</strong>${aiWatchBadge}${trBadge}${tgaBadge}${acted}${_isPrimary && _hasHistory ? `<span onclick="toggleSigHistory('${s.ticker}')" style="cursor:pointer;color:#8b949e;font-size:10px;margin-left:5px;user-select:none" title="${_grp[_tk].length-1} older entr${_grp[_tk].length>2?'ies':'y'}">&#9654;</span>` : ''}</td>
      <td class="${origCls}">${dispSignal}</td>
      <td style="min-width:110px">${compositeCell}</td>
      <td style="min-width:100px">${momentumCell}</td>
      <td>
        <span style="font-weight:600">${price}</span>
        <small class="${chgCls}" style="display:block">${chgStr}</small>
        ${priceImpact}
      </td>
      <td>
        <div class="conf-bar"><div class="conf-fill ${barCls}" style="width:${confBarPct}%"></div></div>
        <small style="color:#8b949e">${(conf*100).toFixed(0)}% <span style="color:#555">(>${(m.conf*100).toFixed(0)}%)</span></small>
        ${impactLine(conf, m.conf, 'pp', 1)}
      </td>
      <td class="${mosCls}">
        ${mosPct}
        ${mosVal != null ? impactLine(mosVal, m.mos, 'pp', 1) : ''}
      </td>
      <td class="${fudCls}">
        ${fudDisp}
        ${impactLine(fudVal, m.fud, 'raw', 2)}
      </td>
      <td title="${gapTip}">
        <span class="gate-pip ${gateConf?'gate-pass':'gate-fail'}" title="Conf: ${(conf*100).toFixed(0)}% vs ${(m.conf*100).toFixed(0)}% threshold">C</span>
        <span class="gate-pip ${gateMos ?'gate-pass':'gate-fail'}" title="MoS: ${mosPct} vs ${(m.mos*100).toFixed(2)}% threshold">M</span>
        <span class="gate-pip ${gateFud ?'gate-pass':'gate-fail'}" title="FUD: ${fudVal.toFixed(2)} vs ${m.fud.toFixed(2)} threshold">F</span>
        <span style="color:${compositeColor};font-weight:700;margin-left:4px">${gatesN}/3</span>
        <div class="score-bar"><div class="score-fill" style="width:${Math.round(composite*100)}%;background:${compositeBarColor}"></div></div>
      </td>
      <td>${regime}</td>
      <td>${quantum}</td>
      <td>${blocker}</td>
      <td style="color:#8b949e;font-size:12px">${localTime}</td>
      <td style="min-width:100px;max-width:300px">${_aiCell}</td>
    </tr>`;
  }} // end inner + outer ticker loop
  el.innerHTML = html + '</table>';
  // Async: load syntheses for BUY rows that don't have one yet
  _loadSyntheses();
}

function _loadSyntheses() {
  const rows = document.querySelectorAll('tr[data-signal-id]');
  const pending = [];
  rows.forEach(row => {
    const sid = row.getAttribute('data-signal-id');
    const span = row.querySelector('.sig-ai-note[data-sid="' + sid + '"]');
    if (span) pending.push(parseInt(sid, 10));
  });
  if (!pending.length) return;
  // Batch into groups of 5
  for (let i = 0; i < pending.length; i += 5) {
    const batch = pending.slice(i, i + 5);
    fetch('/api/signals/synthesize', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ids: batch})
    })
    .then(r => r.json())
    .then(data => {
      Object.entries(data).forEach(([sid, text]) => {
        if (!text) return;
        const span = document.querySelector('.sig-ai-note[data-sid="' + sid + '"]');
        if (span) {
          const det = document.createElement('details');
          det.innerHTML = '<summary style="color:#d29922;cursor:pointer;font-size:10px">&#9672; AI Thesis</summary>' +
            '<div style="color:#c9d1d9;font-size:11px;padding:4px 0;max-width:280px;line-height:1.4">' + text + '</div>';
          span.replaceWith(det);
        }
      });
    })
    .catch(() => {});
  }
}

function toggleSigHistory(ticker) {
  const rows = document.querySelectorAll('.sig-hist-row[data-ticker="' + ticker + '"]');
  rows.forEach(r => { r.style.display = r.style.display === 'none' ? '' : 'none'; });
}

function buildMainTape(signals) {
  const track = document.getElementById('main-tape');
  if (!track) return;
  // Deduplicate: keep only the latest signal row per ticker
  const byTicker = new Map();
  (signals || []).forEach(s => { if (!byTicker.has(s.ticker)) byTicker.set(s.ticker, s); });
  const items = [...byTicker.values()];
  if (!items.length) { track.innerHTML = '<span class="mt-neu">No signals yet</span>'; return; }
  const all = [...items, ...items];
  track.innerHTML = all.map(s => {
    const sig  = (s.signal || '').toUpperCase();
    const isBuy  = sig === 'BUY' || sig === 'STRONG_BUY';
    const isSell = sig === 'SELL' || sig === 'STRONG_SELL';
    const chg  = s.change_pct;          // daily price change — drives arrow direction
    const priceUp = chg != null ? chg >= 0 : null;
    // Arrow: BUY/SELL signals use bright colors; neutral signals use price change direction
    let cls, arr;
    if (isBuy)       { cls = 'mt-bull'; arr = '&#9650;'; }
    else if (isSell) { cls = 'mt-bear'; arr = '&#9660;'; }
    else if (priceUp === true)  { cls = 'mt-chg-up';   arr = '&#9650;'; }
    else if (priceUp === false) { cls = 'mt-chg-dn';   arr = '&#9660;'; }
    else                        { cls = 'mt-neu';       arr = '&#8212;'; }
    const p = s.current_price;
    const chgStr = chg != null ? ' ' + (chg >= 0 ? '+' : '') + chg.toFixed(1) + '%' : '';
    const price  = p ? ' $' + Number(p).toFixed(2) : '';
    return '<span class="' + cls + '">' + arr + ' ' + s.ticker + price + chgStr + '</span>'
         + '<span class="mt-sep">|</span>';
  }).join('');
  track.style.animationDuration = Math.max(40, items.length * 0.8) + 's';
}

async function fetchSignals() {
  try {
    const r = await fetch('/api/signals');
    _cachedSignals = await r.json();
    await _loadTipranksData();
    renderSignals(_cachedSignals);
    buildMainTape(_cachedSignals);
  } catch(e) {
    const el = document.getElementById('signals-table');
    if (el && !_cachedSignals.length) el.innerHTML = '<p class="empty">Could not reach server.</p>';
  }
}

async function fetchTrades() {
  let trades;
  try {
    const r = await fetch('/api/trades');
    trades = await r.json();
  } catch(e) {
    return;
  }
  const el = document.getElementById('trades-table');
  if (!trades.length) { el.innerHTML = '<p class="empty">No trades logged yet.</p>'; return; }
  let html = '<table><tr><th>Ticker</th><th>Action</th><th>Qty</th><th>Price</th><th>Total</th><th>Status</th></tr>';
  for (const t of trades) {
    const cls = t.action === 'BUY' ? 'signal-buy' : 'signal-sell';
    const tag = t.dry_run ? '<span class="dry-run-tag">DRY</span>' : '';
    const total = t.total ? '$' + t.total.toFixed(2) : '—';
    const price = t.price ? '$' + t.price.toFixed(2) : '—';
    html += `<tr>
      <td><strong>${t.ticker}</strong>${tag}</td>
      <td class="${cls}">${t.action}</td>
      <td>${t.quantity || '—'}</td>
      <td>${price}</td><td>${total}</td>
      <td style="color:#8b949e">${t.status || '—'}</td>
    </tr>`;
  }
  el.innerHTML = html + '</table>';
}

async function fetchLogs() {
  let d;
  try {
    const r = await fetch('/api/logs');
    d = await r.json();
  } catch(e) { return; }

  const box = document.getElementById('log-box');
  const wasAtBottom = box.scrollHeight - box.clientHeight <= box.scrollTop + 20;
  box.innerHTML = d.lines.map(line => {
    const cls = line.includes('ERROR') ? 'log-error' : line.includes('WARNING') ? 'log-warning' : 'log-info';
    return `<div class="${cls}">${line.replace(/</g,'&lt;').replace(/>/g,'&gt;')}</div>`;
  }).join('');
  if (wasAtBottom) box.scrollTop = box.scrollHeight;
}

async function togglePause() {
  await fetch('/api/pause', { method: 'POST' });
  await fetchStatus();
}

async function runDiagnostics() {
  const el = document.getElementById('diag-table');
  el.innerHTML = '<p class="empty">Running...</p>';
  const r = await fetch('/api/diagnostics');
  const rows = await r.json();
  let html = '<table><tr><th>Ticker</th><th>Status</th><th>Price Days</th><th>Latest Price</th><th>Fund. Years</th><th>Signals</th><th>Notes</th></tr>';
  for (const row of rows) {
    const cls = row.status === 'ready' ? 'status-ready' : row.status === 'no_fundamentals' ? 'status-warn' : 'status-error';
    const icon = row.status === 'ready' ? '✓' : row.status === 'no_fundamentals' ? '⚠' : '✗';
    html += `<tr>
      <td><strong>${row.ticker}</strong></td>
      <td class="${cls}">${icon} ${row.status}</td>
      <td>${row.price_days}</td>
      <td style="color:#8b949e;font-size:12px">${row.latest_price || '—'}</td>
      <td>${row.fundamental_years || '—'}</td>
      <td>${row.signal_count}</td>
      <td style="color:#8b949e;font-size:12px">${row.message}</td>
    </tr>`;
  }
  el.innerHTML = html + '</table>';
}

let _backfillPoll = null;
async function runBackfill(days) {
  const logEl = document.getElementById('backfill-log');
  logEl.style.display = 'block';
  logEl.innerHTML = 'Starting backfill...';
  await fetch('/api/backfill?days=' + days, { method: 'POST' });
  if (_backfillPoll) clearInterval(_backfillPoll);
  _backfillPoll = setInterval(async () => {
    const r = await fetch('/api/backfill/status');
    const d = await r.json();
    logEl.innerHTML = d.log.map(l => `<div>${l}</div>`).join('');
    logEl.scrollTop = logEl.scrollHeight;
    if (d.done) {
      clearInterval(_backfillPoll);
      _backfillPoll = null;
      await runDiagnostics();
    }
  }, 2000);
}

async function fetchPositions() {
  const el = document.getElementById('positions-table');
  try {
    const resp = await fetch('/api/positions');
    if (!resp.ok) { el.innerHTML = '<p class="empty">Schwab offline or no positions.</p>'; return; }
    const positions = await resp.json();
    if (positions.error) { el.innerHTML = `<p class="empty">${positions.error}</p>`; return; }
    if (!positions.length) { el.innerHTML = '<p class="empty">No open positions.</p>'; return; }
    let html = `<table><tr>
      <th>Ticker</th>
      <th title="Shares currently held">Shares</th>
      <th title="Average cost per share">Avg Cost</th>
      <th title="Current market value">Mkt Value</th>
      <th title="Unrealised P&amp;L (market value − cost basis)">P&amp;L</th>
    </tr>`;
    let totalValue = 0, totalPnl = 0;
    for (const p of positions) {
      const qty   = p.quantity || 0;
      const avg   = p.average_price;
      const mktV  = p.market_value;
      const cost  = avg != null ? avg * qty : null;
      const pnl   = (mktV != null && cost != null) ? mktV - cost : null;
      const pnlCls = pnl == null ? '' : pnl >= 0 ? 'mos-good' : 'mos-bad';
      const pnlStr = pnl != null ? (pnl >= 0 ? '+' : '') + '$' + pnl.toFixed(2) : '—';
      totalValue += mktV || 0;
      totalPnl   += pnl  || 0;
      html += `<tr>
        <td><strong>${p.ticker}</strong></td>
        <td>${qty}</td>
        <td>${avg != null ? '$' + avg.toFixed(2) : '—'}</td>
        <td>${mktV != null ? '$' + mktV.toFixed(2) : '—'}</td>
        <td class="${pnlCls}">${pnlStr}</td>
      </tr>`;
    }
    // Totals row
    const totPnlCls = totalPnl >= 0 ? 'mos-good' : 'mos-bad';
    html += `<tr style="border-top:1px solid #30363d;font-weight:600">
      <td colspan="3" style="color:#8b949e">Total (${positions.length} positions)</td>
      <td>$${totalValue.toFixed(2)}</td>
      <td class="${totPnlCls}">${totalPnl >= 0 ? '+' : ''}$${totalPnl.toFixed(2)}</td>
    </tr>`;
    el.innerHTML = html + '</table>';
  } catch(e) {
    el.innerHTML = '<p class="empty">Could not load positions.</p>';
  }
}

async function refresh() {
  await Promise.allSettled([fetchStatus(), fetchSignals(), fetchTrades(), fetchLogs(), fetchPositions()]);
  document.getElementById('refresh-ts').textContent = 'Updated ' + new Date().toLocaleTimeString('en-US', {timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',second:'2-digit'}) + ' ET';
}

async function fetchIToolPreview() {
  try {
    const d = await fetch('/api/itool').then(r => r.json());
    if (d.counts && (d.counts.bullish || d.counts.bearish)) {
      const el = document.getElementById('itool-preview');
      if (el) el.innerHTML =
        `<span class="up">&#9650;${d.counts.bullish||0} Bullish</span> &nbsp;&#xB7;&nbsp; <span class="dn">&#9660;${d.counts.bearish||0} Bearish</span>`;
    }
  } catch(e) { if(window._nwoErr)_nwoErr(e);else console.error('[NWO]',e); }
}

async function fetchPaperPreview() {
  try {
    const d = await fetch('/api/paper/account').then(r => r.json());
    const el = document.getElementById('paper-preview');
    if (!el || d.error) return;
    const pnl = d.total_pnl || 0;
    const cls = pnl >= 0 ? 'up' : 'dn';
    const sign = pnl >= 0 ? '+' : '';
    el.innerHTML = `$${(d.total_equity||0).toLocaleString('en-US',{maximumFractionDigits:0})} &nbsp;·&nbsp; <span class="${cls}">${sign}$${Math.abs(pnl).toLocaleString('en-US',{maximumFractionDigits:0})} P&amp;L</span>`;
  } catch(e) { if(window._nwoErr)_nwoErr(e);else console.error('[NWO]',e); }
}

// Brief, I-Tool, and Paper previews load once on startup (fast, uses cache)
fetchBriefPreview();
fetchIToolPreview();
fetchPaperPreview();

// ── Hero card loaders ─────────────────────────────────────────────────────────
async function loadHeroBrief() {
  try {
    const d = await fetch('/api/morning-brief').then(r => r.json());
    if (d.error) { document.getElementById('hc-brief-text').textContent = 'Brief not yet available.'; return; }
    const idx = d.indices || {}, fut = d.futures || {}, com = d.commodities || {},
          cry = d.crypto  || {}, rat = d.rates   || {};
    const fmtPct = (v) => v == null ? null : (v >= 0 ? '+' : '') + v.toFixed(1) + '%';
    const fmtPrice = (v) => v == null ? null : v.toFixed(2);
    const items = [
      { label: 'S&P Fut',  val: fmtPct(fut['ES (S&P)']?.pct),           pct: fut['ES (S&P)']?.pct },
      { label: 'VIX',      val: fmtPrice(idx['VIX']?.price),             pct: -(idx['VIX']?.price - 20) },
      { label: 'Oil',      val: fmtPct(com['Oil WTI']?.pct),             pct: com['Oil WTI']?.pct },
      { label: 'Gold',     val: fmtPct(com['Gold']?.pct),                pct: com['Gold']?.pct },
      { label: 'BTC',      val: fmtPct(cry['Bitcoin']?.pct),             pct: cry['Bitcoin']?.pct },
      { label: '10yr',     val: fmtPrice(rat['10yr Yield']?.price) ? fmtPrice(rat['10yr Yield']?.price) + '%' : null, pct: 0 },
      { label: 'Nasdaq',   val: fmtPct(idx['Nasdaq']?.pct),              pct: idx['Nasdaq']?.pct },
      { label: 'Dow',      val: fmtPct(idx['Dow Jones']?.pct),           pct: idx['Dow Jones']?.pct },
    ].filter(i => i.val != null);
    const marketsEl = document.getElementById('hc-brief-markets');
    marketsEl.innerHTML = items.map(i => {
      const col = i.pct == null ? '#8b949e' : i.pct > 0 ? '#3fb950' : i.pct < 0 ? '#f85149' : '#8b949e';
      return `<span style="display:flex;justify-content:space-between;gap:4px"><span style="color:#8b949e">${i.label}</span><span style="color:${col};font-weight:600">${i.val}</span></span>`;
    }).join('');
    const txt = d.narrative || d.summary || '';
    document.getElementById('hc-brief-text').textContent = txt.slice(0, 380) + (txt.length > 380 ? '…' : '');
    const wsb = (d.wsb || []).slice(0, 4).map(t => typeof t === 'string' ? t : t.ticker || t).filter(Boolean);
    const wsbEl = document.getElementById('hc-brief-wsb');
    if (wsb.length) wsbEl.innerHTML = '&#x1F4AC; WSB: ' + wsb.map(t => `<span style="color:#d29922;font-weight:700">${t}</span>`).join(' &middot; ');
  } catch(e) { document.getElementById('hc-brief-text').textContent = 'Brief unavailable.'; }
}

async function loadHeroPositions() {
  try {
    const acct = await fetch('/api/paper/account').then(r => r.json());
    const eq   = acct.total_equity || 0;
    const pnl  = acct.total_pnl    || 0, pct  = acct.total_pnl_pct    || 0;  // return on invested
    const lpnl = acct.lifetime_pnl || 0, lpct = acct.lifetime_pnl_pct || 0;  // vs $100k
    const dpnl = acct.daily_pnl    || 0, dpct = acct.daily_pnl_pct    || 0;
    document.getElementById('hc-paper-equity').textContent =
      '$' + eq.toLocaleString('en-US', {minimumFractionDigits:0, maximumFractionDigits:0});
    const pnlEl = document.getElementById('hc-paper-pnl');
    pnlEl.textContent = (pnl >= 0 ? '+' : '') + '$' + Math.abs(pnl).toFixed(0) + ' Return (' + pct.toFixed(1) + '%)';
    pnlEl.style.color = pnl >= 0 ? '#3fb950' : '#f85149';
    const dCardEl = document.getElementById('hc-paper-daily');
    if (dCardEl) {
      dCardEl.textContent = (dpnl >= 0 ? '+' : '') + '$' + Math.abs(dpnl).toFixed(0) + ' Daily (' + dpct.toFixed(1) + '%)';
      dCardEl.style.color = dpnl >= 0 ? '#3fb950' : '#f85149';
    }
    // Header P&L badges — show return-on-invested for Total, daily for Daily
    const fmtBadge = (v, p, label) => {
      const col = v >= 0 ? '#3fb950' : '#f85149';
      const sign = v >= 0 ? '+' : '';
      return `${label}: <span style="color:${col};font-weight:700">${sign}$${Math.abs(v).toFixed(0)} (${sign}${p.toFixed(1)}%)</span>`;
    };
    const dEl = document.getElementById('hdr-daily-pnl');
    const tEl = document.getElementById('hdr-total-pnl');
    if (dEl) { dEl.innerHTML = fmtBadge(dpnl, dpct, 'Daily');  dEl.style.borderColor = dpnl >= 0 ? '#1a4731' : '#4a1519'; }
    if (tEl) { tEl.innerHTML = fmtBadge(pnl,  pct,  'Return'); tEl.style.borderColor = pnl  >= 0 ? '#1a4731' : '#4a1519'; }
    const pos = (acct.positions || []).slice(0, 4);
    const el = document.getElementById('hc-pos-list');
    if (!pos.length) { el.innerHTML = '<span style="color:#8b949e;font-size:11px">No open positions</span>'; return; }
    el.innerHTML = pos.map(p => {
      const pc = p.pnl_pct || 0;
      return '<div class="hero-pos-row"><span style="font-weight:700">' + p.ticker + '</span>'
           + '<span style="color:' + (pc >= 0 ? '#3fb950' : '#f85149') + '">'
           + (pc >= 0 ? '+' : '') + pc.toFixed(1) + '%</span></div>';
    }).join('');
  } catch(e) { if(window._nwoErr)_nwoErr(e);else console.error('[NWO]',e); }
}

async function loadHeroTrades() {
  try {
    const [live, paper] = await Promise.all([
      fetch('/api/trades').then(r => r.json()).catch(() => []),
      fetch('/api/paper/trades').then(r => r.json()).catch(() => []),
    ]);
    const merged = [
      ...(paper || []).slice(0, 5).map(t => ({...t, _src: 'P'})),
      ...(live  || []).slice(0, 3).map(t => ({...t, _src: 'L'})),
    ].sort((a, b) => (b.timestamp || '').localeCompare(a.timestamp || '')).slice(0, 6);
    const el = document.getElementById('hc-trade-list');
    if (!merged.length) { el.innerHTML = '<span style="color:#8b949e;font-size:11px">No trades yet</span>'; return; }
    el.innerHTML = merged.map(t => {
      const dt = t.timestamp
        ? new Date(t.timestamp + 'Z').toLocaleDateString('en-US', {timeZone:'America/New_York',month:'2-digit',day:'2-digit'})
          + ' ' + new Date(t.timestamp + 'Z').toLocaleTimeString('en-US', {timeZone:'America/New_York',hour:'2-digit',minute:'2-digit'})
        : '\u2014';
      return '<div class="hero-trade-row">'
           + '<span style="color:#8b949e;font-size:10px;white-space:nowrap">' + dt + '</span>'
           + '<span style="font-weight:700">' + t.ticker + '</span>'
           + '<span style="color:' + (t.action === 'BUY' ? '#3fb950' : '#f85149') + '">' + t.action + '</span>'
           + '<span style="color:#8b949e;margin-left:auto">[' + t._src + ']</span>'
           + '</div>';
    }).join('');
  } catch(e) { if(window._nwoErr)_nwoErr(e);else console.error('[NWO]',e); }
}

loadHeroBrief();
loadHeroPositions();
loadHeroTrades();
setInterval(() => { loadHeroPositions(); loadHeroTrades(); }, 30000);

refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD_HTML


# ── Read-only embed view (Google Sites / public) ──────────────────────────────

MORNING_BRIEF_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NWO Morning Brief</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0d1117; color: #e6edf3; font-size: 13px; line-height: 1.6; }
  header { background: #161b22; border-bottom: 1px solid #30363d; padding: 10px 20px;
           display: flex; align-items: center; gap: 10px; position: sticky; top: 0; z-index: 10;
           flex-wrap: wrap; }
  header h1 { font-size: 16px; font-weight: 700; }
  .back-btn { padding: 4px 12px; border-radius: 5px; border: 1px solid #30363d; cursor: pointer;
              font-size: 12px; background: #21262d; color: #e6edf3; text-decoration: none; flex-shrink: 0; }
  .back-btn:hover { background: #30363d; }
  .refresh-btn { padding: 4px 12px; border-radius: 5px; border: 1px solid #1f6feb; cursor: pointer;
                 font-size: 12px; background: #1c2e50; color: #58a6ff; flex-shrink: 0; }
  .refresh-btn:hover { background: #2d4a80; }
  .gen-time { font-size: 11px; color: #8b949e; flex-shrink: 0; }
  /* Heartbeat */
  #hb-status { display: flex; align-items: center; gap: 5px; font-size: 11px;
               color: #8b949e; flex-shrink: 0; }
  #hb-dot { font-size: 14px; line-height: 1; transition: color 0.4s; }
  #hb-dot.ok  { color: #3fb950; }
  #hb-dot.err { color: #f85149; }
  #hb-dot.warn { color: #d29922; }
  main { max-width: 1200px; margin: 0 auto; padding: 20px; display: grid;
         grid-template-columns: 2fr 1fr; gap: 16px; }
  .full { grid-column: 1 / -1; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }
  h2 { font-size: 12px; color: #adb5bd; text-transform: uppercase; letter-spacing: 1px;
       border-bottom: 1px solid #30363d; padding-bottom: 6px; margin-bottom: 10px; font-weight: 700; }
  /* Narrative HTML from Claude */
  #narrative h3 { font-size: 14px; color: #e6edf3; margin: 16px 0 8px; font-weight: 700; }
  #narrative h3:first-child { margin-top: 0; }
  #narrative ul { padding-left: 18px; margin: 6px 0; }
  #narrative li { margin-bottom: 4px; color: #c9d1d9; }
  #narrative p  { color: #c9d1d9; margin-bottom: 8px; }
  #narrative strong { color: #e6edf3; }
  #narrative table { width: 100%; border-collapse: collapse; margin: 8px 0; font-size: 12px; }
  #narrative th { color: #adb5bd; text-align: left; padding: 5px 8px;
                  border-bottom: 2px solid #30363d; background: #1c2128; font-weight: 600; }
  #narrative td { padding: 5px 8px; border-top: 1px solid #21262d; color: #c9d1d9; }
  #narrative tr:nth-child(even) td { background: #161b22; }
  #narrative tr:nth-child(odd)  td { background: #0d1117; }
  #narrative tr:hover td { background: #1f2937; }
  .up   { color: #3fb950; font-weight: 600; }
  .down { color: #f85149; font-weight: 600; }
  /* Data tables (right-panel structured data) */
  table.data { width: 100%; border-collapse: collapse; font-size: 12px; }
  table.data th { color: #adb5bd; text-align: left; padding: 4px 6px;
                  font-weight: 600; border-bottom: 2px solid #30363d;
                  background: #1c2128; font-size: 11px; }
  table.data td { padding: 5px 6px; border-top: 1px solid #21262d; color: #c9d1d9; }
  table.data tr:nth-child(even) td { background: #161b22; }
  table.data tr:hover td { background: #1f2937; }
  .pos { color: #3fb950; } .neg { color: #f85149; } .neu { color: #8b949e; }
  /* WSB pips */
  .wsb-row { display: flex; align-items: center; gap: 6px; padding: 4px 0;
             border-top: 1px solid #161b22; }
  .wsb-rank { color: #8b949e; font-size: 11px; width: 18px; }
  .wsb-ticker { font-weight: 700; font-size: 13px; min-width: 50px; }
  .wsb-bar-wrap { flex: 1; height: 6px; background: #21262d; border-radius: 3px; }
  .wsb-bar { height: 6px; background: #58a6ff; border-radius: 3px; }
  .wsb-mentions { font-size: 11px; color: #8b949e; min-width: 40px; text-align: right; }
  .wsb-delta { font-size: 10px; min-width: 36px; text-align: right; }
  /* News list */
  .news-item { padding: 6px 0; border-top: 1px solid #161b22; }
  .news-item:first-child { border-top: none; }
  .news-source { font-size: 10px; color: #58a6ff; margin-bottom: 2px; }
  .news-title { color: #e6edf3; font-size: 12px; line-height: 1.4; }
  .news-title a { color: #e6edf3; text-decoration: none; }
  .news-title a:hover { color: #58a6ff; }
  /* Congress trades */
  .ct-buy  { color: #3fb950; font-size: 10px; font-weight: 700; }
  .ct-sell { color: #f85149; font-size: 10px; font-weight: 700; }
  .loading { color: #8b949e; font-style: italic; padding: 20px; text-align: center; }
  .error   { color: #f85149; padding: 12px; background: #1a0a0a; border-radius: 6px; }
  #spinner { display: none; color: #8b949e; font-size: 12px; }
  /* Stage Gate add controls */
  .mb-add-wrap { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
  .mb-search { padding: 4px 8px; border-radius: 5px; border: 1px solid #30363d;
               background: #0d1117; color: #e6edf3; font-size: 12px; width: 90px;
               text-transform: uppercase; }
  .mb-search:focus { outline: none; border-color: #58a6ff; }
  .mb-add-s1, .mb-add-s2 { padding: 4px 10px; border-radius: 5px; cursor: pointer;
                             font-size: 12px; font-weight: 600; border: 1px solid; }
  .mb-add-s1 { background: #1c2e50; color: #58a6ff; border-color: #1f6feb; }
  .mb-add-s1:hover { background: #2d4a80; }
  .mb-add-s2 { background: #1a2d1a; color: #3fb950; border-color: #238636; }
  .mb-add-s2:hover { background: #2a4a2a; }
  .mb-mini { padding: 2px 6px; border-radius: 4px; cursor: pointer; font-size: 10px;
             font-weight: 700; border: 1px solid #1f6feb; background: #1c2e50;
             color: #58a6ff; margin-left: 4px; vertical-align: middle; }
  .mb-mini:hover { background: #2d4a80; }
  .mb-toast { position: fixed; bottom: 20px; right: 20px; padding: 8px 16px;
              border-radius: 6px; font-size: 12px; font-weight: 600; z-index: 999;
              opacity: 0; transition: opacity 0.3s; pointer-events: none; }
  .mb-toast.show { opacity: 1; }
  .mb-toast.ok  { background: #1a3a1a; color: #3fb950; border: 1px solid #238636; }
  .mb-toast.err { background: #3a1a1a; color: #f85149; border: 1px solid #da3633; }
  /* ── Ticker tape (morning brief) ──────────────────────────── */
  .mb-tape-wrap  { overflow: hidden; background: #0a0f17;
                   border-bottom: 1px solid #1f6feb; height: 26px; flex-shrink: 0; }
  .mb-tape-track { display: flex; gap: 24px; white-space: nowrap; will-change: transform;
                   animation: mb-tape 80s linear infinite; align-items: center; height: 100%;
                   padding-left: 12px; }
  .mb-tape-track:hover { animation-play-state: paused; }
  @keyframes mb-tape { 0%{transform:translateX(0)} 100%{transform:translateX(-50%)} }
  .mbt-bull { color: #3fb950; font-size: 12px; font-weight: 700; }
  .mbt-bear { color: #f85149; font-size: 12px; font-weight: 700; }
  .mbt-neu  { color: #8b949e; font-size: 12px; }
  .mbt-sep  { color: #30363d; font-size: 10px; }
  /* ── Brief info modal ─────────────────────────────────────── */
  .mb-info-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.8);
                     display: none; align-items: center; justify-content: center; z-index: 9999; }
  .mb-info-overlay.open { display: flex; }
  .mb-info-modal { background: #161b22; border: 1px solid #30363d; border-radius: 10px;
                   padding: 24px; max-width: 520px; width: 92%; max-height: 88vh; overflow-y: auto; }
  .mb-info-modal h2 { font-size: 15px; color: #58a6ff; margin-bottom: 14px; }
  .mb-info-row { display: flex; gap: 10px; margin-bottom: 10px; align-items: flex-start; }
  .mb-info-tag { font-size: 10px; font-weight: 700; color: #e6edf3; min-width: 110px;
                 background: #21262d; padding: 2px 7px; border-radius: 4px; flex-shrink: 0; }
  .mb-info-desc { font-size: 12px; color: #8b949e; line-height: 1.5; }
  .mb-info-close { float: right; background: none; border: 1px solid #30363d;
                   color: #8b949e; cursor: pointer; padding: 4px 10px; border-radius: 4px; font-size: 12px; }
  .mb-info-close:hover { color: #f85149; }
</style>
</head>
<body>
<!-- Brief info modal -->
<div class="mb-info-overlay" id="mb-info-overlay" onclick="if(event.target===this)document.getElementById('mb-info-overlay').classList.remove('open')">
  <div class="mb-info-modal">
    <button class="mb-info-close" onclick="document.getElementById('mb-info-overlay').classList.remove('open')">&#x2715; Close</button>
    <h2>&#9432; About the Morning Brief</h2>
    <div class="mb-info-row"><span class="mb-info-tag">AI Narrator</span><span class="mb-info-desc">Market narrative generated by Google Gemini Flash (primary) with Claude Haiku as fallback. Synthesizes macro data, futures, indices, rates, and watchlist fundamentals.</span></div>
    <div class="mb-info-row"><span class="mb-info-tag">Data sources</span><span class="mb-info-desc">Pre-market futures + indices via yfinance · News headlines via RSS feeds · WSB trending via Reddit API · Congressional trades via house.gov / senate.gov disclosures.</span></div>
    <div class="mb-info-row"><span class="mb-info-tag">Refresh schedule</span><span class="mb-info-desc">Auto-generated at 6:00am and 9:00am ET Monday–Friday. Click "Regenerate" to force a fresh brief at any time.</span></div>
    <div class="mb-info-row"><span class="mb-info-tag">Ticker tape</span><span class="mb-info-desc">Shows current BUY/SELL signals from the NWO pipeline with latest DB close prices.</span></div>
    <div class="mb-info-row"><span class="mb-info-tag">+S1 / +S2 buttons</span><span class="mb-info-desc">Add a ticker to Stage 1 (monitoring only) or Stage 2 (active AI pipeline + paper trading) in the Stage Gate. Use on WSB trending tickers or any stock in the headlines.</span></div>
    <div class="mb-info-row"><span class="mb-info-tag">Top Headlines</span><span class="mb-info-desc">Financial news headlines aggregated from major sources. Click any headline to open full article. Shown at top for quick daily scanning.</span></div>
  </div>
</div>
<header>
  <a class="back-btn" href="/">← Dashboard</a>
  <h1>📊 Morning Market Brief</h1>
  <span class="gen-time" id="gen-time">Loading...</span>
  <span id="hb-status"><span id="hb-dot" class="warn">●</span><span id="hb-label">Connecting...</span></span>
  <div style="flex:1"></div>
  <div class="mb-add-wrap">
    <input class="mb-search" id="mb-ticker-input" placeholder="TICKER" maxlength="8"
           onkeydown="if(event.key==='Enter')mbAddToStage(this.value,'1')" title="Type a ticker and press Enter or click +S1/+S2 to add to Stage Gate">
    <button class="mb-add-s1" onclick="mbAddToStage(document.getElementById('mb-ticker-input').value,'1')" title="Add to Stage 1 (Monitoring)">+S1</button>
    <button class="mb-add-s2" onclick="mbAddToStage(document.getElementById('mb-ticker-input').value,'2')" title="Add to Stage 2 (Active AI)">+S2</button>
  </div>
  <button style="padding:5px 10px;border-radius:6px;border:1px solid #30363d;background:transparent;color:#58a6ff;cursor:pointer;font-size:12px;white-space:nowrap;" onclick="document.getElementById('mb-info-overlay').classList.add('open')" title="About this brief">&#9432; About</button>
  <button class="refresh-btn" id="refresh-btn" onclick="loadBrief(true)">&#8635; Regenerate</button>
</header>
<div class="mb-tape-wrap"><div class="mb-tape-track" id="mb-tape"><span class="mbt-neu">Loading signals...</span></div></div>

<main id="main-grid" style="display:none">
  <!-- Headlines first — quick scan at top -->
  <div class="card full">
    <h2>Top Headlines</h2>
    <div id="headlines-panel" style="columns:2;column-gap:20px"></div>
  </div>

  <!-- Left: AI narrative (full width if no API key, else 2/3) -->
  <div class="card" id="narrative-card">
    <h2>Market Narrative <span id="spinner">⟳ Generating...</span></h2>
    <div id="narrative"><p class="loading">Loading brief...</p></div>
  </div>

  <!-- Right column: raw data panels -->
  <div id="right-col">
    <div class="card" style="margin-bottom:16px">
      <h2>Pre-Market Futures</h2>
      <div id="futures-table"></div>
    </div>
    <div class="card" style="margin-bottom:16px">
      <h2>US Indices (prior close)</h2>
      <div id="indices-table"></div>
    </div>
    <div class="card" style="margin-bottom:16px">
      <h2>Global Markets</h2>
      <div id="global-table"></div>
    </div>
    <div class="card" style="margin-bottom:16px">
      <h2>Rates &amp; FX</h2>
      <div id="rates-table"></div>
    </div>
  </div>

  <!-- Commodities + Crypto side by side -->
  <div class="card">
    <h2>Commodities</h2>
    <div id="commodities-table"></div>
  </div>
  <div class="card">
    <h2>Crypto</h2>
    <div id="crypto-table"></div>
  </div>

  <!-- WSB + Congress side by side -->
  <div class="card">
    <h2>WSB Trending</h2>
    <div id="wsb-panel"></div>
  </div>
  <div class="card">
    <h2>Capitol Hill Trades</h2>
    <div id="congress-panel"></div>
  </div>
</main>
<div id="loading-screen" style="padding:40px;text-align:center;color:#8b949e">
  ⟳ Fetching market data and generating brief...
</div>

<script>
function priceRow(name, d) {
  if (!d) return '';
  const pct = d.pct || 0;
  const cls = pct > 0 ? 'pos' : pct < 0 ? 'neg' : 'neu';
  const sign = pct > 0 ? '+' : '';
  const price = d.price > 1000 ? d.price.toLocaleString('en-US',{maximumFractionDigits:2})
                                : d.price.toFixed(2);
  return `<tr><td>${name}</td><td>${price}</td>
    <td class="${cls}">${sign}${pct.toFixed(2)}%</td></tr>`;
}

function buildTable(data) {
  if (!data || !Object.keys(data).length) return '<p class="neu" style="font-size:11px">No data</p>';
  return `<table class="data"><tr><th>Asset</th><th>Price</th><th>Change</th></tr>` +
    Object.entries(data).map(([k,v]) => priceRow(k, v)).join('') + '</table>';
}

function buildWSB(items) {
  if (!items || !items.length) return '<p class="neu" style="font-size:11px">No data</p>';
  const maxM = Math.max(...items.map(t => t.mentions));
  return items.map(t => {
    const barW = maxM > 0 ? Math.round(t.mentions / maxM * 100) : 0;
    const delta = t.mentions - (t.mentions_24h_ago || 0);
    const dCls = delta > 0 ? 'pos' : delta < 0 ? 'neg' : 'neu';
    const dStr = delta > 0 ? '+'+delta : delta;
    return `<div class="wsb-row">
      <span class="wsb-rank">#${t.rank}</span>
      <span class="wsb-ticker">${t.ticker}<button class="mb-mini" onclick="mbAddToStage('${t.ticker}','1')">+S1</button></span>
      <div class="wsb-bar-wrap"><div class="wsb-bar" style="width:${barW}%"></div></div>
      <span class="wsb-mentions">${t.mentions}</span>
      <span class="wsb-delta ${dCls}">${dStr}</span>
    </div>`;
  }).join('');
}

function buildCongress(trades) {
  if (!trades || !trades.length) return '<p class="neu" style="font-size:11px">No recent disclosures</p>';
  return `<table class="data"><tr><th>Who</th><th>Ticker</th><th>Action</th><th>Amount</th><th>Date</th></tr>` +
    trades.map(t => {
      const isBuy = (t.action||'').toLowerCase().includes('purchase');
      const cls = isBuy ? 'ct-buy' : 'ct-sell';
      return `<tr>
        <td><span style="font-size:10px">${t.politician||'—'} <span style="color:#8b949e">(${t.party||'?'})</span></span></td>
        <td><strong>${t.ticker||'—'}</strong>${t.ticker ? `<button class="mb-mini" onclick="mbAddToStage('${t.ticker}','1')">+S1</button>` : ''}</td>
        <td class="${cls}">${t.action||'—'}</td>
        <td style="font-size:11px;color:#8b949e">${t.amount||'—'}</td>
        <td style="font-size:11px;color:#8b949e">${t.date||'—'}</td>
      </tr>`;
    }).join('') + '</table>';
}

function buildHeadlines(headlines) {
  if (!headlines || !headlines.length) return '<p class="neu">No headlines fetched.</p>';
  return headlines.slice(0, 18).map(h =>
    `<div class="news-item">
      <div class="news-source">${h.source}</div>
      <div class="news-title"><a href="${h.link}" target="_blank">${h.title}</a></div>
    </div>`
  ).join('');
}

let _pollTimer = null;
let _lastGenTime = null;

function svgSparkline(vals, w, h) {
  w = w || 80; h = h || 28;
  if (!vals || vals.length < 2) return '';
  var mn = Math.min.apply(null, vals), mx = Math.max.apply(null, vals);
  var rng = mx - mn || 1;
  var pts = vals.map(function(v, i) {
    return (i * (w / (vals.length - 1))).toFixed(1) + ',' + (h - (v - mn) / rng * (h - 2) - 1).toFixed(1);
  }).join(' ');
  var col = vals[vals.length - 1] >= vals[0] ? '#3fb950' : '#f85149';
  return '<svg width="' + w + '" height="' + h + '" style="vertical-align:middle;margin-left:8px;flex-shrink:0">'
       + '<polyline points="' + pts + '" fill="none" stroke="' + col + '" stroke-width="1.5" stroke-linejoin="round"/>'
       + '</svg>';
}

function buildIndicesWithSparklines(indices, sparklines) {
  if (!indices || !Object.keys(indices).length) return '<p class="neu">No index data.</p>';
  var sp = sparklines || {};
  var rows = Object.entries(indices).map(function(e) {
    var name = e[0], v = e[1];
    if (!v) return '';
    var pct = v.pct || 0, cls = pct > 0 ? 'pos' : pct < 0 ? 'neg' : 'neu';
    var sign = pct > 0 ? '+' : '';
    var price = v.price > 1000 ? v.price.toLocaleString('en-US', {maximumFractionDigits:2}) : v.price.toFixed(2);
    var spark = svgSparkline(sp[name]);
    return '<tr>'
      + '<td style="white-space:nowrap">' + name + '</td>'
      + '<td>' + price + '</td>'
      + '<td class="' + cls + '">' + sign + pct.toFixed(2) + '%</td>'
      + '<td>' + spark + '</td>'
      + '</tr>';
  }).join('');
  return '<table>' + rows + '</table>';
}

function renderBrief(d) {
  const gt = d.generated_at ? new Date(d.generated_at + 'Z').toLocaleString('en-US', {timeZone:'America/New_York',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}) + ' ET' : '—';
  document.getElementById('gen-time').textContent = 'Generated: ' + gt;

  document.getElementById('futures-table').innerHTML     = buildTable(d.futures);
  document.getElementById('indices-table').innerHTML     = buildIndicesWithSparklines(d.indices, d.indices_sparklines);
  document.getElementById('global-table').innerHTML      = buildTable(d.global_markets);
  document.getElementById('rates-table').innerHTML       = buildTable(d.rates);
  document.getElementById('commodities-table').innerHTML = buildTable(d.commodities);
  document.getElementById('crypto-table').innerHTML      = buildTable(d.crypto);
  document.getElementById('wsb-panel').innerHTML         = buildWSB(d.wsb);
  document.getElementById('congress-panel').innerHTML    = buildCongress(d.congress_trades);
  document.getElementById('headlines-panel').innerHTML   = buildHeadlines(d.headlines);

  if (d.narrative_html) {
    document.getElementById('narrative').innerHTML = d.narrative_html;
    document.getElementById('narrative-card').style.display = '';
  } else {
    document.getElementById('narrative-card').style.display = 'none';
  }

  document.getElementById('loading-screen').style.display = 'none';
  document.getElementById('main-grid').style.display = 'grid';
  document.getElementById('spinner').style.display = 'none';
  document.getElementById('refresh-btn').disabled = false;
  document.getElementById('refresh-btn').textContent = '\u21BB Regenerate';
}

function setGenerating(dots) {
  const d = '.'.repeat((dots % 3) + 1);
  document.getElementById('loading-screen').style.display = 'block';
  document.getElementById('main-grid').style.display = 'none';
  document.getElementById('loading-screen').innerHTML =
    `<div style="color:#58a6ff;font-size:14px">
       Generating brief with Gemini AI${d}<br>
       <small style="color:#8b949e">Usually takes 10-20 seconds — page will update automatically</small>
     </div>`;
}

async function pollForBrief(attempt) {
  try {
    const d = await fetch('/api/morning-brief').then(r => r.json());
    if (d.error) {
      document.getElementById('loading-screen').innerHTML = `<div class="error">${d.error}</div>`;
      return;
    }
    // Still generating — keep polling
    if (d._generating || !d.generated_at || d.generated_at === _lastGenTime) {
      setGenerating(attempt);
      _pollTimer = setTimeout(() => pollForBrief(attempt + 1), 2000);
      return;
    }
    // New data ready
    _lastGenTime = d.generated_at;
    renderBrief(d);
  } catch(e) {
    document.getElementById('loading-screen').innerHTML =
      `<div class="error">Connection error: ${e.message}<br><button onclick="loadBrief()" style="margin-top:8px;padding:6px 14px;background:#21262d;border:1px solid #30363d;color:#e6edf3;border-radius:5px;cursor:pointer">Retry</button></div>`;
  }
}

async function loadBrief(forceRefresh = false) {
  if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }
  document.getElementById('refresh-btn').disabled = true;
  document.getElementById('refresh-btn').textContent = 'Generating...';

  try {
    if (forceRefresh) {
      // Fire background job, then start polling
      _lastGenTime = _lastGenTime || null;
      await fetch('/api/morning-brief/refresh', { method: 'POST' });
      setGenerating(0);
      _pollTimer = setTimeout(() => pollForBrief(1), 2000);
    } else {
      // Just load cached — instant
      const d = await fetch('/api/morning-brief').then(r => r.json());
      if (d.error) {
        document.getElementById('loading-screen').innerHTML = `<div class="error">${d.error}</div>`;
        return;
      }
      if (d._generating || !d.generated_at) {
        // Server already generating (e.g. 7am job) — poll for it
        setGenerating(0);
        _pollTimer = setTimeout(() => pollForBrief(1), 2000);
        return;
      }
      _lastGenTime = d.generated_at;
      renderBrief(d);
    }
  } catch(e) {
    document.getElementById('loading-screen').innerHTML =
      `<div class="error">Failed to connect: ${e.message}<br><button onclick="loadBrief()" style="margin-top:8px;padding:6px 14px;background:#21262d;border:1px solid #30363d;color:#e6edf3;border-radius:5px;cursor:pointer">Retry</button></div>`;
  }
}

// ── Heartbeat ──────────────────────────────────────────────────────────────
let _hbOk = false;
function _nextBriefTime() {
  const now = new Date();
  // Brief auto-regenerates at 06:00 and 09:00 ET
  const etNow  = new Date(now.toLocaleString('en-US', {timeZone: 'America/New_York'}));
  const etMins = etNow.getHours() * 60 + etNow.getMinutes();
  const slots   = [6 * 60, 9 * 60];
  let next = null;
  for (const s of slots) { if (etMins < s) { next = s; break; } }
  if (next === null) { next = slots[0] + 24 * 60; }
  const diffMin = next - etMins;
  if (diffMin < 60) return `Next brief in ~${diffMin}m`;
  const h = Math.floor(diffMin / 60), m = diffMin % 60;
  return `Next brief in ~${h}h ${m}m`;
}
async function _heartbeat() {
  const dot   = document.getElementById('hb-dot');
  const label = document.getElementById('hb-label');
  try {
    await fetch('/api/morning-brief', {method:'HEAD', cache:'no-store'});
    dot.className = 'ok'; label.textContent = 'Live \u2022 ' + _nextBriefTime();
    _hbOk = true;
  } catch(e) {
    dot.className = 'err'; label.textContent = 'Server offline';
    _hbOk = false;
  }
}
_heartbeat();
setInterval(_heartbeat, 30000);

let _mbToastTimer = null;
function _mbToast(msg, ok) {
  let t = document.getElementById('mb-toast');
  if (!t) { t = document.createElement('div'); t.id = 'mb-toast'; t.className = 'mb-toast'; document.body.appendChild(t); }
  t.textContent = msg;
  t.className = 'mb-toast ' + (ok ? 'ok' : 'err');
  void t.offsetWidth;
  t.classList.add('show');
  if (_mbToastTimer) clearTimeout(_mbToastTimer);
  _mbToastTimer = setTimeout(() => t.classList.remove('show'), 3000);
}

async function mbAddToStage(ticker, stage) {
  ticker = (ticker || '').trim().toUpperCase().replace(/[^A-Z0-9.]/g, '');
  if (!ticker) { _mbToast('Enter a ticker first', false); return; }
  try {
    const r = await fetch('/api/stagegate/add', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ticker, stage})
    });
    const d = await r.json();
    if (d.status === 'added')         _mbToast(`${ticker} added to Stage ${stage} ✓`, true);
    else if (d.status === 'already_exists') _mbToast(`${ticker} already in Stage Gate`, true);
    else                              _mbToast(d.error || 'Error adding ticker', false);
    const inp = document.getElementById('mb-ticker-input');
    if (inp) inp.value = '';
  } catch(e) {
    _mbToast('Network error: ' + e.message, false);
  }
}

loadBrief();

// ── Morning Brief ticker tape ─────────────────────────────────────────────────
(async function buildMbTape() {
  try {
    const sigs = await fetch('/api/signals').then(r => r.json());
    const track = document.getElementById('mb-tape');
    if (!track) return;
    const items = (sigs || []).filter(s => {
      const sig = (s.signal || '').toUpperCase();
      return sig === 'BUY' || sig === 'STRONG_BUY' || sig === 'SELL' || sig === 'STRONG_SELL';
    });
    if (!items.length) { track.innerHTML = '<span class="mbt-neu">No active signals</span>'; return; }
    const all = [...items, ...items];
    track.innerHTML = all.map(s => {
      const sig  = (s.signal || '').toUpperCase();
      const bull = sig === 'BUY' || sig === 'STRONG_BUY';
      const cls  = bull ? 'mbt-bull' : 'mbt-bear';
      const arr  = bull ? '&#9650;' : '&#9660;';
      const _lp = s.live_price || s.current_price;
      const price = _lp ? ' $' + Number(_lp).toFixed(2) : '';
      return '<span class="' + cls + '">' + arr + ' ' + s.ticker + price + '</span>'
           + '<span class="mbt-sep">|</span>';
    }).join('');
    track.style.animationDuration = Math.max(40, items.length * 0.8) + 's';
  } catch(e) { console.warn('[NWO] tape',e); }
})();

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    document.getElementById('mb-info-overlay').classList.remove('open');
  }
});
</script>
</body>
</html>
"""


EMBED_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NWO Activity Feed</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0d1117; color: #e6edf3; font-size: 13px; padding: 12px; }
  h2 { font-size: 12px; color: #8b949e; text-transform: uppercase; letter-spacing: 1px;
       border-bottom: 1px solid #21262d; padding-bottom: 6px; margin: 14px 0 8px; }
  h2:first-child { margin-top: 0; }
  /* Status bar */
  .status-bar { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; align-items: center; }
  .pill { padding: 3px 10px; border-radius: 12px; font-size: 11px; font-weight: 600; }
  .pill-green  { background: #1a4731; color: #3fb950; border: 1px solid #238636; }
  .pill-red    { background: #4a1519; color: #f85149; border: 1px solid #da3633; }
  .pill-yellow { background: #3d2b00; color: #d29922; border: 1px solid #9e6a03; }
  .pill-blue   { background: #1c2e50; color: #58a6ff; border: 1px solid #1f6feb; }
  .pill-grey   { background: #21262d; color: #8b949e; border: 1px solid #30363d; }
  .ts { font-size: 11px; color: #8b949e; margin-left: auto; }
  /* Tables */
  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; color: #8b949e; font-weight: 500; padding: 4px 6px;
       border-bottom: 1px solid #21262d; font-size: 11px; }
  td { padding: 5px 6px; border-top: 1px solid #161b22; vertical-align: top; }
  tr:hover td { background: #161b22; }
  .buy  { color: #3fb950; font-weight: 700; }
  .sell { color: #f85149; font-weight: 700; }
  .hold { color: #d29922; font-weight: 700; }
  .good { color: #3fb950; }
  .bad  { color: #f85149; }
  .warn { color: #d29922; }
  .dim  { color: #8b949e; font-size: 11px; }
  /* Gate pips */
  .pip { display: inline-block; width: 16px; height: 16px; line-height: 16px;
         text-align: center; border-radius: 3px; font-size: 9px; font-weight: 700; margin-right: 1px; }
  .pip-pass { background: #1a3a22; color: #3fb950; }
  .pip-fail { background: #3a1a1a; color: #f85149; }
  /* Conf bar */
  .bar-wrap { height: 6px; border-radius: 3px; background: #21262d; width: 60px; display: block; margin-bottom: 2px; }
  .bar-fill { height: 6px; border-radius: 3px; display: block; min-width: 2px; }
  /* Impact label */
  .impact { font-size: 10px; display: block; margin-top: 1px; }
  /* Section card */
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
          padding: 12px; margin-bottom: 12px; }
  .empty { color: #8b949e; font-style: italic; padding: 6px 0; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 5px; }
  .dot-green  { background: #3fb950; }
  .dot-red    { background: #f85149; }
  .dot-yellow { background: #d29922; }
</style>
</head>
<body>

<div class="card">
  <div class="status-bar" id="status-bar">
    <span class="pill pill-grey">Loading...</span>
  </div>
</div>

<div class="card">
  <h2>Positions (Live)</h2>
  <div id="positions"></div>
</div>

<div class="card">
  <h2>Recent Signals</h2>
  <div id="signals"></div>
</div>

<div class="card">
  <h2>Recent Trades</h2>
  <div id="trades"></div>
</div>

<script>
const API = '';  // same origin

async function loadStatus() {
  try {
    const d = await fetch(API + '/api/status').then(r => r.json());
    const paused  = d.paused;
    const dryRun  = d.dry_run;
    const vix     = d.vix_level != null ? d.vix_level.toFixed(1) : '—';
    const regime  = d.market_regime || '—';
    const lastCyc = d.last_cycle ? new Date(d.last_cycle.replace(' ','T')+'Z').toLocaleTimeString('en-US',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit'}) + ' ET' : '—';
    document.getElementById('status-bar').innerHTML =
      `<span class="dot ${paused?'dot-red':'dot-green'}"></span>`+
      `<span class="pill ${paused?'pill-red':'pill-green'}">${paused?'PAUSED':'LIVE'}</span>`+
      `<span class="pill ${dryRun?'pill-yellow':'pill-red'}">${dryRun?'DRY RUN':'REAL MONEY'}</span>`+
      `<span class="pill pill-blue">VIX ${vix} · ${regime}</span>`+
      `<span class="ts">Last cycle: ${lastCyc} &nbsp;·&nbsp; auto-refresh 30s</span>`;
  } catch(e) {
    document.getElementById('status-bar').innerHTML = '<span class="pill pill-grey">Offline</span>';
  }
}

async function loadSignals() {
  const el = document.getElementById('signals');
  try {
    const signals = await fetch(API + '/api/signals').then(r => r.json());
    if (!signals.length) { el.innerHTML = '<p class="empty">No signals yet.</p>'; return; }
    let html = `<table><tr>
      <th>Ticker</th><th>Signal</th><th>Price / Day</th>
      <th>Conf</th><th>MoS</th><th>FUD</th><th>Gates</th><th>Time</th>
    </tr>`;
    for (const s of signals) {
      const sigCls = s.signal==='BUY'?'buy':s.signal==='SELL'?'sell':'hold';
      const conf   = s.confidence || 0;
      const mosVal = s.margin_of_safety;
      const fudVal = s.fud_score || 0;

      // Gate evaluation (standard thresholds — display only)
      const gC = conf   >= 0.50;
      const gM = mosVal != null && mosVal >= 0.15;
      const gF = fudVal >= 0.60;
      const gN = [gC,gM,gF].filter(Boolean).length;
      const gCol = gN===3?'#3fb950':gN===2?'#d29922':'#f85149';

      // Conf bar + impact
      const barPct = Math.min(100, Math.round(conf*100));
      const barCol = conf>=0.50?'#3fb950':conf>=0.25?'#d29922':'#f85149';
      const confDelta = ((conf-0.50)*100).toFixed(1);
      const confImpCl = conf>=0.50?'good':'bad';

      // MoS
      const mosPct = mosVal!=null?(mosVal*100).toFixed(1)+'%':'—';
      const mosCls = mosVal==null?'dim':mosVal>=0.15?'good':mosVal>=0?'warn':'bad';
      const mosDelta = mosVal!=null?((mosVal-0.15)*100).toFixed(1):null;
      const mosImpCl = mosVal!=null&&mosVal>=0.15?'good':'bad';

      // FUD
      const fudDelta = (fudVal-0.60).toFixed(2);
      const fudCls   = fudVal>=0.60?'good':'bad';

      // Price — AI Signal History: label depends on source
      //   "exact" = stored live price at signal time  → plain price
      //   "eod"   = EOD close for signal date (approx) → amber "~EOD" badge
      //   null    = no historical data                 → live price with blue "now" badge
      const _histP  = s.current_price;
      const _liveP  = s.live_price;
      const _src    = s.price_source;
      const price   = _histP != null
        ? '$' + Number(_histP).toFixed(2)
          + (_src === 'eod'
              ? '<small style="color:#d29922;font-size:9px" title="EOD close for signal date — intraday price not available"> ~EOD</small>'
              : '')
        : (_liveP != null
            ? '$' + Number(_liveP).toFixed(2) + '<small style="color:#58a6ff;font-size:9px"> now</small>'
            : '—');
      const chg    = s.change_pct;
      const chgStr = chg!=null?(chg>=0?'+':'')+chg.toFixed(2)+'%':'—';
      const chgCls = chg==null?'dim':chg>0?'good':'bad';

      // Time (ET)
      const utcStr = s.generated_at.replace(' ','T')+'Z';
      const t = new Date(utcStr).toLocaleTimeString('en-US',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit'});

      html += `<tr>
        <td><strong>${s.ticker}</strong></td>
        <td class="${sigCls}">${s.signal}</td>
        <td>
          <span style="font-weight:600">${price}</span>
          <span class="${chgCls} dim" style="display:block">${chgStr}</span>
        </td>
        <td>
          <div class="bar-wrap"><div class="bar-fill" style="width:${barPct}%;background:${barCol}"></div></div>
          <span class="dim">${(conf*100).toFixed(0)}%</span>
          <span class="impact ${confImpCl}">${conf>=0.50?'▲':' ▼'} ${confDelta>=0?'+':''}${confDelta}pp</span>
        </td>
        <td class="${mosCls}">
          ${mosPct}
          ${mosDelta!=null?`<span class="impact ${mosImpCl}">${parseFloat(mosDelta)>=0?'▲ +':'▼ '}${mosDelta}pp</span>`:''}
        </td>
        <td class="${fudCls}">
          ${fudVal.toFixed(2)}
          <span class="impact ${fudVal>=0.60?'good':'bad'}">${fudVal>=0.60?'▲':' ▼'} ${parseFloat(fudDelta)>=0?'+':''}${fudDelta}</span>
        </td>
        <td>
          <span class="pip ${gC?'pip-pass':'pip-fail'}">C</span>
          <span class="pip ${gM?'pip-pass':'pip-fail'}">M</span>
          <span class="pip ${gF?'pip-pass':'pip-fail'}">F</span>
          <span style="color:${gCol};font-weight:700;margin-left:2px">${gN}/3</span>
        </td>
        <td class="dim">${t}</td>
      </tr>`;
    }
    el.innerHTML = html + '</table>';
  } catch(e) { el.innerHTML = '<p class="empty">Could not load signals.</p>'; }
}

async function loadTrades() {
  const el = document.getElementById('trades');
  try {
    const trades = await fetch(API + '/api/trades').then(r => r.json());
    if (!trades.length) { el.innerHTML = '<p class="empty">No trades logged yet.</p>'; return; }
    let html = '<table><tr><th>Ticker</th><th>Action</th><th>Qty</th><th>Price</th><th>Total</th><th>Mode</th></tr>';
    for (const t of trades) {
      const cls = t.action==='BUY'?'buy':'sell';
      html += `<tr>
        <td><strong>${t.ticker}</strong></td>
        <td class="${cls}">${t.action}</td>
        <td>${t.quantity||'—'}</td>
        <td>${t.price?'$'+t.price.toFixed(2):'—'}</td>
        <td>${t.total?'$'+t.total.toFixed(2):'—'}</td>
        <td class="dim">${t.dry_run?'DRY RUN':'LIVE'}</td>
      </tr>`;
    }
    el.innerHTML = html + '</table>';
  } catch(e) { el.innerHTML = '<p class="empty">Could not load trades.</p>'; }
}

async function loadPositions() {
  const el = document.getElementById('positions');
  try {
    const resp = await fetch(API + '/api/positions');
    if (!resp.ok) { el.innerHTML = '<p class="empty">Schwab offline or no positions.</p>'; return; }
    const positions = await resp.json();
    if (!positions.length || positions.error) { el.innerHTML = '<p class="empty">No open positions.</p>'; return; }
    let html = '<table><tr><th>Ticker</th><th>Shares</th><th>Avg Cost</th><th>Mkt Value</th><th>P&amp;L</th></tr>';
    let totV = 0, totP = 0;
    for (const p of positions) {
      const qty  = p.quantity || 0;
      const avg  = p.average_price;
      const mktV = p.market_value;
      const cost = avg != null ? avg * qty : null;
      const pnl  = mktV != null && cost != null ? mktV - cost : null;
      const pCls = pnl == null ? 'dim' : pnl >= 0 ? 'good' : 'bad';
      totV += mktV || 0; totP += pnl || 0;
      html += `<tr>
        <td><strong>${p.ticker}</strong></td>
        <td>${qty}</td>
        <td>${avg!=null?'$'+avg.toFixed(2):'—'}</td>
        <td>${mktV!=null?'$'+mktV.toFixed(2):'—'}</td>
        <td class="${pCls}">${pnl!=null?(pnl>=0?'+':'')+'$'+pnl.toFixed(2):'—'}</td>
      </tr>`;
    }
    const tCls = totP >= 0 ? 'good' : 'bad';
    html += `<tr style="font-weight:600;border-top:1px solid #21262d">
      <td colspan="3" class="dim">Total (${positions.length})</td>
      <td>$${totV.toFixed(2)}</td>
      <td class="${tCls}">${totP>=0?'+':''}$${totP.toFixed(2)}</td>
    </tr>`;
    el.innerHTML = html + '</table>';
  } catch(e) { el.innerHTML = '<p class="empty">Could not load positions.</p>'; }
}

async function refresh() {
  await Promise.all([loadStatus(), loadSignals(), loadTrades(), loadPositions()]);
}

refresh();
setInterval(refresh, 30000);
</script>
</body>
</html>
"""


@app.get("/embed", response_class=HTMLResponse)
def embed_view():
    """Read-only activity feed for embedding in Google Sites or any iframe."""
    return EMBED_HTML


# ── Thesis Pattern Analyzer ───────────────────────────────────────────────────

_thesis_analysis_running = False


def _run_thesis_analysis() -> None:
    """
    Read today's thesis log entries (ET date), call Claude Haiku to identify
    patterns, persist result to data/thesis_analysis.json.
    Runs in a background thread — scheduled at 16:10 ET or triggered manually.
    """
    global _thesis_analysis_running
    if _thesis_analysis_running:
        return
    _thesis_analysis_running = True

    import json as _j, logging as _lg
    _log = _lg.getLogger("thesis_analysis")
    try:
        api_key = config.brief.anthropic_api_key
        if not api_key:
            _log.warning("[ThesisAnalysis] No Anthropic API key — skipping")
            return
        if not _THESIS_LOG_PATH.exists():
            _log.info("[ThesisAnalysis] No thesis log yet — skipping")
            return

        today_et = datetime.now(_ET).date()
        try:
            all_entries = _j.loads(_THESIS_LOG_PATH.read_text())
        except Exception as exc:
            _log.warning(f"[ThesisAnalysis] Could not read thesis log: {exc}")
            return

        todays = []
        for e in all_entries:
            raw_ts = e.get("generated_at_utc", "")
            if not raw_ts:
                continue
            try:
                from datetime import datetime as _dt
                utc_dt = _dt.fromisoformat(raw_ts.replace("Z", "+00:00"))
                if utc_dt.astimezone(_ET).date() == today_et:
                    todays.append(e)
            except Exception:
                continue

        if len(todays) < 2:
            _log.info(f"[ThesisAnalysis] Only {len(todays)} thesis(es) today — need ≥2, skipping")
            return

        ticker_count = len({e["ticker"] for e in todays})
        entries_text = "\n".join(
            f"- {e['ticker']} ({e['signal']}, conf={e['confidence']:.0%}, "
            f"composite={e.get('composite_score', 0):.2f}, "
            f"MOS={e.get('margin_of_safety', 0):.0%}): {e['thesis_text']}"
            for e in todays
        )

        prompt = (
            f"Today's AI-generated trade theses for {ticker_count} tickers "
            f"({len(todays)} BUY/STRONG_BUY signals):\n\n"
            f"{entries_text}\n\n"
            f"Write a concise end-of-day pattern analysis covering four points:\n"
            f"1. Recurring sector or ticker themes (what narratives dominated today's signals).\n"
            f"2. Common risk factors mentioned across multiple theses.\n"
            f"3. A market-wide observation implied by the collective signal set.\n"
            f"4. One or two actionable recommendations for the trading system operator.\n\n"
            f"Plain prose only. No markdown, no bullets, no disclaimers. "
            f"4 sentences max — one per point. Reference actual tickers and scores. "
            f"Start directly with point 1."
        )

        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=350,
            messages=[{"role": "user", "content": prompt}],
        )
        analysis_text = msg.content[0].text.strip()

        entry = {
            "date":          str(today_et),
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "analysis":      analysis_text,
            "ticker_count":  ticker_count,
            "signal_count":  len(todays),
        }
        _save_thesis_analysis(entry)
        _log.info(f"[ThesisAnalysis] Done — {ticker_count} tickers, {len(todays)} signals → {len(analysis_text)} chars")

        # Evening push — send analysis to Telegram immediately after generation
        try:
            from monitor.telegram_bot import send_alert as _tg
            _tg(
                f"📊 <b>NWO Pattern Analysis — {entry['date']} (EOD)</b>\n"
                f"<i>{ticker_count} tickers · {len(todays)} signals</i>\n\n"
                f"{analysis_text}"
            )
        except Exception:
            pass

    except Exception as exc:
        import logging as _lg2
        _lg2.getLogger("thesis_analysis").warning(f"[ThesisAnalysis] Claude call failed: {exc}")
    finally:
        _thesis_analysis_running = False


# ── Morning Brief ─────────────────────────────────────────────────────────────

_brief_generating = False   # simple flag — only one generation at a time


@app.get("/api/morning-brief")
def api_morning_brief():
    """Return cached brief immediately. Never blocks."""
    try:
        from monitor.morning_brief import MorningBriefGenerator
        gen = MorningBriefGenerator()
        cached = gen.load_cached()
        if cached:
            cached["_generating"] = _brief_generating
            return cached
        # Nothing cached yet — trigger background generation and return status
        _trigger_brief_generation()
        return {"_generating": True, "narrative_html": "", "generated_at": None}
    except Exception as e:
        return JSONResponse(status_code=503, content={"error": str(e)})


@app.post("/api/morning-brief/refresh")
def api_morning_brief_refresh():
    """Kick off a background regeneration. Returns immediately."""
    if _brief_generating:
        return {"status": "already_generating"}
    _trigger_brief_generation()
    return {"status": "started"}


def _trigger_brief_generation():
    global _brief_generating
    if _brief_generating:
        return
    def _run():
        global _brief_generating
        _brief_generating = True
        try:
            from monitor.morning_brief import get_brief
            get_brief(force_refresh=True)
        except Exception as e:
            _logger.warning(f"[BRIEF] Background generation failed: {e}")
        finally:
            _brief_generating = False
    threading.Thread(target=_run, daemon=True).start()


@app.get("/api/thesis/analysis")
def api_thesis_analysis():
    """Return the most-recent daily pattern analysis, or null."""
    entry = _load_thesis_analysis()
    return JSONResponse(entry)


@app.post("/api/thesis/analysis/trigger")
def api_thesis_analysis_trigger():
    """Manual trigger — for testing without waiting for 16:10 ET."""
    if _thesis_analysis_running:
        return {"status": "already_running"}
    threading.Thread(target=_run_thesis_analysis, daemon=True,
                     name="thesis-analysis-manual").start()
    return {"status": "started"}


@app.get("/morning-brief", response_class=HTMLResponse)
def morning_brief_page():
    html = MORNING_BRIEF_HTML
    html = html.replace('</style>', _NAV_CSS + '</style>', 1)
    html = html.replace('<a class="back-btn" href="/">&#8592; Dashboard</a>', _nav_html('brief'), 1)
    html = html.replace('<a class="back-btn" href="/">← Dashboard</a>', _nav_html('brief'), 1)
    return html


@app.get("/live-trading", response_class=HTMLResponse)
def live_trading_page():
    nav = _nav_html('live')
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Live Trading — NWO Monitor</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; }}
  body {{ margin: 0; background: #0d1117; color: #e6edf3; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }}
  header {{ padding: 10px 16px; border-bottom: 1px solid #21262d; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
  .header-right {{ margin-left: auto; display: flex; gap: 8px; align-items: center; }}
  {_NAV_CSS}
  .lt-hero {{ display: flex; flex-direction: column; align-items: center; justify-content: center;
               padding: 80px 24px; text-align: center; }}
  .lt-badge {{ background: rgba(210,153,34,0.15); border: 1px solid #d29922; color: #d29922;
                border-radius: 20px; padding: 4px 16px; font-size: 12px; font-weight: 700;
                letter-spacing: 1px; text-transform: uppercase; margin-bottom: 24px; }}
  .lt-title {{ font-size: 32px; font-weight: 800; margin-bottom: 12px; }}
  .lt-sub   {{ font-size: 14px; color: #8b949e; max-width: 560px; line-height: 1.75; margin-bottom: 32px; }}
  .lt-grid  {{ display: grid; grid-template-columns: repeat(3,1fr); gap: 16px; max-width: 780px; width: 100%; }}
  .lt-card  {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 18px;
               text-align: left; }}
  .lt-card-title {{ font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: #8b949e; margin-bottom: 8px; }}
  .lt-card-val   {{ font-size: 14px; font-weight: 600; color: #8b949e; }}
  .lt-card-val.ready {{ color: #3fb950; }}
</style>
</head>
<body>
<div class="sticky-banner">
  <header>
    <h1 style="font-size:16px;margin:0;font-weight:700;">&#128185; Live Trading</h1>
    <div class="header-right">{nav}</div>
  </header>
  <div class="sh-tape-wrap"><div class="sh-tape-track" id="sh-tape"><span class="sht-neu">Loading…</span></div></div>
</div>
<div class="lt-hero">
  <div class="lt-badge">&#9888; Coming Soon</div>
  <div class="lt-title">&#128185; Live Trading</div>
  <div class="lt-sub">
    Real order execution through the Schwab Trader API using the identical 6-layer AI pipeline as Paper Trade.
    This page will mirror the Paper Trade interface once live trading is enabled.<br><br>
    <b style="color:#d29922">Dry-run mode remains ON</b> until the account owner explicitly flips the switch.
    All risk controls — max 5% position, 25% sector cap, 3% daily loss halt — are enforced at all times.
  </div>
  <div class="lt-grid">
    <div class="lt-card">
      <div class="lt-card-title">&#128274; Auth / OAuth</div>
      <div class="lt-card-val ready">&#10003; Ready</div>
    </div>
    <div class="lt-card">
      <div class="lt-card-title">&#128202; AI Pipeline</div>
      <div class="lt-card-val ready">&#10003; Ready (Paper)</div>
    </div>
    <div class="lt-card">
      <div class="lt-card-title">&#128176; Live Execution</div>
      <div class="lt-card-val">&#9711; Pending sign-off</div>
    </div>
    <div class="lt-card">
      <div class="lt-card-title">&#128737; Risk Controls</div>
      <div class="lt-card-val ready">&#10003; Enforced</div>
    </div>
    <div class="lt-card">
      <div class="lt-card-title">&#127919; Stop / Target Monitor</div>
      <div class="lt-card-val ready">&#10003; Ready</div>
    </div>
    <div class="lt-card">
      <div class="lt-card-title">&#128483; Multi-user Scope</div>
      <div class="lt-card-val">&#9711; In design</div>
    </div>
  </div>
</div>
{_NAV_TAPE_JS}
</body></html>""")


@app.get("/jstest", response_class=HTMLResponse)
def jstest():
    from fastapi.responses import HTMLResponse as HR
    return HR(content="""<!DOCTYPE html>
<html><body>
<div id="out" style="font-size:24px;padding:20px;background:#0d1117;color:red">JS NOT RUNNING</div>
<script>
document.getElementById('out').textContent = 'JS WORKS';
document.getElementById('out').style.color = 'lime';
fetch('/api/itool').then(r=>r.json()).then(d=>{
  document.getElementById('out').textContent = 'FETCH OK: ' + JSON.stringify(d.counts);
}).catch(e=>{
  document.getElementById('out').textContent = 'FETCH ERROR: ' + e;
  document.getElementById('out').style.color = 'orange';
});
</script>
</body></html>""", headers={"Cache-Control":"no-store"})


# ── PWA Launcher ──────────────────────────────────────────────────────────────
# Served at /launcher — install to home screen, auto-routes LAN vs Tailscale.

LAUNCHER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="NWO">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0d1117">
<link rel="manifest" href="/manifest.json">
<title>NWO Monitor</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d1117; color: #e6edf3; font-family: 'Segoe UI', system-ui, sans-serif;
         display: flex; flex-direction: column; align-items: center; justify-content: center;
         min-height: 100vh; gap: 12px; padding: 24px; }
  .icon { font-size: 64px; line-height: 1; }
  h1 { font-size: 22px; letter-spacing: 3px; color: #58a6ff; }
  .sub { font-size: 13px; color: #8b949e; }
  .status-row { display: flex; align-items: center; gap: 8px; margin-top: 8px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: #d29922;
         animation: blink 1.2s ease-in-out infinite; flex-shrink: 0; }
  .dot.green { background: #3fb950; animation: none; }
  .dot.red   { background: #f85149; animation: none; }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.2} }
  #msg { font-size: 13px; color: #8b949e; }
  .route-pill { padding: 3px 10px; border-radius: 10px; font-size: 11px; font-weight: 600;
                background: #1a4731; color: #3fb950; display: none; }
  .route-pill.vpn { background: #1c2e50; color: #58a6ff; }
  .route-pill.err { background: #4a1519; color: #f85149; }
  button { margin-top: 16px; padding: 12px 28px; background: #161b22; color: #58a6ff;
           border: 1px solid #1f6feb; border-radius: 8px; font-size: 15px; font-weight: 600;
           cursor: pointer; display: none; width: 100%; max-width: 280px; }
  button:hover { background: #1c2e50; }
  .install-hint { font-size: 11px; color: #484f58; margin-top: 24px; text-align: center;
                  line-height: 1.6; max-width: 280px; }
</style>
</head>
<body>
<div class="icon">&#x1F4C8;</div>
<h1>NWO MONITOR</h1>
<p class="sub">Automated Trading Dashboard</p>
<div class="status-row">
  <div class="dot" id="dot"></div>
  <span id="msg">Detecting network...</span>
</div>
<span class="route-pill" id="pill"></span>
<button id="open-btn">Open Dashboard &#x2192;</button>
<button id="retry-btn" onclick="autoRoute()">Retry</button>
<p class="install-hint" id="hint"></p>

<script>
const LAN = 'http://192.168.1.184:8765';
const VPN = 'http://100.119.78.45:8765';
const TIMEOUT_MS = 2500;

function setStatus(dotCls, msg, pillText, pillCls) {
  const dot  = document.getElementById('dot');
  const msgEl = document.getElementById('msg');
  const pill = document.getElementById('pill');
  dot.className  = 'dot ' + (dotCls || '');
  msgEl.textContent = msg;
  if (pillText) {
    pill.textContent = pillText;
    pill.className = 'route-pill ' + (pillCls || '');
    pill.style.display = 'inline';
  } else {
    pill.style.display = 'none';
  }
}

async function tryUrl(url) {
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
    const r = await fetch(url + '/api/status', {
      signal: ctrl.signal,
      cache: 'no-store',
    });
    clearTimeout(t);
    return r.ok;
  } catch {
    return false;
  }
}

// true when running as installed PWA (standalone), false in browser
const isInstalled = window.matchMedia('(display-mode: standalone)').matches
                 || window.navigator.standalone === true;

let _targetUrl = null;

async function autoRoute() {
  document.getElementById('retry-btn').style.display = 'none';
  document.getElementById('open-btn').style.display  = 'none';
  setStatus('', 'Trying local network...', '', '');
  if (await tryUrl(LAN)) {
    _targetUrl = LAN;
    setStatus('green', 'Connected via local WiFi', 'LAN', '');
    if (isInstalled) {
      setTimeout(() => { window.location.replace(LAN); }, 600);
    } else {
      showOpenButton(LAN);
    }
    return;
  }
  setStatus('', 'Trying Tailscale VPN...', '', '');
  if (await tryUrl(VPN)) {
    _targetUrl = VPN;
    setStatus('green', 'Connected via Tailscale', 'Tailscale', 'vpn');
    if (isInstalled) {
      setTimeout(() => { window.location.replace(VPN); }, 600);
    } else {
      showOpenButton(VPN);
    }
    return;
  }
  setStatus('red', 'Dashboard unreachable', 'Offline', 'err');
  document.getElementById('retry-btn').style.display = 'inline-block';
}

function showOpenButton(url) {
  const btn = document.getElementById('open-btn');
  btn.style.display = 'inline-block';
  btn.onclick = () => { window.location.replace(url); };
}

// Show platform-specific install instructions when in browser
const ua = navigator.userAgent;
const hint = document.getElementById('hint');
if (!isInstalled) {
  if (/iPhone|iPad/.test(ua)) {
    hint.innerHTML = '&#8595; Install: tap <strong>Share</strong> then <strong>"Add to Home Screen"</strong>';
  } else if (/Android/.test(ua)) {
    hint.innerHTML = '&#8595; Install: tap <strong>&#8942;</strong> then <strong>"Add to Home Screen"</strong>';
  }
}

// Register service worker for offline launcher caching
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}

autoRoute();
</script>
</body>
</html>"""


MANIFEST_JSON = """{
  "name": "NWO Monitor",
  "short_name": "NWO",
  "description": "New World Order Automated Trading Dashboard",
  "start_url": "/launcher",
  "display": "standalone",
  "background_color": "#0d1117",
  "theme_color": "#0d1117",
  "orientation": "portrait",
  "icons": [
    {
      "src": "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Crect width='100' height='100' rx='20' fill='%230d1117'/%3E%3Ctext y='.9em' font-size='80' x='10'%3E%F0%9F%93%88%3C/text%3E%3C/svg%3E",
      "sizes": "any",
      "type": "image/svg+xml",
      "purpose": "any maskable"
    }
  ]
}"""


SW_JS = """// NWO Monitor service worker — caches the launcher for offline use
const CACHE = 'nwo-launcher-v1';
const LAUNCHER = '/launcher';

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.add(LAUNCHER)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  // Only intercept the launcher page itself — all API calls go straight to network
  if (new URL(e.request.url).pathname === LAUNCHER) {
    e.respondWith(
      fetch(e.request).then(r => {
        // Update cache with fresh copy
        const copy = r.clone();
        caches.open(CACHE).then(c => c.put(e.request, copy));
        return r;
      }).catch(() => caches.match(LAUNCHER))
    );
  }
});
"""


@app.get("/launcher", response_class=HTMLResponse)
def launcher():
    return LAUNCHER_HTML


@app.get("/manifest.json")
def manifest_route():
    from fastapi.responses import Response
    return Response(content=MANIFEST_JSON, media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    from fastapi.responses import Response
    return Response(
        content=SW_JS,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


# ── I-Tool: S&P 500 Technical Scanner ────────────────────────────────────────

_itool_scanning = False

ITOOL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>I-Tool — S&P 500 Scanner</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d1117; color: #e6edf3; font-family: 'Segoe UI', monospace; font-size: 14px; }
  /* Ticker tape */
  .tape-wrap { overflow: hidden; background: #0a0f17; border-bottom: 1px solid #1f6feb;
               height: 34px; display: flex; align-items: center; position: relative; }
  .tape-track { display: flex; gap: 28px; white-space: nowrap; will-change: transform;
                animation: tape-scroll 100s linear infinite; align-items: center; }
  .tape-track:hover { animation-play-state: paused; }
  @keyframes tape-scroll { 0%{transform:translateX(0)} 100%{transform:translateX(-50%)} }
  .tape-bull { color: #3fb950; font-size: 12px; font-weight: 700; }
  .tape-bear { color: #f85149; font-size: 12px; font-weight: 700; }
  .tape-sep  { color: #30363d; font-size: 10px; }
  /* Header */
  header { background: #161b22; padding: 12px 20px; border-bottom: 1px solid #30363d;
           display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
  .back-btn { padding: 5px 12px; border-radius: 6px; border: 1px solid #30363d;
              background: #21262d; color: #8b949e; text-decoration: none; font-size: 12px; }
  .back-btn:hover { background: #30363d; }
  header h1 { font-size: 17px; letter-spacing: 1.5px; color: #58a6ff; }
  .scan-info { font-size: 11px; color: #8b949e; margin-left: auto; }
  .rescan-btn { padding: 6px 16px; border-radius: 6px; border: 1px solid #1f6feb;
                background: #0d1e36; color: #58a6ff; cursor: pointer; font-size: 12px; }
  .rescan-btn:hover { background: #1c2e50; }
  .rescan-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  /* Status bar */
  .status-bar { padding: 8px 20px; font-size: 12px; color: #8b949e;
                border-bottom: 1px solid #21262d; background: #161b22;
                display: flex; gap: 16px; align-items: center; flex-wrap: wrap; }
  .cnt-bull { color: #3fb950; font-weight: 600; }
  .cnt-bear { color: #f85149; font-weight: 600; }
  .scanning-pulse { color: #d29922; animation: pulse 1.2s ease-in-out infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
  /* Main */
  main { padding: 16px; }
  /* Filter tabs */
  .tabs { display: flex; gap: 6px; margin-bottom: 14px; align-items: center; }
  .tab-btn { padding: 5px 16px; border-radius: 6px; border: 1px solid #30363d;
             cursor: pointer; font-size: 12px; font-weight: 600;
             background: #21262d; color: #8b949e; transition: all 0.15s; }
  .tab-btn:hover { border-color: #58a6ff; color: #e6edf3; }
  .tab-btn.active { background: #1c2e50; color: #58a6ff; border-color: #58a6ff; }
  .tab-btn.t-bull.active { background: #1a4731; color: #3fb950; border-color: #3fb950; }
  .tab-btn.t-bear.active { background: #4a1519; color: #f85149; border-color: #f85149; }
  .count-label { font-size: 11px; color: #8b949e; margin-left: 6px; }
  .search-box { padding: 5px 10px; border-radius: 6px; border: 1px solid #30363d;
                background: #21262d; color: #e6edf3; font-size: 12px; width: 160px; margin-left: auto; }
  .search-box::placeholder { color: #8b949e; }
  th.sortable { cursor: pointer; user-select: none; }
  th.sortable:hover { color: #e6edf3; }
  th .sort-arrow { font-size: 10px; margin-left: 3px; opacity: 0.5; }
  th.sort-asc .sort-arrow, th.sort-desc .sort-arrow { opacity: 1; color: #58a6ff; }
  /* Table */
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; color: #8b949e; font-weight: 500; padding: 5px 8px;
       border-bottom: 1px solid #30363d; white-space: nowrap; }
  td { padding: 6px 8px; border-top: 1px solid #21262d; }
  tr:hover td { background: #1c2128; cursor: pointer; }
  .sig-bull { color: #3fb950; font-weight: 700; }
  .sig-bear { color: #f85149; font-weight: 700; }
  .pos { color: #3fb950; }
  .neg { color: #f85149; }
  .neu { color: #8b949e; }
  .chart-btn { padding: 2px 10px; border-radius: 5px; border: 1px solid #30363d;
               cursor: pointer; font-size: 11px; background: #21262d; color: #e6edf3; }
  .chart-btn:hover { background: #30363d; }
  /* Chart modal */
  .modal-bg { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.82);
              z-index: 200; align-items: center; justify-content: center; }
  .modal-bg.open { display: flex; }
  .modal-box { background: #161b22; border: 1px solid #30363d; border-radius: 10px;
               padding: 18px; width: min(860px, 96vw); max-height: 92vh; overflow-y: auto; }
  .modal-header { display: flex; justify-content: space-between; align-items: center;
                  margin-bottom: 14px; }
  .modal-title { font-size: 16px; font-weight: 700; color: #e6edf3; }
  .modal-meta  { font-size: 12px; color: #8b949e; margin-top: 2px; }
  .close-btn { background: none; border: none; color: #8b949e; cursor: pointer;
               font-size: 20px; padding: 0 4px; line-height: 1; }
  .chart-section { margin-bottom: 10px; }
  .chart-label { font-size: 11px; color: #8b949e; margin-bottom: 4px; letter-spacing: 0.5px; }
  canvas { display: block; }
  .empty { color: #8b949e; font-style: italic; padding: 20px; text-align: center; }
  @media (max-width: 700px) {
    th:nth-child(3), td:nth-child(3),
    th:nth-child(4), td:nth-child(4) { display: none; }
    table { font-size: 11px; }
    th, td { padding: 4px 5px; }
  }
</style>
</head>
<body>

<!-- Ticker tape -->
<div class="tape-wrap"><div class="tape-track" id="tape-track">&nbsp;</div></div>

<header>
  <a href="/" class="back-btn">&#8592; Dashboard</a>
  <h1>&#x1F4E1; I-Tool &mdash; S&amp;P 500 Scanner <small style="font-size:11px;color:#8b949e">[v4]</small></h1>
  <span class="scan-info" id="scan-info">Loading...</span>
  <button class="rescan-btn" id="rescan-btn" onclick="triggerScan()">&#8635; Rescan</button>
</header>

<div class="status-bar" id="status-bar">
  <span id="js-check" style="color:#f85149">JS not loaded [v4]</span>
  <noscript><span style="color:orange;font-weight:700"> — JavaScript DISABLED in browser settings</span></noscript>
</div>

<main>
  <div class="tabs">
    <button class="tab-btn active"  id="tab-all"     onclick="setFilter('all')">All Signals</button>
    <button class="tab-btn t-bull"  id="tab-bullish"  onclick="setFilter('bullish')">&#9650; Bullish</button>
    <button class="tab-btn t-bear"  id="tab-bearish"  onclick="setFilter('bearish')">&#9660; Bearish</button>
    <span class="count-label" id="count-label"></span>
    <input class="search-box" id="search-box" type="text" placeholder="&#128269; Search ticker..." oninput="applyFilter()">
  </div>
  <div id="results-wrap"></div>
</main>

<!-- Chart modal -->
<div class="modal-bg" id="chart-modal" onclick="modalBgClick(event)">
  <div class="modal-box" id="modal-box">
    <div class="modal-header">
      <div>
        <div class="modal-title" id="modal-title"></div>
        <div class="modal-meta"  id="modal-meta"></div>
      </div>
      <button class="close-btn" onclick="closeModal()">&#x2715;</button>
    </div>
    <div class="chart-section">
      <div class="chart-label">PRICE &amp; MOVING AVERAGES</div>
      <div style="position:relative;height:180px"><canvas id="chart-price"></canvas></div>
    </div>
    <div class="chart-section">
      <div class="chart-label">MACD HISTOGRAM (8, 17, 9)</div>
      <div style="position:relative;height:100px"><canvas id="chart-macd"></canvas></div>
    </div>
    <div class="chart-section">
      <div class="chart-label">STOCHASTIC %K / %D (14, 5, 5)</div>
      <div style="position:relative;height:100px"><canvas id="chart-stoch"></canvas></div>
    </div>
  </div>
</div>

<script>
// Inline check — runs immediately, no external file needed
document.getElementById('js-check').textContent = 'Inline JS OK, loading itool.js... [v4]';
document.getElementById('js-check').style.color = '#d29922';
</script>
<script src="/itool.js"
  onerror="document.getElementById('js-check').textContent='ERROR: /itool.js failed to load [v4]'; document.getElementById('js-check').style.color='orange';">
</script>
<!-- Chart.js loads async — only needed when a chart modal is opened -->
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js" async></script>
</body>
</html>"""

ITOOL_JS = """
// Debug: confirm script is executing
document.getElementById('js-check').textContent = 'JS loaded, fetching...';
document.getElementById('js-check').style.color = '#d29922';

let _scanData   = null;
let _filter     = 'all';
let _charts     = {};
let _pollTimer  = null;
let _sortCol    = null;   // column key being sorted
let _sortDir    = 1;      // 1 = asc, -1 = desc

// ── Data loading ─────────────────────────────────────────────────────────────

async function loadScan() {
  try {
    const d = await fetch('/api/itool').then(r => r.json());
    if (d.error) { showError(d.error); return; }

    if (d._scanning || !d.generated_at) {
      showScanning();
      if (!_pollTimer) _pollTimer = setInterval(loadScan, 5000);
      return;
    }

    clearInterval(_pollTimer); _pollTimer = null;
    _scanData = d;

    const gt = new Date(d.generated_at + 'Z');
    document.getElementById('scan-info').textContent =
      'Scanned ' + gt.toLocaleDateString('en-US', {timeZone:'America/New_York',month:'2-digit',day:'2-digit'})
      + ' ' + gt.toLocaleTimeString('en-US', {timeZone:'America/New_York',hour:'2-digit',minute:'2-digit'}) + ' ET';

    const c = d.counts || {};
    document.getElementById('status-bar').innerHTML =
      '<span>' + (d.total_scanned||0) + ' stocks scanned</span>' +
      '<span class="cnt-bull">&#9650; ' + (c.bullish||0) + ' Bullish</span>' +
      '<span class="cnt-bear">&#9660; ' + (c.bearish||0) + ' Bearish</span>' +
      '<span class="neu">' + ((c.total||0) - (c.bullish||0) - (c.bearish||0)) + ' Neutral shown</span>';

    buildTape(d.results || []);
    applyFilter();
    document.getElementById('rescan-btn').disabled = false;
  } catch(e) {
    showError('Could not reach server.');
  }
}

function showScanning() {
  document.getElementById('status-bar').innerHTML =
    '<span class="scanning-pulse">&#8635; Scanning ~500 stocks... this takes ~2 minutes. Page auto-refreshes.</span>';
  document.getElementById('rescan-btn').disabled = true;
}

function showError(msg) {
  document.getElementById('status-bar').innerHTML = '<span style="color:#f85149">' + msg + '</span>';
}

async function triggerScan() {
  document.getElementById('rescan-btn').disabled = true;
  showScanning();
  await fetch('/api/itool/refresh', {method: 'POST'});
  if (!_pollTimer) _pollTimer = setInterval(loadScan, 5000);
}

// ── Ticker tape ───────────────────────────────────────────────────────────────

function buildTape(results) {
  const signals = results.filter(r => r.signal !== 'neutral');
  if (!signals.length) {
    document.getElementById('tape-track').innerHTML = '<span class="neu">No signals detected</span>';
    return;
  }
  // Duplicate for seamless loop
  const all = [...signals, ...signals];
  const track = document.getElementById('tape-track');
  track.innerHTML = all.map((r, i) =>
    '<span class="tape-' + r.signal.replace('bullish','bull').replace('bearish','bear') + '">' +
    (r.signal === 'bullish' ? '&#9650;' : '&#9660;') + ' ' +
    r.ticker + ' $' + r.price.toFixed(2) + '</span>' +
    '<span class="tape-sep">|</span>'
  ).join('');
  const dur = Math.max(50, signals.length * 0.625);
  track.style.animationDuration = dur + 's';
}

// ── Filter & table ────────────────────────────────────────────────────────────

function setFilter(f) {
  _filter = f;
  ['all','bullish','bearish'].forEach(id => {
    document.getElementById('tab-' + id).classList.toggle('active', id === f);
  });
  if (_scanData) applyFilter();
}

function setSort(col) {
  if (_sortCol === col) {
    _sortDir = _sortDir === 1 ? -1 : (_sortDir === -1 ? 1 : 1);
  } else {
    _sortCol = col; _sortDir = -1;  // default: largest first
  }
  if (_scanData) applyFilter();
}

function applyFilter() {
  const results = _scanData.results || [];
  const term = (document.getElementById('search-box') || {}).value || '';
  const q = term.trim().toUpperCase();
  let filtered = _filter === 'all' ? results : results.filter(r => r.signal === _filter);
  if (q) filtered = filtered.filter(r => r.ticker.toUpperCase().includes(q));
  // Sort
  if (_sortCol) {
    filtered = [...filtered].sort((a, b) => {
      let av = a[_sortCol], bv = b[_sortCol];
      if (av == null) av = _sortDir > 0 ? Infinity : -Infinity;
      if (bv == null) bv = _sortDir > 0 ? Infinity : -Infinity;
      return (av < bv ? -1 : av > bv ? 1 : 0) * _sortDir;
    });
  }
  document.getElementById('count-label').textContent =
    filtered.length + ' of ' + results.length + ' stocks';
  renderTable(filtered);
}

function renderTable(results) {
  const wrap = document.getElementById('results-wrap');
  if (!results.length) {
    wrap.innerHTML = '<p class="empty">No stocks match the current filter.</p>';
    return;
  }
  function sortTh(col, label) {
    const active = _sortCol === col;
    const dir = active && _sortDir === 1 ? 'desc' : active && _sortDir === -1 ? '' : 'asc';
    const arrow = active ? (_sortDir === 1 ? ' &#9650;' : ' &#9660;') : ' &#8693;';
    const cls = active ? (_sortDir === 1 ? 'sort-asc' : 'sort-desc') : '';
    return '<th class="sortable ' + cls + '" onclick="setSort(\\'' + col + '\\')">' + label + '<span class="sort-arrow">' + arrow + '</span></th>';
  }
  const scanTs = (_scanData && _scanData.generated_at)
    ? new Date(_scanData.generated_at + 'Z').toLocaleString('en-US', {
        timeZone: 'America/New_York',
        month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit'
      }) + ' ET'
    : '&#8212;';
  let html = '<table><tr>' +
    '<th>Ticker</th>' +
    sortTh('price','Price') +
    sortTh('sma30','SMA30') +
    sortTh('sma50','SMA50') +
    sortTh('macd_hist','MACD Hist') +
    sortTh('stoch_k','Stoch %K') +
    '<th>Signal</th><th>Scanned</th><th>Chart</th>' +
    '</tr>';
  for (const r of results) {
    const sigCls  = r.signal === 'bullish' ? 'sig-bull' : 'sig-bear';
    const sigIcon = r.signal === 'bullish' ? '&#9650;' : '&#9660;';
    const mCls = r.macd_hist > 0 ? 'pos' : r.macd_hist < 0 ? 'neg' : 'neu';
    const kCls = r.stoch_k < 25 ? 'pos' : r.stoch_k > 75 ? 'neg' : 'neu';
    html += '<tr onclick="openChart(\\'' + r.ticker + '\\')">' +
      '<td><strong>' + r.ticker + '</strong></td>' +
      '<td>$' + r.price.toFixed(2) + '</td>' +
      '<td>' + (r.sma30 ? '$' + r.sma30.toFixed(2) : '&#8212;') + '</td>' +
      '<td>' + (r.sma50 ? '$' + r.sma50.toFixed(2) : '&#8212;') + '</td>' +
      '<td class="' + mCls + '">' + r.macd_hist.toFixed(4) + '</td>' +
      '<td class="' + kCls + '">' + r.stoch_k.toFixed(1) + '</td>' +
      '<td class="' + sigCls + '">' + sigIcon + ' ' + r.signal.toUpperCase() + '</td>' +
      '<td style="font-size:11px;color:#8b949e;white-space:nowrap;">' + scanTs + '</td>' +
      '<td><button class="chart-btn" onclick="event.stopPropagation();openChart(\\'' +
        r.ticker + '\\')">Chart</button>' +
      ' <button class="it-add-btn it-add-s1" onclick="event.stopPropagation();itAddToStage(\\'' + r.ticker + '\\',\\'1\\')" title="Add to Stage 1">+S1</button>' +
      ' <button class="it-add-btn it-add-s2" onclick="event.stopPropagation();itAddToStage(\\'' + r.ticker + '\\',\\'2\\')" title="Add to Stage 2">+S2</button></td>' +
      '</tr>';
  }
  wrap.innerHTML = html + '</table>';
}

// ── Chart modal ───────────────────────────────────────────────────────────────

function openChart(ticker) {
  if (typeof Chart === 'undefined') {
    document.getElementById('status-bar').innerHTML +=
      ' &nbsp;<span style="color:#d29922">Chart library loading, try again in a moment...</span>';
    return;
  }
  const r = (_scanData?.results || []).find(x => x.ticker === ticker);
  if (!r) return;

  document.getElementById('modal-title').textContent = ticker + ' — Technical Analysis';
  document.getElementById('modal-meta').innerHTML =
    '<span class="' + (r.signal==='bullish'?'sig-bull':'sig-bear') + '">' +
    (r.signal==='bullish'?'&#9650;':'&#9660;') + ' ' + r.signal.toUpperCase() + '</span>' +
    ' &nbsp;|&nbsp; Price: $' + r.price.toFixed(2) +
    ' &nbsp;|&nbsp; MACD: <span class="' + (r.macd_hist>0?'pos':'neg') + '">' +
      r.macd_hist.toFixed(4) + '</span>' +
    ' &nbsp;|&nbsp; Stoch %K: ' + r.stoch_k.toFixed(1);

  document.getElementById('chart-modal').classList.add('open');

  // Destroy previous instances
  ['chart-price','chart-macd','chart-stoch'].forEach(id => {
    if (_charts[id]) { _charts[id].destroy(); delete _charts[id]; }
  });

  const labels = r.dates;
  const dark   = { grid: { color: '#21262d' }, ticks: { color: '#8b949e', font: {size:10} } };

  // ── Panel 1: Price + SMA30 + SMA50 + crossover markers ──
  const bullPts = (r.bull_price_idx || []).map(i => ({x: labels[i], y: r.closes[i]}));
  const bearPts = (r.bear_price_idx || []).map(i => ({x: labels[i], y: r.closes[i]}));

  _charts['chart-price'] = new Chart(
    document.getElementById('chart-price').getContext('2d'), {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: 'Price', data: r.closes, borderColor: '#e6edf3', borderWidth: 1.5,
          pointRadius: 0, tension: 0.1, fill: false, order: 3 },
        { label: 'SMA30', data: r.sma30_series, borderColor: '#58a6ff',
          borderWidth: 1.2, pointRadius: 0, tension: 0, fill: false, order: 2 },
        { label: 'SMA50', data: r.sma50_series, borderColor: '#d29922',
          borderWidth: 1.2, pointRadius: 0, tension: 0, fill: false, order: 2 },
        { type: 'scatter', label: 'Bullish', data: bullPts,
          pointStyle: 'triangle', pointRadius: 9, rotation: 0,
          backgroundColor: '#3fb950', borderColor: '#3fb950', order: 1 },
        { type: 'scatter', label: 'Bearish', data: bearPts,
          pointStyle: 'triangle', pointRadius: 9, rotation: 180,
          backgroundColor: '#f85149', borderColor: '#f85149', order: 1 },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#8b949e', font: {size:11},
        filter: i => i.text !== 'Bullish' && i.text !== 'Bearish' } } },
      scales: {
        x: { ...dark, x: {}, ticks: { ...dark.ticks, maxTicksLimit: 8 } },
        y: { ...dark }
      }
    }
  });

  // ── Panel 2: MACD Histogram ──────────────────────────────
  const histColors = (r.macd_hist_series || []).map(v =>
    v === null ? 'transparent' : v >= 0 ? 'rgba(63,185,80,0.7)' : 'rgba(248,81,73,0.7)');
  const histBorders = (r.macd_hist_series || []).map(v =>
    v === null ? 'transparent' : v >= 0 ? '#3fb950' : '#f85149');

  // Mark MACD crossover bars with bright borders
  (r.bull_macd_idx || []).forEach(i => { histBorders[i] = '#58a6ff'; });
  (r.bear_macd_idx || []).forEach(i => { histBorders[i] = '#ff9900'; });

  _charts['chart-macd'] = new Chart(
    document.getElementById('chart-macd').getContext('2d'), {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        label: 'MACD Hist',
        data: r.macd_hist_series,
        backgroundColor: histColors,
        borderColor: histBorders,
        borderWidth: 1,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#8b949e', font: {size:11} } } },
      scales: {
        x: { ...dark, ticks: { ...dark.ticks, maxTicksLimit: 8 } },
        y: { ...dark }
      }
    }
  });

  // ── Panel 3: Stochastic ──────────────────────────────────
  _charts['chart-stoch'] = new Chart(
    document.getElementById('chart-stoch').getContext('2d'), {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: '%K', data: r.stoch_k_series, borderColor: '#58a6ff',
          borderWidth: 1.5, pointRadius: 0, tension: 0.1, fill: false },
        { label: '%D', data: r.stoch_d_series, borderColor: '#d29922',
          borderWidth: 1, pointRadius: 0, tension: 0.1, fill: false,
          borderDash: [4, 3] },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#8b949e', font: {size:11} } } },
      scales: {
        x: { ...dark, ticks: { ...dark.ticks, maxTicksLimit: 8 } },
        y: {
          ...dark, min: 0, max: 100,
          ticks: { ...dark.ticks,
            callback: v => [0, 25, 50, 75, 100].includes(v) ? v : '' },
          grid: {
            color: ctx => {
              if (ctx.tick.value === 25 || ctx.tick.value === 75)
                return 'rgba(248,81,73,0.25)';
              return '#21262d';
            }
          }
        }
      }
    }
  });
}

function closeModal() {
  document.getElementById('chart-modal').classList.remove('open');
}

function modalBgClick(e) {
  if (e.target.id === 'chart-modal') closeModal();
}

// ── Init ──────────────────────────────────────────────────────────────────────
loadScan();
setInterval(loadScan, 30000);

// ── Stage Gate quick-add buttons ─────────────────────────────────────────────
(function() {
  const s = document.createElement('style');
  s.textContent = `
    .it-add-btn { padding: 2px 6px; font-size: 10px; border-radius: 4px; cursor: pointer; margin-left: 3px; }
    .it-add-s1 { border: 1px solid #8b949e; color: #8b949e; background: transparent; }
    .it-add-s1:hover { background: rgba(139,148,158,0.15); }
    .it-add-s2 { border: 1px solid #58a6ff; color: #58a6ff; background: transparent; }
    .it-add-s2:hover { background: rgba(88,166,255,0.15); }
  `;
  document.head.appendChild(s);
})();

async function itAddToStage(ticker, stage) {
  try {
    const resp = await fetch('/api/stagegate/add', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ticker: ticker, stage: stage}),
    }).then(r => r.json());
    const btn = event.currentTarget;
    const orig = btn.textContent;
    btn.textContent = (resp && resp.status === 'already_exists') ? '\\u2713' : '\\u2713 Added';
    btn.disabled = true;
    setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 2000);
  } catch(e) {
    console.error('itAddToStage error:', e);
  }
}
"""


@app.get("/itool.js")
def itool_js():
    from fastapi.responses import Response
    return Response(
        content=ITOOL_JS,
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )




_itool_scanning = False


def _trigger_itool_scan():
    global _itool_scanning
    if _itool_scanning:
        return
    def _run():
        global _itool_scanning
        _itool_scanning = True
        try:
            from monitor.itool import get_scan
            get_scan(force_refresh=True)
        except Exception as e:
            _logger.warning(f"[ITOOL] Background scan failed: {e}")
        finally:
            _itool_scanning = False
    threading.Thread(target=_run, daemon=True).start()


@app.get("/api/itool")
def api_itool():
    """Return cached I-Tool scan immediately — never blocks."""
    try:
        from monitor.itool import IToolScanner
        scanner = IToolScanner()
        cached = scanner.load_cached()
        if cached:
            cached["_scanning"] = _itool_scanning
            return cached
        _trigger_itool_scan()
        return {"_scanning": True, "results": [], "counts": {}, "generated_at": None}
    except Exception as e:
        return JSONResponse(status_code=503, content={"error": str(e)})


@app.post("/api/itool/refresh")
def api_itool_refresh():
    if _itool_scanning:
        return {"status": "already_scanning"}
    _trigger_itool_scan()
    return {"status": "started"}


@app.get("/i-tool", response_class=HTMLResponse)
def itool_page():
    from fastapi.responses import HTMLResponse as HR
    html = ITOOL_HTML
    html = html.replace('</style>', _NAV_CSS + '</style>', 1)
    html = html.replace('<a href="/" class="back-btn">&#8592; Dashboard</a>', _nav_html('itool'), 1)
    # Sticky wrapper + tape arrow fix (I-Tool has its own tape, not sh-tape)
    sticky_js = ('<script>(function(){var h=document.querySelector("header");'
                 'if(!h||h.closest(".sticky-banner"))return;'
                 'var t=h.nextElementSibling;var isTape=t&&t.className&&t.className.indexOf("tape")>=0;'
                 'var w=document.createElement("div");w.className="sticky-banner";'
                 'h.parentNode.insertBefore(w,h);w.appendChild(h);if(isTape)w.appendChild(t);})();</script>')
    html = html.replace('</body>', sticky_js + '\n</body>', 1)
    return HR(content=html, headers={"Cache-Control": "no-store"})



# ── Paper Trading Dashboard ───────────────────────────────────────────────────


def _get_paper_executor(model: str = "standard"):
    from paper.executor import PaperExecutor, PAPER_MODEL_CONFIGS
    from models.database import init_db as _init_db
    _, _Session = _init_db(config.database.url, echo=False)
    cfg = PAPER_MODEL_CONFIGS.get(model, PAPER_MODEL_CONFIGS["standard"])
    return PaperExecutor(main_db_session_factory=_Session, db_path=cfg["db"],
                         stagegate_file=cfg["stagegate"])


PAPER_HTML = '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="UTF-8">\n<meta name="viewport" content="width=device-width, initial-scale=1.0">\n<title>Paper Trading \\u2014 NWO</title>\n<style>\n  * { box-sizing: border-box; margin: 0; padding: 0; }\n  body { background: #0d1117; color: #e6edf3; font-family: \'Segoe UI\', monospace; font-size: 14px; }\n  header { background: #161b22; padding: 12px 20px; border-bottom: 1px solid #30363d;\n           display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }\n  .back-btn { padding: 5px 12px; border-radius: 6px; border: 1px solid #30363d;\n              background: #21262d; color: #8b949e; text-decoration: none; font-size: 12px; }\n  header h1 { font-size: 17px; letter-spacing: 1px; color: #58a6ff; }\n  .header-right { margin-left: auto; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }\n  .run-btn   { padding: 6px 16px; border-radius: 6px; border: 1px solid #3fb950;\n               background: #1a4731; color: #3fb950; cursor: pointer; font-size: 12px; font-weight: 600; }\n  .run-btn:hover { background: #1e5c3a; }\n  .run-btn:disabled { opacity: 0.5; cursor: not-allowed; }\n  .reset-btn { padding: 5px 14px; border-radius: 6px; border: 1px solid #f85149;\n               background: transparent; color: #f85149; cursor: pointer; font-size: 12px; }\n  .reset-btn:hover { background: rgba(248,81,73,0.1); }\n  .refresh-btn { padding: 5px 14px; border-radius: 6px; border: 1px solid #30363d;\n                 background: #21262d; color: #8b949e; cursor: pointer; font-size: 12px; }\n  #run-status { font-size: 11px; color: #d29922; }\n  main { padding: 16px; display: grid; gap: 16px; }\n  /* Summary cards */\n  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }\n  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px; }\n  .card-label { font-size: 10px; color: #8b949e; letter-spacing: 1px; text-transform: uppercase; margin-bottom: 6px; }\n  .card-value { font-size: 22px; font-weight: 700; }\n  .card-sub { font-size: 11px; color: #8b949e; margin-top: 4px; }\n  .up { color: #3fb950; } .dn { color: #f85149; } .neu { color: #8b949e; }\n  /* Swim lanes */\n  .swim-wrap { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; }\n  .lane { background: #161b22; border: 1px solid #30363d; border-radius: 8px; overflow: hidden; min-width: 0; }\n  .lane-header { padding: 10px 12px; font-size: 11px; font-weight: 700; letter-spacing: 0.5px;\n                 text-transform: uppercase; border-bottom: 1px solid #21262d; }\n  .lane-0 .lane-header { color: #58a6ff; border-top: 3px solid #58a6ff; }\n  .lane-1 .lane-header { color: #d29922; border-top: 3px solid #d29922; }\n  .lane-2 .lane-header { color: #3fb950; border-top: 3px solid #3fb950; }\n  .lane-sub { font-size: 10px; color: #8b949e; font-weight: 400; margin-top: 2px; }\n  .lane-body { padding: 8px; display: flex; flex-direction: column; gap: 6px; min-height: 80px; }\n  .signal-card { background: #0d1117; border: 1px solid #21262d; border-radius: 6px;\n                 padding: 8px 10px; font-size: 12px; }\n  .sig-ticker { font-weight: 700; font-size: 13px; }\n  .sig-bull { color: #3fb950; } .sig-bear { color: #f85149; }\n  .sig-meta { font-size: 10px; color: #8b949e; margin-top: 3px; display: flex; gap: 8px; flex-wrap: wrap; }\n  .lane-empty { color: #8b949e; font-size: 12px; font-style: italic; padding: 12px; text-align: center; }\n  /* Tables */\n  .section { background: #161b22; border: 1px solid #30363d; border-radius: 8px; overflow: hidden; }\n  .section-title { padding: 10px 14px; font-size: 12px; font-weight: 600; letter-spacing: 1px;\n                   color: #8b949e; border-bottom: 1px solid #21262d; text-transform: uppercase; }\n  table { width: 100%; border-collapse: collapse; }\n  th { padding: 8px 12px; text-align: left; font-size: 11px; color: #8b949e;\n       font-weight: 600; letter-spacing: 0.5px; border-bottom: 1px solid #21262d; }\n  td { padding: 8px 12px; font-size: 13px; border-bottom: 1px solid #161b22; }\n  tr:last-child td { border-bottom: none; }\n  tr:hover td { background: #1c2128; }\n  .empty { color: #8b949e; font-style: italic; padding: 20px; text-align: center; display: block; }\n  /* Side-by-side layout for positions + trade history */\n  .side-by-side { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }\n  .side-by-side .section { overflow: hidden; }\n  .side-by-side #positions-wrap, .side-by-side #trades-wrap { overflow-x: auto; }\n  .side-by-side table { min-width: 480px; font-size: 12px; }\n  .side-by-side th, .side-by-side td { padding: 6px 8px; }\n  @media (max-width: 900px) { .side-by-side { grid-template-columns: 1fr; } }\n  @media (max-width: 800px) {\n    .swim-wrap { grid-template-columns: 1fr; }\n    .cards { grid-template-columns: 1fr 1fr; }\n    th:nth-child(n+5), td:nth-child(n+5) { display: none; }\n  }\n</style>\n</head>\n<body>\n<header>\n  <a href="/" class="back-btn">&#8592; Dashboard</a>\n  <h1>&#127918; Paper Trading</h1>\n  <div class="header-right">\n    <span id="run-status"></span>\n    <button class="run-btn" id="run-btn" onclick="runNow()">&#9654; Run Now</button>\n    <button class="refresh-btn" onclick="load()">&#8635; Refresh</button>\n    <button class="reset-btn" onclick="resetAccount()">&#x21BA; Reset</button>\n  </div>\n</header>\n<div id="sched-bar" style="background:#0d1117;border-bottom:1px solid #21262d;padding:4px 20px;font-size:11px;color:#8b949e;display:flex;gap:16px;flex-wrap:wrap;">\n  <span id="sched-mode">&#9711; Auto: loading...</span>\n  <span id="sched-last"></span>\n  <span id="sched-next"></span>\n  <span id="sched-stops"></span>\n  <button id="sched-toggle" onclick="toggleScheduler()" style="margin-left:auto;background:none;border:1px solid #30363d;color:#8b949e;font-size:10px;padding:2px 8px;border-radius:4px;cursor:pointer">Pause</button>\n</div>\n<main>\n  <!-- Summary cards -->\n  <div class="cards">\n    <div class="card"><div class="card-label">Total Equity</div><div class="card-value" id="c-equity">\\u2014</div><div class="card-sub">Starting: $100,000</div></div>\n    <div class="card"><div class="card-label">Cash</div><div class="card-value" id="c-cash">\\u2014</div><div class="card-sub" id="c-cash-sub">&nbsp;</div></div>\n    <div class="card"><div class="card-label">Invested</div><div class="card-value" id="c-invested">\\u2014</div><div class="card-sub" id="c-invested-sub">&nbsp;</div></div>\n    <div class="card"><div class="card-label">Total P&amp;L</div><div class="card-value" id="c-pnl">\\u2014</div><div class="card-sub" id="c-pnl-sub">&nbsp;</div></div>\n    <div class="card"><div class="card-label">Daily P&amp;L</div><div class="card-value" id="c-daily-pnl">\\u2014</div><div class="card-sub" id="c-daily-pnl-sub">&nbsp;</div></div>\n  </div>\n  <!-- Three model swim lanes -->\n  <div class="swim-wrap" id="swim-wrap">\n    <div class="lane lane-0"><div class="lane-header">&#9899; Standard<div class="lane-sub">Conf &gt;50% &middot; MoS &gt;15% &middot; FUD &gt;0.60</div></div><div class="lane-body" id="lane-0"><span class="lane-empty">Loading...</span></div></div>\n    <div class="lane lane-1"><div class="lane-header">&#9898; Relaxed \\u221225%<div class="lane-sub">Conf &gt;37.5% &middot; MoS &gt;11.25% &middot; FUD &gt;0.45</div></div><div class="lane-body" id="lane-1"><span class="lane-empty">Loading...</span></div></div>\n    <div class="lane lane-2"><div class="lane-header">&#9711; Relaxed \\u221250%<div class="lane-sub">Conf &gt;25% &middot; MoS &gt;7.5% &middot; FUD &gt;0.30</div></div><div class="lane-body" id="lane-2"><span class="lane-empty">Loading...</span></div></div>\n  </div>\n  <!-- Open Positions + Trade History side by side -->\n  <div class="side-by-side">\n    <div class="section">\n      <div class="section-title">Open Positions</div>\n      <div id="positions-wrap"><span class="empty">Loading...</span></div>\n    </div>\n    <div class="section">\n      <div class="section-title">Trade History</div>\n      <div id="trades-wrap"><span class="empty">Loading...</span></div>\n    </div>\n  </div>\n</main>\n<script src="/paper.js"></script>\n</body>\n</html>'

PAPER_JS = '\n// ── Model thresholds (mirror main dashboard) ─────────────────────────────────\nconst MODELS = [\n  { name: \'Standard\',    conf: 0.50,  mos: 0.15,   fud: 0.60 },\n  { name: \'Relaxed -25%\', conf: 0.375, mos: 0.1125, fud: 0.45 },\n  { name: \'Relaxed -50%\', conf: 0.25,  mos: 0.075,  fud: 0.30 },\n];\n\n// ── Formatters ────────────────────────────────────────────────────────────────\nfunction fmt(n, d) {\n  if (d === undefined) d = 2;\n  if (n == null) return \'\\u2014\';\n  return \'$\' + Math.abs(n).toLocaleString(\'en-US\', {minimumFractionDigits: d, maximumFractionDigits: d});\n}\nfunction fmtPct(n) { return n == null ? \'\' : (n >= 0 ? \'+\' : \'\') + n.toFixed(2) + \'%\'; }\nfunction cls(n)    { return n > 0 ? \'up\' : n < 0 ? \'dn\' : \'neu\'; }\n\n// ── Account + trades ──────────────────────────────────────────────────────────\nasync function load() {\n  try {\n    const [acct, trades] = await Promise.all([\n      fetch(\'/api/paper/account?model=\' + (window._PAPER_MODEL||\'standard\')).then(r => r.json()),\n      fetch(\'/api/paper/trades?model=\' + (window._PAPER_MODEL||\'standard\')).then(r => r.json()),\n    ]);\n\n    document.getElementById(\'c-equity\').textContent = fmt(acct.total_equity, 0);\n    document.getElementById(\'c-cash\').textContent = fmt(acct.cash, 0);\n    document.getElementById(\'c-cash-sub\').textContent =\n      ((acct.cash / acct.total_equity) * 100).toFixed(1) + \'% of portfolio\';\n    document.getElementById(\'c-invested\').textContent = fmt(acct.positions_value, 0);\n    document.getElementById(\'c-invested-sub\').textContent =\n      ((acct.positions_value / acct.total_equity) * 100).toFixed(1) + \'% of portfolio\';\n\n    const pnlEl = document.getElementById(\'c-pnl\');\n    pnlEl.textContent = (acct.total_pnl >= 0 ? \'+\' : \'\') + fmt(acct.total_pnl, 0);\n    pnlEl.className = \'card-value \' + cls(acct.total_pnl);\n    document.getElementById(\'c-pnl-sub\').innerHTML =\n      \'<span class="\' + cls(acct.total_pnl_pct) + \'">\' + fmtPct(acct.total_pnl_pct) + \'</span> on invested\';\n\n    const dailyEl = document.getElementById(\'c-daily-pnl\');\n    if (dailyEl && acct.daily_pnl != null) {\n      dailyEl.textContent = (acct.daily_pnl >= 0 ? \'+\' : \'\') + fmt(acct.daily_pnl, 0);\n      dailyEl.className = \'card-value \' + cls(acct.daily_pnl);\n      const dailySub = document.getElementById(\'c-daily-pnl-sub\');\n      if (dailySub) dailySub.innerHTML =\n        \'<span class="\' + cls(acct.daily_pnl_pct) + \'">\' + fmtPct(acct.daily_pnl_pct) + \'</span> today\';\n    }\n\n    // Positions\n    const pw = document.getElementById(\'positions-wrap\');\n    if (!acct.positions || !acct.positions.length) {\n      pw.innerHTML = \'<span class="empty">No open positions yet.</span>\';\n    } else {\n      let h = \'<table><tr><th>Ticker</th><th>Qty</th><th>Avg Cost</th><th>Price</th><th>Mkt Value</th><th>P&amp;L</th><th>%</th></tr>\';\n      for (const p of acct.positions) {\n        h += \'<tr data-ticker="\' + p.ticker + \'"><td><strong>\' + p.ticker + \'</strong></td><td>\' + p.qty + \'</td><td>\' +\n          fmt(p.avg_cost) + \'</td><td>\' + fmt(p.cur_price) + \'</td><td>\' + fmt(p.mkt_val, 0) +\n          \'</td><td class="\' + cls(p.pnl) + \'">\' + (p.pnl >= 0 ? \'+\' : \'\') + fmt(p.pnl) +\n          \'</td><td class="\' + cls(p.pnl_pct) + \'">\' + fmtPct(p.pnl_pct) + \'</td></tr>\';\n      }\n      pw.innerHTML = h + \'</table>\';\n    }\n\n    // Trades\n    const tw = document.getElementById(\'trades-wrap\');\n    if (!trades || !trades.length) {\n      tw.innerHTML = \'<span class="empty">No trades yet \\u2014 click Run Now or start python -m paper.runner</span>\';\n    } else {\n      let h = \'<table><tr><th>Time</th><th>Ticker</th><th>Action</th><th>Qty</th><th>Price</th><th>Total</th><th>Cash After</th><th>Source</th></tr>\';\n      for (const t of trades) {\n        const dt = t.timestamp\n          ? new Date(t.timestamp + \'Z\').toLocaleString([], {month:\'2-digit\',day:\'2-digit\',hour:\'2-digit\',minute:\'2-digit\'})\n          : \'\\u2014\';\n        const srcHtml = t.signal === \'MANUAL\'\n          ? \'<span style="color:#8b949e">\\ud83d\\udc64 Manual</span>\'\n          : \'<span style="color:#58a6ff" title="\' + (t.signal || \'AI\') + \'">\\ud83e\\udd16 AI</span>\';\n        h += \'<tr><td class="neu" style="font-size:11px">\' + dt + \'</td><td><strong>\' + t.ticker +\n          \'</strong></td><td class="\' + (t.action===\'BUY\'?\'up\':\'dn\') + \'">\' + t.action +\n          \'</td><td>\' + t.qty + \'</td><td>\' + fmt(t.price) + \'</td><td>\' + fmt(t.total, 0) +\n          \'</td><td class="neu">\' + fmt(t.cash_after, 0) + \'</td><td style="font-size:11px">\' + srcHtml + \'</td></tr>\';\n      }\n      tw.innerHTML = h + \'</table>\';\n    }\n  } catch(e) { console.error(\'Paper load error:\', e); }\n}\n\n// ── Swim lanes ────────────────────────────────────────────────────────────────\nasync function loadSwimLanes() {\n  try {\n    const signals = await fetch(\'/api/signals\').then(r => r.json());\n    if (!signals || !signals.length) {\n      for (let i = 0; i < 3; i++)\n        document.getElementById(\'lane-\' + i).innerHTML =\n          \'<span class="lane-empty">No signals yet.</span>\';\n      return;\n    }\n\n    MODELS.forEach((m, idx) => {\n      // Tickers that pass this model\'s gates\n      const passing = signals.filter(s => {\n        const conf = s.confidence || 0;\n        const mos  = s.margin_of_safety || 0;\n        const fud  = s.fud_score || 0;\n        const sig  = (s.signal || \'\').toUpperCase();\n        return conf >= m.conf && mos >= m.mos && fud >= m.fud\n               && (sig === \'BUY\' || sig === \'STRONG_BUY\');\n      });\n\n      const el = document.getElementById(\'lane-\' + idx);\n      if (!passing.length) {\n        el.innerHTML = \'<span class="lane-empty">No tickers clear this threshold.</span>\';\n        return;\n      }\n\n      // Sort by confidence desc\n      passing.sort((a, b) => (b.confidence || 0) - (a.confidence || 0));\n\n      el.innerHTML = passing.map(s => {\n        const conf   = ((s.confidence || 0) * 100).toFixed(0);\n        const mos    = ((s.margin_of_safety || 0) * 100).toFixed(1);\n        const fud    = (s.fud_score || 0).toFixed(2);\n        const price  = s.current_price ? \'$\' + s.current_price.toFixed(2) : \'\';\n        const chg    = s.change_pct != null\n          ? \'<span class="\' + cls(s.change_pct) + \'">\' + (s.change_pct >= 0 ? \'+\' : \'\') + s.change_pct.toFixed(1) + \'%</span>\'\n          : \'\';\n        return \'<div class="signal-card">\' +\n          \'<div class="sig-ticker sig-bull">&#9650; \' + s.ticker +\n            (price ? \' <span class="neu" style="font-weight:400">\' + price + \'</span>\' : \'\') +\n            (chg ? \' \' + chg : \'\') +\n          \'</div>\' +\n          \'<div class="sig-meta">\' +\n            \'<span>Conf \' + conf + \'%</span>\' +\n            \'<span>MoS \' + mos + \'%</span>\' +\n            \'<span>FUD \' + fud + \'</span>\' +\n          \'</div>\' +\n        \'</div>\';\n      }).join(\'\');\n    });\n  } catch(e) { console.error(\'Swim lanes error:\', e); }\n}\n\n// ── Run Now ───────────────────────────────────────────────────────────────────\nlet _runPollTimer = null;\n\nasync function runNow() {\n  const btn = document.getElementById(\'run-btn\');\n  const status = document.getElementById(\'run-status\');\n  btn.disabled = true;\n  status.textContent = \'\\u29d7 Cycle running...\';\n\n  try {\n    const r = await fetch(\'/api/paper/run\', {method: \'POST\'}).then(r => r.json());\n    if (r.status === \'started\') {\n      status.textContent = \'\\u29d7 Running pipeline...\';\n      // Poll every 5s until done\n      _runPollTimer = setInterval(async () => {\n        const s = await fetch(\'/api/paper/run/status\').then(r => r.json());\n        if (!s.running) {\n          clearInterval(_runPollTimer);\n          btn.disabled = false;\n          status.textContent = \'\\u2713 Done \\u2014 \' + new Date().toLocaleTimeString([], {hour:\'2-digit\',minute:\'2-digit\'});\n          load();\n          loadSwimLanes();\n          if (typeof sg3Boot === \'function\') sg3Boot();\n          setTimeout(() => { status.textContent = \'\'; }, 8000);\n        }\n      }, 5000);\n    } else if (r.status === \'outside_market_hours\') {\n      status.textContent = \'\\u26a0 Outside market hours (9:30am\\u20134pm ET)\';\n      status.style.color = \'#d29922\';\n      btn.disabled = false;\n      setTimeout(() => { status.textContent = \'\'; status.style.color = \'\'; }, 6000);\n    } else {\n      status.textContent = r.status || \'Already running\';\n      btn.disabled = false;\n    }\n  } catch(e) {\n    status.textContent = \'Error: \' + e.message;\n    btn.disabled = false;\n  }\n}\n\nasync function resetAccount() {\n  if (!confirm(\'Reset paper account to $100,000? This erases all trades and positions.\')) return;\n  await fetch(\'/api/paper/reset\', {method: \'POST\'});\n  load();\n}\n\n// ── Init ──────────────────────────────────────────────────────────────────────\nload();\nloadSwimLanes();\n\nasync function refreshPrices() {\n  const model = window._PAPER_MODEL || \'standard\';\n  try {\n    const prices = await fetch(\'/api/paper/live-prices?model=\' + model).then(r => r.json());\n    if (!prices || prices.error) return;\n    for (const [ticker, d] of Object.entries(prices)) {\n      const row = document.querySelector(\'#positions-wrap tr[data-ticker="\' + ticker + \'"]\');\n      if (!row) continue;\n      const tds = row.querySelectorAll(\'td\');\n      if (tds.length < 7) continue;\n      tds[3].textContent = \'$\' + d.price.toFixed(2);\n      tds[4].textContent = \'$\' + Math.abs(d.mkt_val).toLocaleString(\'en-US\',{maximumFractionDigits:0});\n      tds[5].textContent = (d.pnl >= 0 ? \'+$\' : \'-$\') + Math.abs(d.pnl).toFixed(2);\n      tds[5].className = cls(d.pnl);\n      tds[6].textContent = fmtPct(d.pnl_pct);\n      tds[6].className = cls(d.pnl_pct);\n    }\n  } catch(e) { if(window._nwoErr)_nwoErr(e); }\n}\nsetInterval(refreshPrices, 10000);\n\nsetInterval(() => { load(); loadSwimLanes(); }, 30000);\n\n// ── Auto-scheduler status bar ────────────────────────────────────────────────\nlet _schedPaused = false;\n\nasync function loadSchedStatus() {\n  try {\n    const s = await fetch(\'/api/paper/scheduler\').then(r => r.json());\n    _schedPaused = s.paused;\n    const modeEl   = document.getElementById(\'sched-mode\');\n    const lastEl   = document.getElementById(\'sched-last\');\n    const nextEl   = document.getElementById(\'sched-next\');\n    const stopsEl  = document.getElementById(\'sched-stops\');\n    const toggleEl = document.getElementById(\'sched-toggle\');\n    if (!modeEl) return;\n\n    if (s.paused) {\n      modeEl.innerHTML = \'&#9899; Auto: <span style="color:#f85149">PAUSED</span>\';\n      if (toggleEl) { toggleEl.textContent = \'Resume\'; toggleEl.style.color = \'#3fb950\'; }\n    } else if (!s.market_hours) {\n      modeEl.innerHTML = \'&#9711; Auto: market closed (runs 9:30\u20134pm ET Mon\u2013Fri)\';\n      if (toggleEl) { toggleEl.textContent = \'Pause\'; toggleEl.style.color = \'#8b949e\'; }\n    } else if (s.running) {\n      modeEl.innerHTML = \'&#9899; Auto: <span style="color:#d29922">cycle running\u2026</span>\';\n    } else {\n      modeEl.innerHTML = \'&#9898; Auto: <span style="color:#3fb950">active</span> \u00b7 every 5 min\';\n      if (toggleEl) { toggleEl.textContent = \'Pause\'; toggleEl.style.color = \'#8b949e\'; }\n    }\n    lastEl.textContent  = s.last_cycle  ? \'Last: \' + s.last_cycle  : \'\';\n    nextEl.textContent  = s.next_cycle  ? \'Next: \' + s.next_cycle  : \'\';\n    stopsEl.textContent = s.stop_exits  ? \'Stop exits: \' + s.stop_exits : \'\';\n  } catch(e) { if(window._nwoErr)_nwoErr(e); }\n}\n\nasync function toggleScheduler() {\n  const action = _schedPaused ? \'resume\' : \'pause\';\n  await fetch(\'/api/paper/scheduler/\' + action, {method: \'POST\'});\n  loadSchedStatus();\n}\n\nloadSchedStatus();\nsetInterval(loadSchedStatus, 15000);\n'


@app.get("/paper", response_class=HTMLResponse)
def paper_page():
    from fastapi.responses import HTMLResponse as HR
    return HR(content=PAPER_HTML, headers={"Cache-Control": "no-store"})


@app.get("/paper.js")
def paper_js_route():
    from fastapi.responses import Response
    return Response(content=PAPER_JS, media_type="application/javascript; charset=utf-8",
                    headers={"Cache-Control": "no-store"})


def _paper_model_page(model_key: str, label: str, nav_key: str = 'paper'):
    from fastapi.responses import HTMLResponse as HR
    html = PAPER_HTML
    # Update title and heading for this model variant
    html = html.replace('Paper Trading \u2014 NWO', f'Paper {label} \u2014 NWO', 1)
    html = html.replace('&#127918; Paper Trading', f'&#127918; Paper {label}', 1)
    # Swap nav highlight if this page needs a different active key
    if nav_key != 'paper':
        html = html.replace(_nav_html('paper'), _nav_html(nav_key), 1)
    # Inject model variable before paper.js loads
    html = html.replace(
        '<script src="/paper.js"></script>',
        f'<script>window._PAPER_MODEL = {repr(model_key)};</script><script src="/paper.js"></script>',
        1
    )
    return HR(content=html, headers={"Cache-Control": "no-store"})


@app.get("/paper/relaxed", response_class=HTMLResponse)
def paper_relaxed_page():
    return _paper_model_page("relaxed", "Relaxed \u221225%")


@app.get("/paper/very-relaxed", response_class=HTMLResponse)
def paper_very_relaxed_page():
    return _paper_model_page("very_relaxed", "Relaxed \u221250%")


@app.get("/paper/claude", response_class=HTMLResponse)
def paper_claude_page():
    return _paper_model_page("claude", "Claude \U0001F916")


_R2000_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>R2000 Watchlist \u2014 NWO</title>
<style>
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:#0d1117; color:#e6edf3; font-family:'Segoe UI',monospace; font-size:14px; }
  a { color:inherit; text-decoration:none; }
  .nav-bar { background:#161b22; border-bottom:1px solid #30363d; padding:8px 14px;
             display:flex; align-items:center; gap:6px; flex-wrap:wrap;
             position:sticky; top:0; z-index:200; }
  /* Model tabs */
  .model-tabs { background:#161b22; border-bottom:1px solid #30363d; padding:0 14px;
                display:flex; align-items:center; gap:0; overflow-x:auto; }
  .mtab { padding:10px 16px; font-size:12px; font-weight:600; color:#8b949e; cursor:pointer;
          border-bottom:2px solid transparent; white-space:nowrap; background:none; border-top:none;
          border-left:none; border-right:none; letter-spacing:.3px; transition:color .15s; }
  .mtab:hover { color:#e6edf3; }
  .mtab.active { color:#58a6ff; border-bottom-color:#58a6ff; }
  /* Page header */
  .page-hd { background:#0d1117; padding:10px 16px; border-bottom:1px solid #21262d;
             display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  .page-title { font-size:15px; font-weight:700; }
  .page-sub { font-size:11px; color:#8b949e; }
  .scan-note { font-size:10px; color:#484f58; margin-left:4px; }
  .refresh-btn { margin-left:auto; padding:4px 12px; border-radius:5px; border:1px solid #30363d;
                 background:#21262d; color:#8b949e; cursor:pointer; font-size:12px; }
  .refresh-btn:hover { color:#e6edf3; border-color:#8b949e; }
  main { padding:14px; display:grid; gap:10px; }
  .section { background:#161b22; border:1px solid #30363d; border-radius:8px; overflow:hidden; }
  .section-hd { padding:9px 14px; font-size:11px; font-weight:700; letter-spacing:1px;
                color:#8b949e; border-bottom:1px solid #21262d; text-transform:uppercase;
                display:flex; align-items:center; gap:8px; }
  .section-hd.s3-hd { border-top:3px solid #3fb950; }
  .section-hd.s2-hd { border-top:3px solid #58a6ff; }
  .section-hd.s1-hd { border-top:3px solid #484f58; }
  .badge { font-size:10px; font-weight:400; letter-spacing:0; }
  .badge-green { color:#3fb950; } .badge-blue { color:#58a6ff; } .badge-grey { color:#8b949e; }
  /* Chip grids */
  .chip-grid { display:flex; flex-wrap:wrap; gap:6px; padding:10px 14px 12px; }
  .chip { border-radius:5px; padding:5px 11px; font-size:12px; font-weight:700; cursor:default; }
  .chip-s3 { background:#1a2e1a; color:#3fb950; border:1px solid #2ea043; }
  .chip-s2 { background:#0d1f38; color:#58a6ff; border:1px solid #1f6feb; }
  .chip-s1 { background:#0d1117; color:#8b949e; border:1px solid #21262d; }
  .chip-sub { font-size:9px; font-weight:400; margin-left:4px; opacity:.8; }
  .empty-note { font-size:12px; color:#484f58; font-style:italic; padding:10px 14px 12px; }
  /* Legend */
  .legend { display:flex; gap:14px; flex-wrap:wrap; padding:0 14px 10px; }
  .leg { font-size:10px; color:#8b949e; display:flex; align-items:center; gap:4px; }
  .leg-dot { width:7px; height:7px; border-radius:50%; display:inline-block; }
  /* Compare grid */
  .cmp-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:0; }
  .cmp-col { border-right:1px solid #21262d; padding:10px 12px; }
  .cmp-col:last-child { border-right:none; }
  .cmp-title { font-size:11px; font-weight:700; color:#8b949e; margin-bottom:8px;
               text-transform:uppercase; letter-spacing:.5px; }
  .cmp-section { margin-bottom:12px; }
  .cmp-label { font-size:9px; color:#484f58; text-transform:uppercase; letter-spacing:.5px;
               margin-bottom:4px; font-weight:700; }
  .cmp-chips { display:flex; flex-wrap:wrap; gap:4px; }
  .cmp-chip-s3 { background:#1a2e1a; color:#3fb950; border:1px solid #2ea043;
                 border-radius:4px; padding:2px 7px; font-size:11px; font-weight:700; }
  .cmp-chip-s2 { background:#0d1f38; color:#58a6ff; border:1px solid #1f6feb;
                 border-radius:4px; padding:2px 7px; font-size:11px; font-weight:700; }
  .cmp-none { font-size:11px; color:#484f58; font-style:italic; }
  @media(max-width:800px) { .cmp-grid { grid-template-columns:1fr 1fr; } }
  @media(max-width:500px) { .cmp-grid { grid-template-columns:1fr; } }
__NAV_CSS__</style>
</head>
<body>
<div class="nav-bar">__NAV__</div>
<!-- Model tabs -->
<div class="model-tabs">
  <button class="mtab active" data-model="standard"   onclick="setModel('standard')">&#9899; Standard</button>
  <button class="mtab"        data-model="relaxed"    onclick="setModel('relaxed')">&#9898; Relaxed &minus;25%</button>
  <button class="mtab"        data-model="very_relaxed" onclick="setModel('very_relaxed')">&#9711; Very Relaxed &minus;50%</button>
  <button class="mtab"        data-model="claude"     onclick="setModel('claude')">&#129302; Claude</button>
  <button class="mtab"        data-model="compare"    onclick="setModel('compare')">&#9776; Compare</button>
</div>
<div class="page-hd">
  <span class="page-title">&#128202; R2000 Watchlist</span>
  <span class="page-sub">Small-cap universe &mdash; scanned 3&times;/day by all 4 models</span>
  <span class="scan-note">9:00 AM &middot; 12:00 PM &middot; 3:30 PM ET</span>
  <button class="refresh-btn" onclick="loadAll()">\u27f3 Refresh</button>
</div>
<main id="main-content">
  <!-- Stage 3 -->
  <div class="section" id="s3-section">
    <div class="section-hd s3-hd">&#128200; Stage 3 &mdash; Held Positions
      <span class="badge badge-green" id="s3-badge"></span>
    </div>
    <div id="s3-body"><div class="empty-note">Loading\u2026</div></div>
  </div>
  <!-- Stage 2 -->
  <div class="section" id="s2-section">
    <div class="section-hd s2-hd">&#129302; Stage 2 &mdash; Active AI Pipeline
      <span class="badge badge-blue" id="s2-badge"></span>
    </div>
    <div id="s2-body"><div class="empty-note">Loading\u2026</div></div>
  </div>
  <!-- Monitoring Universe -->
  <div class="section" id="s1-section">
    <div class="section-hd s1-hd">&#127758; Monitoring Universe
      <span class="badge badge-grey" id="s1-badge"></span>
    </div>
    <div class="legend">
      <span class="leg"><span class="leg-dot" style="background:#484f58;border:1px solid #30363d;"></span>Monitoring</span>
      <span class="leg"><span class="leg-dot" style="background:#0d1f38;border:1px solid #1f6feb;"></span>In pipeline</span>
      <span class="leg"><span class="leg-dot" style="background:#1a2e1a;border:1px solid #2ea043;"></span>Held</span>
    </div>
    <div class="chip-grid" id="s1-body"><span style="color:#8b949e;font-size:12px">Loading\u2026</span></div>
  </div>
</main>
<script>
const ML = {
  standard:     'Standard',
  relaxed:      'Relaxed \u221225%',
  very_relaxed: 'Very Relaxed \u221250%',
  claude:       '&#129302; Claude',
};
let _currentModel = 'standard';
let _data = null;
let _r2TrData = {};

function setModel(m) {
  _currentModel = m;
  document.querySelectorAll('.mtab').forEach(b => b.classList.toggle('active', b.dataset.model === m));
  if (_data) render(_data);
}

async function loadAll() {
  try {
    _data = await fetch('/api/paper/r2000-universe').then(r => r.json());
    try { _r2TrData = await fetch('/api/tipranks/all').then(r => r.json()); } catch(e) { if(window._nwoErr)_nwoErr(e);else console.error('[NWO]',e); }
    render(_data);
  } catch(e) { console.error(e); }
}

function r2TrBadge(t) {
  const d = _r2TrData[t] || {};
  const ss = d.smart_score;
  if (ss == null) return '';
  const bg  = ss >= 8 ? '#1a4731' : ss >= 4 ? '#3d2b00' : '#4a1519';
  const col = ss >= 8 ? '#3fb950' : ss >= 4 ? '#d29922' : '#f85149';
  return ' <span style="background:' + bg + ';color:' + col + ';border:1px solid ' + col + ';font-size:9px;padding:1px 4px;border-radius:3px;font-weight:700">&#9733;' + ss + '</span>';
}

function render(d) {
  const ms2 = d.model_stage2 || {};
  const ms3 = d.model_stage3 || {};
  const universe = d.universe || [];

  if (_currentModel === 'compare') {
    renderCompare(d);
    return;
  }

  const s2 = ms2[_currentModel] || [];
  const s3 = ms3[_currentModel] || [];
  const s2set = new Set(s2);
  const s3set = new Set(s3);

  // Stage 3
  document.getElementById('s3-badge').textContent = s3.length ? '(' + s3.length + ')' : '';
  document.getElementById('s3-body').innerHTML = s3.length
    ? '<div class="chip-grid">' + s3.map(t => '<div class="chip chip-s3">' + t + r2TrBadge(t) + '</div>').join('') + '</div>'
    : '<div class="empty-note">No R2000 positions held by ' + (ML[_currentModel]||_currentModel) + '.</div>';

  // Stage 2
  document.getElementById('s2-badge').textContent = s2.length ? '(' + s2.length + ')' : '';
  document.getElementById('s2-body').innerHTML = s2.length
    ? '<div class="chip-grid">' + s2.map(t => '<div class="chip chip-s2">' + t + r2TrBadge(t) + '</div>').join('') + '</div>'
    : '<div class="empty-note">No R2000 tickers in active AI pipeline for ' + (ML[_currentModel]||_currentModel) + '.</div>';

  // Universe
  const monCount = universe.filter(t => !s2set.has(t) && !s3set.has(t)).length;
  document.getElementById('s1-badge').textContent = '(' + universe.length + ' tickers \u00b7 ' + monCount + ' monitoring)';
  document.getElementById('s1-body').innerHTML = universe.map(t => {
    if (s3set.has(t)) return '<div class="chip chip-s3" style="font-size:11px;padding:3px 7px" title="Held">' + t + r2TrBadge(t) + '</div>';
    if (s2set.has(t)) return '<div class="chip chip-s2" style="font-size:11px;padding:3px 7px" title="Active pipeline">' + t + r2TrBadge(t) + '</div>';
    return '<div class="chip chip-s1" style="font-size:11px;padding:3px 7px">' + t + r2TrBadge(t) + '</div>';
  }).join('');

  // Show normal sections
  ['s3-section','s2-section','s1-section'].forEach(id => {
    document.getElementById(id).style.display = '';
  });
  const cmp = document.getElementById('compare-section');
  if (cmp) cmp.remove();
}

function renderCompare(d) {
  const ms2 = d.model_stage2 || {};
  const ms3 = d.model_stage3 || {};
  const universe = d.universe || [];
  const models = ['standard','relaxed','very_relaxed','claude'];

  // Hide per-model sections, show compare
  ['s3-section','s2-section','s1-section'].forEach(id => {
    document.getElementById(id).style.display = 'none';
  });

  let existing = document.getElementById('compare-section');
  if (!existing) {
    existing = document.createElement('div');
    existing.id = 'compare-section';
    existing.className = 'section';
    document.getElementById('main-content').appendChild(existing);
  }

  const cols = models.map(m => {
    const s2 = (ms2[m] || []);
    const s3 = (ms3[m] || []);
    const s2html = s2.length
      ? s2.map(t => '<div class="cmp-chip-s2">' + t + r2TrBadge(t) + '</div>').join('')
      : '<span class="cmp-none">None</span>';
    const s3html = s3.length
      ? s3.map(t => '<div class="cmp-chip-s3">' + t + r2TrBadge(t) + '</div>').join('')
      : '<span class="cmp-none">None</span>';
    return '<div class="cmp-col">'
      + '<div class="cmp-title">' + (ML[m]||m) + '</div>'
      + '<div class="cmp-section"><div class="cmp-label">&#128200; Stage 3 &mdash; Held (' + s3.length + ')</div>'
      + '<div class="cmp-chips">' + s3html + '</div></div>'
      + '<div class="cmp-section"><div class="cmp-label">&#129302; Stage 2 &mdash; Pipeline (' + s2.length + ')</div>'
      + '<div class="cmp-chips">' + s2html + '</div></div>'
      + '</div>';
  }).join('');

  existing.innerHTML = '<div class="section-hd">&#9776; All Models &mdash; R2000 Activity</div>'
    + '<div class="cmp-grid">' + cols + '</div>';
}

loadAll();
setInterval(loadAll, 60000);
</script>
</body>
</html>"""


@app.get("/paper/russell2000", response_class=HTMLResponse)
def paper_russell2000_page():
    from fastapi.responses import HTMLResponse as HR
    html = _R2000_PAGE_HTML
    html = html.replace('__NAV_CSS__', _NAV_CSS, 1)
    html = html.replace('__NAV__', _nav_html('r2000'), 1)
    return HR(content=html, headers={"Cache-Control": "no-store"})


_COMPARE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Paper Compare &mdash; NWO</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d1117; color: #e6edf3; font-family: 'Segoe UI', monospace; font-size: 14px; }
  header { background: #161b22; padding: 12px 20px; border-bottom: 1px solid #30363d;
           display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .back-btn { padding: 5px 12px; border-radius: 6px; border: 1px solid #30363d;
              background: #21262d; color: #8b949e; text-decoration: none; font-size: 12px; }
  header h1 { font-size: 17px; letter-spacing: 1px; color: #58a6ff; }
  .header-right { margin-left: auto; display: flex; gap: 8px; align-items: center; }
  .refresh-btn { padding: 5px 14px; border-radius: 6px; border: 1px solid #30363d;
                 background: #21262d; color: #8b949e; cursor: pointer; font-size: 12px; }
  main { padding: 16px; display: flex; flex-direction: column; gap: 20px; }
  .model-cols { display: grid; grid-template-columns: 1fr 1fr 1fr 1fr; gap: 16px; }
  .model-col  { background: #161b22; border: 1px solid #30363d; border-radius: 8px; overflow: hidden; }
  .col-head   { padding: 12px 16px; border-bottom: 1px solid #21262d; display: flex; align-items: center; gap: 10px; }
  .col-standard   .col-head { border-top: 3px solid #8b949e; }
  .col-relaxed    .col-head { border-top: 3px solid #d29922; }
  .col-very-relaxed .col-head { border-top: 3px solid #3fb950; }
  .col-claude     .col-head { border-top: 3px solid #58a6ff; }
  .col-title  { font-size: 13px; font-weight: 700; }
  .col-standard .col-title     { color: #8b949e; }
  .col-relaxed .col-title      { color: #d29922; }
  .col-very-relaxed .col-title { color: #3fb950; }
  .col-claude .col-title       { color: #58a6ff; }
  .col-sub    { font-size: 10px; color: #8b949e; margin-top: 2px; }
  .col-link   { margin-left: auto; font-size: 11px; color: #58a6ff; text-decoration: none; }
  .col-link:hover { text-decoration: underline; }
  .col-body   { padding: 14px 16px; display: flex; flex-direction: column; gap: 10px; }
  .stat-row   { display: flex; justify-content: space-between; align-items: baseline; }
  .stat-label { font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }
  .stat-val   { font-size: 15px; font-weight: 700; }
  .up { color: #3fb950; } .dn { color: #f85149; } .neu { color: #8b949e; }
  .divider    { border: none; border-top: 1px solid #21262d; margin: 4px 0; }
  .pos-mini   { font-size: 11px; color: #c9d1d9; }
  .pos-mini td { padding: 3px 6px; }
  .pos-mini td:first-child { color: #8b949e; font-size: 10px; }
  .empty-note { font-size: 12px; color: #8b949e; font-style: italic; text-align: center; padding: 8px; }
  @media (max-width: 1100px) { .model-cols { grid-template-columns: 1fr 1fr; } }
  @media (max-width: 600px)  { .model-cols { grid-template-columns: 1fr; } }
  .info-toggle { background: none; border: 1px solid #30363d; border-radius: 6px; color: #8b949e;
                 padding: 5px 12px; cursor: pointer; font-size: 12px; }
  .info-toggle:hover { color: #e6edf3; border-color: #58a6ff; }
  .model-info  { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px 20px; }
  .model-info h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 1px; color: #8b949e; margin-bottom: 12px; }
  .info-grid   { display: grid; grid-template-columns: 1fr 1fr 1fr 1fr; gap: 16px; }
  .info-card   { background: #0d1117; border-radius: 6px; padding: 12px 14px; border-left: 3px solid #30363d; }
  .info-card.c-std  { border-left-color: #8b949e; }
  .info-card.c-rel  { border-left-color: #d29922; }
  .info-card.c-vrel { border-left-color: #3fb950; }
  .info-card.c-ai   { border-left-color: #58a6ff; }
  .info-name   { font-size: 12px; font-weight: 700; margin-bottom: 4px; }
  .c-std  .info-name { color: #8b949e; }
  .c-rel  .info-name { color: #d29922; }
  .c-vrel .info-name { color: #3fb950; }
  .c-ai   .info-name { color: #58a6ff; }
  .info-tag    { display: inline-block; font-size: 10px; background: #21262d; color: #8b949e;
                 border-radius: 3px; padding: 1px 5px; margin-bottom: 6px; }
  .info-desc   { font-size: 11px; color: #8b949e; line-height: 1.5; }
  .info-weights { font-size: 10px; color: #6e7681; margin-top: 6px; font-family: monospace; }
  @media (max-width: 1100px) { .info-grid { grid-template-columns: 1fr 1fr; } }
  @media (max-width: 600px)  { .info-grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<header>
  <a href="/paper" class="back-btn">&#8592; Paper Trading</a>
  <h1>&#128202; Model Comparison</h1>
  <div class="header-right">
    <a href="/paper/daily-report" class="refresh-btn" style="text-decoration:none;">&#128196; Daily Report</a>
    <button class="info-toggle" onclick="toggleInfo()" id="info-btn">&#8505; Model Info</button>
    <button class="refresh-btn" onclick="load()">&#8635; Refresh</button>
  </div>
</header>
<main>
  <div class="model-info" id="model-info" style="display:none">
    <h2>Model Philosophies</h2>
    <div class="info-grid">
      <div class="info-card c-std">
        <div class="info-name">Standard</div>
        <div class="info-tag">Threshold &times;1.00</div>
        <div class="info-desc">The baseline — full 6-layer pipeline with all gates at their designed levels. Requires strong fundamentals, solid composite score (&ge;0.10), ensemble bull probability &ge;45%, and passing Kalman/Reynolds filters. Trades only when the evidence is unambiguous. The control group in this experiment.</div>
        <div class="info-weights">Weights: fundamentals 25% &middot; momentum 20% &middot; insider 15% &middot; technical 15% &middot; supertrend 10% &middot; cycle 10% &middot; volume 5%</div>
      </div>
      <div class="info-card c-rel">
        <div class="info-name">Relaxed &minus;25%</div>
        <div class="info-tag">Threshold &times;0.75</div>
        <div class="info-desc">Same signal pipeline as Standard but all decision gates reduced by 25%. Accepts moderate conviction setups that Standard would reject. Useful for testing whether the Standard model is overfitting its thresholds to caution. Trades more frequently; higher expected variance.</div>
        <div class="info-weights">Same weights as Standard &middot; looser gates only</div>
      </div>
      <div class="info-card c-vrel">
        <div class="info-name">Very Relaxed &minus;50%</div>
        <div class="info-tag">Threshold &times;0.50</div>
        <div class="info-desc">Half the Standard thresholds. Acts more like a high-frequency trend follower — enters on weak signals and exits on stop-loss discipline. Stress-tests whether the AI pipeline produces any alpha at all when thresholds are minimal. High drawdown expected; useful as a lower bound.</div>
        <div class="info-weights">Same weights as Standard &middot; gates halved</div>
      </div>
      <div class="info-card c-ai">
        <div class="info-name">&#129302; Claude (AI Momentum)</div>
        <div class="info-tag">Threshold &times;0.85 &middot; Custom weights</div>
        <div class="info-desc">Purpose-built momentum &amp; trend model. Heavy weight on SuperTrend (35%) and momentum (30%); fundamentals deliberately de-emphasised (5%). Non-investable tickers penalised only &minus;10% max — momentum stocks like TSLA/PLTR are not disqualified by value metrics. Hard VIX gate: no trades above VIX 30; reduced size above 25. Designed to capture trending breakouts that pure value models miss.</div>
        <div class="info-weights">ST 35% &middot; Mom 30% &middot; Insider 20% &middot; Technical 10% &middot; Fundamentals 5%</div>
      </div>
    </div>
  </div>
  <div class="model-cols" id="cols">
    <div class="model-col col-standard" id="col-standard"><div class="col-head"><div><div class="col-title">Standard</div><div class="col-sub">Thresholds &times;1.00</div></div><a class="col-link" href="/paper">Open &rarr;</a></div><div class="col-body" id="body-standard"><span class="empty-note">Loading&hellip;</span></div></div>
    <div class="model-col col-relaxed"  id="col-relaxed"><div class="col-head"><div><div class="col-title">Relaxed &minus;25%</div><div class="col-sub">Thresholds &times;0.75</div></div><a class="col-link" href="/paper/relaxed">Open &rarr;</a></div><div class="col-body" id="body-relaxed"><span class="empty-note">Loading&hellip;</span></div></div>
    <div class="model-col col-very-relaxed" id="col-very-relaxed"><div class="col-head"><div><div class="col-title">Very Relaxed &minus;50%</div><div class="col-sub">Thresholds &times;0.50</div></div><a class="col-link" href="/paper/very-relaxed">Open &rarr;</a></div><div class="col-body" id="body-very-relaxed"><span class="empty-note">Loading&hellip;</span></div></div>
    <div class="model-col col-claude" id="col-claude"><div class="col-head"><div><div class="col-title">&#129302; Claude</div><div class="col-sub">ST&times;0.35 &middot; Mom&times;0.30 &middot; VIX gate</div></div><a class="col-link" href="/paper/claude">Open &rarr;</a></div><div class="col-body" id="body-claude"><span class="empty-note">Loading&hellip;</span></div></div>
  </div>
  <div style="margin-top:4px;padding:12px 16px;background:#161b22;border:1px solid #30363d;border-radius:8px;display:flex;align-items:center;gap:16px;flex-wrap:wrap;">
    <div style="font-size:12px;color:#8b949e;">Separate Universe</div>
    <a href="/paper/russell2000" style="padding:5px 14px;border-radius:6px;border:1px solid #58a6ff;color:#58a6ff;text-decoration:none;font-size:12px;">&#128202; Russell 2000 &rarr;</a>
    <span style="font-size:11px;color:#8b949e;">Curated 49-stock small-cap watchlist &middot; reviewed by all 4 models</span>
  </div>
</main>
<script>
function toggleInfo() {
  var el = document.getElementById('model-info');
  var btn = document.getElementById('info-btn');
  if (el.style.display === 'none') {
    el.style.display = 'block';
    btn.textContent = '\u2715 Hide Info';
  } else {
    el.style.display = 'none';
    btn.textContent = '\u2139 Model Info';
  }
}

const MODELS = [
  { key: 'standard',     bodyId: 'body-standard' },
  { key: 'relaxed',      bodyId: 'body-relaxed' },
  { key: 'very_relaxed', bodyId: 'body-very-relaxed' },
  { key: 'claude',       bodyId: 'body-claude' },
];

function fmt(n, d=2) {
  if (n == null) return '\u2014';
  return '$' + Math.abs(n).toLocaleString('en-US', {minimumFractionDigits: d, maximumFractionDigits: d});
}
function fmtPct(n) { return n == null ? '' : (n >= 0 ? '+' : '') + n.toFixed(2) + '%'; }
function cls(n) { return n > 0 ? 'up' : n < 0 ? 'dn' : 'neu'; }

function renderModel(m, acct) {
  const el = document.getElementById(m.bodyId);
  const pnlCls  = cls(acct.total_pnl);
  const dayCls  = cls(acct.daily_pnl);
  const lifeCls = cls(acct.lifetime_pnl);
  const invested = acct.total_invested || 0;
  let html = `
    <div class="stat-row"><span class="stat-label">Equity</span><span class="stat-val">${fmt(acct.total_equity, 0)}</span></div>
    <div class="stat-row"><span class="stat-label">Cash</span><span class="stat-val neu">${fmt(acct.cash, 0)}</span></div>
    <div class="stat-row"><span class="stat-label">Invested</span><span class="stat-val neu">${fmt(invested, 0)}</span></div>
    <div class="stat-row"><span class="stat-label">Return on Invested</span><span class="stat-val ${pnlCls}">${(acct.total_pnl||0) >= 0 ? '+' : ''}${fmt(acct.total_pnl||0, 0)} <small>(${fmtPct(acct.total_pnl_pct||0)})</small></span></div>
    <div class="stat-row"><span class="stat-label">Daily P&L</span><span class="stat-val ${dayCls}">${(acct.daily_pnl||0) >= 0 ? '+' : ''}${fmt(acct.daily_pnl||0, 0)} <small>(${fmtPct(acct.daily_pnl_pct||0)})</small></span></div>
    <div class="stat-row"><span class="stat-label">Lifetime P&L</span><span class="stat-val ${lifeCls}" style="font-size:12px">${(acct.lifetime_pnl||0) >= 0 ? '+' : ''}${fmt(acct.lifetime_pnl||0, 0)} <small>(${fmtPct(acct.lifetime_pnl_pct||0)})</small></span></div>
    <hr class="divider">
  `;
  if (acct.positions && acct.positions.length) {
    html += '<table class="pos-mini"><tr><td>Ticker</td><td>P&L</td><td>%</td></tr>';
    for (const p of acct.positions.slice(0, 8)) {
      html += `<tr><td><strong>${p.ticker}</strong></td><td class="${cls(p.pnl)}">${p.pnl >= 0 ? '+' : ''}${fmt(p.pnl)}</td><td class="${cls(p.pnl_pct)}">${fmtPct(p.pnl_pct)}</td></tr>`;
    }
    html += '</table>';
    if (acct.positions.length > 8) html += `<div class="empty-note">+${acct.positions.length - 8} more</div>`;
  } else {
    html += '<div class="empty-note">No open positions</div>';
  }
  el.innerHTML = html;
}

async function load() {
  await Promise.all(MODELS.map(async m => {
    try {
      const acct = await fetch('/api/paper/account?model=' + m.key).then(r => r.json());
      renderModel(m, acct);
    } catch(e) {
      document.getElementById(m.bodyId).innerHTML = '<span class="empty-note">Error loading</span>';
    }
  }));
}

load();
setInterval(load, 30000);
</script>
</body>
</html>"""


@app.get("/paper/compare", response_class=HTMLResponse)
def paper_compare_page():
    from fastapi.responses import HTMLResponse as HR
    return HR(content=_COMPARE_HTML, headers={"Cache-Control": "no-store"})


# ── Daily Activity Report ─────────────────────────────────────────────────────

@app.get("/api/paper/daily-activity")
def api_daily_activity(date: str = ""):
    """
    Return all paper trades across all 4 models for a given date (YYYY-MM-DD).
    Defaults to today (ET).
    """
    import json as _j
    from datetime import date as _date, timedelta
    import pytz
    ET = pytz.timezone("America/New_York")

    if date:
        try:
            target = _date.fromisoformat(date)
        except ValueError:
            return JSONResponse({"error": "Invalid date"}, status_code=400)
    else:
        target = datetime.now(ET).date()

    from paper.executor import PAPER_MODEL_CONFIGS
    from paper.account import init_paper_db, PaperTrade, PaperAccount, PaperEquitySnapshot

    result = {}
    for model_name, cfg in PAPER_MODEL_CONFIGS.items():
        try:
            _, Sess = init_paper_db(cfg["db"])
            with Sess() as s:
                # Trades on target date (UTC stored, compare by date)
                trades = (
                    s.query(PaperTrade)
                    .filter(PaperTrade.timestamp >= f"{target}T00:00:00")
                    .filter(PaperTrade.timestamp <  f"{target + timedelta(days=1)}T00:00:00")
                    .order_by(PaperTrade.timestamp)
                    .all()
                )
                acct = s.query(PaperAccount).first()
                snap = (
                    s.query(PaperEquitySnapshot)
                    .filter(PaperEquitySnapshot.snap_date == target)
                    .first()
                )
                result[model_name] = {
                    "trades": [
                        {
                            "time":      _to_et_str(t.timestamp, "%H:%M ET") if t.timestamp else "—",
                            "ticker":    t.ticker,
                            "action":    t.action,
                            "qty":       t.qty,
                            "price":     round(t.price, 2),
                            "total":     round(t.total, 2),
                            "cash_after": round(t.cash_after, 2) if t.cash_after else None,
                            "signal":    t.signal,
                            "notes":     t.notes,
                            "stop_loss": round(t.stop_loss, 2) if t.stop_loss else None,
                        }
                        for t in trades
                    ],
                    "trade_count": len(trades),
                    "buys":  sum(1 for t in trades if t.action == "BUY"),
                    "sells": sum(1 for t in trades if t.action == "SELL"),
                    "current_cash":   round(acct.cash, 2) if acct else None,
                    "eod_equity":     round(snap.total_equity, 2) if snap else None,
                }
        except Exception as e:
            result[model_name] = {"error": str(e), "trades": []}

    return {"date": str(target), "models": result}


_DAILY_REPORT_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Daily Report &mdash; NWO</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d1117; color: #e6edf3; font-family: 'Segoe UI', monospace; font-size: 13px; }
  header { background: #161b22; padding: 12px 20px; border-bottom: 1px solid #30363d;
           display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .back-btn { padding: 5px 12px; border-radius: 6px; border: 1px solid #30363d;
              background: #21262d; color: #8b949e; text-decoration: none; font-size: 12px; }
  header h1 { font-size: 17px; letter-spacing: 1px; color: #58a6ff; }
  .hdr-right { margin-left: auto; display: flex; gap: 8px; align-items: center; }
  .date-input { background: #21262d; border: 1px solid #30363d; color: #e6edf3;
                border-radius: 6px; padding: 4px 10px; font-size: 12px; }
  .btn { padding: 5px 14px; border-radius: 6px; border: 1px solid #30363d;
         background: #21262d; color: #8b949e; cursor: pointer; font-size: 12px; }
  .btn:hover { border-color: #58a6ff; color: #58a6ff; }
  main { padding: 16px; display: flex; flex-direction: column; gap: 20px; }
  .date-bar { font-size: 12px; color: #8b949e; padding: 4px 0; }
  .model-section { background: #161b22; border: 1px solid #30363d; border-radius: 8px; overflow: hidden; }
  .model-head { padding: 12px 16px; border-bottom: 1px solid #21262d;
                display: flex; align-items: center; gap: 12px; }
  .m-standard   .model-head { border-top: 3px solid #8b949e; }
  .m-relaxed    .model-head { border-top: 3px solid #d29922; }
  .m-very_relaxed .model-head { border-top: 3px solid #3fb950; }
  .m-claude     .model-head { border-top: 3px solid #58a6ff; }
  .m-title { font-size: 13px; font-weight: 700; }
  .m-standard   .m-title { color: #8b949e; }
  .m-relaxed    .m-title { color: #d29922; }
  .m-very_relaxed .m-title { color: #3fb950; }
  .m-claude     .m-title { color: #58a6ff; }
  .m-stats { display: flex; gap: 16px; margin-left: auto; }
  .m-stat { font-size: 11px; color: #8b949e; text-align: right; }
  .m-stat strong { display: block; font-size: 13px; color: #e6edf3; }
  .trade-table { width: 100%; border-collapse: collapse; }
  .trade-table th { padding: 8px 12px; text-align: left; font-size: 10px; font-weight: 700;
                    text-transform: uppercase; letter-spacing: 0.5px; color: #6e7681;
                    border-bottom: 1px solid #21262d; background: #0d1117; }
  .trade-table td { padding: 8px 12px; border-bottom: 1px solid #161b22; vertical-align: top; }
  .trade-table tr:last-child td { border-bottom: none; }
  .trade-table tr:hover td { background: rgba(88,166,255,0.04); }
  .act-buy  { color: #3fb950; font-weight: 700; }
  .act-sell { color: #f85149; font-weight: 700; }
  .notes-cell { font-size: 11px; color: #6e7681; max-width: 400px; line-height: 1.5; }
  .gate-badge { display: inline-block; font-size: 10px; padding: 1px 5px; border-radius: 3px;
                margin: 1px; background: #21262d; color: #8b949e; border: 1px solid #30363d; }
  .gate-pass { border-color: #3fb950; color: #3fb950; background: rgba(63,185,80,0.08); }
  .gate-fail { border-color: #f85149; color: #f85149; background: rgba(248,81,73,0.08); }
  .empty-note { padding: 16px; text-align: center; font-size: 12px; color: #8b949e; font-style: italic; }
  .sig-badge { display: inline-block; font-size: 10px; padding: 1px 6px; border-radius: 10px;
               font-weight: 700; background: #21262d; color: #8b949e; }
  .sig-buy  { background: rgba(63,185,80,0.15); color: #3fb950; }
  .sig-sell { background: rgba(248,81,73,0.15); color: #f85149; }
  @media (max-width: 700px) { .m-stats { display: none; } }
</style>
</head>
<body>
<header>
  <a href="/paper/compare" class="back-btn">&#8592; Compare</a>
  <h1>&#128196; Daily Activity Report</h1>
  <div class="hdr-right">
    <input type="date" id="date-pick" class="date-input">
    <button class="btn" onclick="load()">&#8635; Load</button>
  </div>
</header>
<main>
  <div class="date-bar" id="date-bar">Loading&hellip;</div>
  <div id="report-body"></div>
</main>
<script>
const MODEL_META = {
  standard:    { label: 'Standard',         cls: 'm-standard' },
  relaxed:     { label: 'Relaxed \u221225%', cls: 'm-relaxed' },
  very_relaxed:{ label: 'Very Relaxed \u221250%', cls: 'm-very_relaxed' },
  claude:      { label: '\uD83E\uDD16 Claude', cls: 'm-claude' },
};
const ORDER = ['standard','relaxed','very_relaxed','claude'];

function today() {
  const d = new Date();
  return d.toISOString().slice(0,10);
}

function fmtMoney(n) {
  if (n == null) return '\u2014';
  return '$' + Math.abs(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
}

function parseNotes(notes) {
  if (!notes) return '';
  // Separate gate results (lines with ✓/✗ or PASS/FAIL) from risk warnings
  return notes.split('\n').map(l => l.trim()).filter(Boolean)
    .map(l => '<div>' + l.replace(/</g,'&lt;').replace(/>/g,'&gt;') + '</div>')
    .join('');
}

function renderModel(key, data) {
  const meta = MODEL_META[key] || {label: key, cls: ''};
  const trades = data.trades || [];
  let html = `<div class="model-section ${meta.cls}">
    <div class="model-head">
      <span class="m-title">${meta.label}</span>
      <div class="m-stats">
        <div class="m-stat"><strong>${data.trade_count||0}</strong>Trades</div>
        <div class="m-stat"><strong class="act-buy">${data.buys||0}</strong>Buys</div>
        <div class="m-stat"><strong class="act-sell">${data.sells||0}</strong>Sells</div>
        ${data.eod_equity ? `<div class="m-stat"><strong>${fmtMoney(data.eod_equity)}</strong>EOD Equity</div>` : ''}
        ${data.current_cash != null ? `<div class="m-stat"><strong>${fmtMoney(data.current_cash)}</strong>Cash</div>` : ''}
      </div>
    </div>`;

  if (data.error) {
    html += `<div class="empty-note">Error: ${data.error}</div>`;
  } else if (!trades.length) {
    html += `<div class="empty-note">No trades on this date</div>`;
  } else {
    html += `<table class="trade-table">
      <thead><tr>
        <th>Time</th><th>Ticker</th><th>Action</th><th>Qty</th>
        <th>Price</th><th>Total</th><th>Cash After</th><th>Signal</th><th>Notes / Gates</th>
      </tr></thead><tbody>`;
    for (const t of trades) {
      const actCls = t.action === 'BUY' ? 'act-buy' : t.action === 'SELL' ? 'act-sell' : '';
      const sigCls = (t.signal||'').toLowerCase().includes('buy') ? 'sig-buy'
                   : (t.signal||'').toLowerCase().includes('sell') ? 'sig-sell' : '';
      html += `<tr>
        <td>${t.time||'—'}</td>
        <td><strong>${t.ticker}</strong></td>
        <td class="${actCls}">${t.action}</td>
        <td>${t.qty}</td>
        <td>${fmtMoney(t.price)}</td>
        <td>${fmtMoney(t.total)}</td>
        <td>${fmtMoney(t.cash_after)}</td>
        <td><span class="sig-badge ${sigCls}">${t.signal||'—'}</span></td>
        <td class="notes-cell">${parseNotes(t.notes)}</td>
      </tr>`;
    }
    html += '</tbody></table>';
  }
  html += '</div>';
  return html;
}

async function load() {
  const dp = document.getElementById('date-pick');
  const dateVal = dp.value || today();
  dp.value = dateVal;
  document.getElementById('date-bar').textContent = 'Loading ' + dateVal + '…';
  document.getElementById('report-body').innerHTML = '';
  try {
    const data = await fetch('/api/paper/daily-activity?date=' + dateVal).then(r => r.json());
    document.getElementById('date-bar').textContent =
      '\uD83D\uDCC5 ' + data.date + ' — showing all 4 models';
    let html = '';
    for (const k of ORDER) {
      if (data.models && data.models[k]) html += renderModel(k, data.models[k]);
    }
    document.getElementById('report-body').innerHTML = html || '<div class="empty-note">No data</div>';
  } catch(e) {
    document.getElementById('date-bar').textContent = 'Error: ' + e.message;
  }
}

// Default to today and auto-load
document.getElementById('date-pick').value = today();
load();
// Auto-refresh every 5 min during the session
setInterval(load, 300000);
</script>
</body>
</html>"""


@app.get("/paper/daily-report", response_class=HTMLResponse)
def paper_daily_report():
    from fastapi.responses import HTMLResponse as HR
    return HR(content=_DAILY_REPORT_HTML, headers={"Cache-Control": "no-store"})


@app.get("/api/paper/account")
def api_paper_account(model: str = "standard"):
    """Return account summary with live P&L — prices from Schwab batch quote."""
    try:
        ex = _get_paper_executor(model)
        # Build price_lookup from live cache for instant response,
        # executor will batch-fetch any missing tickers
        with _live_cache_lock:
            price_lookup = {t: v["price"] for t, v in _live_price_cache.items()}
        return ex.get_account_summary(price_lookup=price_lookup or None)
    except Exception as e:
        return JSONResponse(status_code=503, content={"error": str(e)})


@app.get("/api/prices")
def api_prices(tickers: str = ""):
    """Return {ticker: price} — live intraday price during market hours, else latest EOD close."""
    if not tickers:
        return {}
    ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    result = {}
    # Live cache first
    for ticker in ticker_list:
        live = _live_price(ticker)
        if live:
            result[ticker] = live["price"]
    # EOD fallback for any missed tickers
    remaining = [t for t in ticker_list if t not in result]
    if remaining:
        try:
            from models.database import init_db as _init_db, PriceHistory, Company
            _, _Session = _init_db(config.database.url, echo=False)
            with _Session() as s:
                rows = (
                    s.query(Company.ticker, PriceHistory.close)
                    .join(PriceHistory, PriceHistory.company_id == Company.id)
                    .filter(Company.ticker.in_(remaining))
                    .order_by(Company.ticker, PriceHistory.date.desc())
                    .all()
                )
                seen: set = set()
                for ticker, close in rows:
                    if ticker not in seen and close:
                        result[ticker] = round(float(close), 2)
                        seen.add(ticker)
        except Exception:
            pass
    return result


@app.get("/api/live-prices")
def api_live_prices():
    """Return all cached live prices. Forces a refresh."""
    _refresh_live_prices(force=True)
    with _live_cache_lock:
        return {
            t: {"price": v["price"], "change_pct": v.get("change_pct"), "prev_close": v.get("prev_close")}
            for t, v in _live_price_cache.items()
        }


@app.get("/api/paper/live-prices")
def api_paper_live_prices(model: str = "standard"):
    """Lightweight: returns {ticker: {price, pnl, pnl_pct, mkt_val}} for open positions."""
    try:
        ex = _get_paper_executor(model)
        with _live_cache_lock:
            price_lookup = {t: v["price"] for t, v in _live_price_cache.items()}
        from paper.account import PaperPosition
        with ex.Session() as s:
            positions = s.query(PaperPosition).all()
        result = {}
        for p in positions:
            if p.qty <= 0:
                continue
            cur = price_lookup.get(p.ticker) or p.avg_cost
            cost_basis = round(p.qty * p.avg_cost, 2)
            mkt_val    = round(p.qty * cur, 2)
            pnl        = round(mkt_val - cost_basis, 2)
            pnl_pct    = round((pnl / cost_basis * 100) if cost_basis else 0, 2)
            result[p.ticker] = {"price": round(cur, 2), "pnl": pnl, "pnl_pct": pnl_pct, "mkt_val": mkt_val}
        return result
    except Exception as e:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content={"error": str(e)})


@app.get("/api/paper/r2000-universe")
def api_r2000_universe():
    """Return R2000 universe + which tickers are active/held per model."""
    import json
    from pathlib import Path
    from paper.executor import PAPER_MODEL_CONFIGS
    try:
        sg_r2k = json.loads(Path("data/stagegate_russell2000.json").read_text(encoding="utf-8"))
    except Exception:
        sg_r2k = {}
    universe = sg_r2k.get("stage1", [])
    universe_set = set(universe)
    model_stage2: dict = {}
    model_stage3: dict = {}
    for model, cfg in PAPER_MODEL_CONFIGS.items():
        try:
            sg = json.loads(Path(cfg["stagegate"]).read_text(encoding="utf-8"))
            model_stage2[model] = [t for t in sg.get("stage2", []) if t in universe_set]
            model_stage3[model] = [t for t in sg.get("stage3", []) if t in universe_set]
        except Exception:
            model_stage2[model] = []
            model_stage3[model] = []
    active = list({t for lst in model_stage2.values() for t in lst})
    held   = list({t for lst in model_stage3.values() for t in lst})
    return {
        "universe":     universe,
        "active":       active,
        "held":         held,
        "model_stage2": model_stage2,
        "model_stage3": model_stage3,
    }


@app.get("/api/ai-exits")
def api_ai_exits_get():
    """Return {ticker: bool} AI exit toggle state from data/ai_exits.json."""
    import json
    from pathlib import Path
    f = Path("data/ai_exits.json")
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


@app.post("/api/ai-exits")
async def api_ai_exits_post(request: Request):
    """Update AI exit toggles. Body: {ticker: bool, ...}"""
    import json
    from pathlib import Path
    body = await request.json()
    f = Path("data/ai_exits.json")
    current = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    current.update({k: bool(v) for k, v in body.items()})
    f.write_text(json.dumps(current, indent=2), encoding="utf-8")
    return {"status": "ok"}


# ── Per-model AI exits (under /api/paper/ so fetch interceptor adds ?model=) ──
def _ai_exits_file(model: str) -> "Path":
    from pathlib import Path
    return Path("data/ai_exits.json") if model == "standard" else Path(f"data/ai_exits_{model}.json")

@app.get("/api/paper/ai-exits")
def api_paper_ai_exits_get(model: str = "standard"):
    import json
    f = _ai_exits_file(model)
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}

@app.post("/api/paper/ai-exits")
async def api_paper_ai_exits_post(request: Request, model: str = "standard"):
    import json
    body = await request.json()
    f = _ai_exits_file(model)
    current = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    current.update({k: bool(v) for k, v in body.items()})
    f.write_text(json.dumps(current, indent=2), encoding="utf-8")
    return {"status": "ok"}


# ── Per-model stagegate (under /api/paper/ so fetch interceptor adds ?model=) ──
def _model_stagegate_file(model: str) -> str:
    files = {
        "standard":    "data/stagegate.json",
        "relaxed":     "data/stagegate_relaxed.json",
        "very_relaxed":"data/stagegate_very_relaxed.json",
        "claude":      "data/stagegate_claude.json",
    }
    return files.get(model, "data/stagegate.json")

@app.get("/api/paper/stagegate")
def api_paper_stagegate_get(model: str = "standard"):
    import json
    from pathlib import Path
    std_path = Path("data/stagegate.json")
    std_sg   = json.loads(std_path.read_text(encoding="utf-8")) if std_path.exists() else {"stage1": list(config.watchlist), "stage2": [], "stage3": []}
    if model == "standard":
        return std_sg
    # Non-standard models share stage1/stage2 with standard; stage3 is model-specific
    p = Path(_model_stagegate_file(model))
    model_sg = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    return {
        "stage1": std_sg.get("stage1", []),
        "stage2": std_sg.get("stage2", []),
        "stage3": model_sg.get("stage3", []),
    }

@app.post("/api/paper/stagegate")
async def api_paper_stagegate_post(request: Request, model: str = "standard"):
    import json, threading
    from pathlib import Path
    body = await request.json()
    stage1 = body.get("stage1", [])
    stage2 = body.get("stage2", [])
    stage3 = body.get("stage3", [])

    if model == "standard":
        # Standard: full save + sync stage1/2 to all models
        existing = _load_stagegate()
        # Guard: never wipe a non-empty stage2 with an empty one (race condition / stale frontend)
        if not stage2 and existing.get("stage2"):
            stage2 = existing["stage2"]
        existing_all = set(existing.get("stage1",[]) + existing.get("stage2",[]) + existing.get("stage3",[]))
        incoming_all = set(stage1 + stage2 + stage3)
        new_tickers = [t for t in incoming_all - existing_all if t]
        _save_stagegate({"stage1": stage1, "stage2": stage2, "stage3": stage3})
        if new_tickers:
            def _bg():
                import time; time.sleep(2)
                try:
                    from paper.auto_scheduler import get_scheduler
                    s = get_scheduler()
                    if s: s.trigger_now()
                except Exception:
                    pass
            threading.Thread(target=_bg, daemon=True).start()
    else:
        # Non-standard: only persist stage3 — stage1/stage2 always come from standard
        p = Path(_model_stagegate_file(model))
        existing = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        existing["stage3"] = stage3
        p.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    return {"status": "ok"}


@app.get("/api/paper/trades")
def api_paper_trades(model: str = "standard"):
    try:
        ex = _get_paper_executor(model)
        return ex.get_recent_trades(limit=100)
    except Exception as e:
        return JSONResponse(status_code=503, content={"error": str(e)})


_benchmark_cache: dict = {}   # "YYYY-MM-DD_YYYY-MM-DD" -> result dict

def _benchmark_since_from_db() -> str:
    """Return the most recent reset_at date across all 4 paper models (or 30 days ago)."""
    try:
        import sqlite3 as _sq3
        best = None
        for db_path in ["data/paper_trading.db", "data/paper_relaxed.db",
                        "data/paper_very_relaxed.db", "data/paper_claude.db"]:
            try:
                con = _sq3.connect(db_path)
                row = con.execute("SELECT reset_at FROM paper_account LIMIT 1").fetchone()
                con.close()
                if row and row[0]:
                    d = str(row[0])[:10]   # "YYYY-MM-DD"
                    if best is None or d > best:
                        best = d
            except Exception:
                pass
        if best:
            return best
    except Exception:
        pass
    return (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")

@app.get("/api/paper/benchmarks")
def api_paper_benchmarks(since: str = ""):
    """Return S&P 500 (^GSPC) and S&P 100 (^OEX) % return since a given date.
    If since is not provided (or older than the last account reset), uses the reset date.
    If since is today, backs up to the previous trading day so yfinance has >= 2 data points."""
    try:
        db_since = _benchmark_since_from_db()
        if not since or since < db_since:
            since = db_since
        # If since is today (or a future date), back up to the last weekday before today
        # so yfinance can return at least 2 data points (open and close from prior day)
        from datetime import date as _date
        today = datetime.now(timezone.utc).date()
        since_date = _date.fromisoformat(since)
        if since_date >= today:
            since_date = today - timedelta(days=1)
            while since_date.weekday() >= 5:   # skip Saturday(5) and Sunday(6)
                since_date -= timedelta(days=1)
            since = since_date.isoformat()
    except Exception:
        since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cache_key = f"{since}_{today_str}"
    if cache_key in _benchmark_cache:
        return _benchmark_cache[cache_key]

    result: dict = {
        "since": since,
        "sp500_pct": None, "sp500_start": None, "sp500_current": None,
        "sp100_pct": None, "sp100_start": None, "sp100_current": None,
    }
    try:
        import yfinance as yf
        got_data = False
        for sym, key in [("^GSPC", "sp500"), ("^OEX", "sp100")]:
            try:
                df = yf.download(sym, start=since, interval="1d",
                                 progress=False, auto_adjust=True)
                # Flatten MultiIndex columns (newer yfinance versions)
                if hasattr(df.columns, "levels"):
                    df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
                closes = df["Close"].dropna()
                # Squeeze in case Close is still a DataFrame (multi-ticker artifact)
                if hasattr(closes, "squeeze"):
                    closes = closes.squeeze()
                closes = list(closes)
                if len(closes) >= 2:
                    s, c = float(closes[0]), float(closes[-1])
                    result[f"{key}_start"]   = round(s, 2)
                    result[f"{key}_current"] = round(c, 2)
                    result[f"{key}_pct"]     = round((c - s) / s * 100, 2)
                    got_data = True
            except Exception as _ye:
                _logger.warning(f"[BENCHMARK] yfinance failed for {sym}: {_ye}")
        # Only cache if we got real data — retry on next call if both failed
        if got_data:
            _benchmark_cache[cache_key] = result
    except Exception as e:
        result["error"] = str(e)
        _logger.warning(f"[BENCHMARK] outer error: {e}")
    return result


@app.post("/api/paper/reset")
def api_paper_reset(model: str = "standard"):
    try:
        ex = _get_paper_executor(model)
        ex.reset_account()
        return {"status": "reset"}
    except Exception as e:
        return JSONResponse(status_code=503, content={"error": str(e)})


_paper_running = False


@app.post("/api/paper/run")
def api_paper_run():
    global _paper_running
    if _paper_running:
        return {"status": "already_running"}
    try:
        from paper.auto_scheduler import get_scheduler
        sched = get_scheduler()
        if sched:
            if sched._cycle_running:
                return {"status": "already_running"}
            # Reuse the persistent engine pool — no cold-start overhead
            result = sched.trigger_cycle()
            return {"status": result}
    except Exception:
        pass

    # Fallback: scheduler not ready yet — spin up a one-shot thread
    import threading

    def _run():
        global _paper_running
        try:
            from models.database import init_db as _init_db
            _, _Session_pre = _init_db(config.database.url, echo=False)
            try:
                from pipeline.ingestion import IngestionPipeline
                price_pipeline = IngestionPipeline(db_session_factory=_Session_pre)
                price_pipeline.run_prices_only()
            except Exception as _pe:
                _logger.warning(f"[PAPER] Price refresh failed (using cached): {_pe}")
        except Exception:
            pass
        try:
            from paper.runner import run_paper_cycle
            from analysis.engine import FirstPrinciplesEngine
            from signals.fft_cycles import FFTCycleDetector
            from signals.fibonacci import FibonacciAnalyzer
            from signals.insider_flow import InsiderFlowAnalyzer
            from signals.market_microstructure import (
                VWAPCalculator, VolumeProfileAnalyzer, VIXRegimeDetector
            )
            from signals.aggregator import SignalAggregator
            from fud.filter_engine import FUDFilterEngine
            from decision.engine import DecisionEngine
            from risk.manager import RiskManager
            from broker.market_data import SchwabMarketData
            from paper.executor import PaperExecutor
            from models.database import init_db as _init_db
            _apply_thresh_override()
            _, _Session = _init_db(config.database.url, echo=False)
            run_paper_cycle(
                Session=_Session,
                analysis_engine=FirstPrinciplesEngine(db_session_factory=_Session),
                fft_detector=FFTCycleDetector(),
                fib_analyzer=FibonacciAnalyzer(),
                insider_analyzer=InsiderFlowAnalyzer(),
                vwap_calc=VWAPCalculator(),
                vol_analyzer=VolumeProfileAnalyzer(),
                vix_detector=VIXRegimeDetector(),
                aggregator=SignalAggregator(),
                fud_engine=FUDFilterEngine(db_session_factory=_Session),
                decision_engine=DecisionEngine(db_session_factory=_Session),
                risk_manager=RiskManager(db_session_factory=_Session),
                executor=PaperExecutor(main_db_session_factory=_Session),
                market_data=SchwabMarketData(),
            )
        except Exception as e:
            _logger.warning(f"[PAPER] Run-now cycle failed: {e}")
        finally:
            _paper_running = False

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "started"}


@app.get("/api/paper/run/status")
def api_paper_run_status():
    running = _paper_running
    if not running:
        try:
            from paper.auto_scheduler import get_scheduler as _gs
            _s = _gs()
            if _s:
                running = _s._cycle_running
        except Exception:
            pass
    return {"running": running}


# ── Stage Gate ────────────────────────────────────────────────────────────────

STAGEGATE_HTML = '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="UTF-8">\n<meta name="viewport" content="width=device-width, initial-scale=1.0">\n<title>Stage Gate \\u2014 NWO</title>\n<style>\n  * { box-sizing: border-box; margin: 0; padding: 0; }\n  body { background: #0d1117; color: #e6edf3; font-family: \'Segoe UI\', monospace; font-size: 14px; min-height: 100vh; }\n  header { background: #161b22; padding: 12px 20px; border-bottom: 1px solid #30363d;\n           display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }\n  .back-btn { padding: 5px 12px; border-radius: 6px; border: 1px solid #30363d;\n              background: #21262d; color: #8b949e; text-decoration: none; font-size: 12px; }\n  header h1 { font-size: 17px; letter-spacing: 1px; color: #58a6ff; }\n  .header-right { margin-left: auto; display: flex; gap: 8px; align-items: center; }\n  .add-form { display: flex; gap: 6px; }\n  .add-input { padding: 5px 10px; border-radius: 6px; border: 1px solid #30363d;\n               background: #21262d; color: #e6edf3; font-size: 12px; width: 120px; text-transform: uppercase; }\n  .add-input::placeholder { color: #8b949e; text-transform: none; }\n  .add-btn { padding: 5px 12px; border-radius: 6px; border: 1px solid #58a6ff;\n             background: #0d1e36; color: #58a6ff; cursor: pointer; font-size: 12px; }\n  .add-btn:hover { background: #1c2e50; }\n  .save-badge { font-size: 11px; color: #3fb950; display: none; }\n  /* Two-column layout */\n  .stages { display: grid; grid-template-columns: 1fr 1fr; gap: 0; height: calc(100vh - 57px); }\n  .stage { display: flex; flex-direction: column; border-right: 1px solid #30363d; overflow: hidden; }\n  .stage:last-child { border-right: none; }\n  .stage-header { padding: 14px 18px; background: #161b22; border-bottom: 1px solid #30363d;\n                  display: flex; align-items: center; gap: 10px; flex-shrink: 0; }\n  .stage-1 .stage-header { border-top: 3px solid #8b949e; }\n  .stage-2 .stage-header { border-top: 3px solid #3fb950; }\n  .stage-title { font-size: 14px; font-weight: 700; letter-spacing: 0.5px; }\n  .stage-1 .stage-title { color: #8b949e; }\n  .stage-2 .stage-title { color: #3fb950; }\n  .stage-subtitle { font-size: 11px; color: #8b949e; margin-top: 1px; }\n  .stage-count { margin-left: auto; font-size: 11px; color: #8b949e; background: #21262d;\n                 padding: 2px 8px; border-radius: 10px; }\n  /* Drop zone */\n  .drop-zone { flex: 1; overflow-y: auto; padding: 12px;\n               display: flex; flex-direction: column; gap: 8px; }\n  .drop-zone.drag-over { background: rgba(88,166,255,0.05);\n                          outline: 2px dashed #58a6ff; outline-offset: -4px; border-radius: 4px; }\n  /* Stock cards */\n  .stock-card { background: #161b22; border: 1px solid #30363d; border-radius: 8px;\n                padding: 10px 14px; cursor: grab; display: flex; align-items: center;\n                gap: 10px; transition: border-color 0.15s, background 0.15s;\n                user-select: none; }\n  .stock-card:hover { border-color: #58a6ff; background: #1c2128; }\n  .stock-card:active { cursor: grabbing; }\n  .stock-card.dragging { opacity: 0.4; }\n  .stage-2 .stock-card { border-left: 3px solid #3fb950; }\n  .card-ticker { font-size: 15px; font-weight: 700; min-width: 60px; }\n  .card-meta { font-size: 11px; color: #8b949e; flex: 1; }\n  .card-signal { font-size: 11px; font-weight: 600; }\n  .sig-bull { color: #3fb950; } .sig-bear { color: #f85149; } .sig-hold { color: #8b949e; }\n  .card-remove { background: none; border: none; color: #8b949e; cursor: pointer;\n                 font-size: 14px; padding: 2px 4px; border-radius: 4px; line-height: 1; }\n  .card-remove:hover { color: #f85149; background: rgba(248,81,73,0.1); }\n  .drop-hint { color: #8b949e; font-size: 12px; text-align: center; padding: 30px;\n               border: 2px dashed #21262d; border-radius: 8px; font-style: italic; margin-top: 4px; }\n  @media (max-width: 700px) {\n    .stages { grid-template-columns: 1fr; height: auto; }\n    .stage { min-height: 40vh; border-right: none; border-bottom: 1px solid #30363d; }\n  }\n</style>\n</head>\n<body>\n<header>\n  <a href="/" class="back-btn">&#8592; Dashboard</a>\n  <h1>&#127760; Stage Gate</h1>\n  <div class="header-right">\n    <div class="add-form">\n      <input class="add-input" id="add-input" type="text" placeholder="Add ticker..." maxlength="10"\n             onkeydown="if(event.key===\'Enter\') addTicker()">\n      <button class="add-btn" onclick="addTicker()">+ Add</button>\n    </div>\n    <span class="save-badge" id="save-badge">&#10003; Saved</span>\n  </div>\n</header>\n\n<div class="stages">\n  <!-- Stage 1 -->\n  <div class="stage stage-1">\n    <div class="stage-header">\n      <div>\n        <div class="stage-title">&#128203; Stage 1 &mdash; Monitoring</div>\n        <div class="stage-subtitle">Watching only &middot; drag right to activate</div>\n      </div>\n      <span class="stage-count" id="count-1">0</span>\n    </div>\n    <div class="drop-zone" id="zone-1"\n         ondragover="onDragOver(event,\'1\')" ondragleave="onDragLeave(\'1\')" ondrop="onDrop(event,\'1\')">\n      <div class="drop-hint">Drag stocks here to monitor (no trading)</div>\n    </div>\n  </div>\n\n  <!-- Stage 2 -->\n  <div class="stage stage-2">\n    <div class="stage-header">\n      <div>\n        <div class="stage-title">&#9654; Stage 2 &mdash; Active Trading</div>\n        <div class="stage-subtitle">Full AI pipeline &middot; paper &amp; live execution</div>\n      </div>\n      <span class="stage-count" id="count-2">0</span>\n    </div>\n    <div class="drop-zone" id="zone-2"\n         ondragover="onDragOver(event,\'2\')" ondragleave="onDragLeave(\'2\')" ondrop="onDrop(event,\'2\')">\n      <div class="drop-hint">Drag stocks here to activate AI analysis &amp; trading</div>\n    </div>\n  </div>\n</div>\n\n<script src="/stagegate.js"></script>\n</body>\n</html>'
STAGEGATE_JS   = '\n// ── State ─────────────────────────────────────────────────────────────────────\nlet _state = { stage1: [], stage2: [] };\nlet _signals = {};   // ticker -> signal data from /api/signals\nlet _dragTicker = null;\nlet _dragFrom   = null;\n\n// ── Boot ──────────────────────────────────────────────────────────────────────\nasync function boot() {\n  // Load signals for metadata (confidence, signal type, price)\n  try {\n    const sigs = await fetch(\'/api/signals\').then(r => r.json());\n    (sigs || []).forEach(s => { _signals[s.ticker] = s; });\n  } catch(e) { if(window._nwoErr)_nwoErr(e); }\n\n  // Load stage state\n  try {\n    _state = await fetch(\'/api/stagegate\').then(r => r.json());\n  } catch(e) { if(window._nwoErr)_nwoErr(e); }\n\n  render();\n}\n\n// ── Render ────────────────────────────────────────────────────────────────────\nfunction render() {\n  renderZone(\'1\', _state.stage1);\n  renderZone(\'2\', _state.stage2);\n  document.getElementById(\'count-1\').textContent = _state.stage1.length;\n  document.getElementById(\'count-2\').textContent = _state.stage2.length;\n}\n\nfunction renderZone(stage, tickers) {\n  const zone = document.getElementById(\'zone-\' + stage);\n  if (!tickers.length) {\n    zone.innerHTML = stage === \'1\'\n      ? \'<div class="drop-hint">Drag stocks here to monitor (no trading)</div>\'\n      : \'<div class="drop-hint">Drag stocks here to activate AI analysis &amp; trading</div>\';\n    return;\n  }\n  zone.innerHTML = tickers.map(ticker => cardHtml(ticker, stage)).join(\'\');\n}\n\nfunction cardHtml(ticker, stage) {\n  const s = _signals[ticker] || {};\n  const sig = (s.signal || \'HOLD\').toUpperCase();\n  const sigCls = sig === \'BUY\' || sig === \'STRONG_BUY\' ? \'sig-bull\'\n               : sig === \'SELL\' || sig === \'STRONG_SELL\' ? \'sig-bear\' : \'sig-hold\';\n  const price = s.current_price ? \'$\' + s.current_price.toFixed(2) : \'\';\n  const conf  = s.confidence ? (s.confidence * 100).toFixed(0) + \'% conf\' : \'\';\n  const meta  = [price, conf].filter(Boolean).join(\' \\u00b7 \');\n  return \'<div class="stock-card" draggable="true" data-ticker="\' + ticker + \'" data-stage="\' + stage + \'" \'\n    + \'ondragstart="onDragStart(event)" ondragend="onDragEnd(event)">\'\n    + \'<div class="card-ticker">\' + ticker + \'</div>\'\n    + \'<div class="card-meta">\' + meta + \'</div>\'\n    + \'<span class="card-signal \' + sigCls + \'">\' + sig + \'</span>\'\n    + \'<button class="card-remove" onclick="removeTicker(\\\'\' + ticker + \'\\\')" title="Remove">&#x2715;</button>\'\n    + \'</div>\';\n}\n\n// ── Drag & Drop ───────────────────────────────────────────────────────────────\nfunction onDragStart(e) {\n  _dragTicker = e.currentTarget.dataset.ticker;\n  _dragFrom   = e.currentTarget.dataset.stage;\n  e.currentTarget.classList.add(\'dragging\');\n  e.dataTransfer.effectAllowed = \'move\';\n}\n\nfunction onDragEnd(e) {\n  e.currentTarget.classList.remove(\'dragging\');\n}\n\nfunction onDragOver(e, stage) {\n  e.preventDefault();\n  e.dataTransfer.dropEffect = \'move\';\n  document.getElementById(\'zone-\' + stage).classList.add(\'drag-over\');\n}\n\nfunction onDragLeave(stage) {\n  document.getElementById(\'zone-\' + stage).classList.remove(\'drag-over\');\n}\n\nfunction onDrop(e, targetStage) {\n  e.preventDefault();\n  document.getElementById(\'zone-\' + targetStage).classList.remove(\'drag-over\');\n  if (!_dragTicker || _dragFrom === targetStage) return;\n\n  // Move ticker\n  const fromArr = _state[\'stage\' + _dragFrom];\n  const toArr   = _state[\'stage\' + targetStage];\n  const idx = fromArr.indexOf(_dragTicker);\n  if (idx !== -1) fromArr.splice(idx, 1);\n  if (!toArr.includes(_dragTicker)) toArr.push(_dragTicker);\n\n  render();\n  save();\n}\n\n// ── Add / Remove ──────────────────────────────────────────────────────────────\nfunction addTicker() {\n  const inp = document.getElementById(\'add-input\');\n  const ticker = inp.value.trim().toUpperCase();\n  inp.value = \'\';\n  if (!ticker) return;\n  if (_state.stage1.includes(ticker) || _state.stage2.includes(ticker)) return;\n  _state.stage1.push(ticker);\n  render();\n  save();\n}\n\nfunction removeTicker(ticker) {\n  _state.stage1 = _state.stage1.filter(t => t !== ticker);\n  _state.stage2 = _state.stage2.filter(t => t !== ticker);\n  render();\n  save();\n}\n\n// ── Persist ───────────────────────────────────────────────────────────────────\nasync function save() {\n  try {\n    await fetch(\'/api/stagegate\', {\n      method: \'POST\',\n      headers: {\'Content-Type\': \'application/json\'},\n      body: JSON.stringify(_state),\n    });\n    const badge = document.getElementById(\'save-badge\');\n    badge.style.display = \'inline\';\n    setTimeout(() => { badge.style.display = \'none\'; }, 2000);\n  } catch(e) { if(window._nwoErr)_nwoErr(e); }\n}\n\nboot();\n'

_STAGEGATE_FILE = "data/stagegate.json"


def _load_stagegate() -> dict:
    """Load stage gate state; seed Stage 1 from signals if first run."""
    import json, os
    from pathlib import Path
    if Path(_STAGEGATE_FILE).exists():
        try:
            with open(_STAGEGATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    # First run — seed Stage 1 from config watchlist
    return {"stage1": list(config.watchlist), "stage2": [], "stage3": []}


def _save_stagegate(data: dict):
    import json, os
    from pathlib import Path
    os.makedirs("data", exist_ok=True)
    with open(_STAGEGATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    # Sync stage1/stage2 to all other model stagegate files (stage3 is model-specific)
    _other_sg_files = [
        "data/stagegate_relaxed.json",
        "data/stagegate_very_relaxed.json",
        "data/stagegate_claude.json",
    ]
    for sg_path in _other_sg_files:
        try:
            p = Path(sg_path)
            existing = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
            existing["stage1"] = data.get("stage1", [])
            existing["stage2"] = data.get("stage2", [])
            existing.setdefault("stage3", [])
            with open(sg_path, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2)
        except Exception:
            pass


@app.get("/stagegate", response_class=HTMLResponse)
def stagegate_page():
    from fastapi.responses import HTMLResponse as HR
    return HR(content=STAGEGATE_HTML, headers={"Cache-Control": "no-store"})


@app.get("/stagegate.js")
def stagegate_js():
    from fastapi.responses import Response
    return Response(content=STAGEGATE_JS,
                    media_type="application/javascript; charset=utf-8",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/stagegate")
def api_stagegate_get():
    return _load_stagegate()


@app.post("/api/stagegate")
async def api_stagegate_save(request: Request):
    import json, threading
    body = await request.json()
    stage1 = body.get("stage1", [])
    stage2 = body.get("stage2", [])
    stage3 = body.get("stage3", [])

    # Detect tickers that are genuinely new (not in the previous stagegate)
    existing = _load_stagegate()
    # Guard: never wipe a non-empty stage2 with an empty one (race condition / stale frontend)
    if not stage2 and existing.get("stage2"):
        stage2 = existing["stage2"]
    existing_all = set(existing.get("stage1", []) + existing.get("stage2", []) + existing.get("stage3", []))
    incoming_all = set(stage1 + stage2 + stage3)
    new_tickers  = [t for t in incoming_all - existing_all if t]

    _save_stagegate({"stage1": stage1, "stage2": stage2, "stage3": stage3})

    # Auto-ingest any new tickers in the background (prices + fundamentals)
    if new_tickers:
        import logging as _log
        _log.getLogger("stagegate").info(f"[STAGEGATE] New tickers detected: {new_tickers} — auto-ingesting")

        def _ingest_new():
            try:
                from models.database import init_db as _idb
                from pipeline.ingestion import IngestionPipeline
                import logging as _log2
                _, _S = _idb(config.database.url, echo=False)
                pip = IngestionPipeline(db_session_factory=_S)
                pip.run_prices_only(tickers=new_tickers)   # fast – cards show prices quickly
                pip.run_full_ingest(tickers=new_tickers)   # slow – fundamentals follow
            except Exception as e:
                import logging as _log2
                _log2.getLogger("stagegate").warning(f"[STAGEGATE] Auto-ingest failed: {e}")

        threading.Thread(target=_ingest_new, daemon=True, name="sg-ingest").start()

    return {"status": "ok", "stage1": len(stage1), "stage2": len(stage2),
            "ingesting": new_tickers}


@app.post("/api/stagegate/add")
async def api_stagegate_add(request: Request):
    """
    Atomic single-ticker add — used by morning brief and any other page
    that wants to add a ticker without loading+posting the full state.
    Body: {"ticker": "AAPL", "stage": "1"}  (stage defaults to "1")
    """
    import threading
    body  = await request.json()
    ticker = (body.get("ticker") or "").strip().upper()
    stage  = str(body.get("stage", "1"))

    if not ticker or stage not in ("1", "2", "3"):
        return {"status": "error", "error": "invalid ticker or stage"}

    current   = _load_stagegate()
    target    = "stage" + stage
    in_stages = {k for k in ("stage1", "stage2", "stage3") if ticker in current.get(k, [])}

    if in_stages:
        if target in in_stages:
            return {"status": "already_exists", "ticker": ticker}
        # Allow stage2+stage3 coexistence (split/pyramid mode); block all other cross-stage combos
        allowed = (in_stages == {"stage3"} and target == "stage2") or \
                  (in_stages == {"stage2"} and target == "stage3")
        if not allowed:
            return {"status": "already_exists", "ticker": ticker}

    current.setdefault(target, []).append(ticker)
    _save_stagegate(current)

    # Auto-ingest prices + fundamentals in background
    import logging as _log
    _log.getLogger("stagegate").info(f"[STAGEGATE] Added {ticker} to stage {stage} — auto-ingesting")

    def _ingest():
        try:
            from models.database import init_db as _idb
            from pipeline.ingestion import IngestionPipeline
            import logging as _log2
            _, _S = _idb(config.database.url, echo=False)
            pip = IngestionPipeline(db_session_factory=_S)
            # Fast: prices only (~2-3s) so the card shows a value immediately
            pip.run_prices_only(tickers=[ticker])
            _log2.getLogger("stagegate").info(f"[STAGEGATE] Price fetched for {ticker}")
            # Slow: fundamentals in same thread (no UI blocking)
            pip.run_full_ingest(tickers=[ticker])
        except Exception as e:
            import logging as _log2
            _log2.getLogger("stagegate").warning(f"[STAGEGATE] Auto-ingest failed for {ticker}: {e}")

    threading.Thread(target=_ingest, daemon=True, name=f"sg-ingest-{ticker}").start()
    return {"status": "added", "ticker": ticker, "stage": stage, "ingesting": [ticker]}


# ── Stage Gate embedded in Paper Trading (auto-patched) ───────────────────────
_SG_CSS  = '\n  /* ── Stage Gate ─────────────────────────────────────────────── */\n  .sg-stages { display: grid; grid-template-columns: 1fr 1fr; gap: 0; }\n  .sg-stage { display: flex; flex-direction: column; border-right: 1px solid #21262d; min-width: 0; }\n  .sg-stage:last-child { border-right: none; }\n  .sg-stage-header { padding: 10px 14px; background: #0d1117; border-bottom: 1px solid #21262d;\n                     display: flex; align-items: center; gap: 8px; }\n  .sg-stage-1 .sg-stage-header { border-top: 3px solid #8b949e; }\n  .sg-stage-2 .sg-stage-header { border-top: 3px solid #3fb950; }\n  .sg-stage-title { font-size: 12px; font-weight: 700; }\n  .sg-stage-1 .sg-stage-title { color: #8b949e; }\n  .sg-stage-2 .sg-stage-title { color: #3fb950; }\n  .sg-stage-sub { font-size: 10px; color: #8b949e; margin-top: 2px; }\n  .sg-count { margin-left: auto; font-size: 11px; color: #8b949e; background: #21262d;\n              padding: 2px 7px; border-radius: 10px; }\n  .sg-drop-zone { flex: 1; min-height: 80px; padding: 8px;\n                  display: flex; flex-direction: column; gap: 6px; }\n  .sg-drop-zone.drag-over { background: rgba(88,166,255,0.05);\n                             outline: 2px dashed #58a6ff; outline-offset: -3px; border-radius: 4px; }\n  .sg-card { background: #0d1117; border: 1px solid #21262d; border-radius: 6px;\n             padding: 7px 10px; cursor: grab; display: flex; align-items: center;\n             gap: 8px; user-select: none; transition: border-color 0.15s; }\n  .sg-card:hover { border-color: #58a6ff; }\n  .sg-card:active { cursor: grabbing; }\n  .sg-card.dragging { opacity: 0.4; }\n  .sg-stage-2 .sg-card { border-left: 3px solid #3fb950; }\n  .sg-ticker { font-size: 13px; font-weight: 700; min-width: 55px; }\n  .sg-meta { font-size: 10px; color: #8b949e; flex: 1; }\n  .sg-signal { font-size: 10px; font-weight: 600; }\n  .sg-remove { background: none; border: none; color: #8b949e; cursor: pointer;\n               font-size: 13px; padding: 1px 3px; border-radius: 3px; line-height: 1; }\n  .sg-remove:hover { color: #f85149; background: rgba(248,81,73,0.1); }\n  .sg-hint { color: #8b949e; font-size: 11px; text-align: center; padding: 20px 8px;\n             border: 2px dashed #21262d; border-radius: 6px; font-style: italic; }\n  /* Activation modal */\n  .sg-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.75);\n                display: flex; align-items: center; justify-content: center; z-index: 9999; }\n  .sg-modal-box { background: #161b22; border: 1px solid #30363d; border-radius: 10px;\n                  padding: 24px; width: 320px; display: flex; flex-direction: column; gap: 14px; }\n  .sg-modal-box h3 { font-size: 15px; color: #e6edf3; }\n  .sg-modal-price { font-size: 12px; color: #8b949e; }\n  .sg-toggle { display: flex; gap: 20px; font-size: 13px; }\n  .sg-toggle label { display: flex; align-items: center; gap: 6px; cursor: pointer; color: #e6edf3; }\n  .sg-amount-input { width: 100%; padding: 9px 12px; border-radius: 6px; border: 1px solid #30363d;\n                     background: #21262d; color: #e6edf3; font-size: 15px; }\n  .sg-amount-input:focus { outline: none; border-color: #58a6ff; }\n  .sg-modal-hint { font-size: 11px; color: #8b949e; min-height: 16px; }\n  .sg-modal-btns { display: flex; gap: 8px; justify-content: flex-end; margin-top: 4px; }\n  .sg-cancel-btn { padding: 7px 16px; border-radius: 6px; border: 1px solid #30363d;\n                   background: transparent; color: #8b949e; cursor: pointer; font-size: 13px; }\n  .sg-cancel-btn:hover { background: #21262d; }\n  .sg-act-btn { padding: 7px 18px; border-radius: 6px; border: 1px solid #3fb950;\n                background: #1a4731; color: #3fb950; cursor: pointer; font-size: 13px; font-weight: 600; }\n  .sg-act-btn:hover { background: #1e5c3a; }\n  .sg-act-btn:disabled { opacity: 0.5; cursor: not-allowed; }\n'
_SG_HTML = '\n  <!-- ── Stage Gate ───────────────────────────────────────────── -->\n  <div class="section">\n    <div class="section-title">&#127760; Stage Gate &mdash; Stock Activation</div>\n    <div class="sg-stages">\n      <div class="sg-stage sg-stage-1">\n        <div class="sg-stage-header">\n          <div>\n            <div class="sg-stage-title">&#128203; Stage 1 &mdash; Monitoring</div>\n            <div class="sg-stage-sub">Watching only &middot; drag right to activate trading</div>\n          </div>\n          <span class="sg-count" id="sg-count-1">0</span>\n        </div>\n        <div class="sg-drop-zone" id="sg-zone-1"\n             ondragover="sgDragOver(event,\'1\')" ondragleave="sgDragLeave(\'1\')" ondrop="sgDrop(event,\'1\')">\n          <div class="sg-hint">Drag stocks here to monitor (no trading)</div>\n        </div>\n      </div>\n      <div class="sg-stage sg-stage-2">\n        <div class="sg-stage-header">\n          <div>\n            <div class="sg-stage-title">&#9654; Stage 2 &mdash; Active Trading</div>\n            <div class="sg-stage-sub">AI pipeline &middot; paper execution</div>\n          </div>\n          <span class="sg-count" id="sg-count-2">0</span>\n        </div>\n        <div class="sg-drop-zone" id="sg-zone-2"\n             ondragover="sgDragOver(event,\'2\')" ondragleave="sgDragLeave(\'2\')" ondrop="sgDrop(event,\'2\')">\n          <div class="sg-hint">Drag here to activate AI analysis &amp; trading</div>\n        </div>\n      </div>\n    </div>\n  </div>\n\n  <!-- Activation modal -->\n  <div id="sg-overlay" class="sg-overlay" style="display:none">\n    <div class="sg-modal-box">\n      <h3 id="sg-modal-title">Activate for Trading</h3>\n      <p class="sg-modal-price" id="sg-modal-price"></p>\n      <div class="sg-toggle">\n        <label><input type="radio" name="sg-mode" id="sg-mode-shares" value="shares" checked onchange="sgUpdateHint()"> Shares</label>\n        <label><input type="radio" name="sg-mode" id="sg-mode-dollars" value="dollars" onchange="sgUpdateHint()"> Amount ($)</label>\n      </div>\n      <input class="sg-amount-input" id="sg-amount" type="number" min="1" step="1"\n             placeholder="Enter amount..." oninput="sgUpdateHint()"\n             onkeydown="if(event.key===\'Enter\') sgModalConfirm()">\n      <p class="sg-modal-hint" id="sg-modal-hint">&nbsp;</p>\n      <div class="sg-modal-btns">\n        <button class="sg-cancel-btn" onclick="sgModalCancel()">Cancel</button>\n        <button class="sg-act-btn" id="sg-act-btn" onclick="sgModalConfirm()">&#9654; Start Trading</button>\n      </div>\n    </div>\n  </div>\n'
_SG_JS   = '\n// ── Stage Gate (embedded in paper dashboard) ─────────────────────────────────\nlet _sgState   = { stage1: [], stage2: [] };\nlet _sgSigs    = {};\nlet _sgDragT   = null;\nlet _sgDragF   = null;\nlet _sgPending = null;\n\nasync function sgBoot() {\n  try {\n    const sigs = await fetch(\'/api/signals\').then(r => r.json());\n    (sigs || []).forEach(s => { _sgSigs[s.ticker] = s; });\n  } catch(e) { if(window._nwoErr)_nwoErr(e); }\n  try { _sgState = await fetch(\'/api/stagegate\').then(r => r.json()); } catch(e) { if(window._nwoErr)_nwoErr(e); }\n  sgRender();\n}\n\nfunction sgRender() {\n  const s1 = _sgState.stage1 || [], s2 = _sgState.stage2 || [];\n  sgRenderZone(\'1\', s1);\n  sgRenderZone(\'2\', s2);\n  document.getElementById(\'sg-count-1\').textContent = s1.length;\n  document.getElementById(\'sg-count-2\').textContent = s2.length;\n}\n\nfunction sgRenderZone(stage, tickers) {\n  const zone = document.getElementById(\'sg-zone-\' + stage);\n  if (!tickers.length) {\n    zone.innerHTML = stage === \'1\'\n      ? \'<div class="sg-hint">Drag stocks here to monitor (no trading)</div>\'\n      : \'<div class="sg-hint">Drag here to activate AI analysis &amp; trading</div>\';\n    return;\n  }\n  zone.innerHTML = tickers.map(t => sgCardHtml(t, stage)).join(\'\');\n}\n\nfunction sgCardHtml(ticker, stage) {\n  const s   = _sgSigs[ticker] || {};\n  const sig = (s.signal || \'HOLD\').toUpperCase();\n  const sc  = sig === \'BUY\' || sig === \'STRONG_BUY\'   ? \'sig-bull\'\n            : sig === \'SELL\' || sig === \'STRONG_SELL\'  ? \'sig-bear\' : \'\';\n  const price = s.current_price ? \'$\' + s.current_price.toFixed(2) : \'\';\n  // Use data-ticker on the remove button — no quote escaping needed in onclick\n  return \'<div class="sg-card" draggable="true" data-ticker="\' + ticker + \'" data-stage="\' + stage + \'" \'\n    + \'ondragstart="sgDragStart(event)" ondragend="sgDragEnd(event)">\'\n    + \'<div class="sg-ticker">\' + ticker + \'</div>\'\n    + \'<div class="sg-meta">\' + price + \'</div>\'\n    + \'<span class="sg-signal \' + sc + \'">\' + sig + \'</span>\'\n    + \'<button class="sg-remove" data-ticker="\' + ticker + \'" onclick="sgRemove(this.dataset.ticker)" title="Remove">&#x2715;</button>\'\n    + \'</div>\';\n}\n\n// ── Drag & drop ───────────────────────────────────────────────────────────────\nfunction sgDragStart(e) {\n  _sgDragT = e.currentTarget.dataset.ticker;\n  _sgDragF = e.currentTarget.dataset.stage;\n  e.currentTarget.classList.add(\'dragging\');\n  e.dataTransfer.effectAllowed = \'move\';\n}\nfunction sgDragEnd(e) { e.currentTarget.classList.remove(\'dragging\'); }\nfunction sgDragOver(e, stage) {\n  e.preventDefault();\n  e.dataTransfer.dropEffect = \'move\';\n  document.getElementById(\'sg-zone-\' + stage).classList.add(\'drag-over\');\n}\nfunction sgDragLeave(stage) {\n  document.getElementById(\'sg-zone-\' + stage).classList.remove(\'drag-over\');\n}\nfunction sgDrop(e, toStage) {\n  e.preventDefault();\n  document.getElementById(\'sg-zone-\' + toStage).classList.remove(\'drag-over\');\n  if (!_sgDragT || _sgDragF === toStage) return;\n  if (toStage === \'2\') {\n    _sgPending = _sgDragT;\n    sgShowModal(_sgDragT);\n  } else {\n    sgMoveLocal(_sgDragT, \'2\', \'1\');\n  }\n}\n\nfunction sgMoveLocal(ticker, from, to) {\n  const fa = _sgState[\'stage\' + from] || [];\n  const ta = _sgState[\'stage\' + to]   || [];\n  const i  = fa.indexOf(ticker);\n  if (i !== -1) fa.splice(i, 1);\n  if (!ta.includes(ticker)) ta.push(ticker);\n  sgRender();\n  sgSave();\n}\n\n// ── Modal ─────────────────────────────────────────────────────────────────────\nfunction sgShowModal(ticker) {\n  const s = _sgSigs[ticker] || {};\n  document.getElementById(\'sg-modal-title\').textContent  = \'Activate \' + ticker + \' for Trading\';\n  document.getElementById(\'sg-modal-price\').textContent  =\n    s.current_price ? \'Current price: $\' + s.current_price.toFixed(2) : \'Price not available\';\n  document.getElementById(\'sg-mode-shares\').checked      = true;\n  document.getElementById(\'sg-amount\').value             = \'\';\n  document.getElementById(\'sg-modal-hint\').innerHTML     = \'&nbsp;\';\n  document.getElementById(\'sg-overlay\').style.display   = \'flex\';\n  setTimeout(() => document.getElementById(\'sg-amount\').focus(), 60);\n}\n\nfunction sgUpdateHint() {\n  const mode  = document.querySelector(\'input[name="sg-mode"]:checked\').value;\n  const amt   = parseFloat(document.getElementById(\'sg-amount\').value);\n  const price = (_sgSigs[_sgPending] || {}).current_price;\n  const hint  = document.getElementById(\'sg-modal-hint\');\n  if (!amt || amt <= 0) { hint.innerHTML = \'&nbsp;\'; return; }\n  if (mode === \'shares\') {\n    hint.textContent = price\n      ? \'Total cost ≈ $\' + (amt * price).toLocaleString(\'en-US\', {minimumFractionDigits:2, maximumFractionDigits:2})\n      : amt + \' shares\';\n  } else {\n    const shares = price ? Math.floor(amt / price) : null;\n    hint.textContent = shares != null\n      ? shares + \' shares @ $\' + price.toFixed(2)\n      : \'$\' + amt + \' allocated\';\n  }\n}\n\nfunction sgModalCancel() {\n  document.getElementById(\'sg-overlay\').style.display = \'none\';\n  _sgPending = null;\n}\n\nasync function sgModalConfirm() {\n  const ticker = _sgPending;\n  const mode   = document.querySelector(\'input[name="sg-mode"]:checked\').value;\n  const amount = parseFloat(document.getElementById(\'sg-amount\').value);\n  if (!ticker || !amount || amount <= 0) return;\n\n  const btn = document.getElementById(\'sg-act-btn\');\n  btn.disabled    = true;\n  btn.textContent = \'Activating…\';\n\n  try {\n    const r = await fetch(\'/api/paper/activate\', {\n      method: \'POST\',\n      headers: {\'Content-Type\': \'application/json\'},\n      body: JSON.stringify({ ticker, mode, amount }),\n    }).then(res => res.json());\n\n    if (r.status === \'ok\') {\n      sgMoveLocal(ticker, \'1\', \'2\');\n      document.getElementById(\'sg-overlay\').style.display = \'none\';\n      _sgPending = null;\n      load();   // refresh account summary\n    } else {\n      alert(\'Could not activate: \' + (r.error || \'unknown error\'));\n    }\n  } catch(e) {\n    alert(\'Error: \' + e.message);\n  } finally {\n    btn.disabled    = false;\n    btn.textContent = \'\\u25b6 Start Trading\';\n  }\n}\n\nfunction sgRemove(ticker) {\n  _sgState.stage1 = (_sgState.stage1 || []).filter(t => t !== ticker);\n  _sgState.stage2 = (_sgState.stage2 || []).filter(t => t !== ticker);\n  sgRender();\n  sgSave();\n}\n\nasync function sgSave() {\n  try {\n    await fetch(\'/api/stagegate\', {\n      method: \'POST\',\n      headers: {\'Content-Type\': \'application/json\'},\n      body: JSON.stringify(_sgState),\n    });\n  } catch(e) { if(window._nwoErr)_nwoErr(e); }\n}\n\nsgBoot();\n'

PAPER_HTML = PAPER_HTML.replace('</style>', _SG_CSS + '</style>', 1).replace('</main>', _SG_HTML + '</main>', 1)
PAPER_JS   = PAPER_JS + _SG_JS


@app.post("/api/paper/activate")
async def api_paper_activate(request: Request, model: str = "standard"):
    """Execute a manual paper buy when dragging a stock to Stage 2."""
    import datetime
    body   = await request.json()
    ticker = str(body.get("ticker", "")).upper().strip()
    mode   = body.get("mode", "shares")   # "shares" or "dollars"
    amount = float(body.get("amount", 0))

    if not ticker or amount <= 0:
        return JSONResponse(status_code=400, content={"error": "invalid input"})

    try:
        from paper.executor  import PaperExecutor, PAPER_MODEL_CONFIGS
        from paper.account   import init_paper_db, PaperAccount, PaperPosition, PaperTrade
        from models.database import init_db as _init_db

        cfg = PAPER_MODEL_CONFIGS.get(model, PAPER_MODEL_CONFIGS["standard"])
        _, MainSession = _init_db(config.database.url, echo=False)
        ex    = PaperExecutor(main_db_session_factory=MainSession, db_path=cfg["db"], stagegate_file=cfg["stagegate"])
        price = ex._latest_price(ticker)
        if not price or price <= 0:
            return JSONResponse(status_code=400, content={"error": f"no price data for {ticker}"})

        qty = int(amount / price) if mode == "dollars" else int(amount)
        if qty <= 0:
            return JSONResponse(status_code=400, content={"error": "quantity rounds to zero"})

        _, PaperSession = init_paper_db(cfg["db"])
        with PaperSession() as session:
            acct = session.query(PaperAccount).first()
            if not acct:
                return JSONResponse(status_code=503, content={"error": "no paper account"})
            total = round(qty * price, 2)
            if acct.cash < total:
                return JSONResponse(status_code=400, content={
                    "error": f"insufficient cash: have ${acct.cash:,.2f}, need ${total:,.2f}"
                })
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

        try:
            from monitor.telegram_bot import send_alert as _tg
            _tg(
                f"✋ <b>MANUAL BUY</b>\n"
                f"<b>{ticker}</b>  {qty} shares @ ${price:.2f}\n"
                f"Total: ${total:,.2f}  |  Stage Gate → Stage 3"
            )
        except Exception:
            pass

        # Persist stage gate state change — manual buy goes to Stage 3
        import json as _json, os as _os
        sg_file = cfg["stagegate"]
        if _os.path.exists(sg_file):
            sg = _json.loads(open(sg_file, encoding="utf-8").read())
        else:
            sg = _load_stagegate()
        for k in ("stage1", "stage2", "stage3"):
            sg.setdefault(k, [])
            if ticker in sg[k]:
                sg[k].remove(ticker)
        sg["stage3"].append(ticker)
        _os.makedirs("data", exist_ok=True)
        open(sg_file, "w", encoding="utf-8").write(_json.dumps(sg, indent=2))

        return {"status": "ok", "ticker": ticker, "qty": qty, "price": price, "total": total}

    except Exception as exc:
        import traceback; traceback.print_exc()
        return JSONResponse(status_code=503, content={"error": str(exc)})


# ── AI Decision info modal (auto-patched) ─────────────────────────────────────
_AI_CSS        = '\n  /* ── AI Decision modal ──────────────────────────────────────── */\n  .sg-info { background: none; border: 1px solid #30363d; color: #58a6ff; cursor: pointer;\n             font-size: 12px; padding: 1px 5px; border-radius: 4px; line-height: 1.4; }\n  .sg-info:hover { background: rgba(88,166,255,0.1); }\n  .ai-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.80);\n                display: flex; align-items: flex-start; justify-content: center;\n                z-index: 10000; overflow-y: auto; padding: 40px 16px; }\n  .ai-modal { background: #161b22; border: 1px solid #30363d; border-radius: 12px;\n              width: 100%; max-width: 580px; display: flex; flex-direction: column; gap: 0; }\n  .ai-modal-head { padding: 18px 20px 14px; border-bottom: 1px solid #21262d;\n                   display: flex; align-items: center; gap: 12px; }\n  .ai-modal-ticker { font-size: 20px; font-weight: 700; color: #e6edf3; }\n  .ai-modal-price  { font-size: 13px; color: #8b949e; }\n  .ai-sig-badge { padding: 3px 10px; border-radius: 12px; font-size: 11px; font-weight: 700;\n                  letter-spacing: 0.5px; margin-left: auto; }\n  .ai-sig-bull { background: rgba(63,185,80,0.15); color: #3fb950; border: 1px solid #3fb950; }\n  .ai-sig-bear { background: rgba(248,81,73,0.15);  color: #f85149; border: 1px solid #f85149; }\n  .ai-sig-hold { background: rgba(139,148,158,0.15); color: #8b949e; border: 1px solid #8b949e; }\n  .ai-modal-body { padding: 16px 20px; display: flex; flex-direction: column; gap: 14px; }\n  .ai-narrative { font-size: 13px; color: #c9d1d9; line-height: 1.6;\n                  background: #0d1117; border: 1px solid #21262d; border-radius: 8px;\n                  padding: 12px 14px; }\n  .ai-blocking { font-size: 12px; color: #f85149; background: rgba(248,81,73,0.08);\n                 border: 1px solid rgba(248,81,73,0.25); border-radius: 6px;\n                 padding: 8px 12px; }\n  .ai-gates { display: flex; flex-wrap: wrap; gap: 10px; }\n  .ai-gate-col { display: flex; flex-direction: column; gap: 4px; flex: 1; min-width: 130px; }\n  .ai-gate-title { font-size: 10px; font-weight: 700; letter-spacing: 1px;\n                   text-transform: uppercase; margin-bottom: 2px; }\n  .ai-gate-pass .ai-gate-title { color: #3fb950; }\n  .ai-gate-bypass .ai-gate-title { color: #d29922; }\n  .ai-gate-fail .ai-gate-title { color: #f85149; }\n  .ai-gate-item { font-size: 11px; color: #c9d1d9; padding: 4px 8px;\n                  border-radius: 4px; display: flex; gap: 6px; align-items: flex-start; }\n  .ai-gate-pass .ai-gate-item { background: rgba(63,185,80,0.06); }\n  .ai-gate-bypass .ai-gate-item { background: rgba(210,153,34,0.06); }\n  .ai-gate-fail .ai-gate-item { background: rgba(248,81,73,0.06); }\n  .ai-gate-icon { flex-shrink: 0; margin-top: 1px; }\n  .ai-metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(90px, 1fr)); gap: 8px; }\n  .ai-metric { background: #0d1117; border: 1px solid #21262d; border-radius: 6px;\n               padding: 8px 10px; text-align: center; }\n  .ai-metric-label { font-size: 9px; color: #8b949e; letter-spacing: 0.8px;\n                     text-transform: uppercase; margin-bottom: 4px; }\n  .ai-metric-value { font-size: 14px; font-weight: 700; color: #e6edf3; }\n  .ai-modal-foot { padding: 12px 20px; border-top: 1px solid #21262d;\n                   display: flex; justify-content: space-between; align-items: center; }\n  .ai-gen-time { font-size: 10px; color: #8b949e; }\n  .ai-close-btn { padding: 6px 18px; border-radius: 6px; border: 1px solid #30363d;\n                  background: #21262d; color: #8b949e; cursor: pointer; font-size: 13px; }\n  .ai-close-btn:hover { background: #30363d; }\n'
_AI_MODAL_HTML = '\n  <!-- AI Decision modal -->\n  <div id="ai-overlay" class="ai-overlay" style="display:none" onclick="if(event.target===this) aiClose()">\n    <div class="ai-modal">\n      <div class="ai-modal-head">\n        <div class="ai-modal-ticker" id="ai-ticker"></div>\n        <div class="ai-modal-price" id="ai-price"></div>\n        <span class="ai-sig-badge" id="ai-sig-badge"></span>\n      </div>\n      <div class="ai-modal-body">\n        <div class="ai-narrative" id="ai-narrative"></div>\n        <div class="ai-blocking" id="ai-blocking" style="display:none"></div>\n        <div class="ai-gates" id="ai-gates"></div>\n        <div class="ai-metrics" id="ai-metrics"></div>\n      </div>\n      <div class="ai-modal-foot">\n        <span class="ai-gen-time" id="ai-gen-time"></span>\n        <button class="ai-close-btn" onclick="aiClose()">Close</button>\n      </div>\n    </div>\n  </div>\n'
_AI_JS         = '\n// ── AI Decision info modal ────────────────────────────────────────────────────\n\n// Override sgCardHtml to add the info (ⓘ) button\nfunction sgCardHtml(ticker, stage) {\n  const s   = _sgSigs[ticker] || {};\n  const sig = (s.signal || \'HOLD\').toUpperCase();\n  const sc  = sig === \'BUY\' || sig === \'STRONG_BUY\'   ? \'sig-bull\'\n            : sig === \'SELL\' || sig === \'STRONG_SELL\'  ? \'sig-bear\' : \'\';\n  const price = s.current_price ? \'$\' + s.current_price.toFixed(2) : \'\';\n  return \'<div class="sg-card" draggable="true" data-ticker="\' + ticker + \'" data-stage="\' + stage + \'" \'\n    + \'ondragstart="sgDragStart(event)" ondragend="sgDragEnd(event)">\'\n    + \'<div class="sg-ticker">\' + ticker + \'</div>\'\n    + \'<div class="sg-meta">\' + price + \'</div>\'\n    + \'<span class="sg-signal \' + sc + \'">\' + sig + \'</span>\'\n    + \'<button class="sg-info"  data-ticker="\' + ticker + \'" onclick="sgShowInfo(this.dataset.ticker)" title="AI Decision">&#9432;</button>\'\n    + \'<button class="sg-remove" data-ticker="\' + ticker + \'" onclick="sgRemove(this.dataset.ticker)" title="Remove">&#x2715;</button>\'\n    + \'</div>\';\n}\n\nfunction sgShowInfo(ticker) {\n  const s = _sgSigs[ticker] || {};\n  let reason = {};\n  try { reason = JSON.parse(s.reasoning || \'{}\'); } catch(e) { console.warn(\'[NWO] parse\', e); }\n\n  // Header\n  document.getElementById(\'ai-ticker\').textContent = ticker;\n  const price = s.current_price;\n  document.getElementById(\'ai-price\').textContent = price ? \'$\' + price.toFixed(2) : \'\';\n\n  const sig = (s.signal || \'HOLD\').toUpperCase();\n  const badge = document.getElementById(\'ai-sig-badge\');\n  badge.textContent = sig;\n  badge.className = \'ai-sig-badge \' +\n    (sig === \'BUY\' || sig === \'STRONG_BUY\'   ? \'ai-sig-bull\' :\n     sig === \'SELL\' || sig === \'STRONG_SELL\' ? \'ai-sig-bear\' : \'ai-sig-hold\');\n\n  // Narrative\n  document.getElementById(\'ai-narrative\').textContent =\n    reason.narrative || \'No AI narrative available yet.\';\n\n  // Blocking reason\n  const blockEl = document.getElementById(\'ai-blocking\');\n  if (reason.blocking_reason) {\n    blockEl.textContent = \'\\u26d4 Blocked: \' + reason.blocking_reason;\n    blockEl.style.display = \'block\';\n  } else {\n    blockEl.style.display = \'none\';\n  }\n\n  // Gates — split passed into truly-passed vs bypassed\n  const _allPassed = reason.gates_passed || [];\n  const failed     = reason.gates_failed || [];\n  const bypassed   = _allPassed.filter(g => g.includes(\'BYPASSED\'));\n  const passed     = _allPassed.filter(g => !g.includes(\'BYPASSED\'));\n  function _gateItem(g) {\n    return \'<div class="ai-gate-item"><span class="ai-gate-icon">&#9679;</span><span>\' + g + \'</span></div>\';\n  }\n  let gatesHtml = \'\';\n  if (passed.length) {\n    gatesHtml += \'<div class="ai-gate-col ai-gate-pass">\'\n      + \'<div class="ai-gate-title">&#10003; Gates Passed</div>\'\n      + passed.map(_gateItem).join(\'\')\n      + \'</div>\';\n  }\n  if (bypassed.length) {\n    gatesHtml += \'<div class="ai-gate-col ai-gate-bypass">\'\n      + \'<div class="ai-gate-title">&#8631; Bypassed</div>\'\n      + bypassed.map(_gateItem).join(\'\')\n      + \'</div>\';\n  }\n  if (failed.length) {\n    gatesHtml += \'<div class="ai-gate-col ai-gate-fail">\'\n      + \'<div class="ai-gate-title">&#10007; Gates Failed</div>\'\n      + failed.map(_gateItem).join(\'\')\n      + \'</div>\';\n  }\n  document.getElementById(\'ai-gates\').innerHTML = gatesHtml;\n\n  // Metrics\n  function pct(v)  { return v != null ? (v * 100).toFixed(1) + \'%\' : \'—\'; }\n  function f2(v)   { return v != null ? v.toFixed(2) : \'—\'; }\n  const metrics = [\n    { label: \'Signal\',    value: sig },\n    { label: \'Conf\',      value: pct(s.confidence) },\n    { label: \'P(Bull)\',   value: pct(reason.p_bull) },\n    { label: \'Kelly\',     value: f2(reason.kelly) },\n    { label: \'MoS\',       value: pct(s.margin_of_safety) },\n    { label: \'FUD\',       value: f2(s.fud_score) },\n    { label: \'Regime\',    value: (reason.reynolds_regime || \'—\').toUpperCase() },\n    { label: \'Quantum\',   value: (reason.quantum_state  || \'—\').toUpperCase() },\n  ];\n  document.getElementById(\'ai-metrics\').innerHTML = metrics.map(m =>\n    \'<div class="ai-metric"><div class="ai-metric-label">\' + m.label + \'</div>\'\n    + \'<div class="ai-metric-value">\' + m.value + \'</div></div>\'\n  ).join(\'\');\n\n  // Footer timestamp\n  document.getElementById(\'ai-gen-time\').textContent =\n    s.generated_at ? \'Generated: \' + s.generated_at : \'\';\n\n  document.getElementById(\'ai-overlay\').style.display = \'flex\';\n}\n\nfunction aiClose() {\n  document.getElementById(\'ai-overlay\').style.display = \'none\';\n}\n\n// Close on Escape\ndocument.addEventListener(\'keydown\', e => { if (e.key === \'Escape\') aiClose(); });\n'

PAPER_HTML = PAPER_HTML.replace('</style>', _AI_CSS + '</style>', 1).replace('</main>', _AI_MODAL_HTML + '</main>', 1)
PAPER_JS   = PAPER_JS + _AI_JS


# ── 3-Stage UI override (auto-patched) ────────────────────────────────────────
_SG3_JS  = '\n// ════════════════════════════════════════════════════════════════════════════\n// Stage Gate 3-Stage (overrides old 2-stage code)\n// ════════════════════════════════════════════════════════════════════════════\n(function() {\n\n// ── Inject CSS ────────────────────────────────────────────────────────────────\nconst _sg3style = document.createElement(\'style\');\n_sg3style.textContent = \'\\n  /* ── Stage Gate 3-col ─────────────────────────────────────── */\\n  .sg3-wrap  { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 0; }\\n  .sg3-col   { display: flex; flex-direction: column; border-right: 1px solid #21262d; min-width: 0; }\\n  .sg3-col:last-child { border-right: none; }\\n  .sg3-hd    { padding: 10px 14px; background: #0d1117; border-bottom: 1px solid #21262d;\\n               display: flex; align-items: center; gap: 8px; }\\n  .sg3-c1 .sg3-hd  { border-top: 3px solid #8b949e; }\\n  .sg3-c2 .sg3-hd  { border-top: 3px solid #58a6ff; }\\n  .sg3-c3 .sg3-hd  { border-top: 3px solid #3fb950; }\\n  .sg3-title { font-size: 12px; font-weight: 700; }\\n  .sg3-c1 .sg3-title { color: #8b949e; }\\n  .sg3-c2 .sg3-title { color: #58a6ff; }\\n  .sg3-c3 .sg3-title { color: #3fb950; }\\n  .sg3-sub   { font-size: 10px; color: #8b949e; margin-top: 2px; }\\n  .sg3-cnt   { margin-left: auto; font-size: 11px; color: #8b949e;\\n               background: #21262d; padding: 2px 7px; border-radius: 10px; }\\n  .sg3-zone  { flex: 1; min-height: 80px; padding: 8px;\\n               display: flex; flex-direction: column; gap: 6px; }\\n  .sg3-zone.drag-over { background: rgba(88,166,255,0.05);\\n                        outline: 2px dashed #58a6ff; outline-offset: -3px; border-radius: 4px; }\\n  .sg3-card  { background: #0d1117; border: 1px solid #21262d; border-radius: 6px;\\n               padding: 7px 10px; cursor: grab; display: flex; align-items: center;\\n               gap: 6px; user-select: none; transition: border-color 0.15s; flex-wrap: wrap; }\\n  .sg3-card:hover   { border-color: #58a6ff; }\\n  .sg3-card:active  { cursor: grabbing; }\\n  .sg3-card.dragging { opacity: 0.4; }\\n  .sg3-c2 .sg3-card { border-left: 3px solid #58a6ff; }\\n  .sg3-c3 .sg3-card { border-left: 3px solid #3fb950; }\\n  .sg3-tick  { font-size: 13px; font-weight: 700; min-width: 52px; }\\n  .sg3-meta  { font-size: 10px; color: #8b949e; flex: 1; min-width: 50px; }\\n  .sg3-pnl   { font-size: 10px; font-weight: 600; }\\n  .sg3-sig   { font-size: 10px; font-weight: 600; }\\n  .sg3-acts  { display: flex; gap: 3px; margin-left: auto; }\\n  .sg3-btn   { background: none; border: 1px solid #30363d; color: #8b949e;\\n               cursor: pointer; font-size: 11px; padding: 2px 6px;\\n               border-radius: 4px; line-height: 1.4; white-space: nowrap; }\\n  .sg3-btn-buy  { border-color: #3fb950; color: #3fb950; }\\n  .sg3-btn-buy:hover  { background: rgba(63,185,80,0.12); }\\n  .sg3-btn-sell { border-color: #f85149; color: #f85149; }\\n  .sg3-btn-sell:hover { background: rgba(248,81,73,0.12); }\\n  .sg3-btn-ai   { border-color: #58a6ff; color: #58a6ff; }\\n  .sg3-btn-ai:hover   { background: rgba(88,166,255,0.12); }\\n  .sg3-btn-info { border-color: #30363d; color: #58a6ff; }\\n  .sg3-btn-info:hover { background: rgba(88,166,255,0.08); }\\n  .sg3-btn-rm   { border-color: transparent; color: #8b949e; }\\n  .sg3-btn-rm:hover   { color: #f85149; background: rgba(248,81,73,0.08); }\\n  .sg3-hint  { color: #8b949e; font-size: 11px; text-align: center; padding: 18px 8px;\\n               border: 2px dashed #21262d; border-radius: 6px; font-style: italic; }\\n  .sg3-status { font-size: 9px; font-weight: 700; letter-spacing: 0.5px;\\n                padding: 1px 5px; border-radius: 8px; }\\n  .sg3-status-bought { background: rgba(63,185,80,0.2); color: #3fb950; }\\n  .sg3-status-sold   { background: rgba(248,81,73,0.2);  color: #f85149; }\\n  /* Sell modal */\\n  .sg3-sell-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.75);\\n                      display: flex; align-items: center; justify-content: center; z-index: 10001; }\\n  .sg3-sell-box { background: #161b22; border: 1px solid #30363d; border-radius: 10px;\\n                  padding: 24px; width: 340px; display: flex; flex-direction: column; gap: 14px; }\\n  .sg3-sell-box h3 { font-size: 15px; color: #e6edf3; }\\n  .sg3-sell-pos  { font-size: 12px; color: #8b949e; }\\n  .sg3-sell-dest { font-size: 11px; color: #8b949e; }\\n  @media (max-width: 700px) {\\n    .sg3-wrap { grid-template-columns: 1fr; }\\n    .sg3-col  { border-right: none; border-bottom: 1px solid #21262d; }\\n  }\\n\';\ndocument.head.appendChild(_sg3style);\n\n// ── Replace old Stage Gate section with 3-col ─────────────────────────────────\n(function injectHtml() {\n  // Find old section (has sg-stages or sg3-section)\n  const old = document.getElementById(\'sg3-section\')\n           || Array.from(document.querySelectorAll(\'.section\'))\n                .find(s => s.querySelector(\'.section-title\')\n                        && s.querySelector(\'.section-title\').textContent.includes(\'Stage Gate\'));\n  if (!old) {\n    // Not rendered yet — insert before first .section\n    const main = document.querySelector(\'main\');\n    if (main) {\n      const firstSec = main.querySelector(\'.section\');\n      if (firstSec) {\n        firstSec.insertAdjacentHTML(\'beforebegin\', \'\\n<div id="sg3-section" class="section">\\n  <div class="section-title">&#127760; Stage Gate &mdash; Stock Pipeline</div>\\n  <div class="sg3-wrap">\\n    <div class="sg3-col sg3-c1">\\n      <div class="sg3-hd">\\n        <div><div class="sg3-title">&#128203; Stage 1 &mdash; Monitoring</div>\\n             <div class="sg3-sub">Watching only &middot; no trading</div></div>\\n        <span class="sg3-cnt" id="sg3-cnt-1">0</span>\\n      </div>\\n      <div class="sg3-zone" id="sg3-zone-1"\\n           ondragover="sg3Over(event,\\\'1\\\')" ondragleave="sg3Leave(\\\'1\\\')" ondrop="sg3Drop(event,\\\'1\\\')">\\n        <div class="sg3-hint">Stocks you are watching</div>\\n      </div>\\n    </div>\\n    <div class="sg3-col sg3-c2">\\n      <div class="sg3-hd">\\n        <div><div class="sg3-title">&#129302; Stage 2 &mdash; Active AI</div>\\n             <div class="sg3-sub">AI pipeline &middot; auto-buys on signal</div></div>\\n        <span class="sg3-cnt" id="sg3-cnt-2">0</span>\\n      </div>\\n      <div class="sg3-zone" id="sg3-zone-2"\\n           ondragover="sg3Over(event,\\\'2\\\')" ondragleave="sg3Leave(\\\'2\\\')" ondrop="sg3Drop(event,\\\'2\\\')">\\n        <div class="sg3-hint">Drag here to activate AI trading</div>\\n      </div>\\n    </div>\\n    <div class="sg3-col sg3-c3">\\n      <div class="sg3-hd">\\n        <div><div class="sg3-title">&#128200; Stage 3 &mdash; Open Positions</div>\\n             <div class="sg3-sub">Live positions &middot; drag left to sell</div></div>\\n        <span class="sg3-cnt" id="sg3-cnt-3">0</span>\\n      </div>\\n      <div class="sg3-zone" id="sg3-zone-3"\\n           ondragover="sg3Over(event,\\\'3\\\')" ondragleave="sg3Leave(\\\'3\\\')" ondrop="sg3Drop(event,\\\'3\\\')">\\n        <div class="sg3-hint">Positions appear here after a buy</div>\\n      </div>\\n    </div>\\n  </div>\\n</div>\\n<!-- Sell modal -->\\n<div id="sg3-sell-overlay" class="sg3-sell-overlay" style="display:none"\\n     onclick="if(event.target===this) sg3SellCancel()">\\n  <div class="sg3-sell-box">\\n    <h3 id="sg3-sell-title">Sell</h3>\\n    <p class="sg3-sell-pos" id="sg3-sell-pos"></p>\\n    <div class="sg-toggle">\\n      <label><input type="radio" name="sg3sm" id="sg3sm-all" value="all" checked\\n                    onchange="sg3SellModeChange()"> Sell all</label>\\n      <label><input type="radio" name="sg3sm" id="sg3sm-part" value="partial"\\n                    onchange="sg3SellModeChange()"> Partial</label>\\n    </div>\\n    <input class="sg-amount-input" id="sg3-sell-qty" type="number" min="1" step="1"\\n           placeholder="Shares to sell..." style="display:none"\\n           oninput="sg3UpdateSellHint()" onkeydown="if(event.key===\\\'Enter\\\') sg3SellConfirm()">\\n    <p class="sg-modal-hint" id="sg3-sell-hint">&nbsp;</p>\\n    <p class="sg3-sell-dest" id="sg3-sell-dest"></p>\\n    <div class="sg-modal-btns">\\n      <button class="sg-cancel-btn" onclick="sg3SellCancel()">Cancel</button>\\n      <button class="sg-act-btn" id="sg3-sell-btn"\\n              style="border-color:#f85149;background:rgba(248,81,73,0.15);color:#f85149"\\n              onclick="sg3SellConfirm()">&#x1f4b8; Sell</button>\\n    </div>\\n  </div>\\n</div>\\n\');\n      } else {\n        main.insertAdjacentHTML(\'beforeend\', \'\\n<div id="sg3-section" class="section">\\n  <div class="section-title">&#127760; Stage Gate &mdash; Stock Pipeline</div>\\n  <div class="sg3-wrap">\\n    <div class="sg3-col sg3-c1">\\n      <div class="sg3-hd">\\n        <div><div class="sg3-title">&#128203; Stage 1 &mdash; Monitoring</div>\\n             <div class="sg3-sub">Watching only &middot; no trading</div></div>\\n        <span class="sg3-cnt" id="sg3-cnt-1">0</span>\\n      </div>\\n      <div class="sg3-zone" id="sg3-zone-1"\\n           ondragover="sg3Over(event,\\\'1\\\')" ondragleave="sg3Leave(\\\'1\\\')" ondrop="sg3Drop(event,\\\'1\\\')">\\n        <div class="sg3-hint">Stocks you are watching</div>\\n      </div>\\n    </div>\\n    <div class="sg3-col sg3-c2">\\n      <div class="sg3-hd">\\n        <div><div class="sg3-title">&#129302; Stage 2 &mdash; Active AI</div>\\n             <div class="sg3-sub">AI pipeline &middot; auto-buys on signal</div></div>\\n        <span class="sg3-cnt" id="sg3-cnt-2">0</span>\\n      </div>\\n      <div class="sg3-zone" id="sg3-zone-2"\\n           ondragover="sg3Over(event,\\\'2\\\')" ondragleave="sg3Leave(\\\'2\\\')" ondrop="sg3Drop(event,\\\'2\\\')">\\n        <div class="sg3-hint">Drag here to activate AI trading</div>\\n      </div>\\n    </div>\\n    <div class="sg3-col sg3-c3">\\n      <div class="sg3-hd">\\n        <div><div class="sg3-title">&#128200; Stage 3 &mdash; Open Positions</div>\\n             <div class="sg3-sub">Live positions &middot; drag left to sell</div></div>\\n        <span class="sg3-cnt" id="sg3-cnt-3">0</span>\\n      </div>\\n      <div class="sg3-zone" id="sg3-zone-3"\\n           ondragover="sg3Over(event,\\\'3\\\')" ondragleave="sg3Leave(\\\'3\\\')" ondrop="sg3Drop(event,\\\'3\\\')">\\n        <div class="sg3-hint">Positions appear here after a buy</div>\\n      </div>\\n    </div>\\n  </div>\\n</div>\\n<!-- Sell modal -->\\n<div id="sg3-sell-overlay" class="sg3-sell-overlay" style="display:none"\\n     onclick="if(event.target===this) sg3SellCancel()">\\n  <div class="sg3-sell-box">\\n    <h3 id="sg3-sell-title">Sell</h3>\\n    <p class="sg3-sell-pos" id="sg3-sell-pos"></p>\\n    <div class="sg-toggle">\\n      <label><input type="radio" name="sg3sm" id="sg3sm-all" value="all" checked\\n                    onchange="sg3SellModeChange()"> Sell all</label>\\n      <label><input type="radio" name="sg3sm" id="sg3sm-part" value="partial"\\n                    onchange="sg3SellModeChange()"> Partial</label>\\n    </div>\\n    <input class="sg-amount-input" id="sg3-sell-qty" type="number" min="1" step="1"\\n           placeholder="Shares to sell..." style="display:none"\\n           oninput="sg3UpdateSellHint()" onkeydown="if(event.key===\\\'Enter\\\') sg3SellConfirm()">\\n    <p class="sg-modal-hint" id="sg3-sell-hint">&nbsp;</p>\\n    <p class="sg3-sell-dest" id="sg3-sell-dest"></p>\\n    <div class="sg-modal-btns">\\n      <button class="sg-cancel-btn" onclick="sg3SellCancel()">Cancel</button>\\n      <button class="sg-act-btn" id="sg3-sell-btn"\\n              style="border-color:#f85149;background:rgba(248,81,73,0.15);color:#f85149"\\n              onclick="sg3SellConfirm()">&#x1f4b8; Sell</button>\\n    </div>\\n  </div>\\n</div>\\n\');\n      }\n    }\n  } else if (!document.getElementById(\'sg3-section\')) {\n    old.outerHTML = \'\\n<div id="sg3-section" class="section">\\n  <div class="section-title">&#127760; Stage Gate &mdash; Stock Pipeline</div>\\n  <div class="sg3-wrap">\\n    <div class="sg3-col sg3-c1">\\n      <div class="sg3-hd">\\n        <div><div class="sg3-title">&#128203; Stage 1 &mdash; Monitoring</div>\\n             <div class="sg3-sub">Watching only &middot; no trading</div></div>\\n        <span class="sg3-cnt" id="sg3-cnt-1">0</span>\\n      </div>\\n      <div class="sg3-zone" id="sg3-zone-1"\\n           ondragover="sg3Over(event,\\\'1\\\')" ondragleave="sg3Leave(\\\'1\\\')" ondrop="sg3Drop(event,\\\'1\\\')">\\n        <div class="sg3-hint">Stocks you are watching</div>\\n      </div>\\n    </div>\\n    <div class="sg3-col sg3-c2">\\n      <div class="sg3-hd">\\n        <div><div class="sg3-title">&#129302; Stage 2 &mdash; Active AI</div>\\n             <div class="sg3-sub">AI pipeline &middot; auto-buys on signal</div></div>\\n        <span class="sg3-cnt" id="sg3-cnt-2">0</span>\\n      </div>\\n      <div class="sg3-zone" id="sg3-zone-2"\\n           ondragover="sg3Over(event,\\\'2\\\')" ondragleave="sg3Leave(\\\'2\\\')" ondrop="sg3Drop(event,\\\'2\\\')">\\n        <div class="sg3-hint">Drag here to activate AI trading</div>\\n      </div>\\n    </div>\\n    <div class="sg3-col sg3-c3">\\n      <div class="sg3-hd">\\n        <div><div class="sg3-title">&#128200; Stage 3 &mdash; Open Positions</div>\\n             <div class="sg3-sub">Live positions &middot; drag left to sell</div></div>\\n        <span class="sg3-cnt" id="sg3-cnt-3">0</span>\\n      </div>\\n      <div class="sg3-zone" id="sg3-zone-3"\\n           ondragover="sg3Over(event,\\\'3\\\')" ondragleave="sg3Leave(\\\'3\\\')" ondrop="sg3Drop(event,\\\'3\\\')">\\n        <div class="sg3-hint">Positions appear here after a buy</div>\\n      </div>\\n    </div>\\n  </div>\\n</div>\\n<!-- Sell modal -->\\n<div id="sg3-sell-overlay" class="sg3-sell-overlay" style="display:none"\\n     onclick="if(event.target===this) sg3SellCancel()">\\n  <div class="sg3-sell-box">\\n    <h3 id="sg3-sell-title">Sell</h3>\\n    <p class="sg3-sell-pos" id="sg3-sell-pos"></p>\\n    <div class="sg-toggle">\\n      <label><input type="radio" name="sg3sm" id="sg3sm-all" value="all" checked\\n                    onchange="sg3SellModeChange()"> Sell all</label>\\n      <label><input type="radio" name="sg3sm" id="sg3sm-part" value="partial"\\n                    onchange="sg3SellModeChange()"> Partial</label>\\n    </div>\\n    <input class="sg-amount-input" id="sg3-sell-qty" type="number" min="1" step="1"\\n           placeholder="Shares to sell..." style="display:none"\\n           oninput="sg3UpdateSellHint()" onkeydown="if(event.key===\\\'Enter\\\') sg3SellConfirm()">\\n    <p class="sg-modal-hint" id="sg3-sell-hint">&nbsp;</p>\\n    <p class="sg3-sell-dest" id="sg3-sell-dest"></p>\\n    <div class="sg-modal-btns">\\n      <button class="sg-cancel-btn" onclick="sg3SellCancel()">Cancel</button>\\n      <button class="sg-act-btn" id="sg3-sell-btn"\\n              style="border-color:#f85149;background:rgba(248,81,73,0.15);color:#f85149"\\n              onclick="sg3SellConfirm()">&#x1f4b8; Sell</button>\\n    </div>\\n  </div>\\n</div>\\n\';\n  }\n  // Inject sell modal if not present\n  if (!document.getElementById(\'sg3-sell-overlay\')) {\n    document.body.insertAdjacentHTML(\'beforeend\', \'\\n<div id="sg3-sell-overlay" class="sg3-sell-overlay" style="display:none"\\n     onclick="if(event.target===this) sg3SellCancel()">\\n  <div class="sg3-sell-box">\\n    <h3 id="sg3-sell-title">Sell</h3>\\n    <p class="sg3-sell-pos" id="sg3-sell-pos"></p>\\n    <div class="sg-toggle">\\n      <label><input type="radio" name="sg3sm" id="sg3sm-all" value="all" checked\\n                    onchange="sg3SellModeChange()"> Sell all</label>\\n      <label><input type="radio" name="sg3sm" id="sg3sm-part" value="partial"\\n                    onchange="sg3SellModeChange()"> Partial</label>\\n    </div>\\n    <input class="sg-amount-input" id="sg3-sell-qty" type="number" min="1" step="1"\\n           placeholder="Shares to sell..." style="display:none"\\n           oninput="sg3UpdateSellHint()" onkeydown="if(event.key===\\\'Enter\\\') sg3SellConfirm()">\\n    <p class="sg-modal-hint" id="sg3-sell-hint">&nbsp;</p>\\n    <p class="sg3-sell-dest" id="sg3-sell-dest"></p>\\n    <div class="sg-modal-btns">\\n      <button class="sg-cancel-btn" onclick="sg3SellCancel()">Cancel</button>\\n      <button class="sg-act-btn" id="sg3-sell-btn"\\n              style="border-color:#f85149;background:rgba(248,81,73,0.15);color:#f85149"\\n              onclick="sg3SellConfirm()">&#x1f4b8; Sell</button>\\n    </div>\\n  </div>\\n</div>\\n\');\n  }\n})();\n\n// ── State ─────────────────────────────────────────────────────────────────────\nlet _sg3 = { stage1: [], stage2: [], stage3: [] };\nlet _sg3sigs = {};\nlet _sg3pos  = {};   // ticker -> position data {qty, avg_cost, cur_price, pnl, pnl_pct}\nlet _sg3dragT = null;\nlet _sg3dragF = null;\nlet _sg3pendTicker  = null;  // pending for buy modal\nlet _sg3pendTarget  = null;  // target stage for buy\nlet _sg3sellTicker  = null;  // pending for sell modal\nlet _sg3sellTarget  = null;  // target stage after sell\nlet _sg3recentTrades = {};   // ticker -> \'BOUGHT\'|\'SOLD\' (shown briefly)\n\n// ── Boot ──────────────────────────────────────────────────────────────────────\nasync function sg3Boot() {\n  try {\n    const sigs = await fetch(\'/api/signals\').then(r => r.json());\n    (sigs || []).forEach(s => { _sg3sigs[s.ticker] = s; });\n  } catch(e) { if(window._nwoErr)_nwoErr(e); }\n  try { _sg3 = await fetch(\'/api/paper/stagegate?model=\' + (window._PAPER_MODEL||\'standard\')).then(r => r.json()); } catch(e) { if(window._nwoErr)_nwoErr(e); }\n  _sg3.stage1 = _sg3.stage1 || [];\n  _sg3.stage2 = _sg3.stage2 || [];\n  _sg3.stage3 = _sg3.stage3 || [];\n  sg3Render();\n}\n\n// Override old sgBoot to be a no-op (sg3Boot takes over)\nwindow.sgBoot = function() {};\n\n// ── Sync positions from account data ─────────────────────────────────────────\nfunction sg3SyncPositions(positions) {\n  _sg3pos = {};\n  (positions || []).forEach(p => { _sg3pos[p.ticker] = p; });\n\n  // Auto-promote: any open position not in stage3 → move to stage3\n  let changed = false;\n  Object.keys(_sg3pos).forEach(ticker => {\n    if (!_sg3.stage3.includes(ticker)) {\n      for (const k of [\'stage1\', \'stage2\']) {\n        const i = _sg3[k].indexOf(ticker);\n        if (i !== -1) { _sg3[k].splice(i, 1); }\n      }\n      _sg3.stage3.push(ticker);\n      changed = true;\n    }\n  });\n  // Auto-demote: stage3 ticker with no position → back to stage2\n  _sg3.stage3 = _sg3.stage3.filter(ticker => {\n    if (!_sg3pos[ticker]) {\n      if (!_sg3.stage2.includes(ticker) && !_sg3.stage1.includes(ticker)) {\n        _sg3.stage2.push(ticker);\n      }\n      changed = true;\n      return false;\n    }\n    return true;\n  });\n  if (changed) sg3Save();\n  sg3Render();\n}\n\n// ── Render ────────────────────────────────────────────────────────────────────\nfunction sg3Render() {\n  sg3RenderZone(\'1\', _sg3.stage1);\n  sg3RenderZone(\'2\', _sg3.stage2);\n  sg3RenderZone(\'3\', _sg3.stage3);\n  document.getElementById(\'sg3-cnt-1\').textContent = _sg3.stage1.length;\n  document.getElementById(\'sg3-cnt-2\').textContent = _sg3.stage2.length;\n  document.getElementById(\'sg3-cnt-3\').textContent = _sg3.stage3.length;\n}\n\nfunction sg3RenderZone(stage, tickers) {\n  const zone = document.getElementById(\'sg3-zone-\' + stage);\n  if (!zone) return;\n  if (!tickers.length) {\n    const hints = {\n      \'1\': \'Stocks you are watching\',\n      \'2\': \'Drag here to activate AI trading\',\n      \'3\': \'Positions appear here after a buy\',\n    };\n    zone.innerHTML = \'<div class="sg3-hint">\' + hints[stage] + \'</div>\';\n    return;\n  }\n  zone.innerHTML = tickers.map(t => sg3CardHtml(t, stage)).join(\'\');\n}\n\nfunction sg3CardHtml(ticker, stage) {\n  const s    = _sg3sigs[ticker] || {};\n  const pos  = _sg3pos[ticker]  || {};\n  const sig  = (s.signal || \'HOLD\').toUpperCase();\n  const sc   = sig === \'BUY\' || sig === \'STRONG_BUY\'  ? \'sig-bull\'\n             : sig === \'SELL\'|| sig === \'STRONG_SELL\' ? \'sig-bear\' : \'\';\n  const price = s.current_price ? \'$\' + s.current_price.toFixed(2) : \'\';\n  const recent = _sg3recentTrades[ticker];\n\n  let meta = price;\n  let pnlHtml = \'\';\n  if (stage === \'3\' && pos.qty) {\n    const pnlCls = (pos.pnl || 0) >= 0 ? \'up\' : \'dn\';\n    const pnlStr = ((pos.pnl || 0) >= 0 ? \'+\' : \'\') + \'$\' + Math.abs(pos.pnl || 0).toFixed(0);\n    const pctStr = ((pos.pnl_pct || 0) >= 0 ? \'+\' : \'\') + (pos.pnl_pct || 0).toFixed(1) + \'%\';\n    meta = price + (price ? \' · \' : \'\') + pos.qty + \' sh @ $\' + (pos.avg_cost || 0).toFixed(2);\n    pnlHtml = \'<span class="sg3-pnl \' + pnlCls + \'">\' + pnlStr + \' (\' + pctStr + \')</span>\';\n  }\n\n  let statusHtml = \'\';\n  if (recent) {\n    statusHtml = \'<span class="sg3-status sg3-status-\' + recent.toLowerCase() + \'">\' + recent + \'</span>\';\n  }\n\n  // Action buttons differ per stage\n  let btns = \'<div class="sg3-acts">\';\n  btns += \'<button class="sg3-btn sg3-btn-info" data-ticker="\' + ticker + \'" onclick="sgShowInfo(this.dataset.ticker)" title="AI Analysis">&#9432;</button>\';\n  if (stage === \'1\') {\n    btns += \'<button class="sg3-btn sg3-btn-ai"  data-ticker="\' + ticker + \'" onclick="sg3ActivateAI(this.dataset.ticker)"  title="Activate AI">AI</button>\';\n    btns += \'<button class="sg3-btn sg3-btn-buy" data-ticker="\' + ticker + \'" onclick="sg3OpenBuy(this.dataset.ticker,\\\'3\\\')" title="Buy now">Buy</button>\';\n  } else if (stage === \'2\') {\n    btns += \'<button class="sg3-btn sg3-btn-buy" data-ticker="\' + ticker + \'" onclick="sg3OpenBuy(this.dataset.ticker,\\\'3\\\')" title="Buy now">Buy</button>\';\n  } else if (stage === \'3\') {\n    btns += \'<button class="sg3-btn sg3-btn-buy"  data-ticker="\' + ticker + \'" onclick="sg3OpenBuy(this.dataset.ticker,\\\'3\\\')"  title="Add to position">Buy+</button>\';\n    btns += \'<button class="sg3-btn sg3-btn-sell" data-ticker="\' + ticker + \'" onclick="sg3OpenSell(this.dataset.ticker,\\\'2\\\')" title="Sell">Sell</button>\';\n  }\n  btns += \'<button class="sg3-btn sg3-btn-rm" data-ticker="\' + ticker + \'" onclick="sg3Remove(this.dataset.ticker)" title="Remove">&#x2715;</button>\';\n  btns += \'</div>\';\n\n  return \'<div class="sg3-card" draggable="true" data-ticker="\' + ticker + \'" data-stage="\' + stage + \'" \'\n    + \'ondragstart="sg3DragStart(event)" ondragend="sg3DragEnd(event)">\'\n    + \'<div class="sg3-tick">\' + ticker + \'</div>\'\n    + \'<div class="sg3-meta">\' + meta + \'</div>\'\n    + pnlHtml\n    + statusHtml\n    + \'<span class="sg3-sig \' + sc + \'">\' + sig + \'</span>\'\n    + btns\n    + \'</div>\';\n}\n\n// ── Drag & drop ───────────────────────────────────────────────────────────────\nfunction sg3DragStart(e) {\n  _sg3dragT = e.currentTarget.dataset.ticker;\n  _sg3dragF = e.currentTarget.dataset.stage;\n  e.currentTarget.classList.add(\'dragging\');\n  e.dataTransfer.effectAllowed = \'move\';\n}\nfunction sg3DragEnd(e) { e.currentTarget.classList.remove(\'dragging\'); }\nfunction sg3Over(e, stage) {\n  e.preventDefault();\n  e.dataTransfer.dropEffect = \'move\';\n  const z = document.getElementById(\'sg3-zone-\' + stage);\n  if (z) z.classList.add(\'drag-over\');\n}\nfunction sg3Leave(stage) {\n  const z = document.getElementById(\'sg3-zone-\' + stage);\n  if (z) z.classList.remove(\'drag-over\');\n}\n\nfunction sg3Drop(e, toStage) {\n  e.preventDefault();\n  const z = document.getElementById(\'sg3-zone-\' + toStage);\n  if (z) z.classList.remove(\'drag-over\');\n  if (!_sg3dragT || _sg3dragF === toStage) return;\n  const from = _sg3dragF, ticker = _sg3dragT;\n\n  // Moving to Stage 1 from Stage 2 or 3 → sell popup (if has position)\n  if (toStage === \'1\' && (from === \'2\' || from === \'3\')) {\n    if (_sg3pos[ticker]) {\n      sg3OpenSell(ticker, \'1\');\n    } else {\n      sg3MoveLocal(ticker, from, \'1\');\n    }\n    return;\n  }\n  // Moving Stage 3 → Stage 2 → sell popup\n  if (toStage === \'2\' && from === \'3\') {\n    sg3OpenSell(ticker, \'2\');\n    return;\n  }\n  // Stage 1 → Stage 2: activate for AI (no buy)\n  if (toStage === \'2\' && from === \'1\') {\n    sg3ActivateAI(ticker);\n    return;\n  }\n  // Stage 1/2 → Stage 3: buy popup\n  if (toStage === \'3\') {\n    sg3OpenBuy(ticker, \'3\', from);\n    return;\n  }\n  sg3MoveLocal(ticker, from, toStage);\n}\n\n// ── AI activation (Stage 1 → Stage 2, no immediate buy) ──────────────────────\nasync function sg3ActivateAI(ticker) {\n  try {\n    await fetch(\'/api/paper/activate-ai\', {\n      method: \'POST\',\n      headers: {\'Content-Type\': \'application/json\'},\n      body: JSON.stringify({ ticker }),\n    });\n  } catch(e) { if(window._nwoErr)_nwoErr(e); }\n  sg3MoveLocal(ticker, \'1\', \'2\');\n}\n\n// ── Buy modal ─────────────────────────────────────────────────────────────────\nfunction sg3OpenBuy(ticker, targetStage, fromStage) {\n  _sg3pendTicker = ticker;\n  _sg3pendTarget = targetStage || \'3\';\n  _sg3pendFrom   = fromStage || _sg3dragF || null;\n  // Reuse existing buy modal (sg-overlay) from the previous embed\n  const s = _sg3sigs[ticker] || {};\n  document.getElementById(\'sg-modal-title\').textContent = \'Buy \' + ticker;\n  document.getElementById(\'sg-modal-price\').textContent =\n    s.current_price ? \'Current price: $\' + s.current_price.toFixed(2) : \'Price not available\';\n  document.getElementById(\'sg-mode-shares\').checked = true;\n  document.getElementById(\'sg-amount\').value = \'\';\n  document.getElementById(\'sg-modal-hint\').innerHTML = \'&nbsp;\';\n  document.getElementById(\'sg-overlay\').style.display = \'flex\';\n  setTimeout(() => document.getElementById(\'sg-amount\').focus(), 60);\n  // Swap confirm handler\n  document.getElementById(\'sg-act-btn\').onclick = sg3BuyConfirm;\n  document.getElementById(\'sg-act-btn\').textContent = \'\\u25b6 Buy\';\n}\n\nfunction sgUpdateHint() {  // keep existing hint updater working\n  const mode  = document.querySelector(\'input[name="sg-mode"]:checked\').value;\n  const amt   = parseFloat(document.getElementById(\'sg-amount\').value);\n  const price = (_sg3sigs[_sg3pendTicker] || {}).current_price;\n  const hint  = document.getElementById(\'sg-modal-hint\');\n  if (!amt || amt <= 0) { hint.innerHTML = \'&nbsp;\'; return; }\n  if (mode === \'shares\') {\n    hint.textContent = price\n      ? \'Total \\u2248 $\' + (amt * price).toLocaleString(\'en-US\', {minimumFractionDigits:2, maximumFractionDigits:2})\n      : amt + \' shares\';\n  } else {\n    const sh = price ? Math.floor(amt / price) : null;\n    hint.textContent = sh != null ? sh + \' shares @ $\' + price.toFixed(2) : \'$\' + amt;\n  }\n}\n\nasync function sg3BuyConfirm() {\n  const ticker = _sg3pendTicker;\n  const mode   = document.querySelector(\'input[name="sg-mode"]:checked\').value;\n  const amount = parseFloat(document.getElementById(\'sg-amount\').value);\n  if (!ticker || !amount || amount <= 0) return;\n\n  const btn = document.getElementById(\'sg-act-btn\');\n  btn.disabled = true; btn.textContent = \'Buying\\u2026\';\n\n  try {\n    const r = await fetch(\'/api/paper/activate\', {\n      method: \'POST\',\n      headers: {\'Content-Type\': \'application/json\'},\n      body: JSON.stringify({ ticker, mode, amount }),\n    }).then(res => res.json());\n\n    if (r.status === \'ok\') {\n      if (_sg3pendFrom) sg3RemoveFromStage(_sg3pendTicker, _sg3pendFrom);\n      sg3MoveLocal(ticker, null, \'3\');\n      document.getElementById(\'sg-overlay\').style.display = \'none\';\n      _sg3recentTrades[ticker] = \'BOUGHT\';\n      setTimeout(() => { delete _sg3recentTrades[ticker]; sg3Render(); }, 8000);\n      load();\n    } else {\n      alert(\'Buy failed: \' + (r.error || \'unknown\'));\n    }\n  } catch(e) { alert(\'Error: \' + e.message); }\n  finally {\n    btn.disabled = false; btn.textContent = \'\\u25b6 Start Trading\';\n    btn.onclick  = sgModalConfirm;  // restore original handler\n  }\n}\n\n// ── Sell modal ────────────────────────────────────────────────────────────────\nfunction sg3OpenSell(ticker, targetStage) {\n  _sg3sellTicker = ticker;\n  _sg3sellTarget = targetStage || \'1\';\n  const pos = _sg3pos[ticker] || {};\n  const price = (_sg3sigs[ticker] || {}).current_price || pos.cur_price || pos.avg_cost || 0;\n\n  document.getElementById(\'sg3-sell-title\').textContent = \'Sell \' + ticker;\n  document.getElementById(\'sg3-sell-pos\').textContent =\n    pos.qty\n      ? pos.qty + \' shares · avg cost $\' + (pos.avg_cost || 0).toFixed(2) + \' · current $\' + price.toFixed(2)\n      : \'No open position\';\n  document.getElementById(\'sg3sm-all\').checked = true;\n  document.getElementById(\'sg3-sell-qty\').style.display = \'none\';\n  document.getElementById(\'sg3-sell-qty\').value = \'\';\n  const dest = targetStage === \'1\' ? \'Stage 1 (Monitoring)\' : \'Stage 2 (Active AI)\';\n  document.getElementById(\'sg3-sell-dest\').textContent = \'After sell: move to \' + dest;\n  sg3UpdateSellHint();\n  document.getElementById(\'sg3-sell-overlay\').style.display = \'flex\';\n  if (pos.qty) setTimeout(() => document.getElementById(\'sg3-sell-overlay\').focus?.(), 60);\n}\n\nfunction sg3SellModeChange() {\n  const partial = document.getElementById(\'sg3sm-part\').checked;\n  document.getElementById(\'sg3-sell-qty\').style.display = partial ? \'block\' : \'none\';\n  if (partial) document.getElementById(\'sg3-sell-qty\').focus();\n  sg3UpdateSellHint();\n}\n\nfunction sg3UpdateSellHint() {\n  const pos   = _sg3pos[_sg3sellTicker] || {};\n  const price = (_sg3sigs[_sg3sellTicker] || {}).current_price || pos.cur_price || 0;\n  const hint  = document.getElementById(\'sg3-sell-hint\');\n  const mode  = document.querySelector(\'input[name="sg3sm"]:checked\')?.value || \'all\';\n  const qty   = mode === \'all\' ? (pos.qty || 0) : parseFloat(document.getElementById(\'sg3-sell-qty\').value) || 0;\n  if (!qty || !price) { hint.innerHTML = \'&nbsp;\'; return; }\n  hint.textContent = \'Proceeds \\u2248 $\' + (qty * price).toLocaleString(\'en-US\', {minimumFractionDigits:2, maximumFractionDigits:2});\n}\n\nfunction sg3SellCancel() {\n  document.getElementById(\'sg3-sell-overlay\').style.display = \'none\';\n  _sg3sellTicker = null;\n}\n\nasync function sg3SellConfirm() {\n  const ticker      = _sg3sellTicker;\n  const targetStage = _sg3sellTarget;\n  if (!ticker) return;\n\n  const mode = document.querySelector(\'input[name="sg3sm"]:checked\')?.value || \'all\';\n  const qty  = mode === \'partial\' ? parseFloat(document.getElementById(\'sg3-sell-qty\').value) : null;\n\n  const btn = document.getElementById(\'sg3-sell-btn\');\n  btn.disabled = true; btn.textContent = \'Selling\\u2026\';\n\n  try {\n    const r = await fetch(\'/api/paper/sell\', {\n      method: \'POST\',\n      headers: {\'Content-Type\': \'application/json\'},\n      body: JSON.stringify({ ticker, mode, qty, target_stage: targetStage }),\n    }).then(res => res.json());\n\n    if (r.status === \'ok\') {\n      sg3MoveLocal(ticker, \'3\', targetStage);\n      document.getElementById(\'sg3-sell-overlay\').style.display = \'none\';\n      _sg3sellTicker = null;\n      if (!r.no_position) {\n        _sg3recentTrades[ticker] = \'SOLD\';\n        setTimeout(() => { delete _sg3recentTrades[ticker]; sg3Render(); }, 8000);\n      }\n      load();\n    } else {\n      alert(\'Sell failed: \' + (r.error || \'unknown\'));\n    }\n  } catch(e) { alert(\'Error: \' + e.message); }\n  finally { btn.disabled = false; btn.textContent = \'\\u1f4b8 Sell\'; }\n}\n\n// ── Local state helpers ───────────────────────────────────────────────────────\nfunction sg3RemoveFromStage(ticker, stage) {\n  const arr = _sg3[\'stage\' + stage];\n  if (!arr) return;\n  const i = arr.indexOf(ticker);\n  if (i !== -1) arr.splice(i, 1);\n}\n\nfunction sg3MoveLocal(ticker, from, to) {\n  if (from) sg3RemoveFromStage(ticker, from);\n  const toArr = _sg3[\'stage\' + to];\n  if (toArr && !toArr.includes(ticker)) toArr.push(ticker);\n  sg3Render();\n  sg3Save();\n}\n\nfunction sg3Remove(ticker) {\n  [\'stage1\',\'stage2\',\'stage3\'].forEach(k => {\n    _sg3[k] = (_sg3[k] || []).filter(t => t !== ticker);\n  });\n  sg3Render();\n  sg3Save();\n}\n\nasync function sg3Save() {\n  try {\n    await fetch(\'/api/stagegate\', {\n      method: \'POST\',\n      headers: {\'Content-Type\': \'application/json\'},\n      body: JSON.stringify(_sg3),\n    });\n  } catch(e) { if(window._nwoErr)_nwoErr(e); }\n}\n\n// ── Hook into existing load() to sync Stage 3 ────────────────────────────────\nconst _origLoad = load;\nwindow.load = async function() {\n  await _origLoad();\n  try {\n    const sigs = await fetch(\'/api/signals\').then(r => r.json());\n    (sigs || []).forEach(s => { _sg3sigs[s.ticker] = s; });\n    sg3Render();\n  } catch(e) { if(window._nwoErr)_nwoErr(e); }\n  try {\n    const acct = await fetch(\'/api/paper/account?model=\' + (window._PAPER_MODEL||\'standard\')).then(r => r.json());\n    sg3SyncPositions(acct.positions || []);\n  } catch(e) {}\n};\n\n// Escape closes sell modal too\ndocument.addEventListener(\'keydown\', e => {\n  if (e.key === \'Escape\') { sg3SellCancel(); aiClose(); }\n});\n\n// Boot\nsg3Boot();\n\n})(); // end IIFE\n'
PAPER_JS = PAPER_JS + _SG3_JS

# ── TipRanks badge for Paper Trading stage gate cards ─────────────────────────
_TIPRANKS_SG3_JS = """
var _sg3TrData = {};
function _sg3TrBadge(t) {
  var d = _sg3TrData[t] || {};
  var ss = d.smart_score;
  if (ss == null) return '';
  var bg  = ss >= 8 ? '#1a4731' : ss >= 4 ? '#3d2b00' : '#4a1519';
  var col = ss >= 8 ? '#3fb950' : ss >= 4 ? '#d29922' : '#f85149';
  return ' <span style="background:' + bg + ';color:' + col + ';border:1px solid ' + col + ';font-size:9px;padding:1px 4px;border-radius:3px;font-weight:700">&#9733;' + ss + '</span>';
}
"""
PAPER_JS = PAPER_JS + _TIPRANKS_SG3_JS

# Patch sg3CardHtml to show TipRanks Smart Score badge next to ticker
PAPER_JS = PAPER_JS.replace(
    "    + '<div class=\"sg3-tick\">' + ticker + '</div>'\n",
    "    + '<div class=\"sg3-tick\">' + ticker + _sg3TrBadge(ticker) + '</div>'\n"
)

# Patch sg3Boot to load TipRanks data before rendering
PAPER_JS = PAPER_JS.replace(
    "  _sg3.stage1 = _sg3.stage1 || [];\n  _sg3.stage2 = _sg3.stage2 || [];\n  _sg3.stage3 = _sg3.stage3 || [];\n  sg3Render();\n}",
    "  _sg3.stage1 = _sg3.stage1 || [];\n  _sg3.stage2 = _sg3.stage2 || [];\n  _sg3.stage3 = _sg3.stage3 || [];\n  try { _sg3TrData = await fetch('/api/tipranks/all').then(r => r.json()); } catch(e) {}\n  sg3Render();\n}"
)

# Patch sg3Save + AI exits to use per-model /api/paper/ routes (interceptor adds ?model= automatically)
PAPER_JS = PAPER_JS.replace(
    "await fetch('/api/stagegate', {\n      method: 'POST',\n      headers: {'Content-Type': 'application/json'},\n      body: JSON.stringify(_sg3),\n    });",
    "await fetch('/api/paper/stagegate', {\n      method: 'POST',\n      headers: {'Content-Type': 'application/json'},\n      body: JSON.stringify(_sg3),\n    });"
)


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
async def api_paper_sell(request: Request, model: str = "standard"):
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
        from paper.executor  import PaperExecutor, PAPER_MODEL_CONFIGS
        from models.database import init_db as _init_db

        cfg = PAPER_MODEL_CONFIGS.get(model, PAPER_MODEL_CONFIGS["standard"])
        _, MainSession = _init_db(config.database.url, echo=False)
        ex    = PaperExecutor(main_db_session_factory=MainSession, db_path=cfg["db"], stagegate_file=cfg["stagegate"])
        price = ex._latest_price(ticker)

        import json as _json, os as _os
        sg_file = cfg["stagegate"]

        def _sg_load():
            if _os.path.exists(sg_file):
                return _json.loads(open(sg_file, encoding="utf-8").read())
            return _load_stagegate()

        def _sg_save(sg):
            _os.makedirs("data", exist_ok=True)
            open(sg_file, "w", encoding="utf-8").write(_json.dumps(sg, indent=2))

        _, PaperSession = init_paper_db(cfg["db"])
        with PaperSession() as session:
            acct = session.query(PaperAccount).first()
            pos  = session.query(PaperPosition).filter_by(ticker=ticker).first()

            if not pos or pos.qty <= 0:
                sg = _sg_load()
                for k in ("stage1","stage2","stage3"):
                    sg.setdefault(k, [])
                    if ticker in sg[k]: sg[k].remove(ticker)
                sg.setdefault(f"stage{target_stage}", [])
                sg[f"stage{target_stage}"].append(ticker)
                _sg_save(sg)
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

        sg = _sg_load()
        for k in ("stage1","stage2","stage3"):
            sg.setdefault(k, [])
            if ticker in sg[k]: sg[k].remove(ticker)
        sg[f"stage{target_stage}"].append(ticker)
        _sg_save(sg)

        try:
            from monitor.telegram_bot import send_alert as _tg_manual
            _mlabel = {"standard": "Standard", "relaxed": "Relaxed -25%",
                       "very_relaxed": "Relaxed -50%", "claude": "Claude"}.get(model, model.title())
            _tg_manual(
                f"\U0001f534 <b>PAPER SELL (Manual)</b> [{_mlabel}]\n"
                f"<b>{ticker}</b>  {qty:.2f} shares @ ${price:.2f}\n"
                f"Proceeds: ${proceeds:,.2f}  |  Moved to Stage {target_stage}"
            )
        except Exception:
            pass

        return {"status": "ok", "ticker": ticker, "qty": qty, "price": price, "proceeds": proceeds}

    except Exception as exc:
        import traceback; traceback.print_exc()
        return JSONResponse(status_code=503, content={"error": str(exc)})

# ── Fix: expose sg3 functions globally (IIFE scope fix) ──────────────────────
_EXPOSE_JS = '\n// Expose sg3 functions globally for HTML ondragover/ondrop/etc. attributes\nwindow.sg3Over           = sg3Over;\nwindow.sg3Leave          = sg3Leave;\nwindow.sg3Drop           = sg3Drop;\nwindow.sg3DragStart      = sg3DragStart;\nwindow.sg3DragEnd        = sg3DragEnd;\nwindow.sg3ActivateAI     = sg3ActivateAI;\nwindow.sg3OpenBuy        = sg3OpenBuy;\nwindow.sg3OpenSell       = sg3OpenSell;\nwindow.sg3SellModeChange = sg3SellModeChange;\nwindow.sg3UpdateSellHint = sg3UpdateSellHint;\nwindow.sg3SellCancel     = sg3SellCancel;\nwindow.sg3SellConfirm    = sg3SellConfirm;\nwindow.sg3Remove         = sg3Remove;\nwindow.sg3Boot           = sg3Boot;\n\n'
PAPER_JS   = PAPER_JS.replace('sg3Boot();\n\n})(); // end IIFE',
                               _EXPOSE_JS + 'sg3Boot();\n\n})(); // end IIFE')


# ── Paper dashboard feature additions (auto-patched) ─────────────────────────
_FEAT_JS  = '\n// ════════════════════════════════════════════════════════════════════════════\n// Paper dashboard feature additions\n// ════════════════════════════════════════════════════════════════════════════\n\n// ── Inject extra CSS ─────────────────────────────────────────────────────────\n(function() {\n  const s = document.createElement(\'style\');\n  s.textContent = \'\\n  /* ── Ticker tape (paper page) ─────────────────────────────── */\\n  .p-tape-wrap  { overflow: hidden; background: #0a0f17;\\n                  border-bottom: 1px solid #1f6feb; height: 26px; flex-shrink: 0; }\\n  .p-tape-track { display: flex; gap: 24px; white-space: nowrap; will-change: transform;\\n                  animation: p-tape 100s linear infinite; align-items: center; height: 100%;\\n                  padding-left: 12px; }\\n  .p-tape-track:hover { animation-play-state: paused; }\\n  @keyframes p-tape { 0%{transform:translateX(0)} 100%{transform:translateX(-50%)} }\\n  .pt-bull { color: #3fb950; font-size: 12px; font-weight: 700; }\\n  .pt-bear { color: #f85149; font-size: 12px; font-weight: 700; }\\n  .pt-neu  { color: #8b949e; font-size: 12px; }\\n  .pt-sep  { color: #30363d; font-size: 10px; }\\n  /* ── Search + sync header controls ────────────────────────── */\\n  .p-search-wrap { display: flex; gap: 5px; align-items: center; }\\n  .p-search-input { padding: 5px 10px; border-radius: 6px; border: 1px solid #30363d;\\n                    background: #21262d; color: #e6edf3; font-size: 12px; width: 130px;\\n                    text-transform: uppercase; }\\n  .p-search-input::placeholder { color: #8b949e; text-transform: none; }\\n  .p-add-s1 { padding: 5px 9px; border-radius: 6px; border: 1px solid #8b949e;\\n               background: transparent; color: #8b949e; cursor: pointer; font-size: 11px; }\\n  .p-add-s1:hover { background: rgba(139,148,158,0.12); }\\n  .p-add-s2 { padding: 5px 9px; border-radius: 6px; border: 1px solid #58a6ff;\\n               background: transparent; color: #58a6ff; cursor: pointer; font-size: 11px; }\\n  .p-add-s2:hover { background: rgba(88,166,255,0.12); }\\n  .p-sync-btn { padding: 5px 10px; border-radius: 6px; border: 1px solid #d29922;\\n                background: transparent; color: #d29922; cursor: pointer; font-size: 11px; }\\n  .p-sync-btn:hover { background: rgba(210,153,34,0.12); }\\n  /* ── Compact swim lanes ───────────────────────────────────── */\\n  .swim-compact-wrap { padding: 10px 14px; display: flex; flex-direction: column; gap: 10px; }\\n  .swim-row  { display: flex; gap: 8px; align-items: flex-start; }\\n  .swim-row-label { font-size: 10px; font-weight: 700; letter-spacing: 0.5px;\\n                    text-transform: uppercase; width: 90px; flex-shrink: 0; padding-top: 4px; }\\n  .swim-row-0 .swim-row-label { color: #58a6ff; }\\n  .swim-row-1 .swim-row-label { color: #d29922; }\\n  .swim-row-2 .swim-row-label { color: #3fb950; }\\n  .swim-chips { display: flex; gap: 5px; flex-wrap: wrap; }\\n  .swim-chip  { padding: 3px 8px; border-radius: 10px; font-size: 11px; font-weight: 600;\\n                background: rgba(63,185,80,0.12); color: #3fb950; border: 1px solid rgba(63,185,80,0.3); }\\n  .swim-none  { font-size: 11px; color: #8b949e; font-style: italic; padding-top: 3px; }\\n\';\n  document.head.appendChild(s);\n})();\n\n// ── Inject search + sync into header ─────────────────────────────────────────\n(function() {\n  const hr = document.querySelector(\'.header-right\');\n  if (hr) hr.insertAdjacentHTML(\'afterbegin\', \'\\n  <div class="p-search-wrap">\\n    <input class="p-search-input" id="p-search" type="text" placeholder="Add ticker..."\\n           maxlength="10" onkeydown="if(event.key===\\\'Enter\\\') pAddTicker(\\\'1\\\')">\\n    <button class="p-add-s1" onclick="pAddTicker(\\\'1\\\')" title="Add to Stage 1">+S1</button>\\n    <button class="p-add-s2" onclick="pAddTicker(\\\'2\\\')" title="Add to Stage 2">+S2</button>\\n  </div>\\n  <button class="p-sync-btn" onclick="pSyncWatchlist()" title="Sync top signals to Stage 1">&#8635; Sync</button>\\n\');\n})();\n\n// ── Inject ticker tape after header ──────────────────────────────────────────\n(function() {\n  const hdr = document.querySelector(\'header\');\n  if (hdr) hdr.insertAdjacentHTML(\'afterend\', \'<div class="p-tape-wrap"><div class="p-tape-track" id="p-tape">&nbsp;</div></div>\');\n})();\n\n// ── Build ticker tape from signals (with live prices from /api/prices) ───────\nasync function pBuildTape(sigs) {\n  const track = document.getElementById(\'p-tape\');\n  if (!track) return;\n  const items = (sigs || []).filter(s => {\n    const sig = (s.signal || \'\').toUpperCase();\n    return sig === \'BUY\' || sig === \'STRONG_BUY\' || sig === \'SELL\' || sig === \'STRONG_SELL\';\n  });\n  if (!items.length) { track.innerHTML = \'<span class="pt-neu">No signals</span>\'; return; }\n  // Fetch live prices from /api/prices\n  let livePrices = {};\n  try {\n    const tickerStr = items.map(s => s.ticker).join(\',\');\n    livePrices = await fetch(\'/api/prices?tickers=\' + tickerStr).then(r => r.json());\n  } catch(e) { if(window._nwoErr)_nwoErr(e); }\n  const all = [...items, ...items];\n  track.innerHTML = all.map(s => {\n    const sig = (s.signal || \'\').toUpperCase();\n    const bull = sig === \'BUY\' || sig === \'STRONG_BUY\';\n    const cls  = bull ? \'pt-bull\' : \'pt-bear\';\n    const arr  = bull ? \'&#9650;\' : \'&#9660;\';\n    const liveP = livePrices[s.ticker];\n    const displayP = liveP || s.current_price;\n    const price = displayP ? \' $\' + displayP.toFixed(2) : \'\';\n    return \'<span class="\' + cls + \'">\' + arr + \' \' + s.ticker + price + \'</span>\'\n         + \'<span class="pt-sep">|</span>\';\n  }).join(\'\');\n  track.style.animationDuration = Math.max(40, items.length * 0.7) + \'s\';\n}\n\n// ── Compact swim lanes (replace 3-col grid with single box) ──────────────────\nfunction pBuildCompactSwim(sigs) {\n  const swimSec = document.getElementById(\'swim-section\');\n  if (!swimSec) return;\n\n  const MODELS = [\n    { label: \'Standard\',     conf: 0.50,  mos: 0.15,   fud: 0.60 },\n    { label: \'Relaxed -25%\', conf: 0.375, mos: 0.1125, fud: 0.45 },\n    { label: \'Relaxed -50%\', conf: 0.25,  mos: 0.075,  fud: 0.30 },\n  ];\n\n  const rows = MODELS.map((m, i) => {\n    const passing = (sigs || []).filter(s => {\n      const sig = (s.signal || \'\').toUpperCase();\n      return (s.confidence || 0) >= m.conf\n          && (s.margin_of_safety || 0) >= m.mos\n          && (s.fud_score || 0) >= m.fud\n          && (sig === \'BUY\' || sig === \'STRONG_BUY\');\n    }).sort((a, b) => (b.confidence || 0) - (a.confidence || 0));\n\n    const chips = passing.length\n      ? passing.map(s =>\n          \'<span class="swim-chip" title="Conf \' + ((s.confidence||0)*100).toFixed(0) + \'% | MoS \'\n          + ((s.margin_of_safety||0)*100).toFixed(1) + \'%">\' + s.ticker + \'</span>\'\n        ).join(\'\')\n      : \'<span class="swim-none">None clear this bar</span>\';\n\n    return \'<div class="swim-row swim-row-\' + i + \'">\'\n      + \'<div class="swim-row-label">\' + m.label + \'</div>\'\n      + \'<div class="swim-chips">\' + chips + \'</div>\'\n      + \'</div>\';\n  });\n\n  swimSec.querySelector(\'.section-title\').textContent = \'\\ud83d\\udcca Threshold Models\';\n  let body = swimSec.querySelector(\'.swim-compact-wrap\');\n  if (!body) {\n    // Replace old grid with compact wrap\n    const old = swimSec.querySelector(\'.swim-wrap\');\n    if (old) old.remove();\n    body = document.createElement(\'div\');\n    body.className = \'swim-compact-wrap\';\n    swimSec.appendChild(body);\n  }\n  body.innerHTML = rows.join(\'\');\n\n  // Move swim section below sg3-section\n  const sg3Sec = document.getElementById(\'sg3-section\');\n  if (sg3Sec && swimSec.parentNode) {\n    sg3Sec.insertAdjacentElement(\'afterend\', swimSec);\n  }\n}\n\n// ── Search: add ticker to stage 1 or 2 ───────────────────────────────────────\nfunction pAddTicker(toStage) {\n  const inp = document.getElementById(\'p-search\');\n  const ticker = (inp ? inp.value : \'\').trim().toUpperCase();\n  if (!ticker) return;\n  inp.value = \'\';\n  sg3AddTicker(ticker, toStage);\n}\n\nfunction sg3AddTicker(ticker, toStage) {\n  if (!ticker) return;\n  // Remove from all stages first to avoid duplicates\n  [\'stage1\',\'stage2\',\'stage3\'].forEach(k => {\n    if (_sg3[k] && _sg3[k].includes(ticker)) return; // already there\n  });\n  const already = (_sg3.stage1||[]).includes(ticker)\n               || (_sg3.stage2||[]).includes(ticker)\n               || (_sg3.stage3||[]).includes(ticker);\n  if (already) return;\n  (_sg3[\'stage\' + toStage] || []).push(ticker);\n  sg3Render();\n  sg3Save();\n}\nwindow.sg3AddTicker = sg3AddTicker;\nwindow.pAddTicker   = pAddTicker;\n\n// ── Sync watchlist: top-5 bullish I-Tool + all recent signals ─────────────────\nasync function pSyncWatchlist() {\n  const btn = document.querySelector(\'.p-sync-btn\');\n  if (btn) { btn.disabled = true; btn.textContent = \'\\u29d7 Syncing...\'; }\n  try {\n    const [itool, sigs] = await Promise.all([\n      fetch(\'/api/itool\').then(r => r.json()).catch(() => ({})),\n      fetch(\'/api/signals\').then(r => r.json()).catch(() => []),\n    ]);\n\n    const toAdd = new Set();\n\n    // Top 5 bullish from I-Tool\n    const itoolResults = (itool.results || [])\n      .filter(r => r.signal === \'bullish\')\n      .slice(0, 5);\n    itoolResults.forEach(r => toAdd.add(r.ticker));\n\n    // All bullish from recent signals\n    (sigs || []).forEach(s => {\n      const sig = (s.signal || \'\').toUpperCase();\n      if (sig === \'BUY\' || sig === \'STRONG_BUY\') toAdd.add(s.ticker);\n    });\n\n    let added = 0;\n    toAdd.forEach(ticker => {\n      const inAny = (_sg3.stage1||[]).includes(ticker)\n                 || (_sg3.stage2||[]).includes(ticker)\n                 || (_sg3.stage3||[]).includes(ticker);\n      if (!inAny) {\n        (_sg3.stage1 = _sg3.stage1 || []).push(ticker);\n        added++;\n      }\n    });\n\n    if (added > 0) {\n      sg3Render();\n      await sg3Save();\n    }\n    if (btn) btn.textContent = \'\\u2713 Synced +\' + added;\n  } catch(e) {\n    if (btn) btn.textContent = \'Error\';\n  } finally {\n    setTimeout(() => { if (btn) { btn.disabled = false; btn.textContent = \'\\u8635 Sync\'; } }, 3000);\n  }\n}\nwindow.pSyncWatchlist = pSyncWatchlist;\n\n// ── Hook into existing loadSwimLanes / signal load to drive tape + compact swim ─\nconst _origLoadSwimLanes = typeof loadSwimLanes === \'function\' ? loadSwimLanes : null;\nwindow.loadSwimLanes = async function() {\n  if (_origLoadSwimLanes) await _origLoadSwimLanes();\n  try {\n    const sigs = await fetch(\'/api/signals\').then(r => r.json());\n    pBuildTape(sigs);\n    pBuildCompactSwim(sigs);\n  } catch(e) { if(window._nwoErr)_nwoErr(e); }\n};\n\n// Also seed tape immediately from already-loaded _sg3sigs\nsetTimeout(() => {\n  const sigsArr = Object.values(_sg3sigs || {});\n  if (sigsArr.length) {\n    pBuildTape(sigsArr);\n    pBuildCompactSwim(sigsArr);\n  }\n}, 800);\n'
PAPER_JS  = PAPER_JS + _FEAT_JS


# ── Morning Brief auto-regeneration at 06:00 and 09:00 ET ────────────────────
@app.on_event("startup")
async def _schedule_morning_brief():
    import threading, time, logging as _log
    from datetime import datetime
    import pytz

    _slog = _log.getLogger("brief_scheduler")
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
                    _trigger_brief_generation()
                    _slog.info("[BRIEF] Auto-regen triggered at 06:00 ET")
                elif now.hour == 9 and now.minute == 0 and key_9 not in fired_today:
                    fired_today.add(key_9)
                    _trigger_brief_generation()
                    _slog.info("[BRIEF] Auto-regen triggered at 09:00 ET")
                # Morning recap — 08:00 ET: resend most-recent analysis as day-start context
                key_morning_tap = (now.date(), "thesis_morning")
                if now.hour == 8 and now.minute == 0 and key_morning_tap not in fired_today:
                    fired_today.add(key_morning_tap)
                    def _send_morning_recap():
                        try:
                            entry = _load_thesis_analysis()
                            if not entry:
                                return
                            from monitor.telegram_bot import send_alert as _tg
                            from datetime import date as _date
                            today_str = _date.today().strftime("%Y-%m-%d")
                            _tg(
                                f"☀️ <b>NWO Morning Recap — {today_str}</b>\n"
                                f"<i>Prior close analysis ({entry['date']}) · {entry.get('ticker_count',0)} tickers · {entry.get('signal_count',0)} signals</i>\n\n"
                                f"{entry['analysis']}"
                            )
                        except Exception:
                            pass
                    threading.Thread(target=_send_morning_recap, daemon=True,
                                     name="thesis-morning-recap").start()
                    _slog.info("[ThesisAnalysis] Morning recap sent at 08:00 ET")

                # Market-close pattern analysis — 16:45 ET (theses finish generating ~16:35)
                key_close = (now.date(), "thesis_analysis")
                if now.hour == 16 and now.minute == 45 and key_close not in fired_today:
                    fired_today.add(key_close)
                    threading.Thread(target=_run_thesis_analysis, daemon=True,
                                     name="thesis-analysis").start()
                    _slog.info("[ThesisAnalysis] Auto-analysis triggered at 16:45 ET")
                # Prune old keys daily
                today = now.date()
                fired_today = {k for k in fired_today if k and k[0] == today}
            except Exception as e:
                _slog.warning(f"[BRIEF] Scheduler error: {e}")
            time.sleep(30)

    threading.Thread(target=_brief_scheduler, daemon=True).start()
    _slog.info("[BRIEF] Auto-regen scheduler started (06:00 + 09:00 ET daily)")


# ── Paper trading auto-scheduler startup ──────────────────────────────────────
@app.on_event("startup")
async def _start_paper_scheduler():
    import logging as _logging
    _log = _logging.getLogger("paper_scheduler")
    try:
        from paper.auto_scheduler import init_scheduler
        init_scheduler(main_db_url=config.database.url)
        _log.info("[AUTO] Paper scheduler initialised")
    except Exception as e:
        _log.warning(f"[AUTO] Paper scheduler startup failed (will use manual Run Now): {e}")


@app.get("/api/paper/scheduler")
def api_paper_scheduler_status():
    try:
        from paper.auto_scheduler import get_scheduler
        sched = get_scheduler()
        if sched:
            return sched.status()
    except Exception:
        pass
    return {"running": False, "paused": False, "market_hours": False,
            "last_cycle": None, "next_cycle": None, "cycle_count": 0,
            "stop_exits": 0, "engines_ready": False}


@app.post("/api/paper/scheduler/pause")
def api_paper_scheduler_pause():
    try:
        from paper.auto_scheduler import get_scheduler
        sched = get_scheduler()
        if sched:
            sched.pause()
            return {"status": "paused"}
    except Exception:
        pass
    return {"status": "error"}


@app.post("/api/paper/scheduler/resume")
def api_paper_scheduler_resume():
    try:
        from paper.auto_scheduler import get_scheduler
        sched = get_scheduler()
        if sched:
            sched.resume()
            return {"status": "resumed"}
    except Exception:
        pass
    return {"status": "error"}


# ── Fix: search/sync use IIFE-scoped vars — must inject inside IIFE ──────────
_ADD_INSIDE_JS = "\n// sg3AddTicker — inside IIFE: has access to _sg3, sg3Render, sg3Save\nfunction sg3AddTicker(ticker, toStage) {\n  if (!ticker) return;\n  const already = (_sg3.stage1||[]).includes(ticker)\n               || (_sg3.stage2||[]).includes(ticker)\n               || (_sg3.stage3||[]).includes(ticker);\n  if (already) return;\n  (_sg3['stage' + toStage] = _sg3['stage' + toStage] || []).push(ticker);\n  sg3Render();\n  sg3Save();\n}\nwindow.sg3AddTicker = sg3AddTicker;\nwindow._sg3Ref = () => _sg3;\n\n"
_SYNC_NEW_JS   = "    let added = 0;\n    toAdd.forEach(ticker => {\n      const sg3 = window._sg3Ref ? window._sg3Ref() : {};\n      const inAny = (sg3.stage1||[]).includes(ticker)\n                 || (sg3.stage2||[]).includes(ticker)\n                 || (sg3.stage3||[]).includes(ticker);\n      window.sg3AddTicker(ticker, '1');\n      if (!inAny) added++;\n    });\n\n"

# 1. Inject sg3AddTicker + _sg3Ref inside IIFE (before sg3Boot)
PAPER_JS = PAPER_JS.replace(
    'sg3Boot();\n\n})(); // end IIFE',
    _ADD_INSIDE_JS + 'sg3Boot();\n\n})(); // end IIFE'
)

# 2. Remove broken outer sg3AddTicker (references _sg3 which is IIFE-scoped)
_BAD = (
    'function sg3AddTicker(ticker, toStage) {\n'
    '  if (!ticker) return;\n'
    '  // Remove from all stages first to avoid duplicates\n'
    "  ['stage1','stage2','stage3'].forEach(k => {\n"
    "    if (_sg3[k] && _sg3[k].includes(ticker)) return; // already there\n"
    '  });\n'
    '  const already = (_sg3.stage1||[]).includes(ticker)\n'
    '               || (_sg3.stage2||[]).includes(ticker)\n'
    "               || (_sg3.stage3||[]).includes(ticker);\n"
    '  if (already) return;\n'
    "  (_sg3['stage' + toStage] || []).push(ticker);\n"
    '  sg3Render();\n'
    '  sg3Save();\n'
    '}\n'
    'window.sg3AddTicker = sg3AddTicker;\n'
)
PAPER_JS = PAPER_JS.replace(_BAD, '// sg3AddTicker exposed via window from inside IIFE\n')

# 3. Fix pSyncWatchlist: replace direct _sg3 block with window.sg3AddTicker calls
_SYNC_OLD = (
    '    let added = 0;\n'
    '    toAdd.forEach(ticker => {\n'
    '      const inAny = (_sg3.stage1||[]).includes(ticker)\n'
    '                 || (_sg3.stage2||[]).includes(ticker)\n'
    '                 || (_sg3.stage3||[]).includes(ticker);\n'
    '      if (!inAny) {\n'
    "        (_sg3.stage1 = _sg3.stage1 || []).push(ticker);\n"
    '        added++;\n'
    '      }\n'
    '    });\n'
    '\n'
    '    if (added > 0) {\n'
    '      sg3Render();\n'
    '      await sg3Save();\n'
    '    }\n'
)
PAPER_JS = PAPER_JS.replace(_SYNC_OLD, _SYNC_NEW_JS)


# ── Threshold patch ───────────────────────────────────────────────────────────
# 1. Hide swim-wrap + inject threshold bar CSS
PAPER_HTML = PAPER_HTML.replace('</style>', "\n  /* ── Hide old swim-wrap ─────────────────────────────────────── */\n  #swim-wrap, .swim-wrap { display: none !important; }\n  /* ── AI Threshold control bar ──────────────────────────────── */\n  .thresh-bar { display: flex; align-items: center; gap: 10px; padding: 8px 14px;\n                background: #161b22; border: 1px solid #30363d; border-radius: 8px;\n                flex-wrap: wrap; }\n  .thresh-label { font-size: 10px; font-weight: 700; color: #8b949e;\n                  letter-spacing: 0.5px; text-transform: uppercase; white-space: nowrap; }\n  .thresh-grp { display: flex; gap: 3px; }\n  .thresh-btn  { padding: 3px 11px; border-radius: 20px; border: 1px solid #30363d;\n                 background: transparent; color: #8b949e; font-size: 11px; cursor: pointer; }\n  .thresh-btn.t-high { border-color: #f85149; color: #f85149; background: rgba(248,81,73,0.1); }\n  .thresh-btn.t-med  { border-color: #d29922; color: #d29922; background: rgba(210,153,34,0.1); }\n  .thresh-btn.t-low  { border-color: #3fb950; color: #3fb950; background: rgba(63,185,80,0.1); }\n  .thresh-desc { font-size: 10px; color: #8b949e; white-space: nowrap; }\n  .thresh-chips { display: flex; gap: 5px; flex-wrap: wrap; margin-left: auto; }\n  .thresh-chip  { padding: 2px 7px; border-radius: 10px; font-size: 11px; font-weight: 600;\n                  background: rgba(63,185,80,0.12); color: #3fb950;\n                  border: 1px solid rgba(63,185,80,0.3); }\n  .thresh-none  { font-size: 11px; color: #8b949e; font-style: italic; }\n  /* ── Gate toggles in AI modal ───────────────────────────────── */\n  .ai-gate-sect { margin-top: 12px; padding-top: 10px; border-top: 1px solid #21262d; }\n  .ai-gate-title { font-size: 10px; color: #8b949e; font-weight: 700;\n                   letter-spacing: 0.5px; text-transform: uppercase; margin-bottom: 8px; }\n  .gate-row { display: flex; gap: 8px; flex-wrap: wrap; }\n  .gate-tog { display: flex; align-items: center; gap: 5px; cursor: pointer;\n              padding: 4px 9px; border-radius: 6px; border: 1px solid #30363d;\n              background: #0d1117; user-select: none; }\n  .gate-tog:hover { border-color: #58a6ff; }\n  .gate-tog.bypassed { border-color: #d29922; background: rgba(210,153,34,0.08); }\n  .gate-name { font-size: 11px; font-weight: 700; color: #e6edf3; }\n  .gate-tog.bypassed .gate-name { color: #d29922; }\n  .gate-hint { font-size: 10px; color: #8b949e; }\n  .gate-sw { width: 28px; height: 14px; border-radius: 7px; background: #30363d;\n             position: relative; flex-shrink: 0; }\n  .gate-tog.bypassed .gate-sw { background: #d29922; }\n  .gate-sw::after { content: ''; position: absolute; top: 2px; left: 2px;\n                    width: 10px; height: 10px; border-radius: 50%; background: #8b949e; }\n  .gate-tog.bypassed .gate-sw::after { left: 16px; background: #fff; }\n  .gate-locked { opacity: 0.7; cursor: not-allowed !important; }\n  .gate-locked .gate-hint { font-style: italic; }\n" + '</style>', 1)

# 2. Inject gate toggles into AI modal (before modal footer)
PAPER_HTML = PAPER_HTML.replace(
    '<div class="ai-modal-foot">',
    '\n    <div class="ai-gate-sect" id="ai-gate-sect">\n      <div class="ai-gate-title">&#9881; Gate Overrides &mdash; bypass for this ticker</div>\n      <div class="gate-row" id="gate-row"></div>\n    </div>\n' + '<div class="ai-modal-foot">',
    1
)

# 3. Inject threshold + gate JS inside IIFE
_THRESH_IIFE_JS = '\n// ── AI Threshold control ──────────────────────────────────────────────────────\nconst THRESH_CFG = {\n  high: { label:\'High\', re:5.0,  ens:0.550, qst:0.450, rr:1.50, kal:2.50 },\n  med:  { label:\'Med\',  re:5.75, ens:0.468, qst:0.383, rr:1.28, kal:2.88 },\n  low:  { label:\'Low\',  re:6.50, ens:0.385, qst:0.315, rr:1.05, kal:3.25 },\n};\nlet _thresh = localStorage.getItem(\'sg3_thresh\') || \'high\';\nlet _gateOv  = {};\nlet _lastSigs = [];\n\nfunction _threshDesc(t) {\n  const c = THRESH_CFG[t];\n  return \'Re<\' + c.re + \' · Ens>\' + Math.round(c.ens*100) + \'% · QSt>\'\n       + Math.round(c.qst*100) + \'% · R/R>\' + c.rr + \' · Kal<\' + c.kal + \'σ\';\n}\n\nfunction setThreshLevel(lv) {\n  _thresh = lv;\n  localStorage.setItem(\'sg3_thresh\', lv);\n  fetch(\'/api/paper/set-thresh\', {method:\'POST\',\n    headers:{\'Content-Type\':\'application/json\'}, body:JSON.stringify({level:lv})}).catch(()=>{});\n  renderThreshBar();\n}\nwindow.setThreshLevel = setThreshLevel;\nwindow._threshGet = () => THRESH_CFG[_thresh];\n\nfunction renderThreshBar() {\n  const bar = document.getElementById(\'thresh-bar\');\n  if (!bar) return;\n  const c = THRESH_CFG[_thresh];\n  const passing = _lastSigs.filter(s => {\n    const sig = (s.signal||\'\').toUpperCase();\n    return (s.confidence||0) >= c.ens && (sig===\'BUY\'||sig===\'STRONG_BUY\');\n  }).sort((a,b) => (b.confidence||0)-(a.confidence||0));\n  const chips = passing.length\n    ? passing.map(s => \'<span class="thresh-chip" title="Conf \'\n        + Math.round((s.confidence||0)*100) + \'%">\' + s.ticker + \'</span>\').join(\'\')\n    : \'<span class="thresh-none">No signals at this threshold</span>\';\n  bar.innerHTML =\n    \'<span class="thresh-label">⚡ AI Gates:</span>\' +\n    \'<div class="thresh-grp">\' +\n    [\'high\',\'med\',\'low\'].map(lv => {\n      const act = _thresh===lv;\n      return \'<button class="thresh-btn\' + (act?\' t-\'+lv:\'\') + \'" onclick="setThreshLevel(\\\'\' + lv + \'\\\')">\'\n           + (act?\'● \':\'○ \') + THRESH_CFG[lv].label + \'</button>\';\n    }).join(\'\') + \'</div>\' +\n    \'<span class="thresh-desc">\' + _threshDesc(_thresh) + \'</span>\' +\n    \'<div class="thresh-chips">\' + chips + \'</div>\';\n}\nwindow.renderThreshBar = function(sigs) { if(sigs) _lastSigs=sigs; renderThreshBar(); };\n\n// Inject thresh bar after sg3-section once DOM is ready\nsetTimeout(function() {\n  const sg3 = document.getElementById(\'sg3-section\');\n  if (sg3 && !document.getElementById(\'thresh-bar\')) {\n    const el = document.createElement(\'div\');\n    el.id = \'thresh-bar\'; el.className = \'thresh-bar\';\n    sg3.insertAdjacentElement(\'afterend\', el);\n    renderThreshBar();\n  }\n}, 600);\n\n// ── Per-ticker gate overrides ─────────────────────────────────────────────────\nconst GATE_DEFS_BY_MODEL = {\n  standard:     [{key:\'fud\',abbr:\'FUD\',hint:\'FUD news filter\'},{key:\'reynolds\',abbr:\'Re\', hint:\'Reynolds turbulence\'},{key:\'ensemble\',abbr:\'Ens\',hint:\'Ensemble probability\'},{key:\'quantum\',abbr:\'QSt\',hint:\'Quantum state\'},{key:\'rr\',abbr:\'R/R\',hint:\'Risk/reward ratio\'},{key:\'kalman\',abbr:\'Kal\',hint:\'Kalman filter\'}],\n  relaxed:      [{key:\'fud\',abbr:\'FUD\',hint:\'FUD news filter\'},{key:\'reynolds\',abbr:\'Re\', hint:\'Reynolds turbulence\'},{key:\'ensemble\',abbr:\'Ens\',hint:\'Ensemble probability\'},{key:\'quantum\',abbr:\'QSt\',hint:\'Quantum state\'},{key:\'rr\',abbr:\'R/R\',hint:\'Risk/reward ratio\'},{key:\'kalman\',abbr:\'Kal\',hint:\'Kalman filter\'}],\n  very_relaxed: [{key:\'fud\',abbr:\'FUD\',hint:\'FUD news filter\'},{key:\'reynolds\',abbr:\'Re\', hint:\'Reynolds turbulence\'},{key:\'ensemble\',abbr:\'Ens\',hint:\'Ensemble probability\'},{key:\'quantum\',abbr:\'QSt\',hint:\'Quantum state\'},{key:\'rr\',abbr:\'R/R\',hint:\'Risk/reward ratio\'},{key:\'kalman\',abbr:\'Kal\',hint:\'Kalman filter\'}],\n  claude:       [{key:\'vix\',abbr:\'VIX\',hint:\'VIX hard gate (>30=block)\'},{key:\'ensemble\',abbr:\'Ens\',hint:\'Ensemble probability\'},{key:\'rr\',abbr:\'R/R\',hint:\'Risk/reward ratio\'},{key:\'kalman\',abbr:\'Kal\',hint:\'Kalman filter\'},{key:\'reynolds\',abbr:\'Re\',hint:\'Reynolds turbulence\'}],\n};\nfunction _getGateDefs() {\n  var _gm = (typeof window!==\'undefined\' && window._PAPER_MODEL)||\'standard\';\n  return GATE_DEFS_BY_MODEL[_gm] || GATE_DEFS_BY_MODEL.standard;\n}\n\nfunction sgRenderGateToggles(ticker) {\n  const row = document.getElementById(\'gate-row\');\n  if (!row) return;\n  var _gm = (typeof window!==\'undefined\' && window._PAPER_MODEL)||\'standard\';\n  var _govKey = \'sg3_gate_ov_\'+_gm;\n  _gateOv = JSON.parse(localStorage.getItem(_govKey)||\'{}\')\n  const tov = _gateOv[ticker] || [];\n  row.innerHTML = _getGateDefs().map(g => {\n    var locked = (_gm===\'very_relaxed\' && g.key===\'fud\');\n    var by = locked || tov.includes(g.key);\n    var click = locked ? \'\' : \'onclick="sgToggleGate(\\\'\' + ticker + \'\\\',\\\'\' + g.key + \'\\\')"\';\n    var title = locked ? \'Always bypassed on Very Relaxed\' : (by?\'BYPASSED\':\'Active\');\n    return \'<div class="gate-tog\' + (by?\' bypassed\':\'\') + (locked?\' gate-locked\':\'\') + \'" \'\n      + click + \' title="\' + title + \'">\'\n      + \'<div class="gate-sw"></div>\'\n      + \'<span class="gate-name">\' + g.abbr + \'</span>\'\n      + \'<span class="gate-hint">\' + g.hint + (locked?\' ⊘\':\'\') + \'</span>\'\n      + \'</div>\';\n  }).join(\'\');\n}\nwindow.sgRenderGateToggles = sgRenderGateToggles;\n\nfunction sgToggleGate(ticker, key) {\n  var _gm = (typeof window!==\'undefined\' && window._PAPER_MODEL)||\'standard\';\n  var _govKey = \'sg3_gate_ov_\'+_gm;\n  if (!_gateOv[ticker]) _gateOv[ticker] = [];\n  const i = _gateOv[ticker].indexOf(key);\n  if (i===-1) _gateOv[ticker].push(key); else _gateOv[ticker].splice(i,1);\n  if (!_gateOv[ticker].length) delete _gateOv[ticker];\n  localStorage.setItem(_govKey, JSON.stringify(_gateOv));\n  fetch(\'/api/paper/set-gate\', {method:\'POST\',\n    headers:{\'Content-Type\':\'application/json\'}, body:JSON.stringify({model:_gm,overrides:_gateOv})}).catch(()=>{});\n  sgRenderGateToggles(ticker);\n  // Warn if the diagnostic overlay is open for this ticker\n  var _ov = document.getElementById(\'sg-diag-overlay\');\n  if (_ov && _ov.style.display !== \'none\' && window._diagActiveTicker === ticker) {\n    var _snap = JSON.stringify(_gateOv[ticker] || []);\n    if (_snap !== window._diagBypassSnap && !document.getElementById(\'sg-diag-stale\')) {\n      var _w = document.createElement(\'div\');\n      _w.id = \'sg-diag-stale\';\n      _w.style.cssText=\'margin-bottom:12px;padding:8px 12px;background:rgba(210,153,34,0.12);border-radius:6px;border-left:3px solid #d29922;font-size:12px;color:#d29922;\';\n      _w.innerHTML=\'&#9888; Bypasses changed — <button onclick="sgDiagnose(window._diagActiveTicker)" style="background:none;border:none;color:#58a6ff;cursor:pointer;font-size:12px;text-decoration:underline;padding:0;">re-run diagnostic</button>\';\n      var _b=document.getElementById(\'sg-diag-body\');\n      if (_b) _b.insertBefore(_w,_b.firstChild);\n    }\n  }\n}\nwindow.sgToggleGate = sgToggleGate;\n'
PAPER_JS = PAPER_JS.replace(
    'sg3Boot();\n\n})(); // end IIFE',
    _THRESH_IIFE_JS + 'sg3Boot();\n\n})(); // end IIFE'
)

# 4. Hook renderThreshBar into signal loads
PAPER_JS = PAPER_JS.replace(
    'pBuildTape(sigs);\n    pBuildCompactSwim(sigs);',
    'pBuildTape(sigs);\n    pBuildCompactSwim(sigs);\n    if(window.renderThreshBar) renderThreshBar(sigs);'
)
PAPER_JS = PAPER_JS.replace(
    'pBuildTape(sigsArr);\n    pBuildCompactSwim(sigsArr);',
    'pBuildTape(sigsArr);\n    pBuildCompactSwim(sigsArr);\n    if(window.renderThreshBar) renderThreshBar(sigsArr);'
)

# 5. Hook gate toggles into AI modal open
PAPER_JS = PAPER_JS.replace(
    "document.getElementById('ai-overlay').style.display = 'flex';",
    "document.getElementById('ai-overlay').style.display = 'flex';\n"
    "  if(window.sgRenderGateToggles) { var _t=document.getElementById('ai-ticker'); if(_t) sgRenderGateToggles(_t.textContent.trim()); }"
)

# 6. Fix S1/S2 race condition: guard sg3SyncPositions until sg3Boot completes
#    Inject _sg3Ready flag + visibilitychange inside the IIFE
_RACE_FIX_JS = (
    "// ── Race condition fix: don't sync positions until stagegate is loaded ──\n"
    "let _sg3Ready = false;\n\n"
)
PAPER_JS = PAPER_JS.replace(
    '// ── State ─────────────────────────────────────────────────────────────────────\n'
    'let _sg3 = { stage1: [], stage2: [], stage3: [] };\n',
    _RACE_FIX_JS +
    '// ── State ─────────────────────────────────────────────────────────────────────\n'
    'let _sg3 = { stage1: [], stage2: [], stage3: [] };\n'
)

# Mark ready at end of sg3Boot
PAPER_JS = PAPER_JS.replace(
    '  _sg3.stage1 = _sg3.stage1 || [];\n'
    '  _sg3.stage2 = _sg3.stage2 || [];\n'
    '  _sg3.stage3 = _sg3.stage3 || [];\n'
    '  sg3Render();\n'
    '}\n'
    '\n'
    '// Override old sgBoot',
    '  _sg3.stage1 = _sg3.stage1 || [];\n'
    '  _sg3.stage2 = _sg3.stage2 || [];\n'
    '  _sg3.stage3 = _sg3.stage3 || [];\n'
    '  sg3Render();\n'
    '  _sg3Ready = true;  // stagegate loaded — safe to sync positions now\n'
    '}\n'
    '\n'
    '// Override old sgBoot'
)

# Guard the wrapped load() — skip sg3SyncPositions until _sg3Ready
PAPER_JS = PAPER_JS.replace(
    "const _origLoad = load;\n"
    "window.load = async function() {\n"
    "  await _origLoad();\n"
    "  try {\n"
    "    const acct = await fetch('/api/paper/account').then(r => r.json());\n"
    "    sg3SyncPositions(acct.positions || []);\n"
    "  } catch(e) {}\n"
    "};",
    "const _origLoad = load;\n"
    "window.load = async function() {\n"
    "  await _origLoad();\n"
    "  if (!_sg3Ready) return;  // sg3Boot not done yet — skip sync to prevent overwriting stagegate\n"
    "  try {\n"
    "    const acct = await fetch('/api/paper/account').then(r => r.json());\n"
    "    sg3SyncPositions(acct.positions || []);\n"
    "  } catch(e) { if(window._nwoErr)_nwoErr(e); }\n"
    "};"
)

# Add visibilitychange auto-sync and close sell modal on Escape (10b)
PAPER_JS = PAPER_JS.replace(
    "document.addEventListener('keydown', e => {\n"
    "  if (e.key === 'Escape') { sg3SellCancel(); aiClose(); }\n"
    "});",
    "document.addEventListener('keydown', e => {\n"
    "  if (e.key === 'Escape') { sg3SellCancel(); aiClose(); }\n"
    "});\n"
    "\n"
    "// Auto-sync S1/S2 when user tabs back to this page\n"
    "document.addEventListener('visibilitychange', () => {\n"
    "  if (document.visibilityState === 'visible') {\n"
    "    sg3Boot();  // re-fetch stagegate + signals\n"
    "    load();     // re-fetch account + positions\n"
    "  }\n"
    "});"
)


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
    body      = await request.json()
    # Body may be {model, overrides} (new) or a plain overrides dict (legacy)
    if "overrides" in body:
        model     = body.get("model", "standard")
        overrides = body["overrides"]
    else:
        model     = "standard"
        overrides = body
    fname = "gate_overrides.json" if model == "standard" else f"gate_overrides_{model}.json"
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / fname).write_text(_j.dumps(overrides), encoding="utf-8")
    return {"ok": True, "model": model, "file": fname}


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


# ── Stage layout fixes ────────────────────────────────────────────────────────
PAPER_HTML = PAPER_HTML.replace('</style>', "\n  /* ── Pipeline flow header (Sankey-style) ───────────────────── */\n  .sg3-pipeline { display: flex; align-items: stretch; margin-bottom: 10px; gap: 0; }\n  .sg3-pnode { flex: 1; background: #0d1117; border: 1px solid #21262d; border-radius: 8px;\n               padding: 8px 12px; text-align: center; position: relative; }\n  .sg3-pnode-s1 { border-color: #58a6ff; }\n  .sg3-pnode-s2 { border-color: #d29922; }\n  .sg3-pnode-s3 { border-color: #3fb950; }\n  .sg3-pcount { font-size: 24px; font-weight: 700; line-height: 1; }\n  .sg3-pnode-s1 .sg3-pcount { color: #58a6ff; }\n  .sg3-pnode-s2 .sg3-pcount { color: #d29922; }\n  .sg3-pnode-s3 .sg3-pcount { color: #3fb950; }\n  .sg3-pname  { font-size: 10px; color: #8b949e; font-weight: 600; letter-spacing: 0.5px;\n                text-transform: uppercase; margin-top: 3px; }\n  .sg3-pflow  { display: flex; align-items: center; justify-content: center;\n                padding: 0 4px; flex-shrink: 0; position: relative; }\n  .sg3-pflow-line { flex: 1; height: 2px; background: linear-gradient(90deg, #30363d 0%, #30363d 100%);\n                    position: relative; min-width: 28px; }\n  .sg3-pflow-line::after { content: '\\25b6'; position: absolute; right: -6px; top: 50%;\n                            transform: translateY(-50%); color: #30363d; font-size: 10px; }\n  .sg3-pflow-label { position: absolute; top: -16px; left: 50%; transform: translateX(-50%);\n                     font-size: 9px; color: #484f58; white-space: nowrap; font-weight: 600;\n                     letter-spacing: 0.3px; text-transform: uppercase; }\n" + '</style>', 1)

_STAGE_FIXES_JS = '\n// ── Pipeline flow header ──────────────────────────────────────────────────────\nfunction sg3BuildPipeline() {\n  const sg3Sec = document.getElementById(\'sg3-section\');\n  if (!sg3Sec || document.getElementById(\'sg3-pipeline\')) return;\n  const div = document.createElement(\'div\');\n  div.className = \'sg3-pipeline\'; div.id = \'sg3-pipeline\';\n  div.innerHTML =\n    \'<div class="sg3-pnode sg3-pnode-s1">\'\n      + \'<div class="sg3-pcount" id="sg3-pc-1">0</div>\'\n      + \'<div class="sg3-pname">Monitoring</div>\'\n    + \'</div>\'\n    + \'<div class="sg3-pflow"><div class="sg3-pflow-line"></div>\'\n      + \'<span class="sg3-pflow-label">Promote AI</span></div>\'\n    + \'<div class="sg3-pnode sg3-pnode-s2">\'\n      + \'<div class="sg3-pcount" id="sg3-pc-2">0</div>\'\n      + \'<div class="sg3-pname">Active AI</div>\'\n    + \'</div>\'\n    + \'<div class="sg3-pflow"><div class="sg3-pflow-line"></div>\'\n      + \'<span class="sg3-pflow-label">Execute Buy</span></div>\'\n    + \'<div class="sg3-pnode sg3-pnode-s3">\'\n      + \'<div class="sg3-pcount" id="sg3-pc-3">0</div>\'\n      + \'<div class="sg3-pname">Positions</div>\'\n    + \'</div>\';\n  // Insert pipeline header at top of section (after title)\n  const title = sg3Sec.querySelector(\'.section-title\');\n  if (title) title.insertAdjacentElement(\'afterend\', div);\n  else sg3Sec.prepend(div);\n}\nwindow.sg3BuildPipeline = sg3BuildPipeline;\n\nfunction sg3UpdatePipelineCounts() {\n  const c1 = document.getElementById(\'sg3-pc-1\');\n  const c2 = document.getElementById(\'sg3-pc-2\');\n  const c3 = document.getElementById(\'sg3-pc-3\');\n  if (c1) c1.textContent = (_sg3.stage1||[]).length;\n  if (c2) c2.textContent = (_sg3.stage2||[]).length;\n  if (c3) c3.textContent = (_sg3.stage3||[]).length;\n}\nwindow.sg3UpdatePipelineCounts = sg3UpdatePipelineCounts;\n\n// Override sg3Render to also update pipeline counts\nconst _origSg3Render = typeof sg3Render === \'function\' ? sg3Render : null;\nfunction sg3Render() {\n  if (_origSg3Render) _origSg3Render();\n  sg3UpdatePipelineCounts();\n}\nwindow.sg3Render = sg3Render;\n\n// ── Sell validation: only open sell modal if position exists ──────────────────\nfunction sg3OpenSell(ticker, targetStage) {\n  _sg3sellTicker = ticker;\n  _sg3sellTarget = targetStage || \'1\';\n  const pos = _sg3pos[ticker] || {};\n\n  // No open position — just move the card, no sell needed\n  if (!pos.qty || pos.qty <= 0) {\n    const from = Object.keys({\'1\':_sg3.stage1,\'2\':_sg3.stage2,\'3\':_sg3.stage3})\n      .find(k => (_sg3[\'stage\'+k]||[]).includes(ticker));\n    if (from) sg3MoveLocal(ticker, from, _sg3sellTarget);\n    return;\n  }\n\n  document.getElementById(\'sg3-sell-title\').textContent = \'Sell \' + ticker;\n  const price = (_sg3sigs[ticker] || {}).current_price || pos.cur_price || pos.avg_cost || 0;\n  document.getElementById(\'sg3-sell-pos\').textContent =\n    pos.qty + \' shares \\u00b7 avg $\' + (pos.avg_cost||0).toFixed(2) + \' \\u00b7 now $\' + price.toFixed(2);\n  document.getElementById(\'sg3sm-all\').checked = true;\n  document.getElementById(\'sg3-sell-qty\').style.display = \'none\';\n  document.getElementById(\'sg3-sell-qty\').value = \'\';\n  const dest = targetStage === \'1\' ? \'Stage 1 (Monitoring)\' : \'Stage 2 (Active AI)\';\n  document.getElementById(\'sg3-sell-dest\').textContent = \'After sell: move to \' + dest;\n  sg3UpdateSellHint();\n  document.getElementById(\'sg3-sell-overlay\').style.display = \'flex\';\n}\nwindow.sg3OpenSell = sg3OpenSell;\n\n// ── Buy-more guard: warn if already have a position ───────────────────────────\nconst _origSg3BuyConfirm = typeof sg3BuyConfirm === \'function\' ? sg3BuyConfirm : null;\n\nfunction sg3OpenBuy(ticker, targetStage, fromStage) {\n  _sg3pendTicker = ticker;\n  _sg3pendTarget = targetStage || \'3\';\n  _sg3pendFrom   = fromStage || _sg3dragF || null;\n\n  const existing = _sg3pos[ticker];\n  if (existing && existing.qty > 0) {\n    // Already have a position — ask to confirm "buy more"\n    const price = (_sg3sigs[ticker] || {}).current_price || existing.cur_price || 0;\n    const ok = confirm(\n      ticker + \': you already hold \' + existing.qty + \' shares\'\n      + (existing.avg_cost ? \' (avg $\' + existing.avg_cost.toFixed(2) + \')\' : \'\')\n      + \'.\\n\\nBuy MORE shares now? Click OK to continue or Cancel to skip.\'\n    );\n    if (!ok) return;\n  }\n\n  const s = _sg3sigs[ticker] || {};\n  document.getElementById(\'sg3-modal-title\').textContent = \'Buy \' + ticker;\n  document.getElementById(\'sg3-modal-price\').textContent =\n    s.current_price ? \'Current price: $\' + s.current_price.toFixed(2) : \'Price not available\';\n  document.getElementById(\'sg3-mode-shares\').checked = true;\n  document.getElementById(\'sg3-amount\').value = \'\';\n  document.getElementById(\'sg3-modal-hint\').innerHTML = \'&nbsp;\';\n  document.getElementById(\'sg3-overlay\').style.display = \'flex\';\n  setTimeout(() => document.getElementById(\'sg3-amount\').focus(), 60);\n  document.getElementById(\'sg3-act-btn\').onclick = sg3BuyConfirm;\n  document.getElementById(\'sg3-act-btn\').textContent = \'\\u25b6 Buy\' + (existing && existing.qty > 0 ? \' More\' : \'\');\n}\nwindow.sg3OpenBuy = sg3OpenBuy;\n\n// ── Move thresh-bar before sg3-section instead of after ──────────────────────\nconst _origSg3Boot2 = typeof sg3Boot === \'function\' ? sg3Boot : null;\nfunction sg3Boot() {\n  if (_origSg3Boot2) _origSg3Boot2();\n  sg3BuildPipeline();\n  sg3UpdatePipelineCounts();\n  // Relocate thresh-bar to above sg3-section once it exists\n  setTimeout(() => {\n    const bar = document.getElementById(\'thresh-bar\');\n    const sg3 = document.getElementById(\'sg3-section\');\n    if (bar && sg3 && sg3.parentNode) {\n      sg3.parentNode.insertBefore(bar, sg3);\n    }\n  }, 700);\n}\nwindow.sg3Boot = sg3Boot;\n'
PAPER_JS = PAPER_JS.replace(
    'sg3Boot();\n\n})(); // end IIFE',
    _STAGE_FIXES_JS + 'sg3Boot();\n\n})(); // end IIFE'
)


# ── Fix: remove infinite-recursion sg3Render/sg3Boot overrides ───────────────
_BAD_RENDER = (
    "// Override sg3Render to also update pipeline counts\n"
    "const _origSg3Render = typeof sg3Render === 'function' ? sg3Render : null;\n"
    "function sg3Render() {\n"
    "  if (_origSg3Render) _origSg3Render();\n"
    "  sg3UpdatePipelineCounts();\n"
    "}\n"
    "window.sg3Render = sg3Render;\n"
)
PAPER_JS = PAPER_JS.replace(_BAD_RENDER,
    "// sg3Render pipeline-count hook: applied post-IIFE (see window override below)\n")

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
PAPER_JS = PAPER_JS.replace(_BAD_BOOT,
    "// sg3Boot pipeline + thresh-bar hook: applied post-IIFE (see window override below)\n")

# Add safe post-IIFE wrappers after the IIFE closes
_POST_IIFE_JS = "\n// ── Post-IIFE: safe sg3Boot + sg3Render wrappers ──────────────────────────────\n(function() {\n  // Wrap sg3Boot: run original (fetches data + renders), then add pipeline UI\n  var _origBoot = window.sg3Boot;\n  window.sg3Boot = function() {\n    var r = _origBoot && _origBoot();\n    // After original async boot resolves, build pipeline header + move thresh-bar\n    var after = function() {\n      if (window.sg3BuildPipeline) window.sg3BuildPipeline();\n      if (window.sg3UpdatePipelineCounts) window.sg3UpdatePipelineCounts();\n      setTimeout(function() {\n        var bar = document.getElementById('thresh-bar');\n        var sg3e = document.getElementById('sg3-section');\n        if (bar && sg3e && sg3e.parentNode) sg3e.parentNode.insertBefore(bar, sg3e);\n      }, 800);\n    };\n    if (r && typeof r.then === 'function') r.then(after); else after();\n  };\n\n  // Wrap sg3Render: run original, then update pipeline counts\n  var _origRender = window.sg3Render;\n  window.sg3Render = function() {\n    if (_origRender) _origRender();\n    if (window.sg3UpdatePipelineCounts) window.sg3UpdatePipelineCounts();\n  };\n})();\n"
PAPER_JS = PAPER_JS.replace(
    '})(); // end IIFE',
    '})(); // end IIFE\n' + _POST_IIFE_JS
)

# ── Fix sg3Boot override (comment-less match to avoid Unicode dash issues) ────
_BAD_BOOT2 = (
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
PAPER_JS = PAPER_JS.replace(_BAD_BOOT2,
    "// sg3Boot override removed — safe wrap applied post-IIFE\n")

# ── Fix sg3OpenBuy: override uses wrong sg3- element IDs, correct to sg- ──────
_BAD_BUY = (
    "  const s = _sg3sigs[ticker] || {};\n"
    "  document.getElementById('sg3-modal-title').textContent = 'Buy ' + ticker;\n"
    "  document.getElementById('sg3-modal-price').textContent =\n"
    "    s.current_price ? 'Current price: $' + s.current_price.toFixed(2) : 'Price not available';\n"
    "  document.getElementById('sg3-mode-shares').checked = true;\n"
    "  document.getElementById('sg3-amount').value = '';\n"
    "  document.getElementById('sg3-modal-hint').innerHTML = '&nbsp;';\n"
    "  document.getElementById('sg3-overlay').style.display = 'flex';\n"
    "  setTimeout(() => document.getElementById('sg3-amount').focus(), 60);\n"
    "  document.getElementById('sg3-act-btn').onclick = sg3BuyConfirm;\n"
    "  document.getElementById('sg3-act-btn').textContent = '\u25b6 Buy' + (existing && existing.qty > 0 ? ' More' : '');\n"
)
_GOOD_BUY = (
    "  const s = _sg3sigs[ticker] || {};\n"
    "  document.getElementById('sg-modal-title').textContent = 'Buy ' + ticker;\n"
    "  document.getElementById('sg-modal-price').textContent =\n"
    "    s.current_price ? 'Current price: $' + s.current_price.toFixed(2) : 'Price not available';\n"
    "  document.getElementById('sg-mode-shares').checked = true;\n"
    "  document.getElementById('sg-amount').value = '';\n"
    "  document.getElementById('sg-modal-hint').innerHTML = '&nbsp;';\n"
    "  document.getElementById('sg-overlay').style.display = 'flex';\n"
    "  setTimeout(() => document.getElementById('sg-amount').focus(), 60);\n"
    "  document.getElementById('sg-act-btn').onclick = sg3BuyConfirm;\n"
    "  document.getElementById('sg-act-btn').textContent = '\u25b6 Buy' + (existing && existing.qty > 0 ? ' More' : '');\n"
)
PAPER_JS = PAPER_JS.replace(_BAD_BUY, _GOOD_BUY)

# ── Fix sg3OpenBuy: wrong sg3- element ID prefixes (single-line replacements) ─
# The _fix_stage_layout override used sg3- prefix but modal HTML uses sg- prefix
for _old, _new in [
    ("getElementById('sg3-modal-title')", "getElementById('sg-modal-title')"),
    ("getElementById('sg3-modal-price')", "getElementById('sg-modal-price')"),
    ("getElementById('sg3-mode-shares')", "getElementById('sg-mode-shares')"),
    ("getElementById('sg3-amount')",      "getElementById('sg-amount')"),
    ("getElementById('sg3-modal-hint')",  "getElementById('sg-modal-hint')"),
    ("getElementById('sg3-overlay')",     "getElementById('sg-overlay')"),
    ("getElementById('sg3-act-btn')",     "getElementById('sg-act-btn')"),
]:
    PAPER_JS = PAPER_JS.replace(_old, _new)

# ── Efficiency patches (2026-04-13) ──────────────────────────────────────────

# 1. Sticky header in PAPER_HTML (stays visible while scrolling)
PAPER_HTML = PAPER_HTML.replace(
    'header { background: #161b22; padding: 12px 20px; border-bottom: 1px solid #30363d;\n           display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }',
    'header { background: #161b22; padding: 12px 20px; border-bottom: 1px solid #30363d;\n           display: flex; align-items: center; gap: 10px; flex-wrap: wrap;\n           position: sticky; top: 0; z-index: 20; }'
)

# 2. Sticky sched-bar (sits just below sticky header ~50px)
PAPER_HTML = PAPER_HTML.replace(
    'sched-bar" style="background:#0d1117;border-bottom:1px solid #21262d;padding:4px 20px;font-size:11px;color:#8b949e;display:flex;gap:16px;flex-wrap:wrap;"',
    'sched-bar" style="background:#0d1117;border-bottom:1px solid #21262d;padding:4px 20px;font-size:11px;color:#8b949e;display:flex;gap:16px;flex-wrap:wrap;position:sticky;top:50px;z-index:19;"'
)

# 3. Inject shared signal cache into PAPER_JS (deduplicate /api/signals calls)
#    Both loadSwimLanes() and sg3Boot() (IIFE) call /api/signals independently.
#    fetchSignalsOnce() caches the result for 8 s so they share one network hit.
_SIGNALS_CACHE_JS = (
    r"// Shared signal cache — deduplicate /api/signals fetches within same render cycle" + "\n"
    r"let _sigCache = null, _sigCacheTs = 0;" + "\n"
    r"async function fetchSignalsOnce() {" + "\n"
    r"  const now = Date.now();" + "\n"
    r"  if (_sigCache && (now - _sigCacheTs) < 8000) return _sigCache;" + "\n"
    r"  _sigCache = await fetch('/api/signals').then(r => r.json());" + "\n"
    r"  _sigCacheTs = now;" + "\n"
    r"  return _sigCache;" + "\n"
    r"}" + "\n\n"
)
PAPER_JS = PAPER_JS.replace(
    r"async function loadSwimLanes() {",
    _SIGNALS_CACHE_JS + r"async function loadSwimLanes() {"
)

# 4. loadSwimLanes: use cache instead of direct fetch
PAPER_JS = PAPER_JS.replace(
    r"const signals = await fetch('/api/signals').then(r => r.json());",
    r"const signals = await fetchSignalsOnce();"
)

# 5. sg3Boot (inside IIFE, now part of PAPER_JS after merge): use cache
PAPER_JS = PAPER_JS.replace(
    r"const sigs = await fetch('/api/signals').then(r => r.json());\n    (sigs || []).forEach(s => { _sg3sigs[s.ticker] = s; });",
    r"const sigs = await fetchSignalsOnce();\n    (sigs || []).forEach(s => { _sg3sigs[s.ticker] = s; });"
)

# 6. Invalidate cache on Run Now completion so the post-cycle refresh gets fresh data
PAPER_JS = PAPER_JS.replace(
    r"load();\n          loadSwimLanes();\n          if (typeof sg3Boot === 'function') sg3Boot();",
    r"_sigCacheTs = 0;\n          load();\n          loadSwimLanes();\n          if (typeof sg3Boot === 'function') sg3Boot();"
)

# ── Fix: sg3Boot uses real newlines (not literal \n) — correct the cache replace ──
# Prior attempt used raw r"\n" which didn't match; use real \n strings instead
PAPER_JS = PAPER_JS.replace(
    "const sigs = await fetch('/api/signals').then(r => r.json());\n    (sigs || []).forEach(s => { _sg3sigs[s.ticker] = s; });",
    "const sigs = await fetchSignalsOnce();\n    (sigs || []).forEach(s => { _sg3sigs[s.ticker] = s; });"
)

# ── Fix: post-IIFE loadSwimLanes override also fetches signals for pBuildTape ──
PAPER_JS = PAPER_JS.replace(
    "const sigs = await fetch('/api/signals').then(r => r.json());\n    pBuildTape(sigs);",
    "const sigs = await fetchSignalsOnce();\n    pBuildTape(sigs);"
)

# ── Fix: itool+signals Promise.all — signals half uses cache ──
PAPER_JS = PAPER_JS.replace(
    "      fetch('/api/signals').then(r => r.json()).catch(() => []),",
    "      fetchSignalsOnce().catch(() => []),"
)

# ── Fix Run Now "Done" timestamp to show ET ──────────────────────────────────
PAPER_JS = PAPER_JS.replace(
    "new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})",
    "new Date().toLocaleTimeString('en-US', {timeZone:'America/New_York',hour:'2-digit',minute:'2-digit'}) + ' ET'"
)

# ── Layout + tape fixes (2026-04-13) ─────────────────────────────────────────

# 1. Make .p-tape-wrap sticky (tape stays visible while scrolling)
PAPER_JS = PAPER_JS.replace(
    r'.p-tape-wrap  { overflow: hidden; background: #0a0f17;' + '\n' +
    r'                  border-bottom: 1px solid #1f6feb; height: 26px; flex-shrink: 0; }',
    r'.p-tape-wrap  { overflow: hidden; background: #0a0f17;' + '\n' +
    r'                  border-bottom: 1px solid #1f6feb; height: 26px; flex-shrink: 0;' + '\n' +
    r'                  position: sticky; top: 50px; z-index: 18; }'
)

# 2. Sched-bar now sits below header (50px) + tape (26px) = 76px
PAPER_HTML = PAPER_HTML.replace(
    'position:sticky;top:50px;z-index:19;',
    'position:sticky;top:76px;z-index:19;'
)

# 3. Frontend: after any POST to /api/stagegate (add or save), schedule a delayed
#    sg3Boot() so cards refresh once the backend price fetch completes (~5-7s).
#    Intercepts fetch at the page level — runs after IIFE so window.sg3Boot exists.
_STAGEGATE_REFRESH_JS = (
    "\n// Auto-refresh Stage Gate cards after any stagegate save (picks up freshly-fetched prices)\n"
    "(function() {\n"
    "  const _origFetch = window.fetch.bind(window);\n"
    "  window.fetch = function(url, opts) {\n"
    "    const p = _origFetch(url, opts);\n"
    "    if (typeof url === 'string' && url.includes('/api/stagegate') &&\n"
    "        opts && (opts.method||'').toUpperCase() === 'POST') {\n"
    "      p.then(() => setTimeout(() => { if(window.sg3Boot) window.sg3Boot(); }, 7000));\n"
    "    }\n"
    "    return p;\n"
    "  };\n"
    "})();\n"
)
PAPER_JS = PAPER_JS + _STAGEGATE_REFRESH_JS

# ── Fix tape CSS sticky (prior attempt used actual \n; PAPER_JS needs literal \n) ──
PAPER_JS = PAPER_JS.replace(
    r'.p-tape-wrap  { overflow: hidden; background: #0a0f17;\n                  border-bottom: 1px solid #1f6feb; height: 26px; flex-shrink: 0; }',
    r'.p-tape-wrap  { overflow: hidden; background: #0a0f17;\n                  border-bottom: 1px solid #1f6feb; height: 26px; flex-shrink: 0;\n                  position: sticky; top: 50px; z-index: 18; }'
)

# ── Fix C: sg3Boot() — fill missing prices from /api/prices before sg3Render() ──
# _SG3_JS uses real newlines, so match with actual \n not raw r"\n"
# Also make sg3Boot async (it already uses await; need the keyword for correctness)
PAPER_JS = PAPER_JS.replace(
    "async function sg3Boot() {",
    "async function sg3Boot() { /* price-fill enabled */"
)
# Inject price-fill block just before sg3Render() at end of sg3Boot
# _SG3_JS at runtime has REAL newlines (Python evaluates \n in '...' strings)
PAPER_JS = PAPER_JS.replace(
    "  _sg3.stage3 = _sg3.stage3 || [];\n  sg3Render();\n}",
    (
        "  _sg3.stage3 = _sg3.stage3 || [];\n"
        "  // Fill missing prices for stage gate tickers without signals\n"
        "  try {\n"
        "    const _sgAllTix = [...(_sg3.stage1||[]), ...(_sg3.stage2||[]), ...(_sg3.stage3||[])];\n"
        "    const _sgMissing = _sgAllTix.filter(t => !_sg3sigs[t] || !_sg3sigs[t].current_price);\n"
        "    if (_sgMissing.length) {\n"
        "      const _pm = await fetch('/api/prices?tickers=' + _sgMissing.join(',')).then(r => r.json());\n"
        "      Object.entries(_pm).forEach(([t, p]) => {\n"
        "        if (!_sg3sigs[t]) _sg3sigs[t] = { ticker: t };\n"
        "        _sg3sigs[t].current_price = p;\n"
        "      });\n"
        "    }\n"
        "  } catch(e) {}\n"
        "  sg3Render();\n"
        "}"
    )
)

# ── Fix D: pBuildTape() — show all priced tickers, repeat to fill full banner ──
# _FEAT_JS at runtime has REAL newlines (Python evaluates \n in '...' strings)
PAPER_JS = PAPER_JS.replace(
    "pBuildTape(sigs) {\n"
    "  const track = document.getElementById('p-tape');\n"
    "  if (!track) return;\n"
    "  const items = (sigs || []).filter(s => {\n"
    "    const sig = (s.signal || '').toUpperCase();\n"
    "    return sig === 'BUY' || sig === 'STRONG_BUY' || sig === 'SELL' || sig === 'STRONG_SELL';\n"
    "  });\n"
    "  if (!items.length) { track.innerHTML = '<span class=\"pt-neu\">No signals</span>'; return; }\n"
    "  const all = [...items, ...items];",
    (
        "pBuildTape(sigs) {\n"
        "  const track = document.getElementById('p-tape');\n"
        "  if (!track) return;\n"
        "  // All tickers with a price, sorted by signal strength\n"
        "  const _sigOrder = {'STRONG_BUY':0,'BUY':1,'SELL':2,'STRONG_SELL':3};\n"
        "  const items = (sigs || []).filter(s => s.current_price).sort((a,b) => {\n"
        "    const sa = (a.signal||'z').toUpperCase(), sb = (b.signal||'z').toUpperCase();\n"
        "    return (_sigOrder[sa]??9) - (_sigOrder[sb]??9);\n"
        "  });\n"
        "  if (!items.length) { track.innerHTML = '<span class=\"pt-neu\">No data</span>'; return; }\n"
        "  const _repeat = Math.max(2, Math.ceil(40 / items.length));\n"
        "  const all = Array.from({length: _repeat}, () => items).flat();"
    )
)

# ── AI Exit Toggle: global state, loader, and toggle handler ─────────────────
# Appended after the IIFE so these run at page-load time and are accessible
# as window.* properties from inside the IIFE (sg3CardHtml reads window._sg3aiExits).
_AI_EXIT_JS = (
    "\n// ── AI Exit Toggle (Stage 3) ─────────────────────────────────────────────\n"
    "window._sg3aiExits = {};\n"
    "window.sg3LoadAiExits = async function() {\n"
    "  try { window._sg3aiExits = await fetch('/api/paper/ai-exits').then(r => r.json()); }\n"
    "  catch(e) { if(window._nwoErr)_nwoErr(e); }\n"
    "};\n"
    "window.sg3ToggleAiExit = async function(ticker) {\n"
    "  window._sg3aiExits[ticker] = !window._sg3aiExits[ticker];\n"
    "  try {\n"
    "    await fetch('/api/paper/ai-exits', {\n"
    "      method: 'POST',\n"
    "      headers: {'Content-Type': 'application/json'},\n"
    "      body: JSON.stringify({[ticker]: window._sg3aiExits[ticker]})\n"
    "    });\n"
    "  } catch(e) { if(window._nwoErr)_nwoErr(e); }\n"
    "  if (window.sg3Render) window.sg3Render();\n"
    "};\n"
    "// Wrap window.sg3Boot to also load AI exits before rendering\n"
    "(function() {\n"
    "  const _origSg3Boot = window.sg3Boot;\n"
    "  window.sg3Boot = async function() {\n"
    "    await window.sg3LoadAiExits();\n"
    "    if (_origSg3Boot) await _origSg3Boot();\n"
    "  };\n"
    "})();\n"
    "// CSS for AI exit button states\n"
    "(function() {\n"
    "  const s = document.createElement('style');\n"
    "  s.textContent = '.sg3-btn-ai { color: #8b949e; border-color: #30363d; font-size: 10px; }'\n"
    "    + ' .sg3-btn-ai-on { color: #3fb950; border-color: #3fb950; background: rgba(63,185,80,0.1); font-size: 10px; }';\n"
    "  document.head.appendChild(s);\n"
    "})();\n"
)
PAPER_JS = PAPER_JS + _AI_EXIT_JS

# ── AI Exit Toggle: inject button into Stage 3 card (sg3CardHtml) ─────────────
# Use the unique "Sell</button>';\n  }" tail as the target (avoids backslash-quote escaping issues)
PAPER_JS = PAPER_JS.replace(
    "title=\"Sell\">Sell</button>';\n  }",
    "title=\"Sell\">Sell</button>';\n"
    "    const _aiOn = (window._sg3aiExits || {})[ticker] || false;\n"
    "    btns += '<button class=\"sg3-btn sg3-btn-ai' + (_aiOn ? ' sg3-btn-ai-on' : '') + '\" '\n"
    "          + 'data-ticker=\"' + ticker + '\" '\n"
    "          + 'onclick=\"window.sg3ToggleAiExit && window.sg3ToggleAiExit(this.dataset.ticker)\" '\n"
    "          + 'title=\"AI exit ' + (_aiOn ? 'ON — click to disable' : 'OFF — click to enable') + '\">'  \n"
    "          + '\\uD83E\\uDD16 ' + (_aiOn ? 'ON' : 'OFF') + '</button>';\n"
    "  }"
)

# ── Item 1: Info icon on Paper Trading header ─────────────────────────────────
PAPER_HTML = PAPER_HTML.replace(
    '&#127918; Paper Trading</h1>\n  <div class="header-right">',
    '&#127918; Paper Trading &nbsp;<button id="paper-info-btn" onclick="paperShowInfo()" '
    'style="background:none;border:1px solid #58a6ff;color:#58a6ff;border-radius:50%;'
    'width:20px;height:20px;font-size:11px;cursor:pointer;padding:0;line-height:18px;'
    'vertical-align:middle">&#x2139;</button></h1>\n  <div class="header-right">',
)

_PAPER_INFO_JS = """
// ── Paper Trading info modal ──────────────────────────────────────────────────
function paperShowInfo() {
  let m = document.getElementById('paper-info-modal');
  if (!m) {
    m = document.createElement('div');
    m.id = 'paper-info-modal';
    m.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.75);z-index:9999;display:flex;align-items:center;justify-content:center;';
    m.innerHTML = `<div style="background:#161b22;border:1px solid #30363d;border-radius:10px;padding:24px;max-width:520px;width:92%;max-height:80vh;overflow-y:auto">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
        <h2 style="color:#58a6ff;font-size:16px">&#127918; Paper Trading &#8212; How It Works</h2>
        <button onclick="document.getElementById('paper-info-modal').style.display='none'" style="background:none;border:none;color:#8b949e;font-size:20px;cursor:pointer;line-height:1">&#x2715;</button>
      </div>
      <div style="font-size:13px;line-height:1.75;color:#e6edf3">
        <p><strong style="color:#58a6ff">3-Stage Workflow</strong></p>
        <p style="margin-top:8px">&#9312; <strong>Stage 1 &#8212; Monitoring:</strong> Stocks on your watchlist. AI does not trade them. Use the search box or +S1 buttons to add tickers here.</p>
        <p style="margin-top:8px">&#9313; <strong>Stage 2 &#8212; Active AI:</strong> The full 6-layer pipeline runs every 5 min during market hours (9:30am&#8211;4pm ET). The AI may place paper BUY orders.</p>
        <p style="margin-top:8px">&#9314; <strong>Stage 3 &#8212; Open Positions:</strong> Tickers with an open paper position. Stop-loss and take-profit levels are monitored every 60 seconds.</p>
        <hr style="border:none;border-top:1px solid #30363d;margin:14px 0">
        <p><strong style="color:#d29922">&#129504; AI Exit Toggle</strong> &#8212; Each Stage 3 card has a toggle. When ON, the pipeline evaluates exit signals. A SELL or STRONG_SELL triggers a paper SELL of the full position.</p>
        <hr style="border:none;border-top:1px solid #30363d;margin:14px 0">
        <p><strong style="color:#3fb950">Stage 2 + Stage 3 Split</strong> &#8212; A ticker can exist in both Stage 2 and Stage 3 simultaneously. This lets the AI continue evaluating pyramid BUY signals while a position is open.</p>
        <hr style="border:none;border-top:1px solid #30363d;margin:14px 0">
        <p style="color:#8b949e;font-size:11px">Cycle: every 5 min (Mon&#8211;Fri) &middot; Stops: every 60 s &middot; Starting balance: $100,000</p>
      </div>
    </div>`;
    document.body.appendChild(m);
    m.addEventListener('click', function(e) { if (e.target === m) m.style.display = 'none'; });
  } else {
    m.style.display = 'flex';
  }
}
window.paperShowInfo = paperShowInfo;
"""
PAPER_JS = PAPER_JS + _PAPER_INFO_JS


# ── Item 2a: Condense swim lanes from 3 to 2 rows ────────────────────────────
PAPER_JS = PAPER_JS.replace(
    "  const MODELS = [\n"
    "    { label: 'Standard',     conf: 0.50,  mos: 0.15,   fud: 0.60 },\n"
    "    { label: 'Relaxed -25%', conf: 0.375, mos: 0.1125, fud: 0.45 },\n"
    "    { label: 'Relaxed -50%', conf: 0.25,  mos: 0.075,  fud: 0.30 },\n"
    "  ];",
    "  const MODELS = [\n"
    "    { label: 'Standard',     conf: 0.50,  mos: 0.15,   fud: 0.60 },\n"
    "    { label: 'Relaxed -25%', conf: 0.375, mos: 0.1125, fud: 0.45 },\n"
    "  ];",
)


# ── Item 2b: Collapsible trade history grouped by date ────────────────────────
_TRADE_OLD = (
    "      let h = '<table><tr><th>Time</th><th>Ticker</th><th>Action</th>"
    "<th>Qty</th><th>Price</th><th>Total</th><th>Cash After</th><th>Source</th></tr>';\n"
    "      for (const t of trades) {\n"
    "        const dt = t.timestamp\n"
    "          ? new Date(t.timestamp + 'Z').toLocaleString([], {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})\n"
    "          : '\\u2014';\n"
    "        const srcHtml = t.signal === 'MANUAL'\n"
    "          ? '<span style=\"color:#8b949e\">\\ud83d\\udc64 Manual</span>'\n"
    "          : '<span style=\"color:#58a6ff\" title=\"' + (t.signal || 'AI') + '\">\\ud83e\\udd16 AI</span>';\n"
    "        h += '<tr><td class=\"neu\" style=\"font-size:11px\">' + dt + '</td><td><strong>' + t.ticker +\n"
    "          '</strong></td><td class=\"' + (t.action==='BUY'?'up':'dn') + '\">' + t.action +\n"
    "          '</td><td>' + t.qty + '</td><td>' + fmt(t.price) + '</td><td>' + fmt(t.total, 0) +\n"
    "          '</td><td class=\"neu\">' + fmt(t.cash_after, 0) + '</td><td style=\"font-size:11px\">' + srcHtml + '</td></tr>';\n"
    "      }\n"
    "      tw.innerHTML = h + '</table>';\n"
)
_TRADE_NEW = (
    "      const _byDate = {}, _dkeys = [];\n"
    "      for (const t of trades) {\n"
    "        const d = t.timestamp ? new Date(t.timestamp + 'Z') : null;\n"
    "        const dk = d ? String(d.getMonth()+1).padStart(2,'0') + '/'\n"
    "          + String(d.getDate()).padStart(2,'0') + '/' + d.getFullYear() : 'Unknown';\n"
    "        if (!_byDate[dk]) { _byDate[dk] = []; _dkeys.push(dk); }\n"
    "        _byDate[dk].push(Object.assign({}, t, {_d: d}));\n"
    "      }\n"
    "      let h = '<table><tr><th>Time</th><th>Ticker</th><th>Action</th>"
    "<th>Qty</th><th>Price</th><th>Total</th><th>Cash After</th><th>Source</th></tr>';\n"
    "      _dkeys.forEach(function(dk, i) {\n"
    "        const grp = _byDate[dk], exp = i === 0;\n"
    "        h += '<tr class=\"trade-date-hdr\" onclick=\"toggleTradeDate(this)\" data-date-key=\"' + dk\n"
    "           + '\" style=\"cursor:pointer;background:#161b22\">'\n"
    "           + '<td colspan=\"8\" style=\"padding:6px 12px;font-size:11px;color:#8b949e;letter-spacing:.5px\">'\n"
    "           + (exp ? '&#9660;' : '&#9654;') + ' ' + dk\n"
    "           + ' <span style=\"color:#555\">(' + grp.length + ' trade' + (grp.length > 1 ? 's' : '') + ')</span></td></tr>';\n"
    "        grp.forEach(function(t) {\n"
    "          const dt = t._d ? t._d.toLocaleTimeString('en-US', {timeZone:'America/New_York',hour:'2-digit',minute:'2-digit'}) : '\\u2014';\n"
    "          const srcH = t.signal === 'MANUAL'\n"
    "            ? '<span style=\"color:#8b949e\">\\ud83d\\udc64 Manual</span>'\n"
    "            : '<span style=\"color:#58a6ff\" title=\"' + (t.signal || 'AI') + '\">\\ud83e\\udd16 AI</span>';\n"
    "          h += '<tr class=\"trade-date-row\" data-date-parent=\"' + dk + '\" style=\"display:' + (exp ? '' : 'none') + '\">'\n"
    "             + '<td class=\"neu\" style=\"font-size:11px\">' + dt + '</td>'\n"
    "             + '<td><strong>' + t.ticker + '</strong></td>'\n"
    "             + '<td class=\"' + (t.action === 'BUY' ? 'up' : 'dn') + '\">' + t.action + '</td>'\n"
    "             + '<td>' + t.qty + '</td><td>' + fmt(t.price) + '</td>'\n"
    "             + '<td>' + fmt(t.total, 0) + '</td>'\n"
    "             + '<td class=\"neu\">' + fmt(t.cash_after, 0) + '</td>'\n"
    "             + '<td style=\"font-size:11px\">' + srcH + '</td></tr>';\n"
    "        });\n"
    "      });\n"
    "      tw.innerHTML = h + '</table>';\n"
)

if _TRADE_OLD in PAPER_JS:
    PAPER_JS = PAPER_JS.replace(_TRADE_OLD, _TRADE_NEW)
    _TOGGLE_TRADE_JS = """
// ── Toggle trade date groups ──────────────────────────────────────────────────
function toggleTradeDate(hdr) {
  const dk = hdr.dataset.dateKey;
  const rows = document.querySelectorAll('.trade-date-row[data-date-parent="' + dk + '"]');
  if (!rows.length) return;
  const exp = rows[0].style.display === 'none';
  rows.forEach(function(r) { r.style.display = exp ? '' : 'none'; });
  const cell = hdr.querySelector('td');
  if (cell) cell.innerHTML = cell.innerHTML.replace(exp ? '&#9654;' : '&#9660;', exp ? '&#9660;' : '&#9654;');
}
"""
    PAPER_JS = PAPER_JS + _TOGGLE_TRADE_JS
else:
    import sys as _sys
    print('[WARN] Item 2b: trade history target string not found in PAPER_JS', file=_sys.stderr)

# ── Shared nav: inject into Paper page ───────────────────────────────────────
PAPER_HTML = PAPER_HTML.replace('</style>', _NAV_CSS + '</style>', 1)
PAPER_HTML = PAPER_HTML.replace(
    '<a href="/" class="back-btn">&#8592; Dashboard</a>',
    _nav_html('paper'),
    1
)

# ── Model-switcher bar: injected just before </header> in PAPER_HTML ─────────
_MODEL_NAV = (
    '<div id="model-nav" style="display:flex;gap:6px;margin-left:auto;align-items:center;">'
    '<span style="font-size:10px;color:#8b949e;letter-spacing:.5px;text-transform:uppercase;">Model:</span>'
    '<a href="/paper" id="mnav-standard" style="padding:3px 10px;border-radius:4px;border:1px solid #30363d;'
    'background:#21262d;color:#8b949e;text-decoration:none;font-size:11px;">Standard</a>'
    '<a href="/paper/relaxed" id="mnav-relaxed" style="padding:3px 10px;border-radius:4px;border:1px solid #30363d;'
    'background:#21262d;color:#8b949e;text-decoration:none;font-size:11px;">Relaxed \u221225%</a>'
    '<a href="/paper/very-relaxed" id="mnav-very-relaxed" style="padding:3px 10px;border-radius:4px;border:1px solid #30363d;'
    'background:#21262d;color:#8b949e;text-decoration:none;font-size:11px;">Very Relaxed \u221250%</a>'
    '<a href="/paper/claude" id="mnav-claude" style="padding:3px 10px;border-radius:4px;border:1px solid #30363d;'
    'background:#21262d;color:#8b949e;text-decoration:none;font-size:11px;">\U0001f916 Claude</a>'
    '<a href="/paper/compare" style="padding:3px 10px;border-radius:4px;border:1px solid #30363d;'
    'background:#21262d;color:#8b949e;text-decoration:none;font-size:11px;">&#128200; Compare</a>'
    '</div>'
)
PAPER_HTML = PAPER_HTML.replace('</header>', _MODEL_NAV + '</header>', 1)

# ── Fix 1: Stage 3 card — fall back to pos.cur_price when signal price missing ─
_SG3_PRICE_OLD = "  const price = s.current_price ? '$' + s.current_price.toFixed(2) : '';"
_SG3_PRICE_NEW = (
    "  const _curP = s.current_price || pos.cur_price;\n"
    "  const price = _curP ? '$' + Number(_curP).toFixed(2) : '';"
)
if _SG3_PRICE_OLD in PAPER_JS:
    PAPER_JS = PAPER_JS.replace(_SG3_PRICE_OLD, _SG3_PRICE_NEW)
else:
    import sys as _sys; print('[WARN] sg3 price fallback patch: target not found', file=_sys.stderr)

# ── Fix 1b: sgCardHtml (old 2-stage) wrongly got pos.cur_price — remove it ────
# sgCardHtml has no `pos` variable; strip the fallback using unique trailing context
_SGCARD_BAD  = (
    "  const _curP = s.current_price || pos.cur_price;\n"
    "  const price = _curP ? '$' + Number(_curP).toFixed(2) : '';\n"
    "  // Use data-t"
)
_SGCARD_GOOD = (
    "  const _curP = s.current_price;\n"
    "  const price = _curP ? '$' + Number(_curP).toFixed(2) : '';\n"
    "  // Use data-t"
)
if _SGCARD_BAD in PAPER_JS:
    PAPER_JS = PAPER_JS.replace(_SGCARD_BAD, _SGCARD_GOOD)
else:
    import sys as _sys; print('[WARN] sgCardHtml pos.cur_price fix: target not found', file=_sys.stderr)

# ── Multi-model fetch interceptor (prepend so it runs before any fetch call) ──
_FETCH_INTERCEPTOR = (
    "// ── Multi-model API routing ───────────────────────────────────────────────────\n"
    "(function(){\n"
    "  var _m = (typeof window !== 'undefined' && window._PAPER_MODEL) || 'standard';\n"
    "  if (_m !== 'standard') {\n"
    "    var _of = window.fetch;\n"
    "    window.fetch = function(url, opts) {\n"
    "      if (typeof url === 'string' && url.startsWith('/api/paper/')) {\n"
    "        url = url + (url.indexOf('?') >= 0 ? '&' : '?') + 'model=' + encodeURIComponent(_m);\n"
    "      }\n"
    "      return _of.call(this, url, opts);\n"
    "    };\n"
    "  }\n"
    "})();\n\n"
)
PAPER_JS = _FETCH_INTERCEPTOR + PAPER_JS

# ── Fix 2: AI exit toggle — load persisted state immediately on page load ─────
# sg3Boot() already ran inside the IIFE before _AI_EXIT_JS wrapped it, so
# _sg3aiExits is {} on first render. This snippet loads the saved state right away.
PAPER_JS = PAPER_JS + (
    "\n// ── Fix: load AI exit toggle state immediately on page load ─────────────────\n"
    "(function() {\n"
    "  function _initAiExits() {\n"
    "    if (window.sg3LoadAiExits && window.sg3Render) {\n"
    "      window.sg3LoadAiExits().then(function() { window.sg3Render(); });\n"
    "    } else { setTimeout(_initAiExits, 150); }\n"
    "  }\n"
    "  _initAiExits();\n"
    "})();\n"
)

# ── Fix 3: Stage 3 card — add daily P&L (today's $ change and %) ─────────────
_SG3_DAILY_DECL_OLD = "  let meta = price;\n  let pnlHtml = '';"
_SG3_DAILY_DECL_NEW = "  let meta = price;\n  let pnlHtml = '';\n  let dailyHtml = '';"
if _SG3_DAILY_DECL_OLD in PAPER_JS:
    PAPER_JS = PAPER_JS.replace(_SG3_DAILY_DECL_OLD, _SG3_DAILY_DECL_NEW)
else:
    import sys as _sys; print('[WARN] sg3 dailyHtml decl patch: target not found', file=_sys.stderr)

_SG3_DAILY_CALC_OLD = (
    "    pnlHtml = '<span class=\"sg3-pnl ' + pnlCls + '\">' + pnlStr + ' (' + pctStr + ')</span>';\n"
    "  }"
)
_SG3_DAILY_CALC_NEW = (
    "    pnlHtml = '<span class=\"sg3-pnl ' + pnlCls + '\">' + pnlStr + ' (' + pctStr + ')</span>';\n"
    "    const _chgP = s.change_pct;\n"
    "    if (_chgP != null) {\n"
    "      const _dP = s.current_price || pos.cur_price || pos.avg_cost || 0;\n"
    "      const _dayDol = pos.qty * _dP * (_chgP / 100);\n"
    "      const _dayCls = _dayDol >= 0 ? 'up' : 'dn';\n"
    "      const _dayStr = (_dayDol >= 0 ? '+' : '') + '$' + Math.abs(_dayDol).toFixed(0);\n"
    "      const _dayPct = (_chgP >= 0 ? '+' : '') + Number(_chgP).toFixed(2) + '%';\n"
    "      dailyHtml = '<span class=\"sg3-pnl ' + _dayCls + '\" style=\"font-size:9px;opacity:0.8\">Day ' + _dayStr + ' (' + _dayPct + ')</span>';\n"
    "    }\n"
    "  }"
)
if _SG3_DAILY_CALC_OLD in PAPER_JS:
    PAPER_JS = PAPER_JS.replace(_SG3_DAILY_CALC_OLD, _SG3_DAILY_CALC_NEW)
else:
    import sys as _sys; print('[WARN] sg3 dailyHtml calc patch: target not found', file=_sys.stderr)

_SG3_DAILY_RENDER_OLD = "    + pnlHtml\n    + statusHtml\n    + '<span class=\"sg3-sig '"
_SG3_DAILY_RENDER_NEW = "    + pnlHtml\n    + dailyHtml\n    + statusHtml\n    + '<span class=\"sg3-sig '"
if _SG3_DAILY_RENDER_OLD in PAPER_JS:
    PAPER_JS = PAPER_JS.replace(_SG3_DAILY_RENDER_OLD, _SG3_DAILY_RENDER_NEW)
else:
    import sys as _sys; print('[WARN] sg3 dailyHtml render patch: target not found', file=_sys.stderr)

# ── Stage 1/2 daily change display ────────────────────────────────────────────
# After Fix 3 the if(stage==='3') block has dailyHtml for stage 3.
# Add an else block so Stage 1 and Stage 2 cards also show today's move.
_SG3_12_DAILY_OLD = (
    "      dailyHtml = '<span class=\"sg3-pnl ' + _dayCls + '\" style=\"font-size:9px;opacity:0.8\">Day ' + _dayStr + ' (' + _dayPct + ')</span>';\n"
    "    }\n"
    "  }\n"
    "\n"
    "  let statusHtml = '';"
)
_SG3_12_DAILY_NEW = (
    "      dailyHtml = '<span class=\"sg3-pnl ' + _dayCls + '\" style=\"font-size:9px;opacity:0.8\">Day ' + _dayStr + ' (' + _dayPct + ')</span>';\n"
    "    }\n"
    "  } else {\n"
    "    const _chgP12 = s.change_pct;\n"
    "    if (_chgP12 != null && _curP) {\n"
    "      const _dayDol12 = _curP * (_chgP12 / 100);\n"
    "      const _dayCls12 = _dayDol12 >= 0 ? 'up' : 'dn';\n"
    "      const _dayStr12 = (_dayDol12 >= 0 ? '+' : '') + '$' + Math.abs(_dayDol12).toFixed(2);\n"
    "      const _dayPct12 = (_chgP12 >= 0 ? '+' : '') + Number(_chgP12).toFixed(2) + '%';\n"
    "      dailyHtml = '<span class=\"sg3-pnl ' + _dayCls12 + '\">' + _dayStr12 + ' (' + _dayPct12 + ')</span>';\n"
    "    }\n"
    "  }\n"
    "\n"
    "  let statusHtml = '';"
)
if _SG3_12_DAILY_OLD in PAPER_JS:
    PAPER_JS = PAPER_JS.replace(_SG3_12_DAILY_OLD, _SG3_12_DAILY_NEW)
else:
    import sys as _sys; print('[WARN] sg3 stage1/2 daily patch: target not found', file=_sys.stderr)

# ── Model nav active-link highlight ──────────────────────────────────────────
PAPER_JS = PAPER_JS + (
    "\n// ── Highlight active model in model-nav bar ─────────────────────────────────\n"
    "(function(){\n"
    "  var _m = window._PAPER_MODEL || 'standard';\n"
    "  var _map = { standard: 'mnav-standard', relaxed: 'mnav-relaxed', very_relaxed: 'mnav-very-relaxed', claude: 'mnav-claude' };\n"
    "  var el = document.getElementById(_map[_m]);\n"
    "  if (el) { el.style.borderColor = '#58a6ff'; el.style.color = '#58a6ff';\n"
    "             el.style.background = 'rgba(88,166,255,0.12)'; }\n"
    "})();\n"
)

# ── /api/sg-quotes endpoint — live intraday price when market open, else EOD ──
@app.get("/api/sg-quotes")
def api_sg_quotes(tickers: str = ""):
    """Return {ticker: {price, change_pct, change_dollar}} — live during market hours, else EOD."""
    if not tickers:
        return {}
    ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    result = {}
    # Live prices first
    for ticker in ticker_list:
        live = _live_price(ticker)
        if live:
            chg_dollar = None
            if live.get("prev_close") and live["price"]:
                chg_dollar = round(live["price"] - live["prev_close"], 2)
            result[ticker] = {
                "price":        live["price"],
                "change_pct":   live.get("change_pct"),
                "change_dollar": chg_dollar,
                "live":         True,
            }
    # EOD fallback
    remaining = [t for t in ticker_list if t not in result]
    if remaining:
        try:
            from models.database import init_db as _init_db, PriceHistory, Company
            _, _Session = _init_db(config.database.url, echo=False)
            with _Session() as s:
                from collections import defaultdict as _dd
                rows = (
                    s.query(Company.ticker, PriceHistory.close, PriceHistory.date)
                    .join(PriceHistory, PriceHistory.company_id == Company.id)
                    .filter(Company.ticker.in_(remaining))
                    .order_by(Company.ticker, PriceHistory.date.desc())
                    .all()
                )
                by_ticker = _dd(list)
                for ticker, close, date in rows:
                    if len(by_ticker[ticker]) < 2 and close:
                        by_ticker[ticker].append(float(close))
                for ticker, prices in by_ticker.items():
                    if not prices:
                        continue
                    price = prices[0]
                    change_dollar = round(prices[0] - prices[1], 2) if len(prices) >= 2 and prices[1] else 0
                    change_pct = round(change_dollar / prices[1] * 100, 2) if len(prices) >= 2 and prices[1] else 0
                    result[ticker] = {"price": round(price, 2), "change_pct": change_pct, "change_dollar": change_dollar}
        except Exception:
            pass
    return result

# ── Patch sg3Boot to fetch sg-quotes for stage gate tickers missing price data ─
_SG3_BOOT_QUOTES_OLD = (
    "  _sg3.stage3 = _sg3.stage3 || [];\n"
    "  try { _sg3TrData = await fetch('/api/tipranks/all').then(r => r.json()); } catch(e) {}\n"
    "  sg3Render();\n"
    "}"
)
_SG3_BOOT_QUOTES_NEW = (
    "  _sg3.stage3 = _sg3.stage3 || [];\n"
    "  try { _sg3TrData = await fetch('/api/tipranks/all').then(r => r.json()); } catch(e) { if(window._nwoErr)_nwoErr(e); }\n"
    "  // Fetch price + daily change for any stage gate ticker missing signal data\n"
    "  try {\n"
    "    const _allSgT = [...new Set([..._sg3.stage1, ..._sg3.stage2, ..._sg3.stage3])];\n"
    "    const _missT = _allSgT.filter(t => !_sg3sigs[t] || _sg3sigs[t].current_price == null);\n"
    "    if (_allSgT.length) {\n"
    "      const _tq = _missT.length ? _missT : _allSgT;\n"
    "      const _sq = await fetch('/api/sg-quotes?tickers=' + _tq.join(',')).then(r => r.json());\n"
    "      Object.entries(_sq).forEach(([t, q]) => {\n"
    "        if (!_sg3sigs[t]) _sg3sigs[t] = {ticker: t};\n"
    "        if (!_sg3sigs[t].current_price && q.price) _sg3sigs[t].current_price = q.price;\n"
    "        if (_sg3sigs[t].change_pct == null && q.change_pct != null) _sg3sigs[t].change_pct = q.change_pct;\n"
    "        if (_sg3sigs[t].change_dollar == null && q.change_dollar != null) _sg3sigs[t].change_dollar = q.change_dollar;\n"
    "      });\n"
    "    }\n"
    "  } catch(e) { if(window._nwoErr)_nwoErr(e); }\n"
    "  sg3Render();\n"
    "}"
)
if _SG3_BOOT_QUOTES_OLD in PAPER_JS:
    PAPER_JS = PAPER_JS.replace(_SG3_BOOT_QUOTES_OLD, _SG3_BOOT_QUOTES_NEW)
else:
    import sys as _sys; print('[WARN] sg3Boot quotes patch: target not found', file=_sys.stderr)

# ── Fix: pBuildTape — show ALL tickers deduped, change_pct arrows ─────────────
# Override the function defined in _FEAT_JS which only showed BUY/SELL signals.
PAPER_JS = PAPER_JS + """
// ── Override pBuildTape: show all tickers, dedup, change_pct arrows ───────────
window.pBuildTape = async function(sigs) {
  const track = document.getElementById('p-tape');
  if (!track) return;
  const byT = new Map();
  (sigs || []).forEach(s => { if (!byT.has(s.ticker)) byT.set(s.ticker, s); });
  const items = [...byT.values()];
  if (!items.length) { track.innerHTML = '<span class="pt-neu">No signals</span>'; return; }
  const all = [...items, ...items];
  track.innerHTML = all.map(s => {
    const sig = (s.signal || '').toUpperCase();
    const bull = sig === 'BUY' || sig === 'STRONG_BUY';
    const bear = sig === 'SELL' || sig === 'STRONG_SELL';
    const chg  = s.change_pct;
    const up   = chg != null ? chg > 0 : null;
    let cls, arr;
    if (bull)             { cls = 'pt-bull';    arr = '&#9650;'; }
    else if (bear)        { cls = 'pt-bear';    arr = '&#9660;'; }
    else if (up === true) { cls = 'pt-chg-up';  arr = '&#9650;'; }
    else if (up === false){ cls = 'pt-chg-dn';  arr = '&#9660;'; }
    else                  { cls = 'pt-neu';     arr = '&#8212;'; }
    const p = s.live_price || s.current_price;
    return '<span class="' + cls + '">' + arr + ' ' + s.ticker + (p ? ' $' + Number(p).toFixed(2) : '') + '</span>'
         + '<span class="pt-sep">|</span>';
  }).join('');
  track.style.animationDuration = Math.max(40, items.length * 0.8) + 's';
};
// Seed tape: try _sg3sigs first (fast), fall back to /api/signals fetch
setTimeout(function() {
  const sigsArr = Object.values(window._sg3sigs || {});
  if (sigsArr.length) {
    window.pBuildTape(sigsArr);
  } else {
    fetch('/api/signals').then(r => r.json()).then(function(sigs) {
      if (sigs && sigs.length) window.pBuildTape(sigs);
    }).catch(function() {});
  }
}, 600);
"""

# ── Paper Trade sticky banner wrapper ─────────────────────────────────────────
PAPER_JS = PAPER_JS + """
// ── Sticky wrapper: header + p-tape-wrap ─────────────────────────────────────
(function() {
  var h = document.querySelector('header');
  if (!h || h.closest('.sticky-banner')) return;
  var t = h.nextElementSibling;
  var isTape = t && t.className && t.className.indexOf('tape') >= 0;
  var w = document.createElement('div'); w.className = 'sticky-banner';
  h.parentNode.insertBefore(w, h); w.appendChild(h);
  if (isTape) w.appendChild(t);
  // Page-info bar for Paper Trade
  var INFO = '<b>Paper Trade</b> \u2014 $100k virtual account. Stage\u00a01: Monitoring. Stage\u00a02: Active AI auto-buys every 5\u00a0min (market hours). Stage\u00a03: Open positions with stop-loss & take-profit every 60s. AI exit toggle per position. Threshold model swim lanes.';
  var bar = document.createElement('div');
  bar.className = 'page-info-bar pib-collapsed'; bar.id = 'page-info-bar';
  var togBtn = document.createElement('button');
  togBtn.className = 'page-info-toggle';
  togBtn.innerHTML = '\u2139\uFE0F About this page<span class="pib-arrow">&#9660;</span>';
  togBtn.onclick = function() { bar.classList.toggle('pib-collapsed'); };
  var content = document.createElement('div');
  content.className = 'page-info-content';
  content.innerHTML = INFO;
  bar.appendChild(togBtn);
  bar.appendChild(content);
  w.insertAdjacentElement('afterend', bar);
})();
"""

# ── Deferred re-load: ensures sg3SyncPositions runs after all wrappers are applied
# The initial load() call in PAPER_JS fires before _SG3_JS wraps window.load,
# so positions never sync on first render. This fires 400ms later with the full wrapper chain.
PAPER_JS = PAPER_JS + (
    "\n// ── Deferred sync: run after all JS wrappers applied ────────────────────────\n"
    "setTimeout(function() {\n"
    "  if (typeof load === 'function') load();\n"
    "}, 400);\n"
)

# ═══════════════════════════════════════════════════════════════════════
# Phase 1a — /api/trade-diagnostic endpoint
# Runs the full L2→L4 pipeline dry for a single ticker and returns
# a structured JSON showing exactly which gate passed or failed.
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/trade-diagnostic")
def api_trade_diagnostic(ticker: str, model: str = "standard"):
    """
    Dry-run the full AI pipeline for a single ticker using the specified model's
    DecisionEngine, aggregator, and gate override file.
    """
    ticker = ticker.upper().strip()
    model  = model.lower().strip()
    try:
        from paper.auto_scheduler import get_scheduler
        sched = get_scheduler()
        if sched is None or not sched._engines_ready:
            return JSONResponse(
                {"error": "Scheduler not ready — start the dashboard and wait for engine init"},
                status_code=503
            )

        # Load price data with live intraday quote injected as today's bar
        from paper.runner import _load_price_data as _lpd, _fetch_live_quotes as _flq
        _live_q   = _flq([ticker])
        price_data    = _lpd(sched._Session, ticker, live_quotes=_live_q)
        closes        = price_data.get("closes", [])
        highs         = price_data.get("highs", [])
        lows          = price_data.get("lows", [])
        volumes       = price_data.get("volumes", [])
        current_price = price_data.get("current_price")
        cik           = price_data.get("cik", "")

        # L2: fundamental analysis
        analysis = sched._analysis_engine.analyze_ticker(ticker)
        if not analysis:
            return {"ticker": ticker, "error": "No analysis data — ticker may not be in DB"}

        # Optional signals
        fft  = sched._fft.analyze(ticker, closes)                          if len(closes) >= 64  else None
        fib  = sched._fib.analyze(ticker, highs, lows, closes)             if len(closes) >= 30  else None
        ins  = sched._insider.score(ticker, cik, current_price)            if cik else None
        vwap = sched._vwap.compute_daily(ticker, highs, lows, closes, volumes) if len(closes) >= 5  else None
        vol  = sched._vol.analyze(ticker, highs, lows, closes, volumes)    if len(closes) >= 10 else None

        # VIX
        try:
            vix_q = sched._market_data.get_quote("$VIX")
            vix_lvl = float(vix_q["last_price"]) if vix_q and vix_q.get("last_price") else 20.0
        except Exception:
            vix_lvl = 20.0
        vix_regime = sched._vix.classify(vix_lvl)

        # I-Tool cache
        import json as _j
        from pathlib import Path as _P
        _it = {}
        try:
            _ic = _P("data/itool_scan.json")
            if _ic.exists():
                for r in _j.loads(_ic.read_text(encoding="utf-8")).get("results", []):
                    if r.get("ticker") and r.get("signal"):
                        _it[r["ticker"]] = r["signal"]
        except Exception:
            pass

        # Resolve which model to use
        _model_dict = next(
            (m for m in sched._paper_models if m["name"] == model),
            sched._paper_models[0]
        )
        _agg_instance = _model_dict.get("aggregator") or sched._aggregator
        _de           = _model_dict["decision_engine"]

        # Load model-specific gate overrides
        gate_overrides = {}
        _go_name = "gate_overrides.json" if model == "standard" else f"gate_overrides_{model}.json"
        try:
            _go = _P("data") / _go_name
            if _go.exists():
                gate_overrides = _j.loads(_go.read_text(encoding="utf-8"))
        except Exception:
            pass
        bypasses = set(gate_overrides.get(ticker, []))
        # Very Relaxed always bypasses FUD for all tickers (model-level policy)
        if model == "very_relaxed":
            bypasses.add("fud")

        # For Claude model: if VIX bypass is active, use a neutral VIX so the
        # aggregator's hard gate doesn't block the diagnostic
        _diag_vix_lvl = vix_lvl
        if model == "claude" and "vix" in bypasses:
            _diag_vix_lvl = 15.0
            vix_regime = sched._vix.classify(_diag_vix_lvl)

        # L2 aggregate — use model's aggregator with correct buy threshold for this model
        _bt  = _model_dict.get("buy_threshold", 0.10)
        _st  = sched._st_analyzer.analyze(ticker, highs, lows, closes, volumes) if (sched._st_analyzer and len(closes) >= 20) else None
        _tga = sched._tga_analyzer.analyze(ticker, closes, highs, lows, volumes) if (hasattr(sched, '_tga_analyzer') and len(closes) >= 35) else None
        _mom = sched._momentum_analyzer.analyze(ticker, closes, volumes=volumes) if (hasattr(sched, '_momentum_analyzer') and len(closes) >= 30) else None
        agg = _agg_instance.aggregate(
            analysis=analysis, fft=fft, fib=fib, insider=ins,
            vwap=vwap, vol_profile=vol, vix_regime=vix_regime,
            current_price=current_price or analysis.current_price,
            itool_signal=_it.get(ticker),
            momentum=_mom, supertrend=_st, tga=_tga,
            buy_threshold_override=_bt,
        )

        # L3 FUD
        l3 = sched._fud.analyze_ticker(ticker, agg)

        # L4 Decision — use model's DecisionEngine with model-specific bypasses
        decision = _de.decide(l3, portfolio_value=100_000, bypass_gates=bypasses)

        scores = {
            "fundamentals": round(agg.fundamentals_score, 3),
            "momentum":     round(agg.momentum_score, 3),
            "insider":      round(agg.insider_score, 3),
            "technical":    round(agg.technical_score, 3),
            "supertrend":   round(agg.supertrend_favourability * 2 - 1, 3),
            "tipranks":     round(agg.tipranks_score, 3),
            "3GA":          round({3: 1.0, 2: 0.4, 1: 0.0, 0: -0.2}.get(agg.tga_arrows_count, 0.0), 3),
            "cycle":        round(agg.cycle_score, 3),
            "volume":       round(agg.volume_score, 3),
        }
        tga_info = {
            "arrows": agg.tga_arrows_count,
            "signal": agg.tga_signal,
            "sma":    agg.tga_sma_arrow,
            "macd":   agg.tga_macd_arrow,
            "stoch":  agg.tga_stoch_arrow,
            "vol":    agg.tga_volume_spike,
            "reason": agg.tga_reason,
        }

        return {
            "ticker":          ticker,
            "model":           model,
            "investable":      analysis.is_investable,
            "investable_reasons": analysis.investable_reasons,
            "moat":            analysis.moat_strength,
            "margin_of_safety": round(analysis.margin_of_safety or 0, 3),
            "roic":            round(analysis.roic or 0, 4),
            "wacc":            round(analysis.wacc or 0, 4),
            "price_days":      len(closes),
            "current_price":   current_price,
            "vix":             round(vix_lvl, 1),
            "vix_regime":      vix_regime.regime,
            "scores":          scores,
            "composite_score": round(agg.composite_score, 4),
            "signal_l2":       agg.signal,
            "fud_passed":      l3.proceed_to_execution,
            "fud_score":       round(l3.fud_analysis.avg_fud_score if l3.fud_analysis else 0, 3),
            "fud_adjusted_signal": l3.adjusted_signal,
            "gate_results": {
                "reynolds":  {"pass": not any("Reynolds: EXTREME" in g for g in decision.gates_failed), "regime": decision.reynolds_regime, "re": round(decision.reynolds_number, 2)},
                "quantum":   {"pass": not any("Quantum:" in g for g in decision.gates_failed), "state": decision.quantum_dominant_state, "certainty": round(decision.quantum_certainty, 3)},
                "ensemble":  {"pass": not any("Ensemble:" in g for g in decision.gates_failed), "p_bull": round(decision.ensemble_probability_bull, 3)},
                "rr":        {"pass": not any("Risk/Reward:" in g for g in decision.gates_failed), "ratio": round(decision.risk_reward_ratio or 0, 2)},
                "kalman":    {"pass": not any("Kalman:" in g for g in decision.gates_failed), "innovation_sigma": round(decision.kalman_innovation_sigma, 2), "trend": decision.kalman_trend},
                "fud_signal":{"pass": not any("Composite signal:" in g for g in decision.gates_failed), "signal": l3.adjusted_signal},
                "fud_gate":  {"pass": l3.proceed_to_execution},
            },
            "gates_passed":    decision.gates_passed,
            "gates_failed":    decision.gates_failed,
            "blocked_at":      decision.blocking_reason,
            "go_no_go":        decision.go_no_go,
            "action":          decision.action,
            "why_buy":         agg.why_buy,
            "why_wait":        agg.why_wait,
            "tga":             tga_info,
        }

    except Exception as e:
        import traceback
        return JSONResponse({"ticker": ticker, "error": str(e), "trace": traceback.format_exc()}, status_code=500)


# ── Diagnose button on Stage 2 cards ─────────────────────────────────────────
# Inject a 🔍 button that opens a modal with full gate diagnostics
_DIAG_MODAL_HTML = """
<div id="sg-diag-overlay" style="display:none;position:fixed;top:0;left:0;width:100%;height:100%;
  background:rgba(0,0,0,0.7);z-index:9000;align-items:center;justify-content:center;">
  <div style="background:#161b22;border:1px solid #30363d;border-radius:10px;padding:24px;
    max-width:680px;width:95%;max-height:85vh;overflow-y:auto;">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
      <span id="sg-diag-title" style="font-size:15px;font-weight:700;color:#e6edf3;">
        🔍 Trade Diagnostic</span>
      <button onclick="document.getElementById('sg-diag-overlay').style.display='none'"
        style="background:none;border:none;color:#8b949e;font-size:18px;cursor:pointer;">✕</button>
    </div>
    <div id="sg-diag-body" style="font-size:12px;color:#c9d1d9;line-height:1.8;"></div>
  </div>
</div>
"""

_DIAG_MODAL_OLD = '</body>'
_DIAG_MODAL_NEW = _DIAG_MODAL_HTML + '</body>'
if _DIAG_MODAL_OLD in PAPER_HTML:
    PAPER_HTML = PAPER_HTML.replace(_DIAG_MODAL_OLD, _DIAG_MODAL_NEW, 1)

# Add diagnose JS before </body>
_DIAG_JS = """<script>
async function sgDiagnose(ticker) {
  var ov = document.getElementById('sg-diag-overlay');
  var body = document.getElementById('sg-diag-body');
  var title = document.getElementById('sg-diag-title');
  title.textContent = '🔍 Diagnosing ' + ticker + '…';
  body.innerHTML = '<span style="color:#8b949e">Running full AI pipeline…</span>';
  var _stale = document.getElementById('sg-diag-stale');
  if (_stale) _stale.remove();
  ov.style.display = 'flex';
  // Snapshot bypass state so we can detect staleness if user toggles afterward
  window._diagActiveTicker = ticker;
  window._diagBypassSnap = JSON.stringify(
    JSON.parse(localStorage.getItem('sg3_gate_ov_' + ((window._PAPER_MODEL)||'standard')) || '{}')[ticker] || []
  );
  try {
    var _diagModel = (typeof window !== 'undefined' && window._PAPER_MODEL) || 'standard';
    var d = await fetch('/api/trade-diagnostic?ticker=' + ticker + '&model=' + _diagModel).then(r => r.json());
    if (d.error) { body.innerHTML = '<span style="color:#f85149">Error: ' + d.error + '</span>'; return; }
    var _mLabel = {standard:'Standard',relaxed:'Relaxed',very_relaxed:'Very Relaxed',claude:'Claude'}[d.model||'standard'] || d.model;
    title.textContent = '🔍 ' + ticker + ' [' + _mLabel + '] — ' + (d.go_no_go ? '✅ GO: ' + d.action : '❌ NO-GO: BLOCKED');

    function gateRow(name, obj) {
      var pass = obj && obj.pass;
      var icon = pass ? '✅' : '❌';
      var detail = '';
      if (obj) {
        if (obj.regime) detail += ' regime=' + obj.regime;
        if (obj.re !== undefined) detail += ' Re=' + obj.re;
        if (obj.state) detail += ' state=' + obj.state;
        if (obj.certainty !== undefined) detail += ' certainty=' + (obj.certainty*100).toFixed(0) + '%';
        if (obj.p_bull !== undefined) detail += ' P(bull)=' + (obj.p_bull*100).toFixed(0) + '%';
        if (obj.ratio !== undefined) detail += ' R/R=' + obj.ratio + ':1';
        if (obj.innovation_sigma !== undefined) detail += ' σ=' + obj.innovation_sigma;
        if (obj.trend) detail += ' trend=' + obj.trend;
        if (obj.signal) detail += ' signal=' + obj.signal;
      }
      return '<tr><td style="padding:2px 8px;">' + icon + ' ' + name + '</td>'
           + '<td style="padding:2px 8px;color:#8b949e;">' + detail + '</td></tr>';
    }

    var sc = d.scores || {};
    var html = '<table style="width:100%;border-collapse:collapse;">'
      + '<tr><td colspan="2" style="padding:4px 8px;border-bottom:1px solid #21262d;font-weight:700;color:#58a6ff;">Fundamental Analysis</td></tr>'
      + '<tr><td style="padding:2px 8px;">Investable</td><td style="padding:2px 8px;">' + (d.investable ? '✅ Yes' : '❌ No') + '</td></tr>'
      + ((!d.investable && d.investable_reasons && d.investable_reasons.length) ? '<tr><td colspan="2" style="padding:1px 8px 4px 16px;font-size:11px;color:#8b949e;">' + d.investable_reasons.filter(function(r){return r.indexOf('✓')<0;}).slice(0,3).join('<br>') + '</td></tr>' : '')
      + '<tr><td style="padding:2px 8px;">Moat</td><td style="padding:2px 8px;">' + (d.moat||'unknown') + '</td></tr>'
      + '<tr><td style="padding:2px 8px;">Margin of Safety</td><td style="padding:2px 8px;">' + ((d.margin_of_safety||0)*100).toFixed(1) + '%</td></tr>'
      + '<tr><td style="padding:2px 8px;">ROIC / WACC</td><td style="padding:2px 8px;">' + ((d.roic||0)*100).toFixed(1) + '% / ' + ((d.wacc||0)*100).toFixed(1) + '%</td></tr>'
      + '<tr><td style="padding:2px 8px;">Price history</td><td style="padding:2px 8px;">' + (d.price_days||0) + ' days | VIX=' + d.vix + ' (' + d.vix_regime + ')</td></tr>'
      + '<tr><td colspan="2" style="padding:4px 8px;border-bottom:1px solid #21262d;font-weight:700;color:#58a6ff;padding-top:10px;">Signal Scores (weighted composite: ' + (d.composite_score||0).toFixed(3) + ' → ' + (d.signal_l2||'?') + ')</td></tr>'
      + Object.entries(sc).map(([k,v]) => '<tr><td style="padding:2px 8px;">' + k + '</td><td style="padding:2px 8px;color:' + (v>0?'#3fb950':v<0?'#f85149':'#8b949e') + ';">' + (v>=0?'+':'') + v.toFixed(3) + '</td></tr>').join('')
      + '<tr><td colspan="2" style="padding:4px 8px;border-bottom:1px solid #21262d;font-weight:700;color:#58a6ff;padding-top:10px;">Decision Gates</td></tr>'
      + gateRow('FUD filter', d.gate_results && d.gate_results.fud_gate)
      + gateRow('Reynolds', d.gate_results && d.gate_results.reynolds)
      + gateRow('Quantum', d.gate_results && d.gate_results.quantum)
      + gateRow('Ensemble', d.gate_results && d.gate_results.ensemble)
      + gateRow('Risk/Reward', d.gate_results && d.gate_results.rr)
      + gateRow('Kalman', d.gate_results && d.gate_results.kalman)
      + gateRow('FUD Signal', d.gate_results && d.gate_results.fud_signal)
      + '</table>';
    if (d.blocked_at) {
      html += '<div style="margin-top:12px;padding:8px 12px;background:rgba(248,81,73,0.1);border-radius:6px;border-left:3px solid #f85149;">'
            + '<b style="color:#f85149;">Blocked:</b> ' + d.blocked_at + '</div>';
    }
    if (d.why_buy && d.why_buy.length) {
      html += '<div style="margin-top:10px;color:#3fb950;font-size:11px;"><b>Why Buy:</b><br>' + d.why_buy.join('<br>') + '</div>';
    }
    if (d.why_wait && d.why_wait.length) {
      html += '<div style="margin-top:6px;color:#d29922;font-size:11px;"><b>Caution:</b><br>' + d.why_wait.join('<br>') + '</div>';
    }
    if (d.tga) {
      var tg = d.tga;
      var arrowCols = ['#f85149','#d29922','#d29922','#3fb950'];
      var tgaCol = arrowCols[Math.min(tg.arrows||0, 3)];
      var arrow = function(on) { return '<span style="color:' + (on ? '#3fb950' : '#30363d') + ';font-size:14px;">▲</span>'; };
      html += '<div style="margin-top:10px;padding:8px 12px;background:#0d1117;border-radius:6px;border:1px solid #30363d;">'
            + '<div style="font-size:11px;font-weight:700;color:' + tgaCol + ';margin-bottom:5px;">3 Green Arrows (TOS): '
            + (tg.arrows||0) + '/3 — ' + (tg.signal||'neutral').toUpperCase()
            + (tg.vol ? ' <span style="color:#d29922;font-size:10px;">+ vol spike</span>' : '') + '</div>'
            + '<div style="display:flex;gap:12px;font-size:11px;color:#c9d1d9;">'
            + '<span>' + arrow(tg.sma)  + ' SMA(30)</span>'
            + '<span>' + arrow(tg.macd) + ' MACD(8,17,9)</span>'
            + '<span>' + arrow(tg.stoch)+ ' Stoch(14,5)</span>'
            + '</div>'
            + (tg.reason ? '<div style="margin-top:4px;font-size:10px;color:#8b949e;">' + tg.reason + '</div>' : '')
            + '</div>';
    }
    body.innerHTML = html;
  } catch(e) {
    body.innerHTML = '<span style="color:#f85149">Fetch error: ' + e.message + '</span>';
  }
}
</script>"""

if '</body>' in PAPER_HTML:
    PAPER_HTML = PAPER_HTML.replace('</body>', _DIAG_JS + '</body>', 1)

# ── Add 🔍 Diagnose button to Stage 2 cards ───────────────────────────────────
# Stage 2 card has an AI button: sgActivateAI. We add the diagnose btn after it.
# Inject diagnose + chart buttons after the info button in sg3CardHtml
# Target (in runtime PAPER_JS, after _SG3_JS was merged):
#   btns += '<button class="sg3-btn sg3-btn-info" ... title="AI Analysis">&#9432;</button>';
_DIAG_BTN_OLD = (
    "btns += '<button class=\"sg3-btn sg3-btn-info\" data-ticker=\"' + ticker + '\" "
    "onclick=\"sgShowInfo(this.dataset.ticker)\" title=\"AI Analysis\">&#9432;</button>';"
)
_DIAG_BTN_NEW = (
    "btns += '<button class=\"sg3-btn sg3-btn-info\" data-ticker=\"' + ticker + '\" "
    "onclick=\"sgShowInfo(this.dataset.ticker)\" title=\"AI Analysis\">&#9432;</button>';\n"
    "  btns += '<button style=\"font-size:10px;padding:2px 6px;border-radius:4px;"
    "border:1px solid #30363d;background:#0d1117;color:#8b949e;cursor:pointer;margin-left:2px;\""
    " onclick=\"sgDiagnose(\\'' + ticker + '\\')\" title=\"Full AI diagnostic\">&#128269;</button>';\n"
    "  btns += '<a href=\"/charts?ticker=' + ticker + '\" target=\"_blank\" "
    "style=\"font-size:10px;padding:2px 6px;border-radius:4px;border:1px solid #30363d;"
    "background:#0d1117;color:#8b949e;text-decoration:none;margin-left:2px;\""
    " title=\"Open chart\">&#128200;</a>';"
)
if _DIAG_BTN_OLD in PAPER_JS:
    PAPER_JS = PAPER_JS.replace(_DIAG_BTN_OLD, _DIAG_BTN_NEW)
else:
    import sys as _sys; print('[WARN] diagBtn injection: info btn not found', file=_sys.stderr)


# ═══════════════════════════════════════════════════════════════════════
# Phase 2a — /api/chart-data and /api/chart-indicators endpoints
# Serve OHLCV + EMA/VWAP/signal data in TradingView Lightweight Charts format
# ═══════════════════════════════════════════════════════════════════════

@app.get("/api/chart-data")
def api_chart_data(ticker: str, days: int = 180):
    """
    Return OHLCV data for TradingView Lightweight Charts.
    Format: [{time: "YYYY-MM-DD", open, high, low, close, volume}]
    """
    ticker = ticker.upper().strip()
    try:
        with Session() as session:
            company = session.query(Company).filter_by(ticker=ticker).first()
            if not company:
                return JSONResponse({"error": f"Ticker {ticker} not found"}, status_code=404)

            from datetime import timedelta
            cutoff = datetime.utcnow() - timedelta(days=days)
            records = (
                session.query(PriceHistory)
                .filter(PriceHistory.company_id == company.id)
                .filter(PriceHistory.date >= cutoff)
                .order_by(PriceHistory.date)
                .all()
            )

            # Build candle map keyed by date string; later entries (higher time) overwrite
            candle_map: dict = {}
            for r in records:
                o = r.open or r.close
                h = r.high or r.close
                l = r.low  or r.close
                c = r.adjusted_close or r.close
                if not c:
                    continue
                date_key = r.date.strftime("%Y-%m-%d")
                candle_map[date_key] = {
                    "time":   date_key,
                    "open":   round(float(o), 4),
                    "high":   round(float(h), 4),
                    "low":    round(float(l), 4),
                    "close":  round(float(c), 4),
                    "volume": int(r.volume or 0),
                }

            candles = sorted(candle_map.values(), key=lambda x: x["time"])
            return {"ticker": ticker, "candles": candles}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/chart-intraday")
def api_chart_intraday(ticker: str, freq: int = 5):
    """
    Return today's intraday OHLCV candles.
    Tries Schwab live API first; falls back to yfinance if unavailable.
    Format: [{time (unix epoch seconds), open, high, low, close, volume}]
    """
    ticker = ticker.upper().strip()
    candles = []
    source = "schwab"
    try:
        from broker.market_data import SchwabMarketData
        md = SchwabMarketData()
        candles_raw = md.get_price_history_intraday(ticker, freq_minutes=freq)
        for c in candles_raw:
            if not c.get("close"):
                continue
            candles.append({
                "time":   int(c["date"].timestamp()),
                "open":   round(float(c["open"]  or c["close"]), 4),
                "high":   round(float(c["high"]  or c["close"]), 4),
                "low":    round(float(c["low"]   or c["close"]), 4),
                "close":  round(float(c["close"]),              4),
                "volume": int(c["volume"] or 0),
            })
    except Exception:
        pass

    if not candles:
        # Fallback: yfinance 5-minute bars for most recent trading session
        source = "yfinance"
        try:
            import yfinance as yf
            df = yf.download(ticker, period="2d", interval=f"{freq}m", progress=False, auto_adjust=True)
            if not df.empty:
                import pandas as pd
                # Flatten MultiIndex columns (yfinance 0.2.x returns (Col, Ticker) tuples)
                df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
                # Keep only rows from the last trading day in the data
                df.index = pd.to_datetime(df.index)
                last_day = df.index.normalize().max()
                df = df[df.index.normalize() == last_day]
                for ts, row in df.iterrows():
                    c = float(row["Close"])
                    if not c:
                        continue
                    candles.append({
                        "time":   int(ts.timestamp()),
                        "open":   round(float(row["Open"]  or c), 4),
                        "high":   round(float(row["High"]  or c), 4),
                        "low":    round(float(row["Low"]   or c), 4),
                        "close":  round(c,                        4),
                        "volume": int(row["Volume"] or 0),
                    })
        except Exception as e:
            return JSONResponse({"error": f"yfinance fallback failed: {e}"}, status_code=500)

    return {"ticker": ticker, "candles": candles, "freq_minutes": freq, "source": source}


@app.get("/api/chart-indicators")
def api_chart_indicators(ticker: str, days: int = 180):
    """
    Return technical indicators for TradingView overlay:
    - ema20, ema50: [{time, value}]
    - vwap_line: [{time, value}]
    - signals: [{time, action, price}]
    - fib_levels: {high, low, levels: [{name, price}]}
    """
    ticker = ticker.upper().strip()
    try:
        with Session() as session:
            company = session.query(Company).filter_by(ticker=ticker).first()
            if not company:
                return JSONResponse({"error": f"Ticker {ticker} not found"}, status_code=404)

            from datetime import timedelta
            cutoff = datetime.utcnow() - timedelta(days=days)
            records = (
                session.query(PriceHistory)
                .filter(PriceHistory.company_id == company.id)
                .filter(PriceHistory.date >= cutoff)
                .order_by(PriceHistory.date)
                .all()
            )

            # Deduplicate by date string; later rows (higher timestamp) overwrite earlier
            row_map: dict = {}
            for r in records:
                row_map[r.date.strftime("%Y-%m-%d")] = r
            deduped = sorted(row_map.values(), key=lambda r: r.date)

            times  = [r.date.strftime("%Y-%m-%d") for r in deduped]
            closes = [float(r.adjusted_close or r.close or 0) for r in deduped]
            highs  = [float(r.high  or r.close or 0) for r in deduped]
            lows   = [float(r.low   or r.close or 0) for r in deduped]
            vols   = [float(r.volume or 0) for r in deduped]

            def _ema(values, period):
                result = []
                k = 2.0 / (period + 1)
                ema = None
                for v in values:
                    if v <= 0:
                        result.append(None)
                        continue
                    if ema is None:
                        ema = v
                    else:
                        ema = v * k + ema * (1 - k)
                    result.append(round(ema, 4))
                return result

            ema20 = _ema(closes, 20)
            ema50 = _ema(closes, 50)

            # VWAP (rolling daily using H+L+C/3 * volume / cum_volume)
            vwap_line = []
            cum_tpv = 0.0
            cum_vol = 0.0
            for i, (h, l, c, v) in enumerate(zip(highs, lows, closes, vols)):
                tp = (h + l + c) / 3.0
                cum_tpv += tp * v
                cum_vol  += v
                vwap = round(cum_tpv / cum_vol, 4) if cum_vol > 0 else None
                vwap_line.append(vwap)

            # Trade signals from DB
            sig_rows = (
                session.query(TradeSignal)
                .filter(TradeSignal.company_id == company.id)
                .filter(TradeSignal.generated_at >= cutoff)
                .filter(TradeSignal.signal.in_(["BUY", "SELL"]))
                .order_by(TradeSignal.generated_at)
                .all()
            )
            # Deduplicate: keep only the latest signal per calendar date
            _sig_by_date: dict = {}
            for s in sig_rows:
                if s.generated_at and s.current_price:
                    date_key = s.generated_at.strftime("%Y-%m-%d")
                    if date_key not in _sig_by_date or s.generated_at > _sig_by_date[date_key]["_ts"]:
                        _sig_by_date[date_key] = {
                            "time":   date_key,
                            "action": s.signal,
                            "price":  round(float(s.current_price), 4),
                            "_ts":    s.generated_at,
                        }
            signals = [{"time": v["time"], "action": v["action"], "price": v["price"]}
                       for v in sorted(_sig_by_date.values(), key=lambda x: x["time"])]

            # Fibonacci levels (using range over the period)
            fib_levels = None
            if highs and lows:
                period_high = max(h for h in highs if h > 0)
                period_low  = min(l for l in lows  if l > 0)
                diff = period_high - period_low
                fib_levels = {
                    "high": round(period_high, 4),
                    "low":  round(period_low,  4),
                    "levels": [
                        {"name": "0%",    "price": round(period_low,              4)},
                        {"name": "38.2%", "price": round(period_low + 0.382*diff, 4)},
                        {"name": "50%",   "price": round(period_low + 0.500*diff, 4)},
                        {"name": "61.8%", "price": round(period_low + 0.618*diff, 4)},
                        {"name": "100%",  "price": round(period_high,             4)},
                    ],
                }

            # Build time-indexed arrays for lightweight charts
            def _zip(t, v):
                return [{"time": t[i], "value": v[i]} for i in range(len(t)) if v[i] is not None]

            return {
                "ticker":     ticker,
                "ema20":      _zip(times, ema20),
                "ema50":      _zip(times, ema50),
                "vwap_line":  _zip(times, vwap_line),
                "signals":    signals,
                "fib_levels": fib_levels,
            }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/chart-tga-panel")
def api_chart_tga_panel(ticker: str, days: int = 180):
    """
    Return per-bar TGA indicator series for chart overlay:
    SMA(30), MACD(8,17,9,EMA) histogram, Stochastic(14,5) FullD,
    volume spike markers, SMA cross markers, SuperTrend line.
    """
    ticker = ticker.upper().strip()
    try:
        with Session() as session:
            company = session.query(Company).filter_by(ticker=ticker).first()
            if not company:
                return JSONResponse({"error": f"Ticker {ticker} not found"}, status_code=404)

            from datetime import timedelta
            # Load extra history for warmup
            cutoff = datetime.utcnow() - timedelta(days=days + 90)
            records = (
                session.query(PriceHistory)
                .filter(PriceHistory.company_id == company.id)
                .filter(PriceHistory.date >= cutoff)
                .order_by(PriceHistory.date)
                .all()
            )
            if not records:
                return {"ticker": ticker, "sma30": [], "macd_hist": [], "stoch_fulld": [],
                        "volume_spike_markers": [], "sma_cross_markers": [], "supertrend": []}

            # Deduplicate by date string; later rows (higher timestamp) overwrite earlier
            _row_map: dict = {}
            for r in records:
                _row_map[r.date.strftime("%Y-%m-%d")] = r
            _deduped = sorted(_row_map.values(), key=lambda r: r.date)

            times  = [r.date.strftime("%Y-%m-%d") for r in _deduped]
            closes = [float(r.adjusted_close or r.close or 0) for r in _deduped]
            highs  = [float(r.high or r.close or 0) for r in _deduped]
            lows   = [float(r.low or r.close or 0) for r in _deduped]
            vols   = [float(r.volume or 0) for r in _deduped]

            # TGA indicators
            from signals.three_green_arrows import ThreeGreenArrowsAnalyzer
            tga_series = ThreeGreenArrowsAnalyzer().analyze_series(times, closes, highs, lows, vols)

            # SuperTrend series (Wilder ATR, factor=3, period=10)
            def _supertrend_series(ts, cs, hs, ls, atr_p=10, factor=3.0):
                n = min(len(ts), len(cs), len(hs), len(ls))
                out = []
                trs = [hs[0] - ls[0]]
                for i in range(1, n):
                    trs.append(max(hs[i]-ls[i], abs(hs[i]-cs[i-1]), abs(ls[i]-cs[i-1])))
                atr = [0.0] * n
                if n >= atr_p:
                    atr[atr_p-1] = sum(trs[:atr_p]) / atr_p
                    for i in range(atr_p, n):
                        atr[i] = (atr[i-1]*(atr_p-1) + trs[i]) / atr_p
                direction = 1
                prev_up = prev_lo = 0.0
                for i in range(n):
                    if atr[i] == 0.0:
                        continue
                    hl2 = (hs[i] + ls[i]) / 2
                    b_up = hl2 + factor * atr[i]
                    b_lo = hl2 - factor * atr[i]
                    if i == 0:
                        up, lo = b_up, b_lo
                    else:
                        up = b_up if (b_up < prev_up or cs[i-1] > prev_up) else prev_up
                        lo = b_lo if (b_lo > prev_lo or cs[i-1] < prev_lo) else prev_lo
                    if i == 0:
                        direction = 1 if cs[i] > lo else -1
                    elif direction == 1:
                        direction = -1 if cs[i] < lo else 1
                    else:
                        direction = 1 if cs[i] > up else -1
                    st_val = lo if direction == 1 else up
                    prev_up, prev_lo = up, lo
                    if i >= atr_p:
                        out.append({"time": ts[i], "value": round(st_val, 4),
                                    "direction": "uptrend" if direction == 1 else "downtrend"})
                return out

            st_series = _supertrend_series(times, closes, highs, lows)

            # Trim all series to requested days window
            cutoff_date = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")

            def _trim(lst):
                return [x for x in lst if x.get("time", "") >= cutoff_date]

            return {
                "ticker":               ticker,
                "sma30":               _trim(tga_series["sma30"]),
                "macd_hist":           _trim(tga_series["macd_hist"]),
                "stoch_fulld":         _trim(tga_series["stoch_fulld"]),
                "stoch_overbought":    75,
                "stoch_oversold":      25,
                "volume_spike_markers": _trim(tga_series["volume_spike_markers"]),
                "sma_cross_markers":   _trim(tga_series["sma_cross_markers"]),
                "supertrend":          _trim(st_series),
            }
    except Exception as e:
        import traceback
        return JSONResponse({"error": str(e), "trace": traceback.format_exc()}, status_code=500)


# ═══════════════════════════════════════════════════════════════════════
# Phase 2b — /charts page with TradingView Lightweight Charts
# ═══════════════════════════════════════════════════════════════════════

_CHARTS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NWO Charts</title>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d1117; color: #e6edf3; font-family: -apple-system,BlinkMacSystemFont,'Segoe UI',monospace; }
  {NAV_CSS}
  header { display:flex;align-items:center;gap:10px;padding:10px 16px;background:#161b22;
           border-bottom:1px solid #30363d;flex-wrap:wrap; }
  .chart-wrap { padding: 16px; }
  .chart-controls { display:flex;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap; }
  .chart-controls input { background:#161b22;border:1px solid #30363d;border-radius:6px;
    color:#e6edf3;padding:6px 10px;font-size:13px;width:120px; }
  .chart-controls input:focus { outline:none;border-color:#58a6ff; }
  .period-btn { padding:4px 10px;border-radius:4px;border:1px solid #30363d;
    background:#161b22;color:#8b949e;cursor:pointer;font-size:12px; }
  .period-btn.active, .period-btn:hover { background:#1c2e50;color:#58a6ff;border-color:#58a6ff; }
  .toggle-row { display:flex;gap:8px;flex-wrap:wrap; }
  .ind-toggle { padding:3px 8px;border-radius:4px;border:1px solid #30363d;
    background:#161b22;color:#8b949e;cursor:pointer;font-size:11px; }
  .ind-toggle.on { border-color:#3fb950;color:#3fb950; }
  #main-chart  { width:100%;height:340px;border:1px solid #21262d;border-radius:6px;overflow:hidden; }
  #rsi-chart   { width:100%;height:80px;border:1px solid #21262d;border-radius:6px;overflow:hidden;margin-top:3px; }
  #vol-chart   { width:100%;height:70px;border:1px solid #21262d;border-radius:6px;overflow:hidden;margin-top:3px; }
  #macd-chart  { width:100%;height:75px;border:1px solid #21262d;border-radius:6px;overflow:hidden;margin-top:3px; }
  #stoch-chart { width:100%;height:75px;border:1px solid #21262d;border-radius:6px;overflow:hidden;margin-top:3px; }
  header .brief-btn { padding:3px 8px; }
  header .brief-btn-title { font-size:11px; }
  header .brief-btn-preview { font-size:9px; }
  .pane-label  { font-size:11px;color:#8b949e;padding:2px 8px;margin-top:4px;display:flex;align-items:center;gap:8px; }
  .pane-label b { color:#c9d1d9; }
  .ohlcv-bar { font-size:11px;color:#8b949e;padding:4px 8px;margin-bottom:4px;
    display:flex;gap:14px;align-items:center;flex-wrap:wrap;
    background:#0d1117;border:1px solid #21262d;border-radius:4px; }
  .ohlcv-bar span { white-space:nowrap; }
  .ohlcv-bar .ob-o { color:#8b949e; }
  .ohlcv-bar .ob-h { color:#3fb950; }
  .ohlcv-bar .ob-l { color:#f85149; }
  .ohlcv-bar .ob-c-up   { color:#3fb950;font-weight:700; }
  .ohlcv-bar .ob-c-down { color:#f85149;font-weight:700; }
  .ohlcv-bar .ob-v { color:#8b949e; }
  .ai-bar { display:flex;gap:16px;align-items:center;margin-top:10px;padding:8px 12px;
    background:#161b22;border:1px solid #21262d;border-radius:6px;font-size:12px;flex-wrap:wrap; }
  .ai-bar span { color:#8b949e; }
  .ai-bar b { color:#e6edf3; }
  #chart-signal { padding:3px 8px;border-radius:4px;font-weight:700;font-size:12px; }
  .fib-legend { display:flex;flex-wrap:wrap;gap:6px;margin-top:8px; }
  .fib-badge { font-size:10px;padding:2px 6px;background:#161b22;border:1px solid #21262d;
    border-radius:3px; }
  .chart-title { font-size:14px;font-weight:700;color:#58a6ff; }
  .ind-info-btn { padding:3px 8px;border-radius:4px;border:1px solid #30363d;
    background:#161b22;color:#58a6ff;cursor:pointer;font-size:11px;margin-left:4px; }
  #ind-info-panel { display:none;position:absolute;z-index:500;background:#161b22;
    border:1px solid #30363d;border-radius:8px;padding:14px 16px;font-size:11px;
    color:#8b949e;line-height:1.8;min-width:280px;box-shadow:0 4px 20px rgba(0,0,0,0.5); }
  #ind-info-panel b { color:#e6edf3; }
  .chart-wrap { position:relative; }
</style>
</head>
<body>
<header>
  {NAV}
</header>
{TAPE_HTML}
{PAGE_INFO}
<div class="chart-wrap">
  <div class="chart-controls">
    <input id="chart-ticker" type="text" placeholder="AAPL" value="AAPL" />
    <button class="period-btn" id="btn-1d">1D</button>
    <button class="period-btn" id="btn-1w">1W</button>
    <button class="period-btn" id="btn-1m">1M</button>
    <button class="period-btn active" id="btn-3m">3M</button>
    <button class="period-btn" id="btn-6m">6M</button>
    <button class="period-btn" id="btn-1y">1Y</button>
    <select id="intraday-freq" style="display:none;background:#161b22;border:1px solid #30363d;border-radius:4px;color:#58a6ff;padding:3px 6px;font-size:12px;cursor:pointer;">
      <option value="1">1m</option>
      <option value="5" selected>5m</option>
      <option value="15">15m</option>
      <option value="30">30m</option>
      <option value="60">1h</option>
    </select>
    <span style="color:#30363d;margin:0 4px;">|</span>
    <button class="ind-toggle on" id="tog-ema20" onclick="toggleInd('ema20')">EMA 20</button>
    <button class="ind-toggle on" id="tog-ema50" onclick="toggleInd('ema50')">EMA 50</button>
    <button class="ind-toggle on" id="tog-vwap"  onclick="toggleInd('vwap')">VWAP</button>
    <button class="ind-toggle on" id="tog-fib"   onclick="toggleInd('fib')">Fibonacci</button>
    <button class="ind-toggle on" id="tog-sigs"  onclick="toggleInd('sigs')">Signals</button>
    <span style="color:#30363d;margin:0 4px;">|</span>
    <button class="ind-toggle" id="tog-sma30" onclick="toggleInd('sma30')" title="SMA(30) cross arrows — TOS Study 1">SMA 30</button>
    <button class="ind-toggle" id="tog-st"    onclick="toggleInd('st')"    title="SuperTrend (ATR×3, period 10) — green uptrend / red downtrend">SuperTrend</button>
    <button class="ind-toggle" id="tog-tga"   onclick="toggleInd('tga')"   title="3 Green Arrows panel — MACD(8,17,9) + Stochastic(14,5) panes">3GA Panel</button>
    <button class="ind-info-btn" id="ind-info-btn" onclick="toggleIndInfo(event)">?</button>
    <div id="ind-info-panel">
      <b>EMA 20 / EMA 50</b> — Exponential moving averages. Price above both = bullish trend.<br>
      <b>VWAP</b> — Volume-weighted avg price (cumulative for period). Institutional reference level.<br>
      <b>Fibonacci</b> — Retracement levels from period high/low: 0%, 38.2%, 50%, 61.8%, 100%.<br>
      <b>Signals</b> — BUY/SELL markers from the NWO AI pipeline (one per trading day).<br>
      <b>SMA 30</b> — TOS Study 1: 30-bar SMA with cross-above arrows (orange).<br>
      <b>SuperTrend</b> — ATR×3 trailing stop. Green = uptrend, Red = downtrend.<br>
      <b>3GA Panel</b> — TOS 3 Green Arrows: MACD(8,17,9) histogram + Stochastic(14,5) sub-panes.
    </div>
    <span id="chart-ticker-label" class="chart-title" style="margin-left:8px;">AAPL</span>
  </div>
  <div class="ohlcv-bar" id="ohlcv-bar">
    <span style="color:#58a6ff;font-weight:700;" id="ob-ticker">—</span>
    <span class="ob-o">O: <b id="ob-o">—</b></span>
    <span class="ob-h">H: <b id="ob-h">—</b></span>
    <span class="ob-l">L: <b id="ob-l">—</b></span>
    <span>C: <b id="ob-c">—</b></span>
    <span class="ob-v">V: <b id="ob-v">—</b></span>
  </div>
  <div id="main-chart"></div>
  <div id="vol-label" class="pane-label">Volume <b id="vol-val"></b></div>
  <div id="vol-chart"></div>
  <div id="rsi-label" class="pane-label">RSI (14) <b id="rsi-val"></b></div>
  <div id="rsi-chart"></div>
  <div id="macd-label"  class="pane-label" style="display:none;">MACD (8,17,9 EMA) <b id="macd-val"></b></div>
  <div id="macd-chart"  style="display:none;"></div>
  <div id="stoch-label" class="pane-label" style="display:none;">Stochastic FullD (14,5) <b id="stoch-val"></b> <span style="color:#8b949e;font-size:10px;">OB:75 / OS:25</span></div>
  <div id="stoch-chart" style="display:none;"></div>
  <div class="ai-bar">
    <span>AI Signal: <b id="chart-signal" style="background:#21262d;padding:3px 8px;border-radius:4px;">—</span>
    <span>Composite: <b id="chart-composite">—</b></span>
    <span>Momentum: <b id="chart-momentum">—</b></span>
    <span>3GA: <b id="chart-tga" style="color:#8b949e;">—</b></span>
    <span>Margin of Safety: <b id="chart-mos">—</b></span>
    <span>Investable: <b id="chart-investable">—</b></span>
    <span>Gates Passed: <b id="chart-gates">—</b></span>
    <button onclick="runDiagnostic()" style="padding:4px 10px;border-radius:4px;border:1px solid #58a6ff;
      background:#1c2e50;color:#58a6ff;cursor:pointer;font-size:11px;">🔍 Full Diagnostic</button>
  </div>
  <div id="fib-legend" class="fib-legend"></div>
</div>

<!-- Diagnostic modal (reused from paper trade) -->
<div id="sg-diag-overlay" style="display:none;position:fixed;top:0;left:0;width:100%;height:100%;
  background:rgba(0,0,0,0.7);z-index:9000;align-items:center;justify-content:center;">
  <div style="background:#161b22;border:1px solid #30363d;border-radius:10px;padding:24px;
    max-width:680px;width:95%;max-height:85vh;overflow-y:auto;">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
      <span id="sg-diag-title" style="font-size:15px;font-weight:700;color:#e6edf3;">🔍 Trade Diagnostic</span>
      <button onclick="document.getElementById('sg-diag-overlay').style.display='none'"
        style="background:none;border:none;color:#8b949e;font-size:18px;cursor:pointer;">✕</button>
    </div>
    <div id="sg-diag-body" style="font-size:12px;color:#c9d1d9;line-height:1.8;"></div>
  </div>
</div>

<script>
var _chart, _volChart, _rsiChart, _macdChart, _stochChart;
var _candleSeries, _volSeries, _rsiSeries, _macdSeries, _stochSeries;
var _ema20Series, _ema50Series, _vwapSeries, _sma30Series, _stUpSeries, _stDownSeries;
var _fibLines = [], _sigMarkers = [], _tgaData = null;
var _indState = {ema20:true, ema50:true, vwap:true, fib:true, sigs:true, sma30:false, st:false, tga:false};
var _currentDays = 90;
var _currentTicker = 'AAPL';
var _currentIntraday = false;
var _syncEnabled = false;
var _ohlcvMap = {};

function _updateOhlcvBar(param) {
  var el = document.getElementById('ob-ticker');
  if (el) el.textContent = _currentTicker || '—';
  if (!param || !param.time) return;
  var c = _ohlcvMap[param.time];
  if (!c) return;
  var isUp = c.close >= c.open;
  var cEl = document.getElementById('ob-c');
  if (cEl) { cEl.textContent = c.close.toFixed(2); cEl.parentElement.className = isUp ? 'ob-c-up' : 'ob-c-down'; }
  var oEl = document.getElementById('ob-o'); if (oEl) oEl.textContent = c.open.toFixed(2);
  var hEl = document.getElementById('ob-h'); if (hEl) hEl.textContent = c.high.toFixed(2);
  var lEl = document.getElementById('ob-l'); if (lEl) lEl.textContent = c.low.toFixed(2);
  var vol = c.volume>=1e6?(c.volume/1e6).toFixed(2)+'M':c.volume>=1e3?(c.volume/1e3).toFixed(0)+'K':String(c.volume);
  var vEl = document.getElementById('ob-v'); if (vEl) vEl.textContent = vol;
  if (!param.seriesData) return;
  var vd = param.seriesData.get(_volSeries);
  if (vd) { var vv=vd.value>=1e6?(vd.value/1e6).toFixed(2)+'M':vd.value>=1e3?(vd.value/1e3).toFixed(0)+'K':String(vd.value||0); var vvEl=document.getElementById('vol-val'); if(vvEl) vvEl.textContent=vv; }
  var rd=param.seriesData.get(_rsiSeries);   var rvEl=document.getElementById('rsi-val');   if(rd&&rvEl)  rvEl.textContent=(rd.value||0).toFixed(1);
  var md=param.seriesData.get(_macdSeries);  var mvEl=document.getElementById('macd-val');  if(md&&mvEl)  mvEl.textContent=(md.value||0).toFixed(4);
  var sd=param.seriesData.get(_stochSeries); var svEl=document.getElementById('stoch-val'); if(sd&&svEl)  svEl.textContent=(sd.value||0).toFixed(1);
}

function _createCharts() {
  var mainEl  = document.getElementById('main-chart');
  var volEl   = document.getElementById('vol-chart');
  var rsiEl   = document.getElementById('rsi-chart');
  var macdEl  = document.getElementById('macd-chart');
  var stochEl = document.getElementById('stoch-chart');

  // Only create once — reuse across period/ticker changes to avoid ResizeObserver issues
  if (_chart) return;

  _fibLines = [];
  _syncEnabled = false;

  var w = mainEl.getBoundingClientRect().width || mainEl.clientWidth || 900;

  _chart = LightweightCharts.createChart(mainEl, {
    width: w, height: 340,
    layout: { background: {color:'#0d1117'}, textColor:'#c9d1d9' },
    grid: { vertLines:{color:'#1a1f28'}, horzLines:{color:'#1a1f28'} },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: { borderColor:'#30363d' },
    timeScale: { borderColor:'#30363d', timeVisible:true },
  });

  _volChart = LightweightCharts.createChart(volEl, {
    width: w, height: 70,
    layout: { background:{color:'#0d1117'}, textColor:'#8b949e' },
    grid: { vertLines:{color:'#1a1f28'}, horzLines:{color:'#1a1f28'} },
    rightPriceScale: { borderColor:'#30363d' },
    timeScale: { borderColor:'#30363d', timeVisible:true },
  });

  _rsiChart = LightweightCharts.createChart(rsiEl, {
    width: w, height: 80,
    layout: { background:{color:'#0d1117'}, textColor:'#8b949e' },
    grid: { vertLines:{color:'#1a1f28'}, horzLines:{color:'#1a1f28'} },
    rightPriceScale: { borderColor:'#30363d' },
    timeScale: { borderColor:'#30363d', timeVisible:true },
  });

  _macdChart = LightweightCharts.createChart(macdEl, {
    width: w, height: 75,
    layout: { background:{color:'#0d1117'}, textColor:'#8b949e' },
    grid: { vertLines:{color:'#1a1f28'}, horzLines:{color:'#1a1f28'} },
    rightPriceScale: { borderColor:'#30363d' },
    timeScale: { borderColor:'#30363d', timeVisible:true },
  });

  _stochChart = LightweightCharts.createChart(stochEl, {
    width: w, height: 75,
    layout: { background:{color:'#0d1117'}, textColor:'#8b949e' },
    grid: { vertLines:{color:'#1a1f28'}, horzLines:{color:'#1a1f28'} },
    rightPriceScale: { borderColor:'#30363d', scaleMargins:{top:0.02, bottom:0.02}, autoScale:false },
    timeScale: { borderColor:'#30363d', timeVisible:true },
  });

  _candleSeries = _chart.addCandlestickSeries({
    upColor:'#3fb950', downColor:'#f85149',
    borderUpColor:'#3fb950', borderDownColor:'#f85149',
    wickUpColor:'#3fb950', wickDownColor:'#f85149',
  });
  _ema20Series  = _chart.addLineSeries({ color:'#3fb950', lineWidth:1, lineStyle:0 });
  _ema50Series  = _chart.addLineSeries({ color:'#58a6ff', lineWidth:1, lineStyle:0 });
  _vwapSeries   = _chart.addLineSeries({ color:'#d29922', lineWidth:1, lineStyle:1 });
  _sma30Series  = _chart.addLineSeries({ color:'#e3a02c', lineWidth:2, lineStyle:0, title:'SMA30' });
  _stUpSeries   = _chart.addLineSeries({ color:'#3fb950', lineWidth:2, lineStyle:0 });
  _stDownSeries = _chart.addLineSeries({ color:'#f85149', lineWidth:2, lineStyle:0 });
  _volSeries    = _volChart.addHistogramSeries({ color:'#1f6feb', priceFormat:{type:'volume'} });
  _rsiSeries    = _rsiChart.addLineSeries({ color:'#a371f7', lineWidth:2 });
  _macdSeries   = _macdChart.addHistogramSeries({ priceFormat:{type:'price', precision:4, minMove:0.0001} });
  _stochSeries  = _stochChart.addLineSeries({ color:'#58a6ff', lineWidth:2 });
  // Stochastic OB/OS reference lines
  _stochSeries.createPriceLine({ price:75, color:'#d29922', lineWidth:1, lineStyle:2, axisLabelVisible:true, title:'OB' });
  _stochSeries.createPriceLine({ price:25, color:'#3fb950', lineWidth:1, lineStyle:2, axisLabelVisible:true, title:'OS' });
  // MACD zero line
  _macdSeries.createPriceLine({ price:0, color:'#30363d', lineWidth:1, lineStyle:0, axisLabelVisible:false });

  // Pin stoch scale at 0–100 with invisible anchors
  _stochSeries.createPriceLine({ price:0,   color:'transparent', lineWidth:0, axisLabelVisible:false });
  _stochSeries.createPriceLine({ price:100, color:'transparent', lineWidth:0, axisLabelVisible:false });

  // Wire crosshair to module-level _updateOhlcvBar + _ohlcvMap
  _chart.subscribeCrosshairMove(function(param) { _updateOhlcvBar(param); });

  _chart.timeScale().subscribeVisibleLogicalRangeChange(function(range) {
    if (!_syncEnabled || !range) return;
    try { _volChart.timeScale().setVisibleLogicalRange(range); } catch(e) {}
    try { _rsiChart.timeScale().setVisibleLogicalRange(range); } catch(e) {}
    if (_indState.tga) {
      try { _macdChart.timeScale().setVisibleLogicalRange(range); } catch(e) {}
      try { _stochChart.timeScale().setVisibleLogicalRange(range); } catch(e) {}
    }
  });

  window.addEventListener('resize', function() {
    var rw = mainEl.getBoundingClientRect().width || 900;
    if (_chart)     _chart.resize(rw, 340);
    if (_volChart)  _volChart.resize(rw, 70);
    if (_rsiChart)  _rsiChart.resize(rw, 80);
    if (_macdChart && _indState.tga)  _macdChart.resize(rw, 75);
    if (_stochChart && _indState.tga) _stochChart.resize(rw, 75);
  });
}

// Retry setData in next RAF if LightweightCharts canvas isn't ready yet
function _safeSetData(series, data) {
  return new Promise(function(resolve) {
    function attempt() {
      try { series.setData(data); resolve(); }
      catch(e) {
        if (e && e.message === 'Value is null') {
          requestAnimationFrame(attempt);
        } else { resolve(); }
      }
    }
    attempt();
  });
}

function _computeRSI(closes, period) {
  var result = [];
  var gains = 0, losses = 0;
  for (var i = 0; i < closes.length; i++) {
    if (i < period) { result.push(null); continue; }
    var change = closes[i].close - closes[i-1].close;
    if (i === period) {
      for (var j = 1; j <= period; j++) {
        var ch = closes[j].close - closes[j-1].close;
        if (ch > 0) gains += ch; else losses -= ch;
      }
      gains  /= period;
      losses /= period;
    } else {
      var ch = change;
      gains  = (gains  * (period-1) + (ch>0?ch:0)) / period;
      losses = (losses * (period-1) + (ch<0?-ch:0)) / period;
    }
    var rs  = losses === 0 ? 100 : gains / losses;
    var rsi = 100 - 100/(1+rs);
    result.push({time: closes[i].time, value: Math.round(rsi*100)/100});
  }
  return result.filter(x => x !== null);
}

async function _loadTgaPanel(t, d) {
  _tgaData = null;
  if (!_indState.sma30 && !_indState.st && !_indState.tga) {
    _safeSetData(_sma30Series, []);
    _safeSetData(_stUpSeries,  []);
    _safeSetData(_stDownSeries,[]);
    _safeSetData(_macdSeries,  []);
    _safeSetData(_stochSeries, []);
    return;
  }
  try {
    var data = await fetch('/api/chart-tga-panel?ticker=' + t + '&days=' + d).then(r=>r.json());
    if (data.error) { console.warn('TGA panel error', data.error); return; }
    _tgaData = data;
    _applyTgaData();
    if (_indState.tga) {
      try { _macdChart.timeScale().fitContent(); } catch(e) {}
      try { _stochChart.timeScale().fitContent(); } catch(e) {}
    }
  } catch(e) { console.warn('TGA panel fetch failed', e); }
}

function _applyTgaData() {
  if (!_tgaData) return;
  var d = _tgaData;

  // SMA(30) overlay — orange line on main chart
  _safeSetData(_sma30Series, _indState.sma30 ? (d.sma30||[]) : []);

  // SuperTrend — two colored segments (green uptrend / red downtrend)
  if (_indState.st && d.supertrend && d.supertrend.length) {
    var stUp = [], stDown = [];
    d.supertrend.forEach(function(p) {
      if (p.direction === 'uptrend') stUp.push({time:p.time, value:p.value});
      else                          stDown.push({time:p.time, value:p.value});
    });
    _safeSetData(_stUpSeries,   stUp);
    _safeSetData(_stDownSeries, stDown);
  } else {
    _safeSetData(_stUpSeries,   []);
    _safeSetData(_stDownSeries, []);
  }

  // MACD histogram with TOS color scheme
  _safeSetData(_macdSeries, _indState.tga ? (d.macd_hist||[]) : []);

  // Stochastic FullD
  _safeSetData(_stochSeries, _indState.tga ? (d.stoch_fulld||[]) : []);

  // SMA cross markers — merge with signal markers
  var allMarkers = _sigMarkers.slice();
  if (_indState.sma30 && d.sma_cross_markers) {
    d.sma_cross_markers.forEach(function(m) {
      allMarkers.push({
        time:     m.time,
        position: m.dir === 'above' ? 'belowBar' : 'aboveBar',
        color:    m.dir === 'above' ? '#e3a02c' : '#8b949e',
        shape:    m.dir === 'above' ? 'arrowUp' : 'arrowDown',
        text:     'SMA',
      });
    });
  }
  allMarkers.sort(function(a,b){ return a.time < b.time ? -1 : 1; });
  _candleSeries.setMarkers(allMarkers);
}

async function loadChart(tickerOverride, daysOverride) {
  var t = tickerOverride || document.getElementById('chart-ticker').value.trim().toUpperCase();
  var d = daysOverride || _currentDays;
  if (!t) return;
  _currentTicker = t;
  _currentDays   = d;
  document.getElementById('chart-ticker').value = t;
  document.getElementById('chart-ticker-label').textContent = t;

  // Update period buttons
  document.getElementById('intraday-freq').style.display = 'none';
  document.querySelectorAll('.period-btn').forEach(function(b) {
    b.classList.remove('active');
    if ((d===7&&b.textContent==='1W')||(d===30&&b.textContent==='1M')||(d===90&&b.textContent==='3M')||
        (d===180&&b.textContent==='6M')||(d===365&&b.textContent==='1Y')) b.classList.add('active');
  });
  _currentIntraday = false;

  // Create chart instances on first call; reuse on subsequent calls
  _createCharts();

  var [ohlcv, inds] = await Promise.all([
    fetch('/api/chart-data?ticker=' + t + '&days=' + d).then(r=>r.json()),
    fetch('/api/chart-indicators?ticker=' + t + '&days=' + d).then(r=>r.json()),
  ]);

  if (ohlcv.error || !ohlcv.candles) {
    document.getElementById('chart-signal').textContent = 'No data';
    return;
  }

  _syncEnabled = false;

  // Build OHLCV lookup map for crosshair hover
  _ohlcvMap = {};
  ohlcv.candles.forEach(function(c) { _ohlcvMap[c.time] = c; });
  // Seed OHLCV bar with last candle
  if (ohlcv.candles.length) {
    var last = ohlcv.candles[ohlcv.candles.length - 1];
    _updateOhlcvBar({ time: last.time, seriesData: new Map() });
  }

  await _safeSetData(_candleSeries, ohlcv.candles);
  await _safeSetData(_ema20Series, _indState.ema20 ? (inds.ema20||[]) : []);
  await _safeSetData(_ema50Series, _indState.ema50 ? (inds.ema50||[]) : []);
  await _safeSetData(_vwapSeries,  _indState.vwap  ? (inds.vwap_line||[]) : []);

  var volData = ohlcv.candles.map(function(c) {
    return {time:c.time, value:c.volume, color: c.close>=c.open?'#1f6feb':'#8b2020'};
  });
  await _safeSetData(_volSeries, volData);

  var rsiData = _computeRSI(ohlcv.candles, 14);
  await _safeSetData(_rsiSeries, rsiData);

  // Fibonacci lines
  _fibLines.forEach(function(l) { try { _candleSeries.removePriceLine(l); } catch(e) {} });
  _fibLines = [];
  if (_indState.fib && inds.fib_levels) {
    var colors = ['#8b949e','#3fb950','#d29922','#58a6ff','#8b949e'];
    var legend = '';
    inds.fib_levels.levels.forEach(function(lv, i) {
      var pl = _candleSeries.createPriceLine({
        price: lv.price, color: colors[i]||'#8b949e',
        lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed,
        axisLabelVisible: false, title: lv.name,
      });
      _fibLines.push(pl);
      legend += '<span class="fib-badge" style="color:' + (colors[i]||'#8b949e') + '">'
              + lv.name + ' $' + lv.price.toFixed(2) + '</span>';
    });
    document.getElementById('fib-legend').innerHTML = legend;
  } else {
    document.getElementById('fib-legend').innerHTML = '';
  }

  // Signal markers — store for TGA merge
  _sigMarkers = [];
  if (_indState.sigs && inds.signals && inds.signals.length) {
    _sigMarkers = inds.signals.map(function(s) {
      return {
        time:     s.time,
        position: s.action === 'BUY' ? 'belowBar' : 'aboveBar',
        color:    s.action === 'BUY' ? '#3fb950' : '#f85149',
        shape:    s.action === 'BUY' ? 'arrowUp' : 'arrowDown',
        text:     s.action,
      };
    });
    _candleSeries.setMarkers(_sigMarkers);
  } else {
    _candleSeries.setMarkers([]);
  }

  _syncEnabled = true;
  _chart.timeScale().fitContent();
  _volChart.timeScale().fitContent();
  _rsiChart.timeScale().fitContent();

  // TGA panel — async, non-blocking; restore visibility if toggle is on (may have been hidden by intraday guard)
  if (_indState.tga) {
    ['macd-label','macd-chart','stoch-label','stoch-chart'].forEach(function(id) {
      var el = document.getElementById(id); if (el) el.style.display = 'block';
    });
  }
  _loadTgaPanel(t, d);

  // Load latest signal data for the AI bar
  loadAiBar(t);
}

async function loadChartIntraday(tickerOverride) {
  var t = tickerOverride || document.getElementById('chart-ticker').value.trim().toUpperCase();
  if (!t) return;
  _currentTicker  = t;
  _currentIntraday = true;
  document.getElementById('chart-ticker').value = t;
  document.getElementById('chart-ticker-label').textContent = t + ' (1D intraday)';

  document.querySelectorAll('.period-btn').forEach(function(b) {
    b.classList.remove('active');
    if (b.textContent === '1D') b.classList.add('active');
  });
  document.getElementById('intraday-freq').style.display = 'inline-block';

  _createCharts();

  var freq = parseInt(document.getElementById('intraday-freq').value || '5', 10);
  var ohlcv = await fetch('/api/chart-intraday?ticker=' + t + '&freq=' + freq).then(r=>r.json()).catch(function(){return{error:'fetch failed'};});
  if (ohlcv.error || !ohlcv.candles || !ohlcv.candles.length) {
    document.getElementById('chart-ticker-label').textContent = t + ' (1D — unavailable, showing daily)';
    document.querySelectorAll('.period-btn').forEach(function(b) {
      b.classList.remove('active');
      if (b.textContent==='3M') b.classList.add('active');
    });
    _currentIntraday = false;
    _currentDays = 90;
    await loadChart(t, 90);
    return;
  }

  // Intraday candles use unix epoch seconds; configure chart for that
  _chart.applyOptions({ timeScale: { timeVisible: true, secondsVisible: false } });
  _volChart.applyOptions({ timeScale: { timeVisible: true, secondsVisible: false } });
  _rsiChart.applyOptions({ timeScale: { timeVisible: true, secondsVisible: false } });

  _syncEnabled = false;
  await _safeSetData(_candleSeries,  ohlcv.candles);
  await _safeSetData(_ema20Series,   []);
  await _safeSetData(_ema50Series,   []);
  await _safeSetData(_vwapSeries,    []);
  await _safeSetData(_sma30Series,   []);
  await _safeSetData(_stUpSeries,    []);
  await _safeSetData(_stDownSeries,  []);
  await _safeSetData(_macdSeries,    []);
  await _safeSetData(_stochSeries,   []);

  var volData = ohlcv.candles.map(function(c) {
    return {time:c.time, value:c.volume, color: c.close>=c.open?'#1f6feb':'#8b2020'};
  });
  await _safeSetData(_volSeries, volData);

  var rsiData = _computeRSI(ohlcv.candles, 14);
  await _safeSetData(_rsiSeries, rsiData);
  _tgaData = null;
  _sigMarkers = [];
  _syncEnabled = true;

  _fibLines.forEach(function(l) { try { _candleSeries.removePriceLine(l); } catch(e) {} });
  _fibLines = [];
  document.getElementById('fib-legend').innerHTML = '';
  _candleSeries.setMarkers([]);

  _chart.timeScale().fitContent();
  _volChart.timeScale().fitContent();
  _rsiChart.timeScale().fitContent();

  // TGA panel (MACD/Stoch) uses daily bars — hide for intraday
  ['macd-label','macd-chart','stoch-label','stoch-chart'].forEach(function(id) {
    var el = document.getElementById(id); if (el) el.style.display = 'none';
  });

  loadAiBar(t);
}

async function loadAiBar(ticker) {
  try {
    var sigs = await fetch('/api/signals').then(r=>r.json());
    var s = (sigs.signals||sigs||[]).find(x=>x.ticker===ticker);
    if (s) {
      var el = document.getElementById('chart-signal');
      el.textContent = s.signal || '—';
      el.style.background = s.signal==='BUY'||s.signal==='STRONG_BUY' ? 'rgba(63,185,80,0.2)'
        : s.signal==='SELL'||s.signal==='STRONG_SELL' ? 'rgba(248,81,73,0.2)' : '#21262d';
      el.style.color = s.signal==='BUY'||s.signal==='STRONG_BUY' ? '#3fb950'
        : s.signal==='SELL'||s.signal==='STRONG_SELL' ? '#f85149' : '#e6edf3';
      document.getElementById('chart-composite').textContent = s.composite_score!=null ? (s.composite_score>=0?'+':'') + s.composite_score.toFixed(3) : '—';
      document.getElementById('chart-momentum').textContent  = s.momentum_score!=null  ? (s.momentum_score>=0?'+':'')  + s.momentum_score.toFixed(3)  : '—';
      document.getElementById('chart-mos').textContent       = s.margin_of_safety!=null ? (s.margin_of_safety*100).toFixed(1)+'%' : '—';
      var tgaN = s.tga_arrows != null ? s.tga_arrows : (s.tga_arrows_count != null ? s.tga_arrows_count : null);
      var tgaEl = document.getElementById('chart-tga');
      if (tgaEl) {
        if (tgaN != null) {
          var tgaCol = tgaN===3?'#3fb950':tgaN===2?'#d29922':'#8b949e';
          tgaEl.textContent = tgaN + '/3 \\u25b2';
          tgaEl.style.color = tgaCol;
          tgaEl.title = s.tga_reason || '';
        } else {
          tgaEl.textContent = '—';
          tgaEl.style.color = '#8b949e';
        }
      }
    } else {
      document.getElementById('chart-signal').textContent = 'No signal';
    }
  } catch(e) { console.warn('[NWO] chart signal',e); }
}

function toggleIndInfo(e) {
  var panel = document.getElementById('ind-info-panel');
  var btn   = document.getElementById('ind-info-btn');
  var show  = panel.style.display === 'none' || !panel.style.display;
  panel.style.display = show ? 'block' : 'none';
  if (show) {
    var rect = btn.getBoundingClientRect();
    var wrap = btn.closest('.chart-wrap');
    var wrapRect = wrap ? wrap.getBoundingClientRect() : {left:0,top:0};
    panel.style.top  = (rect.bottom - wrapRect.top + 4) + 'px';
    panel.style.left = Math.max(0, rect.left - wrapRect.left - 60) + 'px';
  }
  e.stopPropagation();
}
document.addEventListener('click', function(e) {
  var panel = document.getElementById('ind-info-panel');
  if (panel && !panel.contains(e.target) && e.target.id !== 'ind-info-btn') {
    panel.style.display = 'none';
  }
});

function toggleInd(key) {
  _indState[key] = !_indState[key];
  var on = _indState[key];
  var btn = document.getElementById('tog-' + key);
  if (btn) { btn.className = 'ind-toggle' + (on ? ' on' : ''); }

  // TGA pane visibility
  if (key === 'tga') {
    var show = on ? 'block' : 'none';
    ['macd-label','macd-chart','stoch-label','stoch-chart'].forEach(function(id) {
      var el = document.getElementById(id);
      if (el) el.style.display = show;
    });
    if (on) {
      if (!_tgaData) {
        _loadTgaPanel(_currentTicker, _currentDays);
      } else {
        _applyTgaData();
      }
      return;
    }
    return;
  }

  // SMA30 / SuperTrend — load TGA panel if not yet fetched
  if ((key === 'sma30' || key === 'st') && on && !_tgaData) {
    _loadTgaPanel(_currentTicker, _currentDays);
    return;
  }

  // For SMA30/ST toggle off, just reapply to clear the series
  if ((key === 'sma30' || key === 'st') && _tgaData) {
    _applyTgaData();
    return;
  }

  loadChart(_currentTicker, _currentDays);
}

async function runDiagnostic() {
  var ov = document.getElementById('sg-diag-overlay');
  var body = document.getElementById('sg-diag-body');
  var title = document.getElementById('sg-diag-title');
  var ticker = _currentTicker;
  title.textContent = '\\ud83d\\udd0d Diagnosing ' + ticker + '\\u2026';
  body.innerHTML = '<span style="color:#8b949e">Running full AI pipeline…</span>';
  ov.style.display = 'flex';
  try {
    var _diagModel = (typeof window !== 'undefined' && window._PAPER_MODEL) || 'standard';
    var d = await fetch('/api/trade-diagnostic?ticker=' + ticker + '&model=' + _diagModel).then(r => r.json());
    if (d.error) { body.innerHTML = '<span style="color:#f85149">Error: ' + d.error + '</span>'; return; }
    title.textContent = '\\ud83d\\udd0d ' + ticker + ' — ' + (d.go_no_go ? '\\u2705 GO: ' + d.action : '\\u274c NO-GO: BLOCKED');
    var sc = d.scores || {};
    var html = '<table style="width:100%;border-collapse:collapse;">'
      + '<tr><td colspan="2" style="padding:4px 8px;border-bottom:1px solid #21262d;font-weight:700;color:#58a6ff;">Fundamentals</td></tr>'
      + '<tr><td style="padding:2px 8px;">Investable</td><td>' + (d.investable ? '\\u2705 Yes' : '\\u274c No') + '</td></tr>'
      + '<tr><td style="padding:2px 8px;">Moat</td><td>' + (d.moat||'?') + '</td></tr>'
      + '<tr><td style="padding:2px 8px;">Margin of Safety</td><td>' + ((d.margin_of_safety||0)*100).toFixed(1) + '%</td></tr>'
      + '<tr><td style="padding:2px 8px;">ROIC/WACC</td><td>' + ((d.roic||0)*100).toFixed(1) + '%/' + ((d.wacc||0)*100).toFixed(1) + '%</td></tr>'
      + '<tr><td colspan="2" style="padding:4px 8px;border-bottom:1px solid #21262d;font-weight:700;color:#58a6ff;padding-top:10px;">Scores — composite: ' + (d.composite_score||0).toFixed(3) + '</td></tr>'
      + Object.entries(sc).map(([k,v]) => '<tr><td style="padding:2px 8px;">' + k + '</td><td style="color:' + (v>0?'#3fb950':v<0?'#f85149':'#8b949e') + ';">' + (v>=0?'+':'') + v.toFixed(3) + '</td></tr>').join('')
      + '<tr><td colspan="2" style="padding:4px 8px;border-bottom:1px solid #21262d;font-weight:700;color:#58a6ff;padding-top:10px;">Gates</td></tr>'
      + (d.gates_passed||[]).filter(g=>!g.includes('BYPASSED')).map(g=>'<tr><td colspan="2" style="padding:2px 8px;color:#3fb950;">\\u2713 '+g+'</td></tr>').join('')
      + (d.gates_passed||[]).filter(g=>g.includes('BYPASSED')).map(g=>'<tr><td colspan="2" style="padding:2px 8px;color:#d29922;">\\u21bb '+g+'</td></tr>').join('')
      + (d.gates_failed||[]).map(g=>'<tr><td colspan="2" style="padding:2px 8px;color:#f85149;">\\u2717 '+g+'</td></tr>').join('')
      + '</table>';
    if (d.blocked_at) html += '<div style="margin-top:10px;padding:8px;background:rgba(248,81,73,0.1);border-radius:6px;border-left:3px solid #f85149;"><b style="color:#f85149;">Blocked:</b> ' + d.blocked_at + '</div>';
    body.innerHTML = html;
  } catch(e) { body.innerHTML = '<span style="color:#f85149">Error: ' + e.message + '</span>'; }
}

document.getElementById('chart-ticker').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') loadChart(this.value.trim().toUpperCase(), _currentDays);
});

// Period buttons need ticker context — re-bind them
document.querySelectorAll('.period-btn').forEach(function(b) {
  b.onclick = function() {
    var txt = b.textContent.trim();
    if (txt === '1D') { loadChartIntraday(_currentTicker); return; }
    var days = txt==='1W'?7:txt==='1M'?30:txt==='3M'?90:txt==='6M'?180:365;
    loadChart(_currentTicker, days);
  };
});

document.getElementById('intraday-freq').addEventListener('change', function() {
  if (_currentIntraday) loadChartIntraday(_currentTicker);
});

// Frame 1: create chart instances (gives LightweightCharts one full frame to init its canvas)
// Frame 2: load data — by this point the canvas context is guaranteed non-null
requestAnimationFrame(function() {
  _createCharts();
  var _initTicker = new URLSearchParams(window.location.search).get('ticker') || 'AAPL';
  var _initDays   = parseInt(new URLSearchParams(window.location.search).get('days') || '90', 10);
  requestAnimationFrame(function() { loadChart(_initTicker, _initDays); });
});
</script>
</body>
</html>"""

# Fill in nav/tape/CSS
_CHARTS_HTML = _CHARTS_HTML.replace("{NAV_CSS}", _NAV_CSS)
_CHARTS_HTML = _CHARTS_HTML.replace("{NAV}", _nav_html("charts"))
_CHARTS_HTML = _CHARTS_HTML.replace("{TAPE_HTML}", _NAV_TAPE_HTML)
_CHARTS_HTML = _CHARTS_HTML.replace("{PAGE_INFO}", _page_info_html("charts"))


@app.get("/charts", response_class=HTMLResponse)
def page_charts():
    return HTMLResponse(_CHARTS_HTML)


# Add "Charts" page info entry
if "charts" not in _PAGE_INFO:
    _PAGE_INFO["charts"] = (
        '<b>Charts</b> — TradingView Lightweight Charts with daily OHLCV candlesticks, '
        'EMA 20/50, VWAP, Fibonacci retracement levels, and AI buy/sell signal markers. '
        'Time range: 1M / 3M / 6M / 1Y. Click <b>Full Diagnostic</b> to run the live AI gate analysis for any ticker.'
    )

# Phase 2c — Chart buttons already injected above in _DIAG_BTN patch.
# (btns += chart link added alongside the diagnose button after the info button)


# ── Redesigned Paper Trade Dashboard (compare page) ───────────────────────────
# Overrides the _COMPARE_HTML defined earlier.  /paper/compare now shows the
# full 4-model view with two swim lanes per model: Open Positions + Trade History
# grouped by date.  Main-nav "Paper Trade" button already redirected here above.
_COMPARE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Paper Trade Dashboard &mdash; NWO</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d1117; color: #e6edf3; font-family: 'Segoe UI', system-ui, sans-serif; font-size: 14px; }
  a { color: inherit; text-decoration: none; }

  /* ── Header ─────────────────────────────────────────────────────────────── */
  header { background: #161b22; padding: 12px 24px; border-bottom: 1px solid #30363d;
           display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
           position: sticky; top: 0; z-index: 100; }
  .back-btn { padding: 5px 12px; border-radius: 6px; border: 1px solid #30363d;
              background: #21262d; color: #8b949e; font-size: 12px; }
  .back-btn:hover { color: #e6edf3; }
  header h1 { font-size: 18px; font-weight: 700; color: #58a6ff; letter-spacing: 0.3px; }
  .hdr-btns { margin-left: auto; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .hdr-btn { padding: 6px 14px; border-radius: 6px; border: 1px solid #30363d;
             background: #21262d; color: #8b949e; cursor: pointer; font-size: 12px;
             font-family: inherit; }
  .hdr-btn:hover { color: #e6edf3; border-color: #8b949e; }

  /* ── Model-info panel ───────────────────────────────────────────────────── */
  .model-info { background: #161b22; border-bottom: 1px solid #30363d; padding: 16px 24px; }
  .info-hdr { font-size: 11px; text-transform: uppercase; letter-spacing: 1.2px;
              color: #8b949e; margin-bottom: 12px; }
  .info-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
  .info-card { background: #0d1117; border-radius: 8px; padding: 12px 14px;
               border-left: 3px solid #30363d; }
  .info-card.c-std  { border-left-color: #8b949e; }
  .info-card.c-rel  { border-left-color: #d29922; }
  .info-card.c-vrel { border-left-color: #3fb950; }
  .info-card.c-ai   { border-left-color: #58a6ff; }
  .info-name { font-size: 12px; font-weight: 700; margin-bottom: 5px; }
  .c-std .info-name  { color: #8b949e; }
  .c-rel .info-name  { color: #d29922; }
  .c-vrel .info-name { color: #3fb950; }
  .c-ai .info-name   { color: #58a6ff; }
  .info-desc { font-size: 11px; color: #8b949e; line-height: 1.6; }
  @media (max-width: 1000px) { .info-grid { grid-template-columns: 1fr 1fr; } }

  main { padding: 16px 14px; }

  /* ── 4 model columns ────────────────────────────────────────────────────── */
  .model-cols { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; }
  @media (max-width: 1200px) { .model-cols { grid-template-columns: 1fr 1fr; } }
  @media (max-width: 620px)  { .model-cols { grid-template-columns: 1fr; } }

  .model-col { display: flex; flex-direction: column; border-radius: 10px; overflow: hidden;
               background: #161b22; border: 1px solid #30363d; }

  /* ── Column header (account stats) ─────────────────────────────────────── */
  .col-hdr { padding: 14px 16px 12px; border-bottom: 1px solid #21262d; }
  .col-standard    .col-hdr { border-top: 4px solid #8b949e; }
  .col-relaxed     .col-hdr { border-top: 4px solid #d29922; }
  .col-very-relaxed .col-hdr { border-top: 4px solid #3fb950; }
  .col-claude      .col-hdr { border-top: 4px solid #58a6ff; }

  .col-title-row { display: flex; align-items: baseline; justify-content: space-between;
                   margin-bottom: 2px; }
  .col-title { font-size: 15px; font-weight: 700; }
  .col-standard    .col-title { color: #c9d1d9; }
  .col-relaxed     .col-title { color: #d29922; }
  .col-very-relaxed .col-title { color: #3fb950; }
  .col-claude      .col-title { color: #58a6ff; }
  .col-open-link { font-size: 11px; color: #58a6ff; opacity: 0.7; }
  .col-open-link:hover { opacity: 1; text-decoration: underline; }
  .col-sub { font-size: 11px; color: #6e7681; margin-bottom: 12px; }

  /* Stats — 2-col grid, 3-col variant for expanded view */
  .stat-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px 12px; }
  .stat-grid-3 { grid-template-columns: 1fr 1fr 1fr; gap: 8px 10px; }
  .stat-item { display: flex; flex-direction: column; gap: 2px; }
  .stat-lbl { font-size: 10px; color: #8b949e; text-transform: uppercase;
              letter-spacing: 0.6px; font-weight: 500; }
  .stat-val { font-size: 15px; font-weight: 700; line-height: 1.2; }
  .stat-sub { font-size: 10px; font-weight: 400; opacity: 0.8; }
  .up { color: #3fb950; } .dn { color: #f85149; } .neu { color: #c9d1d9; }
  /* Benchmark section */
  .col-benchmark { padding: 8px 12px 4px; border-top: 1px solid #21262d; font-size: 11px; }
  .bm-hdr { font-size: 10px; font-weight: 700; color: #8b949e; text-transform: uppercase;
            letter-spacing: 0.5px; margin-bottom: 5px; }
  .bm-row { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
  .bm-lbl { display: none; }
  .bm-model { font-weight: 700; }
  .bm-val { font-weight: 600; }
  .bm-sep { color: #30363d; margin: 0 2px; }
  .bm-alpha { font-size: 10px; padding: 1px 5px; border-radius: 3px;
              background: rgba(255,255,255,0.05); font-weight: 700; }
  .bm-row .pos { color: #3fb950; } .bm-row .neg { color: #f85149; }

  /* ── Swim lanes ─────────────────────────────────────────────────────────── */
  .swim-lane { border-top: 1px solid #21262d; display: flex; flex-direction: column; }
  .lane-hd { padding: 8px 12px; font-size: 11px; font-weight: 600; letter-spacing: 0.4px;
             display: flex; align-items: center; justify-content: space-between; }
  .lane-hd-pos  { color: #3fb950; background: rgba(63,185,80,0.06);
                  border-top: 2px solid rgba(63,185,80,0.4); }
  .lane-hd-hist { color: #58a6ff; background: rgba(88,166,255,0.06);
                  border-top: 2px solid rgba(88,166,255,0.4); }
  .lane-cnt { font-size: 10px; background: #21262d; padding: 1px 7px;
              border-radius: 10px; color: #8b949e; font-weight: 500; }
  .lane-body { max-height: 260px; overflow-y: auto; padding: 8px; display: flex;
               flex-direction: column; gap: 5px; }
  .lane-body::-webkit-scrollbar { width: 4px; }
  .lane-body::-webkit-scrollbar-thumb { background: #30363d; border-radius: 2px; }
  .lane-empty { color: #6e7681; font-size: 12px; font-style: italic;
                text-align: center; padding: 16px 0; }

  /* ── Position cards ─────────────────────────────────────────────────────── */
  .pos-card { background: #0d1117; border: 1px solid #21262d; border-radius: 6px;
              padding: 8px 10px; }
  .pos-top { display: flex; align-items: center; gap: 8px; }
  .pos-ticker { font-size: 14px; font-weight: 700; color: #e6edf3; }
  .pos-pnl { margin-left: auto; font-size: 13px; font-weight: 700; white-space: nowrap; }
  .pos-detail { font-size: 11px; color: #8b949e; margin-top: 3px; }

  /* ── Trade history ───────────────────────────────────────────────────────── */
  .date-hdr { font-size: 11px; font-weight: 600; color: #8b949e; padding: 6px 4px 3px;
              border-bottom: 1px solid #21262d; margin-top: 6px; letter-spacing: 0.2px; }
  .date-hdr:first-child { margin-top: 0; padding-top: 2px; }
  .trade-row { display: grid; grid-template-columns: auto auto 1fr auto auto;
               align-items: center; gap: 5px; padding: 5px 4px; border-radius: 4px; }
  .trade-row:hover { background: #1c2128; }
  .act-badge { font-size: 10px; font-weight: 700; padding: 2px 6px; border-radius: 4px;
               white-space: nowrap; text-align: center; }
  .badge-buy  { background: rgba(63,185,80,0.15); color: #3fb950;
                border: 1px solid rgba(63,185,80,0.4); }
  .badge-sell { background: rgba(248,81,73,0.15); color: #f85149;
                border: 1px solid rgba(248,81,73,0.4); }
  .tr-ticker { font-size: 13px; font-weight: 700; color: #e6edf3; }
  .tr-detail { font-size: 11px; color: #8b949e; overflow: hidden;
               text-overflow: ellipsis; white-space: nowrap; }
  .tr-total  { font-size: 12px; color: #c9d1d9; text-align: right; white-space: nowrap; }
  .tr-src    { font-size: 13px; }
</style>
</head>
<body>
<header>
  <a href="/" class="back-btn">&#8592; Dashboard</a>
  <h1>&#127918; Paper Trade Dashboard</h1>
  <div class="hdr-btns">
    <button class="hdr-btn" onclick="toggleInfo()" id="info-btn">&#8505; Model Info</button>
    <a href="/paper/daily-report" class="hdr-btn">&#128196; Daily Report</a>
    <a href="/paper/russell2000"  class="hdr-btn">&#128202; R2000</a>
    <button class="hdr-btn" onclick="loadAll()">&#8635; Refresh</button>
  </div>
</header>

<div class="model-info" id="model-info" style="display:none">
  <div class="info-hdr">Model Philosophies</div>
  <div class="info-grid">
    <div class="info-card c-std">
      <div class="info-name">Standard</div>
      <div class="info-desc">Full 6-layer pipeline at designed thresholds. The control group &mdash; requires strong fundamentals, ensemble bull prob &ge;45%, and passing Kalman/Reynolds filters. Trades only on unambiguous evidence.</div>
    </div>
    <div class="info-card c-rel">
      <div class="info-name">Relaxed &minus;25%</div>
      <div class="info-desc">Same pipeline as Standard, all gates reduced &minus;25%. Tests whether Standard is over-cautious. Accepts moderate conviction setups. Higher expected variance.</div>
    </div>
    <div class="info-card c-vrel">
      <div class="info-name">Very Relaxed &minus;50%</div>
      <div class="info-desc">Gates halved. High-frequency trend follower &mdash; weak-signal entries with stop-loss discipline. Stress-tests minimum-threshold alpha. High drawdown expected.</div>
    </div>
    <div class="info-card c-ai">
      <div class="info-name">&#129302; Claude (AI Momentum)</div>
      <div class="info-desc">ST&times;0.35 &middot; Mom&times;0.30 &middot; Insider&times;0.20 &middot; Technical&times;0.10 &middot; Fundamentals&times;0.05. Hard VIX gate &gt;30. Built to capture trending breakouts that pure value models miss.</div>
    </div>
  </div>
</div>

<main>
  <div class="model-cols" id="cols"></div>
</main>

<script>
var MODELS = [
  { key: 'standard',     label: 'Standard',                sub: 'Thresholds &times;1.00',                         cls: 'col-standard',     href: '/paper' },
  { key: 'relaxed',      label: 'Relaxed &minus;25%',      sub: 'Thresholds &times;0.75',                         cls: 'col-relaxed',      href: '/paper/relaxed' },
  { key: 'very_relaxed', label: 'Very Relaxed &minus;50%', sub: 'Thresholds &times;0.50',                         cls: 'col-very-relaxed', href: '/paper/very-relaxed' },
  { key: 'claude',       label: '&#129302; Claude',        sub: 'ST&times;0.35 &middot; Mom&times;0.30 &middot; VIX gate', cls: 'col-claude', href: '/paper/claude' },
];
var _trData = {};

function fmt(n, d) {
  if (d === undefined) d = 2;
  if (n == null) return '—';
  return '$' + Math.abs(n).toLocaleString('en-US', {minimumFractionDigits: d, maximumFractionDigits: d});
}
function fmtPct(n) { return n == null ? '—' : (n >= 0 ? '+' : '') + n.toFixed(2) + '%'; }
function cls(n) { return n > 0 ? 'up' : n < 0 ? 'dn' : 'neu'; }

function fmtDateHdr(s) {
  if (!s || s === 'unknown') return 'Unknown date';
  var p = s.split('-');
  var mon = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  return mon[+p[1]-1] + ' ' + +p[2] + ', ' + p[0];
}

function trBadge(ticker) {
  var d = _trData[ticker] || {};
  var ss = d.smart_score;
  if (ss == null) return '';
  var bg  = ss >= 8 ? '#1a4731' : ss >= 4 ? '#3d2b00' : '#4a1519';
  var col = ss >= 8 ? '#3fb950' : ss >= 4 ? '#d29922' : '#f85149';
  return ' <span style="background:' + bg + ';color:' + col + ';border:1px solid ' + col
       + ';font-size:9px;padding:1px 4px;border-radius:3px;font-weight:700">&#9733;' + ss + '</span>';
}

function buildCols() {
  var wrap = document.getElementById('cols');
  wrap.innerHTML = MODELS.map(function(m) {
    return '<div class="model-col ' + m.cls + '" id="col-' + m.key + '">'
      + '<div class="col-hdr">'
        + '<div class="col-title-row">'
          + '<span class="col-title">' + m.label + '</span>'
          + '<a class="col-open-link" href="' + m.href + '">Full view &rarr;</a>'
        + '</div>'
        + '<div class="col-sub">' + m.sub + '</div>'
        + '<div class="col-stats"><span style="color:#8b949e;font-size:11px">Loading&hellip;</span></div>'
        + '<div class="col-benchmark"></div>'
      + '</div>'
      + '<div class="swim-lane">'
        + '<div class="lane-hd lane-hd-pos">&#128200; Open Positions'
          + ' <span class="lane-cnt lane-cnt-pos">&mdash;</span></div>'
        + '<div class="lane-body lane-body-pos"><div class="lane-empty">Loading&hellip;</div></div>'
      + '</div>'
      + '<div class="swim-lane">'
        + '<div class="lane-hd lane-hd-hist">&#128203; Trade History'
          + ' <span class="lane-cnt lane-cnt-hist">&mdash;</span></div>'
        + '<div class="lane-body lane-body-hist"><div class="lane-empty">Loading&hellip;</div></div>'
      + '</div>'
    + '</div>';
  }).join('');
}

function renderCol(m, acct, trades, bench) {
  var colEl = document.getElementById('col-' + m.key);
  if (!colEl) return;

  // ── Account stats ──────────────────────────────────────────────────────────
  var statsEl = colEl.querySelector('.col-stats');
  var pC  = cls(acct.lifetime_pnl || 0), dC = cls(acct.daily_pnl || 0);
  var riC = cls(acct.total_pnl    || 0);
  var pS  = (acct.lifetime_pnl || 0) >= 0 ? '+' : '';
  var dS  = (acct.daily_pnl    || 0) >= 0 ? '+' : '';
  var riS = (acct.total_pnl    || 0) >= 0 ? '+' : '';
  var invested = acct.total_invested || 0;
  statsEl.innerHTML =
    '<div class="stat-grid stat-grid-3">'
    + '<div class="stat-item"><div class="stat-lbl">Equity</div>'
      + '<div class="stat-val neu">' + fmt(acct.total_equity, 0) + '</div></div>'
    + '<div class="stat-item"><div class="stat-lbl">Cash</div>'
      + '<div class="stat-val neu">' + fmt(acct.cash, 0) + '</div></div>'
    + '<div class="stat-item"><div class="stat-lbl">Invested</div>'
      + '<div class="stat-val neu">' + fmt(invested, 0) + '</div>'
      + '<div class="stat-sub" style="color:#8b949e;">'
      + (acct.total_equity > 0 ? Math.round(invested / acct.total_equity * 100) + '% deployed' : '—')
      + '</div></div>'
    + '<div class="stat-item"><div class="stat-lbl">Total P&amp;L</div>'
      + '<div class="stat-val ' + pC + '">' + pS + fmt(acct.lifetime_pnl, 0)
      + '</div><div class="stat-sub ' + pC + '">' + fmtPct(acct.lifetime_pnl_pct) + ' vs $100k</div></div>'
    + '<div class="stat-item"><div class="stat-lbl">Daily P&amp;L</div>'
      + '<div class="stat-val ' + dC + '">' + dS + fmt(acct.daily_pnl, 0)
      + '</div><div class="stat-sub ' + dC + '">' + fmtPct(acct.daily_pnl_pct) + ' vs $100k</div></div>'
    + '<div class="stat-item"><div class="stat-lbl">Ret. on Invested</div>'
      + '<div class="stat-val ' + riC + '">' + riS + fmt(acct.total_pnl, 0)
      + '</div><div class="stat-sub ' + riC + '">' + fmtPct(acct.total_pnl_pct) + ' of cost</div></div>'
    + '</div>';

  // ── Benchmark comparison ───────────────────────────────────────────────────
  var bmEl = colEl.querySelector('.col-benchmark');
  if (bmEl && bench) {
    var lPct  = acct.total_pnl_pct || 0;   // return on invested capital (unrealized / cost basis)
    var sp5   = bench.sp500_pct;
    var sp1   = bench.sp100_pct;
    var alpha5 = (sp5 != null) ? round2(lPct - sp5) : null;
    var alpha1 = (sp1 != null) ? round2(lPct - sp1) : null;
    var mC  = lPct >= 0 ? 'pos' : 'neg';
    var mS  = lPct >= 0 ? '+' : '';
    function bmRow(label, bPct, alpha) {
      if (bPct == null) return '';
      var bC  = bPct  >= 0 ? 'pos' : 'neg';
      var aC  = alpha >= 0 ? 'pos' : 'neg';
      var bS  = bPct  >= 0 ? '+' : '', aS = alpha >= 0 ? '+' : '';
      return '<div class="bm-row">'
        + '<span class="bm-lbl">' + label + '</span>'
        + '<span class="bm-model ' + mC + '" title="Return on invested capital (unrealized P&amp;L ÷ cost basis)">Model: ' + mS + lPct.toFixed(2) + '%</span>'
        + '<span class="bm-sep">|</span>'
        + '<span class="bm-val ' + bC + '">' + label + ': ' + bS + bPct.toFixed(2) + '%</span>'
        + '<span class="bm-sep">|</span>'
        + '<span class="bm-alpha ' + aC + '" title="Alpha vs ' + label + '">'
        + '&#945;: ' + aS + alpha.toFixed(2) + '%</span>'
        + '</div>';
    }
    bmEl.innerHTML =
      '<div class="bm-hdr">vs Benchmark <span style="color:#8b949e;font-size:10px;">since last reset</span></div>'
      + bmRow('S&amp;P 500', sp5, alpha5)
      + bmRow('S&amp;P 100', sp1, alpha1);
  }

  // ── Open Positions swim lane ───────────────────────────────────────────────
  var posEl  = colEl.querySelector('.lane-body-pos');
  var posCnt = colEl.querySelector('.lane-cnt-pos');
  var positions = acct.positions || [];
  if (posCnt) posCnt.textContent = positions.length;
  if (!positions.length) {
    posEl.innerHTML = '<div class="lane-empty">No open positions</div>';
  } else {
    posEl.innerHTML = positions.map(function(p) {
      var pC2 = cls(p.pnl || 0);
      var pStr = (p.pnl >= 0 ? '+' : '') + fmt(p.pnl) + ' (' + fmtPct(p.pnl_pct) + ')';
      return '<div class="pos-card">'
        + '<div class="pos-top">'
          + '<span class="pos-ticker">' + p.ticker + trBadge(p.ticker) + '</span>'
          + '<span class="pos-pnl ' + pC2 + '">' + pStr + '</span>'
        + '</div>'
        + '<div class="pos-detail">'
          + p.qty + ' sh &nbsp;&middot;&nbsp; avg ' + fmt(p.avg_cost)
          + ' &rarr; ' + fmt(p.cur_price)
        + '</div>'
      + '</div>';
    }).join('');
  }

  // ── Trade History swim lane (grouped by date, newest first) ───────────────
  var histEl  = colEl.querySelector('.lane-body-hist');
  var histCnt = colEl.querySelector('.lane-cnt-hist');
  var tradeArr = trades || [];
  if (histCnt) histCnt.textContent = tradeArr.length;
  if (!tradeArr.length) {
    histEl.innerHTML = '<div class="lane-empty">No trades yet</div>';
    return;
  }
  var groups = {}, order = [];
  tradeArr.forEach(function(t) {
    var dt = t.timestamp ? t.timestamp.slice(0, 10) : 'unknown';
    if (!groups[dt]) { groups[dt] = []; order.push(dt); }
    groups[dt].push(t);
  });
  var html = '';
  order.forEach(function(dateStr) {
    html += '<div class="date-hdr">' + fmtDateHdr(dateStr) + '</div>';
    groups[dateStr].forEach(function(t) {
      var isBuy = t.action === 'BUY';
      var src  = t.signal === 'MANUAL' ? '&#128100;' : '&#129302;';
      var timeStr = t.timestamp ? t.timestamp.slice(11, 16) : '';
      html += '<div class="trade-row">'
        + '<span class="act-badge ' + (isBuy ? 'badge-buy' : 'badge-sell') + '">' + t.action + '</span>'
        + '<span class="tr-ticker">' + t.ticker + '</span>'
        + '<span class="tr-detail">' + t.qty + 'sh @ ' + fmt(t.price) + (timeStr ? ' ' + timeStr : '') + '</span>'
        + '<span class="tr-total">' + fmt(Math.abs(t.total || 0), 0) + '</span>'
        + '<span class="tr-src">' + src + '</span>'
        + '</div>';
    });
  });
  histEl.innerHTML = html;
}

var _benchmarkData = null;

function round2(v) { return Math.round(v * 100) / 100; }

async function fetchBenchmarks(since) {
  try {
    var url = '/api/paper/benchmarks' + (since ? '?since=' + since : '');
    _benchmarkData = await fetch(url).then(function(r) { return r.json(); });
  } catch(e) { _benchmarkData = {}; }
}

async function loadModel(m, prefetchedAcct) {
  try {
    var acctPromise = prefetchedAcct
      ? Promise.resolve(prefetchedAcct)
      : fetch('/api/paper/account?model=' + m.key).then(function(r) { return r.json(); });
    var results = await Promise.all([
      acctPromise,
      fetch('/api/paper/trades?model=' + m.key).then(function(r) { return r.json(); }),
    ]);
    renderCol(m, results[0], results[1], _benchmarkData);
  } catch(e) {
    var el = document.getElementById('col-' + m.key);
    if (el) el.querySelector('.col-stats').innerHTML =
      '<span style="color:#f85149;font-size:11px">Error: ' + e.message + '</span>';
  }
}

async function loadAll() {
  try { _trData = await fetch('/api/tipranks/all').then(function(r) { return r.json(); }); } catch(e) { if(window._nwoErr)_nwoErr(e);else console.error('[NWO]',e); }
  // Fetch all accounts once — reuse results for both benchmark window and renderCol
  var accts = await Promise.all(MODELS.map(function(m) {
    return fetch('/api/paper/account?model=' + m.key).then(function(r) { return r.json(); }).catch(function() { return {}; });
  }));
  // Since date is computed server-side from reset_at — no need to pass it from JS
  await fetchBenchmarks();
  await Promise.all(MODELS.map(function(m, i) { return loadModel(m, accts[i]); }));
}

function toggleInfo() {
  var el  = document.getElementById('model-info');
  var btn = document.getElementById('info-btn');
  if (el.style.display === 'none') { el.style.display = 'block'; btn.textContent = '✕ Hide Info'; }
  else { el.style.display = 'none'; btn.textContent = 'ℹ Model Info'; }
}

buildCols();
loadAll();
setInterval(loadAll, 30000);
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════════
# Pipeline Health Page — /pipeline
# Visual stage-by-stage health view + per-ticker decision trace + restore points
# ═══════════════════════════════════════════════════════════════════════════════

import json as _json_mod
import shutil as _shutil


@app.get("/api/pipeline/health")
def api_pipeline_health():
    """
    Returns per-stage health data for the 6-layer pipeline.
    Stage 1: Data Ingestion  (price_history rows)
    Stage 2: Fundamentals    (fundamentals rows, owner_earnings coverage)
    Stage 3: Aggregation     (last signal run time, ticker coverage)
    Stage 4: FUD Filter      (fud_score distribution in recent signals)
    Stage 5: Decision Engine (recent signals, confidence dist, null IV count)
    Stage 6: Paper Execution (open positions, last trade)
    """
    import statistics as _stats

    result = {}

    # ── Stage 1: Data Ingestion ───────────────────────────────────────────────
    with Session() as s:
        total_companies = s.query(Company).count()
        total_prices    = s.query(PriceHistory).count()
        tickers_no_price = []
        tickers_stale    = []
        for t in config.watchlist:
            co = s.query(Company).filter_by(ticker=t).first()
            if not co:
                tickers_no_price.append(t)
                continue
            cnt = s.query(PriceHistory).filter_by(company_id=co.id).count()
            if cnt == 0:
                tickers_no_price.append(t)
            else:
                latest_ph = (
                    s.query(PriceHistory)
                    .filter_by(company_id=co.id)
                    .order_by(PriceHistory.date.desc())
                    .first()
                )
                if latest_ph and latest_ph.date:
                    age_days = (datetime.utcnow() - latest_ph.date).days
                    if age_days > 3:
                        tickers_stale.append({"ticker": t, "age_days": age_days})
        result["ingestion"] = {
            "stage": 1,
            "name": "Data Ingestion",
            "companies": total_companies,
            "price_rows": total_prices,
            "watchlist_count": len(config.watchlist),
            "missing_prices": tickers_no_price,
            "stale_prices": tickers_stale,
            "status": "warn" if (tickers_no_price or tickers_stale) else "ok",
        }

    # ── Stage 2: Fundamental Analysis ────────────────────────────────────────
    with Session() as s:
        tickers_no_fund   = []
        tickers_no_oe     = []
        fund_coverage     = 0
        for t in config.watchlist:
            co = s.query(Company).filter_by(ticker=t).first()
            if not co:
                continue
            annual = (
                s.query(Fundamental)
                .filter_by(company_id=co.id, fiscal_quarter=0)
                .order_by(Fundamental.fiscal_year.desc())
                .first()
            )
            if not annual:
                tickers_no_fund.append(t)
            else:
                fund_coverage += 1
                if annual.owner_earnings is None:
                    tickers_no_oe.append(t)
        result["fundamentals"] = {
            "stage": 2,
            "name": "Fundamental Analysis",
            "covered": fund_coverage,
            "missing_fundamentals": tickers_no_fund,
            "missing_owner_earnings": tickers_no_oe,
            "status": "warn" if (tickers_no_fund or tickers_no_oe) else "ok",
        }

    # ── Stages 3-5: pull from recent TradeSignals ─────────────────────────────
    with Session() as s:
        # Most recent signal per ticker
        from sqlalchemy import func as _func
        subq = (
            s.query(
                TradeSignal.company_id,
                _func.max(TradeSignal.id).label("max_id")
            )
            .group_by(TradeSignal.company_id)
            .subquery()
        )
        recent_sigs = (
            s.query(TradeSignal)
            .join(subq, TradeSignal.id == subq.c.max_id)
            .order_by(TradeSignal.generated_at.desc())
            .limit(200)
            .all()
        )

        # Stage 3: Aggregation coverage
        sig_tickers    = set()
        sig_timestamps = []
        for sig in recent_sigs:
            co = s.query(Company).filter_by(id=sig.company_id).first()
            if co:
                sig_tickers.add(co.ticker)
            if sig.generated_at:
                sig_timestamps.append(sig.generated_at)

        last_run = max(sig_timestamps).strftime("%Y-%m-%d %H:%M ET") if sig_timestamps else None
        missing_from_last_run = [t for t in config.watchlist if t not in sig_tickers]

        result["aggregation"] = {
            "stage": 3,
            "name": "Signal Aggregation",
            "tickers_in_db": len(sig_tickers),
            "last_run": last_run,
            "missing_from_last_run": missing_from_last_run,
            "status": "warn" if missing_from_last_run else "ok",
        }

        # Stage 4: FUD Filter
        fud_scores    = [sig.fud_score for sig in recent_sigs if sig.fud_score is not None]
        fud_blocked   = sum(1 for f in fud_scores if f < 0.3)
        fud_null      = sum(1 for sig in recent_sigs if sig.fud_score is None)
        result["fud_filter"] = {
            "stage": 4,
            "name": "FUD Filter",
            "signals_checked": len(recent_sigs),
            "fud_null": fud_null,
            "fud_blocked": fud_blocked,
            "fud_mean": round(_stats.mean(fud_scores), 3) if fud_scores else None,
            "status": "warn" if fud_null > 5 else "ok",
        }

        # Stage 5: Decision Engine — silent failures
        null_iv        = sum(1 for sig in recent_sigs if sig.intrinsic_value_estimate is None)
        null_mos       = sum(1 for sig in recent_sigs if sig.margin_of_safety is None)
        null_conf      = sum(1 for sig in recent_sigs if sig.confidence is None)
        low_conf       = sum(1 for sig in recent_sigs if sig.confidence is not None and sig.confidence < 0.05)
        buy_count      = sum(1 for sig in recent_sigs if sig.signal == "BUY")
        hold_count     = sum(1 for sig in recent_sigs if sig.signal == "HOLD")
        sell_count     = sum(1 for sig in recent_sigs if sig.signal == "SELL")
        confs          = [sig.confidence for sig in recent_sigs if sig.confidence is not None]

        silent_failures = []
        if null_iv > 0:
            silent_failures.append(f"{null_iv} signals missing intrinsic_value_estimate")
        if null_mos > 0:
            silent_failures.append(f"{null_mos} signals missing margin_of_safety")
        if low_conf > 5:
            silent_failures.append(f"{low_conf} signals with confidence < 0.05 (near-zero)")
        if null_conf > 0:
            silent_failures.append(f"{null_conf} signals with NULL confidence")

        result["decision_engine"] = {
            "stage": 5,
            "name": "Decision Engine",
            "signals_total": len(recent_sigs),
            "buy": buy_count,
            "hold": hold_count,
            "sell": sell_count,
            "null_iv": null_iv,
            "null_mos": null_mos,
            "null_conf": null_conf,
            "low_conf": low_conf,
            "conf_mean": round(_stats.mean(confs), 3) if confs else None,
            "silent_failures": silent_failures,
            "status": "error" if null_conf > 0 else ("warn" if silent_failures else "ok"),
        }

    # ── Stage 6: Paper Execution ──────────────────────────────────────────────
    try:
        from paper.account import init_paper_db, PaperPosition, PaperTrade
        from paper.executor import PAPER_MODEL_CONFIGS
        paper_summary = []
        for model_name, cfg in PAPER_MODEL_CONFIGS.items():
            try:
                _, PSession = init_paper_db(cfg["db"])
                with PSession() as ps:
                    open_pos  = ps.query(PaperPosition).count()
                    last_trade = (
                        ps.query(PaperTrade)
                        .order_by(PaperTrade.id.desc())
                        .first()
                    )
                    last_ts = last_trade.timestamp.strftime("%Y-%m-%d %H:%M ET") if (last_trade and last_trade.timestamp) else None
                    paper_summary.append({
                        "model": model_name,
                        "open_positions": open_pos,
                        "last_trade": last_ts,
                    })
            except Exception:
                paper_summary.append({"model": model_name, "error": "unavailable"})
        result["paper_execution"] = {
            "stage": 6,
            "name": "Paper Execution",
            "models": paper_summary,
            "status": "ok",
        }
    except Exception as e:
        result["paper_execution"] = {
            "stage": 6,
            "name": "Paper Execution",
            "error": str(e),
            "status": "warn",
        }

    # ── Scheduler status ──────────────────────────────────────────────────────
    try:
        from paper.auto_scheduler import get_scheduler
        sched = get_scheduler()
        result["scheduler"] = sched.status() if sched else {"error": "not started"}
    except Exception:
        result["scheduler"] = {"error": "unavailable"}

    return result


@app.get("/api/pipeline/trace/{ticker}")
def api_pipeline_trace(ticker: str):
    """
    Return the most recent TradeSignal reasoning JSON for a ticker,
    plus all scalar fields from the DB row, for decision tracing.
    """
    ticker = ticker.upper().strip()
    with Session() as s:
        co = s.query(Company).filter_by(ticker=ticker).first()
        if not co:
            return JSONResponse({"error": f"{ticker} not in DB"}, status_code=404)

        sig = (
            s.query(TradeSignal)
            .filter_by(company_id=co.id)
            .order_by(TradeSignal.id.desc())
            .first()
        )
        if not sig:
            return JSONResponse({"error": f"No signals for {ticker}"}, status_code=404)

        reasoning = {}
        if sig.reasoning:
            try:
                reasoning = _json_mod.loads(sig.reasoning)
            except Exception:
                reasoning = {"raw": sig.reasoning}

        # Detect silent failures in this signal
        issues = []
        if sig.intrinsic_value_estimate is None:
            issues.append("intrinsic_value_estimate is NULL — IV not propagated from analysis engine")
        if sig.margin_of_safety is None:
            issues.append("margin_of_safety is NULL — IV or price missing")
        if sig.fud_score is None:
            issues.append("fud_score is NULL — FUD filter did not run or failed")
        if sig.confidence is not None and sig.confidence < 0.02:
            issues.append(f"confidence={sig.confidence:.4f} is near-zero — signal effectively suppressed")
        if sig.roic is None:
            issues.append("roic is NULL — fundamental analysis returned no data")

        return {
            "ticker": ticker,
            "signal_id": sig.id,
            "generated_at": _to_et_str(sig.generated_at),
            "signal": sig.signal,
            "confidence": sig.confidence,
            "roic": sig.roic,
            "wacc_estimate": sig.wacc_estimate,
            "margin_of_safety": sig.margin_of_safety,
            "intrinsic_value_estimate": sig.intrinsic_value_estimate,
            "current_price": sig.current_price,
            "fud_score": sig.fud_score,
            "suggested_position_pct": sig.suggested_position_pct,
            "silent_failures": issues,
            "reasoning": reasoning,
        }


@app.post("/api/pipeline/restore-point")
def api_pipeline_restore_point():
    """
    Snapshot the DB and key JSON data files to backups/<timestamp>/.
    Returns list of files saved.
    """
    import time as _t
    ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = ROOT / "backups" / ts
    backup_dir.mkdir(parents=True, exist_ok=True)

    saved = []
    errors = []

    # DB file
    db_url = config.database.url
    if db_url.startswith("sqlite:///"):
        db_path = Path(db_url.replace("sqlite:///", ""))
        if not db_path.is_absolute():
            db_path = ROOT / db_path
        if db_path.exists():
            dest = backup_dir / db_path.name
            _shutil.copy2(str(db_path), str(dest))
            saved.append(str(dest))
        else:
            errors.append(f"DB not found: {db_path}")

    # Paper trade DBs
    try:
        from paper.executor import PAPER_MODEL_CONFIGS
        for m in PAPER_MODEL_CONFIGS.values():
            p = Path(m.get("db", ""))
            if not p.is_absolute():
                p = ROOT / p
            if p.exists():
                dest = backup_dir / p.name
                _shutil.copy2(str(p), str(dest))
                saved.append(str(dest))
    except Exception as e:
        errors.append(f"Paper DB copy error: {e}")

    # Key JSON config / state files
    json_files = [
        ROOT / "data" / "stagegate.json",
        ROOT / "data" / "signal_syntheses.json",
        ROOT / "data" / "thesis_analysis.json",
        ROOT / "data" / "thesis_log.json",
        ROOT / "data" / "ai_exits.json",
        ROOT / "config.py",
    ]
    for jf in json_files:
        if jf.exists():
            try:
                dest = backup_dir / jf.name
                _shutil.copy2(str(jf), str(dest))
                saved.append(str(dest))
            except Exception as e:
                errors.append(f"File copy error {jf.name}: {e}")

    _logger.info(f"[PIPELINE] Restore point created: {backup_dir} ({len(saved)} files)")
    return {
        "snapshot": ts,
        "path": str(backup_dir),
        "files_saved": len(saved),
        "saved": saved,
        "errors": errors,
    }


@app.get("/api/pipeline/restore-points")
def api_pipeline_restore_points():
    """List all available restore point snapshots."""
    backups_dir = ROOT / "backups"
    if not backups_dir.exists():
        return []
    points = []
    for d in sorted(backups_dir.iterdir(), reverse=True):
        if d.is_dir():
            files = list(d.iterdir())
            points.append({
                "snapshot": d.name,
                "path": str(d),
                "file_count": len(files),
                "files": [f.name for f in files],
            })
    return points


# ── Pipeline HTML page ────────────────────────────────────────────────────────

_PIPELINE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pipeline Health &mdash; NWO</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0d1117; color: #e6edf3; font-family: 'Segoe UI', system-ui, sans-serif; font-size: 14px; }
a { color: inherit; text-decoration: none; }

header { background: #161b22; padding: 12px 24px; border-bottom: 1px solid #30363d;
         display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
         position: sticky; top: 0; z-index: 100; }
h1 { font-size: 18px; font-weight: 700; color: #e6edf3; }

{NAV_CSS}

.container { max-width: 1280px; margin: 0 auto; padding: 20px 16px; }

/* ── Stage grid ────────────────────────────────────────────────────────── */
.stage-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; margin-bottom: 24px; }
.stage-card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; position: relative; }
.stage-card.ok     { border-left: 4px solid #3fb950; }
.stage-card.warn   { border-left: 4px solid #d29922; }
.stage-card.error  { border-left: 4px solid #f85149; }
.stage-card.loading{ border-left: 4px solid #30363d; }
.stage-num  { font-size: 10px; color: #8b949e; letter-spacing: 1px; text-transform: uppercase; margin-bottom: 4px; }
.stage-name { font-size: 15px; font-weight: 700; margin-bottom: 12px; }
.stage-badge { position: absolute; top: 14px; right: 14px; font-size: 11px; font-weight: 700;
               padding: 2px 8px; border-radius: 10px; }
.badge-ok    { background: rgba(63,185,80,0.15);  color: #3fb950; }
.badge-warn  { background: rgba(210,153,34,0.15); color: #d29922; }
.badge-error { background: rgba(248,81,73,0.15);  color: #f85149; }
.badge-loading { background: rgba(139,148,158,0.15); color: #8b949e; }

.kv-row { display: flex; justify-content: space-between; padding: 3px 0;
          border-bottom: 1px solid #21262d; font-size: 12px; }
.kv-row:last-child { border-bottom: none; }
.kv-key   { color: #8b949e; }
.kv-val   { color: #e6edf3; font-weight: 600; max-width: 60%; text-align: right; word-break: break-all; }
.kv-val.ok    { color: #3fb950; }
.kv-val.warn  { color: #d29922; }
.kv-val.err   { color: #f85149; }

.issue-list { margin-top: 10px; }
.issue-item { background: rgba(248,81,73,0.08); border: 1px solid rgba(248,81,73,0.2);
              border-radius: 4px; padding: 4px 8px; font-size: 11px; color: #f85149;
              margin-bottom: 4px; }
.warn-item  { background: rgba(210,153,34,0.08); border: 1px solid rgba(210,153,34,0.2);
              border-radius: 4px; padding: 4px 8px; font-size: 11px; color: #d29922;
              margin-bottom: 4px; }

/* ── Scheduler card ────────────────────────────────────────────────────── */
.sched-card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; margin-bottom: 24px; }
.sched-title { font-size: 14px; font-weight: 700; margin-bottom: 12px; }
.sched-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 8px; }
.sched-kv { background: #0d1117; border-radius: 6px; padding: 8px 12px; }
.sched-kv .k { font-size: 10px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }
.sched-kv .v { font-size: 14px; font-weight: 700; margin-top: 2px; }

/* ── Trace section ─────────────────────────────────────────────────────── */
.trace-section { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; margin-bottom: 24px; }
.trace-title  { font-size: 14px; font-weight: 700; margin-bottom: 12px; }
.trace-search { display: flex; gap: 8px; margin-bottom: 16px; }
.trace-input  { flex: 1; background: #0d1117; border: 1px solid #30363d; border-radius: 6px;
                padding: 7px 12px; color: #e6edf3; font-size: 13px; }
.trace-input:focus { outline: none; border-color: #58a6ff; }
.trace-btn    { padding: 7px 16px; border-radius: 6px; background: #1f6feb;
                border: none; color: #fff; font-size: 13px; cursor: pointer; }
.trace-btn:hover { background: #388bfd; }

.trace-result { display: none; }
.trace-header { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 12px; align-items: center; }
.sig-badge    { padding: 4px 12px; border-radius: 6px; font-size: 13px; font-weight: 700; }
.sig-BUY      { background: rgba(63,185,80,0.2);  color: #3fb950; }
.sig-SELL     { background: rgba(248,81,73,0.2);  color: #f85149; }
.sig-HOLD     { background: rgba(139,148,158,0.2);color: #8b949e; }
.trace-grid   { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 8px; margin-bottom: 12px; }
.trace-metric { background: #0d1117; border-radius: 6px; padding: 10px; }
.trace-metric .tm-k { font-size: 10px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }
.trace-metric .tm-v { font-size: 16px; font-weight: 700; margin-top: 4px; }

.reasoning-panel { background: #0d1117; border: 1px solid #21262d; border-radius: 6px;
                   padding: 12px; font-family: monospace; font-size: 11px; color: #8b949e;
                   max-height: 400px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; }
.toggle-reasoning { background: none; border: 1px solid #30363d; color: #8b949e; font-size: 11px;
                    padding: 3px 10px; border-radius: 4px; cursor: pointer; margin-bottom: 8px; }
.toggle-reasoning:hover { background: #21262d; }

/* ── Restore points ────────────────────────────────────────────────────── */
.restore-section { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
                   padding: 16px; margin-bottom: 24px; }
.restore-title   { font-size: 14px; font-weight: 700; margin-bottom: 12px; }
.restore-btn     { padding: 8px 18px; border-radius: 6px; background: #388bfd;
                   border: none; color: #fff; font-size: 13px; cursor: pointer; font-weight: 600; }
.restore-btn:hover { background: #58a6ff; }
.restore-btn:disabled { background: #21262d; color: #8b949e; cursor: not-allowed; }
.restore-list    { margin-top: 12px; }
.restore-row     { display: flex; justify-content: space-between; align-items: center;
                   padding: 6px 10px; border-radius: 6px; background: #0d1117;
                   margin-bottom: 6px; font-size: 12px; }
.restore-row .r-ts   { color: #e6edf3; font-weight: 600; }
.restore-row .r-info { color: #8b949e; }
.restore-msg { padding: 8px 12px; border-radius: 6px; font-size: 12px; margin-top: 10px; }
.restore-ok  { background: rgba(63,185,80,0.12); color: #3fb950; }
.restore-err { background: rgba(248,81,73,0.12); color: #f85149; }

.refresh-row  { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.refresh-btn  { padding: 6px 14px; border-radius: 6px; background: #21262d;
                border: 1px solid #30363d; color: #8b949e; font-size: 12px; cursor: pointer; }
.refresh-btn:hover { background: #2d333b; color: #e6edf3; }
.last-updated { font-size: 11px; color: #8b949e; }
</style>
</head>
<body>
<header>
  <h1>&#128301; Pipeline Health</h1>
  {NAV}
</header>
{TAPE_HTML}
{PAGE_INFO}
<div class="container">

  <div class="refresh-row">
    <span class="last-updated" id="last-updated">Loading…</span>
    <button class="refresh-btn" onclick="loadAll()">&#8635; Refresh</button>
  </div>

  <!-- Scheduler card -->
  <div class="sched-card" id="sched-card">
    <div class="sched-title">&#9200; Scheduler Status</div>
    <div class="sched-grid" id="sched-grid"><span style="color:#8b949e;font-size:12px">Loading…</span></div>
  </div>

  <!-- Stage cards -->
  <div class="stage-grid" id="stage-grid">
    <div class="stage-card loading"><div class="stage-name">Loading pipeline health…</div></div>
  </div>

  <!-- Decision Trace -->
  <div class="trace-section">
    <div class="trace-title">&#128270; Decision Trace — Per-Ticker Signal Inspector</div>
    <div class="trace-search">
      <input class="trace-input" id="trace-ticker" placeholder="Enter ticker (e.g. AAPL)" type="text"
             onkeydown="if(event.key==='Enter') loadTrace()">
      <button class="trace-btn" onclick="loadTrace()">Trace</button>
    </div>
    <div class="trace-result" id="trace-result"></div>
  </div>

  <!-- Restore Points -->
  <div class="restore-section">
    <div class="restore-title">&#128190; Restore Points</div>
    <p style="font-size:12px;color:#8b949e;margin-bottom:12px">
      Snapshots copy the main DB, paper trade DBs, and key JSON files to <code>backups/&lt;timestamp&gt;/</code>.
      Use to roll back after a bad deployment or schema change.
    </p>
    <button class="restore-btn" id="restore-btn" onclick="createRestore()">&#128190; Create Restore Point Now</button>
    <div id="restore-msg"></div>
    <div class="restore-list" id="restore-list"></div>
  </div>

</div>
<script>
var _health = null;

function statusClass(s) {
  if (s === 'ok')    return 'ok';
  if (s === 'error') return 'error';
  return 'warn';
}
function badgeClass(s) {
  if (s === 'ok')    return 'badge-ok';
  if (s === 'error') return 'badge-error';
  if (s === 'loading') return 'badge-loading';
  return 'badge-warn';
}
function badgeLabel(s) {
  if (s === 'ok')    return '&#10003; Healthy';
  if (s === 'error') return '&#9888; Error';
  if (s === 'loading') return '&#9675; Loading';
  return '&#9888; Warning';
}

function kv(key, val, cls) {
  var vc = cls ? ' class="kv-val ' + cls + '"' : ' class="kv-val"';
  return '<div class="kv-row"><span class="kv-key">' + key + '</span><span' + vc + '>' + (val !== null && val !== undefined ? val : '<span style="color:#8b949e">—</span>') + '</span></div>';
}

function renderIngestion(d) {
  var html = kv('Companies in DB', d.companies)
    + kv('Price rows total', d.price_rows ? d.price_rows.toLocaleString() : '—')
    + kv('Watchlist size', d.watchlist_count)
    + kv('Missing prices', d.missing_prices.length ? d.missing_prices.join(', ') : '0', d.missing_prices.length ? 'warn' : 'ok')
    + kv('Stale data (>3d)', d.stale_prices.length ? d.stale_prices.map(function(x){return x.ticker+'('+x.age_days+'d)';}).join(', ') : '0', d.stale_prices.length ? 'warn' : 'ok');
  return html;
}

function renderFundamentals(d) {
  var html = kv('Tickers covered', d.covered)
    + kv('Missing fundamentals', d.missing_fundamentals.length ? d.missing_fundamentals.join(', ') : '0', d.missing_fundamentals.length ? 'warn' : 'ok')
    + kv('Missing owner earnings', d.missing_owner_earnings.length ? d.missing_owner_earnings.join(', ') : '0', d.missing_owner_earnings.length ? 'warn' : 'ok');
  return html;
}

function renderAggregation(d) {
  var html = kv('Tickers with signals', d.tickers_in_db)
    + kv('Last run', d.last_run || '—')
    + kv('Not seen last run', d.missing_from_last_run.length ? d.missing_from_last_run.join(', ') : '0', d.missing_from_last_run.length ? 'warn' : 'ok');
  return html;
}

function renderFUD(d) {
  var html = kv('Signals checked', d.signals_checked)
    + kv('Null fud_score', d.fud_null, d.fud_null > 0 ? 'warn' : 'ok')
    + kv('Blocked (fud < 0.3)', d.fud_blocked)
    + kv('Mean fud_score', d.fud_mean !== null ? d.fud_mean : '—');
  return html;
}

function renderDecision(d) {
  var html = kv('Signals (recent)', d.signals_total)
    + kv('BUY / HOLD / SELL', d.buy + ' / ' + d.hold + ' / ' + d.sell)
    + kv('Null intrinsic_value', d.null_iv, d.null_iv > 0 ? 'err' : 'ok')
    + kv('Null margin_of_safety', d.null_mos, d.null_mos > 0 ? 'warn' : 'ok')
    + kv('Near-zero confidence', d.low_conf, d.low_conf > 5 ? 'warn' : 'ok')
    + kv('Mean confidence', d.conf_mean !== null ? d.conf_mean : '—');
  if (d.silent_failures && d.silent_failures.length) {
    html += '<div class="issue-list">';
    d.silent_failures.forEach(function(f) { html += '<div class="issue-item">&#9888; ' + f + '</div>'; });
    html += '</div>';
  }
  return html;
}

function renderPaper(d) {
  if (d.error) return kv('Status', d.error, 'warn');
  var html = '';
  (d.models || []).forEach(function(m) {
    if (m.error) { html += kv(m.model, m.error, 'warn'); return; }
    html += kv(m.model + ' open', m.open_positions + ' positions')
          + kv(m.model + ' last trade', m.last_trade || '—');
  });
  return html;
}

var _renderers = {
  ingestion:       renderIngestion,
  fundamentals:    renderFundamentals,
  aggregation:     renderAggregation,
  fud_filter:      renderFUD,
  decision_engine: renderDecision,
  paper_execution: renderPaper,
};

function renderStages(data) {
  var order = ['ingestion','fundamentals','aggregation','fud_filter','decision_engine','paper_execution'];
  var html = '';
  order.forEach(function(key) {
    var d = data[key];
    if (!d) return;
    var sc = statusClass(d.status);
    var html_inner = (_renderers[key] || function(){ return ''; })(d);
    html += '<div class="stage-card ' + sc + '">'
          + '<div class="stage-num">Stage ' + d.stage + '</div>'
          + '<div class="stage-name">' + d.name + '</div>'
          + '<span class="stage-badge ' + badgeClass(d.status) + '">' + badgeLabel(d.status) + '</span>'
          + html_inner
          + '</div>';
  });
  document.getElementById('stage-grid').innerHTML = html;
}

function renderScheduler(sched) {
  if (!sched || sched.error) {
    document.getElementById('sched-grid').innerHTML = '<span style="color:#f85149;font-size:12px">' + (sched ? sched.error : 'Unavailable') + '</span>';
    return;
  }
  var items = [
    ['Market Hours',    sched.market_hours ? '<span style="color:#3fb950">Open</span>' : '<span style="color:#8b949e">Closed</span>'],
    ['Running',         sched.running ? '<span style="color:#d29922">Yes</span>' : 'No'],
    ['Paused',          sched.paused  ? '<span style="color:#d29922">Yes</span>' : 'No'],
    ['Last Cycle',      sched.last_cycle  || '—'],
    ['Next Cycle',      sched.next_cycle  || '—'],
    ['Cycle Count',     sched.cycle_count],
    ['Stop Exits',      sched.stop_exits],
    ['Engines Ready',   sched.engines_ready ? '<span style="color:#3fb950">Yes</span>' : '<span style="color:#f85149">No</span>'],
  ];
  var html = items.map(function(i) {
    return '<div class="sched-kv"><div class="k">' + i[0] + '</div><div class="v">' + i[1] + '</div></div>';
  }).join('');
  document.getElementById('sched-grid').innerHTML = html;
}

async function loadAll() {
  document.getElementById('last-updated').textContent = 'Refreshing…';
  try {
    var data = await fetch('/api/pipeline/health').then(function(r){ return r.json(); });
    _health = data;
    renderStages(data);
    renderScheduler(data.scheduler);
    document.getElementById('last-updated').textContent = 'Last updated: ' + new Date().toLocaleTimeString();
  } catch(e) {
    document.getElementById('stage-grid').innerHTML = '<div class="stage-card error"><div class="stage-name">Failed to load health data</div><div style="font-size:11px;color:#f85149">' + e.message + '</div></div>';
  }
  loadRestorePoints();
}

// ── Decision Trace ────────────────────────────────────────────────────────────

async function loadTrace() {
  var ticker = document.getElementById('trace-ticker').value.trim().toUpperCase();
  if (!ticker) return;
  var el = document.getElementById('trace-result');
  el.style.display = 'block';
  el.innerHTML = '<span style="color:#8b949e;font-size:12px">Loading…</span>';
  try {
    var d = await fetch('/api/pipeline/trace/' + encodeURIComponent(ticker)).then(function(r){ return r.json(); });
    if (d.error) { el.innerHTML = '<div class="issue-item">' + d.error + '</div>'; return; }

    var sigClass = 'sig-' + (d.signal || 'HOLD');
    var html = '<div class="trace-header">'
      + '<span style="font-size:16px;font-weight:700">' + d.ticker + '</span>'
      + '<span class="sig-badge ' + sigClass + '">' + (d.signal || '—') + '</span>'
      + '<span style="font-size:11px;color:#8b949e">' + (d.generated_at || '') + '</span>'
      + '</div>';

    // Issues
    if (d.silent_failures && d.silent_failures.length) {
      html += '<div class="issue-list">';
      d.silent_failures.forEach(function(f) { html += '<div class="issue-item">&#9888; ' + f + '</div>'; });
      html += '</div>';
    } else {
      html += '<div style="margin-bottom:8px"><span class="warn-item">&#10003; No silent failures detected</span></div>';
    }

    // Metrics grid
    function fmtNum(v, digits) {
      if (v === null || v === undefined) return '<span style="color:#f85149">NULL</span>';
      return typeof v === 'number' ? v.toFixed(digits !== undefined ? digits : 4) : v;
    }
    html += '<div class="trace-grid">'
      + '<div class="trace-metric"><div class="tm-k">Confidence</div><div class="tm-v">' + fmtNum(d.confidence, 4) + '</div></div>'
      + '<div class="trace-metric"><div class="tm-k">ROIC</div><div class="tm-v">' + fmtNum(d.roic, 3) + '</div></div>'
      + '<div class="trace-metric"><div class="tm-k">WACC Estimate</div><div class="tm-v">' + fmtNum(d.wacc_estimate, 3) + '</div></div>'
      + '<div class="trace-metric"><div class="tm-k">Intrinsic Value</div><div class="tm-v">' + (d.intrinsic_value_estimate !== null && d.intrinsic_value_estimate !== undefined ? '$' + fmtNum(d.intrinsic_value_estimate, 2) : '<span style="color:#f85149">NULL</span>') + '</div></div>'
      + '<div class="trace-metric"><div class="tm-k">Current Price</div><div class="tm-v">' + (d.current_price ? '$' + fmtNum(d.current_price, 2) : '—') + '</div></div>'
      + '<div class="trace-metric"><div class="tm-k">Margin of Safety</div><div class="tm-v">' + (d.margin_of_safety !== null && d.margin_of_safety !== undefined ? fmtNum(d.margin_of_safety * 100, 1) + '%' : '<span style="color:#f85149">NULL</span>') + '</div></div>'
      + '<div class="trace-metric"><div class="tm-k">FUD Score</div><div class="tm-v">' + fmtNum(d.fud_score, 3) + '</div></div>'
      + '<div class="trace-metric"><div class="tm-k">Position %</div><div class="tm-v">' + (d.suggested_position_pct !== null && d.suggested_position_pct !== undefined ? fmtNum(d.suggested_position_pct * 100, 1) + '%' : '—') + '</div></div>'
      + '</div>';

    // Reasoning toggle
    if (d.reasoning && Object.keys(d.reasoning).length > 0) {
      html += '<button class="toggle-reasoning" onclick="(function(b){var p=b.nextElementSibling;p.style.display=p.style.display===\'none\'?\'block\':\'none\';})(this)">&#128270; Show Reasoning JSON</button>';
      html += '<div class="reasoning-panel" style="display:none">' + JSON.stringify(d.reasoning, null, 2) + '</div>';
    }

    el.innerHTML = html;
  } catch(e) {
    el.innerHTML = '<div class="issue-item">Error: ' + e.message + '</div>';
  }
}

// ── Restore Points ────────────────────────────────────────────────────────────

async function createRestore() {
  var btn = document.getElementById('restore-btn');
  var msg = document.getElementById('restore-msg');
  btn.disabled = true;
  btn.textContent = 'Creating snapshot…';
  msg.className = 'restore-msg';
  msg.textContent = '';
  try {
    var d = await fetch('/api/pipeline/restore-point', {method:'POST'}).then(function(r){return r.json();});
    msg.className = 'restore-msg restore-ok';
    msg.textContent = '&#10003; Restore point created: ' + d.snapshot + ' (' + d.files_saved + ' files saved)';
    if (d.errors && d.errors.length) {
      msg.textContent += ' | Errors: ' + d.errors.join('; ');
    }
    loadRestorePoints();
  } catch(e) {
    msg.className = 'restore-msg restore-err';
    msg.textContent = 'Error: ' + e.message;
  }
  btn.disabled = false;
  btn.textContent = '&#128190; Create Restore Point Now';
}

async function loadRestorePoints() {
  try {
    var points = await fetch('/api/pipeline/restore-points').then(function(r){return r.json();});
    var el = document.getElementById('restore-list');
    if (!points.length) { el.innerHTML = '<div style="font-size:12px;color:#8b949e">No restore points yet.</div>'; return; }
    el.innerHTML = points.slice(0, 10).map(function(p) {
      var ts = p.snapshot;
      var fmt = ts.slice(0,4)+'-'+ts.slice(4,6)+'-'+ts.slice(6,8)+' '+ts.slice(9,11)+':'+ts.slice(11,13)+':'+ts.slice(13,15);
      return '<div class="restore-row">'
        + '<span class="r-ts">' + fmt + '</span>'
        + '<span class="r-info">' + p.file_count + ' files &middot; ' + p.files.join(', ') + '</span>'
        + '</div>';
    }).join('');
  } catch(e) { if(window._nwoErr)_nwoErr(e);else console.error('[NWO]',e); }
}

loadAll();
setInterval(loadAll, 60000);
</script>
</body>
</html>"""

_PIPELINE_HTML = _PIPELINE_HTML.replace("{NAV_CSS}", _NAV_CSS)
_PIPELINE_HTML = _PIPELINE_HTML.replace("{NAV}", _nav_html("pipeline"))
_PIPELINE_HTML = _PIPELINE_HTML.replace("{TAPE_HTML}", _NAV_TAPE_HTML)
_PIPELINE_HTML = _PIPELINE_HTML.replace("{PAGE_INFO}", _page_info_html("pipeline"))

if "pipeline" not in _PAGE_INFO:
    _PAGE_INFO["pipeline"] = (
        '<b>Pipeline Health</b> — Real-time status of all 6 pipeline stages: '
        'Data Ingestion, Fundamental Analysis, Signal Aggregation, FUD Filter, Decision Engine, and Paper Execution. '
        'Use <b>Decision Trace</b> to inspect every scalar field and silent failure for any ticker\'s last signal. '
        'Use <b>Restore Points</b> to snapshot the DB and config files before risky changes.'
    )


@app.get("/pipeline", response_class=HTMLResponse)
def page_pipeline():
    return HTMLResponse(_PIPELINE_HTML)
