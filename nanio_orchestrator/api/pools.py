"""Pools CRUD API with nginx config generation + reload."""

from __future__ import annotations

import json
from typing import List

from fastapi import APIRouter, HTTPException, status

from nanio_orchestrator.audit_log import log_audit
from nanio_orchestrator.backup import trigger_backup
from nanio_orchestrator.db import get_db_ctx
from nanio_orchestrator.sidecar import write_pool_sidecar as _write_pool_sidecar_sync, delete_pool_sidecar as _delete_pool_sidecar_sync
import asyncio as _asyncio

async def write_pool_sidecar(*args, **kwargs):
    await _asyncio.to_thread(_write_pool_sidecar_sync, *args, **kwargs)

async def delete_pool_sidecar(*args, **kwargs):
    await _asyncio.to_thread(_delete_pool_sidecar_sync, *args, **kwargs)
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
from nanio_orchestrator.nginx.generator import (
    generate_pool_config,
    node_config_instructions,
    record_file_state,
    render_node_config,
)
from nanio_orchestrator.nginx.executor import test_config, reload_nginx

router = APIRouter(prefix="/api/pools", tags=["pools"])


async def _apply_pool_config(pool_id: int, db) -> tuple:
    """Generate, test, write, reload, record. Returns (ok, output).
    If the pool has no members the config file is removed instead of written.
    """
    filepath, content = await generate_pool_config(pool_id)
    import aiofiles, os
    from nanio_orchestrator.nginx.generator import remove_config_file

    # Empty pool — remove file so nginx doesn't see an invalid upstream block
    if content is None:
        await remove_config_file(filepath)
        await db.execute("DELETE FROM config_files WHERE path = ?", (filepath,))
        reload_result = await reload_nginx()
        return reload_result.ok, f"Pool has no members — config file removed.\nnginx -s reload: {reload_result.output}"

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

    # Reload
    reload_result = await reload_nginx()

    # Record in DB
    await record_file_state(db, filepath, content)
    await db.commit()

    combined = f"nginx -t: {test_result.output}\nnginx -s reload: {reload_result.output}"

    # Trigger DB backup after successful write
    await trigger_backup()

    return reload_result.ok, combined


def _validate_member_role(pool_type: str, role: str) -> None:
    """Enforce role constraints per pool type."""
    if pool_type == "nanio" and role != "active":
        raise HTTPException(
            status_code=400,
            detail="nanio pools use shared storage — all members are active, backup/replica not allowed",
        )
    if pool_type in ("http", "cold") and role not in ("primary", "replica"):
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
        try:
            cursor = await db.execute(
                """INSERT INTO pools (name, description, type, lb_method, keepalive)
                   VALUES (?, ?, ?, ?, ?)""",
                (body.name, body.description, body.type, body.lb_method, body.keepalive),
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
        await write_pool_sidecar(pool["id"], pool["name"], pool["type"], pool.get("description"))

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

        updates = body.model_dump(exclude_none=True)
        if not updates:
            return before

        # If changing type, validate existing members
        new_type = updates.get("type", before["type"])
        if new_type != before["type"]:
            members = await db.execute_fetchall(
                "SELECT role FROM pool_members WHERE pool_id = ?", (pool_id,)
            )
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
        ok, output = await _apply_pool_config(pool_id, db)
        await log_audit(db, "update", "pool", pool_id, before=before, after=after,
                     reload_ok=ok, reload_output=output)
        await db.commit()

        # Update sidecar
        await write_pool_sidecar(after["id"], after["name"], after["type"], after.get("description"))

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
        vhost_refs = await db.execute_fetchall(
            "SELECT server_name FROM vhosts WHERE default_pool_id = ?", (pool_id,)
        )
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
        import os
        s = get_settings()
        filepath = str(s.pools_dir / f"{pool['name']}.conf")
        await remove_config_file(filepath)

        # Remove from config_files table
        await db.execute("DELETE FROM config_files WHERE path = ?", (filepath,))

        # Reload nginx
        reload_result = await reload_nginx()
        await log_audit(db, "delete", "pool", pool_id, before=pool,
                     reload_ok=reload_result.ok, reload_output=reload_result.output)
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
        members = await db.execute_fetchall(
            "SELECT * FROM pool_members WHERE pool_id = ? ORDER BY id", (pool_id,)
        )
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
            (pool_id, body.address, body.role, body.weight, body.max_fails,
             body.fail_timeout_s, 1 if body.enabled else 0),
        )
        await db.commit()

        mrows = await db.execute_fetchall("SELECT * FROM pool_members WHERE id = ?", (cursor.lastrowid,))
        member = dict(mrows[0])

        # Regenerate pool config
        ok, output = await _apply_pool_config(pool_id, db)
        await log_audit(db, "create", "pool_member", member["id"], after=member,
                     reload_ok=ok, reload_output=output)
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

        ok, output = await _apply_pool_config(pool_id, db)
        await log_audit(db, "update", "pool_member", member_id, before=before, after=after,
                     reload_ok=ok, reload_output=output)
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

        ok, output = await _apply_pool_config(pool_id, db)
        await log_audit(db, "delete", "pool_member", member_id, before=before,
                     reload_ok=ok, reload_output=output)
        await db.commit()


# ── Node Config ───────────────────────────────────────────────────────────────


@router.get("/{pool_id}/members/{member_id}/node-config", response_model=NodeConfigOut)
async def get_member_node_config(
    pool_id: int, member_id: int, type: str = "nanio-only",
    data_dir: str = "/data", nanio_port: int = 9000,
    nanio_host: str = "0.0.0.0", nanio_region: str = "us-east-1",
    access_key: str = "", secret_key: str = "",
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

        members = await db.execute_fetchall(
            "SELECT * FROM pool_members WHERE pool_id = ? ORDER BY id", (pool_id,)
        )
        result = []
        for m in members:
            configs = await db.execute_fetchall(
                "SELECT * FROM node_configs WHERE member_id = ? ORDER BY generated_at DESC LIMIT 5",
                (m["id"],),
            )
            result.append({
                "member": dict(m),
                "recent_configs": [dict(c) for c in configs],
            })
        return result
