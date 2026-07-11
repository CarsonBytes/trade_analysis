"""Unit tests for the PURE drawdown-monitoring functions in core/paper.py, extracted
2026-07-11 from app.py so ib_exec's DD-halt gate can share the same logic as the
dashboard's own "Drawdown from peak" stat. Run:
  uv run python -m dashboard.tests.test_paper
"""
from __future__ import annotations

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


if __name__ == "__main__":
    test_deposit_adjusted_series()
    test_current_drawdown_pct()
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {_fails}")
        raise SystemExit(1)
    print("all tests passed.")
