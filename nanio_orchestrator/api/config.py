"""Config operations API — status, validate, rebuild, sync, preview."""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiofiles
from fastapi import APIRouter, Body, HTTPException

from nanio_orchestrator.audit_log import log_audit
from nanio_orchestrator.config import get_settings
from nanio_orchestrator.db import get_db_ctx
from nanio_orchestrator.models import ConfigFileStatus, ConfigStatus, NginxResult
from nanio_orchestrator.nginx import applied
from nanio_orchestrator.nginx.apply import (
    apply_config_file,
    rebuild_all_configs,
    reload_nginx_locked,
    remove_config_file_checked,
)
from nanio_orchestrator.nginx.executor import test_config
from nanio_orchestrator.nginx.generator import (
    generate_pool_config,
    generate_vhost_config,
    record_file_state,
    sha256_str,
)

router = APIRouter(prefix="/api/config", tags=["config"])

_restart_tasks: set = set()


async def _sha256_file(filepath: str) -> str | None:
    """Read a file and return its SHA256 hash, or None if file doesn't exist."""
    try:
        async with aiofiles.open(filepath) as f:
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
        files: list[ConfigFileStatus] = []
        for r in rows:
            disk_hash = await _sha256_file(r["path"])
            drifted = disk_hash is not None and r["sha256_db"] is not None and disk_hash != r["sha256_db"]
            files.append(
                ConfigFileStatus(
                    path=r["path"],
                    sha256_disk=disk_hash,
                    sha256_db=r["sha256_db"],
                    drifted=drifted,
                    last_synced_at=r["last_synced_at"],
                )
            )

        # Last reload from audit_log
        reload_rows = await db.execute_fetchall(
            """SELECT nginx_reload_ok, created_at FROM audit_log
               WHERE nginx_reload_ok IS NOT NULL
               ORDER BY id DESC LIMIT 1"""
        )
    last_reload_ok = bool(reload_rows[0]["nginx_reload_ok"]) if reload_rows else None
    last_reload_at = reload_rows[0]["created_at"] if reload_rows else None

    return ConfigStatus(
        files=files,
        last_reload_ok=last_reload_ok,
        last_reload_at=last_reload_at,
        # compared with what nginx loaded at the last reload done through the orchestrator
        **await asyncio.to_thread(applied.pending_changes),
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
    result = await reload_nginx_locked()
    async with get_db_ctx() as db:
        await log_audit(db, "manual_reload", "config", None, reload_ok=result.ok, reload_output=result.output)
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
    from nanio_orchestrator.rebuild import import_configs

    warnings: list = []
    async with get_db_ctx() as db:
        counts = await import_configs(db, warnings)
        await db.commit()

    return {
        "ok": True,
        "pools_new": counts["pools_new"],
        "pools_updated": counts["pools_updated"],
        "members_new": counts["members_new"],
        "vhosts_new": counts["vhosts_new"],
        "vhosts_updated": counts["vhosts_updated"],
        "routes_synced": counts["routes"],
        "files_synced": counts["files"],
        "warnings": warnings,
    }


# ── Rebuild (DB → disk → reload) ─────────────────────────────────────────────


@router.post("/rebuild")
async def rebuild_all():
    """Rebuild all config files and sidecars from DB, validate, and reload."""
    try:
        result = await rebuild_all_configs()
    except OSError as e:
        return {"ok": False, "output": str(e), "written": [], "removed": [], "errors": [str(e)]}

    if not (result["stage"] == "test" and not result["ok"]):
        async with get_db_ctx() as db:
            await log_audit(db, "rebuild", "config", None, reload_ok=result["ok"], reload_output=result["output"])
            await db.commit()

    return {
        "ok": result["ok"],
        "output": result["output"],
        "written": result["written"],
        "removed": result["removed"],
        "errors": [],
    }


# ── Per-file drift resolution ────────────────────────────────────────────────


def _require_managed_config_path(path: str) -> str:
    """Resolve `path` and require it to be a .conf file directly inside the managed pools/ or
    vhosts/ directory. The endpoint reads the file and stores its content in the database, so
    anything else would let a caller copy arbitrary files readable by the service."""
    s = get_settings()
    try:
        resolved = Path(path).resolve()
    except (OSError, ValueError):
        raise HTTPException(400, "Invalid path") from None
    allowed = {s.pools_dir.resolve(), s.vhosts_dir.resolve()}
    if resolved.suffix != ".conf" or resolved.parent not in allowed:
        raise HTTPException(400, "Not a managed nginx config file")
    return str(resolved)


@router.get("/file")
async def read_config_file(path: str):
    """A managed file as it is on disk next to the version the orchestrator wrote, so the
    two can be compared before a drifted file is absorbed or rewritten."""
    resolved = _require_managed_config_path(path)
    try:
        async with aiofiles.open(resolved) as f:
            content: str | None = await f.read()
    except FileNotFoundError:
        content = None
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall(
            "SELECT content_snapshot FROM config_files WHERE path IN (?, ?)", (path, resolved)
        )
    recorded = rows[0]["content_snapshot"] if rows else None
    if content is None and recorded is None:
        raise HTTPException(404, f"No such managed file: {path}")
    return {
        "path": resolved,
        "content": content,
        "recorded": recorded,
        "drifted": content is not None and recorded is not None and content != recorded,
    }


@router.post("/absorb-file")
async def absorb_file(path: str = Body(..., embed=True)):
    """Accept a drifted file: read current disk state into the DB (sha256_db = sha256_disk)."""
    path = _require_managed_config_path(path)  # recorded under its real path, as the generator does
    try:
        async with aiofiles.open(path) as f:
            content = await f.read()
    except FileNotFoundError:
        raise HTTPException(404, f"File not found on disk: {path}") from None

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
        await log_audit(db, "absorb_drift", "config", None, reload_output=f"Absorbed drift for {path}")
        await db.commit()
    return {"ok": True, "path": path, "sha256": h}


@router.post("/rewrite-file")
async def rewrite_file(path: str = Body(..., embed=True)):
    """Rewrite a single config file from DB state, validate, and reload."""

    s = get_settings()

    # Determine if this is a pool or vhost config by matching against DB entries
    async with get_db_ctx() as db:
        pools = await db.execute_fetchall("SELECT id, name FROM pools")
        vhosts = await db.execute_fetchall("SELECT id, server_name FROM vhosts")

    pool_match = next((p for p in pools if str(s.pools_dir / f"{p['name']}.conf") == path), None)
    vhost_match = next((v for v in vhosts if str(s.vhosts_dir / f"{v['server_name']}.conf") == path), None)

    if pool_match:
        filepath, content = await generate_pool_config(pool_match["id"])
        if content is None:
            # Pool is now empty — remove, unless a vhost still proxies to it
            removal = await remove_config_file_checked(filepath)
            if not removal.ok:
                return {"ok": False, "output": removal.output}
            async with get_db_ctx() as db:
                await db.execute("DELETE FROM config_files WHERE path = ?", (filepath,))
                await db.commit()
            return {"ok": True, "action": "removed", "path": filepath, "reason": "Pool has no members"}
    elif vhost_match:
        filepath, content = await generate_vhost_config(vhost_match["id"])
    else:
        raise HTTPException(404, f"No pool or vhost found matching path: {path}")

    # Write, nginx -t, reload — the previous file is restored if either step fails
    result = await apply_config_file(filepath, content, reload=True)
    if result.stage == "test" and not result.ok:
        return {"ok": False, "output": result.output}

    async with get_db_ctx() as db:
        if result.ok:
            await record_file_state(db, filepath, content)
        await log_audit(db, "rewrite_file", "config", None, reload_ok=result.ok, reload_output=result.output)
        await db.commit()

    return {"ok": result.ok, "output": result.output, "path": filepath}


# ── Preview ───────────────────────────────────────────────────────────────────


@router.get("/preview/pool/{pool_id}")
async def preview_pool_config(pool_id: int):
    """Render upstream config without applying."""
    try:
        filepath, content = await generate_pool_config(pool_id)
    except ValueError:
        raise HTTPException(404, "Pool not found") from None
    return {"filepath": filepath, "content": content}


@router.get("/preview/vhost/{vhost_id}")
async def preview_vhost_config(vhost_id: int):
    """Render server block config without applying."""
    try:
        filepath, content = await generate_vhost_config(vhost_id)
    except ValueError:
        raise HTTPException(404, "Vhost not found") from None
    return {"filepath": filepath, "content": content}


# ── Rebuild from disk ─────────────────────────────────────────────────────────


@router.post("/rebuild-from-disk")
async def rebuild_from_disk_endpoint(dry_run: bool = False, force: bool = False):
    """Reconstruct the entire database from nginx configs + sidecar files.

    Query params:
      - dry_run: report what would be imported, write nothing
      - force: proceed even if DB has existing data (clears first)
    """
    from nanio_orchestrator.db import CLEAR_TABLES, get_db_ctx, init_db
    from nanio_orchestrator.rebuild import rebuild_from_disk

    if not dry_run:
        await init_db()

    if not dry_run and not force:
        async with get_db_ctx() as db:
            pools = await db.execute_fetchall("SELECT COUNT(*) as cnt FROM pools")
        if pools[0]["cnt"] > 0:
            raise HTTPException(
                409, "Database already contains data. Use force=true to overwrite, or dry_run=true to preview."
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

_SECRET_FIELDS = {"api_key", "secret"}


def _mask(value, is_secret: bool) -> str | None:
    if not is_secret:
        return value
    if not value:
        return None
    s = str(value)
    return (s[:4] + "****") if len(s) > 4 else "****"


# Settings that name the binary the service executes or the directories it writes to. Changing
# them is equivalent to shell access as the service user, so the API (reachable with just the
# panel key) does not accept them; the CLI on the host does.
_HOST_ONLY_SETTINGS = frozenset({"rclone_path", "db_path", "db_backup_path", "nginx_config_dir"})


@router.get("/settings")
async def get_settings_endpoint():
    """Return all current settings with secrets masked."""
    from nanio_orchestrator.config import DEV_MODE

    s = get_settings()

    result = {}
    for field in type(s).model_fields:
        value = getattr(s, field)
        result[field] = _mask(value, field in _SECRET_FIELDS)

    # Replace db_backup_path with the effective (derived) value
    result["db_backup_path"] = s.effective_db_backup_path

    from nanio_orchestrator.cli import _get_config_path

    result["config_file"] = _get_config_path()
    result["dev_mode"] = DEV_MODE
    result["host_only"] = sorted(_HOST_ONLY_SETTINGS)

    return result


@router.put("/settings/{key}")
async def update_setting(key: str, body: dict = Body(...)):
    """Update a single setting in the active config file.

    The key may be the short name (e.g. ``log_level``) or the full env-var
    name (``NANIO_ORCHESTRATOR_LOG_LEVEL``).  Changes are persisted to the
    config file immediately but only take effect after a service restart.
    """
    from nanio_orchestrator.cli import _SETTINGS_META, _get_config_path, _set_config_value

    normalized = key.lower().removeprefix("nanio_orchestrator_")
    if normalized not in _SETTINGS_META:
        raise HTTPException(status_code=404, detail=f"Unknown setting: '{key}'")

    if normalized in _HOST_ONLY_SETTINGS:
        raise HTTPException(
            status_code=403,
            detail=f"'{normalized}' decides which program or files the service uses and can only be "
            f"changed on the host: nanio-orchestrator config set {normalized} <value>",
        )

    if "value" not in body:
        raise HTTPException(status_code=422, detail="Missing 'value' field")

    value = str(body["value"])
    # One setting per line in the config file: a line break would smuggle in another setting.
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise HTTPException(status_code=422, detail="Value must not contain control characters")

    config_path = _get_config_path()
    try:
        _set_config_value(normalized, value, config_path)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to write config: {exc}") from exc

    masked = _mask(value, normalized in _SECRET_FIELDS)
    return {"ok": True, "key": normalized, "value": masked, "restart_required": True}


@router.post("/settings/restart")
async def restart_service():
    """Schedule a service restart so pending settings changes take effect.

    Returns 202 immediately; the actual ``sudo systemctl restart`` runs 1.5 s
    later so the HTTP response has time to reach the client before the process
    is replaced.

    Requires the sudoers drop-in installed by ``nanio-orchestrator install``
    to include the restart rule.  Not available in dev mode.
    """
    import shutil
    import subprocess

    from nanio_orchestrator.config import DEV_MODE

    if DEV_MODE:
        raise HTTPException(
            status_code=503,
            detail="Service restart is not available in dev mode. Restart manually.",
        )

    systemctl = shutil.which("systemctl") or "/usr/bin/systemctl"

    async def _do_restart() -> None:
        await asyncio.sleep(1.5)
        subprocess.Popen(
            ["sudo", systemctl, "restart", "nanio-orchestrator"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )

    # keep a reference: a task nobody holds can be garbage-collected before it runs
    _restart_tasks.add(task := asyncio.create_task(_do_restart()))
    task.add_done_callback(_restart_tasks.discard)
    return {"ok": True, "message": "Service restart scheduled"}
