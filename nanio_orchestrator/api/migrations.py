"""rclone migration API endpoints.

Endpoints:
  POST   /api/migrations                        — start a new migration
  GET    /api/migrations                        — list all migrations
  GET    /api/migrations/{id}                   — get migration details
  POST   /api/migrations/{id}/cancel            — cancel a running migration
  GET    /api/migrations/{id}/log               — get migration log entries
"""

from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, HTTPException, Query

from nanio_orchestrator.config import get_settings
from nanio_orchestrator.db import get_db_ctx
from nanio_orchestrator.migration_engine import (
    cancel_migration,
    get_active_count,
    start_migration,
)
from nanio_orchestrator.models import (
    MigrationLogEntry,
    RcloneMigrationCreate,
    RcloneMigrationOut,
)

router = APIRouter(prefix="/api/migrations", tags=["migrations"])
logger = logging.getLogger(__name__)


@router.post("", response_model=RcloneMigrationOut, status_code=201)
async def create_migration(body: RcloneMigrationCreate):
    """Start a new rclone-based migration."""
    s = get_settings()

    # Enforce parallel limit
    if get_active_count() >= s.migration_max_parallel:
        raise HTTPException(
            429,
            f"Max parallel migrations reached ({s.migration_max_parallel}). "
            "Wait for a running migration to finish or cancel one.",
        )

    # Validate pools exist and are distinct
    if body.src_pool_id == body.dst_pool_id:
        raise HTTPException(
            400,
            "Source and destination pools must be different. "
            "Migrating a bucket to the same pool it already lives on would "
            "copy the bucket onto itself and then purge all its content.",
        )

    async with get_db_ctx() as db:
        for pid, label in [(body.src_pool_id, "Source"), (body.dst_pool_id, "Destination")]:
            rows = await db.execute_fetchall("SELECT id FROM pools WHERE id = ?", (pid,))
            if not rows:
                raise HTTPException(400, f"{label} pool {pid} not found")

        # Check we don't already have an active migration for same bucket+vhost
        # (Find any pending/copying/verifying migration for same vhost+bucket)
        # We need a vhost_id. The bucket might belong to multiple vhosts, so
        # we look for the first one. For the API, the caller must know which vhost.
        # Actually, the bucket's vhost_id can be inferred or must be given.
        # Let's derive it from bucket_sync if possible, or require the user to pass it.
        # For now, we'll check if there's already an active migration for any vhost.
        active = await db.execute_fetchall(
            """SELECT id FROM migrations
               WHERE bucket = ? AND phase IN ('pending','copying','verifying','switching')""",
            (body.bucket,),
        )
        if active:
            raise HTTPException(
                409,
                f"An active migration already exists for bucket '{body.bucket}' (id={active[0]['id']})",
            )

    # Find which vhost this bucket belongs to (from bucket_sync)
    async with get_db_ctx() as db:
        bs_rows = await db.execute_fetchall(
            "SELECT vhost_id FROM bucket_sync WHERE bucket = ? LIMIT 1",
            (body.bucket,),
        )

    if bs_rows:
        vhost_id = bs_rows[0]["vhost_id"]
    else:
        # No bucket_sync record — try to find a vhost with the source pool as default
        async with get_db_ctx() as db:
            vh_rows = await db.execute_fetchall(
                "SELECT id FROM vhosts WHERE default_pool_id = ? LIMIT 1",
                (body.src_pool_id,),
            )
        if not vh_rows:
            raise HTTPException(
                400,
                "Cannot determine vhost for this bucket. "
                "Ensure the bucket exists in bucket_sync or the source pool is a vhost default.",
            )
        vhost_id = vh_rows[0]["id"]

    migration_id = await start_migration(vhost_id, body.bucket, body.src_pool_id, body.dst_pool_id, body.mode)

    # Return the created migration
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT * FROM migrations WHERE id = ?", (migration_id,))
    m = dict(rows[0])
    return _to_out(m)


@router.get("", response_model=List[RcloneMigrationOut])
async def list_migrations(
    phase: str = Query(None, description="Filter by phase"),
    limit: int = Query(50, ge=1, le=500),
):
    """List migrations, optionally filtered by phase."""
    async with get_db_ctx() as db:
        if phase:
            rows = await db.execute_fetchall(
                "SELECT * FROM migrations WHERE phase = ? ORDER BY id DESC LIMIT ?",
                (phase, limit),
            )
        else:
            rows = await db.execute_fetchall(
                "SELECT * FROM migrations ORDER BY id DESC LIMIT ?", (limit,)
            )
    return [_to_out(dict(r)) for r in rows]


@router.get("/{migration_id}", response_model=RcloneMigrationOut)
async def get_migration(migration_id: int):
    """Get details of a single migration."""
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM migrations WHERE id = ?", (migration_id,)
        )
    if not rows:
        raise HTTPException(404, "Migration not found")
    return _to_out(dict(rows[0]))


@router.post("/{migration_id}/cancel")
async def cancel(migration_id: int):
    """Cancel a running migration."""
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall(
            "SELECT phase FROM migrations WHERE id = ?", (migration_id,)
        )
    if not rows:
        raise HTTPException(404, "Migration not found")

    phase = rows[0]["phase"]
    if phase in ("done", "error", "cancelled"):
        raise HTTPException(400, f"Migration is already in terminal state: {phase}")

    await cancel_migration(migration_id)
    return {"ok": True, "migration_id": migration_id}


@router.get("/{migration_id}/log", response_model=List[MigrationLogEntry])
async def get_log(migration_id: int, limit: int = Query(100, ge=1, le=1000)):
    """Get log entries for a migration."""
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM migration_log WHERE migration_id = ? ORDER BY id DESC LIMIT ?",
            (migration_id, limit),
        )
        if not rows:
            # Check migration exists
            m = await db.execute_fetchall("SELECT id FROM migrations WHERE id = ?", (migration_id,))
            if not m:
                raise HTTPException(404, "Migration not found")
    return [
        MigrationLogEntry(
            id=r["id"],
            migration_id=r["migration_id"],
            phase=r["phase"],
            message=r["message"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


def _to_out(m: dict) -> RcloneMigrationOut:
    return RcloneMigrationOut(
        id=m["id"],
        vhost_id=m["vhost_id"],
        bucket=m["bucket"],
        src_pool_id=m["src_pool_id"],
        dst_pool_id=m["dst_pool_id"],
        mode=m.get("mode", "copy"),
        phase=m["phase"],
        objects_total=m["objects_total"],
        objects_done=m["objects_done"],
        bytes_total=m["bytes_total"],
        bytes_done=m["bytes_done"],
        error_msg=m.get("error_msg"),
        started_at=m.get("started_at"),
        finished_at=m.get("finished_at"),
        created_at=m["created_at"],
    )
