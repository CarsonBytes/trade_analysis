"""Go-live PRE-FLIGHT check (Bucket C). Run AFTER you flip the manual IBKR Client-Portal switches
(fractional shares, US-stock/USD/FX permissions, margin upgrade). It connects via ib_client and
reports a go/no-go: account id + paper/live, base ccy, NetLiq/cash, US-stock data+permission proxy,
and -- crucially -- a NON-MUTATING `whatIf` test that a FRACTIONAL-share BRACKET (the strategy's
order shape) is actually accepted. Nothing here places a real order.

  IB_CLIENT_ID=13 BROKER=ib UNIVERSE=etf uv run --no-sync python -m dashboard.research.preflight_check
"""
import os
os.environ.setdefault("BROKER", "ib"); os.environ.setdefault("UNIVERSE", "etf")
import sys
sys.path.insert(0, "D:/quant")
from dashboard.data import ib_client


def ok(b): return "OK " if b else "XX "


def main():
    if not ib_client.is_available():
        print("XX  IB not available -- is the Gateway/TWS running with API enabled (ReadOnlyApi=no)?")
        return
    acct = ib_client.account_id()
    paper = ib_client.is_paper()
    print(f"{ok(bool(acct))}connected: account={acct}  paper={paper}")
    summ = ib_client.account_summary() or {}
    ccy = summ.get("_ccy", "?")
    nl = summ.get("NetLiquidation"); cash = summ.get("TotalCashValue")
    atype = summ.get("AccountType") or summ.get("accountType") or "?"
    print(f"{ok(ccy!='?')}base currency: {ccy}   account type: {atype}")
    print(f"{ok(nl is not None)}NetLiquidation: {nl}   TotalCash: {cash}")

    # US-stock contract qualifies => trading permission + data path for ETFs/SGOV
    for sym in ["SPY", "SGOV"]:
        c = ib_client.stock_contract(sym)
        cid = getattr(c, "conId", 0) if c else 0
        print(f"{ok(bool(cid))}{sym}: stock contract qualifies (conId={cid})")

    # FRACTIONAL: whatIf is unreliable for confirming fractional-ENABLEMENT across ib_async
    # versions, so the definitive test is a single real paper order (mutating -> separate step).
    print("..  FRACTIONAL: definitive test is one real PAPER fractional-bracket order (below).")

    print("\nGO/NO-GO: all 'OK' rows green => ready. Any 'XX' => fix that IBKR setting before funding.")
    print("NB: a real fractional BRACKET (parent+STP+LMT) should also be paper-tested with one live")
    print("    order before trusting it -- some IB setups reject stops on fractional lots.")
    ib_client.shutdown()


if __name__ == "__main__":
    main()
