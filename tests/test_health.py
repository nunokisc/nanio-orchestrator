"""Tests for the health endpoint."""



class TestHealth:
    async def test_health_ok(self, client):
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "version" in data
        assert data["db_ok"] is True

    async def test_health_no_auth_required(self, client):
        client.headers.pop("X-Orchestrator-Key", None)
        resp = await client.get("/api/health")
        assert resp.status_code == 200
