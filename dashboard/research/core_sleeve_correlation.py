"""Direct empirical check of the core+sleeve diversification claim. The rationale for
combining them has always been "different risk driver" (trend-following vs panic-dip-buy),
but nobody has actually computed the correlation coefficient between their return series --
only observed that blended Calmar improves, which can happen even with material correlation
if the sleeve's raw Sharpe is high enough on its own. This checks the claim directly.

Run:  uv run python -m dashboard.research.core_sleeve_correlation
"""
from __future__ import annotations
import os
os.environ.setdefault("BROKER", "ib")
os.environ.setdefault("UNIVERSE", "etf")

import numpy as np
import pandas as pd

from dashboard.core.sleeve import SLEEVE_UNIVERSE
from dashboard.research.sleeve_blend import _core_weekly_returns, _sleeve_trades, _sleeve_unit_series

print("Building core return series (pos-cap 0.25, portfolio-cap 1.0, no cash-yield)...")
core_ret, years, n_core = _core_weekly_returns(0.25, 1.0, cash_yield=False)
didx = core_ret.index

print(f"Fetching sleeve data for {len(SLEEVE_UNIVERSE)} tickers...")
sleeve_trades = {tk: _sleeve_trades(tk) for tk in SLEEVE_UNIVERSE}
sleeve_unit = sum((_sleeve_unit_series(trs, didx) for trs in sleeve_trades.values()),
                  pd.Series(0.0, index=didx))

# only compare over days where EITHER series has a nonzero move -- both are mostly-zero
# daily series (core is a resampled weekly curve, sleeve only has values on exit days), so
# correlating the raw daily series would be dominated by shared zeros. Compare on days
# where at least one side actually moved.
active = (core_ret != 0) | (sleeve_unit != 0)
c, s = core_ret[active], sleeve_unit[active]
corr_active_days = np.corrcoef(c, s)[0, 1] if len(c) > 2 else float("nan")

# also the "textbook" version: correlate on ALL days (includes the shared-zero problem, but
# is what a naive off-the-shelf correlation check would report -- show both for honesty)
corr_all_days = np.corrcoef(core_ret, sleeve_unit)[0, 1]

# and specifically: on days the SLEEVE actually closed a trade, what was core doing?
sleeve_active_days = sleeve_unit[sleeve_unit != 0]
core_on_sleeve_days = core_ret.reindex(sleeve_active_days.index)
corr_on_sleeve_days = np.corrcoef(core_on_sleeve_days, sleeve_active_days)[0, 1] \
    if len(sleeve_active_days) > 2 else float("nan")

print(f"\nCorrelation, ALL days (dominated by shared zeros -- least meaningful): {corr_all_days:+.3f}")
print(f"Correlation, days where EITHER side moved: {corr_active_days:+.3f}")
print(f"Correlation, ON SLEEVE-EXIT days specifically (most relevant -- 'when the sleeve "
      f"is doing something, what is core doing'): {corr_on_sleeve_days:+.3f}")

# sign check: does the sleeve tend to profit on days core is losing (the diversification
# story), or do they move together?
both_active = (core_ret != 0) & (sleeve_unit != 0)
if both_active.sum() > 0:
    same_sign = ((core_ret[both_active] > 0) == (sleeve_unit[both_active] > 0)).mean()
    print(f"\nOn the {both_active.sum()} days BOTH core and sleeve had a nonzero move: "
          f"{same_sign*100:.0f}% moved in the SAME direction "
          f"({'concerning if high' if same_sign > 0.6 else 'looks diversifying if low'})")

print("\nNOTE: correlation alone doesn't settle whether the combination is 'worth it' -- a "
      "positively-correlated but higher-Sharpe satellite can still improve blended Calmar. "
      "This just answers the specific factual question of whether the two return streams "
      "actually move independently, which the 'different risk driver' narrative implies but "
      "was never directly measured.")
