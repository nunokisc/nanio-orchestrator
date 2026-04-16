"""Background drift detection task.

Runs every N seconds, checks SHA256 of each managed config file on disk
against the last known hash in the database. Surfaces alerts but never
auto-corrects — operator decides.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import aiofiles

from nanio_orchestrator.config import get_settings
from nanio_orchestrator.db import get_db_ctx
from nanio_orchestrator.nginx.generator import sha256_str

logger = logging.getLogger(__name__)

_running = False


async def check_drift_once() -> list[dict]:
    """Run a single drift check. Returns list of drifted files."""
    drifted = []
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall("SELECT * FROM config_files")
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        for r in rows:
            filepath = r["path"]
            try:
                async with aiofiles.open(filepath, "r") as f:
                    content = await f.read()
                disk_hash = sha256_str(content)
            except FileNotFoundError:
                disk_hash = None
            except Exception as e:
                logger.warning("Cannot read %s: %s", filepath, e)
                continue

            db_hash = r["sha256_db"]

            if disk_hash != db_hash:
                drifted.append({
                    "path": filepath,
                    "sha256_disk": disk_hash,
                    "sha256_db": db_hash,
                    "status": "missing" if disk_hash is None else "modified",
                })
                logger.warning(
                    "Drift detected: %s (disk=%s, db=%s)",
                    filepath,
                    disk_hash or "MISSING",
                    db_hash or "NONE",
                )

            # Update disk hash in DB
            await db.execute(
                "UPDATE config_files SET sha256_disk = ? WHERE path = ?",
                (disk_hash, filepath),
            )
        await db.commit()

    if not drifted:
        logger.debug("Drift check: all files in sync")
    return drifted


async def drift_loop() -> None:
    """Run drift detection in a loop."""
    global _running
    _running = True
    s = get_settings()
    interval = s.drift_interval

    logger.info("Drift detection started (interval=%ds)", interval)

    while _running:
        try:
            await check_drift_once()
        except Exception as e:
            logger.error("Drift check error: %s", e)

        await asyncio.sleep(interval)


def stop_drift() -> None:
    """Signal the drift loop to stop."""
    global _running
    _running = False
