# Project Handoff — D:\quant quant trading platform

**Purpose of this doc:** let a new session continue the work without prior context.
Last updated 2026-07-13.

---

### 🔧 FIXED 2026-07-13: LLM board scan only reviewed the top 10 of 22 watched ETFs, not all
of them -- the "rest are clear WAIT/WATCH" assumption was false
User asked (after the prior "day-batched call" phrasing was ambiguous) whether the scan really
only runs once a day, and whether all watched instruments get checked. Both were worth
correcting -- one was a wording problem, one was a real gap.

**Cadence: NOT once a day.** "Day-batched" in the earlier explanation meant "one call covers
the WHOLE BOARD" (batching ACROSS INSTRUMENTS, per `board_scan.py`'s own docstring: "Instead of
4 calls x N instruments, the whole board costs a single structured-output call"), not "batched
once per day." The actual cadence is `SETTINGS["llm_min"] = 15` minutes (`app.py`), confirmed
against real data: 13 board scans occurred between 04:00-07:09 UTC today, ~14.5min apart on
average. Poor phrasing on my part, not a code issue -- no fix needed here, just the correction.

**Coverage: a real gap, confirmed against real data before fixing.** `board_scan.py`'s
`MAX_INSTRUMENTS = 10` sliced the ranked list to only the top 10 by `obviousness` before
sending to the LLM, with a comment claiming "the rest are clear WAIT/WATCH." Checked this
directly: today, **EFA, HYD, HYG, and SHY all had a real deterministic BUY/SELL signal** (they
were rejected on a DIFFERENT gate -- trend-strength/RSI -- meaning `action` HAD resolved to
BUY/SELL) but were NOT among the 10 sent to the LLM that scan. These 4 were evaluated with
`llm_sig=None`, falling back to `action = score.signal` in `evaluate_signal()` with none of the
LLM's news-awareness or "signals conflict/overextended" judgment applied -- the exact opposite
of the cap's own stated assumption.

**Root cause of the cap**: a stale token-budget worry ("free tiers cap at ~4k") that doesn't
apply to this deployment's actual configured model, `OPENAI_MODEL=gpt-5-mini` (checked
`analyst/.env` directly) -- a large-context model, not a free-tier one. **Fix**: raised
`MAX_INSTRUMENTS` from 10 to 40 (comfortably covers the current 22-ETF universe with headroom
for growth) so every watched instrument gets a real LLM look every scan. Cost is unaffected in
the way that matters -- `store.can_call()`'s daily budget guard caps the number of CALLS, not
per-call size, so this doesn't add API calls, it just makes the one call already being made
actually cover the whole book. Compiled clean, full test suite re-run (all 9 files pass, no
test asserted the old cap value). No config-value regression test added -- this is a single
tuning constant, not logic with edge cases to regress-test.

### 🔧 FIXED 2026-07-13: LLM WAIT-vetoes of a real deterministic BUY/SELL were invisible to
the retrospective/constraint scorecard
User asked what could cause the LLM layer specifically to reject a signal, beyond the plain
deterministic checks (trend strength, RSI overextension) -- then asked to make sure the
retrospective actually tracks those LLM-driven reasons. It didn't, for the most important one.

`evaluate_signal()`'s first gate resolves `action = llm_sig.action if llm_sig else
score.signal`, and returns early with the single reason `"action is WAIT/WATCH"` whenever
that's not BUY/SELL. `place_from_state()` then explicitly filters this exact string out before
logging to the rejected_signals journal ("skip WAIT/WATCH noise" -- most of the 22-ETF book
sits at WATCH on any given day with no real setup, genuinely uninteresting). But this one
reason string covers TWO very different cases: (a) the deterministic scorer itself never found
a real setup (`score.signal` is WATCH) -- correctly noise; (b) the deterministic scorer found a
real BUY/SELL and the LLM actively vetoed it to WAIT -- a news veto, its own overextension
judgment, or a low-confidence calibration (see `board_scan.py`'s system prompt: "WAIT is
correct when signals conflict or a trend is overextended... never overstate confidence"). Case
(b) is the ONE channel where the LLM's news-awareness (the deterministic scorer has zero) can
override a signal, and it was being silently discarded identically to case (a) -- meaning
neither `journal.rejection_counts()` (the retrospective's constraint scorecard) nor the
Criteria-loosening section had ever recorded a single one of these, however many actually
occurred historically.

**Fix**: `evaluate_signal()` now checks whether `score.signal` was actually BUY/SELL AND
`llm_sig.action == "WAIT"` before falling through to the generic label -- when true, returns a
distinguishable reason (`"LLM vetoed to WAIT (deterministic was BUY): <its own rationale>"`)
instead. `place_from_state`'s existing exact-match filter (`reasons != ["action is
WAIT/WATCH"]`) then naturally lets this through unchanged, no further edit needed there. Added
a canonical `journal._GATE_PREFIXES` entry ("LLM vetoed a deterministic BUY/SELL to WAIT") so it
aggregates cleanly in the scorecard instead of fragmenting by rationale text. New regression
test `test_evaluate_signal.py` (6 checks: plain-noise case unchanged both with and without an
agreeing llm_sig, BUY-vetoed and SELL-vetoed cases produce the distinguishable reason, the
journal canonicalizes it correctly, and an LLM-agreeing BUY still passes the gate cleanly).

**Checked against today's real data first, before writing the fix**: 2026-07-13's 84 (live) /
91 (paper) rejections were both 100% the two plain-deterministic reasons (trend strength, RSI)
-- none were LLM WAIT-vetoes today, so this fix has nothing retroactive to surface yet, but the
next time a news-driven or overextension-judgment veto fires, it will now show up correctly in
the constraint scorecard and Criteria-loosening sensitivity sections of `retrospective.py`
instead of vanishing into the "no confluence"-adjacent noise bucket.

### 🎮 2026-07-12: scheduled-task hardening applied + game-experience CPU-priority investigation
User ran the previously-blocked scheduled-task fix themselves from an elevated PowerShell
(`ExecutionTimeLimit=PT0S`, `RestartCount=3`, `RestartInterval=PT1M` on both `DashboardApp` and
`DashboardAppLive`) -- confirmed applied, both tasks show `Running`.

**Then asked to ensure this doesn't interrupt gaming.** Investigated CPU priority end to end:

1. **Confirmed both tasks already run at Priority=7 (BelowNormal) at the Task Scheduler level**,
   and `ui.run(..., show=False)` already prevents any auto-launched browser window stealing
   focus. `_tick()`'s internal `cheap_min`/`llm_min` throttling (see the 2026-07-12 tick-loop
   audit above) means the 30s outer timer doesn't translate into continuous CPU load either.

2. **Found a real gap: the IBC Gateway java.exe process runs at Normal priority, not
   BelowNormal**, because it's launched via `wscript.exe` from inside a `Start-Job` worker
   process (both `run_dashboard_live.ps1` and `dashboard.ps1`) -- neither hop explicitly
   requests a priority class, and the task-level Priority=7 setting doesn't reach a process
   this many hops downstream. Confirmed directly via `tasklist`/`Get-Process`.

3. **Tried fixing it after the fact -- denied both ways.** `.NET`'s
   `$proc.PriorityClass = 'BelowNormal'` → "Access is denied". WMI's
   `Win32_Process.SetPriority()` → ReturnValue 5 (also denied), even from an elevated session.
   This is the SAME class of restriction the codebase already worked around once before for
   `PROCESS_TERMINATE` (`Kill-ProcessHard` uses WMI's `Terminate()` because `Stop-Process`
   fails the same way) -- but that workaround does NOT extend to `SetPriority`.

4. **Fixed via launch-time inheritance instead of after-the-fact modification.** Verified
   directly (isolated `Start-Job` test): Win32 `CreateProcess` defaults a new child to
   `NORMAL_PRIORITY_CLASS` only when the CREATING process is itself Normal-or-higher; if the
   parent is already BelowNormal/Idle, the child inherits that SAME lower class by default,
   with no special access rights needed. Added `(Get-Process -Id $PID).PriorityClass =
   'BelowNormal'` at the top of the `$mon` Start-Job scriptblock in both scripts (before it
   spawns `wscript.exe`), so every descendant it creates from then on should inherit
   BelowNormal for free.

5. **This confirmed to work for the dashboard.app python process itself, but NOT (yet)
   confirmed all the way through to java.exe.** Testing after redeploy found `DashboardApp`
   (paper)'s python process correctly inherited BelowNormal, but `DashboardAppLive`'s did not --
   traced to a REAL, previously-undocumented asymmetry: **`DashboardApp` runs at RunLevel
   `Highest` (elevated) while `DashboardAppLive` runs at RunLevel `Limited` (standard)** --
   discovered as a byproduct of this investigation, not something either of us had verified
   before. Not changed here (an elevation-level asymmetry between the paper and live tasks is a
   real-money-adjacent setting worth a deliberate decision, not a silent fix) -- flagged for the
   user to decide whether this is intentional. The java.exe gateway's priority through the
   `wscript.exe -> cmd -> IBC's StartGateway.bat -> java` chain remains UNCONFIRMED for live;
   modifying IBC's own `StartGateway.bat` to force it (the file's own header says "PLEASE DON'T
   CHANGE ANYTHING BELOW THIS LINE") was deliberately NOT attempted -- the risk of breaking a
   real-money gateway's startup outweighs a CPU-priority nicety.

**Practical risk assessment, since full verification hit a genuine wall**: even where java.exe
ends up at Normal priority, Windows' own foreground-boost scheduling gives temporary priority
preference to whichever window has focus (the game) regardless of a background process's
static priority CLASS -- and the gateway itself is a lightweight, mostly-idle account-polling
process (bursty, not sustained CPU load), not a compute-bound worker. The combination of
Priority=7 at the task level, `show=False` (no window-stealing), the already-throttled tick
cadence, and now explicit BelowNormal on the job's own process tree covers the great majority
of the real risk even where the java.exe leg couldn't be conclusively verified.

## ⭐⭐⭐ ADOPTED PLAN & FIGURES (single source of truth, 2026-06-25)

**LIVE on IBKR paper since 2026-06-24** — `BROKER=ib` + `UNIVERSE=etf`, account DUK968178
(~US$130k / 1.01M HKD PAPER default), port 4002. Restart: `Stop/Start-ScheduledTask DashboardApp`.

**⚠️ REAL starting capital = 100K HKD (~$12.8k) + 30K/mo contributions** (NOT the 1M paper default;
clarified 2026-06-29). Implications at this size: (1) **ENABLE FRACTIONAL SHARES on IBKR — mandatory**,
else 0.5%-risk positions across 18 ETFs round to 0 shares & the book degrades; keep ALL 18 (don't
shrink the universe — breadth IS the edge). (2) **Savings rate dominates:** 30K/mo on 100K = ~360%/yr
contribution yield; investment return = only ~6% of yr-1 wealth growth (rising to ~21% by yr5). All
strategy optimization adds ~+1,500 HKD/yr at 100K = rounding error vs contributions → the edge barely
matters until the base grows. (3) **Panic-MR sleeve OPTIONAL until ~500K** (its +1.5pp ≈ +1,500 HKD/yr
negligible early + adds commission-sensitive fills); run core-only first for simplicity. (4) Commissions
~0.22-0.29% of yr1-avg balance (minor; IBKR fixed/tiered, don't over-trade). (5) **Cash: hold USD
(auto ~3.1%), skip SGOV sweep until ~$75-100k NAV** (T+1 friction not worth it on a tiny contribution-fed
acct). (6) **US estate-tax $60k line crossed at ~month 12** (~470K HKD) — US-domiciled ETFs >$60k =
US-situs, up to 40% on a HK NRA's death; consider Irish-UCITS for long-held core equity then, but bonds/
commodities/REITs have no clean UCITS equiv → **revisit at that ~470K HKD/month-12 point, not ~$130k**
(2026-07-08 fix: the old "$130k" trigger here was just reusing the doc's other scale-milestone number
and doesn't match this section's own $60k/month-12 math — over a year too late for an irreversible
40%-on-death exposure; keep "not an immediate switch" since the live account is ~$1.3k today, ~47x
away, but the review date is month-12/~470K HKD, not $130k). (7)
Account self-bootstraps to ~$130k (planned scale) in **~2.3y**, when the full sizing analysis applies.
DD trivial early (−11% ≈ −11K < half a month's contribution). **Real lever = relentless contributions, not bps of edge.**

### ⭐ FINAL SYSTEM + PERFORMANCE + GO-LIVE (2026-06-30, session close)
**Performance (30.3y, cash@4.3%):** core 18/17-ETF **+7.0% CAGR / −9.7% DD / Sharpe 1.22**; + optional
panic-MR sleeve (SPY+QQQ+XLK) **~+8.7% / −10 to −11% / ~1.25**. % is scale-invariant (holds at 100K).
**Layers:** (1) core weekly TSMOM 0.5%, weekly bars, ATR-SL+RR3-TP, long-only; (2) cash shield idle→USD
(~3.1%)→SGOV (`CASH_USD`/`CASH_SWEEP` live); (3) panic-MR sleeve — **DEFERRED until ~500K** (core-only at
100K; +1.5-2pp but ~rounding-error vs contributions); (4) contribute 30K/mo, stay fully invested (do NOT
hold cash to time market — proven to lose); (5) guardrail: halt new entries if DD>−13%, expR-sign at n≥30.
**Backtests done this session (all in `dashboard/research/`, verdicts in the sections below):** sector_mr
(REJ), spy_dipbuy (the survivor), short_vol iron-condor (REJ), earnings_vol_crush (REJ), dipbuy_sizing/
blend/refine/refine2/refine3 (sleeve tuned→SATURATED: SPY+QQQ+XLK, ADX>20, 0.5%/1%@VIX>30 cap 1%, base
exit), core_ops (vol-target+monthly REJ; VIX-timed contributions REJ on cash-drag), corr_penalty (INERT),
refined_statarb (REJ, DSR≤17%), cost_sensitivity (cost already modeled; limit-orders ~+0.1-0.2% max).
**NEW FEATURE — SGOV-first withdrawal helper** (`ib_exec.prepare_withdrawal` + `broker` dispatch + app.py
header **Withdraw** button/dialog, commits 457a68d/1272a11/05f35a6): frees cash from cash-shield (idle USD→
SGOV) FIRST never Core; earmarks a **reserve** `sweep_cash` excludes (so it won't re-buy); does NOT move
money out (manual IBKR action). Paper dry-run verified ($5k→0 SGOV; $35k→99 SGOV, Core untouched).
**GO-LIVE checklist (real acct, 100K HKD):** ✅ MUST: **enable fractional shares** (user HAS "Global
(小數股) - Stocks" ✓), **US-Stocks permission** ✓, **API write** (`ReadOnlyApi=no` ✅ set), **FX/Forex
permission** for HKD→USD IDEALPRO conversion (⚠️ NOT in user's stock-only list — verify), **Margin (Pro)
account** (avoids T+2 settlement friction). The three NEW US-stock sub-requests (Algo venues / T+0 / T+1
settlement) are **NOT required** by this strategy (simple bracket orders, multi-day holds) — tick for free
optionality or skip. Then flip config to live (port 4001, live login, relax the ib_exec paper guard — a
deliberate real-money decision). **API can't verify LIVE permissions** (gateway is paper-only + IBKR has no
permissions-list endpoint) — run `preflight_check.py` against the live gateway at go-live + place ONE real
fractional-bracket paper order first (the one silent-failure risk: stops on fractional lots).

### ⭐ ADOPTED PLAN + 2-PHASE AUTO-SWITCH (2026-07-01) — the live config
**⭐⭐⭐ UPDATED 2026-07-11 — the live config now has FOUR parameters, not two, and the expected
figures below are superseded.** `RISK_PER_TRADE` + `ETF_POS_CAP` (original two) + `PORTFOLIO_CAP
=1.0` (aggregate gross-exposure cap) + `DD_HALT_PCT=-13.0` (pauses new entries only, below this
drawdown) all run together in `ib_exec.py` -- do NOT read this section without also reading
"PORTFOLIO_CAP -- aggregate gross-exposure cap" and "DD-halt gate" further down. **Only the
first three affect the backtest numbers** (`DD_HALT_PCT` is a live-only execution safety net --
the backtest has no live drawdown state to halt on, so it changes zero backtest figures; don't
look for its effect in any performance table). Current honest performance expectation (33.4y,
current cash-yield/margin-debit model, `RISK_PER_TRADE`+`ETF_POS_CAP`+`PORTFOLIO_CAP` only):
**Full ~5.8% CAGR / −6.8% maxDD, Sharpe ~1.19; OOS (recent decade) ~11.4% CAGR / −5.0% maxDD,
Sharpe ~1.61** -- meaningfully better risk-adjusted (Sharpe +24% full / +31% OOS) than the
pos-cap-only config below, which is now the OUTGOING baseline, not the target. `2x risk`,
`portfolio_cap 105%`, AND `portfolio_cap 80%` were all tested as follow-ups and REJECTED (worse
on OOS risk-adjusted metrics in every case) -- see those entries for the numbers. Original
(2026-07-01) text preserved below for the reasoning trail.

**Settings: `RISK_PER_TRADE=0.01` (1%) + `ETF_POS_CAP=0.25`** (both live defaults; risk% also
persisted in ui_settings). 1% risk "fills" the 25% cap on high-vol names; the CAP is the real
return/DD dial (risk% is inert once the cap binds; strategy-only Sharpe flat ~0.88 at any cap).
Expected @25%: **~7.5% CAGR / −8.8% DD blended (SGOV cash)**, strategy-only ~5.7% / −10.5%.

**⭐ VERIFIED 2026-07-08 on the CURRENT live setup** (21-ETF book, actual deployed
`ETF_POS_CAP=0.25` + `RISK_PER_TRADE=0.01`, not an older 17-ETF/estimated number) —
`--pos-cap 0.25`, full 33.4y history:

| | Full-history CAGR | Full maxDD | OOS CAGR | OOS maxDD |
|---|---|---|---|
| **Strategy-only (cash@0%)** | +5.7% | −11.1% | +13.8% | −9.3% |
| **Blended (cash@4.3%, today's rate)** | +7.3% | −9.6% | +14.8% | −9.3% |

Monthly: avg +0.86%/mo, std 2.58%, worst month −6.0%, positive 61% of months (blended). This is
the honest current-setup number to plan around — **use this table, not the 2026-07-01 one-liner
above**, which was pre-batch-3-6 (17 ETFs) and a rough estimate rather than a direct `--pos-cap`
run. Full-history is the conservative anchor; OOS (~13-14% CAGR) is the recent-regime, bull-
flattered case — don't plan around OOS alone (see the ~4-7%/~33y vs ~10%/~13y split earlier in
this doc for why).

**⭐ SUPERSEDED 2026-07-09** — 22-ETF book (ASHR added) + the dip-sleeve extended to 11 tickers
(both this date, see below), re-verified at the SAME actual settings (`--pos-cap 0.25`, 1% risk):

| | Full CAGR | Full maxDD | Sharpe | OOS CAGR | OOS maxDD |
|---|---|---|---|---|---|
| **Core only (22 ETFs, no sleeve)** | +8.28% | −9.6% | 1.00 | +13.07% | −9.6% |
| **+ dip sleeve @10% weight** | +11.96% | −9.5% | 1.26 | +16.63% | −9.5% |
| **+ dip sleeve @15% weight** | +13.78% | −9.8% | 1.26 | +18.39% | −9.8% |

The sleeve piece is PAPER-ONLY and backtested-only so far — zero live-observed trades yet on the
8 newly added tickers (DIA/IWM/HYG/EFA/EEM/VNQ/PFF/ASHR); don't treat it as confirmed until it's
actually traded a few real cycles. Core-only (no sleeve) remains what's live on both books today.

**⚠️ CORRECTED 2026-07-09 (same day) — the table above understated cost.** User asked "if cash
stays negative, does that hurt the account (margin interest)?" while looking at paper's
Projected-interest stat (verified correct: −HKD 1,265/mo on a −HKD 276,104 cash balance, 7
concurrent positions at 127% deployed notional). That question led to checking whether the
BACKTEST models this cost -- it didn't: `_portfolio()`'s idle-cash accrual (`--cash-yield`) did
`idle = max(equity - dep[0], 0.0)`, flooring at zero, so it credited interest on positive idle
cash but charged NOTHING when deployed notional exceeds equity (`dep[0] > equity`) -- which is
ROUTINE under `ETF_POS_CAP=0.25` + 1% risk once several positions are open at once (confirmed:
paper is at 127% deployed right now), not an edge case. **Every prior `--cash-yield` blended
CAGR figure in this project was overstated.** Fixed: added `MARGIN_DEBIT_RATE=0.055` (matches
`app.py`'s live dashboard rate) and charge it on the excess when `dep[0] > equity`. Re-verified
the table above with the fix:

| | Full CAGR | Full maxDD | Sharpe |
|---|---|---|---|
| Core only (before fix) | +8.28% | −9.6% | 1.00 |
| **Core only (after fix)** | **+7.14%** | **−11.3%** | **0.89** |
| +sleeve@10% (before fix) | +11.96% | −9.5% | 1.26 |
| **+sleeve@10% (after fix)** | **+10.78%** | **−10.8%** | **1.16** |
| +sleeve@15% (before fix) | +13.78% | −9.8% | 1.26 |
| **+sleeve@15% (after fix)** | **+12.58%** | **−11.1%** | **1.17** |

Roughly 1-1.4pp lower CAGR and 1-1.7pp worse DD across every scenario. The sleeve's qualitative
conclusion survives (Sharpe still improves 0.89→1.16-1.17 at 10-15% weight) -- **this is the
current honest number to plan around**, not the table above it. 10-15% sleeve weight remains the
sweet spot (same shape, just uniformly worse than previously stated).

**Two phases, auto-switched by equity** (`paper.account_phase()` / `sleeve_active()`, threshold
`PHASE2_NAV_USD`=$64k ≈ 500K HKD; UI shows a Phase badge):
- **Phase 1 (<500K): core 22-ETF only** (17 orig. + CWB/VNQI/AMLP/HYD/ASHR), 1% + 25% cap.
- **Phase 2 (≥500K): core + panic-MR sleeve** (11 tickers as of 2026-07-09 -- SPY/QQQ/XLK/DIA/
  IWM/HYG/EFA/EEM/VNQ/PFF/ASHR, ADX>20, 0.5%/1%@VIX>30), same cap/risk.
- **Phase 3 REJECTED** (loosening the cap = pure leverage: worse ratio 0.85→0.66, DD −8.8→−14%,
  trips the −13% halt tripwire; not worth ~+1.5pp nominal).
**⚠️ Sleeve ORDER EXECUTION is NOT built yet** — only the auto-switch GATE is wired (`mirror_new`
computes phase; the sleeve, when built, checks `sleeve_active(equity)` and turns on automatically at
500K, no manual step). Sleeve build is the remaining TODO, due before the account nears ~500K (~1yr).
Loosen the cap toward 25→33% later only as a conscious risk-appetite choice (raise the tripwire if so).

### ⭐ PER-POSITION NOTIONAL CAP (2026-07-01) — a REQUIRED safety fix + a de-leveraging dial (NOT alpha)
Found at go-live sizing: risk-based `size_shares` (risk÷stop) buys a huge share count on low-vol
ETFs to risk 0.5% — **SHY = 168sh / $13,793 = 108% of a $12.8k acct in ONE position**; all-18 =
376% notional = 3.8× leverage. Live path `_place_etf_bracket` had NO notional cap (backtest capped
`dep` at equity only for cash-interest, still booked full-0.5% risk). FIX: cap per-position notional
at a fraction of equity + scale risk down when it binds. Implemented `ETF_POS_CAP` env (**default
0.15**, 0=disable) in `_place_etf_bracket`; `research.backtest --pos-cap FRAC` reproduces. Verified:
SHY 168sh→31sh (20%) / →23sh (15%); SPY unaffected.
**⚠️ VERIFIED it is NOT a Sharpe win (retracts an earlier claim).** Sweep with cash@4.3% shows Sharpe
rising as cap tightens (none 1.19 → 15% 1.45 → 5% 2.27) — but that is a **cash-yield ARTIFACT**:
tighter cap = less deployed = more idle cash @4.3% (steady, ~0 vol) inflating the BLENDED Sharpe
toward the T-bill Sharpe. **Strategy-only (cash@0%) Sharpe is FLAT ~0.88 at every cap** (none 0.90,
20% 0.87, 15% 0.89, 10% 0.89, 5% 0.87) while CAGR & DD shrink proportionally (5% cap = 1.19% CAGR /
−2.2% DD strategy-only = engine mostly off). So the cap is a **leverage dial like risk%**, not alpha.
10%/5% = "T-bill fund + sprinkle", capital-inefficient — do NOT chase the pretty Sharpe.
**1% risk ≈ 0.5% once the cap binds** (1%+15% strat-only 3.52%/−6.4%/0.88 ≈ 0.5%+15% 3.35%/−6.4%/0.89
— cap dominates all but the few high-vol names) → keep **0.5% risk**.
**ADOPTED: ETF_POS_CAP=0.15 @ 0.5% risk.** Numbers: strategy-only ~3.35% CAGR / −6.4% DD; blended
w/ SGOV cash ~5.9% / −4.6%. Rationale: 15% removes over-leverage (safety) AND is a conservative
de-lever (lower DD for less return) — fine at 100K where contributions dominate & DD is trivial.
Loosen toward 20-25% later for more return once the base grows (all safe; ≤~40% prevents over-leverage).

**⚠️ CORRECTION (2026-07-08): this 0.15/0.5% decision was written here but NEVER actually applied
to the running system** — found while fact-checking a user critique that assumed 0.15/0.5% was
live. Verified ground truth: `ETF_POS_CAP` has no override anywhere (not in `dashboard.ps1`,
`run_dashboard_live.ps1`, or any machine/user/process env var) → falls through to the code default
**0.25** (`ib_exec.py`); `risk_per_trade` in BOTH `dashboard/dashboard.db` and `dashboard_live.db`'s
persisted `ui_settings` reads **0.01 (1%)** — nobody ever flipped the UI toggle to 0.5% either. So
the system has been running the EARLIER "2-PHASE AUTO-SWITCH" settings (line ~56 above:
`RISK_PER_TRADE=0.01` + `ETF_POS_CAP=0.25`) the whole time, not this later "adopted" 0.15/0.5%.
**That's actually the better outcome** — its own expected numbers (line ~59: ~7.5% CAGR/−8.8% DD
blended, ~5.7%/−10.5% strategy-only) are meaningfully better than this section's 3.35%/5.9% figures,
and 25% is already the cap this section said to "loosen toward... later." No code change made (the
better config is already live) — this note exists so nobody replans around the stale 3.35%/5.9%
numbers again. If a MORE conservative cap is ever wanted, it needs an explicit choice + code change
(set `ETF_POS_CAP=0.15` in both `.ps1` launch scripts + flip the risk toggle to 0.5% in the UI) —
not a rerun of this backtest, since the number was already computed correctly here.

### 🐞 FIXED 2026-07-02: mode-switch showed PAPER trade history/stats after switching to LIVE
Root cause: `paper._DB`/`store._DB` (journal `paper_trades`, `ib_mirror`, cache/settings incl.
`ui_settings`, `withdraw_reserve_usd`, `equity_history`) were ONE fixed file (`dashboard.db`) —
switching mode only relabelled the UI, both modes read/wrote the SAME database. FIX: separate
databases per mode — `dashboard.db` (paper, existing/untouched) vs `dashboard_live.db` (live, fresh/
empty). `DASH_DB_NAME` env picks the file; `app._resolve_mode()` sets it BEFORE importing anything
DB-touching. The mode POINTER itself (which mode is active) lives in an ALWAYS-FIXED separate file
(`dashboard_mode.db`, via new `store.get_mode()`/`set_mode()`) — otherwise there's a chicken-and-egg
(need to know the mode to find the file that says the mode). Also made `paper._DB`/`store._DB` LAZY
(module `__getattr__`, PEP 562) instead of import-time constants, so the path is always correct
regardless of import order (robust for other entrypoints too, not just app.py's exact sequence).
Verified: mode='live' -> both `paper._DB` and `store._DB` -> `dashboard_live.db` (fresh, empty);
mode='paper' (the real persisted state) -> `dashboard.db` (existing, untouched, all history intact).
Dashboard restarted clean (HTTP 200, paper mode, no errors). Live mode now starts with a CLEAN slate.

### ⭐⭐ BUILT 2026-07-02: panic-MR sleeve EXECUTION for PAPER (was gate-only before)
User asked (a) fix the misleading "Phase 2 · core + MR sleeve" badge (it only reflected the
equity threshold, not that anything was actually built/running) and (b) build the real sleeve
for the PAPER account specifically. Both done.

**(a) Badge fix:** now shows 3 honest states — "Phase 1 · core-only" / "Phase 2 threshold ·
sleeve NOT enabled" (equity qualifies but `SLEEVE_ENABLED` isn't set) / "Phase 2 · sleeve
ACTIVE" (both gates true). Can never again claim something isn't really running.

**(b) New module `dashboard/core/sleeve.py`** — the FINAL DIP SLEEVE SPEC (above), ported from
`dashboard/research/dipbuy_refine2.py` bit-for-bit:
- `entry_signal(ticker)`: close<20MA*0.975 & VIX/VIX[-5]-1>0.15 & RSI14<35 & ADX14>20 (SPY/QQQ/
  XLK). **Cross-validated against the backtest over FULL 33y SPY history: 182/182 entry dates
  identical, zero discrepancies** (`/tmp/entry_crosscheck2.py`, formulas copied verbatim from
  dipbuy_refine2.py). Also verified the live 1y-lookback window (vs the backtest's full-history
  EWM) produces numerically negligible drift (RSI diff 0.0, ADX diff 0.000003) — the shorter
  window used for live speed doesn't change the signal.
- Sizing: 0.5% base / 1.0% at VIX>30, stored in `entry_facts` JSON (NOT a global, since it
  differs per-trade and per-VIX-level unlike the core's flat RISK_PER_TRADE).
- **Exit design (hybrid, the key architectural insight):** +3%/-5% are REAL broker STP/LMT
  orders placed at entry (protects the position even if the app is offline — reuses
  `_place_etf_bracket`'s exact bracket mechanics via a new `_place_sleeve_bracket`). Only the
  two DYNAMIC conditions a static order can't express (5-day-MA touch, 10-trading-day cap) are
  checked daily by `should_exit_dynamic()`/`close_expired_sleeves()`, which cancels the
  outstanding bracket children and submits a market flatten (`ib_exec.manual_close_sleeve`) —
  the resulting close is then picked up by the EXISTING, already-fixed `sync_closures()`
  (method-agnostic, no changes needed there).
- Reuses core plumbing throughout: `paper.Trade`/`_insert`/`_has_open`/`_recent_close` for the
  journal (tagged `method="dipbuy-sleeve"`), `ib_mirror`, `sync_closures()` — new code is ONLY
  the signal math + the two sleeve-specific order functions.
- Independent double-gate: `SLEEVE_ENABLED` env (explicit opt-in, default OFF) AND
  `paper.sleeve_active(equity)` (size threshold) — BOTH required. `SLEEVE_ENABLED=1` set ONLY in
  `C:\Scripts\dashboard.ps1` (paper) — deliberately NOT in `run_dashboard_live.ps1`, so live
  needs its own separate go-live decision later, per the user's explicit "for paper" scope.
- Throttled to once/60min (`CHECK_INTERVAL_MIN`) — daily-bar signal, no need to hit yfinance on
  the 1-min cheap-refresh cadence. Wired into `refresh_cheap()` (LLM-independent — the sleeve
  needs no LLM), right after `sync_closures()`, exits-before-entries each cycle.
- `mirror_new()` routing fixed to actually dispatch sleeve trades: it previously filtered
  `t["method"] != MIRROR_METHOD` unconditionally, which would have SILENTLY DROPPED every
  sleeve trade (never mirrored to the broker) had this not been caught before shipping.

**Live-verified end-to-end (not just compiled):** (1) `broker.equity_usd()`/`sleeve_enabled()`/
`sleeve_active()` all correct against the real paper account ($129,326, Phase 2, enabled). (2)
`entry_signal()` for SPY/QQQ/XLK all correctly return None under current calm conditions
(matches the earlier live `evaluate_signal` check showing SPY/QQQ overextended, not oversold).
(3) **Placed ONE real tiny test order** (3sh SPY, 0.1% risk, clearly tagged "MECHANICAL TEST" in
rationale) via the actual `mirror_new()`→`_place_sleeve_bracket()` path — verified on IBKR: SL
$708.47 (exact -5%), TP $768.13 (exact +3%), qty correct for the risk budget, sitting
`PreSubmitted` (market closed at test time, same state as the 4 real core positions' resting
orders — expected, not a bug). Confirmed `manual_close_sleeve`'s cancel logic is correctly
scoped to the app's own persistent connection (cross-client-ID cancel friction during ad hoc
debugging was a test-script artifact, not a code bug). **Cleaned up completely**: cancelled the
3 test orders, removed the test `ib_mirror` row, archived+removed the test `paper_trades` row
(`paper.archive_trades([130])`, recoverable) — real account back to exactly the 4 genuine
positions, verified. Dashboard restarted (the actual scheduled TASK, not just the inner loop —
see the mode-switch lesson above) and confirmed badge shows "Phase 2 · sleeve ACTIVE", no errors
across multiple live refresh cycles.

**Status: LIVE on paper now.** Next real signal (SPY/QQQ/XLK panic dip, ~3-4x/yr per ticker per
backtest) will be placed automatically. Live-account activation is a SEPARATE, deliberate future
decision (add `SLEEVE_ENABLED=1` to `run_dashboard_live.ps1` only when ready).

### ⭐ FEATURE 2026-07-02: reset button for the Constraint scorecard (rejected_signals)
User request: "Constraint scorecard" (Retrospective tab) was showing 6,617 rows dating back to
2026-06-14 — mixing STALE pre-ETF-cutover reasons (e.g. "long-only: short side disabled" from the
old futures/FX config, now meaningless since LONG_ONLY is always true) with current ETF-era data,
making it useless for judging the LIVE config. NOTE: checked first whether the KPI cards (Expectancy/
Win-rate/etc, driven by CLOSED trades) also needed resetting — they didn't: 0 closed trades exist
(strategy holds 3-5wk, only 9 days live), so those were already at their zero starting state.
**Deliberately did NOT reuse the existing "Archive & reset" button** (`paper.archive_and_reset()`)
— that does `DELETE FROM paper_trades` unconditionally, which would have wiped the 4 REAL open
positions' journal rows too, orphaning them from `ib_mirror` and hiding them from Active Trades /
the dedup check (risk of the funnel placing DUPLICATE trades on the same 4 ETFs next cycle, right
after fixing the related ib_mirror-orphan bug above). Built a SCOPED equivalent instead:
`journal.archive_and_reset_rejections()` — same "archive, never delete" pattern (copies to
`rejected_signals_archive` tagged with a batch timestamp, then clears the live table) but touches
ONLY the audit log, never `paper_trades`/`ib_mirror` — open positions completely unaffected.
UI: "Reset" button next to the "Constraint scorecard" label (confirmation dialog, same style as
Archive & reset). **VERIFIED end-to-end:** archived 6,617 rows (recoverable in the archive table),
live table -> 0; then ran a REAL placement funnel cycle and confirmed 6 NEW rows recorded correctly
into clean current-era buckets only ("overextended entry", "trend strength below MIN_STRENGTH") —
confirms constraints keep updating live after a reset, no stale reasons leaking back in.

### 🐞 FIXED 2026-07-02: allocation pie chart was missing all 4 real ETF position slices
User reported the pie chart looked wrong. Root cause found via ground-truth IBKR query: **all 4
open positions (EEM/DBC/VNQ/CPER) were genuinely still held on the real account (confirmed
matching conIds/qty/avgCost), but `ib_mirror.status` had been incorrectly set to `'CLOSED'` for
all 4 rows**, while `paper_trades.status` correctly stayed `'OPEN'` — an inconsistent state.
`live_positions()` only queries `ib_mirror WHERE status='OPEN'`, so it returned `{}`, and the pie's
`positions` dict (sourced from it) silently dropped all 4 real positions -- showing only SGOV +
cash buffer, missing ~HKD 512K of actual holdings.
**Root cause in `sync_closures()` (ib_exec.py):** when a broker position-read comes back empty, the
old code marked `ib_mirror.status='CLOSED'` **unconditionally**, BEFORE checking whether
`_resolve_from_broker()` actually found a confirming closing fill. If the fill lookup returned
`None` (e.g. a TRANSIENT/incomplete `ib.positions()`/`ib.fills()` read right after a reconnect --
this session had several during testing/restarts) the mirror row was permanently orphaned as
CLOSED with no code path to ever reopen it, silently breaking `live_positions()` for that trade
forever, even though the account still held it and `paper_trades` still (correctly) said OPEN.
**FIX:** only commit `ib_mirror.status='CLOSED'` once resolution is actually confirmed (a real
closing fill found, `_resolve_from_broker()` returns a message) OR the paper side had already
resolved some other way; otherwise leave the mirror row OPEN and retry next cycle -- same
"uncertain -> don't commit, re-check" philosophy already used for the pending-order guard just
above it. **Data repair:** the 4 corrupted rows were reset `ib_mirror.status='OPEN'` (verified
safe via a direct ground-truth query against the live IBKR paper account first). **VERIFIED
end-to-end:** `ib_exec.live_positions()` now returns all 4 positions correctly; the dashboard's
own cached `portfolio_snapshot` picked them up on its next refresh after reconnecting (its own IB
connection had also transiently dropped/reconnected during this testing, WinError 1225 ->
recovered, clientId=7); pie-chart math reconciles (4 ETFs ~HKD 512K + SGOV ~HKD 294K + cash ~HKD
196K ≈ NetLiq ~HKD 1.01M, matching GrossPositionValue within FX-rounding).

### 🐞 FOLLOW-UP FIX 2026-07-02: PAPER endpoint could self-flip to LIVE via the shared mode pointer
Even after the DB-separation fix above, `quant.carsonng.com` (port 8080) briefly showed the LIVE
account's near-empty state ("stats gone") because: the shared `store.get_mode()`/`set_mode()`
pointer (`dashboard_mode.db`) had been set to `'live'` by an earlier test click, and `dashboard.ps1`'s
new `$env:DASH_FIXED_MODE = "paper"` pin (added to the FILE on disk) had NOT yet taken effect — the
scheduled task's OUTER PowerShell process was still the one from BEFORE that edit (only its INNER
`python -m dashboard.app` child gets relaunched by the watchdog loop; the outer process's env is fixed
for its own lifetime and doesn't re-read the .ps1 file). So the next Python restart fell through to
the shared pointer (`'live'`) instead of the intended hard pin. **FIX: restarted the actual scheduled
task** (`Stop/Start-ScheduledTask DashboardApp`, not just the inner loop) so the outer process re-runs
`dashboard.ps1` top-to-bottom and `DASH_FIXED_MODE=paper` is set BEFORE Python ever starts — this
pin now takes ABSOLUTE priority over the shared pointer in `app._resolve_mode()`, so a stray click or
leftover pointer value can never again flip the paper endpoint. **VERIFIED:** `dashboard.db` mtime
advancing every refresh cycle (paper data live/growing) while `dashboard_live.db` mtime is FROZEN (no
leakage); `portfolio_snapshot` cache shows NetLiq **HKD 1,008,554** (matches paper DUK968178), not the
live account's ~HKD 40. **Lesson: after editing dashboard.ps1 / run_dashboard_live.ps1, the SCHEDULED
TASK itself must be restarted (Stop/Start-ScheduledTask), not just the app** — env vars set at the top
of a long-running watchdog script only take effect once, at that process's own startup.

### ⭐⭐⭐ FINAL WORKING STATE (2026-07-03) — concurrent paper+live, BOTH publicly reachable
Superseded the single-endpoint idea below (2026-07-01) back to full concurrency (see "CONCURRENT
PAPER + LIVE" further down) — both run simultaneously, isolated, each on its own hostname:

| | Paper | Live |
|---|---|---|
| Local port | 8080 | 8081 |
| Public URL | https://quant.carsonng.com | **https://quant-live.carsonng.com** |
| IB port / account | 4002 / DUK968178 | 4001 / U12991898 |
| Database | dashboard.db | dashboard_live.db |
| Launcher | `C:\Scripts\dashboard.ps1` (task `DashboardApp`) | `run_dashboard_live.ps1` (task `DashboardAppLive` — ⚠️ registration pending: needs an ELEVATED PowerShell to run `Register-ScheduledTask -TaskName 'DashboardAppLive' -Xml (Get-Content 'C:\Scripts\DashboardAppLive.xml' -Raw) -Force`; running as a plain background process meanwhile, survives until next reboot/logoff only) |

**Public hostname is `quant-live.carsonng.com` (2nd-level), NOT `live.quant.carsonng.com` (3rd-level)
— the latter is PERMANENTLY BROKEN, do not use it.** Root cause: Cloudflare's automatic Universal
SSL wildcard only covers `*.carsonng.com` (one level) — a 3rd-level subdomain gets NO certificate
(`SSL alert 40 handshake_failure`, confirmed not a propagation issue after 35+ min). A 2nd-level
name fits the existing wildcard and works immediately (`CN=carsonng.com`, verified `Verify return
code: 0`). Fixed everywhere: `~/.cloudflared/config.yml` ingress + DNS route, `run_dashboard_live.
ps1`'s `LIVE_URL`, `C:\Scripts\dashboard.ps1`'s `LIVE_URL`, `app.py`'s `LIVE_URL` default. (The old
`live.quant.carsonng.com` DNS CNAME still exists, unrouted/pointless — harmless, not cleaned up.)

**Live gateway 2FA gotcha (2026-07-03):** first attempt's push notification wasn't approved within
IBKR's ~6min window → the Gateway silently reset to a blank, unauthenticated login screen (NOT an
error dialog — `open_application`+screenshot was needed to see this, logs alone were ambiguous:
"Re-login... not required" sounds like success but isn't). IBC's credential auto-fill only runs
ONCE per launch and does not retry after a timeout. Fix: kill the whole process tree (java + its
DisplayBannerAndLaunch.bat wrapper) and relaunch `C:\IBC-Live\StartGateway.bat` fresh for a new
push — worked on the second attempt (verified: connected, account=U12991898, NetLiq=HKD 40).
**Lesson for next cold-start:** if the Gateway doesn't come up within ~2min of launching, assume
the push timed out/wasn't seen — don't keep waiting, kill and relaunch immediately.

**Lesson reinforced:** editing a .ps1 launcher file does NOT affect an already-running process
spawned from it (env vars are fixed at that process's own startup) — restarting the SCHEDULED TASK
(or, for the live process, killing the FULL tree down to the outer wrapper before relaunching) is
required every time. A python.exe's ps1-parent chain can be 2-3 processes deep; killing only the
innermost child just gets it relaunched by the (still-stale-env) outer loop.

### ⭐⭐ FIXED 2026-07-06: "Active Trades (3)" misleading + 502 on quant-live.carsonng.com
**Problem:** the LIVE account (HK$40 balance) had 3 signals fire and get logged to the journal that
never actually sized to ≥1 share (too small to fund), yet the header just said "Active Trades (3)"
identically to a real position — no way to tell a phantom trade from a real one.

**Fix — confirmed/pending split (app.py):** `active_panel()` now splits `paper.open_trades()` into
`confirmed` (has a matching row in `positions`/the broker mirror) vs `pending` (logged, never
mirrored). Header reads `"Active Trades (N open · M pending)"`; pending cards render with a dashed
grey border, a "⏳ PENDING" badge, and a computed reason via the new `contracts.min_equity_for_1_share
(stop_per_share, risk_pct)` (inverse of `size_shares`) — e.g. "needs ~$1,220 to size (you have ~$40)".
Same "⏳ PENDING" badge now also mirrors onto the matching card in **Top Opportunities / Other
instruments** (`_signal_card` gained a `pending_keys` param; `_pending_keys()` computes the set once
per render from open trades with no broker mirror) — so a pending signal looks consistent everywhere
it appears, not just in Active Trades. Verified on the running live dashboard: "Active Trades (0 open
· 3 pending)" with 3 dashed PENDING cards, and 3 matching PENDING badges on the same instruments'
cards in the grid section.

**Separately, 502 Bad Gateway on quant-live.carsonng.com:** root cause was both port 8081 (live
dashboard) and port 4001 (live IB gateway) down — `DashboardAppLive` still isn't a registered
scheduled task (see the pending elevated-PowerShell step above), so nothing auto-restarted it after
whatever killed the process. Fixed by relaunching `run_dashboard_live.ps1` and `StartGateway.bat`
fresh; the Gateway reconnected without needing a new 2FA push this time (verified: no "gateway down"/
"connecting" text, `IBKR LIVE` label present, NetLiq loaded). Public URL confirmed back to a normal
302 (Cloudflare Access redirect) instead of 502.
**Until `DashboardAppLive` is registered as a real scheduled task, this will keep recurring on any
crash/reboot** — that PowerShell command is still the top pending action item for the user.

**UPDATE 2026-07-07: `DashboardAppLive` is now registered** (user ran the `Register-ScheduledTask`
command; run-level lowered to `LeastPrivilege` since the script needs no admin rights). Verified by
killing the live process tree entirely and calling `Start-ScheduledTask` — it came back on its own,
gateway reconnected without a fresh 2FA. Live now has the same auto-recovery as paper.

**Also fixed 2026-07-07: IBC's own console window kept popping up during gameplay (not the dashboard's
doing).** Root cause: `StartGateway.bat` launched without `/INLINE` uses `start` internally to open
`DisplayBannerAndLaunch.bat`, and Windows' `start` always spawns a NEW, visible console regardless of
the parent process being hidden — IBC's own doc comment says exactly this ("if using Task Scheduler
... you MUST supply /INLINE"). Fixed both `C:\IBC\start_hidden.vbs` and `C:\IBC-Live\start_hidden.vbs`
to pass `/INLINE`. Verified on paper: killed the gateway's java.exe, watchdog relaunched it, no console
window appeared this time (previously a visible admin cmd window titled "IBC (GATEWAY 1047)" showed
up every relaunch).
(Considered but NOT built: a game-mode auto-detect watchdog to pause `DashboardApp`/`DashboardAppLive`
during play — turned out unnecessary once the actual annoyance (the console popup) was root-caused
and fixed directly; also `Stop-ScheduledTask` was found to sometimes silently set `Enabled=False` on a
task, effectively disabling it — avoid using it for any future pause/resume automation, prefer killing
the process tree directly and leaving the task definition alone.)

### 🔬 INVESTIGATED 2026-07-07: does the LLM gate reduce trade frequency? — corrected finding
**Question:** LLM's `action` already overrides the deterministic signal when present ([paper.py:435](
dashboard/core/paper.py)) — does this meaningfully cut trade frequency today?

**First pass (misleading):** naively counting `board_scan_signals` rows where `det_strength>=5` and
the LLM said WAIT/HOLD gave ~19.7% (1,032/5,248) — looked like a real ~20% frequency cut.

**Corrected finding, after deduping scan-level noise into actual decision points:** that 19.7% almost
entirely counts REPEATED scans of the SAME already-open/already-qualifying trend (scans run every
15min-4h), not independent entry opportunities. Deduping into direction-contiguous "episodes" (a new
streak = an actual entry decision point) over the ~3.5-week live history gives 31 total episodes —
**31/31 had the LLM confirming direction at the episode's start.** Cross-checked two ways: (1) all 4
real paper trades have `llm_bias='bullish'` at entry: (2) the FULL `rejected_signals` log (874 rows)
has **zero** entries citing LLM/no-confluence as a block reason — 100% are `trend strength <5` or
`overextended RSI`. The LLM only turns cautious (WAIT) LATER in an already-open episode, as RSI climbs
— by which point `OVEREXT_FILTER` (the overextended-RSI gate) already blocks re-entry independently.
**Conclusion: in this sample, LLM has never been the actual reason a trade was blocked — its caution
is currently 100% redundant with the existing overextended-RSI filter, not an independent lever.**
Making LLM a stricter/required criterion would change nothing yet; there's no case in the data where
LLM alone would have prevented a trade the RSI filter wouldn't already catch, so its judgment quality
(would a vetoed trade have lost money?) is currently untestable — would need either more live time or
a genuine LLM-vs-RSI disagreement case to ever show up. Re-check once more live history accumulates.

**RETRACTED follow-up (2026-07-07): "would exiting on LLM's WAIT have helped" is not a valid test.**
Tried a counterfactual: for each of the 4 real open trades, find the first post-entry scan where LLM
said WAIT, look up the actual price then (fully reconstructable now from `board_scan_signals` timestamps
+ `get_ohlc()`, no new logging needed -- corrects an earlier claim that this needed unbuilt
infrastructure), and compare "exit there" R vs "hold to today" R. All 4 showed early-exit would have
been worse (-0.07R to -0.29R). **But this result is meaningless and should be ignored**: per the LLM's
own system prompt ([board_scan.py:44](dashboard/web/board_scan.py)), "WAIT is correct when signals
conflict OR a trend is overextended" -- it's a fresh NEW-ENTRY conviction call made independently every
scan, never a judgment on whether to exit a position already held. Treating it as an exit trigger tests
an invented rule the LLM was never designed to support (and "overextended but still trending" is
exactly when a trend-follower should hold, not sell) -- it says nothing about whether the LLM's actual
job (gating new entries) is any good. A valid test still needs a genuine case where the LLM would have
blocked a NEW entry that the RSI filter alone wouldn't have -- none exists yet (see above).

**FOLLOW-UP TOOL (2026-07-07): `dashboard/research/llm_gate_audit.py`** -- re-runnable script for the
one still-valid question above (does LLM ever independently veto a NEW entry the RSI/strength filters
wouldn't already block). Run: `python -m dashboard.research.llm_gate_audit`. First real run found a
candidate in the LIVE db (VNQ, RSI 68.8 -- under the OVEREXT_HI=70 cutoff, so genuinely independent of
the RSI filter) but it didn't survive inspection: confidence was 0.5 (the model's floor) and the very
next scan flipped back to BUY. Deliberately did NOT add an "N consecutive WAIT scans" persistence
filter to auto-reject noise like this -- verified by hand that scan cadence isn't fixed (gaps from
~15min to ~6h in the same instrument's history, weekends pause it), so a raw scan-count threshold is
meaningless; if ever added, anchor it to elapsed wall-clock time instead. Still zero confirmed
independent vetoes in either DB as of this date.

### ⭐ FIXED 2026-07-07: instrument identity + drill-down inconsistent across Board panels
Active Trades cards showed name only, Signal gate status table showed ticker code only, Top
Opportunities/Other instruments had a "Details" button but no code. Unified all three: full name +
`(TICKER)` + a Details link/button wired to the same facts+LLM dialog (`_open_detail`). The gate-status
table's Details is a `q-table` `body-cell-detail` slot template (icon button per row, emits a `detail`
event `.on()`'d to `_open_detail`) since NiceGUI tables can't host a plain `ui.button` per cell.
Verified identical on both paper (8080) and live (8081) -- single shared `app.py`, no divergence risk.
Committed `ddc20d5`.

### 🐞 FIXED 2026-07-08: live gateway silently "stuck alive, never authenticated" doesn't self-heal
User reported quant-live.carsonng.com appeared down; asked whether visiting the URL could trigger a
fresh login. **Answer: no new mechanism needed** -- the watchdog inside `DashboardAppLive` already
retries the login every ~30s whenever port 4001 is down, independent of web traffic. The actual bug:
after a 2FA push times out unapproved (~6min window, `SecondFactorAuthenticationTimeout=180` /
`ReloginAfterSecondFactorAuthenticationTimeout=no`), the java.exe process does NOT exit -- it just sits
at a stuck, unauthenticated login screen indefinitely. Since the PROCESS is still alive, the watchdog's
`Test-Port 4001` check never fires (port is down, but nothing signals "try again"), so it never
self-heals without a manual kill+relaunch. Fixed for now by killing the stuck process tree and
relaunching -- a fresh 2FA push went out immediately and was approved within the window.
**Separately found and fixed a real config bug while debugging this:** `C:\IBC-Live\config.ini` had
`AutoRestartTime=08:00` -- IBC logs `"Auto restart time setting must be hh:mm AM or hh:mm PM"` on
every single restart attempt, meaning the documented "session-preserving daily restart, no 2FA needed"
behavior (see the 2026-06-20 HANDOFF entry) has likely NEVER actually worked since it was set -- every
restart silently fell back to needing a full fresh login. Fixed to `AutoRestartTime=08:00 AM` (12-hour
format required). Takes effect on the next natural restart; didn't force a restart to test it since
that would trigger an unnecessary extra 2FA prompt right after successfully logging in.
**Still open:** the watchdog doesn't detect "alive but stuck" as a failure mode, only "port down" -- if
this recurs, the fix is either (a) also check for the Gateway process running >N minutes without the
API port opening and force-kill it, or (b) increase IBC's own internal timeout/retry behavior. Not
built yet since a single manual kill+relaunch resolves it in under a minute when it happens.
**CLOSED 2026-07-09** -- see the watchdog auto-recovery entry below (option (a), built into the
background monitor itself, not just the on-demand Restart button).

**CONFIRMED WORKING later the same day:** the scheduled 08:00 AM daily auto-restart fired naturally
and completed in ~15 seconds with **no manual 2FA needed** -- the first real proof the format fix
actually restored the session-preserving behavior that had silently never worked before.

### 🐞 FIXED 2026-07-09: dashboard "Restart" button didn't touch a stuck IB Gateway
User reported clicking Restart on quant-live.carsonng.com still showed "gateway down" afterward.
Root cause: `_restart_server()` (`app.py`) only did `os._exit(0)` to let the watchdog relaunch the
*app* process -- it never touched the Gateway, so the "stuck alive, never authenticated" failure
mode above (still open at the time) was completely unaffected by clicking it. Implemented option
(a) from that still-open TODO, triggered on-demand instead of only via the passive port-watchdog:
new `_kill_and_relaunch_gateway()` mirrors `dashboard.ps1`'s existing stale-gateway kill block
(match `cmd.exe` procs with `StartGateway` in their command line + any process titled "IBKR
Gateway", force-kill both, relaunch via the mode-appropriate `start_hidden.vbs`), called from
`_restart_server()` before the app exits. **Gated on `broker_conn`'s `available` flag only, NOT
`ok`** -- `ok` means "is a paper account," which is *expected* False on the live dashboard even
when perfectly healthy (the header dot is orange = available-but-not-paper = normal healthy live,
not red = actually down); gating on `ok` would have force-killed a fine live gateway on every
single restart click. Caught and fixed this before shipping.
**Verified live, end-to-end:** live gateway was genuinely down at test time ("gateway down ○").
Ran the exact kill+relaunch command the button now triggers -- port 4001 came up within ~30s and
the dashboard header flipped to "IBKR LIVE: acct U12991898 ●", no 2FA prompt needed this time
(consistent with the AutoRestartTime session-preserving fix above still holding). Chrome extension
was unavailable to click the literal button in-browser, so verified via the identical underlying
command instead -- the code path clicking the button executes is exactly what was run.

### ⭐ BUILT 2026-07-09: watchdog auto-recovers a stuck-alive gateway (zero-click)
Follow-up to the Restart-button fix above -- user asked whether a stuck gateway could recover
without any manual click. Until now, no: the background port-watchdog (`run_dashboard_live.ps1`'s
`$mon` job, mirrored in `C:\Scripts\dashboard.ps1`) only detects "port down," and relaunching via
`start_hidden.vbs` is a no-op against a gateway that's still alive (just stuck, unauthenticated) --
so the "stuck" case (still open in the 2026-07-08 entry above) needed a human to notice and click
Restart or kill it by hand. Built option (a) from that TODO directly into the watchdog loop:
tracks how long port 4001/4002 has been down; if a process titled "IBKR Gateway" is confirmed
still alive and the port has been down **>=10min** (conservative -- comfortably past the ~6min
natural `SecondFactorAuthenticationTimeout=180` window, so a login that's just slow/awaiting a
phone tap is never killed mid-flight), force-kills it (same kill logic the Restart button uses)
before the next relaunch attempt. Capped at **3 auto-kills** per down-episode so a non-2FA problem
(bad credentials, a real config error) doesn't retry forever -- after the cap it falls back to
today's passive relaunch-only behavior until someone looks. Logs each auto-kill to
`C:\IBC-Live\watchdog.log` / `C:\IBC\watchdog.log` with a timestamp and attempt count. Applied
symmetrically to both the live (`run_dashboard_live.ps1`, repo-tracked) and paper
(`C:\Scripts\dashboard.ps1`, NOT repo-tracked -- edited directly) watchdogs, since both share the
identical latent bug. Syntax-checked via `PSParser::Tokenize` on both (0 errors); not yet run
through a real stuck-gateway episode end-to-end (that failure mode is intermittent) -- next
occurrence will be the real test; the Restart-button path remains available as an immediate
manual override if needed before then.

### 🐞 FIXED 2026-07-09: the watchdog auto-recovery above NEVER actually worked -- two real bugs
User reported quant-live still showed "gateway down" after the fixes above. Live-debugged and
found the auto-recovery had TWO independent bugs, both since fixed:

**(1) `Stop-Process -Force` silently fails against this specific process.** Manually reproduced:
`Stop-Process -Id <gatewayPid> -Force` returns no error with `-ErrorAction SilentlyContinue` but
does NOT kill the process; with `-ErrorAction Stop` it throws **"Access is denied."** The Gateway
process runs at a higher integrity/token level than the watchdog job's (or even an interactive
`Cap`-user PowerShell's) context, even though the process owner username matches. Confirmed
`Invoke-CimMethod -InputObject (Get-CimInstance Win32_Process -Filter "ProcessId=...") -MethodName
Terminate` succeeds where `Stop-Process` doesn't (`ReturnValue 0`, process actually gone after).
Every prior "auto-kill" log line was a lie -- it logged the attempt, not success, so 5 straight
"auto-kill" cycles across two down-episodes (11:54/12:04 and 14:17/14:27/14:38) never killed
anything; each one's fallthrough relaunch just piled a NEW gateway process on top of the still-
alive old one. Found **two concurrent "IBKR Gateway" processes** at debug time, the older one
alive continuously since the very first failed auto-kill attempt.

**(2) The match was on window TITLE, which changes throughout login and can miss a stuck state
entirely.** The Gateway's window title cycles Login dialog -> "Authenticating..." -> "Second
Factor Authentication" -> only eventually "IBKR Gateway" once fully connected. A process stuck
at any earlier stage (confirmed live: one sat at "Authenticating..." for 10+ minutes, genuinely
waiting past its own `SecondFactorAuthenticationTimeout=180` with no further log activity) is
titled something OTHER than "IBKR Gateway" and was **completely invisible** to
`Where-Object { $_.MainWindowTitle -match 'IBKR Gateway' }` -- explaining why the "two processes"
in (1) included one the watchdog's own logic could never have found regardless of the Stop-Process
bug.

**Fixed both**, in `run_dashboard_live.ps1`, `C:\Scripts\dashboard.ps1` (both the `$mon` job AND
its top-level task-start kill block, not repo-tracked), and `app.py`'s `_kill_and_relaunch_gateway`
(the Restart button): (a) all kills now go through `Invoke-CimMethod ... Terminate` instead of
`Stop-Process`; (b) gateway process discovery now matches the java.exe **command line's config
path** (`IBC-Live` for live, `IBC\config.ini` for paper -- stable for the process's entire
lifetime) instead of the window title. The watchdog also now logs a post-kill verification line
("confirmed dead" vs "FAILED, still alive (pids)") so a future session can tell from the log
whether a kill actually worked, instead of trusting that it fired.

**Verified live, end-to-end, real episode (not simulated):** manually cleared both zombie
processes with the new method, restarted `DashboardAppLive` to load the fix, watched a fresh
Gateway launch hit "Second Factor Authentication initiated" in `C:\IBC-Live\Logs\...THURSDAY.txt`,
asked the user to approve the phone push, and confirmed the dashboard flip to
`IBKR LIVE: acct U12991898 ●`. Restarted `DashboardApp` (paper) afterward too, for the same fixed
code -- paper's port 4002 was never actually down, confirmed still `IBKR Paper: acct DUK968178 ●`
after restart. Both `.ps1` files syntax-checked clean (`PSParser::Tokenize`, 0 errors both).

### ⭐ TUNED 2026-07-09: retry a stuck gateway every 2min (was 10) and reissue 2FA each time
User wants a missed/expired 2FA push retried fast, not left waiting out a long conservative
window. Lowered `$stuckThresholdMin` **10 -> 2 minutes** in both watchdogs -- deliberately
SHORTER than IBC's own `SecondFactorAuthenticationTimeout=180`, since that timeout doesn't
self-heal when it fires anyway (the whole reason the auto-kill exists), so there's no benefit to
waiting for it. Each auto-kill+relaunch is a fresh login attempt = a fresh 2FA push, so a missed
one gets retried rather than leaving the account stuck on one that already lapsed. Raised
`$maxAutoKills` **3 -> 10** to keep a similar ~20min overall retry window at the faster cadence
before giving up (vs. the old ~30min at the slower one). Applies uniformly whether the down-
episode started organically or from a manual Restart-button click, since the watchdog polls
continuously regardless of what caused "port down" -- no separate button-side logic needed.
Restarted both tasks; verified both dashboards reconnected (`acct DUK968178 ●` / `acct
U12991898 ●`) on the new thresholds.

### 🔬 TESTED 2026-07-09: turn-of-month calendar effect — REJECTED
Second of two ideas proposed for a "0.5% risk, closes within days" opportunistic slot (the first,
the dip-sleeve extension, is the entry below). Classic Lakonishok-Smidt turn-of-month window
(last trading day of month + first 3 of the next) tested on SPY/QQQ/DIA/IWM/EFA/EEM, 24-33y each.

**Raw effect is real** -- TOM days beat non-TOM days on every ticker but IWM (meanRet, daily):
SPY +0.077%/+0.041%, QQQ +0.104%/+0.044%, DIA +0.056%/+0.038%, EFA +0.058%/+0.028%, EEM
+0.149%/+0.030% (IWM -0.007% differential, only exception). Built into actual monthly trades
(enter close of 2nd-to-last day of month, exit close of 3rd trading day of next month, 0.10%
cost): all 6 tickers show positive per-trade meanR (+0.04% to +0.48%) and win 54-58%.

**Blend into the core book is a clean reject:**
| | Core only | +5% | +10% | +15% |
|---|---|---|---|---|
| CAGR | +8.28% | +8.97% | +9.62% | +10.21% |
| Max DD | −9.6% | −11.4% | −13.6% | −16.0% |
| Sharpe | 1.00 | 0.99 | 0.94 | 0.88 |

Sharpe monotonically WORSENS and DD blows out as weight increases -- the opposite of the dip
sleeve's profile. Root cause: TOM is an UNCONDITIONAL calendar bet on the SAME tickers the core
book already holds (SPY/QQQ/DIA/IWM/EFA/EEM are all core positions) -- it doesn't wait for a
dip/stretch like the panic-MR signal does, it just goes long every month regardless of regime.
When a crash spans a TOM window, it stacks MORE exposure on names the core book is already
losing on, with zero diversification benefit (vs. the dip sleeve's counter-trend entry, a
genuinely different risk driver). Same failure signature as the naive all-22 dip-buy extension,
without the redeeming upside. **Not adopted, no code changes made.**

### 🔬 BUILT & TESTED 2026-07-09: parameter sensitivity sweep + walk-forward validation
User submitted two critique documents; most of their points either repeated already-settled
questions or contained factual errors (see below), but two were genuinely new, valuable, and
untested in this project: parameter robustness and temporal (regime) stability. Built both as
permanent tools in `dashboard/research/`.

**`param_sensitivity.py`** -- one-at-a-time ±20% perturbation of the 4 main tunable parameters
(`SL_ATR_MULT`, `RR_DEFAULT`, `HORIZON_DAYS`, `OVEREXT_HI/LO`), holding all else at the live
baseline (22-ETF book, `--pos-cap 0.25`):
| Parameter | −20% | baseline | +20% |
|---|---|---|---|
| SL_ATR_MULT | 0.486 | **0.543** | 0.483 |
| RR_DEFAULT | 0.502 | **0.543** | 0.518 |
| HORIZON_DAYS | 0.389 | **0.543** | 0.481 |
| OVEREXT band | 0.507 | **0.543** | 0.393 |
(ratio = |CAGR/maxDD|). No collapse toward zero anywhere -- real evidence against overfitting.
HORIZON_DAYS and the RSI OVEREXT band are the most sensitive (baseline happens to be the local
best on both) but even their worst case is only ~28% below baseline, not a collapse.

**`walk_forward.py`** -- 6 rolling ~5y windows across the full 30.3y span (not a true ML
walk-forward, since the strategy has fixed hand-set parameters with no fitting step -- this
checks TEMPORAL stability instead, i.e. is the edge concentrated in one lucky period):
| Window | CAGR | expR | Win% |
|---|---|---|---|
| 1996-2001 | +1.23% | +0.324 | 43% |
| 2001-2006 | **−0.43%** | **−0.118** | 35% |
| 2006-2011 | +4.58% | +0.290 | 46% |
| 2011-2016 | +3.38% | +0.148 | 42% |
| 2016-2021 | +11.02% | +0.445 | 51% |
| 2021-2026 | +12.13% | +0.376 | 48% |
5/6 windows profitable (expR positive), but ratio std (0.684) EXCEEDS its own mean (0.877) --
a wider spread than the critique's own example of "regime-dependent" (0.9±0.5), not the
"narrow, strong robustness" (0.9±0.15) outcome. Honest read: the edge is real and has held up
across most of 30 years, but is meaningfully weaker in 1996-2011 (mostly the +1 to +4.6% CAGR
range, one outright negative window) than 2016-2026 (+11-12% CAGR) -- consistent with the
already-documented recent-regime bull-flattering pattern (see the "~4-7%/33y vs ~10%/13y" note
earlier in this doc). Not a red flag on its own (5/6 positive is a real pass), but a legitimate
caveat: don't assume the last decade's numbers are the steady-state.

**Fact-check on the accompanying critique (both documents), errors found:**
- "开通 Currency Conversion 权限（不是 Leveraged Forex）" -- **backwards**. The user's own IBKR
  screenshot confirmed Currency Conversion is already enabled; Leveraged Forex is the actual
  gap. Following this literally would fix nothing.
- "账户仅~$13k" -- wrong, live account is HKD 10,040 ≈ $1,287. The "manually buy $2-5k SGOV"
  suggestion built on this is larger than the whole account.
- "提高风险至2%，CAGR+15-20%，DD-19.8%" -- verified false at the actual deployed
  `ETF_POS_CAP=0.25`: 2% vs 1% risk gives CAGR +6.1% vs +6.0%, DD −11.1% vs −11.1%, essentially
  no change (the cap saturates first). Ran directly to check, not assumed.
- "批次7-9零采纳，ETF已饱和" -- stale, unaware of today's batch-10 ASHR adoption.
- "19/22...(18/22)" fundable count -- self-contradictory within the same sentence; verified
  actual is 19/22.
- "套筒15%权重" -- conflates the backtest blending-analysis parameter with a real settable
  config value (already clarified with the user separately).
- "#9 per-ETF weighted risk allocation" (weight by historical win-rate/expectancy) -- same
  category as class-weight tilt and conviction-sizing, BOTH already tested this session and
  rejected (OOS ratio got worse in both cases). Not re-tested.
No code deployed to the live/paper systems from this entry -- pure validation/audit work.

### ⭐⭐⭐ BUILT 2026-07-09: staged sleeve rollout, spread guard, SPY benchmark, monthly attribution
Four items from the "改进建议" critique review, all genuinely buildable (not backtest questions):

**(4) Staged sleeve rollout** (`dashboard/core/sleeve.py`). 8 of 11 `SLEEVE_UNIVERSE` tickers have
zero live-observed trades -- rather than jump straight to all 11 the instant an account crosses
the equity gate, `active_sleeve_universe()` widens in stages tied to elapsed time since the
sleeve FIRST activated (`_sleeve_first_active_ts`, written once via `_record_first_active_if_needed`,
never overwritten -- an account already past the gate on day 1 still gets the same ramp, not an
instant jump): Stage 2a (SPY/QQQ/XLK) immediately, +DIA/IWM at 3mo, +HYG/EFA/EEM/VNQ/PFF/ASHR at
6mo. `SLEEVE_UNIVERSE` itself is untouched (ib_exec's membership check + research scripts need
the full 11) -- this only narrows which tickers NEW entries fire on. Also added a PER-TICKER
circuit breaker (`_ticker_breaker_tripped`): auto-removes a single ticker from new entries if its
OWN live closed-trade record shows win<40% or expR<0 once n>=5 closed trades exist -- doesn't
touch other tickers or the core book, a bad result on one satellite doesn't imply the others are.

**(8) Spread-widening guard** (`dashboard/execution/ib_exec.py`, `_place_sleeve_bracket`). The
sleeve's whole thesis is entering during a VIX panic -- exactly when ETF bid-ask spreads can blow
out 5-10x, and the backtest assumes close-price fills with NO spread cost modeled at all. Added
`ib_client.get_stock_tick()` (new function, mirrors the existing futures-only `get_tick()` but for
SMART-routed stocks -- careful about `_LOCK` non-reentrancy, does its own inline qualify rather
than calling `stock_contract()` which takes the lock itself) and a `SLEEVE_MAX_SPREAD_PCT=0.5%`
cap: skip (not cancel -- trade stays unmirrored, retries next cycle) if live spread/mid exceeds
it. Falls through and places the order if no live quote is available (a permanent block on every
missing-quote cycle would silently starve the sleeve on a delayed-data account) -- logged either
way for audit.

**(5) SPY benchmark comparison** (`app.py` + `service.py`). New "vs SPY / excess" line under the
Portfolio panel's P&L headline card -- your % return vs. buy-and-hold SPY over the SAME tracking
window. `base_px` (SPY price at the account's own tracking-start date) is a one-time historical
lookup cached forever (re-fetched only if the tracking-start date itself changes); `cur_px`
refreshes on the same ~4h cadence as `tbill_rate`. **Bug caught during verification**: first
version compared a tz-aware Python datetime against yfinance's tz-naive daily index --
"Invalid comparison between dtype=datetime64 and datetime" on every cycle, silently logged as a
DEBUG line, UI just never showed the row. Fixed by stripping tz from both sides before comparing.
Verified live on both dashboards after the fix: paper "vs SPY +2.03% / excess −1.33%", live
"vs SPY +0.45% / excess −0.45%".

**(6) Monthly attribution table** (`app.py`, `retrospective_panel()`). Breaks monthly $ P&L into
trend-strategy / sleeve / other. Trend and sleeve are computed from CLOSED trades'
`realized_r * risk_money` (risk_money is the ACTUAL dollar risk sized at execution time, read
from `ib_mirror`/`mt5_mirror` -- exact even if `RISK_PER_TRADE` changed between trades, not
re-derived). **"Other" is a deliberate residual** (total month-over-month change on the
deposit-adjusted equity curve, minus trend, minus sleeve) -- there's no historical `AccruedCash`
time series stored anywhere to compute cash-interest contribution directly, so labeling the gap
"other" is the honest choice over fabricating a precise-looking number. Whole table in USD (risk
sizing is natively USD; the equity curve converts from the account's base currency via the same
HKD peg used elsewhere). Verified rendering on paper with real month rows (2026-06, 2026-07).

All four compiled clean, redeployed to both dashboards, verified live (not just compiled) after
catching and fixing the SPY-benchmark timezone bug.

### ⭐⭐ BUILT 2026-07-09: panic-MR dip sleeve extended 3 → 11 tickers, DEPLOYED to paper
User asked for a "1 trade/day, 0.5% risk, closes within days" opportunistic sleeve. Rather than a
new mechanism, re-tested the ALREADY-VALIDATED panic-MR dip-buy signal (close<20MA*0.975, VIX
up>15%/5d, RSI14<35, ADX>20) across broader scopes, since it's the one surviving short-hold idea
in this project (options lottery/short-vol/earnings-vol-crush/stat-arb pairs/sector-rotation MR
all previously rejected -- see the "External/alt-data" section above for the full graveyard).

**Scope test (blended into the core 22-ETF book, 33.4y, 0.5% base risk):**
| | Core only | Current (SPY/QQQ/XLK) @10% | Naive ALL-22 @10% | **Selective 11 @10%** |
|---|---|---|---|---|
| CAGR | +7.91% | +9.68% | +11.16% | **+11.58%** |
| Max DD | −11.2% | −9.7% | −11.5% | −10.4% |
| Sharpe | 1.05 | 1.25 | 1.28 | **1.32** |

Naive "all 22" looked good at 10% but its drawdown blows out to **−15.8% at 15% weight** (current
scope: still −9.6% at 15%) -- broadening across correlated ETFs means the VIX-spike trigger fires
MANY of them simultaneously during the SAME systemic panic, concentrating risk exactly when the
core book is already stressed (the opposite of the diversification benefit breadth gives the core
trend book). Built a SELECTIVE subset instead -- kept only tickers clearing meanR>=0.7% at n>=20
in the per-ticker re-test, dropped the rest:

| Ticker | n | meanR | | Ticker | n | meanR | | Dropped (weak/negative/thin) |
|---|---|---|---|---|---|---|---|---|
| QQQ | 87 | +2.16% | | HYG | 30 | +1.29% | | CPER −0.04%, DBC −0.65% (negative) |
| EEM | 66 | +2.17% | | VNQ | 59 | +1.11% | | GLD +0.03%, TLT +0.05% (~zero) |
| SPY | 98 | +1.40% | | ASHR | 23 | +1.10% | | TIP n=8, IEF n=5 (too thin) |
| EFA | 72 | +1.27% | | PFF | 44 | +0.98% | | CWB +0.17%, VNQI +0.47%, AMLP +0.59%, |
| DIA | 78 | +0.88% | | IWM | 83 | +0.80% | | HYD n=13 (weak/thin) |

**Selective-11 stays stable further out too** (15%: +13.39%/−10.7%/Sh1.29; 20%: +15.19%/−12.6%/
Sh1.23) before it starts trading Sharpe for CAGR the same way, just at a higher weight threshold
than the naive version -- **10-15% weight is the sweet spot**, matching a small opportunistic
allocation rather than a large reallocation.

**NOTE on IWM:** an earlier research round (`dipbuy_refine.py`, 2026-06-29) dropped IWM citing
"weakest edge." This fresh re-test, run against the CURRENT exact production signal spec (which
may differ from that earlier draft), shows genuine edge (n=83, meanR +0.80%, win 72%, comparable
tier to DIA) -- re-included based on this direct measurement against the live spec, which
supersedes the earlier note.

**Implemented:** `dashboard/core/sleeve.py`'s `SLEEVE_UNIVERSE` extended from `["SPY","QQQ","XLK"]`
to `["SPY","QQQ","XLK","DIA","IWM","HYG","EFA","EEM","VNQ","PFF","ASHR"]` (11 tickers). No other
code changes needed -- `ib_exec.py`'s order-placement path and `_place_sleeve_bracket` already
reference `sleeve.SLEEVE_UNIVERSE` dynamically (not a hardcoded copy), and `_load_daily`/
`entry_signal`/`should_exit_dynamic` are all already ticker-generic. Updated stale comments in
`paper.py` and `ib_exec.py` that hardcoded "SPY/QQQ/XLK" in prose.

**Deployed to PAPER only** (`SLEEVE_ENABLED=1` is set only in `C:\Scripts\dashboard.ps1`, per the
existing paper-only gate -- `run_dashboard_live.ps1` deliberately does not set it, unchanged).
Verified: compiled clean, confirmed `sleeve.SLEEVE_UNIVERSE` loads the new 11-ticker list, no
open sleeve trades existed to disrupt, restarted `DashboardApp` and confirmed reconnected
(`acct DUK968178 ●`). (Task restart took an extra cycle this time -- Task Scheduler/UAC hiccup
unrelated to this change, confirmed by running `dashboard.app` directly first and seeing it start
clean.)

**⭐ `SLEEVE_ENABLED=1` also set on LIVE (2026-07-09, user-confirmed via explicit yes/no prompt).**
User asked whether the live sleeve "would do good than bad." Key finding that made this an easy
call: setting the env var is **completely inert today** -- `paper.sleeve_active(equity)` is a
SEPARATE, independent gate requiring equity >= `PHASE2_NAV_USD` (~$64k/500K HKD), and the live
account (~$1,287) is ~50x below it, so nothing can trade regardless of `SLEEVE_ENABLED` until the
account grows that far. Setting it now just means the sleeve auto-activates the moment that
threshold is crossed later, matching the existing "no manual step" Phase 1->2 design already used
everywhere else -- consistent policy, not a new exception. Added to `run_dashboard_live.ps1` with
a comment flagging the still-open caveat: the 11-ticker scope (and even the original 3-ticker
scope) has **zero live-observed trades** -- re-check paper's actual trade history once it
accumulates some, before the $64k gate opens for real. Verified: syntax-checked clean, restarted
`DashboardAppLive`, confirmed reconnected (`acct U12991898 ●`) with the header badge correctly
showing **"Phase 1 · core-only"** (sleeve inactive, exactly as predicted given the equity gap).

### 🔬 TESTED 2026-07-09: conviction-weighted position sizing — REJECTED
User asked for further performance-improvement ideas beyond new ETFs. Proposed scaling risk by
signal conviction WITHIN the already-qualifying band, since `strength` itself has ZERO variance
among gate-passing trades (`MIN_STRENGTH=5` is the max on a 1-5 scale, so every trade that clears
the gate is already strength=5) -- a genuinely different mechanism from the already-rejected
class-weight tilt (which weighted by ASSET CLASS history, not per-trade conviction).
Implemented `--conviction-size` (`backtest.py`): scales risk 0.85x-1.15x by the entry bar's own
20-day momentum magnitude (the continuous signal underlying `strength`/`obviousness`), linearly
between the 3% threshold that gates strength=5 and a 10% saturation point -- no look-ahead, uses
only the entry bar's own facts. Wired through the existing `c.get("risk_mult", 1.0)` hook in
`_portfolio` (already used elsewhere for the sleeve's VIX-scaled sizing), so no changes to the
sizing engine itself were needed.

**Result (21-ETF book, 0.5% risk, matching magnitude to the class-tilt test for comparability):**
| | Full CAGR | Full maxDD | Full ratio | OOS CAGR | OOS maxDD | OOS ratio |
|---|---|---|---|---|---|---|
| Baseline | +5.5% | −12.3% | 0.447 | +13.8% | −6.9% | 2.00 |
| Conviction-sized | +5.6% | −12.5% | 0.448 | +14.1% | −8.1% | **1.74** |

Full-history is a wash (ratio flat). OOS is worse -- CAGR +0.3pp came with DD +1.2pp, a ~13%
relative decline in the ratio. Same failure pattern as VIX-regime and class-tilt: sizing up on
"stronger momentum" trades tends to load up right before the reversals hit hardest. **Not
adopted** -- kept as a re-runnable CLI tool (`CONVICTION_SIZE` defaults False, zero effect on the
live/paper dashboards) in case future data changes the picture, same policy as `--class-weight`/
`--vix-regime`.

### ⭐⭐ ETF UNIVERSE: 21 → 22 (2026-07-09) — batch-10 screen, ASHR adopted
User asked for further ETF candidates to backtest despite the 2026-07-08 "pool genuinely
exhausted" conclusion (batches 7-9, zero adoptions). Rather than retread rejected classes,
targeted RATE-SENSITIVITY structures genuinely unlike the three duration buckets already held
(all plain Treasury duration) plus one new equity sub-market, each picked for a distinct causal
driver:

| Ticker | class | n | expR | verdict |
|---|---|---|---|---|
| USFR | floating_rate | **0** | n/a | **structurally excluded** -- near-zero-duration by design, ZERO gate-passing signals in 12.5y (no price trend for a trend-follower to detect at all; a genuinely new type of negative finding, not just a weak edge) |
| WIP | intl_inflation | 39 | +0.080 | reject (confirms batch-4's BWX/PICB lesson: "intl version of a held FIXED-INCOME class" doesn't generalize) |
| FLOT | ig_floating | 2 | +2.925 | too thin to conclude (same as UNG) -- deferred |
| MBB | mbs | 8 | +0.976 | strong raw expR but isolation test came back **FLAT** (OOS CAGR +11.17%→+11.17%, no visible change) -- deferred, not rejected, given the thin n |
| ASHR | china_eq | 19 | +0.557 | **ADOPT** |

History checked via yfinance before including any of these (this project's "no crypto-length
history" discipline): MBB 19.4y, WIP 18.4y, FLOT 15.1y, ASHR 12.7y, USFR 12.5y -- all clear the
bar, on the shorter side vs the 33y core book (hence the low n's above).

**⚠️ CORRECTION (same day): the standalone isolation script below overstated the DD result.**
Original ad-hoc script (calling `_signals()` directly per instrument, bypassing whatever
filtering `main()`'s full loop applies before candidates reach `_portfolio`) reported "maxDD
identical to 4 decimals" in both full-history and OOS windows. Re-checked against the REAL
production path (`python -m dashboard.research.backtest --longweekly`, same command run before
vs after promoting ASHR -- the actually-trustworthy comparison, not an ad-hoc script) and found a
more mixed picture:

| | Full history (0.5% risk) | OOS (recent ~40%) |
|---|---|---|
| 21-base | CAGR +5.4% / maxDD −12.3% / ratio 0.439 | CAGR +13.3% / maxDD −6.6% / ratio 2.02 |
| 21-base + ASHR | CAGR +5.5% / maxDD −12.3% / ratio **0.447** | CAGR +13.8% / maxDD −6.9% / ratio **2.00** |

Full-history (the honest long-run anchor) DOES improve cleanly: CAGR +0.1pp, DD unchanged, ratio
0.439→0.447. But OOS is NOT "flat DD" as first claimed -- CAGR +0.5pp came with DD 0.3pp *worse*,
so the OOS ratio is essentially flat (2.02→2.00), not an improvement. Net: still clears the
"flat-or-better" bar on the metric this project anchors to (full-history), but by a smaller,
more mixed margin than originally reported -- correcting the record rather than the standing
adoption decision, since full-history still supports keeping ASHR. The original (overstated)
isolation writeup follows for reference, since it's what informed the initial decision:

**Isolation test (21-base + ASHR), precise/unrounded -- ad-hoc script, since found to differ
from the real production path above:**
| | full CAGR | full maxDD | OOS CAGR | OOS maxDD | OOS ratio |
|---|---|---|---|---|---|
| 21-base | +6.2720% | −12.9065% | +11.1741% | −12.9066% | 0.8658 |
| 21-base + ASHR | +6.4352% | −12.9065% | +11.6021% | −12.9066% | 0.8989 |

China A-shares are policy/capital-control-driven and genuinely decouple from broader EM for long
stretches -- this is NOT a narrower geographic slice of the held EEM exposure (the batch-2
failure pattern), which is why it worked where BWX/PICB/WOOD
(batch-4's "intl version of X" attempts) didn't.

Promoted `ASHR` to `ETF_CANDIDATES` (`instruments.py`) and added `"china_eq"` to
`WEEKLY_TREND_CLASSES` (`paper.py`). Verified end-to-end: plain `python -m dashboard.research.backtest
--longweekly` (production `active_universe()` path, no screen flag) reproduces the isolation
numbers exactly (OOS CAGR +13.8%/DD -6.9%/expR +0.405/n=687) -- **live universe now 22 ETFs** (21
prior + ASHR; 23 defined total, EMB still excluded via `WEEKLY_TREND_CLASSES`). Both dashboards
restarted and confirmed scanning the new universe: paper "22/22 ETFs" fundable, live "19/22."
Infra added: `ETF_SCREEN_BATCH_10` (USFR/MBB/FLOT/WIP remain, deferred/rejected -- not deleted,
same reversibility principle as EMB/BKLN/FM) + `--etf-screen10` in `backtest.py`, mirroring the
existing batch pattern exactly.

### 🐞 FIXED 2026-07-10: live dashboard showed HKD 0 -- a second managed account clobbered the real one
User reported the live dashboard's headline P&L flipped to "HKD -10,040 (-25100.00%)". Investigated
and found the account snapshot itself was reading ALL ZERO (`NetLiquidation`, `TotalCashValue`,
`GrossPositionValue`, `AvailableFunds`, `ExcessLiquidity` all 0.0) -- not a display-math bug, the
underlying account read really was zero. User confirmed IBKR's own Client Portal/TWS showed the
correct, UNCHANGED balance -- ruling out an actual account/fund issue and confirming this was a
read-side bug somewhere in this codebase.

**Investigation path (each step ruled out one layer):** (1) found two "IBKR Gateway" processes
running simultaneously (one from 5am, one from the 8am daily restart) -- looked like the same
"duplicate stuck session" bug fixed 2026-07-09, killed both, fresh relaunch confirmed clean login
via the IBC log ("Login has completed", no 2FA needed) -- **still zero**. (2) restarted the
`DashboardAppLive` app process itself in case its `ib_client` connection object was stale --
**still zero**. (3) wrote a minimal, isolated script bypassing the whole dashboard/ib_client
layer, connecting fresh with its own clientId and calling `accountSummaryAsync()` directly --
**this revealed the real cause**: `ib.managedAccounts()` now returns TWO accounts under this
login, `U12991898` (the real one, correctly showing NetLiquidation HKD 10,040) AND a second,
unrelated, genuinely-empty account `U20738951` (all zeros).

**Root cause:** `ib_client.account_summary()` iterated `accountSummaryAsync()`'s returned rows
by TAG ONLY (`NetLiquidation`, `TotalCashValue`, etc.), with no filter on WHICH account a row
belongs to. Once the gateway started returning rows for both accounts, whichever account's row
was processed LAST silently overwrote the correct one in the output dict -- with U20738951 (all
zero) apparently sorting after U12991898, every field ended up zeroed. `account_id()` (used by
`is_paper()`/the live-trading guard) was NOT affected -- it already used `accts[0]`, and
U12991898 is first in the list, so the trading-safety guard was correctly scoped throughout; this
was a DISPLAY bug, not a guard/safety bug -- no orders were at risk of hitting the wrong account.

**Fixed:** `account_summary()` now resolves the same primary account (`managedAccounts()[0]`) and
filters every row to `v.account == target_acct` before accepting it. Verified: an isolated
re-test of the fixed function returned the correct `NetLiquidation: 10040.0` immediately; deployed
to the live dashboard (restarted `DashboardAppLive`), confirmed the UI shows `HKD 10,040` again.
**Still open:** why a second managed account appeared under this login at all is unexplained --
worth asking IBKR / checking Client Portal for any newly-linked account, though it doesn't affect
correctness anymore now that `account_summary()` is properly scoped.

**⭐ FOLLOW-UP (same day): added a general sanity guard, not just this one root-cause fix.**
User's suggestion: "check balance history before showing stats, to avoid unsync problems" --
right diagnosis of the actual gap. The upstream `ib_client.py` fix stops THIS specific cause, but
`service.py`'s `STATE["account"]` assignment had NO plausibility check at all before this, only
`is not None` -- the bad zero reading (`0.0 is not None` == True) sailed straight through and
got displayed. Added a confirm-then-accept guard (`account_pending_anomaly` cache key, same
pattern as the existing `equity_pending_jump` guard): a `NetLiquidation` reading that drops to
zero/negative or moves outside a 2x-either-way band from the last-good value is held pending --
`STATE["account"]` keeps showing the LAST-GOOD reading, not the suspicious one -- and is only
accepted once the SAME anomalous value repeats on the next cycle (a real, sustained change, e.g.
an actual large deposit/withdrawal or a genuine loss), same as a one-off blip gets silently
dropped if the next reading reverts to normal.

**Also found and fixed a related latent gap while implementing this**, in the ALREADY-EXISTING
`equity_history` guard (not the new one): its `implausible = hist and new_val > 0 and ...` check
explicitly EXCLUDED `new_val <= 0` from ever being flagged -- meaning a drop to exactly zero
would have sailed past that guard too and gotten recorded into the permanent equity_history chart
un-flagged (confirmed: the earlier bug episode DID leave a few `[ts, 0.0, 'HKD']` points in
`equity_history` before the STATE-level fix would have caught it upstream). Fixed to check the
PREVIOUS point's validity instead, explicitly catching `new_val<=0`. Also fixed a secondary bug
in the SAME guard's confirm branch: `pending.get("val")` was used as a truthiness check, and `0`
is falsy in Python, so a genuine CONFIRMED drop-to-zero would have stayed stuck in pending limbo
forever (never actually accepted) -- changed to an explicit `is not None` check.

Verified: compiled clean, redeployed to both dashboards, confirmed both still show correct
balances (paper `HKD 1,017,290`, live `HKD 10,040`) after restart.

**⭐ DATA CLEANUP (same day):** the two guards above stop this happening again, but the LIVE
account's `equity_history` already had 45 corrupted `0.0` points baked in from the incident
window (2026-07-10 05:10:46 to 12:46:01, cleanly bracketed by genuine `10040.0` readings on both
sides -- confirmed via direct inspection, not guessed). Since `Drawdown from peak` and the equity
chart are computed FRESH from `equity_history` on every render (no separately-persisted peak
value to also reset), removing the bad points was the only cleanup needed. Also checked
`cash_flows` for any bad entries the incident might have created -- clean, only the genuine
2026-07-08 HKD 10,000 deposit is there (the OLD `equity_history` guard's `new_val > 0` bug meant
the confirmed-jump/cash-flow branch never triggered for a zero reading, so no bogus flow was ever
logged). Removed the 45 points directly from the live SQLite `cache` table (563 -> 518 points),
verified via `store.cache_get()` (the real read path, not a raw query) that zero corrupted points
remain and the series is still chronologically sorted. **No restart needed** -- `portfolio_panel()`
reads `equity_history` fresh from the DB on every render, so the fix was visible immediately.
Verified on the live dashboard: `Drawdown from peak: now +0.0%` and `You are up -- HKD 0
(+0.00%)`, both now honest (a fresh account with zero closed trades really is at 0.0%, not the
fake -25100% from the bug).

**⭐ AUTOMATIC RETROACTIVE SELF-HEAL (same day, `service.py`):** user asked directly whether stats
now get "automatically revised and synced" going forward, not just prevented from breaking
again. Honest answer at the time: no -- the confirm-then-accept guards are PREVENTIVE (stop new
bad points from being WRITTEN), and the 45-point cleanup above was a one-off MANUAL fix, not an
automatic process. Built `_self_heal_equity_history()` to close that gap: scans `equity_history`
for a run of points that deviates >50% (or hits <=0) from the last known-good value AND is later
bracketed by a clean return to that same normal level -- exactly the pattern of both this
incident and the earlier "stray 40" one. Deliberately conservative: a run that ISN'T yet
bracketed by a return to normal (i.e. still ongoing/unconfirmed) is left untouched, so it can
never delete a genuine ongoing change, only already-resolved glitches -- unresolved cases stay
governed by the confirm-then-accept guard, not this audit. Runs via `restore_cache()`, called at
the top of every page load (`main_page()`) -- i.e. before stats are shown, as asked -- throttled
to once per ~10min via a new `equity_healed_ts` cache key so rapid page refreshes don't re-scan
repeatedly. Tested against 4 synthetic scenarios before touching real data: bracketed zero-spike
(removed), a real sustained deposit-like jump with no return (kept, untouched), an unresolved
anomaly still at the end of the series (left alone), normal small fluctuations (untouched) -- all
4 passed. Deployed to both dashboards, confirmed `equity_healed_ts` was set on both (ran without
error) and both still render correctly (paper `HKD 1,017,278`, live `HKD 10,040`, drawdown
`+0.0%`).

### ⭐⭐⭐ BUILT 2026-07-10: auto-reconcile vs IBKR on every fresh login + uncovered a REAL live desync
User asked: "upon every login, system should auto check all history from ibkr and ensure the
stats on server is the same as local one" + "build relevant test cases". TWS API has no simple
"give me historical NAV" call (needs Flex Queries / Client Portal report API — heavier,
separate integration), so built the realistic version: on every FRESH gateway connection
(`ib_client._S["needs_reconcile"]`, set True only on a genuine reconnect, not a reuse — see
`reconcile_needed()`/`mark_reconciled()`), compare IBKR's actual reported positions against
what the dashboard's mirror table thinks is open. New `dashboard/core/reconcile.py`
(`compare_positions()` pure + `reconcile_with_broker()` I/O wrapper), triggered from
`refresh_cheap()`, surfaced as a header badge (`⚠ position mismatch`) in `app.py`.

**Two real bugs found and fixed while building this:**
1. `ib_client.broker_positions()` used `reqPositionsAsync()` — but that method returns a raw
   `asyncio.Future`, not a coroutine, incompatible with `_run()`'s
   `run_coroutine_threadsafe()`. Fixed by wrapping in an inline `async def`.
2. **The comparison source was wrong.** First version compared against
   `paper.all_trades() status=='OPEN'` — but that table tracks trading *signals/ideas*
   (rationale, LLM bias, macro_note), not confirmed broker fills; a signal can be OPEN there
   without ever having been placed at IBKR. Fixed to compare against `ib_mirror status='OPEN'`
   instead (new `ib_exec.mirrored_open_symbols()`) — the table that actually records what got
   sent to the broker.

**Then a real mismatch showed up on live, reproducibly, on every reconnect** — first
misdiagnosed as a connect-timing race (positions not yet synced) and "fixed" with a generous
retry (up to ~24s, harmless, kept). Verified against ground truth before trusting that theory
though (direct `ib.portfolio()` + `accountSummary()` check): **live account U12991898 is
genuinely 100% cash right now — `GrossPositionValue = HKD 0.00`** — while 7 positions
(ASHR/AMLP/CWB/CPER/VNQ/DBC/EEM, placed 2026-06-24 to 2026-07-09) are still marked OPEN in both
`ib_mirror` and the paper journal. Checked daily price history against each position's
recorded SL/TP: only ASHR's 07-08 close ($34.84) crossed its stop ($34.90) — the other 6 never
came close to SL or TP, meaning they likely did NOT exit via normal stop/target logic. Given
this session's earlier ~-$97k HKD margin-debit incident on this same tiny (~$1,287) account, a
margin-related forced liquidation is a real possibility. **Cannot recover exact historical
fills via the TWS API** — `reqExecutions()` returned 0 rows (session-local cache doesn't retain
data that far back); would need a Flex Query report or the IBKR web portal statement.

**Root cause of the stale bookkeeping, found and fixed:** `sync_closures()` (the function that's
supposed to detect broker-side closes) has been failing intermittently for weeks — 422+
`executor closure sync error` occurrences since 2026-06-25 in the rotated log, across several
distinct causes (TimeoutError, socket disconnects). One cause is a **definitively confirmed,
reproducible bug**: `manual_close_sleeve()`'s inner `_do()` called `ib.reqAllOpenOrders()` (the
SYNC wrapper) — which internally calls `ib_async.util.run()` →
`loop.run_until_complete(task)`. But `_do()` itself already runs *on* the dedicated IB event
loop thread (via `ib_client.call()`), so this nests a second `run_until_complete()` inside an
already-running loop → `RuntimeError: This event loop is already running`, matching the log
exactly. Fixed by switching to `ib.openTrades()` (passive in-memory cache, same shape as
`reqAllOpenOrders()`'s result, no I/O) — the same safe pattern already used for
`ib.positions()`/`ib.fills()` elsewhere in this file. Confirmed via `inspect.getsource()` that
`cancelOrder()`/`placeOrder()`/`positions()`/`fills()`/`managedAccounts()` are all safe
non-blocking sync calls with no nested-run risk; `reqAllOpenOrders()` was the only offender
anywhere in `dashboard/`.

**User's explicit decision (asked directly, real-money stakes):** do NOT touch the 7 stale
trade records yet — check the IBKR web portal / account statement for the actual exit
prices/dates first (possible margin liquidation), then resolve them with real numbers rather
than an estimate. DO investigate + fix the sync_closures root cause now (done, above).

**Deployed + verified 5 times this session** (each fix redeployed via
Stop/Start-ScheduledTask + orphan-port kill + curl poll) — reconcile now consistently and
correctly re-flags the same known real mismatch via the corrected `ib_mirror` source
(`only_local(ghost)=['AMLP','ASHR','CPER','CWB','DBC','EEM','VNQ']`), confirming the pipeline
works end-to-end; paper shows clean matches throughout (`match (7 open)` — paper has no
analogous desync).

**Test suite built** (`dashboard/tests/test_service.py`, `test_ib_client.py`,
`test_reconcile.py` — matching the existing `test_contracts.py` convention: custom
`check()`/`approx()` + `_fails` list + `__main__` runner, no pytest): 41 checks across
`heal_series()` (bracketed spike removed / sustained jump kept / unresolved anomaly left
alone / normal fluctuations untouched / empty+singleton edges), `is_nl_implausible()` +
`pending_confirms()` (boundary cases incl. the `pending_val==0.0` falsy trap),
`parse_account_summary_rows()` (including the exact 2-managed-account regression shape),
and `compare_positions()` + `mirrored_open_symbols()` (the latter against an isolated temp
sqlite db via `DASH_DB_NAME` override — never touches the real paper/live journal). All 4
test files (incl. the pre-existing `test_contracts.py`) run clean, exit 0.

**RESOLVED 2026-07-11:** user checked the IBKR Activity Statement's Trades section for the
full window (2026-06-20 to 2026-07-11) and found **zero executions for any of the 7 tickers**
-- not "opened then closed", never filled at all. Confirmed decisively via `ib_mirror`:
**all 7 rows have `perm_id=0`** (a genuinely accepted order gets a real, non-zero permId
almost immediately; 7/7 stuck at the fallback default is a 100% consistent signal, not noise).
Combined with the zero-trades statement and the known Error 435 bug being live for the ENTIRE
window these orders were placed (2026-06-24 to 07-09, well before the 2026-07-10 fix), the
conclusion is definitive: **these 7 "positions" never existed at the broker.** They were
bracket orders submitted and immediately rejected (missing `.account` on a 2-managed-account
login), but `_place_etf_bracket()`/`_place_sleeve_bracket()` only ever checked "did
`placeOrder()` raise", never "did the order actually get acknowledged" -- the same
fire-and-forget blind spot as the `keep_cash_usd()` bug, just never audited on the core
entry-order path until now. No real money was ever at risk; there's no real exit price to
reconstruct because no trade ever happened.

**Cleanup performed:** rather than invent a new "VOID" status that every stats/report/
confidence-model consumer would need to explicitly exclude (`paper.stats()` uses raw `r>0`/
`r<=0` comparisons that silently corrupt on a placeholder value), reused the existing
`archive_trades()` mechanism -- marked all 7 with `status='VOID'` and a full explanatory
`exit_reason` via `_update_resolution()`, THEN archived them out of the live `paper_trades`
journal entirely (preserved in `paper_trades_archive`, reversible via `unarchive()` if ever
needed). Also updated the corresponding `ib_mirror` rows to `status='VOID'`. Verified: `paper.
open_trades()` on live now returns 0; confirmed via a fresh post-restart reconciliation check
that `only_local(ghost)` is now `[]` (was the 7 tickers before).

**Found and fixed a second, related bug while verifying the reconcile output:** the same
fresh check showed a NEW false positive, `only_broker(untracked)=['USD']`. Checked directly --
this is just the account's **USD cash balance** ($12,693, from `keep_cash_usd`) showing up via
`reqPositionsAsync()`, which IBKR also uses to report foreign-currency cash holdings
(`secType='CASH'`) alongside real security positions. Without filtering, this would have
falsely tripped the reconcile mismatch badge FOREVER, since this account always carries some
USD cash by design -- defeating the whole point of the reconciliation feature. Fixed in
`ib_client.broker_positions()`: excludes `secType=='CASH'` entries. Verified: reconcile now
shows `"broker/local positions match (0 open)"` -- completely clean, badge cleared from the UI.

**System is now ready for genuinely new live trades.** All the blockers this session found and
fixed (Error 435, Error 10349, the account-mixup bugs, and now this fire-and-forget mirroring
gap) applied to every order placed before their respective fixes -- there is no longer any
known reason a fresh signal's bracket order would fail to actually reach the broker. The next
real signal (on either account) is the first opportunity to confirm this end-to-end, and to see
`PORTFOLIO_CAP` engage on a genuine concurrent-position scenario for the first time.

### 🔬 TESTED 2026-07-11: 2x risk at the hybrid cap -- REJECTED; portfolio_cap 100%->105% -- REJECTED
Two follow-up questions on the newly-adopted hybrid (`pos=0.25, portfolio<=100%`), both tested
directly rather than reasoned about abstractly.

**"Can I risk 2x more?"** No -- same failure mode as the earlier pure-0.10-cap test:
`PORTFOLIO_CAP` is still the dominant binding constraint in most scenarios, so doubling
`RISK_PER_TRADE` (1%->2%) barely moves CAGR (full +0.03pp, OOS actually *down* -0.12pp) while
making risk-adjusted quality worse across the board -- OOS maxDD -4.96%->-6.41%, OOS Sharpe
1.607->1.537, OOS ratio 2.30->1.77. Rejected.

**Critique proposed loosening `PORTFOLIO_CAP` 100%->105%** for a claimed "free execution
buffer" (reasoning: orders scaled down near the cap are small/oddlot and suffer worse slippage,
~0.1-0.2%/yr unmodeled cost). Tested directly: 105% makes EVERY risk-adjusted metric worse for
a negligible CAGR gain (full Sharpe 1.190->1.179, full maxDD -6.83%->-7.01%, OOS ratio
2.30->2.27, CAGR only +0.05pp full / +0.06pp OOS) -- the same unfavorable leverage trade-off
already established throughout this whole line of research, just a smaller dose. The underlying
premise is also questionable on market-microstructure grounds: smaller orders generally face
LESS slippage risk in liquid markets, not more (market impact scales with size relative to
volume) -- and US-listed ETF odd-lots trade normally with no special penalty (unlike the actual
$25k IDEALPRO forex minimum hit earlier this session, a different market). The claimed
"0.1-0.2%/yr" figure has the same fabricated-precision flavor as other unverified critic numbers
caught this session. Rejected -- kept `PORTFOLIO_CAP=1.0`.

**Also fact-checked (not backtestable, account-policy claims):** a critique's suggestion to
keep USD cash >$10,000 for an IBKR "tiered interest" benefit is unverifiable from this codebase
(our own `ib_rate` model is flat benchmark-minus-spread, no tiering) and the claimed benefit
($10-20/yr) is trivial regardless -- deprioritized pending independent confirmation of IBKR's
actual rate schedule. A "monthly review only" behavioral suggestion is reasonable but is a
personal discipline choice, not a code/backtest matter.

### ⭐ IMPLEMENTED 2026-07-11: DD-halt gate (the ONE surviving idea from a 2-critique review)
User submitted two more AI-generated critiques proposing further Calmar improvements. Fact-
checked every claim against the codebase and prior research rather than accepting at face value.

**Critique 2's entire baseline was wrong** -- it quoted "pure core: CAGR +7.14% / MDD -11.3% /
Calmar 0.63", which matches neither the current hybrid config (verified: +5.83%/-6.83%/0.853)
nor any pre-hybrid figure I can find. Every "improvement over baseline" claim built on top of
that number is unreliable by construction.

**Sleeve activation (both critiques, suspiciously IDENTICAL unverified figures)** -- both cite
"+10.78% CAGR / -10.8% MDD / Calmar 1.00 @ sleeve 10%" for combining the panic-MR sleeve with
the current book. This number doesn't match ANY documented HANDOFF entry (closest: an OLDER
17/18-ETF-book figure, ~+8.7%/-10 to -11%, a different config entirely). `backtest.py`'s only
sleeve proxy (`--meanrev-blend`, a generic ADX<20 z-score MR sleeve) is a DIFFERENT strategy
from the actual production sleeve (`core/sleeve.py` -- VIX-panic-triggered, staged rollout, 11
tickers, 5MA-touch exits), so it can't be used as a quick stand-in without misrepresenting the
real thing. Flagged as unverified; a faithful test needs a dedicated script that actually wires
in `core/sleeve.py`'s real logic, not a quick reproduction -- not done here.

### 🔬🔬 TESTED 2026-07-11: sleeve activation -- a REAL number, finally, and it refutes every prior claim
A THIRD critique doubled down on the unverified sleeve figures from the prior round, claiming
"HANDOFF confirms" sleeve@15% and citing a brand-NEW invented figure for sleeve@20% (+15.19%/
-12.6%) that appears in no prior document at all -- a direct misreading of the entry above
("not explicitly denied" was twisted into "confirmed"; the entry actually says "unverified...
not done here"). Given this is now the third round citing numbers with no backtest behind them,
built the faithful reproduction flagged as needed: reused `dipbuy_refine3.py`'s exact ADOPTED
entry/exit spec (SPY+QQQ+XLK; entry = price<97.5%*20MA & VIX+15%/5d & RSI14<35 & ADX>20; exit =
first of 5MA-touch / +3% TP / -5% SL / 10 trading days; 10bp cost) blended against the CURRENT
core book (hybrid `PORTFOLIO_CAP`, current cash-yield/margin-debit model) -- not a proxy, the
real adopted methodology, just re-run at the current config instead of the old 18-ETF/flat-cap
one.

| config | CAGR | maxDD | Sharpe | Calmar |
|---|---|---|---|---|
| core only (hybrid cap) | +6.44% | -6.83% | 1.032 | 0.943 |
| core + sleeve @5% | +7.32% | -6.52% | 1.163 | 1.122 |
| **core + sleeve @10%** | **+8.19%** | **-6.59%** | **1.263** | **1.244** |
| core + sleeve @15% | +9.07% | -7.52% | 1.327 | 1.206 |
| core + sleeve @20% | +9.94% | -8.45% | 1.360 | 1.176 |

**Every critique-cited figure was substantially overstated:** claimed sleeve@10% (+10.78%/
-10.8%) vs real (+8.19%/-6.59%) -- CAGR overstated 2.6pp, MDD overstated 4.2pp. Claimed @15%
(+12.58%/-11.1%) vs real (+9.07%/-7.52%) -- overstated 3.5pp. The invented @20% figure
(+15.19%/-12.6%) vs real (+9.94%/-8.45%) -- overstated 5.3pp.

**And the real data contradicts the critiques' central claim.** They argued 15% beats 10% as
the "sweet spot". The real test shows Calmar PEAKS at 10% (1.244) and DECLINES at 15%/20%
(1.206, 1.176) -- sizing the sleeve up further trades Calmar for raw CAGR, the same leverage-
style trade-off found everywhere else in this research. If Calmar is the objective, **10% is
better than 15%, the opposite of every prior claim.**

**Two honest scope limits on this result:** (1) only tests the ORIGINAL 3-ticker (SPY/QQQ/XLK)
spec, NOT the 11-ticker staged-rollout universe actually configured to run live (expanded later
without a matching backtest at that broader universe -- still an open gap). (2) Sharpe here uses
daily-interpolated resampling, methodologically different from the `_metrics()`-based Sharpe
quoted elsewhere this session -- don't cross-compare the "core only" baseline (1.032 here) 
against the 1.19 figure quoted in other entries; the RELATIVE comparison across sleeve weights
within this test is what's trustworthy, not the absolute baseline Sharpe.

**No config change made** -- this settles the NUMBERS question (real backtest now exists,
proving the alleged figures wrong) but does NOT settle the DEPLOY question (zero real trading
history exists for this sleeve, confirmed via a direct database check the same day -- see the
"check paper account for sleeve trades" finding). Real numbers now available if/when a genuine
activation decision is made; not acted on here.

**PORTFOLIO_CAP 100%->80% (critique 1) -- tested, NOT the clean win claimed:**
| config | Full Sharpe | Full maxDD | OOS Sharpe | OOS maxDD | OOS ratio |
|---|---|---|---|---|---|
| portfolio<=100% (current) | 1.190 | -6.83% | 1.607 | -4.96% | 2.305 |
| portfolio<=80% | 1.221 | -6.01% | 1.651 | -4.96% | 2.089 |
Full-history ratio improves modestly (not critique's claimed +19.6%), but OOS ratio actually
WORSENS (2.305->2.089, -9.4%) -- a mixed result, not adopted. OOS is the more forward-relevant
number and it gets worse here.

**SGOV/cash-sweep spread (critique 1) -- overstated ~3.5x.** Claimed "IBKR 3.12% vs SGOV 4.80%
= 1.68% spread, +1.01% CAGR benefit". Checked against OUR OWN live rate model: latest ^IRX
3.70% -> `ib_rate`=3.15%, `sgov_rate`=3.63% -> real spread **0.48%**, not 1.68%. The real
benefit at ~60% assumed idle cash is closer to **+0.29% CAGR**, not +1.01%. Real effect, wrong
magnitude -- `sweep_cash()`'s `CASH_SWEEP_MIN_NAV_USD=75,000` threshold blocking the live
account is a legitimate observation, but the payoff case for lowering it is smaller than
claimed.

**Portfolio heat scaling (critique 2 idea 3)** -- functionally IS `PORTFOLIO_CAP`, already
built and deployed this session (measured in notional terms rather than a risk-sum, same
purpose). Critique 2 wasn't aware this already exists.

**Core "has no time exit" (critique 2 idea 4) -- FACTUALLY WRONG.** The core already
force-resolves every trade at `HORIZON_CAL=35` days regardless of price action (`paper.py`'s
`_outcome_for()`: `horizon_passed` forces resolution even on an "EXPIRED"/no-clean-touch
outcome) -- critique 2's proposed threshold (35 days) is literally identical to what already
exists. Idea is moot.

**Already-rejected categories re-proposed:** trailing stop (critique 2 idea 6) duplicates the
already-tested-and-rejected dynamic-exit family (chandelier/trail stops all worse than fixed
3R, ratio 0.52-0.79 vs 0.81 -- see the 2026-07-11 critique-evaluation entry above). VIX-
conditional max-deployment cap (critique 2 idea 5) is the same broad category as multiple
already-rejected VIX/regime overlays throughout this project's history.

**Per-instrument vol-targeting (critique 2 idea 2)** -- a genuinely different mechanism from
what's been tested (existing `--voltarget` scales by the STRATEGY's own realized R-multiple
vol; this proposes scaling by EACH INSTRUMENT'S OWN price vol). Not tested directly, but likely
low marginal value: the existing ATR-based stop-sizing (`qty = risk_$ / (SL_ATR_MULT * ATR)`)
is already an implicit form of per-instrument inverse-vol sizing. Flagged for a dedicated test
if pursued further, not dismissed outright.

**The one thing that survived: `DD_HALT_PCT` -- genuinely missing, now implemented.** The
ADOPTED PLAN's own text ("halt new entries if DD>-13%") was never actually wired into code --
confirmed via direct search, zero matches anywhere. This doesn't change backtest numbers (a
live-only safety net, not an alpha lever), so nothing to sweep -- just build it.

Extracted `deposit_adjusted_series()` and `current_drawdown_pct()` from `app.py`'s inline
"Drawdown from peak" UI logic into `core/paper.py` (both pure, unit-tested --
`test_paper.py`, 10 checks) so the dashboard stat and the new gate share the exact same math
instead of two independent implementations drifting apart. Wired into `mirror_new()`: if
current (deposit-adjusted) drawdown <= `DD_HALT_PCT` (default -13.0, env-overridable, `0`
disables matching `ETF_POS_CAP`/`PORTFOLIO_CAP`'s convention), ALL new entries pause for that
cycle -- existing positions are never touched. Full 6-file test suite passes; deployed to both
dashboards, confirmed no errors and no false trigger (account is nowhere near -13% currently).

### 🔬🔬 TESTED 2026-07-11: full 11-ticker sleeve blend (closes the scope gap flagged above)
The 3-ticker (SPY/QQQ/XLK) faithful sleeve reproduction above explicitly flagged a scope gap:
the LIVE sleeve is staged out to all 11 `SLEEVE_UNIVERSE` tickers, not just the original 3.
Re-ran the same faithful `dipbuy_refine3.py`-exact entry/exit spec across all 11
(SPY,QQQ,XLK,DIA,IWM,HYG,EFA,EEM,VNQ,PFF,ASHR) blended against the current hybrid-cap core book:

| config | CAGR | maxDD | Sharpe | Calmar |
|---|---|---|---|---|
| core only (hybrid cap) | +6.44% | -6.83% | 1.032 | 0.943 |
| core + sleeve(11tk) @5% | +8.27% | -6.60% | 1.255 | 1.252 |
| **core + sleeve(11tk) @10%** | **+10.08%** | **-7.73%** | **1.301** | **1.304** |
| core + sleeve(11tk) @15% | +11.87% | -9.57% | 1.251 | 1.240 |
| core + sleeve(11tk) @20% | +13.65% | -12.63% | 1.176 | 1.081 |

Same qualitative shape as the 3-ticker test, now confirmed at full scope: Calmar peaks at 10%
(1.304, even more decisively than the 3-ticker version's 1.244) and declines at 15%/20% as the
extra tickers add more raw CAGR than they add DD-adjusted quality. **10% remains the Calmar-
optimal weight if/when the sleeve is activated; still NOT deployed** (zero real sleeve fills
exist in the paper journal as of this date, confirmed via a direct DB check) -- this is the
numbers-only answer, not a decision to go live.

### ⚠️⚠️⚠️ CORRECTION 2026-07-11 (later same day): every `--exit-test`/`--vol-horizon`/`--dd-scale`/
`--mom-filter` figure in the three critique-round entries just below was computed on the WRONG
dataset -- read this before trusting any number in them. `research/backtest.py`'s `main()`
fetches DAILY bars over only 5 YEARS by default; `--weekly`/`--longweekly` is required to get
the real live system's 30-year WEEKLY bars. Every command run today for the 4th and 5th critique
rounds (`--exit-test`, `--vol-horizon`, `--dd-scale`) omitted that flag. Since
`paper.HORIZON_DAYS=5` is counted in BARS not days, this silently tested a completely different,
much shorter (~1 week, not ~5 week) horizon system all day.

**Caught it via a sixth critique that (correctly) pushed on the exit-method question again**: an
IS/OOS breakdown of `breakeven@+1R` on the (still-buggy) data showed an enormous, walk-forward-
suspicious improvement (ratio 1.19→1.96). Rather than trust it, built an independent script using
the SAME direct weekly-fetch pattern as `sleeve_blend.py`/`param_sensitivity.py` (which were
never affected -- they don't go through `main()`'s CLI data loader) to cross-check. **The result
completely reversed on the correct data**: breakeven@+1R is WORSE than fixed at every risk level
(1% risk Calmar 0.854→0.678) and in 5/6 walk-forward windows. Re-ran everything properly:

| test | WRONG (5y daily) conclusion | CORRECTED (30y weekly) conclusion |
|---|---|---|
| breakeven@+1R / trailing-stop | (not tested at the time) | REJECTED -- worse at every risk level, 5/6 WF windows |
| vol-horizon (`--vol-horizon`) | "mixed, helps at low risk" | REJECTED -- worse at EVERY risk level + OOS (0.827/0.638/0.692/1.709 vs baseline 0.849/0.774/0.854/2.335) |
| DD-scale (`--dd-scale`, mild) | "both CAGR and maxDD worse" | Essentially INERT (0.853 vs baseline 0.854 at 1% risk) -- still not adopted, but the "makes it worse" framing was itself a wrong-scope artifact |
| exhaustion-exit | "noise, ~0 effect" | Same conclusion, correct on re-check -- numbers below corrected |
| momentum-filter (`--mom-filter`) | (not tested at the time) | REJECTED -- worse at every risk level (0.475 vs baseline 0.540 at 1% risk) |
| time-decay exit | (not tested at the time) | Walk-forward MIXED (wins 2/6 windows, loses 2/6, ties 2/6, aggregate ratio ~identical 0.985 vs 0.986) and WORSE at the live 1% risk setting (0.826 vs 0.854) despite looking good in one IS/OOS split -- REJECTED, same lucky-split lesson as breakeven |

**Every dynamic-exit and regime-overlay idea tested today, on the correct data, loses to the
current fixed 3R-TP/ATR-SL system or is a wash.** This is not a new finding -- it's the SAME
conclusion the futures-universe `LOCKED STRATEGY SPEC` already reached (26.4y, `{metal,index,
rate}`, 2026-06-23-25: "no dynamic exit beats fixed"), now independently reconfirmed on the
current 22-ETF weekly book too, after nearly being overturned by a scope bug.

**Fixed the root cause, not just the numbers**: added a guard to both `_exit_test()` and the
`--mom-filter` handler in `research/backtest.py` -- either now raises `SystemExit` if the
fetched data's median bar spacing looks daily (<4 days) instead of weekly, so this can't
silently happen again. Verified the guard fires correctly and that `--longweekly` clears it.

**WIDENED same day, later**: the two spot-fixes above only covered the two flags actively in
use when the bug was found -- but `paper.HORIZON_DAYS` and every rolling-window lookback
(`MR_WIN=20`, `PULLBACK_WAIT=2`, `REGIME`'s 40wk MA, etc.) are counted in BARS everywhere, so
the SAME vulnerability existed for every other horizon/regime-sensitive flag: `--adx`,
`--voltarget`/`--voltarget-cap`, `--dd-scale`, `--vol-horizon`, `--vix-entry`, `--meanrev`/
`--meanrev-blend`, `--pullback`, `--circuit`, `--regime`, `--vix-regime`, `--class-weight`,
`--conviction-size`, `--horizon-curve`, `--direction-test` -- 14 more flags, unguarded, found by
a direct grep audit rather than assumed safe. Rather than patch each one individually with the
same post-hoc "inspect the fetched data" pattern (several of these generate signals INSIDE the
data-fetch loop, so a post-hoc check would fire too late, after wasted API calls and possibly
already-wrong signals), moved to a single upfront check right after `args = ap.parse_args()`,
before ANY data is fetched: if a horizon-sensitive flag is set and neither `--weekly` nor
`--longweekly` was passed, `SystemExit` fires immediately naming the exact flag(s) responsible.
`--adx`/`--voltarget` get an `is not None` check (not truthy) since a legitimate value could be
`0`; every other flag is a plain bool. Verified directly: `--regime`, `--pullback`, and `--adx
20` all now fail instantly (before any yfinance call); `--pos-cap --portfolio-cap --cash-yield`
(no horizon-sensitive flag) still runs normally; `--weekly --regime` correctly proceeds past the
guard. Universe-selection flags (`--etf*`, `--classes`) and cost-model flags (`--cash-yield`,
`--cash-rate`) are NOT in the guarded list -- they don't depend on bar frequency.

**Lesson, stated plainly**: a scope bug produced a result that "looked exciting" (matched what
two independent critiques predicted) and confirmation bias almost let it through with only an
IS/OOS check. What actually caught it was building a SECOND, independently-written verification
script rather than trusting the first pretty number -- worth remembering next time a result
looks too good relative to years of prior negative findings on the same question.

### 🔬🔬🔬 TESTED 2026-07-11: fourth critique round (4 proposals) -- 2 already closed, 2 tested and rejected
A fourth AI-generated critique proposed four specific technical changes. Fact-checked and/or
backtested each rather than accepting on description alone:

**1. Limit-order entries (vs current market orders).** Genuine, not fabricated -- matches the
already-documented `cost_sensitivity` finding (`ADOPTED PLAN`: "cost already modeled; limit-
orders ~+0.1-0.2% max"). Real but marginal upside, and every order-placement call site in
`ib_exec.py` currently uses `orderType="MKT"` by design (guarantees a fill, avoids the FX-style
silent-cancel failure mode already found and fixed once this session, Error 10349). Not
implemented -- the ~0.1-0.2%/yr upside doesn't clear the bar against a new fill-uncertainty
failure mode on a system that just spent a whole session eliminating silent order failures.

**2. "Bridge the sleeve gap" (make the backtest match the real `core/sleeve.py` logic).**
Already closed before this round started -- direct code comparison confirmed `core/sleeve.py`'s
`entry_signal()`/`should_exit_dynamic()` is byte-for-byte identical to the faithful
`dipbuy_refine3.py`-based reproduction already used for the sleeve tests above (both the
3-ticker and the 11-ticker version just above). No further action needed; the critique's
premise (that the backtest was using a different, unfaithful methodology) doesn't hold --
that was already fixed two critique-rounds ago.

**3. Momentum Exhaustion Exit -- tested, NOT adopted (noise, not a real edge).** Added
`_resolve_exhaustion()` to `research/backtest.py`: once a trade is +0.5R in favour, track the
peak RSI(14) since entry; if RSI decelerates N points off that peak while still in profit, exit
at that bar's close instead of riding to the fixed SL/TP/horizon (same resolver-plugin pattern
as the existing breakeven/trailing/partial-profit tests). Ran through the existing `--exit-test`
battery, OOS @0.5%:

| exit method | OOS expR | OOS CAGR% | OOS DD% | CAGR/DD | win% |
|---|---|---|---|---|---|
| fixed (baseline) | +0.164 | 6.9 | -4.8 | 1.43 | 43% |
| exhaustion RSI-10pt | +0.158 | 6.5 | -4.8 | 1.36 | 43% |
| exhaustion RSI-15pt | +0.167 | 7.0 | -4.8 | 1.45 | 43% |
| exhaustion RSI-20pt | +0.164 | 6.9 | -4.8 | 1.43 | 43% |

**⚠️ CORRECTION 2026-07-11 (same day): the FIRST version of this table was run on the wrong
universe.** `--etf` alone only pulls raw `ETF_UNIVERSE` (10 instruments, no `ETF_CANDIDATES`,
no `WEEKLY_TREND_CLASSES` filter) -- NOT the actual live 22-ETF book. The real live universe is
what `active_universe()` returns under `BROKER=ib UNIVERSE=etf` (confirmed by direct check: 22
instruments). Re-ran with `BROKER=ib UNIVERSE=etf uv run python -u -m dashboard.research.backtest
--pos-cap 0.25 --portfolio-cap 1.0 --exit-test` (no `--etf` flag -> falls through to
`active_universe()`) -- table above is the corrected, real-universe version. The magnitudes
shifted (baseline CAGR/DD went from 1.07 to a real 1.43) but the QUALITATIVE verdict is
unchanged: RSI-15pt still only marginally "beats" baseline (+0.003 expR, +0.02 CAGR/DD, ~1-2%
relative) while `breakeven @+1R` (2.32) and `partial 33%@1R+BE` (2.06) again show far LARGER
apparent gains in the SAME run -- yet the much more rigorously tested 26.4y futures-universe exit
battery already concluded **no dynamic exit beats fixed** on this system (see "LOCKED STRATEGY
SPEC" below). Three different dynamic-exit families all showing outsized gains simultaneously,
on a shorter OOS window, is a sample-size artifact, not a real reversal. **Not adopted** -- no
code path changed in `ib_exec.py`/`core/sleeve.py`.

**4. Volatility-scaled exit horizon -- tested, MIXED result (not the clean rejection first
reported).** Added `--vol-horizon` to `research/backtest.py`: scales each trade's exit horizon
by `20/VIX-at-entry` (clipped to [0.6x, 1.4x] of the base horizon), VIX reindexed onto each
instrument's own bar index via as-of ffill (no look-ahead).

**⚠️ CORRECTION 2026-07-11 (same day): the first version of this test used the same wrong
10-instrument universe as point 3 above** (`--etf` instead of the real `active_universe()`) and
concluded "rejected, worse on every metric" -- that conclusion doesn't survive on the correct
universe. Re-ran with `BROKER=ib UNIVERSE=etf` (22 instruments), with and without the flag:

| risk | fixed CAGR/DD/Calmar | vol-horizon CAGR/DD/Calmar |
|---|---|---|
| 0.25% | 7.1% / -3.9% / 1.82 | 7.3% / -3.8% / 1.92 |
| 0.50% | 7.7% / -4.7% / 1.64 | 7.8% / -4.7% / 1.66 |
| 1.00% (live) | 8.0% / -6.2% / 1.29 | 7.6% / -6.2% / 1.23 |
| OOS @0.5% | +11.4% / -4.7% / 2.43 | +13.0% / -4.4% / 2.96 |

On the real universe, vol-horizon actually IMPROVES Calmar at 0.25%/0.5% risk and OOS (up to
+22% at OOS), and is only modestly worse (-5%) at the live 1% risk setting -- the opposite shape
from the wrong-universe result. Genuinely mixed, not a clean win or a clean loss: it helps at
lower risk/longer OOS windows but slightly hurts at the specific risk level actually deployed
live. **Not adopted at the live 1% setting** (the level that matters), but this is a real,
un-noisy effect (consistent direction across 3 of 4 rows) worth revisiting if `RISK_PER_TRADE`
is ever lowered from 1% -- unlike the exhaustion-exit result above, this one isn't dismissible as
noise. No config change made this round; flagging as a genuine open question, not closing it.

**Housekeeping note:** this correction is a reminder to always pass `BROKER=ib UNIVERSE=etf`
(or use `active_universe()`, not the bare `--etf` flag) when a test is meant to represent the
actual live book -- `--etf` alone silently substitutes a different, smaller universe with no
error or warning. Worth fixing at the CLI level (make `--etf` warn or fail if it disagrees with
`active_universe()`) if this trips anyone up again; not done here, flagging as a known trap.

### 🔬 TESTED 2026-07-11: fifth critique round -- graduated DD risk-scaling and CAP=80%+sleeve combo
A fifth critique (in Chinese) proposed two genuinely new, testable ideas on top of one factual
error and one already-settled question:

**Factual error, corrected:** the critique's centerpiece claim was that the full 11-ticker sleeve
backtest is "still blank / the biggest source of uncertainty" (未知/最大不確定性). This is simply
wrong -- it was tested and documented THIS SAME DAY, earlier in this document (see "full
11-ticker sleeve blend" above): Calmar peaks at 10% weight (1.304), same shape as the 3-ticker
version. The critique appears to have been generated from a stale/earlier snapshot of this doc.

**Already-settled, no new test needed:** the critique's "sleeve weight 5% vs 10%" question is the
same question the already-published table above already answers (10% is Calmar-optimal, both the
3-ticker and 11-ticker versions).

**Idea: graduated DD-based risk scaling for new entries -- tested, REJECTED (dominates in the
wrong direction).** Added `DD_SCALE` to `research/backtest.py` (`--dd-scale T1:M1,T2:M2,...`):
unlike the binary `DD_HALT_PCT` (live-only, no backtest effect) or the already-rejected binary
`CIRCUIT_DD` ("kills CAGR, no DD help"), this scales NEW entries' risk continuously by the
CURRENT drawdown-from-peak at each entry decision, existing positions untouched. Tested the
critique's exact proposed ladder (5%dd->0.8x risk, 8%dd->0.5x, 11%dd->skip) and a more aggressive
variant, on the real 22-ETF book, no cash-yield (isolates the mechanism):

| config | n trades | CAGR | maxDD | Calmar |
|---|---|---|---|---|
| off (baseline) | 1285 | 5.14% | -8.75% | 0.588 |
| mild (5:0.8, 8:0.5, 11:skip) | 1285 | 5.05% | -8.84% | 0.572 |
| aggressive (3:0.7, 6:0.4, 9:skip) | 71 | 0.02% | -9.10% | 0.003 |

The mild version makes BOTH CAGR and maxDD simultaneously WORSE (not even a trade-off) --
reducing size right after a drawdown starts delays the recovery trades that would otherwise
close it out, so a SUBSEQUENT drawdown compounds on a still-lower base and ends up deeper. The
aggressive version is catastrophic: tightening the thresholds starves the system of 94% of its
entries (1285->71) because a trend book spends extended stretches below a recent peak, and
without new entries a trend system has no mechanism to earn its way back -- CAGR collapses to
~0%. This directly extends the already-documented finding that a binary tail-risk circuit
breaker "kills CAGR, no DD help" to the graduated case: softer doesn't fix the underlying
mechanism, it's the same failure mode at a smaller dose. **Rejected, no config change** (verified
via a synthetic unit test first that `DD_SCALE` actually changes `_portfolio()`'s output before
trusting the real-universe run -- confirmed working, not a silent no-op).

**Idea: PORTFOLIO_CAP=80% + sleeve@10% combo -- tested, REFUTES the critique's own predicted
number.** Built a permanent reusable tool for this recurring test, `research/sleeve_blend.py`
(this exact "core + sleeve at some weight/cap" test had already been rebuilt from scratch as
throwaway code twice this session -- worth keeping). Uses `active_universe()` directly (can't
silently drift from the live book) + the exact `core/sleeve.py` entry/exit spec on all 11
`SLEEVE_UNIVERSE` tickers:

| PORTFOLIO_CAP | sleeve weight | CAGR | maxDD | Sharpe | Calmar |
|---|---|---|---|---|---|
| 100% (current) | core only | 4.36% | -8.18% | 0.835 | 0.533 |
| 100% (current) | 5% | 6.15% | -6.36% | 1.090 | 0.967 |
| **100% (current)** | **10%** | **7.93%** | **-7.43%** | **1.139** | **1.068** |
| 100% (current) | 15% | 9.70% | -9.57% | 1.094 | 1.013 |
| 80% | core only | 3.80% | -7.85% | 0.823 | 0.485 |
| 80% | 5% | 5.59% | -6.38% | 1.095 | 0.876 |
| 80% | 10% | 7.35% | -7.24% | 1.124 | 1.015 |
| 80% | 15% | 9.11% | -9.57% | 1.067 | 0.951 |

The critique predicted this combo would reach **Calmar ~1.44**. The real number is **1.015** --
WORSE than the current CAP=100%+sleeve@10% config's 1.068, not better. `PORTFOLIO_CAP=80%`
combined with the sleeve is dominated by `PORTFOLIO_CAP=100%` at every sleeve weight tested here,
consistent with the already-documented standalone `CAP=80%` finding (worse OOS ratio). **Rejected,
kept `PORTFOLIO_CAP=1.0`.** (Note: this script's absolute CAGR figures run lower than the
cash-yield-modeled figures quoted elsewhere in this doc -- `sleeve_blend.py` doesn't model
cash-yield/margin-debit, same "compare relative shape, not absolute level across different
scripts" caveat as the earlier faithful-sleeve-reproduction entries. **Also ran at 0.5% risk, not
the live 1% -- see the correction directly below, which redoes this at the correct risk level.**)

### 🔬 TESTED 2026-07-11: sixth critique round -- sleeve x PORTFOLIO_CAP (properly scoped) and a parameter-interaction lesson
A sixth critique (well-reasoned, correctly flagged two real gaps rather than inventing numbers)
asked: (1) has the sleeve ever been blended against the core book at the EXACT current 4-parameter
config (cash-yield + `PORTFOLIO_CAP=1.0` together)? and (2) has `SL_ATR_MULT`/`RR_DEFAULT`/
`OVEREXT_HI` ever been swept under `PORTFOLIO_CAP` (the existing `param_sensitivity.py` sweep
predates it, 2026-07-09)? It also proposed `MIN_STRENGTH>5`, which turned out to be a
misunderstanding of the scoring scale, not a real lever.

**1. Sleeve x PORTFOLIO_CAP, done properly this time (added `--cash-yield`/`--risk` to
`sleeve_blend.py`).** The FIRST attempt at this (table just above) ran at 0.5% risk, not the
live 1%, and without cash-yield -- caught and corrected before reporting it. Re-ran at the
actual live settings (`--pos-cap 0.25 --portfolio-cap 1.0 --cash-yield --risk 0.01`):

| weight | CAGR | maxDD | Sharpe | Calmar |
|---|---|---|---|---|
| core only | 6.44% | -6.83% | 1.032 | 0.943 |
| 5% | 8.26% | -6.60% | 1.253 | **1.251** |
| 10% | 10.06% | -8.12% | 1.299 | 1.239 |
| 15% | 11.85% | -9.80% | 1.248 | 1.209 |

The core-only row (6.44%/-6.83%/0.943) matches the previously-documented "core only (hybrid
cap)" figure exactly, cross-validating the new tool against the old one-off script despite
using a different (correct, `active_universe()`-based) instrument list. **Both 5% and 10%
weight clear the critique's own "1.2-1.4 = final form" bar** -- but Calmar is a near-flat
PLATEAU across 5-10% (1.251 vs 1.239, ~1% apart), not the sharp peak-at-10% the earlier
(differently-scoped) tests suggested. Read this as "5-10% are statistically indistinguishable
here," not "5% is now definitively better." Still zero real sleeve fills exist in the paper
journal -- this settles the numbers question, not the deploy question.

**2. ATR-SL-mult / RR-mult / OVEREXT sweep under the CURRENT config -- individually promising,
but a clean lesson in why one-at-a-time sweeps mislead.** Re-ran `param_sensitivity.py` with
`PORTFOLIO_CAP=1.0` added (the 2026-07-09 version never had it) and a `MIN_STRENGTH` probe added:

| parameter | -20% | baseline | +20% |
|---|---|---|---|
| SL_ATR_MULT | **0.566** | 0.533 | 0.443 |
| RR_DEFAULT | 0.480 | 0.533 | **0.602** |
| HORIZON_DAYS | 0.526 | 0.533 | 0.546 |
| OVEREXT band | **0.561** | 0.533 | 0.485 |
(ratio = CAGR/|maxDD|; bold = the favourable direction)

Unlike the 2026-07-09 sweep (baseline was locally optimal on all 4), THIS time three parameters
each show a one-directional improvement in isolation: tighter stop (SL_ATR_MULT -20%), wider
target (RR_DEFAULT +20%), and a tighter overextension filter (OVEREXT 65/35) each individually
beat baseline. **Tested whether stacking all three together compounds or cancels, rather than
assuming additivity:**

| config | n | CAGR | maxDD | ratio |
|---|---|---|---|---|
| baseline | 1285 | +4.36% | -8.18% | 0.533 |
| **COMBINED (all 3 favourable directions)** | 1104 | +3.80% | -8.11% | **0.468** |

**The combination is WORSE than baseline (0.468 vs 0.533), not better** -- the three individually
"favourable" nudges interact and cancel rather than compound. This is exactly the overfitting
trap one-at-a-time parameter sweeps are prone to, and is why this project's convention has always
been to verify a combination directly rather than assume individual deltas add up (same lesson
as the earlier `2x risk` / `portfolio_cap 105%` tests, which also looked individually plausible
and failed when actually run). **No config change** -- the current baseline
(`SL_ATR_MULT=1.5, RR_DEFAULT=3.0, HORIZON_DAYS=5, OVEREXT=70/30`) remains the best JOINTLY-
tested configuration, even though it's no longer the best on every individual axis in isolation.

**3. `MIN_STRENGTH>5` -- not a real parameter, confirmed empirically.** `scoring.py` clamps
`strength = max(1, min(5, ...))` -- a hard 1-5 scale -- and the live config's `MIN_STRENGTH=5`
is already that ceiling. Raising it to 6 doesn't tighten the filter, it disables the strategy:
confirmed by running it, `n=0` signals, zero trades. The critique's premise (that 6/7/8 are
untested threshold values worth sweeping) doesn't hold -- there's no headroom above 5 to sweep.

### 🐞🐞🐞 FOUND & FIXED 2026-07-11: `current_drawdown_pct()` could have permanently bricked live trading via a bogus -90% "drawdown"
User asked why the live account still had no open trade. Investigation (not the first assumption
taken at face value) found TWO separate, compounding reasons, one benign and one a real bug that
needed fixing before it caused damage:

**1. Benign: it's Saturday.** `app.py`'s `_do_llm()` skips the LLM board scan (and therefore
`paper.place_from_state()` and `ib_exec.mirror_new()`, the whole chain that creates and places new
trades) whenever `SETTINGS["auto_pause"]` is on (default) and `_market_open()` is False --
Mon-Fri only. Confirmed via the log: last board scan was 00:00:14 today, zero since (11+ hours),
while the lighter "cheap refresh" cycle kept running every ~1min as expected. Working as designed,
resumes automatically Monday.

**2. Also benign, now resolved on its own: 7 stale "already open" blocks finally expired.** The
7 tickers from the earlier-this-session ghost-order cleanup (`CPER,EEM,DBC,VNQ,CWB,AMLP,ASHR` --
orders rejected by the since-fixed Error 435/10349 bug, but the local paper-side signal tracker
kept marking them OPEN) had been blocking new entries on those names via "skip (already open)"
in the funnel log for days. They hit their time horizon and organically resolved as EXPIRED
around 11:23-11:29 today -- confirmed via the log and a direct DB check, `0 OPEN paper trades`
now on live. Everything else being skipped in the funnel log (overextended RSI on SPY/QQQ/IWM,
trend strength 4<5 on HYG/EFA/HYD, R:R too low) is ordinary `MIN_STRENGTH=5` selectivity, not a
bug -- this system does ~1.5 trades/week across 22 instruments, not every name fires every day.

**3. NOT benign -- a real bug that would have blocked live trading indefinitely once triggered.**
While checking whether `DD_HALT_PCT` (implemented earlier today) was itself responsible, queried
`paper.current_drawdown_pct()` directly on the live account and got **-90.5%** -- wildly
implausible for a small account with no reported crisis. Root cause: `current_drawdown_pct()`
initializes `peak = adj[0]`, the deposit-adjusted value at the FIRST history point. For a
brand-new account, that first point is whatever tiny leftover cash sat there BEFORE the first
real deposit landed (here, 40 HKD -- confirmed via the raw `equity_history`/`cash_flows` cache:
`hist[0] = [ts, 40.0, 'HKD']`, then two deposits totalling ~99,984.61 HKD). Once deposits are
netted out, the account's real trading P&L has barely moved off zero yet (it's brand new) -- so
`peak` stays pinned at that ~$5 pre-funding artifact, and ANY trivial dip below it (a few dollars
of commissions) computes as a huge PERCENTAGE drawdown purely because the denominator is
economically meaningless. Grepped the full log (43k lines back to 2026-07-07): **zero
`DD-halt:` messages ever**, despite `mirror_new()` (which checks this every ~15min whenever the
LLM scan runs) having fired 100+ times on Friday alone -- meaning either the DD_HALT code hadn't
reached the running live process yet, or it had and was one restart away from permanently halting
all new live entries over a few-dollar rounding artifact, defeating the whole point of a small,
young, contribution-fed account being allowed to actually deploy capital.

**Fix:** added a materiality floor to `current_drawdown_pct()` in `core/paper.py` -- require
`peak` to be at least 1% of the account's CURRENT raw equity before trusting a percentage;
below that, return 0.0 (not enough real trading P&L yet to judge, same spirit as the existing
`len(hist)<2` guard). Verified directly against the live account's real cache data: -90.5% ->
0.0% after the fix. Extended `test_paper.py` with two new cases (the exact bug shape, and a
control case confirming a genuine drawdown ABOVE the floor still registers correctly) -- both
pass, plus all 4 pre-existing `current_drawdown_pct` cases still pass unaffected (their reference
peaks are all far above their respective 1%-of-final floors). Deployed: restarted both
`DashboardApp` and `DashboardAppLive` (killed an orphaned process still holding port 8081 first),
confirmed live reconnected to `U12991898`, confirmed `current_drawdown_pct()` now reads 0.0% on
the real live cache post-restart. This bug affected BOTH the "Drawdown from peak" UI stat (would
have shown a false, alarming -90% to the user) and the `DD_HALT_PCT` live-trading gate (would
have silently and permanently blocked all new live entries) -- caught before the second one ever
had a chance to fire for real.

### 🐞 FOUND & FIXED 2026-07-11: `_market_open()` used the box's LOCAL (HK) clock, not US market time
User asked to verify: `_market_open()` (the weekend auto-pause gate in `app.py`) should check US
Eastern time, since the live/paper config (`BROKER=ib UNIVERSE=etf`) trades NYSE-listed ETFs, not
local-timezone or 24h FX. Verified directly with `zoneinfo` rather than assumed: this box's system
clock is `Asia/Hong_Kong` (UTC+8), 12h ahead of US Eastern (EDT in July, UTC-4) -- confirmed via
`Get-TimeZone` and a direct conversion (`HK Sat 02:00 -> ET Fri 14:00`, weekday Friday not
Saturday). Using the LOCAL weekday meant roughly half a day of misalignment at EACH week boundary:

- **HK Sat 00:00-04:00** = ET **Fri 12:00pm-4:00pm** (still regular NYSE hours) -- wrongly
  treated as closed, pausing the LLM scan (and therefore all new-trade placement) during real
  trading hours.
- **HK Mon 00:00-21:30** = ET **Sun noon through Mon pre-market** -- wrongly treated as open.

This wasn't theoretical: the SAME investigation that found the drawdown bug above showed the
live account's last board scan fired at exactly `00:00:14` HK time -- which was `12:00pm Friday
ET`, cutting off the rest of Friday's real trading session at the moment HK crossed into
Saturday, hours before the actual US market closed.

**Fix:** `_market_open()` now checks `_ib_broker()` -- if true (the current live/paper config),
uses `datetime.now(ZoneInfo("America/New_York"))` instead of naive local time; the legacy MT5/FX
path (~24h market, no single relevant exchange timezone) is left on local time, unchanged.
Verified against both broken boundaries directly (not just reasoned about): `HK Sat 02:00`
now correctly resolves to `ET Fri` (weekday 4, market open); `HK Mon 08:00` now correctly
resolves to `ET Sun 20:00` (weekday 6, market closed). Still only a day-of-week guard, not an
intraday-hours check (the broker itself enforces 9:30-16:00 ET at order time) -- unchanged scope
from before, just the correct clock. Deployed: restarted both `DashboardApp` and
`DashboardAppLive`.

### 🔬 TESTED 2026-07-11: seventh/eighth critique round -- signal-quality/exit ideas, all rejected on corrected data
Two more critiques (one proposing trailing-stop/time-decay/momentum-filter/universe-health exits,
one proposing risk-dial and RR/sleeve tweaks) arrived while the scope-bug correction above was
underway -- tested against the CORRECTED (30y weekly) data throughout.

**Critique A's 4 ideas:**
1. **Trailing-stop replacing fixed 3R TP** -- REJECTED. Tested the EXACT proposed mechanism
   (arm at 1.5R, trail 2xATR-equivalent / 1.5R) plus the simpler `breakeven@+1R` variant. All
   underperform fixed on the corrected data (see the correction table above) -- worse IS, worse
   OOS expR, worse full-history ratio, worse in 5/6 walk-forward windows for breakeven, and the
   critique's own proposed arm1.5R/dist2R variant is worse on 3 of 4 corrected metrics too.
2. **Time-decay exit** -- REJECTED, but the closest of the four to a real signal. A single IS/OOS
   split looked promising (full-history ratio 0.53→0.60); walk-forward across 6 windows shows it's
   a coin flip (wins 2, loses 2, ties 2, aggregate ratio ~unchanged 0.985 vs 0.986) and it's WORSE
   than fixed at the actual live 1% risk setting (0.826 vs 0.854). Same "one lucky split" lesson
   as breakeven, just less dramatically wrong.
3. **Cross-sectional momentum filter** (`--mom-filter`) -- REJECTED. Worse at every risk level
   (1% risk Calmar 0.475 vs baseline 0.540) and worse OOS (1.117 vs 2.022). The theory (filter
   out weak "follow the herd" breakouts) didn't pan out -- maxDD got WORSE despite fewer trades,
   the opposite of the predicted effect.
4. **Universe-health-based dynamic PORTFOLIO_CAP** (cut exposure when <20% of the book shows
   strength>=5) -- NOT implemented/tested this round (lowest priority in the critique's own
   ranking, and mechanistically the same family as the already-tested-and-inert `DD_SCALE`/
   already-rejected `CIRCUIT_DD` regime-conditioning ideas -- a breadth-based trigger instead of
   a drawdown-based one, same "reduce exposure based on a lagging/coincident signal" pattern that
   has failed every time it's been tried in this project, including the VIX-regime ladder).
   Flagged as low-priority and likely low-probability rather than built.

**Critique B, fact-checked:**
- **"當前系統 Calmar 約0.63~1.0" citing "1%風險(核心): 7.14%/-11.3%/0.63"** -- this is the EXACT
  same stale, already-debunked figure from an earlier critique round (see the 2026-07-11 entry
  above: "Critique 2's entire baseline was wrong... matches neither the current hybrid config...
  nor any pre-hybrid figure"). It predates `PORTFOLIO_CAP` entirely. The real current-config
  Calmar (correct 30y weekly data, cash-yield on) is **0.854 at 1% risk / 2.335 OOS**, not 0.63.
- **"Leveraged Forex 仍缺，這是當前卡關點"** -- FACTUALLY OUTDATED. This was confirmed approved
  and `keep_cash_usd()` confirmed working with a REAL fill (`USD 100.00` balance, HKD
  10,040→9,240.27) on 2026-07-10, a full day before this critique. Not a current blocker.
- **RR_DEFAULT 3.0→3.6 alone (ratio 0.533→0.602)** and **sleeve 5% (Calmar 1.251) vs 10%
  (1.239)** -- both ACCURATE, correctly sourced from already-verified, correctly-scoped tests
  (`param_sensitivity.py`/`sleeve_blend.py`, neither affected by the daily-bar bug since they
  fetch weekly bars directly). No new action -- already documented, and the RR=3.6 finding was
  already shown NOT to survive combination with the other "individually favourable" tweaks (see
  the sixth critique round's COMBINED test: 0.468, worse than baseline).
- **Risk 1%→0.5% "doubles Calmar"** -- the DIRECTION is real (lower risk = lower leverage = less
  drag from `MARGIN_DEBIT_RATE`) but the MAGNITUDE is overstated, built on the same stale 0.63
  baseline. Real numbers: 1% risk Calmar 0.854, 0.5% risk Calmar 0.774 -- LOWER, not higher, on
  the corrected data (0.25% risk is highest at 0.849, but that's inert due to the cap barely
  binding, not a real edge). This directly contradicts the critique's claimed direction.

**Net result: no config or code change from either critique.** The live config
(`RISK_PER_TRADE=0.01, ETF_POS_CAP=0.25, PORTFOLIO_CAP=1.0, DD_HALT_PCT=-13.0`) stands.

### 🔧 SELF-AUDIT 2026-07-11: closed 4 open questions from a "what's still missing" review
Asked directly (not prompted by an external critique this time) what testing/coverage gaps
remained after the scope-bug correction above. Four were concrete enough to act on:

**1. Audited pre-2026-07-11 HANDOFF results for the SAME daily/weekly scope bug.** Grepped
every documented `backtest.py` invocation. All explicitly cite `--longweekly` (EMB/PFF
isolation, ETF batch-3 through -10 screens, the futures class battery) OR carry strong internal
evidence of correct weekly scope (the futures `LOCKED STRATEGY SPEC`'s "26.4y" span and "avg
hold 3.3wk" figures are only possible on weekly bars -- 5yr daily data can't produce a 26-year
backtest at all). **Pre-today research appears unaffected** -- the scope bug was specific to
today's rapid-fire critique-testing, not a standing practice gap. Not 100% provable without
re-running 3 weeks of history, but the internal-consistency evidence is strong.

**2. Real-fill cost reconciliation -- BLOCKED, not yet possible.** Wanted to compare the
backtest's assumed ~10bp cost against actual broker fill slippage. Checked: **zero CLOSED
trades exist anywhere** (paper: 7 positions, all still OPEN; live: 0 open, the ghost-order
batch was archived not closed with real fill data). There's nothing to reconcile against yet --
revisit once the paper book has real closed round-trips (the scheduled 2026-07-18 sleeve check
is a natural point to also look at this for the core book).

**3. `DD_HALT_PCT` had never been tested end-to-end, only its pure function in isolation** --
and a bug in that pure function alone (the -90% "drawdown" bug found earlier today) came close
to permanently halting live trading. Added a real integration test
(`test_ib_exec.py::test_mirror_new_dd_halt_end_to_end`) that mocks out the IB connection
(`_guard()`) and the cache (`store.cache_get`) and calls the ACTUAL `mirror_new()` function --
not a re-implementation of its logic. Confirmed: a genuine -20% synthetic drawdown makes
`mirror_new()` emit the real `log.warning` and return exactly the one halt message (verified
the message contains the correct computed % and the literal text "DD-halt"); a shallow -5%
drawdown does NOT take that path (confirmed by mocking `_equity_usd` to raise a sentinel
exception if execution reaches that far, rather than letting a fake `ib` object attempt a real
network connection, which the first draft of this test accidentally did -- cleaned up before
committing). All 14 tests (10 existing + 4 new) pass.

**4. Inception-date bias in the 30-year backtest -- confirmed real, quantified, not fixable
(nothing to "fix," it's a fact about market history), now documented precisely.** Pulled actual
yfinance inception dates for all 22 live-universe tickers: `SPY 1993, DIA 1998, QQQ 1999, IWM
2000, EFA 2001, IEF/TLT/SHY 2002, EEM/TIP 2003, VNQ/GLD 2004, DBC 2006, SLV 2006, PFF/HYG 2007,
HYD/CWB 2009, AMLP/VNQI 2010, CPER 2011, ASHR 2013`. Mapped against this session's 6-window
walk-forward: **window 1 (1996-2001) ran on a 2-3 ticker universe (SPY/DIA/QQQ only); window 2
(2001-2006) grew to ~13; window 3 (2006-2011) reached ~20; only windows 4-6 (2011-2026) ran on
the full current 22-name book** (ASHR, the last addition, joined Nov 2013). Windows 1 and 2 are
exactly the two weakest windows in every walk-forward test this session (ratio 0.534 and 0.060)
-- previously attributed only to "different market regime" (2026-07-09 `walk_forward.py`
entry); now clear that under-diversification is an independent, compounding factor, not just
regime. **Practical implication: the "full 30-year Calmar" figures used throughout this doc as
the conservative anchor UNDERSTATE what the current fully-diversified 22-ETF system should be
expected to do** -- roughly the first half of the measurement window wasn't testing this
strategy's current form at all. Doesn't change any config (there's no way to "fix" history not
existing), but reweights how much trust to put in full-history vs. recent-window figures when
they diverge -- lean toward 2011-2026 for what to actually expect, not 1996-2026 blended.

### 🔧 SELF-AUDIT 2026-07-11, part 2: 4 more gaps -- universe-selection bias, live-vs-backtest
tool, bootstrap CI, targeted crisis stress test

**1. Universe-selection bias, honestly quantified -- concern was real, verdict is reassuring.**
The 22-ETF book wasn't hand-picked; it was assembled by screening candidates in ~10 rounds
(the original --etf-screen round plus batches 2-10) and keeping the ones with positive
isolation-tested edge. Counted precisely from `instruments.py`: **49 total candidate ETFs
screened** beyond the structurally-mapped base-10 (12 adopted into `ETF_CANDIDATES`, 37
rejected/deferred across `ETF_SCREEN_BATCH` through `_10`). That's real multiple-comparisons
exposure that `deflated_sharpe_ratio()` has only ever been called with `n_trials=1` against.
Recomputed on the real per-trade R series (n=1285, full weekly history, current config):

| n_trials | DSR |
|---|---|
| 1 (as currently reported everywhere) | 100.0% |
| 12 (adopted-candidate count only) | 100.0% |
| 49 (full honest search breadth) | 100.0% |

**DSR is unmoved even at the full 49-trial correction.** With n=1285 trades, the expected-
max-Sharpe-under-the-null bar only grows logarithmically with `n_trials`, and the strategy's
observed edge clears it comfortably regardless. The concern was legitimate to check and hadn't
been checked before -- but the answer is good news, not a hidden problem.

**2. Built `research/live_vs_backtest.py`** -- compares REAL closed trades (core strategy only,
sleeve excluded) against the backtest's expected win-rate/expectancy via a binomial test +
one-sample t-test, gated by `paper.stats()`'s own `trustworthy` (n>=30) flag so a small sample
can't masquerade as a settled verdict. Tested against synthetic data (binomial/t-test logic
confirmed correct); run for real against both DBs -- **0 closed trades exist anywhere**, so it
currently (correctly) reports "nothing to compare yet" rather than fabricating a comparison.
Ready to fire the moment real closed trades exist -- no fresh investigation needed later.

**3. Built `research/bootstrap_ci.py`** -- a moving-BLOCK bootstrap by calendar YEAR (not a
naive i.i.d. per-trade resample, which would shred the real autocorrelation trend-following
trades have within a regime) that re-runs the ACTUAL `_portfolio()`/`_metrics()` pipeline
(position sizing, one-per-instrument de-correlation, `POS_CAP`/`PORTFOLIO_CAP` all apply
exactly as in the real backtest) on 500 resampled 30-year timelines. Result (current config,
strategy-only, no cash-yield):

| metric | point estimate | bootstrap median | 90% CI |
|---|---|---|---|
| CAGR | +5.15% | +6.10% | [+4.05%, +8.35%] |
| maxDD | -8.75% | -8.74% | [-13.46%, -6.67%] |
| Calmar | 0.588 | 0.680 | [0.342, 1.161] |

**P(Calmar < 0) = 0%** (the edge itself is robust across regime-resequencing draws) but
**P(Calmar < 0.5) = 20.6%** -- a real ~1-in-5 chance of landing below the level several
critiques this session used as a rough "is this even working" threshold, purely from WHICH
years happen to occur and in what order (not from anything wrong with the strategy). Read
every point-estimate Calmar in this document as sitting inside a real, fairly wide band, not
as a precise number.

**⭐ FIXED 2026-07-12: this band was strategy-only (no cash-yield) by construction, which meant
it never actually applied to the current best-validated headline (Calmar 0.943, cash-yield ON --
see the sleeve_blend.py `--cash-yield` table above), and mixing the two in later summaries
produced nonsense deltas** (an external critique pasted into this session computed
"0.943 -> 0.488 = -48%" as if those were the same quantity's before/after; they're two
different scripts' outputs on two different configs -- the correct within-methodology
comparison is 0.588->0.488, -17% relative, from `dividend_tax_drag.py`). Root cause: a real
^IRX-rate series can't be looked up against a resampled/reindexed synthetic timeline's fake
dates, so cash-yield was disabled entirely rather than fixed. **Fix: use a CONSTANT rate
(`bt.CASH_YIELD = 0.043`, today's IB USD rate) instead of the real dated series** -- a constant
is immune to the reindexing problem (`_rate()` in backtest.py returns it unconditionally). Also
folded in the SAME trade-count-weighted dividend-withholding drag as `dividend_tax_drag.py`
(-0.88pp/yr), applied per-draw, so this is now ONE self-consistent number: after-tax,
cash-yield-inclusive, with real bootstrapped uncertainty. Re-ran (500 draws):

| metric | point estimate | bootstrap median | 90% CI |
|---|---|---|---|
| CAGR (after-tax) | +6.06% | +6.75% | [+4.82%, +8.93%] |
| maxDD | -6.83% | -6.83% | [-10.68%, -6.09%] |
| Calmar | 0.887 | 0.921 | [0.536, 1.355] |

**This reconciles all three previously-disparate core-only Calmar figures in this doc (0.588
strategy-only, 0.854 an earlier cash-yield-on headline, 0.943 the current cross-validated
headline) into one band centered right where the 0.943 headline sits** -- cash-yield's steady,
low-volatility income cushions the downside tail substantially: **P(Calmar < 0.5) drops from
20.6% to 3.4%** once it's properly included. Treat **[0.536, 1.355], median 0.921** as the
current single source of truth for "how uncertain is the after-tax Calmar", superseding the
[0.342, 1.161] figure above (kept in place, not deleted, so the reasoning trail stays intact).

**4. Built `research/stress_test.py`** -- isolates what the CURRENT exact config did during
specific historical crises (peak-to-trough using ONLY that window's own running peak, not
blended into a multi-year walk-forward average that can hide the worst days):

| event | window | return | worst intra-window DD |
|---|---|---|---|
| 2008 GFC | 2007-09 to 2009-03 | **+9.69%** | -3.11% |
| 2020 COVID crash | 2020-02 to 2020-05 | -0.81% | -3.20% |
| 2022 rate-hike drawdown | 2022-01 to 2022-12 | +3.61% | -5.11% |

**Genuinely reassuring**: positive return through 2 of the 3 crises (including a +9.7% GFC,
consistent with trend-following's classic "does well in sustained directional stress" profile),
and the worst intra-crisis drawdown across all three is only -5.11% (2022) -- far inside the
-13% `DD_HALT_PCT` threshold and inside the already-documented full-history maxDD. The
diversification/de-correlation design (breadth across asset classes, one-per-instrument cap)
appears to be doing its job specifically where it matters most, not just untested.

All three new scripts follow the established "fetch data once, reusable tool" convention. No
config changes from any of these four -- they're verification/tooling, not new signals.

### 🔧 SELF-AUDIT 2026-07-11, part 3: dividend withholding tax, data quality, liquidity, cap freshness

**1. Dividend withholding tax on the core book -- flagged before, now quantified, and it's a
real number.** HANDOFF previously dismissed this via reasoning only, specific to the SLEEVE's
short ~3wk holds ("weak for a 3wk-hold book, matters on bond sleeves only") -- never quantified
for the CORE book's repeated, cumulative exposure to 7 meaningfully-yielding tickers (TLT, IEF,
SHY, HYG, TIP, PFF, HYD, plus VNQ/VNQI/AMLP/DBC/EFA at 4-9%+ trailing yield). `yfinance`'s
`auto_adjust=True` (used in every backtest here) folds every dividend back into price as if
reinvested 100% tax-free; a real HK NRA account has 30% withheld on US-source dividends before
they land. Built `research/dividend_tax_drag.py`: trade-count-weighted blended portfolio yield
2.93%, so 0.30 x 2.93% = **-0.88pp/yr CAGR drag**. On the strategy-only (no cash-yield) current
config: **Calmar 0.588 -> 0.488 after tax** -- crosses below the "0.5" line several critiques
this session used informally as a rough pass/fail threshold. maxDD is essentially unchanged (a
steady yield drag lowers the compounding rate, it doesn't deepen the worst single drawdown). On
the cash-yield-ON headline figures quoted elsewhere in this doc (CAGR 5.83%/maxDD -6.83%,
Calmar 0.854) the same -0.88pp arithmetic gives **~4.95% CAGR / Calmar ~0.725** -- back-of-
envelope, not a fresh full run, but maxDD-invariance makes this a reasonable extension. **This
is an ESTIMATE** (trade-count-weighted average yield x 30% applied as a constant annual drag),
not a bar-by-bar after-tax price reconstruction -- real drag varies by which specific tickers
are held when. **Caught and fixed a real bug while building this**: the first version computed
`years` from `cands[0]`/`cands[-1]` on an UNSORTED candidate list, picking up whichever
instrument happened to iterate first/last rather than the true chronological span -- inflated
CAGR to a nonsensical 8.57% before the fix (same class of self-caught methodology bug as the
daily/weekly scope bug earlier this session; every other script built today explicitly sorted
first). **No config change** -- this doesn't change what the strategy does, it changes how much
of its return an HK NRA account actually keeps after tax; worth remembering when comparing any
point-estimate Calmar in this doc against a personal target.

**2. Data-quality audit -- clean.** Built `research/data_quality_audit.py` (checks zero-volume
weeks, mid-history gaps >21d, single-week jumps >25% unexplained by a nearby split) across all
22 tickers. 5 flagged (SLV -26.5% 2011-05-02, EEM +28.3% 2008-10-27, PFF +37.5% 2009-03-09,
AMLP -30.4% 2020-03-09, CPER zero-volume weeks from 2012-10-01) -- **every single one traces to
a well-known real historical event**, not corrupt data: the May-2011 silver crash (CME margin
hikes), Oct-2008 GFC EM whipsaw, the exact March-2009 GFC bottom (preferred-stock ETFs were
hammered into it then violently rebounded), the March-2020 COVID+oil-price-war double-hit on
MLP energy infrastructure, and CPER's genuinely thin early-history liquidity shortly after its
2011 launch. No garbage-in-garbage-out risk found.

**3. Liquidity/capacity check -- clean at current scale, first constraint appears ~$1M equity.**
Built `research/liquidity_check.py`: pulled real 30-day average $ volume for all 22 tickers and
compared against a 25%-of-equity (`ETF_POS_CAP`) position at 3 account sizes. At $130k (current
paper-equivalent) and $500k, **no ticker's max position exceeds 1% of its own daily $ volume**
-- the flat ~10bp cost assumption holds. At $1M, two names edge past the 1% heuristic (VNQI
1.6%, CPER 1.3%) -- worth revisiting the cost model specifically for those two if/when the
account approaches that size, everything else stays comfortably liquid even there.

**4. `PORTFOLIO_CAP` equity freshness -- clean, no staleness risk.** Checked `ib_exec._equity_usd()`
directly: it calls `ib_client.account_summary()` -> `ib.accountSummaryAsync()`, a genuinely
live IBKR request with NO caching anywhere in the chain. A new contribution landing mid-cycle
is picked up on the very next `mirror_new()` call, not stale by a refresh-interval's worth of
lag. No fix needed.

All three new scripts (`dividend_tax_drag.py`, `data_quality_audit.py`, `liquidity_check.py`)
follow the established "fetch once, reusable tool" convention. No config changes -- items 2-4
are clean, item 1 is a real number worth carrying forward when interpreting future Calmar
figures, not a bug to fix.

**Also verified: the sleeve's `PHASE2_NAV_USD=$64,000` gate was justified using an outdated
edge estimate.** The original reasoning (+1.5pp CAGR ≈ 1,500 HKD/yr "negligible vs
contributions") predates this session's corrected re-verification, which found the REAL 10%-
weight sleeve edge is +3.62pp CAGR (6.44%->10.06% core-only vs blended, live 1% risk +
cash-yield, correct 22-ETF scope) -- **2.4x larger** than assumed. Applying the same "wait
until the dollar edge stops being negligible" logic to the corrected figure implies an
equivalent crossover around ~210K HKD, not 500K HKD. Mechanically, sleeve position sizing is
NOT degenerate even at current live equity (~$12.8k): SPY/QQQ entries size to ~1.7 shares,
handled fine by the already-enabled fractional-share trading. One part of the original
reasoning ("commission-sensitive fills") remains genuinely unquantified -- no IBKR fee-schedule
data was checked. **No config change made** -- flagged for a decision, not acted on
unilaterally given it's a real-money parameter.

### ⭐ DECIDED 2026-07-12: `PHASE2_NAV_USD` set to 0 on BOTH instances -- sleeve equity gate removed
User-confirmed decision (after the verification above, plus a same-day round confirming
sleeve clustering is already priced into the backtest, core/sleeve correlation is genuinely
~0, and IBKR commission drag is smaller than assumed): rather than pick a specific lower
dollar threshold (a $27,000/~210K HKD candidate was derived and verified clean first, per the
same-day request), the user chose to remove the equity gate entirely, effective immediately,
rather than wait ~3.6 months for the account to grow into even that lower number.

**Set `$env:PHASE2_NAV_USD = "0"` in both `run_dashboard_live.ps1` (in-repo) and
`C:\Scripts\dashboard.ps1` (paper, outside the repo)** -- verified directly (not just deployed
blind): `PHASE2_NAV_USD=0` correctly makes `sleeve_active(equity)` return `True` for any real
positive equity reading (`account_phase(12819)` -> 2, `sleeve_active(None)` still correctly
stays `False` -- an unknown-equity reading doesn't accidentally activate anything). This does
NOT force a trade -- `sleeve_active()` is only ONE of two independent gates
(`SLEEVE_ENABLED` + this one); the sleeve's actual VIX-panic entry condition still has to fire
for real before any order is placed. Zero real sleeve fills exist on either account as of this
change (confirmed earlier this session) -- this just means the NEXT real trigger, whenever it
comes, will be allowed through instead of blocked.

**Deployment hit a real, unrelated snag worth recording**: after restarting both dashboards,
`DashboardApp` (paper) came up on a stuck/silent process (port 8080 never bound, ~5s CPU burned
over several minutes, IBC gateway login itself completed cleanly per its own log -- the stall
was in the Python process, not the broker connection). Root cause not conclusively identified
(possibly a stale `Global\DashboardAppMutex` from an earlier session, though Windows normally
releases mutexes on process exit) -- resolved by explicitly killing the specific stuck PIDs
(not just `Stop-ScheduledTask`, which had already been tried once and didn't clear it) and
doing one more clean `Start-ScheduledTask`. Both dashboards confirmed healthy afterward: paper
reconnected to DUK968178 (clientId=7, port 4002), live to U12991898 (clientId=21, port 4001).
**Also flagged and resolved a false alarm during this same investigation**: a "DD-halt: current
drawdown -20.0%" warning in the shared log around this time was NOT a real live-trading halt --
it was `test_ib_exec.py`'s own `test_mirror_new_dd_halt_end_to_end` (run earlier the same turn
as part of the pre-deploy test suite) writing its synthetic -20% test fixture's `log.warning()`
call to the same shared `logs/dashboard.log` file the real dashboards use. Verified directly
against the live DB: real current drawdown was 0.0% throughout, nothing was ever actually
blocked. Worth remembering: this shared log file mixes real dashboard output with any test
run's genuine (not mocked) logging calls -- a timestamp match isn't enough to assume real impact
without checking the DB directly, which is exactly what settled it here.

**Added `--oos` to `sleeve_blend.py`** (previously full-history-only) to get the OOS-window
figure for the core+sleeve@10% combination directly, rather than estimating it:

| scope | CAGR | maxDD | Sharpe | Calmar |
|---|---|---|---|---|
| Full-history, core only | 6.44% | -6.83% | 1.032 | 0.943 |
| Full-history, core+sleeve@10% | 10.08% | -7.73% | 1.302 | 1.305 |
| OOS, core only | 9.63% | -6.65% | 1.339 | 1.449 |
| **OOS, core+sleeve@10%** | **13.08%** | **-7.73%** | **1.806** | **1.693** |

Consistent with every other OOS-vs-full comparison in this project: meaningfully better in the
recent-decade window, but per this project's own stated discipline, treat full-history as the
conservative anchor and OOS as the bull-flattered recent-regime case, not the number to plan
around.

### 🚨🚨🚨 FOUND & FIXED 2026-07-12: the ENTIRE automated trading/monitoring loop was silently
dependent on a browser tab being open -- the most significant bug found this session

**How it was found**: after setting `PHASE2_NAV_USD=0` to let the sleeve trigger on live, its
`sleeve_first_active_ts` stayed `None` for over an hour across multiple restarts, with zero
"cheap refresh" log lines appearing for 20+ minutes at a stretch despite both dashboards
returning healthy HTTP 200 to page loads the entire time. Initially suspected (and partially
fixed as defense-in-depth) a hung `await` inside `_tick()` permanently blocking the `_busy`
flag. That fix (a 120s `asyncio.wait_for` ceiling) was deployed but did NOT resolve the
dormancy -- and critically, it never even logged its own "hung for >120s" message, which it
would have if a hang were the real cause. That absence was the tell.

**Root cause**: `app.py` scheduled the master data/trading tick via
`ui.timer(30.0, _tick)` called INSIDE the per-client `@ui.page('/')` render function -- a
NiceGUI pattern that ties the timer's lifetime to that specific browser client's connection.
With zero browser clients connected to either dashboard (the normal state for a
scheduled-task-run background service that nobody is actively viewing), **`_tick()` never
fired at all** -- no signal generation, no order placement, no `DD_HALT_PCT` checks, no
broker reconciliation, no sleeve entries, nothing. The web server itself (a separate async
concern in NiceGUI) kept responding HTTP 200 to any page load throughout, so every health
check this session that only checked "does it respond" (curl, `Get-NetTCPConnection`) reported
green while the actual trading system was completely inert.

**Verified directly, not just reasoned about**: opened a real browser tab to the live
dashboard while it had been dormant for 20+ minutes -- the INSTANT the client connected, a
cheap refresh fired and the sleeve's staged-rollout clock (stuck at `None` the whole time)
recorded its first activation immediately. Confirmed the reverse too: after deploying the fix
and navigating the only open browser tab AWAY from the dashboard (zero clients connected),
"cheap refresh" log lines continued appearing every ~30-60s on their own.

**Fix**: moved tick scheduling to `app.on_startup(lambda: asyncio.create_task(_tick_loop()))`
-- a persistent background asyncio task, entirely independent of any browser client, that
calls `_tick()` in a `while True: ...; await asyncio.sleep(30)` loop. Removed the per-client
`ui.timer(30.0, _tick)`/`ui.timer(0.1, _tick, once=True)` calls from the page function
entirely (replaced the "kick off immediately on load" behavior with a direct
`_refresh_all_panels()` call so a newly-connecting client's first paint reflects current state
without waiting up to 30s). Kept the per-client `ui.timer(1.0, _ui_tick)` for clock/age-label
display -- that one is fine being client-scoped since it's pure re-rendering from cached
state, not real work. Also kept the `_TICK_TIMEOUT_SEC=120` defensive ceiling from the initial
(wrong-root-cause) investigation -- still worth having even though it wasn't the actual fix.

**Implication for everything documented before today**: this bug has presumably existed since
this dashboard architecture was built, meaning the live/paper trading systems have likely gone
dormant during any past stretch where nobody had a browser tab open on them -- with zero
record of when or for how long, since nothing logs an absence of activity. There is no way to
retroactively know how much of this project's "live since 2026-06-24" history was actually
being monitored/traded vs silently idle. Going forward this is fixed, but it's worth being
aware the historical live/paper track record has this unquantifiable gap in it.

**Also noted, separate and still unexplained**: port 8081 (live) was left orphaned by
`Stop-ScheduledTask -TaskName 'DashboardAppLive'` on 5 separate occasions during this session's
restarts, requiring an explicit process kill beyond the stop command every time. Initially
suspected this might be connected to the tick-loop bug (a stuck process not responding cleanly
to a stop signal) -- ruled out, since the tick-loop issue was never actually a hang, just a
scheduling gap. This orphaning pattern remains a real, reproducible, but still root-cause-
unidentified quirk of this specific deployment -- worth investigating separately if it keeps
recurring; the workaround (check `Get-NetTCPConnection -LocalPort 8081`, kill the specific PID
via `Invoke-CimMethod -MethodName Terminate` if still listening after `Stop-ScheduledTask`) is
now a routine, expected step of the restart procedure, not a one-off.

### 🔬 TESTED 2026-07-12: staged 3-to-11 sleeve ramp, ticker-breaker end-to-end test
Two more items from the same self-directed review round.

**1. Backtested the actual staged ramp for the first time** (not just the static 3-ticker or
11-ticker endpoints already tested). `core/sleeve.py`'s real rollout schedule -- `SLEEVE_STAGE_2A`
(SPY/QQQ/XLK) for 3 months, +`SLEEVE_STAGE_2B_ADD` (DIA/IWM) for months 3-6, full 11 onward --
is what the account will genuinely experience now that `PHASE2_NAV_USD=0` lets the clock run.
Built `research/sleeve_staged_ramp.py`: tested 117 historical 6-month windows (quarterly start
dates across the full sleeve history), comparing the staged ramp's combined R contribution
against jumping straight to the full 11-ticker book over the same window.

| | mean | median | std |
|---|---|---|---|
| staged (2A->5, real ramp) | +9.51% | +4.20% | 18.49pp |
| full-11 (counterfactual) | +17.39% | +8.14% | 39.74pp |
| difference (staged - full) | -7.87pp | -1.82pp | -- |

Staged beat full-11 in only 15% of windows -- **expected and mechanical**, not a red flag:
fewer active tickers for the first 3-6 months necessarily captures a smaller slice of the
sleeve's total edge, since fewer names can fire. This quantifies "how much smaller," not a
problem with the ramp design itself -- the ramp exists deliberately as a risk-management
precaution, independent of the equity gate removed earlier today.

**2. Built an end-to-end integration test for the sleeve's per-ticker circuit breaker**
(`_ticker_breaker_tripped`) -- same class of gap as the `DD_HALT_PCT` test added earlier this
session: the pure function had never been tested, and a bug in an analogous pure function (the
drawdown-calc materiality-floor bug) already came close to causing real harm once this session.
New `dashboard/tests/test_sleeve.py` (isolated temp-db pattern, matches `test_reconcile.py`):
seeds a ticker with 5 bad closed sleeve trades (1/5 win, tripped), one with too few trades to
judge (not tripped), one with good performance (not tripped), and one with bad CORE trades
under a different method (confirms sleeve/core breakers are correctly isolated per-method).
Then calls the REAL `place_sleeve_signals()` (not a reimplementation) with `entry_signal`
mocked, confirming the tripped ticker never reaches `entry_signal` while a clean ticker does,
and only the clean ticker's trade actually gets placed. All 7 checks pass.

### 📋 First-live-sleeve-fill verification checklist
The equity gate is open, the staged clock has started (2a=SPY/QQQ/XLK now, 2b at 3mo, 2c at
6mo), and the tick loop now runs unattended -- the sleeve's first REAL live fill could happen
any day. When it does, verify (mirroring the rigor already applied to the core book's first
post-Error-435-fix fills):
1. Confirm the broker order actually filled (`ib_mirror` row has a nonzero `perm_id`, not the
   "never acknowledged" `perm_id=0` signature already seen once this session on the ghost
   positions).
2. Confirm `reconcile.py`'s broker/local check shows a match (no ghost, no untracked position)
   on the next cycle after the fill.
3. Confirm the position sizing matches the risk-based formula for the actual VIX level at
   entry (`RISK_BASE=0.005` normally, `RISK_HIGH=0.01` if VIX>30) -- sanity-check against the
   account's real equity at that moment, not a stale figure.
4. Once it closes, confirm the realized R lands in a plausible range for a dip-buy exit
   (+3% TP, -5% SL, 5MA-touch, or 10-day time-cap) -- and that it's excluded from `paper.stats()`
   comparisons against the CORE book (different method, tracked separately).
5. Run `research/live_vs_backtest.py` once n>=5-10 real sleeve trades exist (lower bar than the
   core book's n>=30, since the sleeve trades far less often) to get an early read, fully aware
   it won't be "trustworthy" by this project's own n>=30 standard yet.

### 🔧 SELF-AUDIT 2026-07-11, part 4: sleeve clustering, core/sleeve correlation, exit/param DSR, commissions

**1. Sleeve cross-ticker clustering during panics -- real, substantial, but already reflected
in the reported numbers.** Direct code check: `place_sleeve_signals()` only checks
`_has_open(ticker)`/`_recent_close(ticker)` PER TICKER -- no cross-ticker cap at all. Built
`research/sleeve_clustering_check.py`: across 732 historical sleeve exits, clustering is real
and frequent -- up to **all 11 of 11 tickers resolving the same week** (2025-04-07), and
repeated 8-10-ticker clusters during 2008 GFC, 2011 debt-ceiling, 2015 China devaluation, 2020
COVID, and 2022. **Worst single week: -51.0% combined R, the week markets reopened after 9/11**
(at 10% sleeve weight, a -5.10pp single-week portfolio hit). This IS already captured in every
documented blended Calmar/maxDD figure -- `sleeve_blend.py`'s methodology sums real historical
per-ticker R-attribution on its actual resolution day, so the worst clustering event isn't
hidden. The real gap is different: this methodology gives each firing ticker its full
independent weight regardless of concurrency, while PRODUCTION's `PORTFOLIO_CAP` would throttle
new entries once the aggregate cap binds -- meaning REAL sleeve behavior during a heavy
clustering week would likely be MORE MUTED (smaller both up and down) than the backtest
assumes. Not a config change -- a scope note on how to read clustering-week figures.

**2. Core-vs-sleeve correlation -- measured directly for the first time, genuinely reassuring.**
Built `research/core_sleeve_correlation.py`. Correlation on days either series moved: **-0.026**.
On sleeve-exit days specifically (the more relevant comparison): **+0.011**. Both effectively
zero. On the (rare, n=17) days both moved, only 47% moved the same direction -- a coin flip.
**The "different risk driver" diversification story is empirically confirmed, not just assumed.**

**3. DSR correction for the exit-method and parameter searches -- also unmoved.** Applied the
same multiple-comparisons rigor already used for universe selection (49 trials, 100% DSR) to
the OTHER two search processes this session: the exit-method battery (18 configs tested:
fixed/STRUCT/breakeven/trailing variants/vol-trail x4/partial x3/exhaustion x3/time-decay x2)
and the parameter sweep (15 configs: SL_ATR_MULT/RR_DEFAULT/HORIZON_DAYS/OVEREXT/MIN_STRENGTH
+ the combined-interaction test). DSR stays at **100%** at n_trials=18, 15, the 33 combined,
and even 82 (adding the universe-selection 49 on top of both). The strategy's edge survives an
extremely comprehensive correction across every search process run this session.

**4. IBKR commission drag -- computed for the first time, smaller than assumed, likely
another overstatement in the original $64k reasoning.** Using IBKR Pro's well-established
tiered structure (~$0.0035/share, $0.35 minimum -- general knowledge, not independently
re-verified against IBKR's current live published schedule since fee schedules can change):
a sleeve SPY entry at current live equity (~$12.8k, 1.7 shares, ~$1,284 notional) costs ~0.055%
round-trip in commission -- HALF of the 10bp spread cost already modeled in every backtest. At
the $64k gate (8.5 shares), it drops to ~0.011% round-trip. **The "commission-sensitive fills"
part of the original phase-2 gating reasoning appears to be ANOTHER overstatement** -- joining
the already-found 2.4x-understated edge estimate. Both point the same direction: the original
$64k threshold was set more conservatively than its own stated reasoning, corrected, would
imply. Still not changed unilaterally -- a real-money parameter, flagged for a decision.

All new scripts from today (`sleeve_clustering_check.py`, `core_sleeve_correlation.py`) follow
the established convention. No config changes.

### 🔧 5-ROUND SELF-AUDIT 2026-07-12 (post-tick-loop-fix): exception safety, task resilience,
data-fetch load, regression testing, currency risk

**Round 1 -- audited for the same bug class, found a latent recurrence path.** Searched for
any other `ui.timer`/`@ui.page` pattern that could recreate the tick-loop dormancy bug --
clean, only the one (now client-scoped, cosmetic-only) UI-label timer remains. But found
`_tick_loop()` only caught `asyncio.TimeoutError` internally -- ANY other unhandled exception
from `_do_cheap()`/`_do_llm()` would silently kill the whole background task forever, recreating
the exact same invisible dormancy via a different trigger. Wrapped the loop body in a catch-all
so no single tick's failure can ever take down all future ticks.

**Round 2 -- scheduled-task resilience gap found (needs admin access to fix); live/backtest
bar-frequency consistency confirmed clean.** Both `DashboardApp` and `DashboardAppLive` have
`RestartCount=0` (no auto-restart on crash) and `ExecutionTimeLimit=PT72H` (force-killed after
3 days) with a `LogonTrigger` (only fires once per login) -- meaning a crash or the 3-day limit
leaves the dashboard down until the next login, potentially for a long time. **Could not apply
the fix directly (PermissionDenied -- requires elevated PowerShell)**; the commands to run:
```
$task = Get-ScheduledTask -TaskName 'DashboardAppLive'   # and 'DashboardApp'
$task.Settings.ExecutionTimeLimit = 'PT0S'
$task.Settings.RestartCount = 3
$task.Settings.RestartInterval = 'PT1M'
Set-ScheduledTask -TaskName 'DashboardAppLive' -Settings $task.Settings
```
Separately, verified live's actual signal-scoring data source (given how large the analogous
daily/weekly bug was in this session's own research scripts): `providers.get_history()`
explicitly fetches `interval="1wk"` for `BROKER=ib`, with an existing comment confirming this
was always a deliberate, aware design choice -- no bar-frequency mismatch in production.

**Round 3 -- yfinance load under continuous dual-instance polling, checked directly.** With
both dashboards now ticking unconditionally every ~30-60s (previously effectively gated by
whether a browser happened to be open), grepped the full log for any rate-limit fallout
("data source = none", rate-limit errors) -- zero instances found. Real theoretical exposure
(no retry/backoff exists in `providers.py` if this ever changes), but no evidence of actual
impact -- not worth a speculative fix.

**Round 4 -- gave the tick-loop fix an actual regression test.** The "never dies from one
call's failure" property had only been verified by manually watching logs. `app.py` can't be
imported in a test (`ui.run()` at module level blocks), so extracted the safety wrapper into
`core/resilient_loop.run_forever()` (a small, pure, generically-reusable function) with its own
test, `test_resilient_loop.py` (8 checks: survives every call failing, recovers after
intermittent failure, safe with no error handler given). `app.py`'s `_tick_loop()` now just
calls this instead of reimplementing the try/except inline.

**Round 5 -- HKD/USD peg risk, flagged early this session, never actually verified.** This
account converts HKD to USD to trade US ETFs; the peg's assumed stability had never been
checked against real data. Pulled 25 years of USD/HKD monthly closes (2001-2026): only 24/294
months touched slightly outside the official 7.75-7.85 band (monthly return std dev 0.246%,
worst breach 7.6431 -- a few basis points past the strong-side limit, consistent with normal
HKMA intervention during high-capital-flow periods, not a de-peg event). **Currency risk for
this account is confirmed genuinely negligible from real data, not just assumed.**

**Net result: one real, unfixed gap (scheduled-task settings, needs the user's admin access),
one code fix + its own regression test (tick-loop exception safety), and three clean
verifications (bar-frequency, yfinance load, currency peg).**

### 🔍 5-ROUND SELF-AUDIT 2026-07-12b (post-sleeve-gate-removal): exposure math, live sizing,
tick-loop idempotency, delisting risk, per-instance staged-rollout isolation

All 5 rounds this pass came back clean -- no code changes, no backtest changes, no CAGR/DD/
Calmar figures move. Documented because "checked and confirmed clean" is still useful signal,
not because anything was fixed.

**Round 1 -- does `PORTFOLIO_CAP` actually cap core+sleeve TOGETHER, or could the two systems
each independently deploy up to 100%, doubling real exposure?** Read `mirror_new()` directly:
`deployed = [_gpv_usd(ib)]` is seeded ONCE per cycle from the broker's real, live
`GrossPositionValue` (not a core-only or sleeve-only reconstruction) and threaded through BOTH
`_place_etf_bracket()` and `_place_sleeve_bracket()` in the same loop -- each new order sees the
TRUE aggregate room left, regardless of which system is asking. **Confirmed already correct,
not a gap.**

**Round 2 -- verified per-ticker share sizing across the full 22-ETF book at TODAY's real live
equity (~$12.8k / HKD 99,988), not just the SPY/QQQ sleeve check done previously.** Pulled real
ATR14 + price for all 22 tickers and ran the exact production formula (`size_shares` capped by
`ETF_POS_CAP=0.25`): **zero tickers round to 0 shares** -- every one sizes to at least 4 shares
(SPY/QQQ, the two priciest), and `ETF_POS_CAP` (not risk%) is the binding constraint on all 22,
as documented elsewhere in this doc. No zero-share silent-skip risk exists at the account's
current size.

**Round 3 -- now that the tick loop runs unconditionally every ~30s (vs. previously only when a
browser happened to be open), can it double-place orders or overload data sources?** Traced the
guard chain: `_has_open(key, mlabel)` + a `COOLDOWN_MIN` recent-close check block a duplicate
entry on the same ticker+method before a `Trade` row is even created; `mirror_new()`'s own
`done = _mirrored_ids()` blocks re-mirroring an already-mirrored trade; `_tick()`'s `_busy["flag"]`
additionally prevents overlapping runs. Separately, `_tick()` internally throttles the EXPENSIVE
work (`_do_cheap`/`_do_llm`) to `SETTINGS["cheap_min"]`/`["llm_min"]` minutes regardless of how
often the outer 30s timer fires -- so today's fix changed whether the loop runs at all, not how
often the real scoring/scanning work happens. **No duplicate-order or load-increase risk found.**

**Round 4 -- what happens if a ticker in the 22-ETF/11-sleeve universe is delisted or its data
feed goes dark?** `providers.get_history()` fails CLOSED (`return None, "none"` on any fetch
failure or a too-short series) -- no crash, just no new signal that cycle, consistent with the
Round-3-of-the-prior-audit finding that no retry/backoff exists but no evidence of impact either.
**Genuinely low-probability given the current universe** (SPY/GLD/TLT-class large, well-
established funds, not speculative small-caps) but still an unquantified tail case worth naming
honestly: if a HELD position's feed went permanently dark, there's no explicit alert for that
specific scenario (as opposed to a transient fetch failure, which just self-heals next cycle).
Not worth a speculative fix -- flagged, not actioned.

**Round 5 -- does each instance's staged sleeve rollout (2A now / 2B +3mo / 2C +6mo) track its
OWN activation time, or could live have silently inherited paper's already-progressed clock?**
Checked `sleeve_first_active_ts` directly in both DBs -- correctly independent (`store.cache_get`
routes through each instance's own `dashboard.db`/`dashboard_live.db`), confirming they were
never at risk of cross-contamination. **Paper activated 2026-07-09 14:34 UTC** (2B unlocks
~2026-10-09, 2C ~2027-01-09). **Live activated 2026-07-11 17:11 UTC / 2026-07-12 HKT** (2B
unlocks ~2026-10-11, 2C ~2027-01-11) -- 2 days after paper, exactly as expected given the gate
was removed on live a couple of days later. Both currently sit in stage 2A (SPY/QQQ/XLK only).

**Net result: 5 clean verifications, 0 code changes, 0 backtest changes.** The system's
exposure math, sizing, idempotency, and staged-rollout isolation all check out against real
data/code as claimed elsewhere in this doc, rather than being merely asserted.

### 🐞🐞 FIXED 2026-07-10: EVERY live order in `ib_exec.py` was silently vulnerable to Error 435/10349
User reported the account's Leveraged Forex permission had been approved but `keep-cash-usd`
still wasn't converting HKD→USD. Verified directly against live with an error-event listener
(the existing code only checks order status in the instant right after `placeOrder()` -- a
documented "best-effort" check that can't see a delayed rejection) and found the REAL cause had
nothing to do with the forex permission:

1. **Error 435 "You must specify an account."** This login manages TWO accounts (the real
   `U12991898` + the unrelated empty `U20738951` — same pair behind the 2026-07-10 HKD-0 and
   position-reconciliation bugs above). IBKR requires every `Order.account` to be set explicitly
   once a login has >1 managed account; `ib_exec.py` never set it, ANYWHERE — not just
   `keep_cash_usd()`. Grepped: 9 order-placement call sites (3 `bracketOrder()` — core/ETF/sleeve
   entries — plus `manual_close_sleeve`, `_roll_position` x2 legs, `keep_cash_usd`, `sweep_cash`,
   `prepare_withdrawal`), all missing `.account`. Fixed at all 9, using `ib_client.account_id()`
   fetched ONCE per outer function call (never from inside an `ib_client.call()`/`_run()`
   closure — calling it from there would self-deadlock the loop thread, same class of bug as the
   `reqAllOpenOrders()` fix above) and threaded down as a parameter.
2. **Error 10349 "Order TIF was set to DAY based on order preset"**, immediate cancel. A SEPARATE
   bug, `keep_cash_usd()`-specific: every other order type in this file sets `o.tif = "GTC"`;
   the FX order never did. Fixed the same way.

**Verified against live, not just compiled:** connected directly with an `errorEvent` listener,
confirmed a test FX order hit Error 435 before the fix, then Error 10349 after fixing #1 alone,
then reached `Submitted` after fixing both (cancelled the test order manually). Redeployed;
the next real `keep_cash_usd()` cycle went through for the first time ever — live ledger now
shows a genuine `USD 100.00` balance (was always $0) and HKD dropped 10,040 → 9,240.27,
confirming an actual fill, not just a status-log change.

**Root cause note:** this bug predates this session by a long time (every `keep-cash-usd` log
line ever written said "not yet confirmed filled" and never once said Filled) — it was never a
forex-permission problem at all; the account may have had FX enabled long before the user's
recent permission request, but no order ever got far enough to prove or disprove that.

### 🐞 FIXED 2026-07-10: "You are up/down" % was nonsense on a small, newly-funded live account
User saw "HKD -31 (-78.15%)" on live and asked if the % was wrong. It was. `app.py`'s
`portfolio_panel()`: `base0 = hist[0][1]` (the very first `equity_history` point ever recorded)
is used as the % denominator. Live's tracking started 2026-07-03 at a tiny pre-funding
NetLiquidation of **HKD 40**, before the real HKD 10,000 deposit landed 2026-07-08. The dollar
P&L (`total_pl = nl - base0 - net_flows`) correctly nets deposits OUT of the numerator, but
`pct = total_pl / base0 * 100` still divided by the ORIGINAL HKD 40 snapshot alone -- so a
genuine, small -HKD 31 cost (FX-rate marking on the newly-converted USD cash, see the
Error-435/10349 fix above) showed as -78% instead of the true ~-0.3%. Fixed: the denominator
must be the capital base ACTUALLY deployed at each point, i.e. `base0 + net_flows` (deposits
net INTO the base, mirroring how the numerator already nets them OUT of the delta) -- same
principle as a Modified-Dietz-style return, not a raw fraction of a stale opening snapshot.
Verified live: now shows `HKD -31 (-0.31%)`, matching the hand-computed correct value exactly.
Paper unaffected (its `base0` was never near-zero, so the bug was dormant there) -- confirmed
via direct comparison after redeploy (paper still shows a sane `+0.10%`).

Root cause note: NOT a data-corruption bug like the earlier zero-spike incidents (this
session's `heal_series()` self-heal wouldn't have caught it anyway -- it can only compare a
point against a PRIOR good value, and there's no prior point before the very first one in the
series). This is a genuinely different bug class: a percentage-of-near-zero-baseline
instability, structurally similar to what's already documented for the `equity_history`
guards, but in the % *formula* itself rather than in the underlying data.

### ⭐ UI 2026-07-10: added a visible "HKD cash" stat between Cash (buffer) and USD cash
`hkd_c` (the HKD residual `keep_cash_usd` targets down to ~500) was already being READ every
cycle but only ever surfaced buried in the USD cash tooltip text ("HKD residual: N"), not as
its own stat. Promoted it to a proper `_stat("HKD cash", ...)` card in `portfolio_panel()`,
positioned right before USD cash (they read as a currency pair) -- gated the same way USD cash
already is (`fx.get("enabled")`), so it only appears when `CASH_USD=1`. Verified on both
dashboards post-redeploy: live shows Cash (buffer) HKD 10,009 -> HKD cash HKD 491 -> USD cash,
paper shows the same three-stat ordering.

**FOLLOW-UP (same day):** user asked to reorganise the (now-wrapping) Cash & financing row and
add Buying Power (購買力) as a reference figure. Split into two intentional `ui.row()`s instead
of one long flex-wrap: **line 1 = cash composition** (Cash buffer, Cash in SGOV, HKD cash, USD
cash — what currency/form the cash is in), **line 2 = financing capacity/yield** (new "Buying
power (購買力)" from `acct["BuyingPower"]` — already being READ via `ACCOUNT_SUMMARY_TAGS` but
never displayed, Interest accrued, Projected interest). Buying power's tooltip explains the
margin-vs-cash-account read (if it stays ~1x NetLiq, margin capacity likely isn't active).

Verified post-redeploy against a genuine, large intervening change: the user deposited more
funds mid-session -- live NetLiquidation jumped from ~HKD 10,040 to **HKD 99,993.94** (~100K
HKD, matching the original go-live plan's target), USD cash from $1,214 to $12,693, with
`GrossPositionValue` still HKD 0 (cash only, no new positions -- confirmed via a direct API
check before trusting the jump, not just assumed benign). This ALSO settles the earlier
"is live actually margin-enabled" open question from earlier this session: `BuyingPower` is
now HKD 666,626 (~6.7x NetLiq) -- clear real margin capacity, confirming the earlier ~1.0x
reading was an IBKR quirk on a near-empty account, not evidence of a cash-only account.

### ⭐⭐ FIXED 2026-07-10: deposit-detection only caught LARGE jumps -- a real, forward-looking gap
User asked to verify the deposit-vs-P&L detection after the HKD 99,984 deposit (correctly
excluded -- confirmed via the `cash_flows` log). But researched TWS API options together
(confirmed: no native "deposit happened" event exists, `reqAccountSummary`/`reqAccountUpdates`
are polling-only, matching what `reconcile.py` already found for positions) and found a real
forward-looking gap while verifying: the existing confirm-then-accept guard only flags a jump
outside a fixed **0.5x-2.0x** band. That correctly caught this ~10x deposit, but would SILENTLY
MISS a routine ~30% monthly contribution (the user's actual funding plan is ~HKD 30K/mo) once
the account is big enough that a contribution is a smaller fraction of NAV -- letting a future
real deposit get counted as fake trading P&L, exactly the mistake being asked about, just not
manifested yet.

**Fix (`service.py`):** new pure `is_equity_jump_implausible(new_val, prev_val, gpv)` --
when there are NO open positions (`GrossPositionValue <= ~0`), nothing legitimate should move
NetLiquidation beyond tiny interest/FX noise, so ANY change past a small noise band
(`max(HKD 100, 0.5% of prev)`) is flagged regardless of size -- catching deposits/withdrawals
of any magnitude while flat. With open positions, mark-to-market P&L can legitimately swing
equity a lot, so it falls back to the original wide ratio band (a tight absolute band would
misfire constantly on ordinary position price moves). Replaces the inline 0.5x-2.0x check in
the `equity_history` block. 10 new unit tests in `test_service.py` (incl. the key regression:
a 30% flat-account jump is now correctly flagged, where it wasn't before) -- all pass, along
with the full existing suite. Verified live post-redeploy: no false positives on ordinary
cycles (account sat flat at ~HKD 99,993-99,997 across several refreshes, nothing flagged).

### 🎨 UI 2026-07-10: cleaned up redundant zero stats when fully in cash
Same conversation: "Unrealized (open)" and "Invested" both showed a redundant "HKD 0" whenever
the account had no positions (now common, given the account is currently 100% cash). Gated
both behind `GrossPositionValue > 0`; otherwise shows a plain "Fully in cash — no open
positions" line. Verified: live (flat) shows the cash-only message, paper (has real open
positions) still shows both stats normally -- no regression.

### 🎨 UI 2026-07-10: grouped Cash (buffer) with Interest accrued + Projected interest
User asked why paper's Projected interest was -HKD 1,266 while live's was +HKD 261 -- traced
to Cash (buffer) sign (paper -HKD 276,204, margin debit @ ~5.5%; live +HKD 99,996, credit @
~3.1%). User then asked whether Projected interest should sit on the same line as Cash
(buffer) since they're causally linked. Agreed and reorganised: **line 1 = Cash (buffer) +
Interest accrued + Projected interest (1mo)** (the cash position and what it's costing/
earning, grouped so the causality is visible at a glance), **line 2 = HKD cash + USD cash +
Buying power** (currency/form breakdown + financing capacity). Verified on both dashboards
post-redeploy: correct grouping on each.

### ⭐⭐⭐ BUILT 2026-07-11: PORTFOLIO_CAP -- aggregate gross-exposure cap, live in ib_exec.py
User asked whether the margin-debit interest cost is from keeping `ETF_POS_CAP=0.25`, or from
having 7 positions open concurrently. Verified with real numbers (paper's `ib_mirror` qty x
current price): 3 of the 7 positions (CWB/AMLP/ASHR) are genuinely AT the 25% cap, the other 4
sit below it based on their own ATR-derived vol -- summing to the observed 127% gross exposure
exactly. **It's both, interacting**: the per-position cap sets the ceiling any ONE position can
reach; the concurrent count determines how many of those ceilings stack past 100%. Neither alone
would cause it (3 positions @25% = 75%, fine; 7 positions @10% cap = 70%, also fine).

**Researched a hybrid fix**: keep `ETF_POS_CAP` generous (full-size bets when few positions are
open) but ALSO cap the AGGREGATE deployed notional, so new entries scale down only once several
positions are already stacked -- the actual scenario causing the drag, not the per-position cap
in isolation. Added `PORTFOLIO_CAP` to `research/backtest.py` (mirrors `POS_CAP`'s "scale down,
never skip outright" philosophy) and swept it:

| config | Full CAGR | Full maxDD | Full ratio | OOS CAGR | OOS maxDD | OOS ratio |
|---|---|---|---|---|---|---|
| current adopted: pos=0.25, no ceiling | +6.08% | -12.74% | 0.477 | +12.84% | -10.04% | 1.278 |
| **hybrid: pos=0.25, portfolio<=100%** | **+5.83%** | **-6.83%** | **0.853** | **+11.42%** | **-4.96%** | **2.302** |
| pure cap: 0.15, no ceiling | +4.99% | -6.94% | 0.719 | +9.25% | -6.36% | 1.453 |
| pure cap: 0.10, no ceiling | +4.15% | -4.56% | 0.909 | +7.01% | -4.02% | 1.744 |

**The hybrid strictly dominates every pure-cap alternative tested this whole line of research
(0.10 through 0.30)** -- MORE CAGR than either 0.15 or 0.10 alone, AND better/comparable maxDD.
Walk-forward (`walk_forward.py`, now accepts a 2nd arg for portfolio-cap) confirms this holds up
across all 6 rolling historical windows, not just in aggregate: same single negative window
(2001-2006) as every other config, best mean per-window ratio (0.985) of anything tested.

**Implemented in `ib_exec.py`** (both `_place_etf_bracket` and `_place_sleeve_bracket`):
- New `_gpv_usd(ib)` reads the broker's REAL `GrossPositionValue` (not a local reconstruction
  from entry prices, which can drift from reality -- see the ghost-position incident) --
  verified live post-deploy: paper `_gpv_usd()=$165,164` vs `_equity_usd()=$129,760` (~127.3%,
  matches the known exposure exactly).
- New pure `cap_qty_to_portfolio_room(qty, price, equity_usd, portfolio_cap, deployed_usd)` --
  extracted so it's unit-testable without any IB connection (`dashboard/tests/test_ib_exec.py`,
  10 checks, all pass, incl. the boundary/over-cap/disabled-flag edge cases).
- `mirror_new()` seeds a running `deployed = [_gpv_usd(ib)]` once per cycle and threads it
  through both placement functions, incrementing it as each position is actually placed --
  so multiple signals firing in the SAME cycle can't collectively overshoot the cap (each only
  sees room left after earlier ones in the same batch, mirroring the backtest's own
  chronological book-walk). The increment happens only AFTER every skip-check (spread guard,
  qty<1, etc.) -- catching a real bug found during implementation: an earlier draft incremented
  `deployed` before the sleeve's spread-guard check, which would have over-counted skipped
  entries.
- New env var `PORTFOLIO_CAP` (default `"1.0"` = never exceed 100% gross exposure; `0` disables,
  matching `ETF_POS_CAP`'s own convention). Not yet set in either launch script -- relies on this
  same code-default pattern as `ETF_POS_CAP` itself.

Full test suite (5 files, `test_contracts`/`test_service`/`test_ib_client`/`test_reconcile`/
`test_ib_exec`) passes clean post-change. Deployed to both dashboards; confirmed no errors/
tracebacks in the logs after restart.

**Still pending:** no NEW real order has been placed since this deployed (the 7 stale positions
already have `ib_mirror` rows so `mirror_new()` skips them regardless -- see the earlier
reconciliation entry), so this hasn't yet been observed capping a real live signal end-to-end.
Worth watching the first real new signal after this to confirm the cap engages as intended.

### 🐞 FIXED 2026-07-09: "Projected interest (1mo)" ignored the margin-debit rate on negative cash
User asked whether a paper-account "Cash (buffer) HKD -20,547 / USD cash $-2,684 / Projected
interest HKD -54" reading was correct. The negative cash itself is NOT a bug: `GrossPositionValue`
(HKD 1,034,943) slightly exceeds `NetLiquidation` (HKD 1,014,468) because 6 concurrent ETF
positions are each risk-sized independently and their combined notional landed just over 100% of
NAV -- normal for `ETF_POS_CAP=0.25` with several positions open, and `ExcessLiquidity` (HKD
863,364) is nowhere near a margin call. But the **projected interest WAS wrong**: `app.py`'s
"Projected interest (1mo)" multiplied the cash buffer by `ib_rate` (the ~3.2% CREDIT rate paid on
positive cash) regardless of sign -- applying that same low rate to a NEGATIVE buffer, when the
"USD cash" tooltip right next to it already says negative cash is a margin DEBIT charged ~5-6%.
Understated the true monthly cost by ~74% (-54 shown vs -94 actual at a representative 5.5% debit
rate). Fixed: `cash_mo` now picks `ib_rate` when cash>=0 or a new `MARGIN_DEBIT_RATE=5.5` constant
when cash<0 (a fixed approximation -- IBKR's API has no field for this account's actual per-user
margin rate); tooltip now labels which rate applied, and the stat's color flips red when the
projection is negative (previously always green regardless of sign). Verified: recomputed by hand
(-94.17 at 5.5% vs -54.33 at 3.2% for the same -20,546.69 HKD balance) and confirmed the rendered
page now shows "USD cash HKD -94 @ 5.5% (margin debit rate, approx)" on both dashboards after
restart.

### ⭐ UX FIX 2026-07-09: Portfolio panel didn't answer "am I up or down" clearly
Follow-up to the projected-interest bug above -- the user asked directly "am I profiting or
losing?" after seeing the raw stat grid (Cash buffer, USD cash, Interest accrued all sitting as
plain same-size cards right next to Total P&L). Root problem: every figure on the panel looked
the same (label + number, same size, same color logic), so a NEGATIVE cash/USD-cash reading (which
is just margin financing for several concurrent positions, not a loss) was easy to misread as
losing money, while the actual answer (Total P&L) was one card among nine with no visual priority.
`portfolio_panel()` (`app.py`) restructured into two tiers:
1. **Headline**: Total P&L now lives alone in its own colored card (green/red bg, trending
   up/down icon, "You are up/down", large 3xl number) directly under the Portfolio title --
   unmissable, answers the actual question first.
2. **Supporting rows**: Total value / Unrealized / Invested directly below (things that explain
   the headline number), then a `text-grey-6 italic` caption -- "Cash & financing — how positions
   are funded, NOT profit or loss (see the P&L card above for that)" -- before Cash (buffer),
   Cash in SGOV, USD cash, Interest accrued, Projected interest. Tooltips on Cash (buffer) and USD
   cash now explicitly say "NOT a loss" / "NOT profit/loss" and explain the margin-financing
   mechanic in plain terms.
Verified both accounts' actual numbers by hand from `equity_history`/`cash_flows` before touching
any UI: **PAPER is +HKD 1,842 (+0.18%)** since tracking began; **LIVE is +HKD 0 (0.00%)** -- its
HKD 10,040 balance is entirely the deposit, no trades closed yet. Confirmed the new headline
("You are up") renders correctly on paper after restart. Chrome extension was unreachable to
screenshot the visual layout, so this is text-confirmed, not visually confirmed -- worth a look
next session.

### ⭐⭐ ETF UNIVERSE: 17 → 21 (2026-07-08) — batch-3/4/5/6 screens, CWB+VNQI+AMLP+HYD adopted
Also fixed 2026-07-08: THREE real bugs found in this stretch of work.

**(1) A HKD 10,000 deposit into the live account showed as fake trading P&L, AND permanently
froze the equity chart.** `service.py`'s equity_history sanity guard (added 2026-07-02 to reject
a corrupted one-off spike) was a one-shot reject: since every future reading after a REAL deposit
is also >2x the stale pre-deposit baseline, the guard rejected every future point forever, and
"Total P&L" (`nl - base0`) counted the deposit itself as if it were profit. Fixed with a
confirm-then-accept state machine (`equity_pending_jump` cache key): a single implausible jump is
held pending, not recorded or discarded; if the NEXT reading confirms the same new level, it's a
real, sustained change (not a one-off glitch) -- record it AND log it to a new `cash_flows` cache
key so it can be netted out. `portfolio_panel()`'s Total P&L and the equity/drawdown charts now
subtract net cash flows since tracking began (`_deposit_adjusted()` helper) so a deposit is
invisible to P&L instead of masquerading as gains, and never resets the drawdown "peak" to hide a
real ongoing loss. Added a "View: P&L (ex-deposits) / Account value" toggle (`chart_view` setting)
+ dotted deposit markers on the raw-value chart. Verified end-to-end on the actual HKD 10,000
deposit: cash_flows correctly logged `[ts, 10000.0, 'HKD']`, equity_history un-stuck, Total P&L
corrected from "HKD 10,000 (+25000%)" to "HKD 0 (+0.00%)".

**(2)** `keep_cash_usd()` retried a
failing FX order every single cycle forever (224+ live attempts, 0 fills) because `placeOrder()`
is fire-and-forget and a rejection never surfaces as a Python exception -- added a 20min cooldown
+ persistent attempts counter + a dashboard warning badge once repeated attempts produce no real
USD balance (root cause: account likely lacks Forex trading permission -- IBKR categorizes ANY
API-placed IDEALPRO order under "Leveraged Forex", not the separately-held "Currency Conversion"
permission; user's call whether to enable it or drop the automation).

**CONFIRMED 2026-07-09** via an actual IBKR permissions screenshot: **Currency Conversion is
enabled, Leveraged Forex is NOT** -- exactly the predicted gap. User is working through enabling
it on IBKR's side. **Cooldown tightened 20min -> 5min** (`ib_exec.py`) at the user's request, for
a faster confirmation once the permission clears, while still comfortably above the ~70-90s
refresh cadence. As of this check the account is still stuck (44+ attempts, `usd_cash=$0`, full
HKD 10,040 balance unconverted) -- expected until the IBKR permission request is actually
approved, not a regression. Re-check once the user confirms approval.

**(3)** `sweep_cash()` only
gated on a $1,500 rebalance-DELTA, not account size -- added `CASH_SWEEP_MIN_NAV_USD=75_000`
matching the ADOPTED PLAN's own "skip SGOV sweep until ~$75-100k NAV" decision, which the delta
check was never actually enforcing (would start sweeping ~$2,500 NAV, nowhere near $75k).

**Batch-3 screen** (`--etf-screen3`, targeting asset classes with ZERO existing representation,
since batch-2's lesson was that narrower slices of a held class just correlate with it):
| Ticker | class | n | expR | verdict |
|---|---|---|---|---|
| CWB | convertible | 67 | +0.404 | **ADOPT** |
| IGF | infra | 71 | +0.067 | reject (~zero edge) |
| EMLC | em_local_debt | 19 | −0.261 | reject (negative, worst market in set) |
| BKLN | bank_loan | 11 | +0.860 | defer (best number, but n=11 in 33y -- not enough to trust) |
| FM | frontier_eq | 14 | +0.648 | defer (same issue, n=14) |

**Batch-4 screen** (`--etf-screen4`, testing whether "international version of a held class"
generalizes beyond equity, since EFA/EEM succeeding alongside domestic SPY/QQQ was the model):
| Ticker | class | n | expR | verdict |
|---|---|---|---|---|
| VNQI | intl_reit | 36 | +0.235 | **ADOPT** |
| BWX | intl_rate | 25 | −0.139 | reject (negative) |
| PICB | intl_credit | 28 | −0.074 | reject (negative) |
| WOOD | timber | 36 | −0.076 | reject (negative) |
**Finding: the geography-diversification pattern does NOT generalize past equity/hybrid-credit** --
3 of 4 batch-4 candidates went negative. Don't assume "ex-US version of X" is automatically worth
testing again without a specific reason.

**Isolation tests** (same methodology as the 2026-06-23 EMB/PFF test -- add ONE candidate to the
current base, compare OOS CAGR/DD, not just the raw per-market number):
| | OOS CAGR | OOS maxDD | OOS expR | OOS n |
|---|---|---|---|---|
| 17-base | +10.3% | −6.0% | +0.380 | 549 |
| 17 + CWB | +11.3% | −6.1% | +0.389 | 593 |
| 18 (17+CWB) + VNQI | +11.8% | −6.2% | +0.387 | 621 |
CWB: **+1.0pp** OOS CAGR for flat DD -- larger individual contribution than EMB or PFF showed
alone. VNQI: **+0.5pp** for flat DD -- same tier as PFF's original contribution. Both promoted to
`ETF_CANDIDATES` in `instruments.py` and added to `WEEKLY_TREND_CLASSES` in `paper.py`. Verified
end-to-end: plain `python -m dashboard.research.backtest --longweekly` (no screen flag -- the
actual production `active_universe()` path) reproduces the 18+VNQI isolation numbers exactly
(OOS CAGR +11.8%/DD -6.2%/expR +0.387/n=621), confirming the promotion took effect correctly.
Confidence model needs NO rebuild for new instruments -- `confidence_model.py` buckets purely by
`(strength, regime)`, never per-instrument, so it already covers CWB/VNQI like any other symbol.

**Infra added for future batches:** `ETF_SCREEN_BATCH_3` through `_9` in `instruments.py` +
`--etf-screen3` through `--etf-screen9` in `backtest.py`, mirroring the
existing batch-1/2 pattern exactly (including the `if not args.classes: WEEKLY_TREND_CLASSES = set()`
guard that batch-2 was missing, needed to isolate a single candidate rather than always
trading the whole batch). Deferred/rejected batch members stay defined in their batch lists
(not deleted) for future re-screening, same reversibility principle as EMB.

**Batch-5 screen** (`--etf-screen5`, targeting metals with DIFFERENT demand drivers than held
GLD/SLV/CPER, plus a real-asset equity structure not yet tried):
| Ticker | class | n | expR | verdict |
|---|---|---|---|---|
| AMLP | mlp | 41 | +0.502 | **ADOPT** |
| PALL | metal2 | 39 | +0.216 | reject (see isolation test below -- positive raw expR is misleading here) |
| URA | uranium | 26 | +0.110 | reject (weaker than the already-rejected IGF) |
| PPLT | metal2 | 11 | −0.089 | reject (negative + tiny n) |

**Isolation tests, sequential from the 19-base (17+CWB+VNQI):**
| | OOS CAGR | OOS maxDD | Ratio |
|---|---|---|---|
| 19-base | +11.8% | −6.2% | 1.90 |
| 19-base + AMLP | +12.7% | −6.6% | 1.92 (flat/better) |
| 19-base + metal2 (PPLT+PALL) | +12.1% | **−7.4%** | **1.64 (worse)** |

**AMLP: +0.9pp OOS CAGR for -0.4pp extra DD, ratio flat/better -- adopted.** **PALL/PPLT: REJECTED
despite a positive raw per-market expR** -- the portfolio-level isolation test shows the DD cost
(-1.2pp vs 19-base) far outweighs the CAGR gain (+0.3pp), most likely because precious/industrial
metals draw down alongside the existing GLD/SLV/CPER holdings rather than diversifying away from
them. **This is the whole reason the isolation test exists**: the raw per-market screen alone
would have said "maybe" on PALL; only checking the actual portfolio-level CAGR/DD delta caught
that it hurts risk-adjusted performance. Don't skip this step for a future candidate just because
its standalone number looks decent.

Promoted `AMLP` to `ETF_CANDIDATES` (`instruments.py`) and added `"mlp"` to `WEEKLY_TREND_CLASSES`
(`paper.py`). Verified end-to-end: plain `python -m dashboard.research.backtest --longweekly`
(production `active_universe()` path, no screen flag) reproduces the isolation numbers exactly
(OOS CAGR +12.7%/DD -6.6%/expR +0.393/n=655) -- 20-ETF universe confirmed working before batch 6.

**Batch-6 screen** (`--etf-screen6`, targeting municipal HIGH-YIELD -- a different credit tier +
tax-exempt investor base than both HYG and the rejected IG-muni MUB -- a BDC income fund, plus a
confirmatory test of whether GDX's "mining equity carries broad market beta" rejection also holds
for copper miners):
| Ticker | class | n | expR | verdict |
|---|---|---|---|---|
| HYD | muni_hy | 21 | +0.394 | **ADOPT** |
| BIZD | bdc | 39 | +0.176 | reject (isolation: DD cost outweighs gain, same pattern as PALL) |
| COPX | miner2 | 29 | +0.182 | reject (mining-equity-beta drag confirmed for copper too, milder than gold) |

**Isolation tests from the 20-base:**
| | OOS CAGR | OOS maxDD | Ratio |
|---|---|---|---|
| 20-base | +12.7% | −6.6% | 1.92 |
| **20-base + HYD** | **+13.3%** | **−6.6%** | **2.02 (best ratio improvement this session)** |
| 20-base + BIZD | +13.0% | −7.5% | 1.73 (worse) |
| 20-base + COPX | +13.0% | −7.0% | 1.86 (worse) |

**HYD: +0.6pp OOS CAGR for ZERO extra drawdown -- adopted.** BIZD and COPX both show the same
"decent raw expR, DD cost outweighs it once in the actual portfolio" pattern PALL/PPLT showed in
batch 5 -- rejected despite positive standalone numbers. COPX's result is informative on its own:
mining-equity beta drag applies to copper too (not just gold/GDX), just less severely.

Promoted `HYD` to `ETF_CANDIDATES` and added `"muni_hy"` to `WEEKLY_TREND_CLASSES`. Verified
end-to-end: production `active_universe()` path reproduces the isolation numbers exactly (OOS
CAGR +13.3%/DD -6.6%/expR +0.401/n=668) -- **current live universe = 21 ETFs**
(17 + CWB + VNQI + AMLP + HYD; EMB still excluded via WEEKLY_TREND_CLASSES, 22 defined total).

**FEATURE 2026-07-08: signal-frequency vs fill-frequency stat on Active Trades.** The backtest's
~38 trades/year is a SIGNAL frequency (across the whole 21-ETF universe) -- not a promise of how
often trades actually FILL on a small account. A cheap/low-ATR instrument (HYD, CPER) sizes 1
share for a few dollars; an expensive/high-ATR one (SPY needs ~$209, QQQ ~$503 at 1% risk) can eat
most of a small account's capital in one position. Added `_fundable_count()` (`app.py`) -- checks
every active-universe instrument's current ATR against `contracts.min_equity_for_1_share()` and
counts how many could size >=1 share RIGHT NOW at current equity -- displayed as "Signal freq
(backtest): ~38/yr (~0.7/wk) · Fundable now: N/21 ETFs at current equity" under the Active Trades
header. `BACKTEST_SIGNAL_FREQ_YR`/`_WK` are static reference constants (re-measure if the universe
changes again -- same pattern as the drawdown chart's hardcoded "-10.5%" backtest-DD reference
line). Verified on paper (21/21 fundable, full paper balance). Live briefly showed only the
frequency reference with the fundable count hidden while the gateway was disconnected (correct
graceful-degradation, `equity_usd()` returns None when disconnected, same as the existing
`_pending_reason()` fallback) -- confirmed later the same day, once reconnected: 18/21 ETFs
fundable at the live account's actual (small) balance, exactly the kind of real, informative
number this feature exists to surface.

**Batch-7 screen** (`--etf-screen7`, targeting genuinely new STRATEGY structures rather than
asset classes: merger arbitrage/covered-call income, plus one more confirmatory real-asset
thematic-equity test):
| Ticker | class | n | expR | verdict |
|---|---|---|---|---|
| QYLD | covered_call | 39 | +0.418 | reject (isolation: DD cost outweighs gain) |
| PHO | thematic_eq | 76 | +0.157 | reject (weak edge, same pattern as infra/timber) |
| MNA | merger_arb | 28 | −0.009 | reject (flat -- market-neutral strategies rarely throw trend signals) |

**Isolation test:**
| | OOS CAGR | OOS maxDD | Ratio |
|---|---|---|---|
| 21-base | +13.3% | −6.6% | 2.02 |
| 21-base + QYLD | +14.1% | **−7.5%** | **1.88 (worse)** |

**ZERO adoptions from batch 7** -- a legitimate, expected outcome, not a failure of the process.
QYLD's rejection makes sense in hindsight: covered-call's capped-upside structure (from selling
calls against the position) is fundamentally at odds with this strategy's core edge source
("let winners run" on the strongest trends) -- it can't fully participate in the big rallies that
drive returns here, but still shares the downside. This is now the FOURTH candidate this session
(after PALL/PPLT, BIZD, COPX) with a decent-looking raw per-market expR that failed once actually
tested in the portfolio -- a strong empirical case for why the isolation-test step is mandatory,
not optional, for any future candidate regardless of how good its standalone number looks.
**Live universe unchanged at 21 ETFs.**

**Batch-8 screen** (`--etf-screen8`, mortgage REITs vs the already-held equity REITs, natural
gas vs the already-rejected oil/broad commodity, and momentum-factor equity as the one
factor-tilt idea worth actually testing):
| Ticker | class | n | expR | verdict |
|---|---|---|---|---|
| MTUM | factor_eq | 52 | +0.289 | borderline -- see isolation test |
| REM | mortgage_reit | 47 | −0.021 | reject (flat/negative) |
| UNG | energy2 | 5 | +0.819 | reject (only 5 signals in 33y -- too thin, matches BKLN/FM) |

**MTUM isolation:**
| | OOS CAGR | OOS maxDD | Ratio |
|---|---|---|---|
| 21-base | +13.3% | −6.6% | 2.02 |
| 21-base + MTUM | +14.0% | −7.0% | 2.00 (~1% relative decline -- far milder than PALL/BIZD/COPX/QYLD) |

**MTUM was a genuine borderline case, not a clean pass or fail.** Asked the user directly rather
than deciding unilaterally: **left out**, keeping the flat-or-better bar strict and mechanical
rather than making exceptions for close calls. If a future batch needs a tiebreaker candidate,
MTUM is the natural first thing to re-test alongside it. REM's rejection is informative on its
own: the "genuinely different risk driver" reasoning (rate/spread-sensitive vs property-value-
sensitive) was sound, but a different risk driver doesn't automatically mean a USABLE weekly-
trend signal -- worth remembering for future candidate selection. **Live universe unchanged at
21 ETFs; zero adoptions from batch 8 (second batch in a row with none).**

**Batch-9 screen** (`--etf-screen9`, honestly scraping toward the bottom of well-motivated
ideas: lithium/battery metals as a genuinely different metal demand driver, low-vol factor
equity as the scientific complement to MTUM's borderline batch-8 result):
| Ticker | class | n | expR | verdict |
|---|---|---|---|---|
| LIT | metal3 | 23 | +0.547 | reject (isolation) |
| USMV | factor_eq2 | 60 | +0.216 | reject (isolation) |

**Isolation tests:**
| | OOS CAGR | OOS maxDD | Ratio |
|---|---|---|---|
| 21-base | +13.3% | −6.6% | 2.02 |
| + LIT | +13.9% | −7.3% | 1.90 (~6% relative decline) |
| + USMV | +13.8% | −7.4% | 1.86 (~8% relative decline) |

Both clearly worse than MTUM's 1% borderline decline -- no judgment call needed this time,
clean rejects under the flat-or-better rule. USMV underperforming MTUM matches the prediction
(low-vol stocks trend less dramatically, giving a trend-follower less to work with).

**THIRD batch in a row (7/8/9) with zero adoptions.** This is now a strong, consistent signal
that the pool of genuinely well-motivated, untested candidates is exhausted -- not bad luck on
any single batch. Recommend pausing the ETF-screening thread here; **live universe holds at
21 ETFs.** Future re-screening candidates already on file for later (deferred, not rejected, if
more live history accumulates): BKLN (bank_loan, n=11), FM (frontier_eq, n=14). Everything else
tested across batches 1-9 (~27 tickers) was a clean reject or (MTUM) a user judgment call.

### 🐞 FIXED 2026-07-08: pending-trade card said "will never fill" for orders already sent
`_pending_reason()` (app.py) had only two states: "needs $X to size" (funding gap, order never
sent) and a generic "awaiting the next mirror cycle" -- it couldn't tell the difference between
an order that was genuinely never sent and one that WAS sent but just hasn't filled yet (e.g.
placed outside US market hours). The trailing card text ("never placed on the broker, will never
fill on its own") is actively wrong for the latter case. Root-caused live: two paper trades (CWB,
AMLP, both opened 2026-07-08T06:13 UTC = 2:13am ET) showed as pending with that wrong message;
direct IBKR query confirmed their entry orders were `PreSubmitted`, `filled=0.0`, correctly
waiting for market open (9:30am ET) -- not broken, not a funding issue. Fixed: `_pending_reason()`
now returns `(reason, order_already_placed)`, checking `broker.executed_ids()` (already-existing
helper -- a trade has a broker mirror row) before falling through to the generic message. Card
text and the "(not on {broker})" ticket tag both now correctly say "order already placed, waiting
to fill" / "(order placed, unfilled)" instead of the previous wrong wording. Verified live: both
CWB and AMLP corrected after the fix. Committed `06894cd`.

### 🐞 FIXED 2026-07-08: Expectancy/Max-drawdown KPI cards had no n≥30 trust context
Prompted by user's 6-point critique of the trading plan (② "monitor real expR & drawdown after
n≥30 trades, not short-term CAGR"). `paper.stats()` already computes `trustworthy: n >= 30` as a
GENERAL property of the stats dict (same `n` backs expectancy, drawdown, and win rate alike) but
`retrospective_panel()` only surfaced it on the **Win rate** card ("≥30 to trust"/"trustworthy");
Expectancy showed a bare `n=X` and Max drawdown showed only the account-% conversion with no
trust framing at all — silently contradicting the exact metrics ② calls out. Fixed: both cards now
share the same `n=X · ≥30 to trust / trustworthy` subtitle as Win rate. Verified rendered on both
dashboards after restart (3x "≥30 to trust" each, n=6 on both books currently — n≥30 is many
months away, not a near-term milestone). Also evaluated the other 5 critique points: ① (execute
contribution plan) and ⑥ (avoid manual signal overrides) are financial-planning/behavioral, no
code artifact; ③ (review Rejected Signals / P&L ex-deposits) already implemented and working; ④
(Phase 2 + estate tax at >500K) already accurate — `PHASE2_NAV_USD=64000` and the ~$60k US
estate-tax line coincide within ~7%, not a conflation error. ⑤ (0.75% risk / expand universe): got
the EXACT (not interpolated) 0.75%-risk backtest figure by temporarily adding 0.0075 to
`RISK_LEVELS` in `backtest.py` and running the full 21-ETF production backtest — **CAGR +7.9% /
maxDD −18.0% / ratio 0.439**, in line with the flat ~0.43-0.44 ratio already seen at 0.25/0.5/1%,
i.e. 0.75% is a pure linear interpolation point with no special risk-adjusted edge; reverted the
temporary edit immediately (confirmed clean via `git diff --stat`). Also confirmed 0.75% isn't
even a selectable dashboard option (UI toggle only has 0.25/0.5/1/2%) and universe expansion isn't
currently live-actionable (3 straight rejected screening batches, 7-9, already recommend pausing).
No universe/risk-level code change made — the backtest confirmed the status quo, it didn't justify
a change.

### ⭐ SINGLE-ENDPOINT paper/live MODE-SWITCH (2026-07-01) — SUPERSEDES the two-instance model below
User wants **same domain + port** (Cloudflare `quant.carsonng.com` → localhost:8080 only) and
quant.carsonng.com to reach LIVE. So the two-port/two-instance design (below) is ABANDONED for this:
ONE dashboard on :8080, connected to ONE account at a time, chosen by a persisted **`dash_mode`**
(store key). `app._resolve_mode()` runs at startup: mode=live → sets `IB_PORT=4001`,
`IB_ACCOUNT=U12991898`, `IB_ALLOW_LIVE=1` (arms the guard); mode=paper → paper .env defaults +
pop IB_ALLOW_LIVE. UI header **"⇄ Switch to LIVE/PAPER"** button: live needs a RED confirmation
dialog, then persists dash_mode + `os._exit(0)` so the watchdog relaunches THIS process into the
new mode (~10s). Cloudflare needs **NO change** — :8080 serves whichever mode is active, so
quant.carsonng.com shows live after switching. Guard unchanged (paper-only unless live acct+port+flag).
**Requires the active mode's gateway running** (paper 4002 or live 4001). **LIVE GATEWAY CONFIGURED
2026-07-01:** copied C:\IBC → C:\IBC-Live, then StartGateway.bat CONFIG=C:\IBC-Live\config.ini,
TRADING_MODE=live, IBC_PATH=C:\IBC-Live, TWS_SETTINGS_PATH=**C:\Jts-Live** (separate settings, created);
config.ini TradingMode=live, OverrideTwsApiPort=4001, IbLoginId=carsonng2000, **IbPassword=BLANK**
(user fills live pw); start_hidden.vbs → C:\IBC-Live\StartGateway.bat. NOT yet logged in (needs pw +
first 2FA). ⚠️ SAME-USER caveat: carsonng2000 on BOTH 4001+4002 simultaneously may get one kicked by
IBKR — with the mode-switch (one account at a time) run only the active mode's gateway, OR get a 2nd
paper username for true concurrency. Live autostart/watchdog NOT set up yet (start manually first). **AutoRestartTime=08:00 (HKT)** set
in C:\IBC-Live\config.ini (AutoLogoffTime blank → auto-restart mode) → daily restart is session-
preserving, NO 2FA; 2FA still needed on first login, reboot/crash cold-start, and ~weekly forced re-auth. NOT concurrent (one
account at a time; switch = ~10s restart) — the trade for a single URL. `run_dashboard_live.ps1` +
`DASH_PORT`/two-instance bits are now SUPERSEDED (kept, harmless).

### (superseded) CONCURRENT PAPER + LIVE (2026-06-30) — two ISOLATED instances, not one dual-connection process
Chosen design: run the live book as a SEPARATE dashboard process, not by multiplexing two IB
connections in one (that would thread two accounts through ib_client's single global loop = the
fragile ib_async↔nicegui path, one bug from the live account). Everything is env-driven, so a
second instance just needs its own launch env. Enablers added:
- `ib_exec._guard()` — DEFAULT paper-only (unchanged). LIVE opt-in ONLY when **`IB_ALLOW_LIVE=1`**
  AND connected account == `IB_ACCOUNT` AND live port (4001/7496). Named-account match = a mis-set
  port/login refuses rather than trading the wrong book. Paper instance never sets the flag.
- `app.py` — `DASH_PORT` env (default 8080) + window title `[PAPER]`/`[LIVE]` + a prominent header
  badge (green PAPER / red "LIVE — REAL MONEY"). So concurrent windows are unmistakable.
- `run_dashboard_live.ps1` (repo template) — copy to C:\Scripts, fill in live acct; sets
  IB_PORT=4001, IB_CLIENT_ID=21, IB_ACCOUNT=U…, IB_ALLOW_LIVE=1, DASH_PORT=8081; watchdog loop.
- **Needs a 2nd IB Gateway** logged into LIVE on port 4001 (separate IBC/config) alongside the
  paper gateway on 4002. Paper instance keeps `C:\Scripts\dashboard.ps1` on 8080 untouched.
Verdict: paper on :8080 (DU…, guard paper-only), live on :8081 (U…, IB_ALLOW_LIVE) — fully isolated.
**UI SWITCH (2026-07-01):** header button "⇄ LIVE"/"⇄ PAPER" navigates the browser to the OTHER
instance (JS `window.location` host + `OTHER_DASH_PORT`, default swaps 8080↔8081) — a *navigation*
switch, NOT an in-process connection toggle (that would collapse the isolation). Live account =
**U12991898** (currently ~40 HKD, unfunded). Balance detection needs NO new code — the header already
reads NetLiquidation from the connected account, so the live instance shows the live balance
automatically once its gateway (4001) is up. As of 2026-07-01 only the paper gateway (4002) + paper
dash (8080) run; the live gateway (4001) / C:\IBC-Live / live dash (8081) are NOT started yet.

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

**TOTAL return incl. idle-cash interest (`--cash-yield`, real ^IRX 13wk T-bill, 2026-06-25):**
the backtest CAGR above is STRATEGY P&L with cash@0%. Crediting idle cash (strategy is ~60% in
cash at 0.5%) adds, full-history avg, **+1.3% @ 0.5%** (4.4→**5.7%**), +1.7% @0.25%, +0.9% @1% —
**max DD UNCHANGED** (free, no added risk; +months 55→61%). It's the risk-free rate, NOT alpha,
and is rate-regime dependent: ~0 in the 2010-21 zero era (so OOS uplift only +0.7%), ~+2.5-2.8%
at today's ~4% rates with ~60% idle. **Forward total @ current rates ≈ 7% (full-hist anchor) to
~12-13% (recent).** On a real IBKR account this is ~AUTOMATIC (IBKR pays interest on idle cash) —
no need to buy BIL/SHV. This is the ONLY "execution-layer" lever that survived (trade-cost/tax
optimisation are negligible for a liquid, ~3wk-hold strategy; partial-profit-taking, limit-order
execution, rebalance thresholds, SGOV-arb, rate-linked risk all rejected/marginal).
**HKD/USD CASH OPTIMISATION (the real execution lever — account config, NOT code/strategy):**
account is HKD-base holding USD ETFs. Idle HKD earns ~1-2%; idle USD earns ~4.5% (T-bill/SGOV);
and buying USD ETFs vs HKD cash can create a USD DEBIT charged ~5-6% margin interest. So the
actionable fix is ops, not research: (1) hold idle cash in USD (convert HKD→USD, IB FX spread
~0.2bp, keep NAV>$10k past IB's 0% tier) → captures the full ~+2.5% `--cash-yield` USD ceiling;
(2) optionally park USD in SGOV (~+0.3-0.5% over IB's rate); (3) keep a USD buffer to avoid the
5-6% debit. Realistic uplift: **~+0.5-1.5% if left in HKD, ~+2.5% if converted to USD**. NOT
backtestable (FX/IB-tiers/fills aren't in price data); NOT alpha (rate bonus, ~0 if USD rates→0).
Rejected/skip: TWAP-VWAP order-splitting (market impact ~0 at $130k on liquid ETFs); limit orders
= minor cheap insurance only (weekly/liquid book, not intraday).

**HONEST NUMBER (do NOT naively add strategy + cash):** the proper total is the `--cash-yield`
backtest CURVE (correlation-aware: when deployed you hold less idle cash → less interest; cash &
strategy returns are negatively correlated, so you can't pair best-case strategy with best-case
cash). That curve = **+5.7% full-history nominal @ 0.5%** (NOT 4.4%+2.7%=7.1%). Quote REAL returns:
−~2.5% inflation → **~+3% real (full-history) / ~+2% real strategy-only**. The +10-12% "recent"
is a ZIRP+tech-bull ANOMALY — anchor on full-history, never forecast off it.
**Cash lever & account size — SGOV bypasses IB's small-account throttle:** IBKR's *direct* cash
interest is throttled below NAV $100k (a ~$12.8k acct earns ~13% of full rate ≈ +0.3-0.5%). BUT
**SGOV (0-3mo T-bill ETF) pays its ~5% yield regardless of NAV** — so on a SMALL account, parking
idle USD cash in SGOV recovers the FULL ~+1.5-2.5% (current rates) that IB direct interest would
throttle. So the cash lever is ~+2% even on a small account IF via SGOV (not IB interest). Caveat:
needs HKD→USD + sell-SGOV-to-trade (T+1 friction). **AUTO-SWEEP BUILT 2026-06-26** (ib_exec.sweep_cash,
opt-in CASH_SWEEP=1, paper-guarded, parks 60% of idle cash in SGOV keeping a 40% buffer; runs after
sync_closures; dashboard shows "Cash in SGOV" stat+pie slice). **Real IB cash rate observed = 3.12%
APY** (not the 4.3% assumed), so SGOV (~4.8-5%) edge over USD-cash = **~1.8%** (~$1,480/yr on the $130k
paper acct at ~60% idle) — bigger than thought. Automation threshold therefore LOWER: worth it from
~$75-100k NAV; below that just hold USD cash (3.12% auto) and skip the complexity. **Exchange loss is
negligible:** IB FX ~0.2bp+min$2 (~0.005% one-time), HKD pegged to USD (no real FX risk), and the book
trades USD ETFs so cash stays USD (no round-tripping). Convert HKD→USD freely. (Rejected refinements: weekly contribution-splitting = DCA drag, return-negative + moot vs
signal-driven entries; rebalance dead-band = redundant, strategy already gates on trend-strength≥5.) FORWARD total @ constant current rate (`--cash-rate 0.043`, IB USD rate; achievable on ANY
account size via manual SGOV): @0.5% **+6.1% nominal / ~+3.6% real, maxDD −9.2%** (cash drip
slightly REDUCES DD: −10.5→−9.2); recent OOS +11.2%/−5.9%; IS +4.5%. (+1.7% over strategy-only,
> the +1.3% historical ^IRX figure since current rates aren't ZIRP-dragged.) So the all-in REAL
expectation is **~+3.6% real @0.5% if cash is SGOV-parked at current rates** (~+2-3% if rates fall
or cash left in HKD/throttled IB interest). Value = low-DD low-stress real
positive return; the growth engine is CONTRIBUTIONS + capital, not overlays.

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
0.79; PARTIAL profit-taking also REJECTED 2026-06-25 — 50%@1.5R/2R/33%@1R +BE all < fixed on
BOTH expR & ratio (0.73/0.78/0.51 vs 0.81); raises win% 48→51-56% but caps the fat-tail winners,
classic win-rate-vs-expectancy trap; cutting winners loses, as TF theory predicts), pullback entry (--pullback, 2026-06-23:
wait <=2wk for retrace to within 2% of 20wk MA else skip. expR UNCHANGED 0.357→0.351 & win 49%→49%
= NO entry-timing alpha; but drops 58% of signals 1118→465 — the non-retracing breakouts are the
strongest runners — so OOS CAGR collapses 9.9%→3.7%, ratio 1.52→0.82. DD "improves" only via idle
cash. Classic miss-the-runners failure), shorter
horizons (4–6wk plateau), shorts (net-negative→long-only), concentrated (no-op — de-corr
buckets empty for futures+ETF), tail-risk circuit breaker (kills CAGR, no DD help),
class-weighting (worse OOS DD), SPY-regime overlay (hurts diversified book), VIX-regime size
ladder (2026-06-23 --vix-regime: +10.1%/-6.5%→+8.4%/-7.7%, worse CAGR AND worse DD despite
cutting exposure 15% — VIX is coincident/lagging, trend filter already de-risks endogenously;
**RECONFIRMED 2026-07-08 on the current 21-ETF book** (was 18 at the original test) — 0.5% risk
+5.4%/-12.3%→+4.3%/-12.2%, OOS +13.3%/-6.6%→+11.1%/-7.5% (CAGR down, OOS DD *worse* not better).
Same verdict holds post-universe-growth; kills the "LLM as macro risk-dial" idea too — a slower,
noisier signal than VIX can't fix a structural redundancy VIX itself doesn't fix.
**MILD class-tilt variant TESTED 2026-07-08** (a user suggestion proposed a gentler ±10-15% static
tilt toward historically-stronger classes, distinct from the aggressive 0.25x-2.0x trailing-12mo
version already rejected above): temporarily narrowed `_class_factor`'s clamp to 0.85x-1.15x and
reran on the 21-ETF book — 0.5% risk +5.4%/-12.3%→+5.5%/-11.6% (CAGR +0.1pp, a wash), but OOS
+13.3%/-6.6%→+13.2%/-7.6% (OOS DD *worse*). Milder than the aggressive version's damage but still
not a real improvement — reverted, not adopted. Also note: the proposed method (weight by
full-sample expR) would itself be look-ahead-biased in a live implementation; the walk-forward
trailing-12mo version tested here is the honest way to test this and it still doesn't clear the bar.
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
- **Options "lottery fund" (LEAPS state-machine + weekly debit spreads)** — PROPOSED 2026-06-29
  (a "5K HKD/mo lottery" sleeve: buy SPY 9mo 10%-OTM puts on first 50MA break; weekly SPY debit
  spreads on >5MA+ADX>25). REJECTED — signal-level test on SPY (1993-2025,
  `scratchpad/spy_signal_test.py`): (a) after a first 50d-MA break SPY is UP 79% over 9mo (mean
  +8.0%), falls >10% only 12% — buying puts there is shorting the equity-risk-premium, deeply
  -EV (not the claimed 37% win/+112%); (b) >5MA+ADX>25 → next-week signed return −0.21%, win 47%
  (below coin flip) before option theta/spread cost. Both the doc's "historical" tables were
  FABRICATED (its own §7 lists the backtests as not-yet-run). Also: one SPY 9mo put ≈ $800-1500
  > the $640 budget (unaffordable), and options-chain/IV data isn't in-stack to backtest properly.
  Verdict: negative-EV timing/vol bets in the no-edge space already falsified; put 100% of the
  30K/mo into the core ETF book — at this size the savings rate is the engine, not a -EV sleeve.
- **Short-vol / variance-risk-premium (weekly SPY iron condor)** — TESTED 2026-06-29
  (`dashboard/research/short_vol_test.py`), the "last untested frontier" pitch (sell delta-0.25
  condor every Mon, harvest theta). No options chain in-stack, so legs are **BS-modelled with
  ^VIX as IV** (reproduces BOTH the VRP edge — IV>RV — AND the tail: a big weekly move blows
  through the shorts → max loss). 33.4y, ~52/yr. RESULT: edge is REAL but TINY and FRAGILE.
  base meanR **+0.017** (win **65%**, matches the pitch's 65-75% claim), OOS +0.029, **DSR only 60%**
  (< 95% bar). Three things kill it: (1) **COST** — at 8%-of-maxloss frictions (realistic for a
  4-leg weekly SPY condor on a small acct: bid/ask + commissions) meanR flips **−0.023**, totalR
  +30R→−40R. The pitch's "+$10/wk / +3,700 HKD/yr" EV simply **ignores transaction costs**, which
  dominate a weekly 4-leg structure. (2) **TAIL not controllable** — the pitch's "2x-credit stop"
  produced IDENTICAL results to hold-to-expiry (weekly gap moves blow through short strike AND stop
  in the same week; worst week −1.04R = full max loss). Refutes the central safety claim. cum
  drawdown −22R vs +30R total over 33y = gives back ~9 months of premium per crash. (3) The pitch's
  own strike math is wrong: **delta-0.25 WEEKLY ≈ ~2% OTM, not "5-8%"** (5-8% OTM weekly is
  delta ~0.03, ~no premium); it conflates monthly w/ weekly. VERDICT: classic pennies-in-front-of-
  steamroller — small positive carry, equity-like left tail, edge eaten by retail frictions, DSR
  fail, "stop" doesn't protect. Same family verdict as CBOE PUT/PutWrite literature. **The "short
  not long" reframe doesn't escape the no-edge space.** All option sleeves now falsified.
- **Earnings vol-crush short strangle + 0DTE Friday "pin-risk" butterfly** — PROPOSED 2026-06-29
  (sell single-name strangles pre-earnings to harvest the IV crush; sell 0DTE SPY butterflies
  Friday afternoon for OpEx gamma-pin theta). DATA VERDICT FIRST: yfinance options = LIVE chain
  only (no historical IV), intraday = 5m/60d & 1m/7d only. So (a) the earnings **crush MAGNITUDE**
  (the whole edge) is NOT observable in-stack, and (b) **0DTE pin is NOT backtestable at all** (no
  historical intraday underlying or 0DTE chains; 0DTE only existed ~2-3y; the "pin" anomaly is
  contested — dealer gamma cuts both ways — and carries the fattest gamma tail). Earnings part
  TESTED on the part we CAN measure (`dashboard/research/earnings_vol_crush_test.py`, 9 mega-caps,
  earnings 2014+, 666 events): realized earnings gaps are HUGE (mean |move| 4.5-11.6%, 95th pct
  11-26%, **max 35% NVDA / 52% AMD / 42% NFLX**); a 1.5×-expected-move short strike is breached
  **12-31%** of the time (so "90% win" is really ~70-88%). Modelling the strangle and **GRANTING a
  generous 10% IV-overpricing edge + only 6% friction**: meanR **+0.001** (dead flat), win 87% (the
  trap), **worst single event −5.43R**, IS meanR NEGATIVE −0.017 / OOS +0.023 (unstable), **DSR
  40%**. A NAKED single-name strangle = undefined risk; one 20-50% earnings gap is catastrophic on
  a real acct. VERDICT: zero-EV-with-a-cliff even before honest costs/IV; pure single-name
  idiosyncratic risk (which the project already rejects, see "Funds/individual stocks: rejected");
  the +EV claim is unfalsifiable in-stack and the measurable parts are damning. Both REJECTED.
- **Market-neutral pairs / stat-arb** — TESTED 2026-06-29 (`dashboard/research/pairs_test.py`): 10 economic ETF pairs (GLD/SLV, SPY/QQQ, IEF/TLT, EFA/EEM...), log-spread 60d z-score, enter|z|>=2 exit|z|<=0.5 stop|z|>=3.5, 0.20% round-trip cost. REJECTED: full-hist meanRet -0.18%/trade, OOS -0.13%, annSharpe -0.21 to -0.35, DSR 0%; 8/10 pairs -EV. 56% win is a trap (small wins, fat divergence losses + ~44 trades/yr x cost). The last 'short-term uncorrelated' candidate -- dead. CONFIRMS: no short-term/timing/stat-arb edge survives for us; only the slow trend + risk-dial + (modest) MR sleeve work.
  **RE-CONFIRMED 2026-06-29 with a RIGOROUS refined test** (`refined_statarb_test.py`, after user asked
  "stat-arb seems better, adopt its settings?" — premise is BACKWARDS, ours LOST): 29-ETF pool, 406 pairs,
  IS-select top-10 by in-sample MR Sharpe → trade OOS. Top IS pairs are SAME-INDEX WRAPPERS (SPY/VOO 1.43,
  IVV/VOO, SPY/IVV) — spread lives INSIDE the bid-ask, IS Sharpe is an illusion. OOS: @0.04% cost (fictional)
  meanRet +0.13%/Sharpe 0.54/DSR 17%; @0.10% +0.07%/0.29/DSR 3%; @0.20% (realistic) −0.03%/−0.13/DSR 0%.
  Win% collapses 61→38→32% as cost rises = the thin-margin-reversion death-by-cost. **DSR never clears 95%,
  Sharpe ≤0.54 (< core 1.25).** Why reputational stat-arb (Medallion) "seems better": thousands of names +
  HFT + EARNING the spread (rebates/co-lo, ~0 cost) + 12-20× leverage — NONE retail-replicable; the edge is
  INFRASTRUCTURE, not the z-score concept. Transferable principles (breadth, MR) we ALREADY have: trend book
  corr 0.26 + panic-MR sleeve (works as a 1-leg directional bounce, not a 4-leg spread). Don't adopt; dead end.
- **Sector-rotation cross-sectional MR** — TESTED 2026-06-29 (`dashboard/research/sector_mr_test.py`):
  weekly, buy the WORST-2 of the SPDR sectors by trailing 1-wk return, hold 1wk. The raw worst-2
  basket looks fine (+7.6%/yr classic-9, +17.6% OOS user-list) but that is JUST equity beta. The
  proper control — ALPHA = worst-2 minus equal-weight-9 — is NEGATIVE everywhere: classic-9
  (27.5y) full −2.9%/yr, OOS −7.8%, DSR 0%; user's 9-list (8y, XLRE/XLC) full −4.0%, OOS −0.9%,
  DSR 1-7%; and it gets STRICTLY WORSE at realistic 0.10%/leg cost (full −7.8%, OOS −12.5%).
  Buying losing sectors UNDERPERFORMS simply holding the basket — the diversification given up by
  concentrating in 2 names dwarfs the tiny reversion tilt, and cost finishes it. REJECTED. The
  "if this loses too, short-term is dead" candidate from the 2026-06-29 chat — it lost.
- **Panic-MR SIZING + BLEND study** — 2026-06-29 (`dashboard/research/dipbuy_sizing.py` +
  `dipbuy_blend.py`), answering "put more into the dip-buy since it's +EV?": YES lift off the token
  2K to **risk-matched (~one core risk-unit: ~$650 risk ⇒ ~$10-13k / ~80-100K HKD per trade** at the
  −5% stop, from the SGOV pool). **BLEND TEST (the real answer, 30.3y incl. 2008/2020, full 18-ETF
  screened book = ETF_UNIVERSE+ETF_CANDIDATES, idle cash @4.3%):** core-only **+6.94% CAGR / −9.7% DD
  / Sharpe 1.05 / ratio 0.72** → **core+dip +7.39% / −9.7% / 1.11 / 0.77**. i.e. **+0.45pp CAGR at
  FLAT drawdown**, Sharpe up. (NB: a first blend run mistakenly used the bare 10-ETF `ETF_UNIVERSE`
  base → too-low +4.95% core; the real book needs `+ETF_CANDIDATES`; dipbuy_blend.py fixed.)
  **⚠️ CORRECTS an earlier wrong claim in this
  doc that the dip-buy is "tail-correlated / stacks / concentrates"** — that was true of the slower
  WEEKLY --meanrev sleeve (long hold, adds exposure through drawdowns), but the panic dip-buy is a
  FAST ~3-day scalp of the BOUNCE off a VIX-spike low (tight −5% stop): on sleeve-active days
  corr(core,sleeve) = **−0.25 (mildly DIVERSIFYING)** — it harvests the rebound near the core's
  troughs, cushioning them. So at risk-matched size it is modestly accretive on BOTH return and risk,
  not just 手癮. CAVEATS: gain is small (+0.44pp on ~5% base); only 103 trades/30y so the −0.25 corr
  + Sharpe bump have wide error bars (read as "doesn't hurt, slightly helps", NOT a guaranteed
  −1.1pp DD); risk-matched is the sweet spot (bigger erodes the favorable ratio — sleeve-alone DD
  grows); adds a DAILY trigger-watch atop the weekly core. Capacity-limited (~3.2 trades/yr × ~3d =
  ~2.6% of calendar deployed, pool ~97% idle in SGOV). Core 30K/mo stays the engine; the sleeve nudges.
- **Panic-MR REFINEMENTS** — 2026-06-29 (`dashboard/research/dipbuy_refine.py`), testing the
  pitch's tweaks (core 18-ETF +7.11%/−9.6%/Sharpe1.07 baseline). (A) SIZING SWEEP: Sharpe & ratio
  rise MONOTONICALLY with size to 3% risk/trade (no interior optimum) — a LEVERAGE ILLUSION (the
  backtest assumes the −5% stop always fills; real 2008/2020 GAPS through it, clustered at VIX>30).
  So optimal ratio is risk-BOUNDED not Sharpe-maxed: **0.5% risk/trade (~90K HKD), up to 1% (~180K)**
  — 0.5% captures ~95% of the ratio gain (0.74→0.79 vs 0.81 plateau) at unchanged −9.6% DD. (B)
  **VIX-SCALING = VALIDATED, adopt**: dip edge is almost all in VIX>30 entries — meanR VIX<20 +0.48%
  / 20-30 +0.32% / **>30 +2.28% (win 79%, n=47)**, ~5-7×. Rule: small/skip <30, size up (1-1.5%)
  >30. (C) MULTI-ASSET QQQ/IWM/XLK: QQQ meanR +1.93%/XLK +1.77%/IWM +0.62% (weak); pooled 4-asset
  blend @0.5% = +9.05%/−10.1%/Sharpe1.28 vs SPY-only +7.56%/−9.6%/1.13. BUT SPY/QQQ/XLK 0.85-0.95
  corr ⇒ mostly CORRELATED SIZING-UP not diversification (don't double-count w/ (A)); worth it for
  frequency (~3→~13/yr) + small Sharpe edge; drop IWM. (D) STAGED EXITS = REJECTED: base meanR
  +1.21%/win75% → staged +1.10%/win66% (WORSE — same as trend book, the MR edge IS the snap to 5MA;
  holding for a bigger bounce gives it back; pitch's "+1.5-1.8%" claim is false). REJECTED in pitches
  (already-settled, not re-run): vol-targeting/risk-parity (book already ATR=risk-parity; --voltarget
  = pure leverage, ratio flat), XSMOM "top-30%" (=--mom-filter top-5 ≈ top-30% of 18 → CAGR halves,
  ratio 1.54→0.91; breadth IS the edge). Irish-domiciled ETFs (CSPX/VUAA/IDTL) = legit OPS not alpha:
  dividend-withholding 30→15% is weak for a 3wk-hold book (matters on bond sleeves only); the REAL
  point they miss = US ESTATE TAX (US-domiciled >$60k = US-situs, up to 40% on a HK NRA's death;
  Irish UCITS avoid it) — worth it as the acct grows, offset by worse UCITS liquidity + no clean
  CPER/PFF/DBC equivalents. NET RECO: SPY+QQQ+XLK, 0.5% base VIX-scaled (1-1.5% >30), base exit →
  blend ~+8.5-9%/−10%/Sharpe~1.25 vs core +7.1%/−9.6%/1.07; real ~+1.5-2pp, but leans on clean stops
  — size for the gap-through, hence cap ~1% though the optimizer screams 3%.
- **Panic-MR ROUND-2 refinements** — 2026-06-29 (`dashboard/research/dipbuy_refine2.py`), critics'
  round-2 ideas: (1) **Connors RSI(2)<10>200MA entry = LATERAL not better**: 8.6/yr but meanR only
  +0.53% (vs vix_panic 3.5/yr +1.21%), same blend Sharpe 1.11≈1.12 (freq-for-quality swap; a 手癮
  "more trades" dial, not a perf gain). (2) **ADX>20 filter = ADOPT (critic right, against my prior):**
  meanR +1.21→**+1.40%**, win 74→78%, loses only 7/105 trades; ADX<20 trades are −0.25% (dead money).
  Why (vs old --meanrev's ADX<20): this is buy-the-panic-dip-IN-A-TREND, needs a trend to snap back
  into — different signal than z-score range-reversion. (3) **VIX-percentile ≈ absolute** (edge rises
  vpct<50 +0.74%→ >90 +1.31%, same shape); percentile is more regime-robust, no extra alpha. (4)
  **GAP-REAL stops (fill −5% at the actual gapped close) — settles the cap debate:** 0.5% risk DD
  −9.8→−10.1 (fine), 1% −10.5→−11.1 (fine), **2% −13.5→−19.8 (blows out)** — empirical proof 0.5%
  default/1% hard cap is right, 2%+ dangerous. CRITIC WRONG on "market-order stop when VIX>30":
  a market order fills at the gapped-down OPEN = same loss; order type can't beat an overnight gap,
  only SIZE can. **FINAL DIP SLEEVE SPEC:** SPY+QQQ+XLK (no IWM), entry >2.5% below 20MA + VIX↑>15%/5d
  + RSI(14)<35 + **ADX>20**, size 0.5% base / 1.0% at VIX>30 (HARD CAP 1%), exit 5MA-touch|+3%|−5%soft|
  10d (NO staging), funded from SGOV. Gap-real blend ≈ **+8.5-9% CAGR / −10 to −11% DD / Sharpe 1.23-1.29**
  vs core +7.0%/−9.7%/1.06 (sleeve adds ~+1.5-2pp CAGR, ~+0.17 Sharpe, ~flat DD). Caveats: ADX split
  n=98/19 (small), gap-real still understates flash-crash, SPY/QQQ/XLK 0.85-0.95 corr (concentrates) → cap 1%.
- **Panic-MR ROUND-3 = ALL REJECTED, sleeve SATURATED** — 2026-06-29 (`dipbuy_refine3.py`), critic's
  round-3 micro-carvings: (1) **rel-strength filter (SPY underperf RSP/VT>1%)** — directionally
  suggestive (vs VT: underperf +2.98% vs +1.58%) but n=3 (RSP)/n=9 (VT) = UNUSABLE, decimates freq
  to ~0.3/yr + loses pre-2003/08 history; already captured by VIX>30. (2) **deep-overshoot amplifier
  (<−10% below 200MA)** — that bucket IS high-edge (+2.35%, n=29) but REDUNDANT with the VIX>30
  scaling (a −10% 200MA break ≈ a VIX>30 panic; middle bucket −10..−5% is weakest +0.44% = noisy,
  not a clean dim). (3) **VIX-crush early TP (exit VIX −20%/2d in profit)** — INERT: base exit already
  faster (~2.7d), blend byte-IDENTICAL +8.72%/−8.9%/Sh1.27, per-trade marginally worse. Predicted
  +0.1-0.5%/each → reality ~0. **DIP SLEEVE NOW SATURATED — round-2 spec is final; further tuning =
  overfitting. STOP carving, DEPLOY.** (NB ADX-filtered blend DD −8.9% < core −9.7% — filter cleans it.)
- **CORE/OPS proposals ROUND-4** — 2026-06-29 (`dashboard/research/core_ops_test.py`): (A)
  **VOL-TARGETING re-tested on ETF book, REJECTED (critic's table fabricated):** critic claimed @12%
  DD −9.6→−8.2 / Sharpe →1.15; REALITY fixed 0.5% +6.95%/−9.7%/Sh1.22/ratio0.72 → voltarget 12%
  **+14.05%/−29.5%(TRIPLED)/Sh0.99/ratio0.48**, 15% +14.9%/−29.6%/0.96. It LEVERS UP ~3× (book runs
  ~3-4% realized vol; "target 12%" = 3× leverage) → CAGR 2× DD 3×, ratio worse. Vol-target only cuts
  DD if you target BELOW current vol (= just de-risk, which 0.5% already does). (B) **MONTHLY bars
  REJECTED:** weekly +6.95%/−9.7%/Sh1.22 (~38rt/yr) vs monthly +4.09%/−11.1%/**Sh0.70** (~10/yr) —
  coarsening the signal crushes it; critic's "monthly only −0.3% CAGR" off ~10×; premise wrong anyway
  (signal-driven ~32-38rt/yr, NOT 52× rebalance; costs already in R). (C) **VIX-SCALED CONTRIBUTIONS
  = CLAIM RETRACTED after a FAIR test.** The fwd-12mo SPY +23.8%@VIX>30 vs +12.0% all (n=32) is real
  but MISLEADING — it ignores the opportunity cost of holding cash while waiting. Equal-total-contribution
  sim (bank cash, deploy at VIX>30, 34y SPY, reserve@3.6%): tilt UNDERPERFORMS pure DCA by **−3.9%
  (bank30%) to −6.6% (bank50%)** terminal — time-in-market beats timing even WITH a +EV panic signal,
  because the market's up ~80% of months @~10% vs reserve 3.6%. So DON'T hold back the regular 30K/mo
  flow. The ONLY +EV form: deploy GENUINELY-IDLE emergency cash (earning ~0) opportunistically at
  VIX>30 — +EV only vs 0%, a bounded one-off, NOT a CAGR lever. NET PERFORMANCE CHANGE this round = 0.
  Ops endorsed (not backtested): FX via IDEALPRO (already live via CASH_USD), automate+rule-gate the
  sleeve & don't override on streaks & review monthly, Irish-UCITS at $200k+ for long-held core (estate
  -tax driver). **RESEARCH FULLY CLOSED; remaining edge is purely behavioral — contribute relentlessly,
  do NOT hold cash to time the market, opportunistically deploy idle cash in panics, don't touch the red sleeve.**
- **"5 engineering layers" ROUND-5 = ALL REJECTED** — 2026-06-29 (`corr_penalty_test.py`): (1) VIX
  CLOSE>30 trigger — directionally right (fwd-4wk SPY close>30 +2.47% vs intraday-only +1.45% vs calm
  +0.70%) & already how signals work; but serves the DCA-losing contribution-timing idea → minor exec
  detail only (use weekly close for opportunistic idle-cash deploy), "+0.5-1% IRR" fabricated. (2) ATR
  override 1.5× risk at VIX>30 = LEVERAGE-IN-DISGUISE (ATR shrinking notional in high vol is the FEATURE
  = constant risk; 1.5× = the tail we proved dangerous, gap-real 2%→−19.8% DD) → REJECT. (3) Correlation
  penalty TESTED — INERT (byte-identical 6.97%/−9.7%/Sh1.24 at every threshold) + premise FALSE: book
  avg pairwise corr median **0.26**/90th 0.51 (genuinely multi-asset, only ~4/18 US equity, NOT "17
  equities @0.85"); "-35% DD" refuted by 30y incl 2008/2020 = −9.7% (trend EXITS in crashes). Same as
  prior regime-overlay rejections. (4) Tax-loss/STCG/wash-sale = WRONG JURISDICTION: user is **HK = ZERO
  cap-gains tax**; US doesn't tax NRA cap gains (only divs, 30%→15% via Irish UCITS already). DELETE the
  layer entirely (it'd add churn for 0 benefit). (5) Override circuit-breaker — principle (precommit) good
  but specifics broken: "200d-MA of the sleeve" nonsensical (sparse ~3d holds, no price series); "cut risk
  after 4 losing wks / VIX>40 falling" DE-RISKS when fwd returns are HIGHEST (backwards). Correct precommit
  = the EXISTING tripwire (realized DD >−13% → halt NEW entries; expR-sign at n≥30); no risk-cuts on streaks.
  **NET: 0 adopted; 2 factual errors (corr, tax), 1 re-rejected leverage, 1 polish of a dead idea, 1 broken-
  specifics. System FINISHED — further "suggestions" are re-treads/errors. Deploy; collect n≥30 broker-truth.**
- **SPY panic dip-buy entry** — TESTED 2026-06-29 (`dashboard/research/spy_dipbuy_test.py`), the
  proposal's specific rules (close >2.5% below 20d-MA + VIX up >15%/5d + RSI(14)<35; exit at 5d-MA
  / +3% / −5% / 10d cap, 0.10% round-trip). VERDICT: genuinely **positive-EV** — 33.4y, 106
  triggers (~3.2/yr), expR **+1.21%/trade** cost-adj, win **75%**, holds OOS (+0.89%/trade, win
  73%). (Proposal under-stated win 62-65% & over-stated avg loss −4.2%; real avg win +2.6% / loss
  −3.0%.) This is NOT a new edge — it's the same oversold-reversion family as the validated MR
  sleeve (expR +0.451, §Multi-strategy blend). It works but is IRRELEVANT to wealth: ~3 fires/yr ×
  tiny capital = the proposal's own ~+300 HKD/yr "entertainment rebate." Honest framing = a
  psychologically-satisfying hobby sleeve that won't destroy capital, NOT a return driver. The 30K/mo
  contributions + the core 17-ETF book remain the engine. If the user wants the手癮 sleeve, fund it
  small (2K) from cash and gate on these (validated) conditions; do not divert from the core.
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
