"""Config operations API — status, validate, rebuild, sync, preview."""

from __future__ import annotations

from pathlib import Path
from typing import List

import aiofiles
from fastapi import APIRouter, Body, HTTPException

from nanio_orchestrator.audit_log import log_audit
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
from nanio_orchestrator.nginx.parser import is_managed_file

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
        await log_audit(db, "manual_reload", "config", None,
                        reload_ok=result.ok, reload_output=result.output)
        await db.commit()
    return NginxResult(ok=result.ok, output=result.output)


# ── Sync (disk → DB) ─────────────────────────────────────────────────────────


@router.post("/sync")
async def sync_from_disk():
    """Additive import of disk state into the DB.

    Scans managed nginx config files and sidecar files.  Upserts pools,
    members, vhosts, and routes — never deletes existing data.  Safe to run
    at any time; running it twice is idempotent.
    """
    from datetime import datetime, timezone
    from nanio_orchestrator.nginx.parser import is_managed_file, parse_upstream_block, parse_vhost_block
    from nanio_orchestrator.sidecar import scan_pool_sidecars, scan_vhost_sidecars

    s = get_settings()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    pool_sidecars = {sc["name"]: sc for sc in scan_pool_sidecars() if sc.get("name")}
    vhost_sidecars = {sc["server_name"]: sc for sc in scan_vhost_sidecars() if sc.get("server_name")}

    pools_new = pools_updated = members_new = 0
    vhosts_new = vhosts_updated = routes_synced = files_synced = 0
    warnings: list = []

    async with get_db_ctx() as db:
        pool_name_to_id: dict = {}

        # ── Step 1: pools from upstream configs ───────────────────────────
        for conf_path in sorted(s.pools_dir.glob("*.conf")):
            try:
                content = conf_path.read_text(encoding="utf-8")
            except OSError:
                continue
            if not is_managed_file(content):
                continue

            parsed = parse_upstream_block(content)
            if not parsed or not parsed["name"]:
                continue

            name = parsed["name"]
            sidecar = pool_sidecars.get(name, {})
            pool_type = sidecar.get("type", "nanio")
            description = sidecar.get("description")

            existing = await db.execute_fetchall("SELECT id FROM pools WHERE name=?", (name,))
            if existing:
                pool_id = existing[0]["id"]
                await db.execute(
                    """UPDATE pools SET description=?, type=?, lb_method=?, keepalive=?,
                       updated_at=datetime('now') WHERE id=?""",
                    (description, pool_type, parsed["lb_method"], parsed["keepalive"], pool_id),
                )
                pools_updated += 1
            else:
                cursor = await db.execute(
                    "INSERT INTO pools (name, description, type, lb_method, keepalive) VALUES (?,?,?,?,?)",
                    (name, description, pool_type, parsed["lb_method"], parsed["keepalive"]),
                )
                pool_id = cursor.lastrowid
                pools_new += 1

            pool_name_to_id[name] = pool_id

            for m in parsed["members"]:
                already = await db.execute_fetchall(
                    "SELECT id FROM pool_members WHERE pool_id=? AND address=?", (pool_id, m["address"])
                )
                if not already:
                    await db.execute(
                        """INSERT INTO pool_members
                           (pool_id, address, role, weight, max_fails, fail_timeout_s, enabled)
                           VALUES (?,?,?,?,?,?,1)""",
                        (pool_id, m["address"], m["role"], m["weight"],
                         m["max_fails"], m["fail_timeout_s"]),
                    )
                    members_new += 1

            creds = sidecar.get("credentials")
            if creds and creds.get("access_key_enc") and creds.get("secret_key_enc"):
                no_creds = await db.execute_fetchall(
                    "SELECT id FROM pool_credentials WHERE pool_id=?", (pool_id,)
                )
                if not no_creds:
                    await db.execute(
                        """INSERT INTO pool_credentials
                           (pool_id, access_key_enc, secret_key_enc, endpoint_url, region)
                           VALUES (?,?,?,?,?)""",
                        (pool_id, creds["access_key_enc"], creds["secret_key_enc"],
                         creds.get("endpoint_url"), creds.get("region", "us-east-1")),
                    )

            h = sha256_str(content)
            await db.execute(
                """INSERT INTO config_files (path, sha256_disk, sha256_db, content_snapshot, last_synced_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(path) DO UPDATE SET
                     sha256_disk=excluded.sha256_disk, sha256_db=excluded.sha256_db,
                     content_snapshot=excluded.content_snapshot,
                     last_synced_at=excluded.last_synced_at""",
                (str(conf_path), h, h, content, now),
            )
            files_synced += 1

        # ── Step 2: vhosts + routes from server configs ───────────────────
        for conf_path in sorted(s.vhosts_dir.glob("*.conf")):
            try:
                content = conf_path.read_text(encoding="utf-8")
            except OSError:
                continue
            if not is_managed_file(content):
                continue

            parsed = parse_vhost_block(content)
            if not parsed or not parsed["server_name"]:
                continue

            server_name = parsed["server_name"]
            sidecar = vhost_sidecars.get(server_name, {})

            default_pool_id = None
            sc_pool_name = sidecar.get("default_pool_name")
            if sc_pool_name:
                default_pool_id = pool_name_to_id.get(sc_pool_name)
                if not default_pool_id:
                    row = await db.execute_fetchall("SELECT id FROM pools WHERE name=?", (sc_pool_name,))
                    if row:
                        default_pool_id = row[0]["id"]

            existing_v = await db.execute_fetchall(
                "SELECT id FROM vhosts WHERE server_name=?", (server_name,)
            )
            if existing_v:
                vhost_id = existing_v[0]["id"]
                await db.execute(
                    """UPDATE vhosts SET listen_port=?, ssl=?, ssl_cert_path=?, ssl_key_path=?,
                       default_pool_id=COALESCE(?,default_pool_id), updated_at=datetime('now')
                       WHERE id=?""",
                    (parsed["listen_port"], 1 if parsed["ssl"] else 0,
                     parsed["ssl_cert_path"], parsed["ssl_key_path"],
                     default_pool_id, vhost_id),
                )
                vhosts_updated += 1
            else:
                cursor = await db.execute(
                    """INSERT INTO vhosts
                       (server_name, listen_port, ssl, ssl_cert_path, ssl_key_path, enabled, default_pool_id)
                       VALUES (?,?,?,?,?,1,?)""",
                    (server_name, parsed["listen_port"], 1 if parsed["ssl"] else 0,
                     parsed["ssl_cert_path"], parsed["ssl_key_path"], default_pool_id),
                )
                vhost_id = cursor.lastrowid
                vhosts_new += 1

            for route in parsed["routes"]:
                pool_name = route["pool_name"]
                pool_id = pool_name_to_id.get(pool_name)
                if not pool_id:
                    row = await db.execute_fetchall("SELECT id FROM pools WHERE name=?", (pool_name,))
                    pool_id = row[0]["id"] if row else None
                if not pool_id:
                    warnings.append(
                        f"Route {route['path_prefix']} on {server_name}: pool '{pool_name}' not found — skipped"
                    )
                    continue

                await db.execute(
                    """INSERT INTO routes (vhost_id, path_prefix, pool_id, key_prefix, enabled)
                       VALUES (?,?,?,?,1)
                       ON CONFLICT(vhost_id, path_prefix) DO UPDATE SET
                         pool_id=excluded.pool_id,
                         key_prefix=excluded.key_prefix,
                         updated_at=datetime('now')""",
                    (vhost_id, route["path_prefix"], pool_id, route.get("key_prefix")),
                )
                routes_synced += 1

            h = sha256_str(content)
            await db.execute(
                """INSERT INTO config_files (path, sha256_disk, sha256_db, content_snapshot, last_synced_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(path) DO UPDATE SET
                     sha256_disk=excluded.sha256_disk, sha256_db=excluded.sha256_db,
                     content_snapshot=excluded.content_snapshot,
                     last_synced_at=excluded.last_synced_at""",
                (str(conf_path), h, h, content, now),
            )
            files_synced += 1

        await db.commit()

    return {
        "ok": True,
        "pools_new": pools_new,
        "pools_updated": pools_updated,
        "members_new": members_new,
        "vhosts_new": vhosts_new,
        "vhosts_updated": vhosts_updated,
        "routes_synced": routes_synced,
        "files_synced": files_synced,
        "warnings": warnings,
    }


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

    # Save old configs for rollback, then rename .tmp → live
    old_configs: dict = {}
    for filepath, content in to_write:
        tmp = filepath + ".tmp"
        if os.path.exists(filepath):
            async with aiofiles.open(filepath, "r") as f:
                old_configs[filepath] = await f.read()
        try:
            os.rename(tmp, filepath)
        except OSError as e:
            errors.append(f"{filepath}: {e}")

    # Test the actual config nginx will use (all files now in place)
    test_result = await test_config()
    if not test_result.ok:
        # Restore old configs on failure
        for filepath, content in to_write:
            if filepath in old_configs:
                async with aiofiles.open(filepath, "w") as f:
                    await f.write(old_configs[filepath])
            elif os.path.exists(filepath):
                try:
                    os.unlink(filepath)
                except OSError:
                    pass
        return {"ok": False, "output": test_result.output, "written": [], "removed": removed}

    written = [fp for fp, _ in to_write if fp not in [e.split(":")[0] for e in errors]]

    reload_result = await reload_nginx()

    # Record all in DB
    async with get_db_ctx() as db:
        for filepath, content in to_write:
            if filepath in written:
                await record_file_state(db, filepath, content)
        await log_audit(db, "rebuild", "config", None,
                        reload_ok=reload_result.ok, reload_output=reload_result.output)
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
        await log_audit(db, "absorb_drift", "config", None,
                        reload_output=f"Absorbed drift for {path}")
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
        await log_audit(db, "rewrite_file", "config", None,
                        reload_ok=reload_result.ok, reload_output=reload_result.output)
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


# ── Rebuild from disk ─────────────────────────────────────────────────────────


@router.post("/rebuild-from-disk")
async def rebuild_from_disk_endpoint(dry_run: bool = False, force: bool = False):
    """Reconstruct the entire database from nginx configs + sidecar files.

    Query params:
      - dry_run: report what would be imported, write nothing
      - force: proceed even if DB has existing data (clears first)
    """
    from nanio_orchestrator.rebuild import rebuild_from_disk
    from nanio_orchestrator.db import get_db_ctx, init_db, CLEAR_TABLES

    if not dry_run:
        await init_db()

    if not dry_run and not force:
        async with get_db_ctx() as db:
            pools = await db.execute_fetchall("SELECT COUNT(*) as cnt FROM pools")
            if pools[0]["cnt"] > 0:
                raise HTTPException(
                    409,
                    "Database already contains data. Use force=true to overwrite, "
                    "or dry_run=true to preview."
                )

    if not dry_run and force:
        async with get_db_ctx() as db:
            for table in CLEAR_TABLES:
                await db.execute(f"DELETE FROM {table}")
            await db.commit()

    result = await rebuild_from_disk(dry_run=dry_run)
    return result


# ── DB backup trigger ─────────────────────────────────────────────────────────


@router.post("/backup")
async def trigger_backup_endpoint():
    """Trigger an immediate database backup."""
    from nanio_orchestrator.backup import backup_database
    path = await backup_database()
    if path:
        return {"ok": True, "backup_path": path}
    return {"ok": False, "detail": "Backup failed"}


# ── Settings introspection ────────────────────────────────────────────────────

_SECRET_FIELDS = {"api_key", "secret", "s3_access_key", "s3_secret_key"}


def _mask(value, is_secret: bool) -> str | None:
    if not is_secret:
        return value
    if not value:
        return None
    s = str(value)
    return (s[:4] + "****") if len(s) > 4 else "****"


@router.get("/settings")
async def get_settings_endpoint():
    """Return all current settings with secrets masked."""
    from nanio_orchestrator.config import DEV_MODE
    s = get_settings()

    result = {}
    for field in s.model_fields:
        value = getattr(s, field)
        result[field] = _mask(value, field in _SECRET_FIELDS)

    # Replace db_backup_path with the effective (derived) value
    result["db_backup_path"] = s.effective_db_backup_path

    from nanio_orchestrator.cli import _get_config_path
    result["config_file"] = _get_config_path()
    result["dev_mode"] = DEV_MODE

    return result
