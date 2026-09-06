"""
Tests for the quantum-certainty position scalar (P4).

Quantum certainty used to be a flat >=0.60 block. Over 2026-06-03..09-05 that
was the standard model's dominant constraint: 9,381 BUY signals generated, 11
buys executed, ~94% cash. Observed certainty centres near 0.54 in the laminar/
sideways tape that dominates, so 0.60 admitted almost nothing.

It is now a hard floor at MIN_QUANTUM_CERTAIN plus a linear size ramp up to
QUANTUM_FULL_SIZE_CERTAIN.
"""

import pytest

from decision.engine import DecisionEngine


@pytest.fixture
def engine():
    return DecisionEngine(db_session_factory=None)


@pytest.fixture
def relaxed():
    return DecisionEngine(db_session_factory=None, threshold_multiplier=0.75)


# ── ramp shape ────────────────────────────────────────────────────────────────

def test_full_size_at_and_above_threshold(engine):
    assert engine._quantum_position_mult(0.60) == 1.0
    assert engine._quantum_position_mult(0.75) == 1.0
    assert engine._quantum_position_mult(1.0) == 1.0


def test_min_size_at_the_floor(engine):
    assert engine._quantum_position_mult(engine.MIN_QUANTUM_CERTAIN) == pytest.approx(
        engine.QUANTUM_MIN_SIZE_MULT)


def test_below_floor_clamps_rather_than_going_negative(engine):
    """Gate 3 blocks below the floor, but the scalar must never go negative."""
    for c in (0.0, 0.2, 0.44):
        assert engine._quantum_position_mult(c) == pytest.approx(engine.QUANTUM_MIN_SIZE_MULT)


def test_midpoint_is_halfway_up_the_ramp(engine):
    mid = (engine.MIN_QUANTUM_CERTAIN + engine.QUANTUM_FULL_SIZE_CERTAIN) / 2
    expected = engine.QUANTUM_MIN_SIZE_MULT + 0.5 * (1.0 - engine.QUANTUM_MIN_SIZE_MULT)
    assert engine._quantum_position_mult(mid) == pytest.approx(expected)


def test_ramp_is_monotonically_increasing(engine):
    vals = [engine._quantum_position_mult(c / 100) for c in range(40, 71)]
    assert all(b >= a for a, b in zip(vals, vals[1:]))


def test_observed_median_certainty_now_trades(engine):
    """
    0.54 was the observed median and was blocked outright by the old 0.60 wall.
    It must now trade, at meaningfully reduced size.
    """
    m = engine._quantum_position_mult(0.54)
    assert 0.54 >= engine.MIN_QUANTUM_CERTAIN     # no longer blocked
    assert 0.6 < m < 1.0                          # but not full size
    assert m == pytest.approx(0.76, abs=0.02)


# ── threshold_multiplier interaction ──────────────────────────────────────────

def test_relaxed_model_scales_both_ends_of_the_ramp(relaxed, engine):
    assert relaxed.MIN_QUANTUM_CERTAIN < engine.MIN_QUANTUM_CERTAIN
    assert relaxed.QUANTUM_FULL_SIZE_CERTAIN < engine.QUANTUM_FULL_SIZE_CERTAIN
    # ramp keeps its shape rather than collapsing to always-full-size
    assert relaxed.QUANTUM_FULL_SIZE_CERTAIN > relaxed.MIN_QUANTUM_CERTAIN
    assert relaxed._quantum_position_mult(relaxed.MIN_QUANTUM_CERTAIN) == pytest.approx(
        relaxed.QUANTUM_MIN_SIZE_MULT)
    assert relaxed._quantum_position_mult(relaxed.QUANTUM_FULL_SIZE_CERTAIN) == 1.0


def test_relaxed_sizes_a_given_certainty_at_least_as_large_as_standard(relaxed, engine):
    for c in (0.46, 0.50, 0.54, 0.58):
        assert relaxed._quantum_position_mult(c) >= engine._quantum_position_mult(c)


def test_degenerate_ramp_does_not_divide_by_zero(engine):
    """If the two bounds are ever configured equal, fall back to full size."""
    engine.QUANTUM_FULL_SIZE_CERTAIN = engine.MIN_QUANTUM_CERTAIN
    assert engine._quantum_position_mult(0.5) == 1.0


# ── the scalar actually reaches position sizing ───────────────────────────────

def test_ensemble_applies_the_multiplier_to_position_size():
    from decision.ensemble_kelly import EnsembleKellyEngine

    def size(qmult):
        r = EnsembleKellyEngine().run(
            ticker="TEST",
            fundamental_score=0.6, quantum_score=0.5, kalman_score=0.4,
            reynolds_position_mult=1.0, reynolds_regime="laminar",
            technical_score=0.4, insider_score=0.3, momentum_score=0.5,
            win_probability_estimate=0.65, risk_reward_ratio=3.0,
            current_price=100.0, portfolio_value=100_000.0,
            max_trade_dollars=10_000.0, max_position_pct=0.10,
            quantum_position_mult=qmult,
        )
        return r.effective_position_pct

    full, half = size(1.0), size(0.5)
    assert full > 0, "baseline should take a position"
    assert half == pytest.approx(full * 0.5, rel=0.02)


def test_ensemble_default_multiplier_is_neutral():
    """Callers that don't pass the new kwarg must be unaffected."""
    from decision.ensemble_kelly import EnsembleKellyEngine

    kw = dict(ticker="TEST", fundamental_score=0.6, quantum_score=0.5, kalman_score=0.4,
              reynolds_position_mult=1.0, reynolds_regime="laminar",
              technical_score=0.4, insider_score=0.3, momentum_score=0.5,
              win_probability_estimate=0.65, risk_reward_ratio=3.0,
              current_price=100.0, portfolio_value=100_000.0,
              max_trade_dollars=10_000.0, max_position_pct=0.10)

    assert (EnsembleKellyEngine().run(**kw).effective_position_pct
            == pytest.approx(EnsembleKellyEngine().run(**kw, quantum_position_mult=1.0)
                             .effective_position_pct))
