"""Request validation: what the API accepts and what it refuses before anything reaches nginx."""

from __future__ import annotations

import pytest
from nanio_orchestrator.models import (
    CredentialSet,
    MemberCreate,
    NodeConfigRequest,
    PoolCreate,
    PoolUpdate,
    RcloneMigrationCreate,
    RouteCreate,
    RouteUpdate,
    VhostCreate,
    VhostExtraBlock,
    VhostUpdate,
)
from pydantic import ValidationError


def _rejects(model, message: str, **fields) -> None:
    with pytest.raises(ValidationError) as exc:
        model(**fields)
    assert message in str(exc.value)


class TestPool:
    def test_defaults(self):
        pool = PoolCreate(name="pool-2025")
        assert (pool.type, pool.lb_method, pool.keepalive, pool.source_nanio_pool_id) == (
            "nanio",
            "least_conn",
            32,
            None,
        )

    @pytest.mark.parametrize("name", ["a", "pool_1", "pool-2025.eu", "A" * 128])
    def test_names_safe_for_a_file_and_an_upstream_are_accepted(self, name):
        assert PoolCreate(name=name).name == name

    @pytest.mark.parametrize("name", ["", "has space", "semi;colon", "a/b", "../etc", "brace{", "A" * 129])
    def test_other_names_are_refused(self, name):
        with pytest.raises(ValidationError):
            PoolCreate(name=name)
        with pytest.raises(ValidationError):
            PoolUpdate(name=name)

    @pytest.mark.parametrize(
        "fields", [{"type": "cold"}, {"lb_method": "random"}, {"keepalive": -1}, {"keepalive": 1025}]
    )
    def test_unknown_type_method_or_keepalive_out_of_range_is_refused(self, fields):
        with pytest.raises(ValidationError):
            PoolCreate(name="a", **fields)
        with pytest.raises(ValidationError):
            PoolUpdate(**fields)

    def test_http_pool_may_name_the_nanio_pool_it_serves(self):
        assert PoolCreate(name="web", type="http", source_nanio_pool_id=3).source_nanio_pool_id == 3

    def test_nanio_pool_may_not(self):
        _rejects(PoolCreate, "source_nanio_pool_id can only be set on http pools", name="a", source_nanio_pool_id=3)

    def test_update_refuses_the_link_only_when_the_type_becomes_nanio(self):
        _rejects(PoolUpdate, "cannot be set on nanio pools", type="nanio", source_nanio_pool_id=3)
        # Type unchanged: the endpoint checks against the stored type
        assert PoolUpdate(source_nanio_pool_id=3).source_nanio_pool_id == 3
        assert PoolUpdate(type="http", source_nanio_pool_id=3).type == "http"
        assert PoolUpdate(type="nanio").source_nanio_pool_id is None


class TestMember:
    @pytest.mark.parametrize(
        ("given", "stored"),
        [
            ("10.0.0.1:9000", "10.0.0.1:9000"),
            ("  node-1.internal:9000 ", "node-1.internal:9000"),
            ("[::1]:9000", "[::1]:9000"),
            ("unix:/run/nanio.sock", "unix:/run/nanio.sock"),
        ],
    )
    def test_host_port_and_unix_sockets_are_accepted(self, given, stored):
        assert MemberCreate(address=given).address == stored

    @pytest.mark.parametrize("address", ["10.0.0.1", "node-1.internal", "   "])
    def test_address_without_a_port_is_refused(self, address):
        _rejects(MemberCreate, "address must be host:port or unix:/path", address=address)

    @pytest.mark.parametrize(
        "fields",
        [{"role": "backup"}, {"weight": 0}, {"weight": 101}, {"max_fails": -1}, {"fail_timeout_s": 601}],
    )
    def test_out_of_range_tuning_is_refused(self, fields):
        with pytest.raises(ValidationError):
            MemberCreate(address="10.0.0.1:9000", **fields)

    def test_defaults(self):
        member = MemberCreate(address="10.0.0.1:9000")
        assert (member.role, member.weight, member.max_fails, member.fail_timeout_s, member.enabled) == (
            "active",
            1,
            3,
            30,
            True,
        )


PLAIN = {"server_name": "s3.example.com", "ssl": False}


class TestVhostServerName:
    @pytest.mark.parametrize(
        ("given", "stored"),
        [
            ("s3.example.com", "s3.example.com"),
            ("*.s3.example.com", "*.s3.example.com"),
            ("  s3.example.com  ", "s3.example.com"),
            ("localhost", "localhost"),
            ("a", "a"),
        ],
    )
    def test_hostnames_and_wildcards_are_accepted(self, given, stored):
        assert VhostCreate(server_name=given, ssl=False).server_name == stored
        assert VhostUpdate(server_name=given).server_name == stored

    @pytest.mark.parametrize(
        "name",
        [
            "s3.example.com; return 200",
            "a b",
            "{evil}",
            "back\\slash",
            "-leading.example.com",
            "trailing.",
            "*",
            "*.",
            "",
        ],
    )
    def test_anything_that_could_alter_the_nginx_config_is_refused(self, name):
        with pytest.raises(ValidationError):
            VhostCreate(server_name=name, ssl=False)
        with pytest.raises(ValidationError):
            VhostUpdate(server_name=name)

    def test_refusal_explains_what_is_allowed(self):
        _rejects(VhostCreate, "server_name must be a valid hostname or wildcard", server_name="a;b", ssl=False)
        _rejects(VhostUpdate, "server_name must be a valid hostname or wildcard", server_name="a;b")

    def test_update_without_a_server_name_leaves_it_alone(self):
        assert VhostUpdate(listen_port=8080).server_name is None
        assert VhostUpdate(server_name=None, listen_port=8080).server_name is None


class TestVhostTls:
    def test_tls_is_the_default_and_needs_both_files(self):
        vhost = VhostCreate(server_name="s3.example.com", ssl_cert_path="/c.pem", ssl_key_path="/c.key")
        assert (vhost.ssl, vhost.listen_port) == (True, 443)

    @pytest.mark.parametrize("paths", [{}, {"ssl_cert_path": "/c.pem"}, {"ssl_key_path": "/c.key"}])
    def test_tls_without_certificate_or_key_is_refused(self, paths):
        _rejects(
            VhostCreate,
            "ssl_cert_path and ssl_key_path are required when ssl is enabled",
            server_name="s3.example.com",
            **paths,
        )

    def test_plain_http_needs_neither(self):
        assert VhostCreate(**PLAIN).ssl_cert_path is None

    @pytest.mark.parametrize("port", [0, 65536])
    def test_port_out_of_range_is_refused(self, port):
        with pytest.raises(ValidationError):
            VhostCreate(**PLAIN, listen_port=port)
        with pytest.raises(ValidationError):
            VhostUpdate(listen_port=port)


class TestVhostIpRules:
    @pytest.mark.parametrize("model", [lambda **kw: VhostCreate(**PLAIN, **kw), VhostUpdate])
    @pytest.mark.parametrize("ips", [["10.0.0.1"], ["10.0.0.0/8", " 192.168.1.0/24 "], ["2001:db8::/32", "::1"]])
    def test_addresses_and_networks_are_accepted(self, model, ips):
        assert model(ip_rule_mode="allow", ip_rule_ips=ips).ip_rule_ips == ips
        assert model(ip_rule_mode="deny", ip_rule_ips=ips).ip_rule_mode == "deny"

    @pytest.mark.parametrize("model", [lambda **kw: VhostCreate(**PLAIN, **kw), VhostUpdate])
    @pytest.mark.parametrize("ips", [None, []])
    def test_a_mode_without_addresses_is_refused(self, model, ips):
        with pytest.raises(ValidationError) as exc:
            model(ip_rule_mode="allow", ip_rule_ips=ips)
        assert "ip_rule_ips must be provided when ip_rule_mode is set" in str(exc.value)

    @pytest.mark.parametrize("model", [lambda **kw: VhostCreate(**PLAIN, **kw), VhostUpdate])
    @pytest.mark.parametrize("bad", ["10.0.0.1; allow all", "all", "example.com", "10.0.0.1 10.0.0.2", ""])
    def test_an_entry_that_is_not_an_address_is_refused(self, model, bad):
        with pytest.raises(ValidationError) as exc:
            model(ip_rule_mode="deny", ip_rule_ips=["10.0.0.1", bad])
        assert f"Invalid IP/CIDR entry: {bad!r}" in str(exc.value)

    @pytest.mark.parametrize("model", [lambda **kw: VhostCreate(**PLAIN, **kw), VhostUpdate])
    def test_addresses_are_checked_even_without_a_mode(self, model):
        assert model(ip_rule_ips=["10.0.0.1"]).ip_rule_mode is None
        with pytest.raises(ValidationError):
            model(ip_rule_ips=["nonsense"])

    @pytest.mark.parametrize("model", [lambda **kw: VhostCreate(**PLAIN, **kw), VhostUpdate])
    def test_unknown_mode_is_refused(self, model):
        with pytest.raises(ValidationError):
            model(ip_rule_mode="block", ip_rule_ips=["10.0.0.1"])

    def test_no_rules_at_all_is_fine(self):
        assert VhostCreate(**PLAIN).ip_rule_ips is None
        assert VhostUpdate().ip_rule_mode is None


class TestVhostExtraBlocks:
    @pytest.mark.parametrize("zone", ["top", "ssl", "proxy", "end"])
    def test_known_zones(self, zone):
        vhost = VhostCreate(**PLAIN, extra_blocks=[{"zone": zone, "content": "add_header X-A b;"}])
        assert vhost.extra_blocks == [VhostExtraBlock(zone=zone, content="add_header X-A b;")]

    def test_unknown_zone_or_empty_content_is_refused(self):
        with pytest.raises(ValidationError):
            VhostExtraBlock(zone="http", content="x;")
        with pytest.raises(ValidationError):
            VhostExtraBlock(zone="top", content="")


class TestRoutePrefix:
    @pytest.mark.parametrize("prefix", ["/photos/", "/photos", "/photos/2025/", "/a.b_c-d@e/", "/A1/"])
    def test_bucket_and_folder_paths_are_accepted(self, prefix):
        assert RouteCreate(path_prefix=prefix, pool_id=1).path_prefix == prefix
        assert RouteUpdate(path_prefix=prefix).path_prefix == prefix

    @pytest.mark.parametrize("prefix", ["photos/", "~ ^/photos", "= /photos"])
    def test_prefix_must_start_with_a_slash(self, prefix):
        _rejects(RouteCreate, "path_prefix must start with /", path_prefix=prefix, pool_id=1)
        _rejects(RouteUpdate, "path_prefix must start with /", path_prefix=prefix)

    @pytest.mark.parametrize(
        "prefix", ["/", "/pho tos/", "/photos/;", "/photos/ { return 200; }", "/a\\b/", "/a?b/", "/a\n/"]
    )
    def test_characters_with_a_meaning_in_nginx_are_refused(self, prefix):
        _rejects(RouteCreate, "path_prefix must match", path_prefix=prefix, pool_id=1)
        _rejects(RouteUpdate, "path_prefix must match", path_prefix=prefix)

    def test_empty_prefix_is_refused_on_create(self):
        with pytest.raises(ValidationError):
            RouteCreate(path_prefix="", pool_id=1)

    def test_update_without_a_prefix_leaves_it_alone(self):
        update = RouteUpdate(pool_id=2)
        assert (update.path_prefix, update.pool_id, update.enabled) == (None, 2, None)
        assert RouteUpdate(path_prefix=None, enabled=False).path_prefix is None

    def test_trailing_newline_is_refused(self):
        """`$` would let it through; the stored prefix must be exactly what nginx is given."""
        _rejects(RouteCreate, "path_prefix must match", path_prefix="/photos/\n", pool_id=1)
        _rejects(RouteUpdate, "path_prefix must match", path_prefix="/photos\n")

    def test_create_defaults(self):
        route = RouteCreate(path_prefix="/photos/", pool_id=1)
        assert (route.allow_orphan, route.enabled) == (False, True)


class TestMigrationFolder:
    @pytest.mark.parametrize(
        ("given", "stored"),
        [
            ("", ""),
            ("   ", ""),
            ("/", ""),
            ("2025", "2025/"),
            ("2025/", "2025/"),
            ("/2025/q1/", "2025/q1/"),
            ("  2025/q1  ", "2025/q1/"),
            ("user@example.com/in.box", "user@example.com/in.box/"),
        ],
    )
    def test_folder_is_stored_without_leading_and_with_trailing_slash(self, given, stored):
        assert RcloneMigrationCreate(bucket="photos", dst_pool_id=2, folder=given).folder == stored

    def test_whole_bucket_is_the_default(self):
        migration = RcloneMigrationCreate(bucket="photos", dst_pool_id=2)
        assert (migration.folder, migration.src_pool_id, migration.vhost_id) == ("", None, None)

    @pytest.mark.parametrize("folder", ["a b", "a;b", "a/{b}", "a\\b", "a?b", "é"])
    def test_folder_with_other_characters_is_refused(self, folder):
        _rejects(
            RcloneMigrationCreate,
            "folder may contain letters, digits and . _ @ - separated by /",
            bucket="photos",
            dst_pool_id=2,
            folder=folder,
        )

    def test_bucket_is_required(self):
        with pytest.raises(ValidationError):
            RcloneMigrationCreate(bucket="", dst_pool_id=2)


class TestCredentials:
    def test_region_defaults_to_us_east_1(self):
        creds = CredentialSet(access_key="AKIA", secret_key="secret")
        assert (creds.region, creds.endpoint_url) == ("us-east-1", None)

    @pytest.mark.parametrize("fields", [{"access_key": "", "secret_key": "s"}, {"access_key": "a", "secret_key": ""}])
    def test_empty_keys_are_refused(self, fields):
        with pytest.raises(ValidationError):
            CredentialSet(**fields)


class TestNodeConfigRequest:
    def test_defaults(self):
        request = NodeConfigRequest(node_type="nanio-only")
        assert (request.data_dir, request.nanio_port, request.nanio_host, request.nanio_region) == (
            "/data",
            None,
            "0.0.0.0",
            "us-east-1",
        )
        assert (request.access_key, request.secret_key, request.nanio_bin) == (None, None, "/opt/nanio/bin/nanio")

    @pytest.mark.parametrize(
        "fields",
        [
            {"node_type": "nginx-only", "data_dir": "/srv/nanio-data/pool_1"},
            {"node_type": "nginx-nanio", "nanio_port": 9001, "nanio_host": "::1"},
            {"node_type": "nanio-only", "access_key": "AKIA+/=", "secret_key": "s3cr3t with spaces"},
        ],
    )
    def test_ordinary_values_are_accepted(self, fields):
        request = NodeConfigRequest(**fields)
        for name, value in fields.items():
            assert getattr(request, name) == value

    @pytest.mark.parametrize(
        "fields",
        [
            {"node_type": "nanio"},
            {"data_dir": "relative/path"},
            {"data_dir": "/data; rm -rf /"},
            {"data_dir": "/data\nExecStart=/bin/sh"},
            {"nanio_port": 0},
            {"nanio_port": 70000},
            {"nanio_host": "0.0.0.0 evil"},
            {"nanio_region": "us east"},
            {"access_key": 'a"b'},
            {"secret_key": "a\\b"},
            {"secret_key": "line\nbreak"},
            {"nanio_bin": "/opt/nanio/bin/nanio --flag"},
        ],
    )
    def test_values_that_could_break_out_of_the_generated_files_are_refused(self, fields):
        with pytest.raises(ValidationError):
            NodeConfigRequest(**{"node_type": "nanio-only", **fields})
