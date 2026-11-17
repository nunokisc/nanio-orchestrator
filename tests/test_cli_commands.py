"""The command line: serve, config subcommands, rebuild-db, orphaned list and the root-only wrappers."""

from __future__ import annotations

import logging
import runpy
import sqlite3
import sys
from logging.handlers import RotatingFileHandler
from types import SimpleNamespace

import nanio_orchestrator.config as cfg_mod
import nanio_orchestrator.db as db_mod
import pytest
from click.testing import CliRunner
from cryptography.fernet import Fernet
from nanio_orchestrator import cli
from nanio_orchestrator.nginx import executor
from nanio_orchestrator.nginx.generator import render_upstream

PREFIX = "NANIO_ORCHESTRATOR_"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """An isolated working directory, database and nginx directory; settings singletons reset."""
    db_path = tmp_path / "data" / "orchestrator.db"
    nginx_dir = tmp_path / "nginx"
    monkeypatch.chdir(tmp_path)  # dev mode reads and writes ./dev.env
    for name in ("SECRET", "LOG_FILE", "DEV", "DB_BACKUP_PATH", "DB_BACKUP_ROTATE", "HOST", "PORT"):
        monkeypatch.delenv(PREFIX + name, raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv(PREFIX + "DB_PATH", str(db_path))
    monkeypatch.setenv(PREFIX + "NGINX_CONFIG_DIR", str(nginx_dir))
    monkeypatch.setattr(cfg_mod, "settings", None)
    monkeypatch.setattr(db_mod, "_db_path", None)
    return SimpleNamespace(root=tmp_path, db_path=db_path, nginx_dir=nginx_dir, config_file=tmp_path / "dev.env")


@pytest.fixture
def server(monkeypatch):
    """uvicorn.run replaced by a recorder: no server is ever started."""
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: calls.append((args, kwargs)))
    return calls


def run(*args, **kwargs):
    """Invoke the CLI; anything but a clean return or sys.exit is a test failure."""
    result = CliRunner().invoke(cli.main, list(args), **kwargs)
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    return result


def sql(env, statement, params=()):
    with sqlite3.connect(env.db_path) as conn:
        rows = conn.execute(statement, params).fetchall()
        conn.commit()
    conn.close()
    return rows


def write_pool_conf(env, name, members=("10.0.0.1:9000",)):
    pools = env.nginx_dir / "pools"
    pools.mkdir(parents=True, exist_ok=True)
    pool = {"id": 1, "name": name, "type": "nanio", "lb_method": "least_conn", "keepalive": 32}
    rows = [
        {"address": m, "enabled": 1, "role": "primary", "weight": 1, "max_fails": 3, "fail_timeout_s": 30}
        for m in members
    ]
    (pools / f"{name}.conf").write_text(render_upstream(pool, rows))


# ── helpers ───────────────────────────────────────────────────────────────────


def test_config_file_is_dev_env_in_dev_mode_and_the_installed_file_otherwise(monkeypatch):
    monkeypatch.setattr(cfg_mod, "DEV_MODE", True)
    assert cli._get_config_path() == "dev.env"
    monkeypatch.setattr(cfg_mod, "DEV_MODE", False)
    assert cli._get_config_path() == "/etc/nanio-orchestrator/config.env"


@pytest.mark.parametrize(
    "value, is_secret, shown",
    [
        ("info", False, "info"),
        (8080, False, "8080"),
        (None, False, "(unset)"),
        (None, True, "(not set)"),
        ("", True, "(not set)"),
        ("abc", True, "****"),
        ("abcdefgh", True, "abcd****"),
    ],
)
def test_secrets_are_masked_and_plain_values_shown(value, is_secret, shown):
    assert cli._mask(value, is_secret) == shown


def test_set_config_value_creates_the_file_and_its_directory(tmp_path):
    target = tmp_path / "etc" / "config.env"
    cli._set_config_value("port", "9000", str(target))
    assert target.read_text() == "NANIO_ORCHESTRATOR_PORT=9000\n"


def test_set_config_value_replaces_in_place_and_keeps_other_lines(tmp_path):
    target = tmp_path / "config.env"
    target.write_text("# header\nNANIO_ORCHESTRATOR_PORT=8080\nNANIO_ORCHESTRATOR_HOST=0.0.0.0\n")

    cli._set_config_value("port", "9000", str(target))

    assert target.read_text() == "# header\nNANIO_ORCHESTRATOR_PORT=9000\nNANIO_ORCHESTRATOR_HOST=0.0.0.0\n"


def test_set_config_value_activates_a_commented_out_setting(tmp_path):
    target = tmp_path / "config.env"
    target.write_text("# NANIO_ORCHESTRATOR_LOG_LEVEL=info\nNANIO_ORCHESTRATOR_PORT=8080\n")

    cli._set_config_value("log_level", "debug", str(target))

    assert target.read_text() == "NANIO_ORCHESTRATOR_LOG_LEVEL=debug\nNANIO_ORCHESTRATOR_PORT=8080\n"


def test_set_config_value_appends_an_unknown_key_without_touching_similar_ones(tmp_path):
    target = tmp_path / "config.env"
    target.write_text("NANIO_ORCHESTRATOR_DB_PATH_OLD=/x\n")

    cli._set_config_value("db_path", "/y", str(target))

    assert target.read_text() == "NANIO_ORCHESTRATOR_DB_PATH_OLD=/x\nNANIO_ORCHESTRATOR_DB_PATH=/y\n"


# ── serve ─────────────────────────────────────────────────────────────────────


def test_serve_in_dev_mode_prepares_dirs_and_database_and_starts_with_reload(env, server):
    result = run("serve")

    assert result.exit_code == 0
    assert "nanio-orchestrator dev mode → http://localhost:8080  API key: test-key" in result.output
    assert (env.nginx_dir / "pools").is_dir() and (env.nginx_dir / "vhosts").is_dir()
    assert (env.db_path.parent / "migrations").is_dir()
    assert sql(env, "SELECT COUNT(*) FROM pools") == [(0,)]  # schema created before the server starts
    assert server == [
        (
            ("nanio_orchestrator.app:create_app",),
            {"host": "0.0.0.0", "port": 8080, "reload": True, "factory": True, "log_level": "warning"},
        )
    ]


def test_no_subcommand_starts_the_server(env, server):
    result = run()

    assert result.exit_code == 0
    assert len(server) == 1


def test_serve_host_and_port_options_override_the_settings(env, server):
    run("serve", "--host", "127.0.0.1", "--port", "9999")

    assert server[0][1]["host"] == "127.0.0.1"
    assert server[0][1]["port"] == 9999


def test_serve_in_production_mode_announces_the_bind_address_and_does_not_reload(env, server, monkeypatch):
    monkeypatch.setenv(PREFIX + "DEV", "false")
    monkeypatch.setenv(PREFIX + "HOST", "10.1.2.3")
    monkeypatch.setenv(PREFIX + "PORT", "8443")

    result = run("serve")

    assert result.exit_code == 0
    assert "nanio-orchestrator starting on 10.1.2.3:8443" in result.output
    assert "dev mode" not in result.output
    assert server[0][1]["reload"] is False


def test_serve_reload_flag_enables_reload_in_production_mode(env, server, monkeypatch):
    monkeypatch.setenv(PREFIX + "DEV", "false")

    run("serve", "--reload")

    assert server[0][1]["reload"] is True


def test_serve_logs_to_a_rotating_file_when_log_file_is_set(env, server, monkeypatch):
    log_file = env.root / "logs" / "nested" / "orchestrator.log"
    monkeypatch.setenv(PREFIX + "LOG_FILE", str(log_file))
    root_logger = logging.getLogger()
    before = list(root_logger.handlers)

    try:
        result = run("serve")
        added = [h for h in root_logger.handlers if h not in before]

        assert result.exit_code == 0
        assert f"Logging to file: {log_file}" in result.output
        assert len(added) == 1
        handler = added[0]
        assert isinstance(handler, RotatingFileHandler)
        assert handler.baseFilename == str(log_file)
        assert handler.level == logging.WARNING  # NANIO_ORCHESTRATOR_LOG_LEVEL=warning
        assert (handler.maxBytes, handler.backupCount) == (10 * 1024 * 1024, 5)

        handler.emit(logging.LogRecord("nanio.test", logging.ERROR, __file__, 1, "disk is full", None, None))
        handler.flush()
        assert "ERROR    nanio.test: disk is full" in log_file.read_text()
    finally:
        for h in [h for h in root_logger.handlers if h not in before]:
            root_logger.removeHandler(h)
            h.close()


def test_serve_without_log_file_adds_no_handler(env, server):
    before = list(logging.getLogger().handlers)

    result = run("serve")

    assert "Logging to file" not in result.output
    assert logging.getLogger().handlers == before


# ── install / doctor / remove wrappers ────────────────────────────────────────


@pytest.mark.parametrize("args, start", [(["install"], True), (["install", "--no-start"], False)])
def test_install_command_passes_the_start_choice(monkeypatch, args, start):
    calls = []
    monkeypatch.setattr("nanio_orchestrator.install.run_install", lambda **kw: calls.append(kw))

    assert run(*args).exit_code == 0
    assert calls == [{"start": start}]


def test_doctor_command_runs_the_doctor(monkeypatch):
    calls = []
    monkeypatch.setattr("nanio_orchestrator.install.run_doctor", lambda: calls.append("doctor"))

    assert run("doctor").exit_code == 0
    assert calls == ["doctor"]


@pytest.mark.parametrize(
    "args, expected",
    [
        (["remove"], {"purge": False, "yes": False}),
        (["remove", "--purge"], {"purge": True, "yes": False}),
        (["remove", "-y"], {"purge": False, "yes": True}),
        (["remove", "--purge", "--yes"], {"purge": True, "yes": True}),
    ],
)
def test_remove_command_passes_purge_and_confirmation(monkeypatch, args, expected):
    calls = []
    monkeypatch.setattr("nanio_orchestrator.install.run_remove", lambda **kw: calls.append(kw))

    assert run(*args).exit_code == 0
    assert calls == [expected]


# ── rebuild-db ────────────────────────────────────────────────────────────────


def test_rebuild_db_imports_the_configs_found_on_disk(env):
    write_pool_conf(env, "pool-a", members=("10.0.0.1:9000", "10.0.0.2:9000"))

    result = run("rebuild-db")

    assert result.exit_code == 0
    assert "Pools:       1" in result.output
    assert "Members:     2" in result.output
    assert "Vhosts:      0" in result.output
    assert "Routes:      0" in result.output
    assert "Migrations:  0 (in progress — will auto-resume)" in result.output
    assert "Credentials: 0 recovered" in result.output
    assert "audit_log: not recoverable" in result.output
    assert "Database rebuilt successfully." in result.output
    assert sql(env, "SELECT name FROM pools") == [("pool-a",)]
    assert sql(env, "SELECT address FROM pool_members ORDER BY address") == [("10.0.0.1:9000",), ("10.0.0.2:9000",)]


def test_rebuild_db_dry_run_reports_and_writes_nothing(env):
    write_pool_conf(env, "pool-a")

    result = run("rebuild-db", "--dry-run")

    assert result.exit_code == 0
    assert "DRY RUN — no changes written" in result.output
    assert "✓ pool-a" in result.output
    assert "(nanio, 1 members, no credentials, no sidecar)" in result.output
    assert "migration(s) in progress" not in result.output
    assert "Database rebuilt successfully" not in result.output
    assert not env.db_path.exists()


def test_rebuild_db_dry_run_describes_what_each_sidecar_recovers(env, monkeypatch):
    async def fake_rebuild(dry_run=False):
        assert dry_run is True
        return {
            "pools": [
                {"name": "with-creds", "type": "nanio", "members": 2, "has_credentials": True, "has_sidecar": True},
                {"name": "static", "type": "http", "members": 1, "has_credentials": False, "has_sidecar": False},
            ],
            "vhosts": [
                {"server_name": "s3.example.com", "routes": 3, "has_default_pool": True, "has_sidecar": True},
                {"server_name": "old.example.com", "routes": 0, "has_default_pool": False, "has_sidecar": False},
            ],
            "migrations": 2,
        }

    monkeypatch.setattr("nanio_orchestrator.rebuild.rebuild_from_disk", fake_rebuild)

    result = run("rebuild-db", "--dry-run")

    assert result.exit_code == 0
    assert "(nanio, 2 members, credentials recovered, sidecar ✓)" in result.output
    assert "(http, 1 members, no credentials, no sidecar)" in result.output
    assert "(3 routes, default_pool recovered, sidecar ✓)" in result.output
    assert "(0 routes, no default_pool, no sidecar)" in result.output
    assert "✓ 2 migration(s) in progress" in result.output


def test_rebuild_db_refuses_when_the_database_already_has_data(env):
    write_pool_conf(env, "pool-on-disk")
    db_mod.init_db_sync()
    sql(env, "INSERT INTO pools (name) VALUES ('pool-in-db')")

    result = run("rebuild-db")

    assert result.exit_code == 1
    assert "✗ Database already contains data." in result.output
    assert "Use --force to overwrite, or --dry-run to preview." in result.output
    assert "Rebuilding database from disk" not in result.output
    assert sql(env, "SELECT name FROM pools") == [("pool-in-db",)]


def test_rebuild_db_force_replaces_the_existing_data(env):
    write_pool_conf(env, "pool-on-disk")
    db_mod.init_db_sync()
    sql(env, "INSERT INTO pools (name) VALUES ('pool-in-db')")

    result = run("rebuild-db", "--force")

    assert result.exit_code == 0
    assert "Database rebuilt successfully." in result.output
    assert sql(env, "SELECT name FROM pools") == [("pool-on-disk",)]


def test_rebuild_db_prints_the_warnings_of_the_import(env, monkeypatch):
    async def fake_rebuild(dry_run=False):
        return {
            "pools_imported": 1,
            "members_imported": 2,
            "vhosts_imported": 3,
            "routes_imported": 4,
            "migrations_imported": 5,
            "credentials_recovered": 6,
            "warnings": ["vhost a.example.com: default pool not found"],
        }

    monkeypatch.setattr("nanio_orchestrator.rebuild.rebuild_from_disk", fake_rebuild)

    result = run("rebuild-db")

    assert result.exit_code == 0
    assert "Migrations:  5 (in progress — will auto-resume)" in result.output
    assert "Credentials: 6 recovered" in result.output
    assert "⚠ vhost a.example.com: default pool not found" in result.output
    assert "Next: systemctl restart nanio-orchestrator" in result.output


# ── config show / get / set ───────────────────────────────────────────────────


def test_config_show_groups_settings_and_masks_secrets(env, monkeypatch):
    monkeypatch.setenv(PREFIX + "SECRET", "abcdefghijklmnop")

    result = run("config", "show")

    assert result.exit_code == 0
    lines = result.output.splitlines()
    headings = [ln for ln in lines if ln and not ln.startswith(" ") and not ln.startswith("Config file")]
    assert headings == ["Core", "Database", "Security", "S3", "Nginx", "Migrations"]
    by_field = {ln.split()[0]: ln for ln in lines if ln.startswith("  ")}
    assert set(by_field) == set(cli._SETTINGS_META)
    assert "test****" in by_field["api_key"] and "test-key" not in result.output
    assert "abcd****" in by_field["secret"] and "abcdefghijklmnop" not in result.output
    assert str(env.db_path) in by_field["db_path"]
    assert f"{env.db_path}.bak" in by_field["db_backup_path"]  # the effective value, not "(unset)"
    assert by_field["port"].split()[1] == "8080"
    assert lines[-1] == "Config file (dev): dev.env"


def test_config_show_marks_an_unset_secret_and_names_the_production_file(env, monkeypatch):
    monkeypatch.setattr(cfg_mod, "DEV_MODE", False)

    result = run("config", "show")

    assert "secret                       (not set)" in result.output
    assert result.output.splitlines()[-1] == "Config file (production): /etc/nanio-orchestrator/config.env"


def test_config_get_prints_a_single_value(env):
    result = run("config", "get", "db_path")
    assert (result.exit_code, result.output) == (0, f"{env.db_path}\n")


def test_config_get_accepts_the_environment_variable_name(env):
    result = run("config", "get", "NANIO_ORCHESTRATOR_API_KEY")
    assert (result.exit_code, result.output) == (0, "test-key\n")


def test_config_get_prints_the_effective_backup_path(env, monkeypatch):
    assert run("config", "get", "db_backup_path").output == f"{env.db_path}.bak\n"

    monkeypatch.setattr(cfg_mod, "settings", None)
    monkeypatch.setenv(PREFIX + "DB_BACKUP_PATH", "/backups/o.db")
    assert run("config", "get", "db_backup_path").output == "/backups/o.db\n"


def test_config_get_prints_an_empty_line_for_an_unset_value(env):
    result = run("config", "get", "secret")
    assert (result.exit_code, result.output) == (0, "\n")


def test_config_get_rejects_an_unknown_setting(env):
    result = run("config", "get", "no_such_setting")
    assert result.exit_code == 1
    assert "Unknown setting: no_such_setting" in result.output


def test_config_set_writes_the_value_to_the_config_file(env):
    result = run("config", "set", "log_level", "debug")

    assert result.exit_code == 0
    assert result.output == "✓ NANIO_ORCHESTRATOR_LOG_LEVEL=debug  →  dev.env\n"
    assert env.config_file.read_text() == "NANIO_ORCHESTRATOR_LOG_LEVEL=debug\n"


def test_config_set_accepts_the_full_name_and_does_not_echo_a_secret(env):
    env.config_file.write_text("NANIO_ORCHESTRATOR_API_KEY=old\n")

    result = run("config", "set", "NANIO_ORCHESTRATOR_API_KEY", "a-very-long-key")

    assert result.exit_code == 0
    assert "NANIO_ORCHESTRATOR_API_KEY=a-ve****" in result.output
    assert "a-very-long-key" not in result.output
    assert env.config_file.read_text() == "NANIO_ORCHESTRATOR_API_KEY=a-very-long-key\n"


def test_config_set_rejects_an_unknown_setting_and_writes_nothing(env):
    result = run("config", "set", "nonsense", "1")

    assert result.exit_code == 1
    assert "Unknown setting: 'nonsense'" in result.output
    assert "Known settings: api_key, bucket_sync_interval" in result.output
    assert not env.config_file.exists()


def test_a_value_written_by_config_set_is_what_the_settings_load(env):
    run("config", "set", "migration_max_parallel", "7")

    assert run("config", "get", "migration_max_parallel").output == "7\n"


# ── config generate-secret / edit ─────────────────────────────────────────────


def test_generate_secret_prints_a_valid_fernet_key_and_writes_nothing(env):
    result = run("config", "generate-secret")

    assert result.exit_code == 0
    key = result.output.strip()
    assert Fernet(key.encode()).decrypt(Fernet(key.encode()).encrypt(b"x")) == b"x"
    assert not env.config_file.exists()


def test_generate_secret_set_stores_the_printed_key(env):
    result = run("config", "generate-secret", "--set")

    assert result.exit_code == 0
    key = result.output.splitlines()[0]
    Fernet(key.encode())
    assert env.config_file.read_text() == f"NANIO_ORCHESTRATOR_SECRET={key}\n"
    assert "✓ NANIO_ORCHESTRATOR_SECRET written to dev.env" in result.output


@pytest.fixture
def editor(monkeypatch):
    """os.execvp replaced by a recorder; ``available`` is the set of editors found on PATH."""
    state = SimpleNamespace(execs=[], available=set())
    monkeypatch.setattr(cli.os, "execvp", lambda prog, argv: state.execs.append((prog, argv)))
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}" if name in state.available else None)
    return state


def test_config_edit_without_a_config_file_says_how_to_create_one(env, editor):
    result = run("config", "edit")

    assert result.exit_code == 1
    assert "Config file not found: dev.env" in result.output
    assert "Run 'nanio-orchestrator install' first" in result.output
    assert editor.execs == []


@pytest.mark.parametrize(
    "environ, expected",
    [
        ({"EDITOR": "vim", "VISUAL": "code"}, "vim"),
        ({"VISUAL": "code"}, "code"),
    ],
)
def test_config_edit_opens_the_editor_from_the_environment(env, editor, monkeypatch, environ, expected):
    env.config_file.write_text("")
    editor.available = {"nano", "vi"}
    for name, value in environ.items():
        monkeypatch.setenv(name, value)

    result = run("config", "edit")

    assert result.exit_code == 0
    assert editor.execs == [(expected, [expected, "dev.env"])]


@pytest.mark.parametrize("available, expected", [({"nano", "vi"}, "nano"), ({"vi"}, "vi")])
def test_config_edit_falls_back_to_nano_then_vi(env, editor, available, expected):
    env.config_file.write_text("")
    editor.available = available

    result = run("config", "edit")

    assert result.exit_code == 0
    assert editor.execs == [(expected, [expected, "dev.env"])]


def test_config_edit_without_any_editor_points_at_the_file(env, editor):
    env.config_file.write_text("")

    result = run("config", "edit")

    assert result.exit_code == 1
    assert "No editor found. Edit manually: dev.env" in result.output
    assert editor.execs == []


# ── config validate / reload / rebuild ────────────────────────────────────────


def test_config_validate_and_reload_are_dry_runs_in_dev_mode(env, monkeypatch):
    monkeypatch.setattr("os.geteuid", lambda: 995)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/sbin/nginx")

    validate = run("config", "validate")
    reload = run("config", "reload")

    assert (validate.exit_code, validate.output) == (0, "[DRY RUN] sudo /usr/sbin/nginx -t\n")
    assert (reload.exit_code, reload.output) == (0, "[DRY RUN] sudo /usr/sbin/nginx -s reload\n")


@pytest.mark.parametrize("command, function", [("validate", "test_config"), ("reload", "reload_nginx")])
def test_config_validate_and_reload_exit_non_zero_when_nginx_fails(env, monkeypatch, command, function):
    async def failing():
        return executor.NginxExecResult(ok=False, output="nginx: [emerg] bad directive", return_code=1)

    monkeypatch.setattr(executor, function, failing)

    result = run("config", command)

    assert result.exit_code == 1
    assert "nginx: [emerg] bad directive" in result.output


def test_config_rebuild_writes_every_config_and_sidecar_from_the_database(env):
    db_mod.init_db_sync()
    sql(env, "INSERT INTO pools (id, name) VALUES (1, 'pool-a')")
    sql(env, "INSERT INTO pool_members (pool_id, address) VALUES (1, '10.0.0.1:9000')")

    result = run("config", "rebuild")

    conf = env.nginx_dir / "pools" / "pool-a.conf"
    assert result.exit_code == 0
    assert f"✓ {conf}" in result.output
    assert "✓ sidecars (.meta.json) rewritten" in result.output
    assert "upstream pool-a {" in conf.read_text()
    assert "server 10.0.0.1:9000" in conf.read_text()
    assert (env.nginx_dir / "pools" / "pool-a.meta.json").exists()


def test_config_rebuild_reports_removed_files_and_a_rejected_config(env, monkeypatch):
    async def rejected():
        return {
            "ok": False,
            "stage": "test",
            "output": "nginx: [emerg] host not found in upstream",
            "written": ["/etc/nginx/nanio/pools/a.conf"],
            "removed": ["/etc/nginx/nanio/pools/empty.conf"],
        }

    monkeypatch.setattr("nanio_orchestrator.nginx.apply.rebuild_all_configs", rejected)

    result = run("config", "rebuild")

    assert result.exit_code == 1
    assert "- /etc/nginx/nanio/pools/empty.conf (removed: pool has no members)" in result.output
    assert "nginx test failed — previous config files restored" in result.output
    assert "nginx: [emerg] host not found in upstream" in result.output
    assert "✓ /etc/nginx/nanio/pools/a.conf" not in result.output
    assert "sidecars (.meta.json) rewritten" not in result.output


# ── orphaned list ─────────────────────────────────────────────────────────────


def test_orphaned_list_says_when_there_is_nothing(env):
    result = run("orphaned", "list")

    assert (result.exit_code, result.output) == (0, "No orphaned data found.\n")


def test_orphaned_list_shows_each_migration_with_source_data_left_behind(env):
    db_mod.init_db_sync()
    sql(env, "INSERT INTO pools (id, name) VALUES (1, 'old-pool'), (2, 'new-pool')")
    insert = (
        "INSERT INTO migrations (id, vhost_id, bucket, src_pool_id, dst_pool_id, phase,"
        " orphaned_source_pool_id, orphaned_source_prefix, orphaned_at) VALUES (?, 1, ?, 1, 2, 'done', ?, ?, ?)"
    )
    sql(env, insert, (1, "photos", 1, "photos/", "2026-01-02T03:04:05"))
    sql(env, insert, (2, "logs", 77, None, None))  # its source pool no longer exists
    sql(env, insert, (3, "kept", None, None, None))  # nothing orphaned

    result = run("orphaned", "list")

    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert lines[0].split() == ["ID", "Bucket", "Source", "Pool", "Prefix", "Orphaned", "At"]
    assert set(lines[1]) == {"-"}
    rows = {ln.split()[0]: ln.split() for ln in lines[2:]}
    assert rows == {
        "1": ["1", "photos", "old-pool", "photos/", "2026-01-02T03:04:05"],
        "2": ["2", "logs", "77"],
    }


# ── module entry points ───────────────────────────────────────────────────────


def test_python_dash_m_runs_the_cli(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["nanio-orchestrator", "config", "--help"])
    monkeypatch.delitem(sys.modules, "nanio_orchestrator.__main__", raising=False)

    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("nanio_orchestrator", run_name="__main__")

    assert exit_info.value.code == 0
    assert "Config management subcommands." in capsys.readouterr().out


def test_the_cli_module_can_be_run_as_a_script(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["cli.py", "orphaned", "--help"])

    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(cli.__file__, run_name="__main__")

    assert exit_info.value.code == 0
    assert "Orphaned source data management." in capsys.readouterr().out


def test_every_setting_shown_belongs_to_a_listed_category():
    assert {category for category, _, _ in cli._SETTINGS_META.values()} == set(cli._CATEGORY_ORDER)
