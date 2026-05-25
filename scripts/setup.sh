#!/usr/bin/env bash
# Brain Gateway — interactive first-boot setup wizard (CLI)
#
# Walks the user through the same 7 steps the (now-deprecated) web wizard
# covered: Identity, Model, Voice, Push channels, Integrations, Selfcare,
# Review. Writes via the orchestrator's existing /api/setup/* and
# /api/config/* endpoints over localhost:8888 using the API_TOKEN bearer.
#
# Idempotent until `setup_completed: true` — then the orchestrator's kill
# switch makes /api/setup/env return 410 and this script refuses to proceed.
#
# Usually invoked automatically by install.sh at the end of Stage 2; can
# also be run manually after a `docker compose up -d`.
#
# Usage:  bash scripts/setup.sh
set -euo pipefail

# ── Constants + paths ──────────────────────────────────────────────────────
# setup.sh ships at <repo-root>/scripts/setup.sh, so the repo root is always
# one level up from $SCRIPT_DIR. Don't try to be clever with `git rev-parse`
# + `||` here — shell precedence (A || B && C) doubles the output when A
# succeeds and produces a corrupt path.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"
ORCH="${ORCHESTRATOR_URL:-http://localhost:8888}"

# ── Colors (TTY only) ──────────────────────────────────────────────────────
if [ -t 1 ]; then
    RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'
    CYAN=$'\033[0;36m'; BOLD=$'\033[1m'; DIM=$'\033[2m'; NC=$'\033[0m'
else
    RED=""; GREEN=""; YELLOW=""; CYAN=""; BOLD=""; DIM=""; NC=""
fi

say()  { printf '\n%s==>%s %s%s%s\n'   "${CYAN}"  "${NC}" "${BOLD}" "$*" "${NC}"; }
ok()   { printf '%s✓%s %s\n'           "${GREEN}" "${NC}" "$*"; }
warn() { printf '%s!%s %s\n'           "${YELLOW}" "${NC}" "$*"; }
info() { printf '%s  %s%s\n'           "${DIM}"   "$*"  "${NC}"; }
die()  { printf '%s✗%s %s\n'           "${RED}"   "${NC}" "$*" >&2; exit 1; }

# ── Dependency check ───────────────────────────────────────────────────────
need_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        die "Required command '$1' is missing. ${2:-Install it and re-run.}"
    fi
}

need_cmd curl "(apt install -y curl)"
need_cmd jq   "(apt install -y jq)"

# ── Load API_TOKEN from .env ───────────────────────────────────────────────
if [ ! -f "${ENV_FILE}" ]; then
    die ".env not found at ${ENV_FILE}. Run install.sh first."
fi
API_TOKEN="$(grep -E '^API_TOKEN=' "${ENV_FILE}" | tail -1 | cut -d= -f2-)"
if [ -z "${API_TOKEN}" ] || [ "${API_TOKEN}" = "your-api-token-here" ]; then
    die "API_TOKEN not set in ${ENV_FILE}. install.sh should have generated one."
fi

# ── HTTP helpers ───────────────────────────────────────────────────────────
api_get() {
    # Usage: api_get /api/path
    curl -fsS --max-time 5 \
        -H "Authorization: Bearer ${API_TOKEN}" \
        "${ORCH}$1"
}

api_post() {
    # Usage: api_post /api/path '{"json":"body"}'
    # Don't use `${2:-{}}` as the curl body — bash's ${VAR:-DEFAULT}
    # syntax terminates on the FIRST `}`, so it'd parse as ${2:-{}
    # (default = literal `{`) followed by a stray `}` appended to $2.
    local body="${2:-}"
    [ -z "${body}" ] && body='{}'
    curl -fsS --max-time 10 \
        -X POST \
        -H "Authorization: Bearer ${API_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "${body}" \
        "${ORCH}$1"
}

api_put() {
    # On failure, prints the API response body so the user can see the
    # validation error from the backend (curl -fS would swallow it).
    local body http_code
    body="$(curl -s -o /tmp/setup_api_response.json -w '%{http_code}' --max-time 10 \
        -X PUT \
        -H "Authorization: Bearer ${API_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "$2" \
        "${ORCH}$1")"
    http_code="${body}"
    if [ "${http_code}" -lt 200 ] || [ "${http_code}" -ge 300 ]; then
        warn "PUT ${ORCH}$1 returned HTTP ${http_code}:"
        cat /tmp/setup_api_response.json | jq . 2>/dev/null || cat /tmp/setup_api_response.json
        echo
        return 1
    fi
    cat /tmp/setup_api_response.json
}

# Returns 0 on HTTP 2xx, 1 otherwise. Captures HTTP code in $HTTP_CODE.
api_post_raw() {
    # Same brace-matching footgun as api_post — see comment there.
    local body="${2:-}"
    [ -z "${body}" ] && body='{}'
    HTTP_CODE="$(curl -s -o /tmp/setup_api_response.json -w '%{http_code}' --max-time 10 \
        -X POST \
        -H "Authorization: Bearer ${API_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "${body}" \
        "${ORCH}$1")"
    [ "${HTTP_CODE}" -ge 200 ] && [ "${HTTP_CODE}" -lt 300 ]
}

# ── Prompt helpers ─────────────────────────────────────────────────────────
prompt() {
    # prompt VAR_NAME "Question text" "default value"
    local var="$1" text="$2" default="${3:-}" value=""
    if [ -n "${default}" ]; then
        read -r -p "${GREEN}${text}${NC} [${default}]: " value
        value="${value:-${default}}"
    else
        read -r -p "${GREEN}${text}${NC}: " value
    fi
    printf -v "${var}" '%s' "${value}"
}

prompt_secret() {
    # prompt_secret VAR_NAME "Question text"
    local var="$1" text="$2" value=""
    read -r -s -p "${GREEN}${text}${NC} (input hidden): " value
    echo
    printf -v "${var}" '%s' "${value}"
}

prompt_yn() {
    # prompt_yn VAR_NAME "Question text?" "Y" or "N" (default)
    local var="$1" text="$2" default="${3:-Y}" reply=""
    local prompt_suffix
    if [ "${default^^}" = "Y" ]; then
        prompt_suffix="[Y/n]"
    else
        prompt_suffix="[y/N]"
    fi
    while true; do
        read -r -p "${GREEN}${text}${NC} ${prompt_suffix}: " reply
        reply="${reply:-${default}}"
        case "${reply,,}" in
            y|yes) printf -v "${var}" '%s' "true"; return 0 ;;
            n|no)  printf -v "${var}" '%s' "false"; return 0 ;;
            *)     warn "Please answer y or n." ;;
        esac
    done
}

prompt_choice() {
    # prompt_choice VAR_NAME "Question?" "opt1 opt2 opt3" "default"
    local var="$1" text="$2" options="$3" default="${4:-}" reply=""
    echo "${GREEN}${text}${NC}"
    local i=1
    local choices=()
    for opt in ${options}; do
        if [ "${opt}" = "${default}" ]; then
            printf '    %d) %s %s(default)%s\n' "$i" "${opt}" "${DIM}" "${NC}"
        else
            printf '    %d) %s\n' "$i" "${opt}"
        fi
        choices+=("${opt}")
        i=$((i + 1))
    done
    while true; do
        read -r -p "Choose [1-${#choices[@]}, default ${default}]: " reply
        if [ -z "${reply}" ] && [ -n "${default}" ]; then
            printf -v "${var}" '%s' "${default}"; return 0
        fi
        if [[ "${reply}" =~ ^[0-9]+$ ]] && [ "${reply}" -ge 1 ] && [ "${reply}" -le "${#choices[@]}" ]; then
            printf -v "${var}" '%s' "${choices[$((reply - 1))]}"; return 0
        fi
        warn "Enter a number 1-${#choices[@]} or press Enter for default."
    done
}

# ── Pre-flight ─────────────────────────────────────────────────────────────
echo
printf '%s' "${BOLD}"
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║          Brain Gateway — first-boot setup wizard            ║"
echo "╚══════════════════════════════════════════════════════════════╝"
printf '%s\n' "${NC}"

say "Checking orchestrator health…"
if ! api_get /health >/dev/null 2>&1; then
    die "Can't reach orchestrator at ${ORCH}. Is 'docker compose up -d' done? Try: docker compose logs orchestrator"
fi
ok "Orchestrator is reachable."

# Kill-switch check via /api/setup/status
say "Checking setup status…"
status_json="$(api_get /api/setup/status)" || die "Failed to query /api/setup/status"
already_complete="$(echo "${status_json}" | jq -r '.setup_completed // false')"
if [ "${already_complete}" = "true" ]; then
    warn "Setup is already marked complete (${ENV_FILE%/*}/data/app/setup_state.json)."
    warn "The /api/setup/env endpoints are now LOCKED (return 410)."
    warn "To re-run setup: edit data/app/setup_state.json to set setup_completed=false,"
    warn "then re-run this script."
    exit 1
fi
ok "Setup is unconfigured — first-boot wizard can proceed."

# Helper to write env overrides
write_env() {
    # write_env KEY1=VAL1 KEY2=VAL2 ...
    local json_pairs=""
    for kv in "$@"; do
        local k="${kv%%=*}" v="${kv#*=}"
        # JSON-escape the value (jq handles all the escaping)
        v="$(echo -n "${v}" | jq -Rs '.')"
        if [ -n "${json_pairs}" ]; then json_pairs+=","; fi
        json_pairs+="\"${k}\":${v}"
    done
    local body="{\"values\":{${json_pairs}}}"
    if ! api_post_raw /api/setup/env "${body}"; then
        warn "Write failed (HTTP ${HTTP_CODE}):"
        cat /tmp/setup_api_response.json
        echo
        die "/api/setup/env returned non-2xx. Fix and re-run."
    fi
}

# ═══════════════════════════════════════════════════════════════════════════
# Express setup — 2 questions, ~30 seconds.
# Everything else (Home Assistant, push channels, integrations, selfcare
# nudges) is configured AFTER setup completes — either by chatting with Jess
# ("set up Home Assistant") or from the web Settings page.
# ═══════════════════════════════════════════════════════════════════════════
say "Express setup — 2 questions, ~30 seconds"
info ""
info "Everything optional (smart home, phone push, document storage, selfcare"
info "nudges) is configured AFTER you finish setup, either by asking Jess in"
info "the chat ('set up Home Assistant') or visiting the web Settings page."
info ""

# ── Question 1: name ──────────────────────────────────────────────────────
say "1 / 2 — Your name"
current_identity="$(api_get /api/config/identity 2>/dev/null || echo '{}')"
def_user="$(echo "${current_identity}" | jq -r '.user_name // ""')"
prompt USER_NAME "Your name" "${def_user}"

# ── Question 2: timezone ──────────────────────────────────────────────────
say "2 / 2 — Timezone"
def_tz="$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo 'UTC')"
while true; do
    prompt TIMEZONE "Timezone (IANA format, e.g. America/Chicago)" "${def_tz}"
    TIMEZONE_NORMALIZED="${TIMEZONE// //}"
    if [ -f "/usr/share/zoneinfo/${TIMEZONE_NORMALIZED}" ]; then
        TIMEZONE="${TIMEZONE_NORMALIZED}"
        break
    fi
    warn "'${TIMEZONE}' is not a valid IANA timezone."
    info "Common examples:"
    info "  America/New_York   America/Chicago    America/Denver     America/Los_Angeles"
    info "  Europe/London      Europe/Paris       Europe/Berlin      Asia/Tokyo"
    info "  Australia/Sydney   Pacific/Auckland   UTC"
    info "Full list: ls /usr/share/zoneinfo"
done

# ── Auto-defaults ──────────────────────────────────────────────────────────
ASSISTANT_NAME="Jess"
ADHD_MODE="true"
TONE="warm"
TTS_VOICE="aiden"   # qwen-tts's default generic voice; change later in /settings
MODEL_ID="$(grep -E '^VLLM_MODEL=' "${ENV_FILE}" 2>/dev/null | tail -1 | cut -d= -f2- | sed 's/  *#.*//' || true)"

# ── Write identity (single PUT with all five identity fields) ─────────────
say "Writing your settings…"
identity_body=$(jq -nc \
    --arg user_name "${USER_NAME}" \
    --arg assistant_name "${ASSISTANT_NAME}" \
    --arg timezone "${TIMEZONE}" \
    --argjson adhd_mode "${ADHD_MODE}" \
    --arg tone "${TONE}" \
    '{user_name:$user_name, assistant_name:$assistant_name, timezone:$timezone, adhd_mode:$adhd_mode, tone_preference:$tone}')
api_put /api/config/identity "${identity_body}" >/dev/null
ok "Identity saved (name=${USER_NAME}, tz=${TIMEZONE}, assistant=${ASSISTANT_NAME}, ADHD mode on, tone=warm)"

# Write TTS_VOICE default. The model id was already written to .env by
# install.sh's hardware-scan step — no need to re-write here.
write_env "TTS_VOICE=${TTS_VOICE}"
ok "Voice set to '${TTS_VOICE}' (qwen-tts's default generic voice; change in /settings if you want a different one)"

# ── Recap + complete ──────────────────────────────────────────────────────
say "Summary"
printf '%s  Name:     %s%s\n' "${DIM}" "${NC}" "${USER_NAME}"
printf '%s  Timezone: %s%s\n' "${DIM}" "${NC}" "${TIMEZONE}"
printf '%s  Model:    %s%s\n' "${DIM}" "${NC}" "${MODEL_ID:-(unset — set VLLM_MODEL in .env)}"
printf '%s  Voice:    %s%s\n' "${DIM}" "${NC}" "${TTS_VOICE}"
echo
info "Everything else (Home Assistant, ntfy, Pushover, Paperless, selfcare nudges)"
info "is configured AFTER setup — chat with Jess or visit /settings."
echo
prompt_yn CONFIRM "Mark setup complete" "Y"
if [ "${CONFIRM}" != "true" ]; then
    warn "Setup left in-progress. Re-run this script when you're ready."
    exit 0
fi

api_post /api/setup/complete >/dev/null
ok "Setup complete."

# ── Restart orchestrator so it picks up the new env overrides ──────────────
say "Restarting the orchestrator to pick up new env values…"
if command -v docker >/dev/null 2>&1 && [ -f "${REPO_ROOT}/docker-compose.yml" ]; then
    ( cd "${REPO_ROOT}" && docker compose restart orchestrator 2>&1 | tail -5 ) || warn "Restart returned non-zero (check 'docker compose ps')"
    ok "Orchestrator restarted."
else
    warn "docker / compose not found; restart the orchestrator manually:"
    info "    cd ${REPO_ROOT} && docker compose restart orchestrator"
fi

# ── Final URLs ─────────────────────────────────────────────────────────────
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
printf '%s════════════════════════════════════════════════════════════%s\n' "${GREEN}" "${NC}"
printf '%s✓ Setup complete!%s\n' "${GREEN}${BOLD}" "${NC}"
printf '%s════════════════════════════════════════════════════════════%s\n' "${GREEN}" "${NC}"
echo
say "Dashboard"
info "  http://${LAN_IP}:3001/"
say "Settings (change anything later)"
info "  http://${LAN_IP}:3001/settings"
say "Health check"
info "  curl http://localhost:8888/health"
echo
