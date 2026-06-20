"""Breadth check: is the weekly edge broad, or driven by a few instruments?
Run: uv run python -m dashboard.ab_breadth"""
from __future__ import annotations
from . import net  # noqa: F401
import warnings
warnings.filterwarnings("ignore")
import yfinance as yf
from analyst.features import compute_facts
from .scoring import score_from_facts
from . import paper
from .replay import _resolve_daily


def _walk(df, key):
    c = df["close"]; n = len(df); i = 160; rs = []
    while i < n - 1:
        f, _ = compute_facts(c.iloc[: i + 1], key)
        s = score_from_facts(key, f, "")
        if s.signal not in ("BUY", "SELL") or s.strength < 5:
            i += 1; continue
        d = "long" if s.signal == "BUY" else "short"
        rsi = f.get("rsi14") or 50
        if (d == "long" and rsi > 70) or (d == "short" and rsi < 30):
            i += 1; continue
        r = paper.compute_sltp(f, d, "ATR", 3.0)
        if r is None:
            i += 1; continue
        e, sl, tp, rr = r
        if rr < 1.5:
            i += 1; continue
        o = _resolve_daily(d, e, sl, tp, df.iloc[i + 1: i + 6])
        if o is None:
            break
        rs.append(paper.r_multiple(d, e, sl, o[1]))
        i += o[2] + 1
    return rs


def main():
    from .instruments import UNIVERSE
    print("per-instrument WEEKLY expectancy (s5 + overext):")
    pos = neg = 0
    for inst in UNIVERSE:
        raw = yf.download(inst.yf, period="max", interval="1wk",
                          progress=False, auto_adjust=True)
        if raw is None or len(raw) < 220:
            continue
        if raw.columns.nlevels > 1:
            raw.columns = raw.columns.get_level_values(0)
        df = raw[["Open", "High", "Low", "Close"]].copy()
        df.columns = ["open", "high", "low", "close"]
        df = df.dropna()
        rs = _walk(df, inst.key)
        if len(rs) < 10:
            continue
        exp = sum(rs) / len(rs)
        pos += exp > 0; neg += exp <= 0
        print(f"  {inst.key:8} n={len(rs):4} expR={exp:+.3f}")
    print(f"\npositive instruments: {pos} | negative: {neg}")


if __name__ == "__main__":
    main()
