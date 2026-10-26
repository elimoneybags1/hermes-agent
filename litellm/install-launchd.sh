#!/bin/sh
# Install Team's LiteLLM proxy as an always-on launchd agent (com.fullmoney.team-litellm).
# Re-running replaces the existing agent. Logs: ~/state/team/litellm/proxy.log
set -eu
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL=com.fullmoney.team-litellm
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/state/team/litellm/proxy.log"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$DIR/run.sh</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $LABEL; waiting for health..."
for _ in $(seq 1 60); do
  if curl -sf -o /dev/null http://127.0.0.1:4000/health/liveliness; then echo "proxy healthy on 127.0.0.1:4000"; exit 0; fi
  sleep 1
done
echo "proxy did not come up; see $LOG" >&2
exit 1
