"""rclone migration API endpoints.

Endpoints:
  POST   /api/migrations                        — start a new migration
  GET    /api/migrations                        — list all migrations
  GET    /api/migrations/stale                  — active migrations that cannot proceed safely
  GET    /api/migrations/orphaned               — completed migrations with leftover source data
  GET    /api/migrations/{id}                   — get migration details
  POST   /api/migrations/{id}/cancel            — cancel a running migration
  GET    /api/migrations/{id}/log               — get migration log entries
"""

from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, HTTPException, Query

from nanio_orchestrator.audit_log import log_audit
from nanio_orchestrator.credentials import get_pool_s3_params
from nanio_orchestrator.db import get_db_ctx
from nanio_orchestrator.migration_engine import (
    cancel_migration,
    start_migration,
)
from nanio_orchestrator.models import (
    MigrationLogEntry,
    OrphanedMigrationOut,
    RcloneMigrationCreate,
    RcloneMigrationOut,
    StaleMigrationOut,
)
from nanio_orchestrator.s3client import bucket_exists, bucket_has_objects

router = APIRouter(prefix="/api/migrations", tags=["migrations"])
logger = logging.getLogger(__name__)


@router.post("", response_model=RcloneMigrationOut, status_code=201)
async def create_migration(body: RcloneMigrationCreate):
    """Start a new rclone-based migration."""

    # Validate pools exist and are distinct
    if body.src_pool_id == body.dst_pool_id:
        raise HTTPException(
            400,
            "Source and destination pools must be different. "
            "Migrating a bucket to the same pool it already lives on is not allowed.",
        )

    src_member: str | None = None

    async with get_db_ctx() as db:
        for pid, label in [(body.src_pool_id, "Source"), (body.dst_pool_id, "Destination")]:
            rows = await db.execute_fetchall("SELECT id, name, type FROM pools WHERE id = ?", (pid,))
            if not rows:
                raise HTTPException(400, f"{label} pool {pid} not found")
            pool_row = dict(rows[0])
            if pool_row["type"] != "nanio":
                raise HTTPException(
                    400,
                    f"{label} pool '{pool_row['name']}' is of type '{pool_row['type']}'. "
                    "Migrations can only be performed between nanio pools.",
                )

        # Both pools must have at least one enabled member
        src_member_rows = await db.execute_fetchall(
            "SELECT address FROM pool_members WHERE pool_id = ? AND enabled = 1 ORDER BY id LIMIT 1",
            (body.src_pool_id,),
        )
        if not src_member_rows:
            raise HTTPException(
                400,
                f"Source pool {body.src_pool_id} has no enabled members — "
                "cannot validate source bucket before migrating.",
            )
        src_member = dict(src_member_rows[0])["address"]

        dst_member_rows = await db.execute_fetchall(
            "SELECT address FROM pool_members WHERE pool_id = ? AND enabled = 1 ORDER BY id LIMIT 1",
            (body.dst_pool_id,),
        )
        if not dst_member_rows:
            raise HTTPException(
                400,
                f"Destination pool {body.dst_pool_id} has no enabled members.",
            )

        # Reject if an active migration for the same bucket already exists
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

        # Resolve vhost inside the same DB context
        bs_rows = await db.execute_fetchall(
            "SELECT vhost_id, status FROM bucket_sync WHERE bucket = ? LIMIT 1",
            (body.bucket,),
        )
        if bs_rows:
            vhost_id: int = bs_rows[0]["vhost_id"]
            bs_status = bs_rows[0]["status"]
            if bs_status == "deleted":
                raise HTTPException(
                    400,
                    f"Bucket '{body.bucket}' no longer exists on source pool. "
                    "Remove the orphaned route and verify data before migrating.",
                )
        else:
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

        # A migration from the Migrations page REQUIRES a route to exist.
        # The migration engine updates the route during the 'switching' phase;
        # without a route the nginx config never changes and data becomes orphaned.
        route_rows = await db.execute_fetchall(
            "SELECT id, pool_id FROM routes WHERE vhost_id = ? AND path_prefix = ?",
            (vhost_id, f"/{body.bucket}/"),
        )
        if not route_rows:
            raise HTTPException(
                400,
                f"No nginx route found for bucket '{body.bucket}' in vhost {vhost_id}. "
                "Route the bucket first via the Buckets page before creating a migration.",
            )
        route_row = dict(route_rows[0])
        resolved_route_id: int = route_row["id"]

        # Source pool must match the pool that the route currently points to.
        # A mismatch means the data to copy is on a different pool than the one
        # nginx is proxying requests to — undefined behaviour during switching.
        if route_row["pool_id"] != body.src_pool_id:
            pool_name_rows = await db.execute_fetchall("SELECT name FROM pools WHERE id = ?", (route_row["pool_id"],))
            current_pool_name = pool_name_rows[0]["name"] if pool_name_rows else str(route_row["pool_id"])
            raise HTTPException(
                400,
                f"Source pool mismatch: bucket '{body.bucket}' is currently routed to "
                f"pool '{current_pool_name}' (id={route_row['pool_id']}), not pool {body.src_pool_id}. "
                "Set src_pool_id to the pool that currently serves this bucket.",
            )

        # ── Cascade warnings: check if http vhosts linked to src pool have routes ──
        cascade_warnings: list = []
        linked_http_pools = await db.execute_fetchall(
            "SELECT id, name FROM pools WHERE source_nanio_pool_id = ? AND type = 'http'",
            (body.src_pool_id,),
        )
        for hp in linked_http_pools:
            hp_dict = dict(hp)
            http_vhost_routes = await db.execute_fetchall(
                """SELECT v.server_name FROM vhosts v
                   JOIN routes r ON r.vhost_id = v.id
                   WHERE r.pool_id = ? AND r.path_prefix = ?""",
                (hp_dict["id"], f"/{body.bucket}/"),
            )
            if not http_vhost_routes:
                cascade_warnings.append(
                    f"http pool '{hp_dict['name']}' (id={hp_dict['id']}) is linked to this nanio pool "
                    f"but has no route for /{body.bucket}/ — cascade will be skipped for this pool. "
                    "Add the route via Bucket Management before starting migration for full coverage."
                )

    # ── S3 pre-flight: verify bucket exists and has data on the source pool ──
    # This prevents creating migrations that will immediately fail because the
    # source data is absent or already been moved. Done *outside* the DB context
    # so no connection is held during network I/O.
    src_ak, src_sk, _ = await get_pool_s3_params(body.src_pool_id)
    try:
        src_bucket_found = await bucket_exists(src_member, body.bucket, access_key=src_ak, secret_key=src_sk)
        if not src_bucket_found:
            raise HTTPException(
                400,
                f"Bucket '{body.bucket}' does not exist on source pool {body.src_pool_id}. "
                "Verify the source pool and bucket name before starting a migration.",
            )
        src_has_data = await bucket_has_objects(src_member, body.bucket, access_key=src_ak, secret_key=src_sk)
        if not src_has_data:
            raise HTTPException(
                400,
                f"Bucket '{body.bucket}' is empty on source pool {body.src_pool_id} — "
                "nothing to migrate. Verify the source pool contains the data to be moved.",
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            503,
            f"Cannot reach source pool {body.src_pool_id} to validate bucket '{body.bucket}': {exc}",
        )

    try:
        migration_id = await start_migration(
            vhost_id,
            body.bucket,
            body.src_pool_id,
            body.dst_pool_id,
            body.mode,
            route_id=resolved_route_id,
        )
    except RuntimeError as e:
        raise HTTPException(429, str(e))

    # Return the created migration
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT * FROM migrations WHERE id = ?", (migration_id,))
        await log_audit(
            db,
            "create_migration",
            "migration",
            migration_id,
            after={
                "bucket": body.bucket,
                "src_pool_id": body.src_pool_id,
                "dst_pool_id": body.dst_pool_id,
                "mode": body.mode,
                "vhost_id": vhost_id,
                "route_id": resolved_route_id,
            },
        )
        await db.commit()
    m = dict(rows[0])
    out = _to_out(m)
    if cascade_warnings:
        out.cascade_warnings = cascade_warnings
    return out


@router.get("/source-buckets")
async def list_source_buckets(pool_id: int):
    """Return bucket names currently served by a given pool.

    Used by the UI to populate the bucket select-box when the operator
    picks a source pool for a new migration.

    A bucket is "served by pool_id" when:
      • its ``routed_pool_id`` equals pool_id (dedicated route), OR
      • it is unrouted/migrating/ignored AND the vhost's default_pool_id
        equals pool_id (falls back to the default pool).
    """
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall(
            """
            SELECT DISTINCT bs.bucket
            FROM bucket_sync bs
            WHERE
                bs.routed_pool_id = :pid
                AND bs.status NOT IN ('deleted', 'unrouted', 'ignored')
            ORDER BY bs.bucket
            """,
            {"pid": pool_id},
        )
    return {"pool_id": pool_id, "buckets": [r["bucket"] for r in rows]}


@router.get("/orphaned", response_model=List[OrphanedMigrationOut])
async def list_orphaned_migrations():
    """List all migrations that have orphaned source data.

    Orphaned data is source-bucket content that was NOT deleted after migration
    completed — by design, this system never deletes bucket data automatically.
    Operators use this list to decide when and how to clean up source buckets.
    """
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall(
            """SELECT m.id, m.bucket, m.src_pool_id, m.dst_pool_id,
                      m.orphaned_source_pool_id, m.orphaned_source_prefix,
                      m.orphaned_at, m.finished_at
               FROM migrations m
               WHERE m.orphaned_source_pool_id IS NOT NULL
               ORDER BY m.orphaned_at DESC"""
        )
    return [
        OrphanedMigrationOut(
            migration_id=r["id"],
            bucket=r["bucket"],
            src_pool_id=r["src_pool_id"],
            dst_pool_id=r["dst_pool_id"],
            orphaned_source_pool_id=r["orphaned_source_pool_id"],
            orphaned_source_prefix=r["orphaned_source_prefix"],
            orphaned_at=r["orphaned_at"],
            finished_at=r["finished_at"],
        )
        for r in rows
    ]


_ACTIVE_PHASES = ("pending", "copying", "write_routing", "verifying", "switching")


@router.get("/stale", response_model=List[StaleMigrationOut])
async def list_stale_migrations():
    """List active migrations that cannot proceed safely.

    Checks every migration that is not in a terminal state (done/error/cancelled)
    for two classes of problem, mirroring the orphan-detection logic used for
    bucket routing:

    * **DB-level**: source or destination pool has no enabled members — rclone
      would have nothing to connect to.
    * **S3-level**: source bucket no longer exists on the source pool — the data
      to be moved is gone.  S3 checks are skipped for unreachable pools and for
      phases where the source bucket is no longer the read target (switching).

    Transient network errors during S3 probing are swallowed — the endpoint
    never marks a migration stale based on a failed probe.
    """
    placeholders = ",".join("?" for _ in _ACTIVE_PHASES)
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall(
            f"""SELECT m.id, m.bucket, m.src_pool_id, m.dst_pool_id, m.phase, m.created_at,
                       (SELECT COUNT(*) FROM pool_members pm
                        WHERE pm.pool_id = m.src_pool_id AND pm.enabled = 1) AS src_members,
                       (SELECT COUNT(*) FROM pool_members pm
                        WHERE pm.pool_id = m.dst_pool_id AND pm.enabled = 1) AS dst_members,
                       (SELECT pm.address FROM pool_members pm
                        WHERE pm.pool_id = m.src_pool_id AND pm.enabled = 1
                        ORDER BY pm.id LIMIT 1) AS src_member_addr
                FROM migrations m
                WHERE m.phase IN ({placeholders})
                ORDER BY m.id DESC""",
            _ACTIVE_PHASES,
        )

    stale: list[StaleMigrationOut] = []
    for r in rows:
        rd = dict(r)
        mid = rd["id"]
        bucket = rd["bucket"]

        if rd["src_members"] == 0:
            stale.append(
                StaleMigrationOut(
                    migration_id=mid,
                    bucket=bucket,
                    src_pool_id=rd["src_pool_id"],
                    dst_pool_id=rd["dst_pool_id"],
                    phase=rd["phase"],
                    reason="src_no_members",
                    created_at=rd["created_at"],
                )
            )
            continue

        if rd["dst_members"] == 0:
            stale.append(
                StaleMigrationOut(
                    migration_id=mid,
                    bucket=bucket,
                    src_pool_id=rd["src_pool_id"],
                    dst_pool_id=rd["dst_pool_id"],
                    phase=rd["phase"],
                    reason="dst_no_members",
                    created_at=rd["created_at"],
                )
            )
            continue

        # S3-level check: does the source bucket still exist?
        # Skip for 'switching' phase — writes already go to dst at that point.
        src_addr = rd.get("src_member_addr")
        if src_addr and rd["phase"] != "switching":
            try:
                src_ak, src_sk, _ = await get_pool_s3_params(rd["src_pool_id"])
                exists = await bucket_exists(src_addr, bucket, access_key=src_ak, secret_key=src_sk)
                if not exists:
                    stale.append(
                        StaleMigrationOut(
                            migration_id=mid,
                            bucket=bucket,
                            src_pool_id=rd["src_pool_id"],
                            dst_pool_id=rd["dst_pool_id"],
                            phase=rd["phase"],
                            reason="src_bucket_missing",
                            created_at=rd["created_at"],
                        )
                    )
            except Exception:
                # Transient network error — do not flag as stale to avoid false positives
                logger.debug(
                    "migration %d: S3 probe for stale check failed (src=%s bucket=%s), skipping",
                    mid,
                    src_addr,
                    bucket,
                )

    return stale


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
            rows = await db.execute_fetchall("SELECT * FROM migrations ORDER BY id DESC LIMIT ?", (limit,))
    return [_to_out(dict(r)) for r in rows]


@router.get("/{migration_id}", response_model=RcloneMigrationOut)
async def get_migration(migration_id: int):
    """Get details of a single migration."""
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT * FROM migrations WHERE id = ?", (migration_id,))
    if not rows:
        raise HTTPException(404, "Migration not found")
    return _to_out(dict(rows[0]))


@router.post("/{migration_id}/cancel")
async def cancel(migration_id: int):
    """Cancel a running migration."""
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT phase FROM migrations WHERE id = ?", (migration_id,))
    if not rows:
        raise HTTPException(404, "Migration not found")

    phase = rows[0]["phase"]
    if phase in ("done", "error", "cancelled"):
        raise HTTPException(400, f"Migration is already in terminal state: {phase}")

    await cancel_migration(migration_id)
    async with get_db_ctx() as db:
        await log_audit(db, "cancel_migration", "migration", migration_id, before={"phase": phase})
        await db.commit()
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
        orphaned_source_pool_id=m.get("orphaned_source_pool_id"),
        orphaned_source_prefix=m.get("orphaned_source_prefix"),
        orphaned_at=m.get("orphaned_at"),
    )
