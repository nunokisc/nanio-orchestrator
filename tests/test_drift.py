"""Tests for drift detection functionality."""

import hashlib
import pytest
from pathlib import Path
from tests.conftest import create_pool, create_member


class TestDrift:
    async def test_config_drift_detected(self, client, mock_nginx, tmp_dirs):
        pool = await create_pool(client, "drift-pool")
        await create_member(client, pool["id"], "10.0.0.1:9000")

        # Manually modify the config file to introduce drift
        pool_file = Path(tmp_dirs["nginx_dir"]) / "pools" / "drift-pool.conf"
        assert pool_file.exists()

        original = pool_file.read_text()
        pool_file.write_text(original + "\n# manual edit")

        # Config status should detect drift
        resp = await client.get("/api/config/status")
        assert resp.status_code == 200
        data = resp.json()
        drifted = [f for f in data["files"] if f["drifted"]]
        assert len(drifted) >= 1

    async def test_rewrite_file(self, client, mock_nginx, tmp_dirs):
        pool = await create_pool(client, "rewrite-pool")
        await create_member(client, pool["id"], "10.0.0.1:9000")

        pool_file = Path(tmp_dirs["nginx_dir"]) / "pools" / "rewrite-pool.conf"
        original = pool_file.read_text()
        pool_file.write_text("# corrupted content")

        # Rewrite from DB
        resp = await client.post("/api/config/rewrite-file", json={
            "path": str(pool_file),
        })
        assert resp.status_code == 200

        # File should be restored
        assert pool_file.read_text() == original

    async def test_absorb_file(self, client, mock_nginx, tmp_dirs):
        pool = await create_pool(client, "absorb-pool")
        await create_member(client, pool["id"], "10.0.0.1:9000")

        pool_file = Path(tmp_dirs["nginx_dir"]) / "pools" / "absorb-pool.conf"
        new_content = pool_file.read_text() + "\n# accepted edit"
        pool_file.write_text(new_content)

        # Absorb the disk version
        resp = await client.post("/api/config/absorb-file", json={
            "path": str(pool_file),
        })
        assert resp.status_code == 200
