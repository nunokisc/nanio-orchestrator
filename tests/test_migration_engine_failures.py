"""What the migration engine does when a step cannot be carried out: refusals before any
data moves, S3 probes that fail, nginx rejecting a config, and the http vhosts that follow."""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.conftest import create_member, create_pool, create_route, create_vhost
from tests.test_migration_engine_rclone import (
    ScriptedRclone,
    log_messages,
    route_pool_id,
    setup_bucket,
    use_settings,
    vhost_conf,
    wait_terminal,
)

pytestmark = pytest.mark.usefixtures("set_secret", "ensure_migrations_dir")


@pytest.fixture
def scripted():
    fake = ScriptedRclone()
    with patch("asyncio.create_subprocess_exec", new=fake):
        yield fake


async def _start(client, vhost, bucket, src, dst, route=None, folder="") -> dict:
    """Start a migration through the engine, past the API's own validation, and wait for it."""
    from nanio_orchestrator.migration_engine import start_migration

    mig_id = await start_migration(
        vhost["id"], bucket, src["id"], dst["id"], route_id=route["id"] if route else None, folder=folder
    )
    return await wait_terminal(client, mig_id)


async def _migrate(client, bucket, src, dst) -> dict:
    resp = await client.post(
        "/api/migrations", json={"bucket": bucket, "src_pool_id": src["id"], "dst_pool_id": dst["id"]}
    )
    assert resp.status_code == 201, resp.text
    return await wait_terminal(client, resp.json()["id"])


async def _query(sql: str, params=()) -> list[dict]:
    from nanio_orchestrator.db import get_db_ctx

    async with get_db_ctx() as db:
        return [dict(r) for r in await db.execute_fetchall(sql, params)]


async def _execute(sql: str, params=()) -> int:
    from nanio_orchestrator.db import get_db_ctx

    async with get_db_ctx() as db:
        cursor = await db.execute(sql, params)
        await db.commit()
        return cursor.lastrowid


def _reject_config_after_split(mock_nginx, server_name: str, when=lambda conf: True) -> None:
    """nginx -t accepts everything up to and including the split; once the split has been
    live, it rejects the first later config for which `when(conf)` holds, and all after it."""
    seen = {"split": False, "rejecting": False}

    async def test_config(*_args, **_kwargs):
        conf = vhost_conf(server_name)
        if "LIVE MIGRATION" in conf:
            seen["split"] = True
        elif seen["split"] and when(conf):
            seen["rejecting"] = True
        if seen["rejecting"]:
            return MagicMock(ok=False, output="nginx: [emerg] rejected")
        return MagicMock(ok=True, output="syntax ok")

    mock_nginx["test_config"].side_effect = test_config


# ── Refused before anything is copied ─────────────────────────────────────────


class TestRefusedBeforeCopying:
    async def test_unknown_migration_is_reported_and_nothing_is_created(self, client, mock_nginx, scripted, caplog):
        from nanio_orchestrator.migration_engine import run_migration

        with caplog.at_level(logging.ERROR, logger="nanio_orchestrator.migration_engine"):
            await run_migration(4242)

        assert "Migration 4242 not found" in caplog.text
        assert await _query("SELECT id FROM migrations") == []
        assert scripted.commands == []

    async def test_source_and_destination_must_differ(self, client, mock_nginx, mock_s3, scripted):
        src, _, vhost, route = await setup_bucket(client, mock_s3)

        mig = await _start(client, vhost, "bk", src, src, route)

        assert mig["phase"] == "error"
        assert "source and destination are the same pool" in mig["error_msg"]
        assert mig["error_msg"] in await log_messages(client, mig["id"])
        assert scripted.commands == []

    async def test_bucket_that_disappeared_from_its_pool_is_not_migrated(self, client, mock_nginx, mock_s3, scripted):
        src, dst, vhost, route = await setup_bucket(client, mock_s3)
        await _execute("UPDATE bucket_sync SET status = 'deleted' WHERE bucket = 'bk'")

        mig = await _start(client, vhost, "bk", src, dst, route)

        assert mig["phase"] == "error"
        assert mig["error_msg"] == "Bucket 'bk' is marked as deleted — cannot migrate"
        assert scripted.commands == []

    async def test_split_left_by_another_move_blocks_a_different_destination(
        self, client, mock_nginx, mock_s3, scripted
    ):
        """The destination of the unfinished move holds client writes; sending the bucket
        anywhere else would leave them behind."""
        src, dst, vhost, route = await setup_bucket(client, mock_s3)
        other = await create_pool(client, "eng-other")
        await create_member(client, other["id"], "10.0.0.3:9000")
        unfinished = await _execute(
            """INSERT INTO migrations (vhost_id, bucket, src_pool_id, dst_pool_id, phase, split_routing)
               VALUES (?, 'bk', ?, ?, 'error', 1)""",
            (vhost["id"], src["id"], dst["id"]),
        )

        mig = await _start(client, vhost, "bk", src, other, route)

        assert mig["phase"] == "error"
        assert f"Split routing from migration {unfinished} (pool {src['id']} → {dst['id']})" in mig["error_msg"]
        assert mig["split_routing"] is False
        assert (await client.get(f"/api/migrations/{unfinished}")).json()["split_routing"] is True
        assert scripted.commands == []

    async def test_empty_source_is_refused(self, client, mock_nginx, mock_s3, scripted):
        src, dst, vhost, route = await setup_bucket(client, mock_s3)
        mock_s3["count_objects"].return_value = 0

        mig = await _start(client, vhost, "bk", src, dst, route)

        assert mig["phase"] == "error"
        assert mig["error_msg"].startswith("Refusing to migrate: source bucket 'bk' is empty.")
        assert scripted.commands == []
        mock_s3["create_bucket"].assert_not_called()

    async def test_destination_bucket_that_cannot_be_created_ends_the_migration(
        self, client, mock_nginx, mock_s3, scripted
    ):
        src, dst, vhost, route = await setup_bucket(client, mock_s3)
        mock_s3["create_bucket"].return_value = (False, "AccessDenied")

        mig = await _start(client, vhost, "bk", src, dst, route)

        assert mig["phase"] == "error"
        assert mig["error_msg"] == "Failed to create destination bucket 'bk': AccessDenied"
        assert scripted.commands == []

    async def test_unexpected_failure_is_recorded_and_audited(self, client, mock_nginx, mock_s3, scripted):
        src, dst, vhost, route = await setup_bucket(client, mock_s3)
        broken = AsyncMock(side_effect=RuntimeError("cannot decrypt pool credentials"))

        with patch("nanio_orchestrator.migration_engine.get_pool_credentials", new=broken):
            mig = await _start(client, vhost, "bk", src, dst, route)

        assert mig["phase"] == "error"
        assert mig["error_msg"] == "cannot decrypt pool credentials"
        assert "cannot decrypt pool credentials" in await log_messages(client, mig["id"])
        audit = await _query(
            "SELECT after_json FROM audit_log WHERE action = 'migration_error' AND entity_id = ?", (mig["id"],)
        )
        assert [json.loads(a["after_json"]) for a in audit] == [{"error": "cannot decrypt pool credentials"}]
        assert await route_pool_id(route["id"]) == src["id"]


# ── S3 probes that fail are not fatal ─────────────────────────────────────────


class TestProbesUnavailable:
    async def test_source_count_unavailable_before_copying(self, client, mock_nginx, mock_s3, scripted):
        """The orchestrator's own listing may be denied while rclone, with the pool
        credentials, works: the empty-source guard is skipped, the migration goes on."""
        src, dst, vhost, route = await setup_bucket(client, mock_s3)
        mock_s3["count_objects"].side_effect = [PermissionError("403 Forbidden"), 5, 5]

        mig = await _start(client, vhost, "bk", src, dst, route)

        assert mig["phase"] == "done", mig
        log = await log_messages(client, mig["id"])
        assert (
            "Pre-flight object count unavailable (403 Forbidden). "
            "Proceeding — rclone uses its own pool credentials." in log
        )
        assert await route_pool_id(route["id"]) == dst["id"]

    async def test_destination_bucket_check_unavailable(self, client, mock_nginx, mock_s3, scripted):
        src, dst, vhost, route = await setup_bucket(client, mock_s3)
        mock_s3["bucket_exists"].side_effect = RuntimeError("timed out")

        mig = await _start(client, vhost, "bk", src, dst, route)

        assert mig["phase"] == "done", mig
        assert "Destination bucket pre-check unavailable (timed out) — proceeding" in await log_messages(
            client, mig["id"]
        )
        mock_s3["create_bucket"].assert_not_called()

    async def test_object_counts_unavailable_after_a_copy_pass(self, client, mock_nginx, mock_s3, scripted):
        """Convergence cannot be measured: stop the bulk passes and let verification decide."""
        src, dst, vhost, route = await setup_bucket(client, mock_s3)
        mock_s3["count_objects"].side_effect = [5, RuntimeError("listing failed")]

        mig = await _start(client, vhost, "bk", src, dst, route)

        assert mig["phase"] == "done", mig
        assert "Object count unavailable (listing failed) — entering write-routing" in await log_messages(
            client, mig["id"]
        )
        assert len(scripted.copies()) == 2  # one bulk pass, one verification pass

    async def test_pools_without_enabled_members_are_not_probed(
        self, client, mock_nginx, mock_s3, scripted, monkeypatch
    ):
        """Members disabled after the migration was accepted: there is nobody to ask, so every
        configured bulk pass runs and rclone's own check has the last word."""
        use_settings(monkeypatch, migration_max_copy_passes=2)
        src, dst, vhost, route = await setup_bucket(client, mock_s3)
        await _execute("UPDATE pool_members SET enabled = 0")
        for name in ("count_objects", "bucket_exists", "bucket_has_objects", "create_bucket"):
            mock_s3[name].reset_mock()

        mig = await _start(client, vhost, "bk", src, dst, route)

        assert mig["phase"] == "done", mig
        for name in ("count_objects", "bucket_exists", "bucket_has_objects", "create_bucket"):
            mock_s3[name].assert_not_called()
        assert len(scripted.copies()) == 3  # two bulk passes + the verification pass
        assert "endpoint" not in scripted.configs[0]


# ── Switching ─────────────────────────────────────────────────────────────────


class TestSwitching:
    async def test_migration_without_route_id_switches_the_bucket_route(self, client, mock_nginx, mock_s3, scripted):
        """Migrations recorded before route_id existed find their route by path."""
        src, dst, vhost, route = await setup_bucket(client, mock_s3)

        mig = await _start(client, vhost, "bk", src, dst)

        assert mig["phase"] == "done", mig
        assert await route_pool_id(route["id"]) == dst["id"]
        assert "proxy_pass         http://eng-dst;" in vhost_conf("eng.example.com")

    async def test_missing_route_is_an_error_not_a_silent_success(self, client, mock_nginx, mock_s3, scripted):
        src, dst, vhost, route = await setup_bucket(client, mock_s3)
        await _execute("DELETE FROM routes WHERE id = ?", (route["id"],))

        mig = await _start(client, vhost, "bk", src, dst)

        assert mig["phase"] == "error"
        assert mig["error_msg"].startswith("No nginx route found to update for bucket 'bk'")
        assert "nginx was NOT updated" in mig["error_msg"]
        assert mig["orphaned_source_pool_id"] is None  # the source still serves the bucket
        bucket_sync = await _query("SELECT routed_pool_id FROM bucket_sync WHERE bucket = 'bk'")
        assert bucket_sync[0]["routed_pool_id"] != dst["id"]

    @pytest.mark.parametrize("with_route_id", [True, False])
    async def test_rejected_final_config_rolls_the_route_back_and_keeps_the_split(
        self, client, mock_nginx, mock_s3, scripted, with_route_id
    ):
        src, dst, vhost, route = await setup_bucket(client, mock_s3)
        _reject_config_after_split(mock_nginx, "eng.example.com")

        mig = await _start(client, vhost, "bk", src, dst, route if with_route_id else None)

        assert mig["phase"] == "error"
        assert mig["error_msg"] == "nginx test failed during switching: nginx: [emerg] rejected"
        assert "nginx test failed: nginx: [emerg] rejected — rolling back route" in await log_messages(
            client, mig["id"]
        )
        # DB and disk agree again: route on the source, split still serving the bucket
        assert await route_pool_id(route["id"]) == src["id"]
        assert mig["split_routing"] is True
        assert "LIVE MIGRATION → eng-dst" in vhost_conf("eng.example.com")
        assert mig["orphaned_source_pool_id"] is None
        bucket_sync = await _query("SELECT routed_pool_id FROM bucket_sync WHERE bucket = 'bk'")
        assert bucket_sync[0]["routed_pool_id"] != dst["id"]

    async def test_rejected_final_config_of_a_folder_move_leaves_the_bucket_alone(
        self, client, mock_nginx, mock_s3, scripted
    ):
        src, dst, _, _ = await setup_bucket(client, mock_s3)
        before = await _query("SELECT status, routed_pool_id FROM bucket_sync WHERE bucket = 'bk'")
        _reject_config_after_split(mock_nginx, "eng.example.com")

        resp = await client.post("/api/migrations", json={"bucket": "bk", "folder": "2025/", "dst_pool_id": dst["id"]})
        assert resp.status_code == 201, resp.text
        mig = await wait_terminal(client, resp.json()["id"])

        assert mig["phase"] == "error"
        assert mig["error_msg"].startswith("nginx test failed during switching")
        assert mig["split_routing"] is True
        routes = await _query("SELECT pool_id FROM routes WHERE path_prefix = '/bk/2025/'")
        assert [r["pool_id"] for r in routes] == [src["id"]]
        assert await _query("SELECT status, routed_pool_id FROM bucket_sync WHERE bucket = 'bk'") == before


# ── After a folder has moved ──────────────────────────────────────────────────


async def _folder_on_other_pool(client, mock_s3):
    """Bucket `bk` on eng-src with its folder 2025/ already routed to eng-dst."""
    src, dst, vhost, _ = await setup_bucket(client, mock_s3)
    resp = await client.post(
        f"/api/vhosts/{vhost['id']}/routes", json={"path_prefix": "/bk/2025/", "pool_id": dst["id"]}
    )
    assert resp.status_code == 201, resp.text
    return src, dst, vhost


async def _move_folder(client, dst) -> dict:
    resp = await client.post("/api/migrations", json={"bucket": "bk", "folder": "2025/", "dst_pool_id": dst["id"]})
    assert resp.status_code == 201, resp.text
    return await wait_terminal(client, resp.json()["id"])


class TestFolderAftermath:
    async def test_marker_that_cannot_be_written_is_only_a_warning(self, client, mock_nginx, mock_s3, scripted):
        _, dst, _, _ = await setup_bucket(client, mock_s3)
        mock_s3["put_object"].return_value = False

        mig = await _move_folder(client, dst)

        assert mig["phase"] == "done", mig
        warnings = [m for m in await log_messages(client, mig["id"]) if m.startswith("WARNING: The marker")]
        assert len(warnings) == 1
        assert "'2025/.nanio-shard' could not be written to bucket 'bk' on pool 'eng-src'" in warnings[0]

    async def test_folder_back_home_with_config_rejected_is_reported(self, client, mock_nginx, mock_s3, scripted):
        """The move itself is complete; only dropping the now-pointless route failed."""
        src, _, _ = await _folder_on_other_pool(client, mock_s3)
        _reject_config_after_split(mock_nginx, "eng.example.com", when=lambda conf: "location /bk/2025/" not in conf)

        mig = await _move_folder(client, src)

        assert mig["phase"] == "done", mig
        assert (
            "WARNING: nginx test failed while removing the folder route: nginx: [emerg] rejected"
            in await log_messages(client, mig["id"])
        )
        # the rejected config was not left on disk
        assert "location /bk/2025/" in vhost_conf("eng.example.com")

    async def test_folder_back_home_with_marker_that_cannot_be_removed(self, client, mock_nginx, mock_s3, scripted):
        src, _, _ = await _folder_on_other_pool(client, mock_s3)
        mock_s3["delete_object"].side_effect = RuntimeError("connection reset")

        mig = await _move_folder(client, src)

        assert mig["phase"] == "done", mig
        log = await log_messages(client, mig["id"])
        assert "WARNING: could not remove the marker '2025/.nanio-shard': connection reset" in log
        assert "'/bk/2025/' is back on the bucket's own pool — its folder route was removed" in log
        assert await _query("SELECT id FROM routes WHERE path_prefix = '/bk/2025/'") == []
        assert "location /bk/2025/" not in vhost_conf("eng.example.com")

    async def test_folder_back_home_on_a_pool_without_members_skips_the_marker(self, client, mock_nginx, mock_s3):
        from nanio_orchestrator.migration_engine import _finish_folder_move

        home = await create_pool(client, "home")
        vhost = await create_vhost(client, "home.example.com", default_pool_id=home["id"])
        route_id = await _execute(
            "INSERT INTO routes (vhost_id, path_prefix, pool_id, enabled) VALUES (?, '/bk/2025/', ?, 1)",
            (vhost["id"], home["id"]),
        )

        notes = await _finish_folder_move(vhost["id"], route_id, "/bk/2025/", home["id"])

        assert notes == ["'/bk/2025/' is back on the bucket's own pool — its folder route was removed"]
        assert await route_pool_id(route_id) is None
        mock_s3["delete_object"].assert_not_called()


# ── http vhosts following the move ────────────────────────────────────────────


async def _linked_http_vhost(client):
    """nanio pools hs-src → hs-dst, each with an http pool serving its files, and an http
    vhost routing /bk/ to the source's http pool. Returns ids needed by the cascade."""
    src = await create_pool(client, "hs-src")
    await create_member(client, src["id"], "10.0.0.1:9000")
    dst = await create_pool(client, "hs-dst")
    await create_member(client, dst["id"], "10.0.0.2:9000")
    web_src = await create_pool(client, "web-src", pool_type="http", source_nanio_pool_id=src["id"])
    await create_member(client, web_src["id"], "10.0.1.1:80", role="primary")
    web_dst = await create_pool(client, "web-dst", pool_type="http", source_nanio_pool_id=dst["id"])
    await create_member(client, web_dst["id"], "10.0.1.2:80", role="primary")
    vhost = await create_vhost(client, "s3.example.com", default_pool_id=src["id"])
    cdn = await create_vhost(client, "cdn.example.com", default_pool_id=web_src["id"])
    route = await create_route(client, cdn["id"], "bk", web_src["id"])
    mig_id = await _execute(
        """INSERT INTO migrations (vhost_id, bucket, src_pool_id, dst_pool_id, phase, split_routing)
           VALUES (?, 'bk', ?, ?, 'write_routing', 1)""",
        (vhost["id"], src["id"], dst["id"]),
    )
    return {"src": src, "dst": dst, "web_src": web_src, "web_dst": web_dst, "route": route, "mig_id": mig_id}


class TestHttpCascadeFailures:
    async def test_split_rejected_by_nginx_leaves_the_http_vhost_as_it_was(self, client, mock_nginx):
        from nanio_orchestrator.migration_engine import _cascade_http_write_routing

        ids = await _linked_http_vhost(client)
        before = vhost_conf("cdn.example.com")
        mock_nginx["test_config"].return_value = MagicMock(ok=False, output="nginx: [emerg] rejected")

        await _cascade_http_write_routing(ids["mig_id"], "bk", ids["src"]["id"], ids["dst"]["id"])

        assert vhost_conf("cdn.example.com") == before
        assert (
            "WARNING: HTTP vhost 'cdn.example.com': nginx test failed — nginx: [emerg] rejected. "
            "Vhost remains without split-routing." in await log_messages(client, ids["mig_id"])
        )

    async def test_split_that_cannot_be_generated_is_a_warning(self, client, mock_nginx):
        from nanio_orchestrator.migration_engine import _cascade_http_write_routing

        ids = await _linked_http_vhost(client)
        before = vhost_conf("cdn.example.com")
        broken = AsyncMock(side_effect=RuntimeError("template error"))

        with patch("nanio_orchestrator.migration_engine.generate_vhost_config", new=broken):
            await _cascade_http_write_routing(ids["mig_id"], "bk", ids["src"]["id"], ids["dst"]["id"])

        assert vhost_conf("cdn.example.com") == before
        assert (
            "WARNING: HTTP vhost 'cdn.example.com': cascade write_routing failed — template error."
            in await log_messages(client, ids["mig_id"])
        )

    async def test_switch_rejected_by_nginx_rolls_the_http_route_back(self, client, mock_nginx):
        from nanio_orchestrator.migration_engine import _cascade_http_switching

        ids = await _linked_http_vhost(client)
        before = vhost_conf("cdn.example.com")
        mock_nginx["test_config"].return_value = MagicMock(ok=False, output="nginx: [emerg] rejected")

        swept = await _cascade_http_switching(ids["mig_id"], "bk", ids["src"]["id"], ids["dst"]["id"])

        assert swept == []
        assert await route_pool_id(ids["route"]["id"]) == ids["web_src"]["id"]
        assert vhost_conf("cdn.example.com") == before
        assert (
            "WARNING: HTTP vhost 'cdn.example.com': nginx test failed — route rolled back. "
            "Primary migration is still done." in await log_messages(client, ids["mig_id"])
        )

    async def test_switch_that_cannot_be_generated_is_a_warning_and_not_swept(self, client, mock_nginx):
        from nanio_orchestrator.migration_engine import _cascade_http_switching

        ids = await _linked_http_vhost(client)
        before = vhost_conf("cdn.example.com")
        broken = AsyncMock(side_effect=RuntimeError("template error"))

        with patch("nanio_orchestrator.migration_engine.generate_vhost_config", new=broken):
            swept = await _cascade_http_switching(ids["mig_id"], "bk", ids["src"]["id"], ids["dst"]["id"])

        assert swept == []
        assert vhost_conf("cdn.example.com") == before
        assert (
            "WARNING: HTTP vhost 'cdn.example.com': cascade switching failed — template error. "
            "Primary migration is still done." in await log_messages(client, ids["mig_id"])
        )
