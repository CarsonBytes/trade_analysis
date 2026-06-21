"""Instrument universe: the popular, liquid signals across asset classes.

Each instrument carries the symbol for each data provider. MT5 symbols are
broker-dependent (these match IC Markets); yfinance is the no-terminal fallback.
Keys are kept stable across versions so the trade journal stays continuous
(e.g. WTI/SPX/NDX predate the symbol-style keys).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Instrument:
    key: str          # internal id / display label (stable)
    name: str         # human name
    yf: str           # yfinance ticker (fallback)
    mt5: str          # MT5 symbol (broker-dependent)
    asset_class: str  # "metal" | "energy" | "fx" | "index" | "crypto"


UNIVERSE: list[Instrument] = [
    # --- metals (USD-priced) ---
    Instrument("XAUUSD", "Gold",      "GC=F",     "XAUUSD", "metal"),
    Instrument("XAGUSD", "Silver",    "SI=F",     "XAGUSD", "metal"),
    Instrument("XPTUSD", "Platinum",  "PL=F",     "XPTUSD", "metal"),
    Instrument("XPDUSD", "Palladium", "PA=F",     "XPDUSD", "metal"),
    # --- energy ---
    Instrument("WTI",    "Oil (WTI)",   "CL=F",   "XTIUSD", "energy"),
    Instrument("BRENT",  "Oil (Brent)", "BZ=F",   "XBRUSD", "energy"),
    Instrument("NATGAS", "Natural Gas", "NG=F",   "XNGUSD", "energy"),
    # --- FX majors ---
    Instrument("EURUSD", "EUR/USD",  "EURUSD=X",  "EURUSD", "fx"),
    Instrument("GBPUSD", "GBP/USD",  "GBPUSD=X",  "GBPUSD", "fx"),
    Instrument("USDJPY", "USD/JPY",  "USDJPY=X",  "USDJPY", "fx"),
    Instrument("USDCHF", "USD/CHF",  "USDCHF=X",  "USDCHF", "fx"),
    Instrument("AUDUSD", "AUD/USD",  "AUDUSD=X",  "AUDUSD", "fx"),
    Instrument("USDCAD", "USD/CAD",  "USDCAD=X",  "USDCAD", "fx"),
    Instrument("NZDUSD", "NZD/USD",  "NZDUSD=X",  "NZDUSD", "fx"),
    # --- FX popular crosses ---
    Instrument("EURJPY", "EUR/JPY",  "EURJPY=X",  "EURJPY", "fx"),
    Instrument("GBPJPY", "GBP/JPY",  "GBPJPY=X",  "GBPJPY", "fx"),
    Instrument("AUDJPY", "AUD/JPY",  "AUDJPY=X",  "AUDJPY", "fx"),
    Instrument("EURGBP", "EUR/GBP",  "EURGBP=X",  "EURGBP", "fx"),
    Instrument("EURAUD", "EUR/AUD",  "EURAUD=X",  "EURAUD", "fx"),
    # --- equity indices ---
    Instrument("SPX",    "S&P 500",    "^GSPC",   "US500",  "index"),
    Instrument("NDX",    "Nasdaq 100", "^NDX",    "USTEC",  "index"),
    Instrument("DJI",    "Dow 30",     "^DJI",    "US30",   "index"),
    Instrument("DE40",   "DAX 40",     "^GDAXI",  "DE40",   "index"),
    Instrument("UK100",  "FTSE 100",   "^FTSE",   "UK100",  "index"),
    Instrument("JP225",  "Nikkei 225", "^N225",   "JP225",  "index"),
    Instrument("HK50",   "Hang Seng",  "^HSI",    "HK50",   "index"),
    Instrument("AUS200", "ASX 200",    "^AXJO",   "AUS200", "index"),
    # --- crypto ---
    Instrument("BTCUSD", "Bitcoin",  "BTC-USD",   "BTCUSD", "crypto"),
    Instrument("ETHUSD", "Ethereum", "ETH-USD",   "ETHUSD", "crypto"),
    Instrument("SOLUSD", "Solana",   "SOL-USD",   "SOLUSD", "crypto"),
    Instrument("XRPUSD", "XRP",      "XRP-USD",   "XRPUSD", "crypto"),
]

BY_KEY = {i.key: i for i in UNIVERSE}


# --- IBKR futures universe ---------------------------------------------------
# One Instrument per full-size futures MARKET (micros are execution vehicles
# picked at sizing time by contracts.choose_contract, NOT separate signals).
# `key` matches contracts.SPECS so the spec/roll/sizing layer joins by key, and
# `asset_class` matches the spec so WEEKLY_TREND_CLASSES / DECORRELATE keep
# working. `yf` is the continuous-future fallback ticker; `mt5` is unused here.
_FUT_YF = {
    "ES": "ES=F", "NQ": "NQ=F", "YM": "YM=F", "RTY": "RTY=F",
    "GC": "GC=F", "SI": "SI=F", "HG": "HG=F", "CL": "CL=F", "NG": "NG=F",
    "ZN": "ZN=F", "ZB": "ZB=F", "ZF": "ZF=F", "ZC": "ZC=F", "ZW": "ZW=F",
    "ZS": "ZS=F", "KC": "KC=F", "SB": "SB=F", "CT": "CT=F",
    "6E": "6E=F", "6J": "6J=F", "6A": "6A=F",
}
_FUT_NAME = {
    "ES": "E-mini S&P 500", "NQ": "E-mini Nasdaq 100", "YM": "E-mini Dow",
    "RTY": "E-mini Russell 2000", "GC": "Gold", "SI": "Silver", "HG": "Copper",
    "CL": "Crude Oil (WTI)", "NG": "Natural Gas", "ZN": "10Y T-Note",
    "ZB": "30Y T-Bond", "ZF": "5Y T-Note", "ZC": "Corn", "ZW": "Wheat",
    "ZS": "Soybeans", "KC": "Coffee", "SB": "Sugar", "CT": "Cotton",
    "6E": "Euro FX", "6J": "Japanese Yen", "6A": "Australian Dollar",
}


def _build_futures_universe() -> list[Instrument]:
    from dashboard.data.contracts import SPECS  # local import: contracts has no dep on us
    out = []
    for spec in SPECS.values():
        if spec.micro_of is not None:        # skip micros -- not separate signals
            continue
        out.append(Instrument(spec.key, _FUT_NAME.get(spec.key, spec.key),
                              _FUT_YF.get(spec.key, ""), "", spec.asset_class))
    return out


FUTURES_UNIVERSE: list[Instrument] = _build_futures_universe()
FUT_BY_KEY = {i.key: i for i in FUTURES_UNIVERSE}


def _ib_broker() -> bool:
    import os
    return os.environ.get("BROKER", "mt5").lower() == "ib"


def active_universe() -> list[Instrument]:
    """The universe the LIVE system trades, per the BROKER env var: the IBKR
    futures markets under BROKER=ib, else the MT5/yfinance universe. Research
    scripts keep importing UNIVERSE directly (MT5/yfinance backtests)."""
    return FUTURES_UNIVERSE if _ib_broker() else UNIVERSE


def active_by_key(key: str) -> Instrument | None:
    """Look up an instrument by key in the ACTIVE universe, with a fallback to
    the other (so a journal row written under one broker still resolves)."""
    return (FUT_BY_KEY.get(key) or BY_KEY.get(key)) if _ib_broker() \
        else (BY_KEY.get(key) or FUT_BY_KEY.get(key))
