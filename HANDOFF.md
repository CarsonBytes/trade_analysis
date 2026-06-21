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

## Key files
- `paper.py` — signals→trades, gates, sizing, resolution, journal, archive.
- `providers.py` / `mt5_client.py` — data (weekly W1) + MT5 client.
- `confidence_model.py` / `win_model.py` — objective gate models (weekly-trained).
- `backtest.py` — portfolio backtest (`--weekly`, `--longweekly`, `--adx N`). The real one.
- `ab_overext.py`, `ab_regime.py`, `ab_meanrev.py`, `ab_breadth.py`, `ab_fx_validate.py` — A/B harnesses.
- `structure.py` — price-based swing/zone/FVG-proxy features (informational).
- `retrospective.py` — KPI report (broker-truth + all-paper views). `executor.py`, `link_monitor.py`.

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
  (`uv run python -m dashboard.test_contracts`).
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
  `reconcile`. Own `ib_mirror` sqlite table. CLI: `uv run python -m dashboard.ib_exec`.
- ✅ `broker.py` (step 6) — BROKER dispatch shim (executor | ib_exec); `service.py` calls
  `broker.mirror_new/sync_closures/live_positions`. `paper.py` SL/TP rounding + instrument
  lookups + resolution made broker-aware (futures round to contract tick, not MT5 digits).
- Verified no regression: default → executor + 31-instrument universe; `BROKER=ib` →
  ib_exec + 21 futures. Both import and run clean; pure-math tests pass.

**NOT yet done — needs a running TWS/IB Gateway paper session (can't verify offline):**
1. `uv run python -m dashboard.ib_client` (diagnose) — confirm connectivity + the SPECS
   cross-check (multiplier/tick vs broker). **Curated SPECS must be broker-verified before
   any order.**
2. Validate `continuous_rates` weekly vs yfinance GC=F/ES=F (the #1 continuous-vs-front
   trap, IBKR_SCOPE §6).
3. Set `.env`: `BROKER=ib IB_HOST=127.0.0.1 IB_PORT=7497 IB_CLIENT_ID=7 IB_ACCOUNT=DU…`
   then place ONE live signal end-to-end on paper; confirm bracket + fill + reconcile.
4. **Strategy decision (not code):** `WEEKLY_TREND_CLASSES={metal,energy,index}` currently
   EXCLUDES the new rate/grain/soft/fx futures. On futures, diversification is the whole
   edge (HANDOFF §"Open items") — to trade ZN/ZB/ZF etc. set `WEEKLY_TREND_CLASSES=set()`
   (all) or add the classes. Deliberate user call; left as-is so nothing changes silently.
5. Run the weekly backtest on IB continuous futures to re-confirm the edge before scaling.

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
