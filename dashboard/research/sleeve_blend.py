"""Permanent, reusable tool: blend the REAL panic-MR dip-buy sleeve (core/sleeve.py's exact
entry/exit spec, not an approximation) against the CURRENT live core book (active_universe(),
i.e. the actual deployed 22-ETF set under BROKER=ib/UNIVERSE=etf -- NOT a hand-maintained
ticker list, so it can't silently drift out of sync with the live universe the way the earlier
one-off scripts (dipbuy_refine3.py etc.) did).

Built 2026-07-11 because this exact test ("core + sleeve at some weight, at some POS_CAP/
PORTFOLIO_CAP") had already been requested and rebuilt from scratch as throwaway scratch code
twice this session -- worth making permanent. Data (core weekly bars + sleeve daily bars) is
fetched ONCE and reused across every --weight/--pos-cap/--portfolio-cap/--tickers combination
passed on one invocation, so a sweep is cheap.

Run (matches the live config): uv run python -u -m dashboard.research.sleeve_blend \
    --pos-cap 0.25 --portfolio-cap 1.0 --weight 0.10
Sweep several weights/caps in one data fetch:
  uv run python -u -m dashboard.research.sleeve_blend --weight 0.05,0.10,0.15 \
      --portfolio-cap 1.0,0.8
"""
from __future__ import annotations
import os
os.environ.setdefault("BROKER", "ib")
os.environ.setdefault("UNIVERSE", "etf")

import argparse
import numpy as np
import pandas as pd
import yfinance as yf

import dashboard.research.backtest as bt
from dashboard.instruments import active_universe
from dashboard.core.sleeve import SLEEVE_UNIVERSE, SLEEVE_STAGE_2A

COST = 0.0010          # 10bp round-trip cost, matches the adopted spec (dipbuy_refine3.py)
STOP_FRAC = 0.05
TARGET_FRAC = 0.03
TIME_CAP_DAYS = 10


_IRX_CACHE = None


def _cash_yield_series():
    """Real ^IRX 13wk T-bill rate, same fetch as backtest.py's --cash-yield flag (idle-cash
    interest / margin-debit cost -- without this, comparing against the documented
    cash-yield-modeled baselines elsewhere in HANDOFF is apples-to-oranges)."""
    global _IRX_CACHE
    if _IRX_CACHE is None:
        irx = yf.download("^IRX", period="max", interval="1wk", progress=False, auto_adjust=True)
        if hasattr(irx.columns, "nlevels") and irx.columns.nlevels > 1:
            irx.columns = irx.columns.get_level_values(0)
        s = (irx["Close"].dropna() / 100.0)
        if s.index.tz is None:
            s.index = s.index.tz_localize("UTC")
        _IRX_CACHE = s
    return _IRX_CACHE


def _core_weekly_returns(pos_cap: float | None, portfolio_cap: float | None,
                         cash_yield: bool = False, risk: float = 0.01) -> tuple[pd.Series, float, float]:
    """The current live core book's DAILY-resampled return series (ffilled from its own
    weekly equity curve, so it lines up with the sleeve's daily bars for blending)."""
    bt.POS_CAP = pos_cap
    bt.PORTFOLIO_CAP = portfolio_cap
    bt.CASH_YIELD = _cash_yield_series() if cash_yield else None
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
    eq, _ = bt._portfolio(cands, risk)
    eq = eq[~eq.index.duplicated(keep="last")].sort_index()
    s, e = eq.index[0], eq.index[-1]
    didx = pd.date_range(s, e, freq="B", tz="UTC")
    ret = (eq.reindex(eq.index.union(didx)).ffill().reindex(didx).ffill()
           .pct_change().fillna(0.0))
    years = (didx[-1] - didx[0]).days / 365.25
    return ret, years, len(cands)


def _sleeve_trades(ticker: str) -> list[dict]:
    """EXACT reproduction of core/sleeve.py's entry_signal()/should_exit_dynamic(): close <
    20dMA*0.975, VIX +15%/5d, RSI14<35, ADX14>20 -> long; exit at first of 5MA-touch / +3% TP /
    -5% SL / 10 trading days. No look-ahead (entry i uses only bars up to i; exit walk starts
    at i+1)."""
    p = yf.download([ticker, "^VIX"], period="max", interval="1d", progress=False, auto_adjust=True)["Close"]
    if ticker not in p.columns:
        return []
    s = p[ticker].dropna()
    v = p["^VIX"].reindex(s.index).ffill()
    ph = yf.download(ticker, period="max", interval="1d", progress=False, auto_adjust=True)
    if hasattr(ph.columns, "nlevels") and ph.columns.nlevels > 1:
        ph.columns = ph.columns.get_level_values(0)
    h, lo = ph["High"].reindex(s.index), ph["Low"].reindex(s.index)
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
    sv, vv, m5v, m20v = s.values, v.values, ma5.values, ma20.values
    r14v, adxv, idx = r14.values, adx.values, s.index
    vix_up = vv / np.roll(vv, 5) - 1.0
    ent = (sv < m20v * 0.975) & (vix_up > 0.15) & (r14v < 35) & (adxv > 20)
    ent = np.nan_to_num(ent).astype(bool)
    ent[:200] = False                                   # warm-up (indicators need history)
    n = len(sv); out = []; i = 200
    while i < n - 1:
        if not ent[i]:
            i += 1; continue
        e = sv[i]; j = i + 1; R = None
        while j < n:
            r = sv[j] / e - 1.0
            if sv[j] >= m5v[j] or r >= TARGET_FRAC:
                R = r; break
            if r <= -STOP_FRAC:
                R = -STOP_FRAC; break
            if (j - i) >= TIME_CAP_DAYS:
                R = r; break
            j += 1
        if R is None:
            R = sv[min(j, n - 1)] / e - 1.0
        out.append({"d": idx[min(j, n - 1)], "r": R - COST})
        i = j + 1
    return out


def _sleeve_unit_series(trades: list[dict], didx: pd.DatetimeIndex) -> pd.Series:
    u = pd.Series(0.0, index=didx)
    s, e = didx[0], didx[-1]
    for t in trades:
        ts = pd.Timestamp(t["d"])
        ts = ts.tz_localize("UTC") if ts.tz is None else ts
        if s <= ts <= e:
            u.iloc[didx.searchsorted(ts)] += t["r"]
    return u


def _metrics(ret: pd.Series, years: float) -> tuple[float, float, float]:
    eq = (1 + ret).cumprod()
    mo = eq.resample("ME").last().pct_change().dropna()
    cagr = eq.iloc[-1] ** (1 / years) - 1
    maxdd = (eq / eq.cummax() - 1).min()
    sharpe = (mo.mean() / mo.std() * (12 ** 0.5)) if mo.std() > 0 else 0.0
    return cagr, maxdd, sharpe


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pos-cap", type=float, default=0.25)
    ap.add_argument("--portfolio-cap", type=str, default="1.0",
                    help="comma-separated list, e.g. 1.0,0.8")
    ap.add_argument("--weight", type=str, default="0.10",
                    help="comma-separated sleeve weight(s), e.g. 0.05,0.10,0.15")
    ap.add_argument("--tickers", choices=["3", "11"], default="11",
                    help="3 = SLEEVE_STAGE_2A (SPY/QQQ/XLK); 11 = full SLEEVE_UNIVERSE")
    ap.add_argument("--cash-yield", action="store_true",
                    help="credit idle cash at real ^IRX rate (matches the documented "
                         "cash-yield-modeled baselines elsewhere in HANDOFF -- omit for a "
                         "strategy-only comparison)")
    ap.add_argument("--risk", type=float, default=0.01,
                    help="core RISK_PER_TRADE (default 0.01 = the actual live setting)")
    args = ap.parse_args()
    weights = [float(w) for w in args.weight.split(",")]
    caps = [float(c) for c in args.portfolio_cap.split(",")]
    tickers = SLEEVE_STAGE_2A if args.tickers == "3" else SLEEVE_UNIVERSE

    print(f"Fetching sleeve daily data for {len(tickers)} tickers ({tickers})...")
    sleeve_trades = {tk: _sleeve_trades(tk) for tk in tickers}
    for tk, trs in sleeve_trades.items():
        rs = np.array([t["r"] for t in trs]) if trs else np.array([])
        print(f"  {tk:<6} n={len(trs):<4} meanR {rs.mean()*100 if len(rs) else 0:+.2f}% "
              f"win {(rs > 0).mean()*100 if len(rs) else 0:.0f}%")

    print(f"\n{'cap':>6}{'weight':>8}{'CAGR':>9}{'maxDD':>9}{'Sharpe':>9}{'Calmar':>9}")
    n_core = 0
    years = 0.0
    for cap in caps:
        core_ret, years, n_core = _core_weekly_returns(args.pos_cap, cap, args.cash_yield, args.risk)
        didx = core_ret.index
        sleeve_unit = sum((_sleeve_unit_series(trs, didx) for trs in sleeve_trades.values()),
                          pd.Series(0.0, index=didx))
        c_cagr, c_dd, c_sh = _metrics(core_ret, years)
        print(f"{cap:>6.0%}{'core only':>8}{c_cagr*100:>9.2f}{c_dd*100:>9.2f}{c_sh:>9.3f}"
              f"{(c_cagr/abs(c_dd) if c_dd else 0):>9.3f}")
        for w in weights:
            b_cagr, b_dd, b_sh = _metrics(core_ret + w * sleeve_unit, years)
            calmar = b_cagr / abs(b_dd) if b_dd else 0
            print(f"{cap:>6.0%}{w:>8.0%}{b_cagr*100:>9.2f}{b_dd*100:>9.2f}{b_sh:>9.3f}{calmar:>9.3f}")
    print(f"\n({n_core} core signals, {years:.1f}y span, "
          f"pos-cap {args.pos_cap:.0%}, cost {COST:.2%}/trade, no look-ahead)")


if __name__ == "__main__":
    main()
