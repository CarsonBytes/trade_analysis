"""Best-effort cross-project LLM usage logging to the shared Supabase
`llm_calls` table -- the same one D:\\adaptive_study_platform and
D:\\event-radar write to, so usage against the shared chatanywhere.tech
key is visible in one place. Never raises: a logging hiccup must never
affect analysis or trading decisions.
"""
import datetime as dt
import os
import pathlib
import sqlite3
import time

import httpx

from analyst import supabase_meter  # per-endpoint REST counts (memory-only unless SUPABASE_METER_FILE is set)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
# Meter label (spec 2026-09-20): explicit SUPABASE_METER_APP wins (both
# compose files set it: quant-paper / quant-live); otherwise derive from
# DASH_FIXED_MODE, defaulting to paper -- same default as
# dashboard/core/mode.py::resolve_mode(). Paper and live used to share one
# `quant` label, making their egress indistinguishable in the rollup.
_meter_app = os.environ.get("SUPABASE_METER_APP") or (
    "quant-live" if (os.environ.get("DASH_FIXED_MODE") or "").strip().lower() == "live"
    else "quant-paper"
)
supabase_meter.configure(app=_meter_app)

# FIXED 2026-07-24: fetch_shared_usage_today() used a UTC day boundary, deliberately
# "mirroring event-radar's fetch_shared_usage_today() exactly" per that function's own
# comment -- but event-radar's OWN version was itself fixed to HKT on 2026-07-21 (the shared
# chatanywhere.tech key's daily quota resets on HKT's day boundary, its owner being HK-based,
# not UTC's), and this copy was never updated to match. Since this drifted OUT of sync with
# the thing it explicitly says it mirrors, quant + event-radar would disagree on "today"'s
# shared usage for the 8h window between UTC midnight and HKT midnight (08:00-16:00 UTC) --
# exactly the kind of inconsistency the shared-quota guard exists to prevent. Ported
# event-radar's hkt_today_start_utc() verbatim (backend/app/llm_logging.py) rather than
# reinventing it slightly differently a second time.
HKT = dt.timezone(dt.timedelta(hours=8))


def _hkt_today_start_utc() -> dt.datetime:
    """Naive-UTC instant of the most recent HKT midnight. HKT has no DST, always UTC+8, so
    no zoneinfo/tzdata dependency is needed."""
    hkt_midnight = dt.datetime.now(HKT).replace(hour=0, minute=0, second=0, microsecond=0)
    return hkt_midnight.astimezone(dt.timezone.utc).replace(tzinfo=None)

# Per-MTok pricing (USD), input/output -- rough reference only, mirrors
# event-radar's llm_logging.py. Routed through a third-party proxy, so this
# won't match official OpenAI billing exactly.
_PRICING = {"gpt-5-mini": (0.25, 2.00)}
_DEFAULT_PRICING = (0.50, 1.50)


def _mode_db_path() -> pathlib.Path:
    """Fixed location of the mode pointer DB (mirrors dashboard/core/store.py's
    _MODE_DB without importing dashboard -- analyst/ must stay import-safe
    standalone, see analyst/llm.py's notes on the same constraint)."""
    return pathlib.Path(__file__).resolve().parents[1] / "dashboard" / "dashboard_mode.db"


def _resolve_environment() -> str:
    """Attribution for the `environment` ledger column. Never returns None/empty.

    FIXED 2026-09-16: rows logged from any process that doesn't pin DASH_FIXED_MODE
    (analyst CLI runs, backtests, ad-hoc scripts -- and any future failure-path
    log call) landed with environment=NULL, surfacing as unattributable "unset"
    rows in the usage dashboard. Chain is now: DASH_FIXED_MODE env var ->
    persisted mode pointer (the same file store.get_mode() reads) -> "unknown"
    literal. "unknown" is deliberately a real string, not NULL, so it aggregates
    as its own visible bucket instead of silently merging into other projects'
    legitimately-environmentless rows.
    """
    env = (os.environ.get("DASH_FIXED_MODE") or "").strip().lower()
    if env:
        return env
    try:
        db = _mode_db_path()
        if db.exists():
            with sqlite3.connect(db) as c:
                row = c.execute("SELECT v FROM mode WHERE k='dash_mode'").fetchone()
                if row and (row[0] or "").strip():
                    return row[0].strip().lower()
    except Exception:
        pass
    return "unknown"


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Per-MTok reference pricing shared by log_usage() and the local
    scan_metrics ledger -- one definition so the two can never drift apart."""
    in_price, out_price = _PRICING.get(model, _DEFAULT_PRICING)
    return round((input_tokens / 1_000_000) * in_price
                 + (output_tokens / 1_000_000) * out_price, 6)


def log_usage(kind: str, model: str, input_tokens: int, output_tokens: int, latency_ms: int,
             provider: str = "chatanywhere") -> None:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return
    try:
        cost_usd = estimate_cost(model, input_tokens, output_tokens)
        # RESTORED 2026-07-28: the project/call_type/environment/provider columns were
        # reverted 2026-07-18 because they 400'd against the live table at the time (the
        # migration adding them hadn't been run yet). It has since landed -- confirmed live
        # via direct PostgREST query 2026-07-28 -- so readers that key off the real columns
        # (rather than parsing the `purpose` prefix) now see this project's rows again.
        # `environment` goes through _resolve_environment() (2026-09-16: was a bare
        # os.environ.get("DASH_FIXED_MODE"), so any unpinned process logged NULL).
        resp = httpx.post(
            f"{SUPABASE_URL}/rest/v1/llm_calls",
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
            json={
                "purpose": f"quant:{kind}",
                "project": "quant",
                "call_type": kind,
                "provider": provider,
                "environment": _resolve_environment(),
                "model": model,
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "cost_usd": round(cost_usd, 6),
                "latency_ms": latency_ms,
            },
            timeout=5,
        )
        supabase_meter.record("POST", "llm_calls", supabase_meter.response_bytes(resp))
    except Exception:
        pass  # telemetry only -- never let this affect the trading pipeline


# ADDED 2026-07-15: quant's OWN "API calls today: X/200" counter (dashboard/core/store.py's
# calls_today()) only counts THIS instance's own calls -- it is NOT the real constraint. The
# 200/day cap is on the shared chatanywhere.tech key, consumed by quant (paper AND live are
# separate counters!) + event-radar + the study platform together. This is exactly why the
# 2026-07-14 rate-limit incident happened: the local counter said "under budget" while the
# real, shared quota was already exhausted by other callers. Mirrors event-radar's
# fetch_shared_usage_today() (backend/app/llm_logging.py) exactly -- same HKT-day boundary
# (fixed 2026-07-24, see note above), same aggregation shape -- so all three projects can
# display/reason about the identical number.
_shared_usage_cache: dict = {"ts": 0.0, "data": None}
_SHARED_USAGE_CACHE_SEC = 60.0   # a dashboard-render-triggered fetch must never hit Supabase
                                 # on every request (see quant's own account_summary() TTL
                                 # fix, same class of bug) -- this is a background/periodic
                                 # read, not something latency-sensitive


def _project_of(purpose: str) -> str:
    """Rows are tagged '{project}:{kind}'. Legacy/unprefixed rows predate this
    convention and were all written by study (the table's original owner)."""
    if purpose.startswith("events:"):
        return "events"
    if purpose.startswith("quant:"):
        return "quant"
    return "study"


def fetch_shared_usage_today() -> dict:
    """Cross-project usage for today (HKT), from the shared Supabase ledger. Best-effort:
    returns zeros (with "ok": False) if Supabase isn't configured/unreachable, so a caller
    that needs to tell "genuinely 0 calls" apart from "couldn't check" can (see
    shared_calls_ok(), used by the board-scan budget guard). Cached for _SHARED_USAGE_CACHE_SEC.

    ADDED 2026-09-13: reads the one-row `llm_daily_summary` (kept current by a
    trigger on every llm_calls insert -- see
    D:\\llm-usage-dashboard\\migrations\\001_llm_daily_summary.sql) instead of
    fetching every raw row created today and summing them here -- the same swap
    event-radar's backend/app/llm_logging.py::fetch_shared_usage_today() already
    made. The old query's cost scaled with today's row count, paid on every
    poll past the 60s cache. Falls back to the raw-row method if the summary
    table doesn't exist yet (404), so this keeps working before that migration
    has been run."""
    empty = {"calls": 0, "cost_usd": 0.0, "calls_by_project": {},
              "chatanywhere_calls": 0, "ok": False}
    now = time.time()
    if now - _shared_usage_cache["ts"] < _SHARED_USAGE_CACHE_SEC and _shared_usage_cache["data"]:
        return _shared_usage_cache["data"]
    # ADDED 2026-09-17 (cross-instance sharing): paper and live poll the
    # IDENTICAL summary row -- whoever fetched last shares it via the shared
    # volume, halving these reads. Same TTL as the memory cache, same shape,
    # same fail-open behavior (a miss just falls through to the network).
    try:
        from dashboard.core import shared_cache  # local import: analyst/ stays
                                                 # import-safe standalone
        _shared_hit, _ = shared_cache.read(shared_cache.USAGE_KEY, _SHARED_USAGE_CACHE_SEC)
        if isinstance(_shared_hit, dict) and isinstance(_shared_hit.get("result"), dict):
            _shared_usage_cache["ts"] = now
            _shared_usage_cache["data"] = _shared_hit["result"]
            return _shared_hit["result"]
    except Exception:
        pass
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return empty

    headers = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
    }
    # NOT _hkt_today_start_utc().date() -- that instant is naive UTC (HKT
    # midnight is 16:00 the PREVIOUS UTC calendar day), so its .date() is one
    # day behind the HKT calendar day it anchors (found live in event-radar
    # 2026-09-12: every call read yesterday's summary row). Compute the HKT
    # calendar date in the HKT zone directly, same as the migration's trigger.
    today = dt.datetime.now(HKT).date().isoformat()
    try:
        resp = httpx.get(
            f"{SUPABASE_URL}/rest/v1/llm_daily_summary",
            headers=headers,
            params={"select": "total_calls,total_cost_usd,calls_by_project,chatanywhere_calls",
                    "day": f"eq.{today}"},
            timeout=10,
        )
        supabase_meter.record("GET", "llm_daily_summary", supabase_meter.response_bytes(resp))
        if getattr(resp, "status_code", 200) == 404:
            return _fetch_shared_usage_today_from_raw_rows(headers, now)
        resp.raise_for_status()
        rows = resp.json()
    except Exception:
        return empty   # best-effort -- a stale/zeroed display beats a broken tick

    if not rows:
        # Table exists but nothing logged yet today (e.g. just past HKT
        # midnight) -- an empty set, NOT a missing table, so no raw fallback.
        result = {"calls": 0, "cost_usd": 0.0, "calls_by_project": {},
                  "chatanywhere_calls": 0, "ok": True}
    else:
        row = rows[0]
        result = {"calls": row.get("total_calls") or 0,
                  "cost_usd": row.get("total_cost_usd") or 0.0,
                  "calls_by_project": row.get("calls_by_project") or {},
                  # FIXED 2026-09-15: shared_calls_ok() gates against the
                  # chatanywhere.tech 200/day quota specifically -- "calls"
                  # (every provider combined) overcounts it the moment any
                  # project logs a non-chatanywhere call, same fix study-
                  # platform's core/llm.py already made. Falls back to
                  # "calls" only if this summary row predates migration 003
                  # (no chatanywhere_calls column yet) rather than silently
                  # reading None as 0 and under-gating.
                   "chatanywhere_calls": row.get("chatanywhere_calls")
                       if row.get("chatanywhere_calls") is not None else row.get("total_calls") or 0,
                   "ok": True}
    _shared_usage_cache["ts"] = now
    _shared_usage_cache["data"] = result
    _publish_shared_usage(result)
    return result


def _fetch_shared_usage_today_from_raw_rows(headers: dict, now: float) -> dict:
    """Pre-migration fallback: the original approach, summing every row created
    today client-side -- real cost scales with today's row count, which is
    exactly what llm_daily_summary's trigger exists to replace. Kept only so
    fetch_shared_usage_today() doesn't break for a deployment whose Supabase
    project hasn't run 001_llm_daily_summary.sql yet."""
    empty = {"calls": 0, "cost_usd": 0.0, "calls_by_project": {},
             "chatanywhere_calls": 0, "ok": False}
    today_start = _hkt_today_start_utc().isoformat() + "Z"
    try:
        resp = httpx.get(
            f"{SUPABASE_URL}/rest/v1/llm_calls",
            headers=headers,
            params={"select": "purpose,project,provider,cost_usd,created_at",
                    "created_at": f"gte.{today_start}"},
            timeout=10,
        )
        supabase_meter.record("GET", "llm_calls", supabase_meter.response_bytes(resp))
        resp.raise_for_status()
        rows = resp.json()
    except Exception:
        return empty

    calls_by_project: dict[str, int] = {}
    total_cost = 0.0
    chatanywhere_calls = 0
    for row in rows:
        purpose = row.get("purpose") or ""
        project = row.get("project") or _project_of(purpose)
        calls_by_project[project] = calls_by_project.get(project, 0) + 1
        total_cost += row.get("cost_usd") or 0.0
        # Same precedence as the 003 migration's trigger / study-platform's
        # core/llm.py reader: an explicitly-set provider is authoritative;
        # otherwise the project heuristic (quant/events rows are always via
        # the shared proxy key) plus study's own embed calls.
        provider = row.get("provider") or ""
        if provider == "chatanywhere" or (not provider and (
                project in ("quant", "events") or purpose.startswith("study:embed"))):
            chatanywhere_calls += 1

    result = {"calls": len(rows), "cost_usd": total_cost, "calls_by_project": calls_by_project,
              "chatanywhere_calls": chatanywhere_calls, "ok": True}
    _shared_usage_cache["ts"] = now
    _shared_usage_cache["data"] = result
    _publish_shared_usage(result)
    return result


def _publish_shared_usage(result: dict) -> None:
    """Write-through for the cross-instance usage cache (2026-09-17). Never
    raises -- sharing is best-effort; a failed publish just means the other
    instance fetches over the network as it always has."""
    try:
        from dashboard.core import shared_cache  # local import: see above
        shared_cache.write(shared_cache.USAGE_KEY, {"result": result})
    except Exception:
        pass


def shared_calls_ok(cap: int = 200, reserve: int = 10) -> tuple[bool, int | None]:
    """(is_it_safe_to_call, shared_calls_today_or_None). Fails CLOSED: if the
    shared ledger can't be reached, returns (False, None) rather than treating
    an unreachable fetch as "0 calls, all clear" -- see store.py::can_call()
    for why that's the safe default here (skipping one board-scan cycle is
    free; silently overrunning the shared cap is not).

    FIXED 2026-09-15: `cap` is chatanywhere.tech's own 200/day free-tier
    limit -- gating on usage["calls"] (every provider combined: DeepSeek,
    Anthropic, etc.) overcounts it and would trip this guard on traffic that
    doesn't touch that quota at all. usage["chatanywhere_calls"] is the
    number that actually matters, same fix study-platform's core/llm.py
    already made for its own display. Harmless no-op today (100% of current
    traffic is chatanywhere, so the two numbers are identical), but wrong on
    the day any project logs a non-chatanywhere call."""
    usage = fetch_shared_usage_today()
    if not usage["ok"]:
        return False, None
    return usage["chatanywhere_calls"] < (cap - reserve), usage["chatanywhere_calls"]
