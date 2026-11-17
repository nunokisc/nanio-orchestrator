"""Production install, health checks (``doctor``) and removal. Runs as root.

``install`` is idempotent: running it again upgrades the unit and sudoers rule,
keeps the existing configuration and data, and restarts the service.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Optional

from nanio_orchestrator.config import strip_inline_comment
from nanio_orchestrator.nginx.executor import detect_nginx

SERVICE = "nanio-orchestrator"
SERVICE_USER = "nanio-orchestrator"

INSTALL_DIR = Path("/opt/nanio-orchestrator")
CONFIG_FILE = Path("/etc/nanio-orchestrator/config.env")
NGINX_CONF_D = Path("/etc/nginx/conf.d")
NGINX_INCLUDE_FILE = NGINX_CONF_D / "nanio-orchestrator.conf"
SUDOERS_FILE = Path("/etc/sudoers.d/nanio-orchestrator")
UNIT_FILE = Path("/etc/systemd/system/nanio-orchestrator.service")
BIN_LINK = Path("/usr/local/bin/nanio-orchestrator")

DEFAULT_DB_PATH = str(INSTALL_DIR / "data" / "orchestrator.db")
DEFAULT_NGINX_DIR = "/etc/nginx/nanio"
ENV_PREFIX = "NANIO_ORCHESTRATOR_"

CONFIG_ENV_CONTENT = """\
# nanio-orchestrator configuration — restart the service after editing:
#   systemctl restart nanio-orchestrator
# Every setting is described in docs/configuration.md

NANIO_ORCHESTRATOR_HOST=0.0.0.0
NANIO_ORCHESTRATOR_PORT=8080

# Key for the Web UI login and the X-Orchestrator-Key API header
NANIO_ORCHESTRATOR_API_KEY={api_key}

# Fernet key that encrypts the stored pool credentials. Keep a copy somewhere safe:
# without it the stored credentials cannot be decrypted.
NANIO_ORCHESTRATOR_SECRET={secret}

NANIO_ORCHESTRATOR_DB_PATH={db_path}
NANIO_ORCHESTRATOR_NGINX_CONFIG_DIR={nginx_dir}
NANIO_ORCHESTRATOR_LOG_LEVEL=info

# rclone binary used by migrations
NANIO_ORCHESTRATOR_RCLONE_PATH=rclone
"""

SYSTEMD_UNIT = """\
[Unit]
Description=nanio-orchestrator nginx config manager
After=network.target

[Service]
Type=simple
User={user}
Group={user}
EnvironmentFile={config_file}
ExecStart={exec_start}
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""

OK, WARN, FAIL = "ok", "warn", "fail"
_SYMBOL = {OK: "✓", WARN: "!", FAIL: "✗"}
Check = tuple[str, str, str]  # (status, what, how to fix)


def _step(msg: str, ok: bool = True) -> None:
    print(f"  {'✓' if ok else '✗'} {msg}")


def _require_root() -> None:
    if os.geteuid() != 0:
        print("  ✗ This command must be run as root (sudo).\n")
        sys.exit(1)


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def _run_as_service_user(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a command exactly as the service would: its user, its group, no extra groups."""
    try:
        return _run(cmd, user=SERVICE_USER, group=SERVICE_USER, extra_groups=[], cwd="/", timeout=30)
    except (OSError, KeyError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(cmd, 1, "", str(exc))


def _user_exists() -> bool:
    return _run(["id", SERVICE_USER]).returncode == 0


def _systemd_running() -> bool:
    return Path("/run/systemd/system").is_dir() and shutil.which("systemctl") is not None


def read_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=value lines of the config file (short keys, prefix stripped)."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith(ENV_PREFIX):
            values[key[len(ENV_PREFIX) :].lower()] = strip_inline_comment(value)
    return values


def _paths(env: dict[str, str]) -> tuple[Path, Path]:
    """(database file, nginx managed dir) as configured."""
    return Path(env.get("db_path") or DEFAULT_DB_PATH), Path(env.get("nginx_config_dir") or DEFAULT_NGINX_DIR)


def _chown_tree(path: Path) -> None:
    for root, dirs, files in os.walk(path):
        for name in [root] + [os.path.join(root, n) for n in dirs + files]:
            shutil.chown(name, user=SERVICE_USER, group=SERVICE_USER)


def _program_command() -> list[str]:
    """The command systemd runs: this very interpreter, wherever the package was installed."""
    return [sys.executable, "-m", "nanio_orchestrator"]


# ── nginx include ─────────────────────────────────────────────────────────────


def _include_lines(nginx_dir: Path) -> str:
    return f"include {nginx_dir}/pools/*.conf;\ninclude {nginx_dir}/vhosts/*.conf;\n"


def nginx_includes_managed_dirs(nginx_bin: str, nginx_dir: Path) -> Optional[bool]:
    """True/False when nginx's full config does/does not include the managed dirs.

    None when ``nginx -T`` fails (nginx missing or its current config is invalid).
    """
    result = _run([nginx_bin, "-T"])
    if result.returncode != 0:
        return None
    active = [ln.strip() for ln in result.stdout.splitlines() if not ln.strip().startswith("#")]
    return all(
        any(ln.startswith("include") and f"{nginx_dir}/{sub}/" in ln for ln in active) for sub in ("pools", "vhosts")
    )


def _setup_nginx_include(nginx_bin: Optional[str], nginx_dir: Path) -> None:
    manual = "  Add inside the http {} block of nginx.conf, then reload nginx:\n" + "".join(
        f"      {ln}\n" for ln in _include_lines(nginx_dir).splitlines()
    )
    if not nginx_bin:
        _step("nginx include not configured (nginx is not installed)", ok=False)
        print(manual)
        return

    included = nginx_includes_managed_dirs(nginx_bin, nginx_dir)
    if included:
        _step("nginx already includes the managed config")
        return
    if included is None:
        _step("nginx include not configured: 'nginx -T' fails, fix the nginx config first", ok=False)
        print(manual)
        return

    dump = _run([nginx_bin, "-T"]).stdout
    if not NGINX_CONF_D.is_dir() or "conf.d/*.conf" not in dump:
        _step("nginx include not configured (no conf.d include found in nginx.conf)", ok=False)
        print(manual)
        return

    NGINX_INCLUDE_FILE.write_text("# Managed by nanio-orchestrator install\n" + _include_lines(nginx_dir))
    test = _run([nginx_bin, "-t"])
    if test.returncode != 0:
        NGINX_INCLUDE_FILE.unlink()
        _step("nginx rejected the include file — removed it again", ok=False)
        print(f"    {test.stderr.strip()}\n{manual}")
        return
    _run([nginx_bin, "-s", "reload"])  # no-op for traffic unless managed configs already exist
    _step(f"nginx include installed → {NGINX_INCLUDE_FILE}")


# ── sudoers ───────────────────────────────────────────────────────────────────


def _sudoers_content(nginx_path: str, systemctl_path: str = "/usr/bin/systemctl") -> str:
    return (
        "# Allow nanio-orchestrator to validate/reload nginx and restart itself without a password\n"
        f"{SERVICE_USER} ALL=(ALL) NOPASSWD: {nginx_path} -t, {nginx_path} -s reload,"
        f" {systemctl_path} restart {SERVICE}\n"
    )


def _install_sudoers(content: str) -> bool:
    """Write the drop-in only if visudo accepts it: a broken sudoers file locks sudo for everyone."""
    candidate = SUDOERS_FILE.with_name(SUDOERS_FILE.name + ".new")  # names with a dot are ignored by sudo
    candidate.write_text(content)
    candidate.chmod(0o440)
    visudo = shutil.which("visudo")
    if visudo and _run([visudo, "-cf", str(candidate)]).returncode != 0:
        candidate.unlink()
        return False
    os.replace(candidate, SUDOERS_FILE)
    return True


# ── doctor ────────────────────────────────────────────────────────────────────


def collect_checks() -> list[Check]:
    """Everything that must be true for the service to work, each with its fix."""
    checks: list[Check] = []

    def add(ok: bool, what: str, fix: str = "", level: str = FAIL) -> bool:
        checks.append((OK if ok else level, what, "" if ok else fix))
        return ok

    if not add(CONFIG_FILE.exists(), f"Config file {CONFIG_FILE}", "Run: nanio-orchestrator install"):
        return checks
    env = read_env_file(CONFIG_FILE)
    db_path, nginx_dir = _paths(env)

    add(
        env.get("api_key", "") not in ("", "changeme", "dev"),
        "API key is set",
        "Run: nanio-orchestrator config set api_key <a long random value>",
    )

    secret_ok = False
    if env.get("secret"):
        try:
            from cryptography.fernet import Fernet

            Fernet(env["secret"].encode())
            secret_ok = True
        except Exception:
            pass
    add(
        secret_ok,
        "Encryption secret is a valid Fernet key",
        "Run: nanio-orchestrator config generate-secret --set  (needed to store pool credentials)",
    )

    if not add(_user_exists(), f"Service user '{SERVICE_USER}' exists", "Run: nanio-orchestrator install"):
        return checks

    probe = _run_as_service_user([*_program_command()[:1], "-c", "import nanio_orchestrator"])
    add(
        probe.returncode == 0,
        "Service user can run the program",
        f"{sys.executable} is not usable by '{SERVICE_USER}'. Install under {INSTALL_DIR} with install.sh.",
    )

    for path in (db_path.parent, nginx_dir / "pools", nginx_dir / "vhosts"):
        writable = path.is_dir() and _run_as_service_user(["test", "-w", str(path)]).returncode == 0
        add(writable, f"{path} is writable by the service", f"Run: chown -R {SERVICE_USER}: {path}")

    nginx = detect_nginx()
    if add(nginx["installed"], "nginx is installed", "Install nginx (apt install nginx / dnf install nginx)"):
        test = _run_as_service_user(["sudo", "-n", nginx["path"], "-t"])
        sudo_denied = "password is required" in test.stderr or "not allowed" in test.stderr
        if add(not sudo_denied, "Service may run 'nginx -t' through sudo", "Run: nanio-orchestrator install"):
            detail = test.stderr.strip().splitlines()[-1] if test.stderr.strip() else ""
            add(test.returncode == 0, "nginx configuration is valid", f"nginx -t says: {detail}")
        included = nginx_includes_managed_dirs(nginx["path"], nginx_dir)
        add(
            bool(included),
            "nginx includes the managed config",
            "Add to the http {} block of nginx.conf: " + _include_lines(nginx_dir).replace("\n", " ").strip(),
        )

    rclone = env.get("rclone_path") or "rclone"
    add(
        shutil.which(rclone) is not None,
        "rclone available (migrations)",
        "rclone not found. Only migrations need it: install rclone",
        WARN,
    )

    if _systemd_running():
        active = _run(["systemctl", "is-active", SERVICE]).stdout.strip() == "active"
        if add(active, "Service is running", f"Run: systemctl enable --now {SERVICE}; journalctl -u {SERVICE} -n 50"):
            port = env.get("port") or "8080"
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5) as resp:
                    answered = resp.status == 200
            except Exception:
                answered = False
            add(answered, f"API answers on port {port}", f"Run: journalctl -u {SERVICE} -n 50")
    return checks


def print_checks(checks: list[Check]) -> bool:
    """Print the checks; return False if any failed."""
    for status, what, fix in checks:
        print(f"  {_SYMBOL[status]} {what}")
        if fix:
            print(f"      → {fix}")
    return all(status != FAIL for status, _, _ in checks)


def run_doctor() -> None:
    print("nanio-orchestrator doctor\n")
    _require_root()
    ok = print_checks(collect_checks())
    print("\n  Everything looks good." if ok else "\n  Problems found — see the arrows above.")
    sys.exit(0 if ok else 1)


# ── install ───────────────────────────────────────────────────────────────────


def run_install(start: bool = True) -> None:
    """Install or upgrade in place. Never overwrites an existing config or database."""
    print("nanio-orchestrator install\n")
    _require_root()

    if _user_exists():
        _step(f"Service user '{SERVICE_USER}' exists")
    else:
        subprocess.run(
            ["useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin", SERVICE_USER], check=True
        )
        _step(f"Created service user '{SERVICE_USER}'")

    # The service must be able to run this interpreter. A per-user install (pipx, uv tool)
    # lives under a home directory the service user cannot enter.
    probe = _run_as_service_user([*_program_command()[:1], "-c", "import nanio_orchestrator"])
    if probe.returncode != 0:
        _step(f"'{SERVICE_USER}' cannot run {sys.executable}", ok=False)
        print(
            "\n  nanio-orchestrator is installed somewhere the service user cannot reach\n"
            "  (typically a per-user pipx/uv install). Install it system-wide instead:\n\n"
            "    curl -fsSL https://raw.githubusercontent.com/nunokisc/nanio-orchestrator/main/install.sh | sudo bash\n"
        )
        sys.exit(1)

    nginx_info = detect_nginx()
    if nginx_info["installed"]:
        _step(f"nginx detected: {nginx_info['version']}")
    else:
        _step("nginx not found — install it, then run this command again", ok=False)

    # Config file (kept if present)
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    shutil.chown(CONFIG_FILE.parent, user="root", group=SERVICE_USER)
    CONFIG_FILE.parent.chmod(0o750)
    new_api_key = None
    if CONFIG_FILE.exists():
        _step(f"Config kept: {CONFIG_FILE}")
    else:
        from cryptography.fernet import Fernet

        new_api_key = secrets.token_urlsafe(32)
        CONFIG_FILE.touch(mode=0o660)
        CONFIG_FILE.write_text(
            CONFIG_ENV_CONTENT.format(
                api_key=new_api_key,
                secret=Fernet.generate_key().decode(),
                db_path=DEFAULT_DB_PATH,
                nginx_dir=DEFAULT_NGINX_DIR,
            )
        )
        _step(f"Created {CONFIG_FILE} (API key and encryption secret generated)")
    shutil.chown(CONFIG_FILE, user="root", group=SERVICE_USER)
    CONFIG_FILE.chmod(0o660)  # the service user edits it through `config set` / the settings page

    env = read_env_file(CONFIG_FILE)
    db_path, nginx_dir = _paths(env)

    # Data and managed nginx directories, owned by the service user
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.parent.chmod(0o700)
    for sub in ("pools", "vhosts"):
        (nginx_dir / sub).mkdir(parents=True, exist_ok=True)

    from nanio_orchestrator.db import init_db_sync, set_db_path

    set_db_path(str(db_path))
    init_db_sync()
    db_path.chmod(0o600)
    _chown_tree(db_path.parent)
    _chown_tree(nginx_dir)
    _step(f"Database ready: {db_path}")
    _step(f"Managed nginx config: {nginx_dir}/{{pools,vhosts}}")

    _setup_nginx_include(nginx_info.get("path"), nginx_dir)

    nginx_bin = nginx_info.get("path") or "/usr/sbin/nginx"
    systemctl_bin = shutil.which("systemctl") or "/usr/bin/systemctl"
    if _install_sudoers(_sudoers_content(nginx_bin, systemctl_bin)):
        _step(f"sudoers rule installed → {SUDOERS_FILE}")
    else:
        _step("sudoers rule rejected by visudo — not installed", ok=False)

    UNIT_FILE.write_text(
        SYSTEMD_UNIT.format(user=SERVICE_USER, config_file=CONFIG_FILE, exec_start=" ".join(_program_command()))
    )
    _step(f"systemd unit installed → {UNIT_FILE}")

    # Make the CLI reachable for root/sudo when it lives in a private virtualenv
    cli = Path(sys.executable).parent / "nanio-orchestrator"
    if cli.exists() and cli != BIN_LINK and (BIN_LINK.is_symlink() or not BIN_LINK.exists()):
        BIN_LINK.unlink(missing_ok=True)
        BIN_LINK.symlink_to(cli)
        _step(f"Command available as {BIN_LINK}")

    if not _systemd_running():
        _step("systemd is not running here — start the service yourself", ok=False)
    elif start:
        _run(["systemctl", "daemon-reload"])
        _run(["systemctl", "enable", SERVICE])
        restarted = _run(["systemctl", "restart", SERVICE])
        _step("Service enabled and (re)started", ok=restarted.returncode == 0)
        _wait_for_api(env.get("port") or "8080")
    else:
        _run(["systemctl", "daemon-reload"])
        _step(f"Service not started (--no-start). Start it with: systemctl enable --now {SERVICE}")

    print("\nChecks\n")
    ok = print_checks(collect_checks())

    port = env.get("port") or "8080"
    print(f"\n  Web UI:   http://<this-machine>:{port}")
    if new_api_key:
        print(f"  API key:  {new_api_key}")
    else:
        print(f"  API key:  sudo grep API_KEY {CONFIG_FILE}")
    print(f"  Config:   {CONFIG_FILE}")
    print("  Recheck:  sudo nanio-orchestrator doctor\n")
    if not ok:
        print("  Installed, but some checks failed — see the arrows above.\n")
        sys.exit(1)


def _wait_for_api(port: str, seconds: int = 15) -> None:
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2):
                return
        except Exception:
            time.sleep(0.5)


# ── remove ────────────────────────────────────────────────────────────────────


def run_remove(purge: bool = False, yes: bool = False) -> None:
    """Remove the service. The gateway keeps serving unless ``purge`` is given."""
    print("nanio-orchestrator remove\n")
    _require_root()

    env = read_env_file(CONFIG_FILE)
    db_path, nginx_dir = _paths(env)

    if not yes:
        print("  Removes the service, its systemd unit, sudoers rule and service user.")
        if purge:
            print(
                "  --purge ALSO deletes, permanently:\n"
                f"    {nginx_dir}  (the gateway's nginx config: nginx stops serving these\n"
                "                       vhosts at its next reload)\n"
                f"    {db_path.parent}, {CONFIG_FILE.parent}, {INSTALL_DIR}"
            )
        else:
            print(f"  Kept: {nginx_dir} (nginx keeps serving), the database and the config file.")
        if input("\n  Continue? [y/N] ").strip().lower() not in ("y", "yes"):
            print("  Aborted.")
            sys.exit(0)
        print()

    if UNIT_FILE.exists():
        _run(["systemctl", "disable", "--now", SERVICE])
        UNIT_FILE.unlink()
        _run(["systemctl", "daemon-reload"])
        _step(f"Stopped and removed {UNIT_FILE}")

    for path in (SUDOERS_FILE, BIN_LINK):
        if path.is_symlink() or path.exists():
            path.unlink()
            _step(f"Removed {path}")

    if purge:
        for path in (NGINX_INCLUDE_FILE,):
            if path.exists():
                path.unlink()
                _step(f"Removed {path}")
        for path in (nginx_dir, db_path.parent, CONFIG_FILE.parent, INSTALL_DIR):
            if path.exists():
                shutil.rmtree(path)
                _step(f"Removed {path}")

    if _user_exists():
        _run(["userdel", SERVICE_USER])
        _step(f"Removed service user '{SERVICE_USER}'")

    print(
        "\n  Removed."
        if purge
        else "\n  Removed. nginx config, database and settings were kept (use --purge to delete)."
    )
