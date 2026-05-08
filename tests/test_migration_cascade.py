"""Tests for migration cascade behavior: write_routing + switching to linked http vhosts.

When a nanio pool is migrated (src → dst), any http pool with source_nanio_pool_id pointing
to src has its vhost routes updated automatically:
- write_routing phase: http vhosts get split-routing (writes → dst, reads ← src)
- switching phase: http vhost routes are updated to point to the http dst pool

These tests exercise _cascade_http_write_routing and _cascade_http_switching directly,
and verify the POST /api/migrations cascade_warnings field.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from tests.conftest import create_member, create_pool, create_route, create_vhost


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
    from pathlib import Path
    migrations_dir = str(Path(tmp_dirs["db_path"]).parent / "migrations")
    os.makedirs(migrations_dir, exist_ok=True)


async def _insert_migration(db, bucket, src_pool_id, dst_pool_id, vhost_id) -> int:
    """Insert a minimal migration record and return its id."""
    cursor = await db.execute(
        """INSERT INTO migrations (bucket, src_pool_id, dst_pool_id, vhost_id, phase, mode)
           VALUES (?, ?, ?, ?, 'write_routing', 'copy')""",
        (bucket, src_pool_id, dst_pool_id, vhost_id),
    )
    await db.commit()
    return cursor.lastrowid


# ---------------------------------------------------------------------------
# TestCascadeWriteRouting
# ---------------------------------------------------------------------------


class TestCascadeWriteRouting:
    """_cascade_http_write_routing applies split-routing to linked http vhosts."""

    async def _setup(self, client, mock_nginx, prefix):
        """Create src/dst nanio pools, linked http pools, vhosts with routes."""
        nanio_src = await create_pool(client, f"{prefix}-nsrc", pool_type="nanio")
        await create_member(client, nanio_src["id"], "10.0.0.1:9000")

        nanio_dst = await create_pool(client, f"{prefix}-ndst", pool_type="nanio")
        await create_member(client, nanio_dst["id"], "10.0.0.2:9000")

        http_src = await create_pool(
            client, f"{prefix}-hsrc", pool_type="http",
            source_nanio_pool_id=nanio_src["id"],
        )
        await create_member(client, http_src["id"], "192.168.1.10:80", role="primary")

        http_dst = await create_pool(
            client, f"{prefix}-hdst", pool_type="http",
            source_nanio_pool_id=nanio_dst["id"],
        )
        await create_member(client, http_dst["id"], "192.168.1.20:80", role="primary")

        nanio_vh = await create_vhost(
            client, f"{prefix}-nanio.example.com", default_pool_id=nanio_src["id"]
        )
        http_vh = await create_vhost(
            client, f"{prefix}-http.example.com", default_pool_id=http_src["id"]
        )
        # Add route for the migrating bucket on the http vhost
        await create_route(client, http_vh["id"], "mybucket", http_src["id"])

        return nanio_src, nanio_dst, http_src, http_dst, nanio_vh, http_vh

    async def test_cascade_writes_split_config_for_linked_http_vhost(
        self, client, app, mock_nginx
    ):
        """_cascade_http_write_routing logs a split-routing success for the http vhost."""
        from nanio_orchestrator.db import get_db_ctx
        from nanio_orchestrator.migration_engine import _cascade_http_write_routing

        nanio_src, nanio_dst, http_src, http_dst, nanio_vh, http_vh = await self._setup(
            client, mock_nginx, "cwr1"
        )

        async with get_db_ctx() as db:
            mig_id = await _insert_migration(
                db, "mybucket", nanio_src["id"], nanio_dst["id"], nanio_vh["id"]
            )

        await _cascade_http_write_routing(
            mig_id, "mybucket", nanio_src["id"], nanio_dst["id"]
        )

        # Check the migration log for the success message
        resp = await client.get(f"/api/migrations/{mig_id}/log")
        assert resp.status_code == 200
        messages = [e["message"] for e in resp.json()]
        assert any("split-routing applied" in m for m in messages), (
            f"Expected 'split-routing applied' in migration log, got: {messages}"
        )

    async def test_cascade_write_routing_warns_when_no_http_dst_pool(
        self, client, app, mock_nginx
    ):
        """_cascade_http_write_routing logs a warning when there is no linked http dst pool."""
        from nanio_orchestrator.db import get_db_ctx
        from nanio_orchestrator.migration_engine import _cascade_http_write_routing

        nanio_src = await create_pool(client, "cwr2-nsrc", pool_type="nanio")
        await create_member(client, nanio_src["id"], "10.0.0.1:9000")

        nanio_dst = await create_pool(client, "cwr2-ndst", pool_type="nanio")
        await create_member(client, nanio_dst["id"], "10.0.0.2:9000")

        # http pool linked to src but NO http pool linked to dst
        http_src = await create_pool(
            client, "cwr2-hsrc", pool_type="http",
            source_nanio_pool_id=nanio_src["id"],
        )
        await create_member(client, http_src["id"], "192.168.1.10:80", role="primary")

        nanio_vh = await create_vhost(
            client, "cwr2-nanio.example.com", default_pool_id=nanio_src["id"]
        )
        http_vh = await create_vhost(
            client, "cwr2-http.example.com", default_pool_id=http_src["id"]
        )
        await create_route(client, http_vh["id"], "bucket2", http_src["id"])

        async with get_db_ctx() as db:
            mig_id = await _insert_migration(
                db, "bucket2", nanio_src["id"], nanio_dst["id"], nanio_vh["id"]
            )

        # Must not raise even though there is no http dst pool
        await _cascade_http_write_routing(
            mig_id, "bucket2", nanio_src["id"], nanio_dst["id"]
        )

        resp = await client.get(f"/api/migrations/{mig_id}/log")
        messages = [e["message"] for e in resp.json()]
        assert any("WARNING" in m and "No http pool" in m for m in messages), (
            f"Expected warning about missing http dst pool, got: {messages}"
        )

    async def test_cascade_write_routing_noop_when_no_http_src_pool(
        self, client, app, mock_nginx
    ):
        """_cascade_http_write_routing is a no-op when no http pool is linked to src."""
        from nanio_orchestrator.db import get_db_ctx
        from nanio_orchestrator.migration_engine import _cascade_http_write_routing

        nanio_src = await create_pool(client, "cwr3-nsrc", pool_type="nanio")
        await create_member(client, nanio_src["id"], "10.0.0.1:9000")
        nanio_dst = await create_pool(client, "cwr3-ndst", pool_type="nanio")
        await create_member(client, nanio_dst["id"], "10.0.0.2:9000")

        nanio_vh = await create_vhost(
            client, "cwr3-nanio.example.com", default_pool_id=nanio_src["id"]
        )

        async with get_db_ctx() as db:
            mig_id = await _insert_migration(
                db, "bucket3", nanio_src["id"], nanio_dst["id"], nanio_vh["id"]
            )

        # No http pools linked — should complete silently
        await _cascade_http_write_routing(
            mig_id, "bucket3", nanio_src["id"], nanio_dst["id"]
        )

        resp = await client.get(f"/api/migrations/{mig_id}/log")
        messages = [e["message"] for e in resp.json()]
        # No split-routing message (nothing to cascade)
        assert not any("split-routing applied" in m for m in messages)


# ---------------------------------------------------------------------------
# TestCascadeSwitching
# ---------------------------------------------------------------------------


class TestCascadeSwitching:
    """_cascade_http_switching flips http vhost routes to the dst http pool."""

    async def _setup(self, client, mock_nginx, prefix):
        nanio_src = await create_pool(client, f"{prefix}-nsrc", pool_type="nanio")
        await create_member(client, nanio_src["id"], "10.0.0.1:9000")

        nanio_dst = await create_pool(client, f"{prefix}-ndst", pool_type="nanio")
        await create_member(client, nanio_dst["id"], "10.0.0.2:9000")

        http_src = await create_pool(
            client, f"{prefix}-hsrc", pool_type="http",
            source_nanio_pool_id=nanio_src["id"],
        )
        await create_member(client, http_src["id"], "192.168.1.10:80", role="primary")

        http_dst = await create_pool(
            client, f"{prefix}-hdst", pool_type="http",
            source_nanio_pool_id=nanio_dst["id"],
        )
        await create_member(client, http_dst["id"], "192.168.1.20:80", role="primary")

        nanio_vh = await create_vhost(
            client, f"{prefix}-nanio.example.com", default_pool_id=nanio_src["id"]
        )
        http_vh = await create_vhost(
            client, f"{prefix}-http.example.com", default_pool_id=http_src["id"]
        )
        route = await create_route(client, http_vh["id"], "bucket-sw", http_src["id"])

        return nanio_src, nanio_dst, http_src, http_dst, nanio_vh, http_vh, route

    async def test_cascade_switching_updates_route_to_dst(
        self, client, app, mock_nginx
    ):
        """_cascade_http_switching updates the http vhost route pool_id to http_dst."""
        from nanio_orchestrator.db import get_db_ctx
        from nanio_orchestrator.migration_engine import _cascade_http_switching

        nanio_src, nanio_dst, http_src, http_dst, nanio_vh, http_vh, route = (
            await self._setup(client, mock_nginx, "csw1")
        )

        async with get_db_ctx() as db:
            mig_id = await _insert_migration(
                db, "bucket-sw", nanio_src["id"], nanio_dst["id"], nanio_vh["id"]
            )

        swept = await _cascade_http_switching(
            mig_id, "bucket-sw", nanio_src["id"], nanio_dst["id"]
        )

        assert len(swept) == 1, f"Expected 1 swept vhost, got: {swept}"
        assert "csw1-http.example.com" in swept[0]

        # Verify DB: route must now point to http_dst
        async with get_db_ctx() as db:
            rows = await db.execute_fetchall(
                "SELECT pool_id FROM routes WHERE id = ?", (route["id"],)
            )
        assert rows[0]["pool_id"] == http_dst["id"], (
            "http vhost route must point to http_dst after cascade switching"
        )

    async def test_cascade_switching_logs_success(self, client, app, mock_nginx):
        """cascade switching writes a success log entry."""
        from nanio_orchestrator.db import get_db_ctx
        from nanio_orchestrator.migration_engine import _cascade_http_switching

        nanio_src, nanio_dst, http_src, http_dst, nanio_vh, http_vh, _ = (
            await self._setup(client, mock_nginx, "csw2")
        )

        async with get_db_ctx() as db:
            mig_id = await _insert_migration(
                db, "bucket-sw", nanio_src["id"], nanio_dst["id"], nanio_vh["id"]
            )

        await _cascade_http_switching(
            mig_id, "bucket-sw", nanio_src["id"], nanio_dst["id"]
        )

        resp = await client.get(f"/api/migrations/{mig_id}/log")
        messages = [e["message"] for e in resp.json()]
        assert any("route updated" in m for m in messages), (
            f"Expected 'route updated' in log, got: {messages}"
        )

    async def test_cascade_switching_warns_when_no_http_dst_pool(
        self, client, app, mock_nginx
    ):
        """cascade switching logs a warning and returns empty swept list when no http dst pool."""
        from nanio_orchestrator.db import get_db_ctx
        from nanio_orchestrator.migration_engine import _cascade_http_switching

        nanio_src = await create_pool(client, "csw3-nsrc", pool_type="nanio")
        await create_member(client, nanio_src["id"], "10.0.0.1:9000")
        nanio_dst = await create_pool(client, "csw3-ndst", pool_type="nanio")
        await create_member(client, nanio_dst["id"], "10.0.0.2:9000")

        # http src linked to nanio_src, but NO http dst pool for nanio_dst
        http_src = await create_pool(
            client, "csw3-hsrc", pool_type="http",
            source_nanio_pool_id=nanio_src["id"],
        )
        await create_member(client, http_src["id"], "192.168.1.10:80", role="primary")

        nanio_vh = await create_vhost(
            client, "csw3-nanio.example.com", default_pool_id=nanio_src["id"]
        )
        http_vh = await create_vhost(
            client, "csw3-http.example.com", default_pool_id=http_src["id"]
        )
        await create_route(client, http_vh["id"], "bucket-csw3", http_src["id"])

        async with get_db_ctx() as db:
            mig_id = await _insert_migration(
                db, "bucket-csw3", nanio_src["id"], nanio_dst["id"], nanio_vh["id"]
            )

        swept = await _cascade_http_switching(
            mig_id, "bucket-csw3", nanio_src["id"], nanio_dst["id"]
        )

        assert swept == [], "No vhosts swept when http dst pool is missing"

        resp = await client.get(f"/api/migrations/{mig_id}/log")
        messages = [e["message"] for e in resp.json()]
        assert any("WARNING" in m and "No http pool" in m for m in messages), (
            f"Expected warning about missing http dst pool, got: {messages}"
        )

    async def test_cascade_switching_noop_when_no_http_src_pool(
        self, client, app, mock_nginx
    ):
        """cascade switching is a no-op when no http pool is linked to src."""
        from nanio_orchestrator.db import get_db_ctx
        from nanio_orchestrator.migration_engine import _cascade_http_switching

        nanio_src = await create_pool(client, "csw4-nsrc", pool_type="nanio")
        await create_member(client, nanio_src["id"], "10.0.0.1:9000")
        nanio_dst = await create_pool(client, "csw4-ndst", pool_type="nanio")
        await create_member(client, nanio_dst["id"], "10.0.0.2:9000")

        nanio_vh = await create_vhost(
            client, "csw4-nanio.example.com", default_pool_id=nanio_src["id"]
        )

        async with get_db_ctx() as db:
            mig_id = await _insert_migration(
                db, "bucket-csw4", nanio_src["id"], nanio_dst["id"], nanio_vh["id"]
            )

        swept = await _cascade_http_switching(
            mig_id, "bucket-csw4", nanio_src["id"], nanio_dst["id"]
        )

        assert swept == [], "swept must be empty when no http pool is linked to src"


# ---------------------------------------------------------------------------
# TestCascadeWarningsInMigrationsAPI
# ---------------------------------------------------------------------------


class TestCascadeWarningsInMigrationsAPI:
    """POST /api/migrations returns cascade_warnings when http vhosts lack routes."""

    async def test_cascade_warnings_when_http_vhost_has_no_route(
        self, client, mock_nginx, mock_s3, mock_rclone
    ):
        """cascade_warnings is populated when an http vhost is linked but lacks the bucket route."""
        nanio_src = await create_pool(client, "caw1-nsrc", pool_type="nanio")
        await create_member(client, nanio_src["id"], "10.0.0.1:9000")

        nanio_dst = await create_pool(client, "caw1-ndst", pool_type="nanio")
        await create_member(client, nanio_dst["id"], "10.0.0.2:9000")

        # http pool linked to src but no route for the bucket
        http_src = await create_pool(
            client, "caw1-hsrc", pool_type="http",
            source_nanio_pool_id=nanio_src["id"],
        )
        await create_member(client, http_src["id"], "192.168.1.10:80", role="primary")

        nanio_vh = await create_vhost(
            client, "caw1-nanio.example.com", default_pool_id=nanio_src["id"]
        )
        # Sync so bucket_sync knows about the bucket
        mock_s3["list_buckets"].return_value = [{"name": "caw-bk", "created": "2025-01-01"}]
        await client.post(f"/api/vhosts/{nanio_vh['id']}/buckets/sync")

        # Create the nanio route required for migration
        await create_route(client, nanio_vh["id"], "caw-bk", nanio_src["id"])

        resp = await client.post("/api/migrations", json={
            "bucket": "caw-bk",
            "src_pool_id": nanio_src["id"],
            "dst_pool_id": nanio_dst["id"],
        })
        assert resp.status_code == 201
        data = resp.json()
        assert "cascade_warnings" in data, (
            "Response must contain cascade_warnings when http vhost lacks bucket route"
        )
        assert len(data["cascade_warnings"]) >= 1
        assert any("caw1-hsrc" in w for w in data["cascade_warnings"]), (
            f"cascade_warnings must mention the affected http pool, got: {data['cascade_warnings']}"
        )

    async def test_no_cascade_warnings_when_http_vhost_has_route(
        self, client, mock_nginx, mock_s3, mock_rclone
    ):
        """cascade_warnings is absent or empty when the http vhost already has the route."""
        nanio_src = await create_pool(client, "caw2-nsrc", pool_type="nanio")
        await create_member(client, nanio_src["id"], "10.0.0.1:9000")

        nanio_dst = await create_pool(client, "caw2-ndst", pool_type="nanio")
        await create_member(client, nanio_dst["id"], "10.0.0.2:9000")

        http_src = await create_pool(
            client, "caw2-hsrc", pool_type="http",
            source_nanio_pool_id=nanio_src["id"],
        )
        await create_member(client, http_src["id"], "192.168.1.10:80", role="primary")

        nanio_vh = await create_vhost(
            client, "caw2-nanio.example.com", default_pool_id=nanio_src["id"]
        )
        http_vh = await create_vhost(
            client, "caw2-http.example.com", default_pool_id=http_src["id"]
        )

        mock_s3["list_buckets"].return_value = [{"name": "caw2-bk", "created": "2025-01-01"}]
        await client.post(f"/api/vhosts/{nanio_vh['id']}/buckets/sync")

        # Add route to BOTH vhosts
        await create_route(client, nanio_vh["id"], "caw2-bk", nanio_src["id"])
        await create_route(client, http_vh["id"], "caw2-bk", http_src["id"])

        resp = await client.post("/api/migrations", json={
            "bucket": "caw2-bk",
            "src_pool_id": nanio_src["id"],
            "dst_pool_id": nanio_dst["id"],
        })
        assert resp.status_code == 201
        data = resp.json()
        # No cascade_warnings (or empty list)
        assert not data.get("cascade_warnings"), (
            f"cascade_warnings must be absent or empty when http vhost has the route, got: {data.get('cascade_warnings')}"
        )

    async def test_no_cascade_warnings_when_no_http_pool_linked(
        self, client, mock_nginx, mock_s3, mock_rclone
    ):
        """cascade_warnings is absent when no http pool is linked to the src pool."""
        nanio_src = await create_pool(client, "caw3-nsrc", pool_type="nanio")
        await create_member(client, nanio_src["id"], "10.0.0.1:9000")

        nanio_dst = await create_pool(client, "caw3-ndst", pool_type="nanio")
        await create_member(client, nanio_dst["id"], "10.0.0.2:9000")

        nanio_vh = await create_vhost(
            client, "caw3-nanio.example.com", default_pool_id=nanio_src["id"]
        )

        mock_s3["list_buckets"].return_value = [{"name": "caw3-bk", "created": "2025-01-01"}]
        await client.post(f"/api/vhosts/{nanio_vh['id']}/buckets/sync")
        await create_route(client, nanio_vh["id"], "caw3-bk", nanio_src["id"])

        resp = await client.post("/api/migrations", json={
            "bucket": "caw3-bk",
            "src_pool_id": nanio_src["id"],
            "dst_pool_id": nanio_dst["id"],
        })
        assert resp.status_code == 201
        data = resp.json()
        assert not data.get("cascade_warnings"), (
            "cascade_warnings must be absent when no http pools are linked"
        )
