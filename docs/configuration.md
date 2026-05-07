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
| `NANIO_ORCHESTRATOR_LOG_FILE` | str | *(none)* | *(none)* | Path to rotating log file; `None` = stdout only |
| `NANIO_ORCHESTRATOR_DRIFT_INTERVAL` | int | `60` | `60` | Seconds between drift detection checks |
| `NANIO_ORCHESTRATOR_SESSION_TTL` | int | `28800` | `28800` | Session cookie lifetime in seconds (default: 8 h) |
| `NANIO_ORCHESTRATOR_BUCKET_SYNC_INTERVAL` | int | `300` | `300` | Seconds between automatic bucket sync runs |
| `NANIO_ORCHESTRATOR_SECRET` | str | *(none)* | *(none)* | Fernet key for encrypting pool credentials. **Required** if you store pool credentials. Generate with: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `NANIO_ORCHESTRATOR_RCLONE_PATH` | str | `rclone` | `rclone` | Full path or binary name of rclone (required for migrations) |
| `NANIO_ORCHESTRATOR_MIGRATION_MAX_PARALLEL` | int | `2` | `2` | Maximum concurrent rclone migrations |
| `NANIO_ORCHESTRATOR_MIGRATION_BANDWIDTH_LIMIT` | str | *(none)* | *(none)* | rclone `--bwlimit` value, e.g. `50M` |
| `NANIO_ORCHESTRATOR_MIGRATION_CHECKERS` | int | `8` | `8` | rclone `--checkers` value |
| `NANIO_ORCHESTRATOR_MIGRATION_TRANSFERS` | int | `4` | `4` | rclone `--transfers` value |
| `NANIO_ORCHESTRATOR_MIGRATION_MAX_COPY_PASSES` | int | `10` | `10` | Max convergence loop passes in verifying phase |
| `NANIO_ORCHESTRATOR_S3_ACCESS_KEY` | str | *(none)* | *(none)* | S3 access key for polling default pool (optional) |
| `NANIO_ORCHESTRATOR_S3_SECRET_KEY` | str | *(none)* | *(none)* | S3 secret key for polling default pool (optional) |
| `NANIO_ORCHESTRATOR_S3_REQUEST_TIMEOUT` | int | `3600` | `3600` | Socket timeout in seconds for S3 HTTP requests |
| `NANIO_ORCHESTRATOR_DB_BACKUP_PATH` | str | `<db_path>.bak` | `<db_path>.bak` | Path for rotating DB backups |
| `NANIO_ORCHESTRATOR_DB_BACKUP_INTERVAL` | int | `300` | `300` | Seconds between timed backups |
| `NANIO_ORCHESTRATOR_DB_BACKUP_ROTATE` | int | `3` | `3` | Number of backup copies to keep |

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

These files are managed exclusively by the orchestrator. Manual edits are possible but
will be overwritten on the next config sync. Use `POST /api/config/absorb` to adopt
a manually-edited file into the DB.
