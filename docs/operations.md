# Operations manual

Day-to-day use of nanio-orchestrator: the concepts, and how to do each task from the Web UI or
the API.

All API examples assume:

```bash
export ORCH=http://localhost:8080
export KEY=$(sudo grep API_KEY /etc/nanio-orchestrator/config.env | cut -d= -f2)
```

and send the key in the `X-Orchestrator-Key` header.

---

## The model in one page

nanio-orchestrator is a **control plane**. It writes nginx config files and asks nginx to
validate and reload them. Client traffic goes `client → nginx → nanio`; it never passes through
the orchestrator. If the orchestrator is stopped, nginx keeps serving exactly as configured.

| Concept | nginx equivalent | What it is |
|---------|------------------|------------|
| **Pool** | `upstream {}` | A group of machines that hold the same data |
| **Member** | `server` line in the upstream | One machine (`host:port`) of a pool |
| **Vhost** | `server {}` | A hostname clients connect to |
| **Default pool** | `location /` | Where a vhost sends everything that has no dedicated route |
| **Route** | `location /bucket/` | Sends one bucket to a specific pool — this is how buckets are sharded |

A typical layout: one vhost `s3.example.com`, a default pool holding most buckets, and a few
large buckets routed to pools of their own on other machines.

### Pool types

| Type | Members | Use |
|------|---------|-----|
| `nanio` | every member is `active` | nanio instances that share the same data directory. Has S3 credentials, bucket discovery and migrations. |
| `http` | `primary` + `replica` | Plain nginx instances serving the same files read-only. Replicas are written to the upstream as `backup` servers. No credentials, no bucket discovery, no migrations. |

All routes of a vhost must use pools of the same type as its default pool.

### Replicas

Keeping a replica machine in sync (rsync, lsyncd, a replicated filesystem) is done by the
operating system, outside the orchestrator. The orchestrator only needs to know which machines
are replicas, so that it can write them into the upstream:

- In an `http` pool, add the machine as a member with role `replica`. nginx uses it only when
  every `primary` is down.
- In a `nanio` pool every member is `active`: nanio expects all instances of a pool to see the
  same data directory (shared storage), so any member can serve any request.

### What clients must know

- **One set of credentials.** nanio verifies the request signature itself. Every pool behind a
  vhost must accept the credentials the clients use.
- **`ListBuckets` shows the default pool only.** A bucket routed to another pool is fully usable
  but does not appear in the bucket list unless it also exists on the default pool.
- **Buckets are not created for you.** Adding a route does not create the bucket on the target
  pool. Only a migration creates the destination bucket.

---

## Applying changes

Creating or editing a pool, member, vhost or route does three things:

1. writes the new config file,
2. runs `nginx -t`,
3. restores the previous file if nginx rejects it.

It does **not** reload nginx. You apply the pending changes when you are ready:

- Web UI: **Config** tab → *Reload nginx*
- API: `POST /api/config/reload`
- CLI: `nanio-orchestrator config reload`

This lets you make several changes and apply them together. Two things reload on their own:
the migration engine (it must apply its own routing steps) and the bucket-sync cleanup that
removes a route whose bucket no longer exists on its pool.

---

## Tasks

### Create a pool and add machines

```bash
curl -s -X POST $ORCH/api/pools -H "X-Orchestrator-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"name": "pool-2026", "type": "nanio", "lb_method": "least_conn", "description": "2026 buckets"}'

curl -s -X POST $ORCH/api/pools/1/members -H "X-Orchestrator-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"address": "10.0.1.11:9000"}'
```

Member fields: `address` (required), `role` (`active` for nanio; `primary`/`replica` for http),
`weight` (1), `max_fails` (3), `fail_timeout_s` (30), `enabled` (true).

A pool with no members has no config file, because an empty `upstream` is invalid in nginx.

### Set up a storage node

The orchestrator does not log in to the storage machines, but it generates everything a node
needs so that it matches what the gateway expects. Pools page → member → **Node Setup**, or
`POST /api/pools/{id}/members/{mid}/node-config`.

| Node type | Pool | What is generated |
|-----------|------|-------------------|
| `nanio-only` | `nanio` | nanio options file and systemd unit. nanio listens on the member's port. |
| `nginx-nanio` | `nanio` | The same, plus an nginx server block on the member's port in front of nanio on loopback. |
| `nginx-only` | `http` | An nginx server block serving the data directory read-only. nanio's internal metadata directories are never served. |

Along with the files you get the commands to run on the node, in order (install nanio, create
its user, permissions, enable the service, and how to check it from the gateway), and warnings
when something would not work, such as a port that differs from the member address or missing
credentials.

Give it the same access key and secret key you store for the pool: nanio verifies requests
with the keys in its options file. The generated config is returned once and not kept.

The systemd unit refuses to start nanio while the data directory is not mounted. Without that,
a node whose shared storage failed to mount would answer "bucket not found" for everything.

### Store the pool's S3 credentials

The orchestrator needs credentials to list buckets and to run migrations. They are encrypted
with the Fernet key in `NANIO_ORCHESTRATOR_SECRET` (see [configuration.md](configuration.md)).

```bash
curl -s -X PUT $ORCH/api/pools/1/credentials -H "X-Orchestrator-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"access_key": "AKIA...", "secret_key": "...", "region": "us-east-1"}'
```

The secret key is never returned by the API; the access key is returned masked.

### Create a vhost

```bash
curl -s -X POST $ORCH/api/vhosts -H "X-Orchestrator-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"server_name": "s3.example.com", "listen_port": 443, "ssl": true,
       "ssl_cert_path": "/etc/ssl/s3.example.com/fullchain.pem",
       "ssl_key_path":  "/etc/ssl/s3.example.com/privkey.pem",
       "default_pool_id": 1}'
```

With `ssl: true` both certificate paths are required. Optional fields:

- `extra_blocks`: raw nginx directives injected at `top`, `ssl`, `proxy` or `end` of the server
  block. They must be valid nginx syntax or `nginx -t` rejects the change.
- `ip_rule_mode` (`allow` or `deny`) with `ip_rule_ips`: an allowlist or denylist for the whole
  vhost. IPv4, IPv6 and CIDR are accepted.

### Send a bucket to another pool (sharding)

The Buckets page lists the buckets found on the vhost's default pool. For each one you can:

| Action | What it does |
|--------|--------------|
| **Promote** | Adds `location /bucket/` pointing at the chosen pool. With *migrate* it also moves the objects there. |
| **Ignore** | Hides the bucket from the "unrouted" list. |
| **Remove route** | Sends the bucket back to the default pool. |

```bash
# Route the bucket and move its objects
curl -s -X POST $ORCH/api/vhosts/1/buckets/photos/promote -H "X-Orchestrator-Key: $KEY" \
  -H 'Content-Type: application/json' -d '{"pool_id": 2, "migrate": true}'
```

If the bucket already has objects and you route it to a different pool **without** migrating,
the request is refused: the objects would stay behind on the old pool and become unreachable.
Send `"allow_orphan": true` only if that is what you want.

A new, empty bucket can simply be routed (`"migrate": false`) and then created on the target
pool by its owner.

### Move a bucket that is already routed

Use a migration: see [migrations.md](migrations.md).

### Check and fix drift

Every 60 seconds (`DRIFT_INTERVAL`) the orchestrator compares each managed file with the version
it wrote. A file changed by hand shows up as *drifted* on the dashboard, in `GET /api/health`
(`drift_alerts`) and in `GET /api/config/status`. Nothing is corrected automatically. Choose one:

| You want | Do |
|----------|----|
| Keep the manual edit | Config tab → *Absorb*, or `POST /api/config/absorb-file` with `{"path": "..."}` |
| Discard the manual edit | Config tab → *Rewrite from DB*, or `POST /api/config/rewrite-file` with `{"path": "..."}` |
| Import everything found on disk into the DB | Config tab → *Sync from Disk*, or `POST /api/config/sync` (adds and updates, never deletes). Uses the same importer as `rebuild-db`. |
| Regenerate every file and sidecar from the DB | Config tab → *Rebuild All*, `POST /api/config/rebuild` or `nanio-orchestrator config rebuild` |

*Absorb* only records the file as accepted; it does not parse it into pools and routes. If you
added a server or a location by hand, use *sync*.

### Bucket discovery

Every 5 minutes (`BUCKET_SYNC_INTERVAL`) the orchestrator calls `ListBuckets` on the first
enabled member of each vhost's default pool. Bucket states:

| State | Meaning |
|-------|---------|
| `unrouted` | Served by the default pool |
| `routed` | Has its own route |
| `migrating` | A migration is moving it |
| `ignored` | Hidden by the operator |
| `deleted` | No longer returned by `ListBuckets` |

If a routed bucket no longer exists on its target pool, its route is removed and nginx is
reloaded, so requests fall back to the default pool. This happens only on a definite "not
found" answer; if the pool cannot be reached the route is left alone.

### After moving a bucket: the old copy

Neither promote nor a migration deletes anything. The objects left on the old pool are tracked:

- `GET /api/migrations/orphaned` (or `nanio-orchestrator orphaned list`): source data left by
  completed migrations.
- `GET /api/vhosts/{id}/buckets/orphans`: routed buckets that still have objects on the default
  pool.
- `POST /api/vhosts/{id}/buckets/{bucket}/purge-orphan`: deletes that leftover copy. This is
  irreversible; verify the bucket on its new pool first.

### Serving buckets over plain HTTP (`http` pools)

An `http` pool can be linked to the nanio pool whose files it serves (`source_nanio_pool_id`).
A vhost on that pool can then expose individual buckets with
`POST /api/vhosts/{id}/http-bucket-routes/{bucket}`. When a bucket of the linked nanio pool is
migrated, the http vhost's route follows it automatically.

Requests to an `http` pool do not go through nanio, so they are **not authenticated**. Use the
vhost IP rules if the content is not public.

### Audit log

Every change is recorded with before/after values and the nginx output: Web UI → **Audit**, or
`GET /api/audit`. The audit log lives only in the database; it is not recovered by a rebuild
from disk.

---

## Command-line reference

```
nanio-orchestrator [serve]                  Start the server (default command)
nanio-orchestrator install [--no-start]     Install or upgrade in place (root)
nanio-orchestrator doctor                   Check the installation and print fixes (root)
nanio-orchestrator remove [--purge] [-y]    Remove the service; --purge also deletes config and data
nanio-orchestrator rebuild-db               Rebuild the database from the files on disk
  --dry-run                                   Show what would be imported
  --force                                     Replace existing data

nanio-orchestrator config show              All settings, grouped, secrets masked
nanio-orchestrator config get <key>         One setting
nanio-orchestrator config set <key> <val>   Write a setting to the config file
nanio-orchestrator config generate-secret   Generate a Fernet key (--set writes it)
nanio-orchestrator config edit              Open the config file in $EDITOR
nanio-orchestrator config validate          nginx -t
nanio-orchestrator config reload            nginx -s reload
nanio-orchestrator config rebuild           Regenerate every config file and sidecar from the DB and reload

nanio-orchestrator orphaned list            Source data left behind by completed migrations
```

Two commands have similar names and opposite directions:

| Command | Direction | Use when |
|---------|-----------|----------|
| `config rebuild` | database → files | The files on disk are wrong or missing |
| `rebuild-db` | files → database | The database is lost or corrupt |
