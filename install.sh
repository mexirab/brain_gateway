#!/usr/bin/env bash
# Brain Gateway installer
#
# Runs in two stages, separated by a reboot:
#   Stage 1 — Install Docker + NVIDIA driver + container toolkit, then reboot.
#   Stage 2 — Verify drivers, write .env, run hardware scan, bring up stack,
#             print the wizard URL.
#
# Idempotent: safe to re-run at any time. Stage is tracked via a marker file
# at /var/lib/brain-gateway-install/stage.
#
# Usage:  bash install.sh
set -euo pipefail

# ── Constants ───────────────────────────────────────────────────────────────
MARKER_DIR=/var/lib/brain-gateway-install
MARKER="${MARKER_DIR}/stage"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# This script ships at the repo root; SCRIPT_DIR == REPO_ROOT. Prefer git
# in case a user runs the script from inside a subdirectory.
REPO_ROOT="$(git -C "${SCRIPT_DIR}" rev-parse --show-toplevel 2>/dev/null || echo "${SCRIPT_DIR}")"
ENV_FILE="${REPO_ROOT}/.env"
ENV_EXAMPLE="${REPO_ROOT}/.env.example"

# ── Colors (TTY only) ───────────────────────────────────────────────────────
if [ -t 1 ]; then
    RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'
    CYAN=$'\033[0;36m'; BOLD=$'\033[1m'; DIM=$'\033[2m'; NC=$'\033[0m'
else
    RED=""; GREEN=""; YELLOW=""; CYAN=""; BOLD=""; DIM=""; NC=""
fi

# ── Helpers ─────────────────────────────────────────────────────────────────
say()  { printf '%s==>%s %s\n' "${CYAN}" "${NC}" "$*"; }
ok()   { printf '%s✓%s %s\n'   "${GREEN}" "${NC}" "$*"; }
warn() { printf '%s!%s %s\n'   "${YELLOW}" "${NC}" "$*"; }
info() { printf '%s  %s%s\n'   "${DIM}"   "$*"  "${NC}"; }
die()  { printf '%s✗%s %s\n'   "${RED}"   "${NC}" "$*" >&2; exit 1; }

confirm() {
    local prompt="${1:-Press Enter to continue, or Ctrl-C to abort}"
    if [ ! -t 0 ]; then
        warn "Non-interactive stdin; skipping confirmation."
        return 0
    fi
    read -r -p "${prompt}: " _ || die "Aborted"
}

check_os() {
    if [ ! -r /etc/os-release ]; then
        die "Cannot read /etc/os-release — not a Debian/Ubuntu system?"
    fi
    if ! grep -qE 'UBUNTU_CODENAME=(noble|jammy)' /etc/os-release; then
        die "Only Ubuntu 22.04 (jammy) and 24.04 (noble) are supported. Got: $(grep PRETTY /etc/os-release | cut -d= -f2-)"
    fi
    ok "OS: $(grep PRETTY_NAME /etc/os-release | cut -d= -f2- | tr -d '\"')"
}

check_arch() {
    local arch
    arch="$(uname -m)"
    if [ "${arch}" != "x86_64" ]; then
        die "Only x86_64 is supported (got: ${arch})"
    fi
}

check_gpu() {
    if ! command -v lspci >/dev/null 2>&1; then
        sudo apt-get install -y -qq pciutils
    fi
    # Capture lspci output first to avoid SIGPIPE under `set -o pipefail`:
    # `grep -q` closes stdin after the first match, lspci then gets SIGPIPE
    # (exit 141), pipefail propagates that as a failed pipeline, and the
    # NVIDIA check spuriously dies. Run lspci, grep into a variable, then test.
    local nvidia_devices
    nvidia_devices="$(lspci 2>/dev/null | grep -i nvidia || true)"
    if [ -z "${nvidia_devices}" ]; then
        die "No NVIDIA GPU detected via lspci. This installer only supports NVIDIA GPUs."
    fi
    ok "NVIDIA GPU(s) detected:"
    printf '%s\n' "${nvidia_devices}" | sed 's/^/    /'
}

require_sudo() {
    if ! command -v sudo >/dev/null 2>&1; then
        die "sudo is required but not installed"
    fi
    if ! sudo -n true 2>/dev/null; then
        warn "Sudo password required. You'll be prompted."
        sudo -v || die "Sudo authentication failed"
    fi
}

# ── Stage tracking ──────────────────────────────────────────────────────────
detect_stage() {
    if [ ! -f "${MARKER}" ]; then
        echo "1"
    else
        local s
        s="$(cat "${MARKER}" 2>/dev/null | head -1 | tr -d '[:space:]')"
        case "${s}" in
            post-reboot) echo "2" ;;
            complete)    echo "3" ;;
            "")          echo "1" ;;
            *)           echo "0" ;;
        esac
    fi
}

set_marker() {
    sudo mkdir -p "${MARKER_DIR}"
    echo "$1" | sudo tee "${MARKER}" >/dev/null
}

# ── Stage 1: install system deps ────────────────────────────────────────────
stage_1() {
    say "${BOLD}Stage 1 of 2 — installing system dependencies${NC}"
    echo
    say "About to install:"
    echo "    - Docker engine + docker-compose-v2 (apt: docker.io, docker-compose-v2)"
    echo "    - NVIDIA driver 580 (DKMS variant; rebuilds against your current kernel)"
    echo "    - NVIDIA container toolkit (lets containers see the GPU)"
    echo
    echo "    After the reboot, Stage 2 will bring up the orchestrator + the"
    echo "    full local-AI stack (LLM + TTS + STT) and hand off to a 30-second"
    echo "    setup wizard."
    echo
    echo "    Plan on ~30 minutes total: ~5 min for apt installs (this stage),"
    echo "    ~5 min for the reboot, then ~15-20 min for container images +"
    echo "    model weights to download in Stage 2."
    echo
    warn "A reboot is required midway so the new NVIDIA kernel module loads."
    warn "The install resumes automatically on your next SSH login (5s Ctrl-C escape)."
    echo
    confirm

    check_arch
    check_os
    require_sudo
    check_gpu

    say "Updating apt cache..."
    sudo apt-get update -qq

    say "Installing Docker + docker-compose-v2..."
    sudo apt-get install -y -qq docker.io docker-compose-v2 curl gnupg

    say "Adding NVIDIA container toolkit apt repo..."
    if [ ! -f /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg ]; then
        curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
            | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    fi
    curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
        | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
        | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
    sudo apt-get update -qq

    say "Installing NVIDIA driver + container toolkit (this takes a few minutes)..."
    sudo apt-get install -y -qq nvidia-driver-580-open nvidia-container-toolkit

    say "Configuring NVIDIA container runtime for Docker..."
    sudo nvidia-ctk runtime configure --runtime=docker

    say "Adding ${USER} to the docker group..."
    sudo usermod -aG docker "${USER}"

    set_marker "post-reboot"
    install_resume_hook

    echo
    ok "Stage 1 complete."
    echo
    say "${BOLD}REBOOT REQUIRED${NC}"
    echo "    On your next interactive SSH login, install Stage 2 will resume"
    echo "    automatically (with a 5-second Ctrl-C escape hatch)."
    echo "    Just: ssh labadmin@<this-box>  ← and wait."
    echo
    confirm "Press Enter to reboot now (or Ctrl-C to reboot manually later)"

    sudo reboot
}

# ── Auto-resume hook (Stage 1 ↔ Stage 2 bridge across the reboot) ──────────
HOOK_FILE="${HOME}/.brain-gateway-resume.sh"

install_resume_hook() {
    # Write a script that, on next interactive login, checks the marker and
    # auto-runs Stage 2. Removed by Stage 2 on success.
    cat > "${HOOK_FILE}" <<EOF
#!/usr/bin/env bash
# Brain Gateway — auto-resume install Stage 2 after reboot.
# Created by install.sh Stage 1; removed when Stage 2 completes.
if [ -f /var/lib/brain-gateway-install/stage ] \
   && [ "\$(cat /var/lib/brain-gateway-install/stage 2>/dev/null)" = "post-reboot" ] \
   && [ -f "${REPO_ROOT}/install.sh" ]; then
    echo ""
    echo "(brain-gateway: resuming install Stage 2 in 5s — Ctrl-C to skip)"
    sleep 5 || return 0
    cd "${REPO_ROOT}" && bash install.sh
fi
EOF
    chmod 600 "${HOOK_FILE}"
    # Idempotently source it from ~/.bash_profile on every interactive login
    local profile="${HOME}/.bash_profile"
    touch "${profile}"
    if ! grep -q "brain-gateway-resume.sh" "${profile}" 2>/dev/null; then
        printf '\n# brain-gateway install bridge (no-op once the file is removed)\n[ -f ~/.brain-gateway-resume.sh ] && source ~/.brain-gateway-resume.sh\n' >> "${profile}"
    fi
}

remove_resume_hook() {
    # Just remove the file — the source line in ~/.bash_profile becomes a no-op.
    rm -f "${HOOK_FILE}"
}

# ── Stage 2: post-reboot app setup ──────────────────────────────────────────
stage_2() {
    say "${BOLD}Stage 2 of 2 — post-reboot app setup${NC}"
    echo

    say "Verifying NVIDIA driver loaded..."
    if ! command -v nvidia-smi >/dev/null 2>&1; then
        die "nvidia-smi is missing. The driver install in Stage 1 didn't complete. Try: sudo apt install -y nvidia-driver-580-open"
    fi
    if ! nvidia-smi >/dev/null 2>&1; then
        die "nvidia-smi failed. The kernel module didn't load. Check: dmesg | grep -i nvidia"
    fi
    local gpu_name
    gpu_name="$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
    ok "Driver loaded; first GPU: ${gpu_name}"

    say "Verifying Docker daemon..."
    if ! sudo systemctl is-active --quiet docker; then
        sudo systemctl start docker
    fi
    if ! docker info >/dev/null 2>&1; then
        die "Docker daemon not responding. Try: sudo systemctl status docker"
    fi
    ok "Docker daemon is running"

    say "Smoke-testing Docker + GPU integration (pulls ~400 MB CUDA base image)..."
    if ! docker run --rm --gpus all nvidia/cuda:12.8.1-base-ubuntu24.04 nvidia-smi >/dev/null 2>&1; then
        die "Docker can't see the GPU. Check 'sudo nvidia-ctk runtime configure --runtime=docker' completed cleanly and /etc/docker/daemon.json has the nvidia runtime configured."
    fi
    ok "Docker can see the GPU"

    say "Preparing .env..."
    if [ ! -f "${ENV_EXAMPLE}" ]; then
        die ".env.example not found at ${ENV_EXAMPLE} — are you running this from inside the repo?"
    fi
    if [ ! -f "${ENV_FILE}" ]; then
        cp "${ENV_EXAMPLE}" "${ENV_FILE}"
        ok "Created .env from .env.example"
    else
        ok ".env already exists; leaving it alone"
    fi

    say "Setting API_TOKEN..."
    if grep -qE '^API_TOKEN=[A-Za-z0-9_-]{20,}$' "${ENV_FILE}"; then
        ok "API_TOKEN already set (looks like a real token); leaving it alone"
    else
        local token
        token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
        # Remove any existing API_TOKEN line (handles the placeholder) + append fresh
        sed -i.bak '/^API_TOKEN=/d' "${ENV_FILE}" && rm -f "${ENV_FILE}.bak"
        echo "API_TOKEN=${token}" >> "${ENV_FILE}"
        ok "Generated and wrote a fresh API_TOKEN"
    fi

    # JESS_LAN_IP — used by the first-chat welcome to render a clickable
    # /settings URL. Has to be set host-side because the orchestrator
    # container can't reliably enumerate the host's LAN IP from inside Docker.
    # Prefer `ip -4 route get` over `hostname -I | awk '{print $1}'` — the
    # latter can return docker0 (172.17.0.1) or a Tailscale 100.x address
    # first depending on interface-up order, which renders a URL nobody on
    # the LAN can reach. `ip route get` always returns the source IP of the
    # default-route interface, which is the address LAN clients send to.
    local lan_ip_now
    lan_ip_now="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
    if [ -z "${lan_ip_now}" ]; then
        # Fallback to hostname -I if `ip route get` fails (no default route, etc.)
        lan_ip_now="$(hostname -I 2>/dev/null | awk '{print $1}')"
    fi
    if [ -n "${lan_ip_now}" ]; then
        sed -i.bak '/^JESS_LAN_IP=/d' "${ENV_FILE}" && rm -f "${ENV_FILE}.bak"
        echo "JESS_LAN_IP=${lan_ip_now}" >> "${ENV_FILE}"
        ok "Detected LAN IP: ${lan_ip_now} (saved as JESS_LAN_IP)"
    else
        warn "Could not detect LAN IP — the welcome message will show '<your-box-ip>' as a placeholder"
    fi

    say "Running hardware scan + appending recommendation to .env..."
    if [ -x "${REPO_ROOT}/scripts/detect_hardware.sh" ]; then
        bash "${REPO_ROOT}/scripts/detect_hardware.sh" >> "${ENV_FILE}" || warn "Hardware scan exited non-zero (continuing)"
        # Below-floor GPUs (<20 GiB) leave VLLM_MODEL commented out — substitute
        # a sane 7-8B AWQ default so the install brings up a working brain.
        if grep -qE '^# VLLM_MODEL=' "${ENV_FILE}" && ! grep -qE '^VLLM_MODEL=' "${ENV_FILE}"; then
            echo "VLLM_MODEL=Qwen/Qwen3-8B-AWQ  # auto-picked for sub-tier-24 GPU" >> "${ENV_FILE}"
            warn "GPU is below the 20 GiB tier-24 floor; auto-picked Qwen/Qwen3-8B-AWQ."
            warn "Change later by editing ${ENV_FILE} and running 'docker compose up -d'."
        fi
        ok "Hardware scan complete"
    else
        warn "scripts/detect_hardware.sh not found or not executable; skipping"
    fi

    say "Enabling the models profile (LLM + TTS + STT will run as containers)..."
    # Current COMPOSE_PROFILES value, empty if line absent or right-hand-side blank.
    local cur_profiles
    cur_profiles="$(grep -E '^COMPOSE_PROFILES=' "${ENV_FILE}" 2>/dev/null | tail -1 | cut -d= -f2- | sed 's/  *#.*//' | tr -d '[:space:]')"
    if [ -z "${cur_profiles}" ]; then
        # Unset OR empty (the .env.example default is `COMPOSE_PROFILES=`).
        # Strip any existing blank line first so we don't end up with two.
        sed -i.bak '/^COMPOSE_PROFILES=/d' "${ENV_FILE}" && rm -f "${ENV_FILE}.bak"
        echo "COMPOSE_PROFILES=models" >> "${ENV_FILE}"
        ok "COMPOSE_PROFILES=models written to .env"
    elif ! echo "${cur_profiles}" | grep -q "models"; then
        warn "COMPOSE_PROFILES is set to '${cur_profiles}' and does not include 'models'."
        warn "The LLM/TTS/STT containers will NOT start. Edit ${ENV_FILE} to add it."
    else
        ok "COMPOSE_PROFILES already includes 'models' (current: '${cur_profiles}')"
    fi

    say "Bringing up the full stack (first run pulls images + model weights; ~15-25 min)..."
    info "Container images: ~30 GB. Model weights: ~40-50 GB. Both pulled once, cached after."
    cd "${REPO_ROOT}"
    docker compose up -d

    # Health-wait — orchestrator + the three model containers if the profile is on.
    local services=( orchestrator )
    if grep -qE '^COMPOSE_PROFILES=.*models' "${ENV_FILE}"; then
        services+=( vllm-primary qwen-tts parakeet-stt )
    fi

    say "Waiting up to 15 min for ${#services[@]} service(s) to report healthy: ${services[*]}"
    info "(vLLM cold-start is the slowest — it loads ~6-20 GB of weights into VRAM)"

    local timeout_seconds=900   # 15 min
    local elapsed=0
    local poll_interval=10
    local pending=( "${services[@]}" )
    local last_report=""

    while [ ${#pending[@]} -gt 0 ] && [ "${elapsed}" -lt "${timeout_seconds}" ]; do
        local still_pending=()
        for svc in "${pending[@]}"; do
            local healthy="no"
            case "${svc}" in
                orchestrator)
                    curl -s --max-time 2 http://localhost:8888/health >/dev/null 2>&1 && healthy="yes" ;;
                vllm-primary)
                    curl -s --max-time 2 http://localhost:8080/health >/dev/null 2>&1 && healthy="yes" ;;
                qwen-tts)
                    curl -s --max-time 2 http://localhost:8002/health >/dev/null 2>&1 && healthy="yes" ;;
                parakeet-stt)
                    curl -s --max-time 2 http://localhost:8003/health >/dev/null 2>&1 && healthy="yes" ;;
            esac
            if [ "${healthy}" = "yes" ]; then
                ok "${svc} is healthy (after ${elapsed}s)"
            else
                still_pending+=( "${svc}" )
            fi
        done
        pending=( "${still_pending[@]}" )

        # Periodic status (every ~30s) so the user sees we're alive
        if [ ${#pending[@]} -gt 0 ] && [ "$((elapsed % 30))" -eq 0 ] && [ "${elapsed}" -gt 0 ]; then
            local report="${elapsed}s — still waiting on: ${pending[*]}"
            if [ "${report}" != "${last_report}" ]; then
                info "${report}"
                last_report="${report}"
            fi
        fi

        if [ ${#pending[@]} -gt 0 ]; then
            sleep "${poll_interval}"
            elapsed=$((elapsed + poll_interval))
        fi
    done

    if [ ${#pending[@]} -eq 0 ]; then
        ok "All services healthy."
    else
        warn "Timed out after ${timeout_seconds}s. Still not healthy: ${pending[*]}"
        warn "Check 'docker compose logs ${pending[0]}' for details."
        warn "The install will continue — the setup wizard may still work once the stragglers finish loading."
    fi

    set_marker "complete"
    remove_resume_hook

    echo
    ok "${BOLD}Stage 2 complete — handing off to the setup wizard.${NC}"
    echo

    if [ -x "${REPO_ROOT}/scripts/setup.sh" ]; then
        bash "${REPO_ROOT}/scripts/setup.sh"
    else
        warn "scripts/setup.sh not found; run it manually:"
        info "    cd ${REPO_ROOT} && bash scripts/setup.sh"
    fi
}

# ── Stage 3: already installed ──────────────────────────────────────────────
stage_3() {
    local lan_ip
    lan_ip="$(hostname -I | awk '{print $1}')"

    ok "${BOLD}Brain Gateway is already installed on this box.${NC}"
    echo
    say "Dashboard:        http://${lan_ip}:3001/"
    say "Settings page:    http://${lan_ip}:3001/settings"
    say "Health check:     curl -s http://localhost:8888/health"
    say "Re-run setup CLI: bash ${REPO_ROOT}/scripts/setup.sh"
    echo
    say "If you want to re-run Stage 2 (e.g. after a manual wipe), reset the marker:"
    echo "    sudo rm ${MARKER}"
    say "If you want to re-run from Stage 1 (system deps), remove the directory:"
    echo "    sudo rm -rf ${MARKER_DIR}"
    echo
}

# ── Main ────────────────────────────────────────────────────────────────────
echo
echo "${BOLD}Brain Gateway Installer${NC}"
echo

stage="$(detect_stage)"
case "${stage}" in
    0) die "Unknown marker state in ${MARKER}: $(cat "${MARKER}"). Remove it to start fresh." ;;
    1) stage_1 ;;
    2) stage_2 ;;
    3) stage_3 ;;
    *) die "Internal error: unexpected stage ${stage}" ;;
esac
