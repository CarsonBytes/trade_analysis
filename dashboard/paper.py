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

from . import net  # noqa: F401
from dataclasses import dataclass, asdict
import datetime as dt
import json
import sqlite3
import pathlib
import threading

import pandas as pd

from .log import log
from . import store

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
HORIZON_DAYS = 5           # trade validity (trading days ~ calendar 7d window)
HORIZON_CAL = 7
RISK_PER_TRADE = 0.005     # 0.5% notional risk per trade (for size only)
ACCOUNT = 10_000.0
CONF_THRESHOLD = 0.60      # min LLM confidence (forward only)
MIN_STRENGTH = 5           # only the strongest (5/5) trend alignment
VOL_FILTER = True          # enter only when atr14 >= its 60-bar median.
                           # Replay-validated 2026-06-13: OOS expR 0.245->0.423,
                           # OOS DSR 88%->93% (4-trial penalty) vs baseline.
HALF_SPREAD = 0.00005      # per-side cost as fraction of price (~0.5 bp)
COOLDOWN_MIN = 60          # don't re-enter the same instrument within N minutes
                           # of its last close (prevents churning one instrument)

_DB = pathlib.Path(__file__).resolve().parent / "dashboard.db"
_LOCK = threading.Lock()


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
               half_spread: float = HALF_SPREAD) -> float:
    cost = entry * half_spread * 2  # entry + exit
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


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_DB, check_same_thread=False)
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
_METALS = {"XAUUSD", "XAGUSD"}                         # priced in USD
_JPY_SHORT = {"USDJPY", "EURJPY", "GBPJPY"}            # long pair = short JPY
_EQUITY = {"SPX", "NDX"}


def _risk_buckets(instrument: str, direction: str) -> list[tuple[str, int]]:
    """The macro bets this trade expresses, as (bucket, +1/-1) pairs.
    A trade can sit in several buckets (USDJPY is both a USD and a JPY bet)."""
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
        return False, ["action is WAIT/WATCH"], ""
    direction = "long" if action == "BUY" else "short"
    det_action = score.signal
    if det_action not in ("BUY", "SELL") or (det_action == "BUY") != (action == "BUY"):
        reasons.append("no confluence: deterministic trend disagrees with action")
    if llm_sig and llm_sig.confidence < CONF_THRESHOLD:
        reasons.append(f"confidence {llm_sig.confidence:.2f} < {CONF_THRESHOLD}")
    if score.strength < MIN_STRENGTH:
        reasons.append(f"trend strength {score.strength} < {MIN_STRENGTH}")
    if VOL_FILTER:
        atr = score.facts.get("atr14") or 0.0
        med = score.facts.get("atr14_med60") or 0.0
        if med > 0 and atr < med:
            reasons.append(f"vol filter: atr14 {atr:.5g} < 60-bar median {med:.5g}")
    return (len(reasons) == 0), reasons, direction


def place_from_state(state: dict) -> list[str]:
    """Create paper trades for every qualifying signal (both SL/TP methods)."""
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
            mlabel = f"ATR rr{rr:.1f}" if method == "ATR" else "STRUCT"
            if _has_open(key, mlabel):
                logs.append(f"{key} {mlabel}: skip (already open)")
                continue
            risk_price = abs(entry - sl)
            size = (ACCOUNT * RISK_PER_TRADE) / risk_price if risk_price > 0 else 0
            f = score.facts
            entry_facts = json.dumps({
                "rsi14": f.get("rsi14"), "atr14": f.get("atr14"),
                "atr14_med60": f.get("atr14_med60"),
                "realized_vol_annual": f.get("realized_vol_annual"),
                "trend": f.get("trend"), "returns": f.get("returns"),
                "support_60": f.get("support_60"), "resistance_60": f.get("resistance_60"),
                "vol_filter_ok": (f.get("atr14") or 0) >= (f.get("atr14_med60") or 0),
            }, default=float)
            _insert(Trade(
                ts=now.isoformat(timespec="seconds"), instrument=key, direction=direction,
                method=mlabel, entry=entry, sl=sl, tp=tp, rr=round(rr_actual, 2),
                size_units=round(size, 2), horizon_end=horizon_end,
                confidence=conf, rationale=rationale, half_spread=hs,
                invalidation=(llm_sig.invalidation if llm_sig else ""),
                llm_bias=(llm_sig.bias if llm_sig else ""),
                det_strength=score.strength, det_note=score.note[:300],
                macro_note=macro[:500], entry_facts=entry_facts))
            msg = (f"{key} {mlabel}: PLACED {direction} entry {entry:.4f} "
                   f"SL {sl:.4f} TP {tp:.4f} (R:R {rr_actual:.2f}, size {size:.1f}, conf {conf:.2f})")
            logs.append(msg)
            log.info("PLACED %s", msg)
            for b in buckets:
                open_by_bucket.setdefault(b, set()).add(key)
    for line in logs:
        if "skip" in line:
            log.debug("funnel %s", line)
    # persist the counterfactuals (rejected candidates) for constraint analysis
    try:
        from . import journal
        journal.record_rejections(rejected)
    except Exception as e:
        log.warning("journal: could not record rejections: %s", e)
    return logs


def _outcome_for(t: dict, get_ohlc_fn):
    """Resolve one open trade. Prefers exact MT5 tick resolution; falls back to
    (conservative) OHLC-bar resolution. Returns outcome tuple, "OPEN", or None."""
    from .instruments import BY_KEY
    from . import mt5_client
    inst = BY_KEY[t["instrument"]]
    # everything in true UTC; MT5 source times get the broker offset removed
    entry_ts = _as_utc(t["ts"])
    end_ts = _as_utc(t["horizon_end"])
    now_utc = pd.Timestamp.now(tz="UTC")
    horizon_passed = now_utc >= end_ts
    offset = store.cache_get("mt5_offset_sec")[0] or 0

    # 1) exact path: MT5 ticks
    if mt5_client.is_available():
        try:
            t0 = entry_ts.to_pydatetime()
            t1 = (end_ts if horizon_passed else now_utc).to_pydatetime()
            ticks = mt5_client.get_ticks_range(inst.mt5, t0, t1)
            if ticks is not None and len(ticks):
                # keep only ticks strictly AFTER entry (tick index is broker time;
                # convert to true UTC before comparing) -- a trade must never be
                # resolved by a tick at or before the instant it was opened.
                true_idx = ticks.index - pd.Timedelta(seconds=offset)
                ticks = ticks[true_idx > entry_ts]
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
        from . import report  # lazy import to avoid a circular import
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
