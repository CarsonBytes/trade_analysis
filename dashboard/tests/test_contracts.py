"""Unit tests for the PURE parts of contracts.py -- sizing + roll math.

No IB needed (the broker-touching front_contract/continuous_history are not
exercised here). Run:  uv run python -m dashboard.tests.test_contracts
"""
from __future__ import annotations

import datetime as dt

from dashboard.data.contracts import (SPECS, FutureSpec, size_contracts, risk_per_contract,
                        choose_contract, needs_roll, _business_days_between, front_month,
                        cost_points)

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


def test_front_month():
    print("front_month (pure roll calendar):")
    es = SPECS["ES"]   # quarterly HMUZ, roll_offset 5
    # early June 2026: Jun(M) expiry ~15th is < 5 bdays off -> roll to Sep(U)
    y, code, last = front_month(es, dt.date(2026, 6, 12))
    check("ES Jun->Sep roll", (code, y), ("U", 2026))
    # early March: hold Mar(H) still (15th far enough ahead)
    y, code, _ = front_month(es, dt.date(2026, 3, 1))
    check("ES holds Mar", (code, y), ("H", 2026))
    # year wrap: mid-Dec -> next March
    y, code, _ = front_month(es, dt.date(2026, 12, 20))
    check("ES wraps to next H", (code, y), ("H", 2027))
    # monthly contract (CL) on Jan 2: the PURE 15th-rule still holds Jan(F)
    # (approx). NOTE: real CL expires the month BEFORE delivery (~Dec 20), so IB's
    # front_future would already be on Feb(G); the broker value overrides this.
    y, code, _ = front_month(SPECS["CL"], dt.date(2026, 1, 2))
    check("CL approx holds F (broker overrides)", code, "F")


def test_cost_points():
    print("cost_points (futures transaction cost in price points):")
    es = SPECS["ES"]   # mult 50, tick 0.25; $2.50 RT
    # 2.50/50 + 2*1*0.25 = 0.05 + 0.5 = 0.55 points
    approx("ES cost pts", cost_points(es), 0.55)
    gc = SPECS["GC"]   # mult 100, tick 0.10
    # 2.50/100 + 2*0.10 = 0.025 + 0.2 = 0.225
    approx("GC cost pts", cost_points(gc), 0.225)
    # override commission + slippage
    approx("ES 0 cost", cost_points(es, commission_rt=0.0, slippage_ticks=0.0), 0.0)


if __name__ == "__main__":
    for t in (test_risk_per_contract, test_size_contracts, test_choose_contract,
              test_roll, test_front_month, test_cost_points, test_spec_integrity):
        t()
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {_fails}")
        raise SystemExit(1)
    print("all tests passed.")
