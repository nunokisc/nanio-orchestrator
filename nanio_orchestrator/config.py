"""Application settings via pydantic-settings. Supports env vars + config file."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


def _detect_dev_mode() -> bool:
    """Dev mode when DEV=true, or dev.env exists in cwd, or no prod config file."""
    if os.environ.get("DEV", "").lower() in ("1", "true", "yes"):
        return True
    if Path("dev.env").exists():
        return True
    if not Path("/etc/nanio-orchestrator/config.env").exists():
        return True
    return False


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
    db_path: str = (
        str(Path("dev-data/orchestrator.db")) if DEV_MODE else "/opt/nanio-orchestrator/data/orchestrator.db"
    )
    nginx_config_dir: str = str(Path("dev-data/nginx")) if DEV_MODE else "/etc/nginx/nanio"
    log_level: str = "info"
    drift_interval: int = 60
    session_ttl: int = 28800  # seconds; default 8 hours
    bucket_sync_interval: int = 300  # seconds; default 5 minutes
    s3_access_key: Optional[str] = None
    s3_secret_key: Optional[str] = None
    secret: Optional[str] = None  # Fernet key for credential encryption
    s3_proxy_port: int = 8081
    rclone_path: str = "rclone"
    migration_max_parallel: int = 2
    migration_bandwidth_limit: str = ""  # rclone --bwlimit value, e.g. "50M"
    migration_checkers: int = 8
    migration_transfers: int = 4
    dev: bool = DEV_MODE

    @property
    def pools_dir(self) -> Path:
        return Path(self.nginx_config_dir) / "pools"

    @property
    def vhosts_dir(self) -> Path:
        return Path(self.nginx_config_dir) / "vhosts"

    def ensure_dirs(self) -> None:
        """Create all required directories."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.pools_dir.mkdir(parents=True, exist_ok=True)
        self.vhosts_dir.mkdir(parents=True, exist_ok=True)


# Singleton — import this everywhere
settings: Optional[Settings] = None


def get_settings() -> Settings:
    global settings
    if settings is None:
        settings = Settings()
    return settings
