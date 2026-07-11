"""Compare REAL closed trades (paper or live journal) against the backtest's expected
per-trade distribution -- the concrete tool for "verify potential trade performance" rather
than waiting passively for n>=30 and then improvising an analysis. Built 2026-07-11 while
there were still ZERO closed trades anywhere (confirmed via a direct DB check) specifically
so this is ready to run the moment real data exists, instead of needing a fresh investigation
later.

Two comparisons, both on the CORE strategy only (excludes the sleeve, which has its own
separate, already-tested spec in core/sleeve.py and zero real fills as of this writing):
  1. Win-rate: binomial test against the backtest's expected win-rate as the null.
  2. Expectancy (R): one-sample t-test against the backtest's expected expectancy as the null.

Both are honestly caveated by n -- paper.stats()'s own "trustworthy" flag (n>=30) gates how
much weight the verdict should get; below that this reports the numbers but flags them as
not-yet-conclusive rather than pretending a small sample settles anything.

Run:  uv run python -m dashboard.research.live_vs_backtest [--db dashboard.db]
"""
from __future__ import annotations
import os
os.environ.setdefault("BROKER", "ib")
os.environ.setdefault("UNIVERSE", "etf")

import argparse
import yfinance as yf
from scipy import stats as sps

import dashboard.research.backtest as bt
from dashboard.instruments import active_universe
from dashboard.core.sleeve import SLEEVE_METHOD


def _backtest_reference(pos_cap: float, portfolio_cap: float) -> dict:
    """The core strategy's expected per-trade distribution, computed on the SAME
    correctly-scoped (full-history weekly) data and config as the live/paper deployment."""
    bt.POS_CAP = pos_cap
    bt.PORTFOLIO_CAP = portfolio_cap
    bt.CASH_YIELD = None
    cands = []
    for inst in active_universe():
        df = yf.download(inst.yf, period="max", interval="1wk", progress=False, auto_adjust=True)
        if df is None or len(df) == 0:
            continue
        if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
            df.columns = df.columns.get_level_values(0)
        df = df[["Open", "High", "Low", "Close"]].copy()
        df.columns = ["open", "high", "low", "close"]
        df = df.dropna()
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        if len(df) < 220:
            continue
        cands += bt._signals(df, inst.key)
    _, realized = bt._portfolio(cands, 0.01)
    return bt.paper.stats(realized)


def _real_trades(db_name: str) -> list[float]:
    # paper._db_path() reads DASH_DB_NAME fresh on every call (no module-level cache), so
    # setting the env var right before calling is sufficient -- no reload needed.
    os.environ["DASH_DB_NAME"] = db_name
    closed = [t for t in bt.paper.all_trades()
             if t["status"] != "OPEN" and t["method"] != SLEEVE_METHOD]
    return [t["realized_r"] for t in closed]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("DASH_DB_NAME", "dashboard.db"))
    ap.add_argument("--pos-cap", type=float, default=0.25)
    ap.add_argument("--portfolio-cap", type=float, default=1.0)
    args = ap.parse_args()

    real_r = _real_trades(args.db)
    real = bt.paper.stats(real_r)
    print(f"Real closed CORE trades in {args.db}: n={real['n']}")
    if real["n"] == 0:
        print("  Nothing to compare yet -- 0 closed trades. Re-run once some exist.")
        print("  (Sleeve trades are excluded by design; they have their own separate spec.)")
        return
    print(f"  win_rate={real['win_rate']:.1%}  expectancy_R={real['expectancy_R']:+.3f}  "
          f"profit_factor={real['profit_factor']:.2f}  trustworthy(n>=30)={real['trustworthy']}")

    print("\nFetching the backtest's expected distribution (same live config, full weekly "
          "history)...")
    ref = _backtest_reference(args.pos_cap, args.portfolio_cap)
    print(f"Backtest reference: n={ref['n']}  win_rate={ref['win_rate']:.1%}  "
          f"expectancy_R={ref['expectancy_R']:+.3f}  profit_factor={ref['profit_factor']:.2f}")

    print("\n--- Statistical comparison ---")
    # 1. win-rate: binomial test, H0 = real win-rate came from a process with the backtest's
    #    win-rate as its true probability
    wins = int(round(real["win_rate"] * real["n"]))
    bt_result = sps.binomtest(wins, real["n"], ref["win_rate"])
    print(f"Win-rate: real {real['win_rate']:.1%} (n={real['n']}) vs backtest-expected "
          f"{ref['win_rate']:.1%}  ->  p={bt_result.pvalue:.3f} "
          f"({'no significant difference' if bt_result.pvalue > 0.05 else 'SIGNIFICANTLY DIFFERENT'})")

    # 2. expectancy: one-sample t-test, H0 = real trades' mean R came from a distribution
    #    centered on the backtest's expected expectancy
    if real["n"] >= 2:
        t_result = sps.ttest_1samp(real_r, ref["expectancy_R"])
        print(f"Expectancy: real {real['expectancy_R']:+.3f}R (n={real['n']}) vs "
              f"backtest-expected {ref['expectancy_R']:+.3f}R  ->  p={t_result.pvalue:.3f} "
              f"({'no significant difference' if t_result.pvalue > 0.05 else 'SIGNIFICANTLY DIFFERENT'})")
    else:
        print("Expectancy: need n>=2 for a t-test, skipping.")

    if not real["trustworthy"]:
        print(f"\n⚠️  n={real['n']} < 30 -- per this project's own 'trustworthy' bar, treat any "
              "verdict above as PROVISIONAL. A 'no significant difference' result at low n is "
              "weak evidence of consistency, not proof; a 'significantly different' result at "
              "low n could still just be noise. Re-run as more trades close.")


if __name__ == "__main__":
    main()
