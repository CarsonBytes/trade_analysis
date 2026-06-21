"""Append-only audit journal for retrospective analysis.

The paper_trades table records trades. This module records the CONTEXT around
them that the rest of the system would otherwise overwrite or only log to a
rotating file:

  - board_scans:       every LLM board scan in full (macro_note + all per-
                       instrument signals). The live cache keeps only the
                       LATEST scan; this keeps the whole history so you can
                       replay how the model's view evolved.
  - rejected_signals:  every signal that passed the deterministic BUY/SELL gate
                       but was NOT traded, with the exact reason(s). This is the
                       counterfactual record you need to judge whether a
                       constraint (confidence, vol filter, de-correlation,
                       cooldown, strength) is earning its keep.

Same SQLite file as the journal/budget guard. Append-only: nothing here is ever
updated or deleted by the app, so it is a faithful forensic record.
"""
from __future__ import annotations

import json
import sqlite3
import datetime as dt
import threading

from dashboard.core import store  # reuse the same DB path

_LOCK = threading.Lock()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(store._DB, check_same_thread=False)
    c.execute("""CREATE TABLE IF NOT EXISTS board_scans (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, macro_note TEXT,
        n_signals INTEGER, signals_json TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS board_scan_signals (
        scan_id INTEGER, ts TEXT, instrument TEXT, bias TEXT, action TEXT,
        confidence REAL, rationale TEXT, invalidation TEXT,
        det_signal TEXT, det_strength INTEGER)""")
    c.execute("""CREATE TABLE IF NOT EXISTS rejected_signals (
        ts TEXT, instrument TEXT, direction TEXT, det_strength INTEGER,
        confidence REAL, reasons TEXT)""")
    return c


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ---- LLM board-scan history ------------------------------------------------

def record_scan(result, scores_by_key: dict | None = None) -> int:
    """Append one board scan. `result` is a board_scan.BoardScan. Optionally
    pass {key: Score} to also store the deterministic signal/strength alongside
    each LLM signal (so you can see agreement/disagreement). Returns scan id."""
    scores_by_key = scores_by_key or {}
    ts = _now()
    signals = [s.model_dump() for s in result.signals]
    with _LOCK, _conn() as c:
        cur = c.execute(
            "INSERT INTO board_scans(ts, macro_note, n_signals, signals_json) "
            "VALUES(?,?,?,?)",
            (ts, result.macro_note, len(signals), json.dumps(signals)))
        scan_id = cur.lastrowid
        for s in result.signals:
            sc = scores_by_key.get(s.key)
            c.execute(
                "INSERT INTO board_scan_signals(scan_id, ts, instrument, bias, "
                "action, confidence, rationale, invalidation, det_signal, "
                "det_strength) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (scan_id, ts, s.key, s.bias, s.action, s.confidence, s.rationale,
                 s.invalidation, getattr(sc, "signal", ""), getattr(sc, "strength", 0)))
    return scan_id


def scan_history(instrument: str | None = None, limit: int = 500) -> list[dict]:
    """Per-instrument LLM signal history (newest first), optionally filtered."""
    with _LOCK, _conn() as c:
        q = "SELECT * FROM board_scan_signals"
        params: tuple = ()
        if instrument:
            q += " WHERE instrument=?"
            params = (instrument,)
        q += " ORDER BY rowid DESC LIMIT ?"
        cur = c.execute(q, params + (limit,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def macro_history(limit: int = 200) -> list[dict]:
    with _LOCK, _conn() as c:
        cur = c.execute("SELECT id, ts, macro_note, n_signals FROM board_scans "
                        "ORDER BY id DESC LIMIT ?", (limit,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---- rejected-signal (constraint) history ----------------------------------

def record_rejections(rows: list[dict]) -> None:
    """Append rejected signals. Each row: {instrument, direction, det_strength,
    confidence, reasons}. Only call for signals that were genuine BUY/SELL
    candidates (skip the WAIT/WATCH noise)."""
    if not rows:
        return
    ts = _now()
    with _LOCK, _conn() as c:
        for r in rows:
            c.execute("INSERT INTO rejected_signals(ts, instrument, direction, "
                      "det_strength, confidence, reasons) VALUES(?,?,?,?,?,?)",
                      (ts, r["instrument"], r.get("direction", ""),
                       r.get("det_strength", 0), r.get("confidence", 0.0),
                       "; ".join(r.get("reasons", []))))


# canonical constraint labels: match a reason to its gate so different numeric
# values aggregate into one bucket (the scorecard counts gates, not values).
_GATE_PREFIXES = [
    ("confidence", "confidence below threshold"),
    ("objective edge", "objective edge below threshold (losing regime)"),
    ("overextended", "overextended entry (chasing RSI extreme)"),
    ("trend strength", "trend strength below MIN_STRENGTH"),
    ("vol filter", "volatility filter (atr < median)"),
    ("no confluence", "deterministic disagrees with LLM"),
    ("de-correlation", "de-correlation (bucket already held)"),
    ("cooldown", "cooldown after recent close"),
    ("no live price", "no live price for entry"),
]


def _canon(reason: str) -> str:
    r = reason.strip()
    low = r.lower()
    for prefix, label in _GATE_PREFIXES:
        if low.startswith(prefix):
            return label
    return r  # unknown reason: keep verbatim so it's still visible


def rejection_counts() -> list[tuple[str, int]]:
    """How often each GATE blocked a directional candidate -- the constraint
    scorecard. Numeric values are normalised so a gate aggregates to one row."""
    with _LOCK, _conn() as c:
        rows = c.execute("SELECT reasons FROM rejected_signals").fetchall()
    counts: dict[str, int] = {}
    for (reasons,) in rows:
        for part in (reasons or "").split(";"):
            if part.strip():
                key = _canon(part)
                counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
