#!/bin/sh
# Install Team's LiteLLM proxy as an always-on launchd agent (com.fullmoney.team-litellm).
# Re-running replaces the agent. Log: $HOME/state/team/litellm/proxy.log
set -eu
DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$DIR/../.." && pwd)"
LABEL=com.fullmoney.team-litellm
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
STATE="$HOME/state/team/litellm"
LOG="$STATE/proxy.log"
PORT="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["litellm"]["port"])' "$REPO/integrations/portkey/catalog.json")"

mkdir -p "$STATE" "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$DIR/run.sh</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/usr/local/bin:/usr/bin:/bin</string>
    <key>HOME</key><string>$HOME</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $LABEL; waiting for the proxy on 127.0.0.1:$PORT ..."
i=0
while [ $i -lt 120 ]; do
  if curl -sf -o /dev/null "http://127.0.0.1:$PORT/health/liveliness"; then echo "proxy up on 127.0.0.1:$PORT"; exit 0; fi
  sleep 1; i=$((i+1))
done
echo "proxy did not come up; see $LOG" >&2
exit 1
