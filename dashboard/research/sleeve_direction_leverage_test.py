"""Tests two extensions to the LIVE panic-MR dip-buy sleeve (core/sleeve.py), both
user-requested 2026-07-31:

  (1) DIP-SELL sleeve: a mirror-image signal (euphoric melt-up + overbought + VIX
      COMPRESSION, instead of panic + oversold + VIX SPIKE) that buys an INVERSE ETF
      instead of shorting the underlying (this project is long-only throughout --
      LONG_ONLY / "short side disabled" is a hard constraint elsewhere in paper.py, so an
      inverse product is the only way to express a bearish view without breaking that).
  (2) LEVERAGED variants of both the existing dip-buy sleeve and the new dip-sell sleeve
      (2x/3x long AND 2x/3x inverse products).

Methodology, important: entry/exit TIMING is always computed off the UNDERLYING's own
price/RSI/ADX/VIX (exactly reproducing core/sleeve.py's real signal, so this is judging
"would a different INSTRUMENT choice on the same signal help", not a different signal).
The REALIZED return for inverse/leveraged variants is read from that PRODUCT's OWN real
daily closes over the same trade window -- NOT synthesized as -1x/2x/3x the underlying's
return. Leveraged/inverse ETFs compound daily and suffer real volatility decay + expense
ratio + tracking error that a synthetic multiplier would silently erase; the whole point
of this test is to see whether that decay eats the theoretical leverage benefit, so using
each product's real price series is the only honest way to answer that.

Data availability is a real, disclosed constraint: most of these products only exist since
~2006-2010 (some later), far short of the core book's 33-year history -- every reported n
and per-ticker date range is real, not padded.

Run: uv run python -u -m dashboard.research.sleeve_direction_leverage_test
     uv run python -u -m dashboard.research.sleeve_direction_leverage_test --oos --weight 0.05,0.10
"""
from __future__ import annotations
import os
os.environ.setdefault("BROKER", "ib")
os.environ.setdefault("UNIVERSE", "etf")

import argparse
import numpy as np
import pandas as pd
import yfinance as yf

from dashboard.research.sleeve_blend import (
    _core_weekly_returns, _sleeve_trades, _sleeve_unit_series, _metrics, COST,
    STOP_FRAC, TARGET_FRAC, TIME_CAP_DAYS,
)
from dashboard.core.sleeve import SLEEVE_UNIVERSE

# Product map: underlying -> (inverse 1x, inverse 2x, inverse 3x, long 2x, long 3x).
# None where no sufficiently liquid/long-history product exists (checked against real
# yfinance data below, not assumed) -- HYG/PFF/ASHR have no viable leveraged/inverse
# products at all and are excluded from this test entirely, not silently zero-filled.
PRODUCT_MAP: dict[str, dict[str, str | None]] = {
    "SPY": {"inv1": "SH",  "inv2": "SDS", "inv3": "SPXU", "lev2": "SSO",  "lev3": "UPRO"},
    "QQQ": {"inv1": "PSQ", "inv2": "QID", "inv3": "SQQQ", "lev2": "QLD",  "lev3": "TQQQ"},
    "DIA": {"inv1": "DOG", "inv2": "DXD", "inv3": "SDOW", "lev2": "DDM",  "lev3": "UDOW"},
    "IWM": {"inv1": "RWM", "inv2": "TWM", "inv3": "SRTY", "lev2": "UWM",  "lev3": "URTY"},
    "EFA": {"inv1": "EFZ", "inv2": "EFU", "inv3": None,   "lev2": "EFO",  "lev3": None},
    "EEM": {"inv1": "EUM", "inv2": "EEV", "inv3": None,   "lev2": "EET",  "lev3": None},
    "VNQ": {"inv1": "REK", "inv2": "SRS", "inv3": None,   "lev2": "URE",  "lev3": None},
    "XLK": {"inv1": None,  "inv2": None,  "inv3": "TECS", "lev2": "ROM",  "lev3": "TECL"},
}


def _daily_close(ticker: str) -> pd.Series | None:
    try:
        raw = yf.download(ticker, period="max", interval="1d", progress=False, auto_adjust=True)
    except Exception:
        return None
    if raw is None or len(raw) == 0:
        return None
    if hasattr(raw.columns, "nlevels") and raw.columns.nlevels > 1:
        raw.columns = raw.columns.get_level_values(0)
    s = raw["Close"].dropna()
    if s.index.tz is None:
        s.index = s.index.tz_localize("UTC")
    return s if len(s) > 30 else None


def _underlying_signal_frame(ticker: str) -> dict | None:
    """Everything needed to compute BOTH dip-buy and dip-sell entries off one underlying
    fetch (VIX, close, MA5/MA20, RSI14, ADX14) -- shared so we don't refetch per variant."""
    p = yf.download([ticker, "^VIX"], period="max", interval="1d", progress=False, auto_adjust=True)["Close"]
    if ticker not in p.columns:
        return None
    s = p[ticker].dropna()
    v = p["^VIX"].reindex(s.index).ffill()
    ph = yf.download(ticker, period="max", interval="1d", progress=False, auto_adjust=True)
    if hasattr(ph.columns, "nlevels") and ph.columns.nlevels > 1:
        ph.columns = ph.columns.get_level_values(0)
    h, lo = ph["High"].reindex(s.index), ph["Low"].reindex(s.index)
    if s.index.tz is None:
        naive_idx = s.index
        s.index = naive_idx.tz_localize("UTC")
        v.index = s.index; h.index = s.index; lo.index = s.index
    ma5 = s.rolling(5).mean(); ma20 = s.rolling(20).mean()
    d = s.diff()
    g = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    r14 = 100 - 100 / (1 + g / l.replace(0, np.nan))
    tr = pd.concat([(h - lo), (h - s.shift()).abs(), (lo - s.shift()).abs()], axis=1).max(axis=1)
    up = h.diff(); dn = -lo.diff()
    pl = ((up > dn) & (up > 0)) * up
    mi = ((dn > up) & (dn > 0)) * dn
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    pdi = 100 * pl.ewm(alpha=1 / 14, adjust=False).mean() / atr
    mdi = 100 * mi.ewm(alpha=1 / 14, adjust=False).mean() / atr
    adx = (100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)).ewm(alpha=1 / 14, adjust=False).mean()
    return {"idx": s.index, "close": s.values, "vix": v.values, "ma5": ma5.values,
            "ma20": ma20.values, "rsi14": r14.values, "adx14": adx.values}


def _entry_exit_windows(f: dict, direction: str) -> list[tuple[int, int]]:
    """Bar-index (entry, exit) pairs off the underlying's own price action -- direction=
    'buy' reproduces core/sleeve.py's real entry_signal()/should_exit_dynamic() exactly
    (panic dip: close<20MA*0.975, VIX+15%/5d, RSI14<35, ADX>20; exit @ 5MA-touch/+3%/-5%/
    10d). direction='sell' is the deliberate mirror image (melt-up: close>20MA*1.025, VIX
    -15%/5d [complacency], RSI14>65, ADX>20; exit @ 5MA-touch-from-above/-3%/+5%/10d) --
    same magnitudes, same exit structure, opposite side."""
    sv, vv, m5, m20, r14v, adxv = (f["close"], f["vix"], f["ma5"], f["ma20"],
                                    f["rsi14"], f["adx14"])
    vix_chg = vv / np.roll(vv, 5) - 1.0
    if direction == "buy":
        ent = (sv < m20 * 0.975) & (vix_chg > 0.15) & (r14v < 35) & (adxv > 20)
    else:
        ent = (sv > m20 * 1.025) & (vix_chg < -0.15) & (r14v > 65) & (adxv > 20)
    ent = np.nan_to_num(ent).astype(bool)
    ent[:200] = False
    n = len(sv); out = []; i = 200
    while i < n - 1:
        if not ent[i]:
            i += 1; continue
        j = i + 1
        while j < n:
            r = sv[j] / sv[i] - 1.0
            if direction == "buy":
                if sv[j] >= m5[j] or r >= TARGET_FRAC or r <= -STOP_FRAC or (j - i) >= TIME_CAP_DAYS:
                    break
            else:
                if sv[j] <= m5[j] or r <= -TARGET_FRAC or r >= STOP_FRAC or (j - i) >= TIME_CAP_DAYS:
                    break
            j += 1
        out.append((i, min(j, n - 1)))
        i = j + 1
    return out


def _realized_from_product(underlying_idx: pd.DatetimeIndex, windows: list[tuple[int, int]],
                           product: pd.Series) -> list[dict]:
    """Realized R for each (entry,exit) window, read from PRODUCT's own real close prices
    at the underlying's entry/exit DATES (nearest available product bar, no look-ahead --
    uses the last product bar at or before each date). Skips a window entirely if the
    product has no data covering it (product launched later, or already delisted)."""
    out = []
    for i, j in windows:
        d_in, d_out = underlying_idx[i], underlying_idx[j]
        pos_in = product.index.searchsorted(d_in, side="right") - 1
        pos_out = product.index.searchsorted(d_out, side="right") - 1
        if pos_in < 0 or pos_out < 0 or pos_out <= pos_in:
            continue
        # require the matched product bar to be within 5 calendar days of the signal date --
        # otherwise the product simply wasn't trading yet / already delisted at that point,
        # and silently using a stale far-away price would fabricate a trade that never
        # could have happened.
        if (d_in - product.index[pos_in]).days > 5 or (d_out - product.index[pos_out]).days > 5:
            continue
        e, x = product.iloc[pos_in], product.iloc[pos_out]
        out.append({"d": d_out, "r": (x / e - 1.0) - COST})
    return out


def _variant_trades(underlying: str, direction: str, product_ticker: str | None) -> list[dict]:
    if product_ticker is None:
        return []
    f = _underlying_signal_frame(underlying)
    if f is None:
        return []
    windows = _entry_exit_windows(f, direction)
    product = _daily_close(product_ticker)
    if product is None:
        return []
    return _realized_from_product(f["idx"], windows, product)


def _report_line(label: str, ticker: str | None, trades: list[dict], product_series: pd.Series | None) -> None:
    if ticker is None:
        print(f"  {label:<28} --  no viable product, skipped")
        return
    if not trades:
        print(f"  {label:<28} {ticker:<7} n=0 (no usable overlap with real product history)")
        return
    rs = np.array([t["r"] for t in trades])
    span = f"{product_series.index[0].date()}..{product_series.index[-1].date()}" if product_series is not None else "?"
    print(f"  {label:<28} {ticker:<7} n={len(rs):<4} meanR {rs.mean()*100:+6.2f}% "
          f"win {(rs>0).mean()*100:4.0f}%  product history {span}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pos-cap", type=float, default=0.25)
    ap.add_argument("--portfolio-cap", type=float, default=1.0)
    ap.add_argument("--weight", type=str, default="0.10",
                    help="comma-separated sleeve weight(s) blended on top of core, e.g. 0.05,0.10")
    ap.add_argument("--risk", type=float, default=0.01)
    ap.add_argument("--oos", action="store_true")
    args = ap.parse_args()
    weights = [float(w) for w in args.weight.split(",")]

    tickers = list(PRODUCT_MAP.keys())
    print(f"Underlyings tested: {tickers} (HYG/PFF/ASHR excluded -- no viable leveraged/"
          f"inverse product found for them)\n")

    print("=" * 90)
    print("PER-TICKER SIGNAL QUALITY (mean R per trade, real product prices, real cost)")
    print("=" * 90)

    all_trades: dict[str, list[dict]] = {}
    for tk in tickers:
        pm = PRODUCT_MAP[tk]
        print(f"\n{tk}:")
        # baseline: existing live dip-buy sleeve, 1x underlying itself (already proven live)
        base_buy = _sleeve_trades(tk)
        all_trades[f"{tk}_buy_1x"] = base_buy
        rs = np.array([t["r"] for t in base_buy]) if base_buy else np.array([])
        print(f"  {'dip-BUY  (1x, live baseline)':<28} {tk:<7} n={len(rs):<4} "
              f"meanR {rs.mean()*100 if len(rs) else 0:+6.2f}% "
              f"win {(rs>0).mean()*100 if len(rs) else 0:4.0f}%")

        for direction, key, label in [("sell", "inv1", "dip-SELL (1x inverse, NEW)"),
                                       ("buy",  "lev2", "dip-BUY  (2x leveraged)"),
                                       ("buy",  "lev3", "dip-BUY  (3x leveraged)"),
                                       ("sell", "inv2", "dip-SELL (2x lev-inverse)"),
                                       ("sell", "inv3", "dip-SELL (3x lev-inverse)")]:
            ticker = pm.get(key)
            trades = _variant_trades(tk, direction, ticker) if ticker else []
            product = _daily_close(ticker) if ticker else None
            all_trades[f"{tk}_{key}"] = trades
            _report_line(label, ticker, trades, product)

    # ---- portfolio-level blend impact -------------------------------------------------
    print("\n" + "=" * 90)
    print("PORTFOLIO BLEND: core book + each sleeve variant, at the given weight(s)")
    print("=" * 90)
    core_ret, years, n_core = _core_weekly_returns(args.pos_cap, args.portfolio_cap, False, args.risk)
    didx = core_ret.index
    core_cagr, core_dd, core_sh = _metrics(core_ret, years)
    core_calmar = core_cagr / abs(core_dd) if core_dd else 0
    print(f"\ncore only: CAGR {core_cagr*100:+.2f}%  maxDD {core_dd*100:.2f}%  "
          f"Sharpe {core_sh:.3f}  Calmar {core_calmar:.3f}  ({n_core} signals, {years:.1f}y)")

    def _blend_row(label: str, keys: list[str], w: float) -> None:
        unit = sum((_sleeve_unit_series(all_trades.get(k, []), didx) for k in keys),
                   pd.Series(0.0, index=didx))
        n_trades = sum(len(all_trades.get(k, [])) for k in keys)
        ret = core_ret + w * unit
        cagr, dd, sh = _metrics(ret, years)
        calmar = cagr / abs(dd) if dd else 0
        beats = "BEATS core" if (cagr > core_cagr and abs(dd) <= abs(core_dd) * 1.01) else ""
        print(f"  w={w:.0%}  {label:<32} n={n_trades:<5} CAGR {cagr*100:+7.2f}%  "
              f"maxDD {dd*100:7.2f}%  Sharpe {sh:6.3f}  Calmar {calmar:6.3f}  {beats}")

    buy1x_keys = [f"{tk}_buy_1x" for tk in tickers]
    sell1x_keys = [f"{tk}_inv1" for tk in tickers if PRODUCT_MAP[tk]["inv1"]]
    lev2_keys = [f"{tk}_lev2" for tk in tickers if PRODUCT_MAP[tk]["lev2"]]
    lev3_keys = [f"{tk}_lev3" for tk in tickers if PRODUCT_MAP[tk]["lev3"]]
    sellinv2_keys = [f"{tk}_inv2" for tk in tickers if PRODUCT_MAP[tk]["inv2"]]
    sellinv3_keys = [f"{tk}_inv3" for tk in tickers if PRODUCT_MAP[tk]["inv3"]]
    both1x_keys = buy1x_keys + sell1x_keys

    for w in weights:
        print(f"\n-- weight {w:.0%} --")
        _blend_row("dip-BUY only (1x, live today)", buy1x_keys, w)
        _blend_row("dip-SELL only (1x inverse, NEW)", sell1x_keys, w)
        _blend_row("dip-BUY + dip-SELL (1x both directions)", both1x_keys, w)
        _blend_row("dip-BUY (2x leveraged)", lev2_keys, w)
        _blend_row("dip-BUY (3x leveraged)", lev3_keys, w)
        _blend_row("dip-SELL (2x lev-inverse)", sellinv2_keys, w)
        _blend_row("dip-SELL (3x lev-inverse)", sellinv3_keys, w)

    if args.oos:
        print("\n" + "=" * 90)
        print("OOS (last 40% of the core book's date range)")
        print("=" * 90)
        cut = didx[0] + (didx[-1] - didx[0]) * 0.6
        oos_yrs = (didx[-1] - cut).days / 365.25
        core_oos = core_ret[core_ret.index >= cut]
        c_cagr, c_dd, c_sh = _metrics(core_oos, oos_yrs)
        c_calmar = c_cagr / abs(c_dd) if c_dd else 0
        print(f"\ncore only (OOS): CAGR {c_cagr*100:+.2f}%  maxDD {c_dd*100:.2f}%  "
              f"Sharpe {c_sh:.3f}  Calmar {c_calmar:.3f}  ({oos_yrs:.1f}y)")

        def _blend_row_oos(label: str, keys: list[str], w: float) -> None:
            unit = sum((_sleeve_unit_series(all_trades.get(k, []), didx) for k in keys),
                       pd.Series(0.0, index=didx))
            unit_oos = unit[unit.index >= cut]
            n_trades = int((unit_oos != 0).sum())
            ret = core_oos + w * unit_oos
            cagr, dd, sh = _metrics(ret, oos_yrs)
            calmar = cagr / abs(dd) if dd else 0
            beats = "BEATS core" if (cagr > c_cagr and abs(dd) <= abs(c_dd) * 1.01) else ""
            print(f"  w={w:.0%}  {label:<32} n(bars)={n_trades:<5} CAGR {cagr*100:+7.2f}%  "
                  f"maxDD {dd*100:7.2f}%  Sharpe {sh:6.3f}  Calmar {calmar:6.3f}  {beats}")

        for w in weights:
            print(f"\n-- weight {w:.0%} --")
            _blend_row_oos("dip-BUY only (1x, live today)", buy1x_keys, w)
            _blend_row_oos("dip-SELL only (1x inverse, NEW)", sell1x_keys, w)
            _blend_row_oos("dip-BUY + dip-SELL (1x both directions)", both1x_keys, w)
            _blend_row_oos("dip-BUY (2x leveraged)", lev2_keys, w)
            _blend_row_oos("dip-BUY (3x leveraged)", lev3_keys, w)
            _blend_row_oos("dip-SELL (2x lev-inverse)", sellinv2_keys, w)
            _blend_row_oos("dip-SELL (3x lev-inverse)", sellinv3_keys, w)


if __name__ == "__main__":
    main()
