"""Unit tests for web/board_scan.py's rate-limit backoff -- ADDED 2026-07-14.

Context: `run_board_scan()` had NO handling at all for the LLM provider's daily
rate limit (openai.RateLimitError / HTTP 429). Once the shared chatanywhere.tech
free-tier quota (200 req/day) was exhausted, every tick cycle (~15-30s cadence)
re-attempted the doomed call, each one a real, slow network round-trip that
failed anyway -- confirmed via 876 identical RateLimitError log entries and a
matching response-time regression. This tests the fix: catch the rate-limit
condition, cache a backoff deadline, and skip the network call entirely while
still in backoff.

WIDENED 2026-07-25: `run_board_scan()` now goes through
analyst.llm.invoke_with_key_fallback() (a primary-then-fallback-key retry, added after
the primary chatanywhere key was silently invalidated server-side for ~9h) and its
exception handling now also catches 401/403 (auth/permission errors), not just 429 --
see test_permission_denied_error_also_backs_off below.

FIXED 2026-07-25: these tests never mocked usage_log.shared_calls_ok()/store.can_call()
(the budget guard at the top of run_board_scan()), so they silently depended on the REAL
production shared-quota state at whatever moment they happened to run -- confirmed flaky
by reproducing an identical failure on an already-pushed, previously-green commit, purely
because the real shared count was near cap that day. All 3 pre-existing tests now mock
the budget guard explicitly.

Run:  uv run python -m dashboard.tests.test_board_scan
"""
from __future__ import annotations

import os
import tempfile
from unittest import mock

_fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        _fails.append(name)


def _isolated_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    old = os.environ.get("DASH_DB_NAME")
    os.environ["DASH_DB_NAME"] = path
    return old, path


def _restore_db(old, path):
    if old is None:
        os.environ.pop("DASH_DB_NAME", None)
    else:
        os.environ["DASH_DB_NAME"] = old
    try:
        os.remove(path)
    except OSError:
        pass


def _mock_budget_ok(board_scan):
    """Two patches needed to get run_board_scan() past its budget gate regardless of the
    REAL shared/local usage state -- see the module docstring's 2026-07-25 note."""
    return (
        mock.patch("analyst.usage_log.shared_calls_ok", return_value=(True, 0)),
        mock.patch.object(board_scan.store, "can_call", return_value=True),
    )


def test_rate_limit_error_sets_backoff_and_returns_none():
    print("run_board_scan(): a RateLimitError (429) is caught, backoff is cached, "
          "call returns (None, status) instead of raising:")
    old, path = _isolated_db()
    try:
        from dashboard.web import board_scan

        def _raise_429(build_chain, messages, temperature=0.2, model=None):
            raise RuntimeError("Error code: 429 - RateLimitError: rate limit exceeded")

        raised = False
        p1, p2 = _mock_budget_ok(board_scan)
        with p1, p2, mock.patch.object(board_scan, "invoke_with_key_fallback", side_effect=_raise_429):
            try:
                result, status = board_scan.run_board_scan([], [])
            except Exception:
                raised = True
        check("does not raise", raised, False)
        check("result is None", result, None)
        check("status mentions rate-limit", "rate-limited" in status, True)
        cached = board_scan._rate_limited_until()
        check("backoff deadline was cached", cached is not None, True)
    finally:
        _restore_db(old, path)


def test_second_call_skips_llm_entirely_while_in_backoff():
    print("\nrun_board_scan(): while backoff is active, the LLM is never invoked again "
          "(no wasted network round-trip):")
    old, path = _isolated_db()
    try:
        from dashboard.web import board_scan
        board_scan._set_rate_limit_backoff()

        calls = []

        def _never_called(build_chain, messages, temperature=0.2, model=None):
            calls.append(1)
            raise AssertionError("should never be called while backing off")

        p1, p2 = _mock_budget_ok(board_scan)
        with p1, p2, mock.patch.object(board_scan, "invoke_with_key_fallback", side_effect=_never_called):
            result, status = board_scan.run_board_scan([], [])
        check("LLM never invoked", len(calls), 0)
        check("result is None", result, None)
        check("status mentions backing off", "backing off" in status, True)
    finally:
        _restore_db(old, path)


def test_permission_denied_error_also_backs_off():
    print("\nrun_board_scan(): a 403 PermissionDeniedError (2026-07-25 incident shape -- "
          "chatanywhere's old key format was deprecated server-side) is ALSO caught and "
          "backed off, not just 429 -- this used to fall through to `raise` and flood the "
          "log with a full traceback every tick for hours:")
    old, path = _isolated_db()
    try:
        from dashboard.web import board_scan

        class PermissionDeniedError(Exception):
            pass

        def _raise_403(build_chain, messages, temperature=0.2, model=None):
            raise PermissionDeniedError(
                "Error code: 403 - {'error': {'code': '403 FORBIDDEN', 'message': 'key invalid'}}")

        raised = False
        p1, p2 = _mock_budget_ok(board_scan)
        with p1, p2, mock.patch.object(board_scan, "invoke_with_key_fallback", side_effect=_raise_403):
            try:
                result, status = board_scan.run_board_scan([], [])
            except Exception:
                raised = True
        check("does not raise", raised, False)
        check("result is None", result, None)
        check("status mentions backing off", "backing off" in status, True)
        check("status does NOT claim 'rate-limited' (it wasn't -- key was rejected)",
              "rate-limited" in status, False)
        cached = board_scan._rate_limited_until()
        check("backoff deadline was cached", cached is not None, True)
    finally:
        _restore_db(old, path)


def test_non_rate_limit_exception_still_propagates():
    print("\nrun_board_scan(): a genuinely unexpected error is NOT swallowed as a "
          "rate limit -- must still propagate so it surfaces as a real bug:")
    old, path = _isolated_db()
    try:
        from dashboard.web import board_scan

        def _raise_schema_error(build_chain, messages, temperature=0.2, model=None):
            raise ValueError("some unrelated schema validation error")

        raised = False
        p1, p2 = _mock_budget_ok(board_scan)
        with p1, p2, mock.patch.object(board_scan, "invoke_with_key_fallback", side_effect=_raise_schema_error):
            try:
                board_scan.run_board_scan([], [])
            except ValueError:
                raised = True
        check("non-rate-limit exception still propagates", raised, True)
    finally:
        _restore_db(old, path)


if __name__ == "__main__":
    test_rate_limit_error_sets_backoff_and_returns_none()
    test_second_call_skips_llm_entirely_while_in_backoff()
    test_permission_denied_error_also_backs_off()
    test_non_rate_limit_exception_still_propagates()
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {_fails}")
        raise SystemExit(1)
    print("all tests passed.")
