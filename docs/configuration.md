# Configuration Reference

All configuration is done via environment variables prefixed `NANIO_ORCHESTRATOR_`.

- **Production**: variables are read from `/etc/nanio-orchestrator/config.env`
- **Development**: variables are read from `dev.env` in the project root

Dev mode is active when `DEV=true` is set, or when `/etc/nanio-orchestrator/config.env` does
not exist.

---

## Complete variable reference

| Variable | Type | Default (prod) | Default (dev) | Description |
|----------|------|---------------|---------------|-------------|
| `NANIO_ORCHESTRATOR_HOST` | str | `0.0.0.0` | `0.0.0.0` | Bind address |
| `NANIO_ORCHESTRATOR_PORT` | int | `8080` | `8080` | Listen port |
| `NANIO_ORCHESTRATOR_API_KEY` | str | *(generated on install)* | `dev` | API key for all requests |
| `NANIO_ORCHESTRATOR_DB_PATH` | str | `/opt/nanio-orchestrator/data/orchestrator.db` | `./dev-data/orchestrator.db` | SQLite database file path |
| `NANIO_ORCHESTRATOR_NGINX_CONFIG_DIR` | str | `/etc/nginx/nanio` | `./dev-data/nginx` | Directory where nginx configs are written |
| `NANIO_ORCHESTRATOR_LOG_LEVEL` | str | `info` | `debug` | Uvicorn/Python log level |
| `NANIO_ORCHESTRATOR_LOG_FILE` | str | *(none)* | *(none)* | Also log to this file (rotated at 10 MB). Unset = journal/stdout only |
| `NANIO_ORCHESTRATOR_DRIFT_INTERVAL` | int | `60` | `60` | Seconds between drift detection checks |
| `NANIO_ORCHESTRATOR_SESSION_TTL` | int | `28800` | `28800` | Session cookie lifetime in seconds (default: 8 h) |
| `NANIO_ORCHESTRATOR_BUCKET_SYNC_INTERVAL` | int | `300` | `300` | Seconds between automatic bucket sync runs |
| `NANIO_ORCHESTRATOR_SECRET` | str | *(none)* | *(none)* | Fernet key for encrypting pool credentials. **Required** if you store pool credentials. Generate with: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `NANIO_ORCHESTRATOR_RCLONE_PATH` | str | `rclone` | `rclone` | Full path or binary name of rclone (required for migrations) |
| `NANIO_ORCHESTRATOR_MIGRATION_MAX_PARALLEL` | int | `2` | `2` | Maximum concurrent rclone migrations |
| `NANIO_ORCHESTRATOR_MIGRATION_BANDWIDTH_LIMIT` | str | *(none)* | *(none)* | rclone `--bwlimit` value, e.g. `50M` |
| `NANIO_ORCHESTRATOR_MIGRATION_CHECKERS` | int | `8` | `8` | rclone `--checkers` value |
| `NANIO_ORCHESTRATOR_MIGRATION_TRANSFERS` | int | `4` | `4` | rclone `--transfers` value |
| `NANIO_ORCHESTRATOR_MIGRATION_MAX_COPY_PASSES` | int | `10` | `10` | Maximum passes in the `copying` phase, and again in the `verifying` phase |
| `NANIO_ORCHESTRATOR_S3_ACCESS_KEY` | str | *(none)* | *(none)* | Fallback S3 access key, used for a pool that has no credentials of its own |
| `NANIO_ORCHESTRATOR_S3_SECRET_KEY` | str | *(none)* | *(none)* | Fallback S3 secret key (see above) |
| `NANIO_ORCHESTRATOR_S3_REQUEST_TIMEOUT` | int | `3600` | `3600` | Socket timeout in seconds for S3 HTTP requests |
| `NANIO_ORCHESTRATOR_DB_BACKUP_PATH` | str | `<db_path>.bak` | `<db_path>.bak` | Path for rotating DB backups |
| `NANIO_ORCHESTRATOR_DB_BACKUP_INTERVAL` | int | `300` | `300` | Seconds between timed backups |
| `NANIO_ORCHESTRATOR_DB_BACKUP_ROTATE` | int | `3` | `3` | Number of backup copies to keep |

---

## Changing a setting

Settings are read once, at startup. Change the file, then restart the service.

```bash
sudo nanio-orchestrator config show                       # everything, secrets masked
sudo nanio-orchestrator config get bucket_sync_interval
sudo nanio-orchestrator config set bucket_sync_interval 120
sudo nanio-orchestrator config edit                       # opens the file in $EDITOR
sudo systemctl restart nanio-orchestrator
```

Keys are the variable name without the `NANIO_ORCHESTRATOR_` prefix, in lower case. The
Settings page of the Web UI shows the same values and can edit most of them
(`PUT /api/config/settings/{key}`), followed by *Restart Now*.

## What each group is for

| Group | Settings | When to touch them |
|-------|----------|--------------------|
| Core | `HOST`, `PORT`, `API_KEY`, `LOG_LEVEL`, `LOG_FILE`, `SESSION_TTL` | Bind the panel to an internal address; rotate the API key |
| Database | `DB_PATH`, `DB_BACKUP_PATH`, `DB_BACKUP_INTERVAL`, `DB_BACKUP_ROTATE` | Put the backups on another disk |
| Security | `SECRET` | Once, before storing pool credentials |
| S3 | `BUCKET_SYNC_INTERVAL`, `S3_REQUEST_TIMEOUT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` | How often buckets are discovered |
| Nginx | `NGINX_CONFIG_DIR`, `DRIFT_INTERVAL` | Rarely |
| Migrations | `RCLONE_PATH`, `MIGRATION_*` | Speed versus load, see [migrations.md](migrations.md#tuning) |

## Securing the panel

The orchestrator can change what nginx serves and holds the credentials of every pool. Treat it
as an internal administration tool:

- Bind it to a management address (`HOST`) and restrict port 8080 with the firewall.
- It speaks plain HTTP. For remote access put it behind a TLS-terminating reverse proxy or a VPN.
- There is a single API key, which is also the Web UI password. Rotate it with
  `config set api_key <new>` and restart; every session is invalidated.
- `config.env` is readable only by root and the service user. Keep it that way: it contains
  the API key and the Fernet key.

---

## Inline comments in config files

Values may contain inline `#` comments (shell style) — they are stripped automatically:

```env
NANIO_ORCHESTRATOR_MIGRATION_MAX_PARALLEL=2  # max concurrent migrations
```

---

## Generating the Fernet key

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Copy the output into `config.env`:

```env
NANIO_ORCHESTRATOR_SECRET=your-generated-key-here
```

**Keep this key safe.** Losing it means losing access to all stored pool credentials (but the
orchestrator will still function — you will need to re-enter credentials for each pool).

---

## Nginx config directory layout

```
/etc/nginx/nanio/
├── pools/
│   ├── pool-2025.conf          # upstream block for pool "pool-2025"
│   ├── pool-2025.meta.json     # sidecar: type, description, encrypted credentials
│   ├── pool-cdn.conf
│   └── pool-cdn.meta.json
└── vhosts/
    ├── s3.example.pt.conf      # server block for vhost "s3.example.pt"
    └── s3.example.pt.meta.json # sidecar: default_pool_id, extra_blocks, ip_rules
```

These files are managed by the orchestrator. A manual edit is reported as drift and is
overwritten the next time that pool or vhost is saved; see *Check and fix drift* in
[operations.md](operations.md#check-and-fix-drift).

Migration state is kept next to the database, not in the nginx directory:

```
/opt/nanio-orchestrator/data/
├── orchestrator.db
├── orchestrator.db.bak            # newest backup, then .bak.2, .bak.3
└── migrations/
    ├── migration-7.state.json     # migration in progress (or stopped with split routing active)
    └── migration-7.done.json      # completed migration: where the old copy is
```
