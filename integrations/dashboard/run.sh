#!/bin/sh
# Start Team's gateway dashboard on 127.0.0.1 (default port 8791; TEAM_DASHBOARD_PORT overrides).
# It reads and writes catalog.json and the Portkey/LiteLLM .env files; see server.py.
set -eu
DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 -I "$DIR/server.py" --port="${TEAM_DASHBOARD_PORT:-8791}"
