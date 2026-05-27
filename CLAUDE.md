# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Canonical project location

The live production system is at **`C:\Users\neo_w\new_world_order\`** — all development happens there.

The Downloads folder contains older snapshots (`schwab_trader/`, `nwo_updates_full/`) kept for reference only.

## Common commands

All commands run from inside `C:\Users\neo_w\new_world_order\`:

```bash
# Install dependencies
pip install -r requirements.txt

# Schwab OAuth — run initially or when 400 refresh_token errors appear
python get_token.py
python refresh_token.py   # silent token refresh (no browser)

# Run the full trading pipeline (ingest + analysis + scheduler)
python main.py

# Start the web dashboard (http://localhost:8765)
python -m monitor.start_monitor
python -m monitor.start_monitor --port 9000   # custom port

# Diagnose why signals are or aren't firing
python diagnose.py                # DB health + last signal per ticker
python diagnose.py --gates TSLA   # Full gate breakdown for one ticker
python diagnose.py --gates all    # All watchlist tickers

# Refresh CA bundle (Norton AV SSL inspection — see "SSL on Windows" below)
powershell -ExecutionPolicy Bypass -File scripts\refresh_ca_bundle.ps1

# Run tests
pytest
pytest tests/test_specific.py
```

## Architecture: 6-layer pipeline

Data flows through layers in sequence; each layer writes to SQLite and the next reads from it.

| Layer | Module | Role | Output type |
|-------|--------|------|-------------|
| L1 | `pipeline/ingestion.py` | EDGAR fundamentals (06:00 ET daily) + Schwab price data (every 5 min) | DB rows |
| L2 | `analysis/engine.py` (`FirstPrinciplesEngine`) | WACC, moat detection, DCF, intrinsic value | `AnalysisReport` |
| Signals | `signals/` | FFT cycles, Fibonacci, VWAP, volume profile, insider flow, momentum, VIX regime, entanglement | signal objects |
| Aggregator | `signals/aggregator.py` | Combines all signals into a single score | `AggregatedSignal` |
| L3 | `fud/filter_engine.py` | News quality gate — blocks trades below `min_fud_score` (default 0.60) | `Layer3Result` |
| L4 | `decision/engine.py` | Physics-based GO/NO-GO (Reynolds, Kalman, Quantum, Ensemble Kelly) | trade decision |
| L5 | `risk/manager.py` | Position sizing, hard limits (5% max per ticker, 3% daily loss halt) | risk assessment |
| L6 | `broker/executor.py` (`SchwabExecutor`) | Order execution; `dry_run=True` by default | order or dry-run log |

`pipeline/analysis_pipeline.py` orchestrates L2–L5 and is called after every price ingestion cycle.

For per-subsystem details, see:
- `signals/CLAUDE.md` — aggregator weights, individual signal modules, the new entanglement engine
- `decision/CLAUDE.md` — the seven gates, physics models, threshold tuning

### L4 decision gates (all must pass for GO)

1. FUD filter `proceed_to_execution` is True
2. Reynolds turbulence allows entry (AI Watch: override if RVOL ≥ 1.5 + momentum confirmed)
3. Quantum state certainty ≥ 60% *(raised from 0.45 — 45% was near coin flip)*
4. Ensemble Kelly P(bull) ≥ break-even + 5%, dynamically derived from R/R ratio *(replaces fixed 0.55 floor)*
5. Risk/reward ratio ≥ 1.5:1
6. Kalman innovation ≤ 2.5σ (AI Watch breakout: relaxed to 5.0σ)
7. Adjusted signal is `"buy"` or `"strong_buy"`

`diagnose.py --gates <ticker>` shows per-gate pass/fail with reasons — use this before editing thresholds.

### AI Watch override (TSLA and `config.ai_watch_tickers`)

Runs every 1 minute. When momentum ≥ threshold **and** RVOL ≥ 2×: fundamentals score is floored at 0 (prevents premium valuation from blocking confirmed breakout), Reynolds extreme-turbulence block is lifted, and the Kalman innovation cap relaxes from 2.5σ to 5.0σ.

## Claude upstream loop (the predictive layer)

The system used to use Claude only as a downstream narrator (theses generated *after* signals computed). It is now also wired in as an upstream contributor that validates inputs and judges live events.

### Pre-pipeline data validator (`analysis/data_validator.py`)

Sits at the front of L2's `analyze_ticker()`. Catches physically impossible inputs before WACC/moat/IV run — solves the historical bugs where CRM showed 265% gross margins (EDGAR fiscal-year mismatch) and MSFT showed $1,506 intrinsic value (NULL market_cap → WACC collapse).

Two layers: (1) deterministic rules always run; (2) Claude Haiku does semantic anomaly detection when deterministic flags any issue. Cached by input hash. Graceful fallback if Claude is unreachable.

### Live Sentinel (`monitor/sentinel.py`, `/sentinel` tab)

Background thread polling the entanglement engine event queue every 30s during market hours. When new decoherence events accumulate, ships them + live prices + current signals to Claude Haiku. Claude returns one structured alert: `{level, ticker, action, reasoning, time_horizon, confidence}`. Alerts stream to the dashboard. Daily budget tracking; typically $0.05–$0.30/day.

Manual trigger: `POST /api/sentinel/trigger` (useful for testing without waiting for live events).

### Cross-ticker entanglement engine (`signals/entanglement.py`)

Detects "decoherence" — pairs of watchlist tickers that used to be correlated (ρ > 0.50) and suddenly break apart (|Δρ| > 2.5σ across all pairs). The leading ticker has new information the lagging one hasn't propagated yet — a predictive lead-lag signal. Math: 30-day rolling Pearson correlation matrix on daily closes; z-score of the per-pair delta drives the threshold.

Runs every 60s during market hours. Output persists to `data/entanglement_events.json`. The engine is intentionally selective — most days it produces zero events. That is correct behavior: it fires only when something genuinely abnormal happens.

## SSL on Windows (Norton AV SSL inspection)

This developer machine runs Norton Antivirus, which intercepts HTTPS for malware scanning and re-signs traffic with `CN=Norton Web/Mail Shield Root`. PowerShell/Chrome trust this root (Norton installed it in the Windows cert store); Python's certifi bundle doesn't — and to compound the problem, Norton's root cert violates RFC 5280 (BasicConstraints not marked critical), which OpenSSL 3.x rejects strictly.

**Fix:** `utils/ssl_context.py` builds a custom SSL context using `data/ca_bundle_with_norton.pem` (certifi + Norton root) and disables `VERIFY_X509_STRICT` to tolerate the non-spec cert. All Anthropic SDK callers (`monitor/sentinel.py`, `analysis/data_validator.py`, `monitor/morning_brief.py`) use `utils.ssl_context.make_httpx_client()`.

**Rebuild the bundle** when Norton rotates its inspection root (or any other AV is installed):
```bash
powershell -ExecutionPolicy Bypass -File scripts\refresh_ca_bundle.ps1
```
The script auto-detects Norton, Symantec, Zscaler, ESET, Kaspersky, Bitdefender, Avast, McAfee.

## Web dashboard

Started with `python -m monitor.start_monitor` — binds to all interfaces on port 8765. The `monitor/dashboard.py` FastAPI app mounts several sub-apps and routers:

| URL | Source | Description |
|-----|--------|-------------|
| `/` | `monitor/dashboard.py` | Main signal monitor — live signals, trade log, diagnostics; auto-refresh 15s |
| `/signals` | `monitor/signals_dashboard.py` | Composite signal view with RVOL, MACD |
| `/wheel` | `monitor/wheel.py` (`wheel_router`) | Wheel strategy scanner and position tracker |
| `/morning-brief` | `monitor/morning_brief.py` | Markets, futures, crypto summary (Gemini Flash primary, Claude Haiku fallback) |
| `/i-tool` | `monitor/itool.py` | S&P 500 scanner |
| `/paper` | `paper/` | Paper trading — 4-model comparison dashboard |
| `/alfred` | `monitor/alfred.py` (`alfred_router`) | CME futures forecast + weekly backtest |
| `/strat` | `monitor/strat.py` (`strat_router`) | Historical threshold breach analysis (12 symbols, Feb 2020–Feb 2026) |
| `/sentinel` | `monitor/sentinel.py` (`sentinel_router`) | Claude live alerts driven by entanglement events |

All pages share a dark GitHub-style theme (`#0d1117` background) and a live ticker tape that reads from `/api/signals`.

Key dashboard API endpoints:
- `GET /api/signals` — latest signal per watchlist ticker (JSON)
- `GET /api/trades` — last 20 trade log entries
- `GET /api/diagnostics` — per-ticker data health
- `POST /api/pause` — toggle system pause flag (`data/paused.flag`)
- `POST /api/backfill` — trigger price/fundamentals backfill in background
- `GET /api/sentinel/status` — entanglement engine state + Claude budget
- `POST /api/sentinel/trigger` — force a Claude judgment now (testing)

### Adding a new dashboard tab

1. Create `monitor/<tab>.py` with a FastAPI `APIRouter`
2. Import + mount in `monitor/dashboard.py` (~line 36–42 alongside `wheel_router`, `alfred_router`, etc.)
3. Add nav link in the `<div class="header-nav">` block (~line 1282)
4. Match the existing theme (dark `#0d1117`, sub-CLAUDE.md in `monitor/` if you make one)

## Paper trading — 4 models

Each model runs with $100k virtual capital and identical order flow, but with different gate thresholds. Comparison dashboard at `/paper/compare` (refreshes every 30s). Per-model stagegate files (`data/stagegate_<model>.json`) keep them isolated.

| Model | Gate adjustments |
|-------|-----------------|
| **Standard** | All gates at design levels — control group |
| **Relaxed** | All thresholds −25% (e.g. Kelly P(bull) ≥ 37.5%) |
| **Very Relaxed** | All thresholds −50% |
| **Claude** | Pure momentum/trend: 35% SuperTrend, 30% momentum, 5% fundamentals |

## Wheel strategy

The wheel is an options income strategy that cycles: **CSP → (if assigned) SHARES → Covered Call → repeat**.

**Module:** `monitor/wheel.py` — a FastAPI `APIRouter` (`wheel_router`) mounted at `/wheel`.
**Persistence:** positions and scan results survive restarts via `data/wheel_positions.json` and `data/wheel_scan.json`.

### Candidate screener

Scans the top-100 S&P 500 universe (`SP500_UNIVERSE`) for CSP candidates. Filters:
- Option IV (per-leg) ≥ 20% — avoids selling premium in low-vol environments
- VIX proxy < 15 → skip (market-wide low vol gate)
- Target: ~30-delta put, 25–50 DTE, bid ≥ $0.05
- Liquidity gate: bid/ask spread ≤ 15% of mid
- IV rank computed from near-ATM puts only (within 5% of spot) to avoid deep-OTM skew distortion

### Position lifecycle

| Phase | Trigger | Action |
|-------|---------|--------|
| `CSP` | Opened from scanner | Sell cash-secured put; premium collected |
| `SHARES` | "Assign" action | Put exercised; cost basis = strike − premium/share |
| `CC` | "Sell CC" action | Sell covered call against shares; adds to total premium |
| Closed | "Called Away" or "Expire" | P&L calculated; moved to completed cycles |

### Wheel API routes

```
POST /api/wheel/scan          Start background S&P 500 scan
GET  /api/wheel/scan-results  Poll scan progress and results
POST /api/wheel/open          Open a CSP position
POST /api/wheel/assign/{id}   Mark CSP as assigned (shares taken)
POST /api/wheel/sell-cc/{id}  Sell covered call against held shares
POST /api/wheel/close/{id}    Close position (called away or expired)
GET  /api/wheel/positions     All active + completed positions + summary P&L
```

## Key configuration

All settings live in `config.py` as dataclasses, with secrets loaded from `.env`:

```
SCHWAB_API_KEY, SCHWAB_API_SECRET, SCHWAB_REDIRECT_URI, SCHWAB_ACCOUNT_HASH
DATABASE_URL          # defaults to sqlite:///data/schwab_trader.db
EDGAR_USER_AGENT      # required by SEC: "Name email@example.com"
ANTHROPIC_API_KEY     # Claude Haiku — sentinel, validator, morning brief fallback
GOOGLE_API_KEY        # Gemini Flash — morning brief primary
LOG_LEVEL             # defaults to INFO
```

**`config.risk.dry_run` defaults to `True` — no real orders are placed until explicitly set to `False`.**

`config.ai_watch_tickers` (default: `["TSLA"]`) — tickers scanned every 1 minute with the breakout override described above.

## Project layout

```
new_world_order/
├── analysis/              # L2: WACC, moat, DCF, intrinsic value (engine.py)
│   ├── engine.py             # FirstPrinciplesEngine orchestrator
│   ├── data_validator.py     # NEW: Claude pre-pipeline input validator
│   ├── intrinsic_value.py    # DCF on owner earnings
│   ├── moat_detector.py      # 6-proxy moat detection
│   └── wacc.py               # Cost of capital
├── broker/                # L6: Schwab order execution
├── decision/              # L4: Physics-based GO/NO-GO + Kelly sizing
│   ├── engine.py             # DecisionEngine: the 7 gates
│   ├── ensemble_kelly.py     # Bayesian Model Averaging + Kelly
│   ├── reynolds_turbulence.py
│   ├── quantum_kalman.py
│   └── CLAUDE.md             # Subsystem guide
├── edgar/                 # SEC EDGAR fundamentals client
├── fud/                   # L3: News quality / FUD filter
├── monitor/               # Web dashboard + per-tab routers
│   ├── dashboard.py          # Main FastAPI app + signal monitor UI
│   ├── sentinel.py           # NEW: Claude live sentinel + /sentinel tab
│   ├── strat.py              # STRAT historical breach analysis
│   ├── alfred.py             # CME futures forecaster
│   ├── wheel.py              # Options wheel strategy
│   ├── morning_brief.py      # Morning narrative
│   ├── itool.py              # S&P 500 scanner
│   ├── signals_dashboard.py  # Signal monitor sub-app
│   └── start_monitor.py      # uvicorn launcher (kills stale, binds to :8765)
├── paper/                 # 4-model paper trading
├── pipeline/              # L1 ingestion + L2-L5 analysis orchestration
│   ├── ingestion.py          # Schwab prices + EDGAR fundamentals
│   └── analysis_pipeline.py  # Runs L2-L5 per ticker
├── risk/                  # L5: Position sizing + hard limits
├── signals/               # All signal modules (FFT, Fib, VWAP, etc.)
│   ├── aggregator.py         # Composes the composite score
│   ├── entanglement.py       # NEW: cross-ticker decoherence detector
│   ├── momentum.py, fft_cycles.py, fibonacci.py, insider_flow.py, …
│   └── CLAUDE.md             # Subsystem guide
├── utils/                 # NEW: shared helpers
│   └── ssl_context.py        # Norton/AV-aware SSL context
├── models/                # SQLAlchemy ORM
├── scripts/               # One-shot maintenance scripts
│   ├── refresh_ca_bundle.ps1 # Rebuild CA bundle from Windows cert store
│   └── archive/patches/      # Historical one-time patches (already applied)
├── data/                  # SQLite DB, caches, JSON state (gitignored)
├── logs/                  # Rotated logs (gitignored)
├── tokens/                # Schwab OAuth (gitignored)
├── main.py                # Full pipeline + scheduler entrypoint
├── diagnose.py            # CLI diagnostics
├── config.py              # All settings
└── CLAUDE.md              # This file
```

## Data storage

- SQLite at `data/schwab_trader.db` — models in `models/database.py`; schema created by `init_db()` on first run (no Alembic migrations)
- EDGAR filings cached in `data/edgar_cache/`
- OAuth token at `tokens/schwab_token.json`
- Wheel positions at `data/wheel_positions.json`, last scan at `data/wheel_scan.json`
- Stagegate at `data/stagegate.json` + per-model `data/stagegate_<model>.json`
- Entanglement events at `data/entanglement_events.json`; sentinel alerts at `data/sentinel_alerts.json`
- SPY beta returns cached at `data/spy_returns_cache.json` (7-day TTL)
- CA bundle for Norton at `data/ca_bundle_with_norton.pem`
- Validator cache at `data/validator_cache.json`
- Logs rotate at 10 MB, retained 30 days in `logs/`; live dashboard log at `logs/nwo_live.log`
- Pause flag at `data/paused.flag` — presence (not content) halts the trading cycle

## Tech stack

Python 3.x · SQLAlchemy 2 · pandas/numpy/scipy · schwab-py · APScheduler · FastAPI + uvicorn · loguru · anthropic SDK · yfinance · pytest/pytest-asyncio
