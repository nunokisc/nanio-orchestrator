# Installation

One command installs, configures and starts nanio-orchestrator on the gateway machine. The same
command upgrades it.

Requirements on the machine:

| Requirement | Notes |
|-------------|-------|
| Linux with systemd | |
| nginx | Installed and running. Any recent version (1.18 or newer recommended). |
| Python 3.9+ with `venv` | Debian/Ubuntu: `apt install python3-venv` |
| rclone | Only for migrations. Can be installed later. |

---

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/nunokisc/nanio-orchestrator/main/install.sh | sudo bash
```

When it finishes the service is running and the output shows the Web UI address and the
generated API key. There are no further steps.

What it does:

| Step | Result |
|------|--------|
| Program | A private virtualenv in `/opt/nanio-orchestrator/venv`, with the `nanio-orchestrator` command linked into `/usr/local/bin` |
| Service user | `nanio-orchestrator` (system user, no login) |
| Settings | `/etc/nanio-orchestrator/config.env` with a generated API key and a generated encryption secret |
| Database | `/opt/nanio-orchestrator/data/orchestrator.db` |
| Managed nginx config | `/etc/nginx/nanio/pools/` and `/etc/nginx/nanio/vhosts/` |
| nginx include | `/etc/nginx/conf.d/nanio-orchestrator.conf`, so nginx loads the managed config. Validated with `nginx -t`; removed again if nginx rejects it. |
| sudoers rule | Lets the service user run `nginx -t`, `nginx -s reload` and restart its own service, nothing else. Validated with `visudo` before it is installed. |
| systemd unit | `nanio-orchestrator.service`, enabled and started |
| Checks | Runs `nanio-orchestrator doctor` and prints the result |

If your `nginx.conf` does not include `conf.d/*.conf`, the installer says so and prints the two
`include` lines to add inside the `http {}` block yourself.

**Save the encryption secret** (`NANIO_ORCHESTRATOR_SECRET` in `config.env`) somewhere safe.
It encrypts the pool credentials; see [disaster-recovery.md](disaster-recovery.md).

### Options

```bash
# A specific release
curl -fsSL .../install.sh | sudo bash -s -- --version 0.5.0

# Install everything but do not start the service
curl -fsSL .../install.sh | sudo bash -s -- --no-start

# No internet access on the gateway: copy a checkout or a wheel to it
sudo bash install.sh --source /path/to/nanio-orchestrator        # or a .whl file
```

`--source` still needs the Python dependencies to be reachable by `pip` (a local package index
or a wheel directory configured in `pip.conf`).

---

## Check an installation

```bash
sudo nanio-orchestrator doctor
```

Lists everything the service needs — settings, permissions, the nginx include, the sudoers rule,
the service itself — and for each problem the command that fixes it. It changes nothing. Run it
first whenever something does not work.

---

## Upgrade

Run the install command again. It upgrades the program, keeps your settings, database and nginx
config, and restarts the service. nginx is not touched, so client traffic is not affected.

The database schema is upgraded automatically on startup.

A new version may also change how nginx config is generated. Existing files are only rewritten
when their pool or vhost is saved, so after an upgrade regenerate them once: Web UI → **Config**
→ *Rebuild All* (or `POST /api/config/rebuild`). Every file is validated with `nginx -t` before
nginx is reloaded, and the previous files are restored if validation fails.

New settings always have defaults: an existing `config.env` keeps working.

---

## Uninstall

```bash
sudo nanio-orchestrator remove
```

Removes the service, its systemd unit, the sudoers rule and the service user. **The managed
nginx config stays, so nginx keeps serving your vhosts.** The database, the settings and the
program stay too; running the installer again brings the service back as it was.

```bash
sudo nanio-orchestrator remove --purge
```

Also deletes `/etc/nginx/nanio`, the nginx include, the database, the settings and the program.
After this nginx stops serving the managed vhosts at its next reload.

---

## Other ways to install

The installer is the supported path. If you manage Python packages yourself, any install works
as long as the `nanio-orchestrator` system user can run it:

```bash
python3 -m venv /opt/nanio-orchestrator/venv
/opt/nanio-orchestrator/venv/bin/pip install nanio-orchestrator
sudo /opt/nanio-orchestrator/venv/bin/nanio-orchestrator install
```

A per-user install (`pipx install`, `uv tool install` as a normal user) lives inside a home
directory the service user cannot enter. `nanio-orchestrator install` detects that and stops
with an explanation instead of creating a service that cannot start.

---

## Docker

The image `ghcr.io/nunokisc/nanio-orchestrator` contains nginx, the orchestrator and rclone. It
is run **twice**, as two containers:

| Container | Runs | Role |
|-----------|------|------|
| `gateway` | nginx | Serves client traffic |
| `orchestrator` | the control plane | Writes the config, validates it with `nginx -t`, asks the gateway to reload |

They are separate so that the rule still holds in Docker: if the orchestrator stops, the
gateway keeps serving. They are the same image so that the nginx that validates the config is
exactly the nginx that serves it.

```bash
curl -fsSLO https://raw.githubusercontent.com/nunokisc/nanio-orchestrator/main/compose.yaml

echo "NANIO_ORCHESTRATOR_API_KEY=$(openssl rand -base64 32)" > .env
echo "NANIO_ORCHESTRATOR_SECRET=$(docker run --rm ghcr.io/nunokisc/nanio-orchestrator config generate-secret)" >> .env

docker compose up -d
```

The panel is on `http://127.0.0.1:8080`; log in with the API key from `.env`. Keep a copy of
`NANIO_ORCHESTRATOR_SECRET`: it encrypts the stored pool credentials.

What to adapt in `compose.yaml`:

- **Ports.** Publish, on the `gateway` service, every port your vhosts listen on.
- **TLS certificates.** Mount them at the same path in both services; the orchestrator needs
  them to validate the config.
- **Settings.** Every setting of [configuration.md](configuration.md) is an environment
  variable of the `orchestrator` service. There is no `config.env` in a container.

How the two containers are tied together (already in `compose.yaml`):

| Shared | Why |
|--------|-----|
| Volume `nginx-config` at `/etc/nginx/nanio` | The orchestrator writes the config, the gateway reads it. **Back it up**: the database can be rebuilt from it. |
| Volume `nginx-run` at `/run`, and `pid: "service:gateway"` | So that `nginx -s reload` from the orchestrator reaches the gateway's nginx. |
| Volume `data` at `/data` (orchestrator only) | Database, its backups, migration state. |

Differences from a host install:

- The storage nodes must be reachable from both containers by the addresses you give the pool
  members.
- The orchestrator container runs as root: it has to signal the gateway's nginx.
- `install`, `doctor` and `remove` are for host installs; they are not used in containers.
- Upgrade with `docker compose pull && docker compose up -d`. Recreating the `gateway`
  container interrupts traffic for a moment; recreating only the orchestrator
  (`docker compose up -d orchestrator`) does not.

---

## Development

```bash
git clone https://github.com/nunokisc/nanio-orchestrator.git
cd nanio-orchestrator
make install-dev          # creates .venv with the dev dependencies (uses uv when available)
make run                  # dev server, no root required
```

`make run` does not need nginx: nginx commands are printed, not executed. To work against a
real cluster — nginx, two nanio servers and rclone — start the development stack instead:

```bash
make stack                # docker compose -f compose.dev.yaml up: panel on :8080, gateway on :8081
make e2e                  # end-to-end test against it (pools, routes, bucket and folder migrations)
make stack-down
```

The stack runs the code in your checkout and reloads when you edit it. The API key is `dev-key`.

The dev server (`make run`):
- Reads config from `dev.env` (copy `dev.env.example` to `dev.env` to customise)
- Stores data in `dev-data/` (created automatically)
- Uses `dev` as the API key
- Does **not** write real nginx config or require nginx to be present
