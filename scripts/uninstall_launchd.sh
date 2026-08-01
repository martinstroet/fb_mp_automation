#!/bin/zsh
set -euo pipefail
AGENTS="$HOME/Library/LaunchAgents"
for name in cycle digest; do
  launchctl bootout "gui/$(id -u)/com.fbmp.$name" 2>/dev/null || true
  rm -f "$AGENTS/com.fbmp.$name.plist"
  echo "✓ removed com.fbmp.$name"
done
