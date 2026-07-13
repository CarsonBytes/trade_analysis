"""Comprehensive retrospective report for the forward (demo) test.

Pulls together everything needed to judge the system and tune constraints:
  - KPIs: expectancy-R, win rate, profit factor, equity curve, max drawdown
  - per-trade rationale dump: entry context (LLM bias/confidence/invalidation,
    deterministic strength, macro backdrop, key facts) + exit reason + R
  - constraint scorecard: how often each gate blocked a trade (from the
    rejected-signals journal) -- the evidence for add/adjust/remove decisions
  - demo reconciliation: paper R vs real demo-fill R, when MT5 is available

Run:  uv run python -m dashboard.retrospective         # print + write file
      uv run python -m dashboard.retrospective --json   # machine-readable dump
"""
from __future__ import annotations

from dashboard.core import net  # noqa: F401

import json
import datetime as dt
import pathlib

from dashboard.core import paper
from dashboard.core import journal
from dashboard.web.report import _fmt, _group

EXPORT_DIR = pathlib.Path(__file__).resolve().parent.parent / "exports"


def confidence_calibration(closed: list[dict]) -> list[dict]:
    """Group closed trades by LLM-confidence band and report realized expectancy
    per band. THE test of whether confidence is predictive: if higher bands
    don't show higher expectancy, the confidence gate is noise, not signal."""
    bands = [(0.0, 0.60, "<0.60"), (0.60, 0.70, "0.60–0.70"),
             (0.70, 0.80, "0.70–0.80"), (0.80, 0.90, "0.80–0.90"),
             (0.90, 1.01, "≥0.90")]
    out = []
    for lo, hi, label in bands:
        rs = [t["realized_r"] for t in closed
              if lo <= (t.get("confidence") or 0.0) < hi]
        if rs:
            s = paper.stats(rs)
            out.append({"band": label, "n": s["n"], "win": s["win_rate"],
                        "expR": s["expectancy_R"]})
    return out


def equity_curve(closed: list[dict]) -> tuple[list[float], float]:
    """Cumulative R over closed trades (entry order) and max drawdown in R."""
    chron = sorted(closed, key=lambda t: t["exit_ts"] or t["ts"])
    cum, peak, max_dd = 0.0, 0.0, 0.0
    curve = []
    for t in chron:
        cum += t["realized_r"]
        curve.append(round(cum, 3))
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    return curve, round(max_dd, 3)


def _demo_executed_ids() -> set[int]:
    """paper_ids the active broker actually placed (have a mirror row) -- the
    broker-truth set. Broker-aware: mt5_mirror under MT5, ib_mirror under IBKR."""
    from dashboard.execution import broker
    return broker.executed_ids()


def _kpi_block(L: list[str], closed: list[dict]) -> None:
    rs = [t["realized_r"] for t in closed]
    curve, max_dd = equity_curve(closed)
    L.append(_fmt(paper.stats(rs)))
    if curve:
        L.append(f"- equity curve (cumulative R): {curve[0]:+.2f} → {curve[-1]:+.2f}")
        L.append(f"- max drawdown: {max_dd:.2f} R")
        L.append(f"- account impact @ {paper.RISK_PER_TRADE:.1%}/trade: "
                 f"{curve[-1]*paper.RISK_PER_TRADE:+.2%} (peak-to-trough "
                 f"{-max_dd*paper.RISK_PER_TRADE:.2%})")


def build() -> str:
    allt = paper.all_trades()
    closed = [t for t in allt if t["status"] != "OPEN"]
    opent = [t for t in allt if t["status"] == "OPEN"]
    demo_ids = _demo_executed_ids()
    demo_closed = [t for t in closed if t["id"] in demo_ids]

    L: list[str] = []
    L.append(f"# Forward-test retrospective — {dt.datetime.now():%Y-%m-%d %H:%M}")
    L.append(f"Trades: {len(allt)} total | {len(opent)} open | {len(closed)} closed "
             f"| {len(demo_closed)} closed & demo-executed")
    L.append("")
    # PRIMARY: broker truth -- only trades the active broker actually executed
    from dashboard.execution import broker
    L.append(f"## KPIs — {broker.name()}-executed only (broker truth)")
    L.append(f"Only trades with a real {broker.name()} order/fill. Signals never "
             "placed are excluded here.")
    if demo_closed:
        _kpi_block(L, demo_closed)
    else:
        L.append(f"(no closed {broker.name()}-executed trades yet)")
    L.append("")
    # SECONDARY: full signal-logic record (paper, may exceed what the demo took)
    L.append("## KPIs — all paper signals (signal-logic record)")
    L.append("Every signal the logic generated vs real prices, executed or not. "
             "Broader than the broker record above.")
    _kpi_block(L, closed)
    L.append("")
    L.append("## By method / instrument / direction")
    for label, keyfn in [("method", lambda t: t["method"]),
                         ("instrument", lambda t: t["instrument"]),
                         ("direction", lambda t: t["direction"])]:
        for k, s in _group(closed, keyfn).items():
            L.append(f"- {label}:{k:<12} {_fmt(s)}")
    L.append("")

    # ---- constraint scorecard ----------------------------------------------
    L.append("## Constraint scorecard (rejected candidates)")
    counts = journal.rejection_counts()
    if counts:
        L.append("How often each gate blocked a directional candidate. A gate "
                 "that rarely fires adds little; one that fires constantly may be "
                 "too tight — cross-check against the trades it let through.")
        for reason, n in counts:
            L.append(f"- {n:>4}×  {reason}")
    else:
        L.append("(no rejections recorded yet)")
    L.append("")

    # ---- confidence calibration --------------------------------------------
    L.append("## Confidence calibration (is LLM confidence predictive?)")
    cal = confidence_calibration(closed)
    if cal:
        L.append("Realized expectancy by the LLM's self-reported confidence band. "
                 "If expectancy doesn't rise with confidence, the confidence gate "
                 "is not earning its place — consider dropping/lowering it.")
        L.append(f"{'band':<12}{'n':>5}{'win':>7}{'expR':>9}")
        for r in cal:
            L.append(f"{r['band']:<12}{r['n']:>5}{r['win']*100:>6.0f}%{r['expR']:>9.3f}")
    else:
        L.append("(no closed trades with confidence data yet)")
    L.append("")

    # ---- criteria-loosening sensitivity ------------------------------------
    L.append("## Criteria-loosening sensitivity")
    counts = journal.rejection_counts()
    if counts:
        total_rej = sum(n for _, n in counts)
        L.append(f"{total_rej} directional candidates were blocked. Each gate below "
                 "is roughly how many *more* trades you'd evaluate by loosening it "
                 "(candidates can be blocked by several gates at once, so these "
                 "overlap). Loosen ONE at a time and watch the journal.")
        for reason, n in counts:
            L.append(f"- {n:>4}  ← loosening: {reason}")
        L.append("")
        L.append("Current thresholds: "
                 f"MIN_STRENGTH={paper.MIN_STRENGTH}/5, "
                 f"CONF_THRESHOLD={paper.CONF_THRESHOLD}, "
                 f"vol filter={'ON' if paper.VOL_FILTER else 'OFF'}, "
                 f"MIN_RR={paper.MIN_RR}, COOLDOWN={paper.COOLDOWN_MIN}m.")
    else:
        L.append("(no rejections recorded yet)")
    L.append("")

    # ---- per-trade rationale dump ------------------------------------------
    L.append("## Trade-by-trade rationale")
    for t in allt:
        ef = {}
        try:
            ef = json.loads(t.get("entry_facts") or "{}")
        except Exception:
            pass
        head = (f"### #{t['id']} {t['instrument']} {t['direction']} {t['method']} "
                f"— {t['status']}"
                + (f" ({t['realized_r']:+.2f} R)" if t["status"] != "OPEN" else ""))
        L.append(head)
        L.append(f"- opened {t['ts'][:16]} @ {t['entry']:.5f}  SL {t['sl']:.5f}  "
                 f"TP {t['tp']:.5f}  (R:R {t['rr']})")
        if t["status"] != "OPEN":
            L.append(f"- closed {t['exit_ts'][:16]} @ {t['exit_price']:.5f}  "
                     f"— {t.get('exit_reason') or t['status']}")
        L.append(f"- LLM: bias {t.get('llm_bias') or '?'}, conf "
                 f"{t['confidence']:.2f} — {t['rationale']}")
        if t.get("macro_linkage"):
            L.append(f"- macro linkage: {t['macro_linkage']}")
        if t.get("invalidation"):
            L.append(f"- invalidation: {t['invalidation']}")
        L.append(f"- deterministic: strength {t.get('det_strength')}/5 — "
                 f"{t.get('det_note') or '?'}")
        if ef:
            L.append(f"- facts@entry: RSI {ef.get('rsi14', '?'):.0f}, "
                     f"ATR {ef.get('atr14', 0):.5g} (med60 {ef.get('atr14_med60', 0):.5g}, "
                     f"vol_ok={ef.get('vol_filter_ok')}), trend {ef.get('trend')}")
        if t.get("macro_note"):
            L.append(f"- macro@entry: {t['macro_note']}")
        L.append("")

    # ---- recent macro history ----------------------------------------------
    L.append("## Recent macro backdrop (LLM, newest first)")
    for m in journal.macro_history(limit=15):
        L.append(f"- {m['ts'][:16]}  {m['macro_note']}")
    L.append("")

    # ---- broker reconciliation ---------------------------------------------
    from dashboard.execution import broker
    L.append(f"## {broker.name()} fills vs paper (real broker execution)")
    try:
        rows = broker.reconcile()
        if rows:
            L.append("| paper_id | instrument | ticket | closed | paperR | demoR | pnl |")
            L.append("|---|---|---|---|---|---|---|")
            for r in rows:
                L.append(f"| {r['paper_id']} | {r['instrument']} | {r['ticket']} | "
                         f"{r['closed']} | {r['paper_r']:+.2f} | {r['demo_r']:+.2f} | "
                         f"{r['demo_pnl']:+.2f} |")
            done = [r for r in rows if r["closed"] and r["paper_status"] not in ("OPEN", "?")]
            if done:
                gap = sum(r["demo_r"] - r["paper_r"] for r in done) / len(done)
                L.append("")
                L.append(f"avg demo−paper R gap on {len(done)} closed: {gap:+.3f} "
                         f"(negative ⇒ paper cost model too optimistic)")
        else:
            L.append("(no mirrored demo trades yet)")
    except Exception as e:
        L.append(f"(demo reconciliation unavailable: {e})")
    return "\n".join(L)


def export() -> str:
    EXPORT_DIR.mkdir(exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = EXPORT_DIR / f"retrospective_{ts}.md"
    path.write_text(build(), encoding="utf-8")
    return str(path)


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="dump raw data as JSON")
    args = ap.parse_args()
    if args.json:
        print(json.dumps({
            "trades": paper.all_trades(),
            "rejections": journal.rejection_counts(),
            "macro": journal.macro_history(),
        }, indent=2, default=str))
        return
    report = build()
    path = export()
    print(report)
    print(f"\nWritten to {path}")


if __name__ == "__main__":
    main()
