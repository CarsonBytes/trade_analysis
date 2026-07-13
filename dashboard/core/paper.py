"""Paper-trade (forward-test) engine.

Generates SL/TP setups from analysis, resolves them against real subsequent
price action, and scores the result honestly: expectancy in R first, win rate
second. Shared resolution/stat helpers are reused by replay.py.

Honesty rules baked in:
  - costs (half-spread) charged on entry AND exit
  - when a single bar straddles both SL and TP, assume SL hit first (pessimistic)
  - every signal that fails a gate is recorded WITH the reason (no silent skips)
  - we report expectancy in R, not just win rate (win rate alone is a trap)
"""
from __future__ import annotations

from dashboard.core import net  # noqa: F401
from dataclasses import dataclass, asdict
import datetime as dt
import json
import sqlite3
import pathlib
import threading

import pandas as pd

from dashboard.core.log import log
from dashboard.core import store

# ---- config (tunable defaults) --------------------------------------------
# Config below was selected by dashboard/optimize.py (in-sample over 5y) and
# held up out-of-sample at +0.32 R/trade -- BUT deflated Sharpe was only 43%
# (not the >=95% bar). So this is the best-available, UNPROVEN config: a
# hypothesis to forward-test, not a validated edge. Profile = trend-following
# (wide target, tight stop, only the strongest signals -> fewer, higher quality).
SL_ATR_MULT = 1.5          # stop = 1.5 x ATR
RR_DEFAULT = 3.0           # take-profit reward:risk (needs only ~25% win rate)
RR_SWEEP = [2.0, 3.0, 4.0]  # ATR variants we compare in replay
MIN_RR = 1.5               # reject setups whose geometry is worse than this
HORIZON_DAYS = 5           # trade validity in BARS (weekly now -> ~5 weeks);
HORIZON_CAL = 35           # live horizon window in calendar days (5 weeks) -- matches
                           # HORIZON_DAYS=5 weekly bars. Reconciled 2026-06-21: was 49
                           # (7wk), which the horizon curve showed is OUTSIDE the robust
                           # 4-6wk plateau (CAGR/DD 0.76 vs ~1.0). Live now == backtest.
RISK_PER_TRADE = 0.01      # 1% risk/trade -- with ETF_POS_CAP=0.25 this "fills" the cap on
                           # the high-vol names; the cap (not risk%) is the real return/DD dial.
ACCOUNT = 10_000.0
CONF_THRESHOLD = 0.60      # (legacy) LLM self-reported confidence -- recorded
                           # for calibration but no longer gates entries
MIN_EDGE_R = 0.0           # objective gate: require the empirical expectancy of
                           # this signal's regime (strength x vol) to be >= this.
                           # Data-driven replacement for the arbitrary 0.60.
# Overextension filter: don't CHASE -- skip a long when already overbought / a
# short when already oversold. Replay-validated 2026-06-18 (s5, OOS): expR
# 0.150 -> 0.172, DSR held at 92%. Matches the live tape + win_model (entering
# with strong momentum at extremes loses).
OVEREXT_FILTER = True
OVEREXT_HI = 70.0          # block longs with RSI above this
OVEREXT_LO = 30.0          # block shorts with RSI below this
# Weekly time-series momentum works in COMMODITIES + EQUITY INDICES and NOT in
# FX (mean-reverting) -- confirmed by the breadth test and the TSMOM literature
# (Moskowitz/Ooi/Pedersen). Restrict the trend strategy to those classes.
# Empty set = trade all classes.
#
# BROKER-aware default. The FUTURES universe (BROKER=ib) was re-tested in a 7-combo
# OOS class battery (backtest.py --classes): {metal,index,rate} won -- best
# risk-adjusted (OOS +7.4% CAGR at -6.6% DD vs the spot whitelist's +6.1%/-6.5%).
# ENERGY was dead weight (drops OOS expR), grains/softs/fx are net-negative. The
# MT5/spot universe keeps its original whitelist (it has no rate futures, and
# silently dropping energy there would be an unvalidated live change).
def _default_trend_classes() -> set[str]:
    import os
    if os.environ.get("BROKER", "mt5").lower() == "ib":
        if os.environ.get("UNIVERSE", "futures").lower() == "etf":
            # 21-ETF set (16 core/screened + PFF + CWB + VNQI + AMLP + HYD). em_bond (EMB)
            # dropped 2026-06-23: redundant vs HYG credit + TLT duration, and a drag on
            # risk-adjusted return across BOTH the recent and full-history (--longweekly)
            # windows. PFF kept -- best CAGR/DD ratio in both. EMB still defined in
            # instruments.py for research; re-test if a major EM-credit dislocation gives it
            # a distinct edge. CWB (convertible) + VNQI (intl_reit) + AMLP (mlp) + HYD
            # (muni_hy) ADDED 2026-07-08: isolation-tested vs their respective bases, +1.0pp /
            # +0.5pp / +0.9pp / +0.6pp OOS CAGR each for flat-or-better DD ratio -- see
            # HANDOFF. (PALL/PPLT/URA/BWX/PICB/WOOD/EMLC/IGF/BIZD/COPX all screened and
            # rejected; BKLN/FM deferred, n too small.) china_eq (ASHR) ADDED 2026-07-09:
            # isolation-tested vs the 21-base, +0.43pp OOS CAGR (+11.17%->+11.60%) for
            # IDENTICAL maxDD to 4 decimals (-12.9065% both), ratio 0.866->0.899 -- see
            # HANDOFF batch-10. (USFR/WIP/FLOT screened batch-10 and rejected/untestable;
            # MBB deferred, isolation flat despite strong raw expR.)
            return {"metal", "index", "rate", "credit", "inflation", "intl_eq",
                    "commodity", "reit", "preferred", "convertible", "intl_reit", "mlp",
                    "muni_hy", "china_eq"}
        return {"metal", "index", "rate"}      # evidence-based futures universe
    return {"metal", "energy", "index"}        # original spot/MT5 whitelist


WEEKLY_TREND_CLASSES = _default_trend_classes()

# Direction: under BROKER=ib (futures) the SHORT side is net-negative -- index/metal
# futures drift up, so shorting fights the drift. Validated 2026-06-21 (--direction-test):
# long-only OOS expR +0.415 vs long+short +0.282, PF 1.57 vs 1.44, DD -9.3% vs -9.9%,
# and short-only is -0.082 (a certain bleed) that also crowds out longs via the
# one-per-instrument/de-correlation gates. MT5/spot keeps both directions.
import os as _os
LONG_ONLY = _os.environ.get("BROKER", "mt5").lower() == "ib"

# --- account PHASE (auto-switch by equity) ----------------------------------------
# Phase 1 (<PHASE2_NAV_USD): core 17-ETF only, 1% risk + 25% notional cap.
# Phase 2 (>=PHASE2_NAV_USD): core + the panic-MR dip sleeve (11-ticker set, see
# dashboard.core.sleeve.SLEEVE_UNIVERSE). Same cap/risk.
# (Phase 3 / cap-loosening was REJECTED -- pure leverage, worse ratio, trips the DD tripwire.)
# The sleeve's ORDER EXECUTION is a separate build; sleeve_active() is the gate it plugs into,
# so it turns on AUTOMATICALLY when the account crosses the threshold -- no manual step.
PHASE2_NAV_USD = float(_os.environ.get("PHASE2_NAV_USD", "64000"))   # ~500K HKD at 7.8


def account_phase(equity_usd: float | None) -> int:
    """1 or 2 by live equity (USD). Auto-switches the plan; see sleeve_active()."""
    try:
        return 2 if equity_usd and float(equity_usd) >= PHASE2_NAV_USD else 1
    except (TypeError, ValueError):
        return 1


def sleeve_active(equity_usd: float | None) -> bool:
    """True once the account is big enough (Phase 2) to run the panic-MR sleeve."""
    return account_phase(equity_usd) >= 2
MIN_STRENGTH = 5           # only the strongest (5/5) trend alignment. Strength-4
                           # regimes (esp. s4-low, +0.018R) are barely-positive
                           # and noisy -- excluded by choice. The objective edge
                           # gate still applies on top, but with this at 5 only
                           # the (positive-edge) s5 regimes reach it.
VOL_FILTER = False         # RETIRED 2026-06-16: superseded by the objective gate,
                           # which conditions edge on (strength x vol regime)
                           # directly. The blunt filter blocked positive-edge
                           # low-vol regimes (s5-low +0.008R, s4-low +0.018R),
                           # contradicting the model. Kept as a toggle for A/B.
HALF_SPREAD = 0.00005      # per-side cost as fraction of price (~0.5 bp)
COOLDOWN_MIN = 60          # don't re-enter the same instrument within N minutes
                           # of its last close (prevents churning one instrument)

# STABLE location at the dashboard/ package root -- NOT relative to this file's
# subpackage. paper.py lives in dashboard/core/, so parents[1] == dashboard/.
# (Keeps the live journal at dashboard/dashboard.db across the reorg; tying it to
# parent/ would have silently pointed at a new empty dashboard/core/dashboard.db.)
# DASH_DB_NAME picks the file: PAPER and LIVE mode get SEPARATE databases (journal, ib_mirror,
# cache/settings) so switching modes never shows the other account's trade history/stats.
# `_DB` is LAZY (module __getattr__ below), computed fresh from the env var on every access --
# NOT fixed at import time -- so it's correct regardless of when/whether the mode was resolved
# before this module was first imported (robust for other entrypoints: research/tests/etc.).
_LOCK = threading.Lock()


def _dbpath() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1] / _os.environ.get("DASH_DB_NAME", "dashboard.db")


def __getattr__(name):     # PEP 562: makes `paper._DB` a live property, not an import-time constant
    if name == "_DB":
        return _dbpath()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ---- SL/TP computation -----------------------------------------------------

def compute_sltp(facts: dict, direction: str, method: str, rr: float = RR_DEFAULT,
                 entry: float | None = None):
    """Return (entry, sl, tp, rr_actual) or None if geometry is invalid.
    method: 'ATR' (fixed rr) or 'STRUCT' (support/resistance, rr falls out).

    `entry` should be the LIVE price at placement. If omitted we fall back to the
    (possibly stale) last bar close -- which, against live-tick resolution, makes
    trades enter offside and get stopped instantly. Always pass the live price."""
    entry = entry if entry is not None else facts["last_price"]
    atr = facts.get("atr14") or 0.0
    if method == "ATR":
        if atr <= 0:
            return None
        risk = SL_ATR_MULT * atr
        if direction == "long":
            sl, tp = entry - risk, entry + rr * risk
        else:
            sl, tp = entry + risk, entry - rr * risk
        return entry, sl, tp, rr
    # STRUCT
    sup, res = facts["support_60"], facts["resistance_60"]
    if direction == "long":
        sl, tp = sup, res
        risk, reward = entry - sl, tp - entry
    else:
        sl, tp = res, sup
        risk, reward = sl - entry, entry - tp
    if risk <= 0 or reward <= 0:
        return None
    return entry, sl, tp, reward / risk


# ---- resolution + R --------------------------------------------------------

def resolve(direction: str, entry: float, sl: float, tp: float,
            bars: pd.DataFrame) -> tuple[str, float, pd.Timestamp] | None:
    """Walk bars (already filtered to after-entry, within-horizon) and decide
    outcome. SL checked before TP within a bar (conservative). Returns
    (status, exit_price, exit_time) or None if still open (no bars yet)."""
    for ts, row in bars.iterrows():
        hi, lo = row["high"], row["low"]
        hit_sl = (lo <= sl) if direction == "long" else (hi >= sl)
        hit_tp = (hi >= tp) if direction == "long" else (lo <= tp)
        if hit_sl and hit_tp:
            log.warning("ambiguous bar @ %s: both SL %.5f and TP %.5f inside "
                        "[%.5f, %.5f] -> assuming SL hit first (conservative)",
                        ts, sl, tp, lo, hi)
        if hit_sl:
            return "LOSS", sl, ts
        if hit_tp:
            return "WIN", tp, ts
    if len(bars):
        return "EXPIRED", float(bars["close"].iloc[-1]), bars.index[-1]
    return None


def resolve_ticks(direction: str, sl: float, tp: float,
                  ticks: pd.DataFrame) -> tuple[str, float, pd.Timestamp] | None:
    """Exact resolution from tick data: whichever of SL/TP is touched FIRST
    chronologically wins -- no conservative assumption needed. Uses bid to close
    longs, ask to close shorts. `ticks` has columns bid, ask, indexed by time."""
    for ts, row in ticks.iterrows():
        bid, ask = row["bid"], row["ask"]
        if direction == "long":
            if bid <= sl:
                return "LOSS", sl, ts
            if bid >= tp:
                return "WIN", tp, ts
        else:
            if ask >= sl:
                return "LOSS", sl, ts
            if ask <= tp:
                return "WIN", tp, ts
    if len(ticks):
        last = ticks.iloc[-1]
        px = last["bid"] if direction == "long" else last["ask"]
        return "EXPIRED", float(px), ticks.index[-1]
    return None


def _as_utc(x) -> pd.Timestamp:
    """Parse a stored timestamp to tz-aware UTC (naive is assumed already-UTC)."""
    ts = pd.Timestamp(x)
    return ts.tz_localize("UTC") if ts.tz is None else ts.tz_convert("UTC")


def _mt5_to_utc(x, offset_sec: float) -> pd.Timestamp:
    """MT5 stamps times in broker SERVER time (labeled UTC). Subtract the
    detected server offset to get true UTC, so it lines up with entry time."""
    return _as_utc(x) - pd.Timedelta(seconds=offset_sec)


def r_multiple(direction: str, entry: float, sl: float, exit_price: float,
               half_spread: float = HALF_SPREAD, cost_abs: float | None = None) -> float:
    # cost_abs (price points, round-turn) overrides the CFD half-spread fraction
    # when given -- this is the futures path (contracts.cost_points): commission +
    # tick slippage, which is per-contract not a fraction of price.
    cost = cost_abs if cost_abs is not None else entry * half_spread * 2  # entry + exit
    if direction == "long":
        risk, pnl = entry - sl, exit_price - entry - cost
    else:
        risk, pnl = sl - entry, entry - exit_price - cost
    return pnl / risk if risk > 0 else 0.0


# ---- stats -----------------------------------------------------------------

def stats(rs: list[float]) -> dict:
    """Aggregate a list of realized R-multiples into the honest scorecard."""
    n = len(rs)
    if n == 0:
        return {"n": 0, "win_rate": 0, "expectancy_R": 0, "avg_win_R": 0,
                "avg_loss_R": 0, "profit_factor": 0, "total_R": 0, "trustworthy": False}
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "n": n,
        "win_rate": len(wins) / n,
        "expectancy_R": sum(rs) / n,
        "avg_win_R": (gross_win / len(wins)) if wins else 0.0,
        "avg_loss_R": (sum(losses) / len(losses)) if losses else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "total_R": sum(rs),
        "trustworthy": n >= 30,   # below this, the numbers are basically noise
    }


def deposit_adjusted_series(hist: list, flows: list | None) -> list[float]:
    """hist: [[ts, value, ccy], ...] ascending. flows: [[ts, amount, ccy], ...] (see
    service.py's equity_history cash-flow logging). Returns hist's values with the
    cumulative net cash flow up to each point subtracted, so the series reads as pure
    trading P&L -- deposits/withdrawals become invisible instead of looking like gains.
    Shared by app.py's equity chart AND current_drawdown_pct() below -- a deposit must
    never look like a new all-time high that resets the peak and hides a real drawdown."""
    if not flows:
        return [h[1] for h in hist]
    flows_sorted = sorted(flows, key=lambda f: f[0])
    out = []
    fi, cum = 0, 0.0
    for ts, val, _ccy in hist:
        while fi < len(flows_sorted) and flows_sorted[fi][0] <= ts:
            cum += flows_sorted[fi][1]
            fi += 1
        out.append(val - cum)
    return out


def current_drawdown_pct(hist: list, flows: list | None) -> float:
    """Current % drawdown from the all-time (deposit-adjusted) peak -- e.g. -13.5 means
    13.5% below the peak. 0.0 if there isn't enough history yet to judge (<2 points).
    Uses the FULL series unconditionally (never a windowed/recent view) -- the true peak,
    not a peak within some display period, matches the "Drawdown from peak" stat already
    shown on the dashboard (same underlying math, extracted here so ib_exec's DD-halt gate
    can share it instead of re-deriving it).

    MATERIALITY FLOOR (2026-07-11, found live reporting a bogus -90% "drawdown" on a
    brand-new account): `adj[0]` is whatever raw cash happened to be sitting in the account
    BEFORE the first tracked deposit -- for a fresh account that's a near-zero leftover
    balance (here, ~40 HKD / ~$5), not real trading capital. Once deposits land, `adj`
    correctly nets them out and reads as pure trading P&L, which starts near zero and takes
    time to accumulate -- so `peak` ends up being that tiny pre-funding artifact, and ANY
    trivial dip below it (a few dollars, e.g. commissions) computes as a huge % drawdown.
    Require the peak to be at least 1% of the account's CURRENT raw equity (a real, sized
    reference) before trusting a percentage; below that there isn't enough realized trading
    P&L yet to judge -- same "not enough history" spirit as the len(hist)<2 guard above.
    Without this, DD_HALT_PCT could get permanently stuck halting a brand-new account over
    a few-dollar wobble."""
    if len(hist) < 2:
        return 0.0
    adj = deposit_adjusted_series(hist, flows)
    floor = abs(hist[-1][1]) * 0.01
    peak = adj[0]
    cur_dd = 0.0
    for v in adj:
        peak = max(peak, v)
        cur_dd = (v - peak) / peak * 100.0 if peak > floor else 0.0
    return cur_dd


# ---- forward (live) paper trades: persistence ------------------------------

@dataclass
class Trade:
    ts: str
    instrument: str
    direction: str        # long/short
    method: str           # 'ATR rr2.0' | 'STRUCT'
    entry: float
    sl: float
    tp: float
    rr: float
    size_units: float
    horizon_end: str
    confidence: float
    rationale: str
    status: str = "OPEN"  # OPEN/WIN/LOSS/EXPIRED
    exit_ts: str = ""
    exit_price: float = 0.0
    realized_r: float = 0.0
    half_spread: float = HALF_SPREAD  # actual per-side cost fraction at placement
    # --- frozen decision context (for retrospective; never used in logic) ---
    invalidation: str = ""    # LLM's explicit "this is wrong if..." level
    llm_bias: str = ""        # bullish/bearish/neutral
    det_strength: int = 0     # deterministic trend strength 1..5 at entry
    det_note: str = ""        # deterministic scorer's note at entry
    macro_note: str = ""      # macro backdrop at the time of entry
    entry_facts: str = ""     # JSON snapshot of the key facts at entry
    exit_reason: str = ""     # human reason at close (TP/SL/horizon/manual)
    macro_linkage: str = ""   # ADDED 2026-07-14: LLM's explicit check of whether ITS OWN
                              # macro_note actually applies to THIS instrument (e.g. a
                              # USD-strength headwind), not just the general backdrop --
                              # see board_scan.py's InstrumentSignal.macro_linkage


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_dbpath(), check_same_thread=False)
    c.execute("""CREATE TABLE IF NOT EXISTS paper_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, instrument TEXT, direction TEXT,
        method TEXT, entry REAL, sl REAL, tp REAL, rr REAL, size_units REAL,
        horizon_end TEXT, confidence REAL, rationale TEXT, status TEXT,
        exit_ts TEXT, exit_price REAL, realized_r REAL)""")
    # additive migrations for pre-existing DBs: (column, SQL type + default)
    _MIGRATIONS = [
        ("half_spread", "REAL DEFAULT 0.00005"),
        ("invalidation", "TEXT DEFAULT ''"),
        ("llm_bias", "TEXT DEFAULT ''"),
        ("det_strength", "INTEGER DEFAULT 0"),
        ("det_note", "TEXT DEFAULT ''"),
        ("macro_note", "TEXT DEFAULT ''"),
        ("entry_facts", "TEXT DEFAULT ''"),
        ("exit_reason", "TEXT DEFAULT ''"),
        ("macro_linkage", "TEXT DEFAULT ''"),
    ]
    for table in ("paper_trades", "paper_trades_archive"):
        if not c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                         (table,)).fetchone():
            continue
        cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
        for col, decl in _MIGRATIONS:
            if col not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    return c


def _insert(t: Trade) -> None:
    with _LOCK, _conn() as c:
        d = asdict(t)
        cols = ",".join(d.keys())
        c.execute(f"INSERT INTO paper_trades({cols}) VALUES({','.join('?'*len(d))})",
                  list(d.values()))


def open_trades() -> list[dict]:
    with _LOCK, _conn() as c:
        cur = c.execute("SELECT * FROM paper_trades WHERE status='OPEN'")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def all_trades() -> list[dict]:
    with _LOCK, _conn() as c:
        cur = c.execute("SELECT * FROM paper_trades ORDER BY id DESC")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _has_open(instrument: str, method: str) -> bool:
    with _LOCK, _conn() as c:
        return c.execute("SELECT 1 FROM paper_trades WHERE instrument=? AND method=? "
                         "AND status='OPEN' LIMIT 1", (instrument, method)).fetchone() is not None


# --- de-correlation (Tier-2 step 3) ----------------------------------------
# Correlated instruments are one bet sized N times: the FX majors + metals are
# mostly a "long/short USD" bet, the JPY pairs a "short/long JPY" bet, the two
# indices a "long/short equities" bet. Limit concurrent open positions to ONE
# instrument per (bucket, direction); different methods on the SAME instrument
# are still allowed. Oil isn't a clean member of any bucket -> unconstrained.
DECORRELATE = True
_USD_QUOTE = {"EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"}  # USD is the quote ccy
_USD_BASE = {"USDJPY", "USDCAD", "USDCHF"}             # USD is the base ccy
_METALS = {"XAUUSD", "XAGUSD", "XPTUSD", "XPDUSD"}     # priced in USD
_JPY_SHORT = {"USDJPY", "EURJPY", "GBPJPY", "AUDJPY"}  # long pair = short JPY
_EQUITY = {"SPX", "NDX", "DJI", "DE40", "UK100",       # broadly co-move
           "JP225", "HK50", "AUS200"}
_CRYPTO = {"BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD"}     # highly correlated bloc


def _risk_buckets(instrument: str, direction: str) -> list[tuple[str, int]]:
    """The macro bets this trade expresses, as (bucket, +1/-1) pairs. A trade can
    sit in several buckets (USDJPY is both a USD and a JPY bet). Holding several
    instruments in the same (bucket, direction) is really one position sized N
    times, so de-correlation allows only one per (bucket, direction).
    Energy (oil/gas) is left unconstrained -- not a clean member of any bucket."""
    sign = +1 if direction == "long" else -1
    buckets: list[tuple[str, int]] = []
    if instrument in _USD_QUOTE or instrument in _METALS:
        buckets.append(("USD", -sign))
    if instrument in _USD_BASE:
        buckets.append(("USD", sign))
    if instrument in _JPY_SHORT:
        buckets.append(("JPY", -sign))
    if instrument in _EQUITY:
        buckets.append(("EQ", sign))
    if instrument in _CRYPTO:
        buckets.append(("CRYPTO", sign))
    return buckets


def _recent_close(instrument: str, minutes: int = COOLDOWN_MIN) -> bool:
    """True if this instrument had a trade close within the last `minutes`."""
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes))
    with _LOCK, _conn() as c:
        rows = c.execute("SELECT exit_ts FROM paper_trades WHERE instrument=? "
                         "AND status!='OPEN' AND exit_ts!=''", (instrument,)).fetchall()
    for (xt,) in rows:
        try:
            if _as_utc(xt) >= cutoff:
                return True
        except Exception:
            continue
    return False


def _update_resolution(trade_id: int, status: str, exit_ts: str,
                       exit_price: float, realized_r: float,
                       exit_reason: str = "") -> None:
    with _LOCK, _conn() as c:
        c.execute("UPDATE paper_trades SET status=?, exit_ts=?, exit_price=?, "
                  "realized_r=?, exit_reason=? WHERE id=?",
                  (status, exit_ts, exit_price, realized_r, exit_reason, trade_id))


# ---- forward funnel: turn a live signal into trades ------------------------

def evaluate_signal(key: str, score, llm_sig) -> tuple[bool, list[str], str]:
    """Apply the entry gates. Returns (passed, reasons, direction)."""
    reasons: list[str] = []
    action = (llm_sig.action if llm_sig else score.signal)
    if action not in ("BUY", "SELL"):
        # Two different things collapse to the same WAIT/WATCH action, and only one of
        # them is noise: (a) the instrument never had a real deterministic setup at all
        # (score.signal itself is WATCH) -- genuinely uninteresting, most of the 22-ETF
        # book sits here most days; (b) the LLM actively VETOED a real deterministic
        # BUY/SELL into WAIT (news it judged relevant, its own overextension read, or a
        # low-confidence calibration -- see board_scan.py's system prompt). (b) is a
        # meaningful, LLM-driven rejection -- place_from_state()'s "skip WAIT/WATCH
        # noise" filter (`reasons != ["action is WAIT/WATCH"]`) was silently discarding
        # it too, since both cases produced the exact same one-line reason. Distinguish
        # them so (b) reaches the rejected_signals journal / retrospective scorecard.
        if llm_sig and score.signal in ("BUY", "SELL") and llm_sig.action == "WAIT":
            # FIXED 2026-07-13: journal.rejection_counts() joins each trade's reasons with
            # "; " and splits back on the SAME bare ";" to re-separate them for the
            # scorecard -- an assumption that was safe for the fixed-template reasons
            # elsewhere in this function ("trend strength X < 5", etc.) but breaks the
            # moment free-text LLM rationale is embedded here, since the LLM's own
            # sentences routinely contain semicolons (confirmed live: "...muted short-term
            # returns; wait for break" got sliced into two bogus scorecard rows, one of
            # them a meaningless truncated fragment). Strip semicolons from the embedded
            # rationale so it can never accidentally fragment across that split boundary --
            # the canonical gate label _canon() matches on ("llm vetoed to wait...") is
            # unaffected either way, this only protects the free-text tail.
            rationale = (llm_sig.rationale or "")[:100].replace(";", ",")
            return False, [f"LLM vetoed to WAIT (deterministic was {score.signal}): "
                          f"{rationale}"], ""
        return False, ["action is WAIT/WATCH"], ""
    direction = "long" if action == "BUY" else "short"
    if LONG_ONLY and direction == "short":
        return False, ["long-only: short side disabled (net-negative on futures)"], direction
    det_action = score.signal
    if det_action not in ("BUY", "SELL") or (det_action == "BUY") != (action == "BUY"):
        reasons.append("no confluence: deterministic trend disagrees with action")
    # objective confidence: gate on the empirical edge of this regime
    # (strength x volatility), measured from replay + journal -- not the LLM's
    # self-reported number. Neutral when the bucket has too little data.
    from dashboard.models import confidence_model
    obj = confidence_model.objective(score)
    if obj is not None:
        win, exp, nobs = obj
        if nobs >= confidence_model.MIN_SAMPLES and exp < MIN_EDGE_R:
            reasons.append(f"objective edge {exp:+.2f}R (win {win:.0%}, n{nobs})")
    if score.strength < MIN_STRENGTH:
        reasons.append(f"trend strength {score.strength} < {MIN_STRENGTH}")
    if WEEKLY_TREND_CLASSES:
        from dashboard.instruments import active_by_key
        cls = active_by_key(key).asset_class
        if cls not in WEEKLY_TREND_CLASSES:
            reasons.append(f"off-strategy: {cls} (weekly trend = commodities/indices)")
    if OVEREXT_FILTER:
        rsi = score.facts.get("rsi14") or 50.0
        if (direction == "long" and rsi > OVEREXT_HI) or \
           (direction == "short" and rsi < OVEREXT_LO):
            reasons.append(f"overextended RSI {rsi:.0f}")
    if VOL_FILTER:
        atr = score.facts.get("atr14") or 0.0
        med = score.facts.get("atr14_med60") or 0.0
        if med > 0 and atr < med:
            reasons.append(f"vol filter: atr14 {atr:.5g} < 60-bar median {med:.5g}")
    return (len(reasons) == 0), reasons, direction


def place_from_state(state: dict) -> list[str]:
    """Create paper trades for every qualifying signal (both SL/TP methods)."""
    from dashboard.instruments import BY_KEY
    from dashboard.data import mt5_client
    logs: list[str] = []
    log.info("placement run: evaluating %d instruments", len(state.get("scores", {})))
    now = dt.datetime.now(dt.timezone.utc)  # store entry time in true UTC
    horizon_end = (now + dt.timedelta(days=HORIZON_CAL)).isoformat(timespec="seconds")
    # current exposure (instrument set per bucket+direction) for de-correlation
    open_by_bucket: dict[tuple[str, int], set] = {}
    for ot in open_trades():
        for b in _risk_buckets(ot["instrument"], ot["direction"]):
            open_by_bucket.setdefault(b, set()).add(ot["instrument"])
    macro = state.get("macro_note", "")
    rejected: list[dict] = []  # genuine BUY/SELL candidates that got blocked

    def _reject(reasons: list[str], score, llm_sig, direction: str) -> None:
        rejected.append({"instrument": score.key, "direction": direction,
                         "det_strength": score.strength,
                         "confidence": (llm_sig.confidence if llm_sig else 0.0),
                         "reasons": reasons})

    for key, score in state["scores"].items():
        llm_sig = state.get("llm", {}).get(key)
        ok, reasons, direction = evaluate_signal(key, score, llm_sig)
        if not ok:
            logs.append(f"{key}: skip ({'; '.join(reasons)})")
            # only record genuine directional candidates, not WAIT/WATCH noise
            if reasons != ["action is WAIT/WATCH"]:
                _reject(reasons, score, llm_sig, direction)
            continue
        conf = llm_sig.confidence if llm_sig else 0.0
        rationale = (llm_sig.rationale if llm_sig else score.note)[:300]
        # use the LIVE price as entry (same feed as resolution) -- NOT the stale
        # bar close, which makes trades enter offside and stop instantly.
        live = state.get("live", {}).get(key) or {}
        entry_px = live.get("price")
        if entry_px is None:
            logs.append(f"{key}: skip (no live price for entry)")
            _reject(["no live price for entry"], score, llm_sig, direction)
            continue
        # real cost: half the observed live spread as a fraction of price;
        # fall back to the flat default when only bar data is available
        hs = (live["spread"] / 2) / entry_px if live.get("spread") else HALF_SPREAD
        if _recent_close(key):
            logs.append(f"{key}: skip (cooldown — closed within {COOLDOWN_MIN}m)")
            _reject([f"cooldown < {COOLDOWN_MIN}m"], score, llm_sig, direction)
            continue
        buckets = _risk_buckets(key, direction)
        if DECORRELATE:
            clash = next((b for b in buckets
                          if open_by_bucket.get(b, set()) - {key}), None)
            if clash:
                side = "long" if clash[1] > 0 else "short"
                holders = open_by_bucket[clash]
                logs.append(f"{key}: skip (de-correlation: already {side} "
                            f"{clash[0]} via {holders})")
                _reject([f"de-correlation {clash[0]}"], score, llm_sig, direction)
                continue
        variants = [("ATR", rr) for rr in [RR_DEFAULT]] + [("STRUCT", None)]
        for method, rr in variants:
            res = compute_sltp(score.facts, direction, method, rr or RR_DEFAULT, entry=entry_px)
            if res is None:
                logs.append(f"{key} {method}: skip (invalid geometry)")
                continue
            entry, sl, tp, rr_actual = res
            if rr_actual < MIN_RR:
                logs.append(f"{key} {method}: skip (R:R {rr_actual:.2f} < {MIN_RR})")
                continue
            # round SL/TP to the broker's price precision so the paper trade and
            # the demo order use the SAME levels -- otherwise the broker stops out
            # at its rounded level while the paper resolver waits for an unrounded
            # one the market never quite reaches (leaving the trade stuck open).
            # round SL/TP to the broker's tradable precision so paper and live
            # use the SAME levels. MT5: symbol digits. IB futures: contract tick.
            from dashboard.instruments import _ib_broker
            if _ib_broker():
                from dashboard.data.contracts import SPECS
                spec = SPECS.get(key)
                if spec:
                    tick = spec.tick_size
                    entry, sl, tp = (round(round(x / tick) * tick, 10) for x in (entry, sl, tp))
            else:
                digits = mt5_client.symbol_digits(BY_KEY[key].mt5)
                if digits:
                    entry, sl, tp = round(entry, digits), round(sl, digits), round(tp, digits)
            mlabel = f"ATR rr{rr:.1f}" if method == "ATR" else "STRUCT"
            if _has_open(key, mlabel):
                logs.append(f"{key} {mlabel}: skip (already open)")
                continue
            risk_price = abs(entry - sl)
            size = (ACCOUNT * RISK_PER_TRADE) / risk_price if risk_price > 0 else 0
            f = score.facts
            try:
                from dashboard.models import confidence_model
                _obj = confidence_model.objective(score)
            except Exception:
                _obj = None
            entry_facts = json.dumps({
                "rsi14": f.get("rsi14"), "atr14": f.get("atr14"),
                "atr14_med60": f.get("atr14_med60"),
                "realized_vol_annual": f.get("realized_vol_annual"),
                "trend": f.get("trend"), "returns": f.get("returns"),
                "trend_tstat": f.get("trend_tstat"),
                "support_60": f.get("support_60"), "resistance_60": f.get("resistance_60"),
                "vol_filter_ok": (f.get("atr14") or 0) >= (f.get("atr14_med60") or 0),
                "obj_win": _obj[0] if _obj else None,
                "obj_expectancy_R": _obj[1] if _obj else None,
                "obj_n": _obj[2] if _obj else None,
            }, default=float)
            _insert(Trade(
                ts=now.isoformat(timespec="seconds"), instrument=key, direction=direction,
                method=mlabel, entry=entry, sl=sl, tp=tp, rr=round(rr_actual, 2),
                size_units=round(size, 2), horizon_end=horizon_end,
                confidence=conf, rationale=rationale, half_spread=hs,
                invalidation=(llm_sig.invalidation if llm_sig else ""),
                llm_bias=(llm_sig.bias if llm_sig else ""),
                det_strength=score.strength, det_note=score.note[:300],
                macro_note=macro[:500], entry_facts=entry_facts,
                macro_linkage=(llm_sig.macro_linkage if llm_sig else "")))
            msg = (f"{key} {mlabel}: PLACED {direction} entry {entry:.4f} "
                   f"SL {sl:.4f} TP {tp:.4f} (R:R {rr_actual:.2f}, size {size:.1f}, conf {conf:.2f})")
            logs.append(msg)
            log.info("PLACED %s", msg)
            for b in buckets:
                open_by_bucket.setdefault(b, set()).add(key)
    for line in logs:
        # log only INFORMATIVE skips (a gate actually blocked a candidate). The
        # WAIT/WATCH "skips" are just idle instruments -- the normal state for a
        # low-frequency strategy -- and were ~95% of the debug log volume.
        if "skip" in line and "WAIT/WATCH" not in line:
            log.debug("funnel %s", line)
    # persist the counterfactuals (rejected candidates) for constraint analysis
    try:
        from dashboard.core import journal
        journal.record_rejections(rejected)
    except Exception as e:
        log.warning("journal: could not record rejections: %s", e)
    return logs


def gate_report(state: dict) -> list[dict]:
    """Read-only diagnostic: for EVERY instrument, evaluate each entry gate and
    report pass/fail + the live values, so the UI can show exactly why a signal
    is or isn't producing a trade. Reuses the same gate functions as
    place_from_state, so it can never drift from real placement behaviour.

    Returns one dict per instrument, sorted strongest-signal first."""
    # current de-correlation exposure (same construction as place_from_state)
    open_by_bucket: dict[tuple[str, int], set] = {}
    open_methods: dict[str, set] = {}
    for ot in open_trades():
        open_methods.setdefault(ot["instrument"], set()).add(ot["method"])
        for b in _risk_buckets(ot["instrument"], ot["direction"]):
            open_by_bucket.setdefault(b, set()).add(ot["instrument"])

    rows: list[dict] = []
    for key, score in state.get("scores", {}).items():
        llm = state.get("llm", {}).get(key)
        action = (llm.action if llm else score.signal)
        conf = llm.confidence if llm else None
        atr = score.facts.get("atr14") or 0.0
        med = score.facts.get("atr14_med60") or 0.0
        try:
            from dashboard.models import confidence_model
            obj = confidence_model.objective(score)
        except Exception:
            obj = None
        ok, reasons, direction = evaluate_signal(key, score, llm)

        # gates that only apply once it's a real directional candidate
        already_open = ""
        decorr = ""
        cooldown = ""
        if direction:
            if _recent_close(key):
                cooldown = f"cooldown < {COOLDOWN_MIN}m"
            for b in _risk_buckets(key, direction):
                holders = open_by_bucket.get(b, set()) - {key}
                if holders:
                    decorr = f"{b[0]} held by {', '.join(sorted(holders))}"
                    break
            # ATR rr-default is the variant we actually trade live
            mlabel = f"ATR rr{RR_DEFAULT:.1f}"
            if mlabel in open_methods.get(key, set()):
                already_open = "position already open"

        blocked = list(reasons)
        if cooldown:
            blocked.append(cooldown)
        if decorr:
            blocked.append(f"de-correlation: {decorr}")
        if already_open:
            blocked.append(already_open)

        if already_open:
            status = "OPEN"
        elif action not in ("BUY", "SELL"):
            status = "WAIT"
        elif not blocked:
            status = "WOULD TRADE"
        else:
            status = "BLOCKED"

        rows.append({
            "key": key, "action": action, "direction": direction,
            "strength": score.strength, "confidence": conf,
            "atr14": atr, "atr14_med60": med, "vol_ok": (med == 0 or atr >= med),
            "obj_win": obj[0] if obj else None,
            "obj_edge": obj[1] if obj else None, "obj_n": obj[2] if obj else None,
            "status": status, "blocked_by": blocked,
            "obviousness": score.obviousness,
        })
    rows.sort(key=lambda r: r["obviousness"], reverse=True)
    return rows


def _outcome_for(t: dict, get_ohlc_fn):
    """Resolve one open trade. Prefers exact MT5 tick resolution; falls back to
    (conservative) OHLC-bar resolution. Returns outcome tuple, "OPEN", or None."""
    from dashboard.instruments import active_by_key
    from dashboard.data import mt5_client
    inst = active_by_key(t["instrument"])
    # everything in true UTC; MT5 source times get the broker offset removed
    entry_ts = _as_utc(t["ts"])
    end_ts = _as_utc(t["horizon_end"])
    now_utc = pd.Timestamp.now(tz="UTC")
    horizon_passed = now_utc >= end_ts
    offset = store.cache_get("mt5_offset_sec")[0] or 0

    # tick resolution is only worth it for SHORT windows -- over a multi-week
    # (weekly-strategy) horizon the tick fetch is millions of rows. For long
    # windows skip straight to daily-bar resolution (ample for weekly SL/TP).
    win_days = ((end_ts if horizon_passed else now_utc) - entry_ts).days

    # 1) exact path: MT5 ticks
    if mt5_client.is_available() and win_days <= 10:
        try:
            # Pad the query window generously. MT5 stamps ticks in the broker's
            # SERVER timezone, so passing real-UTC bounds makes copy_ticks_range
            # truncate the window by the server offset (several hours) -- which
            # silently drops the most recent ticks and leaves trades unresolved
            # even after their SL/TP was hit. A 12h pad is larger than any real
            # broker offset; we re-trim precisely below using `offset`.
            margin = pd.Timedelta(hours=12)
            t0 = (entry_ts - margin).to_pydatetime()
            t1 = ((end_ts if horizon_passed else now_utc) + margin).to_pydatetime()
            ticks = mt5_client.get_ticks_range(inst.mt5, t0, t1)
            if ticks is not None and len(ticks):
                # CRITICAL: derive the broker server offset from the DATA, not the
                # cached value (which can be 0/stale). MT5 stamps ticks in server
                # time; the newest tick is ~now in reality, so the gap is the
                # offset. Getting this wrong feeds PRE-entry ticks into the
                # resolver and stops fresh trades instantly at a phantom SL.
                est = (ticks.index.max() - now_utc).total_seconds()
                if -7200 <= est <= 50400:        # plausible offsets: -2h .. +14h
                    offset = round(est / 1800) * 1800
                # keep only ticks strictly AFTER entry (convert server->true UTC
                # first) -- a trade must never be resolved by a tick at or before
                # the instant it was opened.
                true_idx = ticks.index - pd.Timedelta(seconds=offset)
                ticks = ticks[true_idx > entry_ts]
                # ...and never resolve on ticks past the horizon once it's passed
                if horizon_passed:
                    true_idx = ticks.index - pd.Timedelta(seconds=offset)
                    ticks = ticks[true_idx <= end_ts]
            if ticks is not None and len(ticks):
                out = resolve_ticks(t["direction"], t["sl"], t["tp"], ticks)
                log.debug("resolve %s %s via TICKS (%d ticks, exact) -> %s",
                          t["instrument"], t["method"], len(ticks),
                          out[0] if out else "open")
                if out and (out[0] != "EXPIRED" or horizon_passed):
                    status, px, xt = out
                    return status, px, _mt5_to_utc(xt, offset)
                return "OPEN"
        except Exception as e:
            log.debug("tick resolution failed for %s (%s); falling back to OHLC",
                      t["instrument"], e)

    # 2) fallback path: OHLC bars (conservative SL-before-TP)
    ohlc = get_ohlc_fn(inst)
    if ohlc is None:
        log.debug("no OHLC for %s; trade stays open", t["instrument"])
        return None
    idx = ohlc.index
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    ohlc = ohlc.set_axis(idx)
    bars = ohlc[(ohlc.index > entry_ts) & (ohlc.index <= end_ts)]
    out = resolve(t["direction"], t["entry"], t["sl"], t["tp"], bars)
    log.debug("resolve %s %s via OHLC (%d bars, conservative) -> %s",
              t["instrument"], t["method"], len(bars), out[0] if out else "open")
    if out and (out[0] != "EXPIRED" or horizon_passed):
        status, px, xt = out
        # MT5 rates are also server-time; yfinance bars are already UTC
        xt = _mt5_to_utc(xt, offset) if mt5_client.is_available() else _as_utc(xt)
        return status, px, xt
    return "OPEN"


def archive_and_reset() -> dict:
    """Snapshot the current journal (CSV + Markdown to exports/), copy every
    trade into paper_trades_archive tagged with a batch timestamp, then CLEAR
    the live paper_trades so counting starts fresh. Nothing is lost.

    Returns {archived, batch, csv, report}."""
    csvp = repp = ""
    try:
        from dashboard.web import report  # lazy import to avoid a circular import
        csvp, repp = report.export()
    except Exception as e:
        log.warning("archive: snapshot export failed: %s", e)

    batch = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    with _LOCK, _conn() as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(paper_trades)").fetchall()]
        c.execute("CREATE TABLE IF NOT EXISTS paper_trades_archive "
                  "(archive_batch TEXT, " + ", ".join(cols) + ")")
        n = c.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
        collist = ", ".join(cols)
        c.execute(f"INSERT INTO paper_trades_archive (archive_batch, {collist}) "
                  f"SELECT ?, {collist} FROM paper_trades", (batch,))
        c.execute("DELETE FROM paper_trades")
    log.info("archived %d trade(s) as batch %s; journal reset", n, batch)
    return {"archived": n, "batch": batch, "csv": csvp, "report": repp}


def archive_trades(ids: list[int]) -> int:
    """Archive SPECIFIC trades by id: copy them into paper_trades_archive (one
    batch) and remove them from the live journal. Nothing is lost -- they can be
    restored via unarchive(). Returns the count archived."""
    if not ids:
        return 0
    batch = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    placeholders = ",".join("?" * len(ids))
    with _LOCK, _conn() as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(paper_trades)").fetchall()]
        c.execute("CREATE TABLE IF NOT EXISTS paper_trades_archive "
                  "(archive_batch TEXT, " + ", ".join(cols) + ")")
        collist = ", ".join(cols)
        n = c.execute(f"SELECT COUNT(*) FROM paper_trades WHERE id IN ({placeholders})",
                      ids).fetchone()[0]
        c.execute(f"INSERT INTO paper_trades_archive (archive_batch, {collist}) "
                  f"SELECT ?, {collist} FROM paper_trades WHERE id IN ({placeholders})",
                  [batch] + ids)
        c.execute(f"DELETE FROM paper_trades WHERE id IN ({placeholders})", ids)
    log.info("archived %d specific trade(s) as batch %s", n, batch)
    return n


def archive_batches() -> list[dict]:
    """List archive batches with counts (for transparency)."""
    with _LOCK, _conn() as c:
        if not c.execute("SELECT name FROM sqlite_master WHERE type='table' "
                         "AND name='paper_trades_archive'").fetchone():
            return []
        rows = c.execute("SELECT archive_batch, COUNT(*) FROM paper_trades_archive "
                         "GROUP BY archive_batch ORDER BY archive_batch DESC").fetchall()
        return [{"batch": b, "n": n} for b, n in rows]


def archived_trades(batch: str | None = None) -> list[dict]:
    """Archived trades (optionally one batch). Each row carries `rowid`, the
    stable selector used by unarchive()."""
    with _LOCK, _conn() as c:
        if not c.execute("SELECT name FROM sqlite_master WHERE type='table' "
                         "AND name='paper_trades_archive'").fetchone():
            return []
        q = "SELECT rowid, * FROM paper_trades_archive"
        params: tuple = ()
        if batch:
            q += " WHERE archive_batch=?"
            params = (batch,)
        q += " ORDER BY rowid DESC"
        cur = c.execute(q, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def unarchive(rowids: list[int]) -> int:
    """Move selected archived rows back into the live journal. They get fresh
    ids; the archived copies are removed. Returns count restored."""
    if not rowids:
        return 0
    with _LOCK, _conn() as c:
        live_cols = [r[1] for r in c.execute("PRAGMA table_info(paper_trades)").fetchall()
                     if r[1] != "id"]
        collist = ", ".join(live_cols)
        placeholders = ", ".join("?" * len(live_cols))
        n = 0
        for rid in rowids:
            row = c.execute(f"SELECT {collist} FROM paper_trades_archive WHERE rowid=?",
                            (rid,)).fetchone()
            if row is None:
                continue
            c.execute(f"INSERT INTO paper_trades ({collist}) VALUES ({placeholders})", row)
            c.execute("DELETE FROM paper_trades_archive WHERE rowid=?", (rid,))
            n += 1
    log.info("unarchived %d trade(s) back to the live journal", n)
    return n


def resolve_open(get_ohlc_fn) -> int:
    """Resolve OPEN trades against fresh data. Returns count resolved."""
    resolved = 0
    for t in open_trades():
        out = _outcome_for(t, get_ohlc_fn)
        if out is None or out == "OPEN":
            continue
        status, exit_price, exit_time = out
        r = r_multiple(t["direction"], t["entry"], t["sl"], exit_price,
                       half_spread=t.get("half_spread") or HALF_SPREAD)
        reason = {"WIN": "take-profit hit", "LOSS": "stop-loss hit",
                  "EXPIRED": "horizon expired"}.get(status, status)
        _update_resolution(t["id"], status, str(exit_time), exit_price, round(r, 3),
                           exit_reason=reason)
        log.info("RESOLVED %s %s %s  R=%+.2f  entry=%.5f exit=%.5f @ %s",
                 t["instrument"], t["method"], status, r, t["entry"], exit_price, exit_time)
        resolved += 1
    return resolved
