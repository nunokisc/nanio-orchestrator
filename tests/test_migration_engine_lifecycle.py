"""Cancelling a migration, and what the engine does at startup with the migrations and the
rclone config directories that a previous process left behind."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from unittest.mock import patch

import pytest

from tests.test_migration_engine_rclone import (
    ScriptedRclone,
    log_messages,
    route_pool_id,
    setup_bucket,
    wait_terminal,
)

pytestmark = pytest.mark.usefixtures("set_secret", "ensure_migrations_dir")

FAKE_PID = 4242  # the pid FakeRclone reports


@pytest.fixture
def scripted():
    fake = ScriptedRclone()
    with patch("asyncio.create_subprocess_exec", new=fake):
        yield fake


@pytest.fixture(autouse=True)
def private_tempdir(tmp_path, monkeypatch):
    """Startup cleanup removes every nanio-rclone-* directory of the temp dir: keep it away
    from the real one, where other processes may have live migrations."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return tmp_path


class Signals:
    """Stands in for os.kill: records every signal and decides whether the process is there."""

    def __init__(self, gone_on=()):
        self.sent: list[tuple[int, int]] = []
        self._gone_on = set(gone_on)  # signals answered with "no such process"

    def __call__(self, pid, sig):
        self.sent.append((pid, sig))
        if sig in self._gone_on:
            raise ProcessLookupError(pid)


@pytest.fixture
def no_waiting():
    """Make the engine's pauses between liveness checks instantaneous."""
    real_sleep = asyncio.sleep

    async def instant(_delay):
        await real_sleep(0)

    return patch("nanio_orchestrator.migration_engine.asyncio.sleep", new=instant)


async def _insert_migration(vhost, bucket, src, dst, phase, rclone_pid=None, split_routing=0, route=None) -> int:
    from nanio_orchestrator.db import get_db_ctx

    async with get_db_ctx() as db:
        cursor = await db.execute(
            """INSERT INTO migrations
               (vhost_id, bucket, src_pool_id, dst_pool_id, phase, rclone_pid, split_routing, route_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                vhost["id"],
                bucket,
                src["id"],
                dst["id"],
                phase,
                rclone_pid,
                split_routing,
                route["id"] if route else None,
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def _migration(mig_id: int) -> dict:
    from nanio_orchestrator.db import get_db_ctx

    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT * FROM migrations WHERE id = ?", (mig_id,))
    return dict(rows[0])


# ── Cancelling ────────────────────────────────────────────────────────────────


class TestCancel:
    async def test_cancel_terminates_rclone_and_stops_the_migration(
        self, client, mock_nginx, mock_s3, scripted, no_waiting
    ):
        from nanio_orchestrator.migration_engine import get_active_count
        from nanio_orchestrator.sidecar import read_migration_state

        src, dst, _, route = await setup_bucket(client, mock_s3)
        never = asyncio.Event()
        scripted.script = lambda kind, nth, cmd: {"hang": never}
        resp = await client.post(
            "/api/migrations", json={"bucket": "bk", "src_pool_id": src["id"], "dst_pool_id": dst["id"]}
        )
        assert resp.status_code == 201, resp.text
        mig_id = resp.json()["id"]
        deadline = asyncio.get_running_loop().time() + 15
        while (await _migration(mig_id))["rclone_pid"] != FAKE_PID:
            assert asyncio.get_running_loop().time() < deadline, "rclone was never started"
            await asyncio.sleep(0.01)
        assert get_active_count() == 1
        signals = Signals(gone_on={0})  # rclone exits on SIGTERM

        with patch("os.kill", new=signals), no_waiting:
            resp = await client.post(f"/api/migrations/{mig_id}/cancel")

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "migration_id": mig_id}
        assert signals.sent == [(FAKE_PID, 15), (FAKE_PID, 0)]
        mig = await wait_terminal(client, mig_id)
        assert mig["phase"] == "cancelled"
        assert get_active_count() == 0
        assert len(scripted.commands) == 1  # nothing ran after the cancelled copy
        assert "Migration cancelled by operator" in await log_messages(client, mig_id)
        assert await route_pool_id(route["id"]) == src["id"]
        assert read_migration_state(mig_id) is None  # nothing left to recover
        audit = (await client.get(f"/api/audit?entity_type=migration&entity_id={mig_id}")).json()
        cancelled = [a for a in audit if a["action"] == "cancel_migration"]
        assert [a["before_json"] for a in cancelled] == ['{"phase": "copying"}']

    async def test_rclone_that_ignores_sigterm_is_killed(self, client, mock_nginx, mock_s3, no_waiting, caplog):
        from nanio_orchestrator.migration_engine import cancel_migration

        src, dst, vhost, _ = await setup_bucket(client, mock_s3)
        mig_id = await _insert_migration(vhost, "bk", src, dst, "copying", rclone_pid=FAKE_PID)
        signals = Signals()

        with patch("os.kill", new=signals), no_waiting, caplog.at_level(logging.WARNING):
            assert await cancel_migration(mig_id) is True

        assert signals.sent == [(FAKE_PID, 15), *[(FAKE_PID, 0)] * 20, (FAKE_PID, 9)]
        assert f"Migration {mig_id}: rclone pid {FAKE_PID} required SIGKILL" in caplog.text
        assert (await _migration(mig_id))["phase"] == "cancelled"

    async def test_rclone_exiting_just_before_sigkill_is_not_reported_as_killed(
        self, client, mock_nginx, mock_s3, no_waiting, caplog
    ):
        from nanio_orchestrator.migration_engine import cancel_migration

        src, dst, vhost, _ = await setup_bucket(client, mock_s3)
        mig_id = await _insert_migration(vhost, "bk", src, dst, "copying", rclone_pid=FAKE_PID)
        signals = Signals(gone_on={9})

        with patch("os.kill", new=signals), no_waiting, caplog.at_level(logging.WARNING):
            await cancel_migration(mig_id)

        assert signals.sent[-1] == (FAKE_PID, 9)
        assert "required SIGKILL" not in caplog.text
        assert (await _migration(mig_id))["phase"] == "cancelled"

    async def test_rclone_already_gone_is_not_signalled_again(self, client, mock_nginx, mock_s3, no_waiting):
        """The pid recorded by a previous process: nothing answers to it any more."""
        from nanio_orchestrator.migration_engine import cancel_migration

        src, dst, vhost, _ = await setup_bucket(client, mock_s3)
        mig_id = await _insert_migration(vhost, "bk", src, dst, "copying", rclone_pid=FAKE_PID)
        signals = Signals(gone_on={15})

        with patch("os.kill", new=signals), no_waiting:
            await cancel_migration(mig_id)

        assert signals.sent == [(FAKE_PID, 15)]
        assert (await _migration(mig_id))["phase"] == "cancelled"

    async def test_migration_that_never_started_rclone_is_cancelled_without_signals(self, client, mock_nginx, mock_s3):
        src, dst, vhost, _ = await setup_bucket(client, mock_s3)
        mig_id = await _insert_migration(vhost, "bk", src, dst, "pending")
        signals = Signals()

        with patch("os.kill", new=signals):
            resp = await client.post(f"/api/migrations/{mig_id}/cancel")

        assert resp.status_code == 200, resp.text
        assert signals.sent == []
        assert (await _migration(mig_id))["phase"] == "cancelled"

    async def test_cancel_during_the_split_keeps_the_split_and_its_state_file(
        self, client, mock_nginx, mock_s3, no_waiting
    ):
        from nanio_orchestrator.sidecar import read_migration_state

        src, dst, vhost, route = await setup_bucket(client, mock_s3)
        mig_id = await _insert_migration(vhost, "bk", src, dst, "verifying", split_routing=1, route=route)

        with patch("os.kill", new=Signals()):
            resp = await client.post(f"/api/migrations/{mig_id}/cancel")

        assert resp.status_code == 200, resp.text
        mig = (await client.get(f"/api/migrations/{mig_id}")).json()
        assert (mig["phase"], mig["split_routing"]) == ("cancelled", True)
        state = read_migration_state(mig_id)
        assert state is not None and state["status"] == "cancelled" and state["nginx_state"] == "split"
        assert any("Split routing stays active" in m for m in await log_messages(client, mig_id))

    async def test_finished_migration_cannot_be_cancelled(self, client, mock_nginx, mock_s3):
        src, dst, vhost, _ = await setup_bucket(client, mock_s3)
        mig_id = await _insert_migration(vhost, "bk", src, dst, "done")
        signals = Signals()

        with patch("os.kill", new=signals):
            resp = await client.post(f"/api/migrations/{mig_id}/cancel")

        assert resp.status_code == 400
        assert resp.json()["detail"] == "Migration is already in terminal state: done"
        assert (await _migration(mig_id))["phase"] == "done"
        assert signals.sent == []


class TestStateFile:
    async def test_no_state_file_for_a_migration_deleted_under_the_engine(self, client, mock_nginx):
        """Deleting a vhost cascades to its migrations; a phase change arriving afterwards
        must not leave a state file that a rebuild would resurrect."""
        from nanio_orchestrator.migration_engine import _set_phase
        from nanio_orchestrator.sidecar import read_migration_state, scan_migration_states

        await _set_phase(4242, "copying")

        assert read_migration_state(4242) is None
        assert scan_migration_states() == []


# ── Startup ───────────────────────────────────────────────────────────────────


class TestStaleRcloneDirs:
    def test_leftover_config_dirs_are_removed(self, private_tempdir):
        from nanio_orchestrator.migration_engine import cleanup_stale_rclone_dirs

        leftover = private_tempdir / "nanio-rclone-abc123"
        leftover.mkdir()
        (leftover / "rclone.conf").write_text("secret_access_key = hunter2\n")
        unrelated = private_tempdir / "something-else"
        unrelated.mkdir()

        cleanup_stale_rclone_dirs()

        assert not leftover.exists()
        assert unrelated.exists()

    def test_dir_that_cannot_be_removed_is_reported_and_the_rest_still_cleaned(self, private_tempdir, caplog):
        from nanio_orchestrator.migration_engine import cleanup_stale_rclone_dirs

        stuck = private_tempdir / "nanio-rclone-stuck"
        stuck.mkdir()
        removable = private_tempdir / "nanio-rclone-zzz"
        removable.mkdir()
        real_rmtree = shutil.rmtree

        def rmtree(path, *args, **kwargs):
            if os.path.basename(path) == "nanio-rclone-stuck":
                raise PermissionError("Operation not permitted")
            real_rmtree(path, *args, **kwargs)

        with patch("shutil.rmtree", new=rmtree), caplog.at_level(logging.WARNING):
            cleanup_stale_rclone_dirs()

        assert stuck.exists() and not removable.exists()
        assert f"Failed to clean up {stuck}: Operation not permitted" in caplog.text


class TestRecovery:
    async def test_nothing_to_recover(self, client, mock_nginx, private_tempdir):
        from nanio_orchestrator.migration_engine import recover_interrupted_migrations

        leftover = private_tempdir / "nanio-rclone-old"
        leftover.mkdir()

        assert await recover_interrupted_migrations() == 0
        assert not leftover.exists()  # plaintext credentials of the previous process

    async def test_migration_interrupted_while_copying_is_run_again(self, client, mock_nginx, mock_s3, scripted):
        from nanio_orchestrator.migration_engine import recover_interrupted_migrations

        src, dst, vhost, route = await setup_bucket(client, mock_s3)
        mig_id = await _insert_migration(vhost, "bk", src, dst, "copying", rclone_pid=999999, route=route)

        assert await recover_interrupted_migrations() == 1

        mig = await wait_terminal(client, mig_id)
        assert mig["phase"] == "done", mig
        assert await route_pool_id(route["id"]) == dst["id"]
        log = await log_messages(client, mig_id)
        assert log[0] == "Restarting interrupted migration after crash/restart"
        assert "--update" not in scripted.copies()[0]

    async def test_migration_interrupted_while_verifying_resumes_under_the_split(
        self, client, mock_nginx, mock_s3, scripted
    ):
        """The destination already holds client writes: no pass of the second run may
        overwrite them."""
        from nanio_orchestrator.migration_engine import recover_interrupted_migrations

        src, dst, vhost, route = await setup_bucket(client, mock_s3)
        mig_id = await _insert_migration(vhost, "bk", src, dst, "verifying", split_routing=1, route=route)

        assert await recover_interrupted_migrations() == 1

        mig = await wait_terminal(client, mig_id)
        assert mig["phase"] == "done", mig
        assert mig["split_routing"] is False
        assert scripted.copies() and all("--update" in c for c in scripted.copies())

    @pytest.mark.parametrize("phase", ["write_routing", "switching"])
    async def test_migration_interrupted_while_changing_nginx_needs_the_operator(
        self, client, mock_nginx, mock_s3, scripted, phase
    ):
        from nanio_orchestrator.migration_engine import get_active_count, recover_interrupted_migrations

        src, dst, vhost, route = await setup_bucket(client, mock_s3)
        mig_id = await _insert_migration(vhost, "bk", src, dst, phase, route=route)

        assert await recover_interrupted_migrations() == 1

        mig = (await client.get(f"/api/migrations/{mig_id}")).json()
        assert mig["phase"] == "error"
        assert mig["error_msg"] == (
            f"Interrupted during {phase} phase (process restart). "
            "Operator review required: check nginx config and routes."
        )
        assert any(f"stuck in '{phase}' phase" in m for m in await log_messages(client, mig_id))
        assert get_active_count() == 0 and scripted.commands == []
        assert await route_pool_id(route["id"]) == src["id"]

    async def test_migration_already_running_in_this_process_is_left_alone(self, client, mock_nginx, mock_s3, scripted):
        from nanio_orchestrator.migration_engine import _active_tasks, recover_interrupted_migrations

        src, dst, vhost, route = await setup_bucket(client, mock_s3)
        mig_id = await _insert_migration(vhost, "bk", src, dst, "copying", route=route)
        running = asyncio.create_task(asyncio.Event().wait())
        _active_tasks[mig_id] = running
        try:
            assert await recover_interrupted_migrations() == 0

            assert _active_tasks[mig_id] is running
            assert (await _migration(mig_id))["phase"] == "copying"
            assert await log_messages(client, mig_id) == []
            assert scripted.commands == []
        finally:
            _active_tasks.pop(mig_id, None)
            running.cancel()
