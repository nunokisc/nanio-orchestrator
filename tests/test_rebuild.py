"""Tests for database resilience: backup, sidecar files, and rebuild."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import create_member, create_pool, create_vhost


class TestDatabaseBackup:
    """Tests for automatic database backup."""

    @pytest.mark.asyncio
    async def test_backup_creates_file(self, tmp_dirs):
        """Backup creates a .bak file."""
        import nanio_orchestrator.config as cfg_mod
        cfg_mod.settings = None

        os.environ["NANIO_ORCHESTRATOR_DB_PATH"] = tmp_dirs["db_path"]

        from nanio_orchestrator.db import init_db, set_db_path
        set_db_path(tmp_dirs["db_path"])
        await init_db()

        from nanio_orchestrator.backup import backup_database
        result = await backup_database()
        assert result is not None
        assert os.path.exists(result)

        cfg_mod.settings = None

    @pytest.mark.asyncio
    async def test_backup_rotation(self, tmp_dirs):
        """Multiple backups rotate files correctly."""
        import nanio_orchestrator.config as cfg_mod
        cfg_mod.settings = None

        os.environ["NANIO_ORCHESTRATOR_DB_PATH"] = tmp_dirs["db_path"]
        os.environ["NANIO_ORCHESTRATOR_DB_BACKUP_ROTATE"] = "3"

        from nanio_orchestrator.db import init_db, set_db_path
        set_db_path(tmp_dirs["db_path"])
        await init_db()

        from nanio_orchestrator.backup import backup_database
        bak_path = await backup_database()
        assert os.path.exists(bak_path)

        # Second backup should rotate the first to .bak.2
        await backup_database()
        assert os.path.exists(bak_path)
        assert os.path.exists(bak_path + ".2")

        # Third backup should have .bak, .bak.2, .bak.3
        await backup_database()
        assert os.path.exists(bak_path)
        assert os.path.exists(bak_path + ".2")
        assert os.path.exists(bak_path + ".3")

        cfg_mod.settings = None

    @pytest.mark.asyncio
    async def test_backup_triggered_after_write(self, client, app, mock_nginx):
        """DB backup is no longer triggered automatically on each CRUD write.
        Backups run periodically via the background task and after migrations."""
        pool = await create_pool(client)
        await create_member(client, pool["id"], "10.0.0.1:9000")
        # trigger_backup is NOT called on CRUD writes anymore
        assert not mock_nginx["trigger_backup"].called


class TestSidecarFiles:
    """Tests for sidecar .meta.json file creation and deletion."""

    @pytest.mark.asyncio
    async def test_pool_sidecar_written_on_create(self, client, app, mock_nginx, tmp_dirs):
        """Pool creation writes a .meta.json sidecar."""
        pool = await create_pool(client, name="my-pool")
        sidecar_path = os.path.join(tmp_dirs["nginx_dir"], "pools", "my-pool.meta.json")
        assert os.path.exists(sidecar_path)
        data = json.loads(Path(sidecar_path).read_text())
        assert data["name"] == "my-pool"
        assert data["type"] == "nanio"
        assert "updated_at" in data

    @pytest.mark.asyncio
    async def test_pool_sidecar_deleted_on_delete(self, client, app, mock_nginx, tmp_dirs):
        """Pool deletion removes the .meta.json sidecar."""
        pool = await create_pool(client, name="del-pool")
        sidecar_path = os.path.join(tmp_dirs["nginx_dir"], "pools", "del-pool.meta.json")
        assert os.path.exists(sidecar_path)

        resp = await client.delete(f"/api/pools/{pool['id']}")
        assert resp.status_code == 204
        assert not os.path.exists(sidecar_path)

    @pytest.mark.asyncio
    async def test_vhost_sidecar_written_on_create(self, client, app, mock_nginx, tmp_dirs):
        """Vhost creation writes a .meta.json sidecar."""
        vhost = await create_vhost(client, server_name="s3.example.com")
        sidecar_path = os.path.join(tmp_dirs["nginx_dir"], "vhosts", "s3.example.com.meta.json")
        assert os.path.exists(sidecar_path)
        data = json.loads(Path(sidecar_path).read_text())
        assert data["server_name"] == "s3.example.com"
        assert data["vhost_id"] == vhost["id"]

    @pytest.mark.asyncio
    async def test_vhost_sidecar_deleted_on_delete(self, client, app, mock_nginx, tmp_dirs):
        """Vhost deletion removes the .meta.json sidecar."""
        vhost = await create_vhost(client, server_name="del.example.com")
        sidecar_path = os.path.join(tmp_dirs["nginx_dir"], "vhosts", "del.example.com.meta.json")
        assert os.path.exists(sidecar_path)

        resp = await client.delete(f"/api/vhosts/{vhost['id']}")
        assert resp.status_code == 204
        assert not os.path.exists(sidecar_path)

    @pytest.mark.asyncio
    async def test_pool_sidecar_atomic_write(self, tmp_dirs):
        """Sidecar writes are atomic (no .tmp files left behind)."""
        import nanio_orchestrator.config as cfg_mod
        cfg_mod.settings = None
        os.environ["NANIO_ORCHESTRATOR_NGINX_CONFIG_DIR"] = tmp_dirs["nginx_dir"]

        from nanio_orchestrator.sidecar import write_pool_sidecar
        write_pool_sidecar(1, "test-atomic", "nanio", "test desc")

        pool_dir = os.path.join(tmp_dirs["nginx_dir"], "pools")
        files = os.listdir(pool_dir)
        assert "test-atomic.meta.json" in files
        assert "test-atomic.meta.json.tmp" not in files
        cfg_mod.settings = None

    @pytest.mark.asyncio
    async def test_credential_sidecar_written_on_set(self, client, app, mock_nginx, tmp_dirs):
        """Setting credentials updates the pool sidecar with encrypted creds."""
        os.environ["NANIO_ORCHESTRATOR_SECRET"] = "ASsfJ7RCxJiOrjeD9KX0LPlMe-EJtA2lKIh2yk-D6U0="

        import nanio_orchestrator.config as cfg_mod
        cfg_mod.settings = None  # force re-read of env vars
        from nanio_orchestrator.credentials import reset_fernet
        reset_fernet()

        pool = await create_pool(client, name="cred-pool")
        cred_body = {
            "access_key": "AKIA1234",
            "secret_key": "supersecret",
            "region": "us-east-1",
        }
        resp = await client.put(f"/api/pools/{pool['id']}/credentials", json=cred_body)
        assert resp.status_code == 200

        sidecar_path = os.path.join(tmp_dirs["nginx_dir"], "pools", "cred-pool.meta.json")
        data = json.loads(Path(sidecar_path).read_text())
        assert "credentials" in data
        assert data["credentials"]["access_key_enc"] != "AKIA1234"  # encrypted
        assert data["credentials"]["region"] == "us-east-1"

        reset_fernet()

    @pytest.mark.asyncio
    async def test_credential_sidecar_removed_on_delete(self, client, app, mock_nginx, tmp_dirs):
        """Deleting credentials removes cred section from sidecar."""
        os.environ["NANIO_ORCHESTRATOR_SECRET"] = "ASsfJ7RCxJiOrjeD9KX0LPlMe-EJtA2lKIh2yk-D6U0="

        import nanio_orchestrator.config as cfg_mod
        cfg_mod.settings = None
        from nanio_orchestrator.credentials import reset_fernet
        reset_fernet()

        pool = await create_pool(client, name="cred-del-pool")
        cred_body = {"access_key": "AKIA1234", "secret_key": "supersecret"}
        await client.put(f"/api/pools/{pool['id']}/credentials", json=cred_body)

        resp = await client.delete(f"/api/pools/{pool['id']}/credentials")
        assert resp.status_code == 200

        sidecar_path = os.path.join(tmp_dirs["nginx_dir"], "pools", "cred-del-pool.meta.json")
        data = json.loads(Path(sidecar_path).read_text())
        assert "credentials" not in data

        reset_fernet()


class TestRebuildFromDisk:
    """Tests for full database rebuild from nginx configs + sidecars."""

    @pytest.fixture
    def setup_nginx_configs(self, tmp_dirs):
        """Create nginx config files and sidecars for rebuild testing."""
        nginx_dir = tmp_dirs["nginx_dir"]
        pools_dir = os.path.join(nginx_dir, "pools")
        vhosts_dir = os.path.join(nginx_dir, "vhosts")
        migrations_dir = os.path.join(nginx_dir, "migrations")
        os.makedirs(migrations_dir, exist_ok=True)

        # Write a pool config
        pool_conf = """# managed by nanio-orchestrator
# pool_id:1 name:pool-default type:nanio updated:2026-04-16T10:00:00Z
upstream pool-default {
    least_conn;
    server 10.0.0.1:9000 weight=1 max_fails=3 fail_timeout=30s;
    server 10.0.0.2:9000 weight=1 max_fails=3 fail_timeout=30s;
    keepalive 32;
}
"""
        Path(os.path.join(pools_dir, "pool-default.conf")).write_text(pool_conf)

        # Write pool sidecar
        pool_sidecar = {
            "pool_id": 1,
            "name": "pool-default",
            "type": "nanio",
            "description": "Default storage pool",
            "updated_at": "2026-04-16T10:00:00Z",
        }
        Path(os.path.join(pools_dir, "pool-default.meta.json")).write_text(
            json.dumps(pool_sidecar)
        )

        # Write a vhost config
        vhost_conf = """# managed by nanio-orchestrator
# vhost_id:1 name:s3.example.com updated:2026-04-16T10:00:00Z
server {
    listen 443 ssl http2;
    server_name s3.example.com;

    ssl_certificate     /etc/ssl/s3.pem;
    ssl_certificate_key /etc/ssl/s3.key;

    client_max_body_size 0;

    # route_id:1 prefix:/assets/
    location /assets/ {
        proxy_pass         http://pool-default;
        proxy_http_version 1.1;
    }
}
"""
        Path(os.path.join(vhosts_dir, "s3.example.com.conf")).write_text(vhost_conf)

        # Write vhost sidecar
        vhost_sidecar = {
            "vhost_id": 1,
            "server_name": "s3.example.com",
            "default_pool_id": 1,
            "default_pool_name": "pool-default",
            "updated_at": "2026-04-16T10:00:00Z",
        }
        Path(os.path.join(vhosts_dir, "s3.example.com.meta.json")).write_text(
            json.dumps(vhost_sidecar)
        )

        return {"pools_dir": pools_dir, "vhosts_dir": vhosts_dir, "migrations_dir": migrations_dir}

    @pytest.mark.asyncio
    async def test_rebuild_recovers_pools(self, tmp_dirs, setup_nginx_configs):
        """Rebuild recovers pool from nginx config."""
        import nanio_orchestrator.config as cfg_mod
        import nanio_orchestrator.db as db_mod
        cfg_mod.settings = None
        db_mod._db_path = None

        os.environ["NANIO_ORCHESTRATOR_DB_PATH"] = tmp_dirs["db_path"]
        os.environ["NANIO_ORCHESTRATOR_NGINX_CONFIG_DIR"] = tmp_dirs["nginx_dir"]

        from nanio_orchestrator.db import set_db_path
        set_db_path(tmp_dirs["db_path"])

        # Delete existing DB to simulate loss
        if os.path.exists(tmp_dirs["db_path"]):
            os.unlink(tmp_dirs["db_path"])

        with patch("nanio_orchestrator.s3client.list_buckets", new_callable=AsyncMock, return_value=[]):
            from nanio_orchestrator.rebuild import rebuild_from_disk
            result = await rebuild_from_disk()

        assert result["pools_imported"] == 1
        assert result["members_imported"] == 2

        # Verify in DB
        from nanio_orchestrator.db import get_db_ctx
        async with get_db_ctx() as db:
            pools = await db.execute_fetchall("SELECT * FROM pools")
            assert len(pools) == 1
            assert pools[0]["name"] == "pool-default"
            assert pools[0]["type"] == "nanio"
            assert pools[0]["description"] == "Default storage pool"

        cfg_mod.settings = None
        db_mod._db_path = None

    @pytest.mark.asyncio
    async def test_rebuild_recovers_vhosts_and_routes(self, tmp_dirs, setup_nginx_configs):
        """Rebuild recovers vhosts and routes."""
        import nanio_orchestrator.config as cfg_mod
        import nanio_orchestrator.db as db_mod
        cfg_mod.settings = None
        db_mod._db_path = None

        os.environ["NANIO_ORCHESTRATOR_DB_PATH"] = tmp_dirs["db_path"]
        os.environ["NANIO_ORCHESTRATOR_NGINX_CONFIG_DIR"] = tmp_dirs["nginx_dir"]

        from nanio_orchestrator.db import set_db_path
        set_db_path(tmp_dirs["db_path"])

        if os.path.exists(tmp_dirs["db_path"]):
            os.unlink(tmp_dirs["db_path"])

        with patch("nanio_orchestrator.s3client.list_buckets", new_callable=AsyncMock, return_value=[]):
            from nanio_orchestrator.rebuild import rebuild_from_disk
            result = await rebuild_from_disk()

        assert result["vhosts_imported"] == 1
        assert result["routes_imported"] == 1

        from nanio_orchestrator.db import get_db_ctx
        async with get_db_ctx() as db:
            vhosts = await db.execute_fetchall("SELECT * FROM vhosts")
            assert len(vhosts) == 1
            assert vhosts[0]["server_name"] == "s3.example.com"
            assert vhosts[0]["ssl"] == 1

            routes = await db.execute_fetchall("SELECT * FROM routes")
            assert len(routes) == 1
            assert routes[0]["path_prefix"] == "/assets/"

        cfg_mod.settings = None
        db_mod._db_path = None

    @pytest.mark.asyncio
    async def test_rebuild_recovers_pool_type_from_sidecar(self, tmp_dirs, setup_nginx_configs):
        """Pool type (nanio/http) is recovered from sidecar."""
        import nanio_orchestrator.config as cfg_mod
        import nanio_orchestrator.db as db_mod
        cfg_mod.settings = None
        db_mod._db_path = None

        os.environ["NANIO_ORCHESTRATOR_DB_PATH"] = tmp_dirs["db_path"]
        os.environ["NANIO_ORCHESTRATOR_NGINX_CONFIG_DIR"] = tmp_dirs["nginx_dir"]

        from nanio_orchestrator.db import set_db_path
        set_db_path(tmp_dirs["db_path"])

        if os.path.exists(tmp_dirs["db_path"]):
            os.unlink(tmp_dirs["db_path"])

        with patch("nanio_orchestrator.s3client.list_buckets", new_callable=AsyncMock, return_value=[]):
            from nanio_orchestrator.rebuild import rebuild_from_disk
            result = await rebuild_from_disk()

        from nanio_orchestrator.db import get_db_ctx
        async with get_db_ctx() as db:
            pools = await db.execute_fetchall("SELECT type FROM pools WHERE name = 'pool-default'")
            assert pools[0]["type"] == "nanio"

        cfg_mod.settings = None
        db_mod._db_path = None

    @pytest.mark.asyncio
    async def test_rebuild_recovers_default_pool_from_sidecar(self, tmp_dirs, setup_nginx_configs):
        """Vhost default_pool_id is recovered from sidecar."""
        import nanio_orchestrator.config as cfg_mod
        import nanio_orchestrator.db as db_mod
        cfg_mod.settings = None
        db_mod._db_path = None

        os.environ["NANIO_ORCHESTRATOR_DB_PATH"] = tmp_dirs["db_path"]
        os.environ["NANIO_ORCHESTRATOR_NGINX_CONFIG_DIR"] = tmp_dirs["nginx_dir"]

        from nanio_orchestrator.db import set_db_path
        set_db_path(tmp_dirs["db_path"])

        if os.path.exists(tmp_dirs["db_path"]):
            os.unlink(tmp_dirs["db_path"])

        with patch("nanio_orchestrator.s3client.list_buckets", new_callable=AsyncMock, return_value=[]):
            from nanio_orchestrator.rebuild import rebuild_from_disk
            result = await rebuild_from_disk()

        from nanio_orchestrator.db import get_db_ctx
        async with get_db_ctx() as db:
            vhosts = await db.execute_fetchall("SELECT default_pool_id FROM vhosts")
            assert vhosts[0]["default_pool_id"] is not None

        cfg_mod.settings = None
        db_mod._db_path = None

    @pytest.mark.asyncio
    async def test_rebuild_recovers_credentials_from_sidecar(self, tmp_dirs, setup_nginx_configs):
        """Pool credentials (encrypted) are recovered from sidecar."""
        import nanio_orchestrator.config as cfg_mod
        import nanio_orchestrator.db as db_mod
        cfg_mod.settings = None
        db_mod._db_path = None

        os.environ["NANIO_ORCHESTRATOR_DB_PATH"] = tmp_dirs["db_path"]
        os.environ["NANIO_ORCHESTRATOR_NGINX_CONFIG_DIR"] = tmp_dirs["nginx_dir"]
        os.environ["NANIO_ORCHESTRATOR_SECRET"] = "ASsfJ7RCxJiOrjeD9KX0LPlMe-EJtA2lKIh2yk-D6U0="

        from nanio_orchestrator.credentials import encrypt, reset_fernet
        reset_fernet()

        # Add credentials to sidecar
        sidecar_path = os.path.join(tmp_dirs["nginx_dir"], "pools", "pool-default.meta.json")
        data = json.loads(Path(sidecar_path).read_text())
        data["credentials"] = {
            "access_key_enc": encrypt("AKIATEST"),
            "secret_key_enc": encrypt("secretkey"),
            "endpoint_url": None,
            "region": "us-east-1",
        }
        Path(sidecar_path).write_text(json.dumps(data))

        from nanio_orchestrator.db import set_db_path
        set_db_path(tmp_dirs["db_path"])

        if os.path.exists(tmp_dirs["db_path"]):
            os.unlink(tmp_dirs["db_path"])

        with patch("nanio_orchestrator.s3client.list_buckets", new_callable=AsyncMock, return_value=[]):
            from nanio_orchestrator.rebuild import rebuild_from_disk
            result = await rebuild_from_disk()

        assert result["credentials_recovered"] == 1

        # Verify credentials are decryptable
        from nanio_orchestrator.credentials import get_pool_credentials
        from nanio_orchestrator.db import get_db_ctx
        async with get_db_ctx() as db:
            pool = await db.execute_fetchall("SELECT id FROM pools WHERE name = 'pool-default'")
        creds = await get_pool_credentials(pool[0]["id"])
        assert creds is not None
        assert creds["access_key"] == "AKIATEST"

        reset_fernet()
        cfg_mod.settings = None
        db_mod._db_path = None

    @pytest.mark.asyncio
    async def test_rebuild_recovers_migration_from_state(self, tmp_dirs, setup_nginx_configs):
        """In-progress migration is recovered from .state.json."""
        import nanio_orchestrator.config as cfg_mod
        import nanio_orchestrator.db as db_mod
        cfg_mod.settings = None
        db_mod._db_path = None

        os.environ["NANIO_ORCHESTRATOR_DB_PATH"] = tmp_dirs["db_path"]
        os.environ["NANIO_ORCHESTRATOR_NGINX_CONFIG_DIR"] = tmp_dirs["nginx_dir"]

        # Create a second pool config for migration target
        pool2_conf = """# managed by nanio-orchestrator
# pool_id:2 name:pool-2025 type:nanio updated:2026-04-16T10:00:00Z
upstream pool-2025 {
    least_conn;
    server 10.0.1.1:9000 weight=1 max_fails=3 fail_timeout=30s;
    keepalive 32;
}
"""
        Path(os.path.join(tmp_dirs["nginx_dir"], "pools", "pool-2025.conf")).write_text(pool2_conf)
        pool2_sidecar = {"pool_id": 2, "name": "pool-2025", "type": "nanio"}
        Path(os.path.join(tmp_dirs["nginx_dir"], "pools", "pool-2025.meta.json")).write_text(
            json.dumps(pool2_sidecar)
        )

        # Create migration state file (lives alongside the DB, not in nginx dir)
        migrations_dir = str(Path(tmp_dirs["db_path"]).parent / "migrations")
        os.makedirs(migrations_dir, exist_ok=True)
        migration_state = {
            "migration_id": 7,
            "vhost_id": 1,
            "bucket": "assets",
            "source_pool_id": 1,
            "source_pool_name": "pool-default",
            "target_pool_id": 2,
            "target_pool_name": "pool-2025",
            "status": "copying",
            "copied_objects": 1842,
            "total_objects": 4291,
            "bytes_transferred": 9834729472,
            "nginx_state": "source",
        }
        Path(os.path.join(migrations_dir, "migration-7.state.json")).write_text(
            json.dumps(migration_state)
        )

        from nanio_orchestrator.db import set_db_path
        set_db_path(tmp_dirs["db_path"])

        if os.path.exists(tmp_dirs["db_path"]):
            os.unlink(tmp_dirs["db_path"])

        with patch("nanio_orchestrator.s3client.list_buckets", new_callable=AsyncMock, return_value=[]):
            from nanio_orchestrator.rebuild import rebuild_from_disk
            result = await rebuild_from_disk()

        assert result["migrations_imported"] == 1

        from nanio_orchestrator.db import get_db_ctx
        async with get_db_ctx() as db:
            migs = await db.execute_fetchall("SELECT * FROM migrations")
            assert len(migs) == 1
            # Active migrations are reset to pending for restart
            assert migs[0]["phase"] == "pending"
            assert migs[0]["bucket"] == "assets"

        cfg_mod.settings = None
        db_mod._db_path = None

    @pytest.mark.asyncio
    async def test_rebuild_without_sidecars(self, tmp_dirs):
        """Rebuild with no sidecar files recovers what it can."""
        import nanio_orchestrator.config as cfg_mod
        import nanio_orchestrator.db as db_mod
        cfg_mod.settings = None
        db_mod._db_path = None

        os.environ["NANIO_ORCHESTRATOR_DB_PATH"] = tmp_dirs["db_path"]
        os.environ["NANIO_ORCHESTRATOR_NGINX_CONFIG_DIR"] = tmp_dirs["nginx_dir"]

        # Write pool config without sidecar
        pool_conf = """# managed by nanio-orchestrator
# pool_id:1 name:no-sidecar type:nanio updated:2026-04-16T10:00:00Z
upstream no-sidecar {
    least_conn;
    server 10.0.0.1:9000 weight=1 max_fails=3 fail_timeout=30s;
    keepalive 32;
}
"""
        Path(os.path.join(tmp_dirs["nginx_dir"], "pools", "no-sidecar.conf")).write_text(pool_conf)

        from nanio_orchestrator.db import set_db_path
        set_db_path(tmp_dirs["db_path"])

        if os.path.exists(tmp_dirs["db_path"]):
            os.unlink(tmp_dirs["db_path"])

        with patch("nanio_orchestrator.s3client.list_buckets", new_callable=AsyncMock, return_value=[]):
            from nanio_orchestrator.rebuild import rebuild_from_disk
            result = await rebuild_from_disk()

        assert result["pools_imported"] == 1
        # Type defaults to "nanio" without sidecar
        from nanio_orchestrator.db import get_db_ctx
        async with get_db_ctx() as db:
            pools = await db.execute_fetchall("SELECT type FROM pools WHERE name = 'no-sidecar'")
            assert pools[0]["type"] == "nanio"

        cfg_mod.settings = None
        db_mod._db_path = None

    @pytest.mark.asyncio
    async def test_rebuild_dry_run(self, tmp_dirs, setup_nginx_configs):
        """--dry-run reports what would be imported without writing."""
        import nanio_orchestrator.config as cfg_mod
        import nanio_orchestrator.db as db_mod
        cfg_mod.settings = None
        db_mod._db_path = None

        os.environ["NANIO_ORCHESTRATOR_DB_PATH"] = tmp_dirs["db_path"]
        os.environ["NANIO_ORCHESTRATOR_NGINX_CONFIG_DIR"] = tmp_dirs["nginx_dir"]

        from nanio_orchestrator.db import init_db, set_db_path
        set_db_path(tmp_dirs["db_path"])
        await init_db()

        from nanio_orchestrator.rebuild import rebuild_from_disk
        result = await rebuild_from_disk(dry_run=True)

        assert result["dry_run"] is True
        assert len(result["pools"]) == 1
        assert result["pools"][0]["name"] == "pool-default"
        assert len(result["vhosts"]) == 1
        assert result["vhosts"][0]["server_name"] == "s3.example.com"

        # DB should NOT have been modified with data
        from nanio_orchestrator.db import get_db_ctx
        async with get_db_ctx() as db:
            pools = await db.execute_fetchall("SELECT COUNT(*) as cnt FROM pools")
            assert pools[0]["cnt"] == 0

        cfg_mod.settings = None
        db_mod._db_path = None

    @pytest.mark.asyncio
    async def test_rebuild_force_overwrites(self, tmp_dirs, setup_nginx_configs):
        """--force clears existing DB data before rebuild."""
        import nanio_orchestrator.config as cfg_mod
        import nanio_orchestrator.db as db_mod
        cfg_mod.settings = None
        db_mod._db_path = None

        os.environ["NANIO_ORCHESTRATOR_DB_PATH"] = tmp_dirs["db_path"]
        os.environ["NANIO_ORCHESTRATOR_NGINX_CONFIG_DIR"] = tmp_dirs["nginx_dir"]

        from nanio_orchestrator.db import get_db_ctx, init_db, set_db_path
        set_db_path(tmp_dirs["db_path"])
        await init_db()

        # Insert some existing data
        async with get_db_ctx() as db:
            await db.execute(
                "INSERT INTO pools (name, type) VALUES ('old-pool', 'nanio')"
            )
            await db.commit()

        # Rebuild via API (force mode)
        with patch("nanio_orchestrator.s3client.list_buckets", new_callable=AsyncMock, return_value=[]):
            from nanio_orchestrator.rebuild import rebuild_from_disk
            # First, clear existing data (simulating force)
            async with get_db_ctx() as db:
                for table in [
                    "migration_log", "migrations",
                    "node_configs", "bucket_sync", "pool_credentials",
                    "routes", "pool_members", "audit_log", "config_files",
                    "vhosts", "pools",
                ]:
                    await db.execute(f"DELETE FROM {table}")
                await db.commit()

            result = await rebuild_from_disk()

        assert result["pools_imported"] == 1

        # Old data should be gone, only rebuilt data
        async with get_db_ctx() as db:
            pools = await db.execute_fetchall("SELECT name FROM pools")
            names = [p["name"] for p in pools]
            assert "pool-default" in names
            assert "old-pool" not in names

        cfg_mod.settings = None
        db_mod._db_path = None

    @pytest.mark.asyncio
    async def test_rebuild_bucket_sync_unreachable(self, tmp_dirs, setup_nginx_configs):
        """Rebuild logs warning when nanio-default is unreachable."""
        import nanio_orchestrator.config as cfg_mod
        import nanio_orchestrator.db as db_mod
        cfg_mod.settings = None
        db_mod._db_path = None

        os.environ["NANIO_ORCHESTRATOR_DB_PATH"] = tmp_dirs["db_path"]
        os.environ["NANIO_ORCHESTRATOR_NGINX_CONFIG_DIR"] = tmp_dirs["nginx_dir"]

        from nanio_orchestrator.db import set_db_path
        set_db_path(tmp_dirs["db_path"])

        if os.path.exists(tmp_dirs["db_path"]):
            os.unlink(tmp_dirs["db_path"])

        # Make list_buckets raise an error
        with patch("nanio_orchestrator.s3client.list_buckets", new_callable=AsyncMock, side_effect=ConnectionError("unreachable")):
            from nanio_orchestrator.rebuild import rebuild_from_disk
            result = await rebuild_from_disk()

        # Should still succeed, just with warnings
        assert result["pools_imported"] == 1
        assert any("unreachable" in w for w in result.get("warnings", []))

        cfg_mod.settings = None
        db_mod._db_path = None

    @pytest.mark.asyncio
    async def test_rebuild_api_endpoint(self, client, app, mock_nginx, tmp_dirs, setup_nginx_configs):
        """POST /api/config/rebuild-from-disk works."""
        with patch("nanio_orchestrator.s3client.list_buckets", new_callable=AsyncMock, return_value=[]):
            resp = await client.post("/api/config/rebuild-from-disk?dry_run=true")
        assert resp.status_code == 200
        data = resp.json()
        assert data["dry_run"] is True

    @pytest.mark.asyncio
    async def test_rebuild_api_refuses_without_force(self, client, app, mock_nginx, tmp_dirs):
        """API refuses rebuild if DB has data and force is not set."""
        # Create a pool first
        await create_pool(client, name="existing-pool")

        resp = await client.post("/api/config/rebuild-from-disk")
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_rebuild_config_files_sha256(self, tmp_dirs, setup_nginx_configs):
        """Rebuild recomputes config_files sha256 records."""
        import nanio_orchestrator.config as cfg_mod
        import nanio_orchestrator.db as db_mod
        cfg_mod.settings = None
        db_mod._db_path = None

        os.environ["NANIO_ORCHESTRATOR_DB_PATH"] = tmp_dirs["db_path"]
        os.environ["NANIO_ORCHESTRATOR_NGINX_CONFIG_DIR"] = tmp_dirs["nginx_dir"]

        from nanio_orchestrator.db import set_db_path
        set_db_path(tmp_dirs["db_path"])

        if os.path.exists(tmp_dirs["db_path"]):
            os.unlink(tmp_dirs["db_path"])

        with patch("nanio_orchestrator.s3client.list_buckets", new_callable=AsyncMock, return_value=[]):
            from nanio_orchestrator.rebuild import rebuild_from_disk
            await rebuild_from_disk()

        from nanio_orchestrator.db import get_db_ctx
        async with get_db_ctx() as db:
            files = await db.execute_fetchall("SELECT * FROM config_files")
            assert len(files) >= 2  # pool + vhost config files
            for f in files:
                assert f["sha256_disk"] is not None
                assert f["sha256_db"] == f["sha256_disk"]

        cfg_mod.settings = None
        db_mod._db_path = None


    @pytest.mark.asyncio
    async def test_rebuild_recovers_source_nanio_pool_id_from_sidecar(self, tmp_dirs, setup_nginx_configs):
        """source_nanio_pool_id on an http pool is recovered from the pool sidecar."""
        import nanio_orchestrator.config as cfg_mod
        import nanio_orchestrator.db as db_mod
        cfg_mod.settings = None
        db_mod._db_path = None

        os.environ["NANIO_ORCHESTRATOR_DB_PATH"] = tmp_dirs["db_path"]
        os.environ["NANIO_ORCHESTRATOR_NGINX_CONFIG_DIR"] = tmp_dirs["nginx_dir"]

        # Write an http pool config that references the existing nanio pool
        pools_dir = os.path.join(tmp_dirs["nginx_dir"], "pools")
        http_conf = """# managed by nanio-orchestrator
# pool_id:99 name:http-cdn type:http updated:2026-04-16T10:00:00Z
upstream http-cdn {
    server 192.168.1.10:80 weight=1;
    keepalive 8;
}
"""
        Path(os.path.join(pools_dir, "http-cdn.conf")).write_text(http_conf)
        http_sidecar = {
            "pool_id": 99,
            "name": "http-cdn",
            "type": "http",
            "description": "CDN layer",
            "source_nanio_pool_id": 1,
            "updated_at": "2026-04-16T10:00:00Z",
        }
        Path(os.path.join(pools_dir, "http-cdn.meta.json")).write_text(json.dumps(http_sidecar))

        from nanio_orchestrator.db import set_db_path
        set_db_path(tmp_dirs["db_path"])

        if os.path.exists(tmp_dirs["db_path"]):
            os.unlink(tmp_dirs["db_path"])

        with patch("nanio_orchestrator.s3client.list_buckets", new_callable=AsyncMock, return_value=[]):
            from nanio_orchestrator.rebuild import rebuild_from_disk
            await rebuild_from_disk()

        from nanio_orchestrator.db import get_db_ctx
        async with get_db_ctx() as db:
            pools = await db.execute_fetchall("SELECT * FROM pools WHERE name = 'http-cdn'")
            assert len(pools) == 1
            p = dict(pools[0])
            assert p["type"] == "http"
            assert p["source_nanio_pool_id"] is not None, (
                "source_nanio_pool_id must be restored from the pool sidecar during rebuild"
            )
            # The rebuild assigns sequential IDs, so look up the nanio pool's actual id
            nanio_rows = await db.execute_fetchall("SELECT id FROM pools WHERE name = 'pool-default'")
            assert len(nanio_rows) == 1
            assert p["source_nanio_pool_id"] == nanio_rows[0]["id"], (
                "source_nanio_pool_id must point to the correct pool after rebuild"
            )

        cfg_mod.settings = None
        db_mod._db_path = None


class TestMigrationStateSidecar:
    """Migration state sidecar files."""

    @pytest.mark.asyncio
    async def test_migration_state_file_written(self, tmp_dirs):
        """Migration phase change writes a state file."""
        import nanio_orchestrator.config as cfg_mod
        import nanio_orchestrator.db as db_mod
        cfg_mod.settings = None
        db_mod._db_path = None

        os.environ["NANIO_ORCHESTRATOR_DB_PATH"] = tmp_dirs["db_path"]
        os.environ["NANIO_ORCHESTRATOR_NGINX_CONFIG_DIR"] = tmp_dirs["nginx_dir"]
        # migrations_dir is now next to the DB, not inside nginx_dir
        os.makedirs(str(Path(tmp_dirs["db_path"]).parent / "migrations"), exist_ok=True)

        from nanio_orchestrator.db import get_db_ctx, init_db, set_db_path
        set_db_path(tmp_dirs["db_path"])
        await init_db()

        # Create test pools and vhost
        async with get_db_ctx() as db:
            await db.execute("INSERT INTO pools (id, name, type) VALUES (1, 'src-pool', 'nanio')")
            await db.execute("INSERT INTO pools (id, name, type) VALUES (2, 'dst-pool', 'nanio')")
            await db.execute("INSERT INTO vhosts (id, server_name) VALUES (1, 'test.com')")
            await db.execute(
                """INSERT INTO migrations (id, vhost_id, bucket, src_pool_id, dst_pool_id, phase)
                   VALUES (1, 1, 'test-bucket', 1, 2, 'pending')"""
            )
            await db.commit()

        from nanio_orchestrator.migration_engine import _set_phase
        await _set_phase(1, "copying")

        state_path = str(Path(tmp_dirs["db_path"]).parent / "migrations" / "migration-1.state.json")
        assert os.path.exists(state_path)
        data = json.loads(Path(state_path).read_text())
        assert data["status"] == "copying"
        assert data["bucket"] == "test-bucket"

        cfg_mod.settings = None
        db_mod._db_path = None

    @pytest.mark.asyncio
    async def test_migration_state_file_deleted_on_done(self, tmp_dirs):
        """Completed migration deletes the state file."""
        import nanio_orchestrator.config as cfg_mod
        import nanio_orchestrator.db as db_mod
        cfg_mod.settings = None
        db_mod._db_path = None

        os.environ["NANIO_ORCHESTRATOR_DB_PATH"] = tmp_dirs["db_path"]
        os.environ["NANIO_ORCHESTRATOR_NGINX_CONFIG_DIR"] = tmp_dirs["nginx_dir"]
        # migrations_dir is now next to the DB, not inside nginx_dir
        os.makedirs(str(Path(tmp_dirs["db_path"]).parent / "migrations"), exist_ok=True)

        from nanio_orchestrator.db import get_db_ctx, init_db, set_db_path
        set_db_path(tmp_dirs["db_path"])
        await init_db()

        async with get_db_ctx() as db:
            await db.execute("INSERT INTO pools (id, name, type) VALUES (1, 'src-pool', 'nanio')")
            await db.execute("INSERT INTO pools (id, name, type) VALUES (2, 'dst-pool', 'nanio')")
            await db.execute("INSERT INTO vhosts (id, server_name) VALUES (1, 'test.com')")
            await db.execute(
                """INSERT INTO migrations (id, vhost_id, bucket, src_pool_id, dst_pool_id, phase)
                   VALUES (1, 1, 'bucket', 1, 2, 'switching')"""
            )
            await db.commit()

        from nanio_orchestrator.migration_engine import _set_phase
        # First write it
        await _set_phase(1, "copying")
        state_path = str(Path(tmp_dirs["db_path"]).parent / "migrations" / "migration-1.state.json")
        assert os.path.exists(state_path)

        # Mark done — should delete
        await _set_phase(1, "done")
        assert not os.path.exists(state_path)

        cfg_mod.settings = None
        db_mod._db_path = None


class TestBackupRotation:
    """Edge-case tests for backup file rotation."""

    @pytest.mark.asyncio
    async def test_backup_rotation_max_copies_1(self, tmp_dirs):
        """max_copies=1 keeps only the single .bak slot and never creates .bak.2."""
        import nanio_orchestrator.config as cfg_mod
        cfg_mod.settings = None

        os.environ["NANIO_ORCHESTRATOR_DB_PATH"] = tmp_dirs["db_path"]
        os.environ["NANIO_ORCHESTRATOR_DB_BACKUP_ROTATE"] = "1"

        from nanio_orchestrator.db import init_db, set_db_path
        set_db_path(tmp_dirs["db_path"])
        await init_db()

        from nanio_orchestrator.backup import backup_database
        bak_path = await backup_database()
        assert bak_path is not None
        assert os.path.exists(bak_path)
        assert not os.path.exists(bak_path + ".2"), ".bak.2 must not be created with max_copies=1"

        # A second backup must overwrite .bak, still no .bak.2
        await backup_database()
        assert os.path.exists(bak_path)
        assert not os.path.exists(bak_path + ".2"), ".bak.2 accumulated on second backup"

        cfg_mod.settings = None


class TestRebuildMigrationRecovery:
    """Rebuild migration state and completion record handling."""

    def _write_pool_configs(self, tmp_dirs):
        pools_dir = os.path.join(tmp_dirs["nginx_dir"], "pools")
        for name, ip in [("pool-src", "10.0.0.1"), ("pool-dst", "10.0.0.2")]:
            conf = f"""# managed by nanio-orchestrator
# pool_id:1 name:{name} type:nanio updated:2026-04-16T10:00:00Z
upstream {name} {{
    least_conn;
    server {ip}:9000 weight=1 max_fails=3 fail_timeout=30s;
    keepalive 32;
}}
"""
            Path(os.path.join(pools_dir, f"{name}.conf")).write_text(conf)
            Path(os.path.join(pools_dir, f"{name}.meta.json")).write_text(
                json.dumps({"pool_id": 1, "name": name, "type": "nanio"})
            )
        vhosts_dir = os.path.join(tmp_dirs["nginx_dir"], "vhosts")
        vhost_conf = """# managed by nanio-orchestrator
# vhost_id:1 name:s3.example.com updated:2026-04-16T10:00:00Z
server {
    listen 80;
    server_name s3.example.com;
    client_max_body_size 0;
}
"""
        Path(os.path.join(vhosts_dir, "s3.example.com.conf")).write_text(vhost_conf)
        Path(os.path.join(vhosts_dir, "s3.example.com.meta.json")).write_text(
            json.dumps({"vhost_id": 1, "server_name": "s3.example.com",
                        "default_pool_name": "pool-src"})
        )

    def _setup_env(self, tmp_dirs):
        import nanio_orchestrator.config as cfg_mod
        import nanio_orchestrator.db as db_mod
        cfg_mod.settings = None
        db_mod._db_path = None
        os.environ["NANIO_ORCHESTRATOR_DB_PATH"] = tmp_dirs["db_path"]
        os.environ["NANIO_ORCHESTRATOR_NGINX_CONFIG_DIR"] = tmp_dirs["nginx_dir"]
        from nanio_orchestrator.db import set_db_path
        set_db_path(tmp_dirs["db_path"])
        if os.path.exists(tmp_dirs["db_path"]):
            os.unlink(tmp_dirs["db_path"])

    @pytest.mark.asyncio
    async def test_rebuild_recovers_completion_with_orphaned_fields(self, tmp_dirs):
        """Rebuild imports .done.json completion records with orphaned_source_* fields."""
        self._write_pool_configs(tmp_dirs)
        self._setup_env(tmp_dirs)

        migrations_dir = str(Path(tmp_dirs["db_path"]).parent / "migrations")
        os.makedirs(migrations_dir, exist_ok=True)
        done_state = {
            "migration_id": 42,
            "vhost_id": 1,
            "bucket": "photos",
            "source_pool_id": 1,
            "source_pool_name": "pool-src",
            "target_pool_id": 2,
            "target_pool_name": "pool-dst",
            "mode": "copy",
            "route_id": None,
            "status": "done",
            "orphaned_source_pool_id": 1,
            "orphaned_source_prefix": "/photos/",
            "orphaned_at": "2026-04-28T10:00:00Z",
        }
        Path(os.path.join(migrations_dir, "migration-42.done.json")).write_text(
            json.dumps(done_state)
        )

        with patch("nanio_orchestrator.s3client.list_buckets", new_callable=AsyncMock, return_value=[]):
            from nanio_orchestrator.rebuild import rebuild_from_disk
            result = await rebuild_from_disk()

        assert result["completed_migrations_imported"] == 1

        from nanio_orchestrator.db import get_db_ctx
        async with get_db_ctx() as db:
            migs = await db.execute_fetchall("SELECT * FROM migrations")
            assert len(migs) == 1
            m = dict(migs[0])
            assert m["phase"] == "done"
            assert m["bucket"] == "photos"
            assert m["orphaned_source_prefix"] == "/photos/"
            assert m["orphaned_at"] == "2026-04-28T10:00:00Z"

        import nanio_orchestrator.config as cfg_mod
        import nanio_orchestrator.db as db_mod
        cfg_mod.settings = None
        db_mod._db_path = None

    @pytest.mark.asyncio
    async def test_rebuild_switching_phase_preserved(self, tmp_dirs):
        """Migrations stuck in 'switching' are preserved as switching (not reset to pending).
        recover_interrupted_migrations will mark them as error for operator review."""
        self._write_pool_configs(tmp_dirs)
        self._setup_env(tmp_dirs)

        migrations_dir = str(Path(tmp_dirs["db_path"]).parent / "migrations")
        os.makedirs(migrations_dir, exist_ok=True)
        state = {
            "migration_id": 5,
            "vhost_id": 1,
            "bucket": "data",
            "source_pool_id": 1,
            "source_pool_name": "pool-src",
            "target_pool_id": 2,
            "target_pool_name": "pool-dst",
            "status": "switching",
            "mode": "copy",
            "route_id": None,
            "copied_objects": 100,
            "total_objects": 100,
            "bytes_transferred": 0,
            "bytes_total": 0,
        }
        Path(os.path.join(migrations_dir, "migration-5.state.json")).write_text(
            json.dumps(state)
        )

        with patch("nanio_orchestrator.s3client.list_buckets", new_callable=AsyncMock, return_value=[]):
            from nanio_orchestrator.rebuild import rebuild_from_disk
            result = await rebuild_from_disk()

        assert result["migrations_imported"] == 1

        from nanio_orchestrator.db import get_db_ctx
        async with get_db_ctx() as db:
            migs = await db.execute_fetchall("SELECT * FROM migrations")
            assert migs[0]["phase"] == "switching"

        import nanio_orchestrator.config as cfg_mod
        import nanio_orchestrator.db as db_mod
        cfg_mod.settings = None
        db_mod._db_path = None

    @pytest.mark.asyncio
    async def test_rebuild_state_includes_mode_and_route_id(self, tmp_dirs):
        """State files now include mode and route_id, which rebuild restores."""
        self._write_pool_configs(tmp_dirs)
        self._setup_env(tmp_dirs)

        migrations_dir = str(Path(tmp_dirs["db_path"]).parent / "migrations")
        os.makedirs(migrations_dir, exist_ok=True)
        state = {
            "migration_id": 9,
            "vhost_id": 1,
            "bucket": "archive",
            "source_pool_id": 1,
            "source_pool_name": "pool-src",
            "target_pool_id": 2,
            "target_pool_name": "pool-dst",
            "status": "copying",
            "mode": "copy",
            "route_id": None,
            "copied_objects": 50,
            "total_objects": 200,
            "bytes_transferred": 0,
            "bytes_total": 0,
        }
        Path(os.path.join(migrations_dir, "migration-9.state.json")).write_text(
            json.dumps(state)
        )

        with patch("nanio_orchestrator.s3client.list_buckets", new_callable=AsyncMock, return_value=[]):
            from nanio_orchestrator.rebuild import rebuild_from_disk
            result = await rebuild_from_disk()

        assert result["migrations_imported"] == 1

        from nanio_orchestrator.db import get_db_ctx
        async with get_db_ctx() as db:
            migs = await db.execute_fetchall("SELECT * FROM migrations")
            m = dict(migs[0])
            assert m["phase"] == "pending"  # copying → pending
            assert m["route_id"] is None

        import nanio_orchestrator.config as cfg_mod
        import nanio_orchestrator.db as db_mod
        cfg_mod.settings = None
        db_mod._db_path = None

    @pytest.mark.asyncio
    async def test_rebuild_no_purge_references(self, tmp_dirs):
        """rebuild.py must not reference purge_source or needs_purge in any path."""
        import inspect

        from nanio_orchestrator.rebuild import rebuild_from_disk
        src = inspect.getsource(rebuild_from_disk)
        assert "purge_source" not in src
        assert "needs_purge" not in src


class TestRebuildAPIEdgeCases:
    """Edge-case tests for the rebuild API endpoint."""

    @pytest.mark.asyncio
    async def test_rebuild_api_works_when_db_file_missing(self, client, app, mock_nginx, tmp_dirs, setup_nginx_configs):
        """POST /api/config/rebuild-from-disk succeeds even when DB file is absent."""
        # Remove the DB so the endpoint must create it via init_db()
        if os.path.exists(tmp_dirs["db_path"]):
            os.unlink(tmp_dirs["db_path"])

        with patch("nanio_orchestrator.s3client.list_buckets", new_callable=AsyncMock, return_value=[]):
            resp = await client.post("/api/config/rebuild-from-disk?force=true")
        assert resp.status_code == 200
        data = resp.json()
        assert data["pools_imported"] >= 1

    @pytest.fixture
    def setup_nginx_configs(self, tmp_dirs):
        """Minimal nginx config for edge-case tests."""
        pools_dir = os.path.join(tmp_dirs["nginx_dir"], "pools")
        pool_conf = """# managed by nanio-orchestrator
# pool_id:1 name:edge-pool type:nanio updated:2026-04-16T10:00:00Z
upstream edge-pool {
    least_conn;
    server 10.0.0.1:9000 weight=1 max_fails=3 fail_timeout=30s;
    keepalive 32;
}
"""
        Path(os.path.join(pools_dir, "edge-pool.conf")).write_text(pool_conf)
        return tmp_dirs
