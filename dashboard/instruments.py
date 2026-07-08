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
    # batch-3/4 keepers (screened 2026-07-08, isolation-tested vs the 17/18-base):
    # CWB +1.0pp OOS CAGR (flat DD), VNQI +0.5pp OOS CAGR (flat DD) -- see HANDOFF.
    Instrument("CWB",  "Convertible Bonds", "CWB",  "", "convertible"),
    Instrument("VNQI", "Intl REITs",        "VNQI", "", "intl_reit"),
    # batch-5 keeper (screened 2026-07-08, isolation-tested vs the 19-base): AMLP +0.9pp
    # OOS CAGR for -0.4pp extra DD (ratio flat/better). PALL/PPLT rejected DESPITE positive
    # raw per-market expR -- portfolio isolation showed -1.2pp DD cost for only +0.3pp CAGR
    # (clusters with existing GLD/SLV/CPER metal risk). See HANDOFF.
    Instrument("AMLP", "MLP Energy Infra",  "AMLP", "", "mlp"),
    # batch-6 keeper (screened 2026-07-08, isolation-tested vs the 20-base): HYD +0.6pp OOS
    # CAGR for ZERO extra DD (best ratio improvement of any candidate this session). BIZD/COPX
    # rejected -- both showed the same "decent raw expR, DD cost outweighs it" pattern as PALL.
    Instrument("HYD",  "Muni High-Yield",   "HYD",  "", "muni_hy"),
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

# Batch 3 to SCREEN (--etf-screen3, 2026-07-08). Batch-2's lesson: narrower slices of an
# asset class already held (sectors, regional-equity subsets, extra commodities/credit)
# just correlate with what's already in the book. This batch targets asset classes with
# NO representation at all in the traded universe, rather than subsets of existing ones.
# CWB PROMOTED to ETF_CANDIDATES 2026-07-08 (isolation-confirmed, +1.0pp OOS CAGR).
# BKLN/FM: promising expR but n=11/14 -- too few trades to trust, re-screen later with
# more history. EMLC: negative edge, clean reject.
ETF_SCREEN_BATCH_3: list[Instrument] = [
    Instrument("BKLN", "Senior Loans",       "BKLN", "", "bank_loan"),
    Instrument("EMLC", "EM Local-Ccy Debt",  "EMLC", "", "em_local_debt"),
    Instrument("IGF",  "Global Infra Eq",    "IGF",  "", "infra"),
    Instrument("FM",   "Frontier Mkts Eq",   "FM",   "", "frontier_eq"),
]
ETF_SCREEN_BATCH_3_BY_KEY = {i.key: i for i in ETF_SCREEN_BATCH_3}

# Batch 4 to SCREEN (--etf-screen4, 2026-07-08). Batch-3's CWB confirmed the pattern that
# works: geographic/structural diversification of an asset class ALREADY held successfully
# (like EFA/EEM alongside domestic SPY/QQQ) beats a narrower slice of one. Applies that same
# logic to REIT (domestic VNQ -> ex-US), rate (domestic IEF/TLT/SHY -> ex-US), and credit
# (domestic HYG -> international IG), plus one genuinely new real-asset class (timber).
# VNQI PROMOTED to ETF_CANDIDATES 2026-07-08 (isolation-confirmed, +0.5pp OOS CAGR). The
# geography-diversification pattern did NOT generalize to rate/credit/timber: BWX/PICB/WOOD
# all showed negative edge -- clean rejects, not deferred (plenty of history, no small-n excuse).
ETF_SCREEN_BATCH_4: list[Instrument] = [
    Instrument("BWX",  "Intl Govt Bonds",    "BWX",  "", "intl_rate"),
    Instrument("PICB", "Intl Corp Bonds",    "PICB", "", "intl_credit"),
    Instrument("WOOD", "Timber & Forestry",  "WOOD", "", "timber"),
]
ETF_SCREEN_BATCH_4_BY_KEY = {i.key: i for i in ETF_SCREEN_BATCH_4}

# Batch 5 to SCREEN (--etf-screen5, 2026-07-08). Neither "narrower slice of a held class"
# (batch-2) nor "international version of a held class" (batch-4, mostly) worked. This batch
# targets precious/industrial metals with DIFFERENT demand drivers than GLD/SLV/CPER (platinum/
# palladium: auto-catalyst demand; uranium: nuclear-fuel cycle, not monetary/industrial-base-
# metal), plus a real-asset equity structure not yet tried (MLP midstream energy -- distinct
# from both broad commodity DBC and the already-rejected energy sector XLE/USO).
# AMLP PROMOTED to ETF_CANDIDATES 2026-07-08 (isolation-confirmed, +0.9pp OOS CAGR). PPLT/
# PALL REJECTED despite positive raw per-market expR -- portfolio isolation showed they
# cluster drawdown risk with existing GLD/SLV/CPER (-1.2pp DD for only +0.3pp CAGR). URA:
# weak raw edge (+0.110, weaker than the already-rejected IGF), not worth an isolation test.
ETF_SCREEN_BATCH_5: list[Instrument] = [
    Instrument("PPLT", "Physical Platinum",  "PPLT", "", "metal2"),
    Instrument("PALL", "Physical Palladium", "PALL", "", "metal2"),
    Instrument("URA",  "Uranium Miners",     "URA",  "", "uranium"),
]
ETF_SCREEN_BATCH_5_BY_KEY = {i.key: i for i in ETF_SCREEN_BATCH_5}

# Batch 6 to SCREEN (--etf-screen6, 2026-07-08). Two genuinely new structures: municipal
# HIGH-YIELD (different credit tier + tax-exempt investor base than both HYG corporate-HY and
# the already-rejected MUB investment-grade muni), and a BDC income fund (leveraged private
# credit, a structure not tested at all yet). Plus one confirmatory test: does the "mining
# EQUITY carries broad market beta, diluting the pure-commodity diversification benefit"
# lesson from GDX's rejection (gold miners, -0.27) also hold for copper miners, given CPER
# (physical copper) itself succeeded?
# HYD PROMOTED to ETF_CANDIDATES 2026-07-08 (isolation-confirmed, +0.6pp OOS CAGR, ZERO extra
# DD -- the best ratio improvement of any candidate this session). BIZD/COPX REJECTED despite
# positive raw expR -- same "DD cost outweighs the CAGR gain" pattern as PALL/PPLT; COPX
# confirms the mining-equity-beta drag applies to copper too, just less severely than gold.
ETF_SCREEN_BATCH_6: list[Instrument] = [
    Instrument("BIZD", "BDC Income",        "BIZD", "", "bdc"),
    Instrument("COPX", "Copper Miners",     "COPX", "", "miner2"),
]
ETF_SCREEN_BATCH_6_BY_KEY = {i.key: i for i in ETF_SCREEN_BATCH_6}

# Batch 7 to SCREEN (--etf-screen7, 2026-07-08). Two genuinely new STRATEGY structures (not
# just new asset classes): merger arbitrage (MNA -- event-driven risk, not macro-trend-driven,
# since 2010) and covered-call income (QYLD -- options-overlay structure, capped upside/income
# focus, since 2013). Plus one more confirmatory real-asset-equity test (PHO water resources),
# extending the COPX result (mining-equity-beta drag) to see if it generalizes to another
# thematic real-asset equity, given infra/timber (both thematic real-asset equity) already
# failed. Deliberately EXCLUDED: managed-futures ETFs (e.g. DBMF) -- only ~6y history, same
# problem as crypto (too short for this project's 33y DSR/OOS discipline).
# ALL THREE REJECTED 2026-07-08: MNA flat edge (-0.009, n28 -- market-neutral strategies rarely
# throw real weekly trend signals). QYLD isolation-rejected despite +0.418 raw expR -- capped
# upside from the call overlay is fundamentally at odds with this strategy's "let winners run"
# edge source (ratio 2.02->1.88, same DD-outweighs-gain pattern as PALL/BIZD/COPX). PHO weak
# edge (+0.157), consistent with infra/timber -- thematic real-asset equity keeps failing.
ETF_SCREEN_BATCH_7: list[Instrument] = [
    Instrument("MNA",  "Merger Arbitrage",  "MNA",  "", "merger_arb"),
    Instrument("QYLD", "Covered-Call Income","QYLD", "", "covered_call"),
    Instrument("PHO",  "Water Resources",   "PHO",  "", "thematic_eq"),
]
ETF_SCREEN_BATCH_7_BY_KEY = {i.key: i for i in ETF_SCREEN_BATCH_7}

# Batch 8 to SCREEN (--etf-screen8, 2026-07-08). Mortgage REITs (REM -- rate/credit-spread
# sensitive leveraged bond-like structure, genuinely different risk driver than the EQUITY
# REITs already held (VNQ/VNQI, property-value/rental-income sensitive)); natural gas (UNG --
# weather/storage-driven demand, distinct from oil (USO, rejected) and broad commodity (DBC,
# held) -- CAVEAT: UNG is notorious for contango/roll decay dragging down long-term returns
# regardless of spot price, expect this one to likely fail); momentum factor equity (MTUM --
# the one factor-tilt idea worth actually testing rather than assuming redundant with SPY/QQQ,
# since this whole strategy IS trend-following and a momentum-tilted basket could plausibly
# behave differently, unlike straight sector/regional subsets which just repeatedly failed).
# REJECTED 2026-07-08: REM flat/negative edge (-0.021, n47). UNG only 5 signals in 33y --
# too thin to conclude (matches BKLN/FM). MTUM was a genuine borderline case (isolation:
# ratio 2.02->2.00, ~1% relative decline -- far milder than the clear rejects PALL/BIZD/COPX/
# QYLD showed) -- USER CALL: left out, keeping the flat-or-better bar strict/mechanical
# rather than making exceptions for close calls. Re-visit if a future batch needs a tiebreaker.
ETF_SCREEN_BATCH_8: list[Instrument] = [
    Instrument("REM",  "Mortgage REITs",    "REM",  "", "mortgage_reit"),
    Instrument("UNG",  "Natural Gas",       "UNG",  "", "energy2"),
    Instrument("MTUM", "Momentum Factor Eq","MTUM", "", "factor_eq"),
]
ETF_SCREEN_BATCH_8_BY_KEY = {i.key: i for i in ETF_SCREEN_BATCH_8}

# Batch 9 to SCREEN (--etf-screen9, 2026-07-08). Two candidates, honestly scraping toward the
# bottom of well-motivated ideas after batches 7-8 both came back empty: lithium/battery metals
# (LIT -- genuinely different demand driver (EV supply chain) than the metals already tested:
# precious GLD/SLV/PPLT/PALL, industrial CPER, nuclear-fuel URA) and low-volatility factor
# equity (USMV -- the scientific complement to MTUM's borderline result; if momentum showed
# marginal promise, testing its opposite is informative either way, though "low-vol" stocks
# trending less dramatically is a real reason to expect a WEAK result here, similar to REM/IGF).
# BOTH REJECTED 2026-07-08 (isolation): LIT ratio 2.02->1.90 (~6% decline), USMV 2.02->1.86
# (~8% decline) -- both more severe than MTUM's 1% borderline, so no judgment call needed,
# clean rejects under the established flat-or-better rule. USMV's weaker result vs MTUM matches
# the prediction (low-vol trends less). THIRD batch in a row (7/8/9) with zero adoptions --
# strong signal the well-motivated-idea pool is genuinely exhausted, not just unlucky timing.
ETF_SCREEN_BATCH_9: list[Instrument] = [
    Instrument("LIT",  "Lithium & Battery",  "LIT",  "", "metal3"),
    Instrument("USMV", "Low-Vol Factor Eq",  "USMV", "", "factor_eq2"),
]
ETF_SCREEN_BATCH_9_BY_KEY = {i.key: i for i in ETF_SCREEN_BATCH_9}

# The validated ETF trading universe = core {metal,index,rate} + screened diversifiers.
# 22 defined here, but EMB (em_bond) is excluded from LIVE trading via WEEKLY_TREND_CLASSES
# (paper.py) -- 21 ETFs actually trade. Latest isolation-tested result (2026-07-08, adding
# CWB+VNQI+AMLP+HYD to the prior 17): OOS CAGR +13.3% / maxDD -6.6% / expR +0.401 (33.4y, 0.5%
# risk). See HANDOFF.md for the full batch-3/4/5/6 screens + per-candidate isolation tests.
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
            or ETF_SCREEN_BATCH_BY_KEY.get(key) or ETF_SCREEN_BATCH_3_BY_KEY.get(key)
            or ETF_SCREEN_BATCH_4_BY_KEY.get(key) or ETF_SCREEN_BATCH_5_BY_KEY.get(key)
            or ETF_SCREEN_BATCH_6_BY_KEY.get(key) or ETF_SCREEN_BATCH_7_BY_KEY.get(key)
            or ETF_SCREEN_BATCH_8_BY_KEY.get(key) or ETF_SCREEN_BATCH_9_BY_KEY.get(key)
            or BY_KEY.get(key)) if _ib_broker() \
        else (BY_KEY.get(key) or FUT_BY_KEY.get(key) or ETF_BY_KEY.get(key)
              or ETF_CANDIDATE_BY_KEY.get(key) or ETF_SCREEN_BATCH_BY_KEY.get(key)
              or ETF_SCREEN_BATCH_3_BY_KEY.get(key) or ETF_SCREEN_BATCH_4_BY_KEY.get(key)
              or ETF_SCREEN_BATCH_5_BY_KEY.get(key) or ETF_SCREEN_BATCH_6_BY_KEY.get(key)
              or ETF_SCREEN_BATCH_7_BY_KEY.get(key) or ETF_SCREEN_BATCH_8_BY_KEY.get(key)
              or ETF_SCREEN_BATCH_9_BY_KEY.get(key))
