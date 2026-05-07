"""Tests for node config generation."""

from tests.conftest import create_member, create_pool


class TestNodeConfig:
    async def test_generate_nanio_only(self, client, mock_nginx):
        pool = await create_pool(client, "node-nanio")
        member = await create_member(client, pool["id"], "10.0.0.1:9000")

        resp = await client.post(
            f"/api/pools/{pool['id']}/members/{member['id']}/node-config",
            json={"node_type": "nanio-only"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["node_type"] == "nanio-only"
        assert len(data["files"]) >= 1
        paths = [f["path"] for f in data["files"]]
        assert any("nanio" in p for p in paths)

    async def test_generate_nginx_only(self, client, mock_nginx):
        pool = await create_pool(client, "node-nginx")
        member = await create_member(client, pool["id"], "10.0.0.1:9000")

        resp = await client.post(
            f"/api/pools/{pool['id']}/members/{member['id']}/node-config",
            json={"node_type": "nginx-only"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["node_type"] == "nginx-only"
        paths = [f["path"] for f in data["files"]]
        assert any("nginx" in p for p in paths)

    async def test_generate_nginx_nanio(self, client, mock_nginx):
        pool = await create_pool(client, "node-both")
        member = await create_member(client, pool["id"], "10.0.0.1:9000")

        resp = await client.post(
            f"/api/pools/{pool['id']}/members/{member['id']}/node-config",
            json={"node_type": "nginx-nanio"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["node_type"] == "nginx-nanio"
        assert len(data["files"]) >= 2

    async def test_invalid_node_type(self, client, mock_nginx):
        pool = await create_pool(client, "node-inv")
        member = await create_member(client, pool["id"], "10.0.0.1:9000")

        resp = await client.post(
            f"/api/pools/{pool['id']}/members/{member['id']}/node-config",
            json={"node_type": "invalid"},
        )
        assert resp.status_code == 422
