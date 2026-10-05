# IBKR-Referenced Accounting — spec (PROPOSED 2026-10-05)

**Status:** proposed, not implemented. Written after the 2026-10-05 session, where reconciling
cash flows against IBKR nearly destroyed real data. This file is the design; the code change is
§3 and is not started.

## 1. The problem, stated as a rule

> **The dashboard may never assert a financial fact that IBKR contradicts — and may never
> *delete* one either, without a human decision.**

Today's near-miss is the reason this needs writing down. Two failures, opposite in direction:

| # | Failure | Direction | Consequence if applied |
|---|---|---|---|
| F1 | `detect_external_cash_flow()` invented a `−9,223.03 HKD` flow on 2026-09-02 | dashboard asserted, IBKR silent | live P&L understated by 9,223 |
| F2 | my reconciler demanded an exact same-day/exact-amount IBKR match and flagged 3 of live's 6 real deposits as contradicted | IBKR "overruled" the dashboard | would have **deleted 159,971.75 HKD of real funding** and inflated live P&L by ~160k |

F1 is a false positive the detector invented. F2 is a false positive *I* invented. Both would
have been applied by a script that believed its own threshold. That is the real defect: **an
automated reconciler with authority to delete, using a match rule narrow enough to produce
false contradictions.**

## 2. Evidence rules

**R1 — IBKR is the authority on *values*.** For NAV, deposits/withdrawals, and positions, a Flex
statement value wins over anything the dashboard derived.

**R2 — IBKR is silent, not wrong, outside its coverage.** Flex generates statements only for
closed periods. A missing day means "not yet generated", never "no transaction". Absence of
evidence is not evidence of absence.

**R3 — Never infer ineligibility from a scoped probe.** A Flex query returns exactly the account
it was created against. Testing paper with the live query proves nothing about paper. (This is
the exact mistake §0.1 of `IBKR_HISTORY_SPEC.md` made, and the user caught it.)

**R4 — Match like-for-like, or do not match at all.** IBKR timestamps a transfer on its *value
date*; the dashboard detects it from a cash-balance change, landing a day later and measuring net
cash rather than gross. A legitimate match is ±1 day and within `max(100, 2%)`. Anything stricter
manufactures false contradictions.

**R5 — Deletion requires a human.** Reconcilers may *flag*; only a human may *remove* a recorded
financial fact. Auto-repair may only add.

## 3. Implementation

**3.1 Flow detection becomes advisory.** `detect_external_cash_flow()` currently writes to
`cash_flows` directly. Change it to emit a *candidate* row marked `unverified`. A candidate never
enters P&L. This alone would have prevented F1.

**3.2 Verification against Flex, per flow.** On the daily Flex pull, match each candidate to
`CNAV` `DepositsWithdrawals` using R4. Outcome recorded on the row:

| state | meaning | counts in P&L |
|---|---|---|
| `unverified` | detected, no Flex coverage yet | **no** |
| `corroborated` | matched to a Flex value | yes |
| `contradicted` | Flex covers the window and shows nothing near it | **no** + raise `critical` |

**3.3 Promotion and demotion are both audited.** A candidate that later corroborates is promoted
and the transition logged. A recorded flow that becomes contradicted is **flagged, not deleted**
— the row survives with `contradicted` state so P&L stops counting it and a human decides
whether to remove it. This is the F2 guard: the reconciler can stop trusting a number, but only
a person can erase it.

**3.4 Backups before every write.** Any `cash_flows` mutation snapshots to
`cash_flows_pre_ibkr_reconcile_backup` (already the convention from today's run) with a printed
before/after count and delta.

**3.5 Coverage is explicit.** Every Flex pull records the exact `[from, to]` window it covers.
R2 is enforced by checking coverage before declaring anything contradicted — the 2026-10-02 flow
is unjudgeable, not contradicted, because IBKR's window ends there.

## 4. NAV, separately

NAV already follows R1: Flex end-of-day overwrites seeded rows, and the dashboard never overwrites
recorded data (the backfill tool refuses on/after the first reading).

One gap worth noting, not fixing: the 20 independently-verifiable paper days agreed to 0.239%
mean because the dashboard's *last sample of a day* is not the *close*. Replacing those with Flex
closes would improve accuracy but destroy intraday granularity the chart needs. Recommendation:
keep intraday samples, and additionally store one IBKR EOD point per day so the series has a
true close. Deferred — flagged, not claimed.

## 5. Tests

| test | asserts |
|---|---|
| `test_unverified_flow_excluded_from_pnl` | a candidate flow does not move P&L (F1) |
| `test_deposit_one_day_offset_still_corroborates` | +10,000 on 07-08 matches IBKR 07-07 (F2) |
| `test_deposit_net_vs_gross_tolerance` | 89,984.61 corroborates IBKR 90,000.00 |
| `test_outside_flex_coverage_is_not_contradicted` | a flow after the Flex window is `unverified`, never `contradicted` (R2) |
| `test_contradicted_flow_survives_on_disk` | demotion preserves the row; only state changes (§3.3) |
| `test_no_auto_delete_of_recorded_flow` | reconciler cannot shrink `cash_flows` (R5) |
| `test_scoped_query_is_not_ineligibility` | a live-query probe returning no `DU` rows does not imply paper is ineligible (R3) |

## 6. Open questions for the user

1. Should `contradicted` rows be **hidden** from the P&L chart, or shown with a marker? Currently
   proposed: excluded from the maths, surfaced in the daily digest.
2. Is `max(100, 2%)` the right tolerance, or should it scale with account size?
3. Should the Flex pull run daily or monthly? Daily detects contradictions sooner; monthly is
   cheaper and matches current cadence.