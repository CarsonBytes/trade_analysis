# Retro — 2026-11-21: LIVE performance Jul–Nov 2026 vs the backtest

**Trigger.** The user said the recent months look much worse than the backtest promised.
They are. This retro measures how much worse, where the money actually came from, and
whether the cause is the strategy or the plumbing.

**Evidence.** Broker truth comes from one IBKR Flex pull for the LIVE account, 2026-07-01..11-20
(`CNAV` daily change-in-NAV, `EQUT` daily NAV, `STFU` every fill/dividend/fee). Journal truth is
a copy of `/data/dashboard_live_docker.db` (`paper_trades`, `ib_mirror`, `rejected_signals`).
Prices come from yfinance daily bars. The repo's own `research/live_vs_backtest.py` and
`research/llm_veto_replay.py` were run against the copy. Scratch scripts and the statement stay
outside the repo.

---

## 1. Headline: the account is up, but the strategy isn't

| | HKD |
|---|---|
| Deposits (7, all verified against IBKR) | 280,000 |
| NAV, Nov 20 EOD (Flex) | 288,873 |
| **Total P&L** | **+8,833** (TWR **+4.7%** over 4.7 months) |
| SPY over the same window (Jul 1 → Nov 20) | **−7.4%** |

Where the +8,833 came from (Flex fills + Nov 20 closes, USD.HKD 7.8388):

| Bucket | HKD | Notes |
|---|---|---|
| **Unintended shorts** (QQQ −8, AMLP −21, CPER −84, SPY −4) | **≈ +9,400** | unrealized. See [RETRO_2026-11-21.md](RETRO_2026-11-21.md). Not strategy. |
| **Strategy trades** (every non-SGOV fill, net of the short legs) | **≈ −450** | includes the 3 "VOID" trades that really filled (§4.3) |
| **SGOV cash shield** | **≈ +100** | §5 |
| FX translation, interest, misc. | ≈ −200 | |

**So the strategy itself made roughly zero in 4.7 months, and the whole headline gain is an
accident that could reverse.**

> **Bottom line (after §7.1).** About 2/3 of the signal-level loss is a genuinely bad regime
> that the backtest *also* lost in (its own Jul–Nov 2026 signals: −0.43R). About 1/3 is a
> live-only bug: scoring the unfinished weekly bar (16 extra signals at −0.68R). Execution was
> neutral.

### Monthly (Flex CNAV, time-weighted)

| Month | NAV end (HKD) | Deposits | Account TWR | SPY | Strategy capital (avg % NAV)* |
|---|---|---|---|---|---|
| Jul | 130,774 | 130,000 | +1.03% | +0.03% | ~62% |
| Aug | 221,780 | 90,000 | +1.07% | +2.68% | (SGOV ramp, not separable) |
| Sep | 252,660 | 30,000 | +0.39% | −0.33% | ~44% |
| Oct | 279,803 | 30,000 | −1.05% | −0.91% | ~35% |
| Nov 1–20 | 288,873 | 0 | +3.24% | −8.72% | ~4% (shorts ≈ 23% NAV) |

\*From Flex `EQUT` StockLong minus the dashboard's SGOV value history. Approximate.

November's +3.24% is the shorts. Without them, November would have been about flat, which is
what a long-only trend system *should* do in a falling month.

## 2. Live vs backtest

| | Backtest (`backtest_reference`, live config) | LIVE, funded trades only |
|---|---|---|
| Trades | ~1,008 | **14** |
| Win rate | 45% | **14%** (2 of 14) |
| Expectancy | **+0.30R** | **−0.37R** |
| Profit factor | 1.58 | ~0.5 (+5.99R won / −11.23R lost) |
| Total | | **−5.24R** |

With 14 trades that is still a small sample. But a 14% win rate against a 46% expectation is a
~1–2% binomial tail, so "just unlucky" isn't a comfortable explanation.

`live_vs_backtest.py` reports n=130 / 4.6% win rate because it counts 116 never-filled
CANCELLED signals as 0R trades. That's the same KPI bug as the retro panel (guard spec G6.5).
The honest live sample is **14**.

## 3. The decisive test: was it the signals or the execution?

All **81 distinct approved signals** (instrument × day, funded or not) were replayed at their
**own signal entry/SL/TP**, the way the backtest fills, and resolved on daily bars to their
horizon:

| | n | avg R | win% | total |
|---|---|---|---|---|
| Jul signals | 19 | −0.00 | 37% | 0.0R |
| Aug | 20 | −0.28 | 20% | −5.7R |
| **Sep** | **37** | **−0.62** | 16% | **−22.8R** |
| Oct | 4 | 0.00 | | 0.0R |
| Nov | 1 | −1.00 | | −1.0R |
| **All** | **81** | **−0.37** | **22%** | **−29.5R** |
| The 14 funded, at **signal** price | 14 | −0.39 | | **−5.5R** (actual fills: −5.24R) |

**Execution didn't make it worse.** Real fills came out 0.3R *better* than signal prices. The
losses are in the signals themselves. The 88 entries that never filled (§4.1) were mostly
September signals, so that outage *saved* about 20R.

## 4. Execution facts (real, but not the main cause)

### 4.1 88 of 132 approved signals never reached a position
Aug 11 → Oct 1. Cause: the S2 bug (a GTC market parent is discarded by IBKR along with its
children; fixed 2026-10-01) plus 7 weeks of gateway instability (GATEWAY_AUTOLOGIN_SPEC §1:
12h, 4h46m and 40.5h dark windows). From Oct 1 to Nov 20 only **6 signals** were generated at
all (§6).

### 4.2 Entry timing changes the trade geometry
Fills landed 0–10 days after the signal. Slippage at fill ranged from **−0.68R to +0.90R**
(mean −0.04R, so not systematic):
- QQQ #141 (filled 5 days late) and DIA #143 had their stop squeezed to **0.90% / 0.53%** from
  the fill, and both stopped out **the same day**.
- DIA #139 filled +0.90R above its signal, so its fixed TP paid **+1.1R** instead of +3R.

The backtest has none of this because it fills at the signal close.

### 4.3 The journal is blind to real fills
Three trades the journal marked **VOID** really filled: AMLP 21 and CPER 84 on Jul 13, and
SPY 4 on Jul 31. They were later sold at a profit (≈ +HKD 3k together), and none of that is in
the journal's R. Their leftover stops are what later opened the AMLP, CPER and SPY shorts.

### 4.4 Size caps make R ≠ dollars
Risk budget is 1% NAV (~USD 360). The 25% position cap bound on most trades, so real dollar
risk per trade was 1–65% of budget (EEM #27 was **1 share**). −5.24R cost only about USD 464
(≈ HKD 3.6k). The backtest reference was computed at `pos_cap=0.30`, while live runs
`ETF_POS_CAP=0.25`.

## 5. The cash shield earned ~nothing

SGOV made **+HKD 99** in total on HKD 150–300k parked:

| | HKD |
|---|---|
| Dividends (gross) | +804 |
| US withholding, **30%** on SGOV distributions | −241 |
| Commissions, **55 SGOV trades** (35 in August alone; 1,793 bought / 1,409 sold to end at 384) | −429 |
| Price/timing | ≈ −35 |

Most SGOV distributions are interest-related dividends that non-US holders can often receive
without withholding. Worth checking the W-8BEN/QII treatment with IBKR. The churn is the
rebalance-every-cycle design (`CASH_SWEEP_MIN_USD` 1,500, 5-min cooldown).

## 6. What went right
- ~~**The LLM veto works.** The 96 signals it blocked would have lost −79.5R.~~ **CORRECTED
  2026-11-21 ([RETRO_LLM_2026-11-21.md](RETRO_LLM_2026-11-21.md)):** that replay never scored
  the *approved* signals. Replayed identically, they lost just as much (LIVE −0.661R approved vs
  −0.656R vetoed). The veto has had **no measurable effect**, good or bad, and it rarely blocks
  anything for long: it re-approves the same ETF ~0.5h later.
- **Long-only sat out November** (SPY −8.7%). The October–November signal drought is mostly
  `trend strength 4 < 5` and the short side being disabled, which is the system declining to
  buy a falling market as designed.
- Detection is now honest (guard phase 1), and the order path is guarded (phase 2, pending
  its PAPER week).

## 7. Why the signals underperform: hypotheses, ranked

| # | Hypothesis | Evidence | How to settle it |
|---|---|---|---|
| **H1** | **Live scores the unfinished weekly bar.** `providers.get_history()` uses yfinance `interval="1wk"`, whose last bar is the *in-progress* week. The backtest only sees completed weekly closes. Live therefore fires "breakouts" mid-week that may not survive to Friday. | Signals are spread evenly over weekdays (Mon 21, Tue 20, Wed 23, Thu 34, Fri 32). A completed-bar system would cluster at the weekly close. | Re-score the 81 signals with the in-progress bar dropped. Fidelity spec F1. |
| H2 | **Regime.** SPY topped Oct 6 (779) after a choppy September, which is classic trend-following whipsaw. September alone is −22.8R. | Losses concentrate in Aug–Sep on DBC, QQQ, CSPX and SHY re-tries. | Where does a 4.5-month, 81-signal window at −0.37R sit in the backtest's own rolling distribution? F2. |
| H3 | **Backtest overfit.** Many gates (re-entry, RSI, pullback band, VIX horizon, sleeve) were each "backtest-validated" on the same history. | 1,008 trades over ~20 years of weekly data with several tuned knobs. | Walk-forward / out-of-sample re-validation. F8. |
| H4 | **Live-only layers differ from the backtest.** LLM veto, tech-pause, 60-min cooldown, partial-week execution window. | The veto helps (§6); the others are untested against the backtest. | Shadow ledger by gate. F3. |

H1 is cheap to test and, if true, means **LIVE has not been trading the strategy that was
backtested**. That would also explain why the losses aren't execution.

### 7.1 Results (F1 + F2, run 2026-11-21, same day)

Both checks use the repo's own `compute_facts` → `score_from_facts` → gates and
`research/backtest._signals` on the same yfinance weekly data. Replays use unadjusted daily
bars, matching the journal's raw prices.

**F2: the backtest itself lost in this exact window.** Running the backtest's own signal
logic over Jul 1 → Nov 20, 2026 produces **33 signals at −0.43R (−14.1R)**: QQQ, CSPX, CPER,
EFA, DIA and AMLP stop-outs, nearly the same names live lost on. Over its full history
(1,020 signals since 2001, +0.30R) the backtest has 786 rolling 140-day windows: median
+0.31R, 5th percentile −0.46R, 1st −0.81R, and the worst at −1.01R (May–Jun 2015). LIVE's
signal-level result, one signal per instrument-week (n=50, −0.32R), sits at the **9th
percentile** of those windows, and the 2.5th percentile of rolling 30-signal windows.
**Verdict: a bad regime the backtest has seen before (about 1 window in 11), not proof the edge
is gone.** It isn't proof the edge is intact either; n is small. F7/F8 still apply.

**F1: H1 confirmed, and the partial-week signals were the worst ones.**

| Live signals (81) | n | avg R | win% | total |
|---|---|---|---|---|
| **A.** also present at the prior completed weekly close (backtest-consistent) | 65 | −0.29 | 25% | −18.6R |
| **B.** only on the in-progress week (the backtest would not take them) | 16 | **−0.68** | 12% | **−10.9R** |
| Per instrument-week: A / B | 38 / 12 | −0.24 / −0.58 | | −9.1R / −6.9R |

13 of the 16 B signals were **blocked at the completed close by the RSI>70 overextension
filter**. A mid-week dip cooled RSI just enough to pass, which means buying the first wobble of
an over-stretched move. 74 of 81 signals reproduce exactly when re-scored on the partial bar at
the live entry price, so the replay matches what live did.

**Putting it together:** of LIVE's −29.5R signal-level loss, about **−11R came from
partial-week signals** that a faithful implementation wouldn't have taken. The rest, about
−19R (−0.29R/signal), is about what the backtest itself did in this window (−0.43R). Execution
added nothing (§3), and the size caps kept the dollar damage small (§4.4). Fixing F1 is
justified. Expect the strategy to keep having stretches like this; that's what the F7 rule is
for.

## 8. Decisions / follow-ups
- Spec: [IBKR_STRATEGY_FIDELITY_SPEC.md](IBKR_STRATEGY_FIDELITY_SPEC.md) (F1–F8). The guard
  spec's unbuilt phases (3–5) are refined there too (§9 of that spec).
- **Open user decision:** until F1/F2 report, should LIVE keep opening new trades at all, or run
  signal-only (shadow) with capital parked? The spec proposes shadow mode as the default for
  the investigation, but this is the user's call.
