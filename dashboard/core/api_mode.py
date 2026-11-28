"""Broker-API on/off switch (QUANT_WEEKEND_DISPLAY_SPEC §2.1).

Over the weekend the dashboards stay up for display while the broker API is off. The
switch is a one-line JSON flag file next to the journal DB (`/data/api-off` in Docker),
written and removed by `scripts/weekend-docker.sh`:

    {"reason": "weekend", "since": <epoch>, "until": <epoch>, "by": "weekend-docker.sh"}

While the flag is active the dashboard makes no broker connection and its tick skips all
broker/LLM work (no orders, no reconcile, no sweeps). It keeps rendering the last
persisted snapshot.

FAIL OPEN, LOUDLY. A flag that is malformed, has no usable `until`, or is past
`until + EXPIRY_GRACE_SEC` is IGNORED (normal trading resumes) and logged as a warning plus
one notable event, so a wake that failed to remove the flag can never keep the LIVE book
silently off into Monday.
"""
from __future__ import annotations

import json
import os
import pathlib
import time

EXPIRY_GRACE_SEC = 2 * 3600      # E4: the flag self-expires 2h after its `until` (wake time)
_CACHE_SEC = 10.0

_cache: dict = {"t": -1e18, "val": None, "warned": set()}


def flag_path() -> pathlib.Path:
    """`API_OFF_FLAG` if set, else `api-off` beside the journal DB (Docker: /data/api-off)."""
    p = os.environ.get("API_OFF_FLAG")
    if p:
        return pathlib.Path(p)
    from dashboard.core import store
    return store._dbpath().parent / "api-off"


def evaluate(raw: str | None, now: float) -> tuple[dict | None, str | None]:
    """Pure: (active_flag_or_None, warning_or_None) for a flag file's text at `now`."""
    if raw is None:
        return None, None
    try:
        d = json.loads(raw)
        if not isinstance(d, dict):
            raise ValueError("not a JSON object")
        until = float(d["until"])
    except Exception as e:                              # noqa: BLE001
        return None, f"api-off flag is malformed ({e}) -- ignored, broker API stays ON"
    if now >= until + EXPIRY_GRACE_SEC:
        return None, (f"api-off flag expired (until {_fmt(until)} + {EXPIRY_GRACE_SEC // 3600}h)"
                      " -- ignored, broker API back ON; the weekend wake did not remove it")
    d["until"] = until
    try:
        d["since"] = float(d.get("since") or 0) or None
    except Exception:                                   # noqa: BLE001
        d["since"] = None
    d.setdefault("reason", "weekend")
    return d, None


def _fmt(epoch: float) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(epoch, dt.timezone(dt.timedelta(hours=8))).strftime(
        "%a %Y-%m-%d %H:%M HKT")


def _read() -> str | None:
    try:
        return flag_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except Exception as e:                              # noqa: BLE001
        return f"<unreadable: {e}>"


def api_off(now: float | None = None) -> dict | None:
    """The active flag dict (keys reason/since/until/by) or None when the API is on.
    Cached for 10s; never raises."""
    t = time.monotonic()
    if now is None and t - _cache["t"] < _CACHE_SEC:
        return _cache["val"]
    try:
        raw = _read()
        val, warn = evaluate(raw, time.time() if now is None else now)
    except Exception as e:                              # noqa: BLE001
        val, warn = None, f"api-off check failed ({e}) -- broker API stays ON"
    if warn:
        _warn_once(warn, raw)
    if now is None:
        _cache["t"], _cache["val"] = t, val
    return val


def _warn_once(msg: str, raw: str | None) -> None:
    key = (msg.split(" (")[0], (raw or "")[:200])
    if key in _cache["warned"]:
        return
    _cache["warned"].add(key)
    try:
        from dashboard.core.log import log
        log.warning("api_mode: %s", msg)
    except Exception:                                   # noqa: BLE001
        pass
    try:
        from dashboard.core import notable_events
        notable_events.record(msg, level="warning", kind="api-mode")
    except Exception:                                   # noqa: BLE001
        pass


def describe(flag: dict | None = None) -> str:
    """Banner text for an active flag."""
    f = flag if flag is not None else api_off()
    if not f:
        return ""
    since = f" since {_fmt(f['since'])}" if f.get("since") else ""
    return f"Weekend mode: broker API off{since}, back {_fmt(f['until'])}."


def reset_cache() -> None:
    """Tests / immediate re-read."""
    _cache["t"], _cache["val"] = -1e18, None
    _cache["warned"] = set()
