"""Universe breadth expansion test -- ADDED 2026-08-06, user-requested critique verification
(agricultural commodities + international/EM bonds, following the inverse-ETF rejection).
Falls squarely under the parameter freeze's "new universe/instrument addition" exception.

Candidates verified before testing: CORN (Teucrium Corn), WEAT (Teucrium Wheat), BNDX
(Vanguard Total International Bond ex-US), LEMB (iShares JPM EM Local Currency Bond).
JO (iPath Coffee ETN), the critique's 3rd agricultural pick, is CONFIRMED DELISTED (real
price data ends 2023-07-17) -- excluded, not a currently-tradable instrument regardless of
backtest performance.

IMPORTANT prior-art check done before running: LEMB is economically very close to EMLC
(both track JPM EM local-currency government bond indices) -- and EMLC was ALREADY tested
in this project's "Batch-3 screen" and REJECTED (n=19, expR=-0.261, "worst market in set").
LEMB is tested here anyway (verifying rather than assuming the prior generalizes to a
different-but-similar product), but the prior result means it should be treated with real
skepticism, not a fresh coin-flip.

History lengths (all shorter than this project's ~30-32y standard, disclosed per this
project's established transparency practice): CORN ~16.2y (from 2010), WEAT ~14.9y (2011),
BNDX ~13.2y (2013), LEMB ~14.8y (2011).

Two-stage screen matching this project's own established methodology (see HANDOFF's
"Batch-3/4 screen" tables): (1) cheap raw isolation stats (n, win rate, expectancy-R) per
candidate BEFORE any portfolio simulation, to filter out clearly-negative candidates early;
(2) for anything with positive expR, the more expensive full 22+1-instrument portfolio
Full/OOS Calmar comparison against the real baseline.

Run: uv run python -u -m dashboard.research.breadth_expansion_test
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
paper.WEEKLY_TREND_CLASSES = set()   # no whitelist -- these are genuinely new candidate
                                      # classes not yet in the adopted set, same convention
                                      # this project's own --etf-screen3/4 flags use

CANDIDATES = [
    Instrument("CORN_CAND", "Corn futures ETF", "CORN", "", "commodity"),
    Instrument("WEAT_CAND", "Wheat futures ETF", "WEAT", "", "commodity"),
    Instrument("BNDX_CAND", "Intl bond ex-US", "BNDX", "", "intl_rate"),
    Instrument("LEMB_CAND", "EM local-ccy bond", "LEMB", "", "em_local_debt"),
]
for _inst in CANDIDATES:
    instruments_mod.BY_KEY[_inst.key] = _inst


def _fetch_signals(inst) -> list[dict]:
    df = yf.download(inst.yf, period="max", interval="1wk", progress=False, auto_adjust=True)
    if df is None or len(df) == 0:
        print(f"  {inst.key}: NO DATA")
        return []
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close"]].copy()
    df.columns = ["open", "high", "low", "close"]
    df = df.dropna()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    if len(df) < 220:
        print(f"  {inst.key}: only {len(df)} bars, skipped")
        return []
    print(f"  {inst.key}: {df.index[0].date()} to {df.index[-1].date()} "
         f"(~{(df.index[-1]-df.index[0]).days/365.25:.1f}y)")
    return bt._signals(df, inst.key)


print("STAGE 1: raw isolation stats per candidate (no portfolio simulation yet)\n")
print(f"{'ticker':<12}{'class':<16}{'n':>5}{'win%':>7}{'expR':>9}{'verdict'}")
candidate_cands: dict[str, list[dict]] = {}
for inst in CANDIDATES:
    cands = _fetch_signals(inst)
    candidate_cands[inst.key] = cands
    rs = [c["r"] for c in cands]
    s = paper.stats(rs)
    verdict = ("ADVANCE to stage 2" if s["expectancy_R"] > 0 and s["n"] >= 15
              else "reject (negative expR)" if s["expectancy_R"] <= 0
              else "reject (n too small to trust)")
    print(f"{inst.key:<12}{inst.asset_class:<16}{s['n']:>5}{s['win_rate']*100:>6.0f}%"
         f"{s['expectancy_R']:>+9.3f}  {verdict}")

print("\n" + "="*100)
print("STAGE 2: full portfolio Calmar/CAGR/DD comparison (only for candidates that advanced)")
print("="*100 + "\n")

print("Fetching baseline 22-ETF universe...")
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
    if len(df) < 220:
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


advancing = {k: v for k, v in candidate_cands.items()
            if v and paper.stats([c["r"] for c in v])["expectancy_R"] > 0
            and paper.stats([c["r"] for c in v])["n"] >= 15}

print(f"{'='*100}\nFULL HISTORY\n{'='*100}")
_row("Baseline (22 ETFs)", base_cands)
if advancing:
    for k, v in advancing.items():
        _row(f"+ {k}", base_cands + v)
    _row("+ ALL advancing candidates combined", base_cands + [c for v in advancing.values() for c in v])
else:
    print("  (no candidate advanced past Stage 1 -- nothing to test at portfolio level)")

oos_base = [c for c in base_cands if c["entry_date"] > cut]
print(f"\n{'='*100}\nOOS (last 40% of the baseline's own date range)\n{'='*100}")
_row("Baseline (22 ETFs)", oos_base)
if advancing:
    for k, v in advancing.items():
        oos_v = [c for c in v if c["entry_date"] > cut]
        _row(f"+ {k}", oos_base + oos_v)
    oos_all = [c for v in advancing.values() for c in v if c["entry_date"] > cut]
    _row("+ ALL advancing candidates combined", oos_base + oos_all)

print("\nNOTE: candidate history is shorter than the ~30-32y baseline in every case -- read "
     "the blended figures as directional, not a byte-for-byte apples-to-apples extension of "
     "the existing headline numbers (same caveat as every other universe-addition test this "
     "project has run).")
