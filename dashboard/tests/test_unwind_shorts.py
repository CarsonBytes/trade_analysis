"""Tests for dashboard/ops/unwind_shorts.py -- the tool that flattens unintended shorts.

This tool SENDS ORDERS, so the properties below are the ones that matter: it must only ever
reduce a short, never exceed what buying power affords, and never touch a long. Built after
duplicate OCA brackets over-sold paper positions into shorts (HYD -6,402 against a journal
record of +66 long) -- see HANDOFF 2026-09-01.

Run:  uv run python -m dashboard.tests.test_unwind_shorts
"""
from __future__ import annotations

import os
from unittest import mock

_fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        _fails.append(name)
    assert ok, f"{name}: got {got!r} want {want!r}"


class _C:
    def __init__(self, con_id, symbol):
        self.conId, self.symbol = con_id, symbol


class _Pos:
    def __init__(self, con_id, symbol, qty):
        self.contract = _C(con_id, symbol)
        self.position = qty


class _PF:
    def __init__(self, con_id, symbol, px):
        self.contract = _C(con_id, symbol)
        self.marketPrice = px


def _run_tool(positions, buying_power_hkd, apply_=False, only="", force_rth=True,
              max_tranches=6):
    """Run main() against a fake broker; return the list of (action, qty, symbol) sent."""
    from dashboard.ops import unwind_shorts as U

    sent = []
    state = {"positions": list(positions)}

    class _FakeIB:
        def positions(self):
            return list(state["positions"])
        def portfolio(self):
            return [_PF(p.contract.conId, p.contract.symbol, 49.50)
                    for p in state["positions"]]
        def placeOrder(self, contract, order):
            sent.append((order.action, int(order.totalQuantity), contract.symbol))
            # simulate the fill: reduce the short by exactly the bought quantity
            for p in state["positions"]:
                if p.contract.conId == contract.conId:
                    p.position += int(order.totalQuantity)
            state["positions"] = [p for p in state["positions"] if p.position]
            return object()

    env = {"APPLY": "1" if apply_ else "", "ONLY": only, "FORCE_RTH": "1" if force_rth else "",
           "MAX_TRANCHES": str(max_tranches), "BP_SAFETY": "0.85"}
    with mock.patch.dict(os.environ, env, clear=False), \
         mock.patch.object(U, "APPLY", apply_), \
         mock.patch.object(U, "ONLY", {s.strip().upper() for s in only.split(",") if s.strip()}), \
         mock.patch.object(U, "FORCE_RTH", force_rth), \
         mock.patch.object(U, "MAX_TRANCHES", max_tranches), \
         mock.patch.object(U, "SETTLE_SEC", 0), \
         mock.patch.object(U.paper, "LONG_ONLY", True), \
         mock.patch.object(U.ib_exec, "_guard", return_value=_FakeIB()), \
         mock.patch.object(U.broker, "is_live", return_value=False), \
         mock.patch.object(U.ib_client, "account_id", return_value="DU1"), \
         mock.patch.object(U.ib_client, "filter_by_account", side_effect=lambda i, a: i), \
         mock.patch.object(U.ib_client, "call", side_effect=lambda fn, **kw: fn()), \
         mock.patch.object(U.ib_client, "account_summary",
                           return_value={"BuyingPower": buying_power_hkd, "_ccy": "HKD"}), \
         mock.patch.object(U.ib_client, "fx_to_usd", return_value=1.0 / 7.8):
        U.main()
    return sent


def test_dry_run_sends_nothing():
    print("\nunwind_shorts: a dry run must place NO orders, however large the short:")
    sent = _run_tool([_Pos(1, "HYD", -6402)], buying_power_hkd=1_386_902, apply_=False)
    check("no orders sent in dry run", sent, [])


def test_never_touches_a_long_position():
    print("unwind_shorts: a LONG position is not an unintended short -- must be ignored "
          "entirely (the tool must never be able to sell the real book):")
    sent = _run_tool([_Pos(1, "VNQ", 203), _Pos(2, "AMLP", 652)],
                     buying_power_hkd=1_386_902, apply_=True)
    check("no orders sent against longs", sent, [])


def test_only_ever_buys_and_only_up_to_the_short():
    print("unwind_shorts: with ample buying power it BUYS exactly the short size -- never "
          "more (which would flip it long), never a SELL:")
    sent = _run_tool([_Pos(1, "CWB", -50)], buying_power_hkd=1_386_902, apply_=True)
    check("exactly one order", len(sent), 1)
    check("BUY 50 CWB -- flattens, does not flip", sent[0], ("BUY", 50, "CWB"))


def test_tranches_respect_buying_power_and_finish_as_margin_frees():
    print("unwind_shorts: when the short costs more than buying power, it must SPLIT into "
          "tranches capped by BP_SAFETY x buying power, re-reading state each time (this is "
          "the HYD case: ~$317k to flatten against ~$177k of buying power):")
    # 1000 sh @ 49.50 = $49,500 to flatten. BP 100,000 HKD = $12,820; x0.85 = $10,897
    # -> affords 220/tranche, so it needs several passes.
    sent = _run_tool([_Pos(1, "HYD", -1000)], buying_power_hkd=100_000, apply_=True,
                     max_tranches=6)
    check("every order is a BUY", {a for a, _q, _s in sent}, {"BUY"})
    check("every order is for HYD", {s for _a, _q, s in sent}, {"HYD"})
    check("more than one tranche was needed", len(sent) > 1, True)
    check("no single tranche exceeds what buying power affords (220)",
          [q for _a, q, _s in sent if q > 220], [])
    check("total bought never exceeds the original short",
          sum(q for _a, q, _s in sent) <= 1000, True)


def test_max_tranches_is_a_hard_stop():
    print("unwind_shorts: MAX_TRANCHES caps how many orders one run can send, so a bad "
          "buying-power reading can't turn into an unbounded order loop:")
    sent = _run_tool([_Pos(1, "HYD", -100000)], buying_power_hkd=100_000, apply_=True,
                     max_tranches=3)
    check("stopped at MAX_TRANCHES", len(sent), 3)


def test_only_filter_restricts_to_named_symbols():
    print("unwind_shorts: ONLY=CWB must leave every other short alone:")
    sent = _run_tool([_Pos(1, "HYD", -6402), _Pos(2, "CWB", -50)],
                     buying_power_hkd=1_386_902, apply_=True, only="CWB")
    check("only CWB was traded", {s for _a, _q, s in sent}, {"CWB"})


def test_refuses_when_long_only_is_false():
    print("unwind_shorts: the whole premise is 'this book never shorts on purpose'. If "
          "LONG_ONLY is False that premise fails and it must refuse outright:")
    from dashboard.ops import unwind_shorts as U
    with mock.patch.object(U.paper, "LONG_ONLY", False):
        try:
            U.main()
            check("raised SystemExit", False, True)
        except SystemExit as e:
            check("refused with an explanatory message", "LONG_ONLY" in str(e), True)


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
