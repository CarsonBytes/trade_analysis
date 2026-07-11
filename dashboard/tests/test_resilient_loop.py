"""Regression test for the exact property that matters: a resilient loop must NEVER die
from a single call's failure. Extracted 2026-07-12 after finding app.py's entire trading/
monitoring tick loop had exactly this bug -- an unhandled exception (or, in an earlier
version, a hung await) would silently kill the whole background task forever, with zero log
output and the web server still responding HTTP 200 throughout. app.py itself can't be
imported in a test (its module-level `ui.run()` call blocks), so this tests the extracted
pure function it now depends on instead.

Run:  uv run python -m dashboard.tests.test_resilient_loop
"""
from __future__ import annotations

import asyncio

from dashboard.core.resilient_loop import run_forever

_fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        _fails.append(name)


async def _run_n_iterations(fn, n, interval_sec=0.0):
    """Drive run_forever() for exactly n iterations then cancel it -- run_forever() itself
    never returns (by design), so the test has to bound it externally."""
    errors = []
    task = asyncio.create_task(run_forever(fn, interval_sec, on_error=errors.append))
    for _ in range(n):
        await asyncio.sleep(0.01)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return errors


def test_survives_every_call_failing():
    print("run_forever: every single call raises -- loop must keep calling anyway:")
    calls = {"n": 0}

    async def _always_fails():
        calls["n"] += 1
        raise ValueError(f"boom #{calls['n']}")

    errors = asyncio.run(_run_n_iterations(_always_fails, n=5))
    check("kept calling despite every call raising (n>=3)", calls["n"] >= 3, True)
    check("every failure was reported via on_error, not swallowed silently",
          len(errors) >= 3, True)
    check("on_error received the actual exception object",
          all(isinstance(e, ValueError) for e in errors), True)


def test_survives_intermittent_failure():
    print("\nrun_forever: only SOME calls raise -- must recover and keep succeeding after:")
    calls = {"n": 0, "succeeded_after_failure": False}

    async def _fails_once_then_ok():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first call fails")
        calls["succeeded_after_failure"] = True

    asyncio.run(_run_n_iterations(_fails_once_then_ok, n=5))
    check("recovered and kept running normally after the one failure",
          calls["succeeded_after_failure"], True)
    check("called more than just the one failing time", calls["n"] >= 3, True)


def test_no_on_error_handler_still_safe():
    print("\nrun_forever: on_error=None (no handler given) -- must not itself crash the loop:")
    calls = {"n": 0}

    async def _always_fails():
        calls["n"] += 1
        raise KeyError("no handler for this")

    asyncio.run(_run_n_iterations(_always_fails, n=5))
    check("loop survives even with no on_error callback at all", calls["n"] >= 3, True)


if __name__ == "__main__":
    test_survives_every_call_failing()
    test_survives_intermittent_failure()
    test_no_on_error_handler_still_safe()
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {_fails}")
        raise SystemExit(1)
    print("all tests passed.")
