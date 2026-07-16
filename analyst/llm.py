"""LLM factory. Chatanywhere.tech (free tier, shared across quant/study/
event-radar) first; once the shared 200/day quota is used up, DeepSeek.

The chatanywhere-vs-DeepSeek decision is centralized in a Supabase Edge
Function (provider-decision) rather than decided locally -- ADDED 2026-07-16,
after the 2026-07-14 incident where quant's own local call counter said
"under budget" while the REAL shared quota (consumed by quant+study+events
together) was already exhausted. Asking the shared ledger directly means
every project switches to DeepSeek at the same moment, not just whichever
one happens to notice first.
"""
from __future__ import annotations

import os
import ssl
import threading
import time
import httpx
import truststore
from langchain_openai import ChatOpenAI

# This machine runs AV (AVG) that intercepts HTTPS and re-signs certs with a
# local root trusted by Windows but NOT by certifi. So verify against the OS
# trust store, which contains that root. TLS verification stays fully ON --
# we trust exactly what Windows trusts, nothing weakened.
_SSL_CTX = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
_HTTP_CLIENT = httpx.Client(verify=_SSL_CTX, timeout=60.0)

_DECISION_CACHE_SEC = 60.0    # matches usage_log.py's own TTL -- this is a budget
                              # decision, not something that needs sub-minute freshness
_decision_cache: dict = {"ts": 0.0, "provider": None}

# Tracks which provider the MOST RECENT make_llm() call actually used (not just what
# provider_decision() said -- if DEEPSEEK_API_KEY isn't configured locally, make_llm()
# stays on chatanywhere even when the shared decision says "deepseek", so this can
# legitimately differ from provider_decision()'s raw answer). The caller (board_scan.py)
# reads this via last_provider_used() to log usage against the provider actually billed,
# not the one that was merely recommended. thread-local (not a plain module global):
# make_llm() has 2 call sites (board_scan.py, analyst/nodes.py) and nothing here
# guarantees they never run on different threads -- a bare global would let one
# thread's decision get overwritten by another's between make_llm() and the caller
# reading it back.
_tls = threading.local()


def last_provider_used() -> str:
    """Which provider the most recent make_llm() call (on THIS thread) actually used.
    Call this AFTER make_llm() (and ideally after the LLM call itself succeeds) --
    see board_scan.py."""
    return getattr(_tls, "provider", "chatanywhere")


def last_model_used() -> str:
    """Which model the most recent make_llm() call (on THIS thread) actually used --
    the OPENAI_MODEL/DEEPSEEK_MODEL env default, or an explicit `model=` override,
    whichever make_llm() actually picked."""
    return getattr(_tls, "model", "")


def provider_decision(cap: int = 200, reserve: int = 10) -> str:
    """Ask the shared Edge Function which provider to use right now.
    Fails OPEN to "chatanywhere" (today's existing behavior) on ANY problem --
    edge function not yet deployed, Supabase unreachable, timeout, missing
    config -- so this is purely additive: if nothing is set up, behavior is
    unchanged from before this existed. Cached briefly so a burst of calls
    (e.g. several instruments scored in a row) doesn't hit the network per call."""
    now = time.time()
    if now - _decision_cache["ts"] < _DECISION_CACHE_SEC and _decision_cache["provider"]:
        return _decision_cache["provider"]

    supabase_url = os.environ.get("SUPABASE_URL")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not supabase_url or not service_key:
        return "chatanywhere"
    try:
        resp = httpx.get(
            f"{supabase_url}/functions/v1/provider-decision",
            params={"cap": cap, "reserve": reserve},
            headers={"Authorization": f"Bearer {service_key}"},
            timeout=3.0,
        )
        resp.raise_for_status()
        provider = resp.json().get("provider", "chatanywhere")
    except Exception as e:                      # noqa: BLE001
        from dashboard.core.log import log      # local import: analyst/ stays import-safe
                                                  # standalone from dashboard/ at module-load
                                                  # time (see features.py's same pattern)
        log.debug("provider_decision: unreachable, defaulting to chatanywhere: %s", e)
        return "chatanywhere"
    _decision_cache["ts"] = now
    _decision_cache["provider"] = provider
    return provider


def make_llm(temperature: float = 0.2, model: str | None = None) -> ChatOpenAI:
    """Return a chat model. Low temperature: we want consistent, auditable
    analysis, not creative writing.

    Env:
      OPENAI_API_KEY    (required for chatanywhere)
      OPENAI_MODEL      (optional, default gpt-4o-mini)
      OPENAI_BASE_URL   (optional, e.g. point at a compatible endpoint)
      DEEPSEEK_API_KEY  (optional -- enables the fallback; without it, always
                         stays on chatanywhere regardless of provider_decision())
      DEEPSEEK_MODEL    (optional, default deepseek-chat)
    """
    provider = "chatanywhere"
    if os.environ.get("DEEPSEEK_API_KEY"):
        provider = provider_decision()

    if provider == "deepseek" and os.environ.get("DEEPSEEK_API_KEY"):
        model = model or os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
        kwargs = {
            "model": model,
            "http_client": _HTTP_CLIENT,
            "api_key": os.environ["DEEPSEEK_API_KEY"],
            "base_url": "https://api.deepseek.com",
            "temperature": temperature,
        }
        _tls.provider = "deepseek"
        _tls.model = model
        return ChatOpenAI(**kwargs)

    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError(
            "OPENAI_API_KEY not set. Put it in analyst/.env or your environment."
        )
    model = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    kwargs = {"model": model, "http_client": _HTTP_CLIENT}
    # gpt-5 / o-series reasoning models only accept the default temperature.
    if not any(model.startswith(p) for p in ("gpt-5", "o1", "o3", "o4")):
        kwargs["temperature"] = temperature
    if os.environ.get("OPENAI_BASE_URL"):
        kwargs["base_url"] = os.environ["OPENAI_BASE_URL"]
    _tls.provider = "chatanywhere"
    _tls.model = model
    return ChatOpenAI(**kwargs)
