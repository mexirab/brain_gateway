#!/usr/bin/env bash
# Claude Code Stop hook: forward each completed turn to the Brain Gateway
# orchestrator so Jess can reference recent coding activity.
#
# Claude Code fires this hook after each assistant response finishes.
# Stdin contains JSON with: session_id, transcript_path, stop_hook_active, etc.
#
# Install: add to ~/.claude/settings.json:
#   {
#     "hooks": {
#       "Stop": [{
#         "hooks": [{
#           "type": "command",
#           "command": "/opt/gateway_mvp/scripts/claude_code_stop_hook.sh"
#         }]
#       }]
#     }
#   }
#
# Environment:
#   ORCHESTRATOR_URL  default http://localhost:8888
#   API_TOKEN         bearer token for auth (optional but recommended)

set -euo pipefail

# Fall back to sourcing .env if API_TOKEN isn't already in the environment.
# Claude Code's hook subshell doesn't inherit the user's login env, so
# requiring the user to export API_TOKEN manually in every terminal is
# fragile. Sourcing .env here means the hook "just works" as long as .env
# is populated, which is the canonical source of truth for the token.
if [ -z "${API_TOKEN:-}" ] && [ -f /opt/gateway_mvp/.env ]; then
    set -a
    # shellcheck disable=SC1091
    source /opt/gateway_mvp/.env 2>/dev/null || true
    set +a
fi

ORCH_URL="${ORCHESTRATOR_URL:-http://localhost:8888}"
ENDPOINT="${ORCH_URL}/api/claude_code/turn"

# Read stdin payload
PAYLOAD=$(cat)

# POST to orchestrator — silently fail so hook errors never block Claude Code
if [ -n "${API_TOKEN:-}" ]; then
    curl -sS -X POST "$ENDPOINT" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer ${API_TOKEN}" \
        -d "$PAYLOAD" \
        --max-time 3 \
        > /dev/null 2>&1 || true
else
    curl -sS -X POST "$ENDPOINT" \
        -H "Content-Type: application/json" \
        -d "$PAYLOAD" \
        --max-time 3 \
        > /dev/null 2>&1 || true
fi

# Hook must exit 0 to avoid blocking Claude Code
exit 0
