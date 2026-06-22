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


# --- ETF universe (for SMALL accounts) --------------------------------------
# Share-priced equivalents of the {metal,index,rate} futures: shares divide
# finely, so 0.5% risk is expressible on any account size (futures can't — even
# micros risk > a small account's budget). Same underlyings/classes; the weekly
# TSMOM strategy ports unchanged. Tagged with the SAME asset_class so
# WEEKLY_TREND_CLASSES / LONG_ONLY apply identically.
ETF_UNIVERSE: list[Instrument] = [
    Instrument("GLD",  "SPDR Gold",          "GLD",  "", "metal"),
    Instrument("SLV",  "iShares Silver",     "SLV",  "", "metal"),
    Instrument("CPER", "US Copper",          "CPER", "", "metal"),
    Instrument("SPY",  "S&P 500 ETF",        "SPY",  "", "index"),
    Instrument("QQQ",  "Nasdaq 100 ETF",     "QQQ",  "", "index"),
    Instrument("DIA",  "Dow 30 ETF",         "DIA",  "", "index"),
    Instrument("IWM",  "Russell 2000 ETF",   "IWM",  "", "index"),
    Instrument("IEF",  "7-10y Treasury ETF", "IEF",  "", "rate"),
    Instrument("TLT",  "20+y Treasury ETF",  "TLT",  "", "rate"),
    Instrument("SHY",  "1-3y Treasury ETF",  "SHY",  "", "rate"),
]
ETF_BY_KEY = {i.key: i for i in ETF_UNIVERSE}

# Diversifiers that PASSED the screen (positive full-sample expR, genuinely different
# exposure). Screened 2026-06-22 via --etf-screen on 33.4y:
#   KEEP: HYG credit +0.52, TIP inflation +0.49, EFA/EEM intl_eq +0.30, DBC commodity
#         +0.25, VNQ reit +0.12.  REJECT: USO energy -0.51, UUP fx -0.31, GDX miner -0.27.
# Adding the keepers lifted full CAGR 2.6%->3.6%, OOS 6.9%->8.4% (real diversification,
# unlike the futures grains/softs/fx that failed). Their classes must be in
# WEEKLY_TREND_CLASSES to trade live.
ETF_CANDIDATES: list[Instrument] = [
    Instrument("HYG",  "High-Yield Bonds",  "HYG",  "", "credit"),
    Instrument("TIP",  "TIPS",              "TIP",  "", "inflation"),
    Instrument("EFA",  "Developed Intl Eq", "EFA",  "", "intl_eq"),
    Instrument("EEM",  "Emerging Mkt Eq",   "EEM",  "", "intl_eq"),
    Instrument("DBC",  "Broad Commodities", "DBC",  "", "commodity"),
    Instrument("VNQ",  "US REITs",          "VNQ",  "", "reit"),
    # batch-2 keepers (screened 2026-06-22): distinct, positive, not equity-cluster.
    Instrument("EMB",  "EM Bonds",          "EMB",  "", "em_bond"),
    Instrument("PFF",  "Preferred Stock",   "PFF",  "", "preferred"),
]
ETF_CANDIDATE_BY_KEY = {i.key: i for i in ETF_CANDIDATES}

# Batch 2 to SCREEN (--etf-screen2). NOT traded unless they clear OOS + add real
# diversification (most are redundant subsets/correlates of the held set).
ETF_SCREEN_BATCH: list[Instrument] = [
    Instrument("XLK", "Tech Sector",       "XLK", "", "us_sector"),
    Instrument("XLF", "Financials Sector", "XLF", "", "us_sector"),
    Instrument("XLE", "Energy Sector",     "XLE", "", "us_sector"),
    Instrument("VGK", "Europe Eq",         "VGK", "", "intl_eq2"),
    Instrument("EWJ", "Japan Eq",          "EWJ", "", "intl_eq2"),
    Instrument("INDA","India Eq",          "INDA","", "intl_eq2"),
    Instrument("GSG", "GSCI Commodity",    "GSG", "", "commodity2"),
    Instrument("DBA", "Agriculture",       "DBA", "", "commodity2"),
    Instrument("LQD", "IG Corp Bonds",     "LQD", "", "ig_credit"),
    Instrument("MUB", "Municipal Bonds",   "MUB", "", "muni"),
]
ETF_SCREEN_BATCH_BY_KEY = {i.key: i for i in ETF_SCREEN_BATCH}

# The validated ETF trading universe = core {metal,index,rate} + screened diversifiers
# (credit/inflation/intl_eq/commodity/reit). 16 ETFs. Best risk-adjusted result found:
# full +4.2% CAGR / -10.7% DD (vs core-10 2.6%/-13.4%). Use for small accounts.
ETF_TRADED: list[Instrument] = ETF_UNIVERSE + ETF_CANDIDATES
ETF_TRADED_BY_KEY = {i.key: i for i in ETF_TRADED}


def _ib_broker() -> bool:
    import os
    return os.environ.get("BROKER", "mt5").lower() == "ib"


def _etf_mode() -> bool:
    """Trade ETFs (shares) instead of futures -- for accounts too small to size even
    micro futures. Set UNIVERSE=etf in the env."""
    import os
    return os.environ.get("UNIVERSE", "futures").lower() == "etf"


def active_universe() -> list[Instrument]:
    """The LIVE traded universe, per env. BROKER=ib + UNIVERSE=etf -> the 16 validated
    ETFs (share-priced, for small accounts); else BROKER=ib -> futures {metal,index,rate};
    MT5 -> spot. Filtered to WEEKLY_TREND_CLASSES (empty => no filter)."""
    if not _ib_broker():
        return UNIVERSE
    from dashboard.core import paper          # late import: avoids circular load
    cls = paper.WEEKLY_TREND_CLASSES
    base = ETF_TRADED if _etf_mode() else FUTURES_UNIVERSE
    return [i for i in base if i.asset_class in cls] if cls else base


def active_by_key(key: str) -> Instrument | None:
    """Look up an instrument by key in the ACTIVE universe, with a fallback to
    the other (so a journal row written under one broker still resolves)."""
    return (FUT_BY_KEY.get(key) or ETF_BY_KEY.get(key) or ETF_CANDIDATE_BY_KEY.get(key)
            or ETF_SCREEN_BATCH_BY_KEY.get(key) or BY_KEY.get(key)) if _ib_broker() \
        else (BY_KEY.get(key) or FUT_BY_KEY.get(key) or ETF_BY_KEY.get(key)
              or ETF_CANDIDATE_BY_KEY.get(key) or ETF_SCREEN_BATCH_BY_KEY.get(key))
