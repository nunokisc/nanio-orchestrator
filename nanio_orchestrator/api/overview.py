"""One read-only summary for the panel's overview and status bar.

Everything here can be had from the other endpoints, at the cost of several requests per
pool; the panel polls this one instead.
"""

from __future__ import annotations

from fastapi import APIRouter

from nanio_orchestrator.db import get_db_ctx

router = APIRouter(tags=["overview"])


@router.get("/api/overview")
async def overview():
    async with get_db_ctx() as db:
        pools = await db.execute_fetchall(
            """SELECT p.id, p.name, p.type,
                      (SELECT COUNT(*) FROM pool_members m WHERE m.pool_id = p.id) AS members,
                      (SELECT COUNT(*) FROM pool_members m WHERE m.pool_id = p.id AND m.enabled = 1)
                        AS enabled_members,
                      EXISTS (SELECT 1 FROM pool_credentials c WHERE c.pool_id = p.id) AS has_credentials
               FROM pools p ORDER BY p.name"""
        )
        counts = await db.execute_fetchall(
            """SELECT (SELECT COUNT(*) FROM vhosts) AS vhosts,
                      (SELECT COUNT(*) FROM routes WHERE path_prefix != '/') AS routes,
                      (SELECT COUNT(*) FROM bucket_sync WHERE status = 'unrouted') AS unrouted_buckets"""
        )
        missing = await db.execute_fetchall(
            """SELECT b.vhost_id, v.server_name, b.bucket, p.name AS pool_name, b.missing_since
               FROM bucket_sync b
               JOIN vhosts v ON v.id = b.vhost_id
               LEFT JOIN pools p ON p.id = b.routed_pool_id
               WHERE b.missing_since IS NOT NULL
               ORDER BY v.server_name, b.bucket"""
        )
    return {
        "pools": [{**dict(p), "has_credentials": bool(p["has_credentials"])} for p in pools],
        "vhosts": counts[0]["vhosts"],
        "routes": counts[0]["routes"],
        "unrouted_buckets": counts[0]["unrouted_buckets"],
        "missing_buckets": [dict(m) for m in missing],
    }
