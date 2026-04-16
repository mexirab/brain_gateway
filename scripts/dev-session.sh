#!/usr/bin/env bash
# Persistent Claude Code dev session via tmux.
# Creates or reattaches to a named session with 3 windows:
#   1. Claude Code   2. Docker logs   3. Shell
#
# Usage:
#   ./dev-session.sh brain     # Brain Gateway
#   ./dev-session.sh conjure   # Conjure app
#   ./dev-session.sh           # defaults to brain

set -euo pipefail

PROJECT="${1:-brain}"

case "$PROJECT" in
    brain)
        SESSION="brain-dev"
        DIR="/opt/gateway_mvp"
        LOG_CONTAINER="brain-orchestrator"
        ;;
    conjure)
        SESSION="conjure-dev"
        DIR="/opt/helios/conjure"
        LOG_CONTAINER=""
        ;;
    *)
        echo "Usage: $0 [brain|conjure]"
        exit 1
        ;;
esac

# If session exists, auto-pull and reattach
if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "Syncing with remote..."
    cd "$DIR"
    git fetch origin main --quiet
    LOCAL=$(git rev-parse HEAD)
    REMOTE=$(git rev-parse origin/main)
    if [ "$LOCAL" != "$REMOTE" ]; then
        echo "Pulling latest changes..."
        git pull --ff-only
    fi
    echo "Reattaching to $SESSION..."
    exec tmux attach -t "$SESSION"
fi

# Create new session
echo "Creating $SESSION session..."
cd "$DIR"

# Pull latest before starting
git fetch origin main --quiet
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)
if [ "$LOCAL" != "$REMOTE" ]; then
    echo "Pulling latest changes..."
    git pull --ff-only
fi

# tmux uses 0-based window indices by default. new-session creates window 0
# automatically; we rename it to 'claude' and add 'logs' + 'shell' as 1 and 2.
tmux new-session -d -s "$SESSION" -c "$DIR"

# Window 0: Claude Code
tmux rename-window -t "$SESSION:0" "claude"
tmux send-keys -t "$SESSION:0" "claude" C-m

# Window 1: Docker logs (if applicable)
if [ -n "$LOG_CONTAINER" ]; then
    tmux new-window -t "$SESSION" -n "logs" -c "$DIR"
    tmux send-keys -t "$SESSION:1" "docker logs $LOG_CONTAINER --tail 50 -f" C-m
fi

# Window 2: Shell (or window 1 if no log container)
tmux new-window -t "$SESSION" -n "shell" -c "$DIR"

tmux select-window -t "$SESSION:0"
exec tmux attach -t "$SESSION"
