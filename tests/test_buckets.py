"""Tests for bucket sync functionality."""

from unittest.mock import AsyncMock, patch

from tests.conftest import create_member, create_pool, create_vhost


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


class TestOrphanScanAndPurge:
    """Tests for GET /vhosts/{id}/buckets/orphans and POST /vhosts/{id}/buckets/{bk}/purge-orphan"""

    async def _setup(self, client, mock_nginx, mock_s3, prefix):
        pool = await create_pool(client, f"{prefix}-pool")
        await create_member(client, pool["id"], "10.0.0.1:9000")
        dst = await create_pool(client, f"{prefix}-dst")
        await create_member(client, dst["id"], "10.0.0.2:9000")
        vh = await create_vhost(client, f"{prefix}.example.com", default_pool_id=pool["id"])
        return pool, dst, vh

    async def test_orphan_scan_no_default_pool(self, client):
        vh = await create_vhost(client, "orp-nopool.example.com")
        resp = await client.get(f"/api/vhosts/{vh['id']}/buckets/orphans")
        assert resp.status_code == 400

    async def test_orphan_scan_no_routed_buckets(self, client, mock_nginx, mock_s3):
        pool, dst, vh = await self._setup(client, mock_nginx, mock_s3, "orp-empty")
        # No routed buckets → orphan scan returns empty list
        with patch("nanio_orchestrator.api.buckets.count_objects", new=AsyncMock(return_value=0)), \
             patch("nanio_orchestrator.api.buckets.get_pool_s3_params", new=AsyncMock(return_value=("ak", "sk", None))):
            resp = await client.get(f"/api/vhosts/{vh['id']}/buckets/orphans")
        assert resp.status_code == 200
        data = resp.json()
        assert data["orphans"] == []
        assert data["checked"] == 0

    async def test_orphan_scan_finds_stale_objects(self, client, mock_nginx, mock_s3):
        pool, dst, vh = await self._setup(client, mock_nginx, mock_s3, "orp-found")
        # Sync + promote bucket to dst (creates a routed bucket_sync entry)
        mock_s3["list_buckets"].return_value = [{"name": "stale-bk", "created": "2025-01-01"}]
        mock_s3["promote_src_has_objects"].return_value = False
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")
        await client.post(f"/api/vhosts/{vh['id']}/buckets/stale-bk/promote", json={
            "pool_id": dst["id"], "migrate": False,
        })
        # Orphan scan: default pool still has objects in stale-bk
        with patch("nanio_orchestrator.api.buckets.count_objects", new=AsyncMock(return_value=3)), \
             patch("nanio_orchestrator.api.buckets.get_pool_s3_params", new=AsyncMock(return_value=("ak", "sk", None))):
            resp = await client.get(f"/api/vhosts/{vh['id']}/buckets/orphans")
        assert resp.status_code == 200
        data = resp.json()
        assert data["checked"] == 1
        assert len(data["orphans"]) == 1
        assert data["orphans"][0]["bucket"] == "stale-bk"
        assert data["orphans"][0]["objects"] == 3

    async def test_orphan_scan_skips_clean_buckets(self, client, mock_nginx, mock_s3):
        pool, dst, vh = await self._setup(client, mock_nginx, mock_s3, "orp-clean")
        mock_s3["list_buckets"].return_value = [{"name": "clean-bk", "created": "2025-01-01"}]
        mock_s3["promote_src_has_objects"].return_value = False
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")
        await client.post(f"/api/vhosts/{vh['id']}/buckets/clean-bk/promote", json={
            "pool_id": dst["id"], "migrate": False,
        })
        # Default pool is empty → not in orphan list
        with patch("nanio_orchestrator.api.buckets.count_objects", new=AsyncMock(return_value=0)), \
             patch("nanio_orchestrator.api.buckets.get_pool_s3_params", new=AsyncMock(return_value=("ak", "sk", None))):
            resp = await client.get(f"/api/vhosts/{vh['id']}/buckets/orphans")
        assert resp.status_code == 200
        assert resp.json()["orphans"] == []

    async def test_purge_orphan_requires_routed_bucket(self, client, mock_nginx, mock_s3):
        pool, dst, vh = await self._setup(client, mock_nginx, mock_s3, "orp-purgereq")
        # Bucket not in bucket_sync at all → 400
        with patch("nanio_orchestrator.api.buckets.get_pool_s3_params", new=AsyncMock(return_value=("ak", "sk", None))):
            resp = await client.post(f"/api/vhosts/{vh['id']}/buckets/nonexistent/purge-orphan")
        assert resp.status_code == 400

    async def test_purge_orphan_deletes_objects(self, client, mock_nginx, mock_s3):
        pool, dst, vh = await self._setup(client, mock_nginx, mock_s3, "orp-purge")
        mock_s3["list_buckets"].return_value = [{"name": "purge-bk", "created": "2025-01-01"}]
        mock_s3["promote_src_has_objects"].return_value = False
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")
        await client.post(f"/api/vhosts/{vh['id']}/buckets/purge-bk/promote", json={
            "pool_id": dst["id"], "migrate": False,
        })
        with patch("nanio_orchestrator.api.buckets.list_objects", new=AsyncMock(return_value=["f1.dat", "f2.dat"])), \
             patch("nanio_orchestrator.api.buckets.delete_object", new=AsyncMock(return_value=True)), \
             patch("nanio_orchestrator.api.buckets.get_pool_s3_params", new=AsyncMock(return_value=("ak", "sk", None))):
            resp = await client.post(f"/api/vhosts/{vh['id']}/buckets/purge-bk/purge-orphan")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["deleted"] == 2

    async def test_purge_orphan_empty_bucket(self, client, mock_nginx, mock_s3):
        pool, dst, vh = await self._setup(client, mock_nginx, mock_s3, "orp-purgeempty")
        mock_s3["list_buckets"].return_value = [{"name": "emptypurge-bk", "created": "2025-01-01"}]
        mock_s3["promote_src_has_objects"].return_value = False
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")
        await client.post(f"/api/vhosts/{vh['id']}/buckets/emptypurge-bk/promote", json={
            "pool_id": dst["id"], "migrate": False,
        })
        with patch("nanio_orchestrator.api.buckets.list_objects", new=AsyncMock(return_value=[])), \
             patch("nanio_orchestrator.api.buckets.get_pool_s3_params", new=AsyncMock(return_value=("ak", "sk", None))):
            resp = await client.post(f"/api/vhosts/{vh['id']}/buckets/emptypurge-bk/purge-orphan")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["deleted"] == 0


class TestBucketDeletedStatus:
    """Bucket marked 'deleted' when it disappears from S3 ListBuckets."""

    async def test_bucket_marked_deleted_when_missing_from_list(
        self, client, mock_s3, mock_nginx
    ):
        """A bucket previously discovered but absent from the next sync becomes 'deleted'."""
        pool = await create_pool(client, "del-status-pool")
        await create_member(client, pool["id"], "10.0.0.1:9000")
        vh = await create_vhost(client, "del-status.example.com", default_pool_id=pool["id"])

        # First sync: bucket exists
        mock_s3["list_buckets"].return_value = [{"name": "vanishing", "created": "2025-01-01"}]
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")

        # Second sync: bucket gone from S3
        mock_s3["list_buckets"].return_value = []
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")

        resp = await client.get(f"/api/vhosts/{vh['id']}/buckets")
        assert resp.status_code == 200
        buckets = resp.json()["buckets"]
        vanishing = next((b for b in buckets if b["name"] == "vanishing"), None)
        assert vanishing is not None, "Deleted bucket must still appear in bucket list"
        assert vanishing["status"] == "deleted", (
            "Bucket missing from ListBuckets must be marked 'deleted'"
        )

    async def test_ignored_bucket_not_marked_deleted(self, client, mock_s3, mock_nginx):
        """An ignored bucket stays 'ignored' even when absent from the next sync."""
        pool = await create_pool(client, "del-ign-pool")
        await create_member(client, pool["id"], "10.0.0.1:9000")
        vh = await create_vhost(client, "del-ign.example.com", default_pool_id=pool["id"])

        mock_s3["list_buckets"].return_value = [{"name": "ignored-bk", "created": "2025-01-01"}]
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")
        await client.post(f"/api/vhosts/{vh['id']}/buckets/ignored-bk/ignore")

        # Bucket disappears from S3 — must stay 'ignored'
        mock_s3["list_buckets"].return_value = []
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")

        resp = await client.get(f"/api/vhosts/{vh['id']}/buckets")
        bk = next(b for b in resp.json()["buckets"] if b["name"] == "ignored-bk")
        assert bk["status"] == "ignored", "Ignored buckets must not be changed to deleted"

    async def test_deleted_bucket_not_promotable(self, client, mock_s3, mock_nginx):
        """A deleted bucket cannot be promoted to a new pool."""
        src = await create_pool(client, "del-prm-src")
        await create_member(client, src["id"], "10.0.0.1:9000")
        dst = await create_pool(client, "del-prm-dst")
        await create_member(client, dst["id"], "10.0.0.2:9000")
        vh = await create_vhost(client, "del-prm.example.com", default_pool_id=src["id"])

        mock_s3["list_buckets"].return_value = [{"name": "gone-bk", "created": "2025-01-01"}]
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")

        mock_s3["list_buckets"].return_value = []
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")

        resp = await client.post(
            f"/api/vhosts/{vh['id']}/buckets/gone-bk/promote",
            json={"pool_id": dst["id"]},
        )
        assert resp.status_code == 400, "Promoting a deleted bucket must be refused"


class TestRemoveBucketRoute:
    """DELETE /api/vhosts/:id/buckets/:bucket/route"""

    async def test_removes_route_and_resets_to_unrouted(self, client, mock_s3, mock_nginx):
        """Route removed and bucket_sync reverts to 'unrouted' for a live bucket."""
        src = await create_pool(client, "rmr-src")
        await create_member(client, src["id"], "10.0.0.1:9000")
        dst = await create_pool(client, "rmr-dst")
        await create_member(client, dst["id"], "10.0.0.2:9000")
        vh = await create_vhost(client, "rmr.example.com", default_pool_id=src["id"])

        mock_s3["list_buckets"].return_value = [{"name": "rmr-bk", "created": "2025-01-01"}]
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")
        # Promote (empty bucket) to create the route
        await client.post(
            f"/api/vhosts/{vh['id']}/buckets/rmr-bk/promote",
            json={"pool_id": dst["id"]},
        )

        resp = await client.delete(f"/api/vhosts/{vh['id']}/buckets/rmr-bk/route")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["route_removed"] == "/rmr-bk/"

        # Route gone from vhost
        routes = (await client.get(f"/api/vhosts/{vh['id']}/routes")).json()
        assert not any(r["path_prefix"] == "/rmr-bk/" for r in routes)

        # bucket_sync reverted to unrouted
        buckets = (await client.get(f"/api/vhosts/{vh['id']}/buckets")).json()["buckets"]
        bk = next((b for b in buckets if b["name"] == "rmr-bk"), None)
        assert bk is not None
        assert bk["status"] == "unrouted"

    async def test_removes_deleted_bucket_record_entirely(self, client, mock_s3, mock_nginx):
        """Removing a route for a 'deleted' bucket also removes the bucket_sync row."""
        src = await create_pool(client, "rmrd-src")
        await create_member(client, src["id"], "10.0.0.1:9000")
        dst = await create_pool(client, "rmrd-dst")
        await create_member(client, dst["id"], "10.0.0.2:9000")
        vh = await create_vhost(client, "rmrd.example.com", default_pool_id=src["id"])

        mock_s3["list_buckets"].return_value = [{"name": "bye-bk", "created": "2025-01-01"}]
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")
        await client.post(
            f"/api/vhosts/{vh['id']}/buckets/bye-bk/promote",
            json={"pool_id": dst["id"]},
        )
        mock_s3["list_buckets"].return_value = []
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")

        resp = await client.delete(f"/api/vhosts/{vh['id']}/buckets/bye-bk/route")
        assert resp.status_code == 200

        buckets = (await client.get(f"/api/vhosts/{vh['id']}/buckets")).json()["buckets"]
        assert not any(b["name"] == "bye-bk" for b in buckets), (
            "bucket_sync record must be removed when route deleted for a 'deleted' bucket"
        )

    async def test_returns_404_when_no_route(self, client, mock_nginx):
        """Removing a route that does not exist returns 404."""
        pool = await create_pool(client, "rmr404-pool")
        await create_member(client, pool["id"], "10.0.0.1:9000")
        vh = await create_vhost(client, "rmr404.example.com", default_pool_id=pool["id"])

        resp = await client.delete(f"/api/vhosts/{vh['id']}/buckets/nonexistent/route")
        assert resp.status_code == 404


class TestHttpBucketRoutes:
    """GET/POST/DELETE /api/vhosts/:id/http-bucket-routes/:bucket"""

    async def _setup(self, client, mock_s3, mock_nginx, prefix):
        """Create nanio pool + http pool with source linkage, vhosts, and sync buckets."""
        nanio_pool = await create_pool(client, f"{prefix}-nanio", pool_type="nanio")
        await create_member(client, nanio_pool["id"], "10.0.0.1:9000")

        http_pool = await create_pool(
            client, f"{prefix}-http", pool_type="http",
            source_nanio_pool_id=nanio_pool["id"],
        )
        await create_member(client, http_pool["id"], "192.168.1.10:80", role="primary")

        nanio_vh = await create_vhost(
            client, f"{prefix}-nanio.example.com", default_pool_id=nanio_pool["id"]
        )
        http_vh = await create_vhost(
            client, f"{prefix}-http.example.com", default_pool_id=http_pool["id"]
        )

        mock_s3["list_buckets"].return_value = [
            {"name": "photos", "created": "2025-01-01"},
            {"name": "videos", "created": "2025-01-02"},
        ]
        await client.post(f"/api/vhosts/{nanio_vh['id']}/buckets/sync")

        return nanio_pool, http_pool, nanio_vh, http_vh

    async def test_list_shows_linked_nanio_buckets(self, client, mock_s3, mock_nginx):
        """GET lists routes and available buckets from the linked nanio pool."""
        _, _, _, http_vh = await self._setup(client, mock_s3, mock_nginx, "hbrl")

        resp = await client.get(f"/api/vhosts/{http_vh['id']}/http-bucket-routes")
        assert resp.status_code == 200
        data = resp.json()
        assert "linked_nanio_buckets" in data
        bucket_names = [b["bucket"] for b in data["linked_nanio_buckets"]]
        assert "photos" in bucket_names
        assert "videos" in bucket_names

    async def test_add_route_creates_nginx_route(self, client, mock_s3, mock_nginx):
        """POST creates a /{bucket}/ route on the http vhost."""
        _, http_pool, _, http_vh = await self._setup(client, mock_s3, mock_nginx, "hbra")

        resp = await client.post(
            f"/api/vhosts/{http_vh['id']}/http-bucket-routes/photos"
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["ok"] is True
        assert data["route"] == "/photos/"
        assert data["pool_id"] == http_pool["id"]

        routes = (await client.get(f"/api/vhosts/{http_vh['id']}/routes")).json()
        assert any(r["path_prefix"] == "/photos/" for r in routes)

    async def test_add_route_duplicate_rejected(self, client, mock_s3, mock_nginx):
        """Adding the same route twice returns 409."""
        _, _, _, http_vh = await self._setup(client, mock_s3, mock_nginx, "hbrd")

        await client.post(f"/api/vhosts/{http_vh['id']}/http-bucket-routes/photos")
        resp = await client.post(f"/api/vhosts/{http_vh['id']}/http-bucket-routes/photos")
        assert resp.status_code == 409

    async def test_remove_route_deletes_it(self, client, mock_s3, mock_nginx):
        """DELETE removes the /{bucket}/ route from the http vhost."""
        _, _, _, http_vh = await self._setup(client, mock_s3, mock_nginx, "hbrr")

        await client.post(f"/api/vhosts/{http_vh['id']}/http-bucket-routes/photos")
        resp = await client.delete(f"/api/vhosts/{http_vh['id']}/http-bucket-routes/photos")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        routes = (await client.get(f"/api/vhosts/{http_vh['id']}/routes")).json()
        assert not any(r["path_prefix"] == "/photos/" for r in routes)

    async def test_remove_nonexistent_route_returns_404(self, client, mock_s3, mock_nginx):
        """Removing a route that was never created returns 404."""
        _, _, _, http_vh = await self._setup(client, mock_s3, mock_nginx, "hbr404")

        resp = await client.delete(
            f"/api/vhosts/{http_vh['id']}/http-bucket-routes/missing"
        )
        assert resp.status_code == 404

    async def test_nanio_vhost_rejected(self, client, mock_nginx):
        """http-bucket-routes endpoints are only available on http vhosts with source link."""
        nanio_pool = await create_pool(client, "hbrn-nanio", pool_type="nanio")
        await create_member(client, nanio_pool["id"], "10.0.0.1:9000")
        nanio_vh = await create_vhost(
            client, "hbrn-nanio.example.com", default_pool_id=nanio_pool["id"]
        )
        resp = await client.get(f"/api/vhosts/{nanio_vh['id']}/http-bucket-routes")
        assert resp.status_code == 400

    async def test_http_vhost_without_source_rejected(self, client, mock_nginx):
        """http vhost without source_nanio_pool_id returns 400."""
        http_pool = await create_pool(client, "hbrns-http", pool_type="http")
        await create_member(client, http_pool["id"], "192.168.1.10:80", role="primary")
        http_vh = await create_vhost(
            client, "hbrns-http.example.com", default_pool_id=http_pool["id"]
        )
        resp = await client.get(f"/api/vhosts/{http_vh['id']}/http-bucket-routes")
        assert resp.status_code == 400

    async def test_list_marks_already_routed_buckets(self, client, mock_s3, mock_nginx):
        """linked_nanio_buckets entries show has_route=True when the route already exists."""
        _, _, _, http_vh = await self._setup(client, mock_s3, mock_nginx, "hbrm")

        await client.post(f"/api/vhosts/{http_vh['id']}/http-bucket-routes/photos")

        resp = await client.get(f"/api/vhosts/{http_vh['id']}/http-bucket-routes")
        buckets = {b["bucket"]: b for b in resp.json()["linked_nanio_buckets"]}
        assert buckets["photos"]["has_route"] is True
        assert buckets["videos"]["has_route"] is False

