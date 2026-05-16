# Model-layer boot test

Validate the `models`-profile compose stack — `vllm-primary` (LLM),
`qwen-tts`, `parakeet-stt` — actually boots, pins to the right GPUs, passes
healthchecks, and serves its APIs. This is the deploy half of the Phase 1
containerization spike (compose stanzas + Dockerfiles authored in commit
`10a3f72`; see `serialized-twirling-spindle.md`).

## Run this on a test box — never Helios

The `models` profile publishes ports 8080 / 8002 / 8003 and claims both GPUs.
On Helios those are owned by the live systemd units (`vllm-primary.service`,
`qwen-tts.service`, `parakeet-stt.service`) — running this there would
collide. Helios's `.env` keeps `COMPOSE_PROFILES=advanced` (no `models`) for
exactly this reason. Use a separate box.

### Test box notes — Uranus (2× RTX 5080, 16 GB each)

- 16 GB is **below the 24 GB hardware floor**. `scripts/detect_hardware.sh`
  will reject it ("below the 24GB floor") — expected; skip it for this test.
- The default `VLLM_MODEL` (27B int4, ~15 GB) will not fit a single 16 GB
  card. Use a small model for the plumbing test — see step 2. The test
  validates the *containerization* (images run, GPU pinning, healthchecks,
  APIs serve), not the production model tier.

## Prerequisites

- NVIDIA driver + Docker + nvidia-container-toolkit on the test box. Verify:
  `docker run --rm --gpus all nvidia/cuda:12.8.1-runtime-ubuntu24.04 nvidia-smi`
- The repo cloned/pulled on the test box.

## Steps

### 1. Pull the repo
```bash
cd /opt/gateway_mvp && git pull        # or git clone fresh
```

### 2. Write `.env`
```bash
cp .env.example .env
```
Set at least:
```
API_TOKEN=<any-non-empty-string>
COMPOSE_PROFILES=models
MODEL_BIND_ADDR=127.0.0.1            # 0.0.0.0 to probe from another machine
# Small model for a 16 GB test box — must fit one card. Keep it Qwen3-family
# so the --reasoning-parser qwen3 / --tool-call-parser qwen3_coder flags stay
# valid (e.g. an ~8B Qwen3 AWQ quant):
VLLM_MODEL=<small Qwen3 AWQ model>
VLLM_SERVED_NAME=<served name to use in the smoke test>
VLLM_QUANTIZATION=awq
VLLM_MAX_MODEL_LEN=8192
VLLM_GPU_MEM_UTIL=0.90
```
> If vLLM fails at startup complaining about `--speculative-config` / MTP,
> the test model has no multi-token-prediction weights. Comment out the two
> `--speculative-config` lines in the `vllm-primary` `command:` block in
> `docker-compose.yml` for the test — speculative decoding is a perf
> optimization, not needed to validate plumbing. **This is a local test-only
> edit — revert it before committing** (it's tuned for the production model).

### 3. Build the TTS/STT images
```bash
docker compose --profile models build qwen-tts parakeet-stt
```
vLLM uses the upstream `vllm/vllm-openai` image — no build.

### 4. Bring up the model layer
```bash
docker compose --profile models up -d vllm-primary qwen-tts parakeet-stt
```

### 5. Watch first boot
```bash
docker compose logs -f vllm-primary qwen-tts parakeet-stt
```
First boot downloads models from HuggingFace into the `model-hf-cache`
volume. Healthcheck `start_period` is 900s for vLLM, 600s for TTS/STT —
containers show `health: starting` until the model finishes loading.

### 6. Wait for healthy
```bash
docker compose ps
```
Wait until all three report `healthy`.

### 7. Smoke test
```bash
bash scripts/model_layer_smoketest.sh
```
Expect every check `PASS`. Set `VLLM_SERVED_NAME` in the environment to match
your `.env` so the chat-completion probe uses the right model name. The
script also prints `docker ps` status and `nvidia-smi` placement.

### 8. Confirm GPU pinning
In `nvidia-smi`, the vLLM process should sit on GPU 0 and the TTS + STT
processes on GPU 1.

### 9. Tear down
```bash
docker compose --profile models down
```
The `--profile models` flag is required — plain `docker compose down` skips
profile-gated services. Add `-v` only if you also want to drop the
`model-hf-cache` volume (forces a re-download next run).

## What to record

- Did the TTS and STT images build cleanly?
- Did all three containers reach `healthy`? How long did first boot take
  (informs whether the `start_period` windows need tuning)?
- Smoke test: pass/fail per service.
- GPU placement correct — vLLM on GPU 0, TTS + STT on GPU 1?

## Not covered here

Full end-to-end (orchestrator + Open WebUI driving the containerized model
layer), the production 27B model on adequate VRAM, and removing the host
systemd units + repointing `MODEL_URL`/`TTS_URL`/`STT_URL` at compose DNS are
separate Phase 1 follow-ups — see `serialized-twirling-spindle.md`.
