#!/usr/bin/env bash
# Claude Code PostToolUse lint hook
# Receives JSON on stdin with tool_input.file_path
# Runs linter + formatter check based on file extension.
# Exit non-zero to surface errors to Claude so it can fix them.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Read the hook input JSON from stdin
INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

if [[ -z "$FILE_PATH" ]]; then
    exit 0
fi

case "$FILE_PATH" in
    *.py)
        if command -v ruff &>/dev/null; then
            ruff check --no-fix "$FILE_PATH"
            ruff format --check --quiet "$FILE_PATH" 2>/dev/null || {
                echo "Formatting issue detected. Auto-fixing..."
                ruff format "$FILE_PATH"
            }
        fi
        ;;
    *.ts|*.tsx|*.js|*.jsx)
        if [[ "$FILE_PATH" == *"frontend/"* ]]; then
            cd "$REPO_ROOT/frontend"
            npx --no-install eslint --no-error-on-unmatched-pattern "$FILE_PATH" 2>/dev/null || true
        fi
        ;;
esac
