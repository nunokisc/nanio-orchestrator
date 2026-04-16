"""Server-rendered HTML routes for the Web UI."""

from __future__ import annotations

import socket
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from jinja2 import Environment, FileSystemLoader

from nanio_orchestrator.config import get_settings
from nanio_orchestrator.db import get_db_ctx
from nanio_orchestrator import __version__
from nanio_orchestrator.auth import (
    clear_session_cookie,
    is_https,
    set_session_cookie,
)

router = APIRouter(tags=["web"], include_in_schema=False)

_jinja_env = None


def _get_env() -> Environment:
    global _jinja_env
    if _jinja_env is None:
        tpl_dir = Path(__file__).parent / "templates"
        _jinja_env = Environment(
            loader=FileSystemLoader(str(tpl_dir)),
            autoescape=True,
        )
    return _jinja_env


def _render(template_name: str, **ctx) -> HTMLResponse:
    s = get_settings()
    env = _get_env()
    tpl = env.get_template(template_name)
    html = tpl.render(version=__version__, dev_mode=s.dev, settings=s, **ctx)
    return HTMLResponse(html)


def _hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return "unknown"


# ── Login / Logout ────────────────────────────────────────────────────────────


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    return _render("login.html", hostname=_hostname(), error=error)


@router.post("/login")
async def login_submit(request: Request, key: str = Form(...)):
    s = get_settings()
    if key != s.api_key:
        return _render("login.html", hostname=_hostname(), error="Invalid API key.")

    response = RedirectResponse(url="/", status_code=303)
    set_session_cookie(response, s.api_key, s.session_ttl, secure=is_https(request))
    return response


@router.post("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    clear_session_cookie(response)
    return response


# ── Dashboard ─────────────────────────────────────────────────────────────────


@router.get("/", response_class=HTMLResponse)
async def dashboard():
    async with get_db_ctx() as db:
        pools = await db.execute_fetchall("SELECT * FROM pools ORDER BY name")
        vhosts = await db.execute_fetchall("SELECT * FROM vhosts ORDER BY server_name")
        config_files = await db.execute_fetchall("SELECT * FROM config_files ORDER BY path")
        recent_audit = await db.execute_fetchall(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT 10"
        )

        # Count drift
        drift_count = 0
        for cf in config_files:
            if cf["sha256_disk"] and cf["sha256_db"] and cf["sha256_disk"] != cf["sha256_db"]:
                drift_count += 1

        # Last reload
        reload_row = await db.execute_fetchall(
            """SELECT nginx_reload_ok, nginx_reload_output, created_at FROM audit_log
               WHERE nginx_reload_ok IS NOT NULL ORDER BY id DESC LIMIT 1"""
        )
        last_reload = dict(reload_row[0]) if reload_row else None

    return _render(
        "dashboard.html",
        pools=[dict(p) for p in pools],
        vhosts=[dict(v) for v in vhosts],
        config_files=[dict(cf) for cf in config_files],
        drift_count=drift_count,
        last_reload=last_reload,
        recent_audit=[dict(a) for a in recent_audit],
    )


# ── Pools ─────────────────────────────────────────────────────────────────────


@router.get("/web/pools", response_class=HTMLResponse)
async def pools_page():
    async with get_db_ctx() as db:
        pools = await db.execute_fetchall("SELECT * FROM pools ORDER BY name")
        result = []
        for p in pools:
            members = await db.execute_fetchall(
                "SELECT * FROM pool_members WHERE pool_id = ? ORDER BY id", (p["id"],)
            )
            pool_dict = dict(p)
            pool_dict["members"] = [dict(m) for m in members]
            result.append(pool_dict)
    return _render("pools.html", pools=result)


# ── Vhosts ────────────────────────────────────────────────────────────────────


@router.get("/web/vhosts", response_class=HTMLResponse)
async def vhosts_page():
    async with get_db_ctx() as db:
        vhosts = await db.execute_fetchall("SELECT * FROM vhosts ORDER BY server_name")
        pools = await db.execute_fetchall("SELECT id, name FROM pools ORDER BY name")
        result = []
        for v in vhosts:
            routes = await db.execute_fetchall(
                """SELECT r.*, p.name as pool_name
                   FROM routes r JOIN pools p ON r.pool_id = p.id
                   WHERE r.vhost_id = ?
                   ORDER BY length(r.path_prefix) DESC""",
                (v["id"],),
            )
            vhost_dict = dict(v)
            vhost_dict["routes"] = [dict(r) for r in routes]
            result.append(vhost_dict)
    return _render("vhosts.html", vhosts=result, pools=[dict(p) for p in pools])


# ── Config ────────────────────────────────────────────────────────────────────


@router.get("/web/config", response_class=HTMLResponse)
async def config_page():
    async with get_db_ctx() as db:
        files = await db.execute_fetchall("SELECT * FROM config_files ORDER BY path")
        file_list = []
        for f in files:
            fd = dict(f)
            fd["drifted"] = (
                fd["sha256_disk"] is not None
                and fd["sha256_db"] is not None
                and fd["sha256_disk"] != fd["sha256_db"]
            )
            file_list.append(fd)
    return _render("config.html", files=file_list)


# ── Audit ─────────────────────────────────────────────────────────────────────


@router.get("/web/audit", response_class=HTMLResponse)
async def audit_page():
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT 100"
        )
    return _render("audit.html", entries=[dict(r) for r in rows])
