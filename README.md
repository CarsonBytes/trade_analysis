# Quantitative Trade-Analysis Platform

A research platform for Gold, Oil and FX with three parts:

1. **Anti-self-deception backtester** — proves whether a strategy idea actually has an edge (walk-forward, deflated Sharpe, noise test). See [backtester details](#backtester) below.
2. **Multi-agent analyst** (`analyst/`) — deterministic facts feed LLM agents (regime / technical / sentiment) → a head-trader decision → deterministic risk gate. Decision support, not auto-execution. See [analyst/README.md](analyst/README.md).
3. **Real-time dashboard + paper trading** (`dashboard/`) — NiceGUI board for Gold/Oil/FX, ranks the most obvious trends, runs a batched LLM scan, and forward-tests SL/TP setups to track success rate. See [dashboard/README.md](dashboard/README.md).

> **Honest framing:** this measures whether ideas work; it does not manufacture an edge. Everything is decision support — it never places a real trade.

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
uv run python -m dashboard.replay --period 5y
```
Replays deterministic signals over history, resolves SL/TP against real prices, and
reports expectancy-in-R / win rate per method. (LLM signals are not replayed — that
would be look-ahead; they are validated only by live forward testing in the dashboard.)

### MT5 setup helper (discover broker symbols)

```powershell
uv run python -m dashboard.mt5_client   # prints availability + matching symbol names
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
