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
from typing import Optional

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


def _canonicalize_query_string(query: str) -> str:
    """Sort and URI-encode query parameters per AWS SigV4 spec."""
    if not query:
        return ""
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    sorted_pairs = sorted(pairs, key=lambda p: (p[0], p[1]))
    return "&".join(f"{urllib.parse.quote(k, safe='~')}={urllib.parse.quote(v, safe='~')}" for k, v in sorted_pairs)


def _make_auth_headers(
    method: str,
    host: str,
    path: str,
    query: str,
    body: bytes,
    access_key: str,
    secret_key: str,
    region: str,
    extra_headers: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """Return headers dict with x-amz-date, x-amz-content-sha256, Authorization."""
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    body_hash = _sha256_hex(body)

    signed_hdrs: dict[str, str] = {
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

    canonical_request = "\n".join(
        [
            method.upper(),
            urllib.parse.quote(path, safe="/-_.~"),
            _canonicalize_query_string(query),
            canonical_headers,
            signed_headers_str,
            body_hash,
        ]
    )

    credential_scope = f"{date_stamp}/{region}/s3/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            _sha256_hex(canonical_request.encode()),
        ]
    )

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


def _parse_address(address: str) -> tuple[str, int]:
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
    extra_headers: Optional[dict[str, str]] = None,
    max_body: Optional[int] = None,
) -> tuple[int, bytes]:
    """Synchronous HTTP request — run via asyncio.to_thread().

    With ``max_body`` at most ``max_body + 1`` bytes of the answer are read: enough for the
    caller to tell that the body is larger than it accepts, without holding all of it.
    """
    host, port = _parse_address(address)
    full_host = f"{host}:{port}"

    headers: dict[str, str] = {
        "Host": full_host,
        "Content-Length": str(len(body)),
    }
    if extra_headers:
        headers.update(extra_headers)

    if access_key and secret_key:
        auth_hdrs = _make_auth_headers(
            method,
            full_host,
            path,
            query,
            body,
            access_key,
            secret_key,
            region,
            extra_headers=extra_headers,
        )
        headers.update(auth_hdrs)

    # Use the same unreserved-character set as SigV4 canonical URI:
    # RFC 3986 unreserved = A-Z a-z 0-9 - _ . ~  plus / as path separator
    encoded_path = urllib.parse.quote(path, safe="/-_.~")
    url = f"{encoded_path}?{query}" if query else encoded_path
    conn = http.client.HTTPConnection(host, port, timeout=get_settings().s3_request_timeout)
    try:
        conn.request(method, url, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read() if max_body is None else resp.read(max_body + 1)
    finally:
        conn.close()


# ── XML helpers ───────────────────────────────────────────────────────────────


def _strip_ns(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _page_key_count(root: ET.Element) -> int:
    """Number of keys in a ListObjectsV2 page.

    That is ``KeyCount``; a server that omits it (or sends something unusable) is judged by
    the ``Contents`` entries it lists, so a page with keys never counts as empty.
    """
    contents = 0
    for elem in root.iter():
        tag = _strip_ns(elem.tag)
        if tag == "KeyCount":
            try:
                return int(elem.text or "")
            except ValueError:
                pass
        elif tag == "Contents":
            contents += 1
    return contents


# ── Public async API ──────────────────────────────────────────────────────────


async def list_buckets(
    address: str,
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    region: str = "us-east-1",
) -> list[dict[str, str]]:
    """Return list of buckets: [{"name": str, "created": str}]."""
    status, body = await asyncio.to_thread(_do_request, "GET", address, "/", "", b"", access_key, secret_key, region)
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
) -> tuple[bool, str]:
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
) -> tuple[bool, str]:
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
        _do_request, "GET", address, f"/{bucket}", "list-type=2&max-keys=1", b"", access_key, secret_key, region
    )
    if status == 200:
        return True
    if status == 404:
        return False
    raise RuntimeError(f"Unexpected HTTP {status} checking bucket {bucket}: {body[:200].decode(errors='replace')}")


async def bucket_has_objects(
    address: str,
    bucket: str,
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    region: str = "us-east-1",
    prefix: str = "",
) -> bool:
    """Return True if bucket contains at least one object (single S3 request).

    With ``prefix`` only keys starting with it count. Returns False for non-existent
    buckets (404). Raises RuntimeError on other errors.
    """
    query = "list-type=2&max-keys=1"
    if prefix:
        query += "&prefix=" + urllib.parse.quote(prefix, safe="")
    status, body = await asyncio.to_thread(
        _do_request, "GET", address, f"/{bucket}", query, b"", access_key, secret_key, region
    )
    if status == 404:
        return False
    if status != 200:
        raise RuntimeError(
            f"ListObjects HTTP {status} for bucket '{bucket}' at {address}: {body[:200].decode(errors='replace')}"
        )
    return _page_key_count(ET.fromstring(body)) > 0


async def count_objects(
    address: str,
    bucket: str,
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    region: str = "us-east-1",
    prefix: str = "",
) -> int:
    """Return number of objects in bucket (paginates to count all objects).

    With ``prefix`` only keys starting with it are counted.
    """
    total = 0
    continuation_token = ""

    while True:
        query = "list-type=2&max-keys=1000"
        if prefix:
            query += "&prefix=" + urllib.parse.quote(prefix, safe="")
        if continuation_token:
            query += "&continuation-token=" + urllib.parse.quote(continuation_token)

        status, body = await asyncio.to_thread(
            _do_request, "GET", address, f"/{bucket}", query, b"", access_key, secret_key, region
        )
        if status == 403:
            raise PermissionError(
                f"ListObjects returned 403 Forbidden for bucket '{bucket}' at {address}. "
                "Check S3 credentials for this pool."
            )
        if status != 200:
            raise RuntimeError(
                f"ListObjects HTTP {status} for bucket '{bucket}' at {address}: {body[:200].decode(errors='replace')}"
            )
        root = ET.fromstring(body)
        total += _page_key_count(root)

        is_truncated = False
        next_token = None
        for elem in root.iter():
            t = _strip_ns(elem.tag)
            if t == "IsTruncated":
                is_truncated = (elem.text or "").lower() == "true"
            elif t == "NextContinuationToken":
                next_token = elem.text

        if not is_truncated:
            break
        if not next_token:
            # Reporting what was read so far as the whole bucket would let a migration believe
            # it has converged, or a purge believe it is done.
            raise RuntimeError(
                f"ListObjects for bucket '{bucket}' at {address} says there are more keys but gives no "
                "continuation token"
            )
        continuation_token = next_token

    return total


async def list_objects(
    address: str,
    bucket: str,
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    region: str = "us-east-1",
) -> list[str]:
    """Return all object keys in bucket (paginates automatically)."""
    keys: list[str] = []
    continuation_token = ""

    while True:
        query = "list-type=2&max-keys=1000"
        if continuation_token:
            query += "&continuation-token=" + urllib.parse.quote(continuation_token)

        status, body = await asyncio.to_thread(
            _do_request, "GET", address, f"/{bucket}", query, b"", access_key, secret_key, region
        )
        if status != 200:
            raise RuntimeError(
                f"ListObjects HTTP {status} for bucket '{bucket}' at {address}: {body[:200].decode(errors='replace')}"
            )

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

        if not is_truncated:
            break
        if not next_token:
            # Reporting what was read so far as the whole bucket would let a migration believe
            # it has converged, or a purge believe it is done.
            raise RuntimeError(
                f"ListObjects for bucket '{bucket}' at {address} says there are more keys but gives no "
                "continuation token"
            )
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
    """Download an object. Returns its bytes, or None if it does not exist (404).

    Any other answer raises: "refused" or "failed" must not look like "not there".
    """
    status, body = await asyncio.to_thread(
        _do_request, "GET", address, f"/{bucket}/{key}", "", b"", access_key, secret_key, region
    )
    if status == 200:
        return body
    if status == 404:
        return None
    if status == 403:
        raise PermissionError(f"GetObject returned 403 Forbidden for '{bucket}/{key}' at {address}")
    raise RuntimeError(f"GetObject HTTP {status} for '{bucket}/{key}' at {address}")


def _head_last_modified(
    address: str,
    path: str,
    access_key: Optional[str],
    secret_key: Optional[str],
    region: str,
) -> tuple[int, Optional[datetime]]:
    """Synchronous HEAD — returns (status, Last-Modified as an aware datetime or None)."""
    status, headers = _head(address, path, access_key, secret_key, region)
    return status, _parse_http_date(headers.get("last-modified"))


def _head(
    address: str,
    path: str,
    access_key: Optional[str],
    secret_key: Optional[str],
    region: str,
) -> tuple[int, dict[str, str]]:
    """Synchronous HEAD — returns (status, response headers with lower-cased names)."""
    host, port = _parse_address(address)
    full_host = f"{host}:{port}"
    headers: dict[str, str] = {"Host": full_host}
    if access_key and secret_key:
        headers.update(_make_auth_headers("HEAD", full_host, path, "", b"", access_key, secret_key, region))
    conn = http.client.HTTPConnection(host, port, timeout=get_settings().s3_request_timeout)
    try:
        conn.request("HEAD", urllib.parse.quote(path, safe="/-_.~"), headers=headers)
        resp = conn.getresponse()
        resp.read()
        return resp.status, {name.lower(): value for name, value in resp.getheaders()}
    finally:
        conn.close()


def _parse_http_date(raw: Optional[str]) -> Optional[datetime]:
    """An HTTP date header as an aware datetime, or None when absent or unreadable."""
    from email.utils import parsedate_to_datetime

    if not raw:
        return None
    try:
        modified = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return modified if modified.tzinfo else modified.replace(tzinfo=timezone.utc)


async def object_last_modified(
    address: str,
    bucket: str,
    key: str,
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    region: str = "us-east-1",
) -> Optional[datetime]:
    """Return the object's Last-Modified time, or None if it does not exist.

    Raises RuntimeError on any status other than 200/404.
    """
    status, modified = await asyncio.to_thread(
        _head_last_modified, address, f"/{bucket}/{key}", access_key, secret_key, region
    )
    if status == 404:
        return None
    if status != 200:
        raise RuntimeError(f"Unexpected HTTP {status} on HEAD {bucket}/{key}")
    return modified


async def delete_object(
    address: str,
    bucket: str,
    key: str,
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    region: str = "us-east-1",
) -> bool:
    """Delete an object. Returns True on success (including 404 = already gone)."""
    # Do NOT pre-encode the key here — _do_request handles all percent-encoding.
    # Pre-encoding would cause double-encoding (%20 → %2520) for keys with spaces.
    status, _ = await asyncio.to_thread(
        _do_request, "DELETE", address, f"/{bucket}/{key}", "", b"", access_key, secret_key, region
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
        _do_request,
        "PUT",
        address,
        f"/{bucket}/{key}",
        "",
        data,
        access_key,
        secret_key,
        region,
        extra_headers={"Content-Type": "application/octet-stream"},
    )
    return status in (200, 204)


# ── Browsing (the panel's read-only explorer) ─────────────────────────────────


class ObjectTooLarge(RuntimeError):
    """The object is larger than the caller is willing to hold in memory."""


def _child_text(element: ET.Element, name: str) -> str:
    return next((child.text or "" for child in element if _strip_ns(child.tag) == name), "")


async def list_objects_page(
    address: str,
    bucket: str,
    *,
    prefix: str = "",
    delimiter: str = "/",
    max_keys: int = 200,
    continuation_token: str = "",
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    region: str = "us-east-1",
) -> Optional[dict]:
    """One page of a bucket listing, one level deep.

    Returns ``{"folders": [prefix, ...], "objects": [{"key", "size", "last_modified", "etag"}],
    "next_token": str | None}``, or None when the bucket does not exist (404). Any other
    answer raises: "refused" or "failed" must not look like "empty".
    """
    params = [("list-type", "2"), ("max-keys", str(max_keys))]
    if delimiter:
        params.append(("delimiter", delimiter))
    if prefix:
        params.append(("prefix", prefix))
    if continuation_token:
        params.append(("continuation-token", continuation_token))
    # quote, not quote_plus: a "+" in the query would be read back as a space when it is signed
    query = "&".join(f"{name}={urllib.parse.quote(value, safe='')}" for name, value in params)

    status, body = await asyncio.to_thread(
        _do_request, "GET", address, f"/{bucket}", query, b"", access_key, secret_key, region
    )
    if status == 404:
        return None
    if status == 403:
        raise PermissionError(
            f"ListObjects returned 403 Forbidden for bucket '{bucket}' at {address}. "
            "Check S3 credentials for this pool."
        )
    if status != 200:
        raise RuntimeError(
            f"ListObjects HTTP {status} for bucket '{bucket}' at {address}: {body[:200].decode(errors='replace')}"
        )

    folders: list[str] = []
    objects: list[dict] = []
    truncated = False
    next_token: Optional[str] = None
    for element in ET.fromstring(body):
        tag = _strip_ns(element.tag)
        if tag == "CommonPrefixes":
            folders.append(_child_text(element, "Prefix"))
        elif tag == "Contents":
            size = _child_text(element, "Size")
            objects.append(
                {
                    "key": _child_text(element, "Key"),
                    "size": int(size) if size.isdigit() else 0,
                    "last_modified": _child_text(element, "LastModified") or None,
                    "etag": _child_text(element, "ETag").strip('"') or None,
                }
            )
        elif tag == "IsTruncated":
            truncated = (element.text or "").lower() == "true"
        elif tag == "NextContinuationToken":
            next_token = element.text
    if truncated and not next_token:
        raise RuntimeError(
            f"ListObjects for bucket '{bucket}' at {address} says there are more keys but gives no continuation token"
        )
    return {"folders": folders, "objects": objects, "next_token": next_token if truncated else None}


async def head_object(
    address: str,
    bucket: str,
    key: str,
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    region: str = "us-east-1",
) -> Optional[dict]:
    """Metadata of an object: ``{"size", "last_modified", "etag", "content_type"}``.

    None when it does not exist (404); any other answer raises.
    """
    status, headers = await asyncio.to_thread(_head, address, f"/{bucket}/{key}", access_key, secret_key, region)
    if status == 404:
        return None
    if status == 403:
        raise PermissionError(f"HeadObject returned 403 Forbidden for '{bucket}/{key}' at {address}")
    if status != 200:
        raise RuntimeError(f"HeadObject HTTP {status} for '{bucket}/{key}' at {address}")
    length = headers.get("content-length", "")
    modified = _parse_http_date(headers.get("last-modified"))
    return {
        "size": int(length) if length.isdigit() else None,
        "last_modified": modified.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if modified else None,
        "etag": headers.get("etag", "").strip('"') or None,
        "content_type": headers.get("content-type") or None,
    }


async def get_object_capped(
    address: str,
    bucket: str,
    key: str,
    max_bytes: int,
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    region: str = "us-east-1",
) -> Optional[bytes]:
    """Download an object of at most ``max_bytes``. None if it does not exist (404).

    A larger object raises ObjectTooLarge after reading one byte past the limit, never the
    whole body.
    """
    status, body = await asyncio.to_thread(
        _do_request, "GET", address, f"/{bucket}/{key}", "", b"", access_key, secret_key, region, None, max_bytes
    )
    if status == 404:
        return None
    if status == 403:
        raise PermissionError(f"GetObject returned 403 Forbidden for '{bucket}/{key}' at {address}")
    if status != 200:
        raise RuntimeError(f"GetObject HTTP {status} for '{bucket}/{key}' at {address}")
    if len(body) > max_bytes:
        raise ObjectTooLarge(f"'{bucket}/{key}' is larger than {max_bytes} bytes")
    return body
