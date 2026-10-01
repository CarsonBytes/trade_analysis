# Dynamic cash-shield spec — park the buffer in SGOV, not in idle cash

**Status:** PROPOSAL (no code changed). 2026-10-01. Builds on `IBKR_EXECUTION_SPEC.md`
(S1 order-confirmation + S2 TIF fix), which this depends on — see §6.

**User ask:** *"utilize SGOV rather than cash buffer as possible if no pending trades are
occupying the money, how about dynamically adjusting?"*

**Today.** `CASH_SWEEP_TARGET = 0.80` is a **static** fraction of idle capital: 80% into
SGOV, 20% stays as cash forever, regardless of whether anything is waiting to be bought.
Live is under-parked for a second reason (its orders are discarded, RC-2 in the execution
spec), but even a working sweep would idle ~$5k of NAV at 80% on a $32k account.

**Proposal.** Replace the static fraction with a **reservation**: hold back exactly what the
pipeline needs (pending entries + unfunded signals) plus a small operational float, and park
everything else in SGOV. When a new signal needs money, sell SGOV to raise it. At rest with
nothing pending, the cash buffer collapses to the float and SGOV holds ~all idle capital.

---

## 0. Priority: performance is the objective, safe order placement is the gate

**Read this section before tuning anything in this doc.** An earlier draft of the discussion
risked optimizing the wrong quantity: float size and sweep windows look like decisions, but on
live they are worth **~$147/year combined**:

| lever | value/yr on live ($32.4k NAV) |
|---|---|
| float `3%/$1,500` → `2%/$1,000` | ~$21 (the absolute floors bind, not the percentages) |
| sweep window 10:00-15:30 ET → full RTH | ~$126 (0.66pp more capital-days on ~$19k idle at 4.3%) |
| **deploying the $26,404 currently idle under PORTFOLIO_CAP** | **~$1,200 expected** (strategy ~8.9% on deployed vs SGOV 4.3%) |

The deployment gap is ~8x the shield tuning, and it is not blocked by cash at all — it is
blocked by order placement (RC-1/RC-2, `IBKR_EXECUTION_SPEC.md`): live is running **$6,020
deployed against $32,425 NAV (18.6%)** while `PORTFOLIO_CAP=1.0` leaves the rest unused. Parking
cash in SGOV does not consume strategy room — `_strategy_deployed_usd()` excludes SGOV
explicitly (ib_exec.py:524) — so **the shield and the strategy are not competing for the same
dollars.** Sequence the work accordingly and do not let shield work delay order-reliability
work.

**The honest limit on "max performance."** Live's journal reads 1 WIN / 17 LOSS, but most LOSSes
carry `signal only -- never funded` — the *paper* side resolving on price data with nothing
filled at the broker. That is neither 17 real losses nor evidence of a real edge; it says the
live track record is simply unmeasured. Max performance is not a well-defined target until a
few dozen trades have genuinely filled. Until then the correct objective is **make performance
measurable and stop leaking orders**, in that order.

**Interim option worth weighing (RC-2 unresolved).** IBKR pays tiered interest on USD cash
balances (~2-4% at this size) — below SGOV's ~4.3%, but with **zero sell friction, zero recall
latency, and no dependence on the order path that is currently discarding orders**. Parking in
IBKR cash until S1/S2 land is a defensible interim state, not a regression.

**Net effect on the parameters in this doc:** because deployment outranks the shield, the float
is **not** tightened prematurely (§8 Q1). A larger idle buffer costs ~$21/yr of yield and buys
robustness while the order path is still unproven — cheap insurance. Revisit once live has real
fills.

## 1. Why this is safe (and why it beats a bigger static buffer)

- **SGOV is cash with a yield.** 0-3 month T-bills, ~$100 AUM per share, enormous volume,
  sub-tick spreads, and it trades all session. Unwinding for a few thousand dollars is a
  market sell that fills in seconds — it is not a "raise cash, wait for settlement" problem,
  *provided the account is a margin account*. (Live buying power is ~$100k+ on ~$32k NAV, so
  it is. §6 Q2 asks for an explicit runtime assertion rather than my read of it.)
- **The buffer exists for operational friction, not for opportunity cost.** With no pending
  order, idle cash earns ~0 while SGOV earns the T-bill yield (~4%+). With a pending order,
  the money is committed either way — holding it as cash earns nothing. So the reservation
  should track *actual commitments*, not a percentage of the account.
- **The strategy does not need same-day precision.** Entries are gated to
  10:00-15:30 ET and driven by daily/weekly-bar signals (`within_entry_execution_window()`'s
  own docstring: holding an identified entry for a few hours costs nothing strategically). So
  a recall that takes minutes is free; a permanently idle 20% is not.

## 2. The formula

```
deployed_usd  = _strategy_deployed_usd(ib)              # filled strategy longs (excl. SGOV)
pending_usd   = pending entry notional at broker         # _pending_entry_notional_usd()
              + expected notional of journal-OPEN trades with no ib_mirror row yet
float_usd     = max(FLOAT_MIN_USD, equity_usd * FLOAT_PCT)
reserve_usd   = min(pending_usd + float_usd, investable_usd)

investable_usd = cash_usd + sgov_usd - withdraw_reserve_usd     # as today
sgov_target_usd = max(0, investable_usd - reserve_usd)
delta_usd       = sgov_target_usd - sgov_usd                    # >0 buy, <0 sell SGOV
```

Defaults to tune (Q1): `FLOAT_PCT = 0.03`, `FLOAT_MIN_USD = 1_500`. The float covers
commission/rounding, partial fills, and a day of margin interest; it is deliberately larger
than the existing anti-churn `CASH_SWEEP_MIN_USD` so a normal-size recall is never blocked
by the churn guard.

**Worked example on live right now** (NAV $32,424, cash $20,291, SGOV $6,024, deployed $6,020):

| state | reserve | SGOV target | action |
|---|---|---|---|
| nothing pending | $1,500 | $24,815 (76.5% of NAV) | buy ~187 sh |
| one DIA entry pending ($7.7k) | $9,200 | $17,115 | sell ~76 sh to fund |
| full cap deployed (room 0) | pending only | $0 + float | unwind to float |

Note the middle row: **the sweep funds the trade by selling SGOV, not by refusing the trade.**
That is the behaviour change the user is asking for.

## 3. Two-tier execution — a recall is not a sweep

A surplus rebalance and a funding recall have opposite urgencies, so they must not share one
code path with one cadence:

- **Surplus sweep** (existing `sweep_cash()`, unchanged cadence ~85s, subject to
  `within_entry_execution_window()`): park cash above the reserve. Cheap to be late.
- **Cash recall** (new `ensure_cash_usd(needed_usd)`, called by `mirror_new()` *before*
  sizing/placement): if `cash_usd < needed_usd`, sell SGOV for the shortfall **now**, confirm
  the fill (S1), then place the bracket. Returns the cash actually raised so the caller can
  size against reality. Never sells more SGOV than is held; never sells below the float.
- Precedence inside one cycle: `recall (if needed) → sweep surplus`. On the entry path the
  recall runs inline, so the sweep does not need to be fast.

## 4. Hysteresis (prevent churn and oscillation)

Signals can arrive in bursts, so a naive target flips the shield every cycle. Rules:

- **Min rebalance delta**: buy/sell only when `|delta_usd| >= max(1_500, 1.5% of NAV)`.
- **One-way cooldown** stays at 300s, plus a **sell-side cool-off**: after selling SGOV for
  a recall, don't re-buy for 30 min (the pending commitment will have resolved or been
  ghost-cancelled by then, and the next sweep recomputes).
- **Session guard**: no SGOV buys in the last 30 min before the close (15:00 ET) if any
  journal-OPEN unfunded trade exists — otherwise we'd park money we are likely to need at
  tomorrow's open, paying two spreads for nothing.
- **Weekend/holiday**: with no pending trades the reserve is float-only, so Friday's close
  parks everything. That is intended, not an edge case.

## 5. Boundary conditions (each one is a real hazard in this codebase)

1. **Ghost/unfunded journal trades.** An entry can sit unfunded (RC-1/RC-2). Reserving for it
   parks nothing and blocks a buy forever. Cap the reservation with a **reservation expiry**:
   a journal-OPEN trade with no mirror row stops reserving after `RESERVE_TTL_MIN` (**90 min** —
   see Q5 in §8; long enough to outlast a full 10:00-15:30 ET entry window plus the 30-min
   `GHOST_ENTRY_GRACE_MIN`) or once `GHOST_ENTRY_GRACE_MIN` has cancelled it. Reserved-but-unfunded
   capital is released, not stranded.
2. **Margin vs cash account.** On a cash account, SGOV sale proceeds are T+1 and the recall
   cannot fund a same-day entry — the whole design breaks. Live is margin (`BuyingPower/NL ≈
   6.4x`). Guard on the *affordability* test rather than account type (§8 Q2): require
   `AvailableFunds + SGOV proceeds ≥ entry notional + Σ open entry commitments`; if it fails,
   treat the account as cash-like, fall back to the current static target, and log why. A
   `BuyingPower/NetLiq < 1.2` read stays as a warning-only signal.
3. **`keep_cash_usd` (CASH_USD=1) is a second cash manager.** It holds USD cash to clear the
   margin debit; the sweep wants that cash in SGOV. They already run in sequence
   (keep-cash-usd first, then sweep) — the spec keeps that order and adds a note that the
   sweep is the tie-breaker for idle capital. See §8 Q3.
4. **Per-currency cash.** `TotalCashValue` is the net of the USD line and the HKD-side debit
   (live: HKD 158,269 = USD 24,161 converted, less a ~HKD 31.3k HKD-side debit). Selling SGOV
   raises *USD* cash and therefore does **not** reduce the HKD-side debit. Whether that debit
   actually costs anything is unmeasured (live `AccruedCash` read 0.0) — amount-only for now
   (§8 Q3).
5. **Odd lots.** SGOV trades in whole shares (~$100 granularity). At $32k NAV the rounding
   error is <0.4% of the target; accept it, floor shares at 0 and don't loop.
6. **Orphan legs (S4 of the execution spec).** If an unrelated orphan SELL fills against a
   flat symbol, the shield's cash reads change underneath it. S4 must land first.
7. **Withdrawals.** `_withdraw_reserve_usd()` already subtracts from investable; keep that,
   and the reserve must never be spent on entries while a withdrawal reserve exists.
8. **NAV ceiling.** `sgov_target` must additionally be capped at `equity_usd` so a large
   deposit cannot push the whole account into SGOV and leave the strategy unlevered.

## 6. Sequencing — this cannot ship before the execution spec

Every step here moves real money, and on live **the sweep's orders are currently discarded**
(RC-2). Shipping a dynamic target on top of an unverified order path just automates the
failure faster. Order:

1. **S2** TIF fix + **S1** order confirmation (execution spec) — orders must be *known* to work.
   This is also the highest-value work in either doc (§0), and it gates everything below.
2. **Shadow mode** (this spec): compute `reserve/sgov_target/delta` every cycle, log it to a
   `cash_shield` store key, expose it on the dashboard, **place no orders**. Run 5 sessions
   and confirm the numbers track reality (spot-check against broker cash+SGOV each day).
3. **Recall first, surplus second**: enable `ensure_cash_usd()` for entries only. This is the
   part that unblocks trading, and it is bounded by real pending demand.
4. **Surplus sweep** on the new target, still bounded by the anti-churn and session guards.
5. **Dashboard panel**: reserve breakdown (pending / float / withdrawal reserve), SGOV % of
   NAV, target band, and last action with its order status — so "why is my cash not parked"
   is answerable without forensics.

**Deliberately deferred (do NOT let it gate steps 1-3):** tightening the float below
`3%/$1,500`, the wider sweep window, and the currency-posture work. Each is worth <~$130/yr
while deployment is worth ~$1,200 (§0). Revisit after live has real fills and a measured edge.

## 7. Verification

| id | check | pass condition |
|---|---|---|
| D-1 | Shadow mode, 5 sessions | computed target vs broker cash+SGOV agree within one rebalance delta every cycle; no order placed |
| D-2 | Recall | one live entry with insufficient cash: SGOV sold, fill confirmed, entry then reaches `Submitted`; cash never negative after |
| D-3 | Surplus | at rest, SGOV reaches ≥70% of idle capital within one session; no rebalance smaller than the floor |
| D-4 | Churn | ≤1 SGOV trade/day on a quiet account; zero buy/sell flips inside 30 min of a recall |
| D-5 | Boundary | withdrawal reserve set → sweep buys nothing; reservation TTL expiry releases stranded reserve; margin assertion trips → static fallback |

## 8. Resolved questions (decided 2026-10-01; rationale kept so they can be revisited)

Answers were requested with pros/tradeoffs and decided in favour of **maximising performance
subject to safe order placement** — which, per §0, means *not* tightening the cheap knobs
prematurely.

- **Q1 — Float size: KEEP `FLOAT_PCT = 0.03`, `FLOAT_MIN_USD = 1_500`.** Tempting to lean to
  `2%/$1,000` (~$21/yr), but the float also serialises recall latency in front of every entry
  and is the buffer that protects trading while the order path is unproven. **Pros of keeping:**
  costs ~$21/yr, buys robustness and no added failure surface. **Cons:** ~$21/yr forgone.
  Revisit after live has real fills; make it env-tunable rather than hardcoded.
- **Q2 — Margin: CONFIRMED margin, but assert the operationally meaningful thing.** Live shows
  `BuyingPower/NL ≈ 6.4x` ($208.6k on $32.4k) — unambiguous. Still, the guard that matters is
  not account type: before a recall require `AvailableFunds + SGOV proceeds ≥ entry notional
  + Σ open entry commitments`. That prevents the rejected order and also catches concentration
  or debit states that account-type detection misses. Keep the account-type read as a
  warning-only log line, never a gate. **Pros:** correct guard, no false trips. **Cons:** needs
  the recall's *filled* value before the entry is sized — another reason S1 lands first.
- **Q3 — Currency: AMOUNT-ONLY for now; measure the HKD deficit before deciding more.** Selling
  SGOV raises USD and does nothing for live's −HKD 31,326 (~$4,016) side, which exists because
  `keep_cash_usd` (CASH_USD=1) converted HKD into USD — the two cash managers are quietly in
  tension. **But the cost is unknown:** live's `AccruedCash` read **0.0** at 12:35 UTC (paper
  read −8,794 HKD), so the deficit may not be accruing at all. Measure first; if it does accrue,
  switch the target to net-of-deficit and add a currency-posture stat. **Pros:** one decision at
  a time, and the shield's job is yield-on-idle. **Cons:** possibly leaving ~$300-500/yr if the
  deficit is punitive — unfalsifiable until measured.
- **Q4 — Sweep window: widen the SURPLUS sweep to full RTH (09:30-16:00 ET); recall stays
  demand-driven.** The 10:00-15:30 gate was justified by spread widening in the first/last 30
  min — true for the strategy ETFs, **not** for SGOV (~1-2bp all session). Worth ~$126/yr.
  **Pros:** instrument-specific rationale correctly applied; free yield. **Cons:** order bugs
  can now fire at 09:35 — mitigated because shadow mode places no orders and the first discarded
  order alerts. Recall needs no change: it only runs because an entry is being placed, and
  entries are already gated to 10:00-15:30. **Deferred** per §6 anyway (<$130/yr).
- **Q5 (added) — Reservation TTL: 90 min, not 60.** Must outlast a full entry window (10:00-15:30
  ET) plus the 30-min `GHOST_ENTRY_GRACE_MIN`, or a legitimately slow signal releases its own
  reservation mid-flight.

## 9. What this does NOT change

The 80%-of-idle default stays as the **fallback** for cash-like accounts and as the ceiling
sanity check. Strategy sizing, risk caps (`RISK_PER_TRADE`, `ETF_POS_CAP`, `PORTFOLIO_CAP`),
the entry window, exit mechanics, and the paper accounting model are untouched — this only
decides *where idle capital sits*, and it must never change *whether* a trade is allowed.