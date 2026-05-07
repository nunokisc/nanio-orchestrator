"""Tests for bucket sync functionality."""

import pytest
from unittest.mock import AsyncMock, patch
from tests.conftest import create_pool, create_member, create_vhost


class TestBucketSync:
    async def test_sync_vhost_no_default_pool(self, client):
        vh = await create_vhost(client, "nosync.example.com")
        resp = await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")
        data = resp.json()
        assert data.get("skipped") is True

    async def test_sync_vhost_with_buckets(self, client, mock_s3, mock_nginx):
        pool = await create_pool(client, "sync-pool")
        await create_member(client, pool["id"], "10.0.0.1:9000")
        vh = await create_vhost(client, "sync.example.com", default_pool_id=pool["id"])

        mock_s3["list_buckets"].return_value = [
            {"name": "photos", "created": "2025-01-01"},
            {"name": "videos", "created": "2025-01-02"},
        ]

        resp = await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")
        data = resp.json()
        assert data["buckets_found"] == 2

    async def test_list_buckets(self, client, mock_s3, mock_nginx):
        pool = await create_pool(client, "list-bk-pool")
        await create_member(client, pool["id"], "10.0.0.1:9000")
        vh = await create_vhost(client, "listbk.example.com", default_pool_id=pool["id"])

        mock_s3["list_buckets"].return_value = [
            {"name": "bucket1", "created": "2025-01-01"},
        ]
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")

        resp = await client.get(f"/api/vhosts/{vh['id']}/buckets")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["buckets"]) == 1
        assert data["buckets"][0]["name"] == "bucket1"
        assert data["buckets"][0]["status"] == "unrouted"

    async def test_ignore_bucket(self, client, mock_s3, mock_nginx):
        pool = await create_pool(client, "ign-pool")
        await create_member(client, pool["id"], "10.0.0.1:9000")
        vh = await create_vhost(client, "ign.example.com", default_pool_id=pool["id"])

        mock_s3["list_buckets"].return_value = [
            {"name": "ignore-me", "created": "2025-01-01"},
        ]
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")

        resp = await client.post(f"/api/vhosts/{vh['id']}/buckets/ignore-me/ignore")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    async def test_promote_bucket(self, client, mock_s3, mock_nginx):
        src_pool = await create_pool(client, "prom-src")
        await create_member(client, src_pool["id"], "10.0.0.1:9000")
        dst_pool = await create_pool(client, "prom-dst")
        await create_member(client, dst_pool["id"], "10.0.0.2:9000")
        vh = await create_vhost(client, "prom.example.com", default_pool_id=src_pool["id"])

        mock_s3["list_buckets"].return_value = [
            {"name": "promoted", "created": "2025-01-01"},
        ]
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")

        resp = await client.post(f"/api/vhosts/{vh['id']}/buckets/promoted/promote", json={
            "pool_id": dst_pool["id"],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["route"] == "/promoted/"

    async def test_promote_bucket_with_migrate(self, client, mock_s3, mock_nginx, mock_rclone):
        """promote with migrate=True should start a rclone migration.

        Crucially, the nginx route must initially point to the SOURCE (default)
        pool so users keep seeing their files while the copy runs.  The migration
        engine's 'switching' phase will update the route to the destination once
        the copy is verified.
        """
        src_pool = await create_pool(client, "prom-mig-src")
        await create_member(client, src_pool["id"], "10.0.0.1:9000")
        dst_pool = await create_pool(client, "prom-mig-dst")
        await create_member(client, dst_pool["id"], "10.0.0.2:9000")
        vh = await create_vhost(client, "prom-mig.example.com", default_pool_id=src_pool["id"])

        mock_s3["list_buckets"].return_value = [{"name": "big-bucket", "created": "2025-01-01"}]
        mock_s3["count_objects"].return_value = 5  # non-empty source
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")

        resp = await client.post(f"/api/vhosts/{vh['id']}/buckets/big-bucket/promote", json={
            "pool_id": dst_pool["id"],
            "migrate": True,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["migration_started"] is True
        assert "migration_id" in data

        # Verify migration record exists with correct src/dst
        mig_resp = await client.get(f"/api/migrations/{data['migration_id']}")
        assert mig_resp.status_code == 200
        mig = mig_resp.json()
        assert mig["bucket"] == "big-bucket"
        assert mig["src_pool_id"] == src_pool["id"]
        assert mig["dst_pool_id"] == dst_pool["id"]

        # The route must point to the SOURCE pool while migration is in progress
        # so users keep seeing their files until the 'switching' phase cuts over.
        routes_resp = await client.get(f"/api/vhosts/{vh['id']}/routes")
        assert routes_resp.status_code == 200
        route = next(r for r in routes_resp.json() if r["path_prefix"] == "/big-bucket/")
        assert route["pool_id"] == src_pool["id"], (
            "Route should point to SOURCE pool during migration — "
            "users must not see files disappear while copy is running"
        )

    async def test_promote_with_migrate_passes_route_id(
        self, client, mock_s3, mock_nginx, mock_rclone
    ):
        """promote(migrate=True) must bind route_id so switching is precise."""
        src = await create_pool(client, "routeid-src")
        await create_member(client, src["id"], "10.0.0.1:9000")
        dst = await create_pool(client, "routeid-dst")
        await create_member(client, dst["id"], "10.0.0.2:9000")
        vh = await create_vhost(client, "routeid.example.com", default_pool_id=src["id"])

        mock_s3["list_buckets"].return_value = [{"name": "rb-bucket", "created": "2025-01-01"}]
        mock_s3["count_objects"].return_value = 3
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")

        resp = await client.post(
            f"/api/vhosts/{vh['id']}/buckets/rb-bucket/promote",
            json={"pool_id": dst["id"], "migrate": True},
        )
        assert resp.status_code == 200
        mig_id = resp.json()["migration_id"]

        from nanio_orchestrator.db import get_db_ctx
        async with get_db_ctx() as db:
            rows = await db.execute_fetchall(
                "SELECT route_id FROM migrations WHERE id = ?", (mig_id,)
            )
        assert rows[0]["route_id"] is not None, \
            "migration record must have route_id bound so switching is precise"

    async def test_promote_without_migrate_blocked_when_bucket_has_data(
        self, client, mock_s3, mock_nginx
    ):
        """Routing a bucket that has data without migrate=True must be refused (data loss prevention)."""
        src = await create_pool(client, "block-src")
        await create_member(client, src["id"], "10.0.0.1:9000")
        dst = await create_pool(client, "block-dst")
        await create_member(client, dst["id"], "10.0.0.2:9000")
        vh = await create_vhost(client, "block.example.com", default_pool_id=src["id"])

        mock_s3["list_buckets"].return_value = [{"name": "full-bucket", "created": "2025-01-01"}]
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")

        mock_s3["promote_src_has_objects"].return_value = True

        resp = await client.post(
            f"/api/vhosts/{vh['id']}/buckets/full-bucket/promote",
            json={"pool_id": dst["id"], "migrate": False},
        )
        assert resp.status_code == 400
        assert "migrate" in resp.json()["detail"].lower()

    async def test_promote_without_migrate_allowed_when_bucket_is_empty(
        self, client, mock_s3, mock_nginx
    ):
        """Routing an empty bucket without migrate=True is allowed."""
        src = await create_pool(client, "empty-src")
        await create_member(client, src["id"], "10.0.0.1:9000")
        dst = await create_pool(client, "empty-dst")
        await create_member(client, dst["id"], "10.0.0.2:9000")
        vh = await create_vhost(client, "empty.example.com", default_pool_id=src["id"])

        mock_s3["list_buckets"].return_value = [{"name": "new-bucket", "created": "2025-01-01"}]
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")

        # Default: promote_src_has_objects = False (empty bucket)
        resp = await client.post(
            f"/api/vhosts/{vh['id']}/buckets/new-bucket/promote",
            json={"pool_id": dst["id"], "migrate": False},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


class TestMigrationRouteValidation:
    """Migrations API must enforce route existence and src pool consistency."""

    async def _setup(self, client, mock_nginx, mock_s3, src_name, dst_name, vh_name):
        src = await create_pool(client, src_name)
        await create_member(client, src["id"], "10.0.0.1:9000")
        dst = await create_pool(client, dst_name)
        await create_member(client, dst["id"], "10.0.0.2:9000")
        vh = await create_vhost(client, vh_name, default_pool_id=src["id"])
        return src, dst, vh

    async def test_migration_rejected_when_no_route_exists(
        self, client, mock_nginx, mock_s3
    ):
        """create_migration must fail if no route exists for the bucket."""
        src, dst, vh = await self._setup(
            client, mock_nginx, mock_s3, "rv-src1", "rv-dst1", "rv1.example.com"
        )
        mock_s3["list_buckets"].return_value = [{"name": "no-route-bk", "created": "2025-01-01"}]
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")

        resp = await client.post("/api/migrations", json={
            "bucket": "no-route-bk",
            "src_pool_id": src["id"],
            "dst_pool_id": dst["id"],
        })
        assert resp.status_code == 400
        assert "route" in resp.json()["detail"].lower()

    async def test_migration_rejected_when_src_pool_mismatches_route(
        self, client, mock_nginx, mock_s3, mock_rclone
    ):
        """create_migration must fail if src_pool_id != route's current pool."""
        src, dst, vh = await self._setup(
            client, mock_nginx, mock_s3, "rv-src2", "rv-dst2", "rv2.example.com"
        )
        other = await create_pool(client, "rv-other2")
        await create_member(client, other["id"], "10.0.0.3:9000")

        mock_s3["list_buckets"].return_value = [{"name": "mismatch-bk", "created": "2025-01-01"}]
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")

        # Promote bucket (empty) -> route points to dst
        mock_s3["promote_src_has_objects"].return_value = False
        await client.post(
            f"/api/vhosts/{vh['id']}/buckets/mismatch-bk/promote",
            json={"pool_id": dst["id"], "migrate": False},
        )

        # Try to migrate with src=other (route points to dst, not other)
        resp = await client.post("/api/migrations", json={
            "bucket": "mismatch-bk",
            "src_pool_id": other["id"],
            "dst_pool_id": src["id"],
        })
        assert resp.status_code == 400
        assert "source pool mismatch" in resp.json()["detail"].lower()

    async def test_migration_accepted_and_route_id_bound(
        self, client, mock_nginx, mock_s3, mock_rclone
    ):
        """create_migration succeeds when route exists, src matches, and route_id is stored."""
        src, dst, vh = await self._setup(
            client, mock_nginx, mock_s3, "rv-src3", "rv-dst3", "rv3.example.com"
        )
        mock_s3["list_buckets"].return_value = [{"name": "ok-bk", "created": "2025-01-01"}]
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")

        # Promote bucket (empty) -> route now points to dst
        mock_s3["promote_src_has_objects"].return_value = False
        await client.post(
            f"/api/vhosts/{vh['id']}/buckets/ok-bk/promote",
            json={"pool_id": dst["id"], "migrate": False},
        )

        # Migrate from dst -> src (route currently points to dst = correct src)
        resp = await client.post("/api/migrations", json={
            "bucket": "ok-bk",
            "src_pool_id": dst["id"],
            "dst_pool_id": src["id"],
        })
        assert resp.status_code == 201
        mig = resp.json()
        assert mig["bucket"] == "ok-bk"

        from nanio_orchestrator.db import get_db_ctx
        async with get_db_ctx() as db:
            rows = await db.execute_fetchall(
                "SELECT route_id FROM migrations WHERE id = ?", (mig["id"],)
            )
        assert rows[0]["route_id"] is not None, \
            "migration record must have route_id bound"
