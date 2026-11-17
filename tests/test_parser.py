"""Reading managed nginx files back: what the generator writes must parse to what went in."""

from __future__ import annotations

import pytest
from nanio_orchestrator.nginx.generator import render_upstream, render_vhost
from nanio_orchestrator.nginx.parser import (
    MANAGED_MARKER,
    is_managed_file,
    parse_metadata_comment,
    parse_upstream_block,
    parse_vhost_block,
)


def _member(address: str, **kw) -> dict:
    return {"address": address, "role": "active", "weight": 1, "max_fails": 3, "fail_timeout_s": 30, "enabled": 1, **kw}


def _pool(**kw) -> dict:
    return {"id": 4, "name": "pool-2025", "type": "nanio", "lb_method": "least_conn", "keepalive": 32, **kw}


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


def _routes(parsed: dict) -> dict:
    return {r["path_prefix"]: r["pool_name"] for r in parsed["routes"]}


class TestManagedMarker:
    def test_only_files_carrying_the_marker_are_managed(self):
        assert is_managed_file(f"{MANAGED_MARKER}\nupstream a {{}}\n")
        assert not is_managed_file("upstream a {\n    server 10.0.0.1:80;\n}\n")
        assert not is_managed_file("")

    def test_unmanaged_files_are_never_parsed(self):
        assert parse_upstream_block("upstream manual {\n    server 10.0.0.1:80;\n}\n") is None
        assert parse_vhost_block("server {\n    listen 80;\n    server_name manual.example.com;\n}\n") is None


class TestMetadataComment:
    def test_pairs_are_split_on_the_first_colon_so_timestamps_stay_whole(self):
        meta = parse_metadata_comment("# pool_id:4 name:pool-2025 type:nanio updated:2026-04-16T10:00:00Z")
        assert meta == {"pool_id": "4", "name": "pool-2025", "type": "nanio", "updated": "2026-04-16T10:00:00Z"}

    def test_words_without_a_colon_are_ignored(self):
        assert parse_metadata_comment("# route_id:7 prefix:/photos/ [LIVE MIGRATION → dst]") == {
            "route_id": "7",
            "prefix": "/photos/",
        }
        assert parse_metadata_comment("# nothing here") == {}


class TestUpstreamRoundTrip:
    @pytest.mark.parametrize("lb_method", ["round_robin", "least_conn", "ip_hash"])
    def test_load_balancing_method(self, lb_method):
        parsed = parse_upstream_block(render_upstream(_pool(lb_method=lb_method), [_member("10.0.0.1:9000")]))
        assert parsed["lb_method"] == lb_method

    def test_name_keepalive_and_metadata(self):
        parsed = parse_upstream_block(render_upstream(_pool(keepalive=128), [_member("10.0.0.1:9000")]))
        assert (parsed["name"], parsed["keepalive"]) == ("pool-2025", 128)
        assert parsed["metadata"]["pool_id"] == "4"
        assert parsed["metadata"]["name"] == "pool-2025"
        assert parsed["metadata"]["type"] == "nanio"

    def test_members_keep_their_tuning_and_order(self):
        members = [
            _member("10.0.0.1:9000", weight=5, max_fails=0, fail_timeout_s=120),
            _member("node-2.internal:9000"),
            _member("unix:/run/nanio.sock", weight=2),
        ]
        parsed = parse_upstream_block(render_upstream(_pool(), members))
        assert parsed["members"] == [
            {"address": "10.0.0.1:9000", "role": "active", "weight": 5, "max_fails": 0, "fail_timeout_s": 120},
            {"address": "node-2.internal:9000", "role": "active", "weight": 1, "max_fails": 3, "fail_timeout_s": 30},
            {"address": "unix:/run/nanio.sock", "role": "active", "weight": 2, "max_fails": 3, "fail_timeout_s": 30},
        ]

    def test_replica_is_written_as_backup_and_read_back_as_replica(self):
        content = render_upstream(_pool(), [_member("10.0.0.1:9000"), _member("10.0.0.2:9000", role="replica")])
        assert "server 10.0.0.2:9000 backup;" in content
        parsed = parse_upstream_block(content)
        assert [(m["address"], m["role"]) for m in parsed["members"]] == [
            ("10.0.0.1:9000", "active"),
            ("10.0.0.2:9000", "replica"),
        ]

    def test_a_host_called_backup_is_not_a_replica(self):
        parsed = parse_upstream_block(render_upstream(_pool(), [_member("backup.internal:9000", weight=3)]))
        assert parsed["members"] == [
            {"address": "backup.internal:9000", "role": "active", "weight": 3, "max_fails": 3, "fail_timeout_s": 30}
        ]

    def test_disabled_members_are_not_in_the_file(self):
        content = render_upstream(_pool(), [_member("10.0.0.1:9000"), _member("10.0.0.2:9000", enabled=0)])
        assert [m["address"] for m in parse_upstream_block(content)["members"]] == ["10.0.0.1:9000"]


class TestUpstreamHandWritten:
    def test_defaults_apply_to_whatever_the_file_leaves_out(self):
        parsed = parse_upstream_block(f"{MANAGED_MARKER}\nupstream bare {{\n    server 10.0.0.1:9000;\n}}\n")
        assert parsed == {
            "name": "bare",
            "lb_method": "round_robin",
            "keepalive": 32,
            "members": [
                {"address": "10.0.0.1:9000", "role": "active", "weight": 1, "max_fails": 3, "fail_timeout_s": 30}
            ],
            "metadata": {},
        }

    def test_comments_blank_lines_and_unknown_directives_are_skipped(self):
        content = f"""{MANAGED_MARKER}
# pool_id:1 name:hot type:nanio updated:2026-01-01T00:00:00Z
upstream hot {{
    # server 10.9.9.9:9000;

    zone hot 64k;
    ip_hash;
    server 10.0.0.1:9000 max_fails=7 fail_timeout=45;
    server 10.0.0.2:9000 weight=4 backup;
    keepalive 16;
}}
"""
        parsed = parse_upstream_block(content)
        assert (parsed["lb_method"], parsed["keepalive"]) == ("ip_hash", 16)
        assert parsed["members"] == [
            {"address": "10.0.0.1:9000", "role": "active", "weight": 1, "max_fails": 7, "fail_timeout_s": 45},
            {"address": "10.0.0.2:9000", "role": "replica", "weight": 1, "max_fails": 3, "fail_timeout_s": 30},
        ]

    @pytest.mark.parametrize("line", ["keepalive;", "keepalive many;", "keepalive_timeout 60s;"])
    def test_unusable_keepalive_keeps_the_default(self, line):
        parsed = parse_upstream_block(f"{MANAGED_MARKER}\nupstream a {{\n    server 10.0.0.1:80;\n    {line}\n}}\n")
        assert parsed["keepalive"] == 32

    def test_server_directive_without_an_address_is_not_a_member(self):
        parsed = parse_upstream_block(f"{MANAGED_MARKER}\nupstream a {{\n    server;\n    server 10.0.0.1:80;\n}}\n")
        assert [m["address"] for m in parsed["members"]] == ["10.0.0.1:80"]

    def test_managed_file_without_an_upstream_block_is_nothing(self):
        assert parse_upstream_block(f"{MANAGED_MARKER}\n# pool_id:1 name:hot\n") is None
        assert parse_upstream_block(f"{MANAGED_MARKER}\nupstream broken\n    server 10.0.0.1:80;\n") is None

    def test_a_vhost_file_is_not_an_upstream(self):
        assert parse_upstream_block(render_vhost(_vhost(), [_route(1, "/photos/", "hot")])) is None


class TestVhostRoundTrip:
    def test_plain_http_vhost(self):
        parsed = parse_vhost_block(render_vhost(_vhost(listen_port=8080), [_route(1, "/photos/", "hot")]))
        assert parsed["server_name"] == "s3.example.com"
        assert (parsed["listen_port"], parsed["ssl"]) == (8080, False)
        assert (parsed["ssl_cert_path"], parsed["ssl_key_path"]) == (None, None)
        assert parsed["metadata"]["vhost_id"] == "9"
        assert parsed["routes"] == [
            {"path_prefix": "/photos/", "pool_name": "hot", "metadata": {"route_id": "1", "prefix": "/photos/"}}
        ]

    def test_tls_vhost(self):
        vhost = _vhost(listen_port=443, ssl=1, ssl_cert_path="/etc/ssl/s3.pem", ssl_key_path="/etc/ssl/s3.key")
        parsed = parse_vhost_block(render_vhost(vhost, []))
        assert (parsed["listen_port"], parsed["ssl"]) == (443, True)
        assert (parsed["ssl_cert_path"], parsed["ssl_key_path"]) == ("/etc/ssl/s3.pem", "/etc/ssl/s3.key")
        assert parsed["routes"] == []

    def test_wildcard_server_name(self):
        parsed = parse_vhost_block(render_vhost(_vhost(server_name="*.s3.example.com"), []))
        assert parsed["server_name"] == "*.s3.example.com"

    def test_bucket_route_yields_one_route_not_one_per_location(self):
        """A bucket route is rendered as three locations (`/b/`, `= /b`, `= /b/`)."""
        content = render_vhost(_vhost(), [_route(1, "/", "default"), _route(2, "/photos/", "hot")])
        assert "location = /photos {" in content and "location = /photos/ {" in content
        assert _routes(parse_vhost_block(content)) == {"/": "default", "/photos/": "hot"}
        assert len(parse_vhost_block(content)["routes"]) == 2

    def test_disabled_routes_are_not_in_the_file(self):
        content = render_vhost(_vhost(), [_route(1, "/photos/", "hot"), _route(2, "/videos/", "hot", enabled=0)])
        assert _routes(parse_vhost_block(content)) == {"/photos/": "hot"}

    def test_split_route_still_belongs_to_the_source_pool(self):
        content = render_vhost(_vhost(), [_route(1, "/", "default"), _route(7, "/photos/", "src", dst="dst")])
        assert "proxy_pass         http://dst;" in content
        parsed = parse_vhost_block(content)
        assert _routes(parsed) == {"/": "default", "/photos/": "src"}
        split = next(r for r in parsed["routes"] if r["path_prefix"] == "/photos/")
        assert split["metadata"] == {
            "route_id": "7",
            "prefix": "/photos/",
            "migration_src": "src",
            "migration_dst": "dst",
        }

    def test_migration_helper_locations_are_not_routes(self):
        """The 404 fallback (`@nanio_mig_N`) and the internal mirrors proxy to the source too."""
        content = render_vhost(_vhost(), [_route(7, "/photos/", "src", dst="dst")])
        for helper in ("location @nanio_mig_7 {", "location = /__nanio_mig_7_delete {", "_multidelete {"):
            assert helper in content
        assert [r["path_prefix"] for r in parse_vhost_block(content)["routes"]] == ["/photos/"]

    def test_sub_folder_routes_and_their_listing_helpers(self):
        routes = [
            _route(1, "/", "default"),
            _route(2, "/photos/", "hot"),
            _route(3, "/photos/2025/", "archive"),
            _route(4, "/logs/2024/q1/", "archive"),
        ]
        content = render_vhost(_vhost(), routes)
        assert "map $arg_prefix $nanio_list_" in content
        assert "location = /__nanio_shard_3_multidelete {" in content
        parsed = parse_vhost_block(content)
        assert _routes(parsed) == {
            "/": "default",
            "/photos/": "hot",
            "/photos/2025/": "archive",
            "/logs/2024/q1/": "archive",
        }
        assert not any(r["pool_name"].startswith("$") for r in parsed["routes"])

    def test_sub_folder_being_migrated_belongs_to_its_source(self):
        routes = [_route(1, "/", "default"), _route(3, "/photos/2025/", "src", dst="dst")]
        assert _routes(parse_vhost_block(render_vhost(_vhost(), routes))) == {"/": "default", "/photos/2025/": "src"}

    def test_ip_rules_and_extra_blocks_do_not_disturb_the_parse(self):
        vhost = _vhost(
            listen_port=443,
            ssl=1,
            ssl_cert_path="/c.pem",
            ssl_key_path="/c.key",
            ip_rule_mode="allow",
            ip_rule_ips_json='["10.0.0.0/8", "2001:db8::/32"]',
            extra_blocks=[
                {"zone": "top", "content": "add_header X-Served-By gateway;"},
                {"zone": "ssl", "content": "ssl_protocols TLSv1.3;"},
                {"zone": "proxy", "content": "proxy_read_timeout 600s;"},
                {"zone": "end", "content": "location = /health {\n    return 200;\n}"},
            ],
        )
        parsed = parse_vhost_block(render_vhost(vhost, [_route(1, "/photos/", "hot")]))
        assert (parsed["server_name"], parsed["listen_port"], parsed["ssl"]) == ("s3.example.com", 443, True)
        assert (parsed["ssl_cert_path"], parsed["ssl_key_path"]) == ("/c.pem", "/c.key")
        assert _routes(parsed) == {"/photos/": "hot"}


class TestVhostHandWritten:
    def test_old_style_listen_line_with_http2(self):
        content = f"""{MANAGED_MARKER}
# vhost_id:1 name:s3.example.com updated:2026-04-16T10:00:00Z
server {{
    listen 443 ssl http2;
    server_name s3.example.com;
    ssl_certificate     /etc/ssl/s3.pem;
    ssl_certificate_key /etc/ssl/s3.key;
}}
"""
        parsed = parse_vhost_block(content)
        assert (parsed["listen_port"], parsed["ssl"]) == (443, True)
        assert (parsed["ssl_cert_path"], parsed["ssl_key_path"]) == ("/etc/ssl/s3.pem", "/etc/ssl/s3.key")

    def test_defaults_when_listen_and_server_name_are_missing(self):
        parsed = parse_vhost_block(f"{MANAGED_MARKER}\nserver {{\n    client_max_body_size 0;\n}}\n")
        assert parsed == {
            "server_name": None,
            "listen_port": 80,
            "ssl": False,
            "ssl_cert_path": None,
            "ssl_key_path": None,
            "routes": [],
            "metadata": {},
        }

    def test_location_without_a_route_comment_is_not_a_route(self):
        content = f"""{MANAGED_MARKER}
server {{
    listen 80;
    server_name s3.example.com;
    location /photos/ {{
        proxy_pass http://hot;
    }}
}}
"""
        assert parse_vhost_block(content)["routes"] == []

    def test_location_inside_an_extra_block_is_not_imported_as_a_route(self):
        """An operator's own `location … { proxy_pass … }` must not come back as a route: the
        next generation would then write that location twice."""
        content = f"""{MANAGED_MARKER}
server {{
    listen 80;
    server_name s3.example.com;

    location /health/ {{
        proxy_pass http://monitoring;
    }}

    # route_id:1 prefix:/
    location / {{
        proxy_pass http://hot;
    }}
}}
"""
        routes = parse_vhost_block(content)["routes"]
        assert [(r["path_prefix"], r["pool_name"]) for r in routes] == [("/", "hot")]

    def test_locations_that_do_not_proxy_to_an_upstream_are_not_routes(self):
        content = f"""{MANAGED_MARKER}
server {{
    listen 80;
    server_name s3.example.com;
    location /static/ {{
        root /var/www;
    }}
    location /health {{
        return 200;
    }}
    location /secure/ {{
        proxy_pass https://elsewhere.example.com;
    }}
    # route_id:1 prefix:/photos/
    location /photos/ {{
        proxy_pass http://hot;
    }}
}}
"""
        assert _routes(parse_vhost_block(content)) == {"/photos/": "hot"}

    def test_exact_match_locations_are_not_routes(self):
        content = f"""{MANAGED_MARKER}
server {{
    listen 80;
    server_name s3.example.com;
    location = /photos {{
        proxy_pass http://hot;
    }}
    location = /__internal {{
        internal;
        proxy_pass http://hot$request_uri;
    }}
}}
"""
        assert parse_vhost_block(content)["routes"] == []

    def test_route_comment_for_another_prefix_does_not_make_a_location_a_route(self):
        content = f"""{MANAGED_MARKER}
server {{
    listen 80;
    server_name s3.example.com;
    # route_id:5 prefix:/videos/ migration_src:src migration_dst:dst
    location /photos/ {{
        proxy_pass http://hot;
    }}
}}
"""
        assert parse_vhost_block(content)["routes"] == []

    @pytest.mark.parametrize(
        "content",
        [
            f"{MANAGED_MARKER}\n",
            f"{MANAGED_MARKER}\n# vhost_id:1 name:s3.example.com\nlisten 80;\nserver_name s3.example.com;\n",
            f"{MANAGED_MARKER}\nserver {{\n    listen 80;\n    server_name s3.example.com;\n",
        ],
    )
    def test_managed_file_without_a_complete_server_block_is_nothing(self, content):
        assert parse_vhost_block(content) is None

    def test_an_upstream_file_is_not_a_vhost(self):
        content = render_upstream(_pool(), [_member("10.0.0.1:9000")])
        assert parse_vhost_block(content) is None
