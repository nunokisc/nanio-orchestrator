"""Buckets API: listing, promotion, migration start, orphan clean-up and the bucket routes of
http vhosts — every refusal leaves routes, bucket records and nginx files as they were."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from tests.api_state import audit, conf, managed_files, routes, sql
from tests.conftest import create_member, create_pool, create_route, create_vhost


async def _cluster(client, default=True):
    pa = await create_pool(client, "pa")
    await create_member(client, pa["id"], "10.0.0.1:9000")
    pb = await create_pool(client, "pb")
    await create_member(client, pb["id"], "10.0.0.2:9000")
    vhost = await create_vhost(client, "s3.example.com", **({"default_pool_id": pa["id"]} if default else {}))
    return pa, pb, vhost


async def _track(vhost, bucket, status, pool=None, discovered_at="2026-01-01T00:00:00Z"):
    await sql(
        "INSERT INTO bucket_sync (vhost_id, bucket, discovered_at, status, routed_pool_id) VALUES (?, ?, ?, ?, ?)",
        (vhost["id"], bucket, discovered_at, status, pool["id"] if pool else None),
    )


async def _tracked(vhost):
    return {
        r["bucket"]: (r["status"], r["routed_pool_id"])
        for r in await sql("SELECT bucket, status, routed_pool_id FROM bucket_sync WHERE vhost_id = ?", (vhost["id"],))
    }


async def _disable_members(client, pool):
    for member in (await client.get(f"/api/pools/{pool['id']}/members")).json():
        resp = await client.put(f"/api/pools/{pool['id']}/members/{member['id']}", json={"enabled": False})
        assert resp.status_code == 200


async def _snapshot(client, tmp_dirs, vhost):
    return await routes(client, vhost), await _tracked(vhost), managed_files(tmp_dirs)


def _vhost_conf(tmp_dirs, name="s3.example.com"):
    return conf(tmp_dirs, "vhosts", name).read_text()


class TestListBuckets:
    async def test_unknown_vhost(self, client):
        assert (await client.get("/api/vhosts/999/buckets")).status_code == 404

    async def test_default_pool_without_an_enabled_member_is_refused(self, client, mock_nginx, mock_s3):
        pa, _pb, vhost = await _cluster(client)
        await _disable_members(client, pa)

        resp = await client.get(f"/api/vhosts/{vhost['id']}/buckets")

        assert resp.status_code == 400
        assert resp.json()["detail"] == f"Pool {pa['id']} has no enabled members"

    async def test_objects_are_counted_only_on_request_and_only_for_buckets_still_on_the_default_pool(
        self, client, mock_nginx, mock_s3
    ):
        _pa, pb, vhost = await _cluster(client)
        await _track(vhost, "fresh", "unrouted", discovered_at="2026-01-03T00:00:00Z")
        await _track(vhost, "moving", "migrating", discovered_at="2026-01-05T00:00:00Z")
        await _track(vhost, "moved", "routed", pool=pb, discovered_at="2026-01-04T00:00:00Z")
        await _track(vhost, "noise", "ignored", discovered_at="2026-01-02T00:00:00Z")

        async def count(address, bucket, **_):
            if bucket == "moving":
                raise OSError("connection reset")
            return 3

        mock_s3["count_objects"].side_effect = count

        plain = await client.get(f"/api/vhosts/{vhost['id']}/buckets")

        assert plain.status_code == 200
        mock_s3["count_objects"].assert_not_awaited()
        assert {b["name"]: b["object_count"] for b in plain.json()["buckets"]} == dict.fromkeys(
            ("fresh", "moving", "moved", "noise")
        )

        counted = await client.get(f"/api/vhosts/{vhost['id']}/buckets?fetch_counts=true")

        assert counted.status_code == 200
        assert sorted(c.args for c in mock_s3["count_objects"].call_args_list) == [
            ("10.0.0.1:9000", "fresh"),
            ("10.0.0.1:9000", "moving"),
        ]
        by_name = {b["name"]: b for b in counted.json()["buckets"]}
        # a count that failed is simply unknown
        assert {name: b["object_count"] for name, b in by_name.items()} == {
            "fresh": 3,
            "moving": None,
            "moved": None,
            "noise": None,
        }
        assert (by_name["moved"]["pool_name"], by_name["moved"]["routed_pool_id"]) == ("pb", pb["id"])
        assert counted.json()["last_synced_at"] == "2026-01-05T00:00:00Z"


class TestSync:
    async def test_unknown_vhost(self, client, mock_s3):
        resp = await client.post("/api/vhosts/999/buckets/sync")

        assert resp.status_code == 404
        mock_s3["list_buckets"].assert_not_awaited()

    async def test_outcome_is_recorded_in_the_audit_log(self, client, mock_nginx, mock_s3):
        _pa, _pb, vhost = await _cluster(client)
        mock_s3["list_buckets"].return_value = [{"name": "photos"}, {"name": "videos"}]

        resp = await client.post(f"/api/vhosts/{vhost['id']}/buckets/sync")

        assert resp.status_code == 200
        assert resp.json()["buckets_found"] == 2
        (entry,) = await audit(client, "sync_buckets")
        assert entry["entity_id"] == vhost["id"]
        assert json.loads(entry["after_json"]) == resp.json()


class TestPromote:
    async def _refused(self, client, tmp_dirs, vhost, body, status, bucket="photos"):
        before = await _snapshot(client, tmp_dirs, vhost)
        resp = await client.post(f"/api/vhosts/{vhost['id']}/buckets/{bucket}/promote", json=body)
        assert resp.status_code == status, resp.text
        assert await _snapshot(client, tmp_dirs, vhost) == before
        assert await sql("SELECT id FROM migrations") == []
        return resp.json()["detail"]

    async def test_unknown_vhost(self, client, mock_s3):
        resp = await client.post("/api/vhosts/999/buckets/photos/promote", json={"pool_id": 1})

        assert resp.status_code == 404
        assert await sql("SELECT id FROM routes") == []

    async def test_vhost_without_a_default_pool_is_refused(self, client, mock_nginx, mock_s3, tmp_dirs):
        _pa, pb, vhost = await _cluster(client, default=False)

        detail = await self._refused(client, tmp_dirs, vhost, {"pool_id": pb["id"]}, 400)

        assert detail == "Vhost has no default_pool_id configured"

    async def test_unknown_pool_is_refused(self, client, mock_nginx, mock_s3, tmp_dirs):
        _pa, _pb, vhost = await _cluster(client)

        detail = await self._refused(client, tmp_dirs, vhost, {"pool_id": 999}, 400)

        assert detail == "Pool 999 not found"

    async def test_bucket_that_already_has_a_route_is_a_conflict(self, client, mock_nginx, mock_s3, tmp_dirs):
        pa, pb, vhost = await _cluster(client)
        await create_route(client, vhost["id"], "photos", pa["id"])

        detail = await self._refused(client, tmp_dirs, vhost, {"pool_id": pb["id"]}, 409)

        assert detail == "Route /photos/ already exists for this vhost"

    async def test_migrating_to_the_default_pool_is_refused(self, client, mock_nginx, mock_s3, tmp_dirs):
        pa, _pb, vhost = await _cluster(client)

        detail = await self._refused(client, tmp_dirs, vhost, {"pool_id": pa["id"], "migrate": True}, 400)

        assert "already lives there" in detail

    async def test_migrating_to_a_pool_without_members_is_refused(self, client, mock_nginx, mock_s3, tmp_dirs):
        _pa, _pb, vhost = await _cluster(client)
        empty = await create_pool(client, "pc")

        detail = await self._refused(client, tmp_dirs, vhost, {"pool_id": empty["id"], "migrate": True}, 400)

        assert detail == "Target pool has no enabled members"

    async def test_migrating_is_refused_while_every_migration_slot_is_taken(
        self, client, mock_nginx, mock_s3, tmp_dirs
    ):
        _pa, pb, vhost = await _cluster(client)
        start = AsyncMock(return_value=7)

        with (
            patch("nanio_orchestrator.api.buckets.get_active_count", return_value=2),
            patch("nanio_orchestrator.api.buckets.engine_start_migration", new=start),
        ):
            detail = await self._refused(client, tmp_dirs, vhost, {"pool_id": pb["id"], "migrate": True}, 429)

        assert "Max parallel migrations reached (2)" in detail
        start.assert_not_awaited()

    async def test_default_pool_without_an_enabled_member_is_refused(self, client, mock_nginx, mock_s3, tmp_dirs):
        pa, pb, vhost = await _cluster(client)
        await _disable_members(client, pa)

        detail = await self._refused(client, tmp_dirs, vhost, {"pool_id": pb["id"]}, 400)

        assert detail == f"Pool {pa['id']} has no enabled members"
        mock_s3["promote_src_has_objects"].assert_not_awaited()

    async def test_allow_orphan_routes_the_bucket_away_from_its_objects(self, client, mock_nginx, mock_s3, tmp_dirs):
        _pa, pb, vhost = await _cluster(client)
        mock_s3["promote_src_has_objects"].return_value = True

        resp = await client.post(
            f"/api/vhosts/{vhost['id']}/buckets/photos/promote", json={"pool_id": pb["id"], "allow_orphan": True}
        )

        assert resp.status_code == 200
        assert (resp.json()["pool"], resp.json()["route"], resp.json()["migration_started"]) == (
            "pb",
            "/photos/",
            False,
        )
        assert mock_s3["promote_src_has_objects"].call_args.args == ("10.0.0.1:9000", "photos")
        assert await routes(client, vhost) == {"/photos/": "pb", "/": "pa"}
        assert await _tracked(vhost) == {"photos": ("routed", pb["id"])}
        assert "proxy_pass         http://pb;" in _vhost_conf(tmp_dirs).split("location /photos/ {")[1].split("}")[0]
        mock_s3["create_bucket"].assert_not_awaited()

    async def test_route_to_the_default_pool_itself_hides_nothing(self, client, mock_nginx, mock_s3, tmp_dirs):
        pa, _pb, vhost = await _cluster(client)
        mock_s3["promote_src_has_objects"].return_value = True

        resp = await client.post(f"/api/vhosts/{vhost['id']}/buckets/photos/promote", json={"pool_id": pa["id"]})

        assert resp.status_code == 200
        assert await routes(client, vhost) == {"/photos/": "pa", "/": "pa"}
        assert await _tracked(vhost) == {"photos": ("routed", pa["id"])}

    async def test_unreachable_source_is_not_taken_for_an_empty_one(self, client, mock_nginx, mock_s3, tmp_dirs):
        """Routing a bucket elsewhere without a migration hides whatever it holds; when the
        pool cannot say what it holds, the answer is no."""
        _pa, pb, vhost = await _cluster(client)
        mock_s3["promote_src_has_objects"].side_effect = OSError("connection refused")
        before = (await routes(client, vhost), managed_files(tmp_dirs))

        resp = await client.post(f"/api/vhosts/{vhost['id']}/buckets/photos/promote", json={"pool_id": pb["id"]})

        assert resp.status_code == 502
        assert "connection refused" in resp.json()["detail"] and "allow_orphan" in resp.json()["detail"]
        assert (await routes(client, vhost), managed_files(tmp_dirs)) == before

    async def test_unreachable_source_can_be_left_behind_on_purpose(self, client, mock_nginx, mock_s3, tmp_dirs):
        _pa, pb, vhost = await _cluster(client)
        mock_s3["promote_src_has_objects"].side_effect = OSError("connection refused")

        resp = await client.post(
            f"/api/vhosts/{vhost['id']}/buckets/photos/promote", json={"pool_id": pb["id"], "allow_orphan": True}
        )

        assert resp.status_code == 200
        assert await routes(client, vhost) == {"/photos/": "pb", "/": "pa"}

    async def test_unreachable_source_does_not_stop_a_promote_that_migrates(
        self, client, mock_nginx, mock_s3, tmp_dirs
    ):
        """The migration checks the source itself, and the route stays on it until then."""
        _pa, pb, vhost = await _cluster(client)
        mock_s3["promote_src_has_objects"].side_effect = OSError("connection refused")
        started = AsyncMock(return_value=41)

        with patch("nanio_orchestrator.api.buckets.engine_start_migration", new=started):
            resp = await client.post(
                f"/api/vhosts/{vhost['id']}/buckets/photos/promote", json={"pool_id": pb["id"], "migrate": True}
            )

        assert resp.status_code == 200 and resp.json()["migration_id"] == 41
        assert await routes(client, vhost) == {"/photos/": "pa", "/": "pa"}


class TestIgnore:
    async def test_unknown_vhost(self, client):
        resp = await client.post("/api/vhosts/999/buckets/photos/ignore")

        assert resp.status_code == 404
        assert await sql("SELECT id FROM bucket_sync") == []

    async def test_bucket_never_seen_before_is_recorded_as_ignored(self, client, mock_nginx):
        _pa, _pb, vhost = await _cluster(client)

        resp = await client.post(f"/api/vhosts/{vhost['id']}/buckets/scratch/ignore")

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "bucket": "scratch", "status": "ignored"}
        assert await _tracked(vhost) == {"scratch": ("ignored", None)}
        (entry,) = await audit(client, "ignore_bucket")
        assert json.loads(entry["after_json"]) == {"vhost_id": vhost["id"], "bucket": "scratch"}


class TestStartMigration:
    """POST /api/vhosts/{id}/buckets/{bucket}/migrate: copy a routed bucket from the default pool."""

    async def _refused(self, client, tmp_dirs, vhost, status, active=0):
        start = AsyncMock(return_value=7)
        before = await _snapshot(client, tmp_dirs, vhost)
        with (
            patch("nanio_orchestrator.api.buckets.get_active_count", return_value=active),
            patch("nanio_orchestrator.api.buckets.engine_start_migration", new=start),
        ):
            resp = await client.post(f"/api/vhosts/{vhost['id']}/buckets/photos/migrate")
        assert resp.status_code == status, resp.text
        start.assert_not_awaited()
        assert await _snapshot(client, tmp_dirs, vhost) == before
        assert await audit(client, "start_migration") == []
        return resp.json()["detail"]

    async def test_unknown_vhost(self, client):
        assert (await client.post("/api/vhosts/999/buckets/photos/migrate")).status_code == 404

    async def test_vhost_without_a_default_pool_is_refused(self, client, mock_nginx, tmp_dirs):
        _pa, _pb, vhost = await _cluster(client, default=False)

        assert await self._refused(client, tmp_dirs, vhost, 400) == "Vhost has no default_pool_id configured"

    async def test_bucket_without_a_route_is_refused(self, client, mock_nginx, tmp_dirs):
        _pa, _pb, vhost = await _cluster(client)

        assert "promote first" in await self._refused(client, tmp_dirs, vhost, 400)

    async def test_bucket_that_no_longer_exists_is_refused(self, client, mock_nginx, tmp_dirs):
        _pa, pb, vhost = await _cluster(client)
        await create_route(client, vhost["id"], "photos", pb["id"])
        await _track(vhost, "photos", "deleted", pool=pb)

        assert "no longer exists on source pool" in await self._refused(client, tmp_dirs, vhost, 400)

    async def test_refused_while_every_migration_slot_is_taken(self, client, mock_nginx, tmp_dirs):
        _pa, pb, vhost = await _cluster(client)
        await create_route(client, vhost["id"], "photos", pb["id"])

        detail = await self._refused(client, tmp_dirs, vhost, 429, active=2)

        assert "Max parallel migrations reached (2)" in detail

    async def test_bucket_routed_to_the_default_pool_has_nothing_to_migrate(self, client, mock_nginx, tmp_dirs):
        pa, _pb, vhost = await _cluster(client)
        await create_route(client, vhost["id"], "photos", pa["id"])

        assert "source and destination are the same pool" in await self._refused(client, tmp_dirs, vhost, 400)

    async def test_copies_from_the_default_pool_to_the_pool_of_the_route(self, client, mock_nginx, tmp_dirs):
        pa, pb, vhost = await _cluster(client)
        await create_route(client, vhost["id"], "photos", pb["id"])
        await _track(vhost, "photos", "routed", pool=pb)
        start = AsyncMock(return_value=7)

        with patch("nanio_orchestrator.api.buckets.engine_start_migration", new=start):
            resp = await client.post(f"/api/vhosts/{vhost['id']}/buckets/photos/migrate")

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "migration_id": 7, "bucket": "photos"}
        assert start.await_args.args == (vhost["id"], "photos", pa["id"], pb["id"])
        (entry,) = await audit(client, "start_migration")
        assert entry["entity_id"] == 7
        assert json.loads(entry["after_json"]) == {
            "vhost_id": vhost["id"],
            "bucket": "photos",
            "src_pool_id": pa["id"],
            "dst_pool_id": pb["id"],
        }
        assert await routes(client, vhost) == {"/photos/": "pb", "/": "pa"}


class TestOrphans:
    async def test_bucket_that_cannot_be_counted_is_left_out_of_the_scan(self, client, mock_nginx, mock_s3):
        _pa, pb, vhost = await _cluster(client)
        for bucket in ("broken", "clean", "stale"):
            await _track(vhost, bucket, "routed", pool=pb)

        async def count(address, bucket, **_):
            if bucket == "broken":
                raise OSError("connection reset")
            return {"clean": 0, "stale": 4}[bucket]

        mock_s3["count_objects"].side_effect = count

        resp = await client.get(f"/api/vhosts/{vhost['id']}/buckets/orphans")

        assert resp.status_code == 200
        assert resp.json() == {"vhost_id": vhost["id"], "orphans": [{"bucket": "stale", "objects": 4}], "checked": 3}
        assert {c.args[0] for c in mock_s3["count_objects"].call_args_list} == {"10.0.0.1:9000"}

    async def test_purge_stops_when_the_bucket_cannot_be_listed(self, client, mock_nginx, mock_s3):
        _pa, pb, vhost = await _cluster(client)
        await _track(vhost, "stale", "routed", pool=pb)
        mock_s3["list_objects"].side_effect = OSError("connection reset")

        resp = await client.post(f"/api/vhosts/{vhost['id']}/buckets/stale/purge-orphan")

        assert resp.status_code == 500
        assert resp.json()["detail"] == "Failed to list objects in bucket 'stale': connection reset"
        assert await audit(client, "purge_orphan") == []

    async def test_purge_reports_the_objects_it_could_not_delete(self, client, mock_nginx, mock_s3):
        _pa, pb, vhost = await _cluster(client)
        await _track(vhost, "stale", "routed", pool=pb)
        outcomes = {"kept-refused": False, "kept-error": OSError("connection reset"), "gone": True}
        mock_s3["list_objects"].return_value = list(outcomes)

        async def delete(address, bucket, key, **_):
            if isinstance(outcomes[key], Exception):
                raise outcomes[key]
            return outcomes[key]

        with patch("nanio_orchestrator.api.buckets.delete_object", new=AsyncMock(side_effect=delete)) as deleted:
            resp = await client.post(f"/api/vhosts/{vhost['id']}/buckets/stale/purge-orphan")

        assert resp.status_code == 200
        assert resp.json() == {
            "ok": False,
            "bucket": "stale",
            "deleted": 1,
            "total": 3,
            "errors": ["kept-refused", "kept-error"],
        }
        # the default pool's copy, never the pool the bucket is routed to
        assert [c.args for c in deleted.call_args_list] == [("10.0.0.1:9000", "stale", key) for key in outcomes]
        (entry,) = await audit(client, "purge_orphan")
        assert json.loads(entry["after_json"]) == {
            "vhost_id": vhost["id"],
            "bucket": "stale",
            "deleted": 1,
            "total": 3,
            "errors": 2,
        }


class TestRemoveBucketRoute:
    async def test_unknown_vhost(self, client):
        assert (await client.delete("/api/vhosts/999/buckets/photos/route")).status_code == 404

    async def test_finished_migration_of_the_route_is_kept_without_it(self, client, mock_nginx, tmp_dirs):
        pa, pb, vhost = await _cluster(client)
        route = await create_route(client, vhost["id"], "photos", pb["id"])
        await _track(vhost, "photos", "routed", pool=pb)
        await sql(
            """INSERT INTO migrations (vhost_id, bucket, src_pool_id, dst_pool_id, phase, route_id)
               VALUES (?, 'photos', ?, ?, 'done', ?)""",
            (vhost["id"], pa["id"], pb["id"], route["id"]),
        )

        resp = await client.delete(f"/api/vhosts/{vhost['id']}/buckets/photos/route")

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "bucket": "photos", "route_removed": "/photos/"}
        assert await routes(client, vhost) == {"/": "pa"}
        assert "location /photos/ {" not in _vhost_conf(tmp_dirs)
        assert await _tracked(vhost) == {"photos": ("unrouted", None)}
        assert await sql("SELECT phase, route_id FROM migrations") == [{"phase": "done", "route_id": None}]
        assert [e["entity_id"] for e in await audit(client, "remove_bucket_route")] == [route["id"]]


class TestHttpBucketRoutes:
    async def _cdn(self, client, source=True):
        pa = await create_pool(client, "pa")
        await create_member(client, pa["id"], "10.0.0.1:9000")
        web = await create_pool(client, "web", pool_type="http", source_nanio_pool_id=pa["id"] if source else None)
        await create_member(client, web["id"], "10.0.1.1:80", role="primary")
        cdn = await create_vhost(client, "cdn.example.com", default_pool_id=web["id"])
        return pa, web, cdn

    async def test_unknown_vhost(self, client):
        assert (await client.get("/api/vhosts/999/http-bucket-routes")).status_code == 404
        assert (await client.post("/api/vhosts/999/http-bucket-routes/photos")).status_code == 404
        assert (await client.delete("/api/vhosts/999/http-bucket-routes/photos")).status_code == 404
        assert await sql("SELECT id FROM routes") == []

    @pytest.mark.parametrize("kind", ["s3 vhost", "http vhost not linked to an s3 pool", "vhost without default pool"])
    async def test_only_for_http_vhosts_that_serve_the_files_of_an_s3_pool(self, client, mock_nginx, tmp_dirs, kind):
        if kind == "s3 vhost":
            pa, _pb, vhost = await _cluster(client)
            await create_route(client, vhost["id"], "photos", pa["id"])
        elif kind == "http vhost not linked to an s3 pool":
            _pa, web, vhost = await self._cdn(client, source=False)
            await create_route(client, vhost["id"], "photos", web["id"])
        else:
            pa, _pb, vhost = await _cluster(client, default=False)
            await create_route(client, vhost["id"], "photos", pa["id"])
        before = await _snapshot(client, tmp_dirs, vhost)

        listed = await client.get(f"/api/vhosts/{vhost['id']}/http-bucket-routes")
        added = await client.post(f"/api/vhosts/{vhost['id']}/http-bucket-routes/videos")
        removed = await client.delete(f"/api/vhosts/{vhost['id']}/http-bucket-routes/photos")

        assert [r.status_code for r in (listed, added, removed)] == [400, 400, 400]
        assert {r.json()["detail"] for r in (listed, added, removed)} == {
            "Route management for http vhosts is only available when source_nanio_pool_id is configured"
        }
        assert await _snapshot(client, tmp_dirs, vhost) == before

    async def test_no_buckets_are_offered_when_no_vhost_serves_the_linked_pool(self, client, mock_nginx):
        pa, web, cdn = await self._cdn(client)

        resp = await client.get(f"/api/vhosts/{cdn['id']}/http-bucket-routes")

        assert resp.status_code == 200
        assert resp.json() == {
            "vhost_id": cdn["id"],
            "server_name": "cdn.example.com",
            "source_nanio_pool_id": pa["id"],
            "source_nanio_pool_name": "pa",
            "routes": [],
            "linked_nanio_buckets": [],
        }
        assert web["source_nanio_pool_id"] == pa["id"]

    async def test_offers_the_live_buckets_of_the_linked_pool_and_marks_the_routed_ones(self, client, mock_nginx):
        pa, web, cdn = await self._cdn(client)
        s3 = await create_vhost(client, "s3.example.com", default_pool_id=pa["id"])
        for bucket, status in (("photos", "unrouted"), ("videos", "routed"), ("gone", "deleted"), ("noise", "ignored")):
            await _track(s3, bucket, status)
        added = await client.post(f"/api/vhosts/{cdn['id']}/http-bucket-routes/photos")
        assert added.status_code == 201

        resp = await client.get(f"/api/vhosts/{cdn['id']}/http-bucket-routes")

        assert resp.status_code == 200
        assert [(r["path_prefix"], r["bucket"], r["pool_id"], r["pool_name"]) for r in resp.json()["routes"]] == [
            ("/photos/", "photos", web["id"], "web")
        ]
        assert resp.json()["linked_nanio_buckets"] == [
            {"bucket": "photos", "has_route": True},
            {"bucket": "videos", "has_route": False},
        ]
        (entry,) = await audit(client, "add_http_bucket_route")
        assert json.loads(entry["after_json"]) == {"vhost_id": cdn["id"], "bucket": "photos", "pool_id": web["id"]}
