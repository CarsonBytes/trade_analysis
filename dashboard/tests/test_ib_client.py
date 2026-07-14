"""Unit tests for the PURE parts of data/ib_client.py -- account-summary row parsing.
No IB gateway needed (fake row objects stand in for accountSummaryAsync()'s rows). Run:
  uv run python -m dashboard.tests.test_ib_client
"""
from __future__ import annotations

from unittest import mock

from dashboard.data.ib_client import parse_account_summary_rows

_fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        _fails.append(name)


class FakeRow:
    """Stand-in for an ib_async AccountValue: .tag/.value/.currency/.account."""
    def __init__(self, tag, value, currency="USD", account="U1"):
        self.tag = tag
        self.value = value
        self.currency = currency
        self.account = account


def test_single_account_no_filter():
    print("single account, no target filter:")
    rows = [FakeRow("NetLiquidation", "1000.0"), FakeRow("TotalCashValue", "800.0")]
    out = parse_account_summary_rows(rows, target_acct=None)
    check("both tags parsed", out, {"NetLiquidation": 1000.0, "TotalCashValue": 800.0, "_ccy": "USD"})


def test_ignores_tags_outside_whitelist():
    print("unrecognised tags are dropped:")
    rows = [FakeRow("NetLiquidation", "1000.0"), FakeRow("SomeRandomTag", "999.0")]
    out = parse_account_summary_rows(rows, target_acct=None)
    check("only whitelisted tag kept", out, {"NetLiquidation": 1000.0, "_ccy": "USD"})


def test_bad_value_skipped():
    print("non-numeric value is skipped, not crashed on:")
    rows = [FakeRow("NetLiquidation", "not-a-number"), FakeRow("TotalCashValue", "500.0")]
    out = parse_account_summary_rows(rows, target_acct=None)
    check("bad row dropped, good row kept", out, {"TotalCashValue": 500.0, "_ccy": "USD"})


def test_empty_rows_returns_none():
    print("empty input -> None (not an empty dict):")
    check("no rows", parse_account_summary_rows([], target_acct=None), None)
    check("rows present but none match whitelist",
          parse_account_summary_rows([FakeRow("Foo", "1.0")], target_acct=None), None)


def test_two_managed_accounts_regression():
    print("regression 2026-07-10: second empty managed account must NOT clobber the real one:")
    # Reproduces the live bug exactly: the real account's rows arrive FIRST, then a second,
    # unrelated, all-zero account's rows arrive LAST in the same accountSummaryAsync() batch.
    # Without the target_acct filter, the zero rows silently overwrote the real ones.
    rows = [
        FakeRow("NetLiquidation", "10040.0", currency="HKD", account="U12991898"),
        FakeRow("TotalCashValue", "10040.0", currency="HKD", account="U12991898"),
        FakeRow("NetLiquidation", "0.0", currency="HKD", account="U20738951"),
        FakeRow("TotalCashValue", "0.0", currency="HKD", account="U20738951"),
    ]
    out = parse_account_summary_rows(rows, target_acct="U12991898")
    check("real account's values survive", out,
          {"NetLiquidation": 10040.0, "TotalCashValue": 10040.0, "_ccy": "HKD"})

    # And the inverse -- if the wrong account is targeted, we correctly get its (empty) data,
    # not a silent fallback to the other account.
    out2 = parse_account_summary_rows(rows, target_acct="U20738951")
    check("other account's values isolated", out2,
          {"NetLiquidation": 0.0, "TotalCashValue": 0.0, "_ccy": "HKD"})


def test_no_target_acct_includes_everything():
    print("target_acct=None means no filtering at all (back-compat / single-account case):")
    rows = [
        FakeRow("NetLiquidation", "100.0", account="A"),
        FakeRow("NetLiquidation", "200.0", account="B"),
    ]
    out = parse_account_summary_rows(rows, target_acct=None)
    # last row wins when nothing distinguishes them -- expected/documented when no target is given
    check("later row wins with no filter", out["NetLiquidation"], 200.0)


# ADDED 2026-07-14: account_summary()'s TTL cache -- found that once quant.carsonng.com went
# public, concurrent page loads each independently triggered a real IB Gateway round-trip
# (equity_usd()/portfolio_room_usd() -> account_summary() -> accountSummaryAsync()), and
# portfolio_room_usd() alone calls it twice internally -- so concurrent visitors multiplied
# real network round-trips serialized through one IB connection, compounding latency under
# load. This tests that a burst of calls within the TTL window makes only ONE real fetch.
def test_account_summary_caches_within_ttl():
    print("\naccount_summary(): a burst of calls within the TTL window makes only ONE real "
          "IB round-trip:")
    import dashboard.data.ib_client as ibc
    ibc._summary_cache["ts"] = 0.0
    ibc._summary_cache["data"] = None
    rows = [FakeRow("NetLiquidation", "1000.0"), FakeRow("TotalCashValue", "800.0")]
    run_calls = []

    def _fake_run(coro, timeout=None):
        run_calls.append(1)
        return rows

    with mock.patch.object(ibc, "_ensure_conn", return_value=mock.Mock()), \
         mock.patch.object(ibc, "call", return_value=["U1"]), \
         mock.patch.object(ibc, "_run", side_effect=_fake_run):
        r1 = ibc.account_summary()
        r2 = ibc.account_summary()
        r3 = ibc.account_summary()
    check("only one real fetch for 3 calls in a burst", len(run_calls), 1)
    check("all calls return the same (cached) data", r1 == r2 == r3, True)
    check("NetLiquidation present", r1["NetLiquidation"], 1000.0)


def test_account_summary_refetches_after_ttl_expires():
    print("\naccount_summary(): a call after the TTL window makes a fresh real fetch:")
    import dashboard.data.ib_client as ibc
    ibc._summary_cache["ts"] = 0.0
    ibc._summary_cache["data"] = None
    rows = [FakeRow("NetLiquidation", "1000.0")]
    run_calls = []

    def _fake_run(coro, timeout=None):
        run_calls.append(1)
        return rows

    with mock.patch.object(ibc, "_ensure_conn", return_value=mock.Mock()), \
         mock.patch.object(ibc, "call", return_value=["U1"]), \
         mock.patch.object(ibc, "_run", side_effect=_fake_run):
        ibc.account_summary()
        ibc._summary_cache["ts"] -= (ibc._SUMMARY_CACHE_SEC + 1)   # simulate TTL expiry
        ibc.account_summary()
    check("TTL expiry triggers a second real fetch", len(run_calls), 2)


def test_account_summary_not_connected_bypasses_cache():
    print("\naccount_summary(): not connected returns None without poisoning the cache:")
    import dashboard.data.ib_client as ibc
    ibc._summary_cache["ts"] = 0.0
    ibc._summary_cache["data"] = None
    with mock.patch.object(ibc, "_ensure_conn", return_value=None):
        r = ibc.account_summary()
    check("returns None when not connected", r, None)
    check("cache left empty (no false-positive caching of a down connection)",
          ibc._summary_cache["data"], None)


if __name__ == "__main__":
    for t in (test_single_account_no_filter, test_ignores_tags_outside_whitelist,
              test_bad_value_skipped, test_empty_rows_returns_none,
              test_two_managed_accounts_regression, test_no_target_acct_includes_everything,
              test_account_summary_caches_within_ttl,
              test_account_summary_refetches_after_ttl_expires,
              test_account_summary_not_connected_bypasses_cache):
        t()
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {_fails}")
        raise SystemExit(1)
    print("all tests passed.")
