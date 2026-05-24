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
    CYAN=$'\033[0;36m'; BOLD=$'\033[1m'; NC=$'\033[0m'
else
    RED=""; GREEN=""; YELLOW=""; CYAN=""; BOLD=""; NC=""
fi

# ── Helpers ─────────────────────────────────────────────────────────────────
say()  { printf '%s==>%s %s\n' "${CYAN}" "${NC}" "$*"; }
ok()   { printf '%s✓%s %s\n'   "${GREEN}" "${NC}" "$*"; }
warn() { printf '%s!%s %s\n'   "${YELLOW}" "${NC}" "$*"; }
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
    warn "A reboot is required midway so the new NVIDIA kernel module loads."
    warn "After the box comes back, re-run this script (it'll continue from where it left off)."
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

    echo
    ok "Stage 1 complete."
    echo
    say "${BOLD}REBOOT REQUIRED${NC}"
    echo "    After the box comes back, SSH in and run:"
    echo
    echo "        cd ${REPO_ROOT}"
    echo "        bash install.sh"
    echo
    confirm "Press Enter to reboot now (or Ctrl-C to reboot manually later)"

    sudo reboot
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

    say "Running hardware scan + appending recommendation to .env..."
    if [ -x "${REPO_ROOT}/scripts/detect_hardware.sh" ]; then
        bash "${REPO_ROOT}/scripts/detect_hardware.sh" >> "${ENV_FILE}" || warn "Hardware scan exited non-zero (continuing)"
        ok "Hardware scan complete"
    else
        warn "scripts/detect_hardware.sh not found or not executable; skipping"
    fi

    say "Bringing up the core stack (first run pulls images; can take 5-15 min)..."
    cd "${REPO_ROOT}"
    docker compose up -d

    say "Waiting up to 3 min for the orchestrator to report healthy..."
    local healthy="no"
    local i
    for i in $(seq 1 36); do
        if curl -s --max-time 2 http://localhost:8888/health >/dev/null 2>&1; then
            healthy="yes"
            break
        fi
        sleep 5
    done
    if [ "${healthy}" = "yes" ]; then
        ok "Orchestrator is healthy"
    else
        warn "Orchestrator didn't report healthy in 3 min. Check 'docker compose logs orchestrator'."
        warn "The setup wizard may still work once the orchestrator finishes its startup."
    fi

    local lan_ip
    lan_ip="$(hostname -I | awk '{print $1}')"
    set_marker "complete"

    echo
    ok "${BOLD}Install complete!${NC}"
    echo
    say "Open the setup wizard from any browser on your LAN:"
    echo
    printf '    %shttp://%s:3001/setup%s\n' "${CYAN}" "${lan_ip}" "${NC}"
    echo
    say "Once you've finished the wizard, the dashboard is at:"
    printf '    %shttp://%s:3001/%s\n' "${CYAN}" "${lan_ip}" "${NC}"
    echo
}

# ── Stage 3: already installed ──────────────────────────────────────────────
stage_3() {
    local lan_ip
    lan_ip="$(hostname -I | awk '{print $1}')"

    ok "${BOLD}Brain Gateway is already installed on this box.${NC}"
    echo
    say "Setup wizard:  http://${lan_ip}:3001/setup"
    say "Dashboard:     http://${lan_ip}:3001/"
    say "Health check:  curl -s http://localhost:8888/health"
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
