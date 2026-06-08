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
import sqlite3
import pathlib
import threading

import pandas as pd

from .log import log

# ---- config (tunable defaults) --------------------------------------------
SL_ATR_MULT = 2.0          # stop = 2 x ATR (matches the risk gate)
RR_DEFAULT = 2.0           # take-profit reward:risk for the ATR variant
RR_SWEEP = [1.5, 2.0, 3.0]  # ATR variants we compare
MIN_RR = 1.5               # reject setups whose geometry is worse than this
HORIZON_DAYS = 5           # trade validity (trading days ~ calendar 7d window)
HORIZON_CAL = 7
RISK_PER_TRADE = 0.005     # 0.5% notional risk per trade (for size only)
ACCOUNT = 10_000.0
CONF_THRESHOLD = 0.60      # min LLM confidence (forward only)
MIN_STRENGTH = 3           # min deterministic trend strength
HALF_SPREAD = 0.00005      # per-side cost as fraction of price (~0.5 bp)

_DB = pathlib.Path(__file__).resolve().parent / "dashboard.db"
_LOCK = threading.Lock()


# ---- SL/TP computation -----------------------------------------------------

def compute_sltp(facts: dict, direction: str, method: str, rr: float = RR_DEFAULT):
    """Return (entry, sl, tp, rr_actual) or None if geometry is invalid.
    method: 'ATR' (fixed rr) or 'STRUCT' (support/resistance, rr falls out)."""
    entry = facts["last_price"]
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


def r_multiple(direction: str, entry: float, sl: float, exit_price: float) -> float:
    cost = entry * HALF_SPREAD * 2  # entry + exit
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


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_DB, check_same_thread=False)
    c.execute("""CREATE TABLE IF NOT EXISTS paper_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, instrument TEXT, direction TEXT,
        method TEXT, entry REAL, sl REAL, tp REAL, rr REAL, size_units REAL,
        horizon_end TEXT, confidence REAL, rationale TEXT, status TEXT,
        exit_ts TEXT, exit_price REAL, realized_r REAL)""")
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


def _update_resolution(trade_id: int, status: str, exit_ts: str,
                       exit_price: float, realized_r: float) -> None:
    with _LOCK, _conn() as c:
        c.execute("UPDATE paper_trades SET status=?, exit_ts=?, exit_price=?, realized_r=? "
                  "WHERE id=?", (status, exit_ts, exit_price, realized_r, trade_id))


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
    return (len(reasons) == 0), reasons, direction


def place_from_state(state: dict) -> list[str]:
    """Create paper trades for every qualifying signal (both SL/TP methods)."""
    logs: list[str] = []
    log.info("placement run: evaluating %d instruments", len(state.get("scores", {})))
    now = dt.datetime.now()
    horizon_end = (now + dt.timedelta(days=HORIZON_CAL)).isoformat(timespec="seconds")
    for key, score in state["scores"].items():
        llm_sig = state.get("llm", {}).get(key)
        ok, reasons, direction = evaluate_signal(key, score, llm_sig)
        if not ok:
            logs.append(f"{key}: skip ({'; '.join(reasons)})")
            continue
        conf = llm_sig.confidence if llm_sig else 0.0
        rationale = (llm_sig.rationale if llm_sig else score.note)[:300]
        variants = [("ATR", rr) for rr in [RR_DEFAULT]] + [("STRUCT", None)]
        for method, rr in variants:
            res = compute_sltp(score.facts, direction, method, rr or RR_DEFAULT)
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
            _insert(Trade(
                ts=now.isoformat(timespec="seconds"), instrument=key, direction=direction,
                method=mlabel, entry=entry, sl=sl, tp=tp, rr=round(rr_actual, 2),
                size_units=round(size, 2), horizon_end=horizon_end,
                confidence=conf, rationale=rationale))
            msg = (f"{key} {mlabel}: PLACED {direction} entry {entry:.4f} "
                   f"SL {sl:.4f} TP {tp:.4f} (R:R {rr_actual:.2f}, size {size:.1f}, conf {conf:.2f})")
            logs.append(msg)
            log.info("PLACED %s", msg)
    for line in logs:
        if "skip" in line:
            log.debug("funnel %s", line)
    return logs


def _outcome_for(t: dict, get_ohlc_fn):
    """Resolve one open trade. Prefers exact MT5 tick resolution; falls back to
    (conservative) OHLC-bar resolution. Returns outcome tuple, "OPEN", or None."""
    from .instruments import BY_KEY
    from . import mt5_client
    inst = BY_KEY[t["instrument"]]
    # store ts naive-local; tick history is UTC -> make both UTC-aware
    entry_ts = pd.Timestamp(t["ts"]).tz_localize(None)
    end_ts = pd.Timestamp(t["horizon_end"]).tz_localize(None)
    horizon_passed = pd.Timestamp.now() >= end_ts

    # 1) exact path: MT5 ticks
    if mt5_client.is_available():
        try:
            t0 = entry_ts.to_pydatetime().astimezone(dt.timezone.utc)
            t1 = (end_ts if horizon_passed else pd.Timestamp.now()).to_pydatetime().astimezone(dt.timezone.utc)
            ticks = mt5_client.get_ticks_range(inst.mt5, t0, t1)
            if ticks is not None and len(ticks):
                out = resolve_ticks(t["direction"], t["sl"], t["tp"], ticks)
                log.debug("resolve %s %s via TICKS (%d ticks, exact) -> %s",
                          t["instrument"], t["method"], len(ticks),
                          out[0] if out else "open")
                if out and (out[0] != "EXPIRED" or horizon_passed):
                    return out
                return "OPEN"
        except Exception as e:
            log.debug("tick resolution failed for %s (%s); falling back to OHLC",
                      t["instrument"], e)

    # 2) fallback path: OHLC bars (conservative SL-before-TP)
    ohlc = get_ohlc_fn(inst)
    if ohlc is None:
        log.debug("no OHLC for %s; trade stays open", t["instrument"])
        return None
    idx = ohlc.index.tz_localize(None) if ohlc.index.tz is not None else ohlc.index
    ohlc = ohlc.set_axis(idx)
    bars = ohlc[(ohlc.index > entry_ts) & (ohlc.index <= end_ts)]
    out = resolve(t["direction"], t["entry"], t["sl"], t["tp"], bars)
    log.debug("resolve %s %s via OHLC (%d bars, conservative) -> %s",
              t["instrument"], t["method"], len(bars), out[0] if out else "open")
    if out and (out[0] != "EXPIRED" or horizon_passed):
        return out
    return "OPEN"


def resolve_open(get_ohlc_fn) -> int:
    """Resolve OPEN trades against fresh data. Returns count resolved."""
    resolved = 0
    for t in open_trades():
        out = _outcome_for(t, get_ohlc_fn)
        if out is None or out == "OPEN":
            continue
        status, exit_price, exit_time = out
        r = r_multiple(t["direction"], t["entry"], t["sl"], exit_price)
        _update_resolution(t["id"], status, str(exit_time), exit_price, round(r, 3))
        log.info("RESOLVED %s %s %s  R=%+.2f  entry=%.5f exit=%.5f @ %s",
                 t["instrument"], t["method"], status, r, t["entry"], exit_price, exit_time)
        resolved += 1
    return resolved
