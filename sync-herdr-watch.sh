#!/usr/bin/env sh
# Backward-compatible entry point for the portable Python installer.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON=$(command -v python3 || command -v python) || {
  echo "error: Python 3.9 or newer is required" >&2
  exit 1
}

exec "$PYTHON" "$SCRIPT_DIR/sync_herdr_watch.py" "$@"
