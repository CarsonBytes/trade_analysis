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
    """Idempotent: sets the guards. DASH_DB_NAME is ALWAYS redirected to a throwaway
    temp DB, even when it is already set (see the 2026-11-25 note below)."""
    os.environ.setdefault("LLM_LEDGER_DISABLED", "1")
    # 3. Real phone pushes. FOUND 2026-11-21: a test that drives a RED notable event (the
    # long-only guard refusing a fake order) reached notify.send() with the host's real
    # TELEGRAM_BOT_TOKEN/CHAT_ID and pushed "[?] GUARD: refused SELL ..." to the operator's
    # phone. Blank (not unset) so a later load_dotenv() -- override=False -- can't refill them;
    # test_notify patches its own values in explicitly.
    for _k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "NTFY_URL"):
        os.environ[_k] = ""
    # FIXED 2026-11-25: this used to keep a pre-set DASH_DB_NAME ("docker-compose sets it for
    # the real deployment"). That made the guard a no-op exactly where it mattered: inside
    # quant-dashboard-docker, DASH_DB_NAME=/data/dashboard_docker.db IS the paper journal, so
    # `docker exec ... python -m dashboard.tests.test_ib_exec` (run 2026-10-01 to check the S2
    # tif fix) let test_bracket_parent_tif_is_day_children_gtc's fake EEM bracket (paper_id 27,
    # 149sh, perm_id 0, con_id 0, risk $500 = 1% of the test's $50k) INSERT a real ib_mirror
    # row with status OPEN. Reconcile then flagged EEM as a ghost for 8 weeks and, once the
    # G7.3 digest landed, paged Telegram daily. No test may ever need the real journal, so a
    # value that is not one of our own temp DBs is always replaced. (test_app_mode's explicit
    # values are set inside its own tests, after this runs, and restored after.)
    if os.environ.get("DASH_DB_NAME") not in _TEMP_DBS:
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
