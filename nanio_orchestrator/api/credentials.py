"""Pool credentials API — encrypt-at-rest S3 credentials per pool.

Endpoints:
  GET    /api/pools/{pool_id}/credentials
  PUT    /api/pools/{pool_id}/credentials
  DELETE /api/pools/{pool_id}/credentials
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from nanio_orchestrator.audit_log import log_audit
from nanio_orchestrator.config import get_settings
from nanio_orchestrator.credentials import (
    delete_pool_credentials,
    get_pool_credentials,
    store_pool_credentials,
)
from nanio_orchestrator.db import get_db_ctx
from nanio_orchestrator.models import CredentialOut, CredentialSet
from nanio_orchestrator.sidecar import (
    delete_pool_credentials_sidecar,
    write_pool_credentials_sidecar,
)

router = APIRouter(prefix="/api/pools", tags=["credentials"])
logger = logging.getLogger(__name__)


def _mask(key: str) -> str:
    """Show only first 4 chars of a secret."""
    if len(key) <= 4:
        return "****"
    return key[:4] + "*" * (len(key) - 4)


async def _require_pool(pool_id: int) -> dict:
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT * FROM pools WHERE id = ?", (pool_id,))
    if not rows:
        raise HTTPException(404, "Pool not found")
    return dict(rows[0])


async def _require_nanio_pool(pool_id: int) -> dict:
    """Require pool exists and is of type 'nanio'. S3 credentials are nanio-only."""
    pool = await _require_pool(pool_id)
    if pool["type"] != "nanio":
        raise HTTPException(
            400,
            f"S3 credentials are only supported for nanio pools. Pool '{pool['name']}' is of type '{pool['type']}'.",
        )
    return pool


@router.get("/{pool_id}/credentials", response_model=CredentialOut)
async def get_credentials(pool_id: int):
    """Return effective credentials for a pool.

    If pool-specific credentials are stored, returns those (masked).
    Otherwise falls back to the global S3_ACCESS_KEY / S3_SECRET_KEY from settings,
    with source='global' to indicate no pool-specific override is set.
    """
    await _require_nanio_pool(pool_id)
    creds = await get_pool_credentials(pool_id)
    if creds:
        return CredentialOut(
            pool_id=pool_id,
            access_key_masked=_mask(creds["access_key"]),
            endpoint_url=creds["endpoint_url"],
            region=creds["region"],
            source="pool",
            created_at=creds["created_at"],
            updated_at=creds["updated_at"],
        )

    s = get_settings()
    return CredentialOut(
        pool_id=pool_id,
        access_key_masked=_mask(s.s3_access_key or "") if s.s3_access_key else "(not set)",
        endpoint_url=None,
        region="us-east-1",
        source="global",
    )


@router.put("/{pool_id}/credentials", response_model=CredentialOut)
async def set_credentials(pool_id: int, body: CredentialSet):
    """Store (or replace) encrypted S3 credentials for a pool."""
    pool = await _require_nanio_pool(pool_id)
    try:
        access_key_enc, secret_key_enc = await store_pool_credentials(
            pool_id,
            access_key=body.access_key,
            secret_key=body.secret_key,
            endpoint_url=body.endpoint_url,
            region=body.region,
        )
    except RuntimeError as e:
        raise HTTPException(500, str(e))

    write_pool_credentials_sidecar(
        pool["name"],
        access_key_enc,
        secret_key_enc,
        body.endpoint_url,
        body.region,
    )

    creds = await get_pool_credentials(pool_id)
    async with get_db_ctx() as db:
        await log_audit(
            db, "set_credentials", "pool", pool_id, after={"endpoint_url": body.endpoint_url, "region": body.region}
        )
        await db.commit()
    return CredentialOut(
        pool_id=creds["pool_id"],
        access_key_masked=_mask(creds["access_key"]),
        endpoint_url=creds["endpoint_url"],
        region=creds["region"],
        created_at=creds["created_at"],
        updated_at=creds["updated_at"],
    )


@router.delete("/{pool_id}/credentials")
async def remove_credentials(pool_id: int):
    """Delete stored credentials for a pool."""
    pool = await _require_nanio_pool(pool_id)
    deleted = await delete_pool_credentials(pool_id)
    if not deleted:
        raise HTTPException(404, "No credentials stored for this pool")

    delete_pool_credentials_sidecar(pool["name"])

    async with get_db_ctx() as db:
        await log_audit(db, "remove_credentials", "pool", pool_id)
        await db.commit()
    return {"ok": True, "pool_id": pool_id}
