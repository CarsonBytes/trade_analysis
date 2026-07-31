"""Follow-up to the 2026-07-31 ADX>20->25 deploy: user asked whether an EVEN HIGHER ADX
threshold does better still. Sweeps a wider, finer grid (15/18/20/22/25/28/30/33/35/40) on
top of the deployed no-VIX filter, full-history AND OOS, to find where the risk-adjusted
peak actually sits rather than assuming 25 (the first value tested) is optimal.

Run: uv run python -u -m dashboard.research.sleeve_adx_sweep_test
"""
from __future__ import annotations
import os
os.environ.setdefault("BROKER", "ib")
os.environ.setdefault("UNIVERSE", "etf")

import numpy as np
import pandas as pd

from dashboard.research.sleeve_blend import (
    _core_weekly_returns, _sleeve_unit_series, _metrics, COST, STOP_FRAC, TARGET_FRAC, TIME_CAP_DAYS,
)
from dashboard.research.meanrev_filter_ablation_test import _frame, _trades_for_entry_mask
from dashboard.core.sleeve import SLEEVE_UNIVERSE

WEIGHT = 0.10
POS_CAP = 0.25
PORTFOLIO_CAP = 1.0
RISK = 0.01
GRID = (15, 18, 20, 22, 25, 28, 30, 33, 35, 40)


def _entries_adx(f: dict, adx_thresh: float) -> np.ndarray:
    sv, m20, r14v, adxv = f["close"], f["ma20"], f["rsi14"], f["adx14"]
    ent = (sv < m20 * 0.975) & (r14v < 35) & (adxv > adx_thresh)
    ent = np.nan_to_num(ent).astype(bool)
    ent[:200] = False
    return ent


def main() -> None:
    tickers = list(SLEEVE_UNIVERSE)
    frames = {tk: _frame(tk) for tk in tickers}

    core_ret, years, n_core = _core_weekly_returns(POS_CAP, PORTFOLIO_CAP, False, RISK)
    didx = core_ret.index
    cut = didx[0] + (didx[-1] - didx[0]) * 0.6
    oos_yrs = (didx[-1] - cut).days / 365.25
    core_oos = core_ret[core_ret.index >= cut]

    def _unit(trades_by_tk):
        return sum((_sleeve_unit_series(trades_by_tk.get(tk, []), didx) for tk in tickers),
                  pd.Series(0.0, index=didx))

    print(f"Universe: {tickers}\n")
    print("=" * 100)
    print(f"ADX SWEEP (weight={WEIGHT:.0%}, on top of the deployed no-VIX filter)")
    print("=" * 100)
    header = f"\n{'ADX>':>6}{'n':>7}{'FULL CAGR':>11}{'FULL DD':>10}{'FULL Calmar':>13}{'OOS CAGR':>11}{'OOS DD':>9}{'OOS Calmar':>12}{'min tickers n':>16}"
    print(header)
    best_full = (None, -999)
    best_oos = (None, -999)
    for adx_thresh in GRID:
        trades_by_tk = {}
        for tk in tickers:
            f = frames[tk]
            if f is None:
                trades_by_tk[tk] = []
                continue
            ent = _entries_adx(f, adx_thresh)
            trades_by_tk[tk] = _trades_for_entry_mask(f, ent)
        n_by_tk = {tk: len(trades_by_tk[tk]) for tk in tickers}
        n_total = sum(n_by_tk.values())
        min_tk_n = min(n_by_tk.values())

        u = _unit(trades_by_tk)
        ret = core_ret + WEIGHT * u
        c, d, s = _metrics(ret, years)
        calmar = c / abs(d) if d else 0

        u_oos = u[u.index >= cut]
        ret_oos = core_oos + WEIGHT * u_oos
        c_oos, d_oos, s_oos = _metrics(ret_oos, oos_yrs)
        calmar_oos = c_oos / abs(d_oos) if d_oos else 0

        tag = " (DEPLOYED)" if adx_thresh == 25 else ""
        print(f"{adx_thresh:>6}{n_total:>7}{c*100:>10.2f}%{d*100:>9.2f}%{calmar:>13.3f}"
              f"{c_oos*100:>10.2f}%{d_oos*100:>8.2f}%{calmar_oos:>12.3f}{min_tk_n:>16}{tag}")

        if calmar > best_full[1]:
            best_full = (adx_thresh, calmar)
        if calmar_oos > best_oos[1]:
            best_oos = (adx_thresh, calmar_oos)

    print(f"\nBest full-history Calmar: ADX>{best_full[0]} ({best_full[1]:.3f})")
    print(f"Best OOS Calmar:          ADX>{best_oos[0]} ({best_oos[1]:.3f})")


if __name__ == "__main__":
    main()
