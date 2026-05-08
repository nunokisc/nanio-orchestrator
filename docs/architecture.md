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
- Server-rendered web UI under `/web/`
- Auth middleware (API key + session cookie)
- Startup tasks: DB init, background bucket sync, background backup

### Config generation (`nginx/generator.py`)

Renders Jinja2 templates to produce nginx config text. Two template types:
- `upstream.conf.j2` → pool upstream blocks
- `vhost.conf.j2` → server blocks with all routes, IP rules, extra blocks

**All config writes are atomic** (write to `.tmp`, run `nginx -t`, rename on success, rollback on
failure). CRUD operations (pools, vhosts, routes, members) write and validate the config but do
**not** reload nginx — the operator applies pending changes explicitly via `POST /api/config/reload`
or the Config tab in the Web UI. Only the migration engine reloads nginx automatically during the
`write_routing` and `switching` phases, because those transitions are autonomous and time-sensitive.

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
| `node_configs` | Generated node configs per pool member |
| `audit_log` | All mutation events with before/after state |
| `credentials` | Fernet-encrypted pool credentials |

### Rebuild from disk (`rebuild.py`)

Reconstructs the entire database from:
1. `pools/*.conf` → pool names, members, lb_method
2. `pools/*.meta.json` → pool type, description, credentials
3. `vhosts/*.conf` → server_name, SSL, routes
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
  ▼ add dedicated route for writes on dst; reads still go to src
write_routing
  │
  ▼ convergence loop: copy → check → retry while diff > 0
verifying
  │
  ▼ switch reads to dst; remove live migration split
switching
  │
  ▼ mark src as orphaned (data still present, tracking added)
done
```

The verifying phase uses a convergence loop (up to `MIGRATION_MAX_COPY_PASSES` iterations)
to handle files uploaded during migration — it retries as long as the diff count decreases.

### Bucket sync (`bucket_sync.py`)

Background task that runs every `BUCKET_SYNC_INTERVAL` seconds. For each vhost with a
`nanio` default pool, it calls `ListBuckets` and records discovered buckets in the `bucket_sync`
table with status `unrouted`, `routed`, `migrating`, or `ignored`.

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
or be accessible only from trusted management machines. The web UI and API have no rate limiting.

---

## Drift detection

The `drift.py` module compares sha256 hashes of managed config files on disk against the
sha256 stored in the `config_files` table. Drift is reported in:
- `GET /api/config/status` (API)
- The Config page in the web UI

Causes of drift:
- Manual edits to nginx files
- External tools modifying managed configs
- Partial writes interrupted before completion

Resolution: `POST /api/config/sync` re-writes all drifted files from DB state.
