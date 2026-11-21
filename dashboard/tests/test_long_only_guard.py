"""Long-only guard, phase 1 (IBKR_LONG_ONLY_GUARD_SPEC G6.1 / G8, 2026-11-21):
  - scripts/flex_nav_reconcile.py: StockShort parsing + short_alarm() (CSV and XML).
  - ib_client.portfolio_item_dict(): PortfolioItem -> plain dict for the untracked card.
Pure, no network, no broker.
Run:  uv run python -m dashboard.tests.test_long_only_guard
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import pathlib
from types import SimpleNamespace

_fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        _fails.append(name)
    assert ok, f"{name}: got {got!r} want {want!r}"


def _flex():
    p = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "flex_nav_reconcile.py"
    spec = importlib.util.spec_from_file_location("flex_nav_reconcile", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Shape of the real LIVE CSV statement (query 1659726), trimmed to the NAV section.
_CSV = b'''"BOF","U0000000","StatementOfFundsNetLiquidation","4","20261116","20261120","20261121;000000","100","100"
"BOA","Acct",20261116,20261120
"BOS","EQUT","Net Asset Value (NAV) in Base; trade date basis"
"HEADER","EQUT","ClientAccountID","ReportDate","Cash","Stock","StockLong","StockShort","Total"
"DATA","EQUT","U0000000","20261116","70996.26","211338.02","284890.66","-73552.63","282155.00"
"DATA","EQUT","U0000000","20261119","75453.96","209730.53","303012.21","-93281.67","285167.63"
"DATA","EQUT","U0000000","20261120","75412.85","213485.88","302936.38","-89450.51","288872.63"
"EOS","EQUT"
"EOA","Acct"
"EOF"
'''


def test_flex_short_stock_csv():
    print("flex: StockShort parsed per day from the CSV NAV section (the 2026-11 LIVE book):")
    f = _flex()
    by_day = f._short_stock_by_day(_CSV)
    check("three days parsed", len(by_day), 3)
    check("Nov 20 short value", by_day[dt.date(2026, 11, 20)], -89450.51)
    check("NAV still parses from the same bytes",
          f._parse_nav(_CSV)[dt.date(2026, 11, 20)], 288872.63)
    check("alarm on the latest day", f.short_alarm(by_day), (dt.date(2026, 11, 20), -89450.51))


def test_flex_short_alarm_rules():
    print("\nflex short_alarm(): latest day only, tolerance respected, no data -> no alarm:")
    f = _flex()
    d1, d2 = dt.date(2026, 11, 19), dt.date(2026, 11, 20)
    check("covered by the latest day -> history, not an alarm",
          f.short_alarm({d1: -93281.67, d2: 0.0}), None)
    check("rounding noise is not a short", f.short_alarm({d2: -0.4}), None)
    check("empty -> None", f.short_alarm({}), None)
    flat = _CSV.replace(b'"-89450.51"', b'"0"')
    check("flat latest day in CSV -> None", f.short_alarm(f._short_stock_by_day(flat)), None)


def test_flex_short_stock_xml():
    print("\nflex: StockShort from the XML statement shape:")
    f = _flex()
    xml = (b'<FlexQueryResponse><FlexStatements><FlexStatement>'
           b'<EquitySummaryInBase>'
           b'<EquitySummaryInReport ReportDate="2026-11-20" Cash="1" Total="288872.63" '
           b'StockShort="-89450.51"/>'
           b'</EquitySummaryInBase></FlexStatement></FlexStatements></FlexQueryResponse>')
    check("xml short parsed", f._short_stock_by_day(xml), {dt.date(2026, 11, 20): -89450.51})
    check("xml without the column -> no data (not 'flat')",
          f._short_stock_by_day(xml.replace(b' StockShort="-89450.51"', b"")), {})


def test_portfolio_item_dict():
    print("\nib_client.portfolio_item_dict(): PortfolioItem -> JSON-safe dict:")
    from dashboard.data.ib_client import portfolio_item_dict
    item = SimpleNamespace(
        contract=SimpleNamespace(symbol="QQQ", conId=320227571, secType="STK", currency="USD"),
        position=-8.0, averageCost=692.94, marketPrice=640.0, marketValue=-5120.0,
        unrealizedPNL=423.5)
    check("short row", portfolio_item_dict(item),
          {"symbol": "QQQ", "con_id": 320227571, "qty": -8.0, "avg_cost": 692.94,
           "market_price": 640.0, "market_value": -5120.0, "unrealized_pnl": 423.5,
           "currency": "USD"})
    cash = SimpleNamespace(contract=SimpleNamespace(symbol="USD", conId=1, secType="CASH"),
                           position=9558.0)
    check("CASH row dropped", portfolio_item_dict(cash), None)
    flat = SimpleNamespace(contract=SimpleNamespace(symbol="EEM", conId=2, secType="STK"),
                           position=0.0)
    check("flat row dropped", portfolio_item_dict(flat), None)


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
