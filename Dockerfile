# Parallel WSL2/Docker deployment (2026-08-11) -- runs ALONGSIDE the existing native Windows
# Task Scheduler deployment (dashboard.ps1/run_dashboard_live.ps1), does not replace it. See
# HANDOFF.md for the full deployment rationale (this session's Task-Scheduler pain: UAC walls,
# a silently-deleted scheduled task, console flashes, duplicate-process races).
FROM python:3.11-slim

# uv itself (same tool the native deployment already uses -- pyproject.toml/uv.lock stay the
# single source of truth for dependencies across both deployment paths).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Dependency layer first so code-only changes don't invalidate the (slow) install layer.
COPY pyproject.toml uv.lock ./
# MetaTrader5 is Windows-only (sys_platform == 'win32' marker in pyproject.toml) -- uv
# correctly skips it on this Linux image, no special-casing needed.
RUN uv sync --frozen --no-dev

# dashboard/ imports several top-level modules (analyst/, data.py, engine.py, etc.) --
# confirmed via grep across dashboard/ for `from {analyst,data,engine,metrics,strategies,
# walkforward,costs} import` -- copy the whole top-level project, not just dashboard/, or
# imports fail at container startup (ModuleNotFoundError: analyst, found live on first run).
COPY analyst/ analyst/
COPY dashboard/ dashboard/
COPY data.py engine.py metrics.py strategies.py walkforward.py costs.py ./

# DASH_PORT is read by dashboard/app.py at import time (ui.run(port=_DASH_PORT)) -- keep the
# in-container port fixed and distinct from both native deployments' 8080/8081; the HOST port
# mapping (which port this is reachable at from Windows) is chosen in docker-compose.yml, kept
# independent of this value on purpose.
ENV DASH_PORT=8090
EXPOSE 8090

CMD ["uv", "run", "--frozen", "python", "-m", "dashboard.app"]
