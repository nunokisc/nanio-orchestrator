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
from typing import Any, Dict, List

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


async def rebuild_from_disk(dry_run: bool = False) -> Dict[str, Any]:
    """Reconstruct the entire database from disk state.

    Args:
        dry_run: If True, report what would be imported without writing to DB.

    Returns dict with counts and warnings.
    """
    from nanio_orchestrator.db import get_db_ctx, init_db

    s = get_settings()
    warnings: List[str] = []
    pool_count = 0
    vhost_count = 0
    route_count = 0
    member_count = 0
    migration_count = 0
    credentials_count = 0

    # Build sidecar lookup tables
    pool_sidecars: Dict[str, dict] = {}
    for sc in scan_pool_sidecars():
        name = sc.get("name")
        if name:
            pool_sidecars[name] = sc

    vhost_sidecars: Dict[str, dict] = {}
    for sc in scan_vhost_sidecars():
        sn = sc.get("server_name")
        if sn:
            vhost_sidecars[sn] = sc

    migration_states = scan_migration_states()
    migration_completions = scan_migration_completions()

    if dry_run:
        return await _dry_run_report(
            s, pool_sidecars, vhost_sidecars, migration_states, migration_completions
        )

    # Step 1: (re)create schema
    await init_db()

    async with get_db_ctx() as db:
        # Name → inserted ID mapping
        pool_name_to_id: Dict[str, int] = {}
        # path → content cache (avoid re-reading files in step 5)
        conf_content_cache: Dict[str, str] = {}

        # Step 2: import pools from nginx upstream configs + sidecars
        for conf_path in sorted(s.pools_dir.glob("*.conf")):
            try:
                content = conf_path.read_text(encoding="utf-8")
            except OSError:
                continue
            if not is_managed_file(content):
                continue
            conf_content_cache[str(conf_path)] = content

            parsed = parse_upstream_block(content)
            if not parsed or not parsed["name"]:
                continue

            name = parsed["name"]
            sidecar = pool_sidecars.get(name, {})

            pool_type = sidecar.get("type", "nanio")
            description = sidecar.get("description")

            cursor = await db.execute(
                """INSERT INTO pools (name, description, type, lb_method, keepalive)
                   VALUES (?, ?, ?, ?, ?)""",
                (name, description, pool_type,
                 parsed["lb_method"], parsed["keepalive"]),
            )
            pool_id = cursor.lastrowid
            pool_name_to_id[name] = pool_id
            pool_count += 1

            # Import members
            for m in parsed["members"]:
                await db.execute(
                    """INSERT INTO pool_members
                       (pool_id, address, role, weight, max_fails, fail_timeout_s, enabled)
                       VALUES (?, ?, ?, ?, ?, ?, 1)""",
                    (pool_id, m["address"], m["role"], m["weight"],
                     m["max_fails"], m["fail_timeout_s"]),
                )
                member_count += 1

            # Recover credentials from sidecar
            creds = sidecar.get("credentials")
            if creds and creds.get("access_key_enc") and creds.get("secret_key_enc"):
                await db.execute(
                    """INSERT INTO pool_credentials
                       (pool_id, access_key_enc, secret_key_enc, endpoint_url, region)
                       VALUES (?, ?, ?, ?, ?)""",
                    (pool_id, creds["access_key_enc"], creds["secret_key_enc"],
                     creds.get("endpoint_url"), creds.get("region", "us-east-1")),
                )
                credentials_count += 1

        # Step 3: import vhosts + routes from nginx server configs + sidecars
        for conf_path in sorted(s.vhosts_dir.glob("*.conf")):
            try:
                content = conf_path.read_text(encoding="utf-8")
            except OSError:
                continue
            if not is_managed_file(content):
                continue
            conf_content_cache[str(conf_path)] = content

            parsed = parse_vhost_block(content)
            if not parsed or not parsed["server_name"]:
                continue

            server_name = parsed["server_name"]
            sidecar = vhost_sidecars.get(server_name, {})

            # Resolve default_pool_id
            default_pool_id = None
            sc_default = sidecar.get("default_pool_id")
            sc_default_name = sidecar.get("default_pool_name")
            if sc_default_name and sc_default_name in pool_name_to_id:
                default_pool_id = pool_name_to_id[sc_default_name]
            elif sc_default and isinstance(sc_default, int):
                # Original ID may not match after reimport; try name-based
                pass

            cursor = await db.execute(
                """INSERT INTO vhosts
                   (server_name, listen_port, ssl, ssl_cert_path, ssl_key_path,
                    enabled, default_pool_id, extra_blocks_json,
                    ip_rule_mode, ip_rule_ips_json)
                   VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)""",
                (server_name, parsed["listen_port"], 1 if parsed["ssl"] else 0,
                 parsed["ssl_cert_path"], parsed["ssl_key_path"],
                 default_pool_id,
                 sidecar.get("extra_blocks_json"),
                 sidecar.get("ip_rule_mode"),
                 sidecar.get("ip_rule_ips_json")),
            )
            vhost_id = cursor.lastrowid
            vhost_count += 1

            # Import routes
            for route in parsed["routes"]:
                pool_name = route["pool_name"]
                pool_id = pool_name_to_id.get(pool_name)
                if not pool_id:
                    warnings.append(
                        f"Route {route['path_prefix']} on {server_name}: "
                        f"pool '{pool_name}' not found, skipping"
                    )
                    continue

                key_prefix = route.get("key_prefix")

                await db.execute(
                    """INSERT INTO routes
                       (vhost_id, path_prefix, pool_id, key_prefix, enabled)
                       VALUES (?, ?, ?, ?, 1)""",
                    (vhost_id, route["path_prefix"], pool_id, key_prefix),
                )
                route_count += 1

        # Step 4: import in-progress migrations from state files
        vhost_rows = await db.execute_fetchall("SELECT id, server_name FROM vhosts")
        for state in migration_states:
            src_name = state.get("source_pool_name")
            dst_name = state.get("target_pool_name")
            src_id = pool_name_to_id.get(src_name) if src_name else None
            dst_id = pool_name_to_id.get(dst_name) if dst_name else None

            if not src_id or not dst_id:
                warnings.append(
                    f"Migration {state.get('migration_id')}: "
                    f"pool not found (src={src_name}, dst={dst_name}), skipping"
                )
                continue

            # Find vhost_id by looking up vhosts
            vhost_id_for_migration = None
            if vhost_rows:
                # Try matching by original vhost_id in state
                for vr in vhost_rows:
                    if vr["id"] == state.get("vhost_id"):
                        vhost_id_for_migration = vr["id"]
                        break
                # If only one vhost, use it as fallback
                if not vhost_id_for_migration and len(vhost_rows) == 1:
                    vhost_id_for_migration = vhost_rows[0]["id"]
                elif not vhost_id_for_migration:
                    # Multiple vhosts and no match — skip to avoid assigning to wrong vhost
                    warnings.append(
                        f"Migration {state.get('migration_id')}: "
                        f"vhost_id {state.get('vhost_id')} not found among rebuilt vhosts, "
                        "skipping to avoid assigning to wrong vhost"
                    )
                    continue

            if not vhost_id_for_migration:
                warnings.append(
                    f"Migration {state.get('migration_id')}: no vhosts found, skipping"
                )
                continue

            phase = state.get("status", "pending")
            # pending/copying/verifying → pending (safe to restart from beginning).
            # write_routing/switching stay as-is: recover_interrupted_migrations will
            # mark them as error and require operator review before resuming.
            if phase in ("pending", "copying", "verifying"):
                phase = "pending"

            await db.execute(
                """INSERT INTO migrations
                   (vhost_id, bucket, src_pool_id, dst_pool_id, phase, mode, route_id,
                    objects_total, objects_done, bytes_total, bytes_done)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (vhost_id_for_migration, state.get("bucket", ""),
                 src_id, dst_id, phase,
                 state.get("mode", "copy"), state.get("route_id"),
                 state.get("total_objects", 0), state.get("copied_objects", 0),
                 state.get("bytes_total", 0), state.get("bytes_transferred", 0)),
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

            vhost_id_for_completion = None
            if vhost_rows:
                for vr in vhost_rows:
                    if vr["id"] == state.get("vhost_id"):
                        vhost_id_for_completion = vr["id"]
                        break
                if not vhost_id_for_completion and len(vhost_rows) == 1:
                    vhost_id_for_completion = vhost_rows[0]["id"]

            if not vhost_id_for_completion:
                warnings.append(
                    f"Completed migration {state.get('migration_id')}: "
                    "vhost not found, skipping"
                )
                continue

            await db.execute(
                """INSERT INTO migrations
                   (vhost_id, bucket, src_pool_id, dst_pool_id, phase, mode, route_id,
                    orphaned_source_pool_id, orphaned_source_prefix, orphaned_at)
                   VALUES (?, ?, ?, ?, 'done', ?, ?, ?, ?, ?)""",
                (vhost_id_for_completion, state.get("bucket", ""),
                 src_id, dst_id,
                 state.get("mode", "copy"), state.get("route_id"),
                 state.get("orphaned_source_pool_id"),
                 state.get("orphaned_source_prefix"),
                 state.get("orphaned_at")),
            )
            completion_count += 1
            migration_count += 1

        # Step 5: rebuild config_files sha256 records
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for path_str, content in conf_content_cache.items():
            h = sha256_str(content)
            await db.execute(
                """INSERT INTO config_files
                   (path, sha256_disk, sha256_db, content_snapshot, last_synced_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET
                     sha256_disk = excluded.sha256_disk,
                     sha256_db = excluded.sha256_db,
                     content_snapshot = excluded.content_snapshot,
                     last_synced_at = excluded.last_synced_at""",
                (path_str, h, h, content, now),
            )

        await db.commit()

    # Step 6: rebuild bucket_sync from live nanio-default (best-effort)
    await _rebuild_bucket_sync(pool_name_to_id, warnings)

    return {
        "pools_imported": pool_count,
        "members_imported": member_count,
        "vhosts_imported": vhost_count,
        "routes_imported": route_count,
        "migrations_imported": migration_count,
        "completed_migrations_imported": completion_count,
        "credentials_recovered": credentials_count,
        "warnings": warnings,
    }


async def _rebuild_bucket_sync(
    pool_name_to_id: Dict[str, int],
    warnings: List[str],
) -> None:
    """Rebuild bucket_sync table from live ListBuckets calls (best-effort)."""
    from nanio_orchestrator.db import get_db_ctx

    try:
        from nanio_orchestrator.s3client import list_buckets
    except ImportError:
        warnings.append("s3client not available — bucket_sync not rebuilt")
        return

    async with get_db_ctx() as db:
        vhosts = await db.execute_fetchall(
            "SELECT id, default_pool_id FROM vhosts WHERE default_pool_id IS NOT NULL"
        )
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
                s = get_settings()
                raw_buckets = await list_buckets(address, s.s3_access_key, s.s3_secret_key)
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
    pool_sidecars: Dict[str, dict],
    vhost_sidecars: Dict[str, dict],
    migration_states: List[dict],
    migration_completions: List[dict],
) -> Dict[str, Any]:
    """Generate a dry-run report without writing anything."""
    pools = []
    vhosts = []
    warnings: List[str] = []

    for conf_path in sorted(s.pools_dir.glob("*.conf")):
        try:
            content = conf_path.read_text(encoding="utf-8")
        except OSError:
            continue
        if not is_managed_file(content):
            continue
        parsed = parse_upstream_block(content)
        if parsed and parsed["name"]:
            sidecar = pool_sidecars.get(parsed["name"], {})
            pools.append({
                "name": parsed["name"],
                "members": len(parsed["members"]),
                "type": sidecar.get("type", "nanio"),
                "has_credentials": bool(sidecar.get("credentials")),
                "has_sidecar": bool(sidecar),
            })

    for conf_path in sorted(s.vhosts_dir.glob("*.conf")):
        try:
            content = conf_path.read_text(encoding="utf-8")
        except OSError:
            continue
        if not is_managed_file(content):
            continue
        parsed = parse_vhost_block(content)
        if parsed and parsed["server_name"]:
            sidecar = vhost_sidecars.get(parsed["server_name"], {})
            vhosts.append({
                "server_name": parsed["server_name"],
                "routes": len(parsed["routes"]),
                "has_default_pool": bool(sidecar.get("default_pool_id") or sidecar.get("default_pool_name")),
                "has_extra_blocks": bool(sidecar.get("extra_blocks_json")),
                "has_ip_rules": bool(sidecar.get("ip_rule_mode")),
                "has_sidecar": bool(sidecar),
            })

    return {
        "dry_run": True,
        "pools": pools,
        "vhosts": vhosts,
        "migrations": len(migration_states),
        "completed_migrations": len(migration_completions),
        "warnings": warnings,
    }
