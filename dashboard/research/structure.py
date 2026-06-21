"""Market-structure primitives (deterministic, no look-ahead).

Swing pivots, trend structure (BOS / CHoCH), liquidity sweeps, and supply/demand
(support/resistance) ZONES, plus a displacement/FVG proxy. Computed from a CLOSE
series -- the data the rest of the system already has, so it costs no extra
fetches and works identically live and in replay.

Honesty note: true Fair Value Gaps and order blocks are INTRABAR (need high/low).
With close-only data we approximate "displacement" (an outsized close-to-close
move that leaves an imbalance) and label it as such -- we don't pretend close
data is intrabar. Everything looks only at bars up to the last one passed in;
the most recent `k` bars can't be confirmed as pivots (need k bars after), so
they're excluded -- no look-ahead.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def swing_points(close: pd.Series, k: int = 5) -> tuple[list[tuple[int, float]],
                                                        list[tuple[int, float]]]:
    """(swing_highs, swing_lows) as [(pos, price)] via a +/-k fractal: a swing
    high at i if close[i] is the max of the 2k+1 window centred on i."""
    v = close.to_numpy(dtype=float)
    n = len(v)
    highs, lows = [], []
    for i in range(k, n - k):
        w = v[i - k:i + k + 1]
        if v[i] == w.max():
            highs.append((i, float(v[i])))
        elif v[i] == w.min():
            lows.append((i, float(v[i])))
    return highs, lows


def _cluster(levels: list[float], tol: float) -> list[tuple[float, int]]:
    """Group nearby pivot levels into zones. Returns [(zone_mid, n_touches)]
    sorted by price. `tol` is the absolute width that counts as 'the same zone'."""
    if not levels:
        return []
    levels = sorted(levels)
    zones: list[list[float]] = [[levels[0]]]
    for lv in levels[1:]:
        if lv - zones[-1][-1] <= tol:
            zones[-1].append(lv)
        else:
            zones.append([lv])
    return [(float(np.mean(z)), len(z)) for z in zones]


def analyse(close: pd.Series, atr: float, k: int = 5, lookback: int = 250) -> dict:
    """Full structure read for the most recent bar. `atr` sets the zone-merge
    tolerance and normalises distances. Returns a flat, JSON-friendly dict."""
    s = close.dropna().astype(float)
    if len(s) < 3 * k + 5:
        return {}
    s = s.iloc[-lookback:]
    price = float(s.iloc[-1])
    atr = atr or (s.diff().abs().tail(14).mean() or (abs(price) * 1e-4))
    highs, lows = swing_points(s, k)

    # --- trend structure: HH/HL = bullish, LH/LL = bearish ------------------
    trend = "range"
    last_event = "none"
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1][1] > highs[-2][1]
        hl = lows[-1][1] > lows[-2][1]
        lh = highs[-1][1] < highs[-2][1]
        ll = lows[-1][1] < lows[-2][1]
        if hh and hl:
            trend = "bullish"
        elif lh and ll:
            trend = "bearish"
        # BOS = continuation break; CHoCH = first counter-trend break
        prior_high = highs[-2][1]
        prior_low = lows[-2][1]
        if price > prior_high:
            last_event = "BOS_up" if trend == "bullish" else "CHoCH_up"
        elif price < prior_low:
            last_event = "BOS_down" if trend == "bearish" else "CHoCH_down"

    # --- liquidity sweep: took out a prior swing then closed back through ----
    swept = "none"
    if highs:
        recent_hi = max(h[1] for h in highs[-3:])
        if s.iloc[-3:].max() > recent_hi >= price:   # poked above, closed below
            swept = "high"
    if lows:
        recent_lo = min(l[1] for l in lows[-3:])
        if s.iloc[-3:].min() < recent_lo <= price:   # poked below, closed above
            swept = "low"

    # --- supply / demand zones (clustered pivots) ---------------------------
    tol = 0.5 * atr
    supply = _cluster([h for _, h in highs], tol)           # resistance side
    demand = _cluster([l for _, l in lows], tol)            # support side
    above = [(m, n) for m, n in supply if m > price]
    below = [(m, n) for m, n in demand if m < price]
    nearest_supply = min(above, key=lambda z: z[0] - price) if above else None
    nearest_demand = max(below, key=lambda z: z[0]) if below else None

    # --- displacement / FVG proxy (close-only) ------------------------------
    # an outsized last move (|ret| > 1.5 ATR) leaves an imbalance to be retraced
    last_move = float(s.iloc[-1] - s.iloc[-2])
    displaced = abs(last_move) > 1.5 * atr
    fvg = ("bullish" if displaced and last_move > 0 else
           "bearish" if displaced else "none")

    return {
        "trend": trend,
        "last_event": last_event,                 # BOS/CHoCH up/down
        "swept": swept,                           # high/low liquidity sweep
        "fvg": fvg,                               # displacement direction (proxy)
        "nearest_supply": round(nearest_supply[0], 6) if nearest_supply else None,
        "supply_touches": nearest_supply[1] if nearest_supply else 0,
        "nearest_demand": round(nearest_demand[0], 6) if nearest_demand else None,
        "demand_touches": nearest_demand[1] if nearest_demand else 0,
        "atr_to_supply": round((nearest_supply[0] - price) / atr, 2) if nearest_supply else None,
        "atr_to_demand": round((price - nearest_demand[0]) / atr, 2) if nearest_demand else None,
    }
