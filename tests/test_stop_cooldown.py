"""
Tests for the post-stop re-entry cooldown (P3).

Over 2026-06-03..09-05, 55-65% of buys in the relaxed/very_relaxed/claude books
were re-entries into a ticker that had just stopped out (NVDA 6x in very_relaxed
with 4 stops; PLD 4x in claude with 4 stops and zero wins). The cooldown benches
a stopped-out name unless price reclaims the entry the stop invalidated.
"""

from datetime import datetime, timedelta, timezone

import pytest

from paper.runner import _stop_cooldown_block, STOP_REENTRY_COOLDOWN_DAYS


class FakeTrade:
    def __init__(self, ticker, action, price, timestamp, notes=""):
        self.ticker = ticker
        self.action = action
        self.price = price
        self.timestamp = timestamp
        self.notes = notes


class FakeQuery:
    """Minimal stand-in for the SQLAlchemy query chain the helper uses."""

    def __init__(self, trades):
        self._trades = list(trades)
        self._action = None
        self._before = None

    def filter(self, *conditions):
        # Conditions are opaque here; the harness sets intent via _spec instead.
        return self

    def order_by(self, *a):
        return self

    def first(self):
        return self._trades[0] if self._trades else None


class FakeSession:
    """
    Returns the newest SELL first, then the newest BUY preceding it — matching
    the two queries the helper issues, in order.
    """

    def __init__(self, trades):
        self.trades = sorted(trades, key=lambda t: t.timestamp, reverse=True)
        self._call = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def query(self, _model):
        self._call += 1
        if self._call == 1:
            sells = [t for t in self.trades if t.action == "SELL"]
            return FakeQuery(sells)
        last_sell = next((t for t in self.trades if t.action == "SELL"), None)
        buys = [t for t in self.trades
                if t.action == "BUY" and (not last_sell or t.timestamp < last_sell.timestamp)]
        return FakeQuery(buys)


class FakeExecutor:
    def __init__(self, trades):
        self._trades = trades

    def Session(self):
        return FakeSession(self._trades)


def _now():
    return datetime.now(timezone.utc)


def stopped_out(days_ago, entry=100.0, exit_px=96.0):
    return [
        FakeTrade("NVDA", "BUY", entry, _now() - timedelta(days=days_ago + 3)),
        FakeTrade("NVDA", "SELL", exit_px, _now() - timedelta(days=days_ago),
                  notes=f"STOP LOSS: ${exit_px:.2f} <= SL ${exit_px:.2f}"),
    ]


# ── blocking ──────────────────────────────────────────────────────────────────

def test_blocks_immediately_after_a_stop_out():
    ex = FakeExecutor(stopped_out(days_ago=1))
    assert _stop_cooldown_block(ex, "NVDA", current_price=97.0) is not None


def test_still_blocked_just_inside_the_window():
    ex = FakeExecutor(stopped_out(days_ago=STOP_REENTRY_COOLDOWN_DAYS - 1))
    assert _stop_cooldown_block(ex, "NVDA", current_price=97.0) is not None


def test_allows_once_the_cooldown_elapses():
    ex = FakeExecutor(stopped_out(days_ago=STOP_REENTRY_COOLDOWN_DAYS + 1))
    assert _stop_cooldown_block(ex, "NVDA", current_price=97.0) is None


def test_message_names_the_ticker_state():
    ex = FakeExecutor(stopped_out(days_ago=2))
    msg = _stop_cooldown_block(ex, "NVDA", current_price=97.0)
    assert "cooldown remaining" in msg
    assert "100.00" in msg          # the reclaim level is surfaced


# ── reclaim override ──────────────────────────────────────────────────────────

def test_reclaiming_the_original_entry_allows_early_reentry():
    ex = FakeExecutor(stopped_out(days_ago=1, entry=100.0, exit_px=96.0))
    assert _stop_cooldown_block(ex, "NVDA", current_price=101.0) is None


def test_bouncing_but_below_entry_stays_blocked():
    """A dead-cat bounce above the stop but under the entry is still benched."""
    ex = FakeExecutor(stopped_out(days_ago=1, entry=100.0, exit_px=96.0))
    assert _stop_cooldown_block(ex, "NVDA", current_price=99.5) is not None


def test_exactly_at_entry_stays_blocked():
    ex = FakeExecutor(stopped_out(days_ago=1, entry=100.0, exit_px=96.0))
    assert _stop_cooldown_block(ex, "NVDA", current_price=100.0) is not None


# ── scope ─────────────────────────────────────────────────────────────────────

def test_take_profit_exit_does_not_trigger_cooldown():
    """Only stop-outs bench a name; winners may be re-entered freely."""
    trades = [
        FakeTrade("NVDA", "BUY", 100.0, _now() - timedelta(days=4)),
        FakeTrade("NVDA", "SELL", 112.0, _now() - timedelta(days=1),
                  notes="TAKE PROFIT: $112.00 >= TP1 $111.00"),
    ]
    assert _stop_cooldown_block(FakeExecutor(trades), "NVDA", current_price=113.0) is None


def test_never_traded_ticker_is_allowed():
    assert _stop_cooldown_block(FakeExecutor([]), "AAPL", current_price=200.0) is None


def test_missing_price_still_blocks_within_window():
    """No live quote must not silently waive the cooldown."""
    ex = FakeExecutor(stopped_out(days_ago=1))
    assert _stop_cooldown_block(ex, "NVDA", current_price=None) is not None


def test_lookup_failure_fails_open():
    """A broken session must not block trading."""
    class Boom:
        def Session(self):
            raise RuntimeError("db gone")

    assert _stop_cooldown_block(Boom(), "NVDA", current_price=100.0) is None


def test_naive_timestamp_is_treated_as_utc():
    """Timestamps stored without tzinfo must not raise on comparison."""
    naive_now = _now().replace(tzinfo=None)
    trades = [
        FakeTrade("NVDA", "BUY", 100.0, naive_now - timedelta(days=4)),
        FakeTrade("NVDA", "SELL", 96.0, naive_now - timedelta(days=1),
                  notes="STOP LOSS: $96.00 <= SL $96.00"),
    ]
    assert _stop_cooldown_block(FakeExecutor(trades), "NVDA", current_price=97.0) is not None
