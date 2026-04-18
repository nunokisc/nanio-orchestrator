"""Tests for rclone migration API and engine."""

import os
import pytest
from unittest.mock import AsyncMock, patch
from tests.conftest import create_pool, create_member, create_vhost


@pytest.fixture(autouse=True)
def set_secret():
    """Set a test Fernet key for credential access."""
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    os.environ["NANIO_ORCHESTRATOR_SECRET"] = key
    import nanio_orchestrator.config as cfg_mod
    cfg_mod.settings = None
    import nanio_orchestrator.credentials as cred_mod
    cred_mod.reset_fernet()
    yield
    os.environ.pop("NANIO_ORCHESTRATOR_SECRET", None)
    cfg_mod.settings = None
    cred_mod.reset_fernet()


class TestMigrationsAPI:
    async def test_create_migration(self, client, mock_nginx, mock_rclone, mock_s3):
        src = await create_pool(client, "mig-src")
        await create_member(client, src["id"], "10.0.0.1:9000")
        dst = await create_pool(client, "mig-dst")
        await create_member(client, dst["id"], "10.0.0.2:9000")
        vh = await create_vhost(client, "mig.example.com", default_pool_id=src["id"])

        # Sync a bucket so bucket_sync has a record
        mock_s3["list_buckets"].return_value = [{"name": "mig-bucket", "created": "2025-01-01"}]
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")

        resp = await client.post("/api/migrations", json={
            "bucket": "mig-bucket",
            "src_pool_id": src["id"],
            "dst_pool_id": dst["id"],
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["bucket"] == "mig-bucket"
        assert data["phase"] == "pending"
        assert data["mode"] == "copy"  # default mode

    async def test_create_migration_sync_mode(self, client, mock_nginx, mock_rclone, mock_s3):
        src = await create_pool(client, "mig-sync-src")
        await create_member(client, src["id"], "10.0.0.1:9000")
        dst = await create_pool(client, "mig-sync-dst")
        await create_member(client, dst["id"], "10.0.0.2:9000")
        vh = await create_vhost(client, "mig-sync.example.com", default_pool_id=src["id"])

        mock_s3["list_buckets"].return_value = [{"name": "sync-bk", "created": "2025-01-01"}]
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")

        resp = await client.post("/api/migrations", json={
            "bucket": "sync-bk",
            "src_pool_id": src["id"],
            "dst_pool_id": dst["id"],
            "mode": "sync",
        })
        assert resp.status_code == 201
        assert resp.json()["mode"] == "sync"

    async def test_list_migrations(self, client):
        resp = await client.get("/api/migrations")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    async def test_get_migration_not_found(self, client):
        resp = await client.get("/api/migrations/99999")
        assert resp.status_code == 404

    async def test_cancel_migration_not_found(self, client):
        resp = await client.post("/api/migrations/99999/cancel")
        assert resp.status_code == 404

    async def test_duplicate_migration_rejected(self, client, mock_nginx, mock_rclone, mock_s3):
        src = await create_pool(client, "dup-mig-src")
        await create_member(client, src["id"], "10.0.0.1:9000")
        dst = await create_pool(client, "dup-mig-dst")
        await create_member(client, dst["id"], "10.0.0.2:9000")
        vh = await create_vhost(client, "dupmig.example.com", default_pool_id=src["id"])

        mock_s3["list_buckets"].return_value = [{"name": "dup-bk", "created": "2025-01-01"}]
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")

        # Create first migration
        resp1 = await client.post("/api/migrations", json={
            "bucket": "dup-bk",
            "src_pool_id": src["id"],
            "dst_pool_id": dst["id"],
        })
        assert resp1.status_code == 201

        # Second should be rejected (active migration exists)
        resp2 = await client.post("/api/migrations", json={
            "bucket": "dup-bk",
            "src_pool_id": src["id"],
            "dst_pool_id": dst["id"],
        })
        assert resp2.status_code == 409

    async def test_migration_log(self, client, mock_nginx, mock_rclone, mock_s3):
        src = await create_pool(client, "log-mig-src")
        await create_member(client, src["id"], "10.0.0.1:9000")
        dst = await create_pool(client, "log-mig-dst")
        await create_member(client, dst["id"], "10.0.0.2:9000")
        vh = await create_vhost(client, "logmig.example.com", default_pool_id=src["id"])

        mock_s3["list_buckets"].return_value = [{"name": "log-bk", "created": "2025-01-01"}]
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")

        resp = await client.post("/api/migrations", json={
            "bucket": "log-bk",
            "src_pool_id": src["id"],
            "dst_pool_id": dst["id"],
        })
        mig_id = resp.json()["id"]

        # Give it a moment to write log entries
        import asyncio
        await asyncio.sleep(0.2)

        log_resp = await client.get(f"/api/migrations/{mig_id}/log")
        assert log_resp.status_code == 200
