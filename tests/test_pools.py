"""Tests for pool CRUD API."""

import pytest
from tests.conftest import create_pool, create_member


class TestPoolCRUD:
    async def test_create_pool(self, client):
        resp = await client.post("/api/pools", json={"name": "my-pool", "type": "nanio"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "my-pool"
        assert data["type"] == "nanio"
        assert data["lb_method"] == "least_conn"

    async def test_create_pool_types(self, client):
        for pt in ("nanio", "http", "cold"):
            resp = await client.post("/api/pools", json={"name": f"pool-{pt}", "type": pt})
            assert resp.status_code == 201

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
