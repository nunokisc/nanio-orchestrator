"""Read-only S3 explorer for the panel: look inside a bucket and see which pool holds what.

Operator-initiated management traffic, like the bucket listing and the orphan purge: the
orchestrator asks one storage node on the operator's behalf. Client traffic still never
passes through it. Nothing here writes to a pool: no upload, delete, rename or bucket
creation.

Answers stay honest: a pool that cannot be asked (no member, no credentials, unreachable,
refusing) is an error with the reason, never an empty listing. 404 means the bucket or the
object really does not exist.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Optional

from fastapi import APIRouter, HTTPException, Path, Query
from fastapi.responses import Response

from nanio_orchestrator import s3client
from nanio_orchestrator.audit_log import log_audit
from nanio_orchestrator.credentials import get_pool_s3_params
from nanio_orchestrator.db import get_db_ctx
from nanio_orchestrator.nginx.generator import is_subfolder_prefix, path_segments
from nanio_orchestrator.subfolders import folder_key, pool_endpoint

router = APIRouter(prefix="/api/explorer", tags=["explorer"])

# A download is held in memory once, so it is capped. Larger objects are fetched with an S3
# client through the gateway, which streams.
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
PAGE_SIZE = 200

_BUCKET = Path(..., pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$", max_length=255)


class _Access:
    """What is needed to ask a pool: its row, one enabled member and its credentials."""

    def __init__(self, pool: dict, address: str, access_key: str, secret_key: str, region: str):
        self.pool = pool
        self.address = address
        self.auth = {"access_key": access_key, "secret_key": secret_key, "region": region}

    @property
    def ref(self) -> dict:
        return {"id": self.pool["id"], "name": self.pool["name"]}


async def _access(pool_id: int) -> _Access:
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT id, name, type FROM pools WHERE id = ?", (pool_id,))
        if not rows:
            raise HTTPException(404, "Pool not found")
        pool = dict(rows[0])
        if pool["type"] != "nanio":
            raise HTTPException(400, f"Pool '{pool['name']}' is an http pool: it has no S3 API to browse.")
        address = await pool_endpoint(db, pool_id)
    if not address:
        raise HTTPException(400, f"Pool '{pool['name']}' has no enabled member to ask.")
    access_key, secret_key, region = await get_pool_s3_params(pool_id)
    if not access_key or not secret_key:
        raise HTTPException(400, f"Pool '{pool['name']}' has no stored credentials. Store them on the Pools screen.")
    return _Access(pool, address, access_key, secret_key, region)


def _unreachable(access: _Access, what: str, exc: Exception) -> HTTPException:
    return HTTPException(502, f"Could not {what} on pool '{access.pool['name']}' ({access.address}): {exc}")


def _download_name(key: str) -> str:
    """A file name that is safe in a Content-Disposition header and on any file system."""
    name = re.sub(r"[^A-Za-z0-9._-]", "_", key.rstrip("/").rsplit("/", 1)[-1]).lstrip(".")
    return name[:200] or "download"


async def _splits(db, vhost_id: int) -> dict:
    """Live-migration splits active on this vhost, by route path (``/bucket/`` or
    ``/bucket/folder/``): {path: {"migration_id", "source", "destination"}}.

    While a split is active nginx sends the path to the destination pool first and falls back
    to the source for objects not copied yet, whatever pool the route still names.
    """
    rows = await db.execute_fetchall(
        """SELECT m.id, m.bucket, m.folder, sp.id AS src_id, sp.name AS src_name,
                  dp.id AS dst_id, dp.name AS dst_name
           FROM migrations m
           JOIN pools sp ON sp.id = m.src_pool_id
           JOIN pools dp ON dp.id = m.dst_pool_id
           WHERE m.vhost_id = ? AND m.split_routing = 1
           ORDER BY m.id""",
        (vhost_id,),
    )
    return {
        f"/{r['bucket']}/{r['folder']}": {
            "migration_id": r["id"],
            "source": {"id": r["src_id"], "name": r["src_name"]},
            "destination": {"id": r["dst_id"], "name": r["dst_name"]},
        }
        for r in rows
    }


async def _routed_folders(vhost_id: int, bucket: str, pool_id: int) -> dict:
    """Folders of ``bucket`` that this vhost serves from a pool other than ``pool_id``:
    {folder key: {"id", "name"}}. The pool is the one the folder's route names, or the
    destination of the split while the folder is being moved."""
    async with get_db_ctx() as db:
        if not await db.execute_fetchall("SELECT id FROM vhosts WHERE id = ?", (vhost_id,)):
            raise HTTPException(404, "Vhost not found")
        rows = await db.execute_fetchall(
            """SELECT r.path_prefix, p.id, p.name FROM routes r JOIN pools p ON p.id = r.pool_id
               WHERE r.vhost_id = ? AND r.enabled = 1 AND p.type = 'nanio'""",
            (vhost_id,),
        )
        splits = await _splits(db, vhost_id)
    folders = {}
    for r in rows:
        if not is_subfolder_prefix(r["path_prefix"]) or path_segments(r["path_prefix"])[0] != bucket:
            continue
        split = splits.get(r["path_prefix"].rstrip("/") + "/")
        serving = split["destination"] if split else {"id": r["id"], "name": r["name"]}
        if serving["id"] != pool_id:
            folders[folder_key(r["path_prefix"])] = serving
    return folders


@router.get("/pools/{pool_id}/buckets")
async def list_buckets(pool_id: int):
    """The buckets that exist on this pool."""
    access = await _access(pool_id)
    try:
        buckets = await s3client.list_buckets(access.address, **access.auth)
    except Exception as exc:
        raise _unreachable(access, "list the buckets", exc) from exc
    return {"pool": access.ref, "member": access.address, "buckets": sorted(b["name"] for b in buckets)}


@router.get("/pools/{pool_id}/buckets/{bucket}/objects")
async def list_objects(
    pool_id: int,
    bucket: str = _BUCKET,
    prefix: str = Query("", max_length=1024),
    token: str = Query("", max_length=2048),
    limit: int = Query(PAGE_SIZE, ge=1, le=1000),
    vhost_id: Optional[int] = None,
):
    """One page of one level of a bucket on this pool: folders and objects under ``prefix``.

    With ``vhost_id``, a folder that a route of that vhost sends to another pool is marked
    with ``routed_pool``: what clients see under it is on that pool, not on this one.
    """
    access = await _access(pool_id)
    routed = await _routed_folders(vhost_id, bucket, pool_id) if vhost_id is not None else {}
    try:
        page = await s3client.list_objects_page(
            access.address, bucket, prefix=prefix, max_keys=limit, continuation_token=token, **access.auth
        )
    except Exception as exc:
        raise _unreachable(access, f"list bucket '{bucket}'", exc) from exc
    if page is None:
        raise HTTPException(404, f"Bucket '{bucket}' does not exist on pool '{access.pool['name']}'.")

    folders = set(page["folders"])
    if not token:
        # A routed folder is part of the bucket for clients even when this pool holds nothing
        # under it (no marker yet): show it where they would see it.
        folders.update(f for f in routed if f.startswith(prefix) and "/" not in f[len(prefix) : -1] and f != prefix)
    return {
        "pool": access.ref,
        "bucket": bucket,
        "prefix": prefix,
        "folders": [{"prefix": f, "routed_pool": routed.get(f)} for f in sorted(folders)],
        "objects": page["objects"],
        "next_token": page["next_token"],
    }


@router.get("/pools/{pool_id}/buckets/{bucket}/object")
async def object_details(pool_id: int, bucket: str = _BUCKET, key: str = Query(..., min_length=1, max_length=1024)):
    """Size, modification time, ETag and content type of one object (HEAD)."""
    access = await _access(pool_id)
    try:
        meta = await s3client.head_object(access.address, bucket, key, **access.auth)
    except Exception as exc:
        raise _unreachable(access, f"read '{bucket}/{key}'", exc) from exc
    if meta is None:
        raise HTTPException(404, f"'{bucket}/{key}' does not exist on pool '{access.pool['name']}'.")
    return {"pool": access.ref, "bucket": bucket, "key": key, **meta}


@router.get("/pools/{pool_id}/buckets/{bucket}/download")
async def download_object(pool_id: int, bucket: str = _BUCKET, key: str = Query(..., min_length=1, max_length=1024)):
    """The bytes of one object, as an attachment. Refused above MAX_DOWNLOAD_BYTES."""
    access = await _access(pool_id)
    too_large = HTTPException(
        413,
        f"'{bucket}/{key}' is larger than the panel's download limit "
        f"({MAX_DOWNLOAD_BYTES // (1024 * 1024)} MiB). Fetch it with an S3 client through the gateway.",
    )
    try:
        meta = await s3client.head_object(access.address, bucket, key, **access.auth)
        if meta is not None and (meta["size"] or 0) > MAX_DOWNLOAD_BYTES:
            raise too_large
        body = (
            None
            if meta is None
            else await s3client.get_object_capped(access.address, bucket, key, MAX_DOWNLOAD_BYTES, **access.auth)
        )
    except HTTPException:
        raise
    except s3client.ObjectTooLarge:
        raise too_large from None
    except Exception as exc:
        raise _unreachable(access, f"read '{bucket}/{key}'", exc) from exc
    if body is None:
        raise HTTPException(404, f"'{bucket}/{key}' does not exist on pool '{access.pool['name']}'.")

    async with get_db_ctx() as db:
        await log_audit(
            db,
            "download_object",
            "object",
            None,
            after={"pool_id": pool_id, "pool": access.pool["name"], "bucket": bucket, "key": key, "size": len(body)},
        )
        await db.commit()

    name = _download_name(key)
    original = urllib.parse.quote(key.rstrip("/").rsplit("/", 1)[-1], safe="")
    return Response(
        content=body,
        # Never the stored content type: an object must not be rendered in the panel's origin.
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename=\"{name}\"; filename*=UTF-8''{original}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@router.get("/vhosts/{vhost_id}/locate")
async def locate(
    vhost_id: int,
    bucket: str = Query(..., pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$", max_length=255),
    prefix: str = Query("", max_length=1024),
):
    """Which pool serves ``bucket`` (or ``prefix`` inside it) on this vhost, according to its
    routes: the most specific route wins, the default pool otherwise.

    While a migration has split routing active on that route, ``pool`` is the destination (it
    is authoritative) and ``split`` names both sides; otherwise ``split`` is null."""
    async with get_db_ctx() as db:
        vhosts = await db.execute_fetchall("SELECT id, server_name FROM vhosts WHERE id = ?", (vhost_id,))
        if not vhosts:
            raise HTTPException(404, "Vhost not found")
        rows = await db.execute_fetchall(
            """SELECT r.path_prefix, p.id, p.name, p.type FROM routes r JOIN pools p ON p.id = r.pool_id
               WHERE r.vhost_id = ? AND r.enabled = 1""",
            (vhost_id,),
        )
        splits = await _splits(db, vhost_id)
    target = f"/{bucket}/{prefix}"
    # "/" (the default pool) matches everything; "/photos/2025/" only what is under that folder
    matches = [r for r in rows if target.startswith(r["path_prefix"].rstrip("/") + "/")]
    if not matches:
        raise HTTPException(
            400, f"Vhost '{vhosts[0]['server_name']}' does not serve bucket '{bucket}': it has no route for it."
        )
    best = max(matches, key=lambda r: len(r["path_prefix"]))
    split = splits.get(best["path_prefix"].rstrip("/") + "/")
    pool = {"id": best["id"], "name": best["name"], "type": best["type"]}
    if split:
        pool = {**split["destination"], "type": best["type"]}
    return {
        "vhost": {"id": vhost_id, "server_name": vhosts[0]["server_name"]},
        "bucket": bucket,
        "prefix": prefix,
        "route": best["path_prefix"],
        "pool": pool,
        "split": split,
    }
