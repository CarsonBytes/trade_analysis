"""Signal-congestion slippage stress test -- ADDED 2026-08-06, user-requested critique
verification (critic 1, item #6). Premise checked empirically FIRST, not assumed: counted how
often multiple CORE signals fire in the same ISO week across the full 30y history -- 161 of
668 signal-weeks (24.1%) have 3+ simultaneous signals, a genuinely common pattern, not a rare
edge case -- so this is worth actually testing, not dismissing.

Applies an ESCALATING per-trade slippage penalty within each congested week: the 1st signal
(by instrument key, alphabetical -- a deterministic, arbitrary-but-reproducible tie-break,
since real signal PRIORITY isn't tracked in this backtest) pays the existing baseline cost
only; the 2nd pays +2bps extra; the 3rd+ pays +5bps extra (matching the critique's own
suggested schedule), simulating each additional same-week market order eating a little more
of a thinner order book. This is layered ON TOP of the existing baseline cost (confirmed by
reading paper.r_multiple()/HALF_SPREAD directly: ETF trades use a 1bp round-trip cost via
`entry * HALF_SPREAD * 2`, HALF_SPREAD=0.00005 -- NOT the "~10bp" figure found in some HANDOFF
prose, which turned out to refer to a different context; disclosed here so this isn't silently
inconsistent with that other number).

Scope: CORE strategy only (where the clustering count above was measured). The sleeve
(critic's original RSI/ADX panic-buying concern) uses a separate resolution mechanism in
sleeve_blend.py with its own cost assumptions -- a natural follow-up, out of scope here given
the CORE result alone is enough to judge whether this class of friction matters.

Run: uv run python -u -m dashboard.research.congestion_slippage_test
"""
from __future__ import annotations
import os
os.environ.setdefault("BROKER", "ib")
os.environ.setdefault("UNIVERSE", "etf")

import pandas as pd
import yfinance as yf

import dashboard.research.backtest as bt
from dashboard.instruments import active_universe

RISK = 0.01
bt.CASH_YIELD = None
bt.POS_CAP = 0.25
bt.PORTFOLIO_CAP = 1.0

RANK_EXTRA_BPS = {1: 0, 2: 2, 3: 5}   # rank -> extra round-trip bps (rank >=3 all pay 5bps)


def _extra_bps_for_rank(rank: int) -> float:
    return RANK_EXTRA_BPS.get(rank, RANK_EXTRA_BPS[3])


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

print(f"{len(cands)} signals generated.\n")

# group by ISO (year, week), assign a deterministic rank within each congested week
weeks: dict[tuple, list] = {}
for c in cands:
    wk = c["entry_date"].isocalendar()[:2]
    weeks.setdefault(wk, []).append(c)

n_congested_trades = 0
for wk, group in weeks.items():
    group.sort(key=lambda c: c["key"])   # deterministic tie-break, see module docstring
    for rank, c in enumerate(group, 1):
        c["_week_rank"] = rank
        if rank >= 2:
            n_congested_trades += 1

print(f"{n_congested_trades} of {len(cands)} trades ({n_congested_trades/len(cands)*100:.1f}%) "
     f"are a 2nd-or-later same-week entry and would pay an extra congestion penalty.\n")

years = bt._span_years(cands)


def _apply_penalty(cands: list[dict]) -> list[dict]:
    """Returns a NEW list with r adjusted -- never mutates the original in place, so the
    unpenalized baseline run below stays valid. Candidate dicts don't carry raw entry/sl
    prices (see _signals()), only "nmult" = entry / risk_per_share (already computed there
    for the idle-cash notional model) -- reuse it: extra_cost_price/risk_per_share =
    (entry*extra_bps/10000)/risk_per_share = nmult * extra_bps/10000, no raw prices needed."""
    out = []
    for c in cands:
        c2 = dict(c)
        extra_bps = _extra_bps_for_rank(c["_week_rank"])
        if extra_bps > 0:
            c2["r"] = c["r"] - c["nmult"] * (extra_bps / 10000.0)
        out.append(c2)
    return out


def _row(label: str, use_cands: list[dict]) -> None:
    eq, real = bt._portfolio(use_cands, RISK)
    m = bt._metrics(eq, real, years)
    calmar = m["cagr"] / abs(m["maxdd"]) if m["maxdd"] else 0.0
    mean_r = sum(real) / len(real) if real else 0.0
    print(f"  {label:<45} n={len(real):<5} meanR={mean_r:+.4f}  CAGR={m['cagr']*100:+7.2f}%  "
         f"maxDD={m['maxdd']*100:7.2f}%  Calmar={calmar:6.3f}")


print("CONGESTION-SLIPPAGE STRESS TEST (core strategy, full history):\n")
_row("Baseline (existing 1bp round-trip cost only)", cands)
_row("+ escalating congestion penalty (0/2/5bps by same-week rank)", _apply_penalty(cands))

# also show the OOS-only slice, same 60% cut convention as everywhere else
cut = min(c["entry_date"] for c in cands) + (max(c["exit_date"] for c in cands) -
                                             min(c["entry_date"] for c in cands)) * 0.6
oos = [c for c in cands if c["entry_date"] > cut]
oos_years = bt._span_years(oos)
print(f"\nOOS only ({oos_years:.1f}y):")


def _row_oos(label: str, use_cands: list[dict]) -> None:
    eq, real = bt._portfolio(use_cands, RISK)
    m = bt._metrics(eq, real, oos_years)
    calmar = m["cagr"] / abs(m["maxdd"]) if m["maxdd"] else 0.0
    print(f"  {label:<45} n={len(real):<5} CAGR={m['cagr']*100:+7.2f}%  "
         f"maxDD={m['maxdd']*100:7.2f}%  Calmar={calmar:6.3f}")


_row_oos("Baseline", oos)
_row_oos("+ congestion penalty", _apply_penalty(oos))

print("\nNOTE: rank-within-week assignment is a deterministic tie-break (alphabetical by "
     "instrument key), NOT a claim about real fill order -- this backtest has no signal-"
     "priority data. The penalty schedule (0/2/5bps) is the critique's own suggested figures, "
     "layered on top of the existing 1bp baseline cost (not the '~10bp' figure found "
     "elsewhere in HANDOFF, which refers to a different context -- see module docstring).")
