# Architecture

nanio-orchestrator is a **control-plane tool only**. It manages nginx configuration and validates
it with `nginx -t`. Traffic never flows through the orchestrator — if it stops, nginx keeps serving.

---

## High-level diagram

```
CLIENT (S3 SDK / browser / aws-cli)
│  HTTPS
▼
NGINX (gateway machine)
│  proxy_pass only — no business logic
├─► upstream pool-2025   →  nanio instances  →  /data/2025/
├─► upstream pool-cdn    →  nginx instances  →  /var/www/cdn/
└─► upstream pool-old    →  nanio instances  →  /data/archive/

ORCHESTRATOR (:8080, internal network only)
│  writes files + validates with nginx -t
├─► /etc/nginx/nanio/pools/*.conf          upstream blocks
├─► /etc/nginx/nanio/pools/*.meta.json     sidecar: type, credentials (encrypted)
├─► /etc/nginx/nanio/vhosts/*.conf         server blocks
├─► /etc/nginx/nanio/vhosts/*.conf.disabled  server blocks of disabled vhosts (not loaded by nginx)
├─► /etc/nginx/nanio/vhosts/*.meta.json    sidecar: default pool, extra_blocks, ip_rules
├─► SQLite: /opt/nanio-orchestrator/data/orchestrator.db
├─► SQLite backup:        orchestrator.db.bak  (rotated copies)
└─► Migration state:      data/migrations/*.state.json
```

---

## Components

### FastAPI application (`app.py`)

The HTTP server. Provides:
- REST API under `/api/`
- The web panel on every other path (see below)
- Auth middleware: everything under `/api/` needs the API key header or the session cookie,
  except the health check, the API documentation pages and the session endpoints
- Startup tasks: DB init, recovery of interrupted migrations, and three background loops (drift detection, bucket sync, database backup)

### Config generation (`nginx/generator.py`)

Renders Jinja2 templates to produce nginx config text. Two template types:
- `upstream.conf.j2` → pool upstream blocks
- `vhost.conf.j2` → server blocks with all routes, IP rules, extra blocks

**All config writes go through `nginx/apply.py`**: write the file at its final path, run `nginx -t`,
restore the previous content on failure (nginx only includes `*.conf`, so a `.tmp` candidate would
not be validated). CRUD operations (pools, vhosts, routes, members) write and validate the config but do
**not** reload nginx — the operator applies pending changes explicitly via `POST /api/config/reload`
or *Reload nginx* in the web panel. Only the migration engine reloads nginx on its own, during
the `write_routing` and `switching` phases.

### What is waiting for a reload (`nginx/applied.py`)

Every successful reload records the sha256 of each managed `.conf` in
`<nginx_config_dir>/.applied.json`. Comparing the files on disk with that record gives the
changes that nginx has not loaded yet (`pending_reload` and `pending_files` in
`GET /api/config/status`). The record is a file, not a database row, because the CLI reloads
from another process and the record must survive a database rebuild. A reload done without the
orchestrator is not seen; before the first recorded reload the answer is "unknown", not "no".

### Web panel (`ui/` → `web/dist`, served by `web/spa.py`)

A single-page application: React, TypeScript and Vite, with hand-written CSS and no runtime
dependency loaded from the network, so it works on isolated networks. `make ui` builds it into
`nanio_orchestrator/web/dist`, which is package data: a wheel and the Docker image contain it,
and only a source checkout needs Node.js. FastAPI serves the files of the build and answers
every other non-API path with the application shell (the application does its own routing).

The shell and its assets are public and contain no data. The application signs in with
`POST /api/auth/login`, which sets the `HttpOnly` session cookie; from then on it calls the same
REST API as any other client. It keeps no state of its own: what it shows is polled from the
API, so two operators see the same thing.

### Explorer (`api/explorer.py`)

Read-only S3 browsing for the operator: list a pool's buckets and one level of a bucket, read
an object's metadata, download an object up to 64 MiB. It asks one enabled member of the pool,
with the pool's stored credentials, when an operator asks: management traffic, like bucket
discovery. It never writes to a pool, and client traffic still does not pass through the
orchestrator. `locate` answers which pool serves a bucket or folder according to a vhost's
routes, which is what lets the panel show where data really is.

### Sidecar files (`sidecar.py`)

Each nginx `.conf` file has a companion `.meta.json` sidecar containing metadata that nginx
doesn't store in its config syntax:
- **Pool sidecars**: `type`, `description`, `credentials` (Fernet-encrypted)
- **Vhost sidecars**: `default_pool_id`, `default_pool_name`, `extra_blocks_json`, `ip_rule_mode`, `ip_rule_ips_json`

Sidecars are the source of truth for DB rebuild. They are updated every time the DB is written.

### Database (`db.py`)

SQLite with WAL mode and foreign keys enabled. Schema:

| Table | Purpose |
|-------|---------|
| `pools` | Pool definitions (name, type, lb_method, keepalive) |
| `pool_members` | Pool member addresses with weight/enabled |
| `vhosts` | Vhost definitions (server_name, SSL, extra_blocks, ip_rules) |
| `routes` | Named path-prefix → pool mappings per vhost |
| `bucket_sync` | Discovered buckets per vhost with status |
| `migrations` | Migration records with phase, progress, orphan tracking |
| `migration_log` | Per-migration phase change log |
| `config_files` | Tracked config files with sha256 for drift detection |
| `audit_log` | All mutation events with before/after state |
| `credentials` | Fernet-encrypted pool credentials |

### Rebuild from disk (`rebuild.py`)

Reconstructs the entire database from:
1. `pools/*.conf` → pool names, members, lb_method
2. `pools/*.meta.json` → pool type, description, credentials
3. `vhosts/*.conf` and `vhosts/*.conf.disabled` → server_name, SSL, routes; the file name says
   whether the vhost is enabled (it wins over the sidecar, with a warning when they disagree)
4. `vhosts/*.meta.json` → default_pool, extra_blocks, ip_rules
5. `migrations/*.state.json` → in-progress migration state
6. Live `ListBuckets` calls → bucket_sync state

This means **the database is fully recoverable from disk**. The nginx config + sidecar files
are the authoritative source of truth. The DB is a derived cache.

### Migration engine (`migration_engine.py`)

State machine using rclone for data movement between pools:

```
pending
  │
  ▼ rclone copy (src → dst, all objects)
copying
  │
  ▼ split routing: dst authoritative (writes → dst, reads → dst then src on 404, DELETE mirrored)
write_routing
  │
  ▼ loop: copy --update → one-way check → retry while unresolved objects decrease
verifying
  │
  ▼ point the route at dst; remove the split
switching
  │
  ▼ mark src as orphaned (data still present, tracking added)
done
```

The split is always entered and is stored in `migrations.split_routing` (also in the migration
state file, so `rebuild-from-disk` restores it). It stays in place if the migration fails or is
cancelled, until a migration between the same two pools completes.

### Bucket sync (`bucket_sync.py`)

Background task that runs every `BUCKET_SYNC_INTERVAL` seconds. For each vhost with a
`nanio` default pool, it calls `ListBuckets` and records discovered buckets in the `bucket_sync`
table with status `unrouted`, `routed`, `migrating`, `ignored` or `deleted`.

It also checks that each routed bucket exists on its target pool. One that does not is flagged
(`missing_since`); the route is never removed automatically.

Only `nanio` pools have S3 semantics — `http` pools are silently skipped.

### S3 client (`s3client.py`)

Custom async S3 HTTP client using stdlib only (no boto3/aiobotocore). Implements:
- SigV4 request signing
- `ListBuckets`, `ListObjectsV2`, `CreateBucket`, `DeleteObject`, `HeadBucket`
- Streaming response handling

---

## Security model

| Concern | Mechanism |
|---------|-----------|
| API authentication | API key (header or cookie session) |
| Session management | Signed session cookie with TTL |
| Credential storage | Fernet symmetric encryption at rest |
| nginx config safety | `nginx -t` gate on every write; atomic writes with rollback |
| IP access control | Per-vhost `allow`/`deny` rules in generated nginx config |
| Internal-only API | The orchestrator should not be exposed to the internet |

The orchestrator is designed for **internal network use only**. It should sit behind a firewall
or be accessible only from trusted management machines. The panel and the API have no rate
limiting, and the panel key can read object contents through the Explorer.

---

## Drift detection

The `drift.py` module compares sha256 hashes of managed config files on disk against the
sha256 stored in the `config_files` table. Drift is reported in:
- `GET /api/config/status` (API)
- The Overview and Configuration screens of the web panel

Causes of drift:
- Manual edits to nginx files
- External tools modifying managed configs
- Partial writes interrupted before completion

Resolution: `POST /api/config/sync` re-writes all drifted files from DB state.
