"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from nanio_orchestrator import __version__
from nanio_orchestrator.config import get_settings
from nanio_orchestrator.db import init_db
from nanio_orchestrator.drift import drift_loop, stop_drift
from nanio_orchestrator.bucket_sync import bucket_sync_loop, stop_bucket_sync
from nanio_orchestrator.migration_engine import recover_interrupted_migrations
from nanio_orchestrator.backup import backup_loop, stop_backup


logger = logging.getLogger(__name__)

# Paths that never require any authentication
_UNPROTECTED = {"/api/health", "/api/docs", "/api/redoc", "/login", "/logout"}
_UNPROTECTED_PREFIXES = ("/static/",)


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
    try:
        await drift_task
    except asyncio.CancelledError:
        pass

    stop_bucket_sync()
    bucket_sync_task.cancel()
    try:
        await bucket_sync_task
    except asyncio.CancelledError:
        pass

    stop_backup()
    backup_task.cancel()
    try:
        await backup_task
    except asyncio.CancelledError:
        pass

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
    )

    # ── Auth middleware ────────────────────────────────────────────────────
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        from nanio_orchestrator.auth import is_authenticated

        path = request.url.path

        # Static files and login endpoints: always pass through
        if path in _UNPROTECTED or any(path.startswith(p) for p in _UNPROTECTED_PREFIXES):
            return await call_next(request)

        # ── /api/* — header OR cookie auth ─────────────────────────────────
        if path.startswith("/api/"):
            key = request.headers.get("X-Orchestrator-Key", "")
            if key and hmac.compare_digest(key, s.api_key):
                return await call_next(request)
            # Also allow a browser session cookie (Web UI calls the API directly)
            if is_authenticated(request, s.api_key, s.session_ttl):
                return await call_next(request)
            return Response(
                content='{"detail":"Invalid or missing X-Orchestrator-Key"}',
                status_code=401,
                media_type="application/json",
            )

        # ── /web/* and / — cookie-based auth ─────────────────────────────
        if not is_authenticated(request, s.api_key, s.session_ttl):
            return RedirectResponse(url="/login", status_code=302)

        return await call_next(request)

    # ── API routers ───────────────────────────────────────────────────────
    from nanio_orchestrator.api.pools import router as pools_router
    from nanio_orchestrator.api.vhosts import router as vhosts_router
    from nanio_orchestrator.api.config import router as config_router
    from nanio_orchestrator.api.health import router as health_router
    from nanio_orchestrator.api.audit import router as audit_router
    from nanio_orchestrator.api.buckets import router as buckets_router
    from nanio_orchestrator.api.credentials import router as credentials_router
    from nanio_orchestrator.api.migrations import router as migrations_router

    app.include_router(pools_router)
    app.include_router(vhosts_router)
    app.include_router(config_router)
    app.include_router(health_router)
    app.include_router(audit_router)
    app.include_router(buckets_router)
    app.include_router(credentials_router)
    app.include_router(migrations_router)

    # ── Web UI (includes /login, /logout, /, /web/*) ──────────────────────
    from nanio_orchestrator.web.routes import router as web_router
    app.include_router(web_router)

    # ── Static files ──────────────────────────────────────────────────────
    static_dir = Path(__file__).parent / "web" / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    return app
