"""Live-vs-backtest drift check -- ADDED 2026-08-26.

dashboard/research/live_vs_backtest.py already answers "are real closed trades still
behaving like the backtest said they would?", but only as a manually-run script whose
answer never surfaces on the dashboard. This module wraps that same methodology so the
Retrospective tab can show it continuously:

  - Real per-strategy stats (core vs panic-MR sleeve) over broker-executed closed trades.
  - The backtest's expected per-trade distribution as a cached reference, computed ON
    DEMAND via a UI button (it downloads max-history weekly data for the whole universe
    -- minutes of work, absolutely not something a page render should ever do) and then
    reused forever until config changes invalidate it.
  - A binomial test on win-rate and a one-sample t-test on expectancy-R, honestly
    caveated by n exactly like paper.stats()'s own "trustworthy" bar.

No NiceGUI import -- pure functions with their own tests.
"""
from __future__ import annotations

import datetime as dt
import os

from dashboard.core.log import log

_CACHE_KEY = "backtest_reference"
# Bump when the deployed config changes materially enough that an old reference stops
# describing what live trades SHOULD look like (see README's parameter-freeze rules --
# this list is intentionally short; bug fixes don't touch it).
_REF_CONFIG = "risk=1% pos_cap=30% gate+sleeve"


def strategy_split(trades: list[dict]) -> dict[str, list[float]]:
    """Closed-trade realized-R lists split by strategy. Sleeve membership is decided by
    method == SLEEVE_METHOD (same rule research scripts use); everything else is core.
    Caller is responsible for passing only CLOSED, BROKER-EXECUTED trades -- same
    discipline as retrospective_panel's own _demo_executed_ids() filter."""
    from dashboard.core import paper
    from dashboard.core.sleeve import SLEEVE_METHOD

    out: dict[str, list[float]] = {"core": [], "sleeve": []}
    for t in trades:
        if t["status"] == "OPEN" or t["realized_r"] is None:
            continue
        key = "sleeve" if t.get("method") == SLEEVE_METHOD else "core"
        out[key].append(float(t["realized_r"]))
    return {k: paper.stats(v) for k, v in out.items()}


def cached_reference() -> tuple[dict | None, float | None]:
    """The stored backtest reference (None if never computed), plus its cache timestamp."""
    from dashboard.core import store
    ref, ts = store.cache_get(_CACHE_KEY)
    if not ref or ref.get("config") != _REF_CONFIG:
        return None, None          # stale reference for a superseded config -> ignore
    return ref, ts


def compute_reference(pos_cap: float = 0.30, portfolio_cap: float = 1.0) -> dict:
    """Run the SAME backtest-reference computation live_vs_backtest.py uses, and cache it.
    Slow (minutes: full-history weekly downloads for the whole active universe) -- must
    only ever be called from an explicit user action, never a render path."""
    os.environ.setdefault("BROKER", "ib")
    os.environ.setdefault("UNIVERSE", "etf")
    from dashboard.core import store
    from dashboard.research.live_vs_backtest import _backtest_reference

    log.warning("drift: computing backtest reference (%s) -- this takes minutes", _REF_CONFIG)
    stats = _backtest_reference(pos_cap, portfolio_cap)
    ref = {
        "config": _REF_CONFIG,
        "pos_cap": pos_cap, "portfolio_cap": portfolio_cap,
        "n": stats["n"], "win_rate": stats["win_rate"],
        "expectancy_R": stats["expectancy_R"], "profit_factor": stats["profit_factor"],
        "computed_ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    store.cache_set(_CACHE_KEY, ref)
    log.warning("drift: backtest reference computed and cached: %s", ref)
    return ref


def compare(real_stats: dict, raw_rs: list[float], ref: dict) -> dict:
    """Win-rate binomial p-value + expectancy one-sample t-test p-value vs the backtest
    reference. p < 0.05 on EITHER means live is measurably diverging from what was
    validated -- which per the forward-test protocol means investigate, not 'the edge
    is gone'. Needs raw realized-R values (not just summaries) for the t-test; callers
    pass the same list strategy_split()'s stats were computed from."""
    out: dict = {"win_p": None, "exp_p": None}
    n = real_stats.get("n") or 0
    if not n or not ref.get("n"):
        return out
    try:
        from scipy import stats as sps
        wins = int(round((real_stats.get("win_rate") or 0.0) * n))
        out["wins"] = wins
        out["win_p"] = float(sps.binomtest(wins, n, ref["win_rate"]).pvalue)
        if len(raw_rs) >= 2:
            res = sps.ttest_1samp(raw_rs, ref["expectancy_R"])
            out["exp_p"] = float(res.pvalue)
    except Exception as e:                             # noqa: BLE001
        log.debug("drift: significance tests unavailable: %s", e)
    return out
