"""Helpers for the API tests: look at what a request left behind (files, rows, audit entries)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from nanio_orchestrator.db import get_db_ctx


def managed_files(tmp_dirs) -> dict:
    """Every managed file (configs and sidecars) with its content."""
    root = Path(tmp_dirs["nginx_dir"])
    return {str(p.relative_to(root)): p.read_text() for p in root.rglob("*") if p.is_file()}


def conf(tmp_dirs, kind: str, name: str) -> Path:
    """Path of `pools/<name>.conf` or `vhosts/<name>.conf`."""
    return Path(tmp_dirs["nginx_dir"]) / kind / f"{name}.conf"


def sidecar(tmp_dirs, kind: str, name: str) -> Path:
    return Path(tmp_dirs["nginx_dir"]) / kind / f"{name}.meta.json"


async def sql(query: str, params=()) -> list:
    """Run one statement straight on the database and return its rows as dicts."""
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall(query, params)
        await db.commit()
    return [dict(r) for r in rows]


async def audit(client, action: str) -> list:
    return [e for e in (await client.get("/api/audit?per_page=200")).json() if e["action"] == action]


async def routes(client, vhost) -> dict:
    """{path_prefix: pool_name} of a vhost."""
    return {r["path_prefix"]: r["pool_name"] for r in (await client.get(f"/api/vhosts/{vhost['id']}/routes")).json()}


def refuse(mock_nginx, output: str = "nginx: [emerg] something is wrong") -> None:
    mock_nginx["test_config"].return_value = MagicMock(ok=False, output=output)


async def break_inserts(table: str) -> None:
    """Make every INSERT into `table` fail the way a failing disk would (not a UNIQUE violation)."""
    await sql(
        f"CREATE TRIGGER broken_{table} BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT, 'disk I/O error'); END"
    )
