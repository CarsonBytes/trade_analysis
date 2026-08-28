"""Unit tests for the PURE parts of data/ib_client.py -- account-summary row parsing.
No IB gateway needed (fake row objects stand in for accountSummaryAsync()'s rows). Run:
  uv run python -m dashboard.tests.test_ib_client
"""
from __future__ import annotations

from unittest import mock

from dashboard.data.ib_client import filter_by_account, parse_account_summary_rows

_fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        _fails.append(name)
    assert ok, f"{name}: got {got!r} want {want!r}"


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


class FakeItem:
    """Stand-in for an ib_async Position/PortfolioItem: just needs .account for this filter
    (the rest of each object -- .contract, .unrealizedPNL, .marketPrice -- is irrelevant to
    filter_by_account() itself, only to the caller)."""
    def __init__(self, account="U1", label=""):
        self.account = account
        self.label = label      # test-only, to identify which item survived filtering

    def __repr__(self):
        return f"FakeItem({self.label!r}, acct={self.account!r})"


# ADDED 2026-07-17: filter_by_account() -- FIXED after the LIVE dashboard's unrealized P&L
# showed exactly $0 for every open position. Root cause: ib.positions()/ib.portfolio() can
# return rows for MULTIPLE managed accounts under one login (the same ghost account,
# U20738951, already known from the 2026-07-10 accountSummaryAsync() fix above -- that fix
# only ever covered accountSummaryAsync(), not positions()/portfolio()), and the un-filtered
# {conId: item} dict comprehensions at every call site let whichever account's row came LAST
# silently win.
def test_filter_by_account_keeps_only_target():
    print("\nfilter_by_account(): keeps only the target account's items:")
    items = [FakeItem("U12991898", "real"), FakeItem("U20738951", "ghost")]
    out = filter_by_account(items, "U12991898")
    check("only the real account's item survives", [i.label for i in out], ["real"])


def test_filter_by_account_no_target_returns_everything():
    print("\nfilter_by_account(): no target_acct (None) -> no filtering, back-compat:")
    items = [FakeItem("U12991898", "a"), FakeItem("U20738951", "b")]
    out = filter_by_account(items, None)
    check("nothing filtered when target_acct is None", len(out), 2)


def test_filter_by_account_keeps_items_with_no_account_set():
    print("\nfilter_by_account(): an item with no .account set is kept (matches "
          "parse_account_summary_rows()'s same defensive behaviour, not over-filtered):")
    items = [FakeItem("", "unset"), FakeItem("U20738951", "ghost")]
    out = filter_by_account(items, "U12991898")
    check("unset-account item survives, ghost account filtered out",
          [i.label for i in out], ["unset"])


def test_filter_by_account_regression_ghost_account_no_longer_wins():
    print("\nfilter_by_account(): regression -- rebuilding a {conId: item} dict from "
          "filtered results, the ghost account can no longer clobber the real one's P&L:")

    class _Contract:
        def __init__(self, con_id):
            self.conId = con_id

    class _Portfolio(FakeItem):
        def __init__(self, account, con_id, unrealized_pnl):
            super().__init__(account, label=f"{account}:{con_id}")
            self.contract = _Contract(con_id)
            self.unrealizedPNL = unrealized_pnl

    # ghost account's zero-P&L row processed AFTER the real one -- exactly the ordering
    # that silently won before this fix.
    raw = [_Portfolio("U12991898", 12345, 987.65), _Portfolio("U20738951", 12345, 0.0)]
    filtered = filter_by_account(raw, "U12991898")
    by_conid = {i.contract.conId: i for i in filtered}
    check("real account's non-zero unrealizedPNL survives", by_conid[12345].unrealizedPNL, 987.65)


def test_stock_contract_primary_exchange_kwarg():
    print("stock_contract(): ADDED 2026-08-19 (UCITS instrument swap) -- primary_exchange "
          "threads through to Stock(primaryExchange=...) when given, and is omitted entirely "
          "(unchanged SMART-only behavior) for every pre-existing caller that doesn't pass it:")
    from dashboard.data import ib_client
    captured = {}

    class FakeStock:
        def __init__(self, symbol, exchange, currency, **kwargs):
            captured["args"] = (symbol, exchange, currency, kwargs)

    fake_mod = mock.MagicMock()
    fake_mod.Stock = FakeStock

    with mock.patch.object(ib_client, "_mod", return_value=fake_mod), \
         mock.patch.object(ib_client, "_ensure_conn", return_value=mock.MagicMock()), \
         mock.patch.object(ib_client, "_run", return_value=None):
        ib_client.stock_contract("CSPX", primary_exchange="LSEETF")
    check("primaryExchange kwarg passed when given",
          captured["args"], ("CSPX", "SMART", "USD", {"primaryExchange": "LSEETF"}))

    captured.clear()
    with mock.patch.object(ib_client, "_mod", return_value=fake_mod), \
         mock.patch.object(ib_client, "_ensure_conn", return_value=mock.MagicMock()), \
         mock.patch.object(ib_client, "_run", return_value=None):
        ib_client.stock_contract("GLD")
    check("no primaryExchange kwarg at all when omitted (backward compat)",
          captured["args"], ("GLD", "SMART", "USD", {}))


class _AV:
    """Minimal stand-in for ib_async's AccountValue rows."""
    def __init__(self, tag, value, currency):
        self.tag, self.value, self.currency = tag, value, currency


def test_fx_rate_from_account_derives_usd_per_ccy():
    print("\nfx_rate_from_account(): ADDED 2026-08-28 -- IBKR's own ExchangeRate rows give "
          "units of BASE per 1 CCY, so USD per 1 CCY is rate(ccy)/rate(USD). Real values "
          "from the live HKD-base account (U12991898): HKD 1.00, USD 7.8395142:")
    from dashboard.data import ib_client

    rows = [_AV("ExchangeRate", "1.00", "BASE"), _AV("ExchangeRate", "1.00", "HKD"),
            _AV("ExchangeRate", "7.8395142", "USD"), _AV("NetLiquidation", "222692.91", "HKD")]

    with mock.patch.object(ib_client, "call", side_effect=lambda fn, **kw: rows), \
         mock.patch.object(ib_client, "account_id", return_value="U12991898"):
        rate = ib_client.fx_rate_from_account(mock.MagicMock(), "HKD")

    check("USD per HKD = rate(HKD)/rate(USD)", round(rate, 7), round(1.0 / 7.8395142, 7))
    check("differs from the hardcoded 1/7.80 peg", round(rate, 7) == round(1.0 / 7.80, 7), False)


def test_fx_rate_from_account_returns_none_when_rate_missing():
    print("fx_rate_from_account(): no usable ExchangeRate rows -> None, so fx_to_usd falls "
          "through to its other routes rather than returning a wrong number:")
    from dashboard.data import ib_client

    with mock.patch.object(ib_client, "call", side_effect=lambda fn, **kw: [
            _AV("NetLiquidation", "1000", "HKD")]), \
         mock.patch.object(ib_client, "account_id", return_value="U1"):
        check("no ExchangeRate rows -> None",
              ib_client.fx_rate_from_account(mock.MagicMock(), "HKD"), None)

    with mock.patch.object(ib_client, "call", side_effect=RuntimeError("no loop")), \
         mock.patch.object(ib_client, "account_id", return_value="U1"):
        check("a raising broker read -> None, never propagates",
              ib_client.fx_rate_from_account(mock.MagicMock(), "HKD"), None)


def test_fx_rate_from_account_never_resolves_account_id_itself():
    print("fx_rate_from_account(): REGRESSION -- it must NOT call account_id() internally. "
          "account_id() takes _LOCK (non-re-entrant) and marshals onto the IB loop thread, "
          "so looking it up inside the lambda -- which already runs ON that thread, under a "
          "lock fx_to_usd holds -- deadlocked until timeout and returned None on every live "
          "call, silently falling through to the pegged constant:")
    from dashboard.data import ib_client

    rows = [_AV("ExchangeRate", "1.00", "HKD"), _AV("ExchangeRate", "7.8395142", "USD")]
    seen = {"acct_arg": None}

    def _fake_call(fn, **kw):
        return rows

    with mock.patch.object(ib_client, "call", side_effect=_fake_call), \
         mock.patch.object(ib_client, "account_id",
                           side_effect=AssertionError(
                               "account_id() must be resolved by the CALLER, not in here")):
        rate = ib_client.fx_rate_from_account(mock.MagicMock(), "HKD", "U12991898")
    check("computed the rate without ever calling account_id()",
          round(rate, 7), round(1.0 / 7.8395142, 7))

    # and fx_to_usd must resolve it OUTSIDE the lock, before calling the helper
    def _capture(ib, ccy, acct=None):
        seen["acct_arg"] = acct
        return 1.0 / 7.8395142

    with mock.patch.object(ib_client, "_ensure_conn", return_value=mock.MagicMock()), \
         mock.patch.object(ib_client, "account_id", return_value="U12991898"), \
         mock.patch.object(ib_client, "fx_rate_from_account", side_effect=_capture):
        ib_client.fx_to_usd("HKD")
    check("fx_to_usd passes the resolved account through", seen["acct_arg"], "U12991898")


def test_fx_to_usd_prefers_account_rate_over_historical_bars():
    print("fx_to_usd(): REGRESSION for the 2026-08-28 error-162 incident -- when the same "
          "IBKR login is active from another IP, EVERY reqHistoricalData FX call fails with "
          "'Trading TWS session is connected from a different IP address', so this used to "
          "fall all the way through to the pegged constant. The account's own ExchangeRate "
          "needs no market-data entitlement and must be preferred, with NO bar request made:")
    from dashboard.data import ib_client

    requested = []

    class FakeForex:
        def __init__(self, symbol):
            requested.append(symbol)

    fake_mod = mock.MagicMock()
    fake_mod.Forex = FakeForex

    with mock.patch.object(ib_client, "_mod", return_value=fake_mod), \
         mock.patch.object(ib_client, "_ensure_conn", return_value=mock.MagicMock()), \
         mock.patch.object(ib_client, "fx_rate_from_account", return_value=1.0 / 7.8395142), \
         mock.patch.object(ib_client, "_run",
                           side_effect=AssertionError("must NOT request FX bars")):
        rate = ib_client.fx_to_usd("HKD")

    check("used the account rate", round(rate, 7), round(1.0 / 7.8395142, 7))
    check("no Forex contract constructed at all", requested, [])

    # and when the account route yields nothing, the bar route still runs (error-162 case
    # resolved, or a currency with no ExchangeRate row) -- then the peg as the last resort
    class _Bar:
        def __init__(self, close):
            self.close = close

    with mock.patch.object(ib_client, "_mod", return_value=fake_mod), \
         mock.patch.object(ib_client, "_ensure_conn", return_value=mock.MagicMock()), \
         mock.patch.object(ib_client, "fx_rate_from_account", return_value=None), \
         mock.patch.object(ib_client, "_run", side_effect=lambda *a, **k: [_Bar(7.80)]):
        rate2 = ib_client.fx_to_usd("HKD")
    check("falls back to the bar route when the account rate is unavailable",
          round(rate2, 6), round(1.0 / 7.80, 6))

    with mock.patch.object(ib_client, "_mod", return_value=fake_mod), \
         mock.patch.object(ib_client, "_ensure_conn", return_value=mock.MagicMock()), \
         mock.patch.object(ib_client, "fx_rate_from_account", return_value=None), \
         mock.patch.object(ib_client, "_run", side_effect=Exception("Error 162")):
        rate3 = ib_client.fx_to_usd("HKD")
    check("both broker routes failing -> pegged constant (never None for HKD)",
          round(rate3, 6), round(1.0 / 7.80, 6))


def test_fx_to_usd_uses_usd_base_first_for_hkd():
    print("\nfx_to_usd(): HKD is in _INVERTED_FX -- must try USDHKD first (correct), "
          "not HKDUSD (which doesn't exist on IBKR and generated error 200 on every "
          "call). FIXED 2026-08-27:")
    from dashboard.data import ib_client

    requested = []

    class FakeForex:
        def __init__(self, symbol):
            requested.append(symbol)
            self.symbol = symbol

    fake_mod = mock.MagicMock()
    fake_mod.Forex = FakeForex

    class _Bar:
        def __init__(self, close):
            self.close = close

    def _run_side_effect(coro, timeout=8):
        return [_Bar(7.80)]                    # USDHKD close: 7.80 HKD per 1 USD

    with mock.patch.object(ib_client, "_mod", return_value=fake_mod), \
         mock.patch.object(ib_client, "_ensure_conn", return_value=mock.MagicMock()), \
         mock.patch.object(ib_client, "_run", side_effect=_run_side_effect):
        rate = ib_client.fx_to_usd("HKD")

    check("tried USD-base pair first (not HKDUSD)",
          requested, ["USDHKD"])
    check("inverted USDHKD's close (HKD per USD) to USD per HKD",
          round(rate, 6), round(1.0 / 7.80, 6))


def test_fx_to_usd_uses_ccy_base_pair_directly_when_it_exists():
    print("fx_to_usd(): a currency whose CCY-base pair DOES exist (e.g. EUR -> EURUSD, the "
          "IBKR-standard convention for majors) must use that close directly, not invert:")
    from dashboard.data import ib_client

    requested = []

    class FakeForex:
        def __init__(self, symbol):
            requested.append(symbol)

    fake_mod = mock.MagicMock()
    fake_mod.Forex = FakeForex

    class _Bar:
        def __init__(self, close):
            self.close = close

    with mock.patch.object(ib_client, "_mod", return_value=fake_mod), \
         mock.patch.object(ib_client, "_ensure_conn", return_value=mock.MagicMock()), \
         mock.patch.object(ib_client, "_run", return_value=[_Bar(1.08)]):
        rate = ib_client.fx_to_usd("EUR")

    check("only the CCY-base pair was requested, first try succeeded", requested, ["EURUSD"])
    check("close used directly, no inversion", rate, 1.08)


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
