"""Render Jinja2 templates → nginx config file content."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiofiles
from jinja2 import Environment, FileSystemLoader

from nanio_orchestrator.config import get_settings
from nanio_orchestrator.db import get_db_ctx

_env: Optional[Environment] = None


def _get_jinja_env() -> Environment:
    global _env
    if _env is None:
        tpl_dir = Path(__file__).parent / "templates"
        _env = Environment(
            loader=FileSystemLoader(str(tpl_dir)),
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )
    return _env


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_str(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ── Upstream block rendering ─────────────────────────────────────────────────


def render_upstream(pool: Dict[str, Any], members: List[Dict[str, Any]]) -> str:
    """Render an upstream config block for a pool."""
    env = _get_jinja_env()
    tpl = env.get_template("upstream.conf.j2")
    return tpl.render(pool=pool, members=members, updated=_now_iso())


# ── Vhost block rendering ────────────────────────────────────────────────────


def render_vhost(vhost: Dict[str, Any], routes: List[Dict[str, Any]]) -> str:
    """Render a server block for a vhost with all its routes."""
    env = _get_jinja_env()
    tpl = env.get_template("vhost.conf.j2")
    # Sort routes by path_prefix length (longest first) for correct nginx matching
    sorted_routes = sorted(routes, key=lambda r: len(r["path_prefix"]), reverse=True)
    return tpl.render(vhost=vhost, routes=sorted_routes, updated=_now_iso())


# ── Write config file (atomic: .tmp → rename) ────────────────────────────────


async def write_config_atomic(filepath: str, content: str) -> str:
    """Write content to filepath atomically. Returns sha256 of written content."""
    tmp_path = filepath + ".tmp"
    async with aiofiles.open(tmp_path, "w") as f:
        await f.write(content)
    os.rename(tmp_path, filepath)
    return sha256_str(content)


async def remove_config_file(filepath: str) -> None:
    """Remove a managed config file if it exists."""
    p = Path(filepath)
    if p.exists():
        p.unlink()


# ── Record file state in DB ──────────────────────────────────────────────────


async def record_file_state(db, filepath: str, content: str) -> None:
    """Update or insert the config_files record for a path."""
    h = sha256_str(content)
    now = _now_iso()
    await db.execute(
        """INSERT INTO config_files (path, sha256_disk, sha256_db, content_snapshot, last_synced_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(path) DO UPDATE SET
             sha256_disk = excluded.sha256_disk,
             sha256_db = excluded.sha256_db,
             content_snapshot = excluded.content_snapshot,
             last_synced_at = excluded.last_synced_at""",
        (filepath, h, h, content, now),
    )


# ── Full generation from DB ──────────────────────────────────────────────────


async def generate_pool_config(pool_id: int) -> tuple:
    """Generate upstream config for a pool.
    Returns (filepath, content) where content is None when the pool has no members
    (caller must remove the file rather than write an empty upstream block).
    """
    s = get_settings()
    async with get_db_ctx() as db:
        row = await db.execute_fetchall("SELECT * FROM pools WHERE id = ?", (pool_id,))
        if not row:
            raise ValueError(f"Pool {pool_id} not found")
        pool = dict(row[0])
        members_rows = await db.execute_fetchall(
            "SELECT * FROM pool_members WHERE pool_id = ? ORDER BY id", (pool_id,)
        )
        members = [dict(m) for m in members_rows]

    filepath = str(s.pools_dir / f"{pool['name']}.conf")
    if not members:
        return filepath, None  # empty pool — caller should remove the file

    content = render_upstream(pool, members)
    return filepath, content


async def generate_vhost_config(vhost_id: int) -> tuple:
    """Generate server block config for a vhost. Returns (filepath, content)."""
    s = get_settings()
    async with get_db_ctx() as db:
        row = await db.execute_fetchall("SELECT * FROM vhosts WHERE id = ?", (vhost_id,))
        if not row:
            raise ValueError(f"Vhost {vhost_id} not found")
        vhost = dict(row[0])
        routes_rows = await db.execute_fetchall(
            """SELECT r.*, p.name as pool_name
               FROM routes r JOIN pools p ON r.pool_id = p.id
               WHERE r.vhost_id = ? ORDER BY length(r.path_prefix) DESC""",
            (vhost_id,),
        )
        routes = [dict(r) for r in routes_rows]
        # Ensure key_prefix is present (may be NULL in older rows)
        for route in routes:
            route.setdefault("key_prefix", None)

        # Attach live-migration write-routing info: if a migration for this
        # vhost is in write_routing or verifying phase, client writes must go
        # directly to the destination pool while reads still come from source.
        mig_rows = await db.execute_fetchall(
            """SELECT m.bucket, p.name AS dst_pool_name
               FROM migrations m
               JOIN pools p ON m.dst_pool_id = p.id
               WHERE m.vhost_id = ? AND m.phase IN ('write_routing', 'verifying')""",
            (vhost_id,),
        )
        migration_map = {row["bucket"]: row["dst_pool_name"] for row in mig_rows}
        for route in routes:
            bucket = route["path_prefix"].strip("/").split("/")[0]
            route["migration_dst_pool_name"] = migration_map.get(bucket)

    content = render_vhost(vhost, routes)
    filepath = str(s.vhosts_dir / f"{vhost['server_name']}.conf")
    return filepath, content


async def generate_all_configs() -> List[tuple]:
    """Generate all pool and vhost configs. Returns list of (filepath, content)."""
    results = []
    async with get_db_ctx() as db:
        pools = await db.execute_fetchall("SELECT id FROM pools")
        vhosts = await db.execute_fetchall("SELECT id FROM vhosts")

    for p in pools:
        results.append(await generate_pool_config(p["id"]))
    for v in vhosts:
        results.append(await generate_vhost_config(v["id"]))
    return results


# ── Node config generation ────────────────────────────────────────────────────


def render_node_config(
    node_type: str,
    member_address: str,
    pool_name: str,
    pool_type: str,
    data_dir: str = "/data",
    nanio_port: int = 9000,
    nanio_host: str = "0.0.0.0",
    nanio_region: str = "us-east-1",
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> List[Dict[str, str]]:
    """Render node config files for an upstream member. Returns list of {path, content}."""
    env = _get_jinja_env()
    ctx = dict(
        member_address=member_address,
        pool_name=pool_name,
        pool_type=pool_type,
        data_dir=data_dir,
        nanio_port=nanio_port,
        nanio_host=nanio_host,
        nanio_region=nanio_region,
        access_key=access_key,
        secret_key=secret_key,
    )
    files: List[Dict[str, str]] = []

    if node_type in ("nanio-only", "nginx-nanio"):
        # nanio options file
        tpl = env.get_template("nanio_options.toml.j2")
        files.append({"path": "/etc/nanio/options.toml", "content": tpl.render(**ctx)})
        # nanio systemd unit
        tpl = env.get_template("nanio_service.j2")
        files.append({"path": "/etc/systemd/system/nanio.service", "content": tpl.render(**ctx)})

    if node_type == "nginx-only":
        tpl = env.get_template("node_nginx_http.conf.j2")
        files.append({"path": "/etc/nginx/conf.d/nanio-serve.conf", "content": tpl.render(**ctx)})

    if node_type == "nginx-nanio":
        tpl = env.get_template("node_nginx_nanio.conf.j2")
        files.append({"path": "/etc/nginx/conf.d/nanio-proxy.conf", "content": tpl.render(**ctx)})

    return files


def node_config_instructions(node_type: str) -> str:
    """Return human-readable instructions for applying node config."""
    if node_type == "nanio-only":
        return (
            "Copy each file to the node and run:\n"
            "  systemctl daemon-reload && systemctl enable --now nanio"
        )
    elif node_type == "nginx-only":
        return (
            "Copy the nginx config to the node and run:\n"
            "  nginx -t && systemctl reload nginx"
        )
    else:  # nginx-nanio
        return (
            "Copy all files to the node and run:\n"
            "  systemctl daemon-reload && systemctl enable --now nanio\n"
            "  nginx -t && systemctl reload nginx"
        )
