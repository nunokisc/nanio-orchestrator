"""Vhosts + Routes CRUD API with nginx config generation (manual apply via Config tab)."""

from __future__ import annotations

import asyncio as _asyncio
import json
import logging
import sqlite3
from typing import Optional

from fastapi import APIRouter, HTTPException

from nanio_orchestrator import s3client, subfolders
from nanio_orchestrator.api.common import reject_if_refused
from nanio_orchestrator.audit_log import log_audit
from nanio_orchestrator.config import get_settings
from nanio_orchestrator.credentials import get_pool_s3_params
from nanio_orchestrator.db import get_db_ctx
from nanio_orchestrator.models import (
    RouteCreate,
    RouteOut,
    RouteUpdate,
    VhostCreate,
    VhostOut,
    VhostUpdate,
)
from nanio_orchestrator.nginx.apply import write_configs
from nanio_orchestrator.nginx.apply import write_vhost_config as _write_vhost_config
from nanio_orchestrator.nginx.generator import (
    SUBFOLDER_MARKER,
    generate_vhost_config,
    is_subfolder_prefix,
    path_segments,
    remove_config_file,
)
from nanio_orchestrator.s3client import count_objects
from nanio_orchestrator.sidecar import delete_vhost_sidecar as _delete_vhost_sidecar_sync
from nanio_orchestrator.sidecar import write_vhost_sidecar as _write_vhost_sidecar_sync

logger = logging.getLogger(__name__)


async def write_vhost_sidecar(*args, **kwargs):
    await _asyncio.to_thread(_write_vhost_sidecar_sync, *args, **kwargs)


async def delete_vhost_sidecar(*args, **kwargs):
    await _asyncio.to_thread(_delete_vhost_sidecar_sync, *args, **kwargs)


router = APIRouter(prefix="/api/vhosts", tags=["vhosts"])


# ── Sub-folder routes (see nanio_orchestrator/subfolders.py) ─────────────────


async def _prepare_subfolder_route(db, vhost: dict, body: RouteCreate, target_pool: dict) -> dict:
    """Checks and provisioning for a new sub-folder route. Changes nothing in the database.

    Returns what the caller needs to finish: parent pool, bucket, folder key and endpoints.
    """
    bucket = path_segments(body.path_prefix)[0]
    folder = subfolders.folder_key(body.path_prefix)
    parent = await subfolders.parent_pool(db, vhost["id"], body.path_prefix)
    if parent is None:
        raise HTTPException(
            400,
            f"Bucket '{bucket}' is not served by this vhost: it has no '/{bucket}/' route and the vhost has no "
            "default pool. A sub-folder route needs the bucket itself to be served first.",
        )
    if parent["type"] != "nanio":
        raise HTTPException(400, f"Bucket '{bucket}' is served by pool '{parent['name']}', which is not an S3 pool.")
    if parent["id"] == target_pool["id"]:
        raise HTTPException(
            400, f"'{body.path_prefix}' is already served by pool '{parent['name']}': nothing to route elsewhere."
        )
    parent_addr = await subfolders.pool_endpoint(db, parent["id"])
    target_addr = await subfolders.pool_endpoint(db, target_pool["id"])
    if not parent_addr or not target_addr:
        missing = parent["name"] if not parent_addr else target_pool["name"]
        raise HTTPException(400, f"Pool '{missing}' has no enabled member to talk to.")

    parent_ak, parent_sk, _ = await get_pool_s3_params(parent["id"])
    target_ak, target_sk, _ = await get_pool_s3_params(target_pool["id"])
    try:
        if not body.allow_orphan and await s3client.bucket_has_objects(
            parent_addr, bucket, access_key=parent_ak, secret_key=parent_sk, prefix=folder
        ):
            raise HTTPException(
                409,
                f"Bucket '{bucket}' already has objects under '{folder}' on pool '{parent['name']}'. Routing the "
                f"folder to '{target_pool['name']}' would hide them. Move them first, or send allow_orphan=true "
                "to leave them behind.",
            )
        ok, message = await s3client.create_bucket(target_addr, bucket, access_key=target_ak, secret_key=target_sk)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Could not check the pools before creating the route: {exc}") from exc
    if not ok:
        raise HTTPException(
            502, f"Could not create bucket '{bucket}' on pool '{target_pool['name']}': {message}. Nothing was changed."
        )
    return {"bucket": bucket, "parent": parent}


async def _sync_default_route(vhost_id: int, pool_id: int | None, db) -> None:
    """Keep the auto-managed '/' route in sync with vhost.default_pool_id."""
    existing = await db.execute_fetchall("SELECT id FROM routes WHERE vhost_id = ? AND path_prefix = '/'", (vhost_id,))
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


async def _get_pool_name(db, pool_id: int | None) -> str | None:
    if not pool_id:
        return None
    rows = await db.execute_fetchall("SELECT name FROM pools WHERE id = ?", (pool_id,))
    return rows[0]["name"] if rows else None


async def _get_pool_type(db, pool_id: int) -> str | None:
    """Return the type of the given pool, or None if there is no such pool."""
    rows = await db.execute_fetchall("SELECT type FROM pools WHERE id = ?", (pool_id,))
    return rows[0]["type"] if rows else None


def _serialize_extra_blocks(blocks: list | None) -> str | None:
    """Serialize a list of VhostExtraBlock to JSON for DB storage."""
    if not blocks:
        return None
    return json.dumps([b.model_dump() for b in blocks])


def _deserialize_extra_blocks(json_str: str | None) -> list | None:
    """Deserialize JSON from DB back to a list of VhostExtraBlock dicts."""
    if not json_str:
        return None
    return json.loads(json_str)


def _enrich_vhost(vhost: dict) -> dict:
    """Parse extra_blocks_json into extra_blocks list for API responses."""
    blocks_raw = vhost.pop("extra_blocks_json", None)
    vhost["extra_blocks"] = _deserialize_extra_blocks(blocks_raw)
    ips_raw = vhost.pop("ip_rule_ips_json", None)
    vhost["ip_rule_ips"] = json.loads(ips_raw) if ips_raw else None
    return vhost


# ── Vhost CRUD ────────────────────────────────────────────────────────────────


@router.get("", response_model=list[VhostOut])
async def list_vhosts():
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT * FROM vhosts ORDER BY server_name")
        return [_enrich_vhost(dict(r)) for r in rows]


@router.post("", response_model=VhostOut, status_code=201)
async def create_vhost(body: VhostCreate):
    async with get_db_ctx() as db:
        # Validate default pool type if provided
        if body.default_pool_id:
            pool_type = await _get_pool_type(db, body.default_pool_id)
            if pool_type is None:
                raise HTTPException(400, f"Pool {body.default_pool_id} not found")

        extra_blocks_json = _serialize_extra_blocks(body.extra_blocks)
        ip_rule_ips_json = json.dumps(body.ip_rule_ips) if body.ip_rule_ips else None
        try:
            cursor = await db.execute(
                """INSERT INTO vhosts (server_name, listen_port, ssl, ssl_cert_path, ssl_key_path,
                   extra_blocks_json, enabled, default_pool_id,
                   ip_rule_mode, ip_rule_ips_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    body.server_name,
                    body.listen_port,
                    1 if body.ssl else 0,
                    body.ssl_cert_path,
                    body.ssl_key_path,
                    extra_blocks_json,
                    1 if body.enabled else 0,
                    body.default_pool_id,
                    body.ip_rule_mode,
                    ip_rule_ips_json,
                ),
            )
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(409, f"Vhost '{body.server_name}' already exists") from e
            raise

        rows = await db.execute_fetchall("SELECT * FROM vhosts WHERE id = ?", (cursor.lastrowid,))
        vhost = dict(rows[0])
        default_pool_name = await _get_pool_name(db, vhost.get("default_pool_id"))

        # Auto-create the immutable '/' catch-all route pointing to the default pool
        if body.default_pool_id:
            await db.execute(
                "INSERT INTO routes (vhost_id, path_prefix, pool_id, enabled) VALUES (?, '/', ?, 1)",
                (vhost["id"], body.default_pool_id),
            )
            # Nothing is committed yet: nginx validates the vhost before it is saved
            ok, output = await _write_vhost_config(vhost["id"], db)
            await reject_if_refused(db, ok, output)

        await log_audit(db, "create", "vhost", vhost["id"], after=vhost)
        await db.commit()
        await write_vhost_sidecar(
            vhost["id"],
            vhost["server_name"],
            vhost.get("default_pool_id"),
            default_pool_name,
            extra_blocks_json=vhost.get("extra_blocks_json"),
            ip_rule_mode=vhost.get("ip_rule_mode"),
            ip_rule_ips_json=vhost.get("ip_rule_ips_json"),
        )

        if body.default_pool_id:
            await log_audit(
                db,
                "create",
                "route",
                None,
                after={"path_prefix": "/", "pool_id": body.default_pool_id, "vhost_id": vhost["id"]},
                reload_ok=ok,
                reload_output=output,
            )
            await db.commit()

        return _enrich_vhost(vhost)


@router.get("/{vhost_id}", response_model=VhostOut)
async def get_vhost(vhost_id: int):
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT * FROM vhosts WHERE id = ?", (vhost_id,))
        if not rows:
            raise HTTPException(404, "Vhost not found")
        return _enrich_vhost(dict(rows[0]))


@router.put("/{vhost_id}", response_model=VhostOut)
async def update_vhost(vhost_id: int, body: VhostUpdate):
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT * FROM vhosts WHERE id = ?", (vhost_id,))
        if not rows:
            raise HTTPException(404, "Vhost not found")
        before = dict(rows[0])

        updates = body.model_dump(exclude_none=True)
        # These three are kept even when null: that is how they are cleared
        for field in ("default_pool_id", "ip_rule_mode", "ip_rule_ips"):
            if field in body.model_fields_set:
                updates[field] = getattr(body, field)
        if not updates:
            return _enrich_vhost(before)

        # Validate SSL cert requirement when enabling SSL
        effective_ssl = updates.get("ssl", bool(before.get("ssl")))
        effective_cert = updates.get("ssl_cert_path", before.get("ssl_cert_path"))
        effective_key = updates.get("ssl_key_path", before.get("ssl_key_path"))
        if effective_ssl and (not effective_cert or not effective_key):
            raise HTTPException(400, "ssl_cert_path and ssl_key_path are required when ssl is enabled")

        # Validate new default_pool type is consistent with existing routes
        if updates.get("default_pool_id") is not None:
            new_pool_type = await _get_pool_type(db, updates["default_pool_id"])
            if new_pool_type is None:
                raise HTTPException(400, f"Pool {updates['default_pool_id']} not found")
            # Check existing routes for type conflicts
            route_rows = await db.execute_fetchall(
                """SELECT p.type, p.name FROM routes r JOIN pools p ON r.pool_id = p.id
                   WHERE r.vhost_id = ? AND r.path_prefix != '/'""",
                (vhost_id,),
            )
            for rr in route_rows:
                if rr["type"] != new_pool_type:
                    raise HTTPException(
                        400,
                        f"Cannot set default pool of type '{new_pool_type}': "
                        f"existing route uses pool '{rr['name']}' of type '{rr['type']}'. "
                        "All pools in a vhost must be the same type.",
                    )

        # Handle extra_blocks serialization
        if "extra_blocks" in updates:
            # from the model: `updates` holds plain dicts by now
            del updates["extra_blocks"]
            updates["extra_blocks_json"] = _serialize_extra_blocks(body.extra_blocks)

        if "ip_rule_ips" in updates:
            raw_ips = updates.pop("ip_rule_ips")
            updates["ip_rule_ips_json"] = json.dumps(raw_ips) if raw_ips else None

        if "ssl" in updates:
            updates["ssl"] = 1 if updates["ssl"] else 0
        if "enabled" in updates:
            updates["enabled"] = 1 if updates["enabled"] else 0

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = [*list(updates.values()), vhost_id]
        try:
            await db.execute(
                f"UPDATE vhosts SET {set_clause}, updated_at = datetime('now') WHERE id = ?",
                values,
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, f"Vhost '{updates.get('server_name')}' already exists") from None

        # Keep the auto-managed '/' route in sync when default_pool_id changes
        if "default_pool_id" in updates:
            await _sync_default_route(vhost_id, updates["default_pool_id"], db)

        rows = await db.execute_fetchall("SELECT * FROM vhosts WHERE id = ?", (vhost_id,))
        after = dict(rows[0])

        # A renamed vhost is one change for nginx: the new file written, the old one gone
        renamed_from = before["server_name"] if after["server_name"] != before["server_name"] else None
        old_file = [str(get_settings().vhosts_dir / f"{renamed_from}.conf")] if renamed_from else []
        ok, output = await write_configs(db, vhost_ids=[vhost_id], remove=old_file)
        await reject_if_refused(db, ok, output)
        await log_audit(db, "update", "vhost", vhost_id, before=before, after=after, reload_ok=ok, reload_output=output)
        await db.commit()
        if renamed_from:
            await delete_vhost_sidecar(renamed_from)

        default_pool_name = await _get_pool_name(db, after.get("default_pool_id"))
        await write_vhost_sidecar(
            after["id"],
            after["server_name"],
            after.get("default_pool_id"),
            default_pool_name,
            extra_blocks_json=after.get("extra_blocks_json"),
            ip_rule_mode=after.get("ip_rule_mode"),
            ip_rule_ips_json=after.get("ip_rule_ips_json"),
        )

        return _enrich_vhost(after)


@router.delete("/{vhost_id}", status_code=204)
async def delete_vhost(vhost_id: int):
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT * FROM vhosts WHERE id = ?", (vhost_id,))
        if not rows:
            raise HTTPException(404, "Vhost not found")
        vhost = dict(rows[0])

        busy = await db.execute_fetchall(
            """SELECT id, bucket FROM migrations WHERE vhost_id = ? AND (split_routing = 1
               OR phase IN ('pending','copying','write_routing','verifying','switching'))""",
            (vhost_id,),
        )
        if busy:
            raise HTTPException(
                409,
                f"Migration {busy[0]['id']} is still moving bucket '{busy[0]['bucket']}' of this vhost. "
                "Finish or cancel it first.",
            )

        # Cascade: migrations (they reference the routes), routes and bucket_sync are children of the vhost
        await db.execute("DELETE FROM migrations WHERE vhost_id = ?", (vhost_id,))
        await db.execute("DELETE FROM routes WHERE vhost_id = ?", (vhost_id,))
        await db.execute("DELETE FROM bucket_sync WHERE vhost_id = ?", (vhost_id,))
        await db.execute("DELETE FROM vhosts WHERE id = ?", (vhost_id,))

        from nanio_orchestrator.config import get_settings

        s = get_settings()
        filepath = str(s.vhosts_dir / f"{vhost['server_name']}.conf")
        await remove_config_file(filepath)
        await db.execute("DELETE FROM config_files WHERE path = ?", (filepath,))

        await log_audit(db, "delete", "vhost", vhost_id, before=vhost)
        await db.commit()

        # Delete sidecar
        await delete_vhost_sidecar(vhost["server_name"])


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("/{vhost_id}/routes", response_model=list[RouteOut])
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
    """Create a new route (Scenario 1 — config only, no migration).

    Creating a new route configures nginx and provisions the destination bucket if
    it does not yet exist.  Data is never automatically copied on route creation.
    To migrate existing bucket data use PUT /{vhost_id}/routes/{route_id} to change
    the route's pool, which triggers the migration state machine.
    """
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT * FROM vhosts WHERE id = ?", (vhost_id,))
        if not rows:
            raise HTTPException(404, "Vhost not found")
        vhost_row = dict(rows[0])
        vhost_default_pool_id: Optional[int] = vhost_row.get("default_pool_id")

        pool_rows = await db.execute_fetchall("SELECT * FROM pools WHERE id = ?", (body.pool_id,))
        if not pool_rows:
            raise HTTPException(400, f"Pool {body.pool_id} not found")
        route_pool = dict(pool_rows[0])

        # Enforce type consistency: if the vhost has a default pool, all routes must
        # use pools of the same type (nanio vhost ↔ nanio pools, http vhost ↔ http pools)
        if vhost_default_pool_id:
            default_pool_type = await _get_pool_type(db, vhost_default_pool_id)
            if default_pool_type and route_pool["type"] != default_pool_type:
                raise HTTPException(
                    400,
                    f"Pool '{route_pool['name']}' is of type '{route_pool['type']}', but this vhost "
                    f"uses '{default_pool_type}' pools. You cannot mix pool types within a vhost.",
                )

        # A folder of a bucket on an S3 pool: checks + bucket on the target pool, before anything is written
        is_folder_route = route_pool["type"] == "nanio" and is_subfolder_prefix(body.path_prefix)
        subfolder = await _prepare_subfolder_route(db, vhost_row, body, route_pool) if is_folder_route else None

    # The S3 bucket name is the first path segment (e.g. /photos/2025/ → bucket=photos)
    bucket_segment = body.path_prefix.strip("/").split("/")[0]

    async with get_db_ctx() as db:
        try:
            cursor = await db.execute(
                """INSERT INTO routes (vhost_id, path_prefix, pool_id, enabled)
                   VALUES (?, ?, ?, ?)""",
                (
                    vhost_id,
                    body.path_prefix,
                    body.pool_id,
                    1 if body.enabled else 0,
                ),
            )
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(409, f"Route with prefix '{body.path_prefix}' already exists for this vhost") from e
            raise

        route_id = cursor.lastrowid
        rrows = await db.execute_fetchall(
            """SELECT r.*, p.name as pool_name
               FROM routes r JOIN pools p ON r.pool_id = p.id
               WHERE r.id = ?""",
            (route_id,),
        )
        route = dict(rrows[0])

        # http vhosts serving this bucket from the parent pool's files follow the folder. Their
        # routes are part of the same change: nginx validates all the vhosts together.
        http_changed: list = []
        http_warnings: list = []
        if subfolder:
            http_changed, http_warnings = await subfolders.sync_http_routes(
                db, subfolder["bucket"], body.path_prefix, subfolder["parent"]["id"], body.pool_id
            )
        ok, output = await write_configs(db, vhost_ids=[vhost_id, *http_changed])
        await reject_if_refused(db, ok, output)
        await log_audit(db, "create", "route", route_id, after=route, reload_ok=ok, reload_output=output)
        await db.commit()

    warnings: list[str] = list(http_warnings)
    if subfolder:
        warnings += await subfolders.ensure_markers(vhost_id)

    return {
        **route,
        "bucket": bucket_segment,
        "bucket_provisioned": subfolder is not None,
        "default_pool_id": vhost_default_pool_id,
        "warnings": warnings,
    }


@router.put("/{vhost_id}/routes/{route_id}", response_model=RouteOut)
async def update_route(vhost_id: int, route_id: int, body: RouteUpdate):
    async with get_db_ctx() as db:
        rrows = await db.execute_fetchall("SELECT * FROM routes WHERE id = ? AND vhost_id = ?", (route_id, vhost_id))
        if not rrows:
            raise HTTPException(404, "Route not found")
        before = dict(rrows[0])

        updates = body.model_dump(exclude_none=True)

        if before["path_prefix"] == "/" and "pool_id" in updates:
            raise HTTPException(
                400,
                "The '/' route pool is controlled by the vhost's default_pool_id; update the vhost to change it",
            )
        if "path_prefix" in updates and updates["path_prefix"] != before["path_prefix"]:
            raise HTTPException(400, "A route's path cannot be changed: delete it and create the new one.")
        if (
            "pool_id" in updates
            and updates["pool_id"] != before["pool_id"]
            and is_subfolder_prefix(before["path_prefix"])
        ):
            target_type = await _get_pool_type(db, before["pool_id"])
            if target_type == "nanio":
                raise HTTPException(
                    400,
                    "A sub-folder route cannot be pointed at another pool: its objects would stay behind on the "
                    "current one. Delete the route and create it again on an empty folder.",
                )
        if not updates:
            rrows2 = await db.execute_fetchall(
                """SELECT r.*, p.name as pool_name
                   FROM routes r JOIN pools p ON r.pool_id = p.id
                   WHERE r.id = ?""",
                (route_id,),
            )
            return dict(rrows2[0])

        current_type = await _get_pool_type(db, before["pool_id"])
        new_type = await _get_pool_type(db, updates["pool_id"]) if "pool_id" in updates else current_type

    if new_type is None:
        raise HTTPException(400, f"Pool {updates['pool_id']} not found")
    if new_type != current_type:
        raise HTTPException(
            400,
            f"Pool {updates['pool_id']} is of type '{new_type}', but this route uses '{current_type}' pools. "
            "You cannot mix pool types within a vhost.",
        )
    if "enabled" in updates:
        updates["enabled"] = 1 if updates["enabled"] else 0

    # ── Scenario 2: pool change on a bucket route → migration ────────────────
    # If pool changes, bucket is not a sub-path, and source has data, start
    # migration instead of directly updating the pool_id.  The migration engine
    # will flip the route to the destination pool when switching completes.
    migration_id: Optional[int] = None

    if "pool_id" in updates and updates["pool_id"] != before["pool_id"]:
        bucket_segment = before["path_prefix"].strip("/").split("/")[0]
        # Check if this is a sub-path (a parent bucket route already exists)
        async with get_db_ctx() as db:
            parent_rows = await db.execute_fetchall(
                """SELECT id FROM routes
                   WHERE vhost_id = ? AND id != ? AND path_prefix IN (?, ?)""",
                (vhost_id, route_id, f"/{bucket_segment}/", f"/{bucket_segment}"),
            )

        if not parent_rows:
            # Not a sub-path: check if source bucket has objects
            src_pool_id_cur = before["pool_id"]
            dst_pool_id_requested = updates["pool_id"]
            src_count = 0
            try:
                async with get_db_ctx() as db:
                    src_member_rows = await db.execute_fetchall(
                        "SELECT address FROM pool_members WHERE pool_id = ? AND enabled = 1 ORDER BY id LIMIT 1",
                        (src_pool_id_cur,),
                    )
                if src_member_rows:
                    src_ak, src_sk, _ = await get_pool_s3_params(src_pool_id_cur)
                    src_count = await count_objects(
                        src_member_rows[0]["address"],
                        bucket_segment,
                        access_key=src_ak,
                        secret_key=src_sk,
                    )
            except Exception as exc:
                logger.warning(
                    "update_route: source object count failed for '%s': %s",
                    bucket_segment,
                    exc,
                )

            if src_count > 0:
                # Scenario 2: keep route on current pool, start migration
                updates.pop("pool_id")
                try:
                    from nanio_orchestrator.migration_engine import start_migration

                    migration_id = await start_migration(
                        vhost_id=vhost_id,
                        bucket=bucket_segment,
                        src_pool_id=src_pool_id_cur,
                        dst_pool_id=dst_pool_id_requested,
                        route_id=route_id,
                    )
                    logger.info(
                        "update_route: started migration %d for '%s' route_id=%d src=%d dst=%d",
                        migration_id,
                        bucket_segment,
                        route_id,
                        src_pool_id_cur,
                        dst_pool_id_requested,
                    )
                except RuntimeError as exc:
                    # Repointing the route now would hide the objects that could not be moved
                    raise HTTPException(429, str(exc)) from exc

    async with get_db_ctx() as db:
        if updates:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            values = [*list(updates.values()), route_id]
            await db.execute(
                f"UPDATE routes SET {set_clause}, updated_at = datetime('now') WHERE id = ?",
                values,
            )

        rrows2 = await db.execute_fetchall(
            """SELECT r.*, p.name as pool_name
               FROM routes r JOIN pools p ON r.pool_id = p.id
               WHERE r.id = ?""",
            (route_id,),
        )
        after = dict(rrows2[0])

        ok, output = await _write_vhost_config(vhost_id, db)
        await reject_if_refused(db, ok, output)
        await log_audit(db, "update", "route", route_id, before=before, after=after, reload_ok=ok, reload_output=output)
        await db.commit()

    after["migration_id"] = migration_id
    return after


@router.delete("/{vhost_id}/routes/{route_id}", status_code=204)
async def delete_route(vhost_id: int, route_id: int, allow_orphan: bool = False):
    marker = None
    http_changed: list[int] = []
    async with get_db_ctx() as db:
        rrows = await db.execute_fetchall("SELECT * FROM routes WHERE id = ? AND vhost_id = ?", (route_id, vhost_id))
        if not rrows:
            raise HTTPException(404, "Route not found")
        before = dict(rrows[0])

        if before["path_prefix"] == "/":
            raise HTTPException(
                400,
                "The '/' catch-all route cannot be deleted; set default_pool_id to null on the vhost to remove it",
            )

        # Sub-folder route on an S3 pool: without it the folder's objects are unreachable
        if is_subfolder_prefix(before["path_prefix"]) and await _get_pool_type(db, before["pool_id"]) == "nanio":
            bucket = path_segments(before["path_prefix"])[0]
            folder = subfolders.folder_key(before["path_prefix"])
            target_addr = await subfolders.pool_endpoint(db, before["pool_id"])
            if not allow_orphan:
                if not target_addr:
                    raise HTTPException(
                        400,
                        f"The pool holding '{folder}' of bucket '{bucket}' has no enabled member to ask whether the "
                        "folder is empty. Send ?allow_orphan=true to delete the route anyway.",
                    )
                target_ak, target_sk, _ = await get_pool_s3_params(before["pool_id"])
                try:
                    has_objects = await s3client.bucket_has_objects(
                        target_addr, bucket, access_key=target_ak, secret_key=target_sk, prefix=folder
                    )
                except Exception as exc:
                    raise HTTPException(502, f"Could not check the folder before deleting its route: {exc}") from exc
                if has_objects:
                    raise HTTPException(
                        409,
                        f"'{folder}' of bucket '{bucket}' still has objects on its pool. Without the route they "
                        "become unreachable. Empty the folder first, or send ?allow_orphan=true to leave them there.",
                    )
            parent = await subfolders.parent_pool(db, vhost_id, before["path_prefix"])
            parent_addr = await subfolders.pool_endpoint(db, parent["id"]) if parent else None
            if parent_addr:
                parent_ak, parent_sk, _ = await get_pool_s3_params(parent["id"])
                marker = (parent_addr, bucket, folder + SUBFOLDER_MARKER, parent_ak, parent_sk)
            if parent:
                http_changed, _ = await subfolders.sync_http_routes(
                    db, bucket, before["path_prefix"], parent["id"], None
                )

        # Null-out route_id in any migrations that reference this route
        # to avoid FK constraint failures (route_id has no ON DELETE SET NULL)
        await db.execute("UPDATE migrations SET route_id = NULL WHERE route_id = ?", (route_id,))
        await db.execute("DELETE FROM routes WHERE id = ?", (route_id,))

        ok, output = await write_configs(db, vhost_ids=[vhost_id, *http_changed])
        await reject_if_refused(db, ok, output)
        await log_audit(db, "delete", "route", route_id, before=before, reload_ok=ok, reload_output=output)
        await db.commit()

    if marker and ok:
        address, bucket, key, access_key, secret_key = marker
        try:
            await s3client.delete_object(address, bucket, key, access_key=access_key, secret_key=secret_key)
        except Exception as exc:
            logger.warning("Could not remove sub-folder marker %s/%s: %s", bucket, key, exc)


# ── Preview ───────────────────────────────────────────────────────────────────


@router.get("/{vhost_id}/preview")
async def preview_vhost_config(vhost_id: int):
    """Render vhost config without applying."""
    try:
        filepath, content = await generate_vhost_config(vhost_id)
    except ValueError:
        raise HTTPException(404, "Vhost not found") from None
    return {"filepath": filepath, "content": content}
