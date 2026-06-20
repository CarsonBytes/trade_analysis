"""A/B test: does an ADX trend-regime filter help? (no look-ahead, OOS)

The 5y portfolio backtest showed regime dependence (older period lost, recent
trending period won). The proposal's most justified idea: only take trend-
following trades when a trend actually exists (ADX high). ADX needs only OHLC
(no volume), so unlike Volume Profile it CAN run on this universe.

Tests the live config (s5 + overext) with no filter vs ADX>=20/25/30, IS/OOS
split + deflated Sharpe.

Run:  uv run python -u -m dashboard.ab_regime
"""
from __future__ import annotations
from . import net  # noqa: F401
import numpy as np
import pandas as pd
from analyst.features import compute_facts
from metrics import deflated_sharpe_ratio
from .instruments import UNIVERSE
from .providers import get_ohlc
from .scoring import score_from_facts
from . import paper
from .replay import _resolve_daily


def _adx(df, n=14):
    """Wilder ADX(14) from OHLC. Causal (ewm) -> no look-ahead when indexed by bar."""
    high, low, close = df["high"], df["low"], df["close"]
    up, dn = high.diff(), -low.diff()
    plus_dm = ((up > dn) & (up > 0)) * up
    minus_dm = ((dn > up) & (dn > 0)) * dn
    tr = pd.concat([high - low, (high - close.shift()).abs(),
                    (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    pdi = 100 * plus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr
    mdi = 100 * minus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


def _walk(df, key, adx, min_adx):
    close = df["close"]; n = len(df); i = 160; rs = []
    while i < n - 1:
        facts, _ = compute_facts(close.iloc[: i + 1], key)
        score = score_from_facts(key, facts, "")
        if score.signal not in ("BUY", "SELL") or score.strength < paper.MIN_STRENGTH:
            i += 1; continue
        direction = "long" if score.signal == "BUY" else "short"
        rsi = facts.get("rsi14") or 50.0
        if paper.OVEREXT_FILTER and ((direction == "long" and rsi > paper.OVEREXT_HI) or
                                     (direction == "short" and rsi < paper.OVEREXT_LO)):
            i += 1; continue
        if min_adx is not None:
            a = adx.iloc[i]
            if not (a == a) or a < min_adx:      # NaN or below threshold -> skip
                i += 1; continue
        res = paper.compute_sltp(facts, direction, "ATR", paper.RR_DEFAULT)
        if res is None:
            i += 1; continue
        entry, sl, tp, rr_act = res
        if rr_act < paper.MIN_RR:
            i += 1; continue
        out = _resolve_daily(direction, entry, sl, tp, df.iloc[i + 1: i + 1 + paper.HORIZON_DAYS])
        if out is None:
            break
        rs.append(paper.r_multiple(direction, entry, sl, out[1]))
        i += out[2] + 1
    return rs


def main():
    data = {}
    for inst in UNIVERSE:
        df = get_ohlc(inst, period="5y", interval="1d")
        if df is not None and len(df) > 300:
            data[inst.key] = (df, _adx(df))
    print(f"{len(data)} instruments | s5 + overext | ADX regime filter\n")
    variants = [("baseline (no ADX)", None), ("ADX>=20", 20), ("ADX>=25", 25), ("ADX>=30", 30)]
    print(f"{'variant':<18}{'IS_expR':>9}{'OOS_expR':>10}{'OOS_n':>7}{'OOS_DSR':>9}")
    for label, madx in variants:
        is_r, oos_r = [], []
        for key, (df, adx) in data.items():
            cut = int(len(df) * 0.6)
            is_r += _walk(df.iloc[:cut], key, adx.iloc[:cut], madx)
            oos_r += _walk(df.iloc[cut:], key, adx.iloc[cut:], madx)
        e = lambda xs: sum(xs) / len(xs) if xs else 0.0
        dsr = deflated_sharpe_ratio(pd.Series(oos_r), n_trials=len(variants)) if oos_r else 0
        print(f"{label:<18}{e(is_r):>9.3f}{e(oos_r):>10.3f}{len(oos_r):>7}{dsr:>9.0%}")
    print("\nadopt only if OOS expR clearly beats baseline AND DSR stays high.")


if __name__ == "__main__":
    main()
