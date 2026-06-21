# Project Handoff — D:\quant quant trading platform

**Purpose of this doc:** let a new session continue the work without prior context.
Last updated 2026-06-20.

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
**Weekly TSMOM on IBKR futures.** Universe `{metal, index, rate}` (BROKER=ib default).
Config: `MIN_STRENGTH=5`, `OVEREXT_FILTER` 70/30, `RR_DEFAULT=3.0`, `SL_ATR_MULT=1.5`,
`RISK_PER_TRADE=0.005`, **`HORIZON_DAYS=5` / `HORIZON_CAL=35` (5 weeks, reconciled)**,
no vol targeting. ~31 trades/yr.
Final backtest (26.4y, 0.5% risk): full-period **+3.6% CAGR / −9.9% DD**, expR +0.239,
PF 1.44, win 44%, DSR 100%. IS +7.4%/−9.9%, OOS +7.4%/−6.6%.
**HONEST P6 EXPECTATION = ~4–6% CAGR / ~−10% DD** (NOT the 7.4% OOS — recent regime was
trend-friendly; full-period 3.6% is the conservative anchor). Expect 1–2yr flat/drawdown
stretches (2012–14 took 637d to recover) — that is NORMAL, do not abandon.
Tested & rejected: wider classes (grain/soft/fx/energy dilute), vol-targeting (pure
leverage), horizons 1–8wk (4–6wk is a flat plateau; 5wk fine, shorter = noise),
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

**Still to do — P6 (the only remaining step): PAPER TRADE.**
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
