"""Health check endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from nanio_orchestrator import __version__
from nanio_orchestrator.config import get_settings
from nanio_orchestrator.db import get_db_ctx
from nanio_orchestrator.models import HealthOut

router = APIRouter(tags=["health"])


@router.get("/api/health", response_model=HealthOut)
async def health():
    s = get_settings()
    db_ok = True
    drift_alerts = 0

    try:
        async with get_db_ctx() as db:
            await db.execute("SELECT 1")
            # Count drifted files
            rows = await db.execute_fetchall(
                "SELECT sha256_disk, sha256_db FROM config_files WHERE sha256_disk IS NOT NULL"
            )
            for r in rows:
                if r["sha256_disk"] != r["sha256_db"]:
                    drift_alerts += 1
    except Exception:
        db_ok = False

    return HealthOut(
        status="ok" if db_ok else "degraded",
        version=__version__,
        dev_mode=s.dev,
        db_ok=db_ok,
        nginx_config_dir=s.nginx_config_dir,
        drift_alerts=drift_alerts,
    )
