"""A change that nginx rejects is not saved: not in the files, not in the database.

Before, only the file was restored. The request still answered 2xx and the database kept the
change, so the panel showed a state nginx was not serving.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from .test_pools import create_member, create_pool
from .test_vhosts import create_vhost


def _refuse(mock_nginx, output="nginx: [emerg] something is wrong"):
    mock_nginx["test_config"].return_value = MagicMock(ok=False, output=output)


def _accept(mock_nginx):
    mock_nginx["test_config"].return_value = MagicMock(ok=True, output="syntax ok")


async def _cluster(client):
    pool = await create_pool(client, "pa")
    member = await create_member(client, pool["id"], "10.0.0.1:9000")
    other = await create_pool(client, "pb")
    await create_member(client, other["id"], "10.0.0.2:9000")
    vhost = await create_vhost(client, "s3.example.com", default_pool_id=pool["id"])
    return pool, member, other, vhost


def _files(tmp_dirs):
    return {
        str(p.relative_to(tmp_dirs["nginx_dir"])): p.read_text() for p in Path(tmp_dirs["nginx_dir"]).rglob("*.conf")
    }


async def _state(client, vhost):
    return {
        "pools": (await client.get("/api/pools")).json(),
        "members": [
            (await client.get(f"/api/pools/{p['id']}/members")).json() for p in (await client.get("/api/pools")).json()
        ],
        "vhosts": (await client.get("/api/vhosts")).json(),
        "routes": (await client.get(f"/api/vhosts/{vhost['id']}/routes")).json(),
    }


REFUSED = [
    ("add a member", lambda c, p, m, o, v: c.post(f"/api/pools/{p['id']}/members", json={"address": "10.0.0.9:9000"})),
    ("edit a member", lambda c, p, m, o, v: c.put(f"/api/pools/{p['id']}/members/{m['id']}", json={"weight": 5})),
    ("edit a pool", lambda c, p, m, o, v: c.put(f"/api/pools/{p['id']}", json={"lb_method": "ip_hash"})),
    ("edit a vhost", lambda c, p, m, o, v: c.put(f"/api/vhosts/{v['id']}", json={"listen_port": 8443})),
    (
        "change the default pool",
        lambda c, p, m, o, v: c.put(f"/api/vhosts/{v['id']}", json={"default_pool_id": o["id"]}),
    ),
    (
        "add a route",
        lambda c, p, m, o, v: c.post(
            f"/api/vhosts/{v['id']}/routes", json={"path_prefix": "/new/", "pool_id": o["id"]}
        ),
    ),
    (
        "promote a bucket",
        lambda c, p, m, o, v: c.post(f"/api/vhosts/{v['id']}/buckets/promoted/promote", json={"pool_id": o["id"]}),
    ),
]


@pytest.mark.parametrize("name, request_", REFUSED, ids=[name for name, _ in REFUSED])
async def test_refused_change_is_not_saved(client, mock_nginx, mock_s3, tmp_dirs, name, request_):
    pool, member, other, vhost = await _cluster(client)
    before_db, before_files = await _state(client, vhost), _files(tmp_dirs)

    _refuse(mock_nginx)
    resp = await request_(client, pool, member, other, vhost)

    assert resp.status_code == 422, resp.text
    assert "nginx rejected this change, so nothing was saved" in resp.json()["detail"]
    assert "something is wrong" in resp.json()["detail"]
    assert await _state(client, vhost) == before_db
    assert _files(tmp_dirs) == before_files

    # and the same request goes through once nginx accepts it
    _accept(mock_nginx)
    assert (await request_(client, pool, member, other, vhost)).status_code in (200, 201)


async def test_refused_vhost_is_not_created(client, mock_nginx, tmp_dirs):
    pool = await create_pool(client, "pa")
    await create_member(client, pool["id"], "10.0.0.1:9000")
    _refuse(mock_nginx)

    resp = await client.post(
        "/api/vhosts",
        json={"server_name": "s3.example.com", "listen_port": 80, "ssl": False, "default_pool_id": pool["id"]},
    )

    assert resp.status_code == 422
    assert (await client.get("/api/vhosts")).json() == []
    assert list(Path(tmp_dirs["nginx_dir"], "vhosts").iterdir()) == []  # no config and no sidecar left behind


async def test_refused_route_removal_keeps_the_route(client, mock_nginx, mock_s3, tmp_dirs):
    _pool, _, other, vhost = await _cluster(client)
    route = (
        await client.post(f"/api/vhosts/{vhost['id']}/routes", json={"path_prefix": "/photos/", "pool_id": other["id"]})
    ).json()
    before = _files(tmp_dirs)

    _refuse(mock_nginx)
    assert (await client.delete(f"/api/vhosts/{vhost['id']}/routes/{route['id']}")).status_code == 422
    assert (await client.delete(f"/api/vhosts/{vhost['id']}/buckets/photos/route")).status_code == 422

    assert len((await client.get(f"/api/vhosts/{vhost['id']}/routes")).json()) == 2
    assert _files(tmp_dirs) == before


async def test_last_member_of_a_pool_in_use_cannot_be_removed(client, mock_nginx, tmp_dirs):
    """Without members a pool has no upstream block; a vhost proxying to it would make nginx
    refuse its whole configuration at the next reload."""
    pool, member, _, _vhost = await _cluster(client)
    pool_file = Path(tmp_dirs["nginx_dir"]) / "pools" / "pa.conf"
    _refuse(mock_nginx, 'host not found in upstream "pa"')

    resp = await client.delete(f"/api/pools/{pool['id']}/members/{member['id']}")

    assert resp.status_code == 422
    assert "still used by a vhost" in resp.json()["detail"]
    assert pool_file.exists()
    assert len((await client.get(f"/api/pools/{pool['id']}/members")).json()) == 1


class TestRouteEndpointsRegenerateTheConfig:
    """These three changed the database and left the nginx file as it was."""

    async def test_remove_bucket_route(self, client, mock_nginx, mock_s3, tmp_dirs):
        _, _, other, vhost = await _cluster(client)
        await client.post(f"/api/vhosts/{vhost['id']}/routes", json={"path_prefix": "/photos/", "pool_id": other["id"]})
        conf = Path(tmp_dirs["nginx_dir"]) / "vhosts" / "s3.example.com.conf"
        assert "location /photos/ {" in conf.read_text()

        assert (await client.delete(f"/api/vhosts/{vhost['id']}/buckets/photos/route")).status_code == 200

        assert "location /photos/" not in conf.read_text()

    async def test_http_bucket_routes(self, client, mock_nginx, mock_s3, tmp_dirs):
        pool, _, _, _ = await _cluster(client)
        web = (
            await client.post("/api/pools", json={"name": "web", "type": "http", "source_nanio_pool_id": pool["id"]})
        ).json()
        await client.post(f"/api/pools/{web['id']}/members", json={"address": "10.0.1.1:80", "role": "primary"})
        cdn = await create_vhost(client, "cdn.example.com", default_pool_id=web["id"])
        conf = Path(tmp_dirs["nginx_dir"]) / "vhosts" / "cdn.example.com.conf"

        assert (await client.post(f"/api/vhosts/{cdn['id']}/http-bucket-routes/photos")).status_code == 201
        assert "location /photos/ {" in conf.read_text()

        assert (await client.delete(f"/api/vhosts/{cdn['id']}/http-bucket-routes/photos")).status_code == 200
        assert "location /photos/" not in conf.read_text()
