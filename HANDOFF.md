# Project Handoff — D:\quant quant trading platform

**Purpose of this doc:** let a new session continue the work without prior context.
Last updated 2026-07-08.

---

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

**Two phases, auto-switched by equity** (`paper.account_phase()` / `sleeve_active()`, threshold
`PHASE2_NAV_USD`=$64k ≈ 500K HKD; UI shows a Phase badge):
- **Phase 1 (<500K): core 17-ETF only**, 1% + 25% cap. (100K start is here for ~1.1y.)
- **Phase 2 (≥500K): core + panic-MR sleeve** (SPY/QQQ/XLK, ADX>20, 0.5%/1%@VIX>30), same cap/risk.
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

**CONFIRMED WORKING later the same day:** the scheduled 08:00 AM daily auto-restart fired naturally
and completed in ~15 seconds with **no manual 2FA needed** -- the first real proof the format fix
actually restored the session-preserving behavior that had silently never worked before.

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
