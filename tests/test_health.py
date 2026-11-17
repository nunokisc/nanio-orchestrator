"""Tests for the health endpoint."""

from unittest.mock import patch

from nanio_orchestrator import __version__
from nanio_orchestrator.db import get_db_ctx


class TestHealth:
    async def test_health_ok(self, client):
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "version" in data
        assert data["db_ok"] is True

    async def test_health_no_auth_required(self, client):
        client.headers.pop("X-Orchestrator-Key", None)
        resp = await client.get("/api/health")
        assert resp.status_code == 200


class TestHealthDetails:
    async def test_reports_version_mode_and_config_dir(self, client, tmp_dirs):
        data = (await client.get("/api/health")).json()
        assert data == {
            "status": "ok",
            "version": __version__,
            "dev_mode": True,
            "db_ok": True,
            "nginx_config_dir": tmp_dirs["nginx_dir"],
            "drift_alerts": 0,
            "missing_buckets": 0,
        }

    async def test_counts_files_whose_disk_hash_differs_from_the_database(self, client):
        async with get_db_ctx() as db:
            for path, disk, db_hash in [
                ("/n/in-sync.conf", "aaa", "aaa"),
                ("/n/edited-1.conf", "bbb", "ccc"),
                ("/n/edited-2.conf", "ddd", "eee"),
                ("/n/not-checked-yet.conf", None, "fff"),
            ]:
                await db.execute(
                    "INSERT INTO config_files (path, sha256_disk, sha256_db) VALUES (?, ?, ?)", (path, disk, db_hash)
                )
            await db.commit()
        data = (await client.get("/api/health")).json()
        assert data["drift_alerts"] == 2
        assert data["status"] == "ok"

    async def test_counts_routed_buckets_missing_on_their_pool(self, client):
        async with get_db_ctx() as db:
            await db.execute("INSERT INTO pools (id, name, type) VALUES (1, 'alpha', 'nanio')")
            await db.execute("INSERT INTO vhosts (id, server_name, default_pool_id) VALUES (1, 's3.example.com', 1)")
            for bucket, missing in [
                ("here", None),
                ("gone-1", "2026-01-01 00:00:00"),
                ("gone-2", "2026-01-02 00:00:00"),
            ]:
                await db.execute(
                    "INSERT INTO bucket_sync (vhost_id, bucket, status, routed_pool_id, missing_since)"
                    " VALUES (1, ?, 'routed', 1, ?)",
                    (bucket, missing),
                )
            await db.commit()
        assert (await client.get("/api/health")).json()["missing_buckets"] == 2

    async def test_database_failure_is_reported_as_degraded_not_as_an_error(self, client):
        with patch("nanio_orchestrator.api.health.get_db_ctx", side_effect=OSError("unable to open database file")):
            resp = await client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert (data["status"], data["db_ok"]) == ("degraded", False)
        assert (data["drift_alerts"], data["missing_buckets"]) == (0, 0)
