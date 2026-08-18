"""Signal-equivalence test for candidate UCITS substitutes -- ADDED 2026-08-19,
user-requested re-check of the full 21-ETF universe for lower-fee, non-US-domiciled
(estate-tax-avoiding) alternatives.

Scope: only the pairs where the candidate tracks a DIFFERENT underlying index/structure
from the current US ETF (confirmed via research, not assumed) -- same-index twins
(SPY/CSPX, QQQ/EQQQ, GLD/SGLN, SLV/SSLN, IEF/IDTM, TLT/IDTL, SHY/IBTS, DIA/CIND,
TIP/ITPS, ASHR/RQFI) don't need this: a full 22-ETF backtest sweep on those would burn
real time to answer a question the fund prospectuses already answer for free. This
script is for the ones that are NOT simple twins, where "would this actually change
what the strategy does" is a real, unanswered question.

Uses the REAL entry-signal machinery (analyst.features.compute_facts +
dashboard.core.scoring.score_from_facts), not a simplified proxy indicator (a hand-
rolled "close > 200dma" check would test a DIFFERENT strategy than the one actually
traded) -- walks each pair's overlapping weekly history week-by-week, comparing the
resulting Score.direction/.signal at every step.

Run: uv run python -u -m dashboard.research.ucits_equivalence_test
"""
from __future__ import annotations

import yfinance as yf
import pandas as pd

from analyst.features import compute_facts
from dashboard.core.scoring import score_from_facts

PAIRS = {
    "EEM":  ("EEM",  "EIMI.L", "MSCI EM -> MSCI EM IMI (adds small-caps)"),
    "VNQ":  ("VNQ",  "IUSP.L", "MSCI US REIT -> FTSE EPRA Nareit US Dividend+ (different index construction)"),
    "DBC":  ("DBC",  "PCOM.L", "DBIQ 14-commodity -> Bloomberg Commodity Index (different basket/weights)"),
    "HYG":  ("HYG",  "IHYA.L", "Markit iBoxx USD HY -> different HY benchmark, TBD"),
    "PFF":  ("PFF",  "PRFD.L", "ICE BofA Core Plus Preferred -> BofA Diversified Core Plus Preferred (related, not identical)"),
    "CPER": ("CPER", "COPA.L", "USCF copper-futures index fund -> WisdomTree swap-based copper ETC (different structure)"),
}


def _weekly_close(ticker: str) -> pd.Series:
    # FOUND running this script: yfinance's interval="1wk" anchors each ticker's weekly
    # bars to whatever day-of-week ITS OWN raw daily history happens to start on (VNQ's
    # weekly bars land on Mondays, IUSP.L's land on Thursdays -- different exchanges,
    # different first trading days) -- an exact-date .intersection() between two "1wk"
    # downloads then silently returns EMPTY for some pairs (VNQ/CPER: 0 overlap despite
    # 920/972 real bars each) and could equally be silently DROPPING weeks it should have
    # matched for pairs that "worked" (EEM/HYG/DBC/PFF above), not just the two that
    # failed outright. Fixed properly: pull DAILY closes and resample both sides to the
    # SAME fixed weekly anchor (Friday) via pandas, not trust yfinance's own "1wk" anchor.
    df = yf.download(ticker, period="max", interval="1d", progress=False, auto_adjust=True)
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)
    s = df["Close"].dropna()
    if s.index.tz is not None:
        s.index = s.index.tz_localize(None)
    return s.resample("W-FRI").last().dropna()


def compare_pair(us_key: str, us_ticker: str, ucits_ticker: str, note: str) -> None:
    us = _weekly_close(us_ticker)
    uc = _weekly_close(ucits_ticker)
    common = us.index.intersection(uc.index)
    if len(common) < 160:
        print(f"{us_key}: only {len(common)} overlapping weekly bars -- too short "
              f"for the 'long' trend tier (150-bar MA) to stabilize, skipping")
        return

    common = sorted(common)
    agree_dir = agree_sig = total = 0
    first_valid = None
    for i, ts in enumerate(common):
        us_hist = us.loc[:ts]
        uc_hist = uc.loc[:ts]
        if len(us_hist) < 150 or len(uc_hist) < 150:
            continue      # need the full "long" tier lookback on BOTH series
        if first_valid is None:
            first_valid = ts
        f_us, _ = compute_facts(us_hist, us_key)
        f_uc, _ = compute_facts(uc_hist, us_key)
        s_us = score_from_facts(us_key, f_us, "")
        s_uc = score_from_facts(us_key, f_uc, "")
        total += 1
        if s_us.direction == s_uc.direction:
            agree_dir += 1
        if s_us.signal == s_uc.signal:
            agree_sig += 1

    if total == 0:
        print(f"{us_key}: never reached 150 bars on both series simultaneously, skipping")
        return
    ret_corr = us.pct_change().reindex(common).corr(uc.pct_change().reindex(common))
    print(f"{us_key} ({note})")
    print(f"  weeks compared: {total} (from {first_valid.date()} to {common[-1].date()})")
    print(f"  direction agreement: {agree_dir}/{total} = {agree_dir/total:.1%}")
    print(f"  signal (BUY/SELL/WATCH) agreement: {agree_sig}/{total} = {agree_sig/total:.1%}")
    print(f"  weekly return correlation: {ret_corr:.4f}")
    print()


if __name__ == "__main__":
    for key, (us_t, uc_t, note) in PAIRS.items():
        try:
            compare_pair(key, us_t, uc_t, note)
        except Exception as e:                     # noqa: BLE001
            print(f"{key}: FAILED -- {e}\n")
