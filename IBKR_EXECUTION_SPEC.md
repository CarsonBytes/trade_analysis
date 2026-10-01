> **Companion doc:** the dynamic version of §S6 below (static 80% target → reservation-based
> target with on-demand SGOV recall) is specced in **`IBKR_CASH_SHIELD_SPEC.md`**. Read that
> one for the cash question; this doc owns order reliability.

# Execution & cash-shield spec — why live stopped trading and why SGOV sits at 19%

**Status:** PROPOSAL (no code changed). Written 2026-10-01 after a broker-level
investigation of the live account (U12991898) — every number below was read directly
from IBKR or from the deployed containers, not inferred from the dashboard.

**Goal:** make "did my order actually reach the broker?" a first-class, alerted fact, and
bring the live cash shield to its designed ratio. Two user-visible symptoms motivate this:
SGOV is 18.6% of live NAV while ~75% of NAV sits as idle cash, and **zero live entries have
filled since 2026-09-22** (11 attempts, every one ghost-cancelled).

---

## 0. The live picture, 2026-10-01 12:35-12:59 UTC

| item | value | % NAV |
|---|---|---|
| Net liquidation | HKD 252,913 ≈ **$32.4k** | 100% |
| Cash (USD 24,161, net of a −HKD 31.3k debit) | ≈ $20.3k | 63% |
| SGOV | 60 sh = **$6,024** | **18.6%** |
| Strategy positions (QQQ 8 = $5,953, EEM 1 = $67) | $6,020 | 18.6% |
| Working broker orders | 12 — **all SELL legs of resolved trades** | — |

Sweep target per today's own formula: `0.80 × (20.3k + 6.0k) = $21.0k` → would need
**~209 SGOV shares**; live holds 60. `sgov_history` has been frozen at HKD 47,118 since
2026-09-26. Paper, by contrast, is at its target: SGOV $115.1k of $143.4k NAV = **80.2%**,
held 1,146 shares (the sweep sold 324 shares today to clear paper's USD margin debit).

---

## 1. Evidence trail

1. **The sweep has been trying and failing for 5+ days.** Live log, Sep 30 18:01-19:29 UTC:
   17 × `cash-sweep: BUY 149 SGOV @~100.68` submissions, one every ~5 min (the 300s
   cooldown), interleaved with `cooldown Ns, skipping`. SGOV quantity never moved (60), no
   SGOV order was ever seen working at the broker, and **no error was recorded** — the
   event log has zero SGOV entries.
2. **Broker-side probe reproduces it.** From a fresh client session against the same live
   account, at 12:50 UTC: `BUY 1 SGOV LMT @50` → `PendingSubmit → PreSubmitted → Inactive →
   Cancelled` in 40 ms with **error 202 "Order was discarded."** A real `BUY 1 SGOV MKT DAY`
   went `Inactive` immediately and never filled. (Both probes were far-off/unfilled and were
   cancelled within seconds — no market impact.)
3. **The live session was fully authenticated.** Gateway log: `Login has completed`
   (12:00:23), 2FA auto-approved in 8 s, `Bypass Order Precautions for API Orders` selected,
   `Read-Only API checkbox ... false`. So this is not a 2FA/permission artefact.
4. **Paper accepts the identical order path.** The paper container sold 324 SGOV shares
   through the same `sweep_cash()` code today. Same code, same account credentials, different
   IBKR *environment* (paper gateway vs live gateway) → **the rejection is specific to the
   live environment.**
5. **Every live entry since Sep 22 died the same way, with a code bug on top.** Live event
   log: 11 × `DIA: broker error 202 -- Order Canceled - reason:` at **age 0.2-0.7 s**, always
   on the bracket's TP/SL child, `reason:` empty — plus AMLP/CPER/EFA. Journal trades
   122-137 all resolved `entry never filled at the broker (30min...)` → CANCELLED.
   The code cause is visible in `ib_exec._place_etf_bracket` (and `_place_bracket`):
   ```python
   bracket.parent.orderType = "MKT"; bracket.parent.lmtPrice = 0.0
   for o in bracket:
       o.tif = "GTC"        # <-- applied to the MKT PARENT too
   ```
   A market order with `tif=GTC` is invalid at IBKR: the parent is discarded, and IBKR
   cascade-cancels the children → the observed 202s at 0.2 s. The resulting error code
   (IBKR uses 10349/135 for TIF-related discards) is **not in our handled set**
   (`110, 200, 201, 202, 10197` in `ib_client._ensure_conn`'s error hook), so the parent-side
   failure was invisible and only the children's 202 surfaced.
6. **Nothing verifies an order reached the broker.** Both `sweep_cash()` and the bracket
   paths log success at *submit* time and return. The verdict arrives asynchronously
   (status event / error event) and is never checked, so "submitted" was treated as "done"
   for five weeks.
7. **Infrastructure churn amplifies everything.** The live gateway container was recreated
   2026-09-30 09:24 UTC (by my deploy — the compose file makes the gateway a dependency of
   the dashboard, so `--build dashboard` recreates the gateway too) and again 2026-10-01
   12:00 UTC (infra watchdog). Each recreate forces a fresh login + 2FA. During Sep 30
   15:34 → Oct 1 09:41 the dashboard logged **693 consecutive `broker unreachable` cycles**
   — i.e. roughly 18 of 24 hours with no broker at all, during which the sweep was dead and
   journal trade #138 (Oct 1 07:00 UTC) was opened with no way to reach the broker.
8. **Orphan protective orders are a live risk.** Of the 12 working orders:
   CPER SELL 84 (#12), AMLP SELL 21 (#16), SPY SELL 4 (#33) have **no broker position at
   all** — the trades are long resolved, the sibling leg was never cancelled (IBKR bracket
   children are not OCA-linked after the parent fills, so a fill of one does not cancel the
   other). If CPER rallies to 41.91 the broker will sell 84 shares we do not own → an
   unintended short with no stop. QQQ carries 9 SELL legs against 8 shares held.
9. **`keep_cash_usd` is half-dead**: 5 × `HKD: broker error 200 -- No security definition
   has been found for the request` (latest Sep 30 09:37) — the FX leg of the cash shield
   errors out, so idle-cash currency management is not doing its job either.
10. **Sweep sizing uses the right basis** (clarified while checking): IBKR's
    `TotalCashValue` (HKD 158,269) is the *net* of USD cash 24,161 (≈HKD 189,595) and a
    −HKD 31,326 HKD-side margin debit, so `cash_usd = TotalCashValue / 7.8 = $20.3k` is
    correct. No bug there.

---

## 2. Root causes, ranked

| # | cause | status |
|---|---|---|
| RC-1 | MKT parent carries `tif="GTC"` → broker discards parent → children 202 at 0.2 s → every entry since Sep 22 is a ghost. Same in the futures path. Parent-side error code not in our handled set → invisible. | **confirmed in code** |
| RC-2 | The live IBKR environment discards API orders ~40 ms after submission ("Order was discarded."), even fully authenticated, even a MKT DAY. Paper environment accepts the same code path. Exact IBKR-side reason still unknown (md-farm flaps 2108/2119/2104 were observed around the submission; Client Portal order history will name it). | **reproduced; cause pending V-1** |
| RC-3 | No post-submit confirmation anywhere: "submitted" is logged and believed. Sweep retried blindly 17×; entries were written to the journal and ghost-cancelled 30 min later; nobody was told. | **confirmed in code** |
| RC-4 | Sweep retry policy has no failure budget and no alert; its duplicate-guard trusts a broker read that, during flaky periods, returns stale/empty results. | confirmed in code |
| RC-5 | Deploys and the watchdog recreate the IB gateway → new login + 2FA → empty order snapshot → ~18 h of broker-unreachable cycles on Sep 30. | confirmed in infra |
| RC-6 | Orphan sibling legs are never cancelled → latent short exposure (CPER/AMLP/SPY) and over-protection (QQQ 9 vs 8). | confirmed at broker |
| RC-7 | `keep_cash_usd` errors 200 on the HKD leg → the FX half of the cash shield is dead. | confirmed in event log |

---

## 3. Proposed design

### S1 — Order lifecycle: submit → confirm (the keystone)
New helper in `ib_exec`, used by **every** order path (ETF bracket, futures bracket, sleeve
bracket, sweep, keep-cash-usd, exits, reprotect):
- place the order(s), capture `orderId`/`permId`;
- wait up to `ORDER_CONFIRM_SEC` (default 5 s) for a status in
  `{PendingSubmit, PreSubmitted, Submitted, ApiPending}`;
- treat `Inactive / Cancelled / Rejected` as **failure**: cancel any siblings immediately,
  record `orderId + status + broker message + error code` to a new `order_exec_log` store
  key (last 100 rows, both instances), and raise a RED notable event **once per
  (symbol, reason)**;
- return the outcome to the caller so the journal does **not** open a trade that never
  reached the broker.
Tests: fake IB object with scripted status callbacks covering accept, discard, parent-death.

### S2 — Fix the TIF bug (S1's first victim)
`o.tif = "DAY"` on the MKT parent; `GTC` only on the TP/SL children. Two lines in
`_place_etf_bracket` and `_place_bracket`. Test asserts parent `DAY` + children `GTC`, and
that a parent-death cancels children client-side.

### S3 — Sweep hardening
- use S1; count consecutive failures; **3 in a row → RED alert** "cash shield not executing"
  (this is the alert that should have fired on Sep 30 morning);
- write each attempt (orderId, status, message) to `order_exec_log` so forensics never need
  the broker again;
- cancel-and-replace a working DAY SGOV order older than 10 min instead of stacking;
- keep the 80% target, but add a USD **ceiling** `min(target, equity_usd)` so a large
  deposit cannot push the whole account into SGOV;
- expose `sgov_ratio_pct = sgov_value / NAV` and `target_pct` on the dashboard as a stat with
  a target band — "why is SGOV low" must be answerable at a glance, not by forensics.

### S4 — Order hygiene (risk reduction, do early)
- each cycle: for every working order whose symbol has **no** broker position and whose
  mirror row is not OPEN → cancel it (that alone removes the CPER/AMLP/SPY short risk);
- `_reprotect_bracket` and every bracket builder cap total open qty per conId at the real
  held quantity (same safety guard `manual_close_position` already applies);
- alert (RED) whenever an order is cancelled by us for being orphaned.

### S5 — Deploy/gateway separation
- live (and paper) dashboard deploys use `docker compose ... up -d --no-deps --build dashboard`
  so the gateway is never recreated by a code deploy;
- the deploy script's success criterion becomes: gateway `Login has completed` **and**
  `/status.trading_ready == true` (account + positions readable **and** a probe order status
  observed — see S1), not merely "container healthy";
- the infra watchdog must not restart a gateway whose session is healthy; if it must, it
  waits for `trading_ready` and notifies.

### S6 — Cash-shield policy (superseded by `IBKR_CASH_SHIELD_SPEC.md`)
Originally: make the target explicit in NAV terms. Superseded by the dynamic reservation
design — hold back only what the pipeline needs plus a float, park the rest in SGOV, and sell
SGOV to fund an entry when one appears. See that doc for the formula, the two-tier
recall/sweep split, and the boundary conditions.

---

## 4. Verification plan

| id | check | pass condition |
|---|---|---|
| V-1 | Discriminate RC-2: inside 10:00-15:30 ET, place 1 far-off LMT + 1 MKT on live with S1 instrumentation; read IBKR Client Portal → Orders for the reject reason; capture md-farm state at submit time. | reason identified and recorded in this doc |
| V-2 | After S1+S2: one live DIA entry | status `Submitted` within 5 s; mirror row exists; fill or clean cancel with reason |
| V-3 | Sweep | SGOV moves toward ~209 shares within one session; alert if 3 consecutive failures |
| V-4 | Hygiene | orphan working orders = 0; QQQ legs = 8 |
| V-5 | 24 h soak | no broker-unreachable streak > 5 min; clientId flap ≤ 3/h; zero unreported order failures |

---

## 5. Build order (smallest provable increments)

Priority rule, set with the user 2026-10-01: **maximise performance subject to safe order
placement.** Measured on live, deploying the currently-idle $26,404 is worth ~$1,200/yr expected
while every cash-shield tuning knob together is worth ~$147/yr — and the deployment gap is
blocked by *this* doc, not by cash policy. So order-reliability work comes first and cash
policy must not gate it.

1. **S2** — two lines + test; deploy paper, then live. *Highest value per line anywhere.*
2. **S4** — orphan sweep (removes live short risk) + qty cap. Deploy.
3. **S1** — the confirmation helper + `order_exec_log`; adopt in sweep first, then brackets.
4. **S3** — sweep failure budget, ceiling, ratio stat.
5. **S5** — `--no-deps` deploys + `trading_ready` gate (coordinate with the watchdog owner).
6. **S6** — policy numbers, after the user picks the target ratio.

Deferred behind real fills on live: tightening the cash float, widening the sweep window,
currency-posture work. See `IBKR_CASH_SHIELD_SPEC.md` §0 for the numbers and §8 for the
resolved decisions.

---

## 6. Open questions (blocking S6, cheap to answer)

- **Q1** Desired steady state for live: SGOV as % of **NAV** (e.g. 60-70%) or the current
  "80% of idle cash"? Right now the answer depends on how much the strategy deploys.
- **Q2** May the sweep run a wider window (09:30-16:00 ET, shield only) than entries
  (10:00-15:30 ET)? The cash shield is not latency-sensitive.
- **Q3** May verification place real (tiny) orders on live — e.g. 1 share, reversed
  immediately? V-1/V-2 need it.
- **Q4** Who owns the infra-watchdog restart policy, so S5 lands in one place?

## 7. What this spec does NOT change

Strategy and sizing model, risk caps, paper accounting, the LLM scan cadence, exit
mechanics, the 80%-of-idle default, and the reconciliation/cross-check monitors. The goal is
that when something breaks, the system says so within seconds instead of after five weeks.