"""Bucket sync and promotion API.

All endpoints are nested under /api/vhosts/{vhost_id}/buckets.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Dict, List

import aiofiles
from fastapi import APIRouter, HTTPException

from nanio_orchestrator.bucket_sync import sync_vhost_buckets_once
from nanio_orchestrator.config import get_settings
from nanio_orchestrator.db import get_db_ctx
from nanio_orchestrator.models import BucketListOut, BucketPromoteRequest, MigrationStatus
from nanio_orchestrator.nginx.executor import reload_nginx, test_config
from nanio_orchestrator.nginx.generator import generate_vhost_config, record_file_state
from nanio_orchestrator.s3client import (
    count_objects,
    create_bucket,
    get_object,
    list_objects,
    put_object,
)

router = APIRouter(prefix="/api/vhosts", tags=["buckets"])
logger = logging.getLogger(__name__)

# Track running migration tasks: (vhost_id, bucket) → asyncio.Task
_migration_tasks: Dict[tuple, asyncio.Task] = {}


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


async def _apply_vhost_config(vhost_id: int, db) -> tuple:
    """Generate, test, write, reload. Returns (ok, output)."""
    filepath, content = await generate_vhost_config(vhost_id)
    tmp = filepath + ".tmp"
    async with aiofiles.open(tmp, "w") as f:
        await f.write(content)

    test_result = await test_config()
    if not test_result.ok:
        os.unlink(tmp)
        return False, test_result.output

    os.rename(tmp, filepath)
    reload_result = await reload_nginx()
    await record_file_state(db, filepath, content)
    await db.commit()
    return reload_result.ok, f"nginx -t: {test_result.output}\nnginx reload: {reload_result.output}"


async def _audit(db, action, entity_type, entity_id, before=None, after=None,
                 reload_ok=None, reload_output=None):
    await db.execute(
        """INSERT INTO audit_log
             (action, entity_type, entity_id, before_json, after_json, nginx_reload_ok, nginx_reload_output)
           VALUES (?,?,?,?,?,?,?)""",
        (action, entity_type, entity_id,
         json.dumps(before) if before else None,
         json.dumps(after) if after else None,
         1 if reload_ok is True else (0 if reload_ok is False else None),
         reload_output),
    )


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
                    default_member, rd["bucket"],
                    access_key=s.s3_access_key, secret_key=s.s3_secret_key,
                )
            except Exception:
                pass
        buckets.append({
            "name": rd["bucket"],
            "status": rd["status"],
            "pool_name": rd.get("pool_name"),
            "routed_pool_id": rd.get("routed_pool_id"),
            "object_count": obj_count,
            "discovered_at": rd["discovered_at"],
        })
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
    return result


# ── Promote ───────────────────────────────────────────────────────────────────


@router.post("/{vhost_id}/buckets/{bucket}/promote")
async def promote_bucket(vhost_id: int, bucket: str, body: BucketPromoteRequest):
    """Promote a bucket to a dedicated pool.

    1. Creates the bucket on all members of the target pool.
    2. Creates an nginx route: /{bucket}/ → target pool.
    3. Optionally kicks off object migration.
    """
    s = get_settings()

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

        target_members = await _all_enabled_members(body.pool_id, db)
        if not target_members:
            raise HTTPException(400, "Target pool has no enabled members")

        default_pool_id = vhost["default_pool_id"]
        default_member = await _first_enabled_member(default_pool_id, db)

    # ── Create bucket on all target pool members ──────────────────────────
    provision_results = []
    for member in target_members:
        ok, msg = await create_bucket(
            member, bucket,
            access_key=s.s3_access_key, secret_key=s.s3_secret_key,
        )
        provision_results.append({"member": member, "ok": ok, "msg": msg})
        if not ok:
            return {
                "ok": False,
                "error": f"Failed to create bucket on {member}: {msg}",
                "provision": provision_results,
            }

    # ── Create nginx route ────────────────────────────────────────────────
    async with get_db_ctx() as db:
        cursor = await db.execute(
            """INSERT INTO routes (vhost_id, path_prefix, pool_id, enabled)
               VALUES (?, ?, ?, 1)""",
            (vhost_id, f"/{bucket}/", body.pool_id),
        )
        route_id = cursor.lastrowid
        await db.commit()

        ok, output = await _apply_vhost_config(vhost_id, db)
        await _audit(db, "promote_bucket", "route", route_id,
                     after={"bucket": bucket, "pool_id": body.pool_id},
                     reload_ok=ok, reload_output=output)

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
        "provision": provision_results,
        "migration_started": False,
    }

    # ── Optionally start migration ─────────────────────────────────────────
    if body.migrate:
        migration_id = await _start_migration(
            vhost_id, bucket, default_pool_id, body.pool_id, default_member, target_members[0], s
        )
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
        await db.commit()
    return {"ok": True, "bucket": bucket, "status": "ignored"}


# ── Migration ─────────────────────────────────────────────────────────────────


async def _start_migration(
    vhost_id: int,
    bucket: str,
    src_pool_id: int,
    dst_pool_id: int,
    src_member: str,
    dst_member: str,
    s,
) -> int:
    """Create an object_migrations record and launch background task. Returns migration id."""
    async with get_db_ctx() as db:
        cursor = await db.execute(
            """INSERT INTO object_migrations
               (vhost_id, bucket, src_pool_id, dst_pool_id, status)
               VALUES (?, ?, ?, ?, 'pending')""",
            (vhost_id, bucket, src_pool_id, dst_pool_id),
        )
        migration_id = cursor.lastrowid
        await db.commit()

    task = asyncio.create_task(
        _run_migration(migration_id, vhost_id, bucket, src_member, dst_member, s)
    )
    _migration_tasks[(vhost_id, bucket)] = task
    return migration_id


async def _run_migration(
    migration_id: int,
    vhost_id: int,
    bucket: str,
    src_member: str,
    dst_member: str,
    s,
) -> None:
    """Background task: copy all objects from src to dst."""
    now = _now()
    async with get_db_ctx() as db:
        await db.execute(
            "UPDATE object_migrations SET status='running', started_at=? WHERE id=?",
            (now, migration_id),
        )
        # Update bucket_sync status
        await db.execute(
            "UPDATE bucket_sync SET status='migrating' WHERE vhost_id=? AND bucket=?",
            (vhost_id, bucket),
        )
        await db.commit()

    try:
        keys = await list_objects(
            src_member, bucket,
            access_key=s.s3_access_key, secret_key=s.s3_secret_key,
        )
        total = len(keys)

        async with get_db_ctx() as db:
            await db.execute(
                "UPDATE object_migrations SET objects_total=? WHERE id=?",
                (total, migration_id),
            )
            await db.commit()

        done = 0
        for key in keys:
            data = await get_object(
                src_member, bucket, key,
                access_key=s.s3_access_key, secret_key=s.s3_secret_key,
            )
            if data is not None:
                ok = await put_object(
                    dst_member, bucket, key, data,
                    access_key=s.s3_access_key, secret_key=s.s3_secret_key,
                )
                if ok:
                    done += 1

            async with get_db_ctx() as db:
                await db.execute(
                    "UPDATE object_migrations SET objects_done=? WHERE id=?",
                    (done, migration_id),
                )
                await db.commit()

            await asyncio.sleep(0)  # yield to event loop between objects

        async with get_db_ctx() as db:
            await db.execute(
                "UPDATE object_migrations SET status='done', finished_at=? WHERE id=?",
                (_now(), migration_id),
            )
            await db.commit()

        logger.info("Migration done: vhost=%d bucket=%s (%d/%d objects)", vhost_id, bucket, done, total)

    except Exception as e:
        logger.error("Migration error: vhost=%d bucket=%s: %s", vhost_id, bucket, e)
        async with get_db_ctx() as db:
            await db.execute(
                "UPDATE object_migrations SET status='error', error_msg=?, finished_at=? WHERE id=?",
                (str(e), _now(), migration_id),
            )
            await db.commit()
    finally:
        _migration_tasks.pop((vhost_id, bucket), None)


@router.post("/{vhost_id}/buckets/{bucket}/migrate")
async def start_migration(vhost_id: int, bucket: str):
    """Start (or restart) object migration for a routed bucket."""
    s = get_settings()

    if (vhost_id, bucket) in _migration_tasks:
        task = _migration_tasks[(vhost_id, bucket)]
        if not task.done():
            raise HTTPException(409, "Migration already running for this bucket")

    async with get_db_ctx() as db:
        vhost = await _require_vhost_with_default_pool(vhost_id, db)

        # Find the route to get target pool
        route_rows = await db.execute_fetchall(
            "SELECT pool_id FROM routes WHERE vhost_id = ? AND path_prefix = ?",
            (vhost_id, f"/{bucket}/"),
        )
        if not route_rows:
            raise HTTPException(400, f"No route found for /{bucket}/ — promote first")

        dst_pool_id = dict(route_rows[0])["pool_id"]
        src_pool_id = vhost["default_pool_id"]

        src_member = await _first_enabled_member(src_pool_id, db)
        dst_member = await _first_enabled_member(dst_pool_id, db)

    migration_id = await _start_migration(
        vhost_id, bucket, src_pool_id, dst_pool_id, src_member, dst_member, s
    )
    return {"ok": True, "migration_id": migration_id, "bucket": bucket}


@router.get("/{vhost_id}/buckets/{bucket}/migrate/status", response_model=MigrationStatus)
async def migration_status(vhost_id: int, bucket: str):
    """Get the latest migration status for a bucket."""
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall(
            """SELECT * FROM object_migrations
               WHERE vhost_id = ? AND bucket = ?
               ORDER BY id DESC LIMIT 1""",
            (vhost_id, bucket),
        )
        if not rows:
            raise HTTPException(404, "No migration record found")
        m = dict(rows[0])

    return MigrationStatus(
        bucket=m["bucket"],
        status=m["status"],
        objects_total=m["objects_total"],
        objects_done=m["objects_done"],
        error_msg=m.get("error_msg"),
        started_at=m.get("started_at"),
        finished_at=m.get("finished_at"),
    )
