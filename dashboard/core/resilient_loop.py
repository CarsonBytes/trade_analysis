"""A tiny, pure, independently-testable utility extracted from app.py's `_tick_loop()`
(2026-07-12) so the "this loop must never die from a single call's failure" property has an
actual regression test -- app.py itself can't be safely imported in a test (it calls
`ui.run()` at module level, which blocks), so the safety-critical PATTERN lives here instead
and app.py just calls it.
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable


async def run_forever(fn: Callable[[], Awaitable[None]], interval_sec: float,
                      on_error: Callable[[BaseException], None] | None = None) -> None:
    """Call `fn()` repeatedly forever, sleeping `interval_sec` between calls. ANY exception
    from a single call is caught and passed to `on_error` (if given); the loop itself must
    NEVER die from one call's failure -- that was exactly the bug this replaces (a hung/
    excepting tick silently killing the whole background task forever, with zero indication
    anything was wrong)."""
    while True:
        try:
            await fn()
        except Exception as e:                # noqa: BLE001 -- deliberately broad: see docstring
            if on_error is not None:
                on_error(e)
        await asyncio.sleep(interval_sec)
