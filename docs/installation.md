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

## Development

```bash
git clone https://github.com/nunokisc/nanio-orchestrator.git
cd nanio-orchestrator
make install-dev          # creates .venv with the dev dependencies (uses uv when available)
make run                  # dev server, no root required
```

The dev server:
- Reads config from `dev.env` (copy `dev.env.example` to `dev.env` to customise)
- Stores data in `dev-data/` (created automatically)
- Uses `dev` as the API key
- Does **not** write real nginx config or require nginx to be present
