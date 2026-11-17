"""The S3 client against a real nanio server.

Skipped unless NANIO_TEST_ENDPOINT (host:port) is set. CI starts a nanio container for it:

    docker run -d -p 9000:9000 -e NANIO_ACCESS_KEY=AKTEST -e NANIO_SECRET_KEY=secretsecret \\
        ghcr.io/apocas/nanio:latest
    NANIO_TEST_ENDPOINT=127.0.0.1:9000 pytest tests/test_s3client_integration.py
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from nanio_orchestrator import s3client

ENDPOINT = os.environ.get("NANIO_TEST_ENDPOINT")
AUTH = {
    "access_key": os.environ.get("NANIO_TEST_ACCESS_KEY", "AKTEST"),
    "secret_key": os.environ.get("NANIO_TEST_SECRET_KEY", "secretsecret"),
}

pytestmark = pytest.mark.skipif(not ENDPOINT, reason="NANIO_TEST_ENDPOINT not set")


@pytest.fixture
async def bucket():
    name = f"it-{uuid.uuid4().hex[:12]}"
    ok, msg = await s3client.create_bucket(ENDPOINT, name, **AUTH)
    assert ok, msg
    yield name
    for key in await s3client.list_objects(ENDPOINT, name, **AUTH):
        await s3client.delete_object(ENDPOINT, name, key, **AUTH)
    await s3client.delete_bucket(ENDPOINT, name, **AUTH)


async def test_bucket_lifecycle():
    name = f"it-{uuid.uuid4().hex[:12]}"
    assert await s3client.bucket_exists(ENDPOINT, name, **AUTH) is False

    ok, _ = await s3client.create_bucket(ENDPOINT, name, **AUTH)
    assert ok
    assert await s3client.bucket_exists(ENDPOINT, name, **AUTH) is True
    assert name in [b["name"] for b in await s3client.list_buckets(ENDPOINT, **AUTH)]

    again, _ = await s3client.create_bucket(ENDPOINT, name, **AUTH)
    assert again  # already exists counts as success

    ok, _ = await s3client.delete_bucket(ENDPOINT, name, **AUTH)
    assert ok
    assert await s3client.bucket_exists(ENDPOINT, name, **AUTH) is False
    gone, _ = await s3client.delete_bucket(ENDPOINT, name, **AUTH)
    assert gone  # already gone counts as success


async def test_objects_roundtrip(bucket):
    assert await s3client.bucket_has_objects(ENDPOINT, bucket, **AUTH) is False
    assert await s3client.count_objects(ENDPOINT, bucket, **AUTH) == 0

    # Keys that exercise the SigV4 canonical path: nested, spaces, unicode, reserved characters
    keys = ["plain.txt", "dir/nested/file.bin", "with space.txt", "ünï/cødé.txt", "a+b=c&d.txt"]
    for i, key in enumerate(keys):
        assert await s3client.put_object(ENDPOINT, bucket, key, f"body-{i}".encode(), **AUTH), key

    assert await s3client.bucket_has_objects(ENDPOINT, bucket, **AUTH) is True
    assert await s3client.count_objects(ENDPOINT, bucket, **AUTH) == len(keys)
    assert sorted(await s3client.list_objects(ENDPOINT, bucket, **AUTH)) == sorted(keys)
    for i, key in enumerate(keys):
        assert await s3client.get_object(ENDPOINT, bucket, key, **AUTH) == f"body-{i}".encode(), key

    assert await s3client.delete_object(ENDPOINT, bucket, "plain.txt", **AUTH)
    assert await s3client.get_object(ENDPOINT, bucket, "plain.txt", **AUTH) is None
    assert await s3client.count_objects(ENDPOINT, bucket, **AUTH) == len(keys) - 1


async def test_listing_paginates(bucket):
    total = 1005  # more than one ListObjectsV2 page
    for i in range(total):
        assert await s3client.put_object(ENDPOINT, bucket, f"k/{i:05d}", b"x", **AUTH)

    assert await s3client.count_objects(ENDPOINT, bucket, **AUTH) == total
    assert len(await s3client.list_objects(ENDPOINT, bucket, **AUTH)) == total


async def test_last_modified_is_what_migration_verification_relies_on(bucket):
    await s3client.put_object(ENDPOINT, bucket, "obj", b"1", **AUTH)

    stamp = await s3client.object_last_modified(ENDPOINT, bucket, "obj", **AUTH)
    assert stamp is not None and stamp.tzinfo is not None
    assert abs(datetime.now(timezone.utc) - stamp) < timedelta(minutes=5)
    assert await s3client.object_last_modified(ENDPOINT, bucket, "missing", **AUTH) is None


async def test_wrong_credentials_are_an_error_not_an_empty_answer(bucket):
    """A signature failure must never look like 'bucket does not exist' — bucket sync and
    promote act on that answer."""
    bad = {"access_key": AUTH["access_key"], "secret_key": "wrong-secret"}

    with pytest.raises(Exception):
        await s3client.list_buckets(ENDPOINT, **bad)
    with pytest.raises(Exception):
        await s3client.bucket_exists(ENDPOINT, bucket, **bad)
    with pytest.raises(Exception):
        await s3client.bucket_has_objects(ENDPOINT, bucket, **bad)


async def test_subfolder_marker_keeps_the_folder_visible_but_is_not_listed(bucket):
    """Sub-folder routes rely on this nanio behaviour: a `.nanio*` object is stored, makes its
    folder appear when the bucket is browsed, and is left out of object listings — so clients
    cannot see or delete it, and an empty-folder check is not fooled by it."""
    import xml.etree.ElementTree as ET

    from nanio_orchestrator.nginx.generator import SUBFOLDER_MARKER

    key = f"2025/{SUBFOLDER_MARKER}"
    assert await s3client.put_object(ENDPOINT, bucket, key, b"marker", **AUTH)
    await s3client.put_object(ENDPOINT, bucket, "2024/a.txt", b"a", **AUTH)

    assert await s3client.get_object(ENDPOINT, bucket, key, **AUTH) == b"marker"
    assert await s3client.list_objects(ENDPOINT, bucket, **AUTH) == ["2024/a.txt"]
    assert await s3client.bucket_has_objects(ENDPOINT, bucket, prefix="2025/", **AUTH) is False
    assert await s3client.bucket_has_objects(ENDPOINT, bucket, prefix="2024/", **AUTH) is True

    status, body = s3client._do_request(
        "GET", ENDPOINT, f"/{bucket}", "delimiter=%2F&list-type=2", b"", AUTH["access_key"], AUTH["secret_key"], "us-east-1"
    )
    assert status == 200
    folders = [el.text for el in ET.fromstring(body).iter() if el.tag.endswith("Prefix") and el.text]
    assert sorted(set(folders)) == ["2024/", "2025/"]

    assert await s3client.delete_object(ENDPOINT, bucket, key, **AUTH)
