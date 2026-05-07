"""Pool credential encryption/decryption using Fernet (symmetric encryption).

Requires NANIO_ORCHESTRATOR_SECRET to be set (a valid Fernet key, 32 bytes
URL-safe base64-encoded).  Generate one with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from cryptography.fernet import Fernet, InvalidToken

from nanio_orchestrator.config import get_settings
from nanio_orchestrator.db import get_db_ctx

logger = logging.getLogger(__name__)

_fernet: Optional[Fernet] = None


def _get_fernet() -> Fernet:
    """Return a cached Fernet instance, or raise if secret is not configured."""
    global _fernet
    if _fernet is None:
        secret = get_settings().secret
        if not secret:
            raise RuntimeError(
                "NANIO_ORCHESTRATOR_SECRET is not set. "
                "Generate one with: python -c "
                '"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
            )
        _fernet = Fernet(secret.encode("utf-8"))
    return _fernet


def reset_fernet() -> None:
    """Reset the cached Fernet instance (for testing)."""
    global _fernet
    _fernet = None


def encrypt(plaintext: str) -> str:
    """Encrypt a string → URL-safe base64 token."""
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(token: str) -> str:
    """Decrypt a Fernet token → plaintext string."""
    try:
        return _get_fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        raise ValueError("Failed to decrypt credential — secret key may have changed")


async def store_pool_credentials(
    pool_id: int,
    access_key: str,
    secret_key: str,
    endpoint_url: Optional[str] = None,
    region: str = "us-east-1",
) -> Tuple[str, str]:
    """Encrypt and store credentials for a pool.

    Returns (access_key_enc, secret_key_enc) so callers can use the encrypted
    values directly (e.g. for sidecar writes) without a second DB round-trip.
    """
    access_enc = encrypt(access_key)
    secret_enc = encrypt(secret_key)

    async with get_db_ctx() as db:
        await db.execute(
            """INSERT INTO pool_credentials
               (pool_id, access_key_enc, secret_key_enc, endpoint_url, region)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(pool_id) DO UPDATE SET
                 access_key_enc = excluded.access_key_enc,
                 secret_key_enc = excluded.secret_key_enc,
                 endpoint_url   = excluded.endpoint_url,
                 region         = excluded.region,
                 updated_at     = datetime('now')""",
            (pool_id, access_enc, secret_enc, endpoint_url, region),
        )
        await db.commit()

    return access_enc, secret_enc


async def get_pool_credentials(pool_id: int) -> Optional[dict]:
    """Retrieve and decrypt credentials for a pool. Returns None if not set."""
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT * FROM pool_credentials WHERE pool_id = ?", (pool_id,))
    if not rows:
        return None

    row = dict(rows[0])
    return {
        "pool_id": row["pool_id"],
        "access_key": decrypt(row["access_key_enc"]),
        "secret_key": decrypt(row["secret_key_enc"]),
        "endpoint_url": row["endpoint_url"],
        "region": row["region"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


async def delete_pool_credentials(pool_id: int) -> bool:
    """Delete credentials for a pool. Returns True if a row was deleted."""
    async with get_db_ctx() as db:
        cursor = await db.execute("DELETE FROM pool_credentials WHERE pool_id = ?", (pool_id,))
        await db.commit()
        return cursor.rowcount > 0


async def get_pool_s3_params(pool_id: int) -> Tuple[Optional[str], Optional[str], str]:
    """Get (access_key, secret_key, region) for S3 operations against a pool.

    Falls back to global s3_access_key / s3_secret_key from config if no
    per-pool credentials are stored.
    """
    creds = await get_pool_credentials(pool_id)
    if creds:
        return creds["access_key"], creds["secret_key"], creds["region"]

    s = get_settings()
    return s.s3_access_key, s.s3_secret_key, "us-east-1"
