# API reference

Base URL: `http://<orchestrator-host>:8080`. Request and response bodies are JSON.

An interactive version generated from the code is served by the orchestrator itself at
`/api/docs` (Swagger UI) and `/api/redoc`.

## Authentication

| Client | How |
|--------|-----|
| API | `X-Orchestrator-Key: <api key>` header on every `/api/*` request |
| Web panel | `POST /api/auth/login` sets the `nanio_session` cookie (`HttpOnly`, `SameSite=Lax`), valid for `SESSION_TTL` seconds. The cookie is accepted on `/api/*` like the header. |

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/auth/login` | Body `{"key": "<api key>"}`. Sets the session cookie and answers the session. `401` with `{"detail": "Invalid API key"}` on a wrong key. |
| POST | `/api/auth/logout` | Expires the session cookie |
| GET | `/api/auth/session` | `{"authenticated": bool, "dev_mode": bool, "version": str}`. Never `401`: it is how the panel finds out whether to show the sign-in screen. |

No authentication: `GET /api/health`, `/api/docs`, `/api/redoc`, `/api/auth/*`, and everything
outside `/api/`, which is the web panel's shell and assets (they contain no data). Every other
`/api/*` path, the OpenAPI schema at `/api/openapi.json` included, answers `401` with a JSON
`detail` when the key or the session is missing or wrong.

```bash
curl -H "X-Orchestrator-Key: $KEY" http://localhost:8080/api/pools
```

Changes to pools, members, vhosts and routes are written and validated with `nginx -t` but
**not applied** until `POST /api/config/reload`. A change that `nginx -t` rejects answers **422**
and is not saved anywhere: the previous file is restored, the database change is undone, and
the response carries the nginx output.

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

`GET /api/overview` (authenticated) is the summary the panel polls:

```json
{"pools": [{"id": 1, "name": "pool-a", "type": "nanio", "members": 2, "enabled_members": 2,
            "has_credentials": true}],
 "vhosts": 1, "routes": 3, "unrouted_buckets": 12,
 "missing_buckets": [{"vhost_id": 1, "server_name": "s3.example.com", "bucket": "photos",
                      "pool_name": "pool-b", "missing_since": "2026-01-02T03:04:05Z"}]}
```

`routes` counts bucket and folder routes (not the default `/`); `unrouted_buckets` the
discovered buckets still served by a default pool.

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
| DELETE | `/api/vhosts/{id}` | Delete a vhost together with its routes. 409 while a migration is moving one of its buckets. |
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
| `default_pool_id` | Pool for `location /`. Its type fixes the type every route of the vhost must use. On update, `null` removes the default pool and its `/` route. |
| `extra_blocks` | Raw nginx directives. `zone`: `top` (after `server_name` and IP rules), `ssl` (after the certificate lines), `proxy` (after the proxy buffering lines), `end` (before the closing brace) |
| `ip_rule_mode` | `allow`: only the listed addresses, then `deny all`. `deny`: the listed addresses are blocked. `null`: no restriction. |
| `ip_rule_ips` | IPv4, IPv6, or CIDR; validated on write |
| `enabled` | Default `true`. `false`: the server block is written as `vhosts/<server_name>.conf.disabled`, which nginx does not load, so the hostname is not served after the next reload. Switching it is one validated change (one file written, the other removed). Disabling answers `409` while a migration is moving one of the vhost's buckets or has left split routing active on it. |

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

- A path is one or more segments of letters, digits and `. _ @ -`; empty segments (`//`) are
  refused.
- A bucket route does not create the bucket on the target pool and does not move any object.
- A change that takes a whole bucket away from the pool that holds it is refused: `409` when
  the bucket has objects on the pool it would leave, `502` when that pool cannot be asked
  (unreachable, no enabled member, credentials refused). `"allow_orphan": true` (`?allow_orphan=true`
  on `DELETE`) overrides both. It applies to:
  - `POST` of a bucket route on a `nanio` vhost that points away from the default pool (the
    bucket leaves the default pool);
  - `PUT` with `"enabled": false` on a bucket route (it leaves the route's pool) and with
    `"enabled": true` (it leaves the default pool);
  - `DELETE` of an enabled bucket route (it leaves the route's pool).

  A bucket that does not exist on that pool has no objects. A route that changes nothing (same
  pool as the default pool, or created disabled) is not checked, and `http` pools are never
  asked.
- A folder route is refused with 409 if the folder already has objects on the bucket's own pool
  (`"allow_orphan": true` overrides). It creates the bucket on the folder's pool. The response
  carries `warnings` (a list) for anything that needs attention.
- `PUT` cannot change a route's path, nor the pool of a folder route, nor point a route at a
  pool of another type. If the route's current pool cannot be asked whether the bucket has
  objects, changing its pool answers 502 unless `"allow_orphan": true` is sent. Changing the pool of a
  bucket route whose bucket has objects starts a migration; when no migration slot is free it
  answers 429 and changes nothing.
- `DELETE` of a folder route is refused with 409 while the folder has objects on its pool, and
  with 400 when its pool has no enabled member to ask (`?allow_orphan=true` overrides both).

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
| DELETE | `/api/vhosts/{id}/buckets/{bucket}/route` | Remove the bucket's route (back to the default pool). `409` while the bucket has objects on the route's pool, `502` when that pool cannot be asked; `?allow_orphan=true` overrides both. A bucket that no longer exists on its pool is not refused. |
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
| GET | `/api/config/status` | Every managed file with its hash on disk, the hash the orchestrator wrote, and whether it drifted; what is waiting for a reload (below) |
| GET | `/api/config/file?path=` | One managed file: `content` (on disk, `null` if missing), `recorded` (what the orchestrator wrote, `null` if it never wrote it) and `drifted`. Only `.conf` files in the managed `pools/` and `vhosts/` directories, and the `.conf.disabled` file of a disabled vhost. |
| POST | `/api/config/validate` | `nginx -t` |
| POST | `/api/config/reload` | `nginx -s reload`: applies pending changes |
| GET | `/api/config/preview/pool/{id}` | The upstream block that would be generated |
| GET | `/api/config/preview/vhost/{id}` | The server block that would be generated |
| POST | `/api/config/rebuild` | **Database → files.** Regenerate every file, validate, reload. Previous files are restored if validation fails. |
| POST | `/api/config/sync` | **Files → database, additive.** Imports pools, members, vhosts and routes found on disk. Never deletes. |
| POST | `/api/config/rebuild-from-disk` | **Files → database, from scratch.** `?dry_run=true` previews; `?force=true` replaces existing data (otherwise `409` if the database is not empty). |
| POST | `/api/config/absorb-file` | Body `{"path": "..."}`. Accept a drifted file as it is on disk. Only `.conf` files in the managed `pools/` and `vhosts/` directories, and the `.conf.disabled` file of a disabled vhost. |
| POST | `/api/config/rewrite-file` | Body `{"path": "..."}`. Overwrite a drifted file from the database, validate, reload. For a vhost, the file it should have (enabled or disabled) is written and the other one removed. |
| POST | `/api/config/backup` | Back up the database now |
| GET | `/api/config/settings` | All settings, secrets masked |
| PUT | `/api/config/settings/{key}` | Body `{"value": "..."}`. Writes the setting to the config file; takes effect after a restart. `rclone_path`, `db_path`, `db_backup_path`, `nginx_config_dir` and `log_file` return `403`: change them on the host with `nanio-orchestrator config set`. |
| POST | `/api/config/settings/restart` | Restart the service. Returns at once; the restart happens 1.5 s later. Not available in dev mode. |

Fields of `GET /api/config/status` about applying changes:

| Field | Meaning |
|-------|---------|
| `pending_reload` | `true`: managed files differ from what nginx loaded at the last reload done through the orchestrator. `false`: they do not. `null`: files exist but no reload has been recorded yet, so it cannot be told. |
| `pending_files` | `[{"path": "...", "change": "added" \| "modified" \| "removed"}]` |
| `last_applied_at` | When that last reload happened (`null` if none was recorded) |
| `last_reload_ok`, `last_reload_at` | Result and time of the last audited change that ran nginx. This includes the `nginx -t` of ordinary changes, so it is not the time of the last reload: use `last_applied_at` for that. |

A reload done without the orchestrator (`systemctl reload nginx`) is not seen: see
[operations.md](operations.md#applying-changes).

---

## Explorer

Read-only browsing of what a `nanio` pool holds. The orchestrator asks the first enabled member
of the pool with the pool's stored credentials. Nothing here writes to a pool. See
[operations.md](operations.md#look-inside-a-bucket-the-explorer).

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/explorer/pools/{id}/buckets` | The buckets on the pool: `{"pool": {"id", "name"}, "member": "host:port", "buckets": ["..."]}` |
| GET | `/api/explorer/pools/{id}/buckets/{bucket}/objects` | One page of one level of the bucket (delimiter `/`) |
| GET | `/api/explorer/pools/{id}/buckets/{bucket}/object?key=` | Metadata of one object (HEAD): `size`, `last_modified`, `etag`, `content_type` |
| GET | `/api/explorer/pools/{id}/buckets/{bucket}/download?key=` | The bytes of one object, as an attachment (`application/octet-stream`). `413` above 64 MiB. Recorded in the audit log (`download_object`). |
| GET | `/api/explorer/vhosts/{id}/locate?bucket=&prefix=` | Which pool serves that bucket, or that prefix inside it, according to the vhost's routes: the most specific enabled route wins, the default pool otherwise. `{"vhost", "bucket", "prefix", "route", "pool": {"id", "name", "type"}, "split"}`. While a migration has split routing active on that route, `pool` is the destination (clients read it first and fall back to the source) and `split` is `{"migration_id", "source": {"id", "name"}, "destination": {"id", "name"}}`; otherwise `split` is `null`. |

Query parameters of `objects`:

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `prefix` | empty | The folder to list (`2025/`), optionally followed by the start of a name to filter on (`2025/rep`) |
| `token` | empty | `next_token` of the previous page |
| `limit` | `200` | Keys per page, 1 to 1000 |
| `vhost_id` | none | Mark the folders that a route of this vhost sends to another pool |

```json
{"pool": {"id": 1, "name": "pool-a"}, "bucket": "photos", "prefix": "",
 "folders": [{"prefix": "2024/", "routed_pool": null},
             {"prefix": "2025/", "routed_pool": {"id": 2, "name": "pool-b"}}],
 "objects": [{"key": "readme.md", "size": 360, "last_modified": "2026-01-02T03:04:05.000Z",
              "etag": "4056fcee807fa6e2ba6f6fd222f7ed8e"}],
 "next_token": null}
```

`routed_pool` is set when `vhost_id` is given and an enabled folder route of that vhost sends
that folder to a pool other than the one being listed: what clients see under it is on that
pool. While the folder is being moved (split routing active) that pool is the destination of
the migration. Such a folder is listed even when this pool holds nothing under it. A server may repeat a
folder on the page after the one that first listed it; merge folders by `prefix`.

Errors:

| Status | When |
|--------|------|
| `400` | The pool is an `http` pool (no S3 API), has no enabled member, or has no stored credentials; for `locate`, the vhost has no route that serves the bucket |
| `404` | The pool or vhost is unknown, or the bucket or object really does not exist on that pool |
| `413` | Download of an object larger than 64 MiB: fetch it with an S3 client through the gateway |
| `502` | The pool could not be asked (unreachable, refused the credentials, answered an error). Never an empty listing. |

---

## Audit

`GET /api/audit` returns the changes, newest first, each with the action, the entity, before and
after values, and the nginx output. Paging: `?page=` (default 1) and `?per_page=` (default 50,
max 200). Filters: `?entity_type=` (`pool`, `pool_member`, `vhost`, `route`, `bucket`,
`migration`, `config`, `object`), `?entity_id=`, and dates with `?from=` and `?to=`.
