"""A/B test: does blocking OVEREXTENDED entries help? (no look-ahead, OOS)

The live tape and the win_model both say entering when price is already stretched
IN THE TRADE DIRECTION (long into high RSI, short into low RSI) loses. This tests
blocking those entries at a few RSI thresholds, on the live config (s5, ATR rr3),
with a 60/40 in-sample / out-of-sample split and deflated Sharpe.

Run:  uv run python -u -m dashboard.ab_overext
"""
from __future__ import annotations
from dashboard.core import net  # noqa: F401
import pandas as pd
from analyst.features import compute_facts
from metrics import deflated_sharpe_ratio
from dashboard.instruments import UNIVERSE
from dashboard.data.providers import get_ohlc
from dashboard.core.scoring import score_from_facts
from dashboard.core import paper
from dashboard.research.replay import _resolve_daily


def _walk(df, key, rr, hi, lo):
    """Return list of realized R for s5 ATR setups, optionally skipping entries
    overextended in the trade direction (long & RSI>hi, short & RSI<lo)."""
    close = df["close"]; n = len(df); i = 160; rs = []
    while i < n - 1:
        facts, _ = compute_facts(close.iloc[: i + 1], key)
        score = score_from_facts(key, facts, "")
        if score.signal not in ("BUY", "SELL") or score.strength < 5:
            i += 1; continue
        direction = "long" if score.signal == "BUY" else "short"
        rsi = facts.get("rsi14") or 50.0
        if hi is not None and ((direction == "long" and rsi > hi) or
                               (direction == "short" and rsi < lo)):
            i += 1; continue                       # skip overextended entry
        res = paper.compute_sltp(facts, direction, "ATR", rr)
        if res is None:
            i += 1; continue
        entry, sl, tp, rr_act = res
        if rr_act < paper.MIN_RR:
            i += 1; continue
        bars = df.iloc[i + 1: i + 1 + paper.HORIZON_DAYS]
        out = _resolve_daily(direction, entry, sl, tp, bars)
        if out is None:
            break
        status, exit_px, used = out
        rs.append(paper.r_multiple(direction, entry, sl, exit_px))
        i += used + 1
    return rs


def main():
    data = {}
    for inst in UNIVERSE:
        df = get_ohlc(inst, period="5y", interval="1d")
        if df is not None and len(df) > 300:
            data[inst.key] = df
    print(f"{len(data)} instruments | s5 ATR rr{paper.RR_DEFAULT}\n")
    variants = [("baseline", None, None), ("block >70/<30", 70, 30),
                ("block >65/<35", 65, 35), ("block >60/<40", 60, 40)]
    print(f"{'variant':<16}{'IS_expR':>9}{'OOS_expR':>10}{'OOS_n':>7}{'OOS_DSR':>9}")
    for label, hi, lo in variants:
        is_r, oos_r = [], []
        for key, df in data.items():
            cut = int(len(df) * 0.6)
            is_r += _walk(df.iloc[:cut], key, paper.RR_DEFAULT, hi, lo)
            oos_r += _walk(df.iloc[cut:], key, paper.RR_DEFAULT, hi, lo)
        e = lambda xs: sum(xs) / len(xs) if xs else 0.0
        dsr = deflated_sharpe_ratio(pd.Series(oos_r), n_trials=len(variants)) if oos_r else 0.0
        print(f"{label:<16}{e(is_r):>9.3f}{e(oos_r):>10.3f}{len(oos_r):>7}{dsr:>9.0%}")


if __name__ == "__main__":
    main()
