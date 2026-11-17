"""rclone-based migration engine with state machine and crash recovery.

State machine phases:
  pending → copying (bulk passes) → write_routing → verifying → switching → done
  Any phase can transition to 'error' or 'cancelled'.

Data-safety model
  copying        The source pool serves everything. rclone copies in passes until the object
                 counts match or the source stops growing.
  write_routing  nginx is switched to split routing and the destination becomes authoritative:
                 writes go to the destination only, reads try the destination and fall back to
                 the source on 404, DELETEs are mirrored to the source. From here on the source
                 receives no new data. The split is ALWAYS entered — switching straight from
                 "counts match" would lose any object written between the last check and the
                 switch.
  verifying      rclone copy --update (never overwrite a destination object that is newer than
                 the source — that is a client write made during the split) followed by a
                 one-way check. A differing object is accepted only when the destination copy
                 is newer than the source copy.
  switching      The route is pointed at the destination and the split is removed.

  The split is recorded in migrations.split_routing, not derived from the phase: if the migration
  fails or is cancelled after clients have written to the destination, the split stays in place
  (regenerating the vhost keeps it) until a new migration for the same bucket and pools finishes
  the job. Removing it would hide those objects behind a source-only route.

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
from typing import Dict, List, Optional

from nanio_orchestrator import s3client, subfolders
from nanio_orchestrator.audit_log import log_audit
from nanio_orchestrator.config import get_settings
from nanio_orchestrator.credentials import get_pool_credentials, get_pool_s3_params
from nanio_orchestrator.db import get_db_ctx
from nanio_orchestrator.nginx.apply import apply_config_file
from nanio_orchestrator.nginx.generator import SUBFOLDER_MARKER, generate_vhost_config, record_file_state
from nanio_orchestrator.s3client import (
    bucket_exists,
    bucket_has_objects,
    count_objects,
    create_bucket,
    object_last_modified,
)
from nanio_orchestrator.sidecar import (
    delete_migration_state,
    write_migration_completion,
    write_migration_state,
)
from nanio_orchestrator.subfolders import ensure_markers

logger = logging.getLogger(__name__)

# Track running migration tasks: migration_id → asyncio.Task
_active_tasks: Dict[int, asyncio.Task] = {}


# ── HTTP cascade helpers ──────────────────────────────────────────────────────


async def _cascade_http_write_routing(
    migration_id: int,
    bucket: str,
    src_pool_id: int,
    dst_pool_id: int,
    route_path: Optional[str] = None,
) -> None:
    """Apply write-routing split config to http vhosts backed by src nanio pool.

    For each http pool linked to src, finds vhosts with a route for /{bucket}/
    and regenerates them with split-routing to dst (if an http dst pool exists).
    """
    async with get_db_ctx() as db:
        http_src_rows = await db.execute_fetchall(
            "SELECT id, name FROM pools WHERE source_nanio_pool_id = ? AND type = 'http'",
            (src_pool_id,),
        )
        http_dst_rows = await db.execute_fetchall(
            "SELECT id, name FROM pools WHERE source_nanio_pool_id = ? AND type = 'http'",
            (dst_pool_id,),
        )

    http_src_pools = [dict(r) for r in http_src_rows]
    http_dst_pool = dict(http_dst_rows[0]) if http_dst_rows else None

    if not http_src_pools:
        return

    if not http_dst_pool:
        await _log(
            migration_id,
            "write_routing",
            f"WARNING: No http pool with source_nanio_pool_id={dst_pool_id} found. "
            f"HTTP vhosts backed by src pool will have no write-routing fallback. "
            f"Create an http pool with source_nanio_pool_id={dst_pool_id} for full coverage.",
        )

    for http_src_pool in http_src_pools:
        async with get_db_ctx() as db:
            route_rows = await db.execute_fetchall(
                """SELECT v.id as vhost_id, v.server_name
                   FROM vhosts v
                   JOIN routes r ON r.vhost_id = v.id
                   WHERE r.pool_id = ? AND r.path_prefix = ?""",
                (http_src_pool["id"], route_path or f"/{bucket}/"),
            )

        for vr in route_rows:
            vhost_id_h = vr["vhost_id"]
            server_name_h = vr["server_name"]

            if not http_dst_pool:
                await _log(
                    migration_id,
                    "write_routing",
                    f"WARNING: Skipping http vhost '{server_name_h}' — no http dst pool found. "
                    "Vhost will continue reading from source only (no write-routing).",
                )
                continue

            try:
                # The split is derived from migrations.split_routing by the generator
                filepath_h, content_h = await generate_vhost_config(vhost_id_h)
                result_h = await apply_config_file(filepath_h, content_h, reload=True)
                if result_h.ok:
                    async with get_db_ctx() as db:
                        await record_file_state(db, filepath_h, content_h)
                        await db.commit()
                    await _log(
                        migration_id,
                        "write_routing",
                        f"HTTP vhost '{server_name_h}': split-routing applied "
                        f"(writes → {http_dst_pool['name']}, reads → {http_src_pool['name']})",
                    )
                else:
                    await _log(
                        migration_id,
                        "write_routing",
                        f"WARNING: HTTP vhost '{server_name_h}': nginx {result_h.stage} failed — "
                        f"{result_h.output}. Vhost remains without split-routing.",
                    )
            except Exception as exc:
                await _log(
                    migration_id,
                    "write_routing",
                    f"WARNING: HTTP vhost '{server_name_h}': cascade write_routing failed — {exc}.",
                )


async def _cascade_http_switching(
    migration_id: int,
    bucket: str,
    src_pool_id: int,
    dst_pool_id: int,
    route_path: Optional[str] = None,
) -> list:
    """After primary switch, update http vhosts to point directly to http dst pool.

    Returns list of server_name strings for vhosts that were successfully swept.
    """
    async with get_db_ctx() as db:
        http_src_rows = await db.execute_fetchall(
            "SELECT id, name FROM pools WHERE source_nanio_pool_id = ? AND type = 'http'",
            (src_pool_id,),
        )
        http_dst_rows = await db.execute_fetchall(
            "SELECT id, name FROM pools WHERE source_nanio_pool_id = ? AND type = 'http'",
            (dst_pool_id,),
        )

    http_src_pools = [dict(r) for r in http_src_rows]
    http_dst_pool = dict(http_dst_rows[0]) if http_dst_rows else None
    swept: list = []

    if not http_src_pools:
        return swept

    if not http_dst_pool:
        await _log(
            migration_id,
            "switching",
            f"WARNING: No http pool with source_nanio_pool_id={dst_pool_id} found. "
            "HTTP vhosts backed by src pool NOT updated. "
            f"Create an http pool with source_nanio_pool_id={dst_pool_id} and manually update routes.",
        )
        return swept

    for http_src_pool in http_src_pools:
        async with get_db_ctx() as db:
            route_rows = await db.execute_fetchall(
                """SELECT r.id as route_id, v.id as vhost_id, v.server_name
                   FROM vhosts v
                   JOIN routes r ON r.vhost_id = v.id
                   WHERE r.pool_id = ? AND r.path_prefix = ?""",
                (http_src_pool["id"], route_path or f"/{bucket}/"),
            )

        for rr in route_rows:
            vhost_id_h = rr["vhost_id"]
            route_id_h = rr["route_id"]
            server_name_h = rr["server_name"]

            try:
                async with get_db_ctx() as db:
                    await db.execute(
                        "UPDATE routes SET pool_id = ?, updated_at = datetime('now') WHERE id = ?",
                        (http_dst_pool["id"], route_id_h),
                    )
                    await db.commit()

                filepath_h, content_h = await generate_vhost_config(vhost_id_h)
                result_h = await apply_config_file(filepath_h, content_h, reload=True)
                if result_h.ok:
                    async with get_db_ctx() as db:
                        await record_file_state(db, filepath_h, content_h)
                        await db.commit()
                    await _log(
                        migration_id,
                        "switching",
                        f"HTTP vhost '{server_name_h}': route updated to {http_dst_pool['name']}",
                    )
                    swept.append(server_name_h)
                else:
                    # Previous config file is back in place — roll the route back to src
                    async with get_db_ctx() as db:
                        await db.execute(
                            "UPDATE routes SET pool_id = ?, updated_at = datetime('now') WHERE id = ?",
                            (http_src_pool["id"], route_id_h),
                        )
                        await db.commit()
                    await _log(
                        migration_id,
                        "switching",
                        f"WARNING: HTTP vhost '{server_name_h}': nginx {result_h.stage} failed — "
                        "route rolled back. Primary migration is still done.",
                    )
            except Exception as exc:
                await _log(
                    migration_id,
                    "switching",
                    f"WARNING: HTTP vhost '{server_name_h}': cascade switching failed — {exc}. "
                    "Primary migration is still done.",
                )

    return swept


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
    if phase in ("error", "cancelled"):
        # Logged before the phase changes, so whoever sees the terminal phase also finds
        # the explanation in the log.
        async with get_db_ctx() as db:
            rows = await db.execute_fetchall("SELECT split_routing FROM migrations WHERE id = ?", (migration_id,))
        if rows and rows[0]["split_routing"]:
            await _log(
                migration_id,
                phase,
                "Split routing stays active: clients keep writing to the destination pool and reading "
                "from it with a fallback to the source, so no object becomes unreachable. Start a new "
                "migration for the same bucket and pools to finish the move.",
            )

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

        split_rows = await db.execute_fetchall("SELECT split_routing FROM migrations WHERE id = ?", (migration_id,))
        split_active = bool(split_rows and split_rows[0]["split_routing"])

        # Write migration state sidecar (for rebuild recovery). A cancelled migration normally
        # has nothing left to recover, unless its split routing is still serving traffic.
        keep_state = phase != "done" and (phase != "cancelled" or split_active)
        if keep_state:
            await _write_state_sidecar(migration_id, db)

    # Delete outside the DB context: the commit is durable at this point.
    # A crash between commit and here leaves a stale state file, which rebuild
    # will re-import as pending — safe because the migration is terminal in the DB.
    if not keep_state:
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
        "folder": m.get("folder") or "",
        "source_pool_id": m["src_pool_id"],
        "source_pool_name": m.get("source_pool_name"),
        "target_pool_id": m["dst_pool_id"],
        "target_pool_name": m.get("target_pool_name"),
        "route_id": m.get("route_id"),
        "status": m["phase"],
        "copied_objects": m["objects_done"],
        "total_objects": m["objects_total"],
        "bytes_transferred": m["bytes_done"],
        "bytes_total": m["bytes_total"],
        "started_at": m.get("started_at"),
        "finished_at": m.get("finished_at"),
        "split_routing": bool(m.get("split_routing")),
        "nginx_state": (
            "split"
            if m.get("split_routing") or m["phase"] in ("write_routing", "verifying")
            else "source"
            if m["phase"] in ("pending", "copying")
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
    update: bool = False,
) -> bool:
    """Run rclone copy or check. Returns True on success.

    update=True adds --update: a destination object newer than its source is left alone.
    Required as soon as split routing is active, because the destination then holds client
    writes that a plain copy would overwrite with the older source version.
    """
    s = get_settings()
    cmd = [s.rclone_path]

    if check_only:
        cmd += ["check", src_remote, dst_remote]
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

    if update and not check_only:
        cmd += ["--update"]

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
) -> tuple[bool, int, Optional[Dict[str, list]]]:
    """Run a one-way rclone check: every source object must exist, identical, at the destination.

    One-way because during split routing the destination legitimately holds objects the source
    never had (written by clients after the split).

    Returns (ok, diff_count, report). diff_count is parsed from the "N differences found" line
    (0 if the check passes or the count cannot be parsed). report maps "differ" / "missing" /
    "error" to the object keys rclone listed in each category, or is None when the report files
    could not be read.
    """
    s = get_settings()
    report_dir = os.path.dirname(config_path)
    report_paths = {name: os.path.join(report_dir, f"check-{name}.txt") for name in ("differ", "missing", "error")}
    for path in report_paths.values():
        try:
            os.unlink(path)
        except OSError:
            pass
    cmd = [
        s.rclone_path,
        "check",
        src_remote,
        dst_remote,
        "--one-way",
        "--differ",
        report_paths["differ"],
        "--missing-on-dst",
        report_paths["missing"],
        "--error",
        report_paths["error"],
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
            return True, 0, None
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
            return False, diff_count, _read_check_report(report_paths)

    except FileNotFoundError:
        await _log(migration_id, "verifying", f"rclone binary not found at: {s.rclone_path}")
        return False, 0, None
    except Exception as e:
        await _log(migration_id, "verifying", f"rclone check error: {e}")
        return False, 0, None


def _read_check_report(report_paths: Dict[str, str]) -> Optional[Dict[str, list]]:
    """Read the key lists written by rclone check. None if any of them is unreadable."""
    report: Dict[str, list] = {}
    try:
        for name, path in report_paths.items():
            with open(path, encoding="utf-8") as f:
                report[name] = [line.rstrip("\n") for line in f if line.strip()]
    except OSError:
        return None
    return report


async def _verify_destination(
    migration_id: int,
    config_path: str,
    src_remote: str,
    dst_remote: str,
    bucket: str,
    src_pool_id: int,
    dst_pool_id: int,
    folder: str = "",
) -> tuple[bool, int]:
    """Verify that the destination holds everything the source has. Returns (ok, unresolved).

    rclone reports keys relative to the remote, i.e. relative to ``folder`` when one is given.

    A source object missing from the destination, or unreadable, is always a failure. An object
    that differs is accepted only when the destination copy is strictly newer than the source
    copy: that is a client write made while split routing was active, and it must win. Anything
    else is counted in `unresolved`, which the caller uses to decide whether another pass helps.
    """
    check_ok, diff_count, report = await _run_rclone_check(migration_id, config_path, src_remote, dst_remote)
    if check_ok:
        return True, 0
    if report is None or report["missing"] or report["error"] or not report["differ"]:
        return False, diff_count

    src_addr = await _first_member_endpoint(src_pool_id)
    dst_addr = await _first_member_endpoint(dst_pool_id)
    if not src_addr or not dst_addr:
        return False, diff_count
    src_ak, src_sk, _ = await get_pool_s3_params(src_pool_id)
    dst_ak, dst_sk, _ = await get_pool_s3_params(dst_pool_id)

    stale = []
    for relative_key in report["differ"]:
        key = folder + relative_key
        try:
            src_time = await object_last_modified(src_addr, bucket, key, access_key=src_ak, secret_key=src_sk)
            dst_time = await object_last_modified(dst_addr, bucket, key, access_key=dst_ak, secret_key=dst_sk)
        except Exception as exc:
            await _log(migration_id, "verifying", f"Could not compare '{key}' on both pools: {exc}")
            stale.append(key)
            continue
        if src_time is None or dst_time is None or dst_time <= src_time:
            stale.append(key)

    newer = len(report["differ"]) - len(stale)
    if newer:
        await _log(
            migration_id,
            "verifying",
            f"{newer} object(s) differ because clients overwrote them on the destination during "
            "split routing — the destination version is newer and is kept",
        )
    if stale:
        sample = ", ".join(stale[:5])
        await _log(migration_id, "verifying", f"{len(stale)} object(s) at the destination are stale: {sample}")
        return False, len(stale)
    return True, 0


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


# ── State machine: run a full migration ──────────────────────────────────────


async def _finish_folder_move(vhost_id: int, route_id: Optional[int], route_path: str, dst_pool_id: int) -> List[str]:
    """After a folder switched pools: drop its route if it is back on the bucket's own pool.

    A folder route that points at the pool already serving the rest of the bucket does
    nothing, so it is removed together with its marker and its http routes. Returns log lines.
    """
    notes: List[str] = []
    changed: List[int] = []
    marker = None
    async with get_db_ctx() as db:
        # the parent is looked up without this route in the way: it is the bucket's own pool
        parent = await subfolders.parent_pool(db, vhost_id, route_path)
        if not parent or parent["id"] != dst_pool_id or not route_id:
            return notes
        bucket = subfolders.path_segments(route_path)[0]
        await db.execute("UPDATE migrations SET route_id = NULL WHERE route_id = ?", (route_id,))
        await db.execute("DELETE FROM routes WHERE id = ?", (route_id,))
        http_changed, _ = await subfolders.sync_http_routes(db, bucket, route_path, parent["id"], None)
        await db.commit()
        changed = [vhost_id] + http_changed
        address = await subfolders.pool_endpoint(db, parent["id"])
        if address:
            marker = (address, bucket, subfolders.folder_key(route_path) + SUBFOLDER_MARKER, parent["id"])
    for changed_vhost_id in changed:
        filepath, content = await generate_vhost_config(changed_vhost_id)
        result = await apply_config_file(filepath, content, reload=True)
        if result.ok:
            async with get_db_ctx() as db:
                await record_file_state(db, filepath, content)
                await db.commit()
        else:
            notes.append(f"WARNING: nginx {result.stage} failed while removing the folder route: {result.output}")
    if marker:
        address, bucket, key, parent_id = marker
        try:
            access_key, secret_key, _ = await get_pool_s3_params(parent_id)
            await s3client.delete_object(address, bucket, key, access_key=access_key, secret_key=secret_key)
        except Exception as exc:
            notes.append(f"WARNING: could not remove the marker '{key}': {exc}")
    notes.append(f"'{route_path}' is back on the bucket's own pool — its folder route was removed")
    return notes


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
        route_id = m.get("route_id")  # may be None for legacy migrations
        # "" moves the whole bucket; "a/b/" moves only that folder, through its own route
        folder = m.get("folder") or ""
        what = f"folder '{folder}' of bucket '{bucket}'" if folder else f"bucket '{bucket}'"
        route_path = f"/{bucket}/{folder}"
        # True once nginx routes this bucket through the split (set below, or inherited from
        # an interrupted run of this migration).
        split_active = bool(m.get("split_routing"))

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

        # ── Safety: refuse if bucket is marked deleted in bucket_sync ─────
        async with get_db_ctx() as db:
            bs_rows = await db.execute_fetchall(
                "SELECT status FROM bucket_sync WHERE vhost_id = ? AND bucket = ?",
                (m["vhost_id"], bucket),
            )
        if bs_rows and dict(bs_rows[0])["status"] == "deleted":
            msg = f"Bucket '{bucket}' is marked as deleted — cannot migrate"
            logger.error("Migration %d aborted: %s", migration_id, msg)
            await _log(migration_id, "error", msg)
            await _set_phase(migration_id, "error", msg)
            return

        # ── Safety: a previous migration may have left split routing in place ──
        # The destination then already holds client writes. Finishing the same move is fine
        # (and takes over the split); pointing the bucket anywhere else is not.
        async with get_db_ctx() as db:
            prior_rows = await db.execute_fetchall(
                """SELECT id, src_pool_id, dst_pool_id, folder FROM migrations
                   WHERE vhost_id = ? AND bucket = ? AND split_routing = 1 AND id != ?""",
                (m["vhost_id"], bucket, migration_id),
            )
            prior = [dict(r) for r in prior_rows]
            # One split at a time per bucket: only the very same move (same folder, same pools)
            # may take it over.
            mismatched = [
                p
                for p in prior
                if (p["src_pool_id"], p["dst_pool_id"], p["folder"] or "") != (src_pool_id, dst_pool_id, folder)
            ]
            if prior and not mismatched:
                await db.execute(
                    f"UPDATE migrations SET split_routing = 0 WHERE id IN ({','.join('?' * len(prior))})",
                    [p["id"] for p in prior],
                )
                await db.execute("UPDATE migrations SET split_routing = 1 WHERE id = ?", (migration_id,))
                await db.commit()
        if mismatched:
            msg = (
                f"Split routing from migration {mismatched[0]['id']} (pool {mismatched[0]['src_pool_id']} → "
                f"{mismatched[0]['dst_pool_id']}) is still active for bucket '{bucket}'. Finish that move "
                "first by migrating the bucket between the same two pools."
            )
            logger.error("Migration %d aborted: %s", migration_id, msg)
            await _log(migration_id, "error", msg)
            await _set_phase(migration_id, "error", msg)
            return
        if prior:
            split_active = True
            await _log(
                migration_id,
                "copying",
                f"Taking over split routing left by migration {prior[0]['id']} — the destination already "
                "holds client writes, every copy pass uses --update",
            )

        # Generate rclone config
        config_content = await _build_rclone_config(src_pool_id, dst_pool_id)
        config_dir = tempfile.mkdtemp(prefix="nanio-rclone-")
        config_path = os.path.join(config_dir, "rclone.conf")
        with open(config_path, "w") as f:
            f.write(config_content)
        # Restrict permissions on the config file
        os.chmod(config_path, 0o600)

        src_remote = f"src:{bucket}/{folder}".rstrip("/")
        dst_remote = f"dst:{bucket}/{folder}".rstrip("/")

        # ── Pre-flight: refuse to copy from an empty source ───────────────
        src_address = await _first_member_endpoint(src_pool_id)
        if src_address:
            ak, sk, _ = await get_pool_s3_params(src_pool_id)
            try:
                src_count = await count_objects(src_address, bucket, access_key=ak, secret_key=sk, prefix=folder)
                if src_count == 0:
                    msg = (
                        f"Refusing to migrate: source {what} is empty. "
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
                        prefix=folder,
                    ):
                        # copy mode is additive — destination objects are preserved
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
            await _log(migration_id, "copying", f"Starting rclone copy {pass_label}: {src_remote} → {dst_remote}")
            ok = await _run_rclone(migration_id, config_path, src_remote, dst_remote, "copying", update=split_active)
            if not ok:
                await _set_phase(migration_id, "error", f"rclone copy failed ({pass_label})")
                return

            # Check convergence by comparing object counts on both sides
            try:
                src_ak, src_sk, _ = await get_pool_s3_params(src_pool_id)
                dst_ak, dst_sk, _ = await get_pool_s3_params(dst_pool_id)
                src_addr = await _first_member_endpoint(src_pool_id)
                dst_addr = await _first_member_endpoint(dst_pool_id)
                if src_addr and dst_addr:
                    src_count = await count_objects(
                        src_addr, bucket, access_key=src_ak, secret_key=src_sk, prefix=folder
                    )
                    dst_count = await count_objects(
                        dst_addr, bucket, access_key=dst_ak, secret_key=dst_sk, prefix=folder
                    )
                    await _log(
                        migration_id, "copying", f"{pass_label}: src={src_count} objects, dst={dst_count} objects"
                    )
                    if src_count == dst_count:
                        await _log(migration_id, "copying", f"Converged after {pass_num} pass(es)")
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
        # Always entered, even when the counts already match: without the split, an object
        # written to the source between the final check and the switch would be left behind.
        # From here on the destination is authoritative and the source receives no new data.
        if not split_active:
            await _set_phase(migration_id, "write_routing")
            await _log(
                migration_id,
                "write_routing",
                "nginx: writes → dst pool | reads → dst pool (404-fallback to src) | DELETE mirrored to src",
            )
            async with get_db_ctx() as db:
                await db.execute("UPDATE migrations SET split_routing = 1 WHERE id = ?", (migration_id,))
                await db.commit()
            vhost_id = m["vhost_id"]
            filepath, content_ng = await generate_vhost_config(vhost_id)
            wr_result = await apply_config_file(filepath, content_ng, reload=True)
            if wr_result.ok:
                async with get_db_ctx() as db:
                    await record_file_state(db, filepath, content_ng)
                    await db.commit()
                split_active = True
                await _log(migration_id, "write_routing", "nginx reloaded with write-routing split config")
                # Cascade write-routing to linked http vhosts
                await _cascade_http_write_routing(migration_id, bucket, src_pool_id, dst_pool_id, route_path)
            else:
                # The previous config file is back in place — the split never went live
                async with get_db_ctx() as db:
                    await db.execute("UPDATE migrations SET split_routing = 0 WHERE id = ?", (migration_id,))
                    await db.commit()
                await _log(
                    migration_id,
                    "write_routing",
                    f"nginx {wr_result.stage} failed — previous config kept, write-routing not active: "
                    f"{wr_result.output}",
                )
                await _set_phase(
                    migration_id, "error", f"nginx {wr_result.stage} failed during write_routing: {wr_result.output}"
                )
                return

        # ── Phase: verifying ──────────────────────────────────────────
        # Split routing is active: the source is frozen and the destination may hold client
        # writes. Each pass copies with --update (never overwriting a newer destination object)
        # and then verifies. Passes repeat while the number of unresolved objects shrinks, which
        # covers an upload that was still in flight to the source when the split went live.
        await _set_phase(migration_id, "verifying")
        s_verify = get_settings()
        max_verify_passes = s_verify.migration_max_copy_passes
        prev_diff_count: Optional[int] = None

        for verify_pass in range(1, max_verify_passes + 1):
            await _log(migration_id, "verifying", f"Verify pass {verify_pass}/{max_verify_passes}: copy then check")

            ok = await _run_rclone(migration_id, config_path, src_remote, dst_remote, "verifying", update=True)
            if not ok:
                await _set_phase(migration_id, "error", f"Verify pass {verify_pass}: copy step failed")
                return

            await _log(
                migration_id, "verifying", f"Verify pass {verify_pass}: rclone check — every source object at dst"
            )
            check_ok, diff_count = await _verify_destination(
                migration_id, config_path, src_remote, dst_remote, bucket, src_pool_id, dst_pool_id, folder
            )
            if check_ok:
                await _log(migration_id, "verifying", f"Verify pass {verify_pass}: check passed — dst holds all of src")
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
        swept_http_vhosts: list = []

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

            if not folder:
                await db.execute(
                    """UPDATE bucket_sync SET status = 'routed', routed_pool_id = ?
                       WHERE vhost_id = ? AND bucket = ?""",
                    (dst_pool_id, vhost_id, bucket),
                )
            await db.execute("UPDATE migrations SET split_routing = 0 WHERE id = ?", (migration_id,))
            await db.commit()

        # Regenerate nginx config (write_routing split is no longer needed)
        filepath, content = await generate_vhost_config(vhost_id)

        # Write, nginx -t, reload through the shared gate — rolled back on failure
        sw_result = await apply_config_file(filepath, content, reload=True)
        if sw_result.ok:
            async with get_db_ctx() as db:
                await record_file_state(db, filepath, content)
                await db.commit()
            await _log(migration_id, "switching", "nginx reloaded with new route")
            # Cascade switching to linked http vhosts
            swept_http_vhosts = await _cascade_http_switching(
                migration_id, bucket, src_pool_id, dst_pool_id, route_path
            )
        else:
            # nginx test/reload failed — the previous config file is back in place;
            # roll the DB route back to the source pool so DB and disk agree.
            await _log(
                migration_id,
                "switching",
                f"nginx {sw_result.stage} failed: {sw_result.output} — rolling back route",
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
                if not folder:
                    await db.execute(
                        """UPDATE bucket_sync SET status = 'migrating', routed_pool_id = NULL
                           WHERE vhost_id = ? AND bucket = ?""",
                        (vhost_id, bucket),
                    )
                await db.execute("UPDATE migrations SET split_routing = 1 WHERE id = ?", (migration_id,))
                await db.commit()
            await _set_phase(
                migration_id, "error", f"nginx {sw_result.stage} failed during switching: {sw_result.output}"
            )
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
                (src_pool_id, route_path, orphaned_at, migration_id),
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
                "folder": folder,
                "source_pool_id": src_pool_id,
                "source_pool_name": pool_names.get(src_pool_id),
                "target_pool_id": dst_pool_id,
                "target_pool_name": pool_names.get(dst_pool_id),
                "route_id": route_id,
                "status": "done",
                "orphaned_source_pool_id": src_pool_id,
                "orphaned_source_prefix": route_path,
                "orphaned_at": orphaned_at,
                "swept_http_vhosts": swept_http_vhosts,
            }
        )

        # A folder that moved changes what its bucket needs around it (marker object, or no
        # route at all when it went back to the bucket's own pool)
        if folder:
            for note in await _finish_folder_move(m["vhost_id"], route_id, route_path, dst_pool_id):
                await _log(migration_id, "switching", note)
        for warning in await ensure_markers(m["vhost_id"]):
            await _log(migration_id, "switching", f"WARNING: {warning}")

        # ── Phase: done ───────────────────────────────────────────────────
        await _set_phase(migration_id, "done")
        await _log(
            migration_id,
            "done",
            f"Migration completed: {what} moved to pool {dst_pool_id}. "
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
    route_id: Optional[int] = None,
    folder: str = "",
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
                   (vhost_id, bucket, src_pool_id, dst_pool_id, phase, route_id, folder)
                   VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
                (vhost_id, bucket, src_pool_id, dst_pool_id, route_id, folder),
            )
            migration_id = cursor.lastrowid
            await log_audit(
                db,
                "start_migration",
                "migration",
                migration_id,
                after={
                    "bucket": bucket,
                    "folder": folder,
                    "src_pool_id": src_pool_id,
                    "dst_pool_id": dst_pool_id,
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
