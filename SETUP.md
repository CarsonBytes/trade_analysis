# Setup (uv)

This project uses [uv](https://docs.astral.sh/uv/) with a `.venv` virtual
environment. All dependencies are declared in `pyproject.toml` and pinned in
`uv.lock`.

## One-time

```powershell
# 1. Install uv (adds uv.exe to the Python Scripts dir)
python -m pip install uv

# That Scripts dir is not on PATH by default. Add it for this session:
$env:Path += ";C:\Users\ls\AppData\Local\Python\pythoncore-3.14-64\Scripts"
# (or add it permanently via System > Environment Variables)

# 2. AVG intercepts HTTPS on this machine, so tell uv to trust the Windows
#    certificate store (where AVG's root lives). Otherwise downloads fail with
#    "invalid peer certificate: UnknownIssuer".
$env:UV_SYSTEM_CERTS = "true"
# To make it permanent:  setx UV_SYSTEM_CERTS true
```

## Create the environment

```powershell
cd C:\Users\ls\Desktop\Claude\quant
uv venv --python 3.14
uv sync                 # core dependencies
# uv sync --extra mt5   # also install MetaTrader5 (live broker prices)
```

`uv sync` creates `.venv\` and installs the exact locked versions.

## Configure the LLM key

```powershell
# analyst/.env  (git-ignored). Example values already used:
#   OPENAI_API_KEY=sk-...
#   OPENAI_BASE_URL=https://api.chatanywhere.tech/v1
#   OPENAI_MODEL=gpt-5-mini
# Optional, for reliable news:
#   FINNHUB_API_KEY=...
```

## Run things (inside the venv)

`uv run` auto-uses `.venv` — no manual activation needed:

```powershell
uv run python -m dashboard.app        # dashboard -> http://localhost:8080
uv run python run_demo.py             # backtester demo
uv run python run_noise_test.py       # backtester self-check on noise
uv run python -m analyst.run --csv eurusd_daily.csv --symbol EURUSD   # 1 analysis
```

Or activate the venv once and use `python` directly:

```powershell
.venv\Scripts\Activate.ps1
python -m dashboard.app
```

## Updating dependencies

```powershell
uv add <package>        # add a dependency (updates pyproject.toml + uv.lock)
uv sync                 # re-sync after editing pyproject.toml
uv lock --upgrade       # bump locked versions
```
