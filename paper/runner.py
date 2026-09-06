"""
paper/runner.py — Standalone paper trading runner.

Runs the full 6-layer AI pipeline on the same watchlist / schedule as main.py
but routes all trades through PaperExecutor against a fake $100k account.

Usage:
    python -m paper.runner

Runs every 5 minutes Mon-Fri 9-4pm ET, same schedule as the live system.
Can run alongside main.py (uses a separate DB and port-less — no web server).
"""

import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PAUSE_FLAG = Path("data/paused.flag")

from loguru import logger

from config import config
from models.database import init_db, Company, PriceHistory

from analysis.engine import FirstPrinciplesEngine
from signals.fft_cycles import FFTCycleDetector
from signals.fibonacci import FibonacciAnalyzer
from signals.insider_flow import InsiderFlowAnalyzer
from signals.market_microstructure import VWAPCalculator, VolumeProfileAnalyzer, VIXRegimeDetector
from signals.aggregator import SignalAggregator, ClaudeMomentumAggregator
from signals.momentum import MomentumAnalyzer
from signals.supertrend import SuperTrendAnalyzer
from signals.three_green_arrows import ThreeGreenArrowsAnalyzer
from fud.filter_engine import FUDFilterEngine
from decision.engine import DecisionEngine
from risk.manager import RiskManager
from broker.market_data import SchwabMarketData

from paper.account import PaperPosition, PaperTrade
from paper.executor import PaperExecutor, PAPER_MODEL_CONFIGS, DEFAULT_STAGE2_TICKERS
from utils.price_data import ohlcv_arrays

# Base buy threshold (must match signals/aggregator.py BUY_THRESHOLD)
_BASE_BUY_THRESHOLD = 0.08

# ── Post-stop re-entry cooldown ───────────────────────────────────────────────
# Over 2026-06-03..09-05, 55-65% of all buys in the relaxed/very_relaxed/claude
# models were re-entries into a ticker that had just stopped out — NVDA was
# bought 6x in very_relaxed (4 stops), PLD 4x in claude (4 stops, zero wins),
# MCD 3x (3 stops, zero wins). The signal flips BUY->HOLD->SELL on consecutive
# 5-minute cycles, so with nothing to damp it the system bought straight back
# into the same chop.
#
# After a STOP exit a ticker is benched for this many days, UNLESS price has
# reclaimed the original entry — a genuine thesis recovery, not a dead-cat
# bounce. Calendar days, not trading days: 7 covers a normal 5-session week.
STOP_REENTRY_COOLDOWN_DAYS = 7

_momentum_analyzer = MomentumAnalyzer()


def _stop_cooldown_block(executor, ticker: str, current_price: float | None):
    """
    Return a human-readable reason if `ticker` is still benched after a stop-out
    in this model's book, or None if a BUY is allowed.

    Reads the model's own paper_trades — no extra state to persist or reset.
    """
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=STOP_REENTRY_COOLDOWN_DAYS)
        with executor.Session() as s:
            last_sell = (
                s.query(PaperTrade)
                .filter(PaperTrade.ticker == ticker, PaperTrade.action == "SELL")
                .order_by(PaperTrade.timestamp.desc())
                .first()
            )
            if not last_sell or "STOP" not in (last_sell.notes or "").upper():
                return None                     # never stopped out, or exited some other way

            ts = last_sell.timestamp
            if ts is None:
                return None
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < cutoff:
                return None                     # cooldown already elapsed

            prior_buy = (
                s.query(PaperTrade)
                .filter(PaperTrade.ticker == ticker,
                        PaperTrade.action == "BUY",
                        PaperTrade.timestamp < last_sell.timestamp)
                .order_by(PaperTrade.timestamp.desc())
                .first()
            )
            entry = prior_buy.price if prior_buy else None

        # Reclaim override — back above the entry the stop invalidated.
        if entry and current_price and current_price > entry:
            return None

        days_left = STOP_REENTRY_COOLDOWN_DAYS - (datetime.now(timezone.utc) - ts).days
        detail = f" (needs > ${entry:.2f} to re-enter early)" if entry else ""
        return (f"stopped out {ts:%Y-%m-%d}, {max(days_left, 0)}d cooldown remaining"
                f"{detail}")
    except Exception as e:
        logger.warning(f"[COOLDOWN] check failed for {ticker}: {e}")
        return None                             # never block a trade on a lookup error


def setup_logging():
    os.makedirs("logs", exist_ok=True)
    logger.remove()
    logger.add(
        sys.stdout, level="INFO", colorize=True,
        format="<green>{time:HH:mm:ss}</green> | <cyan>PAPER</cyan> | <level>{level: <8}</level> | {message}"
    )
    logger.add(
        "logs/paper_trading.log", rotation="10 MB", retention="30 days",
        level="INFO", compression="zip"
    )


def _load_price_data(Session, ticker: str, live_quotes: dict | None = None) -> dict:
    """Load EOD price history from DB and override current_price with live quote if provided.

    Args:
        live_quotes: pre-fetched {ticker: {last_price, ...}} from get_quotes_batch — avoids
                     per-ticker API calls inside the loop. Pass None to skip live override.
    """
    with Session() as session:
        company = session.query(Company).filter_by(ticker=ticker).first()
        if not company:
            return {}
        records = (
            session.query(PriceHistory)
            .filter_by(company_id=company.id)
            .order_by(PriceHistory.date)
            .all()
        )
        if not records:
            return {}
        cik = company.cik

    # Dedupe the doubled daily rows before anything ATR-based reads them.
    _bars   = ohlcv_arrays(records)
    closes  = _bars["closes"]
    highs   = _bars["highs"]
    lows    = _bars["lows"]
    volumes = _bars["volumes"]

    # Inject live intraday bar into all arrays so every indicator uses live data
    if live_quotes is not None:
        q = live_quotes.get(ticker, {})
        lp = q.get("last_price")
        if lp:
            live_price = round(float(lp), 2)
            live_high   = q.get("high_price")  or live_price
            live_low    = q.get("low_price")   or live_price
            live_volume = q.get("volume")      or (volumes[-1] if volumes else 0)
            # Replace last EOD bar with today's live intraday bar
            if closes:  closes[-1]  = live_price
            else:       closes.append(live_price)
            if highs:   highs[-1]   = live_high
            else:       highs.append(live_high)
            if lows:    lows[-1]    = live_low
            else:       lows.append(live_low)
            if volumes: volumes[-1] = live_volume
            else:       volumes.append(live_volume)
        else:
            live_price = None
    else:
        live_price = None

    return {
        "closes": closes, "highs": highs,
        "lows": lows, "volumes": volumes,
        "cik": cik,
        "current_price": live_price if live_price else (closes[-1] if closes else None),
    }


def _fetch_live_quotes(tickers: list) -> dict:
    """Single Schwab batch quote call. Returns {} on failure."""
    try:
        from broker.market_data import SchwabMarketData
        return SchwabMarketData().get_quotes_batch(tickers)
    except Exception:
        return {}


def _append_daily_log(model_name: str, cycle_time: str, decisions: dict, approved_tickers: set, l3_results: dict):
    """Append one JSON line per ticker to data/daily_logs/YYYY-MM-DD.jsonl."""
    import json as _j
    from datetime import datetime
    import pytz
    ET = pytz.timezone("America/New_York")
    today = datetime.now(ET).strftime("%Y-%m-%d")
    log_dir = Path("data/daily_logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{today}.jsonl"
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            for ticker, dec in decisions.items():
                l3 = l3_results.get(ticker)
                composite = getattr(getattr(l3, "incoming_signal", None), "composite_score", None)
                entry = {
                    "time":            cycle_time,
                    "model":           model_name,
                    "ticker":          ticker,
                    "action":          dec.action,
                    "go_no_go":        dec.go_no_go,
                    "traded":          ticker in approved_tickers,
                    "composite_score": round(composite, 4) if composite is not None else None,
                    "blocking_reason": dec.blocking_reason,
                    "gates_passed":    dec.gates_passed,
                    "gates_failed":    dec.gates_failed,
                    "ensemble_prob":   round(dec.ensemble_probability_bull, 3),
                    "rr_ratio":        round(dec.risk_reward_ratio, 2) if dec.risk_reward_ratio else None,
                    "reynolds":        dec.reynolds_regime,
                    "kalman_trend":    dec.kalman_trend,
                    "narrative":       dec.decision_narrative,
                }
                f.write(_j.dumps(entry) + "\n")
    except Exception as e:
        logger.warning(f"[DAILY LOG] Failed to write: {e}")


_SHARED_STAGEGATE = "data/stagegate.json"

def _get_stage2_tickers(stagegate_file: str = "data/stagegate.json") -> list:
    """Return Stage 2 tickers.
    Stage 2 is shared across all models — always read from the standard stagegate.json.
    The per-model file is only used for its own stage3 (open positions)."""
    import json
    from pathlib import Path
    # Stage2 is always sourced from the shared stagegate.json regardless of model
    shared = Path(_SHARED_STAGEGATE)
    if shared.exists():
        try:
            sg = json.loads(shared.read_text(encoding="utf-8"))
            stage2 = sg.get("stage2", [])
            if stage2:
                logger.info(f"Stage Gate: running AI on {len(stage2)} Stage 2 tickers: {stage2}")
                return stage2
        except Exception:
            pass
    logger.info("Stage Gate: stage2 empty — seeding with DEFAULT_STAGE2_TICKERS")
    return list(DEFAULT_STAGE2_TICKERS)


def _get_ai_exit_tickers(stagegate_file: str = "data/stagegate.json") -> list:
    """Return Stage 3 tickers that have the AI exit toggle enabled."""
    import json
    from pathlib import Path
    # Derive model-specific ai_exits file from stagegate_file path
    sg_path = str(stagegate_file)
    if "very_relaxed" in sg_path:
        ai_exits_file = Path("data/ai_exits_very_relaxed.json")
    elif "relaxed" in sg_path:
        ai_exits_file = Path("data/ai_exits_relaxed.json")
    elif "claude" in sg_path:
        ai_exits_file = Path("data/ai_exits_claude.json")
    else:
        ai_exits_file = Path("data/ai_exits.json")
    if not ai_exits_file.exists():
        return []
    try:
        overrides = json.loads(ai_exits_file.read_text(encoding="utf-8"))
        sg_file = Path(stagegate_file)
        if not sg_file.exists():
            return []
        sg = json.loads(sg_file.read_text(encoding="utf-8"))
        stage3 = sg.get("stage3", [])
        return [t for t in stage3 if overrides.get(t, False)]
    except Exception:
        return []


def _seed_model_stagegates():
    """Seed relaxed/very_relaxed stagegate files from standard if they don't exist.
    Only copies stage1/stage2 — stage3 starts empty (each model has its own positions)."""
    import json
    from pathlib import Path
    src = Path("data/stagegate.json")
    if not src.exists():
        return
    try:
        standard = json.loads(src.read_text(encoding="utf-8"))
    except Exception:
        return
    # If the standard stagegate has no stage2 tickers, initialize with defaults
    if not standard.get("stage2"):
        try:
            standard["stage2"] = DEFAULT_STAGE2_TICKERS
            src.write_text(json.dumps(standard, indent=2), encoding="utf-8")
            logger.info(f"[PAPER] Seeded standard stagegate stage2 with {len(DEFAULT_STAGE2_TICKERS)} default tickers")
        except Exception as e:
            logger.warning(f"[PAPER] Could not seed standard stagegate stage2: {e}")
    for model, cfg in PAPER_MODEL_CONFIGS.items():
        if model == "standard":
            continue
        dst = Path(cfg["stagegate"])
        if not dst.exists():
            try:
                seed = {
                    "stage1": standard.get("stage1", []),
                    "stage2": standard.get("stage2", []),
                    "stage3": [],  # each model tracks its own open positions
                }
                dst.write_text(json.dumps(seed, indent=2), encoding="utf-8")
                logger.info(f"[PAPER] Seeded {dst} from standard stagegate (stage3 cleared)")
            except Exception as e:
                logger.warning(f"[PAPER] Could not seed {dst}: {e}")


def _get_vix(market_data: SchwabMarketData) -> float:
    try:
        quote = market_data.get_quote("$VIX")
        if quote and quote.get("last_price"):
            return float(quote["last_price"])
    except Exception:
        pass
    return 20.0


_TOKEN_EXPIRY_WARNED: set = set()  # tracks which expiry dates we've already alerted

def _check_schwab_token_expiry():
    """Warn via Telegram if the Schwab token will expire within 2 days."""
    import json as _json
    from datetime import timedelta, timezone as _tz
    token_path = Path("tokens/schwab_token.json")
    if not token_path.exists():
        return
    try:
        data = _json.loads(token_path.read_text(encoding="utf-8"))
        created_ts = data.get("creation_timestamp")
        if not created_ts:
            return
        created = datetime.fromtimestamp(created_ts, tz=_tz.utc)
        expires = created + timedelta(days=7)
        now = datetime.now(_tz.utc)
        days_left = (expires - now).total_seconds() / 86400
        expiry_date = expires.strftime("%Y-%m-%d")
        if days_left <= 2 and expiry_date not in _TOKEN_EXPIRY_WARNED:
            _TOKEN_EXPIRY_WARNED.add(expiry_date)
            from monitor.telegram_bot import send_alert
            send_alert(
                f"⚠️ <b>Schwab Token Expiring</b>\n"
                f"Your Schwab refresh token expires in <b>{days_left:.1f} days</b> ({expiry_date}).\n"
                f"Run <code>python get_token.py</code> in the NWO folder to re-authenticate."
            )
            logger.warning(f"[AUTH] Schwab token expires in {days_left:.1f} days — Telegram alert sent")
    except Exception as e:
        logger.warning(f"[AUTH] Token expiry check failed: {e}")


def run_paper_cycle(
    Session,
    analysis_engine, fft_detector, fib_analyzer,
    insider_analyzer, vwap_calc, vol_analyzer,
    vix_detector, aggregator, fud_engine,
    risk_manager,
    market_data: SchwabMarketData,
    st_analyzer: SuperTrendAnalyzer = None,
    tga_analyzer: ThreeGreenArrowsAnalyzer = None,
    models: list = None,
):
    """
    Run one paper trading cycle for all configured models.

    Args:
        models: list of dicts, each with keys:
            name           — "standard" | "relaxed" | "very_relaxed"
            executor       — PaperExecutor instance
            decision_engine — DecisionEngine instance
            buy_threshold  — float (e.g. 0.10, 0.075, 0.05)
            stagegate_file — str path to this model's stagegate JSON
    """
    if PAUSE_FLAG.exists():
        logger.info("Paper cycle skipped — system PAUSED")
        return

    _check_schwab_token_expiry()

    if not models:
        logger.warning("run_paper_cycle: no models configured — nothing to do")
        return

    logger.info("─" * 60)
    logger.info(f"Paper trading cycle start — {len(models)} model(s): {[m['name'] for m in models]}")

    # VIX regime is shared across all models
    vix_level  = _get_vix(market_data)
    vix_regime = vix_detector.classify(vix_level)
    logger.info(f"VIX {vix_level:.1f} → {vix_regime.regime} | {vix_regime.action}")

    # Load I-Tool cache (shared)
    _itool_signals: dict = {}
    try:
        import json as _json
        _itool_cache = ROOT / "data" / "itool_scan.json"
        if _itool_cache.exists():
            _itool_data = _json.loads(_itool_cache.read_text(encoding="utf-8"))
            for r in _itool_data.get("results", []):
                if r.get("ticker") and r.get("signal"):
                    _itool_signals[r["ticker"]] = r["signal"]
            logger.info(f"[ITOOL] Loaded {len(_itool_signals)} technical signals from cache")
    except Exception as e:
        logger.warning(f"[ITOOL] Could not load I-Tool cache: {e}")

    # Load TipRanks cache (shared)
    from monitor.tipranks_scanner import get_cached_signal as _tr_get
    from signals.tipranks_signal import TipRanksResult as _TRResult

    # Gate overrides loaded per-model below; keep a shared base for standard
    import json as _json
    _base_gate_overrides = {}
    try:
        _go_file = Path("data/gate_overrides.json")
        if _go_file.exists():
            _base_gate_overrides = _json.loads(_go_file.read_text(encoding="utf-8"))
    except Exception:
        pass

    # Collect union of stage2 tickers across all model stagegates
    _all_sg_tickers = list(dict.fromkeys(
        t for m in models for t in _get_stage2_tickers(m.get("stagegate_file", "data/stagegate.json"))
    ))

    _live_quotes = _fetch_live_quotes(_all_sg_tickers)
    if _live_quotes:
        logger.info(f"[LIVE] Fetched live prices for {len(_live_quotes)} tickers")

    _live_prices = {t: round(float(q["last_price"]), 2)
                    for t, q in _live_quotes.items() if q.get("last_price")}

    # ── Stop-loss / take-profit monitoring ────────────────────────────────────
    # Open positions may hold tickers not in the Stage 2 watchlist, so extend
    # live quotes to cover them before checking stop/TP levels.
    _open_pos_tickers: list = []
    for m in models:
        try:
            with m["executor"].Session() as _s:
                _open_pos_tickers.extend(p.ticker for p in _s.query(PaperPosition).all())
        except Exception:
            pass
    _extra_pos_tickers = list(set(_open_pos_tickers) - set(_all_sg_tickers))
    if _extra_pos_tickers:
        _extra_pos_quotes = _fetch_live_quotes(_extra_pos_tickers)
        _live_quotes.update(_extra_pos_quotes)
        _live_prices.update({t: round(float(q["last_price"]), 2)
                             for t, q in _extra_pos_quotes.items() if q.get("last_price")})

    for m in models:
        ex = m["executor"]
        try:
            with ex.Session() as _s:
                _positions = _s.query(PaperPosition).all()
                _pos_snap  = [(p.ticker, p.stop_loss, p.take_profit_1) for p in _positions]
            for ticker, sl, tp1 in _pos_snap:
                cur_p = _live_prices.get(ticker)
                if not cur_p:
                    continue
                reason = None
                if sl and cur_p <= sl:
                    reason = f"STOP LOSS: ${cur_p:.2f} <= SL ${sl:.2f}"
                # NOTE: no take-profit branch here on purpose. Reaching tp1 is
                # handled by paper/stop_monitor.py, which arms an 8% trailing
                # stop instead of selling so winners can run. This block used to
                # hard-sell at tp1 on the 5-min cycle, which beat the trailing
                # stop to every winner — across 2026-06→09 the paper models
                # logged 30 take-profit exits and zero trailing exits, giving up
                # ~$4.6k. Exits now have a single owner: the stop level.
                if reason:
                    logger.info(f"[{m['name'].upper()}][STOP/TP] {ticker}: {reason} — selling")
                    ex.execute_sell(ticker, cur_p, reason=reason)
        except Exception as e:
            logger.error(f"[{m['name'].upper()}][STOP/TP] monitoring failed: {e}")

    # ── Per-model log header ───────────────────────────────────────────────────
    for m in models:
        try:
            summ = m["executor"].get_account_summary()
            pv   = summ["total_equity"]
            logger.info(
                f"[{m['name'].upper()}] ${pv:,.2f} total "
                f"(cash ${summ['cash']:,.2f} | P&L {summ['total_pnl_pct']:+.2f}%)"
            )
            m["portfolio_value"] = pv
        except Exception as e:
            logger.warning(f"[{m['name'].upper()}] Could not fetch account value: {e}")
            m["portfolio_value"] = None

    # ── Signal computation + per-model pipeline ────────────────────────────────
    # Build per-model layer3 dicts — signals computed once, aggregated per model
    model_l3: dict = {m["name"]: {} for m in models}

    # Union of tickers to analyze (may differ per model if stategates diverge)
    for m in models:
        sg_file  = m.get("stagegate_file", "data/stagegate.json")
        tickers  = _get_stage2_tickers(sg_file)
        bt       = m.get("buy_threshold", _BASE_BUY_THRESHOLD)

        for ticker in tickers:
            try:
                analysis = analysis_engine.analyze_ticker(ticker)
                if not analysis:
                    continue

                price         = _load_price_data(Session, ticker, live_quotes=_live_quotes)
                closes        = price.get("closes", [])
                highs         = price.get("highs", [])
                lows          = price.get("lows", [])
                volumes       = price.get("volumes", [])
                current_price = price.get("current_price") or analysis.current_price
                cik           = price.get("cik") or ""

                fft        = fft_detector.analyze(ticker, closes)              if len(closes) >= 64  else None
                fib        = fib_analyzer.analyze(ticker, highs, lows, closes) if len(closes) >= 30  else None
                insider    = insider_analyzer.score(ticker, cik, current_price) if cik               else None
                vwap       = vwap_calc.compute_daily(ticker, highs, lows, closes, volumes) if len(closes) >= 5  else None
                vol_profile = vol_analyzer.analyze(ticker, highs, lows, closes, volumes)  if len(closes) >= 10 else None
                momentum   = _momentum_analyzer.analyze(ticker, closes, volumes=volumes)  if len(closes) >= 30 else None
                st         = st_analyzer.analyze(ticker, highs, lows, closes, volumes)    if (st_analyzer and len(closes) >= 20) else None
                tga        = tga_analyzer.analyze(ticker, closes, highs, lows, volumes)   if (tga_analyzer and len(closes) >= 35) else None

                _tr_raw = _tr_get(ticker)
                _tr_result = None
                if _tr_raw:
                    _tr_result = _TRResult(
                        ticker=ticker,
                        smart_score=_tr_raw.get("smart_score"),
                        buy_pct=_tr_raw.get("buy_pct"),
                        hold_pct=_tr_raw.get("hold_pct"),
                        sell_pct=_tr_raw.get("sell_pct"),
                        price_target_mean=_tr_raw.get("price_target"),
                        composite_score=_tr_raw.get("composite", 0.0),
                        analyst_count=_tr_raw.get("analyst_count", 0),
                    )

                _agg = m.get("aggregator") or aggregator
                agg = _agg.aggregate(
                    analysis=analysis, fft=fft, fib=fib, insider=insider,
                    vwap=vwap, vol_profile=vol_profile,
                    vix_regime=vix_regime, current_price=current_price,
                    itool_signal=_itool_signals.get(ticker),
                    momentum=momentum, supertrend=st, tipranks=_tr_result, tga=tga,
                    buy_threshold_override=bt,
                )
                l3 = fud_engine.analyze_ticker(ticker, agg)
                model_l3[m["name"]][ticker] = l3

            except Exception as e:
                logger.error(f"[{m['name'].upper()}] L2/Signals/L3 failed for {ticker}: {e}")

    # ── Decision + risk + execution per model ─────────────────────────────────
    # NOTE: the attribute is `max_trade_dollars` (RiskConfig). The old `max_dollar_per_trade`
    # name never existed, so this silently fell back to 500 and capped every position at 0.5%.
    max_dollars = getattr(config.risk, "max_trade_dollars", 500.0)

    for m in models:
        l3_results    = model_l3[m["name"]]
        portfolio_val = m.get("portfolio_value")
        de            = m["decision_engine"]
        ex            = m["executor"]

        if not l3_results:
            logger.info(f"[{m['name'].upper()}] No tickers passed L3 — skipping")
            continue

        try:
            # Load model-specific gate overrides; very_relaxed always bypasses FUD
            _model_name = m["name"]
            _go_fname   = "gate_overrides.json" if _model_name == "standard" else f"gate_overrides_{_model_name}.json"
            _gate_overrides = dict(_base_gate_overrides)
            try:
                _go_path = Path("data") / _go_fname
                if _go_path.exists():
                    _gate_overrides = _json.loads(_go_path.read_text(encoding="utf-8"))
            except Exception:
                pass
            if _model_name == "very_relaxed":
                # "*" key applies FUD bypass to every ticker in run_watchlist
                _gate_overrides.setdefault("*", [])
                if "fud" not in _gate_overrides["*"]:
                    _gate_overrides["*"].append("fud")

            decisions = de.run_watchlist(
                layer3_results=l3_results,
                portfolio_value=portfolio_val,
                max_trade_dollars=max_dollars,
                gate_overrides=_gate_overrides,
                live_prices=_live_prices,
            )
            risk_assessments = risk_manager.assess_watchlist(
                decisions=decisions,
                portfolio_value=portfolio_val,
                market_returns=[],
            )
            approved = []
            benched  = []
            for ticker, assessment in risk_assessments.items():
                if assessment.approved:
                    if (assessment.action or "").upper() == "BUY":
                        cooldown = _stop_cooldown_block(
                            ex, ticker, _live_prices.get(ticker) or assessment.entry_price
                        )
                        if cooldown:
                            benched.append(ticker)
                            logger.info(
                                f"[{m['name'].upper()}][COOLDOWN] {ticker}: BUY skipped — {cooldown}"
                            )
                            continue
                    result = ex.execute(assessment)
                    if result:
                        approved.append(ticker)
            if benched:
                logger.info(
                    f"[{m['name'].upper()}] {len(benched)} buy(s) benched by post-stop cooldown: {benched}"
                )
            logger.info(
                f"[{m['name'].upper()}] Cycle complete. {len(approved)} trade(s)" +
                (f": {approved}" if approved else ".")
            )
            import pytz as _pytz
            _cycle_time = datetime.now(_pytz.timezone("America/New_York")).strftime("%H:%M")
            _append_daily_log(_model_name, _cycle_time, decisions, set(approved), l3_results)
        except Exception as e:
            logger.error(f"[{m['name'].upper()}] Decision/risk/execute failed: {e}")

    # ── AI-managed exits (per model) ───────────────────────────────────────────
    for m in models:
        sg_file       = m.get("stagegate_file", "data/stagegate.json")
        l3_results    = model_l3[m["name"]]
        portfolio_val = m.get("portfolio_value")
        de            = m["decision_engine"]
        ex            = m["executor"]
        bt            = m.get("buy_threshold", _BASE_BUY_THRESHOLD)

        ai_exit_tickers = [t for t in _get_ai_exit_tickers(sg_file) if t not in l3_results]
        if not ai_exit_tickers:
            continue

        logger.info(f"[{m['name'].upper()}][AI EXIT] Checking {len(ai_exit_tickers)} Stage 3 ticker(s)")
        _exit_quotes = _fetch_live_quotes(ai_exit_tickers)
        for ticker in ai_exit_tickers:
            try:
                analysis = analysis_engine.analyze_ticker(ticker)
                if not analysis:
                    continue
                pd2      = _load_price_data(Session, ticker, live_quotes=_exit_quotes)
                closes   = pd2.get("closes", [])
                highs    = pd2.get("highs", [])
                lows     = pd2.get("lows", [])
                volumes  = pd2.get("volumes", [])
                cur_p    = pd2.get("current_price") or analysis.current_price
                cik      = pd2.get("cik") or ""

                fft        = fft_detector.analyze(ticker, closes)              if len(closes) >= 64  else None
                fib        = fib_analyzer.analyze(ticker, highs, lows, closes) if len(closes) >= 30  else None
                insider    = insider_analyzer.score(ticker, cik, cur_p)        if cik               else None
                vwap       = vwap_calc.compute_daily(ticker, highs, lows, closes, volumes) if len(closes) >= 5  else None
                vol_p      = vol_analyzer.analyze(ticker, highs, lows, closes, volumes)   if len(closes) >= 10 else None
                st         = st_analyzer.analyze(ticker, highs, lows, closes, volumes)    if (st_analyzer and len(closes) >= 20) else None
                tga2       = tga_analyzer.analyze(ticker, closes, highs, lows, volumes)   if (tga_analyzer and len(closes) >= 35) else None

                _tr_raw2 = _tr_get(ticker)
                _tr_result2 = None
                if _tr_raw2:
                    _tr_result2 = _TRResult(
                        ticker=ticker,
                        smart_score=_tr_raw2.get("smart_score"),
                        buy_pct=_tr_raw2.get("buy_pct"),
                        hold_pct=_tr_raw2.get("hold_pct"),
                        sell_pct=_tr_raw2.get("sell_pct"),
                        price_target_mean=_tr_raw2.get("price_target"),
                        composite_score=_tr_raw2.get("composite", 0.0),
                        analyst_count=_tr_raw2.get("analyst_count", 0),
                    )

                _agg = m.get("aggregator") or aggregator
                agg = _agg.aggregate(
                    analysis=analysis, fft=fft, fib=fib, insider=insider,
                    vwap=vwap, vol_profile=vol_p, vix_regime=vix_regime,
                    current_price=cur_p, itool_signal=_itool_signals.get(ticker),
                    supertrend=st, tipranks=_tr_result2, tga=tga2, buy_threshold_override=bt,
                )
                l3 = fud_engine.analyze_ticker(ticker, agg)
                _elp = {ticker: round(float(_exit_quotes[ticker]["last_price"]), 2)} \
                    if _exit_quotes.get(ticker, {}).get("last_price") else {}
                dec_map = de.run_watchlist(
                    layer3_results={ticker: l3},
                    portfolio_value=portfolio_val,
                    max_trade_dollars=max_dollars,
                    gate_overrides=_gate_overrides,
                    live_prices=_elp,
                )
                dec = dec_map.get(ticker)
                if dec:
                    sig = (dec.action or "").upper()
                    if sig == "SELL":
                        _ok_to_sell = True
                        try:
                            with ex.Session() as _s:
                                _pos = _s.query(PaperPosition).filter_by(ticker=ticker).first()
                                if _pos and _pos.opened_at:
                                    _opened = _pos.opened_at
                                    if _opened.tzinfo is None:
                                        _opened = _opened.replace(tzinfo=timezone.utc)
                                    _held_h = (datetime.now(timezone.utc) - _opened).total_seconds() / 3600
                                    if _held_h < 2.0:
                                        logger.info(f"[{m['name'].upper()}][AI EXIT] {ticker}: SELL suppressed — held {_held_h:.1f}h < 2h minimum")
                                        _ok_to_sell = False
                        except Exception:
                            pass
                        if _ok_to_sell:
                            logger.info(f"[{m['name'].upper()}][AI EXIT] {ticker}: {sig} — triggering SELL")
                            ex.execute_sell(ticker, cur_p or 0.0, reason=f"AI EXIT: {sig}")
            except Exception as e:
                logger.error(f"[{m['name'].upper()}][AI EXIT] Failed for {ticker}: {e}")


def build_paper_models(Session, risk_manager) -> list:
    """Build paper model configs (standard / relaxed / very_relaxed / claude)."""
    _seed_model_stagegates()
    models = []
    for name, cfg in PAPER_MODEL_CONFIGS.items():
        mult = cfg["multiplier"]
        # Claude model: lower buy threshold + custom aggregator
        if name == "claude":
            bt  = 0.08
            agg = ClaudeMomentumAggregator()
        else:
            bt  = round(_BASE_BUY_THRESHOLD * mult, 4)
            agg = None   # use shared default aggregator
        de   = DecisionEngine(db_session_factory=Session, threshold_multiplier=mult)
        ex   = PaperExecutor(
            main_db_session_factory=Session,
            db_path=cfg["db"],
            stagegate_file=cfg["stagegate"],
        )
        m = {
            "name":            name,
            "executor":        ex,
            "decision_engine": de,
            "buy_threshold":   bt,
            "stagegate_file":  cfg["stagegate"],
        }
        if agg is not None:
            m["aggregator"] = agg
        models.append(m)
        logger.info(
            f"[PAPER MODEL] {name}: multiplier={mult} buy_threshold={bt} "
            f"db={cfg['db']} sg={cfg['stagegate']}"
            + (" [ClaudeMomentumAggregator]" if agg else "")
        )
    return models


def main():
    setup_logging()
    os.makedirs("data", exist_ok=True)

    logger.info("=" * 60)
    logger.info("NWO Paper Trading Runner starting — 3 models")
    logger.info(f"Watchlist: {config.watchlist}")
    logger.info("=" * 60)

    _, Session = init_db(config.database.url, echo=False)

    analysis_engine  = FirstPrinciplesEngine(db_session_factory=Session)
    fft_detector     = FFTCycleDetector()
    fib_analyzer     = FibonacciAnalyzer()
    insider_analyzer = InsiderFlowAnalyzer()
    vwap_calc        = VWAPCalculator()
    vol_analyzer     = VolumeProfileAnalyzer()
    vix_detector     = VIXRegimeDetector()
    aggregator       = SignalAggregator()
    st_analyzer      = SuperTrendAnalyzer()
    tga_analyzer     = ThreeGreenArrowsAnalyzer()
    fud_engine       = FUDFilterEngine(db_session_factory=Session)
    risk_manager     = RiskManager(db_session_factory=Session)
    market_data      = SchwabMarketData()

    paper_models = build_paper_models(Session, risk_manager)

    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = BackgroundScheduler(timezone="America/New_York")

    scheduler.add_job(
        func=lambda: run_paper_cycle(
            Session, analysis_engine,
            fft_detector, fib_analyzer, insider_analyzer,
            vwap_calc, vol_analyzer, vix_detector, aggregator,
            fud_engine, risk_manager, market_data,
            st_analyzer, tga_analyzer, paper_models,
        ),
        trigger=CronTrigger(day_of_week="mon-fri", hour="9-16", minute="*/5"),
        id="paper_trading_cycle",
        name="Paper trading — all 3 models",
        replace_existing=True,
    )

    scheduler.start()
    logger.info("Paper trading scheduler started. Press Ctrl+C to stop.")

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Paper runner shutting down...")
        scheduler.shutdown()


if __name__ == "__main__":
    main()
