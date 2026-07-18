"""Unit tests for the PURE drawdown-monitoring functions in core/paper.py, extracted
2026-07-11 from app.py so ib_exec's DD-halt gate can share the same logic as the
dashboard's own "Drawdown from peak" stat. Run:
  uv run python -m dashboard.tests.test_paper
"""
from __future__ import annotations

import os
import tempfile
from unittest import mock

from dashboard.core.paper import deposit_adjusted_series, current_drawdown_pct

_fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        _fails.append(name)


def approx(name, got, want, tol=1e-6):
    ok = abs(got - want) <= tol
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want ~{want!r}")
    if not ok:
        _fails.append(name)


def test_deposit_adjusted_series():
    print("deposit_adjusted_series:")
    hist = [[100, 10000.0, "HKD"], [200, 10100.0, "HKD"], [300, 20100.0, "HKD"]]
    check("no flows -> raw values unchanged",
          deposit_adjusted_series(hist, None), [10000.0, 10100.0, 20100.0])
    check("empty flows list -> raw values unchanged",
          deposit_adjusted_series(hist, []), [10000.0, 10100.0, 20100.0])
    flows = [[250, 10000.0, "HKD"]]   # a deposit between t=200 and t=300
    check("deposit nets out from points at/after it",
          deposit_adjusted_series(hist, flows), [10000.0, 10100.0, 10100.0])
    flows2 = [[100, 10000.0, "HKD"]]  # a deposit exactly AT the first point
    check("flow exactly at a point's timestamp is included (<=, not <)",
          deposit_adjusted_series(hist, flows2), [0.0, 100.0, 10100.0])


def test_current_drawdown_pct():
    print("current_drawdown_pct:")
    check("empty history -> 0.0", current_drawdown_pct([], None), 0.0)
    check("single point -> 0.0 (not enough history)",
          current_drawdown_pct([[100, 10000.0, "HKD"]], None), 0.0)
    # monotonic rise -> always at the peak -> 0% drawdown
    rising = [[100, 100.0, "HKD"], [200, 110.0, "HKD"], [300, 120.0, "HKD"]]
    approx("monotonic rise -> 0% (at peak)", current_drawdown_pct(rising, None), 0.0)
    # peak then a drop -> negative drawdown from that peak
    dropped = [[100, 100.0, "HKD"], [200, 120.0, "HKD"], [300, 108.0, "HKD"]]
    approx("peak 120 -> now 108 = -10%", current_drawdown_pct(dropped, None), -10.0)
    # a big deposit must NOT be mistaken for a new peak that hides a real drawdown
    dep_hist = [[100, 10000.0, "HKD"], [200, 9000.0, "HKD"], [300, 19000.0, "HKD"]]
    dep_flows = [[250, 10000.0, "HKD"]]
    # deposit-adjusted: [10000, 9000, 9000] -- true peak 10000, now 9000 -> -10%
    approx("deposit doesn't mask a real -10% drawdown",
          current_drawdown_pct(dep_hist, dep_flows), -10.0)
    # exactly at the -13% halt threshold a real caller would check
    at_threshold = [[100, 100.0, "HKD"], [200, 87.0, "HKD"]]
    approx("exactly -13% from peak", current_drawdown_pct(at_threshold, None), -13.0)
    # MATERIALITY FLOOR (2026-07-11 bug): a tiny pre-funding leftover balance (40 HKD) must
    # NOT be treated as an eternal "peak" once real deposits land -- a few-dollar wobble on
    # a near-zero deposit-adjusted P&L shouldn't compute as a huge %. Reproduces the exact
    # live-account shape: near-zero start, a deposit, then a tiny dip below the pre-funding
    # residual.
    fresh_acct = [[100, 40.0, "HKD"], [200, 40.0, "HKD"], [300, 100040.0, "HKD"],
                 [400, 100003.81, "HKD"]]     # deposit lands at t=250, then a tiny real dip
    fresh_flows = [[250, 100000.0, "HKD"]]
    # deposit-adjusted: [40, 40, 40, 3.81] -- naive peak=40 vs now=3.81 would be -90%+,
    # but 40 is far below 1% of current equity (~1000) -> not material, report 0.0
    approx("tiny pre-funding residual doesn't register as a real drawdown",
          current_drawdown_pct(fresh_acct, fresh_flows), 0.0)
    # once real trading P&L clears the materiality floor, a genuine drawdown DOES register
    grown_acct = [[100, 40.0, "HKD"], [200, 100040.0, "HKD"], [300, 102000.0, "HKD"],
                 [400, 101000.0, "HKD"]]      # deposit at t=150, then +2000 P&L, then -1000
    grown_flows = [[150, 100000.0, "HKD"]]
    # deposit-adjusted: [40, 40, 2000, 1000] -- peak 2000, now 1000 -> -50%, and 2000 is
    # well above 1% of current equity (~1010) -> material, must still be caught
    approx("a real drawdown above the materiality floor still registers",
          current_drawdown_pct(grown_acct, grown_flows), -50.0)


def _isolated_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    old = os.environ.get("DASH_DB_NAME")
    os.environ["DASH_DB_NAME"] = path
    return old, path


def _restore_db(old, path):
    if old is None:
        os.environ.pop("DASH_DB_NAME", None)
    else:
        os.environ["DASH_DB_NAME"] = old
    try:
        os.remove(path)
    except OSError:
        pass


# ADDED 2026-07-17: resolve_open()'s exit_reason used to read identically ("stop-loss
# hit") whether a trade was a real closed broker position or a signal that was NEVER
# funded (the portfolio cap held it back the whole time, so no order was ever sent) --
# confirmed live: a CWB "loss" the user had to ask about because nothing in the record
# said it was never real money. This tests that resolve_open() tags the unfunded case.
def test_resolve_open_tags_unfunded_trades():
    print("resolve_open(): a trade NOT in executed_ids gets an explicit "
          "'never funded' qualifier on its exit_reason:")
    old, path = _isolated_db()
    try:
        from dashboard.core import paper
        t = paper.Trade(
            ts="2026-07-13T10:00:00+00:00", instrument="CWB", direction="long",
            method="ATR rr3.0", entry=105.24, sl=101.99, tp=114.97, rr=3.0,
            size_units=30.0, horizon_end="2026-08-17T10:00:00+00:00", confidence=0.6,
            rationale="test",
        )
        paper._insert(t)
        trade_id = paper.open_trades()[0]["id"]
        with mock.patch.object(paper, "_outcome_for", return_value=("LOSS", 101.99, "2026-07-16T00:00:00Z")):
            n = paper.resolve_open(lambda inst: None, executed_ids=set())  # empty set: NOT funded
        check("one trade resolved", n, 1)
        resolved = [t for t in paper.all_trades() if t["id"] == trade_id][0]
        check("status is LOSS", resolved["status"], "LOSS")
        check("exit_reason flags it as never funded",
              "never funded" in resolved["exit_reason"], True)
    finally:
        _restore_db(old, path)


def test_resolve_open_leaves_funded_trades_unqualified():
    print("\nresolve_open(): a trade IN executed_ids (a real broker position) keeps "
          "the plain exit_reason, no false 'never funded' qualifier:")
    old, path = _isolated_db()
    try:
        from dashboard.core import paper
        t = paper.Trade(
            ts="2026-07-13T10:00:00+00:00", instrument="CPER", direction="long",
            method="ATR rr3.0", entry=38.38, sl=36.68, tp=41.91, rr=3.0,
            size_units=30.0, horizon_end="2026-08-17T10:00:00+00:00", confidence=0.6,
            rationale="test",
        )
        paper._insert(t)
        trade_id = paper.open_trades()[0]["id"]
        with mock.patch.object(paper, "_outcome_for", return_value=("WIN", 41.91, "2026-07-16T00:00:00Z")):
            n = paper.resolve_open(lambda inst: None, executed_ids={trade_id})  # WAS funded
        check("one trade resolved", n, 1)
        resolved = [t for t in paper.all_trades() if t["id"] == trade_id][0]
        check("exit_reason has no 'never funded' qualifier",
              resolved["exit_reason"], "take-profit hit")
    finally:
        _restore_db(old, path)


def test_resolve_open_skips_check_when_executed_ids_omitted():
    print("\nresolve_open(): executed_ids=None (the default) -> back-compat, no "
          "qualifier attempted at all (e.g. a caller that can't reach the broker):")
    old, path = _isolated_db()
    try:
        from dashboard.core import paper
        t = paper.Trade(
            ts="2026-07-13T10:00:00+00:00", instrument="VNQ", direction="long",
            method="ATR rr3.0", entry=97.47, sl=94.67, tp=107.42, rr=3.0,
            size_units=30.0, horizon_end="2026-08-17T10:00:00+00:00", confidence=0.6,
            rationale="test",
        )
        paper._insert(t)
        trade_id = paper.open_trades()[0]["id"]
        with mock.patch.object(paper, "_outcome_for", return_value=("LOSS", 94.67, "2026-07-16T00:00:00Z")):
            paper.resolve_open(lambda inst: None)   # executed_ids omitted entirely
        resolved = [t for t in paper.all_trades() if t["id"] == trade_id][0]
        check("plain exit_reason, no qualifier attempted", resolved["exit_reason"], "stop-loss hit")
    finally:
        _restore_db(old, path)


# ADDED 2026-07-18: the backtest-validated re-entry gate (REENTRY_BUFFER_R, HANDOFF.md) --
# after a LOSS, block a same-direction re-entry until price closes beyond that trade's own
# entry by 1.0R (its own entry-to-stop risk). Real trigger: ASHR stopped out 3x in 8 days,
# each re-entry within a day or two at nearly the same price -- COOLDOWN_MIN=60 (minutes) never
# caught this. Best of 19 gate variants tested across 3 backtest rounds in research/backtest.py.
def _closed_trade(instrument, direction, entry, sl, status, exit_ts):
    from dashboard.core import paper
    t = paper.Trade(
        ts="2026-07-01T10:00:00+00:00", instrument=instrument, direction=direction,
        method="ATR rr3.0", entry=entry, sl=sl, tp=entry + 3 * abs(entry - sl), rr=3.0,
        size_units=10.0, horizon_end="2026-08-01T10:00:00+00:00", confidence=0.6,
        rationale="test",
    )
    paper._insert(t)
    trade_id = paper.open_trades()[0]["id"]
    paper._update_resolution(trade_id, status, exit_ts, entry, -1.0 if status == "LOSS" else 1.0)
    return trade_id


def test_reentry_blocked_gates_until_buffer_reclaimed():
    print("_reentry_blocked(): after a LOSS, same-direction re-entry blocked until price "
          "closes beyond the losing trade's entry by REENTRY_BUFFER_R x its own risk:")
    old, path = _isolated_db()
    try:
        from dashboard.core import paper
        _closed_trade("ASHR", "long", entry=35.0, sl=34.0, status="LOSS",
                      exit_ts="2026-07-16T00:00:00+00:00")   # risk=1.0 -> target=36.0 (1.0R)
        check("price still below target -> blocked",
              paper._reentry_blocked("ASHR", "long", 35.5) is not None, True)
        check("price exactly at target -> still blocked (needs to CLOSE beyond, not just touch)",
              paper._reentry_blocked("ASHR", "long", 36.0) is not None, True)
        check("price beyond target -> clear",
              paper._reentry_blocked("ASHR", "long", 36.5), None)
    finally:
        _restore_db(old, path)


def test_reentry_blocked_ignores_opposite_direction():
    print("\n_reentry_blocked(): a LOSS on the long side never blocks a SHORT re-entry:")
    old, path = _isolated_db()
    try:
        from dashboard.core import paper
        _closed_trade("ASHR", "long", entry=35.0, sl=34.0, status="LOSS",
                      exit_ts="2026-07-16T00:00:00+00:00")
        check("opposite direction unaffected, any price",
              paper._reentry_blocked("ASHR", "short", 35.2), None)
    finally:
        _restore_db(old, path)


def test_reentry_blocked_win_does_not_gate():
    print("\n_reentry_blocked(): a WIN (or EXPIRED) resets the gate entirely -- matches "
          "research/backtest.py's _signals() state machine (only the LATEST loss matters):")
    old, path = _isolated_db()
    try:
        from dashboard.core import paper
        _closed_trade("ASHR", "long", entry=35.0, sl=34.0, status="WIN",
                      exit_ts="2026-07-16T00:00:00+00:00")
        check("a WIN never blocks re-entry, any price",
              paper._reentry_blocked("ASHR", "long", 35.1), None)
    finally:
        _restore_db(old, path)


def test_reentry_blocked_no_prior_trade():
    print("\n_reentry_blocked(): an instrument with no closed trades at all -> never blocked:")
    old, path = _isolated_db()
    try:
        from dashboard.core import paper
        check("no history -> clear", paper._reentry_blocked("SPY", "long", 500.0), None)
    finally:
        _restore_db(old, path)


def test_reentry_blocked_uses_most_recent_trade_only():
    print("\n_reentry_blocked(): only the MOST RECENTLY CLOSED trade matters -- an older LOSS "
          "followed by a newer WIN clears the gate, not the other way around:")
    old, path = _isolated_db()
    try:
        from dashboard.core import paper
        _closed_trade("ASHR", "long", entry=35.0, sl=34.0, status="LOSS",
                      exit_ts="2026-07-09T00:00:00+00:00")   # older
        _closed_trade("ASHR", "long", entry=35.5, sl=34.5, status="WIN",
                      exit_ts="2026-07-16T00:00:00+00:00")   # newer -- resets the gate
        check("newer WIN supersedes the older LOSS -> clear",
              paper._reentry_blocked("ASHR", "long", 35.1), None)
    finally:
        _restore_db(old, path)


if __name__ == "__main__":
    test_deposit_adjusted_series()
    test_current_drawdown_pct()
    test_resolve_open_tags_unfunded_trades()
    test_resolve_open_leaves_funded_trades_unqualified()
    test_resolve_open_skips_check_when_executed_ids_omitted()
    test_reentry_blocked_gates_until_buffer_reclaimed()
    test_reentry_blocked_ignores_opposite_direction()
    test_reentry_blocked_win_does_not_gate()
    test_reentry_blocked_no_prior_trade()
    test_reentry_blocked_uses_most_recent_trade_only()
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {_fails}")
        raise SystemExit(1)
    print("all tests passed.")
