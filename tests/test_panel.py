"""The web panel's server side: the session endpoints, the application shell and its assets,
and the read-only summaries the panel polls."""

from __future__ import annotations

import pytest
from nanio_orchestrator import __version__
from nanio_orchestrator.auth import COOKIE_NAME, make_session_token
from nanio_orchestrator.db import get_db_ctx
from nanio_orchestrator.web import spa

KEY = "test-key"


def anonymous(client):
    client.headers.pop("X-Orchestrator-Key", None)
    return client


@pytest.fixture
def built_ui(tmp_path, monkeypatch):
    """A build output: the shell, one hashed asset and one file at the root."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>panel shell</title>")
    (dist / "assets" / "index-abc123.js").write_text("console.log('panel')")
    (dist / "favicon.svg").write_text("<svg/>")
    (tmp_path / "outside.txt").write_text("not part of the build")
    monkeypatch.setattr(spa, "UI_DIR", dist)
    return dist


# ── Session ───────────────────────────────────────────────────────────────────


class TestSession:
    async def test_anonymous_browser_is_told_so_without_a_401(self, client):
        resp = await anonymous(client).get("/api/auth/session")
        assert resp.status_code == 200
        assert resp.json() == {"authenticated": False, "dev_mode": True, "version": __version__}

    async def test_api_key_header_counts_as_signed_in(self, client):
        assert (await client.get("/api/auth/session")).json()["authenticated"] is True

    async def test_login_with_the_key_sets_a_session_that_opens_the_api(self, client):
        anonymous(client)
        resp = await client.post("/api/auth/login", json={"key": KEY})
        assert resp.status_code == 200
        assert resp.json() == {"authenticated": True, "dev_mode": True, "version": __version__}
        cookie = resp.headers["set-cookie"]
        assert cookie.startswith(f"{COOKIE_NAME}=")
        assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Secure" not in cookie

        assert (await client.get("/api/auth/session")).json()["authenticated"] is True
        assert (await client.get("/api/pools")).status_code == 200

    @pytest.mark.parametrize("key", ["", "wrong", "test-key2", "test-ke", "TEST-KEY", "tést-kéy"])
    async def test_wrong_key_is_a_401_and_sets_no_cookie(self, client, key):
        resp = await anonymous(client).post("/api/auth/login", json={"key": key})
        assert resp.status_code == 401
        assert resp.json() == {"detail": "Invalid API key"}
        assert "set-cookie" not in resp.headers
        assert (await client.get("/api/pools")).status_code == 401

    async def test_login_without_a_key_field_is_a_validation_error(self, client):
        resp = await anonymous(client).post("/api/auth/login", json={})
        assert resp.status_code == 422

    @pytest.mark.parametrize(
        "url, headers",
        [("/api/auth/login", {"X-Forwarded-Proto": "HTTPS"}), ("https://test/api/auth/login", {})],
    )
    async def test_session_cookie_is_secure_when_the_panel_is_reached_over_tls(self, client, url, headers):
        resp = await anonymous(client).post(url, json={"key": KEY}, headers=headers)
        assert "Secure" in resp.headers["set-cookie"]

    async def test_logout_expires_the_cookie(self, client):
        anonymous(client)
        await client.post("/api/auth/login", json={"key": KEY})
        resp = await client.post("/api/auth/logout")
        assert resp.status_code == 200
        assert resp.json()["authenticated"] is False
        assert f'{COOKIE_NAME}=""' in resp.headers["set-cookie"]
        assert "Max-Age=0" in resp.headers["set-cookie"]
        assert (await client.get("/api/pools")).status_code == 401

    async def test_logout_needs_no_session(self, client):
        assert (await anonymous(client).post("/api/auth/logout")).status_code == 200

    async def test_forged_cookie_is_not_a_session(self, client):
        anonymous(client)
        client.cookies.set(COOKIE_NAME, make_session_token("guessed-key"))
        assert (await client.get("/api/auth/session")).json()["authenticated"] is False


# ── The application shell ─────────────────────────────────────────────────────


class TestShell:
    @pytest.mark.parametrize("path", ["/", "/pools", "/vhosts/3", "/configuration", "/login", "/a/b/c"])
    async def test_every_page_path_gets_the_shell_without_credentials(self, client, built_ui, path):
        resp = await anonymous(client).get(path)
        assert resp.status_code == 200
        assert "panel shell" in resp.text
        assert resp.headers["content-type"].startswith("text/html")
        assert resp.headers["cache-control"] == "no-cache"

    async def test_assets_and_root_files_are_served_as_files(self, client, built_ui):
        script = await anonymous(client).get("/assets/index-abc123.js")
        assert script.status_code == 200
        assert script.text == "console.log('panel')"
        assert "javascript" in script.headers["content-type"]
        icon = await client.get("/favicon.svg")
        assert icon.status_code == 200
        assert icon.headers["content-type"].startswith("image/svg")

    async def test_missing_asset_is_a_404_not_the_shell(self, client, built_ui):
        resp = await anonymous(client).get("/assets/index-old.js")
        assert resp.status_code == 404

    @pytest.mark.parametrize("path", ["/../outside.txt", "/%2e%2e/outside.txt", "/assets/../../outside.txt"])
    async def test_nothing_outside_the_build_is_served(self, client, built_ui, path):
        resp = await anonymous(client).get(path)
        assert "not part of the build" not in resp.text

    async def test_symlink_out_of_the_build_is_not_followed(self, client, built_ui, tmp_path):
        (built_ui / "leak.txt").symlink_to(tmp_path / "outside.txt")
        resp = await anonymous(client).get("/leak.txt")
        assert "not part of the build" not in resp.text
        assert "panel shell" in resp.text

    @pytest.mark.parametrize("path", ["/api", "/api/nope", "/api/pools/1/nope"])
    async def test_unknown_api_path_is_a_json_404_never_the_shell(self, client, built_ui, path):
        resp = await client.get(path)
        assert resp.status_code == 404
        assert resp.headers["content-type"] == "application/json"

    async def test_wrong_method_on_an_api_path_is_still_a_405(self, client, built_ui):
        assert (await client.get("/api/config/reload")).status_code == 405
        assert (await client.post("/pools")).status_code == 405

    async def test_unknown_api_path_without_credentials_is_a_401(self, client, built_ui):
        assert (await anonymous(client).get("/api/nope")).status_code == 401

    async def test_checkout_without_a_build_says_how_to_build_it(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(spa, "UI_DIR", tmp_path / "no-dist")
        for path in ("/", "/pools", "/assets/index.js"):
            resp = await anonymous(client).get(path)
            assert resp.status_code == 503
            assert "make ui" in resp.text
        assert (await client.get("/api/health")).status_code == 200

    async def test_openapi_schema_is_behind_the_api_authentication(self, client, built_ui):
        assert (await client.get("/api/openapi.json")).status_code == 200
        assert (await anonymous(client).get("/api/openapi.json")).status_code == 401
        assert "panel shell" in (await client.get("/openapi.json")).text


# ── Overview ──────────────────────────────────────────────────────────────────


class TestOverview:
    async def test_requires_authentication(self, client):
        assert (await anonymous(client).get("/api/overview")).status_code == 401

    async def test_empty_installation(self, client):
        assert (await client.get("/api/overview")).json() == {
            "pools": [],
            "vhosts": 0,
            "routes": 0,
            "unrouted_buckets": 0,
            "missing_buckets": [],
        }

    async def test_summarises_pools_vhosts_routes_and_buckets(self, client, mock_nginx):
        a = (await client.post("/api/pools", json={"name": "pool-a"})).json()["id"]
        b = (await client.post("/api/pools", json={"name": "pool-b"})).json()["id"]
        cdn = (await client.post("/api/pools", json={"name": "cdn", "type": "http"})).json()["id"]
        await client.post(f"/api/pools/{a}/members", json={"address": "10.0.0.1:9000"})
        await client.post(f"/api/pools/{a}/members", json={"address": "10.0.0.2:9000", "enabled": False})
        await client.post(f"/api/pools/{b}/members", json={"address": "10.0.0.3:9000"})
        vhost = (
            await client.post(
                "/api/vhosts", json={"server_name": "s3.test", "ssl": False, "listen_port": 8081, "default_pool_id": a}
            )
        ).json()["id"]
        await client.post(f"/api/vhosts/{vhost}/routes", json={"path_prefix": "/photos/", "pool_id": b})
        async with get_db_ctx() as db:
            await db.execute(
                "INSERT INTO pool_credentials (pool_id, access_key_enc, secret_key_enc) VALUES (?, 'x', 'y')", (a,)
            )
            await db.execute(
                """INSERT INTO bucket_sync (vhost_id, bucket, discovered_at, status, routed_pool_id, missing_since)
                   VALUES (?, 'photos', '2026-01-01T00:00:00Z', 'routed', ?, '2026-01-02T00:00:00Z')""",
                (vhost, b),
            )
            await db.execute(
                """INSERT INTO bucket_sync (vhost_id, bucket, discovered_at, status)
                   VALUES (?, 'logs', '2026-01-01T00:00:00Z', 'unrouted')""",
                (vhost,),
            )
            await db.commit()

        body = (await client.get("/api/overview")).json()
        assert body["pools"] == [
            {"id": cdn, "name": "cdn", "type": "http", "members": 0, "enabled_members": 0, "has_credentials": False},
            {"id": a, "name": "pool-a", "type": "nanio", "members": 2, "enabled_members": 1, "has_credentials": True},
            {"id": b, "name": "pool-b", "type": "nanio", "members": 1, "enabled_members": 1, "has_credentials": False},
        ]
        assert (body["vhosts"], body["routes"], body["unrouted_buckets"]) == (1, 1, 1)
        assert body["missing_buckets"] == [
            {
                "vhost_id": vhost,
                "server_name": "s3.test",
                "bucket": "photos",
                "pool_name": "pool-b",
                "missing_since": "2026-01-02T00:00:00Z",
            }
        ]
