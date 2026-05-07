# CLAUDE.md — operating manual for Claude sessions in this repo

Read this before making any changes.

## What is nanio-orchestrator

A **control-plane tool** for managing nginx as an S3-compatible storage gateway on top of
[nanio](https://github.com/nunokisc/nanio) instances. It writes nginx config files and signals
nginx to reload — traffic never flows through the orchestrator.

Key design constraints:
- **Stateless control plane**: the orchestrator can crash or be restarted without dropping traffic
- **Rebuild from disk**: losing the SQLite database is recoverable — see `nanio_orchestrator/rebuild.py`
- **No in-process state**: nothing cached between requests beyond what FastAPI/SQLite provide
- **Security**: API protected by API key + session cookie; credential values encrypted with Fernet

## Repo layout

```
nanio_orchestrator/
├── app.py              # FastAPI app factory + auth middleware
├── cli.py              # Click CLI: serve, install, remove, rebuild
├── config.py           # Settings (pydantic-settings), dev/prod detection
├── db.py               # SQLite schema, migrations, get_db_ctx()
├── models.py           # Pydantic request/response models
├── install.py          # Production install: dirs, systemd, sudoers, DB init
├── rebuild.py          # Reconstruct DB from nginx config + sidecar files
├── sidecar.py          # .meta.json sidecar read/write (pool + vhost sidecars)
├── audit_log.py        # Structured audit log entries
├── auth.py             # API key + session cookie authentication
├── backup.py           # SQLite rotating backups
├── bucket_sync.py      # Background sync of S3 buckets per vhost
├── migration_engine.py # rclone-based migration state machine
├── s3client.py         # Custom async S3 HTTP client (stdlib only, SigV4)
├── drift.py            # Drift detection (DB ↔ disk config mismatch)
├── credentials.py      # Fernet encryption for pool credentials
├── api/
│   ├── pools.py        # Pool + member CRUD; per-pool bucket status
│   ├── vhosts.py       # Vhost + route CRUD; bucket promote
│   ├── buckets.py      # Bucket sync, promote, orphan scan/purge
│   ├── config.py       # Config generation, sync, preview, rebuild-from-disk
│   ├── migrations.py   # Migration CRUD + cancel
│   ├── credentials.py  # Pool credential CRUD (encrypted)
│   ├── audit.py        # Audit log read API
│   └── health.py       # Health check
├── nginx/
│   ├── generator.py    # Jinja2 → nginx config text
│   ├── parser.py       # Parse managed nginx configs back to dicts
│   ├── executor.py     # nginx -t, nginx -s reload (via subprocess/sudo)
│   └── templates/      # Jinja2 templates for upstreams + server blocks
└── web/
    ├── routes.py       # Server-rendered HTML routes (Starlette/Jinja2)
    ├── static/         # app.js, style.css
    └── templates/      # HTML templates (base.html, vhosts.html, …)

tests/                  # pytest suite (177 tests, asyncio_mode=auto)
docs/                   # Detailed documentation
scripts/                # bootstrap.sh (bare-server setup)
systemd/                # nanio-orchestrator.service template
```

## Key concepts

### Pool types
| Type | Meaning |
|------|---------|
| `nanio` | S3-compatible instance; has credentials, can be migration source/destination, bucket sync works |
| `http` | Plain nginx serving static files; no S3 semantics, no credentials, no bucket sync |

### Vhost structure
A vhost is an nginx `server {}` block. The orchestrator manages:
- SSL cert paths, listen port
- A `default_pool` (catch-all `/` route, sets the vhost's pool type)
- Additional named routes (`/bucket-name/` → specific pool)
- `extra_blocks` — freeform nginx directives injected at `top`/`ssl`/`proxy`/`end` zones
- `ip_rule_mode` + `ip_rule_ips` — server-level IP allowlist or denylist

### Sidecar files
Alongside each nginx `.conf` file there is a `.meta.json` sidecar:
- `pools/*.meta.json` — pool type, description, encrypted credentials
- `vhosts/*.meta.json` — default_pool_id, default_pool_name, extra_blocks_json, ip_rule_mode, ip_rule_ips_json

Sidecars are the source of truth for DB rebuild. **Always update sidecars after writing to DB.**

### Migration state machine
Phases: `pending → copying → write_routing → verifying → switching → done`
Error exits: `error`, `cancelled`

The verifying phase runs a convergence loop (up to `migration_max_copy_passes` passes) that
copies then checks, retrying while the diff count decreases. This handles the case where
files are being uploaded during migration.

### DB rebuild
`POST /api/config/rebuild-from-disk` (or `nanio-orchestrator rebuild`) reconstructs the entire
SQLite database from:
1. `nginx/*.conf` files → pools, members, vhosts, routes
2. `*.meta.json` sidecars → pool type/credentials, vhost defaults/extra_blocks/IP rules
3. `*.state.json` files → in-progress migrations
4. Live `ListBuckets` → bucket_sync state

**This is a core feature. Never break it.**

## Golden rules

1. **Always update both DB and sidecar together.** When writing vhost fields (extra_blocks,
   ip_rule_*), call `write_vhost_sidecar()` with all fields after the DB write. Same for pools.

2. **SQL writes go through `get_db_ctx()`** — it handles connection, WAL mode, and foreign keys.
   Never open a raw sqlite3 connection in handlers.

3. **Test nginx config before reload.** All config changes go through `_apply_vhost_config()` /
   `_apply_pool_config()` which run `nginx -t`, write atomically, reload, and roll back on failure.

4. **HTTP pools are second-class.** They have no S3 credentials, no bucket sync, no migration.
   Filter them out in bucket-related endpoints — see `bucket_sync.py` and `api/pools.py`.

5. **Route granularity is bucket-level.** Nginx routes are `location /bucket-name/`. You cannot
   route sub-paths within a bucket to different pools — `ListObjectsV2` always uses `?prefix=`, not URL path.

6. **Security**:
   - Never echo credentials or Fernet keys in API responses
   - All API endpoints (except `/health` and `/auth/*`) require authentication
   - Validate all IP/CIDR entries in `ip_rule_ips` with `_IP_ENTRY_RE`
   - XML and JSON bodies are bounded (no unbounded buffering)

7. **No new background tasks.** The only background tasks are `bucket_sync` and `backup`. They're
   started in `app.py` startup. New recurring work should follow the same pattern.

8. **Keep rebuild working.** Any new field stored in the DB must also be:
   - Written to the sidecar in `sidecar.py` (update `write_vhost_sidecar` or `write_pool_sidecar`)
   - Restored in `rebuild.py` (the INSERT + dry-run report)

## Running commands

```bash
# Dev server
make run                   # or: DEV=true python -m nanio_orchestrator

# Tests
make test                  # pytest -v
make test-cov              # with coverage

# Lint + format
make lint                  # ruff check
make fmt                   # ruff format

# Build wheel
make build                 # uv build or python -m build
```

## Dev environment setup

```bash
# With uv (recommended)
uv sync
source .venv/bin/activate

# With pip
python3 -m venv .venv
pip install -e ".[dev]"
source .venv/bin/activate
```

## Production install (from PyPI)

```bash
# Install the tool (pick one)
pipx install nanio-orchestrator
# or:
uv tool install nanio-orchestrator

# Run the installer (requires root)
sudo nanio-orchestrator install

# Start/enable service
sudo systemctl enable --now nanio-orchestrator
```

## Environment variables

All settings are prefixed `NANIO_ORCHESTRATOR_`. In production they live in
`/etc/nanio-orchestrator/config.env`. In dev, `dev.env` is used.

Key settings:
| Variable | Default (prod) | Description |
|----------|---------------|-------------|
| `NANIO_ORCHESTRATOR_HOST` | `0.0.0.0` | Bind address |
| `NANIO_ORCHESTRATOR_PORT` | `8080` | Listen port |
| `NANIO_ORCHESTRATOR_API_KEY` | *(generated)* | API key for all requests |
| `NANIO_ORCHESTRATOR_DB_PATH` | `/opt/nanio-orchestrator/data/orchestrator.db` | SQLite file |
| `NANIO_ORCHESTRATOR_NGINX_CONFIG_DIR` | `/etc/nginx/nanio` | Where to write nginx configs |
| `NANIO_ORCHESTRATOR_SECRET` | *(none)* | Fernet key for credential encryption |
| `NANIO_ORCHESTRATOR_RCLONE_PATH` | `rclone` | Binary path for migrations |
| `NANIO_ORCHESTRATOR_MIGRATION_MAX_PARALLEL` | `2` | Concurrent migrations |

## Things NOT to break

- The `rebuild_from_disk()` function and the sidecar contract
- `nginx -t` gate: config is only applied after nginx accepts it
- Pool-type consistency within a vhost (all routes must be same type)
- The audit log — all mutations should call `log_audit()`
- Migration convergence loop in `migration_engine.py`
- The 177-test suite (run before every commit)
