#!/bin/sh
# Start Team's Portkey gateway from the portkey/ subtree, loopback only.
# Keys live in $HOME/state/team/portkey/.env and reach the gateway only inside
# the generated configs; the gateway's own environment holds none of them.
set -eu
DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$DIR/../.." && pwd)"
STATE="$HOME/state/team/portkey"
PORT="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["gateway"]["port"])' "$DIR/catalog.json")"
HOST="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["gateway"]["host"])' "$DIR/catalog.json")"

# Never let a paid key from the login environment reach the gateway process.
unset ANTHROPIC_API_KEY OPENAI_API_KEY MINIMAX_API_KEY DASHSCOPE_API_KEY CLAUDE_API_KEY 2>/dev/null || true

# Refuse to serve configs that catalog.json does not generate.
python3 -I "$DIR/build.py" --check

# Rebuild when any source file is newer than the bundle (or there is no bundle).
if [ ! -f "$REPO/portkey/build/start-server.js" ] || \
   [ -n "$(find "$REPO/portkey/src" -newer "$REPO/portkey/build/start-server.js" -type f -print -quit)" ]; then
  echo "run.sh: portkey/src is newer than portkey/build; rebuilding" >&2
  npm --prefix "$REPO/portkey" run build >&2
fi

export PORTKEY_CONFIG_DIR="$STATE/configs"
export PORTKEY_REQUEST_LOG="$STATE/requests.jsonl"
export NODE_ENV=development   # the console at /public/ is only served outside production
exec node "$REPO/portkey/build/start-server.js" "--port=$PORT" "--host=$HOST"
