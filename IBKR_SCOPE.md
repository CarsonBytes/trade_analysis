# Module scope — IBKR provider + contract-roll + sizing-by-specs

**Status:** design/scope (no code yet). Last updated 2026-06-21.
**Goal:** replace the MT5 data+execution layer with an IBKR futures layer, *without
touching* the strategy/research code (scoring, backtest, gates, retrospective). Those
port on top unchanged because they only ever see pandas Series/DataFrames and a
broker-agnostic `executor` API.

---

## 0. The seam we're building against (why this is clean)

The codebase already isolates the broker behind two interfaces. Everything above them
is broker-agnostic:

- **Data seam — `providers.py`**: `get_history` (weekly closes → Series), `get_ohlc`
  (OHLC bars → DataFrame), `get_live_price` (price/spread). Strategy code consumes
  pandas only; it has never imported `mt5_client`.
- **Execution seam — `executor.py`**: `mirror_new()`, `sync_closures()`,
  `live_positions()`, `reconcile()`, `flatten_foreign()`, `is_demo()`. `service.py`
  calls these by name each refresh cycle. `paper.py` owns the journal; the executor
  mirrors the journal to the broker and resolves trades from broker truth.

So the IBKR work is **three modules** that re-satisfy those seams for futures, plus a
**specs/roll** layer that MT5 didn't need (CFDs don't expire; futures do).

```
strategy / gates / backtest / retrospective      (UNCHANGED)
        │ pandas                       │ executor API
   ┌────▼─────────┐              ┌──────▼───────────┐
   │ providers.py │              │   executor.py    │   ← thin dispatch layer
   │  (dispatch)  │              │   (dispatch)     │     picks MT5 | IBKR by config
   └────┬─────────┘              └──────┬───────────┘
        │                               │
   ┌────▼─────────────────────────────────────────────┐
   │  ib_client.py   contracts.py(specs+roll)  ib_exec │   ← NEW
   └───────────────────────────────────────────────────┘
```

---

## 1. New files

### `dashboard/ib_client.py` — connection + raw data (mirror of `mt5_client.py`)
Persistent client over **`ib_async`** (the maintained fork of `ib_insync`). Same
degrade-gracefully contract as `mt5_client`: every function returns `None`/`False` if
TWS/IB Gateway isn't running, so the app keeps falling back to yfinance.

- `is_available() -> bool` — connect once, reuse; serialise behind a lock (ib_async is
  asyncio + not thread-safe across our threadpool).
- `get_rates(contract, timeframe, n) -> DataFrame|None` — `reqHistoricalData`. Map our
  `"W1"/"D1"/"M1"` to IB `barSizeSetting`/`durationStr`. Return the SAME shape
  `mt5_client.get_rates` does: `DatetimeIndex(UTC)` + `[open,high,low,close]` float.
- `get_tick(contract) -> {bid,ask,mid,spread,time,age_sec}|None` — `reqMktData`
  (snapshot or streaming). Same dict keys as MT5 so `get_live_price` is unchanged.
- `account_summary()` / `is_paper()` — IB's paper account guard (see §4).
- `diagnose()` CLI — package present? gateway up? logged in? which account? — mirrors
  `mt5_client.diagnose()`.

**Connection facts to encode:** TWS vs IB Gateway port (7497 paper / 7496 live for TWS;
4002/4001 for Gateway), `clientId` (pick a fixed one, e.g. 7; the dashboard is one
client), and `readonly=False` only for the exec client.

### `dashboard/contracts.py` — instrument specs + roll logic (THE NEW PART)
This is the engineering MT5 never required. Two responsibilities:

**(a) Per-contract specs** — a static table (dataclass `FutureSpec`) carrying everything
sizing needs, keyed by our stable instrument `key`:
```
symbol, exchange, currency, multiplier ($/point), tick_size, tick_value,
sec_type="FUT" (or "CONTFUT" for history), months (e.g. "HMUZ"), roll_offset_days
```
Example: `MES` → exchange GLOBEX, multiplier $5/point, tick 0.25, tick_value $1.25.
`GC` → NYMEX, $100/point. `ZN` → CBOT, $1000/point, tick 1/64. These are NOT derivable
from the broker generically the way MT5's `order_calc_profit` was — they must be a
curated table (with a runtime cross-check against `reqContractDetails`).

**(b) Front-month resolution + roll** —
- `front_contract(spec, asof) -> Contract` — pick the active month. Use IB
  `reqContractDetails` on a continuous/expiry query, choose the nearest expiry that is
  `> asof + roll_offset_days`. Roll a few days *before* expiry (volume/OI migrates
  early). Default: roll on the **business day N days before expiry** (configurable per
  spec; rates/grains differ from indices).
- `continuous_history(spec, n) -> DataFrame` — for *signals/backtest* we need a
  continuous back-adjusted series, NOT a single expiring contract. Use IB `CONTFUT`
  historical bars (back-adjusted) so the weekly MA/RSI/ATR don't see roll gaps. Keep
  the live tradable `Contract` separate from the history series. **This split is the #1
  correctness trap** — signals on continuous data, orders on the front month.
- `needs_roll(open_position, asof) -> bool` — for an OPEN demo position whose contract
  is within the roll window: close front, open next. (Weekly hold ~7wk can straddle a
  roll for quarterly contracts — must handle.)

### `dashboard/ib_exec.py` — paper execution (mirror of `executor.py`)
Same public surface `service.py` already calls, futures semantics underneath:
- `mirror_new()` — for each new OPEN paper trade of the live variant: resolve front
  contract, **size by specs** (§3), place a market order with attached
  stop+limit (IB bracket: parent + SL + TP as a one-cancels-all group). Record
  `paper_id → (conId, localSymbol, qty, risk_money)` in a mirror table (reuse the
  `mt5_mirror` pattern → `ib_mirror`).
- `sync_closures()` — resolve paper trades from IB execution/`fills` (broker truth),
  same as `_resolve_from_broker`. **Plus roll handling**: if `needs_roll`, roll the
  position instead of just closing.
- `live_positions()` / `reconcile()` — join `ib.fills()`/`reqExecutions` back to the
  journal; realized R from `commissionReport` + PnL ÷ risk_money. Same output dict
  shape the UI/retrospective expect.
- **Paper guard** (§4) — the non-negotiable safety analog of `is_demo()`.

---

## 2. Files that change (small, additive)

- `instruments.py` — add the futures universe. Either extend `Instrument` with an
  optional `ib_key` or (cleaner) keep `Instrument` as-is and let `contracts.FutureSpec`
  carry IB fields, joined by `key`. Add the ~15–25 contracts from HANDOFF §"Next phase"
  (ES/NQ/YM/RTY, GC/SI/HG, CL/NG, **ZN/ZB/ZF**, ZC/ZW/ZS, KC/SB/CT, 6E/6J/6A — micros
  where they exist: MES/MNQ/MGC/MCL…). Tag each with `asset_class` so
  `WEEKLY_TREND_CLASSES` and `DECORRELATE` keep working untouched.
- `providers.py` — turn the three `get_*` into a **dispatch on a `BROKER` config**
  (`"mt5"` | `"ib"`). MT5 path stays byte-identical; IB path calls `contracts` +
  `ib_client`. `get_history` must return continuous back-adjusted weekly; `get_ohlc`
  returns the front contract's daily for resolution.
- `executor.py` / `service.py` — dispatch to `ib_exec` when `BROKER=="ib"`. Keep MT5
  the default until IB is proven, so nothing in the live system regresses.
- `pyproject.toml` / `requirements.txt` — add `ib_async` as an extra (`--extra ib`),
  parallel to the existing `--extra mt5`.
- `analyst/.env` — `IB_HOST`, `IB_PORT`, `IB_CLIENT_ID`, `IB_ACCOUNT` (paper acct id).

---

## 3. Sizing-by-specs (replaces broker `order_calc_profit`)

MT5 gave us `order_calc_profit` to convert an SL distance to money. IB has no such
helper — we compute it from the spec, which is *more* honest and is the same math the
backtest should use:

```
risk_money   = account_equity × RISK_PER_TRADE          # 0.5%, from paper.py
stop_points  = SL_ATR_MULT × ATR                         # price distance, paper.py
risk_per_contract = stop_points × multiplier             # $ lost if SL hit, 1 contract
contracts    = floor(risk_money / risk_per_contract)     # whole contracts
```
- **Micros for precision**: on a modest paper account, `contracts` rounds to 0 for big
  contracts (ZB, NQ). Prefer the micro (`MNQ` $2/pt vs `NQ` $20/pt) so 0.5% risk is
  expressible. Selection rule: pick the contract whose `risk_per_contract ≤ risk_money`
  with the finest granularity; if even 1 micro exceeds risk_money, **skip** (don't
  oversize). Log the skip.
- **FX-quoted / non-USD** contracts (6E, DAX-in-EUR): multiply by the contract currency
  and convert to account currency (USD) via a spot rate from `ib_client`. MT5 hid this;
  here it's explicit.
- This sizing math should be a **pure function** `size_contracts(spec, equity, atr,
  risk_pct) -> int` reused by BOTH `ib_exec` and `backtest.py`, so paper and live agree.

---

## 4. Safety guard (non-negotiable, mirrors the demo guard)

`executor.py`'s `is_demo()` refuses to trade a non-demo MT5 account. IB analog:
- `ib_exec._guard()` returns the client **only if** the connected account id starts with
  the paper prefix (`DU…` for IB paper; live is `U…`) **and** matches `IB_ACCOUNT` from
  env. Hard-refuse otherwise, log a warning, place nothing. Non-configurable — a flag
  flip must never reach a live account.
- Belt-and-suspenders: connect the exec client to the **paper port only** (7497/4002)
  and assert the account prefix; both must agree.

---

## 5. Build order (smallest provable increments)

1. **`ib_client.py` + `diagnose()`** — connect to paper gateway, pull one contract's
   weekly bars, print them. Proves connectivity + data shape. (No strategy yet.)
2. **`contracts.py` specs table + `front_contract`/`continuous_history`** — verify the
   continuous series matches yfinance `GC=F`/`ES=F` weekly within roll noise. This
   de-risks the #1 trap before any order logic.
3. **`size_contracts` pure fn + unit test** against a few known specs (1 MES at given
   ATR → expected contracts). Wire into `backtest.py` so the *research* numbers use real
   contract sizing.
4. **`providers.py` dispatch** — flip `BROKER="ib"` and confirm the dashboard scores
   signals off IB continuous data identically to MT5/yfinance.
5. **`ib_exec.mirror_new` + paper guard** — place ONE bracket order on the paper account
   for a live signal; confirm fill + SL/TP attached. Then `sync_closures` + roll.
6. **`reconcile`/`live_positions`** — wire into the existing retrospective UI.

Each step is independently verifiable and leaves MT5 as the working default
(`BROKER="mt5"`) until step 4+ is trusted.

---

## 6. Known traps (write tests/guards for these)

- **Continuous-vs-front split** (§1b) — signals on back-adjusted continuous, orders on
  the dated front month. Mixing them silently corrupts both. Highest-priority test.
- **Roll during an open ~7wk hold** — quarterly contracts (ES/GC) will expire mid-hold;
  `needs_roll` must close+reopen and carry the R accounting across the roll.
- **Market-data subscriptions** — IB paper still needs (paid) real-time data per
  exchange; without it `reqMktData` returns delayed/empty. `get_history` (delayed bars)
  is usually fine; live ticks may not be. Detect and degrade, don't crash.
- **clientId collisions** — dashboard + any manual TWS API session must use distinct
  `clientId`s or IB drops the connection.
- **Whole-contract granularity** — unlike MT5 fractional lots, you can't size 0.37
  contracts. The `floor` + micro-selection + skip-if-too-big rule (§3) is load-bearing
  for the low-risk mandate.
- **asyncio ↔ threadpool** — ib_async is async; the dashboard is threaded. Run a single
  ib_async event loop (its `util.startLoop`/dedicated thread) and marshal calls, or use
  the sync wrappers it provides, all behind one lock — same discipline as `mt5_client._LOCK`.

---

## 7. What explicitly does NOT change
Scoring, gates (`confidence_model`/`win_model`), `backtest.py` strategy logic,
`retrospective.py` KPI math, the weekly-trend decision itself. They consume pandas and
the `executor`/journal API; both seams are preserved. Order-flow research stays a
*later* avenue (now feasible on real futures volume), not part of this module.
</content>
</invoke>
