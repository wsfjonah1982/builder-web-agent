#!/usr/bin/env bash
# Sets up (once) and runs the Porto Agent Flask site on Ubuntu/Debian.
# Usage: ./start_portoagent.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$SCRIPT_DIR/.venv"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found. Install it first: sudo apt-get update && sudo apt-get install -y python3 python3-venv" >&2
  exit 1
fi

if [ ! -f "$VENV_DIR/bin/activate" ]; then
  if [ -d "$VENV_DIR" ]; then
    echo "Found an incomplete virtual environment at $VENV_DIR - removing it and recreating." >&2
    rm -rf "$VENV_DIR"
  fi
  echo "Creating virtual environment at $VENV_DIR ..."
  python3 -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

pip install --quiet --upgrade pip
pip install --quiet -r "$SCRIPT_DIR/requirements.txt"

cd "$SCRIPT_DIR/main"
exec python app.py
