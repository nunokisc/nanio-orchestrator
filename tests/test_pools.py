"""Tests for pool CRUD API."""

from unittest.mock import AsyncMock, patch

from tests.conftest import create_member, create_pool, create_vhost


class TestPoolCRUD:
    async def test_create_pool(self, client):
        resp = await client.post("/api/pools", json={"name": "my-pool", "type": "nanio"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "my-pool"
        assert data["type"] == "nanio"
        assert data["lb_method"] == "least_conn"

    async def test_create_pool_types(self, client):
        for pt in ("nanio", "http"):
            resp = await client.post("/api/pools", json={"name": f"pool-{pt}", "type": pt})
            assert resp.status_code == 201

    async def test_create_pool_invalid_type(self, client):
        resp = await client.post("/api/pools", json={"name": "pool-cold", "type": "cold"})
        assert resp.status_code == 422

    async def test_create_pool_invalid_name(self, client):
        resp = await client.post("/api/pools", json={"name": "bad name!", "type": "nanio"})
        assert resp.status_code == 422

    async def test_create_pool_duplicate(self, client):
        await create_pool(client, "dup-pool")
        resp = await client.post("/api/pools", json={"name": "dup-pool", "type": "nanio"})
        assert resp.status_code == 409

    async def test_list_pools(self, client):
        await create_pool(client, "list-pool-1")
        await create_pool(client, "list-pool-2")
        resp = await client.get("/api/pools")
        assert resp.status_code == 200
        assert len(resp.json()) >= 2

    async def test_get_pool(self, client):
        pool = await create_pool(client, "get-pool")
        resp = await client.get(f"/api/pools/{pool['id']}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "get-pool"

    async def test_get_pool_not_found(self, client):
        resp = await client.get("/api/pools/99999")
        assert resp.status_code == 404

    async def test_update_pool(self, client):
        pool = await create_pool(client, "upd-pool")
        resp = await client.put(f"/api/pools/{pool['id']}", json={"description": "updated"})
        assert resp.status_code == 200
        assert resp.json()["description"] == "updated"

    async def test_delete_pool(self, client, mock_nginx):
        pool = await create_pool(client, "del-pool")
        resp = await client.delete(f"/api/pools/{pool['id']}")
        assert resp.status_code == 204

    async def test_delete_pool_not_found(self, client):
        resp = await client.delete("/api/pools/99999")
        assert resp.status_code == 404


class TestMemberCRUD:
    async def test_add_member(self, client, mock_nginx):
        pool = await create_pool(client, "mem-pool")
        resp = await client.post(
            f"/api/pools/{pool['id']}/members",
            json={"address": "10.0.0.1:9000"},
        )
        assert resp.status_code == 201
        assert resp.json()["address"] == "10.0.0.1:9000"

    async def test_add_member_invalid_address(self, client):
        pool = await create_pool(client, "mem-pool-2")
        resp = await client.post(
            f"/api/pools/{pool['id']}/members",
            json={"address": "no-port"},
        )
        assert resp.status_code == 422

    async def test_member_roles_nanio(self, client, mock_nginx):
        pool = await create_pool(client, "role-nanio", pool_type="nanio")
        # nanio pools only accept 'active' role
        resp = await client.post(
            f"/api/pools/{pool['id']}/members",
            json={"address": "10.0.0.1:9000", "role": "active"},
        )
        assert resp.status_code == 201

    async def test_member_roles_http(self, client, mock_nginx):
        pool = await create_pool(client, "role-http", pool_type="http")
        # http pools use primary/replica
        resp = await client.post(
            f"/api/pools/{pool['id']}/members",
            json={"address": "10.0.0.1:9000", "role": "primary"},
        )
        assert resp.status_code == 201

    async def test_delete_member(self, client, mock_nginx):
        pool = await create_pool(client, "delmem-pool")
        member = await create_member(client, pool["id"])
        resp = await client.delete(f"/api/pools/{pool['id']}/members/{member['id']}")
        assert resp.status_code == 204

    async def test_list_members(self, client, mock_nginx):
        pool = await create_pool(client, "listmem-pool")
        await create_member(client, pool["id"], "10.0.0.1:9000")
        await create_member(client, pool["id"], "10.0.0.2:9000")
        resp = await client.get(f"/api/pools/{pool['id']}/members")
        assert resp.status_code == 200
        assert len(resp.json()) == 2


class TestPoolBucketStatus:
    """Tests for GET /api/pools/{id}/buckets/status"""

    async def _setup(self, client, mock_nginx, pool_name="bs-pool", vh_name="bs.example.com"):
        pool = await create_pool(client, pool_name)
        await create_member(client, pool["id"], "10.0.0.1:9000")
        vh = await create_vhost(client, vh_name, default_pool_id=pool["id"])
        return pool, vh

    async def test_pool_not_found(self, client):
        resp = await client.get("/api/pools/99999/buckets/status")
        assert resp.status_code == 404

    async def test_http_pool_rejected(self, client, mock_nginx):
        pool = await create_pool(client, "bs-http-pool", pool_type="http")
        await create_member(client, pool["id"], "10.0.0.1:80", role="primary")
        resp = await client.get(f"/api/pools/{pool['id']}/buckets/status")
        assert resp.status_code == 400

    async def test_no_members_rejected(self, client):
        pool = await create_pool(client, "bs-nomem-pool")
        resp = await client.get(f"/api/pools/{pool['id']}/buckets/status")
        assert resp.status_code == 400

    async def test_unrouted_bucket(self, client, mock_nginx):
        pool, vh = await self._setup(client, mock_nginx, "bs-unrouted", "bs-unrouted.example.com")
        buckets_mock = AsyncMock(return_value=[{"name": "lonely", "created": "2025-01-01"}])
        creds_mock = AsyncMock(return_value=("ak", "sk", None))
        with patch("nanio_orchestrator.api.pools.s3_list_buckets", new=buckets_mock), \
             patch("nanio_orchestrator.api.pools.get_pool_s3_params", new=creds_mock):
            resp = await client.get(f"/api/pools/{pool['id']}/buckets/status")
        assert resp.status_code == 200
        data = resp.json()
        bucket = next(b for b in data["buckets"] if b["bucket"] == "lonely")
        # pool is the default — bucket is served via the catch-all (via_default)
        assert bucket["status"] == "via_default"

    async def test_routed_bucket(self, client, mock_nginx):
        pool, vh = await self._setup(client, mock_nginx, "bs-routed", "bs-routed.example.com")
        # Create a dedicated route for the bucket
        await client.post(f"/api/vhosts/{vh['id']}/routes", json={
            "path_prefix": "/my-bucket/",
            "pool_id": pool["id"],
        })
        buckets_mock = AsyncMock(return_value=[{"name": "my-bucket", "created": "2025-01-01"}])
        creds_mock = AsyncMock(return_value=("ak", "sk", None))
        with patch("nanio_orchestrator.api.pools.s3_list_buckets", new=buckets_mock), \
             patch("nanio_orchestrator.api.pools.get_pool_s3_params", new=creds_mock):
            resp = await client.get(f"/api/pools/{pool['id']}/buckets/status")
        assert resp.status_code == 200
        bucket = next(b for b in resp.json()["buckets"] if b["bucket"] == "my-bucket")
        assert bucket["status"] == "routed"
        assert len(bucket["routed_in"]) == 1
        assert bucket["routed_in"][0]["server_name"] == "bs-routed.example.com"

    async def test_routed_takes_priority_over_orphaned(self, client, mock_nginx):
        """A bucket with an active route must show 'routed', not 'orphaned'."""
        src = await create_pool(client, "bs-pri-src")
        await create_member(client, src["id"], "10.0.0.1:9000")
        dst = await create_pool(client, "bs-pri-dst")
        await create_member(client, dst["id"], "10.0.0.2:9000")
        vh = await create_vhost(client, "bs-pri.example.com", default_pool_id=src["id"])

        # Create route pointing to src (so src is the "routed" pool)
        await client.post(f"/api/vhosts/{vh['id']}/routes", json={
            "path_prefix": "/priority-bk/",
            "pool_id": src["id"],
        })

        # Inject a migration row marking src as orphaned source
        from nanio_orchestrator.db import get_db_ctx
        async with get_db_ctx() as db:
            await db.execute(
                """INSERT INTO migrations (bucket, src_pool_id, dst_pool_id, vhost_id,
                       phase, orphaned_source_pool_id, mode)
                   VALUES (?, ?, ?, ?, 'done', ?, 'copy')""",
                ("priority-bk", src["id"], dst["id"], vh["id"], src["id"]),
            )
            await db.commit()

        buckets_mock = AsyncMock(return_value=[{"name": "priority-bk", "created": "2025-01-01"}])
        creds_mock = AsyncMock(return_value=("ak", "sk", None))
        with patch("nanio_orchestrator.api.pools.s3_list_buckets", new=buckets_mock), \
             patch("nanio_orchestrator.api.pools.get_pool_s3_params", new=creds_mock):
            resp = await client.get(f"/api/pools/{src['id']}/buckets/status")
        assert resp.status_code == 200
        bucket = next(b for b in resp.json()["buckets"] if b["bucket"] == "priority-bk")
        assert bucket["status"] == "routed", "routed must take priority over orphaned"

    async def test_orphaned_bucket(self, client, mock_nginx):
        src = await create_pool(client, "bs-orp-src")
        await create_member(client, src["id"], "10.0.0.1:9000")
        dst = await create_pool(client, "bs-orp-dst")
        await create_member(client, dst["id"], "10.0.0.2:9000")
        vh = await create_vhost(client, "bs-orp.example.com", default_pool_id=dst["id"])

        # No route to src; mark src as orphaned source
        from nanio_orchestrator.db import get_db_ctx
        async with get_db_ctx() as db:
            await db.execute(
                """INSERT INTO migrations (bucket, src_pool_id, dst_pool_id, vhost_id,
                       phase, orphaned_source_pool_id, mode)
                   VALUES (?, ?, ?, ?, 'done', ?, 'copy')""",
                ("orphan-bk", src["id"], dst["id"], vh["id"], src["id"]),
            )
            await db.commit()

        buckets_mock = AsyncMock(return_value=[{"name": "orphan-bk", "created": "2025-01-01"}])
        creds_mock = AsyncMock(return_value=("ak", "sk", None))
        with patch("nanio_orchestrator.api.pools.s3_list_buckets", new=buckets_mock), \
             patch("nanio_orchestrator.api.pools.get_pool_s3_params", new=creds_mock):
            resp = await client.get(f"/api/pools/{src['id']}/buckets/status")
        assert resp.status_code == 200
        bucket = next(b for b in resp.json()["buckets"] if b["bucket"] == "orphan-bk")
        assert bucket["status"] == "orphaned"

    async def test_s3_error_returns_502(self, client, mock_nginx):
        pool, _ = await self._setup(client, mock_nginx, "bs-err", "bs-err.example.com")
        buckets_mock = AsyncMock(side_effect=Exception("conn refused"))
        creds_mock = AsyncMock(return_value=("ak", "sk", None))
        with patch("nanio_orchestrator.api.pools.s3_list_buckets", new=buckets_mock), \
             patch("nanio_orchestrator.api.pools.get_pool_s3_params", new=creds_mock):
            resp = await client.get(f"/api/pools/{pool['id']}/buckets/status")
        assert resp.status_code == 502


class TestPoolBucketObjects:
    """Tests for GET /api/pools/{id}/buckets/{bucket}/objects"""

    async def test_pool_not_found(self, client):
        resp = await client.get("/api/pools/99999/buckets/mybucket/objects")
        assert resp.status_code == 404

    async def test_http_pool_rejected(self, client, mock_nginx):
        pool = await create_pool(client, "obj-http-pool", pool_type="http")
        await create_member(client, pool["id"], "10.0.0.1:80", role="primary")
        resp = await client.get(f"/api/pools/{pool['id']}/buckets/mybucket/objects")
        assert resp.status_code == 400

    async def test_no_members_rejected(self, client):
        pool = await create_pool(client, "obj-nomem-pool")
        resp = await client.get(f"/api/pools/{pool['id']}/buckets/mybucket/objects")
        assert resp.status_code == 400

    async def test_returns_object_list(self, client, mock_nginx):
        pool = await create_pool(client, "obj-list-pool")
        await create_member(client, pool["id"], "10.0.0.1:9000")
        with patch("nanio_orchestrator.api.pools.list_objects", new=AsyncMock(return_value=["a.txt", "b.txt"])), \
             patch("nanio_orchestrator.api.pools.get_pool_s3_params", new=AsyncMock(return_value=("ak", "sk", None))):
            resp = await client.get(f"/api/pools/{pool['id']}/buckets/mybucket/objects")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        assert "a.txt" in data["objects"]
        assert data["bucket"] == "mybucket"

    async def test_s3_error_returns_502(self, client, mock_nginx):
        pool = await create_pool(client, "obj-err-pool")
        await create_member(client, pool["id"], "10.0.0.1:9000")
        with patch("nanio_orchestrator.api.pools.list_objects", new=AsyncMock(side_effect=Exception("timeout"))), \
             patch("nanio_orchestrator.api.pools.get_pool_s3_params", new=AsyncMock(return_value=("ak", "sk", None))):
            resp = await client.get(f"/api/pools/{pool['id']}/buckets/mybucket/objects")
        assert resp.status_code == 502


class TestPoolBucketPurge:
    """Tests for POST /api/pools/{id}/buckets/{bucket}/purge"""

    async def test_pool_not_found(self, client):
        resp = await client.post("/api/pools/99999/buckets/mybucket/purge")
        assert resp.status_code == 404

    async def test_http_pool_rejected(self, client, mock_nginx):
        pool = await create_pool(client, "purge-http-pool", pool_type="http")
        await create_member(client, pool["id"], "10.0.0.1:80", role="primary")
        resp = await client.post(f"/api/pools/{pool['id']}/buckets/mybucket/purge")
        assert resp.status_code == 400

    async def test_no_members_rejected(self, client):
        pool = await create_pool(client, "purge-nomem-pool")
        resp = await client.post(f"/api/pools/{pool['id']}/buckets/mybucket/purge")
        assert resp.status_code == 400

    async def test_purge_deletes_objects(self, client, mock_nginx):
        pool = await create_pool(client, "purge-ok-pool")
        await create_member(client, pool["id"], "10.0.0.1:9000")
        with patch("nanio_orchestrator.api.pools.list_objects", new=AsyncMock(return_value=["x.txt", "y.txt"])), \
             patch("nanio_orchestrator.api.pools.delete_object", new=AsyncMock(return_value=True)), \
             patch("nanio_orchestrator.api.pools.get_pool_s3_params", new=AsyncMock(return_value=("ak", "sk", None))):
            resp = await client.post(f"/api/pools/{pool['id']}/buckets/to-purge/purge")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["deleted"] == 2
        assert data["total"] == 2
        assert data["bucket"] == "to-purge"

    async def test_purge_empty_bucket(self, client, mock_nginx):
        pool = await create_pool(client, "purge-empty-pool")
        await create_member(client, pool["id"], "10.0.0.1:9000")
        with patch("nanio_orchestrator.api.pools.list_objects", new=AsyncMock(return_value=[])), \
             patch("nanio_orchestrator.api.pools.get_pool_s3_params", new=AsyncMock(return_value=("ak", "sk", None))):
            resp = await client.post(f"/api/pools/{pool['id']}/buckets/empty-bk/purge")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["deleted"] == 0
        assert data["total"] == 0

    async def test_s3_list_error_returns_502(self, client, mock_nginx):
        pool = await create_pool(client, "purge-listerr-pool")
        await create_member(client, pool["id"], "10.0.0.1:9000")
        with patch("nanio_orchestrator.api.pools.list_objects", new=AsyncMock(side_effect=Exception("timeout"))), \
             patch("nanio_orchestrator.api.pools.get_pool_s3_params", new=AsyncMock(return_value=("ak", "sk", None))):
            resp = await client.post(f"/api/pools/{pool['id']}/buckets/err-bk/purge")
        assert resp.status_code == 502
