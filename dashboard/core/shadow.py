"""F3 shadow ledger (IBKR_STRATEGY_FIDELITY_SPEC F3, STRATEGY_IMPROVEMENT_SPEC §10 B2, 2026-11-21).

Every deterministic signal the strategy APPROVES and every one a gate BLOCKS (the LLM veto,
trend strength, RSI overextension, the re-entry gate, cooldown, de-correlation, tech pause, ...)
gets one shadow row with the entry/SL/TP it would have had, resolved on real daily bars to its
horizon exactly like a journal trade. That gives three numbers nobody had on the dashboard:

  * signal-level R   -- what the strategy's signals were worth (approved rows), funded or not
  * execution drag   -- funded realized R minus the same trade's signal-level R
  * gate attribution -- per gate: how much R its blocked signals would have made or lost.
                        "R saved" > 0 means the gate kept the book out of losers.

WHY: the 2026-11-21 retros could only answer "is it the signals or the fills?" and "does the
LLM veto help?" by hand-replaying logs. This ledger answers both continuously, forward-only
(the LLM can't be evaluated on past dates: it knows what happened).

Dedupe: one row per (instrument, source, gate, signal_bar). F1 makes a signal live for a whole
week, so the hourly scan would otherwise record the same setup ~100 times.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import time

from dashboard.core.log import log

RESOLVE_EVERY_SEC = 3600
_RESOLVE_KEY = "shadow_resolve_last_ts"

# canonical gate labels, matched on the rejection reason text (first match wins)
_GATES = (
    ("LLM vetoed", "LLM veto"),
    ("trend strength", "trend strength < 5"),
    ("overextended RSI", "RSI overextended"),
    ("re-entry gate", "re-entry gate"),
    ("cooldown", "cooldown"),
    ("de-correlation", "de-correlation"),
    ("tech investment paused", "tech pause"),
    ("objective edge", "objective edge"),
    ("no confluence", "no confluence"),
    ("off-strategy", "off-strategy class"),
    ("vol filter", "vol filter"),
)


def gate_label(reasons: list[str] | str) -> str:
    """PURE: the canonical gate for a rejection (its FIRST reason decides)."""
    first = (reasons[0] if isinstance(reasons, (list, tuple)) and reasons else reasons) or ""
    for needle, label in _GATES:
        if needle.lower() in str(first).lower():
            return label
    return "other"


def _conn() -> sqlite3.Connection:
    from dashboard.core import paper
    c = sqlite3.connect(paper._DB, timeout=30)
    c.execute("""CREATE TABLE IF NOT EXISTS shadow_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, instrument TEXT, direction TEXT,
        entry REAL, sl REAL, tp REAL, horizon_end TEXT,
        source TEXT, gate TEXT, signal_bar TEXT, paper_id INTEGER,
        status TEXT DEFAULT 'OPEN', exit_ts TEXT, exit_price REAL, r REAL, llm_conf REAL,
        UNIQUE(instrument, source, gate, signal_bar))""")
    cols = {r[1] for r in c.execute("PRAGMA table_info(shadow_signals)")}
    if "llm_conf" not in cols:                       # tables created before 2026-11-21 evening
        c.execute("ALTER TABLE shadow_signals ADD COLUMN llm_conf REAL")
    return c


def record(*, instrument: str, direction: str, entry: float, sl: float, tp: float,
           source: str, gate: str = "", signal_bar: str | None = None,
           paper_id: int | None = None, ts: dt.datetime | None = None,
           horizon_days: int | None = None, llm_conf: float | None = None) -> bool:
    """Insert one shadow row (ignored if this setup is already recorded). Never raises."""
    try:
        from dashboard.core import paper
        now = ts or dt.datetime.now(dt.timezone.utc)
        hz = now + dt.timedelta(days=horizon_days or paper.HORIZON_CAL)
        bar = signal_bar or now.date().isoformat()
        from dashboard.core import paper as _p
        with _p._LOCK, _conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO shadow_signals (ts, instrument, direction, entry, sl, tp, "
                "horizon_end, source, gate, signal_bar, paper_id, llm_conf) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (now.isoformat(timespec="seconds"), instrument, direction, float(entry),
                 float(sl), float(tp), hz.isoformat(timespec="seconds"), source, gate, bar,
                 paper_id, (float(llm_conf) if llm_conf is not None else None)))
            return cur.rowcount > 0
    except Exception as e:                             # noqa: BLE001
        log.debug("shadow: record failed: %s", e)
        return False


def resolve_open(get_ohlc_fn, force: bool = False) -> int:
    """Resolve OPEN shadow rows with the journal's own resolver (paper._outcome_for: daily
    bars, conservative SL-before-TP, EXPIRED at the horizon). At most hourly. Never raises."""
    try:
        from dashboard.core import paper, store
        last, _ts = store.cache_get(_RESOLVE_KEY)
        if not force and last and time.time() - float(last) < RESOLVE_EVERY_SEC:
            return 0
        store.cache_set(_RESOLVE_KEY, int(time.time()))
        with paper._LOCK, _conn() as c:
            rows = c.execute("SELECT id, ts, instrument, direction, entry, sl, tp, horizon_end "
                             "FROM shadow_signals WHERE status='OPEN'").fetchall()
        n = 0
        for sid, ts, inst, d, e, sl, tp, hz in rows:
            t = {"id": sid, "ts": ts, "instrument": inst, "direction": d, "entry": e, "sl": sl,
                 "tp": tp, "horizon_end": hz, "method": "shadow"}
            try:
                out = paper._outcome_for(t, get_ohlc_fn)
            except Exception as ex:                    # noqa: BLE001
                log.debug("shadow: resolve %s failed: %s", inst, ex)
                continue
            if not out or out == "OPEN":
                continue
            status, px, xt = out
            r = paper.r_multiple(d, float(e), float(sl), float(px))
            with paper._LOCK, _conn() as c:
                c.execute("UPDATE shadow_signals SET status=?, exit_ts=?, exit_price=?, r=? "
                          "WHERE id=?", (status, str(xt), float(px), float(r), sid))
            n += 1
        return n
    except Exception as e:                             # noqa: BLE001
        log.debug("shadow: resolve_open failed: %s", e)
        return 0


def _stats(rs: list[float]) -> dict:
    n = len(rs)
    if not n:
        return {"n": 0, "win": None, "avg_r": None, "sum_r": 0.0}
    return {"n": n, "win": sum(1 for r in rs if r > 0) / n, "avg_r": sum(rs) / n,
            "sum_r": sum(rs)}


CONF_BUCKETS = ((0.0, 0.6, "<0.6"), (0.6, 0.8, "0.6-0.8"), (0.8, 1.01, ">=0.8"))


def summarize(rows: list[tuple], funded: dict[int, float]) -> dict:
    """PURE: rows = (source, gate, status, r, paper_id) of shadow_signals; funded =
    {paper_id: realized R} of funded, closed journal trades.
    -> {"approved": stats, "gates": {gate: stats + "r_saved"}, "drag": stats of
        (funded R - signal R) over approved rows that were funded, "open": n unresolved}."""
    approved, gates, drag, open_n = [], {}, [], 0
    llm_veto, conf = [], {lbl: [] for _lo, _hi, lbl in CONF_BUCKETS}
    for row in rows:
        source, gate, status, r, pid = row[:5]
        lc = row[5] if len(row) > 5 else None
        if status == "OPEN" or r is None:
            open_n += 1
            continue
        if source == "approved":
            approved.append(float(r))
            if pid is not None and pid in funded:
                drag.append(float(funded[pid]) - float(r))
            if lc is not None:
                for lo, hi, lbl in CONF_BUCKETS:
                    if lo <= float(lc) < hi:
                        conf[lbl].append(float(r))
        elif source == "llm_monitor":
            llm_veto.append(float(r))           # trades the rules TOOK that the LLM would veto
        else:
            gates.setdefault(gate or "other", []).append(float(r))
    g = {}
    for k, rs in gates.items():
        st = _stats(rs)
        st["r_saved"] = -st["sum_r"]          # blocked losers = R the gate kept out of the book
        g[k] = st
    return {"approved": _stats(approved), "gates": g, "drag": _stats(drag), "open": open_n,
            "llm_would_veto": _stats(llm_veto),
            "llm_conf": {k: _stats(v) for k, v in conf.items()}}


def summary() -> dict:
    """summarize() over the live DB. Funded = broker-executed, closed, not signal-only."""
    from dashboard.core import paper
    from dashboard.execution import broker
    try:
        with paper._LOCK, _conn() as c:
            rows = c.execute("SELECT source, gate, status, r, paper_id, llm_conf "
                             "FROM shadow_signals").fetchall()
        ex = broker.executed_ids()
        funded = {t["id"]: float(t["realized_r"]) for t in paper.all_trades()
                  if t["id"] in ex and t["status"] in ("WIN", "LOSS", "EXPIRED", "BE")
                  and t.get("realized_r") is not None
                  and "(signal only" not in (t.get("exit_reason") or "")}
        return summarize(rows, funded)
    except Exception as e:                             # noqa: BLE001
        log.debug("shadow: summary failed: %s", e)
        return {"approved": _stats([]), "gates": {}, "drag": _stats([]), "open": 0,
                "llm_would_veto": _stats([]), "llm_conf": {}}
