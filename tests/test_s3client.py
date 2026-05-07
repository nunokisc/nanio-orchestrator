"""Tests for S3 client (SigV4 signing, request building)."""

from nanio_orchestrator.s3client import (
    _make_auth_headers,
    _parse_address,
    _sha256_hex,
    _strip_ns,
)


class TestS3ClientHelpers:
    def test_parse_address_with_port(self):
        host, port = _parse_address("10.0.0.1:9000")
        assert host == "10.0.0.1"
        assert port == 9000

    def test_parse_address_no_port(self):
        host, port = _parse_address("s3.example.com")
        assert host == "s3.example.com"
        assert port == 80

    def test_sha256_hex(self):
        h = _sha256_hex(b"")
        assert len(h) == 64
        assert h == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    def test_strip_ns(self):
        assert _strip_ns("{http://s3.amazonaws.com/doc/2006-03-01/}Bucket") == "Bucket"
        assert _strip_ns("Name") == "Name"

    def test_make_auth_headers(self):
        headers = _make_auth_headers(
            method="GET",
            host="10.0.0.1:9000",
            path="/",
            query="",
            body=b"",
            access_key="AKID",
            secret_key="SECRET",
            region="us-east-1",
        )
        assert "Authorization" in headers
        assert "x-amz-date" in headers
        assert "x-amz-content-sha256" in headers
        assert headers["Authorization"].startswith("AWS4-HMAC-SHA256")
        assert "AKID" in headers["Authorization"]
