"""Tests for live migration convergence loop, write_routing phase,
nginx split routing template, and generator migration_map injection.

Covers the changes introduced in the live-migration feature:
- Convergence loop in copying phase (multiple rclone passes until counts match)
- write_routing phase — nginx reloaded with split config when not converged
- nginx_state sidecar = "split" for write_routing / verifying phases
- generator.generate_vhost_config injects migration_dst_pool_name for active migrations
- vhost.conf.j2 renders split routing (error_page 418 + @nanio_mig_N named location)
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
import pytest_asyncio

from tests.conftest import create_member, create_pool, create_vhost


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _wait_migration_terminal(client, mig_id: int, timeout: float = 10.0):
    """Poll until migration reaches a terminal phase, with timeout."""
    terminal = {"done", "error", "cancelled"}
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"/api/migrations/{mig_id}")
        if resp.json()["phase"] in terminal:
            return resp.json()
        await asyncio.sleep(0.05)
    # Return last state even on timeout
    resp = await client.get(f"/api/migrations/{mig_id}")
    return resp.json()


@pytest.fixture(autouse=True)
def set_secret():
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    os.environ["NANIO_ORCHESTRATOR_SECRET"] = key
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
    """Create the migrations directory expected by the engine's sidecar writer."""
    migrations_dir = str(Path(tmp_dirs["db_path"]).parent / "migrations")
    os.makedirs(migrations_dir, exist_ok=True)


def _make_rclone_mock(returncode: int = 0):
    """Return a mock subprocess suitable for asyncio.create_subprocess_exec."""
    proc = AsyncMock()
    proc.pid = 42
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(b"Done.\n", b""))
    return proc


# ---------------------------------------------------------------------------
# 1.  Convergence loop — copies until src == dst
# ---------------------------------------------------------------------------


class TestConvergenceLoop:
    """Tests for the copying-phase convergence loop."""

    async def test_converges_on_first_pass_skips_write_routing(
        self, client, mock_nginx, mock_rclone, mock_s3
    ):
        """When src_count == dst_count after the first copy, write_routing is skipped."""
        src = await create_pool(client, "conv-src-1")
        await create_member(client, src["id"], "10.0.0.1:9000")
        dst = await create_pool(client, "conv-dst-1")
        await create_member(client, dst["id"], "10.0.0.2:9000")
        vh = await create_vhost(client, "conv1.example.com", default_pool_id=src["id"])

        mock_s3["list_buckets"].return_value = [{"name": "conv-bk1", "created": "2025-01-01"}]
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")

        # Both pools report the same count → converged immediately
        mock_s3["count_objects"].return_value = 5

        resp = await client.post("/api/migrations", json={
            "bucket": "conv-bk1",
            "src_pool_id": src["id"],
            "dst_pool_id": dst["id"],
        })
        assert resp.status_code == 201
        mig_id = resp.json()["id"]

        # Wait for engine to finish
        await _wait_migration_terminal(client, mig_id)

        log_resp = await client.get(f"/api/migrations/{mig_id}/log")
        log_lines = [e["message"] for e in log_resp.json()]

        # Should mention convergence
        assert any("skipping write-routing" in m.lower() or "converged" in m.lower()
                   for m in log_lines), f"No convergence message in: {log_lines}"

        # write_routing phase should NOT appear in any log line
        assert not any("write_routing" in m.lower() or "write-routing split" in m.lower()
                       for m in log_lines), f"Unexpected write_routing log: {log_lines}"

    async def test_multiple_passes_before_convergence(
        self, client, mock_nginx, mock_rclone, mock_s3
    ):
        """When src != dst after pass 1 but equal after pass 2, engine runs exactly 2 copy passes."""
        src = await create_pool(client, "conv-src-2")
        await create_member(client, src["id"], "10.0.0.1:9000")
        dst = await create_pool(client, "conv-dst-2")
        await create_member(client, dst["id"], "10.0.0.2:9000")
        vh = await create_vhost(client, "conv2.example.com", default_pool_id=src["id"])

        mock_s3["list_buckets"].return_value = [{"name": "conv-bk2", "created": "2025-01-01"}]
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")

        # First pair of count_objects calls: src=5, dst=3 (not converged)
        # Second pair: src=5, dst=5 (converged)
        # Each pass calls count_objects on src then dst
        call_num = {"n": 0}

        async def side_effect(addr, bucket, **_):
            call_num["n"] += 1
            if call_num["n"] <= 2:        # pass 1: src=5 then dst=3
                return 5 if call_num["n"] == 1 else 3
            return 5                        # pass 2 and beyond: both 5

        mock_s3["count_objects"].side_effect = side_effect

        resp = await client.post("/api/migrations", json={
            "bucket": "conv-bk2",
            "src_pool_id": src["id"],
            "dst_pool_id": dst["id"],
        })
        assert resp.status_code == 201
        mig_id = resp.json()["id"]

        await _wait_migration_terminal(client, mig_id)

        log_resp = await client.get(f"/api/migrations/{mig_id}/log")
        log_lines = [e["message"] for e in log_resp.json()]

        # Expect "pass 1/" and "pass 2/" in logs
        assert any("pass 1/" in m for m in log_lines), f"No pass 1 in: {log_lines}"
        assert any("pass 2/" in m for m in log_lines), f"No pass 2 in: {log_lines}"

        # Should converge on pass 2
        assert any("converged" in m.lower() for m in log_lines), f"No convergence: {log_lines}"

    async def test_source_stable_triggers_write_routing(
        self, client, mock_nginx, mock_rclone, mock_s3
    ):
        """When src count stays the same across two consecutive passes, write_routing is entered."""
        src = await create_pool(client, "conv-src-3")
        await create_member(client, src["id"], "10.0.0.1:9000")
        dst = await create_pool(client, "conv-dst-3")
        await create_member(client, dst["id"], "10.0.0.2:9000")
        vh = await create_vhost(client, "conv3.example.com", default_pool_id=src["id"])

        mock_s3["list_buckets"].return_value = [{"name": "conv-bk3", "created": "2025-01-01"}]
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")

        # src always 10, dst always 8 → src is stable (no new files), not converged
        call_num = {"n": 0}

        async def side_effect(addr, bucket, **_):
            call_num["n"] += 1
            # alternate src / dst per pass
            return 10 if call_num["n"] % 2 == 1 else 8

        mock_s3["count_objects"].side_effect = side_effect

        resp = await client.post("/api/migrations", json={
            "bucket": "conv-bk3",
            "src_pool_id": src["id"],
            "dst_pool_id": dst["id"],
        })
        assert resp.status_code == 201
        mig_id = resp.json()["id"]

        await _wait_migration_terminal(client, mig_id)

        log_resp = await client.get(f"/api/migrations/{mig_id}/log")
        log_lines = [e["message"] for e in log_resp.json()]

        # Engine should log "write-routing" or "stable" when it switches
        assert any("write-routing" in m.lower() or "stable" in m.lower()
                   for m in log_lines), f"Expected write-routing entry: {log_lines}"

    async def test_max_passes_respected(self, client, mock_nginx, mock_rclone, mock_s3):
        """Engine respects migration_max_copy_passes from settings."""
        import nanio_orchestrator.config as cfg_mod

        # Set max to 2
        s = cfg_mod.get_settings()
        original = s.migration_max_copy_passes
        s.migration_max_copy_passes = 2

        try:
            src = await create_pool(client, "maxp-src")
            await create_member(client, src["id"], "10.0.0.1:9000")
            dst = await create_pool(client, "maxp-dst")
            await create_member(client, dst["id"], "10.0.0.2:9000")
            vh = await create_vhost(client, "maxpass.example.com", default_pool_id=src["id"])

            mock_s3["list_buckets"].return_value = [{"name": "maxp-bk", "created": "2025-01-01"}]
            await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")

            # src always higher than dst — never converges
            call_num = {"n": 0}

            async def side_effect(addr, bucket, **_):
                call_num["n"] += 1
                return 10 if call_num["n"] % 2 == 1 else 8

            mock_s3["count_objects"].side_effect = side_effect

            resp = await client.post("/api/migrations", json={
                "bucket": "maxp-bk",
                "src_pool_id": src["id"],
                "dst_pool_id": dst["id"],
            })
            assert resp.status_code == 201
            mig_id = resp.json()["id"]

            await _wait_migration_terminal(client, mig_id)

            log_resp = await client.get(f"/api/migrations/{mig_id}/log")
            log_lines = [e["message"] for e in log_resp.json()]

            # No "pass 3/" log line should appear since max is 2
            assert not any("pass 3/" in m for m in log_lines), \
                f"Unexpected pass 3 found: {log_lines}"
            # "pass 2/" should appear
            assert any("pass 2/" in m for m in log_lines), \
                f"No pass 2 log: {log_lines}"
        finally:
            s.migration_max_copy_passes = original


# ---------------------------------------------------------------------------
# 2.  write_routing phase & nginx split config reload
# ---------------------------------------------------------------------------


class TestWriteRoutingPhase:
    async def test_write_routing_reloads_nginx(self, client, mock_nginx, mock_rclone, mock_s3):
        """When not converged, write_routing phase regenerates nginx config and reloads."""
        src = await create_pool(client, "wr-src")
        await create_member(client, src["id"], "10.0.0.1:9000")
        dst = await create_pool(client, "wr-dst")
        await create_member(client, dst["id"], "10.0.0.2:9000")
        vh = await create_vhost(client, "wr.example.com", default_pool_id=src["id"])

        mock_s3["list_buckets"].return_value = [{"name": "wr-bk", "created": "2025-01-01"}]
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")

        # src=10, dst=8 → never converges  → write_routing must be entered
        call_num = {"n": 0}

        async def side_effect(addr, bucket, **_):
            call_num["n"] += 1
            return 10 if call_num["n"] % 2 == 1 else 8

        mock_s3["count_objects"].side_effect = side_effect

        reload_count_before = mock_nginx["reload_nginx"].call_count

        resp = await client.post("/api/migrations", json={
            "bucket": "wr-bk",
            "src_pool_id": src["id"],
            "dst_pool_id": dst["id"],
        })
        assert resp.status_code == 201
        mig_id = resp.json()["id"]

        await _wait_migration_terminal(client, mig_id)

        # reload_nginx should have been called at least once for write_routing
        # and once more for switching
        assert mock_nginx["reload_nginx"].call_count > reload_count_before

        log_resp = await client.get(f"/api/migrations/{mig_id}/log")
        log_lines = [e["message"] for e in log_resp.json()]
        assert any("write-routing" in m.lower() for m in log_lines), \
            f"No write-routing log: {log_lines}"

    async def test_write_routing_phase_advances_to_done(
        self, client, mock_nginx, mock_rclone, mock_s3
    ):
        """Migration goes through write_routing → verifying → switching → done."""
        src = await create_pool(client, "wr2-src")
        await create_member(client, src["id"], "10.0.0.1:9000")
        dst = await create_pool(client, "wr2-dst")
        await create_member(client, dst["id"], "10.0.0.2:9000")
        vh = await create_vhost(client, "wr2.example.com", default_pool_id=src["id"])

        mock_s3["list_buckets"].return_value = [{"name": "wr2-bk", "created": "2025-01-01"}]
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")

        # Force write_routing: src stable but != dst
        call_num = {"n": 0}

        async def side_effect(addr, bucket, **_):
            call_num["n"] += 1
            return 10 if call_num["n"] % 2 == 1 else 7

        mock_s3["count_objects"].side_effect = side_effect

        resp = await client.post("/api/migrations", json={
            "bucket": "wr2-bk",
            "src_pool_id": src["id"],
            "dst_pool_id": dst["id"],
        })
        assert resp.status_code == 201
        mig_id = resp.json()["id"]

        await _wait_migration_terminal(client, mig_id)

        mig_resp = await client.get(f"/api/migrations/{mig_id}")
        data = mig_resp.json()
        assert data["phase"] in ("done", "switching"), \
            f"Unexpected phase: {data['phase']}"

    async def test_nginx_test_failure_aborts_write_routing(
        self, client, mock_rclone, mock_s3
    ):
        """If nginx test fails during write_routing, migration transitions to error (C4 fix)."""
        # Create a dedicated nginx mock with failing test_config
        test_result_fail = MagicMock(ok=False, output="syntax error")
        test_result_ok = MagicMock(ok=True, output="syntax ok")
        reload_mock = AsyncMock(return_value=MagicMock(ok=True))

        # test_config always fails — simulates bad nginx config
        with patch("nanio_orchestrator.migration_engine.test_config",
                   new=AsyncMock(return_value=test_result_fail)), \
             patch("nanio_orchestrator.migration_engine.reload_nginx", reload_mock), \
             patch("nanio_orchestrator.api.buckets.test_config",
                   new=AsyncMock(return_value=test_result_ok)), \
             patch("nanio_orchestrator.api.buckets.reload_nginx", reload_mock), \
             patch("nanio_orchestrator.nginx.executor.test_config",
                   new=AsyncMock(return_value=test_result_ok)), \
             patch("nanio_orchestrator.nginx.executor.reload_nginx", reload_mock), \
             patch("nanio_orchestrator.api.pools.trigger_backup", new=AsyncMock()), \
             patch("nanio_orchestrator.api.vhosts.trigger_backup", new=AsyncMock()):

            src = await create_pool(client, "wr3-src")
            await create_member(client, src["id"], "10.0.0.1:9000")
            dst = await create_pool(client, "wr3-dst")
            await create_member(client, dst["id"], "10.0.0.2:9000")
            vh = await create_vhost(client, "wr3.example.com", default_pool_id=src["id"])

            mock_s3["list_buckets"].return_value = [{"name": "wr3-bk", "created": "2025-01-01"}]
            await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")

            # Force write_routing path: src stable, dst less
            call_num = {"n": 0}

            async def count_side(addr, bucket, **_):
                call_num["n"] += 1
                return 10 if call_num["n"] % 2 == 1 else 7

            mock_s3["count_objects"].side_effect = count_side

            resp = await client.post("/api/migrations", json={
                "bucket": "wr3-bk",
                "src_pool_id": src["id"],
                "dst_pool_id": dst["id"],
            })
            assert resp.status_code == 201
            mig_id = resp.json()["id"]

            await _wait_migration_terminal(client, mig_id)

            # Migration should be in error phase due to nginx test failure
            mig_resp = await client.get(f"/api/migrations/{mig_id}")
            assert mig_resp.json()["phase"] == "error", \
                f"Expected error phase, got: {mig_resp.json()['phase']}"


# ---------------------------------------------------------------------------
# 3.  nginx_state sidecar: "split" for write_routing and verifying phases
# ---------------------------------------------------------------------------


class TestNginxStateSidecar:
    """_write_state_sidecar must emit nginx_state='split' for write_routing / verifying."""

    async def _get_nginx_state(self, migration_id: int, phase: str) -> str:
        """Set phase in DB and return the nginx_state written to the sidecar."""
        from nanio_orchestrator.migration_engine import _set_phase, _write_state_sidecar
        from nanio_orchestrator.db import get_db_ctx

        written = {}

        def capture(state):
            written["state"] = state

        with patch("nanio_orchestrator.migration_engine.write_migration_state",
                   side_effect=capture):
            async with get_db_ctx() as db:
                await db.execute(
                    "UPDATE migrations SET phase=? WHERE id=?", (phase, migration_id)
                )
                await db.commit()
                await _write_state_sidecar(migration_id, db)

        return written.get("state", {}).get("nginx_state")

    async def test_nginx_state_source_for_pending(self, client, mock_nginx, mock_rclone, mock_s3):
        src = await create_pool(client, "ns-src")
        await create_member(client, src["id"], "10.0.0.1:9000")
        dst = await create_pool(client, "ns-dst")
        await create_member(client, dst["id"], "10.0.0.2:9000")
        vh = await create_vhost(client, "ns.example.com", default_pool_id=src["id"])
        mock_s3["list_buckets"].return_value = [{"name": "ns-bk", "created": "2025-01-01"}]
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")

        resp = await client.post("/api/migrations", json={
            "bucket": "ns-bk",
            "src_pool_id": src["id"],
            "dst_pool_id": dst["id"],
        })
        mig_id = resp.json()["id"]
        await asyncio.sleep(0.1)

        assert await self._get_nginx_state(mig_id, "pending") == "source"
        assert await self._get_nginx_state(mig_id, "copying") == "source"

    async def test_nginx_state_split_for_write_routing(
        self, client, mock_nginx, mock_rclone, mock_s3
    ):
        src = await create_pool(client, "ns2-src")
        await create_member(client, src["id"], "10.0.0.1:9000")
        dst = await create_pool(client, "ns2-dst")
        await create_member(client, dst["id"], "10.0.0.2:9000")
        vh = await create_vhost(client, "ns2.example.com", default_pool_id=src["id"])
        mock_s3["list_buckets"].return_value = [{"name": "ns2-bk", "created": "2025-01-01"}]
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")

        resp = await client.post("/api/migrations", json={
            "bucket": "ns2-bk",
            "src_pool_id": src["id"],
            "dst_pool_id": dst["id"],
        })
        mig_id = resp.json()["id"]
        await asyncio.sleep(0.1)

        assert await self._get_nginx_state(mig_id, "write_routing") == "split"
        assert await self._get_nginx_state(mig_id, "verifying") == "split"

    async def test_nginx_state_target_for_switching(
        self, client, mock_nginx, mock_rclone, mock_s3
    ):
        src = await create_pool(client, "ns3-src")
        await create_member(client, src["id"], "10.0.0.1:9000")
        dst = await create_pool(client, "ns3-dst")
        await create_member(client, dst["id"], "10.0.0.2:9000")
        vh = await create_vhost(client, "ns3.example.com", default_pool_id=src["id"])
        mock_s3["list_buckets"].return_value = [{"name": "ns3-bk", "created": "2025-01-01"}]
        await client.post(f"/api/vhosts/{vh['id']}/buckets/sync")

        resp = await client.post("/api/migrations", json={
            "bucket": "ns3-bk",
            "src_pool_id": src["id"],
            "dst_pool_id": dst["id"],
        })
        mig_id = resp.json()["id"]
        await asyncio.sleep(0.1)

        assert await self._get_nginx_state(mig_id, "switching") == "target"
        assert await self._get_nginx_state(mig_id, "done") == "target"


# ---------------------------------------------------------------------------
# 4.  generator.generate_vhost_config — migration_dst_pool_name injection
# ---------------------------------------------------------------------------


class TestGeneratorMigrationMap:
    """generate_vhost_config attaches migration_dst_pool_name to routes."""

    async def _setup(self, client, mock_s3, bucket_name, src_name, dst_name, vhost_name):
        """Create pools/vhost/bucket-route/migration, return (vhost_id, route_path_prefix, dst_pool_name, src_id, dst_id)."""
        src = await create_pool(client, src_name)
        await create_member(client, src["id"], "10.0.0.1:9000")
        dst = await create_pool(client, dst_name)
        await create_member(client, dst["id"], "10.0.0.2:9000")
        vh = await create_vhost(client, vhost_name, default_pool_id=src["id"])

        # Create a bucket-specific route so the generator migration_map can
        # match the path_prefix against the migration's bucket name
        mock_s3["list_buckets"].return_value = [{"name": bucket_name, "created": "2025-01-01"}]
        route_resp = await client.post(f"/api/vhosts/{vh['id']}/routes", json={
            "path_prefix": f"/{bucket_name}/",
            "pool_id": src["id"],
            "enabled": True,
        })
        assert route_resp.status_code == 201, route_resp.text

        return vh["id"], f"/{bucket_name}/", dst_name, src["id"], dst["id"]

    async def test_no_migration_dst_pool_name_when_no_active_migration(
        self, client, mock_nginx, mock_rclone, mock_s3
    ):
        """Routes without an active migration in write_routing/verifying have no migration_dst_pool_name."""
        from nanio_orchestrator.nginx.generator import generate_vhost_config
        from nanio_orchestrator.db import get_db_ctx

        vhost_id, prefix, dst_name, src_id, dst_id = await self._setup(
            client, mock_s3, "plain-bk", "plain-src", "plain-dst", "plain.example.com"
        )

        _, content = await generate_vhost_config(vhost_id)
        # No migration active — no split routing block
        assert "@nanio_mig_" not in content
        assert "error_page 418" not in content

    async def test_migration_dst_pool_name_injected_for_write_routing_phase(
        self, client, mock_nginx, mock_rclone, mock_s3
    ):
        """When a migration is in write_routing phase, generate_vhost_config injects dst pool name."""
        from nanio_orchestrator.nginx.generator import generate_vhost_config
        from nanio_orchestrator.db import get_db_ctx

        vhost_id, prefix, dst_name, src_id, dst_id = await self._setup(
            client, mock_s3, "wr-bk2", "wr-gsrc", "wr-gdst", "wrgen.example.com"
        )

        # Manually insert a migration in write_routing phase
        async with get_db_ctx() as db:
            await db.execute(
                """INSERT INTO migrations
                   (vhost_id, bucket, src_pool_id, dst_pool_id, phase, mode)
                   VALUES (?, ?, ?, ?, 'write_routing', 'copy')""",
                (vhost_id, "wr-bk2", src_id, dst_id),
            )
            await db.commit()

        _, content = await generate_vhost_config(vhost_id)

        assert "@nanio_mig_" in content, f"Expected split block in:\n{content}"
        assert "error_page 418" in content, f"Expected 418 error_page in:\n{content}"
        assert dst_name in content, f"Expected dst pool name '{dst_name}' in:\n{content}"
        # Source pool (read path) should also be in the location block
        assert "wr-gsrc" in content

    async def test_migration_dst_pool_name_injected_for_verifying_phase(
        self, client, mock_nginx, mock_rclone, mock_s3
    ):
        """write_routing split config is also generated when migration phase is 'verifying'."""
        from nanio_orchestrator.nginx.generator import generate_vhost_config
        from nanio_orchestrator.db import get_db_ctx

        vhost_id, prefix, dst_name, src_id, dst_id = await self._setup(
            client, mock_s3, "ver-bk", "ver-src", "ver-dst", "vergen.example.com"
        )

        async with get_db_ctx() as db:
            await db.execute(
                """INSERT INTO migrations
                   (vhost_id, bucket, src_pool_id, dst_pool_id, phase, mode)
                   VALUES (?, ?, ?, ?, 'verifying', 'copy')""",
                (vhost_id, "ver-bk", src_id, dst_id),
            )
            await db.commit()

        _, content = await generate_vhost_config(vhost_id)

        assert "@nanio_mig_" in content
        assert "error_page 418" in content
        assert dst_name in content

    async def test_no_split_for_copying_phase_migration(
        self, client, mock_nginx, mock_rclone, mock_s3
    ):
        """Migration in copying phase does NOT generate split routing (source not yet frozen)."""
        from nanio_orchestrator.nginx.generator import generate_vhost_config
        from nanio_orchestrator.db import get_db_ctx

        vhost_id, prefix, dst_name, src_id, dst_id = await self._setup(
            client, mock_s3, "copy-bk", "copy-src", "copy-dst", "copygen.example.com"
        )

        async with get_db_ctx() as db:
            await db.execute(
                """INSERT INTO migrations
                   (vhost_id, bucket, src_pool_id, dst_pool_id, phase, mode)
                   VALUES (?, ?, ?, ?, 'copying', 'copy')""",
                (vhost_id, "copy-bk", src_id, dst_id),
            )
            await db.commit()

        _, content = await generate_vhost_config(vhost_id)

        assert "@nanio_mig_" not in content, \
            f"Unexpected split block for copying phase:\n{content}"
        assert "error_page 418" not in content


# ---------------------------------------------------------------------------
# 5.  vhost.conf.j2 template — split routing rendering
# ---------------------------------------------------------------------------


class TestVhostTemplateSplitRouting:
    """Direct rendering tests for the vhost.conf.j2 template."""

    def _render(self, routes):
        from nanio_orchestrator.nginx.generator import render_vhost

        vhost = {
            "id": 1,
            "server_name": "test.example.com",
            "listen_port": 80,
            "ssl": False,
            "ssl_cert_path": None,
            "ssl_key_path": None,
            "extra_directives": None,
        }
        return render_vhost(vhost, routes)

    def test_normal_route_no_split_directives(self):
        routes = [{
            "id": 1,
            "path_prefix": "/mybucket/",
            "pool_name": "pool-a",
            "enabled": True,
            "key_prefix": None,
            "extra_directives": None,
            "migration_dst_pool_name": None,
        }]
        content = self._render(routes)
        assert "proxy_pass         http://pool-a" in content
        assert "@nanio_mig_" not in content
        assert "error_page 418" not in content

    def test_split_route_emits_418_and_named_location(self):
        routes = [{
            "id": 7,
            "path_prefix": "/migbucket/",
            "pool_name": "src-pool",     # reads come from source
            "enabled": True,
            "key_prefix": None,
            "extra_directives": None,
            "migration_dst_pool_name": "dst-pool",
        }]
        content = self._render(routes)

        # The primary location routes reads to src-pool
        assert "proxy_pass         http://src-pool" in content

        # 418 trick routes writes to dst-pool
        assert "error_page 418 = @nanio_mig_7" in content
        assert "$request_method !~ ^(GET|HEAD|OPTIONS)$" in content
        assert "return 418" in content

        # 404 fallback keeps newly-uploaded files visible
        assert "error_page 404 = @nanio_mig_7" in content
        assert "proxy_intercept_errors on" in content

        # Named location sends to dst-pool
        assert "location @nanio_mig_7 {" in content
        assert "proxy_pass         http://dst-pool" in content

    def test_multiple_routes_each_get_named_location(self):
        routes = [
            {
                "id": 10,
                "path_prefix": "/bucket-a/",
                "pool_name": "src-pool",
                "enabled": True,
                "key_prefix": None,
                "extra_directives": None,
                "migration_dst_pool_name": "dst-pool",
            },
            {
                "id": 11,
                "path_prefix": "/bucket-b/",
                "pool_name": "src-pool",
                "enabled": True,
                "key_prefix": None,
                "extra_directives": None,
                "migration_dst_pool_name": "dst-pool",
            },
        ]
        content = self._render(routes)
        assert "location @nanio_mig_10 {" in content
        assert "location @nanio_mig_11 {" in content

    def test_disabled_route_not_rendered(self):
        routes = [{
            "id": 5,
            "path_prefix": "/disabled-bk/",
            "pool_name": "src-pool",
            "enabled": False,
            "key_prefix": None,
            "extra_directives": None,
            "migration_dst_pool_name": "dst-pool",
        }]
        content = self._render(routes)
        assert "@nanio_mig_5" not in content
        assert "/disabled-bk/" not in content

    def test_split_does_not_apply_key_prefix_rewrite(self):
        """key_prefix rewrite must be skipped in split routing mode
        (the rewrite would conflict with proxy_intercept_errors logic)."""
        routes = [{
            "id": 3,
            "path_prefix": "/kp/",
            "pool_name": "src-pool",
            "enabled": True,
            "key_prefix": "prefix/",     # would normally add a rewrite
            "extra_directives": None,
            "migration_dst_pool_name": "dst-pool",
        }]
        content = self._render(routes)
        # Split routing takes precedence; no rewrite directive should appear
        assert "rewrite" not in content
        assert "error_page 418 = @nanio_mig_3" in content

    def test_split_comment_mentions_live_migration(self):
        routes = [{
            "id": 9,
            "path_prefix": "/migbk/",
            "pool_name": "src-pool",
            "enabled": True,
            "key_prefix": None,
            "extra_directives": None,
            "migration_dst_pool_name": "dst-pool",
        }]
        content = self._render(routes)
        assert "LIVE MIGRATION" in content
