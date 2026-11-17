"""How the migration engine drives rclone: the commands and config it is given, what happens
to its output, and how a failed copy or check ends the migration.

Also home of the helpers shared by the other ``test_migration_engine_*`` modules.
"""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from pathlib import Path
from typing import Callable, Optional
from unittest.mock import patch

import pytest

from tests.conftest import T0, FakeRclone, create_member, create_pool, create_route, create_vhost

pytestmark = pytest.mark.usefixtures("set_secret", "ensure_migrations_dir")


# ── Shared helpers ────────────────────────────────────────────────────────────


class _Output:
    """Stands in for the stdout pipe of a subprocess: yields lines, optionally never ending."""

    def __init__(self, lines, hang: Optional[asyncio.Event] = None):
        self._lines = list(lines)
        self._hang = hang

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._lines:
            return self._lines.pop(0)
        if self._hang is not None:
            await self._hang.wait()
        raise StopAsyncIteration


class ScriptedRclone(FakeRclone):
    """A FakeRclone whose runs can print output, exit non-zero, fail to start or hang.

    ``script(kind, nth, cmd)`` is called for the nth (0-based) ``copy`` or ``check`` and may
    return a dict with any of:

    - ``lines``: bytes lines printed by the process
    - ``returncode``: exit status
    - ``raise``: exception raised instead of starting the process
    - ``missing``: keys reported as missing at the destination by this check
    - ``drop_report``: the check leaves no report files behind
    - ``hang``: an asyncio.Event; the process keeps running until it is set
    """

    def __init__(self):
        super().__init__()
        self.script: Optional[Callable[[str, int, list], Optional[dict]]] = None
        self.configs: list[str] = []  # the rclone.conf each run was started with

    async def __call__(self, *cmd, **kwargs):
        cmd = [str(c) for c in cmd]
        kind = cmd[1]
        nth = sum(1 for c in self.commands if c[1] == kind)
        outcome = (self.script(kind, nth, cmd) if self.script else None) or {}
        if "missing" in outcome:
            self.missing = outcome["missing"]
        self.configs.append(Path(cmd[cmd.index("--config") + 1]).read_text())
        proc = await super().__call__(*cmd, **kwargs)
        if "raise" in outcome:
            raise outcome["raise"]
        if outcome.get("drop_report"):
            for flag in ("--differ", "--missing-on-dst", "--error"):
                os.unlink(cmd[cmd.index(flag) + 1])
        proc.returncode = outcome.get("returncode", proc.returncode)
        proc.stdout = _Output(outcome.get("lines", []), outcome.get("hang"))
        return proc

    def checks(self):
        return [c for c in self.commands if c[1] == "check"]


@pytest.fixture
def scripted():
    fake = ScriptedRclone()
    with patch("asyncio.create_subprocess_exec", new=fake):
        yield fake


async def setup_bucket(client, mock_s3, bucket="bk", name="eng"):
    """Bucket routed to pool `<name>-src` on vhost `<name>.example.com`; `<name>-dst` is empty."""
    src = await create_pool(client, f"{name}-src")
    await create_member(client, src["id"], "10.0.0.1:9000")
    dst = await create_pool(client, f"{name}-dst")
    await create_member(client, dst["id"], "10.0.0.2:9000")
    vhost = await create_vhost(client, f"{name}.example.com", default_pool_id=src["id"])
    mock_s3["list_buckets"].return_value = [{"name": bucket, "created": "2025-01-01"}]
    resp = await client.post(f"/api/vhosts/{vhost['id']}/buckets/sync")
    assert resp.status_code == 200, resp.text
    route = await create_route(client, vhost["id"], bucket, src["id"])
    mock_s3["count_objects"].return_value = 5
    return src, dst, vhost, route


async def wait_terminal(client, mig_id: int) -> dict:
    """Wait until the migration reached a terminal phase and its engine task is gone."""
    from nanio_orchestrator.migration_engine import _active_tasks

    loop = asyncio.get_running_loop()
    deadline = loop.time() + 15  # only ever reached by a migration that hangs
    while True:
        data = (await client.get(f"/api/migrations/{mig_id}")).json()
        if data["phase"] in ("done", "error", "cancelled") and mig_id not in _active_tasks:
            return data
        assert loop.time() < deadline, f"migration {mig_id} did not finish: {data}"
        await asyncio.sleep(0.02)


async def migrate(client, bucket, src, dst) -> dict:
    resp = await client.post(
        "/api/migrations", json={"bucket": bucket, "src_pool_id": src["id"], "dst_pool_id": dst["id"]}
    )
    assert resp.status_code == 201, resp.text
    return await wait_terminal(client, resp.json()["id"])


async def log_messages(client, mig_id: int) -> list[str]:
    """The migration log, oldest entry first."""
    resp = await client.get(f"/api/migrations/{mig_id}/log?limit=1000")
    assert resp.status_code == 200, resp.text
    return [e["message"] for e in reversed(resp.json())]


async def route_pool_id(route_id: int) -> Optional[int]:
    from nanio_orchestrator.db import get_db_ctx

    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT pool_id FROM routes WHERE id = ?", (route_id,))
    return rows[0]["pool_id"] if rows else None


def use_settings(monkeypatch, **values) -> None:
    """Override NANIO_ORCHESTRATOR_* settings for the rest of the test."""
    import nanio_orchestrator.config as cfg_mod

    for name, value in values.items():
        monkeypatch.setenv(f"NANIO_ORCHESTRATOR_{name.upper()}", str(value))
    cfg_mod.settings = None


def vhost_conf(server_name: str) -> str:
    from nanio_orchestrator.config import get_settings

    return (get_settings().vhosts_dir / f"{server_name}.conf").read_text()


# ── The rclone config ─────────────────────────────────────────────────────────


class TestRcloneConfig:
    async def test_remotes_use_pool_credentials_and_the_first_member(self, client, mock_nginx, mock_s3, scripted):
        src, dst, _, _ = await setup_bucket(client, mock_s3)
        resp = await client.put(
            f"/api/pools/{src['id']}/credentials",
            json={"access_key": "AKSOURCE", "secret_key": "sk-source", "region": "eu-west-7"},
        )
        assert resp.status_code == 200, resp.text
        seen_dirs = []
        scripted.script = lambda kind, nth, cmd: seen_dirs.append(os.path.dirname(cmd[cmd.index("--config") + 1]))

        mig = await migrate(client, "bk", src, dst)

        assert mig["phase"] == "done", mig
        src_remote, dst_remote = scripted.configs[0].split("[dst]")
        assert src_remote.startswith("[src]\n")
        for line in (
            "access_key_id = AKSOURCE",
            "secret_access_key = sk-source",
            "region = eu-west-7",
            "endpoint = http://10.0.0.1:9000",
        ):
            assert line in src_remote.splitlines()
        # the destination pool has no stored credentials: anonymous remote, own member address
        assert "endpoint = http://10.0.0.2:9000" in dst_remote.splitlines()
        assert "access_key_id" not in dst_remote
        # the config holds plaintext secrets and must not outlive the migration
        assert seen_dirs and not any(os.path.exists(d) for d in seen_dirs)

    async def test_pool_without_enabled_member_gets_no_endpoint(self, client, mock_nginx):
        from nanio_orchestrator.migration_engine import _build_rclone_config

        src = await create_pool(client, "cfg-src")
        dst = await create_pool(client, "cfg-dst")
        await create_member(client, dst["id"], "10.0.0.2:9000")

        src_remote, dst_remote = (await _build_rclone_config(src["id"], dst["id"])).split("[dst]")

        assert "endpoint" not in src_remote
        assert "endpoint = http://10.0.0.2:9000" in dst_remote.splitlines()


# ── The commands ──────────────────────────────────────────────────────────────


class TestRcloneCommands:
    async def test_bandwidth_limit_applies_to_copies_only(self, client, mock_nginx, mock_s3, scripted, monkeypatch):
        use_settings(monkeypatch, migration_bandwidth_limit="10M")
        src, dst, _, _ = await setup_bucket(client, mock_s3)

        mig = await migrate(client, "bk", src, dst)

        assert mig["phase"] == "done", mig
        assert scripted.copies() and scripted.checks()
        for copy in scripted.copies():
            assert copy[copy.index("--bwlimit") + 1] == "10M"
        for check in scripted.checks():
            assert "--bwlimit" not in check

    async def test_no_bandwidth_flag_without_a_limit(self, client, mock_nginx, mock_s3, scripted):
        src, dst, _, _ = await setup_bucket(client, mock_s3)

        await migrate(client, "bk", src, dst)

        assert scripted.copies() and not any("--bwlimit" in c for c in scripted.copies())


# ── Output and progress ───────────────────────────────────────────────────────


class TestRcloneOutput:
    async def test_output_is_logged_and_progress_recorded(self, client, mock_nginx, mock_s3, scripted):
        src, dst, _, _ = await setup_bucket(client, mock_s3)
        lines = [
            b"2026/04/16 18:36:06 INFO  : Transferred:   1.5 KiB / 3 KiB, 50%, 1 KiB/s, ETA 1s\n",
            b"\n",
            b"2026/04/16 18:36:06 INFO  : Checks:         3 / 7, 42%\n",
            b"2026/04/16 18:36:07 NOTICE: some-key: skipped\n",
        ]
        scripted.script = lambda kind, nth, cmd: {"lines": lines} if (kind, nth) == ("copy", 0) else None

        mig = await migrate(client, "bk", src, dst)

        assert mig["phase"] == "done", mig
        # the byte counters survive the later line that only carries the object counters
        assert (mig["bytes_done"], mig["bytes_total"]) == (1536, 3072)
        assert (mig["objects_done"], mig["objects_total"]) == (3, 7)
        log = await log_messages(client, mig["id"])
        for line in lines:
            if line.strip():
                assert line.decode().rstrip() in log
        assert "" not in log

    async def test_long_output_is_logged_completely_and_in_order(self, client, mock_nginx, mock_s3, scripted):
        src, dst, _, _ = await setup_bucket(client, mock_s3)
        lines = [f"object-{n:03d}: copied\n".encode() for n in range(120)]
        scripted.script = lambda kind, nth, cmd: {"lines": lines} if (kind, nth) == ("copy", 0) else None

        mig = await migrate(client, "bk", src, dst)

        log = await log_messages(client, mig["id"])
        assert [m for m in log if m.startswith("object-")] == [f"object-{n:03d}: copied" for n in range(120)]

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            (
                "INFO  : Transferred:   1.234 GiB / 5.678 GiB, 22%, 54 MiB/s, ETA 1m30s",
                {"bytes_done": int(1.234 * 1024**3), "bytes_total": int(5.678 * 1024**3)},
            ),
            ("Transferred: 500 KB / 2 MB, 25%", {"bytes_done": 500_000, "bytes_total": 2_000_000}),
            ("Transferred: 12 B / 1 TiB, 0%", {"bytes_done": 12, "bytes_total": 1024**4}),
            # an unknown unit is taken as bytes rather than dropping the line
            ("Transferred: 7 parsecs / 9 parsecs, 77%", {"bytes_done": 7, "bytes_total": 9}),
            ("INFO  : Checks:         1234 / 5678, 22%", {"objects_done": 1234, "objects_total": 5678}),
            (
                "Transferred: 1 MiB / 2 MiB, 50%, Checks: 4 / 8, 50%",
                {"bytes_done": 1024**2, "bytes_total": 2 * 1024**2, "objects_done": 4, "objects_total": 8},
            ),
            ("NOTICE: some-key: skipped", None),
            ("Transferred: 3 / 9, 33%", None),  # the object-count variant carries no unit
        ],
    )
    def test_stats_line_parsing(self, line, expected):
        from nanio_orchestrator.migration_engine import _parse_rclone_stats

        assert _parse_rclone_stats(line) == expected


# ── A copy that fails ─────────────────────────────────────────────────────────


class TestCopyFailure:
    @pytest.mark.parametrize(
        ("outcome", "logged"),
        [
            ({"returncode": 3}, "rclone exit code 3"),
            ({"raise": FileNotFoundError("rclone")}, "rclone binary not found at: rclone"),
            ({"raise": PermissionError("not executable")}, "rclone error: not executable"),
        ],
    )
    async def test_failed_bulk_copy_ends_the_migration_before_any_routing_change(
        self, client, mock_nginx, mock_s3, scripted, outcome, logged
    ):
        src, dst, _, route = await setup_bucket(client, mock_s3)
        scripted.script = lambda kind, nth, cmd: outcome

        mig = await migrate(client, "bk", src, dst)

        assert mig["phase"] == "error"
        assert mig["error_msg"] == "rclone copy failed (pass 1/10)"
        assert mig["split_routing"] is False
        assert logged in await log_messages(client, mig["id"])
        assert len(scripted.commands) == 1
        assert await route_pool_id(route["id"]) == src["id"]
        assert "LIVE MIGRATION" not in vhost_conf("eng.example.com")

    async def test_failed_copy_during_verification_keeps_the_split(self, client, mock_nginx, mock_s3, scripted):
        src, dst, _, route = await setup_bucket(client, mock_s3)
        # copy 0 is the bulk pass, copy 1 the --update pass of the first verification
        scripted.script = lambda kind, nth, cmd: {"returncode": 1} if (kind, nth) == ("copy", 1) else None

        mig = await migrate(client, "bk", src, dst)

        assert mig["phase"] == "error"
        assert mig["error_msg"] == "Verify pass 1: copy step failed"
        assert mig["split_routing"] is True
        assert not scripted.checks()
        assert await route_pool_id(route["id"]) == src["id"]
        assert "LIVE MIGRATION → eng-dst" in vhost_conf("eng.example.com")


# ── Verification ──────────────────────────────────────────────────────────────


class TestVerification:
    async def test_retries_while_the_number_of_differences_shrinks(self, client, mock_nginx, mock_s3, scripted):
        """An upload still in flight to the source when the split went live shows up as a
        difference; the next pass copies it."""
        src, dst, _, route = await setup_bucket(client, mock_s3)
        reports = [
            {"missing": ["a", "b"], "lines": [b"\n", b"2026/04/16 18:36:06 ERROR : 5 differences found\n"]},
            {
                "missing": ["a"],
                "lines": [
                    b"2026/04/16 18:36:09 ERROR : a: file not in destination\n",
                    b"2026/04/16 18:36:09 ERROR : 1 differences found\n",
                ],
            },
            {"missing": []},
        ]
        scripted.script = lambda kind, nth, cmd: reports[nth] if kind == "check" else None

        mig = await migrate(client, "bk", src, dst)

        assert mig["phase"] == "done", mig
        assert len(scripted.checks()) == 3
        assert len(scripted.copies()) == 4  # bulk + one per verification pass
        log = await log_messages(client, mig["id"])
        assert "Verify pass 1: 5 difference(s) found — will retry" in log
        assert "Verify pass 2: 1 difference(s) found — will retry" in log
        assert "2026/04/16 18:36:09 ERROR : a: file not in destination" in log
        assert "Verify pass 3: check passed — dst holds all of src" in log
        assert "" not in log
        assert await route_pool_id(route["id"]) == dst["id"]

    async def test_gives_up_after_the_configured_number_of_passes(
        self, client, mock_nginx, mock_s3, scripted, monkeypatch
    ):
        use_settings(monkeypatch, migration_max_copy_passes=2)
        src, dst, _, route = await setup_bucket(client, mock_s3)
        reports = [
            {"missing": ["a", "b"], "lines": [b"ERROR : 5 differences found\n"]},
            {"missing": ["a"], "lines": [b"ERROR : 1 differences found\n"]},
        ]
        scripted.script = lambda kind, nth, cmd: reports[nth] if kind == "check" else None

        mig = await migrate(client, "bk", src, dst)

        assert mig["phase"] == "error"
        assert mig["error_msg"].startswith("Verification did not converge after 2 pass(es).")
        assert len(scripted.checks()) == 2
        # clients have been writing to the destination: the split must stay
        assert mig["split_routing"] is True
        assert await route_pool_id(route["id"]) == src["id"]

    @pytest.mark.parametrize(
        ("failure", "logged"),
        [
            (FileNotFoundError("rclone"), "rclone binary not found at: rclone"),
            (PermissionError("not executable"), "rclone check error: not executable"),
        ],
    )
    async def test_check_that_cannot_run_fails_the_migration(
        self, client, mock_nginx, mock_s3, scripted, failure, logged
    ):
        src, dst, _, route = await setup_bucket(client, mock_s3)
        scripted.script = lambda kind, nth, cmd: {"raise": failure} if kind == "check" else None

        mig = await migrate(client, "bk", src, dst)

        assert mig["phase"] == "error"
        assert "diff count did not decrease (0 → 0)" in mig["error_msg"]
        assert logged in await log_messages(client, mig["id"])
        assert mig["split_routing"] is True
        assert await route_pool_id(route["id"]) == src["id"]

    async def test_differences_without_a_readable_report_are_never_accepted(
        self, client, mock_nginx, mock_s3, scripted, mtimes
    ):
        """Without the key lists there is no telling a client overwrite from a bad copy."""
        src, dst, _, _ = await setup_bucket(client, mock_s3)
        scripted.differ = ["overwritten-by-client"]
        mtimes["10.0.0.2:9000"] = T0 + timedelta(minutes=5)  # would be accepted if it were read
        scripted.script = lambda kind, nth, cmd: {"drop_report": True} if kind == "check" else None

        mig = await migrate(client, "bk", src, dst)

        assert mig["phase"] == "error"
        assert "diff count did not decrease" in mig["error_msg"]

    async def test_object_that_cannot_be_compared_counts_as_stale(self, client, mock_nginx, mock_s3, scripted):
        src, dst, _, _ = await setup_bucket(client, mock_s3)
        scripted.differ = ["unreadable"]

        async def unreachable(address, bucket, key, **_):
            raise RuntimeError("connection refused")

        with patch("nanio_orchestrator.migration_engine.object_last_modified", new=unreachable):
            mig = await migrate(client, "bk", src, dst)

        assert mig["phase"] == "error"
        assert "diff count did not decrease (1 → 1)" in mig["error_msg"]
        log = await log_messages(client, mig["id"])
        assert "Could not compare 'unreadable' on both pools: connection refused" in log
        assert "1 object(s) at the destination are stale: unreadable" in log

    async def test_differences_cannot_be_judged_once_a_pool_has_no_member(
        self, client, mock_nginx, mock_s3, scripted, mtimes, tmp_path
    ):
        """The newer-at-destination rule needs both pools; with one unreachable the differing
        objects stay unresolved instead of being waved through."""
        from nanio_orchestrator.db import get_db_ctx
        from nanio_orchestrator.migration_engine import _verify_destination

        src, dst, vhost, _ = await setup_bucket(client, mock_s3)
        async with get_db_ctx() as db:
            cursor = await db.execute(
                """INSERT INTO migrations (vhost_id, bucket, src_pool_id, dst_pool_id, phase, split_routing)
                   VALUES (?, 'bk', ?, ?, 'verifying', 1)""",
                (vhost["id"], src["id"], dst["id"]),
            )
            await db.execute("UPDATE pool_members SET enabled = 0 WHERE pool_id = ?", (dst["id"],))
            await db.commit()
        config = tmp_path / "rclone.conf"
        config.write_text("")
        scripted.differ = ["overwritten-by-client"]
        mtimes["10.0.0.2:9000"] = T0 + timedelta(minutes=5)
        scripted.script = lambda kind, nth, cmd: {"lines": [b"ERROR : 1 differences found\n"]}

        result = await _verify_destination(
            cursor.lastrowid, str(config), "src:bk", "dst:bk", "bk", src["id"], dst["id"]
        )

        assert result == (False, 1)
