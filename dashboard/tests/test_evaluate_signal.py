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
                            invalidation="n/a")


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


if __name__ == "__main__":
    test_no_llm_sig_watch_is_plain_noise()
    test_llm_agrees_watch_is_plain_noise()
    test_llm_vetoes_real_buy_signal()
    test_llm_vetoes_real_sell_signal()
    test_journal_canonicalizes_the_new_reason()
    test_llm_agrees_buy_passes_the_action_gate()
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {_fails}")
        raise SystemExit(1)
    print("all tests passed.")
