"""S3 client against an in-process fake S3 server.

The fake answers every request with a canned response and records what it received, so the
tests can assert both on what the client sent and on what it made of the answer.
"""

from __future__ import annotations

import re
import socket
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

import pytest
from nanio_orchestrator import s3client

NS = "http://s3.amazonaws.com/doc/2006-03-01/"
AUTH_RE = re.compile(
    r"^AWS4-HMAC-SHA256 Credential=(?P<ak>[^/]+)/(?P<date>\d{8})/(?P<region>[^/]+)/s3/aws4_request, "
    r"SignedHeaders=(?P<signed>[a-z0-9;-]+), Signature=(?P<sig>[0-9a-f]{64})$"
)


class Recorded:
    def __init__(self, method: str, target: str, headers: dict, body: bytes):
        self.method = method
        self.target = target  # raw request target, exactly as sent on the wire
        self.path, _, self.query = target.partition("?")
        self.headers = headers  # lower-cased names
        self.body = body


class FakeS3:
    """Canned responses, consumed in order; the last one repeats."""

    def __init__(self):
        self.requests: list[Recorded] = []
        self._responses: list[tuple[int, bytes, dict]] = []

    def reply(self, status: int, body: bytes = b"", headers: Optional[dict] = None) -> None:
        self._responses.append((status, body, headers or {}))

    def next_response(self) -> tuple[int, bytes, dict]:
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]

    @property
    def last(self) -> Recorded:
        return self.requests[-1]


@pytest.fixture
def s3():
    fake = FakeS3()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _handle(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            fake.requests.append(
                Recorded(self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body)
            )
            status, payload, headers = fake.next_response()
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

        do_GET = do_PUT = do_DELETE = do_HEAD = _handle

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    fake.address = f"127.0.0.1:{server.server_address[1]}"
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _closed_port_address() -> str:
    """An address on which nothing listens."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"127.0.0.1:{port}"


def list_result(
    keys=(), key_count: Optional[str] = None, truncated: Optional[str] = None, token: Optional[str] = None, ns=True
) -> bytes:
    root = ET.Element("ListBucketResult", {"xmlns": NS} if ns else {})
    if key_count is not None:
        ET.SubElement(root, "KeyCount").text = key_count
    if truncated is not None:
        ET.SubElement(root, "IsTruncated").text = truncated
    if token is not None:
        ET.SubElement(root, "NextContinuationToken").text = token
    for k in keys:
        ET.SubElement(ET.SubElement(root, "Contents"), "Key").text = k
    return ET.tostring(root)


ERROR_403 = b"<Error><Code>AccessDenied</Code><Message>Access Denied</Message></Error>"
ERROR_500 = b"<Error><Code>InternalError</Code></Error>"
ERROR_404 = b"<Error><Code>NoSuchBucket</Code></Error>"


# ── Helpers ───────────────────────────────────────────────────────────────────


class TestHelpers:
    def test_address_with_non_numeric_port_falls_back_to_port_80(self):
        assert s3client._parse_address("host:abc") == ("host:abc", 80)

    def test_canonical_query_string_is_sorted_and_strictly_encoded(self):
        canonical = s3client._canonicalize_query_string("prefix=a%2Fb+c&list-type=2&empty=&max-keys=1")
        assert canonical == "empty=&list-type=2&max-keys=1&prefix=a%2Fb%20c"

    def test_canonical_query_string_of_no_query_is_empty(self):
        assert s3client._canonicalize_query_string("") == ""

    def test_signature_is_deterministic_for_the_same_instant_and_changes_with_the_secret(self, monkeypatch):
        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

        monkeypatch.setattr(s3client, "datetime", FixedDatetime)
        args = ("GET", "h:9000", "/b", "list-type=2", b"")
        one = s3client._make_auth_headers(*args, "AK", "secret", "us-east-1")
        two = s3client._make_auth_headers(*args, "AK", "secret", "us-east-1")
        other = s3client._make_auth_headers(*args, "AK", "other-secret", "us-east-1")
        assert one == two
        assert one["x-amz-date"] == "20260102T030405Z"
        assert "Credential=AK/20260102/us-east-1/s3/aws4_request" in one["Authorization"]
        assert one["Authorization"] != other["Authorization"]

    def test_extra_headers_are_part_of_the_signed_headers(self):
        hdrs = s3client._make_auth_headers(
            "PUT", "h:9000", "/b/k", "", b"x", "AK", "SK", "us-east-1", extra_headers={"Content-Type": "text/plain"}
        )
        assert "SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date" in hdrs["Authorization"]


# ── Signing and what goes on the wire ─────────────────────────────────────────


class TestRequestShape:
    async def test_request_without_credentials_is_unsigned(self, s3):
        s3.reply(200, list_result(key_count="0"))
        await s3client.bucket_exists(s3.address, "b")
        assert "authorization" not in s3.last.headers
        assert "x-amz-date" not in s3.last.headers
        assert s3.last.headers["host"] == s3.address

    @pytest.mark.parametrize("creds", [("AK", None), (None, "SK"), ("", "SK")])
    async def test_half_a_credential_pair_is_not_used_to_sign(self, s3, creds):
        s3.reply(200, list_result(key_count="0"))
        await s3client.bucket_exists(s3.address, "b", creds[0], creds[1])
        assert "authorization" not in s3.last.headers

    async def test_request_with_credentials_carries_a_sigv4_authorization_header(self, s3):
        s3.reply(200, list_result(key_count="0"))
        await s3client.bucket_exists(s3.address, "b", "AKIAEXAMPLE", "s3cr3t", region="eu-west-1")
        req = s3.last
        m = AUTH_RE.match(req.headers["authorization"])
        assert m, req.headers["authorization"]
        assert m.group("ak") == "AKIAEXAMPLE"
        assert m.group("region") == "eu-west-1"
        assert m.group("signed") == "host;x-amz-content-sha256;x-amz-date"
        assert re.fullmatch(r"\d{8}T\d{6}Z", req.headers["x-amz-date"])
        assert req.headers["x-amz-date"].startswith(m.group("date"))
        # empty body hash
        assert req.headers["x-amz-content-sha256"] == s3client._sha256_hex(b"")
        assert "s3cr3t" not in req.headers["authorization"]

    async def test_signature_covers_the_request_that_was_actually_sent(self, s3):
        """Recompute the signature from the recorded request: it must match what was sent."""
        s3.reply(200, list_result(key_count="1"))
        await s3client.bucket_has_objects(s3.address, "my bucket", "AK", "SK", prefix="dir/a b")
        req = s3.last
        m = AUTH_RE.match(req.headers["authorization"])
        canonical = "\n".join(
            [
                "GET",
                req.path,  # already percent-encoded on the wire
                "&".join(sorted(req.query.split("&"))),
                f"host:{req.headers['host']}\n"
                f"x-amz-content-sha256:{req.headers['x-amz-content-sha256']}\n"
                f"x-amz-date:{req.headers['x-amz-date']}\n",
                "host;x-amz-content-sha256;x-amz-date",
                req.headers["x-amz-content-sha256"],
            ]
        )
        scope = f"{m.group('date')}/us-east-1/s3/aws4_request"
        to_sign = "\n".join(
            ["AWS4-HMAC-SHA256", req.headers["x-amz-date"], scope, s3client._sha256_hex(canonical.encode())]
        )
        key = s3client._signing_key("SK", m.group("date"), "us-east-1", "s3")
        assert s3client._hmac_sha256(key, to_sign).hex() == m.group("sig")

    async def test_object_key_is_percent_encoded_exactly_once(self, s3):
        s3.reply(204)
        assert await s3client.delete_object(s3.address, "b", "dir/my file+ü%.txt") is True
        assert s3.last.method == "DELETE"
        assert s3.last.target == "/b/dir/my%20file%2B%C3%BC%25.txt"

    async def test_put_object_sends_body_length_and_content_type(self, s3):
        s3.reply(200)
        assert await s3client.put_object(s3.address, "b", "k.bin", b"\x00\x01payload", "AK", "SK") is True
        req = s3.last
        assert (req.method, req.target) == ("PUT", "/b/k.bin")
        assert req.body == b"\x00\x01payload"
        assert req.headers["content-length"] == "9"
        assert req.headers["content-type"] == "application/octet-stream"
        assert req.headers["x-amz-content-sha256"] == s3client._sha256_hex(b"\x00\x01payload")
        assert "content-type" in AUTH_RE.match(req.headers["authorization"]).group("signed").split(";")

    async def test_unreachable_host_raises_instead_of_looking_like_an_answer(self):
        address = _closed_port_address()
        with pytest.raises(OSError):
            await s3client.bucket_exists(address, "b")
        with pytest.raises(OSError):
            await s3client.bucket_has_objects(address, "b")
        with pytest.raises(OSError):
            await s3client.count_objects(address, "b")
        with pytest.raises(OSError):
            await s3client.list_buckets(address)
        with pytest.raises(OSError):
            await s3client.object_last_modified(address, "b", "k")


# ── ListBuckets ───────────────────────────────────────────────────────────────


class TestListBuckets:
    async def test_buckets_are_returned_with_name_and_creation_date(self, s3):
        s3.reply(
            200,
            f'<ListAllMyBucketsResult xmlns="{NS}"><Owner><ID>o</ID></Owner><Buckets>'
            "<Bucket><Name>alpha</Name><CreationDate>2026-01-01T00:00:00.000Z</CreationDate></Bucket>"
            "<Bucket><Name>beta</Name></Bucket>"
            "<Bucket><Name>gamma</Name><CreationDate/></Bucket>"
            "</Buckets></ListAllMyBucketsResult>".encode(),
        )
        buckets = await s3client.list_buckets(s3.address, "AK", "SK")
        assert buckets == [
            {"name": "alpha", "created": "2026-01-01T00:00:00.000Z"},
            {"name": "beta", "created": ""},
            {"name": "gamma", "created": ""},
        ]
        assert (s3.last.method, s3.last.target) == ("GET", "/")
        assert AUTH_RE.match(s3.last.headers["authorization"])

    async def test_bucket_entries_without_a_name_are_skipped(self, s3):
        s3.reply(
            200,
            b"<ListAllMyBucketsResult><Buckets>"
            b"<Bucket><CreationDate>2026-01-01T00:00:00Z</CreationDate></Bucket>"
            b"<Bucket><Name></Name></Bucket>"
            b"<Bucket><Name>ok</Name></Bucket>"
            b"</Buckets></ListAllMyBucketsResult>",
        )
        assert await s3client.list_buckets(s3.address) == [{"name": "ok", "created": ""}]

    async def test_no_buckets_is_an_empty_list(self, s3):
        s3.reply(200, b"<ListAllMyBucketsResult><Buckets/></ListAllMyBucketsResult>")
        assert await s3client.list_buckets(s3.address) == []

    @pytest.mark.parametrize("status,body", [(403, ERROR_403), (404, ERROR_404), (500, ERROR_500)])
    async def test_error_status_raises_and_never_returns_an_empty_list(self, s3, status, body):
        s3.reply(status, body)
        with pytest.raises(RuntimeError, match=f"ListBuckets HTTP {status}") as exc:
            await s3client.list_buckets(s3.address, "AK", "SK")
        assert body.decode()[:20] in str(exc.value)

    async def test_malformed_xml_raises(self, s3):
        s3.reply(200, b"<ListAllMyBucketsResult><Buckets>")
        with pytest.raises(ET.ParseError):
            await s3client.list_buckets(s3.address)


# ── Bucket create / delete / exists ───────────────────────────────────────────


class TestBucketLifecycle:
    @pytest.mark.parametrize("status,expected", [(200, "created"), (204, "created"), (409, "already exists")])
    async def test_create_bucket_success_statuses(self, s3, status, expected):
        s3.reply(status)
        assert await s3client.create_bucket(s3.address, "new-bucket", "AK", "SK") == (True, expected)
        assert (s3.last.method, s3.last.target) == ("PUT", "/new-bucket")
        assert s3.last.headers["content-length"] == "0"

    @pytest.mark.parametrize("status,body", [(403, ERROR_403), (500, ERROR_500)])
    async def test_create_bucket_failure_reports_status_and_body(self, s3, status, body):
        s3.reply(status, body)
        ok, msg = await s3client.create_bucket(s3.address, "new-bucket")
        assert ok is False
        assert msg == f"HTTP {status}: {body.decode()}"

    @pytest.mark.parametrize("status,expected", [(200, "deleted"), (204, "deleted"), (404, "already gone")])
    async def test_delete_bucket_success_statuses(self, s3, status, expected):
        s3.reply(status)
        assert await s3client.delete_bucket(s3.address, "old", "AK", "SK") == (True, expected)
        assert (s3.last.method, s3.last.target) == ("DELETE", "/old")

    @pytest.mark.parametrize("status", [403, 409, 500])
    async def test_delete_bucket_failure_reports_status(self, s3, status):
        s3.reply(status, b"<Error><Code>BucketNotEmpty</Code></Error>")
        ok, msg = await s3client.delete_bucket(s3.address, "old")
        assert ok is False
        assert msg.startswith(f"HTTP {status}: ") and "BucketNotEmpty" in msg

    async def test_bucket_exists_is_true_on_200_and_false_on_404(self, s3):
        s3.reply(200, list_result(key_count="0"))
        s3.reply(404, ERROR_404)
        assert await s3client.bucket_exists(s3.address, "b") is True
        assert (s3.last.method, s3.last.target) == ("GET", "/b?list-type=2&max-keys=1")
        assert await s3client.bucket_exists(s3.address, "b") is False

    @pytest.mark.parametrize("status,body", [(403, ERROR_403), (500, ERROR_500), (301, b"")])
    async def test_bucket_exists_raises_on_any_other_status(self, s3, status, body):
        """A refused or failed request says nothing about whether the bucket exists."""
        s3.reply(status, body)
        with pytest.raises(RuntimeError, match=f"Unexpected HTTP {status} checking bucket b"):
            await s3client.bucket_exists(s3.address, "b", "AK", "SK")


# ── bucket_has_objects ────────────────────────────────────────────────────────


class TestBucketHasObjects:
    async def test_key_count_above_zero_means_objects(self, s3):
        s3.reply(200, list_result(keys=["a"], key_count="1"))
        assert await s3client.bucket_has_objects(s3.address, "b", "AK", "SK") is True
        assert s3.last.target == "/b?list-type=2&max-keys=1"

    async def test_key_count_zero_means_empty(self, s3):
        s3.reply(200, list_result(key_count="0"))
        assert await s3client.bucket_has_objects(s3.address, "b") is False

    async def test_prefix_is_sent_fully_encoded(self, s3):
        s3.reply(200, list_result(key_count="0"))
        await s3client.bucket_has_objects(s3.address, "b", prefix="dir/sub dir/é&x=1")
        assert s3.last.query == "list-type=2&max-keys=1&prefix=dir%2Fsub%20dir%2F%C3%A9%26x%3D1"

    async def test_missing_bucket_has_no_objects(self, s3):
        s3.reply(404, ERROR_404)
        assert await s3client.bucket_has_objects(s3.address, "b") is False

    @pytest.mark.parametrize("status,body", [(403, ERROR_403), (500, ERROR_500), (503, b"busy")])
    async def test_error_status_raises_instead_of_reporting_empty(self, s3, status, body):
        s3.reply(status, body)
        with pytest.raises(RuntimeError, match=f"ListObjects HTTP {status} for bucket 'b'"):
            await s3client.bucket_has_objects(s3.address, "b", "AK", "SK")

    async def test_listing_without_key_count_is_judged_by_its_contents(self, s3):
        """A server that omits KeyCount but lists a key must not make the bucket look empty."""
        s3.reply(200, list_result(keys=["a"]))
        assert await s3client.bucket_has_objects(s3.address, "b") is True

    @pytest.mark.parametrize("key_count", ["many", ""])
    async def test_unusable_key_count_is_judged_by_the_contents(self, s3, key_count):
        s3.reply(200, list_result(keys=["a"], key_count=key_count))
        s3.reply(200, list_result(key_count=key_count))
        assert await s3client.bucket_has_objects(s3.address, "b") is True
        assert await s3client.bucket_has_objects(s3.address, "b") is False

    async def test_listing_without_key_count_and_without_contents_is_empty(self, s3):
        s3.reply(200, list_result(ns=False))
        assert await s3client.bucket_has_objects(s3.address, "b") is False

    async def test_malformed_xml_raises(self, s3):
        s3.reply(200, b"not xml at all")
        with pytest.raises(ET.ParseError):
            await s3client.bucket_has_objects(s3.address, "b")


# ── count_objects ─────────────────────────────────────────────────────────────


class TestCountObjects:
    async def test_single_page_count(self, s3):
        s3.reply(200, list_result(keys=["a", "b", "c"], key_count="3", truncated="false"))
        assert await s3client.count_objects(s3.address, "b", "AK", "SK") == 3
        assert len(s3.requests) == 1
        assert s3.last.target == "/b?list-type=2&max-keys=1000"

    async def test_pages_are_followed_with_the_continuation_token_and_summed(self, s3):
        s3.reply(200, list_result(key_count="1000", truncated="true", token="tok+1/=="))
        s3.reply(200, list_result(key_count="1000", truncated="True", token="tok 2"))
        s3.reply(200, list_result(key_count="7", truncated="false"))
        assert await s3client.count_objects(s3.address, "b", "AK", "SK", prefix="p/") == 2007
        assert [r.query for r in s3.requests] == [
            "list-type=2&max-keys=1000&prefix=p%2F",
            "list-type=2&max-keys=1000&prefix=p%2F&continuation-token=tok%2B1/%3D%3D",
            "list-type=2&max-keys=1000&prefix=p%2F&continuation-token=tok%202",
        ]
        assert all(AUTH_RE.match(r.headers["authorization"]) for r in s3.requests)

    async def test_truncated_page_without_a_token_is_an_error_not_a_total(self, s3):
        """A partial count taken for the whole bucket would let a migration believe it converged."""
        s3.reply(200, list_result(key_count="5", truncated="true"))
        with pytest.raises(RuntimeError, match="more keys but gives no continuation token"):
            await s3client.count_objects(s3.address, "b")

    async def test_empty_bucket_counts_zero(self, s3):
        s3.reply(200, list_result(key_count="0", truncated="false"))
        assert await s3client.count_objects(s3.address, "b") == 0

    async def test_page_without_key_count_is_counted_by_its_contents(self, s3):
        s3.reply(200, list_result(keys=["a", "b"], truncated="true", token="t"))
        s3.reply(200, list_result(keys=["c"], key_count="oops"))
        s3.reply(200, list_result(keys=["c"], key_count=""))
        assert await s3client.count_objects(s3.address, "b") == 3
        assert await s3client.count_objects(s3.address, "b") == 1

    async def test_forbidden_is_a_permission_error_naming_the_bucket(self, s3):
        s3.reply(403, ERROR_403)
        with pytest.raises(PermissionError, match="403 Forbidden for bucket 'b'"):
            await s3client.count_objects(s3.address, "b", "AK", "bad")

    @pytest.mark.parametrize("status,body", [(404, ERROR_404), (500, ERROR_500)])
    async def test_other_error_status_raises(self, s3, status, body):
        s3.reply(status, body)
        with pytest.raises(RuntimeError, match=f"ListObjects HTTP {status}"):
            await s3client.count_objects(s3.address, "b")

    async def test_error_on_a_later_page_raises_instead_of_returning_a_partial_count(self, s3):
        s3.reply(200, list_result(key_count="1000", truncated="true", token="t"))
        s3.reply(500, ERROR_500)
        with pytest.raises(RuntimeError, match="ListObjects HTTP 500"):
            await s3client.count_objects(s3.address, "b")


# ── list_objects ──────────────────────────────────────────────────────────────


class TestListObjects:
    async def test_keys_of_all_pages_are_returned_in_order(self, s3):
        s3.reply(200, list_result(keys=["a", "dir/b"], key_count="2", truncated="true", token="next"))
        s3.reply(200, list_result(keys=["c"], key_count="1", truncated="false"))
        assert await s3client.list_objects(s3.address, "b", "AK", "SK") == ["a", "dir/b", "c"]
        assert [r.target for r in s3.requests] == [
            "/b?list-type=2&max-keys=1000",
            "/b?list-type=2&max-keys=1000&continuation-token=next",
        ]

    async def test_empty_bucket_lists_nothing(self, s3):
        s3.reply(200, list_result(key_count="0"))
        assert await s3client.list_objects(s3.address, "b") == []

    async def test_truncated_page_without_a_token_is_an_error_not_a_listing(self, s3):
        s3.reply(200, list_result(keys=["a"], truncated="true"))
        with pytest.raises(RuntimeError, match="more keys but gives no continuation token"):
            await s3client.list_objects(s3.address, "b")

    async def test_empty_key_element_is_kept_as_an_empty_key(self, s3):
        s3.reply(200, b"<ListBucketResult><Contents><Key/></Contents></ListBucketResult>")
        assert await s3client.list_objects(s3.address, "b") == [""]

    @pytest.mark.parametrize("status,body", [(403, ERROR_403), (404, ERROR_404), (500, ERROR_500)])
    async def test_error_status_raises_instead_of_listing_nothing(self, s3, status, body):
        s3.reply(status, body)
        with pytest.raises(RuntimeError, match=f"ListObjects HTTP {status} for bucket 'b'"):
            await s3client.list_objects(s3.address, "b")

    async def test_error_on_a_later_page_raises(self, s3):
        s3.reply(200, list_result(keys=["a"], truncated="true", token="t"))
        s3.reply(403, ERROR_403)
        with pytest.raises(RuntimeError, match="ListObjects HTTP 403"):
            await s3client.list_objects(s3.address, "b")


# ── Objects ───────────────────────────────────────────────────────────────────


class TestObjects:
    async def test_get_object_returns_the_body(self, s3):
        s3.reply(200, b"\x00binary\xff")
        assert await s3client.get_object(s3.address, "b", "dir/k", "AK", "SK") == b"\x00binary\xff"
        assert (s3.last.method, s3.last.target) == ("GET", "/b/dir/k")

    async def test_get_object_returns_none_only_when_the_object_does_not_exist(self, s3):
        s3.reply(404, b"<Error/>")
        assert await s3client.get_object(s3.address, "b", "k") is None

    @pytest.mark.parametrize("status,error", [(403, PermissionError), (500, RuntimeError)])
    async def test_get_object_refused_or_failed_is_not_reported_as_missing(self, s3, status, error):
        s3.reply(status, b"<Error/>")
        with pytest.raises(error, match=f"GetObject.*{status}"):
            await s3client.get_object(s3.address, "b", "k")

    @pytest.mark.parametrize("status,ok", [(200, True), (204, True), (404, True), (403, False), (500, False)])
    async def test_delete_object_result_by_status(self, s3, status, ok):
        s3.reply(status)
        assert await s3client.delete_object(s3.address, "b", "k", "AK", "SK") is ok

    @pytest.mark.parametrize("status,ok", [(200, True), (204, True), (403, False), (404, False), (500, False)])
    async def test_put_object_result_by_status(self, s3, status, ok):
        s3.reply(status)
        assert await s3client.put_object(s3.address, "b", "k", b"data") is ok
        assert "authorization" not in s3.last.headers


# ── HEAD / Last-Modified ──────────────────────────────────────────────────────


class TestObjectLastModified:
    async def test_last_modified_is_returned_as_an_aware_datetime(self, s3):
        s3.reply(200, headers={"Last-Modified": "Wed, 21 Oct 2026 07:28:00 GMT"})
        modified = await s3client.object_last_modified(s3.address, "b", "dir/my key.txt", "AK", "SK")
        assert modified == datetime(2026, 10, 21, 7, 28, tzinfo=timezone.utc)
        assert (s3.last.method, s3.last.target) == ("HEAD", "/b/dir/my%20key.txt")
        assert AUTH_RE.match(s3.last.headers["authorization"])

    async def test_last_modified_without_a_zone_is_taken_as_utc(self, s3):
        s3.reply(200, headers={"Last-Modified": "Wed, 21 Oct 2026 07:28:00 -0000"})
        modified = await s3client.object_last_modified(s3.address, "b", "k")
        assert modified == datetime(2026, 10, 21, 7, 28, tzinfo=timezone.utc)
        assert "authorization" not in s3.last.headers

    async def test_missing_object_has_no_last_modified(self, s3):
        s3.reply(404)
        assert await s3client.object_last_modified(s3.address, "b", "k") is None

    async def test_existing_object_without_the_header_has_no_last_modified(self, s3):
        s3.reply(200)
        assert await s3client.object_last_modified(s3.address, "b", "k") is None

    async def test_unparseable_last_modified_is_none(self, s3):
        s3.reply(200, headers={"Last-Modified": "yesterday-ish"})
        assert await s3client.object_last_modified(s3.address, "b", "k") is None

    @pytest.mark.parametrize("status", [403, 500])
    async def test_error_status_raises_instead_of_reporting_a_missing_object(self, s3, status):
        s3.reply(status)
        with pytest.raises(RuntimeError, match=f"Unexpected HTTP {status} on HEAD b/k"):
            await s3client.object_last_modified(s3.address, "b", "k", "AK", "SK")


# ── Browsing: one page, one level ─────────────────────────────────────────────


def page_result(objects=(), folders=(), truncated: Optional[str] = None, token: Optional[str] = None) -> bytes:
    root = ET.Element("ListBucketResult", {"xmlns": NS})
    ET.SubElement(root, "Name").text = "b"
    if truncated is not None:
        ET.SubElement(root, "IsTruncated").text = truncated
    if token is not None:
        ET.SubElement(root, "NextContinuationToken").text = token
    for fields in objects:
        contents = ET.SubElement(root, "Contents")
        for name, value in fields.items():
            ET.SubElement(contents, name).text = value
    for prefix in folders:
        ET.SubElement(ET.SubElement(root, "CommonPrefixes"), "Prefix").text = prefix
    return ET.tostring(root)


class TestListObjectsPage:
    async def test_folders_and_objects_of_one_level(self, s3):
        s3.reply(
            200,
            page_result(
                objects=[
                    {
                        "Key": "2025/a b.txt",
                        "Size": "12",
                        "LastModified": "2026-01-02T03:04:05.000Z",
                        "ETag": '"0123abcd"',
                    },
                    {"Key": "2025/empty"},
                ],
                folders=["2025/q1/", "2025/q2/"],
                truncated="false",
            ),
        )
        page = await s3client.list_objects_page(s3.address, "b", prefix="2025/", access_key="AK", secret_key="SK")
        assert page == {
            "folders": ["2025/q1/", "2025/q2/"],
            "objects": [
                {
                    "key": "2025/a b.txt",
                    "size": 12,
                    "last_modified": "2026-01-02T03:04:05.000Z",
                    "etag": "0123abcd",
                },
                {"key": "2025/empty", "size": 0, "last_modified": None, "etag": None},
            ],
            "next_token": None,
        }
        assert s3.last.method == "GET"
        assert s3.last.target == "/b?list-type=2&max-keys=200&delimiter=%2F&prefix=2025%2F"
        assert AUTH_RE.match(s3.last.headers["authorization"])

    async def test_prefix_and_token_with_reserved_characters_are_percent_encoded_and_signed(self, s3):
        """Spaces, unicode, '+' and '&' in a key must reach the server as themselves, and the
        signature must cover exactly what was sent."""
        import hashlib
        import hmac

        s3.reply(200, page_result(truncated="false"))
        await s3client.list_objects_page(
            s3.address,
            "b",
            prefix="relatórios 2025/a+b&c=d/",
            continuation_token="tok+en/=&x",
            max_keys=50,
            access_key="AK",
            secret_key="SK",
        )
        req = s3.last
        encoded_prefix = "prefix=relat%C3%B3rios%202025%2Fa%2Bb%26c%3Dd%2F"
        assert req.query == (
            f"list-type=2&max-keys=50&delimiter=%2F&{encoded_prefix}&continuation-token=tok%2Ben%2F%3D%26x"
        )
        canonical_query = s3client._canonicalize_query_string(req.query)
        assert encoded_prefix in canonical_query  # decoded and re-encoded: nothing was lost on the way

        m = AUTH_RE.match(req.headers["authorization"])
        canonical = "\n".join(
            [
                "GET",
                "/b",
                canonical_query,
                f"host:{s3.address}\nx-amz-content-sha256:{req.headers['x-amz-content-sha256']}\n"
                f"x-amz-date:{req.headers['x-amz-date']}\n",
                "host;x-amz-content-sha256;x-amz-date",
                req.headers["x-amz-content-sha256"],
            ]
        )
        to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                req.headers["x-amz-date"],
                f"{m.group('date')}/us-east-1/s3/aws4_request",
                s3client._sha256_hex(canonical.encode()),
            ]
        )
        key = s3client._signing_key("SK", m.group("date"), "us-east-1", "s3")
        assert m.group("sig") == hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()

    async def test_recursive_listing_sends_no_delimiter(self, s3):
        s3.reply(200, page_result())
        await s3client.list_objects_page(s3.address, "b", delimiter="")
        assert s3.last.target == "/b?list-type=2&max-keys=200"

    async def test_truncated_page_returns_the_token_to_continue_with(self, s3):
        s3.reply(200, page_result(objects=[{"Key": "k1"}], truncated="true", token="next-1"))
        page = await s3client.list_objects_page(s3.address, "b")
        assert page["next_token"] == "next-1"

    async def test_token_of_a_page_that_is_not_truncated_is_ignored(self, s3):
        s3.reply(200, page_result(truncated="false", token="stale"))
        assert (await s3client.list_objects_page(s3.address, "b"))["next_token"] is None

    async def test_truncated_page_without_a_token_is_an_error_not_the_end(self, s3):
        s3.reply(200, page_result(objects=[{"Key": "k1"}], truncated="true"))
        with pytest.raises(RuntimeError, match="no continuation token"):
            await s3client.list_objects_page(s3.address, "b")

    async def test_missing_bucket_is_none(self, s3):
        s3.reply(404, ERROR_404)
        assert await s3client.list_objects_page(s3.address, "b") is None

    @pytest.mark.parametrize(
        "status,error,match", [(403, PermissionError, "403 Forbidden"), (500, RuntimeError, "HTTP 500")]
    )
    async def test_refused_or_failed_is_never_an_empty_listing(self, s3, status, error, match):
        s3.reply(status, ERROR_403)
        with pytest.raises(error, match=match):
            await s3client.list_objects_page(s3.address, "b")

    async def test_unreachable_server_raises(self):
        with pytest.raises(OSError):
            await s3client.list_objects_page(_closed_port_address(), "b")


class TestHeadObject:
    async def test_metadata_of_an_object(self, s3):
        s3.reply(
            200,
            b"x" * 42,
            headers={
                "Last-Modified": "Wed, 21 Oct 2026 07:28:00 GMT",
                "ETag": '"abc123"',
                "Content-Type": "image/png",
            },
        )
        meta = await s3client.head_object(s3.address, "b", "dir/my key+ç&.png", "AK", "SK")
        assert meta == {
            "size": 42,
            "last_modified": "2026-10-21T07:28:00Z",
            "etag": "abc123",
            "content_type": "image/png",
        }
        assert (s3.last.method, s3.last.target) == ("HEAD", "/b/dir/my%20key%2B%C3%A7%26.png")
        assert AUTH_RE.match(s3.last.headers["authorization"])

    async def test_headers_the_server_does_not_send_are_none(self, s3, monkeypatch):
        monkeypatch.setattr(s3client, "_head", lambda *a: (200, {}))
        assert await s3client.head_object(s3.address, "b", "k") == {
            "size": None,
            "last_modified": None,
            "etag": None,
            "content_type": None,
        }

    async def test_missing_object_is_none(self, s3):
        s3.reply(404)
        assert await s3client.head_object(s3.address, "b", "k") is None

    @pytest.mark.parametrize("status,error", [(403, PermissionError), (500, RuntimeError)])
    async def test_refused_or_failed_is_not_reported_as_missing(self, s3, status, error):
        s3.reply(status)
        with pytest.raises(error, match=f"HeadObject.*{status}"):
            await s3client.head_object(s3.address, "b", "k")


class TestGetObjectCapped:
    async def test_object_within_the_limit_is_returned_whole(self, s3):
        s3.reply(200, b"0123456789")
        assert await s3client.get_object_capped(s3.address, "b", "a b/k", 10, "AK", "SK") == b"0123456789"
        assert (s3.last.method, s3.last.target) == ("GET", "/b/a%20b/k")

    async def test_larger_object_is_refused_after_one_byte_past_the_limit(self, s3, monkeypatch):
        read_sizes = []
        real_read = s3client.http.client.HTTPResponse.read

        def read(self, amt=None):
            read_sizes.append(amt)
            return real_read(self, amt)

        monkeypatch.setattr(s3client.http.client.HTTPResponse, "read", read)
        s3.reply(200, b"x" * 1000)
        with pytest.raises(s3client.ObjectTooLarge, match="larger than 10 bytes"):
            await s3client.get_object_capped(s3.address, "b", "k", 10)
        assert read_sizes == [11]

    async def test_missing_object_is_none(self, s3):
        s3.reply(404, b"<Error/>")
        assert await s3client.get_object_capped(s3.address, "b", "k", 10) is None

    @pytest.mark.parametrize("status,error", [(403, PermissionError), (500, RuntimeError)])
    async def test_refused_or_failed_is_not_reported_as_missing(self, s3, status, error):
        s3.reply(status, b"<Error/>")
        with pytest.raises(error, match=f"GetObject.*{status}"):
            await s3client.get_object_capped(s3.address, "b", "k", 10)
