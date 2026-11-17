"""Sub-folder routes: `/bucket/folder/` served by another pool."""

from __future__ import annotations

from pathlib import Path

from nanio_orchestrator.nginx.generator import render_vhost
from nanio_orchestrator.nginx.parser import parse_vhost_block

from .test_pools import create_member, create_pool
from .test_vhosts import create_vhost


def _route(i, prefix, pool, dst=None):
    return {"id": i, "path_prefix": prefix, "pool_name": pool, "enabled": 1, "migration_dst_pool_name": dst}


def _render(routes):
    return render_vhost({"id": 1, "server_name": "s3.example.com", "listen_port": 80, "ssl": 0}, routes)


def _block(content, opening):
    return content.split(opening)[1].split("\n    }")[0]


class TestGeneratedConfig:
    def test_listing_inside_the_folder_is_answered_by_its_pool(self):
        content = _render([_route(1, "/", "pa"), _route(2, "/photos/2025/", "pb")])

        assert "location /photos/2025/ {" in content
        assert "proxy_pass         http://pb;" in _block(content, "location /photos/2025/ {")

        var = content.split("map $arg_prefix ")[1].split(" {")[0]
        mapping = content.split(f"map $arg_prefix {var} {{")[1].split("}")[0]
        assert "default pa;" in mapping
        # raw and percent-encoded slash, case-sensitive on the folder name
        assert '"~^2025(/|%2[Ff])" pb;' in mapping

        # The bucket lives on the default pool: its bucket-level requests get their own locations
        for opening in ("location = /photos {", "location = /photos/ {"):
            assert f"proxy_pass         http://{var};" in _block(content, opening)

    def test_longest_folder_is_matched_first_and_names_are_escaped(self):
        content = _render(
            [_route(1, "/", "pa"), _route(2, "/logs/app.v2/", "pb"), _route(3, "/logs/app.v2/old@2024/", "pc")]
        )
        mapping = content.split("map $arg_prefix ")[1].split("}")[0]
        deep = mapping.index(r'"~^app\.v2(/|%2[Ff])old(@|%40)2024(/|%2[Ff])" pc;')
        shallow = mapping.index(r'"~^app\.v2(/|%2[Ff])" pb;')
        assert deep < shallow

    def test_multi_delete_reaches_every_pool_holding_a_folder(self):
        content = _render(
            [
                _route(1, "/", "pa"),
                _route(2, "/photos/2025/", "pb"),
                _route(3, "/photos/2026/", "pc"),
                _route(4, "/photos/2027/", "pb"),
            ]
        )
        exact = _block(content, "location = /photos {")
        assert exact.count("mirror /__nanio_shard_") == 2  # pb once, pc once
        assert "mirror_request_body on;" in exact
        mirror = content.split("location = /__nanio_shard_2_multidelete {")[1]
        assert "proxy_pass         http://pb$request_uri;" in mirror.split("proxy_http_version")[0]
        # object requests keep streaming: no mirror on the folder locations
        assert "mirror" not in _block(content, "location /photos/2025/ {")

    def test_bucket_with_its_own_route_uses_that_pool_as_default(self):
        content = _render([_route(1, "/", "pa"), _route(2, "/photos/", "pc"), _route(3, "/photos/2025/", "pb")])
        mapping = content.split("map $arg_prefix ")[1].split("}")[0]
        assert "default pc;" in mapping
        assert content.count("location = /photos {") == 1

    def test_bucket_being_migrated_lists_from_the_destination(self):
        content = _render(
            [_route(1, "/", "pa"), _route(2, "/photos/", "pc", dst="pd"), _route(3, "/photos/2025/", "pb")]
        )
        assert "default pd;" in content.split("map $arg_prefix ")[1].split("}")[0]
        exact = _block(content, "location = /photos {")
        assert "mirror /__nanio_mig_2_multidelete;" in exact and "mirror /__nanio_shard_3_multidelete;" in exact

    def test_http_routes_get_no_s3_handling(self):
        routes = [_route(1, "/", "web"), _route(2, "/photos/2025/", "web-b")]
        for route in routes:
            route["pool_type"] = "http"
        content = _render(routes)
        assert "map $arg_prefix" not in content and "location = " not in content
        assert "location /photos/2025/ {" in content

    def test_generated_file_parses_back_to_the_same_routes(self):
        content = _render([_route(1, "/", "pa"), _route(2, "/photos/2025/", "pb"), _route(3, "/logs/", "pc")])
        parsed = parse_vhost_block(content)
        assert parsed["server_name"] == "s3.example.com"
        assert sorted((r["path_prefix"], r["pool_name"]) for r in parsed["routes"]) == [
            ("/", "pa"),
            ("/logs/", "pc"),
            ("/photos/2025/", "pb"),
        ]


async def _cluster(client):
    pa = await create_pool(client, "pa")
    await create_member(client, pa["id"], "10.0.0.1:9000")
    pb = await create_pool(client, "pb")
    await create_member(client, pb["id"], "10.0.0.2:9000")
    vhost = await create_vhost(client, "s3.example.com", default_pool_id=pa["id"])
    return pa, pb, vhost


async def _add(client, vhost, pool, prefix="/photos/2025/", **extra):
    return await client.post(
        f"/api/vhosts/{vhost['id']}/routes", json={"path_prefix": prefix, "pool_id": pool["id"], **extra}
    )


def _calls(mock):
    return [c.args for c in mock.call_args_list]


class TestCreate:
    async def test_creates_bucket_on_folder_pool_and_marker_on_parent(self, client, mock_nginx, mock_s3):
        _pa, pb, vhost = await _cluster(client)

        resp = await _add(client, vhost, pb)

        assert resp.status_code == 201, resp.text
        assert resp.json()["bucket_provisioned"] is True and resp.json()["warnings"] == []
        assert _calls(mock_s3["create_bucket"]) == [("10.0.0.2:9000", "photos")]
        address, bucket, key, body = mock_s3["put_object"].call_args.args
        assert (address, bucket, key) == ("10.0.0.1:9000", "photos", "2025/.nanio-shard")
        assert b"pool 'pb'" in body

    async def test_refused_when_the_folder_already_has_objects(self, client, mock_nginx, mock_s3):
        _pa, pb, vhost = await _cluster(client)
        mock_s3["folder_has_objects"].return_value = True

        resp = await _add(client, vhost, pb)

        assert resp.status_code == 409 and "would hide them" in resp.json()["detail"]
        assert mock_s3["folder_has_objects"].call_args.kwargs["prefix"] == "2025/"
        mock_s3["create_bucket"].assert_not_called()
        assert len((await client.get(f"/api/vhosts/{vhost['id']}/routes")).json()) == 1  # only "/"

        assert (await _add(client, vhost, pb, allow_orphan=True)).status_code == 201

    async def test_refused_when_folder_pool_is_the_bucket_pool(self, client, mock_nginx, mock_s3):
        pa, _, vhost = await _cluster(client)
        resp = await _add(client, vhost, pa)
        assert resp.status_code == 400 and "already served by pool 'pa'" in resp.json()["detail"]

    async def test_nothing_changes_when_the_bucket_cannot_be_created(self, client, mock_nginx, mock_s3):
        _, pb, vhost = await _cluster(client)
        mock_s3["create_bucket"].return_value = (False, "HTTP 500")

        resp = await _add(client, vhost, pb)

        assert resp.status_code == 502 and "Nothing was changed" in resp.json()["detail"]
        assert len((await client.get(f"/api/vhosts/{vhost['id']}/routes")).json()) == 1

    async def test_unreachable_pool_is_an_error_not_a_guess(self, client, mock_nginx, mock_s3):
        _, pb, vhost = await _cluster(client)
        mock_s3["folder_has_objects"].side_effect = ConnectionError("refused")
        assert (await _add(client, vhost, pb)).status_code == 502

    async def test_marker_failure_is_a_warning(self, client, mock_nginx, mock_s3):
        _, pb, vhost = await _cluster(client)
        mock_s3["put_object"].return_value = False
        resp = await _add(client, vhost, pb)
        assert resp.status_code == 201
        assert "does not appear when the bucket is listed" in resp.json()["warnings"][0]

    async def test_whole_bucket_routes_are_untouched(self, client, mock_nginx, mock_s3):
        _, pb, vhost = await _cluster(client)
        resp = await _add(client, vhost, pb, prefix="/photos/")
        assert resp.status_code == 201 and resp.json()["bucket_provisioned"] is False
        mock_s3["create_bucket"].assert_not_called()
        mock_s3["put_object"].assert_not_called()


class TestDelete:
    async def test_refused_while_the_folder_has_objects(self, client, mock_nginx, mock_s3):
        _, pb, vhost = await _cluster(client)
        route = (await _add(client, vhost, pb)).json()
        mock_s3["folder_has_objects"].return_value = True

        resp = await client.delete(f"/api/vhosts/{vhost['id']}/routes/{route['id']}")
        assert resp.status_code == 409 and "become unreachable" in resp.json()["detail"]
        assert mock_s3["folder_has_objects"].call_args.args[0] == "10.0.0.2:9000"  # asked the folder's pool

        resp = await client.delete(f"/api/vhosts/{vhost['id']}/routes/{route['id']}?allow_orphan=true")
        assert resp.status_code == 204

    async def test_removes_route_and_marker(self, client, mock_nginx, mock_s3):
        _, pb, vhost = await _cluster(client)
        route = (await _add(client, vhost, pb)).json()

        resp = await client.delete(f"/api/vhosts/{vhost['id']}/routes/{route['id']}")

        assert resp.status_code == 204
        assert _calls(mock_s3["delete_object"]) == [("10.0.0.1:9000", "photos", "2025/.nanio-shard")]
        assert len((await client.get(f"/api/vhosts/{vhost['id']}/routes")).json()) == 1


class TestUpkeep:
    async def test_bucket_sync_rewrites_the_marker(self, client, mock_nginx, mock_s3):
        """A marker lost (e.g. the bucket was migrated to another pool) comes back by itself."""
        from nanio_orchestrator import bucket_sync

        _, pb, vhost = await _cluster(client)
        await _add(client, vhost, pb)
        mock_s3["put_object"].reset_mock()

        await bucket_sync.sync_vhost_buckets_once(vhost["id"])

        assert [c.args[:3] for c in mock_s3["put_object"].call_args_list] == [
            ("10.0.0.1:9000", "photos", "2025/.nanio-shard")
        ]

    async def test_survives_a_database_rebuild(self, client, mock_nginx, mock_s3, tmp_dirs):
        _, pb, vhost = await _cluster(client)
        await _add(client, vhost, pb)
        conf = Path(tmp_dirs["nginx_dir"]) / "vhosts" / "s3.example.com.conf"
        assert "map $arg_prefix" in conf.read_text()

        resp = await client.post("/api/config/rebuild-from-disk?force=true")
        assert resp.status_code == 200, resp.text

        vhost_id = (await client.get("/api/vhosts")).json()[0]["id"]
        routes = {r["path_prefix"]: r["pool_name"] for r in (await client.get(f"/api/vhosts/{vhost_id}/routes")).json()}
        assert routes == {"/": "pa", "/photos/2025/": "pb"}


class TestHttpVhostsFollow:
    async def _web(self, client, pa, pb, link_b=True):
        web_a = (
            await client.post("/api/pools", json={"name": "web-a", "type": "http", "source_nanio_pool_id": pa["id"]})
        ).json()
        await client.post(f"/api/pools/{web_a['id']}/members", json={"address": "10.0.1.1:80", "role": "primary"})
        if link_b:
            web_b = (
                await client.post(
                    "/api/pools", json={"name": "web-b", "type": "http", "source_nanio_pool_id": pb["id"]}
                )
            ).json()
            await client.post(f"/api/pools/{web_b['id']}/members", json={"address": "10.0.1.2:80", "role": "primary"})
        cdn = await create_vhost(client, "cdn.example.com", default_pool_id=web_a["id"])
        resp = await client.post(
            f"/api/vhosts/{cdn['id']}/routes", json={"path_prefix": "/photos/", "pool_id": web_a["id"]}
        )
        assert resp.status_code == 201, resp.text
        return cdn

    async def _cdn_routes(self, client, cdn):
        return {r["path_prefix"]: r["pool_name"] for r in (await client.get(f"/api/vhosts/{cdn['id']}/routes")).json()}

    async def test_folder_route_is_added_and_removed_on_the_http_vhost(self, client, mock_nginx, mock_s3, tmp_dirs):
        pa, pb, vhost = await _cluster(client)
        cdn = await self._web(client, pa, pb)

        route = (await _add(client, vhost, pb)).json()
        assert route["warnings"] == []
        assert (await self._cdn_routes(client, cdn))["/photos/2025/"] == "web-b"
        conf = (Path(tmp_dirs["nginx_dir"]) / "vhosts" / "cdn.example.com.conf").read_text()
        assert "location /photos/2025/ {" in conf and "map $arg_prefix" not in conf

        await client.delete(f"/api/vhosts/{vhost['id']}/routes/{route['id']}")
        assert "/photos/2025/" not in await self._cdn_routes(client, cdn)

    async def test_warns_when_no_http_pool_serves_the_folder_pool(self, client, mock_nginx, mock_s3):
        pa, pb, vhost = await _cluster(client)
        cdn = await self._web(client, pa, pb, link_b=False)

        route = (await _add(client, vhost, pb)).json()

        assert "cdn.example.com" in route["warnings"][0] and "404" in route["warnings"][0]
        assert "/photos/2025/" not in await self._cdn_routes(client, cdn)
