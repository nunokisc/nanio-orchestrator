"""Background bucket sync task.

Polls the nanio-default pool member for each vhost at a configurable interval.
Compares discovered buckets against known routes and upserts into bucket_sync.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import List

from nanio_orchestrator.config import get_settings
from nanio_orchestrator.db import get_db_ctx
from nanio_orchestrator.s3client import list_buckets

logger = logging.getLogger(__name__)

_running = False


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
        # path_prefix like '/assets-2025/' → first segment = 'assets-2025'
        route_rows = await db.execute_fetchall(
            "SELECT path_prefix, pool_id FROM routes WHERE vhost_id = ?", (vhost_id,)
        )
        routed: dict[str, int] = {}
        for r in route_rows:
            seg = r["path_prefix"].strip("/").split("/")[0]
            if seg:
                routed[seg] = r["pool_id"]

    # ── S3 call outside DB context ─────────────────────────────────────────
    s = get_settings()
    try:
        buckets = await list_buckets(
            member_address,
            access_key=s.s3_access_key,
            secret_key=s.s3_secret_key,
        )
    except Exception as e:
        logger.warning("Bucket sync failed for vhost %d (%s): %s", vhost_id, member_address, e)
        return {"vhost_id": vhost_id, "error": str(e)}

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    async with get_db_ctx() as db:
        for b in buckets:
            bucket_name = b["name"]
            if bucket_name in routed:
                new_status = "routed"
                pool_id = routed[bucket_name]
            else:
                # Preserve 'ignored' status if set by operator
                existing = await db.execute_fetchall(
                    "SELECT status FROM bucket_sync WHERE vhost_id = ? AND bucket = ?",
                    (vhost_id, bucket_name),
                )
                if existing and dict(existing[0])["status"] == "ignored":
                    new_status = "ignored"
                else:
                    new_status = "unrouted"
                pool_id = None

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
        await db.commit()

    return {
        "vhost_id": vhost_id,
        "buckets_found": len(buckets),
        "synced_at": now,
    }


async def sync_all_vhosts() -> List[dict]:
    """Sync all vhosts that have a default_pool_id configured."""
    async with get_db_ctx() as db:
        vhosts = await db.execute_fetchall(
            "SELECT id FROM vhosts WHERE default_pool_id IS NOT NULL"
        )
    results = []
    for v in vhosts:
        result = await sync_vhost_buckets_once(v["id"])
        results.append(result)
    return results


async def bucket_sync_loop() -> None:
    """Run bucket sync in a loop."""
    global _running
    _running = True
    s = get_settings()
    interval = s.bucket_sync_interval

    logger.info("Bucket sync started (interval=%ds)", interval)

    while _running:
        try:
            await sync_all_vhosts()
        except Exception as e:
            logger.error("Bucket sync error: %s", e)
        await asyncio.sleep(interval)


def stop_bucket_sync() -> None:
    global _running
    _running = False
