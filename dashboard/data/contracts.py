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


# IBKR futures round-turn cost ~ commission (incl. exchange+regulatory fees) plus
# 1 tick of slippage per side. The backtest runs on FULL-SIZE continuous series,
# so this models the full-size commission (~$2.50 round-turn, IBKR tiered ballpark;
# micros are cheaper but the backtest doesn't use them). Override per call if you
# have a better number for a specific product.
DEFAULT_COMMISSION_RT = 2.50    # USD per contract, round-turn


def cost_points(spec: FutureSpec, commission_rt: float = DEFAULT_COMMISSION_RT,
                slippage_ticks: float = 1.0) -> float:
    """Realistic round-turn transaction cost for ONE contract, expressed in PRICE
    POINTS (so it slots straight into r_multiple alongside entry/sl/exit).

        cost_points = commission_rt / multiplier   +   2 * slippage_ticks * tick_size

    Commission converts to points via $/point (the multiplier); slippage is one
    tick on entry AND exit. Per-contract-invariant in R terms (both pnl and cost
    scale with size), so contract count doesn't enter. NOTE: for a weekly hold
    these costs are tiny (~0.002 R) -- materially smaller than the CFD half-spread
    they replace -- but this makes the futures backtest cost-honest rather than
    borrowing a fraction-of-price model that doesn't apply to futures.
    """
    return commission_rt / spec.multiplier + 2.0 * slippage_ticks * spec.tick_size


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


def size_shares(equity: float, stop_per_share: float, risk_pct: float) -> int:
    """Whole shares of an ETF such that an SL hit loses ~risk_pct of equity.
    `equity` and `stop_per_share` MUST be the same currency (USD for US ETFs --
    convert a non-USD account first). shares = floor(equity*risk_pct / stop_per_share).
    ETFs divide finely, so this is expressible on any account size (unlike futures)."""
    if stop_per_share <= 0 or equity <= 0 or risk_pct <= 0:
        return 0
    return int(math.floor((equity * risk_pct) / stop_per_share))


def min_equity_for_1_share(stop_per_share: float, risk_pct: float) -> float:
    """Inverse of size_shares: the smallest equity (same currency as stop_per_share,
    USD for US ETFs) that would size to >= 1 share at risk_pct. Used to explain WHY a
    signal that qualified couldn't be placed on the broker (a PENDING, unfunded trade) --
    e.g. 'needs ~$1,220 to size, you have $40 available' -- rather than silently vanishing
    into a phantom position with no explanation."""
    if stop_per_share <= 0 or risk_pct <= 0:
        return float("inf")
    return stop_per_share / risk_pct


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
    from dashboard.data import ib_client
    return ib_client.front_future(spec, asof or dt.date.today())


def continuous_history(spec: FutureSpec, n: int = 320, timeframe: str = "W1"):
    """Back-adjusted CONTINUOUS bars for SIGNALS/backtest (no roll gaps).

    Returns a DataFrame shaped like mt5_client.get_rates (UTC DatetimeIndex,
    [open,high,low,close]) or None. Uses IB CONTFUT historical data so the
    weekly MA/RSI/ATR see a smooth series across rolls.
    """
    from dashboard.data import ib_client
    return ib_client.continuous_rates(spec, timeframe=timeframe, n=n)


def needs_roll(expiry: dt.date, spec: FutureSpec, asof: dt.date | None = None) -> bool:
    """True if an OPEN position on a contract expiring `expiry` is inside the
    roll window and must be rolled (close front, open next) before expiry.
    Pure -- the date math is the decision; the actual roll lives in ib_exec.
    """
    asof = asof or dt.date.today()
    return _business_days_between(asof, expiry) <= spec.roll_offset_days


# Futures month codes (CME standard): F=Jan G=Feb H=Mar J=Apr K=May M=Jun
# N=Jul Q=Aug U=Sep V=Oct X=Nov Z=Dec.
_MONTH_CODE = {1: "F", 2: "G", 3: "H", 4: "J", 5: "K", 6: "M",
               7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z"}


def front_month(spec: FutureSpec, asof: dt.date | None = None) -> tuple[int, str, dt.date]:
    """PURE planning answer to "which contract month should I hold on `asof`?".

    Returns (year, month_code, approx_last_trade). Walks `spec.months` forward and
    picks the nearest delivery month whose APPROXIMATE last-trade date is more than
    `roll_offset_days` business days ahead (roll early). The last-trade date is
    APPROXIMATED as the 15th of the delivery month -- good enough for planning,
    sanity-checking and backtest roll scheduling, but NOT exact (real expiries vary
    by product: equity index = 3rd Friday, many commodities expire the month
    BEFORE delivery). For live orders, ib_client.front_future uses the broker's
    actual lastTradeDate and OVERRIDES this. Use this to cross-check that choice.
    """
    asof = asof or dt.date.today()
    months = sorted(_MONTH_CODE_NUM(c) for c in spec.months)
    for year in (asof.year, asof.year + 1, asof.year + 2):
        for m in months:
            approx_last = dt.date(year, m, 15)
            if _business_days_between(asof, approx_last) > spec.roll_offset_days:
                return year, _MONTH_CODE[m], approx_last
    raise ValueError(f"no front month resolvable for {spec.key} at {asof}")


def _MONTH_CODE_NUM(code: str) -> int:
    for num, c in _MONTH_CODE.items():
        if c == code:
            return num
    raise ValueError(f"bad month code {code!r}")


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


def _main() -> None:
    """CLI: which front month should each contract hold on a given date?
        uv run python -m dashboard.data.contracts 2026-07-01
    Cross-checks the PURE approximation against IB's real lastTradeDate when a
    Gateway is reachable (the broker value is ground truth)."""
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--key", help="only this contract (e.g. GC)")
    args = ap.parse_args()
    asof = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    specs = [SPECS[args.key]] if args.key else [s for s in SPECS.values() if s.micro_of is None]
    print(f"front month as of {asof}  (approx = pure 15th-of-month rule; "
          f"broker = IB lastTradeDate if reachable)\n")
    print(f"  {'key':<5} {'approx':<10} {'roll~':<12} {'broker':<10}")
    for spec in specs:
        y, code, last = front_month(spec, asof)
        broker = "-"
        try:
            from dashboard.data import ib_client
            c = ib_client.front_future(spec, asof)
            broker = getattr(c, "localSymbol", "-") if c is not None else "(IB down)"
        except Exception:
            broker = "(IB down)"
        print(f"  {spec.key:<5} {code}{str(y)[2:]:<9} {str(last):<12} {broker:<10}")


if __name__ == "__main__":
    _main()
