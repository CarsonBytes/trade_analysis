"""User critique 2026-07-31 (in Chinese): the panic dip-buy sleeve is a CONDITIONAL mean-
reversion strategy (only fires on a VIX panic spike + ADX-confirmed trend, not on any
deviation). Proposed testing "pure" unconditional mean-reversion (Bollinger-band touch,
RSI<30/>70, Z-score >2sd) instead -- flagged themselves that unconditional MR "catches
falling knives" (逆勢接刀) in a strong trend and may conflict with the trend-following core
book. Also proposed pairs/stat-arb, which they correctly self-identified as already tested
and rejected (pairs_test.py / refined_statarb_test.py, OOS Sharpe <=0.54, DSR <=17%,
negative at realistic cost) -- NOT re-tested here, no new premise to justify re-running it.

This script answers the mean-reversion question as a clean ABLATION on the sleeve's own
proven, already-live logic: same universe, same exit structure (5MA-touch/+3%TP/-5%SL/10d
cap), same cost -- the ONLY thing that changes between variants is the ENTRY filter. That
isolates exactly what the user is asking: is the VIX-panic + ADX-trend confirmation
actually earning its keep, or would looser/unconditional entries do as well or better?

  A  full filter (LIVE today):  close<20MA*0.975 & VIX+15%/5d & RSI14<35 & ADX>20
  B  drop VIX only:              close<20MA*0.975 &              RSI14<35 & ADX>20
  C  drop ADX only:              close<20MA*0.975 & VIX+15%/5d & RSI14<35
  D  unconditional RSI (user's proposal): RSI14<30, no MA/VIX/ADX condition at all
  E  unconditional Bollinger/Z-score (user's proposal): close < MA20 - 2*std20, no
     VIX/ADX condition (mathematically a 2-sigma Z-score threshold, same thing the user
     described as two separate ideas)

Run: uv run python -u -m dashboard.research.meanrev_filter_ablation_test
     uv run python -u -m dashboard.research.meanrev_filter_ablation_test --oos --weight 0.05,0.10
"""
from __future__ import annotations
import os
os.environ.setdefault("BROKER", "ib")
os.environ.setdefault("UNIVERSE", "etf")

import argparse
import numpy as np
import pandas as pd
import yfinance as yf

from dashboard.research.sleeve_blend import (
    _core_weekly_returns, _sleeve_trades, _sleeve_unit_series, _metrics, COST,
    STOP_FRAC, TARGET_FRAC, TIME_CAP_DAYS,
)
from dashboard.core.sleeve import SLEEVE_UNIVERSE


def _frame(ticker: str) -> dict | None:
    p = yf.download([ticker, "^VIX"], period="max", interval="1d", progress=False, auto_adjust=True)["Close"]
    if ticker not in p.columns:
        return None
    s = p[ticker].dropna()
    v = p["^VIX"].reindex(s.index).ffill()
    ph = yf.download(ticker, period="max", interval="1d", progress=False, auto_adjust=True)
    if hasattr(ph.columns, "nlevels") and ph.columns.nlevels > 1:
        ph.columns = ph.columns.get_level_values(0)
    h, lo = ph["High"].reindex(s.index), ph["Low"].reindex(s.index)
    if s.index.tz is None:
        naive_idx = s.index
        s.index = naive_idx.tz_localize("UTC")
        v.index = s.index; h.index = s.index; lo.index = s.index
    ma5 = s.rolling(5).mean(); ma20 = s.rolling(20).mean(); std20 = s.rolling(20).std()
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
    return {"idx": s.index, "close": s.values, "vix": v.values, "ma5": ma5.values,
            "ma20": ma20.values, "std20": std20.values, "rsi14": r14.values, "adx14": adx.values}


def _trades_for_entry_mask(f: dict, ent: np.ndarray) -> list[dict]:
    """Shared walk-forward exit simulator -- IDENTICAL exit structure for every variant
    (5MA-touch / +3% TP / -5% SL / 10-trading-day cap), so only the entry mask differs."""
    sv, m5 = f["close"], f["ma5"]
    ent = np.nan_to_num(ent).astype(bool)
    ent[:200] = False
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
        out.append({"d": f["idx"][min(j, n - 1)], "r": R - COST})
        i = j + 1
    return out


def _variant_entries(f: dict, variant: str) -> np.ndarray:
    sv, vv, m20, std20, r14v, adxv = (f["close"], f["vix"], f["ma20"], f["std20"],
                                       f["rsi14"], f["adx14"])
    vix_up = vv / np.roll(vv, 5) - 1.0
    below_ma = sv < m20 * 0.975
    if variant == "A_full_filter_LIVE":
        return below_ma & (vix_up > 0.15) & (r14v < 35) & (adxv > 20)
    if variant == "B_drop_VIX":
        return below_ma & (r14v < 35) & (adxv > 20)
    if variant == "C_drop_ADX":
        return below_ma & (vix_up > 0.15) & (r14v < 35)
    if variant == "D_unconditional_RSI30":
        return r14v < 30
    if variant == "E_unconditional_bollinger_2sigma":
        return sv < (m20 - 2 * std20)
    raise ValueError(variant)


VARIANTS = ["A_full_filter_LIVE", "B_drop_VIX", "C_drop_ADX",
            "D_unconditional_RSI30", "E_unconditional_bollinger_2sigma"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pos-cap", type=float, default=0.25)
    ap.add_argument("--portfolio-cap", type=float, default=1.0)
    ap.add_argument("--weight", type=str, default="0.10")
    ap.add_argument("--risk", type=float, default=0.01)
    ap.add_argument("--oos", action="store_true")
    args = ap.parse_args()
    weights = [float(w) for w in args.weight.split(",")]

    tickers = list(SLEEVE_UNIVERSE)
    print(f"Universe: {tickers} (the live sleeve's own universe -- exit structure held "
          f"IDENTICAL across all 5 variants; only the entry filter changes)\n")
    print("=" * 100)
    print("PER-TICKER: n trades / mean R / win rate, by entry-filter variant")
    print("=" * 100)

    trades_by_variant: dict[str, dict[str, list[dict]]] = {v: {} for v in VARIANTS}
    conflict_counts: dict[str, int] = {v: 0 for v in VARIANTS}

    for tk in tickers:
        f = _frame(tk)
        print(f"\n{tk}:")
        if f is None:
            print("  (no data)")
            continue
        for v in VARIANTS:
            if v == "A_full_filter_LIVE":
                trs = _sleeve_trades(tk)          # exact live function -- ground truth, not reimplemented
            else:
                ent = _variant_entries(f, v)
                trs = _trades_for_entry_mask(f, ent)
            trades_by_variant[v][tk] = trs
            rs = np.array([t["r"] for t in trs]) if trs else np.array([])
            print(f"  {v:<32} n={len(rs):<5} meanR {rs.mean()*100 if len(rs) else 0:+6.2f}% "
                  f"win {(rs>0).mean()*100 if len(rs) else 0:4.0f}%")

    # ---- how often does a looser variant fire a NEW entry the ADX/VIX filters would have
    # blocked while price is in a genuine strong downtrend (ADX>25) -- i.e. how often is the
    # "catching a falling knife" risk the user described actually present in the extra
    # trades a looser filter adds. ----
    print("\n" + "=" * 100)
    print("\"FALLING KNIFE\" CHECK: of the trades EACH LOOSER VARIANT ADDS beyond the live "
          "filter (A), what fraction fire while ADX>25 (a genuinely strong trend, not just "
          "chop) -- the exact scenario the user flagged as dangerous for unconditional MR")
    print("=" * 100)
    for tk in tickers:
        f = _frame(tk)
        if f is None:
            continue
        a_dates = {t["d"] for t in trades_by_variant["A_full_filter_LIVE"].get(tk, [])}
        for v in ["D_unconditional_RSI30", "E_unconditional_bollinger_2sigma"]:
            trs = trades_by_variant[v].get(tk, [])
            added = [t for t in trs if t["d"] not in a_dates]
            if not added:
                continue
            # approximate ADX-at-entry by re-deriving entry index from exit date is lossy;
            # instead re-walk entries directly for a strong-trend tag
            ent = _variant_entries(f, v)
            ent = np.nan_to_num(ent).astype(bool); ent[:200] = False
            strong_trend_entries = int((ent & (f["adx14"] > 25)).sum())
            total_entries = int(ent.sum())
            pct = strong_trend_entries / total_entries * 100 if total_entries else 0
            print(f"  {tk} {v}: {len(added)} extra trades vs live filter, "
                  f"{strong_trend_entries}/{total_entries} ({pct:.0f}%) of its entries fire "
                  f"during ADX>25 (strong trend)")

    # ---- portfolio blend ---------------------------------------------------------------
    print("\n" + "=" * 100)
    print("PORTFOLIO BLEND: core book + each variant sleeve, at the given weight(s)")
    print("=" * 100)
    core_ret, years, n_core = _core_weekly_returns(args.pos_cap, args.portfolio_cap, False, args.risk)
    didx = core_ret.index
    c_cagr, c_dd, c_sh = _metrics(core_ret, years)
    c_calmar = c_cagr / abs(c_dd) if c_dd else 0
    print(f"\ncore only: CAGR {c_cagr*100:+.2f}%  maxDD {c_dd*100:.2f}%  Sharpe {c_sh:.3f}  "
          f"Calmar {c_calmar:.3f}  ({n_core} signals, {years:.1f}y)")

    def _row(label: str, v: str, w: float, ret_base: pd.Series, yrs: float, base_cagr: float, base_dd: float) -> None:
        unit = sum((_sleeve_unit_series(trades_by_variant[v].get(tk, []), ret_base.index)
                   for tk in tickers), pd.Series(0.0, index=ret_base.index))
        n_trades = sum(len(trades_by_variant[v].get(tk, [])) for tk in tickers)
        ret = ret_base + w * unit
        cagr, dd, sh = _metrics(ret, yrs)
        calmar = cagr / abs(dd) if dd else 0
        beats = "BEATS live-filter (A)" if (v != "A_full_filter_LIVE" and cagr > base_cagr
                                            and abs(dd) <= abs(base_dd) * 1.01) else ""
        print(f"  w={w:.0%}  {label:<38} n={n_trades:<5} CAGR {cagr*100:+7.2f}%  "
              f"maxDD {dd*100:7.2f}%  Sharpe {sh:6.3f}  Calmar {calmar:6.3f}  {beats}")

    for w in weights:
        print(f"\n-- weight {w:.0%} --")
        a_unit = sum((_sleeve_unit_series(trades_by_variant["A_full_filter_LIVE"].get(tk, []), didx)
                     for tk in tickers), pd.Series(0.0, index=didx))
        a_cagr, a_dd, _ = _metrics(core_ret + w * a_unit, years)
        for v in VARIANTS:
            label = {"A_full_filter_LIVE": "A: full filter (LIVE today)",
                     "B_drop_VIX": "B: drop VIX condition",
                     "C_drop_ADX": "C: drop ADX condition",
                     "D_unconditional_RSI30": "D: unconditional RSI<30",
                     "E_unconditional_bollinger_2sigma": "E: unconditional Bollinger 2-sigma"}[v]
            _row(label, v, w, core_ret, years, a_cagr, a_dd)

    if args.oos:
        print("\n" + "=" * 100)
        print("OOS (last 40% of the core book's date range)")
        print("=" * 100)
        cut = didx[0] + (didx[-1] - didx[0]) * 0.6
        oos_yrs = (didx[-1] - cut).days / 365.25
        core_oos = core_ret[core_ret.index >= cut]
        oc_cagr, oc_dd, oc_sh = _metrics(core_oos, oos_yrs)
        oc_calmar = oc_cagr / abs(oc_dd) if oc_dd else 0
        print(f"\ncore only (OOS): CAGR {oc_cagr*100:+.2f}%  maxDD {oc_dd*100:.2f}%  "
              f"Sharpe {oc_sh:.3f}  Calmar {oc_calmar:.3f}  ({oos_yrs:.1f}y)")

        def _row_oos(label: str, v: str, w: float, base_cagr: float, base_dd: float) -> None:
            unit = sum((_sleeve_unit_series(trades_by_variant[v].get(tk, []), didx)
                       for tk in tickers), pd.Series(0.0, index=didx))
            unit_oos = unit[unit.index >= cut]
            n_trades = int((unit_oos != 0).sum())
            ret = core_oos + w * unit_oos
            cagr, dd, sh = _metrics(ret, oos_yrs)
            calmar = cagr / abs(dd) if dd else 0
            beats = "BEATS live-filter (A)" if (v != "A_full_filter_LIVE" and cagr > base_cagr
                                                and abs(dd) <= abs(base_dd) * 1.01) else ""
            print(f"  w={w:.0%}  {label:<38} n(bars)={n_trades:<5} CAGR {cagr*100:+7.2f}%  "
                  f"maxDD {dd*100:7.2f}%  Sharpe {sh:6.3f}  Calmar {calmar:6.3f}  {beats}")

        for w in weights:
            print(f"\n-- weight {w:.0%} --")
            a_unit = sum((_sleeve_unit_series(trades_by_variant["A_full_filter_LIVE"].get(tk, []), didx)
                         for tk in tickers), pd.Series(0.0, index=didx))
            a_unit_oos = a_unit[a_unit.index >= cut]
            a_cagr, a_dd, _ = _metrics(core_oos + w * a_unit_oos, oos_yrs)
            for v in VARIANTS:
                label = {"A_full_filter_LIVE": "A: full filter (LIVE today)",
                         "B_drop_VIX": "B: drop VIX condition",
                         "C_drop_ADX": "C: drop ADX condition",
                         "D_unconditional_RSI30": "D: unconditional RSI<30",
                         "E_unconditional_bollinger_2sigma": "E: unconditional Bollinger 2-sigma"}[v]
                _row_oos(label, v, w, a_cagr, a_dd)


if __name__ == "__main__":
    main()
