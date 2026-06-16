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
