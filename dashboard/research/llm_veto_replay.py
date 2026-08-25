"""Replay every LLM-vetoed signal against real subsequent price action: was the WAIT call
actually worth anything, or did it cost R? Directly answers the open question from the
2026-08-25 performance retro -- the LLM vetoes a real, sizeable share of deterministic
BUY/SELL signals (~11% of paper's rejections, ~20% of live's), but until now there was no
way to tell whether those vetoes were net positive.

`rejected_signals` (journal.py) stores WHEN + WHICH instrument/direction was vetoed, but not
the entry/sl/tp that would have been used -- those were never computed since the trade never
fired. Reconstructs them the same way a real trade would have gotten them: recompute
`compute_facts()` from WEEKLY closes truncated to the rejection's own timestamp (no look-
ahead), feed that into `paper.compute_sltp()` (same ATR/rr=3.0 method the live core strategy
uses), then walk forward on real DAILY OHLC with `paper.resolve()` -- the exact same
resolution function `resolve_open()` uses for real trades -- to get a genuine hypothetical
WIN/LOSS/EXPIRED and R-multiple.

Only "LLM vetoed a deterministic BUY/SELL to WAIT" rows qualify -- other rejection reasons
(tech-pause, trend-strength, re-entry gate, etc.) aren't the LLM's call and are out of scope
here.

Run:  uv run python -m dashboard.research.llm_veto_replay
      DASH_DB_NAME=dashboard_live_docker.db uv run python -m dashboard.research.llm_veto_replay
"""
from __future__ import annotations
import os
os.environ.setdefault("BROKER", "ib")
os.environ.setdefault("UNIVERSE", "etf")

import datetime as dt

import pandas as pd
import yfinance as yf

from dashboard.core import journal, paper
from dashboard.instruments import active_by_key
from analyst.features import compute_facts

HORIZON_DAYS = paper.HORIZON_CAL   # 35 calendar days, same as a real core-method trade


def _vetoed_rows() -> list[dict]:
    with journal._LOCK, journal._conn() as c:
        rows = c.execute(
            "SELECT ts, instrument, direction FROM rejected_signals "
            "WHERE reasons LIKE 'LLM vetoed to WAIT%'").fetchall()
    return [{"ts": ts, "instrument": inst, "direction": d} for ts, inst, d in rows]


def _naive_index(s: pd.Series) -> pd.Series:
    if s.index.tz is not None:
        s = s.copy()
        s.index = s.index.tz_localize(None)
    return s


def _weekly_closes(yf_ticker: str) -> pd.Series | None:
    df = yf.download(yf_ticker, period="8y", interval="1wk", progress=False, auto_adjust=True)
    if df is None or len(df) == 0:
        return None
    close = df["Close"]
    if hasattr(close, "columns"):
        close = close.iloc[:, 0]
    return _naive_index(close.dropna().astype(float))


def _daily_ohlc(yf_ticker: str) -> pd.DataFrame | None:
    df = yf.download(yf_ticker, period="2y", interval="1d", progress=False, auto_adjust=True)
    if df is None or len(df) == 0:
        return None
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close"})
    return _naive_index(df)


print(f"DASH_DB_NAME = {os.environ.get('DASH_DB_NAME', 'dashboard.db')} "
      f"({'LIVE' if 'live' in os.environ.get('DASH_DB_NAME', '') else 'paper'})\n")

vetoes = _vetoed_rows()
print(f"{len(vetoes)} LLM-vetoed BUY/SELL signal(s) found in rejected_signals.\n")
if not vetoes:
    raise SystemExit(0)

# DEDUPE: board_scan re-runs roughly hourly and re-evaluates the SAME still-open
# deterministic setup every cycle until it resolves or the LLM's read changes -- confirmed
# in the raw data (e.g. DIA vetoed 10+ times between 08-17 and 08-21, all resolving to the
# same +0.18R outcome since they're the same underlying opportunity, not independent bets).
# Counting every re-detection as its own trial would badly inflate n and double/triple-count
# the same real episode. Keep only the FIRST veto of a "streak": for each instrument, drop
# any veto within STREAK_GAP_HOURS of the previously KEPT one.
STREAK_GAP_HOURS = 24
raw_by_inst: dict[str, list[dict]] = {}
for v in vetoes:
    raw_by_inst.setdefault(v["instrument"], []).append(v)

by_inst: dict[str, list[dict]] = {}
n_deduped = 0
for key, rows in raw_by_inst.items():
    rows = sorted(rows, key=lambda v: v["ts"])
    kept: list[dict] = []
    last_kept_ts: dt.datetime | None = None
    for v in rows:
        try:
            ts = dt.datetime.fromisoformat(v["ts"]).replace(tzinfo=None)
        except ValueError:
            continue
        if last_kept_ts is None or (ts - last_kept_ts) >= dt.timedelta(hours=STREAK_GAP_HOURS):
            kept.append(v)
            last_kept_ts = ts
        else:
            n_deduped += 1
    by_inst[key] = kept
print(f"After collapsing same-instrument re-detections within {STREAK_GAP_HOURS}h "
      f"(the same underlying opportunity re-vetoed every board-scan cycle): "
      f"{sum(len(v) for v in by_inst.values())} independent veto episode(s) "
      f"({n_deduped} re-detection(s) dropped).\n")

results: list[dict] = []
for key, rows in sorted(by_inst.items()):
    inst = active_by_key(key)
    if inst is None:
        print(f"{key}: not resolvable via active_by_key() (retired/renamed?), "
              f"skipping {len(rows)} row(s)")
        continue
    weekly = _weekly_closes(inst.yf)
    daily = _daily_ohlc(inst.yf)
    if weekly is None or daily is None:
        print(f"{key}: no price data, skipping {len(rows)} row(s)")
        continue
    for v in rows:
        try:
            ts = dt.datetime.fromisoformat(v["ts"]).replace(tzinfo=None)
        except ValueError:
            continue
        direction = v["direction"] or "long"
        w = weekly[weekly.index <= ts]
        if len(w) < 60:                       # not enough history to trust ATR/trend yet
            continue
        facts, _ = compute_facts(w, key)
        got = paper.compute_sltp(facts, direction, "ATR", rr=paper.RR_DEFAULT)
        if got is None:
            continue
        entry, sl, tp, rr = got
        window = daily[(daily.index > ts) & (daily.index <= ts + dt.timedelta(days=HORIZON_DAYS))]
        outcome = paper.resolve(direction, entry, sl, tp, window)
        if outcome is None:
            continue                          # no bars yet (too recent) -- skip, not a loss
        status, exit_price, exit_time = outcome
        r = paper.r_multiple(direction, entry, sl, exit_price, half_spread=paper.HALF_SPREAD)
        results.append({"instrument": key, "ts": v["ts"], "direction": direction,
                        "status": status, "r": round(r, 3)})

print(f"{len(results)} of {len(vetoes)} vetoes replayed to a resolved outcome "
      f"(the rest lack enough history, or are too recent to have resolved within "
      f"{HORIZON_DAYS}d yet).\n")

if results:
    print(f"{'instrument':<10}{'vetoed at':<21}{'dir':<7}{'outcome':<9}{'R':>7}")
    for r in sorted(results, key=lambda x: x["ts"]):
        print(f"{r['instrument']:<10}{r['ts'][:19]:<21}{r['direction']:<7}"
              f"{r['status']:<9}{r['r']:>+7.2f}")

    rs = [r["r"] for r in results]
    n = len(rs)
    wins = sum(1 for x in rs if x > 0)
    total_r = sum(rs)
    expectancy = total_r / n
    gross_win = sum(x for x in rs if x > 0)
    gross_loss = -sum(x for x in rs if x < 0)
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    print(f"\n--- had every vetoed signal been TAKEN instead ---")
    print(f"n={n}  win_rate={wins/n:.0%}  expectancy={expectancy:+.3f}R  "
          f"profit_factor={pf:.2f}  total={total_r:+.2f}R")
    print(f"\nCompare against the REAL placed-trade stats for the same window (see the "
          f"2026-08-25 retro) to judge whether the veto is earning its keep: a MEANINGFULLY "
          f"WORSE hypothetical number than what actually got placed means the veto is doing "
          f"real work; a similar or BETTER number means it's filtering out trades that would "
          f"have been fine, i.e. costing R for nothing. n={n} is still small -- re-run this "
          f"periodically as more vetoes accumulate, same n>=30 discipline as everything else.")
else:
    print("Nothing resolved yet -- too little history behind the vetoes so far.")
