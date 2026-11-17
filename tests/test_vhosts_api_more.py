"""Vhosts and routes API: what each request leaves in the database, the nginx files and the
sidecars — and that a refused request leaves all three alone."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.api_state import audit, break_inserts, conf, managed_files, routes, sidecar, sql
from tests.conftest import create_member, create_pool, create_route, create_vhost


async def _cluster(client, default=True):
    pa = await create_pool(client, "pa")
    await create_member(client, pa["id"], "10.0.0.1:9000")
    pb = await create_pool(client, "pb")
    await create_member(client, pb["id"], "10.0.0.2:9000")
    vhost = await create_vhost(client, "s3.example.com", **({"default_pool_id": pa["id"]} if default else {}))
    return pa, pb, vhost


async def _http_pool(client, name, address, source=None):
    pool = await create_pool(client, name, pool_type="http", source_nanio_pool_id=source["id"] if source else None)
    await create_member(client, pool["id"], address, role="primary")
    return pool


async def _vhosts(client):
    return (await client.get("/api/vhosts")).json()


def _vhost_conf(tmp_dirs, name="s3.example.com"):
    return conf(tmp_dirs, "vhosts", name).read_text()


def _vhost_sidecar(tmp_dirs, name="s3.example.com"):
    return json.loads(sidecar(tmp_dirs, "vhosts", name).read_text())


def _location(content, prefix):
    """The body of `location <prefix> { … }`."""
    return content.split(f"location {prefix} {{")[1].split("\n    }")[0]


class TestCreateVhost:
    async def test_unknown_default_pool_is_refused(self, client, mock_nginx, tmp_dirs):
        resp = await client.post(
            "/api/vhosts", json={"server_name": "s3.example.com", "ssl": False, "default_pool_id": 999}
        )

        assert resp.status_code == 400
        assert resp.json()["detail"] == "Pool 999 not found"
        assert await _vhosts(client) == []
        assert managed_files(tmp_dirs) == {}

    async def test_database_failure_is_not_reported_as_a_duplicate_name(self, client, mock_nginx, tmp_dirs):
        await break_inserts("vhosts")

        resp = await client.post("/api/vhosts", json={"server_name": "s3.example.com", "ssl": False})

        assert resp.status_code == 500
        assert await _vhosts(client) == []
        assert managed_files(tmp_dirs) == {}

    async def test_without_a_default_pool_only_the_sidecar_is_written(self, client, mock_nginx, tmp_dirs):
        vhost = await create_vhost(client, "s3.example.com")

        assert sorted(managed_files(tmp_dirs)) == ["vhosts/s3.example.com.meta.json"]
        written = _vhost_sidecar(tmp_dirs)
        assert (written["vhost_id"], written["default_pool_id"], written["default_pool_name"]) == (
            vhost["id"],
            None,
            None,
        )
        assert await routes(client, vhost) == {}

    async def test_tls_extra_blocks_and_ip_rules_reach_the_config_and_the_sidecar(self, client, mock_nginx, tmp_dirs):
        pool = await create_pool(client, "pa")
        await create_member(client, pool["id"])
        blocks = [
            {"zone": "top", "content": "add_header X-Zone top;"},
            {"zone": "end", "content": "add_header X-Zone end;"},
        ]

        resp = await client.post(
            "/api/vhosts",
            json={
                "server_name": "s3.example.com",
                "ssl_cert_path": "/etc/ssl/s3/fullchain.pem",
                "ssl_key_path": "/etc/ssl/s3/privkey.pem",
                "default_pool_id": pool["id"],
                "extra_blocks": blocks,
                "ip_rule_mode": "allow",
                "ip_rule_ips": ["10.0.0.0/8", "192.168.1.7"],
            },
        )

        assert resp.status_code == 201
        vhost = resp.json()
        assert vhost["extra_blocks"] == blocks
        assert (vhost["ip_rule_mode"], vhost["ip_rule_ips"]) == ("allow", ["10.0.0.0/8", "192.168.1.7"])
        assert (await client.get(f"/api/vhosts/{vhost['id']}")).json() == vhost

        content = _vhost_conf(tmp_dirs)
        assert "listen 443 ssl;" in content
        assert "ssl_certificate     /etc/ssl/s3/fullchain.pem;" in content
        assert "ssl_certificate_key /etc/ssl/s3/privkey.pem;" in content
        assert "allow 10.0.0.0/8;\n    allow 192.168.1.7;\n    deny all;" in content
        assert content.index("X-Zone top") < content.index("location / {") < content.index("X-Zone end")
        assert "proxy_pass         http://pa;" in _location(content, "/")

        written = _vhost_sidecar(tmp_dirs)
        assert (written["default_pool_id"], written["default_pool_name"]) == (pool["id"], "pa")
        assert json.loads(written["extra_blocks_json"]) == blocks
        assert (written["ip_rule_mode"], json.loads(written["ip_rule_ips_json"])) == (
            "allow",
            ["10.0.0.0/8", "192.168.1.7"],
        )
        assert await routes(client, vhost) == {"/": "pa"}


class TestReadVhost:
    async def test_unknown_vhost(self, client):
        assert (await client.get("/api/vhosts/999")).status_code == 404
        assert (await client.get("/api/vhosts/999/routes")).status_code == 404
        assert (await client.get("/api/vhosts/999/preview")).status_code == 404

    async def test_preview_renders_the_config_without_writing_it(self, client, mock_nginx, tmp_dirs):
        vhost = await create_vhost(client, "s3.example.com", listen_port=8080)
        files = managed_files(tmp_dirs)

        resp = await client.get(f"/api/vhosts/{vhost['id']}/preview")

        assert resp.status_code == 200
        assert resp.json()["filepath"] == str(conf(tmp_dirs, "vhosts", "s3.example.com"))
        assert "listen 8080;" in resp.json()["content"]
        assert "server_name s3.example.com;" in resp.json()["content"]
        assert managed_files(tmp_dirs) == files
        mock_nginx["test_config"].assert_not_awaited()


class TestUpdateVhost:
    async def test_unknown_vhost(self, client):
        assert (await client.put("/api/vhosts/999", json={"listen_port": 8080})).status_code == 404

    async def test_nothing_to_change_returns_the_vhost_untouched(self, client, mock_nginx, tmp_dirs):
        _pa, _pb, vhost = await _cluster(client)
        files = managed_files(tmp_dirs)

        resp = await client.put(f"/api/vhosts/{vhost['id']}", json={"listen_port": None})

        assert resp.status_code == 200
        assert resp.json() == vhost
        assert managed_files(tmp_dirs) == files
        assert await audit(client, "update") == []

    async def test_tls_cannot_be_enabled_without_a_certificate(self, client, mock_nginx, tmp_dirs):
        _pa, _pb, vhost = await _cluster(client)
        files = managed_files(tmp_dirs)

        missing_both = await client.put(f"/api/vhosts/{vhost['id']}", json={"ssl": True})
        missing_key = await client.put(f"/api/vhosts/{vhost['id']}", json={"ssl": True, "ssl_cert_path": "/c.pem"})

        assert (missing_both.status_code, missing_key.status_code) == (400, 400)
        assert "ssl_cert_path and ssl_key_path are required" in missing_both.json()["detail"]
        assert (await client.get(f"/api/vhosts/{vhost['id']}")).json() == vhost
        assert managed_files(tmp_dirs) == files

    async def test_tls_port_and_enabled_flag_are_saved_and_written(self, client, mock_nginx, tmp_dirs):
        _pa, _pb, vhost = await _cluster(client)
        assert "listen 443;" in _vhost_conf(tmp_dirs)

        resp = await client.put(
            f"/api/vhosts/{vhost['id']}",
            json={
                "ssl": True,
                "ssl_cert_path": "/c.pem",
                "ssl_key_path": "/k.pem",
                "listen_port": 8443,
                "enabled": False,
            },
        )

        assert resp.status_code == 200
        saved = (await client.get(f"/api/vhosts/{vhost['id']}")).json()
        assert resp.json() == saved
        assert (saved["ssl"], saved["listen_port"], saved["enabled"]) == (True, 8443, False)
        content = _vhost_conf(tmp_dirs)
        assert "listen 8443 ssl;" in content and "ssl_certificate     /c.pem;" in content

        resp = await client.put(f"/api/vhosts/{vhost['id']}", json={"ssl": False, "enabled": True})

        assert (resp.json()["ssl"], resp.json()["enabled"]) == (False, True)
        assert "listen 8443;" in _vhost_conf(tmp_dirs) and "ssl_certificate" not in _vhost_conf(tmp_dirs)

    @pytest.mark.parametrize("pool_id", [999, 0])
    async def test_unknown_default_pool_is_refused(self, client, mock_nginx, tmp_dirs, pool_id):
        _pa, _pb, vhost = await _cluster(client)
        files = managed_files(tmp_dirs)

        resp = await client.put(f"/api/vhosts/{vhost['id']}", json={"default_pool_id": pool_id})

        assert resp.status_code == 400
        assert resp.json()["detail"] == f"Pool {pool_id} not found"
        assert (await client.get(f"/api/vhosts/{vhost['id']}")).json() == vhost
        assert await routes(client, vhost) == {"/": "pa"}
        assert managed_files(tmp_dirs) == files

    async def test_default_pool_of_another_type_than_the_routes_is_refused(self, client, mock_nginx, tmp_dirs):
        pa, _pb, vhost = await _cluster(client, default=False)
        await create_route(client, vhost["id"], "photos", pa["id"])
        web = await _http_pool(client, "web", "10.0.1.1:80")
        files = managed_files(tmp_dirs)

        resp = await client.put(f"/api/vhosts/{vhost['id']}", json={"default_pool_id": web["id"]})

        assert resp.status_code == 400
        assert "existing route uses pool 'pa' of type 'nanio'" in resp.json()["detail"]
        assert (await client.get(f"/api/vhosts/{vhost['id']}")).json()["default_pool_id"] is None
        assert await routes(client, vhost) == {"/photos/": "pa"}
        assert managed_files(tmp_dirs) == files

    async def test_new_default_pool_repoints_the_catch_all_route(self, client, mock_nginx, tmp_dirs):
        pa, pb, vhost = await _cluster(client)
        await create_route(client, vhost["id"], "photos", pa["id"])

        resp = await client.put(f"/api/vhosts/{vhost['id']}", json={"default_pool_id": pb["id"]})

        assert resp.status_code == 200
        assert resp.json()["default_pool_id"] == pb["id"]
        assert await routes(client, vhost) == {"/photos/": "pa", "/": "pb"}
        content = _vhost_conf(tmp_dirs)
        assert "proxy_pass         http://pb;" in _location(content, "/")
        assert "proxy_pass         http://pa;" in _location(content, "/photos/")
        written = _vhost_sidecar(tmp_dirs)
        assert (written["default_pool_id"], written["default_pool_name"]) == (pb["id"], "pb")

    async def test_first_default_pool_creates_the_catch_all_route(self, client, mock_nginx, tmp_dirs):
        pa, _pb, vhost = await _cluster(client, default=False)

        resp = await client.put(f"/api/vhosts/{vhost['id']}", json={"default_pool_id": pa["id"]})

        assert resp.status_code == 200
        assert await routes(client, vhost) == {"/": "pa"}
        assert "proxy_pass         http://pa;" in _location(_vhost_conf(tmp_dirs), "/")
        assert _vhost_sidecar(tmp_dirs)["default_pool_name"] == "pa"

    async def test_null_default_pool_removes_the_catch_all_route(self, client, mock_nginx, tmp_dirs):
        _pa, pb, vhost = await _cluster(client)
        await create_route(client, vhost["id"], "photos", pb["id"])

        resp = await client.put(f"/api/vhosts/{vhost['id']}", json={"default_pool_id": None})

        assert resp.status_code == 200
        assert resp.json()["default_pool_id"] is None
        assert (await client.get(f"/api/vhosts/{vhost['id']}")).json()["default_pool_id"] is None
        assert await routes(client, vhost) == {"/photos/": "pb"}
        content = _vhost_conf(tmp_dirs)
        assert "location / {" not in content and "location /photos/ {" in content
        written = _vhost_sidecar(tmp_dirs)
        assert (written["default_pool_id"], written["default_pool_name"]) == (None, None)

    async def test_null_default_pool_on_a_vhost_that_has_none_changes_no_route(self, client, mock_nginx, tmp_dirs):
        _pa, pb, vhost = await _cluster(client, default=False)
        await create_route(client, vhost["id"], "photos", pb["id"])

        resp = await client.put(f"/api/vhosts/{vhost['id']}", json={"default_pool_id": None})

        assert resp.status_code == 200
        assert resp.json()["default_pool_id"] is None
        assert await routes(client, vhost) == {"/photos/": "pb"}
        assert "location / {" not in _vhost_conf(tmp_dirs)

    async def test_extra_blocks_are_replaced_and_an_empty_list_removes_them(self, client, mock_nginx, tmp_dirs):
        _pa, _pb, vhost = await _cluster(client)
        blocks = [{"zone": "proxy", "content": "proxy_read_timeout 300s;"}]

        resp = await client.put(f"/api/vhosts/{vhost['id']}", json={"extra_blocks": blocks})

        assert resp.status_code == 200
        assert resp.json()["extra_blocks"] == blocks
        assert "proxy_read_timeout 300s;" in _vhost_conf(tmp_dirs)
        assert json.loads(_vhost_sidecar(tmp_dirs)["extra_blocks_json"]) == blocks

        resp = await client.put(f"/api/vhosts/{vhost['id']}", json={"extra_blocks": []})

        assert resp.json()["extra_blocks"] is None
        assert "proxy_read_timeout" not in _vhost_conf(tmp_dirs)
        assert _vhost_sidecar(tmp_dirs)["extra_blocks_json"] is None

    async def test_ip_rules_are_written_and_null_removes_them(self, client, mock_nginx, tmp_dirs):
        _pa, _pb, vhost = await _cluster(client)

        resp = await client.put(
            f"/api/vhosts/{vhost['id']}", json={"ip_rule_mode": "deny", "ip_rule_ips": ["203.0.113.9"]}
        )

        assert resp.status_code == 200
        assert (resp.json()["ip_rule_mode"], resp.json()["ip_rule_ips"]) == ("deny", ["203.0.113.9"])
        assert "deny 203.0.113.9;" in _vhost_conf(tmp_dirs) and "deny all;" not in _vhost_conf(tmp_dirs)
        written = _vhost_sidecar(tmp_dirs)
        assert (written["ip_rule_mode"], json.loads(written["ip_rule_ips_json"])) == ("deny", ["203.0.113.9"])

        resp = await client.put(f"/api/vhosts/{vhost['id']}", json={"ip_rule_mode": None, "ip_rule_ips": None})

        assert resp.status_code == 200
        saved = (await client.get(f"/api/vhosts/{vhost['id']}")).json()
        assert (saved["ip_rule_mode"], saved["ip_rule_ips"]) == (None, None)
        assert "203.0.113.9" not in _vhost_conf(tmp_dirs)
        written = _vhost_sidecar(tmp_dirs)
        assert (written["ip_rule_mode"], written["ip_rule_ips_json"]) == (None, None)


class TestDeleteVhost:
    async def test_unknown_vhost(self, client):
        assert (await client.delete("/api/vhosts/999")).status_code == 404

    async def test_removes_everything_that_belonged_to_the_vhost(self, client, mock_nginx, tmp_dirs):
        pa, pb, vhost = await _cluster(client)
        route = await create_route(client, vhost["id"], "photos", pb["id"])
        await sql(
            """INSERT INTO migrations (vhost_id, bucket, src_pool_id, dst_pool_id, phase, route_id)
               VALUES (?, 'photos', ?, ?, 'done', ?)""",
            (vhost["id"], pa["id"], pb["id"], route["id"]),
        )
        await sql(
            "INSERT INTO bucket_sync (vhost_id, bucket, status, routed_pool_id) VALUES (?, 'photos', 'routed', ?)",
            (vhost["id"], pb["id"]),
        )
        other = await create_vhost(client, "other.example.com", default_pool_id=pa["id"])

        resp = await client.delete(f"/api/vhosts/{vhost['id']}")

        assert resp.status_code == 204
        assert [v["server_name"] for v in await _vhosts(client)] == ["other.example.com"]
        assert sorted(f for f in managed_files(tmp_dirs) if f.startswith("vhosts/")) == [
            "vhosts/other.example.com.conf",
            "vhosts/other.example.com.meta.json",
        ]
        assert await sql("SELECT vhost_id FROM routes") == [{"vhost_id": other["id"]}]
        assert await sql("SELECT id FROM migrations") == []
        assert await sql("SELECT id FROM bucket_sync") == []
        assert await sql("SELECT path FROM config_files WHERE path LIKE '%s3.example.com.conf'") == []
        assert [e["entity_id"] for e in await audit(client, "delete")] == [vhost["id"]]
        # nothing refers to the pool any more
        assert (await client.delete(f"/api/pools/{pb['id']}")).status_code == 204


class TestCreateRoute:
    async def _refused(self, client, tmp_dirs, vhost, body, status):
        before = (await routes(client, vhost), managed_files(tmp_dirs))
        resp = await client.post(f"/api/vhosts/{vhost['id']}/routes", json=body)
        assert resp.status_code == status, resp.text
        assert (await routes(client, vhost), managed_files(tmp_dirs)) == before
        return resp.json()["detail"] if status != 500 else None

    async def test_catch_all_route_cannot_be_created_by_hand(self, client, mock_nginx, tmp_dirs):
        _pa, pb, vhost = await _cluster(client)
        before = (await routes(client, vhost), managed_files(tmp_dirs))

        resp = await client.post(f"/api/vhosts/{vhost['id']}/routes", json={"path_prefix": "/", "pool_id": pb["id"]})

        assert resp.status_code == 422
        assert (await routes(client, vhost), managed_files(tmp_dirs)) == before

    async def test_unknown_vhost(self, client, mock_nginx):
        pool = await create_pool(client, "pa")

        resp = await client.post("/api/vhosts/999/routes", json={"path_prefix": "/photos/", "pool_id": pool["id"]})

        assert resp.status_code == 404
        assert await sql("SELECT id FROM routes") == []

    async def test_unknown_pool(self, client, mock_nginx, tmp_dirs):
        _pa, _pb, vhost = await _cluster(client)

        detail = await self._refused(client, tmp_dirs, vhost, {"path_prefix": "/photos/", "pool_id": 999}, 400)

        assert detail == "Pool 999 not found"

    async def test_pool_of_another_type_than_the_vhost_is_refused(self, client, mock_nginx, tmp_dirs):
        _pa, _pb, vhost = await _cluster(client)
        web = await _http_pool(client, "web", "10.0.1.1:80")

        detail = await self._refused(client, tmp_dirs, vhost, {"path_prefix": "/photos/", "pool_id": web["id"]}, 400)

        assert "cannot mix pool types" in detail

    async def test_same_prefix_twice_is_a_conflict(self, client, mock_nginx, tmp_dirs):
        pa, pb, vhost = await _cluster(client)
        await create_route(client, vhost["id"], "photos", pb["id"])

        detail = await self._refused(client, tmp_dirs, vhost, {"path_prefix": "/photos/", "pool_id": pa["id"]}, 409)

        assert "'/photos/' already exists" in detail
        assert await routes(client, vhost) == {"/photos/": "pb", "/": "pa"}

    async def test_database_failure_is_not_reported_as_a_duplicate_prefix(self, client, mock_nginx, tmp_dirs):
        _pa, pb, vhost = await _cluster(client)
        await break_inserts("routes")

        await self._refused(client, tmp_dirs, vhost, {"path_prefix": "/photos/", "pool_id": pb["id"]}, 500)

    async def test_vhost_without_a_default_pool_takes_a_bucket_route(self, client, mock_nginx, tmp_dirs):
        _pa, pb, vhost = await _cluster(client, default=False)

        resp = await client.post(
            f"/api/vhosts/{vhost['id']}/routes", json={"path_prefix": "/photos/", "pool_id": pb["id"]}
        )

        assert resp.status_code == 201
        created = resp.json()
        assert (created["bucket"], created["bucket_provisioned"], created["default_pool_id"], created["warnings"]) == (
            "photos",
            False,
            None,
            [],
        )
        assert await routes(client, vhost) == {"/photos/": "pb"}
        assert "proxy_pass         http://pb;" in _location(_vhost_conf(tmp_dirs), "/photos/")
        (entry,) = [e for e in await audit(client, "create") if e["entity_type"] == "route"]
        assert entry["entity_id"] == created["id"]

    async def test_disabled_route_is_saved_but_not_served(self, client, mock_nginx, tmp_dirs):
        _pa, pb, vhost = await _cluster(client)

        resp = await client.post(
            f"/api/vhosts/{vhost['id']}/routes", json={"path_prefix": "/photos/", "pool_id": pb["id"], "enabled": False}
        )

        assert resp.status_code == 201
        assert resp.json()["enabled"] == 0
        assert await routes(client, vhost) == {"/photos/": "pb", "/": "pa"}
        assert "location /photos/ {" not in _vhost_conf(tmp_dirs)


class TestCreateSubfolderRoute:
    async def _refused(self, client, tmp_dirs, mock_s3, vhost, pool, status, **extra):
        before = (await routes(client, vhost), managed_files(tmp_dirs))
        resp = await client.post(
            f"/api/vhosts/{vhost['id']}/routes", json={"path_prefix": "/photos/2025/", "pool_id": pool["id"], **extra}
        )
        assert resp.status_code == status, resp.text
        assert (await routes(client, vhost), managed_files(tmp_dirs)) == before
        mock_s3["create_bucket"].assert_not_awaited()
        mock_s3["put_object"].assert_not_awaited()
        return resp.json()["detail"]

    async def test_refused_when_the_vhost_does_not_serve_the_bucket(self, client, mock_nginx, mock_s3, tmp_dirs):
        _pa, pb, vhost = await _cluster(client, default=False)

        detail = await self._refused(client, tmp_dirs, mock_s3, vhost, pb, 400)

        assert "Bucket 'photos' is not served by this vhost" in detail

    async def test_refused_when_the_bucket_is_served_from_plain_files(self, client, mock_nginx, mock_s3, tmp_dirs):
        _pa, pb, vhost = await _cluster(client, default=False)
        web = await _http_pool(client, "web", "10.0.1.1:80")
        await create_route(client, vhost["id"], "photos", web["id"])

        detail = await self._refused(client, tmp_dirs, mock_s3, vhost, pb, 400)

        assert detail == "Bucket 'photos' is served by pool 'web', which is not an S3 pool."

    async def test_refused_when_the_bucket_pool_has_no_member_to_ask(self, client, mock_nginx, mock_s3, tmp_dirs):
        pa, pb, vhost = await _cluster(client)
        (member,) = (await client.get(f"/api/pools/{pa['id']}/members")).json()
        await client.put(f"/api/pools/{pa['id']}/members/{member['id']}", json={"enabled": False})

        detail = await self._refused(client, tmp_dirs, mock_s3, vhost, pb, 400)

        assert detail == "Pool 'pa' has no enabled member to talk to."
        mock_s3["folder_has_objects"].assert_not_awaited()

    async def test_refused_when_the_folder_pool_has_no_member(self, client, mock_nginx, mock_s3, tmp_dirs):
        _pa, _pb, vhost = await _cluster(client)
        empty = await create_pool(client, "pc")

        detail = await self._refused(client, tmp_dirs, mock_s3, vhost, empty, 400)

        assert detail == "Pool 'pc' has no enabled member to talk to."
        mock_s3["folder_has_objects"].assert_not_awaited()

    async def test_allow_orphan_creates_the_route_over_existing_objects(self, client, mock_nginx, mock_s3, tmp_dirs):
        _pa, pb, vhost = await _cluster(client)
        mock_s3["folder_has_objects"].return_value = True

        resp = await client.post(
            f"/api/vhosts/{vhost['id']}/routes",
            json={"path_prefix": "/photos/2025/", "pool_id": pb["id"], "allow_orphan": True},
        )

        assert resp.status_code == 201
        assert resp.json()["bucket_provisioned"] is True
        mock_s3["folder_has_objects"].assert_not_awaited()
        assert mock_s3["create_bucket"].call_args.args == ("10.0.0.2:9000", "photos")
        assert await routes(client, vhost) == {"/photos/2025/": "pb", "/": "pa"}
        assert "proxy_pass         http://pb;" in _location(_vhost_conf(tmp_dirs), "/photos/2025/")

    async def test_folder_route_is_not_saved_when_nginx_refuses_the_http_vhost(
        self, client, mock_nginx, mock_s3, tmp_dirs
    ):
        """The folder route and the http routes that follow it are one change: nginx judges the
        vhosts together, and a refusal leaves the database and every file as they were."""
        pa, pb, vhost = await _cluster(client)
        web_a = await _http_pool(client, "web-a", "10.0.1.1:80", source=pa)
        await _http_pool(client, "web-b", "10.0.1.2:80", source=pb)
        cdn = await create_vhost(client, "cdn.example.com", default_pool_id=web_a["id"])
        await create_route(client, cdn["id"], "photos", web_a["id"])
        before = (await routes(client, vhost), await routes(client, cdn), managed_files(tmp_dirs))
        mock_nginx["test_config"].return_value = MagicMock(ok=False, output="[emerg] cdn is broken")

        resp = await client.post(
            f"/api/vhosts/{vhost['id']}/routes", json={"path_prefix": "/photos/2025/", "pool_id": pb["id"]}
        )

        assert resp.status_code == 422
        assert "cdn is broken" in resp.json()["detail"]
        assert (await routes(client, vhost), await routes(client, cdn), managed_files(tmp_dirs)) == before


class TestUpdateRoute:
    async def _route(self, client, vhost, prefix):
        return next(
            r for r in (await client.get(f"/api/vhosts/{vhost['id']}/routes")).json() if r["path_prefix"] == prefix
        )

    async def _refused(self, client, tmp_dirs, vhost, route, body, status):
        before = (await routes(client, vhost), managed_files(tmp_dirs))
        resp = await client.put(f"/api/vhosts/{vhost['id']}/routes/{route['id']}", json=body)
        assert resp.status_code == status, resp.text
        assert (await routes(client, vhost), managed_files(tmp_dirs)) == before
        return resp.json()["detail"]

    async def test_unknown_route_and_route_of_another_vhost(self, client, mock_nginx, tmp_dirs):
        _pa, pb, vhost = await _cluster(client)
        route = await create_route(client, vhost["id"], "photos", pb["id"])
        other = await create_vhost(client, "other.example.com")
        files = managed_files(tmp_dirs)

        unknown = await client.put(f"/api/vhosts/{vhost['id']}/routes/999", json={"enabled": False})
        foreign = await client.put(f"/api/vhosts/{other['id']}/routes/{route['id']}", json={"enabled": False})

        assert (unknown.status_code, foreign.status_code) == (404, 404)
        assert (await self._route(client, vhost, "/photos/"))["enabled"] is True
        assert managed_files(tmp_dirs) == files

    async def test_catch_all_route_follows_the_vhost_default_pool_only(self, client, mock_nginx, tmp_dirs):
        _pa, pb, vhost = await _cluster(client)
        catch_all = await self._route(client, vhost, "/")

        detail = await self._refused(client, tmp_dirs, vhost, catch_all, {"pool_id": pb["id"]}, 400)

        assert "update the vhost to change it" in detail

    async def test_path_cannot_be_changed(self, client, mock_nginx, tmp_dirs):
        _pa, pb, vhost = await _cluster(client)
        route = await create_route(client, vhost["id"], "photos", pb["id"])

        detail = await self._refused(client, tmp_dirs, vhost, route, {"path_prefix": "/videos/"}, 400)

        assert detail == "A route's path cannot be changed: delete it and create the new one."

    async def test_sending_the_same_path_back_is_accepted(self, client, mock_nginx, tmp_dirs):
        _pa, pb, vhost = await _cluster(client)
        route = await create_route(client, vhost["id"], "photos", pb["id"])

        resp = await client.put(
            f"/api/vhosts/{vhost['id']}/routes/{route['id']}", json={"path_prefix": "/photos/", "enabled": True}
        )

        assert resp.status_code == 200
        assert await routes(client, vhost) == {"/photos/": "pb", "/": "pa"}
        assert "proxy_pass         http://pb;" in _location(_vhost_conf(tmp_dirs), "/photos/")

    async def test_nothing_to_change_returns_the_route_untouched(self, client, mock_nginx, tmp_dirs):
        _pa, pb, vhost = await _cluster(client)
        route = await create_route(client, vhost["id"], "photos", pb["id"])
        files = managed_files(tmp_dirs)

        resp = await client.put(f"/api/vhosts/{vhost['id']}/routes/{route['id']}", json={})

        assert resp.status_code == 200
        assert (resp.json()["id"], resp.json()["pool_name"], resp.json()["path_prefix"]) == (
            route["id"],
            "pb",
            "/photos/",
        )
        assert managed_files(tmp_dirs) == files
        assert await audit(client, "update") == []

    async def test_unknown_pool_is_refused(self, client, mock_nginx, tmp_dirs):
        _pa, pb, vhost = await _cluster(client)
        route = await create_route(client, vhost["id"], "photos", pb["id"])

        detail = await self._refused(client, tmp_dirs, vhost, route, {"pool_id": 999}, 400)

        assert detail == "Pool 999 not found"

    async def test_disabled_route_leaves_the_config_and_comes_back_when_enabled(self, client, mock_nginx, tmp_dirs):
        _pa, pb, vhost = await _cluster(client)
        route = await create_route(client, vhost["id"], "photos", pb["id"])
        url = f"/api/vhosts/{vhost['id']}/routes/{route['id']}"

        resp = await client.put(url, json={"enabled": False})

        assert resp.status_code == 200
        assert resp.json()["enabled"] is False
        assert (await self._route(client, vhost, "/photos/"))["enabled"] is False
        assert "location /photos/ {" not in _vhost_conf(tmp_dirs)

        resp = await client.put(url, json={"enabled": True})

        assert resp.json()["enabled"] is True
        assert "location /photos/ {" in _vhost_conf(tmp_dirs)

    async def test_folder_route_of_a_plain_file_vhost_is_repointed_without_asking_any_pool(
        self, client, mock_nginx, tmp_dirs
    ):
        web_a = await _http_pool(client, "web-a", "10.0.1.1:80")
        web_b = await _http_pool(client, "web-b", "10.0.1.2:80")
        cdn = await create_vhost(client, "cdn.example.com", default_pool_id=web_a["id"])
        await create_route(client, cdn["id"], "photos", web_a["id"])
        folder = await create_route(client, cdn["id"], "photos/2025", web_a["id"])
        count = AsyncMock(return_value=5)

        with patch("nanio_orchestrator.api.vhosts.count_objects", new=count):
            resp = await client.put(f"/api/vhosts/{cdn['id']}/routes/{folder['id']}", json={"pool_id": web_b["id"]})

        assert resp.status_code == 200
        assert (resp.json()["pool_name"], resp.json()["migration_id"]) == ("web-b", None)
        count.assert_not_awaited()
        assert await routes(client, cdn) == {"/photos/2025/": "web-b", "/photos/": "web-a", "/": "web-a"}
        assert "proxy_pass         http://web-b;" in _location(
            _vhost_conf(tmp_dirs, "cdn.example.com"), "/photos/2025/"
        )
        assert await sql("SELECT id FROM migrations") == []

    async def test_route_cannot_be_pointed_at_a_pool_of_another_type(self, client, mock_nginx, tmp_dirs):
        pa, _pb, vhost = await _cluster(client)
        web = await _http_pool(client, "web", "10.0.1.1:80", source=pa)
        route = await create_route(client, vhost["id"], "photos", pa["id"])

        detail = await self._refused(client, tmp_dirs, vhost, route, {"pool_id": web["id"]}, 400)

        assert "cannot mix pool types" in detail

    async def test_route_is_repointed_when_its_pool_has_no_member_left_to_ask(self, client, mock_nginx, tmp_dirs):
        _pa, pb, vhost = await _cluster(client)
        empty = await create_pool(client, "pc")
        route = await create_route(client, vhost["id"], "photos", empty["id"])
        count = AsyncMock(return_value=5)

        with patch("nanio_orchestrator.api.vhosts.count_objects", new=count):
            resp = await client.put(f"/api/vhosts/{vhost['id']}/routes/{route['id']}", json={"pool_id": pb["id"]})

        assert resp.status_code == 200
        count.assert_not_awaited()
        assert await routes(client, vhost) == {"/photos/": "pb", "/": "pa"}
        assert "proxy_pass         http://pb;" in _location(_vhost_conf(tmp_dirs), "/photos/")
        assert await sql("SELECT id FROM migrations") == []

    async def test_route_is_repointed_when_its_pool_does_not_answer(self, client, mock_nginx, tmp_dirs):
        pa, pb, vhost = await _cluster(client)
        route = await create_route(client, vhost["id"], "photos", pa["id"])
        count = AsyncMock(side_effect=OSError("connection refused"))

        with patch("nanio_orchestrator.api.vhosts.count_objects", new=count):
            resp = await client.put(f"/api/vhosts/{vhost['id']}/routes/{route['id']}", json={"pool_id": pb["id"]})

        assert resp.status_code == 200
        assert count.call_args.args == ("10.0.0.1:9000", "photos")
        assert (resp.json()["pool_name"], resp.json()["migration_id"]) == ("pb", None)
        assert await routes(client, vhost) == {"/photos/": "pb", "/": "pa"}
        assert await sql("SELECT id FROM migrations") == []

    async def test_route_stays_where_the_objects_are_when_no_migration_can_start(self, client, mock_nginx, tmp_dirs):
        pa, pb, vhost = await _cluster(client)
        route = await create_route(client, vhost["id"], "photos", pa["id"])
        start = AsyncMock(side_effect=RuntimeError("Max parallel migrations reached (2)."))

        with (
            patch("nanio_orchestrator.api.vhosts.count_objects", new=AsyncMock(return_value=5)),
            patch("nanio_orchestrator.migration_engine.start_migration", new=start),
        ):
            detail = await self._refused(client, tmp_dirs, vhost, route, {"pool_id": pb["id"]}, 429)

        assert detail == "Max parallel migrations reached (2)."
        assert start.call_args.kwargs == {
            "vhost_id": vhost["id"],
            "bucket": "photos",
            "src_pool_id": pa["id"],
            "dst_pool_id": pb["id"],
            "route_id": route["id"],
        }
        assert await routes(client, vhost) == {"/photos/": "pa", "/": "pa"}
        assert await audit(client, "update") == []


class TestDeleteRoute:
    async def _folder_route(self, client, vhost, pool):
        resp = await client.post(
            f"/api/vhosts/{vhost['id']}/routes", json={"path_prefix": "/photos/2025/", "pool_id": pool["id"]}
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    async def _refused(self, client, tmp_dirs, vhost, route, status):
        before = (await routes(client, vhost), managed_files(tmp_dirs))
        resp = await client.delete(f"/api/vhosts/{vhost['id']}/routes/{route['id']}")
        assert resp.status_code == status, resp.text
        assert (await routes(client, vhost), managed_files(tmp_dirs)) == before
        return resp.json()["detail"]

    async def test_unknown_route_and_route_of_another_vhost(self, client, mock_nginx, tmp_dirs):
        _pa, pb, vhost = await _cluster(client)
        route = await create_route(client, vhost["id"], "photos", pb["id"])
        other = await create_vhost(client, "other.example.com")
        files = managed_files(tmp_dirs)

        unknown = await client.delete(f"/api/vhosts/{vhost['id']}/routes/999")
        foreign = await client.delete(f"/api/vhosts/{other['id']}/routes/{route['id']}")

        assert (unknown.status_code, foreign.status_code) == (404, 404)
        assert await routes(client, vhost) == {"/photos/": "pb", "/": "pa"}
        assert managed_files(tmp_dirs) == files

    async def test_catch_all_route_cannot_be_deleted(self, client, mock_nginx, tmp_dirs):
        _pa, _pb, vhost = await _cluster(client)
        catch_all = (await client.get(f"/api/vhosts/{vhost['id']}/routes")).json()[0]

        detail = await self._refused(client, tmp_dirs, vhost, catch_all, 400)

        assert "set default_pool_id to null on the vhost" in detail

    async def test_finished_migration_of_the_route_is_kept_without_it(self, client, mock_nginx, tmp_dirs):
        pa, pb, vhost = await _cluster(client)
        route = await create_route(client, vhost["id"], "photos", pb["id"])
        await sql(
            """INSERT INTO migrations (vhost_id, bucket, src_pool_id, dst_pool_id, phase, route_id)
               VALUES (?, 'photos', ?, ?, 'done', ?)""",
            (vhost["id"], pa["id"], pb["id"], route["id"]),
        )

        resp = await client.delete(f"/api/vhosts/{vhost['id']}/routes/{route['id']}")

        assert resp.status_code == 204
        assert await routes(client, vhost) == {"/": "pa"}
        assert "location /photos/ {" not in _vhost_conf(tmp_dirs)
        assert await sql("SELECT phase, route_id FROM migrations") == [{"phase": "done", "route_id": None}]
        assert [e["entity_id"] for e in await audit(client, "delete")] == [route["id"]]

    async def test_folder_route_stays_when_its_pool_cannot_be_asked(self, client, mock_nginx, mock_s3, tmp_dirs):
        _pa, pb, vhost = await _cluster(client)
        route = await self._folder_route(client, vhost, pb)
        mock_s3["folder_has_objects"].side_effect = OSError("connection refused")

        detail = await self._refused(client, tmp_dirs, vhost, route, 502)

        assert detail == "Could not check the folder before deleting its route: connection refused"
        mock_s3["delete_object"].assert_not_awaited()

    async def test_folder_route_stays_when_its_pool_has_no_member_to_ask(self, client, mock_nginx, mock_s3, tmp_dirs):
        _pa, pb, vhost = await _cluster(client)
        route = await self._folder_route(client, vhost, pb)
        (member,) = (await client.get(f"/api/pools/{pb['id']}/members")).json()
        await client.put(f"/api/pools/{pb['id']}/members/{member['id']}", json={"enabled": False})
        mock_s3["folder_has_objects"].reset_mock()

        detail = await self._refused(client, tmp_dirs, vhost, route, 400)

        assert "no enabled member to ask" in detail and "allow_orphan=true" in detail
        mock_s3["folder_has_objects"].assert_not_awaited()
        mock_s3["delete_object"].assert_not_awaited()

        resp = await client.delete(f"/api/vhosts/{vhost['id']}/routes/{route['id']}?allow_orphan=true")

        assert resp.status_code == 204
        assert await routes(client, vhost) == {"/": "pa"}
        mock_s3["folder_has_objects"].assert_not_awaited()

    async def test_marker_is_left_alone_when_the_bucket_pool_has_no_member(self, client, mock_nginx, mock_s3, tmp_dirs):
        pa, pb, vhost = await _cluster(client)
        route = await self._folder_route(client, vhost, pb)
        (member,) = (await client.get(f"/api/pools/{pa['id']}/members")).json()
        await client.put(f"/api/pools/{pa['id']}/members/{member['id']}", json={"enabled": False})

        resp = await client.delete(f"/api/vhosts/{vhost['id']}/routes/{route['id']}")

        assert resp.status_code == 204
        assert mock_s3["folder_has_objects"].call_args.args == ("10.0.0.2:9000", "photos")
        mock_s3["delete_object"].assert_not_awaited()
        assert await routes(client, vhost) == {"/": "pa"}
        assert "location /photos/2025/ {" not in _vhost_conf(tmp_dirs)

    async def test_folder_route_of_a_bucket_the_vhost_no_longer_serves_can_be_removed(
        self, client, mock_nginx, mock_s3, tmp_dirs
    ):
        pa, pb, vhost = await _cluster(client, default=False)
        bucket_route = await create_route(client, vhost["id"], "photos", pa["id"])
        route = await self._folder_route(client, vhost, pb)
        assert (await client.delete(f"/api/vhosts/{vhost['id']}/routes/{bucket_route['id']}")).status_code == 204

        resp = await client.delete(f"/api/vhosts/{vhost['id']}/routes/{route['id']}")

        assert resp.status_code == 204
        mock_s3["delete_object"].assert_not_awaited()
        assert await routes(client, vhost) == {}
        assert "location" not in _vhost_conf(tmp_dirs)

    async def test_marker_that_cannot_be_removed_does_not_fail_the_request(self, client, mock_nginx, mock_s3, tmp_dirs):
        _pa, pb, vhost = await _cluster(client)
        route = await self._folder_route(client, vhost, pb)
        mock_s3["delete_object"].side_effect = OSError("connection refused")

        resp = await client.delete(f"/api/vhosts/{vhost['id']}/routes/{route['id']}")

        assert resp.status_code == 204
        assert mock_s3["delete_object"].call_args.args == ("10.0.0.1:9000", "photos", "2025/.nanio-shard")
        assert await routes(client, vhost) == {"/": "pa"}
        assert "location /photos/2025/ {" not in _vhost_conf(tmp_dirs)

    async def test_folder_route_of_a_plain_file_vhost_is_removed_without_asking_any_pool(
        self, client, mock_nginx, mock_s3, tmp_dirs
    ):
        web = await _http_pool(client, "web", "10.0.1.1:80")
        cdn = await create_vhost(client, "cdn.example.com", default_pool_id=web["id"])
        folder = await create_route(client, cdn["id"], "photos/2025", web["id"])

        resp = await client.delete(f"/api/vhosts/{cdn['id']}/routes/{folder['id']}")

        assert resp.status_code == 204
        mock_s3["folder_has_objects"].assert_not_awaited()
        mock_s3["delete_object"].assert_not_awaited()
        assert await routes(client, cdn) == {"/": "web"}
        assert "location /photos/2025/ {" not in _vhost_conf(tmp_dirs, "cdn.example.com")
