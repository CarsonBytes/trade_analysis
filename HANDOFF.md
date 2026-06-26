# Project Handoff — D:\quant quant trading platform

**Purpose of this doc:** let a new session continue the work without prior context.
Last updated 2026-06-25.

---

## ⭐⭐⭐ ADOPTED PLAN & FIGURES (single source of truth, 2026-06-25)

**LIVE on IBKR paper since 2026-06-24** — `BROKER=ib` + `UNIVERSE=etf`, account DUK968178
(~US$130k / 1.01M HKD), port 4002. Restart: `Stop/Start-ScheduledTask DashboardApp`.

**Strategy = 17-ETF long-only weekly TSMOM @ 0.5% risk.**
- Universe (17): GLD·SLV·CPER / SPY·QQQ·DIA·IWM / IEF·TLT·SHY / HYG·TIP·EFA·EEM·DBC·VNQ·**PFF**
  (EMB dropped — redundant vs HYG+TLT).
- 5-week horizon (~3.3wk avg hold), fixed ATR stop + 3:1 RR target, equal-risk sizing (risk
  parity), no vol-targeting. ~32-33 trades/yr. Quality (risk-independent): PF **1.52**, win 46%,
  expR +0.27R, DSR 100%.

**Headline backtest @ 0.5% risk (33.4y longweekly) — the numbers to plan around:**

| Period | CAGR | Max DD |
|---|---|---|
| **Full history ~33y** (HONEST ANCHOR) | **+4.4–4.7%** | **−10.5 to −11%** |
| Recent ~13y (OOS — bull-flattered) | +10.0% | −6.5% |
| Older ~20y (IS — weak regime) | +2.4% | −10.5% |

→ **Plan around ~4–7% CAGR / ~−11% worst-case DD.** The +10% is the OPTIMISTIC recent case,
NOT the all-history baseline. (Common confusion: "10%" = recent OOS, not full history.)

**Risk dial (full-history; pure leverage — CAGR/DD ratio constant ~0.42):**

| Risk/trade | CAGR (full) | Max DD | (recent-OOS CAGR ≈ 2×) |
|---|---|---|---|
| 0.25% | +2.2% | −5.4% | ~+5% |
| **0.5% (LIVE)** | **+4.4%** | **−10.5%** | **~+10%** |
| 1.0% | +8.5% | −20.1% | ~+20% |

Note: a SHORTER recent-only window (non-`--longweekly`) shows OOS ~+18-19% @ 0.5% — the most
bull-flattered slice; do NOT plan around it. The 20% figure = either that, or 1% risk.

**Universe selection (longweekly OOS @ 0.5%):** 10→16 = +2.8% OOS (the big win); 16-base 1.51,
16+EMB 1.54, **16+PFF=17 ADOPTED 1.55**, 18 1.47. 16→17→18 is sampling noise → universe SATURATED.

**Everything else REJECTED (see detailed sections below):** vol-targeting (leverage trap),
SPY/VIX regime overlays, pullback entry, all dynamic exits, MR sleeve (only +edge but sub-threshold),
XSMOM, **momentum/relative-strength filter** (`--mom-filter`: top-5 by 13wk return → CAGR halves
10%→4.1%, ratio 1.54→0.91; per-trade expR ticks up +0.389 but breadth loss dominates — same failure
as pullback; 2026-06-25), VIX-sentiment, macro/flows (no data), prop-firm fit (poor). Research COMPLETE.
**Actual Sharpe ≈ 0.9** (full-history, from monthly 0.58%/2.24%) — NOT 1.5 (that's the CAGR/maxDD
ratio). Every "free-lunch overlay" pitch fails: there is no Sharpe improvement to be had here, only
the leverage dial (risk %). Breadth across uncorrelated markets is THE edge; concentration guts it.

---

## ⭐⭐ CURRENT STATE 2026-06-22 — READ THIS FIRST (supersedes older sections below)

**The platform PIVOTED from MT5 spot → IBKR futures → ETFs.** Why ETFs: the IBKR paper
account is ~US$130k, and at 0.5% risk NO futures contract sizes (even micros risk
$900-1125 > $650 budget; copper has no micro). ETFs trade in SHARES → 0.5% is expressible
on any account. Set **`UNIVERSE=etf`** (in addition to `BROKER=ib`) to trade ETFs.

**RECOMMENDED STRATEGY = 18-ETF long-only weekly TSMOM** (validated 33.4y):
GLD/SLV/CPER · SPY/QQQ/DIA/IWM · IEF/TLT/SHY · HYG · TIP · EFA/EEM · DBC · VNQ · EMB · PFF.
5-week hold (`HORIZON_DAYS=5`/`HORIZON_CAL=35`), 0.5% risk, fixed ATR-SL+RR3-TP, equal-RISK
sizing (= risk parity), no vol-targeting. Backtest: full **+4.7% CAGR / −11% DD**, **OOS
+10.3% / −7.0%**, expR +0.277, PF 1.54, DSR 100%, **~32 trades/yr, avg hold 3.3wk**.
Honest forward ≈ **4–7% CAGR / ~−11% DD** (full-period is the anchor; OOS is bull-flattered).

**Research is SATURATED.** ~13 dimensions tested; the ONLY thing that ever improved the
strategy is **adding uncorrelated positive-edge markets** (the ETF universe expansion:
10→16 was +2.8% OOS, 16→18 was +0.6%/flat — see EMB/PFF isolation table below). ALL rejected (with data): wider futures classes,
vol-targeting (LEVERAGE DIAL, no edge at ANY cap — closed 2026-06-23. @3x cap = TRAP: OOS @8%
+20%/-11.5% but FULL DD explodes -10.5%→-27% as it levers into vol spikes. @1.5x cap removes
the blowup but ratio is IDENTICAL to fixed — full 0.42=0.42, OOS 1.55≈1.54 — i.e. it just
scales CAGR+DD ~1.5x. Equivalent to raising base risk 0.5%→0.75%; the machinery adds nothing
over a static risk bump. If more return wanted, bump risk%, don't vol-target), dynamic exits (CLOSED across 1.5-4xATR 2026-06-23: breakeven 0.52 / pure-trail-2R 0.70 / STRUCT
−0.04 all < fixed 0.81 CAGR/DD; vol-trail chandelier: 3-4xATR ≡ fixed (too wide to bind before
RR3 TP), and TIGHTER binds but is STRICTLY WORSE — 1.5x = expR 0.259/DD -11.0/ratio 0.62, 2x =
0.79; cutting winners loses, as TF theory predicts), pullback entry (--pullback, 2026-06-23:
wait <=2wk for retrace to within 2% of 20wk MA else skip. expR UNCHANGED 0.357→0.351 & win 49%→49%
= NO entry-timing alpha; but drops 58% of signals 1118→465 — the non-retracing breakouts are the
strongest runners — so OOS CAGR collapses 9.9%→3.7%, ratio 1.52→0.82. DD "improves" only via idle
cash. Classic miss-the-runners failure), shorter
horizons (4–6wk plateau), shorts (net-negative→long-only), concentrated (no-op — de-corr
buckets empty for futures+ETF), tail-risk circuit breaker (kills CAGR, no DD help),
class-weighting (worse OOS DD), SPY-regime overlay (hurts diversified book), VIX-regime size
ladder (2026-06-23 --vix-regime: +10.1%/-6.5%→+8.4%/-7.7%, worse CAGR AND worse DD despite
cutting exposure 15% — VIX is coincident/lagging, trend filter already de-risks endogenously;
same verdict as SPY-regime → regime overlays are redundant on a long-only TSMOM book), ADX
(halves return), batch-2 ETFs (sectors/intl-subsets/extra-commodities all redundant; kept only
EMB+PFF, EMB later dropped — see below). **XSMOM predicted to fail** here (18 clustered ETFs → collapses to the rejected
class-momentum; needs 100+ names = idiosyncratic risk we reject). Score on yfinance
(=F/ETF tickers, fast, = backtest data); IBKR for EXECUTION only.

### EMB/PFF single-ETF isolation — Route 1 saturation CONFIRMED at fine granularity (2026-06-23)
Tested the open question "is it EMB or PFF that adds value, or does one drag?" by running
each alone vs the 16-base and 18-both. 33.4y longweekly, OOS = last 40%:
`BROKER=ib UNIVERSE=etf uv run --no-sync python -m dashboard.research.backtest --etf-screen --longweekly --classes metal,index,rate,credit,inflation,intl_eq,commodity,reit[,em_bond][,preferred]`
(NB: must be `--etf-screen` + `UNIVERSE=etf` so the class guard knows the candidate classes;
class names are SINGULAR/exact — `intl_eq`, `em_bond`, `preferred`.)

| config | OOS CAGR | OOS maxDD | CAGR/DD | OOS expR | full PF |
|---|---|---|---|---|---|
| 16-base | +9.8% | −6.5% | 1.51 | +0.366 | 1.52 |
| 16 + EMB | +10.0% | −6.5% | 1.54 | +0.357 | 1.53 |
| 16 + PFF | +10.1% | −6.5% | 1.55 | +0.363 | 1.53 |
| 18 (both) | +10.3% | −7.0% | 1.47 | +0.355 | 1.54 |

**Conclusion:** EMB and PFF EACH add only ~+0.2–0.3% OOS CAGR alone (CAGR/DD 1.51→1.54/1.55,
~2% relative — nowhere near a ≥10% bar). Neither clears the threshold individually → the
"maybe EMB alone is worth it" hypothesis is REFUTED. DD only worsens (−6.5→−7.0) when BOTH
are added; 18-both has the WORST OOS CAGR/DD (1.47) yet the best full-sample expR/PF/totalR.
At n≈1100 trades all four differ within sampling noise (expR 0.355–0.366; the DD gap = one
drawdown event). **Route 1 is saturated even at single-ETF granularity — 16/17/18 is a
noise-level choice.** DSR non-discriminating (n_trials=1, correct for a single-config tool).

**FINAL DECISION 2026-06-23 — adopt 17 ETFs (16 + PFF, drop EMB)** (supersedes the earlier
"use 18-both" line above). Reasoning, not a noise-chase: EMB (em_bond) is mechanically
redundant — ≈ HYG credit + TLT duration, both already held — and it is the only candidate that
drags the risk-adjusted ratio in BOTH windows (recent 3.44→3.28; full-history adds it onto
16+PFF as 1.55→1.47 via DD −6.5→−7.0). PFF (preferred) is a more distinct exposure and holds
the best CAGR/DD ratio in both windows. The 16/17/18 *return* gap is sampling noise; the call
is made on redundancy + consistent risk-adjusted direction, so 17 is the **cleanest reasonable**
universe, NOT a claim it is statistically optimal. Fully reversible: EMB still defined in
instruments.py; live drop is one line in `paper._default_trend_classes()` (removed `em_bond`).
Re-test if a major EM-credit dislocation plausibly gives EMB a distinct (non-redundant) edge.

**Honest live expectation (size to the downside, not the OOS):** OOS (favorable regime) ≈
+10% CAGR / −6.5% DD; but full-history IS (unfavorable regime) ≈ **+2% CAGR / −10.7% DD** at
0.5% risk — and OOS>IS means the strategy is NOT overfit, so the +2%/−11% case is a real
multi-year scenario, not a tail. If a −10–11% drawdown is unacceptable, run **0.25% risk**
(≈ −5–6% DD, ~half the CAGR). Pick the risk % against the IS downside before cutover.

### Paper-trading monitoring — judge on n, NOT a 3-month CAGR ratio
A "3-month live CAGR > 80% of backtest → continue / < 50% → stop" rule is statistically
invalid here: 3 months ≈ 8 trades (~1 trade/2wk, 46% win) — far too few to distinguish a
working strategy from bad luck; it would tempt killing a fine strategy or trusting noise.
First months: monitor OPERATIONAL only (fills / auto-roll / sizing execute correctly;
realized per-trade R distribution + equity vol consistent with backtest). Verdict on edge
needs **n≥30 trades ≈ 1yr+** via the broker-truth retrospective (as elsewhere in this doc).

**Objective stop/review tripwires (use these, NOT a live-CAGR-vs-backtest ratio).** Annualized
return at n≈30 has a CI wider than any 50–80%-of-backtest band, so a CAGR-ratio gate just
flips coins. Defensible triggers instead:
- **Drawdown breach:** realized equity DD exceeds the IS worst case with buffer — **> −13%**
  (IS −10.7% + ~20%) → halt new entries, review sizing/execution. Regime-independent.
- **Edge sign at n≥30:** if realized per-trade expR ≤ 0 once n≥30 (broker-truth, costs in R)
  → the edge has not shown up; stop and investigate. (Backtest expR ≈ +0.36 OOS / +0.17 IS;
  the honest fail test is *sign*, not magnitude.)
- **Slippage:** if realized half-spread/fills materially exceed the modeled cost → re-cost,
  don't blame the strategy.
- **0.25%-risk return context** (if chosen for the −5–6% DD): sizing is ~linear in risk %, so
  expect ≈ half the CAGR (~+5% OOS / ~+1% IS). At +1% IS the question "is it worth trading?"
  is fair — that's the price of capping DD at −5–6%.

**EMB re-add trigger (objective, no spread-tracking):** re-test (do NOT auto-add) if EMB
outperforms HYG by **>5% over any rolling 12-month window** — i.e. it has decoupled from the
HYG+TLT blend it's currently redundant to. Outperformance is only a *re-test* signal; only re-add
if a fresh isolation run clears the bar. Checkable once a year, no live data feed needed.

### Research status — THIS strategy is closed; the search space is not
Weekly long-only TSMOM on this ETF universe is **saturated** (every overlay + the universe
sweep tested; only universe-expansion ever helped, now exhausted at single-ETF granularity).
More precisely: the **price-technical** search is closed (everything derivable from OHLC + TA
on this universe). Parked future directions (revisit only after paper-trading yields broker-truth
data — do NOT pre-emptively re-open):
- **Cross-sectional momentum** — needs a much larger, less-clustered universe (18 correlated
  ETFs collapse it to the already-rejected class-rotation; predicted to fail here).
- **External / alt-data** — a different data infrastructure, not a price overlay:
  - **VIX sentiment "buy the fear"** — TESTED 2026-06-24 (`--vix-entry`, VIX_CLASS env to pick the
    bought class). REJECTED across ALL flavors: index (long SPY/QQQ on ^VIX≥28) = IS/OOS instability
    expR −0.013 IS vs +0.544 OOS (QE-era recency luck, not stable); metal (GLD/SLV) +0.199 weak;
    rate (TLT/IEF) +0.157 weak w/ −11.9% DD. All far below trend & the MR sleeve (+0.451). Since CNN
    Fear&Greed is ~a VIX proxy (built from VIX+put/call+momentum), this also stands in for the doc's
    untestable "F&G<20→TLT" — no edge. (--vix-regime size overlay was already separately rejected.)
  - **Macro surprise** (CPI/GDP/NFP actual-vs-consensus), **ETF fund-flows** — UNTESTED, and NOT
    testable in the current stack: both need a new external data feed (economic-calendar surprises;
    issuer creation/redemption data) we don't have. A separate data-acquisition project, not a quick
    backtest. Cited "macro Sharpe 1.6" etc. are cherry-picked from unrelated universes — not evidence.
- **Finer regime-dependent sizing** — prior coarse version was rejected; a better-specified
  one is untested but high overfit risk on this sample.
- **Multi-strategy blend** — TESTED 2026-06-23 (mean-reversion sleeve, `--meanrev`/`--meanrev-blend`).
  THE one positive-edge discovery: long-only oversold reversion (z≤−2 vs 20wk MA, gated ADX<20,
  TP at the mean) has a REAL standalone OOS edge — expR **+0.451** (> trend's +0.357), PF 1.75,
  holds OOS (IS +0.523→OOS +0.451), DSR 100%. BUT blending it does NOT improve the frontier:
  trend+MR = OOS +11.7%/−8.2% (ratio 1.43) vs trend +9.9%/−6.5% (1.52). It raises CAGR but raises
  DD MORE → ratio falls. Matched-risk kill-test: scaling trend to −8.2% DD (×1.26) gives ≈12.5%
  CAGR > the blend's 11.7% — i.e. **just sizing the core up dominates adding MR.** Cause: MR buys
  dips = adds long exposure during stress, stacking on the trend book; both long → tail-correlated,
  so MR's low standalone Sharpe (OOS 0.27) can't diversify a higher-Sharpe book at full size.
  RISK-BUDGETED blend (--meanrev-budget, size MR down) TESTED 2026-06-23: ratio climbs monotonically
  as MR shrinks — full 1.43, 0.5x 1.52, 0.33x **1.54** (just above trend-alone 1.52; matched-DD the
  0.33x blend ~ties/edges trend scaled to -6.8% DD). So a SMALL MR sleeve is frontier-neutral-to-
  marginally-positive — it stops hurting and adds a sliver of diversification. BUT +1.4% relative
  ratio / +0.6pp CAGR is an order of magnitude under the ≥10% adoption bar, inside noise, with mild
  budget-selection risk. VERDICT: not adopted — sub-threshold, and not worth a second counter-trend
  order type in live execution for ~half a point of CAGR. The 17-ETF trend book remains the answer.

### Prop-firm fit (The5ers Bootcamp) — ANALYSED 2026-06-25, not worth the time
Question: trade this strategy on a 3-step funded-trader challenge (rules: +6% target per step,
**5% static max-loss** from step start, no time limit, ~$95, FX/index-CFD/metal-CFD on MT5 —
NO ETFs). Verdict: **poor fit; cheap one-off gamble at best, not a time investment.**
- **Universe mismatch:** only ~7 of 17 ETFs map to CFDs (index→US500/NAS100/US30/US2000,
  metal→XAU/XAG, commodity). The bonds/credit/intl/REIT diversifiers that DRIVE the edge aren't
  CFD-tradeable. That subset (index+metal+commodity, longweekly) = **+2.5% full CAGR / −10.5% DD**
  — half the return, same bad DD → breaches a 5% limit.
- **Gold-only** is the strongest single CFD market (GLD weekly trend: mean **+0.49R**, 52% win,
  ~3.2 trades/yr, 69 trades/21.6y). Monte-Carlo barrier pass-prob (`dashboard/research/prop_passprob.py`,
  bootstrap, static 6%-before-5%): high odds only at impractical timescales —
  0.5% risk 98%/~21y, 1% 79%(57% w/ swap)/~9y, 2% 46%/~3.8y, 3% 31%/~1.9y, 5% 12%/~0.9y. The
  pass-rate-vs-TIME trap (gold trades only ~3x/yr) is the killer; realistic ~2% = ~1-in-3 over ~4y.
- **Gold-only @ 1% risk projection** (real 22y path): CAGR **+1.53%**, **maxDD −11.6%**, vol 2.84%,
  **Sharpe 0.54**, 1.39x/22y — low return, lumpy, single-asset; the −11.6% DD confirms it can breach
  the 5% floor (~21% of steps). Era-risk: +0.49R comes from gold's 2004-25 secular bull, may not persist.
- Caveats stacking against: CFD overnight financing on 3-wk holds (unmodeled, ~0.15R haircut drops
  1% pass 79%→57%), funded stage tightens to 4% DD (harder to KEEP than to pass), single-asset regime risk.
- Conclusion: the strategy is built to COMPOUND a real account over years (Sharpe ~1.5 on the full
  ETF book), not to sprint a +6%/−5% barrier. Diversifying the CFD basket trades frequency for
  cluster-DD risk. Keep the edge on the IBKR account; don't invest research time in prop challenges.

**ETF execution path BUILT + live-verified** (except an actual fill, which awaits a signal):
`contracts.size_shares`, `ib_exec._place_etf_bracket` (SMART Stock bracket), routes ETF vs
futures, `ib_client.stock_contract`/`fx_to_usd`. **Currency bug FIXED** — `_equity_usd`
converts NetLiq HKD→USD (delayed FX + HKD-peg 7.8 fallback; verified equity_usd=$129,777),
used by both paths.

**Ops:** Gateway auto-starts+auto-logs-in via IBC (`C:\IBC`, port 4002, password in
`C:\IBC\config.ini`, NOT in repo); `C:\Scripts\dashboard.ps1` (DashboardApp task) runs a
background monitor that relaunches it HIDDEN (`C:\IBC\start_hidden.vbs`) if 4002 dies.
`ib_async`+`MetaTrader5` are first-class deps. CME real-time data NOT activated (delayed
data is fine for a weekly system).

**NEXT (user prefers continued research — don't push "stop"):**
1. **Go live on ETFs**: set `UNIVERSE=etf` in `analyst/.env`, restart `DashboardApp` →
   dashboard trades the 18 ETFs (now sizeable). Then paper-trade months = highest-value step.
2. OR **test XSMOM** to confirm/refute the prediction it fails on this universe.
3. **UNCOMMITTED** since commit `bf7e45e` (user commits/pushes themselves): instruments.py,
   paper.py, backtest.py, contracts.py, ib_client.py, ib_exec.py — the ETF research+exec.
   Live `.env` is still `BROKER=ib` (futures, 10 mkts); NOT yet `UNIVERSE=etf`.

---

## TL;DR (read this first)

A research + live-demo trading platform. After exhaustive out-of-sample,
deflated-Sharpe-penalised testing, **exactly one strategy has a real edge:**

> **Weekly trend-following (time-series momentum) in commodities + equity indices.**
> OOS +0.11–0.15 R/trade, deflated Sharpe ~100%, ~+3% CAGR @0.5% risk, ~−9% max DD.
> = the published TSMOM result (Moskowitz-Ooi-Pedersen). Modest but genuine.

Everything else tested **failed**: daily trend, daily mean-reversion, FX mean-reversion
(recent-regime only), ADX-on-weekly, order-flow/Volume-Profile (infeasible on spot data).

The live system (`dashboard/`, MT5 demo) now trades this weekly strategy. **Next phase:
move to IBKR futures** (see "Next phase" below) — that's the user's stated direction.

---

## What was done this session (chronological-ish)

1. Fixed many live bugs: MT5 tick-timestamp overflow; **server-offset bug** (trades
   phantom-stopped on pre-entry ticks — now derives offset from tick data);
   **broker-truth resolution** (resolve paper trade from broker's actual closing deal +
   round SL/TP to symbol digits — fixes paper/MT5 mismatch); MT5 **attach-only**
   (don't re-login on startup, preserves manual access-point pick).
2. Built objective gates: `confidence_model.py` (empirical edge per strength×vol regime)
   and `win_model.py` (calibrated P(win), logistic+isotonic, pure numpy) — replaced the
   LLM's self-reported confidence for gating.
3. Expanded universe to 31 instruments; added overextension filter (skip long RSI>70 /
   short RSI<30 — validated); set MIN_STRENGTH=5; retired VOL_FILTER.
4. **Ran a full backtest battery** → conclusion above. Key runs in `backtest.py`
   (portfolio sim) + `ab_*.py` A/B harnesses.
5. **PIVOTED to weekly** (the validated edge): signals on W1 bars, ~7-week hold,
   commodities+indices only. Rebuilt both models on weekly data.
6. UI: per-instrument sparklines, gate-status table (hides WAIT/WATCH), configurable
   refresh/columns/overext-band/risk%, Restart button, broker-truth retrospective.

---

## Current live system

- **Run/restart:** Windows scheduled task **"DashboardApp"** runs `C:\Scripts\dashboard.ps1`
  (watchdog: loops `python -m dashboard.app`, relaunches on exit). App serves
  http://localhost:8080. The python process is PRIVILEGED — a normal shell can't kill it.
  **To restart:** `Stop-ScheduledTask DashboardApp; Start-ScheduledTask DashboardApp`,
  OR click the **Restart** button in the dashboard header (exits → watchdog relaunches ~10s).
- **Strategy config (`dashboard/paper.py`):** `MIN_STRENGTH=5`, `OVEREXT_FILTER=True`
  (70/30), `VOL_FILTER=False`, `MIN_EDGE_R=0`, `RR_DEFAULT=3.0`, `SL_ATR_MULT=1.5`,
  `RISK_PER_TRADE=0.005`, `HORIZON_CAL=49` (~7wk), `WEEKLY_TREND_CLASSES={metal,energy,index}`,
  `DECORRELATE=True`. Many are UI-toggleable in the header.
- **Signals on weekly bars:** `providers.get_history` → MT5 W1 (320 bars). Resolution uses
  daily bars (tick fetch over a 7-week horizon is too large).
- **Execution:** `executor.py` mirrors strength-5 ATR-rr3 signals to the MT5 **demo**
  account (broker-guarded, refuses non-demo). Frequency: **~1 trade every 1–2 weeks**
  (this is correct — the edge is low-frequency; frequent trading = the no-edge daily game).
- **LLM:** light veto only (top-10 instruments, every 30min). Confidence NOT used to gate.

## Repository layout (REORGANISED 2026-06-21 into concern-based subpackages)
The package was refactored from a flat `dashboard/` into subpackages. Imports are
absolute (`dashboard.<subpkg>.<module>`). **Entrypoint UNCHANGED:** the scheduled task
still runs `python -m dashboard.app` (`app.py` deliberately kept at the package root).
- `dashboard/core/` — paper, scoring, journal, store, log, net
- `dashboard/data/` — providers, mt5_client, ib_client, contracts
- `dashboard/execution/` — executor, ib_exec, broker, link_monitor
- `dashboard/models/` — confidence_model, win_model (+ their trained `.json`, kept here
  in VC — NOT moved to artifacts/, since they're committed trained models)
- `dashboard/research/` — backtest, optimize, replay, wide_search, structure, ab_*
- `dashboard/web/` — service, report, board_scan, news_sources, retrospective
- `dashboard/` (root) — app.py (entrypoint), instruments.py
- `dashboard/tests/` — test_contracts
- `artifacts/` (repo root, gitignored) — generated `*.pkl` datasets (replay caches)
Moved-CLI paths: `python -m dashboard.data.ib_client` (diagnose),
`dashboard.execution.ib_exec`, `dashboard.research.backtest|replay|optimize`,
`dashboard.models.confidence_model --build`, `dashboard.tests.test_contracts`.

## Research findings table (all OOS + deflated-Sharpe)
| strategy | verdict |
|---|---|
| Daily trend / mean-reversion | ❌ no edge (DSR 53–58%, ~breakeven, −24% DD) |
| FX weekly mean-reversion | ❌ recent-regime only (negative 2000–2013) |
| ADX regime filter | helps DAILY (+38%), HURTS weekly (don't use on weekly) |
| Order-flow / Volume-Profile | ❌ infeasible on spot FX/CFD (fake tick-volume) |
| **Weekly trend, commodities+indices** | ✅ persistent, broad, OOS DSR ~100% |
| Overextension filter (RSI 70/30) | ✅ validated, in use |

Asset-class breadth (weekly): metals (gold +0.50R, silver +0.34R) and indices (SPX +0.28R,
Nikkei +0.19R) trend; **FX is negative** (mean-reverts) → excluded from the trend strategy.

---

## Next phase: MOVE TO IBKR FUTURES (user's stated direction)

User is switching to **Interactive Brokers + Paper Trading**, wants to maximise profit at
low risk via futures. Full analysis was given; the honest plan:

**Key reframe:** leverage ≠ profit. Futures help via (1) **real volume/order-flow** (the
order-flow approaches infeasible on spot become *researchable*), (2) **access to many
uncorrelated trending markets** (the #1 lever), (3) **micro contracts** for precise risk
sizing, (4) lower costs. The validated edge ALREADY lives on futures data (GC=F, CL=F, ES/NQ
track our indices), so the strategy ports directly.

**The plan (maximize profit at controlled risk):**
1. **Diversify across ~15–25 uncorrelated futures** (the biggest safe gain): indices
   (ES/NQ/YM/RTY), metals (GC/SI/HG), energy (CL/NG), **rates (ZN/ZB/ZF — uncorrelated
   with equities, key diversifier)**, grains (ZC/ZW/ZS), softs (KC/SB/CT), FX-futures
   (6E/6J/6A). More uncorrelated trending bets → higher Sharpe → more profit per unit DD.
2. **Volatility-target the portfolio** (~10–12% annual vol).
3. **Size by RISK, never margin**: contracts = (account×0.5–1%) ÷ (ATR-stop × $/point).
   Use **micros** (MES/MNQ/MGC…) for precision on a modest account.
4. Risk controls: per-trade 0.5–1%, sector/cluster limits, daily-loss circuit breaker,
   **contract-roll discipline** (futures expire — roll front month before expiry; classic bug).
5. **Order flow** (now feasible on futures) = research avenue to VALIDATE later (intraday,
   unproven), NOT part of the core plan.

**Honest expectations:** diversified futures TSMOM → Sharpe ~0.5–0.8 → ~+8–15% annual at
~15% vol, ~15–20% max DD *if the edge holds live*. NOT a moonshot.

**Engineering to do (the real work):**
- New **IBKR provider** (`ib_insync`/`ib_async`) replacing MT5: data + paper execution.
- **Contract-roll logic** + per-contract specs (multiplier/$ per point, tick, margin) for sizing.
- Expand universe to the futures list above. The strategy/research code (scoring, backtest,
  gates, retrospective) **ports on top** — only the data/execution layer changes.
- IBKR needs (paid) real-time market-data subscriptions per exchange even on paper.

---

## IBKR futures layer — progress (2026-06-21)

Scope doc: **`IBKR_SCOPE.md`** (full design — read it first for this track). Build is
following its §5 order; **steps 1–6 done offline, MT5 untouched and still the default
(`BROKER` env var: unset/`mt5` = proven live path; `ib` = futures path).**

- ✅ `dashboard/contracts.py` — `FutureSpec` table (28 contracts incl. micros: ES/MES,
  NQ/MNQ, GC/MGC, CL/MCL, ZN/ZB/ZF rates, grains, softs, 6E/6J/6A); **pure** sizing
  `size_contracts`/`choose_contract` (risk-based, floor, micro-fallback, skip-if-too-big)
  + roll math (`needs_roll`, business-day counter). Shared by ib_exec AND backtest.
- ✅ `dashboard/ib_client.py` — `ib_async` connection (degrades gracefully like
  mt5_client: None/False when no gateway), `continuous_rates` (CONTFUT, for signals),
  `get_rates` (dated front month, for resolution), `get_tick`, `front_future`,
  paper guard data (`is_paper`/`account_id`, DU-prefix), `contract_check`, `diagnose()` CLI.
- ✅ `dashboard/test_contracts.py` — pure-math unit tests, **all pass**
  (`uv run python -m dashboard.tests.test_contracts`).
- ✅ `pyproject.toml` — added `ib` extra (`uv sync --extra ib`).
- ✅ `providers.py` (step 4) — BROKER dispatch. `BROKER=ib` routes get_history →
  CONTINUOUS weekly (signals), get_ohlc → DATED FRONT MONTH (resolution), get_live_price
  → IB tick; yfinance stays the fallback for both brokers. Verified: `BROKER=ib` with no
  gateway falls back to yfinance ES=F (418 weekly bars).
- ✅ `instruments.py` — `FUTURES_UNIVERSE` (21 full-size markets; micros excluded — they're
  execution vehicles picked by choose_contract). `active_universe()`/`active_by_key()` flip
  with BROKER. `_FUT_YF` maps each to its `=F` continuous ticker for fallback.
- ✅ `ib_exec.py` (step 5) — paper execution mirroring executor.py's surface: paper guard
  (DU-prefix + paper port, refuses otherwise), `mirror_new` (front month + size-by-specs +
  bracket order), `sync_closures` (broker-truth resolve + roll-on-expiry), `live_positions`,
  `reconcile`. Own `ib_mirror` sqlite table. CLI: `uv run python -m dashboard.execution.ib_exec`.
- ✅ `broker.py` (step 6) — BROKER dispatch shim (executor | ib_exec); `service.py` calls
  `broker.mirror_new/sync_closures/live_positions`. `paper.py` SL/TP rounding + instrument
  lookups + resolution made broker-aware (futures round to contract tick, not MT5 digits).
- Verified no regression: default → executor + 31-instrument universe; `BROKER=ib` →
  ib_exec + 21 futures. Both import and run clean; pure-math tests pass.

**LIVE-VERIFIED 2026-06-21** against IB Gateway paper (port 4002, account DUK968178):
- ✅ Connectivity + paper guard (`is_paper`=True). ✅ SPECS cross-check passes (MES/GC/ZN
  multiplier+tick match broker). ✅ Front-month resolves (GCN6/ESU6, live prices sane).
  ✅ `continuous_rates` weekly works through the full provider path (`source=ib`): GC 418wk,
  ES 223wk. Continuous-vs-front split confirmed (GC continuous 4245.9 vs front 4229.3 =
  back-adjustment offset, as expected).
- 🐞 **Bug found+fixed during verification** (`ib_client.py`): weekly `durationStr` was built
  as "60 W" → IB Error 366; must be expressed in YEARS. Also ContFuture now `qualifyContracts`
  first (bare ContFuture has no conId → 366). Install ib_async with `uv pip install ib_async`
  (a full `uv sync --extra ib` fails while the live dashboard locks MetaTrader5's .pyd).
  **IMPORTANT: run IB commands with `uv run --no-sync ...`** — a plain `uv run` re-syncs
  the env to the default deps and STRIPS the pip-installed ib_async (the `ib` extra can't be
  synced while MT5 is locked). e.g. `IB_CLIENT_ID=9 uv run --no-sync python -m dashboard.data.ib_client`.
- ⚠️ IB ContFuture history is SHALLOW for newer contracts (ZN ~132wk < the 200-bar signal
  threshold) → those fall back to yfinance for signals. Fine, but uneven; revisit if it matters.
- ⚠️ clientId collisions are real: a lingering prior connection holds clientId 7 (Error 326).
  Ensure `ib_client.shutdown()` on exit; use a distinct IB_CLIENT_ID for ad-hoc probes.

### ⭐ LOCKED STRATEGY SPEC (2026-06-21) — research closed, do not re-tune
**Weekly TSMOM on IBKR futures, LONG-ONLY.** Universe `{metal, index, rate}` (BROKER=ib
default). Config: `MIN_STRENGTH=5`, `OVEREXT_FILTER` 70/30, `RR_DEFAULT=3.0`,
`SL_ATR_MULT=1.5`, `RISK_PER_TRADE=0.005`, **`HORIZON_DAYS=5`/`HORIZON_CAL=35` (5wk)**,
**`LONG_ONLY=True` under ib** (short side is net-negative on up-drifting index/metal
futures), no vol targeting. **~25 trades/yr ≈ one every 2–3 weeks** (per market ~2–3/yr).
Final long-only backtest (26.4y, 0.5%): full **+3.6% CAGR / −9.3% DD**, expR **+0.297**,
PF **1.57**, win 45%, DSR 100%. IS +0.236 expR /+3.1% CAGR/−9.3%; OOS +0.415/+6.9%/−5.4%.
(Long-only beats long+short on expR/PF/win/DD; full CAGR same 3.6%; per-trade quality up.)
**HONEST P6 EXPECTATION = ~4–7% CAGR / ~−9% DD** (full-period 3.6% is the conservative
anchor; recent OOS ~6.9% was trend-friendly). Expect 1–2yr flat/drawdown stretches — NORMAL.
Tested & rejected: wider classes (grain/soft/fx/energy dilute), vol-targeting (pure
leverage), horizons 1–8wk (4–6wk flat plateau; 5wk fine), **short side (net-negative,
−0.082 expR — dropped → long-only)**,
**exit methods on the current config** (`--exit-test`, comprehensive): breakeven, pure
trailing, arm-gated trailing, VOL-ADAPTIVE trailing (3-4xATR), and STRUCT SL/TP placement
ALL tested. Fixed ATR-SL+RR3-TP+5wk WINS. STRUCT = catastrophic (OOS expR -0.581, loses
money). Vol-trail @3-4xATR = IDENTICAL to fixed (never binds on a 5wk hold); tighten it to
bind and it cuts winners (pure trail 2R → +0.236). No trail width helps. breakeven@1R is a
lower-DD/lower-return lever (only if DD-control ever outranks CAGR), not adopted. Exits fully
closed — no dynamic exit beats fixed on this universe.

### Futures research CONCLUDED 2026-06-21 (universe + sizing locked)
Ran a 7-combo OOS class battery + vol-targeting test on 26.4y yfinance `=F` history
(`backtest.py --longweekly --classes ... [--voltarget]`). Findings:
- **Universe = `{metal, index, rate}`** (now the `BROKER=ib` default in
  `paper._default_trend_classes`). OOS **+7.4% CAGR @ −6.6% DD** — best risk-adjusted.
  Per-class OOS expR: metal +0.391, index +0.166, **rate +0.085** (the one genuine
  diversifier — uncorrelated, lifts CAGR at flat DD). **ENERGY is dead weight**
  (drops OOS expR +0.345→+0.281; metal,index alone beats metal,energy,index).
  **REJECTED: grain (−0.133, ZC −0.253), fx (−0.086), soft as a class** (KC +0.243
  is good but the class drags in CT/SB; can't cherry-pick KC without snooping).
  Naive "wide/all" HALVES the edge (expR +0.099) — diversification ≠ "add everything".
- **Vol targeting @12% = FAIL** (pre-registered criteria). Tripled CAGR AND DD
  (full 3.6%/−9.9% → 9.2%/−27.2%); CAGR/DD ratio FLAT (0.36→0.34). It's just ~2.7x
  leverage, no risk-adjusted gain → ABANDONED. Strategy is already as smooth as the
  edge allows; run fixed 0.5% risk. (`--voltarget` flag kept as a tool, off by default.)
- DSR shows 100% for every combo because `deflated_sharpe_ratio(..., n_trials=1)` is
  hardcoded — it's NON-discriminating here; judge on OOS expR + DD, not DSR.
- MT5/spot universe UNCHANGED (`{metal,energy,index}`) — it has no rate futures and
  silently dropping energy there would be an unvalidated live change.

### 🔴 CRITICAL — account too small to trade + a currency bug (found 2026-06-22)
The IBKR paper account is **~1,012,000 HKD ≈ US$130k**. At `RISK_PER_TRADE=0.005` the
budget is ~**US$650/trade**, but ONE contract risks far more, so **NOTHING sizes → zero IBKR
orders placed** (`ib_mirror` empty). Examples (1.5×ATR stop): HG (copper) **$6,750**, GC
**$9,000**, ES **$11,250**, ZN **$1,500** — and even the MICROS exceed the budget (MGC ~$900,
MES ~$1,125). That's why an HG paper trade shows in the journal (notional sizing off
ACCOUNT=$10k) but is NOT on IBKR: `choose_contract` returns 0 (HG has no micro at all).
**Two must-fix-before-first-real-order items:**
1. **Account size / risk**: either (a) raise the IBKR **paper account to ~US$1M** (free, reset
   in IBKR account mgmt — and ideally USD-denominated), or (b) raise `RISK_PER_TRADE` so
   0.5–1% ≥ a micro's risk. On $130k @0.5% the strategy literally can't place a trade. HG is
   the worst case (no micro; needs ~$1.35M @0.5% for 1 contract) — consider dropping HG.
2. **🐞 CURRENCY MISMATCH (safety bug)**: `ib_exec._equity` returns NetLiquidation in the
   account ccy (**HKD**), but `contracts.choose_contract`/`risk_per_contract` compute risk in
   the contract ccy (**USD**) — no conversion. Currently masked (everything sizes to 0), but if
   the account is enlarged this **oversizes ~7.8×** (HKD number treated as USD). MUST convert
   equity→contract-ccy (fetch USD.HKD fx) before sizing, OR refuse when acct ccy≠USD.

### Dashboard instrument count / scope (2026-06-22)
- Board shows **10** futures (ES/NQ/YM/RTY, GC/SI/HG, ZN/ZB/ZF) — NORMAL: `active_universe()`
  filters FUTURES_UNIVERSE(21) to the traded `WEEKLY_TREND_CLASSES={metal,index,rate}`. The
  rejected grain/soft/fx aren't shown. (MT5 mode showed 31 spot instruments — different set.)
- **Funds / individual stocks: NOT recommended.** The validated edge is weekly TSMOM on
  *futures* {metal,index,rate}; equity-index exposure is already covered (ES/NQ/YM/RTY).
  Individual stocks/funds = idiosyncratic risk + a NEW research project (own OOS/DSR). Out of
  scope; would violate "stop researching, start executing".

### ✅ P6 CUTOVER WORKING 2026-06-21 (corrected) — root cause was MT5, not IB threading
The dashboard runs LIVE on `BROKER=ib`: board scores the 10 traded futures (ES/NQ/YM/RTY,
GC/SI/HG, ZN/ZB/ZF) on yfinance, IBKR connects in the refresh worker thread, full cheap
refresh completes. **The earlier "blank board / stall" was NOT the ib_async↔nicegui issue
I feared** — the refresh runs via `run.io_bound` (worker threads), where `ib_client.call`
works fine (standalone-verified). The REAL blocker was a broken MetaTrader5 package: its
`AttributeError` from `mt5_client.is_available()` (called unguarded at the top of every
refresh) aborted the whole loop silently → blank board in BOTH modes. Fixed by guarding
`mt5._ensure_init`. Also: `active_universe()` under ib now filters to WEEKLY_TREND_CLASSES
(shows exactly the traded 10, not the rejected grain/soft/fx).
Remaining proof: first REAL order via `ib_exec.mirror_new` (runs in the LLM refresh worker
thread too) — awaits the next live signal (~weekly cadence). Architecture proven; not yet
exercised with a real fill. Old "needs background-thread fix" note below is SUPERSEDED.

### P6 CUTOVER STATUS 2026-06-21 (attempted; blocked on 2 IB-integration issues) [SUPERSEDED]
- ✅ **IBC Gateway auto-login DONE**: IBC 3.24.0 at `C:\IBC` (config.ini → paper, port
  4002, ReadOnlyApi=no; password filled by user, ACL-locked). `StartGateway.bat` set for
  Gateway 1047, `CONFIG=C:\IBC\config.ini`. Startup-folder shortcut auto-starts it at logon.
  Verified: auto-logs-in, 4002 opens, diagnose sees DUK968178 paper=True.
- ✅ **ib_async threading rewrite** (`ib_client.py`): dedicated event-loop thread (`_ensure_loop`,
  `_run` for async methods, `call()` for sync ops); `ib_exec` routed through `call()`. **Verified
  STANDALONE** from a worker thread (data + exec reads + broker.connection all complete, no hang).
  Also fixed: `readonly=False` (orders were being rejected), `log.py` path (parents[2]), and a
  `set_event_loop()` in the loop thread (for the nicegui case).
- ❌ **Live cutover blocked**: under `BROKER=ib` inside the nicegui process the cheap refresh
  still stalls / shows "gateway down" (ib_async↔nicegui asyncio interaction — works standalone,
  not in-process). Rolled back to mt5 (`BROKER` commented) to keep the dashboard unstuck.
- ⚠️ **Refresh too slow even when it works**: scores all 21 futures via IB `reqHistoricalData`
  (~9s each) + `get_tick` timeouts (~6s, no mkt-data sub) ⇒ ~5min/refresh.
- ⚠️ **MT5 package regressed** ("module has no attribute initialize") → mt5 mode runs on
  yfinance, no MT5 execution. So NEITHER broker is trading right now (safe, but not live).

**DONE 2026-06-21 (kept, correct):**
- ✅ **Data-source split**: under `BROKER=ib`, `providers` SCORES on yfinance (=F weekly,
  fast, = backtest data) + yfinance for ohlc/live-price; IB is execution-only. Verified
  fast (get_history 1.4s, no IB in the data path). This removed the ~5min refresh.
- ✅ `ib_client` dedicated event-loop thread + `call()`/`_run`; `ib_exec` routed through it.
  Works from a PLAIN worker thread (standalone test passed).

**THE remaining blocker (precise):** the cheap refresh ALSO calls `broker.live_positions()`
and `broker.connection()` (IB status), and these run inside **nicegui's ui.timer callback =
nicegui's event-loop thread**, where the dedicated-loop marshalling stalls (ib_async binds to
nicegui's loop, not ours). The standalone test passed because it ran from a PLAIN thread, not
nicegui's loop. So the dashboard refresh under `BROKER=ib` still stalls / shows "gateway down".

**NEXT SESSION — the one fix that lands the cutover:**
1. **Move IB status/execution OFF the nicegui refresh onto a dedicated background thread**
   (mirror `link_monitor`, which is PROVEN to work from a plain thread): a thread that
   periodically calls `broker.live_positions()`/`broker.connection()`/`sync_closures()` and
   writes results into `service.STATE`; the nicegui refresh + UI only READ STATE. `mirror_new`
   already runs in the (threaded) LLM cycle — confirm it's a plain thread too, not the loop.
2. Restart `BROKER=ib`, confirm a completed "cheap refresh" + header shows acct DUK968178.
3. Then place ONE live signal end-to-end (the order path's first real proof).
4. (Optional) repair MetaTrader5 ("no initialize") if an MT5 fallback is wanted.
Alternative if (1) is insufficient: bind ib_async to nicegui's OWN loop (capture it at startup,
`run_coroutine_threadsafe` to it) or isolate IB in a subprocess.

**Then — P6 (the trading phase):**
1. Flip `BROKER=ib` in analyst/.env (currently commented). Place ONE live signal
   end-to-end on paper; confirm bracket + fill + reconcile. (CME real-time data NOT yet
   activated → live ticks unavailable; weekly runs on delayed/historical — acceptable.)
2. Run `BROKER=ib` paper for 3–6 months; confirm fills + auto-roll + that the live
   equity-curve vol matches the backtest. THEN judge via the broker-truth retrospective.
3. **STOP researching.** 7 combos + vol-target tested; further tinkering = overfitting.
4. **Folder reorg** (filed task) — done this session; keep entrypoint `dashboard.app`.

## How a NEW context window should continue

1. **Read this file + `README.md` + the memory** (`~/.claude/projects/D--claude/memory/
   project-quant-dashboard.md`). They're consistent; this file is the fullest.
2. **Don't re-litigate the research** — daily/mean-reversion/order-flow-on-spot are settled
   dead ends; weekly trend on commodities+indices is the one edge. Don't parameter-hunt.
3. **Two tracks the user may pick:**
   - **(a) Keep proving the MT5 weekly demo** — let it run clean (consider Archive&reset of
     the contaminated daily-era journal first), gather n≥30 weekly trades (months), then
     judge via the broker-truth retrospective. Only then scale risk (Method 1: 0.5%→1%).
   - **(b) Build the IBKR futures version** (user's stated next step) — start with the IBKR
     provider + contract-roll + sizing-by-specs module; port the strategy; expand to the
     diversified futures universe; vol-target; paper-trade.
4. **Discipline to keep:** every new idea → OOS split + deflated Sharpe (n_trials penalty);
   adopt only if it clears the bar. `backtest.py --longweekly` and the `ab_*.py` scripts are
   the templates. The user values brutal honesty over hopeful backtests.
5. **Git:** lots is uncommitted. **The USER commits/pushes themselves** — only suggest
   messages, never run commit/push. (A suggested message for the current diff is in the
   chat history; regenerate from `git status` if needed.)
6. **Ops gotchas:** restart via the scheduled task (not killing python); MT5 access point
   switched MANUALLY (API can't); models rebuilt with `--build`; free LLM key caps input
   ~4096 tokens (board_scan sends top-10 only).

---

## Open items / decisions pending
- **Whitelist (commodities+indices only) is a judgment call:** improves per-trade expR
  (+0.107→+0.149) but portfolio return ~same and DD slightly worse (lost FX diversification).
  Revert via `WEEKLY_TREND_CLASSES=set()`. (On *futures*, diversification is the whole game,
  so this concern flips — keep many uncorrelated markets there.)
- **Journal contaminated** with daily-era trades → Archive&reset for a clean weekly test.
- **Uncommitted changes** — user to commit.
