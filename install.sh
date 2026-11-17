#!/usr/bin/env bash
# Install or upgrade nanio-orchestrator. Safe to run again at any time.
#
#   curl -fsSL https://raw.githubusercontent.com/nunokisc/nanio-orchestrator/main/install.sh | sudo bash
#
# Options (after `bash -s --` when piping):
#   --version X.Y.Z   install that release instead of the latest
#   --source PATH     install from a local checkout or wheel (air-gapped hosts)
#   --no-start        install everything but do not start the service
set -euo pipefail
umask 022

PREFIX=/opt/nanio-orchestrator
PACKAGE=nanio-orchestrator

main() {
    local spec="$PACKAGE" install_args=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --version)  spec="$PACKAGE==$2"; shift 2 ;;
            --source)   spec="$(realpath "$2")"; shift 2 ;;
            --no-start) install_args+=(--no-start); shift ;;
            -h|--help)  echo "Usage: install.sh [--version X.Y.Z | --source PATH] [--no-start]"; exit 0 ;;
            *) echo "Unknown option: $1" >&2; exit 1 ;;
        esac
    done

    if [[ $EUID -ne 0 ]]; then
        echo "Run as root: sudo bash install.sh" >&2
        exit 1
    fi

    if ! command -v python3 >/dev/null || ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))'; then
        echo "Python 3.9 or newer is required (apt install python3 / dnf install python3)." >&2
        exit 1
    fi
    if ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
        echo "Python's venv module is missing. Install it first:" >&2
        echo "  apt install python3-venv     (Debian, Ubuntu)" >&2
        exit 1
    fi

    if [[ ! -x "$PREFIX/venv/bin/python" ]]; then
        echo "Creating $PREFIX/venv"
        mkdir -p "$PREFIX"
        python3 -m venv "$PREFIX/venv"
    fi

    echo "Installing $spec"
    "$PREFIX/venv/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade "$spec"

    exec "$PREFIX/venv/bin/nanio-orchestrator" install ${install_args[@]+"${install_args[@]}"}
}

main "$@"
