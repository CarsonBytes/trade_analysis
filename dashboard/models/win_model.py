"""Calibrated P(win) model (Track 2) -- the continuous, learned alternative to
the discrete strength/regime buckets.

A logistic regression on CONTINUOUS features outputs a raw score; isotonic
calibration (PAVA) bends it so the number is an honest probability (when it says
0.40, ~40% of those trades actually make money). Trained + validated OUT OF
SAMPLE on the 5y replay (deterministic, no look-ahead).

No sklearn dependency: logistic is fit with scipy L-BFGS, isotonic via a small
PAVA, and the whole model persists to win_model.json (feature means/stds,
weights, bias, calibration map) so inference is pure numpy.

Build/inspect:  uv run python -m dashboard.win_model --build
Honest metrics printed: Brier, log-loss, AUC, and a reliability table -- the
only things that say whether this predicts or just looks clever.
"""
from __future__ import annotations

from dashboard.core import net  # noqa: F401

import datetime as dt
import json
import pathlib

import numpy as np

from analyst.features import compute_facts
from dashboard.instruments import UNIVERSE
from dashboard.data.providers import get_ohlc
from dashboard.core.scoring import score_from_facts
from dashboard.core import paper
from dashboard.research.replay import _resolve_daily
from dashboard.core.log import log

_MODEL_PATH = pathlib.Path(__file__).resolve().parent / "win_model.json"
_model: dict | None = None


def _weekly_ohlc(inst):
    """Max-history WEEKLY OHLC from yfinance (model must match the weekly strategy)."""
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

# continuous features, in fixed order. Magnitude-based (trades are trend-aligned,
# so sign carries no info); each is a quantity that could plausibly move P(win).
FEATURES = ["strength", "abs_tstat", "atr_ratio", "abs_mom20", "rsi_stretch", "rvol"]


def _feature_row(facts: dict, score) -> list[float] | None:
    try:
        atr = facts.get("atr14") or 0.0
        med = facts.get("atr14_med60") or 0.0
        return [
            float(score.strength),
            abs(float(facts.get("trend_tstat") or 0.0)),
            (atr / med) if med > 0 else 1.0,
            abs(float((facts.get("returns") or {}).get("20d") or 0.0)),
            abs(float(facts.get("rsi14") or 50.0) - 50.0),
            float(facts.get("realized_vol_annual") or 0.0),
        ]
    except Exception:
        return None


# ---- numpy logistic + isotonic (no sklearn) --------------------------------

def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def _fit_logistic(X, y, l2=1.0):
    """L2-regularised logistic regression via L-BFGS. Returns (weights, bias)."""
    from scipy.optimize import minimize
    n, d = X.shape

    def nll(w):
        z = X @ w[:-1] + w[-1]
        p = _sigmoid(z)
        eps = 1e-9
        ll = -(y * np.log(p + eps) + (1 - y) * np.log(1 - p + eps)).mean()
        return ll + l2 * (w[:-1] ** 2).sum() / n

    res = minimize(nll, np.zeros(d + 1), method="L-BFGS-B")
    return res.x[:-1], float(res.x[-1])


def _isotonic(x, y):
    """Pool-Adjacent-Violators: monotone non-decreasing fit of y on sorted x.
    Returns (x_thresholds, y_values) defining a step/interp calibration map."""
    order = np.argsort(x)
    xs = x[order].astype(float)
    ys = y[order].astype(float)
    n = len(ys)
    # PAVA: merge adjacent blocks that violate monotonicity
    vals = ys.copy()
    wts = np.ones(n)
    idx = list(range(n))  # block boundaries as a stack of (value, weight, x_right)
    blocks = [[ys[i], 1.0, xs[i]] for i in range(n)]
    merged = []
    for b in blocks:
        merged.append(b)
        while len(merged) > 1 and merged[-2][0] >= merged[-1][0]:
            v2, w2, x2 = merged.pop()
            v1, w1, x1 = merged.pop()
            merged.append([(v1 * w1 + v2 * w2) / (w1 + w2), w1 + w2, x2])
    cx, cy = [], []
    for v, w, xr in merged:
        cx.append(xr)
        cy.append(v)
    return np.array(cx), np.array(cy)


def _calibrate(raw_p, y):
    return _isotonic(raw_p, y)


def _apply_iso(iso_x, iso_y, p):
    return np.interp(p, iso_x, iso_y, left=iso_y[0], right=iso_y[-1])


# ---- metrics ---------------------------------------------------------------

def _auc(y, p):
    pos, neg = p[y == 1], p[y == 0]
    if len(pos) == 0 or len(neg) == 0:
        return 0.5
    order = np.argsort(p, kind="mergesort")
    ranks = np.empty(len(p)); ranks[order] = np.arange(1, len(p) + 1)
    return float((ranks[y == 1].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def _brier(y, p):
    return float(((p - y) ** 2).mean())


def _logloss(y, p):
    eps = 1e-9
    return float(-(y * np.log(p + eps) + (1 - y) * np.log(1 - p + eps)).mean())


def _reliability(y, p, bins=10):
    out = []
    edges = np.linspace(0, 1, bins + 1)
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (p >= lo) & (p < hi if hi < 1 else p <= hi)
        if m.sum() >= 5:
            out.append((f"{lo:.1f}-{hi:.1f}", int(m.sum()),
                        float(p[m].mean()), float(y[m].mean())))
    return out


# ---- data collection (replay walk) -----------------------------------------

def _collect(period="5y", rr=None):
    """Per setup: (features, outcome=1 if realized R>0, instrument, time_pos)."""
    rr = rr or paper.RR_DEFAULT
    rows = []
    for inst in UNIVERSE:
        df = _weekly_ohlc(inst)          # WEEKLY bars -- coherent with the strategy
        if df is None or len(df) < 220:
            continue
        close = df["close"]; n = len(df); i = 160
        while i < n - 1:
            facts, _ = compute_facts(close.iloc[: i + 1], inst.key)
            score = score_from_facts(inst.key, facts, "")
            if score.signal not in ("BUY", "SELL"):
                i += 1; continue
            direction = "long" if score.signal == "BUY" else "short"
            res = paper.compute_sltp(facts, direction, "ATR", rr)
            if res is None:
                i += 1; continue
            entry, sl, tp, rr_act = res
            if rr_act < paper.MIN_RR:
                i += 1; continue
            fr = _feature_row(facts, score)
            bars = df.iloc[i + 1: i + 1 + paper.HORIZON_DAYS]
            outcome = _resolve_daily(direction, entry, sl, tp, bars)
            if outcome is None:
                break
            status, exit_px, used = outcome
            r = paper.r_multiple(direction, entry, sl, exit_px)
            if fr is not None:
                rows.append((fr, 1 if r > 0 else 0, inst.key, i))
            i += used + 1
    return rows


# ---- build / load / predict ------------------------------------------------

def build_model(period="5y") -> dict:
    rows = _collect(period)
    if len(rows) < 200:
        raise RuntimeError(f"too few samples ({len(rows)}) to fit a model")
    # chronological split PER instrument -> pool (train 60 / calib 20 / test 20)
    by_inst: dict[str, list] = {}
    for r in rows:
        by_inst.setdefault(r[2], []).append(r)
    tr, ca, te = [], [], []
    for key, rs in by_inst.items():
        rs.sort(key=lambda x: x[3])
        a, b = int(len(rs) * 0.6), int(len(rs) * 0.8)
        tr += rs[:a]; ca += rs[a:b]; te += rs[b:]

    def mat(rows):
        X = np.array([r[0] for r in rows], dtype=float)
        y = np.array([r[1] for r in rows], dtype=float)
        return X, y

    Xtr, ytr = mat(tr); Xca, yca = mat(ca); Xte, yte = mat(te)
    mean = Xtr.mean(axis=0); std = Xtr.std(axis=0); std[std == 0] = 1.0
    w, b = _fit_logistic((Xtr - mean) / std, ytr)
    # raw probabilities, then isotonic calibration fit on the calib split
    raw_ca = _sigmoid(((Xca - mean) / std) @ w + b)
    iso_x, iso_y = _calibrate(raw_ca, yca)
    # evaluate on the untouched test split
    raw_te = _sigmoid(((Xte - mean) / std) @ w + b)
    cal_te = _apply_iso(iso_x, iso_y, raw_te)

    model = {
        "built": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "features": FEATURES, "mean": mean.tolist(), "std": std.tolist(),
        "weights": w.tolist(), "bias": b,
        "iso_x": iso_x.tolist(), "iso_y": iso_y.tolist(),
        "n_train": len(tr), "n_test": len(te),
        "metrics": {
            "brier": _brier(yte, cal_te),
            "brier_uncal": _brier(yte, raw_te),
            "logloss": _logloss(yte, cal_te),
            "auc": _auc(yte, cal_te),
            "base_rate": float(yte.mean()),
        },
        "reliability": _reliability(yte, cal_te),
        "coef": dict(zip(FEATURES, w.tolist())),
    }
    _MODEL_PATH.write_text(json.dumps(model, indent=2), encoding="utf-8")
    global _model; _model = model
    log.info("win_model: built on %d train / %d test, AUC %.3f Brier %.4f",
             len(tr), len(te), model["metrics"]["auc"], model["metrics"]["brier"])
    return model


def load_model() -> dict | None:
    global _model
    if _model is None:
        try:
            _model = json.loads(_MODEL_PATH.read_text(encoding="utf-8"))
        except Exception:
            _model = None
    return _model


def p_win(facts: dict, score) -> float | None:
    """Calibrated probability this trade closes positive, or None if no model."""
    m = load_model()
    fr = _feature_row(facts, score)
    if not m or fr is None:
        return None
    x = (np.array(fr) - np.array(m["mean"])) / np.array(m["std"])
    raw = _sigmoid(float(x @ np.array(m["weights"]) + m["bias"]))
    return float(_apply_iso(np.array(m["iso_x"]), np.array(m["iso_y"]), raw))


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--period", default="5y")
    args = ap.parse_args()
    m = build_model(args.period) if args.build else load_model()
    if not m:
        print("No model. Run with --build."); return
    met = m["metrics"]
    print(f"Built {m['built']} | train {m['n_train']} / test {m['n_test']}")
    print(f"\nOUT-OF-SAMPLE metrics:")
    print(f"  AUC        {met['auc']:.3f}   (0.5 = coin flip; >0.55 = some signal)")
    print(f"  Brier      {met['brier']:.4f}  (lower better; uncal {met['brier_uncal']:.4f})")
    print(f"  Log-loss   {met['logloss']:.4f}")
    print(f"  base win%  {met['base_rate']:.1%}  (predict-the-average baseline)")
    print(f"\nFeature weights (standardised; sign = direction of effect on P win):")
    for f, c in m["coef"].items():
        print(f"  {f:<12} {c:+.3f}")
    print(f"\nReliability (calibrated): predicted vs ACTUAL win rate per bin")
    print(f"  {'bin':<10}{'n':>5}{'pred':>8}{'actual':>8}")
    for b, n, pred, act in m["reliability"]:
        print(f"  {b:<10}{n:>5}{pred:>8.2f}{act:>8.2f}")


if __name__ == "__main__":
    main()
