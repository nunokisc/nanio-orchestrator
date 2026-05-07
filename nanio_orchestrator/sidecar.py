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
    """Write JSON atomically: write .tmp, fsync, then rename."""
    tmp = filepath + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, filepath)
    # fsync parent directory to ensure rename is durable
    dir_fd = os.open(os.path.dirname(filepath) or ".", os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


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
    extra_blocks_json: Optional[str] = None,
    ip_rule_mode: Optional[str] = None,
    ip_rule_ips_json: Optional[str] = None,
) -> None:
    """Write or update a vhost sidecar file."""
    filepath = vhost_sidecar_path(server_name)
    data = {
        "vhost_id": vhost_id,
        "server_name": server_name,
        "default_pool_id": default_pool_id,
        "default_pool_name": default_pool_name,
        "extra_blocks_json": extra_blocks_json,
        "ip_rule_mode": ip_rule_mode,
        "ip_rule_ips_json": ip_rule_ips_json,
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


def _scan_dir(directory: Path, pattern: str) -> List[dict]:
    results = []
    for p in sorted(directory.glob(pattern)):
        data = _read_json(str(p))
        if data:
            results.append(data)
    return results


def scan_pool_sidecars() -> List[dict]:
    return _scan_dir(get_settings().pools_dir, "*.meta.json")


def scan_vhost_sidecars() -> List[dict]:
    return _scan_dir(get_settings().vhosts_dir, "*.meta.json")


def scan_migration_states() -> List[dict]:
    return _scan_dir(get_settings().migrations_dir, "*.state.json")


# ── Migration completion records ──────────────────────────────────────────────


def migration_completion_path(migration_id: int) -> str:
    """Return the completion record path for a finished migration."""
    s = get_settings()
    return str(s.migrations_dir / f"migration-{migration_id}.done.json")


def write_migration_completion(state: Dict[str, Any]) -> None:
    """Write a permanent completion record for a finished migration.

    Called when a migration reaches 'done'. Unlike state files, completion
    records are never deleted — they allow rebuild to recover orphaned-source
    tracking info after a DB loss.
    """
    filepath = migration_completion_path(state["migration_id"])
    state_copy = dict(state)
    state_copy["written_at"] = _now_iso()
    _write_atomic(filepath, state_copy)
    logger.debug("Wrote migration completion: %s", filepath)


def scan_migration_completions() -> List[dict]:
    return _scan_dir(get_settings().migrations_dir, "*.done.json")
