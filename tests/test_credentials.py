"""Tests for pool credentials API (Fernet encryption)."""

import os
import pytest
from unittest.mock import patch
from tests.conftest import create_pool


@pytest.fixture(autouse=True)
def set_secret():
    """Set a test Fernet key."""
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    os.environ["NANIO_ORCHESTRATOR_SECRET"] = key

    # Reset config and fernet singletons
    import nanio_orchestrator.config as cfg_mod
    cfg_mod.settings = None
    import nanio_orchestrator.credentials as cred_mod
    cred_mod.reset_fernet()

    yield

    os.environ.pop("NANIO_ORCHESTRATOR_SECRET", None)
    cfg_mod.settings = None
    cred_mod.reset_fernet()


class TestCredentials:
    async def test_set_credentials(self, client):
        pool = await create_pool(client, "cred-pool")
        resp = await client.put(f"/api/pools/{pool['id']}/credentials", json={
            "access_key": "AKIAIOSFODNN7EXAMPLE",
            "secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "region": "eu-west-1",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["pool_id"] == pool["id"]
        assert data["region"] == "eu-west-1"
        # Key should be masked
        assert data["access_key_masked"].startswith("AKIA")
        assert "****" in data["access_key_masked"]

    async def test_get_credentials(self, client):
        pool = await create_pool(client, "get-cred-pool")
        await client.put(f"/api/pools/{pool['id']}/credentials", json={
            "access_key": "TESTKEY123",
            "secret_key": "TESTSECRET456",
        })
        resp = await client.get(f"/api/pools/{pool['id']}/credentials")
        assert resp.status_code == 200
        assert resp.json()["access_key_masked"].startswith("TEST")

    async def test_get_credentials_not_found(self, client):
        # No pool-specific creds — falls back to global credentials (source='global')
        pool = await create_pool(client, "nocred-pool")
        resp = await client.get(f"/api/pools/{pool['id']}/credentials")
        assert resp.status_code == 200
        assert resp.json()["source"] == "global"

    async def test_delete_credentials(self, client):
        pool = await create_pool(client, "delcred-pool")
        await client.put(f"/api/pools/{pool['id']}/credentials", json={
            "access_key": "KEY", "secret_key": "SECRET",
        })
        resp = await client.delete(f"/api/pools/{pool['id']}/credentials")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        # Pool-specific creds deleted — falls back to global credentials (source='global')
        resp = await client.get(f"/api/pools/{pool['id']}/credentials")
        assert resp.status_code == 200
        assert resp.json()["source"] == "global"

    async def test_delete_credentials_not_found(self, client):
        pool = await create_pool(client, "delcred-nf-pool")
        resp = await client.delete(f"/api/pools/{pool['id']}/credentials")
        assert resp.status_code == 404

    async def test_update_credentials(self, client):
        pool = await create_pool(client, "updcred-pool")
        await client.put(f"/api/pools/{pool['id']}/credentials", json={
            "access_key": "OLD", "secret_key": "SECRET",
        })
        resp = await client.put(f"/api/pools/{pool['id']}/credentials", json={
            "access_key": "NEW_KEY_LONG", "secret_key": "NEW_SECRET",
            "endpoint_url": "http://s3.custom.com",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["access_key_masked"].startswith("NEW_")
        assert data["endpoint_url"] == "http://s3.custom.com"

    async def test_credentials_pool_not_found(self, client):
        resp = await client.get("/api/pools/99999/credentials")
        assert resp.status_code == 404

    async def test_credentials_encrypted_in_db(self, client, db):
        pool = await create_pool(client, "enc-pool")
        await client.put(f"/api/pools/{pool['id']}/credentials", json={
            "access_key": "PLAINTEXT_KEY",
            "secret_key": "PLAINTEXT_SECRET",
        })
        # Check DB directly — values should be encrypted
        rows = await db.execute_fetchall(
            "SELECT access_key_enc, secret_key_enc FROM pool_credentials WHERE pool_id = ?",
            (pool["id"],),
        )
        assert len(rows) == 1
        assert rows[0]["access_key_enc"] != "PLAINTEXT_KEY"
        assert rows[0]["secret_key_enc"] != "PLAINTEXT_SECRET"
