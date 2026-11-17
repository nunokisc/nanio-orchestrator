"""A disabled vhost is not served: its server block is kept as ``<name>.conf.disabled``, which
nginx does not include, and everything needed to rebuild it stays on disk."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from nanio_orchestrator.nginx import applied
from nanio_orchestrator.nginx.generator import other_vhost_variant, vhost_config_path, vhost_config_paths

from tests.api_state import refuse, sidecar, sql
from tests.conftest import create_member, create_pool, create_vhost

NAME = "s3.example.com"


def vhost_files(tmp_dirs) -> list[str]:
    """Names of the server-block files in the managed vhosts directory."""
    return sorted(p.name for p in (Path(tmp_dirs["nginx_dir"]) / "vhosts").iterdir() if ".conf" in p.name)


async def recorded(prefix: str = "vhosts") -> list[str]:
    rows = await sql("SELECT path FROM config_files ORDER BY path")
    return [Path(r["path"]).name for r in rows if Path(r["path"]).parent.name == prefix]


async def cluster(client, **vhost) -> dict:
    """pool-a (default) and pool-b, one vhost with a bucket route to pool-b."""
    a = await create_pool(client, "pool-a")
    await create_member(client, a["id"], "10.0.0.1:9000")
    b = await create_pool(client, "pool-b")
    await create_member(client, b["id"], "10.0.0.2:9000")
    v = await create_vhost(client, NAME, listen_port=8081, default_pool_id=a["id"], **vhost)
    await sql("INSERT INTO routes (vhost_id, path_prefix, pool_id) VALUES (?, '/photos/', ?)", (v["id"], b["id"]))
    assert (await client.put(f"/api/vhosts/{v['id']}", json={"listen_port": 8081})).status_code == 200
    return {"a": a, "b": b, "vhost": v}


async def set_enabled(client, vhost: dict, enabled: bool):
    return await client.put(f"/api/vhosts/{vhost['id']}", json={"enabled": enabled})


class TestPaths:
    def test_one_helper_names_both_files(self, app, tmp_dirs):
        vhosts = Path(tmp_dirs["nginx_dir"]) / "vhosts"
        assert vhost_config_path(NAME) == str(vhosts / f"{NAME}.conf")
        assert vhost_config_path(NAME, enabled=False) == str(vhosts / f"{NAME}.conf.disabled")
        assert vhost_config_paths(NAME) == [vhost_config_path(NAME, True), vhost_config_path(NAME, False)]

    def test_each_file_knows_its_other_variant(self, app):
        on, off = vhost_config_paths(NAME)
        assert other_vhost_variant(on) == off
        assert other_vhost_variant(off) == on


class TestToggle:
    async def test_disabling_moves_the_server_block_out_of_what_nginx_includes(self, client, mock_nginx, tmp_dirs):
        c = await cluster(client)
        conf = Path(vhost_config_path(NAME))
        before = conf.read_text()
        mock_nginx["test_config"].reset_mock()

        resp = await set_enabled(client, c["vhost"], False)

        assert resp.status_code == 200
        assert resp.json()["enabled"] is False
        assert vhost_files(tmp_dirs) == [f"{NAME}.conf.disabled"]
        disabled = Path(vhost_config_path(NAME, enabled=False))
        # the same server block, route included: only the name says that it is not served
        assert disabled.read_text().split("\n", 2)[2] == before.split("\n", 2)[2]
        assert "location /photos/" in disabled.read_text()
        assert await recorded() == [f"{NAME}.conf.disabled"]
        mock_nginx["test_config"].assert_awaited_once()  # one change, judged once
        mock_nginx["reload_nginx"].assert_not_awaited()
        assert json.loads(sidecar(tmp_dirs, "vhosts", NAME).read_text())["enabled"] is False
        assert (await client.get(f"/api/vhosts/{c['vhost']['id']}")).json()["enabled"] is False

    async def test_enabling_brings_it_back(self, client, mock_nginx, tmp_dirs):
        c = await cluster(client)
        await set_enabled(client, c["vhost"], False)

        resp = await set_enabled(client, c["vhost"], True)

        assert resp.status_code == 200 and resp.json()["enabled"] is True
        assert vhost_files(tmp_dirs) == [f"{NAME}.conf"]
        assert await recorded() == [f"{NAME}.conf"]
        assert json.loads(sidecar(tmp_dirs, "vhosts", NAME).read_text())["enabled"] is True

    async def test_toggle_is_a_pending_change_until_nginx_is_reloaded(self, client, tmp_dirs):
        """With the dev-mode nginx (a dry run that succeeds), through the real write path."""
        c = await cluster(client)
        await client.post("/api/config/reload")
        assert (await client.get("/api/config/status")).json()["pending_reload"] is False

        await set_enabled(client, c["vhost"], False)
        status = (await client.get("/api/config/status")).json()
        assert status["pending_reload"] is True
        assert status["pending_files"] == [{"path": vhost_config_path(NAME), "change": "removed"}]
        # the file nginx does not load is not part of what it has to load
        assert vhost_config_path(NAME, enabled=False) not in applied.managed_hashes()

        await client.post("/api/config/reload")
        assert (await client.get("/api/config/status")).json()["pending_reload"] is False

        await set_enabled(client, c["vhost"], True)
        status = (await client.get("/api/config/status")).json()
        assert status["pending_files"] == [{"path": vhost_config_path(NAME), "change": "added"}]

    @pytest.mark.parametrize("enabled", [False, True])
    async def test_toggle_that_nginx_refuses_leaves_everything_as_it_was(self, client, mock_nginx, tmp_dirs, enabled):
        c = await cluster(client)
        if enabled:
            await set_enabled(client, c["vhost"], False)
        files = vhost_files(tmp_dirs)
        content = {name: (Path(tmp_dirs["nginx_dir"]) / "vhosts" / name).read_text() for name in files}
        rows = await recorded()
        refuse(mock_nginx, "nginx: [emerg] host not found in upstream")

        resp = await set_enabled(client, c["vhost"], enabled)

        assert resp.status_code == 422
        assert "nothing was saved" in resp.json()["detail"]
        assert vhost_files(tmp_dirs) == files
        assert {name: (Path(tmp_dirs["nginx_dir"]) / "vhosts" / name).read_text() for name in files} == content
        assert await recorded() == rows
        assert (await client.get(f"/api/vhosts/{c['vhost']['id']}")).json()["enabled"] is (not enabled)
        assert json.loads(sidecar(tmp_dirs, "vhosts", NAME).read_text())["enabled"] is (not enabled)

    async def test_vhost_created_disabled_is_never_written_as_conf(self, client, mock_nginx, tmp_dirs):
        pool = await create_pool(client, "pool-a")
        await create_member(client, pool["id"], "10.0.0.1:9000")
        await create_vhost(client, NAME, default_pool_id=pool["id"], enabled=False)
        assert vhost_files(tmp_dirs) == [f"{NAME}.conf.disabled"]

    async def test_editing_a_disabled_vhost_keeps_it_disabled(self, client, mock_nginx, tmp_dirs):
        c = await cluster(client)
        await set_enabled(client, c["vhost"], False)

        assert (await client.put(f"/api/vhosts/{c['vhost']['id']}", json={"listen_port": 9443})).status_code == 200

        assert vhost_files(tmp_dirs) == [f"{NAME}.conf.disabled"]
        assert "listen 9443" in Path(vhost_config_path(NAME, enabled=False)).read_text()

    async def test_route_change_on_a_disabled_vhost_is_written_to_its_file(self, client, mock_nginx, tmp_dirs):
        c = await cluster(client)
        await set_enabled(client, c["vhost"], False)

        resp = await client.post(
            f"/api/vhosts/{c['vhost']['id']}/routes", json={"path_prefix": "/logs/", "pool_id": c["a"]["id"]}
        )

        assert resp.status_code == 201
        assert vhost_files(tmp_dirs) == [f"{NAME}.conf.disabled"]
        assert "location /logs/" in Path(vhost_config_path(NAME, enabled=False)).read_text()


class TestRefusedWhileMoving:
    @pytest.mark.parametrize(
        "phase, split", [("copying", 0), ("write_routing", 1), ("switching", 1), ("error", 1), ("cancelled", 1)]
    )
    async def test_vhost_is_not_disabled_while_a_bucket_is_being_moved(
        self, client, mock_nginx, tmp_dirs, phase, split
    ):
        c = await cluster(client)
        await sql(
            """INSERT INTO migrations (vhost_id, bucket, src_pool_id, dst_pool_id, phase, split_routing)
               VALUES (?, 'photos', ?, ?, ?, ?)""",
            (c["vhost"]["id"], c["b"]["id"], c["a"]["id"], phase, split),
        )
        mid = (await sql("SELECT id FROM migrations"))[0]["id"]

        resp = await set_enabled(client, c["vhost"], False)

        assert resp.status_code == 409
        assert resp.json()["detail"] == (
            f"Migration {mid} is still moving bucket 'photos' of this vhost. Finish or cancel it first."
        )
        assert vhost_files(tmp_dirs) == [f"{NAME}.conf"]
        assert (await client.get(f"/api/vhosts/{c['vhost']['id']}")).json()["enabled"] is True

    @pytest.mark.parametrize("phase", ["done", "error", "cancelled"])
    async def test_finished_migration_without_a_split_does_not_block(self, client, mock_nginx, phase):
        c = await cluster(client)
        await sql(
            """INSERT INTO migrations (vhost_id, bucket, src_pool_id, dst_pool_id, phase, split_routing)
               VALUES (?, 'photos', ?, ?, ?, 0)""",
            (c["vhost"]["id"], c["b"]["id"], c["a"]["id"], phase),
        )
        assert (await set_enabled(client, c["vhost"], False)).status_code == 200

    async def test_enabling_is_never_blocked_by_a_migration(self, client, mock_nginx):
        c = await cluster(client)
        await set_enabled(client, c["vhost"], False)
        await sql(
            """INSERT INTO migrations (vhost_id, bucket, src_pool_id, dst_pool_id, phase, split_routing)
               VALUES (?, 'photos', ?, ?, 'error', 1)""",
            (c["vhost"]["id"], c["b"]["id"], c["a"]["id"]),
        )
        assert (await set_enabled(client, c["vhost"], True)).status_code == 200


class TestRenameAndDelete:
    async def test_renaming_a_disabled_vhost_moves_its_disabled_file(self, client, mock_nginx, tmp_dirs):
        c = await cluster(client)
        await set_enabled(client, c["vhost"], False)

        resp = await client.put(f"/api/vhosts/{c['vhost']['id']}", json={"server_name": "new.example.com"})

        assert resp.status_code == 200
        assert vhost_files(tmp_dirs) == ["new.example.com.conf.disabled"]
        assert await recorded() == ["new.example.com.conf.disabled"]
        assert "server_name new.example.com;" in Path(vhost_config_path("new.example.com", False)).read_text()
        assert not sidecar(tmp_dirs, "vhosts", NAME).exists()
        assert json.loads(sidecar(tmp_dirs, "vhosts", "new.example.com").read_text())["enabled"] is False

    async def test_renaming_and_enabling_at_once_is_one_change(self, client, mock_nginx, tmp_dirs):
        c = await cluster(client)
        await set_enabled(client, c["vhost"], False)
        mock_nginx["test_config"].reset_mock()

        resp = await client.put(
            f"/api/vhosts/{c['vhost']['id']}", json={"server_name": "new.example.com", "enabled": True}
        )

        assert resp.status_code == 200
        assert vhost_files(tmp_dirs) == ["new.example.com.conf"]
        assert await recorded() == ["new.example.com.conf"]
        mock_nginx["test_config"].assert_awaited_once()

    async def test_refused_rename_of_a_disabled_vhost_keeps_the_old_file(self, client, mock_nginx, tmp_dirs):
        c = await cluster(client)
        await set_enabled(client, c["vhost"], False)
        refuse(mock_nginx)

        resp = await client.put(f"/api/vhosts/{c['vhost']['id']}", json={"server_name": "new.example.com"})

        assert resp.status_code == 422
        assert vhost_files(tmp_dirs) == [f"{NAME}.conf.disabled"]
        assert await recorded() == [f"{NAME}.conf.disabled"]

    @pytest.mark.parametrize("enabled", [True, False])
    async def test_deleting_a_vhost_removes_whichever_file_it_has(self, client, mock_nginx, tmp_dirs, enabled):
        c = await cluster(client)
        if not enabled:
            await set_enabled(client, c["vhost"], False)

        assert (await client.delete(f"/api/vhosts/{c['vhost']['id']}")).status_code == 204

        assert vhost_files(tmp_dirs) == []
        assert await recorded() == []
        assert not sidecar(tmp_dirs, "vhosts", NAME).exists()


class TestConfigScreen:
    async def test_status_lists_the_disabled_file_and_notices_a_hand_edit(self, client, mock_nginx, tmp_dirs):
        from nanio_orchestrator.drift import check_drift_once

        c = await cluster(client)
        await set_enabled(client, c["vhost"], False)
        disabled = Path(vhost_config_path(NAME, enabled=False))
        assert await check_drift_once() == []

        disabled.write_text(disabled.read_text() + "# edited by hand\n")

        drifted = await check_drift_once()
        assert [(d["path"], d["status"]) for d in drifted] == [(str(disabled), "modified")]
        status = (await client.get("/api/config/status")).json()
        assert [(Path(f["path"]).name, f["drifted"]) for f in status["files"] if "vhosts" in f["path"]] == [
            (f"{NAME}.conf.disabled", True)
        ]
        assert (await client.get("/api/health")).json()["drift_alerts"] == 1

        compared = (await client.get("/api/config/file", params={"path": str(disabled)})).json()
        assert compared["drifted"] is True and compared["content"].endswith("# edited by hand\n")

    async def test_drifted_disabled_file_can_be_absorbed(self, client, mock_nginx, tmp_dirs):
        c = await cluster(client)
        await set_enabled(client, c["vhost"], False)
        disabled = Path(vhost_config_path(NAME, enabled=False))
        disabled.write_text(disabled.read_text() + "# edited by hand\n")

        resp = await client.post("/api/config/absorb-file", json={"path": str(disabled)})

        assert resp.status_code == 200
        status = (await client.get("/api/config/status")).json()
        assert [f["drifted"] for f in status["files"] if f["path"] == str(disabled)] == [False]

    async def test_drifted_disabled_file_is_rewritten_as_a_disabled_file(self, client, mock_nginx, tmp_dirs):
        c = await cluster(client)
        await set_enabled(client, c["vhost"], False)
        disabled = Path(vhost_config_path(NAME, enabled=False))
        disabled.write_text(disabled.read_text() + "# edited by hand\n")

        resp = await client.post("/api/config/rewrite-file", json={"path": str(disabled)})

        assert resp.json()["ok"] is True and resp.json()["path"] == str(disabled)
        assert "edited by hand" not in disabled.read_text()
        assert vhost_files(tmp_dirs) == [f"{NAME}.conf.disabled"]

    async def test_rewrite_puts_back_the_right_variant_when_the_file_was_renamed_by_hand(
        self, client, mock_nginx, tmp_dirs
    ):
        """Someone re-enabled the vhost by renaming its file: the database still says disabled."""
        c = await cluster(client)
        await set_enabled(client, c["vhost"], False)
        disabled = Path(vhost_config_path(NAME, enabled=False))
        by_hand = Path(vhost_config_path(NAME))
        disabled.rename(by_hand)

        resp = await client.post("/api/config/rewrite-file", json={"path": str(by_hand)})

        assert resp.json()["ok"] is True
        assert vhost_files(tmp_dirs) == [f"{NAME}.conf.disabled"]
        assert await recorded() == [f"{NAME}.conf.disabled"]

    async def test_rewrite_refused_by_nginx_restores_both_files(self, client, mock_nginx, tmp_dirs):
        c = await cluster(client)
        await set_enabled(client, c["vhost"], False)
        disabled = Path(vhost_config_path(NAME, enabled=False))
        by_hand = Path(vhost_config_path(NAME))
        by_hand.write_text("# managed by nanio-orchestrator\nserver { listen 1; }\n")
        refuse(mock_nginx)

        resp = await client.post("/api/config/rewrite-file", json={"path": str(disabled)})

        assert resp.json()["ok"] is False
        assert vhost_files(tmp_dirs) == [f"{NAME}.conf", f"{NAME}.conf.disabled"]
        assert by_hand.read_text() == "# managed by nanio-orchestrator\nserver { listen 1; }\n"

    @pytest.mark.parametrize(
        "name", ["x.conf.disabled.bak", "x.disabled", "x.meta.json", "x.conf.disabled/../../../etc/passwd"]
    )
    async def test_only_the_two_file_names_are_accepted(self, client, tmp_dirs, name):
        path = str(Path(tmp_dirs["nginx_dir"]) / "vhosts" / name)
        assert (await client.get("/api/config/file", params={"path": path})).status_code == 400

    async def test_pools_have_no_disabled_variant(self, client, tmp_dirs):
        path = str(Path(tmp_dirs["nginx_dir"]) / "pools" / "pool-a.conf.disabled")
        assert (await client.post("/api/config/absorb-file", json={"path": path})).status_code == 400

    async def test_rebuild_all_writes_each_vhost_under_its_name_and_removes_the_other(
        self, client, mock_nginx, tmp_dirs
    ):
        c = await cluster(client)
        await set_enabled(client, c["vhost"], False)
        stale = Path(vhost_config_path(NAME))
        stale.write_text("# managed by nanio-orchestrator\nserver { listen 1; }\n")  # left behind by hand
        other = await create_vhost(client, "other.example.com", listen_port=8082, default_pool_id=c["a"]["id"])

        result = (await client.post("/api/config/rebuild")).json()

        assert result["ok"] is True
        assert vhost_files(tmp_dirs) == ["other.example.com.conf", f"{NAME}.conf.disabled"]
        assert result["removed"] == [str(stale)]
        assert vhost_config_path(NAME, enabled=False) in result["written"]
        assert vhost_config_path(other["server_name"]) in result["written"]
        assert await recorded() == ["other.example.com.conf", f"{NAME}.conf.disabled"]

    async def test_rebuild_all_refused_by_nginx_removes_nothing(self, client, mock_nginx, tmp_dirs):
        c = await cluster(client)
        await set_enabled(client, c["vhost"], False)
        stale = Path(vhost_config_path(NAME))
        stale.write_text("# managed by nanio-orchestrator\nserver { listen 1; }\n")
        refuse(mock_nginx)

        result = (await client.post("/api/config/rebuild")).json()

        assert result["ok"] is False and result["removed"] == []
        assert vhost_files(tmp_dirs) == [f"{NAME}.conf", f"{NAME}.conf.disabled"]


class TestRebuildFromDisk:
    async def test_disabled_vhost_comes_back_disabled_with_its_routes(self, client, mock_nginx, tmp_dirs):
        c = await cluster(client)
        await set_enabled(client, c["vhost"], False)

        dry = (await client.post("/api/config/rebuild-from-disk?dry_run=true")).json()
        assert [(v["server_name"], v["enabled"], v["routes"]) for v in dry["vhosts"]] == [(NAME, False, 2)]
        assert dry["warnings"] == []

        result = (await client.post("/api/config/rebuild-from-disk?force=true")).json()

        assert result["vhosts_imported"] == 1 and result["routes_imported"] == 2
        vhosts = (await client.get("/api/vhosts")).json()
        assert [(v["server_name"], v["enabled"], v["listen_port"]) for v in vhosts] == [(NAME, False, 8081)]
        routes = (await client.get(f"/api/vhosts/{vhosts[0]['id']}/routes")).json()
        assert {r["path_prefix"]: r["pool_name"] for r in routes} == {"/": "pool-a", "/photos/": "pool-b"}
        assert await recorded() == [f"{NAME}.conf.disabled"]
        assert vhost_files(tmp_dirs) == [f"{NAME}.conf.disabled"]
        # and it can be enabled again from the rebuilt database
        assert (await set_enabled(client, vhosts[0], True)).status_code == 200
        assert vhost_files(tmp_dirs) == [f"{NAME}.conf"]

    async def test_enabled_vhost_comes_back_enabled(self, client, mock_nginx, tmp_dirs):
        await cluster(client)
        await client.post("/api/config/rebuild-from-disk?force=true")
        assert [v["enabled"] for v in (await client.get("/api/vhosts")).json()] == [True]

    @pytest.mark.parametrize("file_enabled", [True, False])
    async def test_file_name_wins_over_a_sidecar_that_disagrees_and_says_so(
        self, client, mock_nginx, tmp_dirs, file_enabled
    ):
        c = await cluster(client)
        if not file_enabled:
            await set_enabled(client, c["vhost"], False)
        meta = sidecar(tmp_dirs, "vhosts", NAME)
        meta.write_text(json.dumps({**json.loads(meta.read_text()), "enabled": not file_enabled}))
        file_name = f"{NAME}.conf" if file_enabled else f"{NAME}.conf.disabled"
        said, taken = ("disabled", "enabled") if file_enabled else ("enabled", "disabled")
        warning = f"Vhost '{NAME}': its sidecar says {said} but its file is {file_name}; taken as {taken}"

        dry = (await client.post("/api/config/rebuild-from-disk?dry_run=true")).json()
        assert dry["vhosts"][0]["enabled"] is file_enabled
        assert dry["warnings"] == [warning]

        result = (await client.post("/api/config/rebuild-from-disk?force=true")).json()
        assert warning in result["warnings"]
        assert [v["enabled"] for v in (await client.get("/api/vhosts")).json()] == [file_enabled]

    async def test_without_a_sidecar_the_file_name_is_enough(self, client, mock_nginx, tmp_dirs):
        c = await cluster(client)
        await set_enabled(client, c["vhost"], False)
        sidecar(tmp_dirs, "vhosts", NAME).unlink()

        result = (await client.post("/api/config/rebuild-from-disk?force=true")).json()

        assert not [w for w in result["warnings"] if "sidecar says" in w]
        assert [v["enabled"] for v in (await client.get("/api/vhosts")).json()] == [False]

    async def test_both_files_present_the_one_nginx_loads_wins(self, client, mock_nginx, tmp_dirs):
        await cluster(client)
        conf = Path(vhost_config_path(NAME))
        Path(vhost_config_path(NAME, enabled=False)).write_text(conf.read_text())

        result = (await client.post("/api/config/rebuild-from-disk?force=true")).json()

        assert result["vhosts_imported"] == 1
        assert (
            f"Vhost '{NAME}': both {NAME}.conf and {NAME}.conf.disabled exist; the vhost is taken as enabled "
            f"and {NAME}.conf.disabled is ignored"
        ) in result["warnings"]
        assert [v["enabled"] for v in (await client.get("/api/vhosts")).json()] == [True]

    async def test_unmanaged_disabled_file_is_not_imported(self, client, mock_nginx, tmp_dirs):
        (Path(tmp_dirs["nginx_dir"]) / "vhosts" / "mine.conf.disabled").write_text("server { server_name mine; }\n")
        result = (await client.post("/api/config/rebuild-from-disk?force=true")).json()
        assert result["vhosts_imported"] == 0


class TestSync:
    async def test_vhost_disabled_by_renaming_its_file_is_picked_up(self, client, mock_nginx, tmp_dirs):
        c = await cluster(client)
        conf = Path(vhost_config_path(NAME))
        conf.rename(vhost_config_path(NAME, enabled=False))

        result = (await client.post("/api/config/sync")).json()

        assert result["vhosts_updated"] == 1
        assert (await client.get(f"/api/vhosts/{c['vhost']['id']}")).json()["enabled"] is False
        assert await recorded() == [f"{NAME}.conf.disabled"]  # no stale record of the file that is gone
        assert [w for w in result["warnings"] if "sidecar says enabled" in w]

    async def test_vhost_enabled_by_renaming_its_file_is_picked_up(self, client, mock_nginx, tmp_dirs):
        c = await cluster(client)
        await set_enabled(client, c["vhost"], False)
        Path(vhost_config_path(NAME, enabled=False)).rename(vhost_config_path(NAME))

        await client.post("/api/config/sync")

        assert (await client.get(f"/api/vhosts/{c['vhost']['id']}")).json()["enabled"] is True
        assert await recorded() == [f"{NAME}.conf"]

    async def test_sync_leaves_an_untouched_disabled_vhost_alone(self, client, mock_nginx, tmp_dirs):
        c = await cluster(client)
        await set_enabled(client, c["vhost"], False)

        result = (await client.post("/api/config/sync")).json()

        assert result["warnings"] == []
        assert (await client.get(f"/api/vhosts/{c['vhost']['id']}")).json()["enabled"] is False
        assert len((await client.get(f"/api/vhosts/{c['vhost']['id']}/routes")).json()) == 2


async def test_migration_is_refused_on_a_disabled_vhost(client, mock_nginx, mock_s3, tmp_dirs):
    """Its routing steps would go to a file nginx does not load: clients would never be switched."""
    from tests.conftest import create_member, create_pool, create_route, create_vhost

    src = await create_pool(client, "mig-src")
    await create_member(client, src["id"], "10.0.0.1:9000")
    dst = await create_pool(client, "mig-dst")
    await create_member(client, dst["id"], "10.0.0.2:9000")
    vhost = await create_vhost(client, "off.example.com", default_pool_id=src["id"])
    mock_s3["list_buckets"].return_value = [{"name": "photos", "created": "2025-01-01"}]
    await client.post(f"/api/vhosts/{vhost['id']}/buckets/sync")
    await create_route(client, vhost["id"], "photos", src["id"])
    disabled = await client.put(f"/api/vhosts/{vhost['id']}", json={"enabled": False})
    assert disabled.status_code == 200, disabled.text

    resp = await client.post(
        "/api/migrations", json={"bucket": "photos", "src_pool_id": src["id"], "dst_pool_id": dst["id"]}
    )

    assert resp.status_code == 409
    assert "is disabled" in resp.json()["detail"] and "Enable the vhost first" in resp.json()["detail"]
    assert (await client.get("/api/migrations")).json() == []
