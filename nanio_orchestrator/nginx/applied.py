"""What nginx was last told to load, so the panel can say what is waiting for a reload.

A change is written and validated when it is made but applied only by ``nginx -s reload``.
At every successful reload the hash of each managed ``.conf`` file is recorded; a file that
differs from that record now (added, modified or removed) has not been applied.

The record is a file next to the managed config, not a database row: ``nanio-orchestrator
config reload`` runs in another process, and the record has to survive a database rebuild.

Limits, stated rather than hidden: a reload done without the orchestrator (``systemctl
reload nginx``) is not seen, so files changed before it stay listed until the next reload
through the orchestrator; and before the first recorded reload nothing can be said at all.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from nanio_orchestrator.config import get_settings

logger = logging.getLogger(__name__)

STATE_FILE = ".applied.json"


def _state_path() -> Path:
    return Path(get_settings().nginx_config_dir) / STATE_FILE


def managed_hashes() -> dict[str, str]:
    """sha256 of every managed ``.conf`` on disk, by path: exactly what nginx includes."""
    s = get_settings()
    hashes: dict[str, str] = {}
    for directory in (s.pools_dir, s.vhosts_dir):
        for conf in sorted(directory.glob("*.conf")):
            with suppress(FileNotFoundError):  # removed between the listing and the read
                hashes[str(conf)] = hashlib.sha256(conf.read_bytes()).hexdigest()
    return hashes


def record_applied() -> None:
    """Remember the files as they are now: nginx has just loaded them."""
    state = {
        "reloaded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": managed_hashes(),
    }
    path = _state_path()
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(state, indent=1))
        os.replace(tmp, path)
    except OSError as exc:
        # The reload itself succeeded; only the "pending" indicator loses its reference.
        logger.warning("Could not record the applied configuration in %s: %s", path, exc)


def _read_state() -> Optional[dict]:
    try:
        state = json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or not isinstance(state.get("files"), dict):
        return None
    return state


def pending_changes() -> dict:
    """Compare the files on disk with what nginx last loaded.

    ``pending_reload`` is True or False when that is known, and None when no reload has been
    recorded yet while managed files exist: it cannot be told whether nginx runs them.
    """
    current = managed_hashes()
    state = _read_state()
    if state is None:
        return {"pending_reload": None if current else False, "pending_files": [], "last_applied_at": None}

    applied: dict = state["files"]
    changes = [
        {"path": path, "change": "added" if path not in applied else "modified"}
        for path, digest in current.items()
        if applied.get(path) != digest
    ]
    changes += [{"path": path, "change": "removed"} for path in applied if path not in current]
    changes.sort(key=lambda c: c["path"])
    return {
        "pending_reload": bool(changes),
        "pending_files": changes,
        "last_applied_at": state.get("reloaded_at"),
    }
