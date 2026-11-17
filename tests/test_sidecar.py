"""Sidecar and migration state files: what is written, what is preserved, what a scan returns."""

from __future__ import annotations

import json
from pathlib import Path

import nanio_orchestrator.config as cfg_mod
import nanio_orchestrator.db as db_mod
import pytest
from nanio_orchestrator import sidecar

CREDS = {"access_key_enc": "enc-a", "secret_key_enc": "enc-s", "endpoint_url": None, "region": "us-east-1"}


@pytest.fixture
def dirs(tmp_dirs, monkeypatch):
    monkeypatch.setenv("NANIO_ORCHESTRATOR_DB_PATH", tmp_dirs["db_path"])
    monkeypatch.setenv("NANIO_ORCHESTRATOR_NGINX_CONFIG_DIR", tmp_dirs["nginx_dir"])
    cfg_mod.settings = None
    db_mod.set_db_path(tmp_dirs["db_path"])
    yield {
        "pools": Path(tmp_dirs["nginx_dir"]) / "pools",
        "vhosts": Path(tmp_dirs["nginx_dir"]) / "vhosts",
        "migrations": Path(tmp_dirs["db_path"]).parent / "migrations",
    }
    cfg_mod.settings = None
    db_mod._db_path = None


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


class TestPoolSidecar:
    def test_holds_what_nginx_cannot_tell(self, dirs):
        sidecar.write_pool_sidecar(4, "hot", "nanio", description="fast disks", credentials=CREDS)
        data = _load(dirs["pools"] / "hot.meta.json")
        updated_at = data.pop("updated_at")
        assert data == {"pool_id": 4, "name": "hot", "type": "nanio", "description": "fast disks", "credentials": CREDS}
        assert updated_at.endswith("Z")
        assert sorted(p.name for p in dirs["pools"].iterdir()) == ["hot.meta.json"]  # no .tmp left behind

    def test_rewriting_without_credentials_keeps_the_stored_ones(self, dirs):
        sidecar.write_pool_sidecar(4, "hot", "nanio", credentials=CREDS)
        sidecar.write_pool_sidecar(4, "hot", "nanio", description="renamed")
        data = _load(dirs["pools"] / "hot.meta.json")
        assert (data["description"], data["credentials"]) == ("renamed", CREDS)

    def test_pool_that_never_had_credentials_gets_none(self, dirs):
        sidecar.write_pool_sidecar(4, "hot", "nanio")
        sidecar.write_pool_sidecar(4, "hot", "nanio", description="again")
        assert "credentials" not in _load(dirs["pools"] / "hot.meta.json")

    def test_only_http_pools_record_the_nanio_pool_they_serve(self, dirs):
        sidecar.write_pool_sidecar(5, "web", "http", source_nanio_pool_id=4)
        sidecar.write_pool_sidecar(6, "plain", "http")
        sidecar.write_pool_sidecar(4, "hot", "nanio", source_nanio_pool_id=9)
        assert _load(dirs["pools"] / "web.meta.json")["source_nanio_pool_id"] == 4
        assert "source_nanio_pool_id" not in _load(dirs["pools"] / "plain.meta.json")
        assert "source_nanio_pool_id" not in _load(dirs["pools"] / "hot.meta.json")

    def test_credentials_can_be_set_before_and_after_the_pool_sidecar_exists(self, dirs):
        sidecar.write_pool_credentials_sidecar("hot", "a1", "s1", endpoint_url="http://e", region="eu-1")
        assert _load(dirs["pools"] / "hot.meta.json")["credentials"] == {
            "access_key_enc": "a1",
            "secret_key_enc": "s1",
            "endpoint_url": "http://e",
            "region": "eu-1",
        }

        sidecar.write_pool_sidecar(4, "hot", "nanio", description="fast")
        sidecar.write_pool_credentials_sidecar("hot", "a2", "s2")
        data = _load(dirs["pools"] / "hot.meta.json")
        assert (data["pool_id"], data["description"]) == (4, "fast")
        assert data["credentials"] == {
            "access_key_enc": "a2",
            "secret_key_enc": "s2",
            "endpoint_url": None,
            "region": "us-east-1",
        }

    def test_deleting_credentials_keeps_the_rest(self, dirs):
        sidecar.write_pool_sidecar(4, "hot", "nanio", description="fast", credentials=CREDS)
        sidecar.delete_pool_credentials_sidecar("hot")
        data = _load(dirs["pools"] / "hot.meta.json")
        assert "credentials" not in data
        assert (data["pool_id"], data["description"]) == (4, "fast")

    def test_deleting_credentials_that_are_not_there_touches_nothing(self, dirs):
        sidecar.delete_pool_credentials_sidecar("ghost")
        assert list(dirs["pools"].iterdir()) == []

        path = dirs["pools"] / "hot.meta.json"
        path.write_text('{"pool_id": 4, "name": "hot", "updated_at": "then"}')
        sidecar.delete_pool_credentials_sidecar("hot")
        assert path.read_text() == '{"pool_id": 4, "name": "hot", "updated_at": "then"}'

    def test_delete_removes_the_file_and_tolerates_a_missing_one(self, dirs):
        sidecar.write_pool_sidecar(4, "hot", "nanio")
        sidecar.delete_pool_sidecar("hot")
        sidecar.delete_pool_sidecar("hot")
        assert list(dirs["pools"].iterdir()) == []


class TestVhostSidecar:
    def test_holds_default_pool_extra_blocks_and_ip_rules(self, dirs):
        sidecar.write_vhost_sidecar(
            2,
            "s3.example.com",
            default_pool_id=4,
            default_pool_name="hot",
            extra_blocks_json='[{"zone": "top", "content": "x;"}]',
            ip_rule_mode="allow",
            ip_rule_ips_json='["10.0.0.0/8"]',
        )
        data = _load(dirs["vhosts"] / "s3.example.com.meta.json")
        del data["updated_at"]
        assert data == {
            "vhost_id": 2,
            "server_name": "s3.example.com",
            "default_pool_id": 4,
            "default_pool_name": "hot",
            "extra_blocks_json": '[{"zone": "top", "content": "x;"}]',
            "ip_rule_mode": "allow",
            "ip_rule_ips_json": '["10.0.0.0/8"]',
            "enabled": True,
            "disabled_routes": [],
        }

    def test_what_the_server_block_cannot_hold_is_kept_until_replaced(self, dirs):
        disabled = [{"path_prefix": "/old/", "pool_name": "cold"}]
        sidecar.write_vhost_sidecar(2, "s3.example.com", enabled=False, disabled_routes=disabled)
        sidecar.write_vhost_sidecar(2, "s3.example.com", default_pool_name="hot")  # an unrelated rewrite
        data = _load(dirs["vhosts"] / "s3.example.com.meta.json")
        assert data["enabled"] is False and data["disabled_routes"] == disabled

        sidecar.write_vhost_sidecar(2, "s3.example.com", enabled=True, disabled_routes=[])
        data = _load(dirs["vhosts"] / "s3.example.com.meta.json")
        assert data["enabled"] is True and data["disabled_routes"] == []

    def test_pool_members_are_kept_until_replaced(self, dirs):
        members = [{"address": "10.0.0.1:80", "role": "primary", "enabled": False}]
        sidecar.write_pool_sidecar(1, "web", "http", members=members)
        sidecar.write_pool_sidecar(1, "web", "http", description="edited")  # an unrelated rewrite
        assert _load(dirs["pools"] / "web.meta.json")["members"] == members

        sidecar.write_pool_sidecar(1, "web", "http", members=[])
        assert _load(dirs["pools"] / "web.meta.json")["members"] == []

    def test_rewriting_replaces_every_field(self, dirs):
        sidecar.write_vhost_sidecar(2, "s3.example.com", default_pool_id=4, ip_rule_mode="deny")
        sidecar.write_vhost_sidecar(2, "s3.example.com")
        data = _load(dirs["vhosts"] / "s3.example.com.meta.json")
        assert (data["default_pool_id"], data["ip_rule_mode"]) == (None, None)

    def test_delete_removes_the_file_and_tolerates_a_missing_one(self, dirs):
        sidecar.write_vhost_sidecar(2, "s3.example.com")
        sidecar.delete_vhost_sidecar("s3.example.com")
        sidecar.delete_vhost_sidecar("s3.example.com")
        assert list(dirs["vhosts"].iterdir()) == []


class TestScan:
    def test_returns_readable_sidecars_in_file_name_order(self, dirs):
        sidecar.write_pool_sidecar(2, "zeta", "nanio")
        sidecar.write_pool_sidecar(1, "alpha", "http")
        (dirs["pools"] / "broken.meta.json").write_text("{not json")
        (dirs["pools"] / "empty.meta.json").write_text("{}")
        (dirs["pools"] / "dir.meta.json").mkdir()
        (dirs["pools"] / "alpha.conf").write_text("upstream alpha {}")
        sidecar.write_vhost_sidecar(1, "s3.example.com")

        assert [(p["name"], p["type"]) for p in sidecar.scan_pool_sidecars()] == [("alpha", "http"), ("zeta", "nanio")]
        assert [v["server_name"] for v in sidecar.scan_vhost_sidecars()] == ["s3.example.com"]

    def test_empty_directories_scan_to_nothing(self, dirs):
        assert sidecar.scan_pool_sidecars() == []
        assert sidecar.scan_vhost_sidecars() == []
        assert sidecar.scan_migration_states() == []
        assert sidecar.scan_migration_completions() == []


class TestMigrationFiles:
    def test_state_is_written_read_and_deleted(self, dirs):
        state = {"migration_id": 7, "bucket": "photos", "status": "copying"}
        sidecar.write_migration_state(state)

        stored = sidecar.read_migration_state(7)
        assert stored == {**state, "updated_at": stored["updated_at"]}
        assert (dirs["migrations"] / "migration-7.state.json").exists()
        assert sidecar.read_migration_state(8) is None

        sidecar.delete_migration_state(7)
        sidecar.delete_migration_state(7)
        assert sidecar.read_migration_state(7) is None

    def test_completion_record_is_separate_from_the_state_file(self, dirs):
        state = {"migration_id": 7, "bucket": "photos", "status": "done", "orphaned_source_prefix": "/photos/"}
        sidecar.write_migration_state(dict(state))
        sidecar.write_migration_completion(state)
        sidecar.delete_migration_state(7)

        assert "written_at" not in state  # the caller's dict is left alone
        assert sidecar.scan_migration_states() == []
        completions = sidecar.scan_migration_completions()
        assert len(completions) == 1
        assert completions[0]["orphaned_source_prefix"] == "/photos/"
        assert completions[0]["written_at"].endswith("Z")
        assert [p.name for p in dirs["migrations"].iterdir()] == ["migration-7.done.json"]

    def test_scans_tell_states_from_completions(self, dirs):
        sidecar.write_migration_state({"migration_id": 2, "status": "copying"})
        sidecar.write_migration_state({"migration_id": 1, "status": "error"})
        sidecar.write_migration_completion({"migration_id": 3, "status": "done"})

        assert [s["migration_id"] for s in sidecar.scan_migration_states()] == [1, 2]
        assert [s["migration_id"] for s in sidecar.scan_migration_completions()] == [3]


class TestWriteAllSidecars:
    async def test_database_is_the_truth_for_every_sidecar(self, dirs):
        await db_mod.init_db()
        # Stale files: credentials the database no longer has, a default pool that changed
        sidecar.write_pool_sidecar(1, "hot", "nanio", description="old", credentials=CREDS)
        sidecar.write_vhost_sidecar(1, "s3.example.com", default_pool_id=1, default_pool_name="hot")

        async with db_mod.get_db_ctx() as db:
            await db.execute(
                """INSERT INTO pools (id, name, type, description, source_nanio_pool_id) VALUES
                   (1, 'hot', 'nanio', 'fast', NULL), (2, 'cold', 'nanio', NULL, NULL), (3, 'web', 'http', NULL, 2)"""
            )
            await db.execute(
                """INSERT INTO pool_credentials (pool_id, access_key_enc, secret_key_enc, endpoint_url, region)
                   VALUES (2, 'enc-a', 'enc-s', 'http://e', 'eu-1')"""
            )
            await db.execute(
                """INSERT INTO vhosts (id, server_name, default_pool_id, extra_blocks_json, ip_rule_mode, ip_rule_ips_json)
                   VALUES (1, 's3.example.com', 2, '[]', 'deny', '["192.0.2.1"]'), (2, 'bare.example.com', NULL, NULL, NULL, NULL)"""
            )
            await db.commit()
            written = await sidecar.write_all_sidecars(db)

        assert written == 5
        hot = _load(dirs["pools"] / "hot.meta.json")
        assert hot["description"] == "fast" and "credentials" not in hot
        assert _load(dirs["pools"] / "cold.meta.json")["credentials"] == {
            "access_key_enc": "enc-a",
            "secret_key_enc": "enc-s",
            "endpoint_url": "http://e",
            "region": "eu-1",
        }
        assert _load(dirs["pools"] / "web.meta.json")["source_nanio_pool_id"] == 2
        vhost = _load(dirs["vhosts"] / "s3.example.com.meta.json")
        assert (vhost["default_pool_id"], vhost["default_pool_name"]) == (2, "cold")
        assert (vhost["extra_blocks_json"], vhost["ip_rule_mode"], vhost["ip_rule_ips_json"]) == (
            "[]",
            "deny",
            '["192.0.2.1"]',
        )
        bare = _load(dirs["vhosts"] / "bare.example.com.meta.json")
        assert (bare["default_pool_id"], bare["default_pool_name"]) == (None, None)
