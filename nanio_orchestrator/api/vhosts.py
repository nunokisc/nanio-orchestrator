"""Vhosts + Routes CRUD API with nginx config generation + reload."""

from __future__ import annotations

import json
import logging
import os
from typing import List, Optional

import aiofiles
from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

from nanio_orchestrator.backup import trigger_backup
from nanio_orchestrator.credentials import get_pool_s3_params
from nanio_orchestrator.db import get_db_ctx
from nanio_orchestrator.s3client import bucket_exists, create_bucket, count_objects
from nanio_orchestrator.sidecar import write_vhost_sidecar as _write_vhost_sidecar_sync, delete_vhost_sidecar as _delete_vhost_sidecar_sync
import asyncio as _asyncio

async def write_vhost_sidecar(*args, **kwargs):
    await _asyncio.to_thread(_write_vhost_sidecar_sync, *args, **kwargs)

async def delete_vhost_sidecar(*args, **kwargs):
    await _asyncio.to_thread(_delete_vhost_sidecar_sync, *args, **kwargs)
from nanio_orchestrator.models import (
    RouteCreate,
    RouteOut,
    RouteUpdate,
    VhostCreate,
    VhostOut,
    VhostUpdate,
)
from nanio_orchestrator.nginx.executor import reload_nginx, test_config
from nanio_orchestrator.nginx.generator import (
    generate_vhost_config,
    record_file_state,
    remove_config_file,
    write_config_atomic,
)

router = APIRouter(prefix="/api/vhosts", tags=["vhosts"])


async def _sync_default_route(vhost_id: int, pool_id: int | None, db) -> None:
    """Keep the auto-managed '/' route in sync with vhost.default_pool_id."""
    existing = await db.execute_fetchall(
        "SELECT id FROM routes WHERE vhost_id = ? AND path_prefix = '/'", (vhost_id,)
    )
    if pool_id:
        if existing:
            await db.execute(
                "UPDATE routes SET pool_id = ?, updated_at = datetime('now') WHERE id = ?",
                (pool_id, existing[0]["id"]),
            )
        else:
            await db.execute(
                "INSERT INTO routes (vhost_id, path_prefix, pool_id, enabled) VALUES (?, '/', ?, 1)",
                (vhost_id, pool_id),
            )
    else:
        if existing:
            await db.execute("DELETE FROM routes WHERE id = ?", (existing[0]["id"],))
    await db.commit()


async def _get_pool_name(db, pool_id: int | None) -> str | None:
    if not pool_id:
        return None
    rows = await db.execute_fetchall("SELECT name FROM pools WHERE id = ?", (pool_id,))
    return rows[0]["name"] if rows else None


async def _audit(db, action, entity_type, entity_id,
                 before=None, after=None, reload_ok=None, reload_output=None):
    await db.execute(
        """INSERT INTO audit_log (action, entity_type, entity_id, before_json, after_json,
           nginx_reload_ok, nginx_reload_output) VALUES (?,?,?,?,?,?,?)""",
        (action, entity_type, entity_id,
         json.dumps(before) if before else None,
         json.dumps(after) if after else None,
         1 if reload_ok is True else (0 if reload_ok is False else None),
         reload_output),
    )


async def _apply_vhost_config(vhost_id: int, db) -> tuple:
    """Generate, test, write, reload, record. Returns (ok, output)."""
    filepath, content = await generate_vhost_config(vhost_id)
    tmp_path = filepath + ".tmp"
    async with aiofiles.open(tmp_path, "w") as f:
        await f.write(content)

    # Save current config for rollback
    old_content = None
    if os.path.exists(filepath):
        async with aiofiles.open(filepath, "r") as f:
            old_content = await f.read()

    # Rename .tmp → live, then test the actual config nginx will use
    os.rename(tmp_path, filepath)

    test_result = await test_config()
    if not test_result.ok:
        # Restore previous config on failure
        if old_content is not None:
            async with aiofiles.open(filepath, "w") as f:
                await f.write(old_content)
        else:
            os.unlink(filepath)
        return False, test_result.output

    reload_result = await reload_nginx()
    await record_file_state(db, filepath, content)
    await db.commit()

    combined = f"nginx -t: {test_result.output}\nnginx -s reload: {reload_result.output}"

    # Trigger DB backup after successful write
    await trigger_backup()

    return reload_result.ok, combined


# ── Vhost CRUD ────────────────────────────────────────────────────────────────


@router.get("", response_model=List[VhostOut])
async def list_vhosts():
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT * FROM vhosts ORDER BY server_name")
        return [dict(r) for r in rows]


@router.post("", response_model=VhostOut, status_code=201)
async def create_vhost(body: VhostCreate):
    async with get_db_ctx() as db:
        try:
            cursor = await db.execute(
                """INSERT INTO vhosts (server_name, listen_port, ssl, ssl_cert_path, ssl_key_path,
                   extra_directives, enabled, default_pool_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (body.server_name, body.listen_port, 1 if body.ssl else 0,
                 body.ssl_cert_path, body.ssl_key_path, body.extra_directives,
                 1 if body.enabled else 0, body.default_pool_id),
            )
            await db.commit()
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(409, f"Vhost '{body.server_name}' already exists")
            raise

        rows = await db.execute_fetchall("SELECT * FROM vhosts WHERE id = ?", (cursor.lastrowid,))
        vhost = dict(rows[0])
        await _audit(db, "create", "vhost", vhost["id"], after=vhost)

        default_pool_name = await _get_pool_name(db, vhost.get("default_pool_id"))
        await write_vhost_sidecar(vhost["id"], vhost["server_name"], vhost.get("default_pool_id"), default_pool_name)

        # Auto-create the immutable '/' catch-all route pointing to the default pool
        if body.default_pool_id:
            await db.execute(
                "INSERT INTO routes (vhost_id, path_prefix, pool_id, enabled) VALUES (?, '/', ?, 1)",
                (vhost["id"], body.default_pool_id),
            )
        await db.commit()

        if body.default_pool_id:
            ok, output = await _apply_vhost_config(vhost["id"], db)
            await _audit(db, "create", "route", None,
                         after={"path_prefix": "/", "pool_id": body.default_pool_id, "vhost_id": vhost["id"]},
                         reload_ok=ok, reload_output=output)
            await db.commit()

        return vhost


@router.get("/{vhost_id}", response_model=VhostOut)
async def get_vhost(vhost_id: int):
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT * FROM vhosts WHERE id = ?", (vhost_id,))
        if not rows:
            raise HTTPException(404, "Vhost not found")
        return dict(rows[0])


@router.put("/{vhost_id}", response_model=VhostOut)
async def update_vhost(vhost_id: int, body: VhostUpdate):
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT * FROM vhosts WHERE id = ?", (vhost_id,))
        if not rows:
            raise HTTPException(404, "Vhost not found")
        before = dict(rows[0])

        updates = body.model_dump(exclude_none=True)
        if not updates:
            return before

        if "ssl" in updates:
            updates["ssl"] = 1 if updates["ssl"] else 0
        if "enabled" in updates:
            updates["enabled"] = 1 if updates["enabled"] else 0

        set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
        values = list(updates.values()) + [vhost_id]
        await db.execute(
            f"UPDATE vhosts SET {set_clause}, updated_at = datetime('now') WHERE id = ?",
            values,
        )
        await db.commit()

        # Keep the auto-managed '/' route in sync when default_pool_id changes
        if "default_pool_id" in updates:
            await _sync_default_route(vhost_id, updates["default_pool_id"], db)

        rows = await db.execute_fetchall("SELECT * FROM vhosts WHERE id = ?", (vhost_id,))
        after = dict(rows[0])

        ok, output = await _apply_vhost_config(vhost_id, db)
        await _audit(db, "update", "vhost", vhost_id, before=before, after=after,
                     reload_ok=ok, reload_output=output)
        await db.commit()

        default_pool_name = await _get_pool_name(db, after.get("default_pool_id"))
        await write_vhost_sidecar(after["id"], after["server_name"], after.get("default_pool_id"), default_pool_name)

        return after


@router.delete("/{vhost_id}", status_code=204)
async def delete_vhost(vhost_id: int):
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT * FROM vhosts WHERE id = ?", (vhost_id,))
        if not rows:
            raise HTTPException(404, "Vhost not found")
        vhost = dict(rows[0])

        # Cascade: routes and bucket_sync are children of the vhost
        await db.execute("DELETE FROM routes WHERE vhost_id = ?", (vhost_id,))
        await db.execute("DELETE FROM bucket_sync WHERE vhost_id = ?", (vhost_id,))
        await db.execute("DELETE FROM vhosts WHERE id = ?", (vhost_id,))

        from nanio_orchestrator.config import get_settings
        s = get_settings()
        filepath = str(s.vhosts_dir / f"{vhost['server_name']}.conf")
        await remove_config_file(filepath)
        await db.execute("DELETE FROM config_files WHERE path = ?", (filepath,))

        reload_result = await reload_nginx()
        await _audit(db, "delete", "vhost", vhost_id, before=vhost,
                     reload_ok=reload_result.ok, reload_output=reload_result.output)
        await db.commit()

        # Delete sidecar
        await delete_vhost_sidecar(vhost["server_name"])


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("/{vhost_id}/routes", response_model=List[RouteOut])
async def list_routes(vhost_id: int):
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT * FROM vhosts WHERE id = ?", (vhost_id,))
        if not rows:
            raise HTTPException(404, "Vhost not found")
        routes = await db.execute_fetchall(
            """SELECT r.*, p.name as pool_name
               FROM routes r JOIN pools p ON r.pool_id = p.id
               WHERE r.vhost_id = ?
               ORDER BY length(r.path_prefix) DESC""",
            (vhost_id,),
        )
        return [dict(r) for r in routes]


@router.post("/{vhost_id}/routes", status_code=201)
async def create_route(vhost_id: int, body: RouteCreate):
    if body.path_prefix == "/":
        raise HTTPException(
            400,
            "The '/' route is managed automatically via the vhost's default_pool_id and cannot be created manually",
        )

    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT * FROM vhosts WHERE id = ?", (vhost_id,))
        if not rows:
            raise HTTPException(404, "Vhost not found")
        vhost_default_pool_id: Optional[int] = dict(rows[0]).get("default_pool_id")

        pool_rows = await db.execute_fetchall("SELECT * FROM pools WHERE id = ?", (body.pool_id,))
        if not pool_rows:
            raise HTTPException(400, f"Pool {body.pool_id} not found")

    # The S3 bucket name is the first path segment (e.g. /photos/2025/ → bucket=photos)
    bucket_segment = body.path_prefix.strip("/").split("/")[0]

    # Migration is needed when the destination differs from the vhost default (source) pool
    needs_migration = bool(
        bucket_segment
        and vhost_default_pool_id
        and body.pool_id != vhost_default_pool_id
    )

    # Count objects on source before creating the route, so we know whether to migrate
    objects_on_source: Optional[int] = None
    if needs_migration:
        try:
            async with get_db_ctx() as db:
                src_rows = await db.execute_fetchall(
                    "SELECT address FROM pool_members WHERE pool_id = ? AND enabled = 1 ORDER BY id LIMIT 1",
                    (vhost_default_pool_id,),
                )
            if src_rows:
                src_ak, src_sk, _ = await get_pool_s3_params(vhost_default_pool_id)
                objects_on_source = await count_objects(
                    src_rows[0]["address"], bucket_segment,
                    access_key=src_ak, secret_key=src_sk,
                )
        except Exception as exc:
            logger.warning("route create: source object count failed for '%s': %s", bucket_segment, exc)

    # If there are objects to migrate, create the route initially pointing to the
    # source pool so existing data stays available while the migration runs.
    # The migration engine will flip the route to dst when switching completes.
    # If no objects (new bucket) or no migration needed, route goes directly to dst.
    has_objects = objects_on_source is not None and objects_on_source > 0
    initial_pool_id = vhost_default_pool_id if (needs_migration and has_objects) else body.pool_id

    async with get_db_ctx() as db:
        try:
            cursor = await db.execute(
                """INSERT INTO routes (vhost_id, path_prefix, pool_id, key_prefix, extra_directives, enabled)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (vhost_id, body.path_prefix, initial_pool_id, body.key_prefix,
                 body.extra_directives, 1 if body.enabled else 0),
            )
            await db.commit()
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(409, f"Route with prefix '{body.path_prefix}' already exists for this vhost")
            raise

        route_id = cursor.lastrowid
        rrows = await db.execute_fetchall(
            """SELECT r.*, p.name as pool_name
               FROM routes r JOIN pools p ON r.pool_id = p.id
               WHERE r.id = ?""",
            (route_id,),
        )
        route = dict(rrows[0])

        ok, output = await _apply_vhost_config(vhost_id, db)
        await _audit(db, "create", "route", route_id, after=route,
                     reload_ok=ok, reload_output=output)
        await db.commit()

    # ── Auto-provision bucket on destination pool ─────────────────────────────
    bucket_provisioned = False
    if bucket_segment:
        try:
            async with get_db_ctx() as db:
                member_rows = await db.execute_fetchall(
                    "SELECT address FROM pool_members WHERE pool_id = ? AND enabled = 1 ORDER BY id LIMIT 1",
                    (body.pool_id,),
                )
            if member_rows:
                target_ak, target_sk, _ = await get_pool_s3_params(body.pool_id)
                exists = await bucket_exists(member_rows[0]["address"], bucket_segment,
                                             access_key=target_ak, secret_key=target_sk)
                if not exists:
                    ok_create, _ = await create_bucket(member_rows[0]["address"], bucket_segment,
                                                       access_key=target_ak, secret_key=target_sk)
                    bucket_provisioned = ok_create
                    logger.info("route create: provisioned bucket '%s' on pool %d", bucket_segment, body.pool_id)
        except Exception as exc:
            logger.warning("route create: target bucket provisioning failed for '%s': %s", bucket_segment, exc)

    # ── Auto-start migration if source has data ───────────────────────────────
    migration_id: Optional[int] = None
    migration_warning: Optional[str] = None
    if needs_migration and has_objects:
        try:
            from nanio_orchestrator.migration_engine import start_migration
            migration_id = await start_migration(
                vhost_id=vhost_id,
                bucket=bucket_segment,
                src_pool_id=vhost_default_pool_id,
                dst_pool_id=body.pool_id,
                route_id=route_id,
            )
            logger.info(
                "route create: started migration %d for bucket '%s' route_id=%d src=%d dst=%d",
                migration_id, bucket_segment, route_id, vhost_default_pool_id, body.pool_id,
            )
        except RuntimeError as exc:
            migration_warning = str(exc)
            logger.warning(
                "route create: could not start migration for '%s' (route_id=%d): %s",
                bucket_segment, route_id, exc,
            )
            # Route stays on source pool — data remains accessible; operator must start migration manually

    return {
        **route,
        "bucket": bucket_segment,
        "bucket_provisioned": bucket_provisioned,
        "objects_on_source": objects_on_source,
        "default_pool_id": vhost_default_pool_id,
        "migration_id": migration_id,
        "migration_warning": migration_warning,
    }


@router.put("/{vhost_id}/routes/{route_id}", response_model=RouteOut)
async def update_route(vhost_id: int, route_id: int, body: RouteUpdate):
    async with get_db_ctx() as db:
        rrows = await db.execute_fetchall(
            "SELECT * FROM routes WHERE id = ? AND vhost_id = ?", (route_id, vhost_id)
        )
        if not rrows:
            raise HTTPException(404, "Route not found")
        before = dict(rrows[0])

        updates = body.model_dump(exclude_none=True)

        if before["path_prefix"] == "/" and "pool_id" in updates:
            raise HTTPException(
                400,
                "The '/' route pool is controlled by the vhost's default_pool_id; update the vhost to change it",
            )
        if not updates:
            rrows2 = await db.execute_fetchall(
                """SELECT r.*, p.name as pool_name
                   FROM routes r JOIN pools p ON r.pool_id = p.id
                   WHERE r.id = ?""",
                (route_id,),
            )
            return dict(rrows2[0])

        if "pool_id" in updates:
            pool_rows = await db.execute_fetchall("SELECT id FROM pools WHERE id = ?", (updates["pool_id"],))
            if not pool_rows:
                raise HTTPException(400, f"Pool {updates['pool_id']} not found")

        if "enabled" in updates:
            updates["enabled"] = 1 if updates["enabled"] else 0

        set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
        values = list(updates.values()) + [route_id]
        await db.execute(
            f"UPDATE routes SET {set_clause}, updated_at = datetime('now') WHERE id = ?",
            values,
        )
        await db.commit()

        rrows2 = await db.execute_fetchall(
            """SELECT r.*, p.name as pool_name
               FROM routes r JOIN pools p ON r.pool_id = p.id
               WHERE r.id = ?""",
            (route_id,),
        )
        after = dict(rrows2[0])

        ok, output = await _apply_vhost_config(vhost_id, db)
        await _audit(db, "update", "route", route_id, before=before, after=after,
                     reload_ok=ok, reload_output=output)
        await db.commit()
        return after


@router.delete("/{vhost_id}/routes/{route_id}", status_code=204)
async def delete_route(vhost_id: int, route_id: int):
    async with get_db_ctx() as db:
        rrows = await db.execute_fetchall(
            "SELECT * FROM routes WHERE id = ? AND vhost_id = ?", (route_id, vhost_id)
        )
        if not rrows:
            raise HTTPException(404, "Route not found")
        before = dict(rrows[0])

        if before["path_prefix"] == "/":
            raise HTTPException(
                400,
                "The '/' catch-all route cannot be deleted; set default_pool_id to null on the vhost to remove it",
            )

        await db.execute("DELETE FROM routes WHERE id = ?", (route_id,))
        await db.commit()

        ok, output = await _apply_vhost_config(vhost_id, db)
        await _audit(db, "delete", "route", route_id, before=before,
                     reload_ok=ok, reload_output=output)
        await db.commit()


# ── Preview ───────────────────────────────────────────────────────────────────


@router.get("/{vhost_id}/preview")
async def preview_vhost_config(vhost_id: int):
    """Render vhost config without applying."""
    try:
        filepath, content = await generate_vhost_config(vhost_id)
    except ValueError:
        raise HTTPException(404, "Vhost not found")
    return {"filepath": filepath, "content": content}
