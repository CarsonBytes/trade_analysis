"""Best-effort cross-project LLM usage logging to the shared Supabase
`llm_calls` table -- the same one D:\\adaptive_study_platform and
D:\\event-radar write to, so usage against the shared chatanywhere.tech
key is visible in one place. Never raises: a logging hiccup must never
affect analysis or trading decisions.
"""
import datetime as dt
import os
import time

import httpx

from analyst import supabase_meter  # per-endpoint REST counts (memory-only unless SUPABASE_METER_FILE is set)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
supabase_meter.configure(app="quant")

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


def log_usage(kind: str, model: str, input_tokens: int, output_tokens: int, latency_ms: int,
             provider: str = "chatanywhere") -> None:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return
    try:
        in_price, out_price = _PRICING.get(model, _DEFAULT_PRICING)
        cost_usd = (input_tokens / 1_000_000) * in_price + (output_tokens / 1_000_000) * out_price
        # RESTORED 2026-07-28: the project/call_type/environment/provider columns were
        # reverted 2026-07-18 because they 400'd against the live table at the time (the
        # migration adding them hadn't been run yet). It has since landed -- confirmed live
        # via direct PostgREST query 2026-07-28 -- so readers that key off the real columns
        # (rather than parsing the `purpose` prefix) now see this project's rows again.
        # `environment` is filled from DASH_FIXED_MODE when the launch script pins one
        # (dashboard.ps1='paper', run_dashboard_live.ps1='live'); falls back to whatever
        # store.get_mode() would resolve to isn't worth the import here, so unset -> None.
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
                "environment": os.environ.get("DASH_FIXED_MODE"),
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
    empty = {"calls": 0, "cost_usd": 0.0, "calls_by_project": {}, "ok": False}
    now = time.time()
    if now - _shared_usage_cache["ts"] < _SHARED_USAGE_CACHE_SEC and _shared_usage_cache["data"]:
        return _shared_usage_cache["data"]
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
            params={"select": "total_calls,total_cost_usd,calls_by_project", "day": f"eq.{today}"},
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
        result = {"calls": 0, "cost_usd": 0.0, "calls_by_project": {}, "ok": True}
    else:
        row = rows[0]
        result = {"calls": row.get("total_calls") or 0,
                  "cost_usd": row.get("total_cost_usd") or 0.0,
                  "calls_by_project": row.get("calls_by_project") or {},
                  "ok": True}
    _shared_usage_cache["ts"] = now
    _shared_usage_cache["data"] = result
    return result


def _fetch_shared_usage_today_from_raw_rows(headers: dict, now: float) -> dict:
    """Pre-migration fallback: the original approach, summing every row created
    today client-side -- real cost scales with today's row count, which is
    exactly what llm_daily_summary's trigger exists to replace. Kept only so
    fetch_shared_usage_today() doesn't break for a deployment whose Supabase
    project hasn't run 001_llm_daily_summary.sql yet."""
    empty = {"calls": 0, "cost_usd": 0.0, "calls_by_project": {}, "ok": False}
    today_start = _hkt_today_start_utc().isoformat() + "Z"
    try:
        resp = httpx.get(
            f"{SUPABASE_URL}/rest/v1/llm_calls",
            headers=headers,
            params={"select": "purpose,cost_usd,created_at", "created_at": f"gte.{today_start}"},
            timeout=10,
        )
        supabase_meter.record("GET", "llm_calls", supabase_meter.response_bytes(resp))
        resp.raise_for_status()
        rows = resp.json()
    except Exception:
        return empty

    calls_by_project: dict[str, int] = {}
    total_cost = 0.0
    for row in rows:
        project = _project_of(row.get("purpose") or "")
        calls_by_project[project] = calls_by_project.get(project, 0) + 1
        total_cost += row.get("cost_usd") or 0.0

    result = {"calls": len(rows), "cost_usd": total_cost, "calls_by_project": calls_by_project, "ok": True}
    _shared_usage_cache["ts"] = now
    _shared_usage_cache["data"] = result
    return result


def shared_calls_ok(cap: int = 200, reserve: int = 10) -> tuple[bool, int | None]:
    """(is_it_safe_to_call, shared_calls_today_or_None). Fails CLOSED: if the
    shared ledger can't be reached, returns (False, None) rather than treating
    an unreachable fetch as "0 calls, all clear" -- see store.py::can_call()
    for why that's the safe default here (skipping one board-scan cycle is
    free; silently overrunning the shared cap is not)."""
    usage = fetch_shared_usage_today()
    if not usage["ok"]:
        return False, None
    return usage["calls"] < (cap - reserve), usage["calls"]
