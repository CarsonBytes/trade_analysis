"""Panic-MR dip-buy SLEEVE — the one validated satellite strategy (see HANDOFF.md "FINAL DIP
SLEEVE SPEC"). Signal math replicated EXACTLY from the validated backtest
(dashboard/research/dipbuy_refine2.py) so the live version is provably the same strategy that
was backtested, not a similar-looking variant.

Universe: SPY, QQQ, XLK, DIA, IWM, HYG, EFA, EEM, VNQ, PFF, ASHR (11 tickers, EXTENDED
2026-07-09 from the original SPY/QQQ/XLK-only scope). Re-tested the exact signal below against
all 22 currently-held core ETFs: a naive "all 22" extension raised CAGR but blew out tail risk at
higher weight (correlated dip-buy entries firing together during the SAME systemic panic that
triggers the VIX-spike condition -- the opposite of the diversification benefit breadth gives the
core trend book). This 11-ticker set is a SELECTIVE subset -- kept only tickers clearing meanR
>=0.7% at n>=20 in that re-test (dropped CPER/DBC negative edge; GLD/TLT/TIP/CWB/VNQI/AMLP/HYD/
SHY/IEF weak or too-thin). Blended into the core book at 10% weight: CAGR +7.91%->+11.58%, maxDD
-11.2%->-10.4%, Sharpe 1.05->1.32 -- better on every metric than both the original 3-ticker scope
AND the naive all-22 version. NOTE: IWM's inclusion updates an earlier note ("no IWM -- weakest
edge") from an earlier research round -- this re-test, run against the CURRENT exact production
signal spec, shows genuine edge (n=83, meanR +0.80%, win 72%) at a comparable tier to DIA. See
HANDOFF for the full comparison table.
Entry (ALL, daily close): close < 20dMA*0.975  AND  VIX/VIX[-5] - 1 > 0.15  AND  RSI14 < 35
                           AND ADX14 > 20.
Size: 0.5% risk base, 1.0% at VIX>30 (hard cap 1%) -- risk_pct stored in entry_facts so
      ib_exec._place_sleeve_bracket sizes correctly without re-deriving it.
Exit: FOUR conditions, first true wins:
   - +3% target        -> a REAL broker LMT order (works even if this app is offline)
   - -5% stop           -> a REAL broker STP order (ditto)
   - close >= 5-day MA  -> DYNAMIC, checked daily here; broker can't express this statically
   - 10 trading days    -> DYNAMIC, checked daily here
No staging/partial exits (tested and rejected -- see HANDOFF).

SLEEVE_ENABLED gates everything (default OFF): only the paper launch script sets it, so this
never silently activates on the live account. sleeve_active(equity) (paper.py) is the SECOND,
independent gate (account-size phase) -- BOTH must be true to trade.
"""
from __future__ import annotations

import os
import datetime as dt
import numpy as np
import pandas as pd

from dashboard.core.log import log
from dashboard.core import paper

SLEEVE_UNIVERSE = ["SPY", "QQQ", "XLK", "DIA", "IWM", "HYG", "EFA", "EEM", "VNQ", "PFF", "ASHR"]
SLEEVE_METHOD = "dipbuy-sleeve"
# Signal is DAILY-bar based; the app's cheap-refresh runs every ~1min by default, but
# re-fetching yfinance for 11 tickers that often is wasteful and pointless (the underlying
# daily bar hasn't changed). Throttle to once per CHECK_INTERVAL_MIN; in-memory only (resets
# on restart -- harmless, just means one extra check right after a restart).
CHECK_INTERVAL_MIN = 60
_last_check: dict[str, dt.datetime] = {}


def _throttled(name: str) -> bool:
    now = dt.datetime.now(dt.timezone.utc)
    last = _last_check.get(name)
    if last is not None and (now - last).total_seconds() < CHECK_INTERVAL_MIN * 60:
        return True
    _last_check[name] = now
    return False
VIX_HIGH = 30.0
RISK_BASE = 0.005
RISK_HIGH = 0.01
STOP_FRAC = 0.05
TARGET_FRAC = 0.03
TIME_CAP_DAYS = 10


def sleeve_enabled() -> bool:
    """Explicit opt-in, independent of account-size phase. Originally BOTH launch scripts
    set this only for paper (dashboard.ps1), deliberately NOT live -- but the user explicitly
    approved enabling it on live too (2026-07-09, confirmed via an explicit yes/no prompt), so
    run_dashboard_live.ps1 now sets SLEEVE_ENABLED=1 as well (comment corrected 2026-07-11,
    was stale). The real gate keeping the sleeve inert on the live account right now is
    paper.sleeve_active()'s equity-phase check (PHASE2_NAV_USD ~$64k) -- live is currently
    far below that, not blocked by this flag."""
    return os.environ.get("SLEEVE_ENABLED", "").lower() in ("1", "true", "yes")


# --- STAGED ROLLOUT (2026-07-09) --------------------------------------------------------
# 8 of the 11 SLEEVE_UNIVERSE tickers have zero live-observed trades. Rather than go from
# "3 backtested tickers" to "11 all at once" the instant an account crosses the equity gate,
# widen the ACTIVE set in stages, tied to elapsed time since the sleeve first activated (not
# since this code shipped -- an account already past the gate on day 1 still gets the same
# staged ramp, not an instant jump to all 11). SLEEVE_UNIVERSE itself is untouched (still the
# full 11 -- ib_exec's membership check and the research/backtest scripts need the complete
# set); this only narrows which tickers NEW entries are taken on.
SLEEVE_STAGE_2A = ["SPY", "QQQ", "XLK"]                       # immediate: the only 3 with
                                                                # any backtest history pre-dating
                                                                # 2026-07-09's extension
SLEEVE_STAGE_2B_ADD = ["DIA", "IWM"]                           # +3 months: most liquid,
                                                                # closest analogues to 2a
SLEEVE_STAGE_2C_ADD = ["HYG", "EFA", "EEM", "VNQ", "PFF", "ASHR"]   # +6 months: the rest
SLEEVE_STAGE_2B_MONTHS = 3.0
SLEEVE_STAGE_2C_MONTHS = 6.0


def _sleeve_first_active_ts() -> float | None:
    """Unix ts of the first cycle sleeve_active(equity) was True. Written once, never
    overwritten -- drives the staged rollout below, independent of when this code shipped."""
    from dashboard.core import store
    cached, _ = store.cache_get("sleeve_first_active_ts")
    return float(cached) if cached else None


def _record_first_active_if_needed() -> None:
    from dashboard.core import store
    import time as _time
    if _sleeve_first_active_ts() is None:
        store.cache_set("sleeve_first_active_ts", _time.time())
        log.info("sleeve: first activation recorded -- staged rollout clock starts now "
                 "(2a=%s now, 2b +%s at %.0fmo, 2c +%s at %.0fmo)",
                 SLEEVE_STAGE_2A, SLEEVE_STAGE_2B_ADD, SLEEVE_STAGE_2B_MONTHS,
                 SLEEVE_STAGE_2C_ADD, SLEEVE_STAGE_2C_MONTHS)


SLEEVE_BREAKER_MIN_N = 5           # need at least this many closed trades before judging a
                                    # ticker -- same "don't judge on noise" ethos as paper.stats()
SLEEVE_BREAKER_MIN_WIN = 0.40
SLEEVE_BREAKER_MIN_EXPR = 0.0


def _ticker_breaker_tripped(ticker: str) -> str | None:
    """Auto-remove a single ticker from new sleeve entries if ITS OWN live closed-trade
    record turns bad (win% < SLEEVE_BREAKER_MIN_WIN or expR < SLEEVE_BREAKER_MIN_EXPR), once
    there are enough trades to judge. Returns a reason string if tripped, else None. Does NOT
    touch other tickers or the core book -- a bad live result on one satellite ticker doesn't
    imply the others are bad too."""
    rs = [t["realized_r"] for t in paper.all_trades()
          if t["instrument"] == ticker and t["method"] == SLEEVE_METHOD
          and t["status"] != "OPEN"]
    if len(rs) < SLEEVE_BREAKER_MIN_N:
        return None
    s = paper.stats(rs)
    if s["win_rate"] < SLEEVE_BREAKER_MIN_WIN or s["expectancy_R"] < SLEEVE_BREAKER_MIN_EXPR:
        return (f"live win {s['win_rate']:.0%} / expR {s['expectancy_R']:+.3f} over "
                f"{s['n']} closed trades")
    return None


def active_sleeve_universe() -> list[str]:
    """The CURRENTLY tradeable subset of SLEEVE_UNIVERSE, per the staged rollout. Returns
    just SLEEVE_STAGE_2A if the sleeve has never activated yet (harmless default -- nothing
    calls this before checking sleeve_active() anyway)."""
    first_ts = _sleeve_first_active_ts()
    if first_ts is None:
        return list(SLEEVE_STAGE_2A)
    months = (dt.datetime.now(dt.timezone.utc).timestamp() - first_ts) / (30.44 * 86400)
    uni = list(SLEEVE_STAGE_2A)
    if months >= SLEEVE_STAGE_2B_MONTHS:
        uni += SLEEVE_STAGE_2B_ADD
    if months >= SLEEVE_STAGE_2C_MONTHS:
        uni += SLEEVE_STAGE_2C_ADD
    return uni


def _load_daily(ticker: str) -> dict | None:
    """Daily OHLC + VIX + indicators, computed IDENTICALLY to the validated backtest
    (dipbuy_refine2.load()). Returns None on a data-fetch failure (caller skips this cycle)."""
    import yfinance as yf
    try:
        p = yf.download([ticker, "^VIX"], period="1y", interval="1d",
                        progress=False, auto_adjust=True)
    except Exception as e:                              # noqa: BLE001
        log.warning("sleeve: yfinance fetch failed for %s: %s", ticker, e)
        return None
    if p is None or len(p) == 0 or ticker not in p["Close"].columns:
        return None
    s = p["Close"][ticker].dropna()
    v = p["Close"]["^VIX"].reindex(s.index).ffill()
    h = p["High"][ticker].reindex(s.index)
    lo = p["Low"][ticker].reindex(s.index)
    if len(s) < 200:
        return None

    def rsi(n):
        d = s.diff()
        g = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
        l = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
        return 100 - 100 / (1 + g / l.replace(0, np.nan))

    tr = pd.concat([(h - lo), (h - s.shift()).abs(), (lo - s.shift()).abs()], axis=1).max(axis=1)
    up = h.diff()
    dn = -lo.diff()
    pl = ((up > dn) & (up > 0)) * up
    mi = ((dn > up) & (dn > 0)) * dn
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    pdi = 100 * pl.ewm(alpha=1 / 14, adjust=False).mean() / atr
    mdi = 100 * mi.ewm(alpha=1 / 14, adjust=False).mean() / atr
    adx = (100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)).ewm(alpha=1 / 14, adjust=False).mean()
    return {
        "close": float(s.iloc[-1]), "vix": float(v.iloc[-1]),
        "vix_5ago": float(v.iloc[-6]) if len(v) > 5 else float("nan"),
        "ma5": float(s.rolling(5).mean().iloc[-1]),
        "ma20": float(s.rolling(20).mean().iloc[-1]),
        "rsi14": float(rsi(14).iloc[-1]),
        "adx14": float(adx.iloc[-1]),
        "asof": s.index[-1],
    }


def entry_signal(ticker: str) -> dict | None:
    """The exact backtest entry rule. Returns a candidate dict if ALL conditions hold, else
    None. Caller still must check _has_open()/cooldown before acting on this."""
    d = _load_daily(ticker)
    if d is None:
        return None
    if not all(np.isfinite(x) for x in (d["close"], d["ma20"], d["rsi14"], d["adx14"],
                                        d["vix"], d["vix_5ago"])) or d["vix_5ago"] <= 0:
        return None
    vix_up = d["vix"] / d["vix_5ago"] - 1.0
    ok = (d["close"] < d["ma20"] * 0.975) and (vix_up > 0.15) and \
         (d["rsi14"] < 35) and (d["adx14"] > 20)
    if not ok:
        return None
    entry = d["close"]
    risk_pct = RISK_HIGH if d["vix"] > VIX_HIGH else RISK_BASE
    return {
        "instrument": ticker, "entry": entry,
        "sl": round(entry * (1 - STOP_FRAC), 4), "tp": round(entry * (1 + TARGET_FRAC), 4),
        "risk_pct": risk_pct, "vix_at_entry": d["vix"], "asof": d["asof"],
        "rationale": (f"panic dip-buy: close {entry:.2f} < 20MA*0.975 "
                      f"({d['ma20']*0.975:.2f}), VIX {d['vix']:.1f} (+{vix_up:.0%}/5d), "
                      f"RSI14 {d['rsi14']:.0f}, ADX14 {d['adx14']:.0f}"),
    }


def should_exit_dynamic(trade: dict) -> tuple[bool, str] | tuple[bool, None]:
    """Checks ONLY the two conditions a static broker bracket can't express (5MA-touch,
    10-trading-day cap). The +3%/-5% legs are real broker orders, already enforced. Returns
    (True, reason) or (False, None)."""
    d = _load_daily(trade["instrument"])
    if d is None:
        return False, None
    if d["close"] >= d["ma5"]:
        return True, f"5-day MA touch (close {d['close']:.2f} >= MA5 {d['ma5']:.2f})"
    try:
        entry_dt = pd.Timestamp(trade["ts"])
        if entry_dt.tz is None:
            entry_dt = entry_dt.tz_localize("UTC")
        asof = d["asof"]
        if asof.tz is None:
            asof = asof.tz_localize("UTC")
        # trading days elapsed ~ business days between entry and now (daily-bar granularity,
        # matches the backtest's bar-count time cap closely enough for a daily-checked sleeve)
        days = len(pd.bdate_range(entry_dt.normalize(), asof.normalize())) - 1
        if days >= TIME_CAP_DAYS:
            return True, f"{TIME_CAP_DAYS}-trading-day time cap ({days}d elapsed)"
    except Exception as e:                              # noqa: BLE001
        log.warning("sleeve: time-cap check failed for #%s: %s", trade.get("id"), e)
    return False, None


def place_sleeve_signals(equity_usd: float | None) -> list[str]:
    """Entry side: for each universe ticker, if the sleeve is enabled+in-phase and the
    signal fires and we don't already hold it, log a new paper trade (method=dipbuy-sleeve).
    Mirrors the SAME Trade/_insert path the core funnel uses -- ib_exec.mirror_new() then
    places the real bracket on its next cycle, exactly like a core signal."""
    logs: list[str] = []
    if not sleeve_enabled():
        log.debug("sleeve: gate check -- SLEEVE_ENABLED not set, skipping this cycle")
        return logs
    if not paper.sleeve_active(equity_usd):
        log.debug("sleeve: gate check -- not active (equity_usd=%r, phase=%s), skipping "
                  "this cycle", equity_usd, paper.account_phase(equity_usd))
        return logs
    _record_first_active_if_needed()
    if _throttled("entries"):
        return logs
    now = dt.datetime.now(dt.timezone.utc)
    for ticker in active_sleeve_universe():
        if paper._has_open(ticker, SLEEVE_METHOD) or paper._recent_close(ticker):
            continue
        tripped = _ticker_breaker_tripped(ticker)
        if tripped:
            log.warning("sleeve: %s auto-removed from new entries -- %s", ticker, tripped)
            continue
        cand = entry_signal(ticker)
        if cand is None:
            continue
        import json
        entry_facts = json.dumps({
            "risk_pct": cand["risk_pct"], "vix_at_entry": cand["vix_at_entry"],
            "sleeve": True,
        })
        t = paper.Trade(
            ts=now.isoformat(timespec="seconds"), instrument=ticker, direction="long",
            method=SLEEVE_METHOD, entry=cand["entry"], sl=cand["sl"], tp=cand["tp"],
            rr=round(TARGET_FRAC / STOP_FRAC, 2), size_units=0.0,
            horizon_end=(now + dt.timedelta(days=TIME_CAP_DAYS * 1.5)).isoformat(timespec="seconds"),
            confidence=0.0, rationale=cand["rationale"][:300], entry_facts=entry_facts)
        paper._insert(t)
        msg = (f"{ticker} {SLEEVE_METHOD}: PLACED long entry {cand['entry']:.2f} "
              f"SL {cand['sl']:.2f} TP {cand['tp']:.2f} (risk {cand['risk_pct']:.1%}, "
              f"VIX {cand['vix_at_entry']:.1f})")
        logs.append(msg)
        log.info("sleeve: %s", msg)
    return logs


def close_expired_sleeves() -> list[str]:
    """Exit side (dynamic legs only): for each OPEN sleeve trade, check the 5MA-touch /
    time-cap conditions and, if triggered, ask the broker layer to flatten it. The static
    +3%/-5% legs are already-live broker orders and need no action here."""
    logs: list[str] = []
    if not sleeve_enabled():
        return logs
    if _throttled("exits"):
        return logs
    with paper._LOCK, paper._conn() as c:
        rows = c.execute("SELECT * FROM paper_trades WHERE status='OPEN' AND method=?",
                         (SLEEVE_METHOD,)).fetchall()
        cols = [d[0] for d in c.execute(
            "SELECT * FROM paper_trades WHERE 1=0").description]
    for r in rows:
        trade = dict(zip(cols, r))
        should, reason = should_exit_dynamic(trade)
        if not should:
            continue
        from dashboard.execution import ib_exec
        msg = ib_exec.manual_close_sleeve(trade, reason)
        if msg:
            logs.append(msg); log.info("sleeve: %s", msg)
    return logs
