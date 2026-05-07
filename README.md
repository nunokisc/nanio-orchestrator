# nanio-orchestrator

Nginx configuration manager and gateway orchestrator for a distributed nanio S3-compatible storage cluster.
It is a **control plane tool only** — traffic never flows through it. If the orchestrator is stopped or
crashes, nginx keeps serving traffic exactly as configured.

## Architecture

```
CLIENT (S3 SDK / browser / aws-cli)
│ HTTPS
▼
NGINX (gateway machine)
│ proxy_pass only
├─► upstream pool-2025   →  nanio instances  → /data/2025/
├─► upstream pool-2026   →  nanio instances  → /data/2026/
└─► upstream pool-cdn    →  nginx instances  → serve files via root/alias

ORCHESTRATOR (:8080, internal only)
│ writes config files + signals nginx
├─► /etc/nginx/nanio/pools/*.conf          (upstream blocks)
├─► /etc/nginx/nanio/pools/*.meta.json     (sidecar: pool type, description, encrypted credentials)
├─► /etc/nginx/nanio/vhosts/*.conf         (server blocks, proxy_pass only)
├─► /etc/nginx/nanio/vhosts/*.meta.json    (sidecar: default pool)
├─► SQLite at /opt/nanio-orchestrator/data/orchestrator.db
├─► SQLite backup at /opt/nanio-orchestrator/data/orchestrator.db.bak (+ rotated copies)
└─► /opt/nanio-orchestrator/data/migrations/*.state.json  (in-progress migration state — alongside DB)
```

## Quick Start — Production

### Method A: uv (preferred)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/nunokisc/nanio-orchestrator /tmp/nanio-orchestrator
UV_TOOL_DIR=/opt UV_TOOL_BIN_DIR=/usr/local/bin uv tool install /tmp/nanio-orchestrator
nanio-orchestrator install
```

### Method B: pip from local build

```bash
python3 -m ensurepip --upgrade
pip install /path/to/nanio-orchestrator
nanio-orchestrator install
```

### Method C: venv manual (no uv, no global pip)

```bash
python3 -m venv /opt/nanio-orchestrator/venv
/opt/nanio-orchestrator/venv/bin/pip install /path/to/nanio-orchestrator
/opt/nanio-orchestrator/venv/bin/nanio-orchestrator install
```

After install, follow the printed instructions to configure and start the service.

### Required nginx.conf Change

The orchestrator writes config files under `/etc/nginx/nanio/` but nginx only loads them if you
add the following includes to your nginx `http {}` block (e.g. `/etc/nginx/nginx.conf`):

```nginx
http {
    # ... existing config ...

    include /etc/nginx/nanio/pools/*.conf;   # upstream blocks
    include /etc/nginx/nanio/vhosts/*.conf;  # server blocks
}
```

> The install command prints this reminder. Without these includes nginx serves no nanio traffic,
> and `nginx -t` will report "unknown upstream" errors for any vhost that references a pool.

## Quick Start — Development

```bash
git clone https://github.com/nunokisc/nanio-orchestrator
cd nanio-orchestrator
make install-dev       # creates .venv, installs deps
make run               # dev server at http://localhost:8080
```

Or manually:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m nanio_orchestrator
```

Dev mode is auto-detected when `dev.env` exists or `DEV=true` is set. In dev mode:
- DB at `./dev-data/orchestrator.db`
- Nginx config at `./dev-data/nginx/`
- All nginx commands are **dry-run** (printed, not executed)
- uvicorn `--reload` enabled
- Default API key: `dev`

## Configuration Reference

All settings via `/etc/nanio-orchestrator/config.env` (production) or `dev.env` (development).
Every variable is prefixed with `NANIO_ORCHESTRATOR_`.

### Core

| Variable | Default | Description |
|----------|---------|-------------|
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `8080` | Listen port |
| `API_KEY` | `changeme` | API authentication key |
| `DB_PATH` | `/opt/nanio-orchestrator/data/orchestrator.db` | SQLite database path |
| `NGINX_CONFIG_DIR` | `/etc/nginx/nanio` | Root directory for generated nginx configs |
| `LOG_LEVEL` | `info` | Log level (`debug`, `info`, `warning`, `error`) |
| `LOG_FILE` | _(unset)_ | Path to a rotating log file, e.g. `/var/log/nanio-orchestrator/nanio.log`. Up to 10 MB per file, 5 rotated copies. When unset, logs go to stderr only. |
| `SESSION_TTL` | `28800` | Web UI session duration in seconds (8 hours) |
| `SECRET` | _(unset)_ | Fernet key for credential encryption at rest. Generate with: `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |

### S3 / Bucket Sync

| Variable | Default | Description |
|----------|---------|-------------|
| `S3_ACCESS_KEY` | _(unset)_ | Global S3 access key (used when no per-pool credentials are set) |
| `S3_SECRET_KEY` | _(unset)_ | Global S3 secret key |
| `BUCKET_SYNC_INTERVAL` | `300` | Seconds between automatic bucket list syncs |

### Migrations (rclone)

| Variable | Default | Description |
|----------|---------|-------------|
| `RCLONE_PATH` | `rclone` | Path to the rclone binary |
| `MIGRATION_MAX_PARALLEL` | `2` | Maximum concurrent migrations |
| `MIGRATION_BANDWIDTH_LIMIT` | _(unset)_ | rclone `--bwlimit` value, e.g. `50M` |
| `MIGRATION_CHECKERS` | `8` | rclone `--checkers` value |
| `MIGRATION_TRANSFERS` | `4` | rclone `--transfers` value |
| `MIGRATION_MAX_COPY_PASSES` | `10` | Maximum convergence loop passes during the `copying` phase before entering `write_routing`. |
| `S3_REQUEST_TIMEOUT` | `3600` | Socket timeout in seconds for S3 HTTP requests. Increase for buckets with very large objects. |

### Drift Detection

| Variable | Default | Description |
|----------|---------|-------------|
| `DRIFT_INTERVAL` | `60` | Seconds between drift checks |

### Database Backup

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_BACKUP_PATH` | `<DB_PATH>.bak` | Backup file path (defaults to DB path + `.bak`) |
| `DB_BACKUP_INTERVAL` | `300` | Seconds between timed backups |
| `DB_BACKUP_ROTATE` | `3` | Number of backup copies to keep (`.bak`, `.bak.2`, `.bak.3`) |

## Authentication

| Client | Method | Details |
|--------|--------|---------|
| **API** (`/api/*`) | `X-Orchestrator-Key` header | Missing/wrong key returns `401` |
| **Web UI** (`/web/*`, `/`) | Session cookie | Log in at `/login`; HMAC-signed `nanio_session` cookie issued with configurable TTL |

Public endpoints (no auth required): `/api/health`, `/api/docs`, `/api/redoc`, `/api/openapi.json`, `/login`, `/logout`, `/static/*`.

## API Reference

All endpoints under `/api/*` require the `X-Orchestrator-Key` header, except `/api/health`.

### Pools

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/pools` | List all pools |
| POST | `/api/pools` | Create pool |
| GET | `/api/pools/:id` | Get pool |
| PUT | `/api/pools/:id` | Update pool |
| DELETE | `/api/pools/:id` | Delete pool (rejected if routes reference it) |
| GET | `/api/pools/:id/members` | List pool members |
| POST | `/api/pools/:id/members` | Add member |
| PUT | `/api/pools/:id/members/:mid` | Update member |
| DELETE | `/api/pools/:id/members/:mid` | Remove member |
| GET | `/api/pools/:id/members/:mid/node-config` | Generate node config (query params) |
| POST | `/api/pools/:id/members/:mid/node-config` | Generate node config (body) |
| GET | `/api/pools/:id/node-config-summary` | Node config summary for all members |

### Pool Credentials

Per-pool S3 credentials, encrypted at rest with Fernet. Requires `SECRET` to be set.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/pools/:id/credentials` | Get credentials (access key masked) |
| PUT | `/api/pools/:id/credentials` | Store or replace credentials |
| DELETE | `/api/pools/:id/credentials` | Remove credentials |

### Vhosts + Routes

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/vhosts` | List all vhosts |
| POST | `/api/vhosts` | Create vhost |
| GET | `/api/vhosts/:id` | Get vhost |
| PUT | `/api/vhosts/:id` | Update vhost |
| DELETE | `/api/vhosts/:id` | Delete vhost (rejected if routes exist) |
| GET | `/api/vhosts/:id/routes` | List routes |
| POST | `/api/vhosts/:id/routes` | Add route |
| PUT | `/api/vhosts/:id/routes/:rid` | Update route |
| DELETE | `/api/vhosts/:id/routes/:rid` | Delete route |
| GET | `/api/vhosts/:id/preview` | Preview rendered server block |

### Bucket Sync

Tracks buckets discovered on the default pool of each vhost. Background sync runs every `BUCKET_SYNC_INTERVAL` seconds.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/vhosts/:id/buckets` | List buckets with routing status (`unrouted`, `routed`, `migrating`, `ignored`). Pass `?fetch_counts=true` to include object counts. |
| POST | `/api/vhosts/:id/buckets/sync` | Trigger an immediate bucket list sync |
| POST | `/api/vhosts/:id/buckets/:bucket/promote` | Promote a bucket: create it on the target pool, add an nginx route, optionally start migration. **If the source bucket has objects, `migrate=true` is required** — routing without migration would make existing data inaccessible. |
| POST | `/api/vhosts/:id/buckets/:bucket/ignore` | Mark a bucket as ignored |
| POST | `/api/vhosts/:id/buckets/:bucket/migrate` | Start (or restart) object migration for a routed bucket (uses rclone engine) |
| GET | `/api/vhosts/:id/buckets/orphans` | Scan routed buckets for orphan objects still on the default pool |
| POST | `/api/vhosts/:id/buckets/:bucket/purge-orphan` | Delete all objects from the default pool copy of a routed bucket |

### Migrations (rclone)

Full bucket migrations using rclone.

Phases: `pending → copying → write_routing → verifying → switching → done`

- **copying**: rclone copies data in a convergence loop (up to `MIGRATION_MAX_COPY_PASSES` passes). Ends early if counts stabilise across passes.
- **write_routing**: nginx is reconfigured so writes go directly to the destination pool while reads still come from the source (with 404-fallback to destination). Freezes new writes to the source.
- **verifying**: a final copy pass + rclone check to confirm source == destination.
- **switching**: the nginx route is flipped to the destination pool and the DB is updated atomically. Fails hard if the route cannot be found — no silent data loss.
- **done**: migration complete. Source data is **never deleted automatically**. The migration record tracks `orphaned_source_pool_id`, `orphaned_source_prefix`, and `orphaned_at` so operators can locate and clean up the source bucket at their own pace.
- **error** / **cancelled**: terminal failure states.

> **Source data is never purged automatically.** When a migration reaches `done`, the orchestrator records where the original data lives (pool + prefix + timestamp). Use `nanio-orchestrator orphaned list` or `GET /api/migrations/orphaned` to review, and delete the source objects manually when ready.

> **A route must exist before starting a migration.** Use `POST /api/vhosts/:id/buckets/:bucket/promote` (Buckets page) to create the bucket route first. The Migrations page only accepts buckets that are already routed — it validates that the route exists and points to `src_pool_id` before creating the migration record.

- **copy** mode (default): additive — only copies objects from source to destination, never deletes at the destination.
- **sync** mode: mirror — destination becomes identical to the source. A pre-flight guard aborts the migration if the source bucket is empty to prevent accidental data loss.

**Pre-flight validation** (at `POST /api/migrations` time):
- Both pools must have at least one enabled member.
- A route `/{bucket}/` must exist in the vhost and point to `src_pool_id`.
- The source bucket must exist and contain at least one object on the source pool.
- No active migration for the same bucket can already be running.

| Method | Endpoint | Description |
|--------|----------|--------------|
| POST | `/api/migrations` | Start a new migration. Body: `{bucket, src_pool_id, dst_pool_id, mode}` where `mode` is `"copy"` (default) or `"sync"`. Requires an nginx route for the bucket pointing to `src_pool_id`. |
| GET | `/api/migrations` | List migrations (filter with `?phase=`) |
| GET | `/api/migrations/stale` | List active migrations that cannot proceed — source pool has no members, or source bucket has disappeared. |
| GET | `/api/migrations/orphaned` | List completed migrations that have orphaned source data pending manual cleanup |
| GET | `/api/migrations/source-buckets` | List buckets available to migrate from a given pool (`?pool_id=`). Returns buckets from `bucket_sync` and routed paths. Used by the Migrations UI to populate the bucket selector. |
| GET | `/api/migrations/:id` | Get migration details (includes `orphaned_source_pool_id`, `orphaned_source_prefix`, `orphaned_at`) |
| POST | `/api/migrations/:id/cancel` | Cancel a running migration |
| GET | `/api/migrations/:id/log` | Get migration log entries |

### Config Operations

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/config/status` | Drift status per file |
| POST | `/api/config/validate` | Run `nginx -t` |
| POST | `/api/config/reload` | Run `nginx -s reload` |
| POST | `/api/config/sync` | Re-import disk state → DB |
| POST | `/api/config/rebuild` | Rebuild all files from DB → disk → reload |
| POST | `/api/config/absorb-file` | Accept a drifted file: import disk state into DB |
| POST | `/api/config/rewrite-file` | Rewrite a single file from DB state + reload |
| GET | `/api/config/preview/pool/:id` | Preview upstream config (no apply) |
| GET | `/api/config/preview/vhost/:id` | Preview server block config (no apply) |
| POST | `/api/config/rebuild-from-disk` | Reconstruct DB from nginx configs + sidecar files (see [DB Resilience](#db-resilience)) |
| POST | `/api/config/backup` | Trigger an immediate database backup |
| GET | `/api/config/settings` | Current effective settings (secrets masked) |
| PUT | `/api/config/settings/:key` | Update a single setting in the config file (takes effect after restart) |
| POST | `/api/config/settings/restart` | Restart the service to apply pending config changes (requires sudoers rule installed by `nanio-orchestrator install`) |

### Health + Audit

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/health` | Health check (no auth required) |
| GET | `/api/audit` | Audit log (`?page=&entity_type=&from=&to=`) |

## How Nginx Config is Managed

### Write Path

Every config change follows this exact sequence:

1. Render new config from DB state (Jinja2 templates)
2. Write to `<file>.tmp`
3. Run `nginx -t` — if it fails: delete `.tmp`, return error, stop
4. `os.rename(<file>.tmp, <file>)` — atomic on POSIX
5. Run `nginx -s reload`
6. Update DB: sha256, content snapshot, audit log with nginx output
7. Trigger a DB backup

### Sidecar Files

Alongside each nginx config file the orchestrator writes a `.meta.json` sidecar containing
data that cannot be reconstructed from the nginx config alone:

```
/etc/nginx/nanio/
├── pools/
│   ├── pool-2025.conf           # upstream block
│   └── pool-2025.meta.json      # type, description, encrypted credentials
└── vhosts/
    ├── s3.xpto.pt.conf          # server block
    └── s3.xpto.pt.meta.json     # default_pool_id + name

/opt/nanio-orchestrator/data/
├── orchestrator.db
├── orchestrator.db.bak
└── migrations/
    ├── migration-7.state.json   # in-progress migration state (alongside DB, not in nginx dir)
    └── migration-7.done.json    # permanent completion record (written when migration reaches 'done')
```

Sidecars are written atomically (`.tmp` → rename) and are the foundation for
[DB resilience](#db-resilience).

### Drift Detection

Background check every `DRIFT_INTERVAL` seconds:
- SHA256 each managed file on disk
- Compare with the last known hash in DB
- If mismatch: alert in dashboard and `GET /api/config/status`
- **Never auto-corrects** — the operator decides

### Pool Types

| Type | Members | Nginx `backup` flag | Description |
|------|---------|---------------------|-------------|
| `nanio` | All `active` | Never | Shared storage — any member handles any request |
| `http` | `primary` + `replica` | Yes, for replicas | Read-only HTTP serve with failover |
| `cold` | `primary` + `replica` | Yes, for replicas | Read-only archive with failover |

### Node Config Generator

Generates config snippets for upstream nodes (rendered only, never deployed):
- **nanio-only**: nanio `options.toml` + systemd unit
- **nginx-only**: nginx server block for file serving
- **nginx-nanio**: both nanio config and nginx proxy config

Access via API or the "Node Setup" button in the Web UI.

## DB Resilience

The database is not the source of truth — the nginx config files and their sidecar files are.
The DB can be fully rebuilt from disk after loss or corruption.

### Automatic Backup

The DB is backed up automatically:
- After every successful nginx reload
- On a periodic timer (`DB_BACKUP_INTERVAL`, default 60 s)
- On demand via `POST /api/config/backup`

Backups are rotated: `.bak`, `.bak.2`, `.bak.3`, … up to `DB_BACKUP_ROTATE` copies.

### Rebuild from Disk

If the DB is lost or corrupted, reconstruct it without downtime (nginx keeps running):

```bash
# Preview what would be imported
nanio-orchestrator rebuild-db --dry-run

# Rebuild (safe — DB must be empty)
nanio-orchestrator rebuild-db

# Rebuild over existing data
nanio-orchestrator rebuild-db --force
```

Or via API:

```bash
curl -X POST http://localhost:8080/api/config/rebuild-from-disk \
  -H "X-Orchestrator-Key: <key>"

# Force over existing data
curl -X POST "http://localhost:8080/api/config/rebuild-from-disk?force=true" \
  -H "X-Orchestrator-Key: <key>"
```

What is recovered:

| Data | Source | Recovered? |
|------|--------|-----------|
| Pools (name, lb_method, keepalive) | `pools/*.conf` | ✓ |
| Pool members | `pools/*.conf` | ✓ |
| Pool type, description | `pools/*.meta.json` | ✓ |
| Encrypted credentials | `pools/*.meta.json` | ✓ |
| Vhosts (server_name, SSL, ports) | `vhosts/*.conf` | ✓ |
| Routes | `vhosts/*.conf` | ✓ |
| Vhost default_pool_id | `vhosts/*.meta.json` | ✓ |
| In-progress migrations | `data/migrations/*.state.json` | ✓ (reset to pending, will auto-resume) |
| Completed migration records | `data/migrations/*.done.json` | ✓ (orphaned source info preserved) |
| config_files sha256 records | recomputed from disk | ✓ |
| bucket_sync | live `ListBuckets` call per pool member | ✓ (best-effort — requires pool members to be reachable) |
| audit_log | — | ✗ (historical only) |

After rebuild, restart the service so migrations resume:

```bash
systemctl restart nanio-orchestrator
```

## Web UI

The web UI is served at `/` and requires a session cookie obtained via `/login`.

| Page | URL | Description |
|------|-----|-------------|
| Dashboard | `/` | Overview: pools, vhosts, drift count, active migrations, unrouted buckets |
| Pools | `/web/pools` | Manage pools and members |
| Vhosts | `/web/vhosts` | Manage vhosts and routes |
| Buckets | `/web/buckets` or via Vhosts page | Bucket list, promote, migrate, orphan scan and purge per vhost |
| Config | `/web/config` | Config file drift status, per-file actions |
| Migrations | `/web/migrations` | Start (copy or sync mode) and monitor rclone migrations. Bucket selector is populated from the source pool's existing routes. Stale migrations (source pool lost members or source bucket disappeared) are flagged. |
| Audit | `/web/audit` | Last 100 audit log entries |
| Settings | `/web/settings` | View all current settings (secrets masked) |

## CLI Reference

```
nanio-orchestrator [serve]                  Start the server (default command)
nanio-orchestrator install                  Production install (run as root)
nanio-orchestrator rebuild-db               Rebuild DB from disk
  --dry-run                                   Preview without writing
  --force                                     Overwrite existing DB data

nanio-orchestrator config show              Print all settings grouped by category
nanio-orchestrator config get <key>         Print the value of a single setting
nanio-orchestrator config set <key> <val>   Write a setting to the config file
nanio-orchestrator config generate-secret   Generate a Fernet key for SECRET
  --set                                       Also write it to the config file
nanio-orchestrator config edit              Open the config file in $EDITOR
nanio-orchestrator config validate          Run nginx -t
nanio-orchestrator config reload            Run nginx -s reload
nanio-orchestrator config rebuild           Regenerate all config files from DB + reload

nanio-orchestrator orphaned list            List all completed migrations with orphaned source data
```

### `config show` example

```
Core
  host                         0.0.0.0                       Bind address
  port                         8080                          Listen port
  api_key                      chan****                       API authentication key
  log_level                    info                          Log level (debug/info/warning/error)
  session_ttl                  28800                         Web UI session duration (seconds)

Database
  db_path                      /opt/.../orchestrator.db      SQLite database file path
  db_backup_path               /opt/.../orchestrator.db.bak  Backup path (default: db_path + .bak)
  ...

Config file (production): /etc/nanio-orchestrator/config.env
```

### `config set` example

```bash
nanio-orchestrator config set api_key mysecretkey
nanio-orchestrator config set log_level debug
nanio-orchestrator config set migration_max_parallel 4
```

Accepts the short key name (without `NANIO_ORCHESTRATOR_` prefix). Updates the active config file in place, handling commented-out lines.

## Offline / Air-gapped Deployment

```bash
# On a machine with internet:
make build    # produces dist/nanio_orchestrator-*.whl

# Copy the wheel to the target server, then:
python3 -m venv /opt/nanio-orchestrator/venv
/opt/nanio-orchestrator/venv/bin/pip install nanio_orchestrator-*.whl
/opt/nanio-orchestrator/venv/bin/nanio-orchestrator install
```

The wheel bundles all dependencies. No internet required on the target server.

## Troubleshooting

### `nginx -t` fails after config change

The orchestrator never applies a config that fails validation. Check the error output
in the API response or audit log. Common causes:
- Missing SSL certificates referenced in the vhost config
- Upstream pool name conflicts with an existing nginx config
- `include /etc/nginx/nanio/pools/*.conf;` and `include /etc/nginx/nanio/vhosts/*.conf;` not added to the `http {}` block in `nginx.conf`

### Drift detected

A file was modified outside the orchestrator. Options:
1. **Accept the change**: `POST /api/config/sync` to import disk state into DB
2. **Restore from DB**: `POST /api/config/rebuild` to overwrite disk with DB state

### Service won't start

```bash
journalctl -u nanio-orchestrator -f    # check logs
nanio-orchestrator config validate     # test nginx config
```

Common causes:
- DB path not writable
- Port 8080 already in use (change `PORT` in config.env)
- Python version too old (requires 3.9+)

### Credentials API returns 500

`SECRET` is not set or is not a valid Fernet key. Generate and set one:

```bash
nanio-orchestrator config generate-secret --set
```

Or manually:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Then: nanio-orchestrator config set secret <generated-key>
```

Restart the service after setting the key.

### API returns 401

All API endpoints (except `/api/health`) require `X-Orchestrator-Key` set to
`NANIO_ORCHESTRATOR_API_KEY`.

### Web UI keeps redirecting to /login

- Cookies blocked? Make sure the browser allows cookies for the host.
- Behind a TLS-terminating proxy? Ensure `X-Forwarded-Proto: https` is forwarded so the `Secure` cookie flag is set correctly.
- Session expired? Default TTL is 8 hours. Increase `SESSION_TTL` if needed.
- API key changed? Old cookies are immediately invalidated; re-login.

## License

MIT
