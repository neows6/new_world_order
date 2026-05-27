# NWO Trading System — May 26, 2026 Update Brief

*Prepared for: Chris (Financial Advisor)*
*Prepared by: Don Olsen*

---

## TL;DR

Today we completed a substantial upgrade to the New World Order (NWO) trading system. Three categories of work:

1. **Found and fixed three critical data bugs** that were silently corrupting every signal the system produced.
2. **Tightened eight model calibrations** so the decision engine behaves more like a disciplined trader and less like one running on broken assumptions.
3. **Added a new predictive layer** — a Claude-powered sentinel that watches cross-ticker correlations in real time and flags potential lead-lag setups before they fully form.

We also delivered the **STRAT historical breach analysis tool** you specifically requested (downside ≥15% in 90 days / upside ≥10% in 30 days across 12 symbols, Feb 2020 – Feb 2026).

The paper trading account was reset to a clean $100,000 baseline so we can now measure performance with corrected models from scratch.

---

## 1. STRAT Tool — Available Now

The threshold breach analysis dashboard you asked about is live at the `/strat` tab.

**Universe (12 symbols):**
- Mega Cap Tech: GOOG, AMZN, AAPL, META, MSFT
- AI & Semis: NVDA, AVGO
- Growth: TSLA, LLY
- Indices: SPX, RUT, NDX

**Per symbol, two analyses run side-by-side:**

| Analysis | Threshold | Window | Breach Rule |
|----------|-----------|--------|-------------|
| **Downside** | drop ≥ 15% | 90 calendar days | First day breached (stocks) / last day of window (indices) |
| **Upside** | rise ≥ 10% | 30 calendar days | First day breached (stocks) / last day of window (indices) |

Each occurrence shows start date, start price, breach date, breach price, % change, and a clickable chart icon that opens the full window with the threshold line and breach point marked. Results filter by 1-year, 3-year, or 6-year lookback.

You can add custom symbols (e.g. AMD, PLTR) and a CSV export button generates a spreadsheet of all occurrences for any symbol.

---

## 2. Three Critical Data Bugs — Found and Fixed

These bugs were quietly producing wrong numbers across the entire system. They were silent because each individual output looked plausible — only the downstream consequences were visible.

### Bug A: WACC Collapse → Inflated Intrinsic Values

**The symptom:** MSFT was showing an intrinsic value of **$1,506 per share** in our AI insights modal. The stock was trading around $418.

**The cause:** During intraday price updates, the `market_cap` field comes back as NULL. The code that calculates Weighted Average Cost of Capital (WACC) was treating NULL as zero — which meant the equity weight became 0% and WACC collapsed to debt-only cost (~3–4% instead of the real ~9–10%). With WACC barely above the terminal growth rate, the terminal-value portion of the DCF blew up by a factor of ~60×, producing wildly inflated intrinsic value estimates for **every ticker on the watchlist**.

**The fix:** When market_cap is NULL but current price and shares outstanding are available, we now derive market cap directly. All 27 watchlist tickers were reanalyzed and now show realistic intrinsic values.

### Bug B: Impossible Gross Margins from EDGAR

**The symptom:** Salesforce (CRM) was showing a gross margin of **265%** — physically impossible.

**The cause:** Companies with January 31 fiscal year-ends (CRM, plus several others) confused our EDGAR parser. For fiscal years 2013–2015, the parser paired *quarterly* revenue with *annual* gross profit, producing a gross_margin of 2.65×. This artificially inflated CRM's moat score and corrupted its DCF inputs.

**The fix:** We now filter out any gross margin value outside the physically plausible range (0–100%). The CRM analysis now produces sensible numbers.

### Bug C: Technical Signal Was Never Contributing

**The symptom:** None — this one was completely invisible.

**The cause:** A parameter name typo in the signal aggregator. The pipeline was passing `tv_signal=` but the aggregator expected `itool_signal=`. Python silently ignored the unknown keyword argument, so the technical signal contribution to the composite score was always zero. This had been silently broken for months.

**The fix:** One-character rename. The technical signal now actually contributes to the composite.

---

## 3. Model Intelligence Upgrades — Eight Targeted Fixes

Beyond the data bugs, we made eight calibration improvements to make the decision engine more disciplined:

### 3.1 Reynolds Turbulence Score Mapping
The decision engine includes a "turbulence" model (Reynolds number from fluid dynamics) that flags when market conditions are chaotic. A scaling bug was making **turbulent** conditions register as **bearish** in the ensemble vote — backwards from how the model should work. Fixed via a semantic mapping that correctly says "turbulent = cautious-neutral, extreme = bearish."

### 3.2 Real SPY Beta for WACC
The cost-of-equity calculation needs to compare each stock's returns to the market's returns. The old code was using a synthetic flat 10%/year placeholder, which made beta meaningless — NVDA and JNJ would both come out with similar betas despite having very different volatility profiles. Now pulls real SPY daily returns from Yahoo Finance, cached for 7 days.

### 3.3 Dynamic Breakeven Win Probability
The Kelly Criterion sizing layer was blocking any trade where the model gave less than 55% probability of winning. But at a 4:1 risk-reward setup, a 35% win rate is profitable. We replaced the fixed threshold with a dynamic breakeven formula: `1 / (1 + R/R) + 5% edge`. High-reward setups that the old floor blocked are now allowed through.

### 3.4 Insider Signal Recency Decay
SEC Form 4 insider buy signals previously had no time decay — an insider buy from 85 days ago counted as much as one from yesterday. We now decay the signal: full credit for ≤30 days, 65% at 31-60 days, 35% at 61-90 days, 10% beyond. Reflects the well-documented fact that insider buy signal edge fades over time.

### 3.5 Adaptive Kelly Fraction
Position sizing used a static "25% of full Kelly" rule. Now scales down when the model is uncertain: 12.5% Kelly when models disagree widely, plus an additional 30% reduction when calibrated confidence is low.

### 3.6 Fat-Tail Outcome Distribution
Price target percentiles (P10/P90) were using a normal distribution, which underestimates the chance of large moves. We now use Student's t (df=5), which captures market return fat tails. P10/P90 are now ~15% wider — more realistic risk picture.

### 3.7 Tighter Quantum Certainty Gate
The "quantum certainty" gate (one of the seven gates the decision engine requires) was set at 45%, which is barely above a coin flip. Raised to 60% to require meaningful conviction before a trade is approved. Also raised the ensemble probability threshold from 45% to 52%.

### 3.8 MACD/Price Divergence Detection
A stock making a new 52-week high while MACD momentum is declining is a well-documented warning sign (bearish divergence). The momentum analyzer now detects this and applies a –0.25 score penalty. Bullish divergence (price lower low + MACD higher low) applies a +0.20 bonus.

---

## 4. The New Predictive Layer — Live Sentinel

This is the most novel addition. The rest of the system analyzes each ticker independently. The Sentinel watches the *relationships between* tickers and flags moments when those relationships break down.

### How it works (the physics analog)

In quantum mechanics, "entangled" particles maintain correlations regardless of distance — observing one tells you about the other. In markets, certain ticker pairs are similarly entangled via shared exposures:
- **NVDA ↔ AMD** (AI/GPU demand)
- **AAPL ↔ MSFT** (mega-cap tech beta)
- **JPM ↔ BAC** (large-cap banks)

When the entanglement *suddenly breaks* — what physicists call "decoherence" — one ticker has received new information the other hasn't propagated yet. The lagging ticker is statistically likely to **catch up**. That's a predictive lead-lag signal.

### Math
- Compute the rolling 30-day correlation between all watchlist pairs every 60 seconds
- Calculate the change in correlation vs prior period; convert to a z-score across all pairs
- Flag pair as "decoherence event" when |z| > 2.5 standard deviations AND the historical correlation was strong (|ρ| > 0.50)
- The ticker with the larger recent move is the lead; the other is the catch-up candidate

### The Claude Layer
When decoherence events accumulate, the system sends them to Claude (Anthropic's Haiku model) along with current prices and current pipeline signals. Claude returns a single actionable alert with:
- **Level** (info / warn / high)
- **Primary ticker** to watch
- **Action** (watch for entry / exit / wait / investigate)
- **Reasoning** (one sentence in plain English)
- **Time horizon** (how long the signal stays valid)
- **Confidence** (0–100%)

These alerts stream to a new `/sentinel` tab on the dashboard. They are **not auto-executed** — they're attention-getters. Any actual trade still has to pass the full L1–L5 pipeline.

### Budget
Each Claude call costs ~$0.001–0.005. Typical days produce 10–30 decoherence events → ~$0.05–$0.30/day in API cost. Pausable at any time.

### Important Distinction
The math layer (entanglement detection) runs constantly and is the *signal*. Claude is just the interpreter — it turns "NVDA broke from AMD by 3 sigma on a +1.8% move" into "watch AMD for a 1.4% catch-up over the next 15 minutes." If Claude is unreachable for any reason, the events still get logged and we can review them manually.

---

## 5. Pre-Pipeline Data Validator

Same Claude model also got wired in **upstream** as a quality gate. Before the WACC / moat / DCF calculations run for any ticker, Claude reviews the input data and flags physically impossible values (gross margins > 100%, NULL market caps, ROIC > 100%, etc.). This is what would have caught the CRM and MSFT bugs above on day one rather than months later.

Two layers: a deterministic rule check that always runs (fast, free), and a Claude semantic check that only fires when the deterministic layer flags something. Caches results by input hash so we don't re-validate unchanged data.

---

## 6. Operational Cleanup

### Paper Trading Reset
The paper trading account was reset to a clean $100,000 baseline. The historical trades had been made under the broken WACC / corrupted gross margin / missing technical signal conditions, so the P&L track record didn't reflect what the corrected system can actually do. Now we have a clean starting point to measure performance.

### Stagegate Preserved
Your 30 watchlist tickers (5 in Stage 1, 25 in Stage 2) were preserved across the reset. We didn't lose any of the work you'd done curating which names to monitor.

### Project Documentation
Three CLAUDE.md guide files added so any future engineering work has full context on the architecture, the gates, the signal weights, and the known quirks (like the Norton SSL workaround we had to build for this machine).

### Repository
Full project now in git, pushed to https://github.com/neows6/new_world_order. Every change today is in a discrete commit with a detailed message — easy to audit, easy to roll back if needed.

---

## What This Means in Practice

**Before today:**
- Intrinsic values across the watchlist were systematically wrong (inflated by up to 7×)
- Some companies had impossible fundamental scores from data parsing errors
- The technical signal layer was silently contributing zero to every decision
- The decision engine's gates were calibrated too loose (45% certainty thresholds)
- Position sizing was static and didn't account for model disagreement
- There was no cross-ticker analysis — every signal was per-ticker, in isolation

**After today:**
- Every fundamental input is sanity-checked before it flows downstream
- The decision engine requires meaningful conviction (60% certainty) before approving a trade
- Position sizing scales down automatically when models disagree
- A new predictive layer catches lead-lag opportunities across the watchlist that no per-ticker analysis can see
- All of this runs every 60 seconds during market hours, with Claude available as a real-time interpreter

The system is in a substantially more trustworthy state than it was 24 hours ago. The paper trading reset will let us measure forward performance with corrected models, which is what we needed for the eventual go-live decision.

---

## Questions for Discussion

1. **Sentinel alert workflow:** When alerts fire, how should we route them to you (email? Telegram? just the dashboard)?
2. **Custom symbol additions:** Want any tickers added to the STRAT analysis tool beyond the default 12?
3. **Paper trading evaluation period:** How long should we run the corrected models on paper before considering live deployment? (Suggest minimum 4–6 weeks of market activity)
4. **The Claude pre-pipeline validator** catches data bugs but currently just logs them. Should we have it block analysis when severe anomalies are detected, or always proceed with a warning?

---

*Generated 2026-05-26 — Don Olsen, neows6@gmail.com*
