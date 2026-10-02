"""PAPER_LLM_MODE=mirror (2026-10-02): paper never spends an LLM call.

The behaviour this pins down, in the order it matters:

  1. POLICY.  paper_llm_mode() is opt-in, paper-only, and typo-proof -- an
     unrecognised value falls back to "own" rather than raising or silently
     disabling trading. Live can never end up mirrored, because live is the
     source, not the mirror.
  2. NEVER A CALL.  With a shared scan present, absent-but-cached, or entirely
     absent, run_board_scan() must not be reached -- not on the cadence path and
     not on force=True (a manual refresh must not be a way to spend paper's
     quota). Before this existed the fingerprint check made paper call its own
     LLM 32 times in 21d while reusing live's zero times.
  3. SIGNALS STILL ARRIVE.  Fingerprint mismatch used to mean "no signals at
     all" (live's top-9 ordering differs from paper's essentially always).
     Mirror mode serves live's scan anyway -- same universe, different ordering.
  4. PENDING TRADES STILL WORK.  place_from_state()/mirror_new() are what create
     and fund pending paper trades, and both live inside the branch that a
     no-signal result used to skip entirely. With no scan source at all, paper
     must still place off deterministic signals (evaluate_signal supports
     llm_sig=None) instead of stalling.
  5. CADENCE.  Re-reading is free, so the gate stops only on "nothing newer",
     not on "my own board didn't change" -- otherwise the demo brain would sit
     on an old scan for hours.

Run:  uv run python -m dashboard.tests.test_paper_mirror
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


def _sig(key, action="WAIT"):
    return {"key": key, "bias": "neutral", "action": action, "confidence": 0.8,
            "rationale": "r", "macro_linkage": "none material", "invalidation": "x"}


def _setup_state(scores, news):
    from dashboard.web import service
    saved = dict(service.STATE)
    service.STATE["scores"] = {s.key: s for s in scores}
    service.STATE["news"] = news
    service.STATE["positions"] = {}
    service.STATE["llm"] = {}
    return saved


def _no_llm():
    """Patch run_board_scan so ANY call fails the test loudly -- the whole point
    of mirror mode is that this line is unreachable."""
    from dashboard.web import service
    return mock.patch.object(
        service, "run_board_scan",
        side_effect=AssertionError("paper must never call the LLM in mirror mode"))


# ---- 1. policy ---------------------------------------------------------------

def test_mode_is_opt_in_and_paper_only():
    print("\npaper_llm_mode(): opt-in, paper-only, typo-proof:")
    from dashboard.web import board_scan
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("PAPER_LLM_MODE", None)
        check("unset -> own (today's behaviour)", board_scan.paper_llm_mode("paper"), "own")
    with mock.patch.dict(os.environ, {"PAPER_LLM_MODE": "mirror"}):
        check("paper + mirror -> mirror", board_scan.paper_llm_mode("paper"), "mirror")
        check("LIVE can never be mirrored", board_scan.paper_llm_mode("live"), "own")
        check("unresolved mode -> own, not mirror (fail-safe, not fail-open)",
              board_scan.paper_llm_mode("unknown"), "own")
        check("garbage mode -> own", board_scan.paper_llm_mode("???"), "own")
    with mock.patch.dict(os.environ, {"PAPER_LLM_MODE": "MIRROR "}):
        check("case/whitespace tolerant", board_scan.paper_llm_mode("paper"), "mirror")
    for typo in ("miror", "mirrored", "yes", "1"):
        with mock.patch.dict(os.environ, {"PAPER_LLM_MODE": typo}):
            check(f"typo {typo!r} -> own (never silently mirrors)",
                  board_scan.paper_llm_mode("paper"), "own")


def test_mirror_mode_helper_tracks_the_flag():
    print("\n_service._mirror_mode(): same policy, resolved from this instance:")
    from dashboard.web import service
    with mock.patch.dict(os.environ, {"PAPER_LLM_MODE": "mirror"}):
        check("paper -> True", service._mirror_mode("paper"), True)
        check("live -> False", service._mirror_mode("live"), False)
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("PAPER_LLM_MODE", None)
        check("flag off -> False", service._mirror_mode("paper"), False)


# ---- 2/3. never calls, signals still arrive -----------------------------------

def test_mirror_serves_live_scan_despite_fingerprint_mismatch():
    print("\nmirror: serves live's scan even when the fingerprints differ:")
    from dashboard.web import service
    from dashboard.web import board_scan
    from dashboard.core import shared_cache
    old_db, db_path = _isolated_db()
    old_sh, sh_path = _isolated_shared()
    with mock.patch.dict(os.environ, {"PAPER_LLM_MODE": "mirror",
                                       "SHARED_SCAN_ENABLE": "1",
                                       "DASH_FIXED_MODE": "paper"}):
        saved = _setup_state([_score("AAA", signal="BUY", direction="long", strength=5),
                              _score("BBB")], ["headline one"])
        placed = []
        try:
            from dashboard.core import paper as paper_mod
            shared_cache.write(shared_cache.SCAN_KEY, {
                "market_fp": "0" * 32,          # deliberately NOT paper's fingerprint
                "macro_note": "live macro", "signals": [_sig("AAA", "BUY"), _sig("BBB")],
                "model": "gpt-5.4-mini", "provider": "chatanywhere",
                "environment": "live"})
            with _no_llm(), \
                 mock.patch.object(paper_mod, "place_from_state",
                                   side_effect=lambda st: placed.append(1) or ["ok"]), \
                 mock.patch("analyst.usage_log.log_usage"):
                status = service.refresh_llm(cap=200)
            check("status reports a reused live scan",
                  status.startswith("reused live scan"), True)
            check("STATE['llm'] populated from live's scan",
                  sorted(service.STATE["llm"].keys()), ["AAA", "BBB"])
            check("live's actions carried over",
                  service.STATE["llm"]["AAA"].action, "BUY")
            check("macro_note carried over", service.STATE["macro_note"], "live macro")
            check("pending-trade placement still ran", len(placed), 1)
            from dashboard.core import journal
            rows = journal.scan_metrics_history(limit=5)
            check("journal row is a zero-token reuse",
                  (rows[0]["skipped"], rows[0]["cost_usd"]), (1, 0.0))
            check("reason classifies as reused, not skipped",
                  service._classify_scan_row(rows[0]), "reused")
        finally:
            service.STATE.clear()
            service.STATE.update(saved)
    _restore_db(old_db, db_path)
    _restore_shared(old_sh, sh_path)


def test_mirror_never_calls_even_on_force():
    print("\nmirror: force=True re-reads live's scan, it does not buy one:")
    from dashboard.web import service
    from dashboard.core import shared_cache
    old_db, db_path = _isolated_db()
    old_sh, sh_path = _isolated_shared()
    with mock.patch.dict(os.environ, {"PAPER_LLM_MODE": "mirror",
                                       "SHARED_SCAN_ENABLE": "1",
                                       "DASH_FIXED_MODE": "paper"}):
        saved = _setup_state([_score("AAA")], ["h"])
        try:
            shared_cache.write(shared_cache.SCAN_KEY, {
                "market_fp": "f" * 32, "macro_note": "m", "signals": [_sig("AAA")],
                "model": "m", "provider": "p", "environment": "live"})
            with _no_llm(), mock.patch("analyst.usage_log.log_usage"):
                status = service.refresh_llm(cap=200, force=True)
            check("manual refresh still mirrored", status.startswith("reused live scan"), True)
        finally:
            service.STATE.clear()
            service.STATE.update(saved)
    _restore_db(old_db, db_path)
    _restore_shared(old_sh, sh_path)


# ---- 4. pending trades keep working ------------------------------------------

def test_mirror_without_any_scan_still_places_pending_trades():
    print("\nmirror, no scan source at all: pending trades must still be placed:")
    from dashboard.web import service
    from dashboard.core import shared_cache, paper as paper_mod
    old_db, db_path = _isolated_db()
    old_sh, sh_path = _isolated_shared()
    with mock.patch.dict(os.environ, {"PAPER_LLM_MODE": "mirror",
                                       "SHARED_SCAN_ENABLE": "1",
                                       "DASH_FIXED_MODE": "paper"}):
        saved = _setup_state([_score("AAA", signal="BUY", direction="long", strength=5)],
                             ["h"])
        seen_states = []
        mirrored = []
        try:
            with _no_llm(), \
                 mock.patch.object(paper_mod, "place_from_state",
                                   side_effect=lambda st: seen_states.append(dict(st)) or []), \
                 mock.patch.object(service.broker, "mirror_new",
                                   side_effect=lambda: mirrored.append(1) or []):
                status = service.refresh_llm(cap=200, force=True)
            check("status explains the deterministic fallback",
                  "deterministic signals only" in status, True)
            check("placement ran anyway (this is the pending-trade guarantee)",
                  len(seen_states), 1)
            check("it saw NO llm signals, so evaluate_signal falls back to score.signal",
                  seen_states[0].get("llm"), {})
            check("executor mirroring ran too", len(mirrored), 1)
        finally:
            service.STATE.clear()
            service.STATE.update(saved)
    _restore_db(old_db, db_path)
    _restore_shared(old_sh, sh_path)


def test_mirror_falls_back_to_own_last_scan_when_live_is_silent():
    print("\nmirror, live stale: falls back to the last scan paper already applied:")
    from dashboard.web import service
    from dashboard.core import shared_cache, store, paper as paper_mod
    old_db, db_path = _isolated_db()
    old_sh, sh_path = _isolated_shared()
    with mock.patch.dict(os.environ, {"PAPER_LLM_MODE": "mirror",
                                       "SHARED_SCAN_ENABLE": "1",
                                       "DASH_FIXED_MODE": "paper"}):
        saved = _setup_state([_score("AAA")], ["h"])
        try:
            store.cache_set("last_board_scan", {"macro_note": "cached macro",
                                                "signals": [_sig("AAA", "BUY")]})
            store.cache_set("llm_scan_ts", time.time() - 3600)   # applied 1h ago
            with _no_llm(), \
                 mock.patch.object(paper_mod, "place_from_state", return_value=[]), \
                 mock.patch("analyst.usage_log.log_usage"):
                status = service.refresh_llm(cap=200, force=True)
            check("served the cached scan", status.startswith("reused last scan"), True)
            check("cached signals are live again",
                  service.STATE["llm"]["AAA"].action, "BUY")
            check("cached macro_note restored", service.STATE["macro_note"], "cached macro")
            from dashboard.core import journal
            rows = journal.scan_metrics_history(limit=5)
            check("cache-sourced reuse still classifies as reused",
                  service._classify_scan_row(rows[0]), "reused")
            check("cost stays zero", rows[0]["cost_usd"], 0.0)
        finally:
            service.STATE.clear()
            service.STATE.update(saved)
    _restore_db(old_db, db_path)
    _restore_shared(old_sh, sh_path)


# ---- 5. cadence --------------------------------------------------------------

def test_mirror_reapplies_when_live_publishes_something_newer():
    print("\nmirror cadence: re-apply on a NEWER live scan, not on our own delta:")
    from dashboard.web import service
    from dashboard.core import shared_cache, store, paper as paper_mod
    old_db, db_path = _isolated_db()
    old_sh, sh_path = _isolated_shared()
    with mock.patch.dict(os.environ, {"PAPER_LLM_MODE": "mirror",
                                       "SHARED_SCAN_ENABLE": "1",
                                       "DASH_FIXED_MODE": "paper"}):
        saved = _setup_state([_score("AAA")], ["h"])
        applied = []
        try:
            store.cache_set("llm_scan_fingerprint", "paper-fp-that-never-changes")
            store.cache_set("llm_scan_ts", time.time() - 600)
            store.cache_set("llm_scan_attempt_ts", time.time() - 600)
            shared_cache.write(shared_cache.SCAN_KEY, {
                "market_fp": "x" * 32, "macro_note": "newer macro",
                "signals": [_sig("AAA", "BUY")], "model": "m", "provider": "p",
                "environment": "live"})
            with _no_llm(), \
                 mock.patch.object(paper_mod, "place_from_state",
                                   side_effect=lambda st: applied.append(1) or []), \
                 mock.patch("analyst.usage_log.log_usage"):
                status = service.refresh_llm(cap=200)      # NOT forced
            check("own fingerprint unchanged, yet live's newer scan was applied",
                  status.startswith("reused live scan"), True)
            check("placement ran on the new scan", len(applied), 1)

            # same shared scan, already applied -> must NOT re-apply every tick
            store.cache_set("llm_scan_ts", time.time())
            store.cache_set("llm_scan_attempt_ts", time.time())
            with _no_llm(), \
                 mock.patch.object(paper_mod, "place_from_state",
                                   side_effect=AssertionError("must not re-apply")), \
                 mock.patch("analyst.usage_log.log_usage"):
                status2 = service.refresh_llm(cap=200)
            check("nothing newer -> skipped, no re-apply",
                  status2.startswith("skipped"), True)
        finally:
            service.STATE.clear()
            service.STATE.update(saved)
    _restore_db(old_db, db_path)
    _restore_shared(old_sh, sh_path)


def test_own_mode_is_untouched():
    print("\nown mode (default): paper's own LLM path is unchanged:")
    from dashboard.web import service
    from dashboard.core import shared_cache
    old_db, db_path = _isolated_db()
    old_sh, sh_path = _isolated_shared()
    with mock.patch.dict(os.environ, {"SHARED_SCAN_ENABLE": "1",
                                       "DASH_FIXED_MODE": "paper"}):
        os.environ.pop("PAPER_LLM_MODE", None)
        saved = _setup_state([_score("AAA")], ["h"])
        called = []

        def _fake_scan(scores, headlines, cap=200):
            called.append(1)
            return None, "budget guard (fake)"

        try:
            shared_cache.write(shared_cache.SCAN_KEY, {
                "market_fp": "f" * 32, "macro_note": "m", "signals": [_sig("AAA")],
                "model": "m", "provider": "p", "environment": "live"})
            with mock.patch.object(service, "run_board_scan", side_effect=_fake_scan):
                status = service.refresh_llm(cap=200, force=True)
            check("own scan still runs", len(called), 1)
            check("status from own scan", status, "budget guard (fake)")
        finally:
            service.STATE.clear()
            service.STATE.update(saved)
    _restore_db(old_db, db_path)
    _restore_shared(old_sh, sh_path)


def test_reuse_window_covers_lives_real_cadence():
    print("\nwindow sizing: the mirror window must cover live's actual gaps:")
    from dashboard.web import board_scan
    # measured 2026-10-02 over 30d of live scans: p50 16min, p90 31min, average
    # 201min, with quiet stretches up to 12h (and one 96h provider outage).
    check("mirror window is 12h", board_scan.SHARED_SCAN_MIRROR_MAX_AGE_MIN, 720)
    check("strict fingerprint window left at 30min for non-mirror reuse",
          board_scan.SHARED_SCAN_MAX_AGE_MIN, 30)


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