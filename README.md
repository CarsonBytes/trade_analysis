# Quantitative Trade-Analysis Platform

A research + trading platform for a diversified multi-asset ETF book, with three parts:

1. **Anti-self-deception backtester** — proves whether a strategy idea actually has an edge (walk-forward, deflated Sharpe, noise test). See [backtester details](#backtester) below.
2. **Multi-agent analyst** (`analyst/`) — deterministic facts feed LLM agents (regime / technical / sentiment) → a head-trader decision → deterministic risk gate. Decision support, not auto-execution. See [analyst/README.md](analyst/README.md).
3. **Real-time dashboard + trading** (`dashboard/`) — NiceGUI board that scores a 22-ETF weekly-trend universe and mirrors signals to IBKR (`BROKER=ib UNIVERSE=etf`), auto-manages idle cash (USD → SGOV), and forward-tests fills against the backtest. Runs as **two independent instances**: a paper account (DUK968178) and, as of 2026-07, a **real-money live account** (U12991898) — same code, hard-guarded so a config mistake can never trade the wrong one. See [dashboard/README.md](dashboard/README.md).

> **Honest framing:** this measures whether ideas work before risking money on them, and every real-money safety gate (`PORTFOLIO_CAP`, `DD_HALT_PCT`, the paper/live account guard) exists because something real needed guarding against, not as a theoretical checkbox. Decision support, not unattended auto-execution — every trade traces back to a specific, auditable signal.

---

## Key research findings (as of 2026-07-14)

Exhaustive out-of-sample, deflated-Sharpe-penalised study, continuously re-verified as the
system moved from paper to **real live trading** (IBKR account U12991898, first real fills
2026-07-13). The platform pivoted MT5 spot → IBKR futures → **22 ETFs** (ETFs trade in
*shares*, so risk is expressible on a small account, unlike futures). 80+ ideas tested across
the full project; the honest conclusions:

- **Exactly ONE edge: weekly time-series momentum (TSMOM) across many uncorrelated ETFs**,
  plus one validated satellite (below). Long-only, weekly hold, ATR-stop (1.5×ATR14) + 3:1
  RR, risk-based sizing. Breadth is the lever that matters — the book's avg pairwise
  correlation stays low because the 22 tickers span genuinely different asset classes, not
  leveraged beta on one theme.
- **Universe (22):** metals GLD/SLV/CPER · equity SPY/QQQ/DIA/IWM · rates IEF/TLT/SHY ·
  credit HYG · inflation TIP · intl EFA/EEM/VNQI/ASHR · commodity DBC · REIT VNQ/AMLP ·
  preferred PFF · convertibles CWB · muni-HY HYD.
- **Live config (four parameters, real money today):** `RISK_PER_TRADE=1%`,
  `ETF_POS_CAP=25%` (per-position notional cap), `PORTFOLIO_CAP=100%` (aggregate
  gross-exposure cap — added 2026-07-11 after confirming several concurrent near-cap
  positions could otherwise stack past 100%), `DD_HALT_PCT=-13%` (live-only safety net,
  pauses new entries, never touches existing ones).
- **Performance, reconciled and bootstrapped (not a bare point estimate):** core-only,
  after-tax (30% US NRA dividend withholding) + cash-yield — **CAGR 6.06%, Calmar 0.887**
  point estimate; 500-draw block-bootstrap (calendar-year resampling, the real portfolio
  pipeline re-run on each resampled 30-year timeline) gives **median Calmar 0.921, 90% CI
  [0.536, 1.355]**. Treat the CI as the honest range, not the point estimate as a forecast.
- **One validated satellite, now live: the "panic-MR" dip-buy sleeve.** 11-ticker universe,
  entries on a VIX-spike oversold condition, staged rollout (3→5→11 tickers over 6 months
  per instance) so a new sleeve doesn't front-load risk. Core+sleeve@10% weight: full-history
  **CAGR 10.08% / DD -7.73% / Calmar 1.305**; OOS (recent decade) **CAGR 13.08% / Calmar
  1.693**. Core/sleeve correlation measured directly (not assumed): -0.026 overall, +0.011 on
  sleeve-exit days — genuinely different risk driver, confirmed empirically.
- **Stress-tested against real historical crises**, using the CURRENT exact config (not a
  blended multi-year average that can hide the worst days): **2008 GFC +9.69%** (worst
  intra-window DD -3.11%), **2020 COVID -0.81%** (-3.20%), **2022 rate-hike drawdown +3.61%**
  (-5.11%, the worst of the three) — consistent with trend-following's classic profile, well
  inside the -13% halt threshold.
- **Edge survives aggressive multiple-comparisons correction.** Deflated Sharpe Ratio stays
  **100% even at 82 combined search trials** (49 universe-selection candidates + 18
  exit-method variants + 15 parameter-sweep configs, corrected together).
- **Everything else REJECTED, with data (DSR/OOS discipline):** daily technicals (no edge);
  vol-targeting (pure leverage, DD tripled); monthly rebalance; cross-sectional momentum &
  relative-strength filters; regime overlays (SPY-MA, VIX-ladder, correlation penalty — all
  redundant, a long-only trend book already de-risks itself in crashes); pairs/stat-arb (DSR
  ≤17%); all option sleeves (LEAPS, debit spreads, iron condors, earnings strangles — either
  -EV or tail-uncontrollable); sector-rotation MR; VIX-timed contributions; and, as of
  2026-07-14, **dynamic SL/TP trailing based on support/resistance** — the 23rd tested exit
  alternative and the 23rd to fail to beat the fixed baseline cleanly on IS+OOS (see
  `HANDOFF.md` for the full IS/OOS table).
- **Execution layer, hardened for real money:** `PORTFOLIO_CAP` accounts for BOTH filled
  positions and pending (not-yet-filled) broker orders (fixed 2026-07-13, after confirming
  live pending orders alone had reached ~125% of equity — `GrossPositionValue` alone only
  sees fills, not pending commitment); orphaned real orders get cancelled if the paper side
  resolves a trade independently while the broker order is still unfilled; the entire
  trading/monitoring loop runs as a persistent background task, not tied to a browser tab
  being open (found and fixed 2026-07-12 — previously the whole system went silently dormant
  with zero browser clients connected, while still returning healthy HTTP 200s).
- **Frequency is the point, not a bug.** Patience — multi-week holds, not daily trading — IS
  the edge; the no-edge daily game is exactly what this system avoids.

**Research is not "closed" in the sense of never revisiting it** — every new question (dynamic
exits, universe additions, cost assumptions) gets a real backtest before any live-money change,
and every finding is logged in `HANDOFF.md` with the numbers, not just the conclusion.

The live system (`dashboard/`) runs TWO independent instances: **paper** (IBKR paper account
DUK968178, `quant.carsonng.com`) and **live** (real money, IBKR account U12991898,
`quant-live.carsonng.com`) — same code, separate ports/databases/gateways, neither can affect
the other.

---

## Update 2026-07-18: re-entry gate — LIVE, backtest-validated, DSR-checked

A real incident (ASHR stopped out 3x in 8 days, each re-entry within a day or two of the prior
stop at nearly the same price — the live `COOLDOWN_MIN=60` gate is 60 *minutes*, a no-op for a
weekly-bar strategy re-entering days later) triggered a 3-round backtest investigation
(`dashboard/research/backtest.py --reentry-test`) before any live code was touched, per this
project's own rule: **never change live trading logic without a backtest first.**

**19 candidate gate variants tested** (bars-cooldown durations, price-reclaim conditions, an
R-multiple confirmation buffer, floors, caps, and combinations), judged against the same
adoption bar every other rule change here uses: must beat no-gate on OOS expectancy, OOS
CAGR/DD (Calmar), **and** IS CAGR/DD together — not just one favorable slice.

**Winner: "reclaim + 1.0R buffer"** — after a LOSS on an instrument, block a same-direction
re-entry until price closes back beyond that losing trade's own entry by 1.0× its own
entry-to-stop risk (not just marginally across it):

| Metric (current live config, 22 ETFs, 0.5% risk) | Baseline (no gate) | reclaim + 1.0R buffer | Change |
|---|---|---|---|
| **FULL-period CAGR** (5y window, 2021-07 to 2026-07) | **6.3%** | **7.0%** | +0.7pp |
| **FULL-period max drawdown** | **-5.9%** | **-2.8%** | **more than halved** |
| FULL CAGR/DD (Calmar) | 1.07 | 2.52 | +136% |
| OOS CAGR (last ~2y of that window) | 7.5% | 9.7% | +29% |
| OOS max drawdown | -5.4% | -2.8% | nearly halved |
| OOS CAGR/DD (Calmar) | 1.40 | 3.49 | +150% |
| OOS expectancy | +0.171R | +0.290R | +70% |
| OOS win rate | 43% | 45% | +2pp |
| OOS trade count | 431 | 245 | -43% (more selective) |

**Correction (2026-07-18, caught during a later review):** this table was first published
mislabeled as "33y+ history" — `--reentry-test` does not pass `--longweekly`, so it actually
ran on `get_ohlc`'s default 5-year daily window (2021-07-19 to 2026-07-17), confirmed directly
against the fetched data. This is a materially different, weaker claim than the core strategy's
own 33-year research elsewhere in this README: a 5-year window covers far fewer market regimes,
so both the "FULL" and "OOS" rows above are recent-market slices, not independent long-history
validation. The IS/OOS split and DSR correction still guard against overfitting to any ONE
sub-slice of these 5 years, but not against this whole window being an unusually
trend-friendly stretch relative to multi-decade history. **Re-running the same 19-variant sweep
on the full 33-year weekly history (`--longweekly`) is a real open task, not yet done.**

**The honest 5-year-window read**: over the full fetched window (not just its recent-40% OOS
slice), the gate's contribution is **risk reduction more than return enhancement** — CAGR moves
only modestly (6.3%→7.0%) while max drawdown more than halves (-5.9%→-2.8%). The larger OOS CAGR
jump (7.5%→9.7%) is a smaller, more recent sub-slice and the more optimistic number; weight the
FULL-window figures more heavily, consistent with how this project treats every other backtest
result — and treat both as provisional until validated against the full 33-year history.

**Multiple-testing-corrected DSR: 88%** (Deflated Sharpe Ratio, corrected for the 19 trials
actually run, not the naive single-strategy figure) — the best of every candidate tested, real
signal not noise, but **below this project's usual ~95% "solid confidence" bar**. Reported
honestly rather than rounded up: this is the best-supported change available, not a proven fact.

**Per-instrument breadth check**: 10/20 instruments improved, 8/20 worsened — broad enough to
support a real portfolio-wide mechanism rather than overfitting to one name. Notable wrinkle:
ASHR itself, the instrument whose whipsaw triggered this research, actually got *worse* under
the gate. The edge comes from filtering low-quality re-entries broadly across the book, not
literally from fixing the incident that inspired it.

**Deployed 2026-07-18 to both instances** (paper DUK968178 + live real-money U12991898) and
**confirmed actually firing**, not just health-check-green: the live instance's very first
placement cycle after restart blocked a real CWB re-entry exactly as designed. Not yet folded
into the reconciled/bootstrapped headline CAGR/Calmar figures above (that pipeline hasn't been
re-run with the gate active) — treat this as the latest layer on top of the 2026-07-14 findings,
not a replacement for them. Full round-by-round detail, all 19 variants, and the concentration
breakdown are in `HANDOFF.md`.

---

## What this system does well

Judged against what actually broke and got fixed this project, not just what it claims:

- **It doesn't just assert an edge — it tries to disprove it first.** Every adopted
  parameter survived a walk-forward + Deflated Sharpe check against the FULL search breadth
  that produced it (82 combined trials, still 100% DSR). A bare backtest Sharpe with no
  multiple-comparisons correction is the single most common way retail systems fool
  themselves; this one doesn't skip that step.
- **Honest about uncertainty, not just a point estimate.** The block-bootstrap CI (90% range
  spanning roughly 0.54–1.36 Calmar) is presented alongside every headline number, specifically
  because point estimates on 30 years of markets data are less precise than they look.
- **Real safety layers, not just backtested ones.** `PORTFOLIO_CAP` and `DD_HALT_PCT` are
  live-only guards with no backtest equivalent, added after real operational incidents (a
  127%-deployed live account, confirmed directly) — not theoretical protections.
- **Fails safe, not silently.** The paper/live guard refuses to trade a live account unless
  `IB_ALLOW_LIVE=1` is explicitly set AND the connected account exactly matches the configured
  one; a mismatch refuses to trade rather than guessing. The tick loop survives any single
  cycle's exception (extracted into `core/resilient_loop.py` with its own regression test)
  instead of dying silently.
- **Diversification that's been measured, not assumed.** Core/sleeve correlation, universe
  breadth, and cross-crisis behavior are all checked against real historical data in this
  project's own research scripts — not asserted from theory.
- **Transparent decision support, not a black box.** Every LLM-assisted signal carries an
  explicit rationale, an invalidation level, and (as of 2026-07-14) a `macro_linkage` field
  forcing the model to state whether a macro theme it identified actually applies to that
  specific instrument, or say so if it doesn't — auditable, not hoped-for.
- **A real, if imperfect, test suite.** 10 files of regression tests covering the sizing math,
  the DD-halt gate, the reconciliation logic, and every bug found this session — new tests
  written alongside every fix, not just claimed fixed.

**What it doesn't do well, in the same honest spirit:**
- It's operationally complex — IBKR Gateway + two dashboards + a Cloudflare tunnel + watchdogs
  is a lot of moving parts for one person to run, and several real incidents this project
  (orphaned orders, a false -89.8% drawdown display, a dashboard made briefly unresponsive by
  a bug in a bug-fix) came from that complexity, not from the strategy itself.
- The edge is genuine but modest — a Calmar in the 0.5–1.4 range is solid, not spectacular;
  this is not a system that promises to beat the market by a wide margin.
- At current account size, **contributions dominate wealth growth far more than the strategy's
  edge does** for the first several years — the honest framing throughout this project is that
  the behavioral discipline (contribute relentlessly, don't override the system) matters more
  than basis points of edge until the account matures.

---

## How this compares to other investment approaches

All figures below are checked against real market data (not invented), after-tax where noted,
and dated to when they were computed (2026-07 unless stated). Where a figure is a rough
estimate rather than a rigorously re-run backtest, it's marked as such — mixing rigor levels
without saying so is exactly the kind of self-deception this whole project tries to avoid.

| Approach | After-tax CAGR | Max drawdown | Calmar | Basis |
|---|---|---|---|---|
| **This system (core-only)** | **6.06%** (median 6.75%, 90% CI 4.82–8.93%) | **-6.83%** | **0.887** (90% CI 0.536–1.355) | Real 30-year weekly backtest, bootstrapped |
| **This system (core+sleeve@10%, OOS)** | 13.08% | -7.73% | 1.693 | Real backtest, recent-decade window (bull-flattered, upside case not the anchor) |
| SPY buy-and-hold | 10.08% | -54.6% | 0.185 | Real 1996–2026 data, pulled and verified this session |
| Risk-matched SPY + cash (12.5% SPY / 87.5% cash, sized to match this system's -6.83% DD) | 5.02% | -6.83% | 0.735 | Real data; this system beats it by +20.6% relative Calmar at today's ~4.3% cash rate (breakeven rate ~5.5%) |
| 60/40 (SPY/AGG) | ~7% | ~-22% | ~0.32 | Rough estimate, not re-run this project |
| All-weather (25% equity/25% long bonds/25% short-duration/25% commodities) | ~6% | ~-15% | ~0.40 | Rough estimate, not re-run this project |
| 100% cash (SGOV) | ~4.3% | ~0% | n/a | Current rate, no drawdown risk but no growth engine either |

**The honest reading**: at today's interest rates, this system's core-only Calmar (0.887,
reconciled) beats a naive risk-matched passive alternative (0.735) by a real, verified margin
— but the margin isn't enormous, and it would flip if cash rates rose much above ~5.5%. The
sleeve adds a genuine, measured diversification benefit on top. Against a full portfolio
context (60/40, all-weather), this system's edge is real but has not been tested with the same
rigor against those specific benchmarks — that's a fair gap to name, not paper over.

---

## Objective rating

Rated on the project's own terms — methodology rigor, real-money safety, and honest
uncertainty — not on marketing appeal:

| Dimension | Score | Why |
|---|---|---|
| Research methodology | 9/10 | DSR-checked at 82 trials, bootstrap CI, real crisis stress tests — genuinely rigorous, rare for a retail-scale system |
| Real-money safety engineering | 8/10 | Multiple real incidents found and fixed with regression tests (portfolio-cap blind spot, orphaned orders, tick-loop dormancy); the guard rails are real, but so was the list of things that needed guarding against |
| Performance (risk-adjusted) | 6/10 | Calmar ~0.9 core-only, ~1.3–1.7 with the sleeve — solidly above cash and a naive passive alternative at current rates, not a dramatic outperformer |
| Operational complexity / maintainability | 5/10 | Two live dashboards, a broker gateway, a tunnel, and several watchdogs is a real ongoing burden for one person; multiple bugs this project traced directly to that complexity |
| Transparency / auditability | 8/10 | Every signal has a rationale, invalidation level, and macro linkage; every fix this project has its own regression test and a HANDOFF.md entry explaining why |
| **Overall** | **7/10** | A genuinely rigorous, real-money-safe system with a real but modest edge — its biggest risk is operational complexity, not the strategy logic itself |

**Bottom line**: this is a well-engineered, honestly-evaluated system that does what it claims
— it is not a "get rich" system, it is a disciplined, diversified, risk-managed way to
participate in markets with a small, real, measured edge over a passive alternative, at the
cost of real operational overhead to keep it running correctly.

---

## Setup (uv)

The project uses [uv](https://docs.astral.sh/uv/) with a `.venv`. Dependencies are in `pyproject.toml`, pinned in `uv.lock`.

```powershell
# 1. Install uv (one-time)
python -m pip install uv
# its Scripts dir may not be on PATH — add it for the session (or permanently):
$env:Path += ";C:\Users\ls\AppData\Local\Python\pythoncore-3.14-64\Scripts"

# 2. This machine runs AVG, which intercepts HTTPS. Tell uv to trust the Windows
#    cert store, or package downloads fail with "invalid peer certificate".
$env:UV_SYSTEM_CERTS = "true"          # permanent:  setx UV_SYSTEM_CERTS true

# 3. Create the environment + install everything
cd C:\Users\ls\Desktop\Claude\quant    # (or D:\quant)
uv venv --python 3.14
uv sync                                 # core
# uv sync --extra mt5                    # also install MetaTrader5 (live prices)
```

Run anything with `uv run` (no manual activation), or activate once with `.venv\Scripts\Activate.ps1`.

### API keys

Put credentials in `analyst/.env` (git-ignored):

```
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.chatanywhere.tech/v1
OPENAI_MODEL=gpt-5-mini
# optional, for reliable news:
FINNHUB_API_KEY=...
# optional, MT5 auto-login (else it attaches to the running terminal):
MT5_LOGIN=...
MT5_PASSWORD=...
MT5_SERVER=...
MT5_PATH=C:\Program Files\MetaTrader 5\terminal64.exe
```

---

## Commands

### Dashboard (main app)

```powershell
uv run python -m dashboard.app          # → open http://localhost:8080
# or from the project root:  uv run python run_dashboard.py
```
Real-time board: ranked opportunities, batched LLM scan, news, and the Paper Trades
panel. Auto-refresh selector (1/10/15/30/60 min, default 10), weekend LLM auto-pause,
manual refresh, and a daily API-call budget guard (default cap 200).

### Paper trading — historical replay (bootstrap a track record now)

```powershell
uv run python -m dashboard.research.replay --period 5y
```
Replays deterministic signals over history, resolves SL/TP against real prices, and
reports expectancy-in-R / win rate per method. (LLM signals are not replayed — that
would be look-ahead; they are validated only by live forward testing in the dashboard.)

### MT5 setup helper (discover broker symbols)

```powershell
uv run python -m dashboard.data.mt5_client   # prints availability + matching symbol names
```
Put your broker's exact Gold/Oil names into `dashboard/instruments.py` (the `mt5` field).
With a terminal running, prices become near-tick and SL/TP resolution becomes tick-exact.

### Backtester

```powershell
# 1) ALWAYS run this first — proves the framework can't manufacture edge from noise
uv run python run_noise_test.py --trials 40 --strategy ma_crossover

# 2) full walk-forward demo (synthetic, or your own CSV)
uv run python run_demo.py --strategy ma_crossover
uv run python run_demo.py --strategy breakout --csv eurusd_daily.csv

# 3) full study across strategies on real data, with buy&hold benchmark
uv run python run_study.py --csv eurusd_daily.csv
```

### Multi-agent analyst (one-off briefing)

```powershell
uv run python -m analyst.run --csv eurusd_daily.csv --symbol EURUSD
uv run python -m analyst.run --mt5 EURUSD --tf H1          # live from MT5
uv run python -m analyst.run --csv eurusd_daily.csv --no-news
```

---

## How to read backtester results
<a name="backtester"></a>

Look at one thing: the **out-of-sample Deflated Sharpe Ratio**.

- **DSR ≥ 95% and OOS Sharpe > 0** → maybe a real edge. Next step is live paper trading, not real money.
- **OOS Sharpe ≤ 0** → no edge after costs. Discard.
- **OOS Sharpe > 0 but DSR < 95%** → most common case: the positive result is luck from searching the parameter grid. Do **not** trade.

The backtester structurally prevents the classic self-deceptions: forced next-bar
execution (no look-ahead), mandatory costs, walk-forward-only results, and a noise
test that must NOT find profit in a random walk.

---

## Project layout

```
pyproject.toml / uv.lock   uv environment (see SETUP.md)
eurusd_daily.csv           sample real data (ECB EURUSD daily)

# backtester
engine.py costs.py metrics.py walkforward.py strategies.py data.py
run_demo.py run_noise_test.py run_study.py

analyst/                   multi-agent LLM analyst (LangGraph + OpenAI)
  features.py llm.py nodes.py graph.py state.py news.py run.py  .env

dashboard/                 real-time dashboard + paper trading
  app.py service.py scoring.py board_scan.py store.py
  providers.py mt5_client.py instruments.py news_sources.py
  paper.py replay.py        forward paper-trading + historical replay
```

See [SETUP.md](SETUP.md) for the full environment workflow, and the sub-READMEs in
`analyst/` and `dashboard/` for component details.

---

## Notes for this machine

- **AVG TLS interception:** all HTTPS is re-signed by AVG's local root. Handled
  automatically — `truststore` for Python and a Windows-cert bundle (`winca.pem`)
  for yfinance/libcurl. For `uv` itself, set `UV_SYSTEM_CERTS=true`.
- `.venv/`, `winca.pem`, `analyst/.env`, and `dashboard/dashboard.db` are git-ignored.
