"""Unit tests for analyst/llm.py's chatanywhere-then-DeepSeek fallback --
ADDED 2026-07-16. No live network/Supabase needed (httpx.get is mocked).
Run:  uv run python -m dashboard.tests.test_analyst_llm
"""
from __future__ import annotations

import os
from unittest import mock

_fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        _fails.append(name)


class _FakeResp:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status

    def raise_for_status(self):
        if self.status_code != 200:
            raise RuntimeError(f"status {self.status_code}")

    def json(self):
        return self._body


def _reset_cache():
    from analyst import llm
    llm._decision_cache["ts"] = 0.0
    llm._decision_cache["provider"] = None


def test_provider_decision_defaults_chatanywhere_when_not_configured():
    print("provider_decision(): SUPABASE_URL/KEY not set -> chatanywhere, no crash:")
    from analyst import llm
    _reset_cache()
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("SUPABASE_URL", None)
        os.environ.pop("SUPABASE_SERVICE_ROLE_KEY", None)
        result = llm.provider_decision()
    check("defaults to chatanywhere", result, "chatanywhere")


def test_provider_decision_uses_edge_function_response():
    print("\nprovider_decision(): reachable edge function -> uses its answer:")
    from analyst import llm
    _reset_cache()
    with mock.patch.dict(os.environ, {"SUPABASE_URL": "https://fake.supabase.co",
                                      "SUPABASE_SERVICE_ROLE_KEY": "fake-key"}), \
         mock.patch("httpx.get", return_value=_FakeResp({"provider": "deepseek"})):
        result = llm.provider_decision()
    check("returns deepseek per the edge function", result, "deepseek")


def test_provider_decision_fails_open_to_chatanywhere_on_error():
    print("\nprovider_decision(): edge function unreachable -> fails OPEN to "
          "chatanywhere (today's existing behaviour), not an exception:")
    from analyst import llm
    _reset_cache()
    raised = False
    with mock.patch.dict(os.environ, {"SUPABASE_URL": "https://fake.supabase.co",
                                      "SUPABASE_SERVICE_ROLE_KEY": "fake-key"}), \
         mock.patch("httpx.get", side_effect=ConnectionError("network down")):
        try:
            result = llm.provider_decision()
        except Exception:
            raised = True
    check("does not raise", raised, False)
    check("defaults to chatanywhere", result, "chatanywhere")


def test_provider_decision_caches_within_ttl():
    print("\nprovider_decision(): a burst of calls within the TTL window makes only "
          "ONE real HTTP request:")
    from analyst import llm
    _reset_cache()
    calls = []

    def _fake_get(*a, **k):
        calls.append(1)
        return _FakeResp({"provider": "chatanywhere"})

    with mock.patch.dict(os.environ, {"SUPABASE_URL": "https://fake.supabase.co",
                                      "SUPABASE_SERVICE_ROLE_KEY": "fake-key"}), \
         mock.patch("httpx.get", side_effect=_fake_get):
        llm.provider_decision()
        llm.provider_decision()
        llm.provider_decision()
    check("only one real HTTP request for 3 calls in a burst", len(calls), 1)


def test_make_llm_stays_chatanywhere_without_deepseek_key():
    print("\nmake_llm(): DEEPSEEK_API_KEY not set -> always chatanywhere, "
          "even if provider_decision() would say deepseek (nothing to fall back to):")
    from analyst import llm
    _reset_cache()
    with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "fake-openai-key"}, clear=False):
        os.environ.pop("DEEPSEEK_API_KEY", None)
        with mock.patch.object(llm, "provider_decision", return_value="deepseek") as mocked:
            llm.make_llm()
            check("provider_decision() never even called", mocked.called, False)
    check("last_provider_used reports chatanywhere", llm.last_provider_used(), "chatanywhere")


def test_make_llm_switches_to_deepseek_when_decided():
    print("\nmake_llm(): DEEPSEEK_API_KEY set AND provider_decision() says deepseek "
          "-> actually switches, last_provider_used()/last_model_used() reflect it:")
    from analyst import llm
    _reset_cache()
    with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "fake-deepseek-key",
                                      "DEEPSEEK_MODEL": "deepseek-chat"}, clear=False):
        with mock.patch.object(llm, "provider_decision", return_value="deepseek"):
            llm.make_llm()
    check("last_provider_used reports deepseek", llm.last_provider_used(), "deepseek")
    check("last_model_used reports the deepseek model", llm.last_model_used(), "deepseek-chat")


# ADDED 2026-07-25: key-fallback tests, after the primary chatanywhere key was silently
# invalidated server-side for ~9h with no automatic recovery (see HANDOFF.md).
def test_make_llm_uses_fallback_key_when_requested():
    print("\nmake_llm(_use_fallback_key=True): builds the client with "
          "OPENAI_API_KEY_FALLBACK, not the primary OPENAI_API_KEY:")
    from analyst import llm
    with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "primary-key",
                                      "OPENAI_API_KEY_FALLBACK": "fallback-key"}, clear=False):
        primary = llm.make_llm()
        fallback = llm.make_llm(_use_fallback_key=True)
    check("primary uses OPENAI_API_KEY", primary.openai_api_key.get_secret_value(), "primary-key")
    check("fallback uses OPENAI_API_KEY_FALLBACK", fallback.openai_api_key.get_secret_value(), "fallback-key")


def test_make_llm_fallback_key_raises_clearly_when_unset():
    print("\nmake_llm(_use_fallback_key=True): OPENAI_API_KEY_FALLBACK not set -> a clear "
          "RuntimeError naming the specific env var, not a generic auth failure later:")
    from analyst import llm
    with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "primary-key"}, clear=False):
        os.environ.pop("OPENAI_API_KEY_FALLBACK", None)
        raised = None
        try:
            llm.make_llm(_use_fallback_key=True)
        except RuntimeError as e:
            raised = str(e)
    check("names OPENAI_API_KEY_FALLBACK specifically", "OPENAI_API_KEY_FALLBACK" in (raised or ""), True)


def test_is_chatanywhere_unavailable_classifies_correctly():
    print("\nis_chatanywhere_unavailable(): 429/401/403 -> True, anything else -> False:")
    from analyst import llm

    class RateLimitError(Exception):
        pass

    class PermissionDeniedError(Exception):
        pass

    check("429 in message -> True", llm.is_chatanywhere_unavailable(Exception("Error code: 429 - ...")), True)
    check("RateLimitError type -> True", llm.is_chatanywhere_unavailable(RateLimitError("boom")), True)
    check("403 in message -> True (2026-07-25 incident shape)",
          llm.is_chatanywhere_unavailable(Exception("Error code: 403 - {'code': '403 FORBIDDEN'}")), True)
    check("PermissionDeniedError type -> True",
          llm.is_chatanywhere_unavailable(PermissionDeniedError("Error code: 403 - ...")), True)
    check("unrelated ValueError -> False", llm.is_chatanywhere_unavailable(ValueError("bad schema")), False)
    check("unrelated network error -> False", llm.is_chatanywhere_unavailable(ConnectionError("timeout")), False)


def test_invoke_with_key_fallback_retries_on_primary_failure():
    print("\ninvoke_with_key_fallback(): primary key exhausted/dead + fallback configured "
          "-> transparently retries with the fallback key and succeeds:")
    from analyst import llm
    calls = []

    def _fake_make_llm(temperature=0.2, model=None, _use_fallback_key=False):
        calls.append(_use_fallback_key)
        return f"llm(fallback={_use_fallback_key})"

    def _build_chain(fake_llm):
        class _Chain:
            def invoke(self, messages):
                if fake_llm == "llm(fallback=False)":
                    raise Exception("Error code: 429 - rate limit exceeded")
                return "SUCCESS via " + fake_llm
        return _Chain()

    with mock.patch.dict(os.environ, {"OPENAI_API_KEY_FALLBACK": "fallback-key"}, clear=False), \
         mock.patch.object(llm, "make_llm", side_effect=_fake_make_llm):
        result = llm.invoke_with_key_fallback(_build_chain, ["msg"])
    check("tried primary first, then fallback", calls, [False, True])
    check("result came from the fallback call", result, "SUCCESS via llm(fallback=True)")


def test_invoke_with_key_fallback_reraises_when_no_fallback_configured():
    print("\ninvoke_with_key_fallback(): primary fails, no OPENAI_API_KEY_FALLBACK set "
          "-> re-raises the original error rather than silently giving up differently:")
    from analyst import llm

    def _build_chain(fake_llm):
        class _Chain:
            def invoke(self, messages):
                raise Exception("Error code: 429 - rate limit exceeded")
        return _Chain()

    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OPENAI_API_KEY_FALLBACK", None)
        raised = False
        with mock.patch.object(llm, "make_llm", return_value="primary"):
            try:
                llm.invoke_with_key_fallback(_build_chain, ["msg"])
            except Exception:
                raised = True
    check("re-raises when no fallback key is configured", raised, True)


def test_invoke_with_key_fallback_does_not_retry_unrelated_errors():
    print("\ninvoke_with_key_fallback(): a non-key error (e.g. schema validation) is NOT "
          "retried with the fallback key -- a different key won't fix it:")
    from analyst import llm
    calls = []

    def _fake_make_llm(temperature=0.2, model=None, _use_fallback_key=False):
        calls.append(_use_fallback_key)
        return "primary"

    def _build_chain(fake_llm):
        class _Chain:
            def invoke(self, messages):
                raise ValueError("some unrelated schema validation error")
        return _Chain()

    with mock.patch.dict(os.environ, {"OPENAI_API_KEY_FALLBACK": "fallback-key"}, clear=False), \
         mock.patch.object(llm, "make_llm", side_effect=_fake_make_llm):
        raised = False
        try:
            llm.invoke_with_key_fallback(_build_chain, ["msg"])
        except ValueError:
            raised = True
    check("propagates the original ValueError", raised, True)
    check("only the primary was ever built (no wasted fallback attempt)", calls, [False])


if __name__ == "__main__":
    test_provider_decision_defaults_chatanywhere_when_not_configured()
    test_provider_decision_uses_edge_function_response()
    test_provider_decision_fails_open_to_chatanywhere_on_error()
    test_provider_decision_caches_within_ttl()
    test_make_llm_stays_chatanywhere_without_deepseek_key()
    test_make_llm_switches_to_deepseek_when_decided()
    test_make_llm_uses_fallback_key_when_requested()
    test_make_llm_fallback_key_raises_clearly_when_unset()
    test_is_chatanywhere_unavailable_classifies_correctly()
    test_invoke_with_key_fallback_retries_on_primary_failure()
    test_invoke_with_key_fallback_reraises_when_no_fallback_configured()
    test_invoke_with_key_fallback_does_not_retry_unrelated_errors()
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {_fails}")
        raise SystemExit(1)
    print("all tests passed.")
