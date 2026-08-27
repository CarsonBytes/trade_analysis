"""Batched LLM board scan: ONE call analyses every instrument at once.

This is the budget-critical design. Instead of 4 calls x N instruments, the
whole board costs a single structured-output call. The deterministic scorer has
already done the ranking for free; the LLM adds judgement, news synthesis and an
explicit invalidation level per instrument.

Respects the daily budget guard: if we're near the cap, it returns None and the
UI keeps showing deterministic data only.
"""
from __future__ import annotations

from typing import Literal

from dashboard.core import net  # noqa: F401
from pydantic import BaseModel, Field

from analyst.llm import invoke_with_key_fallback, is_chatanywhere_unavailable  # from quant/analyst
from dashboard.core import store
from dashboard.core.scoring import Score


class InstrumentSignal(BaseModel):
    key: str = Field(description="instrument key, exactly as given")
    bias: Literal["bullish", "bearish", "neutral"]
    action: Literal["BUY", "SELL", "WAIT"]
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(description="1-2 sentences grounded in the provided facts/news.")
    macro_linkage: str = Field(description=
        "Does any theme from YOUR OWN macro_note actually apply to THIS instrument "
        "specifically (e.g. a USD-strength headwind on metals, a shared commodity-complex "
        "driver, risk-off FX flows)? One short sentence, and be concrete about the "
        "MECHANISM (not just 'macro is risk-on') -- e.g. copper isn't necessarily bearish "
        "just because oil spiked on a supply shock, but IS exposed if that same shock is "
        "driving safe-haven USD strength. Say 'none material' if nothing genuinely "
        "connects -- don't force a link that isn't really there.")
    invalidation: str = Field(description="specific price/condition that proves this wrong.")


class BoardScan(BaseModel):
    macro_note: str = Field(description="2-3 sentences on the overall macro/risk backdrop.")
    signals: list[InstrumentSignal]


# ADDED 2026-07-14: macro_linkage field + this paragraph, after a real trade (CPER, placed
# 2026-07-13) got a purely technical rationale ("uptrend, momentum favors continuation")
# despite the SAME board scan's own macro_note flagging Iran/Middle-East tension driving
# safe-haven USD strength -- a real, statistically-supported headwind for copper (-0.54
# correlation with DXY over the trailing 2mo, confirmed against real data) that never made it
# into the per-instrument reasoning. The LLM was identifying macro themes at the board level
# but not systematically checking whether they applied to each instrument it scored --
# forcing a dedicated field (rather than hoping the free-text rationale mentions it) makes
# this reliable and auditable instead of hopeful.
SYSTEM = (
    "You are the head analyst on a trading desk. You are given pre-computed, "
    "factual indicators for several instruments (metals, energy, FX, indices, "
    "crypto) plus recent "
    "headlines. Do NOT invent numbers; reason only from the facts provided. "
    "First form the macro_note (2-3 sentences on the overall backdrop). THEN, for each "
    "instrument, give a bias, an action (BUY/SELL/WAIT), a calibrated confidence, a "
    "one-line rationale, an explicit macro_linkage (does any theme from your OWN "
    "macro_note actually apply to THIS instrument, through what mechanism -- or "
    "genuinely nothing? Say so either way, don't skip this step even when the answer is "
    "'none material'), and the explicit invalidation level. "
    "WAIT is correct when signals conflict or a trend is overextended. Only "
    "count headlines actually relevant to an instrument. You advise a human who "
    "makes the final call -- never overstate confidence."
)


def _facts_block(scores: list[Score]) -> str:
    blocks = []
    for s in scores:
        blocks.append(
            f"### {s.key}  (deterministic: {s.signal}, dir {s.direction}, "
            f"strength {s.strength}/5)\n{s.facts_text}"
        )
    return "\n\n".join(blocks)


# FIXED 2026-07-13: this cap's own assumption ("the rest are clear WAIT/WATCH") is false --
# checked directly against a real day's data: EFA/HYD/HYG/SHY all had a real deterministic
# BUY/SELL that day (rejected on a DIFFERENT gate, trend-strength/RSI) but weren't in the
# top-10 sent here, so they got evaluated with NO llm_sig at all (see evaluate_signal() in
# core/paper.py -- action falls back to the deterministic signal, with none of the LLM's
# news-awareness or "signals conflict/overextended" judgment applied). The original "~4k free
# tier" token concern doesn't apply to this deployment's actual configured model
# (OPENAI_MODEL=gpt-5-mini, a large context window) -- 22 instruments' worth of facts_text
# plus headlines is a small fraction of it. Raised to cover the full active ETF universe (22
# today) with headroom for growth, so every watched instrument gets a real LLM look every
# scan, not just the most "obvious" 10. Cost is still bounded by store.can_call()'s daily
# call-COUNT budget (unaffected by per-call size) -- this doesn't add calls, just completeness
# within the one call already being made.
MAX_INSTRUMENTS = 40
MAX_NEWS = 10


# FIXED 2026-07-14: found live (both instances share one OpenAI-compatible API key/quota)
# hammering a THIRD-PARTY free-tier daily limit (chatanywhere.tech, 200 req/day, resets at
# the provider's local midnight) every single tick cycle once exhausted -- 876 identical
# `openai.RateLimitError` failures logged in under a few hours, each one a real, slow
# network round-trip that failed anyway. `store.can_call(cap=cap)`'s own internal counter
# didn't prevent this: it's tracked PER-INSTANCE (paper/live each keep their own count), but
# the real quota is shared account-wide across BOTH, so each instance's own counter can sit
# well under its configured cap while the SHARED provider-side quota is already exhausted --
# the internal budget guard and the real external limit can disagree. Confirmed this was
# real, ongoing degradation (checked timestamps: every ~15-30s, matching the tick cadence)
# during a routine response-time check for an unrelated change, not something invented.
_RATE_LIMIT_BACKOFF_KEY = "llm_rate_limited_until"
_CST = __import__("datetime").timezone(__import__("datetime").timedelta(hours=8))


def _rate_limited_until() -> str | None:
    cached, _ = store.cache_get(_RATE_LIMIT_BACKOFF_KEY)
    return cached


def _clear_backoff() -> None:
    """Clear the backoff so the next run_board_scan() attempt is unrestricted."""
    store.cache_set(_RATE_LIMIT_BACKOFF_KEY, None)


def _set_rate_limit_backoff() -> None:
    """Back off until the next provider reset (00:00 CST / 16:00 UTC).
    The provider's own message says "请00:00后再试" -- that's Beijing time (UTC+8),
    NOT UTC midnight. The previous code used UTC midnight, extending the blackout
    by an unnecessary 8 hours."""
    import datetime as _dt
    now_utc = _dt.datetime.now(_dt.timezone.utc)
    # Provider resets at 00:00 CST = 16:00 UTC
    # If it's already past 16:00 UTC today, the reset already happened; back off
    # to tomorrow's 16:00 UTC. Otherwise back off to today's 16:00 UTC.
    reset_today = _dt.datetime.combine(now_utc.date(), _dt.time(16, 0),
                                       tzinfo=_dt.timezone.utc)
    if now_utc >= reset_today:
        until = reset_today + _dt.timedelta(days=1)
    else:
        until = reset_today
    store.cache_set(_RATE_LIMIT_BACKOFF_KEY, until.isoformat())


def run_board_scan(scores: list[Score], headlines: list[str],
                   cap: int = 200) -> tuple[BoardScan | None, str]:
    """Returns (BoardScan|None, status). status explains why None if applicable.
    Only the top MAX_INSTRUMENTS of the (already-ranked) scores are sent to the
    LLM -- it deep-dives the most actionable, not the whole board."""
    # FIXED 2026-07-15: shared_calls_ok() was being called ONLY to build a nicer error
    # message after store.can_call() (LOCAL count only) had already rejected the call --
    # meaning it never actually GATED anything. If quant's own local count is still under
    # cap but the SHARED quota (quant+study+events combined) is already exhausted by other
    # projects, this would have sailed straight through to llm.invoke() and hit the real
    # 429 anyway -- the exact shape of the 2026-07-14 incident this was meant to prevent.
    # Now both checks actually gate the call.
    from analyst import usage_log
    shared_ok, shared_calls = usage_log.shared_calls_ok(cap=cap)
    if not store.can_call(cap=cap) or not shared_ok:
        shared_txt = f"shared {shared_calls}/{cap}" if shared_calls is not None else "shared quota unreachable"
        return None, f"budget guard: local {store.calls_today()}/{cap}, {shared_txt} (quant+study+events)"

    import datetime as _dt
    backoff = _rate_limited_until()
    if backoff:
        try:
            if _dt.datetime.now(_dt.timezone.utc) < _dt.datetime.fromisoformat(backoff):
                return None, f"provider unavailable -- backing off until {backoff[:16]}"
        except ValueError:
            pass    # malformed cached value -- ignore and attempt normally

    top = scores[:MAX_INSTRUMENTS]
    news = headlines[:MAX_NEWS]
    news_block = "\n".join(f"- {h}" for h in news) or "(no headlines available)"
    human = (
        f"INSTRUMENT FACTS (top {len(top)} by signal strength):\n{_facts_block(top)}\n\n"
        f"RECENT HEADLINES (may be irrelevant; filter yourself):\n{news_block}\n\n"
        "Return a signal for EVERY instrument above, plus a macro_note."
    )
    import time
    _start = time.perf_counter()
    try:
        # invoke_with_key_fallback() already retries once against
        # OPENAI_API_KEY_FALLBACK if the primary chatanywhere key is exhausted or dead --
        # reaching this except block means EITHER no fallback is configured, or both keys
        # failed, so backing off here is still the right call either way.
        #
        # include_raw=True ADDED 2026-08-14 (previously omitted deliberately, to avoid
        # touching this delicate exception handling -- see the now-stale comment that used
        # to sit on the log_usage() call below). Verified safe by reading analyst/nodes.py's
        # _ask(), which already made this exact change: include_raw=True only changes the
        # return SHAPE on a *successful* call (adds .raw/.parsed/.parsing_error) -- it does
        # NOT change how invocation-level errors (429, auth failures) propagate, so the
        # except block below (is_chatanywhere_unavailable(e) etc.) is unaffected either way.
        raw_result = invoke_with_key_fallback(
            lambda llm: llm.with_structured_output(BoardScan, include_raw=True),
            [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": human},
            ],
        )
        result = raw_result["parsed"]
    except Exception as e:                      # noqa: BLE001
        # WIDENED 2026-07-25: this used to only special-case 429/RateLimitError -- the
        # 2026-07-25 incident (chatanywhere silently deprecated the old key format, a 403
        # PermissionDeniedError) fell through to `raise` and flooded the log with a full
        # traceback every ~30-40s tick for ~9h straight, with nothing surfaced to the UI
        # beyond a stale "llm: never" timestamp. is_chatanywhere_unavailable() now covers
        # both classes (quota exhausted OR key rejected) so either backs off gracefully.
        if is_chatanywhere_unavailable(e):
            _set_rate_limit_backoff()
            reason = ("rate-limited by provider" if ("429" in str(e) or "RateLimitError" in type(e).__name__)
                      else "provider rejected the key (auth/permission error)")
            return None, f"{reason} -- backing off until next reset ({e})"
        raise    # anything else is a real, unexpected failure -- don't swallow it
    # SUCCESS: clear any active backoff so subsequent scans aren't blocked by a
    # stale cache entry from a previous transient error.
    _clear_backoff()
    store.record_call(1)
    try:                                          # cross-project usage visibility only
        import os
        from analyst.llm import last_model_used, last_provider_used
        from analyst.usage_log import log_usage
        usage = getattr(raw_result.get("raw"), "usage_metadata", None) or {}
        log_usage(
            kind="board_scan",
            model=last_model_used() or os.environ.get("OPENAI_MODEL", "gpt-5-mini"),
            input_tokens=usage.get("input_tokens", 0), output_tokens=usage.get("output_tokens", 0),
            latency_ms=int((time.perf_counter() - _start) * 1000),
            provider=last_provider_used(),
        )
    except Exception:
        pass                                       # telemetry only -- never affects the scan result
    return result, "ok"
