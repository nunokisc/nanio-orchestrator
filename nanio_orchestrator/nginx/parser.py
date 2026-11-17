"""Parse managed nginx config files back to data structures.

Only parses files containing '# managed by nanio-orchestrator'.
Handles the subset of nginx config the orchestrator generates.
"""

from __future__ import annotations

import contextlib
import re
from typing import Any, Optional

MANAGED_MARKER = "# managed by nanio-orchestrator"


def is_managed_file(content: str) -> bool:
    """Check if a file content is managed by the orchestrator."""
    return MANAGED_MARKER in content


def parse_metadata_comment(line: str) -> dict[str, str]:
    """Parse a metadata comment like '# pool_id:4 name:pool-2025 type:nanio updated:...'"""
    meta = {}
    # Match key:value pairs (value can contain colons for timestamps)
    for match in re.finditer(r"(\w+):(\S+)", line):
        meta[match.group(1)] = match.group(2)
    return meta


def parse_upstream_block(content: str) -> Optional[dict[str, Any]]:
    """Parse an upstream config block.

    Returns dict with: name, lb_method, keepalive, members[], metadata.
    """
    if not is_managed_file(content):
        return None

    result: dict[str, Any] = {
        "name": None,
        "lb_method": "round_robin",
        "keepalive": 32,
        "members": [],
        "metadata": {},
    }

    # Parse metadata comments
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("# pool_id:") or line.startswith("# vhost_id:"):
            result["metadata"] = parse_metadata_comment(line)

    # Parse upstream block
    upstream_match = re.search(r"upstream\s+(\S+)\s*\{(.*?)\}", content, re.DOTALL)
    if not upstream_match:
        return None

    result["name"] = upstream_match.group(1)
    block = upstream_match.group(2)

    for line in block.splitlines():
        line = line.strip().rstrip(";")
        if not line or line.startswith("#"):
            continue

        if line == "least_conn":
            result["lb_method"] = "least_conn"
        elif line == "ip_hash":
            result["lb_method"] = "ip_hash"
        elif line.startswith("keepalive"):
            parts = line.split()
            if len(parts) >= 2:
                with contextlib.suppress(ValueError):
                    result["keepalive"] = int(parts[1])
        elif line.startswith("server"):
            member = _parse_server_directive(line)
            if member:
                result["members"].append(member)

    return result


def _parse_server_directive(line: str) -> Optional[dict[str, Any]]:
    """Parse a 'server <address> [options]' directive."""
    parts = line.split()
    if len(parts) < 2:
        return None

    address = parts[1].rstrip(";")
    member: dict[str, Any] = {
        "address": address,
        "role": "active",
        "weight": 1,
        "max_fails": 3,
        "fail_timeout_s": 30,
    }

    rest = " ".join(parts[2:])

    if "backup" in rest:
        member["role"] = "replica"
        return member

    weight_m = re.search(r"weight=(\d+)", rest)
    if weight_m:
        member["weight"] = int(weight_m.group(1))

    max_fails_m = re.search(r"max_fails=(\d+)", rest)
    if max_fails_m:
        member["max_fails"] = int(max_fails_m.group(1))

    fail_timeout_m = re.search(r"fail_timeout=(\d+)s?", rest)
    if fail_timeout_m:
        member["fail_timeout_s"] = int(fail_timeout_m.group(1))

    return member


def parse_vhost_block(content: str) -> Optional[dict[str, Any]]:
    """Parse a server (vhost) config block.

    Returns dict with: server_name, listen_port, ssl, ssl_cert_path, ssl_key_path,
                       routes[], metadata.
    """
    if not is_managed_file(content):
        return None

    result: dict[str, Any] = {
        "server_name": None,
        "listen_port": 80,
        "ssl": False,
        "ssl_cert_path": None,
        "ssl_key_path": None,
        "routes": [],
        "metadata": {},
    }

    # Parse metadata
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("# vhost_id:"):
            result["metadata"] = parse_metadata_comment(line)

    # Find server block
    server_match = re.search(r"server\s*\{(.*)\}", content, re.DOTALL)
    if not server_match:
        return None

    block = server_match.group(1)

    # server_name
    sn_match = re.search(r"server_name\s+(\S+);", block)
    if sn_match:
        result["server_name"] = sn_match.group(1)

    # listen
    listen_match = re.search(r"listen\s+(\d+)(.*?);", block)
    if listen_match:
        result["listen_port"] = int(listen_match.group(1))
        listen_rest = listen_match.group(2)
        if "ssl" in listen_rest:
            result["ssl"] = True

    # SSL paths
    cert_match = re.search(r"ssl_certificate\s+(\S+);", block)
    if cert_match:
        result["ssl_cert_path"] = cert_match.group(1)

    key_match = re.search(r"ssl_certificate_key\s+(\S+);", block)
    if key_match:
        result["ssl_key_path"] = key_match.group(1)

    # Parse location blocks
    # First collect route metadata comments
    route_metas: dict[str, dict[str, str]] = {}
    for line in block.splitlines():
        line = line.strip()
        if line.startswith("# route_id:"):
            meta = parse_metadata_comment(line)
            prefix = meta.get("prefix", "")
            route_metas[prefix] = meta

    # Find location blocks
    for loc_match in re.finditer(r"location\s+(\S+)\s*\{(.*?)\}", block, re.DOTALL):
        prefix = loc_match.group(1)
        loc_block = loc_match.group(2)

        # Extract proxy_pass upstream name
        pp_match = re.search(r"proxy_pass\s+http://(\S+);", loc_block)
        # A route is a location the generator wrote, announced by its `# route_id:` comment.
        # Any other location with a proxy_pass (one inside an extra block, for instance) is
        # the operator's own nginx config, not a route.
        if pp_match and prefix in route_metas:
            pool_name = pp_match.group(1)
            meta = route_metas[prefix]
            # Live-migration split: the location proxies to the destination pool, but the
            # route still belongs to the source pool until the migration switches.
            if meta.get("migration_src"):
                pool_name = meta["migration_src"]
            route: dict[str, Any] = {
                "path_prefix": prefix,
                "pool_name": pool_name,
                "metadata": meta,
            }
            result["routes"].append(route)

    return result
