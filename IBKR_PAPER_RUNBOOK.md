# IBKR paper-trading runbook

Operational guide for the `BROKER=ib` paper-trading phase (P6). Last updated 2026-06-21.

## What's configured
- **`analyst/.env` → `BROKER=ib`** (acct DUK968178, port 4002). The dashboard uses
  IBKR on its **next restart**; MT5 demo mirroring stops. (Comment it out to revert.)
- **Universe = `{metal, index, rate}`** (auto default under BROKER=ib) — 21 futures
  markets, ~31 trades/year, 0.5% risk, fixed (no vol targeting).
- **Deps durable**: `ib_async` + `MetaTrader5` are now first-class deps, so plain
  `uv run` no longer strips them (no more `--no-sync` needed).
- **UI is broker-aware**: header shows "IBKR Paper · acct DU… ●", data source shows
  IBKR/yfinance, Active Trades show "IBKR Paper fill", retrospective KPIs are over
  IBKR-executed trades (the `ib_mirror` table). Set BROKER=ib ⇒ everything shows IBKR.
- **Robustness**: IB reconnect is throttled (30s) so a down gateway doesn't stall the UI.

## The scheduled task needs NO change
`DashboardApp` → `C:\Scripts\dashboard.ps1` runs `python -m dashboard.app` via the
**venv python directly** (not `uv run`), so it picks up `ib_async` and `BROKER=ib`
from `.env` automatically. Entrypoint is unchanged by the reorg.

## Pre-cutover checklist (do once, before the restart)
1. **IB Gateway running + logged into the paper account**, API enabled, port **4002**,
   `127.0.0.1` trusted. Verify: `uv run python -m dashboard.data.ib_client` →
   should print `connected … account=DUK968178 paper=True` + spec checks OK.
2. **(Optional) Flatten leftover MT5 demo positions** so they aren't orphaned when the
   app stops managing MT5: `uv run python -m dashboard.execution.executor --flatten-foreign`.
   (Harmless to skip — they're demo; the IBKR executor just won't touch them.)
3. **(Optional) CME real-time market-data subscription** for live ticks. Without it the
   weekly system runs fine on delayed/historical + yfinance; you just won't see live ticks.

## Cutover
```
Stop-ScheduledTask DashboardApp; Start-ScheduledTask DashboardApp
```
Then open http://localhost:8080 — the header should read **IBKR Paper · acct DUK968178 ●**.
(Or click **Restart** in the dashboard header.) NOTE: with `BROKER=ib` in `.env`, ANY
watchdog restart now comes up on IBKR — that's intended.

## What to watch over the next weeks
- **Cadence is slow**: ~2–3 trades/month total; whole weeks may be quiet. That's correct.
- **First live trade**: confirm a bracket order appears in IB Gateway (parent MKT + SL + TP),
  and that `dashboard.execution.ib_exec reconcile` (or the retrospective panel) shows it.
- **Rolls**: positions auto-roll near expiry (`needs_roll`); watch the log around the
  monthly/quarterly roll window.
- **Rates sleeve (ZN/ZB/ZF)**: highest cost sensitivity (~2.25% of risk). Watch that real
  fills/slippage track the backtest.
- **Judge via the retrospective** only after n≥30 IBKR-executed trades (months). Don't
  scale risk above 0.5% until live DD is confirmed near the −9.9% backtest.

## Revert
Comment out `BROKER=ib` in `analyst/.env`, restart the task → back to MT5 demo.

---

## Orphan-position audit (run before any account cutover/migration)

**Why this exists**: `reconcile.reconcile_with_broker()` (the dashboard's own built-in check)
only compares *sets of symbols* — it flags a symbol as a "ghost" or "untracked" but does NOT
compare *quantities*. This session (2026-08-13) found that misses a real, dangerous failure
mode: a symbol can already have a legitimate `OPEN` mirror row for a NEWER trade layer, which
makes reconcile's symbol-set check pass clean, while an OLDER, silently-orphaned row for the
SAME symbol (broker still holds those shares for real, but the local row was wrongly marked
CLOSED by a historical `sync_closures()` bug) sits completely invisible. 4 of 5 orphaned
positions found that day were hidden exactly this way — only 1 had ever been flagged before,
because it happened to have no newer OPEN layer masking it. **Always audit by quantity, not
just status**, and run this BEFORE trusting reconcile's own "matched" badge for anything
migration-critical (e.g. before disabling a native deployment in favor of Docker, or before
adding a new deployment as a second consumer of the same account).

### Steps

1. **Get the broker's real, authoritative position for every symbol** — don't infer this from
   any local table:
   ```python
   from dashboard.data import ib_client
   ib_client.broker_positions()          # {symbol: net_qty}, straight from ib.positions()
   ```
   For full detail (avgCost, live P&L, con_id) instead of just qty, call `ib.positions()` /
   `ib.portfolio()` directly the way this session did (see HANDOFF.md 2026-08-13 for the exact
   script) — useful to also confirm the true unrealized P&L nobody's currently tracking.

2. **For EVERY symbol the broker reports (not just ones already flagged), sum ALL local
   `ib_mirror` rows for that symbol/con_id — regardless of status** — and compare against the
   broker's real qty:
   ```sql
   SELECT local_symbol, status, qty, ts, note FROM ib_mirror
   WHERE local_symbol = ? ORDER BY ts;
   ```
   - If `sum(qty where status='OPEN')` already equals the broker's real qty → clean, no orphan.
   - If it's LESS than the broker's real qty → the difference is hiding in one or more `CLOSED`
     rows for that same symbol/con_id that are actually still real at the broker. This is the
     exact pattern that hid AMLP/CPER/CWB/VNQ/QQQ — cross-check each `CLOSED` row's `ts`/`qty`
     against the shortfall to identify which specific row(s) are the orphan(s).
   - A symbol the broker holds with **zero** `ib_mirror` rows at all is the simplest case (was
     VNQ/QQQ's original state before the 08-05 partial fix) — same conclusion, easier to spot.

3. **Cross-check each suspected orphan's `paper_trades` record** — a `CLOSED` `ib_mirror` row
   almost always pairs with a `paper_trades` row already resolved to `WIN`/`LOSS`/`EXPIRED` with
   a computed `exit_price`/`exit_ts`. That resolution was a real strategy decision, but the
   *broker-side close never actually executed* — the recorded exit price is fictional, and any
   P&L already attributed to that trade in stats/Retrospective is wrong by that amount.

4. **Do not silently "fix" anything found this way** — the correct remediation depends on intent
   the audit itself can't determine (close for real now vs. reopen at the true original entry
   vs. flag-only pending manual review — see HANDOFF.md 2026-08-13 for how each was reasoned
   through). Report findings and let the account owner decide per-position, the same way this
   session did via `AskUserQuestion` before touching anything.

### When to run this
- Before disabling any deployment that's currently the sole consumer of an account (native →
  Docker cutover, or vice versa).
- Before adding a second deployment as a consumer of an account that already has real history.
- Periodically as a standing health check even with no migration planned — this bug class can
  recur any time `sync_closures()` (or an equivalent broker-side-close-detection function) has
  a gap, and reconcile's own status-only check will not catch it.
