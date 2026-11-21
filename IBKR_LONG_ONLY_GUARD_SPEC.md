# IBKR Long-Only Guard — spec (2026-11-21)

**Status:** Phase 1 SHIPPED 2026-11-21 (G7 wording/severity/digest, G6.1 untracked card,
G8 Flex StockShort alarm; see the HANDOFF entry). Phases 2–5 PROPOSED.
**Incident:** [RETRO_2026-11-21.md](RETRO_2026-11-21.md). LIVE has been net short (QQQ −8,
AMLP −21, CPER −84, SPY −4) since Oct 30. This is the third unintended-short incident (PAPER
2026-09-01, 2026-09-29).
**Scope:** `dashboard/execution/ib_exec.py`, `dashboard/core/notable_events.py`,
`dashboard/web/service.py`, `dashboard/app.py`, `dashboard/web/retrospective.py`, tests,
runbook. Applies to PAPER and LIVE (`BROKER=ib`).

---

## 0. The invariant

> **I1. For every contract, at every moment: the broker position is ≥ 0, AND the total
> of all resting SELL orders is ≤ the broker's long position.**
> (The only exceptions are SELL child legs attached to a parent BUY that hasn't filled yet,
> since IBKR keeps those inactive until the parent fills.)

`paper.LONG_ONLY` is currently just a signal flag. This spec turns it into an **order-layer
guarantee** with three layers:

1. **Prevent.** One choke point that refuses any SELL that would break I1 (§1).
2. **Clean up.** Find and cancel orphan orders before they can fire (§2, §3).
3. **Detect and repair.** If a short exists anyway, raise a RED alert immediately and
   cover it automatically (§5).

Each layer must stand on its own. The incident got through because only detection existed,
and it was set to YELLOW.

---

## 1. G1: Single order choke point `place_guarded()`

**Problem.** `placeOrder` is called directly from ~8 sites (`_place_etf_bracket`,
`_place_sleeve_bracket`, `_reprotect_bracket`, `manual_close_position`, `sweep_cash`,
`keep_cash_usd`, `prepare_withdrawal`, `unwind_shorts`). Each one applies its own
assumptions about quantity and direction.

**Design.** A new `ib_exec.place_guarded(ib, contract, order, *, intent, parent=None)` is the
**only** code allowed to call `ib.placeOrder`. A test greps the code to enforce this (§9 T1).

For a standalone or child SELL whose parent has already filled:
1. Fresh read (not cached) of the signed position for `conId` (`ib.positions()`, account-filtered).
2. Fresh read of resting SELL quantity on `conId` across **all** clients (`reqAllOpenOrdersAsync`).
3. `allowed = max(0, long_pos − resting_sell_qty)`.
4. `order.totalQuantity > allowed` → **clamp** to `allowed` if `intent` allows it (exit,
   reprotect). Otherwise **refuse**. If `allowed == 0` → refuse and log
   `GUARD: refused SELL <qty> <sym> -- would open/extend a short (long=<x>, resting_sell=<y>)`
   as a RED event.
5. If either read fails or returns empty while a position is held → **refuse**. The guard
   fails closed. (This is the 2026-09-01 lesson: an empty read during a sync gap is not "nothing there".)

For a BUY (entries, short covers, SGOV): there is no I1 check, but `intent="cover"` must
satisfy `qty ≤ abs(short_pos)`, so a cover can never overshoot into a long (the 2026-09-29
unwind overshoot).

Brackets: the parent BUY is placed through the guard with its children attached. Children
are exempt from the position check *only* while attached to an unfilled parent.

`intent` enum: `entry`, `bracket_child`, `reprotect`, `exit`, `cover`, `sweep`, `fx`, `withdraw`.

---

## 2. G2: Never VOID without confirming the cancel

**Problem.** [ib_exec.py:1322](dashboard/execution/ib_exec.py:1322) marks a trade VOID right
after calling `cancelOrder`, without checking that the cancel worked (RC1).

**Design.** A new mirror status, **`CANCEL_PENDING`**.

1. When the journal resolves a trade whose entry never filled: send `cancelOrder` for every
   order on that conId *whose `orderRef` matches `quant#<id>` / `reprotect#<id>`*. Don't
   cancel the whole conId: a newer trade could share it. Then set `CANCEL_PENDING` and
   store `cancel_requested_ts`.
2. Each cycle, re-read open orders. Move to `VOID` **only** when no order with that ref
   shows up in **two consecutive** reads at least 30s apart.
3. Still present after 3 cycles, or Error **10147** received → escalate:
   - try cancelling with the order's own `clientId` (connect briefly as that id if it's in
     the known pool 31–34/41–44);
   - otherwise raise a RED event `ORPHAN ORDER cannot be cancelled by this client: <ref>
     <side> <qty> <sym> @<px>` with a one-click **Global cancel + re-arm** action (§3.3).
4. Never treat `CANCEL_PENDING` as terminal. It keeps blocking the guard's
   `allowed` calculation, because resting SELLs count against I1.

---

## 3. G3: Orphan-order sweeper

**Problem.** Nothing ever lists every resting order and asks "who owns this?" The July
orders sat at the broker for about 4 months.

### 3.1 Classification (every cheap cycle, and once at startup)
For each order from `reqAllOpenOrdersAsync()` (all API clients):

| Class | Rule | Action |
|---|---|---|
| OWNED | `orderRef` → an `ib_mirror` row in OPEN/PENDING; side and qty consistent | none |
| SWEEP | `orderRef` starts with `cash-sweep` / `fx` | none (own lifecycle) |
| **ORPHAN_SELL** | SELL, not OWNED, and I1 would break if it filled | **cancel now** (§3.2); RED if it can't be cancelled |
| ORPHAN_BUY | BUY, not OWNED | cancel; YELLOW (worst case it opens an unwanted long) |
| FOREIGN | no `orderRef` and not from an API client (TWS/mobile manual) | don't touch; YELLOW once per order, listed in the UI |

### 3.2 Cancel path
Same confirmation process as G2. Cancel attempts go through `cancel_guarded()`, which logs
and checks every cancel.

### 3.3 Global cancel + re-arm (manual escalation action)
`reqGlobalCancel()` → wait for two consecutive empty open-order reads → run
`reprotect_naked_positions()` (now guarded, §4) → `sweep_cash()` resumes next session.
Available as a dashboard button behind confirmation, and as `python -m dashboard.ops.global_cancel_rearm`.

---

## 4. G4: Re-protect and qty-heal read the broker position

**Problem.** RC2 (the heal changed the mirror qty but left the resting stop at 8) and RC3
(reprotect armed SELL 7 against a broker position of −1).

**Design.**
1. `reprotect_naked_positions()` reads `pf.position` (signed, broker) for every row:
   - `pos ≤ 0` while the journal says long → **never place anything.** Raise the
     short-incident path (§5). Log that reprotect was refused because the broker is not long.
   - `pos > 0` → arm a bracket for `min(mirror_qty, pos − resting_sell_qty)` through `place_guarded`.
2. **Settle delay.** Reprotect must not run within **60s** of any fill or cancel event on
   the same conId. The QQQ re-arm fired 4 seconds after the stop filled. Track
   `last_order_event_ts[conId]` from `execDetails`/`orderStatus` events.
3. `heal_mirror_quantities()`: after changing a mirror qty, **resize** the resting SL/TP
   legs on that conId to the new qty (modify via the same orderId, through the guard).
   If the resize fails, cancel the old legs and let reprotect re-arm. Never leave a stop
   larger than the position.

---

## 5. G5: Short detection and automatic cover

### 5.1 Detection
Every cycle, for every account position: `position < 0` → **SHORT_INCIDENT**.
- **RED** immediately (Telegram critical, ntfy), deduped per (symbol, incident), with a
  reminder every 24h until resolved. Text: `UNINTENDED SHORT: QQQ -8 (~HKD 43k, 15% NAV) -- auto-cover <scheduled HH:MM ET | disabled>`.
- The header shows a red banner. Health shows `exposure: SHORT <list>`.
- **New entries halt** while any SHORT_INCIDENT is open (`entry_gate: short incident`).
  The cash sweep keeps running but excludes short proceeds from `investable` (HANDOFF
  2026-09-29: the sweep put short-sale proceeds into SGOV).

### 5.2 Auto-cover (`cover_unintended_shorts()`)
Runs only in US regular hours (RTH guard; `FORCE_RTH` for ops).
1. First cancel every resting SELL on that conId (G3 path, confirmed).
2. Re-qualify the contract with **`exchange="SMART"`** plus `primaryExchange`. Never reuse
   `positions().contract` (RC4: `exch=NASDAQ` was discarded).
3. Use a **marketable LMT**, not MKT: `lmt = ask × (1 + COVER_SLIP_BPS/1e4)`, `tif=DAY`,
   `orderRef=cover#<sym>#<incident>`, `intent="cover"`.
4. Tranches: `qty ≤ abs(position)`. After each fill, re-read until **two consecutive
   identical** position readings before sizing the next tranche (runbook overshoot lesson).
5. If init margin rejects it (Error 201 margin), sell SGOV for `1.1 × cover notional`, then retry once.
6. Limits: auto-cover only while `abs(short notional) ≤ AUTO_COVER_MAX_PCT_NAV` (default
   **50%**). Above that, RED "manual cover required" and the command to run.
7. Done = position 0 in two consecutive reads. Then resolve the incident, notify GREEN, and
   record the realized P&L as a journal row with `method='unintended-exposure'` (§6).

Config (`.env` / compose): `AUTO_COVER_SHORTS` (default **1** on LIVE and PAPER),
`AUTO_COVER_MAX_PCT_NAV=50`, `COVER_SLIP_BPS=10`.

### 5.3 Exit-path fixes (RC4)
`manual_close_position()`:
- uses the SMART re-qualified contract (as in §5.2.2);
- outside RTH: **don't send.** Queue it and keep showing the warning. Retrying MKT every
  2 min outside hours just piles up rejections;
- if the broker sign is opposite to the trade direction → **don't close it as an exit.**
  Hand it to §5 (a short is a separate incident, not an exit of this trade);
- if the same exit is rejected N=5 times → RED once with IBKR's reason, then back off exponentially.

---

## 6. G6: Accounting and UI truth

1. **Portfolio panel:** a new **"Untracked broker positions"** card listing every broker
   position that isn't a strategy position or SGOV: symbol, signed qty, avg cost, mark,
   unrealized P&L (HKD), % NAV. Shorts in red. It shows whenever the list isn't empty.
2. **Headline split:** "You are up HKD X" becomes **Strategy P&L** + **Cash/SGOV/interest** +
   **Unintended exposure P&L**. Unintended P&L must never appear as strategy profit. (Today
   +8,059 is about +8.6k from the shorts.)
3. **"Unrealized (open)"** = strategy unrealized + untracked unrealized, with a tooltip for each part.
4. **P&L check** (`diverged +14,863`): when it diverges, show the untracked P&L next to it
   so the gap can be explained.
5. **Retrospective:**
   - KPIs (`n`, win rate, expectancy, max DD) count **filled-and-closed** trades only.
     Exclude CANCELLED/never-filled (fixes n=105 / "2%");
   - add a row: "Unintended exposure since inception: HKD X realized / Y unrealized, N incidents";
   - monthly attribution: an `unintended` column next to trend/sleeve/other.
6. **Stale-data marking:** equity samples taken while `broker_conn.ok == False` are stored
   with a `stale=1` flag. The chart draws them dashed or grey. Daily P&L skips them.

---

## 7. G7: Reconcile and alert wording

1. `notable_events.humanize()`: build the text from the parts that actually differ:
   - `only_broker` → "**Broker holds positions the journal doesn't: <syms>** — possible
     orphan fill or unintended short."
   - `only_local` → "Journal has positions the broker doesn't: <syms> — usually an unfilled order."
   - `qty_mismatch` → "Quantity differs: QQQ broker −8 vs journal 7."
2. Severity: any mismatch involving a **negative broker qty**, or an `only_broker` entry
   lasting > 2 cycles → **RED**. Everything else stays YELLOW.
3. A mismatch that lasts > 24h → daily RED digest, even if YELLOW
   ("reconcile has disagreed for N days: …"). Nothing should be able to stay silent for 22 days again.

---

## 8. G8: Startup and periodic audit
- **Startup:** run G3 classification + G5 detection before the first trading cycle. If
  anything is RED, entries stay blocked until it's resolved.
- **Weekly Flex cross-check** (extends `scripts/flex_nav_reconcile.py`): RED if
  `EQUT.StockShort != 0`. This independent check uses no gateway and no dashboard state,
  and it would have caught this on Nov 2.

---

## 9. Tests (regressions, each named for the incident step)

| ID | Test | Asserts |
|---|---|---|
| T1 | `test_only_place_guarded_calls_placeOrder` | grep: no `ib.placeOrder(` outside `place_guarded` |
| T2 | `test_guard_refuses_sell_beyond_long` | long 7, resting SELL 8 → new SELL 1 refused; SELL 7 against long 0 refused |
| T3 | `test_guard_fails_closed_on_empty_orders_read` | position held + empty open-orders read → refuse |
| T4 | `test_void_requires_confirmed_cancel` | cancel sent, order still listed → CANCEL_PENDING, not VOID; 10147 → RED |
| T5 | `test_orphan_stop_after_void_is_cancelled` | the AMLP #16 replay: VOID-era SELL STP 21 with no long → classified ORPHAN_SELL → cancel |
| T6 | `test_reprotect_refuses_when_broker_short` | the QQQ Oct 30 replay: journal long 7, broker −1 → no order, SHORT_INCIDENT |
| T7 | `test_reprotect_settle_delay` | fill event 4s ago on conId → skipped |
| T8 | `test_qty_heal_resizes_resting_legs` | mirror 8→7 → SL/TP modified to 7 |
| T9 | `test_cover_never_overshoots` | slow partial fills → tranche sizing waits for two identical reads; total BUY == abs(short) |
| T10 | `test_cover_uses_smart_marketable_lmt` | contract.exchange == SMART, LMT, not MKT; refused outside RTH |
| T11 | `test_humanize_mismatch_direction` | only_broker text ≠ only_local text; symbols taken from the right part |
| T12 | `test_short_mismatch_is_red` | negative broker qty → RED |
| T13 | `test_retro_kpis_exclude_unfilled` | CANCELLED rows excluded from n / win rate |
| T14 | `test_untracked_positions_in_portfolio_and_pnl_split` | short shows in the card; headline split sums to total |
| T15 | `test_flex_stockshort_alarm` | EQUT.StockShort < 0 → RED |

Add T2/T6/T11 to the PowerShell regression suite (`test-min2fa.ps1`'s neighbour) as live probes.

---

## 10. Rollout

| Phase | Content | Gate |
|---|---|---|
| 0 (now, manual) | Retro §6: cancel orphans, cover the 4 shorts, confirm flat | user executes during RTH |
| 1 | G7 wording/severity + G6.1 untracked card + G8 Flex alarm | detection is truthful, no order changes |
| 2 | G1 choke point + G4 reprotect/heal fixes | T1–T3, T6–T8 green; PAPER 1 week clean |
| 3 | G2 confirmed VOID + G3 orphan sweeper | T4–T5 green; PAPER startup audit clean |
| 4 | G5 auto-cover (PAPER first, then LIVE) | T9–T10; a PAPER drill: deliberately short 1 share → auto-covered next RTH |
| 5 | G6.2–6.6 accounting/retro | T13–T14 |

## 11. Acceptance criteria
- A deliberate PAPER drill (`SELL 1` of a symbol not held) is **refused by the guard**.
  When forced through TWS, it's detected as RED within one cycle and covered at the next
  RTH, ending flat.
- Startup on an account holding an orphan SELL STP cancels it, or raises RED, before the
  first trading cycle.
- Replaying the QQQ Oct 30 sequence (unit level) ends at 0 shares, not −8.
- The dashboard headline no longer counts unintended P&L as strategy profit. Retro n and
  win rate count filled trades only.
- 30 days on LIVE with `only_broker` empty and Flex StockShort = 0.

## 12. Open decisions (user)
1. **Auto-cover on LIVE.** The spec recommends ON with a 50% NAV cap. The alternative is
   alert-only, with a human covering it.
2. **Account-level backstop.** Check whether IBKR lets this account block short sales (or
   whether a cash-account type is acceptable given the SGOV/margin use). That would be a
   broker-side backstop beyond anything in code.
3. **What to do with the current gain.** Covering at today's prices locks in about
   HKD +8.6k as *unintended* P&L. It should be reported separately and not as strategy edge.
