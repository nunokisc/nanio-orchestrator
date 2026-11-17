"""Rebuild the database from nginx config files + sidecar files.

Reconstruct the entire database when the SQLite file is lost or corrupted:
1. nginx config files → pools, members, vhosts, routes
2. .meta.json sidecars → pool type/description/credentials, vhost default_pool_id
3. .state.json files → in-progress migrations
4. live ListBuckets → bucket_sync state
5. Recompute config_files sha256
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from nanio_orchestrator.config import get_settings
from nanio_orchestrator.nginx.generator import sha256_str
from nanio_orchestrator.nginx.parser import (
    is_managed_file,
    parse_upstream_block,
    parse_vhost_block,
)
from nanio_orchestrator.sidecar import (
    scan_migration_completions,
    scan_migration_states,
    scan_pool_sidecars,
    scan_vhost_sidecars,
)

logger = logging.getLogger(__name__)


_RECORD_FILE_SQL = """INSERT INTO config_files (path, sha256_disk, sha256_db, content_snapshot, last_synced_at)
   VALUES (?, ?, ?, ?, ?)
   ON CONFLICT(path) DO UPDATE SET
     sha256_disk = excluded.sha256_disk, sha256_db = excluded.sha256_db,
     content_snapshot = excluded.content_snapshot, last_synced_at = excluded.last_synced_at"""


def _read_managed(conf_path) -> Optional[str]:
    try:
        content = conf_path.read_text(encoding="utf-8")
    except OSError:
        return None
    return content if is_managed_file(content) else None


async def import_configs(db, warnings: list[str]) -> dict[str, Any]:
    """Upsert the pools, members, credentials, vhosts and routes found on disk. Never deletes.

    The one disk → DB importer: a full import on an empty database (``rebuild_from_disk``),
    an additive one on a live database (``POST /api/config/sync``).

    What the ``.conf`` files define (members, ports, TLS, routes) is always taken from disk.
    What only the sidecars know (pool type, description, credentials, default pool, extra
    blocks, IP rules) fills new rows; on existing rows the database stays authoritative,
    except pool type/description and the default pool, which follow a sidecar that is present.

    The caller commits. Returns counts plus ``pool_name_to_id``.
    """
    s = get_settings()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pool_sidecars = {sc["name"]: sc for sc in scan_pool_sidecars() if sc.get("name")}
    vhost_sidecars = {sc["server_name"]: sc for sc in scan_vhost_sidecars() if sc.get("server_name")}
    counts = {
        "pools_new": 0,
        "pools_updated": 0,
        "members_new": 0,
        "credentials_new": 0,
        "vhosts_new": 0,
        "vhosts_updated": 0,
        "routes": 0,
        "files": 0,
    }

    pool_name_to_id: dict[str, int] = {
        r["name"]: r["id"] for r in await db.execute_fetchall("SELECT id, name FROM pools")
    }
    new_http_pools: list[str] = []

    # ── Pools, members, credentials ──────────────────────────────────────────
    for conf_path in sorted(s.pools_dir.glob("*.conf")):
        content = _read_managed(conf_path)
        parsed = parse_upstream_block(content) if content else None
        if not parsed or not parsed["name"]:
            continue

        name = parsed["name"]
        sidecar = pool_sidecars.get(name)
        pool_id = pool_name_to_id.get(name)
        if pool_id is None:
            pool_type = (sidecar or {}).get("type", "nanio")
            cursor = await db.execute(
                "INSERT INTO pools (name, description, type, lb_method, keepalive) VALUES (?, ?, ?, ?, ?)",
                (name, (sidecar or {}).get("description"), pool_type, parsed["lb_method"], parsed["keepalive"]),
            )
            pool_id = pool_name_to_id[name] = cursor.lastrowid
            counts["pools_new"] += 1
            if pool_type == "http":
                new_http_pools.append(name)
        else:
            await db.execute(
                "UPDATE pools SET lb_method=?, keepalive=?, updated_at=datetime('now') WHERE id=?",
                (parsed["lb_method"], parsed["keepalive"], pool_id),
            )
            if sidecar:  # without a sidecar there is nothing better than what the DB already has
                await db.execute(
                    "UPDATE pools SET description=?, type=? WHERE id=?",
                    (sidecar.get("description"), sidecar.get("type", "nanio"), pool_id),
                )
            counts["pools_updated"] += 1

        # The upstream file lists the enabled members; the sidecar also knows each member's
        # role (an http `primary` is written like any other server) and the disabled ones.
        recorded = {m["address"]: m for m in (sidecar or {}).get("members", [])}
        in_file = {m["address"]: {**m, "enabled": True} for m in parsed["members"]}
        for address, m in {**recorded, **in_file}.items():
            exists = await db.execute_fetchall(
                "SELECT id FROM pool_members WHERE pool_id=? AND address=?", (pool_id, address)
            )
            if not exists:
                role = recorded.get(address, m)["role"]
                await db.execute(
                    """INSERT INTO pool_members
                       (pool_id, address, role, weight, max_fails, fail_timeout_s, enabled)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        pool_id,
                        address,
                        role,
                        m["weight"],
                        m["max_fails"],
                        m["fail_timeout_s"],
                        1 if m["enabled"] else 0,
                    ),
                )
                counts["members_new"] += 1

        creds = (sidecar or {}).get("credentials")
        if creds and creds.get("access_key_enc") and creds.get("secret_key_enc"):
            has_creds = await db.execute_fetchall("SELECT id FROM pool_credentials WHERE pool_id=?", (pool_id,))
            if not has_creds:
                await db.execute(
                    """INSERT INTO pool_credentials
                       (pool_id, access_key_enc, secret_key_enc, endpoint_url, region)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        pool_id,
                        creds["access_key_enc"],
                        creds["secret_key_enc"],
                        creds.get("endpoint_url"),
                        creds.get("region", "us-east-1"),
                    ),
                )
                counts["credentials_new"] += 1

        h = sha256_str(content)
        await db.execute(_RECORD_FILE_SQL, (str(conf_path), h, h, content, now))
        counts["files"] += 1

    # An http pool's sidecar names its backing nanio pool by the id it had when written.
    # Ids change on a rebuild, so resolve it through the sidecar that carried that id.
    for name in new_http_pools:
        old_id = pool_sidecars[name].get("source_nanio_pool_id")
        if old_id is None:
            continue
        source_name = next(
            (sc["name"] for sc in pool_sidecars.values() if sc.get("pool_id") == old_id and sc.get("type") == "nanio"),
            None,
        )
        if source_name in pool_name_to_id:
            await db.execute(
                "UPDATE pools SET source_nanio_pool_id=? WHERE id=?",
                (pool_name_to_id[source_name], pool_name_to_id[name]),
            )
        else:
            warnings.append(
                f"Pool '{name}': could not resolve source_nanio_pool_id {old_id} "
                "(referenced nanio pool not found in sidecars)"
            )

    # ── Vhosts and routes ────────────────────────────────────────────────────
    for conf_path in sorted(s.vhosts_dir.glob("*.conf")):
        content = _read_managed(conf_path)
        parsed = parse_vhost_block(content) if content else None
        if not parsed or not parsed["server_name"]:
            continue

        server_name = parsed["server_name"]
        sidecar = vhost_sidecars.get(server_name, {})
        default_pool_id = pool_name_to_id.get(sidecar.get("default_pool_name") or "")
        from_conf = (parsed["listen_port"], 1 if parsed["ssl"] else 0, parsed["ssl_cert_path"], parsed["ssl_key_path"])

        existing = await db.execute_fetchall("SELECT id FROM vhosts WHERE server_name=?", (server_name,))
        if existing:
            vhost_id = existing[0]["id"]
            await db.execute(
                """UPDATE vhosts SET listen_port=?, ssl=?, ssl_cert_path=?, ssl_key_path=?,
                   default_pool_id=COALESCE(?, default_pool_id), updated_at=datetime('now')
                   WHERE id=?""",
                (*from_conf, default_pool_id, vhost_id),
            )
            counts["vhosts_updated"] += 1
        else:
            cursor = await db.execute(
                """INSERT INTO vhosts
                   (server_name, listen_port, ssl, ssl_cert_path, ssl_key_path, enabled,
                    default_pool_id, extra_blocks_json, ip_rule_mode, ip_rule_ips_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    server_name,
                    *from_conf,
                    0 if sidecar.get("enabled") is False else 1,
                    default_pool_id,
                    sidecar.get("extra_blocks_json"),
                    sidecar.get("ip_rule_mode"),
                    sidecar.get("ip_rule_ips_json"),
                ),
            )
            vhost_id = cursor.lastrowid
            counts["vhosts_new"] += 1

        for route in parsed["routes"]:
            pool_id = pool_name_to_id.get(route["pool_name"])
            if not pool_id:
                warnings.append(
                    f"Route {route['path_prefix']} on {server_name}: pool '{route['pool_name']}' not found, skipping"
                )
                continue
            await db.execute(
                """INSERT INTO routes (vhost_id, path_prefix, pool_id, enabled)
                   VALUES (?, ?, ?, 1)
                   ON CONFLICT(vhost_id, path_prefix) DO UPDATE SET
                     pool_id=excluded.pool_id, updated_at=datetime('now')""",
                (vhost_id, route["path_prefix"], pool_id),
            )
            counts["routes"] += 1

        # Disabled routes are not in the server block at all; the sidecar remembers them
        for route in sidecar.get("disabled_routes", []):
            pool_id = pool_name_to_id.get(route["pool_name"])
            known = await db.execute_fetchall(
                "SELECT id FROM routes WHERE vhost_id=? AND path_prefix=?", (vhost_id, route["path_prefix"])
            )
            if pool_id and not known:
                await db.execute(
                    "INSERT INTO routes (vhost_id, path_prefix, pool_id, enabled) VALUES (?, ?, ?, 0)",
                    (vhost_id, route["path_prefix"], pool_id),
                )
                counts["routes"] += 1

        h = sha256_str(content)
        await db.execute(_RECORD_FILE_SQL, (str(conf_path), h, h, content, now))
        counts["files"] += 1

    return {**counts, "pool_name_to_id": pool_name_to_id}


async def _route_id_for(db, vhost_id: int, state: dict) -> Optional[int]:
    """The route a migration acts on, found by its path.

    Route ids are reassigned by a rebuild, so the id stored in the state file is meaningless
    afterwards; the path (`/bucket/` or `/bucket/folder/`) is what identifies the route.
    """
    path = f"/{state.get('bucket', '')}/{state.get('folder', '')}"
    rows = await db.execute_fetchall("SELECT id FROM routes WHERE vhost_id = ? AND path_prefix = ?", (vhost_id, path))
    return rows[0]["id"] if rows else None


def _vhost_id_for(state: dict, vhost_ids: dict[str, int], old_vhost_names: dict[int, str]) -> Optional[int]:
    """The rebuilt vhost a migration belongs to, or None when that cannot be told.

    Vhost ids are reassigned by a rebuild, like route ids. The vhost sidecars remember which
    vhost had the id stored in the state file. Without a sidecar the id is taken as it is,
    and a lone vhost adopts whatever is left.
    """
    old_id = state.get("vhost_id")
    if old_id in old_vhost_names:
        vhost_id = vhost_ids.get(old_vhost_names[old_id])
    elif old_id in vhost_ids.values():
        vhost_id = old_id
    else:
        vhost_id = None
    if vhost_id is None and len(vhost_ids) == 1:
        vhost_id = next(iter(vhost_ids.values()))
    return vhost_id


async def rebuild_from_disk(dry_run: bool = False) -> dict[str, Any]:
    """Reconstruct the entire database from disk state.

    Args:
        dry_run: If True, report what would be imported without writing to DB.

    Returns dict with counts and warnings.
    """
    from nanio_orchestrator.db import get_db_ctx, init_db

    s = get_settings()
    warnings: list[str] = []
    migration_count = 0

    migration_states = scan_migration_states()
    migration_completions = scan_migration_completions()

    if dry_run:
        pool_sidecars = {sc["name"]: sc for sc in scan_pool_sidecars() if sc.get("name")}
        vhost_sidecars = {sc["server_name"]: sc for sc in scan_vhost_sidecars() if sc.get("server_name")}
        return await _dry_run_report(s, pool_sidecars, vhost_sidecars, migration_states, migration_completions)

    # Step 1: (re)create schema
    await init_db()

    async with get_db_ctx() as db:
        # Steps 2, 3 and 5: pools, members, credentials, vhosts, routes, config_files
        imported = await import_configs(db, warnings)
        pool_name_to_id: dict[str, int] = imported["pool_name_to_id"]

        # Step 4: import in-progress migrations from state files
        vhost_ids = {r["server_name"]: r["id"] for r in await db.execute_fetchall("SELECT id, server_name FROM vhosts")}
        old_vhost_names = {
            sc["vhost_id"]: sc["server_name"]
            for sc in scan_vhost_sidecars()
            if sc.get("server_name") and sc.get("vhost_id") is not None
        }
        for state in migration_states:
            src_name = state.get("source_pool_name")
            dst_name = state.get("target_pool_name")
            src_id = pool_name_to_id.get(src_name) if src_name else None
            dst_id = pool_name_to_id.get(dst_name) if dst_name else None

            if not src_id or not dst_id:
                warnings.append(
                    f"Migration {state.get('migration_id')}: pool not found (src={src_name}, dst={dst_name}), skipping"
                )
                continue

            vhost_id_for_migration = _vhost_id_for(state, vhost_ids, old_vhost_names)
            if not vhost_id_for_migration and vhost_ids:
                # Multiple vhosts and no match — skip to avoid assigning to wrong vhost
                warnings.append(
                    f"Migration {state.get('migration_id')}: "
                    f"vhost_id {state.get('vhost_id')} not found among rebuilt vhosts, "
                    "skipping to avoid assigning to wrong vhost"
                )
                continue
            if not vhost_id_for_migration:
                warnings.append(f"Migration {state.get('migration_id')}: no vhosts found, skipping")
                continue

            phase = state.get("status", "pending")
            # pending/copying/verifying → pending (safe to restart from beginning).
            # write_routing/switching stay as-is: recover_interrupted_migrations will
            # mark them as error and require operator review before resuming.
            if phase in ("pending", "copying", "verifying"):
                phase = "pending"

            await db.execute(
                """INSERT INTO migrations
                   (vhost_id, bucket, src_pool_id, dst_pool_id, phase, route_id,
                    objects_total, objects_done, bytes_total, bytes_done, split_routing, folder)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    vhost_id_for_migration,
                    state.get("bucket", ""),
                    src_id,
                    dst_id,
                    phase,
                    await _route_id_for(db, vhost_id_for_migration, state),
                    state.get("total_objects", 0),
                    state.get("copied_objects", 0),
                    state.get("bytes_total", 0),
                    state.get("bytes_transferred", 0),
                    # Older state files have no split_routing key: fall back to nginx_state
                    1 if state.get("split_routing", state.get("nginx_state") == "split") else 0,
                    state.get("folder", ""),
                ),
            )
            migration_count += 1

        # Step 4b: import completed migrations from .done.json files
        # These carry orphaned_source_* info that is not derivable from nginx configs.
        completion_count = 0
        for state in migration_completions:
            src_name = state.get("source_pool_name")
            dst_name = state.get("target_pool_name")
            src_id = pool_name_to_id.get(src_name) if src_name else None
            dst_id = pool_name_to_id.get(dst_name) if dst_name else None

            if not src_id or not dst_id:
                warnings.append(
                    f"Completed migration {state.get('migration_id')}: "
                    f"pool not found (src={src_name}, dst={dst_name}), skipping"
                )
                continue

            vhost_id_for_completion = _vhost_id_for(state, vhost_ids, old_vhost_names)
            if not vhost_id_for_completion:
                warnings.append(f"Completed migration {state.get('migration_id')}: vhost not found, skipping")
                continue

            await db.execute(
                """INSERT INTO migrations
                   (vhost_id, bucket, src_pool_id, dst_pool_id, phase, route_id,
                    orphaned_source_pool_id, orphaned_source_prefix, orphaned_at, folder)
                   VALUES (?, ?, ?, ?, 'done', ?, ?, ?, ?, ?)""",
                (
                    vhost_id_for_completion,
                    state.get("bucket", ""),
                    src_id,
                    dst_id,
                    await _route_id_for(db, vhost_id_for_completion, state),
                    # The orphaned data is on the source pool; the id in the file is its old one
                    src_id if state.get("orphaned_source_pool_id") is not None else None,
                    state.get("orphaned_source_prefix"),
                    state.get("orphaned_at"),
                    state.get("folder", ""),
                ),
            )
            completion_count += 1
            migration_count += 1

        await db.commit()

    # Step 6: rebuild bucket_sync from live nanio-default (best-effort)
    await _rebuild_bucket_sync(pool_name_to_id, warnings)

    return {
        "pools_imported": imported["pools_new"] + imported["pools_updated"],
        "members_imported": imported["members_new"],
        "vhosts_imported": imported["vhosts_new"] + imported["vhosts_updated"],
        "routes_imported": imported["routes"],
        "migrations_imported": migration_count,
        "completed_migrations_imported": completion_count,
        "credentials_recovered": imported["credentials_new"],
        "warnings": warnings,
    }


async def _rebuild_bucket_sync(
    pool_name_to_id: dict[str, int],
    warnings: list[str],
) -> None:
    """Rebuild bucket_sync table from live ListBuckets calls (best-effort)."""
    from nanio_orchestrator.db import get_db_ctx
    from nanio_orchestrator.s3client import list_buckets

    async with get_db_ctx() as db:
        vhosts = await db.execute_fetchall("SELECT id, default_pool_id FROM vhosts WHERE default_pool_id IS NOT NULL")
        for vhost in vhosts:
            vhost_id = vhost["id"]
            pool_id = vhost["default_pool_id"]
            try:
                # Get a member address for this pool
                members = await db.execute_fetchall(
                    "SELECT address FROM pool_members WHERE pool_id = ? AND enabled = 1 LIMIT 1",
                    (pool_id,),
                )
                if not members:
                    continue

                address = members[0]["address"]
                from nanio_orchestrator.credentials import get_pool_s3_params

                access_key, secret_key, _ = await get_pool_s3_params(pool_id)
                raw_buckets = await list_buckets(address, access_key, secret_key)
                # list_buckets returns [{"name": ..., "created": ...}]
                bucket_names = [b["name"] for b in raw_buckets if b.get("name")]

                # Get already-routed buckets
                routes = await db.execute_fetchall(
                    "SELECT path_prefix, pool_id FROM routes WHERE vhost_id = ?",
                    (vhost_id,),
                )
                routed_prefixes = set()
                for r in routes:
                    # path_prefix is like /bucket-name/ → extract bucket name
                    p = r["path_prefix"].strip("/")
                    if p:
                        routed_prefixes.add(p)

                for bucket_name in bucket_names:
                    status = "routed" if bucket_name in routed_prefixes else "unrouted"
                    await db.execute(
                        """INSERT INTO bucket_sync (vhost_id, bucket, status)
                           VALUES (?, ?, ?)
                           ON CONFLICT(vhost_id, bucket) DO UPDATE SET status = excluded.status""",
                        (vhost_id, bucket_name, status),
                    )
            except Exception as e:
                warnings.append(f"bucket_sync rebuild for vhost {vhost_id}: {e}")

        await db.commit()


async def _dry_run_report(
    s,
    pool_sidecars: dict[str, dict],
    vhost_sidecars: dict[str, dict],
    migration_states: list[dict],
    migration_completions: list[dict],
) -> dict[str, Any]:
    """Generate a dry-run report without writing anything."""
    pools = []
    vhosts = []
    warnings: list[str] = []

    for conf_path in sorted(s.pools_dir.glob("*.conf")):
        content = _read_managed(conf_path)
        parsed = parse_upstream_block(content) if content else None
        if parsed and parsed["name"]:
            sidecar = pool_sidecars.get(parsed["name"], {})
            pools.append(
                {
                    "name": parsed["name"],
                    "members": len(parsed["members"]),
                    "type": sidecar.get("type", "nanio"),
                    "has_credentials": bool(sidecar.get("credentials")),
                    "has_sidecar": bool(sidecar),
                    "source_nanio_pool_id": sidecar.get("source_nanio_pool_id"),
                }
            )

    for conf_path in sorted(s.vhosts_dir.glob("*.conf")):
        content = _read_managed(conf_path)
        parsed = parse_vhost_block(content) if content else None
        if parsed and parsed["server_name"]:
            sidecar = vhost_sidecars.get(parsed["server_name"], {})
            vhosts.append(
                {
                    "server_name": parsed["server_name"],
                    "routes": len(parsed["routes"]),
                    "has_default_pool": bool(sidecar.get("default_pool_id") or sidecar.get("default_pool_name")),
                    "has_extra_blocks": bool(sidecar.get("extra_blocks_json")),
                    "has_ip_rules": bool(sidecar.get("ip_rule_mode")),
                    "has_sidecar": bool(sidecar),
                }
            )

    return {
        "dry_run": True,
        "pools": pools,
        "vhosts": vhosts,
        "migrations": len(migration_states),
        "completed_migrations": len(migration_completions),
        "warnings": warnings,
    }
