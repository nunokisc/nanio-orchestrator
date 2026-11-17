"""Moving one folder of a bucket to another pool, live."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import patch

import pytest

from tests.conftest import T0

from .test_pools import create_member, create_pool
from .test_vhosts import create_vhost

pytestmark = pytest.mark.usefixtures("set_secret", "ensure_migrations_dir")


async def _cluster(client, mock_s3):
    """Bucket `photos` lives on the default pool pa; pb is where a folder will go."""
    pa = await create_pool(client, "pa")
    await create_member(client, pa["id"], "10.0.0.1:9000")
    pb = await create_pool(client, "pb")
    await create_member(client, pb["id"], "10.0.0.2:9000")
    vhost = await create_vhost(client, "s3.example.com", default_pool_id=pa["id"])
    mock_s3["list_buckets"].return_value = [{"name": "photos", "created": "2025-01-01"}]
    await client.post(f"/api/vhosts/{vhost['id']}/buckets/sync")
    mock_s3["count_objects"].return_value = 5
    return pa, pb, vhost


async def _move(client, dst, folder="2025/", expect=201, **extra):
    resp = await client.post(
        "/api/migrations", json={"bucket": "photos", "folder": folder, "dst_pool_id": dst["id"], **extra}
    )
    assert resp.status_code == expect, resp.text
    if expect != 201:
        return resp.json()
    mig_id = resp.json()["id"]
    for _ in range(100):
        await asyncio.sleep(0.05)
        data = (await client.get(f"/api/migrations/{mig_id}")).json()
        if data["phase"] in ("done", "error", "cancelled"):
            return data
    raise AssertionError("migration did not finish")


async def _routes(client, vhost):
    return {r["path_prefix"]: r["pool_name"] for r in (await client.get(f"/api/vhosts/{vhost['id']}/routes")).json()}


def _conf(name="s3.example.com"):
    from nanio_orchestrator.config import get_settings

    return (get_settings().vhosts_dir / f"{name}.conf").read_text()


class TestCarveOut:
    """A folder that is still part of its bucket moves to another pool."""

    async def test_folder_ends_up_routed_to_the_destination(self, client, mock_nginx, mock_s3, rclone, mtimes):
        pa, pb, vhost = await _cluster(client, mock_s3)

        mig = await _move(client, pb, folder="2025")  # normalised to "2025/"

        assert mig["phase"] == "done", mig
        assert mig["folder"] == "2025/" and mig["src_pool_id"] == pa["id"] and mig["split_routing"] is False
        assert await _routes(client, vhost) == {"/": "pa", "/photos/2025/": "pb"}
        assert mig["orphaned_source_prefix"] == "/photos/2025/"
        assert "LIVE MIGRATION" not in _conf()

    async def test_only_the_folder_is_copied_and_counted(self, client, mock_nginx, mock_s3, rclone, mtimes):
        _, pb, _ = await _cluster(client, mock_s3)

        await _move(client, pb)

        for command in rclone.commands:
            assert command[2:4] == ["src:photos/2025", "dst:photos/2025"], command
        assert {c.kwargs.get("prefix") for c in mock_s3["count_objects"].call_args_list} == {"2025/"}
        assert mock_s3["src_bucket_has_objects"].call_args.kwargs["prefix"] == "2025/"

    async def test_bucket_and_marker_are_provided_for_the_folder(self, client, mock_nginx, mock_s3, rclone, mtimes):
        _, pb, _ = await _cluster(client, mock_s3)

        await _move(client, pb)

        assert ("10.0.0.2:9000", "photos") in [c.args for c in mock_s3["create_bucket"].call_args_list]
        assert ("10.0.0.1:9000", "photos", "2025/.nanio-shard") in [
            c.args[:3] for c in mock_s3["put_object"].call_args_list
        ]

    async def test_differing_keys_are_compared_with_their_full_name(self, client, mock_nginx, mock_s3, rclone):
        """rclone reports keys relative to the folder; the pools know them by their full key."""
        _, pb, _ = await _cluster(client, mock_s3)
        rclone.differ = ["a/overwritten.txt"]
        seen = []

        async def last_modified(address, bucket, key, **_):
            seen.append(key)
            return T0 + (timedelta(minutes=5) if address == "10.0.0.2:9000" else timedelta(0))

        with patch("nanio_orchestrator.migration_engine.object_last_modified", new=last_modified):
            mig = await _move(client, pb)

        assert mig["phase"] == "done", mig
        assert set(seen) == {"2025/a/overwritten.txt"}

    async def test_http_vhost_serving_the_bucket_follows(self, client, mock_nginx, mock_s3, rclone, mtimes):
        pa, pb, _ = await _cluster(client, mock_s3)
        for name, nanio_pool, address in (("web-a", pa, "10.0.1.1:80"), ("web-b", pb, "10.0.1.2:80")):
            web = (
                await client.post(
                    "/api/pools", json={"name": name, "type": "http", "source_nanio_pool_id": nanio_pool["id"]}
                )
            ).json()
            await client.post(f"/api/pools/{web['id']}/members", json={"address": address, "role": "primary"})
        web_a = next(p for p in (await client.get("/api/pools")).json() if p["name"] == "web-a")
        cdn = await create_vhost(client, "cdn.example.com", default_pool_id=web_a["id"])
        await client.post(f"/api/vhosts/{cdn['id']}/routes", json={"path_prefix": "/photos/", "pool_id": web_a["id"]})

        mig = await _move(client, pb)

        assert mig["phase"] == "done", mig
        assert (await _routes(client, cdn))["/photos/2025/"] == "web-b"


class TestSplit:
    async def _stuck(self, client, mock_s3, rclone):
        pa, pb, vhost = await _cluster(client, mock_s3)
        rclone.missing = ["never-arrived"]
        mig = await _move(client, pb)
        assert mig["phase"] == "error" and mig["split_routing"] is True
        rclone.missing = []
        return pa, pb, vhost, mig

    async def test_folder_is_split_while_the_rest_of_the_bucket_is_untouched(
        self, client, mock_nginx, mock_s3, rclone, mtimes
    ):
        await self._stuck(client, mock_s3, rclone)
        conf = _conf()

        assert "prefix:/photos/2025/ migration_src:pa migration_dst:pb" in conf
        folder = conf.split("location /photos/2025/ {")[1].split("\n    }")[0]
        assert "proxy_pass         http://pb;" in folder and "error_page 404 = @nanio_mig_" in folder
        # listings inside the folder come from the destination, like every other request for it
        assert '"~^2025(/|%2[Ff])" pb;' in conf
        # a multi-delete reaches the destination too (the source is the bucket's own pool)
        bucket_level = conf.split("location = /photos {")[1].split("\n    }")[0]
        assert "_multidelete;" in bucket_level and "mirror_request_body on;" in bucket_level
        assert "proxy_pass         http://pb$request_uri;" in conf

    async def test_one_move_at_a_time_per_bucket(self, client, mock_nginx, mock_s3, rclone, mtimes):
        pa, pb, vhost, stuck = await self._stuck(client, mock_s3, rclone)
        pc = await create_pool(client, "pc")
        await create_member(client, pc["id"], "10.0.0.3:9000")

        other_folder = await _move(client, pc, folder="2024/", expect=409)
        assert f"Migration {stuck['id']} is still moving folder '2025/'" in other_folder["detail"]
        elsewhere = await _move(client, pc, expect=409)
        assert "Only one move at a time" in elsewhere["detail"]

    async def test_same_move_takes_over_and_finishes(self, client, mock_nginx, mock_s3, rclone, mtimes):
        _, pb, vhost, stuck = await self._stuck(client, mock_s3, rclone)

        retry = await _move(client, pb)

        assert retry["phase"] == "done", retry
        assert (await client.get(f"/api/migrations/{stuck['id']}")).json()["split_routing"] is False
        assert await _routes(client, vhost) == {"/": "pa", "/photos/2025/": "pb"}

    async def test_split_survives_a_database_rebuild(self, client, mock_nginx, mock_s3, rclone, mtimes):
        """Route ids change in a rebuild: the split must find its route by path."""
        from nanio_orchestrator.nginx.generator import generate_vhost_config

        await self._stuck(client, mock_s3, rclone)
        # another route created later, so ids do not line up by accident after the rebuild
        resp = await client.post("/api/config/rebuild-from-disk?force=true")
        assert resp.status_code == 200, resp.text

        vhost_id = (await client.get("/api/vhosts")).json()[0]["id"]
        _, content = await generate_vhost_config(vhost_id)
        assert "prefix:/photos/2025/ migration_src:pa migration_dst:pb" in content
        assert "prefix:/ migration_src" not in content


class TestMoveBack:
    async def test_folder_returning_to_the_bucket_pool_loses_its_route(
        self, client, mock_nginx, mock_s3, rclone, mtimes
    ):
        pa, pb, vhost = await _cluster(client, mock_s3)
        mock_s3["folder_has_objects"].return_value = False
        resp = await client.post(
            f"/api/vhosts/{vhost['id']}/routes", json={"path_prefix": "/photos/2025/", "pool_id": pb["id"]}
        )
        assert resp.status_code == 201, resp.text

        mig = await _move(client, pa)

        assert mig["phase"] == "done", mig
        assert mig["src_pool_id"] == pb["id"]
        assert await _routes(client, vhost) == {"/": "pa"}
        assert "location /photos/2025/" not in _conf() and "map $arg_prefix" not in _conf()
        assert ("10.0.0.1:9000", "photos", "2025/.nanio-shard") in [
            c.args for c in mock_s3["delete_object"].call_args_list
        ]


class TestRefusals:
    async def test_empty_folder_has_nothing_to_migrate(self, client, mock_nginx, mock_s3, rclone):
        _, pb, vhost = await _cluster(client, mock_s3)
        mock_s3["src_bucket_has_objects"].return_value = False

        body = await _move(client, pb, expect=400)

        assert "nothing to migrate" in body["detail"]
        assert await _routes(client, vhost) == {"/": "pa"}  # no route left behind

    async def test_folder_already_on_the_destination(self, client, mock_nginx, mock_s3, rclone):
        pa, _, _ = await _cluster(client, mock_s3)
        assert "already on pool" in (await _move(client, pa, expect=400))["detail"]

    async def test_source_pool_must_agree_with_the_routes(self, client, mock_nginx, mock_s3, rclone):
        _, pb, _ = await _cluster(client, mock_s3)
        pc = await create_pool(client, "pc")
        await create_member(client, pc["id"], "10.0.0.3:9000")
        body = await _move(client, pb, expect=400, src_pool_id=pc["id"])
        assert "Source pool mismatch" in body["detail"]

    async def test_invalid_folder_name(self, client, mock_nginx, mock_s3, rclone):
        _, pb, _ = await _cluster(client, mock_s3)
        await _move(client, pb, folder="20 25;", expect=422)

    async def test_whole_bucket_still_needs_its_source_pool(self, client, mock_nginx, mock_s3, rclone):
        _, pb, _ = await _cluster(client, mock_s3)
        resp = await client.post("/api/migrations", json={"bucket": "photos", "dst_pool_id": pb["id"]})
        assert resp.status_code == 422
