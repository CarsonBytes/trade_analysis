"""Unit tests for the PURE sizing logic in execution/ib_exec.py -- the portfolio-level
gross-exposure cap. Run:  uv run python -m dashboard.tests.test_ib_exec
"""
from __future__ import annotations

from dashboard.execution.ib_exec import cap_qty_to_portfolio_room

_fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        _fails.append(name)


def test_cap_qty_to_portfolio_room():
    print("cap_qty_to_portfolio_room:")
    # equity=$100k, portfolio_cap=1.0 (100%), price=$100/share throughout
    check("plenty of room -> qty unchanged",
          cap_qty_to_portfolio_room(50, 100.0, 100_000.0, 1.0, 0.0), 50)
    check("partial room -> qty scaled down to fit exactly",
          cap_qty_to_portfolio_room(150, 100.0, 100_000.0, 1.0, 90_000.0), 100)
    check("exactly at boundary -> qty unaffected (fits exactly)",
          cap_qty_to_portfolio_room(100, 100.0, 100_000.0, 1.0, 90_000.0), 100)
    check("already deployed AT the cap -> qty forced to 0",
          cap_qty_to_portfolio_room(50, 100.0, 100_000.0, 1.0, 100_000.0), 0)
    check("already deployed OVER the cap -> qty forced to 0, not negative",
          cap_qty_to_portfolio_room(50, 100.0, 100_000.0, 1.0, 110_000.0), 0)
    check("portfolio_cap=0 -> disabled, qty unchanged regardless of deployed",
          cap_qty_to_portfolio_room(500, 100.0, 100_000.0, 0.0, 999_000.0), 500)
    check("negative portfolio_cap -> also disabled (defensive)",
          cap_qty_to_portfolio_room(500, 100.0, 100_000.0, -1.0, 0.0), 500)
    check("price<=0 -> disabled (guard), qty unchanged",
          cap_qty_to_portfolio_room(500, 0.0, 100_000.0, 1.0, 0.0), 500)
    check("tighter portfolio_cap (0.5) with no prior deployment",
          cap_qty_to_portfolio_room(1000, 100.0, 100_000.0, 0.5, 0.0), 500)
    check("qty=0 in -> qty=0 out (never scales UP)",
          cap_qty_to_portfolio_room(0, 100.0, 100_000.0, 1.0, 0.0), 0)


if __name__ == "__main__":
    test_cap_qty_to_portfolio_room()
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {_fails}")
        raise SystemExit(1)
    print("all tests passed.")
