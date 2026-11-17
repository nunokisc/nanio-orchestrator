"""End-to-end test against the development stack (compose.dev.yaml).

Real nginx, two real nanio servers, real rclone. Run inside the orchestrator container:

    docker compose -f compose.dev.yaml up -d --build --wait
    docker compose -f compose.dev.yaml exec -T orchestrator python /e2e/run.py

It drives the orchestrator through its HTTP API and talks S3 to the gateway and, to check
where data really landed, directly to each nanio server. Uses the standard library and the
orchestrator's own S3 client only.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import urllib.error
import urllib.request

from nanio_orchestrator import s3client

API = "http://127.0.0.1:8080"
API_KEY = "dev-key"
GATEWAY = "gateway:8081"
NODE_A, NODE_B = "nanio-a:9000", "nanio-b:9000"
AUTH = {"access_key": "AKDEV", "secret_key": "devsecretdevsecret"}

_step = 0


def step(title: str) -> None:
    global _step
    _step += 1
    print(f"\n[{_step}] {title}", flush=True)


def check(condition: bool, what: str) -> None:
    print(f"    {'ok  ' if condition else 'FAIL'} {what}", flush=True)
    if not condition:
        sys.exit(1)


def api(method: str, path: str, body: dict | None = None, expect: int | None = None):
    request = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Orchestrator-Key": API_KEY, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    data = json.loads(raw) if raw else None
    if expect is not None and status != expect:
        print(f"    FAIL {method} {path} answered {status}, expected {expect}: {data}", flush=True)
        sys.exit(1)
    return data if expect is not None else (status, data)


def download(path: str) -> tuple[int, dict, bytes]:
    """GET that is not JSON: status, headers (lower-cased names) and the raw body."""
    request = urllib.request.Request(API + path, headers={"X-Orchestrator-Key": API_KEY})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as error:
        return error.code, {k.lower(): v for k, v in error.headers.items()}, error.read()


def gateway_answer(path: str) -> str:
    """What an unsigned GET to the gateway gets: the HTTP status, or "connection refused"."""
    try:
        with urllib.request.urlopen(f"http://{GATEWAY}{path}", timeout=10) as response:
            return str(response.status)
    except urllib.error.HTTPError as error:
        return str(error.code)
    except urllib.error.URLError as error:
        return "connection refused" if isinstance(error.reason, ConnectionRefusedError) else f"error: {error.reason}"


def wait_for_migration(migration_id: int) -> dict:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        migration = api("GET", f"/api/migrations/{migration_id}", expect=200)
        if migration["phase"] in ("done", "error", "cancelled"):
            if migration["phase"] != "done":
                for entry in reversed(api("GET", f"/api/migrations/{migration_id}/log?limit=30", expect=200)):
                    print(f"      {entry['phase']}: {entry['message']}")
            return migration
        time.sleep(1)
    print("    FAIL migration did not finish in time")
    sys.exit(1)


async def keys(address: str, bucket: str) -> list[str]:
    return sorted(await s3client.list_objects(address, bucket, **AUTH))


async def put(address: str, bucket: str, key: str, body: bytes) -> None:
    check(await s3client.put_object(address, bucket, key, body, **AUTH), f"PUT {bucket}/{key} via {address}")


async def main() -> None:
    step("Pools, credentials, vhost")
    pools = {}
    for name, node in (("pool-a", NODE_A), ("pool-b", NODE_B)):
        pool = api("POST", "/api/pools", {"name": name, "type": "nanio"}, expect=201)
        api("POST", f"/api/pools/{pool['id']}/members", {"address": node}, expect=201)
        api("PUT", f"/api/pools/{pool['id']}/credentials", {**AUTH, "region": "us-east-1"}, expect=200)
        pools[name] = pool["id"]
    vhost = api(
        "POST",
        "/api/vhosts",
        {"server_name": "s3.dev", "listen_port": 8081, "ssl": False, "default_pool_id": pools["pool-a"]},
        expect=201,
    )
    reload = api("POST", "/api/config/reload", expect=200)
    check(reload["ok"], "nginx accepted and reloaded the generated config")

    step("A change nginx refuses is not saved")
    empty = api("POST", "/api/pools", {"name": "no-members", "type": "nanio"}, expect=201)
    status, body = api(
        "POST",
        "/api/vhosts",
        {"server_name": "broken.dev", "listen_port": 8082, "ssl": False, "default_pool_id": empty["id"]},
    )
    check(status == 422, f"a vhost proxying to a pool without members is refused (got {status})")
    check("nothing was saved" in (body or {}).get("detail", ""), "the answer says nothing was saved")
    names = [v["server_name"] for v in api("GET", "/api/vhosts", expect=200)]
    check(names == ["s3.dev"], f"the vhost was not created: {names}")
    check(api("POST", "/api/config/validate", expect=200)["ok"], "the configuration on disk is still valid")
    api("DELETE", f"/api/pools/{empty['id']}", expect=204)

    step("A bucket on the default pool, through the gateway")
    ok, message = await s3client.create_bucket(GATEWAY, "photos", **AUTH)
    check(ok, f"CreateBucket photos through the gateway ({message})")
    for key in ("2024/a.txt", "2025/b.txt", "2025/deep/c.txt"):
        await put(GATEWAY, "photos", key, key.encode())
    check(await keys(NODE_A, "photos") == ["2024/a.txt", "2025/b.txt", "2025/deep/c.txt"], "objects are on pool-a")

    step("Move folder 2025/ of photos to pool-b (live migration by folder)")
    migration = api(
        "POST", "/api/migrations", {"bucket": "photos", "folder": "2025", "dst_pool_id": pools["pool-b"]}, expect=201
    )
    migration = wait_for_migration(migration["id"])
    check(migration["phase"] == "done", f"migration finished: {migration['phase']} {migration.get('error_msg') or ''}")
    check(await keys(NODE_B, "photos") == ["2025/b.txt", "2025/deep/c.txt"], "the folder's objects are on pool-b")
    routes = {r["path_prefix"]: r["pool_name"] for r in api("GET", f"/api/vhosts/{vhost['id']}/routes", expect=200)}
    check(routes.get("/photos/2025/") == "pool-b", "route /photos/2025/ points at pool-b")
    check(
        await s3client.get_object(GATEWAY, "photos", "2025/deep/c.txt", **AUTH) == b"2025/deep/c.txt",
        "read via gateway",
    )
    await put(GATEWAY, "photos", "2025/new.txt", b"new")
    check("2025/new.txt" in await keys(NODE_B, "photos"), "a new object in the folder lands on pool-b")
    check("2025/new.txt" not in await keys(NODE_A, "photos"), "and not on pool-a")
    await put(GATEWAY, "photos", "2024/more.txt", b"more")
    check("2024/more.txt" in await keys(NODE_A, "photos"), "the rest of the bucket still lands on pool-a")
    inside = await s3client.count_objects(GATEWAY, "photos", prefix="2025/", **AUTH)
    check(inside == 3, f"listing inside the folder through the gateway returns its 3 objects (got {inside})")
    check(
        await s3client.get_object(NODE_A, "photos", "2025/.nanio-shard", **AUTH) is not None,
        "marker object exists on pool-a",
    )

    step("The explorer shows which pool holds what")
    a, b = pools["pool-a"], pools["pool-b"]
    for name, pool_id in pools.items():
        listed = api("GET", f"/api/explorer/pools/{pool_id}/buckets", expect=200)
        check("photos" in listed["buckets"], f"bucket photos exists on {name}")
    root = api("GET", f"/api/explorer/pools/{a}/buckets/photos/objects?vhost_id={vhost['id']}", expect=200)
    served_by = {f["prefix"]: (f["routed_pool"] or {}).get("name") for f in root["folders"]}
    check(
        served_by == {"2024/": None, "2025/": "pool-b"},
        f"seen through the vhost on pool-a, folder 2025/ is flagged as served by pool-b: {served_by}",
    )
    plain = api("GET", f"/api/explorer/pools/{a}/buckets/photos/objects", expect=200)
    check(
        [f for f in plain["folders"] if f["prefix"] == "2025/"] == [{"prefix": "2025/", "routed_pool": None}],
        "the marker keeps 2025/ visible on pool-a; without a vhost nothing is flagged",
    )
    inside = api("GET", f"/api/explorer/pools/{b}/buckets/photos/objects?prefix=2025/", expect=200)
    check(
        {o["key"]: o["size"] for o in inside["objects"]} == {"2025/b.txt": 10, "2025/new.txt": 3}
        and [f["prefix"] for f in inside["folders"]] == ["2025/deep/"],
        f"pool-b reports the folder's objects, one level at a time: {inside['objects']} {inside['folders']}",
    )
    check(
        inside["pool"]["name"] == "pool-b" and inside["next_token"] is None,
        "the listing names its pool and is complete",
    )
    page = api("GET", f"/api/explorer/pools/{b}/buckets/photos/objects?prefix=2025/&limit=1", expect=200)
    check(page["next_token"] is not None, "a short page gives a token to continue with")
    where = api("GET", f"/api/explorer/vhosts/{vhost['id']}/locate?bucket=photos&prefix=2025/deep/", expect=200)
    check(
        (where["pool"]["name"], where["route"]) == ("pool-b", "/photos/2025/"),
        f"locate: photos/2025/deep/ is served by pool-b through /photos/2025/ ({where['pool']['name']}, {where['route']})",
    )
    where = api("GET", f"/api/explorer/vhosts/{vhost['id']}/locate?bucket=photos&prefix=2024/", expect=200)
    check(
        (where["pool"]["name"], where["route"]) == ("pool-a", "/"),
        "locate: the rest of the bucket is on the default pool",
    )
    meta = api("GET", f"/api/explorer/pools/{b}/buckets/photos/object?key=2025/deep/c.txt", expect=200)
    check(meta["size"] == 15 and meta["last_modified"] and meta["etag"], f"object details from pool-b: {meta}")
    status, headers, body = download(f"/api/explorer/pools/{b}/buckets/photos/download?key=2025/deep/c.txt")
    check(
        status == 200
        and body == b"2025/deep/c.txt"
        and headers.get("content-disposition", "").startswith("attachment"),
        f"download from pool-b is the object, sent as an attachment (got {status})",
    )
    status, _ = api("GET", f"/api/explorer/pools/{b}/buckets/photos/object?key=2024/a.txt")
    check(status == 404, f"an object that is only on pool-a is a 404 on pool-b (got {status})")
    status, _ = api("GET", f"/api/explorer/pools/{b}/buckets/no-such-bucket/objects")
    check(status == 404, f"a bucket that does not exist is a 404, not an empty listing (got {status})")

    step("Routing over a folder that has data is refused")
    status, body = api(
        "POST", f"/api/vhosts/{vhost['id']}/routes", {"path_prefix": "/photos/2024/", "pool_id": pools["pool-b"]}
    )
    check(status == 409, f"409 for a route over a non-empty folder (got {status})")

    step("Move a whole bucket to pool-b")
    ok, _ = await s3client.create_bucket(GATEWAY, "docs", **AUTH)
    for key in ("one.txt", "two.txt", "sub/three.txt"):
        await put(GATEWAY, "docs", key, key.encode())
    api("POST", f"/api/vhosts/{vhost['id']}/routes", {"path_prefix": "/docs/", "pool_id": pools["pool-a"]}, expect=201)
    api("POST", "/api/config/reload", expect=200)
    migration = api(
        "POST",
        "/api/migrations",
        {"bucket": "docs", "src_pool_id": pools["pool-a"], "dst_pool_id": pools["pool-b"]},
        expect=201,
    )
    migration = wait_for_migration(migration["id"])
    check(migration["phase"] == "done", f"migration finished: {migration['phase']} {migration.get('error_msg') or ''}")
    check(await keys(NODE_B, "docs") == ["one.txt", "sub/three.txt", "two.txt"], "all objects are on pool-b")
    check(await s3client.get_object(GATEWAY, "docs", "sub/three.txt", **AUTH) == b"sub/three.txt", "read via gateway")
    await put(GATEWAY, "docs", "after.txt", b"after")
    check("after.txt" in await keys(NODE_B, "docs"), "new objects land on pool-b")
    check(await keys(NODE_A, "docs") == ["one.txt", "sub/three.txt", "two.txt"], "the source copy was left in place")

    step("The route of a bucket that has objects on its pool cannot be removed")
    routes = {r["path_prefix"]: r for r in api("GET", f"/api/vhosts/{vhost['id']}/routes", expect=200)}
    for path in (
        f"/api/vhosts/{vhost['id']}/routes/{routes['/docs/']['id']}",
        f"/api/vhosts/{vhost['id']}/buckets/docs/route",
    ):
        status, body = api("DELETE", path)
        check(status == 409, f"DELETE {path.rsplit('/', 2)[-2]}/… is refused with 409 (got {status})")
        check("has objects on pool 'pool-b'" in (body or {}).get("detail", ""), "the answer says where the objects are")
    status, body = api("PUT", f"/api/vhosts/{vhost['id']}/routes/{routes['/docs/']['id']}", {"enabled": False})
    check(status == 409, f"disabling the route is refused with 409 too (got {status})")
    still = [r["path_prefix"] for r in api("GET", f"/api/vhosts/{vhost['id']}/routes", expect=200) if r["enabled"]]
    check("/docs/" in still, "the route is still there and enabled")
    check(await s3client.get_object(GATEWAY, "docs", "one.txt", **AUTH) == b"one.txt", "and the bucket is still served")

    step("ListBuckets through the gateway shows the bucket that now lives on pool-b")
    api("POST", f"/api/vhosts/{vhost['id']}/buckets/sync", expect=200)
    listed = sorted(b["name"] for b in await s3client.list_buckets(GATEWAY, **AUTH))
    check(listed == ["docs", "photos"], f"ListBuckets: {listed}")

    step("Move the folder back into its bucket")
    migration = api(
        "POST", "/api/migrations", {"bucket": "photos", "folder": "2025/", "dst_pool_id": pools["pool-a"]}, expect=201
    )
    migration = wait_for_migration(migration["id"])
    check(migration["phase"] == "done", f"migration finished: {migration['phase']} {migration.get('error_msg') or ''}")
    routes = [r["path_prefix"] for r in api("GET", f"/api/vhosts/{vhost['id']}/routes", expect=200)]
    check("/photos/2025/" not in routes, "the folder route is gone")
    check(
        await s3client.get_object(GATEWAY, "photos", "2025/new.txt", **AUTH) == b"new",
        "its objects are served again by pool-a",
    )
    await put(GATEWAY, "photos", "2025/back.txt", b"back")
    check("2025/back.txt" in await keys(NODE_A, "photos"), "new objects in the folder land on pool-a")

    step("A disabled vhost is not served")
    check(gateway_answer("/photos/2024/a.txt") == "403", "before: the gateway answers (403, unsigned request)")
    disabled = api("PUT", f"/api/vhosts/{vhost['id']}", {"enabled": False}, expect=200)
    check(disabled["enabled"] is False, "the vhost is disabled")
    status_now = api("GET", "/api/config/status", expect=200)
    check(
        status_now["pending_reload"] is True
        and [f["change"] for f in status_now["pending_files"] if f["path"].endswith("/s3.dev.conf")] == ["removed"],
        f"it is a pending change: s3.dev.conf removed ({status_now['pending_files']})",
    )
    check(gateway_answer("/photos/2024/a.txt") == "403", "until nginx is reloaded it is still served")
    check(api("POST", "/api/config/reload", expect=200)["ok"], "nginx reloaded")
    time.sleep(1)
    answer = gateway_answer("/photos/2024/a.txt")
    check(answer == "connection refused", f"after the reload nothing listens on the vhost's port ({answer})")
    dry = api("POST", "/api/config/rebuild-from-disk?dry_run=true", expect=200)
    check(
        [(v["server_name"], v["enabled"]) for v in dry["vhosts"]] == [("s3.dev", False)],
        f"its server block is still on disk and would be rebuilt as disabled ({dry['vhosts']})",
    )
    enabled = api("PUT", f"/api/vhosts/{vhost['id']}", {"enabled": True}, expect=200)
    check(enabled["enabled"] is True, "the vhost is enabled again")
    check(api("POST", "/api/config/reload", expect=200)["ok"], "nginx reloaded")
    time.sleep(1)
    check(
        await s3client.get_object(GATEWAY, "photos", "2024/a.txt", **AUTH) == b"2024/a.txt",
        "after the reload the gateway serves it again",
    )
    check(api("GET", "/api/config/status", expect=200)["pending_reload"] is False, "nothing is left waiting")

    step("Health")
    health = api("GET", "/api/health", expect=200)
    check(health["status"] == "ok" and health["drift_alerts"] == 0, f"healthy, no drift: {health}")

    print("\nEnd-to-end test passed.")


if __name__ == "__main__":
    asyncio.run(main())
