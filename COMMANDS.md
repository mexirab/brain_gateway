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

### Dead-man's switch (Saturn; once installed — `monitoring/deadman/README.md`)
```bash
# On Saturn
systemctl status jupiter-deadman
journalctl -u jupiter-deadman -n 50
python3 -I /opt/jupiter-deadman/jupiter_deadman.py probe   # one probe round of Jupiter, sends nothing
sudo systemctl stop jupiter-deadman    # BEFORE planned Jupiter downtime (never silence Watchdog — that pages)
sudo systemctl start jupiter-deadman   # after; waits 3 min for a fresh heartbeat before it can page
# On Jupiter: are heartbeats arriving? (only Jupiter's address may connect)
curl -s http://10.0.0.58:9466/metrics | grep -E 'heartbeats_total\{result="ok"\}|last_heartbeat'
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

### USB disk dropout / containers on a dead `/mnt` disk (Jupiter)
```bash
sudo systemctl start usb-storage-remount.service   # mount -a, then stop+start containers still on the old disk
journalctl -u usb-storage-restart-stale -n 40      # what it restarted / what failed
journalctl -u usb-storage-stale-check -n 20        # latest 10-min check: which containers are stale
```
Never `docker compose start` a whole project. Manual fallback, verification, install: `monitoring/README.md` → USB Storage.

### Container → host firewall (Jupiter; `jess-docker-input`, observe mode until switched)
```bash
sudo jess-docker-input status                  # script sha256 + both chains with packet counters
sudo jess-docker-input summary --hours 24      # unmatched traffic grouped by network, address, port
sudo jess-docker-input render                  # what it would load (changes nothing)
sudo systemctl reload jess-docker-input        # after editing /etc/jess-docker-input.conf (fails on a bad config, old rules stay)
journalctl -u jess-docker-input -n 30
```
Install, gates before enforce, switch to enforce, rollback: `monitoring/README.md` → Container → host firewall.

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

# Accept a REFUSED sync (last_error starts "refused:" — mass-delete guard) once the deletion
# is confirmed real in Actual. 409 unless the last attempt was a refusal. Not /sync/reset
# (that drops months before the window + every discretionary override).
curl -s -X POST -H "Authorization: Bearer $API_TOKEN" http://localhost:8888/api/finance/sync/accept-deletions | jq .

# After an actualpy bump: the SQLite confinement test (throwaway container, see Common Commands in CLAUDE.md)
#   ... python -m pytest -o asyncio_mode=auto -p no:cacheprovider orchestrator/tests/test_actual_sqlite_confinement.py -q

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

# Both GPUs hold only the brain (262K ctx; the TTS is on a Jupiter RTX 5080): expect GPU0 ~29.1 GiB, GPU1 ~42.9 GiB
# at load, a little more after long prompts. A python process on either GPU = a stray TTS: stop it.
ssh labadmin@10.0.0.195 "nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv"

# Rollback to 64K with the TTS back on Helios GPU1 (Jupiter's voice card unusable): header of tts/llama-server-primary.service

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

### Test TTS with Jessica voice (VoxCPM2 on Jupiter, live)
```bash
# The server admits Docker networks only (a host-side curl times out): test from a container.
docker exec brain-orchestrator python -c "import urllib.request as u; print(u.urlopen('http://host.docker.internal:8004/health', timeout=5).read())"
# Speed / long-text / listening comparison against another server (generic text only; key from
# /etc/voxcpm-tts/api-key): the docker run line in the docstring of scripts/tts_compare.py
```

### Manage the TTS (Jupiter) and STT (Helios)
```bash
# Live TTS is voxcpm-tts (VoxCPM2) on one of Jupiter's RTX 5080s, socket-activated on 172.17.0.1:8004
# (voxcpm-tts.socket). Its journal is not in Loki. On Jupiter:
sudo systemctl status voxcpm-tts.socket voxcpm-tts
sudo systemctl restart voxcpm-tts
journalctl -u voxcpm-tts --no-pager -n 50
nvidia-smi --query-compute-apps=gpu_bus_id,pid,process_name,used_memory --format=csv   # only voxcpm on the voice card

# NVIDIA module for the running + next-boot kernel (hourly timer; NvidiaModuleMissing). Before any reboot:
cat /home/labadmin/node_exporter/textfile_collector/jess_nvidia_modules.prom
sudo apt full-upgrade && sudo systemctl start nvidia-module-check   # if a row is 0

# Helios: STT is stt-onnx (Parakeet v2 int8 ONNX, CPU). parakeet-stt (NeMo, GPU) is disabled.
# voxcpm-tts on Helios is the disabled rollback (refuses to start next to the 262K brain);
# qwen-tts (Qwen3-TTS :8002) is an older rollback, disabled since 2026-10-24.
ssh labadmin@10.0.0.195 "sudo systemctl status stt-onnx"
ssh labadmin@10.0.0.195 "journalctl -u stt-onnx --no-pager -n 50"
```

### Voice satellite (ATOM Echo → Jess; `docs/VOICE_AND_TTS.md` → Voice satellite)
```bash
# Is the bridge connected? Turn outcomes + stage timings (no transcript/reply in logs)
docker logs brain-orchestrator 2>&1 | grep SATELLITE | tail -20

# Connected per device + config problems (skipped VOICE_SATELLITE_DEVICES entries; 0 = fine)
curl -s localhost:9090/api/v1/query --data-urlencode 'query=max by (device)(bgw_satellite_connected)' | jq '.data.result'
curl -s localhost:9090/api/v1/query --data-urlencode 'query=bgw_satellite_config_problems' | jq '.data.result'
docker logs brain-orchestrator 2>&1 | grep 'CONFIG.*VOICE_SATELLITE'

# Connection attempts / turn outcomes per device from Prometheus (loopback on Jupiter); result=duplicate = another device answered
curl -s localhost:9090/api/v1/query --data-urlencode 'query=sum by (device,result)(increase(bgw_satellite_connect_total[1h]))' | jq '.data.result'
curl -s localhost:9090/api/v1/query --data-urlencode 'query=sum by (device,result,wake_word)(increase(bgw_satellite_turn_total[24h]))' | jq '.data.result'
docker logs brain-orchestrator 2>&1 | grep "also heard by" | tail -10

# How listening ended (max_utterance = answers ~15 s late) + the VAD's numbers per run
curl -s localhost:9090/api/v1/query --data-urlencode 'query=sum by (device,reason)(increase(bgw_satellite_listen_end_total[24h]))' | jq '.data.result'
docker logs brain-orchestrator 2>&1 | grep "listening ended" | tail -10

# Long replies (progressive TTS): outcomes (cut short = chunk_failed/stalled/never_read/...) + start/finish lines
curl -s localhost:9090/api/v1/query --data-urlencode 'query=sum by (result)(increase(bgw_tts_progressive_total[24h]))' | jq '.data.result'
docker logs brain-orchestrator 2>&1 | grep TTS-STREAM | tail -10

# New API key (firmware api_encryption_key = VOICE_SATELLITE_API_KEY; one key for every device — rotating = reflash all)
python3 -c 'import base64, os; print(base64.b64encode(os.urandom(32)).decode())'

# Build + OTA flash the firmware (<dir> holds atom-echo-jess.yaml, hey_jess.json/.tflite, secrets.yaml)
docker run --rm -it -v <dir>:/config -v <dir>/.cache:/cache ghcr.io/esphome/esphome:stable \
  -s name <node> run atom-echo-jess.yaml --device <ip>

# After changing any VOICE_SATELLITE_* / VOICE_MATCH_* in .env (VOICE_SATELLITE_DEVICES, VOICE_MATCH_DEVICES: no spaces) — check it sources, then recreate
bash -c 'set -euo pipefail; set -a; source .env' && echo ok
docker compose up -d --force-recreate orchestrator
```

### Voice match (satellite TV filter; `docs/VOICE_AND_TTS.md` → Voice match)
```bash
# Once after the deploy (and after a restore): download the pinned speaker model
docker exec brain-orchestrator python -m orchestrator.voice_match fetch

# Enroll (bridge OFF first: VOICE_SATELLITE_ENABLED=false + recreate; record needs the device's one connection)
docker exec -it brain-orchestrator python -m orchestrator.voice_match record <device-node-name> me1   # then me2, me3; ~20 s each
docker exec brain-orchestrator python -m orchestrator.voice_match enroll owner me1 me2 me3           # >= 0.5 per clip = good; deletes the clips (--keep-clips to keep)

# Voiceprints: list ids / delete one / score a recorded clip at the current threshold
docker exec brain-orchestrator python -m orchestrator.voice_match list
docker exec brain-orchestrator python -m orchestrator.voice_match remove <id>
docker exec brain-orchestrator python -m orchestrator.voice_match score <clip>

# Ready? Verdicts + likely false rejects per device; per-question lines (score, never who)
curl -s localhost:9090/api/v1/query --data-urlencode 'query=bgw_satellite_voice_match_ready' | jq '.data.result'
curl -s localhost:9090/api/v1/query --data-urlencode 'query=sum by (device,result)(increase(bgw_satellite_voice_match_total[24h]))' | jq '.data.result'
curl -s localhost:9090/api/v1/query --data-urlencode 'query=sum by (device)(increase(bgw_satellite_voice_match_retry_total[24h]))' | jq '.data.result'
docker logs brain-orchestrator 2>&1 | grep -i "voice match" | tail -20
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

## Coder box (`jess-coder`; `docs/CODER.md`)

```bash
# Default off; one-time owner setup first: docs/CODER.md -> Setup
jess-coder selftest                 # isolation self-test; must end "selftest: PASS"
jess-coder <name>                   # open/resume a session (Qwen Code, auto-approve inside the box)
jess-coder patch <name> --show      # checked patch + the whole diff; prints the worktree apply commands
jess-coder ls                       # sessions, open boxes, live tokens
cd ~/gateway_nerves && python3 scripts/jess-coder build --if-stale   # rebuild the box image if its inputs changed
docker ps --filter label=jess-coder=1   # open boxes (CoderGuardBlocked: docker kill <name>)
```

---

## Closet monitors kiosk (Jupiter; `scripts/jupiter-kiosk/README.md`)

```bash
systemctl status jupiter-kiosk jupiter-kiosk-x   # browser unit + X unit
journalctl -t jupiter-kiosk -t jupiter-kiosk-x -f   # kiosk.py, X, both preflights
journalctl -u jupiter-kiosk -u jupiter-kiosk-x -b   # systemd's own lines (exit codes, restarts, PAM)
sudo systemctl restart jupiter-kiosk           # after editing /etc/jupiter-kiosk/kiosk.json; restarts X too
sudo systemctl stop jupiter-kiosk && sudo systemctl start getty@tty1   # text console back on tty1
sudo systemctl kill -s USR1 --kill-whom=main jupiter-kiosk   # PNG per screen in /run/user/$(id -u kiosk)/jupiter-kiosk/ (personal data: delete after)
ss -xl | grep nvidia-xdriver                   # must print nothing (X has its own network namespace)
cd ~/gateway_nerves/scripts/jupiter-kiosk && sudo python3 -I make_logins.py   # after changing KIOSK_TOKEN, then restart
sudo nvidia-smi                                # Xorg + chrome on GPU bus 0C only (without sudo: Xorg only)
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

### Adding RAG documents / re-embedding
New or changed files under `RAG_BASE` (on Jupiter) are ingested automatically: the in-process job `rag_ingest_watch` (`orchestrator/rag_ingest.py`) checks every 2 min and re-embeds only files whose hash changed. No command, no restart.

Re-embed the whole collection only after an embedding-model change. `scripts/reindex_rag.py` writes Chroma directly, and Chroma isn't safe for two writers, so back up `CHROMA_HOST_PATH` and stop the orchestrator first (`docs/MEMPALACE.md` → Offline repairs):
```bash
cd /home/labadmin/gateway_nerves
docker compose stop orchestrator && docker compose run --rm --no-deps orchestrator python scripts/reindex_rag.py && docker compose up -d orchestrator
```

### Check RAG doc count
```bash
curl -s http://localhost:8888/health | jq '.rag_documents'
```

---

## Voice Clone Config (Jupiter)

Location: `~/tts-voices/voices.json` on Jupiter (live voice); Helios keeps the same file for the rollback.

```json
{
  "jessica": {
    "ref_audio": "/home/labadmin/tts-voices/jessica_sample.wav",
    "ref_text": "This is a sample sentence read aloud at a natural pace, matching the reference audio above.",
    "description": "Custom voice - warm, energetic narrator"
  }
}
```
