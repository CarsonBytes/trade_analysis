"""Regression tests for the 2026-09-29 scan-throttle follow-ups:

1. transition-only skip journaling -- refresh_llm() journalled ONE scan_metrics
   row per evaluation; with the llm_min gate that is ~48/day of identical rows
   (and ~2,500/day without it -- the flood measured on live). Now a row is
   written only when the skip REASON changes (plus first-after-success/restart).
2. fp_audit counters -- service._fp_audit_tick() keeps minute-resolution churn
   visibility in ONE in-place cache row (zero table growth) between the now-
   sparse journal rows, exposed on /status as fp_audit.

No live LLM/Broker needed: DB isolated via DASH_DB_NAME, shared dir via
SHARED_DIR, run_board_scan mocked (the skip path must never reach it).
Run:  uv run python -m dashboard.tests.test_scan_throttle
"""
from __future__ import annotations

import os
import tempfile
import time
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


def _isolated_shared():
    path = tempfile.mkdtemp()
    old = os.environ.get("SHARED_DIR")
    os.environ["SHARED_DIR"] = path
    return old, path


def _restore_shared(old, path):
    if old is None:
        os.environ.pop("SHARED_DIR", None)
    else:
        os.environ["SHARED_DIR"] = old
    import shutil
    shutil.rmtree(path, ignore_errors=True)


def _score(key, signal="WATCH", direction="neutral", strength=2):
    from dashboard.core.scoring import Score
    return Score(key=key, direction=direction, strength=strength,
                 obviousness=float(strength), signal=signal, note="",
                 facts={}, facts_text=f"Symbol: {key}\nLast price: 100.0")


def _setup_state(scores, news):
    from dashboard.web import service
    saved = dict(service.STATE)
    service.STATE["scores"] = {s.key: s for s in scores}
    service.STATE["news"] = news
    service.STATE["positions"] = {}
    service.STATE["llm"] = {}
    service.STATE.pop("_last_skip_reason", None)
    return saved


def _restore_state(saved):
    from dashboard.web import service
    service.STATE.clear()
    service.STATE.update(saved)


def _seed_current_fingerprint(news):
    """Persist fp/sfp/scan_ts so the NEXT refresh_llm() evaluates as
    'unchanged fingerprint, recently scanned' (i.e. a cadence skip)."""
    from dashboard.core import store
    from dashboard.core.scoring import rank
    from dashboard.web import board_scan
    from dashboard.web import service
    ranked = rank(list(service.STATE["scores"].values()))
    fp = board_scan.scan_fingerprint(ranked, news, ())
    sfp = board_scan.score_fingerprint(ranked, ())
    store.cache_set("llm_scan_fingerprint", fp)
    store.cache_set("llm_score_fingerprint", sfp)
    store.cache_set("llm_scan_ts", time.time())
    store.cache_set("llm_scan_attempt_ts", time.time())


def test_skip_journal_transition_only():
    print("\nrefresh_llm(): skip rows journal on REASON TRANSITION, not per evaluation:")
    from dashboard.core import journal, store
    from dashboard.web import service

    old_db, db_path = _isolated_db()
    old_sh, sh_path = _isolated_shared()
    with mock.patch.dict(os.environ, {"DASH_FIXED_MODE": "paper"}):
        saved = _setup_state([_score("AAA", signal="BUY", direction="long", strength=5),
                              _score("BBB")], ["headline one"])
        try:
            _seed_current_fingerprint(service.STATE["news"])
            no_scan = mock.patch.object(
                service, "run_board_scan",
                side_effect=AssertionError("skip path must never call the LLM"))
            with no_scan:
                s1 = service.refresh_llm(cap=200)      # "no signal delta" (first skip)
                n1 = len(journal.scan_metrics_history(limit=50))
                s2 = service.refresh_llm(cap=200)      # SAME reason -> no new row
                n2 = len(journal.scan_metrics_history(limit=50))
                service.STATE["news"] = ["headline CHANGED"]
                s3 = service.refresh_llm(cap=200)      # "headlines only delta" -> row
                n3 = len(journal.scan_metrics_history(limit=50))
                service.STATE["news"] = ["headline one"]
                s4 = service.refresh_llm(cap=200)      # back to "no signal delta" -> row
                n4 = len(journal.scan_metrics_history(limit=50))
                service.STATE["news"] = ["headline one"]
                s5 = service.refresh_llm(cap=200)      # same -> no row
                n5 = len(journal.scan_metrics_history(limit=50))
            check("1st skip journals", n1, 1)
            check("2nd identical skip is thinned", (s2, n2),
                  ("skipped (no signal delta) -- reusing last scan", 1))
            check("reason transition journals again", (s3, n3),
                  ("skipped (headlines only delta) -- reusing last scan", 2))
            check("transition back journals again", n4, 3)
            check("repeat after transition is thinned", n5, 3)
            check("all rows are zero-token skips",
                  all(r["skipped"] == 1 and r["input_tokens"] == 0
                      for r in journal.scan_metrics_history(limit=50)), True)
            check("skip reasons preserved for the funnel",
                  sorted({r["reason"] for r in journal.scan_metrics_history(limit=50)}),
                  ["headlines only delta", "no signal delta"])
        finally:
            _restore_state(saved)
    _restore_db(old_db, db_path)
    _restore_shared(old_sh, sh_path)


def test_skip_after_success_journals_again():
    print("\nrefresh_llm(): a success resets the transition tracker:")
    from dashboard.core import journal, store
    from dashboard.web import service

    old_db, db_path = _isolated_db()
    old_sh, sh_path = _isolated_shared()
    with mock.patch.dict(os.environ, {"DASH_FIXED_MODE": "paper"}):
        saved = _setup_state([_score("AAA", signal="BUY", direction="long", strength=5)],
                             ["headline one"])
        try:
            _seed_current_fingerprint(service.STATE["news"])
            with mock.patch.object(service, "run_board_scan",
                                   side_effect=AssertionError("skip path must not scan")):
                service.refresh_llm(cap=200)                       # skip: "no signal delta"
                service.refresh_llm(cap=200)                       # thinned
                check("one row before success", len(journal.scan_metrics_history(50)), 1)
            # A real (mocked) scan success: journals a non-skip row AND resets
            # _last_skip_reason so the next skip writes again.
            class _FakeScan:
                macro_note = "m"
                signals = []

            with mock.patch.object(service, "run_board_scan",
                                   return_value=(_FakeScan(), "ok")), \
                    mock.patch.object(service.journal, "record_scan"), \
                    mock.patch.object(service.journal, "gate_agreement", return_value=None), \
                    mock.patch("analyst.usage_log.log_usage"), \
                    mock.patch.object(service.paper, "place_from_state", return_value=[]), \
                    mock.patch.object(service.broker, "mirror_new", return_value=[]), \
                    mock.patch.object(service.usage_log, "fetch_shared_usage_today",
                                      return_value={"calls": 0, "calls_by_project": {}}):
                s_ok = service.refresh_llm(cap=200, force=True)
            check("success path reached", s_ok, "ok")
            check("success row journaled (2 rows total)",
                  len(journal.scan_metrics_history(50)), 2)
            with mock.patch.object(service, "run_board_scan",
                                   side_effect=AssertionError("skip path must not scan")):
                service.refresh_llm(cap=200)                       # skip again: journals (3)
            check("skip after success journals again",
                  len(journal.scan_metrics_history(50)), 3)
        finally:
            _restore_state(saved)
    _restore_db(old_db, db_path)
    _restore_shared(old_sh, sh_path)


def test_fp_audit_counters():
    print("\n_fp_audit_tick(): one in-place row, per-day churn counters:")
    from dashboard.core import store
    from dashboard.web import service

    old_db, db_path = _isolated_db()
    old_sh, sh_path = _isolated_shared()
    with mock.patch.dict(os.environ, {"DASH_FIXED_MODE": "paper"}):
        saved = _setup_state([_score("AAA", signal="BUY", direction="long", strength=5),
                              _score("BBB")], ["headline one"])
        try:
            service._fp_audit_tick()
            a = store.cache_get(service._FP_AUDIT_KEY)[0]
            check("row exists with day+evals", (bool(a.get("day")), a.get("evals")),
                  (True, 1))
            check("first tick records baseline delta", a.get("deltas"), 1)
            service._fp_audit_tick()                    # identical -> evals up, deltas flat
            a = store.cache_get(service._FP_AUDIT_KEY)[0]
            check("2nd tick no delta", (a["evals"], a["deltas"], a["score_deltas"]),
                  (2, 1, 1))
            service.STATE["scores"]["BBB"] = _score("BBB", signal="SELL",
                                                    direction="short", strength=4)
            service._fp_audit_tick()                    # score change -> both counters
            a = store.cache_get(service._FP_AUDIT_KEY)[0]
            check("score change counts", (a["evals"], a["deltas"], a["score_deltas"]),
                  (3, 2, 2))
            service.STATE["news"] = ["totally new headline"]
            service._fp_audit_tick()                    # headline-only churn
            a = store.cache_get(service._FP_AUDIT_KEY)[0]
            check("headline-only churn counts delta but not score_delta",
                  (a["evals"], a["deltas"], a["score_deltas"]), (4, 3, 2))
            check("last_delta_ts tracked", bool(a.get("last_delta_ts")), True)
            check("single row, not a table (re-read is the same key)",
                  store.cache_get(service._FP_AUDIT_KEY)[0]["evals"], 4)
        finally:
            _restore_state(saved)
    _restore_db(old_db, db_path)
    _restore_shared(old_sh, sh_path)


def test_max_instruments_coverage_bounds():
    print("\nMAX_INSTRUMENTS: 2026-09-29 user decision (6 -> 9) keeps quota+coverage bounds:")
    from dashboard.web import board_scan
    check("raised to 9", board_scan.MAX_INSTRUMENTS, 9)
    check("still <= 14 (measured working size)", board_scan.MAX_INSTRUMENTS <= 14, True)
    scores = [_score(f"K{i}") for i in range(12)]
    fp = board_scan.scan_fingerprint(scores, [], ())
    deep = list(scores)
    deep[6] = _score("K6", signal="SELL", direction="short", strength=4)
    check("rank-7 (index 6) IS inside the fingerprint window",
          board_scan.scan_fingerprint(deep, [], ()) != fp, True)
    beyond = list(scores)
    beyond[9] = _score("K9", signal="SELL", direction="short", strength=4)
    check("rank-10 (index 9) is OUTSIDE the window (quota bound holds)",
          board_scan.scan_fingerprint(beyond, [], ()) == fp, True)


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
