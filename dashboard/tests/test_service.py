"""Unit tests for the PURE sanity-guard functions in web/service.py -- equity-history
self-heal and the account-summary confirm-then-accept guard. Run:
  uv run python -m dashboard.tests.test_service
"""
from __future__ import annotations

import datetime as dt

from dashboard.web.service import (heal_series, is_nl_implausible, pending_confirms,
                                   is_equity_jump_implausible, reconcile_due)

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


def test_is_equity_jump_implausible():
    print("is_equity_jump_implausible:")
    check("no baseline yet -> always plausible", is_equity_jump_implausible(10_000.0, 0.0, 0.0), False)
    check("drop to zero -> implausible", is_equity_jump_implausible(0.0, 10_040.0, 0.0), True)
    # FLAT (no open positions): tight noise-band check, regardless of jump size in ratio terms
    check("flat, tiny noise -> plausible", is_equity_jump_implausible(10_090.0, 10_040.0, 0.0), False)
    check("flat, exactly at noise-band boundary -> plausible",
          is_equity_jump_implausible(10_140.0, 10_040.0, 0.0), False)  # noise_band = max(100, 50.2) = 100
    check("flat, just past noise-band boundary -> implausible",
          is_equity_jump_implausible(10_140.01, 10_040.0, 0.0), True)
    # THE KEY REGRESSION CHECK: a ~30% deposit-sized jump used to be MISSED (within the old
    # 0.5x-2.0x band) -- now correctly flagged while flat, since nothing legitimate explains it.
    check("flat, ~30% deposit-sized jump -> now correctly implausible (was missed before)",
          is_equity_jump_implausible(13_000.0, 10_040.0, 0.0), True)
    # a large confirmed jump (the actual live incident) still correctly flagged too
    check("flat, ~10x jump -> implausible", is_equity_jump_implausible(99_994.0, 10_040.0, 0.0), True)
    # WITH open positions: falls back to the wider ratio band (mark-to-market P&L is legitimate)
    check("open positions, 30% move -> plausible (within wide band)",
          is_equity_jump_implausible(13_000.0, 10_040.0, 5_000.0), False)
    check("open positions, >2x move -> implausible",
          is_equity_jump_implausible(21_000.0, 10_040.0, 5_000.0), True)
    # gpv unknown (None, e.g. a connection hiccup before GrossPositionValue populates) -> must
    # NOT be treated as "flat" (we don't actually know) -- falls back to the wide band
    check("gpv unknown -> falls back to wide band, 30% move plausible",
          is_equity_jump_implausible(13_000.0, 10_040.0, None), False)


# ADDED 2026-07-21: broker reconciliation (STATE["reconcile"], the System Health banner's
# "reconcile:" line) used to run ONLY on a fresh IB connection -- once a real mismatch (CWB's
# ghost entry) was found, STATE["reconcile"] never got refreshed again on a stable, never-
# reconnecting connection, so the banner showed "mismatch found" indefinitely, surviving any
# number of browser refreshes, even though the underlying issue was long since fixed.
def test_reconcile_due():
    print("reconcile_due():")
    now = dt.datetime(2026, 7, 21, 12, 0, 0)
    check("never run before (None) -> due immediately", reconcile_due(None, now), True)
    check("just ran (0s ago) -> not due yet",
          reconcile_due(now, now, periodic_sec=600), False)
    check("ran 599s ago -> not due yet (just under the period)",
          reconcile_due(now - dt.timedelta(seconds=599), now, periodic_sec=600), False)
    check("ran exactly 600s ago -> due (boundary)",
          reconcile_due(now - dt.timedelta(seconds=600), now, periodic_sec=600), True)
    check("ran 20min ago -> due", reconcile_due(now - dt.timedelta(minutes=20), now,
                                                periodic_sec=600), True)
    check("default periodic_sec matches RECONCILE_PERIODIC_SEC (600s)",
          reconcile_due(now - dt.timedelta(seconds=601), now), True)


if __name__ == "__main__":
    for t in (test_heal_series_bracketed_zero_spike, test_heal_series_real_sustained_jump_kept,
              test_heal_series_unresolved_anomaly_left_alone,
              test_heal_series_normal_fluctuations_untouched,
              test_heal_series_empty_and_singleton, test_is_nl_implausible,
              test_pending_confirms, test_is_equity_jump_implausible, test_reconcile_due):
        t()
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {_fails}")
        raise SystemExit(1)
    print("all tests passed.")
