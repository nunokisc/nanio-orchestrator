"""Rebuilding the database from disk: what each kind of file contributes, and what is skipped."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, patch

import nanio_orchestrator.config as cfg_mod
import nanio_orchestrator.db as db_mod
import pytest
from nanio_orchestrator.db import get_db_ctx
from nanio_orchestrator.nginx.generator import render_upstream, render_vhost
from nanio_orchestrator.rebuild import rebuild_from_disk


class Disk:
    """Writes what the orchestrator leaves on disk: generated configs, sidecars, state files."""

    def __init__(self, tmp_dirs):
        self.pools = Path(tmp_dirs["nginx_dir"]) / "pools"
        self.vhosts = Path(tmp_dirs["nginx_dir"]) / "vhosts"
        self.migrations = Path(tmp_dirs["db_path"]).parent / "migrations"
        self.db_path = Path(tmp_dirs["db_path"])

    def pool(
        self, name: str, pool_id: int, address: Optional[str] = None, sidecar: Optional[dict] = None, **pool
    ) -> None:
        members = pool.pop("members", None) or [{"address": address or f"10.0.0.{pool_id}:9000"}]
        members = [
            {"role": "active", "weight": 1, "max_fails": 3, "fail_timeout_s": 30, "enabled": 1, **m} for m in members
        ]
        row = {"id": pool_id, "name": name, "type": "nanio", "lb_method": "least_conn", "keepalive": 32, **pool}
        (self.pools / f"{name}.conf").write_text(render_upstream(row, members))
        if sidecar is not None:
            data = {"pool_id": pool_id, "name": name, "type": row["type"], "description": None, **sidecar}
            (self.pools / f"{name}.meta.json").write_text(json.dumps(data))

    def vhost(self, server_name: str, vhost_id: int, routes=(), sidecar: Optional[dict] = None, **vhost) -> None:
        row = {"id": vhost_id, "server_name": server_name, "listen_port": 80, "ssl": 0, **vhost}
        rendered_routes = [
            {
                "id": 100 * vhost_id + n,
                "path_prefix": route[0],
                "pool_name": route[1],
                "pool_type": "nanio",
                "enabled": 1,
                "migration_dst_pool_name": route[2] if len(route) > 2 else None,
            }
            for n, route in enumerate(routes)
        ]
        (self.vhosts / f"{server_name}.conf").write_text(render_vhost(row, rendered_routes))
        if sidecar is not None:
            data = {"vhost_id": vhost_id, "server_name": server_name, **sidecar}
            (self.vhosts / f"{server_name}.meta.json").write_text(json.dumps(data))

    def state(self, migration_id: int, **state) -> None:
        data = {
            "migration_id": migration_id,
            "vhost_id": 1,
            "bucket": "photos",
            "source_pool_name": "src",
            "target_pool_name": "dst",
            "status": "copying",
            **state,
        }
        (self.migrations / f"migration-{migration_id}.state.json").write_text(json.dumps(data))

    def done(self, migration_id: int, **state) -> None:
        data = {
            "migration_id": migration_id,
            "vhost_id": 1,
            "bucket": "photos",
            "source_pool_name": "src",
            "target_pool_name": "dst",
            "status": "done",
            "orphaned_source_prefix": "/photos/",
            "orphaned_at": "2026-04-28T10:00:00Z",
            **state,
        }
        (self.migrations / f"migration-{migration_id}.done.json").write_text(json.dumps(data))

    def two_pools_one_vhost(self, routes=(("/photos/", "src"),)) -> None:
        self.pool("src", 1, sidecar={})
        self.pool("dst", 2, sidecar={})
        self.vhost("s3.example.com", 1, routes=routes, sidecar={"default_pool_name": "src"})


@pytest.fixture
def disk(tmp_dirs, monkeypatch):
    monkeypatch.setenv("NANIO_ORCHESTRATOR_DB_PATH", tmp_dirs["db_path"])
    monkeypatch.setenv("NANIO_ORCHESTRATOR_NGINX_CONFIG_DIR", tmp_dirs["nginx_dir"])
    cfg_mod.settings = None
    db_mod.set_db_path(tmp_dirs["db_path"])
    yield Disk(tmp_dirs)
    cfg_mod.settings = None
    db_mod._db_path = None


async def _rebuild(buckets=(), **kwargs) -> dict:
    listing = AsyncMock(return_value=[{"name": b} for b in buckets])
    with patch("nanio_orchestrator.s3client.list_buckets", listing):
        result = await rebuild_from_disk(**kwargs)
    result["list_buckets"] = listing
    return result


async def _rows(sql: str, params=()) -> list[dict]:
    async with get_db_ctx() as db:
        return [dict(r) for r in await db.execute_fetchall(sql, params)]


async def _pool_id(name: str) -> int:
    return (await _rows("SELECT id FROM pools WHERE name = ?", (name,)))[0]["id"]


async def _vhost_id(server_name: str) -> int:
    return (await _rows("SELECT id FROM vhosts WHERE server_name = ?", (server_name,)))[0]["id"]


# ── Pools ─────────────────────────────────────────────────────────────────────


class TestPools:
    async def test_upstream_file_alone_gives_a_nanio_pool_with_its_members(self, disk):
        disk.pool(
            "hot",
            4,
            lb_method="ip_hash",
            keepalive=64,
            members=[
                {"address": "10.0.0.1:9000", "weight": 5, "max_fails": 2, "fail_timeout_s": 10},
                {"address": "10.0.0.2:9000", "role": "replica"},
            ],
        )
        result = await _rebuild()

        assert (result["pools_imported"], result["members_imported"], result["warnings"]) == (1, 2, [])
        pools = await _rows("SELECT name, type, description, lb_method, keepalive FROM pools")
        assert pools == [{"name": "hot", "type": "nanio", "description": None, "lb_method": "ip_hash", "keepalive": 64}]
        members = await _rows(
            "SELECT address, role, weight, max_fails, fail_timeout_s, enabled FROM pool_members ORDER BY id"
        )
        assert members == [
            {"address": "10.0.0.1:9000", "role": "active", "weight": 5, "max_fails": 2, "fail_timeout_s": 10, "enabled": 1},
            {"address": "10.0.0.2:9000", "role": "replica", "weight": 1, "max_fails": 3, "fail_timeout_s": 30, "enabled": 1},
        ]  # fmt: skip

    async def test_sidecar_supplies_type_description_and_credentials(self, disk):
        creds = {"access_key_enc": "enc-a", "secret_key_enc": "enc-s", "endpoint_url": "http://e", "region": "eu-1"}
        disk.pool("hot", 1, sidecar={"description": "fast disks", "credentials": creds})
        disk.pool("web", 2, type="http", sidecar={})
        result = await _rebuild()

        assert result["credentials_recovered"] == 1
        pools = await _rows("SELECT name, type, description FROM pools ORDER BY name")
        assert pools == [
            {"name": "hot", "type": "nanio", "description": "fast disks"},
            {"name": "web", "type": "http", "description": None},
        ]
        stored = await _rows(
            "SELECT pool_id, access_key_enc, secret_key_enc, endpoint_url, region FROM pool_credentials"
        )
        assert stored == [{"pool_id": await _pool_id("hot"), **creds}]

    async def test_credentials_without_region_default_to_us_east_1(self, disk):
        disk.pool("hot", 1, sidecar={"credentials": {"access_key_enc": "a", "secret_key_enc": "s"}})
        await _rebuild()
        assert await _rows("SELECT endpoint_url, region FROM pool_credentials") == [
            {"endpoint_url": None, "region": "us-east-1"}
        ]

    @pytest.mark.parametrize("creds", [{"access_key_enc": "a"}, {"secret_key_enc": "s"}, {}])
    async def test_incomplete_credentials_are_not_imported(self, disk, creds):
        disk.pool("hot", 1, sidecar={"credentials": creds})
        result = await _rebuild()
        assert result["credentials_recovered"] == 0
        assert await _rows("SELECT * FROM pool_credentials") == []

    async def test_files_that_are_not_managed_upstreams_are_ignored(self, disk):
        disk.pool("hot", 1)
        (disk.pools / "manual.conf").write_text("upstream manual {\n    server 10.9.9.9:80;\n}\n")
        (disk.pools / "empty.conf").write_text("# managed by nanio-orchestrator\n")
        (disk.pools / "dir.conf").mkdir()  # cannot be read as a file
        result = await _rebuild()

        assert result["pools_imported"] == 1
        assert [p["name"] for p in await _rows("SELECT name FROM pools")] == ["hot"]
        assert [Path(f["path"]).name for f in await _rows("SELECT path FROM config_files")] == ["hot.conf"]

    async def test_sidecar_without_a_config_file_creates_nothing(self, disk):
        (disk.pools / "ghost.meta.json").write_text(json.dumps({"pool_id": 1, "name": "ghost", "type": "nanio"}))
        (disk.pools / "nameless.meta.json").write_text(json.dumps({"pool_id": 2, "type": "nanio"}))
        result = await _rebuild()
        assert result["pools_imported"] == 0
        assert await _rows("SELECT * FROM pools") == []

    async def test_http_pool_finds_its_nanio_pool_although_ids_change(self, disk):
        # Sorted by file name, "archive" is imported first: the ids it had before mean nothing now
        disk.pool("archive", 7, sidecar={})
        disk.pool("hot", 9, sidecar={})
        disk.pool("web", 12, type="http", sidecar={"source_nanio_pool_id": 9})
        result = await _rebuild()

        assert result["warnings"] == []
        web = (await _rows("SELECT source_nanio_pool_id FROM pools WHERE name = 'web'"))[0]
        assert web["source_nanio_pool_id"] == await _pool_id("hot")
        assert await _pool_id("hot") != 9

    async def test_http_pool_whose_nanio_pool_is_gone_is_reported(self, disk):
        disk.pool("hot", 1, sidecar={})
        disk.pool("web", 2, type="http", sidecar={"source_nanio_pool_id": 99})
        disk.pool("plain", 3, type="http", sidecar={})  # never had a link: nothing to report
        result = await _rebuild()

        assert len(result["warnings"]) == 1
        assert "Pool 'web'" in result["warnings"][0] and "99" in result["warnings"][0]
        links = await _rows("SELECT name, source_nanio_pool_id FROM pools WHERE type = 'http' ORDER BY name")
        assert links == [
            {"name": "plain", "source_nanio_pool_id": None},
            {"name": "web", "source_nanio_pool_id": None},
        ]

    async def test_http_pool_is_not_linked_to_another_http_pool(self, disk):
        disk.pool("web-old", 5, type="http", sidecar={})
        disk.pool("web", 6, type="http", sidecar={"source_nanio_pool_id": 5})
        result = await _rebuild()
        assert len(result["warnings"]) == 1
        assert (await _rows("SELECT source_nanio_pool_id FROM pools WHERE name = 'web'"))[0][
            "source_nanio_pool_id"
        ] is None


# ── Vhosts and routes ─────────────────────────────────────────────────────────


class TestVhosts:
    async def test_sidecar_restores_default_pool_extra_blocks_and_ip_rules(self, disk):
        blocks = json.dumps([{"zone": "proxy", "content": "proxy_read_timeout 60s;"}])
        ips = json.dumps(["10.0.0.0/8"])
        disk.pool("hot", 3, sidecar={})
        disk.vhost(
            "s3.example.com",
            8,
            listen_port=443,
            ssl=1,
            ssl_cert_path="/etc/ssl/s3.pem",
            ssl_key_path="/etc/ssl/s3.key",
            extra_blocks_json=blocks,
            ip_rule_mode="deny",
            ip_rule_ips_json=ips,
            sidecar={
                "default_pool_id": 3,
                "default_pool_name": "hot",
                "extra_blocks_json": blocks,
                "ip_rule_mode": "deny",
                "ip_rule_ips_json": ips,
            },
        )
        result = await _rebuild()

        assert (result["vhosts_imported"], result["warnings"]) == (1, [])
        vhost = (await _rows("SELECT * FROM vhosts"))[0]
        assert vhost["server_name"] == "s3.example.com"
        assert (vhost["listen_port"], vhost["ssl"], vhost["enabled"]) == (443, 1, 1)
        assert (vhost["ssl_cert_path"], vhost["ssl_key_path"]) == ("/etc/ssl/s3.pem", "/etc/ssl/s3.key")
        assert vhost["default_pool_id"] == await _pool_id("hot")
        assert (vhost["extra_blocks_json"], vhost["ip_rule_mode"], vhost["ip_rule_ips_json"]) == (blocks, "deny", ips)

    async def test_without_sidecar_only_what_nginx_knows_is_restored(self, disk):
        disk.pool("hot", 1)
        disk.vhost("s3.example.com", 1, routes=[("/photos/", "hot")], listen_port=8080)
        result = await _rebuild(buckets=["photos"])

        vhost = (await _rows("SELECT * FROM vhosts"))[0]
        assert (vhost["listen_port"], vhost["ssl"], vhost["ssl_cert_path"]) == (8080, 0, None)
        for column in ("default_pool_id", "extra_blocks_json", "ip_rule_mode", "ip_rule_ips_json"):
            assert vhost[column] is None
        assert result["routes_imported"] == 1
        # No default pool: nobody to ask for the bucket list
        result["list_buckets"].assert_not_called()
        assert await _rows("SELECT * FROM bucket_sync") == []

    async def test_default_pool_named_by_the_sidecar_but_missing_is_left_empty(self, disk):
        disk.pool("hot", 1)
        disk.vhost("s3.example.com", 1, sidecar={"default_pool_name": "gone"})
        await _rebuild()
        assert (await _rows("SELECT default_pool_id FROM vhosts"))[0]["default_pool_id"] is None

    async def test_route_to_a_pool_that_is_not_on_disk_is_skipped_with_a_warning(self, disk):
        disk.pool("hot", 1)
        disk.vhost("s3.example.com", 1, routes=[("/photos/", "hot"), ("/videos/", "gone")])
        result = await _rebuild()

        assert result["routes_imported"] == 1
        assert result["warnings"] == ["Route /videos/ on s3.example.com: pool 'gone' not found, skipping"]
        assert [r["path_prefix"] for r in await _rows("SELECT path_prefix FROM routes")] == ["/photos/"]

    async def test_files_that_are_not_managed_vhosts_are_ignored(self, disk):
        disk.pool("hot", 1)
        disk.vhost("s3.example.com", 1)
        (disk.vhosts / "manual.conf").write_text("server {\n    listen 80;\n    server_name manual.example.com;\n}\n")
        (disk.vhosts / "nameless.conf").write_text("# managed by nanio-orchestrator\nserver {\n    listen 80;\n}\n")
        (disk.vhosts / "dir.conf").mkdir()
        result = await _rebuild()

        assert result["vhosts_imported"] == 1
        assert [v["server_name"] for v in await _rows("SELECT server_name FROM vhosts")] == ["s3.example.com"]

    async def test_each_vhost_gets_its_own_routes(self, disk):
        disk.pool("hot", 1)
        disk.pool("cold", 2)
        disk.vhost("a.example.com", 1, routes=[("/photos/", "hot")])
        disk.vhost("b.example.com", 2, routes=[("/photos/", "cold"), ("/logs/", "hot")])
        result = await _rebuild()

        assert (result["vhosts_imported"], result["routes_imported"]) == (2, 3)
        routes = await _rows(
            """SELECT v.server_name, r.path_prefix, p.name AS pool FROM routes r
               JOIN vhosts v ON v.id = r.vhost_id JOIN pools p ON p.id = r.pool_id
               ORDER BY v.server_name, r.path_prefix"""
        )
        assert routes == [
            {"server_name": "a.example.com", "path_prefix": "/photos/", "pool": "hot"},
            {"server_name": "b.example.com", "path_prefix": "/logs/", "pool": "hot"},
            {"server_name": "b.example.com", "path_prefix": "/photos/", "pool": "cold"},
        ]

    async def test_route_served_through_a_migration_split_still_belongs_to_the_source(self, disk):
        disk.two_pools_one_vhost(routes=[("/photos/", "src", "dst")])
        await _rebuild()
        routes = await _rows("SELECT r.path_prefix, p.name AS pool FROM routes r JOIN pools p ON p.id = r.pool_id")
        assert routes == [{"path_prefix": "/photos/", "pool": "src"}]

    async def test_imported_files_are_recorded_as_in_sync(self, disk):
        disk.two_pools_one_vhost()
        await _rebuild()
        files = await _rows("SELECT path, sha256_disk, sha256_db, content_snapshot FROM config_files ORDER BY path")
        assert [Path(f["path"]).name for f in files] == ["dst.conf", "src.conf", "s3.example.com.conf"]
        for f in files:
            assert f["sha256_disk"] == f["sha256_db"]
            assert f["content_snapshot"] == Path(f["path"]).read_text()


# ── Migrations in progress ────────────────────────────────────────────────────


class TestMigrationStates:
    @pytest.mark.parametrize(
        ("status", "phase"),
        [
            ("pending", "pending"),
            ("copying", "pending"),
            ("verifying", "pending"),
            ("write_routing", "write_routing"),
            ("switching", "switching"),
            ("error", "error"),
            ("cancelled", "cancelled"),
        ],
    )
    async def test_phase_restarts_only_where_that_is_safe(self, disk, status, phase):
        disk.two_pools_one_vhost()
        disk.state(5, status=status)
        result = await _rebuild()

        assert (result["migrations_imported"], result["completed_migrations_imported"]) == (1, 0)
        assert [m["phase"] for m in await _rows("SELECT phase FROM migrations")] == [phase]

    async def test_state_without_status_is_pending(self, disk):
        disk.two_pools_one_vhost()
        (disk.migrations / "migration-5.state.json").write_text(
            json.dumps({"migration_id": 5, "vhost_id": 1, "source_pool_name": "src", "target_pool_name": "dst"})
        )
        await _rebuild()
        rows = await _rows("SELECT phase, bucket, folder, objects_total, split_routing FROM migrations")
        assert rows == [{"phase": "pending", "bucket": "", "folder": "", "objects_total": 0, "split_routing": 0}]

    async def test_pools_counters_and_folder_are_restored(self, disk):
        disk.two_pools_one_vhost(routes=[("/photos/", "src"), ("/photos/2025/", "src")])
        disk.state(
            5,
            folder="2025/",
            total_objects=200,
            copied_objects=50,
            bytes_total=4000,
            bytes_transferred=1000,
            route_id=777,  # the id the route had before the rebuild
        )
        await _rebuild()

        migration = (await _rows("SELECT * FROM migrations"))[0]
        assert migration["vhost_id"] == await _vhost_id("s3.example.com")
        assert (migration["src_pool_id"], migration["dst_pool_id"]) == (await _pool_id("src"), await _pool_id("dst"))
        assert (migration["bucket"], migration["folder"]) == ("photos", "2025/")
        assert (migration["objects_total"], migration["objects_done"]) == (200, 50)
        assert (migration["bytes_total"], migration["bytes_done"]) == (4000, 1000)
        route = await _rows("SELECT id FROM routes WHERE path_prefix = '/photos/2025/'")
        assert migration["route_id"] == route[0]["id"]

    async def test_route_link_is_empty_when_the_bucket_has_no_route(self, disk):
        disk.two_pools_one_vhost(routes=[])
        disk.state(5, route_id=777)
        await _rebuild()
        assert (await _rows("SELECT route_id FROM migrations"))[0]["route_id"] is None

    @pytest.mark.parametrize(
        ("state", "split"),
        [
            ({"split_routing": True}, 1),
            ({"split_routing": False, "nginx_state": "split"}, 0),  # the explicit flag wins
            ({"nginx_state": "split"}, 1),  # state file written before the flag existed
            ({"nginx_state": "normal"}, 0),
            ({}, 0),
        ],
    )
    async def test_split_routing_survives_whatever_the_phase(self, disk, state, split):
        disk.two_pools_one_vhost(routes=[("/photos/", "src", "dst")])
        disk.state(5, status="error", **state)
        await _rebuild()
        assert await _rows("SELECT phase, split_routing FROM migrations") == [
            {"phase": "error", "split_routing": split}
        ]

    @pytest.mark.parametrize(
        ("names", "shown"),
        [
            ({"source_pool_name": "gone"}, "src=gone, dst=dst"),
            ({"target_pool_name": "gone"}, "src=src, dst=gone"),
            ({"source_pool_name": None, "target_pool_name": None}, "src=None, dst=None"),
        ],
    )
    async def test_migration_between_unknown_pools_is_skipped_with_a_warning(self, disk, names, shown):
        disk.two_pools_one_vhost()
        disk.state(5, **names)
        result = await _rebuild()

        assert result["migrations_imported"] == 0
        assert result["warnings"] == [f"Migration 5: pool not found ({shown}), skipping"]
        assert await _rows("SELECT * FROM migrations") == []

    async def test_only_vhost_adopts_a_migration_whose_vhost_id_is_unknown(self, disk):
        disk.two_pools_one_vhost()
        disk.state(5, vhost_id=42)
        result = await _rebuild()
        assert result["warnings"] == []
        assert (await _rows("SELECT vhost_id FROM migrations"))[0]["vhost_id"] == await _vhost_id("s3.example.com")

    async def test_with_several_vhosts_an_unknown_vhost_id_is_not_guessed(self, disk):
        disk.two_pools_one_vhost()
        disk.vhost("other.example.com", 2)
        disk.state(5, vhost_id=42)
        result = await _rebuild()

        assert result["migrations_imported"] == 0
        assert len(result["warnings"]) == 1
        assert "Migration 5: vhost_id 42 not found" in result["warnings"][0]
        assert await _rows("SELECT * FROM migrations") == []

    async def test_with_several_vhosts_the_migration_goes_to_the_one_named_in_the_state(self, disk):
        disk.two_pools_one_vhost()
        disk.vhost("t3.example.com", 2, routes=[("/photos/", "src")])
        disk.state(5, vhost_id=2)
        await _rebuild()
        assert (await _rows("SELECT vhost_id FROM migrations"))[0]["vhost_id"] == await _vhost_id("t3.example.com")

    async def test_without_sidecars_the_vhost_id_in_the_state_is_taken_as_it_is(self, disk):
        disk.pool("src", 1)
        disk.pool("dst", 2)
        disk.vhost("a.example.com", 1)
        disk.vhost("b.example.com", 2)
        disk.state(5, vhost_id=2)
        disk.done(6, vhost_id=1, bucket="old")
        await _rebuild()
        rows = await _rows("SELECT bucket, vhost_id FROM migrations ORDER BY bucket")
        assert rows == [
            {"bucket": "old", "vhost_id": await _vhost_id("a.example.com")},
            {"bucket": "photos", "vhost_id": await _vhost_id("b.example.com")},
        ]

    async def test_migration_of_a_vhost_that_is_no_longer_on_disk_is_not_given_to_another(self, disk):
        disk.two_pools_one_vhost()
        disk.vhost("other.example.com", 2)
        # Its sidecar outlived its config file; the id it had now belongs to other.example.com
        (disk.vhosts / "gone.example.com.meta.json").write_text(
            json.dumps({"vhost_id": 2, "server_name": "gone.example.com"})
        )
        disk.state(5, vhost_id=2)
        result = await _rebuild()

        assert result["migrations_imported"] == 0
        assert "Migration 5: vhost_id 2 not found" in result["warnings"][0]

    async def test_migration_follows_its_vhost_to_its_new_id(self, disk):
        """Vhost ids change on a rebuild; the sidecars say which vhost had the id in the state file."""
        disk.pool("src", 1, sidecar={})
        disk.pool("dst", 2, sidecar={})
        disk.vhost("a.example.com", 2, routes=[("/photos/", "src")], sidecar={})  # imported first: becomes 1
        disk.vhost("b.example.com", 1, sidecar={})
        disk.state(5, vhost_id=2)
        disk.done(6, vhost_id=2, bucket="old")
        result = await _rebuild()

        assert result["warnings"] == []
        vhost_id = await _vhost_id("a.example.com")
        assert vhost_id == 1
        rows = await _rows("SELECT bucket, vhost_id, route_id FROM migrations ORDER BY bucket")
        assert [(r["bucket"], r["vhost_id"]) for r in rows] == [("old", vhost_id), ("photos", vhost_id)]
        assert rows[1]["route_id"] is not None

    async def test_without_any_vhost_migrations_are_skipped(self, disk):
        disk.pool("src", 1)
        disk.pool("dst", 2)
        disk.state(5)
        disk.done(6)
        result = await _rebuild()

        assert result["migrations_imported"] == 0
        assert result["warnings"] == [
            "Migration 5: no vhosts found, skipping",
            "Completed migration 6: vhost not found, skipping",
        ]

    async def test_several_migrations_are_all_imported(self, disk):
        disk.two_pools_one_vhost(routes=[("/photos/", "src"), ("/videos/", "dst")])
        disk.state(5, bucket="photos", status="write_routing", split_routing=True)
        disk.state(6, bucket="videos", source_pool_name="dst", target_pool_name="src", status="copying")
        disk.done(7, bucket="old")
        result = await _rebuild()

        assert (result["migrations_imported"], result["completed_migrations_imported"]) == (3, 1)
        rows = await _rows("SELECT bucket, phase, split_routing FROM migrations ORDER BY bucket")
        assert rows == [
            {"bucket": "old", "phase": "done", "split_routing": 0},
            {"bucket": "photos", "phase": "write_routing", "split_routing": 1},
            {"bucket": "videos", "phase": "pending", "split_routing": 0},
        ]


class TestCompletedMigrations:
    async def test_orphaned_source_is_restored(self, disk):
        disk.two_pools_one_vhost(routes=[("/photos/", "dst"), ("/photos/2025/", "dst")])
        disk.done(9, folder="2025/", orphaned_source_prefix="/photos/2025/", orphaned_source_pool_id=1)
        result = await _rebuild()

        assert (result["migrations_imported"], result["completed_migrations_imported"]) == (1, 1)
        migration = (await _rows("SELECT * FROM migrations"))[0]
        assert (migration["phase"], migration["bucket"], migration["folder"]) == ("done", "photos", "2025/")
        assert (migration["src_pool_id"], migration["dst_pool_id"]) == (await _pool_id("src"), await _pool_id("dst"))
        assert migration["orphaned_source_prefix"] == "/photos/2025/"
        assert migration["orphaned_at"] == "2026-04-28T10:00:00Z"
        assert migration["split_routing"] == 0
        route = await _rows("SELECT id FROM routes WHERE path_prefix = '/photos/2025/'")
        assert migration["route_id"] == route[0]["id"]

    async def test_orphaned_pool_follows_the_source_pool_to_its_new_id(self, disk):
        """Pool ids change on a rebuild; the record must not point at whichever pool got the old id."""
        disk.pool("dst", 31, sidecar={})  # imported first: becomes 1
        disk.pool("src", 30, sidecar={})
        disk.vhost("s3.example.com", 1, sidecar={"default_pool_name": "dst"})
        disk.done(9, orphaned_source_pool_id=30)
        result = await _rebuild()

        assert result["completed_migrations_imported"] == 1
        migration = (await _rows("SELECT src_pool_id, orphaned_source_pool_id FROM migrations"))[0]
        assert migration["orphaned_source_pool_id"] == migration["src_pool_id"] == await _pool_id("src")

    async def test_record_without_orphan_information_stays_without(self, disk):
        disk.two_pools_one_vhost()
        disk.done(9, orphaned_source_prefix=None, orphaned_at=None)
        await _rebuild()
        rows = await _rows("SELECT orphaned_source_pool_id, orphaned_source_prefix, orphaned_at FROM migrations")
        assert rows == [{"orphaned_source_pool_id": None, "orphaned_source_prefix": None, "orphaned_at": None}]

    @pytest.mark.parametrize(
        ("names", "shown"),
        [
            ({"source_pool_name": "gone"}, "src=gone, dst=dst"),
            ({"target_pool_name": None}, "src=src, dst=None"),
        ],
    )
    async def test_record_between_unknown_pools_is_skipped_with_a_warning(self, disk, names, shown):
        disk.two_pools_one_vhost()
        disk.done(9, **names)
        result = await _rebuild()

        assert result["completed_migrations_imported"] == 0
        assert result["warnings"] == [f"Completed migration 9: pool not found ({shown}), skipping"]

    async def test_only_vhost_adopts_a_record_whose_vhost_id_is_unknown(self, disk):
        disk.two_pools_one_vhost()
        disk.done(9, vhost_id=42)
        result = await _rebuild()
        assert result["completed_migrations_imported"] == 1

    async def test_with_several_vhosts_an_unknown_vhost_id_is_not_guessed(self, disk):
        disk.two_pools_one_vhost()
        disk.vhost("other.example.com", 2)
        disk.done(9, vhost_id=42)
        result = await _rebuild()

        assert result["completed_migrations_imported"] == 0
        assert result["warnings"] == ["Completed migration 9: vhost not found, skipping"]


# ── Bucket list ───────────────────────────────────────────────────────────────


class TestBucketSyncRebuild:
    async def test_listed_buckets_are_marked_routed_or_unrouted(self, disk):
        disk.pool("src", 1, address="10.0.0.1:9000", sidecar={})
        disk.pool("dst", 2, sidecar={})
        disk.vhost(
            "s3.example.com",
            1,
            routes=[("/", "src"), ("/photos/", "dst")],
            sidecar={"default_pool_name": "src"},
        )
        result = await _rebuild(buckets=["photos", "logs"])

        assert result["warnings"] == []
        result["list_buckets"].assert_awaited_once_with("10.0.0.1:9000", None, None)
        buckets = await _rows("SELECT bucket, status FROM bucket_sync ORDER BY bucket")
        assert buckets == [{"bucket": "logs", "status": "unrouted"}, {"bucket": "photos", "status": "routed"}]

    async def test_entries_without_a_name_are_ignored(self, disk):
        disk.two_pools_one_vhost()
        listing = AsyncMock(return_value=[{"name": ""}, {"created": "x"}, {"name": "photos"}])
        with patch("nanio_orchestrator.s3client.list_buckets", listing):
            await rebuild_from_disk()
        assert [b["bucket"] for b in await _rows("SELECT bucket FROM bucket_sync")] == ["photos"]

    async def test_unreachable_pool_is_a_warning_and_the_rest_is_still_rebuilt(self, disk):
        disk.two_pools_one_vhost()
        with patch("nanio_orchestrator.s3client.list_buckets", AsyncMock(side_effect=OSError("connection refused"))):
            result = await rebuild_from_disk()

        vhost_id = await _vhost_id("s3.example.com")
        assert result["warnings"] == [f"bucket_sync rebuild for vhost {vhost_id}: connection refused"]
        assert (result["pools_imported"], result["vhosts_imported"], result["routes_imported"]) == (2, 1, 1)
        assert await _rows("SELECT * FROM bucket_sync") == []

    async def test_one_failing_vhost_does_not_stop_the_others(self, disk):
        disk.pool("bad", 1, address="10.0.0.1:9000", sidecar={})
        disk.pool("good", 2, address="10.0.0.2:9000", sidecar={})
        disk.vhost("a.example.com", 1, sidecar={"default_pool_name": "bad"})
        disk.vhost("b.example.com", 2, sidecar={"default_pool_name": "good"})

        async def list_buckets(address, access_key, secret_key):
            if address == "10.0.0.1:9000":
                raise OSError("down")
            return [{"name": "photos"}]

        with patch("nanio_orchestrator.s3client.list_buckets", list_buckets):
            result = await rebuild_from_disk()

        assert len(result["warnings"]) == 1 and "down" in result["warnings"][0]
        buckets = await _rows("SELECT vhost_id, bucket FROM bucket_sync")
        assert buckets == [{"vhost_id": await _vhost_id("b.example.com"), "bucket": "photos"}]

    async def test_default_pool_without_members_is_not_asked(self, disk):
        (disk.pools / "empty.conf").write_text(
            "# managed by nanio-orchestrator\nupstream empty {\n    least_conn;\n    keepalive 32;\n}\n"
        )
        disk.vhost("s3.example.com", 1, sidecar={"default_pool_name": "empty"})
        result = await _rebuild(buckets=["photos"])

        assert result["warnings"] == []
        assert (await _rows("SELECT default_pool_id FROM vhosts"))[0]["default_pool_id"] == await _pool_id("empty")
        result["list_buckets"].assert_not_called()
        assert await _rows("SELECT * FROM bucket_sync") == []

    async def test_stored_credentials_are_used_to_list(self, disk, set_secret):
        from nanio_orchestrator.credentials import encrypt

        creds = {"access_key_enc": encrypt("AKIA"), "secret_key_enc": encrypt("shh")}
        disk.pool("src", 1, address="10.0.0.1:9000", sidecar={"credentials": creds})
        disk.vhost("s3.example.com", 1, sidecar={"default_pool_name": "src"})
        result = await _rebuild(buckets=["photos"])
        result["list_buckets"].assert_awaited_once_with("10.0.0.1:9000", "AKIA", "shh")


# ── Dry run ───────────────────────────────────────────────────────────────────


class TestDryRun:
    async def test_reports_what_is_on_disk_without_creating_a_database(self, disk):
        disk.pool(
            "hot",
            1,
            members=[{"address": "10.0.0.1:9000"}, {"address": "10.0.0.2:9000"}],
            sidecar={"credentials": {"access_key_enc": "a", "secret_key_enc": "s"}},
        )
        disk.pool("web", 2, type="http", sidecar={"source_nanio_pool_id": 1})
        disk.pool("bare", 3)
        disk.vhost(
            "a.example.com",
            1,
            routes=[("/photos/", "hot"), ("/logs/", "hot")],
            sidecar={
                "default_pool_id": 1,
                "default_pool_name": "hot",
                "extra_blocks_json": None,
                "ip_rule_mode": "allow",
                "ip_rule_ips_json": '["10.0.0.1"]',
            },
        )
        disk.vhost("b.example.com", 2, sidecar={"default_pool_name": "hot", "extra_blocks_json": '[{"zone":"top"}]'})
        disk.vhost("c.example.com", 3)
        disk.state(5)
        disk.state(6)
        disk.done(7)

        result = await _rebuild(dry_run=True)

        result["list_buckets"].assert_not_called()
        assert not disk.db_path.exists()
        assert (result["dry_run"], result["migrations"], result["completed_migrations"]) == (True, 2, 1)
        assert result["warnings"] == []
        assert result["pools"] == [
            {
                "name": "bare",
                "members": 1,
                "type": "nanio",
                "has_credentials": False,
                "has_sidecar": False,
                "source_nanio_pool_id": None,
            },
            {
                "name": "hot",
                "members": 2,
                "type": "nanio",
                "has_credentials": True,
                "has_sidecar": True,
                "source_nanio_pool_id": None,
            },
            {
                "name": "web",
                "members": 1,
                "type": "http",
                "has_credentials": False,
                "has_sidecar": True,
                "source_nanio_pool_id": 1,
            },
        ]
        assert result["vhosts"] == [
            {
                "server_name": "a.example.com",
                "enabled": True,
                "routes": 2,
                "has_default_pool": True,
                "has_extra_blocks": False,
                "has_ip_rules": True,
                "has_sidecar": True,
            },
            {
                "server_name": "b.example.com",
                "enabled": True,
                "routes": 0,
                "has_default_pool": True,  # known by name only
                "has_extra_blocks": True,
                "has_ip_rules": False,
                "has_sidecar": True,
            },
            {
                "server_name": "c.example.com",
                "enabled": True,
                "routes": 0,
                "has_default_pool": False,
                "has_extra_blocks": False,
                "has_ip_rules": False,
                "has_sidecar": False,
            },
        ]

    async def test_skips_the_same_files_as_the_real_rebuild(self, disk):
        disk.pool("hot", 1)
        disk.vhost("s3.example.com", 1)
        (disk.pools / "manual.conf").write_text("upstream manual {\n    server 10.9.9.9:80;\n}\n")
        (disk.pools / "empty.conf").write_text("# managed by nanio-orchestrator\n")
        (disk.pools / "dir.conf").mkdir()
        (disk.vhosts / "manual.conf").write_text("server {\n    server_name manual.example.com;\n}\n")
        (disk.vhosts / "nameless.conf").write_text("# managed by nanio-orchestrator\nserver {\n    listen 80;\n}\n")
        (disk.vhosts / "nothing.conf").write_text("# managed by nanio-orchestrator\n")
        (disk.vhosts / "dir.conf").mkdir()

        report = await _rebuild(dry_run=True)
        assert [p["name"] for p in report["pools"]] == ["hot"]
        assert [v["server_name"] for v in report["vhosts"]] == ["s3.example.com"]

        real = await _rebuild()
        assert (real["pools_imported"], real["vhosts_imported"]) == (1, 1)

    async def test_empty_disk_gives_an_empty_report(self, disk):
        result = await _rebuild(dry_run=True)
        del result["list_buckets"]
        assert result == {
            "dry_run": True,
            "pools": [],
            "vhosts": [],
            "migrations": 0,
            "completed_migrations": 0,
            "warnings": [],
        }
