#!/bin/sh
# Start Team's LiteLLM proxy on localhost only. Keys live outside the repo.
set -eu
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$HOME/.local/share/uv/tools/litellm/bin/python"
set -a
. "$HOME/state/team/litellm/.env"
set +a
# Never let a stray paid key from the environment reach the proxy.
unset ANTHROPIC_API_KEY OPENAI_API_KEY MINIMAX_API_KEY DASHSCOPE_API_KEY 2>/dev/null || true
export LITELLM_TELEMETRY=False
# Refuse to serve a config that free-models.json does not generate.
"$PY" "$DIR/build.py" --check
cd "$DIR"  # LiteLLM loads spend_guard.py relative to the config
exec "$HOME/.local/bin/litellm" --config "$DIR/config.yaml" --host 127.0.0.1 --port 4000
