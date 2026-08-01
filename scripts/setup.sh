#!/bin/zsh
# One-time environment setup: venv + deps + sanity checks.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== checking prerequisites =="
command -v python3 >/dev/null || { echo "✗ python3 not found"; exit 1; }
command -v claude  >/dev/null || { echo "✗ claude CLI not found on PATH"; exit 1; }
[ -d "/Applications/Google Chrome.app" ] || { echo "✗ Google Chrome not installed (required — we drive real Chrome)"; exit 1; }
echo "✓ python3, claude, Chrome present"

echo "== creating venv =="
python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
echo "✓ dependencies installed (no Playwright browser download needed — using system Chrome)"

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "→ created .env — EDIT IT with your Gmail app password before continuing"
else
  echo "✓ .env exists"
fi

mkdir -p data/logs data/thumbs data/debug data/browser_profile

cat <<'EOF'

Next steps:
  1. Edit .env (Gmail app password: Google Account -> Security -> App passwords)
  2. Edit config/targets.yaml and config/settings.yaml (set location_slug!)
  3. PYTHONPATH=src .venv/bin/python -m fbmp.main login
  4. PYTHONPATH=src .venv/bin/python -m fbmp.main targets-lint
  5. PYTHONPATH=src .venv/bin/python -m fbmp.main cycle --dry-run
  6. PYTHONPATH=src .venv/bin/python -m fbmp.main test-email
  7. PYTHONPATH=src .venv/bin/python -m fbmp.main cycle --once
  8. scripts/install_launchd.sh
EOF
