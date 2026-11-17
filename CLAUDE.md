# CLAUDE.md — operating manual for Claude sessions in this repo

Read this before making any changes.

## What is nanio-orchestrator

A **control-plane tool** for managing nginx as an S3-compatible storage gateway on top of
[nanio](https://github.com/apocas/nanio) instances. It writes nginx config files and validates
them with `nginx -t` — traffic never flows through the orchestrator.

Key design constraints:
- **Stateless control plane**: the orchestrator can crash or be restarted without dropping traffic
- **Rebuild from disk**: losing the SQLite database is recoverable — see `nanio_orchestrator/rebuild.py`
- **No in-process state**: nothing cached between requests beyond what FastAPI/SQLite provide
- **Security**: API protected by API key + session cookie; credential values encrypted with Fernet

## Repo layout

```
nanio_orchestrator/
├── app.py              # FastAPI app factory + auth middleware
├── cli.py              # Click CLI: serve, install, doctor, remove, rebuild-db, config …
├── config.py           # Settings (pydantic-settings), dev/prod detection
├── db.py               # SQLite schema, migrations, get_db_ctx()
├── models.py           # Pydantic request/response models
├── install.py          # install (idempotent), doctor (checks + fixes), remove
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
│   ├── apply.py        # The only write path: write → nginx -t → (reload) → rollback, under one lock
│   └── templates/      # Jinja2 templates for upstreams + server blocks
└── web/
    ├── routes.py       # Server-rendered HTML routes (Starlette/Jinja2)
    ├── static/         # app.js, style.css
    └── templates/      # HTML templates (base.html, vhosts.html, …)

tests/                  # pytest suite (asyncio_mode=auto)
docs/                   # Detailed documentation
install.sh              # One-command install/upgrade: venv in /opt + `nanio-orchestrator install`
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

From `write_routing` on, nginx serves the bucket through split routing and the **destination is
authoritative** (writes → dst, reads → dst with 404-fallback to src, DELETE mirrored to src). Rules:

- The split is recorded in `migrations.split_routing`, never derived from the phase. It survives
  error/cancel/restart/rebuild until a migration between the same two pools finishes the move.
- Once the split is active every `rclone copy` uses `--update`, and verification is a one-way check
  that accepts a differing object only if the destination copy is newer. A plain copy or a two-way
  check here destroys or rejects client writes.

### DB rebuild
`POST /api/config/rebuild-from-disk` (or `nanio-orchestrator rebuild-db`) reconstructs the entire
SQLite database from:
1. `nginx/*.conf` files → pools, members, vhosts, routes
2. `*.meta.json` sidecars → pool type/credentials, vhost defaults/extra_blocks/IP rules
3. `*.state.json` files → in-progress migrations (including `split_routing`)
4. Live `ListBuckets` → bucket_sync state

**This is a core feature. Never break it.**

There is one importer, `rebuild.import_configs()`: `rebuild_from_disk()` runs it on an empty DB,
`POST /api/config/sync` on a live one. The opposite direction is `apply.rebuild_all_configs()`
(`POST /api/config/rebuild`, `config rebuild`), which rewrites every `.conf` **and** every sidecar.
Schema upgrades live only in `db._run_migrations_async()`; `init_db_sync()` just runs `init_db()`.

## Golden rules

1. **Always update both DB and sidecar together.** When writing vhost fields (extra_blocks,
   ip_rule_*), call `write_vhost_sidecar()` with all fields after the DB write. Same for pools.

2. **SQL writes go through `get_db_ctx()`** — it handles connection, WAL mode, and foreign keys.
   Never open a raw sqlite3 connection in handlers.

3. **Every nginx config write goes through `nginx/apply.py`.** `apply_config_file()` /
   `apply_config_files()` write the file at its final path, run `nginx -t`, and restore the previous
   content on failure — nginx only includes `*.conf`, so testing while the candidate is still a `.tmp`
   validates nothing. Never write a managed `.conf` any other way. `_write_vhost_config()` /
   `_write_pool_config()` are thin wrappers over it and do **not**
   reload nginx — the operator applies changes explicitly via the Config tab (`POST /api/config/reload`).
   Only the migration engine reloads on its own (it must apply its own routing steps). Background
   tasks never change routing: bucket sync only *flags* a routed bucket missing on its pool
   (`bucket_sync.missing_since`) — a node with unmounted storage answers "no such bucket" too.

4. **HTTP pools are second-class.** They have no S3 credentials, no bucket sync, no migration.
   Filter them out in bucket-related endpoints — see `bucket_sync.py` and `api/pools.py`.

5. **Route granularity is bucket-level.** Nginx routes are `location /bucket-name/`. You cannot
   route sub-paths within a bucket to different pools — `ListObjectsV2` always uses `?prefix=`, not URL path.

6. **Security**:
   - Never echo credentials or Fernet keys in API responses
   - All API endpoints (except `/health` and `/auth/*`) require authentication
   - Validate all IP/CIDR entries in `ip_rule_ips` with `_IP_ENTRY_RE`
   - XML and JSON bodies are bounded (no unbounded buffering)
   - Endpoints that take a file path accept managed `pools/*.conf` / `vhosts/*.conf` only
   - Settings that choose a binary or a directory (`_HOST_ONLY_SETTINGS`) are not writable through
     the API — the panel key must not be equivalent to shell access
   - Never persist secrets in plain text (node-config output is returned, not stored)

7. **No new background tasks.** The only background tasks are `drift`, `bucket_sync` and `backup`.
   They're started in `app.py` startup. New recurring work should follow the same pattern.

9. **Bucket creation is not the orchestrator's job.** Route creation and `promote_bucket` do not create
   buckets on the target pool. Bucket provisioning is the responsibility of whoever uses the S3 service.
   The only exception is the migration engine, which will create the destination bucket if it does not
   exist before starting an rclone copy.

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

## Production install

```bash
curl -fsSL https://raw.githubusercontent.com/nunokisc/nanio-orchestrator/main/install.sh | sudo bash
sudo nanio-orchestrator doctor     # verify; prints the fix for anything wrong
```

`install` must stay idempotent (never overwrite config, DB or managed nginx files) and `remove`
without `--purge` must leave `/etc/nginx/nanio` in place — nginx keeps serving.

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
| `NANIO_ORCHESTRATOR_SECRET` | *(generated)* | Fernet key for credential encryption |
| `NANIO_ORCHESTRATOR_RCLONE_PATH` | `rclone` | Binary path for migrations |
| `NANIO_ORCHESTRATOR_MIGRATION_MAX_PARALLEL` | `2` | Concurrent migrations |

## Things NOT to break

- The `rebuild_from_disk()` function and the sidecar contract
- `nginx -t` gate: config is only applied after nginx accepts it
- Pool-type consistency within a vhost (all routes must be same type)
- The audit log — all mutations should call `log_audit()`
- Migration convergence loop in `migration_engine.py`
- The test suite (`make test`, run before every commit)
- The docs: `docs/*.md` describe behaviour operators rely on — update them in the same change
