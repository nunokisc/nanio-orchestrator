"""Drift detection and database backup: one pass, and the loops' start/stop behaviour."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, patch

import nanio_orchestrator.config as cfg_mod
import pytest
from nanio_orchestrator import backup, drift
from nanio_orchestrator.config import get_settings


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def settings(tmp_dirs, monkeypatch):
    """Settings pointing at the test database; the singleton is restored afterwards."""
    monkeypatch.setenv("NANIO_ORCHESTRATOR_DB_PATH", tmp_dirs["db_path"])
    monkeypatch.setenv("NANIO_ORCHESTRATOR_NGINX_CONFIG_DIR", tmp_dirs["nginx_dir"])
    monkeypatch.delenv("NANIO_ORCHESTRATOR_DB_BACKUP_PATH", raising=False)
    monkeypatch.setattr(cfg_mod, "settings", None)
    return get_settings()


async def track(db, path, sha256_db, sha256_disk=None) -> None:
    await db.execute(
        "INSERT INTO config_files (path, sha256_db, sha256_disk) VALUES (?, ?, ?)", (str(path), sha256_db, sha256_disk)
    )
    await db.commit()


async def disk_hashes(db) -> dict:
    rows = await db.execute_fetchall("SELECT path, sha256_disk FROM config_files")
    return {Path(r["path"]).name: r["sha256_disk"] for r in rows}


async def wait_until(predicate, timeout: float = 5.0) -> None:
    async def poll():
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(poll(), timeout)


# ── Drift: one pass ───────────────────────────────────────────────────────────


class TestCheckDriftOnce:
    async def test_nothing_tracked_means_no_drift(self, db):
        assert await drift.check_drift_once() == []

    async def test_file_matching_its_recorded_hash_is_in_sync(self, db, tmp_path):
        conf = tmp_path / "a.conf"
        conf.write_text("upstream a {}\n")
        await track(db, conf, sha("upstream a {}\n"))
        assert await drift.check_drift_once() == []
        assert await disk_hashes(db) == {"a.conf": sha("upstream a {}\n")}

    async def test_edited_file_is_reported_as_modified(self, db, tmp_path):
        conf = tmp_path / "a.conf"
        conf.write_text("upstream a {} # edited by hand\n")
        await track(db, conf, sha("upstream a {}\n"))
        assert await drift.check_drift_once() == [
            {
                "path": str(conf),
                "sha256_disk": sha("upstream a {} # edited by hand\n"),
                "sha256_db": sha("upstream a {}\n"),
                "status": "modified",
            }
        ]
        assert await disk_hashes(db) == {"a.conf": sha("upstream a {} # edited by hand\n")}

    async def test_deleted_file_is_reported_as_missing_and_its_disk_hash_cleared(self, db, tmp_path):
        gone = tmp_path / "gone.conf"
        await track(db, gone, sha("x"), sha256_disk=sha("x"))
        assert await drift.check_drift_once() == [
            {"path": str(gone), "sha256_disk": None, "sha256_db": sha("x"), "status": "missing"}
        ]
        assert await disk_hashes(db) == {"gone.conf": None}

    async def test_file_on_disk_that_was_never_generated_is_reported_as_modified(self, db, tmp_path):
        conf = tmp_path / "found.conf"
        conf.write_text("x")
        await track(db, conf, None)
        (entry,) = await drift.check_drift_once()
        assert (entry["status"], entry["sha256_db"], entry["sha256_disk"]) == ("modified", None, sha("x"))

    async def test_row_with_neither_file_nor_hash_is_not_drift(self, db, tmp_path):
        await track(db, tmp_path / "nothing.conf", None)
        assert await drift.check_drift_once() == []

    async def test_unreadable_file_is_skipped_and_its_recorded_state_kept(self, db, tmp_path, caplog):
        unreadable = tmp_path / "dir.conf"
        unreadable.mkdir()  # opening a directory fails with something other than "not found"
        edited = tmp_path / "z.conf"
        edited.write_text("new")
        await track(db, unreadable, sha("x"), sha256_disk="previous")
        await track(db, edited, sha("old"))

        drifted = await drift.check_drift_once()

        assert [d["path"] for d in drifted] == [str(edited)]  # the pass went on to the next file
        assert await disk_hashes(db) == {"dir.conf": "previous", "z.conf": sha("new")}
        assert any("Cannot read" in r.getMessage() and "dir.conf" in r.getMessage() for r in caplog.records)

    async def test_only_the_drifted_files_are_reported(self, db, tmp_path):
        same = tmp_path / "same.conf"
        same.write_text("same")
        changed = tmp_path / "changed.conf"
        changed.write_text("changed")
        await track(db, same, sha("same"))
        await track(db, changed, sha("original"))
        await track(db, tmp_path / "missing.conf", sha("was here"))
        drifted = await drift.check_drift_once()
        assert {Path(d["path"]).name: d["status"] for d in drifted} == {
            "changed.conf": "modified",
            "missing.conf": "missing",
        }


# ── Drift: loop ───────────────────────────────────────────────────────────────


class TestDriftLoop:
    async def test_stop_before_the_loop_ever_ran_is_harmless(self, monkeypatch):
        monkeypatch.setattr(drift, "_stop_event", None)
        drift.stop_drift()

    async def test_loop_checks_at_once_then_waits_and_stops_when_told(self, settings, monkeypatch):
        monkeypatch.setattr(settings, "drift_interval", 3600)
        check = AsyncMock(return_value=[])
        with patch.object(drift, "check_drift_once", check):
            task = asyncio.create_task(drift.drift_loop())
            await wait_until(lambda: check.await_count == 1)
            await asyncio.sleep(0.02)
            assert not task.done()  # waiting for the interval, not spinning
            assert check.await_count == 1
            drift.stop_drift()
            await asyncio.wait_for(task, timeout=5)
        assert check.await_count == 1

    async def test_loop_checks_again_every_interval(self, settings, monkeypatch):
        monkeypatch.setattr(settings, "drift_interval", 0.01)
        check = AsyncMock(return_value=[])
        with patch.object(drift, "check_drift_once", check):
            task = asyncio.create_task(drift.drift_loop())
            await wait_until(lambda: check.await_count >= 3)
            drift.stop_drift()
            await asyncio.wait_for(task, timeout=5)

    async def test_a_failing_check_does_not_end_the_loop(self, settings, monkeypatch, caplog):
        monkeypatch.setattr(settings, "drift_interval", 0.01)
        check = AsyncMock(side_effect=[RuntimeError("database is locked"), [], []])
        with patch.object(drift, "check_drift_once", check):
            task = asyncio.create_task(drift.drift_loop())
            await wait_until(lambda: check.await_count >= 2)
            drift.stop_drift()
            await asyncio.wait_for(task, timeout=5)
        assert any("Drift check error: database is locked" in r.getMessage() for r in caplog.records)

    async def test_a_restarted_loop_is_not_stopped_by_the_previous_stop(self, settings, monkeypatch):
        monkeypatch.setattr(settings, "drift_interval", 3600)
        check = AsyncMock(return_value=[])
        with patch.object(drift, "check_drift_once", check):
            first = asyncio.create_task(drift.drift_loop())
            await wait_until(lambda: check.await_count == 1)
            drift.stop_drift()
            await asyncio.wait_for(first, timeout=5)

            second = asyncio.create_task(drift.drift_loop())
            await wait_until(lambda: check.await_count == 2)
            await asyncio.sleep(0.02)
            assert not second.done()
            drift.stop_drift()
            await asyncio.wait_for(second, timeout=5)

    async def test_real_loop_records_drift_in_the_database(self, db, settings, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "drift_interval", 3600)
        conf = tmp_path / "a.conf"
        conf.write_text("edited")
        await track(db, conf, sha("generated"))
        task = asyncio.create_task(drift.drift_loop())
        try:
            for _ in range(500):
                if (await disk_hashes(db))["a.conf"] is not None:
                    break
                await asyncio.sleep(0.01)
        finally:
            drift.stop_drift()
            await asyncio.wait_for(task, timeout=5)
        assert await disk_hashes(db) == {"a.conf": sha("edited")}


# ── Backup: one pass ──────────────────────────────────────────────────────────


def pools_in(path) -> list:
    conn = sqlite3.connect(str(path))
    try:
        return [r[0] for r in conn.execute("SELECT name FROM pools ORDER BY name")]
    finally:
        conn.close()


async def add_pool(db, name: str) -> None:
    await db.execute("INSERT INTO pools (name, type) VALUES (?, 'nanio')", (name,))
    await db.commit()


class TestBackupDatabase:
    async def test_backup_is_a_readable_copy_of_the_database(self, db, settings):
        await add_pool(db, "alpha")
        path = await backup.backup_database()
        assert path == settings.db_path + ".bak"
        assert pools_in(path) == ["alpha"]

    async def test_backup_goes_to_the_configured_path_creating_its_directory(self, db, settings, tmp_path, monkeypatch):
        target = tmp_path / "backups" / "nested" / "orch.bak"
        monkeypatch.setattr(settings, "db_backup_path", str(target))
        await add_pool(db, "alpha")
        assert await backup.backup_database() == str(target)
        assert pools_in(target) == ["alpha"]

    async def test_no_database_means_no_backup(self, settings, tmp_path, monkeypatch):
        monkeypatch.setattr(backup, "get_db_path", lambda: str(tmp_path / "absent.db"))
        assert await backup.backup_database() is None
        assert not Path(settings.effective_db_backup_path).exists()

    async def test_successive_backups_rotate_and_keep_the_configured_number(self, db, settings, monkeypatch):
        monkeypatch.setattr(settings, "db_backup_rotate", 3)
        bak = settings.db_path + ".bak"
        for name in ("p1", "p2", "p3", "p4"):
            await add_pool(db, name)
            assert await backup.backup_database() == bak
        assert pools_in(bak) == ["p1", "p2", "p3", "p4"]
        assert pools_in(bak + ".2") == ["p1", "p2", "p3"]
        assert pools_in(bak + ".3") == ["p1", "p2"]
        assert not Path(bak + ".4").exists()

    async def test_failed_backup_returns_none_and_is_logged(self, db, settings, caplog):
        with patch.object(backup.aiosqlite, "connect", side_effect=sqlite3.OperationalError("disk I/O error")):
            assert await backup.backup_database() is None
        assert any("Database backup failed: disk I/O error" in r.getMessage() for r in caplog.records)

    async def test_trigger_backup_makes_a_backup(self, db, settings):
        await add_pool(db, "alpha")
        assert await backup.trigger_backup() is None
        assert pools_in(settings.db_path + ".bak") == ["alpha"]


class TestRotateBackups:
    @staticmethod
    def seed(tmp_path, *suffixes) -> str:
        base = str(tmp_path / "db.bak")
        for suffix in suffixes:
            Path(base + suffix).write_text(f"gen{suffix or '.1'}")
        return base

    @staticmethod
    def state(tmp_path) -> dict:
        return {p.name: p.read_text() for p in sorted(tmp_path.iterdir())}

    def test_nothing_to_rotate(self, tmp_path):
        backup._rotate_backups(str(tmp_path / "db.bak"), 3)
        assert self.state(tmp_path) == {}

    def test_full_set_shifts_by_one_and_drops_the_oldest(self, tmp_path):
        base = self.seed(tmp_path, "", ".2", ".3")
        backup._rotate_backups(base, 3)
        assert self.state(tmp_path) == {"db.bak.2": "gen.1", "db.bak.3": "gen.2"}

    def test_gaps_are_left_alone(self, tmp_path):
        base = self.seed(tmp_path, ".2")
        backup._rotate_backups(base, 4)
        assert self.state(tmp_path) == {"db.bak.3": "gen.2"}

    def test_keeping_a_single_copy_moves_nothing_aside(self, tmp_path):
        base = self.seed(tmp_path, "")
        backup._rotate_backups(base, 1)
        assert self.state(tmp_path) == {"db.bak": "gen.1"}

    def test_keeping_two_copies(self, tmp_path):
        base = self.seed(tmp_path, "", ".2")
        backup._rotate_backups(base, 2)
        assert self.state(tmp_path) == {"db.bak.2": "gen.1"}


# ── Backup: loop ──────────────────────────────────────────────────────────────


class TestBackupLoop:
    async def test_stop_before_the_loop_ever_ran_is_harmless(self, monkeypatch):
        monkeypatch.setattr(backup, "_stop_event", None)
        backup.stop_backup()

    async def test_loop_waits_a_full_interval_before_its_first_backup_and_stops_when_told(self, settings, monkeypatch):
        monkeypatch.setattr(settings, "db_backup_interval", 3600)
        run = AsyncMock()
        with patch.object(backup, "backup_database", run):
            task = asyncio.create_task(backup.backup_loop())
            await asyncio.sleep(0.02)
            assert not task.done()
            backup.stop_backup()
            await asyncio.wait_for(task, timeout=5)
        run.assert_not_awaited()

    async def test_loop_backs_up_every_interval(self, settings, monkeypatch):
        monkeypatch.setattr(settings, "db_backup_interval", 0.01)
        run = AsyncMock()
        with patch.object(backup, "backup_database", run):
            task = asyncio.create_task(backup.backup_loop())
            await wait_until(lambda: run.await_count >= 3)
            backup.stop_backup()
            await asyncio.wait_for(task, timeout=5)

    async def test_stop_requested_during_a_backup_ends_the_loop_after_it(self, settings, monkeypatch):
        monkeypatch.setattr(settings, "db_backup_interval", 0.01)
        calls = []

        async def run():
            calls.append(1)
            backup.stop_backup()

        with patch.object(backup, "backup_database", run):
            await asyncio.wait_for(backup.backup_loop(), timeout=5)
        assert calls == [1]

    async def test_real_loop_writes_a_backup_file(self, db, settings, monkeypatch):
        monkeypatch.setattr(settings, "db_backup_interval", 0.01)
        await add_pool(db, "alpha")
        bak = Path(settings.db_path + ".bak")
        task = asyncio.create_task(backup.backup_loop())
        try:
            await wait_until(bak.exists)
        finally:
            backup.stop_backup()
            await asyncio.wait_for(task, timeout=5)
        assert pools_in(bak) == ["alpha"]
