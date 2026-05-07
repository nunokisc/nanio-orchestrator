"""Production install logic for nanio-orchestrator.

Creates directories, config file, systemd unit, and initializes DB.
Runs as root.
"""

from __future__ import annotations

import os
import secrets
import shutil
import sys
from pathlib import Path

from nanio_orchestrator.nginx.executor import detect_nginx


CONFIG_ENV_CONTENT = """\
# nanio-orchestrator configuration
NANIO_ORCHESTRATOR_HOST=0.0.0.0
NANIO_ORCHESTRATOR_PORT=8080
NANIO_ORCHESTRATOR_API_KEY={generated_api_key}
NANIO_ORCHESTRATOR_DB_PATH=/opt/nanio-orchestrator/data/orchestrator.db
NANIO_ORCHESTRATOR_NGINX_CONFIG_DIR=/etc/nginx/nanio
NANIO_ORCHESTRATOR_LOG_LEVEL=info
NANIO_ORCHESTRATOR_DRIFT_INTERVAL=60
NANIO_ORCHESTRATOR_SESSION_TTL=28800
NANIO_ORCHESTRATOR_BUCKET_SYNC_INTERVAL=300
# Fernet key for encrypting pool credentials (required for credential storage).
# Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# NANIO_ORCHESTRATOR_SECRET=
# rclone binary path (for migrations)
NANIO_ORCHESTRATOR_RCLONE_PATH=rclone
# Migration limits
NANIO_ORCHESTRATOR_MIGRATION_MAX_PARALLEL=2
NANIO_ORCHESTRATOR_MIGRATION_BANDWIDTH_LIMIT=
NANIO_ORCHESTRATOR_MIGRATION_CHECKERS=8
NANIO_ORCHESTRATOR_MIGRATION_TRANSFERS=4
# S3 credentials for polling nanio-default (optional; leave empty if no auth)
# NANIO_ORCHESTRATOR_S3_ACCESS_KEY=
# NANIO_ORCHESTRATOR_S3_SECRET_KEY=
# Database backup settings
NANIO_ORCHESTRATOR_DB_BACKUP_PATH=/opt/nanio-orchestrator/data/orchestrator.db.bak
NANIO_ORCHESTRATOR_DB_BACKUP_INTERVAL=300
NANIO_ORCHESTRATOR_DB_BACKUP_ROTATE=3
"""

def _sudoers_content(nginx_path: str, systemctl_path: str = "/usr/bin/systemctl") -> str:
    return (
        "# Allow nanio-orchestrator to validate/reload nginx and restart itself without a password\n"
        f"nanio-orchestrator ALL=(ALL) NOPASSWD: {nginx_path} -t, {nginx_path} -s reload,"
        f" {systemctl_path} restart nanio-orchestrator\n"
    )

SYSTEMD_UNIT = """\
[Unit]
Description=nanio-orchestrator nginx config manager
After=network.target

[Service]
Type=simple
User=nanio-orchestrator
Group=nanio-orchestrator
EnvironmentFile=/etc/nanio-orchestrator/config.env
ExecStart=/opt/nanio-orchestrator/venv/bin/python -m nanio_orchestrator
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
# Allow nginx -t and nginx -s reload via sudo
AmbientCapabilities=

[Install]
WantedBy=multi-user.target
"""

NEXT_STEPS = """\
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 nanio-orchestrator installed successfully
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Edit config:
    nano /etc/nanio-orchestrator/config.env

  Add to your nginx.conf:
    include /etc/nginx/nanio/pools/*.conf;
    include /etc/nginx/nanio/vhosts/*.conf;

  Enable and start:
    systemctl daemon-reload
    systemctl enable --now nanio-orchestrator

  Open Web UI:
    http://<this-machine-ip>:8080

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""


def _step(msg: str, ok: bool = True) -> None:
    symbol = "✓" if ok else "✗"
    print(f"  {symbol} {msg}")


def run_install() -> None:
    """Execute the full install sequence."""
    print("nanio-orchestrator install\n")

    # 1. Check root
    if os.geteuid() != 0:
        _step("Running as root", ok=False)
        print("\n  This command must be run as root (sudo).\n")
        sys.exit(1)
    _step("Running as root")

    # 2. Create service user
    import subprocess
    try:
        subprocess.run(
            ["id", "nanio-orchestrator"],
            check=True, capture_output=True,
        )
        _step("Service user 'nanio-orchestrator' exists")
    except subprocess.CalledProcessError:
        subprocess.run(
            ["useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin",
             "nanio-orchestrator"],
            check=True,
        )
        _step("Created service user 'nanio-orchestrator'")

    # 3. Detect nginx
    nginx_info = detect_nginx()
    if nginx_info["installed"]:
        _step(f"nginx detected: {nginx_info['version']}")
    else:
        _step("nginx not found (install it before using the orchestrator)", ok=False)
        print("    WARNING: nginx is not installed. The orchestrator can still be")
        print("    configured, but config apply/reload won't work until nginx is installed.\n")

    # 3. Create data directory
    data_dir = Path("/opt/nanio-orchestrator/data")
    data_dir.mkdir(parents=True, exist_ok=True)
    shutil.chown(data_dir, user="nanio-orchestrator", group="nanio-orchestrator")
    data_dir.chmod(0o700)  # only the service user may read/write the DB
    _step(f"Created {data_dir}")

    # 4. Create config directory and write config.env
    config_dir = Path("/etc/nanio-orchestrator")
    config_dir.mkdir(parents=True, exist_ok=True)
    # root owns the dir; group read/execute so the service user can enter it
    shutil.chown(config_dir, user="root", group="nanio-orchestrator")
    config_dir.chmod(0o750)
    config_file = config_dir / "config.env"
    if config_file.exists():
        _step(f"Config exists at {config_file} (not overwritten)")
    else:
        generated_api_key = secrets.token_urlsafe(32)
        config_content = CONFIG_ENV_CONTENT.replace(
            "{generated_api_key}", generated_api_key
        )
        config_file.write_text(config_content)
        _step(f"Created {config_file}")
        print(f"\n  ⚠  API key generated: {generated_api_key}")
        print("     Save this key — it will not be shown again.\n")
    # Ensure config is readable and writable by root and the service user (contains secrets)
    shutil.chown(config_file, user="root", group="nanio-orchestrator")
    config_file.chmod(0o660)

    # 5. Create nginx config directories
    pools_dir = Path("/etc/nginx/nanio/pools")
    vhosts_dir = Path("/etc/nginx/nanio/vhosts")
    pools_dir.mkdir(parents=True, exist_ok=True)
    vhosts_dir.mkdir(parents=True, exist_ok=True)
    for _d in (pools_dir.parent, pools_dir, vhosts_dir):
        shutil.chown(_d, user="nanio-orchestrator", group="nanio-orchestrator")
    _step(f"Created {pools_dir.parent}/{{pools,vhosts}}")

    # 5b. Install sudoers drop-in for nginx commands
    # Use the detected nginx path so the rule matches at runtime; fall back to
    # the most common location if nginx was not found at install time.
    nginx_bin = nginx_info.get("path") or "/usr/sbin/nginx"
    systemctl_bin = shutil.which("systemctl") or "/usr/bin/systemctl"
    sudoers_path = Path("/etc/sudoers.d/nanio-orchestrator")
    sudoers_path.write_text(_sudoers_content(nginx_bin, systemctl_bin))
    sudoers_path.chmod(0o440)
    _step(f"Installed sudoers drop-in → {sudoers_path} (nginx: {nginx_bin}, systemctl: {systemctl_bin})")

    # 6. Install systemd unit
    unit_path = Path("/etc/systemd/system/nanio-orchestrator.service")

    # Detect the actual python/binary path for ExecStart
    venv_python = Path("/opt/nanio-orchestrator/venv/bin/python")
    if venv_python.exists():
        exec_start = f"{venv_python} -m nanio_orchestrator"
    else:
        # Detect from current running python
        current_bin = shutil.which("nanio-orchestrator")
        if current_bin:
            exec_start = f"{current_bin} serve"
        else:
            exec_start = f"{sys.executable} -m nanio_orchestrator"

    unit_content = SYSTEMD_UNIT.replace(
        "ExecStart=/opt/nanio-orchestrator/venv/bin/python -m nanio_orchestrator",
        f"ExecStart={exec_start}",
    )
    unit_path.write_text(unit_content)
    _step(f"Installed systemd unit → {unit_path}")

    # 7. Initialize database
    from nanio_orchestrator.db import set_db_path, init_db_sync
    db_path = "/opt/nanio-orchestrator/data/orchestrator.db"
    set_db_path(db_path)
    init_db_sync()
    shutil.chown(db_path, user="nanio-orchestrator", group="nanio-orchestrator")
    Path(db_path).chmod(0o600)  # only the service user may read/write the DB file
    _step(f"Initialized database at {db_path}")

    # 8. Print next steps
    print()
    print(NEXT_STEPS)


REMOVE_STEPS = """\
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 nanio-orchestrator removed successfully
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  If you also want to delete data and config, run:
    nanio-orchestrator remove --purge

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

REMOVE_PURGE_STEPS = """\
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 nanio-orchestrator purged successfully
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""


def run_remove(purge: bool = False, yes: bool = False) -> None:
    """Remove nanio-orchestrator from the system."""
    import subprocess

    print("nanio-orchestrator remove\n")

    if os.geteuid() != 0:
        _step("Running as root", ok=False)
        print("\n  This command must be run as root (sudo).\n")
        sys.exit(1)
    _step("Running as root")

    if not yes:
        msg = (
            "This will remove the nanio-orchestrator service, systemd unit, sudoers drop-in,\n"
            "  nginx nanio config directories, and service user.\n"
        )
        if purge:
            msg += (
                "  --purge: data (/opt/nanio-orchestrator) and config (/etc/nanio-orchestrator)\n"
                "  will also be permanently deleted.\n"
            )
        print(f"  {msg}")
        answer = input("  Continue? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("  Aborted.")
            sys.exit(0)
        print()

    # 1. Stop and disable systemd service
    unit_path = Path("/etc/systemd/system/nanio-orchestrator.service")
    if unit_path.exists():
        for cmd in (
            ["systemctl", "stop", "nanio-orchestrator"],
            ["systemctl", "disable", "nanio-orchestrator"],
        ):
            subprocess.run(cmd, capture_output=True)
        unit_path.unlink()
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
        _step(f"Stopped, disabled, and removed {unit_path}")
    else:
        _step(f"Systemd unit not found (skipped): {unit_path}")

    # 2. Remove sudoers drop-in
    sudoers_path = Path("/etc/sudoers.d/nanio-orchestrator")
    if sudoers_path.exists():
        sudoers_path.unlink()
        _step(f"Removed sudoers drop-in: {sudoers_path}")
    else:
        _step(f"Sudoers drop-in not found (skipped): {sudoers_path}")

    # 3. Remove nginx nanio config directories
    nanio_nginx_dir = Path("/etc/nginx/nanio")
    if nanio_nginx_dir.exists():
        shutil.rmtree(nanio_nginx_dir)
        _step(f"Removed nginx config dir: {nanio_nginx_dir}")
    else:
        _step(f"Nginx config dir not found (skipped): {nanio_nginx_dir}")

    # 4. Remove service user
    try:
        subprocess.run(["id", "nanio-orchestrator"], check=True, capture_output=True)
        subprocess.run(["userdel", "nanio-orchestrator"], check=True, capture_output=True)
        _step("Removed service user 'nanio-orchestrator'")
    except subprocess.CalledProcessError:
        _step("Service user 'nanio-orchestrator' not found (skipped)")

    # 5. Purge data and config (only with --purge)
    if purge:
        data_dir = Path("/opt/nanio-orchestrator")
        if data_dir.exists():
            shutil.rmtree(data_dir)
            _step(f"Removed data directory: {data_dir}")
        else:
            _step(f"Data directory not found (skipped): {data_dir}")

        config_dir = Path("/etc/nanio-orchestrator")
        if config_dir.exists():
            shutil.rmtree(config_dir)
            _step(f"Removed config directory: {config_dir}")
        else:
            _step(f"Config directory not found (skipped): {config_dir}")

    print()
    if purge:
        print(REMOVE_PURGE_STEPS)
    else:
        print(REMOVE_STEPS)
