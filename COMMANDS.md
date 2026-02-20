# Brain Gateway - Common Commands & Scripts

Quick reference for common operations. See `CLAUDE.md` for architecture overview.

---

## Docker / Orchestrator

### Rebuild orchestrator after code changes
```bash
cd /opt/jupiter/gateway_mvp
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

---

## RAG

### Re-index RAG after adding documents
```bash
cd /opt/jupiter/gateway_mvp/rag
python ingest_rag.py \
  --source ~/rag/nadim_rag \
  --persist ~/.local/share/chroma/personal_rag \
  --collection nadim_rag

# Restart orchestrator to pick up changes
docker compose restart orchestrator
```

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

### List HA entities available to Nemotron
```bash
curl http://localhost:8888/api/ha/entities | jq .
```

---

## Monitoring

### Start/stop monitoring stack
```bash
cd /opt/jupiter/gateway_mvp/monitoring
docker compose -p monitoring up -d    # Start
docker compose -p monitoring down     # Stop
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
/opt/jupiter/gateway_mvp/monitoring/lab_hw_audit.sh
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

### Copy credentials to Jupiter
```bash
scp credentials/google_credentials.json labadmin@100.102.29.14:/opt/jupiter/gateway_mvp/credentials/
scp credentials/google_token.json labadmin@100.102.29.14:/opt/jupiter/gateway_mvp/credentials/
ssh labadmin@100.102.29.14 "cd /opt/jupiter/gateway_mvp && docker compose restart orchestrator"
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

## Helios (120B Expert Model)

Helios auto-starts on demand and stops after 30 min idle to save ~150W.

### Check status
```bash
./scripts/helios-status.sh
```

### Manual start (for deep conversations)
```bash
./scripts/start-helios.sh
```

### Manual stop (save power)
```bash
./scripts/stop-helios.sh
```

### Check via API
```bash
curl -s http://localhost:8888/health | jq .primary_status
# "online" or "offline (auto-starts on demand)"
```

---

## Voice / TTS / STT

### Test TTS with Jessica voice
```bash
curl -X POST http://10.0.0.173:8002/tts \
  -H "Content-Type: application/json" \
  -d '{"text": "Good morning Nadim!", "voice": "jessica"}' \
  --output test.wav
```

### Manage TTS/STT services on Uranus
```bash
# Check status
ssh nadim@10.0.0.173 "sudo systemctl status qwen-tts whisper-stt"

# Restart services
ssh nadim@10.0.0.173 "sudo systemctl restart qwen-tts whisper-stt"

# View logs
ssh nadim@10.0.0.173 "journalctl -u qwen-tts -f"
ssh nadim@10.0.0.173 "journalctl -u whisper-stt -f"
```

### Load a new voice clone
```bash
curl -X POST http://10.0.0.173:8002/voices/load \
  -H "Content-Type: application/json" \
  -d '{
    "name": "jessica",
    "ref_audio": "/home/nadim/tts-voices/jessica_sample.wav",
    "ref_text": "And trying to get my brain to focus on anything I was not excited about was like trying to nail jello to the wall.",
    "description": "Jessica McCabe - warm, energetic ADHD advocate"
  }'
```

---

## Voice Clone Config (Uranus)

Location: `~/tts-voices/voices.json`

```json
{
  "jessica": {
    "ref_audio": "/home/nadim/tts-voices/jessica_sample.wav",
    "ref_text": "And trying to get my brain to focus on anything I was not excited about was like trying to nail jello to the wall.",
    "description": "Jessica McCabe - warm, energetic ADHD advocate"
  }
}
```
