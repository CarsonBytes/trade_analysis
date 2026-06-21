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
