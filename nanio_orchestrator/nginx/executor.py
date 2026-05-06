"""Run nginx -t and nginx -s reload, with dry-run support for dev mode."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from nanio_orchestrator.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class NginxExecResult:
    ok: bool
    output: str
    return_code: int


async def _run_cmd(cmd: list[str]) -> NginxExecResult:
    """Execute a command and capture output."""
    s = get_settings()
    cmd_str = " ".join(cmd)

    if s.dev:
        msg = f"[DRY RUN] {cmd_str}"
        logger.info(msg)
        return NginxExecResult(ok=True, output=msg, return_code=0)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        output = (stdout.decode("utf-8", errors="replace") + stderr.decode("utf-8", errors="replace")).strip()
        ok = proc.returncode == 0
        if not ok:
            logger.error("Command '%s' failed (rc=%d): %s", cmd_str, proc.returncode, output)
        else:
            logger.info("Command '%s' succeeded: %s", cmd_str, output)
        return NginxExecResult(ok=ok, output=output, return_code=proc.returncode or 0)
    except FileNotFoundError:
        msg = f"nginx binary not found when running: {cmd_str}"
        logger.error(msg)
        return NginxExecResult(ok=False, output=msg, return_code=-1)
    except Exception as e:
        msg = f"Error running '{cmd_str}': {e}"
        logger.error(msg)
        return NginxExecResult(ok=False, output=msg, return_code=-1)


async def test_config() -> NginxExecResult:
    """Run nginx -t to validate configuration."""
    return await _run_cmd(["sudo", "nginx", "-t"])


async def reload_nginx() -> NginxExecResult:
    """Run nginx -s reload to apply configuration."""
    return await _run_cmd(["sudo", "nginx", "-s", "reload"])


async def test_and_reload() -> NginxExecResult:
    """Test config first, then reload if valid. Returns the failing result if test fails."""
    test_result = await test_config()
    if not test_result.ok:
        return test_result
    reload_result = await reload_nginx()
    # Combine output
    combined = f"nginx -t: {test_result.output}\nnginx -s reload: {reload_result.output}"
    return NginxExecResult(
        ok=reload_result.ok,
        output=combined,
        return_code=reload_result.return_code,
    )


def detect_nginx() -> dict:
    """Synchronous check for nginx installation. Used by install command."""
    import shutil
    import subprocess

    nginx_path = shutil.which("nginx")
    if not nginx_path:
        return {"installed": False, "path": None, "version": None}

    try:
        result = subprocess.run(
            [nginx_path, "-v"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        version = (result.stderr or result.stdout).strip()
    except Exception:
        version = "unknown"

    return {"installed": True, "path": nginx_path, "version": version}
