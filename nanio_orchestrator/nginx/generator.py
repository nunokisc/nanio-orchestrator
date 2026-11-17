"""Render Jinja2 templates → nginx config file content."""

from __future__ import annotations

import hashlib
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

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

SUBFOLDER_MARKER = ".nanio-shard"


def path_segments(path_prefix: str) -> List[str]:
    return [seg for seg in path_prefix.strip("/").split("/") if seg]


def is_subfolder_prefix(path_prefix: str) -> bool:
    """True for `/bucket/folder/…/`: a route for part of a bucket, not for a whole bucket."""
    return len(path_segments(path_prefix)) >= 2


def _prefix_query_regex(folder_segments: List[str]) -> str:
    """Regex for the raw `prefix=` query value of a listing inside this folder.

    Clients may send the value percent-encoded or not, so `/` and `@` match both forms.
    """
    slash = "(/|%2[Ff])"
    parts = ["".join("(@|%40)" if ch == "@" else re.escape(ch) for ch in seg) for seg in folder_segments]
    return "^" + slash.join(parts) + slash


def _subfolder_listings(vhost: Dict[str, Any], routes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per bucket that has sub-folder routes: how bucket-level requests must be handled.

    `location /bucket/folder/` already sends object requests to the folder's pool. Requests that
    address the bucket itself need help:
      - a listing carries the folder in `?prefix=`, so a `map` picks the pool from it;
      - a multi-object delete carries its keys in the body, so it is mirrored to every pool
        that holds a folder of the bucket (each pool deletes the keys it has).
    """
    enabled = [r for r in routes if r.get("enabled", True)]
    by_path = {r["path_prefix"]: r for r in enabled}
    by_bucket: Dict[str, List[Dict[str, Any]]] = {}
    for route in enabled:
        if is_subfolder_prefix(route["path_prefix"]) and route.get("pool_type", "nanio") == "nanio":
            by_bucket.setdefault(path_segments(route["path_prefix"])[0], []).append(route)

    listings = []
    for bucket in sorted(by_bucket):
        bucket_route = by_path.get(f"/{bucket}/")
        owner = bucket_route or by_path.get("/")
        if owner is None:
            continue
        folders = sorted(by_bucket[bucket], key=lambda r: len(r["path_prefix"]), reverse=True)
        owner_pool = owner.get("migration_dst_pool_name") or owner["pool_name"]
        # A folder being migrated lives on two pools until it switches: its listing is answered
        # by the destination (authoritative), a multi-delete must reach both.
        mirrors, seen = [], {owner["pool_name"], owner_pool}
        for route in folders:
            for suffix, pool_name in (("", route["pool_name"]), ("d", route.get("migration_dst_pool_name"))):
                if pool_name and pool_name not in seen:
                    seen.add(pool_name)
                    mirrors.append({"id": f"{route['id']}{suffix}", "pool_name": pool_name})
        digest = hashlib.sha256(f"{vhost.get('server_name')}/{bucket}".encode()).hexdigest()[:12]
        listings.append(
            {
                "bucket": bucket,
                "var": f"$nanio_list_{digest}",
                "default_pool": owner_pool,
                "entries": [
                    {
                        "regex": _prefix_query_regex(path_segments(r["path_prefix"])[1:]),
                        "pool_name": r.get("migration_dst_pool_name") or r["pool_name"],
                    }
                    for r in folders
                ],
                "mirrors": mirrors,
                "owner": owner,
                "has_bucket_route": bucket_route is not None,
            }
        )
    return listings


def render_vhost(vhost: Dict[str, Any], routes: List[Dict[str, Any]]) -> str:
    """Render a server block for a vhost with all its routes."""
    import json as _json

    env = _get_jinja_env()
    tpl = env.get_template("vhost.conf.j2")
    # Sort routes by path_prefix length (longest first) for correct nginx matching
    sorted_routes = sorted(routes, key=lambda r: len(r["path_prefix"]), reverse=True)

    # A bucket route is `location /bucket/`. S3 bucket-level requests address `/bucket` with no
    # trailing slash; nginx answers those with a 301 unless an exact-match location exists.
    for route in sorted_routes:
        prefix = route["path_prefix"]
        is_bucket = len(prefix) > 2 and prefix.startswith("/") and prefix.endswith("/") and "/" not in prefix[1:-1]
        route["bucket_location"] = prefix[:-1] if is_bucket else None

    listings = _subfolder_listings(vhost, sorted_routes)
    listing_by_bucket = {entry["bucket"]: entry for entry in listings}
    for route in sorted_routes:
        route["listing"] = listing_by_bucket.get(route["bucket_location"][1:]) if route["bucket_location"] else None

    # Parse extra_blocks and split by zone
    raw_blocks = vhost.get("extra_blocks_json") or vhost.get("extra_blocks") or []
    if isinstance(raw_blocks, str):
        try:
            raw_blocks = _json.loads(raw_blocks)
        except Exception:
            raw_blocks = []

    extra_blocks_top = [b for b in raw_blocks if b.get("zone") == "top"]
    extra_blocks_ssl = [b for b in raw_blocks if b.get("zone") == "ssl"]
    extra_blocks_proxy = [b for b in raw_blocks if b.get("zone") == "proxy"]
    extra_blocks_end = [b for b in raw_blocks if b.get("zone") == "end"]

    # IP access control rules
    ip_rule_mode = vhost.get("ip_rule_mode") or None
    ip_rule_ips_raw = vhost.get("ip_rule_ips_json") or None
    ip_rule_ips = _json.loads(ip_rule_ips_raw) if ip_rule_ips_raw else None

    return tpl.render(
        vhost=vhost,
        routes=sorted_routes,
        listings=listings,
        updated=_now_iso(),
        extra_blocks_top=extra_blocks_top,
        extra_blocks_ssl=extra_blocks_ssl,
        extra_blocks_proxy=extra_blocks_proxy,
        extra_blocks_end=extra_blocks_end,
        ip_rule_mode=ip_rule_mode,
        ip_rule_ips=ip_rule_ips,
    )


# ── Remove config file ───────────────────────────────────────────────────────


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


@asynccontextmanager
async def _reading(db):
    """Read through the caller's connection when given, so that changes it has not committed
    yet are part of what is generated — that is how a change is validated before it is saved."""
    if db is not None:
        yield db
    else:
        async with get_db_ctx() as own:
            yield own


async def generate_pool_config(pool_id: int, db=None) -> tuple:
    """Generate upstream config for a pool.
    Returns (filepath, content) where content is None when the pool has no members
    (caller must remove the file rather than write an empty upstream block).
    """
    s = get_settings()
    async with _reading(db) as db:
        row = await db.execute_fetchall("SELECT * FROM pools WHERE id = ?", (pool_id,))
        if not row:
            raise ValueError(f"Pool {pool_id} not found")
        pool = dict(row[0])
        members_rows = await db.execute_fetchall("SELECT * FROM pool_members WHERE pool_id = ? ORDER BY id", (pool_id,))
        members = [dict(m) for m in members_rows]

    filepath = str(s.pools_dir / f"{pool['name']}.conf")
    if not members:
        return filepath, None  # empty pool — caller should remove the file

    content = render_upstream(pool, members)
    return filepath, content


async def generate_vhost_config(vhost_id: int, db=None) -> tuple:
    """Generate server block config for a vhost. Returns (filepath, content)."""
    s = get_settings()
    async with _reading(db) as db:
        row = await db.execute_fetchall("SELECT * FROM vhosts WHERE id = ?", (vhost_id,))
        if not row:
            raise ValueError(f"Vhost {vhost_id} not found")
        vhost = dict(row[0])
        routes_rows = await db.execute_fetchall(
            """SELECT r.*, p.name as pool_name, p.type as pool_type
               FROM routes r JOIN pools p ON r.pool_id = p.id
               WHERE r.vhost_id = ? ORDER BY length(r.path_prefix) DESC""",
            (vhost_id,),
        )
        routes = [dict(r) for r in routes_rows]

        # Attach live-migration split-routing info. The split is driven by the persisted
        # migrations.split_routing flag, NOT by the phase: once clients have written to the
        # destination pool the split must survive an error/cancel/restart, otherwise those
        # objects would silently disappear behind a source-only route.
        mig_rows = await db.execute_fetchall(
            """SELECT m.bucket, m.route_id, p.name AS dst_pool_name
               FROM migrations m
               JOIN pools p ON m.dst_pool_id = p.id
               WHERE m.vhost_id = ? AND m.split_routing = 1""",
            (vhost_id,),
        )
        # Build two lookup maps: precise (by route_id) and legacy (by bucket name)
        migration_by_route_id: Dict[int, str] = {}
        migration_by_bucket: Dict[str, str] = {}
        for row in mig_rows:
            if row["route_id"]:
                migration_by_route_id[row["route_id"]] = row["dst_pool_name"]
            else:
                migration_by_bucket[row["bucket"]] = row["dst_pool_name"]
        for route in routes:
            if route["id"] in migration_by_route_id:
                route["migration_dst_pool_name"] = migration_by_route_id[route["id"]]
            else:
                # Legacy: match by exact bucket name (route path_prefix = /{bucket}/)
                prefix_stripped = route["path_prefix"].strip("/")
                route["migration_dst_pool_name"] = migration_by_bucket.get(prefix_stripped)

        # An http vhost serving the files of a nanio pool follows that pool's migrations: while
        # bucket X is split between nanio pools A → B, a route /X/ on the http pool backed by A
        # is split towards the http pool backed by B. Derived here so that it holds for every
        # regeneration (vhost edit, rebuild, restart), not only when the engine applies it.
        linked_rows = await db.execute_fetchall(
            """SELECT m.bucket, m.folder, sp.id AS http_src_pool_id, dp.name AS http_dst_pool_name
               FROM migrations m
               JOIN pools sp ON sp.source_nanio_pool_id = m.src_pool_id AND sp.type = 'http'
               JOIN pools dp ON dp.source_nanio_pool_id = m.dst_pool_id AND dp.type = 'http'
               WHERE m.split_routing = 1
               ORDER BY dp.id"""
        )
        linked = {}
        for row in linked_rows:
            path = f"/{row['bucket']}/{row['folder']}"
            linked.setdefault((path, row["http_src_pool_id"]), row["http_dst_pool_name"])
        for route in routes:
            if not route["migration_dst_pool_name"]:
                route["migration_dst_pool_name"] = linked.get((route["path_prefix"], route["pool_id"]))

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


def _address_port(address: str) -> int:
    """Port of a `host:port` member address (80 when there is none)."""
    _, sep, port = address.rpartition(":")
    return int(port) if sep and port.isdigit() else 80


def render_node_config(
    node_type: str,
    member_address: str,
    pool_name: str,
    pool_type: str,
    data_dir: str = "/data",
    nanio_port: Optional[int] = None,
    nanio_host: str = "0.0.0.0",
    nanio_region: str = "us-east-1",
    access_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    nanio_bin: str = "/opt/nanio/bin/nanio",
) -> Dict[str, Any]:
    """Render everything a storage node needs to serve as `member_address`.

    Returns {"files": [{path, content}], "instructions": str, "warnings": [str]}.

    The gateway sends traffic to the member address, so that port is what must listen on the
    node: nanio itself for `nanio-only`, nginx for the other two types (with nanio behind it on
    a loopback port for `nginx-nanio`).
    """
    env = _get_jinja_env()
    member_port = _address_port(member_address)
    warnings: List[str] = []

    if node_type == "nanio-only":
        if nanio_port is None:
            nanio_port = member_port
        elif nanio_port != member_port:
            warnings.append(
                f"nanio would listen on port {nanio_port}, but the gateway sends this member's traffic to "
                f"port {member_port} ({member_address}). Use the same port, or change the member address."
            )
    elif node_type == "nginx-nanio":
        if nanio_port is None:
            nanio_port = 9000 if member_port != 9000 else 9001
        if nanio_port == member_port:
            warnings.append(f"nginx and nanio cannot both listen on port {member_port}. Give nanio a different port.")
        if nanio_host == "0.0.0.0":
            nanio_host = "127.0.0.1"  # only the local nginx talks to it

    has_credentials = bool(access_key and secret_key)
    if node_type != "nginx-only" and not has_credentials:
        warnings.append(
            "No credentials given: nanio refuses to start without them. Fill in the [[users]] block of "
            f"options.toml with the credentials stored for pool '{pool_name}'."
        )

    ctx = dict(
        member_address=member_address,
        pool_name=pool_name,
        pool_type=pool_type,
        data_dir=data_dir,
        listen_port=member_port,
        nanio_port=nanio_port,
        nanio_host=nanio_host,
        nanio_region=nanio_region,
        access_key=access_key,
        secret_key=secret_key,
        nanio_bin=nanio_bin,
    )
    files: List[Dict[str, str]] = []

    if node_type in ("nanio-only", "nginx-nanio"):
        files.append(
            {"path": "/etc/nanio/options.toml", "content": env.get_template("nanio_options.toml.j2").render(**ctx)}
        )
        files.append(
            {"path": "/etc/systemd/system/nanio.service", "content": env.get_template("nanio_service.j2").render(**ctx)}
        )

    if node_type == "nginx-only":
        files.append(
            {
                "path": "/etc/nginx/conf.d/nanio-serve.conf",
                "content": env.get_template("node_nginx_http.conf.j2").render(**ctx),
            }
        )

    if node_type == "nginx-nanio":
        files.append(
            {
                "path": "/etc/nginx/conf.d/nanio-proxy.conf",
                "content": env.get_template("node_nginx_nanio.conf.j2").render(**ctx),
            }
        )

    return {"files": files, "instructions": _node_instructions(node_type, ctx), "warnings": warnings}


def _node_instructions(node_type: str, ctx: Dict[str, Any]) -> str:
    """Step-by-step setup of the node, in the order the commands have to run."""
    nanio_steps = f"""\
1. Install nanio (needs Python 3.11 or newer) and create its user:
     python3 -m venv {os.path.dirname(os.path.dirname(ctx["nanio_bin"]))}
     {os.path.dirname(ctx["nanio_bin"])}/pip install nanio
     useradd --system --no-create-home --shell /usr/sbin/nologin nanio

2. Data directory. Every member of pool '{ctx["pool_name"]}' must see the same files here
   (shared storage); mount it before going on.
     mkdir -p {ctx["data_dir"]}
     chown nanio:nanio {ctx["data_dir"]}

3. Copy the generated files, then protect the one holding the secret key:
     mkdir -p /etc/nanio
     chown root:nanio /etc/nanio/options.toml
     chmod 640 /etc/nanio/options.toml

4. Start it:
     systemctl daemon-reload
     systemctl enable --now nanio
     systemctl status nanio
"""
    nginx_step = """\
{n}. Load the nginx server block:
     nginx -t && systemctl reload nginx
"""
    check = f"""\
{{n}}. Check from the gateway machine that the node answers on the member address:
     curl -i http://{ctx["member_address"]}/
   {{expect}}
"""
    if node_type == "nanio-only":
        return nanio_steps + check.format(n=5, expect="nanio replies 403 (it wants a signed request): the node is up.")
    if node_type == "nginx-nanio":
        return (
            nanio_steps
            + nginx_step.format(n=5)
            + check.format(n=6, expect="nanio replies 403 through nginx (it wants a signed request): the node is up.")
        )
    return f"""\
1. The data directory must hold the same files as the nanio pool this pool serves (a replica
   kept in sync by rsync/lsyncd, or the same shared storage), readable by nginx:
     ls {ctx["data_dir"]}

2. Copy the generated server block, then load it:
     nginx -t && systemctl reload nginx

3. Check from the gateway machine:
     curl -I http://{ctx["member_address"]}/<bucket>/<object>
   Expect 200 for an existing object. Requests are not authenticated: restrict access with the
   vhost IP rules on the gateway if the content is not public.
"""
