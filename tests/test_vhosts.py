"""Tests for vhost and route CRUD API."""

import pytest
from tests.conftest import create_pool, create_member, create_vhost


class TestVhostCRUD:
    async def test_create_vhost(self, client):
        resp = await client.post("/api/vhosts", json={"server_name": "s3.example.com"})
        assert resp.status_code == 201
        assert resp.json()["server_name"] == "s3.example.com"

    async def test_create_vhost_with_default_pool(self, client):
        pool = await create_pool(client, "vh-pool")
        resp = await client.post("/api/vhosts", json={
            "server_name": "pool.example.com",
            "default_pool_id": pool["id"],
        })
        assert resp.status_code == 201
        assert resp.json()["default_pool_id"] == pool["id"]

    async def test_create_vhost_duplicate(self, client):
        await create_vhost(client, "dup.example.com")
        resp = await client.post("/api/vhosts", json={"server_name": "dup.example.com"})
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
