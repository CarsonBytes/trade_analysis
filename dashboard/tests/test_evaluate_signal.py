"""Regression test for the WAIT/WATCH reason split in core/paper.py's evaluate_signal(),
added 2026-07-13 after finding place_from_state()'s "skip WAIT/WATCH noise" filter
(`reasons != ["action is WAIT/WATCH"]`) was silently discarding the ONE case that's actually
interesting: the LLM actively vetoing a real deterministic BUY/SELL into WAIT (a news veto, its
own overextension read, or a low-confidence calibration -- see board_scan.py's system prompt).
That's indistinguishable, before this fix, from the mundane "this instrument never had a real
setup at all" case -- both produced the exact one-line "action is WAIT/WATCH" reason, so neither
ever reached the rejected_signals journal or the retrospective's constraint scorecard.

Run:  uv run python -m dashboard.tests.test_evaluate_signal
"""
from __future__ import annotations

from dashboard.core import paper
from dashboard.core.paper import evaluate_signal
from dashboard.core.scoring import Score
from dashboard.web.board_scan import InstrumentSignal
from dashboard.core.journal import _canon

_fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        _fails.append(name)


def _score(signal: str, strength: int = 5) -> Score:
    return Score(key="SPY", direction="long", strength=strength, obviousness=1.0,
                signal=signal, note="test", facts={}, facts_text="")


def _llm(action: str, rationale: str = "test rationale") -> InstrumentSignal:
    return InstrumentSignal(key="SPY", bias="bullish" if action == "BUY" else "neutral",
                            action=action, confidence=0.7, rationale=rationale,
                            macro_linkage="none material", invalidation="n/a")


def test_no_llm_sig_watch_is_plain_noise():
    print("no llm_sig, deterministic WATCH -- plain noise reason (unchanged):")
    ok, reasons, _ = evaluate_signal("SPY", _score("WATCH"), None)
    check("rejected", ok, False)
    check("plain WAIT/WATCH reason", reasons, ["action is WAIT/WATCH"])


def test_llm_agrees_watch_is_plain_noise():
    print("\nllm_sig also WAIT, deterministic WATCH -- still plain noise:")
    ok, reasons, _ = evaluate_signal("SPY", _score("WATCH"), _llm("WAIT"))
    check("rejected", ok, False)
    check("plain WAIT/WATCH reason (no real setup underneath either)",
          reasons, ["action is WAIT/WATCH"])


def test_llm_vetoes_real_buy_signal():
    print("\ndeterministic BUY, llm_sig vetoes to WAIT -- must be DISTINGUISHABLE:")
    ok, reasons, _ = evaluate_signal("SPY", _score("BUY"), _llm("WAIT", "Fed decision risk"))
    check("rejected", ok, False)
    check("exactly one reason", len(reasons), 1)
    check("reason is NOT the generic noise label", reasons == ["action is WAIT/WATCH"], False)
    check("reason names the vetoed deterministic signal",
          reasons[0].startswith("LLM vetoed to WAIT (deterministic was BUY)"), True)
    check("reason carries the LLM's own rationale", "Fed decision risk" in reasons[0], True)


def test_llm_vetoes_real_sell_signal():
    print("\ndeterministic SELL, llm_sig vetoes to WAIT -- same distinction, SELL side:")
    ok, reasons, _ = evaluate_signal("SPY", _score("SELL"), _llm("WAIT"))
    check("rejected", ok, False)
    check("reason names SELL specifically",
          reasons[0].startswith("LLM vetoed to WAIT (deterministic was SELL)"), True)


def test_llm_rationale_semicolons_are_sanitized():
    print("\nFIXED 2026-07-13: rationale containing ';' must not corrupt the scorecard split "
          "(confirmed live: 'muted short-term returns; wait for break' fragmented into 2 "
          "bogus rows because journal.rejection_counts() splits reasons on bare ';'):")
    rationale_with_semicolon = "Uptrends across horizons but near resistance; wait for break"
    ok, reasons, _ = evaluate_signal("SPY", _score("BUY"), _llm("WAIT", rationale_with_semicolon))
    check("rejected", ok, False)
    check("no semicolon survives into the stored reason", ";" in reasons[0], False)
    check("the content is still present (comma-joined, not dropped)",
          "wait for break" in reasons[0], True)
    # this is exactly the scenario that broke before: joining reasons with "; " then
    # splitting back on ";" must yield ONE part for this trade, not two
    joined = "; ".join(reasons)
    check("split(';') on the joined/stored form yields exactly one part (no fragmentation)",
          len(joined.split(";")), 1)


def test_journal_canonicalizes_the_new_reason():
    print("\njournal._canon() maps the new reason to a clean scorecard label:")
    raw = "LLM vetoed to WAIT (deterministic was BUY): Fed decision risk"
    check("canonical label", _canon(raw), "LLM vetoed a deterministic BUY/SELL to WAIT")


def test_llm_agrees_buy_passes_the_action_gate():
    print("\nllm_sig agrees BUY -- must NOT hit the WAIT/WATCH early return at all:")
    ok, reasons, direction = evaluate_signal("SPY", _score("BUY", strength=5), _llm("BUY"))
    check("did not reject on action/WAIT gate",
          "action is WAIT/WATCH" in reasons or any(r.startswith("LLM vetoed") for r in reasons),
          False)
    check("direction resolved to long", direction, "long")


# ADDED 2026-07-30: manual tech pause (paper.TECH_PAUSED/TECH_TICKERS), user-requested after
# the QQQ investigation this same session. Checked FIRST in evaluate_signal(), ahead of every
# other gate -- a deliberate override, not a strategy finding.
def test_tech_paused_blocks_a_strong_qqq_buy():
    print("\nTECH_PAUSED=True: blocks QQQ even with a perfect deterministic+LLM BUY setup, "
          "checked BEFORE any other gate:")
    old = paper.TECH_PAUSED
    paper.TECH_PAUSED = True
    try:
        ok, reasons, direction = evaluate_signal("QQQ", _score("BUY", strength=5), _llm("BUY"))
        check("rejected", ok, False)
        check("exactly one reason (short-circuits before any other gate runs)",
              reasons, ["tech investment paused"])
        check("direction not resolved", direction, "")
    finally:
        paper.TECH_PAUSED = old


def test_tech_paused_blocks_xlk_too():
    print("\nTECH_PAUSED=True: blocks XLK (the sleeve-only tech ticker) the same way:")
    old = paper.TECH_PAUSED
    paper.TECH_PAUSED = True
    try:
        ok, reasons, _ = evaluate_signal("XLK", _score("BUY", strength=5), _llm("BUY"))
        check("rejected", ok, False)
        check("tech-pause reason", reasons, ["tech investment paused"])
    finally:
        paper.TECH_PAUSED = old


def test_tech_paused_does_not_affect_non_tech_instruments():
    print("\nTECH_PAUSED=True: a non-tech instrument (SPY) is completely unaffected:")
    old = paper.TECH_PAUSED
    paper.TECH_PAUSED = True
    try:
        ok, reasons, direction = evaluate_signal("SPY", _score("BUY", strength=5), _llm("BUY"))
        check("not rejected by the tech gate",
              "tech investment paused" in reasons, False)
        check("direction resolved to long", direction, "long")
    finally:
        paper.TECH_PAUSED = old


def test_tech_resumed_lets_qqq_through_again():
    print("\nTECH_PAUSED=False (resumed): QQQ reaches the normal gates again:")
    old = paper.TECH_PAUSED
    paper.TECH_PAUSED = False
    try:
        ok, reasons, direction = evaluate_signal("QQQ", _score("BUY", strength=5), _llm("BUY"))
        check("not rejected by the tech gate",
              "tech investment paused" in reasons, False)
        check("direction resolved to long", direction, "long")
    finally:
        paper.TECH_PAUSED = old


if __name__ == "__main__":
    test_no_llm_sig_watch_is_plain_noise()
    test_llm_agrees_watch_is_plain_noise()
    test_llm_vetoes_real_buy_signal()
    test_llm_vetoes_real_sell_signal()
    test_llm_rationale_semicolons_are_sanitized()
    test_journal_canonicalizes_the_new_reason()
    test_llm_agrees_buy_passes_the_action_gate()
    test_tech_paused_blocks_a_strong_qqq_buy()
    test_tech_paused_blocks_xlk_too()
    test_tech_paused_does_not_affect_non_tech_instruments()
    test_tech_resumed_lets_qqq_through_again()
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {_fails}")
        raise SystemExit(1)
    print("all tests passed.")
