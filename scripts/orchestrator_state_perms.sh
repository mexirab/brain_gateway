#!/usr/bin/env bash
# Make the orchestrator's writable mounts owned by the uid/gid the container
# runs as (non-root since the Dockerfile gained `USER jess`).
#
# The container used to run as root, so everything it ever wrote on the host
# (data/app DBs, the Chroma memory palace, hf_cache, RAG yaml, reminder audio)
# is root-owned and the non-root container can't write it. Docker also creates
# a missing bind-mount source as root, so fresh installs must pre-create them.
#
# Usage (from anywhere; JESS_REPO_ROOT overrides the repo location):
#   bash scripts/orchestrator_state_perms.sh            # dry run: report only
#   bash scripts/orchestrator_state_perms.sh --apply    # mkdir + chown (sudo)
#
# The mount list comes from `docker compose config` — the same resolution
# compose uses (.env, ${VAR} expansion, defaults) — so it can't drift from
# docker-compose.yml. Writable bind mounts + named volumes are fixed;
# read-only bind mounts are only checked for readability.
#
# UID/GID: ORCHESTRATOR_UID / ORCHESTRATOR_GID from .env, else the invoking
# user (SUDO_UID when run under sudo), never 0. With --apply, missing values
# are written to .env so the image build uses the same ids.
#
# On a host where the root container already ran: run --apply immediately
# before the orchestrator is rebuilt as non-root, then once more after (files
# the old container created in between). Rebuild after changing the ids:
#   docker compose up -d --build --force-recreate orchestrator
set -euo pipefail

APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

REPO_ROOT="${JESS_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV_FILE="${REPO_ROOT}/.env"

# Read KEY from .env without sourcing it (values may contain spaces/quotes).
env_get() {
    [ -f "${ENV_FILE}" ] || return 0
    { grep -E "^$1=" "${ENV_FILE}" || true; } | tail -1 | cut -d= -f2- \
        | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

uid="$(env_get ORCHESTRATOR_UID)"
gid="$(env_get ORCHESTRATOR_GID)"
[ -n "${uid}" ] || uid="${SUDO_UID:-$(id -u)}"
[ -n "${gid}" ] || gid="${SUDO_GID:-$(id -g)}"
if ! [[ "${uid}" =~ ^[1-9][0-9]{0,9}$ && "${gid}" =~ ^[1-9][0-9]{0,9}$ ]]; then
    echo "Refusing uid/gid '${uid}:${gid}' — must be a positive number (non-root). Fix ORCHESTRATOR_UID/GID in .env." >&2
    exit 1
fi

# Refuse anything too close to / (a typo like RAG_BASE=/ must not hand the
# whole tree to the container user).
check_path() {
    local p="$1"
    case "${p}" in
        / | /etc* | /usr* | /bin* | /sbin* | /lib* | /boot* | /var | /opt | /root* | /home | /proc* | /sys* | /dev*)
            echo "Refusing to manage '${p}' — not a plausible orchestrator data dir." >&2; return 1 ;;
    esac
    if [ "${p}" = "${HOME}" ] || [ "$(tr -cd / <<<"${p}" | wc -c)" -lt 3 ]; then
        echo "Refusing to manage '${p}' — too close to the filesystem root / a home dir." >&2; return 1
    fi
}

# Mounts as resolved by compose: "<kind>\t<source>" lines, kind = rw | ro | vol.
mounts="$(cd "${REPO_ROOT}" && docker compose config --format json orchestrator 2>/dev/null | python3 -c '
import json, sys
cfg = json.load(sys.stdin)
names = {k: (v or {}).get("name") or k for k, v in (cfg.get("volumes") or {}).items()}
for v in cfg["services"]["orchestrator"].get("volumes", []):
    ro = v.get("read_only", False)
    if v["type"] == "bind":
        print(("ro" if ro else "rw") + "\t" + v["source"])
    elif v["type"] == "volume" and not ro:
        print("vol\t" + names.get(v["source"], v["source"]))
')" || { echo "docker compose config failed — fix .env first." >&2; exit 1; }

echo "Target owner: ${uid}:${gid}"
SUDO=""
if [ "$(id -u)" != "0" ]; then
    # Dry run must not prompt; --apply may.
    if [ "${APPLY}" = 1 ]; then SUDO="sudo"; elif sudo -n true 2>/dev/null; then SUDO="sudo -n"; fi
fi

# Count entries not owned by uid:gid. A vanishing SQLite -journal/-wal file or
# an unreadable subdir must not abort the report (set -e / pipefail).
count_wrong() {
    { ${SUDO} find "$1" -xdev \( ! -uid "${uid}" -o ! -gid "${gid}" \) 2>/dev/null || true; } | wc -l
}
# Same walk as the count: stay on this filesystem, never follow symlinks.
fix_owner() {
    ${SUDO} find "$1" -xdev \( ! -uid "${uid}" -o ! -gid "${gid}" \) -exec chown -h "${uid}:${gid}" {} + 2>/dev/null || true
}

status=0
while IFS=$'\t' read -r kind src; do
    [ -n "${kind}" ] || continue
    case "${kind}" in
        rw)
            [ "${src}" = /dev/null ] && continue
            check_path "${src}" || exit 1
            if [ ! -e "${src}" ]; then
                echo "MISSING  ${src}"
                [ "${APPLY}" = 1 ] || { status=1; continue; }
                # As the invoking user first so new parent dirs (e.g. data/)
                # aren't root-owned; sudo only if the parent isn't writable.
                mkdir -p "${src}" 2>/dev/null || ${SUDO} mkdir -p "${src}"
            fi
            wrong="$(count_wrong "${src}")"
            echo "$([ "${wrong}" = 0 ] && echo OK || echo FIX) ${wrong} path(s) not ${uid}:${gid} under ${src}"
            if [ "${wrong}" != 0 ]; then
                if [ "${APPLY}" = 1 ]; then fix_owner "${src}"; else status=1; fi
            fi
            ;;
        vol)
            docker volume inspect "${src}" >/dev/null 2>&1 || continue  # created on first up, owned by jess
            mp="$(docker volume inspect -f '{{.Mountpoint}}' "${src}")"
            wrong="$(count_wrong "${mp}")"
            echo "$([ "${wrong}" = 0 ] && echo OK || echo FIX) ${wrong} path(s) not ${uid}:${gid} in volume ${src}"
            if [ "${wrong}" != 0 ]; then
                if [ "${APPLY}" = 1 ]; then fix_owner "${mp}"; else status=1; fi
            fi
            ;;
        ro)
            [ "${src}" = /dev/null ] && continue
            [ -e "${src}" ] || continue
            read -r o g m < <(stat -c '%u %g %a' "${src}")
            m="${m: -3}"
            if [ "${o}" = "${uid}" ]; then bit="${m:0:1}"; elif [ "${g}" = "${gid}" ]; then bit="${m:1:1}"; else bit="${m:2:1}"; fi
            if [ "${bit}" -lt 4 ]; then
                echo "WARN     ${src} (owner ${o}:${g}, mode ${m}) isn't readable by ${uid}:${gid} — the container can't read it"
                status=1
            fi
            ;;
    esac
done <<<"${mounts}"

if [ -z "${SUDO}" ] && [ "$(id -u)" != "0" ] && [ "${APPLY}" = 0 ]; then
    echo "(no passwordless sudo: counts only cover paths readable by $(id -un))"
fi

if [ "${APPLY}" = 1 ] && [ -f "${ENV_FILE}" ]; then
    # Write (or fill an empty `KEY=` line) so the image build uses these ids.
    [ -z "$(tail -c1 "${ENV_FILE}")" ] || echo >> "${ENV_FILE}"
    for kv in "ORCHESTRATOR_UID=${uid}" "ORCHESTRATOR_GID=${gid}"; do
        key="${kv%%=*}"
        if [ -z "$(env_get "${key}")" ]; then
            sed -i "/^${key}=/d" "${ENV_FILE}"
            echo "${kv}" >> "${ENV_FILE}"
        fi
    done
    echo "Done."
    exit 0
fi
if [ "${status}" = 0 ]; then echo "All good."; else echo "Dry run — re-run with --apply to fix."; fi
exit "${status}"
