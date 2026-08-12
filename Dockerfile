# Parallel WSL2/Docker deployment (2026-08-11) -- runs ALONGSIDE the existing native Windows
# Task Scheduler deployment (dashboard.ps1/run_dashboard_live.ps1), does not replace it. See
# HANDOFF.md for the full deployment rationale (this session's Task-Scheduler pain: UAC walls,
# a silently-deleted scheduled task, console flashes, duplicate-process races).
#
# DIGEST-PINNED (2026-08-12), not `python:3.12-slim` / `uv:latest` -- this image is rebuilt
# automatically on every push (see .githooks/pre-push + scripts/wsl2-docker-deploy.sh). A
# floating tag means the SAME git commit could produce a DIFFERENT deployed image depending on
# what upstream happened to publish that day -- pinning makes "push this commit" deterministic:
# the only thing that changes the build output is a change to this repo. Bump deliberately
# (re-pull, re-pin, note why) rather than letting it drift silently.
#
# 3.12 (not 3.11) specifically to match study-platform's own Dockerfile (this machine's other
# WSL2/Docker project) -- Docker's local image cache is content-addressed (keyed by digest, not
# by reference string), so pinning quant to the SAME underlying base-OS digest study-platform
# already pulled means that one layer is stored once and shared between both, for free, with
# zero coupling to study-platform's own (separately pinned/floating) dependency layers above it.
FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

# uv itself (same tool the native deployment already uses -- pyproject.toml/uv.lock stay the
# single source of truth for dependencies across both deployment paths).
COPY --from=ghcr.io/astral-sh/uv@sha256:2d890623d310b57771ce840f0da5eed5fc6d657da05ffaa45d82797b53fa3abc /uv /uvx /usr/local/bin/

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
