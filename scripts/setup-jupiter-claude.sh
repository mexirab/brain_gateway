#!/usr/bin/env bash
# One-time setup for Claude Code dev environment on Jupiter.
# Run after cloning or when settings need to be refreshed.
#
# Usage: bash scripts/setup-jupiter-claude.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== Jupiter Claude Code Setup ==="

# Step 1: Install git hooks
echo "[1/3] Installing git hooks..."
bash "$REPO_ROOT/scripts/install-hooks.sh"

# Step 2: Ensure ruff is available
echo "[2/3] Checking ruff..."
if ! command -v ruff &>/dev/null; then
    echo "  Installing ruff..."
    pip3 install --user ruff
    # Verify ruff is actually on PATH after install
    if ! command -v ruff &>/dev/null; then
        echo "  WARNING: ruff installed but not on PATH."
        echo "  Add this to ~/.bashrc: export PATH=\"\$HOME/.local/bin:\$PATH\""
        echo "  Then run: source ~/.bashrc"
        exit 1
    fi
else
    echo "  ruff $(ruff --version) already installed."
fi

# Step 3: Generate settings.local.json if missing
LOCAL_SETTINGS="$REPO_ROOT/.claude/settings.local.json"
if [[ -f "$LOCAL_SETTINGS" ]]; then
    echo "[3/3] settings.local.json already exists — skipping."
    echo "  To regenerate, delete it first: rm $LOCAL_SETTINGS"
else
    echo "[3/3] Creating settings.local.json..."
    cat > "$LOCAL_SETTINGS" << 'SETTINGS_EOF'
{
  "permissions": {
    "allow": [
      "Bash(ssh:*)",
      "Bash(scp:*)",
      "Bash(ssh labadmin@10.0.0.*:*)",
      "Bash(scp:*)",
      "Bash(git push:*)",
      "Bash(git add:*)",
      "Bash(git commit:*)",
      "Bash(git branch:*)",
      "Bash(git remote:*)",
      "Bash(git fetch:*)",
      "Bash(git pull:*)",
      "Bash(git rm:*)",
      "Bash(git status:*)",
      "Bash(git diff:*)",
      "Bash(git log:*)",
      "Bash(git checkout:*)",
      "Bash(git stash:*)",
      "Bash(git reset:*)",
      "Bash(kill:*)",
      "Bash(python3:*)",
      "Bash(python -m pytest:*)",
      "Bash(ruff check:*)",
      "Bash(ruff format:*)",
      "Bash(docker compose:*)",
      "Bash(docker logs:*)",
      "Bash(docker exec:*)",
      "Bash(docker restart:*)",
      "Bash(curl:*)",
      "Bash(bash scripts/install-hooks.sh)",
      "Bash(chmod +x:*)",
      "Bash(pip install:*)",
      "Bash(pip3 install:*)",
      "Bash(pip3 list:*)",
      "Bash(npm run build:*)",
      "Bash(npx --no-install eslint:*)",
      "Bash(gh secret:*)",
      "WebFetch(domain:github.com)",
      "WebFetch(domain:community.home-assistant.io)",
      "WebFetch(domain:developers.home-assistant.io)"
    ]
  }
}
SETTINGS_EOF
    echo "  Created $LOCAL_SETTINGS"
fi

echo ""
echo "Setup complete."
echo "Start a dev session with: ./scripts/dev-session.sh brain"
