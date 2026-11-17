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

Two ways to run it. Both end with the panel on port 8080.

### On the gateway host

Requirements: Linux with systemd, Python 3.9+, nginx. rclone only if you will migrate buckets.

```bash
curl -fsSL https://raw.githubusercontent.com/nunokisc/nanio-orchestrator/main/install.sh | sudo bash
```

That installs the program, creates the settings (API key and encryption secret), hooks the
managed config into nginx, starts the service and prints the panel address and the API key.
Run it again to upgrade. `sudo nanio-orchestrator doctor` checks an installation and prints the
fix for anything that is wrong.

### With Docker

The image `ghcr.io/nunokisc/nanio-orchestrator` holds nginx, the orchestrator and rclone. It runs
as **two containers**: `gateway` (nginx, serving traffic) and `orchestrator` (the control plane).
They are separate so that nginx keeps serving when the orchestrator stops, and the same image so
that the nginx validating the config is the nginx serving it.

```bash
curl -fsSLO https://raw.githubusercontent.com/nunokisc/nanio-orchestrator/main/compose.yaml

echo "NANIO_ORCHESTRATOR_API_KEY=$(openssl rand -base64 32)" > .env
echo "NANIO_ORCHESTRATOR_SECRET=$(docker run --rm ghcr.io/nunokisc/nanio-orchestrator config generate-secret)" >> .env

docker compose up -d
```

The panel is on `http://127.0.0.1:8080`; log in with the API key from `.env`. In
[`compose.yaml`](compose.yaml), publish on the `gateway` service every port your vhosts listen
on, and mount your TLS certificates at the same path in both services. Keep a copy of
`NANIO_ORCHESTRATOR_SECRET`: it encrypts the stored pool credentials.

Details, upgrades and what the two containers share:
[docs/installation.md](docs/installation.md#docker).

### First steps

Then, in the web panel (the **Overview** screen walks you through the same steps):

1. **Pools** → create a pool, add your nanio machines as members, store its S3 credentials.
2. **Vhosts** → create a vhost with that pool as default.
3. *Reload nginx*. Changes are validated when you make them and applied only when you reload;
   while something is waiting, a bar at the top of every screen says so and has the button.
4. **Buckets** → *Route to a pool* when you want to move a bucket to other machines.
5. **Explorer** → look inside a bucket and see which pool really holds what (read-only).

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
| [For applications](docs/clients.md) | What an S3 client must know: settings, limits and what to do instead |
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

### A whole cluster in Docker

The quickest way to see it work, and the environment the end-to-end test runs in: the gateway,
the orchestrator and two nanio storage servers, on one machine.

```bash
git clone https://github.com/nunokisc/nanio-orchestrator
cd nanio-orchestrator
make stack             # builds the image and starts everything
```

| | |
|---|---|
| Panel | `http://localhost:8080`, API key `dev-key` |
| Gateway | `http://localhost:8081` (create a vhost that listens on 8081) |
| Storage servers | `nanio-a:9000` and `nanio-b:9000`, access key `AKDEV`, secret `devsecretdevsecret` |

nginx, nanio and rclone are real here, so routing, reloads and migrations behave as in
production. The orchestrator runs the code of your checkout and restarts when you edit it.

```bash
make e2e               # end-to-end test: pools, routes, bucket and folder migrations, explorer
make stack-down        # stop and delete the cluster's data
```

Without `make`: `docker compose -f compose.dev.yaml up -d --build --wait`. The panel is a React
application; `make stack` builds it when Node.js is installed, otherwise run `make ui` on a
machine that has it.

### Without Docker

```bash
make install-dev       # creates .venv and installs dependencies
make ui                # builds the web panel (needs Node.js); once, and after changing ui/
make run               # dev server at http://localhost:8080, API key "dev"
make test              # pytest
make test-cov          # with coverage; fails below 100%
make lint              # ruff
```

In this mode the database and generated config live under `./dev-data/` and nginx commands are
printed instead of executed: good for working on the API and the panel, not for anything that
depends on what nginx or nanio really do.

The panel is in `ui/`; its build is shipped inside the Python package, so only a source checkout
needs Node.js. See [docs/contributing.md](docs/contributing.md).

## License

MIT
