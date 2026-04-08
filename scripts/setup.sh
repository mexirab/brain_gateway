#!/usr/bin/env bash
# Brain Gateway — Interactive Setup Wizard
# Generates .env and user_profile.yaml from user input.
# Re-runnable: detects existing .env and offers to update.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$PROJECT_DIR/.env"
PROFILE_FILE="$PROJECT_DIR/user_profile.yaml"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

echo -e "${CYAN}"
echo "╔══════════════════════════════════════╗"
echo "║     Brain Gateway Setup Wizard       ║"
echo "╚══════════════════════════════════════╝"
echo -e "${NC}"

# --- Helper functions -------------------------------------------------------

prompt() {
    local var_name="$1" prompt_text="$2" default="${3:-}"
    local value
    if [ -n "$default" ]; then
        read -r -p "$(echo -e "${GREEN}$prompt_text${NC} [$default]: ")" value
        value="${value:-$default}"
    else
        read -r -p "$(echo -e "${GREEN}$prompt_text${NC}: ")" value
    fi
    eval "$var_name='$value'"
}

prompt_secret() {
    local var_name="$1" prompt_text="$2"
    local value
    read -r -s -p "$(echo -e "${GREEN}$prompt_text${NC}: ")" value
    echo ""
    eval "$var_name='$value'"
}

test_url() {
    local url="$1" label="$2"
    if [ -z "$url" ]; then
        echo -e "  ${YELLOW}Skipped${NC} — $label not configured"
        return 1
    fi
    # Try /health first, then root
    local health_url="${url%/v1}/health"
    if curl -sf --max-time 5 "$health_url" >/dev/null 2>&1; then
        echo -e "  ${GREEN}✓${NC} $label reachable at $url"
        return 0
    elif curl -sf --max-time 5 "$url" >/dev/null 2>&1; then
        echo -e "  ${GREEN}✓${NC} $label reachable at $url"
        return 0
    else
        echo -e "  ${RED}✗${NC} $label unreachable at $url"
        return 1
    fi
}

generate_token() {
    python3 -c "import secrets; print(secrets.token_urlsafe(32))" 2>/dev/null || \
    openssl rand -base64 32 2>/dev/null || \
    head -c 32 /dev/urandom | base64
}

# --- Check for existing config ----------------------------------------------

if [ -f "$ENV_FILE" ]; then
    echo -e "${YELLOW}Existing .env found.${NC}"
    read -r -p "Overwrite? (y/N): " overwrite
    if [[ ! "$overwrite" =~ ^[Yy] ]]; then
        echo "Keeping existing .env. Run with a backup if you want to start fresh."
        exit 0
    fi
fi

# --- Core Services -----------------------------------------------------------

echo ""
echo -e "${CYAN}=== Core Services ===${NC}"
echo ""

prompt MODEL_URL "LLM endpoint URL" "http://localhost:8080/v1"
test_url "$MODEL_URL" "Primary LLM" || true

prompt MODEL_NAME "Model name (for logging)" ""

prompt FALLBACK_MODEL_URL "Fallback LLM URL (empty to skip)" ""
if [ -n "$FALLBACK_MODEL_URL" ]; then
    test_url "$FALLBACK_MODEL_URL" "Fallback LLM" || true
    prompt FALLBACK_MODEL_NAME "Fallback model name" ""
else
    FALLBACK_MODEL_NAME=""
fi

prompt TTS_URL "TTS endpoint URL (empty to skip)" ""
if [ -n "$TTS_URL" ]; then
    test_url "$TTS_URL" "TTS" || true
    prompt TTS_VOICE "TTS voice name" "jessica"
else
    TTS_VOICE="jessica"
fi

prompt STT_URL "STT endpoint URL (empty to skip)" ""
if [ -n "$STT_URL" ]; then
    test_url "$STT_URL" "STT" || true
fi

# --- Home Assistant -----------------------------------------------------------

echo ""
echo -e "${CYAN}=== Home Assistant (optional) ===${NC}"
echo ""

prompt HA_URL "Home Assistant URL (empty to skip)" ""
HA_TOKEN=""
REMINDER_SPEAKER=""
FALLBACK_SPEAKER=""
MORNING_BRIEFING_SPEAKER=""
FOCUS_AUDIO_PLAYER=""
PRESENCE_ENTITY=""

if [ -n "$HA_URL" ]; then
    prompt_secret HA_TOKEN "HA Long-Lived Access Token"
    if test_url "$HA_URL/api/" "Home Assistant"; then
        echo -e "  ${GREEN}Tip:${NC} Configure speakers in user_profile.yaml after setup"
    fi
    prompt REMINDER_SPEAKER "Default reminder speaker entity (empty to skip)" ""
    prompt MORNING_BRIEFING_SPEAKER "Morning briefing speaker (empty to skip)" ""
    prompt PRESENCE_ENTITY "Presence entity (e.g., person.yourname, empty to skip)" ""
fi

# --- Google Integration -------------------------------------------------------

echo ""
echo -e "${CYAN}=== Google Integration (optional) ===${NC}"
echo ""

GOOGLE_CREDENTIALS_PATH="/app/credentials/google_credentials.json"
CALENDAR_ENABLED="false"
if [ -f "$PROJECT_DIR/credentials/google_credentials.json" ]; then
    echo -e "  ${GREEN}✓${NC} Google credentials found"
    CALENDAR_ENABLED="true"
else
    echo -e "  ${YELLOW}No Google credentials found.${NC} Calendar and email features disabled."
    echo "  Place google_credentials.json in $PROJECT_DIR/credentials/ to enable."
fi

prompt GOOGLE_MAPS_API_KEY "Google Maps API key (empty to skip)" ""

# --- Vision Model -------------------------------------------------------------

echo ""
echo -e "${CYAN}=== Vision Model (optional) ===${NC}"
echo ""

prompt VISION_MODEL_URL "Vision model URL (empty to skip)" ""
VISION_ENABLED="false"
VISION_MODEL_NAME=""
if [ -n "$VISION_MODEL_URL" ]; then
    VISION_ENABLED="true"
    test_url "$VISION_MODEL_URL" "Vision model" || true
    prompt VISION_MODEL_NAME "Vision model name" ""
fi

# --- Pi-hole ------------------------------------------------------------------

echo ""
echo -e "${CYAN}=== Pi-hole Focus Blocking (optional) ===${NC}"
echo ""

prompt PIHOLE_URLS "Pi-hole URL(s), comma-separated (empty to skip)" ""
PIHOLE_PASSWORD=""
FOCUS_BLOCKING_ENABLED="false"
if [ -n "$PIHOLE_URLS" ]; then
    FOCUS_BLOCKING_ENABLED="true"
    prompt_secret PIHOLE_PASSWORD "Pi-hole password"
fi

# --- Authentication -----------------------------------------------------------

echo ""
echo -e "${CYAN}=== Authentication ===${NC}"
echo ""

API_TOKEN=$(generate_token)
echo -e "  Generated API token: ${YELLOW}${API_TOKEN:0:8}...${NC}"
DASHBOARD_TOKEN=$(generate_token)
echo -e "  Generated dashboard token: ${YELLOW}${DASHBOARD_TOKEN:0:8}...${NC}"

# --- User Profile -------------------------------------------------------------

echo ""
echo -e "${CYAN}=== User Profile ===${NC}"
echo ""

prompt USER_NAME "Your first name" ""
prompt TZ "Timezone" "America/Chicago"
prompt HOME_ADDRESS "Home address (for weather/travel, empty to skip)" ""

# --- Write .env ---------------------------------------------------------------

echo ""
echo -e "${CYAN}Writing .env...${NC}"

cat > "$ENV_FILE" << ENVEOF
# Brain Gateway Configuration
# Generated by scripts/setup.sh on $(date -Iseconds)
# Re-run scripts/setup.sh to regenerate

# --- Authentication ---
API_TOKEN=$API_TOKEN
DASHBOARD_TOKEN=$DASHBOARD_TOKEN

# --- Primary LLM ---
MODEL_URL=$MODEL_URL
MODEL_NAME=$MODEL_NAME

# --- Fallback LLM (empty = disabled) ---
FALLBACK_MODEL_URL=$FALLBACK_MODEL_URL
FALLBACK_MODEL_NAME=$FALLBACK_MODEL_NAME

# --- TTS/STT (empty = disabled) ---
TTS_URL=$TTS_URL
TTS_VOICE=$TTS_VOICE
STT_URL=$STT_URL

# --- Home Assistant (empty = disabled) ---
HA_URL=$HA_URL
HA_TOKEN=$HA_TOKEN

# --- Speakers (empty = skip TTS announcements) ---
REMINDER_SPEAKER=$REMINDER_SPEAKER
FALLBACK_SPEAKER=$FALLBACK_SPEAKER
MORNING_BRIEFING_SPEAKER=$MORNING_BRIEFING_SPEAKER
FOCUS_AUDIO_PLAYER=$FOCUS_AUDIO_PLAYER

# --- Presence (empty = disabled) ---
PRESENCE_ENABLED=$([ -n "$PRESENCE_ENTITY" ] && echo "true" || echo "false")
PRESENCE_ENTITY=$PRESENCE_ENTITY

# --- Google (credentials in ./credentials/) ---
GOOGLE_CREDENTIALS_PATH=$GOOGLE_CREDENTIALS_PATH
GOOGLE_TOKEN_PATH=/app/credentials/google_token.json
GOOGLE_MAPS_API_KEY=$GOOGLE_MAPS_API_KEY
MORNING_BRIEFING_ENABLED=$CALENDAR_ENABLED

# --- Vision (empty = disabled) ---
VISION_ENABLED=$VISION_ENABLED
VISION_MODEL_URL=$VISION_MODEL_URL
VISION_MODEL_NAME=$VISION_MODEL_NAME

# --- Pi-hole Focus Blocking (empty = disabled) ---
PIHOLE_URLS=$PIHOLE_URLS
PIHOLE_PASSWORD=$PIHOLE_PASSWORD
FOCUS_BLOCKING_ENABLED=$FOCUS_BLOCKING_ENABLED

# --- Paths ---
GATEWAY_ROOT_PATH=$PROJECT_DIR
CHROMA_COLLECTION=personal_rag

# --- Timezone ---
TZ=$TZ
HOME_ADDRESS=$HOME_ADDRESS

# --- CORS ---
CORS_ORIGINS=http://localhost:3001

# --- YNAB (empty = disabled) ---
YNAB_ACCESS_TOKEN=
YNAB_BUDGET_ID=
ENVEOF

echo -e "  ${GREEN}✓${NC} .env written to $ENV_FILE"

# --- Write user_profile.yaml -------------------------------------------------

echo -e "${CYAN}Writing user_profile.yaml...${NC}"

cat > "$PROFILE_FILE" << PROFILEEOF
# Brain Gateway User Profile
# Generated by scripts/setup.sh on $(date -Iseconds)

name: "$USER_NAME"
timezone: "$TZ"
home_address: "$HOME_ADDRESS"

# Assistant voice (used for TTS)
assistant_voice: "$TTS_VOICE"

# Home Assistant speakers (fill in your entity IDs)
speakers:
  default: "$REMINDER_SPEAKER"
  morning_briefing: "$MORNING_BRIEFING_SPEAKER"
  focus_audio: "$FOCUS_AUDIO_PLAYER"
  aliases: {}
    # office: "media_player.office_speaker"
    # bedroom: "media_player.bedroom_speaker"

# Mobile notifications (fill in your HA notify service)
mobile_notify_services: []
  # - notify.mobile_app_your_phone

# Temperature sensors (fill in your HA sensor entity IDs)
closet_temp_sensor: ""
ambient_temp_sensor: ""
temp_warning: 85
temp_critical: 95
PROFILEEOF

echo -e "  ${GREEN}✓${NC} user_profile.yaml written to $PROFILE_FILE"

# --- Summary ------------------------------------------------------------------

echo ""
echo -e "${CYAN}╔══════════════════════════════════════╗"
echo "║          Setup Complete!             ║"
echo -e "╚══════════════════════════════════════╝${NC}"
echo ""
echo "Next steps:"
echo "  1. Review .env and user_profile.yaml"
echo "  2. docker compose up -d"
echo "  3. curl http://localhost:8888/health"
echo ""
if [ "$CALENDAR_ENABLED" = "false" ]; then
    echo -e "  ${YELLOW}Note:${NC} Calendar/email disabled. Add Google credentials to enable."
fi
echo ""
