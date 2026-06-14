#!/usr/bin/env bash
# Brain Gateway — Bring-Your-Own-Model setup (no GPU, no Ubuntu required).
#
# Stands the assistant up on a Mac / Windows / Linux box with Docker Desktop,
# pointing at a model YOU provide: a local Ollama / LM Studio, another box on
# your LAN, or a cloud API. Brings up the CPU services only (orchestrator +
# dashboard + redis + searxng) — NOT the bundled GPU model layer.
#
# This is the non-NVIDIA sibling of install.sh. Full guide: docs/BYO_MODEL.md.
#
# Usage:  bash scripts/byo-setup.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"
ENV_EXAMPLE="${REPO_ROOT}/.env.example"

if [ -t 1 ]; then
    GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; CYAN=$'\033[0;36m'; BOLD=$'\033[1m'; NC=$'\033[0m'
else
    GREEN=""; YELLOW=""; CYAN=""; BOLD=""; NC=""
fi
say()  { printf '%s==>%s %s\n' "${CYAN}" "${NC}" "$*"; }
ok()   { printf '%s✓%s %s\n'   "${GREEN}" "${NC}" "$*"; }
warn() { printf '%s!%s %s\n'   "${YELLOW}" "${NC}" "$*"; }
die()  { printf '✗ %s\n' "$*" >&2; exit 1; }

echo
echo "${BOLD}Brain Gateway — Bring-Your-Own-Model setup${NC}"
echo

# ── Prereqs ─────────────────────────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || die "Docker not found. Install Docker Desktop: https://www.docker.com/products/docker-desktop/"
docker compose version >/dev/null 2>&1 || die "docker compose v2 not found. Update Docker Desktop."
docker info >/dev/null 2>&1 || die "Docker daemon not running. Start Docker Desktop and re-run."
command -v python3 >/dev/null 2>&1 || die "python3 is required (used to generate login tokens)."
ok "Docker + python3 present."

cd "${REPO_ROOT}"

# ── .env ────────────────────────────────────────────────────────────────────
if [ ! -f "${ENV_FILE}" ]; then
    cp "${ENV_EXAMPLE}" "${ENV_FILE}"
    ok "Created .env from .env.example"
else
    ok ".env exists — updating model + token settings only (other values untouched)"
fi

# Replace a key's line (delete any existing, append fresh). macOS + GNU safe.
set_env() {
    local key="$1" val="$2"
    sed -i.bak "/^${key}=/d" "${ENV_FILE}" && rm -f "${ENV_FILE}.bak"
    printf '%s=%s\n' "${key}" "${val}" >> "${ENV_FILE}"
}

# ── Tokens (generate only if not already real) ──────────────────────────────
if ! grep -qE '^API_TOKEN=[A-Za-z0-9_-]{20,}$' "${ENV_FILE}"; then
    set_env API_TOKEN "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    ok "Generated API_TOKEN"
fi
if grep -qE '^DASHBOARD_TOKEN=[A-Za-z0-9_-]{20,}$' "${ENV_FILE}"; then
    DASH_TOKEN="$(grep -E '^DASHBOARD_TOKEN=' "${ENV_FILE}" | tail -1 | cut -d= -f2-)"
else
    DASH_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
    set_env DASHBOARD_TOKEN "${DASH_TOKEN}"
    ok "Generated DASHBOARD_TOKEN"
fi

set_env GATEWAY_ROOT_PATH "${REPO_ROOT}"
set_env JESS_LAN_IP "localhost"
set_env COMPOSE_PROFILES ""   # CPU-only: do NOT start the GPU model layer

# ── Choose the model backend ────────────────────────────────────────────────
say "${BOLD}Where is your model?${NC}"
echo "  1) Local — Ollama / LM Studio on THIS machine   (recommended, fully private)"
echo "  2) Local — a model server on another box (LAN)"
echo "  3) Cloud — Anthropic (Claude)                    (opt-in; your own API key)"
echo "  4) Cloud — OpenAI                                (opt-in; your own API key)"
printf '  Choose [1-4] (default 1): '
read -r choice || choice=""
choice="${choice:-1}"

case "${choice}" in
    1)
        set_env MODEL_BACKEND "openai_compatible"
        printf '  Local server port (Ollama=11434, LM Studio=1234) [11434]: '
        read -r port || port=""; port="${port:-11434}"
        set_env MODEL_URL "http://host.docker.internal:${port}/v1"
        printf '  Model id to request (e.g. qwen2.5:14b): '
        read -r m || m=""; set_env MODEL_NAME "${m}"
        set_env MODEL_API_KEY ""
        warn "Make sure the server is running and the model is pulled (e.g. 'ollama pull ${m:-qwen2.5:14b}')."
        if [ "${port}" = "11434" ]; then
            warn "Ollama's default context (4096) truncates Jess's tool-heavy prompt and"
            warn "silently breaks tool calls. Serve it with a bigger window:"
            warn "    OLLAMA_CONTEXT_LENGTH=16384 ollama serve   # see docs/BYO_MODEL.md"
        fi
        ;;
    2)
        set_env MODEL_BACKEND "openai_compatible"
        printf '  Model URL (e.g. http://10.0.0.50:11434/v1): '
        read -r u || u=""; set_env MODEL_URL "${u}"
        printf '  Model id to request: '
        read -r m || m=""; set_env MODEL_NAME "${m}"
        set_env MODEL_API_KEY ""
        ;;
    3)
        set_env MODEL_BACKEND "anthropic"
        set_env MODEL_URL "https://api.anthropic.com"
        printf '  Model id [claude-haiku-4-5]: '
        read -r m || m=""; set_env MODEL_NAME "${m:-claude-haiku-4-5}"
        printf '  Anthropic API key (sk-ant-...): '
        read -r k || k=""; set_env MODEL_API_KEY "${k}"
        warn "Cloud mode: conversation turns are sent to Anthropic under YOUR API account."
        warn "Stored memory / RAG / reminders stay on this box. See docs/BYO_MODEL.md → Privacy."
        ;;
    4)
        set_env MODEL_BACKEND "openai"
        set_env MODEL_URL "https://api.openai.com/v1"
        printf '  Model id [gpt-4o-mini]: '
        read -r m || m=""; set_env MODEL_NAME "${m:-gpt-4o-mini}"
        printf '  OpenAI API key (sk-...): '
        read -r k || k=""; set_env MODEL_API_KEY "${k}"
        warn "Cloud mode: conversation turns are sent to OpenAI under YOUR API account."
        warn "Stored memory / RAG / reminders stay on this box. See docs/BYO_MODEL.md → Privacy."
        ;;
    *) die "Invalid choice '${choice}'." ;;
esac
ok "Model backend configured."

# ── Bring up the CPU services ───────────────────────────────────────────────
say "Building + starting CPU services (orchestrator + dashboard + redis + searxng)..."
say "First run builds the images — a few minutes."
docker compose up -d orchestrator frontend redis searxng

# ── Health wait (orchestrator only) ─────────────────────────────────────────
say "Waiting for the orchestrator to report healthy (up to 90s)..."
healthy="no"
for _ in $(seq 1 18); do
    if curl -s --max-time 2 http://localhost:8888/health >/dev/null 2>&1; then
        healthy="yes"; break
    fi
    sleep 5
done

echo
if [ "${healthy}" = "yes" ]; then
    ok "${BOLD}Orchestrator is healthy.${NC}"
else
    warn "Orchestrator didn't report healthy yet — it may still be starting."
    warn "Check: docker compose logs -f orchestrator"
fi
echo
say "Dashboard:   ${BOLD}http://localhost:3001/${NC}"
say "Login token: ${BOLD}${DASH_TOKEN}${NC}   (saved as DASHBOARD_TOKEN in .env)"
say "Health:      curl -s http://localhost:8888/health"
echo
warn "Voice (TTS/STT) and Home-Assistant voice pucks are OFF in BYO mode — they need the"
warn "GPU model layer or a separate TTS server. Text chat, reminders, calendar, brain-dump,"
warn "and RAG memory all work. Full guide: docs/BYO_MODEL.md"
