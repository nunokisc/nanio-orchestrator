"""Pools API: refusals leave everything as it was, and removals clean up after themselves."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from tests.api_state import audit, break_inserts, conf, managed_files, sidecar, sql
from tests.conftest import create_member, create_pool, create_route, create_vhost


async def _pools(client):
    return (await client.get("/api/pools")).json()


async def _members(client, pool):
    return (await client.get(f"/api/pools/{pool['id']}/members")).json()


def _upstream(tmp_dirs, name):
    return conf(tmp_dirs, "pools", name).read_text()


class TestCreatePool:
    async def test_database_failure_is_not_reported_as_a_duplicate_name(self, client, tmp_dirs):
        await break_inserts("pools")

        resp = await client.post("/api/pools", json={"name": "pa"})

        assert resp.status_code == 500
        assert await _pools(client) == []
        assert managed_files(tmp_dirs) == {}


class TestUpdatePool:
    async def test_unknown_pool(self, client):
        resp = await client.put("/api/pools/999", json={"lb_method": "ip_hash"})

        assert resp.status_code == 404

    @pytest.mark.parametrize("body", [{}, {"description": None, "lb_method": None}])
    async def test_nothing_to_change_returns_the_pool_untouched(self, client, mock_nginx, tmp_dirs, body):
        pool = await create_pool(client, "pa", description="first")
        await create_member(client, pool["id"])
        files = managed_files(tmp_dirs)

        resp = await client.put(f"/api/pools/{pool['id']}", json=body)

        assert resp.status_code == 200
        assert resp.json() == pool
        assert await _pools(client) == [pool]
        assert managed_files(tmp_dirs) == files
        assert await audit(client, "update") == []

    async def test_change_is_written_to_the_upstream_file_and_the_sidecar(self, client, mock_nginx, tmp_dirs):
        pool = await create_pool(client, "pa")
        await create_member(client, pool["id"])
        assert "least_conn;" in _upstream(tmp_dirs, "pa")

        resp = await client.put(
            f"/api/pools/{pool['id']}", json={"lb_method": "ip_hash", "keepalive": 8, "description": "edge"}
        )

        assert resp.status_code == 200
        assert (resp.json()["lb_method"], resp.json()["keepalive"]) == ("ip_hash", 8)
        upstream = _upstream(tmp_dirs, "pa")
        assert "ip_hash;" in upstream and "least_conn;" not in upstream and "keepalive 8;" in upstream
        assert json.loads(sidecar(tmp_dirs, "pools", "pa").read_text())["description"] == "edge"

    async def test_link_to_a_nanio_pool_is_refused_on_a_nanio_pool(self, client, tmp_dirs):
        source = await create_pool(client, "source")
        pool = await create_pool(client, "pa")
        files = managed_files(tmp_dirs)

        resp = await client.put(f"/api/pools/{pool['id']}", json={"source_nanio_pool_id": source["id"]})

        assert resp.status_code == 422
        assert "http pools" in resp.json()["detail"]
        assert (await client.get(f"/api/pools/{pool['id']}")).json() == pool
        assert managed_files(tmp_dirs) == files

    async def test_link_to_a_pool_that_does_not_exist_is_refused(self, client, tmp_dirs):
        pool = await create_pool(client, "web", pool_type="http")
        files = managed_files(tmp_dirs)

        resp = await client.put(f"/api/pools/{pool['id']}", json={"source_nanio_pool_id": 999})

        assert resp.status_code == 422
        assert "does not exist" in resp.json()["detail"]
        assert (await client.get(f"/api/pools/{pool['id']}")).json() == pool
        assert managed_files(tmp_dirs) == files

    async def test_link_to_another_http_pool_is_refused(self, client, tmp_dirs):
        other = await create_pool(client, "web-a", pool_type="http")
        pool = await create_pool(client, "web-b", pool_type="http")
        files = managed_files(tmp_dirs)

        resp = await client.put(f"/api/pools/{pool['id']}", json={"source_nanio_pool_id": other["id"]})

        assert resp.status_code == 422
        assert "must reference a nanio pool" in resp.json()["detail"]
        assert (await client.get(f"/api/pools/{pool['id']}")).json() == pool
        assert managed_files(tmp_dirs) == files

    async def test_http_pool_turned_into_nanio_loses_its_link(self, client, mock_nginx, tmp_dirs):
        source = await create_pool(client, "source")
        pool = await create_pool(client, "web", pool_type="http", source_nanio_pool_id=source["id"])
        sidecar_path = sidecar(tmp_dirs, "pools", "web")
        assert json.loads(sidecar_path.read_text())["source_nanio_pool_id"] == source["id"]

        resp = await client.put(f"/api/pools/{pool['id']}", json={"type": "nanio"})

        assert resp.status_code == 200
        assert resp.json()["type"] == "nanio"
        assert resp.json()["source_nanio_pool_id"] is None
        written = json.loads(sidecar_path.read_text())
        assert written["type"] == "nanio"
        assert "source_nanio_pool_id" not in written

    async def test_type_change_is_refused_when_a_member_has_a_role_the_new_type_lacks(
        self, client, mock_nginx, tmp_dirs
    ):
        pool = await create_pool(client, "pa")
        await create_member(client, pool["id"])  # role 'active': nanio only
        files = managed_files(tmp_dirs)

        resp = await client.put(f"/api/pools/{pool['id']}", json={"type": "http"})

        assert resp.status_code == 400
        assert "'primary' or 'replica'" in resp.json()["detail"]
        assert (await client.get(f"/api/pools/{pool['id']}")).json() == pool
        assert managed_files(tmp_dirs) == files


class TestDeletePool:
    async def _cluster(self, client):
        pa = await create_pool(client, "pa")
        await create_member(client, pa["id"], "10.0.0.1:9000")
        pb = await create_pool(client, "pb")
        await create_member(client, pb["id"], "10.0.0.2:9000")
        vhost = await create_vhost(client, "s3.example.com", default_pool_id=pa["id"])
        return pa, pb, vhost

    async def test_refused_while_a_route_points_at_it(self, client, mock_nginx, tmp_dirs):
        _pa, pb, vhost = await self._cluster(client)
        await create_route(client, vhost["id"], "photos", pb["id"])
        files = managed_files(tmp_dirs)

        resp = await client.delete(f"/api/pools/{pb['id']}")

        assert resp.status_code == 409
        assert "s3.example.com" in resp.json()["detail"]
        assert [p["name"] for p in await _pools(client)] == ["pa", "pb"]
        assert len(await _members(client, pb)) == 1
        assert managed_files(tmp_dirs) == files

    async def test_refused_while_it_is_the_default_pool_of_a_vhost_without_its_catch_all_route(
        self, client, mock_nginx, tmp_dirs
    ):
        # A vhost read from disk can name a default pool without having a `location /`
        pa, _pb, vhost = await self._cluster(client)
        await sql("DELETE FROM routes WHERE vhost_id = ?", (vhost["id"],))
        files = managed_files(tmp_dirs)

        resp = await client.delete(f"/api/pools/{pa['id']}")

        assert resp.status_code == 409
        assert "default pool for vhost(s): s3.example.com" in resp.json()["detail"]
        assert [p["name"] for p in await _pools(client)] == ["pa", "pb"]
        assert managed_files(tmp_dirs) == files

    @pytest.mark.parametrize("phase", ["pending", "copying", "write_routing", "verifying", "switching"])
    @pytest.mark.parametrize("side", ["src_pool_id", "dst_pool_id"])
    async def test_refused_while_a_migration_is_using_it(self, client, mock_nginx, tmp_dirs, phase, side):
        _pa, pb, vhost = await self._cluster(client)
        pc = await create_pool(client, "pc")
        await create_member(client, pc["id"], "10.0.0.3:9000")
        ends = {"src_pool_id": pb["id"], "dst_pool_id": pc["id"]}
        await sql(
            "INSERT INTO migrations (vhost_id, bucket, src_pool_id, dst_pool_id, phase) VALUES (?, 'photos', ?, ?, ?)",
            (vhost["id"], ends["src_pool_id"], ends["dst_pool_id"], phase),
        )
        files = managed_files(tmp_dirs)

        resp = await client.delete(f"/api/pools/{ends[side]}")

        assert resp.status_code == 409
        assert "active migration(s) 1" in resp.json()["detail"]
        assert [p["name"] for p in await _pools(client)] == ["pa", "pb", "pc"]
        assert await sql("SELECT phase FROM migrations") == [{"phase": phase}]
        assert managed_files(tmp_dirs) == files

    async def test_removes_everything_that_belonged_to_the_pool(self, client, mock_nginx, tmp_dirs, set_secret):
        pa, pb, vhost = await self._cluster(client)
        resp = await client.put(f"/api/pools/{pb['id']}/credentials", json={"access_key": "AKIA", "secret_key": "s"})
        assert resp.status_code == 200
        await sql(
            "INSERT INTO migrations (vhost_id, bucket, src_pool_id, dst_pool_id, phase) VALUES (?, 'old', ?, ?, 'done')",
            (vhost["id"], pb["id"], pa["id"]),
        )
        await sql(
            "INSERT INTO bucket_sync (vhost_id, bucket, status, routed_pool_id) VALUES (?, 'old', 'unrouted', ?)",
            (vhost["id"], pb["id"]),
        )
        pools_dir = Path(tmp_dirs["nginx_dir"]) / "pools"
        assert (pools_dir / "pb.conf").exists() and (pools_dir / "pb.meta.json").exists()

        resp = await client.delete(f"/api/pools/{pb['id']}")

        assert resp.status_code == 204
        assert [p["name"] for p in await _pools(client)] == ["pa"]
        assert sorted(p.name for p in pools_dir.iterdir()) == ["pa.conf", "pa.meta.json"]
        assert await sql("SELECT id FROM pool_members WHERE pool_id = ?", (pb["id"],)) == []
        assert await sql("SELECT id FROM pool_credentials") == []
        assert await sql("SELECT id FROM migrations") == []
        assert await sql("SELECT bucket, routed_pool_id FROM bucket_sync") == [
            {"bucket": "old", "routed_pool_id": None}
        ]
        assert await sql("SELECT path FROM config_files WHERE path LIKE '%pb.conf'") == []
        assert [e["entity_id"] for e in await audit(client, "delete")] == [pb["id"]]


class TestMembers:
    async def test_listing_members_of_an_unknown_pool(self, client):
        assert (await client.get("/api/pools/999/members")).status_code == 404

    async def test_adding_a_member_to_an_unknown_pool(self, client):
        resp = await client.post("/api/pools/999/members", json={"address": "10.0.0.1:9000"})

        assert resp.status_code == 404

    @pytest.mark.parametrize(
        ("pool_type", "role", "message"),
        [
            ("nanio", "primary", "all members are active"),
            ("nanio", "replica", "all members are active"),
            ("http", "active", "require role 'primary' or 'replica'"),
        ],
    )
    async def test_role_that_does_not_fit_the_pool_type_is_refused(
        self, client, mock_nginx, tmp_dirs, pool_type, role, message
    ):
        pool = await create_pool(client, "pa", pool_type=pool_type)

        resp = await client.post(f"/api/pools/{pool['id']}/members", json={"address": "10.0.0.1:9000", "role": role})

        assert resp.status_code == 400
        assert message in resp.json()["detail"]
        assert await _members(client, pool) == []
        assert not conf(tmp_dirs, "pools", "pa").exists()

    async def test_updating_a_member_of_an_unknown_pool(self, client):
        resp = await client.put("/api/pools/999/members/1", json={"weight": 2})

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Pool not found"

    async def test_member_of_another_pool_is_not_found(self, client, mock_nginx, tmp_dirs):
        pa = await create_pool(client, "pa")
        pb = await create_pool(client, "pb")
        member = await create_member(client, pb["id"])
        files = managed_files(tmp_dirs)

        update = await client.put(f"/api/pools/{pa['id']}/members/{member['id']}", json={"weight": 2})
        delete = await client.delete(f"/api/pools/{pa['id']}/members/{member['id']}")
        node_config = await client.post(
            f"/api/pools/{pa['id']}/members/{member['id']}/node-config", json={"node_type": "nanio-only"}
        )

        assert [r.status_code for r in (update, delete, node_config)] == [404, 404, 404]
        assert {r.json()["detail"] for r in (update, delete, node_config)} == {"Member not found"}
        assert await _members(client, pb) == [member]
        assert managed_files(tmp_dirs) == files

    async def test_node_config_of_an_unknown_pool(self, client):
        resp = await client.post("/api/pools/999/members/1/node-config", json={"node_type": "nanio-only"})

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Pool not found"

    async def test_nothing_to_change_returns_the_member_untouched(self, client, mock_nginx, tmp_dirs):
        pool = await create_pool(client, "pa")
        member = await create_member(client, pool["id"])
        files = managed_files(tmp_dirs)

        resp = await client.put(f"/api/pools/{pool['id']}/members/{member['id']}", json={})

        assert resp.status_code == 200
        assert resp.json() == member
        assert await _members(client, pool) == [member]
        assert managed_files(tmp_dirs) == files

    async def test_role_change_that_does_not_fit_the_pool_type_is_refused(self, client, mock_nginx, tmp_dirs):
        pool = await create_pool(client, "pa")
        member = await create_member(client, pool["id"])
        files = managed_files(tmp_dirs)

        resp = await client.put(f"/api/pools/{pool['id']}/members/{member['id']}", json={"role": "replica"})

        assert resp.status_code == 400
        assert await _members(client, pool) == [member]
        assert managed_files(tmp_dirs) == files

    async def test_disabled_member_leaves_the_upstream_and_comes_back_when_enabled(self, client, mock_nginx, tmp_dirs):
        pool = await create_pool(client, "pa")
        await create_member(client, pool["id"], "10.0.0.1:9000")
        member = await create_member(client, pool["id"], "10.0.0.2:9000")
        url = f"/api/pools/{pool['id']}/members/{member['id']}"

        resp = await client.put(url, json={"enabled": False, "weight": 7})

        assert resp.status_code == 200
        assert (resp.json()["enabled"], resp.json()["weight"]) == (False, 7)
        assert "server 10.0.0.1:9000" in _upstream(tmp_dirs, "pa")
        assert "10.0.0.2:9000" not in _upstream(tmp_dirs, "pa")

        resp = await client.put(url, json={"enabled": True})

        assert resp.json()["enabled"] is True
        assert "server 10.0.0.2:9000 weight=7 " in _upstream(tmp_dirs, "pa")


class TestBucketStatus:
    async def test_bucket_on_a_pool_no_vhost_serves_is_unrouted_and_listed_first(self, client, mock_nginx):
        pa = await create_pool(client, "pa")
        await create_member(client, pa["id"], "10.0.0.1:9000")
        pb = await create_pool(client, "pb")
        await create_member(client, pb["id"], "10.0.0.2:9000")
        vhost = await create_vhost(client, "s3.example.com", default_pool_id=pa["id"])
        await create_route(client, vhost["id"], "photos", pb["id"])
        await sql(
            """INSERT INTO migrations (vhost_id, bucket, src_pool_id, dst_pool_id, phase, orphaned_source_pool_id)
               VALUES (?, 'moved', ?, ?, 'done', ?)""",
            (vhost["id"], pb["id"], pa["id"], pb["id"]),
        )
        listed = AsyncMock(return_value=[{"name": n} for n in ("photos", "zeta", "moved", "alpha")])

        with patch("nanio_orchestrator.api.pools.s3_list_buckets", new=listed):
            resp = await client.get(f"/api/pools/{pb['id']}/buckets/status")

        assert resp.status_code == 200
        assert listed.call_args.args == ("10.0.0.2:9000",)
        assert [(b["bucket"], b["status"]) for b in resp.json()["buckets"]] == [
            ("alpha", "unrouted"),
            ("zeta", "unrouted"),
            ("moved", "orphaned"),
            ("photos", "routed"),
        ]
        assert all(b["default_vhosts"] == [] for b in resp.json()["buckets"])
        assert resp.json()["buckets"][-1]["routed_in"] == [{"vhost_id": vhost["id"], "server_name": "s3.example.com"}]
        assert resp.json()["all_vhosts"] == [{"id": vhost["id"], "server_name": "s3.example.com"}]


class TestBucketPurge:
    async def test_objects_that_could_not_be_deleted_are_reported(self, client, mock_nginx):
        pool = await create_pool(client, "pa")
        await create_member(client, pool["id"], "10.0.0.1:9000")
        outcomes = {"kept-refused": False, "kept-error": OSError("connection reset"), "gone": True}

        async def delete(address, bucket, key, **_):
            if isinstance(outcomes[key], Exception):
                raise outcomes[key]
            return outcomes[key]

        with (
            patch("nanio_orchestrator.api.pools.list_objects", new=AsyncMock(return_value=list(outcomes))),
            patch("nanio_orchestrator.api.pools.delete_object", new=AsyncMock(side_effect=delete)) as deleted,
        ):
            resp = await client.post(f"/api/pools/{pool['id']}/buckets/stale/purge")

        assert resp.status_code == 200
        assert resp.json() == {
            "ok": True,
            "pool_id": pool["id"],
            "bucket": "stale",
            "deleted": 1,
            "total": 3,
            "errors": ["kept-refused", "kept-error"],
        }
        assert [c.args for c in deleted.call_args_list] == [("10.0.0.1:9000", "stale", key) for key in outcomes]
        (entry,) = await audit(client, "pool_bucket_purge")
        assert json.loads(entry["after_json"]) == {"pool_id": pool["id"], "bucket": "stale", "deleted": 1, "total": 3}
