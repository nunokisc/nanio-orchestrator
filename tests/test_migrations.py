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

    async def test_same_pool_migration_rejected(self, client, mock_nginx, mock_rclone, mock_s3):
        """Migrating a bucket to the same pool must be rejected at the API layer."""
        pool = await create_pool(client, "same-pool")
        await create_member(client, pool["id"], "10.0.0.1:9000")
        vh = await create_vhost(client, "samepool.example.com", default_pool_id=pool["id"])

        mock_s3["list_buckets"].return_value = [{"name": "same-bk", "created": "2025-01-01"}]
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")

        resp = await client.post("/api/migrations", json={
            "bucket": "same-bk",
            "src_pool_id": pool["id"],
            "dst_pool_id": pool["id"],
        })
        assert resp.status_code == 400
        assert "same pool" in resp.json()["detail"].lower()

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


class TestMigrationDstPrecondition:
    """Destination bucket pre-condition checks before copying starts."""

    async def _setup(self, client, mock_nginx, mock_s3, src_name, dst_name, vh_name, bucket_name):
        src = await create_pool(client, src_name)
        await create_member(client, src["id"], "10.0.0.1:9000")
        dst = await create_pool(client, dst_name)
        await create_member(client, dst["id"], "10.0.0.2:9000")
        vh = await create_vhost(client, vh_name, default_pool_id=src["id"])
        mock_s3["list_buckets"].return_value = [{"name": bucket_name, "created": "2025-01-01"}]
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")
        return src, dst, vh

    async def test_migration_refused_when_dst_has_objects(
        self, client, mock_nginx, mock_rclone, mock_s3
    ):
        """Migration must fail immediately when the destination bucket already has objects."""
        src, dst, vh = await self._setup(
            client, mock_nginx, mock_s3,
            "pre-src1", "pre-dst1", "pre1.example.com", "pre-bk1",
        )
        # dst bucket exists AND has objects — should refuse
        mock_s3["bucket_exists"].return_value = True
        mock_s3["bucket_has_objects"].return_value = True

        resp = await client.post("/api/migrations", json={
            "bucket": "pre-bk1",
            "src_pool_id": src["id"],
            "dst_pool_id": dst["id"],
        })
        assert resp.status_code == 201
        mig_id = resp.json()["id"]

        import asyncio
        deadline = asyncio.get_event_loop().time() + 10.0
        while asyncio.get_event_loop().time() < deadline:
            r = await client.get(f"/api/migrations/{mig_id}")
            if r.json()["phase"] in {"error", "done", "cancelled"}:
                break
            await asyncio.sleep(0.05)

        final = (await client.get(f"/api/migrations/{mig_id}")).json()
        assert final["phase"] == "error"
        assert "destination bucket already contains objects" in (final["error_msg"] or "").lower()

    async def test_migration_accepted_dst_exists_but_empty(
        self, client, mock_nginx, mock_rclone, mock_s3
    ):
        """Migration proceeds when destination bucket exists but is empty."""
        src, dst, vh = await self._setup(
            client, mock_nginx, mock_s3,
            "pre-src2", "pre-dst2", "pre2.example.com", "pre-bk2",
        )
        # dst exists but is empty
        mock_s3["bucket_exists"].return_value = True
        mock_s3["bucket_has_objects"].return_value = False
        mock_s3["count_objects"].return_value = 5  # src has objects

        resp = await client.post("/api/migrations", json={
            "bucket": "pre-bk2",
            "src_pool_id": src["id"],
            "dst_pool_id": dst["id"],
        })
        assert resp.status_code == 201
        mig_id = resp.json()["id"]

        import asyncio
        deadline = asyncio.get_event_loop().time() + 10.0
        while asyncio.get_event_loop().time() < deadline:
            r = await client.get(f"/api/migrations/{mig_id}")
            if r.json()["phase"] in {"done", "error", "cancelled"}:
                break
            await asyncio.sleep(0.05)

        # Should not error on pre-condition — may complete or error for other reasons
        final = (await client.get(f"/api/migrations/{mig_id}")).json()
        assert final["phase"] != "error" or "destination bucket already contains objects" not in (
            final.get("error_msg") or ""
        )

    async def test_migration_creates_dst_bucket_when_missing(
        self, client, mock_nginx, mock_rclone, mock_s3
    ):
        """Migration creates the destination bucket when it doesn't exist."""
        src, dst, vh = await self._setup(
            client, mock_nginx, mock_s3,
            "pre-src3", "pre-dst3", "pre3.example.com", "pre-bk3",
        )
        # dst bucket does not exist
        mock_s3["bucket_exists"].return_value = False
        mock_s3["create_bucket"].return_value = (True, "created")
        mock_s3["count_objects"].return_value = 5

        resp = await client.post("/api/migrations", json={
            "bucket": "pre-bk3",
            "src_pool_id": src["id"],
            "dst_pool_id": dst["id"],
        })
        assert resp.status_code == 201
        mig_id = resp.json()["id"]

        import asyncio
        await asyncio.sleep(0.3)

        # create_bucket must have been called for the destination
        mock_s3["create_bucket"].assert_called()

        log_resp = await client.get(f"/api/migrations/{mig_id}/log")
        log_msgs = [e["message"] for e in log_resp.json()]
        assert any("created destination bucket" in m.lower() for m in log_msgs), \
            f"No creation log: {log_msgs}"


class TestMigrationOrphanedTracking:
    """Orphaned source data tracking after migration completes."""

    async def _run_full_migration(self, client, mock_nginx, mock_rclone, mock_s3,
                                   src_name, dst_name, vh_name, bucket_name):
        src = await create_pool(client, src_name)
        await create_member(client, src["id"], "10.0.0.1:9000")
        dst = await create_pool(client, dst_name)
        await create_member(client, dst["id"], "10.0.0.2:9000")
        vh = await create_vhost(client, vh_name, default_pool_id=src["id"])

        mock_s3["list_buckets"].return_value = [{"name": bucket_name, "created": "2025-01-01"}]
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")

        mock_s3["count_objects"].return_value = 5  # converge immediately (same on src/dst)

        resp = await client.post("/api/migrations", json={
            "bucket": bucket_name,
            "src_pool_id": src["id"],
            "dst_pool_id": dst["id"],
        })
        assert resp.status_code == 201
        mig_id = resp.json()["id"]

        import asyncio
        deadline = asyncio.get_event_loop().time() + 10.0
        while asyncio.get_event_loop().time() < deadline:
            r = await client.get(f"/api/migrations/{mig_id}")
            if r.json()["phase"] in {"done", "error", "cancelled"}:
                break
            await asyncio.sleep(0.05)

        return mig_id, src, dst, vh

    async def test_orphaned_fields_set_after_done(
        self, client, mock_nginx, mock_rclone, mock_s3
    ):
        """After migration completes, orphaned_source_pool_id/prefix/at must be set."""
        mig_id, src, dst, vh = await self._run_full_migration(
            client, mock_nginx, mock_rclone, mock_s3,
            "orp-src1", "orp-dst1", "orp1.example.com", "orp-bk1",
        )
        final = (await client.get(f"/api/migrations/{mig_id}")).json()
        assert final["phase"] == "done", f"Migration did not reach done: {final}"
        assert final["orphaned_source_pool_id"] == src["id"]
        assert final["orphaned_source_prefix"] == "/orp-bk1/"
        assert final["orphaned_at"] is not None

    async def test_orphaned_endpoint_returns_entries(
        self, client, mock_nginx, mock_rclone, mock_s3
    ):
        """GET /api/migrations/orphaned lists completed migrations with orphaned data."""
        mig_id, src, dst, vh = await self._run_full_migration(
            client, mock_nginx, mock_rclone, mock_s3,
            "orp-src2", "orp-dst2", "orp2.example.com", "orp-bk2",
        )
        final = (await client.get(f"/api/migrations/{mig_id}")).json()
        if final["phase"] != "done":
            return  # skip if migration didn't reach done

        resp = await client.get("/api/migrations/orphaned")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        ids = [e["migration_id"] for e in data]
        assert mig_id in ids

        entry = next(e for e in data if e["migration_id"] == mig_id)
        assert entry["orphaned_source_pool_id"] == src["id"]
        assert entry["orphaned_source_prefix"] == "/orp-bk2/"
        assert entry["orphaned_at"] is not None

    async def test_state_machine_completes_without_purge(
        self, client, mock_nginx, mock_rclone, mock_s3
    ):
        """Full state machine: pending → copying → verifying → switching → done (no purge phase)."""
        mig_id, src, dst, vh = await self._run_full_migration(
            client, mock_nginx, mock_rclone, mock_s3,
            "orp-src3", "orp-dst3", "orp3.example.com", "orp-bk3",
        )
        final = (await client.get(f"/api/migrations/{mig_id}")).json()
        assert final["phase"] == "done", f"Expected done, got: {final['phase']}"

        # No log entry should mention purge_source
        log_resp = await client.get(f"/api/migrations/{mig_id}/log")
        log_msgs = [e["message"] for e in log_resp.json()]
        assert not any("purge_source" in m for m in log_msgs), \
            f"Unexpected purge_source in log: {log_msgs}"

    async def test_orphaned_endpoint_empty_when_no_migrations(self, client):
        """GET /api/migrations/orphaned returns empty list when no migrations have completed."""
        resp = await client.get("/api/migrations/orphaned")
        assert resp.status_code == 200
        assert resp.json() == []


class TestMigrationRecovery:
    """Crash recovery does not attempt purge."""

    async def test_recovery_does_not_purge(self):
        """recover_interrupted_migrations must not reference purge phases."""
        import inspect
        from nanio_orchestrator.migration_engine import recover_interrupted_migrations
        src = inspect.getsource(recover_interrupted_migrations)
        assert "purge" not in src.lower(), \
            "recover_interrupted_migrations must not reference purge"
        assert "needs_purge" not in src, \
            "recover_interrupted_migrations must not reference needs_purge"
