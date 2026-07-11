"""Capacity/liquidity sanity check: does the backtest's flat ~10bp cost assumption hold up
against REAL current market microstructure for the least-liquid names in the 22-ETF book?
Checkable right now without waiting for real fills (item #3 of a self-directed 'what's still
missing' review, 2026-07-11) -- pulls current average daily $ volume and compares it against
what a 25%-of-equity position (ETF_POS_CAP) would actually be, at both the account's current
size and a future growth milestone, to see which names (if any) could see real market impact
beyond the assumed cost model.

Run:  uv run python -m dashboard.research.liquidity_check
"""
from __future__ import annotations
import os
os.environ.setdefault("BROKER", "ib")
os.environ.setdefault("UNIVERSE", "etf")

import yfinance as yf

from dashboard.instruments import active_universe

POS_CAP = 0.25
EQUITY_SCENARIOS = [130_000, 500_000, 1_000_000]   # current paper-equivalent size, then two
                                                    # growth milestones
IMPACT_WARN_PCT = 1.0        # position as this %+ of daily $ volume gets flagged

print(f"Checking liquidity for {len(active_universe())} instruments "
      f"(30-day avg volume via yfinance)...\n")
rows = []
for inst in active_universe():
    t = yf.Ticker(inst.yf)
    hist = yf.download(inst.yf, period="1mo", interval="1d", progress=False, auto_adjust=True)
    if hist is None or len(hist) == 0:
        print(f"{inst.key:<6} no recent data, skipping")
        continue
    if hasattr(hist.columns, "nlevels") and hist.columns.nlevels > 1:
        hist.columns = hist.columns.get_level_values(0)
    avg_vol = float(hist["Volume"].mean())
    last_px = float(hist["Close"].iloc[-1])
    adv_usd = avg_vol * last_px
    rows.append((inst.key, last_px, avg_vol, adv_usd))

rows.sort(key=lambda r: r[3])   # lowest $ ADV first -- the ones to worry about
print(f"{'ticker':<8}{'price':>9}{'avg vol':>12}{'ADV $':>15}")
for k, px, vol, adv in rows:
    print(f"{k:<8}{px:>9.2f}{vol:>12,.0f}{adv:>15,.0f}")

print()
for equity in EQUITY_SCENARIOS:
    pos_usd = equity * POS_CAP
    print(f"--- at ${equity:,.0f} equity (a {POS_CAP:.0%}-cap position = ${pos_usd:,.0f}) ---")
    flagged = [(k, adv, pos_usd / adv * 100) for k, px, vol, adv in rows if adv > 0
              and pos_usd / adv * 100 >= IMPACT_WARN_PCT]
    if not flagged:
        print(f"  No ticker's max position exceeds {IMPACT_WARN_PCT:.0f}% of its own ADV$ -- "
              f"the flat cost assumption looks fine at this size.")
    else:
        for k, adv, pct in sorted(flagged, key=lambda x: -x[2]):
            print(f"  {k:<6} position would be {pct:.1f}% of ADV$ (ADV$ = ${adv:,.0f}/day) "
                  f"-- worth a closer look, real slippage could exceed 10bp here")
    print()

print("NOTE: 1% of average daily $ volume is a common rough heuristic for 'shouldn't move "
      "the market much' -- it's not a precise market-impact model. Also: this checks a "
      "SINGLE position's size against ADV, not the effect of PORTFOLIO_CAP scaling several "
      "concurrent positions down, so it's a conservative (worst-case single-name) check.")
