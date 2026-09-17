"""Unit tests for the 2026-09-17 cross-instance sharing: the shared_cache
file layer, the live->paper direction guard, and paper's reuse path in
service.refresh_llm().

No live Supabase/LLM/Docker needed -- SHARED_DIR points at a tmp dir, the DB
is isolated, and the real LLM call is mocked.
Run:  uv run python -m dashboard.tests.test_shared_cache
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


def test_shared_cache_roundtrip_and_ttl():
    print("shared_cache: write/read round-trip, staleness expiry:")
    import time
    from dashboard.core import shared_cache
    old, path = _isolated_shared()
    try:
        check("unconfigured content reads back",
              shared_cache.write("k.json", {"a": 1}), True)
        payload, age = shared_cache.read("k.json", 60)
        check("payload", payload, {"a": 1})
        check("age sane", age is not None and 0 <= age < 5, True)
        check("stale entry is a miss",
              shared_cache.read("k.json", -1), (None, None))
    finally:
        _restore_shared(old, path)


def test_shared_cache_never_raises():
    print("\nshared_cache: missing mount / corrupt file are misses, never raises:")
    from dashboard.core import shared_cache
    old = os.environ.get("SHARED_DIR")
    os.environ["SHARED_DIR"] = "/nonexistent/mount-xyz"
    try:
        check("write without mount -> False", shared_cache.write("k.json", {}), False)
        check("read without mount -> miss", shared_cache.read("k.json", 60), (None, None))
        check("available() False", shared_cache.available(), False)
    finally:
        if old is None:
            os.environ.pop("SHARED_DIR", None)
        else:
            os.environ["SHARED_DIR"] = old
    old2, path = _isolated_shared()
    try:
        with open(os.path.join(path, "bad.json"), "w") as f:
            f.write("{not json")
        check("corrupt file -> miss", shared_cache.read("bad.json", 60), (None, None))
    finally:
        _restore_shared(old2, path)


def test_reuse_direction_guard():
    print("\n_reuse_allowed(): live->paper only, flag-gated:")
    from dashboard.web import service
    with mock.patch.dict(os.environ, {"SHARED_SCAN_ENABLE": "1"}):
        check("paper + flag -> allowed", service._reuse_allowed("paper"), True)
        check("live + flag -> DENIED", service._reuse_allowed("live"), False)
        check("unknown + flag -> denied", service._reuse_allowed("unknown"), False)
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("SHARED_SCAN_ENABLE", None)
        check("paper, flag off -> denied (today's behavior)",
              service._reuse_allowed("paper"), False)


def _score(key, signal="WATCH", direction="neutral", strength=2):
    from dashboard.core.scoring import Score
    return Score(key=key, direction=direction, strength=strength,
                 obviousness=float(strength), signal=signal, note="",
                 facts={}, facts_text=f"Symbol: {key}\nLast price: 100.0")


def _sig(key, action="WAIT"):
    return {"key": key, "bias": "neutral", "action": action, "confidence": 0.8,
            "rationale": "r", "macro_linkage": "none material", "invalidation": "x"}


def test_try_shared_reuse_match_and_mismatch():
    print("\n_try_shared_reuse(): fingerprint match reuses, mismatch/stale misses:")
    from dashboard.web import service
    from dashboard.web import board_scan
    from dashboard.core import shared_cache
    old, path = _isolated_shared()
    try:
        scores = [_score("AAA", signal="BUY", direction="long", strength=5),
                  _score("BBB")]
        news = ["headline one"]
        fp = board_scan.scan_fingerprint(scores, news, ())
        shared_cache.write(shared_cache.SCAN_KEY, {
            "market_fp": fp, "macro_note": "m",
            "signals": [_sig("AAA", "BUY"), _sig("BBB", "WAIT")],
            "model": "gpt-5-mini", "provider": "chatanywhere",
            "environment": "live"})
        result, age_min, model, provider, ms = service._try_shared_reuse(fp)
        check("match -> result", result is not None, True)
        check("actions preserved", [s.action for s in result.signals], ["BUY", "WAIT"])
        check("model passed through", model, "gpt-5-mini")
        check("age sane", age_min is not None and 0 <= age_min < 1, True)
        check("mismatch -> miss",
              service._try_shared_reuse("0" * 32)[0], None)
        with mock.patch.object(board_scan, "SHARED_SCAN_MAX_AGE_MIN", -1):
            check("stale -> miss", service._try_shared_reuse(fp)[0], None)
    finally:
        _restore_shared(old, path)


def _setup_state(scores, news):
    from dashboard.web import service
    saved = dict(service.STATE)
    service.STATE["scores"] = {s.key: s for s in scores}
    service.STATE["news"] = news
    service.STATE["positions"] = {}
    service.STATE["llm"] = {}
    return saved


def test_refresh_llm_reuses_live_scan_on_paper():
    print("\nrefresh_llm(): paper with flag reuses live's scan, never calls the LLM:")
    from dashboard.web import service
    from dashboard.web import board_scan
    from dashboard.core import shared_cache
    from dashboard.core import journal
    old_db, db_path = _isolated_db()
    old_sh, sh_path = _isolated_shared()
    with mock.patch.dict(os.environ, {"SHARED_SCAN_ENABLE": "1",
                                       "DASH_FIXED_MODE": "paper"}):
        saved = _setup_state([_score("AAA", signal="BUY", direction="long", strength=5),
                              _score("BBB")], ["headline one"])
        try:
            from dashboard.core.scoring import rank
            ranked = rank(list(service.STATE["scores"].values()))
            fp = board_scan.scan_fingerprint(ranked, service.STATE["news"], ())
            shared_cache.write(shared_cache.SCAN_KEY, {
                "market_fp": fp, "macro_note": "shared macro",
                "signals": [_sig("AAA", "BUY"), _sig("BBB", "WAIT")],
                "model": "gpt-5-mini", "provider": "chatanywhere",
                "environment": "live"})
            with mock.patch.object(
                    service, "run_board_scan",
                    side_effect=AssertionError("LLM must not be called on reuse")):
                with mock.patch("analyst.usage_log.log_usage"):
                    status = service.refresh_llm(cap=200)
            check("status reports reuse", status.startswith("reused live scan"), True)
            check("STATE['llm'] populated from shared",
                  sorted(service.STATE["llm"].keys()), ["AAA", "BBB"])
            check("macro_note from shared",
                  service.STATE["macro_note"], "shared macro")
            rows = journal.scan_metrics_history(limit=5)
            check("metrics row is a skip/reuse",
                  (rows[0]["skipped"], "reused live scan" in rows[0]["reason"]),
                  (1, True))
            check("reuse cost is zero", rows[0]["cost_usd"], 0.0)
        finally:
            service.STATE.clear()
            service.STATE.update(saved)
    _restore_db(old_db, db_path)
    _restore_shared(old_sh, sh_path)


def test_refresh_llm_flag_off_calls_llm():
    print("\nrefresh_llm(): flag off -> normal LLM path (reuse not attempted):")
    from dashboard.web import service
    from dashboard.web import board_scan
    from dashboard.core import shared_cache
    old_db, db_path = _isolated_db()
    old_sh, sh_path = _isolated_shared()
    with mock.patch.dict(os.environ, {"DASH_FIXED_MODE": "paper"}):
        os.environ.pop("SHARED_SCAN_ENABLE", None)
        saved = _setup_state([_score("AAA")], ["headline one"])
        try:
            from dashboard.core.scoring import rank
            ranked = rank(list(service.STATE["scores"].values()))
            fp = board_scan.scan_fingerprint(ranked, service.STATE["news"], ())
            shared_cache.write(shared_cache.SCAN_KEY, {
                "market_fp": fp, "macro_note": "m",
                "signals": [_sig("AAA")],
                "model": "gpt-5-mini", "provider": "chatanywhere",
                "environment": "live"})
            called = []

            def _fake_scan(scores, headlines, cap=200):
                called.append(1)
                return None, "budget guard (fake)"

            with mock.patch.object(service, "run_board_scan", side_effect=_fake_scan):
                status = service.refresh_llm(cap=200)
            check("LLM path taken despite matching shared file", len(called), 1)
            check("status from own scan", status, "budget guard (fake)")
        finally:
            service.STATE.clear()
            service.STATE.update(saved)
    _restore_db(old_db, db_path)
    _restore_shared(old_sh, sh_path)


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
