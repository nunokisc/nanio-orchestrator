"""rclone-based migration engine with state machine and crash recovery.

State machine phases:
  pending → copying → verifying → switching → done
                                           → error
  Any phase can transition to 'error' or 'cancelled'.

rclone is invoked as a subprocess for the copy/verify phases.
A temporary rclone config is generated from pool credentials.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from nanio_orchestrator.config import get_settings
from nanio_orchestrator.credentials import get_pool_credentials
from nanio_orchestrator.db import get_db_ctx
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
        else:
            # Delete state file for terminal states
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
) -> bool:
    """Run rclone copy or check. Returns True on success."""
    s = get_settings()
    cmd = [s.rclone_path]

    if check_only:
        cmd += ["check", src_remote, dst_remote]
    else:
        cmd += ["sync", src_remote, dst_remote]

    cmd += [
        "--config", config_path,
        "--checkers", str(s.migration_checkers),
        "--transfers", str(s.migration_transfers),
        "--stats", "5s",
        "--stats-one-line",
        "--log-level", "INFO",
        "-v",
    ]

    if s.migration_bandwidth_limit and not check_only:
        cmd += ["--bwlimit", s.migration_bandwidth_limit]

    await _log(migration_id, phase, f"Running: {' '.join(cmd)}")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Store PID for potential cancellation
        async with get_db_ctx() as db:
            await db.execute(
                "UPDATE migrations SET rclone_pid=? WHERE id=?",
                (proc.pid, migration_id),
            )
            await db.commit()

        stdout, stderr = await proc.communicate()

        # Clear PID
        async with get_db_ctx() as db:
            await db.execute(
                "UPDATE migrations SET rclone_pid=NULL WHERE id=?",
                (migration_id,),
            )
            await db.commit()

        output = (stdout.decode(errors="replace") + "\n" + stderr.decode(errors="replace")).strip()

        if proc.returncode == 0:
            await _log(migration_id, phase, f"rclone completed successfully")
            return True
        else:
            await _log(migration_id, phase, f"rclone exit code {proc.returncode}: {output[-500:]}")
            return False

    except FileNotFoundError:
        await _log(migration_id, phase, f"rclone binary not found at: {s.rclone_path}")
        return False
    except Exception as e:
        await _log(migration_id, phase, f"rclone error: {e}")
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

        # ── Phase: copying ────────────────────────────────────────────────
        await _set_phase(migration_id, "copying")
        await _log(migration_id, "copying", f"Starting rclone sync: {src_remote} → {dst_remote}")

        ok = await _run_rclone(migration_id, config_path, src_remote, dst_remote, "copying")
        if not ok:
            await _set_phase(migration_id, "error", "rclone sync failed")
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
    vhost_id: int, bucket: str, src_pool_id: int, dst_pool_id: int
) -> int:
    """Create a migration record and launch the background task. Returns migration id."""
    async with get_db_ctx() as db:
        cursor = await db.execute(
            """INSERT INTO migrations
               (vhost_id, bucket, src_pool_id, dst_pool_id, phase)
               VALUES (?, ?, ?, ?, 'pending')""",
            (vhost_id, bucket, src_pool_id, dst_pool_id),
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
    """On startup, find migrations stuck in copying/verifying and restart them.

    Returns the number of migrations recovered.
    """
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall(
            "SELECT id FROM migrations WHERE phase IN ('pending', 'copying', 'verifying')"
        )

    count = 0
    for row in rows:
        mid = row["id"]
        if mid not in _active_tasks:
            await _log(mid, "recovery", "Restarting interrupted migration after crash/restart")
            # Reset phase to pending so the state machine runs from the beginning
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
