"""Unified 'something notable happened' hook -- ADDED 2026-07-14. One call site per event
type feeds BOTH the local changelog (queried by the Alerts tab / Recent events panels) and
a push notification if configured.

v3 (2026-08-26, "Alerts v3" spec) -- three changes driven by real usage:
1. TIERS: every event is classified RED (needs attention -- the only tier that pushes to
   Telegram/ntfy), YELLOW (worth knowing), or WHITE (log). Raw `level` still stored for
   backward compatibility.
2. DEDUPE: repeat events sharing a dedupe_key within DEDUPE_WINDOW_MIN collapse into one
   row with a counter (the EIMI morning produced 5 rows for one incident; now it's 1 card
   showing x3). Escalation rule: a reconcile-mismatch group that hits
   MISMATCH_ESCALATE_AFTER occurrences becomes RED and pushes once.
3. HUMANIZE: plain-language title/detail per event (humanize()); raw message always kept.

Push policy (user-requested): ONLY red-tier alerts notify. A warning alone never buzzes
the phone anymore.

Uses the SAME per-instance database as everything else (paper._DB).
"""
from __future__ import annotations

import datetime as dt
import re
import sqlite3

from dashboard.core.log import log

DEDUPE_WINDOW_MIN = 30
MISMATCH_ESCALATE_AFTER = 3

_table_ready: set[str] = set()   # DB paths already confirmed to have the schema

_COLS = ("id, ts, level, message, read_ts, kind, symbol, title, "
         "dedupe_key, count, last_ts")


def _conn() -> sqlite3.Connection:
    from dashboard.core import paper
    db_path = str(paper._DB)
    c = sqlite3.connect(db_path, check_same_thread=False)
    if db_path not in _table_ready:
        c.execute("""CREATE TABLE IF NOT EXISTS changelog (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, level TEXT, message TEXT,
            read_ts TEXT DEFAULT NULL, kind TEXT DEFAULT NULL, symbol TEXT DEFAULT NULL,
            title TEXT DEFAULT NULL, dedupe_key TEXT DEFAULT NULL,
            count INTEGER DEFAULT 1, last_ts TEXT DEFAULT NULL,
            tier TEXT DEFAULT NULL)""")
        # Additive migrations for pre-existing DBs -- same pattern as paper.py's own
        # _MIGRATIONS. Unread is simply read_ts IS NULL; NOTHING auto-marks events read.
        cols = [r[1] for r in c.execute("PRAGMA table_info(changelog)").fetchall()]
        for col in ("read_ts", "kind", "symbol", "title", "dedupe_key",
                    "count", "last_ts", "tier"):
            if col not in cols:
                decl = {"count": "INTEGER DEFAULT 1"}.get(col, "TEXT DEFAULT NULL")
                c.execute(f"ALTER TABLE changelog ADD COLUMN {col} {decl}")
        c.execute("CREATE INDEX IF NOT EXISTS idx_changelog_dedupe "
                  "ON changelog(dedupe_key, last_ts)")
        _table_ready.add(db_path)
    return c


# ---- classification --------------------------------------------------------

_SELF_HEAL_PAT = re.compile(
    r"(auto-cancelled|re-?protected|auto-healed|ghost)", re.I)


def classify(level: str, message: str) -> str:
    """RED = needs attention (pushes). YELLOW = worth knowing. WHITE = log."""
    m = (message or "").lower()
    if level == "error":
        return "red"
    if any(k in m for k in ("2fa", "second factor", "dd_halt", "halt triggered",
                            "gateway down")):
        return "red"
    if level == "warning":
        return "yellow"
    # info-level exceptions that are still worth knowing
    if any(k in m for k in ("position closed", "deposit", "withdrawal", "cash flow",
                            "cancelled")):
        return "yellow"
    return "white"


def _kind_symbol(message: str) -> tuple[str, str | None]:
    """(kind, symbol) for dedupe grouping. Best-effort on free text."""
    m = message
    ml = m.lower()
    sym = None
    mm = re.match(r"^([A-Z][A-Z0-9]{1,9})\s*:", m)
    if mm:
        sym = mm.group(1)
    if "mismatch" in ml:
        return "reconcile-mismatch", sym
    if "auto-cancelled" in ml or "never filled" in ml:
        return "order-cancelled", sym
    if "new order placed" in ml or "new sleeve order placed" in ml:
        mo = re.search(r"placed:?\s*([A-Z][A-Z0-9]{1,9})\s*:", m)
        return "order-placed", (mo.group(1) if mo else sym)
    if "position closed" in ml:
        mo = re.search(r"closed:\s*([A-Z][A-Z0-9]{1,9})\b", m)
        return "position-closed", (mo.group(1) if mo else sym)
    if _SELF_HEAL_PAT.search(m):
        return "self-heal", sym
    if "tech investment" in ml:
        return "manual-toggle", None
    return "system", sym


def humanize(message: str) -> tuple[str, str]:
    """Plain-language (title, detail) for a raw event message. Falls back to a trimmed
    copy of the raw text -- raw is ALWAYS available via details expansion in the UI."""
    m = message or ""
    ml = m.lower()
    if "mismatch" in ml:
        syms = ", ".join(sorted(set(re.findall(r"[A-Z]{2,6}", m.split("only_local")[-1])))
                         ) if "only_local" in m else ""
        syms = syms or "unknown instruments"
        return (f"Records disagree ({syms})",
                "The journal has them, the broker doesn't -- usually an unfilled order.")
    mm = re.match(r"^([A-Z][A-Z0-9]{1,9})\s*:\s*(.*)$", m)
    if mm and "auto-cancelled" in ml:
        return f"{mm.group(1)} order cancelled", \
               "Broker never accepted the entry; cleaned up automatically."
    mo = re.match(r"^New (?:SLEEVE )?order placed:\s*([A-Z][A-Z0-9]{1,9})\s*:\s*(.*)$", m)
    if mo:
        side = "Sell" if "SELL" in mo.group(2).upper() else "buy"
        return f"{mo.group(1)} {side.lower()} order sent", mo.group(2)
    mp = re.match(r"^Position closed:\s*(.*)$", m)
    if mp:
        return "Position closed", mp.group(1)
    mh = re.match(r"^Flagged position auto-healed:\s*(.*)$", m)
    if mh:
        return "Position auto-healed", mh.group(1)
    mr = re.match(r"^Naked position re-protected:\s*(.*)$", m)
    if mr:
        return "Stop re-protected", mr.group(1)
    if "tech investment paused" in ml:
        return "Tech ETFs paused (by you)", "QQQ/XLK/SPY/EEM/ASHR get no new core entries"
    if "tech investment resumed" in ml:
        return "Tech ETFs resumed (by you)", "Core entries allowed again"
    title = m.strip()
    return ((title[:60] + "…") if len(title) > 60 else title), ""


def record(message: str, level: str = "info", kind: str | None = None,
           symbol: str | None = None, force_push: bool = False) -> None:
    """Record an event (as UNREAD), classified into a tier, deduped by key within the
    window. Push policy: ONLY red-tier events notify Telegram/ntfy (user-requested
    2026-08-26) -- yellow/white land in the local log only. Never raises."""
    ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    try:
        ev_kind, ev_sym = _kind_symbol(message)
        kind = kind or ev_kind
        symbol = symbol or ev_sym
        tier = classify(level, message)
        title, detail = humanize(message)
        dedupe_key = f"{kind}:{symbol or '-'}"

        from dashboard.core import paper
        with paper._LOCK, _conn() as c:
            window_cut = dt.datetime.now(dt.timezone.utc) - \
                dt.timedelta(minutes=DEDUPE_WINDOW_MIN)
            row = c.execute(
                "SELECT id, count, COALESCE(tier,'yellow') FROM changelog "
                "WHERE dedupe_key=? AND last_ts>=? ORDER BY id DESC LIMIT 1",
                (dedupe_key, window_cut.isoformat(timespec="seconds"))).fetchone()
            escalate = False
            if row:
                rid, prev_count, prev_tier = row[0], row[1] or 1, row[2]
                new_count = prev_count + 1
                new_tier = prev_tier
                if (kind == "reconcile-mismatch" and prev_tier != "red"
                        and new_count >= MISMATCH_ESCALATE_AFTER):
                    new_tier = "red"
                    escalate = True
                if tier == "red":
                    new_tier = "red"
                c.execute("UPDATE changelog SET count=?, last_ts=?, tier=? WHERE id=?",
                          (new_count, ts, new_tier, rid))
            else:
                c.execute(
                    "INSERT INTO changelog(ts, level, message, kind, symbol, title, "
                    "dedupe_key, count, last_ts, tier) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (ts, level, message, kind, symbol, title, dedupe_key, 1, ts, tier))

        if tier == "red" or escalate:
            try:
                from dashboard.core import notify
                notify.send(f"{title}" + (f" -- {detail}" if detail else ""),
                            level="error")
            except Exception as e:                  # noqa: BLE001
                log.debug("notable_events: notify failed: %s", e)
    except Exception as e:                      # noqa: BLE001
        log.debug("notable_events: record failed: %s", e)
        return
    (log.warning if tier in ("red", "yellow") and level != "info"
     else log.info)("EVENT[%s]: %s", tier, message)


def recent(limit: int = 20, tiers: list[str] | None = None) -> list[dict]:
    """Most recent incidents, newest activity first. Each dict carries id, ts, level
    (raw), tier, message (raw), title/detail (humanized), kind/symbol, count, read."""
    try:
        where = ""
        args: list = []
        if tiers:
            where = "WHERE tier IN (%s)" % ",".join("?" * len(tiers))
            args = list(tiers)
        with _conn() as c:
            cur = c.execute(
                f"SELECT {_COLS}, COALESCE(tier,'yellow') AS tier_eff FROM changelog "
                f"{where} ORDER BY COALESCE(last_ts, ts) DESC LIMIT ?", (*args, limit))
            out = []
            for r in cur.fetchall():
                keys = [k.strip() for k in _COLS.split(",")] + ["tier_eff"]
                d = dict(zip(keys, r))
                title, detail = humanize(d["message"])
                d["title"] = d["title"] or title
                d["detail"] = detail
                d["count"] = d["count"] or 1
                d["read"] = d["read_ts"] is not None
                out.append(d)
            return out
    except Exception as e:                       # noqa: BLE001
        log.debug("notable_events: recent() read failed: %s", e)
        return []


def unread_count(tier: str | None = None) -> int:
    """Unread rows, optionally restricted to one tier (bell counts 'red' only)."""
    try:
        with _conn() as c:
            if tier:
                row = c.execute("SELECT COUNT(*) FROM changelog WHERE read_ts IS NULL "
                                "AND COALESCE(tier,'yellow')=?", (tier,)).fetchone()
            else:
                row = c.execute("SELECT COUNT(*) FROM changelog WHERE read_ts IS NULL")\
                    .fetchone()
            return int(row[0]) if row else 0
    except Exception as e:                       # noqa: BLE001
        log.debug("notable_events: unread_count() failed: %s", e)
        return 0


def mark_read(event_id: int) -> None:
    """Mark ONE incident read by id (covers its whole dedupe group)."""
    try:
        from dashboard.core import paper
        with paper._LOCK, _conn() as c:
            c.execute("UPDATE changelog SET read_ts = ? "
                      "WHERE id = ? AND read_ts IS NULL",
                      (dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                       event_id))
    except Exception as e:                       # noqa: BLE001
        log.debug("notable_events: mark_read(%s) failed: %s", event_id, e)


def mark_all_read() -> None:
    """Explicitly mark EVERY event read -- a deliberate user action on the Alerts tab,
    never something the system does as a side effect of opening anything."""
    try:
        from dashboard.core import paper
        with paper._LOCK, _conn() as c:
            c.execute("UPDATE changelog SET read_ts = ? WHERE read_ts IS NULL",
                      (dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),))
    except Exception as e:                       # noqa: BLE001
        log.debug("notable_events: mark_all_read() failed: %s", e)
