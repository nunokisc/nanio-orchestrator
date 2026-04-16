"""Production install logic for nanio-orchestrator.

Creates directories, config file, systemd unit, and initializes DB.
Runs as root.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from nanio_orchestrator.nginx.executor import detect_nginx


CONFIG_ENV_CONTENT = """\
# nanio-orchestrator configuration
NANIO_ORCHESTRATOR_HOST=0.0.0.0
NANIO_ORCHESTRATOR_PORT=8080
NANIO_ORCHESTRATOR_API_KEY=changeme
NANIO_ORCHESTRATOR_DB_PATH=/opt/nanio-orchestrator/data/orchestrator.db
NANIO_ORCHESTRATOR_NGINX_CONFIG_DIR=/etc/nginx/nanio
NANIO_ORCHESTRATOR_LOG_LEVEL=info
NANIO_ORCHESTRATOR_DRIFT_INTERVAL=60
NANIO_ORCHESTRATOR_SESSION_TTL=28800
"""

SYSTEMD_UNIT = """\
[Unit]
Description=nanio-orchestrator nginx config manager
After=network.target

[Service]
Type=simple
User=root
EnvironmentFile=/etc/nanio-orchestrator/config.env
ExecStart=/opt/nanio-orchestrator/venv/bin/python -m nanio_orchestrator
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

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

    # 2. Detect nginx
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
    _step(f"Created {data_dir}")

    # 4. Create config directory and write config.env
    config_dir = Path("/etc/nanio-orchestrator")
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "config.env"
    if config_file.exists():
        _step(f"Config exists at {config_file} (not overwritten)")
    else:
        config_file.write_text(CONFIG_ENV_CONTENT)
        _step(f"Created {config_file}")

    # 5. Create nginx config directories
    pools_dir = Path("/etc/nginx/nanio/pools")
    vhosts_dir = Path("/etc/nginx/nanio/vhosts")
    pools_dir.mkdir(parents=True, exist_ok=True)
    vhosts_dir.mkdir(parents=True, exist_ok=True)
    _step(f"Created {pools_dir.parent}/{{pools,vhosts}}")

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
    _step(f"Initialized database at {db_path}")

    # 8. Print next steps
    print()
    print(NEXT_STEPS)
