"""Background bucket sync task.

Polls the nanio-default pool member for each vhost at a configurable interval.
Compares discovered buckets against known routes and upserts into bucket_sync.

After syncing, checks routed buckets: a bucket that its target pool reports as
not existing is flagged (``bucket_sync.missing_since``). The route is never
removed here — a node whose storage failed to mount also answers "no such
bucket", and dropping the route would send clients to another pool. Removing
the route is the operator's decision.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import List

from nanio_orchestrator.config import get_settings
from nanio_orchestrator.credentials import get_pool_s3_params
from nanio_orchestrator.db import get_db_ctx
from nanio_orchestrator.s3client import bucket_exists, list_buckets

logger = logging.getLogger(__name__)

_stop_event = asyncio.Event()


async def sync_vhost_buckets_once(vhost_id: int) -> dict:
    """Sync bucket list for one vhost. Returns a result dict."""
    async with get_db_ctx() as db:
        vhost_rows = await db.execute_fetchall(
            "SELECT id, server_name, default_pool_id FROM vhosts WHERE id = ?", (vhost_id,)
        )
        if not vhost_rows:
            return {"error": "Vhost not found"}
        vhost = dict(vhost_rows[0])

        if not vhost.get("default_pool_id"):
            return {"vhost_id": vhost_id, "skipped": True, "reason": "No default_pool_id configured"}

        # Skip HTTP-type pools — they have no S3 ListBuckets semantics
        pool_rows = await db.execute_fetchall("SELECT type FROM pools WHERE id = ?", (vhost["default_pool_id"],))
        if not pool_rows or pool_rows[0]["type"] != "nanio":
            return {
                "vhost_id": vhost_id,
                "skipped": True,
                "reason": "Default pool is not nanio type — bucket sync skipped",
            }

        member_rows = await db.execute_fetchall(
            """SELECT address FROM pool_members
               WHERE pool_id = ? AND enabled = 1
               ORDER BY id LIMIT 1""",
            (vhost["default_pool_id"],),
        )
        if not member_rows:
            return {"vhost_id": vhost_id, "skipped": True, "reason": "Default pool has no enabled members"}

        member_address = dict(member_rows[0])["address"]

        # Build set of buckets that already have dedicated routes
        # Use the full stripped path_prefix as the bucket key to avoid collisions
        # (e.g. /photos/ and /photos/2025/ are distinct routes)
        route_rows = await db.execute_fetchall(
            "SELECT path_prefix, pool_id FROM routes WHERE vhost_id = ?", (vhost_id,)
        )
        routed: dict[str, int] = {}
        for r in route_rows:
            seg = r["path_prefix"].strip("/")
            if seg:
                routed[seg] = r["pool_id"]

    # ── S3 call outside DB context ─────────────────────────────────────────
    access_key, secret_key, _ = await get_pool_s3_params(vhost["default_pool_id"])
    try:
        buckets = await list_buckets(member_address, access_key=access_key, secret_key=secret_key)
    except Exception as e:
        logger.warning("Bucket sync failed for vhost %d (%s): %s", vhost_id, member_address, e)
        return {"vhost_id": vhost_id, "error": str(e)}

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    async with get_db_ctx() as db:
        # Snapshot existing states for change detection
        existing_rows = await db.execute_fetchall(
            "SELECT bucket, status FROM bucket_sync WHERE vhost_id = ?", (vhost_id,)
        )
        existing_states = {r["bucket"]: r["status"] for r in existing_rows}

        for b in buckets:
            bucket_name = b["name"]
            if bucket_name in routed:
                new_status = "routed"
                pool_id = routed[bucket_name]
            else:
                if existing_states.get(bucket_name) == "ignored":
                    new_status = "ignored"
                else:
                    new_status = "unrouted"
                pool_id = None

            old_status = existing_states.get(bucket_name)
            if old_status is None:
                logger.info("vhost %d: new bucket '%s' discovered (%s)", vhost_id, bucket_name, new_status)
            elif old_status != new_status:
                logger.info("vhost %d: bucket '%s' status %s → %s", vhost_id, bucket_name, old_status, new_status)

            await db.execute(
                """INSERT INTO bucket_sync (vhost_id, bucket, discovered_at, status, routed_pool_id)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(vhost_id, bucket) DO UPDATE SET
                     discovered_at  = excluded.discovered_at,
                     status         = CASE
                                        WHEN bucket_sync.status = 'ignored' THEN 'ignored'
                                        ELSE excluded.status
                                      END,
                     routed_pool_id = excluded.routed_pool_id""",
                (vhost_id, bucket_name, now, new_status, pool_id),
            )

        # Mark buckets that disappeared from S3 as 'deleted'
        discovered_names = {b["name"] for b in buckets}
        for bucket_name, status in existing_states.items():
            if bucket_name not in discovered_names and status not in ("ignored", "deleted"):
                logger.info(
                    "vhost %d: bucket '%s' no longer in ListBuckets — marking deleted",
                    vhost_id,
                    bucket_name,
                )
                await db.execute(
                    """UPDATE bucket_sync SET status = 'deleted'
                       WHERE vhost_id = ? AND bucket = ?""",
                    (vhost_id, bucket_name),
                )

        await db.commit()

    logger.info("vhost %d: sync done — %d bucket(s) found", vhost_id, len(buckets))

    missing = await _flag_missing_routed_buckets(vhost_id, now)
    result: dict = {"vhost_id": vhost_id, "buckets_found": len(buckets), "synced_at": now}
    if missing:
        result["missing_on_pool"] = missing
    return result


async def _flag_missing_routed_buckets(vhost_id: int, now: str) -> List[dict]:
    """Flag routed buckets that their target pool reports as not existing.

    Sets ``missing_since`` (kept from the first time it was seen) and clears it when the
    bucket is found again. Routing is not touched and nginx is not reloaded. An unreachable
    pool or an unexpected answer changes nothing.
    """
    missing: List[dict] = []

    async with get_db_ctx() as db:
        await db.execute(
            "UPDATE bucket_sync SET missing_since = NULL WHERE vhost_id = ? AND status != 'routed'", (vhost_id,)
        )
        await db.commit()
        rows = await db.execute_fetchall(
            """SELECT bs.bucket, bs.routed_pool_id, bs.missing_since,
                      (SELECT pm.address FROM pool_members pm
                       WHERE pm.pool_id = bs.routed_pool_id AND pm.enabled = 1
                       ORDER BY pm.id LIMIT 1) AS target_member
               FROM bucket_sync bs
               JOIN routes r ON r.vhost_id = bs.vhost_id
                            AND r.path_prefix = ('/' || bs.bucket || '/')
               WHERE bs.vhost_id = ?
                 AND bs.status = 'routed'
                 AND bs.routed_pool_id IS NOT NULL""",
            (vhost_id,),
        )
        routed = [dict(r) for r in rows]

    for entry in routed:
        target_member = entry.get("target_member")
        if not target_member:
            continue

        try:
            ak, sk, _ = await get_pool_s3_params(entry["routed_pool_id"])
            exists = await bucket_exists(target_member, entry["bucket"], access_key=ak, secret_key=sk)
        except Exception as exc:
            logger.warning(
                "Could not verify bucket %s on %s (vhost %d): %s", entry["bucket"], target_member, vhost_id, exc
            )
            continue

        if exists and not entry["missing_since"]:
            continue

        since = None if exists else (entry["missing_since"] or now)
        async with get_db_ctx() as db:
            await db.execute(
                "UPDATE bucket_sync SET missing_since = ? WHERE vhost_id = ? AND bucket = ?",
                (since, vhost_id, entry["bucket"]),
            )
            await db.commit()

        if exists:
            logger.info("Bucket '%s' is back on %s (vhost %d)", entry["bucket"], target_member, vhost_id)
        else:
            if not entry["missing_since"]:
                logger.warning(
                    "Bucket '%s' is routed to %s but does not exist there (vhost %d) — route kept, flagged",
                    entry["bucket"],
                    target_member,
                    vhost_id,
                )
            missing.append({"bucket": entry["bucket"], "target_member": target_member, "missing_since": since})

    return missing


async def sync_all_vhosts() -> List[dict]:
    """Sync all vhosts that have a nanio default pool configured."""
    async with get_db_ctx() as db:
        vhosts = await db.execute_fetchall(
            """SELECT v.id FROM vhosts v
               JOIN pools p ON v.default_pool_id = p.id
               WHERE p.type = 'nanio'"""
        )
    results = []
    for v in vhosts:
        result = await sync_vhost_buckets_once(v["id"])
        results.append(result)
    return results


async def bucket_sync_loop() -> None:
    """Run bucket sync in a loop."""
    _stop_event.clear()
    s = get_settings()
    interval = s.bucket_sync_interval

    logger.info("Bucket sync started (interval=%ds)", interval)

    while not _stop_event.is_set():
        try:
            await sync_all_vhosts()
        except Exception as e:
            logger.error("Bucket sync error: %s", e)

        try:
            await asyncio.wait_for(_stop_event.wait(), timeout=interval)
            break  # stop_event was set
        except asyncio.TimeoutError:
            pass  # interval elapsed, continue loop


def stop_bucket_sync() -> None:
    _stop_event.set()
