"""Pool credentials API: what is refused, and that a refusal stores nothing."""

from __future__ import annotations

import json

from tests.api_state import audit, sidecar, sql
from tests.conftest import create_member, create_pool

BODY = {"access_key": "AKIAEXAMPLE", "secret_key": "very-secret"}


class TestCredentialsRefusals:
    async def test_plain_file_pools_have_no_credentials(self, client, mock_nginx, set_secret, tmp_dirs):
        pool = await create_pool(client, "web", pool_type="http")
        await create_member(client, pool["id"], "10.0.1.1:80", role="primary")
        url = f"/api/pools/{pool['id']}/credentials"

        responses = [await client.get(url), await client.put(url, json=BODY), await client.delete(url)]

        assert [r.status_code for r in responses] == [400, 400, 400]
        assert {r.json()["detail"] for r in responses} == {
            "S3 credentials are only supported for nanio pools. Pool 'web' is of type 'http'."
        }
        assert await sql("SELECT id FROM pool_credentials") == []
        assert "credentials" not in json.loads(sidecar(tmp_dirs, "pools", "web").read_text())
        assert await audit(client, "set_credentials") == []

    async def test_nothing_is_stored_when_no_encryption_key_is_configured(self, client, tmp_dirs):
        from nanio_orchestrator import credentials

        credentials.reset_fernet()
        pool = await create_pool(client, "pa")

        resp = await client.put(f"/api/pools/{pool['id']}/credentials", json=BODY)

        assert resp.status_code == 500
        assert "NANIO_ORCHESTRATOR_SECRET is not set" in resp.json()["detail"]
        assert "very-secret" not in resp.text
        assert await sql("SELECT id FROM pool_credentials") == []
        assert "credentials" not in json.loads(sidecar(tmp_dirs, "pools", "pa").read_text())
        assert await audit(client, "set_credentials") == []
        assert (await client.get(f"/api/pools/{pool['id']}/credentials")).status_code == 404
