"""Config operations API — status, validate, rebuild, sync, preview."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import List

import aiofiles
from fastapi import APIRouter, Body, HTTPException

from nanio_orchestrator.config import get_settings
from nanio_orchestrator.db import get_db_ctx
from nanio_orchestrator.models import ConfigFileStatus, ConfigStatus, NginxResult
from nanio_orchestrator.nginx.executor import reload_nginx, test_config
from nanio_orchestrator.nginx.generator import (
    generate_all_configs,
    generate_pool_config,
    generate_vhost_config,
    record_file_state,
    sha256_str,
    write_config_atomic,
)
from nanio_orchestrator.nginx.parser import is_managed_file, scan_managed_files

router = APIRouter(prefix="/api/config", tags=["config"])


async def _sha256_file(filepath: str) -> str | None:
    """Read a file and return its SHA256 hash, or None if file doesn't exist."""
    try:
        async with aiofiles.open(filepath, "r") as f:
            content = await f.read()
        return sha256_str(content)
    except FileNotFoundError:
        return None


# ── Status ────────────────────────────────────────────────────────────────────


@router.get("/status", response_model=ConfigStatus)
async def config_status():
    """Drift status per file, last reload time + result."""
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT * FROM config_files ORDER BY path")
        files: List[ConfigFileStatus] = []
        for r in rows:
            disk_hash = await _sha256_file(r["path"])
            drifted = disk_hash is not None and r["sha256_db"] is not None and disk_hash != r["sha256_db"]
            files.append(ConfigFileStatus(
                path=r["path"],
                sha256_disk=disk_hash,
                sha256_db=r["sha256_db"],
                drifted=drifted,
                last_synced_at=r["last_synced_at"],
            ))

        # Last reload from audit_log
        reload_rows = await db.execute_fetchall(
            """SELECT nginx_reload_ok, created_at FROM audit_log
               WHERE nginx_reload_ok IS NOT NULL
               ORDER BY id DESC LIMIT 1"""
        )
        last_reload_ok = None
        last_reload_at = None
        if reload_rows:
            last_reload_ok = bool(reload_rows[0]["nginx_reload_ok"])
            last_reload_at = reload_rows[0]["created_at"]

    return ConfigStatus(
        files=files,
        last_reload_ok=last_reload_ok,
        last_reload_at=last_reload_at,
    )


# ── Validate ──────────────────────────────────────────────────────────────────


@router.post("/validate", response_model=NginxResult)
async def validate_config():
    """Run nginx -t and return result."""
    result = await test_config()
    return NginxResult(ok=result.ok, output=result.output)


# ── Reload ────────────────────────────────────────────────────────────────────


@router.post("/reload", response_model=NginxResult)
async def reload_config():
    """Run nginx -s reload without config change."""
    result = await reload_nginx()
    async with get_db_ctx() as db:
        await db.execute(
            """INSERT INTO audit_log (action, entity_type, entity_id, nginx_reload_ok, nginx_reload_output)
               VALUES ('manual_reload', 'config', NULL, ?, ?)""",
            (1 if result.ok else 0, result.output),
        )
        await db.commit()
    return NginxResult(ok=result.ok, output=result.output)


# ── Sync (disk → DB) ─────────────────────────────────────────────────────────


@router.post("/sync")
async def sync_from_disk():
    """Re-import disk state into the DB."""
    s = get_settings()
    managed = scan_managed_files(s.nginx_config_dir)
    imported = []

    async with get_db_ctx() as db:
        for item in managed:
            h = sha256_str(item["content"])
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            await db.execute(
                """INSERT INTO config_files (path, sha256_disk, sha256_db, content_snapshot, last_synced_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET
                     sha256_disk = excluded.sha256_disk,
                     content_snapshot = excluded.content_snapshot,
                     last_synced_at = excluded.last_synced_at""",
                (item["path"], h, h, item["content"], now),
            )
            imported.append(item["path"])
        await db.commit()

    return {"imported": imported, "count": len(imported)}


# ── Rebuild (DB → disk → reload) ─────────────────────────────────────────────


@router.post("/rebuild")
async def rebuild_all():
    """Rebuild all config files from DB, validate, and reload."""
    import os
    configs = await generate_all_configs()
    errors = []
    written = []
    removed = []

    # Separate empty-pool entries (content=None) from real configs
    to_write = [(fp, ct) for fp, ct in configs if ct is not None]
    to_remove = [fp for fp, ct in configs if ct is None]

    # Remove files for empty pools
    async with get_db_ctx() as db:
        for filepath in to_remove:
            p = Path(filepath)
            if p.exists():
                p.unlink()
                removed.append(filepath)
            await db.execute("DELETE FROM config_files WHERE path = ?", (filepath,))
        await db.commit()

    # Write .tmp for real configs
    for filepath, content in to_write:
        tmp = filepath + ".tmp"
        async with aiofiles.open(tmp, "w") as f:
            await f.write(content)

    # Test with all .tmp files renamed
    test_result = await test_config()
    if not test_result.ok:
        # Clean up .tmp files
        for filepath, _ in to_write:
            tmp = filepath + ".tmp"
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return {"ok": False, "output": test_result.output, "written": [], "removed": removed}

    # Atomic rename all
    for filepath, content in to_write:
        tmp = filepath + ".tmp"
        try:
            os.rename(tmp, filepath)
            written.append(filepath)
        except OSError as e:
            errors.append(f"{filepath}: {e}")

    reload_result = await reload_nginx()

    # Record all in DB
    async with get_db_ctx() as db:
        for filepath, content in to_write:
            if filepath in written:
                await record_file_state(db, filepath, content)
        await db.execute(
            """INSERT INTO audit_log (action, entity_type, entity_id, nginx_reload_ok, nginx_reload_output)
               VALUES ('rebuild', 'config', NULL, ?, ?)""",
            (1 if reload_result.ok else 0, reload_result.output),
        )
        await db.commit()

    return {
        "ok": reload_result.ok and not errors,
        "output": reload_result.output,
        "written": written,
        "removed": removed,
        "errors": errors,
    }


# ── Per-file drift resolution ────────────────────────────────────────────────


@router.post("/absorb-file")
async def absorb_file(path: str = Body(..., embed=True)):
    """Accept a drifted file: read current disk state into the DB (sha256_db = sha256_disk)."""
    try:
        async with aiofiles.open(path, "r") as f:
            content = await f.read()
    except FileNotFoundError:
        raise HTTPException(404, f"File not found on disk: {path}")

    h = sha256_str(content)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    async with get_db_ctx() as db:
        await db.execute(
            """INSERT INTO config_files (path, sha256_disk, sha256_db, content_snapshot, last_synced_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET
                 sha256_disk = excluded.sha256_disk,
                 sha256_db   = excluded.sha256_db,
                 content_snapshot = excluded.content_snapshot,
                 last_synced_at   = excluded.last_synced_at""",
            (path, h, h, content, now),
        )
        await db.execute(
            """INSERT INTO audit_log (action, entity_type, entity_id, nginx_reload_ok, nginx_reload_output)
               VALUES ('absorb_drift', 'config', NULL, NULL, ?)""",
            (f"Absorbed drift for {path}",),
        )
        await db.commit()
    return {"ok": True, "path": path, "sha256": h}


@router.post("/rewrite-file")
async def rewrite_file(path: str = Body(..., embed=True)):
    """Rewrite a single config file from DB state, validate, and reload."""
    import os
    s = get_settings()

    # Determine if this is a pool or vhost config by matching against DB entries
    async with get_db_ctx() as db:
        pools = await db.execute_fetchall("SELECT id, name FROM pools")
        vhosts = await db.execute_fetchall("SELECT id, server_name FROM vhosts")

    pool_match = next(
        (p for p in pools if str(s.pools_dir / f"{p['name']}.conf") == path), None
    )
    vhost_match = next(
        (v for v in vhosts if str(s.vhosts_dir / f"{v['server_name']}.conf") == path), None
    )

    if pool_match:
        filepath, content = await generate_pool_config(pool_match["id"])
        if content is None:
            # Pool is now empty — remove
            p = Path(filepath)
            if p.exists():
                p.unlink()
            async with get_db_ctx() as db:
                await db.execute("DELETE FROM config_files WHERE path = ?", (filepath,))
                await db.commit()
            return {"ok": True, "action": "removed", "path": filepath, "reason": "Pool has no members"}
    elif vhost_match:
        filepath, content = await generate_vhost_config(vhost_match["id"])
    else:
        raise HTTPException(404, f"No pool or vhost found matching path: {path}")

    # Write .tmp, test, rename, reload
    tmp = filepath + ".tmp"
    async with aiofiles.open(tmp, "w") as f:
        await f.write(content)

    test_result = await test_config()
    if not test_result.ok:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return {"ok": False, "output": test_result.output}

    os.rename(tmp, filepath)
    reload_result = await reload_nginx()

    async with get_db_ctx() as db:
        await record_file_state(db, filepath, content)
        await db.execute(
            """INSERT INTO audit_log (action, entity_type, entity_id, nginx_reload_ok, nginx_reload_output)
               VALUES ('rewrite_file', 'config', NULL, ?, ?)""",
            (1 if reload_result.ok else 0, reload_result.output),
        )
        await db.commit()

    return {"ok": reload_result.ok, "output": reload_result.output, "path": filepath}


# ── Preview ───────────────────────────────────────────────────────────────────


@router.get("/preview/pool/{pool_id}")
async def preview_pool_config(pool_id: int):
    """Render upstream config without applying."""
    try:
        filepath, content = await generate_pool_config(pool_id)
    except ValueError:
        raise HTTPException(404, "Pool not found")
    return {"filepath": filepath, "content": content}


@router.get("/preview/vhost/{vhost_id}")
async def preview_vhost_config(vhost_id: int):
    """Render server block config without applying."""
    try:
        filepath, content = await generate_vhost_config(vhost_id)
    except ValueError:
        raise HTTPException(404, "Vhost not found")
    return {"filepath": filepath, "content": content}
