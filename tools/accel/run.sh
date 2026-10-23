#!/bin/sh
# Start the Apple GPU helper (macOS only). First run creates a venv next to it.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
if [ ! -x "$here/.venv/bin/python" ]; then
  py=$(command -v python3.12 || command -v python3.11 || command -v python3.10 || command -v python3)
  "$py" -m venv "$here/.venv"
  "$here/.venv/bin/pip" install -q onnxruntime==1.22.1 numpy==2.2.6
fi
exec "$here/.venv/bin/python" "$here/server.py" "$@"
