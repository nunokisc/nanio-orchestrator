"""Tests for nginx config generation."""

from tests.conftest import create_member, create_pool, create_vhost


class TestConfigGeneration:
    async def test_pool_config_generated(self, client, mock_nginx, tmp_dirs):
        pool = await create_pool(client, "cfg-pool")
        await create_member(client, pool["id"], "10.0.0.1:9000")

        # Config should have been generated upon member addition
        from pathlib import Path
        pool_file = Path(tmp_dirs["nginx_dir"]) / "pools" / "cfg-pool.conf"
        assert pool_file.exists()
        content = pool_file.read_text()
        assert "upstream cfg-pool" in content
        assert "10.0.0.1:9000" in content

    async def test_vhost_config_generated(self, client, mock_nginx, tmp_dirs):
        pool = await create_pool(client, "vh-cfg-pool")
        await create_member(client, pool["id"], "10.0.0.1:9000")
        vh = await create_vhost(client, "cfg.example.com")
        await client.post(f"/api/vhosts/{vh['id']}/routes", json={
            "path_prefix": "/test/",
            "pool_id": pool["id"],
        })

        from pathlib import Path
        vhost_file = Path(tmp_dirs["nginx_dir"]) / "vhosts" / "cfg.example.com.conf"
        assert vhost_file.exists()
        content = vhost_file.read_text()
        assert "server_name cfg.example.com" in content
        assert "location /test/" in content

    async def test_key_prefix_in_config(self, client, mock_nginx, tmp_dirs):
        pool = await create_pool(client, "kp-cfg-pool")
        await create_member(client, pool["id"], "10.0.0.1:9000")
        vh = await create_vhost(client, "kp-cfg.example.com")
        await client.post(f"/api/vhosts/{vh['id']}/routes", json={
            "path_prefix": "/photos/",
            "pool_id": pool["id"],
            "key_prefix": "photos-2025/",
        })

        from pathlib import Path
        vhost_file = Path(tmp_dirs["nginx_dir"]) / "vhosts" / "kp-cfg.example.com.conf"
        assert vhost_file.exists()
        content = vhost_file.read_text()
        assert "rewrite" in content
        assert "photos-2025/" in content

    async def test_empty_pool_no_file(self, client, mock_nginx, tmp_dirs):
        pool = await create_pool(client, "empty-pool")
        # No members added, so no file should exist
        from pathlib import Path
        pool_file = Path(tmp_dirs["nginx_dir"]) / "pools" / "empty-pool.conf"
        assert not pool_file.exists()

    async def test_rebuild_all(self, client, mock_nginx, tmp_dirs):
        pool = await create_pool(client, "rebuild-pool")
        await create_member(client, pool["id"], "10.0.0.1:9000")

        resp = await client.post("/api/config/rebuild")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True

    async def test_config_status(self, client, mock_nginx, tmp_dirs):
        pool = await create_pool(client, "status-pool")
        await create_member(client, pool["id"], "10.0.0.1:9000")

        resp = await client.get("/api/config/status")
        assert resp.status_code == 200
