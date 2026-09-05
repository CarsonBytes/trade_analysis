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
    except Exception as e:                             # noqa: BLE001 -- fail ACTIVE, loudly
        print(f"gateway_window: could not evaluate ({e}) -- failing ACTIVE", file=sys.stderr)
        sys.exit(1)
    sys.exit(0 if quiet else 1)
