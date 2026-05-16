#!/usr/bin/env bash
# detect_hardware.sh — classify GPU VRAM into a JESS_VRAM_TIER and suggest a
# primary model. Read-only: queries nvidia-smi and prints to stdout. The Phase 3
# setup wizard consumes this to pre-fill model defaults in .env.
#
# Usage:
#   bash scripts/detect_hardware.sh                 # print to stdout
#   bash scripts/detect_hardware.sh >> .env         # append the knobs to .env
set -euo pipefail

if ! command -v nvidia-smi >/dev/null 2>&1; then
    echo "ERROR: nvidia-smi not found — no NVIDIA GPU or driver detected." >&2
    exit 1
fi

# Largest GPU's total memory, in MiB (multi-GPU boxes tier on the biggest card).
max_mib=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits \
    | tr -d ' ' | sort -rn | head -1)

if [[ -z "${max_mib:-}" ]]; then
    echo "ERROR: could not read GPU memory from nvidia-smi." >&2
    exit 1
fi

gib=$(( max_mib / 1024 ))

# Tier by the floor a card clears (a few GiB of headroom for vendor rounding):
#   24GB → RTX 3090/4090 baseline   32GB → RTX 5090   48GB+ → unlocks vision
if   (( gib >= 44 )); then tier=48
elif (( gib >= 30 )); then tier=32
elif (( gib >= 22 )); then tier=24
else
    echo "ERROR: largest GPU has ${gib} GiB VRAM — below the 24GB floor." >&2
    exit 1
fi

case "${tier}" in
    24) model="Qwen/Qwen3-14B-Instruct-AWQ" ;;
    32) model="Lorbus/Qwen3.6-27B-int4-AutoRound" ;;
    48) model="Lorbus/Qwen3.6-27B-int4-AutoRound" ;;
esac

echo "# Detected GPU: ${gib} GiB VRAM → tier ${tier}"
echo "JESS_VRAM_TIER=${tier}"
echo "VLLM_MODEL=${model}"
