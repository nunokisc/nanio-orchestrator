# API Reference

All endpoints require authentication via:
- **Header**: `X-API-Key: <api_key>`
- **Cookie**: `nanio_session=<token>` (obtained via `POST /auth/login`)

The only unauthenticated endpoint is `GET /health`.

Base URL: `http://localhost:8080` (or wherever the orchestrator listens).

---

## Authentication

### `POST /auth/login`
```json
{ "api_key": "your-api-key" }
```
Sets a `nanio_session` cookie valid for `SESSION_TTL` seconds. Returns `{"ok": true}`.

### `POST /auth/logout`
Clears the session cookie.

---

## Pools

Pools map to nginx `upstream {}` blocks. A pool contains one or more member addresses.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/pools` | List all pools |
| POST | `/api/pools` | Create a pool |
| GET | `/api/pools/:id` | Get a pool |
| PUT | `/api/pools/:id` | Update a pool |
| DELETE | `/api/pools/:id` | Delete a pool (fails if vhosts reference it) |
| GET | `/api/pools/:id/members` | List members |
| POST | `/api/pools/:id/members` | Add a member |
| PUT | `/api/pools/:id/members/:mid` | Update a member |
| DELETE | `/api/pools/:id/members/:mid` | Remove a member |
| GET | `/api/pools/:id/members/:mid/node-config` | Get stored node config |
| POST | `/api/pools/:id/members/:mid/node-config` | Generate node config |
| GET | `/api/pools/:id/node-config-summary` | Node config summary for all members |
| GET | `/api/pools/:id/buckets/status` | Per-bucket routing status (nanio pools only) |

### Pool create/update body
```json
{
  "name": "pool-2025",
  "description": "2025 storage tier",
  "type": "nanio",
  "lb_method": "least_conn",
  "keepalive": 32
}
```
`type`: `"nanio"` (S3-compatible) or `"http"` (plain HTTP, read-only).
`lb_method`: `"least_conn"`, `"round_robin"`, or `"ip_hash"`.

### Member create/update body
```json
{
  "address": "10.0.0.1:9000",
  "weight": 1,
  "enabled": true
}
```

### `GET /api/pools/:id/buckets/status`
Lists all buckets on a `nanio` pool via S3 `ListBuckets` and annotates each with its routing status:

| Status | Meaning |
|--------|---------|
| `routed` | A dedicated nginx route in some vhost explicitly points to this pool |
| `via_default` | No dedicated route; this pool is the `default_pool` for at least one vhost |
| `orphaned` | A migration record marks this pool as the stale source (leftover data) |
| `unrouted` | Bucket exists but no vhost serves traffic to it from this pool |

---

## Vhosts

Vhosts map to nginx `server {}` blocks.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/vhosts` | List all vhosts with their routes |
| POST | `/api/vhosts` | Create a vhost |
| GET | `/api/vhosts/:id` | Get a vhost |
| PUT | `/api/vhosts/:id` | Update a vhost |
| DELETE | `/api/vhosts/:id` | Delete a vhost and all its routes |
| GET | `/api/vhosts/:id/routes` | List routes |
| POST | `/api/vhosts/:id/routes` | Add a route |
| PUT | `/api/vhosts/:id/routes/:rid` | Update a route |
| DELETE | `/api/vhosts/:id/routes/:rid` | Delete a route |
| GET | `/api/vhosts/:id/preview` | Preview rendered server block |

### Vhost create body
```json
{
  "server_name": "s3.example.pt",
  "listen_port": 443,
  "ssl": true,
  "ssl_cert_path": "/etc/ssl/s3.example.pt/fullchain.pem",
  "ssl_key_path": "/etc/ssl/s3.example.pt/privkey.pem",
  "default_pool_id": 1,
  "enabled": true,
  "extra_directives": null,
  "extra_blocks": null,
  "ip_rule_mode": null,
  "ip_rule_ips": null
}
```

### IP Access Control
```json
{
  "ip_rule_mode": "allow",
  "ip_rule_ips": ["10.0.0.0/8", "192.168.1.5"]
}
```
`ip_rule_mode`:
- `"allow"` — only listed IPs may access; all others get `deny all`
- `"deny"` — listed IPs are blocked; all others are allowed
- `null` — no IP restrictions

### Extra nginx blocks
```json
{
  "extra_blocks": [
    { "zone": "top",   "content": "add_header X-Frame-Options SAMEORIGIN;" },
    { "zone": "ssl",   "content": "ssl_stapling on;" },
    { "zone": "proxy", "content": "proxy_read_timeout 300s;" },
    { "zone": "end",   "content": "# footer" }
  ]
}
```
Zones: `top` (after `server_name`), `ssl` (after SSL certs), `proxy` (after proxy settings), `end` (before closing `}`).

### Route create body
```json
{
  "path_prefix": "/bucket-name/",
  "pool_id": 2,
  "key_prefix": null,
  "extra_directives": null
}
```
`key_prefix`: if set, rewrites the S3 key — e.g. `path_prefix=/archive/` + `key_prefix=2024/` rewrites `PUT /archive/file.jpg` → `PUT /2024/file.jpg` on the upstream.

---

## Buckets

Tracks S3 buckets discovered on each vhost's default pool.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/vhosts/:id/buckets` | List buckets (`?fetch_counts=true` for object counts) |
| POST | `/api/vhosts/:id/buckets/sync` | Trigger immediate sync |
| POST | `/api/vhosts/:id/buckets/:bucket/promote` | Create route + optional migration |
| POST | `/api/vhosts/:id/buckets/:bucket/ignore` | Mark as ignored |
| GET | `/api/vhosts/:id/buckets/orphans` | Scan for orphan content on default pool |
| POST | `/api/vhosts/:id/buckets/:bucket/purge-orphan` | Delete orphan objects from default pool |

### Promote request body
```json
{
  "pool_id": 2,
  "migrate": true,
  "allow_orphan": false
}
```
- `migrate: true` — starts an rclone migration from default pool → target pool
- `allow_orphan: true` — route without migration even if bucket has objects on the source pool

---

## Migrations

rclone-based data migration between pools.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/migrations` | List all migrations |
| POST | `/api/migrations` | Start a migration |
| GET | `/api/migrations/:id` | Get migration detail + log |
| POST | `/api/migrations/:id/cancel` | Cancel a running migration |

### Migration create body
```json
{
  "bucket": "my-bucket",
  "src_pool_id": 1,
  "dst_pool_id": 2,
  "mode": "copy"
}
```
`mode`: `"copy"` (default) or `"sync"` (deletes from dst what's not in src — destructive).

### Migration phases
`pending → copying → write_routing → verifying → switching → done`

The `verifying` phase runs a convergence loop — it copies, checks, and retries while diff count
decreases. This handles uploads happening during migration.

---

## Config

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/config/status` | List all managed config files with drift status |
| POST | `/api/config/validate` | Run `nginx -t` |
| POST | `/api/config/reload` | Reload nginx (`nginx -s reload`) |
| POST | `/api/config/rebuild` | Re-generate all config files from DB and reload |
| POST | `/api/config/sync` | Write any DB-tracked file that is missing or drifted on disk |
| POST | `/api/config/rebuild-from-disk` | Reconstruct DB from nginx configs + sidecars |
| POST | `/api/config/absorb` | Adopt a manually-edited file into DB tracking |
| GET | `/api/config/preview/pool/:id` | Preview upstream block |
| GET | `/api/config/preview/vhost/:id` | Preview server block |
| POST | `/api/config/backup` | Trigger a manual DB backup |
| GET | `/api/config/settings` | Get current settings (secrets masked) |
| POST | `/api/config/restart` | Restart the orchestrator service via systemctl |

### `POST /api/config/rebuild-from-disk`
Query params:
- `dry_run=true` — report what would be imported without writing (safe to call anytime)
- `force=true` — wipe existing DB before rebuilding (default: skip existing records)

---

## Credentials

Pool credentials are stored encrypted (Fernet). Requires `NANIO_ORCHESTRATOR_SECRET` to be set.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/pools/:id/credentials` | Get credentials (values masked) |
| PUT | `/api/pools/:id/credentials` | Set credentials |
| DELETE | `/api/pools/:id/credentials` | Remove credentials |

### Credentials body
```json
{
  "access_key": "minioadmin",
  "secret_key": "minioadmin"
}
```

---

## Audit Log

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/audit` | Last 100 audit entries (most recent first) |

---

## Health

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check (no authentication required) |

Returns `{"status": "ok", "version": "x.y.z"}`.
