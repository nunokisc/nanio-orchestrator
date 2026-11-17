"""Bucket sync: which vhosts are synced, how bucket states change, and the background loop."""

from __future__ import annotations

import asyncio
import logging
import re

import nanio_orchestrator.config as cfg_mod
import nanio_orchestrator.db as db_mod
import pytest
import pytest_asyncio
from nanio_orchestrator import bucket_sync, s3client, subfolders

DEFAULT, BIG, WEB, IDLE = 1, 2, 3, 4
DEFAULT_NODE, BIG_NODE = "10.0.0.1:9000", "10.0.0.2:9000"


class FakeS3:
    """What each node holds. A node mapped to an exception is unreachable."""

    def __init__(self):
        self.nodes: dict = {DEFAULT_NODE: ["photos"], BIG_NODE: []}
        self.create_result = (True, "created")
        self.put_result = True
        self.created: list = []
        self.puts: list = []
        self.list_calls: list = []

    def _buckets(self, address):
        held = self.nodes.get(address, [])
        if isinstance(held, Exception):
            raise held
        return held

    async def list_buckets(self, address, **kw):
        self.list_calls.append(address)
        return [{"name": name} for name in self._buckets(address)]

    async def bucket_exists(self, address, bucket, **kw):
        return bucket in self._buckets(address)

    async def create_bucket(self, address, bucket, **kw):
        self.created.append((address, bucket))
        return self.create_result

    async def put_object(self, address, bucket, key, data, **kw):
        self.puts.append((address, bucket, key))
        if isinstance(self.put_result, Exception):
            raise self.put_result
        return self.put_result


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(bucket_sync, "list_buckets", fake.list_buckets)
    monkeypatch.setattr(bucket_sync, "bucket_exists", fake.bucket_exists)
    monkeypatch.setattr(s3client, "create_bucket", fake.create_bucket)
    monkeypatch.setattr(s3client, "put_object", fake.put_object)
    return fake


@pytest_asyncio.fixture
async def db(tmp_dirs, monkeypatch):
    """A vhost on the `default` pool, plus a second nanio pool, an http pool and a pool with no
    enabled member."""
    monkeypatch.setenv("NANIO_ORCHESTRATOR_DB_PATH", tmp_dirs["db_path"])
    monkeypatch.setenv("NANIO_ORCHESTRATOR_NGINX_CONFIG_DIR", tmp_dirs["nginx_dir"])
    cfg_mod.settings = None
    db_mod.set_db_path(tmp_dirs["db_path"])
    await db_mod.init_db()
    async with db_mod.get_db_ctx() as conn:
        await conn.execute(
            """INSERT INTO pools (id, name, type) VALUES
               (1, 'default', 'nanio'), (2, 'big', 'nanio'), (3, 'web', 'http'), (4, 'idle', 'nanio')"""
        )
        await conn.execute(
            """INSERT INTO pool_members (pool_id, address, enabled) VALUES
               (1, '10.0.0.1:9000', 1), (2, '10.0.0.2:9000', 1), (3, '10.0.0.3:80', 1), (4, '10.0.0.4:9000', 0)"""
        )
        await conn.execute(
            "INSERT INTO vhosts (id, server_name, listen_port, ssl, default_pool_id) VALUES (1, 's3.example.com', 80, 0, 1)"
        )
        await conn.commit()
        yield conn
    cfg_mod.settings = None
    db_mod._db_path = None


async def _route(db, prefix: str, pool_id: int, vhost_id: int = 1) -> None:
    await db.execute(
        "INSERT INTO routes (vhost_id, path_prefix, pool_id) VALUES (?, ?, ?)", (vhost_id, prefix, pool_id)
    )
    await db.commit()


async def _states(db, vhost_id: int = 1) -> dict:
    rows = await db.execute_fetchall(
        "SELECT bucket, status, routed_pool_id FROM bucket_sync WHERE vhost_id = ? ORDER BY bucket", (vhost_id,)
    )
    return {r["bucket"]: (r["status"], r["routed_pool_id"]) for r in rows}


sync = bucket_sync.sync_vhost_buckets_once


class TestWhichVhostsAreSynced:
    async def test_unknown_vhost(self, db, s3):
        assert await sync(99) == {"error": "Vhost not found"}
        assert s3.list_calls == []

    async def test_vhost_without_a_default_pool_is_skipped(self, db, s3):
        await db.execute("UPDATE vhosts SET default_pool_id = NULL")
        await db.commit()
        assert await sync(1) == {"vhost_id": 1, "skipped": True, "reason": "No default_pool_id configured"}
        assert s3.list_calls == []

    async def test_vhost_on_an_http_pool_is_skipped(self, db, s3):
        await db.execute("UPDATE vhosts SET default_pool_id = ?", (WEB,))
        await db.commit()
        result = await sync(1)
        assert result == {
            "vhost_id": 1,
            "skipped": True,
            "reason": "Default pool is not nanio type — bucket sync skipped",
        }
        assert s3.list_calls == []

    async def test_vhost_whose_pool_has_no_enabled_member_is_skipped(self, db, s3):
        await db.execute("UPDATE vhosts SET default_pool_id = ?", (IDLE,))
        await db.commit()
        assert await sync(1) == {"vhost_id": 1, "skipped": True, "reason": "Default pool has no enabled members"}
        assert s3.list_calls == []

    async def test_unreachable_default_pool_is_an_error_and_changes_nothing(self, db, s3):
        await sync(1)
        before = await _states(db)
        s3.nodes[DEFAULT_NODE] = ConnectionError("connection refused")

        assert await sync(1) == {"vhost_id": 1, "error": "connection refused"}
        assert await _states(db) == before == {"photos": ("unrouted", None)}

    async def test_sync_all_covers_only_vhosts_on_a_nanio_pool(self, db, s3):
        await db.execute(
            """INSERT INTO vhosts (id, server_name, listen_port, ssl, default_pool_id) VALUES
               (2, 'cdn.example.com', 80, 0, 3), (3, 'bare.example.com', 80, 0, NULL), (4, 'big.example.com', 80, 0, 2)"""
        )
        await db.commit()
        s3.nodes[BIG_NODE] = ["videos", "logs"]

        results = await bucket_sync.sync_all_vhosts()

        assert sorted((r["vhost_id"], r["buckets_found"]) for r in results) == [(1, 1), (4, 2)]
        assert sorted(s3.list_calls) == [DEFAULT_NODE, BIG_NODE]
        assert await _states(db, 4) == {"logs": ("unrouted", None), "videos": ("unrouted", None)}


class TestBucketStates:
    async def test_first_sync_records_routed_and_unrouted_buckets(self, db, s3):
        s3.nodes[DEFAULT_NODE] = ["photos", "logs"]
        await _route(db, "/", DEFAULT)  # the catch-all route is not a bucket
        await _route(db, "/photos/", DEFAULT)

        result = await sync(1)

        assert (result["vhost_id"], result["buckets_found"]) == (1, 2)
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", result["synced_at"])
        assert "missing_on_pool" not in result
        assert await _states(db) == {"logs": ("unrouted", None), "photos": ("routed", DEFAULT)}
        rows = await db.execute_fetchall("SELECT DISTINCT discovered_at FROM bucket_sync")
        assert [r["discovered_at"] for r in rows] == [result["synced_at"]]

    async def test_bucket_follows_its_route(self, db, s3, caplog):
        await sync(1)
        assert await _states(db) == {"photos": ("unrouted", None)}

        await _route(db, "/photos/", DEFAULT)
        with caplog.at_level(logging.INFO, logger="nanio_orchestrator.bucket_sync"):
            await sync(1)
        assert await _states(db) == {"photos": ("routed", DEFAULT)}
        assert "bucket 'photos' status unrouted → routed" in caplog.text

        await db.execute("DELETE FROM routes")
        await db.commit()
        await sync(1)
        assert await _states(db) == {"photos": ("unrouted", None)}

    async def test_ignored_bucket_stays_ignored(self, db, s3):
        await sync(1)
        await db.execute("UPDATE bucket_sync SET status = 'ignored'")
        await db.commit()

        await sync(1)
        assert await _states(db) == {"photos": ("ignored", None)}

        s3.nodes[DEFAULT_NODE] = []  # not even its disappearance changes that
        await sync(1)
        assert await _states(db) == {"photos": ("ignored", None)}

    async def test_bucket_that_disappears_is_marked_deleted_and_comes_back_unrouted(self, db, s3):
        s3.nodes[DEFAULT_NODE] = ["photos", "logs"]
        await sync(1)

        s3.nodes[DEFAULT_NODE] = ["logs"]
        result = await sync(1)
        assert result["buckets_found"] == 1
        assert await _states(db) == {"logs": ("unrouted", None), "photos": ("deleted", None)}

        await sync(1)  # still gone: still deleted
        assert (await _states(db))["photos"] == ("deleted", None)

        s3.nodes[DEFAULT_NODE] = ["logs", "photos"]
        await sync(1)
        assert (await _states(db))["photos"] == ("unrouted", None)

    async def test_buckets_of_another_vhost_are_untouched(self, db, s3):
        await db.execute(
            "INSERT INTO vhosts (id, server_name, listen_port, ssl, default_pool_id) VALUES (2, 'b.example.com', 80, 0, 2)"
        )
        await db.execute("INSERT INTO bucket_sync (vhost_id, bucket, status) VALUES (2, 'theirs', 'unrouted')")
        await db.commit()
        await sync(1)
        assert await _states(db, 2) == {"theirs": ("unrouted", None)}


class TestBucketsRoutedToAnotherPool:
    async def test_bucket_living_elsewhere_is_created_empty_on_the_default_pool_and_listed(self, db, s3):
        s3.nodes[DEFAULT_NODE] = []
        s3.nodes[BIG_NODE] = ["videos"]
        await _route(db, "/videos/", BIG)

        result = await sync(1)

        assert s3.created == [(DEFAULT_NODE, "videos")]
        assert result["buckets_found"] == 1
        assert await _states(db) == {"videos": ("routed", BIG)}

    @pytest.mark.parametrize("pool_id", [WEB, IDLE, DEFAULT])
    async def test_nothing_is_created_for_http_pools_idle_pools_or_the_default_pool_itself(self, db, s3, pool_id):
        s3.nodes[DEFAULT_NODE] = []
        s3.nodes["10.0.0.3:80"] = ["videos"]
        s3.nodes["10.0.0.4:9000"] = ["videos"]
        await _route(db, "/videos/", pool_id)

        result = await sync(1)

        assert s3.created == []
        assert result["buckets_found"] == 0
        assert await _states(db) == {}

    async def test_unreachable_pool_is_logged_and_the_sync_goes_on(self, db, s3, caplog):
        s3.nodes[BIG_NODE] = TimeoutError("timed out")
        await _route(db, "/videos/", BIG)

        with caplog.at_level(logging.WARNING, logger="nanio_orchestrator.bucket_sync"):
            result = await sync(1)

        assert s3.created == []
        assert "could not list bucket 'videos' on the default pool: timed out" in caplog.text
        assert result["buckets_found"] == 1
        assert await _states(db) == {"photos": ("unrouted", None)}

    async def test_refused_creation_is_logged_and_the_bucket_is_not_listed(self, db, s3, caplog):
        s3.nodes[BIG_NODE] = ["videos"]
        s3.create_result = (False, "403 AccessDenied")
        await _route(db, "/videos/", BIG)

        with caplog.at_level(logging.WARNING, logger="nanio_orchestrator.bucket_sync"):
            result = await sync(1)

        assert s3.created == [(DEFAULT_NODE, "videos")]
        assert "could not list bucket 'videos' on the default pool: 403 AccessDenied" in caplog.text
        assert result["buckets_found"] == 1
        assert "videos" not in await _states(db)


class TestMissingFlag:
    async def test_routed_bucket_missing_on_its_pool_is_flagged_once(self, db, s3):
        await _route(db, "/photos/", BIG)  # listed by the default pool, absent from `big`
        first = await sync(1)

        assert first["missing_on_pool"] == [
            {"bucket": "photos", "target_member": BIG_NODE, "missing_since": first["synced_at"]}
        ]
        await db.execute("UPDATE bucket_sync SET missing_since = '2020-01-01T00:00:00Z'")
        await db.commit()
        second = await sync(1)
        assert second["missing_on_pool"][0]["missing_since"] == "2020-01-01T00:00:00Z"

    async def test_pool_without_an_enabled_member_cannot_be_asked_so_nothing_is_flagged(self, db, s3):
        await _route(db, "/photos/", IDLE)
        result = await sync(1)

        assert "missing_on_pool" not in result
        rows = await db.execute_fetchall("SELECT status, missing_since FROM bucket_sync")
        assert [dict(r) for r in rows] == [{"status": "routed", "missing_since": None}]

    async def test_flag_is_dropped_when_the_bucket_is_no_longer_routed(self, db, s3):
        await _route(db, "/photos/", BIG)
        await sync(1)
        await db.execute("DELETE FROM routes")
        await db.commit()

        result = await sync(1)

        assert "missing_on_pool" not in result
        rows = await db.execute_fetchall("SELECT status, missing_since FROM bucket_sync")
        assert [dict(r) for r in rows] == [{"status": "unrouted", "missing_since": None}]


class TestSubFolderMarkers:
    async def test_marker_goes_to_the_pool_that_serves_the_rest_of_the_bucket(self, db, s3):
        await _route(db, "/photos/", BIG)
        await _route(db, "/photos/2025/q1/", DEFAULT)  # parent: the /photos/ route
        await _route(db, "/logs/2024/", BIG)  # parent: the vhost's default pool
        await _route(db, "/videos/", BIG)  # a whole bucket needs no marker

        assert await subfolders.ensure_markers(1) == []
        assert sorted(s3.puts) == [
            (DEFAULT_NODE, "logs", "2024/.nanio-shard"),
            (BIG_NODE, "photos", "2025/q1/.nanio-shard"),
        ]

    async def test_no_marker_when_nobody_serves_the_rest_of_the_bucket(self, db, s3):
        await db.execute("UPDATE vhosts SET default_pool_id = NULL")
        await _route(db, "/logs/2024/", BIG)  # no /logs/ route, no default pool
        await _route(db, "/photos/", IDLE)
        await _route(db, "/photos/2025/", BIG)  # the parent pool has no enabled member to write to

        assert await subfolders.ensure_markers(1) == []
        assert s3.puts == []

    async def test_disabled_and_http_folder_routes_get_no_marker(self, db, s3):
        await _route(db, "/photos/2025/", WEB)
        await _route(db, "/logs/2024/", BIG)
        await db.execute("UPDATE routes SET enabled = 0 WHERE path_prefix = '/logs/2024/'")
        await db.commit()

        assert await subfolders.ensure_markers(1) == []
        assert s3.puts == []

    @pytest.mark.parametrize(
        ("put_result", "reason"),
        [(False, "the pool refused it"), (ConnectionError("connection refused"), "connection refused")],
    )
    async def test_marker_that_cannot_be_written_is_a_warning(self, db, s3, put_result, reason):
        s3.put_result = put_result
        await _route(db, "/photos/2025/", BIG)

        warnings = await subfolders.ensure_markers(1)

        assert len(warnings) == 1
        assert warnings[0].startswith(
            f"The marker '2025/.nanio-shard' could not be written to bucket 'photos' on pool 'default' ({reason})."
        )

    async def test_sync_rewrites_markers_and_only_logs_a_failure(self, db, s3, caplog):
        await _route(db, "/photos/2025/", BIG)
        s3.put_result = False

        with caplog.at_level(logging.WARNING, logger="nanio_orchestrator.bucket_sync"):
            result = await sync(1)

        assert s3.puts == [(DEFAULT_NODE, "photos", "2025/.nanio-shard")]
        assert result["buckets_found"] == 1
        assert "vhost 1: The marker '2025/.nanio-shard' could not be written" in caplog.text
        # The folder route does not make the bucket itself "routed"
        assert await _states(db) == {"photos": ("unrouted", None)}

    async def test_parent_pool_and_folder_key(self, db):
        await _route(db, "/photos/", BIG)
        assert subfolders.folder_key("/photos/2025/q1/") == "2025/q1/"
        assert await subfolders.parent_pool(db, 1, "/photos/2025/") == {"id": BIG, "name": "big", "type": "nanio"}
        assert await subfolders.parent_pool(db, 1, "/logs/2024/") == {"id": DEFAULT, "name": "default", "type": "nanio"}
        assert await subfolders.parent_pool(db, 99, "/logs/2024/") is None
        assert await subfolders.pool_endpoint(db, BIG) == BIG_NODE
        assert await subfolders.pool_endpoint(db, IDLE) is None


class TestHttpVhostsFollowSubFolders:
    @pytest_asyncio.fixture
    async def http_vhost(self, db):
        """`cdn` serves /photos/ from the files of the default pool, through the http pool `web`."""
        await db.execute("UPDATE pools SET source_nanio_pool_id = 1 WHERE id = 3")
        await db.execute("INSERT INTO vhosts (id, server_name, listen_port, ssl) VALUES (2, 'cdn.example.com', 80, 0)")
        await _route(db, "/photos/", WEB, vhost_id=2)
        return db

    async def _folder_routes(self, db) -> list:
        rows = await db.execute_fetchall(
            "SELECT vhost_id, pool_id FROM routes WHERE path_prefix = '/photos/2025/' ORDER BY vhost_id"
        )
        return [(r["vhost_id"], r["pool_id"]) for r in rows]

    async def test_nothing_to_do_when_no_http_vhost_serves_the_bucket(self, db):
        assert await subfolders.sync_http_routes(db, "photos", "/photos/2025/", DEFAULT, BIG) == ([], [])

    async def test_folder_route_is_added_on_the_http_pool_linked_to_the_folder_pool(self, http_vhost):
        db = http_vhost
        await db.execute("INSERT INTO pools (id, name, type, source_nanio_pool_id) VALUES (5, 'web-big', 'http', 2)")

        assert await subfolders.sync_http_routes(db, "photos", "/photos/2025/", DEFAULT, BIG) == ([2], [])
        assert await self._folder_routes(db) == [(2, 5)]

    async def test_existing_folder_route_is_repointed(self, http_vhost):
        db = http_vhost
        await db.execute("INSERT INTO pools (id, name, type, source_nanio_pool_id) VALUES (5, 'web-big', 'http', 2)")
        await _route(db, "/photos/2025/", WEB, vhost_id=2)

        assert await subfolders.sync_http_routes(db, "photos", "/photos/2025/", DEFAULT, BIG) == ([2], [])
        assert await self._folder_routes(db) == [(2, 5)]

    async def test_missing_http_pool_for_the_folder_pool_is_a_warning(self, http_vhost):
        changed, warnings = await subfolders.sync_http_routes(http_vhost, "photos", "/photos/2025/", DEFAULT, BIG)

        assert changed == []
        assert len(warnings) == 1
        assert "http vhost 'cdn.example.com' serves bucket 'photos'" in warnings[0]
        assert "'/photos/2025/' answers 404 there" in warnings[0]
        assert await self._folder_routes(http_vhost) == []

    async def test_removing_the_folder_removes_the_http_route_if_there_was_one(self, http_vhost):
        db = http_vhost
        assert await subfolders.sync_http_routes(db, "photos", "/photos/2025/", DEFAULT, None) == ([], [])

        await _route(db, "/photos/2025/", WEB, vhost_id=2)
        assert await subfolders.sync_http_routes(db, "photos", "/photos/2025/", DEFAULT, None) == ([2], [])
        assert await self._folder_routes(db) == []


class TestLoop:
    @pytest.fixture
    def loop_env(self, monkeypatch):
        """Patch the interval and count the sync rounds."""
        env = {"rounds": 0, "interval": 60, "on_round": lambda n: None}

        class Settings:
            @property
            def bucket_sync_interval(self):
                return env["interval"]

        async def sync_all():
            env["rounds"] += 1
            env["on_round"](env["rounds"])

        monkeypatch.setattr(bucket_sync, "get_settings", Settings)
        monkeypatch.setattr(bucket_sync, "sync_all_vhosts", sync_all)
        monkeypatch.setattr(bucket_sync, "_stop_event", None)  # as on a freshly started process
        return env

    async def test_syncs_at_once_and_stops_while_waiting_for_the_next_round(self, loop_env):
        first_round = asyncio.Event()
        loop_env["on_round"] = lambda n: first_round.set()

        task = asyncio.create_task(bucket_sync.bucket_sync_loop())
        await asyncio.wait_for(first_round.wait(), timeout=5)
        assert not task.done()  # waiting out the 60 s interval

        bucket_sync.stop_bucket_sync()
        await asyncio.wait_for(task, timeout=5)
        assert loop_env["rounds"] == 1

    async def test_runs_again_after_each_interval_and_survives_a_failing_round(self, loop_env, caplog):
        loop_env["interval"] = 0.001

        def on_round(n):
            if n == 1:
                raise RuntimeError("database is locked")
            if n == 3:
                bucket_sync.stop_bucket_sync()

        loop_env["on_round"] = on_round
        with caplog.at_level(logging.ERROR, logger="nanio_orchestrator.bucket_sync"):
            await asyncio.wait_for(bucket_sync.bucket_sync_loop(), timeout=5)

        assert loop_env["rounds"] == 3
        assert "Bucket sync error: database is locked" in caplog.text

    async def test_a_stop_before_the_loop_ever_ran_is_harmless(self, loop_env):
        bucket_sync.stop_bucket_sync()
        loop_env["on_round"] = lambda n: bucket_sync.stop_bucket_sync()
        await asyncio.wait_for(bucket_sync.bucket_sync_loop(), timeout=5)
        assert loop_env["rounds"] == 1
