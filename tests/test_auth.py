"""Tests for authentication middleware."""

import pytest


class TestAuth:
    async def test_api_no_key_rejected(self, client):
        # Remove the auth header
        client.headers.pop("X-Orchestrator-Key", None)
        resp = await client.get("/api/pools")
        assert resp.status_code == 401

    async def test_api_wrong_key_rejected(self, client):
        client.headers["X-Orchestrator-Key"] = "wrong-key"
        resp = await client.get("/api/pools")
        assert resp.status_code == 401

    async def test_api_correct_key_accepted(self, client):
        resp = await client.get("/api/pools")
        assert resp.status_code == 200

    async def test_health_no_auth(self, client):
        client.headers.pop("X-Orchestrator-Key", None)
        resp = await client.get("/api/health")
        assert resp.status_code == 200

    async def test_login_page_accessible(self, client):
        client.headers.pop("X-Orchestrator-Key", None)
        resp = await client.get("/login")
        assert resp.status_code == 200
        assert "login" in resp.text.lower()

    async def test_login_wrong_key(self, client):
        resp = await client.post("/login", data={"key": "wrong"})
        # Should render login page with error
        assert resp.status_code == 200
        assert "invalid" in resp.text.lower() or "error" in resp.text.lower()

    async def test_login_correct_key_sets_cookie(self, client):
        resp = await client.post(
            "/login",
            data={"key": "test-key"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "session" in resp.headers.get("set-cookie", "").lower()

    async def test_web_redirect_to_login(self, client):
        client.headers.pop("X-Orchestrator-Key", None)
        resp = await client.get("/", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers.get("location", "")
