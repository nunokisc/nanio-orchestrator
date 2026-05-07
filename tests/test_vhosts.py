"""Tests for vhost and route CRUD API."""

from tests.conftest import create_member, create_pool, create_vhost


class TestVhostCRUD:
    async def test_create_vhost(self, client):
        resp = await client.post("/api/vhosts", json={"server_name": "s3.example.com", "ssl": False})
        assert resp.status_code == 201
        assert resp.json()["server_name"] == "s3.example.com"

    async def test_create_vhost_with_default_pool(self, client):
        pool = await create_pool(client, "vh-pool")
        resp = await client.post("/api/vhosts", json={
            "server_name": "pool.example.com",
            "ssl": False,
            "default_pool_id": pool["id"],
        })
        assert resp.status_code == 201
        assert resp.json()["default_pool_id"] == pool["id"]

    async def test_create_vhost_duplicate(self, client):
        await create_vhost(client, "dup.example.com")
        resp = await client.post("/api/vhosts", json={"server_name": "dup.example.com", "ssl": False})
        assert resp.status_code == 409

    async def test_list_vhosts(self, client):
        await create_vhost(client, "list1.example.com")
        await create_vhost(client, "list2.example.com")
        resp = await client.get("/api/vhosts")
        assert resp.status_code == 200
        assert len(resp.json()) >= 2

    async def test_get_vhost(self, client):
        vh = await create_vhost(client, "get.example.com")
        resp = await client.get(f"/api/vhosts/{vh['id']}")
        assert resp.status_code == 200
        assert resp.json()["server_name"] == "get.example.com"

    async def test_update_vhost(self, client):
        vh = await create_vhost(client, "upd.example.com")
        resp = await client.put(f"/api/vhosts/{vh['id']}", json={"listen_port": 8443})
        assert resp.status_code == 200
        assert resp.json()["listen_port"] == 8443

    async def test_delete_vhost(self, client, mock_nginx):
        vh = await create_vhost(client, "del.example.com")
        resp = await client.delete(f"/api/vhosts/{vh['id']}")
        assert resp.status_code == 204


class TestRouteCRUD:
    async def test_create_route(self, client, mock_nginx):
        pool = await create_pool(client, "rt-pool")
        await create_member(client, pool["id"])
        vh = await create_vhost(client, "route.example.com")
        resp = await client.post(f"/api/vhosts/{vh['id']}/routes", json={
            "path_prefix": "/assets/",
            "pool_id": pool["id"],
        })
        assert resp.status_code == 201
        assert resp.json()["path_prefix"] == "/assets/"

    async def test_create_route_with_key_prefix(self, client, mock_nginx):
        pool = await create_pool(client, "rt-kp-pool")
        await create_member(client, pool["id"])
        vh = await create_vhost(client, "kp.example.com")
        resp = await client.post(f"/api/vhosts/{vh['id']}/routes", json={
            "path_prefix": "/photos/",
            "pool_id": pool["id"],
            "key_prefix": "photos-2025/",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["key_prefix"] == "photos-2025/"

    async def test_create_route_invalid_prefix(self, client):
        pool = await create_pool(client, "rt-inv-pool")
        vh = await create_vhost(client, "inv.example.com")
        resp = await client.post(f"/api/vhosts/{vh['id']}/routes", json={
            "path_prefix": "no-slash",
            "pool_id": pool["id"],
        })
        assert resp.status_code == 422

    async def test_list_routes(self, client, mock_nginx):
        pool = await create_pool(client, "rt-list-pool")
        await create_member(client, pool["id"])
        vh = await create_vhost(client, "rtlist.example.com")
        await client.post(f"/api/vhosts/{vh['id']}/routes", json={
            "path_prefix": "/a/", "pool_id": pool["id"],
        })
        await client.post(f"/api/vhosts/{vh['id']}/routes", json={
            "path_prefix": "/b/", "pool_id": pool["id"],
        })
        resp = await client.get(f"/api/vhosts/{vh['id']}/routes")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    async def test_delete_route(self, client, mock_nginx):
        pool = await create_pool(client, "rt-del-pool")
        await create_member(client, pool["id"])
        vh = await create_vhost(client, "rtdel.example.com")
        route_resp = await client.post(f"/api/vhosts/{vh['id']}/routes", json={
            "path_prefix": "/del/", "pool_id": pool["id"],
        })
        route = route_resp.json()
        resp = await client.delete(f"/api/vhosts/{vh['id']}/routes/{route['id']}")
        assert resp.status_code == 204


class TestRouteScenarios:
    """Scenario 1 (config-only) and Scenario 2 (migration) routing logic."""

    async def test_create_route_never_triggers_migration(self, client, mock_nginx, mock_s3):
        """Scenario 1: creating a new route never auto-starts a migration, even if src has objects."""
        src = await create_pool(client, "sc1-src")
        await create_member(client, src["id"], "10.0.0.1:9000")
        dst = await create_pool(client, "sc1-dst")
        await create_member(client, dst["id"], "10.0.0.2:9000")
        vh = await create_vhost(client, "sc1.example.com", default_pool_id=src["id"])

        # Source bucket has many objects — but create_route must NOT start migration
        mock_s3["count_objects"].return_value = 500

        resp = await client.post(f"/api/vhosts/{vh['id']}/routes", json={
            "path_prefix": "/mybucket/",
            "pool_id": dst["id"],
        })
        assert resp.status_code == 201
        data = resp.json()
        # Route points directly to dst (no migration redirect to src)
        assert data["pool_id"] == dst["id"]
        # No migration_id in response
        assert data.get("migration_id") is None

        # No migration created in the DB
        list_resp = await client.get("/api/migrations")
        assert list_resp.status_code == 200
        assert len(list_resp.json()) == 0

    async def test_create_subpath_route_no_migration(self, client, mock_nginx, mock_s3):
        """Scenario 1: creating a sub-path route when parent bucket exists is config-only."""
        src = await create_pool(client, "sc2-src")
        await create_member(client, src["id"], "10.0.0.1:9000")
        dst = await create_pool(client, "sc2-dst")
        await create_member(client, dst["id"], "10.0.0.2:9000")
        vh = await create_vhost(client, "sc2.example.com", default_pool_id=src["id"])

        mock_s3["count_objects"].return_value = 100

        # Create parent bucket route
        await client.post(f"/api/vhosts/{vh['id']}/routes", json={
            "path_prefix": "/bucket2/",
            "pool_id": src["id"],
        })

        # Create sub-path route on a different pool — must not trigger migration
        resp = await client.post(f"/api/vhosts/{vh['id']}/routes", json={
            "path_prefix": "/bucket2/coisas/",
            "pool_id": dst["id"],
        })
        assert resp.status_code == 201
        assert resp.json().get("migration_id") is None

        list_resp = await client.get("/api/migrations")
        assert len(list_resp.json()) == 0

    async def test_update_route_pool_change_no_objects_is_direct_update(
        self, client, mock_nginx, mock_s3
    ):
        """Scenario 1: updating route pool when source bucket is empty → direct update, no migration."""
        pool1 = await create_pool(client, "sc3-p1")
        await create_member(client, pool1["id"], "10.0.0.1:9000")
        pool2 = await create_pool(client, "sc3-p2")
        await create_member(client, pool2["id"], "10.0.0.2:9000")
        vh = await create_vhost(client, "sc3.example.com")

        route_resp = await client.post(f"/api/vhosts/{vh['id']}/routes", json={
            "path_prefix": "/bucket3/", "pool_id": pool1["id"],
        })
        route = route_resp.json()

        # Source bucket is empty → no migration
        mock_s3["count_objects"].return_value = 0

        resp = await client.put(
            f"/api/vhosts/{vh['id']}/routes/{route['id']}",
            json={"pool_id": pool2["id"]},
        )
        assert resp.status_code == 200
        assert resp.json()["pool_id"] == pool2["id"]  # updated directly
        assert resp.json().get("migration_id") is None

    async def test_update_route_pool_change_with_objects_starts_migration(
        self, client, mock_nginx, mock_rclone, mock_s3
    ):
        """Scenario 2: updating route pool when source bucket has objects → migration starts."""
        src = await create_pool(client, "sc4-src")
        await create_member(client, src["id"], "10.0.0.1:9000")
        dst = await create_pool(client, "sc4-dst")
        await create_member(client, dst["id"], "10.0.0.2:9000")
        vh = await create_vhost(client, "sc4.example.com", default_pool_id=src["id"])

        route_resp = await client.post(f"/api/vhosts/{vh['id']}/routes", json={
            "path_prefix": "/bucket4/", "pool_id": src["id"],
        })
        route = route_resp.json()

        # Source bucket has objects → migration
        mock_s3["count_objects"].return_value = 50

        resp = await client.put(
            f"/api/vhosts/{vh['id']}/routes/{route['id']}",
            json={"pool_id": dst["id"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        # Pool_id stays on src while migration runs
        assert data["pool_id"] == src["id"]
        assert data["migration_id"] is not None

        # Migration exists in DB
        mig_resp = await client.get(f"/api/migrations/{data['migration_id']}")
        assert mig_resp.status_code == 200
        mig = mig_resp.json()
        assert mig["src_pool_id"] == src["id"]
        assert mig["dst_pool_id"] == dst["id"]
        assert mig["bucket"] == "bucket4"

    async def test_update_subpath_route_pool_change_is_direct(
        self, client, mock_nginx, mock_s3
    ):
        """Scenario 1: updating pool on a sub-path route is always direct (no migration)."""
        p1 = await create_pool(client, "sc5-p1")
        await create_member(client, p1["id"], "10.0.0.1:9000")
        p2 = await create_pool(client, "sc5-p2")
        await create_member(client, p2["id"], "10.0.0.2:9000")
        vh = await create_vhost(client, "sc5.example.com")

        # Parent route
        await client.post(f"/api/vhosts/{vh['id']}/routes", json={
            "path_prefix": "/bucket5/", "pool_id": p1["id"],
        })
        # Sub-path route
        sub_resp = await client.post(f"/api/vhosts/{vh['id']}/routes", json={
            "path_prefix": "/bucket5/subpath/", "pool_id": p1["id"],
        })
        sub_route = sub_resp.json()

        mock_s3["count_objects"].return_value = 200  # has objects but is sub-path

        resp = await client.put(
            f"/api/vhosts/{vh['id']}/routes/{sub_route['id']}",
            json={"pool_id": p2["id"]},
        )
        assert resp.status_code == 200
        assert resp.json()["pool_id"] == p2["id"]  # direct update
        assert resp.json().get("migration_id") is None
