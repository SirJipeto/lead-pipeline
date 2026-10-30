#!/bin/bash
# Start LeadPipeline. Listens on this machine only unless HOST is set.
# Single worker is intentional: pipeline status is kept in process memory.
set -e

cd "$(dirname "$0")"

if [ ! -x .venv/bin/gunicorn ]; then
    echo "Dependencies not installed. Run ./setup.sh first."
    exit 1
fi

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-5000}"

echo "LeadPipeline: http://${HOST}:${PORT}"
exec .venv/bin/gunicorn --bind "${HOST}:${PORT}" --workers 1 --threads 8 --timeout 300 app:app
