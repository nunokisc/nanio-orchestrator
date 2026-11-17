"""Renames, multi-file changes and a few refusals that used to leave things inconsistent."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from nanio_orchestrator.models import RouteCreate
from nanio_orchestrator.nginx import apply

from .test_pools import create_member, create_pool
from .test_vhosts import create_vhost


def _dir(tmp_dirs, kind):
    return sorted(p.name for p in (Path(tmp_dirs["nginx_dir"]) / kind).iterdir())


async def _cluster(client):
    pool = await create_pool(client, "pa")
    await create_member(client, pool["id"], "10.0.0.1:9000")
    vhost = await create_vhost(client, "s3.example.com", default_pool_id=pool["id"])
    return pool, vhost


class TestRenamePool:
    async def test_files_follow_the_new_name_and_vhosts_are_regenerated(self, client, mock_nginx, tmp_dirs, set_secret):
        pool, _ = await _cluster(client)
        stored = await client.put(f"/api/pools/{pool['id']}/credentials", json={"access_key": "AK", "secret_key": "SK"})
        assert stored.status_code == 200, stored.text

        resp = await client.put(f"/api/pools/{pool['id']}", json={"name": "renamed"})

        assert resp.status_code == 200, resp.text
        assert _dir(tmp_dirs, "pools") == ["renamed.conf", "renamed.meta.json"]
        conf = Path(tmp_dirs["nginx_dir"]) / "pools" / "renamed.conf"
        assert "upstream renamed {" in conf.read_text()
        vhost_conf = (Path(tmp_dirs["nginx_dir"]) / "vhosts" / "s3.example.com.conf").read_text()
        assert "proxy_pass         http://renamed;" in vhost_conf and "http://pa;" not in vhost_conf
        sidecar = json.loads((Path(tmp_dirs["nginx_dir"]) / "pools" / "renamed.meta.json").read_text())
        assert sidecar["name"] == "renamed" and sidecar["credentials"]["access_key_enc"]
        tracked = [f["path"] for f in (await client.get("/api/config/status")).json()["files"]]
        assert not any(path.endswith("/pa.conf") for path in tracked)

    async def test_refused_rename_changes_nothing(self, client, mock_nginx, tmp_dirs):
        pool, _ = await _cluster(client)
        before = _dir(tmp_dirs, "pools")
        mock_nginx["test_config"].return_value = MagicMock(ok=False, output="[emerg] no")

        resp = await client.put(f"/api/pools/{pool['id']}", json={"name": "renamed"})

        assert resp.status_code == 422
        assert _dir(tmp_dirs, "pools") == before
        assert (await client.get(f"/api/pools/{pool['id']}")).json()["name"] == "pa"

    async def test_name_already_taken(self, client, mock_nginx, tmp_dirs):
        pool, _ = await _cluster(client)
        await create_pool(client, "taken")
        resp = await client.put(f"/api/pools/{pool['id']}", json={"name": "taken"})
        assert resp.status_code == 409
        assert (await client.get(f"/api/pools/{pool['id']}")).json()["name"] == "pa"


class TestRenameVhost:
    async def test_files_follow_the_new_name(self, client, mock_nginx, tmp_dirs):
        _, vhost = await _cluster(client)

        resp = await client.put(f"/api/vhosts/{vhost['id']}", json={"server_name": "new.example.com"})

        assert resp.status_code == 200, resp.text
        assert _dir(tmp_dirs, "vhosts") == ["new.example.com.conf", "new.example.com.meta.json"]
        conf = (Path(tmp_dirs["nginx_dir"]) / "vhosts" / "new.example.com.conf").read_text()
        assert "server_name new.example.com;" in conf

    async def test_refused_rename_changes_nothing(self, client, mock_nginx, tmp_dirs):
        _, vhost = await _cluster(client)
        before = _dir(tmp_dirs, "vhosts")
        mock_nginx["test_config"].return_value = MagicMock(ok=False, output="[emerg] no")

        resp = await client.put(f"/api/vhosts/{vhost['id']}", json={"server_name": "new.example.com"})

        assert resp.status_code == 422
        assert _dir(tmp_dirs, "vhosts") == before

    async def test_name_already_taken(self, client, mock_nginx, tmp_dirs):
        pool, vhost = await _cluster(client)
        await create_vhost(client, "taken.example.com", default_pool_id=pool["id"])
        resp = await client.put(f"/api/vhosts/{vhost['id']}", json={"server_name": "taken.example.com"})
        assert resp.status_code == 409


class TestApplyWithRemovals:
    async def test_removed_file_comes_back_when_nginx_refuses(self, mock_nginx, tmp_path):
        keep, gone = tmp_path / "keep.conf", tmp_path / "gone.conf"
        gone.write_text("old")
        mock_nginx["test_config"].return_value = MagicMock(ok=False, output="no")

        result = await apply.apply_config_files([(str(keep), "new")], remove=[str(gone), str(tmp_path / "never.conf")])

        assert result.ok is False
        assert gone.read_text() == "old" and not keep.exists()

    async def test_removal_is_kept_when_nginx_accepts(self, mock_nginx, tmp_path):
        gone = tmp_path / "gone.conf"
        gone.write_text("old")
        assert (await apply.apply_config_files([], remove=[str(gone)])).ok is True
        assert not gone.exists()


class TestRefusals:
    async def test_vhost_cannot_be_deleted_while_one_of_its_buckets_is_being_moved(self, client, mock_nginx, db):
        pool, vhost = await _cluster(client)
        other = await create_pool(client, "pb")
        await db.execute(
            "INSERT INTO migrations (vhost_id, bucket, src_pool_id, dst_pool_id, phase) VALUES (?, 'photos', ?, ?, 'copying')",
            (vhost["id"], pool["id"], other["id"]),
        )
        await db.commit()

        resp = await client.delete(f"/api/vhosts/{vhost['id']}")

        assert resp.status_code == 409 and "still moving bucket 'photos'" in resp.json()["detail"]
        assert len((await client.get("/api/vhosts")).json()) == 1

    async def test_a_slot_taken_at_the_last_moment_answers_429(self):
        from fastapi import HTTPException
        from nanio_orchestrator.api import buckets

        full = AsyncMock(side_effect=RuntimeError("Max parallel"))
        with patch.object(buckets, "engine_start_migration", full), pytest.raises(HTTPException) as refused:
            await buckets._start_or_429(1, "photos", 1, 2)
        assert refused.value.status_code == 429 and "Max parallel" in refused.value.detail

    @pytest.mark.parametrize("path", ["//", "/a//b/", "/a/b//", "/"])
    def test_route_path_needs_real_segments(self, path):
        with pytest.raises(ValueError):
            RouteCreate(path_prefix=path, pool_id=1)

    @pytest.mark.parametrize("path", ["/photos/", "/photos", "/photos/2025/q1/", "/a.b_c@d-e/"])
    def test_valid_route_paths(self, path):
        assert RouteCreate(path_prefix=path, pool_id=1).path_prefix == path

    async def test_absorbed_file_is_recorded_under_its_real_path(self, client, mock_nginx, tmp_dirs):
        await _cluster(client)
        pools_dir = Path(tmp_dirs["nginx_dir"]) / "pools"
        real = pools_dir / "pa.conf"
        roundabout = str(pools_dir / ".." / "pools" / "pa.conf")
        real.write_text(real.read_text() + "\n# edited by hand\n")

        resp = await client.post("/api/config/absorb-file", json={"path": roundabout})

        assert resp.status_code == 200, resp.text
        files = {f["path"]: f for f in (await client.get("/api/config/status")).json()["files"]}
        assert roundabout not in files
        assert files[str(real.resolve())]["drifted"] is False
