# Brain Gateway - Common Commands & Scripts

Quick reference for common operations. See `CLAUDE.md` for architecture overview.

---

## Docker / Orchestrator

### Rebuild orchestrator after code changes
```bash
cd /opt/gateway_mvp
docker compose down
docker compose build --no-cache orchestrator
docker compose up -d
```

### Quick rebuild (if pip deps unchanged)
```bash
docker compose up -d --build orchestrator
```

### Check orchestrator health
```bash
curl http://localhost:8888/health
```

### View orchestrator logs
```bash
docker logs brain-orchestrator --tail 50 -f
```

### Model-layer compose profile (fresh single-box installs)
The `models` compose profile runs vLLM + Qwen3-TTS + Parakeet STT as containers.
Not used on Helios — that box runs the model layer as host systemd units (see the
Helios Primary Model section below). For a fresh install:
```bash
# Analyze GPU(s) → recommended model config (read-only; append with >> .env)
bash scripts/detect_hardware.sh

# Validate the stanzas without starting anything
docker compose --profile models config

# Bring up the full stack including the model layer
COMPOSE_PROFILES=models docker compose up -d --build

# Once the model containers are healthy, smoke-test them
bash scripts/model_layer_smoketest.sh
```

Full deploy + validation procedure (for a test box): `docs/MODEL_LAYER_BOOT_TEST.md`.

---

## Home Assistant

### Test HA command (structured)
```bash
curl -X POST http://localhost:8888/api/ha/command \
  -H "Authorization: Bearer $API_TOKEN" -H "Content-Type: application/json" \
  -d '{"entity_id": "light.living_room", "service": "turn_on", "data": {"brightness": 128}}'
# or natural language: -d '{"command": "turn off the living room light"}'
# Same guard as the chat tool (ha_guard.py): refusal → 403, HA failure → 502
```

### Test full orchestrator flow
```bash
curl -s http://localhost:8888/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "brain", "messages": [{"role": "user", "content": "Turn on bedroom lights and set to blue at 50%"}]}' | jq .
```

### List HA entities chat/dashboard may control (listed domains, deny list applied)
```bash
curl -H "Authorization: Bearer $API_TOKEN" http://localhost:8888/api/ha/entities | jq .
```

---

## Monitoring

### Start/stop monitoring stack
```bash
cd ~/gateway_nerves
bash scripts/generate-configs.sh   # render configs first — compose bind-mounts them
docker compose -f monitoring/docker-compose.yml --env-file .env -p monitoring up -d    # Start
docker compose -f monitoring/docker-compose.yml --env-file .env -p monitoring down     # Stop
```

### Deploy a Prometheus/Alertmanager/Blackbox config change
```bash
cd ~/gateway_nerves
bash scripts/generate-configs.sh     # render templates + validate (promtool/amtool)
bash scripts/reload-monitoring.sh    # reload Prometheus + Alertmanager + blackbox + verify rules/receivers/modules actually loaded
```
Or just merge to main — CI runs both when `monitoring/` or these scripts change.
Edit the `.template` files, never the renders (`alertmanager/alertmanager.yml` is gitignored — Pushover keys from `.env`).

### Inspect Alertmanager (9093 is loopback-only on Jupiter)
```bash
curl -s localhost:9093/api/v2/status | jq .
docker exec alertmanager amtool alert query
```

### View logs in Grafana
1. Open https://jupiter-amds.tail74fc4a.ts.net:3000 (tailnet) or http://localhost:3000 on Jupiter — login `GF_SECURITY_ADMIN_USER` / `GF_SECURITY_ADMIN_PASSWORD` from `.env`
2. Go to Explore → Select Loki
3. Query: `{container="brain-orchestrator"}`

### Useful Loki queries
```
# All orchestrator logs
{container="brain-orchestrator"}

# Tool calls only
{container="brain-orchestrator"} |~ "tool_call|home_assistant|search_memory|ask_expert"

# Errors only
{container="brain-orchestrator"} |~ "(?i)error|exception|failed"
```

### Hardware audit across cluster
```bash
/opt/gateway_mvp/monitoring/lab_hw_audit.sh
```

---

## Google Calendar

### Run OAuth2 setup (one-time, on Mac)
```bash
python3 -m venv /tmp/google-auth-venv
/tmp/google-auth-venv/bin/pip install google-auth google-auth-oauthlib
/tmp/google-auth-venv/bin/python orchestrator/google_setup.py \
  --credentials credentials/google_credentials.json \
  --token-output credentials/google_token.json
```

### Copy credentials to Helios
```bash
scp credentials/google_credentials.json labadmin@10.0.0.195:/opt/gateway_mvp/credentials/
scp credentials/google_token.json labadmin@10.0.0.195:/opt/gateway_mvp/credentials/
ssh labadmin@10.0.0.195 "cd /opt/gateway_mvp && docker compose restart orchestrator"
```

### Check calendar status
```bash
curl -s http://localhost:8888/health | jq '.calendar'
# {"configured": true, "poll_interval_min": 15, "morning_briefing": "07:30"}
```

### Test calendar via API
```bash
curl -s http://localhost:8888/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "brain", "messages": [{"role": "user", "content": "What is on my calendar this week?"}]}' | jq .
```

---

## Finance (Actual Budget sync)

```bash
# After adding/changing ACTUAL_* in .env (compose interpolation — restart is not enough)
docker compose up -d --force-recreate orchestrator

# Sync status (configured / connected / last_error) and a manual sync (bearer-gated)
curl -s -H "Authorization: Bearer $API_TOKEN" http://localhost:8888/api/finance/sync/status | jq .
curl -s -X POST -H "Authorization: Bearer $API_TOKEN" http://localhost:8888/api/finance/sync | jq .

# Sync logs
docker logs brain-orchestrator 2>&1 | grep '\[ACTUAL\]' | tail -20
```

---

## Helios Primary Model (Qwen3.8-Flash-Next UD-Q3_K_XL MTP via llama.cpp)

Helios (the GPU model layer) is power-tiered — asleep most of the time and woken on demand via an HA smart plug (the orchestrator runs 24/7 on Jupiter). When awake, the primary model serves on port 8080 as `qwen3.8-flash-next-q3kxl` (`llama-server-primary.service` — llama.cpp build 11358 from `/home/labadmin/llama.cpp-mtp`, Qwen3.8-Flash-Next 125B MoE Unsloth UD-Q3_K_XL MTP GGUF, split across GPU0 RTX 5090 + GPU1 RTX PRO 5000, since the 2026-10-24 cutover; repo copy of the unit: `tts/llama-server-primary.service`, whose header comments are authoritative). Loads in 6–9 s from page cache (27 s cold). Needs Jupiter `.env` `VOICE_THINKING_ENABLED=true`. The same endpoint also serves vision (`VISION_*`) and exposes llama.cpp `/metrics`. Single slot (`--parallel 1`): background LLM jobs queue behind chat — watch `llamacpp:requests_deferred`.

### Check via API
```bash
curl -s http://localhost:8888/health | jq .primary_status
curl -s http://10.0.0.195:8080/health    # no key needed: 503 while loading, 200 when ready

# Every other :8080 path needs the brain key (llama-server --api-key-file, 2026-10-27; a key
# the server doesn't require yet is ignored). Run from the repo root on Jupiter: reads
# MODEL_API_KEY from .env without echoing it, and -H @<(...) keeps it out of `ps`.
brain_auth() { printf 'Authorization: Bearer %s\n' "$(grep '^MODEL_API_KEY=' .env | cut -d= -f2-)"; }
curl -s -H @<(brain_auth) http://10.0.0.195:8080/v1/models
```

### Manual start/stop (systemd on Helios, if needed)
```bash
ssh labadmin@10.0.0.195 "sudo systemctl status llama-server-primary"
ssh labadmin@10.0.0.195 "sudo systemctl restart llama-server-primary"

ssh labadmin@10.0.0.195 "journalctl -u llama-server-primary --no-pager -n 100"

# llama.cpp server metrics (queue depth; this build exports no kv_cache_usage_ratio); needs the key —
# brain_auth from "Check via API" above, repo root on Jupiter
curl -s -H @<(brain_auth) http://10.0.0.195:8080/metrics | grep -E 'llamacpp:(requests_deferred|requests_processing)'

# Both GPUs hold the brain; GPU1 also the live TTS (expect ~90 % / ~92 %)
ssh labadmin@10.0.0.195 "nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv"

# Sandbox score
ssh labadmin@10.0.0.195 "systemd-analyze security llama-server-primary"

# Coder (Qwen3-Coder-Next 80B/3B MoE, GPU1) — RETIRED 2026-10-16 (stopped + disabled); the code_agent tool was removed 2026-10-21, so a restored server has no caller. Restore:
# ssh labadmin@10.0.0.195 "sudo systemctl enable --now llama-server-coder"

# Rollback to the 27B Q6_K brain (2026-10-02 → 2026-10-24, GPU0 alone): follow the header of
# tts/llama-server-primary-27b-q6k.service — on Helios swap in
# /etc/systemd/system/llama-server-primary.service.27b-q6k.bak, restart, re-enable qwen-tts;
# on Jupiter set MODEL_NAME/FALLBACK_MODEL_NAME/VISION_MODEL_NAME=qwen3.8-27b-turbo-q6k and
# VOICE_THINKING_ENABLED=false, then docker compose up -d --force-recreate orchestrator.

# Older rollback (vLLM NVFP4 brain, 2026-09-28 → 2026-10-02), then on Jupiter:
#   cp .env.bak-qwen38-nvfp4 .env && docker compose up -d --force-recreate orchestrator
# ssh labadmin@10.0.0.195 "sudo systemctl disable --now llama-server-primary && sudo systemctl enable --now vllm-primary"

# Oldest rollback (vLLM unit → Qwen3.6): on Helios
#   sudo cp /etc/systemd/system/vllm-primary.service.qwen36.bak /etc/systemd/system/vllm-primary.service && sudo systemctl daemon-reload
# then on Jupiter `cp .env.bak-qwen36 .env` and recreate the orchestrator.
```

Garbled output / dropped tool calls: set `--spec-type none` in the unit first (disables MTP, ~105 tok/s). `--enforce-eager` was the vLLM-era lever and does nothing on llama.cpp. Never add `--verbose` or `--slot-save-path` (would log/persist prompt text). Sampling must keep temperature ≤ 1.0 and `repeat_penalty` 1.0 or MTP acceptance collapses. Startup failures (e.g. GPU1 out of memory) fire the Loki alert `BrainFailedToStart`. `vllm-primary.service` (older rollback) and `llama-server.service` (the Qwen3.5-27B primary before 2026-04-26) are disabled but retained on disk. Details: `docs/internal/HELIOS_INFRASTRUCTURE.md`.

---

## Voice / TTS / STT

### Test TTS with Jessica voice (VoxCPM2, live)
```bash
curl -X POST http://10.0.0.195:8004/tts \
  -H "Content-Type: application/json" \
  -d '{"text": "Good morning Nadim!", "voice": "jessica"}' \
  --output test.wav
```

### Manage TTS/STT services on Helios
```bash
# Live TTS is voxcpm-tts (VoxCPM2, :8004, GPU1 — shared with part of the brain).
# qwen-tts (Qwen3-TTS :8002, rollback) is DISABLED since 2026-10-24: it no longer fits on GPU1.
# STT is stt-onnx (Parakeet v2 int8 ONNX, CPU). parakeet-stt (NeMo, GPU) is disabled.
ssh labadmin@10.0.0.195 "sudo systemctl status voxcpm-tts stt-onnx"
ssh labadmin@10.0.0.195 "sudo systemctl restart voxcpm-tts"
ssh labadmin@10.0.0.195 "journalctl -u voxcpm-tts --no-pager -n 50"
```

### Load a new voice clone (Qwen3-TTS rollback only — disabled; VoxCPM2 has no `/voices/load`, see `docs/VOICE_AND_TTS.md`)
```bash
curl -X POST http://10.0.0.195:8002/voices/load \
  -H "Content-Type: application/json" \
  -d '{
    "name": "jessica",
    "ref_audio": "/home/labadmin/tts-voices/jessica_sample.wav",
    "ref_text": "This is a sample sentence read aloud at a natural pace, matching the reference audio above.",
    "description": "Custom voice - warm, energetic narrator"
  }'
```

---

## HTTPS (Tailscale Serve)

### Check status
```bash
ssh labadmin@10.0.0.195 "tailscale serve status"
```

### Enable HTTPS (already running, persists across reboots)
```bash
ssh labadmin@10.0.0.195 "sudo tailscale serve --bg http://localhost:80"
```

### Disable HTTPS
```bash
ssh labadmin@10.0.0.195 "sudo tailscale serve --https=443 off"
```

### Access URL
```
https://helios.tail74fc4a.ts.net/
```

---

## RAG

### Re-index RAG after adding documents
```bash
# Copy new docs to Helios first, then run inside the orchestrator container:
ssh labadmin@10.0.0.195 "docker exec brain-orchestrator python /app/ingest_rag.py \
  --source /rag \
  --persist /chroma/personal_rag \
  --collection personal_rag"

# Restart orchestrator to pick up changes
ssh labadmin@10.0.0.195 "cd /opt/gateway_mvp && docker compose restart orchestrator"
```

### Check RAG doc count
```bash
curl -s http://localhost:8888/health | jq '.rag_documents'
```

---

## Voice Clone Config (Helios)

Location on Helios: `~/tts-voices/voices.json`

```json
{
  "jessica": {
    "ref_audio": "/home/labadmin/tts-voices/jessica_sample.wav",
    "ref_text": "This is a sample sentence read aloud at a natural pace, matching the reference audio above.",
    "description": "Custom voice - warm, energetic narrator"
  }
}
```
