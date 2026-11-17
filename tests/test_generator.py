"""Generating nginx files and node configs from what the database holds."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import nanio_orchestrator.config as cfg_mod
import nanio_orchestrator.db as db_mod
import pytest
import pytest_asyncio
from nanio_orchestrator.db import get_db_ctx, init_db
from nanio_orchestrator.nginx.generator import (
    generate_all_configs,
    generate_pool_config,
    generate_vhost_config,
    is_subfolder_prefix,
    path_segments,
    record_file_state,
    remove_config_file,
    render_node_config,
    render_upstream,
    render_vhost,
    sha256_str,
)


def _route(route_id: int, prefix: str, pool: str, dst=None, **kw) -> dict:
    return {
        "id": route_id,
        "path_prefix": prefix,
        "pool_name": pool,
        "pool_type": "nanio",
        "enabled": 1,
        "migration_dst_pool_name": dst,
        **kw,
    }


def _vhost(**kw) -> dict:
    return {"id": 9, "server_name": "s3.example.com", "listen_port": 80, "ssl": 0, **kw}


class TestHelpers:
    def test_sha256_of_the_utf8_text(self):
        assert sha256_str("") == hashlib.sha256(b"").hexdigest()
        assert sha256_str("café") == hashlib.sha256("café".encode()).hexdigest()

    @pytest.mark.parametrize(
        ("prefix", "segments", "subfolder"),
        [
            ("/", [], False),
            ("/photos/", ["photos"], False),
            ("/photos", ["photos"], False),
            ("/photos/2025/", ["photos", "2025"], True),
            ("/photos//2025/q1/", ["photos", "2025", "q1"], True),
        ],
    )
    def test_path_segments_tell_bucket_routes_from_folder_routes(self, prefix, segments, subfolder):
        assert path_segments(prefix) == segments
        assert is_subfolder_prefix(prefix) is subfolder


class TestUpstream:
    def test_round_robin_is_nginx_default_and_writes_no_method_line(self):
        pool = {"id": 1, "name": "hot", "type": "nanio", "lb_method": "round_robin", "keepalive": 8}
        member = {"address": "10.0.0.1:9000", "role": "active", "weight": 2, "max_fails": 1, "fail_timeout_s": 5}
        content = render_upstream(pool, [{**member, "enabled": 1}])
        body = content.split("upstream hot {\n")[1]
        assert body == "    server 10.0.0.1:9000 weight=2 max_fails=1 fail_timeout=5s;\n    keepalive 8;\n}\n"
        assert content.startswith("# managed by nanio-orchestrator\n# pool_id:1 name:hot type:nanio updated:")


BLOCKS = [
    {"zone": "top", "content": "add_header X-Zone top;"},
    {"zone": "ssl", "content": "ssl_protocols TLSv1.3;"},
    {"zone": "proxy", "content": "proxy_read_timeout 600s;"},
    {"zone": "end", "content": "location = /health {\n    return 200;\n}"},
]
TLS = {"listen_port": 443, "ssl": 1, "ssl_cert_path": "/c.pem", "ssl_key_path": "/c.key"}


class TestVhostExtraBlocksAndIpRules:
    @pytest.mark.parametrize("key", ["extra_blocks", "extra_blocks_json"])
    def test_each_block_lands_in_its_zone(self, key):
        blocks = json.dumps(BLOCKS) if key == "extra_blocks_json" else BLOCKS
        content = render_vhost(_vhost(**TLS, **{key: blocks}), [_route(1, "/photos/", "hot")])

        order = [
            "server_name s3.example.com;",
            "add_header X-Zone top;",
            "ssl_certificate_key /c.key;",
            "ssl_protocols TLSv1.3;",
            "proxy_buffering         off;",
            "proxy_read_timeout 600s;",
            "location /photos/ {",
            "    location = /health {\n        return 200;\n    }\n}",
        ]
        positions = [content.index(text) for text in order]
        assert positions == sorted(positions)

    def test_unreadable_stored_blocks_are_left_out_instead_of_breaking_the_vhost(self):
        content = render_vhost(_vhost(extra_blocks_json="{not json"), [_route(1, "/photos/", "hot")])
        assert "location /photos/ {" in content
        without_blocks = render_vhost(_vhost(), [_route(1, "/photos/", "hot")])
        assert content.splitlines()[2:] == without_blocks.splitlines()[2:]  # all but the timestamp line

    def test_blocks_without_a_known_zone_are_not_rendered(self):
        content = render_vhost(_vhost(extra_blocks=[{"zone": "http", "content": "evil_directive on;"}]), [])
        assert "evil_directive" not in content

    def test_allowlist_denies_everyone_else(self):
        vhost = _vhost(ip_rule_mode="allow", ip_rule_ips_json='["10.0.0.0/8", "2001:db8::1"]')
        content = render_vhost(vhost, [])
        assert "    allow 10.0.0.0/8;\n    allow 2001:db8::1;\n    deny all;\n" in content

    def test_denylist_blocks_only_the_listed_addresses(self):
        content = render_vhost(_vhost(ip_rule_mode="deny", ip_rule_ips_json='["192.0.2.7"]'), [])
        assert "    deny 192.0.2.7;\n" in content
        assert "deny all" not in content and "allow " not in content

    @pytest.mark.parametrize(
        "rules",
        [
            {"ip_rule_mode": "allow", "ip_rule_ips_json": None},
            {"ip_rule_mode": "allow", "ip_rule_ips_json": "[]"},
            {"ip_rule_mode": None, "ip_rule_ips_json": '["10.0.0.1"]'},
            {"ip_rule_mode": "", "ip_rule_ips_json": ""},
        ],
    )
    def test_incomplete_rules_restrict_nothing(self, rules):
        content = render_vhost(_vhost(**rules), [])
        assert "allow " not in content and "deny " not in content

    def test_tls_without_a_certificate_writes_no_certificate_lines(self):
        content = render_vhost(_vhost(listen_port=443, ssl=1, ssl_cert_path=None), [])
        assert "listen 443 ssl;" in content
        assert "ssl_certificate" not in content


class TestVhostRoutes:
    def test_longest_prefix_comes_first(self):
        routes = [_route(1, "/", "default"), _route(2, "/photos/", "hot"), _route(3, "/photos-archive/", "cold")]
        content = render_vhost(_vhost(), routes)
        positions = [content.index(f"location {prefix} {{") for prefix in ("/photos-archive/", "/photos/", "/")]
        assert positions == sorted(positions)

    def test_bucket_route_also_answers_the_bucket_itself(self):
        content = render_vhost(_vhost(), [_route(2, "/photos/", "hot")])
        for location in ("location /photos/ {", "location = /photos {", "location = /photos/ {"):
            assert location in content
        assert content.count("proxy_pass         http://hot;") == 3

    def test_default_and_folder_routes_get_no_bucket_locations(self):
        content = render_vhost(_vhost(), [_route(1, "/", "default"), _route(2, "/photos", "hot")])
        assert "location = " not in content

    def test_folder_of_a_bucket_nobody_serves_gets_only_its_own_location(self):
        """No bucket route and no default route: there is no pool to answer bucket-level requests."""
        content = render_vhost(_vhost(), [_route(3, "/photos/2025/", "archive")])
        assert "location /photos/2025/ {" in content
        assert "map $arg_prefix" not in content
        assert "location = /photos" not in content
        assert "_multidelete" not in content

    def test_disabled_folder_route_changes_nothing(self):
        routes = [_route(1, "/", "default"), _route(3, "/photos/2025/", "archive", enabled=0)]
        content = render_vhost(_vhost(), routes)
        assert "map $arg_prefix" not in content
        assert "/photos/2025/" not in content

    def test_folder_on_the_same_pool_as_its_bucket_needs_no_mirror(self):
        routes = [_route(2, "/photos/", "hot"), _route(3, "/photos/2025/", "hot")]
        content = render_vhost(_vhost(), routes)
        assert "map $arg_prefix" in content  # the listing is still chosen from ?prefix=
        assert "__nanio_shard_" not in content

    def test_split_bucket_route_reads_fall_back_to_the_source(self):
        content = render_vhost(_vhost(), [_route(7, "/photos/", "src", dst="dst")])
        assert "# route_id:7 prefix:/photos/ migration_src:src migration_dst:dst" in content
        assert "error_page 404 = @nanio_mig_7;" in content
        fallback = content.split("location @nanio_mig_7 {")[1].split("}")[1]
        assert "proxy_pass         http://src;" in fallback
        delete_mirror = content.split("location = /__nanio_mig_7_delete {")[1]
        assert "proxy_pass         http://src$request_uri;" in delete_mirror.split("    }")[1]
        assert "location = /__nanio_mig_7_multidelete {" in content

    def test_split_folder_route_has_no_multi_delete_mirror(self):
        """A multi-object delete addresses the bucket, never a folder."""
        content = render_vhost(_vhost(), [_route(7, "/photos/2025/", "src", dst="dst")])
        assert "location = /__nanio_mig_7_delete {" in content
        assert "__nanio_mig_7_multidelete" not in content
        assert "mirror_request_body off;" in content


# ── Generation from the database ──────────────────────────────────────────────


@pytest_asyncio.fixture
async def conn(tmp_dirs, monkeypatch):
    monkeypatch.setenv("NANIO_ORCHESTRATOR_DB_PATH", tmp_dirs["db_path"])
    monkeypatch.setenv("NANIO_ORCHESTRATOR_NGINX_CONFIG_DIR", tmp_dirs["nginx_dir"])
    cfg_mod.settings = None
    db_mod.set_db_path(tmp_dirs["db_path"])
    await init_db()
    async with get_db_ctx() as db:
        await db.execute("INSERT INTO pools (id, name) VALUES (1, 'src'), (2, 'dst'), (3, 'empty')")
        await db.execute(
            "INSERT INTO pool_members (pool_id, address) VALUES (1, '10.0.0.1:9000'), (2, '10.0.0.2:9000')"
        )
        await db.execute("INSERT INTO vhosts (id, server_name, listen_port, ssl) VALUES (1, 's3.example.com', 80, 0)")
        await db.execute("INSERT INTO routes (id, vhost_id, path_prefix, pool_id) VALUES (5, 1, '/photos/', 1)")
        await db.commit()
        yield db
    cfg_mod.settings = None
    db_mod._db_path = None


class TestGenerateFromDatabase:
    async def test_pool_file_is_named_after_the_pool(self, conn, tmp_dirs):
        filepath, content = await generate_pool_config(1)
        assert filepath == str(Path(tmp_dirs["nginx_dir"]) / "pools" / "src.conf")
        assert "upstream src {" in content and "server 10.0.0.1:9000 weight=1" in content

    async def test_pool_without_members_has_no_content(self, conn, tmp_dirs):
        filepath, content = await generate_pool_config(3)
        assert filepath == str(Path(tmp_dirs["nginx_dir"]) / "pools" / "empty.conf")
        assert content is None

    async def test_unknown_pool_or_vhost_is_an_error(self, conn):
        with pytest.raises(ValueError, match="Pool 99 not found"):
            await generate_pool_config(99)
        with pytest.raises(ValueError, match="Vhost 99 not found"):
            await generate_vhost_config(99)

    async def test_vhost_file_is_named_after_the_server(self, conn, tmp_dirs):
        filepath, content = await generate_vhost_config(1)
        assert filepath == str(Path(tmp_dirs["nginx_dir"]) / "vhosts" / "s3.example.com.conf")
        assert "# route_id:5 prefix:/photos/\n" in content
        assert "proxy_pass         http://src;" in content

    async def test_uncommitted_changes_are_generated_only_through_the_connection_that_made_them(self, conn):
        """That is how a change is validated by nginx before it is saved."""
        await conn.execute("INSERT INTO routes (id, vhost_id, path_prefix, pool_id) VALUES (6, 1, '/videos/', 2)")
        await conn.execute("INSERT INTO pool_members (pool_id, address) VALUES (3, '10.0.0.3:9000')")

        assert "location /videos/ {" in (await generate_vhost_config(1, conn))[1]
        assert "10.0.0.3:9000" in (await generate_pool_config(3, conn))[1]
        await conn.rollback()
        assert "location /videos/ {" not in (await generate_vhost_config(1))[1]
        assert (await generate_pool_config(3))[1] is None

    async def test_split_is_driven_by_the_flag_not_by_the_phase(self, conn):
        await conn.execute(
            """INSERT INTO migrations (vhost_id, bucket, src_pool_id, dst_pool_id, phase, route_id, split_routing)
               VALUES (1, 'photos', 1, 2, 'error', 5, 1)"""
        )
        await conn.commit()
        content = (await generate_vhost_config(1))[1]
        assert "# route_id:5 prefix:/photos/ migration_src:src migration_dst:dst" in content

        await conn.execute("UPDATE migrations SET split_routing = 0, phase = 'verifying'")
        await conn.commit()
        assert "migration_dst" not in (await generate_vhost_config(1))[1]

    async def test_migration_without_a_route_link_is_matched_by_bucket_name(self, conn):
        await conn.execute("INSERT INTO routes (id, vhost_id, path_prefix, pool_id) VALUES (6, 1, '/videos/', 1)")
        await conn.execute(
            """INSERT INTO migrations (vhost_id, bucket, src_pool_id, dst_pool_id, phase, route_id, split_routing)
               VALUES (1, 'videos', 1, 2, 'write_routing', NULL, 1)"""
        )
        await conn.commit()
        content = (await generate_vhost_config(1))[1]
        assert "# route_id:6 prefix:/videos/ migration_src:src migration_dst:dst" in content
        assert "# route_id:5 prefix:/photos/\n" in content

    async def test_split_of_another_vhost_does_not_leak(self, conn):
        await conn.execute(
            "INSERT INTO vhosts (id, server_name, listen_port, ssl) VALUES (2, 'other.example.com', 80, 0)"
        )
        await conn.execute("INSERT INTO routes (id, vhost_id, path_prefix, pool_id) VALUES (8, 2, '/photos/', 1)")
        await conn.execute(
            """INSERT INTO migrations (vhost_id, bucket, src_pool_id, dst_pool_id, phase, route_id, split_routing)
               VALUES (2, 'photos', 1, 2, 'write_routing', 8, 1)"""
        )
        await conn.commit()
        assert "migration_dst" not in (await generate_vhost_config(1))[1]
        assert "migration_dst:dst" in (await generate_vhost_config(2))[1]

    async def test_http_vhost_follows_the_split_of_the_nanio_pools_it_serves(self, conn):
        await conn.execute(
            """INSERT INTO pools (id, name, type, source_nanio_pool_id)
               VALUES (11, 'web-src', 'http', 1), (12, 'web-dst', 'http', 2), (13, 'web-dst-b', 'http', 2)"""
        )
        await conn.execute(
            "INSERT INTO vhosts (id, server_name, listen_port, ssl) VALUES (2, 'cdn.example.com', 80, 0)"
        )
        await conn.execute(
            """INSERT INTO routes (id, vhost_id, path_prefix, pool_id)
               VALUES (20, 2, '/photos/', 11), (21, 2, '/videos/', 11), (22, 2, '/photos/2025/', 11)"""
        )
        await conn.execute(
            """INSERT INTO migrations (vhost_id, bucket, folder, src_pool_id, dst_pool_id, phase, route_id, split_routing)
               VALUES (1, 'photos', '', 1, 2, 'write_routing', 5, 1)"""
        )
        await conn.commit()

        content = (await generate_vhost_config(2))[1]
        # The first http pool linked to the destination wins; other buckets and folders are untouched
        assert "# route_id:20 prefix:/photos/ migration_src:web-src migration_dst:web-dst [" in content
        assert "# route_id:21 prefix:/videos/\n" in content
        assert "# route_id:22 prefix:/photos/2025/\n" in content

    async def test_all_configs_lists_every_pool_then_every_vhost(self, conn, tmp_dirs):
        configs = await generate_all_configs()
        pools, vhosts = configs[:3], configs[3:]
        assert sorted((Path(fp).name, ct is None) for fp, ct in pools) == [
            ("dst.conf", False),
            ("empty.conf", True),
            ("src.conf", False),
        ]
        assert [Path(fp).name for fp, _ in vhosts] == ["s3.example.com.conf"]
        assert "server_name s3.example.com;" in vhosts[0][1]

    async def test_recorded_file_state_is_updated_in_place(self, conn):
        await record_file_state(conn, "/etc/nginx/nanio/pools/src.conf", "first")
        await record_file_state(conn, "/etc/nginx/nanio/pools/src.conf", "second")
        await conn.commit()
        rows = await conn.execute_fetchall("SELECT path, sha256_disk, sha256_db, content_snapshot FROM config_files")
        assert [dict(r) for r in rows] == [
            {
                "path": "/etc/nginx/nanio/pools/src.conf",
                "sha256_disk": sha256_str("second"),
                "sha256_db": sha256_str("second"),
                "content_snapshot": "second",
            }
        ]


class TestRemoveConfigFile:
    async def test_removes_the_file(self, tmp_path):
        conf = tmp_path / "a.conf"
        conf.write_text("x")
        await remove_config_file(str(conf))
        assert not conf.exists()

    async def test_missing_file_is_not_an_error(self, tmp_path):
        await remove_config_file(str(tmp_path / "never-there.conf"))
        assert list(tmp_path.iterdir()) == []


# ── Storage node configs ──────────────────────────────────────────────────────


def _node(node_type: str, address: str = "10.0.0.1:9000", **kw) -> dict:
    kw.setdefault("access_key", "AKIA")
    kw.setdefault("secret_key", "secret")
    pool_type = "http" if node_type == "nginx-only" else "nanio"
    return render_node_config(node_type, address, "hot", pool_type, **kw)


def _file(result: dict, path: str) -> str:
    return next(f["content"] for f in result["files"] if f["path"] == path)


class TestNodeConfig:
    def test_nanio_only_listens_on_the_member_port(self):
        result = _node("nanio-only")
        assert [f["path"] for f in result["files"]] == ["/etc/nanio/options.toml", "/etc/systemd/system/nanio.service"]
        assert result["warnings"] == []
        assert "9000" in _file(result, "/etc/nanio/options.toml")
        assert "nanio replies 403 (it wants a signed request)" in result["instructions"]
        assert "nginx -t" not in result["instructions"]

    def test_nanio_only_on_another_port_than_the_member_is_reported(self):
        result = _node("nanio-only", nanio_port=9100)
        assert len(result["warnings"]) == 1
        assert "port 9100" in result["warnings"][0] and "port 9000 (10.0.0.1:9000)" in result["warnings"][0]
        assert _node("nanio-only", nanio_port=9000)["warnings"] == []

    def test_member_address_without_a_port_means_port_80(self):
        result = _node("nginx-only", address="files.internal")
        assert "listen 80" in _file(result, "/etc/nginx/conf.d/nanio-serve.conf")
        assert "curl -I http://files.internal/<bucket>/<object>" in result["instructions"]

    def test_nginx_only_needs_no_credentials_and_no_nanio(self):
        result = _node("nginx-only", address="10.0.0.5:8080", access_key=None, secret_key=None)
        assert [f["path"] for f in result["files"]] == ["/etc/nginx/conf.d/nanio-serve.conf"]
        assert result["warnings"] == []
        assert "listen 8080" in result["files"][0]["content"]
        assert "systemctl enable --now nanio" not in result["instructions"]

    @pytest.mark.parametrize(("address", "nanio_port"), [("10.0.0.1:80", 9000), ("10.0.0.1:9000", 9001)])
    def test_nginx_in_front_puts_nanio_on_a_free_loopback_port(self, address, nanio_port):
        result = _node("nginx-nanio", address=address)
        assert [f["path"] for f in result["files"]] == [
            "/etc/nanio/options.toml",
            "/etc/systemd/system/nanio.service",
            "/etc/nginx/conf.d/nanio-proxy.conf",
        ]
        assert result["warnings"] == []
        proxy = _file(result, "/etc/nginx/conf.d/nanio-proxy.conf")
        assert f"127.0.0.1:{nanio_port}" in proxy
        assert "nanio replies 403 through nginx" in result["instructions"]

    def test_nginx_and_nanio_on_the_same_port_is_reported(self):
        result = _node("nginx-nanio", nanio_port=9000)
        assert result["warnings"] == ["nginx and nanio cannot both listen on port 9000. Give nanio a different port."]

    def test_an_explicit_nanio_host_is_kept_behind_nginx(self):
        options = _file(_node("nginx-nanio", nanio_host="10.0.0.1"), "/etc/nanio/options.toml")
        assert "10.0.0.1" in options and "127.0.0.1" not in options
        assert "127.0.0.1" in _file(_node("nginx-nanio"), "/etc/nanio/options.toml")
        assert "0.0.0.0" in _file(_node("nanio-only"), "/etc/nanio/options.toml")

    @pytest.mark.parametrize("keys", [{"access_key": None}, {"secret_key": None}, {"access_key": "", "secret_key": ""}])
    @pytest.mark.parametrize("node_type", ["nanio-only", "nginx-nanio"])
    def test_nanio_without_credentials_is_reported(self, node_type, keys):
        result = _node(node_type, **keys)
        assert len(result["warnings"]) == 1
        assert "No credentials given" in result["warnings"][0] and "pool 'hot'" in result["warnings"][0]

    def test_instructions_use_the_given_binary_and_data_directory(self):
        result = _node("nanio-only", data_dir="/srv/objects", nanio_bin="/usr/local/nanio/bin/nanio")
        assert "python3 -m venv /usr/local/nanio\n" in result["instructions"]
        assert "/usr/local/nanio/bin/pip install nanio" in result["instructions"]
        assert "chown nanio:nanio /srv/objects" in result["instructions"]
        assert "/usr/local/nanio/bin/nanio" in _file(result, "/etc/systemd/system/nanio.service")
