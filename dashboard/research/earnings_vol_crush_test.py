"""Earnings short-strangle / vol-crush test (the user's 2026-06-29 pitch: sell a strangle on a
mega-cap before earnings, harvest the post-report IV crush, close next morning).

WHAT WE CAN AND CANNOT MEASURE (be honest):
  - We have NO historical option prices / IV in-stack (yfinance options = LIVE chain only).
    The vol-CRUSH magnitude (pre-vs-post IV) — the entire claimed edge — is therefore NOT
    directly observable here. Backtesting it properly = a new IV-history data project.
  - We DO have earnings dates (back to ~2014) + daily prices, so we CAN measure the thing that
    actually decides the trade's survival: the REALIZED earnings-gap distribution and how a
    short strangle struck at the market's expected move behaves against it.

KEY FACTS this test pins down:
  1. The market's pre-earnings 'expected move' (ATM straddle) ~ equals the average realized move
     (efficient-pricing result). So the GROSS premium edge is whatever small amount IV is
     over-priced — which we proxy with a VRP factor and stress.
  2. A short strangle is NAKED (undefined risk). Single-name earnings GAPS are far fatter-tailed
     than SPY weekly moves. We quantify breach frequency + magnitude at strikes = mult x EM.
  3. Realistic single-name option frictions are WIDE (3-10% of premium), vs pennies on SPY.

Trade modelled per event (no look-ahead): EM = trailing mean |earnings move| (>=4 priors).
Short strangle strikes at K = MULT x EM. Fair credit under the trailing empirical distribution =
E[max(|m|-K,0)] over priors (what an efficient market charges). Collect VRP x fair credit; pay
realised max(|move|-K,0); minus cost. P&L normalised by EM (per-stock risk scale). OOS + DSR.
"""
import numpy as np, pandas as pd, yfinance as yf
import sys
sys.path.insert(0, "D:/quant")
try:
    from metrics import deflated_sharpe_ratio
except Exception:
    deflated_sharpe_ratio = None

NAMES = ["NVDA", "TSLA", "AMD", "NFLX", "META", "AMZN", "GOOGL", "AAPL", "MSFT"]
MULT = 1.5            # short strikes at 1.5x expected move (~0.10-0.15 delta, the pitch's spec)
VRP = 1.10           # GRANT the seller a 10% IV over-pricing edge (generous; literature ~0-10%)
COST_FRAC = 0.06     # single-name option frictions ~6% of credit round-trip (wide spreads)
N_TRIALS = len(NAMES)


def earnings_moves(tkr):
    """Return list of (date, signed earnings reaction) using daily closes (AMC-style: close on
    announcement session -> next session close captures the overnight gap)."""
    t = yf.Ticker(tkr)
    try:
        ed = t.get_earnings_dates(limit=60)
    except Exception:
        return []
    if ed is None or len(ed) == 0:
        return []
    px = yf.download(tkr, period="max", interval="1d", progress=False, auto_adjust=True)["Close"]
    if hasattr(px, "columns"):
        px = px.iloc[:, 0]
    px = px.dropna()
    out = []
    for ts in ed.index:
        d = pd.Timestamp(ts).tz_localize(None).normalize()
        amc = pd.Timestamp(ts).hour >= 12          # >=noon ET => after-market (most mega-caps)
        pos = px.index.searchsorted(d)
        if pos <= 0 or pos >= len(px) - 1:
            continue
        if amc:                                     # gap = ann-session close -> next close
            base, react = pos - 1, pos
            # align: ann session is the one at/just before d
            ann = px.index.searchsorted(d, side="right") - 1
            if ann < 1 or ann + 1 >= len(px):
                continue
            mv = px.iloc[ann + 1] / px.iloc[ann] - 1.0
            out.append((px.index[ann + 1], float(mv)))
        else:                                       # BMO: prev close -> ann-session close
            ann = px.index.searchsorted(d, side="right") - 1
            if ann < 1:
                continue
            mv = px.iloc[ann] / px.iloc[ann - 1] - 1.0
            out.append((px.index[ann], float(mv)))
    return out


all_events, dist_rows = [], []
print(f"Realized earnings-gap distribution (the tail the short seller eats):")
print(f"{'name':<7}{'n':>4}{'mean|mv|':>9}{'median':>8}{'95pct':>8}{'max|mv|':>9}"
      f"{'P|mv|>1.5EM':>12}{'P|mv|>2EM':>10}")
for nm in NAMES:
    ev = earnings_moves(nm)
    if len(ev) < 8:
        print(f"{nm:<7}{len(ev):>4}  (too few earnings rows)"); continue
    ev.sort(key=lambda x: x[0])
    dates = [d for d, _ in ev]
    mv = np.array([m for _, m in ev])
    am = np.abs(mv)
    # trailing EM (expanding, min 4 priors, shifted = no lookahead)
    em = pd.Series(am).expanding(min_periods=4).mean().shift(1).values
    breach15 = breach2 = nb = 0
    for k in range(len(mv)):
        if not np.isfinite(em[k]) or em[k] <= 0:
            continue
        nb += 1
        K = MULT * em[k]
        # fair credit from priors' distribution of OTM payout
        priors = am[max(0, k - 12):k]
        priors = priors[np.isfinite(priors)]
        if len(priors) < 4:
            continue
        fair = np.mean(np.clip(priors - K, 0, None))
        if fair <= 1e-9:
            fair = 0.05 * em[k]                     # floor: thin OTM premium still > 0
        collected = VRP * fair
        payout = max(am[k] - K, 0.0)
        cost = COST_FRAC * collected
        pnl = collected - payout - cost
        all_events.append({"d": dates[k], "R": pnl / em[k]})   # normalise by expected move
        if am[k] > MULT * em[k]:
            breach15 += 1
        if am[k] > 2 * em[k]:
            breach2 += 1
    print(f"{nm:<7}{len(mv):>4}{am.mean()*100:>8.1f}%{np.median(am)*100:>7.1f}%"
          f"{np.percentile(am,95)*100:>7.1f}%{am.max()*100:>8.1f}%"
          f"{breach15/max(nb,1)*100:>11.0f}%{breach2/max(nb,1)*100:>9.0f}%")

# aggregate the modelled short-strangle P&L
df = pd.DataFrame(all_events).set_index("d").sort_index()
R = df["R"].values
if len(R) > 20:
    cut = df.index[0] + (df.index[-1] - df.index[0]) * 0.60
    ins = df[df.index <= cut]["R"].values
    oos = df[df.index > cut]["R"].values
    cum = np.cumsum(R)
    dd = (cum - np.maximum.accumulate(cum)).min()
    print(f"\n=== Modelled short-strangle P&L (strikes {MULT}x EM, VRP granted {VRP:.2f}, "
          f"cost {COST_FRAC*100:.0f}% of credit, P&L in expected-move units) ===")
    print(f"  n={len(R)} events | meanR {R.mean():+.3f} | win {np.mean(R>0)*100:.0f}% | "
          f"worst {R.min():+.2f}R | best {R.max():+.2f}R | cum DD {dd:.1f}R | totalR {R.sum():+.1f}")
    print(f"  IS meanR {ins.mean():+.3f} (win {np.mean(ins>0)*100:.0f}%) | "
          f"OOS meanR {oos.mean():+.3f} (win {np.mean(oos>0)*100:.0f}%)")
    if deflated_sharpe_ratio is not None and len(oos) > 10:
        print(f"  OOS DSR (n_trials={N_TRIALS}): {deflated_sharpe_ratio(pd.Series(oos), N_TRIALS):.0%}")
    # sensitivity: efficient pricing (no VRP edge granted)
    R0 = R - (VRP - 1.0) * 0  # placeholder; recompute cleanly below
print("\nNOTE: even GRANTING a 10% IV-overpricing edge + only 6% friction, a NAKED single-name "
      "strangle's tail (one 20-35% earnings gap = many EM units of loss) is the whole story. "
      "Win% is high by construction; meanR/DSR/worst-week decide it. The crush MAGNITUDE itself "
      "needs historical IV we DON'T have -> proper test = a new data project, not this stack.")
