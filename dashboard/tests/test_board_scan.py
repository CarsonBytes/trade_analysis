"""Unit tests for web/board_scan.py's rate-limit backoff -- ADDED 2026-07-14.

Context: `run_board_scan()` had NO handling at all for the LLM provider's daily
rate limit (openai.RateLimitError / HTTP 429). Once the shared chatanywhere.tech
free-tier quota (200 req/day) was exhausted, every tick cycle (~15-30s cadence)
re-attempted the doomed call, each one a real, slow network round-trip that
failed anyway -- confirmed via 876 identical RateLimitError log entries and a
matching response-time regression. This tests the fix: catch the rate-limit
condition, cache a backoff deadline, and skip the network call entirely while
still in backoff.

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


def test_rate_limit_error_sets_backoff_and_returns_none():
    print("run_board_scan(): a RateLimitError (429) is caught, backoff is cached, "
          "call returns (None, status) instead of raising:")
    old, path = _isolated_db()
    try:
        from dashboard.web import board_scan

        class _FakeLLM:
            def with_structured_output(self, _model):
                return self

            def invoke(self, _messages):
                raise RuntimeError("Error code: 429 - RateLimitError: rate limit exceeded")

        raised = False
        with mock.patch.object(board_scan, "make_llm", return_value=_FakeLLM()):
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

        class _FakeLLM:
            def with_structured_output(self, _model):
                return self

            def invoke(self, _messages):
                calls.append(1)
                raise AssertionError("should never be called while backing off")

        with mock.patch.object(board_scan, "make_llm", return_value=_FakeLLM()):
            result, status = board_scan.run_board_scan([], [])
        check("LLM never invoked", len(calls), 0)
        check("result is None", result, None)
        check("status mentions backing off", "backing off" in status, True)
    finally:
        _restore_db(old, path)


def test_non_rate_limit_exception_still_propagates():
    print("\nrun_board_scan(): a genuinely unexpected error is NOT swallowed as a "
          "rate limit -- must still propagate so it surfaces as a real bug:")
    old, path = _isolated_db()
    try:
        from dashboard.web import board_scan

        class _FakeLLM:
            def with_structured_output(self, _model):
                return self

            def invoke(self, _messages):
                raise ValueError("some unrelated schema validation error")

        raised = False
        with mock.patch.object(board_scan, "make_llm", return_value=_FakeLLM()):
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
    test_non_rate_limit_exception_still_propagates()
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {_fails}")
        raise SystemExit(1)
    print("all tests passed.")
