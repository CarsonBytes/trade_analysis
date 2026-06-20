"""A/B test: a PRICE-BASED mean-reversion strategy for the low-ADX (chop) regime.

The trend system bleeds in choppy markets (ADX<25). The complement: instead of
sitting out, FADE the extremes back to a value anchor -- but built from PRICE,
not Volume Profile (which needs real exchange volume this universe lacks).

Definition (daily bars, no look-ahead):
  - regime: ADX(14) < adx_max  (only trade chop)
  - value anchor: 20-bar SMA (the 'POC' proxy); Bollinger band = SMA +/- 2 sigma
  - LONG  when close < lower band AND RSI < rsi_lo  -> revert UP to the SMA
  - SHORT when close > upper band AND RSI > rsi_hi  -> revert DOWN to the SMA
  - TP = SMA at entry; SL = entry -/+ sl_atr * ATR(14)
Resolved against subsequent bars (SL-before-TP within a bar = conservative),
costs charged via paper.r_multiple. IS/OOS split + deflated Sharpe.

Run:  uv run python -u -m dashboard.ab_meanrev
"""
from __future__ import annotations
from . import net  # noqa: F401
import numpy as np
import pandas as pd
from metrics import deflated_sharpe_ratio
from .instruments import UNIVERSE
from .providers import get_ohlc
from . import paper
from .replay import _resolve_daily
from .ab_regime import _adx

HORIZON = 8          # bars to allow the reversion to play out
MA, BB_K = 20, 2.0


def _rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def _atr(df, n=14):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def _walk(df, adx_max, sl_atr, rsi_lo, rsi_hi):
    c = df["close"]
    ma = c.rolling(MA).mean(); sd = c.rolling(MA).std()
    up, lo = ma + BB_K * sd, ma - BB_K * sd
    rsi, atr, adx = _rsi(c), _atr(df), _adx(df)
    n = len(df); i = MA + 5; rs = []
    while i < n - 1:
        a = adx.iloc[i]
        if not (a == a) or a >= adx_max:        # need a valid, LOW adx (chop)
            i += 1; continue
        price = c.iloc[i]; m = ma.iloc[i]; at = atr.iloc[i]
        if not (at > 0) or not (m == m):
            i += 1; continue
        if price < lo.iloc[i] and rsi.iloc[i] < rsi_lo:
            direction, tp, sl = "long", m, price - sl_atr * at
        elif price > up.iloc[i] and rsi.iloc[i] > rsi_hi:
            direction, tp, sl = "short", m, price + sl_atr * at
        else:
            i += 1; continue
        if (direction == "long" and not (tp > price and sl < price)) or \
           (direction == "short" and not (tp < price and sl > price)):
            i += 1; continue
        out = _resolve_daily(direction, price, sl, tp, df.iloc[i + 1: i + 1 + HORIZON])
        if out is None:
            break
        rs.append(paper.r_multiple(direction, price, sl, out[1]))
        i += out[2] + 1
    return rs


def main():
    data = {}
    for inst in UNIVERSE:
        df = get_ohlc(inst, period="5y", interval="1d")
        if df is not None and len(df) > 300:
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            data[inst.key] = df
    print(f"{len(data)} instruments | price-based mean-reversion (MA{MA}, BB{BB_K}, "
          f"horizon {HORIZON}d)\n")
    variants = [
        ("ADX<20 SL1.5 30/70", 20, 1.5, 30, 70),
        ("ADX<25 SL1.5 30/70", 25, 1.5, 30, 70),
        ("ADX<25 SL2.0 30/70", 25, 2.0, 30, 70),
        ("ADX<25 SL1.5 35/65", 25, 1.5, 35, 65),
        ("ADX<30 SL1.5 30/70", 30, 1.5, 30, 70),
    ]
    print(f"{'variant':<22}{'IS_expR':>9}{'OOS_expR':>10}{'OOS_win':>9}{'OOS_n':>7}{'OOS_DSR':>9}")
    for label, amax, slm, rl, rh in variants:
        is_r, oos_r = [], []
        for key, df in data.items():
            cut = int(len(df) * 0.6)
            is_r += _walk(df.iloc[:cut], amax, slm, rl, rh)
            oos_r += _walk(df.iloc[cut:], amax, slm, rl, rh)
        e = lambda xs: sum(xs) / len(xs) if xs else 0.0
        w = lambda xs: (sum(1 for x in xs if x > 0) / len(xs)) if xs else 0.0
        dsr = deflated_sharpe_ratio(pd.Series(oos_r), n_trials=len(variants)) if oos_r else 0
        print(f"{label:<22}{e(is_r):>9.3f}{e(oos_r):>10.3f}{w(oos_r)*100:>8.0f}%{len(oos_r):>7}{dsr:>9.0%}")
    print("\nadopt only if OOS expR > 0.05 AND DSR high. Mean-reversion's failure "
          "mode is strong trends -- the ADX<x gate is what protects it.")


if __name__ == "__main__":
    main()
