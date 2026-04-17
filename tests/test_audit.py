"""Tests for audit log functionality."""

import pytest
from tests.conftest import create_pool, create_member


class TestAudit:
    async def test_audit_log_created(self, client, mock_nginx):
        pool = await create_pool(client, "audit-pool")
        await create_member(client, pool["id"], "10.0.0.1:9000")

        resp = await client.get("/api/audit")
        assert resp.status_code == 200
        entries = resp.json()
        assert len(entries) >= 1

    async def test_audit_log_records_pool_create(self, client, mock_nginx):
        pool = await create_pool(client, "audit-create-pool")

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
