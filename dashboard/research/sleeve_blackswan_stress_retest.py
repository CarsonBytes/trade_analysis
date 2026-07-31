"""Re-verification of the 2026-07-18 black-swan slippage stress test (an ad-hoc script that
was never saved permanently -- this rebuilds it) against the CURRENT sleeve spec (no VIX
entry condition, ADX>25, deployed 2026-07-31). Original question: does the sleeve stay
positive-EV under extreme black-swan slippage on its highest-VIX (VIX>40) entries -- an
extremely pessimistic assumption for SPY/QQQ/XLK specifically, but a real one to check given
VIX>40 entries aren't rare historically (real 2008/2020 fills up to VIX 82.7).

Run: uv run python -u -m dashboard.research.sleeve_blackswan_stress_retest
"""
from __future__ import annotations
import os
os.environ.setdefault("BROKER", "ib")
os.environ.setdefault("UNIVERSE", "etf")

import numpy as np
import yfinance as yf

from dashboard.research.sleeve_blend import (
    COST, STOP_FRAC, TARGET_FRAC, TIME_CAP_DAYS, _core_weekly_returns, _sleeve_unit_series,
    _metrics,
)
from dashboard.core.sleeve import SLEEVE_UNIVERSE, ADX_THRESHOLD
import pandas as pd

WEIGHT = 0.10
POS_CAP = 0.25
PORTFOLIO_CAP = 1.0
RISK = 0.01

BLACKSWAN_ROUND_TRIP_COST = 0.05   # 500bps, same pessimistic assumption as 2026-07-18


def _sleeve_trades_with_vix(ticker: str) -> list[dict]:
    """Same as sleeve_blend.py's _sleeve_trades() (current live spec: no VIX entry
    condition, ADX>ADX_THRESHOLD), but also records vix_at_entry per trade so black-swan
    (VIX>40) entries can be identified after the fact."""
    p = yf.download([ticker, "^VIX"], period="max", interval="1d", progress=False, auto_adjust=True)["Close"]
    if ticker not in p.columns:
        return []
    s = p[ticker].dropna()
    v = p["^VIX"].reindex(s.index).ffill()
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
    sv, vv, m5v, m20v = s.values, v.values, ma5.values, ma20.values
    r14v, adxv, idx = r14.values, adx.values, s.index
    ent = (sv < m20v * 0.975) & (r14v < 35) & (adxv > ADX_THRESHOLD)
    ent = np.nan_to_num(ent).astype(bool)
    ent[:200] = False
    n = len(sv); out = []; i = 200
    while i < n - 1:
        if not ent[i]:
            i += 1; continue
        e = sv[i]; j = i + 1; R = None
        while j < n:
            r = sv[j] / e - 1.0
            if sv[j] >= m5v[j] or r >= TARGET_FRAC:
                R = r; break
            if r <= -STOP_FRAC:
                R = -STOP_FRAC; break
            if (j - i) >= TIME_CAP_DAYS:
                R = r; break
            j += 1
        if R is None:
            R = sv[min(j, n - 1)] / e - 1.0
        out.append({"d": idx[min(j, n - 1)], "r": R - COST, "vix_at_entry": float(vv[i])})
        i = j + 1
    return out


def main() -> None:
    tickers = list(SLEEVE_UNIVERSE)
    all_trades = []
    for tk in tickers:
        trs = _sleeve_trades_with_vix(tk)
        all_trades += trs

    print(f"Universe: {tickers} (current live spec: no VIX entry gate, ADX>{ADX_THRESHOLD})")
    print(f"Total sleeve trades: {len(all_trades)}")

    blackswan = [t for t in all_trades if t["vix_at_entry"] > 40]
    normal = [t for t in all_trades if t["vix_at_entry"] <= 40]
    print(f"VIX>40 at entry: {len(blackswan)} of {len(all_trades)} "
          f"({len(blackswan)/len(all_trades)*100:.1f}%)")
    if blackswan:
        max_vix = max(t["vix_at_entry"] for t in blackswan)
        print(f"Max VIX at entry among these: {max_vix:.1f}")

    def _stats(trades, label):
        rs = np.array([t["r"] for t in trades])
        print(f"  {label}: n={len(rs)} meanR {rs.mean()*100:+.2f}% win {(rs>0).mean()*100:.0f}%")
        return rs

    print("\nBASE (no extra stress):")
    rs_base_bs = _stats(blackswan, "black-swan (VIX>40) cohort")
    rs_base_all = _stats(all_trades, "all trades")

    stressed = [{"d": t["d"], "r": t["r"] - BLACKSWAN_ROUND_TRIP_COST} for t in blackswan] + \
              [{"d": t["d"], "r": t["r"]} for t in normal]
    print(f"\nSTRESSED ({BLACKSWAN_ROUND_TRIP_COST*10000:.0f}bps / {BLACKSWAN_ROUND_TRIP_COST*100:.0f}% "
          f"extra round-trip cost, applied ONLY to the {len(blackswan)} black-swan (VIX>40) entries):")
    rs_stressed_bs = np.array([t["r"] - BLACKSWAN_ROUND_TRIP_COST for t in blackswan])
    print(f"  black-swan cohort under stress: meanR {rs_stressed_bs.mean()*100:+.2f}% "
          f"win {(rs_stressed_bs>0).mean()*100:.0f}%")
    rs_stressed_all = np.array([t["r"] for t in stressed])
    print(f"  all trades under stress: n={len(rs_stressed_all)} "
          f"meanR {rs_stressed_all.mean()*100:+.2f}% win {(rs_stressed_all>0).mean()*100:.0f}%")

    # proper blended-CAGR contribution (same methodology as every other backtest this
    # session: core book + weight*sleeve-unit-series -> real CAGR/DD/Calmar), not a crude
    # meanR-x-frequency approximation -- comparable to today's other reported figures.
    core_ret, years, n_core = _core_weekly_returns(POS_CAP, PORTFOLIO_CAP, False, RISK)
    didx = core_ret.index
    base_unit = _sleeve_unit_series(all_trades, didx)
    stressed_unit = _sleeve_unit_series(stressed, didx)
    c_base, d_base, s_base = _metrics(core_ret + WEIGHT * base_unit, years)
    c_str, d_str, s_str = _metrics(core_ret + WEIGHT * stressed_unit, years)
    calmar_base = c_base / abs(d_base) if d_base else 0
    calmar_str = c_str / abs(d_str) if d_str else 0
    print(f"\nBlended core+sleeve@{WEIGHT:.0%} (pos_cap={POS_CAP:.0%}), same methodology as "
          f"today's other backtests:")
    print(f"  base (no stress)   : CAGR {c_base*100:+.2f}%  maxDD {d_base*100:.2f}%  Calmar {calmar_base:.3f}")
    print(f"  stressed (500bps on VIX>40 entries): CAGR {c_str*100:+.2f}%  maxDD {d_str*100:.2f}%  "
          f"Calmar {calmar_str:.3f}")

    verdict = "STAYS POSITIVE-EV" if rs_stressed_all.mean() > 0 else "FLIPS NEGATIVE"
    print(f"\nVerdict: {verdict} under {BLACKSWAN_ROUND_TRIP_COST*100:.0f}% stress on "
          f"VIX>40 entries (current live spec, ADX>{ADX_THRESHOLD}, no VIX entry gate).")


if __name__ == "__main__":
    main()
