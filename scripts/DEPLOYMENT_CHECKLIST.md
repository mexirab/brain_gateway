# Hardware Optimization Deployment Checklist

> **⚠️ STALE — v6 hybrid era doc.** References Nemotron-8B, `start-helios.sh`, and Helios-off-by-default — all removed in v7. Use only as a historical reference. Current architecture: single unified model (Qwen3.5-27B) on Helios, always-on, see `CLAUDE.md` and `ARCHITECTURE.md`.

This checklist covers deploying the optimized architecture for voice-first ADHD support.

## Architecture Summary

```
ALWAYS-ON (~200W):
├── Saturn: Nemotron-8B (brain, 95%+ of tasks)
├── Uranus GPU 0: TTS/Jessica (voice output)
├── Uranus GPU 1: Parakeet STT (voice input)
└── Jupiter: Orchestrator + Home Assistant

OFF BY DEFAULT (saves ~150W):
└── Helios: Qwen3-32B Expert (manual start for deep dives)
```

---

## Phase 1: Disable Helios Auto-Start

Run these commands on **Helios (10.0.0.195)**:

```bash
# SSH into Helios
ssh nadim@10.0.0.195

# Stop and disable llama-server
sudo systemctl stop llama-server
sudo systemctl disable llama-server

# Verify
sudo systemctl is-enabled llama-server  # Should show "disabled"
```

**Test from Helios:**
```bash
cd /opt/gateway_mvp/scripts
./helios-status.sh  # Should show "STOPPED"
```

---

## Phase 2: Ensure Saturn Auto-Start

Run on **Saturn (10.0.0.58)**:

```bash
# Copy systemd service file
scp /opt/gateway_mvp/tts/nemotron-vllm.service nadim@10.0.0.58:/tmp/
ssh nadim@10.0.0.58 'sudo mv /tmp/nemotron-vllm.service /etc/systemd/system/'

# Enable and start
ssh nadim@10.0.0.58 'sudo systemctl daemon-reload'
ssh nadim@10.0.0.58 'sudo systemctl enable nemotron-vllm'
ssh nadim@10.0.0.58 'sudo systemctl start nemotron-vllm'

# Verify
ssh nadim@10.0.0.58 'sudo systemctl status nemotron-vllm'
curl http://10.0.0.58:8001/health
```

---

## Phase 3: Ensure Uranus TTS/STT Auto-Start

Verify on **Uranus (10.0.0.173)**:

```bash
# Check services are enabled
ssh nadim@10.0.0.173 'sudo systemctl is-enabled qwen-tts'     # Should be "enabled"
ssh nadim@10.0.0.173 'sudo systemctl is-enabled parakeet-stt'  # Should be "enabled"

# Check they're running
curl http://10.0.0.173:8002/health  # TTS
curl http://10.0.0.173:8003/health  # STT
```

If not enabled:
```bash
ssh nadim@10.0.0.173 'sudo systemctl enable qwen-tts parakeet-stt'
```

---

## Phase 4: Update Monitoring Stack

On **Helios**:

```bash
cd /opt/gateway_mvp/monitoring

# Restart monitoring to pick up new config
docker compose -p monitoring down
docker compose -p monitoring up -d

# Verify blackbox exporter is running
docker logs blackbox-exporter

# Check Grafana dashboard
# Open http://10.0.0.195:3000 (admin/braingw)
# The "Voice Pipeline (Always-On)" row should show service status
```

---

## Phase 5: Configure Home Assistant Voice PE

1. Copy configuration to Home Assistant:
   ```bash
   # Review and add to HA configuration.yaml:
   cat /opt/gateway_mvp/ha_automations/configuration_additions.yaml
   ```

2. Add conversation automations:
   ```bash
   # Review and add to HA automations.yaml:
   cat /opt/gateway_mvp/ha_automations/voice_conversation_automation.yaml
   ```

3. Follow the setup guide:
   ```bash
   cat /opt/gateway_mvp/ha_automations/voice_assistant_setup.md
   ```

4. Restart Home Assistant and configure Voice PE device.

---

## Verification Checklist

### Always-On Services
- [ ] `curl http://10.0.0.195:8888/health` - Orchestrator OK
- [ ] `curl http://10.0.0.58:8001/health` - Nemotron OK
- [ ] `curl http://10.0.0.173:8002/health` - TTS OK
- [ ] `curl http://10.0.0.173:8003/health` - STT OK

### Helios Off by Default
- [ ] `curl http://10.0.0.195:8080/health` - Should fail (service stopped)
- [ ] `./scripts/start-helios.sh` - Should start successfully
- [ ] `./scripts/stop-helios.sh` - Should stop successfully

### Voice PE Pipeline
- [ ] Wake word triggers Voice PE
- [ ] Speech is transcribed (check HA logs)
- [ ] Brain Gateway responds
- [ ] TTS plays response on speaker

### Grafana Dashboard
- [ ] Voice Pipeline row shows all services green
- [ ] Helios shows "OFF (saves 150W)" in blue
- [ ] GPU metrics collecting from Saturn/Uranus

---

## Quick Reference

| Service | Host | Port | Check Command |
|---------|------|------|---------------|
| Orchestrator | Helios | 8888 | `curl http://10.0.0.195:8888/health` |
| Nemotron | Saturn | 8001 | `curl http://10.0.0.58:8001/health` |
| TTS | Uranus | 8002 | `curl http://10.0.0.173:8002/health` |
| STT | Uranus | 8003 | `curl http://10.0.0.173:8003/health` |
| Helios | Helios | 8080 | `curl http://10.0.0.195:8080/health` |

## Power Savings

| Before | After | Savings |
|--------|-------|---------|
| ~350W (all models) | ~200W (voice pipeline only) | ~150W |
| ~$20-25/month | ~$10-15/month | ~$10/month |

---

## Troubleshooting

### Service won't start
```bash
# Check systemd logs
journalctl -u <service-name> -n 100

# Check GPU availability
nvidia-smi
```

### Voice PE not responding
1. Check HA Voice Assistant configuration
2. Verify Wyoming/OpenAI integration settings
3. Test STT directly: `curl -F "file=@test.wav" http://10.0.0.173:8003/v1/audio/transcriptions`

### Helios won't start
```bash
# Check VRAM availability (Qwen3-32B-Q5_K_M needs ~24GB VRAM)
ssh helios 'nvidia-smi'

# Check GPU
ssh helios 'nvidia-smi'

# Check logs
ssh helios 'journalctl -u llama-server -n 100'
```
