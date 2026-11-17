"""Shared by the API modules."""

from __future__ import annotations

from typing import Optional

from fastapi import HTTPException

from nanio_orchestrator import s3client
from nanio_orchestrator.credentials import get_pool_s3_params
from nanio_orchestrator.subfolders import pool_endpoint


async def reject_if_refused(db, ok: bool, output: str) -> None:
    """Undo a change that nginx refused, and tell the caller.

    Endpoints make their changes on ``db`` without committing, generate the config from that
    uncommitted state and let nginx test it. Accepted: the write helper commits. Refused: the
    previous file is already back in place, and this rolls the database back too, so the
    database, the files and nginx keep describing the same thing.
    """
    if ok:
        return
    await db.rollback()
    raise HTTPException(422, f"nginx rejected this change, so nothing was saved. nginx said: {output}")


async def refuse_to_leave_objects_behind(
    db, bucket: str, leaving_pool_id: Optional[int], going_pool_id: Optional[int], *, allow_orphan: bool, change: str
) -> None:
    """Refuse a routing change that takes a whole bucket away from the pool that holds it.

    ``change`` sends the requests for ``bucket`` from pool ``leaving_pool_id`` to pool
    ``going_pool_id`` without moving anything. If the bucket has objects on the pool it leaves
    they become unreachable: 409. If that pool cannot be asked: 502, because "cannot tell" is
    not "empty". ``allow_orphan`` is the explicit way through both. A bucket that does not
    exist on the pool has no objects. Pools without an S3 API (http) are not asked.
    """
    if allow_orphan or not leaving_pool_id or leaving_pool_id == going_pool_id:
        return
    rows = await db.execute_fetchall("SELECT name, type FROM pools WHERE id = ?", (leaving_pool_id,))
    if not rows or rows[0]["type"] != "nanio":
        return
    pool_name = rows[0]["name"]
    address = await pool_endpoint(db, leaving_pool_id)

    has_objects = False
    unknown: Optional[str] = None
    if not address:
        unknown = "the pool has no enabled member"
    else:
        access_key, secret_key, _ = await get_pool_s3_params(leaving_pool_id)
        try:
            has_objects = await s3client.bucket_has_objects(
                address, bucket, access_key=access_key, secret_key=secret_key
            )
        except Exception as exc:
            unknown = f"the pool could not be asked ({exc})"

    if unknown:
        raise HTTPException(
            502,
            f"Cannot tell whether bucket '{bucket}' has objects on pool '{pool_name}': {unknown}. {change} would "
            "leave them behind. Bring the pool back, or send allow_orphan=true to do it anyway.",
        )
    if has_objects:
        raise HTTPException(
            409,
            f"Bucket '{bucket}' has objects on pool '{pool_name}'. {change} would leave them there, no longer "
            "reachable through this vhost. Move the bucket with a migration instead, or send allow_orphan=true "
            "to leave them behind.",
        )
