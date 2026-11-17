"""How the process decides between dev and production mode, and how it calls nginx."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from nanio_orchestrator import config as cfg
from nanio_orchestrator.nginx import executor


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
