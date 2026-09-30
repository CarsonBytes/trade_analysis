"""Token truth + scan-output brevity (S1/S2/S5 of the 2026-09-30 token spec).

S1/S2: log_usage() now posts prompt_tokens_est (our own CJK-aware count of the
prompt text, constants measured against api.deepseek.com) next to whatever the
proxy claims, plus a suspect flag for usage that cannot be physically real --
found live the same day: four event-radar embedding rows claimed exactly
1,000,000 prompt tokens in 200ms (real ones take 800-2,900ms) and the proxy
counts CJK rerank batches ~5.8x higher than any real tokenizer.

S5: quant's completion tokens are 44% of its ledger usage (154,980 of
353,500 over 14d) and go almost entirely into the per-instrument prose fields
of the BoardScan schema -- rationale/macro_linkage/invalidation/macro_note now
carry word budgets. The decision fields (bias/action/confidence) and the
freshness floors (SCAN_MAX_IDLE_MIN etc.) are deliberately untouched: this is
a compression of wording, never of what is decided or how stale it may get.

Run:  uv run python -m dashboard.tests.test_token_truth
"""
from __future__ import annotations

import os
from unittest import mock

from dashboard.tests import hermetic  # noqa: F401  (3.3 hermetic env for script runs)

_fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        _fails.append(name)
    assert ok, f"{name}: got {got!r} want {want!r}"


def test_estimate_prompt_tokens_matches_measured_rates():
    print("\nestimate_prompt_tokens(): measured chars-per-token, CJK-aware (S1):")
    from analyst.usage_log import ASCII_CHARS_PER_TOKEN, CJK_CHARS_PER_TOKEN, estimate_prompt_tokens
    check("ASCII: 600 chars / 6.0 = 100 tokens", estimate_prompt_tokens("a" * 600), 100)
    check("CJK: 48 chars / 4.8 = 10 tokens (ASCII-only would say 8)",
          estimate_prompt_tokens("粵" * 48), 10)
    check("mixed sums both halves and rounds up",
          estimate_prompt_tokens("粵" * 48 + "abcde"), 11)
    check("None means 'not measured', distinct from 0",
          estimate_prompt_tokens(None), None)
    check("empty prompt is a real 0", estimate_prompt_tokens(""), 0)
    check("constants are the measured ones (rounded down)",
          (ASCII_CHARS_PER_TOKEN, CJK_CHARS_PER_TOKEN), (6.0, 4.8))


def test_is_suspect_usage_flags_impossible_rows_only():
    print("\nis_suspect_usage(): size/throughput sanity (S2):")
    from analyst.usage_log import is_suspect_usage
    check("the fabricated 1M-in-200ms row is suspect",
          is_suspect_usage(1_000_000, 200), True)
    check("big but slow real call is not",
          is_suspect_usage(90_671, 5_000), False)
    check("failed 0-token row is not", is_suspect_usage(0, 500), False)
    check("unknown latency with sane size is not", is_suspect_usage(3_378, 0), False)
    check("100K tokens in 1s = 100 tok/ms -> suspect (rate check, under size cap)",
          is_suspect_usage(100_000, 1_000), True)


def test_log_usage_posts_estimate_and_flag():
    print("\nlog_usage(): posts prompt_tokens_est + suspect, provider number untouched (S1/S2):")
    from analyst import usage_log
    saved = {k: os.environ.get(k) for k in ("LLM_LEDGER_DISABLED",)}
    os.environ.pop("LLM_LEDGER_DISABLED", None)   # this test exercises the real POST path
    captured = {}

    class _Resp:
        status_code = 201

        def raise_for_status(self):
            pass

    def _fake_post(url, **kw):
        captured.update(kw.get("json") or {})
        return _Resp()

    try:
        with mock.patch.object(usage_log, "SUPABASE_URL", "https://fake.supabase.co"), \
             mock.patch.object(usage_log, "SUPABASE_SERVICE_ROLE_KEY", "fake-key"), \
             mock.patch.object(usage_log, "_resolve_environment", return_value="paper"), \
             mock.patch("httpx.post", side_effect=_fake_post):
            usage_log.log_usage(kind="board_scan", model="gpt-5.4-mini",
                                input_tokens=1_000_000, output_tokens=0, latency_ms=200,
                                prompt_text="a" * 600)
    finally:
        os.environ.update({k: v for k, v in saved.items() if v is not None})
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)

    check("provider claim kept verbatim", captured.get("prompt_tokens"), 1_000_000)
    check("our own estimate of the same text", captured.get("prompt_tokens_est"), 100)
    check("impossible row flagged", captured.get("suspect"), True)


def test_log_usage_posts_null_estimate_without_prompt_text():
    print("\nlog_usage(): no prompt text -> NULL estimate, not a fake zero:")
    from analyst import usage_log
    saved = os.environ.get("LLM_LEDGER_DISABLED")
    os.environ.pop("LLM_LEDGER_DISABLED", None)
    captured = {}

    class _Resp:
        status_code = 201

        def raise_for_status(self):
            pass

    try:
        with mock.patch.object(usage_log, "SUPABASE_URL", "https://fake.supabase.co"), \
             mock.patch.object(usage_log, "SUPABASE_SERVICE_ROLE_KEY", "fake-key"), \
             mock.patch.object(usage_log, "_resolve_environment", return_value="paper"), \
             mock.patch("httpx.post", side_effect=lambda url, **kw: captured.update(kw.get("json") or {}) or _Resp()):
            usage_log.log_usage(kind="heartbeat", model="n/a",
                                input_tokens=0, output_tokens=0, latency_ms=1)
    finally:
        if saved is None:
            os.environ.pop("LLM_LEDGER_DISABLED", None)
        else:
            os.environ["LLM_LEDGER_DISABLED"] = saved

    check("estimate is None (not measured)", captured.get("prompt_tokens_est"), None)
    check("sane row not flagged", captured.get("suspect"), False)


def test_scan_output_schema_carries_word_budgets():
    print("\nBoardScan schema: per-instrument prose has a word budget (S5):")
    from dashboard.web.board_scan import BoardScan, InstrumentSignal
    check("rationale bounded to one 20-word sentence",
          "at most 20 words" in (InstrumentSignal.model_fields["rationale"].description or ""), True)
    check("invalidation bounded to one clause",
          "one clause" in (InstrumentSignal.model_fields["invalidation"].description or ""), True)
    check("macro_note capped at two sentences",
          "TWO sentences" in (BoardScan.model_fields["macro_note"].description or ""), True)
    check("decision fields still present and unchanged",
          sorted(InstrumentSignal.model_fields),
          ["action", "bias", "confidence", "invalidation", "key", "macro_linkage", "rationale"])


def test_system_prompt_asks_for_terse_output():
    print("\nSYSTEM: terseness instruction + unchanged decision contract (S5):")
    from dashboard.web.board_scan import SYSTEM
    check("asks for terse short clauses", "Be terse" in SYSTEM, True)
    check("rationale budget repeated in the prompt", "at most 20 words" in SYSTEM, True)
    check("still requires a signal for every instrument",
          "for EACH" in SYSTEM, True)
    check("still refuses to invent numbers", "Do NOT invent numbers" in SYSTEM, True)
    check("macro_linkage never-skip rule preserved (CPER incident)",
          "never skip" in SYSTEM, True)


def test_freshness_floors_are_untouched():
    print("\nshould_scan(): the quality floors S5 must not have moved:")
    from dashboard.web import board_scan
    check("periodic refresh floor still 8h", board_scan.SCAN_MAX_IDLE_MIN, 480)
    check("signal-change debounce still 30min", board_scan.SCAN_MIN_RESCAN_MIN, 30)

    fp, last_fp, now = "fp1", "fp1", 1_000_000.0
    check("no delta under the idle floor still skips",
          board_scan.should_scan(fp, last_fp, now - 60, now, fp, last_fp),
          (False, "no signal delta"))
    check("past the 8h floor a refresh is forced regardless of delta",
          board_scan.should_scan(fp, last_fp, now - (481 * 60), now, fp, last_fp),
          (True, "max idle refresh"))


if __name__ == "__main__":
    for _name, _fn in list(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
            except AssertionError:
                pass
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {_fails}")
        raise SystemExit(1)
    print("all tests passed.")
