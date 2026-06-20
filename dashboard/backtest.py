"""Comprehensive 5-year PORTFOLIO backtest of the live config.

Unlike replay.py (per-instrument R only), this simulates the whole book:
  - signals on every instrument with the LIVE gates (strength>=MIN_STRENGTH,
    overextension filter, objective-edge gate),
  - a chronological portfolio walk applying de-correlation + one-position-per-
    instrument across instruments,
  - position sizing at RISK_PER_TRADE of COMPOUNDING equity,
so it yields an actual % return, max drawdown, and monthly distribution.

No look-ahead: each signal uses data up to its bar's close, enters at that close,
and is resolved on subsequent daily bars. The LLM veto is NOT modelled (not
replayable) -- this is the deterministic backbone, which is what places most
live trades anyway.

Run:  uv run python -u -m dashboard.backtest
"""
from __future__ import annotations
from . import net  # noqa: F401

import numpy as np
import pandas as pd

from analyst.features import compute_facts
from metrics import deflated_sharpe_ratio
from .instruments import UNIVERSE
from .providers import get_ohlc
from .scoring import score_from_facts
from . import paper, confidence_model
from .replay import _resolve_daily

RISK_LEVELS = [0.0025, 0.005, 0.01]   # 0.25% / 0.5% / 1%
START_EQUITY = 100.0
MIN_ADX: float | None = None          # set via --adx to add the trend-regime filter


def _adx(df, n=14):
    high, low, close = df["high"], df["low"], df["close"]
    up, dn = high.diff(), -low.diff()
    pdm = ((up > dn) & (up > 0)) * up
    mdm = ((dn > up) & (dn > 0)) * dn
    tr = pd.concat([high - low, (high - close.shift()).abs(),
                    (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    pdi = 100 * pdm.ewm(alpha=1 / n, adjust=False).mean() / atr
    mdi = 100 * mdm.ewm(alpha=1 / n, adjust=False).mean() / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


def _signals(df: pd.DataFrame, key: str) -> list[dict]:
    """All gate-passing ATR rr3 setups for one instrument (no look-ahead).
    Returns dicts: entry_i, entry_date, exit_date, direction, r."""
    close = df["close"]; n = len(df); i = 160; out = []
    adx = _adx(df) if MIN_ADX is not None else None
    while i < n - 1:
        facts, _ = compute_facts(close.iloc[: i + 1], key)
        score = score_from_facts(key, facts, "")
        if score.signal not in ("BUY", "SELL") or score.strength < paper.MIN_STRENGTH:
            i += 1; continue
        from .instruments import BY_KEY
        if paper.WEEKLY_TREND_CLASSES and BY_KEY[key].asset_class not in paper.WEEKLY_TREND_CLASSES:
            i += 1; continue
        if MIN_ADX is not None:
            a = adx.iloc[i]
            if not (a == a) or a < MIN_ADX:
                i += 1; continue
        direction = "long" if score.signal == "BUY" else "short"
        rsi = facts.get("rsi14") or 50.0
        if paper.OVEREXT_FILTER and (
                (direction == "long" and rsi > paper.OVEREXT_HI) or
                (direction == "short" and rsi < paper.OVEREXT_LO)):
            i += 1; continue
        obj = confidence_model.objective(score)        # s5 passes; included for parity
        if obj and obj[2] >= confidence_model.MIN_SAMPLES and obj[1] < paper.MIN_EDGE_R:
            i += 1; continue
        res = paper.compute_sltp(facts, direction, "ATR", paper.RR_DEFAULT)
        if res is None:
            i += 1; continue
        entry, sl, tp, rr_act = res
        if rr_act < paper.MIN_RR:
            i += 1; continue
        bars = df.iloc[i + 1: i + 1 + paper.HORIZON_DAYS]
        outcome = _resolve_daily(direction, entry, sl, tp, bars)
        if outcome is None:
            break
        status, exit_px, used = outcome
        r = paper.r_multiple(direction, entry, sl, exit_px)
        out.append({"key": key, "entry_i": i, "entry_date": df.index[i],
                    "exit_date": df.index[min(i + used, n - 1)],
                    "direction": direction, "r": r})
        i += used + 1
    return out


def _portfolio(cands: list[dict], risk: float) -> tuple[pd.Series, list[float]]:
    """Chronological book walk with de-correlation + one-per-instrument; size each
    trade at `risk` of current equity. Returns (equity-by-date, realized-R list)."""
    cands = sorted(cands, key=lambda c: c["entry_date"])
    equity = START_EQUITY
    open_pos: dict[int, dict] = {}      # id -> {exit_date, key, buckets, pnl}
    open_keys: set = set()
    open_buckets: set = set()           # (bucket, sign) currently held
    eq_points: list[tuple] = [(cands[0]["entry_date"], equity)] if cands else []
    realized: list[float] = []
    nid = 0

    def _close_due(upto):
        nonlocal equity
        for pid in [p for p, v in open_pos.items() if v["exit_date"] <= upto]:
            v = open_pos.pop(pid)
            equity += v["pnl"]
            open_keys.discard(v["key"])
            for b in v["buckets"]:
                open_buckets.discard(b)
            eq_points.append((v["exit_date"], equity))
            realized.append(v["r"])

    for c in cands:
        _close_due(c["entry_date"])
        if c["key"] in open_keys:
            continue                                    # already in this instrument
        buckets = paper._risk_buckets(c["key"], c["direction"])
        if any(b in open_buckets for b in buckets):
            continue                                    # de-correlation
        nid += 1
        risk_money = equity * risk
        open_pos[nid] = {"exit_date": c["exit_date"], "key": c["key"],
                         "buckets": buckets, "pnl": c["r"] * risk_money, "r": c["r"]}
        open_keys.add(c["key"])
        for b in buckets:
            open_buckets.add(b)
    _close_due(pd.Timestamp.max.tz_localize(eq_points[0][0].tz) if eq_points and eq_points[0][0].tz else pd.Timestamp.max)
    eq = pd.Series(dict(eq_points)).sort_index()
    eq = eq[~eq.index.duplicated(keep="last")]
    return eq, realized


def _metrics(eq: pd.Series, realized: list[float], years: float) -> dict:
    ret = (eq.iloc[-1] / eq.iloc[0]) - 1
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1 if years > 0 else 0
    peak = eq.cummax(); dd = (eq / peak - 1).min()
    monthly = eq.resample("ME").last().pct_change().dropna()
    return {"return": ret, "cagr": cagr, "maxdd": dd,
            "monthly_mean": monthly.mean() if len(monthly) else 0,
            "monthly_std": monthly.std() if len(monthly) else 0,
            "monthly_best": monthly.max() if len(monthly) else 0,
            "monthly_worst": monthly.min() if len(monthly) else 0,
            "pos_months": (monthly > 0).mean() if len(monthly) else 0,
            "n_months": len(monthly)}


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--adx", type=float, default=None, help="min ADX trend-regime filter")
    ap.add_argument("--weekly", action="store_true", help="resample to weekly bars")
    ap.add_argument("--longweekly", action="store_true",
                    help="fetch max-history WEEKLY bars from yfinance (for real OOS)")
    args = ap.parse_args()
    global MIN_ADX
    MIN_ADX = args.adx
    if MIN_ADX is not None:
        print(f"[ADX regime filter ON: ADX >= {MIN_ADX:.0f}]")
    weekly = args.weekly or args.longweekly
    if weekly:
        print("[LONG-HISTORY WEEKLY (yfinance max)]" if args.longweekly else "[WEEKLY bars]")
    print("Collecting signals across the universe...")
    data, cands = {}, []
    min_bars = 220 if args.longweekly else (120 if weekly else 300)
    for inst in UNIVERSE:
        if args.longweekly:
            import yfinance as yf
            raw = yf.download(inst.yf, period="max", interval="1wk",
                              progress=False, auto_adjust=True)
            if raw is None or len(raw) == 0:
                continue
            if hasattr(raw.columns, "nlevels") and raw.columns.nlevels > 1:
                raw.columns = raw.columns.get_level_values(0)
            df = raw[["Open", "High", "Low", "Close"]].copy()
            df.columns = ["open", "high", "low", "close"]
            df = df.dropna()
        else:
            df = get_ohlc(inst, period="5y", interval="1d")
            if df is None or len(df) < 300:
                continue
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        if args.weekly:
            df = df.resample("W").agg({"open": "first", "high": "max",
                                       "low": "min", "close": "last"}).dropna()
        if len(df) < min_bars:
            continue
        data[inst.key] = df
        cands += _signals(df, inst.key)
    span = max(df.index[-1] for df in data.values()) - min(df.index[0] for df in data.values())
    years = span.days / 365.25
    print(f"{len(data)} instruments | {len(cands)} gate-passing signals | "
          f"{years:.1f}y\n")
    print(f"Config: strength>={paper.MIN_STRENGTH}, overext={paper.OVEREXT_FILTER} "
          f"({paper.OVEREXT_HI:.0f}/{paper.OVEREXT_LO:.0f}), RR{paper.RR_DEFAULT}, "
          f"horizon {paper.HORIZON_DAYS}d\n")

    # per-trade quality (portfolio-filtered realized R at default risk)
    eq05, realized = _portfolio(cands, 0.005)
    s = paper.stats(realized)
    dsr = deflated_sharpe_ratio(pd.Series(realized), n_trials=1) if realized else 0
    per_year = len(realized) / years if years else 0
    print(f"PORTFOLIO TRADES (after de-correlation/one-per-instrument): {len(realized)}")
    print(f"  win {s['win_rate']:.0%} | expectancy {s['expectancy_R']:+.3f} R | "
          f"profit factor {s['profit_factor']:.2f} | total {s['total_R']:+.0f} R | "
          f"DSR {dsr:.0%}")
    print(f"  FREQUENCY: ~{per_year:.0f} trades/year | ~{per_year/52:.1f}/week | "
          f"~{per_year/252:.2f}/trading-day\n")

    print(f"{'risk/trade':<12}{'total %':>10}{'CAGR %':>9}{'max DD %':>10}"
          f"{'avg mo %':>10}{'mo std %':>9}{'worst mo':>10}{'+months':>9}")
    for risk in RISK_LEVELS:
        eq, real = _portfolio(cands, risk)
        m = _metrics(eq, real, years)
        print(f"{risk:<12.4%}{m['return']*100:>10.1f}{m['cagr']*100:>9.1f}"
              f"{m['maxdd']*100:>10.1f}{m['monthly_mean']*100:>10.2f}"
              f"{m['monthly_std']*100:>9.2f}{m['monthly_worst']*100:>10.1f}"
              f"{m['pos_months']*100:>8.0f}%")

    # IS / OOS split by date (default risk)
    print("\nIN-SAMPLE vs OUT-OF-SAMPLE (default 0.5% risk):")
    cut = min(c["entry_date"] for c in cands) + (span * 0.6)
    for lbl, sub in [("in-sample (first 60%)", [c for c in cands if c["entry_date"] <= cut]),
                     ("out-of-sample (40%)", [c for c in cands if c["entry_date"] > cut])]:
        if not sub:
            continue
        eq, real = _portfolio(sub, 0.005)
        yrs = (sub[-1]["entry_date"] - sub[0]["entry_date"]).days / 365.25 if len(sub) > 1 else 1
        m = _metrics(eq, real, max(yrs, 0.1))
        ss = paper.stats(real)
        print(f"  {lbl:<22} n={len(real):<4} win {ss['win_rate']:.0%} "
              f"expR {ss['expectancy_R']:+.3f} | CAGR {m['cagr']*100:+.1f}% "
              f"maxDD {m['maxdd']*100:.1f}%")
    print("\nNOTE: deterministic backbone only (no LLM veto). Costs = per-trade "
          "half-spread already in R. Past performance != future; the OOS row is "
          "the honest estimate.")


if __name__ == "__main__":
    main()
