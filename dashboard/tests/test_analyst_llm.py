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


if __name__ == "__main__":
    test_provider_decision_defaults_chatanywhere_when_not_configured()
    test_provider_decision_uses_edge_function_response()
    test_provider_decision_fails_open_to_chatanywhere_on_error()
    test_provider_decision_caches_within_ttl()
    test_make_llm_stays_chatanywhere_without_deepseek_key()
    test_make_llm_switches_to_deepseek_when_decided()
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {_fails}")
        raise SystemExit(1)
    print("all tests passed.")
