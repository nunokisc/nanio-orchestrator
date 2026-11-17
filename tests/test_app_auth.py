"""Application lifecycle, the auth middleware and the session token."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from nanio_orchestrator import app as app_mod
from nanio_orchestrator import auth
from nanio_orchestrator.auth import COOKIE_NAME
from nanio_orchestrator.config import get_settings

KEY = "test-key"


def anonymous(client):
    client.headers.pop("X-Orchestrator-Key", None)
    return client


# ── Session token ─────────────────────────────────────────────────────────────


class TestSessionToken:
    def test_fresh_token_verifies_with_the_same_key(self):
        token = auth.make_session_token(KEY)
        ts, sig = token.split(".")
        assert ts.isdigit() and len(sig) == 32
        assert auth.verify_session_token(token, KEY, ttl=60) is True

    def test_token_signed_with_another_key_is_rejected(self):
        assert auth.verify_session_token(auth.make_session_token("other-key"), KEY, ttl=60) is False

    def test_tampered_timestamp_is_rejected(self):
        ts, sig = auth.make_session_token(KEY).split(".")
        assert auth.verify_session_token(f"{int(ts) + 1}.{sig}", KEY, ttl=60) is False

    @pytest.mark.parametrize("token", ["", "no-dot", "abc.def", ".", "12345", "1.2.3", "١٢٣.abc", "123.sïg"])
    def test_malformed_token_is_rejected(self, token):
        assert auth.verify_session_token(token, KEY, ttl=60) is False

    def test_token_expires_after_its_ttl(self, monkeypatch):
        monkeypatch.setattr(auth.time, "time", lambda: 1_000_000.0)
        token = auth.make_session_token(KEY)
        assert token.startswith("1000000.")
        monkeypatch.setattr(auth.time, "time", lambda: 1_000_060.0)
        assert auth.verify_session_token(token, KEY, ttl=60) is True  # exactly at the limit
        monkeypatch.setattr(auth.time, "time", lambda: 1_000_061.0)
        assert auth.verify_session_token(token, KEY, ttl=60) is False


# ── Middleware ────────────────────────────────────────────────────────────────


class TestApiAuth:
    async def test_correct_header_key_is_accepted(self, client):
        resp = await client.get("/api/pools")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.parametrize("key", [None, "", "wrong", "test-key2", "test-ke", "TEST-KEY"])
    async def test_missing_or_wrong_header_key_is_rejected_with_json(self, client, key):
        anonymous(client)
        headers = {} if key is None else {"X-Orchestrator-Key": key}
        resp = await client.get("/api/pools", headers=headers)
        assert resp.status_code == 401
        assert resp.headers["content-type"] == "application/json"
        assert resp.json() == {"detail": "Invalid or missing X-Orchestrator-Key"}

    async def test_non_ascii_header_key_is_rejected_not_an_error(self, client):
        anonymous(client)
        resp = await client.get("/api/pools", headers={"X-Orchestrator-Key": "tést-kéy".encode("latin-1")})
        assert resp.status_code == 401

    async def test_writes_are_rejected_without_a_key_and_nothing_is_created(self, client):
        anonymous(client)
        resp = await client.post("/api/pools", json={"name": "sneaky"})
        assert resp.status_code == 401
        client.headers["X-Orchestrator-Key"] = KEY
        assert (await client.get("/api/pools")).json() == []

    async def test_session_cookie_alone_opens_the_api(self, client):
        anonymous(client)
        client.cookies.set(COOKIE_NAME, auth.make_session_token(KEY))
        assert (await client.get("/api/pools")).status_code == 200

    async def test_wrong_header_key_does_not_block_a_valid_session(self, client):
        client.headers["X-Orchestrator-Key"] = "stale"
        client.cookies.set(COOKIE_NAME, auth.make_session_token(KEY))
        assert (await client.get("/api/pools")).status_code == 200

    @pytest.mark.parametrize("cookie", ["garbage", "123.abc", "123.sïg"])
    async def test_invalid_session_cookie_is_rejected(self, client, cookie):
        anonymous(client)
        resp = await client.get("/api/pools", headers={"Cookie": f"{COOKIE_NAME}={cookie}".encode("latin-1")})
        assert resp.status_code == 401

    async def test_cookie_forged_with_another_key_is_rejected(self, client):
        anonymous(client)
        client.cookies.set(COOKIE_NAME, auth.make_session_token("guessed-key"))
        assert (await client.get("/api/pools")).status_code == 401

    async def test_expired_session_cookie_is_rejected(self, client, monkeypatch):
        anonymous(client)
        ttl = get_settings().session_ttl
        client.cookies.set(COOKIE_NAME, auth.make_session_token(KEY))
        assert (await client.get("/api/pools")).status_code == 200

        real_time = auth.time.time
        monkeypatch.setattr(auth.time, "time", lambda: real_time() + ttl + 5)
        assert (await client.get("/api/pools")).status_code == 401
        assert (await client.get("/api/auth/session")).json()["authenticated"] is False


class TestUnprotectedPaths:
    @pytest.mark.parametrize("path", ["/api/health", "/api/docs", "/api/redoc", "/api/auth/session"])
    async def test_reachable_without_any_credentials(self, client, path):
        resp = await anonymous(client).get(path, follow_redirects=False)
        assert resp.status_code == 200

    async def test_paths_that_only_look_unprotected_are_protected(self, client):
        anonymous(client)
        for path in ("/api/healthz", "/api/health/", "/api/auth", "/api/auth/session/x", "/api/docs/x"):
            assert (await client.get(path)).status_code == 401, path
        assert (await client.post("/api/auth/login/x", json={"key": KEY})).status_code == 401


# ── Startup / shutdown ────────────────────────────────────────────────────────


class Loop:
    """A background loop that runs until cancelled and remembers what happened to it."""

    def __init__(self):
        self.started = asyncio.Event()
        self.cancelled = False

    async def __call__(self):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class TestLifespan:
    async def test_startup_prepares_storage_starts_the_loops_and_shutdown_stops_them(self, app, tmp_dirs, caplog):
        import shutil

        # a first start: none of the directories exist yet
        shutil.rmtree(tmp_dirs["nginx_dir"])
        shutil.rmtree(tmp_dirs["tmp_path"] / "migrations")
        drift, sync, backup = Loop(), Loop(), Loop()
        recover = AsyncMock(return_value=0)
        with (
            patch.object(app_mod, "drift_loop", drift),
            patch.object(app_mod, "bucket_sync_loop", sync),
            patch.object(app_mod, "backup_loop", backup),
            patch.object(app_mod, "recover_interrupted_migrations", recover),
            patch.object(app_mod, "stop_drift") as stop_drift,
            patch.object(app_mod, "stop_bucket_sync") as stop_sync,
            patch.object(app_mod, "stop_backup") as stop_backup,
            caplog.at_level(logging.INFO, logger="nanio_orchestrator.app"),
        ):
            async with app.router.lifespan_context(app):
                await asyncio.wait_for(
                    asyncio.gather(drift.started.wait(), sync.started.wait(), backup.started.wait()), timeout=5
                )
                recover.assert_awaited_once()
                for sub in ("pools", "vhosts"):
                    assert (Path(tmp_dirs["nginx_dir"]) / sub).is_dir()
                assert (tmp_dirs["tmp_path"] / "migrations").is_dir()
                assert Path(tmp_dirs["db_path"]).exists()
                stop_drift.assert_not_called()
                assert not (drift.cancelled or sync.cancelled or backup.cancelled)

            stop_drift.assert_called_once()
            stop_sync.assert_called_once()
            stop_backup.assert_called_once()
            assert drift.cancelled and sync.cancelled and backup.cancelled

        messages = [r.getMessage() for r in caplog.records]
        assert any("dev mode" in m for m in messages)
        assert messages[-1] == "nanio-orchestrator stopped"

    async def test_production_start_does_not_log_the_dev_key_hint(self, app, monkeypatch, caplog):
        settings = get_settings()
        monkeypatch.setattr(settings, "dev", False)
        monkeypatch.setattr(settings, "port", 8123)
        with caplog.at_level(logging.INFO, logger="nanio_orchestrator.app"):
            async with app.router.lifespan_context(app):
                pass
        messages = [r.getMessage() for r in caplog.records]
        assert "nanio-orchestrator started on port 8123" in messages
        assert not any("API key" in m for m in messages)

    async def test_loops_that_already_finished_do_not_break_shutdown(self, app):
        """The fixture's loops return at once; shutdown must cope with finished tasks."""
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0)
        app_mod.stop_drift.assert_called()
