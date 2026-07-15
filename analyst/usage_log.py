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

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

# Per-MTok pricing (USD), input/output -- rough reference only, mirrors
# event-radar's llm_logging.py. Routed through a third-party proxy, so this
# won't match official OpenAI billing exactly.
_PRICING = {"gpt-5-mini": (0.25, 2.00)}
_DEFAULT_PRICING = (0.50, 1.50)


def log_usage(kind: str, model: str, input_tokens: int, output_tokens: int, latency_ms: int) -> None:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return
    try:
        in_price, out_price = _PRICING.get(model, _DEFAULT_PRICING)
        cost_usd = (input_tokens / 1_000_000) * in_price + (output_tokens / 1_000_000) * out_price
        httpx.post(
            f"{SUPABASE_URL}/rest/v1/llm_calls",
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
            json={
                "purpose": f"quant:{kind}",
                "model": model,
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "cost_usd": round(cost_usd, 6),
                "latency_ms": latency_ms,
            },
            timeout=5,
        )
    except Exception:
        pass  # telemetry only -- never let this affect the trading pipeline


# ADDED 2026-07-15: quant's OWN "API calls today: X/200" counter (dashboard/core/store.py's
# calls_today()) only counts THIS instance's own calls -- it is NOT the real constraint. The
# 200/day cap is on the shared chatanywhere.tech key, consumed by quant (paper AND live are
# separate counters!) + event-radar + the study platform together. This is exactly why the
# 2026-07-14 rate-limit incident happened: the local counter said "under budget" while the
# real, shared quota was already exhausted by other callers. Mirrors event-radar's
# fetch_shared_usage_today() (backend/app/llm_logging.py) exactly -- same UTC-day boundary,
# same aggregation shape -- so all three projects can display/reason about the identical number.
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
    """Cross-project usage for today (UTC), from the shared Supabase ledger. Best-effort:
    returns zeros if Supabase isn't configured/unreachable. Cached for _SHARED_USAGE_CACHE_SEC."""
    empty = {"calls": 0, "cost_usd": 0.0, "calls_by_project": {}}
    now = time.time()
    if now - _shared_usage_cache["ts"] < _SHARED_USAGE_CACHE_SEC and _shared_usage_cache["data"]:
        return _shared_usage_cache["data"]
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return empty

    today_start = dt.datetime.combine(dt.datetime.utcnow().date(), dt.time.min).isoformat() + "Z"
    try:
        resp = httpx.get(
            f"{SUPABASE_URL}/rest/v1/llm_calls",
            headers={
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            },
            params={"select": "purpose,cost_usd,created_at", "created_at": f"gte.{today_start}"},
            timeout=10,
        )
        resp.raise_for_status()
        rows = resp.json()
    except Exception:
        return empty   # best-effort -- a stale/zeroed display beats a broken tick

    calls_by_project: dict[str, int] = {}
    total_cost = 0.0
    for row in rows:
        project = _project_of(row.get("purpose") or "")
        calls_by_project[project] = calls_by_project.get(project, 0) + 1
        total_cost += row.get("cost_usd") or 0.0

    result = {"calls": len(rows), "cost_usd": total_cost, "calls_by_project": calls_by_project}
    _shared_usage_cache["ts"] = now
    _shared_usage_cache["data"] = result
    return result
