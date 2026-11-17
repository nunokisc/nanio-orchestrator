"""The single write path for managed nginx config files.

nginx only includes ``*.conf``, so a candidate config can only be validated once it
is in its final place. Every write therefore follows the same sequence:

1. remember the current content of each target file
2. write the new content atomically (tmp + fsync + rename) at the final path
3. run ``nginx -t`` against the real tree
4. optionally ``nginx -s reload``
5. if step 3 or 4 fails (or the task is cancelled), put the previous content back

The whole sequence runs under one lock, so a concurrent writer can never have its
change validated (or reloaded) against somebody else's half-applied file.
"""

from __future__ import annotations

import asyncio
import logging
import os
import weakref
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Optional

from nanio_orchestrator.nginx import executor
from nanio_orchestrator.nginx.generator import (
    generate_all_configs,
    generate_vhost_config,
    record_file_state,
    remove_config_file,
)

logger = logging.getLogger(__name__)

# One lock per event loop (the lock must be created inside the loop that uses it).
_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = weakref.WeakKeyDictionary()


@dataclass
class ApplyResult:
    ok: bool
    stage: str  # "test" or "reload" — the last step that ran
    output: str


@asynccontextmanager
async def nginx_lock() -> AsyncIterator[None]:
    """Serialize everything that writes managed config files or reloads nginx."""
    loop = asyncio.get_running_loop()
    lock = _locks.get(loop)
    if lock is None:
        lock = _locks[loop] = asyncio.Lock()
    async with lock:
        yield


def _read_bytes(filepath: str) -> Optional[bytes]:
    try:
        with open(filepath, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _write_bytes_atomic(filepath: str, data: bytes) -> None:
    tmp_path = filepath + ".tmp"
    with open(tmp_path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp_path, filepath)
    dir_fd = os.open(os.path.dirname(filepath) or ".", os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _restore(previous: dict[str, Optional[bytes]]) -> None:
    """Put back the content each file had before the apply. Synchronous on purpose:
    it must complete even when the surrounding task is being cancelled."""
    for filepath, old in previous.items():
        try:
            if old is None:
                if os.path.exists(filepath):
                    os.unlink(filepath)
            else:
                _write_bytes_atomic(filepath, old)
        except OSError as exc:
            logger.error("Could not restore %s after failed apply: %s", filepath, exc)


async def apply_config_files(files: Sequence[tuple[str, str]], *, reload: bool = False) -> ApplyResult:
    """Write ``(filepath, content)`` pairs, validate with ``nginx -t`` and optionally reload.

    On any failure every file is restored to its previous content (or removed if it
    did not exist), so the tree on disk is never left in an unvalidated state.
    """
    async with nginx_lock():
        previous: dict[str, Optional[bytes]] = {}
        try:
            for filepath, content in files:
                previous[filepath] = _read_bytes(filepath)
                _write_bytes_atomic(filepath, content.encode("utf-8"))

            test_result = await executor.test_config()
            if not test_result.ok:
                _restore(previous)
                return ApplyResult(ok=False, stage="test", output=test_result.output)

            if not reload:
                return ApplyResult(ok=True, stage="test", output=test_result.output)

            reload_result = await executor.reload_nginx()
            if not reload_result.ok:
                _restore(previous)
            return ApplyResult(ok=reload_result.ok, stage="reload", output=reload_result.output)
        except BaseException:
            _restore(previous)
            raise


async def remove_config_file_checked(filepath: str) -> ApplyResult:
    """Remove a managed file, keeping the removal only if nginx still accepts the tree.

    An upstream file that a vhost still proxies to must not disappear: nginx would refuse
    its whole configuration at the next reload.
    """
    async with nginx_lock():
        previous = {filepath: _read_bytes(filepath)}
        if previous[filepath] is None:
            return ApplyResult(ok=True, stage="test", output="nothing to remove")
        try:
            os.unlink(filepath)
            test_result = await executor.test_config()
            if not test_result.ok:
                _restore(previous)
            return ApplyResult(ok=test_result.ok, stage="test", output=test_result.output)
        except BaseException:
            _restore(previous)
            raise


async def apply_config_file(filepath: str, content: str, *, reload: bool = False) -> ApplyResult:
    """Single-file convenience wrapper around :func:`apply_config_files`."""
    return await apply_config_files([(filepath, content)], reload=reload)


async def reload_nginx_locked() -> executor.NginxExecResult:
    """Reload nginx without racing a config write that has not been validated yet."""
    async with nginx_lock():
        return await executor.reload_nginx()


async def write_vhost_config(vhost_id: int, db) -> tuple:
    """Generate a vhost config from DB state, apply it and record it. Returns (ok, output).

    Does NOT reload nginx — the operator applies changes via the Config tab.
    """
    filepath, content = await generate_vhost_config(vhost_id, db)
    result = await apply_config_file(filepath, content)
    if not result.ok:
        return False, result.output

    await record_file_state(db, filepath, content)
    await db.commit()
    return True, f"nginx -t: {result.output} (config written — apply via Config tab)"


async def rebuild_all_configs() -> dict[str, object]:
    """Regenerate every managed file and every sidecar from the DB, validate and reload.

    Returns {"ok", "stage", "output", "written", "removed"}. On failure the previous
    ``.conf`` files are back in place and nothing is recorded.
    """
    from nanio_orchestrator.db import get_db_ctx
    from nanio_orchestrator.sidecar import write_all_sidecars

    configs = await generate_all_configs()
    to_write = [(fp, ct) for fp, ct in configs if ct is not None]
    removed: list[str] = []

    # A pool without members has no upstream block, so it must not have a file either
    async with get_db_ctx() as db:
        for filepath, content in configs:
            if content is None:
                if os.path.exists(filepath):
                    removed.append(filepath)
                await remove_config_file(filepath)
                await db.execute("DELETE FROM config_files WHERE path = ?", (filepath,))
        await db.commit()

    result = await apply_config_files(to_write, reload=True)
    written: list[str] = []
    if result.ok:
        async with get_db_ctx() as db:
            for filepath, content in to_write:
                await record_file_state(db, filepath, content)
            await write_all_sidecars(db)
            await db.commit()
        written = [fp for fp, _ in to_write]
    return {"ok": result.ok, "stage": result.stage, "output": result.output, "written": written, "removed": removed}


__all__: list[str] = [
    "ApplyResult",
    "apply_config_file",
    "apply_config_files",
    "nginx_lock",
    "rebuild_all_configs",
    "reload_nginx_locked",
    "remove_config_file_checked",
    "write_vhost_config",
]
