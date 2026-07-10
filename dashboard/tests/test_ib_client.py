"""Unit tests for the PURE parts of data/ib_client.py -- account-summary row parsing.
No IB gateway needed (fake row objects stand in for accountSummaryAsync()'s rows). Run:
  uv run python -m dashboard.tests.test_ib_client
"""
from __future__ import annotations

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


if __name__ == "__main__":
    for t in (test_single_account_no_filter, test_ignores_tags_outside_whitelist,
              test_bad_value_skipped, test_empty_rows_returns_none,
              test_two_managed_accounts_regression, test_no_target_acct_includes_everything):
        t()
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {_fails}")
        raise SystemExit(1)
    print("all tests passed.")
