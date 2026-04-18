"""rclone-based migration engine with state machine and crash recovery.

State machine phases:
  pending → copying → verifying → switching → purge_source → done
                                           → error
  Any phase can transition to 'error' or 'cancelled'.
  purge_source deletes objects from the source bucket after a successful switch,
  eliminating orphan copies. Failure is non-fatal: migration is still marked done.

rclone is invoked as a subprocess for the copy/verify/purge phases.
A temporary rclone config is generated from pool credentials.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from nanio_orchestrator.config import get_settings
from nanio_orchestrator.credentials import get_pool_credentials
from nanio_orchestrator.db import get_db_ctx
from nanio_orchestrator.s3client import count_objects
from nanio_orchestrator.sidecar import write_migration_state, delete_migration_state

logger = logging.getLogger(__name__)

# Track running migration tasks: migration_id → asyncio.Task
_active_tasks: Dict[int, asyncio.Task] = {}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── rclone config generation ─────────────────────────────────────────────────


async def _build_rclone_config(src_pool_id: int, dst_pool_id: int) -> str:
    """Generate a temporary rclone config with [src] and [dst] remotes."""
    src_creds = await get_pool_credentials(src_pool_id)
    dst_creds = await get_pool_credentials(dst_pool_id)

    s = get_settings()
    lines = []

    for name, creds, pool_id in [("src", src_creds, src_pool_id), ("dst", dst_creds, dst_pool_id)]:
        lines.append(f"[{name}]")
        lines.append("type = s3")
        lines.append("provider = Other")
        lines.append("env_auth = false")

        if creds:
            lines.append(f"access_key_id = {creds['access_key']}")
            lines.append(f"secret_access_key = {creds['secret_key']}")
            lines.append(f"region = {creds['region']}")
            if creds.get("endpoint_url"):
                lines.append(f"endpoint = {creds['endpoint_url']}")
            else:
                # Derive endpoint from first enabled member
                addr = await _first_member_endpoint(pool_id)
                if addr:
                    lines.append(f"endpoint = http://{addr}")
        else:
            # Fallback to global credentials
            if s.s3_access_key:
                lines.append(f"access_key_id = {s.s3_access_key}")
            if s.s3_secret_key:
                lines.append(f"secret_access_key = {s.s3_secret_key}")
            addr = await _first_member_endpoint(pool_id)
            if addr:
                lines.append(f"endpoint = http://{addr}")

        lines.append("force_path_style = true")
        lines.append("")

    return "\n".join(lines)


async def _first_member_endpoint(pool_id: int) -> Optional[str]:
    """Get the address of the first enabled member of a pool."""
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall(
            "SELECT address FROM pool_members WHERE pool_id = ? AND enabled = 1 ORDER BY id LIMIT 1",
            (pool_id,),
        )
    if rows:
        return dict(rows[0])["address"]
    return None


# ── Log helpers ───────────────────────────────────────────────────────────────


async def _log(migration_id: int, phase: str, message: str) -> None:
    """Write a migration_log entry."""
    async with get_db_ctx() as db:
        await db.execute(
            "INSERT INTO migration_log (migration_id, phase, message) VALUES (?, ?, ?)",
            (migration_id, phase, message),
        )
        await db.commit()


async def _set_phase(migration_id: int, phase: str, error_msg: Optional[str] = None) -> None:
    """Update migration phase in DB and write state sidecar."""
    async with get_db_ctx() as db:
        if phase == "error":
            await db.execute(
                "UPDATE migrations SET phase=?, error_msg=?, finished_at=? WHERE id=?",
                (phase, error_msg, _now(), migration_id),
            )
        elif phase == "done":
            await db.execute(
                "UPDATE migrations SET phase=?, finished_at=? WHERE id=?",
                (phase, _now(), migration_id),
            )
        elif phase == "copying":
            await db.execute(
                "UPDATE migrations SET phase=?, started_at=? WHERE id=?",
                (phase, _now(), migration_id),
            )
        else:
            await db.execute(
                "UPDATE migrations SET phase=? WHERE id=?", (phase, migration_id)
            )
        await db.commit()

        # Write migration state sidecar (for rebuild recovery)
        if phase not in ("done", "cancelled"):
            await _write_state_sidecar(migration_id, db)

    # Delete outside the DB context: the commit is durable at this point.
    # A crash between commit and here leaves a stale state file, which rebuild
    # will re-import as pending — safe because the migration is terminal in the DB.
    if phase in ("done", "cancelled"):
        delete_migration_state(migration_id)


async def _write_state_sidecar(migration_id: int, db) -> None:
    """Write migration state to sidecar file from current DB state."""
    rows = await db.execute_fetchall(
        """SELECT m.*, sp.name as source_pool_name, dp.name as target_pool_name
           FROM migrations m
           LEFT JOIN pools sp ON m.src_pool_id = sp.id
           LEFT JOIN pools dp ON m.dst_pool_id = dp.id
           WHERE m.id = ?""",
        (migration_id,),
    )
    if not rows:
        return
    m = dict(rows[0])
    state = {
        "migration_id": m["id"],
        "vhost_id": m["vhost_id"],
        "bucket": m["bucket"],
        "source_pool_id": m["src_pool_id"],
        "source_pool_name": m.get("source_pool_name"),
        "target_pool_id": m["dst_pool_id"],
        "target_pool_name": m.get("target_pool_name"),
        "status": m["phase"],
        "copied_objects": m["objects_done"],
        "total_objects": m["objects_total"],
        "bytes_transferred": m["bytes_done"],
        "bytes_total": m["bytes_total"],
        "started_at": m.get("started_at"),
        "finished_at": m.get("finished_at"),
        "nginx_state": "source" if m["phase"] in ("pending", "copying", "verifying") else "target",
    }
    write_migration_state(state)


async def _update_progress(
    migration_id: int,
    objects_total: int = 0,
    objects_done: int = 0,
    bytes_total: int = 0,
    bytes_done: int = 0,
) -> None:
    async with get_db_ctx() as db:
        await db.execute(
            """UPDATE migrations SET
               objects_total=?, objects_done=?, bytes_total=?, bytes_done=?
               WHERE id=?""",
            (objects_total, objects_done, bytes_total, bytes_done, migration_id),
        )
        await db.commit()


# ── rclone execution ─────────────────────────────────────────────────────────


async def _run_rclone(
    migration_id: int,
    config_path: str,
    src_remote: str,
    dst_remote: str,
    phase: str,
    check_only: bool = False,
    mode: str = "copy",
) -> bool:
    """Run rclone copy or check. Returns True on success."""
    s = get_settings()
    cmd = [s.rclone_path]

    if check_only:
        cmd += ["check", src_remote, dst_remote]
    elif mode == "sync":
        cmd += ["sync", src_remote, dst_remote]
    else:
        cmd += ["copy", src_remote, dst_remote]

    cmd += [
        "--config", config_path,
        "--checkers", str(s.migration_checkers),
        "--transfers", str(s.migration_transfers),
        "--stats", "5s",
        "--stats-one-line",
        "--log-level", "INFO",
    ]

    if s.migration_bandwidth_limit and not check_only:
        cmd += ["--bwlimit", s.migration_bandwidth_limit]

    await _log(migration_id, phase, f"Running: {' '.join(cmd)}")
    logger.info("migration %d [%s]: starting rclone %s → %s",
                migration_id, phase, src_remote, dst_remote)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,  # merge stderr into stdout for unified log
        )

        async with get_db_ctx() as db:
            await db.execute(
                "UPDATE migrations SET rclone_pid=? WHERE id=?",
                (proc.pid, migration_id),
            )
            await db.commit()

        # Stream output line by line — gives live progress in migration_log
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if not line:
                continue
            logger.debug("rclone [%d]: %s", migration_id, line)
            await _log(migration_id, phase, line)
            # Parse progress from rclone stats lines and update DB
            progress = _parse_rclone_stats(line)
            if progress:
                await _update_progress(migration_id, **progress)

        await proc.wait()

        async with get_db_ctx() as db:
            await db.execute("UPDATE migrations SET rclone_pid=NULL WHERE id=?", (migration_id,))
            await db.commit()

        if proc.returncode == 0:
            logger.info("migration %d [%s]: rclone finished OK", migration_id, phase)
            await _log(migration_id, phase, "rclone completed successfully")
            return True
        else:
            logger.warning("migration %d [%s]: rclone exit code %d", migration_id, phase, proc.returncode)
            await _log(migration_id, phase, f"rclone exit code {proc.returncode}")
            return False

    except FileNotFoundError:
        await _log(migration_id, phase, f"rclone binary not found at: {s.rclone_path}")
        return False
    except Exception as e:
        await _log(migration_id, phase, f"rclone error: {e}")
        return False


# Matches rclone stats lines such as:
#   2026/04/16 18:36:06 INFO  : Transferred:   1.234 GiB / 5.678 GiB, 22%, 54 MiB/s, ETA 1m30s
#   2026/04/16 18:36:06 INFO  : Checks:         1234 / 5678, 22%
_BYTES_RE = re.compile(
    r"Transferred:.*?([\d.]+)\s*(\w+)\s*/\s*([\d.]+)\s*(\w+),\s*(\d+)%"
)
_CHECKS_RE = re.compile(r"Checks:\s+(\d+)\s*/\s*(\d+)")

_UNIT_BYTES = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4,
               "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4}


def _to_bytes(value: str, unit: str) -> int:
    return int(float(value) * _UNIT_BYTES.get(unit, 1))


def _parse_rclone_stats(line: str) -> Optional[dict]:
    """Extract progress fields from a rclone stats/log line."""
    result: dict = {}
    m = _BYTES_RE.search(line)
    if m:
        result["bytes_done"] = _to_bytes(m.group(1), m.group(2))
        result["bytes_total"] = _to_bytes(m.group(3), m.group(4))
    m2 = _CHECKS_RE.search(line)
    if m2:
        result["objects_done"] = int(m2.group(1))
        result["objects_total"] = int(m2.group(2))
    return result or None


async def _run_rclone_delete(migration_id: int, config_path: str, remote: str, phase: str) -> bool:
    """Run rclone delete to remove all objects from a remote bucket. Returns True on success."""
    s = get_settings()
    cmd = [
        s.rclone_path, "delete", remote,
        "--config", config_path,
        "--log-level", "INFO",
    ]
    await _log(migration_id, phase, f"Running: {' '.join(cmd)}")
    logger.info("migration %d [%s]: deleting objects from %s", migration_id, phase, remote)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        async with get_db_ctx() as db:
            await db.execute("UPDATE migrations SET rclone_pid=? WHERE id=?", (proc.pid, migration_id))
            await db.commit()

        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if line:
                logger.debug("rclone [%d]: %s", migration_id, line)
                await _log(migration_id, phase, line)

        await proc.wait()
        async with get_db_ctx() as db:
            await db.execute("UPDATE migrations SET rclone_pid=NULL WHERE id=?", (migration_id,))
            await db.commit()

        if proc.returncode == 0:
            await _log(migration_id, phase, "Source purge completed successfully")
            logger.info("migration %d [%s]: rclone delete finished OK", migration_id, phase)
            return True
        else:
            await _log(migration_id, phase, f"rclone delete exit code {proc.returncode}")
            return False

    except FileNotFoundError:
        await _log(migration_id, phase, f"rclone binary not found at: {s.rclone_path}")
        return False
    except Exception as e:
        await _log(migration_id, phase, f"rclone delete error: {e}")
        return False


# ── State machine: run a full migration ──────────────────────────────────────


async def run_migration(migration_id: int) -> None:
    """Execute the full migration state machine for a single migration."""
    config_dir = None
    try:
        # Load migration record
        async with get_db_ctx() as db:
            rows = await db.execute_fetchall(
                "SELECT * FROM migrations WHERE id = ?", (migration_id,)
            )
        if not rows:
            logger.error("Migration %d not found", migration_id)
            return

        m = dict(rows[0])
        bucket = m["bucket"]
        src_pool_id = m["src_pool_id"]
        dst_pool_id = m["dst_pool_id"]
        mode = m.get("mode", "copy")

        # Generate rclone config
        config_content = await _build_rclone_config(src_pool_id, dst_pool_id)
        config_dir = tempfile.mkdtemp(prefix="nanio-rclone-")
        config_path = os.path.join(config_dir, "rclone.conf")
        with open(config_path, "w") as f:
            f.write(config_content)
        # Restrict permissions on the config file
        os.chmod(config_path, 0o600)

        src_remote = f"src:{bucket}"
        dst_remote = f"dst:{bucket}"

        # ── Pre-flight: refuse to copy from an empty source ───────────────
        src_address = await _first_member_endpoint(src_pool_id)
        if src_address:
            src_creds = await get_pool_credentials(src_pool_id)
            ak = src_creds["access_key"] if src_creds else None
            sk = src_creds["secret_key"] if src_creds else None
            try:
                src_count = await count_objects(src_address, bucket, access_key=ak, secret_key=sk)
                if src_count == 0:
                    msg = (f"Refusing to migrate: source bucket '{bucket}' is empty. "
                           "Copying from an empty source would erase any content already "
                           "present at the destination. Verify the source bucket and retry.")
                    logger.error("Migration %d aborted: %s", migration_id, msg)
                    await _set_phase(migration_id, "error", msg)
                    return
            except (PermissionError, RuntimeError) as exc:
                logger.warning(
                    "Migration %d: pre-flight ListObjects failed (%s) — "
                    "skipping empty-source guard, rclone will verify independently.",
                    migration_id, exc,
                )
                await _log(
                    migration_id, "copying",
                    f"Pre-flight object count unavailable ({exc}). "
                    "Proceeding — rclone uses its own pool credentials.",
                )

        # ── Phase: copying ────────────────────────────────────────────────
        await _set_phase(migration_id, "copying")
        await _log(migration_id, "copying", f"Starting rclone {mode}: {src_remote} \u2192 {dst_remote}")

        ok = await _run_rclone(migration_id, config_path, src_remote, dst_remote, "copying", mode=mode)
        if not ok:
            await _set_phase(migration_id, "error", f"rclone {mode} failed")
            return

        # ── Phase: verifying ──────────────────────────────────────────────
        await _set_phase(migration_id, "verifying")
        await _log(migration_id, "verifying", "Running rclone check to verify integrity")

        ok = await _run_rclone(migration_id, config_path, src_remote, dst_remote, "verifying", check_only=True)
        if not ok:
            await _set_phase(migration_id, "error", "rclone check verification failed")
            return

        # ── Phase: switching ──────────────────────────────────────────────
        await _set_phase(migration_id, "switching")
        await _log(migration_id, "switching", "Updating nginx route to point to destination pool")

        async with get_db_ctx() as db:
            # Update the route to point to the destination pool
            vhost_id = m["vhost_id"]
            await db.execute(
                """UPDATE routes SET pool_id = ?, updated_at = datetime('now')
                   WHERE vhost_id = ? AND path_prefix = ?""",
                (dst_pool_id, vhost_id, f"/{bucket}/"),
            )
            # Update bucket_sync status
            await db.execute(
                """UPDATE bucket_sync SET status = 'routed', routed_pool_id = ?
                   WHERE vhost_id = ? AND bucket = ?""",
                (dst_pool_id, vhost_id, bucket),
            )
            await db.commit()

        # Regenerate and reload nginx config
        from nanio_orchestrator.nginx.generator import generate_vhost_config, write_config_atomic, record_file_state
        from nanio_orchestrator.nginx.executor import reload_nginx, test_config

        filepath, content = await generate_vhost_config(m["vhost_id"])

        test_result = await test_config()
        if test_result.ok:
            await write_config_atomic(filepath, content)
            await reload_nginx()
            async with get_db_ctx() as db:
                await record_file_state(db, filepath, content)
                await db.commit()
            await _log(migration_id, "switching", "nginx reloaded with new route")
        else:
            await _log(migration_id, "switching", f"nginx test failed: {test_result.output}")
            # Don't fail the migration — route is already updated in DB

        # ── Phase: purge_source ───────────────────────────────────────────
        await _set_phase(migration_id, "purge_source")
        await _log(migration_id, "purge_source",
                   "Purging source bucket content to eliminate orphan copies on default pool")

        purge_ok = await _run_rclone_delete(migration_id, config_path, src_remote, "purge_source")
        if not purge_ok:
            await _log(migration_id, "purge_source",
                       "WARNING: Source purge had errors — orphan content may remain on source pool. "
                       "Use the Orphan Scan on the Buckets page to clean up manually.")
            logger.warning("Migration %d: source purge had errors (non-fatal)", migration_id)

        # ── Phase: done ───────────────────────────────────────────────────
        await _set_phase(migration_id, "done")
        await _log(migration_id, "done", f"Migration completed: {bucket} moved to pool {dst_pool_id}")
        logger.info("Migration %d completed: bucket=%s", migration_id, bucket)

    except asyncio.CancelledError:
        await _set_phase(migration_id, "cancelled", "Migration was cancelled")
        await _log(migration_id, "cancelled", "Migration task cancelled")
        raise
    except Exception as e:
        logger.error("Migration %d error: %s", migration_id, e, exc_info=True)
        await _set_phase(migration_id, "error", str(e)[:500])
        await _log(migration_id, "error", str(e))
    finally:
        _active_tasks.pop(migration_id, None)
        if config_dir:
            shutil.rmtree(config_dir, ignore_errors=True)


# ── Public API ────────────────────────────────────────────────────────────────


async def start_migration(
    vhost_id: int, bucket: str, src_pool_id: int, dst_pool_id: int, mode: str = "copy"
) -> int:
    """Create a migration record and launch the background task. Returns migration id."""
    async with get_db_ctx() as db:
        cursor = await db.execute(
            """INSERT INTO migrations
               (vhost_id, bucket, src_pool_id, dst_pool_id, phase, mode)
               VALUES (?, ?, ?, ?, 'pending', ?)""",
            (vhost_id, bucket, src_pool_id, dst_pool_id, mode),
        )
        migration_id = cursor.lastrowid
        await db.commit()

    task = asyncio.create_task(run_migration(migration_id))
    _active_tasks[migration_id] = task
    return migration_id


async def cancel_migration(migration_id: int) -> bool:
    """Cancel a running migration. Returns True if cancellation was attempted."""
    task = _active_tasks.get(migration_id)
    if task and not task.done():
        task.cancel()

    # Also kill rclone process if running
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall(
            "SELECT rclone_pid FROM migrations WHERE id = ?", (migration_id,)
        )
        if rows:
            pid = dict(rows[0]).get("rclone_pid")
            if pid:
                try:
                    os.kill(pid, 15)  # SIGTERM
                except OSError:
                    pass

    await _set_phase(migration_id, "cancelled", "Cancelled by operator")
    await _log(migration_id, "cancelled", "Migration cancelled by operator")
    return True


async def recover_interrupted_migrations() -> int:
    """On startup, find migrations stuck mid-flight and recover them.

    Returns the number of migrations recovered.
    """
    async with get_db_ctx() as db:
        active_rows = await db.execute_fetchall(
            "SELECT id FROM migrations WHERE phase IN ('pending', 'copying', 'verifying')"
        )
        # purge_source crashed: data is safe on destination — just mark done
        purge_rows = await db.execute_fetchall(
            "SELECT id FROM migrations WHERE phase = 'purge_source'"
        )

    count = 0

    # Handle migrations stuck mid-purge: mark done, orphan scan can clean up
    for row in purge_rows:
        mid = row["id"]
        await _log(mid, "recovery",
                   "Found migration stuck in purge_source — marking done. "
                   "Use Orphan Scan on Buckets page to clean up residual source content.")
        await _set_phase(mid, "done")
        logger.info("Migration %d: purge_source → done (data safe on target)", mid)
        count += 1

    # Restart migrations interrupted during copy/verify
    for row in active_rows:
        mid = row["id"]
        if mid not in _active_tasks:
            await _log(mid, "recovery", "Restarting interrupted migration after crash/restart")
            await _set_phase(mid, "pending")
            task = asyncio.create_task(run_migration(mid))
            _active_tasks[mid] = task
            count += 1
            logger.info("Recovered migration %d", mid)

    if count:
        logger.info("Recovered %d interrupted migration(s)", count)
    return count


def get_active_count() -> int:
    """Return number of currently active migration tasks."""
    return sum(1 for t in _active_tasks.values() if not t.done())
