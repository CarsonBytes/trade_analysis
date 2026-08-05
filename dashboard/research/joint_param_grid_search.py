"""Joint (combined) parameter grid search -- ADDED 2026-08-06, user-requested critique
verification (critic 2, item #1). Premise verified against HANDOFF.md before building this:
a real, documented one-at-a-time-vs-combined divergence exists (param_sensitivity.py's
"COMBINED" block, 2026-07-11) -- three parameters each individually favorable (SL_ATR_MULT
-20%, RR_DEFAULT +20%, tighter OVEREXT 65/35) combined to a WORSE ratio (0.468) than baseline
(0.533), not better. That's exactly the kind of interaction effect a one-at-a-time sweep
can't see, and justifies a broader systematic search rather than trusting that one 3-corner
test generalizes (or assuming it's the only bad combination out there).

Random search (not full grid -- 6x6x5x5=900 combos at several seconds each is not worth
running in full) over the same 4 parameters critic 2 named, at critic 2's own suggested
ranges: SL_ATR_MULT [1.0,2.5], RR_DEFAULT [2.5,4.0], HORIZON_DAYS [3,7], OVEREXT_HI (paired
LO=100-HI) [65,75]. Same overfitting guard the critic themselves proposed: a candidate must
beat baseline on BOTH full-history AND OOS Calmar to be considered at all (not just OOS,
which alone would just re-discover recency-fitting) -- then DSR-corrected for the TOTAL
number of trials actually run, using this project's own existing deflated_sharpe_ratio()
(same one used everywhere else in this project, not a separately-invented check).

Run: uv run python -u -m dashboard.research.joint_param_grid_search [n_trials]
"""
from __future__ import annotations
import os
os.environ.setdefault("BROKER", "ib")
os.environ.setdefault("UNIVERSE", "etf")

import random
import sys
import time

import pandas as pd
import yfinance as yf

sys.path.insert(0, "D:/quant")
import dashboard.research.backtest as bt
from dashboard.instruments import active_universe
from dashboard.core import paper
from metrics import deflated_sharpe_ratio

N_TRIALS = int(sys.argv[1]) if len(sys.argv) > 1 else 120
RISK = 0.01
bt.CASH_YIELD = None
bt.POS_CAP = 0.25
bt.PORTFOLIO_CAP = 1.0

print(f"Fetching full-history weekly data ({len(active_universe())} instruments, ONCE)...")
data = {}
for inst in active_universe():
    raw = yf.download(inst.yf, period="max", interval="1wk", progress=False, auto_adjust=True)
    if raw is None or len(raw) == 0:
        continue
    if hasattr(raw.columns, "nlevels") and raw.columns.nlevels > 1:
        raw.columns = raw.columns.get_level_values(0)
    df = raw[["Open", "High", "Low", "Close"]].copy()
    df.columns = ["open", "high", "low", "close"]
    df = df.dropna()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    if len(df) < 220:
        continue
    data[inst.key] = df
print(f"  {len(data)} instruments loaded.\n")

base_sl, base_rr, base_h = paper.SL_ATR_MULT, paper.RR_DEFAULT, paper.HORIZON_DAYS
base_hi, base_lo = paper.OVEREXT_HI, paper.OVEREXT_LO


def _reset():
    paper.SL_ATR_MULT, paper.RR_DEFAULT, paper.HORIZON_DAYS = base_sl, base_rr, base_h
    paper.OVEREXT_HI, paper.OVEREXT_LO = base_hi, base_lo


def _run_split(cut) -> dict:
    """Full + OOS metrics for the CURRENT global param values (already set by caller)."""
    cands = []
    for key, df in data.items():
        cands += bt._signals(df, key)
    if not cands:
        return {}
    years_full = bt._span_years(cands)
    eq_full, real_full = bt._portfolio(cands, RISK)
    m_full = bt._metrics(eq_full, real_full, years_full)
    calmar_full = m_full["cagr"] / abs(m_full["maxdd"]) if m_full["maxdd"] else 0.0

    oos_cands = [c for c in cands if c["entry_date"] > cut]
    if len(oos_cands) < 10:
        return {"n": len(cands), "full_cagr": m_full["cagr"], "full_dd": m_full["maxdd"],
               "full_calmar": calmar_full, "oos_calmar": None}
    years_oos = bt._span_years(oos_cands)
    eq_oos, real_oos = bt._portfolio(oos_cands, RISK)
    m_oos = bt._metrics(eq_oos, real_oos, years_oos)
    calmar_oos = m_oos["cagr"] / abs(m_oos["maxdd"]) if m_oos["maxdd"] else 0.0
    return {"n": len(cands), "n_oos": len(oos_cands),
           "full_cagr": m_full["cagr"], "full_dd": m_full["maxdd"], "full_calmar": calmar_full,
           "oos_cagr": m_oos["cagr"], "oos_dd": m_oos["maxdd"], "oos_calmar": calmar_oos,
           "oos_real": real_oos}


# date cutoff for the OOS split -- same 60/40 convention used everywhere else in this project
_reset()
all_cands = []
for key, df in data.items():
    all_cands += bt._signals(df, key)
span_start = min(c["entry_date"] for c in all_cands)
span_end = max(c["exit_date"] for c in all_cands)
cut = span_start + (span_end - span_start) * 0.6

print("BASELINE (live-adopted config):")
t0 = time.time()
base_result = _run_split(cut)
base_dt = time.time() - t0
print(f"  SL_ATR_MULT={base_sl} RR_DEFAULT={base_rr} HORIZON_DAYS={base_h} "
     f"OVEREXT={base_hi:.0f}/{base_lo:.0f}")
print(f"  n={base_result['n']:<4} Full CAGR={base_result['full_cagr']*100:+.2f}% "
     f"DD={base_result['full_dd']*100:.2f}% Calmar={base_result['full_calmar']:.3f}  |  "
     f"OOS CAGR={base_result['oos_cagr']*100:+.2f}% DD={base_result['oos_dd']*100:.2f}% "
     f"Calmar={base_result['oos_calmar']:.3f}")
print(f"  (one full+OOS run took {base_dt:.1f}s -> {N_TRIALS} trials should take "
     f"~{N_TRIALS*base_dt/60:.1f} min)\n")

# random search grid, seeded for reproducibility
random.seed(42)
SL_RANGE = (1.0, 2.5)
RR_RANGE = (2.5, 4.0)
H_RANGE = (3, 7)
OVEREXT_HI_RANGE = (65, 75)

trials = []
print(f"Running {N_TRIALS} random joint-parameter trials...")
t_start = time.time()
for i in range(N_TRIALS):
    sl = round(random.uniform(*SL_RANGE), 2)
    rr = round(random.uniform(*RR_RANGE), 2)
    h = random.randint(*H_RANGE)
    hi = round(random.uniform(*OVEREXT_HI_RANGE))
    lo = 100 - hi
    paper.SL_ATR_MULT, paper.RR_DEFAULT, paper.HORIZON_DAYS = sl, rr, h
    paper.OVEREXT_HI, paper.OVEREXT_LO = hi, lo
    r = _run_split(cut)
    _reset()
    if not r or r.get("oos_calmar") is None:
        continue
    r["params"] = (sl, rr, h, hi, lo)
    trials.append(r)
    if (i + 1) % 20 == 0:
        elapsed = time.time() - t_start
        print(f"  {i+1}/{N_TRIALS}  ({elapsed:.0f}s elapsed, "
             f"~{elapsed/(i+1)*(N_TRIALS-i-1):.0f}s remaining)")

print(f"\n{len(trials)} valid trials completed in {time.time()-t_start:.0f}s.\n")

# guard per critic's own proposed methodology: must beat baseline on BOTH full AND OOS Calmar
passing = [t for t in trials
          if t["full_calmar"] > base_result["full_calmar"]
          and t["oos_calmar"] > base_result["oos_calmar"]]
passing.sort(key=lambda t: t["oos_calmar"], reverse=True)

print(f"Trials beating baseline on BOTH full-history AND OOS Calmar: "
     f"{len(passing)}/{len(trials)}\n")

if not passing:
    print("No trial beat baseline on both dimensions -- the live-adopted config is not "
         "dominated by anything in this search. Consistent with param_sensitivity.py's "
         "existing one-at-a-time result (no collapse, no easy win either).")
else:
    print(f"{'rank':<5}{'SL':>6}{'RR':>6}{'HORIZ':>7}{'OVEREXT':>9}"
         f"{'Full Calmar':>13}{'OOS Calmar':>12}{'OOS n':>7}")
    for rank, t in enumerate(passing[:15], 1):
        sl, rr, h, hi, lo = t["params"]
        print(f"{rank:<5}{sl:>6.2f}{rr:>6.2f}{h:>7d}{f'{hi:.0f}/{lo:.0f}':>9}"
             f"{t['full_calmar']:>13.3f}{t['oos_calmar']:>12.3f}{t['n_oos']:>7}")

    print(f"\nDSR correction for the top candidate, n_trials={len(trials)} "
         f"(the honest count -- this many configurations were actually tried, not just the "
         f"winner in isolation):")
    top = passing[0]
    trial_sharpes = []
    for t in trials:
        real = t.get("oos_real")
        if real is not None and len(real) > 1:
            s = pd.Series(real)
            sharpe = s.mean() / s.std() if s.std() > 0 else 0.0
            trial_sharpes.append(sharpe)
    top_real = pd.Series(top["oos_real"])
    naive_dsr = deflated_sharpe_ratio(top_real, n_trials=1)
    corrected_dsr = deflated_sharpe_ratio(top_real, n_trials=len(trials),
                                          trial_sharpes=trial_sharpes)
    sl, rr, h, hi, lo = top["params"]
    print(f"  Best: SL_ATR_MULT={sl} RR_DEFAULT={rr} HORIZON_DAYS={h} OVEREXT={hi:.0f}/{lo:.0f}")
    print(f"  naive DSR (n_trials=1): {naive_dsr:.0%}   "
         f"corrected DSR (n_trials={len(trials)}): {corrected_dsr:.0%}")
    print("\n  Rule of thumb: DSR < 95% after correction means don't trust it as a real edge "
         "over the current config, even though it beat baseline in this one backtest.")

print(f"\n(baseline: SL_ATR_MULT={base_sl} RR_DEFAULT={base_rr} HORIZON_DAYS={base_h} "
     f"OVEREXT={base_hi:.0f}/{base_lo:.0f}, Full Calmar={base_result['full_calmar']:.3f}, "
     f"OOS Calmar={base_result['oos_calmar']:.3f})")
