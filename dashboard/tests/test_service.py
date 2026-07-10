"""Unit tests for the PURE sanity-guard functions in web/service.py -- equity-history
self-heal and the account-summary confirm-then-accept guard. Run:
  uv run python -m dashboard.tests.test_service
"""
from __future__ import annotations

from dashboard.web.service import heal_series, is_nl_implausible, pending_confirms

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


def test_heal_series_bracketed_zero_spike():
    print("heal_series: bracketed zero-spike (the 2026-07-10 incident shape):")
    hist = [["t1", 100.0, "HKD"], ["t2", 0.0, "HKD"], ["t3", 0.0, "HKD"],
            ["t4", 101.0, "HKD"]]
    cleaned, removed = heal_series(hist)
    check("cleaned drops the spike", cleaned, [["t1", 100.0, "HKD"], ["t4", 101.0, "HKD"]])
    check("removed captures the spike", removed, [["t2", 0.0, "HKD"], ["t3", 0.0, "HKD"]])


def test_heal_series_real_sustained_jump_kept():
    print("heal_series: real sustained jump (e.g. a genuine deposit) is kept untouched:")
    hist = [["t1", 100.0, "HKD"], ["t2", 500.0, "HKD"], ["t3", 505.0, "HKD"],
            ["t4", 510.0, "HKD"]]
    cleaned, removed = heal_series(hist)
    check("nothing removed", removed, [])
    check("all points kept", cleaned, hist)


def test_heal_series_unresolved_anomaly_left_alone():
    print("heal_series: an anomaly still at the end of the series (unconfirmed) is left alone:")
    hist = [["t1", 100.0, "HKD"], ["t2", 101.0, "HKD"], ["t3", 0.0, "HKD"]]
    cleaned, removed = heal_series(hist)
    check("nothing removed (not yet bracketed)", removed, [])
    check("cleaned == original", cleaned, hist)


def test_heal_series_normal_fluctuations_untouched():
    print("heal_series: normal small fluctuations never trigger the guard:")
    hist = [["t1", 100.0, "HKD"], ["t2", 98.0, "HKD"], ["t3", 103.0, "HKD"],
            ["t4", 99.5, "HKD"]]
    cleaned, removed = heal_series(hist)
    check("nothing removed", removed, [])
    check("cleaned == original", cleaned, hist)


def test_heal_series_empty_and_singleton():
    print("heal_series: edge cases (empty / single point):")
    check("empty in -> empty out", heal_series([]), ([], []))
    one = [["t1", 100.0, "HKD"]]
    check("single point untouched", heal_series(one), (one, []))


def test_is_nl_implausible():
    print("is_nl_implausible:")
    check("no baseline yet -> always accepted", is_nl_implausible(0.0, None), False)
    check("baseline<=0 -> always accepted", is_nl_implausible(50.0, 0.0), False)
    check("drop to zero vs positive baseline -> implausible", is_nl_implausible(0.0, 10_040.0), True)
    check("negative reading -> implausible", is_nl_implausible(-500.0, 10_040.0), True)
    check("within 0.5x-2x band -> plausible", is_nl_implausible(15_000.0, 10_040.0), False)
    check("just above 2x -> implausible", is_nl_implausible(20_100.0, 10_040.0), True)
    check("just below 0.5x -> implausible", is_nl_implausible(5_000.0, 10_040.0), True)
    check("exactly at 2x boundary -> plausible", is_nl_implausible(20_080.0, 10_040.0), False)
    check("exactly at 0.5x boundary -> plausible", is_nl_implausible(5_020.0, 10_040.0), False)
    check("unchanged -> plausible", is_nl_implausible(10_040.0, 10_040.0), False)


def test_pending_confirms():
    print("pending_confirms:")
    check("no pending yet -> never confirms", pending_confirms(None, 0.0), False)
    check("pending==0.0 CAN confirm (not falsy)", pending_confirms(0.0, 0.0), True)
    check("matching value within tol -> confirms", pending_confirms(100.0, 100.005), True)
    check("outside tol -> does not confirm", pending_confirms(100.0, 101.0), False)
    check("different anomaly value -> does not confirm", pending_confirms(0.0, 50.0), False)


if __name__ == "__main__":
    for t in (test_heal_series_bracketed_zero_spike, test_heal_series_real_sustained_jump_kept,
              test_heal_series_unresolved_anomaly_left_alone,
              test_heal_series_normal_fluctuations_untouched,
              test_heal_series_empty_and_singleton, test_is_nl_implausible,
              test_pending_confirms):
        t()
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {_fails}")
        raise SystemExit(1)
    print("all tests passed.")
