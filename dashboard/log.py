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

_LOG_DIR = pathlib.Path(__file__).resolve().parent.parent / "logs"


def get_logger() -> logging.Logger:
    logger = logging.getLogger("dashboard")
    if logger.handlers:
        return logger
    _LOG_DIR.mkdir(exist_ok=True)
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")

    fh = logging.handlers.RotatingFileHandler(
        _LOG_DIR / "dashboard.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
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
