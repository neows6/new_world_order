"""
paper/auto_scheduler.py — Persistent engine pool + background scheduler.

Two jobs fire automatically during market hours (Mon-Fri 9:30am–4:00pm ET):
  1. Full 6-layer AI cycle  — every 5 minutes  (same logic as paper/runner.py)
  2. Stop / take-profit monitor — every 60 seconds

All engines are initialized ONCE at startup and reused, so Run Now and the
auto-scheduler share the same objects rather than cold-starting each time.

Usage (from dashboard startup):
    from paper.auto_scheduler import PaperScheduler
    scheduler = PaperScheduler(main_db_url=config.database.url)
    scheduler.start()

Run Now (manual trigger from dashboard):
    scheduler.trigger_cycle()

Status (for /api/paper/scheduler endpoint):
    scheduler.status()  →  dict
"""

import threading
from datetime import datetime, timezone
from typing import Optional

import pytz
from loguru import logger

ET = pytz.timezone("America/New_York")

# ── Market hours helper ────────────────────────────────────────────────────────

def _is_market_hours() -> bool:
    """Return True if current ET time is Mon-Fri 9:30am–4:00pm."""
    now = datetime.now(ET)
    if now.weekday() >= 5:          # Saturday=5, Sunday=6
        return False
    t = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= t <= (16 * 60)


# ── Scheduler ─────────────────────────────────────────────────────────────────

class PaperScheduler:
    """
    Holds the engine pool and runs two background threads:
      - _cycle_thread  : fires full AI cycle every 5 min during market hours
      - _monitor_thread: fires stop/target check every 60s during market hours
    """

    def __init__(self, main_db_url: str):
        self._db_url        = main_db_url
        self._lock          = threading.Lock()
        self._cycle_running = False   # True while a cycle is executing
        self._last_cycle:   Optional[datetime] = None
        self._next_cycle:   Optional[datetime] = None
        self._last_stop_check: Optional[datetime] = None
        self._cycle_count   = 0
        self._stop_exits    = 0       # total stop/target exits triggered
        self._started       = False
        self._paused        = False

        # Engine pool — initialized lazily on first use
        self._engines_ready = False
        self._Session       = None
        self._analysis_engine = None
        self._fft = self._fib = self._insider = None
        self._vwap = self._vol = self._vix = None
        self._aggregator = self._fud = None
        self._risk = None
        self._market_data = None
        self._paper_Session = None
        self._st_analyzer   = None
        self._paper_models  = None   # list of 3 model configs

    # ── Public API ─────────────────────────────────────────────────────────────

    def start(self):
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._cycle_loop,      daemon=True, name="paper-cycle").start()
        threading.Thread(target=self._monitor_loop,    daemon=True, name="paper-stops").start()
        threading.Thread(target=self._r2000_loop,      daemon=True, name="paper-r2000").start()
        threading.Thread(target=self._tipranks_loop,   daemon=True, name="paper-tipranks").start()
        threading.Thread(target=self._prewarm_engines, daemon=True, name="paper-prewarm").start()
        logger.info("[AUTO] Paper scheduler started — cycle every 5 min, stops every 60s, R2000 scan 3x/day, TipRanks scan 2x/day")

    def pause(self):
        self._paused = True
        logger.info("[AUTO] Paper scheduler paused")

    def resume(self):
        self._paused = False
        logger.info("[AUTO] Paper scheduler resumed")

    def trigger_cycle(self, force: bool = False) -> str:
        """
        Fire a manual cycle (Run Now). Returns status string.
        Blocked outside market hours unless force=True.
        """
        if not force and not _is_market_hours():
            logger.info("[AUTO] Run Now blocked — outside market hours (9:30am–4:00pm ET Mon-Fri)")
            return "outside_market_hours"
        threading.Thread(target=self._run_cycle, daemon=True, name="paper-manual").start()
        return "started"

    def status(self) -> dict:
        now_et = datetime.now(ET)
        mh     = _is_market_hours()

        def _fmt(dt: Optional[datetime]) -> Optional[str]:
            if dt is None:
                return None
            local = dt.astimezone(ET)
            return local.strftime("%H:%M:%S ET")

        # Compute next scheduled fire (next 5-min boundary)
        next_str = None
        if mh and not self._paused:
            mins_past = now_et.minute % 5
            secs_past = mins_past * 60 + now_et.second
            secs_to_next = 300 - secs_past
            from datetime import timedelta
            nxt = now_et + timedelta(seconds=secs_to_next)
            next_str = nxt.strftime("%H:%M ET")

        return {
            "running":         self._cycle_running,
            "paused":          self._paused,
            "market_hours":    mh,
            "last_cycle":      _fmt(self._last_cycle),
            "next_cycle":      next_str,
            "last_stop_check": _fmt(self._last_stop_check),
            "cycle_count":     self._cycle_count,
            "stop_exits":      self._stop_exits,
            "engines_ready":   self._engines_ready,
        }

    # ── Background loops ───────────────────────────────────────────────────────

    def _cycle_loop(self):
        """Fire the full AI cycle every 5 minutes on the clock boundary."""
        import time
        _eod_snapped: set = set()   # dates already snapshotted
        while True:
            try:
                now = datetime.now(ET)
                if not self._paused and _is_market_hours():
                    if now.minute % 5 == 0 and now.second < 15:
                        if (self._last_cycle is None or
                                (datetime.now(timezone.utc) - self._last_cycle).total_seconds() > 250):
                            self._run_cycle()
                # Save EOD snapshot at 16:00 ET (market close)
                today = now.date()
                if (now.hour == 16 and now.minute == 0 and now.second < 30
                        and now.weekday() < 5
                        and today not in _eod_snapped):
                    _eod_snapped.add(today)
                    self._save_eod_snapshots()
            except Exception as e:
                logger.warning(f"[AUTO] Cycle loop error: {e}")
            time.sleep(10)

    def _monitor_loop(self):
        """Fire the stop/target check every 60 seconds."""
        import time
        while True:
            try:
                if not self._paused and _is_market_hours():
                    self._run_stop_check()
            except Exception as e:
                logger.warning(f"[AUTO] Stop monitor loop error: {e}")
            time.sleep(60)

    def _tipranks_loop(self):
        """Poll every 30s; fire TipRanks batch scan at 09:15 and 16:05 ET."""
        import time, json
        from pathlib import Path as _Path
        while True:
            try:
                from monitor.tipranks_scanner import should_fire_now as _tr_fire, run_scan as _tr_scan
                slot = _tr_fire()
                if slot and not self._paused:
                    from config import config as _cfg
                    from paper.executor import PAPER_MODEL_CONFIGS
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
                    logger.info(f"[AUTO] Firing TipRanks scan for slot {slot} — {len(tickers)} tickers")
                    threading.Thread(
                        target=_tr_scan,
                        args=(list(tickers),),
                        daemon=True,
                        name=f"tipranks-scan-{slot}",
                    ).start()
            except Exception as e:
                logger.warning(f"[AUTO] TipRanks loop error: {e}")
            time.sleep(30)

    def _r2000_loop(self):
        """Poll every 30s; fire R2000 scan at 08:30, 12:00, 15:00 ET."""
        import time
        while True:
            try:
                from paper.r2000_scanner import should_fire_now, run_scan
                slot = should_fire_now()
                if slot and not self._paused:
                    self._ensure_engines()
                    engines = {
                        "fft":        self._fft,
                        "fib":        self._fib,
                        "vwap":       self._vwap,
                        "vol":        self._vol,
                        "vix":        self._vix,
                        "aggregator": self._aggregator,
                        "st":         self._st_analyzer,
                        "analysis":   self._analysis_engine,
                        "market_data":self._market_data,
                    }
                    logger.info(f"[AUTO] Firing R2000 scan for slot {slot}")
                    threading.Thread(
                        target=run_scan,
                        args=(self._Session, engines),
                        daemon=True,
                        name=f"r2000-scan-{slot}",
                    ).start()
            except Exception as e:
                logger.warning(f"[AUTO] R2000 loop error: {e}")
            time.sleep(30)

    # ── Core jobs ──────────────────────────────────────────────────────────────

    def _run_cycle(self):
        with self._lock:
            if self._cycle_running:
                logger.debug("[AUTO] Cycle already running — skipping")
                return
            self._cycle_running = True

        try:
            self._ensure_engines()
            self._apply_thresh_override()

            # Refresh prices for config.watchlist + all stage gate tickers
            try:
                from pipeline.ingestion import IngestionPipeline
                from config import config as _cfg
                import json as _json
                from pathlib import Path as _Path
                sg_file = _Path("data/stagegate.json")
                sg_tickers = []
                if sg_file.exists():
                    _sg = _json.loads(sg_file.read_text(encoding="utf-8"))
                    for k in ("stage1", "stage2", "stage3"):
                        sg_tickers.extend(_sg.get(k, []))
                all_tickers = list(dict.fromkeys(list(_cfg.watchlist) + sg_tickers))
                price_pipeline = IngestionPipeline(db_session_factory=self._Session)
                price_pipeline.run_prices_only(tickers=all_tickers)
            except Exception as e:
                logger.warning(f"[AUTO] Price refresh failed (using cached): {e}")

            from paper.runner import run_paper_cycle
            run_paper_cycle(
                Session         = self._Session,
                analysis_engine = self._analysis_engine,
                fft_detector    = self._fft,
                fib_analyzer    = self._fib,
                insider_analyzer= self._insider,
                vwap_calc       = self._vwap,
                vol_analyzer    = self._vol,
                vix_detector    = self._vix,
                aggregator      = self._aggregator,
                fud_engine      = self._fud,
                risk_manager    = self._risk,
                market_data     = self._market_data,
                st_analyzer     = self._st_analyzer,
                models          = self._paper_models,
            )

            self._last_cycle = datetime.now(timezone.utc)
            self._cycle_count += 1
            logger.info(f"[AUTO] Cycle #{self._cycle_count} complete")

        except Exception as e:
            logger.error(f"[AUTO] Cycle failed: {e}")
        finally:
            self._cycle_running = False

    def _run_stop_check(self):
        try:
            self._ensure_engines()
            from paper.stop_monitor import check_stops
            exits = check_stops(self._paper_Session, self._market_data)
            self._last_stop_check = datetime.now(timezone.utc)
            if exits:
                self._stop_exits += exits
        except Exception as e:
            logger.warning(f"[AUTO] Stop check failed: {e}")

    def _save_eod_snapshots(self):
        """Save end-of-day equity snapshot for all models at market close."""
        try:
            from paper.executor import PAPER_MODEL_CONFIGS
            from paper.account import init_paper_db, PaperAccount, PaperPosition, PaperEquitySnapshot
            from datetime import date
            today = date.today()
            for model, cfg in PAPER_MODEL_CONFIGS.items():
                try:
                    _, MSession = init_paper_db(cfg["db"])
                    with MSession() as s:
                        if s.query(PaperEquitySnapshot).filter_by(snap_date=today).first():
                            continue
                        acct = s.query(PaperAccount).first()
                        positions = s.query(PaperPosition).filter(PaperPosition.qty > 0).all()
                        cash = acct.cash if acct else 0.0
                        pos_val = sum(p.qty * p.avg_cost for p in positions)
                        snap = PaperEquitySnapshot(
                            snap_date=today,
                            total_equity=round(cash + pos_val, 2),
                            cash=round(cash, 2),
                            positions_value=round(pos_val, 2),
                        )
                        s.add(snap)
                        s.commit()
                        logger.info(f"[AUTO] EOD snapshot saved for {model}: equity={cash+pos_val:.2f}")
                except Exception as e:
                    logger.warning(f"[AUTO] EOD snapshot failed for {model}: {e}")
        except Exception as e:
            logger.warning(f"[AUTO] EOD snapshot job failed: {e}")

    # ── Engine initialization ──────────────────────────────────────────────────

    def _prewarm_engines(self):
        """Initialize engine pool shortly after server start so dashboard shows Ready immediately."""
        import time
        time.sleep(15)
        try:
            self._ensure_engines()
        except Exception:
            pass  # Will retry on next market-hours cycle

    def _ensure_engines(self):
        if self._engines_ready:
            return
        logger.info("[AUTO] Initializing paper engine pool...")
        try:
            from models.database import init_db
            from analysis.engine import FirstPrinciplesEngine
            from signals.fft_cycles import FFTCycleDetector
            from signals.fibonacci import FibonacciAnalyzer
            from signals.insider_flow import InsiderFlowAnalyzer
            from signals.market_microstructure import (
                VWAPCalculator, VolumeProfileAnalyzer, VIXRegimeDetector
            )
            from signals.aggregator import SignalAggregator
            from signals.supertrend import SuperTrendAnalyzer
            from signals.three_green_arrows import ThreeGreenArrowsAnalyzer
            from signals.momentum import MomentumAnalyzer
            from fud.filter_engine import FUDFilterEngine
            from risk.manager import RiskManager
            from broker.market_data import SchwabMarketData
            from paper.account import init_paper_db
            from paper.runner import build_paper_models

            _, self._Session = init_db(self._db_url, echo=False)

            self._analysis_engine = FirstPrinciplesEngine(db_session_factory=self._Session)
            self._fft             = FFTCycleDetector()
            self._fib             = FibonacciAnalyzer()
            self._insider         = InsiderFlowAnalyzer()
            self._vwap            = VWAPCalculator()
            self._vol             = VolumeProfileAnalyzer()
            self._vix             = VIXRegimeDetector()
            self._aggregator      = SignalAggregator()
            self._st_analyzer     = SuperTrendAnalyzer()
            self._tga_analyzer    = ThreeGreenArrowsAnalyzer()
            self._momentum_analyzer = MomentumAnalyzer()
            self._fud             = FUDFilterEngine(db_session_factory=self._Session)
            self._risk            = RiskManager(db_session_factory=self._Session)
            self._market_data     = SchwabMarketData()
            self._paper_models    = build_paper_models(self._Session, self._risk)

            _, self._paper_Session = init_paper_db()

            self._engines_ready = True
            logger.info("[AUTO] Engine pool ready")
        except Exception as e:
            logger.error(f"[AUTO] Engine init failed: {e}")
            raise

    def _apply_thresh_override(self):
        """Apply UI threshold override to the standard model's decision engine only."""
        try:
            import json
            from pathlib import Path
            from decision.engine import DecisionEngine as DE
            MULT = {"high": 1.0, "med": 0.85, "low": 0.70}
            f = Path("data/thresh_override.json")
            if not f.exists():
                return
            o = json.loads(f.read_text())
            lv = o.get("level", "high")
            m = MULT.get(lv, 1.0)
            # Only update class-level attrs (affects standard model which has no instance overrides)
            DE.MIN_ENSEMBLE_PROB   = round(0.45 * m, 4)
            DE.MIN_QUANTUM_CERTAIN = round(0.45 * m, 4)
            DE.MAX_REYNOLDS        = round(5.0  / m, 4) if m > 0 else 5.0
            DE.MIN_RR_RATIO        = round(1.5  * m, 4)
            DE.MAX_KALMAN_SURPRISE = round(2.5  / m, 4) if m > 0 else 2.5
        except Exception:
            pass


# ── Module-level singleton ────────────────────────────────────────────────────

_scheduler: Optional[PaperScheduler] = None


def get_scheduler() -> Optional[PaperScheduler]:
    return _scheduler


def init_scheduler(main_db_url: str) -> PaperScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = PaperScheduler(main_db_url)
        _scheduler.start()
    return _scheduler
