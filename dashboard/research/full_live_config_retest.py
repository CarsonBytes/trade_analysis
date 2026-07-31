"""Re-verification of the "Update 2026-07-18" full deployed config (core + reclaim-1.0R
re-entry gate + panic-MR sleeve, pos_cap=30%, risk=1%) against TODAY's sleeve spec (no VIX
entry condition, ADX>25 -- deployed 2026-07-31). The original 2026-07-18 investigation used a
jointly-position-sized single portfolio walk (core+sleeve candidates competing for the same
portfolio-cap budget in one simulation) via an ad-hoc script that was never saved
permanently. This uses the SAME simpler additive-blend methodology as every other sleeve
backtest this session (core return series + weight*sleeve-unit-series) for consistency with
today's other numbers -- NOT a like-for-like reproduction of the original's exact joint-
sizing method, disclosed here rather than silently presented as equivalent-rigor.

Run: uv run python -u -m dashboard.research.full_live_config_retest
"""
from __future__ import annotations
import os
os.environ.setdefault("BROKER", "ib")
os.environ.setdefault("UNIVERSE", "etf")

import pandas as pd
import yfinance as yf

import dashboard.research.backtest as bt
from dashboard.instruments import active_universe
from dashboard.research.sleeve_blend import (
    _sleeve_trades, _sleeve_unit_series, _metrics, _cash_yield_series,
)
from dashboard.core.sleeve import SLEEVE_UNIVERSE, ADX_THRESHOLD

LIVE_POS_CAP = 0.30
LIVE_PORTFOLIO_CAP = 1.0
LIVE_RISK = 0.01
REENTRY_BUFFER_R = 1.0
SLEEVE_WEIGHT = 0.10   # same convention used throughout today's other sleeve backtests


def _core_with_reentry_gate(pos_cap: float, portfolio_cap: float, risk: float,
                            cash_yield: bool = False) -> tuple[pd.Series, float, int]:
    """Same as sleeve_blend.py's _core_weekly_returns(), but with the DEPLOYED re-entry
    gate active (reclaim_buffer, 1.0R) -- that function never wired this in, so this
    reproduces it explicitly rather than adding a rarely-used parameter to the shared one."""
    bt.POS_CAP = pos_cap
    bt.PORTFOLIO_CAP = portfolio_cap
    bt.CASH_YIELD = _cash_yield_series() if cash_yield else None
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
        cands += bt._signals(df, inst.key, reentry_gate="reclaim_buffer",
                             reentry_buffer_r=REENTRY_BUFFER_R)
    eq, _ = bt._portfolio(cands, risk)
    eq = eq[~eq.index.duplicated(keep="last")].sort_index()
    s, e = eq.index[0], eq.index[-1]
    didx = pd.date_range(s, e, freq="B", tz="UTC")
    ret = (eq.reindex(eq.index.union(didx)).ffill().reindex(didx).ffill()
           .pct_change().fillna(0.0))
    years = (didx[-1] - didx[0]).days / 365.25
    return ret, years, len(cands)


def main() -> None:
    print(f"Fetching core book (pos_cap={LIVE_POS_CAP:.0%}, risk={LIVE_RISK:.0%}, "
          f"re-entry gate reclaim+{REENTRY_BUFFER_R}R active)...")
    core_ret, years, n_core = _core_with_reentry_gate(LIVE_POS_CAP, LIVE_PORTFOLIO_CAP, LIVE_RISK)
    didx = core_ret.index

    print(f"Fetching sleeve data ({len(SLEEVE_UNIVERSE)} tickers, current live spec: "
          f"no VIX entry gate, ADX>{ADX_THRESHOLD})...")
    sleeve_trades = {tk: _sleeve_trades(tk) for tk in SLEEVE_UNIVERSE}
    sleeve_unit = sum((_sleeve_unit_series(trs, didx) for trs in sleeve_trades.values()),
                      pd.Series(0.0, index=didx))
    n_sleeve = sum(len(trs) for trs in sleeve_trades.values())

    def _row(label, ret, yrs):
        c, d, s = _metrics(ret, yrs)
        calmar = c / abs(d) if d else 0
        print(f"  {label:<45} CAGR {c*100:+7.2f}%  maxDD {d*100:7.2f}%  Sharpe {s:6.3f}  Calmar {calmar:6.3f}")

    print(f"\n{'='*100}")
    print("FULL HISTORY")
    print(f"{'='*100}")
    _row("core + gate only (no sleeve)", core_ret, years)
    _row(f"core + gate + sleeve@{SLEEVE_WEIGHT:.0%} (LATEST 2026-07-31 sleeve spec)",
        core_ret + SLEEVE_WEIGHT * sleeve_unit, years)

    cut = didx[0] + (didx[-1] - didx[0]) * 0.6
    oos_yrs = (didx[-1] - cut).days / 365.25
    core_oos = core_ret[core_ret.index >= cut]
    sleeve_oos = sleeve_unit[sleeve_unit.index >= cut]
    print(f"\n{'='*100}")
    print(f"OOS (last 40% of the date range, {oos_yrs:.1f}y)")
    print(f"{'='*100}")
    _row("core + gate only (no sleeve)", core_oos, oos_yrs)
    _row(f"core + gate + sleeve@{SLEEVE_WEIGHT:.0%} (LATEST 2026-07-31 sleeve spec)",
        core_oos + SLEEVE_WEIGHT * sleeve_oos, oos_yrs)

    print(f"\n({n_core} core signals, {n_sleeve} sleeve trades, {years:.1f}y full span)")
    print("\nNOTE: this uses the simpler additive-blend methodology (core return series + "
          "weight*sleeve-unit-series), same as every other sleeve backtest this session --")
    print("NOT the original 2026-07-18 investigation's jointly-position-sized single-walk "
          "method (that script was never saved permanently). Directionally comparable, not")
    print("a byte-for-byte reproduction of the 9.78%/-8.83%/Calmar 1.11 figure documented "
          "for that exact methodology.")


if __name__ == "__main__":
    main()
