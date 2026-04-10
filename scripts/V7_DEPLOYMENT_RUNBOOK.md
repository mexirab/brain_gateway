# v7 Unified Architecture Deployment Runbook

> **⚠️ HISTORICAL DOC.** This was the one-time runbook for migrating from v6 hybrid to v7 unified back when the stack lived on Jupiter. The stack has since been migrated to Helios (`/opt/helios/gateway_mvp/`) and the primary model is running as Qwen3.5-27B on GPU1 (RTX PRO 5000). Keep as reference only; do not execute.

Migrates Brain Gateway from v6 hybrid (Helios Qwen3-32B + Nemotron-8B) to v7 unified (Qwen3.5-27B primary + Qwen3.5-9B fallback).

**Prerequisites:** PR #12 merged to `main` on Jupiter.

**Estimated time:** ~45 minutes (mostly model downloads)

---

## Phase 1: Download Models on GPU Nodes

### 1A. Helios — Primary Model (Qwen3.5-27B)

SSH into Helios and download the GGUF:

```bash
ssh labadmin@10.0.0.195

# Check current model and VRAM
nvidia-smi
ls -lh ~/models/

# Download Qwen3.5-27B GGUF (Q5_K_M — fits in 32GB VRAM on RTX 5090)
# ~20GB download, ~24GB VRAM when loaded
mkdir -p ~/models
cd ~/models

# Option A: From HuggingFace (use huggingface-cli if installed)
huggingface-cli download Qwen/Qwen3.5-27B-GGUF qwen3.5-27b-q5_k_m.gguf --local-dir .

# Option B: Direct wget (check HF for exact URL)
# wget https://huggingface.co/Qwen/Qwen3.5-27B-GGUF/resolve/main/qwen3.5-27b-q5_k_m.gguf
```

Update the llama-server systemd service to use the new model:

```bash
# Check current service config
sudo systemctl cat llama-server

# Edit the service file
sudo nano /etc/systemd/system/llama-server.service

# Key changes in [Service] ExecStart:
#   OLD: --model /path/to/Qwen3-32B-Q5_K_M.gguf
#   NEW: --model /home/labadmin/models/qwen3.5-27b-q5_k_m.gguf
#
# Recommended llama.cpp flags for Qwen3.5-27B:
#   --model ~/models/qwen3.5-27b-q5_k_m.gguf
#   --host 0.0.0.0
#   --port 8080
#   --n-gpu-layers 99
#   --ctx-size 32768
#   --parallel 2
#   --flash-attn

# Reload systemd
sudo systemctl daemon-reload

# Test start
sudo systemctl start llama-server

# Watch startup logs (model loads in ~30-60 seconds)
journalctl -u llama-server -f
```

Verify the model is serving:

```bash
# Health check
curl -s http://localhost:8080/health | jq .

# Quick inference test
curl -s http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3.5-27b","messages":[{"role":"user","content":"Say hello"}],"max_tokens":50}' | jq .choices[0].message.content

# Verify tool calling works (critical for v7)
curl -s http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.5-27b",
    "messages": [{"role":"user","content":"Turn on the kitchen light"}],
    "tools": [{"type":"function","function":{"name":"home_assistant","description":"Control devices","parameters":{"type":"object","properties":{"entity_id":{"type":"string"},"service":{"type":"string"}}}}}],
    "tool_choice": "auto",
    "max_tokens": 200
  }' | jq .choices[0].message.tool_calls

# Stop it for now (we'll start it properly after Jupiter is updated)
sudo systemctl stop llama-server
```

### 1B. Saturn — Fallback Model (Qwen3.5-9B)

SSH into Saturn and set up the fallback model:

```bash
ssh labadmin@10.0.0.58

# Check current model setup
nvidia-smi
ls -lh ~/models/

# Download Qwen3.5-9B GGUF
# ~7GB download, fits easily on RTX 3090
mkdir -p ~/models
cd ~/models

huggingface-cli download Qwen/Qwen3.5-9B-GGUF qwen3.5-9b-q5_k_m.gguf --local-dir .
```

**If Saturn runs vLLM (for Nemotron):**

```bash
# Check current service
sudo systemctl cat nemotron-vllm

# Option A: Replace Nemotron with Qwen3.5-9B via llama.cpp
# Create a new service file:
sudo tee /etc/systemd/system/llama-fallback.service << 'EOF'
[Unit]
Description=Qwen3.5-9B Fallback Model (llama.cpp)
After=network.target

[Service]
Type=simple
User=labadmin
ExecStart=/usr/local/bin/llama-server \
    --model /home/labadmin/models/qwen3.5-9b-q5_k_m.gguf \
    --host 0.0.0.0 \
    --port 8001 \
    --n-gpu-layers 99 \
    --ctx-size 32768 \
    --parallel 2 \
    --flash-attn
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Stop old Nemotron service (keep it disabled but don't delete — rollback safety)
sudo systemctl stop nemotron-vllm
sudo systemctl disable nemotron-vllm

# Enable and start the new service
sudo systemctl daemon-reload
sudo systemctl enable llama-fallback
sudo systemctl start llama-fallback

# Watch logs
journalctl -u llama-fallback -f
```

**Option B: If Saturn uses llama.cpp already**, just update the model path in the existing service file.

Verify:

```bash
# Health check
curl -s http://localhost:8001/health | jq .

# Quick inference test
curl -s http://localhost:8001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3.5-9b","messages":[{"role":"user","content":"Say hello"}],"max_tokens":50}' | jq .choices[0].message.content
```

---

## Phase 2: Update Jupiter (.env + Rebuild)

### 2A. Pull Latest Code

```bash
ssh labadmin@100.102.29.14  # or labadmin@10.0.0.248

cd /opt/jupiter/gateway_mvp
git checkout main
git pull origin main
```

### 2B. Update .env

```bash
# Back up current .env
cp .env .env.backup.v6

# Edit .env — change these values:
nano .env
```

Key changes:

```bash
# ---- CHANGE THESE ----

# Enable unified v7 architecture
UNIFIED_MODE=true

# Primary model name (must match what llama.cpp reports)
MODEL_NAME=Qwen3.5-27B

# Fallback model name
FALLBACK_MODEL_NAME=Qwen3.5-9B

# Embedding model (new)
EMBEDDING_MODEL=nomic-ai/nomic-embed-text-v2-moe

# ---- VERIFY THESE (should already be correct) ----

# Primary model URL (Helios, port 8080 — unchanged)
MODEL_URL=http://10.0.0.195:8080/v1

# Fallback model URL (Saturn, port 8001 — unchanged)
FALLBACK_MODEL_URL=http://10.0.0.58:8001/v1
```

### 2C. Rebuild Orchestrator + Frontend

```bash
# Rebuild both services
docker compose up -d --build orchestrator frontend

# Watch orchestrator startup
docker logs brain-orchestrator --tail 30 -f
```

Wait for the orchestrator to report healthy. You should see log lines like:
```
INFO: Unified mode enabled — using MODEL_URL for all LLM calls
INFO: Primary model: Qwen3.5-27B at http://10.0.0.195:8080/v1
INFO: Fallback model: Qwen3.5-9B at http://10.0.0.58:8001/v1
```

---

## Phase 3: Start Primary Model on Helios

```bash
# From Jupiter
./scripts/start-helios.sh

# Or manually:
ssh labadmin@10.0.0.195 "sudo systemctl start llama-server"
```

Verify it's reachable from the orchestrator:

```bash
curl -s http://localhost:8888/health | jq '{
  version, architecture, primary_status, fallback_status, tools
}'
```

Expected output:
```json
{
  "version": "7.0",
  "architecture": "unified",
  "primary_status": "online",
  "fallback_status": "online",
  "tools": ["home_assistant", "search_memory", "set_reminder", ...]
}
```

---

## Phase 4: Re-index RAG Embeddings

The new embedding model (nomic-embed-text-v2-moe) produces different vectors than the old one (all-MiniLM-L6-v2). All existing documents must be re-embedded.

```bash
# Run from inside the orchestrator container
docker exec -it brain-orchestrator bash

# Dry run first — see stats without modifying anything
python scripts/reindex_rag.py \
  --persist /home/labadmin/.local/share/chroma/personal_rag \
  --collection nadim_rag \
  --embed-model nomic-ai/nomic-embed-text-v2-moe \
  --dry-run

# If stats look good (154 docs), run for real
python scripts/reindex_rag.py \
  --persist /home/labadmin/.local/share/chroma/personal_rag \
  --collection nadim_rag \
  --embed-model nomic-ai/nomic-embed-text-v2-moe

exit
```

**Note:** First run will download the nomic model (~1.2GB). Subsequent runs use the cached model. Re-indexing 154 docs takes ~30 seconds.

---

## Phase 5: Verification Checklist

Run through all 10 verification items from the plan:

### 5.1 Health Check
```bash
curl -s http://localhost:8888/health | jq .
# Verify: version "7.0", architecture "unified", primary "online"
```

### 5.2 Conversation Test (no tools)
```bash
curl -s http://localhost:8888/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $(grep API_TOKEN .env | cut -d= -f2)" \
  -d '{"messages":[{"role":"user","content":"Hey, how are you?"}],"stream":false}' | jq .choices[0].message.content
# Verify: natural response, no tool calls
```

### 5.3 Single Tool Test
```bash
curl -s http://localhost:8888/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $(grep API_TOKEN .env | cut -d= -f2)" \
  -d '{"messages":[{"role":"user","content":"Turn on the office light"}],"stream":false}' | jq .
# Verify: home_assistant tool called, light turns on
```

### 5.4 Multi-Tool Test
```bash
curl -s http://localhost:8888/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $(grep API_TOKEN .env | cut -d= -f2)" \
  -d '{"messages":[{"role":"user","content":"What is on my calendar today and what is the weather?"}],"stream":false}' | jq .
# Verify: calendar + web_search tools both called
```

### 5.5 RAG Test
```bash
curl -s http://localhost:8888/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $(grep API_TOKEN .env | cut -d= -f2)" \
  -d '{"messages":[{"role":"user","content":"What is my favorite restaurant?"}],"stream":false}' | jq .choices[0].message.content
# Verify: retrieves answer from RAG memory
```

### 5.6 Fallback Test
```bash
# Stop primary model
ssh labadmin@10.0.0.195 "sudo systemctl stop llama-server"

# Wait a few seconds, then test
sleep 5
curl -s http://localhost:8888/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $(grep API_TOKEN .env | cut -d= -f2)" \
  -d '{"messages":[{"role":"user","content":"Hello, are you there?"}],"stream":false}' | jq .choices[0].message.content
# Verify: response comes from fallback (Saturn)

# Restart primary
ssh labadmin@10.0.0.195 "sudo systemctl start llama-server"
```

### 5.7 Frontend Check
```bash
# Open in browser
open http://10.0.0.248:3001/architecture
# Verify: diagram shows "Brain (Qwen3.5-27B)" instead of Helios/Nemotron

open http://10.0.0.248:3001
# Verify: health card shows "Brain: online" and "Fallback: online"
```

### 5.8 Metrics Check
```bash
curl -s http://localhost:8888/metrics | grep -E "bgw_(fallback_online|helios_online|requests_total)"
# Verify: bgw_fallback_online 1.0, bgw_helios_online 1.0

# Check Grafana
open http://localhost:3000/d/brain-gateway-overview
```

### 5.9 Auto-Learn Test
```bash
# Have a conversation, wait 10 minutes, then check
curl -s http://localhost:8888/api/memory/learned/stats \
  -H "Authorization: Bearer $(grep API_TOKEN .env | cut -d= -f2)" | jq .
```

### 5.10 Focus Timer Test
```bash
curl -s http://localhost:8888/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $(grep API_TOKEN .env | cut -d= -f2)" \
  -d '{"messages":[{"role":"user","content":"Start a 25 minute focus session"}],"stream":false}' | jq .
# Verify: focus timer starts, Pi-hole blocking activates
```

---

## Rollback Procedure

If anything goes wrong, revert to v6 in under 2 minutes:

```bash
# On Jupiter
cd /opt/jupiter/gateway_mvp

# Restore v6 .env
cp .env.backup.v6 .env

# Rebuild orchestrator (uses UNIFIED_MODE=false from restored .env)
docker compose up -d --build orchestrator

# On Saturn — restart Nemotron if you replaced it
ssh labadmin@10.0.0.58 "sudo systemctl stop llama-fallback && sudo systemctl start nemotron-vllm"

# On Helios — restore old model in llama-server.service if needed
ssh labadmin@10.0.0.195 "sudo systemctl stop llama-server"
# Edit service file back to Qwen3-32B, then:
ssh labadmin@10.0.0.195 "sudo systemctl daemon-reload && sudo systemctl start llama-server"
```

**Important:** If you rolled back, the RAG embeddings will be from the new model. Either:
- Re-run `reindex_rag.py` with `--embed-model sentence-transformers/all-MiniLM-L6-v2` to restore old embeddings
- Or leave them — they'll still work but with slightly different similarity scores

---

## Post-Deployment Cleanup (After 1 Week Stable)

Once v7 is verified stable in production:

1. **Set `UNIFIED_MODE` default to `true`** in `.env.example`
2. **Remove v6 code paths** (nemotron_loop.py, ask_expert tool, hybrid branches)
3. **Rename `bgw_helios_*` Prometheus metrics** to `bgw_model_*` (coordinate with Grafana dashboards)
4. **Delete old model files** from Helios/Saturn to free disk space
5. **Update `HELIOS_SETUP.md`** and `DEPLOYMENT_CHECKLIST.md` for v7

---

## Quick Reference

| What | Where | Command |
|------|-------|---------|
| Primary model | Helios (10.0.0.195:8080) | `sudo systemctl start/stop llama-server` |
| Fallback model | Saturn (10.0.0.58:8001) | `sudo systemctl start/stop llama-fallback` |
| Orchestrator | Jupiter (10.0.0.248:8888) | `docker compose up -d --build orchestrator` |
| Frontend | Jupiter (10.0.0.248:3001) | `docker compose up -d --build frontend` |
| Health check | Jupiter | `curl http://localhost:8888/health \| jq .` |
| Orchestrator logs | Jupiter | `docker logs brain-orchestrator --tail 50 -f` |
| Re-index RAG | Jupiter (container) | `docker exec brain-orchestrator python scripts/reindex_rag.py ...` |
| Rollback | Jupiter | `cp .env.backup.v6 .env && docker compose up -d --build orchestrator` |
