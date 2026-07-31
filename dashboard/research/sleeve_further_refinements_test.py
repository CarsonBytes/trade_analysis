"""User follow-up (Cantonese, 2026-07-31) to the deployed no-VIX/ADX>25 sleeve: 4 more
optimization ideas. Tests the 3 that fit this project's existing R-multiple backtest
framework; the 4th (dynamic ATR-scaled ETF_POS_CAP) is a DOLLAR position-sizing mechanism at
the broker-execution layer, not expressible in an R-multiple/unit-series backtest without a
much larger rebuild (would need to simulate actual share counts against a live equity curve
and a portfolio-room constraint, not just blend weighted return series) -- explained, not
faked, in the writeup rather than approximated badly.

  (1) ADX-tiered sizing: 0.5% base / 0.75% at ADX 35-50 / 1.0% at ADX>50 (matching VIX>30's
      existing tier). Real distribution check first: ADX>35 = 35.7% of entries, ADX>50 =
      1.5% -- both tiers meaningfully populated, worth testing.
  (2) [SKIPPED -- ATR-dynamic ETF_POS_CAP, see docstring above]
  (3) ADX-decay early exit: if ADX drops <20 after entering above 25, AND unrealized profit
      >1%, exit early (in addition to the existing 4 exit conditions).
  (4) Portfolio-level drawdown cooldown: if the SLEEVE's own trailing-30-day realized R sum
      (across ALL tickers, not per-ticker) drops below -5%, pause ALL new sleeve entries for
      ~1 week. Directly targets the root cause found in the per-ticker cooldown test
      (2022's damage was spread across many tickers, not concentrated in one) with a
      mechanism that actually looks at the aggregate.

Run: uv run python -u -m dashboard.research.sleeve_further_refinements_test
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
from dashboard.research.meanrev_filter_ablation_test import _frame
from dashboard.core.sleeve import SLEEVE_UNIVERSE

WEIGHT = 0.10
POS_CAP = 0.25
PORTFOLIO_CAP = 1.0
RISK = 0.01
ADX_THRESH = 25.0


def _entries(f: dict) -> np.ndarray:
    sv, m20, r14v, adxv = f["close"], f["ma20"], f["rsi14"], f["adx14"]
    ent = (sv < m20 * 0.975) & (r14v < 35) & (adxv > ADX_THRESH)
    ent = np.nan_to_num(ent).astype(bool)
    ent[:200] = False
    return ent


def _walk_baseline(f: dict, ent: np.ndarray) -> list[dict]:
    """Deployed spec, no refinements -- also returns adx-at-entry for sizing tests."""
    sv, m5, adxv = f["close"], f["ma5"], f["adx14"]
    n = len(sv); out = []; i = 200
    while i < n - 1:
        if not ent[i]:
            i += 1; continue
        e = sv[i]; j = i + 1; R = None
        while j < n:
            r = sv[j] / e - 1.0
            if sv[j] >= m5[j] or r >= TARGET_FRAC:
                R = r; break
            if r <= -STOP_FRAC:
                R = -STOP_FRAC; break
            if (j - i) >= TIME_CAP_DAYS:
                R = r; break
            j += 1
        if R is None:
            R = sv[min(j, n - 1)] / e - 1.0
        out.append({"d": f["idx"][min(j, n - 1)], "r": R - COST, "adx_entry": float(adxv[i])})
        i = j + 1
    return out


def _walk_adx_decay_exit(f: dict, ent: np.ndarray) -> list[dict]:
    """Baseline walk + a 5th exit: ADX drops <20 (from having been >25 at entry) while
    unrealized profit >1% -> exit early at that day's close."""
    sv, m5, adxv = f["close"], f["ma5"], f["adx14"]
    n = len(sv); out = []; i = 200
    while i < n - 1:
        if not ent[i]:
            i += 1; continue
        e = sv[i]; j = i + 1; R = None
        while j < n:
            r = sv[j] / e - 1.0
            if sv[j] >= m5[j] or r >= TARGET_FRAC:
                R = r; break
            if r <= -STOP_FRAC:
                R = -STOP_FRAC; break
            if r > 0.01 and adxv[j] < 20:
                R = r; break
            if (j - i) >= TIME_CAP_DAYS:
                R = r; break
            j += 1
        if R is None:
            R = sv[min(j, n - 1)] / e - 1.0
        out.append({"d": f["idx"][min(j, n - 1)], "r": R - COST})
        i = j + 1
    return out


def _tiered_sizing_trades(baseline_by_tk: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Reweights each baseline trade's R by an ADX-tier sizing multiplier (relative to the
    0.5% base): 1.0x at ADX 25-35, 1.5x at 35-50, 2.0x at ADX>50 (matching VIX>30's existing
    2.0x tier). Same trades/timing as baseline -- only the $ contribution changes."""
    out = {}
    for tk, trades in baseline_by_tk.items():
        scaled = []
        for t in trades:
            adx = t["adx_entry"]
            mult = 2.0 if adx > 50 else (1.5 if adx > 35 else 1.0)
            scaled.append({"d": t["d"], "r": t["r"] * mult})
        out[tk] = scaled
    return out


def _portfolio_cooldown_trades(frames: dict, tickers: list[str], dd_thresh: float = -0.05,
                               window_days: int = 30, pause_days: int = 7) -> dict[str, list[dict]]:
    """Sequential, cross-ticker, chronological walk: before allowing a new entry, checks the
    SLEEVE's own trailing-window_days realized-R sum (summed across ALL tickers' closed
    trades so far, not per-ticker) -- if below dd_thresh, blocks every new sleeve entry
    (all tickers) until pause_days after the triggering exit."""
    candidates = []
    for tk in tickers:
        f = frames[tk]
        if f is None:
            continue
        ent = _entries(f)
        for i in np.where(ent)[0]:
            candidates.append((f["idx"][i], tk, int(i)))
    candidates.sort(key=lambda x: x[0])

    open_until = {tk: -1 for tk in tickers}
    closed: list[tuple] = []          # (exit_date, r)
    out_by_ticker: dict[str, list[dict]] = {tk: [] for tk in tickers}
    pause_until = None

    for d, tk, i in candidates:
        if i < open_until[tk]:
            continue
        if pause_until is not None and d < pause_until:
            continue
        f = frames[tk]
        sv, m5 = f["close"], f["ma5"]
        n = len(sv)
        e = sv[i]; j = i + 1; R = None
        while j < n:
            r = sv[j] / e - 1.0
            if sv[j] >= m5[j] or r >= TARGET_FRAC:
                R = r; break
            if r <= -STOP_FRAC:
                R = -STOP_FRAC; break
            if (j - i) >= TIME_CAP_DAYS:
                R = r; break
            j += 1
        if R is None:
            R = sv[min(j, n - 1)] / e - 1.0
        realized = R - COST
        exit_d = f["idx"][min(j, n - 1)]
        out_by_ticker[tk].append({"d": exit_d, "r": realized})
        open_until[tk] = j + 1
        closed.append((exit_d, realized))

        cutoff = exit_d - pd.Timedelta(days=window_days)
        rolling = sum(r for dt_, r in closed if dt_ >= cutoff)
        if rolling < dd_thresh:
            pause_until = exit_d + pd.Timedelta(days=pause_days)

    return out_by_ticker


def main() -> None:
    tickers = list(SLEEVE_UNIVERSE)
    frames = {tk: _frame(tk) for tk in tickers}

    baseline: dict[str, list[dict]] = {}
    adx_decay: dict[str, list[dict]] = {}
    for tk in tickers:
        f = frames[tk]
        if f is None:
            baseline[tk] = []; adx_decay[tk] = []
            continue
        ent = _entries(f)
        baseline[tk] = _walk_baseline(f, ent)
        adx_decay[tk] = _walk_adx_decay_exit(f, ent)
    tiered_sizing = _tiered_sizing_trades(baseline)
    port_cooldown = _portfolio_cooldown_trades(frames, tickers)

    core_ret, years, n_core = _core_weekly_returns(POS_CAP, PORTFOLIO_CAP, False, RISK)
    didx = core_ret.index
    cut = didx[0] + (didx[-1] - didx[0]) * 0.6
    oos_yrs = (didx[-1] - cut).days / 365.25
    core_oos = core_ret[core_ret.index >= cut]

    def _unit(trades_by_tk):
        return sum((_sleeve_unit_series(trades_by_tk.get(tk, []), didx) for tk in tickers),
                  pd.Series(0.0, index=didx))

    def _report(label, trades_by_tk):
        u = _unit(trades_by_tk)
        ret = core_ret + WEIGHT * u
        c, d, s = _metrics(ret, years)
        calmar = c / abs(d) if d else 0
        u_oos = u[u.index >= cut]
        ret_oos = core_oos + WEIGHT * u_oos
        c_oos, d_oos, _ = _metrics(ret_oos, oos_yrs)
        calmar_oos = c_oos / abs(d_oos) if d_oos else 0
        n_tr = sum(len(trades_by_tk.get(tk, [])) for tk in tickers)
        mask2022 = didx.year == 2022
        yr_ret = (1 + ret[mask2022]).prod() - 1
        eq2022 = (1 + ret[mask2022]).cumprod()
        dd2022 = (eq2022 / eq2022.cummax() - 1).min() if len(eq2022) else 0.0
        print(f"  {label:<34} n={n_tr:<5} FULL CAGR {c*100:+7.2f}% DD {d*100:7.2f}% Calmar {calmar:6.3f} "
              f"| OOS Calmar {calmar_oos:6.3f} | 2022 ret {yr_ret*100:+6.2f}% DD {dd2022*100:6.2f}%")

    print("=" * 100)
    print("Baseline (deployed, no-VIX + ADX>25) vs 3 further refinements")
    print("=" * 100)
    _report("baseline (deployed today)", baseline)
    _report("(1) ADX-tiered sizing", tiered_sizing)
    _report("(3) ADX-decay early exit (>1% profit, ADX<20)", adx_decay)
    _report("(4) portfolio-level 30d drawdown cooldown", port_cooldown)
    print("\n(2) ATR-dynamic ETF_POS_CAP: NOT TESTED -- this is a dollar position-sizing")
    print("    mechanism at the broker-execution layer, not expressible in this R-multiple/")
    print("    unit-series backtest framework without simulating real share counts against a")
    print("    live equity curve and portfolio-room constraint. Would need a genuinely")
    print("    different (much larger) backtest harness -- flagged, not approximated.")


if __name__ == "__main__":
    main()
