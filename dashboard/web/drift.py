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


def live_config() -> dict:
    """The deployed sizing config the backtest reference must match (F5 parity, 2026-11-21)."""
    from dashboard.core import paper
    return {"risk": float(paper.RISK_PER_TRADE),
            "pos_cap": float(os.environ.get("ETF_POS_CAP", "0.25") or 0.25),
            "portfolio_cap": float(os.environ.get("PORTFOLIO_CAP", "1.0") or 1.0)}


def ref_config_label(cfg: dict | None = None) -> str:
    """CHANGED 2026-11-21 (F5): derived from the LIVE config instead of a hard-coded
    "risk=1% pos_cap=30% gate+sleeve". The reference was pinned at pos_cap 30% while LIVE runs
    ETF_POS_CAP=0.25, and nothing noticed. A reference computed for another config is now
    'stale' (cached_reference() returns None), so the card asks for a recompute."""
    c = cfg or live_config()
    return (f"risk={c['risk']:.0%} pos_cap={c['pos_cap']:.0%} "
            f"pcap={c['portfolio_cap']:.0%} gate+sleeve")


def config_parity(ref: dict | None) -> list[str]:
    """PURE-ish (F5): human-readable mismatches between the deployed config and the config a
    cached backtest reference was computed with. [] = in parity."""
    if not ref:
        return ["no backtest reference computed for the deployed config"]
    live = live_config()
    out = []
    for k, label in (("pos_cap", "position cap"), ("portfolio_cap", "portfolio cap")):
        rv = ref.get(k)
        if rv is not None and abs(float(rv) - live[k]) > 1e-9:
            out.append(f"{label}: live {live[k]:.0%} vs reference {float(rv):.0%}")
    if ref.get("risk") is not None and abs(float(ref["risk"]) - live["risk"]) > 1e-9:
        out.append(f"risk/trade: live {live['risk']:.2%} vs reference {float(ref['risk']):.2%}")
    return out


# ---- F7: pre-registered kill rule (IBKR_STRATEGY_FIDELITY_SPEC, 2026-11-21) -------------------
KILL_RULE_MIN_N = int(os.environ.get("KILL_RULE_MIN_N", "30"))
KILL_RULE_CONF = float(os.environ.get("KILL_RULE_CONF", "0.90"))


def kill_rule(rs: list[float], min_n: int | None = None, conf: float | None = None) -> dict:
    """PURE: after >= min_n FUNDED, closed core trades, halt new entries if the one-sided
    `conf` upper bound of expectancy (t-interval) is below 0 -- i.e. we're `conf` sure the
    live edge is negative. Written down BEFORE the next trades so a bad stretch can't move the
    goalposts either way. -> {"verdict": "insufficient"|"ok"|"halt", "n", "mean", "upper"}."""
    import math
    min_n = KILL_RULE_MIN_N if min_n is None else min_n
    conf = KILL_RULE_CONF if conf is None else conf
    rs = [float(r) for r in rs if r is not None]
    n = len(rs)
    if n < max(min_n, 2):
        return {"verdict": "insufficient", "n": n, "mean": (sum(rs) / n) if n else None,
                "upper": None}
    mean = sum(rs) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rs) / (n - 1))
    try:
        from scipy import stats as sps
        tq = float(sps.t.ppf(conf, n - 1))
    except Exception:                                  # noqa: BLE001
        tq = 1.311 if conf >= 0.9 else 1.0             # ~t(0.90, 29)
    upper = mean + tq * sd / math.sqrt(n)
    return {"verdict": "halt" if upper < 0 else "ok", "n": n, "mean": mean, "upper": upper}


def funded_core_rs() -> list[float]:
    """Realized R of FUNDED, closed core trades (broker-executed; never-filled CANCELLED/VOID
    rows excluded -- the KPI dilution guard spec G6.5 called out)."""
    from dashboard.core import paper
    from dashboard.core.sleeve import SLEEVE_METHOD
    from dashboard.execution import broker
    ex = broker.executed_ids()
    out = []
    for t in paper.all_trades():
        if t["id"] not in ex or t.get("method") == SLEEVE_METHOD:
            continue
        if t["status"] in ("WIN", "LOSS", "EXPIRED", "BE") and t.get("realized_r") is not None                 and "(signal only" not in (t.get("exit_reason") or ""):
            out.append(float(t["realized_r"]))
    return out


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
    if not ref or ref.get("config") != ref_config_label():
        return None, None          # stale reference for a superseded config -> ignore
    return ref, ts


def compute_reference(pos_cap: float | None = None, portfolio_cap: float | None = None) -> dict:
    """Run the SAME backtest-reference computation live_vs_backtest.py uses, and cache it.
    Slow (minutes: full-history weekly downloads for the whole active universe) -- must
    only ever be called from an explicit user action, never a render path."""
    os.environ.setdefault("BROKER", "ib")
    os.environ.setdefault("UNIVERSE", "etf")
    from dashboard.core import store
    from dashboard.research.live_vs_backtest import _backtest_reference

    cfg = live_config()                                  # F5: default to the LIVE config
    pos_cap = cfg["pos_cap"] if pos_cap is None else pos_cap
    portfolio_cap = cfg["portfolio_cap"] if portfolio_cap is None else portfolio_cap
    label = ref_config_label({**cfg, "pos_cap": pos_cap, "portfolio_cap": portfolio_cap})
    log.warning("drift: computing backtest reference (%s) -- this takes minutes", label)
    stats = _backtest_reference(pos_cap, portfolio_cap)
    ref = {
        "config": label, "risk": cfg["risk"],
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
