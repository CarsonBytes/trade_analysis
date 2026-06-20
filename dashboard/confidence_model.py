"""Objective confidence from empirical conditional outcomes (Track 1).

Replaces the LLM's self-reported confidence with a MEASURED one: the historical
win-rate and expectancy of trades in the SAME regime, where a regime is
(trend-strength bucket x volatility regime). Outcomes come from replay (5y
deterministic, no look-ahead) plus the live forward journal.

Built offline and persisted to JSON; the live gate just loads it. So the gate
asks "did trades like this one actually make money historically?" instead of
trusting a model's introspection.

Build/refresh:  uv run python -m dashboard.confidence_model --build
Inspect:        uv run python -m dashboard.confidence_model
"""
from __future__ import annotations

from . import net  # noqa: F401

import datetime as dt
import json
import pathlib

from analyst.features import compute_facts
from .instruments import UNIVERSE
from .providers import get_ohlc
from .scoring import score_from_facts
from . import paper
from .replay import _resolve_daily
from .log import log

_MODEL_PATH = pathlib.Path(__file__).resolve().parent / "confidence_model.json"
_model: dict | None = None


def _weekly_ohlc(inst):
    """Max-history WEEKLY OHLC from yfinance (the strategy trades weekly, so the
    model must be trained on weekly bars or its regime edges are mis-scaled)."""
    import warnings
    warnings.filterwarnings("ignore")
    import yfinance as yf
    raw = yf.download(inst.yf, period="max", interval="1wk",
                      progress=False, auto_adjust=True)
    if raw is None or len(raw) == 0:
        return None
    if hasattr(raw.columns, "nlevels") and raw.columns.nlevels > 1:
        raw.columns = raw.columns.get_level_values(0)
    df = raw[["Open", "High", "Low", "Close"]].copy()
    df.columns = ["open", "high", "low", "close"]
    return df.dropna()

MIN_SAMPLES = 20   # a bucket below this is too noisy to gate on (stays neutral)


# ---- regime definition (the conditioning features) -------------------------

def vol_regime(facts: dict) -> str:
    """'high' if current ATR is at/above its 60-bar median, else 'low'. Same
    definition the vol filter uses, so the buckets line up with live trades."""
    atr = facts.get("atr14") or 0.0
    med = facts.get("atr14_med60") or 0.0
    return "high" if (med > 0 and atr >= med) else "low"


def _bucket_key(strength: int, regime: str) -> str:
    return f"s{strength}-{regime}"


# ---- data collection -------------------------------------------------------

def _collect_from_replay(period: str = "5y", rr: float | None = None) -> list[dict]:
    """Walk every instrument's daily history (no look-ahead) and emit one record
    per deterministic BUY/SELL setup: its strength, vol regime, and realized R.
    Unlike replay_variant this does NOT pre-filter on strength/vol -- we want
    every regime so we can measure each one."""
    rr = rr or paper.RR_DEFAULT
    records: list[dict] = []
    for inst in UNIVERSE:
        df = _weekly_ohlc(inst)          # WEEKLY bars -- coherent with the strategy
        if df is None or len(df) < 220:
            continue
        close = df["close"]
        n = len(df)
        i = 160
        while i < n - 1:
            facts, _ = compute_facts(close.iloc[: i + 1], inst.key)
            score = score_from_facts(inst.key, facts, "")
            if score.signal not in ("BUY", "SELL"):
                i += 1
                continue
            direction = "long" if score.signal == "BUY" else "short"
            res = paper.compute_sltp(facts, direction, "ATR", rr)
            if res is None:
                i += 1
                continue
            entry, sl, tp, rr_act = res
            if rr_act < paper.MIN_RR:
                i += 1
                continue
            bars = df.iloc[i + 1: i + 1 + paper.HORIZON_DAYS]
            outcome = _resolve_daily(direction, entry, sl, tp, bars)
            if outcome is None:
                break
            status, exit_px, used = outcome
            r = paper.r_multiple(direction, entry, sl, exit_px)
            records.append({"strength": score.strength,
                            "regime": vol_regime(facts), "r": r})
            i += used + 1
        log.debug("confidence_model: collected %s", inst.key)
    return records


def _collect_from_journal() -> list[dict]:
    """Closed forward trades, with their frozen entry regime."""
    records: list[dict] = []
    for t in paper.all_trades():
        if t["status"] == "OPEN":
            continue
        ef = {}
        try:
            ef = json.loads(t.get("entry_facts") or "{}")
        except Exception:
            pass
        regime = "high" if ef.get("vol_filter_ok") else "low"
        records.append({"strength": t.get("det_strength") or 0,
                        "regime": regime, "r": t["realized_r"]})
    return records


# ---- build / load ----------------------------------------------------------

def build_model(period: str = "5y") -> dict:
    recs = _collect_from_replay(period) + _collect_from_journal()
    buckets: dict[str, list[float]] = {}
    for rec in recs:
        buckets.setdefault(_bucket_key(rec["strength"], rec["regime"]), []).append(rec["r"])
    model = {"built": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
             "period": period, "n_total": len(recs), "buckets": {}}
    for k, rs in sorted(buckets.items()):
        s = paper.stats(rs)
        model["buckets"][k] = {"n": s["n"], "win_rate": round(s["win_rate"], 4),
                               "expectancy_R": round(s["expectancy_R"], 4)}
    _MODEL_PATH.write_text(json.dumps(model, indent=2), encoding="utf-8")
    global _model
    _model = model
    log.info("confidence_model: built %d buckets from %d records",
             len(model["buckets"]), len(recs))
    return model


def load_model() -> dict:
    global _model
    if _model is None:
        try:
            _model = json.loads(_MODEL_PATH.read_text(encoding="utf-8"))
        except Exception:
            _model = {"buckets": {}}
    return _model


def reload_model() -> dict:
    """Force a re-read from disk (e.g. after a rebuild)."""
    global _model
    _model = None
    return load_model()


def lookup(strength: int, regime: str) -> dict | None:
    return load_model().get("buckets", {}).get(_bucket_key(strength, regime))


def objective(score) -> tuple[float, float, int] | None:
    """(win_rate, expectancy_R, n) for this signal's regime, or None if the model
    has no/insufficient data for that bucket."""
    b = lookup(score.strength, vol_regime(score.facts))
    if not b:
        return None
    return b["win_rate"], b["expectancy_R"], b["n"]


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true", help="rebuild from replay + journal")
    ap.add_argument("--period", default="5y")
    args = ap.parse_args()
    model = build_model(args.period) if args.build else load_model()
    if not model.get("buckets"):
        print("No model yet. Run with --build."); return
    print(f"Built {model.get('built','?')} | period {model.get('period','?')} "
          f"| {model.get('n_total','?')} records")
    print(f"{'bucket':<12}{'n':>6}{'win':>7}{'expR':>9}  gate@MIN_EDGE=0")
    for k, b in model["buckets"].items():
        gate = "PASS" if b["expectancy_R"] >= 0 else "block"
        note = gate if b["n"] >= MIN_SAMPLES else f"neutral(n<{MIN_SAMPLES})"
        print(f"{k:<12}{b['n']:>6}{b['win_rate']*100:>6.0f}%{b['expectancy_R']:>9.3f}  {note}")


if __name__ == "__main__":
    main()
