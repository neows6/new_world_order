# decision/ — subsystem guide

Layer 4 of the pipeline. Takes the aggregator's composite signal and the physics models' outputs, then produces a final GO/NO-GO decision with Kelly-sized position recommendation.

## Files

| File | Purpose |
|------|---------|
| `engine.py` | `DecisionEngine` — runs all 7 gates, returns trade decision |
| `ensemble_kelly.py` | Bayesian Model Averaging across 7 sub-models + Kelly Criterion sizing |
| `reynolds_turbulence.py` | Fluid-dynamics turbulence detector (Reynolds number analog) |
| `quantum_kalman.py` | Quantum state probability + Kalman price filter |

## The 7 gates (`engine.py`)

Order of evaluation in `make_decision()`:

| # | Gate | Threshold | Bypassable? |
|---|------|-----------|-------------|
| 1 | FUD filter | `layer3.proceed_to_execution` | yes (`'fud'`) |
| 2 | Reynolds regime | `allow_entry` or `Re ≤ 10.0` | yes (`'reynolds'`) |
| 3 | Quantum certainty | `≥ 0.60` (raised from 0.45) | yes (`'quantum'`) |
| 4 | Ensemble probability | `≥ 0.52` AND `≥ breakeven + 5%` | yes (`'ensemble'`) |
| 5 | Risk/Reward ratio | `≥ 1.5` | yes (`'rr'`) |
| 6 | Kalman innovation | `≤ 2.5σ` (AI Watch breakout: 5.0σ) | yes (`'kalman'`) |
| 7 | Composite signal | `signal in ("buy", "strong_buy")` | **NO** |
| 8 | EMA(10) trend | `price ≥ EMA(10) × 0.995` | yes (`'ema'`) |
| 9 | Margin of Safety | `≥ -0.50` | yes (`'mos'`) |
| 10 | Three Green Arrows | `≥ 1/3` OR positive MoS | yes (`'tga'`) |

`go_no_go = True` only if **no** gate failed.

### Class-level thresholds (`engine.py:189-193`)

```python
MIN_ENSEMBLE_PROB   = 0.52   # was 0.45 — 52% = meaningful edge over coin flip
MIN_QUANTUM_CERTAIN = 0.60   # was 0.45 — require substantial wave collapse
MAX_REYNOLDS        = 10.0   # extreme-turbulence ceiling
MIN_RR_RATIO        = 1.5
MAX_KALMAN_SURPRISE = 2.5
```

Relaxed paper-trading models scale these via `threshold_multiplier` in `__init__` (e.g. `0.75` for "Relaxed", `0.5` for "Very Relaxed").

## Ensemble Kelly (`ensemble_kelly.py`)

Bayesian Model Averaging of 7 sub-model scores → ensemble probability of bullish outcome → Kelly Criterion sizing.

### Weights

```python
ENSEMBLE_WEIGHTS = {
    "fundamental": 0.20, "momentum":  0.15, "quantum":   0.15,
    "kalman":      0.15, "reynolds":  0.10, "technical": 0.15,
    "insider":     0.10,
}
```

### Spread classification

```python
TIGHT_SPREAD    = 0.20    # confidence_mult = 1.0
MODERATE_SPREAD = 0.40    # confidence_mult = 0.75
# above moderate = wide   # confidence_mult = 0.40
```

### Adaptive Kelly fraction (recent fix)

Base `KELLY_FRACTION = 0.25` (25% of full Kelly), but scales down on disagreement:
- spread > 0.40 → `0.50 × base` (12.5% Kelly)
- spread > 0.20 → `0.75 × base` (18.75% Kelly)
- calibrated_confidence < 0.40 → additional `× 0.70`

### Breakeven win-prob gate (recent fix)

Replaces the fixed `MIN_WIN_PROB = 0.55` with:
```
breakeven_p = 1.0 / (1.0 + risk_reward_ratio)
min_win_p   = max(breakeven_p + 0.05, 0.45)
```
At R/R = 3.0, breakeven is 25%, so a 30% ensemble probability suffices. Unlocks high-R/R setups that the old floor blocked.

### Fat-tail outcome distribution (recent fix)

P10/P90 use Student's t(df=5) quantiles (`T5_Q = 1.476`) instead of normal (1.28). Captures fat tails in market returns without a scipy dependency.

## Reynolds turbulence (`reynolds_turbulence.py`)

Fluid-dynamics analog: treats volume/volatility/spread as flow parameters; computes a Reynolds-number-like ratio.

**Critical recent fix in `ensemble_kelly.py`:** the Reynolds *score* (what goes into the BMA ensemble) was wrongly mapped — turbulent state (`mult = 0.40`) was producing score `-0.20` (reads as bearish). Now uses a semantic map:
```python
_REYNOLDS_SCORE_MAP = {1.0: 0.30, 0.40: 0.00, 0.15: -0.20, 0.10: -0.30}
```

Turbulent ≠ bearish — it's cautious-neutral. Extreme is bearish.

## Quantum-Kalman (`quantum_kalman.py`)

`QuantumStateAnalyzer` computes amplitude/probability of bullish vs. bearish states from signal scores, with interference terms when signals disagree. Returns `state_certainty` (how collapsed the wave function is — high = decisive).

`KalmanPriceFilter` is a 1D Kalman filter on price → returns filtered price, velocity, uncertainty bounds, **innovation** (prediction error in σ units). Innovation > 2.5σ = unusual move, often a regime change. AI Watch breakouts relax this to 5.0σ because breakouts are inherently high-innovation events.

## Tuning thresholds

Before editing any threshold:
1. Run `diagnose.py --gates <ticker>` to see the gate breakdown
2. Identify which gate is actually blocking
3. Adjust the relevant constant in `engine.py:189-193` or `ensemble_kelly.py:71`
4. Re-run for the same ticker; verify intended gate now passes
5. Check 5 other tickers don't break

## Common debugging entry points

- `decision.engine.DecisionEngine.make_decision()` — the orchestrator; logs each gate result
- `decision.ensemble_kelly.EnsembleKellyEngine.run()` — produces `EnsembleForecastResult` with full notes/warnings
- `[ENSEMBLE/KELLY]` log lines show `P(bull)`, spread, Kelly, final %
- `[L4]` log lines show gate-by-gate pass/fail with reasons
