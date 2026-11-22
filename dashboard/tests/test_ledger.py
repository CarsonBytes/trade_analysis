"""F3 shadow ledger, F5 config parity, F7 kill rule, A3 cover tickets (2026-11-21).
No network, no broker. Run:  uv run python -m dashboard.tests.test_ledger
"""
from __future__ import annotations

from dashboard.tests import hermetic  # noqa: F401  (before any app module)

import os
import datetime as dt
import tempfile
from unittest import mock

import pandas as pd

_fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok:
        _fails.append(name)
    assert ok, f"{name}: got {got!r} want {want!r}"


def _isolated_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    return path


def test_gate_labels():
    print("shadow.gate_label(): canonical gate from the rejection's first reason:")
    from dashboard.core.shadow import gate_label as g
    check("LLM veto", g(["LLM vetoed to WAIT (deterministic was BUY): overextended"]), "LLM veto")
    check("trend strength", g(["trend strength 4 < 5"]), "trend strength < 5")
    check("RSI", g(["overextended RSI 74"]), "RSI overextended")
    check("re-entry", g(["re-entry gate: 39.95 hasn't reclaimed 40.93"]), "re-entry gate")
    check("unknown -> other", g(["something new"]), "other")


def test_record_dedupes_and_resolves():
    print("\nshadow.record() dedupes per (instrument, source, gate, signal_bar); resolve_open() "
          "scores rows with the journal's own resolver:")
    path = _isolated_db()
    with mock.patch.dict(os.environ, {"DASH_DB_NAME": path}):
        from dashboard.core import shadow, paper
        t0 = dt.datetime(2026, 8, 3, 15, 0, tzinfo=dt.timezone.utc)
        kw = dict(instrument="DIA", direction="long", entry=100.0, sl=95.0, tp=115.0,
                  signal_bar="2026-07-27", ts=t0)
        check("first record inserted", shadow.record(source="blocked", gate="LLM veto", **kw), True)
        check("same setup again (next hourly scan) ignored",
              shadow.record(source="blocked", gate="LLM veto", **kw), False)
        check("approved row for the same bar is separate",
              shadow.record(source="approved", paper_id=7, **kw), True)
        idx = pd.date_range("2026-08-04", periods=10, freq="D", tz="UTC")
        ohlc = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0},
                            index=idx)
        ohlc.iloc[3, ohlc.columns.get_loc("low")] = 94.0            # stop hit on day 4
        with mock.patch("dashboard.data.mt5_client.is_available", return_value=False), \
             mock.patch("dashboard.core.store.cache_get", return_value=(None, None)), \
             mock.patch("dashboard.core.store.cache_set"):
            n = shadow.resolve_open(lambda inst: ohlc, force=True)
        check("both rows resolved", n, 2)
        with shadow._conn() as c:
            rows = c.execute("SELECT source, status, round(r, 2) FROM shadow_signals "
                             "ORDER BY source").fetchall()
        check("stopped out at -1R (minus costs)", rows,
              [("approved", "LOSS", -1.0), ("blocked", "LOSS", -1.0)])
    try:
        os.remove(path)
    except OSError:
        pass


def test_summarize():
    print("\nshadow.summarize(): signal-level R, execution drag, per-gate R saved:")
    from dashboard.core.shadow import summarize
    rows = [("approved", "", "WIN", 3.0, 1), ("approved", "", "LOSS", -1.0, 2),
            ("approved", "", "LOSS", -1.0, None), ("approved", "", "OPEN", None, None),
            ("blocked", "LLM veto", "LOSS", -1.0, None), ("blocked", "LLM veto", "LOSS", -1.0, None),
            ("blocked", "LLM veto", "WIN", 3.0, None), ("blocked", "RSI overextended", "WIN", 3.0,
                                                       None)]
    s = summarize(rows, funded={1: 2.5, 2: -1.2})
    check("approved n / sum", (s["approved"]["n"], s["approved"]["sum_r"]), (3, 1.0))
    check("execution drag over the 2 funded rows", round(s["drag"]["avg_r"], 2), -0.35)
    check("LLM veto: blocked 2 losers + 1 winner -> saved -1R", s["gates"]["LLM veto"]["r_saved"],
          -1.0)
    check("RSI gate blocked a winner -> saved -3R", s["gates"]["RSI overextended"]["r_saved"], -3.0)
    check("open rows counted separately", s["open"], 1)


def test_llm_monitor_only_never_blocks():
    print("\nLLM_GATE=monitor (default, user decision 2026-11-21): an LLM WAIT on a deterministic "
          "BUY no longer blocks the trade; LLM_GATE=veto restores the old behaviour:")
    from types import SimpleNamespace
    from dashboard.core import paper
    from dashboard.core.scoring import Score
    sc = Score(key="CPER", direction="long", strength=5, obviousness=4.0, signal="BUY", note="",
               facts={"rsi14": 55.0, "atr14": 1.0, "atr14_med60": 1.0}, facts_text="")
    wait = SimpleNamespace(action="WAIT", confidence=0.9, rationale="macro risk; wait")
    with mock.patch("dashboard.models.confidence_model.objective", return_value=None), \
         mock.patch.object(paper, "WEEKLY_TREND_CLASSES", set()):
        with mock.patch.dict(os.environ, {"LLM_GATE": ""}):
            ok, reasons, d = paper.evaluate_signal("CPER", sc, wait)
            check("default = monitor: the rules decide, trade passes", (ok, d), (True, "long"))
            check("llm_would_veto() still sees the LLM's call for the ledger",
                  paper.llm_would_veto(sc, wait), True)
        with mock.patch.dict(os.environ, {"LLM_GATE": "veto"}):
            ok, reasons, d = paper.evaluate_signal("CPER", sc, wait)
            check("LLM_GATE=veto: blocked as before", (ok, reasons[0].startswith("LLM vetoed")),
                  (False, True))


def test_summarize_llm_monitor_and_confidence():
    print("\nshadow.summarize(): the LLM's would-be vetoes and confidence buckets are scored:")
    from dashboard.core.shadow import summarize
    rows = [("approved", "", "WIN", 3.0, 1, 0.9), ("approved", "", "LOSS", -1.0, 2, 0.9),
            ("approved", "", "LOSS", -1.0, 3, 0.5), ("llm_monitor", "LLM would veto", "LOSS",
                                                    -1.0, None, 0.7)]
    s = summarize(rows, funded={})
    check("would-veto bucket", (s["llm_would_veto"]["n"], s["llm_would_veto"]["avg_r"]), (1, -1.0))
    check("confidence >=0.8 bucket", (s["llm_conf"][">=0.8"]["n"], s["llm_conf"][">=0.8"]["avg_r"]),
          (2, 1.0))
    check("confidence <0.6 bucket", s["llm_conf"]["<0.6"]["n"], 1)
    check("monitor rows are not a blocking gate", "LLM would veto" in s["gates"], False)


def test_config_parity_and_reference_label():
    print("\nF5 drift.config_parity(): the backtest reference must match the DEPLOYED sizing:")
    from dashboard.web import drift
    with mock.patch.dict(os.environ, {"ETF_POS_CAP": "0.25", "PORTFOLIO_CAP": "1.0"}):
        check("the old pinned reference (30%) is flagged",
              drift.config_parity({"pos_cap": 0.30, "portfolio_cap": 1.0}),
              ["position cap: live 25% vs reference 30%"])
        check("matching reference -> parity", drift.config_parity(
            {"pos_cap": 0.25, "portfolio_cap": 1.0, "risk": 0.01}), [])
        check("no reference -> says so", len(drift.config_parity(None)), 1)
        check("label follows the live config", "pos_cap=25%" in drift.ref_config_label(), True)


def test_kill_rule():
    print("\nF7 drift.kill_rule(): pre-registered -- halt only when >= 30 funded trades AND the "
          "90% upper bound of expectancy < 0:")
    from dashboard.web.drift import kill_rule
    check("14 trades (today's LIVE) -> insufficient", kill_rule([-1.0] * 12 + [3.0] * 2)["verdict"],
          "insufficient")
    bad = [-1.0] * 26 + [3.0] * 4                     # 30 trades, mean -0.47R
    check("30 trades at -0.47R -> halt", kill_rule(bad)["verdict"], "halt")
    ok = [-1.0] * 18 + [3.0] * 12                     # mean +0.6R
    check("30 trades at +0.6R -> ok", kill_rule(ok)["verdict"], "ok")
    noisy = [-1.0] * 22 + [3.0] * 8                   # mean +0.07R -> upper > 0
    check("slightly positive / noisy -> ok (no halt on noise)", kill_rule(noisy)["verdict"], "ok")


def test_cover_tickets_in_short_alert():
    print("\nA3: the UNINTENDED SHORT page carries the exact cover tickets:")
    from dashboard.core import notable_events as ne
    check("tickets", ne.cover_tickets(["QQQ -8", "SPY -4", "CPER -84"]),
          ["BUY 8 QQQ", "BUY 4 SPY", "BUY 84 CPER"])
    title, detail = ne.humanize("reconcile: broker/local position MISMATCH -- SHORT=[QQQ -8, SPY -4] "
                                "only_local(ghost)=[] only_broker(untracked)=['QQQ', 'SPY']")
    check("title unchanged", title, "UNINTENDED SHORT (QQQ -8, SPY -4)")
    check("detail names the tickets", "BUY 8 QQQ; BUY 4 SPY" in detail, True)


if __name__ == "__main__":
    for _name, _fn in list(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
            except AssertionError:
                pass
    print()
    if _fails:
        print(f"{len(_fails)} FAILED: {_fails}")
        raise SystemExit(1)
    print("all tests passed.")
