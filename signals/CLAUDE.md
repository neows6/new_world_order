# signals/ — subsystem guide

This directory holds every per-ticker signal module and the aggregator that composes them. Read this when adding a new signal, retuning weights, or debugging why a composite score moved.

## Files

| File | Output | Purpose |
|------|--------|---------|
| `aggregator.py` | `AggregatedSignal` | Weighted sum of all sub-signals → composite score + conviction floor gates |
| `momentum.py` | `MomentumResult` | RSI(14), MACD(12,26,9), ROC(5d/20d), EMA structure, 52w breakout, MACD/price divergence |
| `fft_cycles.py` | `FFTResult` | FFT-based cycle phase detection (trough/rising/peak/falling) |
| `fibonacci.py` | `FibResult` | Auto-detected swings → Fib retracement confluence zones |
| `market_microstructure.py` | `VIXRegime`, `VWAPResult`, `VolumeProfileResult` | VIX regime gate + VWAP/volume profile |
| `insider_flow.py` | `InsiderSignalResult` | SEC Form 4 cluster buy detection with recency decay |
| `supertrend.py` | `SuperTrendResult` | SuperTrend + take-profit favourability scoring |
| `three_green_arrows.py` | `ThreeGreenArrowsResult` | ThinkorSwim DJ_Analysis emulation (SMA/MACD/Stoch arrows) |
| `tipranks_signal.py` | `TipRanksResult` | Smart Score + analyst consensus + PT upside |
| `tradingview.py` | `TVSignalResult` | TradingView 26-indicator consensus |
| `entanglement.py` | `EntanglementEvent` | **Cross-ticker** decoherence detector — feeds the Sentinel |

## Aggregator weights (`aggregator.py`)

```
fundamentals  0.22    momentum     0.20    insider     0.17
technical     0.13    supertrend   0.10    tipranks    0.10
tga           0.05    cycle        0.02    volume      0.01
```

Thresholds:
- `BUY_THRESHOLD = 0.10` (was 0.25 — was blocking valid signals)
- Strong buy ≥ 0.50, Buy ≥ 0.10, Hold ≥ −0.15, Sell ≥ −0.40, Strong sell < −0.40

**Conviction floor gate** (line ~527): if signal is "buy" but composite < 0.50 AND no momentum/insider confirmation, downgrade to HOLD. This prevents low-confidence buys from leaking through.

**Orphaned signal guard** (line ~544): if confidence < 0.20 AND composite < 0.40, downgrade to HOLD. Reactive tuning to catch mid-band false positives.

## How signals enter the aggregator

`pipeline/analysis_pipeline.py` calls each analyzer in `run_ticker()` (lines ~242+) wrapped in try/except so any one failing doesn't break the pipeline. Each signal's contribution is bounded in `[-1.0, +1.0]` before weighting. Missing signals default to 0 (neutral).

**Kwarg name gotcha:** the aggregator expects `itool_signal=tv_signal` — not `tv_signal=`. This was a silent bug for months. If you add a new signal, double-check kwarg names match the `SignalAggregator.aggregate()` signature.

## Adding a new signal

1. Create `signals/<name>.py` with an analyzer class returning a dataclass result
2. Add weight to `SIGNAL_WEIGHTS` in `aggregator.py` (other weights must rebalance to 1.0)
3. Call the analyzer in `pipeline/analysis_pipeline.py:run_ticker()` and pass result via kwarg
4. Update the aggregator's score composition to consume it
5. Document the new weight here

## The entanglement engine (cross-ticker layer)

`entanglement.py` is structurally different from every other signal here: it operates on the **entire watchlist universe** rather than one ticker. It maintains a rolling 30-day Pearson correlation matrix across all pairs and emits `EntanglementEvent` objects when a pair's correlation breaks abruptly (|Δρ| > 2.5σ AND historical |ρ| > 0.50).

Output is consumed by `monitor/sentinel.py` (the Claude live sentinel), not the per-ticker aggregator. The engine runs in a background thread spawned by `monitor/dashboard.py` on startup; calls `signals.entanglement.start_background_runner()`.

Math primitives `_pearson()` and `_returns()` are pure functions with no DB dependencies — easy to unit test.

## Common pitfalls

- **Length mismatch**: many analyzers need same-length lists for highs/lows/closes/volumes. Always check `len()` early and bail if mismatched.
- **Insufficient bars**: minimum requirements vary (FFT needs 60+, momentum 30+, fib 30+). Return `None` rather than partial results.
- **Float precision in derived metrics**: keep multipliers explicit (e.g. `* 100.0` for percent) — composite score sensitivity to off-by-one rounding is real.
- **EDGAR fiscal year mismatch**: filtered in `moat_detector.py` via `0.0 < m <= 1.0` guard. Don't remove that filter.

## Recent fixes worth knowing about

- Reynolds turbulence rescaling fixed in `decision/ensemble_kelly.py` (turbulent state was wrongly reading as bearish)
- MACD/price divergence detection added in `momentum.py:_detect_divergence()`
- Insider signal recency decay applied in `insider_flow.py` — buys >90 days old now weight at 10%
