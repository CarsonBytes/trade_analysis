"""Cash shield I3 (STRATEGY_IMPROVEMENT_SPEC, 2026-11-21): configurable shield instrument
(SGOV -> IB01, an accumulating Irish UCITS T-bill fund with no US withholding), migration out of
a legacy shield, and anti-churn (5%-of-NAV band + at most one rebalance a week).

Why (RETRO_PERFORMANCE_2026-11-21 §5): SGOV netted HKD +99 on ~HKD 200k Jul-Nov 2026 -- HKD 241
lost to 30% US withholding and HKD 429 to commissions over 55 trades.
No network, no broker (fakes only).
Run:  uv run python -m dashboard.tests.test_cash_shield
"""
from __future__ import annotations

from dashboard.tests import hermetic  # noqa: F401  (before any app module)

import os
import datetime as dt
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

_fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        _fails.append(name)
    assert ok, f"{name}: got {got!r} want {want!r}"


def test_shield_config():
    print("shield_symbol()/shield_exchange()/shield_symbols():")
    from dashboard.execution import ib_exec as x
    with mock.patch.dict(os.environ, {"CASH_SHIELD_SYMBOL": ""}):
        check("default stays SGOV (no behaviour change until configured)", x.shield_symbol(), "SGOV")
        check("SGOV is US-listed", x.shield_exchange(), "")
    with mock.patch.dict(os.environ, {"CASH_SHIELD_SYMBOL": "ib01"}):
        check("IB01 configured (case-insensitive)", x.shield_symbol(), "IB01")
        check("IB01 trades on LSEETF", x.shield_exchange(), "LSEETF")
        check("legacy SGOV is still treated as shield during migration", x.shield_symbols(),
              {"IB01", "SGOV"})


def test_band_and_cadence():
    print("\nsweep_band_usd() / sweep_cadence_ok():")
    from dashboard.execution import ib_exec as x
    check("small account: the USD floor", x.sweep_band_usd(37_000), 2500.0)
    check("bigger account: 5% of NAV", x.sweep_band_usd(200_000), 10_000.0)
    now = 1_800_000_000
    check("never traded -> ok", x.sweep_cadence_ok(None, now), True)
    check("3 days after the last rebalance -> wait", x.sweep_cadence_ok(now - 3 * 86400, now), False)
    check("8 days after -> ok", x.sweep_cadence_ok(now - 8 * 86400, now), True)
    check("margin-debit SELL is urgent -> ok any time",
          x.sweep_cadence_ok(now - 3600, now, urgent=True), True)


def test_execution_window_by_exchange():
    print("\nwithin_entry_execution_window(exchange=...): the shield trades in ITS market's hours:")
    from dashboard.execution import ib_exec as x
    lon = ZoneInfo("Europe/London")
    ny = ZoneInfo("America/New_York")
    mon_0900_lon = dt.datetime(2026, 11, 23, 9, 0, tzinfo=lon)
    mon_1500_ny = dt.datetime(2026, 11, 23, 15, 0, tzinfo=ny)
    check("LSE shield, Mon 09:00 London -> open", x.within_entry_execution_window(
        mon_0900_lon, exchange="LSEETF"), True)
    check("LSE shield, Mon 15:00 New York (20:00 London) -> closed",
          x.within_entry_execution_window(mon_1500_ny, exchange="LSEETF"), False)
    check("US shield, Mon 15:00 New York -> open", x.within_entry_execution_window(
        mon_1500_ny, exchange=""), True)


def _run_sweep(env, positions, cash_hkd, nav_hkd, last_ts=None):
    """Drive sweep_cash() against fakes. Returns (status, list of _sweep_send calls)."""
    from dashboard.execution import ib_exec as x
    sends = []
    contracts = {"IB01": SimpleNamespace(conId=11, symbol="IB01"),
                 "SGOV": SimpleNamespace(conId=22, symbol="SGOV")}

    def _pos(sym, qty):
        return SimpleNamespace(contract=contracts[sym], position=qty)

    def _pf(sym, px):
        return SimpleNamespace(contract=contracts[sym], marketPrice=px)
    marks = {"IB01": 115.0, "SGOV": 100.5}
    ib = SimpleNamespace(positions=lambda: [_pos(s, q) for s, q in positions.items()],
                         portfolio=lambda: [_pf(s, marks[s]) for s in positions])
    summ = {"_ccy": "HKD", "TotalCashValue": cash_hkd, "NetLiquidation": nav_hkd}
    with mock.patch.dict(os.environ, env), \
         mock.patch.object(x, "_guard", return_value=ib), \
         mock.patch.object(x.ib_client, "account_id", return_value="U1"), \
         mock.patch.object(x.ib_client, "stock_contract",
                           side_effect=lambda sym, **kw: contracts[sym]), \
         mock.patch.object(x.ib_client, "filter_by_account", side_effect=lambda i, a: list(i)), \
         mock.patch.object(x.ib_client, "call", side_effect=lambda fn, timeout=None: fn()), \
         mock.patch.object(x.ib_client, "account_summary", return_value=summ), \
         mock.patch.object(x, "within_entry_execution_window", return_value=True), \
         mock.patch.object(x, "_shield_px_estimate", return_value=115.0), \
         mock.patch.object(x, "_withdraw_reserve_usd", return_value=0.0), \
         mock.patch("dashboard.core.store.cache_get", return_value=(last_ts, None)), \
         mock.patch.object(x, "_sweep_send",
                           side_effect=lambda *a, **k: sends.append((a, k)) or a[7]):
        st = x.sweep_cash()
    return st, sends


def test_migration_sells_the_legacy_shield_first():
    print("\nsweep_cash() with CASH_SHIELD_SYMBOL=IB01 while SGOV is still held:")
    env = {"CASH_SWEEP": "1", "CASH_SHIELD_SYMBOL": "IB01"}
    st, sends = _run_sweep(env, {"SGOV": 384.0}, cash_hkd=75_000, nav_hkd=388_000)
    check("one order: the legacy leg", len(sends), 1)
    a, k = sends[0]
    check("SELL all 384 SGOV", (a[3], a[4], a[7 - 1]), ("SELL", 384, "SGOV"))
    check("migration leg does NOT start the weekly cadence", k.get("stamp_cadence"), False)
    check("shield value still reported (legacy included)", st["sgov_value_base"] > 0, True)
    check("status names the configured shield", st["shield"], "IB01")


def test_rebalance_band_and_weekly_cadence():
    print("\nsweep_cash() rebalancing into IB01: band + weekly cadence:")
    env = {"CASH_SWEEP": "1", "CASH_SHIELD_SYMBOL": "IB01"}
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    # HKD 300k idle cash, nothing parked: delta ~ USD 30.8k >> band -> BUY, if cadence allows
    st, sends = _run_sweep(env, {}, cash_hkd=300_000, nav_hkd=300_000,
                           last_ts=now - 2 * 86400)
    check("2 days after the last rebalance -> no order", sends, [])
    check("log explains the weekly cadence", "weekly cadence" in st["log"], True)
    st, sends = _run_sweep(env, {}, cash_hkd=300_000, nav_hkd=300_000,
                           last_ts=now - 8 * 86400)
    check("8 days after -> one BUY of the shield", [(a[3], a[6]) for a, _k in sends],
          [("BUY", "IB01")])
    check("that rebalance starts the cadence", sends[0][1].get("stamp_cadence"), True)
    # already ~80% parked: USD 7.7k cash + 268 x 115 = USD 30.8k shield -> delta ~0 < band
    st, sends = _run_sweep(env, {"IB01": 268.0}, cash_hkd=60_000, nav_hkd=300_000,
                           last_ts=now - 8 * 86400)
    check("delta inside the 5%-of-NAV band -> no order", sends, [])


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
