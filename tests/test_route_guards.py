"""Route changes that would take a whole bucket away from the pool that holds its objects are
refused: 409 when it has objects there, 502 when the pool cannot be asked. ``allow_orphan``
is the explicit way through. Nothing changes on a refusal."""

from __future__ import annotations

from pathlib import Path

import pytest
from nanio_orchestrator import s3client
from nanio_orchestrator.nginx.generator import vhost_config_path

from tests.api_state import audit, routes, sql
from tests.conftest import create_member, create_pool, create_vhost

NAME = "s3.example.com"


class Objects:
    """Stands in for ``s3client.bucket_has_objects``: answers per member address."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.answers: dict[str, object] = {}

    async def __call__(self, address, bucket, **kwargs):
        self.calls.append((address, bucket, kwargs.get("prefix", "")))
        answer = self.answers.get(address, False)
        if isinstance(answer, Exception):
            raise answer
        return answer


A, B = "10.0.0.1:9000", "10.0.0.2:9000"


@pytest.fixture
def objects(monkeypatch):
    fake = Objects()
    monkeypatch.setattr(s3client, "bucket_has_objects", fake)
    return fake


@pytest.fixture
async def cluster(client, mock_nginx):
    """pool-a (default, member A) and pool-b (member B) behind one S3 vhost."""
    a = await create_pool(client, "pool-a")
    await create_member(client, a["id"], A)
    b = await create_pool(client, "pool-b")
    await create_member(client, b["id"], B)
    vhost = await create_vhost(client, NAME, default_pool_id=a["id"])
    return {"a": a, "b": b, "vhost": vhost}


async def add_route(cluster, path: str, pool: str, enabled: int = 1) -> int:
    """A route put in place without going through the guards."""
    await sql(
        "INSERT INTO routes (vhost_id, path_prefix, pool_id, enabled) VALUES (?, ?, ?, ?)",
        (cluster["vhost"]["id"], path, cluster[pool]["id"], enabled),
    )
    return (await sql("SELECT id FROM routes WHERE path_prefix = ?", (path,)))[0]["id"]


def conf() -> str:
    return Path(vhost_config_path(NAME)).read_text()


HAS = "Bucket 'photos' has objects on pool '{pool}'. {change} would leave them there, no longer reachable through this vhost. Move the bucket with a migration instead, or send allow_orphan=true to leave them behind."
UNKNOWN = "Cannot tell whether bucket 'photos' has objects on pool '{pool}': {why}. {change} would leave them behind. Bring the pool back, or send allow_orphan=true to do it anyway."


# ── POST /routes: a whole bucket ──────────────────────────────────────────────


class TestCreateBucketRoute:
    def body(self, cluster, **extra) -> dict:
        return {"path_prefix": "/photos/", "pool_id": cluster["b"]["id"], **extra}

    async def test_refused_while_the_bucket_has_objects_on_the_default_pool(self, client, cluster, objects):
        objects.answers[A] = True
        before = conf()

        resp = await client.post(f"/api/vhosts/{cluster['vhost']['id']}/routes", json=self.body(cluster))

        assert resp.status_code == 409
        assert resp.json()["detail"] == HAS.format(pool="pool-a", change="Routing it to pool 'pool-b'")
        assert objects.calls == [(A, "photos", "")]
        assert await routes(client, cluster["vhost"]) == {"/": "pool-a"}
        assert conf() == before
        assert [e for e in await audit(client, "create") if "photos" in (e["after_json"] or "")] == []

    @pytest.mark.parametrize("error", [ConnectionRefusedError("Connection refused"), PermissionError("403 Forbidden")])
    async def test_refused_when_the_default_pool_cannot_be_asked(self, client, cluster, objects, error):
        objects.answers[A] = error

        resp = await client.post(f"/api/vhosts/{cluster['vhost']['id']}/routes", json=self.body(cluster))

        assert resp.status_code == 502
        assert resp.json()["detail"] == UNKNOWN.format(
            pool="pool-a", why=f"the pool could not be asked ({error})", change="Routing it to pool 'pool-b'"
        )
        assert await routes(client, cluster["vhost"]) == {"/": "pool-a"}

    async def test_refused_when_the_default_pool_has_no_enabled_member(self, client, cluster, objects):
        await sql("UPDATE pool_members SET enabled = 0 WHERE address = ?", (A,))

        resp = await client.post(f"/api/vhosts/{cluster['vhost']['id']}/routes", json=self.body(cluster))

        assert resp.status_code == 502
        assert "the pool has no enabled member" in resp.json()["detail"]
        assert objects.calls == []
        assert await routes(client, cluster["vhost"]) == {"/": "pool-a"}

    @pytest.mark.parametrize("answer", [True, ConnectionRefusedError("down")])
    async def test_allow_orphan_goes_through_without_asking(self, client, cluster, objects, answer):
        objects.answers[A] = answer

        resp = await client.post(
            f"/api/vhosts/{cluster['vhost']['id']}/routes", json=self.body(cluster, allow_orphan=True)
        )

        assert resp.status_code == 201
        assert objects.calls == []
        assert await routes(client, cluster["vhost"]) == {"/photos/": "pool-b", "/": "pool-a"}

    async def test_allowed_when_the_bucket_is_empty_or_absent(self, client, cluster, objects):
        resp = await client.post(f"/api/vhosts/{cluster['vhost']['id']}/routes", json=self.body(cluster))

        assert resp.status_code == 201
        assert objects.calls == [(A, "photos", "")]
        assert "location /photos/" in conf()

    async def test_route_to_the_default_pool_itself_moves_nothing(self, client, cluster, objects):
        objects.answers[A] = True
        resp = await client.post(
            f"/api/vhosts/{cluster['vhost']['id']}/routes",
            json={"path_prefix": "/photos/", "pool_id": cluster["a"]["id"]},
        )
        assert resp.status_code == 201
        assert objects.calls == []

    async def test_route_created_disabled_moves_nothing_yet(self, client, cluster, objects):
        objects.answers[A] = True
        resp = await client.post(f"/api/vhosts/{cluster['vhost']['id']}/routes", json=self.body(cluster, enabled=False))
        assert resp.status_code == 201
        assert objects.calls == []

    async def test_vhost_without_a_default_pool_serves_the_bucket_from_nowhere(self, client, mock_nginx, objects):
        pool = await create_pool(client, "pool-b")
        await create_member(client, pool["id"], B)
        vhost = await create_vhost(client, NAME)

        resp = await client.post(
            f"/api/vhosts/{vhost['id']}/routes", json={"path_prefix": "/photos/", "pool_id": pool["id"]}
        )

        assert resp.status_code == 201
        assert objects.calls == []

    async def test_http_vhost_has_no_s3_to_ask(self, client, mock_nginx, objects):
        web = await create_pool(client, "web", pool_type="http")
        await create_member(client, web["id"], "10.0.1.1:80", role="primary")
        cdn = await create_pool(client, "cdn", pool_type="http")
        await create_member(client, cdn["id"], "10.0.1.2:80", role="primary")
        vhost = await create_vhost(client, NAME, default_pool_id=web["id"])

        resp = await client.post(
            f"/api/vhosts/{vhost['id']}/routes", json={"path_prefix": "/photos/", "pool_id": cdn["id"]}
        )
        assert resp.status_code == 201
        route_id = resp.json()["id"]
        assert (
            await client.put(f"/api/vhosts/{vhost['id']}/routes/{route_id}", json={"enabled": False})
        ).status_code == 200
        assert (await client.delete(f"/api/vhosts/{vhost['id']}/routes/{route_id}")).status_code == 204
        assert objects.calls == []

    async def test_folder_route_keeps_its_own_guard_only(self, client, cluster, objects, set_secret, monkeypatch):
        """The folder check asks about the folder; the bucket guard must not ask on top of it."""
        objects.answers[A] = True
        resp = await client.post(
            f"/api/vhosts/{cluster['vhost']['id']}/routes",
            json={"path_prefix": "/photos/2025/", "pool_id": cluster["b"]["id"]},
        )
        assert resp.status_code == 409
        assert "already has objects under '2025/'" in resp.json()["detail"]
        assert objects.calls == [(A, "photos", "2025/")]


# ── PUT /routes/{id}: enabled ─────────────────────────────────────────────────


class TestToggleBucketRoute:
    async def test_disabling_is_refused_while_the_bucket_has_objects_on_the_routes_pool(self, client, cluster, objects):
        route_id = await add_route(cluster, "/photos/", "b")
        objects.answers[B] = True

        resp = await client.put(f"/api/vhosts/{cluster['vhost']['id']}/routes/{route_id}", json={"enabled": False})

        assert resp.status_code == 409
        assert resp.json()["detail"] == HAS.format(pool="pool-b", change="Disabling its route")
        assert objects.calls == [(B, "photos", "")]
        assert (await sql("SELECT enabled FROM routes WHERE id = ?", (route_id,)))[0]["enabled"] == 1
        assert await audit(client, "update") == []

    async def test_disabling_is_refused_when_the_routes_pool_cannot_be_asked(self, client, cluster, objects):
        route_id = await add_route(cluster, "/photos/", "b")
        objects.answers[B] = TimeoutError("timed out")

        resp = await client.put(f"/api/vhosts/{cluster['vhost']['id']}/routes/{route_id}", json={"enabled": False})

        assert resp.status_code == 502
        assert resp.json()["detail"] == UNKNOWN.format(
            pool="pool-b", why="the pool could not be asked (timed out)", change="Disabling its route"
        )
        assert (await sql("SELECT enabled FROM routes WHERE id = ?", (route_id,)))[0]["enabled"] == 1

    async def test_disabling_with_allow_orphan_or_an_empty_bucket_goes_through(self, client, cluster, objects):
        route_id = await add_route(cluster, "/photos/", "b")
        url = f"/api/vhosts/{cluster['vhost']['id']}/routes/{route_id}"

        assert (await client.put(url, json={"enabled": False})).status_code == 200  # empty
        await sql("UPDATE routes SET enabled = 1 WHERE id = ?", (route_id,))
        objects.answers[B] = True
        resp = await client.put(url, json={"enabled": False, "allow_orphan": True})

        assert resp.status_code == 200 and resp.json()["enabled"] is False
        assert "location /photos/" not in conf()

    async def test_enabling_is_refused_while_the_bucket_has_objects_on_the_default_pool(self, client, cluster, objects):
        route_id = await add_route(cluster, "/photos/", "b", enabled=0)
        objects.answers[A] = True

        resp = await client.put(f"/api/vhosts/{cluster['vhost']['id']}/routes/{route_id}", json={"enabled": True})

        assert resp.status_code == 409
        assert resp.json()["detail"] == HAS.format(pool="pool-a", change="Enabling its route")
        assert objects.calls == [(A, "photos", "")]
        assert (await sql("SELECT enabled FROM routes WHERE id = ?", (route_id,)))[0]["enabled"] == 0

    async def test_enabling_is_refused_when_the_default_pool_cannot_be_asked(self, client, cluster, objects):
        route_id = await add_route(cluster, "/photos/", "b", enabled=0)
        objects.answers[A] = ConnectionRefusedError("Connection refused")

        resp = await client.put(f"/api/vhosts/{cluster['vhost']['id']}/routes/{route_id}", json={"enabled": True})

        assert resp.status_code == 502
        assert (await sql("SELECT enabled FROM routes WHERE id = ?", (route_id,)))[0]["enabled"] == 0

    async def test_enabling_with_allow_orphan_or_an_empty_bucket_goes_through(self, client, cluster, objects):
        route_id = await add_route(cluster, "/photos/", "b", enabled=0)
        url = f"/api/vhosts/{cluster['vhost']['id']}/routes/{route_id}"

        assert (await client.put(url, json={"enabled": True})).status_code == 200  # empty on the default pool
        await sql("UPDATE routes SET enabled = 0 WHERE id = ?", (route_id,))
        objects.answers[A] = True
        resp = await client.put(url, json={"enabled": True, "allow_orphan": True})

        assert resp.status_code == 200 and resp.json()["enabled"] is True
        assert "location /photos/" in conf()

    async def test_saving_the_state_it_already_has_asks_nothing(self, client, cluster, objects):
        route_id = await add_route(cluster, "/photos/", "b")
        objects.answers[B] = True
        resp = await client.put(f"/api/vhosts/{cluster['vhost']['id']}/routes/{route_id}", json={"enabled": True})
        assert resp.status_code == 200
        assert objects.calls == []

    async def test_route_on_the_default_pool_moves_nothing(self, client, cluster, objects):
        route_id = await add_route(cluster, "/photos/", "a")
        objects.answers[A] = True
        resp = await client.put(f"/api/vhosts/{cluster['vhost']['id']}/routes/{route_id}", json={"enabled": False})
        assert resp.status_code == 200
        assert objects.calls == []

    async def test_folder_and_default_routes_are_not_bucket_routes(self, client, cluster, objects):
        folder_id = await add_route(cluster, "/photos/2025/", "b")
        default_id = (await sql("SELECT id FROM routes WHERE path_prefix = '/'"))[0]["id"]
        objects.answers[A] = objects.answers[B] = True
        base = f"/api/vhosts/{cluster['vhost']['id']}/routes"

        assert (await client.put(f"{base}/{folder_id}", json={"enabled": False})).status_code == 200
        assert (await client.put(f"{base}/{default_id}", json={"enabled": False})).status_code == 200
        assert objects.calls == []


# ── DELETE /routes/{id} and DELETE /buckets/{bucket}/route ────────────────────


def delete_urls(cluster, route_id: int) -> dict:
    vhost_id = cluster["vhost"]["id"]
    return {
        "route": (f"/api/vhosts/{vhost_id}/routes/{route_id}", "Deleting its route", 204),
        "bucket": (f"/api/vhosts/{vhost_id}/buckets/photos/route", "Removing its route", 200),
    }


@pytest.mark.parametrize("which", ["route", "bucket"])
class TestDeleteBucketRoute:
    async def test_refused_while_the_bucket_has_objects_on_its_pool(self, client, cluster, objects, which):
        route_id = await add_route(cluster, "/photos/", "b")
        await client.put(f"/api/vhosts/{cluster['vhost']['id']}", json={"listen_port": 8081})
        url, change, _ = delete_urls(cluster, route_id)[which]
        objects.answers[B] = True
        before = conf()

        resp = await client.delete(url)

        assert resp.status_code == 409
        assert resp.json()["detail"] == HAS.format(pool="pool-b", change=change)
        assert objects.calls == [(B, "photos", "")]
        assert await routes(client, cluster["vhost"]) == {"/photos/": "pool-b", "/": "pool-a"}
        assert conf() == before
        assert await audit(client, "delete") == [] and await audit(client, "remove_bucket_route") == []

    async def test_refused_when_its_pool_cannot_be_asked(self, client, cluster, objects, which):
        route_id = await add_route(cluster, "/photos/", "b")
        url, change, _ = delete_urls(cluster, route_id)[which]
        objects.answers[B] = ConnectionRefusedError("Connection refused")

        resp = await client.delete(url)

        assert resp.status_code == 502
        assert resp.json()["detail"] == UNKNOWN.format(
            pool="pool-b", why="the pool could not be asked (Connection refused)", change=change
        )
        assert await routes(client, cluster["vhost"]) == {"/photos/": "pool-b", "/": "pool-a"}

    async def test_refused_when_its_pool_has_no_enabled_member(self, client, cluster, objects, which):
        route_id = await add_route(cluster, "/photos/", "b")
        await sql("UPDATE pool_members SET enabled = 0 WHERE address = ?", (B,))
        url, _, _ = delete_urls(cluster, route_id)[which]

        resp = await client.delete(url)

        assert resp.status_code == 502
        assert "the pool has no enabled member" in resp.json()["detail"]
        assert await routes(client, cluster["vhost"]) == {"/photos/": "pool-b", "/": "pool-a"}

    @pytest.mark.parametrize("answer", [True, ConnectionRefusedError("down")])
    async def test_allow_orphan_goes_through(self, client, cluster, objects, which, answer):
        route_id = await add_route(cluster, "/photos/", "b")
        url, _, ok = delete_urls(cluster, route_id)[which]
        objects.answers[B] = answer

        resp = await client.delete(url, params={"allow_orphan": "true"})

        assert resp.status_code == ok
        assert objects.calls == []
        assert await routes(client, cluster["vhost"]) == {"/": "pool-a"}

    async def test_allowed_when_the_bucket_is_empty_or_gone_from_its_pool(self, client, cluster, objects, which):
        """A bucket that does not exist on the pool has no objects: cleaning up after its owner
        deleted it (the "missing on pool" case) needs no override."""
        route_id = await add_route(cluster, "/photos/", "b")
        url, _, ok = delete_urls(cluster, route_id)[which]

        resp = await client.delete(url)

        assert resp.status_code == ok
        assert objects.calls == [(B, "photos", "")]
        assert await routes(client, cluster["vhost"]) == {"/": "pool-a"}

    async def test_disabled_route_serves_nothing_so_nothing_is_left_behind(self, client, cluster, objects, which):
        route_id = await add_route(cluster, "/photos/", "b", enabled=0)
        url, _, ok = delete_urls(cluster, route_id)[which]
        objects.answers[B] = True

        resp = await client.delete(url)

        assert resp.status_code == ok
        assert objects.calls == []

    async def test_route_on_the_default_pool_moves_nothing(self, client, cluster, objects, which):
        route_id = await add_route(cluster, "/photos/", "a")
        url, _, ok = delete_urls(cluster, route_id)[which]
        objects.answers[A] = True

        resp = await client.delete(url)

        assert resp.status_code == ok
        assert objects.calls == []
