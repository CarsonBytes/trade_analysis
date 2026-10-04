# IBKR PAPER-LOCK SPEC — why paper keeps going dark, and what to do about it

**Status:** proposal, nothing implemented.
**Date:** 2026-10-04. **Author context:** investigation session (also delivered
`84e1966` mirror mode and `ec56912` the equity/sgov cadence fix).
**Scope:** the PAPER instance only. Live is not changed by anything in here.

---

## 1. The ask

*"retro why paper quant still often get locked and suggest spec."*

"Locked" is not one thing. Instrumenting paper for this session turned up **five
distinct lock/stuck modes**, only one of which is the one people mean when they say
"paper is locked". They are listed separately because they have different owners and
different fixes, and because conflating them is why this has survived weeks.

---

## 2. Mode A — IBKR session contention (the real "locked")

**Symptom:** paper's pending-trade pipeline goes dark; fresh quotes stop; the banner
shows the broker unreachable; it recovers on its own minutes-to-hours later.

**Evidence gathered 2026-10-02..04:**

```
Error 10197, reqId 12: No market data during competing live session,
  contract: Stock(conId=319355208, symbol='DBC', exchange='SMART', primaryExchange='ARCA')
Error 10141: Paper trading disclaimer must first be accepted for API connection.
ib_client: connect to ib-gateway:4004 failed after trying clientIds 31-34 (TimeoutError)
ib_client: connected ib-gateway:4004 clientId=31        <- recovers on a later cycle
pending-tick refresh: 0/2 instrument(s) got a fresh IB quote (DBC, QQQ)
```

IBKR allows **one API session per username**. `quant-ibgateway-docker` (paper) and
`quant-ibgateway-live-docker` (live) log the *same* account family, so whoever logs in
last holds the session and the other is refused. This is structural, not a bug in our
code: paper's gateway was recreated by its own deploy at 15:19:31Z and did not get a
usable data session until 15:21:13Z, and even after connecting, its market-data requests
kept drawing 10197 while live's session held the lines.

**Why it recurs so often:** *every paper deploy recreates paper's gateway.* Paper's
script runs `docker compose up -d` after `docker compose build`, which recreates the
gateway as a dependent — fresh login, 2FA, empty order snapshot. Measured cost of one
such cycle on live: **693 consecutive `broker unreachable` cycles over ~18h**
(2026-09-30 15:34 → 2026-10-01 09:41).

**Verdict:** paper does not need IBKR at all. Its own logs say so —
`cheap refresh: 21 scored, data source = yfinance (0/21 MT5-tick)`. Every price it
scores comes from yfinance. IBKR is only being used for account reads, order mirroring
and position truth, and the order mirroring **never funds anything**: paper's
`ib_mirror` table is full of `perm_id=0` rows (`EEM` 2026-10-01, still unfunded).

---

## 3. Mode B — self-inflicted dark windows on every deploy

Independent of contention, each deploy produces a blind window: gateway recreate →
login → 2FA → connect. `2427d25` already made IBC re-request the 2FA push instead of
abandoning at 180s, and the live deploy on 2026-10-03 completed cleanly
(`gateway-login OK: dashboard confirms real IB connection`). But the dashboard itself
treats the window as a normal cycle: it logs `broker unreachable` and keeps
last-good STATE, so the UI shows plausible numbers that are quietly frozen.

---

## 4. Mode C — quota lockout (now mostly retired)

The shared chatanywhere.tech key is capped at 200 req/day across **quant + study +
events**, but `store.can_call()` counts per instance, so each side's counter can read
"under budget" while the real account-wide quota is spent (the 2026-07-14 incident: 876
identical `RateLimitError` failures in a few hours). Seven-day points exhaustion adds an
exponential 4h→24h backoff (`_set_rate_limit_backoff`).

**Retired for paper as of `84e1966`:** paper made 32 board scans in 21 days ($0.05, 17%
of quant's token cost) purely to re-derive what live had already paid for, and the reuse
path meant for that had fired **zero** times because paper's and live's fingerprints
essentially never match. `PAPER_LLM_MODE=mirror` now makes paper serve live's scan and
never call the LLM — `run_board_scan()` is unreachable on paper, including on a manual
refresh. Verified: paper's only ledger row is a zero-token `board_scan_reuse`, local
`calls_today` = 0.

---

## 5. Mode D — monitors permanently red, so real alarms are lost

This is the "it feels locked" factor: paper's UI is *permanently amber*, so a genuine
new problem is indistinguishable from the background.

```
P&L cross-check DIVERGED: equity route +80,607 vs trade route -7,093 HKD
  (gap +87,700, tolerance 56,039) -- fires every 30-90 min, continuously since >= 2026-10-01
reconcile: broker/local position MISMATCH -- only_local(ghost)=['EEM'] -- daily since 10-01
```

**The cross-check divergence is itself a bug, and it is paper-specific.** Measured on
paper just now:

```
paper cash_flows      : [[1788348161, -9223.03, 'HKD']]      <- ONE entry, a withdrawal
paper equity_inception: None                                  <- never set
paper equity_history  : first 1,040,180.01 -> last 1,120,787.49 HKD
deposit_adjusted_series: adj[-1] - adj[0] = +80,607          <- exactly the "equity route" figure
```

With no inception point and essentially no cash-flow ledger, paper's equity route is
measuring **raw window growth** (funding that the dashboard never recorded as a flow),
not trading P&L. Live does not have this problem: 6 flow entries totalling 249,971 HKD
plus `equity_inception = [ts, 0.0, 'HKD']`. So the monitor is not merely noisy on paper —
it is comparing two different quantities, and it has been reporting that every 30-90
minutes ever since the flows went unrecorded.

---

## 6. Mode E — fingerprint no-op (fixed, recorded for completeness)

Before `84e1966`, paper's reuse attempt died on `_try_shared_reuse`'s fingerprint test
and took the branch that does *nothing at all* — no reuse, and no
`place_from_state()`/`mirror_new()` either, since both live inside
`if result is not None`. Paper was paying for a scan and often getting no signals out of
it. Fixed by mirror mode; noted here because "paper did nothing and said nothing" is
exactly what a lock looks like from the outside.

---

## 7. Proposed spec

Each item is independent. S1 is the one that actually removes the lock.

### S1 — Paper stops using IBKR entirely (`BROKER=none` for paper)

Paper's prices already come from yfinance; its orders never fund. Removing the broker
dependency deletes mode A and most of mode B outright.

- Set `BROKER=none` in `docker-compose.yml` (paper only); keep `ib` in the live compose.
- Paper keeps: yfinance scoring, the board (mirrored), `place_from_state()`,
  `paper_trades` journal, pending-trade *bookkeeping*.
- Paper loses: `ib_mirror` mirroring, broker reconcile, the account/cash panels, the
  cash sweep, and IBKR-sourced pending ticks.
- **Acceptance:** paper runs 7 days with no `ib-gateway` container, no 10197, no
  `broker unreachable`, and still places pending trades daily.
- **Risk: low.** It removes capability, so the honest cost is that paper can no longer
  demonstrate real broker fills. Given paper's fills are `perm_id=0` anyway, that is a
  capability paper does not currently have.

### S2 — If IBKR must stay on paper: serialize logins and stop recreating the gateway

- A cross-instance login lock (paper/live share `quant_shared`) so the two IBC sessions
  can never overlap; the loser waits rather than colliding.
- Recreate the gateway only when its image actually changed, not on every dashboard
  deploy — this is what turns each deploy into a blind window.
- **Acceptance:** three consecutive paper deploys produce zero `broker unreachable`
  cycles and at most one login.
- **Risk: low**, but it only mitigates; contention remains possible.

### S3 — Fix paper's cash-flow ledger and set its inception point

- Backfill `equity_inception` for paper, and reconcile the one real flow.
- Root-cause the unrecorded ~+80k HKD: an IBKR-side funding that
  `detect_external_cash_flow()` never saw, or a mis-signed internal cash<->SGOV move
  being read as external. **Do not paper over it by widening the tolerance.**
- **Acceptance:** paper's cross-check stops firing for 7 consecutive days, or the
  residual gap is explained in writing with numbers.

### S4 — A permanently-red monitor is worse than no monitor

- Until S3 lands, `pnl_crosscheck` must alert **once** with the current gap and then stay
  quiet (dedupe by kind+symbol, like the notable-event tiers), instead of re-firing every
  30-90 minutes for a condition that has held for days.
- **Acceptance:** no repeated alert for an unchanged gap; a *changed* gap still alerts.
- **Risk: low.** Slight loss of sensitivity, bought back by not crying wolf.

### S5 — Ghost-position hygiene

Paper's `only_local(ghost)=['EEM']` reconcile mismatch repeats daily. Auto-resolve a
ghost that has been unfunded past `HORIZON_CAL` with one explicit journal entry
(`ghost retired, never funded`) rather than re-alerting forever.
**Acceptance:** the ghost clears once and stops re-alerting; a *new* ghost still alerts
immediately.

### S6 — One quota counter, one owner

With paper silent, the 200/day key is live + study + events. Make `shared_calls_ok()`
the only gate in the decision path; keep `store.can_call()` for display only, and label
it as per-instance so nobody reads it as headroom.
**Acceptance:** no code path can call the provider on a local-counter green light.

### S7 — Mirror mode is the template

Paper should *consume* expensive shared work, never produce it. Same pattern applies to
anything else paper does that live already pays for. Enforce it in review: a new feature
that costs a scarce shared resource must answer "why can't paper reuse live's?" first.

---

## 8. What I did not do

- Did not touch the P&L cross-check numbers themselves — the divergence is a symptom and
  S3 needs the funding root-cause, not a tolerance tweak.
- Did not change live in any way.
- Did not touch the DD-halt gate; it has never fired (`DD_HALT_PCT = -13.0`), so it is
  not a lock.