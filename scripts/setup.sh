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
    curl -fsS --max-time 10 \
        -X POST \
        -H "Authorization: Bearer ${API_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "${2:-{}}" \
        "${ORCH}$1"
}

api_put() {
    curl -fsS --max-time 10 \
        -X PUT \
        -H "Authorization: Bearer ${API_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "$2" \
        "${ORCH}$1"
}

# Returns 0 on HTTP 2xx, 1 otherwise. Captures HTTP code in $HTTP_CODE.
api_post_raw() {
    HTTP_CODE="$(curl -s -o /tmp/setup_api_response.json -w '%{http_code}' --max-time 10 \
        -X POST \
        -H "Authorization: Bearer ${API_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "${2:-{}}" \
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
# Step 1 — Identity
# ═══════════════════════════════════════════════════════════════════════════
say "Step 1 of 7 — Identity"
info "Tell Jess your name, what to call her, and your timezone."

current_identity="$(api_get /api/config/identity 2>/dev/null || echo '{}')"
def_user="$(echo "${current_identity}" | jq -r '.user_name // ""')"
def_assistant="$(echo "${current_identity}" | jq -r '.assistant_name // "Jess"')"
def_tz="$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo 'UTC')"

prompt USER_NAME "Your name" "${def_user}"
prompt ASSISTANT_NAME "Assistant name" "${def_assistant}"
prompt TIMEZONE "Timezone" "${def_tz}"
prompt_yn ADHD_MODE "Enable ADHD mode (warmer, more proactive tone)" "Y"
prompt_choice TONE "Default tone preset" "warm balanced direct" "warm"

identity_body=$(jq -nc \
    --arg user_name "${USER_NAME}" \
    --arg assistant_name "${ASSISTANT_NAME}" \
    --arg timezone "${TIMEZONE}" \
    --argjson adhd_mode "${ADHD_MODE}" \
    --arg tone "${TONE}" \
    '{user_name: $user_name, assistant_name: $assistant_name, timezone: $timezone, adhd_mode: $adhd_mode, tone_preference: $tone}')
api_put /api/config/identity "${identity_body}" >/dev/null
ok "Identity saved."

# ═══════════════════════════════════════════════════════════════════════════
# Step 2 — Model
# ═══════════════════════════════════════════════════════════════════════════
say "Step 2 of 7 — Model"

current_model="$(grep -E '^VLLM_MODEL=' "${ENV_FILE}" 2>/dev/null | tail -1 | cut -d= -f2- || true)"

if [ -n "${current_model}" ]; then
    info "Hardware scan recommended: ${BOLD}${current_model}${NC}"
    prompt_yn USE_RECOMMENDED "Use this model" "Y"
    if [ "${USE_RECOMMENDED}" = "true" ]; then
        MODEL_ID="${current_model}"
    else
        prompt MODEL_ID "Model id (HuggingFace, AWQ or AutoRound recommended)" "${current_model}"
    fi
else
    warn "No model recommendation in .env (your GPU is below the 20 GiB tier-24 floor)."
    info "Pick a 7-8B AWQ model that fits comfortably in your VRAM. Common options:"
    info "  • Qwen/Qwen3-8B-AWQ            (8B, ~6 GB VRAM, good general quality)"
    info "  • Qwen/Qwen3-7B-Instruct-AWQ   (7B, ~5 GB VRAM, faster)"
    prompt MODEL_ID "Model id" "Qwen/Qwen3-8B-AWQ"
fi
write_env "VLLM_MODEL=${MODEL_ID}"
ok "Model saved: ${MODEL_ID}"

# ═══════════════════════════════════════════════════════════════════════════
# Step 3 — Voice
# ═══════════════════════════════════════════════════════════════════════════
say "Step 3 of 7 — Voice"

tts_url="${TTS_URL:-http://localhost:8002}"
if curl -fsS --max-time 2 "${tts_url}/health" >/dev/null 2>&1; then
    voices="$(curl -fsS --max-time 5 "${tts_url}/voices" 2>/dev/null | jq -r '.voices[]?' 2>/dev/null || echo "default")"
    info "TTS server is up. Available voices:"
    voice_list=""
    for v in ${voices}; do
        voice_list+="${v} "
    done
    prompt_choice TTS_VOICE "Pick a voice" "${voice_list}" "default"
else
    warn "TTS server not running (default install profile excludes the model layer)."
    info "Pick a voice id now; it'll be used when you enable the TTS server later."
    prompt TTS_VOICE "TTS voice id" "default"
fi
write_env "TTS_VOICE=${TTS_VOICE}"
ok "Voice saved: ${TTS_VOICE}"

# ═══════════════════════════════════════════════════════════════════════════
# Step 4 — Push channels (ntfy + Pushover)
# ═══════════════════════════════════════════════════════════════════════════
say "Step 4 of 7 — Push channels (optional)"
info "Reminders + ACK/snooze can fire to your phone via ntfy and/or Pushover."
info "Skip either by leaving values blank."

# ntfy
prompt_yn USE_NTFY "Enable ntfy push" "N"
if [ "${USE_NTFY}" = "true" ]; then
    prompt NTFY_URL "ntfy server URL" "https://ntfy.sh"
    prompt NTFY_TOPIC "ntfy topic (a long unguessable string)"
    prompt_secret NTFY_HMAC_SECRET "HMAC secret (32+ random bytes; used to sign ACK/snooze callbacks)"
    write_env "NTFY_ENABLED=true" "NTFY_URL=${NTFY_URL}" "NTFY_TOPIC=${NTFY_TOPIC}" "NTFY_HMAC_SECRET=${NTFY_HMAC_SECRET}"
    ok "ntfy configured."
fi

# Pushover
prompt_yn USE_PUSHOVER "Enable Pushover push" "N"
if [ "${USE_PUSHOVER}" = "true" ]; then
    prompt_secret PUSHOVER_USER_KEY "Pushover user key (from your Pushover dashboard)"
    prompt_secret PUSHOVER_APP_TOKEN "Pushover application token"
    write_env "PUSHOVER_ENABLED=true" "PUSHOVER_USER_KEY=${PUSHOVER_USER_KEY}" "PUSHOVER_APP_TOKEN=${PUSHOVER_APP_TOKEN}"
    ok "Pushover configured."
fi

# ═══════════════════════════════════════════════════════════════════════════
# Step 5 — Integrations (Home Assistant + Paperless-ngx)
# ═══════════════════════════════════════════════════════════════════════════
say "Step 5 of 7 — Integrations (optional)"

# Home Assistant
prompt_yn USE_HA "Enable Home Assistant integration" "N"
if [ "${USE_HA}" = "true" ]; then
    prompt HA_URL "Home Assistant URL (no trailing slash)" "http://homeassistant.local:8123"
    prompt_secret HA_TOKEN "Home Assistant long-lived access token"
    info "Testing connection…"
    validate_body=$(jq -nc --arg url "${HA_URL}" --arg token "${HA_TOKEN}" '{service:"ha", values:{HA_URL:$url, HA_TOKEN:$token}}')
    if api_post_raw /api/setup/env/validate "${validate_body}"; then
        ok "Connection works."
        write_env "HA_URL=${HA_URL}" "HA_TOKEN=${HA_TOKEN}"
    else
        warn "Validation failed:"
        cat /tmp/setup_api_response.json | jq . 2>/dev/null || cat /tmp/setup_api_response.json
        echo
        prompt_yn SAVE_ANYWAY "Save these values anyway (e.g. HA isn't up yet)" "N"
        if [ "${SAVE_ANYWAY}" = "true" ]; then
            write_env "HA_URL=${HA_URL}" "HA_TOKEN=${HA_TOKEN}"
            ok "HA values saved (unverified)."
        else
            warn "Skipping HA integration."
        fi
    fi
fi

# Paperless-ngx
prompt_yn USE_PAPERLESS "Enable Paperless-ngx integration" "N"
if [ "${USE_PAPERLESS}" = "true" ]; then
    prompt PAPERLESS_URL "Paperless-ngx URL" "http://paperless.local:8000"
    prompt_secret PAPERLESS_API_TOKEN "Paperless API token (8+ chars)"
    info "Testing connection…"
    validate_body=$(jq -nc --arg url "${PAPERLESS_URL}" --arg token "${PAPERLESS_API_TOKEN}" '{service:"paperless", values:{PAPERLESS_URL:$url, PAPERLESS_API_TOKEN:$token}}')
    if api_post_raw /api/setup/env/validate "${validate_body}"; then
        ok "Connection works."
        write_env "PAPERLESS_URL=${PAPERLESS_URL}" "PAPERLESS_API_TOKEN=${PAPERLESS_API_TOKEN}"
    else
        warn "Validation failed:"
        cat /tmp/setup_api_response.json | jq . 2>/dev/null || cat /tmp/setup_api_response.json
        echo
        prompt_yn SAVE_ANYWAY "Save these values anyway" "N"
        if [ "${SAVE_ANYWAY}" = "true" ]; then
            write_env "PAPERLESS_URL=${PAPERLESS_URL}" "PAPERLESS_API_TOKEN=${PAPERLESS_API_TOKEN}"
            ok "Paperless values saved (unverified)."
        else
            warn "Skipping Paperless integration."
        fi
    fi
fi

# ═══════════════════════════════════════════════════════════════════════════
# Step 6 — Selfcare nudges
# ═══════════════════════════════════════════════════════════════════════════
say "Step 6 of 7 — Selfcare nudges"
info "Jess can nudge you to eat / drink / take meds / move. Set per-category"
info "intervals here; you can fine-tune later from the /settings page."

selfcare_json='{"categories":{}}'
for category in medication meal hydration movement; do
    case "${category}" in
        medication) default_int=120 ; default_enabled="N" ;;
        meal)       default_int=240 ; default_enabled="Y" ;;
        hydration)  default_int=60  ; default_enabled="Y" ;;
        movement)   default_int=90  ; default_enabled="Y" ;;
    esac
    prompt_yn "ENABLE_${category^^}" "Enable ${category} nudges" "${default_enabled}"
    enabled_var="ENABLE_${category^^}"
    if [ "${!enabled_var}" = "true" ]; then
        prompt INTERVAL "  ${category} interval (minutes between nudges)" "${default_int}"
        selfcare_json=$(echo "${selfcare_json}" | jq --arg cat "${category}" --argjson interval "${INTERVAL}" \
            '.categories[$cat] = {enabled: true, interval_minutes: $interval}')
    else
        selfcare_json=$(echo "${selfcare_json}" | jq --arg cat "${category}" \
            '.categories[$cat] = {enabled: false}')
    fi
done
api_put /api/config/selfcare "${selfcare_json}" >/dev/null
ok "Selfcare schedule saved."

# ═══════════════════════════════════════════════════════════════════════════
# Step 7 — Review + complete
# ═══════════════════════════════════════════════════════════════════════════
say "Step 7 of 7 — Review"
echo
printf '%s  Identity:%s %s, assistant %s (tz %s, ADHD mode %s, tone %s)\n' \
    "${DIM}" "${NC}" "${USER_NAME:-unnamed}" "${ASSISTANT_NAME}" "${TIMEZONE}" "${ADHD_MODE}" "${TONE}"
printf '%s  Model:   %s %s\n' "${DIM}" "${NC}" "${MODEL_ID}"
printf '%s  Voice:   %s %s\n' "${DIM}" "${NC}" "${TTS_VOICE}"
printf '%s  Push:    %s ntfy=%s, pushover=%s\n' "${DIM}" "${NC}" "${USE_NTFY:-false}" "${USE_PUSHOVER:-false}"
printf '%s  HA:      %s %s\n' "${DIM}" "${NC}" "${USE_HA:-false}"
printf '%s  Paperless:%s %s\n' "${DIM}" "${NC}" "${USE_PAPERLESS:-false}"
echo
prompt_yn CONFIRM "Mark setup complete and lock the wizard" "Y"
if [ "${CONFIRM}" != "true" ]; then
    warn "Setup left in-progress. Re-run this script when you're ready to lock it."
    exit 0
fi

api_post /api/setup/complete >/dev/null
ok "Setup is complete and locked."

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
