"""CLI: run the multi-agent analysis and print a trade briefing.

Examples:
  python -m analyst.run --csv eurusd_daily.csv --symbol EURUSD
  python -m analyst.run --mt5 EURUSD --tf H1
  python -m analyst.run --csv eurusd_daily.csv --no-news     # skip news fetch

This produces a RECOMMENDATION for a human. It does NOT place trades.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# allow `import data` from the parent quant/ package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except Exception:
    pass

import data  # from quant/
from analyst.features import compute_facts
from analyst.news import fetch_headlines
from analyst.graph import build_graph


def _print_briefing(state) -> None:
    r, t, s = state["regime"], state["technical"], state["sentiment"]
    d, rk = state["decision"], state["risk"]
    line = "=" * 70
    print(f"\n{line}\nTRADE BRIEFING  -  {state['symbol']}   (decision support, not auto-execution)\n{line}")
    print("FACTS")
    print("  " + state["facts_text"].replace("\n", "\n  "))
    print("-" * 70)
    print("ANALYST VIEWS")
    print(f"  Regime    : {r.regime}  (conf {r.confidence:.0%})")
    print(f"              {r.rationale}")
    print(f"  Technical : {t.direction}  strength {t.strength}/5  "
          f"[S {t.key_support:.5f} / R {t.key_resistance:.5f}]")
    print(f"              {t.rationale}")
    print(f"  Sentiment : {s.score:+.1f}/10   events: {', '.join(s.key_events) or 'none'}")
    print(f"              {s.rationale}")
    print("-" * 70)
    print("DECISION (head trader)")
    print(f"  Proposed  : {d.action}   confidence {d.confidence:.0%}")
    print(f"  Rationale : {d.rationale}")
    if d.disagreements:
        print(f"  Conflicts : {d.disagreements}")
    print(f"  Invalid if: {d.invalidation}")
    print("-" * 70)
    print("RISK GATE (deterministic, final authority)")
    flag = "  *** VETOED LLM ***" if rk.vetoed else ""
    print(f"  FINAL     : {rk.final_action}{flag}")
    if rk.final_action != "WAIT":
        print(f"  Size      : max {rk.max_position_units:,.0f} units, stop {rk.stop_price:.5f}")
    for reason in rk.reasons:
        print(f"  - {reason}")
    print(line)
    print("Reminder: this is research to inform YOUR decision. Verify before risking money.\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", help="OHLC csv with time,close columns")
    src.add_argument("--mt5", help="symbol to pull from a running MT5 terminal")
    ap.add_argument("--tf", default="H1", help="MT5 timeframe (with --mt5)")
    ap.add_argument("--symbol", default=None, help="label for the instrument")
    ap.add_argument("--equity", type=float, default=10_000.0)
    ap.add_argument("--risk", type=float, default=0.005, help="risk per trade as fraction")
    ap.add_argument("--no-news", action="store_true")
    args = ap.parse_args()

    if args.csv:
        prices = data.load_csv(args.csv)
        symbol = args.symbol or Path(args.csv).stem.upper()
    else:
        prices = data.load_mt5(args.mt5, args.tf)
        symbol = args.symbol or args.mt5

    facts, facts_text = compute_facts(prices, symbol)
    headlines = [] if args.no_news else fetch_headlines()
    if not args.no_news:
        print(f"Fetched {len(headlines)} headlines.")

    app = build_graph()
    state = app.invoke({
        "symbol": symbol,
        "facts": facts,
        "facts_text": facts_text,
        "news": headlines,
        "account_equity": args.equity,
        "risk_per_trade": args.risk,
    })
    _print_briefing(state)


if __name__ == "__main__":
    main()
