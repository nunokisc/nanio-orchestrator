"""Installer helpers: the parts that can be checked without root."""

from __future__ import annotations

import subprocess

from click.testing import CliRunner

from nanio_orchestrator import install
from nanio_orchestrator.cli import main


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_read_env_file_strips_prefix_and_comments(tmp_path):
    f = tmp_path / "config.env"
    f.write_text(
        "# comment\n"
        "NANIO_ORCHESTRATOR_PORT=9090  # inline\n"
        "NANIO_ORCHESTRATOR_DB_PATH=/data/o.db\n"
        "# NANIO_ORCHESTRATOR_SECRET=ignored\n"
        "OTHER=1\n"
    )
    assert install.read_env_file(f) == {"port": "9090", "db_path": "/data/o.db"}
    assert install.read_env_file(tmp_path / "missing") == {}


def test_generated_config_is_complete(tmp_path):
    f = tmp_path / "config.env"
    f.write_text(install.CONFIG_ENV_CONTENT.format(api_key="k", secret="s", db_path="/d/o.db", nginx_dir="/n"))
    env = install.read_env_file(f)
    assert env["api_key"] == "k" and env["secret"] == "s"
    assert install._paths(env) == (install.Path("/d/o.db"), install.Path("/n"))


def test_unit_runs_the_installing_interpreter():
    unit = install.SYSTEMD_UNIT.format(
        user=install.SERVICE_USER, config_file=install.CONFIG_FILE, exec_start=" ".join(install._program_command())
    )
    assert f"ExecStart={install.sys.executable} -m nanio_orchestrator" in unit
    assert "User=nanio-orchestrator" in unit


def test_nginx_include_detection(monkeypatch, tmp_path):
    nginx_dir = tmp_path / "nanio"
    dump = f"http {{\n    include {nginx_dir}/pools/*.conf;\n    include {nginx_dir}/vhosts/*.conf;\n}}\n"
    monkeypatch.setattr(install, "_run", lambda cmd, **kw: _proc(stdout=dump))
    assert install.nginx_includes_managed_dirs("nginx", nginx_dir) is True

    commented = dump.replace(f"    include {nginx_dir}/vhosts", f"    # include {nginx_dir}/vhosts")
    monkeypatch.setattr(install, "_run", lambda cmd, **kw: _proc(stdout=commented))
    assert install.nginx_includes_managed_dirs("nginx", nginx_dir) is False

    monkeypatch.setattr(install, "_run", lambda cmd, **kw: _proc(returncode=1))
    assert install.nginx_includes_managed_dirs("nginx", nginx_dir) is None


def test_sudoers_rejected_by_visudo_is_not_installed(monkeypatch, tmp_path):
    target = tmp_path / "nanio-orchestrator"
    monkeypatch.setattr(install, "SUDOERS_FILE", target)
    monkeypatch.setattr(install.shutil, "which", lambda name: "/usr/sbin/visudo")

    monkeypatch.setattr(install, "_run", lambda cmd, **kw: _proc(returncode=1))
    assert install._install_sudoers("broken") is False
    assert list(tmp_path.iterdir()) == []

    monkeypatch.setattr(install, "_run", lambda cmd, **kw: _proc())
    content = install._sudoers_content("/usr/sbin/nginx")
    assert install._install_sudoers(content) is True
    assert target.read_text() == content
    assert "NOPASSWD: /usr/sbin/nginx -t, /usr/sbin/nginx -s reload" in content


def test_print_checks_fails_only_on_fail(capsys):
    assert install.print_checks([(install.OK, "a", ""), (install.WARN, "b", "hint")]) is True
    assert install.print_checks([(install.FAIL, "c", "do this")]) is False
    assert "→ do this" in capsys.readouterr().out


def test_root_only_commands_refuse_without_root(monkeypatch):
    monkeypatch.setattr(install.os, "geteuid", lambda: 1000)
    for cmd in (["install"], ["doctor"], ["remove", "-y"]):
        result = CliRunner().invoke(main, cmd)
        assert result.exit_code == 1
        assert "must be run as root" in result.output
