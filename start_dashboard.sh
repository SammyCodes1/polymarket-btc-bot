#!/usr/bin/env bash
# One-command launcher for the Polymarket BTC bot dashboard (macOS / Linux).
# First run: creates .venv and installs requirements. Then starts the dashboard and opens your browser.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
  PY=""
  for c in python3.12 python3.13 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then PY="$c"; break; fi
  done
  if [ -z "$PY" ]; then echo "[!] Python 3.11+ is required (https://www.python.org/downloads/)."; exit 1; fi
  echo "First run: creating .venv with $PY ..."
  "$PY" -m venv .venv
  .venv/bin/python -m pip install --upgrade pip >/dev/null
  echo "Installing requirements (this can take a minute) ..."
  .venv/bin/python -m pip install -r requirements.txt
fi

if ! .venv/bin/python -c "import fastapi, uvicorn, psutil, requests, dotenv" 2>/dev/null; then
  .venv/bin/python -m pip install -r requirements.txt
fi

echo
echo "  Dashboard: http://127.0.0.1:${DASHBOARD_PORT:-8787}/ unless you passed --port   (Ctrl+C stops the dashboard; a running bot keeps running)"
echo
exec .venv/bin/python dashboard/server.py --open "$@"
