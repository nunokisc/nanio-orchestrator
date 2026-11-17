"""Tests for audit log functionality."""

from nanio_orchestrator.db import get_db_ctx

from tests.conftest import create_member, create_pool


class TestAudit:
    async def test_audit_log_created(self, client, mock_nginx):
        pool = await create_pool(client, "audit-pool")
        await create_member(client, pool["id"], "10.0.0.1:9000")

        resp = await client.get("/api/audit")
        assert resp.status_code == 200
        entries = resp.json()
        assert len(entries) >= 1

    async def test_audit_log_records_pool_create(self, client, mock_nginx):
        await create_pool(client, "audit-create-pool")

        resp = await client.get("/api/audit")
        assert resp.status_code == 200
        entries = resp.json()
        pool_creates = [e for e in entries if e["action"] == "create" and e["entity_type"] == "pool"]
        assert len(pool_creates) >= 1

    async def test_audit_log_records_delete(self, client, mock_nginx):
        pool = await create_pool(client, "audit-del-pool")
        await client.delete(f"/api/pools/{pool['id']}")

        resp = await client.get("/api/audit")
        entries = resp.json()
        deletes = [e for e in entries if e["action"] == "delete" and e["entity_type"] == "pool"]
        assert len(deletes) >= 1


class TestAuditQuery:
    """Filtering and paging of GET /api/audit over a known set of entries."""

    @staticmethod
    async def seed():
        rows = [
            ("create", "pool", 1, "2026-01-01 10:00:00"),
            ("update", "pool", 1, "2026-01-02 10:00:00"),
            ("create", "pool", 2, "2026-01-03 10:00:00"),
            ("create", "vhost", 1, "2026-01-04 10:00:00"),
            ("reload", "config", None, "2026-01-05 10:00:00"),
        ]
        async with get_db_ctx() as db:
            for action, entity_type, entity_id, created_at in rows:
                await db.execute(
                    "INSERT INTO audit_log (action, entity_type, entity_id, created_at) VALUES (?, ?, ?, ?)",
                    (action, entity_type, entity_id, created_at),
                )
            await db.commit()

    @staticmethod
    def summary(resp):
        assert resp.status_code == 200, resp.text
        return [(e["action"], e["entity_type"], e["entity_id"]) for e in resp.json()]

    async def test_empty_log_is_an_empty_list(self, client):
        resp = await client.get("/api/audit")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_entries_are_returned_newest_first_with_all_fields(self, client):
        await self.seed()
        resp = await client.get("/api/audit")
        assert self.summary(resp) == [
            ("reload", "config", None),
            ("create", "vhost", 1),
            ("create", "pool", 2),
            ("update", "pool", 1),
            ("create", "pool", 1),
        ]
        assert resp.json()[0] == {
            "id": 5,
            "actor": "api",
            "action": "reload",
            "entity_type": "config",
            "entity_id": None,
            "before_json": None,
            "after_json": None,
            "nginx_reload_ok": None,
            "nginx_reload_output": None,
            "created_at": "2026-01-05 10:00:00",
        }

    async def test_filter_by_entity_type(self, client):
        await self.seed()
        resp = await client.get("/api/audit", params={"entity_type": "vhost"})
        assert self.summary(resp) == [("create", "vhost", 1)]

    async def test_filter_by_entity_id(self, client):
        await self.seed()
        resp = await client.get("/api/audit", params={"entity_id": 1})
        assert self.summary(resp) == [("create", "vhost", 1), ("update", "pool", 1), ("create", "pool", 1)]

    async def test_filters_combine(self, client):
        await self.seed()
        resp = await client.get("/api/audit", params={"entity_type": "pool", "entity_id": 1})
        assert self.summary(resp) == [("update", "pool", 1), ("create", "pool", 1)]

    async def test_filter_by_date_range_is_inclusive(self, client):
        await self.seed()
        resp = await client.get("/api/audit", params={"from": "2026-01-02 10:00:00", "to": "2026-01-04 10:00:00"})
        assert self.summary(resp) == [("create", "vhost", 1), ("create", "pool", 2), ("update", "pool", 1)]
        resp = await client.get("/api/audit", params={"from": "2026-01-04 10:00:01"})
        assert self.summary(resp) == [("reload", "config", None)]
        resp = await client.get("/api/audit", params={"to": "2026-01-01 23:59:59"})
        assert self.summary(resp) == [("create", "pool", 1)]

    async def test_filter_matching_nothing_is_an_empty_list(self, client):
        await self.seed()
        assert (await client.get("/api/audit", params={"entity_type": "bucket"})).json() == []

    async def test_filter_values_are_bound_not_interpolated(self, client):
        await self.seed()
        resp = await client.get("/api/audit", params={"entity_type": "pool' OR '1'='1"})
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_paging(self, client):
        await self.seed()
        first = await client.get("/api/audit", params={"per_page": 2})
        second = await client.get("/api/audit", params={"per_page": 2, "page": 2})
        third = await client.get("/api/audit", params={"per_page": 2, "page": 3})
        beyond = await client.get("/api/audit", params={"per_page": 2, "page": 4})
        assert [e["id"] for e in first.json()] == [5, 4]
        assert [e["id"] for e in second.json()] == [3, 2]
        assert [e["id"] for e in third.json()] == [1]
        assert beyond.json() == []

    async def test_out_of_range_paging_is_rejected(self, client):
        for params in ({"page": 0}, {"per_page": 0}, {"per_page": 201}, {"entity_id": "abc"}):
            assert (await client.get("/api/audit", params=params)).status_code == 422

    async def test_audit_log_requires_authentication(self, client):
        await self.seed()
        client.headers.pop("X-Orchestrator-Key")
        assert (await client.get("/api/audit")).status_code == 401
