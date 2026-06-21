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
from dashboard.core import net  # noqa: F401

import numpy as np
import pandas as pd

from analyst.features import compute_facts
from metrics import deflated_sharpe_ratio
from dashboard.instruments import active_universe, active_by_key
from dashboard.data.providers import get_ohlc
from dashboard.core.scoring import score_from_facts
from dashboard.core import paper
from dashboard.data import contracts
from dashboard.models import confidence_model
from dashboard.research.replay import _resolve_daily

RISK_LEVELS = [0.0025, 0.005, 0.01]   # 0.25% / 0.5% / 1%
START_EQUITY = 100.0
MIN_ADX: float | None = None          # set via --adx to add the trend-regime filter
# default matches the live config: long-only under BROKER=ib (paper.LONG_ONLY),
# both directions on MT5/spot. --direction / --direction-test override.
_DIRECTIONS: tuple = ("long",) if paper.LONG_ONLY else ("long", "short")
CONCENTRATED: bool = False    # --concentrated: drop one-per-instrument + de-correlation caps
CIRCUIT_DD: tuple | None = None  # --circuit: (stop, resume) e.g. (0.15,0.10): pause new
                                 # entries when portfolio DD >= stop, resume when DD <= resume


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


def _signals(df: pd.DataFrame, key: str, horizon: int | None = None,
             resolver=None, sl_method: str = "ATR") -> list[dict]:
    """All gate-passing setups for one instrument (no look-ahead).
    Returns dicts: entry_i, entry_date, exit_date, direction, r.
    `horizon` (bars) overrides paper.HORIZON_DAYS. `resolver` overrides the fixed
    SL/TP exit (exit-method test). `sl_method` ('ATR'|'STRUCT') sets SL/TP placement."""
    H = horizon if horizon is not None else paper.HORIZON_DAYS
    resolve = resolver or _resolve_daily
    directions = _DIRECTIONS
    close = df["close"]; n = len(df); i = 160; out = []
    adx = _adx(df) if MIN_ADX is not None else None
    while i < n - 1:
        facts, _ = compute_facts(close.iloc[: i + 1], key)
        score = score_from_facts(key, facts, "")
        if score.signal not in ("BUY", "SELL") or score.strength < paper.MIN_STRENGTH:
            i += 1; continue
        if paper.WEEKLY_TREND_CLASSES and active_by_key(key).asset_class not in paper.WEEKLY_TREND_CLASSES:
            i += 1; continue
        if MIN_ADX is not None:
            a = adx.iloc[i]
            if not (a == a) or a < MIN_ADX:
                i += 1; continue
        direction = "long" if score.signal == "BUY" else "short"
        if direction not in directions:           # direction-asymmetry test
            i += 1; continue
        rsi = facts.get("rsi14") or 50.0
        if paper.OVEREXT_FILTER and (
                (direction == "long" and rsi > paper.OVEREXT_HI) or
                (direction == "short" and rsi < paper.OVEREXT_LO)):
            i += 1; continue
        obj = confidence_model.objective(score)        # s5 passes; included for parity
        if obj and obj[2] >= confidence_model.MIN_SAMPLES and obj[1] < paper.MIN_EDGE_R:
            i += 1; continue
        res = paper.compute_sltp(facts, direction, sl_method, paper.RR_DEFAULT)
        if res is None:
            i += 1; continue
        entry, sl, tp, rr_act = res
        if rr_act < paper.MIN_RR:
            i += 1; continue
        bars = df.iloc[i + 1: i + 1 + H]
        outcome = resolve(direction, entry, sl, tp, bars)
        if outcome is None:
            break
        status, exit_px, used = outcome
        # futures (key in SPECS) use the realistic commission+slippage cost in
        # price points; spot/CFD keys (spec is None) keep the half-spread fraction.
        spec = contracts.SPECS.get(key)
        cost_abs = contracts.cost_points(spec) if spec else None
        r = paper.r_multiple(direction, entry, sl, exit_px, cost_abs=cost_abs)
        out.append({"key": key, "entry_i": i, "entry_date": df.index[i],
                    "exit_date": df.index[min(i + used, n - 1)],
                    "direction": direction, "r": r})
        i += used + 1
    return out


VOLTARGET_WINDOW = 20         # trailing CLOSED trades used to estimate vol
VOLTARGET_FACTOR_CAP = 3.0    # max leverage multiple in calm regimes
VOLTARGET_FACTOR_FLOOR = 0.25 # min multiple in turbulent regimes


def _portfolio(cands: list[dict], risk: float, target_vol: float | None = None,
               tpy: float = 33.0) -> tuple[pd.Series, list[float]]:
    """Chronological book walk with de-correlation + one-per-instrument; size each
    trade at `risk` of current equity. Returns (equity-by-date, realized-R list).

    If `target_vol` is set (annualised, e.g. 0.12), apply VOL TARGETING: scale each
    new trade's risk by clip(target_vol / trailing_vol, FLOOR, CAP), where
    trailing_vol is the annualised vol implied by the last VOLTARGET_WINDOW CLOSED
    trades at the base risk (std(R)*risk*sqrt(tpy)). Uses only already-closed trades
    -> no look-ahead. `tpy` = trades per year (for annualising). Bigger size in calm
    regimes, smaller in turbulent ones, to hold portfolio vol near target."""
    cands = sorted(cands, key=lambda c: c["entry_date"])
    equity = START_EQUITY
    open_pos: dict[int, dict] = {}      # id -> {exit_date, key, buckets, pnl}
    open_keys: set = set()
    open_buckets: set = set()           # (bucket, sign) currently held
    eq_points: list[tuple] = [(cands[0]["entry_date"], equity)] if cands else []
    realized: list[float] = []
    nid = 0

    def _vol_factor() -> float:
        if target_vol is None or len(realized) < VOLTARGET_WINDOW:
            return 1.0                              # off, or warming up
        sd = float(np.std(realized[-VOLTARGET_WINDOW:], ddof=1))
        ann_vol = sd * risk * (tpy ** 0.5)          # vol if sized at base risk
        if ann_vol <= 0:
            return VOLTARGET_FACTOR_CAP
        return float(min(VOLTARGET_FACTOR_CAP,
                         max(VOLTARGET_FACTOR_FLOOR, target_vol / ann_vol)))

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

    peak_eq = START_EQUITY
    paused = False
    for c in cands:
        _close_due(c["entry_date"])
        if CIRCUIT_DD:                                  # tail-risk circuit breaker
            peak_eq = max(peak_eq, equity)
            dd = (peak_eq - equity) / peak_eq if peak_eq > 0 else 0.0
            stop, resume = CIRCUIT_DD
            if not paused and dd >= stop:
                paused = True
            elif paused and dd <= resume:
                paused = False
            if paused:
                continue                                # no new entries while halted
        buckets = paper._risk_buckets(c["key"], c["direction"])
        if not CONCENTRATED:                            # risk caps (off in concentrated mode)
            if c["key"] in open_keys:
                continue                                # already in this instrument
            if any(b in open_buckets for b in buckets):
                continue                                # de-correlation
        nid += 1
        risk_money = equity * risk * _vol_factor()
        open_pos[nid] = {"exit_date": c["exit_date"], "key": c["key"],
                         "buckets": buckets, "pnl": c["r"] * risk_money, "r": c["r"]}
        open_keys.add(c["key"])
        for b in buckets:
            open_buckets.add(b)
    _close_due(pd.Timestamp.max.tz_localize(eq_points[0][0].tz) if eq_points and eq_points[0][0].tz else pd.Timestamp.max)
    eq = pd.Series(dict(eq_points)).sort_index()
    eq = eq[~eq.index.duplicated(keep="last")]
    return eq, realized


def _drawdown_episodes(eq: pd.Series) -> list[dict]:
    """Every distinct drawdown episode in the equity curve: depth, peak/trough
    dates, duration and recovery time. An episode runs from a new all-time-high
    peak, down to the trough, until equity reclaims that peak (or end-of-data)."""
    peak = eq.iloc[0]; peak_dt = eq.index[0]
    trough = eq.iloc[0]; trough_dt = eq.index[0]
    in_dd = False
    eps: list[dict] = []
    for dt_, v in eq.items():
        if v >= peak:
            if in_dd:                       # recovered -> close the episode
                eps.append({"depth": trough / peak - 1, "peak_dt": peak_dt,
                            "trough_dt": trough_dt, "recover_dt": dt_,
                            "dd_days": (trough_dt - peak_dt).days,
                            "rec_days": (dt_ - trough_dt).days})
                in_dd = False
            peak = v; peak_dt = dt_
        else:
            if not in_dd or v < trough:
                if not in_dd:
                    trough = v; trough_dt = dt_; in_dd = True
                if v < trough:
                    trough = v; trough_dt = dt_
    if in_dd:                               # still underwater at end of data
        eps.append({"depth": trough / peak - 1, "peak_dt": peak_dt,
                    "trough_dt": trough_dt, "recover_dt": None,
                    "dd_days": (trough_dt - peak_dt).days, "rec_days": None})
    return sorted(eps, key=lambda e: e["depth"])


def _dd_report(cands, years, risks=(0.005, 0.01, 0.015)) -> None:
    """REAL drawdown-frequency analysis (replaces hand-waved frequency tables):
    actual episode counts by depth bucket, and the worst episodes with dates."""
    print(f"\nDRAWDOWN REALITY CHECK ({years:.1f}y of data -- NOT 98y):")
    for risk in risks:
        eq, _ = _portfolio(cands, risk)
        eps = _drawdown_episodes(eq)
        buckets = [(0.02, 0.05), (0.05, 0.10), (0.10, 0.15), (0.15, 1.0)]
        print(f"\n  risk {risk:.2%}  (maxDD {min(e['depth'] for e in eps)*100:.1f}%):")
        print(f"    {'depth band':<14}{'episodes':>9}{'~once per':>12}")
        for lo, hi in buckets:
            n = sum(1 for e in eps if lo <= -e["depth"] < hi)
            freq = f"{years/n:.1f}y" if n else "never"
            print(f"    {-hi*100:.0f}%..{-lo*100:.0f}%{'':<6}{n:>9}{freq:>12}")
        worst = eps[:3]
        print(f"    worst 3: " + " | ".join(
            f"{e['depth']*100:.1f}% ({e['trough_dt'].date()}, "
            f"{'recovered '+str(e['rec_days'])+'d' if e['rec_days'] is not None else 'UNDERWATER'})"
            for e in worst))


def _span_years(trades: list[dict]) -> float:
    """Calendar years between the EARLIEST and LATEST entry. trades/cands are grouped
    by instrument (NOT date-sorted), so list[-1]-list[0] is wrong -- use min/max.
    (This was the IS/OOS CAGR bug: a tiny/garbage span inflated CAGR to ~90%.)"""
    ds = [c["entry_date"] for c in trades]
    return max((max(ds) - min(ds)).days / 365.25, 0.1) if len(ds) > 1 else 0.1


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


def _attribution(cands: list[dict]) -> None:
    """Per-class and per-market edge breakdown -- WHICH markets carry or kill the
    edge. Decides whether to add a class wholesale (bundle reject is lazy) or cherry
    -pick the few markets that individually clear the bar. Uses raw gate-passing
    signals (pre-de-correlation), which is the per-market edge before portfolio caps."""
    from collections import defaultdict
    by_cls: dict[str, list[float]] = defaultdict(list)
    by_key: dict[str, list[float]] = defaultdict(list)
    for c in cands:
        cls = active_by_key(c["key"]).asset_class
        by_cls[cls].append(c["r"]); by_key[c["key"]].append(c["r"])
    print("PER-CLASS edge (raw signals, expR = the honest per-trade number):")
    print(f"  {'class':<8}{'n':>6}{'expR':>9}{'totalR':>9}")
    for cls in sorted(by_cls, key=lambda k: -sum(by_cls[k])):
        rs = by_cls[cls]
        print(f"  {cls:<8}{len(rs):>6}{sum(rs)/len(rs):>+9.3f}{sum(rs):>+9.0f}")
    ranked = sorted(by_key.items(), key=lambda kv: sum(kv[1]) / len(kv[1]))
    def _row(k, rs): return f"  {k:<8}{len(rs):>6}{sum(rs)/len(rs):>+9.3f}{sum(rs):>+9.0f}"
    print("PER-MARKET worst 5 / best 5 (expR):")
    for k, rs in ranked[:5]:
        print(_row(k, rs))
    print("  " + "-" * 30)
    for k, rs in ranked[-5:]:
        print(_row(k, rs))
    print()


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--adx", type=float, default=None, help="min ADX trend-regime filter")
    ap.add_argument("--weekly", action="store_true", help="resample to weekly bars")
    ap.add_argument("--longweekly", action="store_true",
                    help="fetch max-history WEEKLY bars from yfinance (for real OOS)")
    ap.add_argument("--all-classes", action="store_true",
                    help="ignore WEEKLY_TREND_CLASSES whitelist -- trade ALL asset "
                         "classes (the diversification test: rates/grains/etc.)")
    ap.add_argument("--classes", type=str, default=None,
                    help="comma-separated asset classes to trade, e.g. "
                         "'metal,index,rate' (pre-specify a hypothesis; tests OOS)")
    ap.add_argument("--voltarget", type=float, default=None, metavar="VOL",
                    help="annualised portfolio vol target (e.g. 0.12); adds a "
                         "vol-targeted vs fixed-risk comparison at 0.5%% risk")
    ap.add_argument("--ddreport", action="store_true",
                    help="REAL drawdown-frequency analysis (episodes by depth) at "
                         "0.5/1.0/1.5%% risk -- the honest 'how often / how deep'")
    ap.add_argument("--horizon-curve", action="store_true",
                    help="sweep holding period 1-8 weekly bars (data fetched once), "
                         "OOS, with a pre-registered risk-aware decision rule")
    ap.add_argument("--direction-test", action="store_true",
                    help="long+short vs long-only vs short-only (regime/asymmetry), OOS")
    ap.add_argument("--direction", choices=["both", "long", "short"], default=None,
                    help="override trade direction (default: config = long-only under "
                         "BROKER=ib, both on MT5)")
    ap.add_argument("--exit-test", action="store_true",
                    help="compare exit methods (fixed/breakeven/trailing) on the "
                         "current locked config, OOS -- data fetched once")
    ap.add_argument("--concentrated", action="store_true",
                    help="drop one-per-instrument + de-correlation caps (concentrate "
                         "on strong trends -- higher return, much higher DD)")
    ap.add_argument("--circuit", action="store_true",
                    help="tail-risk circuit breaker: pause new entries when DD>=15%%, "
                         "resume when DD<=10%% (tests if it helps or just locks out the recovery)")
    args = ap.parse_args()
    global _DIRECTIONS, CONCENTRATED, CIRCUIT_DD
    if args.concentrated:
        CONCENTRATED = True
        print("[CONCENTRATED: risk caps OFF -- one-per-instrument + de-correlation disabled]")
    if args.circuit:
        CIRCUIT_DD = (0.15, 0.10)
        print("[CIRCUIT BREAKER: pause new entries when DD>=15%, resume when DD<=10%]")
    if args.direction:                            # explicit override; else keep config default
        _DIRECTIONS = {"both": ("long", "short"), "long": ("long",),
                       "short": ("short",)}[args.direction]
    print(f"[DIRECTION: {'+'.join(_DIRECTIONS)}]")
    if args.all_classes:
        paper.WEEKLY_TREND_CLASSES = set()   # set() = no whitelist = trade everything
        print("[ALL CLASSES: WEEKLY_TREND_CLASSES whitelist disabled]")
    elif args.classes:
        want = {c.strip() for c in args.classes.split(",") if c.strip()}
        # GUARD: a typo'd/plural class name (e.g. "rates" vs "rate") would silently
        # match nothing and quietly degrade the universe -> a wrong conclusion you'd
        # trust. Validate against the classes that actually exist in the universe.
        known = {active_by_key(i.key).asset_class for i in active_universe()}
        unknown = want - known
        if unknown:
            raise SystemExit(
                f"--classes: unknown asset class(es) {sorted(unknown)}. "
                f"Valid classes in this universe: {sorted(known)}. "
                f"(note: SINGULAR -- 'rate' not 'rates', 'soft' not 'softs'.)")
        paper.WEEKLY_TREND_CLASSES = want
        print(f"[CLASSES: {sorted(paper.WEEKLY_TREND_CLASSES)}]")
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
    for inst in active_universe():
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
    if args.horizon_curve:
        _horizon_curve(data, span, years)
        return
    if args.exit_test:
        _exit_test(data, span, years)
        return
    if args.direction_test:
        _direction_test(data, span, years)
        return
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

    _attribution(cands)

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
        yrs = _span_years(sub)
        m = _metrics(eq, real, max(yrs, 0.1))
        ss = paper.stats(real)
        print(f"  {lbl:<22} n={len(real):<4} win {ss['win_rate']:.0%} "
              f"expR {ss['expectancy_R']:+.3f} | CAGR {m['cagr']*100:+.1f}% "
              f"maxDD {m['maxdd']*100:.1f}%")
    if args.voltarget is not None:
        _voltarget_compare(cands, span, years, args.voltarget, per_year or 33.0)
    if args.ddreport:
        _dd_report(cands, years)

    print("\nNOTE: deterministic backbone only (no LLM veto). Costs = per-trade "
          "half-spread already in R. Past performance != future; the OOS row is "
          "the honest estimate.")


def _resolve_trail(direction, entry, sl, tp, bars, arm_r=1.0, dist_r=2.0):
    """Exit with a TRAILING stop. Once price reaches +arm_r in our favour, trail
    the stop dist_r (in initial-risk units) behind the best price since entry
    (chandelier-style). TP still caps the upside; horizon still time-stops.
    Conservative: trailing only TIGHTENS the stop, armed from bar close levels."""
    risk = abs(entry - sl)
    peak = entry
    for n, (_ts, row) in enumerate(bars.iterrows(), start=1):
        hi, lo = row["high"], row["low"]
        if direction == "long":
            if lo <= sl:
                return "LOSS" if sl <= entry else "WIN", sl, n
            if hi >= tp:
                return "WIN", tp, n
            peak = max(peak, hi)
            if peak - entry >= arm_r * risk:
                sl = max(sl, peak - dist_r * risk)
        else:
            if hi >= sl:
                return "LOSS" if sl >= entry else "WIN", sl, n
            if lo <= tp:
                return "WIN", tp, n
            peak = min(peak, lo)
            if entry - peak >= arm_r * risk:
                sl = min(sl, peak + dist_r * risk)
    if len(bars):
        return "EXPIRED", float(bars["close"].iloc[-1]), len(bars)
    return None


def _resolve_voltrail(direction, entry, sl, tp, bars, mult=3.0):
    """VOLATILITY-based trailing (chandelier): trail the stop `mult x CURRENT ATR`
    behind the best price, re-adapting ATR each bar (vs _resolve_trail which froze
    the trail unit at entry). Needs a per-bar 'atr' column on `bars`."""
    peak = entry
    for n, (_ts, row) in enumerate(bars.iterrows(), start=1):
        hi, lo, atr = row["high"], row["low"], row.get("atr", 0.0) or 0.0
        if direction == "long":
            if lo <= sl:
                return ("WIN" if sl > entry else "LOSS"), sl, n
            if hi >= tp:
                return "WIN", tp, n
            peak = max(peak, hi)
            if atr:
                sl = max(sl, peak - mult * atr)
        else:
            if hi >= sl:
                return ("WIN" if sl < entry else "LOSS"), sl, n
            if lo <= tp:
                return "WIN", tp, n
            peak = min(peak, lo)
            if atr:
                sl = min(sl, peak + mult * atr)
    if len(bars):
        return "EXPIRED", float(bars["close"].iloc[-1]), len(bars)
    return None


def _direction_test(data, span, years) -> None:
    """REGIME/ASYMMETRY test (the testable core of regime-dependent allocation):
    does the SHORT side earn its keep, or is long-only / asymmetric better? Futures
    TSMOM shorts are often weak. OOS @0.5% on the locked config. One run."""
    global _DIRECTIONS
    cut = min(min(df.index) for df in data.values()) + (span * 0.6)
    print("\nDIRECTION / ASYMMETRY TEST ({metal,index,rate}, 5wk, OOS @0.5%):\n")
    print(f"  {'side':<14}{'OOS expR':>10}{'OOS CAGR%':>11}{'OOS DD%':>9}{'CAGR/DD':>9}{'trades/yr':>11}")
    for label, dirs in [("long+short", ("long", "short")),
                        ("long-only", ("long",)), ("short-only", ("short",))]:
        _DIRECTIONS = dirs
        cands = []
        for key, df in data.items():
            cands += _signals(df, key)
        _DIRECTIONS = ("long", "short")
        oos = [c for c in cands if c["entry_date"] > cut]
        if len(oos) < 2:
            print(f"  {label:<14}(no OOS trades)"); continue
        yrs = _span_years(oos)
        eq, real = _portfolio(oos, 0.005)
        m = _metrics(eq, real, yrs); ss = paper.stats(real)
        cd = (m["cagr"] / abs(m["maxdd"])) if m["maxdd"] else 0
        print(f"  {label:<14}{ss['expectancy_R']:>+10.3f}{m['cagr']*100:>11.1f}"
              f"{m['maxdd']*100:>9.1f}{cd:>9.2f}{len(real)/yrs:>11.0f}")
    print("\n  RULE: drop the short side only if long-only beats long+short on BOTH "
          "OOS CAGR and CAGR/DD (a real asymmetry, not noise).")


def _exit_test(data, span, years) -> None:
    """Pre-specified EXIT-METHOD comparison on the CURRENT locked config (the prior
    breakeven test was on the old spot/daily universe -- doesn't transfer). One run,
    OOS @0.5%, then lock. Fixed SL/TP is the baseline trend-following exit; the rest
    'lock profit early', which theory says should HURT a trend system -- we verify."""
    from functools import partial
    from dashboard.research.ab_meanrev import _atr
    # per-bar ATR for the volatility-adaptive trail (chandelier needs CURRENT ATR)
    for df in data.values():
        if "atr" not in df.columns:
            df["atr"] = _atr(df, 14)
    # (label -> (sl_method, resolver)). sl_method sets SL/TP PLACEMENT (ATR vs
    # STRUCT); resolver sets EXIT MANAGEMENT (None=fixed, breakeven, trailing).
    methods = {
        "fixed (baseline)":   ("ATR",    None),
        "STRUCT SL/TP":       ("STRUCT", None),
        "breakeven @+1R":     ("ATR",    partial(_resolve_daily, breakeven_at=1.0)),
        "pure trail 2R arm0": ("ATR",    partial(_resolve_trail, arm_r=0.0, dist_r=2.0)),
        "vol-trail 3xATR":    ("ATR",    partial(_resolve_voltrail, mult=3.0)),
        "vol-trail 4xATR":    ("ATR",    partial(_resolve_voltrail, mult=4.0)),
    }
    cut = min(min(df.index) for df in data.values()) + (span * 0.6)
    print("\nEXIT-METHOD TEST (current config: {metal,index,rate}, 5wk, RR3, "
          "OOS @0.5%). Data fetched once.\n")
    print(f"  {'exit method':<20}{'OOS expR':>10}{'OOS CAGR%':>11}{'OOS DD%':>9}"
          f"{'CAGR/DD':>9}{'win%':>7}")
    base = None
    for label, (sl_method, resolver) in methods.items():
        cands = []
        for key, df in data.items():
            cands += _signals(df, key, resolver=resolver, sl_method=sl_method)
        oos = [c for c in cands if c["entry_date"] > cut]
        if len(oos) < 2:
            continue
        yrs = _span_years(oos)
        eq, real = _portfolio(oos, 0.005)
        m = _metrics(eq, real, yrs); ss = paper.stats(real)
        cd = (m["cagr"] / abs(m["maxdd"])) if m["maxdd"] else 0
        row = {"expR": ss["expectancy_R"], "cagr": m["cagr"], "cd": cd}
        if base is None:
            base = row
        tag = "  <- baseline" if label.startswith("fixed") else ""
        print(f"  {label:<20}{ss['expectancy_R']:>+10.3f}{m['cagr']*100:>11.1f}"
              f"{m['maxdd']*100:>9.1f}{cd:>9.2f}{ss['win_rate']*100:>7.0f}{tag}")
    print()
    print("  RULE: adopt a dynamic exit ONLY if it beats fixed on BOTH OOS expR and "
          "CAGR/DD. Trend-following theory says cutting winners early should LOSE.")


def _horizon_curve(data, span, years, horizons=range(1, 9)) -> None:
    """Holding-period response curve (1-8 weekly bars), judged OOS at 0.5% risk.
    Data is fetched ONCE and reused per horizon. Reports OOS CAGR/DD/ExpR/trades
    plus CAGR/DD (risk-adjusted) so a higher-CAGR-but-deeper-DD horizon can't be
    mistaken for an improvement. Baseline = current paper.HORIZON_DAYS."""
    cut = min(min(df.index) for df in data.values()) + (span * 0.6)
    print(f"\nHORIZON CURVE (weekly bars, OOS @0.5% risk; baseline = "
          f"{paper.HORIZON_DAYS}wk). Data fetched once, reused per horizon.\n")
    print(f"  {'horizon':<9}{'OOS expR':>10}{'OOS CAGR%':>11}{'OOS DD%':>9}"
          f"{'CAGR/DD':>9}{'trades/yr':>11}")
    base = None
    rows = []
    for H in horizons:
        cands = []
        for key, df in data.items():
            cands += _signals(df, key, horizon=H)
        oos = [c for c in cands if c["entry_date"] > cut]
        if len(oos) < 2:
            continue
        yrs = _span_years(oos)
        eq, real = _portfolio(oos, 0.005)
        m = _metrics(eq, real, yrs)
        ss = paper.stats(real)
        cd = (m["cagr"] / abs(m["maxdd"])) if m["maxdd"] else 0
        tag = "  <- baseline" if H == paper.HORIZON_DAYS else ""
        rows.append({"H": H, "expR": ss["expectancy_R"], "cagr": m["cagr"],
                     "dd": m["maxdd"], "cd": cd, "tpy": len(real) / yrs})
        if H == paper.HORIZON_DAYS:
            base = rows[-1]
        print(f"  {str(H)+'wk':<9}{ss['expectancy_R']:>+10.3f}{m['cagr']*100:>11.1f}"
              f"{m['maxdd']*100:>9.1f}{cd:>9.2f}{len(real)/yrs:>11.0f}{tag}")
    # pre-registered decision rule (risk-aware): higher CAGR AND CAGR/DD not worse
    if base:
        better = [r for r in rows if r["cagr"] > base["cagr"] and r["cd"] >= base["cd"]]
        print()
        if better:
            win = max(better, key=lambda r: r["cagr"])
            print(f"  RULE: {win['H']}wk beats {base['H']}wk baseline "
                  f"(CAGR {win['cagr']*100:.1f}% vs {base['cagr']*100:.1f}%, "
                  f"CAGR/DD {win['cd']:.2f} vs {base['cd']:.2f}) -> switch + lock.")
        else:
            print(f"  RULE: no horizon beats {base['H']}wk on CAGR without worsening "
                  f"CAGR/DD -> KEEP {base['H']}wk. Research over.")


def _voltarget_compare(cands, span, years, target_vol, tpy) -> None:
    """P5: fixed-risk vs vol-targeted, full + OOS, at 0.5% base risk."""
    print(f"\nVOL TARGETING @ {target_vol:.0%} annual (base 0.5% risk, factor "
          f"{VOLTARGET_FACTOR_FLOOR}-{VOLTARGET_FACTOR_CAP}x, {VOLTARGET_WINDOW}-trade window):")
    print(f"  {'variant':<26}{'CAGR %':>9}{'maxDD %':>10}{'CAGR/DD':>9}")
    cut = min(c["entry_date"] for c in cands) + (span * 0.6)
    oos = [c for c in cands if c["entry_date"] > cut]
    oos_yrs = _span_years(oos) if len(oos) > 1 else 1
    for lbl, tv in [("fixed risk", None), (f"vol-target {target_vol:.0%}", target_vol)]:
        for scope, sub, yrs in [("full", cands, years), ("OOS", oos, oos_yrs)]:
            eq, real = _portfolio(sub, 0.005, target_vol=tv, tpy=tpy)
            m = _metrics(eq, real, yrs)
            cd = (m["cagr"] / abs(m["maxdd"])) if m["maxdd"] else 0
            print(f"  {lbl+' ('+scope+')':<26}{m['cagr']*100:>9.1f}"
                  f"{m['maxdd']*100:>10.1f}{cd:>9.2f}")


if __name__ == "__main__":
    main()
