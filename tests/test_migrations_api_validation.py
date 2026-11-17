"""The migrations API: what it refuses before a migration is created, how it finds the vhost
of a bucket, and the read endpoints (source buckets, stale, phase filter, log)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio

from tests.conftest import create_member, create_pool, create_route, create_vhost
from tests.test_migration_engine_rclone import ScriptedRclone, setup_bucket, use_settings, wait_terminal

pytestmark = pytest.mark.usefixtures("set_secret", "ensure_migrations_dir")


@pytest.fixture
def scripted():
    fake = ScriptedRclone()
    with patch("asyncio.create_subprocess_exec", new=fake):
        yield fake


@pytest_asyncio.fixture
async def engine_busy(monkeypatch):
    """One migration slot, already taken by a migration that never ends."""
    from nanio_orchestrator.migration_engine import _active_tasks

    use_settings(monkeypatch, migration_max_parallel=1)
    running = asyncio.create_task(asyncio.Event().wait())
    _active_tasks[-1] = running
    yield
    _active_tasks.pop(-1, None)
    running.cancel()


async def _query(sql: str, params=()) -> list[dict]:
    from nanio_orchestrator.db import get_db_ctx

    async with get_db_ctx() as db:
        return [dict(r) for r in await db.execute_fetchall(sql, params)]


async def _execute(sql: str, params=()) -> int:
    from nanio_orchestrator.db import get_db_ctx

    async with get_db_ctx() as db:
        cursor = await db.execute(sql, params)
        await db.commit()
        return cursor.lastrowid


async def _post(client, expect: int, **body) -> dict:
    resp = await client.post("/api/migrations", json=body)
    assert resp.status_code == expect, resp.text
    return resp.json()


async def _insert_migration(vhost, bucket, src, dst, phase) -> int:
    return await _execute(
        "INSERT INTO migrations (vhost_id, bucket, src_pool_id, dst_pool_id, phase) VALUES (?, ?, ?, ?, ?)",
        (vhost["id"], bucket, src["id"], dst["id"], phase),
    )


async def _http_pool(client, name, linked_to=None):
    extra = {"source_nanio_pool_id": linked_to["id"]} if linked_to else {}
    pool = await create_pool(client, name, pool_type="http", **extra)
    await create_member(client, pool["id"], "10.0.1.1:80", role="primary")
    return pool


# ── Whole-bucket migrations ───────────────────────────────────────────────────


class TestBucketMigrationRefusals:
    @pytest.mark.parametrize(("side", "label"), [("src_pool_id", "Source"), ("dst_pool_id", "Destination")])
    async def test_unknown_pool(self, client, mock_nginx, mock_s3, side, label):
        src, dst, _, _ = await setup_bucket(client, mock_s3)
        body = {"bucket": "bk", "src_pool_id": src["id"], "dst_pool_id": dst["id"], side: 9999}

        detail = (await _post(client, 400, **body))["detail"]

        assert detail == f"{label} pool 9999 not found"
        assert await _query("SELECT id FROM migrations") == []

    async def test_http_pool_cannot_take_part(self, client, mock_nginx, mock_s3):
        src, _, _, _ = await setup_bucket(client, mock_s3)
        web = await _http_pool(client, "web")

        detail = (await _post(client, 400, bucket="bk", src_pool_id=src["id"], dst_pool_id=web["id"]))["detail"]

        assert detail == (
            "Destination pool 'web' is of type 'http'. Migrations can only be performed between nanio pools."
        )
        assert await _query("SELECT id FROM migrations") == []

    async def test_bucket_without_a_route_has_nothing_to_switch(self, client, mock_nginx, mock_s3):
        src, dst, vhost, route = await setup_bucket(client, mock_s3)
        resp = await client.delete(f"/api/vhosts/{vhost['id']}/routes/{route['id']}")
        assert resp.status_code == 204, resp.text

        detail = (await _post(client, 400, bucket="bk", src_pool_id=src["id"], dst_pool_id=dst["id"]))["detail"]

        assert detail.startswith(f"No nginx route found for bucket 'bk' in vhost {vhost['id']}.")
        assert await _query("SELECT id FROM migrations") == []

    async def test_source_must_be_the_pool_serving_the_bucket(self, client, mock_nginx, mock_s3):
        src, dst, _, _ = await setup_bucket(client, mock_s3)
        other = await create_pool(client, "eng-other")
        await create_member(client, other["id"], "10.0.0.3:9000")

        detail = (await _post(client, 400, bucket="bk", src_pool_id=other["id"], dst_pool_id=dst["id"]))["detail"]

        assert detail.startswith(
            f"Source pool mismatch: bucket 'bk' is currently routed to pool 'eng-src' (id={src['id']}), "
            f"not pool {other['id']}."
        )
        assert await _query("SELECT id FROM migrations") == []

    async def test_bucket_that_disappeared_from_its_pool(self, client, mock_nginx, mock_s3):
        src, dst, _, _ = await setup_bucket(client, mock_s3)
        await _execute("UPDATE bucket_sync SET status = 'deleted' WHERE bucket = 'bk'")

        detail = (await _post(client, 400, bucket="bk", src_pool_id=src["id"], dst_pool_id=dst["id"]))["detail"]

        assert detail.startswith("Bucket 'bk' no longer exists on source pool.")
        assert await _query("SELECT id FROM migrations") == []

    async def test_source_pool_that_cannot_be_reached(self, client, mock_nginx, mock_s3):
        src, dst, _, _ = await setup_bucket(client, mock_s3)
        mock_s3["src_bucket_exists"].side_effect = RuntimeError("connection refused")

        detail = (await _post(client, 503, bucket="bk", src_pool_id=src["id"], dst_pool_id=dst["id"]))["detail"]

        assert detail == f"Cannot reach source pool {src['id']} to validate bucket 'bk': connection refused"
        assert await _query("SELECT id FROM migrations") == []

    async def test_no_free_migration_slot(self, client, mock_nginx, mock_s3, engine_busy):
        src, dst, _, _ = await setup_bucket(client, mock_s3)

        detail = (await _post(client, 429, bucket="bk", src_pool_id=src["id"], dst_pool_id=dst["id"]))["detail"]

        assert detail.startswith("Max parallel migrations reached (1).")
        assert await _query("SELECT id FROM migrations") == []


class TestVhostOfABucket:
    """A bucket the sync has not seen yet belongs to the vhost whose default pool is the source."""

    async def _unsynced_bucket(self, client, with_vhost=True):
        src = await create_pool(client, "uns-src")
        await create_member(client, src["id"], "10.0.0.1:9000")
        dst = await create_pool(client, "uns-dst")
        await create_member(client, dst["id"], "10.0.0.2:9000")
        vhost = None
        if with_vhost:
            vhost = await create_vhost(client, "uns.example.com", default_pool_id=src["id"])
            await create_route(client, vhost["id"], "fresh", src["id"])
        await _execute("DELETE FROM bucket_sync")
        return src, dst, vhost

    async def test_found_through_the_default_pool(self, client, mock_nginx, mock_s3, scripted):
        src, dst, vhost = await self._unsynced_bucket(client)
        mock_s3["count_objects"].return_value = 5

        created = await _post(client, 201, bucket="fresh", src_pool_id=src["id"], dst_pool_id=dst["id"])

        assert created["vhost_id"] == vhost["id"]
        assert (await wait_terminal(client, created["id"]))["phase"] == "done"

    async def test_no_vhost_serves_the_source_pool(self, client, mock_nginx, mock_s3):
        src, dst, _ = await self._unsynced_bucket(client, with_vhost=False)

        detail = (await _post(client, 400, bucket="fresh", src_pool_id=src["id"], dst_pool_id=dst["id"]))["detail"]

        assert detail.startswith("Cannot determine vhost for this bucket.")
        assert await _query("SELECT id FROM migrations") == []


# ── Folder migrations ─────────────────────────────────────────────────────────


class TestFolderMigrationVhost:
    async def _two_vhosts(self, client, mock_s3):
        """Two S3 vhosts and a bucket the sync has not attributed to either."""
        src, dst, first, _ = await setup_bucket(client, mock_s3)
        second = await create_vhost(client, "second.example.com", default_pool_id=src["id"])
        await _execute("DELETE FROM bucket_sync")
        return src, dst, first, second

    async def test_ambiguous_vhost_is_refused(self, client, mock_nginx, mock_s3):
        _, dst, _, _ = await self._two_vhosts(client, mock_s3)

        detail = (await _post(client, 400, bucket="bk", folder="2025/", dst_pool_id=dst["id"]))["detail"]

        assert detail == "Cannot tell which vhost serves bucket 'bk'. Send vhost_id."
        assert await _query("SELECT id FROM routes WHERE path_prefix = '/bk/2025/'") == []

    async def test_vhost_id_settles_it(self, client, mock_nginx, mock_s3, scripted):
        _, dst, _, second = await self._two_vhosts(client, mock_s3)

        created = await _post(client, 201, bucket="bk", folder="2025/", dst_pool_id=dst["id"], vhost_id=second["id"])

        assert created["vhost_id"] == second["id"]
        assert (await wait_terminal(client, created["id"]))["phase"] == "done"
        routes = await _query("SELECT vhost_id, pool_id FROM routes WHERE path_prefix = '/bk/2025/'")
        assert routes == [{"vhost_id": second["id"], "pool_id": dst["id"]}]

    async def test_unknown_vhost_id(self, client, mock_nginx, mock_s3):
        _, dst, _, _ = await self._two_vhosts(client, mock_s3)

        detail = (await _post(client, 400, bucket="bk", folder="2025/", dst_pool_id=dst["id"], vhost_id=9999))["detail"]

        assert detail == "Cannot tell which vhost serves bucket 'bk'. Send vhost_id."

    async def test_vhost_serving_plain_files_has_no_folders_to_move(self, client, mock_nginx, mock_s3):
        _, dst, _, _ = await setup_bucket(client, mock_s3)
        web = await _http_pool(client, "web")
        cdn = await create_vhost(client, "cdn.example.com", default_pool_id=web["id"])

        detail = (await _post(client, 400, bucket="bk", folder="2025/", dst_pool_id=dst["id"], vhost_id=cdn["id"]))[
            "detail"
        ]

        assert detail == f"Bucket 'bk' is not served by an S3 pool on vhost {cdn['id']}."


class TestFolderMigrationRefusals:
    async def test_unknown_destination_pool(self, client, mock_nginx, mock_s3):
        await setup_bucket(client, mock_s3)

        detail = (await _post(client, 400, bucket="bk", folder="2025/", dst_pool_id=9999))["detail"]

        assert detail == "Destination pool 9999 not found"

    async def test_http_destination_pool(self, client, mock_nginx, mock_s3):
        await setup_bucket(client, mock_s3)
        web = await _http_pool(client, "web")

        detail = (await _post(client, 400, bucket="bk", folder="2025/", dst_pool_id=web["id"]))["detail"]

        assert detail == "Migrations can only be performed between nanio pools."

    async def test_destination_pool_without_members(self, client, mock_nginx, mock_s3):
        await setup_bucket(client, mock_s3)
        empty = await create_pool(client, "no-members")

        detail = (await _post(client, 400, bucket="bk", folder="2025/", dst_pool_id=empty["id"]))["detail"]

        assert detail == "Both pools need at least one enabled member."

    async def test_source_pool_that_cannot_be_reached(self, client, mock_nginx, mock_s3):
        src, dst, _, _ = await setup_bucket(client, mock_s3)
        mock_s3["src_bucket_has_objects"].side_effect = RuntimeError("connection refused")

        detail = (await _post(client, 503, bucket="bk", folder="2025/", dst_pool_id=dst["id"]))["detail"]

        assert detail == f"Cannot reach source pool {src['id']} to check '/bk/2025/': connection refused"
        assert await _query("SELECT id FROM routes WHERE path_prefix = '/bk/2025/'") == []

    async def test_folder_route_rejected_by_nginx_is_not_kept(self, client, mock_nginx, mock_s3):
        from nanio_orchestrator.config import get_settings

        _, dst, _, _ = await setup_bucket(client, mock_s3)
        conf = get_settings().vhosts_dir / "eng.example.com.conf"
        before = conf.read_text()
        mock_nginx["test_config"].return_value = MagicMock(ok=False, output="nginx: [emerg] rejected")

        detail = (await _post(client, 400, bucket="bk", folder="2025/", dst_pool_id=dst["id"]))["detail"]

        assert detail == "nginx rejected the folder route: nginx: [emerg] rejected"
        assert await _query("SELECT id FROM routes WHERE path_prefix = '/bk/2025/'") == []
        assert await _query("SELECT id FROM migrations") == []
        assert conf.read_text() == before

    async def test_no_free_migration_slot(self, client, mock_nginx, mock_s3, engine_busy):
        _, dst, _, _ = await setup_bucket(client, mock_s3)

        detail = (await _post(client, 429, bucket="bk", folder="2025/", dst_pool_id=dst["id"]))["detail"]

        assert detail.startswith("Max parallel migrations reached (1).")
        assert await _query("SELECT id FROM migrations") == []


# ── Read endpoints ────────────────────────────────────────────────────────────


class TestSourceBuckets:
    async def test_lists_the_buckets_routed_to_the_pool(self, client, mock_nginx, mock_s3):
        src, dst, vhost, _ = await setup_bucket(client, mock_s3)
        mock_s3["list_buckets"].return_value = [
            {"name": name, "created": "2025-01-01"} for name in ("bk", "archive", "unrouted")
        ]
        await create_route(client, vhost["id"], "archive", src["id"])
        await client.post(f"/api/vhosts/{vhost['id']}/buckets/sync")

        on_src = (await client.get(f"/api/migrations/source-buckets?pool_id={src['id']}")).json()
        on_dst = (await client.get(f"/api/migrations/source-buckets?pool_id={dst['id']}")).json()

        assert on_src == {"pool_id": src["id"], "buckets": ["archive", "bk"]}
        assert on_dst == {"pool_id": dst["id"], "buckets": []}

    async def test_bucket_gone_from_its_pool_is_not_offered(self, client, mock_nginx, mock_s3):
        src, _, vhost, _ = await setup_bucket(client, mock_s3)
        await client.post(f"/api/vhosts/{vhost['id']}/buckets/sync")
        url = f"/api/migrations/source-buckets?pool_id={src['id']}"
        assert (await client.get(url)).json()["buckets"] == ["bk"]
        await _execute("UPDATE bucket_sync SET status = 'deleted' WHERE bucket = 'bk'")

        assert (await client.get(url)).json()["buckets"] == []


class TestStaleMigrations:
    async def _stale(self, client) -> list[dict]:
        resp = await client.get("/api/migrations/stale")
        assert resp.status_code == 200, resp.text
        return resp.json()

    @pytest.mark.parametrize(("side", "reason"), [("src", "src_no_members"), ("dst", "dst_no_members")])
    async def test_pool_that_lost_its_members(self, client, mock_nginx, mock_s3, side, reason):
        src, dst, vhost, _ = await setup_bucket(client, mock_s3)
        mig_id = await _insert_migration(vhost, "bk", src, dst, "copying")
        pool = src if side == "src" else dst
        await _execute("UPDATE pool_members SET enabled = 0 WHERE pool_id = ?", (pool["id"],))
        mock_s3["src_bucket_exists"].reset_mock()

        stale = await self._stale(client)

        assert [(s["migration_id"], s["bucket"], s["phase"], s["reason"]) for s in stale] == [
            (mig_id, "bk", "copying", reason)
        ]
        mock_s3["src_bucket_exists"].assert_not_called()  # nobody to ask

    async def test_failed_probe_never_marks_a_migration_stale(self, client, mock_nginx, mock_s3):
        src, dst, vhost, _ = await setup_bucket(client, mock_s3)
        await _insert_migration(vhost, "bk", src, dst, "copying")
        mock_s3["src_bucket_exists"].side_effect = RuntimeError("connection refused")

        assert await self._stale(client) == []
        mock_s3["src_bucket_exists"].assert_awaited_once()

    async def test_source_bucket_is_not_expected_once_switching(self, client, mock_nginx, mock_s3):
        src, dst, vhost, _ = await setup_bucket(client, mock_s3)
        copying = await _insert_migration(vhost, "bk", src, dst, "copying")
        await _insert_migration(vhost, "other", src, dst, "switching")
        mock_s3["src_bucket_exists"].return_value = False

        stale = await self._stale(client)

        assert [(s["migration_id"], s["reason"]) for s in stale] == [(copying, "src_bucket_missing")]

    async def test_healthy_and_finished_migrations_are_not_stale(self, client, mock_nginx, mock_s3):
        src, dst, vhost, _ = await setup_bucket(client, mock_s3)
        await _insert_migration(vhost, "bk", src, dst, "verifying")
        await _insert_migration(vhost, "old", src, dst, "error")
        mock_s3["src_bucket_exists"].return_value = True

        assert await self._stale(client) == []
        assert [c.args[1] for c in mock_s3["src_bucket_exists"].await_args_list] == ["bk"]


class TestListAndLog:
    async def test_list_can_be_filtered_by_phase(self, client, mock_nginx, mock_s3):
        src, dst, vhost, _ = await setup_bucket(client, mock_s3)
        done = await _insert_migration(vhost, "a", src, dst, "done")
        failed = await _insert_migration(vhost, "b", src, dst, "error")

        everything = (await client.get("/api/migrations")).json()
        errors = (await client.get("/api/migrations?phase=error")).json()

        assert [m["id"] for m in everything] == [failed, done]
        assert [(m["id"], m["bucket"], m["phase"]) for m in errors] == [(failed, "b", "error")]

    async def test_log_of_a_migration_that_logged_nothing_is_empty(self, client, mock_nginx, mock_s3):
        src, dst, vhost, _ = await setup_bucket(client, mock_s3)
        mig_id = await _insert_migration(vhost, "bk", src, dst, "pending")

        resp = await client.get(f"/api/migrations/{mig_id}/log")

        assert resp.status_code == 200
        assert resp.json() == []

    async def test_log_of_an_unknown_migration(self, client):
        resp = await client.get("/api/migrations/9999/log")

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Migration not found"
