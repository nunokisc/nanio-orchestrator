"""Vhosts + Routes CRUD API with nginx config generation + reload."""

from __future__ import annotations

import json
import os
from typing import List

import aiofiles
from fastapi import APIRouter, HTTPException

from nanio_orchestrator.backup import trigger_backup
from nanio_orchestrator.db import get_db_ctx
from nanio_orchestrator.sidecar import write_vhost_sidecar, delete_vhost_sidecar
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

    test_result = await test_config()
    if not test_result.ok:
        os.unlink(tmp_path)
        return False, test_result.output

    os.rename(tmp_path, filepath)
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
        await db.commit()

        # Write sidecar
        default_pool_name = None
        if vhost.get("default_pool_id"):
            pr = await db.execute_fetchall("SELECT name FROM pools WHERE id = ?", (vhost["default_pool_id"],))
            if pr:
                default_pool_name = pr[0]["name"]
        write_vhost_sidecar(vhost["id"], vhost["server_name"], vhost.get("default_pool_id"), default_pool_name)

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

        rows = await db.execute_fetchall("SELECT * FROM vhosts WHERE id = ?", (vhost_id,))
        after = dict(rows[0])

        ok, output = await _apply_vhost_config(vhost_id, db)
        await _audit(db, "update", "vhost", vhost_id, before=before, after=after,
                     reload_ok=ok, reload_output=output)
        await db.commit()

        # Update sidecar
        default_pool_name = None
        if after.get("default_pool_id"):
            pr = await db.execute_fetchall("SELECT name FROM pools WHERE id = ?", (after["default_pool_id"],))
            if pr:
                default_pool_name = pr[0]["name"]
        write_vhost_sidecar(after["id"], after["server_name"], after.get("default_pool_id"), default_pool_name)

        return after


@router.delete("/{vhost_id}", status_code=204)
async def delete_vhost(vhost_id: int):
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT * FROM vhosts WHERE id = ?", (vhost_id,))
        if not rows:
            raise HTTPException(404, "Vhost not found")
        vhost = dict(rows[0])

        refs = await db.execute_fetchall("SELECT id FROM routes WHERE vhost_id = ?", (vhost_id,))
        if refs:
            raise HTTPException(409, "Cannot delete vhost: routes still reference it")

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
        delete_vhost_sidecar(vhost["server_name"])


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


@router.post("/{vhost_id}/routes", response_model=RouteOut, status_code=201)
async def create_route(vhost_id: int, body: RouteCreate):
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT * FROM vhosts WHERE id = ?", (vhost_id,))
        if not rows:
            raise HTTPException(404, "Vhost not found")

        pool_rows = await db.execute_fetchall("SELECT * FROM pools WHERE id = ?", (body.pool_id,))
        if not pool_rows:
            raise HTTPException(400, f"Pool {body.pool_id} not found")

        try:
            cursor = await db.execute(
                """INSERT INTO routes (vhost_id, path_prefix, pool_id, key_prefix, extra_directives, enabled)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (vhost_id, body.path_prefix, body.pool_id, body.key_prefix,
                 body.extra_directives, 1 if body.enabled else 0),
            )
            await db.commit()
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(409, f"Route with prefix '{body.path_prefix}' already exists for this vhost")
            raise

        rrows = await db.execute_fetchall(
            """SELECT r.*, p.name as pool_name
               FROM routes r JOIN pools p ON r.pool_id = p.id
               WHERE r.id = ?""",
            (cursor.lastrowid,),
        )
        route = dict(rrows[0])

        ok, output = await _apply_vhost_config(vhost_id, db)
        await _audit(db, "create", "route", route["id"], after=route,
                     reload_ok=ok, reload_output=output)
        await db.commit()
        return route


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
