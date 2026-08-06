"""Sleeve-specific drawdown circuit breaker -- ADDED 2026-08-06, user-requested critique
verification (critic 1's proposal #4, this session): pause NEW sleeve entries once the
SLEEVE'S OWN (not the whole portfolio's) equity drawdown breaches -10%, resume only once it
recovers to within -5% (hysteresis, avoids flapping on/off right at the threshold).

Premise check before building: this project has tested drawdown-based exposure cuts multiple
times before (DD_SCALE, --vix-regime, portfolio-level vol-targeting) and found them inert or
worse EVERY time -- but always at the WHOLE-PORTFOLIO level. This proposal is scoped
differently: only the sleeve's OWN P&L gates the sleeve's OWN new entries, not the whole
book's DD gating everything. Different enough in scope to be worth testing directly rather
than assuming the same prior result generalizes.

Method: reproduces core/sleeve.py's exact entry/exit logic (RSI<35, ADX>25, close<20MA*0.975
-> exit at 5MA-touch/+3%TP/-5%SL/10-day cap), across all 11 SLEEVE_UNIVERSE tickers, merged
into one CHRONOLOGICAL candidate stream (not per-ticker in isolation, since the gate needs to
see the sleeve's AGGREGATE running P&L across tickers). Gate uses only ALREADY-CLOSED trades'
P&L for the DD check at each candidate's entry moment (no look-ahead -- an open position's
unrealized P&L isn't used, matching this project's existing walk-forward gates, e.g.
_class_factor() in backtest.py).

Run: uv run python -u -m dashboard.research.sleeve_dd_gate_test
"""
from __future__ import annotations
import os
os.environ.setdefault("BROKER", "ib")
os.environ.setdefault("UNIVERSE", "etf")

import numpy as np
import pandas as pd
import yfinance as yf

import dashboard.research.backtest as bt
from dashboard.instruments import active_universe
from dashboard.research.sleeve_blend import _sleeve_unit_series, _metrics, _cash_yield_series
from dashboard.core.sleeve import SLEEVE_UNIVERSE, ADX_THRESHOLD, STOP_FRAC, TARGET_FRAC, TIME_CAP_DAYS

COST = 0.0010
PAUSE_DD = -0.10
RESUME_DD = -0.05


def _ticker_entries(ticker: str) -> list[dict]:
    """Same indicator computation as sleeve_blend.py::_sleeve_trades(), but returns
    CANDIDATE ENTRIES (not yet resolved to exits) with entry date/index/price, so the
    caller can merge candidates across tickers into one chronological stream before
    walking exits -- _sleeve_trades() resolves exits per-ticker in isolation, which
    can't support a cross-ticker sleeve-wide gate."""
    p = yf.download([ticker, "^VIX"], period="max", interval="1d", progress=False, auto_adjust=True)["Close"]
    if ticker not in p.columns:
        return []
    s = p[ticker].dropna()
    ph = yf.download(ticker, period="max", interval="1d", progress=False, auto_adjust=True)
    if hasattr(ph.columns, "nlevels") and ph.columns.nlevels > 1:
        ph.columns = ph.columns.get_level_values(0)
    h, lo = ph["High"].reindex(s.index), ph["Low"].reindex(s.index)
    ma5 = s.rolling(5).mean(); ma20 = s.rolling(20).mean()
    d = s.diff()
    g = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    r14 = 100 - 100 / (1 + g / l.replace(0, np.nan))
    tr = pd.concat([(h - lo), (h - s.shift()).abs(), (lo - s.shift()).abs()], axis=1).max(axis=1)
    up = h.diff(); dn = -lo.diff()
    pl = ((up > dn) & (up > 0)) * up
    mi = ((dn > up) & (dn > 0)) * dn
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    pdi = 100 * pl.ewm(alpha=1 / 14, adjust=False).mean() / atr
    mdi = 100 * mi.ewm(alpha=1 / 14, adjust=False).mean() / atr
    adx = (100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)).ewm(alpha=1 / 14, adjust=False).mean()
    sv, m5v, m20v, r14v, adxv, idx = s.values, ma5.values, ma20.values, r14.values, adx.values, s.index
    ent = (sv < m20v * 0.975) & (r14v < 35) & (adxv > ADX_THRESHOLD)
    ent = np.nan_to_num(ent).astype(bool)
    ent[:200] = False
    out = []
    for i in range(200, len(sv) - 1):
        if ent[i]:
            out.append({"ticker": ticker, "entry_i": i, "entry_date": idx[i]})
    return out, s, ma5, idx


def _resolve_exit(s_vals, m5_vals, idx, i: int) -> tuple:
    e = s_vals[i]; j = i + 1; n = len(s_vals)
    while j < n:
        r = s_vals[j] / e - 1.0
        if s_vals[j] >= m5_vals[j] or r >= TARGET_FRAC:
            return idx[min(j, n - 1)], r - COST
        if r <= -STOP_FRAC:
            return idx[min(j, n - 1)], -STOP_FRAC - COST
        if (j - i) >= TIME_CAP_DAYS:
            return idx[min(j, n - 1)], r - COST
        j += 1
    return idx[-1], s_vals[-1] / e - 1.0 - COST


print(f"Fetching sleeve data ({len(SLEEVE_UNIVERSE)} tickers)...")
per_ticker = {}
all_candidates = []
for tk in SLEEVE_UNIVERSE:
    cands, s, ma5, idx = _ticker_entries(tk)
    per_ticker[tk] = (s.values, ma5.values, idx)
    all_candidates += cands
all_candidates.sort(key=lambda c: c["entry_date"])
print(f"{len(all_candidates)} candidate entries across all tickers, merged chronologically.\n")


def _walk(gated: bool) -> list[dict]:
    """Chronological walk. UNGATED: every candidate fires SUBJECT TO the same "one trade
    at a time per ticker" constraint _sleeve_trades() enforces (via its own i=j+1 skip-
    ahead) -- a ticker whose RSI stays <35 for many consecutive days must NOT count as a
    new entry every single day, only once per resolved trade. GATED: additionally skip a
    candidate if the sleeve's own closed-trade equity DD (as of that candidate's date)
    has breached PAUSE_DD and not yet recovered to RESUME_DD."""
    closed: list[dict] = []      # {"d": exit_date, "r": r}
    paused = False
    trades_out = []
    next_free: dict[str, pd.Timestamp] = {}   # ticker -> earliest date it can re-enter
    for c in all_candidates:
        tk = c["ticker"]
        if tk in next_free and c["entry_date"] < next_free[tk]:
            continue   # this ticker still has an open trade -- matches _sleeve_trades()'s i=j+1
        if gated:
            realized = [t["r"] for t in closed if t["d"] < c["entry_date"]]
            if realized:
                eq = np.cumprod([1 + r for r in realized])
                peak = np.maximum.accumulate(eq)
                dd_now = eq[-1] / peak[-1] - 1
                if not paused and dd_now <= PAUSE_DD:
                    paused = True
                elif paused and dd_now >= RESUME_DD:
                    paused = False
            if paused:
                continue
        s_vals, m5_vals, idx = per_ticker[tk]
        exit_date, r = _resolve_exit(s_vals, m5_vals, idx, c["entry_i"])
        next_free[tk] = exit_date + pd.Timedelta(days=1)   # matches i=j+1 (next bar after exit)
        closed.append({"d": exit_date, "r": r})
        trades_out.append({"d": exit_date, "r": r})
    return trades_out


print("Running UNGATED sleeve walk (baseline, matches existing deployed spec)...")
ungated = _walk(gated=False)
print(f"  {len(ungated)} trades\n")

print(f"Running GATED sleeve walk (pause at {PAUSE_DD:.0%} sleeve-own DD, "
     f"resume at {RESUME_DD:.0%})...")
gated = _walk(gated=True)
print(f"  {len(gated)} trades ({len(ungated)-len(gated)} skipped by the gate)\n")


def _core_ret_and_years() -> tuple[pd.Series, float]:
    bt.CASH_YIELD = None
    bt.POS_CAP = 0.25
    bt.PORTFOLIO_CAP = 1.0
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
    eq, _ = bt._portfolio(cands, 0.01)
    eq = eq[~eq.index.duplicated(keep="last")].sort_index()
    s, e = eq.index[0], eq.index[-1]
    didx = pd.date_range(s, e, freq="B", tz="UTC")
    ret = (eq.reindex(eq.index.union(didx)).ffill().reindex(didx).ffill()
           .pct_change().fillna(0.0))
    years = (didx[-1] - didx[0]).days / 365.25
    return ret, years


print("Fetching core book for the blended comparison...")
core_ret, years = _core_ret_and_years()
didx = core_ret.index
WEIGHT = 0.10


def _row(label: str, ret) -> None:
    cagr, dd, sharpe = _metrics(ret, years)
    calmar = cagr / abs(dd) if dd else 0.0
    print(f"  {label:<45} CAGR {cagr*100:+7.2f}%  maxDD {dd*100:7.2f}%  Calmar {calmar:6.3f}")


print(f"\n{'='*90}\nFULL HISTORY ({years:.1f}y)\n{'='*90}")
ungated_unit = _sleeve_unit_series(ungated, didx)
gated_unit = _sleeve_unit_series(gated, didx)
_row("core + sleeve@10% UNGATED (current deployed spec)", core_ret + WEIGHT * ungated_unit)
_row(f"core + sleeve@10% GATED (pause<{PAUSE_DD:.0%}, resume>{RESUME_DD:.0%})",
    core_ret + WEIGHT * gated_unit)

cut = didx[0] + (didx[-1] - didx[0]) * 0.6
oos_yrs = (didx[-1] - cut).days / 365.25
core_oos = core_ret[core_ret.index >= cut]
ungated_oos = ungated_unit[ungated_unit.index >= cut]
gated_oos = gated_unit[gated_unit.index >= cut]
print(f"\n{'='*90}\nOOS (last 40%, {oos_yrs:.1f}y)\n{'='*90}")
_row("core + sleeve@10% UNGATED", core_oos + WEIGHT * ungated_oos)
_row("core + sleeve@10% GATED", core_oos + WEIGHT * gated_oos)

# sleeve-ONLY comparison too (isolates the gate's effect from the core's own noise)
print(f"\n{'='*90}\nSLEEVE-ONLY (no core blend, isolates the gate's own effect)\n{'='*90}")
_row("Sleeve UNGATED", ungated_unit)
_row("Sleeve GATED", gated_unit)

print("\nNOTE: gate decision uses only trades CLOSED strictly before each candidate's own "
     "entry date -- no look-ahead. Overlapping positions on DIFFERENT tickers resolve "
     "independently (matches how the live sleeve already behaves; only NEW entries are "
     "gated, not existing open positions).")
