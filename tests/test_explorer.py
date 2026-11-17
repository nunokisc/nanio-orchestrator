"""The read-only explorer: what is on a pool, which pool serves what, and honest errors."""

from __future__ import annotations

import pytest
from nanio_orchestrator import s3client
from nanio_orchestrator.api import explorer

from tests.api_state import audit
from tests.conftest import create_member, create_pool

CREDS = {"access_key": "AKIAEXAMPLE", "secret_key": "very-secret"}
AUTH = {**CREDS, "region": "us-east-1"}


class FakeS3:
    """Stands in for the S3 client: records the calls and answers from canned values."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.buckets = [{"name": "photos", "created": ""}, {"name": "archive", "created": ""}]
        self.page: object = {"folders": [], "objects": [], "next_token": None}
        self.meta: object = {
            "size": 5,
            "last_modified": "2026-01-02T03:04:05Z",
            "etag": "abc",
            "content_type": "text/html",
        }
        self.body: object = b"hello"

    def _answer(self, value):
        if isinstance(value, Exception):
            raise value
        return value

    async def list_buckets(self, address, **auth):
        self.calls.append(("list_buckets", address, auth))
        return self._answer(self.buckets)

    async def list_objects_page(self, address, bucket, **kwargs):
        self.calls.append(("list_objects_page", address, bucket, kwargs))
        return self._answer(self.page)

    async def head_object(self, address, bucket, key, **auth):
        self.calls.append(("head_object", address, bucket, key, auth))
        return self._answer(self.meta)

    async def get_object_capped(self, address, bucket, key, max_bytes, **auth):
        self.calls.append(("get_object_capped", address, bucket, key, max_bytes, auth))
        return self._answer(self.body)


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    for name in ("list_buckets", "list_objects_page", "head_object", "get_object_capped"):
        monkeypatch.setattr(s3client, name, getattr(fake, name))
    return fake


async def nanio_pool(client, name: str, address: str = "10.0.0.1:9000") -> dict:
    pool = await create_pool(client, name)
    await create_member(client, pool["id"], address)
    assert (await client.put(f"/api/pools/{pool['id']}/credentials", json=CREDS)).status_code == 200
    return pool


@pytest.fixture
async def pool(client, mock_nginx, set_secret):
    return await nanio_pool(client, "pool-a")


@pytest.fixture
async def cluster(client, mock_nginx, set_secret):
    """pool-a (default) and pool-b behind one vhost; /photos/2025/ and /logs/ go to pool-b."""
    a = await nanio_pool(client, "pool-a", "10.0.0.1:9000")
    b = await nanio_pool(client, "pool-b", "10.0.0.2:9000")
    vhost = (
        await client.post(
            "/api/vhosts",
            json={"server_name": "s3.test", "ssl": False, "listen_port": 8081, "default_pool_id": a["id"]},
        )
    ).json()
    from tests.api_state import sql

    for prefix in ("/photos/2025/", "/photos/2025/q1/deep/", "/logs/", "/other/2025/"):
        await sql(
            "INSERT INTO routes (vhost_id, path_prefix, pool_id) VALUES (?, ?, ?)", (vhost["id"], prefix, b["id"])
        )
    await sql(
        "INSERT INTO routes (vhost_id, path_prefix, pool_id, enabled) VALUES (?, '/photos/off/', ?, 0)",
        (vhost["id"], b["id"]),
    )
    return {"a": a, "b": b, "vhost": vhost}


# ── Who can be asked ──────────────────────────────────────────────────────────


class TestAccess:
    URLS = (
        "/api/explorer/pools/{id}/buckets",
        "/api/explorer/pools/{id}/buckets/photos/objects",
        "/api/explorer/pools/{id}/buckets/photos/object?key=k",
        "/api/explorer/pools/{id}/buckets/photos/download?key=k",
    )

    async def test_requires_authentication(self, client):
        client.headers.pop("X-Orchestrator-Key")
        for url in (*self.URLS, "/api/explorer/vhosts/1/locate?bucket=photos"):
            assert (await client.get(url.format(id=1))).status_code == 401

    async def test_unknown_pool_is_a_404(self, client, s3):
        for url in self.URLS:
            resp = await client.get(url.format(id=99))
            assert (resp.status_code, resp.json()["detail"]) == (404, "Pool not found")
        assert s3.calls == []

    async def test_http_pool_has_no_s3_api(self, client, mock_nginx, s3):
        web = await create_pool(client, "web", pool_type="http")
        await create_member(client, web["id"], "10.0.1.1:80", role="primary")
        for url in self.URLS:
            resp = await client.get(url.format(id=web["id"]))
            assert resp.status_code == 400
            assert resp.json()["detail"] == "Pool 'web' is an http pool: it has no S3 API to browse."
        assert s3.calls == []

    async def test_pool_without_an_enabled_member_is_an_error_not_an_empty_listing(self, client, mock_nginx, s3):
        pool = await create_pool(client, "pool-a")
        await create_member(client, pool["id"], "10.0.0.1:9000", enabled=False)
        for url in self.URLS:
            resp = await client.get(url.format(id=pool["id"]))
            assert (resp.status_code, resp.json()["detail"]) == (400, "Pool 'pool-a' has no enabled member to ask.")
        assert s3.calls == []

    async def test_pool_without_credentials_is_an_error_not_an_empty_listing(self, client, mock_nginx, s3):
        pool = await create_pool(client, "pool-a")
        await create_member(client, pool["id"], "10.0.0.1:9000")
        for url in self.URLS:
            resp = await client.get(url.format(id=pool["id"]))
            assert resp.status_code == 400
            assert "has no stored credentials" in resp.json()["detail"]
        assert s3.calls == []

    @pytest.mark.parametrize("bucket", ["..", ".hidden", "a b", "a%2Fb"])
    async def test_bucket_name_must_be_a_bucket_name(self, client, pool, s3, bucket):
        resp = await client.get(f"/api/explorer/pools/{pool['id']}/buckets/{bucket}/objects")
        assert resp.status_code in (404, 422)
        assert s3.calls == []


# ── Buckets ───────────────────────────────────────────────────────────────────


class TestBuckets:
    async def test_lists_the_buckets_of_the_pool_with_its_credentials(self, client, pool, s3):
        resp = await client.get(f"/api/explorer/pools/{pool['id']}/buckets")
        assert resp.status_code == 200
        assert resp.json() == {
            "pool": {"id": pool["id"], "name": "pool-a"},
            "member": "10.0.0.1:9000",
            "buckets": ["archive", "photos"],
        }
        assert s3.calls == [("list_buckets", "10.0.0.1:9000", AUTH)]

    @pytest.mark.parametrize(
        "error", [ConnectionRefusedError("Connection refused"), PermissionError("403"), RuntimeError("HTTP 500")]
    )
    async def test_pool_that_cannot_be_asked_is_a_502_with_the_reason(self, client, pool, s3, error):
        s3.buckets = error
        resp = await client.get(f"/api/explorer/pools/{pool['id']}/buckets")
        assert resp.status_code == 502
        assert resp.json()["detail"] == (f"Could not list the buckets on pool 'pool-a' (10.0.0.1:9000): {error}")


# ── Objects ───────────────────────────────────────────────────────────────────


class TestObjects:
    async def test_one_level_of_a_bucket(self, client, pool, s3):
        s3.page = {
            "folders": ["2025/", "2024/"],
            "objects": [{"key": "readme.txt", "size": 3, "last_modified": "2026-01-01T00:00:00Z", "etag": "e"}],
            "next_token": "more",
        }
        resp = await client.get(f"/api/explorer/pools/{pool['id']}/buckets/photos/objects")
        assert resp.status_code == 200
        assert resp.json() == {
            "pool": {"id": pool["id"], "name": "pool-a"},
            "bucket": "photos",
            "prefix": "",
            "folders": [{"prefix": "2024/", "routed_pool": None}, {"prefix": "2025/", "routed_pool": None}],
            "objects": [{"key": "readme.txt", "size": 3, "last_modified": "2026-01-01T00:00:00Z", "etag": "e"}],
            "next_token": "more",
        }
        assert s3.calls == [
            (
                "list_objects_page",
                "10.0.0.1:9000",
                "photos",
                {"prefix": "", "max_keys": 200, "continuation_token": "", **AUTH},
            )
        ]

    async def test_prefix_token_and_limit_are_passed_on_as_given(self, client, pool, s3):
        params = {"prefix": "relatórios 2025/a+b&c/", "token": "t+k/=", "limit": 50}
        resp = await client.get(f"/api/explorer/pools/{pool['id']}/buckets/photos/objects", params=params)
        assert resp.json()["prefix"] == "relatórios 2025/a+b&c/"
        assert s3.calls[0][3] == {
            "prefix": "relatórios 2025/a+b&c/",
            "max_keys": 50,
            "continuation_token": "t+k/=",
            **AUTH,
        }

    @pytest.mark.parametrize("limit", [0, 1001])
    async def test_limit_is_bounded(self, client, pool, s3, limit):
        resp = await client.get(f"/api/explorer/pools/{pool['id']}/buckets/photos/objects?limit={limit}")
        assert resp.status_code == 422

    async def test_bucket_that_does_not_exist_on_the_pool_is_a_404(self, client, pool, s3):
        s3.page = None
        resp = await client.get(f"/api/explorer/pools/{pool['id']}/buckets/photos/objects")
        assert (resp.status_code, resp.json()["detail"]) == (404, "Bucket 'photos' does not exist on pool 'pool-a'.")

    async def test_pool_that_cannot_be_asked_is_a_502_never_an_empty_listing(self, client, pool, s3):
        s3.page = TimeoutError("timed out")
        resp = await client.get(f"/api/explorer/pools/{pool['id']}/buckets/photos/objects")
        assert resp.status_code == 502
        assert resp.json()["detail"] == "Could not list bucket 'photos' on pool 'pool-a' (10.0.0.1:9000): timed out"


class TestRoutedFolders:
    async def test_folder_routed_to_another_pool_is_marked_with_that_pool(self, client, cluster, s3):
        a, b, vhost = cluster["a"], cluster["b"], cluster["vhost"]
        s3.page = {"folders": ["2024/", "2025/"], "objects": [], "next_token": None}
        resp = await client.get(
            f"/api/explorer/pools/{a['id']}/buckets/photos/objects", params={"vhost_id": vhost["id"]}
        )
        assert resp.json()["folders"] == [
            {"prefix": "2024/", "routed_pool": None},
            {"prefix": "2025/", "routed_pool": {"id": b["id"], "name": "pool-b"}},
        ]

    async def test_routed_folder_is_shown_even_when_this_pool_holds_nothing_under_it(self, client, cluster, s3):
        """No marker yet: clients still see the folder through the gateway."""
        a, vhost = cluster["a"], cluster["vhost"]
        s3.page = {"folders": ["2024/"], "objects": [], "next_token": None}
        resp = await client.get(
            f"/api/explorer/pools/{a['id']}/buckets/photos/objects", params={"vhost_id": vhost["id"]}
        )
        assert [f["prefix"] for f in resp.json()["folders"]] == ["2024/", "2025/"]

    async def test_only_folders_of_the_level_being_listed_are_added(self, client, cluster, s3):
        a, b, vhost = cluster["a"], cluster["b"], cluster["vhost"]
        url = f"/api/explorer/pools/{a['id']}/buckets/photos/objects"
        marked = {"prefix": "2025/q1/deep/", "routed_pool": {"id": b["id"], "name": "pool-b"}}

        inside = (await client.get(url, params={"vhost_id": vhost["id"], "prefix": "2025/q1/"})).json()
        assert inside["folders"] == [marked]
        # the folder itself, one level up, and a filter that does not match: nothing is added
        for prefix in ("2025/q1/deep/", "2025/", "2025/q1/x"):
            body = (await client.get(url, params={"vhost_id": vhost["id"], "prefix": prefix})).json()
            assert body["folders"] == [], prefix
        # a filter typed in the current folder still finds it
        filtered = (await client.get(url, params={"vhost_id": vhost["id"], "prefix": "2025/q1/de"})).json()
        assert filtered["folders"] == [marked]

    async def test_later_pages_do_not_repeat_the_routed_folders(self, client, cluster, s3):
        a, vhost = cluster["a"], cluster["vhost"]
        resp = await client.get(
            f"/api/explorer/pools/{a['id']}/buckets/photos/objects", params={"vhost_id": vhost["id"], "token": "p2"}
        )
        assert resp.json()["folders"] == []

    async def test_on_the_pool_the_folder_is_routed_to_it_is_an_ordinary_folder(self, client, cluster, s3):
        b, vhost = cluster["b"], cluster["vhost"]
        s3.page = {"folders": ["2025/"], "objects": [], "next_token": None}
        resp = await client.get(
            f"/api/explorer/pools/{b['id']}/buckets/photos/objects", params={"vhost_id": vhost["id"]}
        )
        assert resp.json()["folders"] == [{"prefix": "2025/", "routed_pool": None}]

    async def test_without_a_vhost_nothing_is_marked(self, client, cluster, s3):
        s3.page = {"folders": ["2025/"], "objects": [], "next_token": None}
        resp = await client.get(f"/api/explorer/pools/{cluster['a']['id']}/buckets/photos/objects")
        assert resp.json()["folders"] == [{"prefix": "2025/", "routed_pool": None}]

    async def test_unknown_vhost_is_a_404(self, client, pool, s3):
        resp = await client.get(f"/api/explorer/pools/{pool['id']}/buckets/photos/objects?vhost_id=99")
        assert (resp.status_code, resp.json()["detail"]) == (404, "Vhost not found")


# ── Object details and download ───────────────────────────────────────────────


class TestObjectDetails:
    async def test_metadata_of_an_object(self, client, pool, s3):
        resp = await client.get(
            f"/api/explorer/pools/{pool['id']}/buckets/photos/object", params={"key": "2025/a b+ç&.txt"}
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "pool": {"id": pool["id"], "name": "pool-a"},
            "bucket": "photos",
            "key": "2025/a b+ç&.txt",
            "size": 5,
            "last_modified": "2026-01-02T03:04:05Z",
            "etag": "abc",
            "content_type": "text/html",
        }
        assert s3.calls == [("head_object", "10.0.0.1:9000", "photos", "2025/a b+ç&.txt", AUTH)]

    async def test_object_that_does_not_exist_is_a_404(self, client, pool, s3):
        s3.meta = None
        resp = await client.get(f"/api/explorer/pools/{pool['id']}/buckets/photos/object?key=nope")
        assert (resp.status_code, resp.json()["detail"]) == (404, "'photos/nope' does not exist on pool 'pool-a'.")

    async def test_pool_that_cannot_be_asked_is_a_502(self, client, pool, s3):
        s3.meta = PermissionError("HeadObject returned 403 Forbidden")
        resp = await client.get(f"/api/explorer/pools/{pool['id']}/buckets/photos/object?key=k")
        assert resp.status_code == 502
        assert "Could not read 'photos/k' on pool 'pool-a' (10.0.0.1:9000): HeadObject returned 403" in resp.text

    async def test_key_is_required(self, client, pool, s3):
        for url in ("object", "object?key=", "download"):
            assert (await client.get(f"/api/explorer/pools/{pool['id']}/buckets/photos/{url}")).status_code == 422


class TestDownload:
    async def test_object_is_sent_as_an_attachment_and_never_rendered(self, client, pool, s3):
        s3.body = b"<script>alert(1)</script>"
        s3.meta = {**s3.meta, "size": len(s3.body)}
        resp = await client.get(
            f"/api/explorer/pools/{pool['id']}/buckets/photos/download", params={"key": '2025/relatório "final".html'}
        )
        assert resp.status_code == 200
        assert resp.content == b"<script>alert(1)</script>"
        assert resp.headers["content-type"] == "application/octet-stream"
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["cache-control"] == "no-store"
        assert resp.headers["content-disposition"] == (
            "attachment; filename=\"relat_rio__final_.html\"; filename*=UTF-8''relat%C3%B3rio%20%22final%22.html"
        )
        assert s3.calls[-1] == (
            "get_object_capped",
            "10.0.0.1:9000",
            "photos",
            '2025/relatório "final".html',
            explorer.MAX_DOWNLOAD_BYTES,
            AUTH,
        )

    @pytest.mark.parametrize(
        "key, name",
        [
            ("a/.env", "env"),
            ("..", "download"),
            ("dir/", "dir"),
            ("x" * 300, "x" * 200),
            ("a\r\nSet-Cookie: x", "a__Set-Cookie__x"),
        ],
    )
    async def test_download_name_is_safe(self, key, name):
        assert explorer._download_name(key) == name

    async def test_download_is_audited(self, client, pool, s3):
        await client.get(f"/api/explorer/pools/{pool['id']}/buckets/photos/download?key=2025/a.txt")
        entries = await audit(client, "download_object")
        assert len(entries) == 1
        assert entries[0]["entity_type"] == "object"
        import json

        assert json.loads(entries[0]["after_json"]) == {
            "pool_id": pool["id"],
            "pool": "pool-a",
            "bucket": "photos",
            "key": "2025/a.txt",
            "size": 5,
        }

    async def test_browsing_is_not_audited(self, client, pool, s3):
        await client.get(f"/api/explorer/pools/{pool['id']}/buckets")
        await client.get(f"/api/explorer/pools/{pool['id']}/buckets/photos/objects")
        await client.get(f"/api/explorer/pools/{pool['id']}/buckets/photos/object?key=k")
        assert [e["action"] for e in (await client.get("/api/audit?per_page=200")).json()] == [
            "set_credentials",
            "create",
            "create",
        ]

    async def test_object_above_the_cap_is_refused_before_it_is_fetched(self, client, pool, s3):
        s3.meta = {**s3.meta, "size": explorer.MAX_DOWNLOAD_BYTES + 1}
        resp = await client.get(f"/api/explorer/pools/{pool['id']}/buckets/photos/download?key=big.iso")
        assert resp.status_code == 413
        assert resp.json()["detail"] == (
            "'photos/big.iso' is larger than the panel's download limit (64 MiB). "
            "Fetch it with an S3 client through the gateway."
        )
        assert [c[0] for c in s3.calls] == ["head_object"]
        assert await audit(client, "download_object") == []

    async def test_object_exactly_at_the_cap_is_allowed(self, client, pool, s3):
        s3.meta = {**s3.meta, "size": explorer.MAX_DOWNLOAD_BYTES}
        resp = await client.get(f"/api/explorer/pools/{pool['id']}/buckets/photos/download?key=k")
        assert resp.status_code == 200

    async def test_object_that_turns_out_larger_than_announced_is_refused_too(self, client, pool, s3):
        """No size in the HEAD answer, or the object grew meanwhile: the capped read decides."""
        s3.meta = {**s3.meta, "size": None}
        s3.body = s3client.ObjectTooLarge("too large")
        resp = await client.get(f"/api/explorer/pools/{pool['id']}/buckets/photos/download?key=k")
        assert resp.status_code == 413
        assert await audit(client, "download_object") == []

    @pytest.mark.parametrize("gone", ["meta", "body"])
    async def test_object_that_does_not_exist_is_a_404(self, client, pool, s3, gone):
        setattr(s3, gone, None)
        resp = await client.get(f"/api/explorer/pools/{pool['id']}/buckets/photos/download?key=nope")
        assert (resp.status_code, resp.json()["detail"]) == (404, "'photos/nope' does not exist on pool 'pool-a'.")
        assert await audit(client, "download_object") == []

    @pytest.mark.parametrize("failing", ["meta", "body"])
    async def test_pool_that_cannot_be_asked_is_a_502(self, client, pool, s3, failing):
        setattr(s3, failing, ConnectionResetError("Connection reset by peer"))
        resp = await client.get(f"/api/explorer/pools/{pool['id']}/buckets/photos/download?key=k")
        assert resp.status_code == 502
        assert resp.json()["detail"] == (
            "Could not read 'photos/k' on pool 'pool-a' (10.0.0.1:9000): Connection reset by peer"
        )


# ── Which pool serves what ────────────────────────────────────────────────────


class TestLocate:
    @pytest.mark.parametrize(
        "bucket, prefix, route, pool",
        [
            ("photos", "", "/", "pool-a"),
            ("photos", "2024/", "/", "pool-a"),
            ("photos", "2025/", "/photos/2025/", "pool-b"),
            ("photos", "2025/q1/", "/photos/2025/", "pool-b"),
            ("photos", "2025/q1/deep/x/", "/photos/2025/q1/deep/", "pool-b"),
            ("photos", "2025", "/", "pool-a"),  # not inside the folder: a name that starts like it
            ("photos", "20250/", "/", "pool-a"),
            ("photos", "off/", "/", "pool-a"),  # a disabled route is not in nginx
            ("logs", "", "/logs/", "pool-b"),
            ("logs", "app/", "/logs/", "pool-b"),
            ("logs2", "", "/", "pool-a"),
            ("other", "", "/", "pool-a"),
            ("other", "2025/", "/other/2025/", "pool-b"),
        ],
    )
    async def test_most_specific_route_wins(self, client, cluster, bucket, prefix, route, pool):
        vhost = cluster["vhost"]
        resp = await client.get(
            f"/api/explorer/vhosts/{vhost['id']}/locate", params={"bucket": bucket, "prefix": prefix}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["vhost"] == {"id": vhost["id"], "server_name": "s3.test"}
        assert (body["bucket"], body["prefix"], body["route"]) == (bucket, prefix, route)
        assert body["pool"] == {"id": cluster["a" if pool == "pool-a" else "b"]["id"], "name": pool, "type": "nanio"}
        assert body["split"] is None

    async def test_vhost_that_does_not_serve_the_bucket_says_so(self, client, cluster):
        vhost = cluster["vhost"]
        assert (await client.put(f"/api/vhosts/{vhost['id']}", json={"default_pool_id": None})).status_code == 200
        resp = await client.get(f"/api/explorer/vhosts/{vhost['id']}/locate?bucket=archive")
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Vhost 's3.test' does not serve bucket 'archive': it has no route for it."
        # a bucket with a route of its own is still served
        assert (await client.get(f"/api/explorer/vhosts/{vhost['id']}/locate?bucket=logs")).status_code == 200

    async def test_unknown_vhost_is_a_404(self, client):
        resp = await client.get("/api/explorer/vhosts/99/locate?bucket=photos")
        assert (resp.status_code, resp.json()["detail"]) == (404, "Vhost not found")

    @pytest.mark.parametrize("query", ["", "bucket=", "bucket=..", "bucket=a/b"])
    async def test_bucket_is_required_and_must_be_a_bucket_name(self, client, cluster, query):
        resp = await client.get(f"/api/explorer/vhosts/{cluster['vhost']['id']}/locate?{query}")
        assert resp.status_code == 422


# ── While a migration has split routing active ────────────────────────────────


async def split(
    cluster, bucket: str, folder: str, src: str, dst: str, active: int = 1, phase: str = "verifying"
) -> int:
    from tests.api_state import sql

    await sql(
        """INSERT INTO migrations (vhost_id, bucket, folder, src_pool_id, dst_pool_id, phase, split_routing)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (cluster["vhost"]["id"], bucket, folder, cluster[src]["id"], cluster[dst]["id"], phase, active),
    )
    return (await sql("SELECT MAX(id) AS id FROM migrations"))[0]["id"]


class TestSplitRouting:
    async def test_bucket_being_moved_is_served_by_the_destination(self, client, cluster):
        """/logs/ still names pool-b, but the split sends clients to pool-a first."""
        a, b, vhost = cluster["a"], cluster["b"], cluster["vhost"]
        mid = await split(cluster, "logs", "", "b", "a")

        body = (await client.get(f"/api/explorer/vhosts/{vhost['id']}/locate?bucket=logs&prefix=app/")).json()

        assert body["route"] == "/logs/"
        assert body["pool"] == {"id": a["id"], "name": "pool-a", "type": "nanio"}
        assert body["split"] == {
            "migration_id": mid,
            "source": {"id": b["id"], "name": "pool-b"},
            "destination": {"id": a["id"], "name": "pool-a"},
        }

    async def test_folder_being_moved_is_served_by_the_destination(self, client, cluster):
        vhost = cluster["vhost"]
        await split(cluster, "photos", "2025/", "b", "a", phase="error")  # a split left by a failed move

        inside = (await client.get(f"/api/explorer/vhosts/{vhost['id']}/locate?bucket=photos&prefix=2025/x/")).json()
        assert (inside["route"], inside["pool"]["name"]) == ("/photos/2025/", "pool-a")
        assert (inside["split"]["source"]["name"], inside["split"]["destination"]["name"]) == ("pool-b", "pool-a")
        # the rest of the bucket, and a deeper folder with its own route, are not part of that split
        rest = (await client.get(f"/api/explorer/vhosts/{vhost['id']}/locate?bucket=photos&prefix=2024/")).json()
        assert (rest["pool"]["name"], rest["split"]) == ("pool-a", None)
        deep = (
            await client.get(f"/api/explorer/vhosts/{vhost['id']}/locate?bucket=photos&prefix=2025/q1/deep/")
        ).json()
        assert (deep["route"], deep["pool"]["name"], deep["split"]) == ("/photos/2025/q1/deep/", "pool-b", None)

    async def test_listing_marks_the_folder_with_the_pool_that_answers_during_the_split(self, client, cluster, s3):
        a, b, vhost = cluster["a"], cluster["b"], cluster["vhost"]
        await split(cluster, "photos", "2025/", "b", "a")
        s3.page = {"folders": ["2025/"], "objects": [], "next_token": None}

        on_a = await client.get(f"/api/explorer/pools/{a['id']}/buckets/photos/objects?vhost_id={vhost['id']}")
        on_b = await client.get(f"/api/explorer/pools/{b['id']}/buckets/photos/objects?vhost_id={vhost['id']}")

        # pool-a is where clients go first now: there the folder is an ordinary folder
        assert on_a.json()["folders"] == [{"prefix": "2025/", "routed_pool": None}]
        # on pool-b, the source, it is marked as served by pool-a
        assert on_b.json()["folders"] == [{"prefix": "2025/", "routed_pool": {"id": a["id"], "name": "pool-a"}}]

    async def test_once_the_split_is_over_the_route_decides_again(self, client, cluster):
        vhost = cluster["vhost"]
        await split(cluster, "logs", "", "b", "a", active=0, phase="done")
        body = (await client.get(f"/api/explorer/vhosts/{vhost['id']}/locate?bucket=logs")).json()
        assert (body["pool"]["name"], body["split"]) == ("pool-b", None)

    async def test_split_of_another_vhost_does_not_count(self, client, cluster):
        from tests.api_state import sql

        other = (
            await client.post("/api/vhosts", json={"server_name": "other.test", "ssl": False, "listen_port": 8082})
        ).json()
        await sql(
            """INSERT INTO migrations (vhost_id, bucket, src_pool_id, dst_pool_id, phase, split_routing)
               VALUES (?, 'logs', ?, ?, 'verifying', 1)""",
            (other["id"], cluster["b"]["id"], cluster["a"]["id"]),
        )
        body = (await client.get(f"/api/explorer/vhosts/{cluster['vhost']['id']}/locate?bucket=logs")).json()
        assert (body["pool"]["name"], body["split"]) == ("pool-b", None)
