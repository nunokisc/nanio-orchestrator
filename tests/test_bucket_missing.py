"""A routed bucket that its pool reports as missing is flagged; its route is never removed."""

from __future__ import annotations

from pathlib import Path

import pytest

from nanio_orchestrator import bucket_sync


@pytest.fixture
def s3(monkeypatch):
    """Default pool lists `photos`; `state["exists"]` is what the routed pool answers."""
    state = {"exists": True}

    async def list_buckets(address, **kw):
        return [{"name": "photos"}]

    async def bucket_exists(address, bucket, **kw):
        if isinstance(state["exists"], Exception):
            raise state["exists"]
        return state["exists"]

    monkeypatch.setattr(bucket_sync, "list_buckets", list_buckets)
    monkeypatch.setattr(bucket_sync, "bucket_exists", bucket_exists)
    return state


async def _routed_bucket(client):
    default = (await client.post("/api/pools", json={"name": "default", "type": "nanio"})).json()
    await client.post(f"/api/pools/{default['id']}/members", json={"address": "10.0.0.1:9000"})
    big = (await client.post("/api/pools", json={"name": "big", "type": "nanio"})).json()
    await client.post(f"/api/pools/{big['id']}/members", json={"address": "10.0.0.2:9000"})
    vhost = (
        await client.post(
            "/api/vhosts",
            json={"server_name": "s3.example.com", "listen_port": 80, "ssl": False, "default_pool_id": default["id"]},
        )
    ).json()
    resp = await client.post(
        f"/api/vhosts/{vhost['id']}/routes", json={"path_prefix": "/photos/", "pool_id": big["id"]}
    )
    assert resp.status_code == 201, resp.text
    return vhost


async def _bucket(client, vhost):
    buckets = (await client.get(f"/api/vhosts/{vhost['id']}/buckets")).json()["buckets"]
    return next(b for b in buckets if b["name"] == "photos")


async def test_missing_bucket_is_flagged_and_route_kept(client, mock_nginx, tmp_dirs, s3):
    vhost = await _routed_bucket(client)
    conf = Path(tmp_dirs["nginx_dir"]) / "vhosts" / "s3.example.com.conf"
    before = conf.read_text()
    mock_nginx["reload_nginx"].reset_mock()

    s3["exists"] = False
    result = await bucket_sync.sync_vhost_buckets_once(vhost["id"])

    assert [m["bucket"] for m in result["missing_on_pool"]] == ["photos"]
    bucket = await _bucket(client, vhost)
    assert bucket["status"] == "routed" and bucket["missing_since"]
    routes = (await client.get(f"/api/vhosts/{vhost['id']}/routes")).json()
    assert "/photos/" in [r["path_prefix"] for r in routes]
    assert conf.read_text() == before and "location /photos/" in before
    mock_nginx["reload_nginx"].assert_not_called()
    assert (await client.get("/api/health")).json()["missing_buckets"] == 1

    # The first sighting is kept on later runs
    first = bucket["missing_since"]
    await bucket_sync.sync_vhost_buckets_once(vhost["id"])
    assert (await _bucket(client, vhost))["missing_since"] == first


async def test_flag_clears_when_bucket_is_back(client, mock_nginx, tmp_dirs, s3):
    vhost = await _routed_bucket(client)
    s3["exists"] = False
    await bucket_sync.sync_vhost_buckets_once(vhost["id"])

    s3["exists"] = True  # e.g. the node's storage is mounted again
    result = await bucket_sync.sync_vhost_buckets_once(vhost["id"])

    assert "missing_on_pool" not in result
    assert (await _bucket(client, vhost))["missing_since"] is None
    assert (await client.get("/api/health")).json()["missing_buckets"] == 0


async def test_unreachable_pool_changes_nothing(client, mock_nginx, tmp_dirs, s3):
    vhost = await _routed_bucket(client)
    s3["exists"] = ConnectionError("refused")
    result = await bucket_sync.sync_vhost_buckets_once(vhost["id"])

    assert "missing_on_pool" not in result
    assert (await _bucket(client, vhost))["missing_since"] is None


async def test_operator_removes_the_route(client, mock_nginx, tmp_dirs, s3):
    vhost = await _routed_bucket(client)
    s3["exists"] = False
    await bucket_sync.sync_vhost_buckets_once(vhost["id"])

    resp = await client.delete(f"/api/vhosts/{vhost['id']}/buckets/photos/route")
    assert resp.status_code == 200, resp.text
    bucket = await _bucket(client, vhost)
    assert bucket["status"] == "unrouted" and bucket["missing_since"] is None
