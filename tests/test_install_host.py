"""install, doctor and remove against a fake host: every path lives under tmp_path and every
command is recorded instead of executed."""

from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import nanio_orchestrator.db as db_mod
import pytest
from click.testing import CliRunner
from cryptography.fernet import Fernet
from nanio_orchestrator import install
from nanio_orchestrator.cli import main

NGINX = "/usr/sbin/nginx"
USER = install.SERVICE_USER


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class _Response:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Host:
    """The machine the installer believes it runs on."""

    def __init__(self, root: Path, monkeypatch):
        self.root = root
        self.commands: list[list[str]] = []  # run as root
        self.service_commands: list[list[str]] = []  # run as the service user
        self.chowned: dict[str, tuple] = {}
        self.urls: list[str] = []

        # State the fakes answer from; tests change it to describe a broken host.
        self.euid = 0
        self.user_exists = False
        self.systemd = True
        self.service_state = "active"
        self.restart_rc = 0
        self.api_status: object = 200  # a status code, or an exception to raise
        self.nginx: dict = {"installed": True, "path": NGINX, "version": "nginx version: nginx/1.24.0"}
        self.nginx_conf_includes_conf_d = True
        self.nginx_dump_rc = 0
        self.nginx_test = _proc()  # root's `nginx -t`
        self.sudo_nginx_test = _proc()  # the service user's `sudo -n nginx -t`
        self.probe_rc = 0  # can the service user run the interpreter?
        self.unwritable: set[str] = set()
        self.visudo_rc = 0
        self.binaries = {
            "visudo": "/usr/sbin/visudo",
            "systemctl": "/usr/bin/systemctl",
            "rclone": "/usr/bin/rclone",
        }

        etc = root / "etc"
        self.install_dir = root / "opt" / "nanio-orchestrator"
        self.config_file = etc / "nanio-orchestrator" / "config.env"
        self.conf_d = etc / "nginx" / "conf.d"
        self.include_file = self.conf_d / "nanio-orchestrator.conf"
        self.sudoers = etc / "sudoers.d" / "nanio-orchestrator"
        self.unit = etc / "systemd" / "system" / "nanio-orchestrator.service"
        self.bin_link = root / "usr" / "local" / "bin" / "nanio-orchestrator"
        self.db_path = self.install_dir / "data" / "orchestrator.db"
        self.nginx_dir = etc / "nginx" / "nanio"
        self.python = self.install_dir / "venv" / "bin" / "python"
        self.cli = self.python.parent / "nanio-orchestrator"
        for directory in (self.conf_d, self.sudoers.parent, self.unit.parent, self.bin_link.parent, self.python.parent):
            directory.mkdir(parents=True)
        self.cli.write_text("#!/bin/sh\n")

        for name, value in {
            "INSTALL_DIR": self.install_dir,
            "CONFIG_FILE": self.config_file,
            "NGINX_CONF_D": self.conf_d,
            "NGINX_INCLUDE_FILE": self.include_file,
            "SUDOERS_FILE": self.sudoers,
            "UNIT_FILE": self.unit,
            "BIN_LINK": self.bin_link,
            "DEFAULT_DB_PATH": str(self.db_path),
            "DEFAULT_NGINX_DIR": str(self.nginx_dir),
            "_run": self._run,
            "_run_as_service_user": self._run_as_service_user,
            "_systemd_running": lambda: self.systemd,
            "detect_nginx": lambda: dict(self.nginx),
        }.items():
            monkeypatch.setattr(install, name, value)
        monkeypatch.setattr(sys, "executable", str(self.python))
        monkeypatch.setattr(os, "geteuid", lambda: self.euid)
        monkeypatch.setattr(subprocess, "run", self._subprocess_run)
        monkeypatch.setattr(install.shutil, "chown", self._chown)
        monkeypatch.setattr(install.shutil, "which", lambda name: self.binaries.get(name))
        monkeypatch.setattr(install.urllib.request, "urlopen", self._urlopen)
        monkeypatch.setattr(db_mod, "_db_path", None)  # the installer points it at its database

    # ── fakes ─────────────────────────────────────────────────────────────────

    def nginx_dump(self) -> str:
        """What `nginx -T` prints: nginx.conf followed by every file it includes."""
        dump = "# configuration file /etc/nginx/nginx.conf:\nhttp {\n"
        if self.nginx_conf_includes_conf_d:
            dump += "    include /etc/nginx/conf.d/*.conf;\n"
            if self.conf_d.is_dir():
                dump += "".join(f.read_text() for f in sorted(self.conf_d.glob("*.conf")))
        return dump + "}\n"

    def _run(self, cmd, **kwargs):
        cmd = list(cmd)
        self.commands.append(cmd)
        if cmd == ["id", USER]:
            return _proc(0 if self.user_exists else 1)
        if cmd == [self.nginx["path"], "-T"]:
            return _proc(self.nginx_dump_rc, stdout=self.nginx_dump() if self.nginx_dump_rc == 0 else "")
        if cmd == [self.nginx["path"], "-t"]:
            return self.nginx_test
        if cmd[0] == self.binaries["visudo"]:
            return _proc(self.visudo_rc)
        if cmd[:2] == ["systemctl", "is-active"]:
            return _proc(0 if self.service_state == "active" else 3, stdout=self.service_state + "\n")
        if cmd[:2] == ["systemctl", "restart"]:
            return _proc(self.restart_rc)
        if cmd[0] == "userdel":
            self.user_exists = False
        return _proc()

    def _subprocess_run(self, cmd, **kwargs):
        assert cmd[0] == "useradd", f"unexpected real subprocess: {cmd}"
        assert kwargs == {"check": True}
        self.commands.append(list(cmd))
        self.user_exists = True
        return _proc()

    def _run_as_service_user(self, cmd):
        cmd = list(cmd)
        self.service_commands.append(cmd)
        if cmd[1:] == ["-c", "import nanio_orchestrator"]:
            return _proc(self.probe_rc)
        if cmd[:2] == ["test", "-w"]:
            return _proc(1 if cmd[2] in self.unwritable else 0)
        if cmd[:2] == ["sudo", "-n"]:
            return self.sudo_nginx_test
        raise AssertionError(f"unexpected command as the service user: {cmd}")

    def _chown(self, path, user=None, group=None):
        self.chowned[str(path)] = (user, group)

    def _urlopen(self, url, timeout=None):
        self.urls.append(url)
        if isinstance(self.api_status, Exception):
            raise self.api_status
        return _Response(self.api_status)

    # ── helpers ───────────────────────────────────────────────────────────────

    def invoke(self, *args, **kwargs):
        result = CliRunner().invoke(main, list(args), **kwargs)
        assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
        return result

    def systemctl(self) -> list[list[str]]:
        return [cmd[1:] for cmd in self.commands if cmd[0] == "systemctl"]

    def issued(self, *prefix: str) -> bool:
        return any(cmd[: len(prefix)] == list(prefix) for cmd in self.commands)

    def forget_commands(self) -> None:
        self.commands.clear()
        self.service_commands.clear()
        self.chowned.clear()
        self.urls.clear()

    def checks(self) -> dict[str, tuple[str, str]]:
        return {what: (status, fix) for status, what, fix in install.collect_checks()}


@pytest.fixture
def host(tmp_path, monkeypatch):
    return Host(tmp_path, monkeypatch)


@pytest.fixture
def installed(host):
    """A host on which a first install succeeded."""
    result = host.invoke("install")
    assert result.exit_code == 0, result.output
    host.forget_commands()
    return host


class _FakeClock:
    """time.monotonic/time.sleep without waiting: sleeping advances the clock."""

    def __init__(self):
        import time

        self._time = time
        self._real = (time.monotonic, time.sleep)
        self.now = 1000.0
        self.slept: list[float] = []

    def install(self):
        self._time.monotonic = lambda: self.now
        self._time.sleep = self._sleep

    def restore(self):
        self._time.monotonic, self._time.sleep = self._real

    def _sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock():
    fake = _FakeClock()
    fake.install()
    yield fake
    fake.restore()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ── install: a fresh host ─────────────────────────────────────────────────────


def test_fresh_install_creates_user_config_database_and_service(host):
    result = host.invoke("install")

    assert result.exit_code == 0, result.output
    out = result.output
    assert f"Created service user '{USER}'" in out
    assert ["useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin", USER] in host.commands
    assert "nginx detected: nginx version: nginx/1.24.0" in out

    # config file: generated key and secret, shown once, readable by the service group only
    env = install.read_env_file(host.config_file)
    assert len(env["api_key"]) >= 32
    Fernet(env["secret"].encode())
    assert env["db_path"] == str(host.db_path)
    assert env["nginx_config_dir"] == str(host.nginx_dir)
    assert f"API key:  {env['api_key']}" in out
    assert _mode(host.config_file) == 0o660
    assert _mode(host.config_file.parent) == 0o750
    assert host.chowned[str(host.config_file)] == ("root", USER)
    assert host.chowned[str(host.config_file.parent)] == ("root", USER)

    # database and managed nginx directories, owned by the service user
    assert _mode(host.db_path) == 0o600
    assert _mode(host.db_path.parent) == 0o700
    with sqlite3.connect(host.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM pools").fetchone() == (0,)
    conn.close()
    for path in (
        host.db_path,
        host.db_path.parent,
        host.nginx_dir,
        host.nginx_dir / "pools",
        host.nginx_dir / "vhosts",
    ):
        assert host.chowned[str(path)] == (USER, USER)

    # nginx include, validated then reloaded
    assert host.include_file.read_text() == (
        "# Managed by nanio-orchestrator install\n"
        f"include {host.nginx_dir}/pools/*.conf;\n"
        f"include {host.nginx_dir}/vhosts/*.conf;\n"
    )
    assert [NGINX, "-t"] in host.commands
    assert host.commands.index([NGINX, "-s", "reload"]) > host.commands.index([NGINX, "-t"])
    assert f"nginx include installed → {host.include_file}" in out

    # sudoers rule, checked by visudo before it is put in place
    assert host.sudoers.read_text().splitlines()[1] == (
        f"{USER} ALL=(ALL) NOPASSWD: {NGINX} -t, {NGINX} -s reload, /usr/bin/systemctl restart nanio-orchestrator"
    )
    assert _mode(host.sudoers) == 0o440
    assert ["/usr/sbin/visudo", "-cf", f"{host.sudoers}.new"] in host.commands
    assert sorted(p.name for p in host.sudoers.parent.iterdir()) == ["nanio-orchestrator"]

    # systemd unit runs the installing interpreter; service enabled and started
    unit = host.unit.read_text()
    assert f"ExecStart={host.python} -m nanio_orchestrator" in unit
    assert f"EnvironmentFile={host.config_file}" in unit
    assert f"User={USER}" in unit
    assert host.systemctl()[:3] == [
        ["daemon-reload"],
        ["enable", "nanio-orchestrator"],
        ["restart", "nanio-orchestrator"],
    ]
    assert "✓ Service enabled and (re)started" in out
    assert host.urls[0] == "http://127.0.0.1:8080/api/health"

    # the CLI of the private virtualenv is linked onto root's PATH
    assert host.bin_link.is_symlink() and os.readlink(host.bin_link) == str(host.cli)
    assert f"Command available as {host.bin_link}" in out

    assert "Web UI:   http://<this-machine>:8080" in out
    assert "Recheck:  sudo nanio-orchestrator doctor" in out
    assert "some checks failed" not in out
    assert "✗" not in out


def test_install_must_be_run_as_root(host):
    host.euid = 1000

    result = host.invoke("install")

    assert result.exit_code == 1
    assert "must be run as root" in result.output
    assert host.commands == []
    assert not host.config_file.exists()


# ── install: running it again ─────────────────────────────────────────────────


def test_second_install_keeps_config_and_data(installed):
    host = installed
    config_before = host.config_file.read_text()
    with sqlite3.connect(host.db_path) as conn:
        conn.execute("INSERT INTO pools (name) VALUES ('keep-me')")
    conn.close()
    (host.nginx_dir / "pools" / "keep-me.conf").write_text("upstream keep-me {}\n")

    result = host.invoke("install")

    assert result.exit_code == 0, result.output
    assert f"Service user '{USER}' exists" in result.output
    assert not host.issued("useradd")
    assert f"Config kept: {host.config_file}" in result.output
    assert host.config_file.read_text() == config_before
    assert f"API key:  sudo grep API_KEY {host.config_file}" in result.output
    assert install.read_env_file(host.config_file)["api_key"] not in result.output
    with sqlite3.connect(host.db_path) as conn:
        assert conn.execute("SELECT name FROM pools").fetchall() == [("keep-me",)]
    conn.close()
    assert (host.nginx_dir / "pools" / "keep-me.conf").read_text() == "upstream keep-me {}\n"
    assert "nginx already includes the managed config" in result.output
    assert [NGINX, "-s", "reload"] not in host.commands
    assert host.chowned[str(host.nginx_dir / "pools" / "keep-me.conf")] == (USER, USER)
    assert ["restart", "nanio-orchestrator"] in host.systemctl()


def test_install_honours_paths_and_port_of_an_existing_config(host):
    db_path = host.root / "srv" / "data" / "custom.db"
    nginx_dir = host.root / "srv" / "nginx"
    host.config_file.parent.mkdir(parents=True)
    host.config_file.write_text(
        "NANIO_ORCHESTRATOR_API_KEY=a-long-existing-key\n"
        f"NANIO_ORCHESTRATOR_SECRET={Fernet.generate_key().decode()}\n"
        f"NANIO_ORCHESTRATOR_DB_PATH={db_path}\n"
        f"NANIO_ORCHESTRATOR_NGINX_CONFIG_DIR={nginx_dir}\n"
        "NANIO_ORCHESTRATOR_PORT=9443\n"
    )

    result = host.invoke("install")

    assert result.exit_code == 0, result.output
    assert db_path.exists() and not host.db_path.exists()
    assert (nginx_dir / "vhosts").is_dir() and not host.nginx_dir.exists()
    assert f"include {nginx_dir}/pools/*.conf;" in host.include_file.read_text()
    assert f"Database ready: {db_path}" in result.output
    assert host.urls == ["http://127.0.0.1:9443/api/health"] * 2  # wait for the API, then the health check
    assert "Web UI:   http://<this-machine>:9443" in result.output
    assert "✓ API answers on port 9443" in result.output


# ── install: things that go wrong ─────────────────────────────────────────────


def test_install_refuses_a_program_the_service_user_cannot_run(host):
    host.probe_rc = 126  # per-user pipx/uv install under a home directory

    result = host.invoke("install")

    assert result.exit_code == 1
    assert f"✗ '{USER}' cannot run {host.python}" in result.output
    assert "Install it system-wide instead" in result.output
    assert "install.sh | sudo bash" in result.output
    assert host.service_commands == [[str(host.python), "-c", "import nanio_orchestrator"]]
    assert not host.config_file.exists()
    assert not host.unit.exists()
    assert not host.sudoers.exists()


def test_install_without_nginx_installs_the_rest_and_fails_the_checks(host):
    host.nginx = {"installed": False, "path": None, "version": None}

    result = host.invoke("install")

    assert result.exit_code == 1
    assert "✗ nginx not found — install it, then run this command again" in result.output
    assert "✗ nginx include not configured (nginx is not installed)" in result.output
    assert f"include {host.nginx_dir}/pools/*.conf;" in result.output  # the manual instructions
    assert not host.include_file.exists()
    assert host.config_file.exists() and host.unit.exists()
    assert f"NOPASSWD: {NGINX} -t" in host.sudoers.read_text()  # the path nginx will most likely have
    assert "✗ nginx is installed" in result.output
    assert "Installed, but some checks failed" in result.output


def test_install_leaves_nginx_alone_when_its_current_config_is_broken(host):
    host.nginx_dump_rc = 1

    result = host.invoke("install")

    assert result.exit_code == 1
    assert "✗ nginx include not configured: 'nginx -T' fails, fix the nginx config first" in result.output
    assert "Add inside the http {} block of nginx.conf" in result.output
    assert not host.include_file.exists()
    assert [NGINX, "-s", "reload"] not in host.commands


def test_install_does_not_guess_when_there_is_no_conf_d_directory(host):
    host.conf_d.rmdir()

    result = host.invoke("install")

    assert result.exit_code == 1
    assert "✗ nginx include not configured (no conf.d include found in nginx.conf)" in result.output
    assert not host.conf_d.exists()
    assert "✗ nginx includes the managed config" in result.output


def test_install_does_not_write_an_include_nginx_would_never_read(host):
    host.nginx_conf_includes_conf_d = False

    result = host.invoke("install")

    assert result.exit_code == 1
    assert "✗ nginx include not configured (no conf.d include found in nginx.conf)" in result.output
    assert list(host.conf_d.iterdir()) == []


def test_install_removes_an_include_file_that_nginx_rejects(host):
    host.nginx_test = _proc(1, stderr="nginx: [emerg] duplicate upstream in /etc/nginx/nanio/pools/a.conf:1\n")
    host.sudo_nginx_test = host.nginx_test

    result = host.invoke("install")

    assert result.exit_code == 1
    assert "✗ nginx rejected the include file — removed it again" in result.output
    assert "nginx: [emerg] duplicate upstream in /etc/nginx/nanio/pools/a.conf:1" in result.output
    assert not host.include_file.exists()
    assert [NGINX, "-s", "reload"] not in host.commands


def test_install_does_not_install_a_sudoers_rule_visudo_rejects(host):
    host.visudo_rc = 1
    host.sudo_nginx_test = _proc(1, stderr="sudo: a password is required\n")

    result = host.invoke("install")

    assert result.exit_code == 1
    assert "✗ sudoers rule rejected by visudo — not installed" in result.output
    assert list(host.sudoers.parent.iterdir()) == []
    assert host.unit.exists()  # the rest of the install still happens
    assert "✗ Service may run 'nginx -t' through sudo" in result.output


def test_install_without_visudo_and_systemctl_on_path_uses_the_usual_locations(host):
    del host.binaries["systemctl"]
    host.binaries["visudo"] = None

    result = host.invoke("install")

    assert result.exit_code == 0, result.output
    assert "/usr/bin/systemctl restart nanio-orchestrator" in host.sudoers.read_text()
    assert not any("visudo" in cmd[0] for cmd in host.commands)
    assert f"sudoers rule installed → {host.sudoers}" in result.output


def test_install_no_start_installs_the_unit_without_starting_the_service(host):
    host.service_state = "inactive"

    result = host.invoke("install", "--no-start")

    assert host.systemctl() == [["daemon-reload"], ["is-active", "nanio-orchestrator"]]
    assert "Service not started (--no-start). Start it with: systemctl enable --now nanio-orchestrator" in result.output
    assert host.unit.exists()
    assert host.urls == []
    # the checks say the truth: it is not running
    assert "✗ Service is running" in result.output
    assert result.exit_code == 1


def test_install_without_systemd_leaves_starting_to_the_operator(host):
    host.systemd = False

    result = host.invoke("install")

    assert result.exit_code == 0, result.output
    assert "✗ systemd is not running here — start the service yourself" in result.output
    assert host.systemctl() == []
    assert host.unit.exists()
    assert "Service is running" not in result.output


def test_install_reports_a_service_that_does_not_start(host, clock):
    host.restart_rc = 1
    host.service_state = "failed"
    host.api_status = ConnectionRefusedError()

    result = host.invoke("install")

    assert result.exit_code == 1
    assert "✗ Service enabled and (re)started" in result.output
    assert "✗ Service is running" in result.output
    assert "journalctl -u nanio-orchestrator -n 50" in result.output
    assert clock.slept == [0.5] * 30  # waited the full 15 seconds for the API, without really sleeping


@pytest.mark.parametrize("existing", ["absent", "stale-symlink", "dangling-symlink"])
def test_install_links_the_cli_replacing_an_older_link(host, existing):
    if existing == "stale-symlink":
        old = host.root / "old-cli"
        old.write_text("")
        host.bin_link.symlink_to(old)
    elif existing == "dangling-symlink":
        host.bin_link.symlink_to(host.root / "gone")

    host.invoke("install")

    assert os.readlink(host.bin_link) == str(host.cli)


def test_install_never_replaces_a_real_program_at_the_link_location(host):
    host.bin_link.write_text("#!/bin/sh\necho packaged\n")

    result = host.invoke("install")

    assert not host.bin_link.is_symlink()
    assert host.bin_link.read_text() == "#!/bin/sh\necho packaged\n"
    assert "Command available as" not in result.output


def test_install_creates_no_link_when_the_interpreter_has_no_cli_next_to_it(host):
    host.cli.unlink()

    result = host.invoke("install")

    assert not host.bin_link.exists() and not host.bin_link.is_symlink()
    assert "Command available as" not in result.output


def test_install_creates_no_link_when_the_cli_already_is_at_the_link_location(host, monkeypatch):
    monkeypatch.setattr(install, "BIN_LINK", host.cli)

    result = host.invoke("install")

    assert not host.cli.is_symlink()
    assert "Command available as" not in result.output


# ── waiting for the API ───────────────────────────────────────────────────────


def test_wait_for_api_returns_as_soon_as_the_api_answers(host, clock, monkeypatch):
    answers = [ConnectionRefusedError(), ConnectionRefusedError(), 200]

    def urlopen(url, timeout=None):
        host.urls.append((url, timeout))
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return _Response(answer)

    monkeypatch.setattr(install.urllib.request, "urlopen", urlopen)

    install._wait_for_api("8080")

    assert host.urls == [("http://127.0.0.1:8080/api/health", 2)] * 3
    assert clock.slept == [0.5, 0.5]


def test_wait_for_api_gives_up_after_the_deadline(host, clock):
    host.api_status = ConnectionRefusedError()

    install._wait_for_api("9000", seconds=2)

    assert clock.slept == [0.5] * 4
    assert host.urls == ["http://127.0.0.1:9000/api/health"] * 4


# ── doctor ────────────────────────────────────────────────────────────────────


def test_doctor_on_a_healthy_host_passes_every_check(installed):
    host = installed

    result = host.invoke("doctor")

    assert result.exit_code == 0, result.output
    assert "Everything looks good." in result.output
    assert "→" not in result.output
    assert list(host.checks()) == [
        f"Config file {host.config_file}",
        "API key is set",
        "Encryption secret is a valid Fernet key",
        f"Service user '{USER}' exists",
        "Service user can run the program",
        f"{host.db_path.parent} is writable by the service",
        f"{host.nginx_dir}/pools is writable by the service",
        f"{host.nginx_dir}/vhosts is writable by the service",
        "nginx is installed",
        "Service may run 'nginx -t' through sudo",
        "nginx configuration is valid",
        "nginx includes the managed config",
        "rclone available (migrations)",
        "Service is running",
        "API answers on port 8080",
    ]
    assert all(status == install.OK and fix == "" for status, fix in host.checks().values())
    assert ["sudo", "-n", NGINX, "-t"] in host.service_commands


def test_doctor_must_be_run_as_root(installed):
    installed.euid = 1000

    result = installed.invoke("doctor")

    assert result.exit_code == 1
    assert "must be run as root" in result.output
    assert installed.commands == []


def test_doctor_stops_at_a_missing_config_file(host):
    result = host.invoke("doctor")

    assert result.exit_code == 1
    assert f"✗ Config file {host.config_file}" in result.output
    assert "→ Run: nanio-orchestrator install" in result.output
    assert "Problems found — see the arrows above." in result.output
    assert host.checks() == {f"Config file {host.config_file}": (install.FAIL, "Run: nanio-orchestrator install")}
    assert host.commands == []


@pytest.mark.parametrize("api_key", ["", "changeme", "dev"])
def test_doctor_flags_a_default_api_key(installed, api_key):
    from nanio_orchestrator.cli import _set_config_value

    _set_config_value("api_key", api_key, str(installed.config_file))

    assert installed.checks()["API key is set"] == (
        install.FAIL,
        "Run: nanio-orchestrator config set api_key <a long random value>",
    )


@pytest.mark.parametrize(
    "secret_line", ["", "NANIO_ORCHESTRATOR_SECRET=\n", "NANIO_ORCHESTRATOR_SECRET=not-a-fernet-key\n"]
)
def test_doctor_flags_a_missing_or_invalid_secret(installed, secret_line):
    kept = [ln for ln in installed.config_file.read_text().splitlines() if "_SECRET=" not in ln]
    installed.config_file.write_text("\n".join(kept) + "\n" + secret_line)

    status, fix = installed.checks()["Encryption secret is a valid Fernet key"]

    assert status == install.FAIL
    assert fix.startswith("Run: nanio-orchestrator config generate-secret --set")


def test_doctor_stops_at_a_missing_service_user(installed):
    installed.user_exists = False

    checks = installed.checks()

    assert checks[f"Service user '{USER}' exists"] == (install.FAIL, "Run: nanio-orchestrator install")
    assert list(checks)[-1] == f"Service user '{USER}' exists"
    assert installed.service_commands == []  # nothing can be run as a user that does not exist


def test_doctor_flags_an_interpreter_the_service_user_cannot_run(installed):
    installed.probe_rc = 1

    status, fix = installed.checks()["Service user can run the program"]

    assert status == install.FAIL
    assert (
        fix == f"{installed.python} is not usable by '{USER}'. Install under {installed.install_dir} with install.sh."
    )


def test_doctor_flags_directories_the_service_cannot_write(installed):
    host = installed
    host.unwritable.add(str(host.db_path.parent))
    (host.nginx_dir / "vhosts").rmdir()

    checks = host.checks()

    assert checks[f"{host.db_path.parent} is writable by the service"] == (
        install.FAIL,
        f"Run: chown -R {USER}: {host.db_path.parent}",
    )
    assert checks[f"{host.nginx_dir}/vhosts is writable by the service"][0] == install.FAIL
    assert checks[f"{host.nginx_dir}/pools is writable by the service"] == (install.OK, "")
    # a directory that does not exist is not probed
    assert ["test", "-w", str(host.nginx_dir / "vhosts")] not in host.service_commands


def test_doctor_flags_missing_nginx_and_skips_the_nginx_checks(installed):
    installed.nginx = {"installed": False, "path": None, "version": None}

    checks = installed.checks()

    assert checks["nginx is installed"] == (install.FAIL, "Install nginx (apt install nginx / dnf install nginx)")
    assert "Service may run 'nginx -t' through sudo" not in checks
    assert "nginx includes the managed config" not in checks
    assert not any(cmd[0] == "sudo" for cmd in installed.service_commands)


@pytest.mark.parametrize(
    "stderr",
    ["sudo: a password is required\n", f"Sorry, user {USER} is not allowed to execute '/usr/sbin/nginx -t' as root.\n"],
)
def test_doctor_flags_a_missing_sudoers_rule(installed, stderr):
    installed.sudo_nginx_test = _proc(1, stderr=stderr)

    checks = installed.checks()

    assert checks["Service may run 'nginx -t' through sudo"] == (install.FAIL, "Run: nanio-orchestrator install")
    assert "nginx configuration is valid" not in checks  # unknown: nginx -t never ran


def test_doctor_quotes_the_last_line_of_a_failing_nginx_test(installed):
    installed.sudo_nginx_test = _proc(
        1,
        stderr='nginx: [emerg] unknown directive "foo" in /etc/nginx/nginx.conf:3\n'
        "nginx: configuration file /etc/nginx/nginx.conf test failed\n",
    )

    assert installed.checks()["nginx configuration is valid"] == (
        install.FAIL,
        "nginx -t says: nginx: configuration file /etc/nginx/nginx.conf test failed",
    )


def test_doctor_flags_a_failing_nginx_test_that_printed_nothing(installed):
    installed.sudo_nginx_test = _proc(1)

    assert installed.checks()["nginx configuration is valid"] == (install.FAIL, "nginx -t says: ")


@pytest.mark.parametrize("broken", ["include-removed", "nginx-T-fails"])
def test_doctor_flags_managed_config_that_nginx_does_not_include(installed, broken):
    if broken == "include-removed":
        installed.include_file.unlink()
    else:
        installed.nginx_dump_rc = 1

    assert installed.checks()["nginx includes the managed config"] == (
        install.FAIL,
        "Add to the http {} block of nginx.conf: "
        f"include {installed.nginx_dir}/pools/*.conf; include {installed.nginx_dir}/vhosts/*.conf;",
    )


def test_doctor_only_warns_about_missing_rclone(installed):
    del installed.binaries["rclone"]

    result = installed.invoke("doctor")

    assert installed.checks()["rclone available (migrations)"] == (
        install.WARN,
        "rclone not found. Only migrations need it: install rclone",
    )
    assert "! rclone available (migrations)" in result.output
    assert result.exit_code == 0  # a warning is not a failure
    assert "Everything looks good." in result.output


def test_doctor_looks_for_the_configured_rclone_binary(installed):
    from nanio_orchestrator.cli import _set_config_value

    _set_config_value("rclone_path", "/opt/rclone/rclone", str(installed.config_file))
    assert installed.checks()["rclone available (migrations)"][0] == install.WARN

    installed.binaries["/opt/rclone/rclone"] = "/opt/rclone/rclone"
    assert installed.checks()["rclone available (migrations)"] == (install.OK, "")


def test_doctor_skips_the_service_checks_without_systemd(installed):
    installed.systemd = False

    checks = installed.checks()

    assert "Service is running" not in checks
    assert installed.systemctl() == []
    assert installed.urls == []


def test_doctor_flags_a_stopped_service_without_calling_its_api(installed):
    installed.service_state = "inactive"

    checks = installed.checks()

    assert checks["Service is running"] == (
        install.FAIL,
        "Run: systemctl enable --now nanio-orchestrator; journalctl -u nanio-orchestrator -n 50",
    )
    assert not any(what.startswith("API answers") for what in checks)
    assert installed.urls == []


@pytest.mark.parametrize("api_status", [503, ConnectionRefusedError()])
def test_doctor_flags_an_api_that_does_not_answer(installed, api_status):
    installed.api_status = api_status

    result = installed.invoke("doctor")

    assert installed.checks()["API answers on port 8080"] == (
        install.FAIL,
        "Run: journalctl -u nanio-orchestrator -n 50",
    )
    assert result.exit_code == 1
    assert "✗ API answers on port 8080" in result.output
    assert "Problems found — see the arrows above." in result.output


# ── remove ────────────────────────────────────────────────────────────────────


def _present(host) -> dict[str, bool]:
    return {
        "unit": host.unit.exists(),
        "sudoers": host.sudoers.exists(),
        "bin_link": host.bin_link.is_symlink(),
        "include": host.include_file.exists(),
        "nginx_dir": host.nginx_dir.exists(),
        "data": host.db_path.exists(),
        "config": host.config_file.exists(),
        "install_dir": host.install_dir.exists(),
    }


def test_remove_keeps_nginx_config_database_and_settings(installed):
    host = installed

    result = host.invoke("remove", "--yes")

    assert result.exit_code == 0
    assert _present(host) == {
        "unit": False,
        "sudoers": False,
        "bin_link": False,
        "include": True,
        "nginx_dir": True,
        "data": True,
        "config": True,
        "install_dir": True,
    }
    assert host.systemctl() == [["disable", "--now", "nanio-orchestrator"], ["daemon-reload"]]
    assert host.commands[-1] == ["userdel", USER]
    assert f"Stopped and removed {host.unit}" in result.output
    assert f"Removed {host.sudoers}" in result.output
    assert f"Removed {host.bin_link}" in result.output
    assert f"Removed service user '{USER}'" in result.output
    assert "Removed. nginx config, database and settings were kept (use --purge to delete)." in result.output
    assert "Continue?" not in result.output


def test_remove_purge_deletes_everything(installed):
    host = installed

    result = host.invoke("remove", "--purge", "-y")

    assert result.exit_code == 0
    assert not any(_present(host).values())
    assert not host.config_file.parent.exists()
    for path in (host.include_file, host.nginx_dir, host.db_path.parent, host.config_file.parent, host.install_dir):
        assert f"Removed {path}" in result.output
    assert result.output.rstrip().endswith("Removed.")
    assert host.conf_d.is_dir()  # nginx's own directory is not ours to delete


def test_remove_purge_follows_the_configured_paths(host):
    db_path = host.root / "srv" / "data" / "custom.db"
    nginx_dir = host.root / "srv" / "nginx"
    host.config_file.parent.mkdir(parents=True)
    host.config_file.write_text(
        f"NANIO_ORCHESTRATOR_DB_PATH={db_path}\nNANIO_ORCHESTRATOR_NGINX_CONFIG_DIR={nginx_dir}\n"
    )
    db_path.parent.mkdir(parents=True)
    db_path.write_text("")

    result = host.invoke("remove", "--purge", "-y")

    assert result.exit_code == 0
    assert not db_path.parent.exists()
    assert not host.config_file.parent.exists()
    # nothing else was installed: nothing else is reported or attempted
    assert host.systemctl() == []
    assert not host.issued("userdel")
    assert "Stopped and removed" not in result.output
    assert f"Removed {nginx_dir}" not in result.output
    assert "Removed service user" not in result.output


def test_remove_asks_first_and_does_nothing_when_declined(installed):
    host = installed
    before = _present(host)

    result = host.invoke("remove", input="n\n")

    assert result.exit_code == 0
    assert "Removes the service, its systemd unit, sudoers rule and service user." in result.output
    assert f"Kept: {host.nginx_dir} (nginx keeps serving), the database and the config file." in result.output
    assert "Continue? [y/N]" in result.output
    assert "Aborted." in result.output
    assert _present(host) == before
    assert host.commands == []


@pytest.mark.parametrize("answer", ["y", "YES", " yes "])
def test_remove_proceeds_when_confirmed(installed, answer):
    result = installed.invoke("remove", input=f"{answer}\n")

    assert result.exit_code == 0
    assert not installed.unit.exists()
    assert installed.nginx_dir.exists()
    assert "Aborted." not in result.output


def test_remove_purge_spells_out_what_will_be_deleted_before_asking(installed):
    host = installed

    result = host.invoke("remove", "--purge", input="\n")  # just Enter: the default is no

    assert result.exit_code == 0
    assert "--purge ALSO deletes, permanently:" in result.output
    assert f"{host.nginx_dir}  (the gateway's nginx config" in result.output
    assert f"{host.db_path.parent}, {host.config_file.parent}, {host.install_dir}" in result.output
    assert "Aborted." in result.output
    assert all(_present(host).values())


def test_remove_must_be_run_as_root(installed):
    installed.euid = 1000

    result = installed.invoke("remove", "--purge", "-y")

    assert result.exit_code == 1
    assert "must be run as root" in result.output
    assert all(_present(installed).values())


# ── the real helpers behind the fakes ─────────────────────────────────────────


@pytest.fixture
def real_subprocess(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return _proc(0, stdout="ok")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_run_captures_text_output(real_subprocess):
    result = install._run(["id", "someone"], timeout=3)

    assert result.stdout == "ok"
    assert real_subprocess == [(["id", "someone"], {"capture_output": True, "text": True, "timeout": 3})]


def test_user_exists_asks_id(monkeypatch):
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        return _proc(1 if len(seen) == 1 else 0)

    monkeypatch.setattr(install, "_run", fake_run)

    assert install._user_exists() is False
    assert install._user_exists() is True
    assert seen == [["id", USER]] * 2


def test_commands_for_the_service_user_drop_to_its_user_group_and_no_extra_groups(real_subprocess):
    result = install._run_as_service_user(["test", "-w", "/x"])

    assert result.returncode == 0
    assert real_subprocess == [
        (
            ["test", "-w", "/x"],
            {
                "capture_output": True,
                "text": True,
                "user": USER,
                "group": USER,
                "extra_groups": [],
                "cwd": "/",
                "timeout": 30,
            },
        )
    ]


@pytest.mark.parametrize(
    "error",
    [KeyError(f"getpwnam(): name not found: '{USER}'"), PermissionError("denied"), subprocess.TimeoutExpired("x", 30)],
)
def test_a_command_that_cannot_be_run_as_the_service_user_counts_as_failed(monkeypatch, error):
    def failing(cmd, **kwargs):
        raise error

    monkeypatch.setattr(subprocess, "run", failing)

    result = install._run_as_service_user(["test", "-w", "/x"])

    assert result.returncode == 1
    assert result.stderr == str(error)
    assert result.args == ["test", "-w", "/x"]


@pytest.mark.parametrize(
    "runtime_dir, systemctl, expected",
    [(True, "/usr/bin/systemctl", True), (True, None, False), (False, "/usr/bin/systemctl", False)],
)
def test_systemd_is_running_only_with_its_runtime_dir_and_systemctl(monkeypatch, runtime_dir, systemctl, expected):
    asked = []

    def is_dir(self):
        asked.append(str(self))
        return runtime_dir

    monkeypatch.setattr(install.Path, "is_dir", is_dir)
    monkeypatch.setattr(install.shutil, "which", lambda name: systemctl)

    assert install._systemd_running() is expected
    assert asked == ["/run/systemd/system"]


def test_chown_tree_gives_every_file_and_directory_to_the_service_user(tmp_path, monkeypatch):
    (tmp_path / "pools").mkdir()
    (tmp_path / "pools" / "a.conf").write_text("")
    (tmp_path / "top.json").write_text("")
    chowned = {}
    monkeypatch.setattr(install.shutil, "chown", lambda path, user, group: chowned.update({str(path): (user, group)}))

    install._chown_tree(tmp_path)

    assert chowned == {
        str(p): (USER, USER)
        for p in (tmp_path, tmp_path / "pools", tmp_path / "pools" / "a.conf", tmp_path / "top.json")
    }


def test_step_marks_success_and_failure(capsys):
    install._step("done")
    install._step("broken", ok=False)

    assert capsys.readouterr().out == "  ✓ done\n  ✗ broken\n"
