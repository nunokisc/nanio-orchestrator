"""How the process decides between dev and production mode, and how it calls nginx."""

from __future__ import annotations

import pytest
from click.testing import CliRunner
from nanio_orchestrator import config as cfg
from nanio_orchestrator.nginx import executor
from pydantic import ValidationError


@pytest.mark.parametrize(
    "dev_env, config_file_exists, expected",
    [
        ("true", True, True),  # forced dev, even on an installed host
        ("1", False, True),
        ("false", False, False),  # forced production: a container, configured by environment
        ("0", True, False),
        ("", True, False),  # unset: production where the installer wrote the config file
        ("", False, True),  # unset: dev everywhere else
    ],
)
def test_dev_mode_detection(monkeypatch, dev_env, config_file_exists, expected):
    monkeypatch.setenv("DEV", dev_env)
    monkeypatch.setattr(cfg.Path, "exists", lambda self: config_file_exists)
    assert cfg._detect_dev_mode() is expected


@pytest.mark.parametrize("euid, prefix", [(0, []), (995, ["sudo"])])
def test_nginx_is_called_through_sudo_unless_already_root(monkeypatch, euid, prefix):
    monkeypatch.setattr("os.geteuid", lambda: euid)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/sbin/nginx")
    assert executor._nginx_cmd("-t") == [*prefix, "/usr/sbin/nginx", "-t"]
    assert executor._nginx_cmd("-s", "reload") == [*prefix, "/usr/sbin/nginx", "-s", "reload"]


@pytest.mark.parametrize("api_key", ["changeme", "dev", ""])
def test_production_mode_refuses_to_start_with_a_default_api_key(monkeypatch, api_key):
    from nanio_orchestrator import cli

    class Settings:
        dev = False
        host = "127.0.0.1"
        port = 8080

    Settings.api_key = api_key
    monkeypatch.setattr("nanio_orchestrator.config.get_settings", lambda: Settings())
    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: pytest.fail("the server must not start"))

    result = CliRunner().invoke(cli.main, ["serve"])

    assert result.exit_code == 1
    assert "NANIO_ORCHESTRATOR_API_KEY is not set" in result.output


# ── Settings ──────────────────────────────────────────────────────────────────


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """No NANIO_ORCHESTRATOR_* variable and no dev.env in reach; the settings singleton reset."""
    import os

    for name in [n for n in os.environ if n.startswith("NANIO_ORCHESTRATOR_")]:
        monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cfg, "settings", None)
    return tmp_path


def test_inline_comments_are_stripped_from_setting_values(clean_env, monkeypatch):
    monkeypatch.setenv("NANIO_ORCHESTRATOR_DRIFT_INTERVAL", "300 # seconds")
    monkeypatch.setenv("NANIO_ORCHESTRATOR_RCLONE_PATH", "/usr/bin/rclone   # the packaged one")

    s = cfg.Settings()

    assert s.drift_interval == 300
    assert s.rclone_path == "/usr/bin/rclone"


def test_settings_are_read_from_the_env_file_of_the_working_directory(clean_env):
    (clean_env / "dev.env").write_text("NANIO_ORCHESTRATOR_PORT=9191  # panel\nNANIO_ORCHESTRATOR_UNKNOWN=ignored\n")

    assert cfg.Settings().port == 9191


def test_settings_reject_input_that_is_not_a_mapping_with_a_validation_error():
    with pytest.raises(ValidationError):
        cfg.Settings.model_validate("port=1")


def test_backup_path_defaults_to_the_database_path_plus_bak(clean_env):
    assert cfg.Settings(db_path="/data/o.db").effective_db_backup_path == "/data/o.db.bak"
    assert cfg.Settings(db_path="/data/o.db", db_backup_path="/backup/o.db").effective_db_backup_path == "/backup/o.db"


def test_ensure_dirs_creates_the_data_nginx_and_migration_state_directories(clean_env):
    s = cfg.Settings(db_path=str(clean_env / "data" / "o.db"), nginx_config_dir=str(clean_env / "nginx"))
    assert (s.pools_dir, s.vhosts_dir) == (clean_env / "nginx" / "pools", clean_env / "nginx" / "vhosts")
    assert s.migrations_dir == clean_env / "data" / "migrations"

    s.ensure_dirs()
    s.ensure_dirs()  # running it again is harmless

    assert s.pools_dir.is_dir() and s.vhosts_dir.is_dir() and s.migrations_dir.is_dir()
    assert not (clean_env / "data" / "o.db").exists()  # directories only


def test_get_settings_loads_once_and_returns_the_same_object(clean_env, monkeypatch):
    monkeypatch.setenv("NANIO_ORCHESTRATOR_PORT", "7001")
    first = cfg.get_settings()
    monkeypatch.setenv("NANIO_ORCHESTRATOR_PORT", "7002")

    assert cfg.get_settings() is first
    assert first.port == 7001


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("300  # seconds", "300"),
        ("300\t# seconds", "300"),
        ("# nothing but a comment", ""),
        ("abc#123", "abc#123"),  # a `#` inside a value is part of it
        ("p@ss#word  # the panel key", "p@ss#word"),
        ("  plain  ", "plain"),
    ],
)
def test_inline_comments_are_stripped_without_truncating_values(raw, expected):
    assert cfg.strip_inline_comment(raw) == expected


def test_api_key_containing_a_hash_is_read_whole_everywhere(tmp_path, monkeypatch):
    """The settings object and the installer's reader must agree, or `doctor` and the service
    would disagree about the key."""
    from nanio_orchestrator import install

    monkeypatch.delenv("NANIO_ORCHESTRATOR_API_KEY", raising=False)  # the environment would win
    env_file = tmp_path / "config.env"
    env_file.write_text("NANIO_ORCHESTRATOR_API_KEY=abc#123\nNANIO_ORCHESTRATOR_PORT=9090  # panel\n")

    assert install.read_env_file(env_file) == {"api_key": "abc#123", "port": "9090"}
    settings = cfg.Settings(_env_file=str(env_file))
    assert settings.api_key == "abc#123" and settings.port == 9090


@pytest.mark.parametrize("name", ["ensure_dirs", "model_config", "pools_dir", "nope"])
def test_config_get_only_answers_for_settings(name):
    from nanio_orchestrator import cli

    result = CliRunner().invoke(cli.main, ["config", "get", name])

    assert result.exit_code == 1
    assert f"Unknown setting: {name}" in result.output


def test_no_asyncio_primitive_is_created_at_import():
    """On Python 3.9 an Event or Lock belongs to the event loop current when it is constructed.
    One created at import is bound to a loop the server never runs: the first wait on it raises
    'attached to a different loop' and the background loop dies. They must be created lazily."""
    import subprocess
    import sys

    check = (
        "import asyncio\n"
        "from nanio_orchestrator import backup, bucket_sync, drift, migration_engine\n"
        "kinds = (asyncio.Event, asyncio.Lock, asyncio.Condition, asyncio.Semaphore)\n"
        "for module in (backup, bucket_sync, drift, migration_engine):\n"
        "    eager = [name for name, value in vars(module).items() if isinstance(value, kinds)]\n"
        "    assert not eager, f'{module.__name__} creates {eager} at import'\n"
    )
    # a fresh interpreter: in this process other tests have already started the loops
    result = subprocess.run([sys.executable, "-c", check], capture_output=True, text=True, env={"DEV": "true"})
    assert result.returncode == 0, result.stderr
