"""Batched LLM board scan: ONE call analyses every instrument at once.

This is the budget-critical design. Instead of 4 calls x N instruments, the
whole board costs a single structured-output call. The deterministic scorer has
already done the ranking for free; the LLM adds judgement, news synthesis and an
explicit invalidation level per instrument.

Respects the daily budget guard: if we're near the cap, it returns None and the
UI keeps showing deterministic data only.
"""
from __future__ import annotations

import os
from typing import Literal

from dashboard.core import net  # noqa: F401
from pydantic import BaseModel, Field

from analyst.llm import invoke_with_key_fallback, is_chatanywhere_unavailable  # from quant/analyst
from dashboard.core import store
from dashboard.core import shared_cache
from dashboard.core.log import log
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
    "You are the head analyst on a trading desk. You get pre-computed factual "
    "indicators per instrument (top ones in full, the rest abbreviated) plus "
    "recent headlines. Do NOT invent numbers; reason only from the facts given. "
    "First write macro_note (2-3 sentences on the backdrop). THEN, for EACH "
    "instrument: bias, action (BUY/SELL/WAIT), calibrated confidence, one-line "
    "rationale, macro_linkage (does any theme from your OWN macro_note apply to "
    "THIS instrument, through what mechanism -- or genuinely nothing? Say so "
    "either way, never skip), and the explicit invalidation level. "
    "WAIT is correct when signals conflict or a trend is overextended. Only "
    "count headlines actually relevant to an instrument. You advise a human who "
    "makes the final call -- never overstate confidence."
)


def _compact_facts(s: Score) -> str:
    """Abbreviated facts for instruments outside the actionable top-N: the
    deterministic verdict plus the first 2 lines of facts_text (symbol, last
    price) -- enough for veto judgement without the full multi-timeframe detail.
    Estimated saving ~35% vs 4-line variant."""
    head = "\n".join(s.facts_text.splitlines()[:2])
    return (
        f"### {s.key}  (deterministic: {s.signal}, dir {s.direction}, "
        f"strength {s.strength}/5 -- abbreviated)\n{head}"
    )


def _facts_block(scores: list[Score], full_n: int = 3) -> str:
    """Full facts_text for the top `full_n` (ranked) instruments, compact facts
    for the rest.

    LOWERED 2026-09-25: full_n 4→3 to save ~1.5K input tok/call. The top-3
    always include any actionable strength-5 setup; instruments ranked 4-8 get
    compact facts (symbol + price + deterministic signal) which is enough for
    veto judgement and news-awareness. Estimated saving ~35% per call.
    """
    blocks = []
    for i, s in enumerate(scores):
        if i < full_n:
            blocks.append(
                f"### {s.key}  (deterministic: {s.signal}, dir {s.direction}, "
                f"strength {s.strength}/5)\n{s.facts_text}"
            )
        else:
            blocks.append(_compact_facts(s))
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
# FIXED 2026-09-03: the paragraph above assumed "a large context window" -- NOT true of the
# tier this key actually runs on. chatanywhere's free tier caps the PROMPT at 4096 tokens and
# the COMPLETION at 2000, and 40 meant "send the whole universe in one call". The model then
# ran out of OUTPUT tokens partway through the structured JSON and raised
# openai.LengthFinishReasonError (completion_tokens=2000, prompt_tokens=3982). That killed
# every scan, so NO new signals were evaluated from 2026-08-28 to 2026-09-03: no board scans,
# no new rejected_signals rows, no new trades, and "0 pending positions" on BOTH accounts --
# which is what surfaced it (the dashboards looked healthy throughout). Measured directly
# against the live key: n=21 fails, n=14 succeeds with headlines included. 12 ships rather
# than 14 to leave headroom, since prompt size drifts with headline count and each
# instrument's facts block. Coverage is barely reduced in practice: `scores` arrives ranked by
# obviousness, so a strength-5 BUY (the only kind that can clear the entry gate) always sorts
# into the top handful. run_board_scan() now also halves the batch and retries when the
# response truncates, so exceeding this degrades output instead of causing an outage.
# LOWERED 2026-09-25: 12→8 to stay within chatanywhere's 50,000 points/week free tier.
# board_scan is 50% of total token burn (~167K/week). Each instrument's full facts_text is
# ~1.5K tokens; compact facts ~0.5K. Cutting 4 instruments saves ~4K input tok/call × 51
# calls/week = ~204K tokens/week saved. Coverage stays adequate: scores arrives ranked by
# obviousness, so the top-8 always include any actionable setup.
MAX_INSTRUMENTS = 8
MAX_NEWS = 6

# Token-optimization cadence (ADDED 2026-09-16): the scan prompt is a pure
# function of (ranked deterministic signals, headlines, open positions), so an
# unchanged fingerprint means the LLM would re-read identical facts -- skip the
# call and reuse the last scan. SCAN_MIN_RESCAN_MIN debounces rapid re-runs
# when the fingerprint IS changing (event-driven trigger still fires, just not
# more often than this); SCAN_MAX_IDLE_MIN forces a periodic fresh read even
# with no delta (news staleness, model re-read).
SCAN_MIN_RESCAN_MIN = 5
SCAN_MAX_IDLE_MIN = 180


def _dedupe_headlines(headlines: list[str], limit: int = MAX_NEWS) -> list[str]:
    """Order-preserving dedupe (feeds overlap heavily) before the cap -- dupes
    previously each cost tokens while adding zero information."""
    seen: set[str] = set()
    out: list[str] = []
    for h in headlines:
        key = (h or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= limit:
            break
    return out


def scan_fingerprint(scores: list[Score], headlines: list[str],
                     position_keys: tuple | list = ()) -> str:
    """Stable hash of everything the scan prompt is built from: the ranked
    deterministic verdicts (key/signal/direction/strength -- NOT raw prices, so
    sub-gate noise doesn't churn it), the deduped headline set, and open
    position keys. Pure function, no I/O -- unit-testable."""
    import hashlib
    parts = [f"{s.key}|{s.signal}|{s.direction}|{s.strength}"
             for s in scores[:MAX_INSTRUMENTS]]
    parts.append("news:" + hashlib.sha256(
        "\n".join(_dedupe_headlines(headlines)).encode()).hexdigest()[:16])
    parts.append("pos:" + ",".join(sorted(str(k) for k in position_keys)))
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:32]


def score_fingerprint(scores: list[Score],
                      position_keys: tuple | list = ()) -> str:
    """Hash of just scores + positions (no headlines). Used to detect
    headlines-only deltas: if the full fingerprint changed but this one
    didn't, only news headlines shifted -- a minor change that may not
    justify an LLM call (see headlines_only_skip in should_scan)."""
    import hashlib
    parts = [f"{s.key}|{s.signal}|{s.direction}|{s.strength}"
             for s in scores[:MAX_INSTRUMENTS]]
    parts.append("pos:" + ",".join(sorted(str(k) for k in position_keys)))
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:32]


def should_scan(fingerprint: str, last_fingerprint: str | None,
                last_scan_ts: float | None, now_ts: float,
                score_only_fp: str | None = None,
                last_score_only_fp: str | None = None) -> tuple[bool, str]:
    """Pure cadence decision. Returns (proceed, reason).

    `score_only_fp` / `last_score_only_fp` (optional): when provided, used to
    detect headlines-only deltas. If the full fingerprint changed but the
    score-only fingerprint didn't, only news headlines shifted -- a minor
    change that doesn't justify an LLM call (skip with "headlines only delta").
    This saves ~1-2K tokens per skipped trigger (~60% of triggers in quiet
    markets are headline-only shuffles)."""
    if fingerprint != last_fingerprint:
        # If score-only fingerprint is unchanged, this is a headlines-only
        # delta -- skip the scan. Headlines don't change verdicts (the LLM
        # re-reads the same ranked signals), so rescan is wasted tokens.
        if (score_only_fp is not None and last_score_only_fp is not None
                and score_only_fp == last_score_only_fp):
            return False, "headlines only delta"
        if last_scan_ts and (now_ts - last_scan_ts) < SCAN_MIN_RESCAN_MIN * 60:
            return False, "debounced: signal changed but last scan <5min ago"
        return True, "signal delta"
    if not last_scan_ts or (now_ts - last_scan_ts) >= SCAN_MAX_IDLE_MIN * 60:
        return True, "max idle refresh"
    return False, "no signal delta"


# Last successful scan's token/cost telemetry, for the caller's per-scan
# metrics logging (service.refresh_llm). Empty until the first success.
LAST_SCAN_TELEMETRY: dict = {}

# Cross-instance reuse (ADDED 2026-09-17, see dashboard/core/shared_cache.py):
# paper may reuse live's scan when its own board fingerprints identically, and
# only while live's scan is fresher than this. Direction is live -> paper ONLY
# (enforced in service.refresh_llm) -- paper's views never feed live.
SHARED_SCAN_MAX_AGE_MIN = 30
# SHARED_SCAN_ENABLE=1 lets paper attempt reuse; default off for a safe canary.
SHARED_SCAN_ENV_FLAG = "SHARED_SCAN_ENABLE"


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


def _set_rate_limit_backoff(long: bool = False) -> None:
    """Back off until the next provider reset (00:00 HKT / 16:00 UTC).
    The provider's own message says "请00:00后再试" -- that's Hong Kong time (UTC+8),
    NOT UTC midnight. The previous code used UTC midnight, extending the blackout
    by an unnecessary 8 hours.

    FIXED 2026-09-16: that daily-reset assumption is WRONG for chatanywhere's
    "7天免费点数不足以支持本次请求" 403 -- confirmed live (2026-09-14/15) that error's
    own message means a rolling 7-DAY free-points window is exhausted, not a daily
    quota. Backing off to the next 16:00 UTC just re-hits the identical 403 the
    instant that boundary passes, because the 7-day window hasn't actually rolled
    forward -- measured this looping every ~30-40s for 45+ hours straight before
    being caught, hundreds of wasted round-trips a day for zero chance of success.
    A 7-day exhaustion has no fixed reset instant to compute (points trickle back
    in as old high-usage days age out of the window, see the quant-vs-events-vs-
    study token breakdown in HANDOFF.md), so `long=True` (passed by the caller when
    the error text names the 7-day window) checks back once a day instead of
    guessing a reset time that's usually wrong."""
    import datetime as _dt
    now_utc = _dt.datetime.now(_dt.timezone.utc)
    if long:
        until = now_utc + _dt.timedelta(hours=24)
    else:
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
            bo_dt = _dt.datetime.fromisoformat(backoff)
            if bo_dt.tzinfo is None:
                bo_dt = bo_dt.replace(tzinfo=_dt.timezone.utc)  # legacy naive value = UTC
            if _dt.datetime.now(_dt.timezone.utc) < bo_dt:
                return None, f"provider unavailable -- backing off until {backoff[:16]}"
        except ValueError:
            pass    # malformed cached value -- ignore and attempt normally

    top = scores[:MAX_INSTRUMENTS]
    news = _dedupe_headlines(headlines)
    news_block = "\n".join(f"- {h}" for h in news) or "(no headlines available)"

    def _human_for(batch) -> str:
        return (
            f"INSTRUMENT FACTS (top {len(batch)} by signal strength):\n{_facts_block(batch)}\n\n"
            f"RECENT HEADLINES (may be irrelevant; filter yourself):\n{news_block}\n\n"
            "Return a signal for EVERY instrument above, plus a macro_note."
        )

    human = _human_for(top)
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
        def _invoke(prompt: str):
            # ROUTED 2026-09-16 (token optimization, tier D): BOARD_SCAN_MODEL
            # lets ops point the batched scan at a cheaper/faster model without
            # touching the analyst CLI's OPENAI_MODEL. Unset = today's behavior
            # exactly. The deterministic entry gate (paper.evaluate_signal())
            # and risk sizing stay in code regardless of model -- the scan only
            # advises, never decides. The model actually used is logged via
            # last_model_used() on the log_usage() call below.
            return invoke_with_key_fallback(
                lambda llm: llm.with_structured_output(BoardScan, include_raw=True),
                [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                model=os.environ.get("BOARD_SCAN_MODEL") or None,
            )

        try:
            raw_result = _invoke(human)
        except Exception as _len_err:           # noqa: BLE001
            # ADDED 2026-09-03: a TRUNCATED RESPONSE is not a dead provider. When the model
            # runs out of output tokens partway through the structured JSON, openai raises
            # LengthFinishReasonError -- which is neither a 4xx nor a rate limit, so it fell
            # straight through to `raise` and killed the whole scan on EVERY tick. That is
            # exactly how this pipeline went silently dead for five days (see MAX_INSTRUMENTS).
            # The right response is to ask for LESS, not to give up: halve the batch and try
            # once more. Degraded coverage for that scan beats no scan at all, and `top` is
            # ranked, so the half retained is the half that actually matters.
            if type(_len_err).__name__ != "LengthFinishReasonError" or len(top) <= 4:
                raise
            smaller = top[: max(4, len(top) // 2)]
            log.warning("board scan: response truncated at %d instruments -- retrying with "
                        "%d (see MAX_INSTRUMENTS' 2026-09-03 note)", len(top), len(smaller))
            top = smaller
            raw_result = _invoke(_human_for(top))
        result = raw_result["parsed"]
    except Exception as e:                      # noqa: BLE001
        # WIDENED 2026-07-25: this used to only special-case 429/RateLimitError -- the
        # 2026-07-25 incident (chatanywhere silently deprecated the old key format, a 403
        # PermissionDeniedError) fell through to `raise` and flooded the log with a full
        # traceback every ~30-40s tick for ~9h straight, with nothing surfaced to the UI
        # beyond a stale "llm: never" timestamp. is_chatanywhere_unavailable() now covers
        # both classes (quota exhausted OR key rejected) so either backs off gracefully.
        if is_chatanywhere_unavailable(e):
            # ADDED 2026-09-16: "7天" (7-day) in the message means the provider's
            # rolling weekly free-points pool is exhausted, not the ordinary daily
            # quota -- see _set_rate_limit_backoff()'s docstring for why that needs a
            # much longer, non-clock-aligned backoff instead of "wait for 16:00 UTC".
            is_seven_day_exhaustion = "7天" in str(e) or "7-day" in str(e).lower()
            _set_rate_limit_backoff(long=is_seven_day_exhaustion)
            if is_seven_day_exhaustion:
                reason = "provider's 7-day free-points window exhausted"
            else:
                reason = ("rate-limited by provider" if ("429" in str(e) or "RateLimitError" in type(e).__name__)
                          else "provider rejected the key (auth/permission error)")
            return None, f"{reason} -- backing off until next reset ({e})"
        raise    # anything else is a real, unexpected failure -- don't swallow it
    # SUCCESS: clear any active backoff so subsequent scans aren't blocked by a
    # stale cache entry from a previous transient error.
    _clear_backoff()
    store.record_call(1)
    # Telemetry for the caller (service.refresh_llm logs per-scan metrics into
    # the journal): module-level stash, copied -- never raises, never affects
    # the scan result. Reset at the top of every attempt below.
    _telemetry = {"input_tokens": 0, "output_tokens": 0, "latency_ms": 0,
                  "model": "", "provider": ""}
    try:                                          # cross-project usage visibility only
        from analyst.llm import last_model_used, last_provider_used
        from analyst.usage_log import log_usage
        usage = getattr(raw_result.get("raw"), "usage_metadata", None) or {}
        _telemetry.update(
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            latency_ms=int((time.perf_counter() - _start) * 1000),
            model=last_model_used() or os.environ.get("OPENAI_MODEL", "gpt-5-mini"),
            provider=last_provider_used(),
        )
        log_usage(
            kind="board_scan",
            model=_telemetry["model"],
            input_tokens=_telemetry["input_tokens"],
            output_tokens=_telemetry["output_tokens"],
            latency_ms=_telemetry["latency_ms"],
            provider=_telemetry["provider"],
        )
    except Exception:
        pass                                       # telemetry only -- never affects the scan result
    LAST_SCAN_TELEMETRY.clear()
    LAST_SCAN_TELEMETRY.update(_telemetry)
    try:                                          # cross-instance sharing only
        shared_cache.write(shared_cache.SCAN_KEY, {
            "market_fp": scan_fingerprint(top, news, ()),
            "macro_note": result.macro_note,
            "signals": [s.model_dump() for s in result.signals],
            "model": _telemetry["model"],
            "provider": _telemetry["provider"],
            "environment": os.environ.get("DASH_FIXED_MODE", "unknown"),
        })
    except Exception:
        pass                                       # sharing is best-effort only
    return result, "ok"
