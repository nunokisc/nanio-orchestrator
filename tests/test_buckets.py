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
