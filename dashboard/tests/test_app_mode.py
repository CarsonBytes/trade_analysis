"""Unit tests for core/mode.py's resolve_mode() -- extracted from app.py (2026-08-13) so this
has an actual regression test at all (app.py itself can't be imported in a test, `ui.run()` at
module level blocks). Covers the exact silent-data-loss bug this session found and fixed: an
explicitly pre-set DASH_DB_NAME (as docker-compose.yml sets for the WSL2/Docker deployment) must
survive resolve_mode(), not get unconditionally stomped back to the native relative default.
Run:  uv run python -m dashboard.tests.test_app_mode
"""
from __future__ import annotations

import os

_fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        _fails.append(name)
    assert ok, f"{name}: got {got!r} want {want!r}"


def _isolated_env(**overrides):
    """Snapshot + restore the handful of env vars resolve_mode() touches, so tests never leak
    state into each other or into the rest of the suite."""
    keys = ("DASH_FIXED_MODE", "DASH_DB_NAME", "IB_PORT", "IB_ACCOUNT", "IB_ALLOW_LIVE",
            "LIVE_IB_PORT", "LIVE_IB_ACCOUNT")
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    os.environ.update(overrides)
    return saved


def _restore_env(saved):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_resolve_mode_preserves_explicit_dash_db_name_paper():
    print("resolve_mode(): REGRESSION for the 2026-08-13 bug -- an explicitly pre-set "
          "DASH_DB_NAME (as docker-compose.yml sets) must survive the paper branch, not get "
          "silently overwritten to the native relative default 'dashboard.db':")
    from dashboard.core.mode import resolve_mode

    saved = _isolated_env(DASH_FIXED_MODE="paper", DASH_DB_NAME="/data/dashboard_docker.db")
    try:
        mode = resolve_mode()
        check("mode resolved to paper", mode, "paper")
        check("explicit DASH_DB_NAME preserved, NOT stomped to the native default",
              os.environ.get("DASH_DB_NAME"), "/data/dashboard_docker.db")
        check("IB_ALLOW_LIVE not set for paper", os.environ.get("IB_ALLOW_LIVE"), None)
    finally:
        _restore_env(saved)


def test_resolve_mode_preserves_explicit_dash_db_name_live():
    print("\nresolve_mode(): same regression, LIVE branch -- a future live Docker deployment "
          "setting its own DASH_DB_NAME must not get stomped to 'dashboard_live.db' either:")
    from dashboard.core.mode import resolve_mode

    saved = _isolated_env(DASH_FIXED_MODE="live", DASH_DB_NAME="/data/dashboard_docker_live.db")
    try:
        mode = resolve_mode()
        check("mode resolved to live", mode, "live")
        check("explicit DASH_DB_NAME preserved, NOT stomped to the native default",
              os.environ.get("DASH_DB_NAME"), "/data/dashboard_docker_live.db")
        check("IB_ALLOW_LIVE armed for live", os.environ.get("IB_ALLOW_LIVE"), "1")
    finally:
        _restore_env(saved)


def test_resolve_mode_default_unchanged_when_dash_db_name_unset_paper():
    print("\nresolve_mode(): native-deployment behaviour UNCHANGED -- when DASH_DB_NAME isn't "
          "already set (dashboard.ps1 never sets it), paper mode still defaults to "
          "'dashboard.db' exactly as before this fix:")
    from dashboard.core.mode import resolve_mode

    saved = _isolated_env(DASH_FIXED_MODE="paper")
    try:
        resolve_mode()
        check("defaults to the native paper journal", os.environ.get("DASH_DB_NAME"),
              "dashboard.db")
    finally:
        _restore_env(saved)


def test_resolve_mode_default_unchanged_when_dash_db_name_unset_live():
    print("\nresolve_mode(): native-deployment behaviour UNCHANGED -- live mode still "
          "defaults to 'dashboard_live.db' and arms IB_ALLOW_LIVE/IB_PORT/IB_ACCOUNT exactly "
          "as before this fix, when nothing was pre-set:")
    from dashboard.core.mode import resolve_mode

    saved = _isolated_env(DASH_FIXED_MODE="live")
    try:
        mode = resolve_mode()
        check("mode resolved to live", mode, "live")
        check("defaults to the native live journal", os.environ.get("DASH_DB_NAME"),
              "dashboard_live.db")
        check("IB_PORT defaults to the live port", os.environ.get("IB_PORT"), "4001")
        check("IB_ACCOUNT defaults to the live account", os.environ.get("IB_ACCOUNT"),
              "U12991898")
        check("IB_ALLOW_LIVE armed", os.environ.get("IB_ALLOW_LIVE"), "1")
    finally:
        _restore_env(saved)


def test_resolve_mode_defaults_to_paper_when_unset():
    print("\nresolve_mode(): with no DASH_FIXED_MODE and no stored mode pointer, defaults to "
          "'paper' (the safe default):")
    from dashboard.core.mode import resolve_mode
    from unittest import mock

    saved = _isolated_env()
    try:
        with mock.patch("dashboard.core.mode.store.get_mode", return_value=None):
            mode = resolve_mode()
        check("defaults to paper", mode, "paper")
    finally:
        _restore_env(saved)


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
