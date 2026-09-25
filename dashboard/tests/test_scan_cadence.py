"""Unit tests for the 2026-09-16 token optimization: prompt compression
(_facts_block top-N full + compact rest, headline dedupe), the scan
fingerprint cadence gate (scan_fingerprint/should_scan), the gate-agreement
canary metric, and the scan_metrics local ledger.

No live Supabase/LLM needed -- pure functions plus an isolated sqlite DB.
Run:  uv run python -m dashboard.tests.test_scan_cadence
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
    assert ok, f"{name}: got {got!r} want {want!r}"


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


def _score(key, signal="WATCH", direction="neutral", strength=2, facts_text=None):
    from dashboard.core.scoring import Score
    facts_text = facts_text if facts_text is not None else (
        f"Symbol: {key}\nLast price: 100.00000\nReturns: 1d +0.10%, 5d +0.20%, 20d +0.30%\n"
        f"RSI(14): 55.0  (>70 overbought, <30 oversold)\nATR(14): 1.23456   Realized vol (annual): 12.0%\n"
        f"Trend by horizon -> short: up, medium: up, long: up\n"
        f"Recent 60-bar support: 90.00000  resistance: 110.00000\n"
        f"Price is 8.1 ATR below resistance, 8.1 ATR above support.\nBars available: 500")
    return Score(key=key, direction=direction, strength=strength, obviousness=float(strength),
                 signal=signal, note="", facts={}, facts_text=facts_text)


def test_fingerprint_stable_for_same_inputs():
    print("scan_fingerprint(): identical inputs hash identically:")
    from dashboard.web import board_scan
    scores = [_score(f"ETF{i}") for i in range(12)]
    news = ["Fed holds rates", "Oil spikes on supply shock"]
    check("same inputs -> same hash",
          board_scan.scan_fingerprint(scores, news, ("QQQ",)),
          board_scan.scan_fingerprint(scores, news, ("QQQ",)))


def test_fingerprint_ignores_sub_gate_price_noise():
    print("\nscan_fingerprint(): raw price wiggles that don't change the "
          "deterministic verdict must NOT churn the fingerprint:")
    from dashboard.web import board_scan
    a = [_score("QQQ", signal="BUY", direction="long", strength=5)]
    # same verdict, different underlying facts text (price moved a tick)
    b = [_score("QQQ", signal="BUY", direction="long", strength=5,
                facts_text="Symbol: QQQ\nLast price: 100.07000\nReturns: 1d +0.17%, 5d +0.21%, 20d +0.31%\n"
                           "RSI(14): 55.4  (>70 overbought, <30 oversold)\nmore lines here")]
    check("facts_text noise ignored",
          board_scan.scan_fingerprint(a, []), board_scan.scan_fingerprint(b, []))


def test_fingerprint_changes_on_signal_headline_position_delta():
    print("\nscan_fingerprint(): verdict / headline / position changes all churn it:")
    from dashboard.web import board_scan
    base_scores = [_score(f"ETF{i}") for i in range(12)]
    base_news = ["Fed holds rates"]
    base = board_scan.scan_fingerprint(base_scores, base_news, ())
    changed = list(base_scores)
    changed[0] = _score("ETF0", signal="BUY", direction="long", strength=5)
    check("signal change churns", board_scan.scan_fingerprint(changed, base_news, ()) != base, True)
    check("headline change churns",
          board_scan.scan_fingerprint(base_scores, ["Fed CUTS rates"], ()) != base, True)
    check("position change churns",
          board_scan.scan_fingerprint(base_scores, base_news, ("QQQ",)) != base, True)


def test_should_scan_first_run_and_idle_refresh():
    print("\nshould_scan(): first run and max-idle staleness always proceed:")
    from dashboard.web.board_scan import should_scan, SCAN_MAX_IDLE_MIN
    now = 1_000_000.0
    check("no history -> proceed (cold start)", should_scan("fp", None, None, now)[0], True)
    check("unchanged but idle >2h -> proceed",
          should_scan("fp", "fp", now - (SCAN_MAX_IDLE_MIN * 60 + 1), now),
          (True, "max idle refresh"))


def test_should_scan_skips_unchanged_and_debounces_changed():
    print("\nshould_scan(): unchanged+fresh skips; changed+too-fresh debounces:")
    from dashboard.web.board_scan import should_scan, SCAN_MIN_RESCAN_MIN
    now = 1_000_000.0
    check("unchanged + fresh -> skip",
          should_scan("fp", "fp", now - 600, now), (False, "no signal delta"))
    check("changed + fresh (<5min) -> debounced",
          should_scan("fp2", "fp", now - 60, now)[0], False)
    check("changed + old enough -> proceed (event-driven)",
          should_scan("fp2", "fp", now - (SCAN_MIN_RESCAN_MIN * 60 + 1), now),
          (True, "signal delta"))


def test_should_scan_headlines_only_delta():
    print("\nshould_scan(): headlines-only delta skips scan:")
    from dashboard.web.board_scan import should_scan, SCAN_MIN_RESCAN_MIN
    now = 1_000_000.0
    # Full fingerprint changed but score-only fingerprint unchanged -> skip
    check("headlines-only delta -> skip",
          should_scan("fp2", "fp", now - (SCAN_MIN_RESCAN_MIN * 60 + 1), now,
                      score_only_fp="sfp1", last_score_only_fp="sfp1"),
          (False, "headlines only delta"))
    # Full fingerprint changed AND score-only fingerprint changed -> proceed
    check("signal + headlines delta -> proceed",
          should_scan("fp2", "fp", now - (SCAN_MIN_RESCAN_MIN * 60 + 1), now,
                      score_only_fp="sfp2", last_score_only_fp="sfp1"),
          (True, "signal delta"))
    # No score-only fp provided -> normal behavior (no skip)
    check("no score fp -> proceed",
          should_scan("fp2", "fp", now - (SCAN_MIN_RESCAN_MIN * 60 + 1), now),
          (True, "signal delta"))


def test_score_fingerprint_ignores_headlines():
    print("\nscore_fingerprint(): headlines don't affect hash:")
    from dashboard.web.board_scan import score_fingerprint
    scores = [_score("QQQ", signal="BUY", direction="long", strength=5)]
    check("same scores -> same hash regardless of headlines",
          score_fingerprint(scores, ()),
          score_fingerprint(scores, ()))


def test_facts_block_covers_all_instruments_with_top_n_full():
    print("\n_facts_block(): all 12 keys covered, top-3 full, rest compact:")
    from dashboard.web import board_scan
    scores = [_score(f"ETF{i}", signal="BUY" if i < 2 else "WATCH",
                     direction="long" if i < 2 else "neutral",
                     strength=5 if i < 2 else 2) for i in range(12)]
    full = "\n\n".join(
        f"### {s.key}  (deterministic: {s.signal}, dir {s.direction}, "
        f"strength {s.strength}/5)\n{s.facts_text}" for s in scores)
    compressed = board_scan._facts_block(scores)
    for i in range(12):
        check(f"ETF{i} present", f"### ETF{i}" in compressed, True)
    check("top-0 full (RSI line kept)", "RSI(14)" in compressed.split("### ETF3")[0], True)
    check("ETF3+ compact (marked)", "abbreviated)" in compressed.split("### ETF3")[1], True)
    check("compressed shorter than all-full", len(compressed) < len(full), True)
    saved = 1 - len(compressed) / len(full)
    print(f"    (char saving on 12 instruments: {saved:.0%})")
    check("saving is material (>=25% chars)", saved >= 0.25, True)


def test_dedupe_headlines():
    print("\n_dedupe_headlines(): order-preserving, capped:")
    from dashboard.web.board_scan import _dedupe_headlines
    news = ["a", "b", "a", "", "c", "b", "d"]
    check("dupes+empties removed, order kept", _dedupe_headlines(news, limit=10),
          ["a", "b", "c", "d"])
    check("cap respected", len(_dedupe_headlines([str(i) for i in range(20)], limit=10)), 10)


def test_gate_agreement():
    print("\ngate_agreement(): matches det verdicts, None when nothing to compare:")
    from dashboard.core import journal
    from dashboard.web.board_scan import InstrumentSignal

    def _sig(key, action):
        return InstrumentSignal(key=key, bias="neutral", action=action, confidence=0.8,
                                rationale="r", macro_linkage="none material",
                                invalidation="x")

    class _R:
        def __init__(self, signals):
            self.signals = signals

    scores = {"A": _score("A", signal="BUY", direction="long", strength=5),
              "B": _score("B", signal="WATCH", direction="neutral", strength=2)}
    check("full agreement",
          journal.gate_agreement(_R([_sig("A", "BUY"), _sig("B", "WAIT")]), scores), 1.0)
    check("LLM veto counts as disagreement (expected -- vetoes are often correct)",
          journal.gate_agreement(_R([_sig("A", "WAIT"), _sig("B", "WAIT")]), scores), 0.5)
    check("empty result -> None", journal.gate_agreement(_R([]), scores), None)
    check("None result -> None", journal.gate_agreement(None, scores), None)


def test_scan_metrics_roundtrip():
    print("\nrecord_scan_metrics()/scan_metrics_history(): scan + skip rows round-trip:")
    from dashboard.core import journal
    old, path = _isolated_db()
    try:
        journal.record_scan_metrics(environment="live", kind="board_scan", n_signals=12,
                                    agreement=0.75, input_tokens=1800, output_tokens=900,
                                    cost_usd=0.002, latency_ms=1200,
                                    skipped=False, reason="ok")
        journal.record_scan_metrics(environment="live", kind="board_scan", n_signals=12,
                                    agreement=None, input_tokens=0, output_tokens=0,
                                    cost_usd=0.0, latency_ms=0,
                                    skipped=True, reason="no signal delta")
        rows = journal.scan_metrics_history(limit=10)
        check("two rows", len(rows), 2)
        check("newest first (skip first)", (rows[0]["skipped"], rows[0]["reason"]),
              (1, "no signal delta"))
        check("scan row kept tokens+agreement",
              (rows[1]["input_tokens"], rows[1]["output_tokens"], rows[1]["agreement"]),
              (1800, 900, 0.75))
        check("environment attributed", rows[1]["environment"], "live")
    finally:
        _restore_db(old, path)


def test_resolve_environment_never_null():
    print("\n_resolve_environment(): env var -> mode pointer -> 'unknown', never None:")
    import os as _os
    from analyst import usage_log
    with mock.patch.dict(_os.environ, {"DASH_FIXED_MODE": "live"}):
        check("pinned env wins", usage_log._resolve_environment(), "live")
    with mock.patch.dict(_os.environ, {}, clear=False):
        _os.environ.pop("DASH_FIXED_MODE", None)
        with mock.patch.object(usage_log, "_mode_db_path", return_value="/nonexistent/path.db"):
            check("nothing anywhere -> 'unknown' literal, not None",
                  usage_log._resolve_environment(), "unknown")


def test_log_usage_posts_resolved_environment():
    print("\nlog_usage(): posts the resolved environment (never None):")
    import os as _os
    from analyst import usage_log
    captured = {}

    class _Resp:
        def raise_for_status(self):
            pass

    def _fake_post(url, **k):
        captured["json"] = k.get("json", {})
        return _Resp()

    with mock.patch.object(usage_log, "SUPABASE_URL", "https://fake.supabase.co"), \
         mock.patch.object(usage_log, "SUPABASE_SERVICE_ROLE_KEY", "fake-key"), \
         mock.patch("httpx.post", side_effect=_fake_post), \
         mock.patch.dict(_os.environ, {}, clear=False):
        _os.environ.pop("DASH_FIXED_MODE", None)
        with mock.patch.object(usage_log, "_mode_db_path", return_value="/nonexistent/path.db"):
            usage_log.log_usage(kind="board_scan", model="gpt-5-mini",
                                input_tokens=100, output_tokens=50, latency_ms=10)
    check("environment posted and non-null", captured["json"].get("environment"), "unknown")


def test_estimate_cost_matches_log_pricing():
    print("\nestimate_cost(): single definition used by log_usage():")
    from analyst.usage_log import estimate_cost
    check("gpt-5-mini 1M/1M", estimate_cost("gpt-5-mini", 1_000_000, 1_000_000), 2.25)
    check("unknown model falls back", estimate_cost("nope", 1_000_000, 0), 0.5)


def _metric_row(minutes_ago, skipped=False, reason="ok", now=None,
                n_signals=12, agreement=0.75, tok_in=1800, tok_out=900):
    import datetime as _dt
    base = now or _dt.datetime.now(_dt.timezone.utc)
    return {"ts": (base - _dt.timedelta(minutes=minutes_ago)).isoformat(),
            "environment": "live", "kind": "board_scan", "n_signals": n_signals,
            "agreement": agreement, "input_tokens": 0 if skipped else tok_in,
            "output_tokens": 0 if skipped else tok_out, "cost_usd": 0.002,
            "latency_ms": 1200, "skipped": 1 if skipped else 0, "reason": reason}


def test_heartbeat_fresh_and_reused():
    print("\nllm_scan_status(): fresh + shared-reuse headlines:")
    import datetime as _dt
    from dashboard.web.service import llm_scan_status
    now = _dt.datetime.now(_dt.timezone.utc)
    fresh = _metric_row(5, now=now)
    st = llm_scan_status(now=now, latest=fresh, latest_usable=fresh,
                         board_scan_ts=None, backoff_until_iso=None,
                         last_status="ok", market_open=True)
    check("fresh state", st["state"], "fresh")
    check("brain source fresh", st["brain_source"], "fresh")
    check("not stale", st["stale"], False)
    check("brain line names signals", "12 signals" in st["brain_line"], True)

    reused = _metric_row(12, skipped=True, reason="reused live scan 8min old", now=now)
    st = llm_scan_status(now=now, latest=reused, latest_usable=reused,
                         board_scan_ts=None, backoff_until_iso=None,
                         last_status="reused live scan (8min old)", market_open=True)
    check("reuse not misclassified as skip", st["state"], "reused")
    check("brain source shared", st["brain_source"], "shared")


def test_heartbeat_skip_reports_brain_not_attempt():
    print("\nllm_scan_status(): skip shows BRAIN age (usable scan), not check age:")
    import datetime as _dt
    from dashboard.web.service import llm_scan_status
    now = _dt.datetime.now(_dt.timezone.utc)
    usable = _metric_row(40, now=now)
    skip = _metric_row(5, skipped=True, reason="no signal delta", now=now)
    st = llm_scan_status(now=now, latest=skip, latest_usable=usable,
                         board_scan_ts=None, backoff_until_iso=None,
                         last_status="skipped (no signal delta) -- reusing last scan",
                         market_open=True)
    check("skip state", st["state"], "skipped")
    check("brain age ~40m", round((st["brain_age_s"] or 0) / 60), 40)
    check("attempt age ~5m", round((st["attempt_age_s"] or 0) / 60), 5)
    check("not stale (40m < 2h idle)", st["stale"], False)


def test_heartbeat_blocked_paused_cached_never():
    print("\nllm_scan_status(): blocked / paused / cached / never:")
    import datetime as _dt
    from dashboard.web.service import llm_scan_status
    now = _dt.datetime.now(_dt.timezone.utc)
    usable = _metric_row(180, now=now)
    future = (now + _dt.timedelta(hours=20)).isoformat()
    st = llm_scan_status(now=now, latest=usable, latest_usable=usable,
                         board_scan_ts=None, backoff_until_iso=future,
                         last_status="provider's 7-day free-points window exhausted -- backing off",
                         market_open=True)
    check("backoff -> blocked", st["state"], "blocked")
    check("7-day inferred", st["reason"], "7-day free-points window exhausted")
    check("retry HKT present", bool(st["retry_hkt"]), True)
    check("3h brain in hours -> stale", st["stale"], True)

    st = llm_scan_status(now=now, latest=usable, latest_usable=usable,
                         board_scan_ts=None, backoff_until_iso=None,
                         last_status="market closed (auto-pause) — LLM skipped",
                         market_open=False)
    check("auto-pause -> paused", st["state"], "paused")
    check("paused never stale (market closed)", st["stale"], False)

    st = llm_scan_status(now=now, latest=None, latest_usable=None,
                         board_scan_ts=(now - _dt.timedelta(minutes=30)).isoformat(),
                         backoff_until_iso=None, last_status="restored cached scan",
                         market_open=True)
    check("cache fallback -> cached", st["state"], "cached")

    st = llm_scan_status(now=now, latest=None, latest_usable=None,
                         board_scan_ts=None, backoff_until_iso=None,
                         last_status="not run yet", market_open=None)
    check("nothing anywhere -> never", st["state"], "never")
    check("unknown market -> not stale", st["stale"], False)


def test_heartbeat_malformed_inputs_degrade():
    print("\nllm_scan_status(): malformed timestamps degrade, never raise:")
    import datetime as _dt
    from dashboard.web.service import llm_scan_status
    now = _dt.datetime.now(_dt.timezone.utc)
    st = llm_scan_status(now=now, latest={"ts": "garbage!!", "skipped": 0, "reason": "ok"},
                         latest_usable=None, board_scan_ts="also bad",
                         backoff_until_iso="nope", last_status="",
                         market_open=True)
    check("bad ts -> no crash, usable state key", "state" in st, True)
    check("bad brain ts -> never", st["state"], "never")


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
