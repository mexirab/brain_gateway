#!/usr/bin/env bash
# model_layer_smoketest.sh — verify the `models`-profile compose stack is
# serving. Run AFTER `docker compose --profile models up -d` once the three
# containers report healthy. Read-only: issues only HTTP health/inference
# probes. Intended for a test box — NOT Helios's live systemd model stack.
#
# Usage:
#   bash scripts/model_layer_smoketest.sh
#   MODEL_HOST=10.0.0.173 bash scripts/model_layer_smoketest.sh   # remote box
set -uo pipefail

HOST="${MODEL_HOST:-127.0.0.1}"
VLLM_PORT="${SERVICE_MODEL_PORT:-8080}"
TTS_PORT="${SERVICE_TTS_PORT:-8002}"
STT_PORT="${SERVICE_STT_PORT:-8003}"
SERVED_NAME="${VLLM_SERVED_NAME:-qwen3.6-27b-int4}"

pass=0
fail=0

# check <label> <curl args...>
# 60s timeout: a cold vLLM first inference (prefill + first decode before the
# KV cache warms) can run well past 30s.
check() {
    local label="$1"
    shift
    if curl -fsS --max-time 60 "$@" >/dev/null 2>&1; then
        echo "  PASS  ${label}"
        pass=$((pass + 1))
    else
        echo "  FAIL  ${label}"
        fail=$((fail + 1))
    fi
}

echo "=== model-layer smoke test — host ${HOST} ==="
echo
echo "[vLLM :${VLLM_PORT}]"
check "vLLM /health"          "http://${HOST}:${VLLM_PORT}/health"
check "vLLM /v1/models"       "http://${HOST}:${VLLM_PORT}/v1/models"
check "vLLM chat completion"  -X POST "http://${HOST}:${VLLM_PORT}/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"${SERVED_NAME}\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":8}"
echo
echo "[Qwen3-TTS :${TTS_PORT}]"
check "TTS /health"           "http://${HOST}:${TTS_PORT}/health"
echo
echo "[Parakeet STT :${STT_PORT}]"
check "STT /health"           "http://${HOST}:${STT_PORT}/health"
echo
echo "=== container status ==="
docker ps --filter "name=vllm-primary" --filter "name=qwen-tts" --filter "name=parakeet-stt" \
    --format "  {{.Names}}\t{{.Status}}" 2>/dev/null || echo "  (docker ps unavailable)"
echo
echo "=== GPU placement (expect vLLM on GPU 0, TTS + STT on GPU 1) ==="
# Plain nvidia-smi: its Processes table has a GPU-index column, so an operator
# can read off which process landed on GPU 0 vs GPU 1.
nvidia-smi 2>/dev/null || echo "  (nvidia-smi unavailable)"
echo
echo "=== RESULT: ${pass} passed, ${fail} failed ==="
[ "${fail}" -eq 0 ]
