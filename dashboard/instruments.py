"""Instrument universe: Gold, Oil, and FX majors.

Each instrument carries the symbol for each data provider. MT5 oil symbols
vary by broker (USOIL / XTIUSD / WTI / CL) -- adjust mt5 field to match yours.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Instrument:
    key: str          # internal id / display label
    name: str         # human name
    yf: str           # yfinance ticker
    mt5: str          # MT5 symbol (broker-dependent)
    asset_class: str  # "metal" | "energy" | "fx"


UNIVERSE: list[Instrument] = [
    Instrument("XAUUSD", "Gold",     "GC=F",      "XAUUSD", "metal"),
    Instrument("XAGUSD", "Silver",   "SI=F",      "XAGUSD", "metal"),
    Instrument("WTI",    "Oil (WTI)", "CL=F",     "XTIUSD", "energy"),
    Instrument("EURUSD", "EUR/USD",  "EURUSD=X",  "EURUSD", "fx"),
    Instrument("GBPUSD", "GBP/USD",  "GBPUSD=X",  "GBPUSD", "fx"),
    Instrument("USDJPY", "USD/JPY",  "USDJPY=X",  "USDJPY", "fx"),
    Instrument("AUDUSD", "AUD/USD",  "AUDUSD=X",  "AUDUSD", "fx"),
    Instrument("USDCAD", "USD/CAD",  "USDCAD=X",  "USDCAD", "fx"),
    Instrument("USDCHF", "USD/CHF",  "USDCHF=X",  "USDCHF", "fx"),
    Instrument("NZDUSD", "NZD/USD",  "NZDUSD=X",  "NZDUSD", "fx"),
    Instrument("EURJPY", "EUR/JPY",  "EURJPY=X",  "EURJPY", "fx"),
    Instrument("GBPJPY", "GBP/JPY",  "GBPJPY=X",  "GBPJPY", "fx"),
    Instrument("SPX",    "S&P 500",  "^GSPC",     "US500",  "index"),
    Instrument("NDX",    "Nasdaq 100", "^NDX",    "USTEC",  "index"),
]

BY_KEY = {i.key: i for i in UNIVERSE}
