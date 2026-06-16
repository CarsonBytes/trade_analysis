"""MT5 link monitor + reconnect-to-reroll (option B).

The broker serves the account through several named ACCESS POINTS (e.g. NY.01
~198ms vs HK-Demo ~240ms). The terminal picks one and sometimes drifts to a
slower one. The MT5 API can't select an access point directly -- the only lever
is to RECONNECT (login to the same server), which makes the terminal re-roll its
choice. This thread watches ping per access point and, on sustained degradation,
re-rolls until it lands on the best one.

Safety:
  - runs in a daemon thread; the strategy loop never blocks on it
  - re-rolls ONLY when no order is in flight (executor holds ORDER_GATE)
  - cooldown between re-rolls; capped attempts per episode
  - degrades to MONITOR-ONLY (no reconnect) when MT5_LOGIN/PASSWORD are absent

Access-point name + ping are scraped from the terminal journal log, which prints
e.g. "authorized on ICMarketsSC-Demo through Access Server NY.01 (ping: 198 ms)".
"""
from __future__ import annotations

import os
import re
import time
import threading
import statistics
import pathlib

from . import mt5_client
from .log import log

# Held by the executor while placing/closing orders. The re-roll takes it with a
# non-blocking try and skips the cycle if an order is in flight.
ORDER_GATE = threading.Lock()

_LOG_RE = re.compile(r"through Access Server ([\w.\-]+) \(ping: ([\d.]+) ms")


class LinkMonitor(threading.Thread):
    def __init__(self, poll: int = 60, min_margin_ms: float = 15.0,
                 cooldown_s: int = 60, max_rerolls: int = 6, keep: int = 50,
                 target_ms: float = 220.0):
        super().__init__(daemon=True, name="mt5-link-monitor")
        self.poll = poll
        self.min_margin_ms = min_margin_ms     # also reroll if a known AP is this faster
        self.cooldown_s = cooldown_s
        self.max_rerolls = max_rerolls
        self.keep = keep
        # ping we consider "good enough". Above this we re-roll to TRY for a
        # faster access point -- this is what lets us DISCOVER a better one even
        # when we've only ever seen the current (slow) one. Set between your
        # fast and slow access-point pings (e.g. NY.01 ~198 vs HK-Demo ~240).
        self.target_ms = target_ms
        self._stop = threading.Event()
        self.history: dict[str, list[float]] = {}   # access point -> recent pings
        self.last_reroll = 0.0
        # set True once we learn reconnecting can't change the access point on
        # this broker (login() is deterministic) -> stop wasting reconnects.
        self.reroll_disabled = False
        self.state: dict = {}                        # snapshot for the UI

    def stop(self) -> None:
        self._stop.set()

    # --- log scraping -------------------------------------------------------
    def _scan(self, last_n: int = 60) -> list[tuple[str, float]]:
        """All (access_point, ping) from the last `last_n` matching journal
        lines, oldest->newest. Covers EVERY access point the terminal has used
        recently, so we can compare even while sitting on a slow one."""
        dp = mt5_client.data_path()
        if not dp:
            return []
        # dated trading logs only (YYYYMMDD.log) -- skip metaeditor.log etc.
        logs = sorted(p for p in (pathlib.Path(dp) / "logs").glob("*.log")
                      if p.stem.isdigit())
        if not logs:
            return []
        hits: list[tuple[str, float]] = []
        for logf in reversed(logs[-3:]):       # span a few days if needed
            try:
                # MT5 journal logs are UTF-16-LE (BOM); 'utf-16' honours the BOM.
                raw = logf.read_bytes()
                enc = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8"
                lines = raw.decode(enc, errors="ignore").splitlines()
            except Exception:
                continue
            for line in lines:
                m = _LOG_RE.search(line)
                if m:
                    hits.append((m.group(1), float(m.group(2))))
            if len(hits) >= last_n:
                break
        return hits[-last_n:]

    def _best(self) -> tuple[str | None, float | None]:
        means = {ap: statistics.mean(p) for ap, p in self.history.items() if p}
        if not means:
            return None, None
        ap = min(means, key=means.get)
        return ap, means[ap]

    # --- main loop ----------------------------------------------------------
    def run(self) -> None:
        login = os.environ.get("MT5_LOGIN")
        password = os.environ.get("MT5_PASSWORD", "")
        while not self._stop.wait(self.poll):
            try:
                # rebuild per-access-point ping history from the recent log
                scan = self._scan()
                self.history = {}
                for a, p in scan:
                    self.history.setdefault(a, []).append(p)
                for a in self.history:
                    self.history[a] = self.history[a][-self.keep:]
                ap, _logping = scan[-1] if scan else (None, None)
                # current ping: prefer the LIVE ping_last over the (possibly
                # stale) last log line.
                conn = mt5_client.connection_status()
                ping = conn["ping_ms"] if conn else _logping
                best_ap, best_mean = self._best()
                self.state = {
                    "access_point": ap, "ping_ms": ping,
                    "best_ap": best_ap, "best_ping": round(best_mean, 0) if best_mean else None,
                    "can_reroll": bool(login) and not self.reroll_disabled,
                    "reroll_disabled": self.reroll_disabled, "target_ms": self.target_ms,
                    "history": {k: round(statistics.mean(v)) for k, v in self.history.items()},
                }
                if not login or ping is None or self.reroll_disabled:
                    continue
                # Re-roll when the current link is above the acceptable target
                # (this DISCOVERS a faster access point even if we've only seen
                # the slow one), OR when a known access point is clearly faster.
                need = ping > self.target_ms
                if (best_ap and best_ap != ap and best_mean
                        and ping > best_mean + self.min_margin_ms):
                    need = True
                if need and (time.time() - self.last_reroll) > self.cooldown_s:
                    self._reroll(int(login), password)
            except Exception as e:
                log.debug("link monitor: %s", e)

    def _reroll(self, login: int, password: str) -> None:
        """Reconnect (which re-rolls the terminal's access-point pick) up to
        max_rerolls times, stopping as soon as we land on a link at/under the
        target ping. Can't pin a specific access point via the API, so this is
        'reconnect until acceptable'."""
        conn = mt5_client.connection_status()
        if not conn:
            return
        server = conn["server"]
        if not ORDER_GATE.acquire(blocking=False):
            log.info("link monitor: order in flight -- deferring reroll")
            return
        try:
            start = conn["ping_ms"]
            log.info("link monitor: rerolling %s (now %.0fms, target ≤%.0fms)",
                     server, start, self.target_ms)
            seen_aps: set = set()
            for attempt in range(1, self.max_rerolls + 1):
                mt5_client.reconnect(login, password, server)
                time.sleep(3)            # let it authorize + log the access point
                scan = self._scan()
                ap, _ = scan[-1] if scan else (None, None)
                c = mt5_client.connection_status()
                ping = c["ping_ms"] if c else None
                if ap:
                    seen_aps.add(ap)
                log.info("link monitor: reroll %d/%d -> %s (%.0fms)",
                         attempt, self.max_rerolls, ap, ping if ping else -1)
                if ping is not None and ping <= self.target_ms:
                    log.info("link monitor: landed on %s at %.0fms (≤target)", ap, ping)
                    break
                # bail early once it's clear reconnect won't change the AP
                if attempt >= 2 and len(seen_aps) <= 1:
                    break
            # if reconnecting never produced a different access point, this
            # broker's login() is deterministic -- the API can't switch APs.
            if len(seen_aps) <= 1:
                self.reroll_disabled = True
                log.warning("link monitor: reconnect always lands on %s -- the MT5 "
                            "API cannot switch access points on this broker. "
                            "Auto-reroll DISABLED; pin a faster access point "
                            "manually via the MT5 connection icon.", next(iter(seen_aps), "?"))
            self.last_reroll = time.time()
        finally:
            ORDER_GATE.release()


_monitor: LinkMonitor | None = None


def start(**kw) -> LinkMonitor:
    global _monitor
    if _monitor is None:
        _monitor = LinkMonitor(**kw)
        _monitor.start()
        log.info("link monitor started (poll %ds, reroll=%s)",
                 _monitor.poll, bool(os.environ.get("MT5_LOGIN")))
    return _monitor


def status() -> dict:
    return _monitor.state if _monitor else {}


def ap_stats() -> list[dict]:
    """Per-access-point {ap, n, mean_ms, jitter_ms}, fastest first. For the
    'advise' UI -- shows which access point to prefer."""
    if not _monitor:
        return []
    out = []
    for ap, pings in _monitor.history.items():
        if not pings:
            continue
        out.append({
            "ap": ap, "n": len(pings), "mean_ms": round(statistics.mean(pings)),
            "jitter_ms": round(statistics.pstdev(pings)) if len(pings) > 1 else 0,
        })
    out.sort(key=lambda r: r["mean_ms"])
    return out
