"""Historical replay: bootstrap a track record from past data.

For each instrument it walks daily bars, and at each eligible point computes the
DETERMINISTIC signal using only data up to that bar (no look-ahead), opens a
trade with the given SL/TP method, and resolves it against the ACTUAL next bars.
One position per instrument-variant at a time (cooldown until the trade closes),
mirroring the live dedup rule.

LLM signals are deliberately NOT replayed: the model may 'know' the historical
period, which would be look-ahead. So replay grades the reproducible
deterministic logic; the LLM is validated only by live forward testing.

Run:  python -m dashboard.replay
"""
from __future__ import annotations

from . import net  # noqa: F401

import argparse
import pandas as pd

from analyst.features import compute_facts
from metrics import deflated_sharpe_ratio
from .instruments import UNIVERSE, BY_KEY
from .providers import get_ohlc
from . import scoring
from .scoring import score_from_facts
from . import paper


def _resolve_daily(direction, entry, sl, tp, bars):
    """Like paper.resolve but returns (status, exit_price, bars_used)."""
    for n, (_ts, row) in enumerate(bars.iterrows(), start=1):
        hi, lo = row["high"], row["low"]
        if direction == "long":
            if lo <= sl:
                return "LOSS", sl, n
            if hi >= tp:
                return "WIN", tp, n
        else:
            if hi >= sl:
                return "LOSS", sl, n
            if lo <= tp:
                return "WIN", tp, n
    if len(bars):
        return "EXPIRED", float(bars["close"].iloc[-1]), len(bars)
    return None


def replay_variant(df: pd.DataFrame, key: str, method: str, rr: float) -> list[float]:
    close = df["close"]
    rs: list[float] = []
    i = 160  # need history for the long-trend MA
    n = len(df)
    while i < n - 1:
        facts, _ = compute_facts(close.iloc[: i + 1], key)
        score = score_from_facts(key, facts, "")
        if score.signal not in ("BUY", "SELL") or score.strength < paper.MIN_STRENGTH:
            i += 1
            continue
        direction = "long" if score.signal == "BUY" else "short"
        res = paper.compute_sltp(facts, direction, method, rr)
        if res is None:
            i += 1
            continue
        entry, sl, tp, rr_actual = res
        if rr_actual < paper.MIN_RR:
            i += 1
            continue
        bars = df.iloc[i + 1: i + 1 + paper.HORIZON_DAYS]
        outcome = _resolve_daily(direction, entry, sl, tp, bars)
        if outcome is None:
            break
        status, exit_price, used = outcome
        rs.append(paper.r_multiple(direction, entry, sl, exit_price))
        i += used + 1  # cooldown: jump past the closed trade
    return rs


def _run_block(data: dict, variants: list, n_trials: int) -> None:
    print(f"{'variant':<12}{'n':>5}{'win%':>8}{'expR':>8}{'PF':>7}{'totalR':>9}{'DSR':>7}")
    print("-" * 60)
    for method, rr in variants:
        all_r: list[float] = []
        for key, df in data.items():
            all_r += replay_variant(df, key, method, rr)
        s = paper.stats(all_r)
        dsr = deflated_sharpe_ratio(pd.Series(all_r), n_trials=n_trials) if all_r else 0.0
        label = f"{method} rr{rr:.1f}" if method == "ATR" else "STRUCT"
        pf = "inf" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
        flag = "" if s["trustworthy"] else "  (n<30)"
        print(f"{label:<12}{s['n']:>5}{s['win_rate']*100:>7.1f}%{s['expectancy_R']:>8.3f}"
              f"{pf:>7}{s['total_R']:>9.1f}{dsr:>7.0%}{flag}")
    print("-" * 60)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", default="5y")
    args = ap.parse_args()

    variants = [("ATR", rr) for rr in paper.RR_SWEEP] + [("STRUCT", paper.RR_DEFAULT)]
    data = {}
    for inst in UNIVERSE:
        df = get_ohlc(inst, period=args.period, interval="1d")
        if df is not None and len(df) > 200:
            data[inst.key] = df
    print(f"Replay on {len(data)} instruments, period {args.period}, "
          f"horizon {paper.HORIZON_DAYS}d, SL {paper.SL_ATR_MULT}xATR")
    print("Pass bar for the filter: DSR >= 95% AND expR > 0.\n")

    # A/B the pre-registered exhaustion filter, same data, deterministic signals.
    for on in (False, True):
        scoring.BLOCK_EXHAUSTION_ENTRIES = on
        print(f"=== exhaustion filter {'ON' if on else 'OFF (baseline)'} ===")
        _run_block(data, variants, n_trials=len(variants))
        print()
    print("expR = per-trade expectancy in R (THE number). DSR = P(true Sharpe>0)\n"
          "after penalising for trying the variants. LLM signals are NOT replayed\n"
          "(look-ahead); this judges only the reproducible deterministic logic.")


if __name__ == "__main__":
    main()
