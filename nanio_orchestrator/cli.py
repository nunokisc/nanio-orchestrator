"""CLI entry point — click-based commands: serve, install, config."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click

# ── Config metadata ───────────────────────────────────────────────────────────
# (field_name, category, description, is_secret)

_SETTINGS_META = {
    "host":                      ("Core",        "Bind address",                           False),
    "port":                      ("Core",        "Listen port",                            False),
    "api_key":                   ("Core",        "API authentication key",                 True),
    "log_level":                 ("Core",        "Log level (debug/info/warning/error)",   False),
    "session_ttl":               ("Core",        "Web UI session duration (seconds)",      False),
    "db_path":                   ("Database",    "SQLite database file path",              False),
    "db_backup_path":            ("Database",    "Backup path (default: db_path + .bak)",  False),
    "db_backup_interval":        ("Database",    "Seconds between timed backups",          False),
    "db_backup_rotate":          ("Database",    "Number of backup copies to keep",        False),
    "secret":                    ("Security",    "Fernet key for credential encryption",   True),
    "s3_access_key":             ("S3",          "Global S3 access key",                   True),
    "s3_secret_key":             ("S3",          "Global S3 secret key",                   True),
    "bucket_sync_interval":      ("S3",          "Seconds between bucket syncs",           False),
    "nginx_config_dir":          ("Nginx",       "Root directory for generated configs",   False),
    "drift_interval":            ("Nginx",       "Seconds between drift checks",           False),
    "rclone_path":               ("Migrations",  "Path to rclone binary",                  False),
    "migration_max_parallel":    ("Migrations",  "Max concurrent migrations",              False),
    "migration_bandwidth_limit": ("Migrations",  "rclone --bwlimit value (e.g. 50M)",     False),
    "migration_checkers":        ("Migrations",  "rclone --checkers value",                False),
    "migration_transfers":       ("Migrations",  "rclone --transfers value",               False),
}

_CATEGORY_ORDER = ["Core", "Database", "Security", "S3", "Nginx", "Migrations"]


def _get_config_path() -> str:
    from nanio_orchestrator.config import DEV_MODE
    return "dev.env" if DEV_MODE else "/etc/nanio-orchestrator/config.env"


def _mask(value, is_secret: bool) -> str:
    if not is_secret:
        return str(value) if value is not None else "(unset)"
    if not value:
        return "(not set)"
    s = str(value)
    return s[:4] + "****" if len(s) > 4 else "****"


def _set_config_value(short_key: str, value: str, config_path: str) -> None:
    """Update or add NANIO_ORCHESTRATOR_<KEY>=<value> in the config file."""
    env_key = f"NANIO_ORCHESTRATOR_{short_key.upper()}"
    lines = Path(config_path).read_text().splitlines() if Path(config_path).exists() else []

    replaced = False
    for i, line in enumerate(lines):
        stripped = line.lstrip("# ").strip()
        if stripped.startswith(env_key + "="):
            lines[i] = f"{env_key}={value}"
            replaced = True
            break

    if not replaced:
        lines.append(f"{env_key}={value}")

    Path(config_path).parent.mkdir(parents=True, exist_ok=True)
    Path(config_path).write_text("\n".join(lines) + "\n")


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx):
    """nanio-orchestrator — nginx config manager for nanio S3 clusters."""
    if ctx.invoked_subcommand is None:
        # Default: start the server
        ctx.invoke(serve)


@main.command()
@click.option("--host", default=None, help="Bind address")
@click.option("--port", default=None, type=int, help="Port number")
@click.option("--reload", "do_reload", is_flag=True, default=False, help="Enable auto-reload (dev)")
def serve(host, port, do_reload):
    """Start the nanio-orchestrator server."""
    from nanio_orchestrator.config import get_settings

    s = get_settings()
    bind_host = host or s.host
    bind_port = port or s.port

    # Ensure directories exist
    s.ensure_dirs()

    # Init DB synchronously before starting
    from nanio_orchestrator.db import init_db_sync
    init_db_sync()

    import uvicorn

    reload_flag = do_reload or s.dev

    # Configure file logging if LOG_FILE is set
    if s.log_file:
        import logging
        from logging.handlers import RotatingFileHandler
        from pathlib import Path as _Path
        _Path(s.log_file).parent.mkdir(parents=True, exist_ok=True)
        _fmt = logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
        _fh = RotatingFileHandler(
            s.log_file,
            maxBytes=10 * 1024 * 1024,  # 10 MB per file
            backupCount=5,
            encoding="utf-8",
        )
        _fh.setFormatter(_fmt)
        _fh.setLevel(s.log_level.upper())
        logging.getLogger().addHandler(_fh)
        print(f"Logging to file: {s.log_file}")

    if s.dev:
        print(f"nanio-orchestrator dev mode → http://localhost:{bind_port}  API key: {s.api_key}")
    else:
        print(f"nanio-orchestrator starting on {bind_host}:{bind_port}")

    uvicorn.run(
        "nanio_orchestrator.app:create_app",
        host=bind_host,
        port=bind_port,
        reload=reload_flag,
        factory=True,
        log_level=s.log_level,
    )


@main.command()
def install():
    """Install nanio-orchestrator for production use.

    Creates directories, config files, systemd unit, and DB schema.
    Must be run as root.
    """
    from nanio_orchestrator.install import run_install
    run_install()


@main.command("rebuild-db")
@click.option("--dry-run", is_flag=True, default=False, help="Show what would be imported without writing")
@click.option("--force", is_flag=True, default=False, help="Proceed even if DB already exists (overwrites)")
def rebuild_db(dry_run, force):
    """Rebuild the database from nginx config files and sidecar files.

    Use after DB loss or corruption. Reconstructs pools, vhosts, routes,
    credentials (from sidecars), and in-progress migrations (from state files).
    """
    import asyncio

    async def _rebuild():
        from nanio_orchestrator.config import get_settings
        from nanio_orchestrator.db import init_db, get_db_ctx

        s = get_settings()
        s.ensure_dirs()

        if not dry_run and not force:
            await init_db()
            async with get_db_ctx() as db:
                pools = await db.execute_fetchall("SELECT COUNT(*) as cnt FROM pools")
                if pools[0]["cnt"] > 0:
                    print("✗ Database already contains data.")
                    print("  Use --force to overwrite, or --dry-run to preview.")
                    return False

        if not dry_run and force:
            await init_db()
            from nanio_orchestrator.db import CLEAR_TABLES
            async with get_db_ctx() as db:
                for table in CLEAR_TABLES:
                    await db.execute(f"DELETE FROM {table}")
                await db.commit()

        from nanio_orchestrator.rebuild import rebuild_from_disk
        print("Rebuilding database from disk...\n")
        result = await rebuild_from_disk(dry_run=dry_run)

        if dry_run:
            print("DRY RUN — no changes written\n")
            for p in result.get("pools", []):
                creds = "credentials recovered" if p.get("has_credentials") else "no credentials"
                sidecar = "sidecar ✓" if p.get("has_sidecar") else "no sidecar"
                print(f"  ✓ {p['name']:30s} ({p['type']}, {p['members']} members, {creds}, {sidecar})")
            for v in result.get("vhosts", []):
                dp = "default_pool recovered" if v.get("has_default_pool") else "no default_pool"
                sidecar = "sidecar ✓" if v.get("has_sidecar") else "no sidecar"
                print(f"  ✓ {v['server_name']:30s} ({v['routes']} routes, {dp}, {sidecar})")
            mig = result.get("migrations", 0)
            if mig:
                print(f"  ✓ {mig} migration(s) in progress")
            print(f"\n  ⚠ audit_log: not recoverable — historical data only")
        else:
            print(f"  Pools:       {result['pools_imported']}")
            print(f"  Members:     {result['members_imported']}")
            print(f"  Vhosts:      {result['vhosts_imported']}")
            print(f"  Routes:      {result['routes_imported']}")
            print(f"  Migrations:  {result['migrations_imported']} (in progress — will auto-resume)")
            print(f"  Credentials: {result['credentials_recovered']} recovered")
            for w in result.get("warnings", []):
                print(f"  ⚠ {w}")
            print(f"\n  ⚠ audit_log: not recoverable — historical data only")
            print(f"\nDatabase rebuilt successfully.")
            print(f"Next: systemctl restart nanio-orchestrator")

        return True

    ok = asyncio.run(_rebuild())
    sys.exit(0 if ok else 1)


@main.group()
def config():
    """Config management subcommands."""
    pass


@config.command("show")
def config_show():
    """Show all current settings, grouped by category."""
    from nanio_orchestrator.config import get_settings, DEV_MODE
    s = get_settings()
    config_path = _get_config_path()

    by_category: dict[str, list] = {cat: [] for cat in _CATEGORY_ORDER}
    for field, (cat, desc, is_secret) in _SETTINGS_META.items():
        raw = getattr(s, field, None)
        # db_backup_path: show effective value, not the raw (possibly None) setting
        if field == "db_backup_path":
            raw = s.effective_db_backup_path
        by_category.setdefault(cat, []).append((field, raw, desc, is_secret))

    for cat in _CATEGORY_ORDER:
        entries = by_category.get(cat, [])
        if not entries:
            continue
        print(f"\n{cat}")
        for field, raw, desc, is_secret in entries:
            value_str = _mask(raw, is_secret)
            print(f"  {field:<28} {value_str:<30}  {desc}")

    mode = "dev" if DEV_MODE else "production"
    print(f"\nConfig file ({mode}): {config_path}")


@config.command("get")
@click.argument("key")
def config_get(key):
    """Print the current value of a single setting (for scripting)."""
    from nanio_orchestrator.config import get_settings
    s = get_settings()
    normalized = key.lower().removeprefix("nanio_orchestrator_")
    if normalized == "db_backup_path":
        print(s.effective_db_backup_path)
    elif hasattr(s, normalized):
        value = getattr(s, normalized)
        print("" if value is None else value)
    else:
        print(f"Unknown setting: {key}", file=sys.stderr)
        sys.exit(1)


@config.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key, value):
    """Set a configuration value in the active config file.

    KEY is the short name without the NANIO_ORCHESTRATOR_ prefix.

    Examples:

    \b
      nanio-orchestrator config set api_key mysecret
      nanio-orchestrator config set log_level debug
      nanio-orchestrator config set migration_max_parallel 4
    """
    normalized = key.lower().removeprefix("nanio_orchestrator_")
    if normalized not in _SETTINGS_META:
        print(f"Unknown setting: '{key}'", file=sys.stderr)
        print(f"Known settings: {', '.join(sorted(_SETTINGS_META))}", file=sys.stderr)
        sys.exit(1)

    config_path = _get_config_path()
    _set_config_value(normalized, value, config_path)
    env_key = f"NANIO_ORCHESTRATOR_{normalized.upper()}"
    _, _, is_secret = _SETTINGS_META[normalized]
    display = _mask(value, is_secret)
    print(f"✓ {env_key}={display}  →  {config_path}")


@config.command("generate-secret")
@click.option("--set", "do_set", is_flag=True, default=False,
              help="Also write the generated key to the config file.")
def config_generate_secret(do_set):
    """Generate a Fernet encryption key for NANIO_ORCHESTRATOR_SECRET.

    Run with --set to write it directly to the config file.
    """
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    print(key)
    if do_set:
        config_path = _get_config_path()
        _set_config_value("secret", key, config_path)
        print(f"✓ NANIO_ORCHESTRATOR_SECRET written to {config_path}", file=sys.stderr)


@config.command("edit")
def config_edit():
    """Open the config file in $EDITOR (falls back to nano, then vi)."""
    config_path = _get_config_path()
    if not Path(config_path).exists():
        print(f"Config file not found: {config_path}", file=sys.stderr)
        print("Run 'nanio-orchestrator install' first, or create the file manually.", file=sys.stderr)
        sys.exit(1)

    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    if not editor:
        for fallback in ("nano", "vi"):
            import shutil
            if shutil.which(fallback):
                editor = fallback
                break

    if not editor:
        print(f"No editor found. Edit manually: {config_path}", file=sys.stderr)
        sys.exit(1)

    os.execvp(editor, [editor, config_path])


@config.command("validate")
def config_validate():
    """Run nginx -t to validate configuration."""
    import asyncio
    from nanio_orchestrator.nginx.executor import test_config

    result = asyncio.run(test_config())
    print(result.output)
    sys.exit(0 if result.ok else 1)


@config.command("reload")
def config_reload():
    """Run nginx -s reload."""
    import asyncio
    from nanio_orchestrator.nginx.executor import reload_nginx

    result = asyncio.run(reload_nginx())
    print(result.output)
    sys.exit(0 if result.ok else 1)


@config.command("rebuild")
def config_rebuild():
    """Rebuild all config files from DB."""
    import asyncio

    async def _rebuild():
        from nanio_orchestrator.config import get_settings
        from nanio_orchestrator.db import init_db

        s = get_settings()
        s.ensure_dirs()
        await init_db()

        from nanio_orchestrator.nginx.generator import generate_all_configs, write_config_atomic
        configs = await generate_all_configs()
        for filepath, content in configs:
            await write_config_atomic(filepath, content)
            print(f"  ✓ {filepath}")

        from nanio_orchestrator.nginx.executor import test_and_reload
        result = await test_and_reload()
        print(result.output)
        return result.ok

    ok = asyncio.run(_rebuild())
    sys.exit(0 if ok else 1)


@main.group()
def orphaned():
    """Orphaned source data management."""
    pass


@orphaned.command("list")
def orphaned_list():
    """List all migrations with orphaned source data on the original pool.

    After a migration completes, source-bucket data is never deleted automatically.
    Use this command to see which buckets have orphaned data so you can decide
    when and how to clean them up manually.
    """
    import asyncio

    async def _list():
        from nanio_orchestrator.config import get_settings
        from nanio_orchestrator.db import init_db, get_db_ctx

        s = get_settings()
        s.ensure_dirs()
        await init_db()

        async with get_db_ctx() as db:
            rows = await db.execute_fetchall(
                """SELECT m.id, m.bucket, m.orphaned_source_pool_id,
                          m.orphaned_source_prefix, m.orphaned_at, m.finished_at,
                          p.name as src_pool_name
                   FROM migrations m
                   LEFT JOIN pools p ON m.orphaned_source_pool_id = p.id
                   WHERE m.orphaned_source_pool_id IS NOT NULL
                   ORDER BY m.orphaned_at DESC"""
            )

        if not rows:
            print("No orphaned data found.")
            return

        col_w = [6, 24, 22, 26, 22]
        header = (
            f"{'ID':<{col_w[0]}} {'Bucket':<{col_w[1]}} "
            f"{'Source Pool':<{col_w[2]}} {'Prefix':<{col_w[3]}} {'Orphaned At'}"
        )
        print(header)
        print("-" * (sum(col_w) + 4))
        for r in rows:
            row = dict(r)
            pool_label = row["src_pool_name"] or str(row["orphaned_source_pool_id"])
            print(
                f"{row['id']:<{col_w[0]}} "
                f"{row['bucket']:<{col_w[1]}} "
                f"{pool_label:<{col_w[2]}} "
                f"{(row['orphaned_source_prefix'] or ''):<{col_w[3]}} "
                f"{row['orphaned_at'] or ''}"
            )

    asyncio.run(_list())


if __name__ == "__main__":
    main()
