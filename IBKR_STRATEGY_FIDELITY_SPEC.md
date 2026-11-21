# IBKR Strategy Fidelity — spec (2026-11-21)

**Status:** PROPOSED. F1 audit and F2 analysis **run 2026-11-21** (research only, nothing
built). F1 is supported: switch to completed bars. F2 says "bad regime, within history".
Results are in the retro, §7.1.
**Why:** [RETRO_PERFORMANCE_2026-11-21.md](RETRO_PERFORMANCE_2026-11-21.md). LIVE's 14 funded
trades went −0.37R/trade (14% win) against a backtest of +0.30R (45% win). Replaying all 81
approved signals at their own signal prices was just as bad (−0.37R, −29.5R total), so the loss
is in the **signals, not the fills**. The account's +HKD 8.8k is entirely the unintended shorts.
**Goal:** make LIVE trade *exactly* the strategy that was backtested, measure the gap
continuously, and decide on evidence whether the edge is real. Until then, risk as little
capital as possible.
**Scope:** `dashboard/data/providers.py`, `dashboard/core/paper.py`,
`dashboard/execution/ib_exec.py`, `dashboard/research/*`, `dashboard/web/retrospective.py`,
`dashboard/app.py`. Applies to PAPER and LIVE.

---

## 0. Principles
1. **One strategy, two runtimes.** Every rule the backtest uses, LIVE uses the same way, and
   every live-only layer is measured separately. Today neither half of that holds.
2. **Signal-level R is the truth for "is there an edge".** Funded R measures execution.
   Dollar P&L measures sizing. All three get reported, never mixed together.
3. **Pre-register the verdict.** The kill/scale rule (F7) is written down *before* the next
   trades, so a bad month can't move the goalposts in either direction.

---

## F1. Score on completed weekly bars only (H1) — P0, small

**Problem.** `providers.get_history()` uses yfinance `interval="1wk"`. On a weekday its last
bar is the **in-progress week**, so `compute_facts()` → MA/ATR/strength/breakout read a
partial bar. The backtest only ever sees completed weekly closes and enters at that close.
Live signals are spread evenly Mon–Fri, which a completed-bar system wouldn't produce.

**Design.**
1. `providers.completed_weekly(series, now)`: drop the last bar if its week hasn't closed
   (US: Fri 16:00 ET; LSE ETFs: Fri 16:30 London; use the instrument's calendar, which
   `within_entry_execution_window` already has).
2. Scoring and SL/TP (`compute_sltp`) use completed bars only. The **entry reference** is the
   last completed weekly close, exactly as in the backtest.
3. Signals can therefore only appear once per week, right after the weekly close. The scan
   loop keeps running for exits and monitoring.
4. **Evidence first (do this before switching):** `research/partial_bar_audit.py` re-scores
   each of the 81 live signals with the in-progress bar dropped. For each one it records
   whether the signal existed at the prior completed close, and its replayed R under both
   rules. Ship F1 only if the completed-bar subset is materially better or the partial-bar
   signals are the losers. Otherwise H1 is rejected and that's written down.

**Audit result (2026-11-21): SUPPORTED.** 16 of 81 live signals existed only on the
in-progress week: −0.68R avg / 12% win, vs −0.29R / 25% for the 65 that also existed at the
completed close. They cost about −10.9R. 13 of the 16 were RSI>70-blocked at the completed
close and passed only after a mid-week dip. **Ship F1.**

**Tests.** A completed-week detector at each boundary (Fri 15:59 / 16:01 ET, holidays). A
replayed historical week produces the same signal live and in backtest (golden test against
`backtest._signals`).

## F2. "Bad luck or broken?" — the backtest's own rolling distribution (H2) — P0, small

**Design.** `research/rolling_windows.py`:
- From the backtest's realized trade list (live config), compute every rolling window of
  **~4.5 months** and of **~80 signals**: expectancy, win rate and total R.
- Report the percentile of LIVE's signal-level result (−0.37R over 81) and funded result
  (−0.37R over 14) inside that distribution, plus the worst historical windows and their dates.
- **Verdict rule:** above the 5th percentile → "plausible bad regime, continue with F7".
  Below the 1st percentile → "live ≠ backtest; investigate F1/F3/F8 before trading more".
- **Result (2026-11-21): plausible bad regime.** The backtest's own signals for Jul 1 → Nov 20,
  2026 were n=33 at −0.43R (−14.1R). LIVE signal-level, one per instrument-week, n=50 at −0.32R
  = **9th percentile** of 786 rolling 140-day backtest windows (median +0.31R, 5th pct −0.46R,
  worst −1.01R in May–Jun 2015), and the 2.5th percentile of rolling 30-signal windows.
  Make `rolling_windows.py` a repo tool so this is re-run monthly (F3 card).

## F3. Shadow ledger: three R's for every signal (H4) — P1, medium

Every approved **and** gate-rejected signal gets a shadow row resolved on daily bars:
- **signal R**: filled at the signal's entry, the backtest view;
- **funded R**: the real fill, if any;
- **execution drag** = funded − signal;
- **gate attribution**: for rejected signals, which gate rejected it (LLM veto, trend<5,
  RSI, re-entry, cooldown, tech-pause), plus that gate's running "R saved / R missed".
  `llm_veto_replay` already does this for the LLM; generalize it.

Dashboard card **"Live vs backtest"**:
- signal-level expectancy with a CI band against the backtest band;
- funded-only KPIs (this replaces guard spec G6.5's KPI fix: n counts filled-and-closed only);
- execution drag;
- each gate's R saved.

`live_vs_backtest.py` switches to funded-only n (today it reports n=130 with 116
never-filled 0R rows).

## F4. Entry fidelity — P1, small/medium

**Problem.** Fills land 0–10 days after the signal with −0.68R…+0.90R of slippage. A fill near
the stop leaves a 0.5% stop that dies the same day. A fill above the signal cuts a 3R target
to ~1R.

**Design** (only matters once F1 makes signals weekly):
1. Enter in the first execution window after the weekly close (Mon open for US) with a
   **marketable LMT** capped at `entry + 0.25R`. DAY, re-priced once.
2. **Two-sided stale check at order time** from a live quote: cancel if price is beyond
   ±0.25R of the signal entry (today it is one-sided, −0.5R).
3. Keep SL/TP at the signal's levels (backtest geometry). Don't re-anchor to the fill: that
   would change the risk the backtest measured.
4. Record `slip_r` on every fill (feeds F3).

## F5. Sizing transparency and config parity — P1, small
- Every entry logs **realized risk / budget**. The panel shows the distribution. Today the
  25% cap binds on most trades and realized risk is 1–65% of budget.
- **Config parity check at startup:** live `ETF_POS_CAP` (0.25), `PORTFOLIO_CAP`,
  `RISK_PER_TRADE`, gates and universe vs `backtest_reference.config` (computed at
  `pos_cap=0.30`). A mismatch is a YELLOW event and is shown on the card. Either align them
  or recompute the reference at the live config.

## F6. Cash shield: fix it or turn it off — P2, small
**Facts:** SGOV +HKD 99 on HKD 150–300k (55 trades, HKD 429 commissions, 30% US withholding
= HKD 241).
1. Rebalance **at most weekly**, with a band of max(USD 5,000, 5% NAV) instead of
   `CASH_SWEEP_MIN_USD=1,500` every cycle.
2. Check with IBKR whether SGOV distributions can be paid without 30% withholding
   (interest-related dividends / QII; W-8BEN on file). A config/admin item, no code.
3. Monthly comparison vs simply holding USD cash at IBKR's credit rate. If SGOV doesn't beat
   it net of fees and tax for 2 consecutive months → disable the sweep (`CASH_SWEEP=0`).

## F7. Pre-registered verdict and capital rule — P0, decision
Proposed (user decides):
- **Investigation mode (default until F1+F2 report):** LIVE runs **signal-only / shadow**.
  The scanner, journal and F3 ledger all run, but no new entries are funded. Capital stays in
  cash or the shield. Exits and protection for anything still held keep running.
- **Resume funding** when F2 says "plausible regime" **and** F1 is decided (switched or
  rejected with evidence).
- **Kill rule** once funding resumes: after **n ≥ 30 funded trades**, if the 90% CI upper
  bound of funded expectancy is < 0 → halt entries and re-research. The existing DD_HALT
  (−13%) stays as the hard stop.
- **Scale rule:** no deposits into the strategy sleeve until n ≥ 30 with expectancy CI
  overlapping the backtest band.

## F8. Walk-forward re-validation (H3) — P2, medium
Re-run the adopted configuration with every gate **frozen at its adoption date**, on data
strictly after that date (re-entry 2026-07-18, PORTFOLIO_CAP 2026-07-11, etc.). Also run a
rolling 5-year-train / 1-year-test walk-forward of the core parameters (MA lengths, ATR
multiple, rr, MIN_STRENGTH). Any gate whose OOS contribution ≤ 0 is removed. Output: the same
`backtest_reference` fields, computed OOS.

---

## 9. Refinements to the unbuilt guard phases ([IBKR_LONG_ONLY_GUARD_SPEC.md](IBKR_LONG_ONLY_GUARD_SPEC.md))

| Phase | Refinement from this retro |
|---|---|
| **3** (G2 confirmed VOID + G3 sweeper) | **Add G3.4 fill audit.** Three "VOID" trades (AMLP 21, CPER 84, SPY 4) really filled, and the journal never saw it. Each cycle, match broker-wide executions (`reqExecutionsAsync`, `orderRef=quant#<id>`) against mirror rows. A VOID/CANCELLED row with a fill → re-open as *flagged* with the real qty and RED. New test T16 (the AMLP #16 Jul 13 replay: VOID + fill → flagged, stop owned). Phase 3 stays next after phase 2: orphans opened 3 of the 4 shorts. |
| **4** (G5 auto-cover) | Unchanged. While it's unbuilt, any SHORT_INCIDENT also pages "cover at next RTH open" with the exact tickets. |
| **5** (G6.2–6.6 accounting) | G6.5 (KPIs funded-only) and the "unintended exposure" retro row move into **F3** here, since one ledger serves both. G6.2 headline split stays in phase 5. G6.6 stale-sample marking stays. |

## 10. Order of work

| Step | Item | Size | Gate |
|---|---|---|---|
| 1 | F7 decision (shadow vs keep funding) | user | — |
| 2 | ~~F2 rolling windows + F1 partial-bar audit~~ **DONE 2026-11-21**: F1 supported, F2 = bad regime within history | S | written verdict ✓ |
| 3 | Guard phase 2 PAPER week (running) → LIVE | — | spec gate |
| 4 | F1 switch (if the audit supports it) + F4 entries | S/M | golden test live = backtest |
| 5 | Guard phase 3 incl. G3.4 fill audit | M | T4, T5, T16 |
| 6 | F3 shadow ledger + card, F5 parity | M | card shows all three R's |
| 7 | F6 shield fix/kill, F8 walk-forward | S/M | |

## 11. Acceptance
- A replayed historical week produces the **same signal, entry, SL and TP** live and in backtest.
- The dashboard shows signal-level, funded and dollar results separately, with funded-only n.
- F2's percentile and F1's audit are written into the retro with a verdict.
- The kill/scale rule exists in config and fires in a unit test.
