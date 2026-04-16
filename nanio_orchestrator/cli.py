"""CLI entry point — click-based commands: serve, install, config."""

from __future__ import annotations

import os
import sys

import click


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


@main.group()
def config():
    """Config management subcommands."""
    pass


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


if __name__ == "__main__":
    main()
