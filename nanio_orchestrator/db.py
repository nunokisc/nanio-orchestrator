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
    type        TEXT NOT NULL DEFAULT 'nanio' CHECK (type IN ('nanio','http','cold')),
    lb_method   TEXT NOT NULL DEFAULT 'least_conn' CHECK (lb_method IN ('round_robin','least_conn','ip_hash')),
    keepalive   INTEGER NOT NULL DEFAULT 32,
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
    enabled             INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS routes (
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
    member_id    INTEGER NOT NULL REFERENCES pool_members(id),
    node_type    TEXT NOT NULL CHECK (node_type IN ('nanio-only','nginx-only','nginx-nanio')),
    config_json  TEXT NOT NULL,
    generated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


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
        await db.commit()


def init_db_sync() -> None:
    """Synchronous schema creation for CLI / install commands."""
    path = get_db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    conn.close()
