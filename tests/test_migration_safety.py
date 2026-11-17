"""Data-safety guarantees of live migration.

While split routing is active the destination pool holds client writes the source never saw.
These tests pin down what protects them:

- copy passes never overwrite a destination object that is newer than its source
- verification accepts such an object and rejects a stale one
- the split outlives a failed or cancelled migration, config regeneration and a DB rebuild
- only a migration between the same two pools may take the split over and finish the move
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from tests.conftest import create_member, create_pool, create_route, create_vhost

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def set_secret():
    from cryptography.fernet import Fernet

    os.environ["NANIO_ORCHESTRATOR_SECRET"] = Fernet.generate_key().decode()
    import nanio_orchestrator.config as cfg_mod
    import nanio_orchestrator.credentials as cred_mod

    cfg_mod.settings = None
    cred_mod.reset_fernet()
    yield
    os.environ.pop("NANIO_ORCHESTRATOR_SECRET", None)
    cfg_mod.settings = None
    cred_mod.reset_fernet()


@pytest_asyncio.fixture(autouse=True)
async def ensure_migrations_dir(tmp_dirs):
    os.makedirs(str(Path(tmp_dirs["db_path"]).parent / "migrations"), exist_ok=True)


class FakeRclone:
    """Stands in for the rclone binary: records every command and lets a test decide what
    `rclone check` reports (written to the --differ / --missing-on-dst / --error files)."""

    def __init__(self):
        self.commands: list[list[str]] = []
        self.differ: list[str] = []
        self.missing: list[str] = []

    async def __call__(self, *cmd, **_kwargs):
        cmd = [str(c) for c in cmd]
        self.commands.append(cmd)
        proc = AsyncMock()
        proc.pid = 4242
        proc.returncode = 0
        if cmd[1] == "check":
            for flag, keys in (("--differ", self.differ), ("--missing-on-dst", self.missing), ("--error", [])):
                Path(cmd[cmd.index(flag) + 1]).write_text("".join(f"{k}\n" for k in keys))
            proc.returncode = 1 if (self.differ or self.missing) else 0
        return proc

    def copies(self):
        return [c for c in self.commands if c[1] == "copy"]


@pytest.fixture
def rclone():
    fake = FakeRclone()
    with patch("asyncio.create_subprocess_exec", new=fake):
        yield fake


@pytest.fixture
def mtimes():
    """Control the Last-Modified the engine sees per pool member address."""
    times = {"10.0.0.1:9000": T0, "10.0.0.2:9000": T0, "10.0.0.3:9000": T0}

    async def fake(address, bucket, key, **_):
        return times[address]

    with patch("nanio_orchestrator.migration_engine.object_last_modified", new=fake):
        yield times


async def _setup(client, mock_s3, bucket="safe-bk"):
    src = await create_pool(client, "safe-src")
    await create_member(client, src["id"], "10.0.0.1:9000")
    dst = await create_pool(client, "safe-dst")
    await create_member(client, dst["id"], "10.0.0.2:9000")
    vh = await create_vhost(client, "safe.example.com", default_pool_id=src["id"])
    mock_s3["list_buckets"].return_value = [{"name": bucket, "created": "2025-01-01"}]
    await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")
    await create_route(client, vh["id"], bucket, src["id"])
    mock_s3["count_objects"].return_value = 5
    return src, dst, vh


async def _migrate(client, bucket, src, dst, expect=201):
    resp = await client.post(
        "/api/migrations", json={"bucket": bucket, "src_pool_id": src["id"], "dst_pool_id": dst["id"]}
    )
    assert resp.status_code == expect, resp.text
    if expect != 201:
        return resp.json()
    mig_id = resp.json()["id"]
    for _ in range(100):
        await asyncio.sleep(0.05)
        data = (await client.get(f"/api/migrations/{mig_id}")).json()
        if data["phase"] in ("done", "error", "cancelled"):
            return data
    raise AssertionError("migration did not finish")


def _vhost_conf() -> str:
    from nanio_orchestrator.config import get_settings

    return (get_settings().vhosts_dir / "safe.example.com.conf").read_text()


class TestClientWritesSurvive:
    async def test_client_overwrite_on_destination_is_accepted(self, client, mock_nginx, mock_s3, rclone, mtimes):
        """An object that differs because the destination copy is newer is a client write made
        during the split — verification must pass and the destination version must stay."""
        src, dst, _ = await _setup(client, mock_s3)
        rclone.differ = ["overwritten-by-client"]
        mtimes["10.0.0.2:9000"] = T0 + timedelta(minutes=5)

        mig = await _migrate(client, "safe-bk", src, dst)

        assert mig["phase"] == "done", mig
        assert mig["split_routing"] is False
        assert "LIVE MIGRATION" not in _vhost_conf()

    async def test_copies_use_update_once_the_split_is_active(self, client, mock_nginx, mock_s3, rclone, mtimes):
        src, dst, _ = await _setup(client, mock_s3)
        await _migrate(client, "safe-bk", src, dst)

        bulk, final = rclone.copies()
        assert "--update" not in bulk  # destination holds nothing but earlier copies
        assert "--update" in final  # destination may now hold client writes

    @pytest.mark.parametrize("dst_offset", [timedelta(0), timedelta(minutes=-5)])
    async def test_stale_destination_object_fails_verification(
        self, client, mock_nginx, mock_s3, rclone, mtimes, dst_offset
    ):
        """A differing object that is not newer at the destination is a bad copy, not a client write."""
        src, dst, _ = await _setup(client, mock_s3)
        rclone.differ = ["stale"]
        mtimes["10.0.0.2:9000"] = T0 + dst_offset

        mig = await _migrate(client, "safe-bk", src, dst)

        assert mig["phase"] == "error"
        assert "diff count did not decrease" in mig["error_msg"]

    async def test_object_missing_at_destination_fails_verification(
        self, client, mock_nginx, mock_s3, rclone, mtimes
    ):
        src, dst, _ = await _setup(client, mock_s3)
        rclone.missing = ["never-arrived"]
        mtimes["10.0.0.2:9000"] = T0 + timedelta(minutes=5)

        mig = await _migrate(client, "safe-bk", src, dst)

        assert mig["phase"] == "error"
        assert "diff count did not decrease" in mig["error_msg"]


class TestSplitOutlivesFailure:
    async def _fail_after_split(self, client, mock_s3, rclone):
        src, dst, vh = await _setup(client, mock_s3)
        rclone.missing = ["never-arrived"]
        mig = await _migrate(client, "safe-bk", src, dst)
        assert mig["phase"] == "error"
        rclone.missing = []
        rclone.commands.clear()
        return src, dst, vh, mig

    async def test_split_stays_active_after_error(self, client, mock_nginx, mock_s3, rclone, mtimes):
        from nanio_orchestrator.nginx.generator import generate_vhost_config

        _, _, vh, mig = await self._fail_after_split(client, mock_s3, rclone)

        assert mig["split_routing"] is True
        assert "LIVE MIGRATION → safe-dst" in _vhost_conf()

        # Regenerating the vhost (any edit, config rebuild) must not drop the split:
        # the destination holds client writes a source-only route would hide.
        _, content = await generate_vhost_config(vh["id"])
        assert "LIVE MIGRATION → safe-dst" in content

        log = [e["message"] for e in (await client.get(f"/api/migrations/{mig['id']}/log")).json()]
        assert any("Split routing stays active" in m for m in log)

    async def test_linked_http_vhost_keeps_the_split_when_regenerated(
        self, client, mock_nginx, mock_s3, rclone, mtimes
    ):
        """An http vhost serving the source pool's files follows the split on every
        regeneration, not only when the engine applies it — otherwise an edit of that vhost
        hid every object written to the destination until the migration finished."""
        from nanio_orchestrator.config import get_settings
        from nanio_orchestrator.nginx.generator import generate_vhost_config

        src, dst, _ = await _setup(client, mock_s3)
        web_src = (
            await client.post("/api/pools", json={"name": "web-src", "type": "http", "source_nanio_pool_id": src["id"]})
        ).json()
        await client.post(f"/api/pools/{web_src['id']}/members", json={"address": "10.0.1.1:80", "role": "primary"})
        web_dst = (
            await client.post("/api/pools", json={"name": "web-dst", "type": "http", "source_nanio_pool_id": dst["id"]})
        ).json()
        await client.post(f"/api/pools/{web_dst['id']}/members", json={"address": "10.0.1.2:80", "role": "primary"})
        web = await create_vhost(client, "cdn.example.com", default_pool_id=web_src["id"])
        resp = await client.post(f"/api/vhosts/{web['id']}/http-bucket-routes/safe-bk")
        assert resp.status_code == 201, resp.text

        rclone.missing = ["never-arrived"]
        mig = await _migrate(client, "safe-bk", src, dst)
        assert mig["phase"] == "error" and mig["split_routing"] is True

        on_disk = (get_settings().vhosts_dir / "cdn.example.com.conf").read_text()
        assert "migration_src:web-src migration_dst:web-dst" in on_disk

        # Any later regeneration (vhost edit, Rebuild All) derives the same split
        _, regenerated = await generate_vhost_config(web["id"])
        assert "migration_src:web-src migration_dst:web-dst" in regenerated
        assert "proxy_pass         http://web-dst;" in regenerated

        # Finishing the move points the http route at the destination's http pool, split gone
        rclone.missing = []
        done = await _migrate(client, "safe-bk", src, dst)
        assert done["phase"] == "done"
        _, final = await generate_vhost_config(web["id"])
        assert "LIVE MIGRATION" not in final
        assert "proxy_pass         http://web-dst;" in final

    async def test_same_move_takes_over_the_split_and_finishes(self, client, mock_nginx, mock_s3, rclone, mtimes):
        src, dst, _, failed = await self._fail_after_split(client, mock_s3, rclone)

        retry = await _migrate(client, "safe-bk", src, dst)

        assert retry["phase"] == "done", retry
        assert retry["split_routing"] is False
        assert (await client.get(f"/api/migrations/{failed['id']}")).json()["split_routing"] is False
        assert "LIVE MIGRATION" not in _vhost_conf()
        # The destination already holds client writes: no pass may overwrite them
        assert rclone.copies() and all("--update" in c for c in rclone.copies())

    async def test_different_destination_is_refused_while_split_is_active(
        self, client, mock_nginx, mock_s3, rclone, mtimes
    ):
        src, _, _, failed = await self._fail_after_split(client, mock_s3, rclone)
        other = await create_pool(client, "safe-other")
        await create_member(client, other["id"], "10.0.0.3:9000")

        body = await _migrate(client, "safe-bk", src, other, expect=409)

        assert f"migration {failed['id']}" in body["detail"]
        assert "LIVE MIGRATION → safe-dst" in _vhost_conf()

    async def test_second_migration_refused_while_first_is_in_write_routing(self, client, mock_nginx, mock_s3):
        from nanio_orchestrator.db import get_db_ctx

        src, dst, vh = await _setup(client, mock_s3)
        async with get_db_ctx() as db:
            await db.execute(
                """INSERT INTO migrations (vhost_id, bucket, src_pool_id, dst_pool_id, phase, split_routing)
                   VALUES (?, 'safe-bk', ?, ?, 'write_routing', 1)""",
                (vh["id"], src["id"], dst["id"]),
            )
            await db.commit()

        await _migrate(client, "safe-bk", src, dst, expect=409)

    async def test_cancel_keeps_state_file_while_split_is_active(self, client, mock_nginx, mock_s3, rclone, mtimes):
        from nanio_orchestrator.migration_engine import _set_phase
        from nanio_orchestrator.sidecar import read_migration_state

        _, _, _, mig = await self._fail_after_split(client, mock_s3, rclone)

        await _set_phase(mig["id"], "cancelled", "Cancelled by operator")

        state = read_migration_state(mig["id"])
        assert state is not None and state["split_routing"] is True and state["nginx_state"] == "split"

    async def test_split_survives_rebuild_from_disk(self, client, mock_nginx, mock_s3, rclone, mtimes):
        """Losing the DB must not lose the split: it is restored from the state file, and the
        route is still attributed to the source pool although nginx proxies to the destination."""
        from nanio_orchestrator.db import get_db_ctx
        from nanio_orchestrator.nginx.generator import generate_vhost_config

        await self._fail_after_split(client, mock_s3, rclone)

        resp = await client.post("/api/config/rebuild-from-disk?force=true")
        assert resp.status_code == 200, resp.text

        async with get_db_ctx() as db:
            migs = await db.execute_fetchall("SELECT vhost_id, phase, split_routing FROM migrations")
            routes = await db.execute_fetchall(
                """SELECT p.name FROM routes r JOIN pools p ON p.id = r.pool_id
                   WHERE r.path_prefix = '/safe-bk/'"""
            )
        assert [(m["phase"], m["split_routing"]) for m in migs] == [("error", 1)]
        assert [r["name"] for r in routes] == ["safe-src"]

        _, content = await generate_vhost_config(migs[0]["vhost_id"])
        assert "LIVE MIGRATION → safe-dst" in content


class TestSplitConfigRoundTrip:
    def test_parser_attributes_split_route_to_the_source_pool(self):
        from nanio_orchestrator.nginx.generator import render_vhost
        from nanio_orchestrator.nginx.parser import parse_vhost_block

        vhost = {"id": 1, "server_name": "rt.example.com", "listen_port": 80, "ssl": 0, "extra_directives": None}
        routes = [
            {"id": 1, "path_prefix": "/", "pool_name": "pool-a", "enabled": 1, "key_prefix": None,
             "extra_directives": None, "migration_dst_pool_name": None},
            {"id": 2, "path_prefix": "/moving/", "pool_name": "pool-a", "enabled": 1, "key_prefix": None,
             "extra_directives": None, "migration_dst_pool_name": "pool-b"},
        ]  # fmt: skip

        parsed = parse_vhost_block(render_vhost(vhost, routes))

        assert sorted((r["path_prefix"], r["pool_name"]) for r in parsed["routes"]) == [
            ("/", "pool-a"),
            ("/moving/", "pool-a"),
        ]


class TestSchemaUpgrade:
    async def test_existing_database_gains_split_routing(self, tmp_dirs):
        """A DB created before the column existed: the split used to be implied by the phase."""
        import aiosqlite

        import nanio_orchestrator.db as db_mod

        old_schema = db_mod.SCHEMA_SQL.replace("    split_routing           INTEGER NOT NULL DEFAULT 0,\n", "")
        assert old_schema != db_mod.SCHEMA_SQL
        path = os.path.join(os.path.dirname(tmp_dirs["db_path"]), "old.db")
        async with aiosqlite.connect(path) as db:
            await db.executescript(old_schema)
            await db.execute("INSERT INTO pools (id, name, type) VALUES (1, 'a', 'nanio'), (2, 'b', 'nanio')")
            await db.execute("INSERT INTO vhosts (id, server_name) VALUES (1, 'x.example.com')")
            for phase in ("copying", "verifying", "write_routing", "done"):
                await db.execute(
                    "INSERT INTO migrations (vhost_id, bucket, src_pool_id, dst_pool_id, phase) VALUES (1, ?, 1, 2, ?)",
                    (phase, phase),
                )
            await db.commit()

        previous = db_mod.get_db_path()
        db_mod.set_db_path(path)
        try:
            await db_mod.init_db()
            async with db_mod.get_db_ctx() as db:
                rows = await db.execute_fetchall("SELECT phase, split_routing FROM migrations ORDER BY id")
        finally:
            db_mod.set_db_path(previous)

        assert [(r["phase"], r["split_routing"]) for r in rows] == [
            ("copying", 0),
            ("verifying", 1),
            ("write_routing", 1),
            ("done", 0),
        ]

