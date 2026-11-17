"""The nginx executor: dry run in dev mode, real subprocess otherwise, and nginx detection."""

from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace

import pytest
from nanio_orchestrator.nginx import executor


class _Proc:
    def __init__(self, returncode, stdout=b"", stderr=b""):
        self.returncode = returncode
        self._out = (stdout, stderr)

    async def communicate(self):
        return self._out


@pytest.fixture
def production(monkeypatch):
    """Settings that are not in dev mode, so commands are really executed."""
    monkeypatch.setattr(executor, "get_settings", lambda: SimpleNamespace(dev=False))


@pytest.fixture
def spawned(monkeypatch):
    """Replace process creation; returns the list of spawned command lines."""
    calls = []
    state = SimpleNamespace(calls=calls, proc=_Proc(0), error=None)

    async def fake_exec(*cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        if state.error:
            raise state.error
        return state.proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return state


async def test_dev_mode_is_a_dry_run_that_spawns_nothing(monkeypatch, spawned):
    monkeypatch.setattr(executor, "get_settings", lambda: SimpleNamespace(dev=True))

    result = await executor._run_cmd(["sudo", "nginx", "-t"])

    assert result == executor.NginxExecResult(ok=True, output="[DRY RUN] sudo nginx -t", return_code=0)
    assert spawned.calls == []


async def test_successful_command_returns_its_combined_output(production, spawned):
    spawned.proc = _Proc(0, stdout=b"out line\n", stderr=b"syntax is ok\n")

    result = await executor._run_cmd(["nginx", "-t"])

    assert result == executor.NginxExecResult(ok=True, output="out line\nsyntax is ok", return_code=0)
    cmd, kwargs = spawned.calls[0]
    assert cmd == ["nginx", "-t"]
    assert kwargs == {"stdout": asyncio.subprocess.PIPE, "stderr": asyncio.subprocess.PIPE}


async def test_failing_command_reports_the_exit_code_and_stderr(production, spawned, caplog):
    spawned.proc = _Proc(1, stderr=b"nginx: [emerg] unexpected '}' in /etc/nginx/nanio/vhosts/a.conf:3\n")

    with caplog.at_level("ERROR", logger=executor.logger.name):
        result = await executor._run_cmd(["nginx", "-t"])

    assert result.ok is False
    assert result.return_code == 1
    assert result.output == "nginx: [emerg] unexpected '}' in /etc/nginx/nanio/vhosts/a.conf:3"
    assert "failed (rc=1)" in caplog.text


async def test_undecodable_output_does_not_break_the_result(production, spawned):
    spawned.proc = _Proc(0, stdout=b"\xff ok")

    result = await executor._run_cmd(["nginx", "-t"])

    assert result.ok is True
    assert result.output == "� ok"


async def test_missing_binary_is_reported_not_raised(production, spawned):
    spawned.error = FileNotFoundError("nginx")

    result = await executor._run_cmd(["sudo", "nginx", "-t"])

    assert result == executor.NginxExecResult(
        ok=False, output="nginx binary not found when running: sudo nginx -t", return_code=-1
    )


async def test_any_other_spawn_error_is_reported_not_raised(production, spawned):
    spawned.error = PermissionError("denied")

    result = await executor._run_cmd(["nginx", "-s", "reload"])

    assert result == executor.NginxExecResult(
        ok=False, output="Error running 'nginx -s reload': denied", return_code=-1
    )


async def test_test_config_and_reload_issue_the_nginx_commands(production, spawned, monkeypatch):
    monkeypatch.setattr("os.geteuid", lambda: 995)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/sbin/nginx")

    assert (await executor.test_config()).ok is True
    assert (await executor.reload_nginx()).ok is True

    assert [cmd for cmd, _ in spawned.calls] == [
        ["sudo", "/usr/sbin/nginx", "-t"],
        ["sudo", "/usr/sbin/nginx", "-s", "reload"],
    ]


def test_nginx_binary_falls_back_to_the_sudoers_path_when_not_on_path(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert executor._nginx_bin() == "/usr/sbin/nginx"

    monkeypatch.setattr("shutil.which", lambda name: "/opt/nginx/sbin/nginx")
    assert executor._nginx_bin() == "/opt/nginx/sbin/nginx"


def test_detect_nginx_reports_not_installed_without_running_anything(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("nothing to run"))

    assert executor.detect_nginx() == {"installed": False, "path": None, "version": None}


@pytest.mark.parametrize(
    "stdout, stderr, version",
    [
        ("", "nginx version: nginx/1.24.0\n", "nginx version: nginx/1.24.0"),  # nginx -v writes to stderr
        ("nginx version: openresty/1.25\n", "", "nginx version: openresty/1.25"),
    ],
)
def test_detect_nginx_reports_path_and_version(monkeypatch, stdout, stderr, version):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout, stderr)

    monkeypatch.setattr("shutil.which", lambda name: "/usr/sbin/nginx")
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert executor.detect_nginx() == {"installed": True, "path": "/usr/sbin/nginx", "version": version}
    assert calls == [(["/usr/sbin/nginx", "-v"], {"capture_output": True, "text": True, "timeout": 5})]


def test_detect_nginx_survives_a_binary_that_cannot_be_run(monkeypatch):
    def broken(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 5)

    monkeypatch.setattr("shutil.which", lambda name: "/usr/sbin/nginx")
    monkeypatch.setattr(subprocess, "run", broken)

    assert executor.detect_nginx() == {"installed": True, "path": "/usr/sbin/nginx", "version": "unknown"}
