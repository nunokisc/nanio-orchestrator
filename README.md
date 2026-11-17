# nanio-orchestrator

Nginx configuration manager and gateway orchestrator for a distributed
[nanio](https://github.com/apocas/nanio) S3-compatible storage cluster.

It is a **control plane tool only** — traffic never flows through it. If the orchestrator is
stopped or crashes, nginx keeps serving traffic exactly as configured.

## What it is for

nanio is a minimal S3 server: one process, one data directory, no clustering. To grow beyond one
machine you put nginx in front of several nanio instances. nanio-orchestrator manages that
nginx for you:

- **Sharding**: keep most buckets on a default pool and route individual buckets to other
  machines, behind a single hostname.
- **Replicas**: tell it which machines are replicas and it writes them into the upstream as
  backups. Keeping a replica in sync (rsync, lsyncd, a replicated filesystem) stays where it
  belongs, in the operating system.
- **Live migration**: move a bucket from one pool to another with rclone while clients keep
  reading and writing it.
- **Safety net**: every change is validated with `nginx -t` and rolled back if nginx rejects
  it; the database can be rebuilt from the files on disk and the files from the database.

```
CLIENT (S3 SDK / aws-cli / rclone)
│ HTTPS
▼
NGINX (gateway machine) ─────────────── config written by ──┐
│ proxy_pass only                                            │
├─► upstream pool-default  →  nanio instances                │
├─► upstream pool-2026     →  nanio instances        ORCHESTRATOR (:8080, internal)
└─► upstream pool-cdn      →  nginx instances          Web UI · REST API · CLI
                              (+ replicas as backup)   SQLite + files under /etc/nginx/nanio
```

## Concepts

| Concept | nginx | Meaning |
|---------|-------|---------|
| Pool | `upstream` | Machines holding the same data. Type `nanio` (S3) or `http` (read-only file serving, with replicas). |
| Member | `server` | One machine of a pool |
| Vhost | `server {}` | A hostname, with TLS, IP rules and a default pool |
| Route | `location /bucket/` | Sends one bucket, or one folder of a bucket (`/bucket/folder/`), to a specific pool |

## Quick start

Requirements: Linux with systemd, Python 3.9+, nginx. rclone only if you will migrate buckets.

```bash
curl -fsSL https://raw.githubusercontent.com/nunokisc/nanio-orchestrator/main/install.sh | sudo bash
```

That installs the program, creates the settings (API key and encryption secret), hooks the
managed config into nginx, starts the service and prints the Web UI address and the API key.
Run it again to upgrade. `sudo nanio-orchestrator doctor` checks an installation and prints the
fix for anything that is wrong.

Then, in the Web UI:

1. **Pools** → create a pool, add your nanio machines as members, store its S3 credentials.
2. **Vhosts** → create a vhost with that pool as default.
3. **Config** → *Reload nginx*. Changes are validated when you make them and applied only when
   you reload.
4. **Buckets** → promote a bucket to another pool when you want to move it.

The same steps with `curl` are in [docs/operations.md](docs/operations.md).

Keep the panel internal: bind it to a management address and firewall port 8080. It holds the
credentials of every pool and can change what nginx serves.

## Documentation

| | |
|---|---|
| [Installation](docs/installation.md) | Install, upgrade, check (`doctor`), uninstall |
| [Configuration](docs/configuration.md) | Every setting, file layout, securing the panel |
| [Operations manual](docs/operations.md) | Pools, vhosts, sharding, replicas, drift, CLI |
| [Migrations](docs/migrations.md) | How a bucket is moved without losing writes, and how to finish an interrupted move |
| [Disaster recovery](docs/disaster-recovery.md) | What to back up and how to recover each kind of loss |
| [Troubleshooting](docs/troubleshooting.md) | Symptoms, causes, fixes |
| [API reference](docs/api-reference.md) | Every endpoint |
| [Architecture](docs/architecture.md) | How it works inside |
| [Contributing](docs/contributing.md) | Development workflow |

## Design rules

- **Control plane only.** Nothing the orchestrator does sits in the request path.
- **nginx decides.** A change that `nginx -t` rejects is rolled back; it never reaches a reload.
- **Nothing is deleted for you.** Moving a bucket leaves the old copy in place and tracks it;
  removing it is an explicit operator action.
- **Two copies of the truth.** The SQLite database and the files next to the nginx config
  describe the same state; either can be rebuilt from the other.

## Development

```bash
git clone https://github.com/nunokisc/nanio-orchestrator
cd nanio-orchestrator
make install-dev       # creates .venv and installs dependencies
make run               # dev server at http://localhost:8080, API key "dev"
make test              # pytest
make lint              # ruff
```

In dev mode the database and generated config live under `./dev-data/` and nginx commands are
printed instead of executed. See [docs/contributing.md](docs/contributing.md).

## License

MIT
