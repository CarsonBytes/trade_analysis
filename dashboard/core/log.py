"""Debug logging for auditing the system's decisions.

Writes a rotating file at logs/dashboard.log (and INFO+ to the console). Use it
to confirm the assumptions are holding:
  - which DATA SOURCE each refresh used (MT5 tick vs yfinance bar) + tick age
  - every entry-funnel decision WITH its reason (placed / why skipped)
  - every resolution: which path (exact ticks vs conservative OHLC bars), how
    many ticks/bars were examined, which level hit first, exit price/time, R
  - WARNING whenever the conservative 'SL-before-TP in one bar' assumption is
    actually applied — so you can see if/when it matters
"""
from __future__ import annotations

import logging
import logging.handlers
import pathlib

# repo-root logs/ (log.py is in dashboard/core/, so parents[2] is the repo root).
# parent.parent would resolve to dashboard/ after the reorg -- same path bug class
# as paper._DB / net._QUANT_DIR.
_LOG_DIR = pathlib.Path(__file__).resolve().parents[2] / "logs"


def get_logger() -> logging.Logger:
    logger = logging.getLogger("dashboard")
    if logger.handlers:
        return logger
    _LOG_DIR.mkdir(exist_ok=True)
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")

    # ~50 MB of history (10 MB x 5) so a multi-week forward test isn't silently
    # truncated. The structured audit lives in SQLite (journal.py); this is the
    # human-readable narrative backup.
    fh = logging.handlers.RotatingFileHandler(
        _LOG_DIR / "dashboard.log", maxBytes=10_000_000, backupCount=5, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    logger.propagate = False
    return logger


log = get_logger()


def get_access_logger() -> logging.Logger:
    """ADDED 2026-07-14: separate rotating file at logs/access.log for HTTP-level access
    logging (client IP + method + path + status + user-agent per request) -- kept in its
    OWN logger/file, not the main `dashboard` one, so reviewing "who hit the dashboard" isn't
    mixed in with the trading/monitoring narrative log. Added when quant.carsonng.com's
    Cloudflare Access (login) gate was removed to make it public -- this is the compensating
    visibility control, run entirely locally (not dependent on a paid Cloudflare Logpush
    plan)."""
    logger = logging.getLogger("dashboard.access")
    if logger.handlers:
        return logger
    _LOG_DIR.mkdir(exist_ok=True)
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(
        _LOG_DIR / "access.log", maxBytes=10_000_000, backupCount=5, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.propagate = False    # don't also spam this into dashboard.log
    return logger


access_log = get_access_logger()
