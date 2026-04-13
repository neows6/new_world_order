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
        self._decision = self._risk = None
        self._executor   = None
        self._market_data = None
        self._paper_Session = None

    # ── Public API ─────────────────────────────────────────────────────────────

    def start(self):
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._cycle_loop,   daemon=True, name="paper-cycle").start()
        threading.Thread(target=self._monitor_loop, daemon=True, name="paper-stops").start()
        logger.info("[AUTO] Paper scheduler started — cycle every 5 min, stops every 60s (market hours)")

    def pause(self):
        self._paused = True
        logger.info("[AUTO] Paper scheduler paused")

    def resume(self):
        self._paused = False
        logger.info("[AUTO] Paper scheduler resumed")

    def trigger_cycle(self):
        """Fire a manual cycle immediately (Run Now). Non-blocking."""
        threading.Thread(target=self._run_cycle, daemon=True, name="paper-manual").start()

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
        while True:
            try:
                if not self._paused and _is_market_hours():
                    now = datetime.now(ET)
                    if now.minute % 5 == 0 and now.second < 15:
                        if (self._last_cycle is None or
                                (datetime.now(timezone.utc) - self._last_cycle).total_seconds() > 250):
                            self._run_cycle()
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
                Session        = self._Session,
                analysis_engine= self._analysis_engine,
                fft_detector   = self._fft,
                fib_analyzer   = self._fib,
                insider_analyzer=self._insider,
                vwap_calc      = self._vwap,
                vol_analyzer   = self._vol,
                vix_detector   = self._vix,
                aggregator     = self._aggregator,
                fud_engine     = self._fud,
                decision_engine= self._decision,
                risk_manager   = self._risk,
                executor       = self._executor,
                market_data    = self._market_data,
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

    # ── Engine initialization ──────────────────────────────────────────────────

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
            from fud.filter_engine import FUDFilterEngine
            from decision.engine import DecisionEngine
            from risk.manager import RiskManager
            from broker.market_data import SchwabMarketData
            from paper.executor import PaperExecutor
            from paper.account import init_paper_db

            _, self._Session = init_db(self._db_url, echo=False)

            self._analysis_engine = FirstPrinciplesEngine(db_session_factory=self._Session)
            self._fft             = FFTCycleDetector()
            self._fib             = FibonacciAnalyzer()
            self._insider         = InsiderFlowAnalyzer()
            self._vwap            = VWAPCalculator()
            self._vol             = VolumeProfileAnalyzer()
            self._vix             = VIXRegimeDetector()
            self._aggregator      = SignalAggregator()
            self._fud             = FUDFilterEngine(db_session_factory=self._Session)
            self._decision        = DecisionEngine(db_session_factory=self._Session)
            self._risk            = RiskManager(db_session_factory=self._Session)
            self._market_data     = SchwabMarketData()
            self._executor        = PaperExecutor(main_db_session_factory=self._Session)

            _, self._paper_Session = init_paper_db()

            self._engines_ready = True
            logger.info("[AUTO] Engine pool ready")
        except Exception as e:
            logger.error(f"[AUTO] Engine init failed: {e}")
            raise

    def _apply_thresh_override(self):
        """Mirror dashboard's threshold override logic."""
        try:
            import json
            from pathlib import Path
            from decision.engine import DecisionEngine as DE
            f = Path("data/thresh_override.json")
            if not f.exists():
                return
            o = json.loads(f.read_text())
            if "min_confidence"     in o: DE.MIN_CONFIDENCE     = o["min_confidence"]
            if "min_mos"            in o: DE.MIN_MARGIN_OF_SAFETY = o["min_mos"]
            if "min_fud_quality"    in o: DE.MIN_FUD_QUALITY     = o["min_fud_quality"]
            if "reynolds_bypass"    in o: DE.REYNOLDS_BYPASS     = o["reynolds_bypass"]
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
