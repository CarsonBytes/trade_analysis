"""QUANT_WEEKEND_DISPLAY_SPEC §3: the weekend api-off flag.

The flag stops broker connects and tick work while it's active. It must FAIL OPEN
(normal trading) when malformed or expired, so a failed wake can never keep LIVE off
silently. Run:
  uv run python -m dashboard.tests.test_api_mode
"""
from __future__ import annotations

from dashboard.tests import hermetic  # noqa: F401  (script-style runs)

import json
import os
import tempfile
import time
from unittest import mock

from dashboard.core import api_mode

_fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        _fails.append(name)
    assert ok, f"{name}: got {got!r} want {want!r}"


def _flag(until, since=None):
    return json.dumps({"reason": "weekend", "since": since or until - 3600, "until": until,
                       "by": "test"})


def test_evaluate_pure():
    print("evaluate():")
    now = 1_800_000_000
    check("absent -> on", api_mode.evaluate(None, now), (None, None))
    f, w = api_mode.evaluate(_flag(now + 3600), now)
    check("fresh -> off", (f is not None, w), (True, None))
    check("until parsed", f["until"], float(now + 3600))
    f, w = api_mode.evaluate(_flag(now - 3600), now)   # past until, inside the 2h grace
    check("inside grace -> still off", f is not None, True)
    f, w = api_mode.evaluate(_flag(now - api_mode.EXPIRY_GRACE_SEC), now)
    check("expired -> on", f, None)
    check("expired warns", "expired" in (w or ""), True)
    for bad in ("", "not json", "[]", json.dumps({"reason": "weekend"}),
                json.dumps({"until": "soon"})):
        f, w = api_mode.evaluate(bad, now)
        check(f"malformed {bad[:20]!r} -> on", f, None)
        check(f"malformed {bad[:20]!r} warns", "malformed" in (w or ""), True)


def _with_flag(text):
    d = tempfile.mkdtemp(prefix="apimode-")
    p = os.path.join(d, "api-off")
    if text is not None:
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
    return mock.patch.dict(os.environ, {"API_OFF_FLAG": p}), p


def test_api_off_reads_file_and_caches():
    print("api_off() reads the flag file:")
    ctx, p = _with_flag(_flag(time.time() + 3600))
    with ctx:
        api_mode.reset_cache()
        check("flag present -> off", api_mode.api_off() is not None, True)
        os.remove(p)
        check("cached for 10s", api_mode.api_off() is not None, True)
        api_mode.reset_cache()
        check("after cache reset -> on", api_mode.api_off(), None)
        check("describe() empty when on", api_mode.describe(), "")


def test_expired_flag_warns_once():
    print("an expired flag resumes trading and warns once:")
    ctx, _ = _with_flag(_flag(time.time() - 3 * 3600))
    with ctx, mock.patch("dashboard.core.notable_events.record") as rec:
        api_mode.reset_cache()
        check("expired -> on", api_mode.api_off(), None)
        api_mode._cache["t"] = -1e18           # force re-read, keep the warned set
        api_mode.api_off()
        check("one notable event", rec.call_count, 1)
        check("warning tier", rec.call_args.kwargs.get("level"), "warning")
    api_mode.reset_cache()


def test_default_path_is_beside_the_db():
    print("default flag path sits next to the journal DB:")
    with mock.patch.dict(os.environ, {"DASH_DB_NAME": "/data/dashboard_docker.db"}):
        os.environ.pop("API_OFF_FLAG", None)
        check("Docker path", api_mode.flag_path().as_posix().endswith("/data/api-off"), True)


def test_ib_client_never_connects_while_off():
    print("ib_client._ensure_conn() makes no connect attempt while off:")
    from dashboard.data import ib_client

    class FakeIB:
        made = 0

        def __init__(self):
            FakeIB.made += 1

    fake_mod = mock.Mock(IB=FakeIB)
    ctx, _ = _with_flag(_flag(time.time() + 3600))
    with ctx, mock.patch.object(ib_client, "_mod", return_value=fake_mod), \
            mock.patch.dict(ib_client._S, {"ib": None, "last_attempt": 0.0}):
        api_mode.reset_cache()
        check("returns None", ib_client._ensure_conn(), None)
        check("no IB() constructed", FakeIB.made, 0)

        class Conn:
            def __init__(self):
                self.up = True

            def isConnected(self):
                return self.up

            def disconnect(self):
                self.up = False
        c = Conn()
        ib_client._S["ib"] = c
        with mock.patch.object(ib_client, "call", side_effect=lambda fn, timeout=0: fn()):
            check("existing session -> None", ib_client._ensure_conn(), None)
        check("existing session disconnected", c.up, False)
        check("handle cleared", ib_client._S["ib"], None)
    api_mode.reset_cache()


def test_status_snapshot_reports_off():
    print("/status carries api_mode and accepts a cached snapshot while off:")
    from dashboard.web import fleet, service
    import datetime as dt
    ctx, _ = _with_flag(_flag(time.time() + 3600))
    with ctx, mock.patch.dict(service.STATE, {"account": {"NetLiquidation": 1000.0},
                                              "acct_ts": None,
                                              "last_tick_ts": dt.datetime.now()}):
        api_mode.reset_cache()
        snap = fleet.status_snapshot()
        check("api_mode off", snap["api_mode"], "off")
        check("until set", snap["api_off_until"] is not None, True)
        check("ok without a fresh account read", snap["ok"], True)
        check("html says weekend", "WEEKEND" in fleet.render_status_html(snap), True)
    api_mode.reset_cache()
    with mock.patch.dict(os.environ, {"API_OFF_FLAG": "/nonexistent/api-off"}), \
            mock.patch.dict(service.STATE, {"account": {"NetLiquidation": 1000.0},
                                            "acct_ts": None,
                                            "last_tick_ts": dt.datetime.now()}):
        api_mode.reset_cache()
        snap = fleet.status_snapshot()
        check("api_mode on", snap["api_mode"], "on")
        check("on + stale account -> not ok (unchanged)", snap["ok"], False)
    api_mode.reset_cache()


if __name__ == "__main__":
    for fn in [v for k, v in list(globals().items()) if k.startswith("test_")]:
        fn()
    print(f"\n{'ALL PASS' if not _fails else f'{len(_fails)} FAIL: {_fails}'}")
    raise SystemExit(1 if _fails else 0)
