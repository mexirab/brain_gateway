#!/usr/bin/env bash
# ============================================================
# lab_hw_audit.sh  (run this on Voyager)
#
# SSH into each node and print:
#  - GPU(s): NVIDIA (nvidia-smi), AMD (rocm-smi), fallback lspci
#  - RAM: total (GiB)
#  - CPU: model + cores/threads
#
# Modified to work with SSH config aliases (no domain suffix)
# ============================================================

set -euo pipefail

NODES_DEFAULT=(voyager helios jupiter saturn uranus)
NODES=("${NODES_DEFAULT[@]}")

SSH_OPTS=(
  -o BatchMode=yes
  -o ConnectTimeout=5
  -o ServerAliveInterval=5
  -o ServerAliveCountMax=1
  -o StrictHostKeyChecking=accept-new
)

usage() {
  cat <<EOF
Usage: $0 [--nodes "n1 n2 ..."]
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --nodes) shift; [[ $# -gt 0 ]] || { usage; exit 1; }; # shellcheck disable=SC2206
            NODES=($1) ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown arg: $1"; usage; exit 1 ;;
  esac
  shift
done

REMOTE_SCRIPT='
set -euo pipefail

host="$(hostname 2>/dev/null || echo unknown)"

# ---- CPU ----
cpu_model="$(lscpu 2>/dev/null | awk -F: "/Model name/ {gsub(/^[ \t]+/, \"\", \$2); print \$2; exit}" || true)"
cpu_cores="$(lscpu 2>/dev/null | awk -F: "/Core\\(s\\) per socket/ {gsub(/^[ \t]+/, \"\", \$2); print \$2; exit}" || true)"
cpu_sockets="$(lscpu 2>/dev/null | awk -F: "/Socket\\(s\\)/ {gsub(/^[ \t]+/, \"\", \$2); print \$2; exit}" || true)"
cpu_threads="$(lscpu 2>/dev/null | awk -F: "/CPU\\(s\\)/ {gsub(/^[ \t]+/, \"\", \$2); print \$2; exit}" || true)"

if [[ -z "${cpu_model}" ]]; then
  cpu_model="$(awk -F: "/model name/ {gsub(/^[ \t]+/, \"\", \$2); print \$2; exit}" /proc/cpuinfo 2>/dev/null || true)"
  cpu_threads="$(grep -c "^processor" /proc/cpuinfo 2>/dev/null || true)"
  cpu_cores="?"
  cpu_sockets="?"
fi

# ---- RAM ----
ram_gib="$(free -g 2>/dev/null | awk "/Mem:/ {print \$2}" || true)"
if [[ -z "${ram_gib}" ]]; then
  mem_kib="$(awk "/MemTotal/ {print \$2}" /proc/meminfo 2>/dev/null || true)"
  if [[ -n "${mem_kib}" ]]; then
    ram_gib="$(( (mem_kib + 1024*1024 - 1) / (1024*1024) ))"
  else
    ram_gib="?"
  fi
fi

# ---- GPU(s) robust detection ----
gpu_info=""

try_nvidia() {
  command -v nvidia-smi >/dev/null 2>&1 || return 1
  local out
  out="$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || true)"
  [[ -n "${out}" ]] || return 1
  gpu_info="$(echo "${out}" \
    | awk -F, "{gsub(/^[ \t]+|[ \t]+$/, \"\", \$1); gsub(/^[ \t]+|[ \t]+$/, \"\", \$2); print \$1 \" (\" \$2 \")\"}" \
    | paste -sd "; " -)"
  [[ -n "${gpu_info}" ]] || return 1
  return 0
}

try_amd() {
  if command -v rocm-smi >/dev/null 2>&1; then
    local out
    out="$(rocm-smi -i 2>/dev/null || true)"
    [[ -n "${out}" ]] || return 1
    gpu_info="$(echo "${out}" | awk "NF" | paste -sd " | " -)"
    [[ -n "${gpu_info}" ]] || return 1
    return 0
  fi

  if command -v lspci >/dev/null 2>&1; then
    local out
    out="$(lspci 2>/dev/null | grep -Ei "VGA|3D|Display" | grep -Ei "AMD|ATI" || true)"
    [[ -n "${out}" ]] || return 1
    gpu_info="$(echo "${out}" | sed -E "s/^.*: //" | paste -sd "; " -)"
    [[ -n "${gpu_info}" ]] || return 1
    return 0
  fi

  return 1
}

try_generic_lspci() {
  command -v lspci >/dev/null 2>&1 || return 1
  local out
  out="$(lspci 2>/dev/null | grep -Ei "VGA|3D|Display" || true)"
  [[ -n "${out}" ]] || return 1
  gpu_info="$(echo "${out}" | sed -E "s/^.*: //" | paste -sd "; " -)"
  [[ -n "${gpu_info}" ]] || return 1
  return 0
}

# Order: NVIDIA -> AMD -> generic
if ! try_nvidia; then
  if ! try_amd; then
    if ! try_generic_lspci; then
      gpu_info="(no gpu detected or tools missing)"
    fi
  fi
fi

printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
  "${host}" \
  "${gpu_info}" \
  "${ram_gib}" \
  "${cpu_model:-unknown}" \
  "${cpu_cores:-?}" \
  "${cpu_sockets:-?}" \
  "${cpu_threads:-?}"
'

printf "%-12s | %-60s | %-7s | %-40s | %-5s | %-7s | %-7s\n" \
  "NODE" "GPU(s)" "RAM" "CPU" "Cores" "Sockets" "Threads"
printf "%s\n" "$(printf -- '-%.0s' {1..150})"

for node in "${NODES[@]}"; do
  if [[ "${node}" == "voyager" ]] || [[ "$(hostname 2>/dev/null || true)" == "${node}"* ]]; then
    line="$(bash -c "$REMOTE_SCRIPT" 2>/dev/null || true)"
  else
    # Use SSH config alias directly
    line="$(ssh "${SSH_OPTS[@]}" "$node" "bash -c $(printf '%q' "$REMOTE_SCRIPT")" 2>/dev/null || true)"
  fi

  if [[ -z "${line}" ]]; then
    printf "%-12s | %-60s | %-7s | %-40s | %-5s | %-7s | %-7s\n" \
      "${node}" "SSH FAILED / UNREACHABLE" "-" "-" "-" "-" "-"
    continue
  fi

  IFS=$'\t' read -r host gpu ram cpu cores sockets threads <<< "$line"

  gpu_display="$gpu"
  [[ "${#gpu_display}" -gt 60 ]] && gpu_display="${gpu_display:0:57}..."

  cpu_display="$cpu"
  [[ "${#cpu_display}" -gt 40 ]] && cpu_display="${cpu_display:0:37}..."

  printf "%-12s | %-60s | %-7s | %-40s | %-5s | %-7s | %-7s\n" \
    "${host:-$node}" \
    "${gpu_display:-unknown}" \
    "${ram:-?}GiB" \
    "${cpu_display:-unknown}" \
    "${cores:-?}" \
    "${sockets:-?}" \
    "${threads:-?}"
done
