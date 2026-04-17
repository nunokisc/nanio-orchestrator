"""Tests for the database layer."""

import pytest

from nanio_orchestrator.db import get_db_ctx, init_db, set_db_path


class TestDatabase:
    async def test_init_db_creates_tables(self, tmp_dirs):
        set_db_path(tmp_dirs["db_path"])
        await init_db()

        async with get_db_ctx() as db:
            tables = await db.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            table_names = {r["name"] for r in tables}

        expected = {
            "pools", "pool_members", "vhosts", "routes",
            "config_files", "audit_log", "node_configs",
            "bucket_sync", "object_migrations",
            "pool_credentials", "migrations", "migration_log",
        }
        assert expected.issubset(table_names)

    async def test_wal_mode(self, tmp_dirs):
        set_db_path(tmp_dirs["db_path"])
        await init_db()

        async with get_db_ctx() as db:
            rows = await db.execute_fetchall("PRAGMA journal_mode")
            assert rows[0][0] == "wal"

    async def test_foreign_keys_enabled(self, tmp_dirs):
        set_db_path(tmp_dirs["db_path"])
        await init_db()

        async with get_db_ctx() as db:
            rows = await db.execute_fetchall("PRAGMA foreign_keys")
            assert rows[0][0] == 1

    async def test_routes_has_key_prefix(self, tmp_dirs):
        set_db_path(tmp_dirs["db_path"])
        await init_db()

        async with get_db_ctx() as db:
            info = await db.execute_fetchall("PRAGMA table_info(routes)")
            col_names = {r["name"] for r in info}
            assert "key_prefix" in col_names

    async def test_pool_credentials_table(self, tmp_dirs):
        set_db_path(tmp_dirs["db_path"])
        await init_db()

        async with get_db_ctx() as db:
            info = await db.execute_fetchall("PRAGMA table_info(pool_credentials)")
            col_names = {r["name"] for r in info}
            assert "access_key_enc" in col_names
            assert "secret_key_enc" in col_names
            assert "endpoint_url" in col_names
            assert "region" in col_names
