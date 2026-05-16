#!/usr/bin/env bash
# detect_hardware.sh — analyze the box's GPU(s) + RAM and recommend a vLLM
# model configuration for the `models` compose profile.
#
#   bash scripts/detect_hardware.sh           # print the analysis + recommendation
#   bash scripts/detect_hardware.sh >> .env   # append the recommended KEY=value knobs
#
# Human-readable analysis goes to stderr; only the KEY=value recommendation
# goes to stdout, so the `>> .env` form yields a clean env fragment. This is
# the seed of the Phase 3 setup-wizard "choose your model" step.
set -uo pipefail

# Minimum NVIDIA driver branch for the cu128 torch builds the model images use.
MIN_DRIVER_MAJOR=570

emit() { printf '%s\n' "$*"; }       # machine-readable -> stdout
log()  { printf '%s\n' "$*" >&2; }   # human-readable    -> stderr

# --- GPU presence ----------------------------------------------------------
if ! command -v nvidia-smi >/dev/null 2>&1; then
    log "ERROR: nvidia-smi not found — no NVIDIA GPU or driver detected."
    log "The model layer needs an NVIDIA GPU; install the driver + nvidia-container-toolkit."
    exit 1
fi

mapfile -t gpus < <(nvidia-smi --query-gpu=index,name,memory.total \
    --format=csv,noheader,nounits 2>/dev/null)
if [ "${#gpus[@]}" -eq 0 ]; then
    log "ERROR: nvidia-smi ran but reported no GPUs."
    exit 1
fi

# --- enumerate GPUs --------------------------------------------------------
log "=== Hardware analysis ==="
log ""
log "GPUs (${#gpus[@]}):"
max_mib=0
second_mib=0
for row in "${gpus[@]}"; do
    idx=$(printf '%s' "$row"  | cut -d, -f1 | tr -d ' ')
    name=$(printf '%s' "$row" | cut -d, -f2 | sed 's/^ *//;s/ *$//')
    mib=$(printf '%s' "$row"  | cut -d, -f3 | tr -d ' ')
    # nvidia-smi can report [N/A] for memory on virtualized / MIG / failing
    # GPUs — skip any GPU whose memory total isn't a clean integer rather than
    # letting a non-numeric value abort the arithmetic below.
    if ! [[ "$mib" =~ ^[0-9]+$ ]]; then
        log "  [${idx}] ${name} — memory unreadable ('${mib}'), skipping"
        continue
    fi
    log "  [${idx}] ${name} — $(( mib / 1024 )) GiB"
    if [ "$mib" -gt "$max_mib" ]; then
        second_mib=$max_mib
        max_mib=$mib
    elif [ "$mib" -gt "$second_mib" ]; then
        second_mib=$mib
    fi
done

if [ "$max_mib" -eq 0 ]; then
    log ""
    log "ERROR: could not read a usable memory total from any GPU."
    exit 1
fi
max_gib=$(( max_mib / 1024 ))

# --- driver + RAM ----------------------------------------------------------
driver=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 | tr -d ' ')
driver_major=${driver%%.*}
log ""
log "Driver: ${driver:-unknown}"
if [ -z "${driver}" ]; then
    log "  WARNING: could not read the driver version from nvidia-smi."
elif ! [[ "${driver_major}" =~ ^[0-9]+$ ]]; then
    log "  WARNING: could not parse the driver version ('${driver}')."
elif [ "${driver_major}" -lt "${MIN_DRIVER_MAJOR}" ]; then
    log "  WARNING: driver branch ${driver_major} is below ${MIN_DRIVER_MAJOR}. The model"
    log "           images use cu128 torch and may fail at runtime — update the driver."
fi

if [ -r /proc/meminfo ]; then
    ram_gib=$(awk '/^MemTotal:/ {printf "%d", $2/1024/1024}' /proc/meminfo)
    log "System RAM: ${ram_gib} GiB"
fi

# --- tier + recommendation -------------------------------------------------
# Based on the LARGEST single GPU — the default compose pins vLLM to one GPU.
log ""
log "Largest GPU: ${max_gib} GiB (the model recommendation is based on this)."

tier=""
model=""
quant="awq"
maxlen="8192"
gpumem="0.90"
note=""

# Thresholds carry headroom: nvidia-smi reports memory.total in MiB, and
# MiB/1024 truncates a marketed "32GB" card (32607 MiB) to 31 GiB and a
# "24GB" card to 23 GiB. The tier-24 floor is 20 (not 22) so a 24GB card
# whose usable VRAM is shaved by ECC reservation still tiers correctly.
if   [ "${max_gib}" -lt 10 ]; then
    log ""
    log "ERROR: largest GPU has only ${max_gib} GiB — too small to serve a useful LLM."
    exit 1
elif [ "${max_gib}" -lt 20 ]; then
    quant="awq"; maxlen="8192"; gpumem="0.88"
    note="below-floor"
elif [ "${max_gib}" -lt 30 ]; then
    tier="24"; model="Qwen/Qwen3-14B-Instruct-AWQ"
    quant="awq"; maxlen="32768"; gpumem="0.90"
elif [ "${max_gib}" -lt 44 ]; then
    tier="32"; model="Lorbus/Qwen3.6-27B-int4-AutoRound"
    quant="auto_round"; maxlen="153600"; gpumem="0.92"
else
    tier="48"; model="Lorbus/Qwen3.6-27B-int4-AutoRound"
    quant="auto_round"; maxlen="153600"; gpumem="0.93"
    note="vision-capable"
fi

# --- tensor-parallel advisory ---------------------------------------------
# 2+ GPUs where the smaller is >=85% of the largest (within ~15%) => a TP
# pair could run a bigger model.
if [ "${#gpus[@]}" -ge 2 ] && [ "${second_mib}" -gt 0 ] \
   && [ $(( second_mib * 100 / max_mib )) -ge 85 ]; then
    log ""
    log "Multi-GPU: ${#gpus[@]} similar GPUs detected. Tensor-parallel across them"
    log "  could run a larger model (effective VRAM ≈ the sum). The default compose"
    log "  pins vLLM to one GPU — using TP needs a manual edit of the vllm-primary"
    log "  device_ids plus a --tensor-parallel-size flag."
fi

# --- summary ---------------------------------------------------------------
log ""
log "=== Recommendation ==="
if [ "${note}" = "below-floor" ]; then
    log "  Tier:  below the 24 GiB floor — stack runs, but pick a small model yourself"
    log "  Model: a ~7-8B AWQ quant (e.g. an 8B-class Qwen3 AWQ)"
else
    log "  Tier:  ${tier} GiB"
    log "  Model: ${model}"
fi
log "  Quantization: ${quant}   Context: ${maxlen}   GPU mem util: ${gpumem}"
if [ "${note}" = "vision-capable" ]; then
    log "  Vision: feasible at this tier — VISION_ENABLED=true is an option."
fi
log ""
log "Append the recommendation to .env:  bash scripts/detect_hardware.sh >> .env"
log ""

# --- emit machine-readable KEY=value (stdout) ------------------------------
emit "# --- recommended by scripts/detect_hardware.sh ---"
if [ -n "${tier}" ]; then
    emit "JESS_VRAM_TIER=${tier}"
fi
if [ -n "${model}" ]; then
    emit "VLLM_MODEL=${model}"
else
    emit "# VLLM_MODEL=   # ${max_gib} GiB is below the 24GB floor — pick a ~7-8B AWQ model"
fi
emit "VLLM_QUANTIZATION=${quant}"
emit "VLLM_MAX_MODEL_LEN=${maxlen}"
emit "VLLM_GPU_MEM_UTIL=${gpumem}"
