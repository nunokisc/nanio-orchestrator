"""Minimal async S3-compatible HTTP client (stdlib only, no boto3).

Supports unsigned requests (no credentials) and AWS Signature V4 when
access_key + secret_key are provided.

All blocking I/O runs via asyncio.to_thread() (Python 3.9+).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import http.client
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from nanio_orchestrator.config import get_settings


# ── SigV4 helpers ─────────────────────────────────────────────────────────────


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, date: str, region: str, service: str) -> bytes:
    k = _hmac_sha256(("AWS4" + secret).encode("utf-8"), date)
    k = _hmac_sha256(k, region)
    k = _hmac_sha256(k, service)
    return _hmac_sha256(k, "aws4_request")


def _make_auth_headers(
    method: str,
    host: str,
    path: str,
    query: str,
    body: bytes,
    access_key: str,
    secret_key: str,
    region: str,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Return headers dict with x-amz-date, x-amz-content-sha256, Authorization."""
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    body_hash = _sha256_hex(body)

    signed_hdrs: Dict[str, str] = {
        "host": host,
        "x-amz-content-sha256": body_hash,
        "x-amz-date": amz_date,
    }
    if extra_headers:
        for k, v in extra_headers.items():
            signed_hdrs[k.lower()] = v

    sorted_keys = sorted(signed_hdrs)
    canonical_headers = "".join(f"{k}:{signed_hdrs[k]}\n" for k in sorted_keys)
    signed_headers_str = ";".join(sorted_keys)

    canonical_request = "\n".join([
        method.upper(),
        urllib.parse.quote(path, safe="/-_.~"),
        query,
        canonical_headers,
        signed_headers_str,
        body_hash,
    ])

    credential_scope = f"{date_stamp}/{region}/s3/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        credential_scope,
        _sha256_hex(canonical_request.encode()),
    ])

    sig = hmac.new(
        _signing_key(secret_key, date_stamp, region, "s3"),
        string_to_sign.encode(),
        hashlib.sha256,
    ).hexdigest()

    auth = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers_str}, Signature={sig}"
    )
    return {
        "x-amz-date": amz_date,
        "x-amz-content-sha256": body_hash,
        "Authorization": auth,
    }


# ── Core HTTP request ─────────────────────────────────────────────────────────


def _parse_address(address: str) -> Tuple[str, int]:
    """Parse 'host:port' → (host, port). Falls back to port 80."""
    host, _, port_str = address.rpartition(":")
    if not host:
        return address, 80
    try:
        return host, int(port_str)
    except ValueError:
        return address, 80


def _do_request(
    method: str,
    address: str,
    path: str,
    query: str = "",
    body: bytes = b"",
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    region: str = "us-east-1",
    extra_headers: Optional[Dict[str, str]] = None,
) -> Tuple[int, bytes]:
    """Synchronous HTTP request — run via asyncio.to_thread()."""
    host, port = _parse_address(address)
    full_host = f"{host}:{port}"

    headers: Dict[str, str] = {
        "Host": full_host,
        "Content-Length": str(len(body)),
    }
    if extra_headers:
        headers.update(extra_headers)

    if access_key and secret_key:
        auth_hdrs = _make_auth_headers(
            method, full_host, path, query, body,
            access_key, secret_key, region,
            extra_headers=extra_headers,
        )
        headers.update(auth_hdrs)

    encoded_path = urllib.parse.quote(path, safe="/-")
    url = f"{encoded_path}?{query}" if query else encoded_path
    conn = http.client.HTTPConnection(host, port, timeout=get_settings().s3_request_timeout)
    try:
        conn.request(method, url, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


# ── XML helpers ───────────────────────────────────────────────────────────────


def _strip_ns(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


# ── Public async API ──────────────────────────────────────────────────────────


async def list_buckets(
    address: str,
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    region: str = "us-east-1",
) -> List[Dict[str, str]]:
    """Return list of buckets: [{"name": str, "created": str}]."""
    status, body = await asyncio.to_thread(
        _do_request, "GET", address, "/", "", b"", access_key, secret_key, region
    )
    if status not in (200, 206):
        raise RuntimeError(f"ListBuckets HTTP {status}: {body[:300].decode(errors='replace')}")

    root = ET.fromstring(body)
    buckets = []
    for bucket_el in root.iter():
        if _strip_ns(bucket_el.tag) == "Bucket":
            name = next((c.text for c in bucket_el if _strip_ns(c.tag) == "Name"), None)
            created = next((c.text or "" for c in bucket_el if _strip_ns(c.tag) == "CreationDate"), "")
            if name:
                buckets.append({"name": name, "created": created})
    return buckets


async def create_bucket(
    address: str,
    bucket: str,
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    region: str = "us-east-1",
) -> Tuple[bool, str]:
    """Create a bucket. Returns (ok, message). 409 = already exists (treated as ok)."""
    status, body = await asyncio.to_thread(
        _do_request, "PUT", address, f"/{bucket}", "", b"", access_key, secret_key, region
    )
    if status in (200, 204, 409):
        return True, "created" if status != 409 else "already exists"
    return False, f"HTTP {status}: {body[:200].decode(errors='replace')}"


async def delete_bucket(
    address: str,
    bucket: str,
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    region: str = "us-east-1",
) -> Tuple[bool, str]:
    """Delete an empty bucket. Returns (ok, message). 404 = already gone (treated as ok)."""
    status, body = await asyncio.to_thread(
        _do_request, "DELETE", address, f"/{bucket}", "", b"", access_key, secret_key, region
    )
    if status in (200, 204, 404):
        return True, "deleted" if status != 404 else "already gone"
    return False, f"HTTP {status}: {body[:200].decode(errors='replace')}"


async def bucket_exists(
    address: str,
    bucket: str,
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    region: str = "us-east-1",
) -> bool:
    """Return True if bucket exists. Raises RuntimeError on unexpected status codes."""
    status, body = await asyncio.to_thread(
        _do_request, "GET", address, f"/{bucket}", "list-type=2&max-keys=1",
        b"", access_key, secret_key, region
    )
    if status == 200:
        return True
    if status == 404:
        return False
    raise RuntimeError(f"Unexpected HTTP {status} checking bucket {bucket}: {body[:200].decode(errors='replace')}")


async def count_objects(
    address: str,
    bucket: str,
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    region: str = "us-east-1",
) -> int:
    """Return number of objects in bucket (first page only, max 1000)."""
    status, body = await asyncio.to_thread(
        _do_request, "GET", address, f"/{bucket}", "list-type=2&max-keys=1000",
        b"", access_key, secret_key, region
    )
    if status == 403:
        raise PermissionError(
            f"ListObjects returned 403 Forbidden for bucket '{bucket}' at {address}. "
            "Check S3 credentials for this pool."
        )
    if status != 200:
        raise RuntimeError(
            f"ListObjects HTTP {status} for bucket '{bucket}' at {address}: "
            f"{body[:200].decode(errors='replace')}"
        )
    root = ET.fromstring(body)
    for elem in root.iter():
        if _strip_ns(elem.tag) == "KeyCount":
            try:
                return int(elem.text or "0")
            except ValueError:
                return 0
    return 0


async def list_objects(
    address: str,
    bucket: str,
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    region: str = "us-east-1",
) -> List[str]:
    """Return all object keys in bucket (paginates automatically)."""
    keys: List[str] = []
    continuation_token = ""

    while True:
        query = "list-type=2&max-keys=1000"
        if continuation_token:
            query += "&continuation-token=" + urllib.parse.quote(continuation_token)

        status, body = await asyncio.to_thread(
            _do_request, "GET", address, f"/{bucket}", query,
            b"", access_key, secret_key, region
        )
        if status != 200:
            break

        root = ET.fromstring(body)
        for elem in root.iter():
            if _strip_ns(elem.tag) == "Key":
                keys.append(elem.text or "")

        is_truncated = False
        next_token = None
        for elem in root.iter():
            t = _strip_ns(elem.tag)
            if t == "IsTruncated":
                is_truncated = (elem.text or "").lower() == "true"
            elif t == "NextContinuationToken":
                next_token = elem.text

        if not is_truncated or not next_token:
            break
        continuation_token = next_token

    return keys


async def get_object(
    address: str,
    bucket: str,
    key: str,
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    region: str = "us-east-1",
) -> Optional[bytes]:
    """Download an object. Returns bytes or None on error."""
    status, body = await asyncio.to_thread(
        _do_request, "GET", address, f"/{bucket}/{key}", "",
        b"", access_key, secret_key, region
    )
    return body if status == 200 else None


async def delete_object(
    address: str,
    bucket: str,
    key: str,
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    region: str = "us-east-1",
) -> bool:
    """Delete an object. Returns True on success (including 404 = already gone)."""
    encoded_key = urllib.parse.quote(key, safe="/")
    status, _ = await asyncio.to_thread(
        _do_request, "DELETE", address, f"/{bucket}/{encoded_key}", "",
        b"", access_key, secret_key, region
    )
    return status in (200, 204, 404)


async def put_object(
    address: str,
    bucket: str,
    key: str,
    data: bytes,
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    region: str = "us-east-1",
) -> bool:
    """Upload an object. Returns True on success."""
    status, _ = await asyncio.to_thread(
        _do_request, "PUT", address, f"/{bucket}/{key}", "", data,
        access_key, secret_key, region,
        extra_headers={"Content-Type": "application/octet-stream"},
    )
    return status in (200, 204)
