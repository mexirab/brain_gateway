#!/usr/bin/env bash
# Install git hooks by symlinking from scripts/ to .git/hooks/
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOKS_DIR="$REPO_ROOT/.git/hooks"

ln -sf "$REPO_ROOT/scripts/pre-commit" "$HOOKS_DIR/pre-commit"
chmod +x "$HOOKS_DIR/pre-commit"

echo "Git hooks installed:"
echo "  pre-commit -> scripts/pre-commit"
