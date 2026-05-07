"""rclone-based migration engine with state machine and crash recovery.

State machine phases:
  pending → copying (convergence loop) → write_routing → verifying → switching → done
                                       ↗ (skips write_routing if counts converged in copying)
                                           → error
  Any phase can transition to 'error' or 'cancelled'.
  write_routing: nginx routes writes to dst pool; reads still come from src with 404-fallback.
  On done, orphaned_source_pool_id/prefix/at are recorded — the source data is NEVER deleted
  automatically; that is the exclusive responsibility of the human operator.

rclone is invoked as a subprocess for the copy/verify phases.
A temporary rclone config is generated from pool credentials.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from typing import Dict, Optional

from nanio_orchestrator.audit_log import log_audit
from nanio_orchestrator.config import get_settings
from nanio_orchestrator.credentials import get_pool_credentials, get_pool_s3_params
from nanio_orchestrator.db import get_db_ctx
from nanio_orchestrator.nginx.executor import reload_nginx, test_config
from nanio_orchestrator.nginx.generator import (
    generate_vhost_config,
    record_file_state,
    write_config_atomic,
)
from nanio_orchestrator.s3client import bucket_exists, bucket_has_objects, count_objects, create_bucket
from nanio_orchestrator.sidecar import (
    delete_migration_state,
    write_migration_completion,
    write_migration_state,
)

logger = logging.getLogger(__name__)

# Track running migration tasks: migration_id → asyncio.Task
_active_tasks: Dict[int, asyncio.Task] = {}
_migration_lock = asyncio.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cleanup_stale_rclone_dirs() -> None:
    """Remove leftover /tmp/nanio-rclone-* dirs from prior SIGKILL or crash.

    Called at startup before any migrations run. These dirs contain plaintext
    S3 credentials that must not persist on disk after the process exits.
    """
    import glob

    for d in glob.glob(os.path.join(tempfile.gettempdir(), "nanio-rclone-*")):
        try:
            shutil.rmtree(d)
            logger.info("Cleaned up stale rclone config dir: %s", d)
        except OSError as e:
            logger.warning("Failed to clean up %s: %s", d, e)


# ── rclone config generation ─────────────────────────────────────────────────


async def _build_rclone_config(src_pool_id: int, dst_pool_id: int) -> str:
    """Generate a temporary rclone config with [src] and [dst] remotes.

    The endpoint is ALWAYS derived from the first enabled pool member address,
    bypassing any proxy or endpoint_url override stored in credentials.
    rclone must talk directly to the S3 backend nodes to avoid routing ambiguity
    (the proxy may redirect based on the current nginx state) and to support all
    S3 operations required by rclone check (multipart checksums, etc.).
    """
    src_creds = await get_pool_credentials(src_pool_id)
    dst_creds = await get_pool_credentials(dst_pool_id)

    s = get_settings()
    lines = []

    for name, creds, pool_id in [("src", src_creds, src_pool_id), ("dst", dst_creds, dst_pool_id)]:
        # Always use the direct member address — never go through the proxy
        addr = await _first_member_endpoint(pool_id)

        lines.append(f"[{name}]")
        lines.append("type = s3")
        lines.append("provider = Other")
        lines.append("env_auth = false")

        if creds:
            lines.append(f"access_key_id = {creds['access_key']}")
            lines.append(f"secret_access_key = {creds['secret_key']}")
            lines.append(f"region = {creds['region']}")
        else:
            # Fallback to global credentials
            if s.s3_access_key:
                lines.append(f"access_key_id = {s.s3_access_key}")
            if s.s3_secret_key:
                lines.append(f"secret_access_key = {s.s3_secret_key}")

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

# Maximum log rows per migration — oldest entries are pruned when exceeded
_MAX_LOG_ROWS_PER_MIGRATION = 10000


async def _log(migration_id: int, phase: str, message: str) -> None:
    """Write a migration_log entry, pruning oldest if cap is exceeded."""
    async with get_db_ctx() as db:
        await db.execute(
            "INSERT INTO migration_log (migration_id, phase, message) VALUES (?, ?, ?)",
            (migration_id, phase, message),
        )
        # Prune oldest entries if cap is exceeded
        await db.execute(
            """DELETE FROM migration_log WHERE id IN (
                SELECT id FROM migration_log
                WHERE migration_id = ?
                ORDER BY id ASC
                LIMIT MAX(0, (SELECT COUNT(*) FROM migration_log WHERE migration_id = ?) - ?)
            )""",
            (migration_id, migration_id, _MAX_LOG_ROWS_PER_MIGRATION),
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
            await db.execute("UPDATE migrations SET phase=? WHERE id=?", (phase, migration_id))
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
        "mode": m.get("mode", "copy"),
        "route_id": m.get("route_id"),
        "status": m["phase"],
        "copied_objects": m["objects_done"],
        "total_objects": m["objects_total"],
        "bytes_transferred": m["bytes_done"],
        "bytes_total": m["bytes_total"],
        "started_at": m.get("started_at"),
        "finished_at": m.get("finished_at"),
        "nginx_state": (
            "source"
            if m["phase"] in ("pending", "copying")
            else "split"
            if m["phase"] in ("write_routing", "verifying")
            else "target"
        ),
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
        "--config",
        config_path,
        "--checkers",
        str(s.migration_checkers),
        "--transfers",
        str(s.migration_transfers),
        "--stats",
        "5s",
        "--stats-one-line",
        "--log-level",
        "NOTICE",
    ]

    if s.migration_bandwidth_limit and not check_only:
        cmd += ["--bwlimit", s.migration_bandwidth_limit]

    await _log(migration_id, phase, f"Running: {' '.join(cmd)}")
    logger.info("migration %d [%s]: starting rclone %s → %s", migration_id, phase, src_remote, dst_remote)

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
        # Buffer log lines and flush in batches to avoid per-line DB transactions
        log_buffer: list = []
        _LOG_BATCH_SIZE = 50

        async def _flush_log_buffer():
            if not log_buffer:
                return
            async with get_db_ctx() as db:
                await db.executemany(
                    "INSERT INTO migration_log (migration_id, phase, message) VALUES (?, ?, ?)",
                    log_buffer,
                )
                await db.commit()
            log_buffer.clear()

        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if not line:
                continue
            logger.debug("rclone [%d]: %s", migration_id, line)
            log_buffer.append((migration_id, phase, line))
            if len(log_buffer) >= _LOG_BATCH_SIZE:
                await _flush_log_buffer()
            # Parse progress from rclone stats lines and update DB
            progress = _parse_rclone_stats(line)
            if progress:
                await _update_progress(migration_id, **progress)

        # Flush remaining buffered log lines
        await _flush_log_buffer()

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


_DIFF_COUNT_RE = re.compile(r"(\d+)\s+differences?\s+found", re.IGNORECASE)


async def _run_rclone_check(
    migration_id: int,
    config_path: str,
    src_remote: str,
    dst_remote: str,
) -> tuple[bool, int]:
    """Run rclone check. Returns (ok, diff_count).

    diff_count is the number of differences reported by rclone check, parsed from
    the NOTICE output line "N differences found".  Returns 0 if check passes or if
    the count cannot be parsed.
    """
    s = get_settings()
    cmd = [
        s.rclone_path,
        "check",
        src_remote,
        dst_remote,
        "--config",
        config_path,
        "--checkers",
        str(s.migration_checkers),
        "--transfers",
        str(s.migration_transfers),
        "--stats",
        "5s",
        "--stats-one-line",
        "--log-level",
        "NOTICE",
    ]

    await _log(migration_id, "verifying", f"Running: {' '.join(cmd)}")
    logger.info("migration %d [verifying]: starting rclone check %s → %s", migration_id, src_remote, dst_remote)

    diff_count = 0
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        async with get_db_ctx() as db:
            await db.execute("UPDATE migrations SET rclone_pid=? WHERE id=?", (proc.pid, migration_id))
            await db.commit()

        log_buffer: list = []

        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if not line:
                continue
            logger.debug("rclone check [%d]: %s", migration_id, line)
            log_buffer.append((migration_id, "verifying", line))
            m = _DIFF_COUNT_RE.search(line)
            if m:
                diff_count = int(m.group(1))

        if log_buffer:
            async with get_db_ctx() as db:
                await db.executemany(
                    "INSERT INTO migration_log (migration_id, phase, message) VALUES (?, ?, ?)",
                    log_buffer,
                )
                await db.commit()

        await proc.wait()

        async with get_db_ctx() as db:
            await db.execute("UPDATE migrations SET rclone_pid=NULL WHERE id=?", (migration_id,))
            await db.commit()

        if proc.returncode == 0:
            logger.info("migration %d [verifying]: rclone check passed", migration_id)
            await _log(migration_id, "verifying", "rclone check completed successfully")
            return True, 0
        else:
            logger.warning(
                "migration %d [verifying]: rclone check exit code %d, diffs=%d",
                migration_id,
                proc.returncode,
                diff_count,
            )
            await _log(
                migration_id, "verifying", f"rclone check exit code {proc.returncode} — {diff_count} difference(s)"
            )
            return False, diff_count

    except FileNotFoundError:
        await _log(migration_id, "verifying", f"rclone binary not found at: {s.rclone_path}")
        return False, 0
    except Exception as e:
        await _log(migration_id, "verifying", f"rclone check error: {e}")
        return False, 0


# Matches rclone stats lines such as:
#   2026/04/16 18:36:06 INFO  : Transferred:   1.234 GiB / 5.678 GiB, 22%, 54 MiB/s, ETA 1m30s
#   2026/04/16 18:36:06 INFO  : Checks:         1234 / 5678, 22%
_BYTES_RE = re.compile(r"Transferred:.*?([\d.]+)\s*(\w+)\s*/\s*([\d.]+)\s*(\w+),\s*(\d+)%")
_CHECKS_RE = re.compile(r"Checks:\s+(\d+)\s*/\s*(\d+)")

_UNIT_BYTES = {
    "B": 1,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": 1024**3,
    "TiB": 1024**4,
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
}


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


async def _restore_nginx_config(migration_id: int, phase: str, filepath: str) -> None:
    """Restore a vhost nginx config from the DB content_snapshot after a failed reload.

    Called when reload_nginx() fails after a file rename so the broken config
    does not affect future nginx reloads.  Best-effort: logs a warning on failure.
    """
    try:
        async with get_db_ctx() as db:
            rows = await db.execute_fetchall("SELECT content_snapshot FROM config_files WHERE path = ?", (filepath,))
        snapshot = dict(rows[0]).get("content_snapshot") if rows else None
        if snapshot:
            await write_config_atomic(filepath, snapshot)
            await reload_nginx()
            logger.info(
                "migration %d [%s]: nginx config restored from snapshot (%s)",
                migration_id,
                phase,
                filepath,
            )
        else:
            await _log(
                migration_id,
                phase,
                f"WARNING: No config snapshot available to restore {filepath}. "
                "Run config rebuild to re-sync nginx configs.",
            )
    except Exception as exc:
        logger.warning(
            "migration %d [%s]: failed to restore nginx config from snapshot: %s",
            migration_id,
            phase,
            exc,
        )
        await _log(
            migration_id,
            phase,
            f"WARNING: Could not restore nginx config: {exc}. Run config rebuild to re-sync nginx configs.",
        )


# ── State machine: run a full migration ──────────────────────────────────────


async def run_migration(migration_id: int) -> None:
    """Execute the full migration state machine for a single migration."""
    config_dir = None
    s = get_settings()
    try:
        # Load migration record
        async with get_db_ctx() as db:
            rows = await db.execute_fetchall("SELECT * FROM migrations WHERE id = ?", (migration_id,))
        if not rows:
            logger.error("Migration %d not found", migration_id)
            return

        m = dict(rows[0])
        bucket = m["bucket"]
        src_pool_id = m["src_pool_id"]
        dst_pool_id = m["dst_pool_id"]
        mode = m.get("mode", "copy")
        route_id = m.get("route_id")  # may be None for legacy migrations

        # ── Safety: refuse if source and destination are the same pool ────
        if src_pool_id == dst_pool_id:
            msg = (
                f"Refusing to migrate: source and destination are the same pool "
                f"(pool_id={src_pool_id}). Source and destination must be different pools."
            )
            logger.error("Migration %d aborted: %s", migration_id, msg)
            await _log(migration_id, "error", msg)
            await _set_phase(migration_id, "error", msg)
            return

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
            ak, sk, _ = await get_pool_s3_params(src_pool_id)
            try:
                src_count = await count_objects(src_address, bucket, access_key=ak, secret_key=sk)
                if src_count == 0:
                    msg = (
                        f"Refusing to migrate: source bucket '{bucket}' is empty. "
                        "Copying from an empty source would erase any content already "
                        "present at the destination. Verify the source bucket and retry."
                    )
                    logger.error("Migration %d aborted: %s", migration_id, msg)
                    await _log(migration_id, "error", msg)
                    await _set_phase(migration_id, "error", msg)
                    return
            except (PermissionError, RuntimeError) as exc:
                logger.warning(
                    "Migration %d: pre-flight ListObjects failed (%s) — "
                    "skipping empty-source guard, rclone will verify independently.",
                    migration_id,
                    exc,
                )
                await _log(
                    migration_id,
                    "copying",
                    f"Pre-flight object count unavailable ({exc}). Proceeding — rclone uses its own pool credentials.",
                )

        # ── Pre-condition: destination bucket must not contain objects ───────
        # If bucket doesn't exist on dst → create it.
        # If it exists but has objects → refuse (would cause data conflicts).
        # If it exists and is empty → proceed.
        dst_address_precond = await _first_member_endpoint(dst_pool_id)
        if dst_address_precond:
            dst_ak_pre, dst_sk_pre, _ = await get_pool_s3_params(dst_pool_id)
            try:
                dst_bucket_found = await bucket_exists(
                    dst_address_precond,
                    bucket,
                    access_key=dst_ak_pre,
                    secret_key=dst_sk_pre,
                )
                if dst_bucket_found:
                    if await bucket_has_objects(
                        dst_address_precond,
                        bucket,
                        access_key=dst_ak_pre,
                        secret_key=dst_sk_pre,
                    ):
                        if mode == "sync":
                            # sync mode would overwrite/delete destination content — refuse
                            msg = (
                                "destination bucket already contains objects; "
                                "refusing sync-mode migration to avoid data loss at destination. "
                                "Use copy mode, or manually empty the destination bucket first."
                            )
                            logger.error("Migration %d aborted: %s", migration_id, msg)
                            await _log(migration_id, "error", msg)
                            await _set_phase(migration_id, "error", msg)
                            return
                        else:
                            # copy mode is additive — destination objects are safe
                            await _log(
                                migration_id,
                                "copying",
                                f"Destination bucket '{bucket}' already has objects — "
                                "copy mode will add missing objects, existing destination objects are preserved",
                            )
                    else:
                        await _log(
                            migration_id, "copying", f"Destination bucket '{bucket}' exists and is empty — proceeding"
                        )
                else:
                    dst_ok, dst_create_msg = await create_bucket(
                        dst_address_precond,
                        bucket,
                        access_key=dst_ak_pre,
                        secret_key=dst_sk_pre,
                    )
                    if not dst_ok:
                        msg = f"Failed to create destination bucket '{bucket}': {dst_create_msg}"
                        logger.error("Migration %d aborted: %s", migration_id, msg)
                        await _log(migration_id, "error", msg)
                        await _set_phase(migration_id, "error", msg)
                        return
                    await _log(migration_id, "copying", f"Created destination bucket '{bucket}' on pool {dst_pool_id}")
            except Exception as exc:
                logger.warning(
                    "Migration %d: destination bucket pre-check failed (%s) — proceeding",
                    migration_id,
                    exc,
                )
                await _log(migration_id, "copying", f"Destination bucket pre-check unavailable ({exc}) — proceeding")

        # ── Phase: copying (convergence loop) ───────────────────────────
        # Repeat rclone copy until src and dst object counts match, or until
        # the configured max passes is reached.  Each pass only transfers
        # new/changed objects, so later passes are fast.  The loop breaks
        # early when src count stabilises (no new files arriving).
        await _set_phase(migration_id, "copying")
        converged = False
        prev_src_count: int = -1
        for pass_num in range(1, s.migration_max_copy_passes + 1):
            pass_label = f"pass {pass_num}/{s.migration_max_copy_passes}"
            await _log(migration_id, "copying", f"Starting rclone {mode} {pass_label}: {src_remote} → {dst_remote}")
            ok = await _run_rclone(migration_id, config_path, src_remote, dst_remote, "copying", mode=mode)
            if not ok:
                await _set_phase(migration_id, "error", f"rclone {mode} failed ({pass_label})")
                return

            # Check convergence by comparing object counts on both sides
            try:
                src_ak, src_sk, _ = await get_pool_s3_params(src_pool_id)
                dst_ak, dst_sk, _ = await get_pool_s3_params(dst_pool_id)
                src_addr = await _first_member_endpoint(src_pool_id)
                dst_addr = await _first_member_endpoint(dst_pool_id)
                if src_addr and dst_addr:
                    src_count = await count_objects(src_addr, bucket, access_key=src_ak, secret_key=src_sk)
                    dst_count = await count_objects(dst_addr, bucket, access_key=dst_ak, secret_key=dst_sk)
                    await _log(
                        migration_id, "copying", f"{pass_label}: src={src_count} objects, dst={dst_count} objects"
                    )
                    if src_count == dst_count:
                        await _log(
                            migration_id, "copying", f"Converged after {pass_num} pass(es) — skipping write-routing"
                        )
                        converged = True
                        break
                    if src_count == prev_src_count:
                        await _log(
                            migration_id,
                            "copying",
                            f"Source count stable at {src_count} objects — entering write-routing",
                        )
                        break
                    prev_src_count = src_count
            except Exception as exc:
                await _log(migration_id, "copying", f"Object count unavailable ({exc}) — entering write-routing")
                break  # cannot measure convergence, proceed to write-routing

        if not converged:
            await _log(
                migration_id, "copying", "Source still receiving writes — entering write-routing to freeze source"
            )

        # ── Phase: write_routing ───────────────────────────────────────
        # nginx routes client writes (PUT/POST/DELETE) directly to the dst
        # pool.  Reads still come from source with a 404-fallback to dst.
        # The source is frozen for new objects from this point on.
        if not converged:
            await _set_phase(migration_id, "write_routing")
            await _log(
                migration_id, "write_routing", "nginx: writes → dst pool | reads → src pool (404-fallback to dst)"
            )
            vhost_id = m["vhost_id"]
            filepath, content_ng = await generate_vhost_config(vhost_id)
            tmp_path = filepath + ".tmp"
            await write_config_atomic(tmp_path, content_ng)
            wr_test = await test_config()
            if wr_test.ok:
                import os as _os

                _os.rename(tmp_path, filepath)
                reload_result = await reload_nginx()
                if reload_result.ok:
                    async with get_db_ctx() as db:
                        await record_file_state(db, filepath, content_ng)
                        await db.commit()
                    await _log(migration_id, "write_routing", "nginx reloaded with write-routing split config")
                else:
                    await _log(
                        migration_id,
                        "write_routing",
                        f"nginx reload failed — restoring previous config: {reload_result.output}",
                    )
                    await _restore_nginx_config(migration_id, "write_routing", filepath)
                    await _set_phase(
                        migration_id, "error", f"nginx reload failed during write_routing: {reload_result.output}"
                    )
                    return
            else:
                try:
                    import os as _os

                    _os.unlink(tmp_path)
                except OSError:
                    pass
                await _log(
                    migration_id, "write_routing", f"nginx test failed — write-routing not active: {wr_test.output}"
                )
                await _set_phase(migration_id, "error", f"nginx test failed during write_routing: {wr_test.output}")
                return

        # ── Phase: verifying ──────────────────────────────────────────
        # Source may still be receiving writes (not frozen — write-routing was skipped
        # when counts converged early).  We run copy → check in a loop until check
        # passes cleanly, or until diff_count stops shrinking (genuinely stuck).
        #
        # This handles the common case where an upload is in progress during
        # migration: rclone check would see a size mismatch on the in-flight file,
        # but a subsequent copy + check picks it up once the upload completes.
        await _set_phase(migration_id, "verifying")
        s_verify = get_settings()
        max_verify_passes = s_verify.migration_max_copy_passes
        prev_diff_count: Optional[int] = None

        for verify_pass in range(1, max_verify_passes + 1):
            await _log(migration_id, "verifying", f"Verify pass {verify_pass}/{max_verify_passes}: copy then check")

            ok = await _run_rclone(migration_id, config_path, src_remote, dst_remote, "verifying", mode="copy")
            if not ok:
                await _set_phase(migration_id, "error", f"Verify pass {verify_pass}: copy step failed")
                return

            await _log(migration_id, "verifying", f"Verify pass {verify_pass}: rclone check — verifying src == dst")
            check_ok, diff_count = await _run_rclone_check(migration_id, config_path, src_remote, dst_remote)
            if check_ok:
                await _log(migration_id, "verifying", f"Verify pass {verify_pass}: check passed — src == dst")
                break

            await _log(
                migration_id, "verifying", f"Verify pass {verify_pass}: {diff_count} difference(s) found — will retry"
            )

            if prev_diff_count is not None and diff_count >= prev_diff_count:
                msg = (
                    f"Verify pass {verify_pass}: diff count did not decrease "
                    f"({prev_diff_count} → {diff_count}) — source is diverging faster than "
                    "we can copy. Failing migration to avoid an infinite loop."
                )
                await _log(migration_id, "verifying", msg)
                await _set_phase(migration_id, "error", msg)
                return

            prev_diff_count = diff_count
        else:
            msg = (
                f"Verification did not converge after {max_verify_passes} pass(es). "
                "The source bucket may be receiving objects faster than rclone can copy them."
            )
            await _log(migration_id, "verifying", msg)
            await _set_phase(migration_id, "error", msg)
            return

        # ── Phase: switching ──────────────────────────────────────────────
        await _set_phase(migration_id, "switching")
        await _log(migration_id, "switching", "Updating nginx route to point to destination pool")

        # Generate the new nginx config BEFORE committing DB changes.
        # We need to temporarily update the route in DB to generate the correct
        # config, but we wrap everything in a transaction that only commits
        # after nginx successfully reloads.
        vhost_id = m["vhost_id"]

        # First, generate the target config by temporarily updating DB
        async with get_db_ctx() as db:
            if route_id:
                cur = await db.execute(
                    "UPDATE routes SET pool_id = ?, updated_at = datetime('now') WHERE id = ?",
                    (dst_pool_id, route_id),
                )
                rows_updated = cur.rowcount
            else:
                cur = await db.execute(
                    """UPDATE routes SET pool_id = ?, updated_at = datetime('now')
                       WHERE vhost_id = ? AND path_prefix = ?""",
                    (dst_pool_id, vhost_id, f"/{bucket}/"),
                )
                rows_updated = cur.rowcount

            if rows_updated == 0:
                # No route to update — cannot safely complete the migration.
                # The data has been fully copied to dst, but nginx cannot be
                # reconfigured.  Mark as error so the operator can investigate.
                msg = (
                    f"No nginx route found to update for bucket '{bucket}' "
                    f"(route_id={route_id}, vhost_id={vhost_id}). "
                    "Data was copied to the destination pool but nginx was NOT updated. "
                    "Create the route manually via the Buckets page and then "
                    "verify/clean up the source pool."
                )
                logger.error("Migration %d switching aborted: %s", migration_id, msg)
                await db.rollback()
                await _set_phase(migration_id, "error", msg)
                return

            await db.execute(
                """UPDATE bucket_sync SET status = 'routed', routed_pool_id = ?
                   WHERE vhost_id = ? AND bucket = ?""",
                (dst_pool_id, vhost_id, bucket),
            )
            await db.commit()

        # Regenerate nginx config (write_routing split is no longer needed)
        filepath, content = await generate_vhost_config(vhost_id)

        # Write .tmp, test, rename, reload — only then is the switch durable
        tmp_path = filepath + ".tmp"
        await write_config_atomic(tmp_path, content)
        test_result = await test_config()
        if test_result.ok:
            import os as _os

            _os.rename(tmp_path, filepath)
            reload_result = await reload_nginx()
            if reload_result.ok:
                async with get_db_ctx() as db:
                    await record_file_state(db, filepath, content)
                    await db.commit()
                await _log(migration_id, "switching", "nginx reloaded with new route")
            else:
                # Reload failed — rollback DB route to source pool, then regenerate
                # correct nginx config (pointing to src) so the broken file doesn't
                # affect future nginx reloads.
                await _log(
                    migration_id,
                    "switching",
                    f"nginx reload failed: {reload_result.output} — rolling back route and restoring config",
                )
                async with get_db_ctx() as db:
                    if route_id:
                        await db.execute(
                            "UPDATE routes SET pool_id = ?, updated_at = datetime('now') WHERE id = ?",
                            (src_pool_id, route_id),
                        )
                    else:
                        await db.execute(
                            """UPDATE routes SET pool_id = ?, updated_at = datetime('now')
                               WHERE vhost_id = ? AND path_prefix = ?""",
                            (src_pool_id, vhost_id, f"/{bucket}/"),
                        )
                    await db.execute(
                        """UPDATE bucket_sync SET status = 'migrating', routed_pool_id = NULL
                           WHERE vhost_id = ? AND bucket = ?""",
                        (vhost_id, bucket),
                    )
                    await db.commit()
                # After DB rollback, route points to src. Regenerate config and reload
                # so nginx on disk matches DB state. Best-effort: don't fail if this fails.
                try:
                    restored_fp, restored_content = await generate_vhost_config(vhost_id)
                    await write_config_atomic(restored_fp, restored_content)
                    restore_reload = await reload_nginx()
                    if restore_reload.ok:
                        async with get_db_ctx() as db:
                            await record_file_state(db, restored_fp, restored_content)
                            await db.commit()
                        await _log(
                            migration_id, "switching", "nginx config restored and reloaded (pointing to source pool)"
                        )
                    else:
                        await _log(
                            migration_id,
                            "switching",
                            f"WARNING: Config restoration reload also failed: {restore_reload.output}. "
                            "Run config rebuild to re-sync nginx configs.",
                        )
                except Exception as restore_exc:
                    await _log(
                        migration_id,
                        "switching",
                        f"WARNING: Could not restore nginx config: {restore_exc}. "
                        "Run config rebuild to re-sync nginx configs.",
                    )
                await _set_phase(migration_id, "error", f"nginx reload failed during switching: {reload_result.output}")
                return
        else:
            # nginx -t failed — rollback DB route to source pool, remove .tmp
            await _log(migration_id, "switching", f"nginx test failed: {test_result.output} — rolling back route")
            try:
                import os as _os

                _os.unlink(tmp_path)
            except OSError:
                pass
            async with get_db_ctx() as db:
                if route_id:
                    await db.execute(
                        "UPDATE routes SET pool_id = ?, updated_at = datetime('now') WHERE id = ?",
                        (src_pool_id, route_id),
                    )
                else:
                    await db.execute(
                        """UPDATE routes SET pool_id = ?, updated_at = datetime('now')
                           WHERE vhost_id = ? AND path_prefix = ?""",
                        (src_pool_id, vhost_id, f"/{bucket}/"),
                    )
                await db.execute(
                    """UPDATE bucket_sync SET status = 'migrating', routed_pool_id = NULL
                       WHERE vhost_id = ? AND bucket = ?""",
                    (vhost_id, bucket),
                )
                await db.commit()
            await _set_phase(migration_id, "error", f"nginx test failed during switching: {test_result.output}")
            return

        # ── Record orphaned source data ───────────────────────────────────
        # Source data is NEVER deleted automatically. Track it so operators can
        # make an informed decision about cleaning up the source bucket.
        orphaned_at = _now()
        async with get_db_ctx() as db:
            await db.execute(
                """UPDATE migrations SET
                   orphaned_source_pool_id = ?,
                   orphaned_source_prefix = ?,
                   orphaned_at = ?
                   WHERE id = ?""",
                (src_pool_id, f"/{bucket}/", orphaned_at, migration_id),
            )
            await db.commit()
            pool_rows = await db.execute_fetchall(
                "SELECT id, name FROM pools WHERE id IN (?, ?)",
                (src_pool_id, dst_pool_id),
            )

        pool_names = {r["id"]: r["name"] for r in pool_rows}
        write_migration_completion(
            {
                "migration_id": migration_id,
                "vhost_id": m["vhost_id"],
                "bucket": bucket,
                "source_pool_id": src_pool_id,
                "source_pool_name": pool_names.get(src_pool_id),
                "target_pool_id": dst_pool_id,
                "target_pool_name": pool_names.get(dst_pool_id),
                "mode": mode,
                "route_id": route_id,
                "status": "done",
                "orphaned_source_pool_id": src_pool_id,
                "orphaned_source_prefix": f"/{bucket}/",
                "orphaned_at": orphaned_at,
            }
        )

        # ── Phase: done ───────────────────────────────────────────────────
        await _set_phase(migration_id, "done")
        await _log(
            migration_id,
            "done",
            f"Migration completed: {bucket} moved to pool {dst_pool_id}. "
            f"Source data on pool {src_pool_id} is now orphaned — "
            "use GET /api/migrations/orphaned to track.",
        )
        async with get_db_ctx() as db:
            await log_audit(
                db,
                "migration_done",
                "migration",
                migration_id,
                after={
                    "bucket": bucket,
                    "src_pool_id": src_pool_id,
                    "dst_pool_id": dst_pool_id,
                    "vhost_id": m["vhost_id"],
                },
            )
            await db.commit()
        logger.info("Migration %d completed: bucket=%s", migration_id, bucket)

    except asyncio.CancelledError:
        await _set_phase(migration_id, "cancelled", "Migration was cancelled")
        await _log(migration_id, "cancelled", "Migration task cancelled")
        async with get_db_ctx() as db:
            await log_audit(db, "migration_cancelled", "migration", migration_id)
            await db.commit()
        raise
    except Exception as e:
        logger.error("Migration %d error: %s", migration_id, e, exc_info=True)
        await _set_phase(migration_id, "error", str(e)[:500])
        await _log(migration_id, "error", str(e))
        async with get_db_ctx() as db:
            await log_audit(db, "migration_error", "migration", migration_id, after={"error": str(e)[:500]})
            await db.commit()
    finally:
        _active_tasks.pop(migration_id, None)
        if config_dir:
            shutil.rmtree(config_dir, ignore_errors=True)


# ── Public API ────────────────────────────────────────────────────────────────


async def start_migration(
    vhost_id: int,
    bucket: str,
    src_pool_id: int,
    dst_pool_id: int,
    mode: str = "copy",
    route_id: Optional[int] = None,
) -> int:
    """Create a migration record and launch the background task. Returns migration id.

    Raises RuntimeError if the max parallel migration limit is reached.

    route_id: if set, the switching phase updates this specific route instead of
              looking up by '/{bucket}/' path_prefix. Required when the route
              path_prefix doesn't match /{bucket}/ (e.g. /photos/2025/).
    """
    s = get_settings()
    async with _migration_lock:
        if get_active_count() >= s.migration_max_parallel:
            raise RuntimeError(
                f"Max parallel migrations reached ({s.migration_max_parallel}). "
                "Wait for a running migration to finish or cancel one."
            )

        async with get_db_ctx() as db:
            cursor = await db.execute(
                """INSERT INTO migrations
                   (vhost_id, bucket, src_pool_id, dst_pool_id, phase, mode, route_id)
                   VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
                (vhost_id, bucket, src_pool_id, dst_pool_id, mode, route_id),
            )
            migration_id = cursor.lastrowid
            await log_audit(
                db,
                "start_migration",
                "migration",
                migration_id,
                after={
                    "bucket": bucket,
                    "src_pool_id": src_pool_id,
                    "dst_pool_id": dst_pool_id,
                    "mode": mode,
                    "vhost_id": vhost_id,
                    "route_id": route_id,
                },
            )
            await db.commit()

        task = asyncio.create_task(run_migration(migration_id))
        _active_tasks[migration_id] = task
        return migration_id


async def cancel_migration(migration_id: int) -> bool:
    """Cancel a running migration. Returns True if cancellation was attempted."""
    # Kill rclone process if running — SIGTERM first, SIGKILL after 10s
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT rclone_pid FROM migrations WHERE id = ?", (migration_id,))
        if rows:
            pid = dict(rows[0]).get("rclone_pid")
            if pid:
                try:
                    os.kill(pid, 15)  # SIGTERM
                    # Wait up to 10s for process to exit, then SIGKILL
                    for _ in range(20):
                        await asyncio.sleep(0.5)
                        try:
                            os.kill(pid, 0)  # check if still alive
                        except OSError:
                            break  # process exited
                    else:
                        # Still alive after 10s — escalate to SIGKILL
                        try:
                            os.kill(pid, 9)  # SIGKILL
                            logger.warning("Migration %d: rclone pid %d required SIGKILL", migration_id, pid)
                        except OSError:
                            pass
                except OSError:
                    pass

    # Cancel the asyncio task
    task = _active_tasks.get(migration_id)
    if task and not task.done():
        task.cancel()

    await _set_phase(migration_id, "cancelled", "Cancelled by operator")
    await _log(migration_id, "cancelled", "Migration cancelled by operator")
    return True


async def recover_interrupted_migrations() -> int:
    """On startup, find migrations stuck mid-flight and recover them.

    Returns the number of migrations recovered.
    """
    cleanup_stale_rclone_dirs()

    async with get_db_ctx() as db:
        active_rows = await db.execute_fetchall(
            "SELECT id FROM migrations WHERE phase IN ('pending', 'copying', 'verifying')"
        )
        # Migrations stuck in write_routing or switching: nginx and DB state are
        # uncertain — require operator review rather than auto-resuming.
        stuck_rows = await db.execute_fetchall(
            "SELECT id, phase FROM migrations WHERE phase IN ('write_routing', 'switching')"
        )

    count = 0

    for row in stuck_rows:
        mid = row["id"]
        phase = row["phase"]
        await _log(
            mid,
            "recovery",
            f"Found migration stuck in '{phase}' phase (process restart). "
            "Marking as error — operator review required. "
            "Verify nginx config and route state, then restart or cancel.",
        )
        await _set_phase(
            mid,
            "error",
            f"Interrupted during {phase} phase (process restart). "
            "Operator review required: check nginx config and routes.",
        )
        logger.warning("Migration %d: stuck in %s → error (operator review required)", mid, phase)
        count += 1

    # Restart migrations interrupted during copy/verify
    for row in active_rows:
        mid = row["id"]
        if mid not in _active_tasks:
            await _log(mid, "recovery", "Restarting interrupted migration after crash/restart")
            # Force mode to 'copy' on recovery: rclone sync could delete
            # destination objects that were added between the crash and restart
            # but weren't yet in the source.
            async with get_db_ctx() as db:
                await db.execute(
                    "UPDATE migrations SET mode = 'copy' WHERE id = ? AND mode = 'sync'",
                    (mid,),
                )
                await db.commit()
            await _set_phase(mid, "pending")
            task = asyncio.create_task(run_migration(mid))
            _active_tasks[mid] = task
            count += 1
            logger.info("Recovered migration %d (forced copy mode)", mid)

    if count:
        logger.info("Recovered %d interrupted migration(s)", count)
    return count


def get_active_count() -> int:
    """Return number of currently active migration tasks."""
    return sum(1 for t in _active_tasks.values() if not t.done())
