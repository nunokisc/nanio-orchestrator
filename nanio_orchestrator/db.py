"""SQLite database helpers — connection, schema creation, migrations."""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

import aiosqlite

from nanio_orchestrator.config import get_settings

_db_path: Optional[str] = None

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS pools (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    type        TEXT NOT NULL DEFAULT 'nanio' CHECK (type IN ('nanio','http')),
    lb_method   TEXT NOT NULL DEFAULT 'least_conn' CHECK (lb_method IN ('round_robin','least_conn','ip_hash')),
    keepalive   INTEGER NOT NULL DEFAULT 32,
    source_nanio_pool_id INTEGER REFERENCES pools(id) ON DELETE SET NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pool_members (
    id             INTEGER PRIMARY KEY,
    pool_id        INTEGER NOT NULL REFERENCES pools(id),
    address        TEXT NOT NULL,
    role           TEXT NOT NULL DEFAULT 'active'
                   CHECK (role IN ('active', 'primary', 'replica')),
    weight         INTEGER NOT NULL DEFAULT 1,
    max_fails      INTEGER NOT NULL DEFAULT 3,
    fail_timeout_s INTEGER NOT NULL DEFAULT 30,
    enabled        INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS vhosts (
    id                  INTEGER PRIMARY KEY,
    server_name         TEXT NOT NULL UNIQUE,
    listen_port         INTEGER NOT NULL DEFAULT 443,
    ssl                 INTEGER NOT NULL DEFAULT 1,
    ssl_cert_path       TEXT,
    ssl_key_path        TEXT,
    extra_directives    TEXT,
    extra_blocks_json   TEXT,
    enabled             INTEGER NOT NULL DEFAULT 1,
    default_pool_id     INTEGER REFERENCES pools(id),
    ip_rule_mode        TEXT CHECK (ip_rule_mode IN ('allow', 'deny')),
    ip_rule_ips_json    TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS routes (
    id              INTEGER PRIMARY KEY,
    vhost_id        INTEGER NOT NULL REFERENCES vhosts(id),
    path_prefix     TEXT NOT NULL,
    pool_id         INTEGER NOT NULL REFERENCES pools(id),
    key_prefix      TEXT,
    extra_directives TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(vhost_id, path_prefix)
);

CREATE TABLE IF NOT EXISTS config_files (
    id              INTEGER PRIMARY KEY,
    path            TEXT NOT NULL UNIQUE,
    sha256_disk     TEXT,
    sha256_db       TEXT,
    content_snapshot TEXT,
    last_synced_at  TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id                  INTEGER PRIMARY KEY,
    actor               TEXT NOT NULL DEFAULT 'api',
    action              TEXT NOT NULL,
    entity_type         TEXT NOT NULL,
    entity_id           INTEGER,
    before_json         TEXT,
    after_json          TEXT,
    nginx_reload_ok     INTEGER,
    nginx_reload_output TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS node_configs (
    id           INTEGER PRIMARY KEY,
    member_id    INTEGER NOT NULL REFERENCES pool_members(id) ON DELETE CASCADE,
    node_type    TEXT NOT NULL CHECK (node_type IN ('nanio-only','nginx-only','nginx-nanio')),
    config_json  TEXT NOT NULL,
    generated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS bucket_sync (
    id              INTEGER PRIMARY KEY,
    vhost_id        INTEGER NOT NULL REFERENCES vhosts(id) ON DELETE CASCADE,
    bucket          TEXT NOT NULL,
    discovered_at   TEXT NOT NULL DEFAULT (datetime('now')),
    status          TEXT NOT NULL DEFAULT 'unrouted'
                    CHECK (status IN ('unrouted','routed','migrating','ignored','deleted')),
    routed_pool_id  INTEGER REFERENCES pools(id),
    UNIQUE(vhost_id, bucket)
);

CREATE TABLE IF NOT EXISTS pool_credentials (
    id              INTEGER PRIMARY KEY,
    pool_id         INTEGER NOT NULL UNIQUE REFERENCES pools(id) ON DELETE CASCADE,
    access_key_enc  TEXT NOT NULL,
    secret_key_enc  TEXT NOT NULL,
    endpoint_url    TEXT,
    region          TEXT NOT NULL DEFAULT 'us-east-1',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS migrations (
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
    rclone_pid              INTEGER,
    objects_total           INTEGER NOT NULL DEFAULT 0,
    objects_done            INTEGER NOT NULL DEFAULT 0,
    bytes_total             INTEGER NOT NULL DEFAULT 0,
    bytes_done              INTEGER NOT NULL DEFAULT 0,
    error_msg               TEXT,
    started_at              TEXT,
    finished_at             TEXT,
    route_id                INTEGER REFERENCES routes(id),
    orphaned_source_pool_id INTEGER REFERENCES pools(id),
    orphaned_source_prefix  TEXT,
    orphaned_at             TEXT,
    created_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS migration_log (
    id              INTEGER PRIMARY KEY,
    migration_id    INTEGER NOT NULL REFERENCES migrations(id) ON DELETE CASCADE,
    phase           TEXT NOT NULL,
    message         TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


# Tables to truncate when clearing all data (order respects FK constraints)
CLEAR_TABLES = [
    "migration_log",
    "migrations",
    "node_configs",
    "bucket_sync",
    "pool_credentials",
    "routes",
    "pool_members",
    "audit_log",
    "config_files",
    "vhosts",
    "pools",
]


def get_db_path() -> str:
    global _db_path
    if _db_path is None:
        _db_path = get_settings().db_path
    return _db_path


def set_db_path(path: str) -> None:
    global _db_path
    _db_path = path


async def get_db() -> aiosqlite.Connection:
    """Get an aiosqlite connection (caller must close)."""
    db = await aiosqlite.connect(get_db_path())
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


@asynccontextmanager
async def get_db_ctx() -> AsyncIterator[aiosqlite.Connection]:
    """Async context manager for a database connection."""
    db = await get_db()
    try:
        yield db
    finally:
        await db.close()


async def init_db() -> None:
    """Create all tables if they do not exist."""
    async with get_db_ctx() as db:
        await db.executescript(SCHEMA_SQL)
        await _run_migrations_async(db)
        await db.commit()


async def _run_migrations_async(db) -> None:
    """Add columns/indexes that may be missing in existing databases."""
    # vhosts.default_pool_id (added in bucket-sync feature)
    info = await db.execute_fetchall("PRAGMA table_info(vhosts)")
    col_names = {r["name"] for r in info}
    if "default_pool_id" not in col_names:
        await db.execute("ALTER TABLE vhosts ADD COLUMN default_pool_id INTEGER REFERENCES pools(id)")
    # routes.key_prefix (added for sub-folder routing)
    info = await db.execute_fetchall("PRAGMA table_info(routes)")
    col_names = {r["name"] for r in info}
    if "key_prefix" not in col_names:
        await db.execute("ALTER TABLE routes ADD COLUMN key_prefix TEXT")

    # vhosts.extra_blocks_json (structured extra nginx blocks per zone)
    info = await db.execute_fetchall("PRAGMA table_info(vhosts)")
    col_names = {r["name"] for r in info}
    if "extra_blocks_json" not in col_names:
        await db.execute("ALTER TABLE vhosts ADD COLUMN extra_blocks_json TEXT")

    # vhosts.ip_rule_mode + ip_rule_ips_json (per-vhost IP access control)
    info = await db.execute_fetchall("PRAGMA table_info(vhosts)")
    col_names = {r["name"] for r in info}
    if "ip_rule_mode" not in col_names:
        await db.execute("ALTER TABLE vhosts ADD COLUMN ip_rule_mode TEXT")
        await db.execute("ALTER TABLE vhosts ADD COLUMN ip_rule_ips_json TEXT")

    # source_nanio_pool_id on pools (backing nanio pool link for http pools)
    info = await db.execute_fetchall("PRAGMA table_info(pools)")
    col_names = {r["name"] for r in info}
    if "source_nanio_pool_id" not in col_names:
        await db.execute(
            "ALTER TABLE pools ADD COLUMN source_nanio_pool_id INTEGER REFERENCES pools(id) ON DELETE SET NULL"
        )

    # bucket_sync: add 'deleted' status — requires table rebuild if old CHECK doesn't include it
    row = await db.execute_fetchall("SELECT sql FROM sqlite_master WHERE type='table' AND name='bucket_sync'")
    bucket_sync_sql = row[0]["sql"] if row else ""
    if "'deleted'" not in bucket_sync_sql:
        await db.execute("PRAGMA foreign_keys=OFF")
        await db.execute("""
            CREATE TABLE bucket_sync_new (
                id              INTEGER PRIMARY KEY,
                vhost_id        INTEGER NOT NULL REFERENCES vhosts(id) ON DELETE CASCADE,
                bucket          TEXT NOT NULL,
                discovered_at   TEXT NOT NULL DEFAULT (datetime('now')),
                status          TEXT NOT NULL DEFAULT 'unrouted'
                                CHECK (status IN ('unrouted','routed','migrating','ignored','deleted')),
                routed_pool_id  INTEGER REFERENCES pools(id),
                UNIQUE(vhost_id, bucket)
            )
        """)
        await db.execute("""
            INSERT INTO bucket_sync_new
            SELECT id, vhost_id, bucket, discovered_at, status, routed_pool_id
            FROM bucket_sync
        """)
        await db.execute("DROP TABLE bucket_sync")
        await db.execute("ALTER TABLE bucket_sync_new RENAME TO bucket_sync")
        await db.execute("PRAGMA foreign_keys=ON")

    # Migration: rename pool type 'cold' → 'http' (cold was an alias with no functional difference)
    await db.execute("UPDATE pools SET type = 'http' WHERE type = 'cold'")

    # Rebuild migrations table when needed:
    # - Remove purge_source / needs_purge phases (purge was removed intentionally — data is never
    #   deleted automatically; orphaned source data is tracked instead)
    # - Add mode, route_id, orphaned_* columns
    # - Any existing purge_source/needs_purge records are converted to 'done' (switching already
    #   completed, data is safe on dst; source data is now orphaned and tracked)
    info = await db.execute_fetchall("PRAGMA table_info(migrations)")
    col_names = {r["name"] for r in info}
    row = await db.execute_fetchall("SELECT sql FROM sqlite_master WHERE type='table' AND name='migrations'")
    migrations_sql = row[0]["sql"] if row else ""
    needs_rebuild = (
        "purge_source" in migrations_sql
        or "needs_purge" in migrations_sql
        or "orphaned_source_pool_id" not in col_names
        or "route_id" not in col_names
        or "mode" not in col_names
    )
    if needs_rebuild:
        await db.execute("PRAGMA foreign_keys=OFF")
        await db.execute("""CREATE TABLE migrations_new (
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
            rclone_pid              INTEGER,
            objects_total           INTEGER NOT NULL DEFAULT 0,
            objects_done            INTEGER NOT NULL DEFAULT 0,
            bytes_total             INTEGER NOT NULL DEFAULT 0,
            bytes_done              INTEGER NOT NULL DEFAULT 0,
            error_msg               TEXT,
            started_at              TEXT,
            finished_at             TEXT,
            route_id                INTEGER REFERENCES routes(id),
            orphaned_source_pool_id INTEGER REFERENCES pools(id),
            orphaned_source_prefix  TEXT,
            orphaned_at             TEXT,
            created_at              TEXT NOT NULL DEFAULT (datetime('now'))
        )""")
        # Migrate existing data; remap purge phases to 'done' (data is safe on dst)
        existing_cols = {r["name"] for r in await db.execute_fetchall("PRAGMA table_info(migrations)")}
        src_cols = [
            "id",
            "vhost_id",
            "bucket",
            "src_pool_id",
            "dst_pool_id",
            "rclone_pid",
            "objects_total",
            "objects_done",
            "bytes_total",
            "bytes_done",
            "error_msg",
            "started_at",
            "finished_at",
            "created_at",
        ]
        optional_cols = {
            "mode": "'copy'",
            "route_id": "NULL",
        }
        select_parts = []
        for c in src_cols:
            select_parts.append(c if c in existing_cols else f"NULL AS {c}")
        for c, default in optional_cols.items():
            select_parts.append(c if c in existing_cols else f"{default} AS {c}")
        # Phase remapping: purge_source/needs_purge → done
        phase_expr = "CASE WHEN phase IN ('purge_source','needs_purge') THEN 'done' ELSE phase END"
        select_parts.append(f"{phase_expr} AS phase")
        # orphaned_* always NULL for pre-existing records (unknown)
        select_parts.extend(
            ["NULL AS orphaned_source_pool_id", "NULL AS orphaned_source_prefix", "NULL AS orphaned_at"]
        )
        await db.execute(f"INSERT INTO migrations_new SELECT {', '.join(select_parts)} FROM migrations")
        await db.execute("DROP TABLE migrations")
        await db.execute("ALTER TABLE migrations_new RENAME TO migrations")
        await db.commit()
        await db.execute("PRAGMA foreign_keys=ON")

    # node_configs: ensure ON DELETE CASCADE on member_id FK
    row = await db.execute_fetchall("SELECT sql FROM sqlite_master WHERE type='table' AND name='node_configs'")
    if row and "ON DELETE CASCADE" not in (row[0]["sql"] or ""):
        await db.execute("PRAGMA foreign_keys=OFF")
        await db.execute("""CREATE TABLE node_configs_new (
            id           INTEGER PRIMARY KEY,
            member_id    INTEGER NOT NULL REFERENCES pool_members(id) ON DELETE CASCADE,
            node_type    TEXT NOT NULL CHECK (node_type IN ('nanio-only','nginx-only','nginx-nanio')),
            config_json  TEXT NOT NULL,
            generated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""")
        await db.execute("INSERT INTO node_configs_new SELECT * FROM node_configs")
        await db.execute("DROP TABLE node_configs")
        await db.execute("ALTER TABLE node_configs_new RENAME TO node_configs")
        await db.commit()
        await db.execute("PRAGMA foreign_keys=ON")


def init_db_sync() -> None:
    """Synchronous schema creation for CLI / install commands."""
    path = get_db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    # Migration: vhosts.default_pool_id
    info = conn.execute("PRAGMA table_info(vhosts)").fetchall()
    col_names = {r[1] for r in info}
    if "default_pool_id" not in col_names:
        conn.execute("ALTER TABLE vhosts ADD COLUMN default_pool_id INTEGER REFERENCES pools(id)")
    # Migration: routes.key_prefix
    info = conn.execute("PRAGMA table_info(routes)").fetchall()
    col_names = {r[1] for r in info}
    if "key_prefix" not in col_names:
        conn.execute("ALTER TABLE routes ADD COLUMN key_prefix TEXT")

    # Migration: vhosts.ip_rule_mode + ip_rule_ips_json (per-vhost IP access control)
    info = conn.execute("PRAGMA table_info(vhosts)").fetchall()
    col_names = {r[1] for r in info}
    if "ip_rule_mode" not in col_names:
        conn.execute("ALTER TABLE vhosts ADD COLUMN ip_rule_mode TEXT")
        conn.execute("ALTER TABLE vhosts ADD COLUMN ip_rule_ips_json TEXT")

    # Migration: rename pool type 'cold' → 'http' (cold was an alias with no functional difference)
    conn.execute("UPDATE pools SET type = 'http' WHERE type = 'cold'")

    # Rebuild migrations table when needed:
    # - Remove purge_source / needs_purge phases (purge was removed intentionally — data is never
    #   deleted automatically; orphaned source data is tracked instead)
    # - Add mode, route_id, orphaned_* columns
    # - Any existing purge_source/needs_purge records are converted to 'done'
    info = conn.execute("PRAGMA table_info(migrations)").fetchall()
    col_names_mig = {r[1] for r in info}
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='migrations'").fetchone()
    migrations_sql = row[0] if row else ""
    needs_rebuild = (
        "purge_source" in migrations_sql
        or "needs_purge" in migrations_sql
        or "orphaned_source_pool_id" not in col_names_mig
        or "route_id" not in col_names_mig
        or "mode" not in col_names_mig
    )
    if needs_rebuild:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("""CREATE TABLE migrations_new (
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
            rclone_pid              INTEGER,
            objects_total           INTEGER NOT NULL DEFAULT 0,
            objects_done            INTEGER NOT NULL DEFAULT 0,
            bytes_total             INTEGER NOT NULL DEFAULT 0,
            bytes_done              INTEGER NOT NULL DEFAULT 0,
            error_msg               TEXT,
            started_at              TEXT,
            finished_at             TEXT,
            route_id                INTEGER REFERENCES routes(id),
            orphaned_source_pool_id INTEGER REFERENCES pools(id),
            orphaned_source_prefix  TEXT,
            orphaned_at             TEXT,
            created_at              TEXT NOT NULL DEFAULT (datetime('now'))
        )""")
        src_cols = [
            "id",
            "vhost_id",
            "bucket",
            "src_pool_id",
            "dst_pool_id",
            "rclone_pid",
            "objects_total",
            "objects_done",
            "bytes_total",
            "bytes_done",
            "error_msg",
            "started_at",
            "finished_at",
            "created_at",
        ]
        optional_cols = {"mode": "'copy'", "route_id": "NULL"}
        select_parts = []
        for c in src_cols:
            select_parts.append(c if c in col_names_mig else f"NULL AS {c}")
        for c, default in optional_cols.items():
            select_parts.append(c if c in col_names_mig else f"{default} AS {c}")
        phase_expr = "CASE WHEN phase IN ('purge_source','needs_purge') THEN 'done' ELSE phase END"
        select_parts.append(f"{phase_expr} AS phase")
        select_parts.extend(
            ["NULL AS orphaned_source_pool_id", "NULL AS orphaned_source_prefix", "NULL AS orphaned_at"]
        )
        conn.execute(f"INSERT INTO migrations_new SELECT {', '.join(select_parts)} FROM migrations")
        conn.execute("DROP TABLE migrations")
        conn.execute("ALTER TABLE migrations_new RENAME TO migrations")
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")

    # node_configs: ensure ON DELETE CASCADE on member_id FK
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='node_configs'").fetchone()
    if row and "ON DELETE CASCADE" not in (row[0] or ""):
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("""CREATE TABLE node_configs_new (
            id           INTEGER PRIMARY KEY,
            member_id    INTEGER NOT NULL REFERENCES pool_members(id) ON DELETE CASCADE,
            node_type    TEXT NOT NULL CHECK (node_type IN ('nanio-only','nginx-only','nginx-nanio')),
            config_json  TEXT NOT NULL,
            generated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""")
        conn.execute("INSERT INTO node_configs_new SELECT * FROM node_configs")
        conn.execute("DROP TABLE node_configs")
        conn.execute("ALTER TABLE node_configs_new RENAME TO node_configs")
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")
    conn.commit()
    conn.close()
