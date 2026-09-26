"""Cross-instance shared cache (ADDED 2026-09-17): lets the paper instance
reuse work the live instance already paid for (LLM board scan, Supabase usage
reads, provider-decision polls) instead of re-requesting the identical thing.

Transport is a shared Docker volume (`quant_shared`, mounted at /shared on
both dashboards -- see docker-compose.yml / docker-compose.live.yml), NOT
Supabase: sharing must keep working through exactly the outages (e.g. the
2026-09-16 WSL2 443 blackout) that make sharing valuable, and a Supabase
table would add the read/write costs we're trying to remove.

Files are TTL-stamped JSON, written atomically (tmp + rename). Every read is
best-effort: missing/stale/corrupt/absent-mount all return a miss, and the
caller falls back to its own request -- today's behavior. A missing /shared
mount disables this module silently (fresh `os.path.isdir` check per call, so
no restart is needed if the volume is added later).

SHARED_DIR env overrides the mount point (tests point it at a tmp dir).
"""
from __future__ import annotations

import json
import os
import time

SCAN_KEY = "board_scan.json"
USAGE_KEY = "supabase_usage.json"
PROVIDER_KEY = "provider_decision.json"
BACKOFF_KEY = "rate_limit_backoff.json"


def shared_dir() -> str:
    return os.environ.get("SHARED_DIR", "/shared")


def available() -> bool:
    """Is the shared mount present and writable, right now."""
    try:
        d = shared_dir()
        return os.path.isdir(d) and os.access(d, os.W_OK)
    except Exception:
        return False


def _path(key: str) -> str:
    return os.path.join(shared_dir(), key)


def write(key: str, payload: dict) -> bool:
    """Atomic best-effort write. Returns False (never raises) when the mount
    is absent/read-only or the write fails -- the caller just skips sharing."""
    try:
        if not available():
            return False
        doc = {"ts": time.time(), "payload": payload}
        path = _path(key)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def read(key: str, max_age_sec: float) -> tuple[dict | None, float | None]:
    """(payload, age_sec) on a fresh hit, else (None, None). Never raises."""
    try:
        if not available():
            return None, None
        with open(_path(key), encoding="utf-8") as f:
            doc = json.load(f)
        ts = float(doc.get("ts", 0))
        age = time.time() - ts
        if age < 0 or age > max_age_sec:
            return None, None
        payload = doc.get("payload")
        if not isinstance(payload, dict):
            return None, None
        return payload, age
    except Exception:
        return None, None
