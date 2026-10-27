#!/bin/sh
# Start Team's LiteLLM proxy from the litellm/ subtree, loopback only.
# Its environment holds one secret, the master key; every provider key lives
# in Portkey's configs, which LiteLLM reaches by config name.
set -eu
DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$DIR/../.." && pwd)"
STATE="$HOME/state/team/litellm"
VENV="$HOME/state/team/litellm-venv"
CATALOG="$REPO/integrations/portkey/catalog.json"
PORT="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["litellm"]["port"])' "$CATALOG")"
HOST="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["litellm"]["host"])' "$CATALOG")"

set -a
. "$STATE/.env"
set +a
# Never let a provider key, paid or free, reach the proxy: it must not be able to call anyone but Portkey.
for v in $(env | sed -n 's/^\([A-Z0-9_]*_API_KEY\)=.*/\1/p'); do
  [ "$v" = "LITELLM_MASTER_KEY" ] || unset "$v"
done
unset ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_OAUTH_TOKEN 2>/dev/null || true
export LITELLM_TELEMETRY=False
export NO_PROXY=127.0.0.1

# Refuse to serve a config that catalog.json does not generate.
python3 -I "$REPO/integrations/portkey/build.py" --check

[ -x "$VENV/bin/litellm" ] || { echo "run.sh: no venv at $VENV; run install-venv.sh" >&2; exit 1; }
cd "$DIR"  # the callbacks (forward_headers, session_pin, usage_log) import from here
exec "$VENV/bin/litellm" --config "$STATE/config.yaml" --host "$HOST" --port "$PORT"
