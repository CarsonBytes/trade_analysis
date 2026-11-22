# Strategy Improvement — spec (2026-11-21)

**Status:** PROPOSED, with work started 2026-11-21:
- **I3 cash shield BUILT** (configurable shield + SGOV→IB01 migration + weekly/band
  anti-churn). PAPER compose set to IB01; LIVE unchanged.
- **I1** risk-matched re-test run (§3.1).
- **Universe scan** run (§9).

Nothing deployed to LIVE. Exit numbers in §3 are from a quick
portfolio-level run in this session and must be re-run under the project's adoption bar (§1)
before anything ships.
**Companions:** [IBKR_STRATEGY_FIDELITY_SPEC.md](IBKR_STRATEGY_FIDELITY_SPEC.md) (make live trade
the backtested strategy: F1 built, F4/F3 next) and
[RETRO_PERFORMANCE_2026-11-21.md](RETRO_PERFORMANCE_2026-11-21.md) (why Jul–Nov was bad).
Fidelity comes first. There's no point improving a strategy that live doesn't actually trade.

---

## 0. Where the system stands (evidence, this session)

| | Value | Source |
|---|---|---|
| Backtest, live config (pos_cap 25%, PORTFOLIO_CAP 1.0, 1% risk, no cash yield) | **CAGR 5.1%, maxDD −7.6%, Sharpe ~0.91**, 40 trades/yr | `explore.py` (scratch) on `backtest._portfolio` |
| Same, no position cap | CAGR 11.1%, maxDD −24.7%, Sharpe ~0.92 | same |
| Signal-level edge | +0.30R/trade, 45% win, PF ~1.6, 1,020 signals since 2001 | same |
| **Skew** | **the top 10% of trades = 100% of total R** (+305.6R of +305.3R). 152 trades hit the 3R TP, 521 stopped at −1R, 347 timed out | same |
| Edge by year | lumpy: 2015 −0.48R, 2018 −0.20R, 2007 −0.17R; 2017 +0.91R, 2024 +0.67R, 2025 +0.85R | same |
| Edge by class | positive in 11 of 13 classes; negative: inflation (TIPS), preferred (PRFD), long Treasury (IDTL) | same |
| Live capital actually in the strategy | ~35–60% of NAV; the rest sat in SGOV/cash | retro §1 |
| Already tested | ~150 ideas: exits within TP3/5wk, regime filters, sizing, vol targeting, 10 ETF-screen batches, sleeves, options, pairs (inventory in §6) | HANDOFF + `research/` |

**Two structural facts drive this spec:**
1. **Risk-adjusted return is flat across position caps** (Sharpe ~0.9 at 25% cap and with no
   cap), so the cap is a leverage dial, not edge. Real improvement has to raise Sharpe or cut
   costs, not just add size.
2. **The edge is a positive-skew trend edge**, and the fixed 3R take-profit plus 5-week time
   stop cut exactly the trades that pay for everything else. Every exit test so far kept both
   caps (inventory §1, "untested #1").

## 1. Adoption bar (unchanged; the project's own rules)
A change ships only if it beats the baseline on **Full AND OOS Calmar (CAGR/maxDD)**, plus OOS
expR, and **IS CAGR/DD is not worse** (no lucky-split wins). It also needs **30 forward
trades** and **explicit user approval** (parameter freeze, HANDOFF 2026-08-06). Every number
below is a hypothesis until it clears that bar in `research/backtest.py` with the existing
walk-forward and bootstrap tools.

---

## 2. Candidate levers, ranked by (expected value × confidence) / effort

| # | Lever | Why it could work | Evidence so far | Effort | Risk |
|---|---|---|---|---|---|
| **I1** | **Let winners run** (exit redesign) | Positive-skew edge; TP3/5wk caps the right tail | §3.1: at 0.5% risk, same CAGR, Sharpe 0.91 → 1.33, bootstrap Calmar 0.63 → 1.58; **but single-path Calmar fails** (0.67 → 0.58) | M | Historical worst DD −7.6% → −9.0% |
| **I2** | **Kill the 30% US dividend withholding** (accumulating Irish UCITS share classes, including the cash shield) | Measured drag −0.88pp/yr, Calmar 0.588→0.488 (`dividend_tax_drag.py`). Live SGOV lost 30% of its distributions | Partly done: 9 UCITS swaps adopted for tax/fee | S | Equivalence of tracking; LSE hours; VNQ-type mismatches |
| **I3** | **Fix or replace the cash shield** | SGOV netted HKD 99 on ~HKD 200k (55 trades, HKD 429 commissions, HKD 241 withholding) | F6 in the fidelity spec | S | — |
| **I4** | **Idle capital: core allocation + trend overlay** | Strategy uses ~35–60% of NAV; Sharpe ~0.9 flat, so the idle half earns only T-bill | Never tested (inventory "untested #4") | M | Adds equity beta and a deeper DD; must clear Calmar, not CAGR |
| I5 | Trend-signal definition walk-forward (MA lengths, 20-bar momentum, 3% threshold, votes rule) | Never swept; 15-trial joint search is thin | = fidelity F8 | M | Overfitting; needs strict OOS |
| I6 | Drop chronic losers (TIPS, PRFD, IDTL) | Negative expR over 12/9/12 trades | Small n; diversification value | S | Noise-mining |
| I7 | Cross-sectional momentum as a *separate* sleeve | Top-N *filter* was rejected (breadth loss), but XSMOM as its own return stream was never run | HANDOFF only predicts it fails | M | Correlated with the trend book |
| I8 | Daily-bar entry timing on weekly signals | Never tested; overlaps fidelity F4 | — | S | Pullback-entry relative was rejected (misses runners) |

**Not proposed (already rejected, don't retry without new evidence):** breakeven stops,
fixed-R/ATR trailing *within* TP3/5wk, partial profits, STRUCT stops, RSI-exhaustion and
time-decay exits, pullback entries, 5d/20d return filters, the cross-sectional top-N filter,
ADX/SPY/VIX regime filters, class tilts, DD circuit/scaling, vol targeting (leverage dial),
conviction sizing, 2% risk, monthly bars, short side, inverse ETFs, options/pairs/stat-arb,
VIX-timed contributions. References are in §6.

---

## 3. I1 — Let winners run (exit redesign)

**Exploration result (this session, signal level, weekly bars, longs, all 1,020 signals):**

| Exit | expR | win% | since-2016 expR |
|---|---|---|---|
| baseline TP3 / 5wk | +0.304 | 45% | +0.409 |
| TP3 / 10wk | +0.334 | 38% | +0.470 |
| no TP / 13wk | +0.398 | 31% | +0.530 |
| **TP5 / 13wk** | **+0.493** | 34% | **+0.661** |
| **chandelier 3×ATR, no TP, 26wk** | **+0.473** | 35% | **+0.640** |
| chandelier 2×ATR, no TP, 26wk | +0.320 | 38% | +0.457 |
| TP3 + breakeven@1R / 5wk | +0.265 | 36% | +0.373 (matches the earlier rejection) |

Only 16% of losers ever reached +1R before stopping, so tighter protection buys little. The
gain comes from the right tail.

**Portfolio level** (`explore2.py`, scratch). This re-generates the signals with each exit, so
the one-trade-per-instrument walk, PORTFOLIO_CAP and capital tie-up are honest. 1% risk, no
cash yield. IS = before 2016, OOS = 2016 onward. Calmar = CAGR / |maxDD|.

| Exit (pos_cap 25%, live) | IS expR | IS CAGR / DD | **IS Calmar** | OOS expR | OOS CAGR / DD | **OOS Calmar** | OOS Sharpe | trades/yr |
|---|---|---|---|---|---|---|---|---|
| baseline TP3 / 5wk | +0.078 | 1.64% / −7.5% | 0.22 | +0.404 | 10.05% / −7.5% | **1.34** | 1.30 | 64 |
| TP3 / 10wk | +0.113 | 1.48% / −7.6% | 0.19 | +0.504 | 9.82% / −10.5% | 0.94 | 1.34 | 54 |
| TP5 / 13wk | +0.278 | 2.17% / −6.9% | **0.31** | +0.779 | 10.90% / −16.3% | 0.67 | 1.44 | 43 |
| no TP / 13wk | +0.262 | 1.99% / −7.7% | 0.26 | +0.700 | 9.92% / −12.1% | 0.82 | 1.36 | 39 |
| chandelier 3×ATR / 26wk | +0.187 | 1.33% / −8.1% | 0.16 | +0.720 | 7.99% / −12.4% | 0.64 | 0.92 | 38 |
| **chandelier 3×ATR + TP5 / 26wk** | +0.243 | 1.67% / −7.2% | **0.23** | +0.772 | **11.62%** / −12.5% | 0.93 | **1.51** | 42 |

| Exit (no pos cap) | IS Calmar | OOS CAGR / DD | OOS Calmar | OOS Sharpe |
|---|---|---|---|---|
| baseline | 0.06 | 26.1% / −16.0% | **1.63** | 1.40 |
| TP5 / 13wk | 0.17 | 32.1% / −22.1% | 1.45 | **1.72** |
| chandelier 3×ATR + TP5 / 26wk | 0.14 | 31.2% / −19.6% | 1.59 | **1.72** |

**Reading.**
- Letting winners run **roughly doubles OOS expectancy** (+0.40R → +0.77R) and **raises
  Sharpe** (1.30 → 1.44–1.51 capped, 1.40 → 1.72 uncapped). IS Calmar also improves for TP5
  and for chandelier+TP5.
- But longer holds overlap, so **max drawdown deepens** (OOS −7.5% → −12.5…−16.3%) and **OOS
  Calmar falls**. **As configured it fails the adoption bar.** That matches the old
  horizon-curve result (7wk Calmar worse) for the same reason.
- The open question is whether that's a sizing problem or a real one. Higher Sharpe with a
  deeper single-path DD often means "same edge, more correlated exposure". Hence the next test:

**Next test (before any build):** run TP5/13wk and chandelier+TP5/26wk at **lower risk per
trade (0.5–0.8%)** and **PORTFOLIO_CAP 0.6–1.0**, against the baseline at 1%:
1. Compare at **equal maxDD** (DD-matched CAGR) and on the **bootstrap distribution of maxDD**
   (`bootstrap_ci.py`), not one historical path.
2. Run walk-forward windows (`walk_forward.py`) for stability.
3. Adopt only if Full and OOS Calmar both beat the baseline at the chosen risk.

### 3.1 Risk-matched re-test (2026-11-21, all 21 instruments)
12-month block bootstrap of monthly returns, 1,000 paths. (A first run silently lost DIA to a
yfinance download failure. Its within-run comparison pointed the same way; the table below is
the full-universe rerun with retries.)

| Config | Full CAGR / DD | Full Calmar | Full Sharpe | OOS Calmar | OOS Sharpe | Bootstrap median DD / 5th-pct DD | Bootstrap Calmar median / 10th pct |
|---|---|---|---|---|---|---|---|
| **baseline** TP3/5wk, 1% risk | 5.11% / −7.6% | **0.67** | 0.91 | **1.34** | 1.30 | −10.3% / −15.9% | 0.63 / 0.29 |
| **chandelier 3×ATR + TP5 / 26wk, 0.5% risk** | 5.21% / −9.0% | 0.58 | **1.33** | 1.17 | **1.70** | **−6.2% / −9.9%** | **1.58 / 0.82** |
| chandelier + TP5, 0.6% risk | 5.61% / −9.9% | 0.57 | 1.29 | 1.15 | 1.64 | −6.8% / −11.1% | 1.52 / 0.79 |
| TP5 / 13wk, 0.5% risk | 5.12% / −9.1% | 0.57 | 1.27 | 1.06 | 1.63 | −6.8% / −10.4% | 1.31 / 0.70 |

The PORTFOLIO_CAP 0.8 variants were worse than 1.0 in every case.

**Verdict.**
- At half the risk, the chandelier+TP5 exit makes the **same CAGR** with **Sharpe +46%**
  (0.91 → 1.33) and a **much shallower drawdown distribution**: the bootstrap median DD goes
  −10.3% → −6.2% and the bad-case DD −15.9% → −9.9%. Bootstrap Calmar is 0.63 → 1.58.
- **But the one historical path's worst drawdown is deeper** (−7.6% → −9.0%), so on the
  project's single-path Full/OOS Calmar bar it **fails** (0.67 → 0.58, 1.34 → 1.17).
- It is a strong candidate whose evidence conflicts with the adoption rule. **Next:**
  1. identify the episode that sets its −9.0% (date, instruments) and check whether it's a
     one-off cluster;
  2. walk-forward windows (`walk_forward.py`);
  3. run it as a **shadow exit on PAPER** through the F3 ledger (same signals, both exits
     recorded) for 30+ trades;
  4. the user decides whether the bootstrap distribution may count alongside single-path
     Calmar in the adoption bar.

**Design (if §1 passes):**
1. `paper.EXIT_MODE` = `fixed` (today) | `tp_far` | `chandelier`. Parameters: `RR_DEFAULT`,
   `HORIZON_DAYS` / `HORIZON_CAL`, `TRAIL_ATR`.
2. Chandelier: stop = max(initial SL, highest weekly close since entry − k×ATR14), evaluated
   on completed weekly bars only (F1). The broker stop is modified weekly through
   `place_guarded(intent="resize")`. Phase 2's guard already supports in-place stop moves.
3. A longer horizon means more concurrent positions, so `PORTFOLIO_CAP` and the de-correlation
   buckets bind more often. The test must run with the live caps, not uncapped.
4. Journal and retro: R is still measured from the initial risk; holding-time distribution is
   added to the F3 card.
5. Roll-out: backtest IS/OOS + walk-forward + bootstrap → 30 forward PAPER trades → user approval.

## 4. I2/I3 — Cut the tax and cash drags

1. **Inventory each holding's domicile and distribution policy.** US-domiciled distributing
   ETFs (SGOV, AMLP, QQQ, DIA, IWM, EFA, VNQ, DBC, CPER, ...) lose 30% of distributions to US
   withholding for an HK resident. Irish-domiciled *accumulating* UCITS share classes pay 15%
   inside the fund on US dividends and nothing to the holder, and reinvest the rest.
2. **Cash shield first (biggest, simplest):** compare SGOV with an accumulating USD T-bill UCITS
   ETF (e.g. a 0–1y US Treasury UCITS fund on the LSE). Criteria: tracking vs T-bill, spread,
   LSE trading hours vs the sweep's execution window, IBKR commission. Then rebalance weekly
   with a wide band (fidelity F6), not every cycle.
3. **Core book:** extend `ucits_equivalence_test.py` to the remaining US-listed names that
   have Irish accumulating equivalents. Same bar as the 2026 UCITS swaps: signal agreement
   ≥ 90% on weekly bars (VNQ was rejected at 79.5%) and no Calmar loss.
4. Ask IBKR whether SGOV's distributions qualify as interest-related dividends (QII) that are
   exempt for non-US holders. That could recover past withholding. An admin item.

Expected: about +0.5–0.9pp/yr CAGR for the same risk (the measured withholding drag), plus
the SGOV fix. This is the most *certain* improvement in the spec.

### 4.1 Measured 2026-11-21
**Cash shield** (yfinance total return, same window):

| | Nov 2024 → Nov 2026 | Nov 2025 → Nov 2026 | Pays out? | Trading |
|---|---|---|---|---|
| SGOV (US, distributing) | 4.04%/yr gross | 3.76% | yes, 3.6–3.9%/yr, **30% withheld = −1.1–1.2%/yr** | NYSE, ~21.6M sh/day |
| **IB01** (Irish UCITS, accumulating) | **4.05%/yr** | 3.71% | **no → nothing withheld** | LSE, USD, ~431k sh/day |
| ZPR1 (Irish UCITS, accumulating) | 4.01% | 3.77% | no | Xetra, thin (~3k/day) |
| ERNS (ultrashort bond) | 4.51% | 4.03% | yes (GBP line) | not a T-bill fund |

**IB01 is the switch**: same gross yield, about +1.1–1.2%/yr net on everything parked. At the
Nov-2026 shield size (~HKD 300k) that is ~HKD 3.4k/yr, about 1.2% of NAV.

**Core book**, time-weighted upper bound since 2016 (US-domiciled names only; UCITS names pay
no US withholding to the holder): **~0.41% of NAV/yr**, led by AMLP 0.13, VNQ 0.07, VNQI 0.06,
CWB 0.04, DIA 0.04, DBC 0.03, ASHR 0.03, QQQ 0.02. (EFA/IWM yields were unavailable; add
~0.05%.) The realized drag is lower because portfolio caps skip some signals. Swapping these is
worth ≤0.3–0.4%/yr in total and each needs the equivalence test (VNQ already failed at
79.5%), so **it ranks below the shield**. Candidates to test: QQQ→CNDX (USD, accumulating,
LSE), ASHR→CNYA, DBC→an accumulating commodity UCITS.

### 4.2 BUILT 2026-11-21 (`ib_exec.py`, not on LIVE)
- `CASH_SHIELD_SYMBOL` (default **SGOV**, so nothing changes until configured) and
  `shield_symbols()` = configured + legacy. Reconcile, the untracked-positions card and
  deployed-capital all exclude every shield symbol.
- **Migration:** a legacy shield still held is SOLD first (its own US window, SMART-qualified
  contract, one leg per cycle, through the long-only guard). The next eligible cycle re-parks
  the cash in the configured shield, inside that shield's own exchange window
  (`within_entry_execution_window(exchange=...)`, LSE for IB01).
- **Anti-churn:** rebalance only if |delta| ≥ max(USD 2,500, 5% NAV) **and** at most one
  confirmed rebalance per 7 days (persisted in `cash_sweep_last_confirmed_ts`). The exception
  is a SELL that clears a margin debit worse than USD 500.
- `prepare_withdrawal` frees cash from whichever shield is held. The dashboard pie label is
  now "Cash shield".
- Tests: `test_cash_shield` (config, band/cadence, LSE vs US windows, migration sells SGOV
  first without starting the cadence, weekly cadence blocks/permits, band). Full suite 348 passed.
- **PAPER** (`docker-compose.yml`) sets `CASH_SHIELD_SYMBOL=IB01`; deploys on the next push.
  **LIVE:** after PAPER shows a clean SGOV→IB01 migration and a weekly rebalance, set the same
  env in `docker-compose.live.yml`.
- Still open: ask IBKR whether past SGOV withholding is reclaimable (QII); the core-book swaps
  listed above.

## 5. I4 — Put the idle half to work (core + overlay)

**Hypothesis:** the trend book is low-correlation with a static global allocation (its edge
comes from trends in any direction of the macro cycle), so a static core funded from the idle
cash could raise *portfolio* Sharpe, not just CAGR.
**Test (`research/core_overlay_test.py`):**
- Arms:
  - (a) today: trend book + T-bill cash;
  - (b) trend book + a 40/60 global equity/bond core in the idle cash;
  - (c) trend book + a 60/40 core;
  - (d) trend book + an all-weather core.
- Monthly rebalance. Same 1% trend risk. Margin cost modeled at the real IBKR rate when
  deployment exceeds 100%.
- Report Full/OOS Calmar, Sharpe, worst drawdown and correlation of the two return streams.
- **Adopt only if Calmar improves.** A CAGR gain bought with drawdown is just leverage, which
  the project has already rejected (vol targeting, 2% risk).

## 6. Inventory of what's already been tested (summary)
Full table: this session's inventory, compiled from HANDOFF.md and `dashboard/research/`.
Highlights:
- **Exits:** breakeven (Calmar 0.854→0.678), fixed/ATR trailing within TP3/5wk (0.70–0.79 vs
  0.81), partial profits (0.51–0.78), STRUCT (OOS expR −0.581), time-decay, RSI-exhaustion,
  and S/R trail: all rejected. Horizon 1–8 bars with TP3: 5wk plateau adopted.
- **Entries/filters:** RSI 70 block, MIN_STRENGTH 5 and the re-entry gate (reclaim+1R, OOS cd
  3.49 vs 1.40) adopted. Pullback, return filters and the top-N momentum filter rejected. The
  90-day return filter is mixed (walk-forward re-selection never run).
- **Regime / sizing:** all regime overlays, DD circuits, vol targeting and conviction/vol-risk
  sizing rejected. PORTFOLIO_CAP 1.0 adopted. pos_cap is a leverage dial (Sharpe flat ~0.88).
- **Universe:** 10 screening batches. Adopted CWB, VNQI, AMLP, ASHR and the 9 UCITS swaps.
  Long list rejected. HYD removed.
- **Sleeves:** panic-MR dip-buy adopted (11 tickers, 10%, ADX>25). MR sleeve, pairs, stat-arb,
  short vol, earnings and LEAPS rejected.
- **Costs:** withholding drag −0.88pp/yr (unaddressed beyond the UCITS swaps), congestion
  slippage small, commissions small.
- **Robustness:** walk-forward 5/6 windows profitable, DSR 100%, bootstrap Calmar 90% CI
  [0.54, 1.36]. **The deployed config has not been bootstrapped.**

## 9. Universe scan (2026-11-21, live config, TP3/5wk)
Leave-one-out on the 21 instruments, plus 22 never-tested ETFs added one at a time. Bar: Full
AND OOS Calmar must both improve.
- **Adds:** none worth it. All 10 sector ETFs (XLV/XLP/XLU/XLI/XLB/XLY/SMH/IBB/ITA/ICLN)
  *hurt*: they crowd the equity-index capacity. EWZ/EWT/EMLP/MBB/BKLN/MTUM/DBB hurt too. KWEB
  +0.010/+0.025 (n=27), GLTR/FLOT ≈ 0.
- **Leave-one-out "drop?":** DIA (+0.106 / +0.186), QQQ (+0.073 / +0.113), IDTL
  (+0.020 / +0.117), PRFD (+0.023 / +0.057), TIPS (+0.006 / +0.041), IHYA, IGLN (tiny).
- **Joint:**

| Universe | Full Calmar | CAGR | DD | Sharpe | OOS Calmar |
|---|---|---|---|---|---|
| today | 0.674 | 5.11% | −7.6% | 0.91 | 1.344 |
| −IDTL −PRFD −TIPS | 0.721 | 5.47% | −7.6% | 0.99 | 1.533 |
| −DIA | 0.780 | 5.75% | −7.4% | 0.98 | 1.531 |
| −QQQ | 0.747 | 5.17% | −6.9% | 1.00 | 1.457 |
| −DIA −QQQ | **0.669** | 5.32% | −8.0% | 0.98 | **1.195** |
| −DIA −QQQ −IDTL −PRFD −TIPS | 0.927 | 5.89% | −6.3% | 1.09 | 1.694 |
| … + KWEB | 0.927 | 6.30% | −6.8% | 1.16 | 1.718 |
| … −IHYA −IGLN as well | 0.741 | 5.54% | −7.5% | 1.03 | 1.335 |

**Reading.**
- Removals are **not additive**: −DIA and −QQQ each help, but both together hurt. Removing an
  instrument reshuffles which later signals get capital under PORTFOLIO_CAP and the
  one-per-instrument walk, so single-path Calmar moves chaotically. Picking the best of these
  combinations would be overfitting.
- **The robust part is IDTL, PRFD and TIPS.** All three have negative standalone expectancy
  (−0.17 / −0.16 / −0.12R), each improves Calmar when removed alone, and together they improve
  both windows. **Recommend dropping them** (user approval required; the parameter freeze
  applies).
- **DONE 2026-11-21 (user-approved):** IDTL, PRFD and TIPS retired from the traded universe
  (21 → 18 instruments). They stay resolvable for journal rows (`_RETIRED_ETF_*` in
  `instruments.py`).
- DIA/QQQ: candidates only. Test with bootstrap + walk-forward before deciding, and note they
  are also two of the US-domiciled (withholding) names.

## 7. Order of work
| Step | Item | Gate |
|---|---|---|
| 0 | Fidelity first: guard phase 2 + F1 PAPER week; F4 entries; F3 ledger | fidelity spec |
| 1 | **I2/I3 tax and cash** (inventory, UCITS T-bill comparison, weekly sweep band) | equivalence ≥ 90%, no Calmar loss |
| 2 | **I1 exit redesign**: risk-matched re-test (0.5–0.8% risk, PORTFOLIO_CAP 0.6–1.0) + bootstrap maxDD + walk-forward | §1 bar (fails today at 1% risk) |
| 3 | I4 core + overlay test | Calmar improves |
| 4 | I5/F8 signal walk-forward; I6/I7/I8 as capacity allows | §1 bar |
| 5 | Bootstrap the *deployed* config after any adoption | 90% CI reported |

## 8. Expectations
This is a low-drawdown trend system: ~5–7% CAGR with ~−8 to −10% DD at the live caps. The
realistic upside from this spec is roughly **+1–3pp CAGR at similar drawdown** (tax and cash
drag recovery is the surest part; the exit redesign is the biggest but least certain). None of
it removes bad stretches like Jul–Nov 2026, which the backtest has lived through before. The
pre-registered kill/scale rule (fidelity F7) is what governs those.


---

## 10. Roadmap: what's still worth building (2026-11-21)

The question was whether the remaining phases are still needed. **Short answer: yes for
safety (guard phase 3) and measurement (fidelity F3/F4); guard phase 4 becomes optional;
guard phase 5 folds into F3.** Two new performance ideas came out of the universe scan (C1, C2).

### A. Safety (needed regardless of performance)
| # | Item | Verdict | Why | Size |
|---|---|---|---|---|
| A1 | Guard phase 2 → LIVE | **YES**, after the PAPER week (running since Nov 21) | Order choke point + broker-aware reprotect | deploy |
| A2 | **Guard phase 3** (confirmed VOID + orphan sweeper + **G3.4 fill audit**) | **YES, next build** | Orphan stops opened 3 of the 4 LIVE shorts. More may still rest (#34 QQQ). Three "VOID" trades really filled. Phase 2 blocks *new* bad SELLs but can't clean up old ones | M |
| A3 | Guard phase 4 (auto-cover) | **OPTIONAL / defer** | With phases 2+3 every known short-creation path is closed, and phase 1 already pages RED. Do the cheap part now: the RED page carries the exact cover tickets. Build auto-cover only if a short ever appears again | S now / M later |
| A4 | Guard phase 5 (accounting) | **Merge into B2 (F3)** | One ledger serves both | — |

### B. Fidelity / measurement (needed to trust any performance decision)
| # | Item | Verdict | Why | Size |
|---|---|---|---|---|
| B1 | F1 completed weekly bars | **BUILT** (PAPER) → LIVE with A1 | −11R of partial-week signals | deploy |
| B2 | **F3 shadow ledger + "Live vs backtest" card** | **YES** | Signal-level vs funded vs execution-drag R, funded-only KPIs, gate attribution (incl. the LLM veto), and **shadow exits** so I1 gets its 30 forward trades without risking money | M |
| B3 | **F4 entry fidelity** (two-sided ±0.25R stale check at order time, marketable LMT, first window after the weekly close) | **YES** | Live fills squeezed stops to 0.5–0.9% (QQQ #141, DIA #143 stopped the same day) | S |
| B4 | F5 config-parity check | YES | Live pos_cap 25% vs the backtest reference at 30% went unnoticed | S |
| B5 | F7 kill/scale rule in config | YES (user sets the numbers) | Pre-registered verdict for the next bad stretch | S |
| B6 | F8 walk-forward of the signal definition | LATER | Valuable, but slower and lower priority than B2/B3 | M |

### C. Performance
| # | Item | Verdict | Evidence / rationale | Size |
|---|---|---|---|---|
| C0 | I3 shield → LIVE | **YES**, after the PAPER SGOV→IB01 migration | ~+1.1–1.2%/yr on the shield (~1.2% of NAV/yr today) | config |
| C1 | **NEW: US-large-cap cluster cap** (at most N concurrent positions across CSPX/QQQ/DIA/IWM; test N=1, 2) | **RESEARCH next** | Every sector ETF and DIA/QQQ hurt by *crowding* the equity capacity, not by bad signals (−DIA and −QQQ each helped, both together hurt). A cluster cap targets crowding directly instead of throwing instruments away. Untested (`--concentrated` was a no-op on ETFs) | S |
| C2 | **NEW: capital priority when PORTFOLIO_CAP binds** (today first-come; test ranking same-week signals by strength/obviousness, or favouring a new asset class) | **RESEARCH** | The universe scan showed outcomes swing on *which* signals get capital. A deterministic priority would make that choice deliberate and stabilize results. Untested | S |
| C3 | I1 exit (chandelier + TP5/26wk at 0.5% risk) | **RESEARCH → shadow via B2** | Sharpe 0.91 → 1.33, bootstrap Calmar 0.63 → 1.58, but single-path DD −7.6% → −9.0%. Find the −9% episode, run walk-forward, run as a shadow exit. User decides whether the bootstrap counts in the bar | M |
| C4 | I4 core allocation for idle cash | RESEARCH | Untested. Adopt only if Calmar improves | S/M |
| C5 | UCITS swaps in the core book (QQQ→CNDX, ASHR→CNYA, DBC→accumulating commodity) | LATER | ≤0.3–0.4%/yr total; each needs the ≥90% equivalence test | S each |
| C6 | KWEB add | DEFER | +0.01/+0.03 only; re-check after C1 (China overlap with ASHR) | S |

### Suggested order
1. **Now (user):** close the 4 LIVE shorts and cancel orphan SELLs in TWS during Monday's US
   session.
2. **PAPER week (running):** phase 2 + F1 + IB01 shield + 18-instrument universe. Watch for
   `GUARD: refused`, the SGOV→IB01 migration and weekly-only sweeps.
3. **Build:** A2 guard phase 3 (with G3.4) + B3 F4 (both in `ib_exec.py`, one PR).
4. **Build:** B2 F3 ledger + card (shadow exits for C3), then B4/B5.
5. **Research in parallel** (no live risk): C1, C2, C3 episode + walk-forward, C4.
6. **LIVE deploy bundle**, once PAPER is clean: phase 2 + F1 + IB01 + universe change, then
   phase 3 + F4 after their own PAPER run.

---

## 11. Return vs drawdown frontier (2026-11-21, 18 instruments)
Idle cash earns 3%/yr, and margin (deployed > equity) costs 5.5%/yr, so leverage is charged
for. Single historical path. Full = 2001+, OOS = 2016+.

| Config | Full CAGR / DD | Full Calmar | OOS CAGR / DD | OOS Calmar | OOS Sharpe |
|---|---|---|---|---|---|
| **live today**: pos cap 25%, 1% risk, PORTFOLIO_CAP 1.0 | 6.65% / −7.0% | 0.95 | **11.83%** / −7.0% | 1.69 | 1.55 |
| same, risk 1.5% | 6.68% / −6.9% | 0.97 | 11.91% / −6.9% | 1.72 | 1.53 |
| **PORTFOLIO_CAP 1.5** (25% cap, 0.75% risk) | 6.88% / −9.9% | 0.69 | **13.58%** / −7.6% | 1.79 | 1.53 |
| chandelier+TP5 exit, PC 1.5, 0.75% risk | 7.42% / −9.3% | 0.80 | **14.29%** / −9.0% | 1.58 | 1.45 |
| pos cap 35%, PC 1.5, 1.25% risk | 7.90% / −10.8% | 0.73 | **15.68%** / −10.7% | 1.46 | 1.44 |
| chandelier, pos cap 35%, PC 1.5, 1.25% | 8.41% / −14.8% | 0.57 | 17.24% / −14.8% | 1.16 | 1.32 |
| no pos cap, 1% risk | 8.14% / **−30.2%** | 0.27 | 20.16% / −24.9% | 0.81 | 1.19 |

**Findings.**
1. **The binding constraint is the 25% position cap, not risk per trade.** Raising risk from
   0.75% to 1.5% at the 25% cap changes almost nothing (11.73% → 11.91% OOS).
2. Leverage buys CAGR at a falling Calmar: ~12% OOS at −7% DD, ~14% at −9 to −10%, ~16% at
   −11%, ~17% at −15%. Uncapped is a −25 to −45% DD.
3. The **single-path DD understates tail risk**. At live settings, the bootstrap 5th-percentile
   DD was about 2× the historical path (§3.1). Plan for ~1.5× the table's DD.
4. **OOS 2016+ is a favourable window** (bull market, full universe). 2001–2015 delivered only
   ~2%/yr. The full-history 6.7–7.9% is the honest long-run expectation.

**Decision for the user (new parameter freeze item):** choose a drawdown budget.
- −7% path (≈ −10–12% tail) = today, ~12% OOS / ~6.7% full.
- **−10% path (≈ −15% tail) = PORTFOLIO_CAP 1.5 (+ the chandelier exit if adopted), ~13.6–14.3% OOS / ~6.9–7.4% full.**
- −11% path (≈ −16% tail) = pos cap 35% + PC 1.5, ~15.7% OOS / ~7.9% full.

Every option needs the IBKR margin account. Margin cost is already charged at 5.5% in these
numbers.

**DECIDED 2026-11-21 (user): keep the −7% budget.**
- No change to pos cap (25%), risk (1%) or PORTFOLIO_CAP (1.0); no margin.
- Improvements must therefore come from return per unit of risk and from costs, not leverage:
  - I3 tax/cash (built);
  - the universe cleanup (done);
  - C1 cluster cap, C2 capital priority;
  - the I1 exit only if it fits inside the same budget (at 0.5% risk: same CAGR, Sharpe
    0.91 → 1.33);
  - I4 idle-capital allocation only if it keeps the path DD within ~−7%.


---

## 12. Built + researched 2026-11-21 ("do the rest of the spec")

### 12.1 Built (all tests: `pytest dashboard/tests` 358 passed; nothing on LIVE)
| Item | What | Where | Tests |
|---|---|---|---|
| **A2 guard phase 3** | G2: a resolved trade's own orders (by orderRef) are cancelled and the row goes **CANCEL_PENDING**, not VOID. VOID only after two clean order reads ≥30s apart; still resting after 3 cycles → RED. G3: every resting order (all clients) is classified OWNED / CANCELLING / SHIELD / ORPHAN_SELL / ORPHAN_SELL_COVERED / ORPHAN_BUY / FOREIGN; orphans get cancelled (`ORPHAN_SWEEP=act`, or `report`), RED if they won't go. G3.4: hourly broker-fill audit; a VOID/ghost row whose entry really filled → RED, and the row is re-opened if the broker still holds the shares | `ib_exec.py` (`cancel_pending_step`, `classify_open_orders`, `sweep_orphan_orders`, `void_fill_findings`, `audit_void_fills`), `ops/global_cancel_rearm.py` (manual escalation, dry run by default) | T4, T5, T16 in `test_long_only_guard`; VOID tests updated |
| **B3 F4 entries** | **Bug fixed: the −0.5R stale-signal check looked ETFs up in the MT5 universe and never ran for any ETF** (QQQ #141 / DIA #143 were funded at −0.63R / −0.68R). New chase rule: cancel if the live reward:risk to the trade's own SL/TP < MIN_RR (1.5). ETF entries are a marketable LMT capped at that price (entry + 0.6R; `ENTRY_LMT_CAP=0` restores MKT) | `ib_exec._stale_signal_check`, `entry_live_rr`, `entry_limit_price`, `_place_etf_bracket` | `test_f4_stale_check_now_runs_for_etfs_and_refuses_chasing` |
| **B4 F5 parity** | The backtest reference label/config follows the LIVE sizing (it was pinned at pos_cap 30% vs live 25%). Mismatches show on the drift card | `web/drift.py` | `test_ledger` |
| **B5 F7 kill rule** | ≥30 funded closed core trades and the 90% upper bound of expectancy < 0 → `mirror_new` halts NEW entries (RED). `KILL_RULE=0` disables, by decision. The card shows its reading | `drift.kill_rule`, `ib_exec.mirror_new` | `test_ledger` |
| **A3** | The UNINTENDED SHORT page lists the exact cover tickets | `notable_events.cover_tickets` | `test_ledger` |
| **B2 F3 shadow ledger** | Every approved and every blocked (per gate, LLM included) long signal gets a shadow row with its SL/TP, deduped per signal bar, resolved hourly by the journal's resolver. A "Signal & gate ledger" card shows signal-level R, execution drag, and R saved per gate | `core/shadow.py`, hooks in `paper.place_from_state` + `service`, card in `app.py` | `test_ledger` |

### 12.2 Research verdicts (order-robust: median over 20 random same-date orderings)
**New evaluation standard (C2 finding):** the order in which same-week signals get capital
alone moves single-path Calmar 0.82–1.13 (full) / 1.46–2.57 (OOS). From now on, compare configs
on the **paired distribution over random orderings**, not one path.

| Change | Wins vs 18-instrument baseline (full / OOS) | Median Calmar full / OOS | Verdict |
|---|---|---|---|
| 21 → 18 instruments (IDTL/PRFD/TIPS dropped) | **100% / 100%** | 0.96 → **1.06** / 1.70 → **2.01** | **robust, done** |
| C1 US-large-cap cluster cap | 30% / 30% | 1.01 / 1.88 | reject |
| I1 chandelier+TP5 @0.5% | 50% / 40% | 1.05 / 1.83 | **no gain** (it is only less order-sensitive). Shadow-exit work dropped |
| I1 chandelier @0.75% | 0% / 0% | 0.77 / 1.38 | reject |
| I4 idle-cash core blend (corrected monthly series) | — | Calmar 1.18 → 1.25 at 10%, OOS CAGR falls | reject at −7% |
| C2 "non-equity first" priority | single path 1.12 / 2.38 (above the 75th pct of random orders) | — | weak candidate; needs its own order-robust check |
| LLM veto (RETRO_LLM) | approved −0.661R vs vetoed −0.656R | portfolio A/B within ordering noise | keep, don't credit, don't extend |

### 12.3 What's left
- LIVE deploy bundle once PAPER is clean: guard phases 2+3, F1, F4, IB01 shield, 18-instrument
  universe, ledger, kill rule.
- The ledger needs ~143 setups per group before it can show a 0.3R gate effect.
- Make the LLM one frozen decision per completed weekly bar (RETRO_LLM) so the ledger can
  score it cleanly.
- C2 order-robust test; F8 walk-forward of the signal definition; core-book UCITS swaps.
