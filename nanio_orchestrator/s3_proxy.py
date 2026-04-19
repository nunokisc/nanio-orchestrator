"""S3-compatible listing proxy running on a separate port.

Merges bucket listings from the default pool and any dedicated-route pools,
presenting a unified view to S3 clients. Supports:
  - GET / (ListBuckets)
  - GET /{bucket}?list-type=2 (ListObjectsV2)

Runs as a secondary uvicorn server started from app.py lifespan.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

from fastapi import FastAPI, Request, Response

from nanio_orchestrator.config import get_settings
from nanio_orchestrator.credentials import get_pool_s3_params
from nanio_orchestrator.db import get_db_ctx
from nanio_orchestrator.s3client import list_buckets, count_objects

logger = logging.getLogger(__name__)


def create_proxy_app() -> FastAPI:
    """Create the S3 listing proxy FastAPI app."""
    app = FastAPI(title="nanio-s3-proxy", docs_url=None, redoc_url=None)

    @app.get("/")
    async def proxy_list_buckets(request: Request):
        """Merge bucket listings from default pool across all vhosts."""
        all_buckets: Dict[str, dict] = {}

        async with get_db_ctx() as db:
            vhosts = await db.execute_fetchall(
                "SELECT id, default_pool_id FROM vhosts WHERE default_pool_id IS NOT NULL"
            )

        for v in vhosts:
            vhost = dict(v)
            pool_id = vhost["default_pool_id"]

            async with get_db_ctx() as db:
                members = await db.execute_fetchall(
                    "SELECT address FROM pool_members WHERE pool_id = ? AND enabled = 1 ORDER BY id LIMIT 1",
                    (pool_id,),
                )
            if not members:
                continue

            address = members[0]["address"]
            ak, sk, region = await get_pool_s3_params(pool_id)

            try:
                buckets = await list_buckets(address, access_key=ak, secret_key=sk, region=region)
                for b in buckets:
                    if b["name"] not in all_buckets:
                        all_buckets[b["name"]] = b
            except Exception as e:
                logger.warning("S3 proxy: failed to list buckets from %s: %s", address, e)

        # Build XML response
        xml = _build_list_buckets_xml(list(all_buckets.values()))
        return Response(content=xml, media_type="application/xml")

    @app.get("/{bucket}")
    async def proxy_list_objects(bucket: str, request: Request):
        """List objects in a bucket, routing to the correct pool."""
        # Determine which pool serves this bucket
        pool_id, address = await _resolve_bucket_pool(bucket)
        if not pool_id or not address:
            return Response(
                content=_build_error_xml("NoSuchBucket", f"Bucket '{bucket}' not found"),
                status_code=404,
                media_type="application/xml",
            )

        ak, sk, region = await get_pool_s3_params(pool_id)

        # Forward query params
        params = dict(request.query_params)
        list_type = params.get("list-type", "2")
        max_keys = min(int(params.get("max-keys", "1000")), 1000)
        prefix = params.get("prefix", "")
        continuation = params.get("continuation-token", "")

        # Build the query to forward to the backend directly
        query = f"list-type={list_type}&max-keys={max_keys}"
        if prefix:
            query += f"&prefix={urllib.parse.quote(prefix, safe='')}"
        if continuation:
            query += f"&continuation-token={urllib.parse.quote(continuation, safe='')}"

        try:
            from nanio_orchestrator.s3client import _do_request
            import xml.etree.ElementTree as _ET

            status_code, body = await asyncio.to_thread(
                _do_request, "GET", address, f"/{bucket}", query,
                b"", ak, sk, region
            )
            if status_code != 200:
                return Response(
                    content=_build_error_xml("InternalError", f"Backend returned HTTP {status_code}"),
                    status_code=500,
                    media_type="application/xml",
                )
            # Forward the backend XML response directly — preserves IsTruncated and NextContinuationToken
            return Response(content=body, media_type="application/xml")
        except Exception as e:
            return Response(
                content=_build_error_xml("InternalError", str(e)),
                status_code=500,
                media_type="application/xml",
            )

    return app


async def _resolve_bucket_pool(bucket: str):
    """Find which pool and member serves a given bucket.

    Priority:
      1. Dedicated route (bucket_sync.routed_pool_id)
      2. Default pool of the first vhost that has it in bucket_sync
      3. Default pool of the first vhost
    """
    async with get_db_ctx() as db:
        # Check routed buckets first
        rows = await db.execute_fetchall(
            """SELECT bs.routed_pool_id, bs.vhost_id
               FROM bucket_sync bs
               WHERE bs.bucket = ? AND bs.status = 'routed' AND bs.routed_pool_id IS NOT NULL
               LIMIT 1""",
            (bucket,),
        )
        if rows:
            pool_id = rows[0]["routed_pool_id"]
            members = await db.execute_fetchall(
                "SELECT address FROM pool_members WHERE pool_id = ? AND enabled = 1 ORDER BY id LIMIT 1",
                (pool_id,),
            )
            if members:
                return pool_id, members[0]["address"]

        # Check bucket_sync for any vhost
        rows = await db.execute_fetchall(
            """SELECT bs.vhost_id, v.default_pool_id
               FROM bucket_sync bs
               JOIN vhosts v ON bs.vhost_id = v.id
               WHERE bs.bucket = ? AND v.default_pool_id IS NOT NULL
               LIMIT 1""",
            (bucket,),
        )
        if rows:
            pool_id = rows[0]["default_pool_id"]
            members = await db.execute_fetchall(
                "SELECT address FROM pool_members WHERE pool_id = ? AND enabled = 1 ORDER BY id LIMIT 1",
                (pool_id,),
            )
            if members:
                return pool_id, members[0]["address"]

        # No match — do NOT fall through to an arbitrary vhost's default pool,
        # as that would cross tenant boundaries in multi-vhost deployments.

    return None, None


# ── XML builders ──────────────────────────────────────────────────────────────


def _build_list_buckets_xml(buckets: List[dict]) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<ListAllMyBucketsResult>",
        "  <Buckets>",
    ]
    for b in sorted(buckets, key=lambda x: x["name"]):
        lines.append("    <Bucket>")
        lines.append(f"      <Name>{_xml_escape(b['name'])}</Name>")
        lines.append(f"      <CreationDate>{b.get('created', '')}</CreationDate>")
        lines.append("    </Bucket>")
    lines.append("  </Buckets>")
    lines.append("</ListAllMyBucketsResult>")
    return "\n".join(lines)


def _build_list_objects_xml(bucket: str, keys: List[str], is_truncated: bool) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<ListBucketResult>",
        f"  <Name>{_xml_escape(bucket)}</Name>",
        f"  <KeyCount>{len(keys)}</KeyCount>",
        f"  <MaxKeys>1000</MaxKeys>",
        f"  <IsTruncated>{'true' if is_truncated else 'false'}</IsTruncated>",
    ]
    for key in keys:
        lines.append("  <Contents>")
        lines.append(f"    <Key>{_xml_escape(key)}</Key>")
        lines.append("  </Contents>")
    lines.append("</ListBucketResult>")
    return "\n".join(lines)


def _build_error_xml(code: str, message: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Error>\n"
        f"  <Code>{_xml_escape(code)}</Code>\n"
        f"  <Message>{_xml_escape(message)}</Message>\n"
        "</Error>"
    )


def _xml_escape(s: str) -> str:
    """Escape XML special characters."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


# ── Server lifecycle ──────────────────────────────────────────────────────────


_proxy_server = None


async def start_proxy_server() -> None:
    """Start the S3 proxy server in the background."""
    global _proxy_server
    import uvicorn

    s = get_settings()
    port = s.s3_proxy_port
    app = create_proxy_app()

    config = uvicorn.Config(
        app,
        host=s.host,
        port=port,
        log_level="warning",
    )
    _proxy_server = uvicorn.Server(config)
    logger.info("S3 listing proxy starting on port %d", port)
    await _proxy_server.serve()


async def stop_proxy_server() -> None:
    """Signal the proxy server to shut down."""
    global _proxy_server
    if _proxy_server:
        _proxy_server.should_exit = True
        _proxy_server = None
