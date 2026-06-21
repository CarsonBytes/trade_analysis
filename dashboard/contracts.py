"""Futures contract specs, risk-based position sizing, and roll logic.

This is the part MT5 hid from us. CFDs don't expire and MT5 exposed
`order_calc_profit` to turn an SL distance into money; IB futures do neither, so:

  - SPECS is a curated table of contract multipliers/ticks/exchanges (with a
    runtime cross-check against IB `reqContractDetails`, see ib_client).
  - `size_contracts` computes whole-contract size from RISK, using the spec's
    $/point -- the same honest math the backtest should use (so paper == live).
  - the roll helpers pick the front month and decide when an open position must
    roll, because a ~7-week weekly hold can straddle a quarterly expiry.

CRITICAL split (the #1 correctness trap): SIGNALS run on a continuous
back-adjusted series (`continuous_history`), ORDERS go on the dated front month
(`front_contract`). Never size/score off the same object you trade.

The IB-touching functions (`front_contract`, `continuous_history`) import
ib_client lazily and degrade to None when no gateway is up, so this module --
and its pure sizing math -- is importable and testable with no IB installed.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field


# ---- contract specs --------------------------------------------------------

@dataclass(frozen=True)
class FutureSpec:
    """Everything sizing + ordering needs for one futures market.

    `key` matches the stable Instrument.key in instruments.py so the journal,
    scoring and asset-class gating (WEEKLY_TREND_CLASSES) all keep working.
    `multiplier` is $ per 1.0 of price move (a.k.a. point value). `tick_value`
    is `tick_size * multiplier` and is stored only for cross-checking the spec
    against the broker. `micro_of` points a micro at its full-size sibling key
    so size_contracts can prefer the finer-grained instrument.
    """
    key: str
    symbol: str            # IB symbol (e.g. "MES")
    exchange: str          # IB exchange (e.g. "CME", "NYMEX", "CBOT")
    currency: str          # contract currency (USD unless noted)
    multiplier: float      # $ (in `currency`) per 1.0 price move
    tick_size: float       # minimum price increment
    asset_class: str       # metal|energy|index|fx|rate|grain|soft -- gating
    months: str = "HMUZ"   # active delivery months (quarterly default)
    roll_offset_days: int = 5   # roll this many business days before expiry
    micro_of: str | None = None  # key of the full-size sibling, if this is a micro

    @property
    def tick_value(self) -> float:
        return self.tick_size * self.multiplier


# Curated table. multiplier/tick are contract-spec facts (CME/CBOT/NYMEX). The
# micros (M*) mirror their big siblings at 1/10 size and exist for precise risk
# on a modest paper account. Cross-checked at runtime in ib_client.contract_check.
SPECS: dict[str, FutureSpec] = {s.key: s for s in [
    # --- equity indices (CME, quarterly HMUZ) ---
    FutureSpec("ES",  "ES",  "CME", "USD",  50.0, 0.25, "index"),
    FutureSpec("MES", "MES", "CME", "USD",   5.0, 0.25, "index", micro_of="ES"),
    FutureSpec("NQ",  "NQ",  "CME", "USD",  20.0, 0.25, "index"),
    FutureSpec("MNQ", "MNQ", "CME", "USD",   2.0, 0.25, "index", micro_of="NQ"),
    FutureSpec("YM",  "YM",  "CBOT","USD",   5.0, 1.0,  "index"),
    FutureSpec("MYM", "MYM", "CBOT","USD",   0.5, 1.0,  "index", micro_of="YM"),
    FutureSpec("RTY", "RTY", "CME", "USD",  50.0, 0.10, "index"),
    FutureSpec("M2K", "M2K", "CME", "USD",   5.0, 0.10, "index", micro_of="RTY"),
    # --- metals (NYMEX/COMEX) ---
    FutureSpec("GC",  "GC",  "COMEX","USD", 100.0, 0.10, "metal", months="GJMQVZ"),
    FutureSpec("MGC", "MGC", "COMEX","USD",  10.0, 0.10, "metal", months="GJMQVZ", micro_of="GC"),
    FutureSpec("SI",  "SI",  "COMEX","USD",5000.0, 0.005,"metal", months="HKNUZ"),
    FutureSpec("SIL", "SIL", "COMEX","USD",1000.0, 0.005,"metal", months="HKNUZ", micro_of="SI"),
    FutureSpec("HG",  "HG",  "COMEX","USD",25000.0,0.0005,"metal",months="HKNUZ"),
    # --- energy (NYMEX, monthly) ---
    FutureSpec("CL",  "CL",  "NYMEX","USD",1000.0, 0.01, "energy", months="FGHJKMNQUVXZ"),
    FutureSpec("MCL", "MCL", "NYMEX","USD", 100.0, 0.01, "energy", months="FGHJKMNQUVXZ", micro_of="CL"),
    FutureSpec("NG",  "NG",  "NYMEX","USD",10000.0,0.001,"energy", months="FGHJKMNQUVXZ"),
    # --- rates (CBOT) -- the key equity diversifier ---
    FutureSpec("ZN",  "ZN",  "CBOT","USD",1000.0, 0.015625, "rate"),   # 1/64 of a point
    FutureSpec("ZB",  "ZB",  "CBOT","USD",1000.0, 0.03125,  "rate"),   # 1/32
    FutureSpec("ZF",  "ZF",  "CBOT","USD",1000.0, 0.0078125,"rate"),   # 1/128
    # --- grains (CBOT) ---
    FutureSpec("ZC",  "ZC",  "CBOT","USD",  50.0, 0.25, "grain", months="HKNUZ"),
    FutureSpec("ZW",  "ZW",  "CBOT","USD",  50.0, 0.25, "grain", months="HKNUZ"),
    FutureSpec("ZS",  "ZS",  "CBOT","USD",  50.0, 0.25, "grain", months="FHKNQUX"),
    # --- softs (ICE) ---
    FutureSpec("KC",  "KC",  "NYBOT","USD", 375.0, 0.05, "soft", months="HKNUZ"),
    FutureSpec("SB",  "SB",  "NYBOT","USD",1120.0, 0.01, "soft", months="HKNV"),
    FutureSpec("CT",  "CT",  "NYBOT","USD", 500.0, 0.01, "soft", months="HKNVZ"),
    # --- FX futures (CME) -- non-USD contract ccy handled in sizing ---
    FutureSpec("6E",  "6E",  "CME", "USD",125000.0,0.00005,"fx"),
    FutureSpec("6J",  "6J",  "CME", "USD",12500000.0,0.0000005,"fx"),
    FutureSpec("6A",  "6A",  "CME", "USD",100000.0,0.00005,"fx"),
]}


# ---- risk-based sizing (PURE -- shared by ib_exec and backtest.py) ---------

def risk_per_contract(spec: FutureSpec, stop_points: float,
                      fx_to_usd: float = 1.0) -> float:
    """$ (account ccy) lost if the SL is hit holding ONE contract.

    stop_points is the SL distance in PRICE units (= SL_ATR_MULT * ATR). For a
    non-USD contract, pass fx_to_usd = price of 1 unit of `spec.currency` in USD
    (e.g. EURUSD for a EUR-denominated contract); USD contracts use 1.0.
    """
    return abs(stop_points) * spec.multiplier * fx_to_usd


def size_contracts(spec: FutureSpec, equity: float, stop_points: float,
                   risk_pct: float, fx_to_usd: float = 1.0) -> int:
    """Whole contracts of `spec` such that an SL hit loses ~`risk_pct` of equity.

        contracts = floor( (equity * risk_pct) / (stop_points * multiplier) )

    Returns 0 when even one contract risks more than the budget -- the caller
    should then try the micro sibling (see choose_contract) or SKIP. Never
    rounds up: oversizing the low-risk mandate is the cardinal sin here.
    """
    if stop_points <= 0 or equity <= 0 or risk_pct <= 0:
        return 0
    risk_money = equity * risk_pct
    per = risk_per_contract(spec, stop_points, fx_to_usd)
    if per <= 0:
        return 0
    return int(math.floor(risk_money / per))


def choose_contract(spec: FutureSpec, equity: float, stop_points: float,
                    risk_pct: float, fx_to_usd: float = 1.0,
                    ) -> tuple[FutureSpec, int]:
    """Pick the tradable contract + size that best fits the risk budget.

    If the full-size contract sizes to >= 1, use it. Otherwise fall back to its
    micro sibling (10x finer) so a modest account can still express 0.5% risk.
    Returns (chosen_spec, contracts); contracts == 0 means SKIP (too big even as
    a micro -- caller logs and places nothing).
    """
    n = size_contracts(spec, equity, stop_points, risk_pct, fx_to_usd)
    if n >= 1:
        return spec, n
    # find a micro that points at this spec
    micro = next((s for s in SPECS.values() if s.micro_of == spec.key), None)
    if micro is not None:
        m = size_contracts(micro, equity, stop_points, risk_pct, fx_to_usd)
        if m >= 1:
            return micro, m
    return spec, 0


# ---- roll logic (front month + when to roll an open position) --------------
# These touch IB; they import ib_client lazily and return None when no gateway.

def front_contract(spec: FutureSpec, asof: dt.date | None = None):
    """Resolve the active dated front-month IB Contract to TRADE.

    Picks the nearest non-expired contract whose expiry is more than
    `roll_offset_days` business days ahead of `asof` (roll early -- volume/OI
    migrates before expiry). Returns an ib_async Contract, or None if IB is
    unavailable. Orders go on THIS; never score signals off it.
    """
    from . import ib_client
    return ib_client.front_future(spec, asof or dt.date.today())


def continuous_history(spec: FutureSpec, n: int = 320, timeframe: str = "W1"):
    """Back-adjusted CONTINUOUS bars for SIGNALS/backtest (no roll gaps).

    Returns a DataFrame shaped like mt5_client.get_rates (UTC DatetimeIndex,
    [open,high,low,close]) or None. Uses IB CONTFUT historical data so the
    weekly MA/RSI/ATR see a smooth series across rolls.
    """
    from . import ib_client
    return ib_client.continuous_rates(spec, timeframe=timeframe, n=n)


def needs_roll(expiry: dt.date, spec: FutureSpec, asof: dt.date | None = None) -> bool:
    """True if an OPEN position on a contract expiring `expiry` is inside the
    roll window and must be rolled (close front, open next) before expiry.
    Pure -- the date math is the decision; the actual roll lives in ib_exec.
    """
    asof = asof or dt.date.today()
    return _business_days_between(asof, expiry) <= spec.roll_offset_days


def _business_days_between(a: dt.date, b: dt.date) -> int:
    """Mon-Fri days from a (exclusive) to b (inclusive). Negative if b < a.
    Coarse (ignores exchange holidays) -- roll_offset_days carries slack."""
    if b < a:
        return -_business_days_between(b, a)
    days, d = 0, a
    one = dt.timedelta(days=1)
    while d < b:
        d += one
        if d.weekday() < 5:
            days += 1
    return days
