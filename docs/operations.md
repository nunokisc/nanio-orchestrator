# Operations manual

Day-to-day use of nanio-orchestrator: the concepts, and how to do each task from the web panel
or the API.

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
| **Route** | `location /bucket/` | Sends one bucket, or one folder of a bucket, to a specific pool — this is how data is sharded |

A typical layout: one vhost `s3.example.com`, a default pool holding most buckets, and a few
large buckets routed to pools of their own on other machines.

### Pool types

| Type | Members | Use |
|------|---------|-----|
| `nanio` | every member is `active` | nanio instances that share the same data directory. Has S3 credentials, bucket discovery and migrations. |
| `http` | `primary` + `replica` | Plain nginx instances serving the same files read-only. Replicas are written to the upstream as `backup` servers. No credentials, no bucket discovery, no migrations. |

All routes of a vhost must use pools of the same type as its default pool.

### The web panel

Open `http://<orchestrator-host>:8080` and sign in with the API key.

| Screen | What it is for |
|--------|----------------|
| **Overview** | Health at a glance: is the nginx configuration valid, what is waiting for a reload, drifted files, buckets missing on their pool, running migrations, splits left by interrupted ones. Each problem links to where it is fixed. On a new installation it lists the first steps. |
| **Pools** | Pools, their members and credentials; *Node setup* for a storage machine |
| **Vhosts** | Hostnames and their routes, shown as bucket → folders |
| **Buckets** | The buckets discovered on each vhost and where each is served from; leftover copies |
| **Explorer** | Read-only view inside a bucket: which pool holds what |
| **Migrations** | Start, follow and cancel migrations; old copies left by completed ones |
| **Configuration** | The managed nginx files: reload, validate, drift, rebuild in either direction |
| **Audit log** | Every change with its values before and after |
| **Settings** | The settings, with secrets masked |

Three things hold on every screen:

- **Changes not applied.** While a managed file differs from what nginx last loaded, a bar at
  the top says how many changes are waiting and has the *Reload nginx* button.
- **Refusals are shown as the API wrote them.** When a refusal can be overridden
  (`allow_orphan`), the override is a separate step that states what happens to the data and
  has to be ticked by hand.
- **Destructive actions ask first**, and the irreversible ones (deleting a pool or a vhost,
  purging a leftover copy, replacing the database) need the name typed.

### Replicas

Keeping a replica machine in sync (rsync, lsyncd, a replicated filesystem) is done by the
operating system, outside the orchestrator. The orchestrator only needs to know which machines
are replicas, so that it can write them into the upstream:

- In an `http` pool, add the machine as a member with role `replica`. nginx uses it only when
  every `primary` is down.
- In a `nanio` pool every member is `active`: nanio expects all instances of a pool to see the
  same data directory (shared storage), so any member can serve any request.

### What clients must know

The short version; [clients.md](clients.md) is the page to hand to whoever writes the
application.

- **Path-style addressing and one set of credentials.** Requests are routed by their path, and
  nanio verifies the signature itself, so every pool behind a vhost must accept the credentials
  the clients use.
- **A new bucket is created on the default pool.** Adding a bucket route does not create the
  bucket on the target pool; the owner creates it through the gateway once the route is applied.
- **`ListBuckets` is answered by the default pool.** For a bucket that lives on another pool the
  orchestrator keeps an empty bucket of the same name on the default pool, so that it is listed.
  It never receives objects: every request for the bucket follows its route.
- **Some operations cannot span pools**: server-side copy between pools, and a recursive listing
  of a bucket that has a folder on another pool. They are listed, with what to do instead, in
  [clients.md](clients.md#limits-that-cannot-be-removed).

---

## Applying changes

Creating or editing a pool, member, vhost or route does three things:

1. writes the new config file,
2. runs `nginx -t`,
3. if nginx rejects it: restores the previous file, undoes the change in the database and
   answers HTTP 422 with what nginx said. Nothing is saved.

So the database, the files and nginx always describe the same state.

A change that touches several files is still one change: nginx judges them together and either
all of it is kept or none. That covers renaming a pool (its upstream file under the new name,
every vhost that uses it, and the old file removed), renaming a vhost, and a folder route
together with the routes it adds to `http` vhosts.

It does **not** reload nginx. You apply the pending changes when you are ready:

- Web panel: *Reload nginx*, on the bar that appears at the top of every screen while changes
  are waiting, or on the **Configuration** screen
- API: `POST /api/config/reload`
- CLI: `nanio-orchestrator config reload`

This lets you make several changes and apply them together. The only thing that reloads on its
own is the migration engine, which must apply its own routing steps.

**What is waiting.** At every reload done through the orchestrator (panel, API, CLI or a
migration) it records the managed `.conf` files as nginx has just loaded them. A file that is
new, changed or removed since then is listed as not applied: on the panel's bar, on the
Configuration screen and in `GET /api/config/status` (`pending_reload`, `pending_files`). Two
limits: a reload done without the orchestrator (`systemctl reload nginx`) is not seen, so the
files changed before it stay listed until the next reload through the orchestrator; and until
the first reload is recorded the panel says that it cannot tell, rather than guess.

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
needs so that it matches what the gateway expects. **Pools** → member → *Node setup*, or
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

### Take a vhost out of service

Disabling a vhost stops nginx answering for that hostname without deleting anything:

```bash
curl -s -X PUT $ORCH/api/vhosts/1 -H "X-Orchestrator-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"enabled": false}'
```

Web panel: **Vhosts** → *Disable* (the confirmation names the hostname), later *Enable*.

The server block is kept on disk, with the same content, as `vhosts/<hostname>.conf.disabled`.
nginx includes only `*.conf`, so it is no longer loaded; the vhost keeps its routes, its bucket
list and its sidecar, and a database rebuild from disk brings it back as disabled. Like every
change it is validated by `nginx -t` and takes effect at the next reload, so it shows as a change
not applied until then. After the reload clients of that hostname get connection errors (when
nothing else listens on its port) or the answer of whichever server block nginx falls back to.

Disabling is refused (HTTP 409) while a migration is moving one of the vhost's buckets or has
left split routing active on it. Enabling writes the file back as `<hostname>.conf`; if nginx
rejects the configuration, nothing changes.

### Send a bucket to another pool (sharding)

The **Buckets** screen lists the buckets found on the vhost's default pool. For each one you can:

| Action | What it does |
|--------|--------------|
| **Route to a pool** | Adds `location /bucket/` pointing at the chosen pool (the API calls this *promote*). *Move them (live migration)* also moves the objects there; *Route only* is for a new, empty bucket. |
| **Ignore** | Hides the bucket from the "unrouted" list. |
| **Remove route** | Sends the bucket back to the default pool. |
| **Move** | For a bucket that already has a route: opens the migration form. |
| **Sync now** | Asks the default pool for its buckets now instead of at the next discovery. |

**Vhosts** → *Add route* with a path `/bucket/` leads to the same choice.

The rule behind all of them: **a change that takes a bucket away from the pool that holds its
objects is refused.** That covers adding a bucket route (the bucket leaves the default pool),
removing or disabling one (it leaves the route's pool) and enabling one again (it leaves the
default pool). If the bucket has objects on the pool it would leave, the answer is 409; if that
pool cannot be asked, 502. A bucket that does not exist on the pool has no objects, so cleaning
up the route of a deleted bucket is not refused. `allow_orphan` (`"allow_orphan": true` in the
body, `?allow_orphan=true` on a `DELETE`) is the explicit way through; in the panel it is a
separate, hand-ticked step under the refusal.

```bash
# Route the bucket and move its objects
curl -s -X POST $ORCH/api/vhosts/1/buckets/photos/promote -H "X-Orchestrator-Key: $KEY" \
  -H 'Content-Type: application/json' -d '{"pool_id": 2, "migrate": true}'
```

If the bucket already has objects and you route it to a different pool **without** migrating,
the request is refused (409): the objects would stay behind on the old pool and become
unreachable. Send `"allow_orphan": true` only if that is what you want.

The same goes when the orchestrator **cannot tell**: if the pool that serves the bucket now is
down or has no enabled member, routing the bucket elsewhere is refused (502). "Cannot ask" is not
"empty". To take a route off a dead pool on purpose, send `"allow_orphan": true`; this also
applies to changing the pool of an existing route.

A new, empty bucket can simply be routed (`"migrate": false`) and then created on the target
pool by its owner.

### Send one folder of a bucket to another pool

When a single bucket outgrows its machines, a folder of it can live on another pool. Add a
route whose path is the bucket followed by the folder:

```bash
curl -s -X POST $ORCH/api/vhosts/1/routes -H "X-Orchestrator-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"path_prefix": "/photos/2025/", "pool_id": 2}'
```

Web panel: **Vhosts** → *Add route* → path `/photos/2025/`. The form explains `/bucket/` versus
`/bucket/folder/`, and the vhost then shows the folder under its bucket.

The rule is the same as for buckets: **the most specific route wins.**

| Request | Answered by |
|---------|-------------|
| Any object under `photos/2025/` (read, write, delete, multipart) | The folder's pool |
| Listing inside the folder (`prefix=2025/…`) | The folder's pool |
| Everything else in `photos` | The bucket's own pool (its `/photos/` route, or the default pool) |
| Multi-object delete | Every pool that holds part of the bucket; each deletes the keys it has |

What the orchestrator does for you when the route is created:

1. Checks that the folder is **empty** on the bucket's own pool. Objects already there would be
   hidden by the new route, so the request is refused (HTTP 409).
2. Creates the bucket on the folder's pool. Clients cannot do that through the gateway.
3. Writes a small marker object `2025/.nanio-shard` on the bucket's own pool, so that the folder
   appears when the bucket is browsed. nanio does not list `.nanio*` objects, so clients never
   see it. Bucket sync rewrites it on every cycle.
4. Adds the matching route to every `http` vhost that serves this bucket, pointing at the
   `http` pool linked to the folder's pool. If there is none you get a warning.

Removing the route is refused (HTTP 409) while the folder still has objects on its pool: without
the route they would be unreachable. Changing the pool of a folder route by hand is refused for
the same reason. To move a folder that holds data, to another pool or back into its bucket, use
a migration: [migrations.md](migrations.md#moving-one-folder-of-a-bucket).

Limits:

| Situation | Effect |
|-----------|--------|
| Listing the **whole** bucket without a delimiter (recursive listing from the root) | Does not include the folder's objects. Browsing folder by folder works, and so does a recursive listing that starts inside the folder. |
| Server-side copy (`CopyObject`) between the folder and the rest of the bucket | Fails with 404: the two sides are on different machines. The client must download and upload. |
| A folder that already holds data | Adding a route is refused. Move it with a migration instead: see [migrations.md](migrations.md#moving-one-folder-of-a-bucket). |
| Credentials | Both pools must accept the credentials the clients use. |

The first two cannot be removed without putting a component in the request path. What a client
can do instead is in [clients.md](clients.md#limits-that-cannot-be-removed).

### Move a bucket that is already routed

Use a migration: see [migrations.md](migrations.md).

### Check and fix drift

Every 60 seconds (`DRIFT_INTERVAL`) the orchestrator compares each managed file with the version
it wrote. A file changed by hand shows up as *drifted* on the Overview and Configuration
screens, in `GET /api/health` (`drift_alerts`) and in `GET /api/config/status`. Nothing is
corrected automatically. On the **Configuration** screen, *Compare* shows the lines that were
removed and added by hand (`GET /api/config/file`). Then choose one:

| You want | Do |
|----------|----|
| Keep the manual edit | Configuration → *Absorb*, or `POST /api/config/absorb-file` with `{"path": "..."}` |
| Discard the manual edit | Configuration → *Rewrite from the database*, or `POST /api/config/rewrite-file` with `{"path": "..."}` |
| Import everything found on disk into the DB | Configuration → *Sync from disk*, or `POST /api/config/sync` (adds and updates, never deletes). Uses the same importer as `rebuild-db`. |
| Regenerate every file and sidecar from the DB | Configuration → *Rebuild all from the database*, `POST /api/config/rebuild` or `nanio-orchestrator config rebuild` |

*Absorb* only records the file as accepted; it does not parse it into pools and routes. If you
added a member to an upstream by hand, use *sync*.

A `location` is imported as a route only when it carries the `# route_id:` comment that the
orchestrator writes. A location you add yourself (in a vhost's extra blocks, for instance) stays
what it is: your own nginx config. Create routes through the panel or the API.

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

A bucket that lives on another pool is not returned by the default pool, so it would be
missing from `ListBuckets` and from this page. When a bucket route points at another pool and the
bucket exists there, bucket sync creates an **empty bucket of the same name on the default
pool**. It only makes the name appear; all requests for the bucket follow the route. If the
owner later deletes the bucket, that empty bucket stays until you remove the route and delete
it.

If a routed bucket's target pool answers that the bucket does not exist, the bucket is marked
**missing on pool** on the Buckets and Overview screens, in `GET /api/vhosts/{id}/buckets` (`missing_since`) and
in `GET /api/health` (`missing_buckets`). The route is **not** removed and nginx is not
reloaded. Decide which case it is:

| Cause | What to do |
|-------|------------|
| The bucket was deleted by its owner | Buckets → *Remove route* (or `DELETE /api/vhosts/{id}/buckets/{bucket}/route`), then reload nginx |
| The pool's storage is not mounted, or the node points at an empty data directory | Fix the node. The mark clears by itself at the next sync. |

The route is kept on purpose: a node with unmounted storage reports every bucket as missing, and
removing the routes would send clients to the default pool, where an older copy of the bucket
may still exist.

### After moving a bucket: the old copy

Neither promote nor a migration deletes anything. The objects left on the old pool are tracked:

- `GET /api/migrations/orphaned` (or `nanio-orchestrator orphaned list`): source data left by
  completed migrations.
- `GET /api/vhosts/{id}/buckets/orphans`: routed buckets that still have objects on the default
  pool.
- `POST /api/vhosts/{id}/buckets/{bucket}/purge-orphan`: deletes that leftover copy. This is
  irreversible; verify the bucket on its new pool first.

In the panel: **Buckets** → *Leftover copies on the default pool* → *Check now* lists them, with
*Browse* (the Explorer, on that pool) and *Purge* (the bucket's name has to be typed).
**Migrations** lists the old copies left by completed migrations, each with *Browse the old
copy*; those are deleted by you, directly on the pool.

### Look inside a bucket: the Explorer

**Explorer** shows what a pool holds, one level at a time: pick a pool, a bucket, then folders
and objects with their size and modification time. It answers the question no S3 client can
answer through the gateway: *which pool* holds this.

- **Seen through a vhost.** *Browse* on a bucket or a route (Vhosts, Buckets) opens the Explorer
  on the pool that serves it according to the vhost's routes, and says which pool that is and
  through which route. A folder routed to another pool is marked *served by pool X*; opening it
  switches to that pool. Choosing another pool by hand shows what *that* pool holds under the
  same path, with a warning that clients are not served from it: this is how you look at an old
  copy.
- **During a migration.** While split routing is active on a bucket or folder, the Explorer says
  that it is being moved from pool X to pool Y: clients read Y first and fall back to X for
  objects not copied yet, and write to Y only. It opens on Y, the destination; an object can be
  on either pool until the migration is done.
- **Object details**: size, modification time, ETag and content type.
- **Download** of a single object up to 64 MiB. Larger objects are refused: fetch them with an
  S3 client through the gateway. Each download is recorded in the audit log; browsing is not.
- **Read-only.** There is no upload, delete, rename or bucket creation.

These requests are made by the orchestrator, with the pool's stored credentials, to one member
of the pool, because an operator asked: management traffic, like bucket discovery or purging a
leftover copy. Client traffic still never passes through the orchestrator.

A pool that cannot be asked (no enabled member, no stored credentials, unreachable, refusing
the credentials) is reported as an error with the reason, never as an empty listing.

### Serving buckets over plain HTTP (`http` pools)

An `http` pool can be linked to the nanio pool whose files it serves (`source_nanio_pool_id`).
A vhost on that pool can then expose individual buckets with
`POST /api/vhosts/{id}/http-bucket-routes/{bucket}`. When a bucket of the linked nanio pool is
migrated, the http vhost's route follows it automatically.

Requests to an `http` pool do not go through nanio, so they are **not authenticated**. Use the
vhost IP rules if the content is not public.

### Audit log

Every change is recorded with before/after values and the nginx output: web panel → **Audit log**
(filter by entity and date; open an entry to see the values side by side), or `GET /api/audit`. The audit log lives only in the database; it is not recovered by a rebuild
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
