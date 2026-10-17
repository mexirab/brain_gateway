#!/usr/bin/env bash
# Make the orchestrator's writable bind-mounts owned by the uid/gid the
# container runs as (non-root since the Dockerfile gained `USER jess`).
#
# The container used to run as root, so everything it ever wrote on the host
# (data/app DBs, the Chroma memory palace, hf_cache, RAG yaml, reminder audio)
# is root-owned and the non-root container can't write it. Docker also creates
# a missing bind-mount source as root, so fresh installs must pre-create them.
#
# Usage (from the repo root):
#   bash scripts/orchestrator_state_perms.sh            # dry run: report only
#   bash scripts/orchestrator_state_perms.sh --apply    # mkdir + chown (sudo)
#
# UID/GID: ORCHESTRATOR_UID / ORCHESTRATOR_GID from .env, else the invoking
# user (SUDO_UID when run under sudo), never 0. With --apply, missing values
# are written to .env so the image build uses the same ids.
#
# Run --apply with the orchestrator STOPPED (or immediately before recreating
# it), so the old root container isn't writing new root-owned files meanwhile.
set -euo pipefail

APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

REPO_ROOT="${JESS_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV_FILE="${REPO_ROOT}/.env"

# Read KEY from .env without sourcing it (values may contain spaces/quotes).
env_get() {
    [ -f "${ENV_FILE}" ] || return 0
    { grep -E "^$1=" "${ENV_FILE}" || true; } | tail -1 | cut -d= -f2- | sed -e 's/[[:space:]]*#.*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

uid="$(env_get ORCHESTRATOR_UID)"
gid="$(env_get ORCHESTRATOR_GID)"
[ -n "${uid}" ] || uid="${SUDO_UID:-$(id -u)}"
[ -n "${gid}" ] || gid="${SUDO_GID:-$(id -g)}"
if [ "${uid}" = "0" ] || [ "${gid}" = "0" ]; then
    echo "Refusing uid/gid 0 — the point is a non-root container. Set ORCHESTRATOR_UID/GID in .env." >&2
    exit 1
fi

root_path="$(env_get GATEWAY_ROOT_PATH)"
root_path="${root_path:-${REPO_ROOT}}"
chroma="$(env_get CHROMA_HOST_PATH)"
rag="$(env_get RAG_BASE)"
docs="$(env_get DOCUMENTS_PATH)"
# Relative defaults in docker-compose.yml resolve against the repo root.
abs() { case "$1" in /*) echo "$1" ;; *) echo "${REPO_ROOT}/${1#./}" ;; esac; }

# Writable bind-mount sources (docker-compose.yml `orchestrator:` volumes).
dirs=(
    "$(abs "${root_path}/data/app")"
    "$(abs "${chroma:-./data/chroma}")"
    "$(abs "${rag:-./data/rag}")"
    "$(abs "${root_path}/data/hf_cache")"
    "$(abs "${root_path}/credentials")"
    "$(abs "${docs:-./data/documents}")"
)

# Named volume for reminder audio (project name = compose project).
project="$(env_get COMPOSE_PROJECT_NAME)"
project="${project:-$(basename "${REPO_ROOT}")}"
audio_vol="${project}_brain-audio"

echo "Target owner: ${uid}:${gid}"
SUDO=""
[ "$(id -u)" = "0" ] || SUDO="sudo"

for d in "${dirs[@]}"; do
    if [ ! -e "${d}" ]; then
        echo "MISSING  ${d}"
        if [ "${APPLY}" = 1 ]; then
            # As the invoking user first so new parent dirs (e.g. data/) aren't
            # root-owned; sudo only if the parent isn't writable.
            mkdir -p "${d}" 2>/dev/null || ${SUDO} mkdir -p "${d}"
        fi
    fi
    if [ -e "${d}" ]; then
        wrong="$(${SUDO} find "${d}" -xdev \( ! -uid "${uid}" -o ! -gid "${gid}" \) 2>/dev/null | wc -l)"
        echo "$( [ "${wrong}" = 0 ] && echo OK || echo FIX ) ${wrong} path(s) not ${uid}:${gid} under ${d}"
        if [ "${APPLY}" = 1 ] && [ "${wrong}" != 0 ]; then
            ${SUDO} chown -R "${uid}:${gid}" "${d}"
        fi
    fi
done

if docker volume inspect "${audio_vol}" >/dev/null 2>&1; then
    mp="$(docker volume inspect -f '{{.Mountpoint}}' "${audio_vol}")"
    wrong="$(${SUDO} find "${mp}" -xdev \( ! -uid "${uid}" -o ! -gid "${gid}" \) 2>/dev/null | wc -l)"
    echo "$( [ "${wrong}" = 0 ] && echo OK || echo FIX ) ${wrong} path(s) not ${uid}:${gid} in volume ${audio_vol}"
    if [ "${APPLY}" = 1 ] && [ "${wrong}" != 0 ]; then
        ${SUDO} chown -R "${uid}:${gid}" "${mp}"
    fi
fi

# Files mounted read-only must still be readable by the container user.
for f in .env user_profile.yaml data/routines.yaml data/palace.yaml; do
    p="$(abs "${root_path}/${f}")"
    [ -e "${p}" ] || continue
    owner="$(stat -c '%u' "${p}")"
    mode="$(stat -c '%a' "${p}")"
    if [ "${owner}" != "${uid}" ] && [ "${mode: -1}" -lt 4 ]; then
        echo "WARN     ${p} is owned by uid ${owner} with mode ${mode} — the container (uid ${uid}) can't read it"
    fi
done

if [ "${APPLY}" = 1 ] && [ -f "${ENV_FILE}" ]; then
    # Write (or fill an empty `KEY=` line) so the image build uses these ids.
    for kv in "ORCHESTRATOR_UID=${uid}" "ORCHESTRATOR_GID=${gid}"; do
        key="${kv%%=*}"
        if [ -z "$(env_get "${key}")" ]; then
            sed -i "/^${key}=/d" "${ENV_FILE}"
            echo "${kv}" >> "${ENV_FILE}"
        fi
    done
fi

[ "${APPLY}" = 1 ] && echo "Done." || echo "Dry run — re-run with --apply to fix."
