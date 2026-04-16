#!/usr/bin/env bash
# bootstrap.sh — bare-server bootstrap for nanio-orchestrator
# Usage: bash scripts/bootstrap.sh --prod --source /path/to/clone
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

PROD=false
SOURCE_DIR=""
INSTALL_DIR="/opt/nanio-orchestrator"

while [[ $# -gt 0 ]]; do
    case $1 in
        --prod)     PROD=true; shift ;;
        --source)   SOURCE_DIR="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: bash bootstrap.sh [--prod] [--source /path/to/clone]"
            echo ""
            echo "Options:"
            echo "  --prod     Install for production under /opt/nanio-orchestrator"
            echo "  --source   Path to the project source directory"
            exit 0
            ;;
        *) error "Unknown option: $1"; exit 1 ;;
    esac
done

# ── 1. Check Python ──────────────────────────────────────────────────────────

info "Checking Python..."

if ! command -v python3 &>/dev/null; then
    error "python3 not found. Install Python 3.9+ and try again."
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [[ "$PYTHON_MAJOR" -lt 3 ]] || { [[ "$PYTHON_MAJOR" -eq 3 ]] && [[ "$PYTHON_MINOR" -lt 9 ]]; }; then
    error "Python 3.9+ required, found $PYTHON_VERSION"
    exit 1
fi
info "Python $PYTHON_VERSION found"

# ── 2. Determine source directory ────────────────────────────────────────────

if [[ -z "$SOURCE_DIR" ]]; then
    # Assume script is in scripts/ subdir of the project
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    SOURCE_DIR="$(dirname "$SCRIPT_DIR")"
fi

if [[ ! -f "$SOURCE_DIR/pyproject.toml" ]]; then
    error "Cannot find pyproject.toml in $SOURCE_DIR"
    error "Use --source to specify the project directory"
    exit 1
fi
info "Source directory: $SOURCE_DIR"

# ── 3. Try uv, fall back to pip ──────────────────────────────────────────────

USE_UV=false

if command -v uv &>/dev/null; then
    info "uv found, using it for installation"
    USE_UV=true
else
    info "uv not found, attempting to install..."
    if curl -LsSf https://astral.sh/uv/install.sh 2>/dev/null | sh 2>/dev/null; then
        export PATH="$HOME/.local/bin:$PATH"
        if command -v uv &>/dev/null; then
            info "uv installed successfully"
            USE_UV=true
        fi
    fi
    if ! $USE_UV; then
        warn "Could not install uv, falling back to pip"
    fi
fi

# ── 4. Set up installation ───────────────────────────────────────────────────

if $PROD; then
    info "Production install to $INSTALL_DIR"

    # Check root
    if [[ $EUID -ne 0 ]]; then
        error "Production install requires root. Run with sudo."
        exit 1
    fi

    # Copy source
    mkdir -p "$INSTALL_DIR/app"
    rsync -a --delete --exclude='.venv' --exclude='dev-data' --exclude='__pycache__' \
        --exclude='.git' --exclude='dist' --exclude='*.egg-info' \
        "$SOURCE_DIR/" "$INSTALL_DIR/app/"
    info "Copied source to $INSTALL_DIR/app/"

    # Create venv
    if [[ ! -d "$INSTALL_DIR/venv" ]]; then
        python3 -m venv "$INSTALL_DIR/venv"
        info "Created venv at $INSTALL_DIR/venv/"
    fi

    # Install
    if $USE_UV; then
        uv pip install --python "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/app/"
    else
        # Ensure pip exists in venv
        "$INSTALL_DIR/venv/bin/python" -m ensurepip --upgrade 2>/dev/null || true
        "$INSTALL_DIR/venv/bin/pip" install --upgrade pip 2>/dev/null || true
        "$INSTALL_DIR/venv/bin/pip" install "$INSTALL_DIR/app/"
    fi
    info "Installed nanio-orchestrator into venv"

    # Run install subcommand
    "$INSTALL_DIR/venv/bin/nanio-orchestrator" install

else
    # Dev install
    info "Development install"

    cd "$SOURCE_DIR"

    if $USE_UV; then
        uv venv .venv 2>/dev/null || python3 -m venv .venv
        uv pip install --python .venv/bin/python -e ".[dev]"
    else
        if [[ ! -d ".venv" ]]; then
            python3 -m venv .venv
        fi
        .venv/bin/python -m ensurepip --upgrade 2>/dev/null || true
        .venv/bin/pip install --upgrade pip 2>/dev/null || true
        .venv/bin/pip install -e ".[dev]"
    fi

    info "Development install complete"
    echo ""
    echo "  Activate the environment:"
    echo "    source .venv/bin/activate"
    echo ""
    echo "  Start the dev server:"
    echo "    python -m nanio_orchestrator"
    echo "    # or: make run"
    echo ""
fi
