#!/bin/sh
# What is actually running: launchd state, the listening socket, config drift,
# the venv, and the last call in usage.db. Exit 1 if any check fails.
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$DIR/../.." && pwd)"
STATE="$HOME/state/team/litellm"
LABEL=com.fullmoney.team-litellm
PORT="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["litellm"]["port"])' "$REPO/integrations/portkey/catalog.json")"
rc=0

if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  echo "launchd  $LABEL: $(launchctl print "gui/$(id -u)/$LABEL" | awk '/^\tstate = /{print $3}') pid $(launchctl print "gui/$(id -u)/$LABEL" | awk '/^\tpid = /{print $3}')"
else
  echo "launchd  $LABEL: not loaded"; rc=1
fi

sock="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print $9}' | head -1)"
if [ -n "$sock" ]; then echo "listen   $sock"; else echo "listen   nothing on :$PORT"; rc=1; fi
case "$sock" in 127.0.0.1:*) ;; "") ;; *) echo "listen   WARNING: not loopback-only"; rc=1;; esac

if out="$(python3 -I "$REPO/integrations/portkey/build.py" --check 2>&1)"; then echo "configs  $out"; else echo "configs  $out"; rc=1; fi

if curl -sf -o /dev/null --max-time 3 "http://127.0.0.1:$PORT/health/liveliness"; then echo "http     /health/liveliness ok"; else echo "http     /health/liveliness failed"; rc=1; fi

if [ -f "$STATE/usage.db" ]; then
  echo "usage.db $(sqlite3 "$STATE/usage.db" 'select count(*) from calls') calls; last: $(sqlite3 "$STATE/usage.db" 'select ts, ok, "group", deployment, session, latency_ms||"ms" from calls order by id desc limit 1')"
else
  echo "usage.db none yet"
fi
exit $rc
