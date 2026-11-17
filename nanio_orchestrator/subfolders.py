"""Sub-folder routes: one folder of a bucket served by another pool.

A route ``/bucket/folder/`` on an S3 vhost sends that folder to its own pool. The rest of
the bucket stays on the "parent" pool: the pool of the ``/bucket/`` route, or the vhost's
default pool.

    object requests under the folder  → the folder's pool   (nginx prefix match)
    listings inside the folder        → the folder's pool   (chosen from ?prefix=)
    everything else in the bucket     → the parent pool

Two things must exist for this to work and clients cannot create them through the gateway,
so the orchestrator does:

- the bucket on the folder's pool (created when the route is created);
- a marker object ``folder/.nanio-shard`` on the parent pool, so the folder shows up when
  the bucket is browsed. nanio hides ``.nanio*`` names from object listings, so clients
  never see or delete it. Bucket sync rewrites it every cycle, which also restores it after
  the bucket itself is migrated to another pool.

``http`` vhosts that serve the parent pool's files get a matching plain route.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from nanio_orchestrator import s3client
from nanio_orchestrator.credentials import get_pool_s3_params
from nanio_orchestrator.nginx.generator import SUBFOLDER_MARKER, is_subfolder_prefix, path_segments

logger = logging.getLogger(__name__)


def folder_key(path_prefix: str) -> str:
    """``/photos/2025/q1/`` → ``2025/q1/`` (the key prefix inside the bucket)."""
    return "/".join(path_segments(path_prefix)[1:]) + "/"


async def pool_endpoint(db, pool_id: int) -> Optional[str]:
    rows = await db.execute_fetchall(
        "SELECT address FROM pool_members WHERE pool_id = ? AND enabled = 1 ORDER BY id LIMIT 1", (pool_id,)
    )
    return rows[0]["address"] if rows else None


async def parent_pool(db, vhost_id: int, path_prefix: str) -> Optional[dict]:
    """The pool serving the rest of the bucket, or None if the vhost does not serve it."""
    bucket = path_segments(path_prefix)[0]
    rows = await db.execute_fetchall(
        """SELECT p.id, p.name, p.type FROM routes r JOIN pools p ON p.id = r.pool_id
           WHERE r.vhost_id = ? AND r.path_prefix = ?""",
        (vhost_id, f"/{bucket}/"),
    )
    if not rows:
        rows = await db.execute_fetchall(
            """SELECT p.id, p.name, p.type FROM vhosts v JOIN pools p ON p.id = v.default_pool_id
               WHERE v.id = ?""",
            (vhost_id,),
        )
    return dict(rows[0]) if rows else None


async def ensure_markers(vhost_id: int) -> List[str]:
    """(Re)write the marker of every sub-folder route of a vhost. Returns warnings."""
    from nanio_orchestrator.db import get_db_ctx

    todo: List[Tuple[str, str, str, int, str, str]] = []
    async with get_db_ctx() as db:
        routes = await db.execute_fetchall(
            """SELECT r.path_prefix, p.name AS pool_name FROM routes r JOIN pools p ON p.id = r.pool_id
               WHERE r.vhost_id = ? AND r.enabled = 1 AND p.type = 'nanio'""",
            (vhost_id,),
        )
        for route in routes:
            if not is_subfolder_prefix(route["path_prefix"]):
                continue
            parent = await parent_pool(db, vhost_id, route["path_prefix"])
            address = await pool_endpoint(db, parent["id"]) if parent else None
            if address:
                bucket = path_segments(route["path_prefix"])[0]
                key = folder_key(route["path_prefix"]) + SUBFOLDER_MARKER
                todo.append((address, bucket, key, parent["id"], parent["name"], route["pool_name"]))

    warnings: List[str] = []
    for address, bucket, key, parent_id, parent_name, target_name in todo:
        text = (
            f"The objects of this folder are stored on pool '{target_name}' "
            "(nanio-orchestrator sub-folder route). This marker keeps the folder visible in listings.\n"
        )
        try:
            access_key, secret_key, _ = await get_pool_s3_params(parent_id)
            ok = await s3client.put_object(
                address, bucket, key, text.encode(), access_key=access_key, secret_key=secret_key
            )
            reason = "the pool refused it"
        except Exception as exc:
            ok, reason = False, str(exc)
        if not ok:
            warnings.append(
                f"The marker '{key}' could not be written to bucket '{bucket}' on pool '{parent_name}' ({reason}). "
                "The folder works; it just does not appear when the bucket is listed from its root."
            )
    return warnings


async def sync_http_routes(
    db, bucket: str, path_prefix: str, parent_pool_id: int, target_pool_id: Optional[int]
) -> Tuple[List[int], List[str]]:
    """Make http vhosts that serve the bucket from the parent pool's files follow a sub-folder.

    With ``target_pool_id`` the folder route is added (or repointed) on each such vhost; with
    None it is removed. Returns (ids of vhosts whose config must be rewritten, warnings).
    The caller commits and rewrites the configs.
    """
    vhosts = await db.execute_fetchall(
        """SELECT v.id, v.server_name FROM routes r
           JOIN vhosts v ON v.id = r.vhost_id
           JOIN pools p ON p.id = r.pool_id
           WHERE r.path_prefix = ? AND p.type = 'http' AND p.source_nanio_pool_id = ?""",
        (f"/{bucket}/", parent_pool_id),
    )
    changed: List[int] = []
    warnings: List[str] = []
    if not vhosts:
        return changed, warnings

    http_target: Optional[Dict] = None
    if target_pool_id is not None:
        rows = await db.execute_fetchall(
            "SELECT id, name FROM pools WHERE type = 'http' AND source_nanio_pool_id = ? ORDER BY id LIMIT 1",
            (target_pool_id,),
        )
        http_target = dict(rows[0]) if rows else None

    for vhost in vhosts:
        if target_pool_id is None:
            cursor = await db.execute(
                "DELETE FROM routes WHERE vhost_id = ? AND path_prefix = ?", (vhost["id"], path_prefix)
            )
            if cursor.rowcount:
                changed.append(vhost["id"])
        elif http_target is None:
            warnings.append(
                f"http vhost '{vhost['server_name']}' serves bucket '{bucket}' but no http pool is linked to the "
                f"folder's pool, so '{path_prefix}' answers 404 there. Create an http pool linked to that pool and "
                "add the route to the vhost."
            )
        else:
            await db.execute(
                """INSERT INTO routes (vhost_id, path_prefix, pool_id, enabled) VALUES (?, ?, ?, 1)
                   ON CONFLICT(vhost_id, path_prefix) DO UPDATE SET
                     pool_id = excluded.pool_id, updated_at = datetime('now')""",
                (vhost["id"], path_prefix, http_target["id"]),
            )
            changed.append(vhost["id"])
    return changed, warnings
