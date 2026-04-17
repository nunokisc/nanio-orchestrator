"""Tests for the S3 listing proxy."""

import pytest
from unittest.mock import AsyncMock, patch
from httpx import ASGITransport, AsyncClient

from tests.conftest import create_pool, create_member, create_vhost


class TestS3Proxy:
    async def test_proxy_list_buckets_empty(self, client):
        """Proxy returns empty listing when no vhosts configured."""
        from nanio_orchestrator.s3_proxy import create_proxy_app
        proxy_app = create_proxy_app()

        transport = ASGITransport(app=proxy_app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://proxy") as pc:
            resp = await pc.get("/")
            assert resp.status_code == 200
            assert "ListAllMyBucketsResult" in resp.text

    async def test_proxy_list_buckets_with_data(self, client, mock_s3, mock_nginx):
        """Proxy merges buckets from default pool."""
        pool = await create_pool(client, "proxy-pool")
        await create_member(client, pool["id"], "10.0.0.1:9000")
        await create_vhost(client, "proxy.example.com", default_pool_id=pool["id"])

        mock_s3["list_buckets"].return_value = [
            {"name": "photos", "created": "2025-01-01"},
            {"name": "videos", "created": "2025-01-02"},
        ]

        from nanio_orchestrator.s3_proxy import create_proxy_app
        proxy_app = create_proxy_app()
        transport = ASGITransport(app=proxy_app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://proxy") as pc:
            resp = await pc.get("/")
            assert resp.status_code == 200
            assert "photos" in resp.text
            assert "videos" in resp.text

    async def test_proxy_bucket_not_found(self, client):
        """Proxy returns 404 for unknown bucket."""
        from nanio_orchestrator.s3_proxy import create_proxy_app
        proxy_app = create_proxy_app()
        transport = ASGITransport(app=proxy_app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://proxy") as pc:
            resp = await pc.get("/nonexistent-bucket")
            assert resp.status_code == 404
            assert "NoSuchBucket" in resp.text
