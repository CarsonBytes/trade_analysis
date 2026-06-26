"""Monte-Carlo pass-probability of a single-instrument weekly-trend strategy against
a prop-challenge barrier (The5ers Bootcamp: +6% target before -5% static max-loss).

Pulls the strategy's real per-trade R sequence for one instrument (default GLD) from
the same engine as the backtest, then bootstraps equity paths at several risk-per-trade
levels. Static barriers from the step's STARTING balance: pass at +6%, fail at -5%.

Run: BROKER=ib UNIVERSE=etf uv run python -m dashboard.research.prop_passprob [SYMBOL] [SWAP_R]
  SWAP_R = optional per-trade R haircut to approximate CFD overnight financing (default 0).
"""
import sys
import numpy as np
import pandas as pd

from dashboard.instruments import active_by_key
from dashboard.core import paper
from dashboard.research import backtest as bt


def _r_sequence(symbol: str) -> list[float]:
    inst = active_by_key(symbol)
    import yfinance as yf
    raw = yf.download(inst.yf, period="max", interval="1wk", progress=False, auto_adjust=True)
    if raw is None or len(raw) == 0:
        raise SystemExit(f"no data for {symbol} ({inst.yf})")
    if hasattr(raw.columns, "nlevels") and raw.columns.nlevels > 1:
        raw.columns = raw.columns.get_level_values(0)
    df = raw[["Open", "High", "Low", "Close"]].copy()
    df.columns = ["open", "high", "low", "close"]
    df = df.dropna()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    paper.WEEKLY_TREND_CLASSES = set()        # don't class-gate a single symbol
    trades = bt._signals(df, symbol)
    return [t["r"] for t in trades], df.index[0], df.index[-1]


def _pass_prob(r: np.ndarray, risk: float, target=0.06, maxloss=0.05,
               n=40000, cap=20000, rng=None):
    """P(equity hits 1+target before 1-maxloss) and median #trades among PASSING paths."""
    rng = rng or np.random.default_rng(7)
    up, lo = 1.0 + target, 1.0 - maxloss
    wins = 0
    win_len = []
    for _ in range(n):
        e = 1.0
        for k in range(1, cap + 1):
            e *= 1.0 + risk * r[rng.integers(len(r))]
            if e >= up:
                wins += 1; win_len.append(k); break
            if e <= lo:
                break
    med = float(np.median(win_len)) if win_len else float("nan")
    return wins / n, med


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "GLD"
    swap_r = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
    r, d0, d1 = _r_sequence(symbol)
    r = np.array(r, dtype=float) - swap_r
    yrs = (d1 - d0).days / 365.25
    print(f"{symbol}: {len(r)} weekly-trend trades over {yrs:.1f}y "
          f"(~{len(r)/yrs:.1f}/yr) | mean R {r.mean():+.3f} | win {np.mean(r>0):.0%}"
          + (f" | swap haircut {swap_r:+.2f}R" if swap_r else ""))
    print(f"Barrier: +6% target before -5% max-loss (static, from step start). No time limit.\n")
    tpy = len(r) / yrs
    print(f"{'risk/trade':>11}{'1-step':>9}{'3-step':>9}{'med trades/step':>16}"
          f"{'~yrs/step':>11}{'~yrs all 3':>12}")
    for risk in (0.005, 0.01, 0.02, 0.03, 0.05, 0.075, 0.10):
        p1, med = _pass_prob(r, risk)
        yps = med / tpy
        print(f"{risk:>10.1%}{p1:>9.1%}{p1**3:>9.1%}{med:>16.0f}{yps:>11.1f}{yps*3:>12.1f}")
    print("\nNote: R already includes the ETF half-spread cost; CFD overnight financing on "
          "~3-week holds is NOT included unless a swap haircut is passed. 3-step assumes "
          "independent steps (same % rule each).")


if __name__ == "__main__":
    main()
