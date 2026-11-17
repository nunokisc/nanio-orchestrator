"""The panel key must not be enough to read arbitrary files, run arbitrary programs,
or leave storage-node secrets lying around."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import create_member, create_pool


class TestAbsorbFile:
    @pytest.mark.parametrize("target", ["outside.conf", "nginx/pools/notes.txt", "nginx/pools/sub/x.conf"])
    async def test_refuses_anything_but_a_managed_conf(self, client, mock_nginx, tmp_dirs, target):
        from nanio_orchestrator.db import get_db_ctx

        victim = Path(tmp_dirs["nginx_dir"]).parent / target
        victim.parent.mkdir(parents=True, exist_ok=True)
        victim.write_text("secret material")

        resp = await client.post("/api/config/absorb-file", json={"path": str(victim)})

        assert resp.status_code == 400
        async with get_db_ctx() as db:
            rows = await db.execute_fetchall("SELECT 1 FROM config_files WHERE path = ?", (str(victim),))
        assert rows == []

    async def test_refuses_traversal_out_of_the_managed_directory(self, client, mock_nginx, tmp_dirs):
        outside = Path(tmp_dirs["nginx_dir"]).parent / "escape.conf"
        outside.write_text("secret material")
        sneaky = f"{tmp_dirs['nginx_dir']}/pools/../../escape.conf"

        resp = await client.post("/api/config/absorb-file", json={"path": sneaky})

        assert resp.status_code == 400

    async def test_managed_file_is_still_absorbed(self, client, mock_nginx, tmp_dirs):
        pool = await create_pool(client, "absorb-ok")
        await create_member(client, pool["id"], "10.0.0.1:9000")
        conf = Path(tmp_dirs["nginx_dir"]) / "pools" / "absorb-ok.conf"
        conf.write_text(conf.read_text() + "# edited by hand\n")

        resp = await client.post("/api/config/absorb-file", json={"path": str(conf)})

        assert resp.status_code == 200
        status = (await client.get("/api/config/status")).json()
        assert [f["drifted"] for f in status["files"] if f["path"] == str(conf)] == [False]


class TestSettingsApi:
    @pytest.mark.parametrize("key", ["rclone_path", "db_path", "db_backup_path", "nginx_config_dir"])
    async def test_host_only_settings_cannot_be_changed_through_the_api(self, client, key):
        resp = await client.put(f"/api/config/settings/{key}", json={"value": "/tmp/evil"})

        assert resp.status_code == 403
        assert "config set" in resp.json()["detail"]

    async def test_host_only_also_refused_under_the_env_var_name(self, client):
        resp = await client.put("/api/config/settings/NANIO_ORCHESTRATOR_RCLONE_PATH", json={"value": "/tmp/evil"})
        assert resp.status_code == 403

    @pytest.mark.parametrize("value", ["info\nNANIO_ORCHESTRATOR_RCLONE_PATH=/tmp/evil", "info\r", "in\x00fo"])
    async def test_value_cannot_smuggle_a_second_setting(self, client, value):
        """The config file holds one setting per line."""
        resp = await client.put("/api/config/settings/log_level", json={"value": value})

        assert resp.status_code == 422

    async def test_ordinary_setting_is_still_written(self, client, tmp_path, monkeypatch):
        cfg = tmp_path / "config.env"
        monkeypatch.setattr("nanio_orchestrator.cli._get_config_path", lambda: str(cfg))

        resp = await client.put("/api/config/settings/bucket_sync_interval", json={"value": "120"})

        assert resp.status_code == 200
        assert cfg.read_text() == "NANIO_ORCHESTRATOR_BUCKET_SYNC_INTERVAL=120\n"

    async def test_settings_listing_names_the_host_only_keys(self, client):
        data = (await client.get("/api/config/settings")).json()
        assert data["host_only"] == ["db_backup_path", "db_path", "nginx_config_dir", "rclone_path"]


class TestNodeConfigSecrets:
    async def test_generated_config_is_not_stored(self, client, mock_nginx):
        from nanio_orchestrator.db import get_db_ctx

        pool = await create_pool(client, "node-secret")
        member = await create_member(client, pool["id"], "10.0.0.1:9000")

        resp = await client.post(
            f"/api/pools/{pool['id']}/members/{member['id']}/node-config",
            json={"node_type": "nanio-only", "access_key": "AK", "secret_key": "super-secret"},
        )

        assert resp.status_code == 200
        assert "super-secret" in resp.text  # returned to the caller once...
        async with get_db_ctx() as db:  # ...and kept nowhere: the table that held them is gone
            tables = await db.execute_fetchall("SELECT name FROM sqlite_master WHERE name = 'node_configs'")
        assert tables == []

    async def test_secret_cannot_be_sent_in_the_url(self, client, mock_nginx):
        pool = await create_pool(client, "node-get")
        member = await create_member(client, pool["id"], "10.0.0.1:9000")

        resp = await client.get(
            f"/api/pools/{pool['id']}/members/{member['id']}/node-config?type=nanio-only&secret_key=super-secret"
        )

        assert resp.status_code == 405

    async def test_table_left_by_older_versions_is_dropped_on_startup(self, client, mock_nginx):
        from nanio_orchestrator.db import get_db_ctx, init_db

        async with get_db_ctx() as db:
            await db.execute("CREATE TABLE node_configs (id INTEGER PRIMARY KEY, member_id INTEGER, config_json TEXT)")
            await db.execute("INSERT INTO node_configs (member_id, config_json) VALUES (1, 'secret_key = \"super-secret\"')")
            await db.commit()

        await init_db()

        async with get_db_ctx() as db:
            tables = await db.execute_fetchall("SELECT name FROM sqlite_master WHERE name = 'node_configs'")
        assert tables == []
