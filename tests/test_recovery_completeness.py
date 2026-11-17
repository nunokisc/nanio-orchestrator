"""What the nginx files cannot hold is kept in the sidecars and comes back in a rebuild."""

from __future__ import annotations

import json
from pathlib import Path

from nanio_orchestrator import sidecar

from .test_pools import create_pool
from .test_vhosts import create_vhost


async def _cluster(client):
    hot = await create_pool(client, "hot")
    await client.post(f"/api/pools/{hot['id']}/members", json={"address": "10.0.0.1:9000"})
    spare = (await client.post(f"/api/pools/{hot['id']}/members", json={"address": "10.0.0.2:9000"})).json()
    web = (await client.post("/api/pools", json={"name": "web", "type": "http"})).json()
    await client.post(f"/api/pools/{web['id']}/members", json={"address": "10.0.1.1:80", "role": "primary"})
    await client.post(f"/api/pools/{web['id']}/members", json={"address": "10.0.1.2:80", "role": "replica"})
    vhost = await create_vhost(client, "s3.example.com", default_pool_id=hot["id"])
    route = (
        await client.post(f"/api/vhosts/{vhost['id']}/routes", json={"path_prefix": "/photos/", "pool_id": hot["id"]})
    ).json()
    return hot, spare, web, vhost, route


def _sidecar(tmp_dirs, kind, name):
    return json.loads((Path(tmp_dirs["nginx_dir"]) / kind / f"{name}.meta.json").read_text())


async def _members(client, pool):
    rows = (await client.get(f"/api/pools/{pool['id']}/members")).json()
    return {m["address"]: (m["role"], m["enabled"]) for m in rows}


async def test_every_accepted_change_is_written_to_the_sidecars(client, mock_nginx, mock_s3, tmp_dirs):
    hot, spare, _web, vhost, route = await _cluster(client)

    await client.put(f"/api/pools/{hot['id']}/members/{spare['id']}", json={"enabled": False})
    await client.put(f"/api/vhosts/{vhost['id']}/routes/{route['id']}", json={"enabled": False})

    recorded = {m["address"]: (m["role"], m["enabled"]) for m in _sidecar(tmp_dirs, "pools", "hot")["members"]}
    assert recorded == {"10.0.0.1:9000": ("active", True), "10.0.0.2:9000": ("active", False)}
    assert [m["role"] for m in _sidecar(tmp_dirs, "pools", "web")["members"]] == ["primary", "replica"]
    assert _sidecar(tmp_dirs, "vhosts", "s3.example.com")["disabled_routes"] == [
        {"path_prefix": "/photos/", "pool_name": "hot"}
    ]


async def test_rebuild_restores_disabled_members_roles_and_disabled_routes(client, mock_nginx, mock_s3, tmp_dirs):
    hot, spare, _web, vhost, route = await _cluster(client)
    await client.put(f"/api/pools/{hot['id']}/members/{spare['id']}", json={"enabled": False})
    await client.put(f"/api/vhosts/{vhost['id']}/routes/{route['id']}", json={"enabled": False})
    await client.put(f"/api/vhosts/{vhost['id']}", json={"enabled": False})

    resp = await client.post("/api/config/rebuild-from-disk?force=true")
    assert resp.status_code == 200, resp.text

    pools = {p["name"]: p for p in (await client.get("/api/pools")).json()}
    assert await _members(client, pools["hot"]) == {
        "10.0.0.1:9000": ("active", True),
        "10.0.0.2:9000": ("active", False),
    }
    assert await _members(client, pools["web"]) == {"10.0.1.1:80": ("primary", True), "10.0.1.2:80": ("replica", True)}
    rebuilt = (await client.get("/api/vhosts")).json()[0]
    assert rebuilt["enabled"] is False
    routes = {r["path_prefix"]: r["enabled"] for r in (await client.get(f"/api/vhosts/{rebuilt['id']}/routes")).json()}
    assert routes == {"/": True, "/photos/": False}


async def test_disabled_route_to_a_pool_that_is_gone_is_left_out(client, mock_nginx, mock_s3, tmp_dirs):
    _, _, _, _vhost, _ = await _cluster(client)
    path = Path(tmp_dirs["nginx_dir"]) / "vhosts" / "s3.example.com.meta.json"
    data = json.loads(path.read_text())
    data["disabled_routes"] = [{"path_prefix": "/gone/", "pool_name": "no-such-pool"}]
    path.write_text(json.dumps(data))

    resp = await client.post("/api/config/rebuild-from-disk?force=true")
    assert resp.status_code == 200, resp.text

    rebuilt = (await client.get("/api/vhosts")).json()[0]
    paths = [r["path_prefix"] for r in (await client.get(f"/api/vhosts/{rebuilt['id']}/routes")).json()]
    assert "/gone/" not in paths


async def test_sidecar_of_a_pool_or_vhost_that_does_not_exist_is_not_written(db, tmp_dirs):
    import nanio_orchestrator.config as cfg

    cfg.settings = None
    await sidecar.write_pool_sidecar_from_db(db, 999)
    await sidecar.write_vhost_sidecar_from_db(db, 999)
    assert list(Path(tmp_dirs["nginx_dir"]).rglob("*.meta.json")) == []
