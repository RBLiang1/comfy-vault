#!/usr/bin/env sh
# comfy-vault launcher (macOS / Linux)
set -e
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "[ERROR] Python 3.9+ not found. Install it and try again."
  exit 1
fi

echo "Starting comfy-vault ..."
echo "Web UI : http://127.0.0.1:8791   (change \"port\" in config.json)"
echo "Press Ctrl+C to stop the server."
echo
exec "$PY" app.py
