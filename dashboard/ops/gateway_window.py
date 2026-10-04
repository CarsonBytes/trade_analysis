"""Should the gateway login watchdog be allowed to act right now?

ADDED 2026-09-05 after the user reported being 2FA-prompted on weekends. Confirmed in
/home/cap/gateway-restart.log: scripts/gateway-login-watchdog.sh runs every minute, 24/7,
with no calendar gate at all -- while the SCHEDULED relogin cron beside it is weekday-only
(`0 20 * * 1-5`). IBKR's weekly server reset means a gateway simply cannot hold a login
through much of the weekend, so the API port stays closed, so the watchdog cycled a relogin
(and pushed "APPROVE THE SECOND-FACTOR PROMPT" to the phone) three times an hour, all
weekend. Measured over 2026-08-29..31: 21 cycles Saturday, 52 Sunday, 31 Monday, against
4-11 on a normal weekday -- ~104 phone pushes across one weekend, none of them actionable.

Deliberately NOT a day-of-week rule: 2026-09-07 is Labor Day, a Monday the US market is
shut, and a weekday rule would have spammed right through it. The real question is "is
there a trading session to be logged in FOR", which market_calendar already answers from
the NYSE calendar (holidays included).

Exit status is the interface (this is called from bash):
    0 -> QUIET, the watchdog should not restart or notify
    1 -> ACTIVE, normal watchdog behaviour
    2 -> evaluation itself failed (calendar error) -> still treated as ACTIVE

STDOUT carries a sentinel, `QUIET` or `ACTIVE`, printed only on a successful
evaluation (and nothing at all otherwise). ADDED 2026-10-04 because exit codes
alone are ambiguous across the docker boundary: `docker exec` on a missing or
mid-recreate container exits 1 -- identical to this program's ACTIVE -- so the
caller cannot distinguish "the market is open" from "the container is gone".
See scripts/gateway-login-watchdog.sh::market_quiet for the consumer.

2 (ADDED 2026-10-04) is deliberately distinct from 1: the watchdog needs to tell
"the calendar said the market is open" apart from "I could not ask the calendar",
because the third state -- the container is simply unavailable, e.g. mid-recreate
during a deploy -- must not be mistaken for a real answer either. Everything
non-zero still means ACTIVE to a naive caller, so existing behaviour is unchanged.

Fails ACTIVE. A broken calendar must not silently disable the watchdog -- a gateway that is
logged out with nobody watching is the exact silent-outage class this project keeps hitting
(see HANDOFF's 2026-07-29 / 2026-08-26 / 2026-09-03 entries). A noisy phone beats an
undetected outage.
"""
from __future__ import annotations

import datetime as dt
import sys

# How long before the next open the watchdog wakes up. The routine login is the scheduled
# cron's job (20:00 HKT = 1.5h before the 21:30 HKT open); this is the safety net behind it,
# so it needs to be awake comfortably earlier than that, and no earlier.
LEAD_HOURS = 4.0


def should_be_quiet(now: dt.datetime | None = None) -> bool:
    """True when there is no session close enough to justify waking anyone up."""
    from dashboard.core import market_calendar
    now = now or dt.datetime.now(dt.timezone.utc)
    status = market_calendar.market_status(now)
    if status.get("is_open"):
        return False                                   # mid-session: always act
    nxt = status.get("next_change")
    if nxt is None or status.get("next_change_type") != "open":
        return False                                   # no answer -> fail ACTIVE
    return (nxt - now).total_seconds() > LEAD_HOURS * 3600.0


if __name__ == "__main__":
    try:
        quiet = should_be_quiet()
    except Exception as e:                             # noqa: BLE001 -- fail ACTIVE, distinctly
        print(f"gateway_window: could not evaluate ({e}) -- failing ACTIVE", file=sys.stderr)
        sys.exit(2)
    # Sentinel on stdout. Needed because exit codes alone are ambiguous across the
    # docker boundary: `docker exec` on a missing/recreating container exits 1, which
    # is the very same code this program uses for ACTIVE -- so a caller literally
    # cannot tell "the market is open" from "the container is gone". A token only a
    # successful evaluation can print is unambiguous.
    sys.stdout.write("QUIET\n" if quiet else "ACTIVE\n")
    sys.exit(0 if quiet else 1)
