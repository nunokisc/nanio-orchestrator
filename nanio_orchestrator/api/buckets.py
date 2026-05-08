"""Bucket sync and promotion API.

All endpoints are nested under /api/vhosts/{vhost_id}/buckets.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import List

import aiofiles
from fastapi import APIRouter, HTTPException

from nanio_orchestrator.audit_log import log_audit
from nanio_orchestrator.bucket_sync import sync_vhost_buckets_once
from nanio_orchestrator.config import get_settings
from nanio_orchestrator.credentials import get_pool_s3_params
from nanio_orchestrator.db import get_db_ctx
from nanio_orchestrator.migration_engine import start_migration as engine_start_migration
from nanio_orchestrator.models import BucketListOut, BucketPromoteRequest
from nanio_orchestrator.nginx.executor import test_config
from nanio_orchestrator.nginx.generator import generate_vhost_config, record_file_state
from nanio_orchestrator.s3client import (
    bucket_has_objects,
    count_objects,
    delete_object,
    list_objects,
)

router = APIRouter(prefix="/api/vhosts", tags=["buckets"])
logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _require_vhost_with_default_pool(vhost_id: int, db):
    rows = await db.execute_fetchall("SELECT * FROM vhosts WHERE id = ?", (vhost_id,))
    if not rows:
        raise HTTPException(404, "Vhost not found")
    vhost = dict(rows[0])
    if not vhost.get("default_pool_id"):
        raise HTTPException(400, "Vhost has no default_pool_id configured")
    return vhost


async def _require_vhost(vhost_id: int, db):
    """Return vhost dict without requiring a default_pool_id."""
    rows = await db.execute_fetchall("SELECT * FROM vhosts WHERE id = ?", (vhost_id,))
    if not rows:
        raise HTTPException(404, "Vhost not found")
    return dict(rows[0])


async def _first_enabled_member(pool_id: int, db) -> str:
    rows = await db.execute_fetchall(
        "SELECT address FROM pool_members WHERE pool_id = ? AND enabled = 1 ORDER BY id LIMIT 1",
        (pool_id,),
    )
    if not rows:
        raise HTTPException(400, f"Pool {pool_id} has no enabled members")
    return dict(rows[0])["address"]


async def _all_enabled_members(pool_id: int, db) -> List[str]:
    rows = await db.execute_fetchall(
        "SELECT address FROM pool_members WHERE pool_id = ? AND enabled = 1 ORDER BY id",
        (pool_id,),
    )
    return [dict(r)["address"] for r in rows]


async def _write_vhost_config(vhost_id: int, db) -> tuple:
    """Generate, test, write config to disk, record in DB. Returns (ok, output).
    Does NOT reload nginx — the operator applies changes via the Config tab.
    Exception: called from migration engine paths where reload is done separately.
    """
    filepath, content = await generate_vhost_config(vhost_id)
    tmp = filepath + ".tmp"
    async with aiofiles.open(tmp, "w") as f:
        await f.write(content)

    test_result = await test_config()
    if not test_result.ok:
        os.unlink(tmp)
        return False, test_result.output

    os.rename(tmp, filepath)
    await record_file_state(db, filepath, content)
    await db.commit()
    return True, f"nginx -t: {test_result.output} (config written — apply via Config tab)"


# ── List buckets ──────────────────────────────────────────────────────────────


@router.get("/{vhost_id}/buckets", response_model=BucketListOut)
async def list_vhost_buckets(vhost_id: int, fetch_counts: bool = False):
    """List all tracked buckets for this vhost with routing status."""
    s = get_settings()
    async with get_db_ctx() as db:
        vhost = await _require_vhost_with_default_pool(vhost_id, db)
        default_member = await _first_enabled_member(vhost["default_pool_id"], db)

        rows = await db.execute_fetchall(
            """SELECT bs.bucket, bs.status, bs.discovered_at, bs.routed_pool_id,
                      p.name as pool_name
               FROM bucket_sync bs
               LEFT JOIN pools p ON bs.routed_pool_id = p.id
               WHERE bs.vhost_id = ?
               ORDER BY bs.status, bs.bucket""",
            (vhost_id,),
        )

    last_synced = None
    buckets = []
    for r in rows:
        rd = dict(r)
        obj_count: int | None = None
        if fetch_counts and rd["status"] in ("unrouted", "migrating"):
            try:
                obj_count = await count_objects(
                    default_member,
                    rd["bucket"],
                    access_key=s.s3_access_key,
                    secret_key=s.s3_secret_key,
                )
            except Exception:
                pass
        buckets.append(
            {
                "name": rd["bucket"],
                "status": rd["status"],
                "pool_name": rd.get("pool_name"),
                "routed_pool_id": rd.get("routed_pool_id"),
                "object_count": obj_count,
                "discovered_at": rd["discovered_at"],
            }
        )
        if last_synced is None or (rd["discovered_at"] and rd["discovered_at"] > last_synced):
            last_synced = rd["discovered_at"]

    return BucketListOut(vhost_id=vhost_id, buckets=buckets, last_synced_at=last_synced)


# ── Trigger sync ──────────────────────────────────────────────────────────────


@router.post("/{vhost_id}/buckets/sync")
async def trigger_bucket_sync(vhost_id: int):
    """Immediately sync bucket list from nanio-default for this vhost."""
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT id FROM vhosts WHERE id = ?", (vhost_id,))
        if not rows:
            raise HTTPException(404, "Vhost not found")
    result = await sync_vhost_buckets_once(vhost_id)
    async with get_db_ctx() as db:
        await log_audit(
            db, "sync_buckets", "vhost", vhost_id, after={"found": result.get("found"), "new": result.get("new")}
        )
        await db.commit()
    return result


# ── Promote ───────────────────────────────────────────────────────────────────


@router.post("/{vhost_id}/buckets/{bucket}/promote")
async def promote_bucket(vhost_id: int, bucket: str, body: BucketPromoteRequest):
    """Promote a bucket to a dedicated pool by creating an nginx route.

    Creates a route /{bucket}/ → target pool in DB and writes the nginx config
    (validated with nginx -t). The operator applies changes via the Config tab.
    Bucket creation on the target pool is the responsibility of the S3 service
    user; the migration engine will create the bucket if needed during migration.
    Optionally kicks off a migration when migrate=true.
    """
    async with get_db_ctx() as db:
        vhost = await _require_vhost_with_default_pool(vhost_id, db)

        # Validate target pool
        pool_rows = await db.execute_fetchall("SELECT * FROM pools WHERE id = ?", (body.pool_id,))
        if not pool_rows:
            raise HTTPException(400, f"Pool {body.pool_id} not found")
        pool = dict(pool_rows[0])

        # Check route doesn't already exist
        existing_route = await db.execute_fetchall(
            "SELECT id FROM routes WHERE vhost_id = ? AND path_prefix = ?",
            (vhost_id, f"/{bucket}/"),
        )
        if existing_route:
            raise HTTPException(409, f"Route /{bucket}/ already exists for this vhost")

        # Reject deleted buckets — they no longer exist on S3
        bs_rows = await db.execute_fetchall(
            "SELECT status FROM bucket_sync WHERE vhost_id = ? AND bucket = ?",
            (vhost_id, bucket),
        )
        if bs_rows and dict(bs_rows[0])["status"] == "deleted":
            raise HTTPException(
                400,
                f"Bucket '{bucket}' no longer exists on the source pool and cannot be promoted. "
                "Remove the bucket_sync record via DELETE .../buckets/{bucket}/route if a route exists.",
            )

        default_pool_id = vhost["default_pool_id"]

        # Refuse to migrate when target is already the default pool
        if body.migrate and body.pool_id == default_pool_id:
            raise HTTPException(
                400,
                f"Cannot migrate bucket '{bucket}' to the default pool — "
                "the bucket already lives there. Select a different destination pool.",
            )

        if body.migrate:
            # Migration needs at least one member on the target pool to proceed
            target_members = await _all_enabled_members(body.pool_id, db)
            if not target_members:
                raise HTTPException(400, "Target pool has no enabled members")

        default_member = await _first_enabled_member(default_pool_id, db)

    default_ak, default_sk, _ = await get_pool_s3_params(default_pool_id)

    # ── Pre-flight: check whether the bucket already has objects on the source pool ──
    src_has_data = False
    try:
        src_has_data = await bucket_has_objects(default_member, bucket, access_key=default_ak, secret_key=default_sk)
    except Exception as exc:
        logger.warning(
            "promote %s (vhost %d): could not check source bucket contents (%s) — proceeding",
            bucket,
            vhost_id,
            exc,
        )

    if src_has_data and not body.migrate:
        if body.pool_id != default_pool_id and not body.allow_orphan:
            raise HTTPException(
                400,
                f"Bucket '{bucket}' already has objects on the source pool. "
                "Routing to a different pool without migration will leave existing data "
                "inaccessible via this route. Either enable 'Migrate existing objects' "
                "or re-submit with allow_orphan=true to acknowledge data will remain on the source pool.",
            )

    # ── Create nginx route ────────────────────────────────────────────────
    # When migration is requested the route initially points to the SOURCE
    # (default) pool so users continue to see their files while the copy runs.
    # The migration engine's 'switching' phase will update the route to the
    # destination pool once the copy has been verified successfully.
    initial_route_pool_id = default_pool_id if body.migrate else body.pool_id

    async with get_db_ctx() as db:
        cursor = await db.execute(
            """INSERT INTO routes (vhost_id, path_prefix, pool_id, enabled)
               VALUES (?, ?, ?, 1)""",
            (vhost_id, f"/{bucket}/", initial_route_pool_id),
        )
        route_id = cursor.lastrowid
        await db.commit()

        ok, output = await _write_vhost_config(vhost_id, db)
        await log_audit(
            db,
            "promote_bucket",
            "route",
            route_id,
            after={
                "bucket": bucket,
                "pool_id": body.pool_id,
                "initial_route_pool_id": initial_route_pool_id,
                "migrate": body.migrate,
            },
            reload_ok=ok,
            reload_output=output,
        )

        # Update bucket_sync status
        await db.execute(
            """INSERT INTO bucket_sync (vhost_id, bucket, discovered_at, status, routed_pool_id)
               VALUES (?, ?, ?, 'routed', ?)
               ON CONFLICT(vhost_id, bucket) DO UPDATE SET
                 status = 'routed',
                 routed_pool_id = excluded.routed_pool_id""",
            (vhost_id, bucket, _now(), body.pool_id),
        )
        await db.commit()

    result = {
        "ok": ok,
        "output": output,
        "bucket": bucket,
        "pool": pool["name"],
        "route": f"/{bucket}/",
        "migration_started": False,
    }

    # ── Optionally start migration (via rclone engine) ───────────────────────────
    if body.migrate:
        migration_id = await engine_start_migration(vhost_id, bucket, default_pool_id, body.pool_id, route_id=route_id)
        result["migration_started"] = True
        result["migration_id"] = migration_id

    return result


# ── Ignore ────────────────────────────────────────────────────────────────────


@router.post("/{vhost_id}/buckets/{bucket}/ignore")
async def ignore_bucket(vhost_id: int, bucket: str):
    """Mark a bucket as ignored — won't appear as unrouted."""
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT id FROM vhosts WHERE id = ?", (vhost_id,))
        if not rows:
            raise HTTPException(404, "Vhost not found")

        await db.execute(
            """INSERT INTO bucket_sync (vhost_id, bucket, discovered_at, status)
               VALUES (?, ?, ?, 'ignored')
               ON CONFLICT(vhost_id, bucket) DO UPDATE SET status = 'ignored'""",
            (vhost_id, bucket, _now()),
        )
        await log_audit(db, "ignore_bucket", "bucket", None, after={"vhost_id": vhost_id, "bucket": bucket})
        await db.commit()
    return {"ok": True, "bucket": bucket, "status": "ignored"}


# ── Migration via rclone engine ───────────────────────────────────────────────────


@router.post("/{vhost_id}/buckets/{bucket}/migrate")
async def start_migration(vhost_id: int, bucket: str):
    """Start (or restart) object migration for a routed bucket via rclone engine."""
    from nanio_orchestrator.migration_engine import get_active_count

    s = get_settings()

    async with get_db_ctx() as db:
        vhost = await _require_vhost_with_default_pool(vhost_id, db)

        route_rows = await db.execute_fetchall(
            "SELECT pool_id FROM routes WHERE vhost_id = ? AND path_prefix = ?",
            (vhost_id, f"/{bucket}/"),
        )
        if not route_rows:
            raise HTTPException(400, f"No route found for /{bucket}/ — promote first")

        dst_pool_id = dict(route_rows[0])["pool_id"]
        src_pool_id = vhost["default_pool_id"]

        # Reject if bucket is deleted
        bs_rows = await db.execute_fetchall(
            "SELECT status FROM bucket_sync WHERE vhost_id = ? AND bucket = ?",
            (vhost_id, bucket),
        )
        if bs_rows and dict(bs_rows[0])["status"] == "deleted":
            raise HTTPException(
                400,
                f"Bucket '{bucket}' no longer exists on source pool. "
                "Remove the orphaned route and verify data before migrating.",
            )

    # Check parallel limit
    if get_active_count() >= s.migration_max_parallel:
        raise HTTPException(
            429,
            f"Max parallel migrations reached ({s.migration_max_parallel}). "
            "Wait for a running migration to finish or cancel one.",
        )

    if src_pool_id == dst_pool_id:
        raise HTTPException(
            400,
            f"Bucket '{bucket}' is already routed to the default pool — "
            "source and destination are the same pool. No migration needed.",
        )

    migration_id = await engine_start_migration(vhost_id, bucket, src_pool_id, dst_pool_id)
    async with get_db_ctx() as db:
        await log_audit(
            db,
            "start_migration",
            "migration",
            migration_id,
            after={"vhost_id": vhost_id, "bucket": bucket, "src_pool_id": src_pool_id, "dst_pool_id": dst_pool_id},
        )
        await db.commit()
    return {"ok": True, "migration_id": migration_id, "bucket": bucket}


# ── Orphan detection ──────────────────────────────────────────────────────────


@router.get("/{vhost_id}/buckets/orphans")
async def list_orphans(vhost_id: int):
    """Scan routed buckets for orphan content still on the default pool.

    A bucket is an orphan when it has been routed to a dedicated pool but the
    default pool's copy still contains objects.  This happens after every migration
    because source data is never deleted automatically — cleanup is the operator's
    responsibility.
    """
    async with get_db_ctx() as db:
        vhost = await _require_vhost_with_default_pool(vhost_id, db)
        default_pool_id = vhost["default_pool_id"]
        default_member = await _first_enabled_member(default_pool_id, db)
        routed_rows = await db.execute_fetchall(
            """SELECT bucket FROM bucket_sync
               WHERE vhost_id = ? AND status = 'routed'
                 AND routed_pool_id IS NOT NULL
                 AND routed_pool_id != ?""",
            (vhost_id, default_pool_id),
        )

    default_ak, default_sk, _ = await get_pool_s3_params(default_pool_id)

    orphans = []
    for row in routed_rows:
        bucket_name = row["bucket"]
        try:
            obj_count = await count_objects(
                default_member,
                bucket_name,
                access_key=default_ak,
                secret_key=default_sk,
            )
            if obj_count > 0:
                orphans.append({"bucket": bucket_name, "objects": obj_count})
        except Exception as exc:
            logger.warning("orphan scan: error checking bucket '%s': %s", bucket_name, exc)

    return {"vhost_id": vhost_id, "orphans": orphans, "checked": len(routed_rows)}


@router.post("/{vhost_id}/buckets/{bucket}/purge-orphan")
async def purge_orphan(vhost_id: int, bucket: str):
    """Delete all objects from the default pool's copy of a routed bucket.

    The bucket itself is preserved (needed for ListBuckets).  Only the object
    content is removed so the default pool no longer serves stale data.
    """
    async with get_db_ctx() as db:
        vhost = await _require_vhost_with_default_pool(vhost_id, db)
        default_pool_id = vhost["default_pool_id"]
        default_member = await _first_enabled_member(default_pool_id, db)

        bs_rows = await db.execute_fetchall(
            "SELECT status FROM bucket_sync WHERE vhost_id = ? AND bucket = ?",
            (vhost_id, bucket),
        )

    if not bs_rows or dict(bs_rows[0])["status"] != "routed":
        raise HTTPException(
            400,
            "Bucket is not in 'routed' status — only routed buckets can be orphan-purged",
        )

    default_ak, default_sk, _ = await get_pool_s3_params(default_pool_id)

    try:
        keys = await list_objects(default_member, bucket, access_key=default_ak, secret_key=default_sk)
    except Exception as exc:
        raise HTTPException(500, f"Failed to list objects in bucket '{bucket}': {exc}")

    deleted = 0
    errors: list = []
    for key in keys:
        try:
            ok = await delete_object(default_member, bucket, key, access_key=default_ak, secret_key=default_sk)
            if ok:
                deleted += 1
            else:
                errors.append(key)
        except Exception as exc:
            errors.append(key)
            logger.warning("purge_orphan: error deleting '%s/%s': %s", bucket, key, exc)

    logger.info(
        "purge_orphan: vhost %d bucket '%s': deleted %d/%d objects (%d errors)",
        vhost_id,
        bucket,
        deleted,
        len(keys),
        len(errors),
    )

    async with get_db_ctx() as db:
        await log_audit(
            db,
            "purge_orphan",
            "bucket",
            None,
            after={
                "vhost_id": vhost_id,
                "bucket": bucket,
                "deleted": deleted,
                "total": len(keys),
                "errors": len(errors),
            },
        )
        await db.commit()

    return {
        "ok": len(errors) == 0,
        "bucket": bucket,
        "deleted": deleted,
        "total": len(keys),
        "errors": errors[:20],
    }


# ── Remove orphaned route (for deleted buckets) ───────────────────────────────


@router.delete("/{vhost_id}/buckets/{bucket}/route", status_code=200)
async def remove_bucket_route(vhost_id: int, bucket: str):
    """Remove the nginx route for a bucket and revert bucket_sync to unrouted/deleted.

    Used to clean up orphaned routes when a bucket has been deleted from S3.
    """
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT id FROM vhosts WHERE id = ?", (vhost_id,))
        if not rows:
            raise HTTPException(404, "Vhost not found")

        route_rows = await db.execute_fetchall(
            "SELECT id FROM routes WHERE vhost_id = ? AND path_prefix = ?",
            (vhost_id, f"/{bucket}/"),
        )
        if not route_rows:
            raise HTTPException(404, f"No route found for /{bucket}/ on vhost {vhost_id}")

        route_id = route_rows[0]["id"]
        await db.execute("DELETE FROM routes WHERE id = ?", (route_id,))

        # Check if bucket_sync has a 'deleted' entry — if so, delete it; else mark unrouted
        bs_rows = await db.execute_fetchall(
            "SELECT status FROM bucket_sync WHERE vhost_id = ? AND bucket = ?",
            (vhost_id, bucket),
        )
        if bs_rows and dict(bs_rows[0])["status"] == "deleted":
            await db.execute(
                "DELETE FROM bucket_sync WHERE vhost_id = ? AND bucket = ?",
                (vhost_id, bucket),
            )
        else:
            await db.execute(
                """UPDATE bucket_sync SET status = 'unrouted', routed_pool_id = NULL
                   WHERE vhost_id = ? AND bucket = ?""",
                (vhost_id, bucket),
            )

        await db.commit()

        await log_audit(
            db,
            "remove_bucket_route",
            "route",
            route_id,
            after={"vhost_id": vhost_id, "bucket": bucket},
        )
        await db.commit()

    return {"ok": True, "bucket": bucket, "route_removed": f"/{bucket}/"}


# ── HTTP vhost bucket route management ───────────────────────────────────────


@router.get("/{vhost_id}/http-bucket-routes")
async def list_http_bucket_routes(vhost_id: int):
    """List routes on an http vhost and available buckets from its linked nanio pool.

    Only available for http vhosts that have source_nanio_pool_id configured.
    Returns existing routes and buckets on the linked nanio pool that lack a route.
    """
    async with get_db_ctx() as db:
        vhost_rows = await db.execute_fetchall(
            """SELECT v.*, p.name as pool_name, p.source_nanio_pool_id, p.type as pool_type
               FROM vhosts v
               LEFT JOIN pools p ON v.default_pool_id = p.id
               WHERE v.id = ?""",
            (vhost_id,),
        )
        if not vhost_rows:
            raise HTTPException(404, "Vhost not found")
        vhost = dict(vhost_rows[0])

        if vhost.get("pool_type") != "http" or not vhost.get("source_nanio_pool_id"):
            raise HTTPException(
                400,
                "Route management for http vhosts is only available when source_nanio_pool_id is configured",
            )

        nanio_pool_id = vhost["source_nanio_pool_id"]

        # Get existing routes on this vhost
        route_rows = await db.execute_fetchall(
            """SELECT r.id, r.path_prefix, r.pool_id, p.name as pool_name
               FROM routes r JOIN pools p ON r.pool_id = p.id
               WHERE r.vhost_id = ? ORDER BY r.path_prefix""",
            (vhost_id,),
        )
        existing_routes = [dict(r) for r in route_rows]
        routed_buckets = {r["path_prefix"].strip("/") for r in existing_routes}

        # Get buckets from the linked nanio pool via bucket_sync
        # (bucket_sync is on a nanio vhost, not this http vhost)
        nanio_vhost_rows = await db.execute_fetchall(
            "SELECT id FROM vhosts WHERE default_pool_id = ? LIMIT 1",
            (nanio_pool_id,),
        )
        nanio_buckets = []
        if nanio_vhost_rows:
            nanio_vhost_id = nanio_vhost_rows[0]["id"]
            bs_rows = await db.execute_fetchall(
                """SELECT bucket, status FROM bucket_sync
                   WHERE vhost_id = ? AND status NOT IN ('deleted', 'ignored')
                   ORDER BY bucket""",
                (nanio_vhost_id,),
            )
            for r in bs_rows:
                bname = r["bucket"]
                nanio_buckets.append(
                    {
                        "bucket": bname,
                        "has_route": bname in routed_buckets,
                    }
                )

        nanio_pool_rows = await db.execute_fetchall("SELECT name FROM pools WHERE id = ?", (nanio_pool_id,))
        nanio_pool_name = nanio_pool_rows[0]["name"] if nanio_pool_rows else str(nanio_pool_id)

    return {
        "vhost_id": vhost_id,
        "server_name": vhost["server_name"],
        "source_nanio_pool_id": nanio_pool_id,
        "source_nanio_pool_name": nanio_pool_name,
        "routes": existing_routes,
        "linked_nanio_buckets": nanio_buckets,
    }


@router.post("/{vhost_id}/http-bucket-routes/{bucket}", status_code=201)
async def add_http_bucket_route(vhost_id: int, bucket: str):
    """Create a route /{bucket}/ on an http vhost pointing to its default pool.

    Only available for http vhosts with source_nanio_pool_id configured.
    Does not create the bucket on the S3 backend — only creates the nginx route.
    """
    async with get_db_ctx() as db:
        vhost_rows = await db.execute_fetchall(
            """SELECT v.*, p.source_nanio_pool_id, p.type as pool_type
               FROM vhosts v LEFT JOIN pools p ON v.default_pool_id = p.id
               WHERE v.id = ?""",
            (vhost_id,),
        )
        if not vhost_rows:
            raise HTTPException(404, "Vhost not found")
        vhost = dict(vhost_rows[0])

        if vhost.get("pool_type") != "http" or not vhost.get("source_nanio_pool_id"):
            raise HTTPException(
                400,
                "Route management for http vhosts is only available when source_nanio_pool_id is configured",
            )
        if not vhost.get("default_pool_id"):
            raise HTTPException(400, "Vhost has no default_pool_id — cannot create a route")

        # Check route doesn't already exist
        existing = await db.execute_fetchall(
            "SELECT id FROM routes WHERE vhost_id = ? AND path_prefix = ?",
            (vhost_id, f"/{bucket}/"),
        )
        if existing:
            raise HTTPException(409, f"Route /{bucket}/ already exists on this vhost")

        cursor = await db.execute(
            "INSERT INTO routes (vhost_id, path_prefix, pool_id, enabled) VALUES (?, ?, ?, 1)",
            (vhost_id, f"/{bucket}/", vhost["default_pool_id"]),
        )
        route_id = cursor.lastrowid
        await db.commit()

        await log_audit(
            db,
            "add_http_bucket_route",
            "route",
            route_id,
            after={"vhost_id": vhost_id, "bucket": bucket, "pool_id": vhost["default_pool_id"]},
        )
        await db.commit()

    return {"ok": True, "bucket": bucket, "route": f"/{bucket}/", "pool_id": vhost["default_pool_id"]}


@router.delete("/{vhost_id}/http-bucket-routes/{bucket}", status_code=200)
async def remove_http_bucket_route(vhost_id: int, bucket: str):
    """Remove a route /{bucket}/ from an http vhost.

    Only available for http vhosts with source_nanio_pool_id configured.
    """
    async with get_db_ctx() as db:
        vhost_rows = await db.execute_fetchall(
            """SELECT v.*, p.source_nanio_pool_id, p.type as pool_type
               FROM vhosts v LEFT JOIN pools p ON v.default_pool_id = p.id
               WHERE v.id = ?""",
            (vhost_id,),
        )
        if not vhost_rows:
            raise HTTPException(404, "Vhost not found")
        vhost = dict(vhost_rows[0])

        if vhost.get("pool_type") != "http" or not vhost.get("source_nanio_pool_id"):
            raise HTTPException(
                400,
                "Route management for http vhosts is only available when source_nanio_pool_id is configured",
            )

        route_rows = await db.execute_fetchall(
            "SELECT id FROM routes WHERE vhost_id = ? AND path_prefix = ?",
            (vhost_id, f"/{bucket}/"),
        )
        if not route_rows:
            raise HTTPException(404, f"No route found for /{bucket}/ on this vhost")

        route_id = route_rows[0]["id"]
        await db.execute("DELETE FROM routes WHERE id = ?", (route_id,))
        await db.commit()

        await log_audit(
            db,
            "remove_http_bucket_route",
            "route",
            route_id,
            after={"vhost_id": vhost_id, "bucket": bucket},
        )
        await db.commit()

    return {"ok": True, "bucket": bucket, "route_removed": f"/{bucket}/"}
