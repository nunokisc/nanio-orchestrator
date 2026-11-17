# API reference

Base URL: `http://<orchestrator-host>:8080`. Request and response bodies are JSON.

An interactive version generated from the code is served by the orchestrator itself at
`/api/docs` (Swagger UI) and `/api/redoc`.

## Authentication

| Client | How |
|--------|-----|
| API | `X-Orchestrator-Key: <api key>` header on every `/api/*` request |
| Web UI | `POST /login` (form field `key`) sets the `nanio_session` cookie, valid for `SESSION_TTL` seconds. The cookie is also accepted on `/api/*`. `POST /logout` clears it. |

No authentication: `GET /api/health`, `/api/docs`, `/api/redoc`, `/login`, `/static/*`.
A missing or wrong key returns `401`.

```bash
curl -H "X-Orchestrator-Key: $KEY" http://localhost:8080/api/pools
```

Changes to pools, members, vhosts and routes are written and validated with `nginx -t` but
**not applied** until `POST /api/config/reload`. If nginx rejects a change, the previous file is
restored and the response carries the nginx output.

---

## Health

`GET /api/health`

```json
{"status": "ok", "version": "0.5.0", "dev_mode": false, "db_ok": true,
 "nginx_config_dir": "/etc/nginx/nanio", "drift_alerts": 0, "missing_buckets": 0}
```

`status` is `degraded` when the database cannot be read. `drift_alerts` is the number of
managed files that differ from what the orchestrator wrote. `missing_buckets` is the number of
routed buckets whose pool reports that the bucket does not exist.

---

## Pools

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/pools` | List pools |
| POST | `/api/pools` | Create a pool |
| GET | `/api/pools/{id}` | Get a pool |
| PUT | `/api/pools/{id}` | Update a pool (any subset of the fields) |
| DELETE | `/api/pools/{id}` | Delete a pool; refused while routes or vhosts reference it |

```json
{
  "name": "pool-2026",
  "description": "2026 buckets",
  "type": "nanio",
  "lb_method": "least_conn",
  "keepalive": 32,
  "source_nanio_pool_id": null
}
```

| Field | Values | Default |
|-------|--------|---------|
| `name` | letters, digits, `.`, `_`, `-` (it becomes the nginx upstream name) | required |
| `type` | `nanio`, `http` | `nanio` |
| `lb_method` | `least_conn`, `round_robin`, `ip_hash` | `least_conn` |
| `keepalive` | idle keepalive connections to the pool | `32` |
| `source_nanio_pool_id` | `http` pools only: the nanio pool whose files this pool serves | `null` |

### Members

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/pools/{id}/members` | List members |
| POST | `/api/pools/{id}/members` | Add a member |
| PUT | `/api/pools/{id}/members/{mid}` | Update a member |
| DELETE | `/api/pools/{id}/members/{mid}` | Remove a member |

```json
{"address": "10.0.1.11:9000", "role": "active", "weight": 1, "max_fails": 3, "fail_timeout_s": 30, "enabled": true}
```

`role`: `active` for `nanio` pools (the only value allowed); `primary` or `replica` for `http`
pools. A `replica` is written to the upstream as a `backup` server.

### Credentials (`nanio` pools only)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/pools/{id}/credentials` | Stored credentials; the access key is masked, the secret is never returned. 404 when none are stored. |
| PUT | `/api/pools/{id}/credentials` | Store or replace |
| DELETE | `/api/pools/{id}/credentials` | Remove |

```json
{"access_key": "AKIA...", "secret_key": "...", "region": "us-east-1", "endpoint_url": null}
```

Requires `NANIO_ORCHESTRATOR_SECRET`. On an `http` pool these endpoints return `400`.

### Buckets of a pool (`nanio` pools only)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/pools/{id}/buckets/status` | Buckets on the pool with their routing status (`routed`, `via_default`, `orphaned`, `unrouted`) and the vhosts serving them |
| GET | `/api/pools/{id}/buckets/{bucket}/objects` | List the objects of a bucket on this pool |
| POST | `/api/pools/{id}/buckets/{bucket}/purge` | **Delete every object** of the bucket on this pool. Irreversible. |

### Node config

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/pools/{id}/members/{mid}/node-config` | Generate everything needed to set up the machine behind this member. Nothing is deployed or stored. |

| Field | Default | Meaning |
|-------|---------|---------|
| `node_type` | required | `nanio-only` or `nginx-nanio` for a `nanio` pool; `nginx-only` for an `http` pool |
| `data_dir` | `/data` | Where the objects live on the node |
| `nanio_port` | from the member address | Port nanio listens on. For `nginx-nanio`, the loopback port behind nginx (default 9000). |
| `nanio_host` | `0.0.0.0` | Bind address (forced to `127.0.0.1` for `nginx-nanio`) |
| `nanio_region` | `us-east-1` | Region nanio reports |
| `access_key`, `secret_key` | none | Written to the node's options file. Use the pool's credentials. |
| `nanio_bin` | `/opt/nanio/bin/nanio` | Path of the nanio binary on the node |

Response: `files` (path + content), `instructions` (the commands to run on the node, in order)
and `warnings` (for example a port that does not match the member address, or missing
credentials). See [operations.md](operations.md#set-up-a-storage-node).

---

## Vhosts

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/vhosts` | List vhosts |
| POST | `/api/vhosts` | Create a vhost |
| GET | `/api/vhosts/{id}` | Get a vhost |
| PUT | `/api/vhosts/{id}` | Update a vhost |
| DELETE | `/api/vhosts/{id}` | Delete a vhost together with its routes |
| GET | `/api/vhosts/{id}/preview` | The server block that would be generated |

```json
{
  "server_name": "s3.example.com",
  "listen_port": 443,
  "ssl": true,
  "ssl_cert_path": "/etc/ssl/s3.example.com/fullchain.pem",
  "ssl_key_path": "/etc/ssl/s3.example.com/privkey.pem",
  "default_pool_id": 1,
  "enabled": true,
  "extra_blocks": [{"zone": "proxy", "content": "proxy_read_timeout 300s;"}],
  "ip_rule_mode": "allow",
  "ip_rule_ips": ["10.0.0.0/8", "192.168.1.5"]
}
```

| Field | Notes |
|-------|-------|
| `ssl` | Default `true`; then both certificate paths are required |
| `listen_port` | Default `443` |
| `default_pool_id` | Pool for `location /`. Its type fixes the type every route of the vhost must use. |
| `extra_blocks` | Raw nginx directives. `zone`: `top` (after `server_name` and IP rules), `ssl` (after the certificate lines), `proxy` (after the proxy buffering lines), `end` (before the closing brace) |
| `ip_rule_mode` | `allow`: only the listed addresses, then `deny all`. `deny`: the listed addresses are blocked. `null`: no restriction. |
| `ip_rule_ips` | IPv4, IPv6, or CIDR; validated on write |

### Routes

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/vhosts/{id}/routes` | List routes |
| POST | `/api/vhosts/{id}/routes` | Add a route |
| PUT | `/api/vhosts/{id}/routes/{rid}` | Update a route |
| DELETE | `/api/vhosts/{id}/routes/{rid}` | Delete a route |

```json
{"path_prefix": "/photos/", "pool_id": 2, "enabled": true}
```

`path_prefix` is `/bucket/` for a whole bucket or `/bucket/folder/` for one folder of it
(see [operations.md](operations.md#send-one-folder-of-a-bucket-to-another-pool)).

- A bucket route does not create the bucket on the target pool and does not move any object.
- A folder route is refused with 409 if the folder already has objects on the bucket's own pool
  (`"allow_orphan": true` overrides). It creates the bucket on the folder's pool. The response
  carries `warnings` (a list) for anything that needs attention.
- `PUT` cannot change a route's path, nor the pool of a folder route.
- `DELETE` of a folder route is refused with 409 while the folder has objects on its pool
  (`?allow_orphan=true` overrides).

---

## Buckets of a vhost

Only for vhosts whose default pool is of type `nanio`.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/vhosts/{id}/buckets` | Discovered buckets with status. `missing_since` is set when a routed bucket does not exist on its pool. `?fetch_counts=true` adds object counts. |
| POST | `/api/vhosts/{id}/buckets/sync` | Discover buckets now |
| POST | `/api/vhosts/{id}/buckets/{bucket}/promote` | Route the bucket to a pool, optionally migrating it |
| POST | `/api/vhosts/{id}/buckets/{bucket}/migrate` | Start or restart the migration of a routed bucket |
| POST | `/api/vhosts/{id}/buckets/{bucket}/ignore` | Hide the bucket from the unrouted list |
| DELETE | `/api/vhosts/{id}/buckets/{bucket}/route` | Remove the bucket's route (back to the default pool) |
| GET | `/api/vhosts/{id}/buckets/orphans` | Routed buckets that still have objects on the default pool |
| POST | `/api/vhosts/{id}/buckets/{bucket}/purge-orphan` | **Delete** that leftover copy on the default pool. Irreversible. |

Status values: `unrouted`, `routed`, `migrating`, `ignored`, `deleted`.

Promote body:

| Field | Default | Meaning |
|-------|---------|---------|
| `pool_id` | required | Target pool |
| `migrate` | `false` | Also move the objects (starts a migration) |
| `allow_orphan` | `false` | Route to a different pool without migrating even though the bucket has objects. They stay on the old pool and are no longer reachable through this vhost. |

Without `migrate`, routing a non-empty bucket to a different pool returns `400` unless
`allow_orphan` is set.

### Buckets on `http` vhosts

For a vhost whose default pool is an `http` pool linked to a nanio pool.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/vhosts/{id}/http-bucket-routes` | Current routes and the buckets available on the linked nanio pool |
| POST | `/api/vhosts/{id}/http-bucket-routes/{bucket}` | Add `/{bucket}/` pointing at the vhost's default pool |
| DELETE | `/api/vhosts/{id}/http-bucket-routes/{bucket}` | Remove it |

---

## Migrations

See [migrations.md](migrations.md) for the behaviour.

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/migrations` | Start a migration |
| GET | `/api/migrations` | List; `?phase=` filters |
| GET | `/api/migrations/{id}` | One migration |
| GET | `/api/migrations/{id}/log` | Log lines, newest first; `?limit=` (default 100, max 1000) |
| POST | `/api/migrations/{id}/cancel` | Cancel |
| GET | `/api/migrations/source-buckets?pool_id=` | Buckets currently served by a pool |
| GET | `/api/migrations/orphaned` | Completed migrations whose source copy still exists |
| GET | `/api/migrations/stale` | Active migrations that cannot proceed (a pool without members, source bucket gone) |

```json
{"bucket": "photos", "src_pool_id": 1, "dst_pool_id": 2}
{"bucket": "photos", "folder": "2025/", "dst_pool_id": 2}
```

| Field | |
|-------|---|
| `bucket` | Required |
| `dst_pool_id` | Required |
| `src_pool_id` | Required for a whole bucket: the pool its route points at. For a folder it is worked out; if sent it must agree. |
| `folder` | Move only this folder of the bucket. A missing trailing slash is added. |
| `vhost_id` | Folder moves only, when the bucket's vhost cannot be worked out |

Both pools must be `nanio` pools with at least one enabled member. A second move of a bucket
that is already being moved (itself or one of its folders) answers 409.

Response fields worth knowing:

| Field | Meaning |
|-------|---------|
| `phase` | `pending`, `copying`, `write_routing`, `verifying`, `switching`, `done`, `error`, `cancelled` |
| `folder` | Empty for a whole bucket, otherwise the folder being moved |
| `split_routing` | `true` while nginx serves the bucket (or folder) through the live-migration split. Stays `true` after `error`/`cancelled` until a migration between the same pools completes. |
| `objects_done` / `objects_total`, `bytes_done` / `bytes_total` | Progress reported by rclone |
| `error_msg` | Why it stopped |
| `orphaned_source_pool_id`, `orphaned_source_prefix`, `orphaned_at` | Where the old copy is, once `done` |
| `cascade_warnings` | Linked `http` vhosts that have no route for this bucket |

`409`: a migration for the bucket is already active, or split routing left by an earlier
migration points at a different pool.

---

## Config

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/config/status` | Every managed file with its hash on disk, the hash the orchestrator wrote, and whether it drifted; time and result of the last reload |
| POST | `/api/config/validate` | `nginx -t` |
| POST | `/api/config/reload` | `nginx -s reload`: applies pending changes |
| GET | `/api/config/preview/pool/{id}` | The upstream block that would be generated |
| GET | `/api/config/preview/vhost/{id}` | The server block that would be generated |
| POST | `/api/config/rebuild` | **Database → files.** Regenerate every file, validate, reload. Previous files are restored if validation fails. |
| POST | `/api/config/sync` | **Files → database, additive.** Imports pools, members, vhosts and routes found on disk. Never deletes. |
| POST | `/api/config/rebuild-from-disk` | **Files → database, from scratch.** `?dry_run=true` previews; `?force=true` replaces existing data (otherwise `409` if the database is not empty). |
| POST | `/api/config/absorb-file` | Body `{"path": "..."}`. Accept a drifted file as it is on disk. Only `.conf` files in the managed `pools/` and `vhosts/` directories. |
| POST | `/api/config/rewrite-file` | Body `{"path": "..."}`. Overwrite a drifted file from the database, validate, reload. |
| POST | `/api/config/backup` | Back up the database now |
| GET | `/api/config/settings` | All settings, secrets masked |
| PUT | `/api/config/settings/{key}` | Body `{"value": "..."}`. Writes the setting to the config file; takes effect after a restart. `rclone_path`, `db_path`, `db_backup_path` and `nginx_config_dir` return `403`: change them on the host with `nanio-orchestrator config set`. |
| POST | `/api/config/settings/restart` | Restart the service. Returns at once; the restart happens 1.5 s later. Not available in dev mode. |

---

## Audit

`GET /api/audit` returns the changes, newest first, each with the action, the entity, before and
after values, and the nginx output. Paging: `?page=` (default 1) and `?per_page=` (default 50,
max 200). Date filter: `?from=` and `?to=`.
