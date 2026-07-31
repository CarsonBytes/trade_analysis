"""User-proposed follow-ups (Cantonese, 2026-07-31) to the VIX-drop deploy: (1) a per-ticker
consecutive-loss cooldown (2 losses -> pause that ticker's sleeve entries for 10 trading
days), specifically aimed at the 2022 "slow bleed" finding; (2) a portfolio-level weekly cap
(max 2 new sleeve entries/week across all 11 tickers, keeping the highest-ADX signals when
more fire) aimed at the account's small size / cost sensitivity. Both are genuinely new
mechanisms, not yet backtested -- this script builds and tests them for real rather than
accepting the predicted magnitudes ("~-2.8% 2022 DD", "~$50-100/yr saved") on assertion.

Run: uv run python -u -m dashboard.research.sleeve_cooldown_weekly_cap_test
"""
from __future__ import annotations
import os
os.environ.setdefault("BROKER", "ib")
os.environ.setdefault("UNIVERSE", "etf")

from collections import defaultdict
import numpy as np
import pandas as pd

from dashboard.research.sleeve_blend import (
    _core_weekly_returns, _sleeve_unit_series, _metrics, COST, STOP_FRAC, TARGET_FRAC, TIME_CAP_DAYS,
)
from dashboard.research.meanrev_filter_ablation_test import _frame, _variant_entries
from dashboard.core.sleeve import SLEEVE_UNIVERSE

WEIGHT = 0.10
POS_CAP = 0.25
PORTFOLIO_CAP = 1.0
RISK = 0.01


def _weekly_cap_allowed(frames: dict, tickers: list[str], max_per_week: int = 2) -> dict[str, set[int]]:
    """Every deployed-spec entry across ALL tickers, grouped by ISO week, keeping only the
    highest-ADX `max_per_week` signals per week (user's own tie-break rule -- 'the strongest
    ADX' when more than the cap fire in one week). Returns allowed bar-indices per ticker;
    callers still walk sequentially and must skip an allowed index that falls inside an
    already-open trade's window, same as the live _has_open() gate."""
    all_entries = []
    for tk in tickers:
        f = frames[tk]
        if f is None:
            continue
        ent = _variant_entries(f, "B_drop_VIX")
        ent = np.nan_to_num(ent).astype(bool); ent[:200] = False
        for i in np.where(ent)[0]:
            all_entries.append((f["idx"][i], tk, int(i), float(f["adx14"][i])))
    by_week: dict[tuple, list] = defaultdict(list)
    for d, tk, i, adx in all_entries:
        iso = d.isocalendar()
        by_week[(iso.year, iso.week)].append((d, tk, i, adx))
    allowed: dict[str, set[int]] = defaultdict(set)
    for wk, items in by_week.items():
        items.sort(key=lambda x: -x[3])
        for d, tk, i, adx in items[:max_per_week]:
            allowed[tk].add(i)
    return allowed


def _walk(f: dict, allowed_idxs: set[int] | None, use_cooldown: bool,
         loss_streak: int = 2, cooldown_days: int = 10) -> list[dict]:
    """General walk: entries must be in `allowed_idxs` (None = all signal bars allowed, i.e.
    no weekly cap) AND not currently in an open trade AND (if use_cooldown) not currently
    cooling down from a recent loss streak on this ticker."""
    sv, m5 = f["close"], f["ma5"]
    ent = _variant_entries(f, "B_drop_VIX")
    ent = np.nan_to_num(ent).astype(bool); ent[:200] = False
    n = len(sv); out = []; i = 200
    consecutive_losses = 0; cooldown_until = -1
    while i < n - 1:
        if not ent[i] or (allowed_idxs is not None and i not in allowed_idxs) or \
           (use_cooldown and i < cooldown_until):
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
        realized = R - COST
        out.append({"d": f["idx"][min(j, n - 1)], "r": realized})
        if use_cooldown:
            if realized < 0:
                consecutive_losses += 1
                if consecutive_losses >= loss_streak:
                    cooldown_until = min(j, n - 1) + cooldown_days
                    consecutive_losses = 0
            else:
                consecutive_losses = 0
        i = j + 1
    return out


def main() -> None:
    tickers = list(SLEEVE_UNIVERSE)
    frames = {tk: _frame(tk) for tk in tickers}
    weekly_allowed = _weekly_cap_allowed(frames, tickers)

    baseline: dict[str, list[dict]] = {}
    cooldown: dict[str, list[dict]] = {}
    weekly_cap: dict[str, list[dict]] = {}
    both: dict[str, list[dict]] = {}
    for tk in tickers:
        f = frames[tk]
        if f is None:
            baseline[tk] = []; cooldown[tk] = []; weekly_cap[tk] = []; both[tk] = []
            continue
        baseline[tk] = _walk(f, None, use_cooldown=False)
        cooldown[tk] = _walk(f, None, use_cooldown=True)
        weekly_cap[tk] = _walk(f, weekly_allowed.get(tk, set()), use_cooldown=False)
        both[tk] = _walk(f, weekly_allowed.get(tk, set()), use_cooldown=True)

    core_ret, years, n_core = _core_weekly_returns(POS_CAP, PORTFOLIO_CAP, False, RISK)
    didx = core_ret.index

    def _unit(trades_by_tk):
        return sum((_sleeve_unit_series(trades_by_tk.get(tk, []), didx) for tk in tickers),
                  pd.Series(0.0, index=didx))

    def _report(label, trades_by_tk):
        u = _unit(trades_by_tk)
        ret = core_ret + WEIGHT * u
        c, d, s = _metrics(ret, years)
        calmar = c / abs(d) if d else 0
        n_tr = sum(len(trades_by_tk.get(tk, [])) for tk in tickers)
        mask2022 = didx.year == 2022
        yr_ret = (1 + ret[mask2022]).prod() - 1
        eq2022 = (1 + ret[mask2022]).cumprod()
        dd2022 = (eq2022 / eq2022.cummax() - 1).min()
        print(f"  {label:<32} n={n_tr:<5} CAGR {c*100:+7.2f}%  maxDD {d*100:7.2f}%  "
              f"Calmar {calmar:6.3f}  | 2022: ret {yr_ret*100:+6.2f}%  DD {dd2022*100:6.2f}%")

    print("=" * 100)
    print("Baseline (deployed today, no cooldown/cap) vs the two proposed mechanisms")
    print("=" * 100)
    _report("baseline (deployed, ADX>20)", baseline)
    _report("+ consecutive-loss cooldown", cooldown)
    _report("+ weekly cap (max 2/wk, top-ADX)", weekly_cap)
    _report("+ both together", both)


if __name__ == "__main__":
    main()
