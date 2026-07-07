"""Re-runnable audit: has the LLM ever independently blocked a NEW entry that
the RSI/strength filters wouldn't already have blocked on their own?

This is the one open, valid question from the 2026-07-07 HANDOFF investigation
(see HANDOFF.md) -- NOT the retracted "exit on WAIT" simulation, which tested
an invented rule the LLM was never designed to support (WAIT is a fresh-entry
conviction call only, per its own system prompt in web/board_scan.py -- never
a signal about closing an existing position).

Two checks, both cheap and re-runnable as more live history accumulates:

1. rejected_signals: does "no confluence" (LLM disagreement) ever appear as
   the SOLE reason a candidate was blocked? If it's always alongside an RSI/
   strength reason, the LLM isn't doing independent work -- those trades were
   getting blocked anyway.
2. board_scan_signals: dedupe scan-level noise into direction-contiguous
   "episodes" (each new streak = one real entry decision point) and report
   what fraction the LLM confirmed vs vetoed AT THE START of the episode --
   the only point where LLM's call actually gates a new trade.

Run:  python -m dashboard.research.llm_gate_audit [db_path ...]
Defaults to auditing both dashboard.db and dashboard_live.db if no path given.
"""
from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

DEFAULT_DBS = ["dashboard.db", "dashboard_live.db"]


def _norm(action: str) -> str:
    return action if action in ("BUY", "SELL") else "WAIT"


def audit_rejections(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT reasons FROM rejected_signals").fetchall()
    total = len(rows)
    mentions_confluence = [r[0] for r in rows if "no confluence" in r[0]]
    solo_confluence = [r for r in mentions_confluence
                       if "RSI" not in r and "strength" not in r]
    print(f"  rejected_signals: {total} total rows")
    print(f"    mention 'no confluence' (LLM disagreed on direction): {len(mentions_confluence)}")
    print(f"    'no confluence' as the ONLY reason (independent LLM veto): "
          f"{len(solo_confluence)}")
    if solo_confluence:
        print("    *** FOUND an independent LLM veto -- worth investigating: ***")
        for r in solo_confluence[:10]:
            print("      ", r)
    else:
        print("    -> none yet: every LLM-disagreement case so far was already "
              "blocked by RSI/strength anyway.")


def audit_episodes(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT instrument, ts, action, det_signal FROM board_scan_signals "
        "WHERE det_strength >= 5 ORDER BY instrument, ts").fetchall()
    by_inst: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for inst, ts, action, det in rows:
        if _norm(det) in ("BUY", "SELL"):
            by_inst[inst].append((ts, _norm(action), _norm(det)))

    episodes: list[tuple[str, str, str, str]] = []  # (inst, start_ts, det_dir, llm_at_start)
    for inst, seq in by_inst.items():
        cur_dir = None
        for ts, a, d in seq:
            if d != cur_dir:
                episodes.append((inst, ts, d, a))
                cur_dir = d

    n = len(episodes)
    confirmed = sum(1 for *_x, a in episodes if a != "WAIT")
    vetoed = [e for e in episodes if e[3] == "WAIT"]
    print(f"  board_scan_signals: {n} direction-contiguous episodes "
          f"(each = one real entry decision point)")
    print(f"    LLM confirmed at episode start: {confirmed}/{n}")
    print(f"    LLM said WAIT at episode start (would have blocked a NEW entry): "
          f"{len(vetoed)}/{n}")
    if vetoed:
        print("    *** FOUND a case where LLM vetoed a fresh entry -- worth "
              "investigating: ***")
        for e in vetoed[:10]:
            print("      ", e)
        # NOTE (2026-07-07): a single-scan WAIT at episode start can be noise --
        # checked the live DB's 5 vetoed cases by hand and every one flipped back
        # to BUY within ONE scan (confidence sitting right at ~0.5, the model's
        # floor). Don't "fix" this by requiring N consecutive WAIT scans: scan
        # cadence is NOT fixed (observed gaps from ~15min up to ~6h in the same
        # instrument's history -- weekends pause it too), so a raw scan-count
        # threshold doesn't correspond to any consistent wall-clock window. If a
        # persistence check is ever added, anchor it to ELAPSED TIME since the
        # episode start (e.g. "WAIT held continuously for >=60min"), not scan
        # count -- and only bother once a real candidate needs it: as of this
        # date, EVERY vetoed episode in both DBs reverts within one scan, so
        # there is nothing yet for a persistence filter to actually change.


def main() -> None:
    paths = sys.argv[1:] or DEFAULT_DBS
    base = Path(__file__).resolve().parents[1]  # dashboard/
    for p in paths:
        path = Path(p)
        if not path.is_absolute():
            path = base / path
        if not path.exists():
            print(f"[skip] {path} not found")
            continue
        print(f"=== {path.name} ===")
        conn = sqlite3.connect(path)
        try:
            audit_rejections(conn)
            audit_episodes(conn)
        finally:
            conn.close()
        print()


if __name__ == "__main__":
    main()
