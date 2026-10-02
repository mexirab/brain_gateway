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
  -H "Content-Type: application/json" \
  -d '{"entity_id": "light.living_room", "service": "turn_on", "data": {"brightness": 128}}'
```

### Test full orchestrator flow
```bash
curl -s http://localhost:8888/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "brain", "messages": [{"role": "user", "content": "Turn on bedroom lights and set to blue at 50%"}]}' | jq .
```

### List HA entities available to the primary model
```bash
curl http://localhost:8888/api/ha/entities | jq .
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

### Deploy a Prometheus/Alertmanager config change
```bash
cd ~/gateway_nerves
bash scripts/generate-configs.sh     # render templates + validate (promtool/amtool)
bash scripts/reload-monitoring.sh    # reload both + verify rules/receivers actually loaded
```
Or just merge to main — CI runs both when `monitoring/` or these scripts change.
Edit the `.template` files, never the renders (`alertmanager/alertmanager.yml` is gitignored — Pushover keys from `.env`).

### Inspect Alertmanager (9093 is loopback-only on Jupiter)
```bash
curl -s localhost:9093/api/v2/status | jq .
docker exec alertmanager amtool alert query
```

### View logs in Grafana
1. Open http://localhost:3000 (admin/braingw)
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

## Helios Primary Model (Qwen3.8-27B TURBO Q6_K MTP via llama.cpp)

Helios (the GPU model layer) is power-tiered — asleep most of the time and woken on demand via an HA smart plug (the orchestrator runs 24/7 on Jupiter). When awake, the primary model serves on port 8080 as `qwen3.8-27b-turbo-q6k` (`llama-server-primary.service` — llama.cpp build 11358 from `/home/labadmin/llama.cpp-mtp`, DavidAU Qwen3.8-27B TURBO Fable Q6_K MTP GGUF, GPU0 RTX 5090, since the 2026-10-02 cutover; repo copy of the unit: `tts/llama-server-primary.service`, whose header comments are authoritative). Loads in ~4 s from page cache (~60 s cold). The same endpoint also serves vision (`VISION_*`) and exposes llama.cpp `/metrics`. Single slot (`--parallel 1`): background LLM jobs queue behind chat — watch `llamacpp:requests_deferred`.

### Check via API
```bash
curl -s http://localhost:8888/health | jq .primary_status
curl -s http://10.0.0.195:8080/v1/models
```

### Manual start/stop (systemd on Helios, if needed)
```bash
ssh labadmin@10.0.0.195 "sudo systemctl status llama-server-primary"
ssh labadmin@10.0.0.195 "sudo systemctl restart llama-server-primary"

ssh labadmin@10.0.0.195 "journalctl -u llama-server-primary --no-pager -n 100"

# llama.cpp server metrics (queue depth, MTP acceptance, KV usage)
curl -s http://10.0.0.195:8080/metrics | grep -E 'llamacpp:(requests_deferred|requests_processing|kv_cache_usage_ratio)'

# Sandbox score (expect ~3.1)
ssh labadmin@10.0.0.195 "systemd-analyze security llama-server-primary"

# Coder (Qwen3-Coder-Next 80B/3B MoE) on GPU1
ssh labadmin@10.0.0.195 "sudo systemctl status llama-server-coder"

# Rollback to the vLLM NVFP4 brain (2026-09-28 → 2026-10-02), then on Jupiter:
#   cp .env.bak-qwen38-nvfp4 .env && docker compose up -d --force-recreate orchestrator
ssh labadmin@10.0.0.195 "sudo systemctl disable --now llama-server-primary && sudo systemctl enable --now vllm-primary"

# Older rollback (vLLM unit → Qwen3.6): on Helios
#   sudo cp /etc/systemd/system/vllm-primary.service.qwen36.bak /etc/systemd/system/vllm-primary.service && sudo systemctl daemon-reload
# then on Jupiter `cp .env.bak-qwen36 .env` and recreate the orchestrator.
```

Garbled output / dropped tool calls: set `--spec-type none` in the unit first (disables MTP, ~45% slower). `--enforce-eager` was the vLLM-era lever and does nothing on llama.cpp. Never add `--verbose` or `--slot-save-path` (would log/persist prompt text). Sampling must keep temperature ≤ 1.0 and `repeat_penalty` 1.0 or MTP acceptance collapses. `vllm-primary.service` (rollback) and `llama-server.service` (the Qwen3.5-27B primary before 2026-04-26) are disabled but retained on disk. Details: `docs/internal/HELIOS_INFRASTRUCTURE.md`.

---

## Voice / TTS / STT

### Test TTS with Jessica voice
```bash
curl -X POST http://10.0.0.195:8002/tts \
  -H "Content-Type: application/json" \
  -d '{"text": "Good morning Nadim!", "voice": "jessica"}' \
  --output test.wav
```

### Manage TTS/STT services on Helios
```bash
# TTS on Helios GPU1 (RTX PRO 5000, via drop-in qwen-tts.service.d/gpu1.conf);
# STT is stt-onnx (Parakeet v2 int8 ONNX, CPU). parakeet-stt (NeMo, GPU) is disabled.
ssh labadmin@10.0.0.195 "sudo systemctl status qwen-tts stt-onnx"
ssh labadmin@10.0.0.195 "sudo systemctl restart qwen-tts"
ssh labadmin@10.0.0.195 "journalctl -u qwen-tts --no-pager -n 50"
```

### Load a new voice clone
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
