"""DB → disk (`config rebuild`) and disk → DB (`sync`, `rebuild-from-disk`) must round-trip."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def fernet(monkeypatch):
    from cryptography.fernet import Fernet

    import nanio_orchestrator.config as cfg_mod
    from nanio_orchestrator.credentials import reset_fernet

    monkeypatch.setenv("NANIO_ORCHESTRATOR_SECRET", Fernet.generate_key().decode())
    cfg_mod.settings = None
    reset_fernet()
    yield
    reset_fernet()


async def _cluster(client):
    """One nanio pool with credentials, one http pool linked to it, one vhost with an IP rule."""
    pool = (await client.post("/api/pools", json={"name": "hot", "type": "nanio"})).json()
    await client.post(f"/api/pools/{pool['id']}/members", json={"address": "10.0.0.1:9000"})
    resp = await client.put(
        f"/api/pools/{pool['id']}/credentials",
        json={"access_key": "AKIA1234", "secret_key": "secret", "region": "us-east-1"},
    )
    assert resp.status_code == 200, resp.text
    web = (
        await client.post("/api/pools", json={"name": "web", "type": "http", "source_nanio_pool_id": pool["id"]})
    ).json()
    await client.post(f"/api/pools/{web['id']}/members", json={"address": "10.0.0.5:80", "role": "primary"})
    vhost = await client.post(
        "/api/vhosts",
        json={
            "server_name": "s3.example.com",
            "listen_port": 80,
            "ssl": False,
            "default_pool_id": pool["id"],
            "ip_rule_mode": "allow",
            "ip_rule_ips": ["10.0.0.0/8"],
        },
    )
    assert vhost.status_code == 201, vhost.text
    return pool, web, vhost.json()


def _sidecars(tmp_dirs):
    return sorted(Path(tmp_dirs["nginx_dir"]).rglob("*.meta.json"))


class TestConfigRebuildRestoresSidecars:
    async def test_lost_sidecars_are_rewritten(self, client, mock_nginx, tmp_dirs, fernet):
        await _cluster(client)
        before = {p.name: json.loads(p.read_text()) for p in _sidecars(tmp_dirs)}
        assert set(before) == {"hot.meta.json", "web.meta.json", "s3.example.com.meta.json"}
        for p in _sidecars(tmp_dirs):
            p.unlink()

        resp = await client.post("/api/config/rebuild")
        assert resp.json()["ok"] is True

        after = {p.name: json.loads(p.read_text()) for p in _sidecars(tmp_dirs)}
        for name, old in before.items():
            old.pop("updated_at")
            after[name].pop("updated_at")
            assert after[name] == old, name
        assert after["hot.meta.json"]["credentials"]["access_key_enc"]

    async def test_rebuilt_files_are_enough_to_rebuild_the_database(
        self, client, mock_nginx, mock_s3, tmp_dirs, fernet
    ):
        await _cluster(client)
        for p in _sidecars(tmp_dirs):
            p.unlink()
        await client.post("/api/config/rebuild")

        resp = await client.post("/api/config/rebuild-from-disk?force=true")
        assert resp.status_code == 200, resp.text

        pools = {p["name"]: p for p in (await client.get("/api/pools")).json()}
        assert pools["web"]["type"] == "http"
        assert pools["web"]["source_nanio_pool_id"] == pools["hot"]["id"]
        assert (await client.get(f"/api/pools/{pools['hot']['id']}/credentials")).status_code == 200
        vhost = (await client.get("/api/vhosts")).json()[0]
        assert vhost["default_pool_id"] == pools["hot"]["id"]
        assert vhost["ip_rule_mode"] == "allow" and vhost["ip_rule_ips"] == ["10.0.0.0/8"]

    async def test_stale_credentials_leave_the_sidecar(self, client, mock_nginx, tmp_dirs, fernet):
        pool, _, _ = await _cluster(client)
        sidecar = Path(tmp_dirs["nginx_dir"]) / "pools" / "hot.meta.json"
        stale = sidecar.read_text()
        await client.delete(f"/api/pools/{pool['id']}/credentials")
        sidecar.write_text(stale)  # e.g. restored from an older backup

        await client.post("/api/config/rebuild")
        assert "credentials" not in json.loads(sidecar.read_text())


class TestSyncFromDisk:
    async def _wipe_db(self):
        from nanio_orchestrator.db import CLEAR_TABLES, get_db_ctx

        async with get_db_ctx() as db:
            for table in CLEAR_TABLES:
                await db.execute(f"DELETE FROM {table}")
            await db.commit()

    async def test_sync_imports_what_only_sidecars_know(self, client, mock_nginx, tmp_dirs, fernet):
        """A vhost found on disk keeps its IP rule; before, sync dropped it and the next
        regeneration of the vhost silently removed the allowlist from nginx."""
        await _cluster(client)
        await self._wipe_db()

        resp = await client.post("/api/config/sync")
        body = resp.json()
        assert body["pools_new"] == 2 and body["vhosts_new"] == 1 and body["warnings"] == []

        vhost = (await client.get("/api/vhosts")).json()[0]
        assert vhost["ip_rule_mode"] == "allow" and vhost["ip_rule_ips"] == ["10.0.0.0/8"]
        pools = {p["name"]: p for p in (await client.get("/api/pools")).json()}
        assert pools["web"]["source_nanio_pool_id"] == pools["hot"]["id"]

    async def test_sync_is_idempotent(self, client, mock_nginx, tmp_dirs, fernet):
        await _cluster(client)
        first = (await client.post("/api/config/sync")).json()
        assert first["pools_new"] == 0 and first["members_new"] == 0 and first["vhosts_new"] == 0
        assert first["pools_updated"] == 2 and first["vhosts_updated"] == 1
        assert len((await client.get("/api/pools")).json()) == 2

    async def test_missing_sidecar_does_not_change_pool_type(self, client, mock_nginx, tmp_dirs, fernet):
        """Without its sidecar an existing http pool used to be turned into a nanio pool."""
        await _cluster(client)
        (Path(tmp_dirs["nginx_dir"]) / "pools" / "web.meta.json").unlink()

        await client.post("/api/config/sync")
        pools = {p["name"]: p for p in (await client.get("/api/pools")).json()}
        assert pools["web"]["type"] == "http"


def test_init_db_sync_applies_every_schema_upgrade(tmp_path):
    """The CLI/installer path used to carry its own, incomplete copy of the upgrades."""
    import nanio_orchestrator.db as db_mod

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """CREATE TABLE pools (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, description TEXT,
               type TEXT NOT NULL DEFAULT 'nanio', lb_method TEXT NOT NULL DEFAULT 'least_conn',
               keepalive INTEGER NOT NULL DEFAULT 32, created_at TEXT, updated_at TEXT);
           CREATE TABLE vhosts (id INTEGER PRIMARY KEY, server_name TEXT NOT NULL UNIQUE,
               listen_port INTEGER NOT NULL DEFAULT 443, ssl INTEGER NOT NULL DEFAULT 1, ssl_cert_path TEXT,
               ssl_key_path TEXT, extra_directives TEXT, enabled INTEGER NOT NULL DEFAULT 1,
               created_at TEXT, updated_at TEXT);"""
    )
    conn.close()

    previous = db_mod._db_path
    db_mod.set_db_path(str(path))
    try:
        db_mod.init_db_sync()
    finally:
        db_mod._db_path = previous

    conn = sqlite3.connect(path)
    vhost_cols = {r[1] for r in conn.execute("PRAGMA table_info(vhosts)")}
    pool_cols = {r[1] for r in conn.execute("PRAGMA table_info(pools)")}
    migration_cols = {r[1] for r in conn.execute("PRAGMA table_info(migrations)")}
    conn.close()
    assert {"default_pool_id", "extra_blocks_json", "ip_rule_mode", "ip_rule_ips_json"} <= vhost_cols
    assert "source_nanio_pool_id" in pool_cols
    assert "split_routing" in migration_cols
