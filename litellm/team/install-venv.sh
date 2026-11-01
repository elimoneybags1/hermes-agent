#!/bin/sh
# Build LiteLLM's own venv from the litellm/ subtree (its pins clash with Hermes's).
# Re-running refreshes the install in place.
set -eu
DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$DIR/../.." && pwd)"
SRC="${LITELLM_SRC:-$REPO/litellm}"
VENV="$HOME/state/team/litellm-venv"
[ -f "$SRC/pyproject.toml" ] || { echo "install-venv.sh: no LiteLLM source at $SRC" >&2; exit 1; }
[ -x "$VENV/bin/python" ] || uv venv "$VENV" --python 3.11
uv pip install --python "$VENV/bin/python" -e "$SRC[proxy]"
"$VENV/bin/python" -c "import litellm, sys; print('litellm', litellm.__version__ if hasattr(litellm, '__version__') else '?', 'from', litellm.__file__)"
