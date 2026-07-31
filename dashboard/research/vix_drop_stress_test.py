"""User critique 2026-07-31 (in Cantonese) of the same-day VIX-condition-drop deploy: raised
three risks and confirmed one idea (VIX-crush exit) was already tested/inert -- not re-tested
here, see dipbuy_refine3.py + HANDOFF.md line ~5189 ("INERT: base exit already..."). This
script answers the three genuinely new, testable claims:

  (1) "2022 slow-bleed" risk: VIX sat mostly 25-30 in 2022 (elevated, not spiking), so
      dropping the VIX-spike gate might let the sleeve repeatedly "catch falling knives" in
      a grinding bear market. -> per-CALENDAR-YEAR breakdown of variant A (with VIX) vs B
      (without), 2022 called out specifically.
  (2) Data-mining / selection-bias concern re: picking variant B after seeing 5 variants'
      results. DSR was ALREADY computed for this exact ablation (see HANDOFF.md) and found
      uninformative at this sample size (saturates 100% for every variant, including the
      worse ones -- can't discriminate). The user's sharper point stands, though: the
      weight-sweep (5/10/15%) I used as "robustness across 6 configs" is mostly the SAME
      underlying trade series linearly rescaled, not 6 independent tests -- weaker evidence
      than it looked. The IS/OOS split and this script's per-year/per-ticker breakdowns are
      the more genuinely independent checks.
  (3) Cost/slippage sensitivity: +54% trade count (736->1134) means more round-trip cost drag
      on a still-small account. -> re-price both variants' trades at 10bp (current), 20bp,
      30bp round-trip cost, see if B's advantage over A survives.

Run: uv run python -u -m dashboard.research.vix_drop_stress_test
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
from dashboard.research.meanrev_filter_ablation_test import (
    _frame, _trades_for_entry_mask, _variant_entries,
)
from dashboard.core.sleeve import SLEEVE_UNIVERSE

# NOTE: sleeve_blend.py's _sleeve_trades() was updated 2026-07-31 to match the NOW-DEPLOYED
# no-VIX spec (it's "exact reproduction of the live function", and the live function changed
# today) -- so it can no longer be used as variant A's ("before today") baseline here. Both A
# and B are reconstructed independently via meanrev_filter_ablation_test.py's own
# _variant_entries(), which hardcodes each formula explicitly and was never touched by
# today's sleeve.py edit.

WEIGHT = 0.10
POS_CAP = 0.25
PORTFOLIO_CAP = 1.0
RISK = 0.01


def _entries_adx(f: dict, adx_thresh: float) -> np.ndarray:
    """Variant B's entry (no VIX, RSI<35) with a PARAMETERIZED ADX threshold instead of the
    fixed >20 -- for the ADX sensitivity sweep."""
    sv, m20, r14v, adxv = f["close"], f["ma20"], f["rsi14"], f["adx14"]
    return (sv < m20 * 0.975) & (r14v < 35) & (adxv > adx_thresh)


def main() -> None:
    tickers = list(SLEEVE_UNIVERSE)
    print(f"Universe: {tickers}\n")

    frames = {tk: _frame(tk) for tk in tickers}
    trades_a: dict[str, list[dict]] = {}
    trades_b: dict[str, list[dict]] = {}
    for tk in tickers:
        f = frames[tk]
        if f is None:
            trades_a[tk] = []; trades_b[tk] = []
            continue
        trades_a[tk] = _trades_for_entry_mask(f, _variant_entries(f, "A_full_filter_LIVE"))  # pre-today (with VIX)
        trades_b[tk] = _trades_for_entry_mask(f, _variant_entries(f, "B_drop_VIX"))          # deployed today (no VIX)

    core_ret, years, n_core = _core_weekly_returns(POS_CAP, PORTFOLIO_CAP, False, RISK)
    didx = core_ret.index

    def _unit(trades_by_tk: dict[str, list[dict]]) -> pd.Series:
        return sum((_sleeve_unit_series(trades_by_tk.get(tk, []), didx) for tk in tickers),
                  pd.Series(0.0, index=didx))

    unit_a = _unit(trades_a)
    unit_b = _unit(trades_b)
    ret_a = core_ret + WEIGHT * unit_a
    ret_b = core_ret + WEIGHT * unit_b

    # ---- (1) per-calendar-year breakdown, 2022 called out -------------------------------
    print("=" * 100)
    print("(1) PER-CALENDAR-YEAR: A (with VIX, pre-today) vs B (no VIX, deployed today)")
    print("=" * 100)
    eq_a = (1 + ret_a).cumprod()
    eq_b = (1 + ret_b).cumprod()
    years_seen = sorted(set(didx.year))
    print(f"\n{'year':<6}{'A yr-ret':>10}{'B yr-ret':>10}{'A intra-yr DD':>16}{'B intra-yr DD':>16}{'A n':>6}{'B n':>6}")
    for yr in years_seen:
        mask = didx.year == yr
        if mask.sum() < 5:
            continue
        ra = ret_a[mask]; rb = ret_b[mask]
        yr_ret_a = (1 + ra).prod() - 1
        yr_ret_b = (1 + rb).prod() - 1
        sub_eq_a = eq_a[mask]; sub_eq_b = eq_b[mask]
        dd_a = (sub_eq_a / sub_eq_a.cummax() - 1).min()
        dd_b = (sub_eq_b / sub_eq_b.cummax() - 1).min()
        na = sum(1 for tk in tickers for t in trades_a.get(tk, []) if pd.Timestamp(t["d"]).year == yr)
        nb = sum(1 for tk in tickers for t in trades_b.get(tk, []) if pd.Timestamp(t["d"]).year == yr)
        flag = "  <-- 2022" if yr == 2022 else ""
        print(f"{yr:<6}{yr_ret_a*100:>9.2f}%{yr_ret_b*100:>9.2f}%{dd_a*100:>15.2f}%{dd_b*100:>15.2f}%{na:>6}{nb:>6}{flag}")

    # ---- (2) note on DSR / selection bias (no new computation -- see docstring) ----------
    print("\n" + "=" * 100)
    print("(2) DSR / selection-bias: already checked (HANDOFF.md) -- naive AND n_trials=5")
    print("    corrected DSR both saturate at 100% for ALL 5 variants (A-E), including the")
    print("    worse ones -- DSR can't discriminate B from A at this sample size (n=736-1508")
    print("    pooled trades each). The user's sharper point: the weight-sweep (5/10/15%) used")
    print("    as 'robustness' is mostly the SAME trade series rescaled, not independent tests.")
    print("    The genuinely independent checks are: IS vs OOS split (already done, both agree)")
    print("    + the per-year breakdown above (11 more quasi-independent windows) + per-ticker")
    print("    consistency (below) + the trade-count increase having a clear, principled")
    print("    mechanism (VIX doesn't always coincide with a real oversold-in-trend setup) --")
    print("    not a data-mined curve-fit with no ex-ante rationale.")
    print("\n    Per-ticker: does B beat A on meanR, ticker by ticker (not just pooled)?")
    for tk in tickers:
        ra = np.array([t["r"] for t in trades_a.get(tk, [])])
        rb = np.array([t["r"] for t in trades_b.get(tk, [])])
        ma = ra.mean() if len(ra) else 0.0
        mb = rb.mean() if len(rb) else 0.0
        print(f"      {tk:<6} A meanR {ma*100:+6.2f}% (n={len(ra):<4}) -> B meanR {mb*100:+6.2f}% "
              f"(n={len(rb):<4}) {'better' if mb > ma else 'worse'}")

    # ---- (3) cost sensitivity -------------------------------------------------------------
    print("\n" + "=" * 100)
    print("(3) COST SENSITIVITY: does B's advantage survive higher round-trip cost?")
    print("=" * 100)
    for extra_cost in (0.0, 0.0010, 0.0020):
        total_cost = COST + extra_cost
        def _repriced(trades_by_tk):
            out = {}
            for tk in tickers:
                out[tk] = [{"d": t["d"], "r": t["r"] - extra_cost} for t in trades_by_tk.get(tk, [])]
            return out
        ua = _unit(_repriced(trades_a)); ub = _unit(_repriced(trades_b))
        ca, da, sa = _metrics(core_ret + WEIGHT * ua, years)
        cb, db, sb = _metrics(core_ret + WEIGHT * ub, years)
        calmar_a = ca / abs(da) if da else 0
        calmar_b = cb / abs(db) if db else 0
        print(f"\n  round-trip cost {total_cost*100:.2f}% (base {COST*100:.2f}% + {extra_cost*100:.2f}% extra):")
        print(f"    A (with VIX): CAGR {ca*100:+7.2f}%  maxDD {da*100:7.2f}%  Calmar {calmar_a:6.3f}")
        print(f"    B (no VIX)  : CAGR {cb*100:+7.2f}%  maxDD {db*100:7.2f}%  Calmar {calmar_b:6.3f}  "
              f"{'B still wins' if calmar_b > calmar_a else 'A wins at this cost'}")

    # ---- (4) ADX threshold sweep ----------------------------------------------------------
    print("\n" + "=" * 100)
    print("(4) ADX THRESHOLD SWEEP (on top of the deployed no-VIX filter): 20 (deployed) / 22 / 25")
    print("=" * 100)
    for adx_thresh in (20, 22, 25):
        trades_adx = {}
        for tk in tickers:
            f = frames[tk]
            if f is None:
                trades_adx[tk] = []
                continue
            ent = _entries_adx(f, adx_thresh)
            trades_adx[tk] = _trades_for_entry_mask(f, ent)
        u = _unit(trades_adx)
        c, d, s = _metrics(core_ret + WEIGHT * u, years)
        calmar = c / abs(d) if d else 0
        n_trades = sum(len(trades_adx[tk]) for tk in tickers)
        tag = " (DEPLOYED TODAY)" if adx_thresh == 20 else ""
        print(f"  ADX>{adx_thresh}{tag}: n={n_trades:<5} CAGR {c*100:+7.2f}%  maxDD {d*100:7.2f}%  "
              f"Sharpe {s:6.3f}  Calmar {calmar:6.3f}")


if __name__ == "__main__":
    main()
