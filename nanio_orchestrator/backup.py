"""Automatic SQLite database backup with rotation.

Uses SQLite's online backup API (safe while DB is in use).
Backup triggers:
- After every successful write operation (via trigger_backup())
- On a timed schedule (via backup_loop())
- After every nginx reload (caller invokes trigger_backup())
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

import aiosqlite

from nanio_orchestrator.config import get_settings
from nanio_orchestrator.db import get_db_path

logger = logging.getLogger(__name__)

_stop_event = asyncio.Event()


async def backup_database() -> str | None:
    """Copy SQLite DB to backup location using the online backup API.

    Rotates existing backups (keeps N copies).
    Returns the backup path on success, None on failure.
    """
    s = get_settings()
    src = get_db_path()
    dst = s.effective_db_backup_path

    if not Path(src).exists():
        return None

    Path(dst).parent.mkdir(parents=True, exist_ok=True)

    # Rotate existing backups (blocking I/O — run in thread to avoid stalling event loop)
    await asyncio.to_thread(_rotate_backups, dst, s.db_backup_rotate)

    try:
        async with aiosqlite.connect(src) as src_db:
            async with aiosqlite.connect(dst) as dst_db:
                await src_db.backup(dst_db)
        logger.debug("Database backed up to %s", dst)
        return dst
    except Exception as e:
        logger.error("Database backup failed: %s", e)
        return None


def _rotate_backups(base_path: str, max_copies: int) -> None:
    """Rotate backup files: .bak → .bak.2, .bak.2 → .bak.3, etc."""
    # Remove oldest if it exists
    oldest = f"{base_path}.{max_copies}"
    if os.path.exists(oldest):
        os.unlink(oldest)

    # Shift: .bak.N-1 → .bak.N, ..., .bak.2 → .bak.3
    for i in range(max_copies - 1, 1, -1):
        src = f"{base_path}.{i}"
        dst = f"{base_path}.{i + 1}"
        if os.path.exists(src):
            shutil.move(src, dst)

    # .bak → .bak.2 (only when keeping more than one copy)
    if max_copies >= 2 and os.path.exists(base_path):
        shutil.move(base_path, f"{base_path}.2")


async def trigger_backup() -> None:
    """Trigger a one-off backup (call after write operations)."""
    await backup_database()


async def backup_loop() -> None:
    """Periodic backup loop. Runs until stop_backup() is called."""
    s = get_settings()
    interval = s.db_backup_interval
    _stop_event.clear()

    while not _stop_event.is_set():
        try:
            await asyncio.wait_for(_stop_event.wait(), timeout=interval)
            break  # stop_event was set
        except asyncio.TimeoutError:
            pass  # interval elapsed, do backup

        await backup_database()


def stop_backup() -> None:
    """Signal the backup loop to stop."""
    _stop_event.set()
