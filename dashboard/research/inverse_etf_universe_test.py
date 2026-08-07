"""Inverse-ETF universe addition test -- ADDED 2026-08-06, user-requested critique
verification ("should this system add short exposure"). Tests the ONE proposal from the
critique that survives scrutiny (Option A: add inverse ETFs as new, ordinary long-only
universe members -- buying SH when SH itself trends up IS economically shorting SPY, with
bounded risk, zero borrow cost, and the exact same 1.5xATR/3:1RR/1% engine unmodified) --
explicitly the freeze policy's allowed "new universe/instrument addition" category, not a
core-parameter change.

IMPORTANT premise correction found before building this: the critique's own list (SH, PSQ,
TBT, GLL) mixes two structurally different products without flagging it. Verified directly
(2y daily beta vs the named underlying): SH beta=-0.97, PSQ beta=-0.99 (genuine 1x inverse) --
but TBT beta=-1.99, GLL beta=-1.98 (2x LEVERAGED inverse). This project already tested and
rejected 2x/3x leveraged sleeve variants for the exact reason leveraged/inverse products decay
under multi-week holds ("textbook decay... Calmar worse than the 1x sleeve at every weight,
IS and OOS, no exceptions" -- README). Tested here in 2 separate tiers so a leveraged-decay
effect (if present) doesn't get laundered into the "just add inverse ETFs" headline result.

ALSO disclosed: none of these 4 products have this project's usual ~30-32y history -- SH/PSQ
launched 2006 (~20y), TBT launched May 2008 (~18y, mid-GFC, no pre-crisis history at all),
GLL launched Dec 2008 (~18y). The "full history" comparison below is NOT apples-to-apples
across tiers -- baseline uses the full ~30y book, the +SH/PSQ and +TBT/GLL tiers necessarily
blend in candidates only from the newer instruments' shorter available history.

Run: uv run python -u -m dashboard.research.inverse_etf_universe_test
"""
from __future__ import annotations
import os
os.environ.setdefault("BROKER", "ib")
os.environ.setdefault("UNIVERSE", "etf")

import pandas as pd
import yfinance as yf

import dashboard.research.backtest as bt
from dashboard import instruments as instruments_mod
from dashboard.instruments import active_universe, Instrument

RISK = 0.01
bt.CASH_YIELD = None
bt.POS_CAP = 0.25
bt.PORTFOLIO_CAP = 1.0

# asset_class reuses the EXISTING WEEKLY_TREND_CLASSES vocabulary (paper.py) matching each
# product's own underlying -- an invented "inverse_*" class would be silently filtered out
# by _signals()'s own class check (confirmed the hard way: first version of this script used
# invented class names and produced a NoneType crash via active_by_key(), then would have
# silently produced zero candidates once that lookup was fixed, without the class matching).
TIER_1X = [
    Instrument("SH_INV", "Short S&P 500 (1x)", "SH", "", "index"),
    Instrument("PSQ_INV", "Short Nasdaq 100 (1x)", "PSQ", "", "index"),
]
TIER_2X = [
    Instrument("TBT_INV", "UltraShort 20+Y Treasury (2x)", "TBT", "", "rate"),
    Instrument("GLL_INV", "UltraShort Gold (2x)", "GLL", "", "metal"),
]
# register into instruments.BY_KEY so active_by_key() (called inside bt._signals()) resolves
# these temporary test instruments -- BY_KEY is a plain dict built once at import time, safe
# to extend at runtime without touching the real live/paper universe config on disk.
for _inst in TIER_1X + TIER_2X:
    instruments_mod.BY_KEY[_inst.key] = _inst


def _fetch_candidates(instruments) -> tuple[list[dict], dict]:
    cands = []
    spans = {}
    for inst in instruments:
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
            print(f"  {inst.key}: only {len(df)} weekly bars (<220 min), skipped")
            continue
        spans[inst.key] = (df.index[0], df.index[-1])
        cands += bt._signals(df, inst.key)
    return cands, spans


print("Fetching baseline 22-ETF universe...")
base_cands, _ = _fetch_candidates(active_universe())
print(f"  {len(base_cands)} signals\n")

print("Fetching TIER 1 (SH, PSQ -- genuine 1x inverse)...")
tier1_cands, tier1_spans = _fetch_candidates(TIER_1X)
for k, (s, e) in tier1_spans.items():
    print(f"  {k}: {s.date()} to {e.date()} (~{(e-s).days/365.25:.1f}y, vs baseline's ~30y)")
print(f"  {len(tier1_cands)} signals\n")

print("Fetching TIER 2 (TBT, GLL -- 2x LEVERAGED inverse, separate tier)...")
tier2_cands, tier2_spans = _fetch_candidates(TIER_2X)
for k, (s, e) in tier2_spans.items():
    print(f"  {k}: {s.date()} to {e.date()} (~{(e-s).days/365.25:.1f}y)")
print(f"  {len(tier2_cands)} signals\n")

years = bt._span_years(base_cands)
# use the SAME 60% cut convention as everywhere else, computed on the baseline's own span
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


print(f"{'='*100}\nFULL HISTORY (methodology caveat: NOT apples-to-apples across tiers, see "
     f"module docstring)\n{'='*100}")
_row("Baseline (22 ETFs, ~30y)", base_cands)
_row("+ SH/PSQ (1x inverse)", base_cands + tier1_cands)
_row("+ SH/PSQ + TBT/GLL (adds 2x leveraged)", base_cands + tier1_cands + tier2_cands)

oos_base = [c for c in base_cands if c["entry_date"] > cut]
oos_t1 = [c for c in tier1_cands if c["entry_date"] > cut]
oos_t2 = [c for c in tier2_cands if c["entry_date"] > cut]
print(f"\n{'='*100}\nOOS (last 40% of the BASELINE's own date range)\n{'='*100}")
_row("Baseline (22 ETFs)", oos_base)
_row("+ SH/PSQ (1x inverse)", oos_base + oos_t1)
_row("+ SH/PSQ + TBT/GLL", oos_base + oos_t1 + oos_t2)

# correlation check: do the new inverse instruments' own trade R-series correlate with SPY's,
# i.e. is this genuinely a NEW bet, or just SPY's existing trend re-expressed a second way?
print(f"\n{'='*100}\nCorrelation check: new instruments' trades vs SPY's own core trades\n{'='*100}")
spy_dates = {c["entry_date"].date() for c in base_cands if c["key"] == "SPY"}
for tier_name, tier_cands in [("SH/PSQ", tier1_cands), ("TBT/GLL", tier2_cands)]:
    overlap = sum(1 for c in tier_cands if c["entry_date"].date() in spy_dates)
    print(f"  {tier_name}: {len(tier_cands)} trades, {overlap} land on the SAME entry date as "
         "an existing SPY core trade (can't happen simultaneously long+short SPY-linked "
         "trend by construction, but same-week entries elsewhere in the book indicate how "
         "correlated the TIMING is)")

print("\nNOTE: 'full history' and 'OOS' figures above blend a ~30y baseline book with "
     "~18-20y-history new instruments -- read as directional, not a byte-for-byte apples-to-"
     "apples extension of the existing 32-year headline figures.")
