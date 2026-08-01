#!/bin/zsh
# Render plist templates with absolute paths and install them into launchd.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"
AGENTS="$HOME/Library/LaunchAgents"
DIGEST_HOUR=$(.venv/bin/python -c "
import yaml; print((yaml.safe_load(open('config/settings.yaml')).get('alerts') or {}).get('digest_hour', 18))")
CLAUDE_DIR="$(dirname "$(command -v claude)")"
LPATH="/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin:$CLAUDE_DIR"

mkdir -p "$AGENTS"
for name in cycle digest; do
  sed -e "s|__REPO__|$REPO|g" -e "s|__DIGEST_HOUR__|$DIGEST_HOUR|g" -e "s|__PATH__|$LPATH|g" \
    "launchd/com.fbmp.$name.plist.template" > "$AGENTS/com.fbmp.$name.plist"
  launchctl bootout "gui/$(id -u)/com.fbmp.$name" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$AGENTS/com.fbmp.$name.plist"
  echo "✓ installed com.fbmp.$name"
done

echo
echo "Verify with:"
echo "  launchctl print gui/$(id -u)/com.fbmp.cycle | head -20"
echo "  launchctl kickstart gui/$(id -u)/com.fbmp.cycle   # force a run now"
echo "  tail -f data/logs/fbmp.log"
