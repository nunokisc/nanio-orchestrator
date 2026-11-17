"""The shared nginx write path: write → nginx -t → (reload) → rollback on failure."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from nanio_orchestrator.nginx.apply import (
    apply_config_file,
    apply_config_files,
    nginx_lock,
    rebuild_all_configs,
    reload_nginx_locked,
    remove_config_file_checked,
    write_vhost_config,
)

OK = MagicMock(ok=True, output="syntax ok")
FAIL = MagicMock(ok=False, output="syntax error")


def _nginx(test=OK, reload=OK):
    test_mock = AsyncMock(return_value=test)
    reload_mock = AsyncMock(return_value=reload)
    return (
        patch("nanio_orchestrator.nginx.executor.test_config", test_mock),
        patch("nanio_orchestrator.nginx.executor.reload_nginx", reload_mock),
        test_mock,
        reload_mock,
    )


class TestApplyConfig:
    async def test_candidate_is_in_place_when_nginx_tests_it(self, tmp_path):
        """nginx only includes *.conf — the new content must be at the final path during nginx -t."""
        conf = tmp_path / "a.conf"
        conf.write_text("old")
        seen = {}

        async def fake_test():
            seen["content"] = conf.read_text()
            seen["leftovers"] = sorted(p.name for p in tmp_path.iterdir())
            return OK

        with patch("nanio_orchestrator.nginx.executor.test_config", fake_test):
            result = await apply_config_file(str(conf), "new")

        assert result.ok and result.stage == "test"
        assert seen == {"content": "new", "leftovers": ["a.conf"]}
        assert conf.read_text() == "new"

    async def test_failed_test_restores_previous_content(self, tmp_path):
        conf = tmp_path / "a.conf"
        conf.write_text("old")
        p_test, p_reload, _, reload_mock = _nginx(test=FAIL)
        with p_test, p_reload:
            result = await apply_config_file(str(conf), "broken", reload=True)

        assert not result.ok and result.stage == "test" and result.output == "syntax error"
        assert conf.read_text() == "old"
        reload_mock.assert_not_called()

    async def test_failed_test_removes_file_that_did_not_exist(self, tmp_path):
        conf = tmp_path / "new.conf"
        p_test, p_reload, _, _ = _nginx(test=FAIL)
        with p_test, p_reload:
            result = await apply_config_file(str(conf), "broken")

        assert not result.ok
        assert list(tmp_path.iterdir()) == []

    async def test_failed_reload_restores_previous_content(self, tmp_path):
        conf = tmp_path / "a.conf"
        conf.write_text("old")
        p_test, p_reload, _, _ = _nginx(reload=MagicMock(ok=False, output="nginx is not running"))
        with p_test, p_reload:
            result = await apply_config_file(str(conf), "new", reload=True)

        assert not result.ok and result.stage == "reload"
        assert conf.read_text() == "old"

    async def test_no_reload_unless_requested(self, tmp_path):
        p_test, p_reload, test_mock, reload_mock = _nginx()
        with p_test, p_reload:
            await apply_config_file(str(tmp_path / "a.conf"), "new")
        test_mock.assert_awaited_once()
        reload_mock.assert_not_called()

    async def test_multi_file_failure_restores_every_file(self, tmp_path):
        a, b, c = tmp_path / "a.conf", tmp_path / "b.conf", tmp_path / "c.conf"
        a.write_text("old-a")
        b.write_text("old-b")
        p_test, p_reload, _, _ = _nginx(test=FAIL)
        with p_test, p_reload:
            result = await apply_config_files([(str(a), "new-a"), (str(b), "new-b"), (str(c), "new-c")])

        assert not result.ok
        assert a.read_text() == "old-a" and b.read_text() == "old-b"
        assert not c.exists()

    async def test_cancellation_restores_previous_content(self, tmp_path):
        """A migration cancelled mid-apply must not leave an unvalidated file behind."""
        conf = tmp_path / "a.conf"
        conf.write_text("old")
        started = asyncio.Event()

        async def hanging_test():
            started.set()
            await asyncio.sleep(60)

        with patch("nanio_orchestrator.nginx.executor.test_config", hanging_test):
            task = asyncio.create_task(apply_config_file(str(conf), "new"))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert conf.read_text() == "old"

    async def test_applies_are_serialized(self, tmp_path):
        """A second writer must not be validated against the first writer's in-flight file."""
        a, b = tmp_path / "a.conf", tmp_path / "b.conf"
        in_test = 0
        overlap = False

        async def slow_test():
            nonlocal in_test, overlap
            in_test += 1
            overlap = overlap or in_test > 1
            await asyncio.sleep(0.01)
            in_test -= 1
            return OK

        with patch("nanio_orchestrator.nginx.executor.test_config", slow_test):
            await asyncio.gather(apply_config_file(str(a), "a"), apply_config_file(str(b), "b"))

        assert not overlap
        async with nginx_lock():  # released afterwards
            pass


class TestRestore:
    async def test_a_file_that_cannot_be_restored_does_not_stop_the_others(self, tmp_path, caplog):
        lost_dir = tmp_path / "lost"
        lost_dir.mkdir()
        lost, kept = lost_dir / "a.conf", tmp_path / "b.conf"
        lost.write_text("old-a")
        kept.write_text("old-b")

        async def failing_test():
            lost.unlink()
            lost_dir.rmdir()  # the previous content of a.conf has nowhere to go back to
            return FAIL

        with patch("nanio_orchestrator.nginx.executor.test_config", failing_test):
            result = await apply_config_files([(str(lost), "new-a"), (str(kept), "new-b")])

        assert not result.ok
        assert kept.read_text() == "old-b"
        assert any("Could not restore" in r.getMessage() and str(lost) in r.getMessage() for r in caplog.records)

    async def test_new_file_that_already_disappeared_needs_no_cleanup(self, tmp_path):
        conf = tmp_path / "new.conf"

        async def failing_test():
            conf.unlink()
            return FAIL

        with patch("nanio_orchestrator.nginx.executor.test_config", failing_test):
            result = await apply_config_file(str(conf), "broken")

        assert not result.ok
        assert list(tmp_path.iterdir()) == []

    async def test_error_while_testing_restores_and_is_raised(self, tmp_path):
        conf = tmp_path / "a.conf"
        conf.write_text("old")
        failing = AsyncMock(side_effect=RuntimeError("no nginx"))
        with patch("nanio_orchestrator.nginx.executor.test_config", failing), pytest.raises(RuntimeError):
            await apply_config_file(str(conf), "new")
        assert conf.read_text() == "old"


class TestRemoveChecked:
    async def test_missing_file_is_nothing_to_remove(self, tmp_path):
        p_test, p_reload, test_mock, _ = _nginx()
        with p_test, p_reload:
            result = await remove_config_file_checked(str(tmp_path / "gone.conf"))
        assert (result.ok, result.stage, result.output) == (True, "test", "nothing to remove")
        test_mock.assert_not_called()

    async def test_file_is_gone_when_nginx_tests_the_tree_and_stays_gone(self, tmp_path):
        conf = tmp_path / "pool.conf"
        conf.write_text("upstream pool {}")
        seen = {}

        async def fake_test():
            seen["exists"] = conf.exists()
            return OK

        with patch("nanio_orchestrator.nginx.executor.test_config", fake_test):
            result = await remove_config_file_checked(str(conf))

        assert (result.ok, result.stage, result.output) == (True, "test", "syntax ok")
        assert seen == {"exists": False}
        assert not conf.exists()

    async def test_upstream_still_in_use_is_put_back(self, tmp_path):
        conf = tmp_path / "pool.conf"
        conf.write_text("upstream pool {}")
        p_test, p_reload, _, reload_mock = _nginx(test=FAIL)
        with p_test, p_reload:
            result = await remove_config_file_checked(str(conf))

        assert (result.ok, result.stage, result.output) == (False, "test", "syntax error")
        assert conf.read_text() == "upstream pool {}"
        reload_mock.assert_not_called()

    async def test_cancellation_puts_the_file_back(self, tmp_path):
        conf = tmp_path / "pool.conf"
        conf.write_text("upstream pool {}")
        started = asyncio.Event()

        async def hanging_test():
            started.set()
            await asyncio.sleep(60)

        with patch("nanio_orchestrator.nginx.executor.test_config", hanging_test):
            task = asyncio.create_task(remove_config_file_checked(str(conf)))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert conf.read_text() == "upstream pool {}"


class TestReloadLocked:
    async def test_returns_what_nginx_answered(self):
        answer = MagicMock(ok=False, output="nginx is not running")
        p_test, p_reload, test_mock, reload_mock = _nginx(reload=answer)
        with p_test, p_reload:
            assert await reload_nginx_locked() is answer
        reload_mock.assert_awaited_once()
        test_mock.assert_not_called()

    async def test_waits_for_a_write_that_is_still_being_validated(self):
        p_test, p_reload, _, reload_mock = _nginx()
        with p_test, p_reload:
            async with nginx_lock():
                task = asyncio.create_task(reload_nginx_locked())
                await asyncio.sleep(0.01)
                reload_mock.assert_not_called()
            await task
        reload_mock.assert_awaited_once()


@pytest_asyncio.fixture
async def cluster(tmp_dirs, monkeypatch):
    """Two pools (one without members) and a vhost, in the database only."""
    import nanio_orchestrator.config as cfg_mod
    import nanio_orchestrator.db as db_mod

    monkeypatch.setenv("NANIO_ORCHESTRATOR_DB_PATH", tmp_dirs["db_path"])
    monkeypatch.setenv("NANIO_ORCHESTRATOR_NGINX_CONFIG_DIR", tmp_dirs["nginx_dir"])
    cfg_mod.settings = None
    db_mod.set_db_path(tmp_dirs["db_path"])
    await db_mod.init_db()
    async with db_mod.get_db_ctx() as db:
        await db.execute("INSERT INTO pools (id, name, description) VALUES (1, 'hot', 'fast'), (2, 'empty', NULL)")
        await db.execute("INSERT INTO pool_members (pool_id, address) VALUES (1, '10.0.0.1:9000')")
        await db.execute(
            """INSERT INTO vhosts (id, server_name, listen_port, ssl, default_pool_id, ip_rule_mode, ip_rule_ips_json)
               VALUES (1, 's3.example.com', 80, 0, 1, 'allow', '["10.0.0.0/8"]')"""
        )
        await db.execute("INSERT INTO routes (vhost_id, path_prefix, pool_id) VALUES (1, '/photos/', 1)")
        await db.commit()
        nginx_dir = Path(tmp_dirs["nginx_dir"])
        yield {
            "db": db,
            "pool": nginx_dir / "pools" / "hot.conf",
            "empty": nginx_dir / "pools" / "empty.conf",
            "vhost": nginx_dir / "vhosts" / "s3.example.com.conf",
        }
    cfg_mod.settings = None
    db_mod._db_path = None


async def _recorded(db) -> dict:
    rows = await db.execute_fetchall("SELECT path, content_snapshot FROM config_files")
    return {Path(r["path"]).name: r["content_snapshot"] for r in rows}


class TestWriteVhostConfig:
    async def test_accepted_config_is_written_recorded_and_not_reloaded(self, cluster):
        p_test, p_reload, test_mock, reload_mock = _nginx()
        with p_test, p_reload:
            ok, output = await write_vhost_config(1, cluster["db"])

        assert ok and output == "nginx -t: syntax ok (config written — apply with Reload nginx)"
        content = cluster["vhost"].read_text()
        assert "location /photos/ {" in content
        assert await _recorded(cluster["db"]) == {"s3.example.com.conf": content}
        test_mock.assert_awaited_once()
        reload_mock.assert_not_called()

    async def test_rejected_config_is_neither_left_on_disk_nor_recorded(self, cluster):
        cluster["vhost"].write_text("previous")
        p_test, p_reload, _, _ = _nginx(test=FAIL)
        with p_test, p_reload:
            ok, output = await write_vhost_config(1, cluster["db"])

        assert (ok, output) == (False, "syntax error")
        assert cluster["vhost"].read_text() == "previous"
        assert await _recorded(cluster["db"]) == {}


class TestRebuildAllConfigs:
    async def test_every_file_and_sidecar_is_regenerated_and_nginx_reloaded(self, cluster):
        p_test, p_reload, _, reload_mock = _nginx(reload=MagicMock(ok=True, output="reloaded"))
        with p_test, p_reload:
            result = await rebuild_all_configs()

        assert (result["ok"], result["stage"], result["output"]) == (True, "reload", "reloaded")
        assert sorted(result["written"]) == sorted([str(cluster["pool"]), str(cluster["vhost"])])
        assert result["removed"] == []
        reload_mock.assert_awaited_once()
        assert "upstream hot {" in cluster["pool"].read_text()
        assert not cluster["empty"].exists()
        assert await _recorded(cluster["db"]) == {
            "hot.conf": cluster["pool"].read_text(),
            "s3.example.com.conf": cluster["vhost"].read_text(),
        }

        pool_sidecar = json.loads(cluster["pool"].with_name("hot.meta.json").read_text())
        assert (pool_sidecar["name"], pool_sidecar["type"], pool_sidecar["description"]) == ("hot", "nanio", "fast")
        assert cluster["empty"].with_name("empty.meta.json").exists()  # the pool exists, only its upstream does not
        vhost_sidecar = json.loads(cluster["vhost"].with_name("s3.example.com.meta.json").read_text())
        assert (vhost_sidecar["default_pool_name"], vhost_sidecar["ip_rule_mode"]) == ("hot", "allow")
        assert vhost_sidecar["ip_rule_ips_json"] == '["10.0.0.0/8"]'

    async def test_upstream_file_of_a_pool_without_members_is_removed(self, cluster):
        cluster["empty"].write_text("# managed by nanio-orchestrator\nupstream empty {\n    server 10.0.0.9:80;\n}\n")
        await cluster["db"].execute(
            "INSERT INTO config_files (path, sha256_disk, sha256_db) VALUES (?, 'x', 'x')", (str(cluster["empty"]),)
        )
        await cluster["db"].commit()

        p_test, p_reload, _, _ = _nginx()
        with p_test, p_reload:
            result = await rebuild_all_configs()

        assert result["ok"]
        assert result["removed"] == [str(cluster["empty"])]
        assert not cluster["empty"].exists()
        assert "empty.conf" not in await _recorded(cluster["db"])

    @pytest.mark.parametrize(
        ("nginx", "stage", "output"),
        [
            ({"test": FAIL}, "test", "syntax error"),
            ({"reload": MagicMock(ok=False, output="nginx is not running")}, "reload", "nginx is not running"),
        ],
    )
    async def test_rejected_tree_keeps_the_previous_files_and_records_nothing(self, cluster, nginx, stage, output):
        cluster["pool"].write_text("previous pool")
        p_test, p_reload, _, _ = _nginx(**nginx)
        with p_test, p_reload:
            result = await rebuild_all_configs()

        assert result == {"ok": False, "stage": stage, "output": output, "written": [], "removed": []}
        assert cluster["pool"].read_text() == "previous pool"
        assert not cluster["vhost"].exists()
        assert await _recorded(cluster["db"]) == {}
        assert not cluster["pool"].with_name("hot.meta.json").exists()
