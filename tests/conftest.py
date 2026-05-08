"""Shared fixtures for nanio-orchestrator tests."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Force dev mode before any imports
os.environ["DEV"] = "true"
os.environ["NANIO_ORCHESTRATOR_API_KEY"] = "test-key"
os.environ["NANIO_ORCHESTRATOR_LOG_LEVEL"] = "warning"
os.environ.setdefault("NANIO_ORCHESTRATOR_S3_REQUEST_TIMEOUT", "2")  # short timeout in tests


@pytest.fixture(scope="session")
def event_loop():
    """Create a session-scoped event loop."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def tmp_dirs(tmp_path):
    """Create temp directories for DB, nginx configs."""
    db_path = str(tmp_path / "test.db")
    nginx_dir = str(tmp_path / "nginx")
    os.makedirs(os.path.join(nginx_dir, "pools"), exist_ok=True)
    os.makedirs(os.path.join(nginx_dir, "vhosts"), exist_ok=True)
    # Migration state files live alongside the DB (db_path.parent/migrations), not in nginx dir
    os.makedirs(str(tmp_path / "migrations"), exist_ok=True)
    return {"db_path": db_path, "nginx_dir": nginx_dir, "tmp_path": tmp_path}


@pytest_asyncio.fixture
async def app(tmp_dirs):
    """Create a test FastAPI app with isolated DB and nginx dirs."""
    os.environ["NANIO_ORCHESTRATOR_DB_PATH"] = tmp_dirs["db_path"]
    os.environ["NANIO_ORCHESTRATOR_NGINX_CONFIG_DIR"] = tmp_dirs["nginx_dir"]
    os.environ["NANIO_ORCHESTRATOR_DRIFT_INTERVAL"] = "999999"
    os.environ["NANIO_ORCHESTRATOR_BUCKET_SYNC_INTERVAL"] = "999999"

    # Reset singletons
    import nanio_orchestrator.config as cfg_mod
    cfg_mod.settings = None

    import nanio_orchestrator.db as db_mod
    db_mod._db_path = None

    from nanio_orchestrator.db import init_db, set_db_path
    set_db_path(tmp_dirs["db_path"])
    await init_db()

    # Mock the background services so they don't actually run
    with patch("nanio_orchestrator.app.drift_loop", new_callable=AsyncMock) as _dl, \
         patch("nanio_orchestrator.app.stop_drift") as _sd, \
         patch("nanio_orchestrator.app.bucket_sync_loop", new_callable=AsyncMock) as _bsl, \
         patch("nanio_orchestrator.app.stop_bucket_sync") as _sbs, \
         patch("nanio_orchestrator.app.recover_interrupted_migrations", new_callable=AsyncMock, return_value=0) as _rim, \
         patch("nanio_orchestrator.app.backup_loop", new_callable=AsyncMock) as _bkl, \
         patch("nanio_orchestrator.app.stop_backup") as _sbk:
        from nanio_orchestrator.app import create_app
        application = create_app()
        yield application

    # Cleanup singletons
    cfg_mod.settings = None
    db_mod._db_path = None


@pytest_asyncio.fixture
async def client(app):
    """Async HTTP client with auth header."""
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.headers["X-Orchestrator-Key"] = "test-key"
        yield c


@pytest_asyncio.fixture
async def db(tmp_dirs):
    """Direct DB connection for test assertions."""
    import nanio_orchestrator.db as db_mod
    db_mod._db_path = None

    from nanio_orchestrator.db import get_db_ctx, init_db, set_db_path
    set_db_path(tmp_dirs["db_path"])
    await init_db()

    async with get_db_ctx() as conn:
        yield conn


# ── Mock fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def mock_nginx():
    """Mock nginx test and reload at all consumption sites."""
    test_result = MagicMock(ok=True, output="syntax ok")
    reload_result = MagicMock(ok=True, output="reloaded")

    test_mock = AsyncMock(return_value=test_result)
    reload_mock = AsyncMock(return_value=reload_result)
    backup_mock = AsyncMock()

    # Patch at all sites that import test_config / reload_nginx
    test_targets = [
        "nanio_orchestrator.nginx.executor.test_config",
        "nanio_orchestrator.api.buckets.test_config",
        "nanio_orchestrator.migration_engine.test_config",
    ]
    reload_targets = [
        "nanio_orchestrator.nginx.executor.reload_nginx",
        "nanio_orchestrator.migration_engine.reload_nginx",
    ]
    backup_targets = [
        "nanio_orchestrator.backup.backup_database",
    ]

    active = []
    for t in test_targets:
        p = patch(t, test_mock)
        active.append(p)
    for t in reload_targets:
        p = patch(t, reload_mock)
        active.append(p)
    for t in backup_targets:
        p = patch(t, backup_mock)
        active.append(p)

    for p in active:
        p.start()

    yield {"test_config": test_mock, "reload_nginx": reload_mock, "trigger_backup": backup_mock}

    for p in active:
        p.stop()


@pytest.fixture
def mock_s3():
    """Mock S3 client operations at all consumption sites."""
    patches = {}
    mocks = {}

    # Functions and the modules that import them
    targets = {
        "list_buckets": [
            "nanio_orchestrator.s3client.list_buckets",
            "nanio_orchestrator.bucket_sync.list_buckets",
        ],
        "create_bucket": [
            "nanio_orchestrator.s3client.create_bucket",
            "nanio_orchestrator.api.buckets.create_bucket",
            "nanio_orchestrator.migration_engine.create_bucket",
        ],
        "bucket_exists": [
            "nanio_orchestrator.migration_engine.bucket_exists",
        ],
        "bucket_has_objects": [
            "nanio_orchestrator.migration_engine.bucket_has_objects",
        ],
        # promote endpoint (api/buckets) checks source bucket for data before
        # allowing route creation without migration.
        "promote_src_has_objects": [
            "nanio_orchestrator.api.buckets.bucket_has_objects",
        ],
        # Separate keys for the API-layer pre-flight checks so existing tests
        # that control the *destination* bucket_exists behaviour (engine-level)
        # don't interfere with the *source* bucket validation in create_migration.
        "src_bucket_exists": [
            "nanio_orchestrator.api.migrations.bucket_exists",
        ],
        "src_bucket_has_objects": [
            "nanio_orchestrator.api.migrations.bucket_has_objects",
        ],
        "list_objects": [
            "nanio_orchestrator.s3client.list_objects",
            "nanio_orchestrator.api.buckets.list_objects",
        ],
        "count_objects": [
            "nanio_orchestrator.s3client.count_objects",
            "nanio_orchestrator.api.buckets.count_objects",
            "nanio_orchestrator.migration_engine.count_objects",
            "nanio_orchestrator.api.vhosts.count_objects",
        ],
        "get_object": [
            "nanio_orchestrator.s3client.get_object",
        ],
        "put_object": [
            "nanio_orchestrator.s3client.put_object",
        ],
    }

    defaults = {
        "list_buckets": [],
        "create_bucket": (True, "created"),
        "bucket_exists": False,          # dst bucket doesn't exist → will be created
        "bucket_has_objects": False,     # dst bucket is empty → migration proceeds
        "promote_src_has_objects": False, # source bucket is empty → promote without migrate ok
        "src_bucket_exists": True,       # source bucket exists by default
        "src_bucket_has_objects": True,  # source bucket has data by default
        "list_objects": [],
        "count_objects": 1,
        "get_object": b"data",
        "put_object": True,
    }

    active_patches = []
    for name, paths in targets.items():
        mock = AsyncMock(return_value=defaults[name])
        mocks[name] = mock
        for path in paths:
            p = patch(path, mock)
            active_patches.append(p)

    for p in active_patches:
        p.start()

    yield mocks

    for p in active_patches:
        p.stop()


@pytest.fixture(autouse=True)
def mock_vhost_s3_calls():
    """Autouse: prevent real S3 calls from vhost route-creation helpers.

    The object-count step in PUT /vhosts/{id}/routes/{id} (migration trigger)
    makes real TCP connections which hang in tests.  Mock it here so tests that
    don't need mock_s3 don't block on network I/O.
    """
    patches = [
        patch("nanio_orchestrator.api.vhosts.count_objects",
              new=AsyncMock(return_value=0)),
    ]
    for p in patches:
        p.start()
    yield
    for p in patches:
        p.stop()


@pytest.fixture
def mock_rclone():
    """Mock rclone subprocess."""
    mock_proc = AsyncMock()
    mock_proc.pid = 12345
    mock_proc.communicate = AsyncMock(return_value=(b"Done\n", b""))
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc) as cse:
        yield {"create_subprocess_exec": cse, "process": mock_proc}


# ── Helper factories ──────────────────────────────────────────────────────────


async def create_pool(client: AsyncClient, name: str = "test-pool", pool_type: str = "nanio", **kwargs) -> dict:
    """Helper: create a pool via API."""
    body = {"name": name, "type": pool_type, **kwargs}
    resp = await client.post("/api/pools", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def create_member(client: AsyncClient, pool_id: int, address: str = "10.0.0.1:9000", **kwargs) -> dict:
    """Helper: add a member to a pool via API."""
    body = {"address": address, **kwargs}
    resp = await client.post(f"/api/pools/{pool_id}/members", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def create_vhost(client: AsyncClient, server_name: str = "test.example.com", **kwargs) -> dict:
    """Helper: create a vhost via API."""
    body = {"server_name": server_name, "ssl": False, **kwargs}
    resp = await client.post("/api/vhosts", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def create_route(client: AsyncClient, vhost_id: int, bucket: str, pool_id: int) -> dict:
    """Helper: create a vhost route for a bucket (required before creating migrations)."""
    resp = await client.post(f"/api/vhosts/{vhost_id}/routes", json={
        "path_prefix": f"/{bucket}/",
        "pool_id": pool_id,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()
