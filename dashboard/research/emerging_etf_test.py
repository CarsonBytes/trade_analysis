"""Emerging-ETF addition test -- ADDED 2026-08-11, user-requested: URNJ, ARKX, NLR, PHO, CHAT.

Data-availability check done FIRST (yfinance, direct): URNJ (185 weekly bars, ~3.5y) and CHAT
(170 bars, ~3.2y) both fall BELOW this project's own 220-bar minimum used in every research
script this session -- excluded from the portfolio test, not silently included with a
too-thin sample. ARKX (281 bars, ~5.4y) clears the bar but is still shorter than the ~30y
standard -- disclosed, not hidden. NLR (992 bars, ~19y) and PHO (1080 bars, ~20.7y) have ample
history.

Two-stage screen matching this project's own established methodology: (1) cheap isolation
stats per candidate before any portfolio simulation: (2) for anything with positive expR and
n>=15, the full current-universe Full/OOS Calmar comparison.

Run: uv run python -u -m dashboard.research.emerging_etf_test
"""
from __future__ import annotations
import os
os.environ.setdefault("BROKER", "ib")
os.environ.setdefault("UNIVERSE", "etf")

import yfinance as yf

import dashboard.research.backtest as bt
from dashboard.core import paper
from dashboard import instruments as instruments_mod
from dashboard.instruments import active_universe, Instrument

RISK = 0.01
bt.CASH_YIELD = None
bt.POS_CAP = 0.25
bt.PORTFOLIO_CAP = 1.0
paper.WEEKLY_TREND_CLASSES = set()   # no whitelist -- new candidate classes not yet adopted,
                                      # same convention as every prior --etf-screenN test

CANDIDATES = [
    Instrument("URNJ_CAND", "Junior Uranium Miners", "URNJ", "", "uranium"),
    Instrument("ARKX_CAND", "Space Exploration",     "ARKX", "", "space_theme"),
    Instrument("NLR_CAND",  "Uranium & Nuclear",     "NLR",  "", "uranium"),
    Instrument("PHO_CAND",  "Water Resources",       "PHO",  "", "water"),
    Instrument("CHAT_CAND", "Generative AI",         "CHAT", "", "ai_theme"),
]
for _inst in CANDIDATES:
    instruments_mod.BY_KEY[_inst.key] = _inst

MIN_BARS = 220


def _fetch(inst) -> tuple[list[dict], int]:
    df = yf.download(inst.yf, period="max", interval="1wk", progress=False, auto_adjust=True)
    if df is None or len(df) == 0:
        return [], 0
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close"]].copy()
    df.columns = ["open", "high", "low", "close"]
    df = df.dropna()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    n = len(df)
    if n < MIN_BARS:
        return [], n
    return bt._signals(df, inst.key), n


print("Fetching candidates...")
by_key: dict[str, list[dict]] = {}
excluded: list[str] = []
for inst in CANDIDATES:
    cands, n = _fetch(inst)
    if n == 0:
        print(f"  {inst.key}: NO DATA")
        excluded.append(inst.key)
        continue
    if not cands and n < MIN_BARS:
        print(f"  {inst.key}: only {n} bars (<{MIN_BARS} minimum) -- EXCLUDED from portfolio test")
        excluded.append(inst.key)
        continue
    by_key[inst.key] = cands
    print(f"  {inst.key}: {n} bars, {len(cands)} candidate signals")
print()

print("="*100)
print("STAGE 1: isolated per-instrument stats (candidates that cleared the data bar)")
print("="*100)
for inst in CANDIDATES:
    if inst.key not in by_key:
        continue
    rs = [c["r"] for c in by_key[inst.key]]
    s = paper.stats(rs)
    verdict = ("ADVANCE to stage 2" if s["expectancy_R"] > 0 and s["n"] >= 15
              else "reject (negative expR)" if s["expectancy_R"] <= 0
              else "reject (n too small to trust)")
    print(f"  {inst.key:<10} n={s['n']:<5} win%={s['win_rate']*100:5.1f}  "
         f"expR={s['expectancy_R']:+.3f}  {verdict}")

advancing = {k: v for k, v in by_key.items()
            if v and paper.stats([c["r"] for c in v])["expectancy_R"] > 0
            and paper.stats([c["r"] for c in v])["n"] >= 15}

print(f"\n{'='*100}\nSTAGE 2: full portfolio Calmar/CAGR/DD comparison (only for advancing candidates)\n{'='*100}")
print("Fetching current live universe (21 instruments)...")
base_cands = []
for inst in active_universe():
    df = yf.download(inst.yf, period="max", interval="1wk", progress=False, auto_adjust=True)
    if df is None or len(df) == 0:
        continue
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close"]].copy()
    df.columns = ["open", "high", "low", "close"]
    df = df.dropna()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    if len(df) < MIN_BARS:
        continue
    base_cands += bt._signals(df, inst.key)
print(f"  {len(base_cands)} signals\n")

years = bt._span_years(base_cands)
_start = min(c["entry_date"] for c in base_cands)
_end = max(c["exit_date"] for c in base_cands)
cut = _start + (_end - _start) * 0.6


def _row(label: str, cands: list[dict]) -> None:
    if not cands:
        print(f"  {label:<45} NO CANDIDATES")
        return
    yrs = bt._span_years(cands)
    eq, real = bt._portfolio(cands, RISK)
    m = bt._metrics(eq, real, yrs)
    calmar = m["cagr"] / abs(m["maxdd"]) if m["maxdd"] else 0.0
    print(f"  {label:<45} n={len(real):<5} CAGR={m['cagr']*100:+7.2f}%  "
         f"maxDD={m['maxdd']*100:7.2f}%  Calmar={calmar:6.3f}")


print(f"{'='*100}\nFULL HISTORY\n{'='*100}")
_row("Baseline (current 21-instrument universe)", base_cands)
if advancing:
    for k, v in advancing.items():
        _row(f"+ {k}", base_cands + v)
    _row("+ ALL advancing candidates combined", base_cands + [c for v in advancing.values() for c in v])
else:
    print("  (no candidate advanced past Stage 1)")

oos_base = [c for c in base_cands if c["entry_date"] > cut]
print(f"\n{'='*100}\nOOS (last 40% of the baseline's own date range)\n{'='*100}")
_row("Baseline (current 21-instrument universe)", oos_base)
if advancing:
    for k, v in advancing.items():
        oos_v = [c for c in v if c["entry_date"] > cut]
        _row(f"+ {k}", oos_base + oos_v)
    oos_all = [c for v in advancing.values() for c in v if c["entry_date"] > cut]
    _row("+ ALL advancing candidates combined", oos_base + oos_all)

if excluded:
    print(f"\nExcluded from testing entirely (insufficient history, <{MIN_BARS} weekly bars): "
         f"{', '.join(excluded)}")
print("\nNOTE: candidate history is shorter than the ~20-30y baseline in every case that "
     "advanced -- read the blended figures as directional, same caveat as every other "
     "universe-addition test this project has run.")
