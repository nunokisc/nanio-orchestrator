"""Databases created by older versions are upgraded in place, and their rows survive."""

from __future__ import annotations

import sqlite3

import pytest
from nanio_orchestrator.db import SCHEMA_SQL, get_db_ctx, init_db, init_db_sync, set_db_path

# ── Schemas as older versions created them ───────────────────────────────────

POOLS_WITH_COLD = """
CREATE TABLE pools (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    type        TEXT NOT NULL DEFAULT 'nanio' CHECK (type IN ('nanio','http','cold')),
    lb_method   TEXT NOT NULL DEFAULT 'least_conn' CHECK (lb_method IN ('round_robin','least_conn','ip_hash')),
    keepalive   INTEGER NOT NULL DEFAULT 32,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

VHOSTS_FIRST = """
CREATE TABLE vhosts (
    id                  INTEGER PRIMARY KEY,
    server_name         TEXT NOT NULL UNIQUE,
    listen_port         INTEGER NOT NULL DEFAULT 443,
    ssl                 INTEGER NOT NULL DEFAULT 1,
    ssl_cert_path       TEXT,
    ssl_key_path        TEXT,
    extra_directives    TEXT,
    enabled             INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

ROUTES = """
CREATE TABLE routes (
    id              INTEGER PRIMARY KEY,
    vhost_id        INTEGER NOT NULL REFERENCES vhosts(id),
    path_prefix     TEXT NOT NULL,
    pool_id         INTEGER NOT NULL REFERENCES pools(id),
    extra_directives TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(vhost_id, path_prefix)
);
"""

NODE_CONFIGS = """
CREATE TABLE node_configs (
    id           INTEGER PRIMARY KEY,
    member_id    INTEGER NOT NULL,
    node_type    TEXT NOT NULL CHECK (node_type IN ('nanio-only','nginx-only','nginx-nanio')),
    config_json  TEXT NOT NULL,
    generated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

BUCKET_SYNC_WITHOUT_DELETED = """
CREATE TABLE bucket_sync (
    id              INTEGER PRIMARY KEY,
    vhost_id        INTEGER NOT NULL REFERENCES vhosts(id) ON DELETE CASCADE,
    bucket          TEXT NOT NULL,
    discovered_at   TEXT NOT NULL DEFAULT (datetime('now')),
    status          TEXT NOT NULL DEFAULT 'unrouted'
                    CHECK (status IN ('unrouted','routed','migrating','ignored')),
    routed_pool_id  INTEGER REFERENCES pools(id),
    UNIQUE(vhost_id, bucket)
);
"""

BUCKET_SYNC_WITHOUT_MISSING_SINCE = BUCKET_SYNC_WITHOUT_DELETED.replace("'ignored')", "'ignored','deleted')")

MIGRATION_LOG = """
CREATE TABLE migration_log (
    id              INTEGER PRIMARY KEY,
    migration_id    INTEGER NOT NULL REFERENCES migrations(id) ON DELETE CASCADE,
    phase           TEXT NOT NULL,
    message         TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_MIGRATION_COUNTERS = """
    rclone_pid      INTEGER,
    objects_total   INTEGER NOT NULL DEFAULT 0,
    objects_done    INTEGER NOT NULL DEFAULT 0,
    bytes_total     INTEGER NOT NULL DEFAULT 0,
    bytes_done      INTEGER NOT NULL DEFAULT 0,
    error_msg       TEXT,
    started_at      TEXT,
    finished_at     TEXT,
"""

# The first migrations table: no mode, no route_id, no orphaned_* columns
MIGRATIONS_FIRST = f"""
CREATE TABLE migrations (
    id              INTEGER PRIMARY KEY,
    vhost_id        INTEGER NOT NULL REFERENCES vhosts(id) ON DELETE CASCADE,
    bucket          TEXT NOT NULL,
    src_pool_id     INTEGER NOT NULL REFERENCES pools(id),
    dst_pool_id     INTEGER NOT NULL REFERENCES pools(id),
    phase           TEXT NOT NULL DEFAULT 'pending'
                    CHECK (phase IN ('pending','copying','verifying','switching','done','error','cancelled')),
    {_MIGRATION_COUNTERS}
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# When the source was purged automatically: purge phases, a 'sync' mode, route_id
MIGRATIONS_WITH_PURGE = f"""
CREATE TABLE migrations (
    id              INTEGER PRIMARY KEY,
    vhost_id        INTEGER NOT NULL REFERENCES vhosts(id) ON DELETE CASCADE,
    bucket          TEXT NOT NULL,
    src_pool_id     INTEGER NOT NULL REFERENCES pools(id),
    dst_pool_id     INTEGER NOT NULL REFERENCES pools(id),
    mode            TEXT NOT NULL DEFAULT 'copy'
                    CHECK (mode IN ('copy','sync')),
    phase           TEXT NOT NULL DEFAULT 'pending'
                    CHECK (phase IN ('pending','copying','write_routing','verifying','switching',
                                     'purge_source','needs_purge','done','error','cancelled')),
    {_MIGRATION_COUNTERS}
    route_id        INTEGER REFERENCES routes(id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# After orphan tracking replaced the purge, before the split was persisted
MIGRATIONS_WITHOUT_SPLIT = f"""
CREATE TABLE migrations (
    id                      INTEGER PRIMARY KEY,
    vhost_id                INTEGER NOT NULL REFERENCES vhosts(id) ON DELETE CASCADE,
    bucket                  TEXT NOT NULL,
    src_pool_id             INTEGER NOT NULL REFERENCES pools(id),
    dst_pool_id             INTEGER NOT NULL REFERENCES pools(id),
    mode                    TEXT NOT NULL DEFAULT 'copy'
                            CHECK (mode IN ('copy')),
    phase                   TEXT NOT NULL DEFAULT 'pending'
                            CHECK (phase IN
                                ('pending','copying','write_routing','verifying',
                                 'switching','done','error','cancelled')),
    {_MIGRATION_COUNTERS}
    route_id                INTEGER REFERENCES routes(id),
    orphaned_source_pool_id INTEGER REFERENCES pools(id),
    orphaned_source_prefix  TEXT,
    orphaned_at             TEXT,
    created_at              TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

SEED = """
INSERT INTO pools (id, name, type, keepalive) VALUES (1, 'hot', 'nanio', 16), (2, 'archive', 'cold', 8);
INSERT INTO vhosts (id, server_name, listen_port, ssl) VALUES (1, 's3.example.com', 8080, 0);
INSERT INTO routes (id, vhost_id, path_prefix, pool_id) VALUES (7, 1, '/photos/', 1);
"""


def _old_db(path: str, *statements: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("".join(statements))
    conn.commit()
    conn.close()


async def _upgrade(tmp_dirs) -> None:
    set_db_path(tmp_dirs["db_path"])
    await init_db()


async def _rows(sql: str) -> list[dict]:
    async with get_db_ctx() as db:
        return [dict(r) for r in await db.execute_fetchall(sql)]


async def _columns(table: str) -> list[str]:
    return [r["name"] for r in await _rows(f"PRAGMA table_info({table})")]


async def _table_sql(table: str) -> str:
    return (await _rows(f"SELECT sql FROM sqlite_master WHERE type='table' AND name='{table}'"))[0]["sql"]


def _fresh_columns(tmp_path) -> dict[str, set]:
    """Column names per table of a database created by the current version."""
    conn = sqlite3.connect(str(tmp_path / "fresh.db"))
    conn.executescript(SCHEMA_SQL)
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    columns = {t: {r[1] for r in conn.execute(f"PRAGMA table_info({t})")} for t in tables}
    conn.close()
    return columns


class TestFirstVersionDatabase:
    """Everything at once: the oldest schema of every table that was changed later."""

    @pytest.fixture
    def old_db(self, tmp_dirs):
        _old_db(
            tmp_dirs["db_path"],
            POOLS_WITH_COLD,
            VHOSTS_FIRST,
            ROUTES,
            NODE_CONFIGS,
            BUCKET_SYNC_WITHOUT_DELETED,
            MIGRATIONS_FIRST,
            MIGRATION_LOG,
            SEED,
            """
            INSERT INTO node_configs (member_id, node_type, config_json) VALUES (1, 'nanio-only', '{"secret":"x"}');
            INSERT INTO bucket_sync (id, vhost_id, bucket, discovered_at, status, routed_pool_id)
                 VALUES (3, 1, 'photos', '2025-01-02T03:04:05Z', 'routed', 1),
                        (4, 1, 'logs', '2025-01-03T00:00:00Z', 'ignored', NULL);
            INSERT INTO migrations (id, vhost_id, bucket, src_pool_id, dst_pool_id, phase, rclone_pid,
                                    objects_total, objects_done, bytes_total, bytes_done,
                                    error_msg, started_at, finished_at, created_at)
                 VALUES (11, 1, 'photos', 1, 2, 'error', 4321, 100, 40, 9000, 3600,
                         'rclone exited 1', '2025-02-01T10:00:00Z', '2025-02-01T11:00:00Z', '2025-02-01T09:00:00Z'),
                        (12, 1, 'videos', 2, 1, 'verifying', NULL, 5, 5, 50, 50,
                         NULL, '2025-03-01T10:00:00Z', NULL, '2025-03-01T09:00:00Z');
            INSERT INTO migration_log (migration_id, phase, message) VALUES (11, 'copying', 'started rclone');
            """,
        )
        return tmp_dirs

    async def test_schema_ends_up_identical_to_a_fresh_install(self, old_db, tmp_path):
        await _upgrade(old_db)
        fresh = _fresh_columns(tmp_path)
        for table, columns in fresh.items():
            assert set(await _columns(table)) == columns, table

    async def test_pools_keep_their_rows_and_cold_becomes_http(self, old_db):
        await _upgrade(old_db)
        pools = await _rows("SELECT id, name, type, keepalive, source_nanio_pool_id FROM pools ORDER BY id")
        assert pools == [
            {"id": 1, "name": "hot", "type": "nanio", "keepalive": 16, "source_nanio_pool_id": None},
            {"id": 2, "name": "archive", "type": "http", "keepalive": 8, "source_nanio_pool_id": None},
        ]

    async def test_vhost_gains_default_pool_extra_blocks_and_ip_rules(self, old_db):
        await _upgrade(old_db)
        vhosts = await _rows("SELECT * FROM vhosts")
        assert len(vhosts) == 1
        vhost = vhosts[0]
        assert (vhost["server_name"], vhost["listen_port"], vhost["ssl"]) == ("s3.example.com", 8080, 0)
        for column in ("default_pool_id", "extra_blocks_json", "ip_rule_mode", "ip_rule_ips_json"):
            assert vhost[column] is None

        async with get_db_ctx() as db:  # the new columns are usable
            await db.execute("UPDATE vhosts SET default_pool_id = 1, ip_rule_mode = 'allow' WHERE id = 1")
            await db.commit()
        assert (await _rows("SELECT default_pool_id FROM vhosts"))[0]["default_pool_id"] == 1

    async def test_bucket_sync_rows_survive_and_can_be_marked_deleted(self, old_db):
        await _upgrade(old_db)
        rows = await _rows("SELECT * FROM bucket_sync ORDER BY id")
        assert rows == [
            {
                "id": 3,
                "vhost_id": 1,
                "bucket": "photos",
                "discovered_at": "2025-01-02T03:04:05Z",
                "status": "routed",
                "routed_pool_id": 1,
                "missing_since": None,
            },
            {
                "id": 4,
                "vhost_id": 1,
                "bucket": "logs",
                "discovered_at": "2025-01-03T00:00:00Z",
                "status": "ignored",
                "routed_pool_id": None,
                "missing_since": None,
            },
        ]

        async with get_db_ctx() as db:
            await db.execute("UPDATE bucket_sync SET status = 'deleted', missing_since = 'x' WHERE id = 3")
            await db.commit()
            with pytest.raises(sqlite3.IntegrityError):  # the UNIQUE constraint was carried over
                await db.execute("INSERT INTO bucket_sync (vhost_id, bucket) VALUES (1, 'logs')")
        assert (await _rows("SELECT status FROM bucket_sync WHERE id = 3"))[0]["status"] == "deleted"

    async def test_migrations_keep_every_value_in_its_own_column(self, old_db):
        await _upgrade(old_db)
        rows = await _rows("SELECT * FROM migrations ORDER BY id")
        assert rows == [
            {
                "id": 11,
                "vhost_id": 1,
                "bucket": "photos",
                "src_pool_id": 1,
                "dst_pool_id": 2,
                "mode": "copy",
                "phase": "error",
                "rclone_pid": 4321,
                "objects_total": 100,
                "objects_done": 40,
                "bytes_total": 9000,
                "bytes_done": 3600,
                "error_msg": "rclone exited 1",
                "started_at": "2025-02-01T10:00:00Z",
                "finished_at": "2025-02-01T11:00:00Z",
                "route_id": None,
                "orphaned_source_pool_id": None,
                "orphaned_source_prefix": None,
                "orphaned_at": None,
                "created_at": "2025-02-01T09:00:00Z",
                "split_routing": 0,
                "folder": "",
            },
            {
                "id": 12,
                "vhost_id": 1,
                "bucket": "videos",
                "src_pool_id": 2,
                "dst_pool_id": 1,
                "mode": "copy",
                "phase": "verifying",
                "rclone_pid": None,
                "objects_total": 5,
                "objects_done": 5,
                "bytes_total": 50,
                "bytes_done": 50,
                "error_msg": None,
                "started_at": "2025-03-01T10:00:00Z",
                "finished_at": None,
                "route_id": None,
                "orphaned_source_pool_id": None,
                "orphaned_source_prefix": None,
                "orphaned_at": None,
                "created_at": "2025-03-01T09:00:00Z",
                "split_routing": 1,  # verifying implied the split before the column existed
                "folder": "",
            },
        ]

    async def test_migration_log_is_not_emptied_by_the_table_rebuild(self, old_db):
        await _upgrade(old_db)
        log = await _rows("SELECT migration_id, phase, message FROM migration_log")
        assert log == [{"migration_id": 11, "phase": "copying", "message": "started rclone"}]

    async def test_migration_phases_now_include_write_routing(self, old_db):
        await _upgrade(old_db)
        async with get_db_ctx() as db:
            await db.execute("UPDATE migrations SET phase = 'write_routing' WHERE id = 11")
            await db.commit()
        assert "purge_source" not in await _table_sql("migrations")

    async def test_stored_node_configs_are_dropped(self, old_db):
        """The table held S3 secret keys in plain text."""
        await _upgrade(old_db)
        assert await _rows("SELECT name FROM sqlite_master WHERE name = 'node_configs'") == []

    async def test_rebuilt_tables_leave_no_dangling_references(self, old_db):
        await _upgrade(old_db)
        async with get_db_ctx() as db:
            assert await db.execute_fetchall("PRAGMA foreign_key_check") == []
            # migration_log still points at the rebuilt migrations table
            with pytest.raises(sqlite3.IntegrityError):
                await db.execute("INSERT INTO migration_log (migration_id, phase, message) VALUES (999, 'x', 'y')")

    async def test_upgrading_twice_changes_nothing(self, old_db):
        await _upgrade(old_db)
        schema = await _rows("SELECT name, sql FROM sqlite_master ORDER BY name")
        migrations = await _rows("SELECT * FROM migrations ORDER BY id")
        buckets = await _rows("SELECT * FROM bucket_sync ORDER BY id")

        await init_db()

        assert await _rows("SELECT name, sql FROM sqlite_master ORDER BY name") == schema
        assert await _rows("SELECT * FROM migrations ORDER BY id") == migrations
        assert await _rows("SELECT * FROM bucket_sync ORDER BY id") == buckets


class TestPurgeEraMigrations:
    @pytest.fixture
    def old_db(self, tmp_dirs):
        _old_db(
            tmp_dirs["db_path"],
            POOLS_WITH_COLD,
            VHOSTS_FIRST,
            ROUTES,
            MIGRATIONS_WITH_PURGE,
            MIGRATION_LOG,
            SEED,
            """
            INSERT INTO migrations (id, vhost_id, bucket, src_pool_id, dst_pool_id, mode, phase, route_id, objects_done)
                 VALUES (1, 1, 'photos', 1, 2, 'sync', 'purge_source', 7, 10),
                        (2, 1, 'videos', 1, 2, 'copy', 'needs_purge', NULL, 20),
                        (3, 1, 'logs', 1, 2, 'copy', 'write_routing', NULL, 30),
                        (4, 1, 'tmp', 1, 2, 'copy', 'cancelled', NULL, 40);
            INSERT INTO migration_log (migration_id, phase, message) VALUES (1, 'purge_source', 'purging');
            """,
        )
        return tmp_dirs

    async def test_purge_phases_become_done_and_the_others_are_kept(self, old_db):
        await _upgrade(old_db)
        rows = await _rows("SELECT id, phase, objects_done FROM migrations ORDER BY id")
        assert rows == [
            {"id": 1, "phase": "done", "objects_done": 10},
            {"id": 2, "phase": "done", "objects_done": 20},
            {"id": 3, "phase": "write_routing", "objects_done": 30},
            {"id": 4, "phase": "cancelled", "objects_done": 40},
        ]

    async def test_sync_mode_becomes_copy(self, old_db):
        await _upgrade(old_db)
        assert {r["mode"] for r in await _rows("SELECT mode FROM migrations")} == {"copy"}
        async with get_db_ctx() as db:
            with pytest.raises(sqlite3.IntegrityError):
                await db.execute("UPDATE migrations SET mode = 'sync' WHERE id = 1")

    async def test_route_link_is_kept(self, old_db):
        await _upgrade(old_db)
        rows = await _rows("SELECT id, route_id FROM migrations ORDER BY id")
        assert [r["route_id"] for r in rows] == [7, None, None, None]

    async def test_only_the_phase_that_split_traffic_is_marked_split(self, old_db):
        await _upgrade(old_db)
        rows = await _rows("SELECT id FROM migrations WHERE split_routing = 1")
        assert [r["id"] for r in rows] == [3]

    async def test_log_of_a_purged_migration_survives(self, old_db):
        await _upgrade(old_db)
        assert [r["message"] for r in await _rows("SELECT message FROM migration_log")] == ["purging"]


class TestRecentDatabases:
    """Tables that only miss the latest columns are altered, not rebuilt."""

    SEED_MIGRATIONS = """
        INSERT INTO migrations (id, vhost_id, bucket, src_pool_id, dst_pool_id, phase, route_id,
                                orphaned_source_pool_id, orphaned_source_prefix, orphaned_at)
             VALUES (1, 1, 'photos', 1, 2, 'done', 7, 1, '/photos/', '2025-05-05T05:05:05Z'),
                    (2, 1, 'videos', 1, 2, 'write_routing', NULL, NULL, NULL, NULL),
                    (3, 1, 'logs', 1, 2, 'error', NULL, NULL, NULL, NULL);
    """

    async def test_orphan_tracking_survives_when_split_routing_is_added(self, tmp_dirs):
        _old_db(
            tmp_dirs["db_path"],
            POOLS_WITH_COLD,
            VHOSTS_FIRST,
            ROUTES,
            MIGRATIONS_WITHOUT_SPLIT,
            SEED,
            self.SEED_MIGRATIONS,
        )
        await _upgrade(tmp_dirs)

        rows = await _rows(
            """SELECT id, phase, route_id, orphaned_source_pool_id, orphaned_source_prefix, orphaned_at,
                      split_routing, folder FROM migrations ORDER BY id"""
        )
        assert rows[0] == {
            "id": 1,
            "phase": "done",
            "route_id": 7,
            "orphaned_source_pool_id": 1,
            "orphaned_source_prefix": "/photos/",
            "orphaned_at": "2025-05-05T05:05:05Z",
            "split_routing": 0,
            "folder": "",
        }
        assert [(r["id"], r["split_routing"]) for r in rows] == [(1, 0), (2, 1), (3, 0)]

    async def test_a_persisted_split_is_not_recomputed_when_folder_is_added(self, tmp_dirs):
        """A migration that failed after the split still serves through it: the flag must stay."""
        _old_db(
            tmp_dirs["db_path"],
            POOLS_WITH_COLD,
            VHOSTS_FIRST,
            ROUTES,
            MIGRATIONS_WITHOUT_SPLIT.replace(
                "created_at ", "split_routing INTEGER NOT NULL DEFAULT 0,\n    created_at "
            ),
            SEED,
            self.SEED_MIGRATIONS,
            "UPDATE migrations SET split_routing = 1 WHERE id = 3; UPDATE migrations SET phase = 'pending' WHERE id = 2;",
        )
        await _upgrade(tmp_dirs)

        rows = await _rows("SELECT id, split_routing, folder FROM migrations ORDER BY id")
        assert rows == [
            {"id": 1, "split_routing": 0, "folder": ""},
            {"id": 2, "split_routing": 0, "folder": ""},
            {"id": 3, "split_routing": 1, "folder": ""},
        ]

    async def test_bucket_sync_gains_missing_since_without_a_rebuild(self, tmp_dirs):
        _old_db(
            tmp_dirs["db_path"],
            POOLS_WITH_COLD,
            VHOSTS_FIRST,
            ROUTES,
            BUCKET_SYNC_WITHOUT_MISSING_SINCE,
            SEED,
            "INSERT INTO bucket_sync (id, vhost_id, bucket, discovered_at, status) VALUES (9, 1, 'gone', 'then', 'deleted');",
        )
        await _upgrade(tmp_dirs)

        assert await _rows("SELECT * FROM bucket_sync") == [
            {
                "id": 9,
                "vhost_id": 1,
                "bucket": "gone",
                "discovered_at": "then",
                "status": "deleted",
                "routed_pool_id": None,
                "missing_since": None,
            }
        ]

    async def test_current_database_is_left_alone(self, tmp_dirs):
        await _upgrade(tmp_dirs)
        async with get_db_ctx() as db:
            await db.execute("INSERT INTO pools (id, name) VALUES (1, 'a'), (2, 'b')")
            await db.execute("INSERT INTO vhosts (id, server_name) VALUES (1, 'v')")
            await db.execute(
                """INSERT INTO migrations (vhost_id, bucket, src_pool_id, dst_pool_id, phase, split_routing, folder)
                   VALUES (1, 'photos', 1, 2, 'error', 1, '2025/')"""
            )
            await db.commit()
        schema = await _rows("SELECT name, sql FROM sqlite_master ORDER BY name")

        await init_db()

        assert await _rows("SELECT name, sql FROM sqlite_master ORDER BY name") == schema
        rows = await _rows("SELECT phase, split_routing, folder FROM migrations")
        assert rows == [{"phase": "error", "split_routing": 1, "folder": "2025/"}]


def test_sync_entry_point_upgrades_an_old_database_and_creates_its_directory(tmp_path):
    """The CLI and the installer upgrade through init_db_sync()."""
    db_path = tmp_path / "data" / "nested" / "orchestrator.db"
    set_db_path(str(db_path))
    init_db_sync()  # creates the missing directories and a current schema
    assert db_path.exists()

    old_path = str(tmp_path / "old.db")
    _old_db(
        old_path,
        POOLS_WITH_COLD,
        VHOSTS_FIRST,
        ROUTES,
        MIGRATIONS_WITH_PURGE,
        SEED,
        "INSERT INTO migrations (id, vhost_id, bucket, src_pool_id, dst_pool_id, phase) "
        "VALUES (1, 1, 'photos', 1, 2, 'needs_purge');",
    )
    set_db_path(old_path)
    init_db_sync()

    conn = sqlite3.connect(old_path)
    assert conn.execute("SELECT phase, mode, split_routing, folder FROM migrations").fetchall() == [
        ("done", "copy", 0, "")
    ]
    assert conn.execute("SELECT type FROM pools WHERE name = 'archive'").fetchone() == ("http",)
    conn.close()
