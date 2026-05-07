"""Pydantic request/response models for the REST API."""

from __future__ import annotations

import re
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

# Patterns for nginx-safe values
_PATH_PREFIX_RE = re.compile(r"^/[A-Za-z0-9._/@-]+/?$")
_SERVER_NAME_RE = re.compile(r"^(\*\.)?[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?$")
_NGINX_DANGEROUS_RE = re.compile(r"[;{}\\]")
_IP_ENTRY_RE = re.compile(r"^[\d:a-fA-F./]+$")  # IPv4, IPv6, CIDR notation


# ── Pool ──────────────────────────────────────────────────────────────────────


class PoolCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9._-]+$")
    description: Optional[str] = None
    type: str = Field("nanio", pattern=r"^(nanio|http)$")
    lb_method: str = Field("least_conn", pattern=r"^(round_robin|least_conn|ip_hash)$")
    keepalive: int = Field(32, ge=0, le=1024)


class PoolUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9._-]+$")
    description: Optional[str] = None
    type: Optional[str] = Field(None, pattern=r"^(nanio|http)$")
    lb_method: Optional[str] = Field(None, pattern=r"^(round_robin|least_conn|ip_hash)$")
    keepalive: Optional[int] = Field(None, ge=0, le=1024)


class PoolOut(BaseModel):
    id: int
    name: str
    description: Optional[str]
    type: str
    lb_method: str
    keepalive: int
    created_at: str
    updated_at: str


# ── Pool Member ───────────────────────────────────────────────────────────────


class MemberCreate(BaseModel):
    address: str = Field(..., min_length=1)
    role: str = Field("active", pattern=r"^(active|primary|replica)$")
    weight: int = Field(1, ge=1, le=100)
    max_fails: int = Field(3, ge=0, le=100)
    fail_timeout_s: int = Field(30, ge=0, le=600)
    enabled: bool = True

    @field_validator("address")
    @classmethod
    def validate_address(cls, v: str) -> str:
        v = v.strip()
        if v.startswith("unix:"):
            return v
        if ":" not in v:
            raise ValueError("address must be host:port or unix:/path")
        return v


class MemberUpdate(BaseModel):
    address: Optional[str] = None
    role: Optional[str] = Field(None, pattern=r"^(active|primary|replica)$")
    weight: Optional[int] = Field(None, ge=1, le=100)
    max_fails: Optional[int] = Field(None, ge=0, le=100)
    fail_timeout_s: Optional[int] = Field(None, ge=0, le=600)
    enabled: Optional[bool] = None


class MemberOut(BaseModel):
    id: int
    pool_id: int
    address: str
    role: str
    weight: int
    max_fails: int
    fail_timeout_s: int
    enabled: bool
    created_at: str
    updated_at: str


# ── Vhost Extra Block ─────────────────────────────────────────────────────────


class VhostExtraBlock(BaseModel):
    """A freeform nginx config block injected at a specific zone of a vhost."""

    zone: Literal["top", "ssl", "proxy", "end"] = Field(
        ...,
        description=(
            "Where to inject: 'top' after server_name, 'ssl' after SSL certs,"
            " 'proxy' after proxy directives, 'end' before closing brace"
        ),
    )
    content: str = Field(..., min_length=1)


# ── Vhost ─────────────────────────────────────────────────────────────────────


class VhostCreate(BaseModel):
    server_name: str = Field(..., min_length=1, max_length=253)
    listen_port: int = Field(443, ge=1, le=65535)
    ssl: bool = True
    ssl_cert_path: Optional[str] = None
    ssl_key_path: Optional[str] = None
    extra_directives: Optional[str] = None
    extra_blocks: Optional[List[VhostExtraBlock]] = None
    enabled: bool = True
    default_pool_id: Optional[int] = None
    ip_rule_mode: Optional[Literal["allow", "deny"]] = None
    ip_rule_ips: Optional[List[str]] = None

    @field_validator("server_name")
    @classmethod
    def validate_server_name(cls, v: str) -> str:
        v = v.strip()
        if not _SERVER_NAME_RE.match(v):
            raise ValueError(
                "server_name must be a valid hostname or wildcard (e.g. *.example.com); "
                "characters ; { } \\ are not allowed"
            )
        return v

    @model_validator(mode="after")
    def require_ssl_certs_when_ssl(self) -> "VhostCreate":
        if self.ssl and (not self.ssl_cert_path or not self.ssl_key_path):
            raise ValueError("ssl_cert_path and ssl_key_path are required when ssl is enabled")
        if self.ip_rule_mode and not self.ip_rule_ips:
            raise ValueError("ip_rule_ips must be provided when ip_rule_mode is set")
        if self.ip_rule_ips:
            for ip in self.ip_rule_ips:
                if not _IP_ENTRY_RE.match(ip.strip()):
                    raise ValueError(f"Invalid IP/CIDR entry: {ip!r}")
        return self


class VhostUpdate(BaseModel):
    server_name: Optional[str] = Field(None, min_length=1, max_length=253)
    listen_port: Optional[int] = Field(None, ge=1, le=65535)
    ssl: Optional[bool] = None
    ssl_cert_path: Optional[str] = None
    ssl_key_path: Optional[str] = None
    extra_directives: Optional[str] = None
    extra_blocks: Optional[List[VhostExtraBlock]] = None
    enabled: Optional[bool] = None
    default_pool_id: Optional[int] = None
    ip_rule_mode: Optional[Literal["allow", "deny"]] = None
    ip_rule_ips: Optional[List[str]] = None

    @field_validator("server_name")
    @classmethod
    def validate_server_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not _SERVER_NAME_RE.match(v):
            raise ValueError(
                "server_name must be a valid hostname or wildcard (e.g. *.example.com); "
                "characters ; { } \\ are not allowed"
            )
        return v

    @model_validator(mode="after")
    def validate_ip_rules(self) -> "VhostUpdate":
        if self.ip_rule_mode and not self.ip_rule_ips:
            raise ValueError("ip_rule_ips must be provided when ip_rule_mode is set")
        if self.ip_rule_ips:
            for ip in self.ip_rule_ips:
                if not _IP_ENTRY_RE.match(ip.strip()):
                    raise ValueError(f"Invalid IP/CIDR entry: {ip!r}")
        return self


class VhostOut(BaseModel):
    id: int
    server_name: str
    listen_port: int
    ssl: bool
    ssl_cert_path: Optional[str]
    ssl_key_path: Optional[str]
    extra_directives: Optional[str]
    extra_blocks: Optional[List[VhostExtraBlock]] = None
    enabled: bool
    default_pool_id: Optional[int] = None
    ip_rule_mode: Optional[Literal["allow", "deny"]] = None
    ip_rule_ips: Optional[List[str]] = None
    created_at: str
    updated_at: str


# ── Route ─────────────────────────────────────────────────────────────────────


class RouteCreate(BaseModel):
    path_prefix: str = Field(..., min_length=1)
    pool_id: int
    key_prefix: Optional[str] = None
    extra_directives: Optional[str] = None
    enabled: bool = True

    @field_validator("path_prefix")
    @classmethod
    def validate_prefix(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError("path_prefix must start with /")
        if not _PATH_PREFIX_RE.match(v):
            raise ValueError(
                "path_prefix must match /[A-Za-z0-9._/@-]+/? — characters ; { } \\ and spaces are not allowed"
            )
        return v

    @field_validator("extra_directives")
    @classmethod
    def validate_extra_directives(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and _NGINX_DANGEROUS_RE.search(v):
            raise ValueError(
                "extra_directives contains dangerous characters (; { } \\). "
                "Use the config rebuild API for advanced directives."
            )
        return v


class RouteUpdate(BaseModel):
    path_prefix: Optional[str] = None
    pool_id: Optional[int] = None
    key_prefix: Optional[str] = None
    extra_directives: Optional[str] = None
    enabled: Optional[bool] = None

    @field_validator("path_prefix")
    @classmethod
    def validate_prefix(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not v.startswith("/"):
            raise ValueError("path_prefix must start with /")
        if not _PATH_PREFIX_RE.match(v):
            raise ValueError(
                "path_prefix must match /[A-Za-z0-9._/@-]+/? — characters ; { } \\ and spaces are not allowed"
            )
        return v

    @field_validator("extra_directives")
    @classmethod
    def validate_extra_directives(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and _NGINX_DANGEROUS_RE.search(v):
            raise ValueError(
                "extra_directives contains dangerous characters (; { } \\). "
                "Use the config rebuild API for advanced directives."
            )
        return v


class RouteOut(BaseModel):
    id: int
    vhost_id: int
    path_prefix: str
    pool_id: int
    pool_name: Optional[str] = None
    key_prefix: Optional[str] = None
    extra_directives: Optional[str]
    enabled: bool
    created_at: str
    updated_at: str
    migration_id: Optional[int] = None


# ── Bucket Sync ────────────────────────────────────────────────────────────────


class BucketEntry(BaseModel):
    name: str
    status: str  # unrouted | routed | migrating | ignored
    pool_name: Optional[str] = None
    routed_pool_id: Optional[int] = None
    object_count: Optional[int] = None
    discovered_at: Optional[str] = None


class BucketListOut(BaseModel):
    vhost_id: int
    buckets: List[BucketEntry]
    last_synced_at: Optional[str] = None


class BucketPromoteRequest(BaseModel):
    pool_id: int
    migrate: bool = False
    allow_orphan: bool = False  # allow routing to a different pool without migration (data stays on source)


# ── Pool Credentials ───────────────────────────────────────────────────────────


class CredentialSet(BaseModel):
    access_key: str = Field(..., min_length=1)
    secret_key: str = Field(..., min_length=1)
    endpoint_url: Optional[str] = None
    region: str = Field("us-east-1")


class CredentialOut(BaseModel):
    pool_id: int
    access_key_masked: str
    endpoint_url: Optional[str]
    region: str
    source: str = "pool"  # "pool" | "global"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# ── rclone Migration ──────────────────────────────────────────────────────────


class RcloneMigrationCreate(BaseModel):
    bucket: str = Field(..., min_length=1)
    src_pool_id: int
    dst_pool_id: int
    mode: str = Field("copy", pattern=r"^copy$")


class RcloneMigrationOut(BaseModel):
    id: int
    vhost_id: int
    bucket: str
    src_pool_id: int
    dst_pool_id: int
    mode: str = "copy"
    phase: str
    objects_total: int
    objects_done: int
    bytes_total: int
    bytes_done: int
    error_msg: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    created_at: str
    orphaned_source_pool_id: Optional[int] = None
    orphaned_source_prefix: Optional[str] = None
    orphaned_at: Optional[str] = None


class OrphanedMigrationOut(BaseModel):
    migration_id: int
    bucket: str
    src_pool_id: int
    dst_pool_id: int
    orphaned_source_pool_id: int
    orphaned_source_prefix: str
    orphaned_at: str
    finished_at: Optional[str] = None


class StaleMigrationOut(BaseModel):
    """An active migration that cannot proceed safely.

    reason values:
      src_no_members     — source pool has no enabled members
      dst_no_members     — destination pool has no enabled members
      src_bucket_missing — source bucket no longer exists on the source pool
    """

    migration_id: int
    bucket: str
    src_pool_id: int
    dst_pool_id: int
    phase: str
    reason: str
    created_at: str


class MigrationLogEntry(BaseModel):
    id: int
    migration_id: int
    phase: str
    message: str
    created_at: str


# ── Config Status ─────────────────────────────────────────────────────────────


class ConfigFileStatus(BaseModel):
    path: str
    sha256_disk: Optional[str]
    sha256_db: Optional[str]
    drifted: bool
    last_synced_at: Optional[str]


class ConfigStatus(BaseModel):
    files: List[ConfigFileStatus]
    last_reload_ok: Optional[bool] = None
    last_reload_at: Optional[str] = None


class NginxResult(BaseModel):
    ok: bool
    output: str


# ── Node Config ───────────────────────────────────────────────────────────────


class NodeConfigFile(BaseModel):
    path: str
    content: str


class NodeConfigRequest(BaseModel):
    node_type: str = Field(..., pattern=r"^(nanio-only|nginx-only|nginx-nanio)$")
    data_dir: str = Field("/data")
    nanio_port: int = Field(9000, ge=1, le=65535)
    nanio_host: str = Field("0.0.0.0")
    nanio_region: str = Field("us-east-1")
    access_key: Optional[str] = None
    secret_key: Optional[str] = None


class NodeConfigOut(BaseModel):
    node_type: str
    member_address: str
    files: List[NodeConfigFile]
    instructions: str


# ── Audit ─────────────────────────────────────────────────────────────────────


class AuditEntry(BaseModel):
    id: int
    actor: str
    action: str
    entity_type: str
    entity_id: Optional[int]
    before_json: Optional[str]
    after_json: Optional[str]
    nginx_reload_ok: Optional[bool]
    nginx_reload_output: Optional[str]
    created_at: str


# ── Health ────────────────────────────────────────────────────────────────────


class HealthOut(BaseModel):
    status: str = "ok"
    version: str
    dev_mode: bool
    db_ok: bool
    nginx_config_dir: str
    drift_alerts: int = 0
