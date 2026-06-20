"""Rigorous validation of Method 3: FX weekly mean-reversion.

The first test showed an INVERTED IS/OOS (IS -0.07, OOS +0.24) -- a red flag that
the edge may be recent-regime luck, not robust. This interrogates that:
  1. per-CALENDAR-ERA expectancy (is it positive across decades, or only recently?)
  2. breadth across FX pairs (broad, or 1-2 pairs?)
  3. parameter robustness (does it survive a small sweep + multi-trial DSR?)

Run: uv run python -u -m dashboard.ab_fx_validate
"""
from __future__ import annotations
from . import net  # noqa: F401
import warnings
warnings.filterwarnings("ignore")
import yfinance as yf
import pandas as pd
from metrics import deflated_sharpe_ratio
from .instruments import UNIVERSE
from . import paper
from .replay import _resolve_daily
from .ab_meanrev import _rsi, _atr, MA, BB_K, HORIZON
from .ab_regime import _adx


def _walk_dated(df, adx_max, sl_atr, rsi_lo, rsi_hi):
    """Mean-reversion walk returning [(entry_date, R)]."""
    c = df["close"]
    ma = c.rolling(MA).mean(); sd = c.rolling(MA).std()
    up, lo = ma + BB_K * sd, ma - BB_K * sd
    rsi, atr, adx = _rsi(c), _atr(df), _adx(df)
    n = len(df); i = MA + 5; out = []
    while i < n - 1:
        a = adx.iloc[i]
        if not (a == a) or a >= adx_max:
            i += 1; continue
        price = c.iloc[i]; m = ma.iloc[i]; at = atr.iloc[i]
        if not (at > 0) or not (m == m):
            i += 1; continue
        if price < lo.iloc[i] and rsi.iloc[i] < rsi_lo:
            d, tp, sl = "long", m, price - sl_atr * at
        elif price > up.iloc[i] and rsi.iloc[i] > rsi_hi:
            d, tp, sl = "short", m, price + sl_atr * at
        else:
            i += 1; continue
        if (d == "long" and not (tp > price and sl < price)) or \
           (d == "short" and not (tp < price and sl > price)):
            i += 1; continue
        o = _resolve_daily(d, price, sl, tp, df.iloc[i + 1: i + 1 + HORIZON])
        if o is None:
            break
        out.append((df.index[i], paper.r_multiple(d, price, sl, o[1])))
        i += o[2] + 1
    return out


def _fx_data():
    out = {}
    for inst in UNIVERSE:
        if inst.asset_class != "fx":
            continue
        raw = yf.download(inst.yf, period="max", interval="1wk",
                          progress=False, auto_adjust=True)
        if raw is None or len(raw) < 220:
            continue
        if raw.columns.nlevels > 1:
            raw.columns = raw.columns.get_level_values(0)
        df = raw[["Open", "High", "Low", "Close"]].copy()
        df.columns = ["open", "high", "low", "close"]
        out[inst.key] = df.dropna()
    return out


def main():
    data = _fx_data()
    print(f"FX weekly mean-reversion validation ({len(data)} pairs)\n")
    e = lambda x: sum(x) / len(x) if x else 0.0
    w = lambda x: sum(1 for v in x if v > 0) / len(x) if x else 0.0

    # base config
    base = (25, 1.5, 35, 65)
    dated = []
    per_inst = {}
    for k, df in data.items():
        rows = _walk_dated(df, *base)
        per_inst[k] = [r for _, r in rows]
        dated += rows

    # 1) per-calendar-era
    print("1) PER-ERA expectancy (is the edge persistent or recent-only?):")
    eras = [("2000-2007", 2000, 2008), ("2008-2013", 2008, 2014),
            ("2014-2019", 2014, 2020), ("2020-2026", 2020, 2027)]
    for label, y0, y1 in eras:
        rs = [r for dt, r in dated if y0 <= dt.year < y1]
        print(f"   {label}: n={len(rs):4} expR={e(rs):+.3f} win={w(rs)*100:3.0f}%")

    # 2) breadth across pairs
    print("\n2) BREADTH across FX pairs:")
    pos = neg = 0
    for k, rs in sorted(per_inst.items(), key=lambda kv: -e(kv[1])):
        if len(rs) < 8:
            continue
        pos += e(rs) > 0; neg += e(rs) <= 0
        print(f"   {k:8} n={len(rs):3} expR={e(rs):+.3f}")
    print(f"   -> positive: {pos} | negative: {neg}")

    # 3) parameter robustness (OOS + multi-trial DSR)
    print("\n3) PARAMETER ROBUSTNESS (OOS, DSR penalised for the sweep):")
    variants = [(25, 1.5, 35, 65), (25, 1.5, 30, 70), (20, 1.5, 35, 65),
                (30, 1.5, 35, 65), (25, 2.0, 35, 65)]
    print(f"   {'adx<,sl,rsi':<18}{'OOS_expR':>9}{'OOS_n':>7}{'DSR':>7}")
    for v in variants:
        is_r, oos_r = [], []
        for k, df in data.items():
            cut = int(len(df) * 0.6)
            is_r += [r for _, r in _walk_dated(df.iloc[:cut], *v)]
            oos_r += [r for _, r in _walk_dated(df.iloc[cut:], *v)]
        dsr = deflated_sharpe_ratio(pd.Series(oos_r), n_trials=len(variants)) if oos_r else 0
        print(f"   {str(v):<18}{e(oos_r):>9.3f}{len(oos_r):>7}{dsr:>7.0%}")
    print("\nVERDICT GUIDE: trustworthy only if positive across MOST eras, broad "
          "across pairs, AND robust to params with DSR staying high.")


if __name__ == "__main__":
    main()
