"""Pool credentials API — encrypt-at-rest S3 credentials per pool.

Endpoints:
  GET    /api/pools/{pool_id}/credentials
  PUT    /api/pools/{pool_id}/credentials
  DELETE /api/pools/{pool_id}/credentials
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from nanio_orchestrator.credentials import (
    delete_pool_credentials,
    get_pool_credentials,
    store_pool_credentials,
)
from nanio_orchestrator.db import get_db_ctx
from nanio_orchestrator.models import CredentialOut, CredentialSet
from nanio_orchestrator.sidecar import (
    write_pool_credentials_sidecar,
    delete_pool_credentials_sidecar,
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


@router.get("/{pool_id}/credentials", response_model=CredentialOut)
async def get_credentials(pool_id: int):
    """Retrieve credentials for a pool (access_key is masked)."""
    await _require_pool(pool_id)
    creds = await get_pool_credentials(pool_id)
    if not creds:
        raise HTTPException(404, "No credentials stored for this pool")
    return CredentialOut(
        pool_id=creds["pool_id"],
        access_key_masked=_mask(creds["access_key"]),
        endpoint_url=creds["endpoint_url"],
        region=creds["region"],
        created_at=creds["created_at"],
        updated_at=creds["updated_at"],
    )


@router.put("/{pool_id}/credentials", response_model=CredentialOut)
async def set_credentials(pool_id: int, body: CredentialSet):
    """Store (or replace) encrypted S3 credentials for a pool."""
    pool = await _require_pool(pool_id)
    try:
        await store_pool_credentials(
            pool_id,
            access_key=body.access_key,
            secret_key=body.secret_key,
            endpoint_url=body.endpoint_url,
            region=body.region,
        )
    except RuntimeError as e:
        raise HTTPException(500, str(e))

    # Update sidecar with encrypted credentials
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall(
            "SELECT access_key_enc, secret_key_enc FROM pool_credentials WHERE pool_id = ?",
            (pool_id,),
        )
    if rows:
        row = dict(rows[0])
        write_pool_credentials_sidecar(
            pool["name"], row["access_key_enc"], row["secret_key_enc"],
            body.endpoint_url, body.region,
        )

    creds = await get_pool_credentials(pool_id)
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
    pool = await _require_pool(pool_id)
    deleted = await delete_pool_credentials(pool_id)
    if not deleted:
        raise HTTPException(404, "No credentials stored for this pool")

    # Remove credentials from sidecar
    delete_pool_credentials_sidecar(pool["name"])

    return {"ok": True, "pool_id": pool_id}
