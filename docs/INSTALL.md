# Install guide

End-to-end install for a single-box Brain Gateway deployment.

**The fast path** (recommended for almost everyone): `bash install.sh`. The installer handles Docker, the NVIDIA driver, the container toolkit, the full local-AI stack (LLM + TTS + STT + dashboard), and a 2-question CLI wizard at the end. See [The fast path](#the-fast-path).

**The manual path:** if you want to know exactly what's being installed, run a custom subset of steps, or you're on a non-standard config (different Linux distro, pre-existing Docker setup, etc.), follow the step-by-step manual install below.

If you just want hardware-by-hardware recommendations, see [`docs/HARDWARE.md`](HARDWARE.md).

---

## Pre-flight checklist

Before you start (either path), confirm:

- [ ] **OS:** Ubuntu 22.04 LTS or 24.04 LTS (see [HARDWARE.md](HARDWARE.md) for non-Ubuntu notes)
- [ ] **GPU:** at least 20 GiB VRAM (24 GB card class). Below that, manual model selection — see [HARDWARE.md](HARDWARE.md).
- [ ] **Driver:** none yet, or `nvidia-driver-580-open` or newer
- [ ] **Disk:** ~120 GiB free on the partition that holds `/var/lib/docker` and the repo
- [ ] **RAM:** 16 GiB minimum, 32 GiB if you plan to enable the advanced profile or code agent
- [ ] **Network:** the box can reach the public internet to pull container images and HuggingFace models (one-time)
- [ ] **Sudo:** you can `sudo` on the box
- [ ] **Time:** ~30 minutes end-to-end on a fresh box (mostly waiting on container images + model weights to download, ~50 GB the first time). The CLI wizard at the end takes ~30 seconds.

---

## The fast path

```bash
# 1. Clone
git clone https://github.com/mexirab/brain_gateway.git
cd brain_gateway

# 2. Run the installer
bash install.sh
```

The script runs in two stages, separated by one reboot. **You don't have to re-run anything after the reboot** — a bash-profile hook auto-resumes Stage 2 on your next SSH login.

| Stage | What it does |
|-------|--------------|
| **1** | Installs Docker + docker-compose-v2, adds the NVIDIA container toolkit apt repo, installs `nvidia-driver-580-open` + `nvidia-container-toolkit`, configures the runtime, adds you to the `docker` group, installs a bash-profile auto-resume hook, then prompts you to reboot. |
| **2 (auto-resumes on next login)** | Verifies `nvidia-smi` works, smoke-tests Docker+GPU integration, writes `API_TOKEN`, `DASHBOARD_TOKEN`, `JESS_LAN_IP`, `GATEWAY_ROOT_PATH`, and `COMPOSE_PROFILES=models` to `.env`; runs `scripts/detect_hardware.sh` to pick the right vLLM model for your GPU (auto-substitutes `VLLM_MODEL=Qwen/Qwen3-8B-AWQ` + `VLLM_EXTRA_ARGS=--tool-call-parser hermes` + `VLLM_MAX_MODEL_LEN=16384` if your card is below the 20 GiB tier-24 floor); brings up the **full local-AI base**: orchestrator + vLLM (LLM) + qwen-tts (TTS) + parakeet-stt (STT) + dashboard; waits up to 15 min for all four to report healthy (model weights are ~50 GB the first time); removes the auto-resume hook; hands off to the setup CLI. |
| **Setup CLI** | 2 questions: your name, your timezone. Everything else takes auto-defaults — `assistant_name=Jess`, `adhd_mode=true`, `tone=warm`, `TTS_VOICE=aiden`. Saves identity via `/api/config/identity`, marks setup complete (kill switch flips), `docker compose up -d --force-recreate orchestrator` (recreate, not restart — env-file changes need it), prints the dashboard URL **and the auto-generated `DASHBOARD_TOKEN` login password**. ~30 seconds. |

### After install — Jess greets you

When you open the dashboard for the first time and send any message, Jess prepends a one-time welcome listing what's working, which optional integrations aren't yet configured (Home Assistant, ntfy, Pushover, Paperless), and a clickable link to `/settings`. The Settings page is the single configuration surface — no in-chat setup wizard.

This makes the post-install path discoverable without you having to read docs to find the Settings page.

The auto-resume hook is `~/.brain-gateway-resume.sh` plus a single sourcing line appended to `~/.bash_profile`. Both are idempotent. The script file is removed by Stage 2; the sourcing line becomes a harmless no-op.

If you ever need to manually re-run Stage 2 (skipped the auto-resume, killed the script, etc.):

```bash
cd brain_gateway
bash install.sh   # detects the post-reboot marker and continues from Stage 2
```

**To re-run the setup CLI later** (change something after setup is locked):

```bash
# Option A: use the /settings page in the dashboard (recommended)
xdg-open http://<box-ip>:3001/settings

# Option B: re-open the setup CLI by clearing the kill switch first
# (data/app/setup_state.json → set setup_completed: false)
bash scripts/setup.sh
```

**To re-run the installer from scratch** (e.g. after a wipe-test):

```bash
sudo rm -rf /var/lib/brain-gateway-install
rm -f ~/.brain-gateway-resume.sh
bash install.sh
```

---

## The manual path

This is what the installer is doing under the hood. Follow it if you want full control, are debugging a failed install, or are on a non-standard config.

---

## Step 1 — Install the NVIDIA driver

Skip this section if `nvidia-smi` already prints a working table and the driver is 580 or newer.

```bash
# Remove anything older / conflicting (safe no-op if nothing is installed)
sudo apt purge 'nvidia-*' 'libnvidia-*' 2>/dev/null || true
sudo apt autoremove --purge -y

# Install the DKMS variant — rebuilds against your current kernel
sudo apt update
sudo apt install -y nvidia-driver-580-open

# Reboot so the new kernel module loads cleanly
sudo reboot
```

After reboot:

```bash
nvidia-smi   # should print the driver version + your GPU
```

**If you see "Failed to initialize NVML: Driver/library version mismatch":** you have the *prebuilt-module* package version-locked to a kernel that no longer matches the running one. Purge and reinstall the DKMS flavor:

```bash
sudo apt purge 'linux-modules-nvidia-580-open*'
sudo apt install nvidia-driver-580-open
sudo reboot
```

This bit the Uranus boot-test box (kernel 6.8.0-111 vs prebuilt module for 6.8.0-110). It is the single most common install-day NVIDIA failure on reimaged hardware.

---

## Step 2 — Install Docker + NVIDIA container toolkit

Docker:

```bash
# Engine
sudo apt install -y docker.io docker-compose-v2

# Let your user run docker without sudo
sudo usermod -aG docker "$USER"
newgrp docker      # or log out + back in
docker run --rm hello-world   # smoke test
```

NVIDIA container toolkit (lets containers see the GPU):

```bash
# Add the NVIDIA container toolkit repo
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

sudo apt update
sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# Smoke test — should print nvidia-smi from inside a container
docker run --rm --gpus all nvidia/cuda:12.8.1-base-ubuntu24.04 nvidia-smi
```

If the smoke test fails, fix it here before continuing. None of the model containers will boot if the toolkit isn't wired up.

---

## Step 3 — Clone the repo + minimum .env

```bash
sudo mkdir -p /opt/gateway_mvp
sudo chown "$USER:$USER" /opt/gateway_mvp
git clone https://github.com/mexirab/brain_gateway.git /opt/gateway_mvp
cd /opt/gateway_mvp
cp .env.example .env
```

You need two secrets in `.env` before first boot — the orchestrator's API token and the dashboard login password. Generate both:

```bash
python3 -c "import secrets; print('API_TOKEN=' + secrets.token_urlsafe(32))" >> .env
python3 -c "import secrets; print('DASHBOARD_TOKEN=' + secrets.token_urlsafe(24))" >> .env
```

You'll also want to set `GATEWAY_ROOT_PATH=$(pwd)` so the Docker bind-mounts resolve to absolute paths (the `.env.example` default is empty so the `install.sh` path can fill it in). And, optionally, `JESS_LAN_IP=$(ip -4 route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')` so the first-chat welcome renders a clickable `/settings` URL.

Everything else (Home Assistant token, push channel secrets, voice, model) gets entered through the CLI wizard in Step 6 — or wired up after install via the `/settings` page.

---

## Step 4 — Hardware scan (recommended)

Append a tier + model recommendation to your `.env`:

```bash
bash scripts/detect_hardware.sh           # print the analysis
bash scripts/detect_hardware.sh >> .env   # append KEY=value to .env
```

This writes a block to the bottom of `.env`:

```
# --- recommended by scripts/detect_hardware.sh ---
JESS_VRAM_TIER=32
VLLM_MODEL=Lorbus/Qwen3.6-27B-int4-AutoRound
VLLM_QUANTIZATION=auto_round
VLLM_MAX_MODEL_LEN=153600
VLLM_GPU_MEM_UTIL=0.92
```

If your hardware is below the 20 GiB floor, `detect_hardware.sh` comments the recommendation out. In the manual path, set `VLLM_MODEL=Qwen/Qwen3-8B-AWQ` + `VLLM_EXTRA_ARGS=--tool-call-parser hermes` + `VLLM_MAX_MODEL_LEN=16384` yourself before continuing — the `.env.example` Lorbus defaults won't fit and `VLLM_EXTRA_ARGS`'s MTP speculative-config will crash on an 8B model that has no MTP weights. (`install.sh` does this auto-substitution on the fast path.) See [HARDWARE.md](HARDWARE.md) for other picks.

---

## Step 5 — Bring up the core stack

```bash
docker compose up -d
```

This pulls + starts the **default** profile:
- `brain-orchestrator` — FastAPI app, port 8888
- `brain-frontend` — Next.js dashboard, port 3001
- `open-webui` — chat UI, port 80/443
- `redis`, `chromadb`, `searxng`, `wyoming-whisper`, `wyoming-jessica-tts`

It does **not** start (these are opt-in):
- `vllm-primary`, `qwen-tts`, `parakeet-stt` — model layer, behind the `models` profile (Helios runs these as host systemd units; new installs that want them via compose set `COMPOSE_PROFILES=models`)
- `promtail`, `nebula-sync`, `nut-exporter` — operator tooling, behind `advanced`

First-boot will take 5–15 minutes to pull all the container images. Watch progress:

```bash
docker compose logs -f --tail=20
```

Once `brain-orchestrator` reports `healthy`, you're ready for the wizard.

```bash
curl -s http://localhost:8888/health
# {"ok":true,...}
```

---

## Step 6 — Run the express setup wizard

Run the 2-question CLI wizard:

```bash
bash scripts/setup.sh
```

It asks just **your name** and **your timezone**. Everything else takes auto-defaults — `assistant_name=Jess`, `adhd_mode=true`, `tone=warm`, `TTS_VOICE=aiden`. All optional integrations (Home Assistant, ntfy, Pushover, Paperless, selfcare nudge cadence) are configured AFTER setup via the `/settings` page in the dashboard.

What the wizard does:
- Saves identity via `PUT /api/config/identity`.
- Writes `TTS_VOICE` to the `setup_overrides.env` overlay via `POST /api/setup/env`.
- Calls `POST /api/setup/complete` — the wizard's env-write endpoints become **immutable** (`POST /api/setup/env`, `DELETE /api/setup/env`, and `POST /api/setup/env/validate` all return HTTP 410 from this point on).
- Runs `docker compose up -d --force-recreate orchestrator` so the new env values are picked up (`restart` does not re-read `.env`).
- Prints the dashboard URL plus the auto-generated `DASHBOARD_TOKEN` you'll use to log in.

To change anything later, use the `/settings` page in the dashboard. There is no web setup wizard — `frontend/src/app/setup/` and `frontend/src/components/setup/` were deleted in favor of this CLI path.

**Re-run the wizard later?** Edit `data/app/setup_state.json` and set `setup_completed: false`:

```bash
cat data/app/setup_state.json 2>/dev/null
```

---

## Step 7 (optional) — Enable the advanced profile

Some integrations are gated behind the `advanced` Docker Compose profile and/or the `JESS_ADVANCED=true` env flag. Set them in `.env` and bring the stack up again:

```bash
# .env
COMPOSE_PROFILES=advanced
JESS_ADVANCED=true
```

```bash
docker compose up -d
```

This adds:
- `promtail` (ships container logs to a Loki instance you provide via `LOKI_PUSH_URL`)
- `nebula-sync` (multi-instance Pi-hole replication)
- `nut-exporter` (UPS monitoring)
- Owner-specific tools (`code_agent`, `ask_expert`, `query_budget`, `finance_status`, `check_claude_activity`)
- Background jobs (self-audit, training corpus drain)

Most users never need this. The default profile is the recommended starting point.

---

## Step 8 (optional) — HTTPS via Tailscale

Mobile mic access needs HTTPS. The simplest path is Tailscale Serve (free tier):

```bash
# Install Tailscale on the box (one-time)
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

# Expose port 80 over HTTPS at https://<machine>.<tailnet>.ts.net/
sudo tailscale serve --bg http://localhost:80
```

This is the maintainer's deployment (`https://helios.tail74fc4a.ts.net`). Your iPhone / Android can hit the dashboard with a valid cert without opening any port on your router.

Cert renewal is automatic. To regenerate manually:

```bash
sudo tailscale cert --cert-file /opt/gateway_mvp/certs/<host>.crt \
  --key-file /opt/gateway_mvp/certs/<host>.key <host>.<tailnet>.ts.net
```

---

## Step 9 (legacy) — Per-section CLI wizard (now the only wizard)

`scripts/setup.sh` IS the wizard. Step 6 above describes it. There is no web-based setup wizard — `frontend/src/app/setup/` and `frontend/src/components/setup/` were deleted before v1.0.0 in favor of this short CLI flow.

---

## Common failures

| Symptom | Cause | Fix |
|---------|-------|-----|
| `docker: Error response from daemon: could not select device driver "" with capabilities: [[gpu]]` | NVIDIA container toolkit not configured | Re-run `sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker` |
| `Failed to initialize NVML: Driver/library version mismatch` | Prebuilt NVIDIA module pinned to wrong kernel | Purge + reinstall DKMS variant (Step 1, troubleshooting) |
| `Error 804: forward compatibility was attempted on non supported HW` (vLLM on Blackwell) | Driver too old | Install driver 580+ |
| `brain-orchestrator` keeps restarting | `API_TOKEN` not set | Check `.env` has `API_TOKEN=...`; run `docker compose up -d` again |
| Wizard returns 410 on `/api/setup/env` | Setup already marked complete | This is by design (kill switch). Edit `data/app/setup_state.json` → `setup_completed: false` to re-run, or use `/settings`. |
| `qwen-tts` crash-loops with HF 401 | Stale model name in env | Confirm `QWEN_TTS_MODEL=Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice` (or unset to use compose default) |
| Setup CLI can't reach orchestrator | `docker compose up -d` hasn't finished | `docker compose logs orchestrator`; wait until `curl http://localhost:8888/health` returns 200 |
| Below-floor GPU crashes vLLM with `Unsupported speculative method: 'mtp'` | `.env.example` Lorbus defaults not overridden | `install.sh` does this automatically; if hand-installing, also unset `VLLM_EXTRA_ARGS` or set it to `--tool-call-parser hermes` |
| Model takes 2+ minutes to first-respond | Cold start — vLLM is loading 16+ GiB of weights into VRAM | Wait. Subsequent requests are fast. |

---

## What to do after install

- Open the dashboard, click around. Everything is keyboard-accessible.
- Talk to it: web UI, mobile browser, or via the OpenAI-compatible API (`POST /v1/chat/completions` with `Authorization: Bearer $API_TOKEN`).
- For the voice-puck path (ATOM Echo + Wyoming bridges), see [`docs/VOICE_AND_TTS.md`](VOICE_AND_TTS.md).
- For everything Jess can do voice-first, see [`docs/JESS_QUICK_START.md`](JESS_QUICK_START.md).
- To upgrade later, see [`docs/UPGRADE.md`](UPGRADE.md).
