"""Bond-instrument exclusion test -- ADDED 2026-08-11, user-requested re-justification of
HYG/CWB/HYD (user observed these 3 sitting PENDING/unfilled for an unusually long time and
asked whether they're worth keeping). All three are EXISTING, already-adopted universe
members (not new candidates) -- HYG was an original "KEEP" from the base screen, CWB and HYD
were both promoted via isolation-tested batch screens (CWB +1.0pp OOS CAGR, HYD +0.6pp OOS
CAGR "zero extra DD" -- see instruments.py's own inline history). This re-tests whether that
original justification still holds against the CURRENT full dataset (more history accrued
since those screens ran), by removing each (and all three together) from the live 22-
instrument universe and comparing Full/OOS Calmar -- same methodology as every other
universe-composition test this project has run.

Run: uv run python -u -m dashboard.research.bond_exclusion_test
"""
from __future__ import annotations
import os
os.environ.setdefault("BROKER", "ib")
os.environ.setdefault("UNIVERSE", "etf")

import yfinance as yf

import dashboard.research.backtest as bt
from dashboard.instruments import active_universe

RISK = 0.01
bt.CASH_YIELD = None
bt.POS_CAP = 0.25
bt.PORTFOLIO_CAP = 1.0

EXCLUDE_KEYS = {"HYG", "CWB", "HYD"}


def _fetch_all() -> dict[str, list[dict]]:
    out = {}
    for inst in active_universe():
        df = yf.download(inst.yf, period="max", interval="1wk", progress=False, auto_adjust=True)
        if df is None or len(df) == 0:
            print(f"  {inst.key}: NO DATA, skipped")
            continue
        if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
            df.columns = df.columns.get_level_values(0)
        df = df[["Open", "High", "Low", "Close"]].copy()
        df.columns = ["open", "high", "low", "close"]
        df = df.dropna()
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        if len(df) < 220:
            print(f"  {inst.key}: only {len(df)} bars, skipped")
            continue
        out[inst.key] = bt._signals(df, inst.key)
    return out


print(f"Fetching full live universe ({len(active_universe())} instruments)...")
by_key = _fetch_all()
all_cands = [c for v in by_key.values() for c in v]
print(f"  {len(all_cands)} total signals across {len(by_key)} instruments\n")

for k in EXCLUDE_KEYS:
    n = len(by_key.get(k, []))
    print(f"  {k}: {n} candidate signals in the full history")
print()

years = bt._span_years(all_cands)
_start = min(c["entry_date"] for c in all_cands)
_end = max(c["exit_date"] for c in all_cands)
cut = _start + (_end - _start) * 0.6


def _row(label: str, cands: list[dict]) -> None:
    if not cands:
        print(f"  {label:<40} NO CANDIDATES")
        return
    yrs = bt._span_years(cands)
    eq, real = bt._portfolio(cands, RISK)
    m = bt._metrics(eq, real, yrs)
    calmar = m["cagr"] / abs(m["maxdd"]) if m["maxdd"] else 0.0
    print(f"  {label:<40} n={len(real):<5} CAGR={m['cagr']*100:+7.2f}%  "
         f"maxDD={m['maxdd']*100:7.2f}%  Calmar={calmar:6.3f}")


def _isolation_stats(key: str) -> None:
    from dashboard.core import paper
    cands = by_key.get(key, [])
    rs = [c["r"] for c in cands]
    s = paper.stats(rs)
    print(f"  {key:<6} n={s['n']:<5} win%={s['win_rate']*100:5.1f}  expR={s['expectancy_R']:+.3f}")


print("="*100)
print("STAGE 1: isolated per-instrument stats (all history)")
print("="*100)
for k in EXCLUDE_KEYS:
    _isolation_stats(k)

print(f"\n{'='*100}\nFULL HISTORY\n{'='*100}")
_row("Baseline (full 22-instrument universe)", all_cands)
for k in EXCLUDE_KEYS:
    ex_cands = [c for c in all_cands if c["key"] != k]
    _row(f"Without {k}", ex_cands)
all_three = [c for c in all_cands if c["key"] not in EXCLUDE_KEYS]
_row("Without HYG+CWB+HYD (all three)", all_three)

oos_all = [c for c in all_cands if c["entry_date"] > cut]
print(f"\n{'='*100}\nOOS (last 40% of the baseline's own date range)\n{'='*100}")
_row("Baseline (full 22-instrument universe)", oos_all)
for k in EXCLUDE_KEYS:
    ex_oos = [c for c in oos_all if c["key"] != k]
    _row(f"Without {k}", ex_oos)
oos_three = [c for c in oos_all if c["key"] not in EXCLUDE_KEYS]
_row("Without HYG+CWB+HYD (all three)", oos_three)
