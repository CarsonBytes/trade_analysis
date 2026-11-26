"""Regression tests for dashboard/tests/hermetic.py -- the guard that keeps test runs off the
real journal.

FOUND 2026-11-25: hermetic.apply() kept any PRE-SET DASH_DB_NAME. Inside the paper container
that variable is /data/dashboard_docker.db -- the real journal -- so a test run there wrote a
fake EEM bracket (paper_id 27, perm_id 0, con_id 0) into ib_mirror as OPEN. Reconcile reported
it as a broker ghost from 2026-10-01 until it was found, paging Telegram daily from 2026-11-22.

Each case runs in a fresh interpreter: hermetic applies itself on import, so the parent
process (already hermetic) can't observe the "pre-set by docker-compose" starting state.
Run:  uv run python -m dashboard.tests.test_hermetic
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing

_fails = []
_ROOT = str(pathlib.Path(__file__).resolve().parents[2])


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        _fails.append(name)
    assert ok, f"{name}: got {got!r} want {want!r}"


def _run(code: str, dash_db_name: str) -> list[str]:
    env = dict(os.environ, DASH_DB_NAME=dash_db_name, PYTHONPATH=_ROOT)
    out = subprocess.run([sys.executable, "-c", code], env=env, cwd=_ROOT,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip().splitlines()


def _fake_prod_db() -> tuple[str, str]:
    """A stand-in for /data/dashboard_docker.db: an ib_mirror table with one real row."""
    fd, path = tempfile.mkstemp(prefix="quant_fake_prod_", suffix=".db")
    os.close(fd)
    with closing(sqlite3.connect(path)) as c, c:
        c.execute("CREATE TABLE ib_mirror (paper_id INTEGER UNIQUE, perm_id INTEGER, "
                  "con_id INTEGER, local_symbol TEXT, qty REAL, risk_money REAL, expiry TEXT, "
                  "ts TEXT, status TEXT, note TEXT)")
        c.execute("INSERT INTO ib_mirror VALUES (176,1,4215230,'XLK',192,1437.65,'',"
                  "'2026-11-24T16:46:28+00:00','CLOSED','sleeve')")
    return path, hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def test_preset_dash_db_name_is_redirected():
    print("\nhermetic: a PRE-SET DASH_DB_NAME (docker-compose's real journal) is replaced by a "
          "throwaway temp DB, not kept:")
    prod, _ = _fake_prod_db()
    try:
        lines = _run("from dashboard.tests import hermetic\n"
                     "import os\n"
                     "first = os.environ['DASH_DB_NAME']\n"
                     "hermetic.apply()\n"               # conftest applies a second time
                     "print(first)\n"
                     "print(os.environ['DASH_DB_NAME'])\n", prod)
        check("DASH_DB_NAME no longer points at the real journal", lines[0] != prod, True)
        check("it points at a hermetic temp DB", "quant_test_dash_" in lines[0], True)
        check("apply() is idempotent (no second temp DB on re-apply)", lines[1], lines[0])
    finally:
        os.remove(prod)


def test_mirror_write_under_test_never_reaches_real_journal():
    print("\nhermetic: an ib_mirror INSERT made under test (what the S2 bracket test did on "
          "2026-10-01) leaves the real journal byte-identical:")
    prod, sha_before = _fake_prod_db()
    try:
        lines = _run("from dashboard.tests import hermetic  # noqa: F401\n"
                     "from dashboard.execution import ib_exec\n"
                     "from dashboard.core import paper\n"
                     "with ib_exec._conn() as c:\n"
                     "    c.execute(\"INSERT OR IGNORE INTO ib_mirror VALUES "
                     "(27,0,0,'EEM',149,500.0,'','2026-10-01T13:57:34+00:00','OPEN','etf')\")\n"
                     "print(paper._DB)\n"
                     "print(sorted(ib_exec.mirrored_open_symbols()))\n", prod)
        check("the write went to a temp DB, not the real one", str(lines[0]) != prod, True)
        check("the test-side DB sees its own fake row", lines[1], "['EEM']")
        sha_after = hashlib.sha256(pathlib.Path(prod).read_bytes()).hexdigest()
        check("real journal byte-identical", sha_after, sha_before)
        with closing(sqlite3.connect(prod)) as c:
            open_rows = c.execute("SELECT COUNT(*) FROM ib_mirror WHERE status='OPEN'").fetchone()
        check("no OPEN ghost row in the real journal", open_rows[0], 0)
    finally:
        os.remove(prod)


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
