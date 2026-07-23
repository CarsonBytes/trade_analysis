"""Unit tests for analyst/usage_log.py's cross-project shared-usage counter --
ADDED 2026-07-15. No live Supabase needed (httpx.get is mocked).
Run:  uv run python -m dashboard.tests.test_usage_log
"""
from __future__ import annotations

import datetime as dt
from unittest import mock

_fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        _fails.append(name)


class _FakeResp:
    def __init__(self, rows):
        self._rows = rows

    def raise_for_status(self):
        pass

    def json(self):
        return self._rows


def _reset_cache():
    from analyst import usage_log
    usage_log._shared_usage_cache["ts"] = 0.0
    usage_log._shared_usage_cache["data"] = None


def test_hkt_boundary_is_exactly_midnight_in_hkt():
    print("_hkt_today_start_utc(): returns the instant of the most recent HKT midnight:")
    from analyst.usage_log import HKT, _hkt_today_start_utc
    boundary = _hkt_today_start_utc()
    check("naive (no tzinfo), to compare against naive utcnow()-stamped rows",
          boundary.tzinfo, None)
    as_hkt = boundary.replace(tzinfo=dt.timezone.utc).astimezone(HKT)
    check("is midnight when reinterpreted in HKT",
          (as_hkt.hour, as_hkt.minute, as_hkt.second, as_hkt.microsecond), (0, 0, 0, 0))


def test_hkt_boundary_is_16_00_utc_and_within_the_last_24h():
    print("\n_hkt_today_start_utc(): HKT has no DST, so HKT midnight is always 16:00 UTC "
          "(of the same or previous UTC calendar day):")
    from analyst.usage_log import _hkt_today_start_utc
    boundary = _hkt_today_start_utc()
    check("boundary hour is 16 (UTC)", boundary.hour, 16)
    now = dt.datetime.utcnow()
    check("boundary is not in the future", boundary <= now, True)
    check("boundary is within the last 24h", now - boundary < dt.timedelta(hours=24), True)


def test_fetch_uses_hkt_boundary():
    print("\nfetch_shared_usage_today(): queries the ledger using the HKT day boundary, "
          "not a UTC one (this is the 2026-07-24 fix -- was drifted stale vs event-radar's "
          "own already-HKT-fixed implementation):")
    from analyst import usage_log
    _reset_cache()
    captured = {}

    def _fake_get(*a, **k):
        captured["params"] = k.get("params", {})
        return _FakeResp([])

    with mock.patch.object(usage_log, "SUPABASE_URL", "https://fake.supabase.co"), \
         mock.patch.object(usage_log, "SUPABASE_SERVICE_ROLE_KEY", "fake-key"), \
         mock.patch("httpx.get", side_effect=_fake_get):
        usage_log.fetch_shared_usage_today()
    expected = usage_log._hkt_today_start_utc().isoformat() + "Z"
    check("created_at filter uses the HKT-midnight boundary",
          captured["params"]["created_at"], f"gte.{expected}")


def test_project_of_prefixes():
    print("_project_of(): tags rows by their purpose prefix:")
    from analyst.usage_log import _project_of
    check("events: prefix -> events", _project_of("events:rerank"), "events")
    check("quant: prefix -> quant", _project_of("quant:board_scan"), "quant")
    check("unprefixed -> study (legacy convention)", _project_of("explain_wrong_answer"), "study")
    check("empty string -> study", _project_of(""), "study")


def test_fetch_returns_zeros_when_not_configured():
    print("\nfetch_shared_usage_today(): Supabase not configured -> zeros, no crash:")
    from analyst import usage_log
    _reset_cache()
    with mock.patch.object(usage_log, "SUPABASE_URL", ""), \
         mock.patch.object(usage_log, "SUPABASE_SERVICE_ROLE_KEY", ""):
        r = usage_log.fetch_shared_usage_today()
    check("calls is 0", r["calls"], 0)
    check("cost_usd is 0.0", r["cost_usd"], 0.0)
    check("calls_by_project is empty", r["calls_by_project"], {})
    check("ok is False -- distinguishes 'couldn't check' from 'genuinely 0 calls'",
          r["ok"], False)


def test_fetch_aggregates_by_project():
    print("\nfetch_shared_usage_today(): aggregates rows into per-project counts + total cost:")
    from analyst import usage_log
    _reset_cache()
    rows = [
        {"purpose": "quant:board_scan", "cost_usd": 0.01, "created_at": "2026-07-15T01:00:00Z"},
        {"purpose": "quant:board_scan", "cost_usd": 0.01, "created_at": "2026-07-15T02:00:00Z"},
        {"purpose": "events:rerank", "cost_usd": 0.002, "created_at": "2026-07-15T03:00:00Z"},
        {"purpose": "explain_wrong_answer", "cost_usd": 0.0001, "created_at": "2026-07-15T04:00:00Z"},
    ]
    with mock.patch.object(usage_log, "SUPABASE_URL", "https://fake.supabase.co"), \
         mock.patch.object(usage_log, "SUPABASE_SERVICE_ROLE_KEY", "fake-key"), \
         mock.patch("httpx.get", return_value=_FakeResp(rows)):
        r = usage_log.fetch_shared_usage_today()
    check("total calls", r["calls"], 4)
    check("calls_by_project quant", r["calls_by_project"]["quant"], 2)
    check("calls_by_project events", r["calls_by_project"]["events"], 1)
    check("calls_by_project study", r["calls_by_project"]["study"], 1)
    check("total cost summed", round(r["cost_usd"], 4), round(0.01 + 0.01 + 0.002 + 0.0001, 4))
    check("ok is True on a successful fetch", r["ok"], True)


def test_fetch_caches_within_ttl():
    print("\nfetch_shared_usage_today(): a burst of calls within the TTL window makes only "
          "ONE real Supabase request:")
    from analyst import usage_log
    _reset_cache()
    calls = []

    def _fake_get(*a, **k):
        calls.append(1)
        return _FakeResp([{"purpose": "quant:x", "cost_usd": 0.0, "created_at": "t"}])

    with mock.patch.object(usage_log, "SUPABASE_URL", "https://fake.supabase.co"), \
         mock.patch.object(usage_log, "SUPABASE_SERVICE_ROLE_KEY", "fake-key"), \
         mock.patch("httpx.get", side_effect=_fake_get):
        r1 = usage_log.fetch_shared_usage_today()
        r2 = usage_log.fetch_shared_usage_today()
        r3 = usage_log.fetch_shared_usage_today()
    check("only one real HTTP request for 3 calls in a burst", len(calls), 1)
    check("all calls return the same (cached) data", r1 == r2 == r3, True)


def test_fetch_handles_request_failure_gracefully():
    print("\nfetch_shared_usage_today(): Supabase unreachable -> zeros, never raises:")
    from analyst import usage_log
    _reset_cache()
    raised = False
    with mock.patch.object(usage_log, "SUPABASE_URL", "https://fake.supabase.co"), \
         mock.patch.object(usage_log, "SUPABASE_SERVICE_ROLE_KEY", "fake-key"), \
         mock.patch("httpx.get", side_effect=ConnectionError("network down")):
        try:
            r = usage_log.fetch_shared_usage_today()
        except Exception:
            raised = True
    check("does not raise", raised, False)
    check("returns zeros on failure", r,
          {"calls": 0, "cost_usd": 0.0, "calls_by_project": {}, "ok": False})


# ADDED 2026-07-15: shared_calls_ok() -- fails CLOSED (treats "couldn't reach the ledger"
# as "not safe to call"), the same conservative direction as store.py's can_call(). The
# opposite choice (treating an unreachable ledger as "0 calls, all clear") is exactly the
# kind of gap that let the 2026-07-14 incident happen.
def test_shared_calls_ok_true_when_under_cap():
    print("\nshared_calls_ok(): under cap and reachable -> (True, calls):")
    from analyst import usage_log
    _reset_cache()
    rows = [{"purpose": "quant:x", "cost_usd": 0.0, "created_at": "t"}] * 5
    with mock.patch.object(usage_log, "SUPABASE_URL", "https://fake.supabase.co"), \
         mock.patch.object(usage_log, "SUPABASE_SERVICE_ROLE_KEY", "fake-key"), \
         mock.patch("httpx.get", return_value=_FakeResp(rows)):
        ok, calls = usage_log.shared_calls_ok(cap=200, reserve=10)
    check("ok is True", ok, True)
    check("calls reported", calls, 5)


def test_shared_calls_ok_false_when_near_cap():
    print("\nshared_calls_ok(): within the reserve of the cap -> (False, calls):")
    from analyst import usage_log
    _reset_cache()
    rows = [{"purpose": "quant:x", "cost_usd": 0.0, "created_at": "t"}] * 195
    with mock.patch.object(usage_log, "SUPABASE_URL", "https://fake.supabase.co"), \
         mock.patch.object(usage_log, "SUPABASE_SERVICE_ROLE_KEY", "fake-key"), \
         mock.patch("httpx.get", return_value=_FakeResp(rows)):
        ok, calls = usage_log.shared_calls_ok(cap=200, reserve=10)
    check("ok is False (195 >= 200-10)", ok, False)
    check("calls still reported", calls, 195)


def test_shared_calls_ok_fails_closed_when_unreachable():
    print("\nshared_calls_ok(): ledger unreachable -> (False, None), NOT (True, 0) -- "
          "fails closed rather than assuming 'all clear':")
    from analyst import usage_log
    _reset_cache()
    with mock.patch.object(usage_log, "SUPABASE_URL", "https://fake.supabase.co"), \
         mock.patch.object(usage_log, "SUPABASE_SERVICE_ROLE_KEY", "fake-key"), \
         mock.patch("httpx.get", side_effect=ConnectionError("network down")):
        ok, calls = usage_log.shared_calls_ok()
    check("ok is False on unreachable ledger", ok, False)
    check("calls is None (distinguishes from a real 0)", calls, None)


if __name__ == "__main__":
    test_hkt_boundary_is_exactly_midnight_in_hkt()
    test_hkt_boundary_is_16_00_utc_and_within_the_last_24h()
    test_fetch_uses_hkt_boundary()
    test_project_of_prefixes()
    test_fetch_returns_zeros_when_not_configured()
    test_fetch_aggregates_by_project()
    test_fetch_caches_within_ttl()
    test_fetch_handles_request_failure_gracefully()
    test_shared_calls_ok_true_when_under_cap()
    test_shared_calls_ok_false_when_near_cap()
    test_shared_calls_ok_fails_closed_when_unreachable()
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {_fails}")
        raise SystemExit(1)
    print("all tests passed.")
