#!/bin/sh
# What is actually running: launchd state, the listening socket, config drift,
# and the last request the gateway logged. Exit 1 if any check fails.
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
STATE="$HOME/state/team/portkey"
LABEL=com.fullmoney.team-portkey
PORT="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["gateway"]["port"])' "$DIR/catalog.json")"
rc=0

if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  echo "launchd  $LABEL: $(launchctl print "gui/$(id -u)/$LABEL" | awk '/^\tstate = /{print $3}') pid $(launchctl print "gui/$(id -u)/$LABEL" | awk '/^\tpid = /{print $3}')"
else
  echo "launchd  $LABEL: not loaded"; rc=1
fi

sock="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print $9}' | head -1)"
if [ -n "$sock" ]; then echo "listen   $sock"; else echo "listen   nothing on :$PORT"; rc=1; fi
case "$sock" in 127.0.0.1:*) ;; "") ;; *) echo "listen   WARNING: not loopback-only"; rc=1;; esac

if out="$(python3 -I "$DIR/build.py" --check 2>&1)"; then echo "configs  $out"; else echo "configs  $out"; rc=1; fi

if curl -sf -o /dev/null --max-time 3 "http://127.0.0.1:$PORT/"; then echo "http     GET / ok"; else echo "http     GET / failed"; rc=1; fi

if [ -s "$STATE/requests.jsonl" ]; then
  echo "requests $(wc -l < "$STATE/requests.jsonl" | tr -d ' ') lines; last: $(tail -1 "$STATE/requests.jsonl" | python3 -I -c 'import json,sys; l=json.loads(sys.stdin.read()); print(l["time"], l["status"], l.get("config"), l.get("provider"), l.get("model_answered"), "trace", l.get("trace_id"))')"
else
  echo "requests none logged yet"
fi
exit $rc
