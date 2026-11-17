# Troubleshooting

First check, whatever the symptom:

```bash
sudo nanio-orchestrator doctor
```

It verifies the settings, permissions, the nginx include, the sudoers rule and the service, and
prints the fix for each problem. Then, if needed:

```bash
sudo systemctl status nanio-orchestrator
sudo journalctl -u nanio-orchestrator -n 200 --no-pager
curl -s http://localhost:8080/api/health            # no authentication needed
sudo nginx -t
```

`/api/health` returns `status`, `db_ok`, `drift_alerts`, `missing_buckets` and the version. Remember that client
traffic does not depend on the orchestrator: if clients are failing, look at nginx and the
storage nodes first.

---

## The service

### It does not start

| Log message / symptom | Cause | Fix |
|-----------------------|-------|-----|
| `unable to open database file`, permission denied | The data directory or the DB is not owned by the service user (typically after running a CLI command as root) | `sudo chown -R nanio-orchestrator: /opt/nanio-orchestrator/data` |
| `address already in use` | Port 8080 is taken | `nanio-orchestrator config set port 8081`, restart |
| Starts, but every nginx action fails with a sudo error | The sudoers drop-in is missing or names a different nginx path | Re-run `sudo nanio-orchestrator install` (it does not overwrite your config) |
| `status=203/EXEC` or `Permission denied` on the program | The package was installed per-user (pipx, uv tool) where the service user cannot reach it | Install with `install.sh` ([installation.md](installation.md)) |
| Runs in dev mode on a production machine (API key `dev`, files under `./dev-data`) | `/etc/nanio-orchestrator/config.env` does not exist, or `DEV=true` is set | Restore the config file ([disaster-recovery.md](disaster-recovery.md#4-configenv-is-lost)) |

### API returns 401

Send the key in the `X-Orchestrator-Key` header:

```bash
curl -H "X-Orchestrator-Key: $(sudo grep API_KEY /etc/nanio-orchestrator/config.env | cut -d= -f2)" \
     http://localhost:8080/api/pools
```

### The panel keeps returning to the sign-in screen

- The session expired (default 8 hours, `SESSION_TTL`).
- The API key changed: every existing session is invalidated.
- The browser blocks cookies for the host.

### The panel shows "The web panel is not built"

The package was installed from a source checkout whose panel had not been built. Run `make ui`
in the checkout (needs Node.js) and install again, or install a released wheel, which contains
it. The API is not affected.

### The panel says changes are not applied, but nginx was reloaded

It only knows about reloads done through the orchestrator (panel, API, CLI, migrations). After a
`systemctl reload nginx` the files changed before it stay listed; *Reload nginx* in the panel
clears the list. See [operations.md](operations.md#applying-changes).

### A setting I changed has no effect

Settings are read at startup. Restart the service (`systemctl restart nanio-orchestrator`, or
*Restart the service* on the Settings screen, offered after a setting is saved).

---

## nginx and config changes

### A change is refused with an `nginx -t` error

The orchestrator wrote the new file, nginx rejected the configuration, and the previous file was
put back. Nothing changed. The nginx message is in the API response and in the audit log.

| nginx says | Cause |
|-----------|-------|
| `host not found in upstream "pool-x"` | The two `include` lines are missing from `nginx.conf`, or the pool has no members (an empty pool has no upstream file) |
| `cannot load certificate` | `ssl_cert_path` / `ssl_key_path` do not exist or are not readable by nginx |
| `duplicate upstream` / `conflicting server name` | Something outside `/etc/nginx/nanio` already defines that name |
| Errors in a file you never touched | The existing configuration is already invalid. `nginx -t` validates everything, so no change can be applied until it is fixed. |
| Syntax error inside a vhost | An `extra_blocks` entry is not valid nginx syntax |

### I changed something and nothing happened for clients

Changes are written and validated, not applied. The panel shows a bar at the top of every screen
while something is waiting. Reload nginx: *Reload nginx* on that bar or on the Configuration
screen, `POST /api/config/reload` or `nanio-orchestrator config reload`.

### The panel reports drift

A managed file differs from what the orchestrator wrote. See
[operations.md](operations.md#check-and-fix-drift).

### After an upgrade the generated config looks unchanged

New versions change the templates, but existing files are only rewritten when their pool or
vhost is saved. Regenerate everything once after upgrading: Configuration → *Rebuild all from
the database*.

---

## Clients

### `403 SignatureDoesNotMatch` through the gateway, but direct access to nanio works

| Cause | Check |
|-------|-------|
| The pool behind this bucket does not accept the client's credentials | Every pool behind a vhost must accept the same client credentials |
| Clock skew | nanio accepts a 15-minute window; run NTP on clients and storage nodes |
| The vhost listens on a non-standard port and its config was generated by an older version | Older templates forwarded `Host` without the port. Regenerate the configs (*Rebuild all from the database*) |
| A proxy in front of nginx rewrites the `Host` header | The signature covers `Host`; it must reach nanio unchanged |

### `301 Moved Permanently` when listing a routed bucket

Fixed in the template: each bucket route now also matches `/bucket` without the trailing slash.
Regenerate the configs (*Rebuild all from the database*) and reload.

### A bucket is missing from the client's bucket list

`ListBuckets` is answered by the vhost's default pool. A bucket routed to another pool is
usable but is not listed unless it also exists on the default pool.

### `404 NoSuchBucket` right after routing a bucket to a new pool

Routing does not create the bucket. Create it on the target pool, or use a migration (which
does).

### Objects seem to have disappeared after routing a bucket

The bucket was routed to another pool without migrating, so the objects are still on the old
pool. Remove the route to get them back immediately, then route the bucket again with *Move
them (live migration)*. The Explorer shows which pool holds the objects.
`GET /api/vhosts/{id}/buckets/orphans` lists such leftovers.

### Uploads fail with `413` or large uploads are slow

The generated vhost sets `client_max_body_size 0` and disables request buffering. If you see
413, the request is being handled by a different `server` block: check `server_name` and the
listen port.

---

## Buckets and credentials

### The Buckets screen is empty or bucket sync reports an error

- The vhost has no default pool, or the default pool is of type `http`.
- The default pool has no enabled member.
- No credentials are stored for the pool, or they are wrong (`GET /api/pools/{id}/credentials`).
- The orchestrator machine cannot reach the member's `host:port`.

Trigger a sync by hand to see the error: `POST /api/vhosts/{id}/buckets/sync`.

### A bucket is marked "missing on pool"

The pool the bucket is routed to answers that the bucket does not exist. Nothing was changed:
the route is still in place. Check the node first (is its data directory mounted? `ls` it on
the node); if several buckets of the same pool are marked at once, it is almost certainly the
node and not the buckets. See [operations.md](operations.md#bucket-discovery).

### Credentials API returns an error about the secret

`NANIO_ORCHESTRATOR_SECRET` is not set or is not a valid Fernet key:

```bash
sudo nanio-orchestrator config generate-secret --set
sudo systemctl restart nanio-orchestrator
```

### `Failed to decrypt credential — secret key may have changed`

The credentials were encrypted with a different `SECRET`. Put the original key back, or store
the credentials again.

### A route disappeared on its own

Bucket sync removes a route when its bucket no longer exists on the target pool (a definite
"not found" from the pool, never on a connection error). The removal is in the service log
(`removing route and reverting to unrouted`). Recreate the bucket on that pool and promote it
again.

---

## Migrations

See [migrations.md](migrations.md) for how a migration works and how to finish an interrupted
one. Quick reference:

| Symptom | Cause | Fix |
|---------|-------|-----|
| `rclone binary not found` | rclone is not installed or not on the service's `PATH` | Install it, or set `RCLONE_PATH` to the full path and restart |
| `Max parallel migrations reached` | `MIGRATION_MAX_PARALLEL` migrations are running | Wait, cancel one, or raise the limit |
| `An active migration already exists for bucket` | One migration per bucket | Wait for it or cancel it |
| `Split routing from migration N ... is still active` (409) | An earlier migration of this bucket stopped after the split began | Migrate between the same two pools as migration N to finish it |
| `Refusing to migrate: source bucket is empty` | Wrong source pool, or the pool's credentials see nothing | Check the route and the credentials |
| `No nginx route found to update` | The bucket has no route on the vhost | Route the bucket first (Buckets → *Route to a pool*) |
| `diff count did not decrease` | Objects could not be copied or keep failing verification | Read the rclone lines in the migration log; check disk space and reachability of the destination; then start the migration again |
| Ended in `error` after a service restart | The restart happened during a routing change | Check `split_routing` on the migration and start it again |
| Listed under `GET /api/migrations/stale` | A pool lost all its members, or the source bucket is gone | Fix the pool or cancel the migration |

---

## Recovering from mistakes

| Mistake | Recovery |
|---------|----------|
| Deleted a route by accident | Requests fall back to the default pool. Route the bucket again (*Route only*) to the pool where its data is; nothing was deleted. The Explorer shows which pool that is. |
| Edited a managed file by hand and broke nginx | `POST /api/config/rewrite-file` with the path restores it from the database |
| Deleted the database | [disaster-recovery.md](disaster-recovery.md#2-the-database-is-corrupt-or-deleted) |
| Purged the wrong copy of a bucket | Not recoverable from the orchestrator. Restore from the replica or backup of that storage node. |

---

## Getting more detail

```bash
sudo nanio-orchestrator config set log_level debug && sudo systemctl restart nanio-orchestrator
sudo journalctl -u nanio-orchestrator -f
```

`LOG_FILE` writes the same output to a rotating file. The audit log (`GET /api/audit`) holds the
before/after of every change together with the nginx output.
