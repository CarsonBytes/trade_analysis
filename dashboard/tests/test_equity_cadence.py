"""equity_history / sgov_history sampling cadence (2026-10-03).

The regression this pins down: commit 2aabd62 (2026-09-26) switched both series from a
~10min throttle to "one point per UTC day" so the 3000-point budget would span years
instead of ~21 days. Long history was the goal, but every CONSUMER of the series silently
became day-granular -- measured live on 2026-10-03:

    points per UTC day   09-24: 136   09-25: 135   09-26: 40   09-27..10-03: 1 each
    newest point         4h stale while the tick loop was running every ~2min
    "P&L over time" @1W  7 points, each up to 24h old

So the 1-week P&L the user reads is a staircase of day-old samples. The fix keeps BOTH
properties: sample on the documented ~10min cadence, and when the budget is exceeded,
halve the resolution of the OLDER prefix rather than discarding it -- so recent charts
are exact and old history still spans months.

Run:  uv run python -m dashboard.tests.test_equity_cadence
"""
from __future__ import annotations

import os
import tempfile

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
    old = os.environ.get("DASH_DB_NAME")
    os.environ["DASH_DB_NAME"] = path
    return old, path


def _restore_db(old, path):
    if old is None:
        os.environ.pop("DASH_DB_NAME", None)
    else:
        os.environ["DASH_DB_NAME"] = old
    try:
        os.remove(path)
    except OSError:
        pass


def test_cadence_constants_are_the_documented_ones():
    print("\nsampling constants: ~10min cadence, 3000 budget, 10-day exact tail:")
    from dashboard.web import service
    check("cadence back to 10min (not one-per-day)", service.EQUITY_SNAPSHOT_MIN_SEC, 600)
    check("budget unchanged", service.EQUITY_HISTORY_MAX, 3000)
    check("tail kept at full resolution", service.EQUITY_TAIL_KEEP, 1440)


def test_thin_keeps_recent_tail_at_full_resolution():
    print("\n_thin_history(): the RECENT window keeps every point (this is what 1W draws):")
    from dashboard.web import service
    cap, tail = 3000, 1440
    hist = [[i * 600, 100000.0 + i, "HKD"] for i in range(cap + 500)]   # 10-min spacing
    out = service._thin_history(hist, cap=cap, tail=tail)
    check("fits the budget", len(out) <= cap, True)
    check("newest point preserved", out[-1], hist[-1])
    check("entire tail preserved verbatim (no gaps in the recent chart)",
          out[-tail:], hist[-tail:])
    recent_gaps = {out[i][0] - out[i - 1][0] for i in range(len(out) - tail + 1, len(out))}
    check("recent spacing is still 600s", recent_gaps, {600})


def test_thin_halves_the_old_prefix_instead_of_dropping_it():
    print("\n_thin_history(): old history degrades in resolution, it does NOT disappear:")
    from dashboard.web import service
    cap, tail = 3000, 1440
    hist = [[i * 600, 100000.0 + i, "HKD"] for i in range(cap + 500)]
    out = service._thin_history(hist, cap=cap, tail=tail)
    head = out[:len(out) - tail]
    check("older prefix is halved", len(head), (len(hist) - tail + 1) // 2)
    head_gaps = {head[i][0] - head[i - 1][0] for i in range(1, len(head))}
    check("older prefix now at 1200s spacing", head_gaps, {1200})
    check("oldest data still present", out[0][0] <= hist[0][0] + 600, True)
    check("span is longer than the old 3000-point truncation would give",
          out[-1][0] - out[0][0] > hist[cap][0] - hist[0][0], True)


def test_same_day_second_point_is_due():
    print("\n_should_snapshot(): the rule that was broken -- a second reading on the SAME "
          "day must be recorded:")
    from dashboard.web import service
    day = 24 * 3600
    base = 1000 * day                      # some timestamp, arbitrary day
    check("first point ever is always due", service._should_snapshot(None, base), True)
    check("same day, 5min later -> NOT due (this is the throttle working)",
          service._should_snapshot(base, base + 300), False)
    check("same day, 10min later -> DUE (the per-day gate skipped this entirely)",
          service._should_snapshot(base, base + 600), True)
    check("same day, 20min later -> DUE", service._should_snapshot(base, base + 1200), True)
    check("next day -> DUE", service._should_snapshot(base, base + 86400), True)
    check("explicit min_sec honoured", service._should_snapshot(base, base + 60, min_sec=60), True)


def test_thin_is_idempotent_and_safe():
    print("\n_thin_history(): no-ops when under budget, and never returns junk:")
    from dashboard.web import service
    small = [[i * 600, 1.0, "HKD"] for i in range(10)]
    check("under cap -> returned unchanged (same object)", service._thin_history(small) is small, True)
    check("empty list is fine", service._thin_history([]), [])
    check("tail=0 degrades to a plain truncation, never an empty result",
          len(service._thin_history([[i, 1.0] for i in range(5000)], cap=100, tail=0)), 100)


def test_live_1w_window_keeps_its_resolution():
    print("\nthe actual 1W chart window survives a thinning pass:")
    from dashboard.web import service
    cap, tail = 3000, 1440
    # 30 days of 10-min points, then thin it the way the recorder would
    hist = [[i * 600, 100000.0, "HKD"] for i in range(30 * 144)]
    thinned = service._thin_history(hist, cap=cap, tail=tail)
    week_start = thinned[-1][0] - 7 * 86400
    in_week = [e for e in thinned if e[0] >= week_start]
    check("a full 7-day window still has hundreds of points, not 7",
          len(in_week) > 100, True)
    check("every 7d point survives the pass (no holes)",
          len(in_week), len([e for e in hist if e[0] >= week_start]))
    check("window spacing stays at the 10min cadence", in_week[1][0] - in_week[0][0], 600)


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