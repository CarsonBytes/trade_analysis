"""Hermetic test environment -- the 3.3 fix. Import this BEFORE any app module.

Prevents two classes of real-world damage from test runs (both observed for
real on 2026-09-30, the day this landed):

1. **Prod ledger writes.** Tests mock `invoke_with_key_fallback` (so no LLM is
   actually called) but never mocked `usage_log.log_usage()`, so a successful
   mocked `run_board_scan()` still POSTed a real 0-token row into the shared
   Supabase `llm_calls` table -- the same table event-radar and study write to.
   Reproduced with a stack trace: `test_board_scan.py::
   test_truncated_response_retries_with_a_smaller_batch_instead_of_dying`
   reached `httpx.post`. Setting `LLM_LEDGER_DISABLED=1` makes
   `analyst.usage_log.log_usage()` an early-return no-op (the guard is read at
   call time, so import order is irrelevant).

2. **Host dashboard.db writes.** `store._dbpath()` resolves
   `os.environ["DASH_DB_NAME"]` at call time against `dashboard/`, so tests
   that don't call their own `_isolated_db()` were mutating the live
   27 MB paper journal (SHA-256 of dashboard/dashboard.db changed during a
   single pytest run). Redirecting DASH_DB_NAME to a throwaway path keeps the
   host file byte-identical.

Applied automatically by dashboard/tests/conftest.py for `pytest` runs. For
script-style runs (`uv run python -m dashboard.tests.test_X`) each module must
import this first:

    from dashboard.tests import hermetic  # noqa: F401
"""
from __future__ import annotations

import atexit
import os
import tempfile

_TEMP_DBS: list[str] = []


def apply() -> None:
    """Idempotent: sets the two guards, leaving any pre-set value alone
    (docker-compose sets DASH_DB_NAME for the real deployment, and
    test_app_mode.py asserts explicit values survive)."""
    os.environ.setdefault("LLM_LEDGER_DISABLED", "1")
    if not os.environ.get("DASH_DB_NAME"):
        fd, path = tempfile.mkstemp(prefix="quant_test_dash_", suffix=".db")
        os.close(fd)
        os.remove(path)  # store creates it on first connect; never pre-seed it
        os.environ["DASH_DB_NAME"] = path
        _TEMP_DBS.append(path)


def _cleanup() -> None:
    for path in _TEMP_DBS:
        try:
            os.remove(path)
        except OSError:
            pass


atexit.register(_cleanup)

apply()  # importing the module IS applying it
