"""Pools CRUD API with nginx config generation (manual apply via Config tab)."""

from __future__ import annotations

import asyncio as _asyncio
import json
from typing import List

from fastapi import APIRouter, HTTPException

from nanio_orchestrator.audit_log import log_audit
from nanio_orchestrator.credentials import get_pool_s3_params
from nanio_orchestrator.db import get_db_ctx
from nanio_orchestrator.models import (
    MemberCreate,
    MemberOut,
    MemberUpdate,
    NodeConfigOut,
    NodeConfigRequest,
    PoolCreate,
    PoolOut,
    PoolUpdate,
)
from nanio_orchestrator.nginx.executor import test_config
from nanio_orchestrator.nginx.generator import (
    generate_pool_config,
    node_config_instructions,
    record_file_state,
    render_node_config,
)
from nanio_orchestrator.s3client import delete_object, list_objects
from nanio_orchestrator.s3client import list_buckets as s3_list_buckets
from nanio_orchestrator.sidecar import delete_pool_sidecar as _delete_pool_sidecar_sync
from nanio_orchestrator.sidecar import write_pool_sidecar as _write_pool_sidecar_sync


async def write_pool_sidecar(*args, **kwargs):
    await _asyncio.to_thread(_write_pool_sidecar_sync, *args, **kwargs)


async def delete_pool_sidecar(*args, **kwargs):
    await _asyncio.to_thread(_delete_pool_sidecar_sync, *args, **kwargs)


router = APIRouter(prefix="/api/pools", tags=["pools"])


async def _write_pool_config(pool_id: int, db) -> tuple:
    """Generate, test, write config to disk, record in DB. Returns (ok, output).
    Does NOT reload nginx — the operator applies changes via the Config tab.
    If the pool has no members the config file is removed instead of written.
    """
    filepath, content = await generate_pool_config(pool_id)
    import os

    import aiofiles

    from nanio_orchestrator.nginx.generator import remove_config_file

    # Empty pool — remove file so nginx doesn't see an invalid upstream block
    if content is None:
        await remove_config_file(filepath)
        await db.execute("DELETE FROM config_files WHERE path = ?", (filepath,))
        return True, "Pool has no members — config file removed (apply nginx changes via Config tab)"

    # Write to .tmp
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

    # Record in DB (no reload — operator applies via Config tab)
    await record_file_state(db, filepath, content)
    await db.commit()

    return True, f"nginx -t: {test_result.output} (config written — apply via Config tab)"


def _validate_member_role(pool_type: str, role: str) -> None:
    """Enforce role constraints per pool type."""
    if pool_type == "nanio" and role != "active":
        raise HTTPException(
            status_code=400,
            detail="nanio pools use shared storage — all members are active, backup/replica not allowed",
        )
    if pool_type == "http" and role not in ("primary", "replica"):
        raise HTTPException(
            status_code=400,
            detail=f"{pool_type} pools require role 'primary' or 'replica', not '{role}'",
        )


# ── Pool CRUD ─────────────────────────────────────────────────────────────────


@router.get("", response_model=List[PoolOut])
async def list_pools():
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT * FROM pools ORDER BY name")
        return [dict(r) for r in rows]


@router.post("", response_model=PoolOut, status_code=201)
async def create_pool(body: PoolCreate):
    async with get_db_ctx() as db:
        # Validate source_nanio_pool_id
        if body.source_nanio_pool_id is not None:
            ref_rows = await db.execute_fetchall(
                "SELECT id, type FROM pools WHERE id = ?", (body.source_nanio_pool_id,)
            )
            if not ref_rows:
                raise HTTPException(
                    422,
                    f"source_nanio_pool_id {body.source_nanio_pool_id} does not exist",
                )
            if dict(ref_rows[0])["type"] != "nanio":
                raise HTTPException(
                    422,
                    f"source_nanio_pool_id must reference a nanio pool, "
                    f"but pool {body.source_nanio_pool_id} is of type '{dict(ref_rows[0])['type']}'",
                )
        try:
            cursor = await db.execute(
                """INSERT INTO pools (name, description, type, lb_method, keepalive, source_nanio_pool_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (body.name, body.description, body.type, body.lb_method, body.keepalive, body.source_nanio_pool_id),
            )
            await db.commit()
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(409, f"Pool '{body.name}' already exists")
            raise

        row = await db.execute_fetchall("SELECT * FROM pools WHERE id = ?", (cursor.lastrowid,))
        pool = dict(row[0])
        await log_audit(db, "create", "pool", pool["id"], after=pool)
        await db.commit()

        # Write sidecar
        await write_pool_sidecar(
            pool["id"],
            pool["name"],
            pool["type"],
            pool.get("description"),
            source_nanio_pool_id=pool.get("source_nanio_pool_id"),
        )

        return pool


@router.get("/{pool_id}", response_model=PoolOut)
async def get_pool(pool_id: int):
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT * FROM pools WHERE id = ?", (pool_id,))
        if not rows:
            raise HTTPException(404, "Pool not found")
        return dict(rows[0])


@router.put("/{pool_id}", response_model=PoolOut)
async def update_pool(pool_id: int, body: PoolUpdate):
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT * FROM pools WHERE id = ?", (pool_id,))
        if not rows:
            raise HTTPException(404, "Pool not found")
        before = dict(rows[0])

        updates_raw = body.model_dump(exclude_unset=True)
        # Keep None only for source_nanio_pool_id (allows explicit clearing), drop other None fields
        updates = {k: v for k, v in updates_raw.items() if v is not None or k == "source_nanio_pool_id"}
        if not updates:
            return before

        # Determine the effective type after update
        new_type = updates.get("type", before["type"])

        # Validate source_nanio_pool_id
        if "source_nanio_pool_id" in updates:
            snp_id = updates["source_nanio_pool_id"]
            if snp_id is not None:
                if new_type != "http":
                    raise HTTPException(
                        422,
                        "source_nanio_pool_id can only be set on http pools",
                    )
                ref_rows = await db.execute_fetchall(
                    "SELECT id, type FROM pools WHERE id = ?", (snp_id,)
                )
                if not ref_rows:
                    raise HTTPException(
                        422,
                        f"source_nanio_pool_id {snp_id} does not exist",
                    )
                if dict(ref_rows[0])["type"] != "nanio":
                    raise HTTPException(
                        422,
                        f"source_nanio_pool_id must reference a nanio pool, "
                        f"but pool {snp_id} is of type '{dict(ref_rows[0])['type']}'",
                    )
            else:
                # Explicitly setting to NULL is allowed for any pool type
                pass
        elif new_type == "nanio" and before.get("source_nanio_pool_id") is not None:
            # If changing type to nanio, clear source_nanio_pool_id
            updates["source_nanio_pool_id"] = None

        # If changing type, validate existing members
        if new_type != before["type"]:
            members = await db.execute_fetchall("SELECT role FROM pool_members WHERE pool_id = ?", (pool_id,))
            for m in members:
                _validate_member_role(new_type, m["role"])

        set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
        values = list(updates.values()) + [pool_id]
        await db.execute(
            f"UPDATE pools SET {set_clause}, updated_at = datetime('now') WHERE id = ?",
            values,
        )
        await db.commit()

        rows = await db.execute_fetchall("SELECT * FROM pools WHERE id = ?", (pool_id,))
        after = dict(rows[0])

        # Regenerate config
        ok, output = await _write_pool_config(pool_id, db)
        await log_audit(db, "update", "pool", pool_id, before=before, after=after, reload_ok=ok, reload_output=output)
        await db.commit()

        # Update sidecar
        await write_pool_sidecar(
            after["id"],
            after["name"],
            after["type"],
            after.get("description"),
            source_nanio_pool_id=after.get("source_nanio_pool_id"),
        )

        return after


@router.delete("/{pool_id}", status_code=204)
async def delete_pool(pool_id: int):
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT * FROM pools WHERE id = ?", (pool_id,))
        if not rows:
            raise HTTPException(404, "Pool not found")
        pool = dict(rows[0])

        # Check for referencing routes — include which vhosts use this pool as default
        refs = await db.execute_fetchall(
            """SELECT r.id, v.server_name FROM routes r
               JOIN vhosts v ON r.vhost_id = v.id
               WHERE r.pool_id = ?""",
            (pool_id,),
        )
        if refs:
            vhost_names = ", ".join(sorted({r["server_name"] for r in refs}))
            raise HTTPException(
                409,
                f"Cannot delete pool: it is referenced by routes on vhost(s): {vhost_names}. "
                "Change the vhost default_pool_id or remove the bucket routes first.",
            )

        # Check if any vhost uses this pool as its default
        vhost_refs = await db.execute_fetchall("SELECT server_name FROM vhosts WHERE default_pool_id = ?", (pool_id,))
        if vhost_refs:
            names = ", ".join(r["server_name"] for r in vhost_refs)
            raise HTTPException(
                409,
                f"Cannot delete pool: it is the default pool for vhost(s): {names}. "
                "Change the vhost default_pool_id first.",
            )

        # Block if active migrations reference this pool
        active_mig = await db.execute_fetchall(
            """SELECT id FROM migrations
               WHERE (src_pool_id = ? OR dst_pool_id = ?)
               AND phase IN ('pending','copying','verifying','switching')""",
            (pool_id, pool_id),
        )
        if active_mig:
            ids = ", ".join(str(r["id"]) for r in active_mig)
            raise HTTPException(
                409,
                f"Cannot delete pool: active migration(s) {ids} reference this pool. "
                "Cancel or wait for them to finish first.",
            )

        # Clean up finished migration records referencing this pool
        await db.execute(
            "DELETE FROM migrations WHERE src_pool_id = ? OR dst_pool_id = ?",
            (pool_id, pool_id),
        )
        # Null out bucket_sync references (nullable column)
        await db.execute(
            "UPDATE bucket_sync SET routed_pool_id = NULL WHERE routed_pool_id = ?",
            (pool_id,),
        )

        # Delete node_configs for all members first (FK chain)
        await db.execute(
            "DELETE FROM node_configs WHERE member_id IN (SELECT id FROM pool_members WHERE pool_id = ?)",
            (pool_id,),
        )
        # Delete members, then the pool itself
        await db.execute("DELETE FROM pool_members WHERE pool_id = ?", (pool_id,))
        await db.execute("DELETE FROM pools WHERE id = ?", (pool_id,))

        # Remove config file
        from nanio_orchestrator.config import get_settings
        from nanio_orchestrator.nginx.generator import remove_config_file

        s = get_settings()
        filepath = str(s.pools_dir / f"{pool['name']}.conf")
        await remove_config_file(filepath)

        # Remove from config_files table
        await db.execute("DELETE FROM config_files WHERE path = ?", (filepath,))

        await log_audit(db, "delete", "pool", pool_id, before=pool)
        await db.commit()

        # Delete sidecar
        await delete_pool_sidecar(pool["name"])


# ── Pool Members ──────────────────────────────────────────────────────────────


@router.get("/{pool_id}/members", response_model=List[MemberOut])
async def list_members(pool_id: int):
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT * FROM pools WHERE id = ?", (pool_id,))
        if not rows:
            raise HTTPException(404, "Pool not found")
        members = await db.execute_fetchall("SELECT * FROM pool_members WHERE pool_id = ? ORDER BY id", (pool_id,))
        return [dict(m) for m in members]


@router.post("/{pool_id}/members", response_model=MemberOut, status_code=201)
async def create_member(pool_id: int, body: MemberCreate):
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT * FROM pools WHERE id = ?", (pool_id,))
        if not rows:
            raise HTTPException(404, "Pool not found")
        pool = dict(rows[0])

        _validate_member_role(pool["type"], body.role)

        cursor = await db.execute(
            """INSERT INTO pool_members (pool_id, address, role, weight, max_fails, fail_timeout_s, enabled)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                pool_id,
                body.address,
                body.role,
                body.weight,
                body.max_fails,
                body.fail_timeout_s,
                1 if body.enabled else 0,
            ),
        )
        await db.commit()

        mrows = await db.execute_fetchall("SELECT * FROM pool_members WHERE id = ?", (cursor.lastrowid,))
        member = dict(mrows[0])

        # Regenerate pool config
        ok, output = await _write_pool_config(pool_id, db)
        await log_audit(db, "create", "pool_member", member["id"], after=member, reload_ok=ok, reload_output=output)
        await db.commit()
        return member


@router.put("/{pool_id}/members/{member_id}", response_model=MemberOut)
async def update_member(pool_id: int, member_id: int, body: MemberUpdate):
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT * FROM pools WHERE id = ?", (pool_id,))
        if not rows:
            raise HTTPException(404, "Pool not found")
        pool = dict(rows[0])

        mrows = await db.execute_fetchall(
            "SELECT * FROM pool_members WHERE id = ? AND pool_id = ?", (member_id, pool_id)
        )
        if not mrows:
            raise HTTPException(404, "Member not found")
        before = dict(mrows[0])

        updates = body.model_dump(exclude_none=True)
        if not updates:
            return before

        # Validate role if changing
        new_role = updates.get("role", before["role"])
        _validate_member_role(pool["type"], new_role)

        if "enabled" in updates:
            updates["enabled"] = 1 if updates["enabled"] else 0

        set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
        values = list(updates.values()) + [member_id]
        await db.execute(
            f"UPDATE pool_members SET {set_clause}, updated_at = datetime('now') WHERE id = ?",
            values,
        )
        await db.commit()

        mrows = await db.execute_fetchall("SELECT * FROM pool_members WHERE id = ?", (member_id,))
        after = dict(mrows[0])

        ok, output = await _write_pool_config(pool_id, db)
        await log_audit(
            db, "update", "pool_member", member_id, before=before, after=after, reload_ok=ok, reload_output=output
        )
        await db.commit()
        return after


@router.delete("/{pool_id}/members/{member_id}", status_code=204)
async def delete_member(pool_id: int, member_id: int):
    async with get_db_ctx() as db:
        mrows = await db.execute_fetchall(
            "SELECT * FROM pool_members WHERE id = ? AND pool_id = ?", (member_id, pool_id)
        )
        if not mrows:
            raise HTTPException(404, "Member not found")
        before = dict(mrows[0])

        # Remove saved node config before deleting the member (FK constraint)
        await db.execute("DELETE FROM node_configs WHERE member_id = ?", (member_id,))
        await db.execute("DELETE FROM pool_members WHERE id = ?", (member_id,))
        await db.commit()

        ok, output = await _write_pool_config(pool_id, db)
        await log_audit(db, "delete", "pool_member", member_id, before=before, reload_ok=ok, reload_output=output)
        await db.commit()


# ── Node Config ───────────────────────────────────────────────────────────────


@router.get("/{pool_id}/members/{member_id}/node-config", response_model=NodeConfigOut)
async def get_member_node_config(
    pool_id: int,
    member_id: int,
    type: str = "nanio-only",
    data_dir: str = "/data",
    nanio_port: int = 9000,
    nanio_host: str = "0.0.0.0",
    nanio_region: str = "us-east-1",
    access_key: str = "",
    secret_key: str = "",
):
    if type not in ("nanio-only", "nginx-only", "nginx-nanio"):
        raise HTTPException(400, "type must be nanio-only, nginx-only, or nginx-nanio")

    async with get_db_ctx() as db:
        pool_rows = await db.execute_fetchall("SELECT * FROM pools WHERE id = ?", (pool_id,))
        if not pool_rows:
            raise HTTPException(404, "Pool not found")
        pool = dict(pool_rows[0])

        mrows = await db.execute_fetchall(
            "SELECT * FROM pool_members WHERE id = ? AND pool_id = ?", (member_id, pool_id)
        )
        if not mrows:
            raise HTTPException(404, "Member not found")
        member = dict(mrows[0])

    files = render_node_config(
        node_type=type,
        member_address=member["address"],
        pool_name=pool["name"],
        pool_type=pool["type"],
        data_dir=data_dir,
        nanio_port=nanio_port,
        nanio_host=nanio_host,
        nanio_region=nanio_region,
        access_key=access_key or None,
        secret_key=secret_key or None,
    )
    instructions = node_config_instructions(type)

    # Store in node_configs for history
    async with get_db_ctx() as db:
        await db.execute(
            "INSERT INTO node_configs (member_id, node_type, config_json) VALUES (?, ?, ?)",
            (member_id, type, json.dumps(files)),
        )
        await db.commit()

    return NodeConfigOut(
        node_type=type,
        member_address=member["address"],
        files=files,
        instructions=instructions,
    )


@router.post("/{pool_id}/members/{member_id}/node-config", response_model=NodeConfigOut)
async def generate_member_node_config(pool_id: int, member_id: int, body: NodeConfigRequest):
    """Generate node config via POST with full parameters."""
    async with get_db_ctx() as db:
        pool_rows = await db.execute_fetchall("SELECT * FROM pools WHERE id = ?", (pool_id,))
        if not pool_rows:
            raise HTTPException(404, "Pool not found")
        pool = dict(pool_rows[0])

        mrows = await db.execute_fetchall(
            "SELECT * FROM pool_members WHERE id = ? AND pool_id = ?", (member_id, pool_id)
        )
        if not mrows:
            raise HTTPException(404, "Member not found")
        member = dict(mrows[0])

    files = render_node_config(
        node_type=body.node_type,
        member_address=member["address"],
        pool_name=pool["name"],
        pool_type=pool["type"],
        data_dir=body.data_dir,
        nanio_port=body.nanio_port,
        nanio_host=body.nanio_host,
        nanio_region=body.nanio_region,
        access_key=body.access_key,
        secret_key=body.secret_key,
    )
    instructions = node_config_instructions(body.node_type)

    async with get_db_ctx() as db:
        await db.execute(
            "INSERT INTO node_configs (member_id, node_type, config_json) VALUES (?, ?, ?)",
            (member_id, body.node_type, json.dumps(files)),
        )
        await db.commit()

    return NodeConfigOut(
        node_type=body.node_type,
        member_address=member["address"],
        files=files,
        instructions=instructions,
    )


@router.get("/{pool_id}/node-config-summary")
async def pool_node_config_summary(pool_id: int):
    """Get node config summary for all members in a pool."""
    async with get_db_ctx() as db:
        pool_rows = await db.execute_fetchall("SELECT * FROM pools WHERE id = ?", (pool_id,))
        if not pool_rows:
            raise HTTPException(404, "Pool not found")

        members = await db.execute_fetchall("SELECT * FROM pool_members WHERE pool_id = ? ORDER BY id", (pool_id,))
        result = []
        for m in members:
            configs = await db.execute_fetchall(
                "SELECT * FROM node_configs WHERE member_id = ? ORDER BY generated_at DESC LIMIT 5",
                (m["id"],),
            )
            result.append(
                {
                    "member": dict(m),
                    "recent_configs": [dict(c) for c in configs],
                }
            )
        return result


@router.get("/{pool_id}/buckets/status")
async def pool_bucket_status(pool_id: int):
    """List all buckets on a nanio pool with their routing status across all vhosts.

    Status values:
    - ``orphaned``: a migration record marks this pool as the orphaned source for this bucket —
      data here is stale and should be purged.
    - ``routed``: a dedicated nginx route in at least one vhost points /{bucket}/ → this pool.
    - ``via_default``: no dedicated route, but this pool is the default_pool for at least one
      vhost — traffic reaches this bucket via the catch-all route.
    - ``unrouted``: the bucket exists on this pool but no vhost serves it from here.
    """
    async with get_db_ctx() as db:
        pool_rows = await db.execute_fetchall("SELECT * FROM pools WHERE id = ?", (pool_id,))
        if not pool_rows:
            raise HTTPException(404, "Pool not found")
        pool = dict(pool_rows[0])
        if pool["type"] != "nanio":
            raise HTTPException(400, "Bucket status is only available for nanio pools")

        member_rows = await db.execute_fetchall(
            "SELECT address FROM pool_members WHERE pool_id = ? AND enabled = 1 ORDER BY id LIMIT 1",
            (pool_id,),
        )
        if not member_rows:
            raise HTTPException(400, "Pool has no enabled members")
        member_address = dict(member_rows[0])["address"]

        # Dedicated routes pointing to this pool: bucket_name → [{vhost_id, server_name}]
        route_rows = await db.execute_fetchall(
            """SELECT r.path_prefix, v.id AS vhost_id, v.server_name
               FROM routes r
               JOIN vhosts v ON r.vhost_id = v.id
               WHERE r.pool_id = ? AND r.enabled = 1""",
            (pool_id,),
        )
        routed: dict[str, list] = {}
        for r in route_rows:
            rd = dict(r)
            bucket_name = rd["path_prefix"].strip("/")
            if bucket_name:
                routed.setdefault(bucket_name, []).append(
                    {"vhost_id": rd["vhost_id"], "server_name": rd["server_name"]}
                )

        # Vhosts where this pool is the default
        default_vhost_rows = await db.execute_fetchall(
            "SELECT id, server_name FROM vhosts WHERE default_pool_id = ? ORDER BY server_name",
            (pool_id,),
        )
        default_vhosts = [dict(r) for r in default_vhost_rows]

        # Buckets with orphaned migration records pointing to this pool
        orphan_rows = await db.execute_fetchall(
            """SELECT DISTINCT bucket FROM migrations
               WHERE orphaned_source_pool_id = ? AND bucket IS NOT NULL""",
            (pool_id,),
        )
        orphaned_set = {dict(r)["bucket"] for r in orphan_rows}

        # All vhosts (for route modal context)
        all_vhost_rows = await db.execute_fetchall(
            """SELECT v.id, v.server_name FROM vhosts v
               JOIN pools p ON v.default_pool_id = p.id
               WHERE p.type = 'nanio'
               ORDER BY v.server_name""",
        )
        all_vhosts = [dict(r) for r in all_vhost_rows]

    # ListBuckets via S3 API
    ak, sk, _ = await get_pool_s3_params(pool_id)
    try:
        raw_buckets = await s3_list_buckets(member_address, access_key=ak, secret_key=sk)
    except Exception as exc:
        raise HTTPException(502, f"Failed to list buckets on pool '{pool['name']}': {exc}")

    buckets = []
    for b in raw_buckets:
        name = b["name"]
        if name in routed:
            status = "routed"
        elif name in orphaned_set:
            status = "orphaned"
        elif default_vhosts:
            status = "via_default"
        else:
            status = "unrouted"

        buckets.append(
            {
                "bucket": name,
                "status": status,
                "routed_in": routed.get(name, []),
                "default_vhosts": default_vhosts if status == "via_default" else [],
            }
        )

    # Sort: unrouted/orphaned first, then routed, then via_default
    _order = {"unrouted": 0, "orphaned": 1, "via_default": 2, "routed": 3}
    buckets.sort(key=lambda x: (_order.get(x["status"], 9), x["bucket"]))

    return {
        "pool_id": pool_id,
        "pool_name": pool["name"],
        "member_address": member_address,
        "buckets": buckets,
        "all_vhosts": all_vhosts,
    }


@router.get("/{pool_id}/buckets/{bucket}/objects")
async def pool_bucket_objects(pool_id: int, bucket: str):
    """List all objects in a bucket on a specific nanio pool.

    Intended for inspecting orphaned buckets before deciding whether to purge them.
    """
    async with get_db_ctx() as db:
        pool_rows = await db.execute_fetchall("SELECT * FROM pools WHERE id = ?", (pool_id,))
        if not pool_rows:
            raise HTTPException(404, "Pool not found")
        pool = dict(pool_rows[0])
        if pool["type"] != "nanio":
            raise HTTPException(400, "Object listing is only available for nanio pools")
        member_rows = await db.execute_fetchall(
            "SELECT address FROM pool_members WHERE pool_id = ? AND enabled = 1 ORDER BY id LIMIT 1",
            (pool_id,),
        )
        if not member_rows:
            raise HTTPException(400, "Pool has no enabled members")
        member_address = dict(member_rows[0])["address"]

    ak, sk, _ = await get_pool_s3_params(pool_id)
    try:
        keys = await list_objects(member_address, bucket, access_key=ak, secret_key=sk)
    except Exception as exc:
        raise HTTPException(502, f"Failed to list objects in bucket '{bucket}': {exc}")

    return {"pool_id": pool_id, "bucket": bucket, "objects": keys, "count": len(keys)}


@router.post("/{pool_id}/buckets/{bucket}/purge")
async def pool_bucket_purge(pool_id: int, bucket: str):
    """Delete all objects in a bucket on a specific nanio pool.

    Use this to clean up orphaned buckets after a migration — the bucket container
    is preserved so ListBuckets still works; only the object content is removed.
    """
    async with get_db_ctx() as db:
        pool_rows = await db.execute_fetchall("SELECT * FROM pools WHERE id = ?", (pool_id,))
        if not pool_rows:
            raise HTTPException(404, "Pool not found")
        pool = dict(pool_rows[0])
        if pool["type"] != "nanio":
            raise HTTPException(400, "Purge is only available for nanio pools")
        member_rows = await db.execute_fetchall(
            "SELECT address FROM pool_members WHERE pool_id = ? AND enabled = 1 ORDER BY id LIMIT 1",
            (pool_id,),
        )
        if not member_rows:
            raise HTTPException(400, "Pool has no enabled members")
        member_address = dict(member_rows[0])["address"]

    ak, sk, _ = await get_pool_s3_params(pool_id)
    try:
        keys = await list_objects(member_address, bucket, access_key=ak, secret_key=sk)
    except Exception as exc:
        raise HTTPException(502, f"Failed to list objects in bucket '{bucket}': {exc}")

    deleted = 0
    errors: list = []
    for key in keys:
        try:
            ok = await delete_object(member_address, bucket, key, access_key=ak, secret_key=sk)
            if ok:
                deleted += 1
            else:
                errors.append(key)
        except Exception:
            errors.append(key)

    async with get_db_ctx() as db:
        await log_audit(
            db,
            "pool_bucket_purge",
            "pool",
            pool_id,
            after={"pool_id": pool_id, "bucket": bucket, "deleted": deleted, "total": len(keys)},
        )
        await db.commit()

    return {"ok": True, "pool_id": pool_id, "bucket": bucket, "deleted": deleted, "total": len(keys), "errors": errors}
