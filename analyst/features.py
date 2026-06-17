"""Deterministic market facts.

THE most important design choice in this whole system: the LLM never computes a
number. Code computes RSI, ATR, trend, levels, volatility; the agents only
*reason about* those facts. This keeps the analysis grounded and reproducible
and stops the model from hallucinating indicator values.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _rsi(prices: pd.Series, period: int = 14) -> float:
    delta = prices.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    return float(rsi.iloc[-1])


def _atr_proxy(prices: pd.Series, period: int = 14) -> float:
    """Close-to-close ATR proxy (we only have closes from some sources)."""
    return float(prices.diff().abs().rolling(period).mean().iloc[-1])


def _trend_tstat(prices: pd.Series, n: int = 60) -> float:
    """Objective trend strength: the t-statistic of the slope of a linear fit to
    the last n closes. |t| large = a statistically significant (un-noisy) trend;
    near 0 = directionless chop. Sign = trend direction. Reproducible, unlike a
    subjective 'confidence'."""
    y = prices.tail(n).to_numpy(dtype=float)
    m = len(y)
    if m < 10:
        return 0.0
    x = np.arange(m, dtype=float)
    xm = x.mean()
    sxx = float(((x - xm) ** 2).sum())
    if sxx == 0:
        return 0.0
    slope = float(((x - xm) * (y - y.mean())).sum() / sxx)
    intercept = y.mean() - slope * xm
    resid = y - (slope * x + intercept)
    dof = m - 2
    s2 = float((resid ** 2).sum() / dof) if dof > 0 else 0.0
    se = (s2 / sxx) ** 0.5
    return slope / se if se > 0 else 0.0


def _trend(prices: pd.Series, fast: int, slow: int) -> str:
    if len(prices) < slow:
        return "n/a"
    f = prices.rolling(fast).mean().iloc[-1]
    s = prices.rolling(slow).mean().iloc[-1]
    return "up" if f > s else "down"


def compute_facts(prices: pd.Series, symbol: str) -> tuple[dict, str]:
    """Return (facts_dict, readable_summary).

    `prices` is a close series (any timeframe). Multi-timeframe trend is
    approximated by resampling the same series to coarser buckets.
    """
    prices = prices.astype(float).dropna()
    last = float(prices.iloc[-1])

    ret = {
        "1d": float(prices.pct_change(1).iloc[-1]),
        "5d": float(prices.pct_change(5).iloc[-1]) if len(prices) > 5 else np.nan,
        "20d": float(prices.pct_change(20).iloc[-1]) if len(prices) > 20 else np.nan,
    }

    rsi = _rsi(prices)
    atr = _atr_proxy(prices)
    # rolling median of the ATR series: the vol-regime baseline (trend entries
    # only earn their keep when current vol is at/above this -- replay-validated)
    atr_series = prices.diff().abs().rolling(14).mean()
    atr_med60 = float(atr_series.tail(60).median())
    realized_vol = float(prices.pct_change().tail(20).std() * np.sqrt(252))

    # support/resistance from recent extremes
    lookback = min(60, len(prices))
    window = prices.tail(lookback)
    support = float(window.min())
    resistance = float(window.max())

    # multi-timeframe trend (short / medium / long via MA pairs on the series)
    trends = {
        "short": _trend(prices, 10, 30),
        "medium": _trend(prices, 20, 60),
        "long": _trend(prices, 50, 150),
    }

    # distance to recent high/low in ATR units = how stretched price is
    atr_safe = atr if atr and atr > 0 else 1e-9
    stretch_to_high = (resistance - last) / atr_safe
    stretch_to_low = (last - support) / atr_safe

    # market structure: swing-based trend (BOS/CHoCH), liquidity sweep,
    # supply/demand zones, displacement (FVG proxy). Deterministic, no look-ahead.
    try:
        from dashboard import structure
        struct = structure.analyse(prices, atr)
    except Exception:
        struct = {}

    facts = {
        "symbol": symbol,
        "last_price": last,
        "returns": ret,
        "rsi14": rsi,
        "atr14": atr,
        "atr14_med60": atr_med60,
        "trend_tstat": _trend_tstat(prices, 60),
        "realized_vol_annual": realized_vol,
        "support_60": support,
        "resistance_60": resistance,
        "trend": trends,
        "atr_to_resistance": stretch_to_high,
        "atr_to_support": stretch_to_low,
        "structure": struct,
        "n_bars": len(prices),
    }

    summary = (
        f"Symbol: {symbol}\n"
        f"Last price: {last:.5f}\n"
        f"Returns: 1d {ret['1d']:+.2%}, 5d {ret['5d']:+.2%}, 20d {ret['20d']:+.2%}\n"
        f"RSI(14): {rsi:.1f}  (>70 overbought, <30 oversold)\n"
        f"ATR(14): {atr:.5f}   Realized vol (annual): {realized_vol:.1%}\n"
        f"Trend by horizon -> short: {trends['short']}, medium: {trends['medium']}, long: {trends['long']}\n"
        f"Recent 60-bar support: {support:.5f}  resistance: {resistance:.5f}\n"
        f"Price is {stretch_to_high:.1f} ATR below resistance, {stretch_to_low:.1f} ATR above support.\n"
    )
    if struct:
        ns = struct.get("nearest_supply"); nd = struct.get("nearest_demand")
        summary += (
            f"Structure: {struct['trend']} (last event {struct['last_event']}), "
            f"sweep {struct['swept']}, displacement {struct['fvg']}.\n"
            f"Supply zone {ns if ns is not None else '-'} "
            f"({struct.get('atr_to_supply')} ATR up), "
            f"demand zone {nd if nd is not None else '-'} "
            f"({struct.get('atr_to_demand')} ATR down).\n"
        )
    summary += f"Bars available: {len(prices)}"
    return facts, summary
