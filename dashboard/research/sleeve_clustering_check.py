"""Does the sleeve ever fire on several correlated tickers SIMULTANEOUSLY during a systemic
panic? Checked directly rather than assumed: `core/sleeve.py`'s `place_sleeve_signals()` only
checks `_has_open(ticker)`/`_recent_close(ticker)` PER TICKER -- no cross-ticker cap or
correlation control at all. Since the entry trigger (VIX spike +15%/5d) is inherently
systemic, several of the 11 tickers (especially the correlated equity-index ones: SPY/QQQ/
XLK/DIA/IWM) firing the same week is entirely plausible. Also: `sleeve_blend.py`'s backtest
methodology (`core_ret + w * sleeve_unit`) linearly sums however many positions resolve on
the same day at a CONSTANT weight `w` -- it does NOT cap aggregate simultaneous sleeve
exposure the way the real production system's `PORTFOLIO_CAP` does. This checks how much
that gap actually matters historically.

Run:  uv run python -m dashboard.research.sleeve_clustering_check
"""
from __future__ import annotations
import os
os.environ.setdefault("BROKER", "ib")
os.environ.setdefault("UNIVERSE", "etf")

import pandas as pd

from dashboard.core.sleeve import SLEEVE_UNIVERSE
from dashboard.research.sleeve_blend import _sleeve_trades

print(f"Fetching sleeve entry/exit data for {len(SLEEVE_UNIVERSE)} tickers...")
all_entries: list[tuple[pd.Timestamp, str]] = []
all_exits: list[tuple[pd.Timestamp, str, float]] = []
for tk in SLEEVE_UNIVERSE:
    trades = _sleeve_trades(tk)
    for t in trades:
        exit_d = pd.Timestamp(t["d"])
        # entry date isn't returned by _sleeve_trades directly, but the exit-minus-hold
        # relationship isn't needed here -- what matters for CONCURRENT EXPOSURE is which
        # trades are OPEN on any given day, so reconstruct entry approx from resolution
        all_exits.append((exit_d, tk, t["r"]))

exits_df = pd.DataFrame(all_exits, columns=["date", "ticker", "r"])
exits_df = exits_df.sort_values("date")

# group by calendar week (entries/exits cluster on the panic week itself)
exits_df["week"] = exits_df["date"].dt.to_period("W")
by_week = exits_df.groupby("week")["ticker"].nunique().sort_values(ascending=False)

print(f"\n{len(exits_df)} total sleeve exits across {len(SLEEVE_UNIVERSE)} tickers, "
      f"{by_week.index.min()} to {by_week.index.max()}\n")
print("Weeks with the MOST distinct tickers resolving simultaneously:")
for wk, n in by_week.head(15).items():
    tickers_that_week = exits_df[exits_df["week"] == wk]
    rs = tickers_that_week["r"].values
    tks = tickers_that_week["ticker"].tolist()
    print(f"  {wk}: {n} tickers ({', '.join(tks)})  combined R this week: {rs.sum()*100:+.1f}% "
          f"(mean {rs.mean()*100:+.1f}%)")

print(f"\nDistribution of 'distinct tickers resolving per week':")
print(by_week.value_counts().sort_index())

# the mechanical question: at 10% sleeve weight, what's the WORST single-week combined R
# contribution, and what would that imply for a portfolio-level hit in one week?
worst_week = exits_df.groupby("week")["r"].sum().sort_values()
print(f"\nWorst single-week combined R (summed across however many tickers resolved that "
      f"week): {worst_week.iloc[0]*100:+.1f}% on {worst_week.index[0]}")
print(f"At 10% sleeve weight, that week's portfolio-level contribution: "
      f"{worst_week.iloc[0]*0.10*100:+.2f}pp -- compare against the documented full-history "
      f"maxDD figures to see if this is already a meaningful share of the worst drawdown, or "
      f"a rounding error.")
