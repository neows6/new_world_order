# New World Order (NWO) — Systematic Trading System
### Overview for Financial Adviser
*Prepared April 2026 — Confidential*

---

## What Is NWO?

NWO is a personal, fully automated equity analysis and paper trading system built on top of the Charles Schwab Trader API. It is currently running in **paper (simulated) mode only** — no real capital is at risk. The system is designed to develop, test, and refine a rules-based investment approach before any live capital is committed.

The system monitors a curated watchlist of equities and, on a 5-minute cycle during market hours, runs each ticker through a 6-layer analytical pipeline, then simulates trades in a virtual $100,000 account.

---

## The Analytical Pipeline (6 Layers)

| Layer | What It Does |
|---|---|
| 1. Data Ingestion | Pulls real-time prices from Schwab, fundamentals from SEC EDGAR, and news from regulated sources (Reuters, EDGAR filings) |
| 2. First Principles Analysis | Calculates owner earnings, ROIC vs WACC, gross margin trends, and net debt ratios — assesses intrinsic quality |
| 3. FUD Filter | Scores news sources for credibility; discards high-emotion / low-information noise; elevates Form 4 insider filings and earnings transcripts |
| 4. Decision Engine | 7 quantitative gates (trend confirmation, momentum, statistical ensemble, risk/reward ratio) must all pass before a trade is considered |
| 5. Risk Manager | Max 5% per position, 25% per sector, 3% daily drawdown halt — hard-coded, non-negotiable |
| 6. Executor | Places a simulated trade with stop-loss and take-profit levels automatically calculated |

---

## The 4-Model Paper Trading Experiment

To understand where the system's thresholds are well-calibrated versus too conservative or too aggressive, four independent models are running simultaneously — each starting with an identical $100,000 simulated balance on the same date. They trade the same watchlist but with different decision criteria.

### Model 1 — Standard (Control)
The baseline configuration. All 7 decision gates operate at their designed levels. Requires strong fundamental quality, a composite signal score above threshold, and a bull probability above 45% from the statistical ensemble model. This is the model intended for eventual live trading if it proves its edge.

**What it tests:** Does the full, conservative pipeline generate alpha?

---

### Model 2 — Relaxed (−25% Thresholds)
Identical to Standard but every decision gate is reduced by 25%. For example, a signal score that Standard requires to be 0.10 only needs to be 0.075 here.

**What it tests:** Is Standard leaving money on the table by being too cautious? Or does relaxing thresholds introduce noise that hurts returns?

---

### Model 3 — Very Relaxed (−50% Thresholds)
All thresholds halved. Enters on weak signals and relies heavily on the stop-loss discipline to limit damage. Trades significantly more frequently.

**What it tests:** A lower bound. If this model performs poorly, it validates that the stricter thresholds in Standard are doing real filtering work, not just suppressing trades arbitrarily.

---

### Model 4 — Claude (AI Momentum Strategy)
This model was designed from first principles as a pure momentum and trend-following strategy, deliberately contrasting with the value-oriented approach of the other three.

**Key differences:**
- Signal weights heavily favour **SuperTrend** (35%) and **momentum** (30%); fundamentals carry only 5% weight
- Stocks that fail value screens (ROIC < WACC, no margin of safety) are not automatically penalised — breakout/trending stocks like Tesla and Palantir are evaluated primarily on price behaviour
- **Hard VIX gate:** no new positions when the CBOE Volatility Index exceeds 30 (market fear is too high); position sizing is reduced when VIX is above 25
- Named "Claude" because this strategy was designed by the AI assistant as its own independent approach — it is philosophically distinct from the other three models

**What it tests:** Can a momentum/trend approach outperform a fundamentals-first approach on this watchlist over this timeframe? And does AI-designed strategy construction add value?

---

## The Experiment

The four models run side-by-side in real market conditions on a real portfolio composition (same watchlist, same prices, same news) but with different decision logic. Over 3–6 months, the comparison will answer:

1. Is the Standard model's caution justified by better risk-adjusted returns?
2. Do looser thresholds generate more trades with proportionally higher returns, or just more noise?
3. Does a momentum/trend approach (Claude model) outperform the fundamentals-first approach in this market regime?
4. What is the relationship between trade frequency and drawdown across the four models?

The comparison dashboard is accessible at `/paper/compare` and refreshes every 30 seconds.

---

## What This Is Not

- This is **not** algorithmic high-frequency trading. The system trades at most once per 5-minute cycle per ticker, based on multi-layer fundamental and technical analysis.
- This is **not** leveraged or options-based (a separate Wheel Strategy options screener exists but is manual).
- There is **no live capital at risk** at this time. The dry-run flag must be explicitly disabled before any real orders would be placed, and an additional account-level risk review would precede that step.

---

## Summary

NWO is a disciplined, data-driven attempt to develop a systematic trading approach using public market data, company filings, and quantitative signal processing. The 4-model experiment provides a controlled framework to evaluate whether conservative versus aggressive threshold calibration — and fundamentals versus momentum weighting — produces meaningfully different outcomes in real market conditions.

Results will be reviewed periodically as part of broader portfolio and strategy discussions.

---

*System built and maintained by the account holder. Running on paper trade simulation only.*
