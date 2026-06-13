"""Widened-universe validation + penalized re-search (run after adding the
2026-06 instruments).

Two honest tests:
  A. The CURRENT live config (rr3.0, SL 1.5xATR, strength 5, vol filter) on the
     NEW instruments only. Their history played no part in choosing the config,
     so this is genuine out-of-sample evidence about the strategy itself.
  B. A small penalized grid (rr x sl x strength, vol filter ON) over the full
     14-instrument universe with a 60/40 IS/OOS split. Selection by IS
     expectancy, judged by OOS expectancy + DSR deflated for every config tried.

Resumable: replay_variant checkpoints every walk to replay_cache.json.

Run:  uv run python -u -m dashboard.wide_search
"""
from __future__ import annotations

import itertools
import pathlib

import pandas as pd

from metrics import deflated_sharpe_ratio
from .instruments import UNIVERSE
from .providers import get_ohlc
from . import paper
from .replay import replay_variant

NEW_KEYS = {"XAGUSD", "USDCHF", "NZDUSD", "EURJPY", "GBPJPY", "SPX", "NDX"}
RR_GRID = [3.0, 4.0]
SL_GRID = [1.5, 2.0]
STR_GRID = [4, 5]
VOL_WIN = 60
SPLIT = 0.6
DATA_PATH = pathlib.Path(__file__).resolve().parent / "replay_data_5y_wide.pkl"


def _exp(rs: list[float]) -> float:
    return sum(rs) / len(rs) if rs else 0.0


def main() -> None:
    if DATA_PATH.exists():
        data = pd.read_pickle(DATA_PATH)
        print(f"(resuming with frozen dataset {DATA_PATH.name})")
    else:
        data = {}
        for inst in UNIVERSE:
            df = get_ohlc(inst, period="5y", interval="1d")
            if df is not None and len(df) > 300:
                data[inst.key] = df
            else:
                print(f"  !! no usable 5y daily data for {inst.key}, excluded")
        pd.to_pickle(data, DATA_PATH)
    print(f"{len(data)} instruments with data\n")

    # --- Test A: live config on instruments it was never tuned on -----------
    paper.SL_ATR_MULT, paper.MIN_STRENGTH = 1.5, 5
    new_r: list[float] = []
    for key in sorted(NEW_KEYS & set(data)):
        new_r += replay_variant(data[key], key, "ATR", 3.0, None, VOL_WIN)
    dsr = deflated_sharpe_ratio(pd.Series(new_r), n_trials=1) if new_r else 0.0
    print(f"\nA. live config on NEW instruments only (pure OOS): "
          f"n={len(new_r)} expR={_exp(new_r):+.3f} DSR={dsr:.0%}\n")

    # --- Test B: penalized grid, vol filter ON, full universe ----------------
    configs = list(itertools.product(RR_GRID, SL_GRID, STR_GRID))
    rows = []
    for rr, slm, ms in configs:
        paper.SL_ATR_MULT, paper.MIN_STRENGTH = slm, ms
        is_r, oos_r = [], []
        for key, df in data.items():
            cut = int(len(df) * SPLIT)
            is_r += replay_variant(df.iloc[:cut], key, "ATR", rr, None, VOL_WIN)
            oos_r += replay_variant(df.iloc[cut:], key, "ATR", rr, None, VOL_WIN)
        rows.append({"rr": rr, "sl": slm, "str": ms, "is": _exp(is_r),
                     "oos": _exp(oos_r), "n_oos": len(oos_r), "oos_r": oos_r})
        print(f"  config rr{rr} sl{slm} str{ms}: IS {_exp(is_r):+.3f} "
              f"OOS {_exp(oos_r):+.3f} (n={len(oos_r)})", flush=True)

    print(f"\nB. grid over {len(configs)} configs (vol filter ON), "
          f"by IS expectancy:")
    print(f"{'rr':>5}{'sl':>5}{'str':>5}{'IS_expR':>9}{'OOS_expR':>10}"
          f"{'OOS_n':>7}{'OOS_DSR':>9}")
    for r in sorted(rows, key=lambda r: r["is"], reverse=True):
        d = deflated_sharpe_ratio(pd.Series(r["oos_r"]), n_trials=len(configs)) \
            if r["oos_r"] else 0.0
        print(f"{r['rr']:>5}{r['sl']:>5}{r['str']:>5}{r['is']:>9.3f}"
              f"{r['oos']:>10.3f}{r['n_oos']:>7}{d:>9.0%}")
    print("\nAdoption rule unchanged: a config replaces the live one only if "
          "it beats it on OOS expectancy AND DSR, judged on the IS-best pick.")


if __name__ == "__main__":
    main()
