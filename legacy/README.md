# Retired native Windows deployment

This directory holds launch scripts from the pre-Docker deployment of this project. They
are **not referenced by anything active** -- the only scheduled task that ever named them
(`DashboardAppLive`) is `Disabled`, confirmed live 2026-09-14 via a direct Task Scheduler
query. Kept here rather than deleted so the migration stays findable by anyone grepping the
working tree; full history is preserved (`git log --follow` on any file here still shows its
original life at the repo root).

## What replaced this

The current deployment is WSL2/Docker, driven by `docker-compose.yml` (paper) and
`docker-compose.live.yml` (live) at the repo root. See `README.md`'s "Infrastructure &
deployment" section and `HANDOFF.md` for the full migration history.

Windows-side supervision that's still genuinely in use (not retired) lives at the repo root,
not here: `watchdog.ps1` / `watchdog-start.vbs` / `install-watchdog.ps1` supervise the Docker
deployment from the Windows side (repointed 2026-09-12; see their own headers).

## What's in here

- `run_dashboard_live.ps1`, `run-dashboard-live-task.vbs` -- launched the LIVE dashboard as a
  native Windows process (`DashboardAppLive` scheduled task). `dashboard.ps1` (the paper
  counterpart, `DashboardApp` task) had already been deleted before this cleanup.
- `fix-tunnel-route.ps1`, `fix-tunnel-route-live.ps1` -- one-off Cloudflare Tunnel route
  repairs for the native deployment's ports (8080/8081), superseded by the tunnel now
  pointing at the Docker deployment's ports (18080/18081) instead.

## If you're reverting to native

Don't just copy these back -- multiple things changed underneath them since they were
retired (env var wiring, IB clientId assignments, the gateway login watchdog). Read
`HANDOFF.md`'s migration entries first, and treat this as a reference for what the old
shape looked like, not a ready-to-run restore.
