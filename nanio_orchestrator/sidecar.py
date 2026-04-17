"""Sidecar .meta.json files for data that cannot be reconstructed from nginx configs.

Written atomically (write .tmp → rename) alongside nginx config files.
Three types:
- Pool sidecars:  pools/{name}.meta.json
- Vhost sidecars: vhosts/{server_name}.meta.json
- Migration state: migrations/migration-{id}.state.json
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from nanio_orchestrator.config import get_settings

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_atomic(filepath: str, data: dict) -> None:
    """Write JSON atomically: write .tmp then rename."""
    tmp = filepath + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
        f.write("\n")
    os.rename(tmp, filepath)


def _read_json(filepath: str) -> Optional[dict]:
    """Read a JSON file, return None if not found or invalid."""
    try:
        with open(filepath) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        logger.debug("Could not read sidecar %s: %s", filepath, e)
        return None


def _delete_file(filepath: str) -> None:
    """Delete a file if it exists."""
    try:
        os.unlink(filepath)
    except FileNotFoundError:
        pass


# ── Pool sidecars ─────────────────────────────────────────────────────────────


def pool_sidecar_path(pool_name: str) -> str:
    """Return the sidecar path for a pool."""
    s = get_settings()
    return str(s.pools_dir / f"{pool_name}.meta.json")


def write_pool_sidecar(
    pool_id: int,
    name: str,
    pool_type: str,
    description: Optional[str] = None,
    credentials: Optional[Dict[str, Any]] = None,
) -> None:
    """Write or update a pool sidecar file."""
    filepath = pool_sidecar_path(name)
    data: Dict[str, Any] = {
        "pool_id": pool_id,
        "name": name,
        "type": pool_type,
        "description": description,
        "updated_at": _now_iso(),
    }
    if credentials:
        # Store credentials in encrypted form (pass-through from DB)
        data["credentials"] = credentials
    else:
        # Preserve existing credentials if sidecar already exists
        existing = _read_json(filepath)
        if existing and "credentials" in existing:
            data["credentials"] = existing["credentials"]
    _write_atomic(filepath, data)
    logger.debug("Wrote pool sidecar: %s", filepath)


def read_pool_sidecar(pool_name: str) -> Optional[dict]:
    """Read a pool sidecar file."""
    return _read_json(pool_sidecar_path(pool_name))


def delete_pool_sidecar(pool_name: str) -> None:
    """Delete a pool sidecar file."""
    _delete_file(pool_sidecar_path(pool_name))


def write_pool_credentials_sidecar(
    pool_name: str,
    access_key_enc: str,
    secret_key_enc: str,
    endpoint_url: Optional[str] = None,
    region: str = "us-east-1",
) -> None:
    """Update just the credentials section of a pool sidecar."""
    filepath = pool_sidecar_path(pool_name)
    existing = _read_json(filepath) or {}
    existing["credentials"] = {
        "access_key_enc": access_key_enc,
        "secret_key_enc": secret_key_enc,
        "endpoint_url": endpoint_url,
        "region": region,
    }
    existing["updated_at"] = _now_iso()
    _write_atomic(filepath, existing)


def delete_pool_credentials_sidecar(pool_name: str) -> None:
    """Remove the credentials section from a pool sidecar."""
    filepath = pool_sidecar_path(pool_name)
    existing = _read_json(filepath)
    if existing and "credentials" in existing:
        del existing["credentials"]
        existing["updated_at"] = _now_iso()
        _write_atomic(filepath, existing)


# ── Vhost sidecars ────────────────────────────────────────────────────────────


def vhost_sidecar_path(server_name: str) -> str:
    """Return the sidecar path for a vhost."""
    s = get_settings()
    return str(s.vhosts_dir / f"{server_name}.meta.json")


def write_vhost_sidecar(
    vhost_id: int,
    server_name: str,
    default_pool_id: Optional[int] = None,
    default_pool_name: Optional[str] = None,
) -> None:
    """Write or update a vhost sidecar file."""
    filepath = vhost_sidecar_path(server_name)
    data = {
        "vhost_id": vhost_id,
        "server_name": server_name,
        "default_pool_id": default_pool_id,
        "default_pool_name": default_pool_name,
        "updated_at": _now_iso(),
    }
    _write_atomic(filepath, data)
    logger.debug("Wrote vhost sidecar: %s", filepath)


def read_vhost_sidecar(server_name: str) -> Optional[dict]:
    """Read a vhost sidecar file."""
    return _read_json(vhost_sidecar_path(server_name))


def delete_vhost_sidecar(server_name: str) -> None:
    """Delete a vhost sidecar file."""
    _delete_file(vhost_sidecar_path(server_name))


# ── Migration state files ────────────────────────────────────────────────────


def migration_state_path(migration_id: int) -> str:
    """Return the state file path for a migration."""
    s = get_settings()
    return str(s.migrations_dir / f"migration-{migration_id}.state.json")


def write_migration_state(state: Dict[str, Any]) -> None:
    """Write or update a migration state file."""
    filepath = migration_state_path(state["migration_id"])
    state["updated_at"] = _now_iso()
    _write_atomic(filepath, state)
    logger.debug("Wrote migration state: %s", filepath)


def read_migration_state(migration_id: int) -> Optional[dict]:
    """Read a migration state file."""
    return _read_json(migration_state_path(migration_id))


def delete_migration_state(migration_id: int) -> None:
    """Delete a migration state file."""
    _delete_file(migration_state_path(migration_id))


# ── Scanning ──────────────────────────────────────────────────────────────────


def scan_pool_sidecars() -> List[dict]:
    """Scan all pool sidecar files."""
    s = get_settings()
    results = []
    for p in sorted(s.pools_dir.glob("*.meta.json")):
        data = _read_json(str(p))
        if data:
            results.append(data)
    return results


def scan_vhost_sidecars() -> List[dict]:
    """Scan all vhost sidecar files."""
    s = get_settings()
    results = []
    for p in sorted(s.vhosts_dir.glob("*.meta.json")):
        data = _read_json(str(p))
        if data:
            results.append(data)
    return results


def scan_migration_states() -> List[dict]:
    """Scan all migration state files."""
    s = get_settings()
    results = []
    for p in sorted(s.migrations_dir.glob("*.state.json")):
        data = _read_json(str(p))
        if data:
            results.append(data)
    return results
