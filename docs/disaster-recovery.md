# Disaster recovery

What to back up, and what to do when something is lost.

The starting point is always the same: **nginx does not depend on the orchestrator**. Whatever
happens to the orchestrator or its database, client traffic keeps flowing with the last config
nginx loaded. Recovery is about getting the management plane back, not about restoring service.

---

## Where the state lives

| Path | Contents | If lost |
|------|----------|---------|
| `/etc/nanio-orchestrator/config.env` | Settings, the API key, the Fernet `SECRET` | API key: generate a new one. `SECRET`: stored pool credentials can no longer be decrypted and must be entered again. |
| `/etc/nginx/nanio/pools/*.conf`, `vhosts/*.conf` | What nginx actually serves | Regenerate from the database (`config rebuild`). |
| `/etc/nginx/nanio/**/*.meta.json` | Sidecars: pool type, description, encrypted credentials, linked pool; vhost default pool, extra blocks, IP rules | Needed to rebuild the database. Rewritten whenever the pool or vhost is saved. |
| `/opt/nanio-orchestrator/data/orchestrator.db` | The database | Restore a backup, or rebuild from the files above. |
| `/opt/nanio-orchestrator/data/orchestrator.db.bak*` | Rotating database backups | — |
| `/opt/nanio-orchestrator/data/migrations/*.state.json` | Migrations in progress, including whether split routing is active | An unfinished migration is not restored by a rebuild. |
| `/opt/nanio-orchestrator/data/migrations/*.done.json` | Completed migrations and where the old copy is | The list of orphaned source data is lost. |

The database and the files describe the same thing twice. Either one can be rebuilt from the
other, which is why losing only one of them is never fatal. The audit log is the exception: it
exists only in the database.

## What to back up

Copy these three locations off the machine:

```
/etc/nanio-orchestrator/config.env
/etc/nginx/nanio/
/opt/nanio-orchestrator/data/
```

Notes:

- The automatic database backups are written next to the database by default, on the same disk.
  Point `NANIO_ORCHESTRATOR_DB_BACKUP_PATH` at another disk, or include the data directory in
  your normal backup job.
- Keep a copy of `SECRET` somewhere safe. Without it the encrypted credentials in the sidecars
  are useless.
- The objects themselves are not the orchestrator's concern. Replication and backup of the
  storage nodes (rsync, lsyncd, filesystem snapshots) are handled at operating-system level.

### Automatic database backups

- every `DB_BACKUP_INTERVAL` seconds (default 300),
- on demand: `POST /api/config/backup`.

They are made with SQLite's online backup API, so they are consistent even while the service is
running. `DB_BACKUP_ROTATE` copies are kept (default 3): `.bak` is the newest, then `.bak.2`,
`.bak.3`.

---

## Scenarios

### 1. The orchestrator is down

Nothing to recover. Traffic is unaffected.

```bash
sudo systemctl status nanio-orchestrator
sudo journalctl -u nanio-orchestrator -n 100
sudo systemctl restart nanio-orchestrator
```

Migrations that were copying or verifying resume on startup. A migration interrupted while it
was changing the routing is marked `error`; see [migrations.md](migrations.md#when-it-does-not-finish).

### 2. The database is corrupt or deleted

**Option A — restore the latest backup** (keeps the audit log):

```bash
sudo systemctl stop nanio-orchestrator
cd /opt/nanio-orchestrator/data
sudo cp orchestrator.db.bak orchestrator.db        # or .bak.2 / .bak.3
sudo rm -f orchestrator.db-wal orchestrator.db-shm
sudo chown nanio-orchestrator: orchestrator.db && sudo chmod 600 orchestrator.db
sudo systemctl start nanio-orchestrator
```

The backup can be a few minutes older than the files on disk. Open the Configuration screen:
anything changed since the backup shows as drift. Use *Sync from disk* (`POST /api/config/sync`) to bring
the database up to date with the files.

**Option B — rebuild from the files** (no backup needed):

```bash
sudo systemctl stop nanio-orchestrator
sudo rm -f /opt/nanio-orchestrator/data/orchestrator.db*      # only if it is corrupt
sudo nanio-orchestrator rebuild-db --dry-run                   # preview
sudo nanio-orchestrator rebuild-db
sudo chown -R nanio-orchestrator: /opt/nanio-orchestrator/data  # the command ran as root
sudo chmod 600 /opt/nanio-orchestrator/data/orchestrator.db
sudo systemctl start nanio-orchestrator
```

If the service is still running with a damaged or outdated database, the same thing is available
without stopping it: `POST /api/config/rebuild-from-disk?dry_run=true`, then `?force=true`.

What a rebuild recovers:

| Data | Source | Recovered |
|------|--------|-----------|
| Pools, members, load-balancing method | `pools/*.conf` | Yes |
| Pool type, description, linked nanio pool | `pools/*.meta.json` | Yes |
| Pool credentials | `pools/*.meta.json` | Yes, if `SECRET` is unchanged |
| Vhosts, TLS settings, routes | `vhosts/*.conf` | Yes |
| Disabled vhosts, with their routes | `vhosts/*.conf.disabled` (the file name is what says "disabled") | Yes |
| Vhost default pool, extra blocks, IP rules | `vhosts/*.meta.json` | Yes |
| Migrations in progress, split routing | `migrations/*.state.json` | Yes |
| Completed migrations, orphaned source data | `migrations/*.done.json` | Yes |
| Bucket list | Live `ListBuckets` on each pool | Yes, if the pools are reachable |
| Audit log | — | No |

Without a sidecar a pool is imported as type `nanio` with no credentials, and a vhost has no
default pool. Fix those in the UI afterwards.

What comes from where:

| From the `.conf` files | From the `.meta.json` sidecars |
|------------------------|--------------------------------|
| Pools and their enabled members, load-balancing method | Pool type, description, encrypted credentials |
| Vhosts: hostname, port, TLS | Each member's role, and the members that are disabled |
| Enabled routes | Vhost default pool, extra blocks, IP rules, enabled state |
| | Disabled routes |

The sidecars are rewritten with every change nginx accepts, so the directory is always enough
to rebuild from. Back up the two kinds of file together.

### 3. The nginx config directory is damaged or deleted

The database is intact, so regenerate the files from it:

```bash
sudo nanio-orchestrator config rebuild
sudo chown -R nanio-orchestrator: /etc/nginx/nanio              # the command ran as root
```

or, with the service running, Configuration → *Rebuild all from the database* (no ownership fix needed). Every file is written, validated with `nginx -t` and nginx is
reloaded; if validation fails the previous files are put back.

The `.meta.json` sidecars are rewritten too (pool type, encrypted credentials, vhost default
pool, extra blocks, IP rules), so after this the directory is again enough to rebuild the
database from.

### 4. `config.env` is lost

Recreate it by running the installer again; it does not overwrite existing files and generates a
new API key and a new encryption secret. Replace that secret with the one you saved:

```bash
sudo nanio-orchestrator install
sudo nanio-orchestrator config set secret '<your saved Fernet key>'
sudo systemctl restart nanio-orchestrator
```

If the Fernet key is gone for good, generate a new one
(`nanio-orchestrator config generate-secret --set`), restart, and enter the credentials of each
pool again. Everything else keeps working; only bucket discovery and migrations need
credentials.

### 5. The whole gateway machine is lost

On the replacement machine:

1. Install nginx and rclone.
2. Restore `/etc/nanio-orchestrator/config.env` and `/etc/nginx/nanio/` from backup.
3. Run the installer ([installation.md](installation.md)). It keeps the restored files, adds the
   nginx include and reloads nginx — the gateway is serving again at this point.
4. Restore `/opt/nanio-orchestrator/data/` from backup, **or** rebuild the database:
   `sudo nanio-orchestrator rebuild-db --force`.
5. `sudo chown -R nanio-orchestrator: /opt/nanio-orchestrator/data && sudo systemctl restart nanio-orchestrator`.
6. `sudo nanio-orchestrator doctor`.

With no backup of the nginx directory either, the configuration has to be entered again; the
objects on the storage nodes are untouched.

### 6. A storage node is lost

The orchestrator does not copy data between nodes. Its part is to stop sending traffic there:

- nginx already skips a member that fails (`max_fails` / `fail_timeout_s`).
- To take it out explicitly, disable or delete the member, then reload nginx.
- In an `http` pool, members with role `replica` take over automatically when every `primary`
  is down. To make a replica permanent, change its role to `primary`.
- When the replacement machine is ready (data restored by your replication tooling), add it as
  a member and reload.

### 7. A migration stopped half-way

No data is lost and the bucket stays reachable. See
[migrations.md](migrations.md#when-it-does-not-finish) for how to finish it.

### 8. Files and database disagree

See *Check and fix drift* in [operations.md](operations.md#check-and-fix-drift).

---

## Checklist: test your recovery

Run this once on a staging gateway so the first time is not during an incident:

1. Stop the service and move the database away.
2. `rebuild-db --dry-run`: are all pools and vhosts listed, with "credentials recovered"?
3. `rebuild-db`, start the service, compare with the original in the UI.
4. Confirm `GET /api/health` reports `drift_alerts: 0`.
