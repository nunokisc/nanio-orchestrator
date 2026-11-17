"""Application settings via pydantic-settings. Supports env vars + config file."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_INLINE_COMMENT_RE = re.compile(r"(^|\s)#.*$")


def strip_inline_comment(value: str) -> str:
    """`300  # seconds` → `300`. A `#` starts a comment only at the start of the value or after
    whitespace, so a `#` inside a value (an API key such as `abc#123`) is kept."""
    return _INLINE_COMMENT_RE.sub("", value).strip()


def _detect_dev_mode() -> bool:
    """Dev mode: nginx is never called and data lives under ./dev-data.

    DEV=true forces it, DEV=false forces production mode (containers are configured through
    environment variables and have no config file). With DEV unset, production mode is
    assumed only where the installer has written the config file.
    """
    dev = os.environ.get("DEV", "").lower()
    if dev in ("1", "true", "yes"):
        return True
    if dev in ("0", "false", "no"):
        return False
    return not Path("/etc/nanio-orchestrator/config.env").exists()


DEV_MODE = _detect_dev_mode()


class Settings(BaseSettings):
    """All configuration knobs for nanio-orchestrator."""

    model_config = SettingsConfigDict(
        env_prefix="NANIO_ORCHESTRATOR_",
        env_file="/etc/nanio-orchestrator/config.env" if not DEV_MODE else "dev.env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8080
    api_key: str = "dev" if DEV_MODE else "changeme"
    db_path: str = str(Path("dev-data/orchestrator.db")) if DEV_MODE else "/opt/nanio-orchestrator/data/orchestrator.db"
    nginx_config_dir: str = str(Path("dev-data/nginx")) if DEV_MODE else "/etc/nginx/nanio"
    log_level: str = "info"
    drift_interval: int = 60
    session_ttl: int = 28800  # seconds; default 8 hours
    bucket_sync_interval: int = 300  # seconds; default 5 minutes
    secret: Optional[str] = None  # Fernet key for credential encryption
    rclone_path: str = "rclone"
    migration_max_parallel: int = 2
    migration_bandwidth_limit: str = ""  # rclone --bwlimit value, e.g. "50M"
    migration_checkers: int = 8
    migration_transfers: int = 4
    migration_max_copy_passes: int = 10  # max convergence loop passes before entering write_routing
    s3_request_timeout: int = 3600  # seconds; socket timeout for S3 HTTP requests (large for big file transfers)
    log_file: Optional[str] = None  # path to rotating log file; None = log to stdout only
    db_backup_path: Optional[str] = None  # auto-derived from db_path if None
    db_backup_interval: int = 300  # seconds between timed backups
    db_backup_rotate: int = 3  # keep N backup copies
    dev: bool = DEV_MODE

    @model_validator(mode="before")
    @classmethod
    def strip_inline_comments(cls, values: dict) -> dict:
        """Strip inline shell-style comments from env file values (e.g. '300 # seconds' → '300')."""
        if isinstance(values, dict):
            return {k: strip_inline_comment(v) if isinstance(v, str) else v for k, v in values.items()}
        return values

    @property
    def effective_db_backup_path(self) -> str:
        """Return the backup path, defaulting to db_path + '.bak'."""
        if self.db_backup_path:
            return self.db_backup_path
        return self.db_path + ".bak"

    @property
    def pools_dir(self) -> Path:
        return Path(self.nginx_config_dir) / "pools"

    @property
    def vhosts_dir(self) -> Path:
        return Path(self.nginx_config_dir) / "vhosts"

    @property
    def migrations_dir(self) -> Path:
        """State files for in-progress migrations — stored alongside the DB, not in nginx config."""
        return Path(self.db_path).parent / "migrations"

    def ensure_dirs(self) -> None:
        """Create all required directories."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.pools_dir.mkdir(parents=True, exist_ok=True)
        self.vhosts_dir.mkdir(parents=True, exist_ok=True)
        self.migrations_dir.mkdir(parents=True, exist_ok=True)


# Singleton — import this everywhere
settings: Optional[Settings] = None


def get_settings() -> Settings:
    global settings
    if settings is None:
        settings = Settings()
    return settings
