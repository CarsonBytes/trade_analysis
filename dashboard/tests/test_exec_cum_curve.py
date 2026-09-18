"""Unit tests for retrospective.exec_cum_curve() -- the Track Record tab's
chronological cumulative-R curve (2026-09-18: replaced the per-row newest-first
"cumulative R" column, which read as miscalculated next to winning rows).

Pure function, no DB. Run:  pytest dashboard/tests/test_exec_cum_curve.py -q
"""
from __future__ import annotations

_fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        _fails.append(name)
    assert ok, f"{name}: got {got!r} want {want!r}"


def _t(i, r, exit_ts, status="LOSS"):
    return {"id": i, "instrument": "X", "status": status,
            "realized_r": r, "exit_ts": exit_ts, "ts": "2026-01-01T00:00:00"}


def test_wins_lift_losses_drop_chronologically():
    print("\nexec_cum_curve(): wins lift, losses drop, oldest-first:")
    from dashboard.web.retrospective import exec_cum_curve
    trades = [_t(3, 0.661, "2026-09-16 00:00:00+00:00", "EXPIRED"),
              _t(2, -1.003, "2026-08-31 00:00:00+00:00"),
              _t(1, 2.991, "2026-08-04 00:00:00+00:00", "WIN")]
    xs, curve = exec_cum_curve(trades, {1, 2, 3})
    check("x labels chronological MM-DD", xs, ["08-04", "08-31", "09-16"])
    check("win lifts first step", curve[0], 2.991)
    check("loss drops second step", curve[1], round(2.991 - 1.003, 3))
    check("final equals sum", curve[-1], round(2.991 - 1.003 + 0.661, 3))


def test_signal_only_excluded():
    print("\nexec_cum_curve(): non-executed ids never enter the curve:")
    from dashboard.web.retrospective import exec_cum_curve
    trades = [_t(2, -1.006, "2026-09-15 00:00:00+00:00"),
              _t(1, 2.991, "2026-08-04 00:00:00+00:00", "WIN")]
    xs, curve = exec_cum_curve(trades, {1})
    check("only the executed close counted", (xs, curve), (["08-04"], [2.991]))


def test_same_day_ties_keep_input_order():
    print("\nexec_cum_curve(): same-date exits keep input (id-DESC) order:")
    from dashboard.web.retrospective import exec_cum_curve
    trades = [_t(9, -1.003, "2026-08-31 00:00:00+00:00"),
              _t(8, -1.012, "2026-08-31 00:00:00+00:00")]
    xs, curve = exec_cum_curve(trades, {8, 9})
    check("tie order stable (newer id first)", curve, [round(-1.003, 3), round(-1.003 - 1.012, 3)])


def test_empty_when_nothing_executed():
    print("\nexec_cum_curve(): empty in, empty out (no chart rendered):")
    from dashboard.web.retrospective import exec_cum_curve
    check("no executed -> empty", exec_cum_curve([_t(1, -1.0, "2026-09-15 00:00:00+00:00")], set()),
          ([], []))


if __name__ == "__main__":
    for _name, _fn in list(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
            except AssertionError:
                pass
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {_fails}")
        raise SystemExit(1)
    print("all tests passed.")
