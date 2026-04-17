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

        # Active rclone migrations
        active_mig_rows = await db.execute_fetchall(
            """SELECT m.*, sp.name as src_pool_name, dp.name as dst_pool_name
               FROM migrations m
               LEFT JOIN pools sp ON m.src_pool_id = sp.id
               LEFT JOIN pools dp ON m.dst_pool_id = dp.id
               WHERE m.phase IN ('pending','copying','verifying','switching')
               ORDER BY m.id DESC"""
        )
        active_migrations = [dict(r) for r in active_mig_rows]

        # Unrouted buckets per vhost (for dashboard widget)
        unrouted_rows = await db.execute_fetchall(
            """SELECT bs.vhost_id, bs.bucket, bs.discovered_at, v.server_name
               FROM bucket_sync bs
               JOIN vhosts v ON bs.vhost_id = v.id
               WHERE bs.status = 'unrouted'
               ORDER BY v.server_name, bs.bucket"""
        )
        # Group by vhost
        unrouted_by_vhost: dict = {}
        for r in unrouted_rows:
            rd = dict(r)
            vid = rd["vhost_id"]
            if vid not in unrouted_by_vhost:
                unrouted_by_vhost[vid] = {
                    "vhost_id": vid,
                    "server_name": rd["server_name"],
                    "buckets": [],
                }
            unrouted_by_vhost[vid]["buckets"].append({
                "name": rd["bucket"],
                "discovered_at": rd["discovered_at"],
            })

    return _render(
        "dashboard.html",
        pools=[dict(p) for p in pools],
        vhosts=[dict(v) for v in vhosts],
        config_files=[dict(cf) for cf in config_files],
        drift_count=drift_count,
        last_reload=last_reload,
        recent_audit=[dict(a) for a in recent_audit],
        unrouted_by_vhost=list(unrouted_by_vhost.values()),
        active_migrations=active_migrations,
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
        pool_names = {p["id"]: p["name"] for p in pools}
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
            vhost_dict["default_pool_name"] = pool_names.get(v["default_pool_id"]) if v["default_pool_id"] else None
            result.append(vhost_dict)
    return _render("vhosts.html", vhosts=result, pools=[dict(p) for p in pools])


# ── Config ────────────────────────────────────────────────────────────────────


@router.get("/web/config", response_class=HTMLResponse)
async def config_page():
    import hashlib
    async with get_db_ctx() as db:
        files = await db.execute_fetchall("SELECT * FROM config_files ORDER BY path")
        file_list = []
        for f in files:
            fd = dict(f)
            # Compute live disk hash so the page always reflects current state
            try:
                content = Path(fd["path"]).read_text()
                live_hash = hashlib.sha256(content.encode()).hexdigest()
            except (FileNotFoundError, PermissionError):
                live_hash = None
            fd["sha256_disk"] = live_hash
            fd["drifted"] = (
                live_hash is not None
                and fd["sha256_db"] is not None
                and live_hash != fd["sha256_db"]
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

# ── Migrations ────────────────────────────────────────────────────────────


@router.get("/web/settings", response_class=HTMLResponse)
async def settings_page():
    return _render("settings.html")


@router.get("/web/migrations", response_class=HTMLResponse)
async def migrations_page():
    async with get_db_ctx() as db:
        rows = await db.execute_fetchall(
            """SELECT m.*, sp.name as src_pool_name, dp.name as dst_pool_name
               FROM migrations m
               LEFT JOIN pools sp ON m.src_pool_id = sp.id
               LEFT JOIN pools dp ON m.dst_pool_id = dp.id
               ORDER BY m.id DESC LIMIT 100"""
        )
        pools = await db.execute_fetchall("SELECT id, name FROM pools ORDER BY name")
    return _render(
        "migrations.html",
        migrations=[dict(r) for r in rows],
        pools=[dict(p) for p in pools],
    )