"""Portfolio-level realized-vol scaling, DOWNSIDE-ONLY -- ADDED 2026-08-06, user-requested
critique verification (critic 1, item #3). Premise checked first: this project's EXISTING
vol-targeting mechanism (`_portfolio()`'s `target_vol` param + `_vol_factor()`) is SYMMETRIC --
it can scale risk UP TO VOLTARGET_FACTOR_CAP=3.0x in calm regimes as well as down to 0.25x in
turbulent ones. That symmetric version was already tested and REJECTED (HANDOFF 2026-06-23/
2026-07-28: "voltarget 12% ~3-4% realized vol = 3x leverage -> CAGR 2x DD 3x, ratio worse").

The critique's proposal is narrower: only cut exposure when realized vol RISES, never lever
UP when calm -- explicitly distinguishing itself from the already-rejected symmetric version.
This reuses the SAME existing, already-tested machinery -- no new sizing code needed -- just
forces VOLTARGET_FACTOR_CAP=1.0 (never exceed baseline risk) while keeping the existing 0.25x
floor, isolating the downside-only effect cleanly.

Caveat flagged before running: this is conceptually close to 2 OTHER already-rejected ideas
in this project's history (DD_SCALE, --vix-regime) with the identical stated reason both
times -- "reduce exposure based on a lagging/coincident signal... trend filter already
de-risks endogenously" -- so the prior here is skeptical. Testing anyway since it's cheap
(reuses existing code) and the signal (realized portfolio vol) is not IDENTICAL to VIX or
drawdown, just similar in kind.

Run: uv run python -u -m dashboard.research.portfolio_vol_scale_downside_only_test
"""
from __future__ import annotations
import os
os.environ.setdefault("BROKER", "ib")
os.environ.setdefault("UNIVERSE", "etf")

import yfinance as yf

import dashboard.research.backtest as bt
from dashboard.instruments import active_universe

RISK = 0.01          # live risk level -- the one that matters
bt.CASH_YIELD = None
bt.POS_CAP = 0.25
bt.PORTFOLIO_CAP = 1.0

print(f"Fetching full-history weekly data ({len(active_universe())} instruments)...")
cands = []
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
    if len(df) < 220:
        continue
    cands += bt._signals(df, inst.key)
print(f"{len(cands)} signals.\n")

years = bt._span_years(cands)
cut = min(c["entry_date"] for c in cands) + (max(c["exit_date"] for c in cands) -
                                             min(c["entry_date"] for c in cands)) * 0.6
oos = [c for c in cands if c["entry_date"] > cut]
oos_years = bt._span_years(oos)


def _row(label: str, sub, yrs, target_vol=None) -> None:
    eq, real = bt._portfolio(sub, RISK, target_vol=target_vol, tpy=38.0)
    m = bt._metrics(eq, real, yrs)
    calmar = m["cagr"] / abs(m["maxdd"]) if m["maxdd"] else 0.0
    print(f"  {label:<50} CAGR {m['cagr']*100:+7.2f}%  maxDD {m['maxdd']*100:7.2f}%  "
         f"Calmar {calmar:6.3f}")


print("BASELINE (fixed risk, no vol scaling):")
_row("Full history", cands, years)
_row("OOS", oos, oos_years)

# realized ANNUALIZED vol at baseline sizing, for a sensible target_vol reference point
eq0, real0 = bt._portfolio(cands, RISK)
import numpy as np
base_ann_vol = float(np.std(real0[-bt.VOLTARGET_WINDOW:], ddof=1)) * RISK * (38.0 ** 0.5)
print(f"\n(baseline trailing realized annualized vol at {RISK:.0%} risk, last "
     f"{bt.VOLTARGET_WINDOW} closed trades: ~{base_ann_vol*100:.1f}% -- using this as the "
     f"target_vol reference so the downside-only variant is centered near current behavior, "
     f"not an arbitrary target)\n")

print("DOWNSIDE-ONLY VOL SCALING (VOLTARGET_FACTOR_CAP forced to 1.0x -- NEVER lever up in "
     "calm markets, only ever cuts risk when realized vol rises above target):")
old_cap = bt.VOLTARGET_FACTOR_CAP
bt.VOLTARGET_FACTOR_CAP = 1.0
for floor in (0.25, 0.5):
    bt.VOLTARGET_FACTOR_FLOOR = floor
    print(f"\n  floor={floor}x:")
    _row(f"    Full history", cands, years, target_vol=base_ann_vol)
    _row(f"    OOS", oos, oos_years, target_vol=base_ann_vol)
bt.VOLTARGET_FACTOR_CAP = old_cap

print("\nNOTE: this is the SAME risk_mult-style sizing hook used elsewhere in this project "
     "(DD_SCALE, --vix-regime, --conviction-size) -- if this shows the same 'worse or inert, "
     "not better' pattern those did, that's a 4th confirmation of the same structural lesson, "
     "not a new one.")
