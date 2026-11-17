"""Changes written but not applied: what differs from the files nginx last loaded."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from nanio_orchestrator.nginx import applied, executor


@pytest.fixture
def tree(app, tmp_dirs):
    """The managed config directory of the test app."""
    return Path(tmp_dirs["nginx_dir"])


def write(tree: Path, relative: str, content: str) -> str:
    path = tree / relative
    path.write_text(content)
    return str(path)


class TestPendingChanges:
    def test_nothing_on_disk_and_no_reload_yet_is_not_pending(self, tree):
        assert applied.pending_changes() == {"pending_reload": False, "pending_files": [], "last_applied_at": None}

    def test_files_without_a_recorded_reload_cannot_be_judged(self, tree):
        write(tree, "pools/a.conf", "upstream a {}")
        assert applied.pending_changes() == {"pending_reload": None, "pending_files": [], "last_applied_at": None}

    def test_right_after_a_reload_nothing_is_pending(self, tree):
        write(tree, "pools/a.conf", "upstream a {}")
        applied.record_applied()
        state = applied.pending_changes()
        assert state["pending_reload"] is False
        assert state["pending_files"] == []
        assert state["last_applied_at"].endswith("Z")

    def test_added_modified_and_removed_files_are_listed(self, tree):
        kept = write(tree, "pools/kept.conf", "upstream kept {}")
        changed = write(tree, "vhosts/changed.conf", "server { listen 1; }")
        removed = write(tree, "vhosts/removed.conf", "server { listen 2; }")
        applied.record_applied()

        Path(changed).write_text("server { listen 3; }")
        Path(removed).unlink()
        added = write(tree, "pools/added.conf", "upstream added {}")

        state = applied.pending_changes()
        assert state["pending_reload"] is True
        assert state["pending_files"] == sorted(
            [
                {"path": added, "change": "added"},
                {"path": changed, "change": "modified"},
                {"path": removed, "change": "removed"},
            ],
            key=lambda c: c["path"],
        )
        assert kept not in [c["path"] for c in state["pending_files"]]

    def test_a_change_that_is_undone_is_no_longer_pending(self, tree):
        path = write(tree, "pools/a.conf", "upstream a {}")
        applied.record_applied()
        Path(path).write_text("upstream a { server x; }")
        assert applied.pending_changes()["pending_reload"] is True
        Path(path).write_text("upstream a {}")
        assert applied.pending_changes()["pending_reload"] is False

    def test_only_conf_files_of_the_managed_directories_count(self, tree):
        applied.record_applied()
        write(tree, "pools/a.meta.json", "{}")
        write(tree, "pools/a.conf.tmp", "half written")
        write(tree, "other.conf", "not included by nginx")
        assert applied.pending_changes()["pending_reload"] is False

    @pytest.mark.parametrize("content", ["not json", "[]", '{"files": "nope"}'])
    def test_unreadable_record_is_treated_as_no_record(self, tree, content):
        write(tree, "pools/a.conf", "upstream a {}")
        (tree / applied.STATE_FILE).write_text(content)
        assert applied.pending_changes()["pending_reload"] is None

    def test_file_that_disappears_while_being_read_is_skipped(self, tree, monkeypatch):
        gone = write(tree, "pools/gone.conf", "x")
        stays = write(tree, "pools/stays.conf", "y")
        real = Path.read_bytes

        def read_bytes(self):
            if str(self) == gone:
                raise FileNotFoundError(gone)
            return real(self)

        monkeypatch.setattr(Path, "read_bytes", read_bytes)
        assert list(applied.managed_hashes()) == [stays]


class TestRecording:
    def test_record_lists_every_managed_file_and_leaves_no_temporary_file(self, tree):
        a = write(tree, "pools/a.conf", "upstream a {}")
        applied.record_applied()
        state = json.loads((tree / applied.STATE_FILE).read_text())
        assert list(state["files"]) == [a]
        assert len(state["files"][a]) == 64
        assert not (tree / (applied.STATE_FILE + ".tmp")).exists()

    def test_record_that_cannot_be_written_is_logged_not_raised(self, tree, monkeypatch, caplog):
        monkeypatch.setattr(applied, "_state_path", lambda: tree / "missing-dir" / applied.STATE_FILE)
        with caplog.at_level("WARNING", logger=applied.logger.name):
            applied.record_applied()
        assert "Could not record the applied configuration" in caplog.text

    async def test_successful_reload_records_what_nginx_loaded(self, tree):
        """Dev mode: the reload is a dry run that succeeds."""
        write(tree, "vhosts/s3.conf", "server {}")
        assert applied.pending_changes()["pending_reload"] is None
        assert (await executor.reload_nginx()).ok is True
        assert applied.pending_changes()["pending_reload"] is False

    async def test_failed_reload_records_nothing(self, tree, monkeypatch):
        async def failing(cmd):
            return executor.NginxExecResult(ok=False, output="nginx is not running", return_code=1)

        monkeypatch.setattr(executor, "_run_cmd", failing)
        write(tree, "vhosts/s3.conf", "server {}")
        assert (await executor.reload_nginx()).ok is False
        assert not (tree / applied.STATE_FILE).exists()


class TestConfigStatus:
    async def test_change_is_pending_until_nginx_is_reloaded(self, client, tree):
        """The whole loop through the API, with the dev-mode nginx (dry run)."""
        pool = (await client.post("/api/pools", json={"name": "pool-a"})).json()["id"]
        status = (await client.get("/api/config/status")).json()
        assert (status["pending_reload"], status["pending_files"], status["last_applied_at"]) == (False, [], None)

        await client.post(f"/api/pools/{pool}/members", json={"address": "10.0.0.1:9000"})
        assert (await client.get("/api/config/status")).json()["pending_reload"] is None

        assert (await client.post("/api/config/reload")).json()["ok"] is True
        status = (await client.get("/api/config/status")).json()
        assert status["pending_reload"] is False
        assert status["last_applied_at"]

        await client.post(f"/api/pools/{pool}/members", json={"address": "10.0.0.2:9000"})
        status = (await client.get("/api/config/status")).json()
        assert status["pending_reload"] is True
        assert status["pending_files"] == [{"path": str(tree / "pools" / "pool-a.conf"), "change": "modified"}]

        await client.post("/api/config/reload")
        assert (await client.get("/api/config/status")).json()["pending_reload"] is False


class TestReadConfigFile:
    async def test_file_in_sync_is_returned_with_its_recorded_version(self, client, tree):
        pool = (await client.post("/api/pools", json={"name": "pool-a"})).json()["id"]
        await client.post(f"/api/pools/{pool}/members", json={"address": "10.0.0.1:9000"})
        path = str(tree / "pools" / "pool-a.conf")

        body = (await client.get("/api/config/file", params={"path": path})).json()
        assert body["path"] == path
        assert "10.0.0.1:9000" in body["content"]
        assert body["recorded"] == body["content"]
        assert body["drifted"] is False

    async def test_hand_edited_file_shows_both_versions(self, client, tree):
        pool = (await client.post("/api/pools", json={"name": "pool-a"})).json()["id"]
        await client.post(f"/api/pools/{pool}/members", json={"address": "10.0.0.1:9000"})
        path = tree / "pools" / "pool-a.conf"
        path.write_text(path.read_text() + "# edited by hand\n")

        body = (await client.get("/api/config/file", params={"path": str(path)})).json()
        assert body["drifted"] is True
        assert body["content"].endswith("# edited by hand\n")
        assert "edited by hand" not in body["recorded"]

    async def test_file_deleted_by_hand_still_shows_what_was_written(self, client, tree):
        pool = (await client.post("/api/pools", json={"name": "pool-a"})).json()["id"]
        await client.post(f"/api/pools/{pool}/members", json={"address": "10.0.0.1:9000"})
        path = tree / "pools" / "pool-a.conf"
        path.unlink()

        body = (await client.get("/api/config/file", params={"path": str(path)})).json()
        assert body["content"] is None
        assert "10.0.0.1:9000" in body["recorded"]
        assert body["drifted"] is False

    async def test_file_the_orchestrator_never_wrote_has_no_recorded_version(self, client, tree):
        path = write(tree, "vhosts/manual.conf", "server {}")
        body = (await client.get("/api/config/file", params={"path": path})).json()
        assert (body["content"], body["recorded"], body["drifted"]) == ("server {}", None, False)

    async def test_unknown_managed_path_is_a_404(self, client, tree):
        resp = await client.get("/api/config/file", params={"path": str(tree / "pools" / "nope.conf")})
        assert resp.status_code == 404

    @pytest.mark.parametrize("target", ["/etc/passwd", "pools/a.meta.json", ".applied.json", "pools/../../secret.conf"])
    async def test_only_managed_conf_files_can_be_read(self, client, tree, target):
        path = target if target.startswith("/") else str(tree / target)
        resp = await client.get("/api/config/file", params={"path": path})
        assert resp.status_code == 400
