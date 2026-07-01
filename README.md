# Quantitative Trade-Analysis Platform

A research + paper-trading platform for a diversified multi-asset ETF book, with three parts:

1. **Anti-self-deception backtester** — proves whether a strategy idea actually has an edge (walk-forward, deflated Sharpe, noise test). See [backtester details](#backtester) below.
2. **Multi-agent analyst** (`analyst/`) — deterministic facts feed LLM agents (regime / technical / sentiment) → a head-trader decision → deterministic risk gate. Decision support, not auto-execution. See [analyst/README.md](analyst/README.md).
3. **Real-time dashboard + paper trading** (`dashboard/`) — NiceGUI board that scores a 17-ETF weekly-trend universe, mirrors signals to an **IBKR paper account** (`BROKER=ib UNIVERSE=etf`), auto-manages idle cash (USD → SGOV), and forward-tests fills against the backtest. See [dashboard/README.md](dashboard/README.md).

> **Honest framing:** this measures whether ideas work; it does not manufacture an edge. It runs on a **paper** account (hard-guarded) and never moves real money.

---

## Key research findings (as of 2026-06-30)

Exhaustive out-of-sample, deflated-Sharpe-penalised study. The platform pivoted MT5 spot →
IBKR futures → **17 ETFs** (ETFs trade in *shares*, so 0.5% risk is expressible on a small
account, unlike futures). ~25+ ideas tested; the honest conclusions:

- **Exactly ONE edge: weekly time-series momentum (TSMOM) across many uncorrelated ETFs.**
  Long-only, 5-week hold, fixed ATR-stop + 3:1 RR, equal-risk sizing, 0.5% risk. The single
  lever that ever helped is **breadth** — adding uncorrelated positive-edge markets (10→16
  ETFs was +2.8% OOS, the big win); the book's avg pairwise correlation is **0.26**, so it is
  genuinely diversified, not leveraged beta.
- **Universe (17):** metals GLD/SLV/CPER · equity SPY/QQQ/DIA/IWM · rates IEF/TLT/SHY ·
  credit HYG · inflation TIP · intl EFA/EEM · commodity DBC · REIT VNQ · preferred PFF.
- **Performance (33y full history, the anchor):** strategy-only **+4.4% CAGR / −11% DD**;
  with idle cash swept to SGOV at current rates **~+7.0% CAGR / −9.7% DD / Sharpe ~1.22**.
  Recent ~13y OOS is bull-flattered (~+10–12%) — do **not** plan around it. Risk is a pure
  leverage dial (CAGR/DD ratio ~constant): 0.25%→~−5% DD, 0.5%→~−10%, 1%→~−20%.
- **One positive-EV satellite: a "panic-MR" dip-buy sleeve.** Buy SPY/QQQ/XLK on a VIX-spike
  oversold (>2.5% below 20-day MA + VIX↑>15%/5d + RSI<35 + **ADX>20**), exit at the 5-day MA;
  size 0.5%, up to 1% when VIX>30 (hard cap 1%). +1.21%/trade, 75% win, holds OOS. Blended
  with the core it adds **~+1.5–2pp CAGR at ~flat drawdown → ~+8.7% / −10% / Sharpe ~1.25**.
  (Deferred until the account is larger; at a small size contributions dwarf it.)
- **Everything else REJECTED, with data (DSR/OOS discipline):** daily technicals (no edge,
  DSR 53–58%); vol-targeting (pure leverage — DD *tripled* to −29% at 12% target); monthly
  rebalance (Sharpe 1.22→0.70); pullback/dynamic/staged exits; cross-sectional momentum &
  relative-strength filters (breadth loss halves CAGR); regime overlays — SPY-MA, VIX-ladder,
  and a correlation penalty (all **redundant** — a long-only trend book de-risks itself by
  exiting in crashes); **pairs / stat-arb** (even proper cointegration/OOS: DSR ≤17%, negative
  after cost — the "best" pairs are same-index wrappers); **all option sleeves** (LEAPS, weekly
  debit spreads, iron condors, single-name earnings strangles, 0DTE pin — either −EV, tail-
  uncontrollable, or unaffordable/unbacktestable); sector-rotation MR (−EV vs equal-weight);
  VIX-timed contributions (lose to plain DCA on cash-drag).
- **Execution layer:** costs already modeled (~1 bp round-trip on liquid ETFs); a market→limit
  switch is worth ~+0.1–0.2%/yr at most. Idle cash: convert HKD→USD (~3.1%) and sweep 60% to
  **SGOV** (~T-bill yield), keeping a 40% buffer — free, slightly *reduces* DD.
- **Frequency is the point, not a bug.** ~32 core round-trips/yr (~3.3-week holds) → ~1–2
  fills/week. Frequent trading = the no-edge daily game; patience IS the alpha.

**Research is closed** — the price-technical + option + stat-arb search space is exhausted;
the remaining edge is behavioral (contribute relentlessly, stay invested, don't override).

The live system (`dashboard/`) runs the core config on IBKR paper: 17-ETF weekly TSMOM,
0.5% risk, `CASH_USD`/`CASH_SWEEP` on, with a SGOV-first manual-withdrawal helper in the UI.

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
