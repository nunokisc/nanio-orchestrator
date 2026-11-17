"""Config API: status, validate, reload, rebuild, per-file drift resolution, previews, backup,
settings and the service restart."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from nanio_orchestrator.api import config as config_api

from tests.api_state import audit, conf, managed_files, refuse, sql
from tests.conftest import create_member, create_pool, create_vhost

HAND_EDIT = "# edited by hand\n"


async def _cluster(client):
    pool = await create_pool(client, "pa")
    await create_member(client, pool["id"], "10.0.0.1:9000")
    vhost = await create_vhost(client, "s3.example.com", default_pool_id=pool["id"])
    return pool, vhost


async def _status(client):
    resp = await client.get("/api/config/status")
    assert resp.status_code == 200
    return resp.json()


async def _drifted(client):
    return {Path(f["path"]).name: f["drifted"] for f in (await _status(client))["files"]}


def _edit_by_hand(path):
    path.write_text(path.read_text() + HAND_EDIT)


class TestStatus:
    async def test_nothing_managed_yet(self, client):
        assert await _status(client) == {
            "files": [],
            "last_reload_ok": None,
            "last_reload_at": None,
            "pending_reload": False,
            "pending_files": [],
            "last_applied_at": None,
        }

    async def test_reports_edited_and_missing_files(self, client, mock_nginx, tmp_dirs):
        await _cluster(client)
        pool_conf = conf(tmp_dirs, "pools", "pa")
        vhost_conf = conf(tmp_dirs, "vhosts", "s3.example.com")
        _edit_by_hand(pool_conf)
        vhost_conf.unlink()

        status = await _status(client)

        by_name = {Path(f["path"]).name: f for f in status["files"]}
        assert by_name["pa.conf"]["drifted"] is True
        assert by_name["pa.conf"]["sha256_disk"] != by_name["pa.conf"]["sha256_db"]
        # a file that is gone is not "drifted": there is nothing on disk to compare
        assert (by_name["s3.example.com.conf"]["sha256_disk"], by_name["s3.example.com.conf"]["drifted"]) == (
            None,
            False,
        )
        assert status["last_reload_ok"] is True


class TestValidate:
    async def test_answers_what_nginx_says_and_changes_nothing(self, client, mock_nginx, tmp_dirs):
        await _cluster(client)
        files = managed_files(tmp_dirs)

        # the endpoint holds its own reference to the executor
        verdicts = [MagicMock(ok=True, output="syntax ok"), MagicMock(ok=False, output="[emerg] unknown directive")]
        with patch("nanio_orchestrator.api.config.test_config", new=AsyncMock(side_effect=verdicts)):
            accepted = await client.post("/api/config/validate")
            refused = await client.post("/api/config/validate")

        assert accepted.json() == {"ok": True, "output": "syntax ok"}
        assert (refused.status_code, refused.json()) == (200, {"ok": False, "output": "[emerg] unknown directive"})
        assert managed_files(tmp_dirs) == files
        mock_nginx["reload_nginx"].assert_not_awaited()


class TestReload:
    async def test_reloads_nginx_and_records_the_outcome(self, client, mock_nginx):
        resp = await client.post("/api/config/reload")

        assert resp.json() == {"ok": True, "output": "reloaded"}
        mock_nginx["reload_nginx"].assert_awaited_once()
        (entry,) = await audit(client, "manual_reload")
        assert (entry["nginx_reload_ok"], entry["nginx_reload_output"]) == (1, "reloaded")
        assert (await _status(client))["last_reload_ok"] is True

    async def test_failed_reload_is_reported_and_recorded(self, client, mock_nginx):
        mock_nginx["reload_nginx"].return_value = MagicMock(ok=False, output="nginx is not running")

        resp = await client.post("/api/config/reload")

        assert (resp.status_code, resp.json()) == (200, {"ok": False, "output": "nginx is not running"})
        (entry,) = await audit(client, "manual_reload")
        assert (entry["nginx_reload_ok"], entry["nginx_reload_output"]) == (0, "nginx is not running")
        assert (await _status(client))["last_reload_ok"] is False


class TestRebuild:
    async def test_rewrites_edited_files_and_removes_the_file_of_an_empty_pool(self, client, mock_nginx, tmp_dirs):
        await _cluster(client)
        await create_pool(client, "empty")
        pool_conf = conf(tmp_dirs, "pools", "pa")
        stray = conf(tmp_dirs, "pools", "empty")
        _edit_by_hand(pool_conf)
        stray.write_text("upstream empty { }\n")

        resp = await client.post("/api/config/rebuild")

        assert resp.status_code == 200
        body = resp.json()
        assert (body["ok"], body["output"], body["errors"]) == (True, "reloaded", [])
        assert sorted(Path(p).name for p in body["written"]) == ["pa.conf", "s3.example.com.conf"]
        assert body["removed"] == [str(stray)]
        assert HAND_EDIT not in pool_conf.read_text()
        assert not stray.exists()
        assert await _drifted(client) == {"pa.conf": False, "s3.example.com.conf": False}
        (entry,) = await audit(client, "rebuild")
        assert entry["nginx_reload_ok"] == 1

    async def test_refused_by_nginx_puts_the_files_back_and_records_nothing(self, client, mock_nginx, tmp_dirs):
        await _cluster(client)
        _edit_by_hand(conf(tmp_dirs, "pools", "pa"))
        files = managed_files(tmp_dirs)
        refuse(mock_nginx, "[emerg] host not found in upstream")

        resp = await client.post("/api/config/rebuild")

        assert resp.status_code == 200
        assert resp.json() == {
            "ok": False,
            "output": "[emerg] host not found in upstream",
            "written": [],
            "removed": [],
            "errors": [],
        }
        assert managed_files(tmp_dirs) == files
        assert (await _drifted(client))["pa.conf"] is True
        assert await audit(client, "rebuild") == []
        mock_nginx["reload_nginx"].assert_not_awaited()

    async def test_failed_reload_puts_the_files_back_and_is_recorded(self, client, mock_nginx, tmp_dirs):
        await _cluster(client)
        _edit_by_hand(conf(tmp_dirs, "pools", "pa"))
        files = managed_files(tmp_dirs)
        mock_nginx["reload_nginx"].return_value = MagicMock(ok=False, output="nginx is not running")

        resp = await client.post("/api/config/rebuild")

        assert (resp.json()["ok"], resp.json()["output"], resp.json()["written"]) == (
            False,
            "nginx is not running",
            [],
        )
        assert managed_files(tmp_dirs) == files
        (entry,) = await audit(client, "rebuild")
        assert (entry["nginx_reload_ok"], entry["nginx_reload_output"]) == (0, "nginx is not running")

    async def test_disk_error_is_reported_not_raised(self, client, mock_nginx, tmp_dirs):
        await _cluster(client)
        files = managed_files(tmp_dirs)

        with patch(
            "nanio_orchestrator.nginx.apply._write_bytes_atomic", side_effect=OSError("No space left on device")
        ):
            resp = await client.post("/api/config/rebuild")

        assert resp.status_code == 200
        assert resp.json() == {
            "ok": False,
            "output": "No space left on device",
            "written": [],
            "removed": [],
            "errors": ["No space left on device"],
        }
        assert managed_files(tmp_dirs) == files
        assert await audit(client, "rebuild") == []
        mock_nginx["reload_nginx"].assert_not_awaited()


class TestAbsorbFile:
    async def test_path_that_cannot_be_resolved_is_refused(self, client, tmp_dirs):
        resp = await client.post("/api/config/absorb-file", json={"path": "/etc/ng\x00inx/pools/pa.conf"})

        assert resp.status_code == 400
        assert await sql("SELECT path FROM config_files") == []

    async def test_managed_file_that_is_not_on_disk(self, client, tmp_dirs):
        missing = conf(tmp_dirs, "pools", "ghost")

        resp = await client.post("/api/config/absorb-file", json={"path": str(missing)})

        assert resp.status_code == 404
        assert resp.json()["detail"] == f"File not found on disk: {missing}"
        assert await sql("SELECT path FROM config_files") == []
        assert await audit(client, "absorb_drift") == []


class TestRewriteFile:
    async def _rewrite(self, client, path):
        resp = await client.post("/api/config/rewrite-file", json={"path": str(path)})
        assert resp.status_code == 200, resp.text
        return resp.json()

    async def test_path_that_is_no_pool_or_vhost_file(self, client, mock_nginx, tmp_dirs):
        await _cluster(client)
        files = managed_files(tmp_dirs)

        resp = await client.post("/api/config/rewrite-file", json={"path": "/etc/passwd"})

        assert resp.status_code == 404
        assert managed_files(tmp_dirs) == files
        mock_nginx["reload_nginx"].assert_not_awaited()

    @pytest.mark.parametrize(("kind", "name"), [("pools", "pa"), ("vhosts", "s3.example.com")])
    async def test_edited_file_is_written_again_from_the_database_and_nginx_reloaded(
        self, client, mock_nginx, tmp_dirs, kind, name
    ):
        await _cluster(client)
        path = conf(tmp_dirs, kind, name)
        _edit_by_hand(path)
        assert (await _drifted(client))[path.name] is True

        result = await self._rewrite(client, path)

        assert result == {"ok": True, "output": "reloaded", "path": str(path)}
        assert HAND_EDIT not in path.read_text()
        assert name in path.read_text()
        assert (await _drifted(client))[path.name] is False
        mock_nginx["reload_nginx"].assert_awaited_once()
        (entry,) = await audit(client, "rewrite_file")
        assert entry["nginx_reload_ok"] == 1

    async def test_refused_by_nginx_keeps_the_edited_file(self, client, mock_nginx, tmp_dirs):
        await _cluster(client)
        path = conf(tmp_dirs, "pools", "pa")
        _edit_by_hand(path)
        files = managed_files(tmp_dirs)
        refuse(mock_nginx, "[emerg] invalid upstream")

        result = await self._rewrite(client, path)

        assert result == {"ok": False, "output": "[emerg] invalid upstream"}
        assert managed_files(tmp_dirs) == files
        assert (await _drifted(client))["pa.conf"] is True
        assert await audit(client, "rewrite_file") == []
        mock_nginx["reload_nginx"].assert_not_awaited()

    async def test_failed_reload_keeps_the_edited_file_and_is_recorded(self, client, mock_nginx, tmp_dirs):
        await _cluster(client)
        path = conf(tmp_dirs, "pools", "pa")
        _edit_by_hand(path)
        files = managed_files(tmp_dirs)
        mock_nginx["reload_nginx"].return_value = MagicMock(ok=False, output="nginx is not running")

        result = await self._rewrite(client, path)

        assert result == {"ok": False, "output": "nginx is not running", "path": str(path)}
        assert managed_files(tmp_dirs) == files
        assert (await _drifted(client))["pa.conf"] is True
        (entry,) = await audit(client, "rewrite_file")
        assert (entry["nginx_reload_ok"], entry["nginx_reload_output"]) == (0, "nginx is not running")

    async def test_file_of_a_pool_without_members_is_removed(self, client, mock_nginx, tmp_dirs):
        await create_pool(client, "empty")
        path = conf(tmp_dirs, "pools", "empty")
        path.write_text("upstream empty { }\n")
        assert (await client.post("/api/config/absorb-file", json={"path": str(path)})).status_code == 200

        result = await self._rewrite(client, path)

        assert result == {"ok": True, "action": "removed", "path": str(path), "reason": "Pool has no members"}
        assert not path.exists()
        assert await sql("SELECT path FROM config_files") == []

        # asking again, with the file already gone, is not an error
        assert (await self._rewrite(client, path))["action"] == "removed"

    async def test_file_of_a_pool_without_members_stays_when_nginx_still_needs_it(self, client, mock_nginx, tmp_dirs):
        await create_pool(client, "empty")
        path = conf(tmp_dirs, "pools", "empty")
        path.write_text("upstream empty { }\n")
        assert (await client.post("/api/config/absorb-file", json={"path": str(path)})).status_code == 200
        refuse(mock_nginx, '[emerg] host not found in upstream "empty"')

        result = await self._rewrite(client, path)

        assert result == {"ok": False, "output": '[emerg] host not found in upstream "empty"'}
        assert path.read_text() == "upstream empty { }\n"
        assert await sql("SELECT path FROM config_files") == [{"path": str(path)}]


class TestPreview:
    async def test_unknown_pool_and_vhost(self, client):
        assert (await client.get("/api/config/preview/pool/999")).status_code == 404
        assert (await client.get("/api/config/preview/vhost/999")).status_code == 404

    async def test_renders_without_writing(self, client, mock_nginx, tmp_dirs):
        pool, vhost = await _cluster(client)
        empty = await create_pool(client, "empty")
        files = managed_files(tmp_dirs)
        mock_nginx["test_config"].reset_mock()

        pool_preview = (await client.get(f"/api/config/preview/pool/{pool['id']}")).json()
        empty_preview = (await client.get(f"/api/config/preview/pool/{empty['id']}")).json()
        vhost_preview = (await client.get(f"/api/config/preview/vhost/{vhost['id']}")).json()

        assert pool_preview["filepath"] == str(conf(tmp_dirs, "pools", "pa"))
        assert "upstream pa {" in pool_preview["content"] and "server 10.0.0.1:9000" in pool_preview["content"]
        # a pool without members has no upstream block to show
        assert empty_preview == {"filepath": str(conf(tmp_dirs, "pools", "empty")), "content": None}
        assert vhost_preview["filepath"] == str(conf(tmp_dirs, "vhosts", "s3.example.com"))
        assert "proxy_pass         http://pa;" in vhost_preview["content"]
        assert managed_files(tmp_dirs) == files
        mock_nginx["test_config"].assert_not_awaited()


class TestRebuildFromDisk:
    async def test_refused_on_a_database_with_data_unless_forced(self, client, mock_nginx, mock_s3, tmp_dirs):
        pool, vhost = await _cluster(client)

        refused = await client.post("/api/config/rebuild-from-disk")

        assert refused.status_code == 409
        assert "force=true" in refused.json()["detail"]
        assert (await client.get(f"/api/pools/{pool['id']}")).json() == pool
        assert (await client.get(f"/api/vhosts/{vhost['id']}")).json() == vhost

    async def test_dry_run_reports_and_writes_nothing(self, client, mock_nginx, mock_s3, tmp_dirs):
        pool, _vhost = await _cluster(client)
        await sql("DELETE FROM routes")
        await sql("DELETE FROM vhosts")
        files = managed_files(tmp_dirs)

        resp = await client.post("/api/config/rebuild-from-disk?dry_run=true")

        assert resp.status_code == 200
        # the vhost is still on disk, and stays out of the database
        assert (await client.get("/api/vhosts")).json() == []
        assert (await client.get("/api/pools")).json() == [pool]
        assert managed_files(tmp_dirs) == files


class TestBackup:
    async def test_backup_is_written_next_to_the_database(self, client, tmp_dirs):
        await create_pool(client, "pa")

        resp = await client.post("/api/config/backup")

        assert resp.json() == {"ok": True, "backup_path": tmp_dirs["db_path"] + ".bak"}
        assert Path(tmp_dirs["db_path"] + ".bak").stat().st_size > 0

    async def test_failed_backup_is_reported(self, client):
        with patch("nanio_orchestrator.backup.backup_database", new=AsyncMock(return_value=None)):
            resp = await client.post("/api/config/backup")

        assert (resp.status_code, resp.json()) == (200, {"ok": False, "detail": "Backup failed"})


class TestSettings:
    @pytest.fixture
    def config_file(self, tmp_path, monkeypatch):
        path = tmp_path / "config.env"
        monkeypatch.setattr("nanio_orchestrator.cli._get_config_path", lambda: str(path))
        return path

    async def test_secrets_are_masked_in_the_listing(self, client, set_secret):
        data = (await client.get("/api/config/settings")).json()

        assert data["api_key"] == "test****"
        assert len(data["secret"]) == 8 and data["secret"].endswith("****")
        assert data["log_level"] == "warning"

    async def test_secret_that_is_not_set_is_listed_as_null(self, client):
        assert (await client.get("/api/config/settings")).json()["secret"] is None

    async def test_unknown_setting(self, client, config_file):
        resp = await client.put("/api/config/settings/no_such_setting", json={"value": "1"})

        assert resp.status_code == 404
        assert not config_file.exists()

    async def test_value_is_required(self, client, config_file):
        resp = await client.put("/api/config/settings/log_level", json={"valeu": "info"})

        assert resp.status_code == 422
        assert resp.json()["detail"] == "Missing 'value' field"
        assert not config_file.exists()

    @pytest.mark.parametrize(("value", "shown"), [("a-long-new-key", "a-lo****"), ("abc", "****")])
    async def test_secret_is_written_in_full_and_echoed_masked(self, client, config_file, value, shown):
        resp = await client.put("/api/config/settings/NANIO_ORCHESTRATOR_API_KEY", json={"value": value})

        assert resp.json() == {"ok": True, "key": "api_key", "value": shown, "restart_required": True}
        assert config_file.read_text() == f"NANIO_ORCHESTRATOR_API_KEY={value}\n"

    async def test_config_file_that_cannot_be_written(self, client, tmp_path, monkeypatch):
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("")
        monkeypatch.setattr("nanio_orchestrator.cli._get_config_path", lambda: str(blocker / "config.env"))

        resp = await client.put("/api/config/settings/log_level", json={"value": "info"})

        assert resp.status_code == 500
        assert resp.json()["detail"].startswith("Failed to write config: ")


class TestRestart:
    async def test_not_available_in_dev_mode(self, client):
        with patch("subprocess.Popen") as popen:
            resp = await client.post("/api/config/settings/restart")

        assert resp.status_code == 503
        popen.assert_not_called()
        assert config_api._restart_tasks == set()

    async def test_restart_is_scheduled_after_the_response(self, client):
        real_sleep = asyncio.sleep
        delays = []

        async def sleep(delay):
            delays.append(delay)
            await real_sleep(0)

        with (
            patch("nanio_orchestrator.config.DEV_MODE", False),
            patch("shutil.which", return_value="/bin/systemctl"),
            patch("asyncio.sleep", new=sleep),
            patch("subprocess.Popen") as popen,
        ):
            resp = await client.post("/api/config/settings/restart")
            # answered before anything is restarted
            assert (resp.status_code, resp.json()) == (200, {"ok": True, "message": "Service restart scheduled"})
            await asyncio.gather(*config_api._restart_tasks)
            await real_sleep(0)

        assert delays == [1.5]
        assert popen.call_args.args == (["sudo", "/bin/systemctl", "restart", "nanio-orchestrator"],)
        assert config_api._restart_tasks == set()


async def test_rebuild_from_disk_without_force_runs_on_an_empty_database(client, mock_nginx, mock_s3, tmp_dirs):
    """`force` is only needed to replace existing data; a fresh database is simply filled."""
    from pathlib import Path

    from nanio_orchestrator.nginx.generator import render_upstream

    pool = {"id": 7, "name": "restored", "type": "nanio", "lb_method": "least_conn", "keepalive": 32}
    member = {
        "address": "10.0.0.1:9000",
        "role": "active",
        "weight": 1,
        "max_fails": 3,
        "fail_timeout_s": 30,
        "enabled": 1,
    }
    (Path(tmp_dirs["nginx_dir"]) / "pools" / "restored.conf").write_text(render_upstream(pool, [member]))

    resp = await client.post("/api/config/rebuild-from-disk")

    assert resp.status_code == 200, resp.text
    assert resp.json()["pools_imported"] == 1
    assert [p["name"] for p in (await client.get("/api/pools")).json()] == ["restored"]
