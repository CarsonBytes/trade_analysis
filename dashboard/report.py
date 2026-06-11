"""Export the paper-trade journal for record-keeping and sharing.

Produces two files in exports/:
  - trades_<ts>.csv   : every trade, raw (for your records / spreadsheets)
  - report_<ts>.md    : a readable scorecard (stats by method / instrument /
                        direction + open & closed lists) — paste this to share.

The report leads with expectancy-in-R (the number that predicts profitability),
not win rate, and flags when the sample is too small to trust.

Run:  python -m dashboard.report          # writes files + prints the report
"""
from __future__ import annotations

from . import net  # noqa: F401

import csv
import datetime as dt
import pathlib

from . import paper
from .instruments import BY_KEY

EXPORT_DIR = pathlib.Path(__file__).resolve().parent.parent / "exports"


def _fmt(s: dict) -> str:
    pf = "inf" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
    flag = "" if s["trustworthy"] else "  (!) n<30 (noisy)"
    return (f"n={s['n']:<3} win={s['win_rate']:.0%}  expR={s['expectancy_R']:+.3f}  "
            f"avgWin={s['avg_win_R']:+.2f}  avgLoss={s['avg_loss_R']:+.2f}  "
            f"PF={pf}  totR={s['total_R']:+.1f}{flag}")


def _group(closed: list[dict], keyfn) -> dict[str, dict]:
    groups: dict[str, list[float]] = {}
    for t in closed:
        groups.setdefault(keyfn(t), []).append(t["realized_r"])
    return {k: paper.stats(v) for k, v in sorted(groups.items())}


def build_report() -> str:
    allt = paper.all_trades()
    closed = [t for t in allt if t["status"] != "OPEN"]
    opent = [t for t in allt if t["status"] == "OPEN"]
    rs = [t["realized_r"] for t in closed]

    L: list[str] = []
    L.append(f"# Paper-trade report — {dt.datetime.now():%Y-%m-%d %H:%M}")
    L.append(f"Total {len(allt)} | open {len(opent)} | closed {len(closed)}")
    L.append("")
    L.append("## Overall (closed)")
    L.append(_fmt(paper.stats(rs)))
    L.append("")
    L.append("## By SL/TP method")
    for k, s in _group(closed, lambda t: t["method"]).items():
        L.append(f"- {k:<12} {_fmt(s)}")
    L.append("")
    L.append("## By instrument")
    for k, s in _group(closed, lambda t: t["instrument"]).items():
        L.append(f"- {k:<8} {_fmt(s)}")
    L.append("")
    L.append("## By direction")
    for k, s in _group(closed, lambda t: t["direction"]).items():
        L.append(f"- {k:<6} {_fmt(s)}")
    L.append("")
    L.append("## Closed trades")
    L.append("| instrument | dir | method | status | R | entry | exit | exit_time |")
    L.append("|---|---|---|---|---|---|---|---|")
    for t in closed:
        L.append(f"| {t['instrument']} | {t['direction']} | {t['method']} | "
                 f"{t['status']} | {t['realized_r']:+.2f} | {t['entry']:.4f} | "
                 f"{t['exit_price']:.4f} | {t['exit_ts'][:16]} |")
    L.append("")
    L.append("## Open trades")
    L.append("| instrument | dir | method | entry | SL | TP | R:R | opened |")
    L.append("|---|---|---|---|---|---|---|---|")
    for t in opent:
        L.append(f"| {t['instrument']} | {t['direction']} | {t['method']} | "
                 f"{t['entry']:.4f} | {t['sl']:.4f} | {t['tp']:.4f} | {t['rr']} | "
                 f"{t['ts'][:16]} |")
    return "\n".join(L)


def export() -> tuple[str, str]:
    """Write CSV + Markdown report. Returns (csv_path, report_path)."""
    EXPORT_DIR.mkdir(exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = EXPORT_DIR / f"trades_{ts}.csv"
    rep_path = EXPORT_DIR / f"report_{ts}.md"

    allt = paper.all_trades()
    if allt:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(allt[0].keys()))
            w.writeheader()
            w.writerows(allt)
    rep_path.write_text(build_report(), encoding="utf-8")
    return str(csv_path), str(rep_path)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", action="store_true",
                    help="snapshot + archive all trades and reset counts to zero")
    args = ap.parse_args()
    if args.archive:
        r = paper.archive_and_reset()
        print(f"Archived {r['archived']} trade(s) as batch {r['batch']}.")
        print(f"Snapshot saved:\n  {r['csv']}\n  {r['report']}")
        print("Live journal reset — counting starts fresh.")
        print("\nArchive batches:")
        for b in paper.archive_batches():
            print(f"  {b['batch']}  ({b['n']} trades)")
    else:
        csvp, repp = export()
        print(f"Wrote:\n  {csvp}\n  {repp}\n")
        print(build_report())
