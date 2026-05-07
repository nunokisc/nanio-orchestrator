# Installation

nanio-orchestrator is a Python application distributed as a PyPI package. It requires
**Python 3.9+** and **nginx** already installed on the target machine.

---

## Production — from PyPI (recommended)

### 1. Install the tool

Pick one method:

```bash
# pipx (isolated environment, recommended for CLI tools)
pipx install nanio-orchestrator

# uv tool (same concept, faster)
uv tool install nanio-orchestrator

# pip into a virtualenv
python3 -m venv /opt/nanio-venv
/opt/nanio-venv/bin/pip install nanio-orchestrator
```

After installation the `nanio-orchestrator` command is available on `$PATH`.

### 2. Run the installer

```bash
sudo nanio-orchestrator install
```

This performs the following (requires root):

| Step | What happens |
|------|-------------|
| Creates dirs | `/opt/nanio-orchestrator/data/`, `/etc/nginx/nanio/pools/`, `/etc/nginx/nanio/vhosts/` |
| Creates config | `/etc/nanio-orchestrator/config.env` with a generated API key |
| Creates user | `nanio-orchestrator` system user (no login shell) |
| Writes sudoers | Allows service user to run `nginx -t` and `nginx -s reload` without password |
| Installs systemd | `/etc/systemd/system/nanio-orchestrator.service` |
| Initialises DB | SQLite schema at `/opt/nanio-orchestrator/data/orchestrator.db` |

### 3. Configure nginx to include managed configs

Add these two includes to your main `nginx.conf` (inside the `http {}` block):

```nginx
include /etc/nginx/nanio/pools/*.conf;
include /etc/nginx/nanio/vhosts/*.conf;
```

Then reload nginx once manually:

```bash
sudo nginx -t && sudo nginx -s reload
```

### 4. Start and enable the service

```bash
sudo systemctl enable --now nanio-orchestrator
sudo systemctl status nanio-orchestrator
```

### 5. Get your API key

```bash
sudo grep API_KEY /etc/nanio-orchestrator/config.env
```

---

## Production — from source (air-gapped / pinned version)

```bash
git clone https://github.com/nunokisc/nanio-orchestrator.git
cd nanio-orchestrator

# Build the wheel
make build                  # produces dist/nanio_orchestrator-*.whl

# Copy wheel to target server, then:
pipx install nanio_orchestrator-*.whl
# or:
pip install nanio_orchestrator-*.whl

sudo nanio-orchestrator install
```

Alternatively use the provided bootstrap script:

```bash
# On the target server (cloned or rsync'd from CI):
sudo bash scripts/bootstrap.sh --prod --source /path/to/nanio-orchestrator
```

---

## Development

```bash
git clone https://github.com/nunokisc/nanio-orchestrator.git
cd nanio-orchestrator

# With uv (recommended)
uv sync
source .venv/bin/activate

# With pip
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Start dev server (no root required, writes to dev-data/)
make run
# or:
DEV=true python -m nanio_orchestrator
```

The dev server:
- Reads config from `dev.env` (copy `dev.env.example` to `dev.env` to customise)
- Stores data in `dev-data/` (created automatically)
- Uses `dev` as the API key
- Does **not** write real nginx config or require nginx to be present

---

## Upgrading

### From PyPI

```bash
pipx upgrade nanio-orchestrator
# or:
uv tool upgrade nanio-orchestrator
```

The database schema is migrated automatically on startup — no manual steps needed.

### Config file changes

New settings added in later versions always have defaults. The existing
`/etc/nanio-orchestrator/config.env` continues to work without modification.

---

## Uninstalling

```bash
# Remove service, dirs, config (keeps data unless --purge)
sudo nanio-orchestrator remove

# Also remove data directory and database
sudo nanio-orchestrator remove --purge

# Then uninstall the package
pipx uninstall nanio-orchestrator
```

---

## System requirements

| Requirement | Minimum |
|------------|---------|
| Python | 3.9+ |
| nginx | Any recent version (≥ 1.18 recommended) |
| rclone | Required only for migrations; any recent version |
| OS | Linux (systemd-based distributions) |
| Disk | ~50 MB for the app; DB grows with number of pools/vhosts |
| RAM | ~50 MB resident |
