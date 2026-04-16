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
├─► /etc/nginx/nanio/pools/*.conf     (upstream blocks)
├─► /etc/nginx/nanio/vhosts/*.conf    (server blocks, proxy_pass only)
├─► SQLite at /opt/nanio-orchestrator/data/orchestrator.db
└─► Web UI + REST API
```

## Quick Start — Production

### Method A: uv (preferred)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone <repo-url> /tmp/nanio-orchestrator
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

### Method D: bootstrap script (bare server)

```bash
bash scripts/bootstrap.sh --prod --source /path/to/nanio-orchestrator
```

After install, follow the printed instructions to configure and start the service.

## Quick Start — Development

```bash
git clone <repo-url>
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

Dev mode auto-detected when `dev.env` exists or `DEV=true` is set. In dev mode:
- DB at `./dev-data/orchestrator.db`
- Nginx config at `./dev-data/nginx/`
- All nginx commands are **dry-run** (printed, not executed)
- uvicorn `--reload` enabled
- Default API key: `dev`

## Configuration Reference

All settings via `/etc/nanio-orchestrator/config.env` (production) or `dev.env` (development):

| Variable | Default | Description |
|----------|---------|-------------|
| `NANIO_ORCHESTRATOR_HOST` | `0.0.0.0` | Bind address |
| `NANIO_ORCHESTRATOR_PORT` | `8080` | Listen port |
| `NANIO_ORCHESTRATOR_API_KEY` | `changeme` | API authentication key |
| `NANIO_ORCHESTRATOR_DB_PATH` | `/opt/nanio-orchestrator/data/orchestrator.db` | SQLite database path |
| `NANIO_ORCHESTRATOR_NGINX_CONFIG_DIR` | `/etc/nginx/nanio` | Directory for generated nginx configs |
| `NANIO_ORCHESTRATOR_LOG_LEVEL` | `info` | Log level (debug, info, warning, error) |
| `NANIO_ORCHESTRATOR_DRIFT_INTERVAL` | `60` | Drift check interval in seconds |
| `NANIO_ORCHESTRATOR_SESSION_TTL` | `28800` | Web UI session duration in seconds (8 hours) |

## Authentication

nanio-orchestrator uses two separate auth schemes depending on the client:

| Client | Method | Details |
|--------|--------|---------|
| **API** (`/api/*`) | `X-Orchestrator-Key` header | Pass the API key as a request header; missing/wrong key returns `401 {"detail": "..."}`  |
| **Web UI** (`/web/*`, `/`) | Session cookie | Log in at `/login` with the API key; an HMAC-signed `nanio_session` cookie is issued with a configurable TTL |

Public endpoints (no auth required): `/api/health`, `/api/docs`, `/api/redoc`, `/api/openapi.json`, `/login`, `/logout`, `/static/*`.

## API Reference

All API endpoints (`/api/*`) require the `X-Orchestrator-Key` header (except `/api/health`).

### Pools

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/pools` | List all pools |
| POST | `/api/pools` | Create pool |
| GET | `/api/pools/:id` | Get pool details |
| PUT | `/api/pools/:id` | Update pool |
| DELETE | `/api/pools/:id` | Delete pool (rejects if routes reference it) |
| GET | `/api/pools/:id/members` | List pool members |
| POST | `/api/pools/:id/members` | Add member to pool |
| PUT | `/api/pools/:id/members/:mid` | Update member |
| DELETE | `/api/pools/:id/members/:mid` | Remove member |
| GET | `/api/pools/:id/members/:mid/node-config` | Generate node config (GET with query params) |
| POST | `/api/pools/:id/members/:mid/node-config` | Generate node config (POST with body) |
| GET | `/api/pools/:id/node-config-summary` | Node config summary for all members |

### Vhosts + Routes

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/vhosts` | List all vhosts |
| POST | `/api/vhosts` | Create vhost |
| GET | `/api/vhosts/:id` | Get vhost details |
| PUT | `/api/vhosts/:id` | Update vhost |
| DELETE | `/api/vhosts/:id` | Delete vhost (rejects if routes exist) |
| GET | `/api/vhosts/:id/routes` | List routes for vhost |
| POST | `/api/vhosts/:id/routes` | Add route |
| PUT | `/api/vhosts/:id/routes/:rid` | Update route |
| DELETE | `/api/vhosts/:id/routes/:rid` | Delete route |
| GET | `/api/vhosts/:id/preview` | Preview vhost config |

### Config Operations

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/config/status` | Drift status per file, last reload info |
| POST | `/api/config/validate` | Run `nginx -t` |
| POST | `/api/config/reload` | Run `nginx -s reload` |
| POST | `/api/config/sync` | Re-import disk state → DB |
| POST | `/api/config/rebuild` | Rebuild all files from DB → disk → reload |
| GET | `/api/config/preview/pool/:id` | Preview upstream config |
| GET | `/api/config/preview/vhost/:id` | Preview server block config |

### Health + Audit

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/health` | Health check (no auth required) |
| GET | `/api/audit` | Audit log with filters (?page=&entity_type=&from=&to=) |

## Offline / Air-gapped Deployment

For servers without internet access:

```bash
# On a machine with internet:
make build    # produces dist/nanio_orchestrator-*.whl

# Copy the wheel to the target server, then:
python3 -m venv /opt/nanio-orchestrator/venv
/opt/nanio-orchestrator/venv/bin/pip install nanio_orchestrator-*.whl
/opt/nanio-orchestrator/venv/bin/nanio-orchestrator install
```

The wheel bundles all dependencies. No internet required on the target server.

## How Nginx Config is Managed

### Write Path

Every config change follows this exact sequence:

1. Render new config from DB state
2. Write to `<file>.tmp`
3. Run `nginx -t` — if fails: delete `.tmp`, return error, stop
4. `os.rename(<file>.tmp, <file>)` — atomic on POSIX
5. Run `nginx -s reload`
6. Update DB: sha256, content_snapshot, audit_log with nginx output

### Config Files on Disk = Source of Truth

The orchestrator generates config under `/etc/nginx/nanio/`:

```
/etc/nginx/nanio/
├── pools/
│   ├── pool-2025.conf        # upstream blocks
│   └── pool-cdn.conf
└── vhosts/
    ├── s3.xpto.pt.conf       # server blocks (proxy_pass only)
    └── cdn.xpto.pt.conf
```

### Drift Detection

Background check every 60 seconds:
- SHA256 each managed file on disk
- Compare with last known hash in DB
- If mismatch: alert in dashboard and `/api/config/status`
- **Never auto-corrects** — operator decides

### Startup Reconciliation

On start, the orchestrator reads all files under `/etc/nginx/nanio/` with the
`# managed by nanio-orchestrator` marker and reconciles with the DB.

### Pool Types

| Type | Members | Nginx `backup` | Description |
|------|---------|-----------------|-------------|
| `nanio` | All `active` | Never | Shared storage — any member handles any request |
| `http` | `primary` + `replica` | Yes, for replicas | Read-only HTTP serve with failover |
| `cold` | `primary` + `replica` | Yes, for replicas | Read-only archive with failover |

### Node Config Generator

The orchestrator can generate config snippets for upstream nodes (not deployed — just rendered):
- **nanio-only**: nanio options.toml + systemd unit
- **nginx-only**: nginx server block for file serving
- **nginx-nanio**: both nanio config and nginx proxy config

Access via API or the "Node Setup" button in the Web UI.

## Troubleshooting

### `nginx -t` fails after config change

The orchestrator never applies a config that fails validation. Check the error output
in the API response or audit log. Common causes:
- Missing SSL certificates (referenced in vhost config)
- Upstream pool name conflict with existing nginx config
- `include /etc/nginx/nanio/pools/*.conf;` not added to `nginx.conf`

### Drift detected

A file was modified outside the orchestrator. Options:
1. **Accept the change**: `POST /api/config/sync` to import disk state
2. **Restore from DB**: `POST /api/config/rebuild` to overwrite with DB state

### Service won't start

```bash
journalctl -u nanio-orchestrator -f    # check logs
nanio-orchestrator config validate     # test nginx config
```

Common causes:
- DB path not writable
- Port 8080 already in use (change in config.env)
- Python version too old (need 3.9+)

### API returns 401

All API endpoints (except `/api/health`) require the `X-Orchestrator-Key` header.
Set it to the value of `NANIO_ORCHESTRATOR_API_KEY` in your config.

### Web UI keeps redirecting to /login

- Cookies blocked? Make sure the browser allows cookies for the host.
- Accessing over HTTP behind a TLS-terminating proxy? Ensure `X-Forwarded-Proto: https` is forwarded so the `Secure` flag is set correctly on the cookie.
- Session expired? Default TTL is 8 hours (`NANIO_ORCHESTRATOR_SESSION_TTL=28800`). Increase if needed.
- API key changed? Old cookies become invalid immediately; re-login.

## License

MIT
