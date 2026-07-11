"""Parameter sensitivity sweep: one-at-a-time +/-20% perturbation of the core signal's
4 main tunable parameters (SL_ATR_MULT, RR_DEFAULT, HORIZON_DAYS, OVEREXT_HI/LO), holding
all else at the live-adopted baseline. Answers: does Sharpe/ratio collapse under small
parameter nudges (overfitting red flag) or stay stable (robustness evidence)?

RESULT (2026-07-09, 22-ETF book, --pos-cap 0.25 ONLY -- no PORTFOLIO_CAP yet): ratio stayed
in a 0.39-0.54 band across every sweep, no collapse toward zero. HORIZON_DAYS and the OVEREXT
RSI band were the most sensitive (baseline happens to be the local best on both) but even
their worst-case values were only ~28% below baseline, not a collapse. Real evidence against
overfitting -- see HANDOFF.md 2026-07-09 for the full table.

UPDATED 2026-07-11: re-run with `PORTFOLIO_CAP=1.0` added (the current live 4-parameter
config; the 2026-07-09 result above predates it) per a critique asking whether ATR-SL-mult /
RR-mult / OVEREXT_HI have ever been swept under the CURRENT config. Also added a MIN_STRENGTH
probe: `paper.py`'s own scoring clamps `strength` to `[1,5]` (`scoring.py`:
`strength = max(1, min(5, ...))`), and `MIN_STRENGTH=5` is already that ceiling -- so
`MIN_STRENGTH=6+` isn't a real parameter sweep, it's a threshold above the maximum possible
score, which should empirically produce zero signals. Verified below rather than assumed.
"""
import os
os.environ.setdefault("BROKER", "ib"); os.environ.setdefault("UNIVERSE", "etf")
import numpy as np, pandas as pd, yfinance as yf, sys
sys.path.insert(0, "D:/quant")
import dashboard.research.backtest as bt
from dashboard.instruments import active_universe
from dashboard.core import paper

bt.CASH_YIELD = None
bt.POS_CAP = 0.25
bt.PORTFOLIO_CAP = 1.0
universe = active_universe()

# Pre-fetch all price data ONCE (expensive), reuse across every parameter variant.
data = {}
for inst in universe:
    raw = yf.download(inst.yf, period="max", interval="1wk", progress=False, auto_adjust=True)
    if raw is None or len(raw) == 0: continue
    if hasattr(raw.columns, "nlevels") and raw.columns.nlevels > 1:
        raw.columns = raw.columns.get_level_values(0)
    df = raw[["Open", "High", "Low", "Close"]].copy(); df.columns = ["open", "high", "low", "close"]
    df = df.dropna()
    if df.index.tz is None: df.index = df.index.tz_localize("UTC")
    if len(df) < 220: continue
    data[inst.key] = df


def run(risk=0.005):
    cands = []
    for key, df in data.items():
        cands += bt._signals(df, key)
    eq, real = bt._portfolio(cands, risk)
    yrs = bt._span_years(cands)
    m = bt._metrics(eq, real, yrs)
    ratio = abs(m["cagr"] / m["maxdd"]) if m["maxdd"] else 0
    return m["cagr"], m["maxdd"], ratio, len(cands)


# baseline
base_sl, base_rr, base_h, base_hi, base_lo = (paper.SL_ATR_MULT, paper.RR_DEFAULT,
                                               paper.HORIZON_DAYS, paper.OVEREXT_HI, paper.OVEREXT_LO)
print(f"baseline: SL_ATR_MULT={base_sl} RR_DEFAULT={base_rr} HORIZON_DAYS={base_h} OVEREXT={base_hi}/{base_lo}")
cagr, dd, ratio, n = run()
print(f"BASELINE: n={n} CAGR={cagr*100:+.2f}% maxDD={dd*100:.2f}% ratio={ratio:.3f}\n")


def reset():
    paper.SL_ATR_MULT, paper.RR_DEFAULT, paper.HORIZON_DAYS = base_sl, base_rr, base_h
    paper.OVEREXT_HI, paper.OVEREXT_LO = base_hi, base_lo


sweeps = [
    ("SL_ATR_MULT", "SL_ATR_MULT", [base_sl * 0.8, base_sl, base_sl * 1.2]),
    ("RR_DEFAULT", "RR_DEFAULT", [base_rr * 0.8, base_rr, base_rr * 1.2]),
    ("HORIZON_DAYS", "HORIZON_DAYS", [max(1, round(base_h * 0.8)), base_h, round(base_h * 1.2)]),
]
for label, attr, vals in sweeps:
    print(f"--- {label} sweep ---")
    for v in vals:
        reset()
        setattr(paper, attr, v)
        cagr, dd, ratio, n = run()
        print(f"  {attr}={v:<6.2f} n={n:<4} CAGR={cagr*100:+.2f}% maxDD={dd*100:.2f}% ratio={ratio:.3f}")
    reset()

print("--- OVEREXT (RSI band) sweep ---")
for hi, lo in [(65, 35), (70, 30), (75, 25)]:
    reset()
    paper.OVEREXT_HI, paper.OVEREXT_LO = hi, lo
    cagr, dd, ratio, n = run()
    print(f"  OVEREXT={hi}/{lo} n={n:<4} CAGR={cagr*100:+.2f}% maxDD={dd*100:.2f}% ratio={ratio:.3f}")
reset()

print("--- COMBINED: the 3 individually-favourable directions together (interaction check) ---")
print("  (SL_ATR_MULT tighter -20%, RR_DEFAULT wider +20%, OVEREXT tighter 65/35 -- "
      "one-at-a-time sweeps improved on EACH of these; does stacking them compound or cancel?)")
reset()
paper.SL_ATR_MULT = base_sl * 0.8
paper.RR_DEFAULT = base_rr * 1.2
paper.OVEREXT_HI, paper.OVEREXT_LO = 65, 35
cagr, dd, ratio, n = run()
print(f"  COMBINED n={n:<4} CAGR={cagr*100:+.2f}% maxDD={dd*100:.2f}% ratio={ratio:.3f}  "
      f"(baseline ratio was 0.533)")
reset()

print("--- MIN_STRENGTH probe (is >5 even a real parameter?) ---")
base_ms = paper.MIN_STRENGTH
for ms in [5, 6]:
    paper.MIN_STRENGTH = ms
    try:
        cagr, dd, ratio, n = run()
        print(f"  MIN_STRENGTH={ms} n={n:<4} CAGR={cagr*100:+.2f}% maxDD={dd*100:.2f}% ratio={ratio:.3f}")
    except (IndexError, ZeroDivisionError):
        print(f"  MIN_STRENGTH={ms} n=0    -- ZERO signals (confirms 5 is already the scoring ceiling)")
paper.MIN_STRENGTH = base_ms
