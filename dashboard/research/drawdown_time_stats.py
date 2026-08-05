"""Drawdown TIME-DIMENSION stats -- ADDED 2026-08-06, user-requested critique verification
(critic 1, item #5): this project has always reported drawdown DEPTH (maxDD) but never its
DURATION, which matters at least as much for real trading psychology (an -8% drawdown that
recovers in 3 weeks vs one that drags for 8 months are very different experiences, even at
identical depth). No new backtest methodology risk here -- reuses the SAME "core + reentry
gate + sleeve@10%" additive-blend config as full_live_config_retest.py (2026-08-06, same
day) and the SAME per-episode `dd_days`/`rec_days` data backtest.py's own
`_drawdown_episodes()` has already been computing all along; this just aggregates it into
the 3 stats the critique asked for, which weren't previously reported anywhere:

1. Max days to recovery -- trough to next new equity high (critic's own definition: "從 DD
   低點到創出淨值新高所需的最長交易日數" -- explicitly TROUGH-to-recovery, not the full
   peak-to-recovery round trip).
2. % of time spent >5% underwater -- fraction of (business) days the equity curve sat more
   than 5% below its running peak.
3. Time-weighted Calmar -- CAGR / mean absolute underwater depth (a Pain-Ratio/Ulcer-style
   variant), replacing the single-worst-point denominator with a full-history average, per
   the critique's "分母從'最大點回撤'改為'水下面積積分'" framing.

Run: uv run python -u -m dashboard.research.drawdown_time_stats
"""
from __future__ import annotations
import os
os.environ.setdefault("BROKER", "ib")
os.environ.setdefault("UNIVERSE", "etf")

import pandas as pd
import yfinance as yf

import dashboard.research.backtest as bt
from dashboard.instruments import active_universe
from dashboard.research.sleeve_blend import _sleeve_trades, _sleeve_unit_series, _cash_yield_series
from dashboard.core.sleeve import SLEEVE_UNIVERSE

LIVE_POS_CAP = 0.30
LIVE_PORTFOLIO_CAP = 1.0
LIVE_RISK = 0.01
REENTRY_BUFFER_R = 1.0
SLEEVE_WEIGHT = 0.10


def _core_with_reentry_gate() -> tuple[pd.Series, float]:
    """Same as full_live_config_retest.py's identically-named helper -- kept a separate
    copy (not imported) to match this project's existing one-script-per-investigation
    convention, and because that script's version isn't exported as a reusable API."""
    bt.POS_CAP = LIVE_POS_CAP
    bt.PORTFOLIO_CAP = LIVE_PORTFOLIO_CAP
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
        cands += bt._signals(df, inst.key, reentry_gate="reclaim_buffer",
                             reentry_buffer_r=REENTRY_BUFFER_R)
    eq, _ = bt._portfolio(cands, LIVE_RISK)
    eq = eq[~eq.index.duplicated(keep="last")].sort_index()
    s, e = eq.index[0], eq.index[-1]
    didx = pd.date_range(s, e, freq="B", tz="UTC")
    ret = (eq.reindex(eq.index.union(didx)).ffill().reindex(didx).ffill()
           .pct_change().fillna(0.0))
    years = (didx[-1] - didx[0]).days / 365.25
    return ret, years


def _time_stats(ret: pd.Series, years: float) -> dict:
    eq = (1 + ret).cumprod()
    eps = bt._drawdown_episodes(eq)
    recovered = [e for e in eps if e["rec_days"] is not None]
    max_days_to_recovery = max((e["rec_days"] for e in recovered), default=0)
    still_underwater = [e for e in eps if e["rec_days"] is None]
    underwater_frac = float((eq / eq.cummax() - 1 <= -0.05).mean())
    running_dd = (eq / eq.cummax() - 1)
    mean_underwater_depth = float(running_dd.mean())   # always <= 0
    cagr = eq.iloc[-1] ** (1 / years) - 1
    maxdd = running_dd.min()
    calmar = cagr / abs(maxdd) if maxdd else 0.0
    time_weighted_calmar = (cagr / abs(mean_underwater_depth)
                            if mean_underwater_depth else 0.0)
    return {
        "cagr": cagr, "maxdd": maxdd, "calmar": calmar,
        "max_days_to_recovery": max_days_to_recovery,
        "n_episodes_recovered": len(recovered),
        "still_underwater_at_end": len(still_underwater),
        "pct_time_gt5pct_underwater": underwater_frac,
        "mean_underwater_depth": mean_underwater_depth,
        "time_weighted_calmar": time_weighted_calmar,
    }


def _print_row(label: str, stats: dict) -> None:
    print(f"\n  {label}")
    print(f"    CAGR {stats['cagr']*100:+.2f}%  maxDD {stats['maxdd']*100:.2f}%  "
         f"Calmar (point) {stats['calmar']:.3f}")
    print(f"    Max days to recovery (trough -> new high): "
         f"{stats['max_days_to_recovery']} trading days "
         f"(~{stats['max_days_to_recovery']/21:.1f} months)")
    print(f"    Episodes fully recovered: {stats['n_episodes_recovered']}  |  "
         f"still underwater at end of data: {stats['still_underwater_at_end']}")
    print(f"    % of time spent >5% underwater: {stats['pct_time_gt5pct_underwater']*100:.1f}%")
    print(f"    Mean underwater depth (all-time avg drawdown-from-peak): "
         f"{stats['mean_underwater_depth']*100:.2f}%")
    print(f"    Time-weighted Calmar (CAGR / mean underwater depth, Pain-Ratio-style): "
         f"{stats['time_weighted_calmar']:.3f}")


def main() -> None:
    print("Fetching core book (reentry gate reclaim+1.0R, pos_cap=30%, risk=1%)...")
    core_ret, years = _core_with_reentry_gate()
    didx = core_ret.index

    print(f"Fetching sleeve data ({len(SLEEVE_UNIVERSE)} tickers, current live spec)...")
    sleeve_trades = {tk: _sleeve_trades(tk) for tk in SLEEVE_UNIVERSE}
    sleeve_unit = sum((_sleeve_unit_series(trs, didx) for trs in sleeve_trades.values()),
                      pd.Series(0.0, index=didx))

    print(f"\n{'='*100}\nFULL HISTORY ({years:.1f}y)\n{'='*100}")
    _print_row("core + gate only (no sleeve)", _time_stats(core_ret, years))
    _print_row(f"core + gate + sleeve@{SLEEVE_WEIGHT:.0%} (deployed config)",
              _time_stats(core_ret + SLEEVE_WEIGHT * sleeve_unit, years))

    cut = didx[0] + (didx[-1] - didx[0]) * 0.6
    oos_yrs = (didx[-1] - cut).days / 365.25
    core_oos = core_ret[core_ret.index >= cut]
    sleeve_oos = sleeve_unit[sleeve_unit.index >= cut]
    print(f"\n{'='*100}\nOOS (last 40%, {oos_yrs:.1f}y)\n{'='*100}")
    _print_row("core + gate only (no sleeve)", _time_stats(core_oos, oos_yrs))
    _print_row(f"core + gate + sleeve@{SLEEVE_WEIGHT:.0%} (deployed config)",
              _time_stats(core_oos + SLEEVE_WEIGHT * sleeve_oos, oos_yrs))

    print("\nNOTE: 'max days to recovery' and 'still underwater at end' are the honest "
         "companions to the maxDD figure reported everywhere else -- a deep-but-fast "
         "drawdown and a shallow-but-endless one both matter, and only depth was ever "
         "surfaced before this.")


if __name__ == "__main__":
    main()
