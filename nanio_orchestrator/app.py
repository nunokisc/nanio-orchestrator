"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request, Response

from nanio_orchestrator import __version__
from nanio_orchestrator.backup import backup_loop, stop_backup
from nanio_orchestrator.bucket_sync import bucket_sync_loop, stop_bucket_sync
from nanio_orchestrator.config import get_settings
from nanio_orchestrator.db import init_db
from nanio_orchestrator.drift import drift_loop, stop_drift
from nanio_orchestrator.migration_engine import recover_interrupted_migrations

logger = logging.getLogger(__name__)

# /api/* paths that need no authentication. Everything else under /api/ does; everything
# outside /api/ is the panel's shell and assets, which hold no data.
_UNPROTECTED = {
    "/api/health",
    "/api/docs",
    "/api/redoc",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/session",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic."""
    s = get_settings()
    s.ensure_dirs()

    # Init DB
    await init_db()
    logger.info("Database initialized at %s", s.db_path)

    # Start drift detection
    drift_task = asyncio.create_task(drift_loop())

    # Start bucket sync
    bucket_sync_task = asyncio.create_task(bucket_sync_loop())

    # Recover interrupted migrations
    await recover_interrupted_migrations()

    # Start DB backup loop
    backup_task = asyncio.create_task(backup_loop())

    if s.dev:
        logger.info(
            "nanio-orchestrator dev mode → http://localhost:%d  API key: dev",
            s.port,
        )
    else:
        logger.info("nanio-orchestrator started on port %d", s.port)

    yield

    # Shutdown
    stop_drift()
    drift_task.cancel()
    with suppress(asyncio.CancelledError):
        await drift_task

    stop_bucket_sync()
    bucket_sync_task.cancel()
    with suppress(asyncio.CancelledError):
        await bucket_sync_task

    stop_backup()
    backup_task.cancel()
    with suppress(asyncio.CancelledError):
        await backup_task

    logger.info("nanio-orchestrator stopped")


def create_app() -> FastAPI:
    """Build and return the FastAPI application."""
    s = get_settings()

    app = FastAPI(
        title="nanio-orchestrator",
        version=__version__,
        description="Nginx configuration manager for nanio S3 storage clusters",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        # under /api/ so that the schema is behind the same authentication as the API
        openapi_url="/api/openapi.json",
    )

    # ── Auth middleware ────────────────────────────────────────────────────
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        from nanio_orchestrator.auth import is_authorized

        path = request.url.path
        if not path.startswith("/api/") or path in _UNPROTECTED:
            return await call_next(request)

        # API key header, or the session cookie of the panel
        if is_authorized(request, s.api_key, s.session_ttl):
            return await call_next(request)
        return Response(
            content='{"detail":"Invalid or missing X-Orchestrator-Key"}',
            status_code=401,
            media_type="application/json",
        )

    # ── API routers ───────────────────────────────────────────────────────
    from nanio_orchestrator.api.audit import router as audit_router
    from nanio_orchestrator.api.auth import router as auth_router
    from nanio_orchestrator.api.buckets import router as buckets_router
    from nanio_orchestrator.api.config import router as config_router
    from nanio_orchestrator.api.credentials import router as credentials_router
    from nanio_orchestrator.api.explorer import router as explorer_router
    from nanio_orchestrator.api.health import router as health_router
    from nanio_orchestrator.api.migrations import router as migrations_router
    from nanio_orchestrator.api.overview import router as overview_router
    from nanio_orchestrator.api.pools import router as pools_router
    from nanio_orchestrator.api.vhosts import router as vhosts_router

    app.include_router(auth_router)
    app.include_router(pools_router)
    app.include_router(vhosts_router)
    app.include_router(config_router)
    app.include_router(health_router)
    app.include_router(overview_router)
    app.include_router(audit_router)
    app.include_router(buckets_router)
    app.include_router(credentials_router)
    app.include_router(migrations_router)
    app.include_router(explorer_router)

    # ── Web panel: every other path (last, it matches anything) ───────────
    from nanio_orchestrator.web.spa import router as panel_router

    app.include_router(panel_router)

    return app
