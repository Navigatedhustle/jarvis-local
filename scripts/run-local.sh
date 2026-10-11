#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
PY="${PROJECT_PYTHON:-$PWD/.venv/bin/python}"
PORT="${PROJECT_PORT:-18100}"
if [ ! -x "$PY" ]; then echo 'Install the project environment first; see docs/OPERATIONS.md.'; exit 1; fi
"$PY" -c 'import socket,sys; s=socket.socket(); s.bind(("127.0.0.1",int(sys.argv[1]))); s.close()' "$PORT"
mkdir -p data logs artifacts out run runtime var
export PYTHONPATH="$PWD/src:$PWD${PYTHONPATH:+:$PYTHONPATH}"
exec "$PY" -m uvicorn jarvis.api:app --host 127.0.0.1 --port "$PORT" --no-access-log
