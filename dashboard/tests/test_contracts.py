"""Unit tests for the PURE parts of contracts.py -- sizing + roll math.

No IB needed (the broker-touching front_contract/continuous_history are not
exercised here). Run:  uv run python -m dashboard.test_contracts
"""
from __future__ import annotations

import datetime as dt

from dashboard.data.contracts import (SPECS, FutureSpec, size_contracts, risk_per_contract,
                        choose_contract, needs_roll, _business_days_between)

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


def test_risk_per_contract():
    print("risk_per_contract:")
    es = SPECS["ES"]   # $50/point
    # 40-point stop -> $2000 risk for one ES contract
    approx("ES 40pt", risk_per_contract(es, 40.0), 2000.0)
    mes = SPECS["MES"]  # $5/point
    approx("MES 40pt", risk_per_contract(mes, 40.0), 200.0)
    gc = SPECS["GC"]   # $100/point
    approx("GC 15.0 stop", risk_per_contract(gc, 15.0), 1500.0)


def test_size_contracts():
    print("size_contracts:")
    es = SPECS["ES"]            # $50/pt
    # equity 100k, 0.5% = $500 budget; 20pt stop -> $1000/contract -> 0 ES
    check("ES too big -> 0", size_contracts(es, 100_000, 20.0, 0.005), 0)
    mes = SPECS["MES"]         # $5/pt; 20pt -> $100/contract -> 5 micros
    check("MES 5 micros", size_contracts(mes, 100_000, 20.0, 0.005), 5)
    # never round up: $500 budget, $300/contract -> 1, not 2
    gc = FutureSpec("X", "X", "E", "USD", 100.0, 0.1, "metal")
    check("floor not round", size_contracts(gc, 100_000, 3.0, 0.005), 1)  # $300/contract, budget $500
    # guards
    check("zero stop -> 0", size_contracts(es, 100_000, 0.0, 0.005), 0)
    check("neg equity -> 0", size_contracts(es, -1, 20.0, 0.005), 0)


def test_choose_contract():
    print("choose_contract:")
    es = SPECS["ES"]
    # big contract sizes to 0 -> should fall back to MES micro
    spec, n = choose_contract(es, 100_000, 20.0, 0.005)
    check("falls back to micro", spec.key, "MES")
    check("micro size 5", n, 5)
    # when full-size fits, keep it
    spec2, n2 = choose_contract(es, 100_000, 4.0, 0.005)  # $200/contract -> 2 ES
    check("keeps full-size", spec2.key, "ES")
    check("full size 2", n2, 2)
    # too big even as micro -> skip (0)
    spec3, n3 = choose_contract(es, 1_000, 100.0, 0.005)  # $5 budget
    check("skip too big", n3, 0)


def test_roll():
    print("roll math:")
    asof = dt.date(2026, 6, 1)  # a Monday
    # Fri 2026-06-05 is 4 business days after Mon 06-01
    check("bdays Mon->Fri", _business_days_between(asof, dt.date(2026, 6, 5)), 4)
    # weekend excluded: Mon 06-01 -> Mon 06-08 = 5 bdays
    check("bdays skips weekend", _business_days_between(asof, dt.date(2026, 6, 8)), 5)
    es = SPECS["ES"]  # roll_offset_days = 5
    # expiry 4 bdays away (<=5) -> must roll
    check("inside window rolls", needs_roll(dt.date(2026, 6, 5), es, asof), True)
    # expiry far out -> no roll
    check("far expiry no roll", needs_roll(dt.date(2026, 9, 18), es, asof), False)


def test_spec_integrity():
    print("spec table integrity:")
    check("keys unique", len(SPECS), len({s.key for s in SPECS.values()}))
    # every micro_of points at a real key
    bad = [s.key for s in SPECS.values() if s.micro_of and s.micro_of not in SPECS]
    check("micro_of valid", bad, [])
    # tick_value derived correctly
    approx("ES tick_value", SPECS["ES"].tick_value, 12.5)  # 0.25 * 50


if __name__ == "__main__":
    for t in (test_risk_per_contract, test_size_contracts, test_choose_contract,
              test_roll, test_spec_integrity):
        t()
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {_fails}")
        raise SystemExit(1)
    print("all tests passed.")
